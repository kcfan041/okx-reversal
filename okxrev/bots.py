from __future__ import annotations

import json
import logging
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, ms_to_str
from okxrev.okx_client import OkxClient, OkxError

log = logging.getLogger("okxrev.bots")

BOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    algo_id      VARCHAR PRIMARY KEY,
    algo_type    VARCHAR,
    inst_type    VARCHAR,
    inst_id      VARCHAR,
    direction    VARCHAR,
    lever        DOUBLE,
    investment   DOUBLE,
    c_time       BIGINT,
    stop_time    BIGINT,
    state        VARCHAR,
    total_pnl    DOUBLE,
    grid_profit  DOUBLE,
    float_profit DOUBLE,
    fee          DOUBLE,
    funding_fee  DOUBLE,
    pnl_ratio    DOUBLE,
    min_px       DOUBLE,
    max_px       DOUBLE,
    grid_num     INTEGER,
    run_px       DOUBLE,
    trade_num    INTEGER,
    stop_type    VARCHAR,
    copy_type    VARCHAR,
    raw          VARCHAR,
    unrealized   DOUBLE
);
ALTER TABLE bots ADD COLUMN IF NOT EXISTS unrealized DOUBLE;

CREATE TABLE IF NOT EXISTS bot_orders (
    algo_id   VARCHAR NOT NULL,
    ord_id    VARCHAR NOT NULL,
    inst_id   VARCHAR,
    side      VARCHAR,
    ord_type  VARCHAR,
    strategy_order_type VARCHAR,
    px        DOUBLE,
    avg_px    DOUBLE,
    sz        DOUBLE,
    ct_val    DOUBLE,
    fee       DOUBLE,
    pnl       DOUBLE,
    c_time    BIGINT,
    u_time    BIGINT,
    PRIMARY KEY (algo_id, ord_id)
);

CREATE TABLE IF NOT EXISTS copy_positions (
    sub_pos_id   VARCHAR PRIMARY KEY,
    inst_type    VARCHAR,
    inst_id      VARCHAR,
    lead_code    VARCHAR,
    side         VARCHAR,
    lever        DOUBLE,
    open_avg_px  DOUBLE,
    close_avg_px DOUBLE,
    sub_pos      DOUBLE,
    open_time    BIGINT,
    close_time   BIGINT,
    pnl          DOUBLE,
    fee          DOUBLE,
    funding_fee  DOUBLE,
    open_ord_id  VARCHAR,
    close_ord_id VARCHAR,
    state        VARCHAR
);

