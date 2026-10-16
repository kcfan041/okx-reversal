"""Forward paper trading: traders observe closed 5m bars, decide, and simulated orders fill at the next bar's open.

Nothing here talks to an exchange order endpoint. Every decision (taken or skipped) is logged with a feature
snapshot so it can later be used as training data.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.paper")

BAR = db.BAR_MS
DAY = 86_400_000
FUND_MS = 8 * 3_600_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    trader_id  VARCHAR PRIMARY KEY,
    kind       VARCHAR,
    label      VARCHAR,
    params     VARCHAR,
    created_at BIGINT,
    active     BOOLEAN
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id VARCHAR PRIMARY KEY,
    trader_id   VARCHAR,
    inst_id     VARCHAR,
    bar_ts      BIGINT,
    side        VARCHAR,
    action      VARCHAR,
    score       DOUBLE,
    reason      VARCHAR,
    exit_rule   VARCHAR,
    model_ver   VARCHAR,
    features    VARCHAR,
    created_at  BIGINT
);
CREATE TABLE IF NOT EXISTS positions (
    pos_id      VARCHAR PRIMARY KEY,
    decision_id VARCHAR,
    trader_id   VARCHAR,
    book        VARCHAR,
    inst_id     VARCHAR,
    side        VARCHAR,
    state       VARCHAR,
    signal_ts   BIGINT,
    entry_ts    BIGINT,
    entry_px    DOUBLE,
    notional    DOUBLE,
    lever       DOUBLE,
    stop_px     DOUBLE,
    liq_px      DOUBLE,
    hold_bars   INTEGER,
    deadline_ts BIGINT,
    checked_ts  BIGINT,
    funding     DOUBLE,
    exit_ts     BIGINT,
    exit_px     DOUBLE,
    exit_reason VARCHAR,
    fee         DOUBLE,
    ret_net     DOUBLE,
    pnl         DOUBLE,
    mae         DOUBLE,
    mfe         DOUBLE
);
CREATE TABLE IF NOT EXISTS books (
    trader_id VARCHAR,
    book      VARCHAR,
    equity    DOUBLE,
    start_eq  DOUBLE,
    updated   BIGINT,
    PRIMARY KEY (trader_id, book)
);
CREATE TABLE IF NOT EXISTS equity_hist (
    trader_id VARCHAR,
    book      VARCHAR,
    ts        BIGINT,
    equity    DOUBLE,
    PRIMARY KEY (trader_id, book, ts)
);
CREATE TABLE IF NOT EXISTS ticks (
    tick_ts     BIGINT PRIMARY KEY,
    last_bar    BIGINT,
    duration    DOUBLE,
    n_signals   INTEGER,
    n_opened    INTEGER,
    n_closed    INTEGER,
    note        VARCHAR
);
CREATE TABLE IF NOT EXISTS llm_calls (
    ts        BIGINT,
    trader_id VARCHAR,
    decision_id VARCHAR,
    model     VARCHAR,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost      DOUBLE,
    ok        BOOLEAN,
    verdict   VARCHAR,
    reason    VARCHAR
);
"""


# ---------------------------------------------------------------- signals / traders
@dataclass
class Signal:
    trader_id: str
    inst_id: str
    bar_ts: int
    side: str
    exit_rule: str
    score: float = 0.0
    reason: str = ""
    model_ver: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    action: str = "enter"

    @property
    def decision_id(self) -> str:
        return f"{self.trader_id}|{self.inst_id}|{self.bar_ts}|{self.side}"


EXIT_RULES = {
    "15m": (3, None), "1h": (12, None), "6h": (72, None), "24h": (288, None), "3d": (864, None),
    "6h_sl2atr": (72, 2.0), "24h_sl2atr": (288, 2.0), "you": (None, None),
    "24h_sl10pct": (288, None), "24h_sl20pct": (288, None),
}
FIXED_STOP = {"24h_sl10pct": 0.10, "24h_sl20pct": 0.20}
FALLBACK_STOP = 0.10
SL20_BASES = ("15m", "1h", "6h", "24h", "3d")
for _b in SL20_BASES:
    if f"{_b}_sl20pct" not in EXIT_RULES:
        EXIT_RULES[f"{_b}_sl20pct"] = (EXIT_RULES[_b][0], None)
    FIXED_STOP[f"{_b}_sl20pct"] = 0.20


def with_sl20(exit_rule: str) -> str | None:
    """The +20% fixed-stop variant of a plain time exit (None if the exit already has a stop or is unknown)."""
    return f"{exit_rule}_sl20pct" if exit_rule in SL20_BASES else None


