import json

import pytest
from fastapi.testclient import TestClient

from okxrev import collect, db, paper, sync
from okxrev.config import Paths, load_config


@pytest.fixture()
def cfg(tmp_path):
    c = load_config().copy_with({"paper": {"db": str(tmp_path / "paper.duckdb")}})
    c.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    (tmp_path / "r").mkdir()
    return c


def test_pause_resume(cfg, monkeypatch):
    t = [1_000_000_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: t[0])
    r = collect.pause(cfg, "test")
    assert r["paused"] and collect.is_paused(cfg) and r["since"] == t[0]
    t[0] += 30 * 3_600_000
    r = collect.pause(cfg, "test")
    assert r["since"] == 1_000_000_000_000 and r["hours"] == pytest.approx(30)
    assert [f["name"] for f in r["lost"]] == ["多空比", "爆倉紀錄", "OI 5 分鐘"]
    r = collect.resume(cfg, lambda: {"registered": True, "state": "Ready"})
    assert not r["paused"] and not collect.is_paused(cfg) and "無法補回" in r["message"] and "系統管理員" not in r["message"]


def test_resume_warns_when_task_disabled(cfg):
    collect.pause(cfg)
    r = collect.resume(cfg, lambda: {"registered": True, "state": "Disabled"})
    assert not r["paused"] and "Enable-ScheduledTask" in r["message"]


def test_never_touches_scheduled_task():
    import inspect
    from okxrev.web import tasks
    assert "Disable-ScheduledTask" not in inspect.getsource(collect) or "Enable-ScheduledTask" in collect.TASK_DISABLED_HINT
    assert not hasattr(tasks, "set_task_enabled")


def test_old_plain_flag_file_still_works(cfg):
    collect.flag_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    collect.flag_path(cfg).write_text("paused from web UI\n", encoding="utf-8")
    s = collect.state(cfg)
    assert s["paused"] and s["hours"] >= 0


def test_sync_and_paper_skip_when_paused(cfg, monkeypatch):
    collect.pause(cfg)
    spawned = []
    monkeypatch.setattr(sync, "spawn_paper", lambda c: spawned.append(1))
    assert sync.run_sync(cfg, 10, None, False, False) == 0 and spawned == []
    ran = []
    monkeypatch.setattr(paper, "run_tick", lambda *a, **k: ran.append(1) or {})
    monkeypatch.setattr(paper, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(paper, "setup_logging", lambda *a, **k: None)
    assert paper.main(["--tick", "--quiet"]) == 0 and ran == []
    assert paper.main(["--tick", "--quiet", "--force"]) == 0 and ran == [1]


def test_web_collect_endpoints(cfg, monkeypatch):
    from okxrev.web import tasks
    from okxrev.web.app import create_app
    monkeypatch.setattr(tasks, "task_info", lambda: {"registered": True, "state": "Ready"})
    monkeypatch.setattr(tasks, "start_sync", lambda c, only=None, force=False: {"ok": True, "message": f"started force={force}"})
    with db.open_db(cfg.paths.db):
        pass
    c = TestClient(create_app(cfg))
    h = {"x-okxrev": "1"}
    assert c.get("/api/collect").json()["paused"] is False
    assert c.post("/api/collect/pause", headers=h).json()["paused"]
    r = c.post("/api/task/run", headers=h).json()
    assert r["needs_confirm"] and not r["ok"]
    assert c.post("/api/task/run?force=true", headers=h).json()["message"] == "started force=True"
    assert c.post("/api/paper/tick", headers=h).json()["needs_confirm"]
    assert not c.post("/api/collect/resume", headers=h).json()["paused"]
    assert c.post("/api/task/pause", headers=h).json()["paused"]
    assert not c.post("/api/task/resume", headers=h).json()["paused"]
    assert c.post("/api/task/run", headers=h).json()["message"] == "started force=False"
