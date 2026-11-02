import numpy as np
import pandas as pd
import pytest

from okxrev import exit_lab, flow, listings, margin, xsec
from okxrev.paper import BAR, Context, Signal, Trader

D = 86_400_000
H = 3_600_000
T0 = 1_780_000_000_000 - (1_780_000_000_000 % D)


# ---------------------------------------------------------------- cross-section
def daily(n=40, days=120, mom=0.0, seed=0):
    """Daily closes where yesterday's winners keep (mom > 0) or give back (mom < 0) part of their move."""
    rng = np.random.default_rng(seed)
    r = np.zeros((days, n))
    r[0] = rng.normal(0, 0.03, n)
    for t in range(1, days):
        r[t] = mom * r[t - 1] + rng.normal(0, 0.03, n)
    C = pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=T0 + np.arange(days) * D, columns=[f"C{i:02d}-USDT-SWAP" for i in range(n)])
    C.attrs["list_time"] = {c: T0 - 100 * D for c in C.columns}
    V = pd.DataFrame(1e6, index=C.index, columns=C.columns)
    return C, V


def test_xsec_detects_momentum_and_reversal():
    for mom, sign in ((0.5, 1), (-0.5, -1)):
        C, V = daily(mom=mom)
        P = xsec.portfolios(C, V, 1, 1)
        st = xsec.seg_stats(P, 1, 0.0)
        assert np.sign(st["spread_gross"]) == sign and abs(st["spread_t"]) > 3
        assert st["spread_rev"] == pytest.approx(-st["spread"], abs=1e-12)


def test_xsec_costs_reduce_both_sides():
    C, V = daily(mom=0.0)
    P = xsec.portfolios(C, V, 1, 1)
    a, b = xsec.seg_stats(P, 1, 0.0), xsec.seg_stats(P, 1, 0.002)
    assert b["spread"] == pytest.approx(a["spread"] - 0.004)
    assert b["spread_rev"] == pytest.approx(a["spread_rev"] - 0.004)


# ---------------------------------------------------------------- flow / premium
def test_flow_zscore_baseline_excludes_the_current_day():
    x = pd.DataFrame({"a": np.r_[np.zeros(800), np.full(24, 5.0)]})
    z = flow.zscore(x + np.random.default_rng(0).normal(0, 0.1, (824, 1)))
    assert z["a"].iloc[-1] > 20


def test_flow_event_stats_beats_control():
    idx = T0 + np.arange(2000) * H
    f = pd.DataFrame({"a": np.where(np.arange(2000) % 100 == 0, -0.05, 0.0)}, index=idx)
    mask = pd.DataFrame({"a": np.arange(2000) % 100 == 0}, index=idx)
    ctrl = pd.DataFrame({"a": np.arange(2000) % 10 == 0}, index=idx)
    st = flow.event_stats({"f6": f}, mask, ctrl, -1, 6, 0.0, int(idx[0]), int(idx[-1]) + H)
    assert st["n"] == 20 and st["mean"] == pytest.approx(0.05) and st["ctrl_mean"] < st["mean"]


def test_flow_cooldown():
    m = pd.DataFrame({"a": [True, True, False, True] + [False] * 30 + [True]})
    assert flow.cooldown(m, 24)["a"].tolist().count(True) == 2


# ---------------------------------------------------------------- listings
@pytest.mark.parametrize("typ,title,kind,toks", [
    ("announcements-new-listings", "OKX to list SANDUSD and PARTIUSD X-Perp", "xperp", ["SAND", "PARTI"]),
    ("announcements-new-listings", "OKX to list PENGUSD, US100USD and NFLXUSD Equity X-Perps", None, []),
    ("announcements-new-listings", "OKX to list CT/USDT (Concrete) for spot trading", "spot_list", ["CT"]),
    ("announcements-new-listings", "OKX to list RESOLV (Resolv) for spot trading", "spot_list", ["RESOLV"]),
    ("announcements-new-listings", "OKX delays the listing of MORPHO (Morpho) for spot trading", None, []),
    ("announcements-new-listings", "OKX to list perpetual futures for MOODENG, NEIRO crypto", "perp_new", ["MOODENG", "NEIRO"]),
    ("announcements-new-listings", "OKX to list perpetual for VINE crypto, along with its margin trading and Simple Earn", "perp_new", ["VINE"]),
    ("announcements-new-listings", "OKX to list TradFi perpetual futures for MCD, JPM, V and GS", "tradfi_new", ["MCD", "JPM", "GS"]),
    ("announcements-delistings", "OKX to delist DORA, ICX, STORJ, ZEUS and ELF spot trading pairs", "spot_delist", ["DORA", "ICX", "STORJ", "ZEUS", "ELF"]),
    ("announcements-delistings", "OKX to delist perpetual futures for ONEUSDT", "perp_delist", ["ONE"]),
    ("announcements-delistings", "OKX to postpone the delisting of ONEUSDT perpetual futures", None, []),
])
def test_listing_classify(typ, title, kind, toks):
    k, t = listings.classify(typ, title)
    assert k == kind and t == toks


