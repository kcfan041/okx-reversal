"""Fine-grained market data: 1s / 1m candles and recent trades, fetched on demand from OKX and cached.

Candles: the newest part from /market/candles (up to 300 rows), older parts paged from /market/history-candles
(100 rows per page, `after` = exclusive upper bound). 1m candles are cached in data/fine.duckdb once confirmed;
1s candles and trades are kept only in a short in-memory cache. Range caps: 1s <= 2h, 1m <= 3 days (otherwise the
caller is told to use a coarser bar).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, ms_to_str
from okxrev.okx_client import OkxClient, OkxError

log = logging.getLogger("okxrev.fine")

BAR_MS = {"1s": 1_000, "1m": 60_000, "5m": 300_000}
MAX_SPAN = {"1s": 2 * 3_600_000, "1m": 3 * 86_400_000, "5m": 30 * 86_400_000}
COLS = ["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles_1m (
    inst_id VARCHAR NOT NULL, ts BIGINT NOT NULL, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    vol DOUBLE, vol_ccy DOUBLE, vol_quote DOUBLE, PRIMARY KEY (inst_id, ts)
);
CREATE TABLE IF NOT EXISTS fetched_1m (
    inst_id VARCHAR NOT NULL, lo BIGINT NOT NULL, hi BIGINT NOT NULL, fetched BIGINT
);
"""

_mem: dict[tuple[Any, ...], tuple[float, Any]] = {}
_mem_lock = threading.Lock()
_db_lock = threading.Lock()
MEM_TTL = 2.0


class RangeError(ValueError):
    pass


def fine_db(cfg: Config) -> Path:
    return cfg.paths.root / "data" / "fine.duckdb"


def _parse(rows: list[list[str]], confirmed_only: bool) -> pd.DataFrame:
    recs = []
    for r in rows:
        if confirmed_only and len(r) > 8 and r[8] != "1":
            continue
        recs.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                     float(r[5] or 0), float(r[6] or 0), float(r[7] or 0) if len(r) > 7 else np.nan))
    return pd.DataFrame(recs, columns=COLS)


def _fetch_range(client: OkxClient, inst: str, bar: str, start: int, end: int, include_live: bool) -> pd.DataFrame:
    """Page newest -> oldest until `start` is covered. `end` is exclusive."""
    ms = BAR_MS[bar]
    frames = []
    now = db.now_ms()
    after = end
    if end > now - 290 * ms:
        rows = client.get("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": "300"})
        df = _parse(rows, confirmed_only=not include_live)
        if len(df):
            frames.append(df)
            after = min(after, int(df["ts"].min()))
    guard = 0
    while after > start and guard < 400:
        guard += 1
        rows = client.get("/api/v5/market/history-candles", {"instId": inst, "bar": bar, "limit": "100", "after": str(after)})
        if not rows:
            break
        df = _parse(rows, confirmed_only=True)
        if df.empty:
            break
        frames.append(df)
        lo = int(df["ts"].min())
        if lo >= after:
            break
        after = lo
    if not frames:
        return pd.DataFrame(columns=COLS)
    out = pd.concat(frames, ignore_index=True).drop_duplicates("ts", keep="first").sort_values("ts")
    return out[(out["ts"] >= start) & (out["ts"] < end)].reset_index(drop=True)


def _covered_until(con: duckdb.DuckDBPyConnection, inst: str, start: int, end: int) -> int:
    """End of the contiguous fetched span starting at `start` (== start when nothing is cached there)."""
    r = con.execute("SELECT lo, hi FROM fetched_1m WHERE inst_id = ? AND hi > ? AND lo < ? ORDER BY lo", [inst, start, end]).fetchall()
    cur = start
    for lo, hi in r:
        if lo > cur:
            break
        cur = max(cur, hi)
        if cur >= end:
            break
    return cur


def _covered(con: duckdb.DuckDBPyConnection, inst: str, start: int, end: int) -> bool:
    return _covered_until(con, inst, start, end) >= end


def _compact(con: duckdb.DuckDBPyConnection, inst: str) -> None:
    r = con.execute("SELECT lo, hi FROM fetched_1m WHERE inst_id = ? ORDER BY lo", [inst]).fetchall()
    if len(r) < 20:
        return
    merged: list[list[int]] = []
    for lo, hi in r:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    con.execute("DELETE FROM fetched_1m WHERE inst_id = ?", [inst])
    now = db.now_ms()
    for lo, hi in merged:
        con.execute("INSERT INTO fetched_1m VALUES (?, ?, ?, ?)", [inst, lo, hi, now])


