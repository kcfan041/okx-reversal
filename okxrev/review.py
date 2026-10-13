from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.account import load_trades, summarize_trades
from okxrev.config import Config, load_config, ms_to_str
from okxrev.detector import btc_features, prepare, pump_mask, red_mask
from okxrev.features import CONDITIONS, as_arrays, bar_features
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.review")

BAR = db.BAR_MS
TZ_HOURS = 8


def candle_inst(inst_id: str) -> tuple[str, bool]:
    if inst_id.endswith("-USDT-SWAP"):
        return inst_id, False
    return inst_id.split("-")[0] + "-USDT-SWAP", True


def _sign(side: str) -> float:
    return -1.0 if side == "short" else 1.0


def _idx(index: np.ndarray, ts: int) -> int | None:
    if len(index) == 0:
        return None
    i = (int(ts) - int(index[0])) // BAR
    if 0 <= i < len(index):
        return int(i)
    return None


def _last_before(ev: np.ndarray, i: int, window: int) -> float:
    k = np.searchsorted(ev, i, side="right") - 1
    if k >= 0 and i - ev[k] <= window:
        return float(i - ev[k])
    return np.nan


def _next_after(ev: np.ndarray, i: int, window: int) -> float:
    k = np.searchsorted(ev, i, side="right")
    if k < len(ev) and ev[k] - i <= window:
        return float(ev[k] - i)
    return np.nan


def enrich_path(trades: pd.DataFrame, g: pd.DataFrame, feats: pd.DataFrame, rv: dict[str, Any], cost: float) -> list[dict[str, Any]]:
    idx = g.index.to_numpy(np.int64)
    o = g["open"].to_numpy(float)
    h = g["high"].to_numpy(float)
    lo = g["low"].to_numpy(float)
    c = g["close"].to_numpy(float)
    n = len(g)
    red = red_mask(g, rv["det"])
    pump = pump_mask(g, rv["det"])
    ev_red = np.flatnonzero(red)
    ev_pr = np.flatnonzero(red & pump)
    farr = as_arrays(feats)
    post = [int(k) for k in rv["post_exit_bars"]]
    holds = [int(k) for k in rv["hold_grid_bars"]]
    stops = [float(s) for s in rv["stop_grid"]]
    max_path = int(rv["max_hold_bars_for_path"])
    out: list[dict[str, Any]] = []
    for tr in trades.itertuples(index=False):
        r: dict[str, Any] = {"trade_id": tr.trade_id}
        ie = _idx(idx, tr.open_ts)
        ix = _idx(idx, tr.close_ts)
        if ie is None or ix is None or ie < 1 or ix < ie or np.isnan(c[ie]):
            r["path_ok"] = False
            out.append(r)
            continue
        s = _sign(tr.side)
        ref = float(tr.entry_px) if tr.entry_px and abs(float(tr.entry_px) / c[ie] - 1) < 0.05 else float(c[ie - 1])
        xref = float(tr.exit_px) if tr.exit_px and abs(float(tr.exit_px) / c[ix] - 1) < 0.05 else float(c[ix])
        r["price_ref_proxy"] = ref != tr.entry_px
        d = ie - 1
        for k in feats.columns:
            r[f"f_{k}"] = farr[k][d]
        for key, (_, _, fn) in CONDITIONS.items():
            r[f"c_{key}"] = bool(fn({k: farr[k][d:d + 1] for k in farr})[0])
        r["bars_since_red"] = _last_before(ev_red, d, 288)
        r["bars_since_pump_red"] = _last_before(ev_pr, d, 288)
        r["bars_to_next_pump_red"] = _next_after(ev_pr, d, 288)
        end = min(ix, ie + max_path)
        ph, pl = h[ie:end + 1], lo[ie:end + 1]
        if s > 0:
            fav = ph / ref - 1
            adv = 1 - pl / ref
        else:
            fav = 1 - pl / ref
            adv = ph / ref - 1
        fav = np.nan_to_num(fav, nan=0.0)
        adv = np.nan_to_num(adv, nan=0.0)
        r["path_ok"] = True
        r["hold_bars"] = int(ix - ie)
        r["mfe"] = float(max(fav.max(), 0.0))
        r["mae"] = float(max(adv.max(), 0.0))
        r["mae_before_mfe"] = bool(np.argmax(adv) < np.argmax(fav))
        r["capture"] = float(tr.ret_px / r["mfe"]) if r["mfe"] > 1e-4 and not np.isnan(tr.ret_px) else np.nan
        hi24 = np.nanmax(h[max(0, ie - 288):ie]) if ie > 0 else np.nan
        lo24 = np.nanmin(lo[max(0, ie - 288):ie]) if ie > 0 else np.nan
        r["entry_range_pos"] = (ref - lo24) / (hi24 - lo24) if hi24 > lo24 else np.nan
        for k in post:
            j = ix + k
            r[f"post_{k}"] = s * (c[j] / xref - 1) if j < n and not np.isnan(c[j]) else np.nan
        lev = float(tr.lever) if tr.lever and not np.isnan(tr.lever) and tr.lever > 1 else None
        liq = 1.0 / lev * 0.9 if lev else None
        for k in holds:
            j = ie + k
            if j >= n or np.isnan(c[j]):
                r[f"hold_{k}"] = np.nan
                continue
            wh, wl = h[ie:j + 1], lo[ie:j + 1]
            adv_k = float(np.nanmax(wh / ref - 1) if s < 0 else np.nanmax(1 - wl / ref))
            if liq is not None and adv_k >= liq:
                r[f"hold_{k}"] = -1.0 / lev - cost
                r[f"hold_{k}_liq"] = True
            else:
                r[f"hold_{k}"] = s * (c[j] / ref - 1) - cost
                r[f"hold_{k}_liq"] = False
        cum_adv = np.maximum.accumulate(adv)
        for st in stops:
            hit = bool(cum_adv[-1] >= st)
            r[f"stop_{st:g}"] = -st - cost if hit else (tr.ret_net if not np.isnan(tr.ret_net) else tr.ret_px - cost)
        j = ie + 288
        r["fwd_24h_from_entry"] = s * (c[j] / ref - 1) if j < n and not np.isnan(c[j]) else np.nan
        out.append(r)
    return out


