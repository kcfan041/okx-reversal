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


def test_pause_resume_flag_and_task(cfg, monkeypatch):
    calls = []
    set_task = lambda en: calls.append(en) or (True, "")
    t = [1_000_000_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: t[0])
    r = collect.pause(cfg, "flag", "test", set_task)
    assert r["paused"] and r["mode"] == "flag" and calls == [] and collect.is_paused(cfg)
    t[0] += 30 * 3_600_000
    r = collect.pause(cfg, "task", "test", set_task)
    assert calls == [False] and r["mode"] == "task" and r["since"] == 1_000_000_000_000 and r["hours"] == pytest.approx(30)
    assert [f["name"] for f in r["lost"]] == ["多空比", "爆倉紀錄", "OI 5 分鐘"]
    r = collect.resume(cfg, set_task)
    assert calls == [False, True] and not r["paused"] and not collect.is_paused(cfg) and "無法補回" in r["message"]
    with pytest.raises(ValueError):
        collect.pause(cfg, "nope")
    collect.pause(cfg, "flag")
    collect.resume(cfg, set_task, lambda: {"state": "Ready"})
    assert calls == [False, True]
    collect.resume(cfg, set_task, lambda: {"state": "Disabled"})
    assert calls == [False, True, True]


def test_task_permission_error_explained(cfg):
    denied = lambda en: (False, "Disable-ScheduledTask : Access is denied.\nmore")
    r = collect.pause(cfg, "task", "t", denied)
    assert r["paused"] and "系統管理員" in r["message"] and "旗標暫停" in r["message"]
    r = collect.resume(cfg, denied)
    assert not r["paused"] and "系統管理員" in r["message"]


def test_old_plain_flag_file_still_works(cfg):
    collect.flag_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    collect.flag_path(cfg).write_text("paused from web UI\n", encoding="utf-8")
    s = collect.state(cfg)
    assert s["paused"] and s["mode"] == "flag" and s["hours"] >= 0


def test_sync_and_paper_skip_when_paused(cfg, monkeypatch):
    collect.pause(cfg, "flag")
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
    calls = []
    monkeypatch.setattr(tasks, "set_task_enabled", lambda en: calls.append(en) or (True, ""))
    monkeypatch.setattr(tasks, "task_info", lambda: {"registered": True, "state": "Ready"})
    monkeypatch.setattr(tasks, "start_sync", lambda c, only=None, force=False: {"ok": True, "message": f"started force={force}"})
    with db.open_db(cfg.paths.db):
        pass
    c = TestClient(create_app(cfg))
    h = {"x-okxrev": "1"}
    assert c.get("/api/collect").json()["paused"] is False
    assert c.post("/api/collect/pause?mode=bad", headers=h).status_code == 400
    r = c.post("/api/collect/pause?mode=task", headers=h).json()
    assert r["paused"] and calls == [False]
    r = c.post("/api/task/run", headers=h).json()
    assert r["needs_confirm"] and not r["ok"]
    assert c.post("/api/task/run?force=true", headers=h).json()["message"] == "started force=True"
    assert c.post("/api/paper/tick", headers=h).json()["needs_confirm"]
    r = c.post("/api/collect/resume", headers=h).json()
    assert not r["paused"] and calls == [False, True]
    assert c.post("/api/task/run", headers=h).json()["message"] == "started force=False"
