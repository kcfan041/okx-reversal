from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from filelock import FileLock, Timeout

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_console_encoding, setup_logging
from okxrev.okx_client import (
    BudgetExceeded,
    InstrumentNotFound,
    OkxClient,
    OkxError,
    RetryExhausted,
)

log = logging.getLogger("okxrev.sync")

BAR_MS = db.BAR_MS
DAY_MS = 86_400_000
FLUSH_ROWS = 30_000
FLUSH_SEC = 20.0


@dataclass
class InstState:
    inst_id: str
    state: str
    list_time: int | None
    oldest_ts: int | None
    newest_ts: int | None
    backfill_done: bool


@dataclass
class Stats:
    requests: int = 0
    rows: int = 0
    forward_insts: int = 0
    backfill_pages: int = 0
    backfill_completed: int = 0
    errors: int = 0
    forward_sec: float = 0.0
    new_insts: list[str] = field(default_factory=list)
    delisted: list[str] = field(default_factory=list)


def layer_targets(cfg: Config, now: int) -> list[int]:
    start = cfg.backfill_start_ms
    days = list(cfg.sync["backfill_layers_days"])
    targets = [max(start, now - int(d) * DAY_MS) for d in days[:-1]]
    targets.append(start)
    return targets


def backfill_pages_per_inst(sync_cfg: dict[str, Any], history_limit: int) -> int:
    bars = int(sync_cfg.get("backfill_bars_per_inst", 2000))
    return max(1, -(-bars // max(1, int(history_limit))))


def is_done_for(st: InstState, target: int) -> bool:
    if st.backfill_done:
        return True
    if st.oldest_ts is None:
        return False
    if st.oldest_ts <= target:
        return True
    return st.list_time is not None and st.oldest_ts <= st.list_time


class Writer:
    def __init__(self, cfg: Config, stats: Stats):
        self.cfg = cfg
        self.stats = stats
        self.pending: list[tuple[str, pd.DataFrame | None, dict[str, Any]]] = []
        self.pending_rows = 0
        self.delist: list[str] = []
        self.last_flush = time.monotonic()

    def add(self, inst_id: str, df: pd.DataFrame | None, **kw: Any) -> None:
        self.pending.append((inst_id, df, kw))
        self.pending_rows += 0 if df is None else len(df)

    def maybe_flush(self) -> None:
        if self.pending_rows >= FLUSH_ROWS or (self.pending and time.monotonic() - self.last_flush >= FLUSH_SEC):
            self.flush()

    def flush(self, run: dict[str, Any] | None = None) -> bool:
        if not self.pending and not self.delist and run is None:
            return True
        try:
            with db.open_db(self.cfg.paths.db, wait_sec=2.0, max_tries=30) as con:
                for inst_id in self.delist:
                    db.mark_delisted(con, inst_id)
                for inst_id, df, kw in self.pending:
                    self.stats.rows += db.write_batch(con, inst_id, df, **kw)
                if run is not None:
                    run["rows_written"] = self.stats.rows
                    db.record_run(con, run)
        except db.DbBusy:
            log.warning("database busy for 60s; keeping %d pending batches for next flush", len(self.pending))
            return False
        self.pending.clear()
        self.delist.clear()
        self.pending_rows = 0
        self.last_flush = time.monotonic()
        return True


class Syncer:
    def __init__(self, cfg: Config, deadline: float, only: list[str] | None = None, smoke: bool = False):
        self.cfg = cfg
        self.bar = cfg.universe["bar"]
        self.btc = cfg.universe["btc_inst"]
        self.only = set(only) if only else None
        self.smoke = smoke
        self.deadline = deadline
        self.stats = Stats()
        self.client = OkxClient(cfg.okx, deadline=deadline)
        self.writer = Writer(cfg, self.stats)
        self.states: dict[str, InstState] = {}
        self._skip: set[str] = set()

    def close(self) -> None:
        self.client.close()

    def _order(self, ids: list[str]) -> list[str]:
        return sorted(ids, key=lambda i: (i != self.btc, i))

    def _selected(self, st: InstState) -> bool:
        return self.only is None or st.inst_id in self.only

    def refresh_instruments(self) -> None:
        u = self.cfg.universe
        insts = self.client.instruments(u["inst_type"])
        exclude = set(u.get("exclude") or [])
        live = [
            {"inst_id": i.inst_id, "uly": i.uly, "ct_val": i.ct_val, "list_time": i.list_time}
            for i in insts
            if i.inst_id.endswith(u["quote_suffix"]) and i.state == "live" and i.inst_id not in exclude
        ]
        if len(live) < 50:
            raise RuntimeError(f"instrument list suspiciously small ({len(live)}); refusing to mark delistings")
        with db.open_db(self.cfg.paths.db) as con:
            new, gone = db.upsert_instruments(con, live)
            ov = db.sync_overview(con)
        self.stats.new_insts, self.stats.delisted = new, gone
        if new:
            log.info("new instruments: %d %s", len(new), new[:10])
        if gone:
            log.info("delisted instruments: %s", gone)
        self._load_states(ov)

    def load_states_from_db(self) -> None:
        with db.open_db(self.cfg.paths.db) as con:
            self._load_states(db.sync_overview(con))

    def _load_states(self, ov: pd.DataFrame) -> None:
        def iv(v: Any) -> int | None:
            return None if pd.isna(v) else int(v)

        self.states = {
            r.inst_id: InstState(
                inst_id=r.inst_id,
                state=r.state,
                list_time=iv(r.list_time),
                oldest_ts=iv(r.oldest_ts),
                newest_ts=iv(r.newest_ts),
                backfill_done=bool(r.backfill_done),
            )
            for r in ov.itertuples(index=False)
        }

    def _record(self, st: InstState, df: pd.DataFrame, **kw: Any) -> None:
        if not df.empty:
            lo, hi = int(df["ts"].min()), int(df["ts"].max())
            st.oldest_ts = lo if st.oldest_ts is None else min(st.oldest_ts, lo)
            st.newest_ts = hi if st.newest_ts is None else max(st.newest_ts, hi)
        self.writer.add(st.inst_id, df, **kw)
        self.writer.maybe_flush()

    def _handle_error(self, st: InstState, e: Exception, phase: str) -> None:
        if isinstance(e, InstrumentNotFound):
            log.info("%s: 51001 during %s -> mark delisted", st.inst_id, phase)
            st.state = "delisted"
            st.backfill_done = True
            self.writer.delist.append(st.inst_id)
            return
        self.stats.errors += 1
        log.warning("%s: %s failed: %s", st.inst_id, phase, e)
        self.writer.add(st.inst_id, None, error=f"{phase}: {e}")

    def forward(self) -> None:
        ids = self._order([i for i, s in self.states.items() if s.state == "live" and self._selected(s)])
        log.info("forward: %d instruments", len(ids))
        for inst_id in ids:
            st = self.states[inst_id]
            try:
                self._forward_one(st)
                self.stats.forward_insts += 1
            except BudgetExceeded:
                raise
            except (OkxError, RetryExhausted) as e:
                self._handle_error(st, e, "forward")

    def _forward_one(self, st: InstState) -> None:
        latest = self.client.candles(st.inst_id, self.bar, limit=300)
        now = db.now_ms()
        if latest.empty:
            self.writer.add(st.inst_id, None, forward_at=now)
            return
        first_latest = int(latest["ts"].min())
        if st.newest_ts is not None and first_latest > st.newest_ts + BAR_MS:
            cursor = st.newest_ts
            log.info("%s: forward gap %s -> %s, filling", st.inst_id, ms_to_str(cursor), ms_to_str(first_latest))
            lim = self.client.history_limit
            while cursor + BAR_MS < first_latest:
                page, _ = self.client.history_candles(
                    st.inst_id, self.bar, limit=lim, before=cursor, after=cursor + (lim + 1) * BAR_MS
                )
                if page.empty:
                    cursor += lim * BAR_MS
                    continue
                page = page[page["ts"] < first_latest]
                if page.empty:
                    break
                self._record(st, page)
                cursor = int(page["ts"].max())
        if st.newest_ts is not None:
            latest = latest[latest["ts"] >= st.newest_ts]
        self._record(st, latest, forward_at=now, clear_error=True)

    def backfill(self, smoke_target: int | None = None) -> None:
        now = db.now_ms()
        targets = [smoke_target] if smoke_target is not None else layer_targets(self.cfg, now)
        pages_per = backfill_pages_per_inst(self.cfg.sync, self.client.history_limit)
        start_ms = self.cfg.backfill_start_ms
        for li, target in enumerate(targets, 1):
            rounds = 0
            while True:
                rounds += 1
                todo = [
                    s for s in self.states.values()
                    if self._selected(s) and s.oldest_ts is not None and not is_done_for(s, target)
                ]
                if not todo:
                    log.info("backfill layer %d/%d (%s) complete", li, len(targets), ms_to_str(target))
                    break
                todo.sort(key=lambda s: (s.inst_id != self.btc, -(s.oldest_ts or 0)))
                log.log(logging.INFO if rounds == 1 else logging.DEBUG,
                        "backfill layer %d/%d (%s): %d instruments pending", li, len(targets), ms_to_str(target), len(todo))
                for st in todo:
                    try:
                        self._backfill_one(st, target, pages_per, start_ms, smoke=smoke_target is not None)
                    except BudgetExceeded:
                        raise
                    except (OkxError, RetryExhausted) as e:
                        self._handle_error(st, e, "backfill")
                        self._skip.add(st.inst_id)
                remaining = [s for s in todo if not is_done_for(s, target)]
                if remaining and all(s.inst_id in self._skip for s in remaining):
                    log.info("backfill layer %d: remaining instruments skipped this run; stopping", li)
                    return

    def _backfill_one(self, st: InstState, target: int, pages: int, start_ms: int, smoke: bool) -> None:
        if st.inst_id in self._skip:
            return
        for _ in range(pages):
            if is_done_for(st, target):
                return
            assert st.oldest_ts is not None
            df, n_raw = self.client.history_candles(st.inst_id, self.bar, after=st.oldest_ts)
            self.stats.backfill_pages += 1
            now = db.now_ms()
            if n_raw == 0:
                if not smoke:
                    st.backfill_done = True
                    self.stats.backfill_completed += 1
                    log.info("%s: backfill reached listing (%s)", st.inst_id, ms_to_str(st.oldest_ts))
                    self._record(st, df, backfill_at=now, backfill_done=True)
                else:
                    self._skip.add(st.inst_id)
                return
            if df.empty:
                log.warning("%s: history page had %d unconfirmed rows only; skipping", st.inst_id, n_raw)
                self._skip.add(st.inst_id)
                return
            done = False
            if not smoke and int(df["ts"].min()) <= start_ms:
                done = True
                st.backfill_done = True
                self.stats.backfill_completed += 1
            self._record(st, df, backfill_at=now, backfill_done=True if done else None)
            if done:
                return


def run_sync(cfg: Config, budget_sec: float, only: list[str] | None, smoke: bool, no_backfill: bool, force: bool = False) -> int:
    started = time.monotonic()
    started_ms = db.now_ms()
    deadline = started + budget_sec
    if pause_flag(cfg).exists() and not force:
        log.info("paused (%s exists); exiting", pause_flag(cfg))
        return 0
    lock = FileLock(str(cfg.paths.lock), timeout=0)
    cfg.paths.lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.acquire()
    except Timeout:
        log.info("another sync is running (lock held); exiting")
        return 0
    syncer = Syncer(cfg, deadline, only=only, smoke=smoke)
    note = "ok"
    try:
        try:
            syncer.refresh_instruments()
            phases = ["forward", "backfill"] if cfg.sync.get("forward_first", True) else ["backfill", "forward"]
            for ph in phases:
                if ph == "forward":
                    t_fw = time.monotonic()
                    try:
                        syncer.forward()
                    finally:
                        syncer.stats.forward_sec = time.monotonic() - t_fw
                elif not no_backfill:
                    smoke_target = db.now_ms() - int(cfg.sync["smoke_days"]) * DAY_MS if smoke else None
                    syncer.backfill(smoke_target)
            if cfg.sync.get("derivs", True) and not smoke and not only:
                syncer.writer.flush()
                from okxrev.derivs import sync_derivs
                sync_derivs(cfg, syncer.client, deadline - 5, max_sec=float(cfg.sync.get("derivs_max_sec", 60)))
                from okxrev.heat import focus_insts
                from okxrev.heatdata import sync_heat
                sync_heat(cfg, syncer.client, deadline - 5, max_sec=float(cfg.sync.get("heat_max_sec", 60)), focus=focus_insts(cfg))
        except BudgetExceeded:
            note = "budget"
            log.info("time budget reached")
        except KeyboardInterrupt:
            note = "interrupted"
            log.warning("interrupted; flushing pending data")
        except Exception as e:
            note = f"error: {type(e).__name__}: {e}"
            syncer.stats.errors += 1
            log.exception("sync failed")
        s = syncer.stats
        s.requests = syncer.client.n_requests
        run = {
            "started_at": started_ms,
            "duration_sec": round(time.monotonic() - started, 1),
            "requests": s.requests,
            "rows_written": 0,
            "forward_insts": s.forward_insts,
            "backfill_pages": s.backfill_pages,
            "errors": s.errors,
            "note": ("smoke " if smoke else "") + note,
        }
        ok = syncer.writer.flush(run=run)
        log.info(
            "run done: %.1fs, %d requests (%d retries, %d rate-limited), %d rows, forward %d in %.0fs, backfill pages %d (+%d done), errors %d, %s%s",
            time.monotonic() - started, s.requests, syncer.client.n_retries, syncer.client.n_limited, s.rows,
            s.forward_insts, s.forward_sec,
            s.backfill_pages, s.backfill_completed, s.errors, note, "" if ok else " [UNFLUSHED]",
        )
        return 1 if note.startswith("error") else 0
    finally:
        syncer.close()
        lock.release()
        if cfg.paper.get("enabled", True) and not smoke and not only:
            spawn_paper(cfg)


def spawn_paper(cfg: Config) -> None:
    """Run one paper-trading tick in a detached low-priority process (never blocks or fails the sync)."""
    from okxrev.web.tasks import background_popen, venv_python
    try:
        background_popen(cfg, [venv_python(cfg, windowless=True), "-m", "okxrev.paper", "--tick", "--quiet"])
    except OSError as e:
        log.warning("could not start paper tick: %s", e)


def gap_summary(con: Any) -> pd.DataFrame:
    return con.execute(
        f"""
        WITH d AS (
            SELECT inst_id, ts - LAG(ts) OVER (PARTITION BY inst_id ORDER BY ts) AS dt FROM candles
        )
        SELECT inst_id, COUNT(*) AS gaps, SUM(dt - {BAR_MS}) / 60000 AS missing_min
        FROM d WHERE dt > {BAR_MS} GROUP BY inst_id ORDER BY missing_min DESC
        """
    ).df()


def pause_flag(cfg: Config):
    return cfg.paths.db.parent / "PAUSE"


def _iv(v: Any) -> int | None:
    return None if v is None or pd.isna(v) else int(v)


def collect_status(cfg: Config, with_gaps: bool = False, top: int = 10) -> dict[str, Any]:
    p = cfg.paths.db
    now = db.now_ms()
    out: dict[str, Any] = {
        "db_path": str(p),
        "exists": p.exists(),
        "now": now,
        "paused": pause_flag(cfg).exists(),
    }
    if not p.exists():
        return out
    out["db_size_mb"] = round(p.stat().st_size / 1024 / 1024, 1)
    with db.open_db(p, read_only=True) as con:
        ov = db.sync_overview(con)
        out["candles"] = int(con.execute("SELECT COUNT(*) FROM candles").fetchone()[0])
        last = con.execute(
            "SELECT started_at, duration_sec, requests, rows_written, forward_insts, backfill_pages, errors, note "
            "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        r24 = con.execute(
            "SELECT COUNT(*), SUM(requests), SUM(rows_written), SUM(errors) FROM sync_runs WHERE started_at >= ?",
            [now - DAY_MS],
        ).fetchone()
        counts = dict(con.execute("SELECT inst_id, COUNT(*) FROM candles GROUP BY inst_id").fetchall())
        gaps = gap_summary(con) if with_gaps else None

    out["n_live"] = int((ov["state"] == "live").sum())
    out["n_delisted"] = int((ov["state"] == "delisted").sum())
    out["n_no_data"] = int(ov["newest_ts"].isna().sum())

    states = [
        InstState(r.inst_id, r.state, _iv(r.list_time), _iv(r.oldest_ts), _iv(r.newest_ts), bool(r.backfill_done))
        for r in ov.itertuples(index=False)
    ]
    targets = layer_targets(cfg, now)
    layers = []
    current = None
    for li, t in enumerate(targets, 1):
        done = sum(is_done_for(s, t) for s in states)
        layers.append({"layer": li, "target": t, "done": done, "total": len(states)})
        if current is None and done < len(states):
            current = li
    out["layers"] = layers
    out["current_layer"] = current

    live = ov[(ov["state"] == "live") & ov["newest_ts"].notna()]
    if not live.empty:
        lag = (now - (live["newest_ts"].astype("int64") + BAR_MS)) / 60000
        btc = live[live["inst_id"] == cfg.universe["btc_inst"]]
        out["lag_min"] = {
            "max": float(lag.max()),
            "median": float(lag.median()),
            "btc": float((now - (int(btc["newest_ts"].iloc[0]) + BAR_MS)) / 60000) if not btc.empty else None,
        }
    if last is not None:
        out["last_run"] = dict(zip(
            ["started_at", "duration_sec", "requests", "rows_written", "forward_insts", "backfill_pages", "errors", "note"], last
        ))
    out["last_24h"] = {
        "runs": int(r24[0] or 0), "requests": int(r24[1] or 0), "rows": int(r24[2] or 0), "errors": int(r24[3] or 0),
    }
    errs = ov[ov["last_error"].notna()]
    out["errors"] = [{"inst_id": r.inst_id, "error": str(r.last_error)} for r in errs.itertuples(index=False)]

    cov = ov[ov["oldest_ts"].notna()].sort_values("oldest_ts", ascending=False).head(top)
    out["least_covered"] = [
        {"inst_id": r.inst_id, "oldest_ts": _iv(r.oldest_ts), "newest_ts": _iv(r.newest_ts),
         "bars": int(counts.get(r.inst_id, 0)), "done": bool(r.backfill_done)}
        for r in cov.itertuples(index=False)
    ]
    if gaps is not None:
        out["gaps"] = {
            "n_gaps": int(gaps["gaps"].sum()) if len(gaps) else 0,
            "n_insts": int(len(gaps)),
            "missing_hours": float(gaps["missing_min"].sum()) / 60 if len(gaps) else 0.0,
            "top": [{"inst_id": r.inst_id, "gaps": int(r.gaps), "missing_hours": float(r.missing_min) / 60}
                    for r in gaps.head(20).itertuples(index=False)],
        }
    return out


def print_status(cfg: Config, top: int = 10) -> int:
    s = collect_status(cfg, with_gaps=True, top=top)
    if not s["exists"]:
        print(f"DB: {s['db_path']} (not created yet)")
        return 0
    print(f"DB: {s['db_path']}  ({s['db_size_mb']:,.1f} MB)")
    if s["paused"]:
        print("PAUSED: data/PAUSE exists; scheduled runs exit immediately")
    print(f"Instruments: {s['n_live']} live, {s['n_delisted']} delisted")
    print(f"Candles: {s['candles']:,} rows")
    cur = s["current_layer"]
    if cur is None:
        print(f"Backfill: all {len(s['layers'])} layers complete")
    else:
        L = s["layers"][cur - 1]
        print(f"Backfill: layer {cur}/{len(s['layers'])} (to {ms_to_str(L['target'], '%Y-%m-%d')}), {L['done']}/{L['total']} done")
    if "lag_min" in s:
        lg = s["lag_min"]
        btc = f"{lg['btc']:.0f} min" if lg["btc"] is not None else "-"
        print(f"Latest bar lag: max {lg['max']:.0f} min, median {lg['median']:.0f} min ({cfg.universe['btc_inst']} {btc})")
    if s["n_no_data"]:
        print(f"Instruments without data yet: {s['n_no_data']}")
    if "last_run" in s:
        r = s["last_run"]
        print(f"Last run: {ms_to_str(r['started_at'])} UTC, {r['duration_sec']:.0f} s, {r['requests']:,} requests, "
              f"{r['rows_written']:,} rows, {r['errors']} errors ({r['note']})")
    d = s["last_24h"]
    if d["runs"]:
        print(f"Last 24h: {d['runs']} runs, {d['requests']:,} requests, {d['rows']:,} rows, {d['errors']} errors")
    if s["errors"]:
        print(f"Instruments with last_error: {len(s['errors'])}")
        for e in s["errors"][:5]:
            print(f"  {e['inst_id']}: {e['error'][:100]}")
    print()
    print(f"Oldest coverage (top {top} least covered):")
    if s["least_covered"]:
        print(f"{'inst_id':<22}{'oldest':<18}{'newest':<18}{'bars':>9}  done")
        for r in s["least_covered"]:
            print(f"{r['inst_id']:<22}{ms_to_str(r['oldest_ts']):<18}{ms_to_str(r['newest_ts']):<18}{r['bars']:>9,}  {'yes' if r['done'] else 'no'}")
    print()
    g = s["gaps"]
    if not g["n_insts"]:
        print("Data gaps: none")
    else:
        print(f"Data gaps: {g['n_gaps']:,} gaps in {g['n_insts']} instruments, {g['missing_hours']:,.1f} h missing in total")
        for r in g["top"][:5]:
            print(f"  {r['inst_id']:<22}{r['gaps']:>6} gaps {r['missing_hours']:>9.1f} h")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.sync", description="Sync OKX USDT-SWAP 5m candles into DuckDB")
    ap.add_argument("--config", default=None)
    ap.add_argument("--status", action="store_true", help="show coverage status (no network)")
    ap.add_argument("--smoke", action="store_true", help="small validation run on smoke_insts")
    ap.add_argument("--max-minutes", type=float, default=None, help="override run_budget_sec")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--only", nargs="+", default=None)
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even if data/PAUSE exists")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.status:
        setup_console_encoding()
        try:
            return print_status(cfg)
        except db.DbBusy:
            print("database is busy (sync is writing); try again shortly")
            return 1

    setup_logging(cfg.paths.logs, "sync", level=logging.DEBUG if args.verbose else logging.INFO)
    budget = float(cfg.sync["run_budget_sec"])
    if args.max_minutes is not None:
        budget = args.max_minutes * 60
    only = args.only
    if args.smoke:
        only = list(cfg.sync["smoke_insts"])
        if args.max_minutes is None:
            budget = 120
    log.info("sync start (budget %.0fs%s%s)", budget, ", smoke" if args.smoke else "", f", only {only}" if only else "")
    return run_sync(cfg, budget, only=only, smoke=args.smoke, no_backfill=args.no_backfill, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
