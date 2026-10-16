import duckdb
import numpy as np
import pandas as pd
import pytest

from okxrev import db, paper, replay
from okxrev.config import Paths, load_config
from okxrev.paper import BAR, Context, Engine, Signal, Trader

T0 = 1_700_000_000_000


def make_cfg(tmp_path):
    cfg = load_config().copy_with({
        "paper": {"db": str(tmp_path / "paper.duckdb"), "models_dir": str(tmp_path / "models"),
                  "books": {"A": {"equity": 1000, "size_frac": 0.1, "lever": 1, "max_open": 3}, "C": {"unit": True}}},
        "replay": {"db": str(tmp_path / "old.duckdb"), "out": str(tmp_path / "replay.duckdb"),
                   "start": "2023-11-15", "end": "2023-11-20", "warmup_days": 1},
    })
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "logs", results=tmp_path / "r", lock=tmp_path / "l")
    return cfg


def bars(n, seed=0, t0=T0):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    o = np.r_[c[0], c[:-1]]
    idx = pd.Index(t0 + np.arange(n, dtype=np.int64) * BAR)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998, "close": c}, index=idx)


class Every(Trader):
    """Signals on every bar whose index is a multiple of k (deterministic, depends only on bar timestamps)."""
    kind = "test"

    def __init__(self, tid, k, side="long", exit_rule="1h"):
        self.trader_id, self.label, self.k, self.side, self.exit = tid, tid, k, side, exit_rule

    def signals(self, ctx, inst, f, b):
        return [Signal(self.trader_id, inst, int(x), self.side, self.exit, 1.0, "t", features={"atr": 0.01})
                for x in b if ((int(x) - T0) // BAR) % self.k == 0]


def run_live(cfg, con, traders, g, step):
    eng = Engine(cfg, traders, con)
    f = pd.DataFrame({"turnover24": 1e8, "r24h": 0.0}, index=g.index)
    fund = pd.DataFrame({"ts": [int(g.index[100]), int(g.index[200])], "rate": [0.001, -0.0005]})
    last = None
    for end in range(0, len(g), step):
        upto = int(g.index[end])
        ix = g.index.to_numpy(np.int64)
        lo = upto - (step - 1) * BAR if last is not None else int(g.index[0])
        new = ix[(ix >= lo) & (ix <= upto)]
        ctx = Context(cfg, upto, upto, {"AAA": new}, {"AAA": f}, {"AAA": g.loc[:upto]}, {"AAA": fund}, con)
        eng.tick(ctx)
        last = upto
    ctx = Context(cfg, int(g.index[-1]), int(g.index[-1]), {}, {"AAA": f}, {"AAA": g}, {"AAA": fund}, con)
    eng.check_open(ctx.candles, ctx.funding, ctx.now)
    return con.execute("SELECT trader_id, book, inst_id, side, entry_ts, entry_px, exit_ts, exit_px, exit_reason, ret_net "
                       "FROM positions WHERE state = 'closed' ORDER BY trader_id, book, entry_ts").df()


def test_step_size_does_not_change_results(tmp_path):
    """5-minute live ticks and hourly replay steps must produce identical trades."""
    cfg = make_cfg(tmp_path)
    g = bars(400)
    a = run_live(cfg, duckdb.connect(str(tmp_path / "a.duckdb")), [Every("t", 7), Every("s", 11, "short", "6h_sl2atr")], g, 1)
    b = run_live(cfg, duckdb.connect(str(tmp_path / "b.duckdb")), [Every("t", 7), Every("s", 11, "short", "6h_sl2atr")], g, 12)
    assert len(a) > 20
    pd.testing.assert_frame_equal(a, b)


def test_precomputed_matches_live_signals(tmp_path):
    """Batch (replay) and bar-by-bar (live) evaluation must fire on exactly the same bars for every trader type."""
    from okxrev import discover as D
    from okxrev.traders import ModelTrader, Precomputed, RandomTrader, RuleTrader
    rng = np.random.default_rng(3)
    n = 3000
    idx = pd.Index(T0 + np.arange(n, dtype=np.int64) * BAR)
    f = pd.DataFrame({"r24h": rng.normal(0, 0.1, n), "pump_red": rng.random(n) < 0.01, "btc_regime": "weak",
                      "atr": 0.01, "turnover24": 1e8}, index=idx)
    f.iloc[:5, 0] = np.nan
    rule = D.Rule("pump_reversal", "short", D.BASES["pump_reversal"][0][1], (D.FILTERS[2],))
    rt = RuleTrader({"key": rule.key, "exit": "1h", "label": rule.label, "verdict": "x", "valid": {}, "holdout": {}}, 1)

    class Stub:
        def predict_proba(self, X):
            p = (X["r24h"].fillna(0).to_numpy() + 0.3) / 0.6
            return np.column_stack([1 - p, p])

    mt = ModelTrader(load_config(), "ml")
    mt.prepare = lambda ctx: None
    mt.models = {"long": {"model": Stub(), "features": ["r24h"], "threshold": 0.8, "exit": "6h_sl2atr", "version": "v"}}
    import okxrev.ml as ml
    orig = ml.matrix
    ml.matrix = lambda fr: fr[["r24h"]]

    class Ctx:
        feats = {"AAA": f}

        def snapshot(self, inst, ts):
            return {}

    try:
        for tr in (rt, RandomTrader("long", "1h", 0.01), mt):
            live = []
            ix = idx.to_numpy()
            for k in range(0, n, 1):
                live += [s.bar_ts for s in tr.signals(Ctx(), "AAA", f.iloc[: k + 1], ix[k:k + 1])]
            p = Precomputed(tr)
            p.inner.prepare = lambda ctx: None
            p.build(Ctx(), int(idx[0]), int(idx[-1]))
            batch = sorted(p.by_inst.get("AAA", {}))
            assert live == batch and len(batch) > 3, tr.trader_id
    finally:
        ml.matrix = orig


def test_uhash_uniform_and_stable():
    from okxrev.traders import _seed, uhash
    ts = T0 + np.arange(200_000, dtype=np.int64) * BAR
    u = uhash(_seed("a", "b"), ts)
    assert 0 <= u.min() and u.max() < 1 and abs(u.mean() - 0.5) < 0.01 and abs((u < 0.01).mean() - 0.01) < 0.002
    assert np.array_equal(u[1000:1010], uhash(_seed("a", "b"), ts[1000:1010]))
    assert not np.array_equal(u[:10], uhash(_seed("a", "c"), ts[:10]))


def test_walk_position_unit():
    ts = np.array([1, 2, 3, 4], dtype=np.int64) * BAR
    h = np.array([101, 102, 104, 103.0])
    lo = np.array([99, 100, 101, 95.0])
    c = np.array([100, 101, 103, 96.0])
    fund = pd.DataFrame({"ts": [int(2.5 * BAR)], "rate": [0.001]})
    r = paper.walk_position("long", 100.0, 96.0, None, 10 * BAR, 0, ts, h, lo, c, fund, 0.0, 0.0, 0.0)
    assert r[2] == "stop" and r[0] == 4 * BAR and r[1] == 96.0 and r[4] == pytest.approx(-0.001) and r[6] == pytest.approx(0.04)
    r = paper.walk_position("short", 100.0, None, None, 3 * BAR, 0, ts[:3], h[:3], lo[:3], c[:3], None, 0.0, 0.0, 0.0)
    assert r[2] == "time" and r[1] == 103.0 and r[5] == pytest.approx(0.04)
    r = paper.walk_position("long", 100.0, None, None, 10 * BAR, 0, ts[:2], h[:2], lo[:2], c[:2], None, 0.0, 0.0, 0.0)
    assert r[2] is None and r[3] == 2 * BAR


def test_synthetic_funding_grid():
    f = replay.synthetic_funding(0, 3 * 86_400_000, 0.0001)
    assert len(f) == 9 and f["ts"].iloc[0] == 8 * 3_600_000 and (f["rate"] == 0.0001).all()


class FakeHist:
    """Serves 300-bar pages backwards in time; raises after `fail_after` calls once to test resume."""

    def __init__(self, oldest, newest, fail_after=None):
        self.oldest, self.newest, self.calls, self.fail_after = oldest, newest, 0, fail_after
        self.n_requests = 0

    def history_candles(self, inst, bar, limit=None, after=None, before=None):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            self.fail_after = None
            from okxrev.okx_client import RetryExhausted
            raise RetryExhausted("boom")
        hi = min(after, self.newest + BAR)
        ts = np.arange(max(self.oldest, hi - 300 * BAR), hi, BAR, dtype=np.int64)[::-1]
        df = pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 1.0, "vol_ccy": 1.0, "vol_quote": 1.0})
        return df.sort_values("ts").reset_index(drop=True), len(df)

    def close(self):
        pass


