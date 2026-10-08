from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from okxrev import db  # noqa: E402
from okxrev.config import Config, load_config, ms_to_str  # noqa: E402
from okxrev.detector import BAR_MS  # noqa: E402
from okxrev.log import setup_logging  # noqa: E402

log = logging.getLogger("okxrev.charts")

UP = "#26a69a"
DOWN = "#ef5350"


def _pct(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return "-" if np.isnan(f) else f"{f * 100:+.1f}%"


def _candles(ax: Any, x: np.ndarray, g: pd.DataFrame, w: float) -> None:
    o, h, lo, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    for i in range(len(x)):
        if np.isnan(o[i]):
            continue
        col = UP if c[i] >= o[i] else DOWN
        ax.vlines(x[i], lo[i], h[i], color=col, linewidth=0.6)
        ax.add_patch(Rectangle((x[i] - w / 2, min(o[i], c[i])), w, max(abs(c[i] - o[i]), 1e-12), color=col, linewidth=0))


def draw_event(cfg: Config, ev: pd.Series, g: pd.DataFrame, btc: pd.DataFrame) -> Any:
    ch = cfg.charts
    t_ev = int(ev["event_ts"])
    lo = t_ev - int(ch["pre_hours"]) * 12 * BAR_MS
    hi = t_ev + int(ch["post_hours"]) * 12 * BAR_MS
    w = g.loc[(g.index >= lo) & (g.index <= hi)]
    hours = (w.index.to_numpy(np.int64) - t_ev) / BAR_MS / 12

    fig, axes = plt.subplots(3, 1, figsize=(13, 8.5), sharex=True, gridspec_kw={"height_ratios": [3.2, 1.2, 1.2]})
    ax, axv, axb = axes
    bw = 0.7 / 12
    _candles(ax, hours, w, bw)
    o = w["open"].to_numpy(float)
    c = w["close"].to_numpy(float)

    entry = float(ev["entry_price"]) if pd.notna(ev["entry_price"]) else np.nan
    ax.axvline(0, color="black", linestyle="--", linewidth=0.8)
    if not np.isnan(entry):
        ax.axhline(entry, color="#1565c0", linewidth=0.8, label=f"entry {entry:.6g}")
        if pd.notna(ev.get("mae_24h")):
            ax.axhline(entry * (1 + float(ev["mae_24h"])), color="#e65100", linestyle=":", linewidth=0.9, label=f"MAE {_pct(ev['mae_24h'])}")
        if pd.notna(ev.get("mfe_24h")):
            ax.axhline(entry * (1 + float(ev["mfe_24h"])), color="#2e7d32", linestyle=":", linewidth=0.9, label=f"MFE {_pct(ev['mfe_24h'])}")
        ax.axvspan(0, 24, color="#90caf9", alpha=0.07)
    ax.legend(loc="upper right", fontsize=8)
    ax.set_ylabel("price")
    ax.set_yscale("log")
    fmt = FuncFormatter(lambda v, _: f"{v:.6g}")
    ax.yaxis.set_major_formatter(fmt)
    ax.yaxis.set_minor_formatter(fmt)
    ax.grid(alpha=0.2)
    ax.set_title(
        f"{ev['inst_id']}  {ms_to_str(t_ev)} UTC  [{ev.get('mode', '')}]   red {_pct(ev['red_ret_prev_close'])}  "
        f"vol x{float(ev['red_vol_ratio']):.1f}   24h {_pct(ev.get('fwd_ret_24h'))}   BTC {ev.get('btc_regime', '-')}",
        fontsize=10,
    )

    vq = w["vol_quote"].to_numpy(float)
    vcol = [UP if (not np.isnan(o[i]) and c[i] >= o[i]) else DOWN for i in range(len(hours))]
    axv.bar(hours, np.nan_to_num(vq), width=bw, color=vcol)
    axv.plot(hours, w["vol_ma"].to_numpy(float), color="#424242", linewidth=0.8, label="vol MA20")
    axv.axvline(0, color="black", linestyle="--", linewidth=0.8)
    axv.set_ylabel("USDT vol")
    axv.legend(loc="upper right", fontsize=8)
    axv.grid(alpha=0.2)

    bw_ = btc.loc[(btc.index >= lo) & (btc.index <= hi), "btc_close"]
    if len(bw_) and t_ev in bw_.index and pd.notna(bw_.loc[t_ev]):
        axb.plot((bw_.index.to_numpy(np.int64) - t_ev) / BAR_MS / 12, bw_ / bw_.loc[t_ev] * 100, color="#f57c00", linewidth=0.9)
    axb.axvline(0, color="black", linestyle="--", linewidth=0.8)
    axb.axhline(100, color="grey", linewidth=0.5)
    axb.set_ylabel("BTC (=100)")
    axb.set_xlabel("hours from event")
    axb.grid(alpha=0.2)

    txt = (
        f"pre 6h {_pct(ev.get('ret_6h_pre'))}   pre 24h {_pct(ev.get('ret_24h_pre'))}{' (new listing)' if bool(ev.get('is_new_listing')) else ''}   |   "
        f"fwd 30m {_pct(ev.get('fwd_ret_30m'))}  1h {_pct(ev.get('fwd_ret_1h'))}  6h {_pct(ev.get('fwd_ret_6h'))}  "
        f"24h {_pct(ev.get('fwd_ret_24h'))}  3d {_pct(ev.get('fwd_ret_3d'))}   |   "
        f"MAE {_pct(ev.get('mae_24h'))}  MFE {_pct(ev.get('mfe_24h'))}  new high 24h: {ev.get('new_high_24h')}"
    )
    fig.text(0.01, 0.005, txt, fontsize=8.5, family="monospace")
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    return fig


def draw_events(cfg: Config, events: pd.DataFrame, limit: int) -> int:
    from okxrev.detector import btc_features, prepare

    if events.empty:
        return 0
    out = cfg.paths.charts
    out.mkdir(parents=True, exist_ok=True)
    events = events.sort_values("event_ts").head(limit)
    pre = int(cfg.charts["pre_hours"]) * 12 * BAR_MS
    post = int(cfg.charts["post_hours"]) * 12 * BAR_MS
    warm = 300 * BAR_MS
    n = 0
    with db.open_db(cfg.paths.db, read_only=True) as con:
        btc_raw = db.load_candles(con, cfg.universe["btc_inst"],
                                  int(events["event_ts"].min()) - pre - warm, int(events["event_ts"].max()) + post + BAR_MS)
    btc = btc_features(btc_raw, cfg.btc_regime)
    for inst, grp in events.groupby("inst_id"):
        lo = int(grp["event_ts"].min()) - pre - warm
        hi = int(grp["event_ts"].max()) + post + BAR_MS
        with db.open_db(cfg.paths.db, read_only=True) as con:
            raw = db.load_candles(con, inst, lo, hi)
        if raw.empty:
            continue
        g = prepare(raw, cfg.detector)
        for _, ev in grp.iterrows():
            fig = draw_event(cfg, ev, g, btc)
            name = f"{inst}_{ms_to_str(int(ev['event_ts']), '%Y%m%d_%H%M')}.png"
            fig.savefig(out / name, dpi=int(cfg.charts["dpi"]))
            plt.close(fig)
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.charts")
    ap.add_argument("--config", default=None)
    ap.add_argument("--events", default=None, help="events.csv path (default results/events.csv)")
    ap.add_argument("--mode", default="pump_red")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "backtest")
    path = args.events or (cfg.paths.results / "events.csv")
    ev = pd.read_csv(path)
    ev = ev[ev["mode"] == args.mode]
    n = draw_events(cfg, ev, args.limit or int(cfg.charts["max_charts"]))
    log.info("charts: %d written to %s", n, cfg.paths.charts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
