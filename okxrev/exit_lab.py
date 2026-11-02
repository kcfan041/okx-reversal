"""Human-like exits for the discovered rules: does a stop/target, trailing stop, breakeven, partial take-profit or a
structure stop beat the rule's plain time exit on the same entries?

Entries: the active rules' events in results/discover_events.parquet (live database, split by time into a search part,
first 2/3, and a validation part) and the same rules' blind-replay positions in data/replay.duckdb (old database).
Paths come from the 5m candles from the entry bar; every preset keeps the rule's holding time as the time limit.
The exit simulator is exits.simulate (pessimistic intrabar order: the adverse extreme first; trailing and breakeven only
use the peak of earlier bars). Costs: round-trip fee plus entry slippage, plus exit slippage on stop / time exits.

Presets are in units of R = the 14-bar 5m ATR before entry x sqrt(holding bars), roughly one standard deviation of
the move over the rule's holding time (a 5m ATR alone is far too tight for 1-3 day holds); structure = distance to
the 24h high (short) / low (long) before entry:
  time          the rule's own exit, no stop                    tp1_sl075 / tp15_sl1   fixed target and stop
  trail05 / trail1  trailing stop after 0.5 / 1 R of profit     be05       stop to breakeven after 0.5 R, target 1.5 R
  partial       half off at 0.75 R, the rest breakeven + trail   struct / struct_2r   stop beyond the 24h extreme (+ 2x risk target)

A preset "improves" a rule when its mean net return per trade beats the time exit in all three segments and is
positive in validation and blind. Those rules get a paper trader variant `<rule>_hx` with the best such preset
(chosen on the search part).
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
from okxrev.exits import INF, PARAMS, atr_pct, simulate
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.exit_lab")

BAR = db.BAR_MS
HOLD = {"15m": 3, "1h": 12, "6h": 72, "24h": 288, "3d": 864, "6h_sl2atr": 72, "24h_sl2atr": 288}
PRESETS: dict[str, dict[str, Any]] = {
    "time": {"label": "原本的時間出場", "unit": "r", "p": {}},
    "tp1_sl075": {"label": "止盈 1R／止損 0.75R", "unit": "r", "p": {"tp": 1, "sl": 0.75}},
    "tp15_sl1": {"label": "止盈 1.5R／止損 1R", "unit": "r", "p": {"tp": 1.5, "sl": 1}},
    "trail05": {"label": "獲利 0.5R 後移動停利 0.5R（止損 1R）", "unit": "r", "p": {"trail_act": 0.5, "trail_dist": 0.5, "sl": 1}},
    "trail1": {"label": "獲利 1R 後移動停利 0.75R（止損 1.25R）", "unit": "r", "p": {"trail_act": 1, "trail_dist": 0.75, "sl": 1.25}},
    "be05": {"label": "獲利 0.5R 後保本，止盈 1.5R（止損 1R）", "unit": "r", "p": {"be_trig": 0.5, "tp": 1.5, "sl": 1}},
    "partial": {"label": "0.75R 先出一半，其餘保本＋移動停利 0.5R（止損 1R）", "unit": "r", "p": {"part_tp": 0.75, "trail_dist": 0.5, "sl": 1}},
    "struct": {"label": "止損放在 24h 高點（做空）／低點（做多）外", "unit": "struct", "p": {"sl": 1}},
    "struct_2r": {"label": "24h 高低點外止損＋2 倍風險止盈", "unit": "struct", "p": {"sl": 1, "tp": 2}},
}
ENGINE_OK = ("time", "tp1_sl075", "tp15_sl1", "trail05", "trail1", "be05", "struct", "struct_2r")   # partial: research only
SPLIT = 2 / 3
MAX_EVENTS = 6000


def paths_for(path: Any, trades: pd.DataFrame, horizon: int) -> dict[str, Any]:
    """Like exits.build_paths (signed so favorable > 0) plus the structure distance (24h extreme before entry)."""
    t = trades.reset_index(drop=True)
    n = len(t)
    fav, adv, cls = (np.full((n, horizon), np.nan) for _ in range(3))
    atr, struct = np.full(n, np.nan), np.full(n, np.nan)
    with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=60) as con:
        for inst, rows in t.groupby("inst_id"):
            lo_ = int(rows["open_ts"].min()) - 300 * BAR
            hi_ = int(rows["open_ts"].max()) + (horizon + 2) * BAR
            g = db.load_candles(con, inst, lo_, hi_)
            if g.empty:
                continue
            from okxrev.detector import to_grid
            g = to_grid(g)
            idx = g.index.to_numpy(np.int64)
            h, lo, c = (g[k].to_numpy(float) for k in ("high", "low", "close"))
            for k, tr in rows.iterrows():
                ie = int((int(tr["open_ts"]) - int(idx[0])) // BAR)
                if ie < 289 or ie >= len(c) or not np.isfinite(c[ie - 1]):
                    continue
                ref = float(c[ie - 1])
                s = -1.0 if tr["side"] == "short" else 1.0
                end = min(len(c), ie + horizon)
                hh, ll, cc = h[ie:end], lo[ie:end], c[ie:end]
                if s > 0:
                    f, a, r = hh / ref - 1, 1 - ll / ref, cc / ref - 1
                    ext = np.nanmin(lo[ie - 288:ie])
                    d = 1 - ext / ref
                else:
                    f, a, r = 1 - ll / ref, hh / ref - 1, 1 - cc / ref
                    ext = np.nanmax(h[ie - 288:ie])
                    d = ext / ref - 1
                m = end - ie
                fav[k, :m], adv[k, :m], cls[k, :m] = f, a, r
                atr[k] = atr_pct(h, lo, c, ie, ref)
                struct[k] = d + 0.002 if np.isfinite(d) and d > 0 else np.nan
    last = np.where(np.isnan(cls), -1, np.arange(horizon)).max(axis=1)
    ok = (last >= 0) & np.isfinite(atr) & np.isfinite(struct)
    return {"fav": fav, "adv": adv, "cls": cls, "atr": atr, "struct": struct, "ok": ok, "last": last}


def run_presets(P: dict[str, Any], hold: int, fee: float, slip: float) -> dict[str, np.ndarray]:
    """Net return per trade for every preset (NaN where the path is unusable)."""
    sel = P["ok"]
    n = int(sel.sum())
    out = {}
    if n == 0:
        return {k: np.array([]) for k in PRESETS}
    for name, pr in PRESETS.items():
        prm = {k: np.array([INF]) for k in PARAMS}
        for k, v in pr["p"].items():
            prm[k] = np.array([float(v)])
        prm["time_bars"] = np.array([float(hold)])
        scale = P["struct"][sel] if pr["unit"] == "struct" else P["atr"][sel] * np.sqrt(hold)
        ret, _ = simulate(P, sel, prm, np.full(n, fee), slip, np.ones(n), scale)
        out[name] = ret[0]
    return out


def rule_events(cfg: Config) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    """Active rules with trader ids, and their discovery events (entry = next bar open)."""
    from okxrev import discover
    from okxrev.traders import rule_ids
    res = discover.load(cfg) or {}
    active = [r for r in res.get("results", []) if r["verdict"] in ("可用", "觀察中")]
    if not active:
        return [], pd.DataFrame()
    ids = rule_ids(cfg, active)
    keys = {r.key: i for i, r in enumerate(discover.enumerate_rules())}
    rules = [{**r, "trader_id": tid, "ri": keys.get(r["key"])} for r, tid in zip(active, ids) if r["key"] in keys]
    p = cfg.paths.results / "discover_events.parquet"
    import duckdb
    con = duckdb.connect()
    ev = con.execute(f"SELECT rule, ts, inst, exit FROM read_parquet('{p.as_posix()}') WHERE rule IN ({','.join(str(r['ri']) for r in rules)})").df()
    con.close()
    return rules, ev


def blind_entries(cfg: Config, trader_ids: list[str]) -> pd.DataFrame:
    from okxrev import replay
    p = replay.out_db_path(cfg)
    if not p.exists():
        return pd.DataFrame(columns=["trader_id", "inst_id", "open_ts", "side"])
    import duckdb
    con = duckdb.connect(str(p), read_only=True)
    try:
        q = f"SELECT trader_id, inst_id, entry_ts AS open_ts, side FROM positions WHERE book = 'C' AND entry_ts IS NOT NULL AND trader_id IN ({','.join('?' * len(trader_ids))})"
        return con.execute(q, trader_ids).df()
    finally:
        con.close()


def seg(x: np.ndarray) -> dict[str, Any]:
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return {"n": 0}
    return {"n": int(len(x)), "mean": float(x.mean()), "win": float(np.mean(x > 0)),
            "t": float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else None}


def evaluate(cfg: Config) -> dict[str, Any]:
    from okxrev import replay
    t0 = time.monotonic()
    fee = 2 * float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"])
    slip = float(cfg.metrics["slippage_per_side"])
    rules, ev = rule_events(cfg)
    if not rules:
        raise RuntimeError("沒有可用／觀察中的探索規則")
    blind = blind_entries(cfg, [r["trader_id"] for r in rules])
    oldp = replay.old_db_path(cfg)
    rng = np.random.default_rng(0)
    out_rules = []
    for r in rules:
        hold = HOLD.get(r["exit"], 72)
        d = ev[(ev["rule"] == r["ri"]) & (ev["exit"] == r["exit"])]
        if len(d) > MAX_EVENTS:
            d = d.iloc[np.sort(rng.choice(len(d), MAX_EVENTS, replace=False))]
        tr = pd.DataFrame({"inst_id": d["inst"].astype(str).to_numpy(), "open_ts": d["ts"].to_numpy(np.int64) + BAR, "side": r["side"]})
        cut = int(tr["open_ts"].min() + (tr["open_ts"].max() - tr["open_ts"].min()) * SPLIT) if len(tr) else 0
        P = paths_for(cfg.paths.db, tr, hold)
        res_m = run_presets(P, hold, fee, slip)
        ts_ok = tr["open_ts"].to_numpy(np.int64)[P["ok"]]
        segs: dict[str, dict[str, Any]] = {k: {} for k in PRESETS}
        for k, x in res_m.items():
            segs[k]["train"] = seg(x[ts_ok < cut])
            segs[k]["valid"] = seg(x[ts_ok >= cut])
        b = blind[blind["trader_id"] == r["trader_id"]]
        if len(b) and oldp.exists():
            Pb = paths_for(oldp, b.assign(side=r["side"]), hold)
            for k, x in run_presets(Pb, hold, fee, slip).items():
                segs[k]["blind"] = seg(x)
        base = segs["time"]
        improves = []
        for k in PRESETS:
            if k == "time":
                continue
            s = segs[k]
            ok = all((s.get(x) or {}).get("n", 0) >= 10 and (base.get(x) or {}).get("n", 0) >= 10 and s[x]["mean"] > base[x]["mean"] for x in ("train", "valid", "blind")) \
                and s["valid"]["mean"] > 0 and s["blind"]["mean"] > 0
            if ok:
                improves.append(k)
        best = max((k for k in improves if k in ENGINE_OK), key=lambda k: segs[k]["train"]["mean"], default=None)
        out_rules.append({"trader_id": r["trader_id"], "label": r["label"], "side": r["side"], "exit": r["exit"], "hold_bars": hold,
                          "presets": segs, "improves": improves, "best": best})
        log.info("exit_lab %s: base train %+.2f%% valid %+.2f%% blind %s; improves %s", r["trader_id"], 100 * (base["train"].get("mean") or 0),
                 100 * (base["valid"].get("mean") or 0), f"{100 * base['blind']['mean']:+.2f}%" if base.get("blind", {}).get("n") else "-", improves)
    summary = {}
    for k in PRESETS:
        if k == "time":
            continue
        diffs = {s: [x["presets"][k][s]["mean"] - x["presets"]["time"][s]["mean"] for x in out_rules
                     if (x["presets"][k].get(s) or {}).get("n") and (x["presets"]["time"].get(s) or {}).get("n")] for s in ("train", "valid", "blind")}
        summary[k] = {"label": PRESETS[k]["label"], "n_rules_improved": sum(k in x["improves"] for x in out_rules),
                      **{f"{s}_better": sum(v > 0 for v in diffs[s]) for s in diffs}, **{f"{s}_mean_diff": float(np.mean(diffs[s])) if diffs[s] else None for s in diffs},
                      "n_rules": len(out_rules)}
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1), "presets": {k: v["label"] for k, v in PRESETS.items()},
           "engine_presets": list(ENGINE_OK), "summary": summary, "rules": out_rules,
           "caveats": ["盤中順序假設不利（同一根 K 線先碰到不利的極端）；移動停利與保本只用前面 K 線的高點", "盲測用舊資料重播裡這條規則實際的進場點",
                       "分批止盈只做研究，紙上交易引擎沒有支援（不會變成交易員）"]}
    out = cfg.paths.results / "exit_lab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    return res


# ---------------------------------------------------------------- paper traders
from okxrev.paper import Signal, Trader  # noqa: E402


class ExitVariant(Trader):
    """Same entries as a rule trader, exit replaced by the preset that improved it in every segment (fixed target and
    stop in R units, R = signal-bar ATR x sqrt(holding bars); the rule's holding time stays the time limit)."""

    def __init__(self, inner: Trader, preset: str, hold: int):
        self.inner, self.preset, self.hold = inner, preset, hold
        self.kind = inner.kind
        self.exit = f"{inner.exit}_hx"
        self.trader_id = f"{inner.trader_id}_hx"
        self.label = f"{inner.label}＋人性化出場（{PRESETS[preset]['label']}）"

    def params(self) -> dict[str, Any]:
        return {**self.inner.params(), "exit": self.exit, "exit_preset": self.preset, "exit_variant_of": self.inner.trader_id}

    def prepare(self, ctx: Any) -> None:
        if self.inner.trader_id not in getattr(ctx, "computed", {}):
            self.inner.prepare(ctx)

    def _conv(self, ctx: Any, sigs: list[Signal]) -> list[Signal]:
        from dataclasses import replace as _replace
        p = PRESETS[self.preset]["p"]
        out = []
        for s in sigs:
            f = {**(s.features or ctx.snapshot(s.inst_id, s.bar_ts))}
            atr = f.get("atr")
            if atr is None or not np.isfinite(float(atr)) or float(atr) <= 0:
                continue
            r = float(atr) * np.sqrt(self.hold)
            f.update(_hold_bars=self.hold, _stop_frac=float(p["sl"]) * r, exit_preset=self.preset, R=r)
            if "tp" in p:
                f["_tp_frac"] = float(p["tp"]) * r
            out.append(_replace(s, trader_id=self.trader_id, exit_rule=self.exit, features=f))
        return out

    def signals_batch(self, ctx: Any, items: list[tuple[str, pd.DataFrame, np.ndarray]]) -> list[Signal]:
        done = getattr(ctx, "computed", {}).get(self.inner.trader_id)
        return self._conv(ctx, done if done is not None else self.inner.signals_batch(ctx, items))


def traders(cfg: Config, rule_traders: list[Trader], kinds: dict[str, bool]) -> list[ExitVariant]:
    """Variants for the rules whose best preset improved them (and that the paper engine supports: fixed target / stop)."""
    if not kinds.get("human_exit", True):
        return []
    res = load(cfg) or {}
    by_id = {t.trader_id: t for t in rule_traders}
    out = []
    for r in res.get("rules", []):
        b = r.get("best")
        if b and b != "time" and r["trader_id"] in by_id and PRESETS.get(b, {}).get("unit") == "r" and set(PRESETS[b]["p"]) <= {"tp", "sl"}:
            out.append(ExitVariant(by_id[r["trader_id"]], b, int(r["hold_bars"])))
    return out


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "exit_lab.json"
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null")) if p.exists() else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.exit_lab")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "exit_lab")
    res = evaluate(cfg) if args.evaluate else load(cfg)
    if not res:
        print("no results yet: python -m okxrev.exit_lab --evaluate")
        return 0
    for k, s in res["summary"].items():
        f = lambda x: "-" if s.get(f"{x}_mean_diff") is None else f"{s[f'{x}_mean_diff'] * 100:+.2f}% ({s[f'{x}_better']}/{s['n_rules']})"
        print(f"{k:10s} improves {s['n_rules_improved']:2d}/{s['n_rules']}  vs time exit: train {f('train')} valid {f('valid')} blind {f('blind')}  {s['label']}")
    for r in res["rules"]:
        if r["improves"]:
            b = r["presets"][r["best"]] if r["best"] else None
            t = r["presets"]["time"]
            print(f"  {r['trader_id']:18s} improves={r['improves']} best={r['best']}  time {t['valid'].get('mean', 0) * 100:+.2f}%/{t['blind'].get('mean', 0) * 100:+.2f}%"
                  + (f"  best {b['valid']['mean'] * 100:+.2f}%/{b['blind']['mean'] * 100:+.2f}% (valid/blind)" if b else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