class Trader:
    trader_id: str = ""
    kind: str = ""
    label: str = ""

    def params(self) -> dict[str, Any]:
        return {}

    def prepare(self, ctx: "Context") -> None:
        pass

    def signals(self, ctx: "Context", inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        return []

    def signals_batch(self, ctx: "Context", items: list[tuple[str, pd.DataFrame, np.ndarray]]) -> list[Signal]:
        out: list[Signal] = []
        for inst, f, bars in items:
            out += self.signals(ctx, inst, f, bars)
        return out

    def review(self, ctx: "Context", sigs: list[Signal]) -> list[Signal]:
        return sigs


# ---------------------------------------------------------------- market context
FEATURE_SNAPSHOT = ["r1h", "r6h", "r24h", "r3d", "vol_ratio", "vmax_1h", "pos24", "pos7d", "dist_ema", "vol_rank",
                    "atr", "turnover24", "btc_regime", "btc_r1h", "btc_r4h", "breadth", "funding", "oi_chg", "age_d"]


@dataclass
class Context:
    cfg: Config
    now: int
    last_bar: int
    new_bars: dict[str, np.ndarray]
    feats: dict[str, pd.DataFrame]
    candles: dict[str, pd.DataFrame]
    funding: dict[str, pd.DataFrame]
    con: duckdb.DuckDBPyConnection
    computed: dict[str, list[Signal]] = field(default_factory=dict)
    _win: dict[tuple[str, int, int], tuple[np.ndarray, dict[str, np.ndarray]]] = field(default_factory=dict, repr=False)

    def window(self, inst: str, f: pd.DataFrame, lo: int) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """(index, column arrays) for f.iloc[lo:], computed once per tick and shared by every trader."""
        key = (inst, lo, len(f))
        w = self._win.get(key)
        if w is None:
            sub = f.iloc[lo:]
            w = (sub.index.to_numpy(np.int64), {k: sub[k].to_numpy() for k in sub.columns})
            self._win[key] = w
        return w

    def snapshot(self, inst: str, ts: int) -> dict[str, Any]:
        f = self.feats.get(inst)
        if f is None or ts not in f.index:
            return {}
        row = f.loc[ts]
        out = {}
        for k in FEATURE_SNAPSHOT:
            if k in row:
                v = row[k]
                out[k] = None if (isinstance(v, float) and not np.isfinite(v)) else (v.item() if hasattr(v, "item") else v)
        return out


def load_market(cfg: Config, since: int) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, Any]]:
    from okxrev.derivs import attach
    from okxrev.detector import btc_features, prepare
    from okxrev.discover import features, market_breadth_recent
    det, reg = cfg.detector, cfg.btc_regime
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        insts = db.list_candle_insts(con)
        meta = db.sync_overview(con).set_index("inst_id")
        df = con.execute("SELECT inst_id, ts, open, high, low, close, vol, vol_ccy, vol_quote FROM candles WHERE ts >= ? ORDER BY inst_id, ts",
                         [since]).df()
        try:
            fund_all = con.execute("SELECT inst_id, ts, rate FROM funding WHERE ts >= ?", [since - 2 * DAY]).df()
            oi_all = con.execute("SELECT inst_id, period, ts, oi_usd FROM open_interest WHERE ts >= ?", [since - 4 * DAY]).df()
        except duckdb.Error:
            fund_all = pd.DataFrame(columns=["inst_id", "ts", "rate"])
            oi_all = pd.DataFrame(columns=["inst_id", "period", "ts", "oi_usd"])
    btc_raw = df[df["inst_id"] == cfg.universe["btc_inst"]].drop(columns="inst_id")
    btc = btc_features(btc_raw, reg)
    breadth = market_breadth_recent(cfg, insts, since)
    candles, feats, fund = {}, {}, {}
    fg, og = dict(tuple(fund_all.groupby("inst_id"))), dict(tuple(oi_all.groupby("inst_id")))
    for inst, raw in df.groupby("inst_id"):
        raw = raw.drop(columns="inst_id").reset_index(drop=True)
        if len(raw) < 900:
            continue
        lt = meta.at[inst, "list_time"] if inst in meta.index else None
        lt = None if lt is None or pd.isna(lt) else int(lt)
        g = prepare(raw, det, btc, list_time=lt)
        fu = fg.get(inst, pd.DataFrame(columns=["ts", "rate"]))
        f = features(g, det, breadth, lt, attach(g, fu, og.get(inst, pd.DataFrame(columns=["period", "ts", "oi_usd"]))))
        f["breadth"] = pd.Series(f["breadth"].to_numpy(), index=g.index).ffill(limit=12).to_numpy()
        candles[inst] = g[["open", "high", "low", "close"]]
        feats[inst] = f
        fund[inst] = fu.sort_values("ts")
    return candles, feats, fund, {"n_insts": len(candles)}


# ---------------------------------------------------------------- books
@dataclass
class BookCfg:
    name: str
    label: str
    equity: float = 0.0
    size_frac: float | None = None
    notional: float | str | None = None
    lever: float = 1.0
    max_open: int | None = None
    unit: bool = False


def book_cfgs(cfg: Config, your_notional: float | None = None) -> list[BookCfg]:
    out = []
    for name, b in (cfg.paper.get("books") or {}).items():
        n = b.get("notional")
        if n == "auto":
            n = your_notional or 2000.0
        out.append(BookCfg(name, b.get("label", name), float(b.get("equity", 0)), b.get("size_frac"), n,
                           float(b.get("lever", 1)), b.get("max_open"), bool(b.get("unit", False))))
    return out


def your_median_notional(cfg: Config) -> float | None:
    try:
        from okxrev import account
        t = account.load_trades(cfg)
        x = t.loc[t["inst_type"] == "SWAP", "notional_usd"].dropna()
        return float(x.median()) if len(x) else None
    except Exception:
        return None


