"""Smart-portfolio (智能組合) reconstruction from the trading-account bill flow.

OKX exposes no API for 智能組合, but every spot fill it makes appears in /account/bills-archive with a running
`bal` that is tracked per strategy. Linking bills whose pre-balance (bal - balChg) matches a previous bill's bal
splits one coin's flow into separate chains (manual balance, each bot). Chains that end at the coin's current
`stgyEq` belong to the running portfolio; chains that start and end at ~0 inside bot-placed orders are finished runs.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.okx_client import OkxClient

log = logging.getLogger("okxrev.portfolio")

BILLS_PATH = "/api/v5/account/bills-archive"
BALANCE_PATH = "/api/v5/account/balance"
LINK_TOL = 0.015
END_TOL = 0.01
DUST_USD = 0.5
RUN_GAP_MS = 5 * 60_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    bill_id    VARCHAR PRIMARY KEY,
    ts         BIGINT,
    ccy        VARCHAR,
    inst_type  VARCHAR,
    inst_id    VARCHAR,
    type       VARCHAR,
    sub_type   VARCHAR,
    bal_chg    DOUBLE,
    bal        DOUBLE,
    sz         DOUBLE,
    px         DOUBLE,
    fee        DOUBLE,
    pnl        DOUBLE,
    ord_id     VARCHAR,
    cl_ord_id  VARCHAR,
    exec_type  VARCHAR
);

CREATE TABLE IF NOT EXISTS strategy_balances (
    ts      BIGINT,
    ccy     VARCHAR,
    stgy_eq DOUBLE,
    eq      DOUBLE,
    eq_usd  DOUBLE,
    PRIMARY KEY (ts, ccy)
);
"""


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def bill_row(d: dict[str, Any]) -> tuple:
    return (str(d["billId"]), int(d["ts"]), d.get("ccy"), d.get("instType") or None, d.get("instId") or None,
            str(d.get("type")), str(d.get("subType")), _f(d.get("balChg")), _f(d.get("bal")), _f(d.get("sz")),
            _f(d.get("px")), _f(d.get("fee")), _f(d.get("pnl")), d.get("ordId") or None, d.get("clOrdId") or None,
            d.get("execType") or None)


def sync_bills(client: OkxClient, con: duckdb.DuckDBPyConnection, max_pages: int = 500) -> int:
    con.execute(SCHEMA)
    newest = con.execute("SELECT MAX(CAST(bill_id AS HUGEINT)) FROM bills").fetchone()[0]
    after: str | None = None
    n_new = 0
    for _ in range(max_pages):
        p: dict[str, Any] = {"limit": "100"}
        if after:
            p["after"] = after
        rows = client.get_private(BILLS_PATH, p)
        if not rows:
            break
        before = con.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        con.executemany("INSERT OR REPLACE INTO bills VALUES (" + ", ".join(["?"] * 16) + ")", [bill_row(r) for r in rows])
        n_new += con.execute("SELECT COUNT(*) FROM bills").fetchone()[0] - before
        lo = min(int(r["billId"]) for r in rows)
        if len(rows) < 100 or (newest is not None and lo <= int(newest)):
            break
        after = str(lo)
    return int(n_new)


def snapshot_balances(client: OkxClient, con: duckdb.DuckDBPyConnection, now: int | None = None) -> pd.DataFrame:
    con.execute(SCHEMA)
    now = now or db.now_ms()
    rows = []
    for acct in client.get_private(BALANCE_PATH, {}):
        for d in acct.get("details", []):
            rows.append((now, d.get("ccy"), _f(d.get("stgyEq")) or 0.0, _f(d.get("eq")) or 0.0, _f(d.get("eqUsd")) or 0.0))
    if rows:
        con.executemany("INSERT OR REPLACE INTO strategy_balances VALUES (?, ?, ?, ?, ?)", rows)
    return pd.DataFrame(rows, columns=["ts", "ccy", "stgy_eq", "eq", "eq_usd"])


