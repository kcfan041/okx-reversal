"""Market-wide entry-rule discovery with time-split validation, day-clustered stats, random and shuffle baselines.

Pipeline
  1. For every USDT swap: compute bar features known at bar close (no lookahead) and forward outcomes for a fixed
     set of exits (fixed hold, or 2xATR stop + time stop) for both sides.
  2. Enumerate rules = family-specific base condition + up to 2 filters, per side; events are de-duplicated with a
     per-instrument cooldown equal to the hold.
  3. Score rules on TRAIN only (first ~6 months), keep the best K per (family, horizon); validate on VALID (next 3
     months); evaluate the survivors once on HOLDOUT (last 3 months).
  4. Compare with a time-shuffled null (same search on circularly shifted outcomes) and same-period random entries.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str, parse_utc_ms
from okxrev.detector import btc_features, prepare, pump_mask, red_mask
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.discover")

BAR = db.BAR_MS
DAY = 86_400_000
EXITS = {
    "15m": {"hold": 3, "stop": None}, "1h": {"hold": 12, "stop": None}, "6h": {"hold": 72, "stop": None},
    "24h": {"hold": 288, "stop": None}, "3d": {"hold": 864, "stop": None},
    "6h_sl2atr": {"hold": 72, "stop": 2.0}, "24h_sl2atr": {"hold": 288, "stop": 2.0},
}
HORIZON_GROUP = {"15m": "短線 15m-1h", "1h": "短線 15m-1h", "6h": "日內 1-24h", "24h": "日內 1-24h",
                 "6h_sl2atr": "日內 1-24h", "24h_sl2atr": "日內 1-24h", "3d": "波段 1-3d"}
SLIP_EXTRA_SMALL = 0.001
MIN_TRAIN_EVENTS = 40
MIN_TRAIN_DAYS = 15
TOP_K = 5
N_SHUFFLE = 30


# ---------------------------------------------------------------- features
def features(g: pd.DataFrame, det: dict[str, Any], breadth: pd.Series | None, list_time: int | None,
             fund_oi: pd.DataFrame | None) -> pd.DataFrame:
    c, h, lo, o, vq = g["close"], g["high"], g["low"], g["open"], g["vol_quote"]
    f = _ColBuilder(g.index)
    f["r1h"] = c / c.shift(12) - 1
    f["r6h"] = c / c.shift(72) - 1
    f["r24h"] = c / c.shift(288) - 1
    f["r3d"] = c / c.shift(864) - 1
    f["ret5"] = c / c.shift(1) - 1
    f["vol_ratio"] = g["vol_ratio"]
    f["vmax_1h"] = g["vol_ratio"].rolling(12, min_periods=1).max()
    hi24, lo24 = h.rolling(288, min_periods=144).max(), lo.rolling(288, min_periods=144).min()
    hi7, lo7 = h.rolling(2016, min_periods=576).max(), lo.rolling(2016, min_periods=576).min()
    f["pos24"] = (c - lo24) / (hi24 - lo24)
    f["pos7d"] = (c - lo7) / (hi7 - lo7)
    f["new_high24"] = c >= h.shift(1).rolling(288, min_periods=144).max()
    f["new_low24"] = c <= lo.shift(1).rolling(288, min_periods=144).min()
    ema = c.ewm(span=288, adjust=False).mean()
    f["dist_ema"] = c / ema - 1
    lr = np.log(c).diff()
    f["vol1d"] = lr.rolling(288, min_periods=144).std()
    f["vol_rank"] = f["vol1d"] / lr.rolling(2016, min_periods=576).std()
    pc = c.shift(1)
    tr = pd.concat([h - lo, (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    f["atr"] = tr.rolling(14, min_periods=14).mean() / c
    f["turnover24"] = vq.rolling(288, min_periods=144).sum()
    red = red_mask(g, det)
    pump = pump_mask(g, det)
    f["pump"] = pump
    f["red_k"] = red
    f["pump_red"] = red & pump
    f["pump_red_24h"] = pd.Series((red & pump).astype(float), index=g.index).rolling(288, min_periods=1).max() > 0
    f["btc_regime"] = g["btc_regime"]
    f["btc_r1h"] = g["btc_ret_1h"]
    f["btc_r4h"] = g["btc_ret_4h"]
    f["breadth"] = breadth.reindex(g.index).to_numpy() if breadth is not None else np.nan
    age = (g.index.to_numpy(np.int64) - list_time) / DAY if list_time else np.full(len(g), np.inf)
    f["age_d"] = age
    if fund_oi is not None:
        f["funding"] = fund_oi["funding"].to_numpy()
        f["oi_chg"] = fund_oi["oi_chg_24h"].to_numpy()
    else:
        f["funding"] = np.nan
        f["oi_chg"] = np.nan
    return f.frame()


class _ColBuilder(dict):
    """Collects columns (Series aligned to `index`, arrays or scalars) and builds the DataFrame once; avoids
    pandas' per-column block insertion, which dominated feature time. Reading back a column returns a Series."""

    def __init__(self, index: pd.Index):
        super().__init__()
        self.index = index

    def __setitem__(self, k: str, v: Any) -> None:
        if isinstance(v, pd.Series):
            v = v.reindex(self.index) if not v.index.equals(self.index) else v
            v = v.to_numpy()
        elif np.isscalar(v) or v is None:
            v = np.full(len(self.index), v, dtype=float if v is None or isinstance(v, float) else object)
        super().__setitem__(k, np.asarray(v))

    def __getitem__(self, k: str) -> pd.Series:
        return pd.Series(super().__getitem__(k), index=self.index, name=k)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame({k: dict.__getitem__(self, k) for k in self.keys()}, index=self.index)