def test_backfill_resumes_and_is_idempotent(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    from okxrev.config import parse_utc_ms
    start = parse_utc_ms("2023-11-15") - 86_400_000
    end = parse_utc_ms("2023-11-20") + 4 * 86_400_000
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "BTC-USDT-SWAP"}, {"inst_id": "AAA-USDT-SWAP", "list_time": start - 30 * 86_400_000},
                                    {"inst_id": "NEW-USDT-SWAP", "list_time": end}])
    fake = FakeHist(start - 5 * 86_400_000, end, fail_after=3)
    monkeypatch.setattr(replay, "OkxClient", lambda *a, **k: fake)
    assert replay.targets(cfg, start, end) == ["BTC-USDT-SWAP", "AAA-USDT-SWAP"]
    r1 = replay.backfill(cfg)
    assert not r1["complete"] and r1["done"] == 0
    r2 = replay.backfill(cfg)
    assert r2["complete"] and r2["done"] == 2
    expected = (end - start) // BAR
    with db.open_db(replay.old_db_path(cfg), read_only=True) as con:
        n = dict(con.execute("SELECT inst_id, COUNT(*) FROM candles GROUP BY 1").fetchall())
        lo = con.execute("SELECT MIN(ts) FROM candles").fetchone()[0]
    assert n["BTC-USDT-SWAP"] == expected and n["AAA-USDT-SWAP"] == expected and lo == start
    calls = fake.calls
    assert replay.backfill(cfg)["complete"] and fake.calls == calls
    st = replay.backfill_status(cfg)
    assert st["done"] == 2 and st["rows"] == 2 * expected


