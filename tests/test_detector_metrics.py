import numpy as np
import pandas as pd
import pytest

from okxrev.config import load_config
from okxrev.detector import BAR_MS, apply_cooldown, btc_features, detect, prepare, to_grid
from okxrev.metrics import bootstrap_ci, event_metrics, summarize

CFG = load_config()
DET = CFG.detector
MET = CFG.metrics
REG = CFG.btc_regime

N = 1400
EV = 700


def flat(n=N, price=100.0, vol=1000.0, t0=1_700_000_000_000):
    ts = t0 + np.arange(n, dtype=np.int64) * BAR_MS
    return pd.DataFrame({
        "ts": ts, "open": price, "high": price * 1.001, "low": price * 0.999, "close": price,
        "vol": 1.0, "vol_ccy": 1.0, "vol_quote": vol,
    })


def pumped(n=N, ev=EV):
    df = flat(n)
    c = np.full(n, 100.0)
    ramp = np.arange(ev - 200, ev)
    c[ramp] = np.linspace(100, 200, len(ramp))
    c[ev:] = 180.0
    c[ev + 1:] = 180.0
    df["close"] = c
    df["open"] = np.r_[c[0], c[:-1]]
    df.loc[ev, "open"] = 200.0
    df["high"] = np.maximum(df["open"], df["close"]) * 1.001
    df["low"] = np.minimum(df["open"], df["close"]) * 0.999
    df.loc[ev - 50, "vol_quote"] = 10000.0
    df.loc[ev, "vol_quote"] = 10000.0
    return df


def btc_df(n=N, weak=False):
    df = flat(n, price=50000.0)
    if weak:
        c = np.full(n, 50000.0)
        c[EV - 48:EV + 1] = np.linspace(50000, 49000, 49)
        c[EV + 1:] = 49000
        df["close"] = c
    return df


def test_detect_event_position():
    g = prepare(pumped(), DET, btc_features(btc_df(), REG))
    ev = detect(g, DET)
    assert ev["red_only"].tolist() == [EV]
    assert ev["pump_red"].tolist() == [EV]
    assert ev["pump_red_btcweak"].tolist() == []
    assert g["ret_1"].iloc[EV] == pytest.approx(-0.1)
    assert g["vol_ratio"].iloc[EV] == pytest.approx(10.0)


def test_btc_weak_mode():
    g = prepare(pumped(), DET, btc_features(btc_df(weak=True), REG))
    assert g["btc_regime"].iloc[EV] == "weak"
    assert detect(g, DET)["pump_red_btcweak"].tolist() == [EV]


def test_no_lookahead_future_changes():
    base = pumped()
    g1 = prepare(base, DET, btc_features(btc_df(), REG))
    alt = base.copy()
    alt.loc[EV + 1:, ["open", "high", "low", "close"]] = 5.0
    alt.loc[EV + 1:, "vol_quote"] = 1e9
    g2 = prepare(alt, DET, btc_features(btc_df(), REG))
    cols = ["ret_1", "vol_ma", "vol_ratio", "ret_6h_pre", "ret_24h_pre", "pump_vol_ratio_max", "btc_regime"]
    pd.testing.assert_frame_equal(g1.iloc[: EV + 1][cols], g2.iloc[: EV + 1][cols])
    assert detect(g1, DET)["pump_red"].tolist() == detect(g2, DET)["pump_red"].tolist()


def test_vol_ma_excludes_current_bar():
    df = flat()
    df.loc[500, "vol_quote"] = 21000.0
    g = prepare(df, DET)
    assert g["vol_ma"].iloc[500] == pytest.approx(1000.0)
    assert g["vol_ratio"].iloc[500] == pytest.approx(21.0)
    assert g["vol_ma"].iloc[501] == pytest.approx(2000.0)


def test_pump_uses_prev_close_only():
    df = flat()
    df.loc[EV, "close"] = 1000.0
    g = prepare(df, DET)
    assert g["ret_24h_pre"].iloc[EV] == pytest.approx(0.0)
    assert g["ret_24h_pre"].iloc[EV + 1] == pytest.approx(9.0)


def test_gap_blocks_detection():
    df = pumped()
    df = df.drop(index=[EV - 10]).reset_index(drop=True)
    g = prepare(df, DET)
    assert np.isnan(g["ret_24h_pre"].iloc[EV])
    assert EV not in detect(g, DET)["pump_red"].tolist()


