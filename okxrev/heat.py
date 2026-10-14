"""Overheat score.

All indicators are computed on an hourly grid from data closed at that hour (5m candles resampled to 1H,
hourly taker volume, hourly mark/index closes, OI, funding). Each raw indicator becomes a trailing percentile
within the same coin's previous `pct_window_days` (default 30 days, current hour included, nothing after it).
Five dimension scores (0..100) are the mean percentile of their indicators; the overheat score is the mean of the
dimensions that have data. Sentiment (L/S ratio, liquidations) is reported but excluded from the validated score
until enough history exists.

Mapping to 5m bars: an hourly row with timestamp T (the hour [T, T+1h)) becomes known at T+1h, so it is attached
to 5m bars with bar_ts + 5m >= T + 1h (strictly after the hour has closed).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str, parse_utc_ms
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.heat")

HOUR = 3_600_000
DAY = 86_400_000

DIMENSIONS: dict[str, dict[str, Any]] = {
    "stretch": {"label": "價格偏離", "cols": ["dist_ema", "rsi", "bb_pctb", "move_atr", "up_streak"]},
    "flow": {"label": "量能與買盤", "cols": ["taker_ratio_6h", "vol_surge", "div_buy"]},
    "leverage": {"label": "槓桿與擁擠", "cols": ["oi_chg_24h", "oi_turnover", "funding", "funding_chg"]},
    "premium": {"label": "合約溢價", "cols": ["premium", "premium_chg"]},
    "sentiment": {"label": "情緒與爆倉", "cols": ["ls_acct", "ls_top", "liq_short"], "experimental": True},
}
VALIDATED = [k for k, v in DIMENSIONS.items() if not v.get("experimental")]
LABELS = {
    "dist_ema": "距日均線", "rsi": "RSI(14h)", "bb_pctb": "布林位置", "move_atr": "24h 漲幅 / ATR", "up_streak": "連續上漲小時",
    "taker_ratio_6h": "6h 主動買/賣", "vol_surge": "成交額放大", "div_buy": "價漲買弱背離",
    "oi_chg_24h": "OI 24h 變化", "oi_turnover": "OI / 成交額", "funding": "資金費率", "funding_chg": "資金費率變化",
    "premium": "合約溢價", "premium_chg": "溢價 6h 變化", "ls_acct": "多空帳戶比", "ls_top": "大戶多空比", "liq_short": "空單爆倉",
}


def hcfg(cfg: Config) -> dict[str, Any]:
    return {"watchlist": [], "pct_window_days": 30, "alert_threshold": 80, "alert_cooldown_h": 6, "top_n": 10,
            **(cfg.raw.get("heat") or {})}


# ---------------------------------------------------------------- raw indicators (hourly)
def hourly_ohlc(candles: pd.DataFrame) -> pd.DataFrame:
    """5m bars (index = bar open ts ms) -> complete 1H bars (index = hour open ts ms). Incomplete hours dropped.
    Vectorised with numpy reduceat (rows are sorted by time)."""
    g = candles
    ts = g.index.to_numpy(np.int64)
    c = g["close"].to_numpy(float)
    ok = np.isfinite(c)
    ts, c = ts[ok], c[ok]
    if len(ts) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "vq"])
    o = g["open"].to_numpy(float)[ok]
    hi = g["high"].to_numpy(float)[ok]
    lo = g["low"].to_numpy(float)[ok]
    vq = g["vol_quote"].to_numpy(float)[ok] if "vol_quote" in g else np.zeros(len(ts))
    hour = (ts // HOUR) * HOUR
    starts = np.r_[0, np.flatnonzero(np.diff(hour)) + 1]
    ends = np.r_[starts[1:], len(ts)]
    n = ends - starts
    df = pd.DataFrame({"open": o[starts], "high": np.maximum.reduceat(hi, starts), "low": np.minimum.reduceat(lo, starts),
                       "close": c[ends - 1], "vq": np.add.reduceat(np.nan_to_num(vq), starts)}, index=pd.Index(hour[starts], name="h"))
    return df[n >= 12]


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def indicators(h: pd.DataFrame, extra: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Raw indicator values per closed hour. `extra` holds per-coin taker/mark/oi/funding/ls/liq frames."""
    idx = h.index.to_numpy(np.int64)
    c = h["close"]
    out: dict[str, Any] = {}
    ema = c.ewm(span=24, adjust=False, min_periods=24).mean()
    out["dist_ema"] = c / ema - 1
    out["rsi"] = _rsi(c)
    m, s = c.rolling(20, min_periods=20).mean(), c.rolling(20, min_periods=20).std()
    out["bb_pctb"] = (c - (m - 2 * s)) / (4 * s)
    tr = pd.concat([h["high"] - h["low"], (h["high"] - c.shift()).abs(), (h["low"] - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(24, min_periods=24).mean()
    out["move_atr"] = (c - c.shift(24)) / atr
    up = (c.diff() > 0).astype(int)
    out["up_streak"] = up.groupby((up == 0).cumsum()).cumsum().astype(float)
    vq = h["vq"]
    out["vol_surge"] = vq.rolling(6, min_periods=6).sum() / vq.shift(6).rolling(72, min_periods=24).mean() / 6

    def on_grid(df: pd.DataFrame | None, cols: list[str], lag: int = 0) -> pd.DataFrame:
        if df is None or not len(df):
            return pd.DataFrame({k: np.nan for k in cols}, index=h.index)
        d = df.sort_values("ts").drop_duplicates("ts", keep="last")
        k = np.searchsorted(d["ts"].to_numpy(np.int64) + lag, idx, side="right") - 1
        res = {}
        for col in cols:
            v = d[col].to_numpy(float)
            res[col] = np.where(k >= 0, v[np.clip(k, 0, None)], np.nan)
        return pd.DataFrame(res, index=h.index)

    tk = extra.get("taker")
    if tk is not None and len(tk):
        tk = tk[tk["ts"] < (idx[-1] + HOUR if len(idx) else 0)]
        t = tk.drop_duplicates("ts").set_index("ts").reindex(idx)
        buy6 = t["buy"].rolling(6, min_periods=4).sum()
        sell6 = t["sell"].rolling(6, min_periods=4).sum()
        out["taker_ratio_6h"] = pd.Series((buy6 / sell6).to_numpy(), index=h.index)
        buy24 = t["buy"].rolling(24, min_periods=12).sum().to_numpy()
        prev = pd.Series(buy24, index=h.index).shift(24).to_numpy()
        price_up = (c / c.shift(24) - 1).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(prev > 0, buy24 / prev, np.nan)
            out["div_buy"] = pd.Series(np.where(price_up > 0, price_up * (1 - ratio), 0.0), index=h.index)
    else:
        out["taker_ratio_6h"] = np.nan
        out["div_buy"] = np.nan

    mi = extra.get("mark")
    if mi is not None and len(mi):
        x = mi.drop_duplicates("ts").set_index("ts").reindex(idx)
        prem = pd.Series((x["mark"] / x["idx"] - 1).to_numpy(), index=h.index)
        out["premium"] = prem
        out["premium_chg"] = prem - prem.shift(6)
    else:
        out["premium"] = np.nan
        out["premium_chg"] = np.nan

    oi = extra.get("oi")
    if oi is not None and len(oi):
        o = oi[oi["period"] == "1H"]
        if len(o) < 30:
            o = oi[oi["period"] == "1D"]
        g = on_grid(o.rename(columns={"oi_usd": "v"}), ["v"], lag=0)["v"]
        look = 24 if (oi["period"] == "1H").sum() >= 30 else 1
        out["oi_chg_24h"] = g / g.shift(24) - 1 if look == 24 else np.nan
        out["oi_turnover"] = g / vq.rolling(24, min_periods=12).sum()
    else:
        out["oi_chg_24h"] = np.nan
        out["oi_turnover"] = np.nan

    fu = extra.get("funding")
    if fu is not None and len(fu):
        f = on_grid(fu, ["rate"])["rate"]
        out["funding"] = f
        out["funding_chg"] = f - f.shift(24)
    else:
        out["funding"] = np.nan
        out["funding_chg"] = np.nan

    ls = extra.get("ls")
    if ls is not None and len(ls):
        g = on_grid(ls, ["acct", "top"])
        out["ls_acct"], out["ls_top"] = g["acct"], g["top"]
    else:
        out["ls_acct"] = out["ls_top"] = np.nan
    lq = extra.get("liq")
    if lq is not None and len(lq):
        q = lq[lq["pos_side"] == "short"].copy()
        q["h"] = (q["ts"] // HOUR) * HOUR
        usd = (q["sz"] * q["px"]).groupby(q["h"]).sum()
        first = int(lq["ts"].min() // HOUR * HOUR)
        v = usd.reindex(idx).fillna(0).to_numpy()
        out["liq_short"] = pd.Series(np.where(idx >= first, v, np.nan), index=h.index)
    else:
        out["liq_short"] = np.nan
    df = pd.DataFrame({k: (v.to_numpy() if isinstance(v, pd.Series) else np.full(len(h), v)) for k, v in out.items()}, index=h.index)
    return df.replace([np.inf, -np.inf], np.nan)


def trailing_pct(x: pd.DataFrame, window: int, min_periods: int) -> pd.DataFrame:
    """Percentile (0..100) of each value within the trailing `window` rows including itself."""
    return x.rolling(window, min_periods=min_periods).rank(pct=True) * 100


MIN_PCT_HOURS = 72


def score(ind: pd.DataFrame, window_h: int) -> pd.DataFrame:
    """A percentile needs at least MIN_PCT_HOURS of that indicator's own history (fixed, so a partial live history
    such as 4 days of premium never yields a percentile from a handful of samples)."""
    live = [c for c in ind.columns if ind[c].notna().sum() >= MIN_PCT_HOURS]
    pct = trailing_pct(ind[live], window_h, MIN_PCT_HOURS).reindex(columns=ind.columns)
    P = pct.to_numpy(float)
    col = {c: i for i, c in enumerate(pct.columns)}
    cols: dict[str, np.ndarray] = {}
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for k, d in DIMENSIONS.items():
                ix = [col[c] for c in d["cols"] if c in col and c in live]
                cols[k] = np.nanmean(P[:, ix], axis=1) if ix else np.full(len(ind), np.nan)
            V = np.column_stack([cols[k] for k in VALIDATED])
            cols["score"] = np.nanmean(V, axis=1)
    cols["n_dims"] = np.isfinite(V).sum(axis=1)
    for c, i in col.items():
        cols[f"p_{c}"] = P[:, i]
    return pd.DataFrame(cols, index=ind.index)


def cooling(ind: pd.DataFrame, sc: pd.DataFrame, h: pd.DataFrame) -> pd.Series:
    """Cooling after heat: score was >= 70 within 6h and now at least one of
    taker buying turns weaker than selling, OI falls over 3h, a large red hourly candle, premium narrows."""
    hot = sc["score"].rolling(6, min_periods=1).max() >= 70
    c = h["close"]
    red = (c / h["open"] - 1) <= -0.03
    weak_buy = ind["taker_ratio_6h"] < 1.0
    oi_dn = ind["oi_chg_24h"] < ind["oi_chg_24h"].shift(3) - 0.05
    prem_dn = ind["premium_chg"] < 0
    return (hot & (red | weak_buy | oi_dn | prem_dn)).fillna(False)


def coin_heat(candles: pd.DataFrame, extra: dict[str, pd.DataFrame], window_days: int = 30) -> pd.DataFrame:
    """Hourly frame: raw indicators, percentiles, dimension scores, score, cooling."""
    h = hourly_ohlc(candles)
    if len(h) < 72:
        return pd.DataFrame()
    ind = indicators(h, extra)
    sc = score(ind, window_days * 24)
    more = {"cooling": cooling(ind, sc, h).to_numpy(bool), "close": h["close"].to_numpy(float)}
    more.update({f"raw_{c}": ind[c].to_numpy() for c in ind.columns})
    return pd.concat([sc, pd.DataFrame(more, index=sc.index)], axis=1)


def to_bars(hourly: pd.DataFrame, bar_index: pd.Index, cols: list[str]) -> pd.DataFrame:
    """Attach hourly values to 5m bars once the hour has closed (bar close >= hour close)."""
    idx = bar_index.to_numpy(np.int64)
    if hourly.empty:
        return pd.DataFrame({c: np.nan for c in cols}, index=bar_index)
    known = hourly.index.to_numpy(np.int64) + HOUR
    k = np.searchsorted(known, idx + db.BAR_MS, side="right") - 1
    res = {}
    for c in cols:
        v = hourly[c].to_numpy()
        res[c] = np.where(k >= 0, v[np.clip(k, 0, None)], np.nan) if v.dtype.kind in "fc" else \
            np.where(k >= 0, v[np.clip(k, 0, None)], False)
    return pd.DataFrame(res, index=bar_index)


# ---------------------------------------------------------------- market-wide current snapshot
_KEYS = {"taker": "taker_volume", "mark": "mark_index", "ls": "ls_ratio", "liq": "liquidations", "oi": "open_interest", "funding": "funding"}


def split_extra(extra_all: dict[str, pd.DataFrame]) -> dict[str, dict[str, pd.DataFrame]]:
    """Pre-split every extra table by instrument once (instead of boolean-masking each table per coin)."""
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for short, table in _KEYS.items():
        d = extra_all.get(table)
        if d is None or not len(d):
            continue
        for inst, g in d.groupby("inst_id", sort=False):
            out.setdefault(inst, {})[short] = g
    return out


def _per_coin(extra_all: dict[str, Any], inst: str) -> dict[str, pd.DataFrame]:
    if "__split__" in extra_all:
        return extra_all["__split__"].get(inst, {})
    def sub(k: str) -> pd.DataFrame:
        d = extra_all.get(k)
        return d[d["inst_id"] == inst] if d is not None and len(d) else pd.DataFrame()
    return {short: sub(table) for short, table in _KEYS.items()}


def load_extra(con: Any, since: int | None) -> dict[str, pd.DataFrame]:
    from okxrev.heatdata import load_all
    import duckdb
    ex = load_all(con, since=since)
    for t, q in (("open_interest", "SELECT inst_id, period, ts, oi_usd FROM open_interest"), ("funding", "SELECT inst_id, ts, rate FROM funding")):
        try:
            ex[t] = con.execute(q + (" WHERE ts >= ?" if since else ""), [since] if since else []).df()
        except duckdb.CatalogException:
            ex[t] = pd.DataFrame()
    return {k: (v.sort_values("ts") if len(v) else v) for k, v in ex.items()}


def snapshot(cfg: Config, candles: dict[str, pd.DataFrame] | None = None, now: int | None = None) -> dict[str, Any]:
    """Current heat for every coin (latest closed hour) + market rank. Used by the web page and alerts."""
    hc = hcfg(cfg)
    wd = int(hc["pct_window_days"])
    now = now or db.now_ms()
    since = now - (wd + 3) * DAY
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        if candles is None:
            df = con.execute("SELECT inst_id, ts, open, high, low, close, vol_quote FROM candles WHERE ts >= ? ORDER BY inst_id, ts", [since]).df()
            candles = {i: g.drop(columns="inst_id").set_index("ts") for i, g in df.groupby("inst_id")}
        extra = load_extra(con, since)
    extra = {"__split__": split_extra(extra)}
    rows = []
    hist: dict[str, pd.DataFrame] = {}
    for inst, g in candles.items():
        sc = coin_heat(g, _per_coin(extra, inst), wd)
        if sc.empty or not sc["score"].notna().any():
            continue
        last = sc.iloc[-1]
        top = sorted(((DIMENSIONS[k]["label"], float(last[k])) for k in DIMENSIONS if pd.notna(last[k])), key=lambda x: -x[1])
        drivers = sorted(((LABELS.get(c[2:], c), float(last[c])) for c in sc.columns if c.startswith("p_") and pd.notna(last[c])),
                         key=lambda x: -x[1])[:3]
        c24 = sc["close"].iloc[-25] if len(sc) > 24 else np.nan
        rows.append({
            "inst_id": inst, "ts": int(sc.index[-1]) + HOUR, "score": float(last["score"]) if pd.notna(last["score"]) else None,
            "n_dims": int(last["n_dims"]), "cooling": bool(last["cooling"]),
            **{k: (float(last[k]) if pd.notna(last[k]) else None) for k in DIMENSIONS},
            "ret_24h": float(sc["close"].iloc[-1] / c24 - 1) if pd.notna(c24) else None,
            "funding": _nz(last.get("raw_funding")), "oi_chg_24h": _nz(last.get("raw_oi_chg_24h")), "premium": _nz(last.get("raw_premium")),
            "taker_ratio_6h": _nz(last.get("raw_taker_ratio_6h")), "ls_acct": _nz(last.get("raw_ls_acct")),
            "drivers": drivers, "dims_sorted": top,
            "score_6h_max": float(sc["score"].iloc[-6:].max()) if sc["score"].iloc[-6:].notna().any() else None,
        })
        hist[inst] = sc
    snap = pd.DataFrame(rows)
    if len(snap):
        snap["rank"] = snap["score"].rank(ascending=False, method="min")
        snap = snap.sort_values("score", ascending=False)
    return {"ts": now, "rows": snap.to_dict("records"), "n": int(len(snap)), "_hist": hist}


def _nz(v: Any) -> float | None:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def coin_detail(cfg: Config, inst: str, days: int = 10) -> dict[str, Any]:
    hc = hcfg(cfg)
    wd = int(hc["pct_window_days"])
    now = db.now_ms()
    since = now - (wd + days + 2) * DAY
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        g = db.load_candles(con, inst, since).set_index("ts")
        extra = load_extra(con, since)
        try:
            liq = con.execute("SELECT ts, pos_side, px, sz FROM liquidations WHERE inst_id = ? ORDER BY ts DESC LIMIT 200", [inst]).df()
        except Exception:
            liq = pd.DataFrame()
    sc = coin_heat(g, _per_coin(extra, inst), wd)
    if sc.empty:
        return {"inst_id": inst, "empty": True}
    sc = sc[sc.index >= now - days * DAY]
    t = (sc.index.to_numpy(np.int64) + HOUR) // 1000
    series = {k: [None if pd.isna(v) else round(float(v), 2) for v in sc[k]] for k in ["score", *DIMENSIONS]}
    raws = {c[4:]: [None if pd.isna(v) else float(v) for v in sc[c]] for c in sc.columns if c.startswith("raw_")}
    last = sc.iloc[-1]
    return {"inst_id": inst, "t": t.tolist(), "series": series, "raw": raws, "cooling": sc["cooling"].astype(bool).tolist(),
            "close": sc["close"].round(8).tolist(),
            "now": {"score": _nz(last["score"]), **{k: _nz(last[k]) for k in DIMENSIONS},
                    "indicators": {c[2:]: {"label": LABELS.get(c[2:], c[2:]), "pct": _nz(last[c]), "raw": _nz(last.get(f"raw_{c[2:]}"))}
                                   for c in sc.columns if c.startswith("p_")}},
            "dims": {k: {"label": v["label"], "experimental": bool(v.get("experimental"))} for k, v in DIMENSIONS.items()},
            "liquidations": liq.to_dict("records") if len(liq) else []}


# ---------------------------------------------------------------- focus list and alerts
def focus_insts(cfg: Config) -> list[str]:
    """Watchlist + top-N from the last saved snapshot (for the high-frequency L/S and liquidation feeds)."""
    hc = hcfg(cfg)
    out = list(hc.get("watchlist") or [])
    p = cfg.paths.results / "heat_snapshot.json"
    if p.exists():
        try:
            rows = json.loads(p.read_text(encoding="utf-8")).get("rows", [])
            out += [r["inst_id"] for r in rows[: int(hc["top_n"])]]
        except (ValueError, KeyError):
            pass
    return list(dict.fromkeys(out))


ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS heat_alerts (
    ts BIGINT, inst_id VARCHAR, kind VARCHAR, score DOUBLE, detail VARCHAR, PRIMARY KEY (ts, inst_id, kind)
);
"""


def alerts_for(snap_rows: list[dict[str, Any]], watch: set[str], threshold: float, rules_hit: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Alert candidates: watchlist coins crossing the threshold (6h max was below), cooling after heat, rule03/04 hits."""
    out = []
    for r in snap_rows:
        inst = r["inst_id"]
        if inst not in watch:
            continue
        s = r.get("score")
        if s is not None and s >= threshold:
            out.append({"inst_id": inst, "kind": "hot", "score": s, "detail": "、".join(f"{a} {b:.0f}" for a, b in r["drivers"])})
        if r.get("cooling"):
            out.append({"inst_id": inst, "kind": "cooling", "score": s, "detail": "過熱後出現降溫訊號"})
        if rules_hit and inst in rules_hit:
            out.append({"inst_id": inst, "kind": "rule", "score": s, "detail": rules_hit[inst]})
    return out


def record_alerts(cfg: Config, con: Any, cands: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
    """Insert alerts unless the same (inst, kind) fired within the cooldown."""
    con.execute(ALERT_SCHEMA)
    cool = int(hcfg(cfg)["alert_cooldown_h"]) * HOUR
    new = []
    for a in cands:
        last = con.execute("SELECT MAX(ts) FROM heat_alerts WHERE inst_id = ? AND kind = ?", [a["inst_id"], a["kind"]]).fetchone()[0]
        if last is not None and now - int(last) < cool:
            continue
        con.execute("INSERT OR REPLACE INTO heat_alerts VALUES (?, ?, ?, ?, ?)", [now, a["inst_id"], a["kind"], a.get("score"), a.get("detail")])
        new.append({**a, "ts": now})
    return new


def scores_path(cfg: Config) -> Any:
    return cfg.paths.results / "heat_scores.parquet"


def refresh(cfg: Config, now: int) -> tuple[dict[str, Any], bool]:
    """Market snapshot + hourly score history, recomputed once per clock hour and saved (heat_snapshot.json and
    heat_scores.parquet). Returns (snapshot dict, recomputed?)."""
    hc = hcfg(cfg)
    out = cfg.paths.results / "heat_snapshot.json"
    hour = (now // HOUR) * HOUR
    prev = None
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except ValueError:
            prev = None
    if prev and prev.get("hour") == hour and prev.get("rows") and scores_path(cfg).exists():
        complete = (prev.get("last_h") or 0) >= hour - HOUR
        if complete or now - hour > 20 * 60_000 or now - int(prev.get("ts") or 0) < 4 * 60_000:
            return prev, False
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as dcon:
        since = now - (int(hc["pct_window_days"]) + 3) * DAY
        df = dcon.execute("SELECT inst_id, ts, open, high, low, close, vol_quote FROM candles WHERE ts >= ? ORDER BY inst_id, ts", [since]).df()
    full = {i: g.drop(columns="inst_id").set_index("ts") for i, g in df.groupby("inst_id")}
    snap = snapshot(cfg, full, now)
    keep_from = now - 3 * DAY
    parts = [pd.DataFrame({"inst": i, "h": sc.index.to_numpy(np.int64), "score": sc["score"].to_numpy(float), "cooling": sc["cooling"].to_numpy(bool)})
             .query("h >= @keep_from") for i, sc in snap["_hist"].items()]
    sp = scores_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["inst", "h", "score", "cooling"])).to_parquet(sp.with_suffix(".tmp"), index=False)
    sp.with_suffix(".tmp").replace(sp)
    last_h = max((int(sc.index.max()) for sc in snap["_hist"].values() if len(sc)), default=0)
    data = {"ts": now, "hour": hour, "last_h": last_h, "rows": snap["rows"], "alerted_hour": (prev or {}).get("alerted_hour")}
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=_json), encoding="utf-8")
    tmp.replace(out)
    return data, True


def load_scores(cfg: Config) -> pd.DataFrame:
    p = scores_path(cfg)
    return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=["inst", "h", "score", "cooling"])


def tick(cfg: Config, candles: dict[str, pd.DataFrame], feats: dict[str, pd.DataFrame], con: Any, now: int) -> dict[str, Any]:
    """Called from the paper tick: refresh the snapshot once per closed hour (all indicators are hourly), then
    record alerts (stored in paper.duckdb). Hot / cooling alerts once per hour; rule-hit alerts every tick."""
    hc = hcfg(cfg)
    data, fresh = refresh(cfg, now)
    rows = data["rows"]
    hour = data["hour"]
    rules_hit = _rules_hit(con, now)
    watch = set(hc.get("watchlist") or []) | {r["inst_id"] for r in rows[: int(hc["top_n"])]}
    cands = alerts_for(rows, watch, float(hc["alert_threshold"]), rules_hit)
    full = data.get("alerted_hour") != hour
    if not full:
        cands = [a for a in cands if a["kind"] == "rule"]
    new = record_alerts(cfg, con, cands, now)
    if full:
        out = cfg.paths.results / "heat_snapshot.json"
        data["alerted_hour"] = hour
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=_json), encoding="utf-8")
        tmp.replace(out)
    return {"n": len(rows), "alerts": len(new), "cached": not fresh}


