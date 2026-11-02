"""Cross-sectional momentum / reversal: each rebalance day rank the alts by their past L-day return, hold the top and
bottom fifth for H days.

Universe each day: USDT swaps listed >= 30 days, BTC/ETH excluded, the `UNIVERSE` most traded over the last 7 days.
Entry at the day's close, equal weight, non-overlapping rebalances every H days (the result is averaged over the H
possible start offsets). Legs: winners (top fifth) and losers (bottom fifth), each measured against the universe
mean (so a leg is a market-neutral bet); spread = winners - losers ("momentum"; its negative is "reversal").
Costs: every rebalance pays a full round trip on both legs (conservative); short funding is not included.

Segments: the live database is split into a parameter-search part (first 2/3) and a validation part (last 1/3);
the old database (2024-09 .. 2025-09) is the blind test. A combination is "可用" only if the side chosen on the
search part keeps its sign with net gain on both validation and the blind period.
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

from okxrev import db, regime
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.xsec")

D = regime.D
LOOKBACKS = (1, 3, 7, 14)
HOLDS = (1, 3, 7)
UNIVERSE = 100
FRAC = 0.2
SPLIT = 2 / 3


def portfolios(C: pd.DataFrame, V: pd.DataFrame, L: int, H: int, universe: int = UNIVERSE, frac: float = FRAC) -> pd.DataFrame:
    """One row per formation day: H-day forward simple returns of winners, losers and the universe (gross)."""
    keep = [c for c in C.columns if c.split("-")[0] not in regime.EXCL]
    C, V = C[keep], V[keep]
    ages = regime.ages_of(C)
    lr = np.log(C).diff()
    past = np.log(C / C.shift(L))
    fwd = np.expm1(lr.shift(-1).rolling(H, min_periods=H).sum().shift(-(H - 1)))
    to7 = V.rolling(7, min_periods=5).sum()
    rows = []
    for i, d in enumerate(C.index):
        if i + H >= len(C):
            break
        ok = (ages.iloc[i] >= regime.MIN_AGE) & past.iloc[i].notna() & fwd.iloc[i].notna() & to7.iloc[i].notna()
        if ok.sum() < 30:
            continue
        u = to7.iloc[i][ok].sort_values(ascending=False).index[:universe]
        p = past.iloc[i][u].sort_values()
        k = max(3, int(len(p) * frac))
        f = fwd.iloc[i]
        rows.append({"d": int(d), "win": float(f[p.index[-k:]].mean()), "lose": float(f[p.index[:k]].mean()), "all": float(f[u].mean()), "n": int(len(u))})
    return pd.DataFrame(rows)


def seg_stats(P: pd.DataFrame, H: int, cost: float) -> dict[str, Any]:
    """Non-overlapping rebalances, averaged over the H start offsets. Returns per-rebalance net means and t."""
    if P.empty:
        return {"n": 0}
    out: dict[str, list[float]] = {"spread": [], "spread_rev": [], "spread_gross": [], "win_x": [], "lose_x": []}
    tvals: dict[str, list[float]] = {k: [] for k in out}
    n_reb = []
    for off in range(H):
        q = P.iloc[off::H]
        if len(q) < 3:
            continue
        legs = {"spread": q["win"] - q["lose"] - 2 * cost, "spread_rev": q["lose"] - q["win"] - 2 * cost, "spread_gross": q["win"] - q["lose"], "win_x": q["win"] - q["all"] - cost, "lose_x": q["lose"] - q["all"] - cost}
        for k, x in legs.items():
            out[k].append(float(x.mean()))
            tvals[k].append(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if x.std(ddof=1) > 0 else 0.0)
        n_reb.append(len(q))
    if not n_reb:
        return {"n": 0}
    res: dict[str, Any] = {"n": int(np.mean(n_reb)), "days": int(len(P))}
    for k in out:
        res[k] = float(np.mean(out[k]))
        res[f"{k}_t"] = float(np.mean(tvals[k]))
    return res


def evaluate(cfg: Config) -> dict[str, Any]:
    from okxrev import replay
    t0 = time.monotonic()
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]))
    Cm, Vm = regime.daily_panel(cfg.paths.db)
    cut = int(Cm.index[0] + (Cm.index[-1] - Cm.index[0]) * SPLIT) // D * D
    old_p = replay.old_db_path(cfg)
    Co, Vo = regime.daily_panel(old_p) if old_p.exists() else (None, None)
    combos = []
    for L in LOOKBACKS:
        for H in HOLDS:
            P = portfolios(Cm, Vm, L, H)
            tr, va = P[P["d"] < cut], P[P["d"] >= cut]
            st = {"train": seg_stats(tr, H, cost), "valid": seg_stats(va, H, cost)}
            if Co is not None:
                st["blind"] = seg_stats(portfolios(Co, Vo, L, H), H, cost)
            side = "momentum" if st["train"].get("spread", 0) > st["train"].get("spread_rev", 0) else "reversal"
            key = "spread" if side == "momentum" else "spread_rev"
            keep_sign = all((st.get(s) or {}).get(key, -1) > 0 for s in ("valid", "blind") if s in st)
            verdict = "可用" if keep_sign and (st["train"].get(key) or 0) > 0 else "無效"
            combos.append({"lookback_d": L, "hold_d": H, "side": side, "key": key, "verdict": verdict, **st})
            log.info("xsec L=%d H=%d %s: train %+.2f%% valid %+.2f%% blind %+.2f%%", L, H, side, 100 * (st["train"].get(key) or 0),
                     100 * (st["valid"].get(key) or 0), 100 * ((st.get("blind") or {}).get(key) or 0))
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1), "cost_round_trip": cost,
           "universe": UNIVERSE, "frac": FRAC,
           "segments": {"train": [ms_to_str(int(Cm.index[0]), "%Y-%m-%d"), ms_to_str(cut, "%Y-%m-%d")],
                        "valid": [ms_to_str(cut, "%Y-%m-%d"), ms_to_str(int(Cm.index[-1]), "%Y-%m-%d")],
                        "blind": [ms_to_str(int(Co.index[0]), "%Y-%m-%d"), ms_to_str(int(Co.index[-1]), "%Y-%m-%d")] if Co is not None else None},
           "combos": combos,
           "caveats": ["每次換倉都算兩邊來回手續費與滑價（保守）；沒有算做空資金費", "舊資料只有現存老合約（倖存者偏差）",
                       "12 種組合同時比較，判定要求找參數期挑出的方向在驗證期與盲測期都賺"]}
    out = cfg.paths.results / "xsec.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    return res


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "xsec.json"
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null")) if p.exists() else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.xsec")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "xsec")
    res = evaluate(cfg) if args.evaluate else load(cfg)
    if not res:
        print("no results yet: python -m okxrev.xsec --evaluate")
        return 0
    print(res["segments"])
    for c in res["combos"]:
        k = c["key"]
        f = lambda s: "-" if not c.get(s) or c[s].get(k) is None else f"{c[s][k] * 100:+6.2f}% (t={c[s][k + '_t']:+.1f}, n={c[s]['n']})"
        g = lambda s: "-" if not c.get(s) or c[s].get("spread_gross") is None else f"{c[s]['spread_gross'] * 100:+.2f}%"
        print(f"L={c['lookback_d']:2d} H={c['hold_d']} {c['side']:8s} {c['verdict']}  train {f('train')}  valid {f('valid')}  blind {f('blind')}"
              f"  | gross momentum spread {g('train')}/{g('valid')}/{g('blind')} | winners-x {c['train'].get('win_x', 0) * 100:+.2f}% losers-x {c['train'].get('lose_x', 0) * 100:+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
