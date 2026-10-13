import json

import pytest

from okxrev import chat, chat_tools as T, live
from okxrev.config import Paths, load_config

GRID = {"algoId": "g1", "algoOrdType": "contract_grid", "instId": "CT-USDT-SWAP", "direction": "long", "lever": "10", "investment": "302",
        "cTime": "1791875469331", "totalPnl": "-18.6", "gridProfit": "18.1", "floatProfit": "-36.7", "pnlRatio": "-0.06",
        "minPx": "0.3281", "maxPx": "0.4011", "gridNum": "200", "arbitrageNum": "40", "liqPx": "0.1606", "state": "running"}
POS = {"algoId": "g1", "avgPx": "0.3648", "pos": "264", "liqPx": "0.1606", "last": "0.3690", "markPx": "0.3688", "mgnRatio": "26.3",
       "notionalUsd": "971.4", "upl": "9.49", "mgnMode": "cross"}


class FakeClient:
    def __init__(self, grids=(GRID,), fail=()):
        self.grids, self.fail, self.calls = list(grids), set(fail), []

    def get_private(self, path, params):
        self.calls.append((path, dict(params)))
        if path in self.fail:
            raise RuntimeError("boom")
        if path == live.GRID_PENDING:
            return [g for g in self.grids if g["algoOrdType"] == params["algoOrdType"]]
        if path == live.GRID_POSITIONS:
            return [POS]
        return []

    def close(self):
        pass


@pytest.fixture(autouse=True)
def clear_cache():
    live._bots_cache.update(t=0.0, data=None)
    yield
    live._bots_cache.update(t=0.0, data=None)


@pytest.fixture()
def cfg(tmp_path):
    c = load_config()
    c.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    (tmp_path / "r").mkdir()
    return c


def test_running_bots_snapshot_and_cache(cfg):
    fc = FakeClient()
    s = live.running_bots(cfg, fc, now_ms=1791875469331 + 4 * 3_600_000)
    b = s["bots"][0]
    assert b["inst_id"] == "CT-USDT-SWAP" and b["direction"] == "long" and b["lever"] == 10
    assert b["total_pnl"] == pytest.approx(-18.6) and b["grid_profit"] == pytest.approx(18.1) and b["float_profit"] == pytest.approx(-36.7)
    assert b["pos_avg_px"] == pytest.approx(0.3648) and b["pos_size"] == 264 and b["liq_px"] == pytest.approx(0.1606)
    assert b["in_range"] == "區間內" and b["hours"] == pytest.approx(4.0) and b["liq_dist"] == pytest.approx(1 - 0.1606 / 0.369)
    n = len(fc.calls)
    live.running_bots(cfg, fc)
    assert len(fc.calls) == n
    brief = live.bots_brief(s)
    assert "CT-USDT-SWAP 合約網格 做多 10x" in brief and "強平 0.1606" in brief and "區間內" in brief


def test_running_bots_partial_failure(cfg):
    s = live.running_bots(cfg, FakeClient(fail={live.GRID_POSITIONS}), refresh=True)
    assert s["bots"][0]["pos_avg_px"] is None and s["bots"][0]["liq_px"] == pytest.approx(0.1606)
    assert any("網格持倉" in e for e in s["errors"])
    s2 = live.running_bots(cfg, FakeClient(fail={live.GRID_PENDING}), refresh=True)
    assert s2["bots"] == [] and len(s2["errors"]) >= 2


def test_chat_bots_tool_live_first(cfg, monkeypatch):
    (cfg.paths.results / "bots.json").write_text(json.dumps({"n": 2, "pnl": -5.0, "bots": [
        {"algo_id": "g1", "inst_id": "CT-USDT-SWAP", "state": "running", "pnl": -1.0},
        {"algo_id": "old", "inst_id": "CT-USDT-SWAP", "state": "stopped", "pnl": -4.0}]}), encoding="utf-8")
    box = T.Toolbox(cfg)
    monkeypatch.setattr(box, "priv", lambda: FakeClient())
    txt, ok = T.call(box, "bots", {"inst": "CT"})
    d = json.loads(txt)
    assert ok and d["running_now_live"][0]["total_pnl"] == pytest.approx(-18.6) and d["running_now_live"][0]["algo_id"] == "g1"
    assert [x.get("pnl") for x in d["history_bots"]] == [-4.0]
    off = T.Toolbox(cfg, allow_live=False)
    d2 = json.loads(T.call(off, "bots", {})[0])
    assert d2["running_now_live"] == [] and len(d2["history_bots"]) == 2


def test_chat_system_context(cfg, monkeypatch):
    box = T.Toolbox(cfg)
    monkeypatch.setattr(box, "priv", lambda: FakeClient())
    monkeypatch.setattr("okxrev.config.load_credentials", lambda root=None: None)
    assert chat.live_context(cfg, box) == ""
    monkeypatch.setattr("okxrev.config.load_credentials", lambda root=None: ("k", "s", "p"))
    ctx = chat.live_context(cfg, box)
    assert "運行中的機器人" in ctx and "CT-USDT-SWAP" in ctx
    live._bots_cache.update(t=0.0, data=None)
    monkeypatch.setattr(box, "priv", lambda: FakeClient(grids=()))
    assert "沒有運行中" in chat.live_context(cfg, box)
    assert chat.live_context(cfg, T.Toolbox(cfg, allow_live=False)) == ""