def outcomes(g: pd.DataFrame, atr: np.ndarray, cost: float) -> dict[str, np.ndarray]:
    """Net long returns for each exit (short = -gross - cost, computed separately because stops are asymmetric)."""
    o, h, lo, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(c)
    entry = np.r_[o[1:], np.nan]
    out: dict[str, np.ndarray] = {}
    for name, ex in EXITS.items():
        k = ex["hold"]
        exitp = np.full(n, np.nan)
        exitp[: n - k] = c[k:]
        gl = exitp / entry - 1
        gs = -gl
        if ex["stop"]:
            st = ex["stop"] * atr
            hh = np.full(n, np.nan)
            ll = np.full(n, np.nan)
            from numpy.lib.stride_tricks import sliding_window_view as sw
            if n > k + 1:
                hw = sw(h[1:], k)
                lw = sw(lo[1:], k)
                m = min(len(hw), n)
                hh[:m] = np.nanmax(hw, axis=1)[:m]
                ll[:m] = np.nanmin(lw, axis=1)[:m]
            adv_l = 1 - ll / entry
            adv_s = hh / entry - 1
            gl = np.where(adv_l >= st, -st, gl)
            gs = np.where(adv_s >= st, -st, gs)
        out[f"L_{name}"] = gl - cost
        out[f"S_{name}"] = gs - cost
    return out


# ---------------------------------------------------------------- rules
Cond = Callable[[dict[str, np.ndarray]], np.ndarray]


def gt(k: str, v: float) -> Cond:
    return lambda f: np.nan_to_num(np.asarray(f[k], float), nan=-np.inf) >= v


def lt(k: str, v: float) -> Cond:
    return lambda f: np.nan_to_num(np.asarray(f[k], float), nan=np.inf) <= v


def flag(k: str) -> Cond:
    return lambda f: np.asarray(f[k], dtype=bool)


def eq(k: str, v: str) -> Cond:
    return lambda f: np.asarray(f[k]) == v


@dataclass(frozen=True)
class C:
    key: str
    label: str
    fn: Cond
    group: str


BASES: dict[str, list[tuple[str, C]]] = {
    "pump_reversal": [
        ("short", C("pump_red", "pump 後巨大紅K", flag("pump_red"), "base")),
        ("short", C("pump_red_24h", "近24h出現 pump_red", flag("pump_red_24h"), "base")),
        ("short", C("r24>50_red", "24h漲>50% 且本根紅K≤-5%", lambda f: gt("r24h", 0.5)(f) & lt("ret5", -0.05)(f), "base")),
        ("short", C("r24>30_top", "24h漲>30% 且在24h區間頂部", lambda f: gt("r24h", 0.3)(f) & gt("pos24", 0.9)(f), "base")),
        ("short", C("r6>20_fade", "6h漲>20% 後1h轉跌>3%", lambda f: gt("r6h", 0.2)(f) & lt("r1h", -0.03)(f), "base")),
    ],
    "dip_rebound": [
        ("long", C("r24<-20", "24h跌>20%", lt("r24h", -0.2), "base")),
        ("long", C("r6<-15", "6h跌>15%", lt("r6h", -0.15), "base")),
        ("long", C("r1h<-8_vol", "1h跌>8% 且爆量", lambda f: lt("r1h", -0.08)(f) & gt("vmax_1h", 4)(f), "base")),
        ("long", C("newlow_bot", "創24h新低 且7日區間底部", lambda f: flag("new_low24")(f) & lt("pos7d", 0.1)(f), "base")),
        ("long", C("r3d<-30", "3日跌>30%", lt("r3d", -0.3), "base")),
    ],
    "breakout": [
        ("long", C("newhigh_vol", "創24h新高 且爆量≥4x", lambda f: flag("new_high24")(f) & gt("vol_ratio", 4)(f), "base")),
        ("long", C("r1h>5_vol", "1h漲>5% 且爆量", lambda f: gt("r1h", 0.05)(f) & gt("vmax_1h", 4)(f), "base")),
        ("long", C("r24>20_top", "24h漲>20% 且在區間頂部", lambda f: gt("r24h", 0.2)(f) & gt("pos24", 0.9)(f), "base")),
        ("long", C("ema_break", "站上日均線且波動放大", lambda f: gt("dist_ema", 0.03)(f) & gt("vol_rank", 1.3)(f), "base")),
        ("short", C("newlow_vol", "創24h新低 且爆量≥4x（追空）", lambda f: flag("new_low24")(f) & gt("vol_ratio", 4)(f), "base")),
    ],
    "btc_follow": [
        ("long", C("btc_up1h", "BTC 1h漲>1%", gt("btc_r1h", 0.01), "base")),
        ("short", C("btc_dn1h", "BTC 1h跌>1%", lt("btc_r1h", -0.01), "base")),
        ("long", C("btc_strong", "BTC 強勢（4h>+1%）", eq("btc_regime", "strong"), "base")),
        ("short", C("btc_weak", "BTC 弱勢（4h<-1%）", eq("btc_regime", "weak"), "base")),
        ("long", C("lag_btc_up", "BTC 4h漲>2% 但本幣 4h 未漲", lambda f: gt("btc_r4h", 0.02)(f) & lt("r6h", 0.0)(f), "base")),
        ("short", C("lag_btc_dn", "BTC 4h跌>2% 但本幣未跌", lambda f: lt("btc_r4h", -0.02)(f) & gt("r6h", 0.0)(f), "base")),
    ],
    "new_listing": [
        ("short", C("new_d1", "上市 1 天內", lt("age_d", 1), "base")),
        ("short", C("new_d3", "上市 3 天內", lt("age_d", 3), "base")),
        ("long", C("new_d1_l", "上市 1 天內（做多）", lt("age_d", 1), "base")),
        ("short", C("new_d7_pump", "上市 7 天內 且24h漲>30%", lambda f: lt("age_d", 7)(f) & gt("r24h", 0.3)(f), "base")),
        ("long", C("new_d7_dip", "上市 7 天內 且24h跌>20%", lambda f: lt("age_d", 7)(f) & lt("r24h", -0.2)(f), "base")),
    ],
}

