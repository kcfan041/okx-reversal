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


def call(cfg: Config, user: str, session: requests.Session | None = None) -> tuple[str, int, int]:
    s = settings(cfg)
    lc = cfg.paper.get("llm", {})
    url = s["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    body = {"model": s["LLM_MODEL"], "temperature": 0, "max_tokens": 300, "stream": False,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
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


def review(ctx: Any, cfg: Config, sigs: list[Any], session: requests.Session | None = None) -> list[Any]:
    lc = cfg.paper.get("llm", {})
    max_calls = int(lc.get("max_calls_per_day", 200))
    max_cost = float(lc.get("max_cost_usd_per_day", 1000.0))
    per_tick = int(lc.get("max_per_tick", 4))
    pin, pout = float(lc.get("price_in_per_1k", 0.00015)), float(lc.get("price_out_per_1k", 0.0006))
    model = settings(cfg).get("LLM_MODEL", "")
    sigs = sorted(sigs, key=lambda s: -s.score)
    out = []
    used = 0
    for s in sigs:
        calls, cost = usage_today(ctx.con)
        if not enabled(cfg) or calls >= max_calls or cost >= max_cost or used >= per_tick:
            s.action = "skip_llm"
            s.reason = f"{s.reason}｜LLM 未審核（{'未設定' if not enabled(cfg) else '已達上限'}）"
            out.append(s)
            continue
        used += 1
        ok, verdict, why, tin, tout = False, "error", "", 0, 0
        try:
            text, tin, tout = call(cfg, prompt(ctx, s), session)
            verdict, conf, why = parse(text)
            ok = True
            s.action = "enter" if verdict == "accept" else "reject"
            s.score = conf
            s.reason = f"{s.reason}｜LLM {verdict} {conf:.2f}：{why}"
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:200]}"
            s.action = "skip_llm"
            s.reason = f"{s.reason}｜LLM 失敗：{why}"
            log.warning("llm review failed: %s", why)
        c = tin / 1000 * pin + tout / 1000 * pout
        ctx.con.execute("INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [db.now_ms(), s.trader_id, s.decision_id, model, tin, tout, c, ok, verdict, why])
        out.append(s)
    return out
