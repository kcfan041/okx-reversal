"""Pump-fade short: a coin that has been falling for a month pumps hard within a day, and once it gives back part of
the pump we short it for a quick target, with a wide stop and small size.

Signal at the close of 5-minute bar i (only data <= i is used):
  - 24h window = the last 288 bars up to i: peak = highest high, base = lowest low at or before the peak
  - pump = peak / base - 1 >= PUMP_MIN, and the peak is before bar i
  - the coin was falling: close just before the base vs 30 days earlier <= PRE_MAX (None = no requirement)
  - retrace: close_i <= peak - RETRACE * (peak - base), and no close since the peak was already at or below that level
  - at least COOLDOWN bars after the previous signal on the same coin
Entry at the next bar's open (engine), exit by the exit rule (take-profit / stop / max hold).
"""
from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

from okxrev.paper import FIXED_STOP, FIXED_TP, Signal, Trader

BAR = 300_000
HOUR = 3_600_000
DAY = 86_400_000
WIN = 288
PRE_MS = 30 * DAY
COOLDOWN = 864
PUMP_MIN = 0.5
RETRACE = 0.25
PRE_MAX = -0.2

FADE_SETS: list[dict[str, Any]] = [
    {"id": "fade_pump", "exit": "7d_tp12_sl50", "pre_max": PRE_MAX,
     "label": "急漲回落做空（長期下跌後 24h 漲 ≥50%，回吐 25% 進場；止盈 12%、止損 50%、最長 7 天）"},
    {"id": "fade_pump_sl30", "exit": "7d_tp12_sl30", "pre_max": PRE_MAX, "label": "急漲回落做空，止損 30%（對照）"},
    {"id": "fade_pump_anypre", "exit": "7d_tp12_sl50", "pre_max": None, "label": "急漲回落做空，不看前 30 天跌幅（對照）"},
]
MAIN_ID = "fade_pump"


def _asof(ts: np.ndarray, px: np.ndarray, t: int) -> float:
    k = int(np.searchsorted(ts, t, side="right")) - 1
    return float(px[k]) if k >= 0 and t - int(ts[k]) <= 2 * HOUR else float("nan")


