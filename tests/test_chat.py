import inspect
import json

import numpy as np
import pandas as pd
import pytest

from okxrev import chat, chat_tools as T, db
from okxrev.config import Paths, load_config

from test_detector_metrics import BAR_MS, btc_df, pumped


@pytest.fixture()
def cfg(tmp_path):
    c = load_config().copy_with({"chat": {"max_rounds": 3, "max_calls": 5, "max_tool_chars": 400, "daily_calls": 100},
                                 "replay": {"db": str(tmp_path / "old.duckdb")},
                                 "paper": {"db": str(tmp_path / "paper.duckdb")}})
    c.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    (tmp_path / "r").mkdir()
    (tmp_path / ".env").write_text("LLM_BASE_URL=http://x/v1\nLLM_API_KEY=k\nLLM_MODEL=m\n", encoding="utf-8")
    with db.open_db(c.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "BTC-USDT-SWAP"}, {"inst_id": "AAA-USDT-SWAP"}])
        db.write_batch(con, "BTC-USDT-SWAP", btc_df(n=3000))
        db.write_batch(con, "AAA-USDT-SWAP", pumped(n=3000))
    return c


def test_norm_inst():
    assert T.norm_inst("ct") == "CT-USDT-SWAP" and T.norm_inst("CT-USDT") == "CT-USDT-SWAP" and T.norm_inst("BTC-USD-SWAP") == "BTC-USD-SWAP"
    for bad in ("", "a;drop", "../x"):
        with pytest.raises(T.ToolError):
            T.norm_inst(bad)


def test_tools_are_read_only():
    src = inspect.getsource(T.Toolbox)
    for bad in ("INSERT", "UPDATE ", "DELETE", "DROP", "/trade/order", "/api/v5/trade", "post(", "write_text", "to_csv", "unlink"):
        assert bad not in src, bad
    assert set(T.TOOLS) <= {n for n, _ in inspect.getmembers(T.Toolbox, inspect.isfunction)}
    for s in T.schemas():
        assert s["function"]["parameters"]["type"] == "object"


def test_price_summary_and_truncation(cfg):
    box = T.Toolbox(cfg, allow_live=False)
    t0 = int(pumped(n=3000)["ts"].iloc[0])
    txt, ok = T.call(box, "price_summary", {"inst": "AAA", "start": t0, "end": t0 + 3000 * BAR_MS, "bar": "5m", "max_rows": 200})
    d = json.loads(txt)
    assert ok and d["inst"] == "AAA-USDT-SWAP" and len(d["bars"]) <= 120 and d["bar"] in ("1h", "4h")
    assert d["high"] >= d["low"] and d["high_at"].endswith("UTC")
    small, ok2 = T.call(box, "price_summary", {"inst": "AAA", "start": t0, "end": t0 + 3000 * BAR_MS}, max_chars=200)
    assert ok2 and len(small) <= 200 and "已截斷" in small
    bad, ok3 = T.call(box, "price_summary", {"inst": "ZZZ", "start": "2026-01-01"})
    assert not ok3 and "沒有 K 線" in json.loads(bad)["error"]
    err, ok4 = T.call(box, "nope", {})
    assert not ok4
    off, ok5 = T.call(box, "live_market", {"inst": "AAA"})
    assert not ok5 and "停用" in off
    x, ok6 = T.call(box, "price_summary", {"inst": "AAA", "start": "yesterday-ish"})
    assert not ok6 and "時間格式" in x


def test_simulate_trade(cfg):
    box = T.Toolbox(cfg, allow_live=False)
    df = pumped(n=3000)
    t = int(df["ts"].iloc[650])
    d = json.loads(T.call(box, "simulate_trade", {"inst": "AAA", "side": "long", "entry_time": t, "stop_pct": 0.05, "hold_hours": 2, "lever": 1})[0])
    assert d["exit_reason"] in ("止損", "時間到", "止盈") and d["mfe"] >= 0 and d["mae"] >= 0
    s = json.loads(T.call(box, "simulate_trade", {"inst": "AAA", "side": "short", "entry_time": int(df["ts"].iloc[600]), "hold_hours": 10, "lever": 20})[0])
    assert s["exit_reason"] == "強平" and s["return_on_margin"] == -1.0


def test_private_tools_respect_flag(cfg):
    box = T.Toolbox(cfg, allow_private=False, allow_live=False)
    for name in ("my_trades", "trading_review", "bots", "positions"):
        txt, ok = T.call(box, name, {})
        assert not ok and "未授權" in json.loads(txt)["error"]


class FakeResp:
    def __init__(self, chunks):
        self.chunks = chunks
        self.status_code = 200
        self.text = ""

    def iter_lines(self, decode_unicode=False):
        for c in self.chunks:
            yield ("data: " + json.dumps(c)).encode()
        yield b"data: [DONE]"


def text_chunks(s):
    return [{"choices": [{"delta": {"content": s[i:i + 5]}}]} for i in range(0, len(s), 5)] + [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]


