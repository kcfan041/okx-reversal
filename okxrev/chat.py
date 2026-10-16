"""Chat with an LLM that can call the read-only project tools (okxrev.chat_tools).

Loop per user message: send history + tool schemas -> the model streams text and/or tool calls -> run all tool calls
of that round (in parallel) -> append results -> repeat until the model answers without tools or a budget is hit
(rounds, calls, total tool-result characters). When a budget is hit the model is told to answer with what it has,
and the turn is marked so the UI can offer "continue". Conversations are stored in data/chat.duckdb.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator

import duckdb
import requests

from okxrev import db
from okxrev.chat_tools import Toolbox, call as call_tool, schemas
from okxrev.config import Config, ms_to_str

log = logging.getLogger("okxrev.chat")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id VARCHAR PRIMARY KEY, title VARCHAR, inst VARCHAR, model VARCHAR, created BIGINT, updated BIGINT
);
CREATE TABLE IF NOT EXISTS messages (
    conv_id VARCHAR, seq INTEGER, role VARCHAR, content VARCHAR, tool_calls VARCHAR, tool_call_id VARCHAR, name VARCHAR,
    ts BIGINT, meta VARCHAR, PRIMARY KEY (conv_id, seq)
);
CREATE TABLE IF NOT EXISTS tool_log (
    ts BIGINT, conv_id VARCHAR, name VARCHAR, args VARCHAR, ok BOOLEAN, chars INTEGER, ms INTEGER
);
"""

SYSTEM = """你是使用者的加密貨幣交易研究助理，連接到他的 okx-reversal 專案。你可以呼叫工具讀取：
OKX USDT 永續合約的 5 分 K 歷史（約 2024-09 起）、即時行情、指標與過熱分數、資金費率／OI／主動買賣／溢價／多空比／爆倉、
進場探索規則與盲測成績、紙上交易、回測事件、使用者真實的交易紀錄與操作診斷、機器人（網格、DCA、智能組合）、目前持倉與出場計畫，
以及「相似歷史情境」和交易試算。另外可即時抓 1 秒／1 分 K 線與逐筆成交，並使用「事件實驗室」：拆解急漲後爆跌事件、
找全市場相似事件、測試樣板、查詢使用者存的自訂規則。

原則：
1. 只根據工具查到的資料回答；查不到或資料不足時直接說明，不要猜測或編造數字。
2. 先用最少的工具拿到需要的資料；可以在同一回合同時呼叫多個工具。不確定能查什麼時先呼叫 project_overview。
3. 回答要具體：引用數字、時間（UTC）、樣本數；指出哪些結論樣本少或尚未通過盲測。
4. 盲測（2024-10～2025-09，策略從沒看過的資料）是目前最可信的證據；各項目前的證據等級見下方「目前證據摘要」（由結果檔自動產生），
   以它為準，不要沿用舊對話裡的結論。
5. 不下確定的買賣指令；被問到要不要進場時，列出支持與反對的證據、歷史上類似情況的結果、風險（槓桿、強平、止損距離），讓使用者自己判斷。
6. 使用者的交易習慣問題（過去診斷）：做空抱太短、高槓桿與強平、逆 BTC 方向、追高、做多選時機差。相關時可以提醒。
7. 用繁體中文回答，精簡、有條理；適合時用表格。時間一律標 UTC，格式 YYYY-MM-DD HH:MM（介面會把它變成可點擊開 K 線）。
8. 這些工具全部唯讀；你不能下單或修改任何設定。
9. 同一個數字若不同工具給出不同值（例如即時 vs 資料庫快照），說明差異來源，以即時為準。
10. 分析單一事件：先 analyze_event，再 find_similar_events 看歷史類似情況，必要時 test_template 調整條件；說明找參數期與驗證期的差別，
    提醒調條件次數越多越容易過度擬合；盲測只能由使用者在介面上按。
11. 機器人（網格、DCA）的現況以系統訊息裡的「運行中的機器人」和 bots 工具的 running_now_live 為準（即時查詢）；
    歷史同步資料可能落後幾小時。網格的總損益 = 網格收益 + 浮動損益；持倉的未實現損益（pos_upl）只看目前持倉相對均價，兩者不同。"""


