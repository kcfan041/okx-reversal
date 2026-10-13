from __future__ import annotations

import hashlib
import logging
from typing import Any

import numpy as np
import pandas as pd

from okxrev.config import Config
from okxrev.paper import EXIT_RULES, Context, Signal, Trader

log = logging.getLogger("okxrev.traders")


def _h(*parts: Any) -> float:
    d = hashlib.sha1("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(d[:8], "big") / 2**64


def _seed(*parts: Any) -> np.uint64:
    return np.uint64(int.from_bytes(hashlib.sha1("|".join(map(str, parts)).encode()).digest()[:8], "big"))


def uhash(seed: np.uint64, ts: np.ndarray) -> np.ndarray:
    """Deterministic uniform [0,1) per (seed, bar timestamp) via splitmix64; vectorised so a year of bars is cheap
    and identical whether evaluated bar by bar (live) or in one batch (replay)."""
    with np.errstate(over="ignore"):
        z = np.asarray(ts, dtype=np.uint64) * np.uint64(0x9E3779B97F4A7C15) + seed
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        z = z ^ (z >> np.uint64(31))
    return (z >> np.uint64(11)).astype(np.float64) / float(1 << 53)


class RuleTrader(Trader):
    kind = "rule"

    def __init__(self, res: dict[str, Any], idx: int):
        from okxrev import discover as D
        self.res = res
        rules = {r.key: r for r in D.enumerate_rules()}
        self.rule = rules[res["key"]]
        self.exit = res["exit"]
        self.trader_id = f"rule{idx:02d}_{res['exit']}"
        self.label = f"{res['label']}（{res['exit']}，{res['verdict']}）"
        self.fresh_gap = D.FRESH_GAP

    def params(self) -> dict[str, Any]:
        return {"key": self.res["key"], "exit": self.exit, "verdict": self.res["verdict"],
                "valid_day_mean": self.res["valid"].get("day_mean"), "holdout_day_mean": self.res["holdout"].get("day_mean")}

    def signals(self, ctx: Context, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        idx = f.index.to_numpy(np.int64)
        lo = max(0, int(np.searchsorted(idx, bars[0])) - self.fresh_gap - 1)
        if hasattr(ctx, "window"):
            sidx, cols = ctx.window(inst, f, lo)
        else:
            sub = f.iloc[lo:]
            sidx, cols = sub.index.to_numpy(np.int64), {k: sub[k].to_numpy() for k in sub.columns}
        m = self.rule.mask(cols)
        cs = np.cumsum(m.astype(np.int32))
        out = []
        for b in bars:
            i = int(np.searchsorted(sidx, b))
            if i >= len(sidx) or sidx[i] != b or not m[i]:
                continue
            prev = cs[i - 1] if i > 0 else 0
            back = cs[i - self.fresh_gap - 1] if i - self.fresh_gap - 1 >= 0 else 0
            if prev - back == 0:
                out.append(Signal(self.trader_id, inst, int(b), self.rule.side, self.exit, 1.0, self.rule.label))
        return out


class RandomTrader(Trader):
    kind = "random"

    def __init__(self, side: str, exit_rule: str, prob: float):
        self.side, self.exit, self.prob = side, exit_rule, prob
        self.trader_id = f"random_{side}_{exit_rule}"
        self.label = f"隨機{'做多' if side == 'long' else '做空'}（{exit_rule}）對照組"

    def params(self) -> dict[str, Any]:
        return {"side": self.side, "exit": self.exit, "prob": self.prob}

    def signals(self, ctx: Context, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        bars = np.asarray(bars, dtype=np.int64)
        u = uhash(_seed(self.trader_id, inst), bars)
        pick = bars[u < self.prob]
        if len(pick) == 0:
            return []
        r24 = f["r24h"].reindex(pick).to_numpy(float)
        return [Signal(self.trader_id, inst, int(b), self.side, self.exit, 0.0, "random") for b, v in zip(pick, r24) if np.isfinite(v)]


class ModelTrader(Trader):
    """Scores bars with a saved LightGBM model (okxrev.ml). Long and short are separate models."""
    kind = "ml"

    def __init__(self, cfg: Config, name: str = "ml", label: str = "機器學習模型（LightGBM）", exit_override: str | None = None):
        self.cfg = cfg
        self.name = name
        self.trader_id = name if exit_override is None else f"{name}_{exit_override}"
        self.label = label
        self.exit_override = exit_override
        self.models: dict[str, Any] = {}

    def params(self) -> dict[str, Any]:
        return {k: {"version": m["version"], "threshold": m["threshold"], "exit": m["exit"]} for k, m in self.models.items()}

    _cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def prepare(self, ctx: Context) -> None:
        """Load the latest model per side; reuse the in-process copy while the file on disk is unchanged."""
        from okxrev import ml
        self.models = {}
        for side in ("long", "short"):
            files = sorted(ml.model_dir(self.cfg, self.name).glob(f"{side}_*.lgb"))
            if not files:
                continue
            key = (str(files[-1]), side)
            mt = files[-1].stat().st_mtime
            hit = ModelTrader._cache.get(key)
            if hit is None or hit[0] != mt:
                m = ml.load_latest(self.cfg, self.name, side)
                if m is None:
                    continue
                ModelTrader._cache[key] = (mt, m)
                hit = ModelTrader._cache[key]
            self.models[side] = hit[1]

    def signals(self, ctx: Context, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        return self.signals_batch(ctx, [(inst, f, bars)])

    def signals_batch(self, ctx: Context, items: list[tuple[str, pd.DataFrame, np.ndarray]]) -> list[Signal]:
        """Fires only at the start of an episode: score >= threshold now and below it for the previous FRESH_GAP
        bars (same freshness rule as RuleTrader). All instruments' windows are scored in one predict call per model;
        LightGBM scores rows independently, so batching gives the same probabilities as per-instrument calls."""
        if not self.models or not items:
            return []
        from okxrev import ml
        from okxrev.discover import FRESH_GAP
        parts, meta = [], []
        for inst, f, bars in items:
            bars = np.asarray(bars, dtype=np.int64)
            if len(bars) == 0:
                continue
            idx = f.index.to_numpy(np.int64)
            lo = max(0, int(np.searchsorted(idx, bars[0])) - FRESH_GAP - 1)
            hi = int(np.searchsorted(idx, bars[-1], side="right"))
            if hi <= lo:
                continue
            sub = f.iloc[lo:hi]
            parts.append(sub)
            meta.append((inst, sub.index.to_numpy(np.int64), bars))
        if not parts:
            return []
        big = pd.concat(parts, ignore_index=False, sort=False)
        bounds = np.cumsum([0] + [len(m[1]) for m in meta])
        out: list[Signal] = []
        for side, m in self.models.items():
            p_all = ml.predict(m, big)
            for (inst, sidx, bars), a, b in zip(meta, bounds[:-1], bounds[1:]):
                p = p_all[a:b]
                hit = np.isfinite(p) & (p >= m["threshold"])
                cs = np.cumsum(hit.astype(np.int32))
                prev = np.r_[0, cs[:-1]]
                back = np.r_[np.zeros(FRESH_GAP + 1, dtype=np.int64), cs[: max(len(cs) - FRESH_GAP - 1, 0)]][: len(cs)]
                fresh = hit & ((prev - back) == 0) & np.isin(sidx, bars)
                for ts, prob in zip(sidx[fresh], p[fresh]):
                    ex = self.exit_override or m["exit"]
                    feats = ctx.snapshot(inst, int(ts))
                    if ex == "you":
                        feats["_hold_bars"] = int(m.get("your_hold_bars") or 72)
                    out.append(Signal(self.trader_id, inst, int(ts), side, ex, float(prob), f"p={prob:.3f} ≥ {m['threshold']:.3f}",
                                      m["version"], feats))
        return out


class Precomputed(Trader):
    """Replay helper: runs the wrapped trader's own `signals()` once per instrument over the whole period, then
    serves those signals bar by bar. Every signal depends only on bars at or before its timestamp (rule masks,
    freshness, per-bar hashes and model scores are all computed from past data), so batching does not change
    which bars fire."""

    def __init__(self, inner: Trader):
        self.inner = inner
        self.trader_id, self.kind, self.label = inner.trader_id, inner.kind, inner.label
        self.by_inst: dict[str, dict[int, list[Signal]]] = {}

    def params(self) -> dict[str, Any]:
        return self.inner.params()

    def build(self, ctx: Context, lo: int, hi: int, chunk: int = 8) -> int:
        """Batch `chunk` instruments at a time through the trader's own signals_batch (bounded memory)."""
        self.inner.prepare(ctx)
        items = []
        for inst, f in ctx.feats.items():
            ix = f.index.to_numpy(np.int64)
            bars = ix[(ix >= lo) & (ix <= hi)]
            if len(bars):
                items.append((inst, f, bars))
        n = 0
        for k in range(0, len(items), chunk):
            for s in self.inner.signals_batch(ctx, items[k:k + chunk]):
                self.by_inst.setdefault(s.inst_id, {}).setdefault(int(s.bar_ts), []).append(s)
                n += 1
        return n

    def signals(self, ctx: Context, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        d = self.by_inst.get(inst)
        if not d:
            return []
        out = []
        for b in bars:
            out += d.get(int(b), [])
        return out


class LLMReviewer(Trader):
    """Wraps another trader; every candidate is sent to an LLM which accepts or rejects it."""
    kind = "llm"

    def __init__(self, cfg: Config, inner: Trader):
        self.cfg = cfg
        self.inner = inner
        self.trader_id = f"llm_{inner.trader_id}"
        self.label = f"LLM 審核：{inner.label}"

    def params(self) -> dict[str, Any]:
        from okxrev import llm
        return {"inner": self.inner.trader_id, "model": llm.settings(self.cfg).get("LLM_MODEL")}

    def _rename(self, sigs: list[Signal]) -> list[Signal]:
        from dataclasses import replace as _replace
        return [_replace(s, trader_id=self.trader_id, features=dict(s.features)) for s in sigs]

    def prepare(self, ctx: Context) -> None:
        if self.inner.trader_id not in getattr(ctx, "computed", {}):
            self.inner.prepare(ctx)

    def signals(self, ctx: Context, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        return self._rename(self.inner.signals(ctx, inst, f, bars))

    def signals_batch(self, ctx: Context, items: list[tuple[str, pd.DataFrame, np.ndarray]]) -> list[Signal]:
        """Reuse the wrapped trader's signals from this tick when the engine already computed them."""
        done = getattr(ctx, "computed", {}).get(self.inner.trader_id)
        if done is not None:
            return self._rename(done)
        return self._rename(self.inner.signals_batch(ctx, items))

    def review(self, ctx: Context, sigs: list[Signal]) -> list[Signal]:
        from okxrev import llm
        if not sigs:
            return sigs
        sigs = [s for s in sigs if not ctx.con.execute("SELECT 1 FROM decisions WHERE decision_id = ?", [s.decision_id]).fetchone()]
        return llm.review(ctx, self.cfg, sigs)


def build(cfg: Config, which: set[str] | None = None) -> list[Trader]:
    from okxrev import discover, llm, ml
    out: list[Trader] = []
    pc = cfg.paper
    res = discover.load(cfg) or {}
    active = [r for r in res.get("results", []) if r["verdict"] in ("可用", "觀察中")]
    exits_used = sorted({r["exit"] for r in active} | {"6h_sl2atr"})
    for i, r in enumerate(active, 1):
        out.append(RuleTrader(r, i))
    from okxrev import custom_rules
    lab = custom_rules.traders(cfg)
    out += lab
    exits_used = sorted(set(exits_used) | {t.exit for t in lab})
    prob = float(pc.get("random_prob", 0.0002))
    for side in ("long", "short"):
        for ex in exits_used:
            out.append(RandomTrader(side, ex, prob))
    if ml.has_model(cfg, "ml"):
        out.append(ModelTrader(cfg, "ml"))
    if ml.has_model(cfg, "twin"):
        out.append(ModelTrader(cfg, "twin", "你的分身（最佳化出場）"))
        out.append(ModelTrader(cfg, "twin", "你的分身（你自己的出場習慣）", exit_override="you"))
    if llm.enabled(cfg):
        best = [t for t in out if isinstance(t, RuleTrader)][:2]
        if ml.has_model(cfg, "ml"):
            best.append(ModelTrader(cfg, "ml"))
        for t in best:
            out.append(LLMReviewer(cfg, t))
    if which:
        out = [t for t in out if t.trader_id in which or t.kind in which]
    return out
