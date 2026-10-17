import numpy as np
import pandas as pd
import pytest

from okxrev import fade, paper
from okxrev.paper import BAR, Engine

from test_paper import ctx_for, make_cfg

T0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR)


def path(pre_drop=True, pump=0.6):
    """Hourly-ish story on 5-minute bars: 31 days falling (or flat), 12h pump, then a slow fade."""
    n_pre = 31 * 288
    pre = np.linspace(100.0, 60.0 if pre_drop else 100.0, n_pre)
    base = pre[-1]
    up = np.linspace(base, base * (1 + pump), 144)
    down = np.linspace(up[-1], base, 600)
    p = np.concatenate([pre, up, down, np.full(200, base)])
    ts = T0 + np.arange(len(p), dtype=np.int64) * BAR
    return ts, p * 1.001, p * 0.999, p


def test_signal_fires_once_after_retrace_and_respects_prior_decline():
    ts, h, lo, c = path()
    s = fade.fade_signals(ts, h, lo, c, fade.PRE_MAX)
    assert len(s) == 1
    e = s[0]
    assert e["pump"] >= 0.5 and e["pre30"] <= -0.2
    assert c[e["i"]] <= e["level"] < c[e["i"] - 1]
    assert fade.fade_signals(*path(pre_drop=False), fade.PRE_MAX) == []
    assert len(fade.fade_signals(*path(pre_drop=False), None)) == 1
    assert fade.fade_signals(*path(pump=0.3), fade.PRE_MAX) == []


def test_no_lookahead():
    ts, h, lo, c = path()
    e = fade.fade_signals(ts, h, lo, c, fade.PRE_MAX)[0]
    k = e["i"] + 1
    h2, lo2, c2 = h.copy(), lo.copy(), c.copy()
    h2[k:], lo2[k:], c2[k:] = 1e6, 1e6, 1e6
    e2 = fade.fade_signals(ts, h2, lo2, c2, fade.PRE_MAX)
    assert [x["ts"] for x in e2] == [e["ts"]]
    cut = fade.fade_signals(ts[:k], h[:k], lo[:k], c[:k], fade.PRE_MAX)
    assert [x["ts"] for x in cut] == [e["ts"]]


def test_cooldown_blocks_second_pump():
    ts, h, lo, c = path()
    e = fade.fade_signals(ts, h, lo, c, None)[0]
    j = e["i"] + 300
    b = c[j]
    seg = np.concatenate([np.linspace(b, b * 1.7, 100), np.linspace(b * 1.7, b, 200)])
    c2 = np.concatenate([c[:j], seg])
    ts2 = T0 + np.arange(len(c2), dtype=np.int64) * BAR
    assert len(fade.fade_signals(ts2, c2 * 1.001, c2 * 0.999, c2, None)) == 1
    assert len(fade.fade_signals(ts2, c2 * 1.001, c2 * 0.999, c2, None, cooldown=100)) == 2


def test_old_history_supplies_prior_price():
    ts, h, lo, c = path()
    k = 20 * 288
    e = fade.fade_signals(ts[k:], h[k:], lo[k:], c[k:], fade.PRE_MAX)
    assert e == []
    hourly = slice(0, k, 12)
    e = fade.fade_signals(ts[k:], h[k:], lo[k:], c[k:], fade.PRE_MAX, old=(ts[hourly], c[hourly]))
    assert len(e) == 1


def test_suggest_sizing():
    s = fade.suggest(1.0, "7d_tp12_sl50", 10_000)
    assert s["notional"] == pytest.approx(400)
    assert s["tp_px"] == pytest.approx(0.88) and s["sl_px"] == pytest.approx(1.5)


def test_trader_in_engine_risk_sized_and_tp(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    ts, h, lo, c = path()
    g = pd.DataFrame({"open": c, "high": h, "low": lo, "close": c}, index=pd.Index(ts))
    tr = fade.FadeTrader(fade.FADE_SETS[0])
    eng = Engine(cfg, [tr], con)
    e = fade.fade_signals(ts, h, lo, c, fade.PRE_MAX)[0]
    ctx = ctx_for(cfg, con, g, int(ts[-1]), ts[e["i"] - 5:e["i"] + 3])
    eng.tick(ctx)
    r = con.execute("SELECT book, notional, exit_reason, ret_net FROM positions ORDER BY book").fetchall()
    a = next(x for x in r if x[0] == "A")
    assert a[1] == pytest.approx(1000 * 0.02 / 0.5)
    eng.tick(ctx_for(cfg, con, g, int(ts[-1]), ts[e["i"] + 3:]))
    reasons = {b: why for b, _, why, _ in con.execute("SELECT book, notional, exit_reason, ret_net FROM positions").fetchall()}
    assert reasons["C"] == "tp"


def test_live_alerts_written_once_and_switch(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    ts, h, lo, c = path()
    g = pd.DataFrame({"open": c, "high": h, "low": lo, "close": c}, index=pd.Index(ts))
    eng = Engine(cfg, [fade.FadeTrader(fade.FADE_SETS[0])], con)
    e = fade.fade_signals(ts, h, lo, c, fade.PRE_MAX)[0]
    eng.tick(ctx_for(cfg, con, g, int(ts[e["i"] + 2]), ts[e["i"] - 2:e["i"] + 3]))
    monkeypatch.setattr(fade, "account_equity", lambda cfg_: (10_000.0, "測試"))
    now = int(ts[e["i"] + 2])
    assert fade.live_alerts(cfg, con, now) == 1
    assert fade.live_alerts(cfg, con, now + BAR) == 0
    det = con.execute("SELECT kind, detail FROM heat_alerts").fetchall()
    assert det[0][0] == "fade" and "400 U" in det[0][1] and "不會自動下單" in det[0][1]
    con.execute("DELETE FROM heat_alerts")
    off = cfg.copy_with({"paper": {"fade": {"alerts": False}}})
    assert fade.live_alerts(off, con, now) == 0


def test_build_includes_fade_and_random_controls():
    from okxrev import traders as T
    from okxrev.config import load_config
    ids = {t.trader_id for t in T.build(load_config())}
    assert {"fade_pump", "fade_pump_sl30", "fade_pump_anypre", "random_short_7d_tp12_sl50"} <= ids
