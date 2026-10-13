from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
import pandas as pd

from okxrev import account, db
from okxrev.config import Config, ms_to_str
from okxrev.exits import ATR_N, atr_pct, load_plan
from okxrev.okx_client import OkxClient

log = logging.getLogger("okxrev.live")

BAR = db.BAR_MS
POSITIONS_PATH = "/api/v5/account/positions"
BALANCE_PATH = "/api/v5/account/balance"
MIN_SPOT_USD = 1.0
STABLE_EXTRA = {"USDG", "DAI", "FDUSD", "PYUSD", "TUSD", "USDE"}


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def parse_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in rows:
        pos = _f(d.get("pos")) or 0.0
        if pos == 0:
            continue
        ps = (d.get("posSide") or "net").lower()
        side = ps if ps in ("long", "short") else ("long" if pos > 0 else "short")
        out.append({
            "inst_type": d.get("instType"), "inst_id": d.get("instId"), "side": side, "size": abs(pos),
            "entry_px": _f(d.get("avgPx")), "lever": _f(d.get("lever")) or 1.0, "liq_px": _f(d.get("liqPx")),
            "mark_px": _f(d.get("markPx")) or _f(d.get("last")), "open_ts": int(d.get("cTime") or 0) or None,
            "upl": _f(d.get("upl")), "notional_usd": _f(d.get("notionalUsd")), "mgn_mode": d.get("mgnMode"),
        })
    return out


def spot_cost_from_fills(fills: pd.DataFrame, inst: str) -> tuple[float | None, int | None]:
    f = fills[(fills["inst_id"] == inst)].copy()
    if f.empty:
        return None, None
    f["bill_num"] = pd.to_numeric(f["bill_id"], errors="coerce")
    qty = cost = 0.0
    start: int | None = None
    base, quote = inst.split("-")[:2]
    for r in f.sort_values(["ts", "bill_num"]).itertuples(index=False):
        px, sz, fee, fccy = float(r.fill_px or 0), float(r.fill_sz or 0), float(r.fee or 0), (r.fee_ccy or "").upper()
        if r.side == "buy":
            if qty * px < MIN_SPOT_USD:
                qty, cost, start = 0.0, 0.0, int(r.ts)
            qty += sz + (fee if fccy == base else 0.0)
            cost += sz * px - (fee if fccy == quote else 0.0)
        else:
            take = min(sz, qty)
            if qty > 0:
                cost -= cost / qty * take
            qty -= take
    if qty <= 0:
        return None, None
    return cost / qty, start


