from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, load_credentials, ms_to_str
from okxrev.log import setup_logging
from okxrev.okx_client import Credentials, OkxClient

log = logging.getLogger("okxrev.account")

POS_PATH = "/api/v5/account/positions-history"
FILLS_PATH = "/api/v5/trade/fills-history"
DAY_MS = 86_400_000
STABLE = {"USDT", "USDC", "USD"}
CLOSE_TYPES = {"1": "partial", "2": "full", "3": "liquidation", "4": "partial_liquidation", "5": "adl"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions_history (
    pos_id        VARCHAR NOT NULL,
    u_time        BIGINT  NOT NULL,
    c_time        BIGINT,
    inst_type     VARCHAR,
    inst_id       VARCHAR,
    mgn_mode      VARCHAR,
    close_type    VARCHAR,
    pos_side      VARCHAR,
    direction     VARCHAR,
    lever         DOUBLE,
    open_avg_px   DOUBLE,
    close_avg_px  DOUBLE,
    open_max_pos  DOUBLE,
    close_total_pos DOUBLE,
    pnl           DOUBLE,
    realized_pnl  DOUBLE,
    fee           DOUBLE,
    funding_fee   DOUBLE,
    liq_penalty   DOUBLE,
    pnl_ratio     DOUBLE,
    ccy           VARCHAR,
    uly           VARCHAR,
    PRIMARY KEY (pos_id, u_time)
);

CREATE TABLE IF NOT EXISTS fills (
    inst_type  VARCHAR NOT NULL,
    bill_id    VARCHAR NOT NULL,
    trade_id   VARCHAR,
    ord_id     VARCHAR,
    inst_id    VARCHAR,
    side       VARCHAR,
    pos_side   VARCHAR,
    fill_px    DOUBLE,
    fill_sz    DOUBLE,
    fee        DOUBLE,
    fee_ccy    VARCHAR,
    exec_type  VARCHAR,
    ts         BIGINT,
    PRIMARY KEY (inst_type, bill_id)
);

CREATE TABLE IF NOT EXISTS acct_instruments (
    inst_id     VARCHAR PRIMARY KEY,
    inst_type   VARCHAR,
    ct_val      DOUBLE,
    ct_type     VARCHAR,
    ct_val_ccy  VARCHAR,
    settle_ccy  VARCHAR
);

CREATE TABLE IF NOT EXISTS account_sync_state (
    stream       VARCHAR PRIMARY KEY,
    newest_key   BIGINT,
    oldest_key   BIGINT,
    n_rows       BIGINT,
    last_sync_at BIGINT,
    last_error   VARCHAR
);
"""


class NoCredentials(RuntimeError):
    pass


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return int(v)


def connect(path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if read_only and not Path(path).exists():
        raise FileNotFoundError(str(path))
    if not read_only:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for _ in range(20):
        try:
            con = duckdb.connect(str(path), read_only=read_only)
            if not read_only:
                from okxrev.bots import BOT_SCHEMA
                from okxrev.portfolio import SCHEMA as PF_SCHEMA
                con.execute(SCHEMA)
                con.execute(BOT_SCHEMA)
                con.execute(PF_SCHEMA)
            return con
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            if not db._is_lock_error(e):
                raise
            last = e
            time.sleep(0.5)
    raise db.DbBusy(str(last))


def make_client(cfg: Config) -> OkxClient:
    creds = load_credentials(cfg.paths.root)
    if creds is None:
        raise NoCredentials("未設定 OKX API 金鑰：請在 .env 設定 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE（唯讀權限）")
    return OkxClient(cfg.okx, credentials=Credentials(*creds))


def pos_row(d: dict[str, Any]) -> tuple:
    return (
        d.get("posId") or f"{d.get('instId')}-{d.get('cTime')}", int(d["uTime"]), _i(d.get("cTime")),
        d.get("instType"), d.get("instId"), d.get("mgnMode"), d.get("type"), d.get("posSide"), d.get("direction"),
        _f(d.get("lever")), _f(d.get("openAvgPx")), _f(d.get("closeAvgPx")), _f(d.get("openMaxPos")),
        _f(d.get("closeTotalPos")), _f(d.get("pnl")), _f(d.get("realizedPnl")), _f(d.get("fee")),
        _f(d.get("fundingFee")), _f(d.get("liqPenalty")), _f(d.get("pnlRatio")), d.get("ccy"), d.get("uly"),
    )


def fill_row(d: dict[str, Any]) -> tuple:
    return (
        d.get("instType"), str(d["billId"]), d.get("tradeId"), d.get("ordId"), d.get("instId"), d.get("side"),
        d.get("posSide"), _f(d.get("fillPx")), _f(d.get("fillSz")), _f(d.get("fee")), d.get("feeCcy"),
        d.get("execType"), _i(d.get("ts") or d.get("fillTime")),
    )


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    ph = ", ".join(["?"] * len(rows[0]))
    con.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({ph})", rows)
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - before)


def _state(con: duckdb.DuckDBPyConnection, stream: str) -> tuple[int | None, int | None]:
    r = con.execute("SELECT newest_key, oldest_key FROM account_sync_state WHERE stream = ?", [stream]).fetchone()
    return (r[0], r[1]) if r else (None, None)


def _save_state(con: duckdb.DuckDBPyConnection, stream: str, keys: Iterable[int], n_new: int, error: str | None = None) -> None:
    keys = list(keys)
    newest, oldest = _state(con, stream)
    if keys:
        newest = max([k for k in (newest,) if k is not None] + keys)
        oldest = min([k for k in (oldest,) if k is not None] + keys)
    con.execute(
        """
        INSERT INTO account_sync_state (stream, newest_key, oldest_key, n_rows, last_sync_at, last_error)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (stream) DO UPDATE SET newest_key = excluded.newest_key, oldest_key = excluded.oldest_key,
            n_rows = COALESCE(account_sync_state.n_rows, 0) + excluded.n_rows,
            last_sync_at = excluded.last_sync_at, last_error = excluded.last_error
        """,
        [stream, newest, oldest, n_new, db.now_ms(), error],
    )


def sync_positions(client: OkxClient, con: duckdb.DuckDBPyConnection, inst_type: str, since_ms: int, limit: int = 100, max_pages: int = 2000) -> int:
    stream = f"pos:{inst_type}"
    newest, _ = _state(con, stream)
    after: int | None = None
    n_new = 0
    keys: list[int] = []
    for _ in range(max_pages):
        params: dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        rows = client.get_private(POS_PATH, params)
        if not rows:
            break
        n_new += _insert(con, "positions_history", [pos_row(r) for r in rows])
        page_keys = [int(r["uTime"]) for r in rows]
        keys += page_keys
        lo = min(page_keys)
        if len(rows) < limit or lo < since_ms or (newest is not None and lo <= newest):
            break
        nxt = lo + 1
        after = lo if after == nxt else nxt
    _save_state(con, stream, keys, n_new)
    return n_new


def sync_fills(client: OkxClient, con: duckdb.DuckDBPyConnection, inst_type: str, since_ms: int, limit: int = 100, max_pages: int = 5000) -> int:
    stream = f"fills:{inst_type}"
    newest, _ = _state(con, stream)
    after: str | None = None
    n_new = 0
    keys: list[int] = []
    for _ in range(max_pages):
        params: dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
        if after is not None:
            params["after"] = after
        rows = client.get_private(FILLS_PATH, params)
        if not rows:
            break
        n_new += _insert(con, "fills", [fill_row(r) for r in rows])
        bills = [int(r["billId"]) for r in rows]
        keys += bills
        ts_lo = min(int(r.get("ts") or r.get("fillTime") or 0) for r in rows)
        if len(rows) < limit or ts_lo < since_ms or (newest is not None and min(bills) <= newest):
            break
        after = str(min(bills))
    _save_state(con, stream, keys, n_new)
    return n_new


def sync_instruments(client: OkxClient, con: duckdb.DuckDBPyConnection, inst_types: Iterable[str]) -> int:
    rows = []
    for t in inst_types:
        if t == "SPOT":
            continue
        for d in client.get("/api/v5/public/instruments", {"instType": t}):
            rows.append((d["instId"], t, _f(d.get("ctVal")), d.get("ctType"), d.get("ctValCcy"), d.get("settleCcy")))
    _insert(con, "acct_instruments", rows)
    return len(rows)


def run_sync(cfg: Config, client: OkxClient | None = None) -> dict[str, Any]:
    acfg = cfg.account
    types = [t.upper() for t in acfg.get("inst_types", ["SWAP", "FUTURES", "SPOT"])]
    since = db.now_ms() - int(acfg.get("history_days", 92)) * DAY_MS
    limit = int(acfg.get("page_limit", 100))
    own = client is None
    client = client or make_client(cfg)
    out: dict[str, Any] = {"ok": True, "streams": {}}
    try:
        con = connect(cfg.account_db)
        try:
            try:
                sync_instruments(client, con, types)
            except Exception as e:
                log.warning("instrument metadata fetch failed: %s", e)
            for t in types:
                if t in ("SWAP", "FUTURES"):
                    n = sync_positions(client, con, t, since, limit)
                    out["streams"][f"pos:{t}"] = n
                    log.info("positions-history %s: %d new rows", t, n)
                if t == "SPOT":
                    n = sync_fills(client, con, t, since, limit)
                    out["streams"][f"fills:{t}"] = n
                    log.info("fills-history %s: %d new rows", t, n)
            from okxrev import bots
            if acfg.get("sync_bots", True):
                r = bots.sync_bots(client, con)
                out["streams"]["bots"] = r["bots"]
                out["streams"]["bot_orders"] = r["orders"]
                out.setdefault("errors", []).extend(r["errors"])
                log.info("bots: %d seen, %d new, %d new sub-orders", r["n_seen"], r["bots"], r["orders"])
            if acfg.get("sync_bills", True):
                from okxrev import portfolio
                n = portfolio.sync_bills(client, con)
                portfolio.snapshot_balances(client, con)
                res = portfolio.store(con)
                out["streams"]["bills"] = n
                out["portfolio"] = {"running": bool(res["running"]), "runs": len(res["runs"])}
                log.info("bills: %d new; smart portfolio running=%s, finished runs=%d", n, bool(res["running"]), len(res["runs"]))
            if acfg.get("sync_copy", True):
                r = bots.sync_copy(client, con)
                out["streams"]["copy"] = r["positions"]
                out.setdefault("errors", []).extend(r["errors"])
                log.info("copy trading: %d positions seen, %d leaders", r["n_seen"], r["leaders"])
        finally:
            con.close()
    finally:
        if own:
            client.close()
    out["requests"] = client.n_requests
    return out


def derivative_trades(pos: pd.DataFrame, inst: pd.DataFrame) -> pd.DataFrame:
    if pos.empty:
        return pd.DataFrame()
    p = pos.sort_values("u_time").groupby(["pos_id", "c_time"], dropna=False, as_index=False).last()
    p = p.merge(inst, on="inst_id", how="left", suffixes=("", "_i"))
    side = p["direction"].fillna(p["pos_side"]).str.lower()
    side = side.where(side.isin(["long", "short"]))
    sign = np.where(side == "short", -1.0, 1.0)
    entry = p["open_avg_px"].astype(float)
    exit_ = p["close_avg_px"].astype(float)
    ret_px = sign * (exit_ / entry - 1)
    ccy = p["ccy"].fillna("").str.upper()
    to_usd = np.where(ccy.isin(list(STABLE)) | (ccy == ""), 1.0, exit_)
    sz = p["close_total_pos"].astype(float)
    ct_val = p["ct_val"].astype(float)
    inverse = p["ct_type"].fillna("").eq("inverse")
    notional = np.where(inverse, sz * ct_val, sz * ct_val * entry)
    notional = np.where(ct_val.isna(), np.nan, notional)
    t = pd.DataFrame({
        "trade_id": p["pos_id"].astype(str) + "-" + p["c_time"].astype("Int64").astype(str),
        "inst_type": p["inst_type"],
        "inst_id": p["inst_id"],
        "side": side,
        "open_ts": p["c_time"].astype("Int64"),
        "close_ts": p["u_time"].astype("Int64"),
        "entry_px": entry,
        "exit_px": exit_,
        "size": sz,
        "notional_usd": notional,
        "lever": p["lever"].astype(float),
        "mgn_mode": p["mgn_mode"],
        "close_type": p["close_type"].map(CLOSE_TYPES).fillna(p["close_type"]),
        "ret_px": ret_px,
        "gross_pnl_usd": p["pnl"].astype(float) * to_usd,
        "fee_usd": p["fee"].fillna(0).astype(float) * to_usd,
        "funding_usd": p["funding_fee"].fillna(0).astype(float) * to_usd,
        "liq_penalty_usd": p["liq_penalty"].fillna(0).astype(float) * to_usd,
        "pnl_usd": p["realized_pnl"].astype(float) * to_usd,
        "pnl_ccy": ccy,
        "cost_known": True,
    })
    t["pnl_usd"] = t["pnl_usd"].fillna(t["gross_pnl_usd"] + t["fee_usd"] + t["funding_usd"] + t["liq_penalty_usd"])
    return t


def spot_trades(fills: pd.DataFrame, dust_usd: float = 1.0) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    out: list[dict[str, Any]] = []
    f = fills[fills["inst_type"] == "SPOT"].copy()
    f["bill_num"] = pd.to_numeric(f["bill_id"], errors="coerce")
    for inst, g in f.sort_values(["ts", "bill_num"]).groupby("inst_id"):
        parts = str(inst).split("-")
        if len(parts) != 2:
            continue
        base, quote = parts
        priced = quote in STABLE
        qty = cost = 0.0
        cur: dict[str, Any] | None = None
        for r in g.itertuples(index=False):
            px, sz, fee, fccy = float(r.fill_px or 0), float(r.fill_sz or 0), float(r.fee or 0), (r.fee_ccy or "").upper()
            if px <= 0 or sz <= 0:
                continue
            fee_quote = fee if fccy == quote else (fee * px if fccy == base else 0.0)
            if r.side == "buy":
                if cur is None:
                    cur = {"inst_id": inst, "open_ts": int(r.ts), "first_bill": r.bill_id, "buy_qty": 0.0, "buy_val": 0.0,
                           "sell_qty": 0.0, "sell_val": 0.0, "fees": 0.0, "realized": 0.0, "cost_known": True, "n_fills": 0}
                got = sz + (fee if fccy == base else 0.0)
                qty += got
                cost += sz * px - (fee if fccy == quote else 0.0)
                cur["buy_qty"] += sz
                cur["buy_val"] += sz * px
            else:
                if cur is None:
                    cur = {"inst_id": inst, "open_ts": int(r.ts), "first_bill": r.bill_id, "buy_qty": 0.0, "buy_val": 0.0,
                           "sell_qty": 0.0, "sell_val": 0.0, "fees": 0.0, "realized": 0.0, "cost_known": False, "n_fills": 0}
                if qty <= 1e-12 or sz > qty * 1.01:
                    cur["cost_known"] = False
                take = min(sz, qty) if qty > 0 else 0.0
                avg = cost / qty if qty > 0 else 0.0
                cur["realized"] += take * px - take * avg + (fee if fccy == quote else 0.0)
                cost -= avg * take
                qty -= take
                cur["sell_qty"] += sz
                cur["sell_val"] += sz * px
            cur["fees"] += fee_quote
            cur["n_fills"] += 1
            cur["close_ts"] = int(r.ts)
            if r.side == "sell" and (qty <= 1e-12 or qty * px < dust_usd):
                qty = cost = 0.0
                out.append({**cur, "priced": priced})
                cur = None
        if cur is not None and cur["sell_qty"] > 0:
            out.append({**cur, "priced": priced, "open": True})
    if not out:
        return pd.DataFrame()
    s = pd.DataFrame(out)
    entry = s["buy_val"] / s["buy_qty"].replace(0, np.nan)
    exit_ = s["sell_val"] / s["sell_qty"].replace(0, np.nan)
    known = s["cost_known"] & s["priced"]
    return pd.DataFrame({
        "trade_id": "spot-" + s["inst_id"] + "-" + s["first_bill"].astype(str),
        "inst_type": "SPOT",
        "inst_id": s["inst_id"],
        "side": "long",
        "open_ts": s["open_ts"].astype("Int64"),
        "close_ts": s["close_ts"].astype("Int64"),
        "entry_px": entry,
        "exit_px": exit_,
        "size": s["sell_qty"],
        "notional_usd": np.where(s["priced"], s["buy_val"].where(s["buy_val"] > 0, s["sell_val"]), np.nan),
        "lever": 1.0,
        "mgn_mode": "cash",
        "close_type": np.where(s.get("open", pd.Series(False, index=s.index)).fillna(False), "partial", "full"),
        "ret_px": exit_ / entry - 1,
        "gross_pnl_usd": np.where(known, s["realized"] - s["fees"], np.nan),
        "fee_usd": np.where(s["priced"], s["fees"], np.nan),
        "funding_usd": 0.0,
        "liq_penalty_usd": 0.0,
        "pnl_usd": np.where(known, s["realized"], np.nan),
        "pnl_ccy": np.where(s["priced"], "USD", "unpriced"),
        "cost_known": known,
    })


SOURCES = ("manual", "bot", "copy")
SOURCE_LABEL = {"manual": "手動", "bot": "機器人", "copy": "跟單"}


def _df(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    try:
        return con.execute(sql).df()
    except duckdb.CatalogException:
        return pd.DataFrame()


def mark_overlaps(manual: pd.DataFrame, copy_pos: pd.DataFrame) -> pd.Series:
    """Manual positions that are actually copy-trading sub-positions (same inst, open within 60s)."""
    flag = pd.Series(False, index=manual.index)
    if manual.empty or copy_pos.empty:
        return flag
    for c in copy_pos.itertuples(index=False):
        if pd.isna(c.open_time):
            continue
        m = (manual["inst_id"] == c.inst_id) & ((manual["open_ts"].astype("int64") - int(c.open_time)).abs() <= 60_000)
        flag |= m
    return flag


def load_trades(cfg: Config, con: duckdb.DuckDBPyConnection | None = None, sources: tuple[str, ...] = ("manual",)) -> pd.DataFrame:
    from okxrev import bots
    own = con is None
    if own:
        try:
            con = connect(cfg.account_db, read_only=True)
        except FileNotFoundError:
            return pd.DataFrame()
    try:
        pos = con.execute("SELECT * FROM positions_history").df()
        inst = con.execute("SELECT inst_id, ct_val, ct_type FROM acct_instruments").df()
        fills = con.execute("SELECT * FROM fills").df()
        bot_df = bots.load_bots(con) if "bot" in sources else pd.DataFrame()
        copy_df = _df(con, "SELECT * FROM copy_positions")
    finally:
        if own:
            con.close()
    manual = [x for x in (derivative_trades(pos, inst), spot_trades(fills)) if len(x)]
    frames = []
    if manual:
        m = pd.concat(manual, ignore_index=True)
        m["source"] = np.where(mark_overlaps(m, copy_df), "copy_dup", "manual")
        if "manual" in sources:
            frames.append(m[m["source"] == "manual"])
    if "bot" in sources:
        frames.append(bots.bot_trades(bot_df))
    if "copy" in sources:
        frames.append(bots.copy_trades(copy_df))
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame()
    t = pd.concat(frames, ignore_index=True)
    t = t[t["open_ts"].notna() & t["close_ts"].notna()].sort_values("close_ts").reset_index(drop=True)
    t["hold_min"] = (t["close_ts"].astype("int64") - t["open_ts"].astype("int64")) / 60_000
    t["ret_net"] = t["pnl_usd"] / t["notional_usd"]
    t["ret_net"] = t["ret_net"].where(t["notional_usd"] > 0, t["ret_px"])
    return t


def max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    return float((equity - peak).min())


def _stats(t: pd.DataFrame) -> dict[str, Any]:
    p = t["pnl_usd"].dropna()
    wins, losses = p[p > 0], p[p < 0]
    gw, gl = float(wins.sum()), float(-losses.sum())
    eq = p.cumsum().to_numpy()
    return {
        "n": int(len(t)),
        "n_priced": int(len(p)),
        "pnl": float(p.sum()),
        "win_rate": float((p > 0).mean()) if len(p) else None,
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "payoff": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        "profit_factor": gw / gl if gl > 0 else None,
        "expectancy": float(p.mean()) if len(p) else None,
        "fees": float(t["fee_usd"].fillna(0).sum()),
        "funding": float(t["funding_usd"].fillna(0).sum()),
        "max_drawdown": max_drawdown(eq),
        "best": float(p.max()) if len(p) else None,
        "worst": float(p.min()) if len(p) else None,
        "median_hold_min": float(t["hold_min"].median()) if len(t) else None,
        "liquidations": int(t["close_type"].isin(["liquidation", "partial_liquidation"]).sum()),
        "unrealized": float(t["unrealized_usd"].fillna(0).sum()) if "unrealized_usd" in t else 0.0,
        "n_open": int(t["is_open"].fillna(False).astype(bool).sum()) if "is_open" in t else 0,
    }


def filter_trades(t: pd.DataFrame, since_ms: int | None = None, until_ms: int | None = None,
                  inst_type: str | None = None, inst: str | None = None, side: str | None = None,
                  sources: tuple[str, ...] | None = None) -> pd.DataFrame:
    if t.empty:
        return t
    m = pd.Series(True, index=t.index)
    if sources is not None and "source" in t:
        m &= t["source"].isin(sources)
    if since_ms is not None:
        m &= t["close_ts"].astype("int64") >= since_ms
    if until_ms is not None:
        m &= t["close_ts"].astype("int64") < until_ms
    if inst_type:
        m &= t["inst_type"] == inst_type.upper()
    if inst:
        m &= t["inst_id"].str.contains(inst.upper(), regex=False)
    if side:
        m &= t["side"] == side
    return t[m]


def summarize_trades(t: pd.DataFrame, tz_hours: int = 8) -> dict[str, Any]:
    if t.empty:
        return {"overall": {"n": 0}}
    t = t.sort_values("close_ts")
    p = t["pnl_usd"].fillna(0.0)
    local = pd.to_datetime(t["close_ts"].astype("int64") + tz_hours * 3_600_000, unit="ms")
    day = local.dt.strftime("%Y-%m-%d")
    month = local.dt.strftime("%Y-%m")
    eq = p.cumsum()
    daily = p.groupby(day).agg(["sum", "count"]).reset_index()
    daily.columns = ["day", "pnl", "n"]
    monthly = []
    for m, g in t.groupby(month):
        s = _stats(g)
        monthly.append({"month": m, **{k: s[k] for k in ("n", "pnl", "win_rate", "profit_factor", "fees", "funding", "max_drawdown")}})

    def grp(col: str, top: int | None = None) -> list[dict[str, Any]]:
        rows = []
        for k, g in t.groupby(col):
            s = _stats(g)
            rows.append({"key": k, **{x: s[x] for x in ("n", "pnl", "win_rate", "payoff", "profit_factor", "fees", "median_hold_min", "unrealized", "n_open")}})
        rows.sort(key=lambda r: r["pnl"])
        return rows if top is None else rows[:top] + [r for r in rows[-top:] if r not in rows[:top]]

    return {
        "overall": _stats(t),
        "first_ts": int(t["open_ts"].min()),
        "last_ts": int(t["close_ts"].max()),
        "equity": {"t": (t["close_ts"].astype("int64") // 1000).tolist(), "v": eq.round(4).tolist()},
        "daily": daily.to_dict("records"),
        "monthly": monthly,
        "by_inst": grp("inst_id", top=25),
        "by_side": grp("side"),
        "by_type": grp("inst_type"),
        "by_source": [{**r, "label": SOURCE_LABEL.get(r["key"], r["key"])} for r in grp("source")] if "source" in t else [],
        "tz_hours": tz_hours,
    }


def status(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {"credentials": load_credentials(cfg.paths.root) is not None, "db": str(cfg.account_db), "exists": cfg.account_db.exists()}
    if not out["exists"]:
        return out
    con = connect(cfg.account_db, read_only=True)
    try:
        out["positions"] = int(con.execute("SELECT COUNT(*) FROM positions_history").fetchone()[0])
        out["fills"] = int(con.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
        out["streams"] = con.execute("SELECT * FROM account_sync_state ORDER BY stream").df().to_dict("records")
        b = _df(con, "SELECT COUNT(*) AS n, COALESCE(SUM(total_pnl), 0) AS pnl FROM bots")
        out["bots"] = int(b["n"].iloc[0]) if len(b) else 0
        c = _df(con, "SELECT COUNT(*) AS n FROM copy_positions")
        out["copy_positions"] = int(c["n"].iloc[0]) if len(c) else 0
        out["copy_leaders"] = _df(con, "SELECT unique_code, nick_name, begin_time, copy_total_amt, copy_total_pnl FROM copy_leaders").to_dict("records")
    finally:
        con.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.account")
    ap.add_argument("--config", default=None)
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "account")
    if args.sync:
        try:
            r = run_sync(cfg)
        except NoCredentials as e:
            print(e)
            return 2
        print(f"sync done: {r['streams']} ({r['requests']} requests)")
    st = status(cfg)
    print(f"credentials: {'ok' if st['credentials'] else 'missing'}  db: {st['db']}")
    if st.get("exists"):
        print(f"positions_history rows: {st['positions']}, fills rows: {st['fills']}")
        for s in st["streams"]:
            print(f"  {s['stream']:<14} oldest {ms_to_str(s['oldest_key']) if 'pos' in s['stream'] else s['oldest_key']}  "
                  f"newest {ms_to_str(s['newest_key']) if 'pos' in s['stream'] else s['newest_key']}  last sync {ms_to_str(s['last_sync_at'])}")
        print(f"bots: {st['bots']}, copy positions: {st['copy_positions']}, copy leaders: {[x['nick_name'] for x in st['copy_leaders']]}")
        t = load_trades(cfg, sources=SOURCES)
        if len(t):
            for name, sub in [("all", t)] + [(s, t[t["source"] == s]) for s in SOURCES]:
                if sub.empty:
                    continue
                s = summarize_trades(sub)["overall"]
                print(f"{name:<7} trades: {s['n']:>4}  pnl {s['pnl']:+9.2f} USD (unrealized {s['unrealized']:+.2f})  win {s['win_rate'] or 0:.1%}  "
                      f"PF {s['profit_factor'] or 0:.2f}  maxDD {s['max_drawdown']:.2f}  fees {s['fees']:.2f}  funding {s['funding']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
