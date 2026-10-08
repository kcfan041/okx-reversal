from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str, parse_utc_ms
from okxrev.detector import MODES, apply_cooldown, btc_features, mode_masks, prepare, pump_mask, red_mask
from okxrev.log import setup_logging
from okxrev.metrics import event_metrics, summarize

log = logging.getLogger("okxrev.backtest")

EVENT_COLUMNS = [
    "mode", "inst_id", "event_ts", "event_time_utc", "is_new_listing",
    "ret_1h_pre", "ret_6h_pre", "ret_24h_pre", "pump_vol_ratio_max",
    "red_ret_prev_close", "red_ret_body", "red_vol_ratio",
    "btc_ret_1h", "btc_ret_4h", "btc_below_ema", "btc_regime",
    "entry_price",
    "fwd_ret_30m", "fwd_ret_1h", "fwd_ret_6h", "fwd_ret_24h", "fwd_ret_3d",
    "mae_24h", "mfe_24h", "mae_before_mfe", "bars_to_mfe", "new_high_24h",
    "short_pnl_net_24h", "incomplete",
]

CAVEATS = [
    "survivorship bias: instruments delisted before the system started are missing (OKX REST returns 51001)",
    "funding rate not included in short PnL",
    "volume is OKX-only",
    "events are not independent (market-wide crashes trigger many at once); see n_days and by_month",
    "slippage is a flat estimate; illiquid coins may fill much worse than open[t+1]",
    "parameter sweep: picking the best of many combinations is optimistic (multiple comparisons)",
]


class Reservoir:
    def __init__(self, k: int, seed: int):
        self.k = k
        self.rng = np.random.default_rng(seed)
        self.seen = 0
        self.items: list[tuple[str, int]] = []

    def offer(self, inst_id: str, idx: np.ndarray) -> list[int]:
        keep_new: list[int] = []
        for i in idx:
            self.seen += 1
            if len(self.items) < self.k:
                self.items.append((inst_id, int(i)))
                keep_new.append(len(self.items) - 1)
            else:
                j = int(self.rng.integers(0, self.seen))
                if j < self.k:
                    self.items[j] = (inst_id, int(i))
                    keep_new.append(j)
        return keep_new


class BaselineSampler:
    def __init__(self, k: int, seed: int):
        self.res = Reservoir(k, seed)
        self.rows: dict[int, dict[str, Any]] = {}

    def offer(self, inst_id: str, g: pd.DataFrame, idx: np.ndarray, m_cfg: dict[str, Any]) -> None:
        slots = self.res.offer(inst_id, idx)
        if not slots:
            return
        chosen = [self.res.items[s][1] for s in slots]
        mt = event_metrics(g, np.asarray(chosen), m_cfg)
        mt.insert(0, "inst_id", inst_id)
        for s, rec in zip(slots, mt.to_dict("records")):
            self.rows[s] = rec

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([self.rows[k] for k in sorted(self.rows)])


def eligible_idx(g: pd.DataFrame, m_cfg: dict[str, Any], lo: int | None, hi: int | None) -> np.ndarray:
    ok = g["ret_1"].notna() & g["vol_ratio"].notna()
    ok &= g["open"].shift(-1).notna()
    ok &= g["close"].shift(-int(m_cfg["excursion_bars"])).notna()
    ts = g.index.to_numpy(np.int64)
    m = ok.to_numpy()
    if lo is not None:
        m &= ts >= lo
    if hi is not None:
        m &= ts < hi
    return np.flatnonzero(m)


def in_range(idx: np.ndarray, ts: np.ndarray, lo: int | None, hi: int | None) -> np.ndarray:
    if len(idx) == 0:
        return idx
    m = np.ones(len(idx), bool)
    if lo is not None:
        m &= ts[idx] >= lo
    if hi is not None:
        m &= ts[idx] < hi
    return idx[m]