def _rules_hit(con: Any, now: int) -> dict[str, str]:
    """Coins where the blind-test-validated rules (rule03 / rule04) fired in the last hour."""
    try:
        r = con.execute("SELECT inst_id, trader_id FROM decisions WHERE bar_ts >= ? AND (trader_id LIKE 'rule03%' OR trader_id LIKE 'rule04%')",
                        [now - HOUR]).fetchall()
        return {i: f"觸發 {t}" for i, t in r}
    except Exception:
        return {}


def _json(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


# ---------------------------------------------------------------- validation
def _forward(h: pd.DataFrame, hours: int, cost: float) -> np.ndarray:
    """Net short return entering at the next hour's open, exiting at the close `hours` later."""
    o = h["open"].shift(-1).to_numpy(float)
    c = h["close"].shift(-hours).to_numpy(float)
    return -(c / o - 1) - cost


STOPS = (0.10, 0.20)


def _path(h: pd.DataFrame, hours: int) -> tuple[np.ndarray, np.ndarray]:
    """Highest high / lowest low over the `hours` hourly bars after the signal hour (entry hour .. exit hour)."""
    hh = h["high"].rolling(hours, min_periods=hours).max().shift(-hours).to_numpy(float)
    ll = h["low"].rolling(hours, min_periods=hours).min().shift(-hours).to_numpy(float)
    return hh, ll


def forward_risk(h: pd.DataFrame, cost: float, fund: pd.DataFrame | None) -> dict[str, np.ndarray]:
    """Stop-loss variants, max adverse / favourable excursion and funding for a 24h short (and 6h with a 10% stop).
    A stop fills at exactly entry*(1+stop) (no gap slippage); if the stop is touched in any bar of the window it
    counts as stopped, even if the same bar also reached a profit level."""
    o = h["open"].shift(-1).to_numpy(float)
    out: dict[str, np.ndarray] = {}
    for hours in (6, 24):
        base = _forward(h, hours, cost)
        hh, ll = _path(h, hours)
        up = hh / o - 1
        for s in STOPS:
            if hours == 6 and s != 0.10:
                continue
            out[f"s_{hours}h_sl{int(s * 100)}"] = np.where(up >= s, -s - cost, base)
        if hours == 24:
            out["mae_24h"] = up
            out["mfe_24h"] = 1 - ll / o
    idx = h.index.to_numpy(np.int64)
    f24 = np.full(len(idx), np.nan)
    if fund is not None and len(fund):
        fd = fund.sort_values("ts").drop_duplicates("ts")
        ft = fd["ts"].to_numpy(np.int64)
        cs = np.r_[0.0, np.cumsum(np.nan_to_num(fd["rate"].to_numpy(float)))]
        entry = idx + HOUR
        a = np.searchsorted(ft, entry, side="right")
        b = np.searchsorted(ft, entry + 24 * HOUR, side="right")
        covered = (entry >= ft[0]) & (entry + 24 * HOUR <= ft[-1] + 8 * HOUR)
        f24 = np.where(covered, cs[b] - cs[a], np.nan)
    out["fund_24h"] = f24
    return out


def events_for_period(cfg: Config, db_path: Any, start: int, end: int, window_days: int, progress: bool = True) -> pd.DataFrame:
    """Hourly score + forward short returns for every coin in [start, end) from the given candle database."""
    from okxrev import replay
    fee, slip = float(cfg.metrics["fee_per_side"]), float(cfg.metrics["slippage_per_side"])
    warm = (window_days + 3) * DAY
    with db.open_db(db_path, read_only=True, wait_sec=2.0, max_tries=60) as con:
        insts = db.list_candle_insts(con)
        extra = load_extra(con, start - warm)
    if db_path != cfg.paths.db:
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con2:
            live = load_extra(con2, start - warm)
        for k in ("funding", "open_interest"):
            if k in live and len(live[k]):
                extra[k] = pd.concat([extra.get(k, pd.DataFrame()), live[k]], ignore_index=True)
    extra = {"__split__": split_extra(extra)}
    rows = []
    for n, inst in enumerate(insts, 1):
        with db.open_db(db_path, read_only=True, wait_sec=2.0, max_tries=60) as con:
            g = con.execute("SELECT ts, open, high, low, close, vol_quote FROM candles WHERE inst_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                            [inst, start - warm, end + 2 * DAY]).df().set_index("ts")
        if len(g) < 2000:
            continue
        sc = coin_heat(g, _per_coin(extra, inst), window_days)
        if sc.empty:
            continue
        h = hourly_ohlc(g).reindex(sc.index)
        small = float(np.nanmedian(g["vol_quote"].rolling(288).sum())) < 5e6
        cost = 2 * (fee + slip + (0.001 if small else 0.0))
        keep = (sc.index >= start) & (sc.index < end)
        fu = _per_coin(extra, inst).get("funding")
        d = pd.DataFrame({"inst": inst, "ts": sc.index.to_numpy(np.int64), "score": sc["score"].to_numpy(),
                          "cooling": sc["cooling"].to_numpy(bool), "n_dims": sc["n_dims"].to_numpy(),
                          **{k: sc[k].to_numpy() for k in DIMENSIONS},
                          **{f"s_{hh}h": _forward(h, hh, cost) for hh in (1, 6, 24)},
                          **{k: v.astype(np.float32) for k, v in forward_risk(h, cost, fu).items()}})
        rows.append(d[keep])
        if progress and n % 50 == 0:
            log.info("heat events %d/%d", n, len(insts))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def score_history(cfg: Config, db_path: Any, start: int, end: int) -> pd.DataFrame:
    """Hourly score + cooling for every coin in [start, end) from a candle database; cached as parquet."""
    from pathlib import Path
    wd = int(hcfg(cfg)["pct_window_days"])
    cache = cfg.paths.results / f"heat_hist_{Path(str(db_path)).stem}_{start // DAY}_{end // DAY}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    warm = (wd + 3) * DAY
    with db.open_db(db_path, read_only=True, wait_sec=2.0, max_tries=60) as con:
        insts = db.list_candle_insts(con)
        extra = load_extra(con, start - warm)
    if Path(str(db_path)) != Path(str(cfg.paths.db)):
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con2:
            live_ex = load_extra(con2, start - warm)
        for k in ("funding", "open_interest"):
            if k in live_ex and len(live_ex[k]):
                extra[k] = pd.concat([extra.get(k, pd.DataFrame()), live_ex[k]], ignore_index=True)
    extra = {"__split__": split_extra(extra)}
    parts = []
    for n, inst in enumerate(insts, 1):
        with db.open_db(db_path, read_only=True, wait_sec=2.0, max_tries=60) as con:
            g = con.execute("SELECT ts, open, high, low, close, vol_quote FROM candles WHERE inst_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                            [inst, start - warm, end]).df().set_index("ts")
        if len(g) < 300:
            continue
        sc = coin_heat(g, _per_coin(extra, inst), wd)
        if sc.empty:
            continue
        sc = sc[sc.index >= start - DAY]
        parts.append(pd.DataFrame({"inst": inst, "h": sc.index.to_numpy(np.int64), "score": sc["score"].to_numpy(float),
                                   "cooling": sc["cooling"].to_numpy(bool)}))
        if n % 50 == 0:
            log.info("heat history %d/%d", n, len(insts))
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["inst", "h", "score", "cooling"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(cache, index=False)
    return out


def episode_starts(d: pd.DataFrame, mask: np.ndarray, gap_h: int = 24) -> pd.DataFrame:
    """First hour of each episode per coin (condition false for the previous gap_h hours)."""
    d = d.assign(_m=mask).sort_values(["inst", "ts"])
    out = []
    for inst, g in d.groupby("inst", sort=False):
        m = g["_m"].to_numpy()
        cs = np.cumsum(m)
        prev = np.r_[0, cs[:-1]]
        back = np.r_[np.zeros(gap_h + 1, int), cs[: max(len(cs) - gap_h - 1, 0)]][: len(cs)]
        out.append(g[m & ((prev - back) == 0)])
    return pd.concat(out) if out else d.iloc[0:0]


def evaluate(cfg: Config, progress: bool = True) -> dict[str, Any]:
    from okxrev import replay
    from okxrev.discover import day_stats
    hc = hcfg(cfg)
    wd = int(hc["pct_window_days"])
    t0 = time.monotonic()
    rc = replay.rcfg(cfg)
    old_start, old_end = parse_utc_ms(rc["start"]), parse_utc_ms(rc["end"])
    with db.open_db(cfg.paths.db, read_only=True) as con:
        lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM candles").fetchone()
    new_start = max(int(lo) + (wd + 3) * DAY, old_end + 4 * DAY)
    new_end = int(hi) - 2 * DAY
    log.info("heat discovery period %s .. %s", ms_to_str(new_start), ms_to_str(new_end))
    disc = events_for_period(cfg, cfg.paths.db, new_start, new_end, wd, progress)
    blind = events_for_period(cfg, replay.old_db_path(cfg), old_start, old_end, wd, progress) if replay.old_db_path(cfg).exists() else pd.DataFrame()

    def stats(d: pd.DataFrame, mask: np.ndarray, col: str) -> dict[str, Any]:
        e = episode_starts(d, mask)
        x = e[col].dropna()
        s = day_stats(x.to_numpy(float), e.loc[x.index, "ts"].to_numpy(np.int64))
        s["n_insts"] = int(e["inst"].nunique())
        return s

    def base(d: pd.DataFrame, col: str) -> float:
        x = d[col].dropna()
        return float(x.mean()) if len(x) else 0.0

    variants: list[tuple[str, str, Any]] = []
    for thr in (50, 70, 80, 90):
        variants.append((f"score≥{thr}", "score", lambda d, t=thr: (d["score"] >= t).to_numpy()))
        variants.append((f"score≥{thr} + 降溫", "score+cool", lambda d, t=thr: ((d["score"] >= t) & d["cooling"]).to_numpy()))
    for k in VALIDATED:
        variants.append((f"{DIMENSIONS[k]['label']}≥90", k, lambda d, kk=k: (d[kk] >= 90).to_numpy()))
    results = []
    for name, kind, fn in variants:
        row: dict[str, Any] = {"name": name, "kind": kind}
        for col in ("s_1h", "s_6h", "s_24h"):
            dv = stats(disc, fn(disc), col) if len(disc) else {"n": 0}
            bv = stats(blind, fn(blind), col) if len(blind) else {"n": 0}
            row[col] = {"disc": dv, "blind": bv, "base_disc": base(disc, col) if len(disc) else None,
                        "base_blind": base(blind, col) if len(blind) else None}
        results.append(row)
    for r in results:
        best = max(("s_1h", "s_6h", "s_24h"), key=lambda c: (r[c]["disc"].get("t") or -9))
        r["best_h"] = best
        dv, bv = r[best]["disc"], r[best]["blind"]
        good = lambda s, b: s.get("n_days", 0) >= 10 and s.get("ci95") is not None and s["ci95"][0] > 0 and s.get("day_mean", -1) > (b or 0)
        pos = lambda s, b: s.get("n", 0) > 0 and s.get("day_mean", -1) > max(b or 0, 0)
        r["verdict"] = "可用" if good(dv, r[best]["base_disc"]) and good(bv, r[best]["base_blind"]) else \
            ("觀察中" if pos(dv, r[best]["base_disc"]) and pos(bv, r[best]["base_blind"]) else "無效")
    risk = []
    for name, kind, fn in variants:
        if kind not in ("score", "stretch"):
            continue
        rr: dict[str, Any] = {"name": name}
        for per, d in (("disc", disc), ("blind", blind)):
            if not len(d):
                continue
            e = episode_starts(d, fn(d))
            ts_ = e["ts"].to_numpy(np.int64)
            cell: dict[str, Any] = {"n": int(len(e))}
            for col in ("s_24h", "s_24h_sl10", "s_24h_sl20", "s_6h", "s_6h_sl10"):
                if col in e:
                    x = e[col].to_numpy(float)
                    ok = np.isfinite(x)
                    s = day_stats(x[ok], ts_[ok], boot=300)
                    cell[col] = {k: s.get(k) for k in ("n", "n_days", "day_mean", "win_rate", "ci95")}
                    cell[f"base_{col}"] = base(d, col)
            if "mae_24h" in e:
                mae = e["mae_24h"].to_numpy(float)
                mae = mae[np.isfinite(mae)]
                if len(mae):
                    cell["mae"] = {"p50": float(np.quantile(mae, 0.5)), "p75": float(np.quantile(mae, 0.75)), "p90": float(np.quantile(mae, 0.9)),
                                   "p95": float(np.quantile(mae, 0.95)), "max": float(mae.max()),
                                   **{f"hit_{int(s * 100)}": float((mae >= s).mean()) for s in (0.05, 0.1, 0.2, 0.3, 0.5)}}
                mfe = e["mfe_24h"].to_numpy(float)
                mfe = mfe[np.isfinite(mfe)]
                if len(mfe):
                    cell["mfe_p50"] = float(np.quantile(mfe, 0.5))
            if "fund_24h" in e:
                f = e["fund_24h"].to_numpy(float)
                ok = np.isfinite(f)
                cell["funding"] = {"n": int(ok.sum()), "mean_24h": float(f[ok].mean()) if ok.any() else None,
                                   "p90_24h": float(np.quantile(f[ok], 0.9)) if ok.sum() >= 10 else None}
                if ok.sum() >= 30:
                    x = (e["s_24h"].to_numpy(float) + f)[ok]
                    s = day_stats(x[np.isfinite(x)], ts_[ok][np.isfinite(x)], boot=300)
                    cell["s_24h_with_funding"] = {k: s.get(k) for k in ("n", "n_days", "day_mean", "win_rate", "ci95")}
            rr[per] = cell
        risk.append(rr)
    corr = {}
    if len(disc):
        for k in VALIDATED:
            m = disc[[k, "s_24h"]].dropna()
            corr[k] = float(m[k].corr(m["s_24h"], method="spearman")) if len(m) > 100 else None
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1),
           "discovery": [ms_to_str(new_start), ms_to_str(new_end)], "blind": [ms_to_str(old_start), ms_to_str(old_end)],
           "n_disc_hours": int(len(disc)), "n_blind_hours": int(len(blind)),
           "coverage": {k: {"disc": float(disc[k].notna().mean()) if len(disc) else None,
                            "blind": float(blind[k].notna().mean()) if len(blind) else None} for k in DIMENSIONS},
           "results": results, "risk": risk, "spearman_24h": corr,
           "notes": ["報酬為做空淨報酬（下一小時開盤進場，扣手續費與滑價，未計資金費）",
                     "風險表：止損以小時 K 最高價判斷，觸及即以止損價出場（不計跳空滑價）；MAE＝24 小時內對空單最不利的漲幅；"
                     "資金費以實際費率加總（空單收正費率），只有 2026-06 之後的資料",
                     "同一幣種 24 小時內只算第一次觸發；信賴區間以每日平均 bootstrap",
                     "門檻與指標在 2025-10 之後的資料檢視，盲測期 2024-10 ～ 2025-09 只測一次",
                     "槓桿類（OI、資金費率）在盲測期幾乎沒有資料，該期分數只由其他類別組成",
                     "情緒與爆倉類尚無歷史，未納入分數"]}
    p = cfg.paths.results / "heat.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=_json), encoding="utf-8")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.heat")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--coin", default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "heat")
    if args.evaluate:
        r = evaluate(cfg)
        print(json.dumps({k: v for k, v in r.items() if k != "results"}, ensure_ascii=False, indent=1, default=_json))
        for x in r["results"]:
            b = x[x["best_h"]]
            print(f"[{x['verdict']}] {x['name']:<22} {x['best_h']}  發現 n={b['disc'].get('n', 0):>5} 日均 {(b['disc'].get('day_mean') or 0) * 100:+.2f}%"
                  f" (基準 {(b['base_disc'] or 0) * 100:+.2f}%) | 盲測 n={b['blind'].get('n', 0):>5} 日均 {(b['blind'].get('day_mean') or 0) * 100:+.2f}%"
                  f" (基準 {(b['base_blind'] or 0) * 100:+.2f}%)")
    if args.snapshot:
        s = snapshot(cfg)
        for r in s["rows"][:15]:
            print(f"{r['inst_id']:<20} {r['score']:5.1f} {'降溫' if r['cooling'] else '':<4} {r['drivers']}")
    if args.coin:
        d = coin_detail(cfg, args.coin)
        print(json.dumps(d.get("now"), ensure_ascii=False, indent=1, default=_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
