import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from okxrev import custom_rules as CR
from okxrev import db, events_lab as L, fine
from okxrev.config import Paths, load_config

BAR = db.BAR_MS
T0 = 1_760_000_000_000 - (1_760_000_000_000 % 86_400_000)


def spike(n=3000, at=1500, rise=0.6, up_bars=60, crash=0.35, down_bars=6, price=100.0, t0=T0, vol=1000.0, seed=0):
    """Flat series with one pump (linear ramp over up_bars) then a crash over down_bars, then drift."""
    rng = np.random.default_rng(seed)
    c = price * (1 + rng.normal(0, 0.001, n)).cumprod()
    base = c[at - up_bars]
    c[at - up_bars:at + 1] = np.linspace(base, base * (1 + rise), up_bars + 1)
    top = c[at]
    c[at + 1:at + 1 + down_bars] = np.linspace(top * (1 - crash / down_bars), top * (1 - crash), down_bars)
    c[at + 1 + down_bars:] = top * (1 - crash) * (1 + rng.normal(0, 0.001, n - at - 1 - down_bars)).cumprod()
    o = np.r_[c[0], c[:-1]]
    vq = np.full(n, vol)
    vq[at - 5:at + down_bars] = vol * 8
    return pd.DataFrame({"ts": t0 + np.arange(n, dtype=np.int64) * BAR, "open": o, "high": np.maximum(o, c) * 1.001,
                         "low": np.minimum(o, c) * 0.999, "close": c, "vol": 1.0, "vol_ccy": 1.0, "vol_quote": vq})