def run_backtest(cfg: Config, since: str | None, until: str | None, insts: list[str] | None, sweep: bool) -> dict[str, Any]:
    t0 = time.monotonic()
    det, met, reg, bt = cfg.detector, cfg.metrics, cfg.btc_regime, cfg.backtest
    lo = parse_utc_ms(since) if since else None
    hi = parse_utc_ms(until) if until else None
    warm = 300 * db.BAR_MS
    tail = (int(max(met["horizons"].values())) + 10) * db.BAR_MS
    load_lo = lo - warm if lo is not None else None
    load_hi = hi + tail if hi is not None else None
    btc_id = cfg.universe["btc_inst"]

    with db.open_db(cfg.paths.db, read_only=True) as con:
        all_insts = db.list_candle_insts(con)
        meta = db.sync_overview(con).set_index("inst_id")
        btc_raw = db.load_candles(con, btc_id, load_lo, load_hi)
    if btc_raw.empty:
        raise SystemExit(f"no {btc_id} data in database; run sync first")
    btc_feat = btc_features(btc_raw, reg)
    del btc_raw

    targets = [i for i in (insts or all_insts) if i != btc_id and i in set(all_insts)]
    log.info("backtest: %d instruments, range %s .. %s", len(targets), since or "start", until or "end")

    base_cfg = bt["baseline"]
    k = int(base_cfg["random_samples"])
    seed = int(base_cfg["seed"])
    rnd_any = BaselineSampler(k, seed)
    rnd_pump = BaselineSampler(k, seed + 1)

    sweep_grid = [(r, v) for r in bt["sweep"]["red_ret_max"] for v in bt["sweep"]["red_vol_ratio_min"]] if sweep else []
    sweep_events: dict[tuple[float, float], list[pd.DataFrame]] = {p: [] for p in sweep_grid}

    event_frames: list[pd.DataFrame] = []
    cd = int(det["cooldown_bars"])
    for n, inst in enumerate(targets, 1):
        with db.open_db(cfg.paths.db, read_only=True) as con:
            raw = db.load_candles(con, inst, load_lo, load_hi)
        if len(raw) < 400:
            continue
        lt = meta.at[inst, "list_time"] if inst in meta.index else None
        lt = None if lt is None or pd.isna(lt) else int(lt)
        g = prepare(raw, det, btc_feat, list_time=lt)
        del raw
        ts = g.index.to_numpy(np.int64)
        pump = pump_mask(g, det)
        red = red_mask(g, det)
        for mode, mask in mode_masks(g, det, red=red, pump=pump).items():
            idx = in_range(apply_cooldown(mask, cd), ts, lo, hi)
            if len(idx):
                mt = event_metrics(g, idx, met)
                mt.insert(0, "inst_id", inst)
                mt.insert(0, "mode", mode)
                event_frames.append(mt)

        elig = eligible_idx(g, met, lo, hi)
        rnd_any.offer(inst, g, elig, met)
        pump_only = elig[pump[elig] & ~red[elig]] if len(elig) else elig
        rnd_pump.offer(inst, g, pump_only, met)

        for (r, v) in sweep_grid:
            m = pump & red_mask(g, det, ret_max=r, vol_ratio_min=v)
            idx = in_range(apply_cooldown(m, cd), ts, lo, hi)
            if len(idx):
                mt = event_metrics(g, idx, met)
                mt.insert(0, "inst_id", inst)
                sweep_events[(r, v)].append(mt)
        if n % 50 == 0:
            log.info("  %d/%d instruments processed", n, len(targets))

    events = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame(columns=EVENT_COLUMNS)
    extra = [c for c in events.columns if c not in EVENT_COLUMNS]
    events = events.reindex(columns=EVENT_COLUMNS + extra).sort_values(["mode", "event_ts", "inst_id"]).reset_index(drop=True)

    boot = bt["bootstrap"]
    modes = {m: summarize(events[events["mode"] == m], met, boot) for m in MODES}
    any_df = rnd_any.frame()
    pump_df = rnd_pump.frame()
    baselines = {
        "random_any": summarize(any_df, met, boot) if len(any_df) else {"n_events": 0},
        "random_pump": summarize(pump_df, met, boot) if len(pump_df) else {"n_events": 0},
    }
    baselines["random_any"]["population"] = rnd_any.res.seen
    baselines["random_pump"]["population"] = rnd_pump.res.seen

    sweep_df = None
    if sweep:
        rows = []
        for (r, v), frames in sweep_events.items():
            ev = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            s = summarize(ev, met, boot, by_group=False) if len(ev) else {"n_events": 0}
            h = s.get("horizons", {}).get("24h", {})
            rows.append({
                "red_ret_max": r, "red_vol_ratio_min": v,
                "n": h.get("n", 0), "n_insts": h.get("n_insts", 0), "n_days": h.get("n_days", 0),
                "fwd_ret_24h_mean": h.get("mean"), "fwd_ret_24h_median": h.get("median"),
                "bear_rate_24h": h.get("bear_rate"), "mae_mean": s.get("mae_mean"),
                "short_pnl_net_24h_mean": h.get("short_pnl_net_mean"),
                "ci95_lo": (h.get("mean_ci95") or [None, None])[0], "ci95_hi": (h.get("mean_ci95") or [None, None])[1],
            })
        sweep_df = pd.DataFrame(rows)

    ov = meta
    has = ov[ov["oldest_ts"].notna()]
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "range": {"since": since, "until": until},
        "data_coverage": {
            "n_insts": int(len(has)),
            "n_insts_tested": len(targets),
            "first_ts": ms_to_str(has["oldest_ts"].min(), "%Y-%m-%dT%H:%MZ") if len(has) else None,
            "last_ts": ms_to_str(has["newest_ts"].max(), "%Y-%m-%dT%H:%MZ") if len(has) else None,
            "median_oldest_ts": ms_to_str(has["oldest_ts"].median(), "%Y-%m-%dT%H:%MZ") if len(has) else None,
            "backfill_done": int(has["backfill_done"].sum()),
            "delisted_included": int((has["state"] == "delisted").sum()),
        },
        "config": {"detector": det, "metrics": met, "btc_regime": reg, "baseline": base_cfg, "bootstrap": boot},
        "modes": modes,
        "baselines": baselines,
        "caveats": CAVEATS,
    }
    return {"events": events, "summary": summary, "sweep": sweep_df}


