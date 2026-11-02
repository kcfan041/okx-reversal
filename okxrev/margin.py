"""Isolated vs cross margin, and a safe leverage, for every paper trader.

Takes each trader's closed unit-book (C) positions (live paper trading, or the blind replay) and replays them as one
account: equity 1,000 U, each new position puts MARGIN_FRAC of the current equity as margin at leverage L
(notional = margin x L), at most MAX_OPEN positions at a time.

  isolated  a position is liquidated when its maximum adverse excursion (recorded on 5m bars) reaches
            1/L - maintenance; it then loses its margin, otherwise it returns L x its net return
  cross     all positions share the equity; every hour the account is marked at each open position's worst price of
            that hour (high for shorts, low for longs); when equity falls to the total maintenance margin every
            position is closed there and the account is wiped (the run stops)

Safe leverage = the highest L at which no more than 5% of the trader's trades would have hit the isolated
liquidation price (from the MAE distribution). Funding and fees are already inside each position's net return.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from okxrev import db, regime
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.margin")

H = 3_600_000
LEVERS = (1, 3, 5, 10, 20)
MARGIN_FRAC = 0.05
MAX_OPEN = 10
MMR = 0.005
EQ0 = 1000.0
MIN_TRADES = 20


def liq_frac(lever: float) -> float:
    return max(1.0 / lever - MMR, 0.0) if lever > 1 else np.inf


def safe_lever(mae: np.ndarray, q: float = 0.95) -> float:
    m = float(np.quantile(mae[np.isfinite(mae)], q)) if np.isfinite(mae).any() else np.nan
    return float(max(1.0, np.floor(1.0 / (m + MMR)))) if np.isfinite(m) and m > 0 else float("inf")


def isolated(pos: pd.DataFrame, lever: float) -> dict[str, Any]:
    """pos sorted by entry: entry_ts, exit_ts, ret_net, mae."""
    eq, peak, dd, liq, taken = EQ0, EQ0, 0.0, 0, 0
    open_: list[tuple[int, float]] = []          # (exit_ts, pnl) of positions still open
    lf = liq_frac(lever)
    for r in pos.itertuples(index=False):
        done = [x for x in open_ if x[0] <= r.entry_ts]
        for x in done:
            eq += x[1]
        open_ = [x for x in open_ if x[0] > r.entry_ts]
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak) if peak > 0 else dd
        if len(open_) >= MAX_OPEN or eq <= 0:
            continue
        m = eq * MARGIN_FRAC
        if r.mae >= lf:
            pnl = -m
            liq += 1
        else:
            pnl = max(-m, m * lever * r.ret_net)
        open_.append((int(r.exit_ts), pnl))
        taken += 1
    for x in open_:
        eq += x[1]
    peak = max(peak, eq)
    dd = max(dd, 1 - eq / peak) if peak > 0 else dd
    return {"trades": taken, "liquidations": liq, "liq_rate": liq / taken if taken else None, "ret": eq / EQ0 - 1, "max_dd": dd}


def hourly_extremes(path: Any, insts: list[str], lo: int, hi: int) -> dict[str, pd.DataFrame]:
    with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=60) as con:
        q = f"""SELECT inst_id, (ts // {H}) * {H} AS h, max(high) AS hi, min(low) AS lo, arg_max(close, ts) AS c FROM candles
                WHERE ts >= ? AND ts < ? AND inst_id IN ({','.join('?' * len(insts))}) GROUP BY 1, 2"""
        df = con.execute(q, [lo, hi, *insts]).df()
    return {i: g.set_index("h").sort_index() for i, g in df.groupby("inst_id")}


def cross(pos: pd.DataFrame, lever: float, px: dict[str, pd.DataFrame]) -> dict[str, Any]:
    eq, peak, dd, taken = EQ0, EQ0, 0.0, 0
    book: list[dict[str, Any]] = []
    rows = list(pos.itertuples(index=False))
    events = sorted({int(r.entry_ts) // H * H for r in rows} | {int(r.exit_ts) // H * H for r in rows})
    if not events:
        return {"trades": 0}
    hours = np.arange(events[0], events[-1] + H, H)
    by_entry: dict[int, list[Any]] = {}
    for r in rows:
        by_entry.setdefault(int(r.entry_ts) // H * H, []).append(r)
    blown = None
    for h in hours:
        still = []
        for b in book:
            if b["exit_ts"] < h + H:
                eq += b["notional"] * b["ret_net"]
            else:
                still.append(b)
        book = still
        for r in by_entry.get(int(h), []):
            if len(book) >= MAX_OPEN or eq <= 0:
                continue
            g = px.get(r.inst_id)
            if g is None or pd.isna(r.entry_px) or r.entry_px <= 0:
                continue
            book.append({"inst": r.inst_id, "side": 1 if r.side == "long" else -1, "epx": float(r.entry_px), "exit_ts": int(r.exit_ts),
                         "notional": eq * MARGIN_FRAC * lever, "ret_net": float(r.ret_net)})
            taken += 1
        if book:
            upl, maint = 0.0, 0.0
            for b in book:
                g = px.get(b["inst"])
                if g is None or h not in g.index:
                    continue
                worst = g.at[h, "lo"] if b["side"] > 0 else g.at[h, "hi"]
                upl += b["notional"] * b["side"] * (worst / b["epx"] - 1)
                maint += b["notional"] * MMR
            if eq + upl <= maint:
                blown = int(h)
                eq = max(0.0, eq + upl)
                book = []
                peak = max(peak, EQ0)
                dd = 1.0
                break
            cur = eq + upl
            peak = max(peak, cur)
            dd = max(dd, 1 - cur / peak) if peak > 0 else dd
    for b in book:
        eq += b["notional"] * b["ret_net"]
    return {"trades": taken, "blown_at": ms_to_str(blown) if blown else None, "ret": eq / EQ0 - 1, "max_dd": dd}


def load_positions(source: str, cfg: Config) -> pd.DataFrame:
    from okxrev import replay
    p = cfg.paper_db if source == "live" else replay.out_db_path(cfg)
    if not p.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(p), read_only=True)
    try:
        return con.execute("SELECT trader_id, inst_id, side, entry_ts, exit_ts, entry_px, ret_net, mae FROM positions "
                           "WHERE book = 'C' AND state = 'closed' AND entry_ts IS NOT NULL AND ret_net IS NOT NULL").df()
    finally:
        con.close()


def evaluate(cfg: Config, sources: tuple[str, ...] = ("live", "blind")) -> dict[str, Any]:
    from okxrev import replay
    t0 = time.monotonic()
    out: dict[str, Any] = {}
    for src in sources:
        pos = load_positions(src, cfg)
        if pos.empty:
            continue
        path = cfg.paths.db if src == "live" else replay.old_db_path(cfg)
        pos = pos.sort_values("entry_ts")
        rows = []
        tids = [t for t, g in pos.groupby("trader_id") if len(g) >= MIN_TRADES]
        log.info("margin %s: %d traders", src, len(tids))
        for tid in tids:
            g = pos[pos["trader_id"] == tid]
            px = hourly_extremes(path, sorted(g["inst_id"].unique()), int(g["entry_ts"].min()) - H, int(g["exit_ts"].max()) + 2 * H)
            mae = g["mae"].to_numpy(float)
            row = {"trader_id": tid, "n": int(len(g)), "mean": float(g["ret_net"].mean()), "mae_p50": float(np.nanmedian(mae)),
                   "mae_p95": float(np.nanquantile(mae, 0.95)), "safe_lever": safe_lever(mae), "levers": {}}
            for L in LEVERS:
                row["levers"][str(L)] = {"isolated": isolated(g, L), "cross": cross(g, L, px)}
            rows.append(row)
        out[src] = rows
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1), "levers": list(LEVERS),
           "margin_frac": MARGIN_FRAC, "max_open": MAX_OPEN, "mmr": MMR, "sources": out,
           "caveats": [f"每筆用當下權益的 {MARGIN_FRAC:.0%} 當保證金，最多同時 {MAX_OPEN} 筆", "全倉用每小時最不利價格估算，比實際（逐筆即時）粗",
                       "逐倉用 5 分 K 記錄的最大反向波動判斷是否強平", "安全槓桿：歷史上 95% 的交易不會碰到逐倉強平價的最高倍數"]}
    p = cfg.paths.results / "margin.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    return res


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "margin.json"
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null").replace("Infinity", "null")) if p.exists() else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.margin")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--source", choices=["live", "blind", "both"], default="both")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "margin")
    if args.evaluate:
        res = evaluate(cfg, ("live", "blind") if args.source == "both" else (args.source,))
    else:
        res = load(cfg)
    if not res:
        print("no results yet: python -m okxrev.margin --evaluate")
        return 0
    for src, rows in res["sources"].items():
        print(f"\n== {src}")
        for r in sorted(rows, key=lambda x: -x["mean"])[:25]:
            L = r["levers"]
            cell = lambda k: (f"iso {L[k]['isolated']['ret'] * 100:+6.0f}% liq {L[k]['isolated']['liquidations']:3d} | "
                              f"cross {L[k]['cross'].get('ret', 0) * 100:+6.0f}%{' BLOWN' if L[k]['cross'].get('blown_at') else '      '}")
            print(f"{r['trader_id']:26s} n={r['n']:5d} mean={r['mean'] * 100:+6.2f}% mae95={r['mae_p95'] * 100:5.1f}% safe={r['safe_lever']}x"
                  f" || 5x {cell('5')} || 20x {cell('20')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
