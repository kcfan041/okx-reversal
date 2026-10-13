"""Regression tests for the CPU-spike fixes (docs/cpu-spike-report.md)."""
import subprocess

import numpy as np
import pandas as pd
import pytest

from okxrev import ml, paper
from okxrev.config import load_config
from okxrev.paper import BAR, Context, Signal

T0 = 1_700_000_000_000


def test_background_popen_flags_and_env(monkeypatch):
    from okxrev.web import tasks
    seen = {}

    def fake(args, creationflags=0, **kw):
        seen.update(args=args, flags=creationflags, env=kw["env"], cwd=kw["cwd"])
        return "proc"

    monkeypatch.setattr(subprocess, "Popen", fake)
    monkeypatch.setattr(tasks.sys, "platform", "win32")
    cfg = load_config().copy_with({"background": {"priority": "idle", "threads": 3}})
    assert tasks.background_popen(cfg, ["x"]) == "proc"
    assert seen["flags"] & 0x00000040 and seen["flags"] & tasks.CREATE_NO_WINDOW
    assert all(seen["env"][k] == "3" for k in tasks.THREAD_VARS)
    cfg2 = cfg.copy_with({"background": {"priority": "below_normal"}})
    tasks.background_popen(cfg2, ["x"])
    assert seen["flags"] & 0x00004000 and not seen["flags"] & 0x00000040


def test_lightgbm_thread_budget(monkeypatch):
    monkeypatch.setenv("OKXREV_THREADS", "2")
    assert ml.n_threads() == 2
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"a": rng.normal(size=500), "b": rng.normal(size=500)})
    b = ml.Booster(5, num_leaves=3, min_data_in_leaf=5).fit(X, (X["a"] > 0).astype(int))
    assert b.booster_.params.get("num_threads") == 2
    monkeypatch.delenv("OKXREV_THREADS")
    assert ml.n_threads() == 0


def _feats(n, seed):
    rng = np.random.default_rng(seed)
    idx = pd.Index(T0 + np.arange(n, dtype=np.int64) * BAR)
    f = pd.DataFrame({c: rng.normal(0, 0.05, n) for c in ml.NUM}, index=idx)
    f["turnover24"] = 1e7
    f["btc_regime"] = "neutral"
    for c in ml.FLAGS:
        f[c] = False
    return f


def test_batch_prediction_matches_per_instrument(tmp_path):
    from okxrev.traders import ModelTrader
    rng = np.random.default_rng(1)
    train = _feats(3000, 9)
    X = ml.matrix(train)
    y = (train["r1h"] + 0.01 * rng.normal(size=3000) > 0.08).astype(int)
    booster = ml.Booster(30, num_leaves=7, min_data_in_leaf=20).fit(X, y)
    thr = float(np.quantile(booster.predict_proba(X)[:, 1], 0.97))
    mt = ModelTrader(load_config(), "ml")
    mt.models = {"long": {"model": booster, "features": list(X.columns), "threshold": thr, "exit": "1h", "version": "v"}}

    class Ctx:
        def snapshot(self, inst, ts):
            return {}

    data = {f"I{k}": _feats(500, k) for k in range(6)}
    items = [(i, f, f.index.to_numpy()[-150:]) for i, f in data.items()]
    batch = sorted((s.inst_id, s.bar_ts, round(s.score, 12)) for s in mt.signals_batch(Ctx(), items))
    single = sorted((s.inst_id, s.bar_ts, round(s.score, 12)) for i, f, b in items for s in mt.signals(Ctx(), i, f, b))
    assert batch == single and len(batch) >= 3
    for i, f, b in items:
        sub = f.iloc[-150 - 73:]
        p1 = ml.predict(mt.models["long"], sub)
        p2 = ml.predict(mt.models["long"], pd.concat([data["I0"].iloc[:10], sub]))[10:]
        assert np.max(np.abs(p1 - p2)) < 1e-12


def test_context_window_cache_shared():
    f = _feats(300, 2)
    ctx = Context(load_config(), 0, 0, {}, {"A": f}, {}, {}, None)
    a = ctx.window("A", f, 100)
    b = ctx.window("A", f, 100)
    assert a is b and len(a[0]) == 200 and set(a[1]) == set(f.columns)


def test_llm_reuses_inner_signals():
    from okxrev.traders import LLMReviewer

    class Inner(paper.Trader):
        trader_id, kind, label = "rule01", "rule", "r"
        calls = 0

        def prepare(self, ctx):
            Inner.calls += 10

        def signals(self, ctx, inst, f, bars):
            Inner.calls += 1
            return [Signal("rule01", inst, int(bars[0]), "short", "1h", 1.0, "x", features={"atr": 0.01})]

    inner = Inner()
    rv = LLMReviewer(load_config(), inner)
    f = _feats(10, 3)
    ctx = Context(load_config(), 0, 0, {}, {"A": f}, {}, {}, None)
    items = [("A", f, f.index.to_numpy()[-1:])]
    ctx.computed["rule01"] = inner.signals_batch(ctx, items)
    Inner.calls = 0
    rv.prepare(ctx)
    out = rv.signals_batch(ctx, items)
    assert Inner.calls == 0
    assert [s.trader_id for s in out] == ["llm_rule01"] and ctx.computed["rule01"][0].trader_id == "rule01"
    out[0].features["x"] = 1
    assert "x" not in ctx.computed["rule01"][0].features


def test_feature_builder_matches_dataframe():
    from okxrev import discover as D
    from okxrev.detector import prepare
    from test_detector_metrics import CFG, pumped
    g = prepare(pumped(n=2400), CFG.detector, None)
    new = D.features(g, CFG.detector, None, None, None)
    orig = D._ColBuilder

    class Old(pd.DataFrame):
        def __init__(self, index):
            super().__init__(index=index)

        def frame(self):
            return pd.DataFrame(self)

    D._ColBuilder = Old
    try:
        old = D.features(g, CFG.detector, None, None, None)
    finally:
        D._ColBuilder = orig
    pd.testing.assert_frame_equal(new, old, check_exact=True)
