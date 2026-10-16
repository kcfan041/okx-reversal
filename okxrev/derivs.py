"""Funding-rate and open-interest history (public endpoints) for USDT swaps.

OKX keeps ~3.5 months of funding history and short OI history (5m: hours, 1H: ~6 weeks, 1D: ~1 year),
so this module backfills what it can and then accumulates forward on every scheduled sync.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any, Callable

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config
from okxrev.log import setup_logging
from okxrev.okx_client import BudgetExceeded, InstrumentNotFound, OkxClient, OkxError, RetryExhausted

log = logging.getLogger("okxrev.derivs")

FUNDING_PATH = "/api/v5/public/funding-rate-history"
OI_PATH = "/api/v5/rubik/stat/contracts/open-interest-history"
OI_PERIODS = {"5m": 300_000, "1H": 3_600_000, "1D": 86_400_000}
OI_1H_EVERY = 3_600_000
FUNDING_GRACE = 5 * 60_000
FUNDING_DEFAULT = 4 * 3_600_000
FUNDING_MAX_AGE = 9 * 3_600_000


def funding_intervals(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Settlement interval per coin = the most common gap between its last ~10 funding timestamps (2h/4h/8h)."""
    try:
        rows = con.execute("""
            WITH x AS (SELECT inst_id, ts, ROW_NUMBER() OVER (PARTITION BY inst_id ORDER BY ts DESC) rn FROM funding),
                 y AS (SELECT inst_id, ts - LEAD(ts) OVER (PARTITION BY inst_id ORDER BY ts DESC) d FROM x WHERE rn <= 10)
            SELECT inst_id, mode(d) FROM y WHERE d > 0 GROUP BY 1""").fetchall()
    except duckdb.Error:
        return {}
    return {i: int(d) for i, d in rows if d and 3_600_000 <= d <= 24 * 3_600_000}

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding (
    inst_id  VARCHAR NOT NULL,
    ts       BIGINT  NOT NULL,
    rate     DOUBLE,
    realized DOUBLE,
    PRIMARY KEY (inst_id, ts)
);
CREATE TABLE IF NOT EXISTS open_interest (
    inst_id  VARCHAR NOT NULL,
    period   VARCHAR NOT NULL,
    ts       BIGINT  NOT NULL,
    oi_ct    DOUBLE,
    oi_ccy   DOUBLE,
    oi_usd   DOUBLE,
    PRIMARY KEY (inst_id, period, ts)
);
CREATE TABLE IF NOT EXISTS derivs_state (
    inst_id    VARCHAR NOT NULL,
    stream     VARCHAR NOT NULL,
    newest_ts  BIGINT,
    oldest_ts  BIGINT,
    backfill_done BOOLEAN DEFAULT FALSE,
    last_run   BIGINT,
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


def fetch_funding(client: OkxClient, inst: str, after: int | None = None) -> list[tuple]:
    p: dict[str, Any] = {"instId": inst, "limit": "100"}
    if after is not None:
        p["after"] = str(after)
    return [(inst, int(r["fundingTime"]), _f(r.get("fundingRate")), _f(r.get("realizedRate"))) for r in client.get(FUNDING_PATH, p)]


def fetch_oi(client: OkxClient, inst: str, period: str, end: int | None = None) -> list[tuple]:
    p: dict[str, Any] = {"instId": inst, "period": period, "limit": "100"}
    if end is not None:
        p["end"] = str(end)
    return [(inst, period, int(r[0]), _f(r[1]), _f(r[2]), _f(r[3])) for r in client.get(OI_PATH, p)]


class DerivSync:
    """Network calls happen with no DB connection open; rows and state are buffered and flushed in short
    write transactions via `flush` (or written directly when a connection is passed, as in tests)."""

    def __init__(self, cfg: Config, client: OkxClient, con: duckdb.DuckDBPyConnection | None = None):
        self.cfg, self.client, self.con = cfg, client, con
        self.n_rows = 0
        self.n_req = 0
        self.buf: dict[str, list[tuple]] = {"funding": [], "open_interest": []}
        self.states: dict[tuple[str, str], tuple[int | None, int | None, bool, int | None]] = {}
        self.dirty: set[tuple[str, str]] = set()
        self.intervals: dict[str, int] = {}
        if con is not None:
            ensure_schema(con)
            self.load_states(con)

    def load_states(self, con: duckdb.DuckDBPyConnection) -> None:
        for r in con.execute("SELECT inst_id, stream, newest_ts, oldest_ts, backfill_done, last_run FROM derivs_state").fetchall():
            self.states[(r[0], r[1])] = (r[2], r[3], bool(r[4]), r[5])
        self.intervals = funding_intervals(con)

    def _state(self, inst: str, stream: str) -> tuple[int | None, int | None, bool, int | None]:
        return self.states.get((inst, stream), (None, None, False, None))

    def _save(self, inst: str, stream: str, keys: list[int], done: bool | None = None) -> None:
        newest, oldest, bdone, _ = self._state(inst, stream)
        if keys:
            newest = max([x for x in (newest,) if x is not None] + keys)
            oldest = min([x for x in (oldest,) if x is not None] + keys)
        self.states[(inst, stream)] = (newest, oldest, bdone if done is None else done, db.now_ms())
        self.dirty.add((inst, stream))
        if self.con is not None:
            self.flush(self.con)

    def _write(self, table: str, rows: list[tuple]) -> None:
        if rows:
            self.buf[table].extend(rows)
            self.n_rows += len(rows)

    def pending(self) -> int:
        return sum(len(v) for v in self.buf.values())

    def flush(self, con: duckdb.DuckDBPyConnection) -> None:
        ensure_schema(con)
        for table, rows in self.buf.items():
            if rows:
                con.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({', '.join(['?'] * len(rows[0]))})", rows)
            rows.clear()
        for key in list(self.dirty):
            n, o, d, last = self.states[key]
            con.execute(
                """INSERT INTO derivs_state VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (inst_id, stream) DO UPDATE SET newest_ts = excluded.newest_ts, oldest_ts = excluded.oldest_ts,
                   backfill_done = excluded.backfill_done, last_run = excluded.last_run""", [key[0], key[1], n, o, d, last])
        self.dirty.clear()

    def funding(self, inst: str, max_pages: int = 5) -> None:
        newest, oldest, done, _ = self._state(inst, "funding")
        rows = fetch_funding(self.client, inst)
        self.n_req += 1
        self._write("funding", rows)
        keys = [r[1] for r in rows]
        if not done:
            after = min(keys) if keys else oldest
            for _ in range(max_pages):
                if after is None:
                    break
                page = fetch_funding(self.client, inst, after=after)
                self.n_req += 1
                if not page:
                    done = True
                    break
                self._write("funding", page)
                keys += [r[1] for r in page]
                after = min(r[1] for r in page)
        self._save(inst, "funding", keys, done)

    def oi(self, inst: str, period: str, max_pages: int = 3) -> None:
        stream = f"oi:{period}"
        newest, oldest, done, _ = self._state(inst, stream)
        rows = fetch_oi(self.client, inst, period)
        self.n_req += 1
        self._write("open_interest", rows)
        keys = [r[2] for r in rows]
        if not keys and oldest is None:
            done = True
        if not done and period != "5m":
            end = (min(keys) if keys else oldest)
            for _ in range(max_pages):
                if end is None:
                    break
                page = fetch_oi(self.client, inst, period, end=end - 1)
                self.n_req += 1
                if not page:
                    done = True
                    break
                self._write("open_interest", page)
                keys += [r[2] for r in page]
                end = min(r[2] for r in page)
        self._save(inst, stream, keys, done if period != "5m" else True)

    def due(self, inst: str, stream: str, now: int, every_ms: int) -> bool:
        _, _, _, last = self._state(inst, stream)
        return last is None or now - last >= every_ms

    def funding_due(self, inst: str, now: int) -> tuple[bool, float]:
        """Funding settles every 2/4/8h depending on the coin. Due once the next settlement (last settled + the
        coin's interval) is FUNDING_GRACE past, or when the last attempt is older than FUNDING_MAX_AGE (safety net
        for coins whose interval changed). Unknown interval -> every FUNDING_DEFAULT."""
        newest, _, _, last = self._state(inst, "funding")
        if last is None:
            return True, 1e9
        iv = self.intervals.get(inst)
        if newest is not None and iv:
            nxt = newest + iv + FUNDING_GRACE
            if now >= nxt and last < nxt:
                return True, (now - nxt) / iv + 1.0
        if now - last >= FUNDING_MAX_AGE:
            return True, (now - last) / FUNDING_MAX_AGE
        if not iv and now - last >= FUNDING_DEFAULT:
            return True, (now - last) / FUNDING_DEFAULT
        return False, 0.0

    def run(self, insts: list[str], deadline: float | None = None, on_batch: Callable[[], None] | None = None,
            oi5m_insts: set[str] | None = None) -> dict[str, Any]:
        """Jobs are scheduled purely by staleness (oldest refresh first) within each stream's interval, so streams whose
        history can never be fully backfilled (OKX keeps only ~3 months of funding) cannot starve the others.
        Each job always fetches the newest page and then pages back a little if history is still incomplete.
        5-minute OI is fetched only for `oi5m_insts` (None = every instrument); nothing reads it beyond research."""
        now = db.now_ms()
        jobs: list[tuple[float, str, str]] = []
        for inst in insts:
            ok, prio = self.funding_due(inst, now)
            if ok:
                jobs.append((prio, inst, "funding"))
            for stream, every in (("oi:5m", 15 * 60_000), ("oi:1H", OI_1H_EVERY), ("oi:1D", 86_400_000)):
                if stream == "oi:5m" and oi5m_insts is not None and inst not in oi5m_insts:
                    continue
                if self.due(inst, stream, now, every):
                    _, _, _, last = self._state(inst, stream)
                    jobs.append((((now - last) / every) if last else 1e9, inst, stream))
        jobs.sort(key=lambda j: -j[0])
        jobs = [(0, 0, i, s) for _, i, s in jobs]
        n_done = 0
        for _, _, inst, stream in jobs:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                if stream == "funding":
                    self.funding(inst)
                else:
                    self.oi(inst, stream.split(":")[1])
                n_done += 1
            except (InstrumentNotFound, OkxError) as e:
                log.debug("%s %s: %s", inst, stream, e)
                self._save(inst, stream, [], True)
            except (BudgetExceeded, RetryExhausted):
                break
            if on_batch is not None and (self.pending() >= 20_000 or n_done % 50 == 0):
                on_batch()
        return {"jobs": len(jobs), "done": n_done, "rows": self.n_rows, "requests": self.n_req}


def sync_derivs(cfg: Config, client: OkxClient, deadline: float | None = None, max_sec: float | None = None,
                focus: list[str] | None = None) -> dict[str, Any]:
    if max_sec is not None:
        cap = time.monotonic() + max_sec
        deadline = cap if deadline is None else min(deadline, cap)
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
        insts = db.list_candle_insts(con)
        try:
            ensure_ok = con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'derivs_state'").fetchone()
        except duckdb.Error:
            ensure_ok = None
        s = DerivSync(cfg, client)
        if ensure_ok:
            s.load_states(con)

    def flush() -> None:
        try:
            with db.open_db(cfg.paths.db, wait_sec=2.0, max_tries=15) as wcon:
                s.flush(wcon)
        except db.DbBusy:
            log.warning("derivs flush deferred: database busy")

    scope = str(cfg.sync.get("oi5m_scope", "focus")).lower()
    oi5m = None if scope == "all" else set(focus or [])
    r = s.run(insts, deadline, on_batch=flush, oi5m_insts=oi5m)
    flush()
    log.info("derivs: %d/%d jobs, %d rows, %d requests", r["done"], r["jobs"], r["rows"], r["requests"])
    return r


def load_funding(con: duckdb.DuckDBPyConnection, inst: str) -> pd.DataFrame:
    try:
        return con.execute("SELECT ts, rate FROM funding WHERE inst_id = ? ORDER BY ts", [inst]).df()
    except duckdb.CatalogException:
        return pd.DataFrame(columns=["ts", "rate"])


def load_oi(con: duckdb.DuckDBPyConnection, inst: str) -> pd.DataFrame:
    """Best available OI per timestamp: finest period wins."""
    try:
        df = con.execute("SELECT period, ts, oi_usd FROM open_interest WHERE inst_id = ? ORDER BY ts", [inst]).df()
    except duckdb.CatalogException:
        return pd.DataFrame(columns=["ts", "oi_usd"])
    return df


def attach(g: pd.DataFrame, fund: pd.DataFrame, oi: pd.DataFrame) -> pd.DataFrame:
    """Add funding_rate (last settled, known at bar close) and oi_chg_24h (from daily/hourly OI, lagged) to bars."""
    idx = g.index.to_numpy(np.int64)
    out = pd.DataFrame(index=g.index)
    if len(fund):
        f = fund.sort_values("ts")
        k = np.searchsorted(f["ts"].to_numpy(np.int64), idx + db.BAR_MS, side="right") - 1
        out["funding"] = np.where(k >= 0, f["rate"].to_numpy(float)[np.clip(k, 0, None)], np.nan)
    else:
        out["funding"] = np.nan
    out["oi_chg_24h"] = np.nan
    if len(oi):
        for period, lag, look in (("1D", 86_400_000, 1), ("1H", 3_600_000, 24)):
            o = oi[oi["period"] == period].sort_values("ts")
            if len(o) <= look:
                continue
            ts = o["ts"].to_numpy(np.int64) + lag
            v = o["oi_usd"].to_numpy(float)
            chg = np.full(len(v), np.nan)
            chg[look:] = v[look:] / v[:-look] - 1
            k = np.searchsorted(ts, idx + db.BAR_MS, side="right") - 1
            val = np.where(k >= 0, chg[np.clip(k, 0, None)], np.nan)
            out["oi_chg_24h"] = np.where(np.isnan(val), out["oi_chg_24h"], val)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.derivs")
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-minutes", type=float, default=30.0)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "derivs")
    deadline = time.monotonic() + args.max_minutes * 60
    client = OkxClient(cfg.okx, deadline=deadline)
    try:
        with db.open_db(cfg.paths.db) as con:
            ensure_schema(con)
        r = sync_derivs(cfg, client, deadline)
    finally:
        client.close()
    print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
