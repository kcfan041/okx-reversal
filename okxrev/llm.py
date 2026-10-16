"""OpenAI-compatible LLM reviewer for paper-trade candidates.

.env: LLM_BASE_URL (e.g. https://api.openai.com/v1 or a local router), LLM_API_KEY, LLM_MODEL.
Every call is logged in paper.duckdb `llm_calls` with token usage and estimated cost; daily call/cost caps apply.
When disabled, over budget, or on error the candidate is recorded as `skip_llm` (not entered) so the reviewer's
book only contains trades the LLM actually approved.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import numpy as np
import requests

from okxrev import db
from okxrev.config import Config, load_env, ms_to_str

log = logging.getLogger("okxrev.llm")

KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
DAY = 86_400_000

SYSTEM = (
    "你是加密貨幣永續合約的風控審核員。系統會給你一個由規則或模型產生的模擬進場候選，以及當下的市場特徵與最近 K 線摘要。"
    "你的任務是判斷是否接受這筆模擬交易。只根據提供的資料判斷，不要假設任何未提供的消息。"
    "請只回覆 JSON：{\"decision\": \"accept\" 或 \"reject\", \"confidence\": 0 到 1, \"reason\": \"一兩句中文理由\"}"
)


def settings(cfg: Config) -> dict[str, str]:
    return load_env(KEYS, cfg.paths.root)


def enabled(cfg: Config) -> bool:
    s = settings(cfg)
    return all(k in s for k in KEYS)


def usage_today(con: Any) -> tuple[int, float]:
    r = con.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM llm_calls WHERE ts >= ?", [db.now_ms() - DAY]).fetchone()
    return int(r[0]), float(r[1])


def bar_summary(ctx: Any, inst: str, ts: int, n: int = 24) -> list[list[float]]:
    g = ctx.candles.get(inst)
    if g is None:
        return []
    sub = g[g.index <= ts].tail(n)
    base = float(sub["close"].iloc[-1]) if len(sub) else 1.0
    return [[round(float(r.open) / base - 1, 4), round(float(r.high) / base - 1, 4), round(float(r.low) / base - 1, 4),
             round(float(r.close) / base - 1, 4)] for r in sub.itertuples(index=False)]


def prompt(ctx: Any, s: Any) -> str:
    feats = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in (s.features or ctx.snapshot(s.inst_id, s.bar_ts)).items() if not k.startswith("_")}
    payload = {
        "合約": s.inst_id, "方向": "做多" if s.side == "long" else "做空", "訊號時間UTC": ms_to_str(s.bar_ts),
        "來源": s.reason, "預定出場": s.exit_rule,
        "特徵說明": "r1h/r6h/r24h/r3d=前1h/6h/24h/3d報酬；vol_ratio=本根量/20根均量；vmax_1h=近1h最大量比；pos24/pos7d=在24h/7日區間位置(0底1頂)；"
                  "dist_ema=距日均線；vol_rank=日波動/週波動；atr=14根ATR佔價格比；turnover24=24h成交額USDT；btc_regime=BTC 4h環境；"
                  "breadth=全市場24h上漲比例；funding=最近資金費率；oi_chg=OI 24h變化；age_d=上市天數",
        "特徵": feats,
        "最近24根5分K(相對最新收盤的OHLC比例)": bar_summary(ctx, s.inst_id, s.bar_ts),
    }
    return json.dumps(payload, ensure_ascii=False)


def parse(text: str) -> tuple[str, float, str]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json")
    d = json.loads(m.group(0))
    dec = str(d.get("decision", "")).lower()
    if dec not in ("accept", "reject"):
        raise ValueError(f"bad decision {dec}")
    return dec, float(d.get("confidence", 0) or 0), str(d.get("reason", ""))[:500]


def call(cfg: Config, user: str, session: requests.Session | None = None, system: str | None = None,
         max_tokens: int = 300) -> tuple[str, int, int]:
    s = settings(cfg)
    lc = cfg.paper.get("llm", {})
    url = s["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    body = {"model": s["LLM_MODEL"], "temperature": 0, "max_tokens": max_tokens, "stream": False,
            "messages": [{"role": "system", "content": system or SYSTEM}, {"role": "user", "content": user}]}
    http = session or requests
    r = http.post(url, json=body, headers={"Authorization": f"Bearer {s['LLM_API_KEY']}"}, timeout=float(lc.get("timeout_sec", 20)))
    r.raise_for_status()
    try:
        d = r.json()
    except ValueError:
        head = (getattr(r, "text", "") or "")[:80]
        if head.lstrip().startswith("data:"):
            raise ValueError("LLM 回傳串流格式（SSE），請確認服務有遵守 stream=false") from None
        raise ValueError(f"LLM 回傳的不是 JSON：{head!r}") from None
    if not d.get("choices"):
        raise ValueError(f"LLM 回應沒有 choices：{str(d)[:120]}")
    text = d["choices"][0]["message"]["content"]
    u = d.get("usage") or {}
    return text, int(u.get("prompt_tokens") or len(user) // 3), int(u.get("completion_tokens") or len(text) // 3)


def limits(cfg: Config) -> dict[str, Any]:
    lc = cfg.paper.get("llm", {})
    return {"max_calls": int(lc.get("max_calls_per_day", 200)), "max_cost": float(lc.get("max_cost_usd_per_day", 1000.0)),
            "per_tick": int(lc.get("max_per_tick", 8)), "per_bar": int(lc.get("max_per_bar", 2)),
            "stale_ms": int(lc.get("stale_min", 15)) * 60_000, "max_sec": float(lc.get("max_sec", 50)),
            "pin": float(lc.get("price_in_per_1k", 0.00015)), "pout": float(lc.get("price_out_per_1k", 0.0006))}


SKIP = {"off": "未設定 LLM", "day": "每日呼叫或費用上限", "tick": "本輪名額已滿", "bar": "同一根 K 線名額已滿",
        "stale": "補跑的舊訊號，不送審", "time": "本輪 LLM 時間已用完"}


def _skip(s: Any, why: str) -> None:
    s.action = "skip_llm"
    s.reason = f"{s.reason}｜LLM 未審核（{SKIP[why]}）"


def _budget(ctx: Any, lim: dict[str, Any]) -> dict[str, Any]:
    """Per-tick LLM budget shared by every reviewer in the same tick (calls and wall time)."""
    import time
    b = getattr(ctx, "_llm_budget", None)
    if b is None:
        b = {"used": 0, "t0": time.monotonic()}
        setattr(ctx, "_llm_budget", b)
    return b


def _gate(ctx: Any, cfg: Config, lim: dict[str, Any], b: dict[str, Any]) -> str | None:
    import time
    if not enabled(cfg):
        return "off"
    calls, cost = usage_today(ctx.con)
    if calls >= lim["max_calls"] or cost >= lim["max_cost"]:
        return "day"
    if b["used"] >= lim["per_tick"]:
        return "tick"
    if time.monotonic() - b["t0"] >= lim["max_sec"]:
        return "time"
    return None


def _log(ctx: Any, cfg: Config, lim: dict[str, Any], trader: str, decision: str, tin: int, tout: int, ok: bool, verdict: str, why: str) -> None:
    c = tin / 1000 * lim["pin"] + tout / 1000 * lim["pout"]
    ctx.con.execute("INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [db.now_ms(), trader, decision, settings(cfg).get("LLM_MODEL", ""), tin, tout, c, ok, verdict, why[:500]])


def _fresh(ctx: Any, sigs: list[Any], lim: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Signals older than stale_ms before the tick's last bar (catch-up after a pause) are not sent for review."""
    last = int(getattr(ctx, "last_bar", 0) or 0)
    fresh, stale = [], []
    for s in sigs:
        (stale if last and int(s.bar_ts) < last - lim["stale_ms"] else fresh).append(s)
    return fresh, stale