def parse_spot(rows: list[dict[str, Any]], fills: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for acct in rows:
        for d in acct.get("details", []):
            ccy = d.get("ccy")
            if ccy in account.STABLE or ccy in STABLE_EXTRA:
                continue
            eq = _f(d.get("eq")) or 0.0
            stgy = _f(d.get("stgyEq")) or 0.0
            qty = max(eq - stgy, 0.0)
            eq_usd = (_f(d.get("eqUsd")) or 0.0) * (qty / eq if eq else 0.0)
            if eq_usd < MIN_SPOT_USD or qty <= 0:
                continue
            inst = f"{ccy}-USDT"
            avg = _f(d.get("openAvgPx")) or _f(d.get("accAvgPx"))
            fa, start = spot_cost_from_fills(fills, inst) if not fills.empty else (None, None)
            out.append({
                "inst_type": "SPOT", "inst_id": inst, "side": "long", "size": qty, "entry_px": avg or fa,
                "lever": 1.0, "liq_px": None, "mark_px": eq_usd / qty, "open_ts": start, "upl": _f(d.get("spotUpl")),
                "notional_usd": eq_usd, "mgn_mode": "cash", "cost_known": (avg or fa) is not None,
                "frozen": max((_f(d.get("frozenBal")) or 0.0) - stgy, 0.0) / qty if qty else 0.0,
            })
    return out


def _candles(client: OkxClient, inst: str, after: int | None = None, limit: int = 300) -> pd.DataFrame:
    try:
        if after is None:
            return client.candles(inst, "5m", limit=limit)
        df, _ = client.history_candles(inst, "5m", limit=min(limit, 100), after=after)
        return df
    except Exception as e:
        log.warning("candles %s failed: %s", inst, e)
        return pd.DataFrame()


def entry_atr(client: OkxClient, inst: str, open_ts: int | None, ref: float) -> tuple[float, str]:
    if open_ts:
        g = _candles(client, inst, after=(open_ts // BAR) * BAR, limit=ATR_N + 2)
        if len(g) >= ATR_N + 1:
            h, lo, c = (g[k].to_numpy(float) for k in ("high", "low", "close"))
            v = atr_pct(h, lo, c, len(g), ref)
            if not np.isnan(v):
                return v, "entry"
    g = _candles(client, inst, limit=ATR_N + 2)
    if len(g) >= ATR_N + 1:
        h, lo, c = (g[k].to_numpy(float) for k in ("high", "low", "close"))
        return atr_pct(h, lo, c, len(g), ref), "current"
    return np.nan, "none"


def peak_since(client: OkxClient, inst: str, open_ts: int | None, side: str, ref: float) -> tuple[float | None, bool]:
    g = _candles(client, inst, limit=300)
    if g.empty or not open_ts:
        return None, False
    complete = int(g["ts"].min()) <= open_ts
    g = g[g["ts"] >= (open_ts // BAR) * BAR]
    if g.empty:
        return 0.0, complete
    if side == "short":
        return float(1 - g["low"].min() / ref), complete
    return float(g["high"].max() / ref - 1), complete


def plan_for(pos: dict[str, Any], plan: dict[str, Any], client: OkxClient, now_ms: int) -> dict[str, Any]:
    groups = plan.get("groups", {})
    gname = pos["side"] if pos["side"] in groups else "all"
    g = groups.get(gname)
    out = dict(pos)
    out["group"] = gname
    ref = pos.get("entry_px")
    if not g or not ref:
        out["error"] = "沒有可用的出場規則" if not g else "不知道進場成本（近 3 個月沒有這個幣的買進紀錄）"
        if pos.get("frozen", 0) > 0.5:
            out["error"] += f"；{pos['frozen']:.0%} 被凍結（掛單或其他用途）"
        return out
    rec = g["recommended"]
    rule, unit = rec["rule"], rec["unit"]
    md_inst = pos["inst_id"]
    atr, atr_src = entry_atr(client, md_inst, pos.get("open_ts"), ref)
    if unit == "atr" and np.isnan(atr):
        alt = g["units"].get("pct")
        if alt:
            rec, rule, unit = alt, alt["rule"], "pct"
    scale = atr if unit == "atr" else 1.0
    s = -1.0 if pos["side"] == "short" else 1.0

    def px(frac: float | None) -> float | None:
        return None if frac is None else ref * (1 + s * frac)

    def frac(k: str) -> float | None:
        v = rule.get(k)
        return None if v is None else v * scale

    sl, tp, ta, td, be, ptp = (frac(k) for k in ("sl", "tp", "trail_act", "trail_dist", "be_trig", "part_tp"))
    mark = pos.get("mark_px")
    cur = s * (mark / ref - 1) if mark else None
    peak, peak_complete = peak_since(client, md_inst, pos.get("open_ts"), pos["side"], ref)
    if peak is not None and cur is not None:
        peak = max(peak, cur)
    fee = 2 * 0.0005
    be_on = (be is not None and peak is not None and peak >= be) or (ptp is not None and peak is not None and peak >= ptp)
    tr_on = (ta is not None and peak is not None and peak >= ta) or (ptp is not None and td is not None and peak is not None and peak >= ptp)
    stop_frac = sl
    if be_on:
        stop_frac = min(stop_frac, -fee)
    if tr_on and td is not None and peak is not None:
        stop_frac = min(stop_frac, td - peak)
    deadline = pos["open_ts"] + int(rule["time_bars"]) * BAR if pos.get("open_ts") else None
    out.update({
        "rule_text": rec["text"], "unit": unit, "atr_pct": None if np.isnan(atr) else atr, "atr_source": atr_src,
        "cur_ret": cur, "peak_ret": peak, "peak_complete": peak_complete,
        "sl_px": px(-sl) if sl is not None else None, "tp_px": px(tp),
        "partial_px": px(ptp), "be_trigger_px": px(be), "trail_act_px": px(ta), "trail_dist": td,
        "be_active": bool(be_on), "trail_active": bool(tr_on),
        "active_stop_px": px(-stop_frac) if stop_frac is not None else None,
        "deadline_ts": deadline, "minutes_left": (deadline - now_ms) / 60_000 if deadline else None,
        "expected": {"mean_ret": rec["mean_ret"], "win_rate": rec["win_rate"], "oos_mean": rec["oos_mean"]},
    })
    warn = []
    liq = pos.get("liq_px")
    if liq and out["active_stop_px"]:
        if (s > 0 and out["active_stop_px"] <= liq) or (s < 0 and out["active_stop_px"] >= liq):
            warn.append(f"建議停損 {out['active_stop_px']:.6g} 比強平價 {liq:.6g} 更遠：降低槓桿或把停損設在強平價之前")
    if deadline and deadline < now_ms:
        warn.append("已超過建議持倉時間")
    if pos.get("cost_known") is False:
        warn.append("現貨成本不完整，價位僅供參考")
    if not peak_complete and pos.get("open_ts"):
        warn.append("持倉超過 25 小時，最高浮盈只計最近 25 小時")
    out["warnings"] = warn
    return out


def cooldown_status(cfg: Config, plan: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
    t = account.load_trades(cfg)
    if t.empty:
        return None
    last = t.sort_values("close_ts").iloc[-1]
    win = (last["pnl_usd"] or 0) >= 0
    rec = (plan.get("cooldown") or {}).get("after_win" if win else "after_loss", {}).get("recommended")
    out = {"last_inst": last["inst_id"], "last_pnl": float(last["pnl_usd"]) if pd.notna(last["pnl_usd"]) else None,
           "last_close_ts": int(last["close_ts"]), "minutes": rec["minutes"] if rec else None}
    if rec:
        until = int(last["close_ts"]) + rec["minutes"] * 60_000
        out["until_ts"] = until
        out["active"] = until > now_ms
    return out


def live_plan(cfg: Config, client: OkxClient | None = None, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    plan = load_plan(cfg)
    if not plan or not plan.get("groups"):
        return {"ok": False, "message": "尚未執行出場最佳化（python -m okxrev.exits 或按「同步並分析」）", "positions": []}
    own = client is None
    client = client or account.make_client(cfg)
    try:
        positions = parse_positions(client.get_private(POSITIONS_PATH, {}))
        try:
            con = account.connect(cfg.account_db)
            try:
                account.sync_fills(client, con, "SPOT", now_ms - 92 * 86_400_000)
                fills = con.execute("SELECT * FROM fills WHERE inst_type = 'SPOT'").df()
            finally:
                con.close()
        except Exception as e:
            log.warning("spot fills refresh failed: %s", e)
            fills = pd.DataFrame()
        bal = client.get_private(BALANCE_PATH, {})
        positions += parse_spot(bal, fills)
        res = [plan_for(p, plan, client, now_ms) for p in positions]
        bot_hold = bot_holdings(cfg, bal)
    finally:
        if own:
            client.close()
    return {"ok": True, "now": now_ms, "positions": res, "bot_holdings": bot_hold, "cooldown": cooldown_status(cfg, plan, now_ms),
            "plan_groups": {k: v["recommended"]["text"] for k, v in plan["groups"].items()}}


def bot_holdings(cfg: Config, balance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coins held by running bots (stgyEq), with cost from the reconstructed smart portfolio."""
    from okxrev import portfolio
    costs: dict[str, dict[str, Any]] = {}
    try:
        con = account.connect(cfg.account_db, read_only=True)
        try:
            bills, _ = portfolio.load(con)
            snap = pd.DataFrame([(d.get("ccy"), _f(d.get("stgyEq")) or 0.0, _f(d.get("eq")) or 0.0, _f(d.get("eqUsd")) or 0.0)
                                 for a in balance for d in a.get("details", [])], columns=["ccy", "stgy_eq", "eq", "eq_usd"])
            r = portfolio.reconstruct(bills, snap, portfolio.bot_windows(con))["running"]
            costs = {c["ccy"]: c for c in (r or {}).get("coins", [])}
        finally:
            con.close()
    except FileNotFoundError:
        pass
    out = []
    for a in balance:
        for d in a.get("details", []):
            stgy = _f(d.get("stgyEq")) or 0.0
            eq = _f(d.get("eq")) or 0.0
            if stgy <= 0 or not eq or d.get("ccy") in account.STABLE or d.get("ccy") in STABLE_EXTRA:
                continue
            px = (_f(d.get("eqUsd")) or 0.0) / eq
            c = costs.get(d.get("ccy"), {})
            avg = c.get("avg_cost")
            out.append({"ccy": d.get("ccy"), "qty": stgy, "px_now": px, "value_usd": stgy * px, "avg_cost": avg,
                        "unrealized": (px - avg) * stgy if avg else None, "ret": px / avg - 1 if avg else None,
                        "bot": "智能組合" if c else "機器人", "since": c.get("start")})
    return sorted(out, key=lambda x: -x["value_usd"])


def print_live(res: dict[str, Any]) -> None:
    if not res.get("ok"):
        print(res.get("message"))
        return
    cd = res.get("cooldown")
    if cd and cd.get("active"):
        print(f"冷卻中：上一筆 {cd['last_inst']} {cd['last_pnl']:+.2f} USD，建議 {ms_to_str(cd['until_ts'])} UTC 之後再開新單")
    if not res["positions"]:
        print("目前沒有手動持倉")
    for h in res.get("bot_holdings", []):
        print(f"[{h['bot']}] {h['ccy']} {h['qty']:g}  市值 {h['value_usd']:.2f}  成本 {h['avg_cost'] or 0:.6g}  "
              f"未實現 {h['unrealized'] if h['unrealized'] is not None else float('nan'):+.2f}")
    for p in res["positions"]:
        print(f"\n{p['inst_id']} {'多' if p['side'] == 'long' else '空'} {p['size']:g} @ {p.get('entry_px') or 0:.6g}  槓桿 {p['lever']:g}x  現價 {p.get('mark_px') or 0:.6g}")
        if p.get("error"):
            print("  " + p["error"])
            continue
        pc = lambda v: "-" if v is None else f"{v * 100:+.2f}%"
        print(f"  規則：{p['rule_text']}")
        print(f"  目前 {pc(p['cur_ret'])}  最高浮盈 {pc(p['peak_ret'])}  止損 {p['sl_px'] and f'{p['sl_px']:.6g}'}  止盈 {p['tp_px'] and f'{p['tp_px']:.6g}'}  "
              f"目前有效停損 {p['active_stop_px'] and f'{p['active_stop_px']:.6g}'}")
        if p["minutes_left"] is not None:
            print(f"  建議出場時間 {ms_to_str(p['deadline_ts'])} UTC（剩 {p['minutes_left']:.0f} 分鐘）")
        for w in p["warnings"]:
            print("  ! " + w)