@pytest.fixture()
def cfg(tmp_path):
    c = load_config().copy_with({"replay": {"db": str(tmp_path / "old.duckdb")}, "paper": {"db": str(tmp_path / "paper.duckdb")},
                                 "lab": {"similar_k": 50, "confirm_n": 0}})
    c.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    (tmp_path / "r").mkdir()
    with db.open_db(c.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": i} for i in ("BTC-USDT-SWAP", "AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP")])
        db.write_batch(con, "BTC-USDT-SWAP", spike(rise=0.0, crash=0.0, price=50000.0, seed=9))
        db.write_batch(con, "AAA-USDT-SWAP", spike(at=1500, seed=1))
        db.write_batch(con, "BBB-USDT-SWAP", spike(at=2200, rise=0.5, crash=0.3, seed=2))
        db.write_batch(con, "CCC-USDT-SWAP", spike(at=900, rise=0.0, crash=0.0, seed=3))
    old = tmp_path / "old.duckdb"
    with db.open_db(old) as con:
        db.upsert_instruments(con, [{"inst_id": "BTC-USDT-SWAP"}, {"inst_id": "AAA-USDT-SWAP"}])
        db.write_batch(con, "BTC-USDT-SWAP", spike(rise=0.0, crash=0.0, price=50000.0, t0=T0 - 400 * 86_400_000, seed=8))
        db.write_batch(con, "AAA-USDT-SWAP", spike(at=1000, t0=T0 - 400 * 86_400_000, seed=4))
    return c


def _arrays(df):
    return df["ts"].to_numpy(np.int64), *(df[k].to_numpy(float) for k in ("high", "low", "close"))


def test_shape_matches_hand_calc():
    df = spike(at=1500, rise=0.6, up_bars=60, crash=0.35, down_bars=6)
    ts, h, lo, c = _arrays(df)
    i = 1503
    sh = L.shape_at(ts, h, lo, c, np.array([i]))
    pk_i = int(np.argmax(h[i - L.PEAK_BARS + 1:i + 1])) + i - L.PEAK_BARS + 1
    lo_i = int(np.argmin(lo[pk_i - L.BASE_BARS + 1:pk_i + 1])) + pk_i - L.BASE_BARS + 1
    assert sh["rise"][0] == pytest.approx(h[pk_i] / lo[lo_i] - 1)
    assert sh["drop"][0] == pytest.approx(1 - c[i] / h[pk_i])
    assert sh["drop_bars"][0] == i - pk_i and sh["rise_bars"][0] == pk_i - lo_i
    assert 0.5 < sh["rise"][0] < 0.7 and pk_i in (1500, 1501)


def test_prefilter_is_superset():
    df = spike()
    ts, h, lo, c = _arrays(df)
    allidx = np.arange(L.MIN_HISTORY, len(c))
    sh = L.shape_at(ts, h, lo, c, allidx)
    exact = set(allidx[(sh["rise"] >= 0.2) & (sh["drop"] >= 0.1)])
    pre = set(L.prefilter(h, lo, c, 0.2, 0.1))
    assert exact and exact <= pre


def test_template_no_lookahead():
    """Changing bars after the trigger never changes whether / where the template fires."""
    df = spike()
    t = L.norm_template({"rise_min": 0.4, "drop_min": 0.1, "drop_bars_max": 12, "rise_bars_max": 288})

    def fires(d):
        ts, h, lo, c = _arrays(d)
        idx = L.prefilter(h, lo, c, t["rise_min"], t["drop_min"])
        sh = L.shape_at(ts, h, lo, c, idx)
        cols = {**sh, **{k: v[idx] for k, v in L.context_cols(d.set_index("ts")).items()}, "age_d": np.full(len(idx), np.inf), "btc": np.full(len(idx), "neutral")}
        m = L.mask(t, cols)
        return ts[idx][m]
    a = fires(df)
    assert len(a)
    first = int(np.searchsorted(df["ts"].to_numpy(), a[0]))
    d2 = df.copy()
    d2.loc[first + 1:, ["open", "high", "low", "close"]] *= 3.0
    b = fires(d2)
    assert b[0] == a[0]


def test_fresh_hits_cooldown():
    inst = np.array([0, 0, 0, 0, 1, 1])
    ts = np.array([0, 1, 2, 100, 0, 1]) * BAR
    hit = np.array([True, True, False, True, True, True])
    assert L.fresh_hits(inst, ts, hit, 10).tolist() == [0, 3, 4]


def test_outcomes_short_long_and_stop():
    n = 400
    o = np.full(n, 100.0)
    c = np.full(n, 100.0)
    c[20:] = 90.0
    o[21:] = 90.0
    h = np.maximum(o, c) + 0.1
    lo = np.minimum(o, c) - 0.1
    atr = np.full(n, 0.01)
    r = L.outcomes_at(o, h, lo, c, atr, np.array([10]), cost=0.0)
    assert r["S_1h"][0] == pytest.approx(0.1) and r["L_1h"][0] == pytest.approx(-0.1)
    assert r["L_6h_sl2atr"][0] == pytest.approx(-0.02)
    assert r["S_6h_sl2atr"][0] == pytest.approx(0.1)


def test_build_index_and_template_test(cfg):
    m = L.build_index(cfg)
    assert m["rows"] > 0 and m["by_src"]["main"] > 0 and m["by_src"]["old"] > 0
    t = {"rise_min": 0.4, "drop_min": 0.15, "drop_bars_max": 12, "rise_bars_max": 288}
    r = L.test_template(cfg, t)
    assert r["n"] >= 2 and not r["blind_shown"] and r["n_by_period"]["blind"] >= 1
    assert all("blind" not in c for c in r["combos"])
    assert all(e["period"] != "blind" for e in r["examples"])
    insts = {e["inst"] for e in r["examples"]}
    assert insts <= {"AAA-USDT-SWAP", "BBB-USDT-SWAP"}
    rb = L.test_template(cfg, t, blind=True)
    assert rb["blind_shown"] and any("blind" in c for c in rb["combos"])
    assert len(L.blind_log(cfg)) == 1
    again = L.test_template(cfg, t)
    assert again["blind_shown"] and again["blind_done_before"] and len(L.blind_log(cfg)) == 1


def test_periods_do_not_overlap(cfg):
    L.build_index(cfg)
    idx, _, _ = L.load_index(cfg)
    split = L.periods(cfg, idx)["split"]
    per = L.period_of(idx["src"].to_numpy(), idx["ts"].to_numpy(np.int64), split)
    main = idx["src"].astype(str).to_numpy() == "main"
    assert set(per[~main]) <= {"blind"} and "blind" not in set(per[main])
    assert (idx["ts"].to_numpy()[per == "disc"] < split).all() and (idx["ts"].to_numpy()[per == "valid"] >= split).all()


def test_analyze_and_similar_find_known_event(cfg):
    L.build_index(cfg)
    at = T0 + 1500 * BAR
    a = L.analyze_event(cfg, "AAA-USDT-SWAP", at, with_fine=False)
    assert abs(a["bars"]["peak"] - at) <= 2 * BAR
    assert 0.5 < a["shape"]["rise"] < 0.7 and a["shape"]["drop_total"] > 0.3
    assert a["bars"]["trigger"] > a["bars"]["peak"]
    t = a["template"]
    assert t["rise_min"] <= a["shape"]["rise"] and t["drop_min"] <= a["shape"]["drop_total"]
    s = L.find_similar(cfg, a)
    found = {(r["inst"], r["period"]) for r in s["rows"]}
    assert ("BBB-USDT-SWAP", "disc") in found or ("BBB-USDT-SWAP", "valid") in found
    assert not any(r["inst"] == "AAA-USDT-SWAP" and abs(r["ts"] - at) < 2 * 86_400_000 for r in s["rows"])
    assert all(r["period"] != "blind" for r in s["rows"])
    sb = L.find_similar(cfg, a, include_blind=True)
    assert any(r["period"] == "blind" for r in sb["rows"])


def test_custom_rule_trader_fires_once(cfg):
    from okxrev.derivs import attach
    from okxrev.detector import btc_features, prepare
    from okxrev.discover import features
    L.build_index(cfg)
    r = CR.add(cfg, "測試", {"rise_min": 0.4, "drop_min": 0.15, "drop_bars_max": 12, "rise_bars_max": 288}, "short", "6h")
    assert r["result"]["verdict"] and CR.load(cfg)[0]["id"] == r["id"]
    tr = CR.traders(cfg)
    assert len(tr) == 1 and tr[0].trader_id == f"lab_{r['id']}" and ("未盲測" in tr[0].label or "待盲測" in tr[0].label)
    raw = spike(at=1500, seed=1)
    btc = btc_features(spike(rise=0.0, crash=0.0, price=50000.0, seed=9), cfg.btc_regime)
    g = prepare(raw, cfg.detector, btc)
    f = features(g, cfg.detector, None, None, attach(g, pd.DataFrame(columns=["ts", "rate"]), pd.DataFrame(columns=["period", "ts", "oi_usd"])))

    class Ctx:
        candles = {"AAA-USDT-SWAP": g[["open", "high", "low", "close"]]}
    bars = g.index.to_numpy(np.int64)
    batch = tr[0].signals(Ctx, "AAA-USDT-SWAP", f, bars)
    assert len(batch) == 1 and T0 + 1500 * BAR < batch[0].bar_ts <= T0 + 1512 * BAR and batch[0].side == "short"
    step = []
    for k in range(1400, 1700, 12):
        step += tr[0].signals(Ctx, "AAA-USDT-SWAP", f, bars[k:k + 12])
    assert [s.bar_ts for s in step] == [batch[0].bar_ts]
    import okxrev.traders as T
    assert any(t.trader_id == f"lab_{r['id']}" for t in T.build(cfg))
    CR.update(cfg, r["id"], active=False)
    assert CR.traders(cfg) == []
    CR.delete(cfg, r["id"])
    assert CR.load(cfg) == []


def test_fine_paging_dedup_and_cache(cfg, monkeypatch):
    now = db.now_ms()
    end = (now // 60_000) * 60_000 - 10 * 3_600_000
    start = end - 400 * 60_000
    calls = []

    class FakeClient:
        def get(self, path, params):
            calls.append((path, dict(params)))
            if path.endswith("/market/candles"):
                return []
            after = int(params["after"])
            rows = []
            for k in range(1, 101):
                t = after - k * 60_000
                rows.append([str(t), "1", "2", "0.5", "1.5", "10", "10", "15", "1"])
            return rows

        def close(self):
            pass
    df = fine.candles(cfg, "AAA-USDT-SWAP", "1m", start, end, client=FakeClient(), include_live=False)
    assert len(df) == 400 and df["ts"].is_unique and df["ts"].min() == start and df["ts"].max() == end - 60_000
    assert (np.diff(df["ts"].to_numpy()) == 60_000).all()
    n1 = len(calls)
    fine._mem.clear()
    df2 = fine.candles(cfg, "AAA-USDT-SWAP", "1m", start + 60 * 60_000, end - 60 * 60_000, client=FakeClient(), include_live=False)
    assert len(calls) == n1 and len(df2) == 280
    with pytest.raises(fine.RangeError):
        fine.candles(cfg, "AAA-USDT-SWAP", "1s", start, start + 3 * 3_600_000, client=FakeClient())


def test_micro_and_summary():
    n = 30
    ts = 1_700_000_000_000 + np.arange(n) * 60_000
    c = np.r_[np.full(25, 100.0), [99, 97, 95, 93, 92]]
    df = pd.DataFrame({"ts": ts, "open": np.r_[c[0], c[:-1]], "high": np.r_[c[0], c[:-1]] * 1.001, "low": c * 0.999, "close": c,
                       "vol": 1.0, "vol_ccy": 1.0, "vol_quote": np.r_[np.full(25, 100.0), np.full(5, 1000.0)]})
    m = fine.micro(df, "1m")
    w = m["windows"]["5m"]
    assert w["change"] == pytest.approx(92 / 100 - 1, rel=1e-6) and w["vol_ratio"] > 3 and w["streak"] < 0
    assert "帶量下殺" in fine.summary_line(m)
    tr = pd.DataFrame({"ts": ts[:10], "px": 1.0, "sz": [1, 1, 1, 1, 1, 1, 1, 1, 1, 100], "side": ["sell"] * 9 + ["buy"]})
    f = fine.trade_flow(tr, big_mult=5)
    assert f["big_n"] == 1 and f["big_buy"] == 1 and f["buy_share_notional"] == pytest.approx(100 / 109)


def test_lab_web_endpoints(cfg):
    from okxrev.web.app import create_app
    L.build_index(cfg)
    c = TestClient(create_app(cfg))
    h = {"x-okxrev": "1"}
    s = c.get("/api/lab/status").json()
    assert s["index"]["exists"] and "6h" in s["exits"]
    a = c.get(f"/api/lab/analyze?inst=AAA&at={T0 + 1500 * BAR}")
    assert a.status_code == 200 and a.json()["shape"]["rise"] > 0.5
    assert c.get("/api/lab/analyze?inst=../x&at=1").status_code == 400
    r = c.post("/api/lab/test", json={"template": a.json()["template"]}, headers=h).json()
    assert r["n"] >= 1 and not r["blind_shown"]
    assert c.post("/api/lab/test", json={"template": {"rise_min": "abc"}}, headers=h).status_code == 400
    assert c.post("/api/lab/test", json={"template": {}}).status_code == 403
    sv = c.post("/api/lab/rules", json={"name": "x", "template": a.json()["template"], "side": "short", "exit": "6h"}, headers=h).json()
    assert sv["ok"]
    assert c.post("/api/lab/rules", json={"name": "x", "template": {}, "side": "up", "exit": "6h"}, headers=h).status_code == 400
    rules = c.get("/api/lab/rules").json()
    assert len(rules) == 1 and "verdict_label" in rules[0] and rules[0]["live"]["triggers"] == 0
    rid = rules[0]["id"]
    assert c.post("/api/lab/rules/op", json={"id": rid, "op": "pause"}, headers=h).json()["ok"]
    assert c.post("/api/lab/rules/op", json={"id": "../../x", "op": "delete"}, headers=h).status_code == 400
    assert c.post("/api/lab/rules/op", json={"id": rid, "op": "delete"}, headers=h).json()["ok"]
    assert c.get("/api/lab/recent").status_code == 200


def test_chat_lab_tools(cfg):
    from okxrev import chat_tools as T
    L.build_index(cfg)
    box = T.Toolbox(cfg, allow_live=False)
    txt, ok = T.call(box, "analyze_event", {"inst": "AAA", "at": T0 + 1500 * BAR})
    d = json.loads(txt)
    assert ok and d["times"]["peak"].endswith("UTC") and "template" in d
    txt, ok = T.call(box, "test_template", {"template": d["template"]})
    assert ok and "blind_" not in "".join(json.loads(txt)["combos"][0].keys())
    txt, ok = T.call(box, "find_similar_events", {"inst": "AAA", "at": T0 + 1500 * BAR, "k": 10})
    assert ok and json.loads(txt)["n"] >= 1
    txt, ok = T.call(box, "custom_rules", {})
    assert ok and json.loads(txt)["n"] == 0
    txt, ok = T.call(box, "recent_trades", {"inst": "AAA"})
    assert not ok
