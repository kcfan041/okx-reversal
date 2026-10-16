"""Data-collection master switch.

Pausing creates data/PAUSE: the scheduled sync still starts every 5 minutes but exits at once, and paper ticks
are skipped as well (alerts, heat snapshot, lab detection). Resuming removes the flag. The Windows scheduled task
itself is never modified (that needs administrator rights); if it was disabled by hand, the status says so.
Live read-only lookups from the web page (positions, live chart, AI tools) are not affected.

The flag file holds a small JSON record (since, by) so the UI can show how long collection has been off and which
feeds cannot be recovered after such a gap.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str

HOUR = 3_600_000
UNRECOVERABLE = [
    {"name": "多空比", "keep_h": 24, "note": "OKX 只保留約 1 天"},
    {"name": "爆倉紀錄", "keep_h": 0, "note": "每個幣只能查到最新約 100 筆，停太久的部分會缺"},
    {"name": "OI 5 分鐘", "keep_h": 4, "note": "只保留幾小時"},
    {"name": "OI 1 小時", "keep_h": 24 * 42, "note": "約 6 週"},
    {"name": "資金費率", "keep_h": 24 * 100, "note": "約 3.5 個月"},
]
TASK_DISABLED_HINT = ("Windows 排程目前是停用狀態，資料不會收集；請以系統管理員身分開 PowerShell 執行 "
                      "Enable-ScheduledTask -TaskName okx-reversal-sync")


def flag_path(cfg: Config) -> Path:
    return cfg.paths.db.parent / "PAUSE"


def state(cfg: Config, task_info: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    p = flag_path(cfg)
    out: dict[str, Any] = {"paused": p.exists()}
    if p.exists():
        info: dict[str, Any] = {}
        try:
            info = json.loads(p.read_text(encoding="utf-8") or "{}")
        except ValueError:
            info = {}
        since = int(info.get("since") or p.stat().st_mtime * 1000)
        out.update(since=since, by=info.get("by"), hours=(db.now_ms() - since) / HOUR)
        out["lost"] = lost_feeds(out["hours"])
    if task_info is not None:
        try:
            t = task_info()
            out["task"] = {"registered": t.get("registered"), "state": t.get("state")}
        except Exception as e:
            out["task"] = {"error": type(e).__name__}
    return out


def lost_feeds(hours: float) -> list[dict[str, Any]]:
    return [{**f, "missing_h": max(0.0, hours - f["keep_h"])} for f in UNRECOVERABLE if hours > f["keep_h"]]


def pause(cfg: Config, by: str = "cli") -> dict[str, Any]:
    """Create the flag; keeps the original start time if already paused."""
    p = flag_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    since = db.now_ms()
    if p.exists():
        try:
            since = int(json.loads(p.read_text(encoding="utf-8") or "{}").get("since") or since)
        except ValueError:
            pass
    p.write_text(json.dumps({"since": since, "by": by}), encoding="utf-8")
    return {"ok": True, "message": "資料收集已暫停（排程仍每 5 分鐘觸發但立即結束；紙上交易與提醒一起暫停）", **state(cfg)}


def resume(cfg: Config, task_info: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    st = state(cfg)
    flag_path(cfg).unlink(missing_ok=True)
    msg = "資料收集已恢復"
    if st.get("paused"):
        msg += f"；停了 {st['hours']:.1f} 小時，下一次同步會補回 K 線等可回補的資料"
        if st.get("lost"):
            msg += "；" + "、".join(f["name"] for f in st["lost"]) + " 有一段無法補回"
    now = state(cfg, task_info)
    if (now.get("task") or {}).get("state") == "Disabled":
        msg += "。注意：" + TASK_DISABLED_HINT
    return {"ok": True, "message": msg, "was": st, **now}


def is_paused(cfg: Config) -> bool:
    return flag_path(cfg).exists()


def main(argv: list[str] | None = None) -> int:
    from okxrev.web import tasks
    ap = argparse.ArgumentParser(prog="python -m okxrev.collect")
    ap.add_argument("--config", default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pause", action="store_true")
    g.add_argument("--resume", action="store_true")
    g.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.pause:
        r = pause(cfg, "cli")
    elif args.resume:
        r = resume(cfg, tasks.task_info)
    else:
        r = state(cfg, tasks.task_info)
        r["message"] = f"已暫停，自 {ms_to_str(r['since'])} UTC，{r['hours']:.1f} 小時" if r["paused"] else "執行中"
        if (r.get("task") or {}).get("state") == "Disabled":
            r["message"] += "。注意：" + TASK_DISABLED_HINT
    print(r["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
