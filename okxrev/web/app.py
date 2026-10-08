from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from okxrev import db
from okxrev.config import Config, load_config
from okxrev.detector import btc_features
from okxrev.sync import collect_status, pause_flag
from okxrev.web import tasks

STATIC = Path(__file__).parent / "static"
INST_RE = re.compile(r"^[A-Z0-9]+-USDT-SWAP$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:]+Z?)?$")
CHART_RE = re.compile(r"^[A-Za-z0-9_\-]+\.png$")
MAX_BARS = 20_000
CSRF_HEADER = "x-okxrev"


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def ok(data: Any) -> JSONResponse:
    return JSONResponse(_clean(data))


class OnlyReq(BaseModel):
    insts: list[str] = Field(min_length=1, max_length=50)


class BacktestReq(BaseModel):
    since: str | None = None
    until: str | None = None
    sweep: bool = False
    charts: bool = False
    insts: list[str] | None = None


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="okx-reversal", docs_url=None, redoc_url=None)
    job = tasks.Job()
    app.state.cfg = cfg
    app.state.job = job

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"detail": "missing header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
                return JSONResponse({"detail": "bad origin"}, status_code=403)
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "bad host"}, status_code=403)
        return await call_next(request)

    def ro():
        try:
            return db.open_db(cfg.paths.db, read_only=True, wait_sec=1.0, max_tries=10)
        except FileNotFoundError:
            raise HTTPException(404, "資料庫尚未建立")

    def _check_inst(i: str) -> str:
        if not INST_RE.match(i):
            raise HTTPException(400, f"invalid inst_id: {i}")
        return i

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status(gaps: bool = False) -> JSONResponse:
        try:
            return ok(collect_status(cfg, with_gaps=gaps, top=20))
        except db.DbBusy:
            raise HTTPException(503, "資料庫忙碌中（同步正在寫入），請稍後再試")

    @app.get("/api/runs")
    def runs(limit: int = 100) -> JSONResponse:
        limit = max(1, min(limit, 2000))
        with ro() as con:
            df = con.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?", [limit]).df()
        return ok(df.to_dict("records"))

    @app.get("/api/log")
    def get_log(name: str = "sync", lines: int = 200) -> JSONResponse:
        if name not in ("sync", "backtest"):
            raise HTTPException(400, "unknown log")
        return ok({"lines": tasks.tail(cfg.paths.logs / f"{name}.log", max(1, min(lines, 2000)))})

    @app.get("/api/task")
    def task() -> JSONResponse:
        return ok({**tasks.task_info(), "paused": pause_flag(cfg).exists(), "sync_running": tasks.sync_running(cfg)})

    @app.post("/api/task/run")
    def task_run() -> JSONResponse:
        return ok(tasks.start_sync(cfg, force=pause_flag(cfg).exists()))

    @app.post("/api/task/pause")
    def task_pause() -> JSONResponse:
        f = pause_flag(cfg)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("paused from web UI\n", encoding="utf-8")
        return ok({"ok": True, "message": "已暫停：之後的排程執行會立即結束", "paused": True})

    @app.post("/api/task/resume")
    def task_resume() -> JSONResponse:
        pause_flag(cfg).unlink(missing_ok=True)
        return ok({"ok": True, "message": "已恢復", "paused": False})

    @app.post("/api/sync/only")
    def sync_only(req: OnlyReq) -> JSONResponse:
        insts = [_check_inst(i.strip().upper()) for i in req.insts]
        return ok(tasks.start_sync(cfg, only=insts, force=True))

    @app.post("/api/backtest")
    def backtest(req: BacktestReq) -> JSONResponse:
        args: list[str] = []
        for flag, v in (("--since", req.since), ("--until", req.until)):
            if v:
                if not DATE_RE.match(v):
                    raise HTTPException(400, f"invalid date: {v}")
                args += [flag, v]
        if req.sweep:
            args.append("--sweep")
        if not req.charts:
            args.append("--no-charts")
        if req.insts:
            args += ["--insts", *[_check_inst(i.strip().upper()) for i in req.insts]]
        return ok(job.start(cfg, args))

    @app.get("/api/backtest/job")
    def backtest_job() -> JSONResponse:
        return ok(job.status())

    @app.post("/api/backtest/stop")
    def backtest_stop() -> JSONResponse:
        return ok(job.stop())

    @app.get("/api/results/summary")
    def results_summary() -> JSONResponse:
        p = cfg.paths.results / "summary.json"
        if not p.exists():
            raise HTTPException(404, "尚無回測結果")
        return ok({"mtime": p.stat().st_mtime, **json.loads(p.read_text(encoding="utf-8"))})

    @app.get("/api/results/sweep")
    def results_sweep() -> JSONResponse:
        p = cfg.paths.results / "sweep.csv"
        if not p.exists():
            return ok([])
        return ok(pd.read_csv(p).to_dict("records"))

    @app.get("/api/results/events")
    def results_events(mode: str | None = None, inst: str | None = None, regime: str | None = None) -> JSONResponse:
        p = cfg.paths.results / "events.csv"
        if not p.exists():
            return ok({"events": [], "charts": []})
        df = pd.read_csv(p)
        if mode:
            df = df[df["mode"] == mode]
        if inst:
            df = df[df["inst_id"].str.contains(inst.upper(), regex=False)]
        if regime:
            df = df[df["btc_regime"] == regime]
        charts_dir = cfg.paths.charts
        charts = sorted(f.name for f in charts_dir.glob("*.png")) if charts_dir.exists() else []
        return ok({"events": df.head(5000).to_dict("records"), "total": int(len(df)), "charts": charts})

    @app.get("/charts/{name}")
    def chart(name: str) -> FileResponse:
        if not CHART_RE.match(name):
            raise HTTPException(400, "bad name")
        p = cfg.paths.charts / name
        if not p.exists():
            raise HTTPException(404, "not found")
        return FileResponse(p, media_type="image/png")

    @app.get("/api/instruments")
    def instruments() -> JSONResponse:
        with ro() as con:
            df = db.sync_overview(con)
        df = df[df["newest_ts"].notna()]
        return ok(df[["inst_id", "state", "oldest_ts", "newest_ts", "backfill_done"]].to_dict("records"))

    @app.get("/api/candles")
    def candles(inst: str, start: int | None = None, end: int | None = None, btc: bool = True) -> JSONResponse:
        _check_inst(inst)
        with ro() as con:
            if start is None and end is None:
                mx = con.execute("SELECT MAX(ts) FROM candles WHERE inst_id = ?", [inst]).fetchone()[0]
                if mx is None:
                    raise HTTPException(404, "no data")
                end = int(mx) + db.BAR_MS
                start = end - 3 * 86_400_000
            elif start is None:
                start = int(end) - 3 * 86_400_000
            elif end is None:
                end = int(start) + 3 * 86_400_000
            if (end - start) / db.BAR_MS > MAX_BARS:
                start = end - MAX_BARS * db.BAR_MS
            df = db.load_candles(con, inst, start, end)
            b = db.load_candles(con, cfg.universe["btc_inst"], start - 300 * db.BAR_MS, end) if btc else None
        vol_ma = df["vol_quote"].shift(1).rolling(int(cfg.detector["vol_ma_bars"])).mean()
        out: dict[str, Any] = {
            "inst": inst, "start": start, "end": end,
            "t": (df["ts"] // 1000).tolist(),
            "o": df["open"].tolist(), "h": df["high"].tolist(), "l": df["low"].tolist(), "c": df["close"].tolist(),
            "v": df["vol_quote"].tolist(), "vma": vol_ma.tolist(),
        }
        if b is not None and not b.empty:
            f = btc_features(b, cfg.btc_regime)
            f = f[(f.index >= start) & (f.index < end)]
            out["btc"] = {"t": (f.index.to_numpy() // 1000).tolist(), "c": f["btc_close"].tolist(), "regime": f["btc_regime"].tolist()}
        ev_path = cfg.paths.results / "events.csv"
        if ev_path.exists():
            ev = pd.read_csv(ev_path, usecols=["mode", "inst_id", "event_ts", "red_ret_prev_close", "fwd_ret_24h"])
            ev = ev[(ev["inst_id"] == inst) & (ev["event_ts"] >= start) & (ev["event_ts"] < end)]
            out["events"] = ev.to_dict("records")
        return ok(out)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.web")
    ap.add_argument("--config", default=None)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    import threading
    import webbrowser

    import uvicorn

    cfg = load_config(args.config)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"okx-reversal web UI: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