def behavior(t: pd.DataFrame, revenge_min: float) -> pd.DataFrame:
    t = t.sort_values("open_ts").reset_index(drop=True)
    op = t["open_ts"].astype("int64").to_numpy()
    cl = t["close_ts"].astype("int64").to_numpy()
    pnl = t["pnl_usd"].to_numpy(float)
    notional = t["notional_usd"].to_numpy(float)
    order = np.argsort(cl, kind="stable")
    cl_sorted = cl[order]
    rows = []
    for i in range(len(t)):
        k = int(np.searchsorted(cl_sorted, op[i], side="left"))
        prev = order[:k]
        r: dict[str, Any] = {"trade_id": t.at[i, "trade_id"]}
        if len(prev):
            p = prev[-1]
            r["prev_pnl"] = pnl[p]
            r["gap_min"] = (op[i] - cl[p]) / 60_000
            streak = 0
            for q in prev[::-1]:
                if np.isnan(pnl[q]) or pnl[q] >= 0:
                    break
                streak += 1
            r["loss_streak_before"] = streak
            recent = notional[prev[-20:]]
            recent = recent[~np.isnan(recent)]
            r["size_vs_recent"] = notional[i] / np.median(recent) if len(recent) and np.median(recent) > 0 else np.nan
        else:
            r.update(prev_pnl=np.nan, gap_min=np.nan, loss_streak_before=0, size_vs_recent=np.nan)
        r["concurrent"] = int(((op < op[i]) & (cl > op[i])).sum())
        r["revenge"] = bool(r["prev_pnl"] < 0 and r["gap_min"] <= revenge_min) if not np.isnan(r["prev_pnl"]) else False
        rows.append(r)
    b = pd.DataFrame(rows)
    local = pd.to_datetime(t["open_ts"].astype("int64") + TZ_HOURS * 3_600_000, unit="ms")
    b["open_hour"] = local.dt.hour.to_numpy()
    b["open_weekday"] = local.dt.weekday.to_numpy()
    b["open_day"] = local.dt.strftime("%Y-%m-%d").to_numpy()
    return b