# ---------------------------------------------------------------- position walk (shared by live and replay)
def walk_position(side: str, epx: float, stop_px: float | None, liq_px: float | None, deadline: int, checked: int,
                  ts: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, fund: pd.DataFrame | None,
                  fund_acc: float, mae: float, mfe: float) -> tuple[int | None, float | None, str | None, int, float, float, float]:
    """Advance an open position over bars `ts` (all > checked, <= deadline). Liquidation is checked before the stop,
    both before the time exit; funding settles at each funding timestamp in (previous bar, bar]."""
    s = 1 if side == "long" else -1
    last_ts = checked
    fts = frate = None
    if fund is not None and len(fund):
        fts = fund["ts"].to_numpy(np.int64)
        frate = fund["rate"].to_numpy(float)
    for i in range(len(ts)):
        t = int(ts[i])
        if not np.isfinite(c[i]):
            last_ts = t
            continue
        if fts is not None:
            k0, k1 = np.searchsorted(fts, last_ts, side="right"), np.searchsorted(fts, t, side="right")
            if k1 > k0:
                r = frate[k0:k1]
                fund_acc += float(-s * r[np.isfinite(r)].sum())
        adv_px = lo[i] if s > 0 else h[i]
        fav_px = h[i] if s > 0 else lo[i]
        mae = max(mae, s * (epx - adv_px) / epx)
        mfe = max(mfe, s * (fav_px - epx) / epx)
        for px, why in ((liq_px, "liquidation"), (stop_px, "stop")):
            if px is not None and ((s > 0 and lo[i] <= px) or (s < 0 and h[i] >= px)):
                return t, float(px), why, t, fund_acc, mae, mfe
        last_ts = t
        if t >= deadline:
            return t, float(c[i]), "time", t, fund_acc, mae, mfe
    return None, None, None, last_ts, fund_acc, mae, mfe


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, cfg: Config, traders: list[Trader], con: duckdb.DuckDBPyConnection):
        self.cfg = cfg
        self.traders = traders
        self.con = con
        con.execute(SCHEMA)
        self.fee = float(cfg.metrics["fee_per_side"])
        self.slip = float(cfg.metrics["slippage_per_side"])
        fixed = cfg.paper.get("resolved_notional")
        self.books = book_cfgs(cfg, float(fixed) if fixed else your_median_notional(cfg))
        now = db.now_ms()
        for t in traders:
            con.execute("INSERT INTO traders VALUES (?, ?, ?, ?, ?, TRUE) ON CONFLICT (trader_id) DO UPDATE SET label = excluded.label, "
                        "params = excluded.params, active = TRUE", [t.trader_id, t.kind, t.label, json.dumps(t.params(), ensure_ascii=False), now])
            for b in self.books:
                if not b.unit:
                    con.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", [t.trader_id, b.name, b.equity, b.equity, now])

    # -- helpers
    def _cost(self, inst: str, feats: dict[str, pd.DataFrame]) -> float:
        f = feats.get(inst)
        small = f is not None and float(np.nanmedian(f["turnover24"].to_numpy())) < 5e6
        return self.fee + self.slip + (0.001 if small else 0.0)

    def open_count(self, trader: str, book: str) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM positions WHERE trader_id = ? AND book = ? AND state IN ('pending', 'open')",
                                    [trader, book]).fetchone()[0])

    def has_open(self, trader: str, book: str, inst: str) -> bool:
        return bool(self.con.execute("SELECT COUNT(*) FROM positions WHERE trader_id = ? AND book = ? AND inst_id = ? AND state IN ('pending', 'open')",
                                     [trader, book, inst]).fetchone()[0])

    def equity(self, trader: str, book: str) -> float:
        r = self.con.execute("SELECT equity FROM books WHERE trader_id = ? AND book = ?", [trader, book]).fetchone()
        return float(r[0]) if r else 0.0

    # -- decisions
    def record(self, s: Signal) -> bool:
        exists = self.con.execute("SELECT 1 FROM decisions WHERE decision_id = ?", [s.decision_id]).fetchone()
        if exists:
            return False
        self.con.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [s.decision_id, s.trader_id, s.inst_id, s.bar_ts, s.side, s.action, s.score, s.reason[:2000], s.exit_rule,
                          s.model_ver, json.dumps(s.features, ensure_ascii=False, default=float), db.now_ms()])
        return True

    def resolve_stop(self, s: Signal, feats: dict[str, pd.DataFrame]) -> None:
        """Stop distance (fraction of entry) for the signal, stored in its features before it is recorded:
        fixed-% exits use their %, ATR exits use mult x ATR (from the signal, else from the bar's features), and fall
        back to FALLBACK_STOP when ATR is unavailable (marked _stop_fallback)."""
        if "_stop_frac" in s.features:
            return
        _, stop_mult = EXIT_RULES.get(s.exit_rule, (72, None))
        frac = FIXED_STOP.get(s.exit_rule)
        if frac is None and stop_mult:
            atr = s.features.get("atr")
            if atr is None or not np.isfinite(float(atr)):
                f = feats.get(s.inst_id)
                if f is not None and "atr" in f and s.bar_ts in f.index:
                    v = f.at[s.bar_ts, "atr"]
                    atr = float(v) if v is not None and np.isfinite(v) else None
                else:
                    atr = None
            if atr is not None and atr > 0:
                frac = float(stop_mult * atr)
            else:
                frac = FALLBACK_STOP
                s.features["_stop_fallback"] = True
        if frac is not None:
            s.features["_stop_frac"] = float(frac)

    def queue(self, s: Signal, feats: dict[str, pd.DataFrame]) -> int:
        n = 0
        hold, stop_mult = EXIT_RULES.get(s.exit_rule, (72, None))
        hold = int(s.features.get("_hold_bars") or hold or 72)
        self.resolve_stop(s, feats)
        for b in self.books:
            if self.has_open(s.trader_id, b.name, s.inst_id):
                continue
            if not b.unit:
                if b.max_open is not None and self.open_count(s.trader_id, b.name) >= b.max_open:
                    continue
                eq = self.equity(s.trader_id, b.name)
                margin_used = float(self.con.execute(
                    "SELECT COALESCE(SUM(notional / lever), 0) FROM positions WHERE trader_id = ? AND book = ? AND state IN ('pending', 'open')",
                    [s.trader_id, b.name]).fetchone()[0])
                free = eq - margin_used
                if free <= 0:
                    continue
                notional = (eq * float(b.size_frac) * b.lever) if b.size_frac else float(b.notional or 0)
                notional = min(notional, free * b.lever)
                if notional <= 0:
                    continue
            else:
                notional = 1.0
            pid = f"{s.decision_id}|{b.name}"
            self.con.execute(
                "INSERT INTO positions (pos_id, decision_id, trader_id, book, inst_id, side, state, signal_ts, notional, lever, hold_bars, funding, fee, mae, mfe) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 0, 0, 0, 0) ON CONFLICT DO NOTHING",
                [pid, s.decision_id, s.trader_id, b.name, s.inst_id, s.side, s.bar_ts, notional, b.lever if not b.unit else 1.0, hold])
            self.con.execute("UPDATE positions SET stop_px = ? WHERE pos_id = ?", [s.features.get("_stop_frac"), pid])
            n += 1
        return n

    # -- fills and exits
    def fill_pending(self, candles: dict[str, pd.DataFrame], feats: dict[str, pd.DataFrame]) -> int:
        rows = self.con.execute("SELECT pos_id, inst_id, side, signal_ts, lever, stop_px, hold_bars FROM positions WHERE state = 'pending'").fetchall()
        n = 0
        for pid, inst, side, sts, lever, stop_frac, hold in rows:
            g = candles.get(inst)
            t = int(sts) + BAR
            if g is None:
                continue
            if t not in g.index:
                if g.index.max() > t + 3 * BAR:
                    self.con.execute("UPDATE positions SET state = 'cancelled', exit_reason = 'no bar' WHERE pos_id = ?", [pid])
                continue
            o = float(g.at[t, "open"])
            if not np.isfinite(o) or o <= 0:
                continue
            s = 1 if side == "long" else -1
            stop_px = o * (1 - s * stop_frac) if stop_frac else None
            liq_px = o * (1 - s * 0.9 / lever) if lever and lever > 1 else None
            self.con.execute("UPDATE positions SET state = 'open', entry_ts = ?, entry_px = ?, stop_px = ?, liq_px = ?, deadline_ts = ?, "
                             "checked_ts = ?, fee = ? WHERE pos_id = ?",
                             [t, o, stop_px, liq_px, t + (int(hold) - 1) * BAR, t - BAR, self._cost(inst, feats), pid])
            n += 1
        return n

    def check_open(self, candles: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame], now: int) -> int:
        rows = self.con.execute("SELECT pos_id, trader_id, book, inst_id, side, entry_ts, entry_px, notional, lever, stop_px, liq_px, deadline_ts, "
                                "checked_ts, funding, fee, mae, mfe FROM positions WHERE state = 'open'").fetchall()
        closed = 0
        for (pid, trader, book, inst, side, ets, epx, notional, lever, stop_px, liq_px, deadline, checked, fund_acc, fee, mae, mfe) in rows:
            g = candles.get(inst)
            if g is None:
                continue
            seg = g[(g.index > checked) & (g.index <= deadline)]
            if seg.empty:
                if checked >= deadline:
                    self._close(pid, trader, book, side, epx, notional, lever, deadline, epx, "stale", fund_acc, fee, mae, mfe)
                    closed += 1
                elif g.index.max() > checked + 864 * BAR:
                    self._close(pid, trader, book, side, epx, notional, lever, int(g.index.max()), float(g["close"].iloc[-1]), "data gap",
                                fund_acc, fee, mae, mfe)
                    closed += 1
                continue
            exit_ts, exit_px, reason, last_ts, fund_acc, mae, mfe = walk_position(
                side, epx, stop_px, liq_px, deadline, checked, seg.index.to_numpy(np.int64), seg["high"].to_numpy(float),
                seg["low"].to_numpy(float), seg["close"].to_numpy(float), funding.get(inst), fund_acc, mae, mfe)
            if reason:
                self._close(pid, trader, book, side, epx, notional, lever, exit_ts, exit_px, reason, fund_acc, fee, mae, mfe)
                closed += 1
            else:
                self.con.execute("UPDATE positions SET checked_ts = ?, funding = ?, mae = ?, mfe = ? WHERE pos_id = ?", [last_ts, fund_acc, mae, mfe, pid])
        return closed

    def _close(self, pid: str, trader: str, book: str, side: str, epx: float, notional: float, lever: float, ts: int, px: float,
               reason: str, fund_acc: float, fee_side: float, mae: float, mfe: float) -> None:
        s = 1 if side == "long" else -1
        gross = s * (px / epx - 1)
        if reason == "liquidation":
            gross = -1.0 / lever if lever and lever > 1 else gross
        ret = gross - 2 * fee_side + fund_acc
        pnl = ret * notional
        self.con.execute("UPDATE positions SET state = 'closed', exit_ts = ?, exit_px = ?, exit_reason = ?, ret_net = ?, pnl = ?, funding = ?, "
                         "mae = ?, mfe = ?, checked_ts = ? WHERE pos_id = ?", [ts, px, reason, ret, pnl, fund_acc, mae, mfe, ts, pid])
        bk = next((b for b in self.books if b.name == book), None)
        if bk is not None and not bk.unit:
            self.con.execute("UPDATE books SET equity = equity + ?, updated = ? WHERE trader_id = ? AND book = ?", [pnl, ts, trader, book])
            eq = self.equity(trader, book)
            self.con.execute("INSERT OR REPLACE INTO equity_hist VALUES (?, ?, ?, ?)", [trader, book, ts, eq])

    # -- tick
    def tick(self, ctx: Context) -> dict[str, Any]:
        """Process the new bars in time order. For each bar t: fill orders signalled at t-1 (open of t), walk open
        positions through t, then take decisions on t. This makes the outcome independent of how many bars are
        processed per call (5-minute live ticks vs hourly replay steps)."""
        t0 = time.monotonic()
        all_sigs: dict[int, list[Signal]] = {}
        items = [(inst, f, ctx.new_bars[inst]) for inst, f in ctx.feats.items()
                 if inst in ctx.new_bars and len(ctx.new_bars[inst])]
        order = sorted(self.traders, key=lambda t: (t.kind == "follow", t.kind == "llm", hasattr(t, "inner")))
        for tr in order:
            try:
                tr.prepare(ctx)
                sigs = tr.signals_batch(ctx, items) if items else []
                ctx.computed[tr.trader_id] = list(sigs)
                sigs = tr.review(ctx, sigs)
            except Exception:
                log.exception("trader %s failed", tr.trader_id)
                continue
            for s in sigs:
                all_sigs.setdefault(int(s.bar_ts), []).append(s)
        n_fill = n_closed = n_sig = n_open = 0
        for t in sorted(all_sigs):
            insts = {p for (p,) in self.con.execute("SELECT DISTINCT inst_id FROM positions WHERE state IN ('open', 'pending')").fetchall()}
            insts |= {s.inst_id for s in all_sigs[t]}
            upto = {i: ctx.candles[i].loc[:t] for i in insts if i in ctx.candles}
            n_fill += self.fill_pending(upto, ctx.feats)
            n_closed += self.check_open(upto, ctx.funding, ctx.now)
            for s in all_sigs.get(t, []):
                if not s.features:
                    s.features = ctx.snapshot(s.inst_id, s.bar_ts)
                if s.action == "enter":
                    self.resolve_stop(s, ctx.feats)
                if self.record(s):
                    n_sig += 1
                    if s.action == "enter":
                        n_open += self.queue(s, ctx.feats)
        n_fill += self.fill_pending(ctx.candles, ctx.feats)
        n_closed += self.check_open(ctx.candles, ctx.funding, ctx.now)
        return {"fills": n_fill, "closed": n_closed, "signals": n_sig, "queued": n_open, "sec": round(time.monotonic() - t0, 1)}


