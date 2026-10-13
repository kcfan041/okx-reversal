"""LightGBM entry models.

ml   : predicts P(net return > 0) for a fixed exit, separately for long and short, trained on market-wide
       sampled bars from the candle history (only data strictly before the training cutoff).
twin : learns where *you* enter (positives = your manual entries, negatives = other bars of the coins/times you
       were active), so the paper trader can imitate your entry selection with a better exit.

Walk-forward evaluation is run once per training to report what the chosen threshold would have done out of
sample; the live model is retrained weekly and every version is kept.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.ml")

DAY = 86_400_000
BAR = db.BAR_MS
NUM = ["r1h", "r6h", "r24h", "r3d", "ret5", "vol_ratio", "vmax_1h", "pos24", "pos7d", "dist_ema", "vol1d", "vol_rank", "atr",
       "btc_r1h", "btc_r4h", "breadth", "age_d", "funding", "oi_chg"]
CAT = {"btc_regime": {"strong": 2, "neutral": 1, "weak": 0, "unknown": -1}}
FLAGS = ["pump", "red_k", "pump_red_24h", "new_high24", "new_low24"]


def matrix(f: pd.DataFrame) -> pd.DataFrame:
    """Model input matrix; built column-wise into a dict and assembled once (no per-column DataFrame inserts)."""
    n = len(f)
    nan = np.full(n, np.nan)
    cols: dict[str, np.ndarray] = {}
    for c in NUM:
        cols[c] = pd.to_numeric(f[c], errors="coerce").to_numpy(dtype=float) if c in f else nan
    cols["log_turnover"] = (np.log10(np.clip(pd.to_numeric(f["turnover24"], errors="coerce").to_numpy(dtype=float), 1, None))
                            if "turnover24" in f else nan)
    cols["age_d"] = np.minimum(cols["age_d"], 3650)
    for c, m in CAT.items():
        cols[c] = f[c].map(m).fillna(-1).to_numpy(dtype=float) if c in f else np.full(n, -1.0)
    for c in FLAGS:
        cols[c] = f[c].to_numpy(dtype=float) if c in f else nan
    cols["hour"] = (f.index.to_numpy(np.int64) // 3_600_000) % 24
    return pd.DataFrame(cols, index=f.index)


# ---------------------------------------------------------------- dataset
def build_dataset(cfg: Config, exit_name: str, stride: int, since: int | None = None, insts: list[str] | None = None) -> pd.DataFrame:
    from okxrev.derivs import attach, load_funding, load_oi
    from okxrev.detector import btc_features, prepare
    from okxrev.discover import features, market_breadth, outcomes
    det, reg = cfg.detector, cfg.btc_regime
    fee, slip = float(cfg.metrics["fee_per_side"]), float(cfg.metrics["slippage_per_side"])
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=3.0, max_tries=100) as con:
        all_insts = db.list_candle_insts(con)
        meta = db.sync_overview(con).set_index("inst_id")
        btc = btc_features(db.load_candles(con, cfg.universe["btc_inst"], since), reg)
    targets = insts or all_insts
    breadth = market_breadth(cfg, targets)
    rows = []
    for n, inst in enumerate(targets, 1):
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=3.0, max_tries=100) as con:
            raw = db.load_candles(con, inst, since)
            fund, oi = load_funding(con, inst), load_oi(con, inst)
        if len(raw) < 1200:
            continue
        lt = meta.at[inst, "list_time"] if inst in meta.index else None
        lt = None if lt is None or pd.isna(lt) else int(lt)
        g = prepare(raw, det, btc, list_time=lt)
        f = features(g, det, breadth, lt, attach(g, fund, oi))
        f["breadth"] = pd.Series(f["breadth"].to_numpy(), index=g.index).ffill(limit=12).to_numpy()
        small = float(np.nanmedian(f["turnover24"].to_numpy())) < 5e6
        cost = 2 * (fee + slip + (0.001 if small else 0.0))
        out = outcomes(g, f["atr"].to_numpy(float), cost)
        off = int(abs(hash(inst)) % stride)
        pick = np.arange(off + 300, len(g), stride)
        if len(pick) == 0:
            continue
        X = matrix(f.iloc[pick])
        X["y_long"] = out["L_" + exit_name][pick]
        X["y_short"] = out["S_" + exit_name][pick]
        X["inst"] = inst
        X["ts"] = g.index.to_numpy(np.int64)[pick]
        rows.append(X.reset_index(drop=True))
        if n % 100 == 0:
            log.info("dataset %d/%d", n, len(targets))
    d = pd.concat(rows, ignore_index=True)
    return d[d["r24h"].notna()]


def feature_cols(d: pd.DataFrame) -> list[str]:
    return [c for c in d.columns if c not in ("y_long", "y_short", "inst", "ts", "y")]


def auc_score(y: np.ndarray, p: np.ndarray) -> float:
    r = pd.Series(p).rank().to_numpy()
    pos = y == 1
    n1, n0 = pos.sum(), (~pos).sum()
    return float((r[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


class Booster:
    """Minimal classifier wrapper over the native LightGBM API (no scikit-learn dependency)."""

    def __init__(self, rounds: int = 300, **params: Any):
        self.rounds = rounds
        self.params = {"objective": "binary", "verbose": -1, **params}
        self.booster_: Any = None

    def fit(self, X: pd.DataFrame, y: Any) -> "Booster":
        import lightgbm as lgb
        y = np.asarray(y, dtype=float)
        p = dict(self.params)
        if n_threads():
            p.setdefault("num_threads", n_threads())
        if p.pop("balanced", False):
            pos = max(y.sum(), 1.0)
            p["scale_pos_weight"] = float((len(y) - pos) / pos)
        self.booster_ = lgb.train(p, lgb.Dataset(X, label=y, free_raw_data=True), num_boost_round=self.rounds)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        p = self.booster_.predict(X, num_threads=n_threads())
        return np.column_stack([1 - p, p])


def n_threads() -> int:
    """Thread budget for LightGBM; background processes get OKXREV_THREADS (default 2), interactive runs use 0 (all)."""
    import os
    v = os.environ.get("OKXREV_THREADS")
    return int(v) if v and v.isdigit() else 0


def fit(X: pd.DataFrame, y: np.ndarray, seed: int = 0) -> Booster:
    return Booster(300, learning_rate=0.03, num_leaves=31, min_data_in_leaf=200, bagging_fraction=0.8, bagging_freq=1,
                   feature_fraction=0.8, lambda_l2=1.0, seed=seed).fit(X, y)


def walk_forward(d: pd.DataFrame, side: str, q: float, n_folds: int = 4, test_days: int = 45) -> dict[str, Any]:
    """Expanding-window evaluation: train on everything before each test window, pick threshold = q-quantile of
    train predictions, then measure the selected test bars' net return vs all test bars."""
    ycol = "y_" + side
    d = d[d[ycol].notna()].sort_values("ts")
    end = int(d["ts"].max())
    folds = []
    cols = feature_cols(d)
    for k in range(n_folds, 0, -1):
        t_lo = end - k * test_days * DAY
        t_hi = t_lo + test_days * DAY
        tr = d[d["ts"] < t_lo - DAY]
        te = d[(d["ts"] >= t_lo) & (d["ts"] < t_hi)]
        if len(tr) < 20_000 or len(te) < 2_000:
            continue
        m = fit(tr[cols], (tr[ycol] > 0).astype(int).to_numpy())
        thr = float(np.quantile(m.predict_proba(tr[cols])[:, 1], q))
        p = m.predict_proba(te[cols])[:, 1]
        sel = te[p >= thr]
        folds.append({"test_start": ms_to_str(t_lo), "test_end": ms_to_str(t_hi), "n_train": int(len(tr)), "n_test": int(len(te)),
                      "threshold": thr, "n_sel": int(len(sel)), "sel_mean": float(sel[ycol].mean()) if len(sel) else None,
                      "sel_win": float((sel[ycol] > 0).mean()) if len(sel) else None, "all_mean": float(te[ycol].mean()),
                      "sel_days": int(sel["ts"].floordiv(DAY).nunique()) if len(sel) else 0})
    good = [f for f in folds if f["sel_mean"] is not None]
    return {"folds": folds, "mean_sel": float(np.mean([f["sel_mean"] for f in good])) if good else None,
            "mean_all": float(np.mean([f["all_mean"] for f in folds])) if folds else None,
            "positive_folds": int(sum(1 for f in good if f["sel_mean"] > 0)), "n_folds": len(folds)}