def enrich(cfg: Config, trades: pd.DataFrame | None = None) -> pd.DataFrame:
    t = load_trades(cfg) if trades is None else trades
    if t.empty:
        return t
    rv = {**cfg.account.get("review", {}), "det": cfg.detector}
    rv.setdefault("post_exit_bars", [12, 72, 288])
    rv.setdefault("hold_grid_bars", [12, 72, 288])
    rv.setdefault("stop_grid", [0.03, 0.05, 0.08, 0.12])
    rv.setdefault("max_hold_bars_for_path", 8640)
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]))
    t = t.copy()
    mapped = t["inst_id"].map(candle_inst)
    t["candle_inst"] = [m[0] for m in mapped]
    t["candle_proxy"] = [m[1] for m in mapped]
    btc_id = cfg.universe["btc_inst"]
    pad = 2 * 288 * BAR
    lo_all = int(t["open_ts"].min()) - pad
    hi_all = int(t["close_ts"].max()) + pad
    rows: list[dict[str, Any]] = []
    with db.open_db(cfg.paths.db, read_only=True) as con:
        have = set(db.list_candle_insts(con))
        btc = btc_features(db.load_candles(con, btc_id, lo_all - 300 * BAR, hi_all), cfg.btc_regime)
        for inst, grp in t.groupby("candle_inst"):
            if inst not in have:
                rows += [{"trade_id": x, "path_ok": False} for x in grp["trade_id"]]
                continue
            lo_ = int(grp["open_ts"].min()) - pad
            hi_ = int(grp["close_ts"].max()) + pad
            raw = db.load_candles(con, inst, lo_, hi_)
            if raw.empty:
                rows += [{"trade_id": x, "path_ok": False} for x in grp["trade_id"]]
                continue
            g = prepare(raw, cfg.detector, btc)
            f = bar_features(g, cfg.detector)
            rows += enrich_path(grp, g, f, rv, cost)
    e = pd.DataFrame(rows)
    b = behavior(t, float(rv.get("revenge_window_min", 30)))
    out = t.merge(e, on="trade_id", how="left").merge(b, on="trade_id", how="left")
    out["path_ok"] = out["path_ok"].fillna(False).astype(bool)
    return out.sort_values("close_ts").reset_index(drop=True)


def _sub(et: pd.DataFrame, m: pd.Series) -> dict[str, Any]:
    s = et[m]
    p = s["pnl_usd"].dropna()
    return {"n": int(len(s)), "pnl": float(p.sum()), "win_rate": float((p > 0).mean()) if len(p) else None,
            "avg": float(p.mean()) if len(p) else None, "avg_ret": float(s["ret_net"].mean()) if s["ret_net"].notna().any() else None}


def _issue(key: str, title: str, finding: str, impact: float, evidence: dict[str, Any], advice: str) -> dict[str, Any]:
    return {"key": key, "title": title, "finding": finding, "impact_usd": float(impact), "evidence": evidence, "advice": advice}


def _subset_issue(et: pd.DataFrame, m: pd.Series, key: str, title: str, desc: str, advice: str, min_n: int = 3) -> dict[str, Any] | None:
    m = m.fillna(False).astype(bool)
    if m.sum() < min_n or (~m).sum() < 1:
        return None
    a, b = _sub(et, m), _sub(et, ~m)
    if a["pnl"] >= 0 and (a["avg"] or 0) >= (b["avg"] or 0):
        return None
    finding = (f"{desc}：{a['n']} 筆，合計 {a['pnl']:+.2f} USD，勝率 {a['win_rate'] or 0:.0%}，每筆平均 {a['avg'] or 0:+.2f}；"
               f"其餘 {b['n']} 筆每筆平均 {b['avg'] or 0:+.2f}")
    return _issue(key, title, finding, -min(a["pnl"], 0.0), {"subset": a, "rest": b}, advice)