def build_chains(fills: pd.DataFrame) -> pd.DataFrame:
    """Assign a chain id to every base-currency spot fill of one coin by linking pre-balance to a prior bal."""
    f = fills.sort_values(["ts", "bill_id"], key=lambda s: s if s.name == "ts" else s.astype("int64")).copy()
    open_bal: dict[int, float] = {}
    chain = []
    nxt = 0
    for r in f.itertuples(index=False):
        pre = r.bal - r.bal_chg
        best, err = None, None
        for cid, b in open_bal.items():
            scale = max(abs(b), abs(pre), 1e-12)
            e = abs(b - pre) / scale
            if e <= LINK_TOL and (err is None or e < err):
                best, err = cid, e
        if best is None:
            best = nxt
            nxt += 1
        open_bal[best] = r.bal
        chain.append(best)
    f["chain"] = chain
    return f


def _chain_pnl(ch: pd.DataFrame, quote: pd.DataFrame, px_now: float | None, qty_now: float | None) -> dict[str, Any]:
    ch = ch.sort_values("ts")
    first = ch.iloc[0]
    qty0 = max(first["bal"] - first["bal_chg"], 0.0)
    px0 = float(first["px"])
    qty, cost = qty0, qty0 * px0
    realized = 0.0
    q = quote[quote["ord_id"].isin(ch["ord_id"])].groupby("ord_id")["bal_chg"].sum()
    fees = 0.0
    buys = sells = 0.0
    for ord_id, g in ch.groupby("ord_id", sort=False):
        dq = float(g["bal_chg"].sum())
        usd = float(q.get(ord_id, 0.0))
        fees += float(g["fee"].fillna(0).abs().sum() * g["px"].iloc[0])
        if dq > 0:
            qty += dq
            cost += -usd
            buys += -usd
        elif dq < 0 and qty > 0:
            take = min(-dq, qty)
            avg = cost / qty
            realized += usd - avg * take
            cost -= avg * take
            qty -= take
            sells += usd
    end_qty = qty_now if qty_now is not None else float(ch.iloc[-1]["bal"])
    unreal = (end_qty * px_now - cost * (end_qty / qty if qty > 0 else 0.0)) if px_now and end_qty > 0 else 0.0
    return {"qty_open": qty0, "px_open": px0, "open_value": qty0 * px0, "cost_from_history": qty0 > 0,
            "qty_end": end_qty, "avg_cost": cost / qty if qty > 0 else None,
            "realized": realized, "unrealized": unreal, "buys": buys, "sells": sells, "fees": fees,
            "start": int(first["ts"]), "end": int(ch["ts"].max()), "n_fills": int(len(ch)), "n_orders": int(ch["ord_id"].nunique())}


def _price_now(snap: pd.DataFrame, ccy: str) -> float | None:
    r = snap[snap["ccy"] == ccy]
    if r.empty or not r["eq"].iloc[0]:
        return None
    return float(r["eq_usd"].iloc[0] / r["eq"].iloc[0])