def new_bars_since(feats: dict[str, pd.DataFrame], last_done: int | None, last_bar: int, max_catchup: int) -> dict[str, np.ndarray]:
    lo = last_bar - (max_catchup - 1) * BAR if last_done is None else max(last_done + BAR, last_bar - (max_catchup - 1) * BAR)
    out = {}
    for inst, f in feats.items():
        idx = f.index.to_numpy(np.int64)
        out[inst] = idx[(idx >= lo) & (idx <= last_bar)]
    return out


def common_last_bar(candles: dict[str, pd.DataFrame], cfg: Config) -> int | None:
    btc = candles.get(cfg.universe["btc_inst"])
    if btc is not None and len(btc):
        return int(btc.index.max())
    lasts = [int(g.index.max()) for g in candles.values() if len(g)]
    return int(np.median(lasts)) if lasts else None


def build_traders(cfg: Config, which: Iterable[str] | None = None) -> list[Trader]:
    from okxrev import traders
    return traders.build(cfg, set(which) if which else None)


def connect(cfg: Config, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = cfg.paper_db
    if read_only and not p.exists():
        raise FileNotFoundError(str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for _ in range(30):
        try:
            con = duckdb.connect(str(p), read_only=read_only)
            if not read_only:
                con.execute(SCHEMA)
            return con
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            if not db._is_lock_error(e):
                raise
            last = e
            time.sleep(1.0)
    raise db.DbBusy(str(last))


def run_tick(cfg: Config, which: Iterable[str] | None = None, traders_: list[Trader] | None = None, now: int | None = None) -> dict[str, Any]:
    pc = cfg.paper
    now = now or db.now_ms()
    t0 = time.monotonic()
    since = now - int(pc.get("window_days", 12)) * DAY
    candles, feats, funding, _ = load_market(cfg, since)
    last_bar = common_last_bar(candles, cfg)
    if last_bar is None:
        return {"note": "no data"}
    con = connect(cfg)
    try:
        trs = traders_ if traders_ is not None else build_traders(cfg, which)
        if any(t.kind == "heat" for t in trs):
            try:
                from okxrev import heat
                heat.refresh(cfg, now)
            except Exception:
                log.exception("heat refresh before tick failed")
        eng = Engine(cfg, trs, con)
        r = con.execute("SELECT MAX(last_bar) FROM ticks").fetchone()
        last_done = int(r[0]) if r and r[0] is not None else None
        bars = new_bars_since(feats, last_done, last_bar, int(pc.get("max_catchup_bars", 36)))
        ctx = Context(cfg, now, last_bar, bars, feats, candles, funding, con)
        res = eng.tick(ctx)
        snapshot_equity(con, eng, candles, last_bar)
        con.execute("INSERT OR REPLACE INTO ticks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [now, last_bar, round(time.monotonic() - t0, 1), res["signals"], res["queued"], res["closed"], "ok"])
        if traders_ is None and any(t.kind == "lab" for t in trs):
            t1 = time.monotonic()
            try:
                from okxrev import custom_rules
                res["lab"] = {**custom_rules.live_alerts(cfg, con, last_bar, now), "sec": round(time.monotonic() - t1, 1)}
            except Exception:
                log.exception("lab alerts failed")
        if cfg.raw.get("heat") is not None and traders_ is None:
            t1 = time.monotonic()
            try:
                from okxrev import heat
                h = heat.tick(cfg, candles, feats, con, now)
                res["heat"] = {**h, "sec": round(time.monotonic() - t1, 1)}
            except Exception:
                log.exception("heat tick failed")
    finally:
        con.close()
    res.update(last_bar=ms_to_str(last_bar), total_sec=round(time.monotonic() - t0, 1), n_traders=len(trs))
    log.info("paper tick: %s", res)
    return res


def snapshot_equity(con: duckdb.DuckDBPyConnection, eng: Engine, candles: dict[str, pd.DataFrame], ts: int) -> None:
    """Mark-to-market equity (realized + open PnL) every few bars for the equity curve."""
    every = int(eng.cfg.paper.get("snapshot_every_bars", 3))
    if (ts // BAR) % every:
        return
    opn = con.execute("SELECT trader_id, book, inst_id, side, entry_px, notional, fee, funding FROM positions WHERE state = 'open'").df()
    upl: dict[tuple[str, str], float] = {}
    for r in opn.itertuples(index=False):
        g = candles.get(r.inst_id)
        if g is None or not len(g):
            continue
        px = float(g["close"].iloc[-1])
        s = 1 if r.side == "long" else -1
        upl[(r.trader_id, r.book)] = upl.get((r.trader_id, r.book), 0.0) + (s * (px / r.entry_px - 1) - r.fee + r.funding) * r.notional
    for tid, book, eq in con.execute("SELECT trader_id, book, equity FROM books").fetchall():
        con.execute("INSERT OR REPLACE INTO equity_hist VALUES (?, ?, ?, ?)", [tid, book, ts, eq + upl.get((tid, book), 0.0)])


# ---------------------------------------------------------------- reporting
def day_t(ret: np.ndarray, ts: np.ndarray) -> dict[str, Any]:
    if len(ret) == 0:
        return {"n": 0}
    dm = pd.Series(ret).groupby(ts // DAY).mean().to_numpy()
    out = {"n": int(len(ret)), "n_days": int(len(dm)), "mean": float(ret.mean()), "day_mean": float(dm.mean()), "win_rate": float((ret > 0).mean())}
    if len(dm) >= 5 and dm.std(ddof=1) > 0:
        out["t"] = float(dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm))))
    return out


def leaderboard(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    tr = con.execute("SELECT * FROM traders").df()
    pos = con.execute("SELECT * FROM positions WHERE state = 'closed'").df()
    opn = con.execute("SELECT trader_id, book, COUNT(*) AS n FROM positions WHERE state IN ('open', 'pending') GROUP BY 1, 2").df()
    bk = con.execute("SELECT * FROM books").df()
    rand = pos[(pos["trader_id"].str.startswith("random")) & (pos["book"] == "C")] if len(pos) else pos
    out = []
    for t in tr.itertuples(index=False):
        row: dict[str, Any] = {"trader_id": t.trader_id, "kind": t.kind, "label": t.label, "params": json.loads(t.params or "{}")}
        for b in ("A", "B", "C"):
            p = pos[(pos["trader_id"] == t.trader_id) & (pos["book"] == b)] if len(pos) else pos
            st = day_t(p["ret_net"].to_numpy(float), p["entry_ts"].to_numpy(np.int64)) if len(p) else {"n": 0}
            st["pnl"] = float(p["pnl"].sum()) if len(p) else 0.0
            st["liquidations"] = int((p["exit_reason"] == "liquidation").sum()) if len(p) else 0
            e = bk[(bk["trader_id"] == t.trader_id) & (bk["book"] == b)]
            if len(e):
                st["equity"] = float(e["equity"].iloc[0])
                st["start"] = float(e["start_eq"].iloc[0])
            o = opn[(opn["trader_id"] == t.trader_id) & (opn["book"] == b)]
            st["open"] = int(o["n"].iloc[0]) if len(o) else 0
            row[b] = st
        c = pos[(pos["trader_id"] == t.trader_id) & (pos["book"] == "C")] if len(pos) else pos
        if len(c) >= 5 and len(rand) >= 5 and not t.trader_id.startswith("random"):
            a = pd.Series(c["ret_net"].to_numpy(float)).groupby(c["entry_ts"].to_numpy(np.int64) // DAY).mean()
            r = pd.Series(rand["ret_net"].to_numpy(float)).groupby(rand["entry_ts"].to_numpy(np.int64) // DAY).mean()
            diff = a.mean() - r.mean()
            se = np.sqrt(a.var(ddof=1) / max(len(a), 1) + r.var(ddof=1) / max(len(r), 1)) if len(a) > 1 and len(r) > 1 else np.nan
            row["vs_random"] = {"diff": float(diff), "t": float(diff / se) if se and np.isfinite(se) and se > 0 else None,
                                "significant": bool(se and np.isfinite(se) and se > 0 and diff / se > 2 and len(a) >= 20)}
        out.append(row)
    pc = pos[pos["book"] == "C"] if len(pos) else pos
    diag = diagnostics(pc)
    from okxrev.discover import book_a
    for row in out:
        if row["trader_id"] in diag:
            row["diag"] = diag[row["trader_id"]]
        c = pc[pc["trader_id"] == row["trader_id"]] if len(pc) else pc
        row["book_a_ret"] = book_a(c["ret_net"].to_numpy(float), c["entry_ts"].to_numpy(np.int64)) if len(c) else None
        v = (row.get("diag") or {}).get("vs_same_day") or {}
        row["profitable"] = bool(len(c) and (row["C"].get("mean") or 0) > 0 and (row["book_a_ret"] or 0) > 0 and v.get("significant"))
    out.sort(key=lambda r: -(r["C"].get("day_mean") or -9) if r["C"].get("n") else 9)
    return out


MIN_SAME_DAY_RANDOM = 3
OVERLAP_MIN = 0.5
GROUP_MIN = 0.8


def diagnostics(pos: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per trader (closed unit-book positions):
    - concentration: trades per entry day -> effective number of days ((sum w)^2 / sum w^2), busiest day's share of
      trades, share of total return from the 3 best days;
    - same-day random: each trade minus the mean of random-control trades of the same side and exit entered on the
      same UTC day (falls back to the same side, any exit), aggregated per day -> mean excess and t over days;
    - overlap: the other trader sharing the largest fraction of its entries (same coin, same signal hour), and a
      group label for traders whose entries are >= 80% shared both ways (effectively one signal)."""
    if pos is None or not len(pos):
        return {}
    p = pos[["trader_id", "inst_id", "side", "entry_ts", "signal_ts", "ret_net"]].copy()
    p = p[p["entry_ts"].notna() & p["ret_net"].notna()]
    if p.empty:
        return {}
    p["day"] = p["entry_ts"].astype(np.int64) // DAY
    rnd = p[p["trader_id"].str.startswith("random")].copy()
    rnd["exit"] = rnd["trader_id"].str.extract(r"^random_(?:long|short)_(.+)$")[0]
    key_exit = rnd.groupby(["side", "exit", "day"])["ret_net"].agg(["mean", "count"])
    key_side = rnd.groupby(["side", "day"])["ret_net"].agg(["mean", "count"])
    exits = {}
    out: dict[str, dict[str, Any]] = {}
    sets: dict[str, set[tuple[str, int]]] = {}
    for t, g in p.groupby("trader_id"):
        sets[t] = set(zip(g["inst_id"], (g["signal_ts"].fillna(g["entry_ts"]).astype(np.int64) // 3_600_000)))
    for t, g in p.groupby("trader_id"):
        if t.startswith("random"):
            continue
        d: dict[str, Any] = {}
        w = g.groupby("day").size().to_numpy(float)
        d["n_days"] = int(len(w))
        d["eff_days"] = float(w.sum() ** 2 / (w ** 2).sum())
        d["max_day_share"] = float(w.max() / w.sum())
        dr = g.groupby("day")["ret_net"].sum()
        tot = float(dr.sum())
        d["top3_days_ret_share"] = float(dr.nlargest(3).sum() / tot) if tot > 0 else None
        base = pd.Series(np.nan, index=g.index)
        if not rnd.empty:
            for ex in _exit_candidates(t, exits):
                k = pd.MultiIndex.from_arrays([g["side"], pd.Series(ex, index=g.index), g["day"]])
                m = key_exit.reindex(k)
                v = np.where(m["count"].to_numpy(float) >= MIN_SAME_DAY_RANDOM, m["mean"].to_numpy(float), np.nan)
                base = base.fillna(pd.Series(v, index=g.index))
            k2 = pd.MultiIndex.from_arrays([g["side"], g["day"]])
            m2 = key_side.reindex(k2)
            v2 = np.where(m2["count"].to_numpy(float) >= MIN_SAME_DAY_RANDOM, m2["mean"].to_numpy(float), np.nan)
            base = base.fillna(pd.Series(v2, index=g.index))
        e = g["ret_net"].astype(float) - base
        ok = e.notna()
        if ok.sum() >= 3:
            dm = e[ok].groupby(g.loc[ok, "day"]).mean()
            sd = dm.std(ddof=1) if len(dm) > 1 else np.nan
            tval = float(dm.mean() / (sd / np.sqrt(len(dm)))) if sd and np.isfinite(sd) and sd > 0 else None
            d["vs_same_day"] = {"excess": float(dm.mean()), "t": tval, "n": int(ok.sum()), "n_days": int(len(dm)),
                                "coverage": float(ok.mean()), "significant": bool(tval is not None and tval > 2 and len(dm) >= 20)}
        best, share = None, 0.0
        mine = sets[t]
        for o, s in sets.items():
            if o == t or o.startswith("random") or not mine:
                continue
            sh = len(mine & s) / len(mine)
            if sh > share:
                best, share = o, sh
        if best and share >= OVERLAP_MIN:
            d["overlap"] = {"with": best, "share": share}
        out[t] = d
    names = [t for t in out if sets.get(t)]
    parent = {t: t for t in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            inter = len(sets[a] & sets[b])
            if inter and inter / len(sets[a]) >= GROUP_MIN and inter / len(sets[b]) >= GROUP_MIN:
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for t in names:
        groups.setdefault(find(t), []).append(t)
    for members in groups.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        for t in members:
            out[t]["group"] = members
    return out


def _exit_candidates(trader_id: str, cache: dict[str, list[str]]) -> list[str]:
    """Exit rule names to match random controls: the suffix after the first '_' that names a known exit."""
    if trader_id in cache:
        return cache[trader_id]
    parts = trader_id.split("_")
    cands = ["_".join(parts[i:]) for i in range(1, len(parts))]
    res = [c for c in cands if c in EXIT_RULES]
    cache[trader_id] = res
    return res


def market_context(con: duckdb.DuckDBPyConnection, candle_db: Any | None = None) -> dict[str, Any]:
    """Background for a leaderboard period: random-control per-trade means by side/exit, and (if a candle database
    is given) the market's compounded median daily return over the same days."""
    try:
        pos = con.execute("SELECT trader_id, side, entry_ts, ret_net FROM positions WHERE book = 'C' AND state = 'closed'").df()
    except duckdb.Error:
        return {}
    if pos.empty:
        return {}
    lo, hi = int(pos["entry_ts"].min()), int(pos["entry_ts"].max())
    out: dict[str, Any] = {"from": lo, "to": hi, "days": (hi - lo) / DAY}
    rnd = pos[pos["trader_id"].str.startswith("random")]
    if len(rnd):
        g = rnd.assign(exit=rnd["trader_id"].str.replace(r"^random_(long|short)_", "", regex=True)).groupby(["side", "exit"])["ret_net"]
        out["random"] = [{"side": s, "exit": e, "n": int(len(x)), "mean": float(x.mean())} for (s, e), x in g]
    if candle_db is not None:
        try:
            with db.open_db(candle_db, read_only=True, wait_sec=1.0, max_tries=5) as c:
                r = c.execute("""
                    WITH d AS (SELECT inst_id, ts, close FROM candles WHERE ts >= ? AND ts <= ? AND ts % 86400000 = 0),
                         x AS (SELECT ts, close / LAG(close) OVER (PARTITION BY inst_id ORDER BY ts) - 1 AS r FROM d)
                    SELECT ts, median(r) m, avg(CASE WHEN r < 0 THEN 1.0 ELSE 0.0 END) dn FROM x WHERE r IS NOT NULL GROUP BY ts ORDER BY ts""",
                              [lo - lo % DAY, hi + DAY]).fetchall()
            if r:
                out["market_cum"] = float(np.prod([1 + m for _, m, _ in r]) - 1)
                out["market_days"] = [{"ts": int(t), "median": float(m), "down": float(dn)} for t, m, dn in r]
        except Exception:
            pass
    return out


def status(cfg: Config) -> dict[str, Any]:
    try:
        con = connect(cfg, read_only=True)
    except FileNotFoundError:
        return {"exists": False}
    try:
        last = con.execute("SELECT * FROM ticks ORDER BY tick_ts DESC LIMIT 1").df().to_dict("records")
        out = {
            "exists": True, "last_tick": last[0] if last else None,
            "n_ticks": int(con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]),
            "first_tick": con.execute("SELECT MIN(tick_ts) FROM ticks").fetchone()[0],
            "n_decisions": int(con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]),
            "n_open": int(con.execute("SELECT COUNT(*) FROM positions WHERE state IN ('open', 'pending')").fetchone()[0]),
            "n_closed": int(con.execute("SELECT COUNT(*) FROM positions WHERE state = 'closed'").fetchone()[0]),
            "leaderboard": leaderboard(con),
            "market": market_context(con, cfg.paths.db),
        }
        llm = con.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM llm_calls WHERE ts >= ?", [db.now_ms() - DAY]).fetchone()
        out["llm_24h"] = {"calls": int(llm[0]), "cost": float(llm[1])}
        return out
    finally:
        con.close()


def export(cfg: Config, fmt: str = "parquet") -> list[str]:
    con = connect(cfg, read_only=True)
    out = []
    try:
        d = cfg.paths.results / "paper_export"
        d.mkdir(parents=True, exist_ok=True)
        for t in ("decisions", "positions", "traders", "equity_hist", "llm_calls"):
            df = con.execute(f"SELECT * FROM {t}").df()
            p = d / f"{t}.{fmt}"
            if fmt == "parquet":
                df.to_parquet(p, index=False)
            else:
                df.to_csv(p, index=False, encoding="utf-8")
            out.append(str(p))
    finally:
        con.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.paper")
    ap.add_argument("--config", default=None)
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--export", choices=["parquet", "csv"], default=None)
    ap.add_argument("--traders", nargs="+", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--force", action="store_true", help="run the tick even when data collection is paused")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "paper", console=not args.quiet)
    from okxrev import collect
    if args.tick and collect.is_paused(cfg) and not args.force:
        log.info("data collection paused (data/PAUSE); skipping paper tick")
        if not args.quiet:
            print("資料收集已暫停，跳過這一輪（加 --force 強制執行）")
        args.tick = False
        if not (args.export or args.status):
            return 0
    if args.tick:
        from filelock import FileLock, Timeout
        lock = FileLock(str(cfg.paper_db) + ".lock", timeout=0)
        try:
            lock.acquire()
        except Timeout:
            log.info("another paper tick is running; exiting")
            return 0
        try:
            r = run_tick(cfg, args.traders)
            if r.get("total_sec", 0) > 240:
                log.warning("paper tick took %.0fs (>240s); CPU may be busy (background priority is %s)",
                            r["total_sec"], (cfg.raw.get("background") or {}).get("priority", "idle"))
            from okxrev import ml
            if cfg.paper.get("auto_retrain", True) and (ml.needs_retrain(cfg, "ml") or ml.needs_retrain(cfg, "twin")):
                from okxrev.web.tasks import background_popen, venv_python
                background_popen(cfg, [venv_python(cfg, windowless=True), "-m", "okxrev.ml", "--if-due"])
        except Exception:
            log.exception("paper tick failed")
            return 1
        finally:
            lock.release()
        if not args.quiet:
            print(r)
    if args.export:
        print("\n".join(export(cfg, args.export)))
    if args.status or not (args.tick or args.export):
        s = status(cfg)
        if not s.get("exists"):
            print("尚未執行紙上交易（python -m okxrev.paper --tick）")
            return 0
        print(f"ticks {s['n_ticks']}  decisions {s['n_decisions']}  open {s['n_open']}  closed {s['n_closed']}  LLM 24h {s['llm_24h']}")
        for r in s["leaderboard"]:
            c = r["C"]
            print(f"  {r['trader_id']:<34} n={c.get('n', 0):>4} 日均 {c.get('day_mean', 0) * 100 if c.get('n') else 0:+6.2f}%  "
                  f"A {r['A'].get('equity', 0):>9.2f}  B {r['B'].get('equity', 0):>8.2f}  open {r['A'].get('open', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
