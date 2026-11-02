import numpy as np
import pandas as pd
import pytest

from okxrev import regime as R
from okxrev.paper import BAR, Context

D = R.D
T0 = 1_780_000_000_000 - (1_780_000_000_000 % D)


def test_streaks_and_first_hits():
    r = pd.Series([0.01, -0.01, -0.02, -0.01, 0.0, -0.01, -0.01, 0.02, 0.01, 0.01])
    st = R.streaks(r)
    assert st["dn"].tolist() == [0, 1, 2, 3, 0, 1, 2, 0, 0, 0]
    assert st["up"].tolist() == [1, 0, 0, 0, 0, 0, 0, 1, 2, 3]
    assert R.first_hits(st, "dn", 2).tolist() == [2, 6]
    assert R.first_hits(st, "dn", 3).tolist() == [3]


def test_event_test_uses_one_event_per_streak():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.01, 400)
    for s in range(20, 380, 40):            # five down days then a +5% day
        x[s - 1] = 0.01
        x[s:s + 5] = -0.01
        x[s + 5] = 0.05
    r = pd.Series(x, index=T0 + np.arange(400) * D)
    e = R.event_test(r, 5, 1, "dn", sims=200)
    assert e["n"] >= 9 and e["mean"] > 0.025 and e["p"] < 0.05


def market(days=12, n=25, down=(3, 4, 5, 6)):
    """5m candles for n alts: flat, except every coin falls 2% per day on the `down` days."""
    idx = T0 + np.arange(days * 288, dtype=np.int64) * BAR
    day = (idx - T0) // D
    step = np.where(np.isin(day, down), np.log(0.98) / 288, 0.0)
    p = 100 * np.exp(np.cumsum(step))
    candles, feats = {}, {}
    for i in range(n):
        inst = f"C{i:02d}-USDT-SWAP"
        candles[inst] = pd.DataFrame({"open": p, "high": p, "low": p, "close": p}, index=idx)
        feats[inst] = pd.DataFrame({"age_d": 100.0, "turnover24": 1e6 * (i + 1)}, index=idx)
    return candles, feats


def test_trader_buys_top_turnover_once_when_streak_reaches_k():
    candles, feats = market()
    tr = {t.trader_id: t for t in R.traders({})}["mkt_rebound_k4"]
    ctx = Context(None, 0, 0, {}, feats, candles, {}, None)
    tr.prepare(ctx)
    st = tr.st
    assert st.loc[T0 + 6 * D, "dn"] == 4
    items = [(i, feats[i], feats[i].index.to_numpy(np.int64)) for i in candles]
    sigs = tr.signals_batch(ctx, items)
    assert {s.bar_ts for s in sigs} == {T0 + 7 * D - BAR}
    got = sorted(s.inst_id for s in sigs)
    assert got == sorted(f"C{i:02d}-USDT-SWAP" for i in range(5, 25))
    assert all(s.side == "long" and s.exit_rule == "3d_sl20pct" for s in sigs)
    k3 = {t.trader_id: t for t in R.traders({})}["mkt_rebound_k3"]
    k3.prepare(ctx)
    assert {s.bar_ts for s in k3.signals_batch(ctx, items)} == {T0 + 6 * D - BAR}


def test_young_coins_are_not_bought():
    candles, feats = market()
    for i in range(20, 25):
        feats[f"C{i:02d}-USDT-SWAP"]["age_d"] = 5.0
    tr = R.RegimeTrader(R.TRADER_SETS[0])
    ctx = Context(None, 0, 0, {}, feats, candles, {}, None)
    tr.prepare(ctx)
    sigs = tr.signals_batch(ctx, [(i, feats[i], feats[i].index.to_numpy(np.int64)) for i in candles])
    assert not any(s.inst_id in {f"C{i:02d}-USDT-SWAP" for i in range(20, 25)} for s in sigs)
    assert len(sigs) == 20