# ---------------------------------------------------------------- exits
def test_presets_run_and_time_matches_close():
    n, hold = 4, 10
    cls = np.tile(np.linspace(0.001, 0.02, hold), (n, 1))
    P = {"fav": cls + 0.001, "adv": np.full((n, hold), 0.001), "cls": cls, "atr": np.full(n, 0.002), "struct": np.full(n, 0.05),
         "ok": np.ones(n, bool), "last": np.full(n, hold - 1)}
    res = exit_lab.run_presets(P, hold, 0.0, 0.0)
    assert set(res) == set(exit_lab.PRESETS)
    assert res["time"] == pytest.approx(np.full(n, 0.02))
    r = 0.002 * np.sqrt(hold)
    assert res["tp1_sl075"] == pytest.approx(np.full(n, r))     # the target (1R) is reached before the time limit


class Base(Trader):
    kind, trader_id, label, exit = "rule", "rule99_6h", "base", "6h"

    def signals_batch(self, ctx, items):
        return [Signal(self.trader_id, "AAA", int(T0), "long", "6h", 1.0, "x")]


def test_exit_variant_sets_per_trade_stop_and_target():
    f = pd.DataFrame({"atr": [0.004], "turnover24": [1e8]}, index=[T0])
    ctx = Context(None, T0, T0, {}, {"AAA": f}, {}, {}, None)
    v = exit_lab.ExitVariant(Base(), "tp15_sl1", 72)
    s = v.signals_batch(ctx, [])[0]
    r = 0.004 * np.sqrt(72)
    assert s.trader_id == "rule99_6h_hx" and s.features["_hold_bars"] == 72
    assert s.features["_stop_frac"] == pytest.approx(r) and s.features["_tp_frac"] == pytest.approx(1.5 * r)


# ---------------------------------------------------------------- margin
def pos(rets, maes, side="long", gap=H):
    n = len(rets)
    return pd.DataFrame({"trader_id": "t", "inst_id": "AAA", "side": side, "entry_ts": T0 + np.arange(n) * gap, "exit_ts": T0 + np.arange(n) * gap + gap // 2,
                         "entry_px": 100.0, "ret_net": rets, "mae": maes})


def test_isolated_liquidates_on_mae_and_loses_only_margin():
    p = pos([0.02, 0.02, 0.02], [0.01, 0.06, 0.01])
    r1 = margin.isolated(p, 1)
    r20 = margin.isolated(p, 20)
    assert r1["liquidations"] == 0 and r20["liquidations"] == 1
    f = margin.MARGIN_FRAC
    eq = margin.EQ0
    eq += eq * f * 20 * 0.02          # trade 1: +2% x 20
    eq -= eq * f                      # trade 2: liquidated, loses its margin
    eq += eq * f * 20 * 0.02          # trade 3
    assert r20["ret"] == pytest.approx(eq / margin.EQ0 - 1)


def test_cross_blows_up_on_a_large_adverse_hour():
    p = pos([0.01] * 10, [0.3] * 10, gap=0)
    hrs = T0 + np.arange(3) * H
    px = {"AAA": pd.DataFrame({"hi": 100.0, "lo": [100.0, 60.0, 100.0], "c": 100.0}, index=hrs)}
    p["exit_ts"] = T0 + 2 * H
    r = margin.cross(p, 20, px)
    assert r["blown_at"] is not None and r["ret"] == pytest.approx(-1.0)
    ok = margin.cross(p, 1, px)
    assert ok["blown_at"] is None


def test_safe_lever_from_mae():
    assert margin.safe_lever(np.full(100, 0.095)) == 10.0
    assert margin.safe_lever(np.full(100, 0.6)) == 1.0