def counterfactuals(et: pd.DataFrame, rv: dict[str, Any]) -> list[dict[str, Any]]:
    p = et[et["path_ok"] & et["notional_usd"].notna() & et["pnl_usd"].notna()]
    if p.empty:
        return []
    actual = float(p["pnl_usd"].sum())
    out = []
    for col, label in ([(f"hold_{k}", f"固定持有 {int(k) * 5 // 60 if int(k) >= 12 else int(k) * 5}{'h' if int(k) >= 12 else 'm'} 出場") for k in rv["hold_grid_bars"]]
                       + [(f"stop_{float(s):g}", f"加 {float(s):.0%} 硬停損，其餘照實際出場") for s in rv["stop_grid"]]):
        if col not in p:
            continue
        q = p[p[col].notna()]
        if q.empty:
            continue
        cf = float((q[col] * q["notional_usd"]).sum())
        act = float(q["pnl_usd"].sum())
        n_liq = int(q[f"{col}_liq"].fillna(False).astype(bool).sum()) if f"{col}_liq" in q else 0
        out.append({"rule": label, "col": col, "n": int(len(q)), "actual_usd": act, "cf_usd": cf, "delta_usd": cf - act,
                    "cf_win_rate": float((q[col] > 0).mean()), "n_liquidated": n_liq})
    out.sort(key=lambda r: -r["delta_usd"])
    return [{"actual_total_usd": actual, **r} for r in out]


