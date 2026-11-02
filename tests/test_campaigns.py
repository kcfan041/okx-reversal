import numpy as np
import pandas as pd
import pytest

from okxrev import announce, campaigns as CP, db, paper
from okxrev.config import Paths
from okxrev.paper import BAR, Context, Engine

from test_paper import make_cfg

H, D = CP.H, CP.D
T0 = 1_780_000_000_000 - (1_780_000_000_000 % D)


def write_coins(cfg, n=24, days=100, token_list_day=30, drop=(60, 65, 0.25), seed=0):
    """Hourly candles: n control coins listed at day 0, TOK listed at `token_list_day` that loses `drop[2]` (log)
    between days drop[0] and drop[1], LATE listed at day 70 (too young / not yet listed for early windows)."""
    rng = np.random.default_rng(seed)
    t = T0 + np.arange(days * 24, dtype=np.int64) * H
    frames = {}
    for i in range(n):
        frames[f"C{i:02d}-USDT-SWAP"] = np.exp(np.cumsum(rng.normal(0, 0.002, len(t))))
    frames["BTC-USDT-SWAP"] = np.exp(np.cumsum(rng.normal(0, 0.001, len(t))))
    tok = np.exp(np.cumsum(rng.normal(0, 0.002, len(t))))
    a, b = drop[0] * 24, drop[1] * 24
    tok[a:b] *= np.exp(-drop[2] * np.arange(1, b - a + 1) / (b - a))
    tok[b:] *= np.exp(-drop[2])
    frames["TOK-USDT-SWAP"] = tok
    frames["LATE-USDT-SWAP"] = np.exp(np.cumsum(rng.normal(0, 0.002, len(t))))
    start = {"TOK-USDT-SWAP": token_list_day * 24, "LATE-USDT-SWAP": 70 * 24}
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": k} for k in frames])
        for k, p in frames.items():
            s = start.get(k, 0)
            df = pd.DataFrame({"ts": t[s:], "open": p[s:], "high": p[s:] * 1.001, "low": p[s:] * 0.999, "close": p[s:],
                               "vol": 1.0, "vol_ccy": 1.0, "vol_quote": 1e6})
            db.write_batch(con, k, df)
    return t


def add_campaign(cfg, url, token, kind, pub_day, start_day, end_day):
    con = announce.connect(cfg)
    try:
        c = {"kind": kind, "tokens": [token], "start_ts": T0 + start_day * D, "end_ts": T0 + end_day * D, "status": "ok", "pool": "1 X"}
        announce.save_campaign(con, url, f"OKX × {token} campaign share 1 {token}", T0 + pub_day * D, c, 0)
    finally:
        con.close()


@pytest.fixture()
def cfg(tmp_path):
    c = make_cfg(tmp_path)
    c.raw["announce"] = {"db": "announce.duckdb"}
    return c


def test_event_study_finds_the_drop_and_writes_results(cfg):
    write_coins(cfg)
    add_campaign(cfg, "u1", "TOK", "flash", 58, 60, 65)
    res = CP.evaluate(cfg, draws=20)
    ev = res["events"][0]
    assert ev["token"] == "TOK" and ev["grp"] == "earn"
    assert ev["dur_raw"] == pytest.approx(-0.25, abs=0.06)
    assert ev["dur_ab"] < -0.15 and abs(ev["post3_ab"]) < 0.08
    assert res["main"]["earn"]["verdict"] == "樣本不足"
    pl = res["placebo"]["earn_dur"]
    assert pl.get("n", 0) == 1
    p = CP.hourly_panel(cfg, T0)
    ev_ = CP.load_events(cfg, p)
    cv = CP.curves(p, ev_, CP.event_rows(p, ev_), min_n=1)["earn_start"]
    assert cv["mean"][cv["h"].index(0)] == 0 and cv["mean"][-1] < -0.15
    assert (cfg.paths.results / "campaigns.json").exists() and CP.load(cfg)["n_events"] == 1


def test_controls_use_age_at_window_start_and_skip_busy_coins(cfg):
    write_coins(cfg)
    add_campaign(cfg, "u1", "TOK", "flash", 58, 60, 65)
    add_campaign(cfg, "u2", "C00", "t2e", 50, 50, 55)
    p = CP.hourly_panel(cfg, T0)
    ev = CP.load_events(cfg, p)
    k = p.ci["TOK-USDT-SWAP"]
    a = T0 + 60 * D
    busy = CP.busy_mask(p, CP.ev_index(p, ev), a, T0 + 65 * D)
    m = CP.controls(p, k, a, busy)
    assert busy[p.ci["C00-USDT-SWAP"]] and not m[p.ci["C00-USDT-SWAP"]]
    assert not m[p.ci["LATE-USDT-SWAP"]]
    assert not m[p.ci["BTC-USDT-SWAP"]]
    assert m[p.ci["C01-USDT-SWAP"]]
    young = CP.controls(p, k, T0 + 31 * D, np.zeros(len(p.cols), bool))
    assert not young.any() or p.ages(T0 + 31 * D)[young].min() >= 14


