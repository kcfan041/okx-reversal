"""Blind test: replay the current (frozen) traders over an older period none of them has seen.

backfill  Downloads 5m candles for instruments that were already listed, from `start` to `end`, into a separate
          database (data/okx_old.duckdb) so the live sync database is never touched. Resumable.
replay    Runs the same Engine as live paper trading, stepping hourly through the period. Signals are generated
          per trader with the same code paths as live ticks (rule masks with the same freshness gap, deterministic
          random draws, model thresholds), so live and replay agree bar for bar. Results go to data/replay.duckdb.

Bias notes: instruments delisted before today are unavailable (survivorship); funding history does not exist that
far back, so a constant average rate is charged and flagged; funding/OI-based conditions never fire.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import duckdb
import numpy as np
import pandas as pd
from filelock import FileLock, Timeout

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str, parse_utc_ms
from okxrev.log import setup_logging
from okxrev.okx_client import BudgetExceeded, InstrumentNotFound, OkxClient, OkxError, RetryExhausted

log = logging.getLogger("okxrev.replay")

BAR = db.BAR_MS
DAY = 86_400_000
STEP_BARS = 12
DEFAULTS = {"start": "2024-10-01", "end": "2025-09-15", "db": "data/okx_old.duckdb", "out": "data/replay.duckdb",
            "warmup_days": 12, "requests_per_sec": 4.0}


def rcfg(cfg: Config) -> dict[str, Any]:
    return {**DEFAULTS, **(cfg.raw.get("replay") or {})}


def old_db_path(cfg: Config) -> Path:
    p = Path(rcfg(cfg)["db"])
    return p if p.is_absolute() else (cfg.paths.root / p).resolve()


def out_db_path(cfg: Config) -> Path:
    p = Path(rcfg(cfg)["out"])
    return p if p.is_absolute() else (cfg.paths.root / p).resolve()


def old_cfg(cfg: Config) -> Config:
    """Config whose candle database is the old-period one (feature code reads cfg.paths.db)."""
    c = Config(raw=cfg.raw, paths=replace(cfg.paths, db=old_db_path(cfg)), source=cfg.source)
    return c


# ---------------------------------------------------------------- backfill
def targets(cfg: Config, start: int, end: int) -> list[str]:
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        df = con.execute("SELECT inst_id, list_time FROM instruments WHERE state = 'live'").df()
    btc = cfg.universe["btc_inst"]
    lt = df["list_time"]
    ids = df.loc[lt.isna() | (lt < end - 7 * DAY), "inst_id"].tolist()
    return sorted(set(ids) | {btc}, key=lambda i: (i != btc, i))


def backfill(cfg: Config, max_minutes: float | None = None, insts: list[str] | None = None,
             progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    rc = rcfg(cfg)
    start, end = parse_utc_ms(rc["start"]) - int(rc["warmup_days"]) * DAY, parse_utc_ms(rc["end"]) + 4 * DAY
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    okx = dict(cfg.okx)
    okx["history_requests_per_sec"] = min(float(okx.get("history_requests_per_sec", 8)), float(rc["requests_per_sec"]))
    client = OkxClient(okx, deadline=deadline)
    ids = insts or targets(cfg, start, end)
    path = old_db_path(cfg)
    n_rows = n_req = 0
    done_insts = 0
    try:
        with db.open_db(path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS old_state (inst_id VARCHAR PRIMARY KEY, done BOOLEAN, oldest BIGINT, note VARCHAR)")
            db.upsert_instruments(con, [{"inst_id": i} for i in ids])
            st = dict((r[0], (r[1], r[2])) for r in con.execute("SELECT inst_id, done, oldest FROM old_state").fetchall())
        for k, inst in enumerate(ids, 1):
            done, oldest = st.get(inst, (False, None))
            if done:
                done_insts += 1
                continue
            after = int(oldest) if oldest else end
            note = "ok"
            batch: list[pd.DataFrame] = []
            try:
                while after > start:
                    df, n_raw = client.history_candles(inst, "5m", after=after)
                    n_req += 1
                    if n_raw == 0 or df.empty:
                        note = "no more data"
                        break
                    batch.append(df[df["ts"] >= start])
                    after = int(df["ts"].min())
                    if sum(len(b) for b in batch) >= 20_000:
                        n_rows += _flush(path, inst, batch, after, False)
                        batch = []
                complete = True
            except InstrumentNotFound:
                note, complete = "not found", True
            except (BudgetExceeded, RetryExhausted, OkxError) as e:
                note, complete = f"stopped: {type(e).__name__}", False
            n_rows += _flush(path, inst, batch, after, complete, note)
            if complete:
                done_insts += 1
            if progress:
                progress(k, len(ids), inst)
            if not complete:
                break
    finally:
        client.close()
    return {"insts": len(ids), "done": done_insts, "rows": n_rows, "requests": n_req,
            "range": [ms_to_str(start), ms_to_str(end)], "complete": done_insts >= len(ids)}


def _flush(path: Path, inst: str, batch: list[pd.DataFrame], oldest: int, done: bool, note: str = "ok") -> int:
    rows = pd.concat(batch, ignore_index=True) if batch else None
    with db.open_db(path, wait_sec=2.0, max_tries=30) as con:
        n = db.write_batch(con, inst, rows)
        con.execute("INSERT INTO old_state VALUES (?, ?, ?, ?) ON CONFLICT (inst_id) DO UPDATE SET done = excluded.done, "
                    "oldest = excluded.oldest, note = excluded.note", [inst, done, oldest, note])
    return n


def backfill_status(cfg: Config) -> dict[str, Any]:
    p = old_db_path(cfg)
    if not p.exists():
        return {"exists": False}
    try:
        with db.open_db(p, read_only=True, wait_sec=1.0, max_tries=5) as con:
            n = con.execute("SELECT COUNT(*), SUM(done::INT) FROM old_state").fetchone()
            rows = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            rng = con.execute("SELECT MIN(ts), MAX(ts) FROM candles").fetchone()
            insts = con.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    except db.DbBusy:
        return {"exists": True, "busy": True}
    return {"exists": True, "insts": int(insts), "started": int(n[0] or 0), "done": int(n[1] or 0), "rows": int(rows),
            "range": [ms_to_str(rng[0]), ms_to_str(rng[1])] if rng[0] else None, "size_mb": round(p.stat().st_size / 1e6)}


# ---------------------------------------------------------------- replay
class LazyCut(dict):
    """Mapping inst -> candles truncated at `upto`, sliced only when an instrument is actually accessed."""

    def __init__(self, full: dict[str, pd.DataFrame], upto: int):
        super().__init__()
        self.full, self.upto = full, upto

    def get(self, k: str, default: Any = None) -> Any:
        if k not in self.full:
            return default
        if not super().__contains__(k):
            super().__setitem__(k, self.full[k].loc[: self.upto])
        return super().__getitem__(k)

    def __getitem__(self, k: str) -> pd.DataFrame:
        v = self.get(k)
        if v is None:
            raise KeyError(k)
        return v

    def __contains__(self, k: object) -> bool:
        return k in self.full

    def items(self):  # type: ignore[override]
        return ((k, self[k]) for k in self.full)

    def values(self):  # type: ignore[override]
        return (self[k] for k in self.full)


def avg_funding(cfg: Config) -> float:
    try:
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
            r = con.execute("SELECT AVG(rate) FROM funding WHERE rate IS NOT NULL").fetchone()[0]
        return float(r) if r is not None else 0.0001
    except Exception:
        return 0.0001


def synthetic_funding(ts_lo: int, ts_hi: int, rate: float) -> pd.DataFrame:
    first = ((ts_lo // (8 * 3_600_000)) + 1) * 8 * 3_600_000
    t = np.arange(first, ts_hi + 1, 8 * 3_600_000, dtype=np.int64)
    return pd.DataFrame({"ts": t, "rate": rate})


def load_all(cfg: Config, start: int, end: int) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    from okxrev.derivs import attach
    from okxrev.detector import btc_features, prepare
    from okxrev.discover import features, market_breadth
    oc = old_cfg(cfg)
    det, reg = cfg.detector, cfg.btc_regime
    warm = int(rcfg(cfg)["warmup_days"]) * DAY
    with db.open_db(oc.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
        insts = db.list_candle_insts(con)
        btc = btc_features(db.load_candles(con, cfg.universe["btc_inst"], start - warm, end + 4 * DAY), reg)
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        meta = db.sync_overview(con).set_index("inst_id")
    breadth = market_breadth(oc, insts)
    candles, feats = {}, {}
    empty_oi = pd.DataFrame(columns=["period", "ts", "oi_usd"])
    empty_f = pd.DataFrame(columns=["ts", "rate"])
    for n, inst in enumerate(insts, 1):
        with db.open_db(oc.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
            raw = db.load_candles(con, inst, start - warm, end + 4 * DAY)
        if len(raw) < 900:
            continue
        lt = meta.at[inst, "list_time"] if inst in meta.index else None
        lt = None if lt is None or pd.isna(lt) else int(lt)
        g = prepare(raw, det, btc, list_time=lt)
        f = features(g, det, breadth, lt, attach(g, empty_f, empty_oi))
        f["breadth"] = pd.Series(f["breadth"].to_numpy(), index=g.index).ffill(limit=12).to_numpy()
        candles[inst] = g[["open", "high", "low", "close"]]
        feats[inst] = f
        if n % 50 == 0:
            log.info("replay load %d/%d", n, len(insts))
    return candles, feats


def connect_out(cfg: Config, fresh: bool) -> duckdb.DuckDBPyConnection:
    from okxrev.paper import SCHEMA
    p = out_db_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fresh and p.exists():
        p.unlink()
    con = duckdb.connect(str(p))
    con.execute(SCHEMA)
    con.execute("CREATE TABLE IF NOT EXISTS replay_meta (k VARCHAR PRIMARY KEY, v VARCHAR)")
    return con


def replay_traders(cfg: Config) -> list[Any]:
    from okxrev import traders as T
    trs = [t for t in T.build(cfg) if t.kind not in ("llm", "campaign")]
    if any(t.kind == "fade" for t in trs):
        from okxrev import fade
        op = old_db_path(cfg)
        fade.FadeTrader.old_source = staticmethod(lambda c, a, b: fade.load_old_closes(c, a, b, op))
    heat_trs = [t for t in trs if t.kind == "heat"]
    if heat_trs:
        from okxrev import heat
        rc = rcfg(cfg)
        start, end = parse_utc_ms(rc["start"]), parse_utc_ms(rc["end"])
        log.info("replay: computing hourly heat scores for the old period")
        hist = heat.score_history(cfg, old_db_path(cfg), start, end + 2 * DAY)
        for t in heat_trs:
            t.use_scores(hist)
    return trs


SIG_COLS = ["trader_id", "kind", "label", "params", "inst_id", "bar_ts", "side", "exit_rule", "score", "reason", "model_ver", "features"]


from okxrev.paper import Trader as _Trader


class Replayed(_Trader):
    """Serves signals from the precomputed table to the Engine (the Engine only needs trader_id/kind/label/params)."""

    def __init__(self, trader_id: str, kind: str, label: str, params: str, sigs: pd.DataFrame):
        from okxrev.paper import Signal
        self.trader_id, self.kind, self.label, self._params = trader_id, kind, label, params
        self.by_inst: dict[str, dict[int, list[Any]]] = {}
        for r in sigs.itertuples(index=False):
            s = Signal(trader_id, r.inst_id, int(r.bar_ts), r.side, r.exit_rule, float(r.score), r.reason, r.model_ver or "",
                       json.loads(r.features) if r.features else {})
            self.by_inst.setdefault(r.inst_id, {}).setdefault(int(r.bar_ts), []).append(s)

    def params(self) -> dict[str, Any]:
        return json.loads(self._params or "{}")

    def prepare(self, ctx: Any) -> None:
        pass

    def signals(self, ctx: Any, inst: str, f: Any, bars: np.ndarray) -> list[Any]:
        d = self.by_inst.get(inst)
        if not d:
            return []
        out = []
        for b in bars:
            out += d.get(int(b), [])
        return out

    def review(self, ctx: Any, sigs: list[Any]) -> list[Any]:
        return sigs


def precompute(cfg: Config, trs: list[Any], candles: dict[str, pd.DataFrame], feats: dict[str, pd.DataFrame],
               start: int, end: int) -> pd.DataFrame:
    """Phase 1: every trader's signals for the whole period, with the feature snapshot the live engine would store."""
    from okxrev.paper import Context
    from okxrev.traders import Precomputed
    ctx0 = Context(cfg, start, start, {}, feats, candles, {}, None)  # type: ignore[arg-type]
    rows = []
    built: dict[str, Any] = {}
    from okxrev.traders import StopVariant
    for tr in sorted(trs, key=lambda t: (t.kind == "follow", isinstance(t, StopVariant))):
        if tr.kind == "follow":
            continue
        t1 = time.monotonic()
        if isinstance(tr, StopVariant) and tr.inner.trader_id in built:
            p = Precomputed(tr)
            base = built[tr.inner.trader_id]
            n = 0
            for inst, d in base.by_inst.items():
                for ts, sigs in d.items():
                    p.by_inst.setdefault(inst, {})[ts] = tr._conv(sigs)
                    n += len(sigs)
        else:
            p = Precomputed(tr)
            n = p.build(ctx0, start, end)
        built[tr.trader_id] = p
        log.info("precomputed %-28s %6d signals in %.1fs", tr.trader_id, n, time.monotonic() - t1)
        prm = json.dumps(tr.params(), ensure_ascii=False, default=float)
        for inst, d in p.by_inst.items():
            for ts, sigs in d.items():
                for s in sigs:
                    feats_ = s.features or ctx0.snapshot(inst, ts)
                    rows.append((tr.trader_id, tr.kind, tr.label, prm, inst, int(ts), s.side, s.exit_rule, float(s.score), s.reason,
                                 s.model_ver, json.dumps(feats_, ensure_ascii=False, default=float)))
    return pd.DataFrame(rows, columns=SIG_COLS)