def review(ctx: Any, cfg: Config, sigs: list[Any], session: requests.Session | None = None) -> list[Any]:
    """One call per candidate. Fresh signals only; at most per_bar per bar (highest score first) and per_tick /
    max_sec per tick (shared by all reviewers). Every skipped candidate says why."""
    lim = limits(cfg)
    b = _budget(ctx, lim)
    fresh, stale = _fresh(ctx, sigs, lim)
    for s in stale:
        _skip(s, "stale")
    out = list(stale)
    per_bar: dict[int, int] = {}
    for s in sorted(fresh, key=lambda x: (-int(x.bar_ts), -x.score)):
        why = _gate(ctx, cfg, lim, b)
        if why is None and per_bar.get(int(s.bar_ts), 0) >= lim["per_bar"]:
            why = "bar"
        if why:
            _skip(s, why)
            out.append(s)
            continue
        b["used"] += 1
        per_bar[int(s.bar_ts)] = per_bar.get(int(s.bar_ts), 0) + 1
        ok, verdict, why_, tin, tout = False, "error", "", 0, 0
        try:
            text, tin, tout = call(cfg, prompt(ctx, s), session)
            verdict, conf, why_ = parse(text)
            ok = True
            s.action = "enter" if verdict == "accept" else "reject"
            s.score = conf
            s.reason = f"{s.reason}｜LLM {verdict} {conf:.2f}：{why_}"
        except Exception as e:
            why_ = f"{type(e).__name__}: {str(e)[:200]}"
            s.action = "skip_llm"
            s.reason = f"{s.reason}｜LLM 失敗：{why_}"
            log.warning("llm review failed: %s", why_)
        _log(ctx, cfg, lim, s.trader_id, s.decision_id, tin, tout, ok, verdict, why_)
        out.append(s)
    return out


