from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def setup_logging(log_dir: Path, name: str, level: int = logging.INFO, console: bool = True) -> logging.Logger:
    setup_console_encoding()
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    formatter = logging.Formatter(FMT)
    formatter.converter = time.gmtime

    fh = RotatingFileHandler(log_dir / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    if console and sys.stderr is not None:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(level)
        ch.setFormatter(formatter)
        root.addHandler(ch)

    for noisy in ("urllib3", "matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("okxrev")
