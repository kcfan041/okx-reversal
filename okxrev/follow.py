"""Dynamic follow traders: every `step` hours re-pick the traders with the best recent per-trade results (net return
minus the same-side random-control mean over the look-back window, using only positions already closed before the
re-pick time) and copy their new entries until the next re-pick.

Rules shared by every follow trader:
  - candidates exclude random controls, LLM reviewers, follow traders; a rule and its _sl20 variant count as one
    (only the better one can be picked);
  - a candidate needs >= min_n closed trades in the window and a positive raw per-trade mean;
  - the same coin / side / bar is entered once (from the highest-ranked pick);
  - every copied entry gets a 20% stop: plain time exits are switched to their _sl20pct variant, other exits without
    a stop get _stop_frac = 0.20, exits that already have a stop keep it.
Long sets are the main candidates; short sets are kept as controls (in a rough blind-period check they lost money).
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from okxrev.paper import EXIT_RULES, FIXED_STOP, Signal, Trader, with_sl20

HOUR = 3_600_000
FOLLOW_STOP = 0.20

FOLLOW_SETS: list[dict[str, Any]] = [
    {"id": "follow_7d_60d_top3", "step_h": 168, "look_h": 1440, "top": 3, "min_n": 20, "short": False},
    {"id": "follow_7d_30d_top3", "step_h": 168, "look_h": 720, "top": 3, "min_n": 10, "short": False},
    {"id": "follow_14d_90d_top5", "step_h": 336, "look_h": 2160, "top": 5, "min_n": 20, "short": False},
    {"id": "follow_1d_3d_top3", "step_h": 24, "look_h": 72, "top": 3, "min_n": 5, "short": True},
    {"id": "follow_6h_2d_top3", "step_h": 6, "look_h": 48, "top": 3, "min_n": 5, "short": True},
    {"id": "follow_3d_14d_top3", "step_h": 72, "look_h": 336, "top": 3, "min_n": 10, "short": True},
]


def _h(hours: int) -> str:
    return f"{hours // 24} 天" if hours % 24 == 0 else f"{hours} 小時"


def label(fs: dict[str, Any]) -> str:
    base = f"跟單：每 {_h(fs['step_h'])}重選，回看 {_h(fs['look_h'])}，跟前 {fs['top']} 名（≥{fs['min_n']} 筆），止損 20%"
    return base + ("（短期對照，盲測粗測虧損）" if fs["short"] else "")


def family(trader_id: str) -> str:
    return trader_id[:-5] if trader_id.endswith("_sl20") else trader_id


def eligible(trader_id: str) -> bool:
    return not trader_id.startswith(("random", "llm_", "follow_"))


def rank(closed: pd.DataFrame, at: int, look_h: int, top: int, min_n: int) -> list[dict[str, Any]]:
    """closed: trader_id, side, exit_ts, ret_net (unit book). Uses rows with exit_ts in [at - look, at)."""
    if closed is None or closed.empty:
        return []
    w = closed[(closed["exit_ts"] < at) & (closed["exit_ts"] >= at - look_h * HOUR)]
    if w.empty:
        return []
    rnd = w[w["trader_id"].str.startswith("random")]
    base = rnd.groupby("side")["ret_net"].mean() if len(rnd) else pd.Series(dtype=float)
    c = w[w["trader_id"].map(eligible)]
    if c.empty:
        return []
    c = c.assign(x=c["ret_net"] - c["side"].map(base).fillna(0.0))
    g = c.groupby("trader_id").agg(score=("x", "mean"), n=("x", "size"), mean=("ret_net", "mean"))
    g = g[(g["n"] >= min_n) & (g["mean"] > 0)].sort_values("score", ascending=False)
    out, fams = [], set()
    for tid, r in g.iterrows():
        f = family(str(tid))
        if f in fams:
            continue
        fams.add(f)
        out.append({"trader_id": str(tid), "score": float(r["score"]), "n": int(r["n"]), "mean": float(r["mean"])})
        if len(out) >= top:
            break
    return out


def copy_signal(s: Signal, follow_id: str, src: str) -> Signal:
    feats = dict(s.features or {})
    ex = s.exit_rule
    sl = with_sl20(ex)
    if sl:
        ex = sl
    elif ex not in FIXED_STOP and not (EXIT_RULES.get(ex, (None, None))[1]):
        feats["_stop_frac"] = FOLLOW_STOP
    feats["_follow_src"] = src
    return replace(s, trader_id=follow_id, exit_rule=ex, features=feats, reason=f"跟 {src}：{s.reason}"[:500])


def period_of(ts: int, step_h: int) -> int:
    return (int(ts) // (step_h * HOUR)) * step_h * HOUR


class FollowTrader(Trader):
    kind = "follow"

    def __init__(self, fs: dict[str, Any]):
        self.fs = fs
        self.trader_id = fs["id"]
        self.label = label(fs)
        self.picks_cache: dict[int, list[dict[str, Any]]] = {}

    def params(self) -> dict[str, Any]:
        return {k: v for k, v in self.fs.items() if k != "id"}

    def picks(self, con: Any, p: int) -> list[dict[str, Any]]:
        """Picks for the period starting at p; computed once and stored in follow_picks."""
        if p in self.picks_cache:
            return self.picks_cache[p]
        row = con.execute("SELECT picks FROM follow_picks WHERE trader_id = ? AND period_ts = ?", [self.trader_id, p]).fetchone()
        if row:
            out = json.loads(row[0])
        else:
            lo = p - self.fs["look_h"] * HOUR
            closed = con.execute("SELECT trader_id, side, exit_ts, ret_net FROM positions WHERE book = 'C' AND state = 'closed' "
                                 "AND exit_ts >= ? AND exit_ts < ?", [lo, p]).df()
            out = rank(closed, p, self.fs["look_h"], self.fs["top"], self.fs["min_n"])
            con.execute("INSERT OR REPLACE INTO follow_picks VALUES (?, ?, ?, ?)", [self.trader_id, p, json.dumps(out), int(pd.Timestamp.now().value // 1_000_000)])
        self.picks_cache[p] = out
        return out

    def signals_batch(self, ctx: Any, items: list[tuple[str, pd.DataFrame, np.ndarray]]) -> list[Signal]:
        computed = getattr(ctx, "computed", {})
        bars = sorted({int(b) for _, _, bb in items for b in bb})
        if not bars or ctx.con is None:
            return []
        periods = sorted({period_of(b, self.fs["step_h"]) for b in bars})
        out: list[Signal] = []
        seen: set[tuple[str, int, str]] = set()
        for p in periods:
            lead = self.picks(ctx.con, p)
            hi = p + self.fs["step_h"] * HOUR
            for pk in lead:
                for s in computed.get(pk["trader_id"], []):
                    if not (p <= s.bar_ts < hi) or s.action != "enter":
                        continue
                    k = (s.inst_id, int(s.bar_ts), s.side)
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(copy_signal(s, self.trader_id, pk["trader_id"]))
        return out

    def signals(self, ctx: Any, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        return [s for s in self.signals_batch(ctx, [(inst, f, bars)]) if s.inst_id == inst]


def traders(kinds: dict[str, bool]) -> list[FollowTrader]:
    return [FollowTrader(fs) for fs in FOLLOW_SETS if kinds.get("follow_short" if fs["short"] else "follow_long", True)]


def replay_signals(sig: pd.DataFrame, closed: pd.DataFrame, start: int, end: int, sets: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Blind-test version: picks at every re-pick time from the first-pass positions, copied signals from the
    precomputed signal table (columns SIG_COLS). Returns (signals, picks log)."""
    rows, log_rows = [], []
    sig = sig.sort_values("bar_ts")
    for fs in sets:
        step = fs["step_h"] * HOUR
        p = period_of(start, fs["step_h"])
        prm = json.dumps({k: v for k, v in fs.items() if k != "id"})
        while p <= end:
            lead = rank(closed, p, fs["look_h"], fs["top"], fs["min_n"])
            log_rows.append((fs["id"], p, json.dumps(lead)))
            if lead:
                w = sig[(sig["bar_ts"] >= p) & (sig["bar_ts"] < p + step) & sig["trader_id"].isin([x["trader_id"] for x in lead])]
                order = {x["trader_id"]: i for i, x in enumerate(lead)}
                w = w.assign(_o=w["trader_id"].map(order)).sort_values(["bar_ts", "_o"]).drop_duplicates(["inst_id", "bar_ts", "side"])
                for r in w.itertuples(index=False):
                    s = Signal(r.trader_id, r.inst_id, int(r.bar_ts), r.side, r.exit_rule, float(r.score), r.reason, r.model_ver or "",
                               json.loads(r.features) if r.features else {})
                    c = copy_signal(s, fs["id"], r.trader_id)
                    rows.append((fs["id"], "follow", label(fs), prm, c.inst_id, c.bar_ts, c.side, c.exit_rule, c.score, c.reason,
                                 c.model_ver, json.dumps(c.features, ensure_ascii=False, default=float)))
            p += step
    from okxrev.replay import SIG_COLS
    return pd.DataFrame(rows, columns=SIG_COLS), pd.DataFrame(log_rows, columns=["trader_id", "period_ts", "picks"])