FILTERS: list[C] = [
    C("btc_strong", "BTC 強勢", eq("btc_regime", "strong"), "btc"),
    C("btc_neutral", "BTC 中性", eq("btc_regime", "neutral"), "btc"),
    C("btc_weak", "BTC 弱勢", eq("btc_regime", "weak"), "btc"),
    C("breadth_hi", "全市場多數上漲（>60%）", gt("breadth", 0.6), "breadth"),
    C("breadth_lo", "全市場多數下跌（<40%）", lt("breadth", 0.4), "breadth"),
    C("vol_hi", "波動放大（>1.5 倍週均）", gt("vol_rank", 1.5), "vol"),
    C("vol_lo", "波動收斂（<0.8 倍週均）", lt("vol_rank", 0.8), "vol"),
    C("big_cap", "高成交額（24h > 5千萬U）", gt("turnover24", 5e7), "size"),
    C("small_cap", "低成交額（24h < 5百萬U）", lt("turnover24", 5e6), "size"),
    C("vol_spike", "近1h爆量≥4x", gt("vmax_1h", 4), "volume"),
    C("above_ema", "在日均線之上", gt("dist_ema", 0), "trend"),
    C("below_ema", "在日均線之下", lt("dist_ema", 0), "trend"),
]
SECONDARY: list[C] = [
    C("fund_pos", "資金費率 > 0.03%（多頭擁擠）", gt("funding", 0.0003), "funding"),
    C("fund_neg", "資金費率 < -0.03%（空頭擁擠）", lt("funding", -0.0003), "funding"),
    C("oi_up", "OI 24h 增加 > 20%", gt("oi_chg", 0.2), "oi"),
    C("oi_dn", "OI 24h 減少 > 15%", lt("oi_chg", -0.15), "oi"),
]
MARKET_SECONDARY: list[C] = [
    C("mkt_dn3", "大盤連跌 ≥3 天", gt("mkt_dn", 2.5), "market"),
    C("mkt_up3", "大盤連漲 ≥3 天", gt("mkt_up", 2.5), "market"),
    C("mkt_flat", "大盤沒有連漲／連跌 3 天", lambda f: lt("mkt_dn", 2.5)(f) & lt("mkt_up", 2.5)(f), "market"),
]


@dataclass
class Rule:
    family: str
    side: str
    base: C
    filters: tuple[C, ...]

    @property
    def key(self) -> str:
        return "|".join([self.family, self.side, self.base.key, *sorted(x.key for x in self.filters)])

    @property
    def label(self) -> str:
        s = "做多" if self.side == "long" else "做空"
        return f"{s}：" + " + ".join([self.base.label, *[x.label for x in self.filters]])

    def mask(self, f: dict[str, np.ndarray]) -> np.ndarray:
        m = self.base.fn(f)
        for x in self.filters:
            m = m & x.fn(f)
        return m


def enumerate_rules(families: list[str] | None = None) -> list[Rule]:
    out = []
    for fam, bases in BASES.items():
        if families and fam not in families:
            continue
        for side, b in bases:
            out.append(Rule(fam, side, b, ()))
            for x in FILTERS:
                out.append(Rule(fam, side, b, (x,)))
            for x, y in itertools.combinations(FILTERS, 2):
                if x.group != y.group:
                    out.append(Rule(fam, side, b, (x, y)))
    return out


# ---------------------------------------------------------------- per-instrument pass
FRESH_GAP = 72


def fresh(mask: np.ndarray, gap: int = FRESH_GAP) -> np.ndarray:
    """Indices where the condition is true and was false for the previous `gap` bars (start of a new episode)."""
    cs = np.cumsum(mask.astype(np.int32))
    prev = np.empty_like(cs)
    prev[0] = 0
    prev[1:] = cs[:-1]
    back = np.zeros_like(cs)
    back[gap + 1:] = cs[: len(cs) - gap - 1]
    return np.flatnonzero(mask & ((prev - back) == 0))


def cooldown_first(mask: np.ndarray, gap: int) -> np.ndarray:
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return idx
    keep = [idx[0]]
    for i in idx[1:]:
        if i - keep[-1] > gap:
            keep.append(i)
    return np.asarray(keep)


def market_breadth(cfg: Config, insts: list[str]) -> pd.Series:
    """Share of instruments with positive 24h return at each bar (computed on a 1h grid, ffilled)."""
    acc: dict[int, list[float]] = {}
    with db.open_db(cfg.paths.db, read_only=True) as con:
        df = con.execute(
            "SELECT inst_id, ts, close FROM candles WHERE ts % 3600000 = 0"
        ).df()
    df = df[df["inst_id"].isin(insts)]
    p = df.pivot(index="ts", columns="inst_id", values="close").sort_index()
    r = p / p.shift(24) - 1
    b = (r > 0).sum(axis=1) / r.notna().sum(axis=1)
    b = b.where(r.notna().sum(axis=1) >= 30)
    b.index = b.index.astype("int64") + 3_600_000
    return b


