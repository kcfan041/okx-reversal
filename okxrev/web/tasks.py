from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from okxrev.config import Config

TASK_NAME = "okx-reversal-sync"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PRIORITY_FLAGS = {"idle": 0x00000040, "below_normal": 0x00004000, "normal": 0}
THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "OKXREV_THREADS")


def background_settings(cfg: Config) -> tuple[int, int]:
    b = cfg.raw.get("background") or {}
    prio = str(b.get("priority", "idle")).lower()
    flags = CREATE_NO_WINDOW | (PRIORITY_FLAGS.get(prio, PRIORITY_FLAGS["idle"]) if sys.platform == "win32" else 0)
    return flags, max(1, int(b.get("threads", 2)))


def background_env(cfg: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    _, n = background_settings(cfg)
    env = dict(os.environ if base is None else base)
    env.update({k: str(n) for k in THREAD_VARS})
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def background_popen(cfg: Config, args: list[str], **kw: Any) -> subprocess.Popen:
    """Start a CPU-heavy helper process at low priority with a small thread budget so games/recording keep priority."""
    flags, _ = background_settings(cfg)
    kw.setdefault("cwd", str(cfg.paths.root))
    kw.setdefault("stdin", subprocess.DEVNULL)
    kw.setdefault("stdout", subprocess.DEVNULL)
    kw.setdefault("stderr", subprocess.DEVNULL)
    kw["env"] = background_env(cfg, kw.get("env"))
    return subprocess.Popen(args, creationflags=flags, **kw)


def venv_python(cfg: Config, windowless: bool = False) -> str:
    scripts = cfg.paths.root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    name = ("pythonw.exe" if windowless else "python.exe") if sys.platform == "win32" else "python"
    p = scripts / name
    return str(p) if p.exists() else sys.executable


def _ps(cmd: str, timeout: float = 20) -> tuple[int, str]:
    if sys.platform != "win32":
        return 1, "scheduled tasks are only available on Windows"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def task_info() -> dict[str, Any]:
    code, out = _ps(
        f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction Stop; $i=$t|Get-ScheduledTaskInfo; "
        "[pscustomobject]@{state=[string]$t.State; last_run=$i.LastRunTime.ToUniversalTime().ToString('o'); "
        "last_result=$i.LastTaskResult; next_run=if($i.NextRunTime){$i.NextRunTime.ToUniversalTime().ToString('o')}else{$null}} "
        "| ConvertTo-Json -Compress"
    )
    if code != 0:
        return {"registered": False, "error": out.strip()[:300]}
    try:
        d = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"registered": False, "error": out.strip()[:300]}
    d["registered"] = True
    return d


def sync_running(cfg: Config) -> bool:
    lock = FileLock(str(cfg.paths.lock), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return True
    lock.release()
    return False


def start_sync(cfg: Config, only: list[str] | None = None, force: bool = False) -> dict[str, Any]:
    if sync_running(cfg):
        return {"ok": False, "message": "同步正在執行中"}
    if not only and not force:
        code, out = _ps(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")
        if code == 0:
            return {"ok": True, "message": "已透過工作排程器啟動", "via": "task"}
    args = [venv_python(cfg, windowless=True), "-m", "okxrev.sync"]
    if only:
        args += ["--only", *only]
    if force:
        args.append("--force")
    subprocess.Popen(
        args, cwd=str(cfg.paths.root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
    )
    return {"ok": True, "message": "已直接啟動同步程序", "via": "process"}


def tail(path: Path, lines: int = 200, max_bytes: int = 512 * 1024) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    text = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and text:
        text = text[1:]
    return text[-lines:]


class Job:
    def __init__(self, module: str = "okxrev.backtest", label: str = "回測") -> None:
        self.module = module
        self.label = label
        self.lock = threading.Lock()
        self.proc: subprocess.Popen[str] | None = None
        self.lines: deque[str] = deque(maxlen=2000)
        self.args: list[str] = []
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.returncode: int | None = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.proc is not None and self.returncode is None,
                "args": self.args,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "returncode": self.returncode,
                "log": list(self.lines),
            }

    def start(self, cfg: Config, args: list[str], module: str | None = None) -> dict[str, Any]:
        with self.lock:
            if self.proc is not None and self.returncode is None:
                return {"ok": False, "message": f"已有{self.label}正在執行"}
            self.lines.clear()
            self.args = args
            self.started_at = time.time()
            self.ended_at = None
            self.returncode = None
            self.proc = background_popen(
                cfg, [venv_python(cfg), "-u", "-m", module or self.module, *args],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            )
        threading.Thread(target=self._pump, daemon=True).start()
        return {"ok": True, "message": f"{self.label}已啟動"}

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.lock:
                self.lines.append(line.rstrip("\n"))
        rc = self.proc.wait()
        with self.lock:
            self.returncode = rc
            self.ended_at = time.time()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if self.proc is None or self.returncode is not None:
                return {"ok": False, "message": f"沒有執行中的{self.label}"}
            self.proc.terminate()
        return {"ok": True, "message": "已送出停止"}
