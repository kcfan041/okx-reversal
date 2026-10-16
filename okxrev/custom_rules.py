"""Rules saved from the event lab (data/custom_rules.json).

Every saved rule becomes a paper trader `lab_<id>` (live paper trading and the 2024-25 replay), labelled with its
blind-test verdict. Live detection: the paper tick (every 5 minutes, 5m bars) produces the signals; signals on the
newest bars are then checked on 1m candles and recorded as alerts (heat_alerts, kind 'lab').
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, ms_to_str
from okxrev.paper import EXIT_RULES, Signal, Trader

log = logging.getLogger("okxrev.custom_rules")
_lock = threading.Lock()
ID_RE = re.compile(r"^[a-z0-9]{4,16}$")
BAR = db.BAR_MS


def path(cfg: Config) -> Path:
    return cfg.paths.root / "data" / "custom_rules.json"


def load(cfg: Config) -> list[dict[str, Any]]:
    p = path(cfg)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("custom_rules.json unreadable")
        return []


def _save(cfg: Config, rules: list[dict[str, Any]]) -> None:
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=1, default=_jd), encoding="utf-8")
    tmp.replace(p)


def _jd(o: Any) -> Any:
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def _summary(test: dict[str, Any], side: str, exit_: str) -> dict[str, Any]:
    c = next((x for x in test.get("combos", []) if x["side"] == side and x["exit"] == exit_), None)
    if c is None:
        return {}
    pick = lambda s: {k: (s or {}).get(k) for k in ("n", "n_days", "mean", "day_mean", "win_rate", "ci95", "book_a")}
    return {"verdict": c["verdict"], "disc": pick(c.get("disc")), "valid": pick(c.get("valid")), "blind": pick(c.get("blind")) if c.get("blind") else None,
            "base_valid": c.get("base_valid"), "base_blind": c.get("base_blind"), "blind_tested": bool(test.get("blind_shown"))}


def add(cfg: Config, name: str, template: dict[str, Any], side: str, exit_: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    from okxrev import events_lab as L
    if side not in ("long", "short"):
        raise ValueError("side 必須是 long 或 short")
    if exit_ not in L.EXITS or exit_ not in EXIT_RULES:
        raise ValueError(f"不支援的出場：{exit_}")
    name = (name or "").strip()[:60] or "自訂規則"
    t = L.norm_template(template)
    test = L.test_template(cfg, t, blind=False)
    rule = {"id": uuid.uuid4().hex[:8], "name": name, "template": t, "desc": L.describe(t), "side": side, "exit": exit_,
            "created": db.now_ms(), "source": source or {}, "result": _summary(test, side, exit_), "hash": test["hash"], "active": True}
    with _lock:
        rules = load(cfg)
        rules.append(rule)
        _save(cfg, rules)
    return rule


def update(cfg: Config, rid: str, **kw: Any) -> dict[str, Any]:
    if not ID_RE.match(rid or ""):
        raise ValueError("bad id")
    with _lock:
        rules = load(cfg)
        r = next((x for x in rules if x["id"] == rid), None)
        if r is None:
            raise KeyError(rid)
        for k in ("active", "name"):
            if k in kw and kw[k] is not None:
                r[k] = bool(kw[k]) if k == "active" else str(kw[k])[:60]
        _save(cfg, rules)
    return r


def delete(cfg: Config, rid: str) -> None:
    if not ID_RE.match(rid or ""):
        raise ValueError("bad id")
    with _lock:
        rules = [x for x in load(cfg) if x["id"] != rid]
        _save(cfg, rules)


def retest(cfg: Config, rid: str, blind: bool = False) -> dict[str, Any]:
    from okxrev import events_lab as L
    with _lock:
        rules = load(cfg)
        r = next((x for x in rules if x["id"] == rid), None)
        if r is None:
            raise KeyError(rid)
    test = L.test_template(cfg, r["template"], blind=blind)
    with _lock:
        rules = load(cfg)
        for x in rules:
            if x["id"] == rid:
                x["result"] = _summary(test, x["side"], x["exit"])
                x["retested"] = db.now_ms()
                r = x
        _save(cfg, rules)
    return r


def verdict_label(r: dict[str, Any]) -> str:
    res = r.get("result") or {}
    v = res.get("verdict") or "未測"
    return v if res.get("blind_tested") else f"{v}，未盲測" if "待盲測" not in v else v


# ---------------------------------------------------------------- paper trader
class LabTrader(Trader):
    """Fires the template on 5m bars (shape computed from candles at or before each bar, freshness = cooldown)."""
    kind = "lab"

    def __init__(self, rule: dict[str, Any]):
        from okxrev import events_lab as L
        self.rule = rule
        self.t = L.norm_template(rule["template"])
        self.side = rule["side"]
        self.exit = rule["exit"]
        self.trader_id = f"lab_{rule['id']}"
        self.label = f"實驗室：{rule['name']}（{'做空' if self.side == 'short' else '做多'}，{self.exit}，{verdict_label(rule)}）"

    def params(self) -> dict[str, Any]:
        return {"id": self.rule["id"], "template": self.t, "desc": self.rule.get("desc"), "side": self.side, "exit": self.exit,
                "result": self.rule.get("result")}

    def hits(self, g: pd.DataFrame, f: pd.DataFrame, lo_ts: int, hi_ts: int) -> np.ndarray:
        """Timestamps in [lo_ts, hi_ts] where the template fires (fresh)."""
        from okxrev import events_lab as L
        ts = g.index.to_numpy(np.int64)
        h, lo, c = (g[k].to_numpy(float) for k in ("high", "low", "close"))
        cd = int(self.t["cooldown"])
        a = max(0, int(np.searchsorted(ts, lo_ts)) - cd - 1)
        b = int(np.searchsorted(ts, hi_ts, side="right"))
        if b <= a:
            return np.array([], dtype=np.int64)
        cand = L.prefilter(h, lo, c, self.t["rise_min"], self.t["drop_min"])
        cand = cand[(cand >= a) & (cand < b)]
        if len(cand) == 0:
            return np.array([], dtype=np.int64)
        sh = L.shape_at(ts, h, lo, c, cand)
        ft = f.reindex(ts[cand])
        cols = {**sh, "ret_bar": ft["ret5"].to_numpy(float), "vmax_1h": ft["vmax_1h"].to_numpy(float), "turnover24": ft["turnover24"].to_numpy(float),
                "age_d": ft["age_d"].to_numpy(float), "btc": ft["btc_regime"].astype(str).to_numpy()}
        m = L.mask(self.t, cols)
        hit_ts = ts[cand][m]
        if len(hit_ts) == 0:
            return hit_ts
        keep = np.r_[True, np.diff(hit_ts) / BAR > cd]
        fresh = hit_ts[keep]
        return fresh[(fresh >= lo_ts) & (fresh <= hi_ts)]

    def signals(self, ctx: Any, inst: str, f: pd.DataFrame, bars: np.ndarray) -> list[Signal]:
        g = ctx.candles.get(inst)
        if g is None or len(bars) == 0:
            return []
        bars = np.asarray(bars, dtype=np.int64)
        hit = self.hits(g, f, int(bars[0]), int(bars[-1]))
        hit = hit[np.isin(hit, bars)]
        return [Signal(self.trader_id, inst, int(t), self.side, self.exit, 1.0, self.rule.get("desc", "")[:300]) for t in hit]


def traders(cfg: Config) -> list[Trader]:
    out: list[Trader] = []
    for r in load(cfg):
        if not r.get("active", True):
            continue
        try:
            out.append(LabTrader(r))
        except Exception:
            log.exception("bad custom rule %s", r.get("id"))
    return out


# ---------------------------------------------------------------- live 1m confirmation + alerts
def confirm_1m(cfg: Config, inst: str, bar_ts: int, t: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Re-measure the drop from the 1m peak at the 5m signal: the 1m close at the end of the signal bar must still
    be at least 80% of the template's drop below the highest 1m high of the last 12h."""
    from okxrev import fine
    end = bar_ts + BAR
    m1 = fine.candles(cfg, inst, "1m", end - 12 * 3_600_000, end, client=client, include_live=False)
    if len(m1) < 30:
        return {"ok": None, "note": "1m 資料不足"}
    pk = float(m1["high"].max())
    last = float(m1["close"].iloc[-1])
    drop = 1 - last / pk if pk > 0 else 0.0
    mm = fine.micro(m1, "1m")
    return {"ok": bool(drop >= 0.8 * float(t["drop_min"])), "drop_1m": drop, "peak_1m": pk, "last_1m": last,
            "summary": fine.summary_line(mm)}


