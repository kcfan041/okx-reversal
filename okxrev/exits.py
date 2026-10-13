from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.account import load_trades
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging
from okxrev.review import _idx, _json_default, candle_inst

log = logging.getLogger("okxrev.exits")

BAR = db.BAR_MS
INF = np.inf
ATR_N = 14
HORIZON = 288
MIN_SIDE = 20
TIME_GRID = [3, 6, 12, 36, 72, 288]

GRIDS: dict[str, dict[str, dict[str, list[float]]]] = {
    "pct": {
        "fixed": {"tp": [0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08, INF], "sl": [0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08]},
        "trail": {"trail_act": [0.005, 0.01, 0.02, 0.03], "trail_dist": [0.003, 0.005, 0.01, 0.02], "sl": [0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08]},
        "breakeven": {"be_trig": [0.005, 0.01, 0.02], "tp": [0.01, 0.015, 0.02, 0.03, 0.05, 0.08, INF], "sl": [0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08]},
        "partial": {"part_tp": [0.005, 0.01, 0.02, 0.03], "trail_dist": [0.005, 0.01, 0.02], "sl": [0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08]},
    },
    "atr": {
        "fixed": {"tp": [0.5, 1, 1.5, 2, 3, 4, 6, INF], "sl": [0.5, 1, 1.5, 2, 3, 4, 6]},
        "trail": {"trail_act": [0.5, 1, 2, 3], "trail_dist": [0.5, 1, 1.5, 2], "sl": [0.5, 1, 1.5, 2, 3, 4, 6]},
        "breakeven": {"be_trig": [0.5, 1, 2], "tp": [1, 1.5, 2, 3, 4, 6, INF], "sl": [0.5, 1, 1.5, 2, 3, 4, 6]},
        "partial": {"part_tp": [0.5, 1, 2, 3], "trail_dist": [0.5, 1, 2], "sl": [0.5, 1, 1.5, 2, 3, 4, 6]},
    },
}
PARAMS = ("tp", "sl", "trail_act", "trail_dist", "be_trig", "part_tp", "time_bars")
FAMILY_LABEL = {"fixed": "固定止盈止損", "trail": "移動停利", "breakeven": "保本停損", "partial": "分批止盈（先出一半，其餘保本＋移動停利）"}
UNIT_LABEL = {"pct": "固定百分比", "atr": "ATR 倍數"}


def atr_pct(h: np.ndarray, lo: np.ndarray, c: np.ndarray, i: int, ref: float, n: int = ATR_N) -> float:
    """ATR of the n bars strictly before bar i, as a fraction of ref."""
    if i - n - 1 < 0:
        return np.nan
    hh, ll, pc = h[i - n:i], lo[i - n:i], c[i - n - 1:i - 1]
    tr = np.nanmax(np.vstack([hh - ll, np.abs(hh - pc), np.abs(ll - pc)]), axis=0)
    v = float(np.nanmean(tr)) / ref
    return v if v > 0 else np.nan


