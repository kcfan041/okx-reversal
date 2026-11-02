"""Taker-flow and premium divergence: price and aggressive (taker) buying, or the perpetual premium, disagree.

Hourly, per coin: r24 = 24h return; imb = (taker buy - taker sell) / (buy + sell) over the last 24h, as a z-score
against the coin's own previous 30 days. Events (24h cooldown per coin):

  short_div   r24 >= +R and z <= -Z   price up while sellers dominate (absorbed buying, likely top)
  long_div    r24 <= -R and z >= +Z   price down while buyers dominate
  short_hot   z >= +Z2                extreme aggressive buying (fade)
  long_cold   z <= -Z2                extreme aggressive selling (fade)

  short_prem  r24 >= +R and premium z >= +Z   rally with a stretched perpetual premium (crowded longs)
  long_prem   r24 <= -R and premium z <= -Z   sell-off with a deep discount (crowded shorts)
  short_pdiv  r24 >= +R and premium z <= -Z   rally while the perpetual trades at a discount (fade)
  long_pdiv   r24 <= -R and premium z >= +Z   sell-off while the perpetual keeps a premium (rebound)

OKX's hourly taker-volume history is all zeros before about 2026-08 (both databases), so those rows are treated as
missing and the taker events only have the last few months; the premium (mark / index - 1, hourly) covers both
periods.

Entry at the close of the signal hour, exits after 6 or 24 hours, net of costs. Each event type is compared with the
"price only" control (same r24 condition, any imbalance; for the hot/cold ones: all hours) over the same segment, so
the result shows what the flow adds beyond the price move. Segments as in xsec: live database first 2/3 (search) and
last 1/3 (validation), old database (2024-09 .. 2025-09) as the blind test.
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

log = logging.getLogger("okxrev.flow")

H = 3_600_000
D = 24 * H
WIN = 24
NORM = 720
COOLDOWN = 24
HOLDS = (6, 24)
GRID = [{"R": 0.10, "Z": 1.0}, {"R": 0.10, "Z": 1.5}, {"R": 0.20, "Z": 1.0}, {"R": 0.20, "Z": 1.5}]
Z_HOT = (2.0, 2.5)
SPLIT = 2 / 3


def load_hourly(path: Any) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(close, taker buy, taker sell, premium) per hour x swap; empty taker hours (buy + sell = 0) are missing."""
    with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=60) as con:
        c = con.execute(f"""SELECT inst_id, (ts // {H}) * {H} + {H} AS h, arg_max(close, ts) AS c FROM candles
                            WHERE inst_id LIKE '%-USDT-SWAP' GROUP BY 1, 2""").df()
        tv = con.execute("SELECT inst_id, ts + 3600000 AS h, buy, sell FROM taker_volume WHERE buy + sell > 0").df()
        mi = con.execute("SELECT inst_id, ts + 3600000 AS h, mark / idx - 1 AS prem FROM mark_index WHERE idx > 0 AND mark > 0").df()
    C = c.pivot(index="h", columns="inst_id", values="c").sort_index()
    full = np.arange(int(C.index.min()), int(C.index.max()) + H, H)
    C = C.reindex(full)
    B = tv.pivot_table(index="h", columns="inst_id", values="buy").reindex(index=full, columns=C.columns)
    S = tv.pivot_table(index="h", columns="inst_id", values="sell").reindex(index=full, columns=C.columns)
    P = mi.pivot_table(index="h", columns="inst_id", values="prem").reindex(index=full, columns=C.columns)
    return C, B, S, P