# ---------------------------------------------------------------- storage
def model_dir(cfg: Config, name: str) -> Path:
    p = cfg.models_dir / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def save(cfg: Config, name: str, side: str, obj: dict[str, Any]) -> Path:
    d = model_dir(cfg, name)
    p = d / f"{side}_{obj['version']}.lgb"
    p.write_text(obj["model"].booster_.model_to_string(), encoding="utf-8")
    meta = {k: v for k, v in obj.items() if k != "model"}
    (d / f"{side}_{obj['version']}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return p


def load_latest(cfg: Config, name: str, side: str) -> dict[str, Any] | None:
    import lightgbm as lgb
    files = sorted(model_dir(cfg, name).glob(f"{side}_*.lgb"))
    if not files:
        return None
    meta = json.loads(files[-1].with_suffix(".json").read_text(encoding="utf-8"))
    b = Booster()
    b.booster_ = lgb.Booster(model_str=files[-1].read_text(encoding="utf-8"))
    return {**meta, "model": b}


def has_model(cfg: Config, name: str) -> bool:
    return any(model_dir(cfg, name).glob("*.lgb"))


def latest_meta(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("ml", "twin"):
        d = cfg.models_dir / name
        if not d.exists():
            continue
        for side in ("long", "short"):
            js = sorted(d.glob(f"{side}_*.json"))
            if js:
                out[f"{name}_{side}"] = {**json.loads(js[-1].read_text(encoding="utf-8")), "n_versions": len(js)}
    return out


def predict(m: dict[str, Any], f: pd.DataFrame) -> np.ndarray:
    X = matrix(f)[m["features"]]
    return m["model"].predict_proba(X)[:, 1]


def needs_retrain(cfg: Config, name: str) -> bool:
    days = float(cfg.paper.get("ml", {}).get("retrain_days", 7))
    files = sorted(model_dir(cfg, name).glob("*.lgb"))
    return not files or time.time() - files[-1].stat().st_mtime > days * 86400


# ---------------------------------------------------------------- training
def train_ml(cfg: Config) -> dict[str, Any]:
    mc = cfg.paper.get("ml", {})
    exit_name = mc.get("exit", "6h_sl2atr")
    stride = int(mc.get("stride", 24))
    q = float(mc.get("top_quantile", 0.995))
    since = db.now_ms() - int(mc.get("train_days", 365)) * DAY
    t0 = time.monotonic()
    d = build_dataset(cfg, exit_name, stride, since)
    hold = int(cfg.metrics["horizons"].get("6h", 72)) if exit_name.startswith("6h") else 288
    cutoff = int(d["ts"].max()) - hold * BAR
    d = d[d["ts"] <= cutoff]
    version = ms_to_str(db.now_ms(), "%Y%m%d_%H%M")
    out = {}
    cols = feature_cols(d)
    for side in ("long", "short"):
        wf = walk_forward(d, side, q)
        ycol = "y_" + side
        dd = d[d[ycol].notna()]
        m = fit(dd[cols], (dd[ycol] > 0).astype(int).to_numpy())
        thr = max(float(np.quantile(m.predict_proba(dd[cols])[:, 1], q)), float(mc.get("min_prob", 0.5)))
        imp = sorted(zip(cols, m.booster_.feature_importance("gain")), key=lambda x: -x[1])[:15]
        obj = {"name": "ml", "side": side, "version": version, "model": m, "features": cols, "threshold": thr, "exit": exit_name,
               "train_end": ms_to_str(cutoff), "n_train": int(len(dd)), "base_rate": float((dd[ycol] > 0).mean()),
               "walk_forward": wf, "importance": [{"feature": k, "gain": float(v)} for k, v in imp]}
        save(cfg, "ml", side, obj)
        out[side] = {k: v for k, v in obj.items() if k != "model"}
        log.info("ml %s: thr %.3f, wf sel %.4f vs all %.4f (%d/%d folds positive)", side, thr, wf["mean_sel"] or 0, wf["mean_all"] or 0,
                 wf["positive_folds"], wf["n_folds"])
    out["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return out


def twin_dataset(cfg: Config) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Positives: bars where you opened a manual SWAP position. Negatives: every `stride`-th bar of the same coins
    within ±3 days of your trades (the situations you watched but did not trade)."""
    from okxrev import account
    from okxrev.derivs import attach, load_funding, load_oi
    from okxrev.detector import btc_features, prepare
    from okxrev.discover import features, market_breadth
    t = account.load_trades(cfg)
    t = t[(t["inst_type"] == "SWAP") & t["inst_id"].str.endswith("-USDT-SWAP") & t["side"].isin(["long", "short"])]
    if t.empty:
        return pd.DataFrame(), {}
    stride = int(cfg.paper.get("twin", {}).get("stride", 24))
    det, reg = cfg.detector, cfg.btc_regime
    lo_all = int(t["open_ts"].min()) - 10 * DAY
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=3.0, max_tries=100) as con:
        meta = db.sync_overview(con).set_index("inst_id")
        btc = btc_features(db.load_candles(con, cfg.universe["btc_inst"], lo_all), reg)
        have = set(db.list_candle_insts(con))
    breadth = market_breadth(cfg, sorted(have))
    rows = []
    for inst, tr in t.groupby("inst_id"):
        if inst not in have:
            continue
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=3.0, max_tries=100) as con:
            raw = db.load_candles(con, inst, int(tr["open_ts"].min()) - 10 * DAY, int(tr["open_ts"].max()) + 4 * DAY)
            fund, oi = load_funding(con, inst), load_oi(con, inst)
        if len(raw) < 900:
            continue
        lt = meta.at[inst, "list_time"] if inst in meta.index else None
        lt = None if lt is None or pd.isna(lt) else int(lt)
        g = prepare(raw, det, btc, list_time=lt)
        f = features(g, det, breadth, lt, attach(g, fund, oi))
        f["breadth"] = pd.Series(f["breadth"].to_numpy(), index=g.index).ffill(limit=12).to_numpy()
        idx = g.index.to_numpy(np.int64)
        pos_bars = {}
        for r in tr.itertuples(index=False):
            k = int(np.searchsorted(idx, (int(r.open_ts) // BAR) * BAR) - 1)
            if 0 <= k < len(idx):
                pos_bars[k] = r.side
        window = np.zeros(len(idx), bool)
        for k in pos_bars:
            window[max(0, k - 864):k + 864] = True
        neg = [k for k in np.flatnonzero(window)[::stride] if all(abs(k - p) > 12 for p in pos_bars)]
        for side in ("long", "short"):
            ks = [k for k, s in pos_bars.items() if s == side]
            sel = np.array(ks + neg, dtype=int)
            if len(ks) == 0 or len(sel) == 0:
                continue
            X = matrix(f.iloc[sel])
            X["y"] = np.r_[np.ones(len(ks)), np.zeros(len(neg))]
            X["side"] = side
            X["inst"] = inst
            X["ts"] = idx[sel]
            rows.append(X.reset_index(drop=True))
    d = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return d, {"n_trades": int(len(t)), "your_hold_bars": int(max(1, round(t["hold_min"].median() / 5)))}


def train_twin(cfg: Config) -> dict[str, Any]:
    from okxrev import exits
    d, info = twin_dataset(cfg)
    if d.empty:
        return {"note": "no manual trades"}
    plan = exits.load_plan(cfg) or {}
    version = ms_to_str(db.now_ms(), "%Y%m%d_%H%M")
    out: dict[str, Any] = {}
    for side in ("long", "short"):
        dd = d[d["side"] == side].drop(columns="side")
        npos = int(dd["y"].sum())
        if npos < 8:
            out[side] = {"note": f"只有 {npos} 筆{side}，不訓練"}
            continue
        cols = feature_cols(dd)
        m = Booster(200, learning_rate=0.05, num_leaves=7, min_data_in_leaf=5, bagging_fraction=0.8, bagging_freq=1,
                    feature_fraction=0.8, lambda_l2=2.0, balanced=True, seed=0)
        dd = dd.sort_values("ts")
        cut = int(dd["ts"].quantile(0.7))
        tr, te = dd[dd["ts"] <= cut], dd[dd["ts"] > cut]
        auc = None
        thr, neg_rate = None, None
        if tr["y"].sum() >= 4 and te["y"].sum() >= 2 and (te["y"] == 0).sum() >= 2:
            m.fit(tr[cols], tr["y"].astype(int))
            pte = m.predict_proba(te[cols])[:, 1]
            yte = te["y"].to_numpy()
            auc = auc_score(yte, pte)
            thr = float(np.quantile(pte[yte == 1], 0.5))
            neg_rate = float((pte[yte == 0] >= thr).mean())
        m.fit(dd[cols], dd["y"].astype(int))
        if thr is None:
            p = m.predict_proba(dd[cols])[:, 1]
            thr = float(np.quantile(p[dd["y"].to_numpy() == 1], 0.5))
            neg_rate = float((p[dd["y"].to_numpy() == 0] >= thr).mean())
        g = (plan.get("groups") or {}).get(side) or (plan.get("groups") or {}).get("all") or {}
        rec = g.get("recommended", {})
        rule = rec.get("rule", {})
        ex = "6h_sl2atr" if (rule.get("time_bars") or 72) <= 72 else "24h_sl2atr"
        imp = sorted(zip(cols, m.booster_.feature_importance("gain")), key=lambda x: -x[1])[:12]
        obj = {"name": "twin", "side": side, "version": version, "model": m, "features": cols, "threshold": max(thr, 0.3), "exit": ex,
               "n_pos": npos, "n_neg": int((dd["y"] == 0).sum()), "holdout_auc": auc, "neg_fire_rate": neg_rate,
               "your_hold_bars": info.get("your_hold_bars"), "exit_from_plan": rec.get("text"),
               "importance": [{"feature": k, "gain": float(v)} for k, v in imp],
               "note": "樣本少，只用來模仿你的進場選擇；可信度低"}
        save(cfg, "twin", side, obj)
        out[side] = {k: v for k, v in obj.items() if k != "model"}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.ml")
    ap.add_argument("--config", default=None)
    ap.add_argument("--train", choices=["ml", "twin", "all"], default="all")
    ap.add_argument("--if-due", action="store_true", help="only retrain when the latest model is older than retrain_days")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "ml")
    from filelock import FileLock, Timeout
    cfg.models_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(cfg.models_dir / "train.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("training already running")
        return 0
    res = {}
    try:
        if args.train in ("ml", "all") and (not args.if_due or needs_retrain(cfg, "ml")):
            res["ml"] = train_ml(cfg)
        if args.train in ("twin", "all") and (not args.if_due or needs_retrain(cfg, "twin")):
            res["twin"] = train_twin(cfg)
    finally:
        lock.release()
    print(json.dumps(res, ensure_ascii=False, indent=1, default=float)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