def build_paths(cfg: Config, trades: pd.DataFrame, horizon: int = HORIZON) -> dict[str, Any]:
    """Per-trade favorable/adverse/close return paths from the entry bar, signed so favorable > 0."""
    t = trades.reset_index(drop=True)
    n = len(t)
    fav = np.full((n, horizon), np.nan)
    adv = np.full((n, horizon), np.nan)
    cls = np.full((n, horizon), np.nan)
    atr = np.full(n, np.nan)
    ok = np.zeros(n, dtype=bool)
    insts = t["inst_id"].map(lambda x: candle_inst(x)[0])
    with db.open_db(cfg.paths.db, read_only=True) as con:
        have = set(db.list_candle_insts(con))
        for inst, rows in t.groupby(insts):
            if inst not in have:
                continue
            lo_ = int(rows["open_ts"].min()) - 30 * BAR
            hi_ = int(rows["open_ts"].max()) + (horizon + 2) * BAR
            g = db.load_candles(con, inst, lo_, hi_)
            if g.empty:
                continue
            from okxrev.detector import to_grid
            g = to_grid(g)
            idx = g.index.to_numpy(np.int64)
            h, lo, c = (g[k].to_numpy(float) for k in ("high", "low", "close"))
            for k, tr in rows.iterrows():
                ie = _idx(idx, int(tr["open_ts"]))
                if ie is None or ie < 1 or np.isnan(c[ie]):
                    continue
                ref = float(tr["entry_px"]) if tr["entry_px"] and abs(float(tr["entry_px"]) / c[ie] - 1) < 0.05 else float(c[ie - 1])
                s = -1.0 if tr["side"] == "short" else 1.0
                end = min(len(c), ie + horizon)
                hh, ll, cc = h[ie:end], lo[ie:end], c[ie:end]
                if s > 0:
                    f, a, r = hh / ref - 1, 1 - ll / ref, cc / ref - 1
                else:
                    f, a, r = 1 - ll / ref, hh / ref - 1, 1 - cc / ref
                m = end - ie
                fav[k, :m], adv[k, :m], cls[k, :m] = f, a, r
                atr[k] = atr_pct(h, lo, c, ie, ref)
                ok[k] = True
    last = np.where(np.isnan(cls), -1, np.arange(horizon)).max(axis=1)
    return {"fav": fav, "adv": adv, "cls": cls, "atr": atr, "ok": ok & (last >= 0), "last": last}


