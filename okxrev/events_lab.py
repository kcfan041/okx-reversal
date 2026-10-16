"""Event lab: dissect one pump-then-crash event, find similar cases across the market, turn the shape into a
template (rule), and test it with time splits and a one-shot blind test.

Shape at bar i (5m grid, only bars <= i are used, so a template never looks ahead):
    peak      = highest high within the last PEAK_BARS bars (incl. i); pk_i its index
    base low  = lowest low within the BASE_BARS bars ending at the peak; lo_i its index
    rise      = peak / base_low - 1            rise_bars  = pk_i - lo_i
    drop      = 1 - close_i / peak             drop_bars  = i - pk_i
    retrace   = (peak - close_i) / (peak - base_low)      share of the rise given back
A template fires on the first bar where all its conditions hold and they did not hold during the previous
`cooldown` bars (same freshness rule as the discovered rules). Entry = next bar's open; exits are the paper-trading
exits (fixed hold, or 2xATR stop + time stop).

The market index (results/lab_index.parquet) holds every bar of every instrument (main + old database) that passes
loose minimum thresholds (rise >= INDEX_RISE_MIN and drop >= INDEX_DROP_MIN), with its shape, context and outcomes.
Templates are evaluated on it in memory.

Periods: main database split into discovery (first 2/3) and validation (last 1/3); the old database
(2024-10 ~ 2025-09) is the blind period, evaluated only on explicit request and every request is logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str, parse_utc_ms

log = logging.getLogger("okxrev.lab")

BAR = db.BAR_MS
HOUR = 3_600_000
DAY = 86_400_000
PEAK_BARS = 144
BASE_BARS = 288
MIN_HISTORY = 36
INDEX_RISE_MIN = 0.15
INDEX_DROP_MIN = 0.05
EXITS = {"15m": (3, None), "1h": (12, None), "6h": (72, None), "24h": (288, None), "6h_sl2atr": (72, 2.0), "24h_sl2atr": (288, 2.0)}
EXIT_LABEL = {"15m": "持有 15 分", "1h": "持有 1 小時", "6h": "持有 6 小時", "24h": "持有 24 小時",
              "6h_sl2atr": "6 小時＋止損 2×ATR", "24h_sl2atr": "24 小時＋止損 2×ATR"}
SHAPE_COLS = ["rise", "rise_bars", "drop", "drop_bars", "retrace", "pk_ts", "lo_ts", "peak", "base_low"]
CTX_COLS = ["ret_bar", "vol_ratio", "vmax_1h", "r24h", "turnover24", "atr", "age_d", "btc"]
SLIP_EXTRA_SMALL = 0.001

TEMPLATE_DEFAULT: dict[str, Any] = {
    "rise_min": 0.3, "rise_max": None, "rise_bars_max": 288, "drop_min": 0.1, "drop_max": None, "drop_bars_max": 36,
    "retrace_min": None, "vol_min": None, "ret_bar_max": None, "turnover_min": None, "turnover_max": None,
    "age_max_d": None, "btc": [], "cooldown": 72,
}
NUM_KEYS = [k for k, v in TEMPLATE_DEFAULT.items() if k not in ("btc",)]


# ---------------------------------------------------------------- paths / config
def lcfg(cfg: Config) -> dict[str, Any]:
    return {"split_frac": 2 / 3, "similar_k": 200, "confirm_n": 200, "recent_hours": 24, **(cfg.raw.get("lab") or {})}


def lab_dir(cfg: Config) -> Path:
    p = cfg.paths.results / "lab"
    p.mkdir(parents=True, exist_ok=True)
    return p


def index_path(cfg: Config) -> Path:
    return lab_dir(cfg) / "index.parquet"


def random_path(cfg: Config) -> Path:
    return lab_dir(cfg) / "random.parquet"


# ---------------------------------------------------------------- shape
def rolling_max(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).rolling(n, min_periods=1).max().to_numpy()


def rolling_min(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).rolling(n, min_periods=1).min().to_numpy()


def prefilter(h: np.ndarray, lo: np.ndarray, c: np.ndarray, rise_min: float, drop_min: float) -> np.ndarray:
    """Necessary condition for shape(rise >= rise_min, drop >= drop_min): the exact base low lies inside the last
    PEAK_BARS + BASE_BARS bars, so rise <= rolling_peak / rolling_low(P+B) - 1."""
    pk = rolling_max(h, PEAK_BARS)
    lw = rolling_min(lo, PEAK_BARS + BASE_BARS)
    with np.errstate(divide="ignore", invalid="ignore"):
        ok = (1 - c / pk >= drop_min) & (pk / lw - 1 >= rise_min)
    ok &= np.arange(len(c)) >= MIN_HISTORY
    return np.flatnonzero(np.nan_to_num(ok, nan=0).astype(bool))


def shape_at(ts: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, idx: np.ndarray) -> dict[str, np.ndarray]:
    n = len(idx)
    out = {k: np.full(n, np.nan) for k in SHAPE_COLS}
    for j, i in enumerate(np.asarray(idx, dtype=np.int64)):
        if not np.isfinite(c[i]):
            continue
        w0 = max(0, i - PEAK_BARS + 1)
        seg = h[w0:i + 1]
        if not np.isfinite(seg).any():
            continue
        pk_i = w0 + int(np.nanargmax(seg))
        b0 = max(0, pk_i - BASE_BARS + 1)
        sl = lo[b0:pk_i + 1]
        if not np.isfinite(sl).any():
            continue
        lo_i = b0 + int(np.nanargmin(sl))
        pk, bl = h[pk_i], lo[lo_i]
        if not (pk > 0 and bl > 0):
            continue
        out["rise"][j] = pk / bl - 1
        out["rise_bars"][j] = pk_i - lo_i
        out["drop"][j] = 1 - c[i] / pk
        out["drop_bars"][j] = i - pk_i
        out["retrace"][j] = (pk - c[i]) / (pk - bl) if pk > bl else np.nan
        out["pk_ts"][j] = ts[pk_i]
        out["lo_ts"][j] = ts[lo_i]
        out["peak"][j] = pk
        out["base_low"][j] = bl
    return out


def context_cols(g: pd.DataFrame) -> dict[str, np.ndarray]:
    """Same definitions as detector.prepare / discover.features (vol_ratio, vmax_1h, r24h, turnover24, atr)."""
    c, h, lo, vq = g["close"], g["high"], g["low"], g["vol_quote"]
    vol_ma = vq.shift(1).rolling(20, min_periods=20).mean()
    vr = vq / vol_ma
    pc = c.shift(1)
    tr = pd.concat([h - lo, (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return {"ret_bar": (c / pc - 1).to_numpy(), "vol_ratio": vr.to_numpy(), "vmax_1h": vr.rolling(12, min_periods=1).max().to_numpy(),
            "r24h": (c / c.shift(288) - 1).to_numpy(), "turnover24": vq.rolling(288, min_periods=144).sum().to_numpy(),
            "atr": (tr.rolling(14, min_periods=14).mean() / c).to_numpy()}


def outcomes_at(o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, atr: np.ndarray, idx: np.ndarray,
                cost: float) -> dict[str, np.ndarray]:
    """Net returns per exit for entries at the next open after bar idx (same as discover.outcomes, evaluated only
    at idx), plus 24h max favourable / adverse excursion in long terms."""
    n = len(c)
    idx = np.asarray(idx, dtype=np.int64)
    entry = np.where(idx + 1 < n, o[np.minimum(idx + 1, n - 1)], np.nan)
    out: dict[str, np.ndarray] = {}
    for name, (k, stop) in EXITS.items():
        ex = np.where(idx + k < n, c[np.minimum(idx + k, n - 1)], np.nan)
        gl = ex / entry - 1
        gs = -gl
        if stop:
            hh, ll = _win_extreme(h, lo, idx, k)
            st = stop * atr[idx]
            gl = np.where(1 - ll / entry >= st, -st, gl)
            gs = np.where(hh / entry - 1 >= st, -st, gs)
        out[f"L_{name}"] = gl - cost
        out[f"S_{name}"] = gs - cost
    hh, ll = _win_extreme(h, lo, idx, 288)
    out["up24"] = hh / entry - 1
    out["dn24"] = 1 - ll / entry
    return out


def _win_extreme(h: np.ndarray, lo: np.ndarray, idx: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """max high / min low over bars idx+1 .. idx+k (NaN if the window runs past the data)."""
    n = len(h)
    hh = np.full(len(idx), np.nan)
    ll = np.full(len(idx), np.nan)
    for j, i in enumerate(idx):
        if i + k >= n:
            continue
        a, b = h[i + 1:i + k + 1], lo[i + 1:i + k + 1]
        if np.isfinite(a).any():
            hh[j] = np.nanmax(a)
            ll[j] = np.nanmin(b)
    return hh, ll


# ---------------------------------------------------------------- template
def norm_template(t: dict[str, Any] | None) -> dict[str, Any]:
    t = {**TEMPLATE_DEFAULT, **{k: v for k, v in (t or {}).items() if k in TEMPLATE_DEFAULT}}
    for k in NUM_KEYS:
        v = t[k]
        if v in ("", None):
            t[k] = None
            continue
        try:
            t[k] = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} 必須是數字")
        if not np.isfinite(t[k]):
            raise ValueError(f"{k} 不是有效數字")
    if t["rise_min"] is None or t["rise_min"] < INDEX_RISE_MIN:
        t["rise_min"] = INDEX_RISE_MIN
    if t["drop_min"] is None or t["drop_min"] < INDEX_DROP_MIN:
        t["drop_min"] = INDEX_DROP_MIN
    t["rise_bars_max"] = min(float(t["rise_bars_max"] or BASE_BARS), float(BASE_BARS))
    t["drop_bars_max"] = min(float(t["drop_bars_max"] or PEAK_BARS - 1), float(PEAK_BARS - 1))
    t["cooldown"] = int(min(max(t["cooldown"] or 72, 1), 2016))
    btc = t.get("btc") or []
    if isinstance(btc, str):
        btc = [btc]
    t["btc"] = sorted({b for b in btc if b in ("weak", "neutral", "strong")})
    return t


def template_hash(t: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(norm_template(t), sort_keys=True).encode()).hexdigest()[:10]


def describe(t: dict[str, Any]) -> str:
    t = norm_template(t)
    p = lambda v: f"{v * 100:.0f}%"
    hrs = lambda b: f"{b * 5 / 60:.1f} 小時" if b >= 12 else f"{b * 5:.0f} 分"
    parts = [f"{hrs(t['rise_bars_max'])}內從低點漲 ≥{p(t['rise_min'])}" + (f"（≤{p(t['rise_max'])}）" if t["rise_max"] else ""),
             f"高點後 {hrs(t['drop_bars_max'])}內跌 ≥{p(t['drop_min'])}" + (f"（≤{p(t['drop_max'])}）" if t["drop_max"] else "")]
    if t["retrace_min"]:
        parts.append(f"回吐漲幅 ≥{p(t['retrace_min'])}")
    if t["vol_min"]:
        parts.append(f"近 1h 量比 ≥{t['vol_min']:.1f}x")
    if t["ret_bar_max"] is not None:
        parts.append(f"觸發 K 線 ≤{p(t['ret_bar_max'])}")
    if t["turnover_min"]:
        parts.append(f"24h 成交額 ≥{t['turnover_min'] / 1e6:.1f}M")
    if t["turnover_max"]:
        parts.append(f"24h 成交額 ≤{t['turnover_max'] / 1e6:.1f}M")
    if t["age_max_d"]:
        parts.append(f"上市 ≤{t['age_max_d']:.0f} 天")
    if t["btc"]:
        parts.append("BTC " + "/".join({"weak": "弱勢", "neutral": "中性", "strong": "強勢"}[b] for b in t["btc"]))
    return "，".join(parts)


def mask(t: dict[str, Any], cols: dict[str, np.ndarray] | pd.DataFrame) -> np.ndarray:
    t = norm_template(t)
    g = lambda k: np.asarray(cols[k], dtype=float)
    m = (g("rise") >= t["rise_min"]) & (g("drop") >= t["drop_min"]) & (g("rise_bars") <= t["rise_bars_max"]) & (g("drop_bars") <= t["drop_bars_max"])
    if t["rise_max"] is not None:
        m &= g("rise") <= t["rise_max"]
    if t["drop_max"] is not None:
        m &= g("drop") <= t["drop_max"]
    if t["retrace_min"] is not None:
        m &= g("retrace") >= t["retrace_min"]
    if t["vol_min"] is not None:
        m &= g("vmax_1h") >= t["vol_min"]
    if t["ret_bar_max"] is not None:
        m &= g("ret_bar") <= t["ret_bar_max"]
    if t["turnover_min"] is not None:
        m &= g("turnover24") >= t["turnover_min"]
    if t["turnover_max"] is not None:
        m &= g("turnover24") <= t["turnover_max"]
    if t["age_max_d"] is not None:
        m &= g("age_d") <= t["age_max_d"]
    if t["btc"]:
        m &= np.isin(np.asarray(cols["btc"]).astype(str), t["btc"])
    return np.nan_to_num(m, nan=0).astype(bool)


def fresh_hits(inst: np.ndarray, ts: np.ndarray, hit: np.ndarray, cooldown: int) -> np.ndarray:
    """Rows (of a frame sorted by inst, ts) where the condition holds and held on no bar of the same instrument
    within the previous `cooldown` bars. Bars missing from the index cannot satisfy any template (they fail the
    index minimums), so checking only indexed hits is exact."""
    idx = np.flatnonzero(hit)
    if len(idx) == 0:
        return idx
    i_, t_ = inst[idx], ts[idx]
    same = np.r_[False, i_[1:] == i_[:-1]]
    gap = np.r_[np.inf, (t_[1:] - t_[:-1]) / BAR]
    keep = ~same | (gap > cooldown)
    return idx[keep]


# ---------------------------------------------------------------- index build
def _btc_regime(cfg: Config, path: Path) -> pd.Series:
    from okxrev.detector import btc_features
    with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=30) as con:
        b = db.load_candles(con, cfg.universe["btc_inst"])
    return btc_features(b, cfg.btc_regime)["btc_regime"]


def build_index(cfg: Config, progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    from okxrev import replay
    from okxrev.detector import to_grid
    t0 = time.monotonic()
    fee, slip = float(cfg.metrics["fee_per_side"]), float(cfg.metrics["slippage_per_side"])
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        meta = db.sync_overview(con).set_index("inst_id")
    sources = [("main", cfg.paths.db)]
    oldp = replay.old_db_path(cfg)
    if oldp.exists():
        sources.append(("old", oldp))
    frames, rnd = [], []
    rng = np.random.default_rng(11)
    counts = {}
    for src, path in sources:
        btc = _btc_regime(cfg, path)
        with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=60) as con:
            insts = db.list_candle_insts(con)
        n_src = 0
        for k in range(0, len(insts), 40):
            chunk = insts[k:k + 40]
            with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=60) as con:
                df = con.execute("SELECT inst_id, ts, open, high, low, close, vol, vol_ccy, vol_quote FROM candles "
                                 "WHERE inst_id IN (SELECT unnest(?)) ORDER BY inst_id, ts", [chunk]).df()
            for inst, raw in df.groupby("inst_id", sort=False):
                if len(raw) < 600:
                    continue
                g = to_grid(raw.drop(columns="inst_id"))
                lt = meta.at[inst, "list_time"] if inst in meta.index else None
                lt = None if lt is None or pd.isna(lt) else int(lt)
                part, r = _index_one(inst, src, g, btc, lt, fee, slip, rng)
                if part is not None:
                    frames.append(part)
                    n_src += len(part)
                if r is not None:
                    rnd.append(r)
            if progress:
                progress(src, min(k + 40, len(insts)), len(insts))
        counts[src] = n_src
    idx = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(idx):
        idx = idx.sort_values(["inst", "ts"]).reset_index(drop=True)
        for c in idx.columns:
            if idx[c].dtype == np.float64 and c not in ("ts", "pk_ts", "lo_ts", "peak", "base_low", "close", "turnover24"):
                idx[c] = idx[c].astype(np.float32)
        idx["inst"] = idx["inst"].astype("category")
        idx["src"] = idx["src"].astype("category")
        idx["btc"] = idx["btc"].astype("category")
    idx.to_parquet(index_path(cfg), index=False)
    rd = pd.concat(rnd, ignore_index=True) if rnd else pd.DataFrame()
    rd.to_parquet(random_path(cfg), index=False)
    _cache.clear()
    meta_out = {"built": db.now_ms(), "rows": int(len(idx)), "by_src": counts, "random_rows": int(len(rd)),
                "sec": round(time.monotonic() - t0, 1), "range": {s: [int(x["ts"].min()), int(x["ts"].max())] for s, x in idx.groupby("src", observed=True)} if len(idx) else {}}
    (lab_dir(cfg) / "index_meta.json").write_text(json.dumps(meta_out), encoding="utf-8")
    return meta_out


def _index_one(inst: str, src: str, g: pd.DataFrame, btc: pd.Series, list_time: int | None, fee: float, slip: float,
               rng: np.random.Generator) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    ts = g.index.to_numpy(np.int64)
    o, h, lo, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    ctx = context_cols(g)
    small = float(np.nanmedian(ctx["turnover24"])) < 5e6 if np.isfinite(ctx["turnover24"]).any() else True
    cost = 2 * (fee + slip + (SLIP_EXTRA_SMALL if small else 0.0))
    btc_a = btc.reindex(g.index).fillna("unknown").to_numpy().astype(str)
    age = (ts - list_time) / DAY if list_time else np.full(len(ts), np.inf)
    valid = np.flatnonzero(np.isfinite(c) & (np.arange(len(c)) >= 288))
    r = None
    if len(valid):
        pick = np.sort(rng.choice(valid, size=min(80, len(valid)), replace=False))
        oc = outcomes_at(o, h, lo, c, ctx["atr"], pick, cost)
        r = pd.DataFrame({"ts": ts[pick], "inst": inst, "src": src, "btc": btc_a[pick], "turnover24": ctx["turnover24"][pick], **oc})
    cand = prefilter(h, lo, c, INDEX_RISE_MIN, INDEX_DROP_MIN)
    if len(cand) == 0:
        return None, r
    sh = shape_at(ts, h, lo, c, cand)
    keep = (sh["rise"] >= INDEX_RISE_MIN) & (sh["drop"] >= INDEX_DROP_MIN)
    cand = cand[keep]
    if len(cand) == 0:
        return None, r
    sh = {k: v[keep] for k, v in sh.items()}
    oc = outcomes_at(o, h, lo, c, ctx["atr"], cand, cost)
    d = {"inst": inst, "src": src, "ts": ts[cand], "close": c[cand], **sh, **{k: ctx[k][cand] for k in ctx},
         "age_d": age[cand], "btc": btc_a[cand], "cost": cost, **oc}
    return pd.DataFrame(d), r


_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def load_index(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    p = index_path(cfg)
    if not p.exists():
        raise FileNotFoundError("尚未建立事件索引")
    mt = p.stat().st_mtime
    with _cache_lock:
        if _cache.get("mt") != mt:
            idx = pd.read_parquet(p)
            rp = random_path(cfg)
            rnd = pd.read_parquet(rp) if rp.exists() else pd.DataFrame()
            mp = lab_dir(cfg) / "index_meta.json"
            meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
            _cache.update(mt=mt, idx=idx, rnd=rnd, meta=meta, arrays={
                "inst": idx["inst"].cat.codes.to_numpy() if len(idx) else np.array([]), "ts": idx["ts"].to_numpy(np.int64) if len(idx) else np.array([])})
        return _cache["idx"], _cache["rnd"], _cache["meta"]


def index_status(cfg: Config) -> dict[str, Any]:
    mp = lab_dir(cfg) / "index_meta.json"
    if not mp.exists() or not index_path(cfg).exists():
        return {"exists": False}
    m = json.loads(mp.read_text(encoding="utf-8"))
    return {"exists": True, **m, "age_h": round((db.now_ms() - m["built"]) / HOUR, 1)}


# ---------------------------------------------------------------- periods / stats
def periods(cfg: Config, idx: pd.DataFrame) -> dict[str, Any]:
    main = idx[idx["src"] == "main"]
    if main.empty:
        return {"split": None}
    lo, hi = int(main["ts"].min()), int(main["ts"].max())
    split = lo + int((hi - lo) * float(lcfg(cfg)["split_frac"]))
    split = (split // DAY) * DAY
    return {"split": split, "main": [lo, hi], "old": [int(idx.loc[idx["src"] == "old", "ts"].min()), int(idx.loc[idx["src"] == "old", "ts"].max())]
            if (idx["src"] == "old").any() else None}


def period_of(src: np.ndarray, ts: np.ndarray, split: int) -> np.ndarray:
    return np.where(np.asarray(src).astype(str) == "old", "blind", np.where(ts < split, "disc", "valid"))


def _stats(ret: np.ndarray, ts: np.ndarray, boot: int = 400) -> dict[str, Any]:
    from okxrev.discover import day_stats
    ok = np.isfinite(ret)
    return day_stats(ret[ok].astype(float), ts[ok].astype(np.int64), boot=boot)


def _base(rnd: pd.DataFrame, col: str, per: str, split: int, extra: np.ndarray | None = None) -> float | None:
    if rnd.empty or col not in rnd:
        return None
    p = period_of(rnd["src"].to_numpy(), rnd["ts"].to_numpy(np.int64), split)
    m = p == per
    if extra is not None:
        m &= extra
    x = rnd.loc[m, col].dropna()
    return float(x.mean()) if len(x) else None


def verdict_of(valid: dict[str, Any], blind: dict[str, Any] | None, bv: float | None, bb: float | None) -> str:
    from okxrev.discover import trade_ok, verdict
    if blind is None:
        good = (valid.get("n_days", 0) >= 8 and valid.get("day_mean", -1) > max(bv or 0, 0) and (valid.get("ci95") or [-1])[0] > 0
                and trade_ok(valid, bv))
        pos = valid.get("n", 0) > 0 and valid.get("day_mean", -1) > max(bv or 0, 0) and trade_ok(valid, bv)
        return "驗證通過（待盲測）" if good else "驗證期為正（待盲測）" if pos else "驗證期無效"
    return verdict(valid, blind, bv or 0.0, bb or 0.0, True)


def blind_log_path(cfg: Config) -> Path:
    return lab_dir(cfg) / "blind_log.json"


def blind_log(cfg: Config) -> list[dict[str, Any]]:
    p = blind_log_path(cfg)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def test_template(cfg: Config, t: dict[str, Any], blind: bool = False, examples: int = 40) -> dict[str, Any]:
    t = norm_template(t)
    idx, rnd, meta = load_index(cfg)
    if idx.empty:
        return {"n": 0, "note": "索引是空的"}
    pr = periods(cfg, idx)
    split = pr["split"]
    arr = _cache["arrays"]
    hit = mask(t, idx)
    rows = fresh_hits(arr["inst"], arr["ts"], hit, int(t["cooldown"]))
    ev = idx.iloc[rows]
    ts = ev["ts"].to_numpy(np.int64)
    per = period_of(ev["src"].to_numpy(), ts, split)
    th = template_hash(t)
    prev_blind = [b for b in blind_log(cfg) if b["hash"] == th]
    show_blind = blind or bool(prev_blind)
    if blind and not prev_blind:
        log_ = blind_log(cfg)
        log_.append({"hash": th, "ts": db.now_ms(), "template": t, "desc": describe(t)})
        blind_log_path(cfg).write_text(json.dumps(log_, ensure_ascii=False), encoding="utf-8")
    combos = []
    for side in ("short", "long"):
        for ex in EXITS:
            col = f"{'S' if side == 'short' else 'L'}_{ex}"
            r = ev[col].to_numpy(float)
            d = {"side": side, "exit": ex, "col": col}
            for p in ("disc", "valid", "blind"):
                if p == "blind" and not show_blind:
                    continue
                m = per == p
                d[p] = _stats(r[m], ts[m], boot=300 if p != "disc" else 0)
                d[f"base_{p}"] = _base(rnd, col, p, split)
            combos.append(d)
    best = max(combos, key=lambda d: (d["disc"].get("t", -9) if d["disc"].get("n_days", 0) >= 5 else -9, d["disc"].get("day_mean", -9)))
    for d in combos:
        d["verdict"] = verdict_of(d["valid"], d.get("blind"), d.get("base_valid"), d.get("base_blind"))
    groups = _groups(ev, per, best["col"], rnd, split)
    months = pd.Series(ev[best["col"]].to_numpy(float)).groupby(pd.to_datetime(ts, unit="ms").strftime("%Y-%m")).agg(["mean", "count"])
    ex_rows = ev.assign(period=per).sort_values("ts", ascending=False)
    if not show_blind:
        ex_rows = ex_rows[ex_rows["period"] != "blind"]
    ex_rows = ex_rows.head(examples)
    return {
        "template": t, "hash": th, "desc": describe(t), "n": int(len(ev)), "n_by_period": {p: int((per == p).sum()) for p in ("disc", "valid", "blind")},
        "n_insts": int(ev["inst"].nunique()), "periods": {"split": split, "split_str": ms_to_str(split, "%Y-%m-%d"),
                                                          "main": [ms_to_str(x, "%Y-%m-%d") for x in pr["main"]],
                                                          "old": [ms_to_str(x, "%Y-%m-%d") for x in pr["old"]] if pr["old"] else None},
        "blind_shown": show_blind, "blind_done_before": bool(prev_blind), "blind_attempts_total": len(blind_log(cfg)),
        "combos": combos, "best": {"side": best["side"], "exit": best["exit"], "verdict": best["verdict"]}, "groups": groups,
        "by_month": [{"m": m, "mean": float(x["mean"]), "n": int(x["count"])} for m, x in months.iterrows()
                     if show_blind or m >= ms_to_str(pr["main"][0], "%Y-%m")],
        "examples": _ex_records(ex_rows, best["col"]), "index": {"rows": meta.get("rows"), "built": meta.get("built")},
    }


def _groups(ev: pd.DataFrame, per: np.ndarray, col: str, rnd: pd.DataFrame, split: int) -> list[dict[str, Any]]:
    out = []
    ts = ev["ts"].to_numpy(np.int64)
    r = ev[col].to_numpy(float)
    to = ev["turnover24"].to_numpy(float)
    size = np.where(to >= 5e7, "大（>5千萬U）", np.where(to < 5e6, "小（<5百萬U）", "中"))
    rsize = np.where(rnd["turnover24"].to_numpy(float) >= 5e7, "大（>5千萬U）", np.where(rnd["turnover24"].to_numpy(float) < 5e6, "小（<5百萬U）", "中")) if len(rnd) else None
    for name, key, rkey in (("成交額", size, rsize), ("BTC 環境", ev["btc"].astype(str).to_numpy(), rnd["btc"].astype(str).to_numpy() if len(rnd) else None)):
        for v in pd.unique(key):
            m = (key == v) & (per != "blind")
            if m.sum() < 3:
                continue
            s = _stats(r[m], ts[m], boot=0)
            base = None
            if rkey is not None and col in rnd:
                rm = (rkey == v) & (period_of(rnd["src"].to_numpy(), rnd["ts"].to_numpy(np.int64), split) != "blind")
                x = rnd.loc[rm, col].dropna()
                base = float(x.mean()) if len(x) else None
            out.append({"group": name, "value": str(v), "n": s["n"], "n_days": s.get("n_days"), "day_mean": s.get("day_mean"), "win_rate": s.get("win_rate"), "base": base})
    return out


def _ex_records(ev: pd.DataFrame, col: str) -> list[dict[str, Any]]:
    keep = ["inst", "ts", "period", "rise", "rise_bars", "drop", "drop_bars", "retrace", "vmax_1h", "turnover24", "btc", "pk_ts", "lo_ts",
            "L_1h", "L_6h", "L_24h", "S_1h", "S_6h", "S_24h", "up24", "dn24"]
    d = ev[[k for k in keep if k in ev]].copy()
    d["ret"] = ev[col].to_numpy(float)
    d["inst"] = d["inst"].astype(str)
    d["btc"] = d["btc"].astype(str)
    return d.to_dict("records")


# ---------------------------------------------------------------- one event
def _load_5m(cfg: Config, inst: str, since: int, until: int) -> pd.DataFrame:
    from okxrev import replay
    frames = []
    for p in (cfg.paths.db, replay.old_db_path(cfg)):
        if not Path(p).exists():
            continue
        try:
            with db.open_db(p, read_only=True, wait_sec=1.0, max_tries=20) as con:
                frames.append(db.load_candles(con, inst, since, until))
        except db.DbBusy:
            continue
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def analyze_event(cfg: Config, inst: str, at: int, with_fine: bool = True) -> dict[str, Any]:
    """Locate the pump-then-crash around `at` (peak within +-12h), measure it on 5m (and 1m / 1s if available),
    and propose a template whose trigger fires during the crash using only past data."""
    from okxrev import fine
    from okxrev.detector import btc_features, to_grid
    raw = _load_5m(cfg, inst, at - 3 * DAY, at + 2 * DAY)
    if len(raw) < 100:
        raise ValueError(f"{inst} 在 {ms_to_str(at)} 附近沒有足夠的 5 分 K 資料")
    g = to_grid(raw)
    ts = g.index.to_numpy(np.int64)
    o, h, lo, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    win = (ts >= at - 12 * HOUR) & (ts <= at + 12 * HOUR)
    if not win.any():
        raise ValueError("指定時間附近沒有資料")
    wi = np.flatnonzero(win)
    pk_i = int(wi[np.nanargmax(h[wi])])
    b0 = max(0, pk_i - BASE_BARS + 1)
    lo_i = b0 + int(np.nanargmin(lo[b0:pk_i + 1]))
    a_end = min(len(c), pk_i + PEAK_BARS)
    cr_i = pk_i + int(np.nanargmin(lo[pk_i:a_end]))
    pk, bl, crash_low = h[pk_i], lo[lo_i], lo[cr_i]
    rise = pk / bl - 1
    drop_total = 1 - crash_low / pk
    ctx = context_cols(g)
    btc = btc_features(_load_5m(cfg, cfg.universe["btc_inst"], at - 4 * DAY, at + 2 * DAY), cfg.btc_regime).reindex(g.index)
    trig_drop = float(max(INDEX_DROP_MIN, round(0.4 * drop_total, 2))) if drop_total >= INDEX_DROP_MIN else INDEX_DROP_MIN
    trig_i = None
    for i in range(pk_i, a_end):
        if np.isfinite(c[i]) and 1 - c[i] / pk >= trig_drop:
            trig_i = i
            break
    out: dict[str, Any] = {
        "inst": inst, "at": at, "bars": {"base": int(ts[lo_i]), "peak": int(ts[pk_i]), "crash_low": int(ts[cr_i]), "trigger": int(ts[trig_i]) if trig_i is not None else None},
        "prices": {"base": float(bl), "peak": float(pk), "crash_low": float(crash_low), "trigger": float(c[trig_i]) if trig_i is not None else None},
        "shape": {"rise": float(rise), "rise_bars": int(pk_i - lo_i), "rise_hours": (pk_i - lo_i) * 5 / 60, "drop_total": float(drop_total),
                  "drop_bars_total": int(cr_i - pk_i), "drop_hours": (cr_i - pk_i) * 5 / 60, "retrace_total": float((pk - crash_low) / (pk - bl)) if pk > bl else None,
                  "pre_r24h": float(c[lo_i] / c[lo_i - 288] - 1) if lo_i >= 288 and np.isfinite(c[lo_i - 288]) else None,
                  "vmax_rise": float(np.nanmax(ctx["vol_ratio"][lo_i:pk_i + 1])) if pk_i > lo_i else None,
                  "vmax_drop": float(np.nanmax(ctx["vol_ratio"][pk_i:cr_i + 1])) if cr_i >= pk_i else None,
                  "turnover24_at_peak": float(ctx["turnover24"][pk_i]) if np.isfinite(ctx["turnover24"][pk_i]) else None,
                  "btc_r4h_at_peak": float(btc["btc_ret_4h"].iloc[pk_i]) if np.isfinite(btc["btc_ret_4h"].iloc[pk_i]) else None,
                  "btc_regime_at_peak": str(btc["btc_regime"].iloc[pk_i])},
    }
    after = {}
    for k, lab in ((12, "1h"), (72, "6h"), (288, "24h")):
        if trig_i is not None and trig_i + k < len(c) and trig_i + 1 < len(o):
            after[lab] = float(c[trig_i + k] / o[trig_i + 1] - 1)
    out["after_trigger"] = after
    if trig_i is not None:
        sh = shape_at(ts, h, lo, c, np.array([trig_i]))
        out["trigger_shape"] = {k: (float(v[0]) if np.isfinite(v[0]) else None) for k, v in sh.items()}
        out["trigger_ctx"] = {k: (float(ctx[k][trig_i]) if np.isfinite(ctx[k][trig_i]) else None) for k in ctx}
        out["trigger_ctx"]["btc"] = str(btc["btc_regime"].iloc[trig_i])
    out["template"] = suggest_template(out)
    out["template_desc"] = describe(out["template"])
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=1.0, max_tries=20) as con:
        lt = con.execute("SELECT list_time FROM instruments WHERE inst_id = ?", [inst]).fetchone()
        out["age_d_at_peak"] = (int(ts[pk_i]) - int(lt[0])) / DAY if lt and lt[0] else None
        out["derivs"] = _derivs_around(con, inst, int(ts[lo_i]) - 6 * HOUR, int(ts[cr_i]) + 6 * HOUR)
    if with_fine:
        out["fine"] = _fine_detail(cfg, inst, int(ts[lo_i]), int(ts[pk_i]), int(ts[cr_i]), fine)
    return out


def suggest_template(a: dict[str, Any]) -> dict[str, Any]:
    s = a["shape"]
    ts_ = a.get("trigger_shape") or {}
    tc = a.get("trigger_ctx") or {}
    rise = s["rise"]
    t = dict(TEMPLATE_DEFAULT)
    t["rise_min"] = max(INDEX_RISE_MIN, round(0.7 * rise, 2))
    t["rise_bars_max"] = int(min(BASE_BARS, max(12, round(1.5 * s["rise_bars"]))))
    t["drop_min"] = float(ts_.get("drop") and max(INDEX_DROP_MIN, round(0.4 * s["drop_total"], 2)) or INDEX_DROP_MIN)
    t["drop_bars_max"] = int(min(PEAK_BARS - 1, max(6, 2 * (ts_.get("drop_bars") or 6))))
    vm = tc.get("vmax_1h")
    t["vol_min"] = round(max(2.0, 0.5 * vm), 1) if vm and vm >= 4 else None
    return norm_template(t)


def _derivs_around(con: Any, inst: str, lo: int, hi: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    q = {
        "funding": "SELECT ts, rate FROM funding WHERE inst_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
        "oi": "SELECT ts, oi_usd FROM open_interest WHERE inst_id = ? AND period = '1H' AND ts BETWEEN ? AND ? ORDER BY ts",
        "taker": "SELECT ts, buy, sell FROM taker_volume WHERE inst_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
        "liquidations": "SELECT ts, pos_side, px, sz FROM liquidations WHERE inst_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
    }
    for k, sql in q.items():
        try:
            d = con.execute(sql, [inst, lo, hi]).df()
        except Exception:
            continue
        if len(d):
            out[k] = d.tail(200).to_dict("records")
    return out


def _fine_detail(cfg: Config, inst: str, base_ts: int, pk_ts: int, low_ts: int, fine: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    s = max(base_ts - 30 * 60_000, pk_ts - 2 * DAY)
    e = min(low_ts + 2 * HOUR, s + 3 * DAY - 60_000, db.now_ms())
    try:
        m1 = fine.candles(cfg, inst, "1m", s, e, include_live=False)
    except Exception as ex:
        return {"error": f"1m 取得失敗：{type(ex).__name__}"}
    if len(m1):
        out["m1"] = fine_shape(m1)
        out["m1_range"] = [s, e]
    s1, e1 = pk_ts - 15 * 60_000, min(pk_ts + 45 * 60_000, db.now_ms())
    try:
        x = fine.candles(cfg, inst, "1s", s1, e1, include_live=False)
        if len(x) > 60:
            out["s1"] = {"n": int(len(x)), "range": [s1, e1], "max_10s_drop": float((x["close"] / x["close"].shift(10) - 1).min()),
                         "max_10s_rise": float((x["close"] / x["close"].shift(10) - 1).max())}
    except Exception:
        pass
    return out


def fine_shape(m1: pd.DataFrame) -> dict[str, Any]:
    """1m features of an event window (used to compare an event with its 5m look-alikes)."""
    c, h, lo = m1["close"].to_numpy(float), m1["high"].to_numpy(float), m1["low"].to_numpy(float)
    vq = m1["vol_quote"].to_numpy(float) if m1["vol_quote"].notna().any() else m1["vol"].to_numpy(float)
    ts = m1["ts"].to_numpy(np.int64)
    pk = int(np.nanargmax(h))
    lo_after = pk + int(np.nanargmin(lo[pk:])) if pk < len(lo) else pk
    lo_before = int(np.nanargmin(lo[:pk + 1]))
    r5 = pd.Series(c).pct_change(5).to_numpy()
    r1 = pd.Series(c).pct_change(1).to_numpy()
    o_pk = float(m1["open"].iloc[pk])
    rng = h[pk] - lo[pk]
    return {"peak_ts": int(ts[pk]), "peak": float(h[pk]), "low_after_ts": int(ts[lo_after]), "low_before_ts": int(ts[lo_before]),
            "rise_min": int(pk - lo_before), "drop_min": int(lo_after - pk),
            "max_5m_drop": float(np.nanmin(r5[pk:])) if pk < len(r5) - 1 else None, "max_1m_drop": float(np.nanmin(r1[pk:])) if pk < len(r1) - 1 else None,
            "max_5m_rise": float(np.nanmax(r5[:pk + 1])) if pk > 5 else None,
            "peak_upper_wick": float((h[pk] - max(o_pk, c[pk])) / rng) if rng > 0 else 0.0,
            "vol_conc": float(np.nanmax(vq) / np.nanmean(vq)) if np.nanmean(vq) > 0 else None,
            "peak_vol_share": float(np.nansum(vq[max(0, pk - 5):pk + 6]) / np.nansum(vq)) if np.nansum(vq) > 0 else None}


# ---------------------------------------------------------------- similar events
SIM_FEATS = ["l_rise", "l_rise_bars", "drop_bars", "retrace", "l_vmax", "l_turn", "r24h"]
SIM_W = np.array([2.0, 1.0, 1.0, 1.0, 0.7, 0.7, 0.5])


def _sim_matrix(d: pd.DataFrame) -> np.ndarray:
    pre = d["r24h"].to_numpy(float) - d["rise"].to_numpy(float)
    return np.column_stack([np.log1p(d["rise"].to_numpy(float)), np.log1p(d["rise_bars"].to_numpy(float)), d["drop_bars"].to_numpy(float) / 12,
                            d["retrace"].to_numpy(float), np.log1p(np.nan_to_num(d["vmax_1h"].to_numpy(float), nan=1.0)),
                            np.log10(np.nan_to_num(d["turnover24"].to_numpy(float), nan=1e6) + 1), np.nan_to_num(pre, nan=0.0)])


def find_similar(cfg: Config, a: dict[str, Any], k: int | None = None, exclude_self: bool = True, include_blind: bool = False) -> dict[str, Any]:
    """Rank market episodes whose trigger bar (first bar where the drop from peak reaches the event's trigger
    drop, rise at least half the event's) looks like the event's trigger bar. The blind period is left out unless
    asked for, so looking at look-alikes does not spend the blind test."""
    k = int(k or lcfg(cfg)["similar_k"])
    idx, rnd, _ = load_index(cfg)
    ts_ = a.get("trigger_shape")
    if not ts_ or ts_.get("rise") is None:
        raise ValueError("這個事件沒有觸發點（跌幅不足），無法找類似事件")
    t = norm_template({"rise_min": max(INDEX_RISE_MIN, 0.5 * ts_["rise"]), "drop_min": a["template"]["drop_min"], "rise_bars_max": BASE_BARS,
                       "drop_bars_max": PEAK_BARS - 1, "cooldown": 72})
    arr = _cache["arrays"]
    rows = fresh_hits(arr["inst"], arr["ts"], mask(t, idx), 72)
    ev = idx.iloc[rows].copy()
    if not include_blind:
        ev = ev[ev["src"].astype(str) != "old"]
    if exclude_self:
        ev = ev[~((ev["inst"].astype(str) == a["inst"]) & ((ev["ts"] - a["bars"]["peak"]).abs() <= 2 * DAY))]
    if ev.empty:
        return {"n": 0, "rows": []}
    tc = a.get("trigger_ctx") or {}
    src = pd.DataFrame([{**{kk: ts_.get(kk) for kk in ("rise", "rise_bars", "drop_bars", "retrace")},
                         "vmax_1h": tc.get("vmax_1h"), "turnover24": tc.get("turnover24"), "r24h": tc.get("r24h")}]).astype(float)
    X, x0 = _sim_matrix(ev), _sim_matrix(src)[0]
    sd = np.nanstd(X, axis=0)
    sd[sd == 0] = 1
    Z = (np.nan_to_num(X - x0, nan=0.0) / sd) * SIM_W
    ev["dist"] = np.sqrt((Z ** 2).sum(axis=1))
    ev = ev.sort_values("dist").head(k)
    split = periods(cfg, idx)["split"]
    ev["period"] = period_of(ev["src"].to_numpy(), ev["ts"].to_numpy(np.int64), split)
    ts = ev["ts"].to_numpy(np.int64)
    summ = []
    for side in ("short", "long"):
        for ex in ("1h", "6h", "24h", "6h_sl2atr"):
            col = f"{'S' if side == 'short' else 'L'}_{ex}"
            s = _stats(ev[col].to_numpy(float), ts, boot=300)
            base = float(rnd[col].mean()) if len(rnd) and col in rnd else None
            summ.append({"side": side, "exit": ex, **{kk: s.get(kk) for kk in ("n", "n_days", "day_mean", "mean", "median", "win_rate", "ci95")}, "base": base})
    return {"n": int(len(ev)), "trigger_template": t, "summary": summ,
            "rows": [{**r, "dist": float(dv)} for r, dv in zip(_ex_records(ev, "S_6h"), ev["dist"])],
            "include_blind": include_blind,
            "note": "相似度：漲幅、上漲時間、跌落時間、回吐比例、量比、成交額、起漲前 24h 漲跌（標準化後加權距離）；"
                    + ("包含 2024-25 盲測期事件" if include_blind else "不含 2024-25 盲測期（保留給盲測）")}


def confirm_similar(cfg: Config, source: dict[str, Any], rows: list[dict[str, Any]], n: int | None = None,
                    progress: Callable[[int, int], None] | None = None) -> list[dict[str, Any]]:
    """Fetch 1m candles for the top `n` look-alikes (cached in fine.duckdb) and compare 1m features with the source."""
    from okxrev import fine
    from okxrev.okx_client import OkxClient
    n = int(n or lcfg(cfg)["confirm_n"])
    sf = ((source.get("fine") or {}).get("m1")) or {}
    keys = ["max_5m_drop", "max_1m_drop", "max_5m_rise", "peak_upper_wick", "vol_conc", "peak_vol_share", "drop_min"]
    out = []
    client = OkxClient(cfg.okx)
    try:
        for j, r in enumerate(rows[:n]):
            s = int(r["lo_ts"]) - 30 * 60_000 if r.get("lo_ts") else int(r["ts"]) - 6 * HOUR
            s = max(s, int(r["pk_ts"]) - 2 * DAY)
            e = int(r["ts"]) + HOUR
            rec = dict(r)
            try:
                m1 = fine.candles(cfg, r["inst"], "1m", s, e, client=client, include_live=False)
                if len(m1) >= 30:
                    fs = fine_shape(m1)
                    rec["m1"] = fs
                    if sf:
                        dd = []
                        for kk in keys:
                            a_, b_ = sf.get(kk), fs.get(kk)
                            if a_ is None or b_ is None:
                                continue
                            scale = {"drop_min": 30.0, "vol_conc": 5.0}.get(kk, 0.1)
                            dd.append(min(3.0, abs(float(a_) - float(b_)) / scale))
                        rec["m1_dist"] = float(np.mean(dd)) if dd else None
                else:
                    rec["m1_note"] = "1m 資料不足"
            except Exception as ex:
                rec["m1_note"] = f"{type(ex).__name__}"
            out.append(rec)
            if progress:
                progress(j + 1, min(n, len(rows)))
    finally:
        client.close()
    ds = [r["m1_dist"] for r in out if r.get("m1_dist") is not None]
    cut = float(np.quantile(ds, 0.7)) if len(ds) >= 5 else None
    for r in out:
        r["m1_like"] = None if r.get("m1_dist") is None or cut is None else bool(r["m1_dist"] <= max(cut, 0.6))
    return out


# ---------------------------------------------------------------- recent events (auto list)
def recent_events(cfg: Config, hours: int | None = None, limit: int = 40) -> list[dict[str, Any]]:
    from okxrev.detector import to_grid
    hours = int(hours or lcfg(cfg)["recent_hours"])
    now = db.now_ms()
    since = now - hours * HOUR - (PEAK_BARS + BASE_BARS) * BAR
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        df = con.execute("SELECT inst_id, ts, open, high, low, close, vol, vol_ccy, vol_quote FROM candles WHERE ts >= ? ORDER BY inst_id, ts", [since]).df()
    out = []
    for inst, raw in df.groupby("inst_id", sort=False):
        g = to_grid(raw.drop(columns="inst_id"))
        ts = g.index.to_numpy(np.int64)
        h, lo, c = (g[k].to_numpy(float) for k in ("high", "low", "close"))
        cand = prefilter(h, lo, c, INDEX_RISE_MIN, 0.08)
        cand = cand[ts[cand] >= now - hours * HOUR]
        if len(cand) == 0:
            continue
        sh = shape_at(ts, h, lo, c, cand)
        ok = (sh["rise"] >= INDEX_RISE_MIN) & (sh["drop"] >= 0.08)
        if not ok.any():
            continue
        j = int(np.nanargmax(np.where(ok, sh["drop"], -1)))
        vq = g["vol_quote"].to_numpy(float)
        out.append({"inst": inst, "ts": int(ts[cand[j]]), "pk_ts": int(sh["pk_ts"][j]), "lo_ts": int(sh["lo_ts"][j]), "rise": float(sh["rise"][j]),
                    "drop": float(sh["drop"][j]), "rise_hours": float(sh["rise_bars"][j]) * 5 / 60, "drop_hours": float(sh["drop_bars"][j]) * 5 / 60,
                    "peak": float(sh["peak"][j]), "last": float(c[np.isfinite(c)][-1]), "now_drop": float(1 - c[np.isfinite(c)][-1] / sh["peak"][j]),
                    "turnover24": float(np.nansum(vq[-288:]))})
    out.sort(key=lambda r: -min(r["rise"], 1.0) * r["drop"])
    return out[:limit]


# ---------------------------------------------------------------- CLI (background jobs)
def _write(cfg: Config, name: str, obj: Any) -> Path:
    p = lab_dir(cfg) / name
    p.write_text(json.dumps(obj, ensure_ascii=False, default=_json_default), encoding="utf-8")
    return p


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return int(o.value // 1_000_000)
    return str(o)


def main(argv: list[str] | None = None) -> int:
    from okxrev.log import setup_logging
    ap = argparse.ArgumentParser(prog="python -m okxrev.events_lab")
    ap.add_argument("--config", default=None)
    ap.add_argument("--build-index", action="store_true")
    ap.add_argument("--similar", metavar="INST@TIME", help="analyze + find similar + 1m confirm, e.g. CT-USDT-SWAP@2026-10-13T02:50")
    ap.add_argument("--confirm", type=int, default=None)
    ap.add_argument("--out", default=None, help="result file name inside results/lab/")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "lab")
    if args.build_index:
        def prog(src: str, n: int, tot: int) -> None:
            print(f"索引 {src}：{n}/{tot}", flush=True)
        m = build_index(cfg, prog)
        print(f"完成：{m['rows']:,} 列（{m['by_src']}），隨機樣本 {m['random_rows']:,}，{m['sec']} 秒", flush=True)
        return 0
    if args.similar:
        inst, at = args.similar.split("@", 1)
        if not index_path(cfg).exists():
            print("尚未建立索引，先建立（約 1-3 分鐘）", flush=True)
            build_index(cfg, lambda s, n, t: print(f"索引 {s}：{n}/{t}", flush=True))
        print(f"分析 {inst} {at}", flush=True)
        a = analyze_event(cfg, inst, int(at) if at.isdigit() else parse_utc_ms(at))
        sim = find_similar(cfg, a)
        print(f"5 分 K 相似：{sim['n']} 筆；開始抓 1m 確認", flush=True)
        n = args.confirm if args.confirm is not None else int(lcfg(cfg)["confirm_n"])

        def prog2(j: int, tot: int) -> None:
            if j % 10 == 0 or j == tot:
                print(f"1m 確認 {j}/{tot}", flush=True)
        rows = confirm_similar(cfg, a, sim["rows"], n, prog2) + sim["rows"][n:]
        sim["rows"] = rows
        sim["confirmed_n"] = n
        _write(cfg, args.out or "similar_last.json", {"event": a, "similar": sim, "generated": db.now_ms()})
        print("完成", flush=True)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