def pct(v: Any, w: int = 8) -> str:
    return f"{'-':>{w}}" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v * 100:>{w - 1}.1f}%"


def print_summary(summary: dict[str, Any], sweep_df: pd.DataFrame | None) -> None:
    cov = summary["data_coverage"]
    print(f"Data: {cov['n_insts']} instruments ({cov['n_insts_tested']} tested), {cov['first_ts']} .. {cov['last_ts']}, "
          f"median oldest {cov['median_oldest_ts']}, backfill done {cov['backfill_done']}")
    print()
    hdr = f"{'':<18}{'n':>6}{'insts':>7}{'days':>6}{'24h mean':>10}{'24h med':>10}{'bear':>8}{'MAE mean':>10}{'MFE mean':>10}{'net short':>11}   CI95 of mean"
    print(hdr)
    rows = [(m, summary["modes"][m]) for m in MODES] + [(b, summary["baselines"][b]) for b in ("random_pump", "random_any")]
    for name, s in rows:
        h = s.get("horizons", {}).get("24h", {})
        ci = h.get("mean_ci95") or [None, None]
        print(f"{name:<18}{h.get('n', 0):>6}{h.get('n_insts', 0):>7}{h.get('n_days', 0):>6}"
              f"{pct(h.get('mean'), 10)}{pct(h.get('median'), 10)}{pct(h.get('bear_rate'))}"
              f"{pct(s.get('mae_mean'), 10)}{pct(s.get('mfe_mean'), 10)}{pct(h.get('short_pnl_net_mean'), 11)}"
              f"   [{pct(ci[0], 7)}, {pct(ci[1], 7)}]")
    print()
    print("Mean forward return by horizon:")
    names = list(summary["config"]["metrics"]["horizons"].keys())
    print(f"{'':<18}" + "".join(f"{n:>9}" for n in names))
    for name, s in rows:
        hs = s.get("horizons", {})
        print(f"{name:<18}" + "".join(pct(hs.get(n, {}).get("mean"), 9) for n in names))
    pr = summary["modes"]["pump_red"]
    if pr.get("by_btc_regime"):
        print()
        print("pump_red by BTC regime (24h):")
        for reg, s in pr["by_btc_regime"].items():
            h = s.get("horizons", {}).get("24h", {})
            print(f"  {reg:<10} n={h.get('n', 0):>4}  mean {pct(h.get('mean'))}  bear {pct(h.get('bear_rate'))}  MAE {pct(s.get('mae_mean'))}")
    if sweep_df is not None and len(sweep_df):
        print()
        print("Sweep (pump_red, 24h) - all combinations shown:")
        print(f"{'red<=':>7}{'vol>=':>7}{'n':>6}{'mean':>9}{'median':>9}{'bear':>8}{'MAE':>8}{'net':>9}")
        for r in sweep_df.itertuples(index=False):
            print(f"{r.red_ret_max * 100:>6.0f}%{r.red_vol_ratio_min:>7.1f}{r.n:>6}{pct(r.fwd_ret_24h_mean, 9)}{pct(r.fwd_ret_24h_median, 9)}"
                  f"{pct(r.bear_rate_24h)}{pct(r.mae_mean)}{pct(r.short_pnl_net_24h_mean, 9)}")


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.backtest")
    ap.add_argument("--config", default=None)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--insts", nargs="+", default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "backtest")
    since = args.since or cfg.backtest.get("since")
    until = args.until or cfg.backtest.get("until")
    res = run_backtest(cfg, since, until, args.insts, args.sweep)

    out = cfg.paths.results
    out.mkdir(parents=True, exist_ok=True)
    res["events"].to_csv(out / "events.csv", index=False, encoding="utf-8", float_format="%.6g")
    with open(out / "summary.json", "w", encoding="utf-8") as f:
        json.dump(res["summary"], f, ensure_ascii=False, indent=2, default=_json_default)
    if res["sweep"] is not None:
        res["sweep"].to_csv(out / "sweep.csv", index=False, encoding="utf-8", float_format="%.6g")
    log.info("wrote %s (%d events)", out / "events.csv", len(res["events"]))
    print()
    print_summary(res["summary"], res["sweep"])

    if cfg.charts["enabled"] and not args.no_charts:
        from okxrev.charts import draw_events
        ev = res["events"]
        ev = ev[(ev["mode"] == "pump_red")]
        n = draw_events(cfg, ev, limit=int(cfg.charts["max_charts"]))
        log.info("charts: %d written to %s", n, cfg.paths.charts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