def simulate(paths: dict[str, Any], sel: np.ndarray, prm: dict[str, np.ndarray], fee: np.ndarray, slip: float,
             lever: np.ndarray, scale: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised over P parameter sets x N trades. Returns (ret[P,N], exit_bar[P,N]).

    Intrabar order is pessimistic: the adverse extreme is assumed to happen before the favorable one,
    and trailing / breakeven stops only use the peak reached in previous bars.
    """
    fav = np.nan_to_num(paths["fav"][sel], nan=0.0)
    adv = np.nan_to_num(paths["adv"][sel], nan=0.0)
    cls = paths["cls"][sel]
    last = paths["last"][sel]
    N = fav.shape[0]
    P = len(prm["sl"])
    sc = scale[None, :]

    def p(k: str) -> np.ndarray:
        v = prm[k][:, None]
        return np.where(np.isinf(v), INF, v * sc) if k != "time_bars" else np.broadcast_to(v, (P, N))

    tp, sl, ta, td, be, ptp = p("tp"), p("sl"), p("trail_act"), p("trail_dist"), p("be_trig"), p("part_tp")
    tb = p("time_bars")
    fee_ = fee[None, :]
    lev = lever[None, :]
    liq = np.where(lev > 1, 0.9 / lev, INF)
    liq_loss = np.where(lev > 1, 1.0 / lev, 0.0)
    alive = np.ones((P, N), bool)
    rem = np.ones((P, N))
    acc = np.zeros((P, N))
    pdone = np.zeros((P, N), bool)
    peak = np.zeros((P, N))
    xbar = np.full((P, N), -1)
    has_part = np.isfinite(ptp)
    for j in range(fav.shape[1]):
        if not alive.any():
            break
        fj, aj = fav[:, j][None, :], adv[:, j][None, :]
        cj = np.nan_to_num(cls[:, j], nan=0.0)[None, :]
        stop = sl.copy()
        be_on = (peak >= be) | pdone
        stop = np.where(be_on, np.minimum(stop, -fee_), stop)
        tr_on = (peak >= ta) | (pdone & np.isfinite(td))
        stop = np.where(tr_on, np.minimum(stop, td - peak), stop)
        hit_liq = alive & (aj >= liq) & (stop >= liq)
        hit_sl = alive & ~hit_liq & (aj >= stop)
        acc = np.where(hit_liq, acc - rem * liq_loss, acc)
        acc = np.where(hit_sl, acc - rem * stop - rem * slip, acc)
        done = hit_liq | hit_sl
        xbar = np.where(done, j, xbar)
        alive &= ~done
        hp = alive & has_part & ~pdone & (fj >= ptp)
        acc = np.where(hp, acc + 0.5 * ptp, acc)
        rem = np.where(hp, 0.5, rem)
        pdone |= hp
        htp = alive & (fj >= tp)
        acc = np.where(htp, acc + rem * tp, acc)
        xbar = np.where(htp, j, xbar)
        alive &= ~htp
        ht = alive & ((j + 1 >= tb) | (j >= last[None, :]))
        acc = np.where(ht, acc + rem * cj - rem * slip, acc)
        xbar = np.where(ht, j, xbar)
        alive &= ~ht
        peak = np.maximum(peak, fj)
    return acc - fee_, xbar


def param_table(unit: str) -> pd.DataFrame:
    rows = []
    for fam, grid in GRIDS[unit].items():
        keys = list(grid)
        for vals in itertools.product(*grid.values(), TIME_GRID):
            r = {k: INF for k in PARAMS}
            r.update(dict(zip(keys, vals)))
            r["time_bars"] = vals[-1]
            r["family"] = fam
            r["gidx"] = tuple([grid[k].index(v) for k, v in zip(keys, vals[:-1])] + [TIME_GRID.index(vals[-1])])
            rows.append(r)
    return pd.DataFrame(rows)


def neighborhood_score(tab: pd.DataFrame, score: np.ndarray) -> np.ndarray:
    """Median score over each combo and its grid neighbours (same family, every index within ±1)."""
    out = np.full(len(tab), np.nan)
    for fam, sub in tab.groupby("family"):
        pos = {g: i for i, g in zip(sub.index, sub["gidx"])}
        d = len(next(iter(pos)))
        offs = list(itertools.product((-1, 0, 1), repeat=d))
        for g, i in pos.items():
            nb = [pos[t] for t in (tuple(a + b for a, b in zip(g, o)) for o in offs) if t in pos]
            out[i] = float(np.nanmedian(score[nb]))
    return out


def split_folds(open_ts: np.ndarray) -> np.ndarray:
    """Two time folds split at the largest gap between trades (each fold ≥ 25%), else at the median."""
    o = np.sort(open_ts)
    n = len(o)
    if n < 4:
        return np.zeros(len(open_ts), dtype=int)
    lo, hi = max(1, n // 4), min(n - 1, n - n // 4)
    gaps = np.diff(o)[lo - 1:hi]
    cut = o[lo - 1 + int(np.argmax(gaps)) + 1] if len(gaps) and gaps.max() > 7 * 86_400_000 else o[n // 2]
    return (open_ts >= cut).astype(int)


def describe_rule(r: dict[str, Any], unit: str) -> str:
    u = (lambda v: f"{v * 100:g}%") if unit == "pct" else (lambda v: f"{v:g}×ATR")
    parts = []
    fam = r["family"]
    if fam == "partial":
        parts.append(f"到 +{u(r['part_tp'])} 先出一半，其餘移到保本並以回落 {u(r['trail_dist'])} 移動停利")
    if fam == "trail":
        parts.append(f"浮盈達 +{u(r['trail_act'])} 後啟動移動停利，從最高點回落 {u(r['trail_dist'])} 出場")
    if fam == "breakeven":
        parts.append(f"浮盈達 +{u(r['be_trig'])} 後停損移到保本")
    if np.isfinite(r.get("tp", INF)):
        parts.insert(0, f"止盈 +{u(r['tp'])}")
    parts.insert(0, f"止損 -{u(r['sl'])}")
    tb = int(r["time_bars"])
    parts.append(f"持倉 {tb * 5} 分鐘未出場就平倉" if tb < HORIZON else "最長持有 24 小時")
    return "；".join(parts)


def _clean_rule(r: pd.Series | dict[str, Any]) -> dict[str, Any]:
    d = dict(r)
    out = {"family": d["family"]}
    for k in PARAMS:
        v = d.get(k, INF)
        out[k] = None if v is None or (isinstance(v, float) and np.isinf(v)) else float(v)
    out["time_bars"] = int(d["time_bars"])
    return out


def optimize(cfg: Config, trades: pd.DataFrame | None = None) -> dict[str, Any]:
    t0 = time.monotonic()
    t = load_trades(cfg) if trades is None else trades
    t = t[t["notional_usd"].notna() & (t["notional_usd"] > 0) & t["ret_net"].notna()].reset_index(drop=True)
    if t.empty:
        return {"n_trades": 0}
    paths = build_paths(cfg, t)
    ok = paths["ok"] & ~np.isnan(paths["atr"])
    t = t[ok].reset_index(drop=True)
    paths = {k: (v[ok] if isinstance(v, np.ndarray) and len(v) == len(ok) else v) for k, v in paths.items()}
    if len(t) < 10:
        return {"n_trades": int(len(t)), "note": "可對上 K 線的交易少於 10 筆，無法最佳化"}
    slip = float(cfg.metrics["slippage_per_side"])
    fee = np.clip((-t["fee_usd"].fillna(0) / t["notional_usd"]).to_numpy(float), 0, 0.01)
    fee = np.where(fee > 0, fee, 2 * float(cfg.metrics["fee_per_side"]))
    lever = t["lever"].fillna(1).to_numpy(float)
    actual = t["ret_net"].to_numpy(float)
    notional = t["notional_usd"].to_numpy(float)
    fold = split_folds(t["open_ts"].astype("int64").to_numpy())
    fold_ranges = [[ms_to_str(t.loc[fold == f, "open_ts"].min(), "%Y-%m-%d"), ms_to_str(t.loc[fold == f, "open_ts"].max(), "%Y-%m-%d")]
                   for f in (0, 1)]

    groups: list[tuple[str, np.ndarray]] = []
    sides = t["side"].to_numpy()
    small = [s for s in ("long", "short") if (sides == s).sum() < MIN_SIDE]
    if small:
        groups.append(("all", np.ones(len(t), bool)))
    for s in ("long", "short"):
        if s not in small:
            groups.append((s, sides == s))

    out: dict[str, Any] = {
        "n_trades": int(len(t)), "folds": fold_ranges, "fold_sizes": [int((fold == 0).sum()), int((fold == 1).sum())],
        "pooled_sides": small, "groups": {}, "horizon_bars": HORIZON,
    }
    grids: list[pd.DataFrame] = []
    for gname, gm in groups:
        res_units: dict[str, Any] = {}
        for unit in ("pct", "atr"):
            tab = param_table(unit)
            prm = {k: tab[k].to_numpy(float) for k in PARAMS}
            scale = paths["atr"][gm] if unit == "atr" else np.ones(int(gm.sum()))
            ret, xbar = simulate(paths, gm, prm, fee[gm], slip, lever[gm], scale)
            f = fold[gm]
            m_all = ret.mean(axis=1)
            mA = ret[:, f == 0].mean(axis=1) if (f == 0).any() else np.full(len(tab), np.nan)
            mB = ret[:, f == 1].mean(axis=1) if (f == 1).any() else np.full(len(tab), np.nan)
            usd = (ret * notional[gm][None, :]).sum(axis=1)
            rA, rB = neighborhood_score(tab, mA), neighborhood_score(tab, mB)
            rboth = neighborhood_score(tab, np.fmin(mA, mB))
            iA, iB, iF = int(np.nanargmax(rA)), int(np.nanargmax(rB)), int(np.nanargmax(rboth))
            nA, nB = int((f == 0).sum()), int((f == 1).sum())
            oos = (mB[iA] * nB + mA[iB] * nA) / max(nA + nB, 1)
            act = actual[gm]
            best = tab.iloc[iF]
            r_best = ret[iF]
            res_units[unit] = {
                "rule": _clean_rule(best), "text": describe_rule(dict(best), unit), "unit": unit,
                "mean_ret": float(m_all[iF]), "usd": float(usd[iF]), "win_rate": float((r_best > 0).mean()),
                "fold_means": [float(mA[iF]), float(mB[iF])], "neighborhood": float(rboth[iF]),
                "oos_mean": float(oos), "oos_detail": {"train_A_test_B": float(mB[iA]), "train_B_test_A": float(mA[iB]),
                                                        "rule_A": describe_rule(dict(tab.iloc[iA]), unit), "rule_B": describe_rule(dict(tab.iloc[iB]), unit)},
                "avg_hold_min": float((xbar[iF] + 1).mean() * 5),
                "liquidations": int(((r_best <= -(1 / np.maximum(lever[gm], 1)) + 1e-9) & (lever[gm] > 1)).sum()),
                "best_by_family": {fam: {"text": describe_rule(dict(tab.iloc[i]), unit), "mean_ret": float(m_all[i]),
                                         "fold_means": [float(mA[i]), float(mB[i])], "usd": float(usd[i])}
                                   for fam in GRIDS[unit] for i in [int(np.nanargmax(np.where(tab["family"] == fam, rboth, -INF)))]},
            }
            g = tab.drop(columns=["gidx"]).assign(group=gname, unit=unit, mean_ret=m_all, fold_a=mA, fold_b=mB, usd=usd, robust=rboth)
            grids.append(g)
        act = actual[gm]
        choice = max(res_units, key=lambda u: res_units[u]["oos_mean"])
        hold_curve = {}
        for k in (1, 3, 6, 12, 36, 72, 144, 288):
            c = paths["cls"][gm][:, k - 1]
            v = c[~np.isnan(c)] - fee[gm][~np.isnan(c)] - slip
            hold_curve[f"{k * 5}m"] = {"n": int(len(v)), "mean": float(v.mean()) if len(v) else None}
        out["groups"][gname] = {
            "n": int(gm.sum()),
            "actual": {"mean_ret": float(act.mean()), "usd": float((act * notional[gm]).sum()), "win_rate": float((act > 0).mean()),
                       "fold_means": [float(act[fold[gm] == 0].mean()) if (fold[gm] == 0).any() else None,
                                      float(act[fold[gm] == 1].mean()) if (fold[gm] == 1).any() else None],
                       "median_hold_min": float(t.loc[gm, "hold_min"].median())},
            "units": res_units, "chosen_unit": choice, "recommended": res_units[choice],
            "atr_median": float(np.nanmedian(paths["atr"][gm])),
            "hold_curve": hold_curve,
        }
    out["cooldown"] = cooldown(t)
    out["elapsed_sec"] = round(time.monotonic() - t0, 1)
    out["caveats"] = [
        "以 5 分鐘 K 線模擬：同一根 K 同時碰到止盈與止損時一律算止損；移動停利只用前幾根 K 的最高浮盈（保守）",
        "進場那根 K 的高低點可能發生在你進場之前，極短線單會失真",
        "參數用兩段時間互相驗證並取鄰近參數的中位數挑選，但樣本仍少，請當作起點而非定律",
        "手續費用你每筆實際費率，停損／時間出場另加滑價",
    ]
    out["_grid"] = pd.concat(grids, ignore_index=True)
    return out


def cooldown(t: pd.DataFrame) -> dict[str, Any]:
    t = t.sort_values("open_ts").reset_index(drop=True)
    op = t["open_ts"].astype("int64").to_numpy()
    cl = t["close_ts"].astype("int64").to_numpy()
    pnl = t["pnl_usd"].to_numpy(float)
    order = np.argsort(cl, kind="stable")
    prev_pnl = np.full(len(t), np.nan)
    gap = np.full(len(t), np.nan)
    for i in range(len(t)):
        k = int(np.searchsorted(cl[order], op[i], side="left"))
        if k:
            p = order[k - 1]
            prev_pnl[i] = pnl[p]
            gap[i] = (op[i] - cl[p]) / 60_000
    d = pd.DataFrame({"pnl": pnl, "gap": gap, "prev": np.where(np.isnan(prev_pnl), "none", np.where(prev_pnl < 0, "loss", "win")),
                      "fold": split_folds(op)})
    bins = [-1, 5, 15, 30, 60, 240, 1e12]
    labels = ["<5m", "5-15m", "15-30m", "30m-1h", "1-4h", ">4h"]
    d["bucket"] = pd.cut(d["gap"], bins, labels=labels)
    table = (d[d["prev"] != "none"].groupby(["prev", "bucket"], observed=True)["pnl"]
             .agg(n="count", sum="sum", mean="mean", win_rate=lambda x: float((x > 0).mean())).reset_index())
    rec: dict[str, Any] = {}
    for prev in ("loss", "win"):
        best = None
        rows = []
        for x in (5, 10, 15, 30, 60, 120, 240):
            m = (d["prev"] == prev) & (d["gap"] < x)
            if m.sum() == 0:
                continue
            saved = -float(d.loc[m, "pnl"].sum())
            sf = [-float(d.loc[m & (d["fold"] == f), "pnl"].sum()) for f in (0, 1)]
            row = {"minutes": x, "skipped": int(m.sum()), "saved_usd": saved, "saved_by_fold": sf,
                   "skipped_win_rate": float((d.loc[m, "pnl"] > 0).mean())}
            rows.append(row)
            if m.sum() >= 3 and saved > 0 and min(sf) >= 0 and (best is None or saved > best["saved_usd"]):
                best = row
        rec[prev] = {"grid": rows, "recommended": best}
    return {"table": table.to_dict("records"), "after_loss": rec["loss"], "after_win": rec["win"]}


def run(cfg: Config) -> dict[str, Any]:
    res = optimize(cfg)
    grid = res.pop("_grid", None)
    out = cfg.paths.results
    out.mkdir(parents=True, exist_ok=True)
    if grid is not None:
        grid.replace([INF], np.nan).to_csv(out / "exit_grid.csv", index=False, encoding="utf-8", float_format="%.6g")
    (out / "exits.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    return res


def load_plan(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "exits.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def print_plan(res: dict[str, Any]) -> None:
    if not res.get("groups"):
        print(res.get("note", "沒有可最佳化的交易"))
        return
    pc = lambda v: "-" if v is None else f"{v * 100:+.2f}%"
    print(f"出場最佳化：{res['n_trades']} 筆；驗證分段 A {res['folds'][0]} ({res['fold_sizes'][0]} 筆) / B {res['folds'][1]} ({res['fold_sizes'][1]} 筆)")
    for g, r in res["groups"].items():
        name = {"long": "做多", "short": "做空", "all": "多空合併"}[g]
        a = r["actual"]
        print(f"\n== {name}（{r['n']} 筆，ATR 中位數 {r['atr_median'] * 100:.2f}%）==")
        print(f"  你實際：每筆 {pc(a['mean_ret'])}  勝率 {a['win_rate']:.0%}  合計 {a['usd']:+.2f} USD  (A {pc(a['fold_means'][0])} / B {pc(a['fold_means'][1])})")
        for u, x in r["units"].items():
            mark = "★" if u == r["chosen_unit"] else " "
            print(f" {mark}{UNIT_LABEL[u]}：{x['text']}")
            print(f"     每筆 {pc(x['mean_ret'])}  勝率 {x['win_rate']:.0%}  合計 {x['usd']:+.2f} USD  (A {pc(x['fold_means'][0])} / B {pc(x['fold_means'][1])})  "
                  f"樣本外 {pc(x['oos_mean'])}  平均持倉 {x['avg_hold_min']:.0f} 分"
                  + (f"  ⚠ 以你原槓桿會強平 {x['liquidations']} 筆" if x["liquidations"] else ""))
    cd = res["cooldown"]
    for k, name in (("after_loss", "虧損後"), ("after_win", "獲利後")):
        rec = cd[k]["recommended"]
        if rec:
            print(f"\n{name}冷卻：建議 {rec['minutes']} 分鐘內不開新單（過去 {rec['skipped']} 筆，可少虧 {rec['saved_usd']:.2f} USD，兩段都成立）")
        else:
            print(f"\n{name}冷卻：資料不支持設定冷卻時間")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.exits")
    ap.add_argument("--config", default=None)
    ap.add_argument("--live", action="store_true", help="show plan for current open positions")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "exits")
    if args.live:
        from okxrev.live import print_live, live_plan
        print_live(live_plan(cfg))
        return 0
    print_plan(run(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