def _old_db_with(cfg, data):
    with db.open_db(replay.old_db_path(cfg)) as con:
        db.upsert_instruments(con, [{"inst_id": i} for i in data])
        for inst, g in data.items():
            df = g.reset_index().rename(columns={"index": "ts"})
            df["ts"] = g.index.to_numpy(np.int64)
            df["vol"] = df["vol_ccy"] = df["vol_quote"] = 1.0
            db.write_batch(con, inst, df[["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote"]])


def test_run_and_compare(tmp_path):
    cfg = make_cfg(tmp_path)
    from okxrev.config import parse_utc_ms
    start = parse_utc_ms("2023-11-15")
    g = bars(2400, t0=start - 86_400_000)
    f = pd.DataFrame({"turnover24": 1e8, "r24h": 0.0}, index=g.index)
    res = replay.run(cfg, traders_=[Every("t", 37)], candles={"AAA": g}, feats={"AAA": f}, workers=1)
    assert res["signals"] > 10 and res["n_insts"] == 1
    c = replay.compare(cfg)
    assert c["exists"] and c["leaderboard"][0]["trader_id"] == "t" and c["leaderboard"][0]["C"]["n"] > 10
    con = duckdb.connect(str(replay.out_db_path(cfg)), read_only=True)
    first = con.execute("SELECT MIN(signal_ts) FROM positions").fetchone()[0]
    con.close()
    assert first >= start


