from __future__ import annotations

import itertools
import json
import logging
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config
from okxrev.detector import apply_cooldown, btc_features, prepare
from okxrev.features import CONDITIONS, as_arrays, bar_features, rule_label, rule_mask
from okxrev.metrics import bootstrap_ci

log = logging.getLogger("okxrev.replicate")

MIN_OOS = 30


def candidate_rules(et: pd.DataFrame, min_n: int, max_rules: int = 10) -> list[dict[str, Any]]:
    p = et[et["path_ok"] & et["pnl_usd"].notna()]
    cand: list[dict[str, Any]] = []
    for side in ("long", "short"):
        s = p[p["side"] == side]
        if len(s) < min_n:
            continue
        keys = [k for k in CONDITIONS if f"c_{k}" in s and s[f"c_{k}"].fillna(False).astype(bool).sum() >= min_n]
        combos: list[tuple[str, ...]] = [(k,) for k in keys]
        combos += [c for c in itertools.combinations(keys, 2) if CONDITIONS[c[0]][0] != CONDITIONS[c[1]][0]]
        for conds in combos:
            m = np.ones(len(s), bool)
            for c in conds:
                m &= s[f"c_{c}"].fillna(False).astype(bool).to_numpy()
            sub = s[m]
            if len(sub) < min_n:
                continue
            wr = float((sub["pnl_usd"] > 0).mean())
            if sub["pnl_usd"].sum() <= 0 or wr < 0.5:
                continue
            winners = sub[sub["pnl_usd"] > 0]
            hold = int(np.clip(np.nanmedian(sub["hold_bars"]), 1, 864))
            stop = float(np.clip(np.nanquantile(winners["mae"], 0.9) * 1.2 if len(winners) else 0.1, 0.02, 0.5))
            cand.append({
                "side": side, "conds": conds, "label": rule_label(side, conds),
                "your_n": int(len(sub)), "your_pnl": float(sub["pnl_usd"].sum()), "your_win_rate": wr,
                "your_avg_ret": float(sub["ret_net"].mean()), "hold_bars": hold, "stop": stop,
                "score": float(sub["pnl_usd"].sum()) * wr,
            })
    cand.sort(key=lambda r: -r["score"])
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, frozenset[str]]] = set()
    for c in cand:
        key = (c["side"], frozenset(c["conds"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= max_rules:
            break
    return out


def simulate(o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, idx: np.ndarray, side: str,
             hold: int, stop: float, cost: float) -> tuple[np.ndarray, np.ndarray]:
    n = len(c)
    s = -1.0 if side == "short" else 1.0
    ret = np.full(len(idx), np.nan)
    mae = np.full(len(idx), np.nan)
    for k, t in enumerate(idx):
        e = t + 1
        x = t + hold
        if x >= n or np.isnan(o[e]) or np.isnan(c[x]):
            continue
        entry = o[e]
        wh, wl = h[e:x + 1], lo[e:x + 1]
        adv = (wh / entry - 1) if s < 0 else (1 - wl / entry)
        adv = np.nan_to_num(adv, nan=0.0)
        hit = np.flatnonzero(adv >= stop)
        if len(hit):
            ret[k] = -stop - cost
        else:
            ret[k] = s * (c[x] / entry - 1) - cost
        mae[k] = float(adv[: hit[0] + 1].max() if len(hit) else adv.max())
    return ret, mae


def _stats(r: np.ndarray, boot: dict[str, Any]) -> dict[str, Any]:
    r = r[~np.isnan(r)]
    if len(r) == 0:
        return {"n": 0}
    lo, hi = bootstrap_ci(r, int(boot["iterations"]), int(boot["seed"])) if len(r) >= 2 else (np.nan, np.nan)
    return {"n": int(len(r)), "mean": float(r.mean()), "median": float(np.median(r)), "win_rate": float((r > 0).mean()),
            "ci95": [None if np.isnan(lo) else float(lo), None if np.isnan(hi) else float(hi)]}


def in_sample_mask(ts: np.ndarray, opens: np.ndarray, window_ms: int = 86_400_000) -> np.ndarray:
    if len(opens) == 0 or len(ts) == 0:
        return np.zeros(len(ts), dtype=bool)
    i = np.searchsorted(opens, ts)
    prev = opens[np.clip(i - 1, 0, len(opens) - 1)]
    nxt = opens[np.clip(i, 0, len(opens) - 1)]
    return (np.abs(ts - prev) <= window_ms) | (np.abs(nxt - ts) <= window_ms)


def verdict(oos: dict[str, Any], base: dict[str, Any], months_pos: float | None) -> str:
    if oos.get("n", 0) < MIN_OOS:
        return "證據不足"
    lo = (oos.get("ci95") or [None])[0]
    bm = base.get("mean", 0.0) or 0.0
    if lo is not None and lo > 0 and oos["mean"] > bm and (months_pos is None or months_pos >= 0.5):
        return "可複製"
    if oos["mean"] > 0 and oos["mean"] > bm:
        return "證據不足"
    return "無效"


def run(cfg: Config, et: pd.DataFrame, rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rc = {"min_trades_per_group": 5, "cooldown_bars": 288, "max_matches_per_rule": 20000, **cfg.account.get("replicate", {})}
    rules = rules if rules is not None else candidate_rules(et, int(rc["min_trades_per_group"]))
    res: dict[str, Any] = {"rules": [], "your_window": None}
    if not rules:
        res["note"] = f"找不到同時滿足「至少 {rc['min_trades_per_group']} 筆、合計獲利、勝率 ≥ 50%」的進場情境"
        _write(cfg, res, pd.DataFrame())
        return res
    w_lo = int(et["open_ts"].min())
    w_hi = int(et["close_ts"].max())
    res["your_window"] = [w_lo, w_hi]
    your_opens = np.sort(et["open_ts"].dropna().astype("int64").to_numpy())
    res["in_sample_def"] = "你任一筆進場前後 24 小時內的時間點；其餘為樣本外"
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]))
    cd = int(rc["cooldown_bars"])
    boot = cfg.backtest["bootstrap"]
    rng = np.random.default_rng(int(cfg.backtest["baseline"]["seed"]))
    btc_id = cfg.universe["btc_inst"]
    hits: list[list[pd.DataFrame]] = [[] for _ in rules]
    bases: list[list[np.ndarray]] = [[] for _ in rules]
    with db.open_db(cfg.paths.db, read_only=True) as con:
        insts = db.list_candle_insts(con)
        meta = db.sync_overview(con).set_index("inst_id")
        btc = btc_features(db.load_candles(con, btc_id), cfg.btc_regime)
    for n_i, inst in enumerate(insts, 1):
        with db.open_db(cfg.paths.db, read_only=True) as con:
            raw = db.load_candles(con, inst)
        if len(raw) < 600:
            continue
        lt = meta.at[inst, "list_time"] if inst in meta.index else None
        lt = None if lt is None or pd.isna(lt) else int(lt)
        g = prepare(raw, cfg.detector, btc, list_time=lt)
        f = as_arrays(bar_features(g, cfg.detector))
        o, h, lo, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        ts = g.index.to_numpy(np.int64)
        valid = np.flatnonzero(~np.isnan(f["r24h"].astype(float)))
        rnd = rng.choice(valid, size=min(40, len(valid)), replace=False) if len(valid) else valid
        for k, rule in enumerate(rules):
            idx = apply_cooldown(rule_mask(f, rule["conds"]), cd)
            if len(idx):
                r, m = simulate(o, h, lo, c, idx, rule["side"], rule["hold_bars"], rule["stop"], cost)
                hits[k].append(pd.DataFrame({"inst_id": inst, "ts": ts[idx], "ret": r, "mae": m}))
            if len(rnd):
                br, _ = simulate(o, h, lo, c, np.sort(rnd), rule["side"], rule["hold_bars"], rule["stop"], cost)
                bases[k].append(br)
        if n_i % 50 == 0:
            log.info("replicate: %d/%d instruments", n_i, len(insts))
    all_hits = []
    for k, rule in enumerate(rules):
        ev = pd.concat(hits[k], ignore_index=True) if hits[k] else pd.DataFrame(columns=["inst_id", "ts", "ret", "mae"])
        ev = ev[ev["ret"].notna()]
        if len(ev) > int(rc["max_matches_per_rule"]):
            ev = ev.sample(int(rc["max_matches_per_rule"]), random_state=0)
        ins = in_sample_mask(ev["ts"].to_numpy(np.int64), your_opens)
        base = np.concatenate(bases[k]) if bases[k] else np.array([])
        months = pd.to_datetime(ev.loc[~ins, "ts"], unit="ms").dt.strftime("%Y-%m")
        by_m = ev.loc[~ins].groupby(months)["ret"].mean()
        mp = float((by_m > 0).mean()) if len(by_m) >= 2 else None
        all_s = _stats(ev["ret"].to_numpy(float), boot)
        is_s = _stats(ev.loc[ins, "ret"].to_numpy(float), boot)
        oos_s = _stats(ev.loc[~ins, "ret"].to_numpy(float), boot)
        base_s = _stats(base, boot)
        regime = {}
        if len(ev):
            reg = btc["btc_regime"].reindex(ev["ts"].to_numpy()).fillna("unknown").to_numpy()
            for r_ in ("strong", "neutral", "weak"):
                x = ev["ret"].to_numpy(float)[reg == r_]
                if len(x):
                    regime[r_] = {"n": int(len(x)), "mean": float(x.mean()), "win_rate": float((x > 0).mean())}
        out = {
            **{k_: v for k_, v in rule.items() if k_ != "score"},
            "conds": list(rule["conds"]),
            "market_all": all_s, "in_sample": is_s, "out_of_sample": oos_s, "baseline": base_s,
            "oos_months_positive": mp, "by_month_oos": {m: float(v) for m, v in by_m.items()},
            "by_btc_regime": regime,
            "mae_median": float(ev["mae"].median()) if len(ev) else None,
            "n_insts": int(ev["inst_id"].nunique()) if len(ev) else 0,
            "verdict": verdict(oos_s, base_s, mp),
        }
        res["rules"].append(out)
        if len(ev):
            all_hits.append(ev.assign(rule=rule["label"], in_sample=ins))
    hits_df = pd.concat(all_hits, ignore_index=True) if all_hits else pd.DataFrame()
    _write(cfg, res, hits_df)
    return res


def _write(cfg: Config, res: dict[str, Any], hits: pd.DataFrame) -> None:
    out = cfg.paths.results
    out.mkdir(parents=True, exist_ok=True)
    from okxrev.review import _json_default
    (out / "replicate.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    rows = []
    for r in res["rules"]:
        rows.append({"rule": r["label"], "verdict": r["verdict"], "your_n": r["your_n"], "your_pnl": r["your_pnl"],
                     "your_win_rate": r["your_win_rate"], "hold_bars": r["hold_bars"], "stop": r["stop"],
                     **{f"{p}_{k}": r[p].get(k) for p in ("in_sample", "out_of_sample", "baseline") for k in ("n", "mean", "win_rate")},
                     "oos_ci_lo": (r["out_of_sample"].get("ci95") or [None, None])[0],
                     "oos_ci_hi": (r["out_of_sample"].get("ci95") or [None, None])[1],
                     "oos_months_positive": r["oos_months_positive"]})
    pd.DataFrame(rows).to_csv(out / "replicate.csv", index=False, encoding="utf-8", float_format="%.6g")
    if len(hits):
        hits.to_csv(out / "replicate_hits.csv", index=False, encoding="utf-8", float_format="%.6g")


def print_replicate(res: dict[str, Any]) -> None:
    print("\n== 可複製性：把你賺錢的進場情境套到全市場 ==")
    if not res["rules"]:
        print("  " + res.get("note", "無候選規則"))
        return
    def pc(v: Any) -> str:
        return "    -" if v is None else f"{v * 100:+5.1f}%"
    for r in res["rules"]:
        o, b, i = r["out_of_sample"], r["baseline"], r["in_sample"]
        ci = o.get("ci95") or [None, None]
        print(f"[{r['verdict']}] {r['label']}")
        print(f"   你：{r['your_n']} 筆 {r['your_pnl']:+.2f} USD 勝率 {r['your_win_rate']:.0%}；模擬出場：持有 {r['hold_bars'] * 5} 分鐘 / 停損 {r['stop']:.1%}")
        print(f"   樣本內 n={i.get('n', 0):>5} 平均 {pc(i.get('mean'))}  | 樣本外 n={o.get('n', 0):>5} 平均 {pc(o.get('mean'))} "
              f"勝率 {pc(o.get('win_rate'))} CI[{pc(ci[0])},{pc(ci[1])}]  | 隨機基準 {pc(b.get('mean'))}")
