import pandas as pd
import pytest
from fastapi.testclient import TestClient

from okxrev import account, bots

from test_account import H, T0, env  # noqa: F401
from test_detector_metrics import BAR_MS, EV

GRID = {"algoId": "g1", "algoOrdType": "contract_grid", "instType": "SWAP", "instId": "AAA-USDT-SWAP", "direction": "long",
        "lever": "10", "investment": "100", "cTime": str(T0 + (EV - 10) * BAR_MS), "uTime": str(T0 + (EV + 20) * BAR_MS),
        "state": "stopped", "totalPnl": "-50", "gridProfit": "5", "fee": "-1", "fundingFee": "0", "pnlRatio": "-0.5",
        "minPx": "190", "maxPx": "210", "gridNum": "10", "stopType": "1",
        "triggerParams": [{"triggerAction": "stop", "triggerTime": str(T0 + (EV + 20) * BAR_MS)}]}
DCA = {"algoId": "d1", "algoOrdType": "contract_dca", "instId": "AAA-USDT-SWAP", "direction": "short", "lever": "5",
       "investmentAmt": "40", "cTime": str(T0 + 100 * BAR_MS), "stopTime": str(T0 + 110 * BAR_MS), "state": "stopped",
       "totalPnl": "8", "uTime": str(T0 + 110 * BAR_MS)}
SUB = {"algoId": "g1", "ordId": "o1", "instId": "AAA-USDT-SWAP", "side": "buy", "ordType": "limit", "strategyOrderType": "grid",
       "px": "195", "avgPx": "195", "accFillSz": "1", "ctVal": "1", "fee": "-0.1", "pnl": "", "cTime": "1", "uTime": "2"}
COPY = {"subPosId": "c1", "instType": "SWAP", "instId": "AAA-USDT-SWAP", "uniqueCode": "L1", "posSide": "short", "lever": "10",
        "openAvgPx": "180", "closeAvgPx": "170", "subPos": "1", "openTime": str(T0 + 600 * BAR_MS), "closeTime": str(T0 + 650 * BAR_MS),
        "pnl": "10", "fee": "-0.2", "fundingFee": "0"}


class FakeBotClient:
    def __init__(self, copy=True):
        self.calls = []
        self.copy = copy
        self.n_requests = 0

    def get_private(self, path, params):
        self.calls.append((path, dict(params)))
        if path.endswith("grid/orders-algo-history") and params.get("algoOrdType") == "contract_grid":
            return [GRID]
        if path.endswith("grid/orders-algo-details"):
            return [{**GRID, "floatProfit": "-55", "runPx": "200", "tradeNum": "7"}]
        if path.endswith("grid/sub-orders"):
            return [SUB]
        if path.endswith("dca/history-list") and params.get("algoOrdType") == "contract_dca":
            return [DCA]
        if path.endswith("recurring/orders-algo-history"):
            from okxrev.okx_client import OkxError
            raise OkxError("50001", "not available", path)
        if path.endswith("copytrading/subpositions-history") and params["instType"] == "SWAP" and self.copy:
            return [COPY]
        if path.endswith("copytrading/current-lead-traders"):
            return [{"uniqueCode": "L1", "nickName": "lead", "beginCopyTime": "1", "copyTotalAmt": "0", "copyTotalPnl": "0"}]
        return []


def test_sync_bots_and_copy(env):  # noqa: F811
    con = account.connect(env.account_db)
    c = FakeBotClient()
    r = bots.sync_bots(c, con)
    assert r["bots"] == 2 and r["orders"] == 1 and any("recurring" in e for e in r["errors"])
    row = con.execute("SELECT direction, lever, investment, stop_time, float_profit, run_px FROM bots WHERE algo_id = 'g1'").fetchone()
    assert row[0] == "long" and row[1] == 10 and row[2] == 100 and row[3] == T0 + (EV + 20) * BAR_MS and row[4] == -55 and row[5] == 200
    assert con.execute("SELECT investment FROM bots WHERE algo_id = 'd1'").fetchone()[0] == 40
    n_calls = len(c.calls)
    r2 = bots.sync_bots(c, con)
    assert r2["bots"] == 0 and r2["orders"] == 0
    assert not any(p.endswith("sub-orders") for p, _ in c.calls[n_calls:])
    rc = bots.sync_copy(c, con)
    assert rc["positions"] == 1 and rc["leaders"] == 1
    con.close()


def test_sources_and_copy_dedupe(env):  # noqa: F811
    con = account.connect(env.account_db)
    bots.sync_bots(FakeBotClient(), con)
    bots.sync_copy(FakeBotClient(), con)
    con.close()
    manual = account.load_trades(env)
    assert len(manual) == 4 and set(manual["source"]) == {"manual"}
    assert not manual["trade_id"].str.startswith("2-").any()
    allt = account.load_trades(env, sources=account.SOURCES)
    assert allt["source"].value_counts().to_dict() == {"manual": 4, "bot": 2, "copy": 1}
    b = allt[allt["source"] == "bot"].set_index("trade_id")
    assert b.loc["bot-g1", "pnl_usd"] == -50 and b.loc["bot-g1", "notional_usd"] == 1000
    cp = allt[allt["source"] == "copy"].iloc[0]
    assert cp["pnl_usd"] == pytest.approx(9.8)
    s = account.summarize_trades(allt)
    assert {r["key"] for r in s["by_source"]} == {"manual", "bot", "copy"}
    assert s["overall"]["pnl"] == pytest.approx(sum(r["pnl"] for r in s["by_source"]))


def test_bot_analysis(env):  # noqa: F811
    con = account.connect(env.account_db)
    bots.sync_bots(FakeBotClient(copy=False), con)
    res = bots.analyze(env, con)
    con.close()
    assert res["n"] == 2 and res["pnl"] == pytest.approx(-42)
    g = [x for x in res["bots"] if x["algo_id"] == "g1"][0]
    assert g["mkt_broke"] == "down" and g["trend_with"] < 0
    keys = {f["key"] for f in res["findings"]}
    assert {"grid_split", "worst", "range_break"} <= keys


def test_review_excludes_bots_and_web(env):  # noqa: F811
    con = account.connect(env.account_db)
    bots.sync_bots(FakeBotClient(copy=False), con)
    con.close()
    from okxrev import review
    rep = review.run(env)["report"]
    assert rep["n_trades"] == 5 and rep["bots"]["n"] == 2
    assert rep["all_sources"]["overall"]["n"] == 7
    from okxrev.web.app import create_app
    c = TestClient(create_app(env))
    assert c.get("/api/account/summary").json()["overall"]["n"] == 7
    assert c.get("/api/account/summary?source=manual").json()["overall"]["n"] == 5
    assert c.get("/api/account/summary?source=bot").json()["overall"]["n"] == 2
    assert c.get("/api/account/summary?source=evil").status_code == 400
    tr = c.get("/api/account/trades?source=manual").json()["trades"]
    assert all(t["source"] == "manual" for t in tr) and "mae" in tr[0]
    assert c.get("/api/account/bots").json()["n"] == 2
    st = c.get("/api/account/status").json()
    assert st["bots"] == 2
