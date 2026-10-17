import json

import numpy as np
import pandas as pd
import pytest

from okxrev import llm, paper
from okxrev.config import Paths, load_config
from okxrev.paper import BAR, Context, Engine, Signal, Trader

T0 = 1_700_000_000_000


def make_cfg(tmp_path):
    cfg = load_config()
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "logs", results=tmp_path / "r", lock=tmp_path / "l")
    cfg = cfg.copy_with({"paper": {"db": str(tmp_path / "paper.duckdb"), "models_dir": str(tmp_path / "models"),
                                   "books": {"A": {"equity": 1000, "size_frac": 0.1, "lever": 1, "max_open": 2},
                                             "B": {"equity": 100, "notional": 1000, "lever": 20, "max_open": 1},
                                             "C": {"unit": True}}}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "logs", results=tmp_path / "r", lock=tmp_path / "l")
    return cfg


def bars(prices, t0=T0):
    idx = pd.Index(t0 + np.arange(len(prices), dtype=np.int64) * BAR)
    p = np.asarray(prices, float)
    return pd.DataFrame({"open": p, "high": p * 1.001, "low": p * 0.999, "close": p}, index=idx)


class Fixed(Trader):
    kind = "test"

    def __init__(self, tid, at, side="long", exit_rule="1h", atr=0.01):
        self.trader_id, self.label, self.at, self.side, self.exit, self.atr = tid, tid, at, side, exit_rule, atr

    def signals(self, ctx, inst, f, bars_):
        return [Signal(self.trader_id, inst, int(b), self.side, self.exit, 1.0, "t", features={"atr": self.atr}) for b in bars_ if int(b) in self.at]


def ctx_for(cfg, con, g, upto, new, fund=None):
    sub = g[g.index <= upto]
    f = pd.DataFrame({"turnover24": 1e8, "r24h": 0.0}, index=sub.index)
    return Context(cfg, upto, upto, {"AAA": np.asarray(new, dtype=np.int64)}, {"AAA": f}, {"AAA": sub},
                   {"AAA": fund if fund is not None else pd.DataFrame(columns=["ts", "rate"])}, con)


def test_fill_next_open_time_exit_and_books(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    prices = [100.0] * 10 + [110.0] * 30
    g = bars(prices)
    sig_ts = int(g.index[5])
    eng = Engine(cfg, [Fixed("t1", {sig_ts})], con)
    r = eng.tick(ctx_for(cfg, con, g, sig_ts, [sig_ts]))
    assert r["signals"] == 1 and r["queued"] == 3
    assert con.execute("SELECT COUNT(*) FROM positions WHERE state='pending'").fetchone()[0] == 3
    eng.tick(ctx_for(cfg, con, g, sig_ts + BAR, [sig_ts + BAR]))
    p = con.execute("SELECT entry_ts, entry_px, deadline_ts FROM positions WHERE book='A'").fetchone()
    assert p[0] == sig_ts + BAR and p[1] == 100.0 and p[2] == sig_ts + BAR + 11 * BAR
    eng.tick(ctx_for(cfg, con, g, int(g.index[-1]), []))
    rows = con.execute("SELECT book, exit_reason, exit_px, ret_net, pnl FROM positions ORDER BY book").fetchall()
    fee = eng._cost("AAA", {"AAA": pd.DataFrame({"turnover24": [1e8]})})
    assert all(r[1] == "time" and r[2] == 110.0 for r in rows)
    assert rows[0][3] == pytest.approx(0.1 - 2 * fee)
    assert rows[0][4] == pytest.approx(rows[0][3] * 100)
    assert con.execute("SELECT equity FROM books WHERE trader_id='t1' AND book='A'").fetchone()[0] == pytest.approx(1000 + rows[0][4])
    assert rows[2][4] == pytest.approx(rows[2][3])


def test_idempotent_and_no_duplicate(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    g = bars([100.0] * 40)
    s = int(g.index[5])
    eng = Engine(cfg, [Fixed("t1", {s, s + BAR})], con)
    c = ctx_for(cfg, con, g, s, [s])
    eng.tick(c)
    eng.tick(c)
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    eng.tick(ctx_for(cfg, con, g, s + BAR, [s + BAR]))
    assert con.execute("SELECT COUNT(*) FROM positions WHERE book='A'").fetchone()[0] == 1


def test_stop_short_and_mae(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    prices = [100.0] * 8 + [100.0, 101.0, 103.5, 99.0] + [99.0] * 20
    g = bars(prices)
    s = int(g.index[6])
    eng = Engine(cfg, [Fixed("t1", {s}, side="short", exit_rule="6h_sl2atr", atr=0.015)], con)
    eng.tick(ctx_for(cfg, con, g, s, [s]))
    eng.tick(ctx_for(cfg, con, g, int(g.index[-1]), []))
    r = con.execute("SELECT exit_reason, exit_px, ret_net, mae FROM positions WHERE book='C'").fetchone()
    assert r[0] == "stop" and r[1] == pytest.approx(103.0)
    assert r[3] >= 0.03


def test_liquidation_book_b(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    prices = [100.0] * 8 + [100.0, 95.0] + [95.0] * 20
    g = bars(prices)
    s = int(g.index[6])
    eng = Engine(cfg, [Fixed("t1", {s}, exit_rule="1h")], con)
    eng.tick(ctx_for(cfg, con, g, s, [s]))
    eng.tick(ctx_for(cfg, con, g, int(g.index[-1]), []))
    b = con.execute("SELECT exit_reason, ret_net, pnl FROM positions WHERE book='B'").fetchone()
    a = con.execute("SELECT exit_reason FROM positions WHERE book='A'").fetchone()
    assert b[0] == "liquidation" and b[2] == pytest.approx(b[1] * 1000) and b[1] < -0.05 + 1e-9
    assert a[0] == "time"


def test_funding_charged(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    g = bars([100.0] * 40)
    s = int(g.index[5])
    fund = pd.DataFrame({"ts": [int(g.index[8]), int(g.index[30])], "rate": [0.001, 0.002]})
    eng = Engine(cfg, [Fixed("t1", {s}, exit_rule="1h")], con)
    eng.tick(ctx_for(cfg, con, g, s, [s], fund))
    eng.tick(ctx_for(cfg, con, g, int(g.index[-1]), [], fund))
    f = con.execute("SELECT funding FROM positions WHERE book='C'").fetchone()[0]
    assert f == pytest.approx(-0.001)


def test_max_open_and_equity_snapshot(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    g = bars([100.0] * 60)
    at = {int(g.index[i]) for i in (5, 6, 7)}

    class Multi(Fixed):
        def signals(self, ctx, inst, f, b):
            return [Signal(self.trader_id, f"X{int(x)}", int(x), "long", "1h", 1.0, "t") for x in b if int(x) in self.at]

    eng = Engine(cfg, [Multi("t1", at)], con)
    c = ctx_for(cfg, con, g, int(g.index[7]), [int(g.index[i]) for i in (5, 6, 7)])
    eng.tick(c)
    assert con.execute("SELECT COUNT(*) FROM positions WHERE book='A'").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM positions WHERE book='B'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM positions WHERE book='C'").fetchone()[0] == 3


def test_new_bars_since_catchup():
    f = {"A": pd.DataFrame(index=pd.Index(T0 + np.arange(100, dtype=np.int64) * BAR))}
    last = T0 + 99 * BAR
    assert len(paper.new_bars_since(f, None, last, 36)["A"]) == 36
    assert paper.new_bars_since(f, last - 2 * BAR, last, 36)["A"].tolist() == [last - BAR, last]
    assert len(paper.new_bars_since(f, last, last, 36)["A"]) == 0


def test_rule_and_random_traders_deterministic(tmp_path):
    from okxrev import discover as D
    from okxrev.traders import RandomTrader, RuleTrader
    r = D.enumerate_rules()[0]
    res = {"key": r.key, "exit": "1h", "label": r.label, "verdict": "觀察中", "valid": {}, "holdout": {}}
    t = RuleTrader(res, 1)
    idx = pd.Index(T0 + np.arange(200, dtype=np.int64) * BAR)
    f = pd.DataFrame({"pump_red": False, "r24h": 0.1}, index=idx)
    f.iloc[150, 0] = True
    f.iloc[151, 0] = True
    s = t.signals(None, "AAA", f, idx.to_numpy()[140:])
    assert [x.bar_ts for x in s] == [int(idx[150])] and s[0].side == "short"
    rt = RandomTrader("long", "1h", 0.5)
    a = rt.signals(None, "AAA", f, idx.to_numpy())
    b = rt.signals(None, "AAA", f, idx.to_numpy())
    assert [x.bar_ts for x in a] == [x.bar_ts for x in b] and 60 < len(a) < 140


def test_llm_review_caps_and_parse(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg = cfg.copy_with({"paper": {"llm": {"max_calls_per_day": 2, "max_per_tick": 5, "max_cost_usd_per_day": 1, "stale_min": 240}}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "logs", results=tmp_path / "r", lock=tmp_path / "l")
    (tmp_path / ".env").write_text("LLM_BASE_URL=http://x/v1\nLLM_API_KEY=k\nLLM_MODEL=m\n", encoding="utf-8")
    con = paper.connect(cfg)
    answers = iter(['{"decision":"accept","confidence":0.8,"reason":"ok"}', 'garbage', ''])

    class R:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self.text}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    class S:
        def post(self, url, json=None, headers=None, timeout=None):
            assert url == "http://x/v1/chat/completions" and headers["Authorization"] == "Bearer k"
            return R(next(answers))

    g = bars([100.0] * 30)
    c = ctx_for(cfg, con, g, int(g.index[-1]), [])
    sigs = [Signal("llm_x", "AAA", int(g.index[i]), "long", "1h", 1.0 - i / 100, "rule", features={"atr": 0.01}) for i in (20, 21, 22)]
    out = {s.bar_ts: s for s in llm.review(c, cfg, sigs, session=S())}
    o = [out[int(g.index[i])] for i in (22, 21, 20)]
    assert [s.action for s in o] == ["enter", "skip_llm", "skip_llm"]
    assert "每日呼叫或費用上限" in o[2].reason and "失敗" in o[1].reason
    n, cost = llm.usage_today(con)
    assert n == 2 and cost == pytest.approx(2 * (1.0 * 0.00015 + 0.1 * 0.0006))
    assert llm.parse('前言 {"decision":"reject","confidence":0.3,"reason":"x"}')[0] == "reject"


def test_llm_call_disables_stream_and_detects_sse(tmp_path):
    cfg = make_cfg(tmp_path)
    (tmp_path / ".env").write_text("LLM_BASE_URL=http://localhost:20128/v1/\nLLM_API_KEY=k\nLLM_MODEL=gpt\n", encoding="utf-8")
    sent = {}

    class R:
        def __init__(self, ok):
            self.ok = ok
            self.text = "" if ok else 'data: {"id":"x"}\n\n'

        def raise_for_status(self):
            pass

        def json(self):
            if not self.ok:
                raise ValueError("not json")
            return {"choices": [{"message": {"content": '{"decision":"accept","confidence":0.7,"reason":"r"}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    class S:
        def __init__(self, ok):
            self.ok = ok

        def post(self, url, json=None, headers=None, timeout=None):
            sent.update(url=url, body=json)
            return R(self.ok)

    text, tin, tout = llm.call(cfg, "hi", S(True))
    assert sent["url"] == "http://localhost:20128/v1/chat/completions"
    assert sent["body"]["stream"] is False and sent["body"]["model"] == "gpt"
    assert llm.parse(text)[0] == "accept" and (tin, tout) == (10, 5)
    with pytest.raises(ValueError, match="串流"):
        llm.call(cfg, "hi", S(False))


def test_model_fires_once_per_episode(monkeypatch):
    from okxrev import ml
    from okxrev.discover import FRESH_GAP
    from okxrev.traders import ModelTrader
    n = 400
    idx = pd.Index(T0 + np.arange(n, dtype=np.int64) * BAR)
    p = np.zeros(n)
    p[50:120] = 0.9
    p[150:160] = 0.9
    p[300:310] = 0.9
    f = pd.DataFrame({"p": p}, index=idx)

    class Stub:
        def predict_proba(self, X):
            return np.column_stack([1 - X["p"].to_numpy(), X["p"].to_numpy()])

    monkeypatch.setattr(ml, "matrix", lambda fr: fr[["p"]])
    mt = ModelTrader(load_config(), "ml")
    mt.models = {"long": {"model": Stub(), "features": ["p"], "threshold": 0.5, "exit": "1h", "version": "v"}}

    class Ctx:
        def snapshot(self, inst, ts):
            return {}

    got = [s.bar_ts for s in mt.signals(Ctx(), "AAA", f, idx.to_numpy())]
    assert got == [int(idx[50]), int(idx[300])]
    assert 150 - 120 < FRESH_GAP
    live = []
    for k in range(n):
        live += [s.bar_ts for s in mt.signals(Ctx(), "AAA", f.iloc[: k + 1], idx.to_numpy()[k:k + 1])]
    assert live == got


def test_margin_book_cannot_overcommit(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg = cfg.copy_with({"paper": {"books": {"B": {"equity": 100, "notional": 1500, "lever": 20, "max_open": 5}}}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "logs", results=tmp_path / "r", lock=tmp_path / "l")
    con = paper.connect(cfg)
    g = bars([100.0] * 60)
    at = [int(g.index[i]) for i in (5, 6, 7)]

    class Multi(Fixed):
        def signals(self, ctx, inst, f, b):
            return [Signal(self.trader_id, f"X{int(x)}", int(x), "long", "6h", 1.0, "t") for x in b if int(x) in self.at]

    eng = Engine(cfg, [Multi("t1", set(at))], con)
    eng.tick(ctx_for(cfg, con, g, at[-1], at))
    n = con.execute("SELECT notional FROM positions WHERE book='B' ORDER BY signal_ts").fetchall()
    assert [round(x[0]) for x in n] == [1500, 500]


def test_unit_book_no_duplicate_open(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    g = bars([100.0] * 60)
    at = {int(g.index[i]) for i in (5, 6, 7)}
    eng = Engine(cfg, [Fixed("t1", at, exit_rule="6h")], con)
    eng.tick(ctx_for(cfg, con, g, int(g.index[7]), [int(g.index[i]) for i in (5, 6, 7)]))
    assert con.execute("SELECT COUNT(*) FROM positions WHERE book='C' AND state IN ('open','pending')").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 3


def test_ml_auc_and_matrix():
    from okxrev import ml
    assert ml.auc_score(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.3, 0.4])) == 1.0
    assert ml.auc_score(np.array([0, 1]), np.array([0.5, 0.5])) == 0.5
    f = pd.DataFrame({"r1h": [0.1], "turnover24": [1e6], "btc_regime": ["weak"], "pump": [True]}, index=[T0])
    X = ml.matrix(f)
    assert X.at[T0, "btc_regime"] == 0 and X.at[T0, "log_turnover"] == 6 and X.at[T0, "pump"] == 1.0 and np.isnan(X.at[T0, "r6h"])
    rng = np.random.default_rng(0)
    Xs = pd.DataFrame({"a": rng.normal(size=4000), "b": rng.normal(size=4000)})
    y = (Xs["a"] + 0.3 * rng.normal(size=4000) > 0).astype(int)
    b = ml.Booster(50, learning_rate=0.1, num_leaves=7, min_data_in_leaf=20).fit(Xs, y)
    p = b.predict_proba(Xs)
    assert p.shape == (4000, 2) and ml.auc_score(y.to_numpy(), p[:, 1]) > 0.9
    bb = ml.Booster(20, balanced=True, num_leaves=3, min_data_in_leaf=5).fit(Xs, (Xs["a"] > 1.5).astype(int))
    assert bb.booster_.params["scale_pos_weight"] > 5


def test_ml_save_load_roundtrip(tmp_path):
    from okxrev import ml
    cfg = make_cfg(tmp_path)
    rng = np.random.default_rng(0)
    Xs = pd.DataFrame({"a": rng.normal(size=500), "b": rng.normal(size=500)})
    b = ml.Booster(10, num_leaves=3, min_data_in_leaf=5).fit(Xs, (Xs["a"] > 0).astype(int))
    ml.save(cfg, "ml", "long", {"version": "v1", "model": b, "features": ["a", "b"], "threshold": 0.6, "exit": "1h"})
    assert ml.has_model(cfg, "ml") and not ml.has_model(cfg, "twin")
    m = ml.load_latest(cfg, "ml", "long")
    assert m["threshold"] == 0.6 and np.allclose(m["model"].predict_proba(Xs)[:, 1], b.predict_proba(Xs)[:, 1])
    assert ml.latest_meta(cfg)["ml_long"]["n_versions"] == 1


def test_web_paper_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from okxrev.web.app import create_app
    cfg = make_cfg(tmp_path)
    c = TestClient(create_app(cfg))
    assert c.get("/api/paper/status").json()["exists"] is False
    con = paper.connect(cfg)
    Engine(cfg, [Fixed("t1", set())], con)
    con.close()
    s = c.get("/api/paper/status").json()
    assert s["exists"] and s["leaderboard"][0]["trader_id"] == "t1"
    assert c.get("/api/paper/positions?state=bad").status_code == 400
    assert c.get("/api/paper/positions?trader=../x").status_code == 400
    assert c.get("/api/paper/positions").json() == []
    assert c.get("/api/paper/equity?book=A").json() == {}
    assert c.post("/api/paper/tick").status_code == 403
    r = c.post("/api/paper/export", headers={"x-okxrev": "1"}).json()
    assert r["ok"] and "decisions.parquet" in r["files"]


def test_leaderboard_vs_random(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    Engine(cfg, [Fixed("rule01", set()), Fixed("random_long_1h", set())], con)
    rows = []
    for i in range(30):
        ts = T0 + i * paper.DAY
        rows.append((f"r{i}", "rule01", "C", ts, 0.02 + 0.001 * (i % 3)))
        rows.append((f"x{i}", "random_long_1h", "C", ts, -0.001 + 0.001 * (i % 3)))
    for pid, tid, book, ts, ret in rows:
        con.execute("INSERT INTO positions (pos_id, trader_id, book, inst_id, side, state, entry_ts, ret_net, pnl, exit_reason) "
                    "VALUES (?, ?, ?, 'AAA', 'long', 'closed', ?, ?, ?, 'time')", [pid, tid, book, ts, ret, ret])
    lb = {r["trader_id"]: r for r in paper.leaderboard(con)}
    assert lb["rule01"]["C"]["n"] == 30 and lb["rule01"]["vs_random"]["significant"]
    assert "vs_random" not in lb["random_long_1h"]


def test_rule_ids_are_stable(tmp_path):
    from okxrev import traders as TR
    paths = make_cfg(tmp_path).paths
    cfg = make_cfg(tmp_path).copy_with({"replay": {"out": str(tmp_path / "no_replay.duckdb")}})
    cfg.paths = paths
    a = {"key": "k|a", "exit": "6h"}
    b = {"key": "k|b", "exit": "24h"}
    c = {"key": "k|c", "exit": "3d"}
    assert TR.rule_ids(cfg, [a, b]) == ["rule01_6h", "rule02_24h"]
    assert TR.rule_ids(cfg, [c, b]) == ["rule03_3d", "rule02_24h"]
    assert TR.rule_ids(cfg, [a]) == ["rule01_6h"]


def test_diagnostics_concentration_same_day_and_groups():
    D = paper.DAY
    rows = []

    def add(tid, inst, day, ret, side="long", hour=0):
        rows.append({"trader_id": tid, "inst_id": inst, "side": side, "entry_ts": T0 + day * D + hour * 3_600_000 + 300_000,
                     "signal_ts": T0 + day * D + hour * 3_600_000, "ret_net": ret})
    for k in range(10):
        add("ruleA_24h", f"C{k}", 0 if k < 8 else k, 0.10)
        add("ruleB_3d", f"C{k}", 0 if k < 8 else k, 0.12)
    add("ruleC_24h", "Z", 1, 0.0)
    for d in range(12):
        for j in range(4):
            add("random_long_24h", f"R{j}", d, 0.08 if d == 0 else 0.0)
            add("random_long_1h", f"Q{j}", d, -0.5)
    out = paper.diagnostics(pd.DataFrame(rows))
    a = out["ruleA_24h"]
    assert a["n_days"] == 3 and a["max_day_share"] == pytest.approx(0.8) and a["eff_days"] == pytest.approx(100 / 66)
    v = a["vs_same_day"]
    assert v["n"] == 10 and v["excess"] == pytest.approx((0.02 + 0.10 + 0.10) / 3)
    assert a["group"] == ["ruleA_24h", "ruleB_3d"] and a["overlap"] == {"with": "ruleB_3d", "share": 1.0}
    assert "group" not in out["ruleC_24h"] and "overlap" not in out["ruleC_24h"]
    assert "random_long_24h" not in out


def test_atr_stop_fallback_and_feature_atr(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    g = bars([100.0] * 40)
    s = int(g.index[5])
    eng = Engine(cfg, [Fixed("noatr", {s}, side="short", exit_rule="6h_sl2atr", atr=None),
                       Fixed("fromf", {s}, side="short", exit_rule="6h_sl2atr", atr=None),
                       Fixed("fixed", {s}, side="short", exit_rule="3d_sl20pct", atr=None)], con)
    c = ctx_for(cfg, con, g, s, [s])
    c.feats["AAA"]["atr"] = np.nan
    eng.tick(c)
    stops = dict(con.execute("SELECT trader_id, stop_px FROM positions WHERE book='C'").fetchall())
    assert stops["noatr"] == pytest.approx(paper.FALLBACK_STOP) and stops["fixed"] == pytest.approx(0.20)
    feats = json.loads(con.execute("SELECT features FROM decisions WHERE trader_id='noatr'").fetchone()[0])
    assert feats["_stop_fallback"] is True
    con2 = paper.connect(make_cfg(tmp_path / "b"))
    eng2 = Engine(make_cfg(tmp_path / "b"), [Fixed("fromf", {s}, side="short", exit_rule="6h_sl2atr", atr=None)], con2)
    c2 = ctx_for(cfg, con2, g, s, [s])
    c2.feats["AAA"]["atr"] = 0.02
    eng2.tick(c2)
    assert con2.execute("SELECT stop_px FROM positions WHERE book='C'").fetchone()[0] == pytest.approx(0.04)


def test_sl20_variant_stops_out_and_shares_signals(tmp_path):
    from okxrev.traders import StopVariant
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    prices = [100.0] * 8 + [100.0, 105.0, 125.0] + [125.0] * 30
    g = bars(prices)
    s = int(g.index[6])
    base = Fixed("ruleX_3d", {s}, side="short", exit_rule="3d")
    var = StopVariant(base, paper.with_sl20("3d"))
    assert var.trader_id == "ruleX_3d_sl20" and var.exit == "3d_sl20pct"
    eng = Engine(cfg, [var, base], con)
    eng.tick(ctx_for(cfg, con, g, s, [s]))
    eng.tick(ctx_for(cfg, con, g, int(g.index[-1]), []))
    r = dict(con.execute("SELECT trader_id, exit_reason FROM positions WHERE book='C'").fetchall())
    assert r["ruleX_3d_sl20"] == "stop"
    assert con.execute("SELECT state FROM positions WHERE book='C' AND trader_id='ruleX_3d'").fetchone()[0] == "open"
    assert paper.with_sl20("6h_sl2atr") is None and paper.EXIT_RULES["3d_sl20pct"][0] == 864


def test_follow_trader_live_copies_picked(tmp_path):
    from okxrev.follow import FollowTrader, period_of
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    H = 3_600_000
    g = bars([100.0] * 400)
    s = int(g.index[300])
    p = period_of(s, 24)
    for k in range(6):
        con.execute("INSERT INTO positions (pos_id, trader_id, book, inst_id, side, state, entry_ts, exit_ts, ret_net, pnl) "
                    "VALUES (?, 'winner', 'C', 'X', 'long', 'closed', ?, ?, 0.05, 0.05)", [f"w{k}", p - 10 * H, p - 5 * H + k])
        con.execute("INSERT INTO positions (pos_id, trader_id, book, inst_id, side, state, entry_ts, exit_ts, ret_net, pnl) "
                    "VALUES (?, 'loser', 'C', 'X', 'long', 'closed', ?, ?, -0.05, -0.05)", [f"l{k}", p - 10 * H, p - 5 * H + k])
    fs = {"id": "follow_t", "step_h": 24, "look_h": 48, "top": 1, "min_n": 3, "short": True}
    eng = Engine(cfg, [FollowTrader(fs), Fixed("winner", {s}, exit_rule="6h"), Fixed("loser", {s}, exit_rule="6h")], con)
    eng.tick(ctx_for(cfg, con, g, s, [s]))
    d = con.execute("SELECT exit_rule, features FROM decisions WHERE trader_id = 'follow_t'").fetchall()
    assert len(d) == 1 and d[0][0] == "6h_sl20pct" and json.loads(d[0][1])["_follow_src"] == "winner"
    pk = con.execute("SELECT picks FROM follow_picks WHERE trader_id = 'follow_t'").fetchone()[0]
    assert json.loads(pk)[0]["trader_id"] == "winner"


def test_web_follow_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from okxrev.web.app import create_app
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    con.execute("INSERT INTO follow_picks VALUES ('follow_7d_60d_top3', 1000, '[{\"trader_id\": \"rule04_3d\", \"score\": 0.1, \"n\": 25}]', 1)")
    con.close()
    c = TestClient(create_app(cfg))
    d = c.get("/api/paper/follow?trader=follow_7d_60d_top3").json()
    assert d["picks"][0]["picks"][0]["trader_id"] == "rule04_3d" and d["set"]["top"] == 3 and d["next_repick"]
    assert c.get("/api/paper/follow?trader=../x").status_code == 400


def _llm_env(tmp_path, extra=None):
    cfg = make_cfg(tmp_path)
    cfg = cfg.copy_with({"paper": {"llm": {"max_calls_per_day": 100, "max_per_tick": 8, "max_per_bar": 2, "stale_min": 15, "max_sec": 60,
                                           **(extra or {})}}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "logs", results=tmp_path / "r", lock=tmp_path / "l")
    (tmp_path / ".env").write_text("LLM_BASE_URL=http://x/v1\nLLM_API_KEY=k\nLLM_MODEL=m\n", encoding="utf-8")
    return cfg


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.text}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class _Sess:
    def __init__(self, answer):
        self.answer, self.bodies = answer, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(json)
        return _Resp(self.answer(json) if callable(self.answer) else self.answer)


def test_llm_stale_and_per_bar_limits(tmp_path):
    cfg = _llm_env(tmp_path)
    con = paper.connect(cfg)
    g = bars([100.0] * 40)
    last = int(g.index[-1])
    c = ctx_for(cfg, con, g, last, [])
    old = [Signal("llm_x", f"O{i}", last - 30 * 60_000, "long", "1h", 0.9, "r", features={"atr": 0.01}) for i in range(3)]
    now = [Signal("llm_x", f"N{i}", last, "long", "1h", 0.5 + i / 100, "r", features={"atr": 0.01}) for i in range(5)]
    sess = _Sess('{"decision":"reject","confidence":0.6,"reason":"x"}')
    out = llm.review(c, cfg, old + now, session=sess)
    by = {s.inst_id: s for s in out}
    assert all("補跑的舊訊號" in by[f"O{i}"].reason for i in range(3))
    assert by["N4"].action == "reject" and by["N3"].action == "reject"
    assert all("同一根 K 線名額已滿" in by[f"N{i}"].reason for i in range(3))
    assert len(sess.bodies) == 2


def test_llm_tick_budget_shared(tmp_path):
    cfg = _llm_env(tmp_path, {"max_per_tick": 3, "max_per_bar": 5})
    con = paper.connect(cfg)
    g = bars([100.0] * 40)
    last = int(g.index[-1])
    c = ctx_for(cfg, con, g, last, [])
    sess = _Sess('{"decision":"accept","confidence":0.6,"reason":"x"}')
    a = llm.review(c, cfg, [Signal("llm_a", f"A{i}", last, "long", "1h", 0.5, "r") for i in range(2)], session=sess)
    b = llm.review(c, cfg, [Signal("llm_b", f"B{i}", last, "long", "1h", 0.5, "r") for i in range(2)], session=sess)
    assert len(sess.bodies) == 3
    assert sum(s.action == "enter" for s in a + b) == 3 and any("本輪名額已滿" in s.reason for s in b)


def test_llm_batch_review(tmp_path):
    import json as _j
    cfg = _llm_env(tmp_path)
    con = paper.connect(cfg)
    g = bars([100.0] * 40)
    last = int(g.index[-1])
    c = ctx_for(cfg, con, g, last, [])
    sigs = [Signal("llmb_ml", f"C{i}-USDT-SWAP", last, "long", "6h_sl2atr", 0.5 + i / 100, "p", features={"atr": 0.01}) for i in range(4)]
    sigs.append(Signal("llmb_ml", "OLD-USDT-SWAP", last - 3600_000, "long", "6h_sl2atr", 0.9, "p"))
    sess = _Sess('{"accept": ["C2-USDT-SWAP", "c3-usdt-swap", "NOPE"], "confidence": 0.7, "reason": "y"}')
    out = {s.inst_id: s for s in llm.review_batch(c, cfg, sigs, session=sess)}
    assert len(sess.bodies) == 1
    sent = _j.loads(sess.bodies[0]["messages"][1]["content"])
    assert sent["候選數"] == 4 and "accept" in sess.bodies[0]["messages"][0]["content"]
    assert out["C2-USDT-SWAP"].action == "enter" and out["C3-USDT-SWAP"].action == "enter"
    assert out["C0-USDT-SWAP"].action == "reject" and "4 選 2" in out["C0-USDT-SWAP"].reason
    assert "補跑" in out["OLD-USDT-SWAP"].reason
    bad = _Sess("not json")
    out2 = llm.review_batch(ctx_for(cfg, con, g, last, []), cfg, sigs[:2], session=bad)
    assert all(s.action == "skip_llm" and "批次失敗" in s.reason for s in out2)


def test_inverse_candidates_and_flip(tmp_path):
    from okxrev import discover as D
    from okxrev.traders import InverseTrader, RuleTrader, inverse_candidates
    key = next(r.key for r in D.enumerate_rules() if r.side == "short")
    seg = lambda n, m: {"n": n, "mean": m}
    good = {"key": key, "exit": "1h", "side": "short", "label": "x", "verdict": "無效", "valid": seg(50, -0.008), "holdout": seg(60, -0.009),
            "base_valid": -0.003, "base_hold": -0.003}
    dup = {**good, "key": key + "|dup"}
    weak = {**good, "holdout": seg(60, -0.004)}
    few = {**good, "valid": seg(10, -0.02)}
    assert [r["key"] for r in inverse_candidates([good, dup, weak, few])] == [key]
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    prices = [100.0] * 8 + [100.0] * 2 + [110.0] * 30
    g = bars(prices)
    s = int(g.index[6])
    base = Fixed("ruleZ_1h", {s}, side="short", exit_rule="1h")
    inv = InverseTrader(base, {"valid_mean": -0.008})
    eng = Engine(cfg, [inv], con)
    eng.tick(ctx_for(cfg, con, g, s, [s]))
    eng.tick(ctx_for(cfg, con, g, int(g.index[-1]), []))
    side, ret = con.execute("SELECT side, ret_net FROM positions WHERE trader_id = 'inv_ruleZ_1h' AND book = 'C'").fetchone()
    assert side == "long" and ret > 0.05
    assert inv.kind == "inverse" and inv.params()["inverse_of"] == "ruleZ_1h"
    from okxrev.follow import eligible
    assert not eligible("inv_ruleZ_1h")


def test_take_profit_short_and_long_and_stop_first(tmp_path):
    cfg = make_cfg(tmp_path)
    con = paper.connect(cfg)
    prices = [100.0] * 8 + [100.0, 95.0, 87.0] + [87.0] * 20
    g = bars(prices)
    s = int(g.index[6])
    eng = Engine(cfg, [Fixed("sh", {s}, side="short", exit_rule="7d_tp12_sl50"), Fixed("lg", {s}, side="long", exit_rule="7d_tp12_sl50")], con)
    eng.tick(ctx_for(cfg, con, g, s, [s]))
    tp = dict(con.execute("SELECT trader_id, tp_px FROM positions WHERE book='C'").fetchall())
    assert tp["sh"] == pytest.approx(0.12)
    eng.tick(ctx_for(cfg, con, g, int(g.index[-1]), []))
    r = {t: (why, px, ret) for t, why, px, ret in con.execute("SELECT trader_id, exit_reason, exit_px, ret_net FROM positions WHERE book='C'").fetchall()}
    assert r["sh"][0] == "tp" and r["sh"][1] == pytest.approx(88.0) and r["sh"][2] > 0.11
    assert con.execute("SELECT state FROM positions WHERE book='C' AND trader_id='lg'").fetchone()[0] == "open"
    ts = np.arange(3, dtype=np.int64)
    out = paper.walk_position("short", 100.0, 150.0, None, 10, -1, ts, np.array([101.0, 160.0, 101.0]), np.array([99.0, 80.0, 99.0]),
                              np.array([100.0, 100.0, 100.0]), None, 0.0, 0.0, 0.0, tp_px=88.0)
    assert out[2] == "stop" and out[1] == 150.0


def test_migrate_adds_tp_column(tmp_path):
    import duckdb
    p = tmp_path / "old.duckdb"
    c = duckdb.connect(str(p))
    c.execute("CREATE TABLE positions (pos_id VARCHAR PRIMARY KEY, state VARCHAR)")
    c.close()
    c = duckdb.connect(str(p))
    paper.migrate(c)
    assert "tp_px" in [r[0] for r in c.execute("DESCRIBE positions").fetchall()]
    paper.migrate(c)
