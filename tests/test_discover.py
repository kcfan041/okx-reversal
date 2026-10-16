import numpy as np
import pandas as pd
import pytest

from okxrev import db, derivs
from okxrev import discover as D
from okxrev.config import Paths, load_config
from okxrev.detector import btc_features, prepare

from test_detector_metrics import BAR_MS, EV, btc_df, flat, pumped

CFG = load_config()


def test_fresh_episodes():
    m = np.zeros(300, bool)
    m[[10, 11, 12, 50, 200]] = True
    assert D.fresh(m, gap=72).tolist() == [10, 200]
    assert D.fresh(m, gap=5).tolist() == [10, 50, 200]


def test_features_no_lookahead():
    df = pumped(n=2400)
    btc = btc_features(btc_df(n=2400), CFG.btc_regime)
    g1 = prepare(df, CFG.detector, btc)
    f1 = D.features(g1, CFG.detector, None, None, None)
    df2 = df.copy()
    df2.loc[EV + 1:, ["open", "high", "low", "close"]] *= 3
    df2.loc[EV + 1:, "vol_quote"] *= 50
    g2 = prepare(df2, CFG.detector, btc)
    f2 = D.features(g2, CFG.detector, None, None, None)
    cols = [c for c in f1.columns if c != "age_d"]
    pd.testing.assert_frame_equal(f1.iloc[: EV + 1][cols], f2.iloc[: EV + 1][cols])


def test_outcomes_entry_next_open_and_stop():
    df = flat(n=400)
    df.loc[101:, ["open", "close"]] = 110.0
    df["high"] = df[["open", "close"]].max(axis=1) * 1.001
    df["low"] = df[["open", "close"]].min(axis=1) * 0.999
    g = prepare(df, CFG.detector, None)
    atr = np.full(len(g), 0.01)
    out = D.outcomes(g, atr, cost=0.0)
    assert out["L_1h"][100] == pytest.approx(0.0, abs=1e-9)
    assert out["L_1h"][99] == pytest.approx(0.1)
    assert out["S_6h_sl2atr"][99] == pytest.approx(-0.02)
    assert np.isnan(out["L_3d"][-1])


def test_rules_unique_and_mask():
    rules = D.enumerate_rules()
    keys = [r.key for r in rules]
    assert len(keys) == len(set(keys))
    assert all(len({x.group for x in r.filters}) == len(r.filters) for r in rules)
    f = {"pump_red": np.array([True, True, False]), "btc_regime": np.array(["weak", "strong", "weak"])}
    r = D.Rule("pump_reversal", "short", D.BASES["pump_reversal"][0][1], (D.FILTERS[2],))
    assert r.mask(f).tolist() == [True, False, False]


def test_day_stats_clusters_days():
    ts = np.array([0, 1, 2, 3] + [D.DAY * k for k in range(1, 10)], dtype=np.int64)
    ret = np.array([1.0, 1.0, 1.0, 1.0] + [-0.1] * 9)
    s = D.day_stats(ret, ts)
    assert s["n"] == 13 and s["n_days"] == 10
    assert s["day_mean"] == pytest.approx((1.0 - 0.9) / 10)


def test_fast_t_matches_day_stats():
    rng = np.random.default_rng(0)
    ts = rng.integers(0, 40 * D.DAY, 400)
    df = pd.DataFrame({"rule": 0, "exit": "1h", "ts": ts, "ret": rng.normal(0.001, 0.01, 400)})
    a = D.fast_t(df)
    s = D.day_stats(df["ret"].to_numpy(), df["ts"].to_numpy())
    assert a["t"].iloc[0] == pytest.approx(s["t"])


def test_split_points_order():
    lo = pd.Timestamp("2025-10-01").value // 1_000_000
    hi = pd.Timestamp("2026-10-01").value // 1_000_000
    v, h = D.split_points(lo, hi)
    assert lo < v < h < hi
    assert pd.Timestamp(h, unit="ms").strftime("%Y-%m") == "2026-07"


def test_verdict_rules():
    good = {"n": 100, "n_days": 30, "mean": 0.01, "day_mean": 0.01, "ci95": [0.002, 0.02], "book_a": 0.2}
    weak = {"n": 50, "n_days": 10, "mean": 0.002, "day_mean": 0.002, "ci95": [-0.01, 0.01], "book_a": 0.002}
    bad = {"n": 50, "n_days": 10, "mean": -0.01, "day_mean": -0.01, "ci95": [-0.02, 0.0], "book_a": -0.1}
    assert D.verdict(good, good, 0.0, 0.0, True) == "可用"
    assert D.verdict(good, good, 0.0, 0.0, False) == "觀察中"
    assert D.verdict(weak, weak, 0.0, 0.0, True) == "觀察中"
    assert D.verdict(good, bad, 0.0, 0.0, True) == "無效"
    assert D.verdict(good, good, 0.02, 0.0, True) == "無效"
    day_only = {**good, "mean": -0.004, "book_a": -0.25}
    assert D.verdict(good, day_only, 0.0, 0.0, True) == "無效"
    no_book = {**good, "book_a": -0.01}
    assert D.verdict(good, no_book, 0.0, 0.0, True) == "無效"


