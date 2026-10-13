import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from okxrev import exits, live
from okxrev.exits import INF, atr_pct, neighborhood_score, param_table, simulate, split_folds

from test_account import env  # noqa: F401


def paths(fav, adv, cls):
    fav, adv, cls = (np.atleast_2d(np.asarray(x, float)) for x in (fav, adv, cls))
    last = np.where(np.isnan(cls), -1, np.arange(cls.shape[1])).max(axis=1)
    return {"fav": fav, "adv": adv, "cls": cls, "last": last}


def run(p, fee=0.0, slip=0.0, lever=1.0, scale=1.0, **kw):
    prm = {k: np.array([kw.get(k, INF)], float) for k in exits.PARAMS}
    prm["time_bars"] = np.array([kw.get("time_bars", 288)], float)
    n = p["fav"].shape[0]
    r, x = simulate(p, np.ones(n, bool), prm, np.full(n, fee), slip, np.full(n, lever), np.full(n, scale))
    return r[0], x[0]


def test_take_profit_and_fee():
    p = paths([[0.01, 0.03, 0.0]], [[0.0, 0.0, 0.0]], [[0.0, 0.02, 0.0]])
    r, x = run(p, fee=0.001, tp=0.02, sl=0.05)
    assert r[0] == pytest.approx(0.019) and x[0] == 1


def test_same_bar_counts_stop_first():
    p = paths([[0.05]], [[0.03]], [[0.0]])
    r, _ = run(p, slip=0.001, tp=0.02, sl=0.02)
    assert r[0] == pytest.approx(-0.021)


def test_time_stop_and_end_of_data():
    p = paths([[0.0, 0.0, 0.0, 0.0]], [[0.0] * 4], [[0.001, 0.002, 0.004, 0.009]])
    r, x = run(p, sl=0.05, time_bars=3)
    assert r[0] == pytest.approx(0.004) and x[0] == 2
    p2 = paths([[0.0, 0.0, np.nan]], [[0.0, 0.0, np.nan]], [[0.001, 0.006, np.nan]])
    r2, x2 = run(p2, sl=0.05)
    assert r2[0] == pytest.approx(0.006) and x2[0] == 1


def test_trailing_uses_previous_peak():
    p = paths([[0.02, 0.04, 0.0]], [[0.0, -0.02, 0.0]], [[0.0, 0.0, 0.0]])
    r, x = run(p, sl=0.05, trail_act=0.01, trail_dist=0.01)
    assert r[0] == pytest.approx(-(0.01 - 0.04)) and x[0] == 2


def test_breakeven():
    p = paths([[0.02, 0.0]], [[0.0, 0.01]], [[0.0, 0.0]])
    r, _ = run(p, fee=0.001, sl=0.03, be_trig=0.01)
    assert r[0] == pytest.approx(0.001 - 0.001)


def test_partial_then_breakeven():
    p = paths([[0.02, 0.0]], [[0.0, 0.01]], [[0.0, 0.0]])
    r, _ = run(p, sl=0.03, part_tp=0.01, trail_dist=0.05)
    assert r[0] == pytest.approx(0.5 * 0.01 + 0.5 * 0.0)


def test_liquidation_before_stop():
    p = paths([[0.0]], [[0.05]], [[0.0]])
    r, _ = run(p, lever=20, sl=0.08)
    assert r[0] == pytest.approx(-1 / 20)
    r2, _ = run(p, lever=20, sl=0.03)
    assert r2[0] == pytest.approx(-0.03)


def test_atr_scale():
    p = paths([[0.03]], [[0.0]], [[0.0]])
    r, _ = run(p, tp=2, sl=1, scale=0.01)
    assert r[0] == pytest.approx(0.02)


def test_atr_no_lookahead():
    rng = np.random.default_rng(0)
    c = 100 + rng.normal(0, 1, 50).cumsum()
    h, lo = c + 1, c - 1
    a = atr_pct(h, lo, c, 30, 100.0)
    h2 = h.copy(); h2[30:] += 50
    assert atr_pct(h2, lo, c, 30, 100.0) == a


def test_folds_split_on_gap():
    d = 86_400_000
    o = np.array([0, 1, 2, 3, 100, 101, 102, 103, 104]) * d
    f = split_folds(o)
    assert f.tolist() == [0, 0, 0, 0, 1, 1, 1, 1, 1]


def test_neighborhood_prefers_plateau():
    tab = param_table("pct")
    fixed = tab["family"] == "fixed"
    score = np.zeros(len(tab))
    spike = tab.index[fixed][0]
    score[spike] = 1.0
    plateau = [i for i in tab.index[fixed] if tab.at[i, "gidx"][0] in (3, 4, 5) and tab.at[i, "gidx"][1] in (2, 3, 4)]
    score[plateau] = 0.5
    ns = neighborhood_score(tab, score)
    assert ns[spike] < ns[plateau].max()