def settings(cfg: Config) -> dict[str, Any]:
    from okxrev import llm
    c = {"model": "claude", "max_rounds": 12, "max_calls": 40, "max_tool_chars": 60000, "tool_result_chars": 12000,
         "daily_calls": 500, "timeout_sec": 180, "max_tokens": 4000, "history_messages": 40, **(cfg.raw.get("chat") or {})}
    c["llm"] = llm.settings(cfg)
    return c


def connect(cfg: Config, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = cfg.paths.root / "data" / "chat.duckdb"
    p.parent.mkdir(parents=True, exist_ok=True)
    if read_only and not p.exists():
        con = duckdb.connect(":memory:")
        con.execute(SCHEMA)
        return con
    con = duckdb.connect(str(p), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


# ---------------------------------------------------------------- storage
def new_conversation(con: duckdb.DuckDBPyConnection, inst: str | None, model: str, title: str = "新對話") -> str:
    cid = uuid.uuid4().hex[:12]
    now = db.now_ms()
    con.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)", [cid, title, inst, model, now, now])
    return cid


def add_message(con: duckdb.DuckDBPyConnection, cid: str, msg: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
    seq = con.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE conv_id = ?", [cid]).fetchone()[0]
    con.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [cid, seq, msg["role"], msg.get("content"), json.dumps(msg["tool_calls"], ensure_ascii=False) if msg.get("tool_calls") else None,
                 msg.get("tool_call_id"), msg.get("name"), db.now_ms(), json.dumps(meta, ensure_ascii=False) if meta else None])
    con.execute("UPDATE conversations SET updated = ? WHERE id = ?", [db.now_ms(), cid])


def load_messages(con: duckdb.DuckDBPyConnection, cid: str) -> list[dict[str, Any]]:
    rows = con.execute("SELECT role, content, tool_calls, tool_call_id, name, meta, ts FROM messages WHERE conv_id = ? ORDER BY seq", [cid]).fetchall()
    out = []
    for role, content, tc, tcid, name, meta, ts in rows:
        m: dict[str, Any] = {"role": role, "content": content}
        if tc:
            m["tool_calls"] = json.loads(tc)
        if tcid:
            m["tool_call_id"] = tcid
        if name:
            m["name"] = name
        if meta:
            m["_meta"] = json.loads(meta)
        m["_ts"] = ts
        out.append(m)
    return out


def list_conversations(con: duckdb.DuckDBPyConnection, limit: int = 50) -> list[dict[str, Any]]:
    return con.execute("SELECT id, title, inst, model, created, updated FROM conversations ORDER BY updated DESC LIMIT ?", [limit]).df().to_dict("records")


def calls_today(con: duckdb.DuckDBPyConnection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM tool_log WHERE ts >= ?", [db.now_ms() - 86_400_000]).fetchone()[0])


def normalize_args(raw: str | None) -> str:
    """Tool-call arguments must be a JSON object string; providers (Anthropic via a router) reject "" or non-objects
    when the call is echoed back in the next request."""
    try:
        v = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        v = {}
    return json.dumps(v if isinstance(v, dict) else {}, ensure_ascii=False)


# ---------------------------------------------------------------- LLM streaming
def stream_completion(cfg: Config, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                      session: Any = None) -> Iterator[dict[str, Any]]:
    """Yield {"type": "text", "text"} chunks and finally {"type": "done", "content", "tool_calls", "finish"}."""
    c = settings(cfg)
    s = c["llm"]
    if not all(s.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY")):
        raise RuntimeError("未設定 LLM（.env 的 LLM_BASE_URL / LLM_API_KEY）")
    body: dict[str, Any] = {"model": model, "stream": True, "max_tokens": int(c["max_tokens"]), "messages": messages}
    if tools:
        body["tools"] = tools
    http = session or requests
    r = http.post(s["LLM_BASE_URL"].rstrip("/") + "/chat/completions", json=body, headers={"Authorization": f"Bearer {s['LLM_API_KEY']}"},
                  timeout=float(c["timeout_sec"]), stream=True)
    if r.status_code >= 400:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
    content = ""
    calls: dict[int, dict[str, Any]] = {}
    finish = None
    for line in r.iter_lines(decode_unicode=False):
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            break
        try:
            d = json.loads(payload)
        except ValueError:
            continue
        for ch in d.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content += delta["content"]
                yield {"type": "text", "text": delta["content"]}
            for tc in delta.get("tool_calls") or []:
                i = int(tc.get("index", 0))
                slot = calls.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    tool_calls = [calls[i] for i in sorted(calls) if calls[i]["function"]["name"]]
    for k, tc in enumerate(tool_calls):
        tc["id"] = tc["id"] or f"call_{k}_{uuid.uuid4().hex[:6]}"
        tc["function"]["arguments"] = normalize_args(tc["function"]["arguments"])
    yield {"type": "done", "content": content, "tool_calls": tool_calls, "finish": finish}


# ---------------------------------------------------------------- one user turn
def _wire(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def evidence_summary(cfg: Config) -> str:
    """Current evidence levels built from the result files (discover / replay / heat / paper), so the prompt never
    carries stale conclusions."""
    lines: list[str] = []
    pc = lambda v: "-" if v is None else f"{v * 100:+.2f}%"
    try:
        from okxrev import replay
        cmp_ = replay.compare(cfg)
        lb = [r for r in cmp_.get("leaderboard", []) if not r["trader_id"].startswith("random")]
        win = [r for r in lb if r.get("profitable")]
        if win:
            lines.append("盲測重播中「每筆平均為正、帳本 A 獲利、且扣同日隨機顯著」的交易員（其餘都不符合）：" + "、".join(
                f"{r['trader_id']}（每筆 {pc(r['C'].get('mean'))}，帳本 A {pc(r.get('book_a_ret'))}，{r['C'].get('n')} 筆）" for r in win[:8]))
        elif lb:
            lines.append("盲測重播：沒有交易員同時做到每筆平均為正、帳本 A 獲利、扣同日隨機顯著")
        lose = [r for r in lb if (r.get("diag") or {}).get("vs_same_day", {}).get("significant") and not r.get("profitable")]
        if lose:
            lines.append("只是「比隨機少虧」、實際照做會虧的：" + "、".join(f"{r['trader_id']}（每筆 {pc(r['C'].get('mean'))}）" for r in lose[:8]))
    except Exception:
        pass
    p = cfg.paths.results / "heat.json"
    if p.exists():
        try:
            h = json.loads(p.read_text(encoding="utf-8"))
            ok_ = [r for r in h.get("results", []) if r.get("verdict") in ("可用", "觀察中")]
            parts = []
            for r in ok_:
                b = r[r["best_h"]]
                parts.append(f"{r['name']}［{r['verdict']}，做空{r['best_h'][2:]}：盲測每筆 {pc(b['blind'].get('mean'))}、帳本 A {pc(b['blind'].get('book_a'))}、"
                             f"日均 {pc(b['blind'].get('day_mean'))}，隨機每筆 {pc(b.get('base_blind'))}］")
            bad = [r["name"] for r in h.get("results", []) if r.get("verdict") == "無效"]
            lines.append(f"過熱分數驗證（{h.get('generated_at')}，判定需每筆平均為正且帳本 A 獲利）：" + ("；".join(parts) if parts else "沒有條件通過") +
                         (f"；無效：{'、'.join(bad)}" if bad else "") +
                         "。注意：全市場同時過熱的日子做空大虧，「日均」把這些日子壓成一天，會高估；請以每筆平均與帳本 A 為準")
            rk = next((x for x in h.get("risk", []) if x["name"] == "score≥80"), None)
            if rk and rk.get("blind", {}).get("mae"):
                m, bl = rk["blind"]["mae"], rk["blind"]
                lines.append(f"過熱≥80 做空 24h 風險（盲測）：中途最大反向漲幅中位數 {m['p50'] * 100:.0f}%、90 分位 {m['p90'] * 100:.0f}%，"
                             f"{m['hit_20'] * 100:.0f}% 的案例曾逆向漲 20% 以上；止損 10% 版本日均 {pc((bl.get('s_24h_sl10') or {}).get('day_mean'))}、"
                             f"止損 20% 版本 {pc((bl.get('s_24h_sl20') or {}).get('day_mean'))}")
        except Exception:
            pass
    d = cfg.paths.results / "discover.json"
    if d.exists():
        try:
            res = json.loads(d.read_text(encoding="utf-8")).get("results", [])
            n = {v: sum(1 for r in res if r["verdict"] == v) for v in ("可用", "觀察中")}
            lines.append(f"進場探索（探索期判定，非盲測）：可用 {n['可用']} 條、觀察中 {n['觀察中']} 條")
        except Exception:
            pass
    from okxrev import ml
    if ml.has_model(cfg, "ml") or ml.has_model(cfg, "twin"):
        lines.append("機器學習與使用者分身：只有紙上交易／盲測重播的排行可作為證據，請以 paper_trading 工具查實際成績")
    return "\n\n目前證據摘要（自動產生）：\n" + "\n".join(f"- {x}" for x in lines) if lines else ""


def live_context(cfg: Config, box: Toolbox) -> str:
    """Running bots right now (OKX, cached 60s); empty when there is no key, live data is off, or the call fails."""
    if not box.allow_private or not box.allow_live:
        return ""
    from okxrev import live
    from okxrev.config import load_credentials
    if load_credentials(cfg.paths.root) is None:
        return ""
    try:
        snap = live.running_bots(cfg, box.priv())
    except Exception as e:
        log.info("live bot context skipped: %s", e)
        return ""
    brief = live.bots_brief(snap)
    t = ms_to_str(snap.get("ts"), "%H:%M:%S")
    if not brief:
        return f"\n\n使用者目前沒有運行中的網格／DCA 機器人（OKX 即時查詢 {t} UTC）。"
    return (f"\n\n使用者目前運行中的機器人（OKX 即時查詢 {t} UTC，以此為準；詳細數字可呼叫 bots 工具）：\n{brief}")


def run_turn(cfg: Config, cid: str | None, user_text: str | None, inst: str | None = None, model: str | None = None,
             cont: bool = False, session: Any = None, toolbox: Toolbox | None = None) -> Iterator[dict[str, Any]]:
    """Generator of UI events: conv, text, tool_start, tool_end, notice, done, error."""
    c = settings(cfg)
    model = model or c["model"]
    con = connect(cfg)
    box = toolbox or Toolbox(cfg)
    try:
        if not cid:
            title = (user_text or "新對話").strip().replace("\n", " ")[:40]
            cid = new_conversation(con, inst, model, title)
        else:
            con.execute("UPDATE conversations SET model = ?, inst = COALESCE(?, inst) WHERE id = ?", [model, inst, cid])
        yield {"type": "conv", "id": cid}
        if user_text:
            add_message(con, cid, {"role": "user", "content": user_text}, {"inst": inst})
        elif cont:
            add_message(con, cid, {"role": "user", "content": "請繼續查詢剛才沒查完的部分，然後補充或修正你的分析。"}, {"continue": True})
        hist = load_messages(con, cid)
        sys_msg = SYSTEM + f"\n\n現在時間：{ms_to_str(db.now_ms())} UTC。"
        if inst:
            sys_msg += f"\n使用者目前選擇討論的合約：{inst}（問題沒指明合約時就是指它）。"
        sys_msg += evidence_summary(cfg)
        sys_msg += live_context(cfg, box)
        msgs = [{"role": "system", "content": sys_msg}] + [_wire(m) for m in hist[-int(c["history_messages"]):]]
        while msgs[1:] and msgs[1]["role"] == "tool":
            msgs.pop(1)
        tools = schemas()
        rounds = n_calls = n_chars = 0
        exhausted = False
        while True:
            if calls_today(con) >= int(c["daily_calls"]):
                yield {"type": "notice", "text": f"今天的工具呼叫已達上限（{c['daily_calls']} 次），將只根據已有資料回答"}
                exhausted = True
            budget_left = not exhausted and rounds < int(c["max_rounds"]) and n_calls < int(c["max_calls"]) and n_chars < int(c["max_tool_chars"])
            if not budget_left and not exhausted:
                exhausted = True
                msgs.append({"role": "user", "content": "（系統）查詢額度已用完：請根據目前已取得的資料直接回答，並在最後列出還有哪些沒查到、建議繼續查什麼。"})
                yield {"type": "notice", "text": "已達本輪查詢上限，AI 會先根據現有資料回答"}
            res = None
            for ev in stream_completion(cfg, msgs, tools if not exhausted else None, model, session):
                if ev["type"] == "text":
                    yield ev
                else:
                    res = ev
            assert res is not None
            amsg: dict[str, Any] = {"role": "assistant", "content": res["content"] or None}
            if res["tool_calls"] and not exhausted:
                amsg["tool_calls"] = res["tool_calls"]
            add_message(con, cid, amsg, {"model": model, "round": rounds})
            msgs.append(_wire(amsg))
            if not amsg.get("tool_calls"):
                break
            rounds += 1
            calls = amsg["tool_calls"][: max(0, int(c["max_calls"]) - n_calls)] or amsg["tool_calls"][:1]
            for tc in calls:
                yield {"type": "tool_start", "id": tc["id"], "name": tc["function"]["name"], "args": tc["function"]["arguments"]}

            def run_one(tc: dict[str, Any]) -> tuple[dict[str, Any], str, bool, int]:
                t0 = time.monotonic()
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except ValueError:
                    return tc, json.dumps({"error": "工具參數不是合法 JSON"}, ensure_ascii=False), False, 0
                text, ok = call_tool(box, tc["function"]["name"], args, int(c["tool_result_chars"]))
                return tc, text, ok, int((time.monotonic() - t0) * 1000)

            with ThreadPoolExecutor(max_workers=min(6, len(calls))) as ex:
                results = list(ex.map(run_one, calls))
            answered = set()
            for tc, text, ok, ms in results:
                n_calls += 1
                n_chars += len(text)
                answered.add(tc["id"])
                tmsg = {"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"], "content": text}
                add_message(con, cid, tmsg, {"ok": ok, "ms": ms})
                msgs.append(tmsg)
                con.execute("INSERT INTO tool_log VALUES (?, ?, ?, ?, ?, ?, ?)", [db.now_ms(), cid, tc["function"]["name"], tc["function"]["arguments"], ok, len(text), ms])
                yield {"type": "tool_end", "id": tc["id"], "name": tc["function"]["name"], "ok": ok, "ms": ms, "chars": len(text), "preview": text[:400]}
            for tc in amsg["tool_calls"]:
                if tc["id"] not in answered:
                    skip = {"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"], "content": json.dumps({"error": "超過本輪呼叫上限，未執行"}, ensure_ascii=False)}
                    add_message(con, cid, skip, {"ok": False})
                    msgs.append(skip)
        yield {"type": "done", "id": cid, "rounds": rounds, "calls": n_calls, "chars": n_chars, "exhausted": exhausted}
    except Exception as e:
        log.exception("chat turn failed")
        yield {"type": "error", "text": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        if toolbox is None:
            box.close()
        con.close()


def transcript(cfg: Config, cid: str) -> dict[str, Any]:
    con = connect(cfg, read_only=True)
    try:
        conv = con.execute("SELECT id, title, inst, model, created, updated FROM conversations WHERE id = ?", [cid]).df().to_dict("records")
        msgs = load_messages(con, cid) if conv else []
    finally:
        con.close()
    return {"conversation": conv[0] if conv else None, "messages": msgs}