def tool_chunks(calls):
    out = []
    for i, (cid, name, args) in enumerate(calls):
        a = json.dumps(args)
        out.append({"choices": [{"delta": {"tool_calls": [{"index": i, "id": cid, "type": "function", "function": {"name": name, "arguments": a[:4]}}]}}]})
        out.append({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": a[4:]}}]}}]})
    out.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    return out


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.bodies = []

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        import copy
        self.bodies.append(copy.deepcopy(json))
        return FakeResp(self.script.pop(0))


def test_run_turn_with_parallel_tools(cfg):
    sess = FakeSession([
        tool_chunks([("c1", "price_summary", {"inst": "AAA", "start": "2023-11-14"}), ("c2", "pump_events", {"inst": "AAA"})]),
        text_chunks("AAA 在 2023-11-15 08:00 UTC 附近暴漲後回落。"),
    ])
    box = T.Toolbox(cfg, allow_live=False)
    evs = list(chat.run_turn(cfg, None, "分析 AAA", inst="AAA-USDT-SWAP", model="m", session=sess, toolbox=box))
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "conv" and kinds.count("tool_start") == 2 and kinds.count("tool_end") == 2 and kinds[-1] == "done"
    assert "".join(e["text"] for e in evs if e["type"] == "text").startswith("AAA")
    second = sess.bodies[1]["messages"]
    assert [m["role"] for m in second][-3:] == ["assistant", "tool", "tool"]
    assert "AAA-USDT-SWAP" in sess.bodies[0]["messages"][0]["content"] and sess.bodies[0]["tools"]
    cid = evs[0]["id"]
    tr = chat.transcript(cfg, cid)
    assert tr["conversation"]["inst"] == "AAA-USDT-SWAP" and [m["role"] for m in tr["messages"]] == ["user", "assistant", "tool", "tool", "assistant"]
    sess2 = FakeSession([text_chunks("好的。")])
    evs2 = list(chat.run_turn(cfg, cid, "謝謝", model="m", session=sess2, toolbox=box))
    assert evs2[-1]["type"] == "done"
    assert len(sess2.bodies[0]["messages"]) == 1 + 5 + 1


def test_empty_tool_args_become_object(cfg):
    assert chat.normalize_args("") == "{}" and chat.normalize_args(None) == "{}" and chat.normalize_args("[1]") == "{}"
    assert chat.normalize_args('{"a": 1}') == '{"a": 1}' and chat.normalize_args("{bad") == "{}"
    sess = FakeSession([[{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "p", "type": "function", "function": {"name": "positions", "arguments": ""}}]}}]},
                         {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]])
    done = list(chat.stream_completion(cfg, [], None, "m", sess))[-1]
    assert done["tool_calls"][0]["function"]["arguments"] == "{}"


def test_budget_forces_answer_and_continue(cfg):
    calls = [("x%d" % i, "pump_events", {"inst": "AAA"}) for i in range(1)]
    sess = FakeSession([tool_chunks(calls), tool_chunks(calls), tool_chunks(calls), text_chunks("先根據現有資料回答。")])
    box = T.Toolbox(cfg, allow_live=False)
    evs = list(chat.run_turn(cfg, None, "深入分析", model="m", session=sess, toolbox=box))
    done = evs[-1]
    assert done["type"] == "done" and done["exhausted"] and done["rounds"] == 3
    assert "tools" not in sess.bodies[-1] and "查詢額度已用完" in sess.bodies[-1]["messages"][-1]["content"]
    assert any(e["type"] == "notice" for e in evs)
    sess2 = FakeSession([text_chunks("補充完成。")])
    evs2 = list(chat.run_turn(cfg, done["id"], None, model="m", cont=True, session=sess2, toolbox=box))
    assert evs2[-1]["type"] == "done"
    assert "請繼續查詢" in [m for m in sess2.bodies[0]["messages"] if m["role"] == "user"][-1]["content"]


def test_char_budget(cfg):
    """After a large tool result exceeds max_tool_chars (400), the next round must answer without tools."""
    t0 = int(pumped(n=3000)["ts"].iloc[0])
    sess = FakeSession([tool_chunks([("a", "price_summary", {"inst": "AAA", "start": t0, "end": t0 + 3000 * BAR_MS})]), text_chunks("ok")])
    box = T.Toolbox(cfg, allow_live=False)
    evs = list(chat.run_turn(cfg, None, "q", model="m", session=sess, toolbox=box))
    ends = [e for e in evs if e["type"] == "tool_end"]
    assert ends and ends[0]["chars"] > 400
    assert evs[-1]["exhausted"] and evs[-1]["calls"] == 1 and "tools" not in sess.bodies[-1]


def test_web_chat(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from okxrev.web.app import create_app

    def fake_turn(cfg_, cid, text, inst, model, cont):
        yield {"type": "conv", "id": "abc123"}
        yield {"type": "text", "text": f"{inst}|{text}"}
        yield {"type": "done", "id": "abc123", "rounds": 0, "calls": 0, "chars": 0, "exhausted": False}

    monkeypatch.setattr(chat, "run_turn", fake_turn)
    c = TestClient(create_app(cfg))
    assert c.post("/api/chat", json={"text": "hi"}).status_code == 403
    r = c.post("/api/chat", headers={"x-okxrev": "1"}, json={"text": "hi", "inst": "ct"})
    evs = [json.loads(x[5:]) for x in r.text.split("\n\n") if x.startswith("data:")]
    assert evs[1]["text"] == "CT-USDT-SWAP|hi" and evs[-1]["type"] == "done"
    assert c.post("/api/chat", headers={"x-okxrev": "1"}, json={"text": "hi", "inst": "x;y"}).status_code == 400
    assert c.post("/api/chat", headers={"x-okxrev": "1"}, json={"text": "hi", "conv_id": "../x"}).status_code == 400
    assert c.post("/api/chat", headers={"x-okxrev": "1"}, json={}).status_code == 400
    info = c.get("/api/chat/info").json()
    assert info["enabled"] and len(info["tools"]) == len(T.TOOLS)
    assert c.get("/api/chat/conversations").json() == []
    assert c.get("/api/chat/conversation?id=zz").status_code == 400