def live_alerts(cfg: Config, con: Any, last_bar: int, now: int) -> dict[str, Any]:
    """After a paper tick: lab signals on the newest 2 bars -> 1m confirmation -> heat_alerts (kind 'lab')."""
    from okxrev import heat
    from okxrev.okx_client import OkxClient
    rules = {f"lab_{r['id']}": r for r in load(cfg) if r.get("active", True)}
    if not rules:
        return {"n": 0}
    try:
        rows = con.execute("SELECT trader_id, inst_id, bar_ts, side FROM decisions WHERE trader_id LIKE 'lab_%' AND bar_ts >= ?",
                           [last_bar - BAR]).fetchall()
    except Exception:
        return {"n": 0}
    if not rows:
        return {"n": 0}
    cands = []
    client = OkxClient(cfg.okx)
    try:
        for tid, inst, bts, side in rows[:20]:
            r = rules.get(tid)
            if r is None:
                continue
            try:
                c = confirm_1m(cfg, inst, int(bts), r["template"], client)
            except Exception as e:
                c = {"ok": None, "note": type(e).__name__}
            if c.get("ok") is False:
                continue
            tag = "1m 確認" if c.get("ok") else "1m 未確認"
            cands.append({"inst_id": inst, "kind": "lab", "score": None,
                          "detail": f"{r['name']}（{'做空' if side == 'short' else '做多'} {r['exit']}，{verdict_label(r)}）{tag}；"
                                    f"{c.get('summary') or c.get('note', '')}"[:480]})
    finally:
        client.close()
    new = heat.record_alerts(cfg, con, cands, now) if cands else []
    return {"n": len(rows), "alerts": len(new)}


def recent_hits(cfg: Config, rid: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from okxrev import paper
    try:
        con = paper.connect(cfg, read_only=True)
    except (FileNotFoundError, db.DbBusy):
        return []
    try:
        sql = ("SELECT d.trader_id, d.inst_id, d.bar_ts, d.side, d.exit_rule, p.state, p.entry_px, p.exit_px, p.ret_net, p.exit_reason "
               "FROM decisions d LEFT JOIN positions p ON p.decision_id = d.decision_id AND p.book = 'C' WHERE d.trader_id LIKE ?")
        df = con.execute(sql + " ORDER BY d.bar_ts DESC LIMIT ?", [f"lab_{rid}" if rid else "lab_%", int(limit)]).df()
    except Exception:
        df = pd.DataFrame()
    finally:
        con.close()
    return [{**r, "time": ms_to_str(r["bar_ts"])} for r in df.to_dict("records")]
