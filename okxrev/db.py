from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import duckdb
import pandas as pd

log = logging.getLogger("okxrev.db")

BAR_MS = 5 * 60 * 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    inst_id    VARCHAR NOT NULL,
    ts         BIGINT  NOT NULL,
    open       DOUBLE  NOT NULL,
    high       DOUBLE  NOT NULL,
    low        DOUBLE  NOT NULL,
    close      DOUBLE  NOT NULL,
    vol        DOUBLE,
    vol_ccy    DOUBLE,
    vol_quote  DOUBLE,
    PRIMARY KEY (inst_id, ts)
);

CREATE TABLE IF NOT EXISTS instruments (
    inst_id      VARCHAR PRIMARY KEY,
    uly          VARCHAR,
    ct_val       DOUBLE,
    list_time    BIGINT,
    state        VARCHAR,
    first_seen   BIGINT,
    last_seen    BIGINT,
    delisted_at  BIGINT
);

CREATE TABLE IF NOT EXISTS sync_state (
    inst_id          VARCHAR PRIMARY KEY,
    oldest_ts        BIGINT,
    newest_ts        BIGINT,
    backfill_done    BOOLEAN DEFAULT FALSE,
    last_forward_at  BIGINT,
    last_backfill_at BIGINT,
    last_error       VARCHAR
);

