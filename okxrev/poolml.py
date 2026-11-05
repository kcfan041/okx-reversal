"""Meta-label model for the shared-capital pool: score every candidate signal and take only the high scores.

Signals are the pool's candidate traders' closed unit-book (C) positions (the same set okxrev.pool picks members from).
Features per signal, all known at entry: the 19 market features the trader logged with its decision, side, hold
length and exit rule, trader family and kind, the trader's last-LOOK-day record at the latest weekly re-pick (trades,
mean, excess over the same-side random controls and its t-value, as in the pool), and the market state (median alt
24h / 7d log return over the replayed coins and the alt index up / down streak in days). Label: the stop-adjusted
return (pool.prepare) > 0.

Walk-forward by calendar month, starting TRAIN_D days after the first signal. For month M: train on signals that
exited before M - HOLD_D days, score the hold-out (entered in the last HOLD_D days, exited before M) and set the
threshold to its (1 - f) quantile, then score month M with that same model. Nothing after M's start is used.

The taken signals go through the same pool simulation (okxrev.pool.simulate) from the first scored month, compared
with: the pool's main member rule over the same period, the main rule's member entries filtered by the model, every
candidate signal, and random subsets of the same size per month (SIMS runs). A final model trained on the whole
blind replay is applied to live paper trading as the out-of-sample check.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db, ml, pool, regime
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.poolml")

H, D = pool.H, pool.D
NUM = ["r1h", "r6h", "r24h", "r3d", "vol_ratio", "vmax_1h", "pos24", "pos7d", "dist_ema", "vol_rank", "atr", "btc_r1h", "btc_r4h",
       "breadth", "age_d", "funding", "oi_chg"]
CATS = ["family", "kind", "exit_rule"]
LOOK = pool.MAIN[1]
TRAIN_D = 90
HOLD_D = 30
FRACS = (0.1, 0.2, 0.4)
MAIN_F = 0.2
SIMS = 50


# ---------------------------------------------------------------- data
def load_signals(cfg: Config, source: str) -> tuple[pd.DataFrame, dict[str, str]]:
    from okxrev import replay
    p = cfg.paper_db if source == "live" else replay.out_db_path(cfg)
    if not p.exists():
        return pd.DataFrame(), {}
    con = db.connect(p, read_only=True, wait_sec=2.0, max_tries=60)
    try:
        pos = con.execute("""SELECT p.trader_id, p.inst_id, p.side, p.entry_ts, p.exit_ts, p.entry_px, p.stop_px, p.ret_net, p.mae,
                                    d.exit_rule, d.features
                             FROM positions p JOIN decisions d USING (decision_id)
                             WHERE p.book = 'C' AND p.state = 'closed' AND p.entry_ts IS NOT NULL AND p.exit_ts IS NOT NULL
                               AND p.ret_net IS NOT NULL AND p.entry_px > 0""").df()
        kinds = dict(con.execute("SELECT trader_id, kind FROM traders").fetchall())
    finally:
        con.close()
    return pos, kinds


def hold_hours(rule: str | None) -> float:
    tok = (rule or "").split("_")[0]
    try:
        n = float(tok[:-1])
    except ValueError:
        return np.nan
    return {"m": n / 60, "h": n, "d": n * 24}.get(tok[-1:], np.nan)


def market_state(grid: np.ndarray, P: np.ndarray, col: dict[str, int]) -> pd.DataFrame:
    """Per hour bucket: median alt 24h / 7d log return and the alt up (+) / down (-) day streak up to the previous day."""
    keep = [i for k, i in col.items() if k.split("-")[0] not in regime.EXCL]
    L = np.log(P[:, keep])
    with np.errstate(invalid="ignore", divide="ignore"):
        r24 = np.full(len(grid), np.nan)
        r7d = np.full(len(grid), np.nan)
        r24[24:] = np.nanmedian(L[24:] - L[:-24], axis=1)
        r7d[168:] = np.nanmedian(L[168:] - L[:-168], axis=1)
    day = grid // D
    last = pd.Series(np.arange(len(grid))).groupby(day).max()          # last hour bucket of each UTC day
    dret = pd.Series(r24[last.to_numpy()], index=last.index)
    st = regime.streaks(dret)
    streak = (st["up"] - st["dn"]).shift(1)                            # the day's own close is not known yet
    return pd.DataFrame({"alt24": r24, "alt7d": r7d, "streak": streak.reindex(day).to_numpy(float)}, index=grid)


def trader_stats(cand: pd.DataFrame, rand: pd.DataFrame, t0: int, t1: int) -> tuple[np.ndarray, list[pd.DataFrame]]:
    steps = np.arange((t0 // D) * D + pool.STEP_D * D, t1 + pool.STEP_D * D, pool.STEP_D * D, dtype=np.int64)
    return steps, [pool.window_stats(cand, rand, int(t), LOOK) for t in steps]


def features(sig: pd.DataFrame, steps: np.ndarray, stats: list[pd.DataFrame], mkt: pd.DataFrame, cats: dict[str, list[str]]) -> pd.DataFrame:
    f = pd.DataFrame([json.loads(x) if x else {} for x in sig["features"]], index=sig.index)
    cols: dict[str, Any] = {c: pd.to_numeric(f[c], errors="coerce") if c in f else np.nan for c in NUM}
    cols["age_d"] = np.minimum(cols["age_d"], 3650)
    cols["log_turnover"] = np.log10(np.clip(pd.to_numeric(f["turnover24"], errors="coerce"), 1, None)) if "turnover24" in f else np.nan
    cols["btc_regime"] = f["btc_regime"].map(ml.CAT["btc_regime"]).fillna(-1) if "btc_regime" in f else -1.0
    cols["sgn"] = sig["sgn"].astype(float)
    cols["hold_h"] = sig["exit_rule"].map(hold_hours)
    cols["stop"] = sig["stop"]
    for c in CATS:
        v = sig[c].fillna("?")
        cols[c] = pd.Categorical(v.where(v.isin(cats[c])), categories=cats[c])     # unseen (new traders live) -> missing
    si = np.searchsorted(steps, sig["entry_ts"].to_numpy(np.int64), side="right") - 1
    for c in ("n", "mean", "ex", "t"):
        v = np.full(len(sig), np.nan)
        for i in np.unique(si[si >= 0]):
            m = si == i
            v[m] = sig["trader_id"][m].map(stats[i][c]).to_numpy(float) if c in stats[i] else np.nan
        cols[f"tr_{c}"] = v
    cols["tr_n"] = np.nan_to_num(cols["tr_n"])
    hi = np.searchsorted(mkt.index.to_numpy(), (sig["entry_ts"].to_numpy(np.int64) // H) * H) - 1   # last finished hour
    ok = hi >= 0
    for c in mkt.columns:
        v = np.full(len(sig), np.nan)
        v[ok] = mkt[c].to_numpy(float)[hi[ok]]
        cols[c] = v
    return pd.DataFrame(cols, index=sig.index)


def load_source(cfg: Config, source: str) -> dict[str, Any] | None:
    from okxrev import replay
    pos, kinds = load_signals(cfg, source)
    if pos.empty:
        return None
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]))
    pos = pool.prepare(pos, cost)
    pos["kind"] = pos["trader_id"].map(kinds).fillna("?")
    pos["family"] = pos["trader_id"].map(pool.family)
    rand = pos[pos["kind"].eq("random")]
    cand = pos[~pos["kind"].isin(pool.EXCL_KINDS) & ~pos["trader_id"].str.endswith("_ctrl")]
    t0, t1 = int(pos["entry_ts"].min()), int(pos["exit_ts"].max())
    path = cfg.paths.db if source == "live" else replay.old_db_path(cfg)
    grid, P, col = pool.hourly_close(path, sorted(pos["inst_id"].unique()), t0 - 8 * D, t1 + 2 * H)
    steps, stats = trader_stats(cand, rand, t0, t1)
    log.info("poolml %s: %d candidate signals, %s .. %s", source, len(cand), ms_to_str(t0), ms_to_str(t1))
    return {"pos": pos, "cand": cand, "rand": rand, "t0": t0, "t1": t1, "grid": grid, "P": P, "col": col,
            "steps": steps, "stats": stats, "mkt": market_state(grid, P, col)}


# ---------------------------------------------------------------- model
def fit(X: pd.DataFrame, y: np.ndarray) -> ml.Booster:
    return ml.Booster(300, learning_rate=0.03, num_leaves=31, min_data_in_leaf=300, bagging_fraction=0.8, bagging_freq=1,
                      feature_fraction=0.8, lambda_l2=1.0, cat_smooth=50, seed=0).fit(X, y)


def train_at(X: pd.DataFrame, y: np.ndarray, sig: pd.DataFrame, cut: int) -> tuple[ml.Booster, dict[float, float], dict[str, Any]] | None:
    """Model trained on exits before cut - HOLD_D, thresholds from its scores on the hold-out ending at cut."""
    ho_lo = cut - HOLD_D * D
    tr = (sig["exit_ts"] < ho_lo).to_numpy()
    ho = ((sig["entry_ts"] >= ho_lo) & (sig["exit_ts"] < cut)).to_numpy()
    if tr.sum() < 5_000 or ho.sum() < 500:
        return None
    m = fit(X[tr], y[tr])
    ph = m.predict_proba(X[ho])[:, 1]
    thr = {f: float(np.quantile(ph, 1 - f)) for f in FRACS}
    return m, thr, {"n_train": int(tr.sum()), "n_hold": int(ho.sum()), "hold_auc": ml.auc_score(y[ho], ph)}


def per_trade(ret: pd.Series, sel: np.ndarray) -> dict[str, Any]:
    s = ret[sel]
    return {"n_all": int(len(ret)), "n_sel": int(len(s)), "all_mean": float(ret.mean()) if len(ret) else None,
            "sel_mean": float(s.mean()) if len(s) else None, "all_win": float((ret > 0).mean()) if len(ret) else None,
            "sel_win": float((s > 0).mean()) if len(s) else None}


def month_starts(lo: int, hi: int) -> list[int]:
    a = pd.Timestamp(lo, unit="ms").to_period("M").to_timestamp() + pd.offsets.MonthBegin(1)
    return [int(t.value // 1_000_000) for t in pd.date_range(a, pd.Timestamp(hi, unit="ms"), freq="MS")]


def pool_members(S: dict[str, Any]) -> tuple[pd.DataFrame, int]:
    """The pool's main member rule (top K by t over the last LOOK days, weekly), as in okxrev.pool.run_source."""
    k, look = pool.MAIN
    start = (S["t0"] // D) * D + look * D
    steps = np.arange(start, S["t1"], pool.STEP_D * D, dtype=np.int64)
    if not len(steps):
        return S["cand"].iloc[:0], start
    mem = [[t for t, _ in pool.pick(pool.window_stats(S["cand"], S["rand"], int(t), look), k, pool.MIN_N[look])] for t in steps]
    return pool.schedule_entries(S["cand"], steps, mem), int(steps[0])


def random_same(cand: pd.DataFrame, sel: pd.Series, months: list[int], sim: Any, rng: np.random.Generator, target: float) -> dict[str, Any]:
    """Random subsets with the model's count per month, through the same pool."""
    ent_ts = cand["entry_ts"].to_numpy(np.int64)
    edges = months + [np.iinfo(np.int64).max]
    groups = [(np.flatnonzero((ent_ts >= a) & (ent_ts < b)), int(sel[(ent_ts >= a) & (ent_ts < b)].sum())) for a, b in zip(edges[:-1], edges[1:])]
    rets = []
    for _ in range(SIMS):
        idx = np.concatenate([rng.choice(g, size=min(c, len(g)), replace=False) for g, c in groups if len(g)])
        rets.append(sim(cand.iloc[np.sort(idx)])["ret"])
    a = np.array(rets)
    return {"p05": float(np.quantile(a, 0.05)), "p50": float(np.median(a)), "p95": float(np.quantile(a, 0.95)), "sims": len(a),
            "beat": float((a < target).mean())}


def summary(res: dict[str, Any]) -> dict[str, Any]:
    keep = ("ret", "max_dd", "underwater_days", "worst_week", "worst_30d", "pos_weeks", "trades", "signals", "halts", "halves",
            "best_week", "ret_ex_best_week", "curve", "by_member")
    return {k: res[k] for k in keep if k in res}


# ---------------------------------------------------------------- evaluation
def run_blind(S: dict[str, Any], cats: dict[str, list[str]]) -> tuple[dict[str, Any], tuple[ml.Booster, dict[float, float]] | None, list[str]]:
    cand = S["cand"]
    X = features(cand, S["steps"], S["stats"], S["mkt"], cats)
    y = (cand["ret"] > 0).to_numpy(int)
    cols = list(X.columns)
    months = month_starts(S["t0"] + TRAIN_D * D - D, S["t1"])
    score = pd.Series(np.nan, index=cand.index)
    sel = {f: pd.Series(False, index=cand.index) for f in FRACS}
    mrows, gains = [], []
    edges = months + [S["t1"] + D]
    for a, b in zip(edges[:-1], edges[1:]):
        tm = train_at(X, y, cand, a)
        te = ((cand["entry_ts"] >= a) & (cand["entry_ts"] < b)).to_numpy()
        if tm is None or not te.any():
            continue
        m, thr, info = tm
        p = m.predict_proba(X[te])[:, 1]
        score[te] = p
        for f in FRACS:
            sel[f][te] = p >= thr[f]
        g = m.booster_.feature_importance("gain")
        gains.append(g / max(g.sum(), 1e-12))
        r = cand["ret"][te]
        mrows.append({"month": ms_to_str(a, "%Y-%m"), **info, "n": int(te.sum()), "auc": ml.auc_score(y[te], p), "thr": thr[MAIN_F],
                      **per_trade(r, (p >= thr[MAIN_F]))})
        log.info("poolml month %s: auc %.3f, all %+.2f%%, picked %d (%+.2f%%)", mrows[-1]["month"], mrows[-1]["auc"],
                 (mrows[-1]["all_mean"] or 0) * 100, mrows[-1]["n_sel"], (mrows[-1]["sel_mean"] or 0) * 100)
    oos = score.notna().to_numpy()
    if not oos.any():
        return {}, None, cols
    start = int(cand["entry_ts"][oos].min()) // D * D
    sim = lambda e, **kw: pool.simulate(e, S["grid"], S["P"], S["col"], start, **kw)  # noqa: E731
    ent_main, _ = pool_members(S)
    in_main = ent_main.index
    runs = {"pool_main": summary(sim(ent_main, detail=True)), "all": summary(sim(cand[oos], detail=True)),
            "pool_model": summary(sim(ent_main[sel[MAIN_F].reindex(in_main).fillna(False).to_numpy()], detail=True)),
            "model": {str(f): summary(sim(cand[sel[f].to_numpy()], detail=f == MAIN_F)) for f in FRACS}}
    rnd = random_same(cand[oos], sel[MAIN_F][oos], [m for m in months if m >= start] or [start], sim, np.random.default_rng(0),
                      runs["model"][str(MAIN_F)]["ret"])
    imp = np.mean(gains, axis=0)
    out = {"range": [ms_to_str(S["t0"], "%Y-%m-%d"), ms_to_str(S["t1"], "%Y-%m-%d")], "oos_start": ms_to_str(start, "%Y-%m-%d"),
           "months": mrows, "runs": runs, "random_same": rnd,
           "per_trade": {str(f): per_trade(cand["ret"][oos], sel[f][oos].to_numpy()) for f in FRACS},
           "pool_model_trade": per_trade(ent_main["ret"][ent_main["entry_ts"] >= start], sel[MAIN_F].reindex(in_main).fillna(False)[ent_main["entry_ts"] >= start].to_numpy()),
           "importance": sorted(({"feature": c, "gain": float(v)} for c, v in zip(cols, imp)), key=lambda x: -x["gain"])}
    final = train_at(X, y, cand, S["t1"] + H)
    return out, (final[0], final[1]) if final else None, cols


def run_live(S: dict[str, Any], cats: dict[str, list[str]], model: tuple[ml.Booster, dict[float, float]], cols: list[str]) -> dict[str, Any]:
    cand = S["cand"]
    X = features(cand, S["steps"], S["stats"], S["mkt"], cats)[cols]
    m, thr = model
    p = m.predict_proba(X)[:, 1]
    y = (cand["ret"] > 0).to_numpy(int)
    sel = p >= thr[MAIN_F]
    start = S["t0"] // D * D
    sim = lambda e, **kw: pool.simulate(e, S["grid"], S["P"], S["col"], start, **kw)  # noqa: E731
    runs = {"model": summary(sim(cand[sel], detail=True)), "all": summary(sim(cand, detail=True))}
    rnd = random_same(cand, pd.Series(sel, index=cand.index), [start], sim, np.random.default_rng(0), runs["model"]["ret"])
    unseen = sorted(set(cand["family"]) - set(cats["family"]))
    return {"range": [ms_to_str(S["t0"], "%Y-%m-%d"), ms_to_str(S["t1"], "%Y-%m-%d")], "auc": ml.auc_score(y, p),
            "per_trade": per_trade(cand["ret"], sel), "unseen_families": unseen,
            "unseen_share": float(cand["family"].isin(unseen).mean()),
            "runs": runs, "random_same": rnd}


def evaluate(cfg: Config) -> dict[str, Any]:
    t0 = time.monotonic()
    B = load_source(cfg, "blind")
    if B is None:
        raise SystemExit("no blind replay: python -m okxrev.replay first")
    cats = {c: sorted(B["cand"][c].fillna("?").unique().tolist()) for c in CATS}
    blind, final, cols = run_blind(B, cats)
    live = None
    if final is not None and (L := load_source(cfg, "live")) is not None:
        live = run_live(L, cats, final, cols)
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1), "blind": blind, "live": live,
           "params": {"fracs": list(FRACS), "main_f": MAIN_F, "train_d": TRAIN_D, "hold_d": HOLD_D, "look": LOOK, "sims": SIMS,
                      "features": cols, "label": "stop-adjusted net return > 0"},
           "caveats": [f"主要設定（只接分數前 {MAIN_F:.0%}）是事先定的；10%、40% 只是參考",
                       f"每月重訓：只用該月開始前 {HOLD_D} 天以前已平倉的交易訓練，門檻用最後 {HOLD_D} 天的保留資料決定",
                       "同一時間很多交易員對同一波行情進場，訊號彼此高度相關，有效樣本比筆數少很多",
                       "盲測期間沒有資金費率與 OI 的歷史，模型在盲測裡看不到這兩項",
                       "即時期間的新交易員家族在盲測裡沒有，模型把它們當成未知",
                       "盲測期間只有現存的老合約（倖存者偏差）"]}
    p = cfg.paths.results / "pool_ml.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    log.info("poolml: done in %.0fs", res["elapsed_sec"])
    return res


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "pool_ml.json"
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null").replace("Infinity", "null")) if p.exists() else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.poolml")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "poolml")
    res = evaluate(cfg) if args.evaluate else load(cfg)
    if not res:
        print("no results yet: python -m okxrev.poolml --evaluate")
        return 0
    b = res.get("blind") or {}
    if b:
        print(f"== blind, scored from {b['oos_start']}")
        for m in b["months"]:
            print(f"  {m['month']}  auc {m['auc']:.3f}  hold-out auc {m['hold_auc']:.3f}  all {m['all_mean'] * 100:+.2f}%  "
                  f"picked {m['n_sel']:5d}/{m['n']:5d} {((m['sel_mean'] or 0) * 100):+.2f}%")
        for name, r in [("pool main", b["runs"]["pool_main"]), ("pool + model", b["runs"]["pool_model"]), ("all signals", b["runs"]["all"])] + \
                [(f"model top {float(f):.0%}", r) for f, r in b["runs"]["model"].items()]:
            print(f"  {name:16s} ret {r['ret'] * 100:+7.1f}%  dd {r['max_dd'] * 100:5.1f}%  trades {r['trades']:5d}  "
                  f"ex best week {r.get('ret_ex_best_week', float('nan')) * 100:+6.1f}%  halves {[round(h * 100, 1) for h in r.get('halves', [])]}")
        rs = b["random_same"]
        print(f"  random same size  {rs['p05'] * 100:+.1f} / {rs['p50'] * 100:+.1f} / {rs['p95'] * 100:+.1f}%  model top {MAIN_F:.0%} beats {rs['beat']:.0%}")
        print("  importance: " + ", ".join(f"{x['feature']} {x['gain']:.2f}" for x in b["importance"][:10]))
    lv = res.get("live")
    if lv:
        pt = lv["per_trade"]
        print(f"== live {lv['range'][0]} .. {lv['range'][1]}  auc {lv['auc']:.3f}  all {pt['all_mean'] * 100:+.2f}% ({pt['n_all']})  "
              f"picked {(pt['sel_mean'] or 0) * 100:+.2f}% ({pt['n_sel']})  unseen families {lv['unseen_share']:.0%}")
        for k, r in lv["runs"].items():
            print(f"  {k:8s} ret {r['ret'] * 100:+6.1f}%  dd {r['max_dd'] * 100:4.1f}%  trades {r['trades']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