BATCH_SYSTEM = (
    "你是加密貨幣永續合約的風控審核員。同一根 5 分 K 收盤時，系統同時產生了多個模擬進場候選（同一個策略），"
    "請一起比較後決定要接受哪些（可以全部拒絕，也可以接受多個）。只根據提供的資料判斷。"
    "請只回覆 JSON：{\"accept\": [\"合約代號\", ...], \"confidence\": 0 到 1, \"reason\": \"一兩句中文理由\"}"
)
BATCH_MAX = 15


def batch_prompt(ctx: Any, sigs: list[Any]) -> str:
    items = []
    for s in sigs:
        feats = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in (s.features or ctx.snapshot(s.inst_id, s.bar_ts)).items()
                 if not k.startswith("_")}
        items.append({"合約": s.inst_id, "方向": "做多" if s.side == "long" else "做空", "來源": s.reason, "預定出場": s.exit_rule,
                      "特徵": feats, "最近12根5分K": bar_summary(ctx, s.inst_id, s.bar_ts, 12)})
    return json.dumps({"訊號時間UTC": ms_to_str(sigs[0].bar_ts), "候選數": len(sigs),
                       "特徵說明": "r1h/r6h/r24h/r3d=前1h/6h/24h/3d報酬；vol_ratio=量比；pos24=24h區間位置；btc_regime=BTC 4h環境；"
                                   "breadth=全市場24h上漲比例；turnover24=24h成交額；funding=資金費率；oi_chg=OI 24h變化",
                       "候選": items}, ensure_ascii=False)


def parse_batch(text: str, insts: list[str]) -> tuple[set[str], float, str]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json")
    d = json.loads(m.group(0))
    acc = d.get("accept")
    if not isinstance(acc, list):
        raise ValueError("accept 不是陣列")
    valid = set(insts)
    return {str(x).upper() for x in acc} & valid, float(d.get("confidence", 0) or 0), str(d.get("reason", ""))[:500]


def review_batch(ctx: Any, cfg: Config, sigs: list[Any], session: requests.Session | None = None) -> list[Any]:
    """One call per bar: all fresh candidates of the same bar are compared together (top BATCH_MAX by score)."""
    lim = limits(cfg)
    b = _budget(ctx, lim)
    fresh, stale = _fresh(ctx, sigs, lim)
    for s in stale:
        _skip(s, "stale")
    out = list(stale)
    by_bar: dict[int, list[Any]] = {}
    for s in fresh:
        by_bar.setdefault(int(s.bar_ts), []).append(s)
    for bar in sorted(by_bar, reverse=True):
        group = sorted(by_bar[bar], key=lambda x: -x.score)
        sent, extra = group[:BATCH_MAX], group[BATCH_MAX:]
        for s in extra:
            _skip(s, "bar")
        out += extra
        why = _gate(ctx, cfg, lim, b)
        if why:
            for s in sent:
                _skip(s, why)
            out += sent
            continue
        b["used"] += 1
        ok, verdict, why_, tin, tout = False, "error", "", 0, 0
        try:
            text, tin, tout = call(cfg, batch_prompt(ctx, sent), session, system=BATCH_SYSTEM, max_tokens=500)
            acc, conf, why_ = parse_batch(text, [s.inst_id for s in sent])
            ok = True
            verdict = f"accept {len(acc)}/{len(sent)}"
            for s in sent:
                yes = s.inst_id in acc
                s.action = "enter" if yes else "reject"
                s.score = conf
                s.reason = f"{s.reason}｜LLM 批次（{len(sent)} 選 {len(acc)}）{'accept' if yes else 'reject'} {conf:.2f}：{why_}"
        except Exception as e:
            why_ = f"{type(e).__name__}: {str(e)[:200]}"
            for s in sent:
                s.action = "skip_llm"
                s.reason = f"{s.reason}｜LLM 批次失敗：{why_}"
            log.warning("llm batch review failed: %s", why_)
        _log(ctx, cfg, lim, sent[0].trader_id, f"batch|{sent[0].trader_id}|{bar}|{len(sent)}", tin, tout, ok, verdict, why_)
        out += sent
    return out