def test_lret_uses_closes_at_window_edges(cfg):
    write_coins(cfg, n=6)
    p = CP.hourly_panel(cfg, T0)
    k = p.ci["C01-USDT-SWAP"]
    a, b = T0 + 10 * D, T0 + 11 * D
    i, j = p.pos(a), p.pos(b)
    assert p.idx[i] + H == a and p.idx[j] + H == b
    assert p.lret(a, b)[k] == pytest.approx(p.L[j, k] - p.L[i, k])


def test_trader_enters_at_start_and_control_picks_another_coin(cfg):
    write_coins(cfg)
    rows = pd.DataFrame([{"url": "u1", "title": "t", "token": "TOK", "inst_id": "TOK-USDT-SWAP", "kind": "flash", "grp": "earn",
                          "p_time": T0 + 58 * D, "start_ts": T0 + 60 * D, "end_ts": T0 + 65 * D}])
    CP.CampaignTrader.source = staticmethod(lambda c: rows)
    try:
        insts = ["TOK-USDT-SWAP", "C01-USDT-SWAP", "C02-USDT-SWAP"]
        sb = CP.signal_bar(T0 + 60 * D)
        assert sb + BAR == T0 + 60 * D
        idx = pd.Index(T0 + 60 * D + np.arange(-12, 4) * BAR)
        feats = {i: pd.DataFrame({"age_d": 30.0 if i.startswith("TOK") else 60.0, "turnover24": 1e8}, index=idx) for i in insts}
        cand = {i: pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=idx) for i in insts}
        con = paper.connect(cfg)
        ctx = Context(cfg, sb, sb, {i: np.array([sb]) for i in insts}, feats, cand, {}, con)
        trs = {t.trader_id: t for t in CP.traders(cfg, {})}
        out = {}
        for tid in ("camp_earn", "camp_earn_ctrl", "camp_t2e_post"):
            trs[tid].prepare(ctx)
            out[tid] = trs[tid].signals_batch(ctx, [(i, feats[i], np.array([sb])) for i in insts])
        s = out["camp_earn"][0]
        assert (s.inst_id, s.bar_ts, s.side) == ("TOK-USDT-SWAP", sb, "short")
        assert s.features["_hold_bars"] == 5 * 288 and s.features["_stop_frac"] == 0.2
        assert out["camp_earn_ctrl"][0].inst_id in ("C01-USDT-SWAP", "C02-USDT-SWAP")
        assert out["camp_t2e_post"] == []
        eng = Engine(cfg, [trs["camp_earn"]], con)
        eng.tick(ctx)
        pos = con.execute("SELECT hold_bars, entry_ts, stop_px, deadline_ts FROM positions WHERE book = 'C'").fetchone()
        assert pos == (5 * 288, T0 + 60 * D, pytest.approx(1.2), T0 + 65 * D - BAR)
        con.close()
    finally:
        CP.CampaignTrader.source = None


def test_alerts_for_new_campaign_and_trade_end_once(cfg):
    write_coins(cfg, n=6)
    add_campaign(cfg, "u1", "TOK", "t2e", 60, 60, 65)
    con = paper.connect(cfg)
    assert CP.live_alerts(cfg, con, T0 + 60 * D + H) == 1
    assert CP.live_alerts(cfg, con, T0 + 60 * D + 2 * H) == 0
    assert CP.live_alerts(cfg, con, T0 + 65 * D + H) == 1
    assert CP.live_alerts(cfg, con, T0 + 65 * D + 2 * H) == 0
    kinds = sorted(k for (k,) in con.execute("SELECT kind FROM heat_alerts").fetchall())
    assert kinds == ["campaign", "campaign_end"]
    con.close()


def test_live_status_return_since_start(cfg):
    write_coins(cfg)
    add_campaign(cfg, "u1", "TOK", "flash", 58, 60, 65)
    st = CP.live_status(cfg, now=T0 + 62 * D, days=60)
    assert len(st) == 1 and st[0]["phase"] == "running"
    assert st[0]["ret"] == pytest.approx(-0.1, abs=0.05) and "vs_alts" in st[0]
    ended = CP.live_status(cfg, now=T0 + 70 * D, days=60)[0]
    assert ended["phase"] == "ended" and ended["ret"] == pytest.approx(-0.25, abs=0.06)