def scan(cfg: Config, rules: list[Rule], since: int | None = None, insts: list[str] | None = None,
         progress: Callable[[int, int], None] | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    det, reg = cfg.detector, cfg.btc_regime
    fee = float(cfg.metrics["fee_per_side"])
    slip = float(cfg.metrics["slippage_per_side"])
    from okxrev.derivs import attach, load_funding, load_oi
    with db.open_db(cfg.paths.db, read_only=True) as con:
        all_insts = db.list_candle_insts(con)
        meta = db.sync_overview(con).set_index("inst_id")
        btc = btc_features(db.load_candles(con, cfg.universe["btc_inst"], since), reg)
    targets = [i for i in (insts or all_insts) if i in set(all_insts)]
    breadth = market_breadth(cfg, targets)
    rows: list[pd.DataFrame] = []
    rnd_rows: list[pd.DataFrame] = []
    rng = np.random.default_rng(7)
    exit_names = list(EXITS)
    base_keys = sorted({(r.base.key, r.side) for r in rules})
    filt_keys = sorted({x.key for r in rules for x in r.filters})
    filt_fn = {x.key: x.fn for r in rules for x in r.filters}
    base_fn = {(r.base.key, r.side): r.base.fn for r in rules}
    rule_parts = [(base_keys.index((r.base.key, r.side)), tuple(filt_keys.index(x.key) for x in r.filters), r.side) for r in rules]
    for n, inst in enumerate(targets, 1):
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=3.0, max_tries=100) as con:
            raw = db.load_candles(con, inst, since)
            fund = load_funding(con, inst)
            oi = load_oi(con, inst)
        if len(raw) < 1200:
            continue
        lt_ = meta.at[inst, "list_time"] if inst in meta.index else None
        lt_ = None if lt_ is None or pd.isna(lt_) else int(lt_)
        g = prepare(raw, det, btc, list_time=lt_)
        fo = attach(g, fund, oi)
        f = features(g, det, breadth, lt_, fo)
        b_ffill = pd.Series(f["breadth"].to_numpy(), index=g.index).ffill(limit=12).to_numpy()
        f["breadth"] = b_ffill
        small = float(np.nanmedian(f["turnover24"].to_numpy())) < 5e6
        cost = 2 * (fee + slip + (SLIP_EXTRA_SMALL if small else 0.0))
        out = outcomes(g, f["atr"].to_numpy(float), cost)
        fa = {k: f[k].to_numpy() for k in f.columns}
        ts = g.index.to_numpy(np.int64)
        valid = ~np.isnan(np.r_[g["open"].to_numpy(float)[1:], np.nan]) & ~np.isnan(fa["r24h"].astype(float))
        bm = [base_fn[k](fa) & valid for k in base_keys]
        fm = [filt_fn[k](fa) for k in filt_keys]
        L = np.vstack([out["L_" + e] for e in exit_names]).T.astype(np.float32)
        S = np.vstack([out["S_" + e] for e in exit_names]).T.astype(np.float32)
        r_idx, e_idx = [], []
        for ri, (bi, fis, side) in enumerate(rule_parts):
            m = bm[bi]
            if not m.any():
                continue
            for fi in fis:
                m = m & fm[fi]
            idx = fresh(m)
            if len(idx):
                r_idx.append(np.full(len(idx), ri, dtype=np.int32))
                e_idx.append(idx)
        if r_idx:
            ri_all = np.concatenate(r_idx)
            ix_all = np.concatenate(e_idx)
            long_side = np.array([rule_parts[i][2] == "long" for i in range(len(rule_parts))])[ri_all]
            R = np.where(long_side[:, None], L[ix_all], S[ix_all])
            df = pd.DataFrame(R, columns=exit_names)
            df.insert(0, "rule", ri_all)
            df.insert(1, "ts", ts[ix_all])
            df.insert(2, "inst", np.int16(n - 1))
            rows.append(df)
        vi = np.flatnonzero(valid)
        if len(vi):
            pick = rng.choice(vi, size=min(60, len(vi)), replace=False)
            d = {"ts": ts[pick], "inst": inst}
            for k_, arr in out.items():
                d[k_] = arr[pick]
            rnd_rows.append(pd.DataFrame(d))
        if progress:
            progress(n, len(targets))
    wide = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["rule", "ts", "inst", *exit_names])
    rnd = pd.concat(rnd_rows, ignore_index=True) if rnd_rows else pd.DataFrame()
    ev = wide.melt(id_vars=["rule", "ts", "inst"], value_vars=exit_names, var_name="exit", value_name="ret").dropna(subset=["ret"])
    ev["exit"] = ev["exit"].astype("category")
    ev["inst"] = np.asarray(targets, dtype=object)[ev["inst"].to_numpy(np.int64)]
    return ev.reset_index(drop=True), {"random": rnd, "n_insts": len(targets)}


# ---------------------------------------------------------------- stats
BOOK_A_EQUITY = 10_000.0
BOOK_A_FRAC = 0.02


def book_a(ret: np.ndarray, ts: np.ndarray) -> float:
    """Return of 'book A' (10,000U, every trade sized 2% of current equity, unlevered), trades applied in entry
    order. Unlike day-averaged stats every trade counts once, so days with many simultaneous signals weigh in
    proportion to how many trades they really produce."""
    if len(ret) == 0:
        return 0.0
    order = np.argsort(ts, kind="stable")
    r = np.clip(np.asarray(ret, float)[order], -1.0, None)
    return float(np.prod(1 + BOOK_A_FRAC * r) - 1)


def day_stats(ret: np.ndarray, ts: np.ndarray, boot: int = 500, seed: int = 0) -> dict[str, Any]:
    if len(ret) == 0:
        return {"n": 0}
    day = ts // DAY
    s = pd.Series(ret).groupby(day)
    dm = s.mean().to_numpy()
    out = {"n": int(len(ret)), "n_days": int(len(dm)), "mean": float(ret.mean()), "median": float(np.median(ret)),
           "win_rate": float((ret > 0).mean()), "day_mean": float(dm.mean()), "pos_days": float((dm > 0).mean()),
           "book_a": book_a(ret, ts)}
    if len(ret) >= 5 and ret.std(ddof=1) > 0:
        out["t_trade"] = float(ret.mean() / (ret.std(ddof=1) / np.sqrt(len(ret))))
    if len(dm) >= 5:
        if boot > 0:
            rng = np.random.default_rng(seed)
            bm = dm[rng.integers(0, len(dm), size=(boot, len(dm)))].mean(axis=1)
            out["ci95"] = [float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))]
        out["t"] = float(dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm)))) if dm.std(ddof=1) > 0 else 0.0
    return out