def test_cooldown_sequential():
    m = np.zeros(1000, bool)
    m[[10, 100, 299, 300, 600, 700]] = True
    assert apply_cooldown(m, 288).tolist() == [10, 299, 600]


def test_event_metrics_hand_calc():
    df = pumped()
    df.loc[EV + 1, "open"] = 180.0
    df.loc[EV + 6, "close"] = 171.0
    df.loc[EV + 12, "close"] = 162.0
    df.loc[EV + 50, "high"] = 198.0
    df.loc[EV + 100, "low"] = 90.0
    df.loc[EV + 288, "close"] = 144.0
    g = prepare(df, DET, btc_features(btc_df(), REG))
    m = event_metrics(g, np.array([EV]), MET).iloc[0]
    assert m["entry_price"] == 180.0
    assert m["fwd_ret_30m"] == pytest.approx(171 / 180 - 1)
    assert m["fwd_ret_1h"] == pytest.approx(162 / 180 - 1)
    assert m["fwd_ret_24h"] == pytest.approx(144 / 180 - 1)
    assert m["mae_24h"] == pytest.approx(198 / 180 - 1)
    assert m["mfe_24h"] == pytest.approx(90 / 180 - 1)
    assert m["mae_before_mfe"] == True  # noqa: E712
    assert m["bars_to_mfe"] == 100
    assert m["new_high_24h"] == False  # noqa: E712
    assert m["short_pnl_net_24h"] == pytest.approx(-(144 / 180 - 1) - 0.002)
    assert m["red_ret_prev_close"] == pytest.approx(-0.1)
    assert m["red_ret_body"] == pytest.approx(-0.1)
    assert not m["incomplete"]


def test_event_metrics_entry_missing_incomplete():
    df = pumped().iloc[: EV + 1]
    g = prepare(df, DET)
    m = event_metrics(g, np.array([EV]), MET).iloc[0]
    assert m["incomplete"]
    assert np.isnan(m["fwd_ret_30m"])


def test_new_high_detected():
    df = pumped()
    df.loc[EV + 20, "high"] = 250.0
    g = prepare(df, DET)
    assert event_metrics(g, np.array([EV]), MET).iloc[0]["new_high_24h"] == True  # noqa: E712


def test_btc_regime_hand_calc():
    b = btc_features(btc_df(weak=True), REG)
    t0 = b.index[EV]
    assert b.loc[t0, "btc_ret_4h"] == pytest.approx(49000 / 50000 - 1)
    assert b.loc[t0, "btc_regime"] == "weak"
    assert bool(b.loc[t0, "btc_below_ema"]) is True
    assert b["btc_regime"].iloc[10] == "unknown"


def test_btc_ffill_limit():
    df = btc_df()
    df = df.drop(index=range(500, 505)).reset_index(drop=True)
    b = btc_features(df, REG)
    grid_ts = to_grid(btc_df()).index
    assert not np.isnan(b.loc[grid_ts[502], "btc_close"])
    assert np.isnan(b.loc[grid_ts[503], "btc_close"])


def test_new_listing_pump():
    df = pumped(n=600, ev=300)
    g = prepare(df, DET, list_time=int(df["ts"].iloc[0]))
    assert bool(g["is_new_listing"].iloc[200])
    assert g["ret_24h_pre"].iloc[200] == pytest.approx(g["close"].iloc[199] / g["open"].iloc[0] - 1)
    assert not bool(g["is_new_listing"].iloc[50])


def test_summary_and_bootstrap():
    rng = np.random.default_rng(0)
    x = rng.normal(-0.1, 0.05, 500)
    lo, hi = bootstrap_ci(x, 500, 1)
    assert lo < x.mean() < hi
    ev = pd.DataFrame({
        "inst_id": ["A"] * 500, "event_ts": np.arange(500) * 86_400_000,
        "incomplete": False, "btc_regime": "neutral",
        "mae_24h": 0.1, "mfe_24h": -0.2, "mae_before_mfe": True, "new_high_24h": False,
    })
    for h in MET["horizons"]:
        ev[f"fwd_ret_{h}"] = x
        ev[f"short_pnl_net_{h}"] = -x - 0.002
    s = summarize(ev, MET, {"iterations": 200, "seed": 1})
    assert s["horizons"]["24h"]["n"] == 500
    assert s["horizons"]["24h"]["bear_rate"] > 0.9
    assert "neutral" in s["by_btc_regime"]
