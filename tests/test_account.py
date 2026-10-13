import base64
import hashlib
import hmac
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from okxrev import account, db, review
from okxrev.config import Paths, load_config, load_credentials, read_dotenv
from okxrev.okx_client import Credentials, OkxClient, OkxError, sign_request
from okxrev.web.app import create_app

from test_detector_metrics import BAR_MS, EV, btc_df, pumped
from test_okx_client import CFG as OKX_CFG, FakeResp

H = {"x-okxrev": "1"}
T0 = 1_700_000_000_000


def test_sign_matches_hmac():
    ts, path = "2020-12-08T09:08:57.715Z", "/api/v5/account/positions-history?instType=SWAP&limit=100"
    exp = base64.b64encode(hmac.new(b"sec", (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    assert sign_request("sec", ts, "get", path) == exp


def test_dotenv_and_credentials(tmp_path):
    (tmp_path / ".env").write_text('# c\nOKX_API_KEY=k\nexport OKX_API_SECRET="s"\nOKX_API_PASSPHRASE=\'p\'\n', encoding="utf-8")
    assert read_dotenv(tmp_path / ".env")["OKX_API_SECRET"] == "s"
    assert load_credentials(tmp_path, environ={}) == ("k", "s", "p")
    assert load_credentials(tmp_path, environ={"OKX_API_KEY": "env"})[0] == "env"
    assert load_credentials(tmp_path / "none", environ={}) is None


def test_private_get_sends_headers():
    seen = {}

    class S:
        headers = {}

        def get(self, url, params=None, timeout=None, headers=None):
            seen.update(headers or {})
            return FakeResp(200, {"code": "0", "data": []})

    c = OkxClient(OKX_CFG, credentials=Credentials("k", "s", "p"), sleep=lambda d: None)
    c.session = S()
    c.get_private("/api/v5/account/positions-history", {"instType": "SWAP"})
    assert seen["OK-ACCESS-KEY"] == "k" and seen["OK-ACCESS-PASSPHRASE"] == "p" and seen["OK-ACCESS-SIGN"]
    assert repr(Credentials("k", "s", "p")).find("'s'") == -1
    c2 = OkxClient(OKX_CFG)
    with pytest.raises(OkxError):
        c2._auth_headers("/x", {})


def pos(pos_id, inst, side, c_time, u_time, open_px, close_px, sz, pnl, fee=-0.1, lever=5, typ="2"):
    return {"posId": pos_id, "instType": "SWAP", "instId": inst, "mgnMode": "isolated", "type": typ, "posSide": "net",
            "direction": side, "lever": str(lever), "openAvgPx": str(open_px), "closeAvgPx": str(close_px),
            "openMaxPos": str(sz), "closeTotalPos": str(sz), "pnl": str(pnl), "realizedPnl": str(pnl + fee),
            "fee": str(fee), "fundingFee": "0", "liqPenalty": "0", "pnlRatio": "0", "ccy": "USDT", "uly": inst[:-5],
            "cTime": str(c_time), "uTime": str(u_time)}


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.n_requests = 0

    def get_private(self, path, params):
        self.calls.append((path, dict(params)))
        self.n_requests += 1
        key = (path, params.get("instType"), params.get("after"))
        return self.pages.get(key, [])

    def get(self, path, params):
        self.n_requests += 1
        if params["instType"] == "SWAP":
            return [{"instId": "AAA-USDT-SWAP", "ctVal": "1", "ctType": "linear", "ctValCcy": "AAA", "settleCcy": "USDT"}]
        return []

    def close(self):
        pass


def test_sync_positions_pages_and_dedupes(tmp_path):
    p1 = [pos(str(i), "AAA-USDT-SWAP", "long", 1000 + i, 2000 + i, 100, 101, 1, 1) for i in (9, 8)]
    p2 = [pos("7", "AAA-USDT-SWAP", "long", 1007, 2007, 100, 101, 1, 1)]
    pages = {(account.POS_PATH, "SWAP", None): p1, (account.POS_PATH, "SWAP", "2009"): p2}
    con = account.connect(tmp_path / "a.duckdb")
    c = FakeClient(pages)
    assert account.sync_positions(c, con, "SWAP", 0, limit=2) == 3
    assert [x[1].get("after") for x in c.calls] == [None, "2009"]
    c2 = FakeClient(pages)
    assert account.sync_positions(c2, con, "SWAP", 0, limit=2) == 0
    assert len(c2.calls) == 1
    assert con.execute("SELECT COUNT(*) FROM positions_history").fetchone()[0] == 3


def test_spot_avg_cost():
    f = pd.DataFrame([
        {"inst_type": "SPOT", "bill_id": "1", "inst_id": "AAA-USDT", "side": "buy", "fill_px": 10.0, "fill_sz": 10.0, "fee": -0.1, "fee_ccy": "AAA", "ts": 1},
        {"inst_type": "SPOT", "bill_id": "2", "inst_id": "AAA-USDT", "side": "buy", "fill_px": 20.0, "fill_sz": 10.1, "fee": 0.0, "fee_ccy": "AAA", "ts": 2},
        {"inst_type": "SPOT", "bill_id": "3", "inst_id": "AAA-USDT", "side": "sell", "fill_px": 30.0, "fill_sz": 20.0, "fee": -0.6, "fee_ccy": "USDT", "ts": 3},
        {"inst_type": "SPOT", "bill_id": "4", "inst_id": "BBB-USDT", "side": "sell", "fill_px": 5.0, "fill_sz": 1.0, "fee": 0.0, "fee_ccy": "USDT", "ts": 4},
    ])
    t = account.spot_trades(f)
    a = t[t["inst_id"] == "AAA-USDT"].iloc[0]
    assert a["pnl_usd"] == pytest.approx(30 * 20 - (100 + 202) - 0.6)
    assert bool(a["cost_known"])
    b = t[t["inst_id"] == "BBB-USDT"].iloc[0]
    assert np.isnan(b["pnl_usd"]) and not bool(b["cost_known"])


def test_max_drawdown_and_summary():
    assert account.max_drawdown(np.array([1.0, 3.0, 0.0, 2.0])) == -3.0
    assert account.max_drawdown(np.array([-1.0, -2.0])) == -2.0


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = load_config()
    cfg = cfg.copy_with({"backtest": {"baseline": {"random_samples": 20}, "bootstrap": {"iterations": 50}},
                         "account": {"db": str(tmp_path / "data" / "account.duckdb"), "replicate": {"min_trades_per_group": 2}}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "data" / "t.duckdb", logs=tmp_path / "logs",
                      results=tmp_path / "results", lock=tmp_path / "data" / "sync.lock")
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "BTC-USDT-SWAP"}, {"inst_id": "AAA-USDT-SWAP"}, {"inst_id": "BBB-USDT-SWAP"}])
        db.write_batch(con, "BTC-USDT-SWAP", btc_df())
        db.write_batch(con, "AAA-USDT-SWAP", pumped())
        db.write_batch(con, "BBB-USDT-SWAP", pumped())
    t_ev = T0 + EV * BAR_MS
    rows = [
        pos("1", "AAA-USDT-SWAP", "short", t_ev + 2 * BAR_MS, t_ev + 40 * BAR_MS, 180, 180, 10, 0.0),
        pos("2", "AAA-USDT-SWAP", "long", T0 + 600 * BAR_MS, T0 + 650 * BAR_MS, 175, 190, 10, 150.0),
        pos("3", "AAA-USDT-SWAP", "long", T0 + 660 * BAR_MS, T0 + 699 * BAR_MS, 195, 199.5, 10, 45.0),
        pos("4", "AAA-USDT-SWAP", "long", t_ev - 1 * BAR_MS, t_ev + 30 * BAR_MS, 200, 180, 50, -1000.0, lever=50),
        pos("5", "AAA-USDT-SWAP", "long", t_ev + 32 * BAR_MS, t_ev + 34 * BAR_MS, 180, 179, 100, -100.0, lever=50),
    ]
    con = account.connect(cfg.account_db)
    account._insert(con, "positions_history", [account.pos_row(r) for r in rows])
    account._insert(con, "acct_instruments", [("AAA-USDT-SWAP", "SWAP", 1.0, "linear", "AAA", "USDT")])
    con.close()
    monkeypatch.setattr("okxrev.web.tasks.task_info", lambda: {"registered": False, "error": "test"})
    return cfg


def test_load_trades_and_enrich(env):
    t = account.load_trades(env)
    assert len(t) == 5
    big = t[t["trade_id"].str.startswith("4-")].iloc[0]
    assert big["side"] == "long" and big["notional_usd"] == pytest.approx(10000)
    assert big["pnl_usd"] == pytest.approx(-1000.1)
    et = review.enrich(env, t)
    assert et["path_ok"].all()
    e4 = et[et["trade_id"].str.startswith("4-")].iloc[0]
    assert e4["mae"] > 0.09 and e4["hold_bars"] == 31
    e5 = et[et["trade_id"].str.startswith("5-")].iloc[0]
    assert bool(e5["revenge"]) and e5["loss_streak_before"] == 1
    assert bool(e4["c_r24h>=+30%"])
    e1 = et[et["trade_id"].str.startswith("1-")].iloc[0]
    assert e1["bars_since_pump_red"] == 1


def test_no_lookahead_in_entry_features(env):
    t = account.load_trades(env)
    et1 = review.enrich(env, t)
    with db.open_db(env.paths.db) as con:
        df = pumped()
        df.loc[EV + 33:, ["open", "high", "low", "close"]] = 999.0
        db.write_batch(con, "AAA-USDT-SWAP", df)
    et2 = review.enrich(env, t)
    cols = [c for c in et1.columns if c.startswith(("f_", "c_"))] + ["bars_since_pump_red"]
    pd.testing.assert_frame_equal(et1[cols], et2[cols])


def test_report_and_replicate(env):
    res = review.run(env)
    rep = res["report"]
    assert rep["n_trades"] == 5
    keys = {i["key"] for i in rep["issues"]}
    assert "tail_loss" in keys or "high_lever" in keys
    assert rep["counterfactuals"]
    stop = [r for r in rep["counterfactuals"] if r["col"] == "stop_0.05"][0]
    assert stop["delta_usd"] > 0
    json.loads((env.paths.results / "review.json").read_text(encoding="utf-8"))
    from okxrev import replicate
    rules = [{"side": "long", "conds": ("r24h>=+30%",), "label": "x", "your_n": 2, "your_pnl": 1.0, "your_win_rate": 1.0,
              "your_avg_ret": 0.1, "hold_bars": 12, "stop": 0.05}]
    rr = replicate.run(env, res["enriched"], rules)
    r0 = rr["rules"][0]
    assert r0["market_all"]["n"] >= 2 and r0["verdict"] in ("可複製", "證據不足", "無效")
    assert (env.paths.results / "replicate.csv").exists()


def test_web_account(env):
    c = TestClient(create_app(env))
    assert c.get("/api/account/status").json()["credentials"] is False
    assert c.post("/api/account/run", headers=H, json={"sync": True}).status_code == 400
    s = c.get("/api/account/summary").json()
    assert s["overall"]["n"] == 5 and len(s["equity"]["t"]) == 5
    assert c.get("/api/account/summary?inst_type=SPOT").json()["overall"]["n"] == 0
    assert c.get("/api/account/summary?since=bad").status_code == 400
    assert c.get("/api/account/trades?side=long").json()["total"] == 4
    assert c.get("/api/account/review").json()["review"] is None
    assert "tab-ac" in c.get("/").text