CREATE TABLE IF NOT EXISTS copy_leaders (
    unique_code  VARCHAR PRIMARY KEY,
    nick_name    VARCHAR,
    begin_time   BIGINT,
    copy_total_amt DOUBLE,
    copy_total_pnl DOUBLE,
    last_seen    BIGINT
);
"""

GRID_TYPES = ("grid", "contract_grid")
DCA_TYPES = ("contract_dca", "spot_dca")
TYPE_LABEL = {"grid": "現貨網格", "contract_grid": "合約網格", "contract_dca": "合約 DCA", "spot_dca": "現貨 DCA",
              "recurring": "定投", "signal": "訊號機器人", "smart_portfolio": "智能組合"}
STOP_TYPE = {"1": "手動停止／賣出", "2": "停止但保留倉位", "3": "止盈", "4": "止損", "5": "強平", "6": "到期"}


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        return None if v in (None, "") else int(v)
    except (TypeError, ValueError):
        return None


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(BOT_SCHEMA)


def bot_row(d: dict[str, Any]) -> tuple:
    stop = _i(d.get("stopTime"))
    if stop is None:
        stops = [_i(p.get("triggerTime")) for p in d.get("triggerParams") or [] if p.get("triggerAction") == "stop"]
        stop = next((s for s in stops if s), None) or (_i(d.get("uTime")) if d.get("state") == "stopped" else None)
    return (
        d["algoId"], d.get("algoOrdType"), d.get("instType") or ("SWAP" if str(d.get("instId", "")).endswith("-SWAP") else "SPOT"),
        d.get("instId"), d.get("direction") or ("long" if d.get("algoOrdType") in ("grid", "spot_dca", "recurring") else None),
        _f(d.get("lever")) or 1.0, _f(d.get("investment")) or _f(d.get("investmentAmt")) or _f(d.get("sz")),
        _i(d.get("cTime")), stop, d.get("state"), _f(d.get("totalPnl")) or _f(d.get("pnl")) or 0.0,
        _f(d.get("gridProfit")) or _f(d.get("arbitragePnL")), _f(d.get("floatProfit")), _f(d.get("fee")),
        _f(d.get("fundingFee")) or _f(d.get("totalFundingFee")), _f(d.get("pnlRatio")),
        _f(d.get("minPx")), _f(d.get("maxPx")), _i(d.get("gridNum")), _f(d.get("runPx")), _i(d.get("tradeNum")),
        str(d.get("stopType") or d.get("cancelType") or ""), str(d.get("copyType") or ""),
        json.dumps(d, ensure_ascii=False),
        (_f(d.get("floatProfit")) or 0.0) if d.get("state") not in ("stopped",) else 0.0,
    )


def sub_row(d: dict[str, Any]) -> tuple:
    return (
        d["algoId"], d["ordId"], d.get("instId"), d.get("side"), d.get("ordType"), d.get("strategyOrderType"),
        _f(d.get("px")), _f(d.get("avgPx")), _f(d.get("accFillSz")) or _f(d.get("sz")), _f(d.get("ctVal")),
        _f(d.get("fee")), _f(d.get("pnl")), _i(d.get("cTime")), _i(d.get("uTime")),
    )


def copy_row(d: dict[str, Any], state: str) -> tuple:
    return (
        d["subPosId"], d.get("instType"), d.get("instId"), d.get("uniqueCode"),
        d.get("posSide") if d.get("posSide") in ("long", "short") else ("long" if d.get("side") == "buy" else "short"),
        _f(d.get("lever")), _f(d.get("openAvgPx")), _f(d.get("closeAvgPx")), _f(d.get("subPos")),
        _i(d.get("openTime")), _i(d.get("closeTime")), _f(d.get("pnl")), _f(d.get("fee")), _f(d.get("fundingFee")),
        d.get("openOrdId"), d.get("closeOrdId"), state,
    )


def _upsert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    ph = ", ".join(["?"] * len(rows[0]))
    con.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({ph})", rows)
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - before)


def _paged(client: OkxClient, path: str, params: dict[str, Any], key: str, max_pages: int = 50, limit: int = 100) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(max_pages):
        p = {**params, "limit": str(limit)}
        if after:
            p["after"] = after
        rows = client.get_private(path, p)
        if not rows:
            break
        out += rows
        if len(rows) < limit:
            break
        after = str(rows[-1][key])
    return out


def _safe(fn, what: str, errors: list[str]) -> Any:
    try:
        return fn()
    except OkxError as e:
        log.warning("%s: %s", what, e)
        errors.append(f"{what}: {e.code} {e.msg}")
    except Exception as e:
        log.warning("%s: %s", what, e)
        errors.append(f"{what}: {type(e).__name__}")
    return None


def sync_bots(client: OkxClient, con: duckdb.DuckDBPyConnection, with_orders: bool = True) -> dict[str, Any]:
    ensure_schema(con)
    errors: list[str] = []
    out: dict[str, Any] = {"bots": 0, "orders": 0}
    bots: list[dict[str, Any]] = []
    for t in GRID_TYPES:
        for path in ("/api/v5/tradingBot/grid/orders-algo-history", "/api/v5/tradingBot/grid/orders-algo-pending"):
            r = _safe(lambda: _paged(client, path, {"algoOrdType": t}, "algoId"), f"{path.rsplit('/', 1)[-1]} {t}", errors)
            bots += r or []
    for path in ("/api/v5/tradingBot/dca/history-list", "/api/v5/tradingBot/dca/ongoing-list"):
        for t in DCA_TYPES:
            r = _safe(lambda: _paged(client, path, {"algoOrdType": t}, "algoId"), f"{path.rsplit('/', 1)[-1]} {t}", errors)
            bots += r or []
    for path in ("/api/v5/tradingBot/recurring/orders-algo-history", "/api/v5/tradingBot/recurring/orders-algo-pending"):
        r = _safe(lambda: _paged(client, path, {}, "algoId"), path.rsplit("/", 2)[-2] + " recurring", errors)
        bots += [{**x, "algoOrdType": x.get("algoOrdType") or "recurring"} for x in (r or [])]
    for path in ("/api/v5/tradingBot/signal/orders-algo-history", "/api/v5/tradingBot/signal/orders-algo-pending"):
        r = _safe(lambda: _paged(client, path, {"algoOrdType": "contract"}, "algoId"), "signal", errors)
        bots += [{**x, "algoOrdType": "signal"} for x in (r or [])]

    known = dict(con.execute("SELECT algo_id, state FROM bots").fetchall())
    details: list[dict[str, Any]] = []
    for b in bots:
        if b.get("algoOrdType") in GRID_TYPES and (known.get(b["algoId"]) != "stopped" or b.get("state") != "stopped"):
            d = _safe(lambda: client.get_private("/api/v5/tradingBot/grid/orders-algo-details",
                                                  {"algoOrdType": b["algoOrdType"], "algoId": b["algoId"]}), "grid details", errors)
            if d:
                b = {**b, **{k: v for k, v in d[0].items() if v not in ("", None)}}
        details.append(b)
    out["bots"] = _upsert(con, "bots", [bot_row(b) for b in details])
    if with_orders:
        have = {r[0] for r in con.execute("SELECT DISTINCT algo_id FROM bot_orders").fetchall()}
        for b in details:
            if b.get("algoOrdType") not in GRID_TYPES:
                continue
            if b["algoId"] in have and b.get("state") == "stopped" and known.get(b["algoId"]) == "stopped":
                continue
            r = _safe(lambda: _paged(client, "/api/v5/tradingBot/grid/sub-orders",
                                     {"algoOrdType": b["algoOrdType"], "algoId": b["algoId"], "type": "filled"}, "ordId", max_pages=100),
                      "grid sub-orders", errors)
            out["orders"] += _upsert(con, "bot_orders", [sub_row(x) for x in (r or [])])
    out["n_seen"] = len(details)
    out["errors"] = errors
    return out


def sync_copy(client: OkxClient, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    ensure_schema(con)
    errors: list[str] = []
    rows: list[tuple] = []
    for t in ("SWAP", "SPOT"):
        r = _safe(lambda: _paged(client, "/api/v5/copytrading/subpositions-history", {"instType": t}, "subPosId"), f"copy history {t}", errors)
        rows += [copy_row(x, "closed") for x in (r or [])]
        r = _safe(lambda: _paged(client, "/api/v5/copytrading/current-subpositions", {"instType": t}, "subPosId"), f"copy current {t}", errors)
        rows += [copy_row(x, "open") for x in (r or [])]
    n = _upsert(con, "copy_positions", rows)
    leaders = _safe(lambda: client.get_private("/api/v5/copytrading/current-lead-traders", {"instType": "SWAP"}), "copy leaders", errors) or []
    now = db.now_ms()
    _upsert(con, "copy_leaders", [(x.get("uniqueCode"), x.get("nickName"), _i(x.get("beginCopyTime")), _f(x.get("copyTotalAmt")),
                                   _f(x.get("copyTotalPnl")), now) for x in leaders if x.get("uniqueCode")])
    return {"positions": n, "n_seen": len(rows), "leaders": len(leaders), "errors": errors}


def load_bots(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    try:
        return con.execute("SELECT * EXCLUDE (raw), CASE WHEN algo_type = 'smart_portfolio' THEN raw END AS raw FROM bots ORDER BY c_time").df()
    except duckdb.CatalogException:
        return pd.DataFrame()


def bot_trades(b: pd.DataFrame) -> pd.DataFrame:
    """One row per bot in the same shape as account.load_trades, so totals can include bots."""
    if b.empty:
        return pd.DataFrame()
    b = b[b["c_time"].notna()].copy()
    end = b["stop_time"].fillna(db.now_ms())
    lever = b["lever"].fillna(1.0)
    inv = b["investment"].astype(float)
    return pd.DataFrame({
        "trade_id": "bot-" + b["algo_id"].astype(str),
        "inst_type": b["inst_type"], "inst_id": b["inst_id"], "side": b["direction"].where(b["direction"].isin(["long", "short"])),
        "open_ts": b["c_time"].astype("Int64"), "close_ts": end.astype("Int64"),
        "entry_px": b["run_px"], "exit_px": np.nan, "size": np.nan,
        "notional_usd": inv * lever, "lever": lever, "mgn_mode": "bot",
        "close_type": np.where(b["state"] == "stopped", b["stop_type"].map(STOP_TYPE).fillna("stopped"), "running"),
        "ret_px": np.nan, "gross_pnl_usd": b["total_pnl"] - b["fee"].fillna(0) - b["funding_fee"].fillna(0),
        "fee_usd": b["fee"].fillna(0.0), "funding_usd": b["funding_fee"].fillna(0.0), "liq_penalty_usd": 0.0,
        "pnl_usd": b["total_pnl"].astype(float), "pnl_ccy": "USDT", "cost_known": True,
        "source": "bot", "bot_type": b["algo_type"],
        "unrealized_usd": b["unrealized"].fillna(0.0).astype(float) if "unrealized" in b else 0.0,
        "is_open": (b["state"] != "stopped").to_numpy(),
    })


def copy_trades(c: pd.DataFrame) -> pd.DataFrame:
    if c.empty:
        return pd.DataFrame()
    c = c[c["state"] == "closed"]
    if c.empty:
        return pd.DataFrame()
    s = np.where(c["side"] == "short", -1.0, 1.0)
    return pd.DataFrame({
        "trade_id": "copy-" + c["sub_pos_id"].astype(str), "inst_type": c["inst_type"], "inst_id": c["inst_id"], "side": c["side"],
        "open_ts": c["open_time"].astype("Int64"), "close_ts": c["close_time"].astype("Int64"),
        "entry_px": c["open_avg_px"], "exit_px": c["close_avg_px"], "size": c["sub_pos"], "notional_usd": np.nan,
        "lever": c["lever"], "mgn_mode": "copy", "close_type": "full", "ret_px": s * (c["close_avg_px"] / c["open_avg_px"] - 1),
        "gross_pnl_usd": c["pnl"], "fee_usd": c["fee"].fillna(0), "funding_usd": c["funding_fee"].fillna(0), "liq_penalty_usd": 0.0,
        "pnl_usd": c["pnl"].fillna(0) + c["fee"].fillna(0) + c["funding_fee"].fillna(0), "pnl_ccy": "USDT", "cost_known": True,
        "source": "copy", "lead_code": c["lead_code"],
    })


def _range_ctx(g: pd.DataFrame, lo: float | None, hi: float | None) -> dict[str, Any]:
    if g.empty:
        return {}
    c = g["close"].to_numpy(float)
    first, last = float(g["open"].iloc[0]), float(c[-1])
    out = {"px_start": first, "px_end": last, "move": last / first - 1,
           "max_up": float(g["high"].max()) / first - 1, "max_down": float(g["low"].min()) / first - 1}
    if lo and hi and hi > lo:
        inside = (g["close"] >= lo) & (g["close"] <= hi)
        out["in_range_rate"] = float(inside.mean())
        out["broke"] = "down" if g["low"].min() < lo else ("up" if g["high"].max() > hi else None)
        bars = np.flatnonzero(~inside.to_numpy())
        out["first_exit_min"] = float(bars[0] * 5) if len(bars) else None
        out["range_width"] = hi / lo - 1
    ret = np.diff(np.log(c[~np.isnan(c)])) if len(c) > 2 else np.array([])
    out["vol_5m"] = float(ret.std()) if len(ret) > 2 else None
    return out


def analyze(cfg: Config, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    b = load_bots(con)
    if b.empty:
        return {"n": 0}
    rows = []
    with db.open_db(cfg.paths.db, read_only=True) as mcon:
        have = set(db.list_candle_insts(mcon))
        for r in b.itertuples(index=False):
            end = int(r.stop_time) if pd.notna(r.stop_time) else db.now_ms()
            inst = r.inst_id if str(r.inst_id).endswith("-USDT-SWAP") else str(r.inst_id).split("-")[0] + "-USDT-SWAP"
            ctx: dict[str, Any] = {}
            pre: dict[str, Any] = {}
            post: dict[str, Any] = {}
            multi = "/" in str(r.inst_id)
            if inst in have and not multi:
                g = db.load_candles(mcon, inst, int(r.c_time), end + db.BAR_MS)
                ctx = _range_ctx(g, r.min_px, r.max_px)
                gp = db.load_candles(mcon, inst, int(r.c_time) - 288 * db.BAR_MS, int(r.c_time))
                if len(gp) > 12:
                    pre = {"ret_24h": float(gp["close"].iloc[-1] / gp["close"].iloc[0] - 1),
                           "ret_1h": float(gp["close"].iloc[-1] / gp["close"].iloc[-13] - 1)}
                ga = db.load_candles(mcon, inst, end, end + 72 * db.BAR_MS)
                if len(ga) > 1 and ctx.get("px_end"):
                    s = -1.0 if r.direction == "short" else 1.0
                    post = {"after_6h": s * float(ga["close"].iloc[-1] / ctx["px_end"] - 1)}
            s = -1.0 if r.direction == "short" else 1.0
            hours = (end - int(r.c_time)) / 3_600_000
            rows.append({
                "algo_id": r.algo_id, "type": r.algo_type, "type_label": TYPE_LABEL.get(r.algo_type, r.algo_type),
                "inst_id": r.inst_id, "direction": r.direction, "lever": r.lever, "investment": r.investment,
                "start": int(r.c_time), "end": end, "hours": hours, "state": r.state,
                "stop_reason": STOP_TYPE.get(str(r.stop_type), r.stop_type),
                "pnl": r.total_pnl, "pnl_ratio": r.pnl_ratio, "grid_profit": r.grid_profit, "float_profit": r.float_profit,
                "fee": r.fee, "funding": r.funding_fee, "min_px": r.min_px, "max_px": r.max_px, "grid_num": r.grid_num,
                "trade_num": r.trade_num, "run_px": r.run_px,
                "pnl_per_hour": (r.total_pnl or 0) / hours if hours > 0 else None,
                "unrealized": float(getattr(r, "unrealized", 0.0) or 0.0) if pd.notna(getattr(r, "unrealized", np.nan)) else 0.0,
                "detail": json.loads(r.raw) if hasattr(r, "raw") and r.algo_type == "smart_portfolio" and r.raw else None,
                "trend_with": (s * ctx["move"]) if ctx.get("move") is not None and r.direction in ("long", "short") else None,
                **{f"mkt_{k}": v for k, v in ctx.items()}, **{f"pre_{k}": v for k, v in pre.items()}, **{f"post_{k}": v for k, v in post.items()},
            })
    a = pd.DataFrame(rows)
    findings = bot_findings(a)
    by = lambda col: a.groupby(col, dropna=False)["pnl"].agg(n="count", pnl="sum", mean="mean",
                                                             win_rate=lambda x: float((x > 0).mean())).reset_index().to_dict("records")
    a["hold_bucket"] = pd.cut(a["hours"], [-1, 1, 6, 24, 72, 1e9], labels=["<1h", "1-6h", "6-24h", "1-3d", ">3d"]).astype(str)
    a["lever_bucket"] = pd.cut(a["lever"], [0, 5, 10, 20, 1e9], labels=["≤5x", "5-10x", "10-20x", ">20x"]).astype(str)
    return {
        "n": int(len(a)), "pnl": float(a["pnl"].sum()), "win_rate": float((a["pnl"] > 0).mean()),
        "unrealized": float(a["unrealized"].sum()), "n_running": int((a["state"] != "stopped").sum()),
        "portfolio": portfolio_summary(a),
        "fees": float(a["fee"].fillna(0).sum()), "funding": float(a["funding"].fillna(0).sum()),
        "grid_profit": float(a["grid_profit"].fillna(0).sum()),
        "float_profit": float((a["pnl"] - a["grid_profit"].fillna(0)).where(a["type"].isin(GRID_TYPES)).sum()),
        "bots": a.drop(columns=["hold_bucket", "lever_bucket"]).to_dict("records"),
        "by_type": by("type_label"), "by_direction": by("direction"), "by_inst": by("inst_id"),
        "by_hold": by("hold_bucket"), "by_lever": by("lever_bucket"),
        "findings": findings,
        "caveats": [
            "機器人損益採 OKX 回報的 totalPnl（已含手續費與資金費）",
            "網格損益 = 網格套利收益 + 持倉浮動損益；浮動損益通常是虧損主因",
            f"目前只有 {len(a)} 個機器人，以下歸納僅供參考",
        ],
    }


def portfolio_summary(a: pd.DataFrame) -> dict[str, Any] | None:
    p = a[a["type"] == "smart_portfolio"]
    if p.empty:
        return None
    run = p[p["state"] != "stopped"]
    return {"n_runs": int((p["state"] == "stopped").sum()), "realized_runs": float(p.loc[p["state"] == "stopped", "pnl"].sum()),
            "running": run.iloc[0].to_dict() if len(run) else None}


def bot_findings(a: pd.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if a.empty:
        return out
    g = a[a["type"].isin(GRID_TYPES)]
    if len(g):
        gp = float(g["grid_profit"].fillna(0).sum())
        fl = float((g["pnl"] - g["grid_profit"].fillna(0)).sum())
        out.append({"key": "grid_split", "title": "網格收益拆解",
                    "text": f"網格套利共賺 {gp:+.2f} USD，但持倉方向（浮動損益＋費用）合計 {fl:+.2f} USD；網格的成敗主要取決於方向，而不是格子"})
    worst = a.sort_values("pnl").iloc[0]
    if worst["pnl"] < 0 and worst["pnl"] < -0.5 * a.loc[a["pnl"] > 0, "pnl"].sum():
        ctx = []
        if pd.notna(worst.get("mkt_move")):
            ctx.append(f"運行期間價格 {worst['mkt_move']:+.1%}")
        if worst.get("mkt_broke"):
            ctx.append(f"價格{'跌破' if worst['mkt_broke'] == 'down' else '突破'}網格區間" +
                       (f"（啟動後 {worst['mkt_first_exit_min']:.0f} 分鐘）" if pd.notna(worst.get("mkt_first_exit_min")) else ""))
        if pd.notna(worst.get("pre_ret_24h")):
            ctx.append(f"啟動前 24h {worst['pre_ret_24h']:+.1%}")
        out.append({"key": "worst", "title": "單一機器人吃掉大部分獲利",
                    "text": f"{worst['inst_id']} {TYPE_LABEL.get(worst['type'], worst['type'])}（{'多' if worst['direction'] == 'long' else '空'} {worst['lever']:g}x）"
                            f"虧 {worst['pnl']:+.2f} USD，超過其他機器人獲利總和的一半。" + "；".join(ctx)})
    d = a[a["trend_with"].notna()]
    if len(d) >= 3:
        w, ag = d[d["trend_with"] > 0], d[d["trend_with"] < 0]
        out.append({"key": "direction", "title": "方向與行情是否一致",
                    "text": f"順勢（方向與運行期間價格變動一致）{len(w)} 個合計 {w['pnl'].sum():+.2f} USD；逆勢 {len(ag)} 個合計 {ag['pnl'].sum():+.2f} USD"})
    br = a[a["mkt_broke"].notna()] if "mkt_broke" in a else a.iloc[0:0]
    if len(br):
        out.append({"key": "range_break", "title": "價格跑出網格區間",
                    "text": f"{len(br)} 個網格運行期間價格離開設定區間，合計 {br['pnl'].sum():+.2f} USD；"
                            f"未跑出的 {int(len(g) - len(br))} 個合計 {g.loc[~g.index.isin(br.index), 'pnl'].sum():+.2f} USD"})
    hi = a[a["lever"] >= 20]
    if len(hi):
        out.append({"key": "lever", "title": "高槓桿機器人", "text": f"20x 以上 {len(hi)} 個，合計 {hi['pnl'].sum():+.2f} USD；其餘合計 {a.loc[a['lever'] < 20, 'pnl'].sum():+.2f} USD"})
    return out


def print_bots(res: dict[str, Any]) -> None:
    if not res.get("n"):
        print("沒有機器人紀錄")
        return
    print(f"\n== 機器人：{res['n']} 個，合計 {res['pnl']:+.2f} USD，勝率 {res['win_rate']:.0%}（網格套利 {res['grid_profit']:+.2f}，方向／浮動 {res['float_profit']:+.2f}）==")
    for x in res["bots"]:
        mv = x.get("mkt_move")
        print(f"  {ms_to_str(x['start'])} {x['inst_id']:<16} {x['type_label']:<6} {'多' if x['direction'] == 'long' else '空'} {x['lever']:>4g}x "
              f"{x['hours']:>6.1f}h  {x['pnl']:+9.2f} USD  區間內價格變動 {'-' if mv is None or pd.isna(mv) else f'{mv:+.1%}'}")
    for f in res["findings"]:
        print(f"  - {f['title']}：{f['text']}")
