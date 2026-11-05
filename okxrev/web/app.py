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


class ChatReq(BaseModel):
    conv_id: str | None = Field(default=None, max_length=40)
    text: str | None = Field(default=None, max_length=8000)
    inst: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=60)
    cont: bool = False


class WatchReq(BaseModel):
    insts: list[str] = Field(max_length=50)


class SettingsReq(BaseModel):
    changes: dict[str, Any] = Field(max_length=60)


class AccountRunReq(BaseModel):
    sync: bool = True
    replicate: bool = True


class LabTemplateReq(BaseModel):
    template: dict[str, Any] = Field(default_factory=dict)
    blind: bool = False


class LabSimilarReq(BaseModel):
    inst: str = Field(max_length=40)
    at: int
    confirm: int = Field(default=200, ge=0, le=300)


class LabRuleReq(BaseModel):
    name: str = Field(default="", max_length=60)
    template: dict[str, Any] = Field(default_factory=dict)
    side: str = Field(max_length=5)
    exit: str = Field(max_length=12)
    source: dict[str, Any] | None = None


class LabRuleOpReq(BaseModel):
    id: str = Field(max_length=16)
    op: str = Field(max_length=10)
    name: str | None = Field(default=None, max_length=60)


TRADE_COLS = [
    "trade_id", "source", "bot_type", "is_open", "unrealized_usd", "inst_type", "inst_id", "candle_inst", "candle_proxy", "side", "open_ts", "close_ts", "hold_min",
    "entry_px", "exit_px", "notional_usd", "lever", "close_type", "ret_px", "ret_net", "pnl_usd", "fee_usd", "funding_usd",
    "path_ok", "mae", "mfe", "capture", "post_12", "post_72", "post_288", "entry_range_pos",
    "f_r1h", "f_r24h", "f_btc_regime", "bars_since_pump_red", "bars_to_next_pump_red", "revenge", "loss_streak_before", "size_vs_recent",
]


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="okx-reversal", docs_url=None, redoc_url=None)
    job = tasks.Job()
    acct_job = tasks.Job("okxrev.review", "戰績分析")
    app.state.cfg = cfg
    app.state.job = job
    app.state.acct_job = acct_job

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
    def index():
        """index.html with ?v=<mtime> on app.js / style.css so a code change always reaches the browser; the page
        itself is never cached."""
        from fastapi.responses import HTMLResponse
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "style.css"):
            v = int((STATIC / name).stat().st_mtime)
            html = html.replace(f'/static/{name}"', f'/static/{name}?v={v}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

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
        from okxrev import collect
        return ok({**tasks.task_info(), "paused": pause_flag(cfg).exists(), "collect": collect.state(cfg), "sync_running": tasks.sync_running(cfg)})

    @app.post("/api/task/run")
    def task_run(force: bool = False) -> JSONResponse:
        if pause_flag(cfg).exists() and not force:
            return ok({"ok": False, "needs_confirm": True, "message": "資料收集已暫停；確定要強制同步一次嗎？"})
        return ok(tasks.start_sync(cfg, force=pause_flag(cfg).exists()))

    collect_cache: dict[str, Any] = {"t": 0.0, "task": None}

    def _task_cached() -> dict[str, Any]:
        import time as _t
        if collect_cache["task"] is None or _t.monotonic() - collect_cache["t"] > 30:
            collect_cache.update(t=_t.monotonic(), task=tasks.task_info())
        return collect_cache["task"]

    @app.get("/api/collect")
    def collect_state() -> JSONResponse:
        from okxrev import collect
        return ok(collect.state(cfg, _task_cached))

    @app.post("/api/collect/pause")
    @app.post("/api/task/pause")
    def collect_pause() -> JSONResponse:
        from okxrev import collect
        return ok(collect.pause(cfg, "web"))

    @app.post("/api/collect/resume")
    @app.post("/api/task/resume")
    def collect_resume() -> JSONResponse:
        from okxrev import collect
        collect_cache["task"] = None
        return ok(collect.resume(cfg, _task_cached))

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
        try:
            from okxrev import announce
            cp = announce.campaign_tokens(announce.load_campaigns(cfg))
            if len(cp):
                cp = cp[(cp["inst_id"] == inst) & (cp["p_time"] < end) & (cp["end_ts"].fillna(cp["p_time"]) + 7 * 86_400_000 > start)]
                out["campaigns"] = [{"kind": announce.KIND_LABEL.get(r.kind, r.kind), "grp": r.grp, "title": r.title, "p_time": r.p_time,
                                     "start_ts": None if pd.isna(r.start_ts) else int(r.start_ts), "end_ts": None if pd.isna(r.end_ts) else int(r.end_ts),
                                     "dist_ts": None if pd.isna(r.dist_ts) else int(r.dist_ts)} for r in cp.itertuples(index=False)]
        except Exception:
            pass
        return ok(out)

    @app.get("/api/account/status")
    def account_status() -> JSONResponse:
        from okxrev import account
        try:
            st = account.status(cfg)
        except db.DbBusy:
            raise HTTPException(503, "帳戶資料庫忙碌中")
        st.pop("db", None)
        rv = cfg.paths.results / "review.json"
        rp = cfg.paths.results / "replicate.json"
        st["review_mtime"] = rv.stat().st_mtime if rv.exists() else None
        st["replicate_mtime"] = rp.stat().st_mtime if rp.exists() else None
        return ok(st)

    @app.post("/api/account/run")
    def account_run(req: AccountRunReq) -> JSONResponse:
        from okxrev.config import load_credentials
        args: list[str] = []
        if req.sync:
            if load_credentials(cfg.paths.root) is None:
                raise HTTPException(400, "未設定 OKX API 金鑰：請在專案根目錄 .env 設定 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE（唯讀權限）")
            args.append("--sync")
        if req.replicate:
            args.append("--replicate")
        return ok(acct_job.start(cfg, args))

    @app.get("/api/account/job")
    def account_job() -> JSONResponse:
        return ok(acct_job.status())

    @app.post("/api/account/stop")
    def account_stop() -> JSONResponse:
        return ok(acct_job.stop())

    def _range(since: str | None, until: str | None) -> tuple[int | None, int | None]:
        from okxrev.config import parse_utc_ms
        out = []
        for v in (since, until):
            if v and not DATE_RE.match(v):
                raise HTTPException(400, f"invalid date: {v}")
            out.append(parse_utc_ms(v) if v else None)
        return out[0], out[1]

    def _trades(since: str | None, until: str | None, inst_type: str | None, inst: str | None, side: str | None,
                source: str | None) -> pd.DataFrame:
        from okxrev import account
        if inst_type and inst_type.upper() not in ("SWAP", "FUTURES", "SPOT"):
            raise HTTPException(400, "bad inst_type")
        if side and side not in ("long", "short"):
            raise HTTPException(400, "bad side")
        if inst and not re.match(r"^[A-Za-z0-9\-]{1,40}$", inst):
            raise HTTPException(400, "bad inst")
        srcs = tuple(s for s in (source or "").split(",") if s) or account.SOURCES
        if any(s not in account.SOURCES for s in srcs):
            raise HTTPException(400, "bad source")
        lo, hi = _range(since, until)
        try:
            t = account.load_trades(cfg, sources=srcs)
        except db.DbBusy:
            raise HTTPException(503, "帳戶資料庫忙碌中")
        p = cfg.paths.results / "trades_enriched.csv"
        if len(t) and p.exists():
            e = pd.read_csv(p)
            extra = [c for c in e.columns if c not in t.columns]
            t = t.merge(e[["trade_id", *extra]], on="trade_id", how="left")
        return account.filter_trades(t, lo, hi, inst_type, inst, side)

    @app.get("/api/account/summary")
    def account_summary(since: str | None = None, until: str | None = None, inst_type: str | None = None,
                        inst: str | None = None, side: str | None = None, source: str | None = None) -> JSONResponse:
        from okxrev import account
        t = _trades(since, until, inst_type, inst, side, source)
        return ok(account.summarize_trades(t))

    @app.get("/api/account/bots")
    def account_bots() -> JSONResponse:
        p = cfg.paths.results / "bots.json"
        return ok(json.loads(p.read_text(encoding="utf-8")) if p.exists() else None)

    @app.get("/api/account/trades")
    def account_trades(since: str | None = None, until: str | None = None, inst_type: str | None = None,
                       inst: str | None = None, side: str | None = None, source: str | None = None) -> JSONResponse:
        t = _trades(since, until, inst_type, inst, side, source)
        t = t.sort_values("close_ts", ascending=False)
        keep = [c for c in TRADE_COLS if c in t.columns]
        return ok({"total": int(len(t)), "trades": t[keep].head(5000).to_dict("records")})

    @app.get("/api/account/review")
    def account_review() -> JSONResponse:
        out: dict[str, Any] = {}
        for k in ("review", "replicate"):
            p = cfg.paths.results / f"{k}.json"
            out[k] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        return ok(out)

    ml_job = tasks.Job("okxrev.ml", "模型訓練")
    tick_job = tasks.Job("okxrev.paper", "紙上交易")

    def _paper_con(src: str = "live"):
        from okxrev import paper, replay
        if src not in ("live", "replay"):
            raise HTTPException(400, "bad source")
        if src == "replay":
            p = replay.out_db_path(cfg)
            if not p.exists():
                return None
            try:
                import duckdb
                return duckdb.connect(str(p), read_only=True)
            except Exception:
                raise HTTPException(503, "盲測資料庫忙碌中（可能正在重播）")
        try:
            return paper.connect(cfg, read_only=True)
        except FileNotFoundError:
            return None
        except db.DbBusy:
            raise HTTPException(503, "紙上交易資料庫忙碌中")

    @app.post("/api/chat")
    def chat_send(req: ChatReq):
        from fastapi.responses import StreamingResponse
        from okxrev import chat
        from okxrev.chat_tools import ToolError, norm_inst
        if not req.text and not req.cont:
            raise HTTPException(400, "請輸入問題")
        if req.conv_id and not re.match(r"^[0-9a-f]{6,40}$", req.conv_id):
            raise HTTPException(400, "bad conv_id")
        inst = None
        if req.inst:
            try:
                inst = norm_inst(req.inst)
            except ToolError as e:
                raise HTTPException(400, str(e))
        if req.model and not re.match(r"^[A-Za-z0-9_.:/\-]{1,60}$", req.model):
            raise HTTPException(400, "bad model")

        def gen():
            for ev in chat.run_turn(cfg, req.conv_id, req.text, inst, req.model, req.cont):
                yield "data: " + json.dumps(_clean(ev), ensure_ascii=False) + "\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/chat/conversations")
    def chat_list() -> JSONResponse:
        from okxrev import chat
        con = chat.connect(cfg, read_only=True)
        try:
            return ok(chat.list_conversations(con))
        finally:
            con.close()

    @app.get("/api/chat/conversation")
    def chat_get(id: str) -> JSONResponse:
        from okxrev import chat
        if not re.match(r"^[0-9a-f]{6,40}$", id):
            raise HTTPException(400, "bad id")
        return ok(chat.transcript(cfg, id))

    @app.post("/api/chat/delete")
    def chat_delete(req: ChatReq) -> JSONResponse:
        from okxrev import chat
        if not req.conv_id or not re.match(r"^[0-9a-f]{6,40}$", req.conv_id):
            raise HTTPException(400, "bad id")
        con = chat.connect(cfg)
        try:
            con.execute("DELETE FROM messages WHERE conv_id = ?", [req.conv_id])
            con.execute("DELETE FROM conversations WHERE id = ?", [req.conv_id])
        finally:
            con.close()
        return ok({"ok": True, "message": "已刪除"})

    @app.get("/api/chat/info")
    def chat_info() -> JSONResponse:
        from okxrev import chat, chat_tools
        c = chat.settings(cfg)
        con = chat.connect(cfg, read_only=True)
        try:
            used = chat.calls_today(con)
        finally:
            con.close()
        return ok({"model": c["model"], "models": ["claude", "gpt", "default"], "enabled": bool(c["llm"].get("LLM_API_KEY")),
                   "limits": {k: c[k] for k in ("max_rounds", "max_calls", "max_tool_chars", "daily_calls")}, "calls_today": used,
                   "tools": [{"name": k, "desc": v["desc"]} for k, v in chat_tools.TOOLS.items()],
                   "watchlist": (cfg.raw.get("heat") or {}).get("watchlist") or []})

    replay_job = tasks.Job("okxrev.replay", "舊資料盲測")
    heat_job = tasks.Job("okxrev.heat", "過熱驗證")

    @app.get("/api/heat/snapshot")
    def heat_snapshot() -> JSONResponse:
        from okxrev import heat
        p = cfg.paths.results / "heat_snapshot.json"
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"rows": [], "ts": None}
        data["watchlist"] = heat.hcfg(cfg).get("watchlist") or []
        data["threshold"] = heat.hcfg(cfg)["alert_threshold"]
        data["dims"] = {k: {"label": v["label"], "experimental": bool(v.get("experimental"))} for k, v in heat.DIMENSIONS.items()}
        return ok(data)

    @app.get("/api/heat/coin")
    def heat_coin(inst: str, days: int = 10) -> JSONResponse:
        from okxrev import heat
        _check_inst(inst)
        try:
            return ok(heat.coin_detail(cfg, inst, max(2, min(days, 30))))
        except db.DbBusy:
            raise HTTPException(503, "資料庫忙碌中")

    @app.get("/api/heat/alerts")
    def heat_alerts(hours: int = 24) -> JSONResponse:
        con = _paper_con()
        if con is None:
            return ok([])
        try:
            df = con.execute("SELECT * FROM heat_alerts WHERE ts >= ? ORDER BY ts DESC", [db.now_ms() - max(1, min(hours, 720)) * 3_600_000]).df()
        except Exception:
            df = pd.DataFrame()
        finally:
            con.close()
        return ok(df.to_dict("records"))

    @app.get("/api/heat/validation")
    def heat_validation() -> JSONResponse:
        from okxrev import heatdata
        p = cfg.paths.results / "heat.json"
        out = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        return ok({"result": out, "backfill": heatdata.backfill_status(cfg)})

    @app.post("/api/heat/evaluate")
    def heat_evaluate() -> JSONResponse:
        return ok(heat_job.start(cfg, ["--evaluate"]))

    @app.get("/api/heat/job")
    def heat_job_status() -> JSONResponse:
        return ok(heat_job.status())

    @app.post("/api/heat/watchlist")
    def heat_watchlist(req: WatchReq) -> JSONResponse:
        insts = [_check_inst(i.strip().upper()) for i in req.insts if i.strip()]
        _save_settings({"heat.watchlist": insts})
        return ok({"ok": True, "message": f"關注清單已更新（{len(insts)} 個）", "watchlist": insts})

    def _save_settings(changes: dict[str, Any]) -> dict[str, Any]:
        from okxrev import settings as S
        try:
            ov = S.save(cfg.paths.root, changes)
        except S.SettingsError as e:
            raise HTTPException(400, str(e))
        base = cfg._cache.get("base_raw") or cfg.raw
        import copy
        fresh = S.apply(copy.deepcopy(base), ov)
        cfg.raw.clear()
        cfg.raw.update(fresh)
        return ov

    @app.get("/api/settings")
    def settings_get() -> JSONResponse:
        from okxrev import settings as S
        from okxrev.config import load_credentials, load_env
        base = cfg._cache.get("base_raw") or cfg.raw
        ov = S.load_overrides(cfg.paths.root)
        llm = load_env(("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"), cfg.paths.root)
        status = {"okx_key": load_credentials(cfg.paths.root) is not None, "llm": bool(llm.get("LLM_API_KEY") and llm.get("LLM_BASE_URL")),
                  "llm_model": llm.get("LLM_MODEL"), "settings_file": str(S.settings_path(cfg.paths.root))}
        return ok({"fields": S.describe(base, ov), "groups": S.GROUPS, "status": status})

    @app.post("/api/settings")
    def settings_post(req: SettingsReq) -> JSONResponse:
        ov = _save_settings(req.changes)
        return ok({"ok": True, "message": f"已儲存（{len(req.changes)} 項）", "overrides": ov})

    @app.get("/api/replay/summary")
    def replay_summary() -> JSONResponse:
        from okxrev import replay
        try:
            c = replay.compare(cfg)
        except Exception as e:
            raise HTTPException(503, f"盲測資料庫忙碌中：{type(e).__name__}")
        c["backfill"] = replay.backfill_status(cfg)
        return ok(c)

    @app.post("/api/replay/run")
    def replay_run() -> JSONResponse:
        return ok(replay_job.start(cfg, ["--run"]))

    @app.get("/api/replay/job")
    def replay_job_status() -> JSONResponse:
        return ok(replay_job.status())

    @app.get("/api/paper/status")
    def paper_status() -> JSONResponse:
        from okxrev import llm, ml, paper
        try:
            s = paper.status(cfg)
        except db.DbBusy:
            raise HTTPException(503, "紙上交易資料庫忙碌中")
        s["models"] = ml.latest_meta(cfg)
        s["llm_enabled"] = llm.enabled(cfg)
        s["llm_model"] = llm.settings(cfg).get("LLM_MODEL")
        return ok(s)

    @app.get("/api/paper/positions")
    def paper_positions(trader: str | None = None, state: str = "open", book: str = "C", limit: int = 500, src: str = "live") -> JSONResponse:
        if state not in ("open", "closed", "all") or book not in ("A", "B", "C"):
            raise HTTPException(400, "bad params")
        if trader and not re.match(r"^[A-Za-z0-9_\-.]{1,80}$", trader):
            raise HTTPException(400, "bad trader")
        con = _paper_con(src)
        if con is None:
            return ok([])
        try:
            sql = ("SELECT p.*, d.reason, d.score, d.model_ver FROM positions p LEFT JOIN decisions d USING (decision_id) WHERE p.book = ?")
            args: list[Any] = [book]
            if state == "open":
                sql += " AND p.state IN ('open', 'pending')"
            elif state == "closed":
                sql += " AND p.state = 'closed'"
            if trader:
                sql += " AND p.trader_id = ?"
                args.append(trader)
            sql += " ORDER BY COALESCE(p.exit_ts, p.entry_ts, p.signal_ts) DESC LIMIT ?"
            args.append(max(1, min(limit, 5000)))
            df = con.execute(sql, args).df()
        finally:
            con.close()
        return ok(df.to_dict("records"))

    @app.get("/api/paper/follow")
    def paper_follow(trader: str, src: str = "live", limit: int = 30) -> JSONResponse:
        if not re.match(r"^follow_[A-Za-z0-9_]{1,60}$", trader):
            raise HTTPException(400, "bad trader")
        from okxrev.follow import FOLLOW_SETS, HOUR
        fs = next((x for x in FOLLOW_SETS if x["id"] == trader), None)
        con = _paper_con(src)
        if con is None:
            return ok({"picks": []})
        try:
            try:
                rows = con.execute("SELECT period_ts, picks FROM follow_picks WHERE trader_id = ? ORDER BY period_ts DESC LIMIT ?",
                                   [trader, max(1, min(limit, 500))]).fetchall()
            except Exception:
                rows = []
            src_n = con.execute("SELECT json_extract_string(d.features, '$._follow_src') s, COUNT(*), AVG(p.ret_net) FROM positions p "
                                "JOIN decisions d USING (decision_id) WHERE p.trader_id = ? AND p.book = 'C' AND p.state = 'closed' GROUP BY 1",
                                [trader]).fetchall()
        finally:
            con.close()
        picks = [{"period_ts": int(p), "picks": json.loads(k)} for p, k in rows]
        nxt = None
        if fs:
            step = fs["step_h"] * HOUR
            nxt = (db.now_ms() // step + 1) * step
        return ok({"set": fs, "picks": picks, "next_repick": nxt,
                   "by_source": [{"trader_id": s, "n": int(n), "mean": float(m) if m is not None else None} for s, n, m in src_n]})

    @app.get("/api/paper/equity")
    def paper_equity(book: str = "A", src: str = "live") -> JSONResponse:
        if book not in ("A", "B"):
            raise HTTPException(400, "bad book")
        con = _paper_con(src)
        if con is None:
            return ok({})
        try:
            df = con.execute("SELECT trader_id, ts, equity FROM equity_hist WHERE book = ? ORDER BY ts", [book]).df()
        finally:
            con.close()
        return ok({t: {"t": (g["ts"] // 1000).tolist(), "v": g["equity"].round(2).tolist()} for t, g in df.groupby("trader_id")})

    @app.get("/api/paper/decisions")
    def paper_decisions(kind: str = "llm", limit: int = 200) -> JSONResponse:
        con = _paper_con()
        if con is None:
            return ok([])
        try:
            if kind == "llm":
                df = con.execute("SELECT d.*, c.verdict, c.cost FROM decisions d LEFT JOIN llm_calls c USING (decision_id) "
                                 "WHERE d.trader_id LIKE 'llm_%' OR d.trader_id LIKE 'llmb_%' ORDER BY d.bar_ts DESC LIMIT ?", [max(1, min(limit, 2000))]).df()
            else:
                df = con.execute("SELECT * FROM decisions ORDER BY bar_ts DESC LIMIT ?", [max(1, min(limit, 2000))]).df()
        finally:
            con.close()
        return ok(df.drop(columns=["features"], errors="ignore").to_dict("records"))

    @app.post("/api/paper/tick")
    def paper_tick(force: bool = False) -> JSONResponse:
        if pause_flag(cfg).exists():
            if not force:
                return ok({"ok": False, "needs_confirm": True, "message": "資料收集已暫停；確定要強制跑一輪紙上交易嗎？"})
            return ok(tick_job.start(cfg, ["--tick", "--force"]))
        return ok(tick_job.start(cfg, ["--tick"]))

    @app.post("/api/paper/train")
    def paper_train() -> JSONResponse:
        return ok(ml_job.start(cfg, []))

    @app.get("/api/paper/jobs")
    def paper_jobs() -> JSONResponse:
        return ok({"train": ml_job.status(), "tick": tick_job.status()})

    @app.post("/api/paper/export")
    def paper_export() -> JSONResponse:
        from okxrev import paper
        try:
            files = paper.export(cfg, "parquet")
        except FileNotFoundError:
            raise HTTPException(404, "尚無紙上交易資料")
        return ok({"ok": True, "message": f"已匯出 {len(files)} 個檔案到 results/paper_export/", "files": [f.split("\\")[-1] for f in files]})

    disc_job = tasks.Job("okxrev.discover", "進場探索")

    @app.post("/api/discover/run")
    def discover_run() -> JSONResponse:
        return ok(disc_job.start(cfg, []))

    @app.get("/api/discover/job")
    def discover_job() -> JSONResponse:
        return ok(disc_job.status())

    @app.post("/api/discover/stop")
    def discover_stop() -> JSONResponse:
        return ok(disc_job.stop())

    @app.get("/api/discover/results")
    def discover_results() -> JSONResponse:
        p = cfg.paths.results / "discover.json"
        if not p.exists():
            return ok(None)
        return ok({"mtime": p.stat().st_mtime, **json.loads(p.read_text(encoding="utf-8"))})

    scan_cache: dict[str, Any] = {"t": 0.0, "data": None}

    @app.get("/api/discover/scan")
    def discover_scan(refresh: bool = False) -> JSONResponse:
        import time as _t
        from okxrev import discover
        if not refresh and scan_cache["data"] is not None and _t.monotonic() - scan_cache["t"] < 240:
            return ok(scan_cache["data"])
        try:
            data = discover.live_scan(cfg)
        except db.DbBusy:
            raise HTTPException(503, "資料庫忙碌中")
        scan_cache.update(t=_t.monotonic(), data=data)
        return ok(data)

    @app.get("/api/account/exit-plan")
    def account_exit_plan() -> JSONResponse:
        p = cfg.paths.results / "exits.json"
        if not p.exists():
            return ok(None)
        return ok({"mtime": p.stat().st_mtime, **json.loads(p.read_text(encoding="utf-8"))})

    live_cache: dict[str, Any] = {"t": 0.0, "data": None}

    @app.get("/api/account/positions")
    def account_positions(refresh: bool = False) -> JSONResponse:
        import time as _t
        from okxrev import account, live
        from okxrev.config import load_credentials
        if load_credentials(cfg.paths.root) is None:
            raise HTTPException(400, "未設定 OKX API 金鑰")
        if not refresh and live_cache["data"] is not None and _t.monotonic() - live_cache["t"] < 15:
            return ok(live_cache["data"])
        try:
            data = live.live_plan(cfg)
        except account.NoCredentials as e:
            raise HTTPException(400, str(e))
        except db.DbBusy:
            raise HTTPException(503, "帳戶資料庫忙碌中")
        except Exception as e:
            raise HTTPException(502, f"讀取持倉失敗：{type(e).__name__}: {str(e)[:200]}")
        live_cache.update(t=_t.monotonic(), data=data)
        return ok(data)

    # ------------------------------------------------------------ event lab
    lab_job = tasks.Job("okxrev.events_lab", "事件實驗室")

    def _lab_inst(i: str) -> str:
        from okxrev.chat_tools import ToolError, norm_inst
        try:
            return _check_inst(norm_inst(i))
        except ToolError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/lab/fine")
    def lab_fine(inst: str, bar: str = "1m", start: int | None = None, end: int | None = None, trades: bool = False) -> JSONResponse:
        from okxrev import fine
        inst = _lab_inst(inst)
        if bar not in ("1s", "1m"):
            raise HTTPException(400, "bar 只能是 1s 或 1m")
        now = db.now_ms()
        end_ = min(end or now, now + 60_000)
        start_ = start or (end_ - (30 * 60_000 if bar == "1s" else 6 * 3_600_000))
        try:
            df = fine.candles(cfg, inst, bar, int(start_), int(end_))
        except fine.RangeError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, f"OKX 取得失敗：{type(e).__name__}: {str(e)[:150]}")
        m = fine.micro(df, bar) if len(df) else {}
        out: dict[str, Any] = {"inst": inst, "bar": bar, "start": start_, "end": end_, "t": (df["ts"] // 1000).tolist(), "o": df["open"].tolist(),
                               "h": df["high"].tolist(), "l": df["low"].tolist(), "c": df["close"].tolist(),
                               "v": df["vol_quote"].fillna(df["vol"]).tolist(), "micro": m}
        if trades:
            try:
                tr = fine.trades(cfg, inst)
                out["flow"] = fine.trade_flow(tr)
                out["trades"] = tr.tail(60).iloc[::-1].to_dict("records")
            except Exception as e:
                out["flow_error"] = f"{type(e).__name__}"
            try:
                with db.open_db(cfg.paths.db, read_only=True, wait_sec=0.5, max_tries=4) as con:
                    out["liquidations"] = con.execute("SELECT ts, pos_side, px, sz FROM liquidations WHERE inst_id = ? ORDER BY ts DESC LIMIT 30",
                                                      [inst]).df().to_dict("records")
            except Exception:
                out["liquidations"] = []
        out["summary"] = fine.summary_line(m, out.get("flow")) if m else ""
        return ok(out)

    @app.get("/api/lab/status")
    def lab_status() -> JSONResponse:
        from okxrev import custom_rules, events_lab
        return ok({"index": events_lab.index_status(cfg), "rules": len(custom_rules.load(cfg)), "blind_attempts": len(events_lab.blind_log(cfg)),
                   "exits": events_lab.EXIT_LABEL, "defaults": events_lab.TEMPLATE_DEFAULT})

    @app.post("/api/lab/build-index")
    def lab_build() -> JSONResponse:
        return ok(lab_job.start(cfg, ["--build-index"]))

    @app.get("/api/lab/job")
    def lab_job_status() -> JSONResponse:
        return ok(lab_job.status())

    @app.post("/api/lab/stop")
    def lab_stop() -> JSONResponse:
        return ok(lab_job.stop())

    recent_cache: dict[str, Any] = {"t": 0.0, "data": None}

    @app.get("/api/lab/recent")
    def lab_recent(hours: int = 24, refresh: bool = False) -> JSONResponse:
        import time as _t
        from okxrev import events_lab
        if not refresh and recent_cache["data"] is not None and _t.monotonic() - recent_cache["t"] < 240 and recent_cache.get("h") == hours:
            return ok(recent_cache["data"])
        try:
            data = events_lab.recent_events(cfg, max(1, min(hours, 72)))
        except db.DbBusy:
            raise HTTPException(503, "資料庫忙碌中")
        recent_cache.update(t=_t.monotonic(), data=data, h=hours)
        return ok(data)

    @app.get("/api/lab/analyze")
    def lab_analyze(inst: str, at: int) -> JSONResponse:
        from okxrev import events_lab
        inst = _lab_inst(inst)
        try:
            return ok(events_lab.analyze_event(cfg, inst, int(at)))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except db.DbBusy:
            raise HTTPException(503, "資料庫忙碌中")

    @app.post("/api/lab/test")
    def lab_test(req: LabTemplateReq) -> JSONResponse:
        from okxrev import events_lab
        try:
            return ok(events_lab.test_template(cfg, req.template, blind=req.blind))
        except FileNotFoundError:
            raise HTTPException(404, "尚未建立事件索引（按「建立索引」）")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/lab/similar")
    def lab_similar(req: LabSimilarReq) -> JSONResponse:
        from okxrev import events_lab
        inst = _lab_inst(req.inst)
        if not events_lab.index_path(cfg).exists():
            raise HTTPException(404, "尚未建立事件索引（按「建立索引」）")
        args = ["--similar", f"{inst}@{int(req.at)}", "--confirm", str(int(req.confirm)), "--out", "similar_last.json"]
        return ok(lab_job.start(cfg, args))

    @app.get("/api/lab/similar")
    def lab_similar_result() -> JSONResponse:
        p = cfg.paths.results / "lab" / "similar_last.json"
        if not p.exists():
            return ok(None)
        return ok(json.loads(p.read_text(encoding="utf-8")))

    @app.get("/api/lab/rules")
    def lab_rules() -> JSONResponse:
        from okxrev import custom_rules
        rules = custom_rules.load(cfg)
        hits = custom_rules.recent_hits(cfg, None, 300)
        for r in rules:
            mine = [h for h in hits if h["trader_id"] == f"lab_{r['id']}"]
            closed = [h["ret_net"] for h in mine if h.get("ret_net") is not None and not (isinstance(h["ret_net"], float) and math.isnan(h["ret_net"]))]
            r["live"] = {"triggers": len(mine), "closed": len(closed), "mean": float(np.mean(closed)) if closed else None,
                         "last": mine[0]["bar_ts"] if mine else None, "recent": mine[:10]}
            r["verdict_label"] = custom_rules.verdict_label(r)
        return ok(rules)

    @app.post("/api/lab/rules")
    def lab_rule_add(req: LabRuleReq) -> JSONResponse:
        from okxrev import custom_rules
        try:
            r = custom_rules.add(cfg, req.name, req.template, req.side, req.exit, req.source)
        except FileNotFoundError:
            raise HTTPException(404, "尚未建立事件索引")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return ok({"ok": True, "message": f"已存成規則 lab_{r['id']}，下一輪紙上交易開始觀察", "rule": r})

    @app.post("/api/lab/rules/op")
    def lab_rule_op(req: LabRuleOpReq) -> JSONResponse:
        from okxrev import custom_rules
        try:
            if req.op == "delete":
                custom_rules.delete(cfg, req.id)
                return ok({"ok": True, "message": "已刪除（紙上交易紀錄保留）"})
            if req.op in ("pause", "resume"):
                custom_rules.update(cfg, req.id, active=req.op == "resume")
                return ok({"ok": True, "message": "已暫停" if req.op == "pause" else "已恢復"})
            if req.op == "rename":
                custom_rules.update(cfg, req.id, name=req.name)
                return ok({"ok": True, "message": "已改名"})
            if req.op in ("retest", "blind"):
                r = custom_rules.retest(cfg, req.id, blind=req.op == "blind")
                return ok({"ok": True, "message": f"判定：{custom_rules.verdict_label(r)}", "rule": r})
        except KeyError:
            raise HTTPException(404, "找不到規則")
        except ValueError as e:
            raise HTTPException(400, str(e))
        raise HTTPException(400, "bad op")

    camp_job = tasks.Job("okxrev.campaigns", "瓜分活動")

    @app.get("/api/campaigns")
    def campaigns_list() -> JSONResponse:
        from okxrev import announce, campaigns as CP
        allc = announce.load_campaigns(cfg, status=None)
        rows = []
        for r in allc.itertuples(index=False):
            rows.append({"url": r.url, "p_time": r.p_time, "kind": r.kind, "kind_label": announce.KIND_LABEL.get(r.kind, r.kind), "title": r.title,
                         "title_zh": r.title_zh, "tokens": r.tokens, "start_ts": r.start_ts, "end_ts": r.end_ts, "dist_ts": r.dist_ts,
                         "pre_sub_ts": r.pre_sub_ts, "pool": r.pool, "status": r.status, "period_raw": r.period_raw,
                         "grp": "trade" if r.kind in announce.TRADE_KINDS else "earn"})
        st = announce.status(cfg)
        return ok({"live": CP.live_status(cfg), "campaigns": rows, "status": st, "kinds": announce.KIND_LABEL,
                   "has_results": (cfg.paths.results / "campaigns.json").exists()})

    @app.get("/api/campaigns/results")
    def campaigns_results() -> JSONResponse:
        from okxrev import campaigns as CP
        return ok(CP.load(cfg))

    @app.post("/api/campaigns/evaluate")
    def campaigns_evaluate() -> JSONResponse:
        return ok(camp_job.start(cfg, ["--evaluate"]))

    @app.post("/api/campaigns/sync")
    def campaigns_sync() -> JSONResponse:
        return ok(camp_job.start(cfg, ["--backfill"], module="okxrev.announce"))

    @app.get("/api/campaigns/job")
    def campaigns_job() -> JSONResponse:
        return ok(camp_job.status())

    research_job = tasks.Job("okxrev.regime", "研究")
    RESEARCH = {"regime": "okxrev.regime", "xsec": "okxrev.xsec", "flow": "okxrev.flow", "listings": "okxrev.listings",
                "exit_lab": "okxrev.exit_lab", "margin": "okxrev.margin", "pool": "okxrev.pool",
                "earn": "okxrev.earn"}

    @app.get("/api/research/job")
    def research_job_status() -> JSONResponse:
        return ok(research_job.status())

    @app.get("/api/research/{name}")
    def research_result(name: str) -> JSONResponse:
        if name not in RESEARCH:
            raise HTTPException(404, "unknown study")
        import importlib
        mod = importlib.import_module(RESEARCH[name])
        out: dict[str, Any] = {"result": mod.load(cfg)}
        if name == "regime":
            try:
                out["current"] = mod.current(cfg)
            except Exception:
                out["current"] = {}
        return ok(out)

    @app.post("/api/research/{name}/run")
    def research_run(name: str) -> JSONResponse:
        if name not in RESEARCH:
            raise HTTPException(404, "unknown study")
        return ok(research_job.start(cfg, ["--evaluate"], module=RESEARCH[name]))

    @app.get("/api/market")
    def market_overview(days: int = 7) -> JSONResponse:
        from okxrev import market
        try:
            return ok(market.overview(cfg, days))
        except db.DbBusy:
            raise HTTPException(503, "資料庫忙碌中")

    @app.get("/api/market/positions")
    def market_positions(trader: str, days: int = 7) -> JSONResponse:
        from okxrev import market
        if not re.match(r"^[A-Za-z0-9_\-.]{1,80}$", trader):
            raise HTTPException(400, "bad trader")
        try:
            return ok(market.trader_positions(cfg, trader, max(1, min(days, 60))))
        except FileNotFoundError:
            return ok([])
        except db.DbBusy:
            raise HTTPException(503, "紙上交易資料庫忙碌中")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


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
    if port_in_use(args.port):
        print(f"已經有網頁伺服器在 {url} 執行；不再啟動第二個。要重開請先關閉原本的（或用 --port 換連接埠）。")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    print(f"okx-reversal web UI: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