def fade_signals(ts: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, pre_max: float | None,
                 old: tuple[np.ndarray, np.ndarray] | None = None, pump_min: float = PUMP_MIN, retrace: float = RETRACE,
                 cooldown: int = COOLDOWN, lo_ts: int | None = None) -> list[dict[str, Any]]:
    """Signal bars for one coin on a 5-minute grid. `old` = (ts, close) of older history (any resolution) used for the
    30-day-earlier price when it is not inside `ts`. Signals before `lo_ts` are only used for the cooldown."""
    n = len(c)
    out: list[dict[str, Any]] = []
    if n < WIN // 2 + 2:
        return out
    hs, ls = pd.Series(h), pd.Series(lo)
    hh = hs.rolling(WIN, min_periods=WIN // 2).max().to_numpy()
    ll = ls.rolling(WIN, min_periods=WIN // 2).min().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        maybe = (hh / ll - 1 >= pump_min) & np.isfinite(c) & (c <= hh - retrace * (hh - ll))
    cand = np.flatnonzero(maybe)
    if not len(cand):
        return out
    fin = np.isfinite(c)
    cts, cpx = ts[fin], c[fin]
    if old is not None and len(old[0]):
        m = old[0] < (cts[0] if len(cts) else np.iinfo(np.int64).max)
        cts, cpx = np.concatenate([old[0][m], cts]), np.concatenate([old[1][m], cpx])
    last = -10 ** 9
    for i in cand:
        w0 = max(0, i - WIN + 1)
        seg_h = h[w0:i + 1]
        if not np.isfinite(seg_h).any():
            continue
        pk = w0 + int(np.nanargmax(seg_h))
        if pk >= i:
            continue
        seg_l = lo[w0:pk + 1]
        if not np.isfinite(seg_l).any():
            continue
        b0 = w0 + int(np.nanargmin(seg_l))
        peak, base = float(h[pk]), float(lo[b0])
        if not base > 0 or peak / base - 1 < pump_min:
            continue
        level = peak - retrace * (peak - base)
        if not c[i] <= level:
            continue
        prev = c[pk + 1:i]
        if len(prev) and (prev[np.isfinite(prev)] <= level).any():
            continue
        if i - last < cooldown:
            continue
        tb = int(ts[b0]) - BAR
        now_px, then_px = _asof(cts, cpx, tb), _asof(cts, cpx, tb - PRE_MS)
        pre = now_px / then_px - 1 if then_px > 0 and np.isfinite(now_px) else float("nan")
        if pre_max is not None and not pre <= pre_max:
            continue
        last = i
        if lo_ts is not None and int(ts[i]) < lo_ts:
            continue
        out.append({"i": int(i), "ts": int(ts[i]), "peak": peak, "base": base, "pump": peak / base - 1,
                    "pre30": None if not np.isfinite(pre) else float(pre), "close": float(c[i]), "level": float(level)})
    return out


def load_old_closes(cfg: Any, since: int, until: int, path: Any = None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Hourly closes (one per hour) from the candle database, for the 30-day-earlier price."""
    from okxrev import db
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    try:
        with db.open_db(path or cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
            df = con.execute("SELECT inst_id, ts, close FROM candles WHERE ts >= ? AND ts < ? AND ts % 3600000 = 0 ORDER BY inst_id, ts",
                             [since, until]).df()
    except Exception:
        return out
    for inst, g in df.groupby("inst_id"):
        out[inst] = (g["ts"].to_numpy(np.int64), g["close"].to_numpy(float))
    return out


class FadeTrader(Trader):
    kind = "fade"
    old_source: Callable[[Any, int, int], dict[str, tuple[np.ndarray, np.ndarray]]] | None = None
    _shared: dict[str, Any] = {}

    def __init__(self, fs: dict[str, Any]):
        self.fs = fs
        self.trader_id = fs["id"]
        self.exit = fs["exit"]
        self.label = fs["label"]
        self.old: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def params(self) -> dict[str, Any]:
        return {"exit": self.exit, "pump_min": PUMP_MIN, "retrace": RETRACE, "pre_max": self.fs["pre_max"],
                "tp": FIXED_TP.get(self.exit), "sl": FIXED_STOP.get(self.exit), "cooldown_bars": COOLDOWN}

    def prepare(self, ctx: Any) -> None:
        if self.fs["pre_max"] is None:
            return
        first = None
        for g in ctx.candles.values():
            if len(g):
                t = int(g.index[0])
                first = t if first is None else min(first, t)
        if first is None:
            return
        first = first // DAY * DAY
        key = (first - PRE_MS - 2 * DAY, first)
        cache = FadeTrader._shared
        if cache.get("key") != key:
            src = type(self).old_source or (lambda cfg, a, b: load_old_closes(cfg, a, b))
            cache["key"], cache["old"] = key, src(ctx.cfg, *key)
        self.old = cache["old"]

    def signals(self, ctx: Any, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        g = ctx.candles.get(inst)
        if g is None or len(bars) == 0:
            return []
        bars = np.asarray(bars, dtype=np.int64)
        lo_ts = int(bars[0]) - (WIN + COOLDOWN + 12) * BAR
        old = self.old.get(inst)
        if self.fs["pre_max"] is not None:
            early = g[g.index < lo_ts]
            if len(early):
                early = early[early.index % HOUR == 0]
                ets, epx = early.index.to_numpy(np.int64), early["close"].to_numpy(float)
                if old is not None and len(old[0]):
                    m = old[0] < (ets[0] if len(ets) else lo_ts)
                    ets, epx = np.concatenate([old[0][m], ets]), np.concatenate([old[1][m], epx])
                old = (ets, epx)
        g = g[(g.index >= lo_ts) & (g.index <= int(bars[-1]))]
        if len(g) < WIN // 2 + 2:
            return []
        ts = g.index.to_numpy(np.int64)
        sigs = fade_signals(ts, g["high"].to_numpy(float), g["low"].to_numpy(float), g["close"].to_numpy(float),
                            self.fs["pre_max"], old, lo_ts=int(bars[0]))
        want = set(bars.tolist())
        out = []
        for e in sigs:
            if e["ts"] not in want:
                continue
            feats = {"pump": e["pump"], "pre30": e["pre30"], "peak": e["peak"], "base": e["base"], "signal_close": e["close"],
                     "_risk_frac": risk_frac(ctx.cfg)}
            pre = "" if e["pre30"] is None else f"，前 30 天 {e['pre30'] * 100:+.0f}%"
            why = f"24h 內由 {e['base']:.6g} 漲到 {e['peak']:.6g}（+{e['pump'] * 100:.0f}%）{pre}，收盤 {e['close']:.6g} 回吐 25%"
            out.append(Signal(self.trader_id, inst, e["ts"], "short", self.exit, float(e["pump"]), why, features=feats))
        return out


def risk_frac(cfg: Any) -> float:
    try:
        return float(((cfg.paper or {}).get("fade") or {}).get("risk", 0.02))
    except (TypeError, ValueError, AttributeError):
        return 0.02


def suggest(entry: float, exit_rule: str, equity: float, risk: float = 0.02) -> dict[str, Any]:
    """Short sizing: lose `risk` of equity if the stop is hit (isolated margin, 1x-equivalent notional)."""
    sl, tp = FIXED_STOP.get(exit_rule) or 0.5, FIXED_TP.get(exit_rule) or 0.12
    notional = equity * risk / sl if equity > 0 else 0.0
    return {"entry": entry, "tp_px": entry * (1 - tp), "sl_px": entry * (1 + sl), "notional": notional,
            "equity": equity, "risk": risk, "risk_usd": equity * risk, "tp": tp, "sl": sl}


_EQ: dict[str, Any] = {}


def account_equity(cfg: Any, max_age_sec: float = 600) -> tuple[float, str]:
    """(equity in USD, source). Trading-account totalEq from the read-only key, cached; else the configured amount."""
    import time
    fc = ((cfg.paper or {}).get("fade") or {})
    fallback = float(fc.get("equity_fallback", 1000.0))
    if _EQ and time.time() - _EQ["at"] < max_age_sec:
        return _EQ["eq"], _EQ["src"]
    eq, src = fallback, "設定值"
    try:
        from okxrev import account
        from okxrev.live import BALANCE_PATH
        client = account.make_client(cfg)
        try:
            rows = client.get_private(BALANCE_PATH, {})
        finally:
            client.close()
        v = float((rows[0] or {}).get("totalEq") or 0) if rows else 0.0
        if v > 0:
            eq, src = v, "OKX 帳戶"
    except Exception:
        pass
    _EQ.update(eq=eq, src=src, at=time.time())
    return eq, src


def alert_detail(inst: str, entry: float, exit_rule: str, equity: float, src: str, risk: float, deadline_ms: int, why: str) -> str:
    from okxrev.config import ms_to_str
    s = suggest(entry, exit_rule, equity, risk)
    return (f"{why}。參考進場（做空）{entry:.6g}，止盈 {s['tp_px']:.6g}（-{s['tp'] * 100:.0f}%），止損 {s['sl_px']:.6g}（+{s['sl'] * 100:.0f}%），"
            f"最長持有到 {ms_to_str(deadline_ms)}。建議逐倉部位約 {s['notional']:,.0f} U（{src}權益 {equity:,.0f} U × 風險 {risk * 100:.0f}% ÷ 止損 {s['sl'] * 100:.0f}%，"
            f"打到止損約虧 {s['risk_usd']:,.0f} U）。紙上訊號，不會自動下單。")


def live_alerts(cfg: Any, con: Any, now: int) -> int:
    """Alerts for new main fade_pump positions (pending or just opened) not alerted yet."""
    fc = ((cfg.paper or {}).get("fade") or {})
    if not fc.get("alerts", True):
        return 0
    from okxrev import heat
    from okxrev.paper import EXIT_RULES
    con.execute(heat.ALERT_SCHEMA)
    rows = con.execute(
        "SELECT p.inst_id, p.signal_ts, d.exit_rule, d.reason, d.features, p.entry_px FROM positions p JOIN decisions d USING (decision_id) "
        "WHERE p.trader_id = ? AND p.book = 'C' AND p.signal_ts >= ?", [MAIN_ID, now - 2 * HOUR]).fetchall()
    n = 0
    for inst, sts, ex, why, feats, epx in rows:
        if con.execute("SELECT 1 FROM heat_alerts WHERE inst_id = ? AND kind = 'fade' AND ts >= ?", [inst, int(sts)]).fetchone():
            continue
        import json
        f = json.loads(feats or "{}")
        entry = float(epx) if epx else float(f.get("signal_close") or 0)
        if entry <= 0:
            continue
        eq, src = account_equity(cfg)
        hold = EXIT_RULES.get(ex, (2016, None))[0] or 2016
        det = alert_detail(inst, entry, ex, eq, src, risk_frac(cfg), int(sts) + BAR + (hold - 1) * BAR, why)
        con.execute("INSERT OR REPLACE INTO heat_alerts VALUES (?, ?, 'fade', ?, ?)", [now, inst, float(f.get("pump") or 0) * 100, det])
        n += 1
    return n


def traders(kinds: dict[str, bool]) -> list[FadeTrader]:
    return [FadeTrader(fs) for fs in FADE_SETS] if kinds.get("fade", True) else []
