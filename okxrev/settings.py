"""User settings editable from the web panel.

Values are stored in data/settings.json as {"section.key": value} and applied on top of config.yaml by
load_config (config.yaml and .env are never written). Only the fields listed in FIELDS can be set; each is type-
and range-checked. Deleting a key (or the file) falls back to config.yaml.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_lock = threading.Lock()

# section label, key path, type, label, help, extra (min/max/options), when it takes effect
FIELDS: list[dict[str, Any]] = [
    {"group": "collect", "path": "sync.oi5m_scope", "type": "choice", "options": {"focus": "只抓關注清單＋過熱前 N 名", "all": "全部合約"},
     "label": "每 5 分鐘 OI 範圍", "help": "停抓的部分之後補不回來（OKX 只保留幾小時）", "when": "下一輪同步"},
    {"group": "collect", "path": "heat.sentiment_all", "type": "bool", "label": "多空比與爆倉也抓其他幣",
     "help": "關閉後只抓關注清單＋過熱前 N 名", "when": "下一輪同步"},
    {"group": "collect", "path": "heat.liq_all_every_min", "type": "int", "min": 10, "max": 1440, "label": "其他幣爆倉每幾分鐘抓一次",
     "help": "每個幣只能查到最新約 100 筆", "when": "下一輪同步"},
    {"group": "collect", "path": "heat.ls_all_every_min", "type": "int", "min": 60, "max": 1440, "label": "其他幣多空比每幾分鐘抓一次",
     "help": "一頁約 8 小時，超過 480 分鐘會有缺口", "when": "下一輪同步"},
    {"group": "collect", "path": "sync.derivs_max_sec", "type": "int", "min": 20, "max": 120, "label": "每輪衍生品最多幾秒",
     "help": "整輪同步上限 240 秒", "when": "下一輪同步"},
    {"group": "heat", "path": "heat.watchlist", "type": "insts", "label": "關注清單", "help": "多空比、爆倉、OI 5 分鐘會優先抓；提醒只看這些與前 N 名",
     "when": "下一輪同步"},
    {"group": "heat", "path": "heat.top_n", "type": "int", "min": 0, "max": 50, "label": "過熱前 N 名也視為關注", "when": "下一輪同步"},
    {"group": "heat", "path": "heat.alert_threshold", "type": "int", "min": 50, "max": 100, "label": "過熱提醒門檻", "when": "下一小時"},
    {"group": "heat", "path": "heat.alert_cooldown_h", "type": "int", "min": 1, "max": 72, "label": "同一幣同類提醒冷卻（小時）", "when": "立即"},
    {"group": "paper", "path": "paper.kinds.rule", "type": "bool", "label": "探索規則交易員", "when": "下一輪紙上交易"},
    {"group": "paper", "path": "paper.kinds.heat", "type": "bool", "label": "過熱分數交易員", "help": "盲測判定：每筆平均為負，照做會虧", "when": "下一輪紙上交易"},
    {"group": "paper", "path": "paper.kinds.lab", "type": "bool", "label": "事件實驗室規則", "when": "下一輪紙上交易"},
    {"group": "paper", "path": "paper.kinds.ml", "type": "bool", "label": "機器學習模型", "when": "下一輪紙上交易"},
    {"group": "paper", "path": "paper.kinds.twin", "type": "bool", "label": "你的分身", "when": "下一輪紙上交易"},
    {"group": "paper", "path": "paper.kinds.llm", "type": "bool", "label": "LLM 審核", "help": "會呼叫 .env 設定的 LLM", "when": "下一輪紙上交易"},
    {"group": "paper", "path": "heat.paper_thresholds", "type": "intlist", "min": 50, "max": 100, "label": "過熱交易員門檻", "when": "下一輪紙上交易"},
    {"group": "paper", "path": "paper.books.A.equity", "type": "float", "min": 100, "max": 1e7, "label": "帳本 A 起始資金（U）",
     "help": "只影響新建立的帳本；已有的帳本不重設", "when": "新帳本"},
    {"group": "paper", "path": "paper.books.A.size_frac", "type": "float", "min": 0.001, "max": 1, "label": "帳本 A 每筆占比", "when": "下一筆"},
    {"group": "paper", "path": "paper.books.B.lever", "type": "float", "min": 1, "max": 125, "label": "帳本 B 槓桿", "when": "下一筆"},
    {"group": "ai", "path": "chat.model", "type": "choice", "options": {"claude": "claude", "gpt": "gpt", "default": "default"},
     "label": "AI 討論預設模型", "when": "下一個問題"},
    {"group": "ai", "path": "chat.daily_calls", "type": "int", "min": 10, "max": 5000, "label": "每日工具呼叫上限", "when": "立即"},
    {"group": "ai", "path": "chat.max_rounds", "type": "int", "min": 2, "max": 30, "label": "每題最多回合", "when": "下一個問題"},
    {"group": "ai", "path": "chat.max_calls", "type": "int", "min": 5, "max": 100, "label": "每題最多工具次數", "when": "下一個問題"},
    {"group": "bg", "path": "background.priority", "type": "choice", "options": {"idle": "最低（idle）", "below_normal": "低於一般"},
     "label": "背景程式優先權", "help": "遊戲／錄影時建議最低", "when": "下一個背景程式"},
    {"group": "bg", "path": "background.threads", "type": "int", "min": 1, "max": 16, "label": "背景程式執行緒數", "when": "下一個背景程式"},
]
GROUPS = {"collect": "資料收集", "heat": "過熱監控與提醒", "paper": "紙上交易", "ai": "AI 討論", "bg": "背景程式"}
BY_PATH = {f["path"]: f for f in FIELDS}


class SettingsError(ValueError):
    pass


def settings_path(root: Path) -> Path:
    return root / "data" / "settings.json"


def load_overrides(root: Path) -> dict[str, Any]:
    p = settings_path(root)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {k: v for k, v in d.items() if k in BY_PATH}


def get_path(raw: dict[str, Any], path: str) -> Any:
    cur: Any = raw
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def set_path(raw: dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    cur = raw
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def apply(raw: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for k, v in overrides.items():
        set_path(raw, k, v)
    return raw


DEFAULTS: dict[str, Any] = {
    "sync.oi5m_scope": "focus", "heat.sentiment_all": True, "heat.liq_all_every_min": 30, "heat.ls_all_every_min": 360,
    "sync.derivs_max_sec": 60, "heat.watchlist": [], "heat.top_n": 10, "heat.alert_threshold": 80, "heat.alert_cooldown_h": 6,
    "paper.kinds.rule": True, "paper.kinds.heat": True, "paper.kinds.lab": True, "paper.kinds.ml": True, "paper.kinds.twin": True,
    "paper.kinds.llm": True, "heat.paper_thresholds": [80, 90], "chat.model": "claude", "chat.daily_calls": 500, "chat.max_rounds": 12,
    "chat.max_calls": 40, "background.priority": "idle", "background.threads": 2,
}


def validate(path: str, value: Any) -> Any:
    import re
    f = BY_PATH.get(path)
    if f is None:
        raise SettingsError(f"不能修改：{path}")
    t = f["type"]
    lab = f["label"]
    if t == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{lab} 必須是開或關")
        return value
    if t == "choice":
        if value not in f["options"]:
            raise SettingsError(f"{lab} 只能是 {'、'.join(f['options'])}")
        return value
    if t in ("int", "float"):
        try:
            v = int(value) if t == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{lab} 必須是數字")
        if t == "int" and isinstance(value, float) and not float(value).is_integer():
            raise SettingsError(f"{lab} 必須是整數")
        if not (f["min"] <= v <= f["max"]):
            raise SettingsError(f"{lab} 必須在 {f['min']:g} ～ {f['max']:g} 之間")
        return v
    if t == "intlist":
        if not isinstance(value, list) or not value or len(value) > 5:
            raise SettingsError(f"{lab} 需要 1～5 個數字")
        out = []
        for x in value:
            try:
                n = int(x)
            except (TypeError, ValueError):
                raise SettingsError(f"{lab} 必須是整數")
            if not (f["min"] <= n <= f["max"]):
                raise SettingsError(f"{lab} 每個值必須在 {f['min']}～{f['max']} 之間")
            out.append(n)
        return sorted(set(out))
    if t == "insts":
        if isinstance(value, str):
            value = [x for x in re.split(r"[\s,]+", value) if x]
        if not isinstance(value, list) or len(value) > 50:
            raise SettingsError(f"{lab} 最多 50 個")
        out = []
        for x in value:
            s = str(x).strip().upper()
            if "-" not in s:
                s = f"{s}-USDT-SWAP"
            if not re.fullmatch(r"[A-Z0-9]{1,20}-USDT-SWAP", s):
                raise SettingsError(f"合約代號格式錯誤：{x}")
            out.append(s)
        return list(dict.fromkeys(out))
    raise SettingsError(f"未知欄位型別：{t}")


def save(root: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """Validate every change first, then write the whole file atomically. A value of None removes the override."""
    clean: dict[str, Any] = {}
    for k, v in changes.items():
        clean[k] = None if v is None else validate(k, v)
    with _lock:
        cur = load_overrides(root)
        for k, v in clean.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        p = settings_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    return cur


def describe(raw_base: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field with its effective value, the config.yaml default, and whether it is overridden."""
    out = []
    for f in FIELDS:
        base = get_path(raw_base, f["path"])
        if base is None:
            base = DEFAULTS.get(f["path"])
        ov = f["path"] in overrides
        out.append({**f, "group_label": GROUPS[f["group"]], "default": base, "value": overrides[f["path"]] if ov else base, "overridden": ov})
    return out