CREATE TABLE IF NOT EXISTS sync_runs (
    started_at   BIGINT PRIMARY KEY,
    duration_sec DOUBLE,
    requests     INTEGER,
    rows_written BIGINT,
    forward_insts INTEGER,
    backfill_pages INTEGER,
    errors       INTEGER,
    note         VARCHAR
);
"""


class DbBusy(Exception):
    pass


def _is_lock_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in ("lock", "already open", "used by another", "being used by", "conflicting", "cannot open file",
                                  "另一個程序", "另一个程序"))


def connect(
    path: Path,
    read_only: bool = False,
    wait_sec: float = 2.0,
    max_tries: int = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> duckdb.DuckDBPyConnection:
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    last: Exception | None = None
    for attempt in range(max_tries):
        try:
            con = duckdb.connect(str(path), read_only=read_only, config={"threads": 2})
            if not read_only:
                con.execute(SCHEMA)
            return con
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            if not _is_lock_error(e):
                raise
            last = e
            log.debug("db busy (%s), attempt %d/%d", "ro" if read_only else "rw", attempt + 1, max_tries)
            if attempt + 1 < max_tries:
                sleep(wait_sec)
    raise DbBusy(str(last))


@contextmanager
def open_db(path: Path, read_only: bool = False, **kw: Any) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(path, read_only=read_only, **kw)
    try:
        yield con
    finally:
        con.close()


def now_ms() -> int:
    return int(time.time() * 1000)


def upsert_instruments(con: duckdb.DuckDBPyConnection, live: list[dict[str, Any]], now: int | None = None) -> tuple[list[str], list[str]]:
    now = now or now_ms()
    existing = dict(con.execute("SELECT inst_id, state FROM instruments").fetchall())
    live_ids = {d["inst_id"] for d in live}
    new_ids = [d["inst_id"] for d in live if d["inst_id"] not in existing]
    gone = [i for i, s in existing.items() if s == "live" and i not in live_ids]
    con.execute("BEGIN")
    try:
        for d in live:
            con.execute(
                """
                INSERT INTO instruments (inst_id, uly, ct_val, list_time, state, first_seen, last_seen, delisted_at)
                VALUES (?, ?, ?, ?, 'live', ?, ?, NULL)
                ON CONFLICT (inst_id) DO UPDATE SET
                    uly = excluded.uly, ct_val = excluded.ct_val, list_time = excluded.list_time,
                    state = 'live', last_seen = excluded.last_seen, delisted_at = NULL
                """,
                [d["inst_id"], d.get("uly"), d.get("ct_val"), d.get("list_time"), now, now],
            )
            con.execute("INSERT INTO sync_state (inst_id) VALUES (?) ON CONFLICT DO NOTHING", [d["inst_id"]])
        for i in gone:
            con.execute("UPDATE instruments SET state = 'delisted', delisted_at = ? WHERE inst_id = ?", [now, i])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return new_ids, gone


def mark_delisted(con: duckdb.DuckDBPyConnection, inst_id: str, now: int | None = None) -> None:
    now = now or now_ms()
    con.execute(
        "UPDATE instruments SET state = 'delisted', delisted_at = COALESCE(delisted_at, ?) WHERE inst_id = ?",
        [now, inst_id],
    )
    con.execute(
        "INSERT INTO sync_state (inst_id, backfill_done) VALUES (?, TRUE) ON CONFLICT (inst_id) DO UPDATE SET backfill_done = TRUE",
        [inst_id],
    )


def write_batch(
    con: duckdb.DuckDBPyConnection,
    inst_id: str,
    df: pd.DataFrame | None,
    *,
    forward_at: int | None = None,
    backfill_at: int | None = None,
    backfill_done: bool | None = None,
    error: str | None = None,
    clear_error: bool = False,
) -> int:
    n = 0 if df is None else len(df)
    con.execute("BEGIN")
    try:
        con.execute("INSERT INTO sync_state (inst_id) VALUES (?) ON CONFLICT DO NOTHING", [inst_id])
        if n:
            batch = df[["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote"]].copy()
            batch.insert(0, "inst_id", inst_id)
            con.register("_batch", batch)
            try:
                con.execute(
                    "INSERT OR REPLACE INTO candles SELECT inst_id, ts, open, high, low, close, vol, vol_ccy, vol_quote FROM _batch"
                )
            finally:
                con.unregister("_batch")
            lo, hi = int(batch["ts"].min()), int(batch["ts"].max())
            con.execute(
                """
                UPDATE sync_state SET
                    oldest_ts = CASE WHEN oldest_ts IS NULL OR ? < oldest_ts THEN ? ELSE oldest_ts END,
                    newest_ts = CASE WHEN newest_ts IS NULL OR ? > newest_ts THEN ? ELSE newest_ts END
                WHERE inst_id = ?
                """,
                [lo, lo, hi, hi, inst_id],
            )
        sets, args = [], []
        if forward_at is not None:
            sets.append("last_forward_at = ?")
            args.append(forward_at)
        if backfill_at is not None:
            sets.append("last_backfill_at = ?")
            args.append(backfill_at)
        if backfill_done is not None:
            sets.append("backfill_done = ?")
            args.append(backfill_done)
        if error is not None:
            sets.append("last_error = ?")
            args.append(error[:500])
        elif clear_error:
            sets.append("last_error = NULL")
        if sets:
            con.execute(f"UPDATE sync_state SET {', '.join(sets)} WHERE inst_id = ?", [*args, inst_id])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def record_run(con: duckdb.DuckDBPyConnection, run: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO sync_runs
        (started_at, duration_sec, requests, rows_written, forward_insts, backfill_pages, errors, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run["started_at"], run["duration_sec"], run["requests"], run["rows_written"],
         run["forward_insts"], run["backfill_pages"], run["errors"], run.get("note")],
    )


def sync_overview(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        """
        SELECT i.inst_id, i.state, i.list_time, i.delisted_at,
               s.oldest_ts, s.newest_ts, COALESCE(s.backfill_done, FALSE) AS backfill_done,
               s.last_forward_at, s.last_backfill_at, s.last_error
        FROM instruments i LEFT JOIN sync_state s USING (inst_id)
        ORDER BY i.inst_id
        """
    ).df()


def load_candles(
    con: duckdb.DuckDBPyConnection,
    inst_id: str,
    since: int | None = None,
    until: int | None = None,
) -> pd.DataFrame:
    sql = "SELECT ts, open, high, low, close, vol, vol_ccy, vol_quote FROM candles WHERE inst_id = ?"
    args: list[Any] = [inst_id]
    if since is not None:
        sql += " AND ts >= ?"
        args.append(since)
    if until is not None:
        sql += " AND ts < ?"
        args.append(until)
    sql += " ORDER BY ts"
    return con.execute(sql, args).df()


def list_candle_insts(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT inst_id FROM sync_state WHERE newest_ts IS NOT NULL ORDER BY inst_id"
    ).fetchall()]
