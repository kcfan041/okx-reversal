"""Extra market data for the overheat score.

taker_volume   hourly taker buy/sell volume (rubik), history to at least 2024-10
mark_index     hourly mark-price and index-price closes -> perpetual premium, history to at least 2024-10
ls_ratio       5-minute account long/short ratio and top-trader ratio (OKX keeps ~1 day; accumulated forward)
liquidations   filled liquidation orders (only the latest ~100 per family; accumulated forward, de-duplicated)

`sync_heat` is called from every scheduled sync (time-capped). `backfill` fills history into the live database
(from 2025-09-15) and into the old-period database (2024-09 .. 2025-09) used by the blind test. Both are resumable.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str, parse_utc_ms
from okxrev.log import setup_logging
from okxrev.okx_client import BudgetExceeded, InstrumentNotFound, OkxClient, OkxError, RetryExhausted

log = logging.getLogger("okxrev.heatdata")

HOUR = 3_600_000
TAKER_PATH = "/api/v5/rubik/stat/taker-volume-contract"
MARK_PATH = "/api/v5/market/history-mark-price-candles"
INDEX_PATH = "/api/v5/market/history-index-candles"
LS_PATH = "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract"
TOP_PATH = "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader"
LIQ_PATH = "/api/v5/public/liquidation-orders"

SCHEMA = """
CREATE TABLE IF NOT EXISTS taker_volume (
    inst_id VARCHAR NOT NULL, ts BIGINT NOT NULL, sell DOUBLE, buy DOUBLE, PRIMARY KEY (inst_id, ts)
);
CREATE TABLE IF NOT EXISTS mark_index (
    inst_id VARCHAR NOT NULL, ts BIGINT NOT NULL, mark DOUBLE, idx DOUBLE, PRIMARY KEY (inst_id, ts)
);
CREATE TABLE IF NOT EXISTS ls_ratio (
    inst_id VARCHAR NOT NULL, ts BIGINT NOT NULL, acct DOUBLE, top DOUBLE, PRIMARY KEY (inst_id, ts)
);
CREATE TABLE IF NOT EXISTS liquidations (
    inst_id VARCHAR NOT NULL, ts BIGINT NOT NULL, pos_side VARCHAR, side VARCHAR, px DOUBLE, sz DOUBLE,
    PRIMARY KEY (inst_id, ts, pos_side, px, sz)
);
CREATE TABLE IF NOT EXISTS heat_state (
    inst_id VARCHAR NOT NULL, stream VARCHAR NOT NULL, oldest BIGINT, newest BIGINT, done BOOLEAN, last_run BIGINT,
    PRIMARY KEY (inst_id, stream)
);
"""


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def family(inst: str) -> str:
    return inst.rsplit("-", 1)[0]


def index_id(inst: str) -> str:
    return family(inst)


# ---------------------------------------------------------------- fetchers (newest-first pages)
def fetch_taker(client: OkxClient, inst: str, end: int | None = None) -> list[tuple]:
    p: dict[str, Any] = {"instId": inst, "period": "1H", "limit": "100"}
    if end is not None:
        p["end"] = str(end)
    return [(inst, int(r[0]), _f(r[1]), _f(r[2])) for r in client.get(TAKER_PATH, p)]


def fetch_mark_index(client: OkxClient, inst: str, after: int | None = None) -> list[tuple]:
    p: dict[str, Any] = {"instId": inst, "bar": "1H", "limit": "100"}
    q: dict[str, Any] = {"instId": index_id(inst), "bar": "1H", "limit": "100"}
    if after is not None:
        p["after"] = q["after"] = str(after)
    def typical(r: list[str]) -> float | None:
        """Hourly typical price (O+H+L+C)/4; a single close is too coarse for tick-sized premiums on cheap coins."""
        v = [_f(x) for x in r[1:5]]
        return None if any(x is None for x in v) else sum(v) / 4

    m = {int(r[0]): typical(r) for r in client.get(MARK_PATH, p) if len(r) < 6 or r[5] == "1"}
    try:
        ix = {int(r[0]): typical(r) for r in client.get(INDEX_PATH, q) if len(r) < 6 or r[5] == "1"}
    except OkxError:
        ix = {}
    return [(inst, t, m[t], ix.get(t)) for t in sorted(m, reverse=True)]


def fetch_ls(client: OkxClient, inst: str) -> list[tuple]:
    a = {int(r[0]): _f(r[1]) for r in client.get(LS_PATH, {"instId": inst, "period": "5m", "limit": "100"})}
    try:
        t = {int(r[0]): _f(r[1]) for r in client.get(TOP_PATH, {"instId": inst, "period": "5m", "limit": "100"})}
    except OkxError:
        t = {}
    return [(inst, ts, a.get(ts), t.get(ts)) for ts in sorted(set(a) | set(t))]


def fetch_liq(client: OkxClient, inst: str) -> list[tuple]:
    out = []
    for grp in client.get(LIQ_PATH, {"instType": "SWAP", "instFamily": family(inst), "state": "filled", "limit": "100"}):
        if grp.get("instId") and grp["instId"] != inst:
            continue
        for d in grp.get("details", []):
            out.append((inst, int(d["ts"]), d.get("posSide"), d.get("side"), _f(d.get("bkPx")), _f(d.get("sz"))))
    return out


# ---------------------------------------------------------------- incremental sync
class HeatSync:
    def __init__(self, client: OkxClient):
        self.client = client
        self.buf: dict[str, list[tuple]] = {"taker_volume": [], "mark_index": [], "ls_ratio": [], "liquidations": []}
        self.state: dict[tuple[str, str], list[Any]] = {}
        self.dirty: set[tuple[str, str]] = set()
        self.n_req = 0

    def load(self, con: duckdb.DuckDBPyConnection) -> None:
        try:
            for r in con.execute("SELECT inst_id, stream, oldest, newest, done, last_run FROM heat_state").fetchall():
                self.state[(r[0], r[1])] = [r[2], r[3], bool(r[4]), r[5]]
        except duckdb.CatalogException:
            pass

    def st(self, inst: str, stream: str) -> list[Any]:
        return self.state.setdefault((inst, stream), [None, None, False, None])

    def mark(self, inst: str, stream: str, keys: list[int], done: bool | None = None) -> None:
        s = self.st(inst, stream)
        if keys:
            s[0] = min([x for x in (s[0],) if x is not None] + keys)
            s[1] = max([x for x in (s[1],) if x is not None] + keys)
        if done is not None:
            s[2] = done
        s[3] = db.now_ms()
        self.dirty.add((inst, stream))

    COLS = {"taker_volume": ["inst_id", "ts", "sell", "buy"], "mark_index": ["inst_id", "ts", "mark", "idx"],
            "ls_ratio": ["inst_id", "ts", "acct", "top"], "liquidations": ["inst_id", "ts", "pos_side", "side", "px", "sz"]}

    def flush(self, con: duckdb.DuckDBPyConnection) -> int:
        ensure_schema(con)
        n = 0
        for t, rows in self.buf.items():
            if rows:
                key = ["inst_id", "ts"] if t != "liquidations" else ["inst_id", "ts", "pos_side", "px", "sz"]
                df = pd.DataFrame(rows, columns=self.COLS[t]).drop_duplicates(key, keep="last")
                con.register("_buf", df)
                con.execute(f"INSERT OR REPLACE INTO {t} SELECT * FROM _buf")
                con.unregister("_buf")
                n += len(df)
                rows.clear()
        for k in list(self.dirty):
            o, nw, d, last = self.state[k]
            con.execute("INSERT OR REPLACE INTO heat_state VALUES (?, ?, ?, ?, ?, ?)", [k[0], k[1], o, nw, d, last])
        self.dirty.clear()
        return n

    def due(self, inst: str, stream: str, every: int, now: int) -> bool:
        last = self.st(inst, stream)[3]
        return last is None or now - last >= every

    def forward(self, inst: str, stream: str) -> None:
        """Fetch the newest page (enough to cover a sync gap of several hours)."""
        if stream == "taker":
            rows = fetch_taker(self.client, inst)
            self.buf["taker_volume"] += rows
            self.mark(inst, stream, [r[1] for r in rows])
        elif stream == "mark":
            rows = fetch_mark_index(self.client, inst)
            self.buf["mark_index"] += rows
            self.mark(inst, stream, [r[1] for r in rows])
            self.n_req += 1
        elif stream == "ls":
            rows = fetch_ls(self.client, inst)
            self.buf["ls_ratio"] += rows
            self.mark(inst, stream, [r[1] for r in rows])
            self.n_req += 1
        elif stream == "liq":
            rows = fetch_liq(self.client, inst)
            self.buf["liquidations"] += rows
            self.mark(inst, stream, [r[1] for r in rows])
        self.n_req += 1


def sync_heat(cfg: Config, client: OkxClient, deadline: float | None = None, max_sec: float | None = 60,
              focus: list[str] | None = None) -> dict[str, Any]:
    """Forward sync. Taker/mark for every instrument once an hour; L/S ratio and liquidations for `focus`
    (watchlist + current top-heat coins) every sync. The rest of the market is covered on a slower cycle
    (heat.ls_all_every_min, default 360: one 5m L/S page holds ~8h; heat.liq_all_every_min, default 60), lowest
    priority, so a busy sync only delays them."""
    hc = cfg.raw.get("heat") or {}
    ls_every = int(hc.get("ls_all_every_min", 360)) * 60_000
    liq_every = int(hc.get("liq_all_every_min", 60)) * 60_000
    all_market = bool(hc.get("sentiment_all", True))
    if max_sec is not None:
        cap = time.monotonic() + max_sec
        deadline = cap if deadline is None else min(deadline, cap)
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
        insts = db.list_candle_insts(con)
        hs = HeatSync(client)
        hs.load(con)
    now = db.now_ms()
    focus = [i for i in (focus or []) if i in set(insts)]
    jobs: list[tuple[int, int, str, str]] = []
    for inst in focus:
        for s in ("ls", "liq"):
            if hs.due(inst, s, 4 * 60_000, now):
                jobs.append((0, hs.st(inst, s)[3] or 0, inst, s))
    for inst in insts:
        for s in ("taker", "mark"):
            if hs.due(inst, s, HOUR, now):
                jobs.append((1, hs.st(inst, s)[3] or 0, inst, s))
    if all_market:
        fset = set(focus)
        for inst in insts:
            if inst in fset:
                continue
            if hs.due(inst, "liq", liq_every, now):
                jobs.append((2, hs.st(inst, "liq")[3] or 0, inst, "liq"))
            if hs.due(inst, "ls", ls_every, now):
                jobs.append((3, hs.st(inst, "ls")[3] or 0, inst, "ls"))
    jobs.sort()
    done = 0
    for _, _, inst, s in jobs:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            hs.forward(inst, s)
            done += 1
        except (InstrumentNotFound, OkxError) as e:
            log.debug("%s %s: %s", inst, s, e)
            hs.mark(inst, s, [])
        except (BudgetExceeded, RetryExhausted):
            break
    try:
        with db.open_db(cfg.paths.db, wait_sec=2.0, max_tries=15) as con:
            n = hs.flush(con)
    except db.DbBusy:
        n = 0
        log.warning("heat flush deferred: database busy")
    log.info("heat sync: %d/%d jobs, %d rows, %d requests", done, len(jobs), n, hs.n_req)
    return {"jobs": len(jobs), "done": done, "rows": n, "requests": hs.n_req}


# ---------------------------------------------------------------- history backfill
def backfill(cfg: Config, max_minutes: float | None = None, progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    """Page taker volume and mark/index candles back to `heat.backfill_start` (default 2024-09-19). Rows newer than
    the replay boundary go to the live DB, older rows to the old-period DB. Resumable via heat_state."""
    from okxrev import replay
    hc = cfg.raw.get("heat") or {}
    start = parse_utc_ms(hc.get("backfill_start", "2024-09-19"))
    boundary = parse_utc_ms(replay.rcfg(cfg)["end"]) + 4 * 86_400_000
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    client = OkxClient(cfg.okx, deadline=deadline)
    old_db = replay.old_db_path(cfg)
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        insts = db.list_candle_insts(con)
        meta = db.sync_overview(con).set_index("inst_id")
    with db.open_db(cfg.paths.db, wait_sec=2.0, max_tries=60) as con:
        ensure_schema(con)
        hs = HeatSync(client)
        hs.load(con)
    if old_db.exists():
        with db.open_db(old_db, wait_sec=2.0, max_tries=30) as con:
            ensure_schema(con)
    n_rows = 0

    def write(rows: dict[str, list[tuple]]) -> int:
        """Bulk insert via a registered DataFrame (executemany row-by-row held the write lock for minutes)."""
        n = 0
        for t, rs in rows.items():
            if not rs:
                continue
            cols = {"taker_volume": ["inst_id", "ts", "sell", "buy"], "mark_index": ["inst_id", "ts", "mark", "idx"]}[t]
            df = pd.DataFrame(rs, columns=cols).drop_duplicates(["inst_id", "ts"], keep="last")
            for path, part in ((cfg.paths.db, df[df["ts"] >= boundary]), (old_db, df[df["ts"] < boundary])):
                if len(part) and (path == cfg.paths.db or old_db.exists()):
                    with db.open_db(path, wait_sec=2.0, max_tries=60) as con:
                        ensure_schema(con)
                        con.register("_part", part)
                        con.execute(f"INSERT OR REPLACE INTO {t} SELECT * FROM _part")
                        con.unregister("_part")
                    n += len(part)
        return n

    import threading
    wlock = threading.Lock()

    def locked_write(rows: dict[str, list[tuple]]) -> int:
        with wlock:
            return write(rows)

    def save_locked() -> None:
        with wlock:
            _save_state(cfg, hs)

    def run_stream(stream: str) -> bool:
        """Backfill one stream for all instruments; taker (rubik limiter) and mark (default limiter) run in parallel."""
        nonlocal n_rows
        for k, inst in enumerate(insts, 1):
            lt = meta.at[inst, "list_time"] if inst in meta.index else None
            floor = max(start, int(lt) if lt is not None and not pd.isna(lt) else start)
            for stream in (stream,):
                bstream = f"fill:{stream}"
                s = hs.st(inst, bstream)
                if s[2]:
                    continue
                cursor = s[0]
                buf: list[tuple] = []
                complete = False
                try:
                    while True:
                        if stream == "taker":
                            rows = fetch_taker(client, inst, end=(cursor - 1) if cursor else None)
                        else:
                            rows = fetch_mark_index(client, inst, after=cursor)
                        if not rows:
                            complete = True
                            break
                        buf += rows
                        lo = min(r[1] for r in rows)
                        with wlock:
                            hs.mark(inst, bstream, [r[1] for r in rows])
                        if cursor is not None and lo >= cursor:
                            complete = True
                            break
                        cursor = lo
                        if lo <= floor:
                            complete = True
                            break
                        if len(buf) >= 20000:
                            n_rows += locked_write({"taker_volume" if stream == "taker" else "mark_index": buf})
                            buf = []
                            save_locked()
                except (InstrumentNotFound, OkxError) as e:
                    log.debug("backfill %s %s: %s", inst, stream, e)
                    complete = True
                except (BudgetExceeded, RetryExhausted):
                    n_rows += locked_write({"taker_volume" if stream == "taker" else "mark_index": buf})
                    save_locked()
                    return False
                n_rows += locked_write({"taker_volume" if stream == "taker" else "mark_index": buf})
                if complete:
                    with wlock:
                        hs.mark(inst, bstream, [], True)
                save_locked()
            if progress:
                progress(k, len(insts), f"{stream} {inst}")
        return True

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(run_stream, ("taker", "mark")))
        n_done = len(insts) if all(results) else 0
    finally:
        client.close()
    return {"insts": len(insts), "done": n_done, "rows": n_rows, "complete": all(results)}


def _save_state(cfg: Config, hs: HeatSync) -> None:
    """Persist backfill progress (heat_state). Backfill writes rows itself, so the row buffers are empty here."""
    with db.open_db(cfg.paths.db, wait_sec=2.0, max_tries=60) as con:
        hs.flush(con)


def backfill_status(cfg: Config) -> dict[str, Any]:
    try:
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=1.0, max_tries=5) as con:
            r = con.execute("SELECT stream, COUNT(*), SUM(done::INT) FROM heat_state WHERE stream LIKE 'fill:%' GROUP BY 1").fetchall()
    except (db.DbBusy, duckdb.CatalogException, FileNotFoundError):
        return {}
    return {s: {"insts": int(n), "done": int(d or 0)} for s, n, d in r}


# ---------------------------------------------------------------- loaders
def load_all(con: duckdb.DuckDBPyConnection, insts: list[str] | None = None, since: int | None = None) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    where = " WHERE ts >= ?" if since is not None else ""
    args = [since] if since is not None else []
    for t in ("taker_volume", "mark_index", "ls_ratio", "liquidations"):
        try:
            df = con.execute(f"SELECT * FROM {t}{where}", args).df()
        except duckdb.CatalogException:
            df = pd.DataFrame()
        if insts is not None and len(df):
            df = df[df["inst_id"].isin(insts)]
        out[t] = df
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.heatdata")
    ap.add_argument("--config", default=None)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "heat")
    if args.backfill:
        from filelock import FileLock, Timeout
        lock = FileLock(str(cfg.paths.db) + ".heatfill.lock", timeout=0)
        try:
            lock.acquire()
        except Timeout:
            print("heat backfill already running")
            return 0
        try:
            def prog(k: int, n: int, inst: str) -> None:
                if k % 10 == 0 or k == n:
                    log.info("heat backfill %d/%d (%s)", k, n, inst)
            print(backfill(cfg, args.max_minutes, prog))
        finally:
            lock.release()
    print(backfill_status(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
