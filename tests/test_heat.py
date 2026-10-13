import duckdb
import numpy as np
import pandas as pd
import pytest

from okxrev import heat, heatdata
from okxrev.config import load_config

T0 = 1_700_000_000_000 - (1_700_000_000_000 % heat.HOUR)
BAR = 300_000


def candles(n_bars, seed=0, trend=0.0):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(trend, 0.003, n_bars)))
    o = np.r_[c[0], c[:-1]]
    idx = pd.Index(T0 + np.arange(n_bars, dtype=np.int64) * BAR, name="ts")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.001, "low": np.minimum(o, c) * 0.999, "close": c,
                         "vol_quote": rng.uniform(1e5, 2e5, n_bars)}, index=idx)


def extra_for(g, seed=1):
    rng = np.random.default_rng(seed)
    hours = np.unique((g.index.to_numpy(np.int64) // heat.HOUR) * heat.HOUR)
    tk = pd.DataFrame({"inst_id": "A", "ts": hours, "sell": rng.uniform(1, 2, len(hours)), "buy": rng.uniform(1, 2, len(hours))})
    mk = pd.DataFrame({"inst_id": "A", "ts": hours, "mark": 100 + rng.normal(0, 1, len(hours))})
    mk["idx"] = mk["mark"] * (1 - rng.normal(0, 0.001, len(hours)))
    oi = pd.DataFrame({"inst_id": "A", "period": "1H", "ts": hours, "oi_usd": rng.uniform(1e6, 2e6, len(hours))})
    fu = pd.DataFrame({"inst_id": "A", "ts": hours[::8], "rate": rng.normal(0, 1e-4, len(hours[::8]))})
    return {"taker": tk, "mark": mk, "oi": oi, "funding": fu, "ls": pd.DataFrame(), "liq": pd.DataFrame()}


def test_hourly_ohlc_drops_incomplete():
    g = candles(12 * 5 + 7)
    h = heat.hourly_ohlc(g)
    assert len(h) == 5
    first = g.iloc[:12]
    assert h.iloc[0]["open"] == first["open"].iloc[0] and h.iloc[0]["close"] == first["close"].iloc[-1]
    assert h.iloc[0]["high"] == first["high"].max() and h.iloc[0]["vq"] == pytest.approx(first["vol_quote"].sum())


def test_no_lookahead():
    g = candles(12 * 24 * 40, seed=3)
    ex = extra_for(g)
    a = heat.coin_heat(g, ex, 20)
    cut = int(a.index[-200])
    g2 = g.copy()
    g2.loc[g2.index >= cut + heat.HOUR, ["open", "high", "low", "close"]] *= 2.5
    ex2 = {k: (v.copy() if len(v) else v) for k, v in ex.items()}
    for k in ("taker", "mark", "oi"):
        late = ex2[k]["ts"] >= cut + heat.HOUR
        col = {"taker": "buy", "mark": "mark", "oi": "oi_usd"}[k]
        ex2[k].loc[late, col] *= 7
    b = heat.coin_heat(g2, ex2, 20)
    cols = ["score", *heat.DIMENSIONS, "cooling"]
    pd.testing.assert_frame_equal(a.loc[:cut, cols], b.loc[:cut, cols])


def test_percentile_and_score():
    x = pd.DataFrame({"a": [1.0, 2.0, 3.0, 2.5, 10.0]})
    p = heat.trailing_pct(x, 3, 1)
    assert p["a"].tolist() == pytest.approx([100, 100, 100, 200 / 3, 100])
    ind = pd.DataFrame({c: np.arange(200, dtype=float) for d in heat.DIMENSIONS.values() for c in d["cols"]})
    ind["ls_acct"] = np.nan
    sc = heat.score(ind, 100)
    assert sc["score"].iloc[-1] == pytest.approx(100) and sc["n_dims"].iloc[-1] == len(heat.VALIDATED)
    assert sc["stretch"].iloc[-1] == pytest.approx(100)


def test_rising_coin_scores_hot():
    g = pd.concat([candles(12 * 24 * 30, seed=4), candles(12 * 24 * 2, seed=5, trend=0.002)])
    g.index = pd.Index(T0 + np.arange(len(g), dtype=np.int64) * BAR, name="ts")
    g["close"] = np.r_[g["close"].iloc[: 12 * 24 * 30].to_numpy(),
                       g["close"].iloc[12 * 24 * 30 - 1] * np.exp(np.cumsum(np.full(12 * 24 * 2, 0.002)))]
    g["open"] = np.r_[g["close"].iloc[0], g["close"].iloc[:-1].to_numpy()]
    g["high"] = np.maximum(g["open"], g["close"]) * 1.001
    g["low"] = np.minimum(g["open"], g["close"]) * 0.999
    sc = heat.coin_heat(g, {}, 20)
    assert sc["stretch"].iloc[-1] > 90 and sc["score"].iloc[-1] > 60


def test_to_bars_waits_for_hour_close():
    h = pd.DataFrame({"score": [10.0, 20.0]}, index=pd.Index([T0, T0 + heat.HOUR]))
    bars = pd.Index(T0 + np.arange(36, dtype=np.int64) * BAR)
    b = heat.to_bars(h, bars, ["score"])
    assert np.isnan(b["score"].iloc[10])
    assert b["score"].iloc[11] == 10.0 and b["score"].iloc[22] == 10.0 and b["score"].iloc[23] == 20.0


def test_episode_starts():
    d = pd.DataFrame({"inst": "A", "ts": np.arange(80) * heat.HOUR})
    m = np.zeros(80, bool)
    m[[5, 6, 7, 20, 40, 65]] = True
    e = heat.episode_starts(d, m, gap_h=24)
    assert e["ts"].tolist() == [5 * heat.HOUR, 65 * heat.HOUR]


def test_alerts_and_cooldown(tmp_path):
    cfg = load_config().copy_with({"heat": {"alert_cooldown_h": 6}})
    rows = [{"inst_id": "CT", "score": 85, "cooling": True, "drivers": [("RSI", 99.0)]},
            {"inst_id": "X", "score": 95, "cooling": False, "drivers": []},
            {"inst_id": "Y", "score": 50, "cooling": False, "drivers": []}]
    c = heat.alerts_for(rows, {"CT", "Y"}, 80, {"CT": "觸發 rule04"})
    assert sorted(a["kind"] for a in c) == ["cooling", "hot", "rule"]
    con = duckdb.connect(str(tmp_path / "p.duckdb"))
    assert len(heat.record_alerts(cfg, con, c, 1_000 * heat.HOUR)) == 3
    assert len(heat.record_alerts(cfg, con, c, 1_000 * heat.HOUR + 5 * heat.HOUR)) == 0
    assert len(heat.record_alerts(cfg, con, c, 1_000 * heat.HOUR + 6 * heat.HOUR)) == 3


def test_tick_recomputes_once_per_hour(tmp_path, monkeypatch):
    import json
    from okxrev.config import Paths
    cfg = load_config().copy_with({"heat": {"watchlist": ["CT"], "alert_threshold": 80}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    from okxrev import db
    with db.open_db(cfg.paths.db) as con:
        pass
    calls = []

    def fake_snapshot(cfg_, full, now):
        calls.append(now)
        return {"n": 1, "rows": [{"inst_id": "CT", "score": 90, "cooling": False, "drivers": []}]}

    monkeypatch.setattr(heat, "snapshot", fake_snapshot)
    con = duckdb.connect(str(tmp_path / "p.duckdb"))
    con.execute("CREATE TABLE decisions (inst_id VARCHAR, trader_id VARCHAR, bar_ts BIGINT)")
    h0 = 1000 * heat.HOUR
    r1 = heat.tick(cfg, {}, {}, con, h0 + 60_000)
    r2 = heat.tick(cfg, {}, {}, con, h0 + 30 * 60_000)
    con.execute("INSERT INTO decisions VALUES ('CT', 'rule04_3d', ?)", [h0 + 30 * 60_000])
    r3 = heat.tick(cfg, {}, {}, con, h0 + 35 * 60_000)
    r4 = heat.tick(cfg, {}, {}, con, h0 + heat.HOUR + 60_000)
    assert len(calls) == 2 and not r1["cached"] and r2["cached"] and r3["cached"] and not r4["cached"]
    assert r1["alerts"] == 1 and r2["alerts"] == 0 and r3["alerts"] == 1
    assert json.loads((tmp_path / "r" / "heat_snapshot.json").read_text(encoding="utf-8"))["hour"] == h0 + heat.HOUR


def test_web_heat_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from okxrev.config import Paths
    from okxrev.web.app import create_app
    import json
    cfg = load_config()
    src = tmp_path / "config.yaml"
    src.write_text((cfg.source).read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(src, root=tmp_path)
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "heat_snapshot.json").write_text(json.dumps({"ts": 1, "rows": [{"inst_id": "CT-USDT-SWAP", "score": 80}]}), encoding="utf-8")
    c = TestClient(create_app(cfg))
    s = c.get("/api/heat/snapshot").json()
    assert s["rows"][0]["score"] == 80 and "stretch" in s["dims"] and s["dims"]["sentiment"]["experimental"]
    assert c.get("/api/heat/coin?inst=../x").status_code == 400
    assert c.post("/api/heat/watchlist", json={"insts": ["X"]}).status_code == 403
    r = c.post("/api/heat/watchlist", headers={"x-okxrev": "1"}, json={"insts": ["PUMP-USDT-SWAP", "CT-USDT-SWAP"]}).json()
    assert r["watchlist"] == ["PUMP-USDT-SWAP", "CT-USDT-SWAP"]
    assert load_config(src, root=tmp_path).raw["heat"]["watchlist"] == ["PUMP-USDT-SWAP", "CT-USDT-SWAP"]
    assert c.post("/api/heat/watchlist", headers={"x-okxrev": "1"}, json={"insts": ["bad;rm"]}).status_code == 400
    assert c.get("/api/heat/alerts").json() == []
    assert c.get("/api/heat/validation").json()["result"] is None


class FakeHeatClient:
    def __init__(self, oldest, newest):
        self.oldest, self.newest = oldest, newest
        self.calls = []

    def get(self, path, p):
        self.calls.append((path, dict(p)))
        if path == heatdata.TAKER_PATH:
            end = int(p["end"]) if "end" in p else self.newest
            ts = [t for t in range(end - end % heat.HOUR, self.oldest - 1, -heat.HOUR)][:100]
            return [[str(t), "1", "2"] for t in ts]
        if path in (heatdata.MARK_PATH, heatdata.INDEX_PATH):
            after = int(p["after"]) if "after" in p else self.newest + heat.HOUR
            ts = [t for t in range(after - heat.HOUR, self.oldest - 1, -heat.HOUR)][:100]
            v = "1.01" if path == heatdata.MARK_PATH else "1.0"
            return [[str(t), v, v, v, v, "1"] for t in ts]
        return []

    def close(self):
        pass


def test_backfill_splits_and_resumes(tmp_path, monkeypatch):
    from okxrev import db, replay
    from okxrev.config import Paths, parse_utc_ms
    cfg = load_config().copy_with({"heat": {"backfill_start": "2024-09-25"},
                                   "replay": {"db": str(tmp_path / "old.duckdb"), "end": "2024-10-05"}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "AAA-USDT-SWAP"}])
        db.write_batch(con, "AAA-USDT-SWAP", pd.DataFrame({"ts": [parse_utc_ms("2024-10-20")], "open": 1.0, "high": 1.0, "low": 1.0,
                                                          "close": 1.0, "vol": 1.0, "vol_ccy": 1.0, "vol_quote": 1.0}))
    with db.open_db(replay.old_db_path(cfg)) as con:
        pass
    fake = FakeHeatClient(parse_utc_ms("2024-09-20"), parse_utc_ms("2024-10-15"))
    monkeypatch.setattr(heatdata, "OkxClient", lambda *a, **k: fake)
    r = heatdata.backfill(cfg)
    assert r["complete"]
    boundary = parse_utc_ms("2024-10-09")
    with db.open_db(cfg.paths.db, read_only=True) as con:
        new = con.execute("SELECT MIN(ts), COUNT(*) FROM taker_volume").fetchone()
        prem = con.execute("SELECT AVG(mark / idx) FROM mark_index").fetchone()[0]
    with db.open_db(replay.old_db_path(cfg), read_only=True) as con:
        old = con.execute("SELECT MAX(ts), MIN(ts) FROM taker_volume").fetchone()
    assert new[0] >= boundary and old[0] < boundary and old[1] <= parse_utc_ms("2024-09-25")
    assert prem == pytest.approx(1.01)
    n = len(fake.calls)
    assert heatdata.backfill(cfg)["complete"] and len(fake.calls) == n
    st = heatdata.backfill_status(cfg)
    assert st["fill:taker"]["done"] == 1 and st["fill:mark"]["done"] == 1
