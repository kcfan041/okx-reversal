import pytest
from fastapi.testclient import TestClient

from okxrev import db
from okxrev.backtest import run_backtest
from okxrev.config import Paths, load_config
from okxrev.web.app import create_app

from test_detector_metrics import btc_df, pumped

H = {"x-okxrev": "1"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = load_config()
    cfg = cfg.copy_with({"backtest": {"baseline": {"random_samples": 20}, "bootstrap": {"iterations": 20}}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "data" / "t.duckdb", logs=tmp_path / "logs",
                      results=tmp_path / "results", lock=tmp_path / "data" / "sync.lock")
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "BTC-USDT-SWAP"}, {"inst_id": "AAA-USDT-SWAP"}])
        db.write_batch(con, "BTC-USDT-SWAP", btc_df())
        db.write_batch(con, "AAA-USDT-SWAP", pumped())
        db.record_run(con, {"started_at": 1, "duration_sec": 2.0, "requests": 3, "rows_written": 4,
                            "forward_insts": 2, "backfill_pages": 0, "errors": 0, "note": "ok"})
    cfg.paths.logs.mkdir()
    (cfg.paths.logs / "sync.log").write_text("line1\nline2 WARNING x\n", encoding="utf-8")
    import json
    res = run_backtest(cfg, None, None, None, sweep=True)
    cfg.paths.results.mkdir()
    res["events"].to_csv(cfg.paths.results / "events.csv", index=False)
    res["sweep"].to_csv(cfg.paths.results / "sweep.csv", index=False)
    from okxrev.backtest import _json_default
    (cfg.paths.results / "summary.json").write_text(json.dumps(res["summary"], default=_json_default), encoding="utf-8")
    monkeypatch.setattr("okxrev.web.tasks.task_info", lambda: {"registered": False, "error": "test"})
    return TestClient(create_app(cfg)), cfg


def test_index_and_static(client):
    c, _ = client
    assert "okx-reversal" in c.get("/").text
    assert c.get("/static/app.js").status_code == 200


def test_status_and_runs(client):
    c, _ = client
    s = c.get("/api/status").json()
    assert s["candles"] > 0 and s["n_live"] == 2 and len(s["layers"]) == 4
    assert "gaps" in c.get("/api/status?gaps=true").json()
    assert c.get("/api/runs").json()[0]["rows_written"] == 4
    assert c.get("/api/log").json()["lines"] == ["line1", "line2 WARNING x"]
    assert c.get("/api/log?name=../x").status_code == 400


def test_post_requires_header(client):
    c, cfg = client
    assert c.post("/api/task/pause").status_code == 403
    assert c.post("/api/task/pause", headers={**H, "origin": "http://evil.com"}).status_code == 403
    r = c.post("/api/task/pause", headers=H).json()
    assert r["paused"] and (cfg.paths.db.parent / "PAUSE").exists()
    assert c.get("/api/task").json()["paused"] is True
    c.post("/api/task/resume", headers=H)
    assert not (cfg.paths.db.parent / "PAUSE").exists()


def test_bad_host_rejected(client):
    c, _ = client
    assert c.get("/api/status", headers={"host": "evil.com"}).status_code == 403


def test_input_validation(client):
    c, _ = client
    assert c.post("/api/sync/only", headers=H, json={"insts": ["../../x"]}).status_code == 400
    assert c.post("/api/backtest", headers=H, json={"since": "2026-01-01; rm"}).status_code == 400
    assert c.get("/api/candles?inst=bad").status_code == 400
    assert c.get("/charts/..%2Fx.png").status_code in (400, 404)


def test_results_and_candles(client):
    c, _ = client
    s = c.get("/api/results/summary").json()
    assert s["modes"]["pump_red"]["n_events"] == 1
    assert len(c.get("/api/results/sweep").json()) == 12
    ev = c.get("/api/results/events?mode=pump_red").json()
    assert ev["total"] == 1
    d = c.get("/api/candles?inst=AAA-USDT-SWAP").json()
    assert len(d["t"]) == len(d["o"]) > 0 and "btc" in d
    insts = {i["inst_id"] for i in c.get("/api/instruments").json()}
    assert insts == {"BTC-USDT-SWAP", "AAA-USDT-SWAP"}


def test_paused_sync_exits(client):
    _, cfg = client
    from okxrev.sync import run_sync
    (cfg.paths.db.parent / "PAUSE").write_text("x")
    assert run_sync(cfg, 10, None, False, False) == 0


def test_second_web_server_does_not_start(monkeypatch, capsys):
    import socket
    from okxrev.web import app as webapp
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    port = s.getsockname()[1]
    try:
        assert webapp.port_in_use(port)
        called = []
        monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.append(1))
        assert webapp.main(["--no-browser", "--port", str(port)]) == 0
        assert called == [] and "已經有網頁伺服器" in capsys.readouterr().out
    finally:
        s.close()
    assert not webapp.port_in_use(port)


def test_index_busts_static_cache(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    import re
    assert re.search(r'/static/app\.js\?v=\d+"', r.text) and re.search(r'/static/style\.css\?v=\d+"', r.text)


def test_campaign_endpoints_without_data(client):
    c, _ = client
    r = c.get("/api/campaigns").json()
    assert r["live"] == [] and r["campaigns"] == [] and r["status"] == {"exists": False} and r["has_results"] is False
    assert c.get("/api/campaigns/results").json() is None
    assert c.get("/api/campaigns/job").json()["running"] is False
    assert c.post("/api/campaigns/evaluate").status_code == 403
    d = c.get("/api/candles", params={"inst": "AAA-USDT-SWAP"}).json()
    assert "campaigns" not in d or d["campaigns"] == []
