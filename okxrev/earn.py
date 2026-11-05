"""OKX Simple Earn (savings) lending rates: hourly history per currency, and whether high rates pay for holding.

Data (public, no key): /api/v5/finance/savings/lending-rate-summary lists the currencies; lending-rate-history gives one
row per hour with the market rate ("rate") and the rate lenders actually got ("lendingRate"), annualised. The backfill
walks back to SINCE and is resumable; --sync only fetches the newest pages. Storage: data/earn.duckdb (only writer).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging
from okxrev.okx_client import OkxClient, OkxError, RateLimiter

log = logging.getLogger("okxrev.earn")

H = 3_600_000
D = 86_400_000
SINCE = 1_726_704_000_000          # 2024-09-19, where the old candle DB starts
REQ_PER_SEC = 4.0                  # endpoint limit is 6/s per IP; the sync shares the IP
SCHEMA = """
CREATE TABLE IF NOT EXISTS lending_rate (ccy VARCHAR, ts BIGINT, rate DOUBLE, lending_rate DOUBLE, PRIMARY KEY (ccy, ts));
CREATE TABLE IF NOT EXISTS earn_state (ccy VARCHAR PRIMARY KEY, oldest_ts BIGINT, newest_ts BIGINT, back_done BOOLEAN, updated BIGINT);
CREATE TABLE IF NOT EXISTS earn_summary (ccy VARCHAR PRIMARY KEY, avg_rate DOUBLE, est_rate DOUBLE, pre_rate DOUBLE, fetched BIGINT);
"""


def db_path(cfg: Config) -> Path:
    return cfg.paths.root / "data" / "earn.duckdb"


def connect(cfg: Config, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = db_path(cfg)
    if read_only:
        return db.connect(p, read_only=True, wait_sec=1.0, max_tries=30)
    p.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for _ in range(30):                    # not db.connect: in write mode it creates the candle schema
        try:
            con = duckdb.connect(str(p))
            con.execute(SCHEMA)
            return con
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            if not db._is_lock_error(e):
                raise
            last = e
            time.sleep(1.0)
    raise db.DbBusy(str(last))


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _rows(ccy: str, data: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [(ccy, int(x["ts"]), _f(x.get("rate")), _f(x.get("lendingRate"))) for x in data if x.get("ts")]


def summary(client: OkxClient, con: duckdb.DuckDBPyConnection) -> list[str]:
    data = client.get("/api/v5/finance/savings/lending-rate-summary", {})
    now = db.now_ms()
    summ = pd.DataFrame([(x["ccy"], _f(x.get("avgRate")), _f(x.get("estRate")), _f(x.get("preRate")), now) for x in data if x.get("ccy")],
                        columns=["ccy", "avg_rate", "est_rate", "pre_rate", "fetched"])
    con.execute("INSERT OR REPLACE INTO earn_summary SELECT * FROM summ")
    return sorted(x["ccy"] for x in data if x.get("ccy"))     # an empty ccy would page through every currency


def sync_ccy(client: OkxClient, con: duckdb.DuckDBPyConnection, ccy: str, back: bool) -> int:
    """Catch up from the newest page to the stored range, then (back) continue below the oldest row to SINCE.
    The state is saved after every page, so an interrupted back-fill resumes where it stopped."""
    st = con.execute("SELECT oldest_ts, newest_ts, back_done FROM earn_state WHERE ccy = ?", [ccy]).fetchone()
    oldest, newest, done = st if st else (None, None, False)
    n = 0

    def walk(after: int | None, stop: int) -> bool:
        """Pages going back from `after` (None = newest) until a row <= stop; True when the history ran out."""
        nonlocal oldest, newest, n
        while True:
            params: dict[str, Any] = {"ccy": ccy, "limit": 100, **({"after": after} if after else {})}
            try:
                rows = _rows(ccy, client.get("/api/v5/finance/savings/lending-rate-history", params))
            except OkxError as e:
                if e.code == "58003":      # listed in the summary but "Savings doesn't support this crypto"
                    return True
                raise
            if not rows:
                return True
            page = pd.DataFrame(rows, columns=["ccy", "ts", "rate", "lending_rate"])     # one statement: executemany commits per row
            con.execute("INSERT OR REPLACE INTO lending_rate SELECT * FROM page")
            n += len(rows)
            lo, hi = min(r[1] for r in rows), max(r[1] for r in rows)
            newest, oldest = max(newest or 0, hi), lo if oldest is None else min(oldest, lo)
            con.execute("INSERT OR REPLACE INTO earn_state VALUES (?, ?, ?, ?, ?)", [ccy, oldest, newest, bool(done), db.now_ms()])
            if lo <= stop:
                return False
            if len(rows) < 100:
                return True
            after = lo

    if newest is None:
        ended = walk(None, SINCE if back else db.now_ms() - 7 * D)
    else:
        walk(None, newest)
        ended = walk(oldest, SINCE) if back and not done else False
    done = bool(done or ended or (oldest is not None and oldest <= SINCE))
    con.execute("INSERT OR REPLACE INTO earn_state VALUES (?, ?, ?, ?, ?)", [ccy, oldest, newest, done, db.now_ms()])
    return n


def sync(cfg: Config, back: bool = True, max_sec: float | None = None) -> dict[str, Any]:
    t0 = time.monotonic()
    client = OkxClient(cfg.okx, deadline=t0 + max_sec if max_sec else None)
    client.limiter = RateLimiter(REQ_PER_SEC, name="earn")
    total, done_n = 0, 0
    con = connect(cfg)
    try:
        ccys = summary(client, con)
    finally:
        con.close()
    try:
        for i, ccy in enumerate(ccys):
            con = connect(cfg)             # reopened per currency so readers (the study, the web page) can get in between
            try:
                total += sync_ccy(client, con, ccy, back)
            except Exception as e:  # noqa: BLE001 - one currency failing should not stop the rest
                if type(e).__name__ == "BudgetExceeded":
                    break
                log.warning("earn %s: %s", ccy, e)
            finally:
                con.close()
                if back:
                    time.sleep(1.5)        # a reader retrying every second (the study, the web page) gets in between currencies
            done_n = i + 1
            if done_n % 10 == 0:
                log.info("earn sync: %d/%d currencies, %d rows, %d requests", done_n, len(ccys), total, client.n_requests)
    finally:
        client.close()
    log.info("earn sync done: %d currencies, %d rows in %.0fs", done_n, total, time.monotonic() - t0)
    return {"currencies": done_n, "rows": total}


def status(cfg: Config) -> dict[str, Any]:
    if not db_path(cfg).exists():
        return {"currencies": 0}
    with db.open_db(db_path(cfg), read_only=True, wait_sec=1.0, max_tries=5) as con:
        n, done, lo, hi, rows = con.execute("""SELECT count(*), count(*) FILTER (WHERE back_done), min(oldest_ts), max(newest_ts),
                                                 (SELECT count(*) FROM lending_rate) FROM earn_state""").fetchone()
    return {"currencies": n, "back_done": done, "oldest": ms_to_str(lo) if lo else None, "newest": ms_to_str(hi) if hi else None, "rows": rows}


def load(cfg: Config) -> dict[str, Any] | None:
    from okxrev import earn_study
    return earn_study.load(cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.earn")
    ap.add_argument("--config", default=None)
    ap.add_argument("--backfill", action="store_true", help="fetch history back to 2024-09-19 (resumable, about 2 hours)")
    ap.add_argument("--sync", action="store_true", help="fetch only the newest pages")
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--max-sec", type=float, default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "earn")
    if args.backfill or args.sync or args.evaluate:
        sync(cfg, back=args.backfill, max_sec=args.max_sec or (None if args.backfill else 600))   # --evaluate alone first fetches the newest hours
    if args.evaluate:
        from okxrev import earn_study
        earn_study.evaluate(cfg)
    print(status(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
