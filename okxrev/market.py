"""Market overview for the web page: what the whole market is doing now, and what the paper traders did meanwhile.

  hourly        BTC / ETH / alt index cumulative return and breadth (share of swaps above their close 24h earlier)
  now           BTC / ETH price and returns, alt median 24h / 7d, today's alt-index streak (same definition as
                regime.py: UTC-day median log return of alts listed >= 30 days), BTC 4h strength (D19 thresholds)
  derivs        medians over the latest heat snapshot (funding, OI 24h change, taker ratio, L/S accounts) and 24h
                liquidations by side (OKX only returns the latest ~100 per coin, so this is a lower bound)
  movers        24h gainers / losers / volume surges (24h quote volume vs the 7-day daily average)
  traders       book C positions that were open at any time in the window: hourly long / short counts per trader
                kind, per-trader activity and closed-trade returns, split by the alt index 24h direction at entry

Only reads the databases (read-only, closed right after) and keeps the result for a minute.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db, regime
from okxrev.config import Config, ms_to_str

HOUR = 3_600_000
D = regime.D
EXCL = regime.EXCL
MIN_VQ_SURGE = 500_000.0
CACHE_SEC = 60

_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _med(xs: list[Any]) -> float | None:
    a = np.array([x for x in (_f(v) for v in xs) if x is not None])
    return float(np.median(a)) if len(a) else None


# ---------------------------------------------------------------- candles
def hourly_panel(cfg: Config, since: int) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """(close, quote volume) per hour x USDT swap from the 5-minute candles; the current hour holds the latest close."""
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=1.0, max_tries=20) as con:
        df = con.execute(f"""
            SELECT inst_id, (ts // {HOUR}) * {HOUR} AS h, arg_max(close, ts) AS c, SUM(vol_quote) AS vq
            FROM candles WHERE inst_id LIKE '%-USDT-SWAP' AND ts >= ? GROUP BY 1, 2""", [since]).df()
        try:
            lt = {k: int(v) for k, v in con.execute("SELECT inst_id, list_time FROM instruments WHERE list_time IS NOT NULL").fetchall()}
        except Exception:
            lt = {}
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), lt
    C = df.pivot(index="h", columns="inst_id", values="c").sort_index()
    V = df.pivot(index="h", columns="inst_id", values="vq").reindex(C.index)
    full = np.arange(int(C.index.min()), int(C.index.max()) + HOUR, HOUR)
    return C.reindex(full).ffill(limit=3), V.reindex(full).fillna(0.0), lt


def _alts(C: pd.DataFrame, lt: dict[str, int], at: int) -> list[str]:
    """Alts listed >= 30 days at `at` (list time when known, else the first hour with data)."""
    first = C.apply(lambda s: s.first_valid_index())
    out = []
    for k in C.columns:
        if k.split("-")[0] in EXCL:
            continue
        t0 = lt.get(k, first[k])
        if t0 is not None and at - int(t0) >= regime.MIN_AGE * D:
            out.append(k)
    return out


def _ret(s: pd.Series, hours: int) -> float | None:
    s = s.dropna()
    if len(s) <= hours:
        return None
    return _f(s.iloc[-1] / s.iloc[-1 - hours] - 1)


def market(cfg: Config, days: int, now: int) -> tuple[dict[str, Any], pd.Series]:
    """Everything that comes from the candles. Also returns the hourly alt-index 24h change (for the traders split)."""
    start = (now // HOUR) * HOUR - days * D
    C, V, lt = hourly_panel(cfg, start - max(days, 8) * D - D)
    if C.empty:
        return {}, pd.Series(dtype=float)
    alts = _alts(C, lt, start)
    lr = np.log(C).diff()
    alt_lr = lr[alts].median(axis=1).where(lr[alts].notna().sum(axis=1) >= 20)
    alt_24 = np.exp(alt_lr.rolling(24, min_periods=20).sum()) - 1
    above = (C > C.shift(24)).where(C.notna() & C.shift(24).notna())
    breadth = above.mean(axis=1).where(above.notna().sum(axis=1) >= 30)
    win = C.index >= start
    idx = C.index[win]

    def cum(s: pd.Series) -> list[float | None]:
        s = s[win]
        return [_f(v) for v in (s - 1)]
    btc, eth = C.get("BTC-USDT-SWAP"), C.get("ETH-USDT-SWAP")
    alt_cum = np.exp(alt_lr[win].fillna(0.0).cumsum())
    hourly = {"t": (idx // 1000).tolist(), "alt": [_f(v) for v in (alt_cum - 1)],
              "btc": cum(btc / btc[win].dropna().iloc[0]) if btc is not None and btc[win].notna().any() else [],
              "eth": cum(eth / eth[win].dropna().iloc[0]) if eth is not None and eth[win].notna().any() else [],
              "breadth": [_f(v) for v in breadth[win]]}

    def coin(s: pd.Series | None) -> dict[str, Any] | None:
        if s is None or not s.notna().any():
            return None
        return {"px": _f(s.dropna().iloc[-1]), **{f"r{h}h": _ret(s, h) for h in (1, 4, 24, 168)}}
    r24 = (C.iloc[-1] / C.shift(24).iloc[-1] - 1)
    r7d = (C.iloc[-1] / C.shift(168).iloc[-1] - 1)
    alts_now = _alts(C, lt, now)
    up24 = r24.dropna()

    # today's streak from finished UTC days (regime.py definition; the day closes at hour 23)
    last_full = (now // D) * D
    daily = C[(C.index % D == D - HOUR) & (C.index < last_full)]
    daily.index = daily.index - (D - HOUR)
    daily.attrs["list_time"] = {k: v for k, v in lt.items() if k in daily.columns}
    streak: dict[str, Any] = {}
    if len(daily) >= 3:
        ix = regime.alt_index(daily, min_n=20)
        st = regime.streaks(ix["ret"])
        if len(st):
            streak = {"day": ms_to_str(int(st.index[-1]), "%Y-%m-%d"), "down_days": int(st["dn"].iloc[-1]), "up_days": int(st["up"].iloc[-1]),
                      "last_ret": _f(np.expm1(ix["ret"].iloc[-1])),
                      "days": [{"day": ms_to_str(int(d), "%Y-%m-%d"), "ret": _f(np.expm1(v))} for d, v in ix["ret"][ix.index >= start].items()]}
    b4 = _ret(btc, 4) if btc is not None else None
    reg = cfg.btc_regime
    btc_state = None if b4 is None else "weak" if b4 <= reg["ret_4h_weak"] else "strong" if b4 >= reg["ret_4h_strong"] else "neutral"

    vq24 = V.iloc[-24:].sum()
    vq7 = V.iloc[-168:].sum() / 7
    rows = pd.DataFrame({"r24": r24, "vq24": vq24, "surge": vq24 / vq7.replace(0, np.nan)}).dropna(subset=["r24"])
    rows = rows[rows["vq24"] > 0]

    def pick(d: pd.DataFrame) -> list[dict[str, Any]]:
        return [{"inst_id": k, "r24": _f(r.r24), "vq24": _f(r.vq24), "surge": _f(r.surge)} for k, r in d.iterrows()]
    out = {
        "from": int(start), "to": int(C.index[-1]) + HOUR, "days": days, "hourly": hourly,
        "now": {"btc": coin(btc), "eth": coin(eth), "btc_state": btc_state,
                "alt_r24": _f(r24[alts_now].median()) if alts_now else None, "alt_r7d": _f(r7d[alts_now].median()) if alts_now else None,
                "n_alts": len(alts_now), "breadth24": _f((up24 > 0).mean()) if len(up24) else None, "n_coins": int(len(up24)),
                "streak": streak},
        "dist": [_f(v) for v in up24.to_numpy()],
        "movers": {"up": pick(rows.nlargest(10, "r24")), "down": pick(rows.nsmallest(10, "r24")),
                   "surge": pick(rows[rows["vq24"] >= MIN_VQ_SURGE].nlargest(10, "surge"))},
    }
    return out, alt_24


# ---------------------------------------------------------------- derivatives
def derivs(cfg: Config, now: int) -> dict[str, Any]:
    from okxrev import heat
    out: dict[str, Any] = {}
    p = cfg.paths.results / "heat_snapshot.json"
    try:
        snap = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        snap = {}
    rows = snap.get("rows") or []
    if rows:
        fr = [_f(r.get("funding")) for r in rows]
        fr = [x for x in fr if x is not None]
        thr = float(heat.hcfg(cfg)["alert_threshold"])
        out.update({"snap_ts": snap.get("ts"), "n": len(rows), "funding_med": _med(fr), "funding_neg": sum(x < 0 for x in fr),
                    "funding_hi": sum(x >= 0.001 for x in fr), "funding_lo": sum(x <= -0.001 for x in fr), "n_funding": len(fr),
                    "oi_chg_med": _med([r.get("oi_chg_24h") for r in rows]), "taker_med": _med([r.get("taker_ratio_6h") for r in rows]),
                    "ls_med": _med([r.get("ls_acct") for r in rows]), "premium_med": _med([r.get("premium") for r in rows]),
                    "hot": sum((_f(r.get("score")) or 0) >= thr for r in rows), "threshold": thr})
    try:
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=1.0, max_tries=5) as con:
            liq = con.execute("""
                SELECT l.pos_side, SUM(l.px * l.sz * COALESCE(i.ct_val, 1)), COUNT(*), COUNT(DISTINCT l.inst_id)
                FROM liquidations l LEFT JOIN instruments i USING (inst_id) WHERE l.ts >= ? GROUP BY 1""", [now - D]).fetchall()
        out["liq"] = {str(s): {"usd": _f(u), "n": int(n), "coins": int(c)} for s, u, n, c in liq}
    except Exception:
        out["liq"] = {}
    return out


# ---------------------------------------------------------------- paper traders
def traders(cfg: Config, grid: np.ndarray, now: int, alt_24: pd.Series) -> dict[str, Any]:
    """`grid` = the hours of the market chart (so the three charts share one time axis)."""
    from okxrev import paper
    start = int(grid[0])
    try:
        con = paper.connect(cfg, read_only=True)
    except FileNotFoundError:
        return {}
    try:
        tr = con.execute("SELECT trader_id, kind, label, active FROM traders").df()
        pos = con.execute("""
            SELECT trader_id, inst_id, side, state, entry_ts, exit_ts, ret_net FROM positions
            WHERE book = 'C' AND entry_ts IS NOT NULL AND entry_ts < ? AND (exit_ts IS NULL OR exit_ts >= ?)""", [now, start]).df()
    finally:
        con.close()
    kind = dict(zip(tr["trader_id"], tr["kind"]))
    pos["kind"] = pos["trader_id"].map(kind).fillna("?")
    e = pos["entry_ts"].to_numpy(np.int64)
    x = pos["exit_ts"].fillna(now + 2 * HOUR).to_numpy(np.int64)

    def count(mask: np.ndarray) -> list[int]:
        # positions open at the end of each hour
        hend = grid + HOUR
        a = np.sort(e[mask])
        b = np.sort(x[mask])
        return (np.searchsorted(a, hend, side="right") - np.searchsorted(b, hend, side="right")).tolist()
    long_, short_ = pos["side"].eq("long").to_numpy(), pos["side"].eq("short").to_numpy()
    kinds = {}
    for k in sorted(pos["kind"].unique()):
        m = pos["kind"].eq(k).to_numpy()
        kinds[k] = {"long": count(m & long_), "short": count(m & short_)}

    a24 = alt_24.dropna()
    eh = (pos["entry_ts"] // HOUR) * HOUR - HOUR      # the last finished hour at entry
    pos["mkt"] = eh.map(a24) if len(a24) else np.nan
    opened = pos["entry_ts"] >= start
    closed = pos["state"].eq("closed") & pos["exit_ts"].ge(start) & pos["ret_net"].notna()
    is_open = pos["state"].eq("open")
    rows = []
    groups = dict(list(pos.groupby("trader_id")))
    ids = list(dict.fromkeys([*tr.loc[tr["active"].fillna(False).astype(bool), "trader_id"], *groups]))
    for tid in ids:
        g = groups.get(tid, pos.iloc[:0])
        o, c, op = g[opened.loc[g.index]], g[closed.loc[g.index]], g[is_open.loc[g.index]]
        cu, cd = c[c["mkt"] > 0], c[c["mkt"] <= 0]
        rows.append({"trader_id": tid, "kind": kind.get(tid, "?"),
                     "open_long": int(o["side"].eq("long").sum()), "open_short": int(o["side"].eq("short").sum()),
                     "now_long": int(op["side"].eq("long").sum()), "now_short": int(op["side"].eq("short").sum()),
                     "n": int(len(c)), "mean": _f(c["ret_net"].mean()) if len(c) else None,
                     "win": _f((c["ret_net"] > 0).mean()) if len(c) else None, "sum": _f(c["ret_net"].sum()) if len(c) else None,
                     "n_up": int(len(cu)), "mean_up": _f(cu["ret_net"].mean()) if len(cu) else None,
                     "n_dn": int(len(cd)), "mean_dn": _f(cd["ret_net"].mean()) if len(cd) else None,
                     "coins": int(o["inst_id"].nunique())})
    labels = dict(zip(tr["trader_id"], tr["label"]))
    for r in rows:
        r["label"] = labels.get(r["trader_id"]) or ""
    return {"t": (grid // 1000).tolist(), "kinds": kinds, "rows": rows}


def trader_positions(cfg: Config, trader: str, days: int, now: int | None = None) -> list[dict[str, Any]]:
    from okxrev import paper
    now = now or db.now_ms()
    start = (now // HOUR) * HOUR - days * D
    con = paper.connect(cfg, read_only=True)
    try:
        df = con.execute("""
            SELECT p.inst_id, p.side, p.state, p.entry_ts, p.entry_px, p.exit_ts, p.exit_px, p.exit_reason, p.ret_net, p.mae, p.mfe, d.reason
            FROM positions p LEFT JOIN decisions d USING (decision_id)
            WHERE p.book = 'C' AND p.trader_id = ? AND p.entry_ts IS NOT NULL AND p.entry_ts < ? AND (p.exit_ts IS NULL OR p.exit_ts >= ?)
            ORDER BY p.entry_ts DESC LIMIT 2000""", [trader, now, start]).df()
    finally:
        con.close()
    return json.loads(df.to_json(orient="records"))


# ---------------------------------------------------------------- research context
def research_note(cfg: Config, streak: dict[str, Any]) -> list[dict[str, Any]]:
    """What followed the same streak length in the regime study (continuation table), for both periods."""
    res = regime.load(cfg)
    if not res or not streak:
        return []
    side, k = ("down", streak["down_days"]) if streak.get("down_days") else ("up", streak.get("up_days") or 0)
    if not k:
        return []
    out = []
    for name, label in (("main", "目前資料庫"), ("old", "舊資料")):
        tab = ((res.get("periods") or {}).get(name) or {}).get("continuation", {}).get(side) or []
        hit = next((r for r in tab if r["k"] == k), None) or (tab[-1] if tab and tab[-1]["k"] < k else None)
        if hit:
            out.append({"period": label, "range": res["periods"][name].get("range"), "side": side, **hit})
    return out


# ---------------------------------------------------------------- entry point
def overview(cfg: Config, days: int = 30) -> dict[str, Any]:
    days = int(max(1, min(days, 60)))
    with _lock:
        hit = _cache.get(days)
        if hit and time.monotonic() - hit[0] < CACHE_SEC:
            return hit[1]
    now = db.now_ms()
    try:
        mk, alt_24 = market(cfg, days, now)
    except db.DbBusy:
        if hit:                     # the sync holds the database: show the last result, marked as old
            return {**hit[1], "stale": True}
        raise
    if not mk:
        return {"empty": True}
    mk["derivs"] = derivs(cfg, now)
    mk["traders"] = traders(cfg, np.asarray(mk["hourly"]["t"], dtype=np.int64) * 1000, now, alt_24)
    mk["research"] = research_note(cfg, mk["now"]["streak"])
    mk["ts"] = now
    with _lock:
        _cache[days] = (time.monotonic(), mk)
    return mk