def simulate(cfg: Config, sig: pd.DataFrame, candles: dict[str, pd.DataFrame], start: int, end: int, rate: float,
             out_path: Path | None = None, tag: str = "", turnover: dict[str, float] | None = None) -> duckdb.DuckDBPyConnection:
    """Phase 2: run the paper Engine for the traders in `sig` on an in-memory database; optionally persist it."""
    from okxrev.paper import SCHEMA, Context, Engine, snapshot_equity
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    trs = [Replayed(t, g["kind"].iloc[0], g["label"].iloc[0], g["params"].iloc[0], g) for t, g in sig.groupby("trader_id", sort=True)]
    fund = {i: synthetic_funding(start, end + 4 * DAY, rate) for i in candles}
    to = turnover or {}
    fmin = {i: pd.DataFrame({"turnover24": [to.get(i, np.nan)]}) for i in set(sig["inst_id"]) | set(to)}
    eng = Engine(cfg, trs, con)
    by_hour: dict[int, dict[str, set[int]]] = {}
    for t in trs:
        for inst, d in t.by_inst.items():
            for ts in d:
                k_ = int(-(-(ts - start) // (STEP_BARS * BAR)))
                by_hour.setdefault(k_, {}).setdefault(inst, set()).add(int(ts))
    grid = np.arange(start, end + 1, STEP_BARS * BAR, dtype=np.int64)
    t_loop = time.monotonic()
    last_log = -1
    n_sig = 0
    for k, step_end in enumerate(grid):
        has_open = con.execute("SELECT 1 FROM positions WHERE state IN ('open', 'pending') LIMIT 1").fetchone()
        if k not in by_hour and not has_open:
            continue
        new = {i: np.array(sorted(v), dtype=np.int64) for i, v in by_hour.get(k, {}).items()}
        ctx = Context(cfg, int(step_end), int(step_end), new, fmin, LazyCut(candles, int(step_end)), fund, con)
        n_sig += eng.tick(ctx)["signals"]
        day = k // 24
        if day != last_log and day % 7 == 0:
            last_log = day
            held = {p_ for (p_,) in con.execute("SELECT DISTINCT inst_id FROM positions WHERE state = 'open'").fetchall()}
            snapshot_equity(con, eng, {i: candles[i].loc[:step_end] for i in held if i in candles},
                            int(step_end) - int(step_end) % (BAR * int(cfg.paper.get("snapshot_every_bars", 3))))
            el = time.monotonic() - t_loop
            log.info("%sreplay %s (%d/%d hours), %d decisions, %.0fs elapsed, ~%.0f min left", tag,
                     ms_to_str(int(step_end), "%Y-%m-%d"), k + 1, len(grid), n_sig, el, el / max(k + 1, 1) * (len(grid) - k - 1) / 60)
    eng.check_open(candles, fund, end + 4 * DAY)
    if out_path is not None:
        if out_path.exists():
            out_path.unlink()
        con.execute(f"ATTACH '{out_path.as_posix()}' AS outdb")
        con.execute("USE outdb")
        con.execute(SCHEMA)
        for t in ("traders", "decisions", "positions", "books", "equity_hist"):
            con.execute(f"INSERT INTO outdb.{t} SELECT * FROM memory.{t}")
        con.execute("USE memory")
        con.execute("DETACH outdb")
    return con


def _worker(args: tuple[Any, ...]) -> str:
    """Subprocess entry: load only price columns for the instruments this group trades, simulate, persist."""
    cfg_path, sig_path, out_path, start, end, rate, tag, notional = args
    cfg = load_config(cfg_path).copy_with({"paper": {"resolved_notional": notional}})
    setup_logging(cfg.paths.logs, "replay", console=False)
    sig = pd.read_parquet(sig_path)
    insts = sorted(sig["inst_id"].unique())
    oc = old_cfg(cfg)
    candles = {}
    from okxrev.detector import to_grid
    with db.open_db(oc.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        for inst in insts:
            raw = db.load_candles(con, inst, start - DAY, end + 5 * DAY)
            if len(raw):
                candles[inst] = to_grid(raw)[["open", "high", "low", "close"]]
    to = pd.read_parquet(Path(sig_path).with_name("turnover.parquet"))
    turnover = dict(zip(to["inst_id"], to["turnover24"]))
    t0 = time.monotonic()
    con = simulate(cfg, sig, candles, start, end, rate, Path(out_path), tag, turnover)
    con.close()
    log.info("%sdone in %.0fs", tag, time.monotonic() - t0)
    return out_path


def _hold_bars() -> dict[str, int]:
    from okxrev.paper import EXIT_RULES
    return {k: int(v[0] or 12) for k, v in EXIT_RULES.items()}


HOLD_BARS = _hold_bars()


def split_groups(sig: pd.DataFrame, n: int) -> list[list[str]]:
    """Greedy balance traders across n groups by expected work: signals x holding bars (open positions are what
    each hourly step has to walk), not by signal count alone."""
    w = sig["exit_rule"].map(HOLD_BARS).fillna(72)
    cnt = w.groupby(sig["trader_id"]).sum().sort_values(ascending=False)
    groups: list[list[str]] = [[] for _ in range(max(1, n))]
    load = [0] * len(groups)
    for t, c in cnt.items():
        i = int(np.argmin(load))
        groups[i].append(str(t))
        load[i] += int(c)
    return [g for g in groups if g]


def follow_replay(sig: pd.DataFrame, closed: pd.DataFrame, start: int, end: int, sets: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    from okxrev.follow import replay_signals
    return replay_signals(sig, closed, start, end, sets)


def _closed_from(parts: list[Path], parts_con: list[Any]) -> pd.DataFrame:
    q = "SELECT trader_id, side, exit_ts, ret_net FROM {} WHERE book = 'C' AND state = 'closed'"
    frames = [pc.execute(q.format("positions")).df() for pc in parts_con]
    for p in parts:
        c = duckdb.connect(str(p), read_only=True)
        try:
            frames.append(c.execute(q.format("positions")).df())
        finally:
            c.close()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["trader_id", "side", "exit_ts", "ret_net"])


def _simulate_parts(cfg: Config, sig: pd.DataFrame, candles: dict[str, pd.DataFrame], start: int, end: int, rate: float,
                    turnover: dict[str, float], workers: int, work: Path, tag: str) -> tuple[list[Path], list[Any]]:
    """Run the engine on `sig`, split across worker processes by trader. Returns (part db files, in-memory cons)."""
    groups = split_groups(sig, workers) if len(sig) else []
    if workers <= 1 or len(groups) <= 1:
        return [], [simulate(cfg, sig, candles, start, end, rate, out_path=None, turnover=turnover)] if len(sig) else []
    to_path = work / "turnover.parquet"
    if not to_path.exists():
        pd.DataFrame({"inst_id": list(turnover), "turnover24": list(turnover.values())}).to_parquet(to_path, index=False)
    sig_paths = []
    for i, g in enumerate(groups):
        sp = work / f"sig_{tag}_{i}.parquet"
        sig[sig["trader_id"].isin(g)].to_parquet(sp, index=False)
        sig_paths.append((str(cfg.source) if cfg.source else None, str(sp), str(work / f"part_{tag}_{i}.duckdb"), start, end, rate,
                          f"[{tag}w{i}] ", cfg.paper["resolved_notional"]))
    import gc
    gc.collect()
    from concurrent.futures import ProcessPoolExecutor
    import os
    from okxrev.web.tasks import THREAD_VARS
    for k in THREAD_VARS:
        os.environ.setdefault(k, "1")
    log.info("simulate %s: %d workers, groups %s", tag, len(groups), [len(g) for g in groups])
    with ProcessPoolExecutor(max_workers=len(groups)) as ex:
        return [Path(p) for p in ex.map(_worker, sig_paths)], []


def run(cfg: Config, traders_: list[Any] | None = None, progress: Callable[[int, int], None] | None = None,
        candles: dict[str, pd.DataFrame] | None = None, feats: dict[str, pd.DataFrame] | None = None,
        workers: int | None = None) -> dict[str, Any]:
    rc = rcfg(cfg)
    start, end = parse_utc_ms(rc["start"]), parse_utc_ms(rc["end"])
    workers = int(rc.get("workers", 4) if workers is None else workers)
    t0 = time.monotonic()
    if candles is None or feats is None:
        candles, feats = load_all(cfg, start, end)
    if not candles:
        return {"note": "no old data; run backfill first"}
    rate = avg_funding(cfg)
    from okxrev.paper import your_median_notional
    cfg = cfg.copy_with({"paper": {"resolved_notional": your_median_notional(cfg) or 2000.0}})
    trs = traders_ if traders_ is not None else replay_traders(cfg)
    follow_sets = [t.fs for t in trs if t.kind == "follow"]
    sig = precompute(cfg, trs, candles, feats, start, end)
    turnover = {i: float(np.nanmedian(f["turnover24"].to_numpy())) for i, f in feats.items() if "turnover24" in f}
    log.info("phase 1 done: %d signals from %d traders in %.0fs", len(sig), len(trs), time.monotonic() - t0)
    out = out_db_path(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / "replay_work"
    work.mkdir(exist_ok=True)
    for p in work.glob("*"):
        p.unlink()
    del feats
    parts, parts_con = _simulate_parts(cfg, sig, candles, start, end, rate, turnover, workers, work, "p1")
    picks_log = pd.DataFrame()
    if follow_sets:
        closed = _closed_from(parts, parts_con)
        fsig, picks_log = follow_replay(sig, closed, start, end, follow_sets)
        log.info("follow traders: %d copied signals from %d sets", len(fsig), len(follow_sets))
        if len(fsig):
            p2, c2 = _simulate_parts(cfg, fsig, candles, start, end, rate, turnover, workers, work, "p2")
            parts += p2
            parts_con += c2
            sig = pd.concat([sig, fsig], ignore_index=True)
    groups = split_groups(sig, workers) if len(sig) else []
    if out.exists():
        out.unlink()
    con = duckdb.connect(str(out))
    from okxrev.paper import SCHEMA
    con.execute(SCHEMA)
    con.execute("CREATE TABLE IF NOT EXISTS replay_meta (k VARCHAR PRIMARY KEY, v VARCHAR)")
    try:
        tables = ("traders", "decisions", "positions", "books", "equity_hist")
        for pc in parts_con:
            for t in tables:
                df = pc.execute(f"SELECT * FROM {t}").df()
                con.register("_df", df)
                con.execute(f"INSERT INTO {t} SELECT * FROM _df")
                con.unregister("_df")
            pc.close()
        for p in parts:
            con.execute(f"ATTACH '{p.as_posix()}' AS part (READ_ONLY)")
            for t in tables:
                con.execute(f"INSERT INTO {t} SELECT * FROM part.{t}")
            con.execute("DETACH part")
        if len(picks_log):
            con.register("_pk", picks_log.assign(created=db.now_ms()))
            con.execute("INSERT OR REPLACE INTO follow_picks SELECT trader_id, period_ts, picks, created FROM _pk")
            con.unregister("_pk")
        n_dec = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        meta = {"start": rc["start"], "end": rc["end"], "n_insts": int(sig["inst_id"].nunique()) if len(sig) else 0,
                "funding_rate_assumed": rate, "generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1),
                "signals": int(n_dec), "traders": sorted(sig["trader_id"].unique().tolist()) if len(sig) else [], "workers": len(groups) or 1}
        for k_, v in meta.items():
            con.execute("INSERT OR REPLACE INTO replay_meta VALUES (?, ?)", [k_, json.dumps(v, ensure_ascii=False)])
    finally:
        con.close()
    for p in work.glob("*"):
        p.unlink()
    return meta


# ---------------------------------------------------------------- reporting
def compare(cfg: Config) -> dict[str, Any]:
    """Leaderboard for the replay plus, for each rule trader, its discovery-time scores side by side."""
    from okxrev import discover
    from okxrev.paper import leaderboard
    p = out_db_path(cfg)
    if not p.exists():
        return {"exists": False}
    con = duckdb.connect(str(p), read_only=True)
    try:
        meta = {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM replay_meta").fetchall()}
        lb = leaderboard(con)
        from okxrev.paper import market_context
        market = market_context(con, old_db_path(cfg) if old_db_path(cfg).exists() else None)
        pos = con.execute("SELECT trader_id, entry_ts, ret_net FROM positions WHERE state = 'closed' AND book = 'C'").df()
    finally:
        con.close()
    by_month = {}
    if len(pos):
        pos["m"] = pd.to_datetime(pos["entry_ts"], unit="ms").dt.strftime("%Y-%m")
        for t, g in pos.groupby("trader_id"):
            by_month[t] = g.groupby("m")["ret_net"].agg(["mean", "count"]).reset_index().rename(columns={"count": "n"}).to_dict("records")
    disc = {r["key"] + "|" + r["exit"]: r for r in (discover.load(cfg) or {}).get("results", [])}
    rows = []
    for r in lb:
        prm = r.get("params") or {}
        d = disc.get(f"{prm.get('key')}|{prm.get('exit')}") if r["kind"] == "rule" else None
        rows.append({**r, "by_month": by_month.get(r["trader_id"], []),
                     "discovery": {"valid": d["valid"].get("day_mean"), "holdout": d["holdout"].get("day_mean"), "verdict": d["verdict"]} if d else None})
    return {"exists": True, "meta": meta, "leaderboard": rows, "market": market,
            "caveats": ["只含目前仍在交易、且在測試期間已上市的合約；期間下架的幣無法取得（存活者偏差，偏樂觀）",
                        f"該期間沒有資金費率歷史，持倉以平均費率 {meta.get('funding_rate_assumed', 0):.5%} 每 8 小時估計扣除",
                        "資金費率／OI 條件在舊資料中為空值，相關條件不會觸發",
                        "LLM 審核員不參與盲測（可能記得當時的真實行情）"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.replay")
    ap.add_argument("--config", default=None)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "replay")
    lock = FileLock(str(old_db_path(cfg)) + ".job.lock", timeout=0)
    old_db_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    if args.backfill or args.run:
        try:
            lock.acquire()
        except Timeout:
            print("another replay job is running")
            return 0
    try:
        if args.backfill:
            def prog(k: int, n: int, inst: str) -> None:
                if k % 10 == 0 or k == n:
                    log.info("backfill %d/%d (%s)", k, n, inst)
            r = backfill(cfg, args.max_minutes, progress=prog)
            print(r)
            if r["complete"] and not args.run:
                args.run = True
        if args.run:
            def prog2(k: int, n: int) -> None:
                log.info("replay %d/%d steps", k, n)
            print(run(cfg, progress=prog2))
    finally:
        if lock.is_locked:
            lock.release()
    if args.status or not (args.backfill or args.run):
        print(backfill_status(cfg))
        c = compare(cfg)
        if c.get("exists"):
            print(c["meta"])
            for r in c["leaderboard"]:
                x = r["C"]
                print(f"  {r['trader_id']:<28} n={x.get('n', 0):>5} days={x.get('n_days', 0):>4} 日均 {(x.get('day_mean') or 0) * 100:+6.2f}%  "
                      f"vs 隨機 {r.get('vs_random', {}).get('t') if r.get('vs_random') else '-'}  探索 {r.get('discovery')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