def test_book_a_and_trade_stats():
    ret = np.array([0.1, -0.05, 0.02])
    ts = np.array([3, 1, 2]) * D.DAY
    assert D.book_a(ret, ts) == pytest.approx((1 - 0.001) * (1 + 0.0004) * (1 + 0.002) - 1)
    s = D.day_stats(np.array([0.01, 0.02, -0.03, 0.04, 0.05]), np.arange(5) * D.DAY, boot=0)
    assert s["book_a"] > 0 and s["t_trade"] > 0 and s["mean"] == pytest.approx(0.018)


def test_null_is_lower_for_noise():
    rng = np.random.default_rng(1)
    n = 6000
    ev = pd.DataFrame({"rule": rng.integers(0, 30, n), "exit": "1h", "ts": rng.integers(0, 120 * D.DAY, n),
                       "inst": rng.integers(0, 20, n), "ret": rng.normal(0, 0.01, n)})
    ev["exit"] = ev["exit"].astype("category")
    nulls = D.null_scores(ev, 200 * D.DAY, n=5)
    assert len(nulls) == 5 and np.all(np.isfinite(nulls)) and nulls.max() < 6


def test_derivs_attach_lagged():
    idx = pd.Index(np.arange(0, 3 * D.DAY, BAR_MS, dtype=np.int64))
    fund = pd.DataFrame({"ts": [D.DAY, 2 * D.DAY], "rate": [0.001, -0.002]})
    oi = pd.DataFrame({"period": "1D", "ts": [0, D.DAY], "oi_usd": [100.0, 150.0]})
    a = derivs.attach(pd.DataFrame(index=idx), fund, oi)
    k = int(D.DAY // BAR_MS)
    assert np.isnan(a["funding"].iloc[k - 2])
    assert a["funding"].iloc[k - 1] == 0.001
    assert np.isnan(a["oi_chg_24h"].iloc[k * 2 - 2]) and a["oi_chg_24h"].iloc[2 * k - 1] == pytest.approx(0.5)


def test_derivs_sync_pages(tmp_path):
    class C:
        def __init__(self):
            self.calls = []

        def get(self, path, p):
            self.calls.append((path, dict(p)))
            if path == derivs.FUNDING_PATH:
                if "after" not in p:
                    return [{"fundingTime": "3000", "fundingRate": "0.001", "realizedRate": "0.001"}]
                return [] if int(p["after"]) <= 2000 else [{"fundingTime": "2000", "fundingRate": "0.0", "realizedRate": "0"}]
            if p.get("period") == "1D" and "end" not in p:
                return [["5000", "1", "1", "100"]]
            return []

    import duckdb
    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    s = derivs.DerivSync(CFG, C(), con)
    r = s.run(["AAA-USDT-SWAP"])
    assert r["done"] == 6
    assert con.execute("SELECT COUNT(*) FROM funding").fetchone()[0] == 2
    assert con.execute("SELECT backfill_done FROM derivs_state WHERE stream='funding'").fetchone()[0]
    assert all(con.execute("SELECT backfill_done FROM derivs_state WHERE stream LIKE 'oi:1%'").fetchall())
    r2 = s.run(["AAA-USDT-SWAP"])
    assert r2["jobs"] == 0


def test_oi_history_runs_after_all_newest_pages(tmp_path):
    import duckdb
    from okxrev.db import now_ms
    order = []

    class C:
        def get(self, path, p):
            order.append((p.get("instId"), p.get("period"), "end" in p))
            return [[str(now_ms() - 10 * 3_600_000), "1", "1", "1"]] if "end" in p else [[str(now_ms()), "1", "1", "1"]]

    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    s = derivs.DerivSync(CFG, C(), con)
    now = now_ms()
    for i in ("A", "B"):
        s.states[(i, "funding")] = (now, None, True, now)
        s.states[(i, "oi:1D")] = (now, None, True, now)
        s.states[(i, "oi:1H")] = (now, now - 100 * 3_600_000, False, now - 2 * 3_600_000)
    s.run(["A", "B"], oi5m_insts=set())
    first_back = next(k for k, x in enumerate(order) if x[2])
    assert {x[0] for x in order[:first_back]} == {"A", "B"} and not any(x[2] for x in order[:first_back])
    assert s.states[("A", "oi:1H")][3] > now - 60_000


def test_derivs_no_starvation(tmp_path):
    """Streams that never finish backfilling must not starve fresher-needed streams: stalest first, by interval."""
    import duckdb
    from okxrev.db import now_ms

    class C:
        def __init__(self):
            self.calls = []

        def get(self, path, p):
            self.calls.append((path, p.get("period")))
            return []

    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    s = derivs.DerivSync(CFG, C(), con)
    now = now_ms()
    for inst in ("A", "B"):
        s.states[(inst, "funding")] = (None, None, False, now - 60_000)
        s.states[(inst, "oi:5m")] = (None, None, True, now - 3 * 3_600_000)
    jobs = []
    s.funding = lambda inst, **k: jobs.append(("funding", inst))
    s.oi = lambda inst, period, **k: jobs.append((f"oi:{period}", inst))
    s.run(["A", "B"])
    assert jobs[0][0] in ("oi:1H", "oi:1D")
    assert ("oi:5m", "A") in jobs and ("funding", "A") not in jobs


def _sched(s, insts, **kw):
    jobs = []
    s.funding = lambda inst, **k: jobs.append(("funding", inst))
    s.oi = lambda inst, period, **k: jobs.append((f"oi:{period}", inst))
    s.run(insts, **kw)
    return jobs


def test_oi5m_only_for_focus(tmp_path):
    import duckdb
    from okxrev.db import now_ms
    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    s = derivs.DerivSync(CFG, object(), con)
    now = now_ms()
    for inst in ("A", "B"):
        for st in ("funding", "oi:1H", "oi:1D"):
            s.states[(inst, st)] = (now, None, True, now)
    jobs = _sched(s, ["A", "B"], oi5m_insts={"A"})
    assert ("oi:5m", "A") in jobs and ("oi:5m", "B") not in jobs
    jobs = _sched(s, ["A", "B"], oi5m_insts=None)
    assert ("oi:5m", "B") in jobs


@pytest.mark.parametrize("hours", [2, 4, 8])
def test_funding_due_by_settlement_interval(tmp_path, hours, monkeypatch):
    import duckdb
    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    H = 3_600_000
    t0 = 1_800_000_000_000 - 1_800_000_000_000 % (8 * H)
    derivs.ensure_schema(con)
    con.executemany("INSERT INTO funding VALUES (?, ?, ?, ?)", [("X", t0 - k * hours * H, 0.0001, 0.0001) for k in range(6)])
    s = derivs.DerivSync(CFG, object(), con)
    assert s.intervals["X"] == hours * H
    s.states[("X", "funding")] = (t0, None, True, t0 + 60_000)
    for st in ("oi:1H", "oi:1D"):
        s.states[("X", st)] = (None, None, True, 10 ** 15)
    for now, expect in ((t0 + hours * H, False), (t0 + hours * H + 6 * 60_000, True)):
        monkeypatch.setattr(db, "now_ms", lambda n=now: n)
        jobs = _sched(s, ["X"], oi5m_insts=set())
        assert (("funding", "X") in jobs) is expect, (hours, now - t0)
    s.states[("X", "funding")] = (t0, None, True, t0 + hours * H + 6 * 60_000)
    monkeypatch.setattr(db, "now_ms", lambda: t0 + hours * H + 30 * 60_000)
    assert ("funding", "X") not in _sched(s, ["X"], oi5m_insts=set())


def test_oi1h_every_hour(tmp_path):
    import duckdb
    from okxrev.db import now_ms
    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    s = derivs.DerivSync(CFG, object(), con)
    now = now_ms()
    s.states[("A", "funding")] = (now, None, True, now)
    s.states[("A", "oi:1D")] = (now, None, True, now)
    s.states[("A", "oi:1H")] = (now, None, True, now - 61 * 60_000)
    assert ("oi:1H", "A") in _sched(s, ["A"], oi5m_insts=set())
    s.states[("A", "oi:1H")] = (now, None, True, now - 30 * 60_000)
    assert ("oi:1H", "A") not in _sched(s, ["A"], oi5m_insts=set())


def test_scan_end_to_end(tmp_path):
    cfg = load_config()
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "t.duckdb", logs=tmp_path / "logs", results=tmp_path / "r", lock=tmp_path / "l")
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "BTC-USDT-SWAP"}, {"inst_id": "AAA-USDT-SWAP"}])
        db.write_batch(con, "BTC-USDT-SWAP", btc_df(n=2400))
        db.write_batch(con, "AAA-USDT-SWAP", pumped(n=2400))
    rules = [D.Rule("pump_reversal", "short", D.BASES["pump_reversal"][0][1], ())]
    ev, extra = D.scan(cfg, rules)
    assert set(ev["exit"].astype(str)) <= set(D.EXITS)
    hits = ev[(ev["rule"] == 0) & (ev["exit"] == "1h")]
    assert len(hits) == 1 and hits["ts"].iloc[0] == pumped(n=2400)["ts"].iloc[EV]
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]) + D.SLIP_EXTRA_SMALL)
    assert hits["ret"].iloc[0] == pytest.approx(-cost, abs=1e-6)
    assert not extra["random"].empty