def test_parallel_equals_single(tmp_path):
    """Splitting traders over worker processes must give exactly the same positions as one process."""
    from okxrev.config import parse_utc_ms
    cfg = make_cfg(tmp_path)
    cfg.source = tmp_path / "cfg.yaml"
    import yaml
    cfg.source.write_text(yaml.safe_dump({**cfg.raw, "paths": {"db": str(cfg.paths.db), "logs": str(cfg.paths.logs),
                                                               "results": str(cfg.paths.results), "lock": str(cfg.paths.lock)}},
                                         allow_unicode=True), encoding="utf-8")
    start = parse_utc_ms("2023-11-15")
    data = {"AAA": bars(2400, seed=1, t0=start - 86_400_000), "BBB": bars(2400, seed=2, t0=start - 86_400_000)}
    _old_db_with(cfg, data)
    feats = {i: pd.DataFrame({"turnover24": 1e8, "r24h": 0.0}, index=g.index) for i, g in data.items()}
    trs = [Every("a", 37), Every("b", 53, "short", "6h_sl2atr"), Every("c", 41, "long", "6h")]
    cols = "trader_id, book, inst_id, side, entry_ts, entry_px, exit_ts, exit_px, exit_reason, ret_net, pnl"

    def positions():
        con = duckdb.connect(str(replay.out_db_path(cfg)), read_only=True)
        df = con.execute(f"SELECT {cols} FROM positions ORDER BY trader_id, book, inst_id, entry_ts").df()
        eq = con.execute("SELECT trader_id, book, equity FROM books ORDER BY 1, 2").df()
        con.close()
        return df, eq

    replay.run(cfg, traders_=trs, candles=data, feats=feats, workers=1)
    a, ea = positions()
    replay.run(cfg, traders_=trs, candles=data, feats=feats, workers=2)
    b, eb = positions()
    assert len(a) > 30
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_frame_equal(ea, eb)


def test_replay_with_follow_trader(tmp_path):
    from okxrev.config import parse_utc_ms
    from okxrev.follow import FollowTrader
    cfg = make_cfg(tmp_path)
    start = parse_utc_ms("2023-11-15")
    g = bars(2400, seed=3, t0=start - 86_400_000)
    f = pd.DataFrame({"turnover24": 1e8, "r24h": 0.0}, index=g.index)
    fs = {"id": "follow_test", "step_h": 24, "look_h": 48, "top": 1, "min_n": 3, "short": True}
    trs = [Every("up", 7, "long", "1h"), Every("dn", 11, "short", "1h"), Every("random_long_1h", 5, "long", "1h"), FollowTrader(fs)]
    replay.run(cfg, traders_=trs, candles={"AAA": g}, feats={"AAA": f}, workers=1)
    con = duckdb.connect(str(replay.out_db_path(cfg)), read_only=True)
    try:
        picks = con.execute("SELECT period_ts, picks FROM follow_picks WHERE trader_id = 'follow_test' ORDER BY 1").fetchall()
        fp = con.execute("SELECT p.entry_ts, p.entry_px, p.stop_px, p.side, d.exit_rule, d.features FROM positions p JOIN decisions d USING (decision_id) "
                         "WHERE p.trader_id = 'follow_test' AND p.book = 'C'").df()
        src = con.execute("SELECT trader_id, bar_ts FROM decisions WHERE trader_id IN ('up', 'dn')").df()
    finally:
        con.close()
    import json
    assert picks and picks[0][0] <= start
    chosen = [json.loads(p)[0]["trader_id"] for _, p in picks if json.loads(p)]
    assert chosen and set(chosen) <= {"up", "dn"}
    assert len(fp) > 0 and (fp["exit_rule"] == "1h_sl20pct").all()
    sgn = np.where(fp["side"] == "long", 1.0, -1.0)
    assert np.allclose(fp["stop_px"], fp["entry_px"] * (1 - sgn * 0.20))
    for r in fp.itertuples():
        feats = json.loads(r.features)
        assert feats["_follow_src"] in {"up", "dn"}


def test_follow_rank_uses_only_past_and_excludes():
    from okxrev.follow import rank
    H = 3_600_000
    rows = []
    for k in range(10):
        rows.append(("good", "long", 100 * H + k, 0.05))
        rows.append(("good_sl20", "long", 100 * H + k, 0.06))
        rows.append(("bad", "long", 100 * H + k, -0.02))
        rows.append(("random_long_1h", "long", 100 * H + k, 0.01))
        rows.append(("llm_good", "long", 100 * H + k, 0.5))
        rows.append(("follow_x", "long", 100 * H + k, 0.5))
        rows.append(("future", "long", 200 * H + k, 0.9))
    df = pd.DataFrame(rows, columns=["trader_id", "side", "exit_ts", "ret_net"])
    r = rank(df, 150 * H, 100, 5, 5)
    assert [x["trader_id"] for x in r] == ["good_sl20"]
    assert r[0]["score"] == pytest.approx(0.05)
    assert rank(df, 150 * H, 100, 5, 20) == []