def diagnose(et: pd.DataFrame, rv: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    priced = et[et["pnl_usd"].notna()]
    if priced.empty:
        return issues
    pnl = priced["pnl_usd"]
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    p = priced[priced["path_ok"]]

    if len(losses) >= 3:
        worst = losses.sort_values().head(max(1, len(priced) // 10))
        share = float(worst.sum() / losses.sum())
        if share >= 0.4:
            issues.append(_issue(
                "tail_loss", "少數大虧吃掉獲利",
                f"最差 {len(worst)} 筆（{len(worst) / len(priced):.0%} 交易）占全部虧損 {share:.0%}，合計 {worst.sum():+.2f} USD；"
                f"總獲利 {wins.sum():+.2f} USD", -float(worst.sum()) * 0.5,
                {"worst_trades": priced.loc[worst.index, ["trade_id", "inst_id", "side", "pnl_usd", "mae", "hold_min", "lever"]].to_dict("records")},
                "設定單筆最大虧損上限（如帳戶 1-2%），進場前先決定停損位置"))

    if len(p) >= 5:
        pw, pl_ = p[p["pnl_usd"] > 0], p[p["pnl_usd"] < 0]
        if len(pw) >= 3 and "post_72" in p:
            cont = pw["post_72"].dropna()
            cap = pw["capture"].dropna()
            if len(cont) and cont.median() > 0.02:
                left = float((cont.clip(lower=0) * pw.loc[cont.index, "notional_usd"]).sum())
                issues.append(_issue(
                    "early_exit", "獲利單太早出場",
                    f"獲利單出場後 6h 價格仍朝有利方向走，中位數 {cont.median():+.1%}；平均只吃到持倉期間最大浮盈的 {cap.median():.0%}",
                    left * 0.5, {"post_6h_median": float(cont.median()), "capture_median": float(cap.median()) if len(cap) else None, "n": int(len(cont))},
                    "改用分批出場或移動停利，保留部分倉位讓趨勢延續"))
        if len(pl_) >= 3 and len(pw) >= 1:
            ratio = pl_["hold_min"].median() / max(pw["hold_min"].median(), 1e-9)
            if ratio >= 1.5:
                issues.append(_issue(
                    "hold_losers", "虧損單抱太久",
                    f"虧損單持倉中位數 {pl_['hold_min'].median():.0f} 分鐘，是獲利單（{pw['hold_min'].median():.0f} 分鐘）的 {ratio:.1f} 倍；"
                    f"虧損單平均最大逆向 {pl_['mae'].mean():.1%}",
                    -float(pl_["pnl_usd"].sum()) * 0.3, {"loser_hold_med": float(pl_["hold_min"].median()), "winner_hold_med": float(pw["hold_min"].median()),
                                                          "loser_mae_mean": float(pl_["mae"].mean()), "winner_mae_mean": float(pw["mae"].mean())},
                    "「賺小賠大」的典型：虧損時傾向等回本。把停損寫進下單流程，觸發就走"))
        if len(pl_) >= 3 and "post_72" in p:
            rec = pl_["post_72"].dropna()
            if len(rec) >= 3 and rec.median() > 0.02:
                issues.append(_issue(
                    "stopped_at_bottom", "砍在最痛點",
                    f"虧損單出場後 6h 價格回到有利方向，中位數 {rec.median():+.1%}（{(rec > 0).mean():.0%} 的虧損單出場後反轉）",
                    float((rec.clip(lower=0) * pl_.loc[rec.index, "notional_usd"]).sum()) * 0.3,
                    {"post_6h_median": float(rec.median()), "n": int(len(rec))},
                    "停損放在結構位（前低/前高）之外而非固定小百分比，或縮小部位以容納正常波動"))

    issue = _subset_issue(et, et["revenge"], "revenge", "虧損後急著再進場",
                          f"上一筆虧損後 {rv.get('revenge_window_min', 30)} 分鐘內再開倉", "虧損後強制冷靜一段時間再交易")
    if issue:
        issues.append(issue)
    issue = _subset_issue(et, et["loss_streak_before"] >= 2, "tilt", "連虧後表現變差", "連續虧損 2 筆以上後的交易",
                          "設定每日最大連虧次數，達到就停止當天交易")
    if issue:
        issues.append(issue)
    issue = _subset_issue(et, et["size_vs_recent"] >= 2, "size_up", "放大部位的交易虧損", "部位 ≥ 近期中位數 2 倍的交易",
                          "部位大小固定化；不要因為「有把握」或想回本而加大")
    if issue:
        issues.append(issue)
    if et["lever"].notna().any():
        issue = _subset_issue(et, et["lever"] >= max(10.0, float(et["lever"].quantile(0.75))), "high_lever", "高槓桿交易拖累",
                              f"槓桿 ≥ {max(10.0, float(et['lever'].quantile(0.75))):.0f}x 的交易", "降低槓桿，用部位大小而非槓桿控制風險")
        if issue:
            issues.append(issue)

    day_n = et.groupby("open_day")["trade_id"].transform("count")
    thr = max(5.0, float(et.groupby("open_day")["trade_id"].count().quantile(0.8)))
    issue = _subset_issue(et, day_n >= thr, "overtrading", "交易過度頻繁的日子虧損", f"單日開倉 ≥ {thr:.0f} 筆那幾天的交易",
                          "設定每日交易上限")
    if issue:
        issues.append(issue)

    if p.shape[0] >= 5:
        chase = ((p["side"] == "long") & (p["entry_range_pos"] >= 0.8)) | ((p["side"] == "short") & (p["entry_range_pos"] <= 0.2))
        issue = _subset_issue(p, chase, "chasing", "追高殺低", "做多進在 24h 區間頂部 20%、或做空進在底部 20%",
                              "等回測／反彈再進場，或改用限價單")
        if issue:
            issues.append(issue)
        against = ((p["side"] == "long") & (p["f_btc_regime"] == "weak")) | ((p["side"] == "short") & (p["f_btc_regime"] == "strong"))
        issue = _subset_issue(p, against, "against_btc", "逆 BTC 環境交易", "BTC 弱勢時做多、或 BTC 強勢時做空",
                              "BTC 4h 方向明確時，山寨順著大盤方向做")
        if issue:
            issues.append(issue)
        early = p["bars_to_next_pump_red"].notna() & (p["side"] == "short") & p["bars_since_pump_red"].isna()
        issue = _subset_issue(p, early, "short_too_early", "做空過早（訊號出現前就進場）", "在 pump_red 訊號出現前的 24h 內就做空",
                              "等巨大紅 K 確認結構破壞後再空，不要猜頂")
        if issue:
            issues.append(issue)

    for col, name, fmt in (("open_hour", "時段", "{:02d} 點"), ("open_weekday", "星期", "週{}")):
        g = et.groupby(col)["pnl_usd"].agg(["sum", "count", "mean"])
        g = g[g["count"] >= 3]
        if len(g) >= 3:
            bad = g[g["sum"] < 0].sort_values("sum").head(3)
            if len(bad) and bad["sum"].sum() < -0.2 * max(float(wins.sum()), 1e-9):
                labels = [fmt.format("一二三四五六日"[int(k)] if col == "open_weekday" else int(k)) for k in bad.index]
                issues.append(_issue(
                    f"bad_{col}", f"特定{name}表現差", f"{'、'.join(labels)}（UTC+{TZ_HOURS}）合計 {bad['sum'].sum():+.2f} USD，{int(bad['count'].sum())} 筆",
                    -float(bad["sum"].sum()), {"by": g.reset_index().to_dict("records")}, f"檢查這些{name}是否疲勞、分心或流動性差，考慮避開"))

    liq = et[et["close_type"].isin(["liquidation", "partial_liquidation"])]
    if len(liq):
        issues.append(_issue("liquidation", "發生強制平倉", f"{len(liq)} 筆強平，合計 {liq['pnl_usd'].sum():+.2f} USD（含強平罰金 {liq['liq_penalty_usd'].sum():+.2f}）",
                             -float(liq["pnl_usd"].sum()), {"trades": liq[["trade_id", "inst_id", "side", "lever", "pnl_usd"]].to_dict("records")},
                             "任何單筆都不應走到強平；用逐倉 + 停損，或降低槓桿"))

    gross = float(et["gross_pnl_usd"].fillna(0).sum())
    fees = -float(et["fee_usd"].fillna(0).sum())
    if fees > 0 and (gross <= 0 or fees / max(gross, 1e-9) > 0.3):
        issues.append(_issue("fees", "手續費侵蝕", f"手續費 {fees:.2f} USD，毛利 {gross:+.2f} USD",
                             fees * 0.5, {"fees": fees, "gross": gross}, "減少交易次數、用 maker 限價單、或拉長持倉"))
    fund = float(et["funding_usd"].fillna(0).sum())
    if fund < 0 and -fund > 0.1 * max(abs(gross), 1e-9):
        issues.append(_issue("funding", "資金費率成本", f"資金費合計 {fund:+.2f} USD", -fund, {"funding": fund},
                             "避免在極端費率時長時間持有擁擠方向"))
    issues.sort(key=lambda r: -r["impact_usd"])
    return issues


def strengths(et: pd.DataFrame) -> list[dict[str, Any]]:
    p = et[et["path_ok"] & et["pnl_usd"].notna()]
    out = []
    if len(p) < 5:
        return out
    for side in ("long", "short"):
        s = p[p["side"] == side]
        for key, (_, label, _) in CONDITIONS.items():
            col = f"c_{key}"
            if col not in s:
                continue
            m = s[col].fillna(False).astype(bool)
            if m.sum() < 3:
                continue
            a = s[m]
            if a["pnl_usd"].sum() > 0 and (a["pnl_usd"] > 0).mean() >= 0.55:
                out.append({"side": side, "cond": key, "label": ("做多" if side == "long" else "做空") + "：" + label,
                            "n": int(len(a)), "pnl": float(a["pnl_usd"].sum()), "win_rate": float((a["pnl_usd"] > 0).mean()),
                            "avg_ret": float(a["ret_net"].mean())})
    out.sort(key=lambda r: -r["pnl"])
    return out[:10]


def build_report(cfg: Config, et: pd.DataFrame) -> dict[str, Any]:
    rv = {"revenge_window_min": 30, "hold_grid_bars": [12, 72, 288], "stop_grid": [0.03, 0.05, 0.08, 0.12], **cfg.account.get("review", {})}
    if et.empty:
        return {"n_trades": 0}
    p = et[et["path_ok"]]
    entry = {}
    if len(p):
        entry = {
            "n_with_candles": int(len(p)),
            "n_proxy": int(p["candle_proxy"].sum()),
            "by_btc_regime": p.groupby("f_btc_regime")["pnl_usd"].agg(["count", "sum", "mean"]).reset_index().to_dict("records"),
            "range_pos_median": {s: float(p.loc[p["side"] == s, "entry_range_pos"].median()) for s in ("long", "short") if (p["side"] == s).any()},
            "with_pump_red_24h": _sub(p, p["bars_since_pump_red"].notna()),
            "pre_r24h_median": {s: float(p.loc[p["side"] == s, "f_r24h"].median()) for s in ("long", "short") if (p["side"] == s).any()},
        }
    exit_ = {}
    if len(p):
        w, l_ = p[p["pnl_usd"] > 0], p[p["pnl_usd"] < 0]
        exit_ = {
            "winner": {"mfe_med": float(w["mfe"].median()) if len(w) else None, "mae_med": float(w["mae"].median()) if len(w) else None,
                       "capture_med": float(w["capture"].median()) if len(w) else None, "hold_min_med": float(w["hold_min"].median()) if len(w) else None,
                       "post_6h_med": float(w["post_72"].median()) if len(w) and "post_72" in w else None},
            "loser": {"mfe_med": float(l_["mfe"].median()) if len(l_) else None, "mae_med": float(l_["mae"].median()) if len(l_) else None,
                      "hold_min_med": float(l_["hold_min"].median()) if len(l_) else None,
                      "post_6h_med": float(l_["post_72"].median()) if len(l_) and "post_72" in l_ else None,
                      "was_green_rate": float((l_["mfe"] > 0.02).mean()) if len(l_) else None},
        }
    risk = {
        "lever_buckets": et.assign(b=pd.cut(et["lever"], [0, 3, 5, 10, 20, 50, 1e9], labels=["≤3", "3-5", "5-10", "10-20", "20-50", ">50"]))
            .groupby("b", observed=True)["pnl_usd"].agg(["count", "sum", "mean"]).reset_index().astype({"b": str}).to_dict("records"),
        "size_quintiles": (et.assign(b=pd.qcut(et["notional_usd"], 5, duplicates="drop")).groupby("b", observed=True)["pnl_usd"]
                           .agg(["count", "sum", "mean"]).reset_index().astype({"b": str}).to_dict("records")) if et["notional_usd"].notna().sum() >= 5 else [],
    }
    beh = {
        "by_hour": et.groupby("open_hour")["pnl_usd"].agg(["count", "sum", "mean"]).reset_index().to_dict("records"),
        "by_weekday": et.groupby("open_weekday")["pnl_usd"].agg(["count", "sum", "mean"]).reset_index().to_dict("records"),
        "revenge": _sub(et, et["revenge"].fillna(False).astype(bool)),
        "after_loss_streak2": _sub(et, et["loss_streak_before"] >= 2),
        "hold_buckets": et.assign(b=pd.cut(et["hold_min"], [-1, 15, 60, 240, 1440, 4320, 1e12], labels=["<15m", "15m-1h", "1-4h", "4-24h", "1-3d", ">3d"]))
            .groupby("b", observed=True)["pnl_usd"].agg(["count", "sum", "mean"]).reset_index().astype({"b": str}).to_dict("records"),
    }
    return {
        "n_trades": int(len(et)),
        "summary": summarize_trades(et, TZ_HOURS),
        "issues": diagnose(et, rv),
        "strengths": strengths(et),
        "counterfactuals": counterfactuals(et, rv),
        "entry": entry,
        "exit": exit_,
        "risk": risk,
        "behavior": beh,
        "caveats": [
            "K 線為 5m，進出場所在那根 K 的高低點也計入 MAE/MFE，短線單會略為高估",
            "現貨／交割／幣本位合約以同幣種 USDT 永續 K 線代理",
            "反事實試算未考慮你實際可能的滑價與心理執行難度",
            "樣本少時（< 30 筆）各項結論僅供參考",
        ],
    }


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return o.isoformat()
    if o is pd.NA:
        return None
    raise TypeError(type(o))


def run(cfg: Config) -> dict[str, Any]:
    from okxrev import account, bots
    et = enrich(cfg)
    rep = build_report(cfg, et)
    out = cfg.paths.results
    out.mkdir(parents=True, exist_ok=True)
    try:
        con = account.connect(cfg.account_db, read_only=True)
        try:
            ba = bots.analyze(cfg, con)
        finally:
            con.close()
    except FileNotFoundError:
        ba = {"n": 0}
    allt = account.load_trades(cfg, sources=account.SOURCES)
    rep["bots"] = ba
    rep["all_sources"] = {
        "overall": account.summarize_trades(allt)["overall"] if len(allt) else {"n": 0},
        "by_source": account.summarize_trades(allt).get("by_source", []) if len(allt) else [],
    }
    (out / "bots.json").write_text(json.dumps(ba, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    if len(et):
        et.to_csv(out / "trades_enriched.csv", index=False, encoding="utf-8", float_format="%.6g")
    (out / "review.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    return {"enriched": et, "report": rep}


def print_report(rep: dict[str, Any]) -> None:
    if not rep.get("n_trades"):
        print("沒有交易紀錄。先執行 python -m okxrev.account --sync")
        return
    al = rep.get("all_sources") or {}
    if al.get("by_source"):
        o = al["overall"]
        print(f"帳戶合計（含機器人／跟單）：{o['pnl']:+.2f} USD，" + "，".join(f"{r['label']} {r['n']} 筆 {r['pnl']:+.2f}" for r in al["by_source"]))
        print("以下「操作問題」只看手動交易\n")
    s = rep["summary"]["overall"]
    print(f"手動交易 {s['n']} 筆  {ms_to_str(rep['summary']['first_ts'])} ~ {ms_to_str(rep['summary']['last_ts'])}")
    print(f"總損益 {s['pnl']:+.2f} USD  勝率 {s['win_rate'] or 0:.1%}  盈虧比 {s['payoff'] or 0:.2f}  獲利因子 {s['profit_factor'] or 0:.2f}  "
          f"最大回撤 {s['max_drawdown']:.2f}  手續費 {s['fees']:.2f}  資金費 {s['funding']:.2f}")
    print("\n== 操作問題（依估計影響排序）==")
    for i, it in enumerate(rep["issues"][:8], 1):
        print(f"{i}. {it['title']}  (影響約 {it['impact_usd']:.2f} USD)\n   {it['finding']}\n   建議：{it['advice']}")
    if rep["counterfactuals"]:
        print("\n== 反事實試算（同一批交易，改變出場方式）==")
        for r in rep["counterfactuals"]:
            print(f"  {r['rule']:<28} n={r['n']:>4}  實際 {r['actual_usd']:+10.2f}  試算 {r['cf_usd']:+10.2f}  差 {r['delta_usd']:+10.2f}")
    if rep["strengths"]:
        print("\n== 你做得好的情境 ==")
        for r in rep["strengths"]:
            print(f"  {r['label']:<36} n={r['n']:>3}  {r['pnl']:+.2f} USD  勝率 {r['win_rate']:.0%}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.review")
    ap.add_argument("--config", default=None)
    ap.add_argument("--sync", action="store_true", help="sync account history from OKX first")
    ap.add_argument("--replicate", action="store_true", help="also run the market-wide replication scan")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "review")
    if args.sync:
        from okxrev.account import NoCredentials, run_sync
        try:
            r = run_sync(cfg)
            print(f"帳戶同步完成：{r['streams']}（{r['requests']} 次請求）")
        except NoCredentials as e:
            print(e)
            return 2
    res = run(cfg)
    print_report(res["report"])
    from okxrev.bots import print_bots
    print_bots(res["report"].get("bots") or {})
    if len(res["enriched"]):
        from okxrev.exits import print_plan, run as run_exits
        print()
        print_plan(run_exits(cfg))
    if args.replicate and len(res["enriched"]):
        from okxrev.replicate import print_replicate, run as run_rep
        print_replicate(run_rep(cfg, res["enriched"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