def reconstruct(bills: pd.DataFrame, snap: pd.DataFrame, bot_windows: list[tuple[str, int, int]] | None = None) -> dict[str, Any]:
    """Return running portfolio holdings and finished portfolio runs."""
    if bills.empty:
        return {"running": None, "runs": []}
    b = bills.copy()
    b["cl_ord_id"] = b["cl_ord_id"].where(~b["cl_ord_id"].isin(["", "nan", "None"]), None)
    spot = b[(b["type"] == "2") & (b["inst_type"] == "SPOT") & b["inst_id"].notna()]
    spot = spot[spot["inst_id"].str.endswith("-USDT")]
    base = spot[spot["ccy"] == spot["inst_id"].str.split("-").str[0]]
    quote = spot[spot["ccy"] == "USDT"]
    snap = snap if snap is not None else pd.DataFrame(columns=["ccy", "stgy_eq", "eq", "eq_usd"])
    stgy = {r.ccy: r.stgy_eq for r in snap.itertuples(index=False) if r.stgy_eq and r.stgy_eq > 0}
    windows = bot_windows or []

    def in_known_bot(inst: str, ts: int) -> bool:
        return any(inst == i and s - 60_000 <= ts <= e + 60_000 for i, s, e in windows)

    holdings: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    for ccy, fills in base.groupby("ccy"):
        ch = build_chains(fills)
        px_now = _price_now(snap, ccy)
        target = stgy.get(ccy)
        best_cid, best_err = None, None
        if target:
            for cid, g in ch.groupby("chain"):
                last = float(g.sort_values("ts").iloc[-1]["bal"])
                e = abs(last - target) / max(target, 1e-12)
                if e <= END_TOL and (best_err is None or e < best_err) and g["cl_ord_id"].notna().any():
                    best_cid, best_err = cid, e
        for cid, g in ch.groupby("chain"):
            g = g.sort_values("ts")
            if cid == best_cid:
                holdings.append({"ccy": ccy, "chain": int(cid), **_chain_pnl(g, quote, px_now, target), "px_now": px_now,
                                 "value_now": target * px_now if px_now else None})
                continue
            if not g["cl_ord_id"].notna().all():
                continue
            if all(in_known_bot(r.inst_id, int(r.ts)) for r in g.itertuples(index=False)):
                continue
            first, last = g.iloc[0], g.iloc[-1]
            peak = float(g["bal"].max())
            start_zero = (first["bal"] - first["bal_chg"]) <= peak * END_TOL
            end_zero = last["bal"] <= peak * END_TOL
            if start_zero and end_zero and peak * float(g["px"].max()) >= DUST_USD:
                finished.append({"ccy": ccy, "inst_id": f"{ccy}-USDT", **_chain_pnl(g, quote, None, 0.0)})

    running = None
    if holdings:
        h = pd.DataFrame(holdings)
        cash = stgy.get("USDT", 0.0)
        running = {
            "coins": h.to_dict("records"), "cash_usdt": cash,
            "value_now": float(h["value_now"].fillna(0).sum() + cash),
            "realized": float(h["realized"].sum()), "unrealized": float(h["unrealized"].sum()),
            "open_value": float(h["open_value"].sum()), "fees": float(h["fees"].sum()),
            "start": int(h["start"].min()), "last_trade": int(h["end"].max()),
            "n_orders": int(h["n_orders"].sum()),
            "history_start": int(b["ts"].min()),
            "cost_note": "帳單流水最早只到 " + pd.to_datetime(int(b["ts"].min()), unit="ms").strftime("%Y-%m-%d")
                         + "；在那之前已持有的部分，以當時第一筆成交價當作成本，實際成本與更早的損益無法取得",
        }
        running["pnl"] = running["realized"] + running["unrealized"]

    runs: list[dict[str, Any]] = []
    for r in sorted(finished, key=lambda x: x["start"]):
        if runs and r["start"] - runs[-1]["start"] <= RUN_GAP_MS:
            runs[-1]["coins"].append(r)
        else:
            runs.append({"start": r["start"], "coins": [r]})
    for run in runs:
        c = run["coins"]
        run["end"] = max(x["end"] for x in c)
        run["realized"] = float(sum(x["realized"] for x in c))
        run["buys"] = float(sum(x["buys"] for x in c))
        run["fees"] = float(sum(x["fees"] for x in c))
        run["ccys"] = [x["ccy"] for x in c]
    return {"running": running, "runs": runs}


def load(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, pd.DataFrame]:
    try:
        bills = con.execute("SELECT * FROM bills").df()
        snap = con.execute("SELECT * FROM strategy_balances WHERE ts = (SELECT MAX(ts) FROM strategy_balances)").df()
    except duckdb.CatalogException:
        return pd.DataFrame(), pd.DataFrame()
    return bills, snap