def _open_cache(cfg: Config) -> duckdb.DuckDBPyConnection | None:
    p = fine_db(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        try:
            con = duckdb.connect(str(p), config={"threads": 1})
            con.execute(SCHEMA)
            return con
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            if not db._is_lock_error(e):
                raise
            time.sleep(0.3)
    return None


def candles(cfg: Config, inst: str, bar: str, start: int, end: int, client: OkxClient | None = None,
            include_live: bool = True) -> pd.DataFrame:
    """1s / 1m / 5m candles for [start, end). 1m confirmed bars are cached on disk; the live (unconfirmed) bar is
    appended when include_live and the range reaches now."""
    if bar not in BAR_MS:
        raise RangeError(f"不支援的 K 線：{bar}")
    if end <= start:
        raise RangeError("時間範圍錯誤")
    if end - start > MAX_SPAN[bar]:
        raise RangeError(f"{bar} 最長只能查 {MAX_SPAN[bar] // 3_600_000} 小時，請縮短範圍或改用較粗的 K 線")
    ms = BAR_MS[bar]
    start = (start // ms) * ms
    key = (inst, bar, start // 1000, end // 1000)
    with _mem_lock:
        hit = _mem.get(key)
        if hit and time.monotonic() - hit[0] < MEM_TTL:
            return hit[1].copy()
    own = client is None
    client = client or OkxClient(cfg.okx)
    try:
        now = db.now_ms()
        if bar == "1m":
            df = _cached_1m(cfg, client, inst, start, end, now)
            if include_live and end > now - 2 * ms:
                live = _parse(client.get("/api/v5/market/candles", {"instId": inst, "bar": "1m", "limit": "3"}), confirmed_only=False)
                live = live[(live["ts"] >= start) & (live["ts"] < end)]
                df = pd.concat([df, live], ignore_index=True).drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
        elif bar == "1s" and include_live and end >= now - 5_000:
            df = _live_1s(client, inst, start, end, now)
        else:
            df = _fetch_range(client, inst, bar, start, end, include_live)
    finally:
        if own:
            client.close()
    with _mem_lock:
        _mem[key] = (time.monotonic(), df)
        if len(_mem) > 200:
            for k in sorted(_mem, key=lambda k: _mem[k][0])[:100]:
                _mem.pop(k, None)
    return df.copy()


_live: dict[str, pd.DataFrame] = {}


def _live_1s(client: OkxClient, inst: str, start: int, end: int, now: int) -> pd.DataFrame:
    """Live 1s window: keep a per-instrument rolling frame; when it already covers `start` and is recent, only the
    newest 300 bars are fetched and merged (one request per refresh instead of re-paging the whole window)."""
    with _mem_lock:
        prev = _live.get(inst)
    if prev is not None and len(prev) and int(prev["ts"].min()) <= start and int(prev["ts"].max()) >= now - 250_000:
        new = _parse(client.get("/api/v5/market/candles", {"instId": inst, "bar": "1s", "limit": "300"}), confirmed_only=False)
        df = pd.concat([prev, new], ignore_index=True).drop_duplicates("ts", keep="last").sort_values("ts")
    else:
        df = _fetch_range(client, inst, "1s", start, end, include_live=True)
    df = df[df["ts"] >= now - MAX_SPAN["1s"]].reset_index(drop=True)
    with _mem_lock:
        _live[inst] = df
        if len(_live) > 20:
            _live.pop(next(iter(_live)))
    return df[(df["ts"] >= start) & (df["ts"] < end)].reset_index(drop=True)


def _cached_1m(cfg: Config, client: OkxClient, inst: str, start: int, end: int, now: int) -> pd.DataFrame:
    """Confirmed 1m bars from fine.duckdb; fetch and store missing spans (bars older than 2 minutes only)."""
    stable_end = min(end, ((now // 60_000) - 1) * 60_000)
    with _db_lock:
        con = _open_cache(cfg)
        if con is None:
            log.info("fine cache busy; fetching %s 1m directly", inst)
            return _fetch_range(client, inst, "1m", start, end, include_live=False)
        try:
            if stable_end > start:
                cu = _covered_until(con, inst, start, stable_end)
                if cu < stable_end:
                    df = _fetch_range(client, inst, "1m", cu, stable_end, include_live=False)
                    if len(df):
                        d = df.assign(inst_id=inst)[["inst_id", *COLS]]
                        con.register("_d", d)
                        con.execute("INSERT OR REPLACE INTO candles_1m SELECT * FROM _d")
                        con.unregister("_d")
                    con.execute("INSERT INTO fetched_1m VALUES (?, ?, ?, ?)", [inst, cu, stable_end, now])
                    _compact(con, inst)
            return con.execute(f"SELECT {', '.join(COLS)} FROM candles_1m WHERE inst_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                               [inst, start, end]).df()
        finally:
            con.close()


def trades(cfg: Config, inst: str, client: OkxClient | None = None, limit: int = 500) -> pd.DataFrame:
    key = (inst, "trades")
    with _mem_lock:
        hit = _mem.get(key)
        if hit and time.monotonic() - hit[0] < MEM_TTL:
            return hit[1].copy()
    own = client is None
    client = client or OkxClient(cfg.okx)
    try:
        rows = client.get("/api/v5/market/trades", {"instId": inst, "limit": str(min(int(limit), 500))})
    finally:
        if own:
            client.close()
    df = pd.DataFrame([{"ts": int(r["ts"]), "px": float(r["px"]), "sz": float(r["sz"]), "side": r["side"]} for r in rows])
    if len(df):
        df = df.sort_values("ts").reset_index(drop=True)
    with _mem_lock:
        _mem[key] = (time.monotonic(), df)
    return df.copy()


# ---------------------------------------------------------------- micro-structure metrics
def micro(df: pd.DataFrame, bar: str, now_ts: int | None = None) -> dict[str, Any]:
    """Metrics over the last 1 / 5 / 15 minutes of a fine candle frame (+ whole-range stats)."""
    if df.empty:
        return {}
    ms = BAR_MS[bar]
    ts = df["ts"].to_numpy(np.int64)
    end = int(ts[-1]) + ms if now_ts is None else now_ts
    c = df["close"].to_numpy(float)
    vq = df["vol_quote"].to_numpy(float) if df["vol_quote"].notna().any() else df["vol"].to_numpy(float)
    typ = (df["high"] + df["low"] + df["close"]).to_numpy(float) / 3
    out: dict[str, Any] = {"bar": bar, "n": int(len(df)), "from": int(ts[0]), "to": int(ts[-1]) + ms}
    tot = np.nansum(vq)
    vwap = float(np.nansum(typ * vq) / tot) if tot > 0 else float(np.nanmean(typ))
    out["vwap"] = vwap
    out["last"] = float(c[-1])
    out["dist_vwap"] = float(c[-1] / vwap - 1) if vwap else None
    out["range_high"] = float(df["high"].max())
    out["range_low"] = float(df["low"].min())
    out["range_high_at"] = int(ts[int(df["high"].to_numpy().argmax())])
    out["range_low_at"] = int(ts[int(df["low"].to_numpy().argmin())])
    lr = np.diff(np.log(c))
    per_year = 365 * 86_400_000 / ms
    out["rv_annual"] = float(np.nanstd(lr) * np.sqrt(per_year)) if len(lr) > 5 else None
    base_rate = np.nanmean(vq) / ms if len(vq) else np.nan
    windows = {}
    for label, mins in (("1m", 1), ("5m", 5), ("15m", 15)):
        w = ts >= end - mins * 60_000
        if not w.any():
            continue
        sub = df[w]
        o, h, lo, cl = float(sub["open"].iloc[0]), float(sub["high"].max()), float(sub["low"].min()), float(sub["close"].iloc[-1])
        rng = h - lo
        body_top, body_bot = max(o, cl), min(o, cl)
        vsub = vq[w]
        rate = np.nansum(vsub) / (mins * 60_000)
        rs = np.sign(np.diff(sub["close"].to_numpy(float)))
        streak = 0
        if len(rs):
            last = rs[-1]
            for x in rs[::-1]:
                if x == last and x != 0:
                    streak += 1
                else:
                    break
            streak = int(streak * (1 if last > 0 else -1))
        windows[label] = {"change": cl / o - 1 if o else None, "high": h, "low": lo,
                          "upper_wick": (h - body_top) / rng if rng > 0 else 0.0, "lower_wick": (body_bot - lo) / rng if rng > 0 else 0.0,
                          "range_pct": rng / o if o else None, "vol_ratio": float(rate / base_rate) if base_rate and base_rate > 0 else None,
                          "turnover": float(np.nansum(vsub)), "streak": streak}
    out["windows"] = windows
    big = int(np.nanargmax(vq)) if len(vq) else 0
    out["max_vol_bar"] = {"ts": int(ts[big]), "turnover": float(vq[big]), "change": float(c[big] / df["open"].iloc[big] - 1)}
    rets = c[1:] / c[:-1] - 1 if len(c) > 1 else np.array([])
    if len(rets):
        i_up, i_dn = int(np.argmax(rets)), int(np.argmin(rets))
        out["biggest_up_bar"] = {"ts": int(ts[i_up + 1]), "change": float(rets[i_up])}
        out["biggest_down_bar"] = {"ts": int(ts[i_dn + 1]), "change": float(rets[i_dn])}
    return out


def trade_flow(tr: pd.DataFrame, big_mult: float = 10.0) -> dict[str, Any]:
    if tr.empty:
        return {}
    usd = tr["px"] * tr["sz"]
    buy = tr["side"] == "buy"
    span = max(1, (int(tr["ts"].iloc[-1]) - int(tr["ts"].iloc[0])) / 1000)
    thr = float(usd.mean() * big_mult)
    bigs = tr[usd >= thr]
    return {"n": int(len(tr)), "from": int(tr["ts"].iloc[0]), "to": int(tr["ts"].iloc[-1]), "seconds": float(span),
            "trades_per_sec": float(len(tr) / span), "buy_share_sz": float(tr.loc[buy, "sz"].sum() / tr["sz"].sum()) if tr["sz"].sum() else None,
            "buy_share_notional": float(usd[buy].sum() / usd.sum()) if usd.sum() else None, "vwap": float((tr["px"] * tr["sz"]).sum() / tr["sz"].sum()),
            "big_threshold_notional": thr, "big_n": int(len(bigs)), "big_buy": int((bigs["side"] == "buy").sum()),
            "big_sell": int((bigs["side"] == "sell").sum()),
            "big_latest": [[int(r.ts), r.side, float(r.px), float(r.sz)] for r in bigs.tail(20).itertuples()]}


def summary_line(m: dict[str, Any], flow: dict[str, Any] | None = None) -> str:
    """One-sentence rule-based description of the last 5 minutes."""
    w = (m.get("windows") or {}).get("5m") or (m.get("windows") or {}).get("1m")
    if not w:
        return "資料不足"
    parts = []
    ch = w.get("change") or 0
    parts.append(f"5 分鐘 {ch:+.1%}")
    vr = w.get("vol_ratio")
    if vr is not None:
        parts.append(f"量比 {vr:.1f} 倍")
    if flow and flow.get("buy_share_notional") is not None:
        parts.append(f"主動買 {flow['buy_share_notional']:.0%}")
    dv = m.get("dist_vwap")
    if dv is not None:
        parts.append(f"{'高於' if dv >= 0 else '低於'} VWAP {abs(dv):.1%}")
    tag = []
    if vr and vr >= 3 and ch <= -0.02:
        tag.append("帶量下殺")
    elif vr and vr >= 3 and ch >= 0.02:
        tag.append("帶量急拉")
    if w.get("upper_wick", 0) >= 0.5 and (w.get("range_pct") or 0) >= 0.01:
        tag.append("長上影（上方賣壓）")
    if w.get("lower_wick", 0) >= 0.5 and (w.get("range_pct") or 0) >= 0.01:
        tag.append("長下影（下方承接）")
    if flow and flow.get("buy_share_notional") is not None:
        if flow["buy_share_notional"] <= 0.35:
            tag.append("賣壓集中")
        elif flow["buy_share_notional"] >= 0.65:
            tag.append("買盤集中")
    if vr is not None and vr < 0.5 and abs(ch) < 0.005:
        tag.append("量縮盤整")
    return "、".join(parts) + ("：" + "、".join(tag) if tag else "")


def fmt_t(ms: int | None) -> str | None:
    return ms_to_str(ms, "%Y-%m-%d %H:%M:%S") if ms is not None else None
