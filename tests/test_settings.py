import json
import os

import pytest
from fastapi.testclient import TestClient

from okxrev import settings as S
from okxrev.config import Paths, load_config


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "config.yaml"
    src.write_text(load_config().source.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text("OKX_API_KEY=key-ABC123\nOKX_API_SECRET=sec-XYZ789\nOKX_API_PASSPHRASE=pass-QQQ\n", encoding="utf-8")
    os.environ.pop("OKXREV_NO_SETTINGS", None)
    yield tmp_path, src
    os.environ["OKXREV_NO_SETTINGS"] = "1"


def test_validate_types_and_ranges():
    assert S.validate("heat.top_n", "12") == 12
    assert S.validate("heat.watchlist", "ct, pump-usdt-swap ct") == ["CT-USDT-SWAP", "PUMP-USDT-SWAP"]
    assert S.validate("heat.paper_thresholds", [90, 80, 90]) == [80, 90]
    assert S.validate("paper.kinds.heat", False) is False
    for path, bad in (("heat.top_n", -1), ("heat.top_n", 1.5), ("heat.top_n", "x"), ("sync.oi5m_scope", "some"),
                      ("paper.kinds.heat", "no"), ("heat.watchlist", ["bad;rm"]), ("heat.paper_thresholds", []),
                      ("paths.db", "x"), ("okx.base_url", "http://evil")):
        with pytest.raises(S.SettingsError):
            S.validate(path, bad)


def test_overrides_apply_and_reset(env):
    root, src = env
    before = src.read_text(encoding="utf-8")
    base = load_config(src, root=root)
    assert base.raw["heat"]["top_n"] == 10
    S.save(root, {"heat.top_n": 3, "paper.kinds.heat": False, "chat.model": "gpt"})
    c = load_config(src, root=root)
    assert c.raw["heat"]["top_n"] == 3 and c.raw["paper"]["kinds"]["heat"] is False and c.raw["chat"]["model"] == "gpt"
    assert c._cache["base_raw"]["heat"]["top_n"] == 10
    S.save(root, {"heat.top_n": None})
    assert load_config(src, root=root).raw["heat"]["top_n"] == 10
    assert src.read_text(encoding="utf-8") == before
    assert (root / ".env").read_text(encoding="utf-8").startswith("OKX_API_KEY=key-ABC123")
    with pytest.raises(S.SettingsError):
        S.save(root, {"heat.top_n": 5, "heat.alert_threshold": 5})
    assert load_config(src, root=root).raw["heat"].get("top_n") == 10


def test_kinds_switch_disables_traders(env):
    from okxrev import traders as T
    root, src = env
    S.save(root, {"paper.kinds.heat": False})
    cfg = load_config(src, root=root)
    cfg.paths = Paths(root=root, db=root / "okx.duckdb", logs=root / "l", results=root / "r", lock=root / "k")
    assert not any(t.kind == "heat" for t in T.build(cfg))
    S.save(root, {"paper.kinds.heat": None})
    cfg = load_config(src, root=root)
    cfg.paths = Paths(root=root, db=root / "okx.duckdb", logs=root / "l", results=root / "r", lock=root / "k")
    assert any(t.kind == "heat" for t in T.build(cfg))


def test_web_settings_api(env):
    from okxrev.web.app import create_app
    root, src = env
    cfg = load_config(src, root=root)
    cfg.paths = Paths(root=root, db=root / "okx.duckdb", logs=root / "l", results=root / "r", lock=root / "k")
    c = TestClient(create_app(cfg))
    h = {"x-okxrev": "1"}
    g = c.get("/api/settings").json()
    f = {x["path"]: x for x in g["fields"]}
    assert f["heat.top_n"]["value"] == 10 and not f["heat.top_n"]["overridden"] and g["status"]["okx_key"] is True
    blob = json.dumps(g, ensure_ascii=False)
    assert all(s not in blob for s in ("key-ABC123", "sec-XYZ789", "pass-QQQ"))
    assert c.post("/api/settings", json={"changes": {"heat.top_n": 4}}).status_code == 403
    assert c.post("/api/settings", headers=h, json={"changes": {"heat.top_n": 999}}).status_code == 400
    assert c.post("/api/settings", headers=h, json={"changes": {"paths.db": "x"}}).status_code == 400
    r = c.post("/api/settings", headers=h, json={"changes": {"heat.top_n": 4, "heat.watchlist": "ct"}}).json()
    assert r["ok"] and cfg.raw["heat"]["top_n"] == 4 and cfg.raw["heat"]["watchlist"] == ["CT-USDT-SWAP"]
    f = {x["path"]: x for x in c.get("/api/settings").json()["fields"]}
    assert f["heat.top_n"]["overridden"] and f["heat.top_n"]["default"] == 10
    c.post("/api/settings", headers=h, json={"changes": {"heat.top_n": None}})
    assert cfg.raw["heat"]["top_n"] == 10