def test_optimize_end_to_end(env):  # noqa: F811
    from okxrev import account
    t = account.load_trades(env)
    t = pd.concat([t.assign(trade_id=t["trade_id"] + f"-{k}", open_ts=t["open_ts"] + k * 300_000,
                            close_ts=t["close_ts"] + k * 300_000) for k in range(3)], ignore_index=True)
    res = exits.optimize(env, t)
    assert res["n_trades"] == 15
    assert set(res["groups"]) == {"all"}
    g = res["groups"]["all"]
    assert g["chosen_unit"] in ("pct", "atr") and g["recommended"]["text"]
    assert "after_loss" in res["cooldown"]


class FakeLive:
    n_requests = 0

    def get_private(self, path, params):
        if path == live.POSITIONS_PATH:
            return [{"instType": "SWAP", "instId": "AAA-USDT-SWAP", "pos": "-10", "posSide": "net", "avgPx": "100",
                     "lever": "20", "liqPx": "104", "markPx": "99", "cTime": "1700000000000", "upl": "10", "notionalUsd": "990"}]
        if path == live.BALANCE_PATH:
            return [{"details": [{"ccy": "USDT", "eqUsd": "100", "cashBal": "100"},
                                 {"ccy": "SOL", "eqUsd": "220", "eq": "2", "cashBal": "0.0001", "frozenBal": "1.9999", "openAvgPx": "100"},
                                 {"ccy": "USDG", "eqUsd": "50", "eq": "50"},
                                 {"ccy": "DUST", "eqUsd": "0.1", "eq": "5"}]}]
        return []

    def candles(self, inst, bar, limit=300, after=None, before=None):
        ts = 1700000000000 + np.arange(-20, 10) * 300_000
        return pd.DataFrame({"ts": ts, "open": 100.0, "high": 101.0, "low": 98.0, "close": 100.0, "vol": 1, "vol_ccy": 1, "vol_quote": 1})

    def history_candles(self, inst, bar, limit=100, after=None, before=None):
        df = self.candles(inst, bar)
        return df[df["ts"] < after], len(df)

    def close(self):
        pass


def test_live_plan_short(env):  # noqa: F811
    plan = {"groups": {"all": {"recommended": {"rule": {"family": "fixed", "tp": 0.02, "sl": 0.05, "trail_act": None, "trail_dist": None,
                                                         "be_trig": None, "part_tp": None, "time_bars": 12},
                                                "unit": "pct", "text": "x", "mean_ret": 0.01, "win_rate": 0.6, "oos_mean": 0.005},
                               "units": {}}}, "cooldown": {}}
    pos = live.parse_positions(FakeLive().get_private(live.POSITIONS_PATH, {}))
    p = live.plan_for(pos[0], plan, FakeLive(), 1700000000000 + 90 * 60_000)
    assert p["side"] == "short" and p["sl_px"] == pytest.approx(105) and p["tp_px"] == pytest.approx(98)
    assert p["cur_ret"] == pytest.approx(0.01) and p["peak_ret"] == pytest.approx(0.02)
    assert any("強平" in w for w in p["warnings"]) and any("超過" in w for w in p["warnings"])
    spot = live.parse_spot(FakeLive().get_private(live.BALANCE_PATH, {}), pd.DataFrame())
    assert [s["inst_id"] for s in spot] == ["SOL-USDT"] and spot[0]["entry_px"] == 100


def test_spot_cost_from_fills():
    f = pd.DataFrame([
        {"inst_id": "A-USDT", "bill_id": "1", "side": "buy", "fill_px": 10.0, "fill_sz": 10.0, "fee": 0.0, "fee_ccy": "A", "ts": 1},
        {"inst_id": "A-USDT", "bill_id": "2", "side": "sell", "fill_px": 12.0, "fill_sz": 10.0, "fee": 0.0, "fee_ccy": "USDT", "ts": 2},
        {"inst_id": "A-USDT", "bill_id": "3", "side": "buy", "fill_px": 20.0, "fill_sz": 5.0, "fee": 0.0, "fee_ccy": "A", "ts": 3},
        {"inst_id": "A-USDT", "bill_id": "4", "side": "buy", "fill_px": 30.0, "fill_sz": 5.0, "fee": 0.0, "fee_ccy": "A", "ts": 4},
    ])
    avg, start = live.spot_cost_from_fills(f, "A-USDT")
    assert avg == pytest.approx(25) and start == 3


def test_web_exit_endpoints(env):  # noqa: F811
    from okxrev.web.app import create_app
    c = TestClient(create_app(env))
    assert c.get("/api/account/exit-plan").json() is None
    assert c.get("/api/account/positions").status_code == 400