def bot_windows(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int, int]]:
    try:
        rows = con.execute("SELECT inst_id, c_time, COALESCE(stop_time, c_time) FROM bots WHERE algo_type NOT LIKE 'smart_portfolio%'").fetchall()
    except duckdb.CatalogException:
        return []
    return [(r[0], int(r[1]), int(r[2])) for r in rows if r[1] is not None]


def as_bot_rows(res: dict[str, Any], now: int | None = None) -> list[dict[str, Any]]:
    """Portfolio running + finished runs in the shape of `bots` table rows."""
    now = now or db.now_ms()
    out = []
    r = res.get("running")
    if r:
        out.append({
            "algo_id": f"portfolio-running-{r['start']}", "algo_type": "smart_portfolio", "inst_type": "SPOT",
            "inst_id": "/".join(c["ccy"] for c in r["coins"]), "direction": "long", "lever": 1.0,
            "investment": r["open_value"] + max(0.0, sum(c["buys"] for c in r["coins"]) - sum(c["sells"] for c in r["coins"])),
            "c_time": r["start"], "stop_time": None, "state": "running", "total_pnl": r["pnl"],
            "grid_profit": None, "float_profit": r["unrealized"], "fee": -r["fees"], "funding_fee": 0.0,
            "pnl_ratio": r["pnl"] / r["open_value"] if r["open_value"] else None, "min_px": None, "max_px": None,
            "grid_num": None, "run_px": None, "trade_num": r["n_orders"], "stop_type": "", "copy_type": "",
            "raw": json.dumps({"history_start": r["history_start"], "cash_usdt": r["cash_usdt"], "value_now": r["value_now"],
                               "realized": r["realized"], "unrealized": r["unrealized"], "cost_note": r["cost_note"],
                               "coins": [{k: c[k] for k in ("ccy", "qty_end", "avg_cost", "px_now", "value_now", "realized", "unrealized",
                                                            "qty_open", "px_open", "n_orders", "start")} for c in r["coins"]]}),
            "unrealized": r["unrealized"],
        })
    for run in res.get("runs", []):
        out.append({
            "algo_id": f"portfolio-{run['start']}", "algo_type": "smart_portfolio", "inst_type": "SPOT",
            "inst_id": "/".join(run["ccys"]), "direction": "long", "lever": 1.0, "investment": run["buys"],
            "c_time": run["start"], "stop_time": run["end"], "state": "stopped", "total_pnl": run["realized"],
            "grid_profit": None, "float_profit": 0.0, "fee": -run["fees"], "funding_fee": 0.0,
            "pnl_ratio": run["realized"] / run["buys"] if run["buys"] else None, "min_px": None, "max_px": None,
            "grid_num": None, "run_px": None, "trade_num": sum(c["n_orders"] for c in run["coins"]), "stop_type": "1", "copy_type": "",
            "raw": "{}", "unrealized": 0.0,
        })
    return out


BOT_COLS = ["algo_id", "algo_type", "inst_type", "inst_id", "direction", "lever", "investment", "c_time", "stop_time", "state",
            "total_pnl", "grid_profit", "float_profit", "fee", "funding_fee", "pnl_ratio", "min_px", "max_px", "grid_num",
            "run_px", "trade_num", "stop_type", "copy_type", "raw", "unrealized"]


def store(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    bills, snap = load(con)
    res = reconstruct(bills, snap, bot_windows(con))
    rows = as_bot_rows(res)
    con.execute("DELETE FROM bots WHERE algo_type = 'smart_portfolio'")
    if rows:
        con.executemany(f"INSERT OR REPLACE INTO bots ({', '.join(BOT_COLS)}) VALUES ({', '.join(['?'] * len(BOT_COLS))})",
                        [tuple(r[c] for c in BOT_COLS) for r in rows])
    return res
