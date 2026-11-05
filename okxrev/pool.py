"""Shared-capital pool of paper traders, replayed offline from their closed unit-book (C) positions (no engine change).

Members (walk-forward, no look-ahead): every STEP_D days the pool re-picks up to K traders from the candidates using
only positions that closed in the previous `look` days. Score per trade = net return minus the same-side random-control
mean of the same window; a trader qualifies with >= MIN_N trades, a positive mean and t >= T_MIN; the K best t-values
are taken, one per family (a rule and its _sl20 / inverse / LLM variants count as one). Candidates exclude random
controls, follow traders and *_ctrl controls. The pool takes the members' entries until the next re-pick.

Every position gets a stop of at most STOP: when its recorded MAE reached the stop, the return is -stop - costs.

Pool: one equity (EQ0). Position size = equity x RISK / stop (so a stop loses RISK of equity), at most MAX_POS_FRAC of
equity; open notional <= GROSS x equity (no leverage) and <= SIDE_CAP x equity per side; at most MAX_OPEN positions,
MAX_PER_MEMBER per member; the same coin and side is held once. Equity is marked hourly at the close.
Brakes: drawdown from the peak >= DD_HALF -> half size; >= DD_HALT -> no new entries for HALT_D days, then the peak
resets; equity down DAY_LOSS within a UTC day -> no new entries for the rest of that day.

Controls, all with the same pool rules: random members (K random candidates with enough trades each re-pick, SIMS
runs), every candidate at once ("all"), the random-control traders as members ("random entries"), and the main
setting without brakes. Sources: live paper trading and the 2024-25 blind replay.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db, regime
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.pool")

H = 3_600_000
D = 86_400_000
STEP_D = 7
LOOKS = (14, 30, 60)
KS = (3, 5, 10)
MAIN = (5, 30)
MIN_N = {14: 10, 30: 20, 60: 20}
T_MIN = 2.0
EQ0 = 10_000.0
RISK = 0.01
STOP = 0.20
MAX_POS_FRAC = 0.10
GROSS = 1.0
SIDE_CAP = 0.7
MAX_OPEN = 20
MAX_PER_MEMBER = 5
DD_HALF = 0.10
DD_HALT = 0.15
HALT_D = 7
DAY_LOSS = 0.03
SIMS = 50
SIMS_MAIN = 200
EXCL_KINDS = ("random", "follow")


def family(tid: str) -> str:
    base = re.sub(r"^(inv|llm)_", "", tid).split("_")[0]
    return "heat" if base.startswith("heat") else base      # heat80 / heat90 are the same score at two thresholds


# ---------------------------------------------------------------- data
def load_positions(cfg: Config, source: str) -> tuple[pd.DataFrame, dict[str, str]]:
    from okxrev import replay
    p = cfg.paper_db if source == "live" else replay.out_db_path(cfg)
    if not p.exists():
        return pd.DataFrame(), {}
    con = db.connect(p, read_only=True, wait_sec=2.0, max_tries=60)      # the paper tick writes paper.duckdb every 5 minutes
    try:
        pos = con.execute("""SELECT trader_id, inst_id, side, entry_ts, exit_ts, entry_px, stop_px, ret_net, mae FROM positions
                             WHERE book = 'C' AND state = 'closed' AND entry_ts IS NOT NULL AND exit_ts IS NOT NULL
                               AND ret_net IS NOT NULL AND entry_px > 0""").df()
        kinds = dict(con.execute("SELECT trader_id, kind FROM traders").fetchall())
    finally:
        con.close()
    return pos, kinds


def prepare(pos: pd.DataFrame, cost: float) -> pd.DataFrame:
    own = (pos["stop_px"] / pos["entry_px"] - 1).abs()
    stop = np.where(own.notna() & (own > 0), np.minimum(own.fillna(STOP), STOP), STOP)
    hit = pos["mae"].fillna(0).to_numpy(float) >= stop
    out = pos.assign(stop=stop, ret=np.where(hit, -stop - cost, pos["ret_net"].to_numpy(float)),
                     sgn=np.where(pos["side"].eq("long"), 1, -1))
    return out.sort_values("entry_ts").reset_index(drop=True)


def hourly_close(path: Any, insts: list[str], lo: int, hi: int) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """(hour grid, close matrix hours x insts forward-filled, inst -> column)."""
    with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=60) as con:
        df = con.execute(f"""SELECT inst_id, (ts // {H}) * {H} AS h, arg_max(close, ts) AS c FROM candles
                             WHERE ts >= ? AND ts < ? AND inst_id IN (SELECT UNNEST(?::VARCHAR[])) GROUP BY 1, 2""", [lo, hi, insts]).df()
    grid = np.arange(lo // H * H, hi // H * H + H, H, dtype=np.int64)
    P = df.pivot(index="h", columns="inst_id", values="c").reindex(index=grid, columns=insts).ffill()
    return grid, P.to_numpy(float), {k: i for i, k in enumerate(insts)}


# ---------------------------------------------------------------- members
def window_stats(pos: pd.DataFrame, rand: pd.DataFrame, t: int, look_d: int) -> pd.DataFrame:
    lo = t - look_d * D
    w = pos[(pos["exit_ts"] < t) & (pos["exit_ts"] >= lo)]
    r = rand[(rand["exit_ts"] < t) & (rand["exit_ts"] >= lo)]
    if w.empty:
        return pd.DataFrame(columns=["n", "mean", "t"])
    rm = r.groupby("side")["ret"].mean().to_dict()
    ex = w["ret"] - w["side"].map(rm).fillna(0.0)
    g = w.assign(ex=ex).groupby("trader_id")
    st = pd.DataFrame({"n": g.size(), "mean": g["ret"].mean(), "ex": g["ex"].mean(), "sd": g["ex"].std()})
    st["t"] = st["ex"] / (st["sd"] / np.sqrt(st["n"]))
    return st


def pick(st: pd.DataFrame, k: int, min_n: int) -> list[tuple[str, float]]:
    ok = st[(st["n"] >= min_n) & (st["mean"] > 0) & (st["t"] >= T_MIN)].sort_values("t", ascending=False)
    out, fams = [], set()
    for tid, r in ok.iterrows():
        if family(tid) in fams:
            continue
        fams.add(family(tid))
        out.append((tid, float(r["t"])))
        if len(out) >= k:
            break
    return out


def pick_random(st: pd.DataFrame, k: int, min_n: int, rng: np.random.Generator) -> list[str]:
    ids = list(st.index[st["n"] >= min_n])
    rng.shuffle(ids)
    out, fams = [], set()
    for tid in ids:
        if family(tid) not in fams:
            fams.add(family(tid))
            out.append(tid)
        if len(out) >= k:
            break
    return out


def schedule_entries(pos: pd.DataFrame, steps: np.ndarray, members: list[list[str]]) -> pd.DataFrame:
    parts = []
    for i, t in enumerate(steps):
        if not members[i]:
            continue
        end = steps[i + 1] if i + 1 < len(steps) else np.iinfo(np.int64).max
        parts.append(pos[(pos["entry_ts"] >= t) & (pos["entry_ts"] < end) & pos["trader_id"].isin(members[i])])
    return pd.concat(parts).sort_values("entry_ts") if parts else pos.iloc[:0]


# ---------------------------------------------------------------- pool simulation
def simulate(ent: pd.DataFrame, grid: np.ndarray, P: np.ndarray, col: dict[str, int], start: int, brakes: bool = True,
             detail: bool = False) -> dict[str, Any]:
    e_ts = ent["entry_ts"].to_numpy(np.int64)
    e_h = (e_ts // H) * H
    rows = list(zip(ent["trader_id"], ent["inst_id"].map(col).fillna(-1).astype(int), ent["sgn"].to_numpy(int), ent["exit_ts"].to_numpy(np.int64),
                    ent["entry_px"].to_numpy(float), ent["ret"].to_numpy(float), ent["stop"].to_numpy(float)))
    j, n = 0, len(rows)
    cash, peak, peak_all, dd_max = EQ0, EQ0, EQ0, 0.0
    book: list[list[Any]] = []          # [tid, col, sgn, exit_ts, epx, ret, stop, notional, inst_side]
    held: set[tuple[int, int]] = set()
    halt_until, day_block, day_open, cur_day = 0, -1, EQ0, -1
    skipped = {"cap": 0, "same_coin": 0, "member_cap": 0, "brake": 0, "no_price": 0}
    taken, halts, half_h, uw_start, uw_max = 0, 0, 0, None, 0
    pnl_by: dict[str, float] = {}
    daily: list[tuple[int, float]] = []
    gi = np.searchsorted(grid, start // H * H)
    while j < n and e_h[j] < grid[gi]:
        j += 1
    for hi in range(gi, len(grid)):
        h = int(grid[hi])
        if book:
            keep = []
            for b in book:
                if b[3] < h + H:
                    pnl = b[7] * b[5]
                    cash += pnl
                    pnl_by[b[0]] = pnl_by.get(b[0], 0.0) + pnl
                    held.discard(b[8])
                else:
                    keep.append(b)
            book = keep
        unreal = 0.0
        row = P[hi]
        for b in book:
            px = row[b[1]]
            if np.isfinite(px):
                unreal += b[7] * max(b[2] * (px / b[4] - 1), -b[6])
        eq = cash + unreal
        day = h // D
        if day != cur_day:
            if cur_day >= 0:
                daily.append((cur_day * D, eq))
            cur_day, day_open = day, eq
        if h >= halt_until and halt_until:
            peak, halt_until = eq, 0
        peak, peak_all = max(peak, eq), max(peak_all, eq)
        dd = 1 - eq / peak if peak > 0 else 0.0            # for the brakes (the peak resets after a halt)
        dd_all = 1 - eq / peak_all if peak_all > 0 else 0.0  # reported: from the highest equity so far
        dd_max = max(dd_max, dd_all)
        if dd_all > 1e-9:
            uw_start = uw_start if uw_start is not None else h
            uw_max = max(uw_max, (h - uw_start) // D)
        else:
            uw_start = None
        mult = 1.0
        if brakes:
            if dd >= DD_HALT and not halt_until:
                halt_until, halts = h + HALT_D * D, halts + 1
            if eq <= day_open * (1 - DAY_LOSS):
                day_block = day
            if dd >= DD_HALF:
                mult, half_h = 0.5, half_h + 1
        while j < n and e_h[j] <= h:
            tid, c, sgn, x_ts, epx, ret, stop, = rows[j]
            j += 1
            if brakes and (halt_until or day_block == day):
                skipped["brake"] += 1
                continue
            if c < 0 or not np.isfinite(row[c]):
                skipped["no_price"] += 1
                continue
            if (c, sgn) in held:
                skipped["same_coin"] += 1
                continue
            if len(book) >= MAX_OPEN:
                skipped["cap"] += 1
                continue
            if sum(1 for b in book if b[0] == tid) >= MAX_PER_MEMBER:
                skipped["member_cap"] += 1
                continue
            notional = min(eq * RISK / stop * mult, eq * MAX_POS_FRAC)
            gross = sum(b[7] for b in book)
            side = sum(b[7] for b in book if b[2] == sgn)
            if notional <= 0 or gross + notional > GROSS * eq or side + notional > SIDE_CAP * eq:
                skipped["cap"] += 1
                continue
            book.append([tid, c, sgn, int(x_ts), epx, ret, stop, notional, (c, sgn)])
            held.add((c, sgn))
            taken += 1
    for b in book:                       # positions still open at the end close at their recorded result
        pnl = b[7] * b[5]
        cash += pnl
        pnl_by[b[0]] = pnl_by.get(b[0], 0.0) + pnl
    daily.append((cur_day * D, cash))
    eqs = np.array([v for _, v in daily])
    out: dict[str, Any] = {"ret": cash / EQ0 - 1, "max_dd": dd_max, "underwater_days": int(uw_max), "trades": taken,
                           "signals": n, "skipped": skipped, "halts": halts, "half_size_share": half_h / max(1, len(grid) - gi),
                           "days": len(daily)}
    if len(eqs) >= 8:
        wk = eqs[7::7] / eqs[:-7:7][: len(eqs[7::7])] - 1
        out.update({"worst_week": float(wk.min()), "best_week": float(wk.max()), "pos_weeks": float((wk > 0).mean()), "weeks": int(len(wk)),
                    "ret_ex_best_week": float((1 + out["ret"]) / (1 + wk.max()) - 1)})
    if len(eqs) >= 31:
        out["worst_30d"] = float((eqs[30:] / eqs[:-30] - 1).min())
    if len(eqs) >= 4:
        mid = len(eqs) // 2
        out["halves"] = [float(eqs[mid] / EQ0 - 1), float(eqs[-1] / eqs[mid] - 1)]
    if detail:
        out["curve"] = {"t": [int(t) // 1000 for t, _ in daily], "v": [round(float(v), 2) for _, v in daily]}
        out["by_member"] = sorted(({"trader_id": k, "pnl": round(v, 2)} for k, v in pnl_by.items()), key=lambda x: -x["pnl"])
    return out


# ---------------------------------------------------------------- one source
def run_source(cfg: Config, source: str) -> dict[str, Any] | None:
    from okxrev import replay
    pos, kinds = load_positions(cfg, source)
    if pos.empty:
        return None
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]))
    pos = prepare(pos, cost)
    pos["kind"] = pos["trader_id"].map(kinds).fillna("?")
    rand = pos[pos["kind"].eq("random")]
    cand = pos[~pos["kind"].isin(EXCL_KINDS) & ~pos["trader_id"].str.endswith("_ctrl")]
    t0, t1 = int(pos["entry_ts"].min()), int(pos["exit_ts"].max())
    path = cfg.paths.db if source == "live" else replay.old_db_path(cfg)
    log.info("pool %s: %d positions, %d candidates, %s .. %s", source, len(pos), cand["trader_id"].nunique(), ms_to_str(t0), ms_to_str(t1))
    grid, P, col = hourly_close(path, sorted(pos["inst_id"].unique()), t0 - H, t1 + 2 * H)
    rng = np.random.default_rng(0)
    out: dict[str, Any] = {"range": [ms_to_str(t0, "%Y-%m-%d"), ms_to_str(t1, "%Y-%m-%d")], "candidates": int(cand["trader_id"].nunique()), "runs": []}
    for look in LOOKS:
        start = (t0 // D) * D + look * D
        if start >= t1 - STEP_D * D:
            continue
        steps = np.arange(start, t1, STEP_D * D, dtype=np.int64)
        stats = [window_stats(cand, rand, int(t), look) for t in steps]
        min_n = MIN_N[look]
        for k in KS:
            main = (k, look) == MAIN
            picks = [pick(st, k, min_n) for st in stats]
            mem = [[t for t, _ in p] for p in picks]
            ent = schedule_entries(cand, steps, mem)
            res = simulate(ent, grid, P, col, int(steps[0]), detail=main)
            sims = []
            for _ in range(SIMS_MAIN if main else SIMS):
                rm = [pick_random(st, k, min_n, rng) for st in stats]
                sims.append(simulate(schedule_entries(cand, steps, rm), grid, P, col, int(steps[0]))["ret"])
            sims_a = np.array(sims)
            row = {"k": k, "look": look, "main": main, "start": ms_to_str(int(steps[0]), "%Y-%m-%d"), "steps": int(len(steps)),
                   "empty_steps": int(sum(1 for m in mem if not m)), **res,
                   "random_members": {"p05": float(np.quantile(sims_a, 0.05)), "p50": float(np.median(sims_a)), "p95": float(np.quantile(sims_a, 0.95)),
                                      "beat": float((sims_a < res["ret"]).mean()), "sims": len(sims)}}
            if main:
                row["picks"] = [{"t": ms_to_str(int(t), "%Y-%m-%d"), "members": [{"trader_id": a, "t": round(b, 2)} for a, b in p]} for t, p in zip(steps, picks)]
                row["no_brakes"] = simulate(ent, grid, P, col, int(steps[0]), brakes=False)
                allm = [sorted(cand["trader_id"].unique().tolist())] * len(steps)
                row["all"] = simulate(schedule_entries(cand, steps, allm), grid, P, col, int(steps[0]), detail=True)
                rnd_ids = [sorted(rand["trader_id"].unique().tolist())] * len(steps)
                row["random_entries"] = simulate(schedule_entries(rand, steps, rnd_ids), grid, P, col, int(steps[0]), detail=True)
            log.info("pool %s k=%d look=%d: ret %+.1f%% dd %.1f%% (random members median %+.1f%%, beat %.0f%%)", source, k, look,
                     res["ret"] * 100, res["max_dd"] * 100, row["random_members"]["p50"] * 100, row["random_members"]["beat"] * 100)
            out["runs"].append(row)
    return out


def evaluate(cfg: Config, sources: tuple[str, ...] = ("live", "blind")) -> dict[str, Any]:
    t0 = time.monotonic()
    out = {s: r for s in sources if (r := run_source(cfg, s))}
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1), "sources": out,
           "params": {"step_d": STEP_D, "looks": list(LOOKS), "ks": list(KS), "main": {"k": MAIN[0], "look": MAIN[1]}, "min_n": MIN_N, "t_min": T_MIN,
                      "eq0": EQ0, "risk": RISK, "stop": STOP, "max_pos_frac": MAX_POS_FRAC, "gross": GROSS, "side_cap": SIDE_CAP,
                      "max_open": MAX_OPEN, "max_per_member": MAX_PER_MEMBER, "dd_half": DD_HALF, "dd_halt": DD_HALT, "halt_d": HALT_D, "day_loss": DAY_LOSS},
           "caveats": [f"主要設定（每 {STEP_D} 天重選、回看 {MAIN[1]} 天、前 {MAIN[0]} 名）是事先定的；其他組合只是參考，挑最好的那組等於事後選（多重比較）",
                       "只用已平倉的帳本 C 紀錄重跑：進出場時間、報酬照原紀錄，部位大小與能不能進場由資金池決定",
                       f"止損用每筆記錄的最大反向波動判斷（最多 {STOP:.0%}），碰到就算 -止損 - 成本，不知道實際碰到的時間，資金會被多占一段",
                       "每小時用收盤價盯市，比逐筆粗；未含滑價以外的衝擊成本",
                       "即時紙上交易只有幾週，回看 30 天以上的設定幾乎沒有樣本；主要看 2024-25 盲測",
                       "盲測期間只有現存的老合約（倖存者偏差）"]}
    p = cfg.paths.results / "pool.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    log.info("pool: done in %.0fs", res["elapsed_sec"])
    return res


def load(cfg: Config) -> dict[str, Any] | None:
    from okxrev import poolml
    p = cfg.paths.results / "pool.json"
    try:
        res = json.loads(p.read_text(encoding="utf-8").replace("NaN", "null").replace("Infinity", "null")) if p.exists() else None
    except ValueError:
        return None
    if res is not None:
        res["model"] = poolml.load(cfg)     # model filter (okxrev.poolml), shown with the pool study
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.pool")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--source", choices=["live", "blind", "both"], default="both")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "pool")
    if args.evaluate:
        from okxrev import poolml
        evaluate(cfg, ("live", "blind") if args.source == "both" else (args.source,))
        if args.source != "live":
            poolml.evaluate(cfg)            # the model filter is compared with this pool, so it reruns with it
    res = load(cfg)
    if not res:
        print("no results yet: python -m okxrev.pool --evaluate")
        return 0
    for src, s in res["sources"].items():
        print(f"\n== {src} {s['range'][0]} .. {s['range'][1]}, {s['candidates']} candidates")
        for r in s["runs"]:
            rm = r["random_members"]
            print(f"{'*' if r['main'] else ' '} k={r['k']:2d} look={r['look']:2d}d  ret {r['ret'] * 100:+7.1f}%  dd {r['max_dd'] * 100:5.1f}%  "
                  f"trades {r['trades']:5d}  random members {rm['p05'] * 100:+6.1f} / {rm['p50'] * 100:+6.1f} / {rm['p95'] * 100:+6.1f}%  beat {rm['beat'] * 100:3.0f}%")
            if r["main"]:
                for k in ("no_brakes", "all", "random_entries"):
                    x = r[k]
                    print(f"    {k:15s} ret {x['ret'] * 100:+7.1f}%  dd {x['max_dd'] * 100:5.1f}%  trades {x['trades']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