def split_points(ts_min: int, ts_max: int, train_m: int = 6, valid_m: int = 3) -> tuple[int, int]:
    end = pd.Timestamp(ts_max, unit="ms")
    hold_start = end - pd.DateOffset(months=3)
    valid_start = hold_start - pd.DateOffset(months=valid_m)
    return int(valid_start.value // 1_000_000), int(hold_start.value // 1_000_000)


def score(train: dict[str, Any]) -> float:
    return float(train.get("t", 0.0)) if train.get("n_days", 0) >= MIN_TRAIN_DAYS and train.get("n", 0) >= MIN_TRAIN_EVENTS else -np.inf


def fast_t(df: pd.DataFrame, ret: np.ndarray | None = None) -> pd.DataFrame:
    """Day-clustered t statistic per (rule, exit) without per-group python loops."""
    d = df[["rule", "exit", "ts"]].copy()
    d["ret"] = df["ret"].to_numpy() if ret is None else ret
    d["day"] = d["ts"].to_numpy(np.int64) // DAY
    dm = d.groupby(["rule", "exit", "day"], observed=True)["ret"].agg(["mean", "count"]).reset_index()
    a = dm.groupby(["rule", "exit"], observed=True).agg(n_days=("mean", "size"), dmean=("mean", "mean"), dstd=("mean", "std"), n=("count", "sum")).reset_index()
    a["t"] = a["dmean"] / (a["dstd"] / np.sqrt(a["n_days"]))
    ok = (a["n_days"] >= MIN_TRAIN_DAYS) & (a["n"] >= MIN_TRAIN_EVENTS) & (a["dstd"] > 0)
    a["score"] = np.where(ok, a["t"], -np.inf)
    return a


def select(ev: pd.DataFrame, rules: list[Rule], t_valid: int, t_hold: int) -> pd.DataFrame:
    tr = ev[ev["ts"] < t_valid]
    if tr.empty:
        return pd.DataFrame()
    a = fast_t(tr)
    a = a[np.isfinite(a["score"])].copy()
    a["family"] = [rules[i].family for i in a["rule"]]
    a["hgroup"] = [HORIZON_GROUP[e] for e in a["exit"]]
    top = a.sort_values("score", ascending=False).groupby(["family", "hgroup"]).head(TOP_K)
    trains = []
    for r in top.itertuples(index=False):
        d = tr[(tr["rule"] == r.rule) & (tr["exit"] == r.exit)]
        trains.append(day_stats(d["ret"].to_numpy(), d["ts"].to_numpy(np.int64)))
    top = top.assign(train=trains)
    return top


def null_scores(ev: pd.DataFrame, t_valid: int, n: int = N_SHUFFLE, seed: int = 1) -> np.ndarray:
    """Best train t achievable after circularly shifting each instrument's outcomes in time (keeps the rule
    masks and per-instrument return distribution, breaks the entry-to-outcome link)."""
    tr = ev[ev["ts"] < t_valid].sort_values(["exit", "inst", "ts"]).reset_index(drop=True)
    if tr.empty:
        return np.array([])
    rng = np.random.default_rng(seed)
    grp = tr.groupby(["exit", "inst"], observed=True).indices
    out = []
    for _ in range(n):
        r = tr["ret"].to_numpy().copy()
        for idx in grp.values():
            if len(idx) > 2:
                r[idx] = np.roll(r[idx], int(rng.integers(1, len(idx))))
        a = fast_t(tr, r)
        out.append(float(a["score"].max()))
    return np.asarray(out)


def trade_ok(s: dict[str, Any], b: float | None) -> bool:
    """Per-trade checks added to every verdict: the plain per-trade mean must be positive and above the same-period
    random mean, and book A (each trade 2% of equity, in time order) must end in profit. Day-averaged stats alone
    can be positive while trading every signal loses money (busy days count as one day)."""
    m = s.get("mean")
    return m is not None and m > max(b or 0.0, 0.0) and (s.get("book_a") is None or s["book_a"] > 0)


def verdict(valid: dict[str, Any], hold: dict[str, Any], base_valid: float, base_hold: float, beats_null: bool) -> str:
    def good(s: dict[str, Any], b: float) -> bool:
        ci = s.get("ci95")
        return s.get("n_days", 0) >= 8 and s.get("day_mean", -1) > max(b, 0) and ci is not None and ci[0] > 0 and trade_ok(s, b)
    def pos(s: dict[str, Any], b: float) -> bool:
        return s.get("n", 0) > 0 and s.get("day_mean", -1) > max(b, 0) and trade_ok(s, b)
    if good(valid, base_valid) and good(hold, base_hold) and beats_null:
        return "可用"
    if pos(valid, base_valid) and pos(hold, base_hold):
        return "觀察中"
    return "無效"


def evaluate(cfg: Config, families: list[str] | None = None, since: str | None = None,
             progress: Callable[[int, int], None] | None = None, reuse: bool = False) -> dict[str, Any]:
    t0 = time.monotonic()
    rules = enumerate_rules(families)
    log.info("discover: %d rules x %d exits", len(rules), len(EXITS))
    cache = cfg.paths.results / "discover_events.parquet"
    rcache = cfg.paths.results / "discover_random.parquet"
    if reuse and cache.exists() and rcache.exists():
        ev = pd.read_parquet(cache)
        extra = {"random": pd.read_parquet(rcache), "n_insts": int(ev["inst"].nunique())}
        log.info("reusing cached scan: %d events", len(ev))
    else:
        ev, extra = scan(cfg, rules, parse_utc_ms(since) if since else None, progress=progress)
        cfg.paths.results.mkdir(parents=True, exist_ok=True)
        ev.assign(exit=ev["exit"].astype(str)).to_parquet(cache, index=False)
        extra["random"].to_parquet(rcache, index=False)
        ev["exit"] = ev["exit"].astype("category")
    if ev["exit"].dtype != "category":
        ev["exit"] = ev["exit"].astype("category")
    rnd = extra["random"]
    if ev.empty:
        return {"n_rules": len(rules), "results": [], "note": "no events"}
    ts_min, ts_max = int(ev["ts"].min()), int(ev["ts"].max())
    t_valid, t_hold = split_points(ts_min, ts_max)
    sel = select(ev, rules, t_valid, t_hold)
    log.info("selected %d candidates; shuffle null x%d", len(sel), N_SHUFFLE)
    nulls = null_scores(ev, t_valid)
    null95 = float(np.quantile(nulls, 0.95)) if len(nulls) else np.inf
    results = []
    for r in sel.itertuples(index=False):
        rule = rules[r.rule]
        d = ev[(ev["rule"] == r.rule) & (ev["exit"] == r.exit)]
        va = d[(d["ts"] >= t_valid) & (d["ts"] < t_hold)]
        ho = d[d["ts"] >= t_hold]
        col = ("L_" if rule.side == "long" else "S_") + r.exit
        def base(lo_: int, hi_: int | None) -> float:
            if rnd.empty or col not in rnd:
                return 0.0
            m = (rnd["ts"] >= lo_) & ((rnd["ts"] < hi_) if hi_ else True)
            x = rnd.loc[m, col].dropna()
            return float(x.mean()) if len(x) else 0.0
        bv, bh = base(t_valid, t_hold), base(t_hold, None)
        sv = day_stats(va["ret"].to_numpy(), va["ts"].to_numpy(np.int64))
        sh = day_stats(ho["ret"].to_numpy(), ho["ts"].to_numpy(np.int64))
        months = pd.to_datetime(d["ts"], unit="ms").dt.strftime("%Y-%m")
        by_m = d.groupby(months)["ret"].agg(["mean", "count"])
        results.append({
            "key": rule.key, "family": rule.family, "side": rule.side, "label": rule.label, "exit": r.exit,
            "hgroup": r.hgroup, "conds": [rule.base.key, *[x.key for x in rule.filters]],
            "train": r.train, "valid": sv, "holdout": sh, "base_valid": bv, "base_hold": bh,
            "train_score": float(r.score), "beats_null": bool(r.score > null95),
            "verdict": verdict(sv, sh, bv, bh, bool(r.score > null95)),
            "by_month": [{"m": m, "mean": float(x["mean"]), "n": int(x["count"])} for m, x in by_m.iterrows()],
            "months_pos": float((by_m["mean"] > 0).mean()) if len(by_m) else None,
            "n_insts": int(d["inst"].nunique()),
            "top_insts": d.groupby("inst", observed=True)["ret"].agg(["count", "mean"]).sort_values("count", ascending=False).head(8).reset_index().to_dict("records"),
            "examples": d.sort_values("ts").tail(12)[["inst", "ts", "ret"]].to_dict("records"),
        })
    order = {"可用": 0, "觀察中": 1, "無效": 2}
    results.sort(key=lambda x: (order[x["verdict"]], -x["train_score"]))
    secondary = secondary_filters(cfg, ev, rules, results, t_valid)
    return {
        "generated_at": ms_to_str(db.now_ms(), "%Y-%m-%dT%H:%MZ"), "elapsed_sec": round(time.monotonic() - t0, 1),
        "n_rules": len(rules), "n_exits": len(EXITS), "n_tested": int(ev.groupby(["rule", "exit"]).ngroups), "n_insts": extra["n_insts"],
        "range": [ms_to_str(ts_min), ms_to_str(ts_max)], "split": {"valid_start": ms_to_str(t_valid), "holdout_start": ms_to_str(t_hold)},
        "null": {"n": int(len(nulls)), "p95": null95, "max": float(nulls.max()) if len(nulls) else None},
        "results": results, "secondary": secondary,
        "random_by_exit": {c: float(rnd[c].mean()) for c in rnd.columns if c.startswith(("L_", "S_"))} if not rnd.empty else {},
        "caveats": [
            "訓練段挑出各類別、各持有時間 t 值最高的前 5 條規則，驗證段與保留段完全沒參與挑選；保留段只看一次",
            "信賴區間以「每日平均」做 bootstrap，避免同一天大量同時觸發被當成獨立樣本",
            "洗牌基準：把同一出場的報酬在時間上循環位移後重做同樣挑選，取 95 分位；訓練 t 值需超過它",
            "成本：手續費＋滑價每邊各計，低成交額幣額外加 0.1% 滑價；未計資金費",
            "資金費率／OI 只有近幾個月，僅作為已入選規則的附加篩選，標示樣本短",
            "未納入已下架合約（存活者偏差）",
        ],
    }


def secondary_filters(cfg: Config, ev: pd.DataFrame, rules: list[Rule], results: list[dict[str, Any]], t_valid: int) -> list[dict[str, Any]]:
    """For promising rules, check whether funding / OI conditions improve them (recent data only)."""
    keep = [r for r in results if r["verdict"] != "無效"][:10]
    if not keep:
        return []
    from okxrev.derivs import attach, load_funding, load_oi
    out = []
    need: dict[str, list[int]] = {}
    for r in keep:
        ri = next(i for i, x in enumerate(rules) if x.key == r["key"])
        d = ev[(ev["rule"] == ri) & (ev["exit"] == r["exit"])]
        for inst, x in d.groupby("inst", observed=True):
            need.setdefault(inst, []).extend(x["ts"].tolist())
    try:
        from okxrev import regime
        C_, _ = regime.daily_panel(cfg.paths.db)
        mkt = regime.streaks(regime.alt_index(C_)["ret"])
    except Exception:
        mkt = None
    fo_vals: dict[tuple[str, int], tuple[float, float]] = {}
    with db.open_db(cfg.paths.db, read_only=True) as con:
        for inst, tss in need.items():
            fund, oi = load_funding(con, inst), load_oi(con, inst)
            if fund.empty and oi.empty:
                continue
            idx = pd.Index(np.asarray(sorted(set(tss)), dtype=np.int64))
            a = attach(pd.DataFrame(index=idx), fund, oi)
            for t, f_, o_ in zip(idx, a["funding"], a["oi_chg_24h"]):
                fo_vals[(inst, int(t))] = (f_, o_)
    for r in keep:
        ri = next(i for i, x in enumerate(rules) if x.key == r["key"])
        d = ev[(ev["rule"] == ri) & (ev["exit"] == r["exit"])].copy()
        vals = [fo_vals.get((i, int(t)), (np.nan, np.nan)) for i, t in zip(d["inst"], d["ts"])]
        d["funding"] = [v[0] for v in vals]
        d["oi_chg"] = [v[1] for v in vals]
        has = d[d["funding"].notna() | d["oi_chg"].notna()]
        mrows = []
        if mkt is not None:
            sd = mkt.reindex((d["ts"].to_numpy(np.int64) // DAY) * DAY - DAY)
            fm = {"mkt_dn": sd["dn"].to_numpy(float), "mkt_up": sd["up"].to_numpy(float)}
            for c in MARKET_SECONDARY:
                m = c.fn(fm)
                if m.sum() >= 5:
                    s = day_stats(d.loc[m, "ret"].to_numpy(), d.loc[m, "ts"].to_numpy(np.int64), boot=0)
                    mrows.append({"cond": c.label, **{k: s.get(k) for k in ("n", "n_days", "mean", "win_rate")}})
        if len(has) < 10:
            if mrows:
                all_s = day_stats(d["ret"].to_numpy(), d["ts"].to_numpy(np.int64), boot=0)
                out.append({"label": r["label"], "exit": r["exit"], "n_with_data": int(len(d)), "since": ms_to_str(int(d["ts"].min())),
                            "base": {k: all_s.get(k) for k in ("n", "mean", "win_rate")}, "filters": mrows})
            continue
        fa = {"funding": has["funding"].to_numpy(float), "oi_chg": has["oi_chg"].to_numpy(float)}
        rows = []
        base_s = day_stats(has["ret"].to_numpy(), has["ts"].to_numpy(np.int64), boot=0)
        for c in SECONDARY:
            m = c.fn(fa)
            if m.sum() < 5:
                continue
            s = day_stats(has.loc[m, "ret"].to_numpy(), has.loc[m, "ts"].to_numpy(np.int64), boot=0)
            rows.append({"cond": c.label, **{k: s.get(k) for k in ("n", "n_days", "mean", "win_rate")}})
        rows += mrows
        out.append({"label": r["label"], "exit": r["exit"], "n_with_data": int(len(has)),
                    "since": ms_to_str(int(has["ts"].min())), "base": {k: base_s.get(k) for k in ("n", "mean", "win_rate")}, "filters": rows})
    return out


# ---------------------------------------------------------------- grid direction
def grid_direction(cfg: Config, insts: list[str] | None = None, days: int = 365) -> dict[str, Any]:
    """Simulate a simple 24h grid (range ±1.5×daily vol, 20 levels) started every 6h in long / short / neutral mode,
    and tabulate PnL by entry conditions to learn which direction suits which regime."""
    det, reg = cfg.detector, cfg.btc_regime
    since = db.now_ms() - days * DAY
    with db.open_db(cfg.paths.db, read_only=True) as con:
        all_insts = db.list_candle_insts(con)
        btc = btc_features(db.load_candles(con, cfg.universe["btc_inst"], since), reg)
    targets = insts or all_insts
    fee = float(cfg.metrics["fee_per_side"])
    rows = []
    for inst in targets:
        with db.open_db(cfg.paths.db, read_only=True) as con:
            raw = db.load_candles(con, inst, since)
        if len(raw) < 2000:
            continue
        g = prepare(raw, det, btc)
        c = g["close"].to_numpy(float)
        h, lo = g["high"].to_numpy(float), g["low"].to_numpy(float)
        lr = np.diff(np.log(c), prepend=np.nan)
        vol = pd.Series(lr).rolling(288, min_periods=200).std().to_numpy() * np.sqrt(288)
        r24 = c / np.r_[np.full(288, np.nan), c[:-288]] - 1
        for s in range(300, len(c) - 289, 72):
            p0, v = c[s], vol[s]
            if np.isnan(p0) or np.isnan(v) or v <= 0:
                continue
            width = 1.5 * v
            lo_p, hi_p = p0 * (1 - width), p0 * (1 + width)
            levels = np.linspace(lo_p, hi_p, 21)
            step = levels[1] - levels[0]
            path = c[s:s + 289]
            if np.isnan(path).any():
                continue
            cross = 0
            for a, b in zip(path[:-1], path[1:]):
                cross += int(abs(np.floor((b - lo_p) / step) - np.floor((a - lo_p) / step)))
            grid_pnl = cross * (step / p0) / 20 * 0.5 - cross * fee * 2 / 20
            end = path[-1] / p0 - 1
            broke = bool(np.nanmin(lo[s:s + 289]) < lo_p or np.nanmax(h[s:s + 289]) > hi_p)
            rows.append({"inst": inst, "ts": int(g.index[s]), "r24": r24[s], "btc": g["btc_regime"].iloc[s], "vol": v,
                         "long": grid_pnl + 0.5 * end, "short": grid_pnl - 0.5 * end, "neutral": grid_pnl, "broke": broke, "move": end})
    d = pd.DataFrame(rows)
    if d.empty:
        return {"n": 0}
    d["r24_b"] = pd.cut(d["r24"], [-9, -0.1, -0.03, 0.03, 0.1, 9], labels=["跌>10%", "跌3-10%", "盤整±3%", "漲3-10%", "漲>10%"]).astype(str)
    d["vol_b"] = pd.qcut(d["vol"], 3, labels=["低波動", "中波動", "高波動"]).astype(str)
    t_valid, t_hold = split_points(int(d["ts"].min()), int(d["ts"].max()))
    tables = {}
    for by in ("r24_b", "btc", "vol_b"):
        rows_ = []
        for k, x in d.groupby(by):
            tr, te = x[x["ts"] < t_valid], x[x["ts"] >= t_valid]
            best_tr = max(("long", "short", "neutral"), key=lambda m: tr[m].mean()) if len(tr) else None
            rows_.append({"key": k, "n": int(len(x)), **{m: float(x[m].mean()) for m in ("long", "short", "neutral")},
                          "break_rate": float(x["broke"].mean()), "best_train": best_tr,
                          "best_train_test": float(te[best_tr].mean()) if best_tr and len(te) else None,
                          "neutral_test": float(te["neutral"].mean()) if len(te) else None})
        tables[by] = rows_
    return {"n": int(len(d)), "n_insts": int(d["inst"].nunique()), "tables": tables,
            "overall": {m: float(d[m].mean()) for m in ("long", "short", "neutral")},
            "note": "簡化模擬：每 6 小時啟動一個 24 小時網格，區間 ±1.5 倍日波動、20 格；做多／做空網格各持半倉底倉。"
                    "格子收益假設每次穿越都成交且不計未成交與滑價，絕對數字偏樂觀，請只比較多／空／中性之間的差異",
            "split": {"valid_start": ms_to_str(t_valid)}}


# ---------------------------------------------------------------- live scanner
def live_scan(cfg: Config, res: dict[str, Any] | None = None, lookback_bars: int = 3) -> dict[str, Any]:
    res = res or load(cfg)
    if not res:
        return {"ok": False, "message": "尚未執行進場探索"}
    active = [r for r in res.get("results", []) if r["verdict"] in ("可用", "觀察中")]
    if not active:
        return {"ok": True, "rules": 0, "hits": [], "message": "目前沒有通過驗證的規則，因此不產生訊號"}
    rules = {x.key: x for x in enumerate_rules()}
    det, reg = cfg.detector, cfg.btc_regime
    since = db.now_ms() - 12 * DAY
    from okxrev.derivs import attach, load_funding, load_oi
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        insts = db.list_candle_insts(con)
        meta = db.sync_overview(con).set_index("inst_id")
        btc = btc_features(db.load_candles(con, cfg.universe["btc_inst"], since - 2 * DAY), reg)
        df = con.execute("SELECT inst_id, ts, open, high, low, close, vol, vol_ccy, vol_quote FROM candles WHERE ts >= ? ORDER BY inst_id, ts",
                         [since]).df()
        try:
            fund_all = con.execute("SELECT inst_id, ts, rate FROM funding WHERE ts >= ?", [since - 2 * DAY]).df()
            oi_all = con.execute("SELECT inst_id, period, ts, oi_usd FROM open_interest WHERE ts >= ?", [since - 4 * DAY]).df()
        except Exception:
            fund_all = pd.DataFrame(columns=["inst_id", "ts", "rate"])
            oi_all = pd.DataFrame(columns=["inst_id", "period", "ts", "oi_usd"])
    breadth = market_breadth_recent(cfg, insts, since)
    by_inst = dict(tuple(df.groupby("inst_id")))
    fund_g = dict(tuple(fund_all.groupby("inst_id")))
    oi_g = dict(tuple(oi_all.groupby("inst_id")))
    hits = []
    for inst in insts:
        raw = by_inst.get(inst)
        if raw is None:
            continue
        raw = raw.drop(columns="inst_id").reset_index(drop=True)
        fund = fund_g.get(inst, pd.DataFrame(columns=["ts", "rate"]))
        oi = oi_g.get(inst, pd.DataFrame(columns=["period", "ts", "oi_usd"]))
        if len(raw) < 900:
            continue
        lt_ = meta.at[inst, "list_time"] if inst in meta.index else None
        lt_ = None if lt_ is None or pd.isna(lt_) else int(lt_)
        g = prepare(raw, det, btc, list_time=lt_)
        f = features(g, det, breadth, lt_, attach(g, fund, oi))
        f["breadth"] = pd.Series(f["breadth"].to_numpy(), index=g.index).ffill(limit=12).to_numpy()
        tail = f.iloc[-lookback_bars:]
        fa = {k: tail[k].to_numpy() for k in tail.columns}
        for r in active:
            rule = rules.get(r["key"])
            if rule is None:
                continue
            m = rule.mask(fa)
            if m.any():
                i = int(np.flatnonzero(m)[-1])
                ex = EXITS[r["exit"]]
                atr = float(tail["atr"].iloc[i])
                px = float(g["close"].iloc[-lookback_bars + i])
                s = 1 if rule.side == "long" else -1
                hits.append({"inst": inst, "ts": int(tail.index[i]), "rule": r["label"], "verdict": r["verdict"], "side": rule.side,
                             "exit": r["exit"], "hold_min": ex["hold"] * 5, "price": px,
                             "stop_px": px * (1 - s * ex["stop"] * atr) if ex["stop"] and not np.isnan(atr) else None,
                             "holdout_mean": r["holdout"].get("day_mean"), "valid_mean": r["valid"].get("day_mean")})
    hits.sort(key=lambda x: -x["ts"])
    return {"ok": True, "rules": len(active), "hits": hits, "scanned_at": db.now_ms()}


def market_breadth_recent(cfg: Config, insts: list[str], since: int) -> pd.Series:
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        df = con.execute("SELECT inst_id, ts, close FROM candles WHERE ts >= ? AND ts % 3600000 = 0", [since - 2 * DAY]).df()
    p = df.pivot(index="ts", columns="inst_id", values="close").sort_index()
    r = p / p.shift(24) - 1
    b = (r > 0).sum(axis=1) / r.notna().sum(axis=1)
    b.index = b.index.astype("int64") + 3_600_000
    return b


# ---------------------------------------------------------------- io
def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "discover.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def write(cfg: Config, res: dict[str, Any]) -> None:
    out = cfg.paths.results
    out.mkdir(parents=True, exist_ok=True)
    (out / "discover.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    rows = []
    for r in res.get("results", []):
        rows.append({"verdict": r["verdict"], "family": r["family"], "rule": r["label"], "exit": r["exit"],
                     **{f"{p}_{k}": r[p].get(k) for p in ("train", "valid", "holdout") for k in ("n", "n_days", "day_mean", "win_rate")},
                     "valid_ci_lo": (r["valid"].get("ci95") or [None])[0], "holdout_ci_lo": (r["holdout"].get("ci95") or [None])[0],
                     "base_valid": r["base_valid"], "base_hold": r["base_hold"], "train_t": r["train_score"], "beats_null": r["beats_null"]})
    pd.DataFrame(rows).to_csv(out / "discover.csv", index=False, encoding="utf-8", float_format="%.6g")


def print_summary(res: dict[str, Any]) -> None:
    pc = lambda v: "     -" if v is None else f"{v * 100:+6.2f}%"
    print(f"規則 {res['n_rules']} 條 × 出場 {res['n_exits']} 種，實際有事件的組合 {res['n_tested']}；{res['n_insts']} 個合約，{res['range'][0]} ~ {res['range'][1]}")
    print(f"切分：訓練 < {res['split']['valid_start']} ≤ 驗證 < {res['split']['holdout_start']} ≤ 保留；洗牌基準 t 值 95 分位 {res['null']['p95']:.2f}")
    cnt = pd.Series([r["verdict"] for r in res["results"]]).value_counts().to_dict()
    print(f"入選 {len(res['results'])} 條：{cnt}\n")
    for r in res["results"][:40]:
        print(f"[{r['verdict']}] {r['label']}  出場 {r['exit']}")
        print(f"   訓練 n={r['train']['n']:>5} 日均 {pc(r['train'].get('day_mean'))} t={r['train_score']:.1f} | "
              f"驗證 n={r['valid'].get('n', 0):>4} {pc(r['valid'].get('day_mean'))} (隨機 {pc(r['base_valid'])}) | "
              f"保留 n={r['holdout'].get('n', 0):>4} {pc(r['holdout'].get('day_mean'))} (隨機 {pc(r['base_hold'])})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.discover")
    ap.add_argument("--config", default=None)
    ap.add_argument("--families", nargs="+", default=None, choices=list(BASES))
    ap.add_argument("--no-grid", action="store_true")
    ap.add_argument("--scan", action="store_true", help="only run the live scanner with existing results")
    ap.add_argument("--reuse", action="store_true", help="reuse the cached market scan (skip the slow part)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "discover")
    if args.scan:
        r = live_scan(cfg)
        print(json.dumps(r, ensure_ascii=False, indent=1, default=_json_default)[:4000])
        return 0

    def prog(n: int, tot: int) -> None:
        if n % 25 == 0 or n == tot:
            log.info("scan %d/%d instruments", n, tot)

    res = evaluate(cfg, args.families, progress=prog, reuse=args.reuse)
    if not args.no_grid:
        log.info("grid direction simulation")
        res["grid"] = grid_direction(cfg)
    write(cfg, res)
    print_summary(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