def zscore(x: pd.DataFrame) -> pd.DataFrame:
    """Against the coin's own previous 30 days (ending 24h earlier, so the current move is not in the baseline)."""
    mu = x.rolling(NORM, min_periods=NORM // 2).mean().shift(WIN)
    sd = x.rolling(NORM, min_periods=NORM // 2).std().shift(WIN)
    return (x - mu) / sd


def signals(C: pd.DataFrame, B: pd.DataFrame, S: pd.DataFrame, P: pd.DataFrame) -> dict[str, pd.DataFrame]:
    keep = [c for c in C.columns if c.split("-")[0] not in regime.EXCL]
    C, B, S, P = C[keep], B[keep], S[keep], P[keep]
    b24, s24 = B.rolling(WIN, min_periods=WIN - 2).sum(), S.rolling(WIN, min_periods=WIN - 2).sum()
    imb = (b24 - s24) / (b24 + s24)
    z = zscore(imb)
    pz = zscore(P.rolling(6, min_periods=4).mean())
    r24 = C / C.shift(WIN) - 1
    fwd = {h: C.shift(-h) / C - 1 for h in HOLDS}
    return {"z": z, "pz": pz, "r24": r24, **{f"f{h}": f for h, f in fwd.items()}}


def cooldown(mask: pd.DataFrame, gap: int = COOLDOWN) -> pd.DataFrame:
    m = mask.to_numpy()
    out = np.zeros_like(m, dtype=bool)
    for j in range(m.shape[1]):
        last = -10 ** 9
        for i in np.flatnonzero(m[:, j]):
            if i - last >= gap:
                out[i, j] = True
                last = i
    return pd.DataFrame(out, index=mask.index, columns=mask.columns)


def event_stats(sig: dict[str, pd.DataFrame], mask: pd.DataFrame, ctrl: pd.DataFrame, side: int, h: int, cost: float,
                lo: int, hi: int) -> dict[str, Any]:
    rows = (mask.index >= lo) & (mask.index < hi)
    f = sig[f"f{h}"].to_numpy()[rows]
    ev = cooldown(mask.loc[rows]).to_numpy() & np.isfinite(f)
    cm = cooldown(ctrl.loc[rows]).to_numpy() & np.isfinite(f)
    x = side * f[ev] - cost
    y = side * f[cm] - cost
    if len(x) < 5:
        return {"n": int(len(x))}
    days = (np.nonzero(ev)[0] + 0) // 24
    dm = pd.Series(x).groupby(days).mean()
    se = np.sqrt(x.var(ddof=1) / len(x) + (y.var(ddof=1) / len(y) if len(y) > 1 else 0))
    return {"n": int(len(x)), "n_days": int(len(dm)), "mean": float(x.mean()), "median": float(np.median(x)), "win": float(np.mean(x > 0)),
            "day_mean": float(dm.mean()), "ctrl_n": int(len(y)), "ctrl_mean": float(y.mean()) if len(y) else None,
            "t_vs_ctrl": float((x.mean() - y.mean()) / se) if len(y) > 1 and se > 0 else None}


def masks(sig: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    z, r, pz = sig["z"], sig["r24"], sig["pz"]
    out = []
    for g in GRID:
        R, Z = g["R"], g["Z"]
        out.append({"name": f"short_div_r{int(R * 100)}_z{Z}", "label": f"24h 漲 ≥{R:.0%} 但主動賣量偏多（z ≤ -{Z}）→ 做空", "side": -1,
                    "mask": (r >= R) & (z <= -Z), "ctrl": r >= R, "ctrl_label": f"只看 24h 漲 ≥{R:.0%}"})
        out.append({"name": f"long_div_r{int(R * 100)}_z{Z}", "label": f"24h 跌 ≥{R:.0%} 但主動買量偏多（z ≥ {Z}）→ 做多", "side": 1,
                    "mask": (r <= -R) & (z >= Z), "ctrl": r <= -R, "ctrl_label": f"只看 24h 跌 ≥{R:.0%}"})
    for g in GRID:
        R, Z = g["R"], g["Z"]
        out.append({"name": f"short_prem_r{int(R * 100)}_z{Z}", "label": f"24h 漲 ≥{R:.0%} 且合約溢價偏高（z ≥ {Z}）→ 做空", "side": -1,
                    "mask": (r >= R) & (pz >= Z), "ctrl": (r >= R) & pz.notna(), "ctrl_label": f"只看 24h 漲 ≥{R:.0%}"})
        out.append({"name": f"long_prem_r{int(R * 100)}_z{Z}", "label": f"24h 跌 ≥{R:.0%} 且合約折價偏深（z ≤ -{Z}）→ 做多", "side": 1,
                    "mask": (r <= -R) & (pz <= -Z), "ctrl": (r <= -R) & pz.notna(), "ctrl_label": f"只看 24h 跌 ≥{R:.0%}"})
        out.append({"name": f"short_pdiv_r{int(R * 100)}_z{Z}", "label": f"24h 漲 ≥{R:.0%} 但合約折價（z ≤ -{Z}）→ 做空", "side": -1,
                    "mask": (r >= R) & (pz <= -Z), "ctrl": (r >= R) & pz.notna(), "ctrl_label": f"只看 24h 漲 ≥{R:.0%}"})
        out.append({"name": f"long_pdiv_r{int(R * 100)}_z{Z}", "label": f"24h 跌 ≥{R:.0%} 但合約仍有溢價（z ≥ {Z}）→ 做多", "side": 1,
                    "mask": (r <= -R) & (pz >= Z), "ctrl": (r <= -R) & pz.notna(), "ctrl_label": f"只看 24h 跌 ≥{R:.0%}"})
    allh = z.notna()
    for Z in Z_HOT:
        out.append({"name": f"short_hot_z{Z}", "label": f"主動買量極端偏多（z ≥ {Z}）→ 做空", "side": -1, "mask": z >= Z, "ctrl": allh, "ctrl_label": "所有時段"})
        out.append({"name": f"long_cold_z{Z}", "label": f"主動賣量極端偏多（z ≤ -{Z}）→ 做多", "side": 1, "mask": z <= -Z, "ctrl": allh, "ctrl_label": "所有時段"})
    return out


def verdict_of(good: bool, few: bool, st: dict[str, Any]) -> str:
    """可用 needs a combined t (sum of the segments' t vs control / sqrt(3)) of at least 2; weaker passes are 觀察中."""
    if few:
        return "資料不足"
    if not good:
        return "無效"
    ts = [(st.get(s) or {}).get("t_vs_ctrl") or 0.0 for s in ("train", "valid", "blind") if s in st]
    return "可用" if sum(ts) / np.sqrt(len(ts)) >= 2 else "觀察中"


def evaluate(cfg: Config) -> dict[str, Any]:
    from okxrev import replay
    t0 = time.monotonic()
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]))
    data = {}
    sm = signals(*load_hourly(cfg.paths.db))
    has = sm["pz"].notna().any(axis=1)
    lo_m = int(sm["pz"].index[has.to_numpy().argmax()])
    hi_m = int(sm["pz"].index[-1]) + H
    cut = lo_m + int((hi_m - lo_m) * SPLIT) // H * H
    data["main"] = (sm, masks(sm))
    op = replay.old_db_path(cfg)
    if op.exists():
        so = signals(*load_hourly(op))
        data["old"] = (so, masks(so))
    rows = []
    for i, m in enumerate(data["main"][1]):
        for h in HOLDS:
            st = {"train": event_stats(sm, m["mask"], m["ctrl"], m["side"], h, cost, lo_m, cut),
                  "valid": event_stats(sm, m["mask"], m["ctrl"], m["side"], h, cost, cut, hi_m)}
            if "old" in data:
                so, mo = data["old"]
                st["blind"] = event_stats(so, mo[i]["mask"], mo[i]["ctrl"], m["side"], h, cost, -10 ** 15, 10 ** 15)
            few = any((st.get(s) or {}).get("n", 0) < 20 for s in ("train", "valid", "blind") if s in st)
            good = not few and all((st.get(s) or {}).get("n", 0) >= 20 and (st[s].get("mean") or -1) > 0 and (st[s].get("t_vs_ctrl") or 0) > 0
                       for s in ("train", "valid", "blind") if s in st)
            rows.append({"name": m["name"], "label": m["label"], "ctrl_label": m["ctrl_label"], "hold_h": h,
                         "verdict": verdict_of(good, few, st), **st})
            log.info("flow %s %dh: train %s valid %s blind %s", m["name"], h, *(f"{(st.get(s) or {}).get('mean', float('nan')) * 100:+.2f}%" for s in ("train", "valid", "blind")))
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1), "cost_round_trip": cost,
           "segments": {"train": [ms_to_str(lo_m, "%Y-%m-%d"), ms_to_str(cut, "%Y-%m-%d")], "valid": [ms_to_str(cut, "%Y-%m-%d"), ms_to_str(hi_m, "%Y-%m-%d")],
                        "blind": [ms_to_str(int(data["old"][0]["z"].index[0]), "%Y-%m-%d"), ms_to_str(int(data["old"][0]["z"].index[-1]), "%Y-%m-%d")] if "old" in data else None},
           "events": rows,
           "caveats": ["判定：三段都至少 20 筆、每筆淨報酬為正、而且比「只看價格」的對照好；三段合計 t ≥ 2 才算可用，否則觀察中",
                       "OKX 主動買賣量的歷史在 2026-08 以前全是 0（兩個資料庫都是），主動買賣的訊號只有最近幾個月，過熱分數的「量能與買盤」類在這之前也沒有主動買賣資料", "舊資料只有現存老合約（倖存者偏差）",
                       f"{len(rows)} 種組合同時比較（多重比較）"]}
    out = cfg.paths.results / "flow.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    return res


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "flow.json"
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null")) if p.exists() else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.flow")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "flow")
    res = evaluate(cfg) if args.evaluate else load(cfg)
    if not res:
        print("no results yet: python -m okxrev.flow --evaluate")
        return 0
    print(res["segments"])
    for r in res["events"]:
        f = lambda s: "-" if not r.get(s) or r[s].get("mean") is None else (
            f"{r[s]['mean'] * 100:+6.2f}% n={r[s]['n']:4d} ctrl {r[s]['ctrl_mean'] * 100:+.2f}% t={r[s]['t_vs_ctrl'] or 0:+.1f}")
        print(f"{r['verdict']} {r['name']:22s} {r['hold_h']:2d}h | train {f('train')} | valid {f('valid')} | blind {f('blind')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
