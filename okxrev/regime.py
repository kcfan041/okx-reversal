"""Market direction streaks: do the alt market / BTC trend for a fixed number of days, and what follows a run of
down (or up) days?

Alt index = each UTC day, the median daily log return of the USDT swaps listed >= 30 days (BTC/ETH excluded).
Studied separately on the old database (2024-09 .. 2025-09) and the live one (2025-10 ..), plus BTC daily candles
from 2020 fetched from OKX.

  runs          up / down streak lengths vs the same days in random order (shuffled signs)
  continuation  P(same sign tomorrow | k days in a row), next 1 / 3 / 5 day returns
  events        the day a streak first reaches k days (one event per streak, no overlap): next h-day return of the
                index and of a basket of the N most traded alts bought at that day's close (costs included),
                compared with all days and with shuffled day orders
  rules         the discovered rules' events split by the market streak at signal time

Paper traders (kind "regime"): `mkt_rebound_k4` / `_k3` buy the 20 most traded alts (listed >= 30 days) at the close
of the day the alt index has fallen k days in a row (once per streak), hold 3 days, 20% stop. They compute the index
from the candles they are given, so live ticks and the blind replay use the same code.
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

from okxrev import db
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging
from okxrev.paper import Signal, Trader

log = logging.getLogger("okxrev.regime")

D = 86_400_000
BAR = db.BAR_MS
MIN_AGE = 30
EXCL = ("BTC", "ETH", "USDC")
HOLDS = (1, 3, 5)
KS = (2, 3, 4, 5)
TOP_N = (20, 50)


# ---------------------------------------------------------------- data
def daily_panel(path: Any, since: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(close, quote volume) per UTC day x swap; the last (unfinished) day is dropped."""
    with db.open_db(path, read_only=True, wait_sec=2.0, max_tries=60) as con:
        df = con.execute(f"""
            SELECT inst_id, (ts // {D}) * {D} AS d, arg_max(close, ts) AS c, SUM(vol_quote) AS vq, MAX(ts) AS last
            FROM candles WHERE inst_id LIKE '%-USDT-SWAP' {"AND ts >= ?" if since else ""} GROUP BY 1, 2""", [since] if since else []).df()
        try:
            lt = dict(con.execute("SELECT inst_id, list_time FROM instruments WHERE list_time IS NOT NULL").fetchall())
        except Exception:
            lt = {}
    df = df[df["last"] == df["d"] + D - BAR]
    C = df.pivot(index="d", columns="inst_id", values="c").sort_index()
    V = df.pivot(index="d", columns="inst_id", values="vq").reindex(C.index)
    full = np.arange(int(C.index.min()), int(C.index.max()) + D, D)
    C, V = C.reindex(full), V.reindex(full)
    C.attrs["list_time"] = {k: int(v) for k, v in lt.items() if k in C.columns}
    return C, V


def ages_of(C: pd.DataFrame) -> pd.DataFrame:
    """Days since listing (instrument list time when known, else the first day with data)."""
    lt = C.attrs.get("list_time") or {}
    first = C.apply(lambda s: s.first_valid_index())
    return pd.DataFrame({k: (C.index - min(lt.get(k, first[k]) or first[k], first[k])) / D for k in C.columns}, index=C.index)


def alt_index(C: pd.DataFrame, ages: pd.DataFrame | None = None, min_n: int = 30) -> pd.DataFrame:
    lr = np.log(C).diff()
    if ages is None:
        ages = ages_of(C)
    keep = [k for k in C.columns if k.split("-")[0] not in EXCL]
    m = lr[keep].where(ages[keep] >= MIN_AGE)
    out = pd.DataFrame({"ret": m.median(axis=1), "n": m.notna().sum(axis=1)})
    out.loc[out["n"] < min_n, "ret"] = np.nan
    return out


def streaks(ret: pd.Series) -> pd.DataFrame:
    """Consecutive down / up days ending at each day (0 when the day has the other sign or no data)."""
    dn = np.zeros(len(ret), dtype=int)
    up = np.zeros(len(ret), dtype=int)
    x = ret.to_numpy(float)
    for i, v in enumerate(x):
        if np.isfinite(v):
            dn[i] = (dn[i - 1] + 1 if i else 1) if v < 0 else 0
            up[i] = (up[i - 1] + 1 if i else 1) if v > 0 else 0
    return pd.DataFrame({"dn": dn, "up": up}, index=ret.index)


def btc_daily(cfg: Config) -> pd.Series:
    from okxrev.okx_client import OkxClient
    frames, after = [], None
    with OkxClient(cfg.okx) as c:
        for _ in range(40):
            df, n = c.history_candles(cfg.universe["btc_inst"], bar="1Dutc", limit=100, after=after)
            if not n:
                break
            frames.append(df)
            after = int(df["ts"].min())
    if not frames:
        return pd.Series(dtype=float)
    b = pd.concat(frames).drop_duplicates("ts").sort_values("ts").set_index("ts")["close"]
    return np.log(b).diff().dropna()


# ---------------------------------------------------------------- statistics
def _runs(sign: np.ndarray) -> list[tuple[int, int]]:
    out, cur, n = [], int(sign[0]), 1
    for v in sign[1:]:
        if v == cur:
            n += 1
        else:
            out.append((cur, n))
            cur, n = int(v), 1
    out.append((cur, n))
    return out


def run_stats(r: pd.Series, sims: int = 1000, seed: int = 0) -> dict[str, Any]:
    x = r.dropna().to_numpy()
    s = (x > 0).astype(int)
    rr = _runs(s)
    up = np.array([n for v, n in rr if v == 1])
    dn = np.array([n for v, n in rr if v == 0])
    rng = np.random.default_rng(seed)
    sim = []
    for _ in range(sims):
        q = _runs(rng.permutation(s))
        sim.append((len(q), np.mean([n for v, n in q if v == 1]), np.mean([n for v, n in q if v == 0]),
                    max(n for v, n in q if v == 1), max(n for v, n in q if v == 0)))
    sim = np.array(sim)
    p = float(np.mean(np.abs(sim[:, 0] - sim[:, 0].mean()) >= abs(len(rr) - sim[:, 0].mean())))
    hist = {lab: [int((a == k).sum()) for k in range(1, 9)] + [int((a > 8).sum())] for lab, a in (("up", up), ("down", dn))}
    lags = [1, 2, 3, 5, 7]
    ac = [float(np.corrcoef(x[:-k], x[k:])[0, 1]) for k in lags]

    def vr(q: int) -> float:
        return float(pd.Series(x).rolling(q).sum().dropna().var(ddof=1) / (q * x.var(ddof=1)))

    return {"days": int(len(x)), "up_share": float(s.mean()), "runs": len(rr), "runs_random": float(sim[:, 0].mean()), "runs_p": p,
            "up_mean": float(up.mean()), "up_mean_random": float(sim[:, 1].mean()), "up_max": int(up.max()), "up_max_random95": float(np.percentile(sim[:, 3], 95)),
            "down_mean": float(dn.mean()), "down_mean_random": float(sim[:, 2].mean()), "down_max": int(dn.max()),
            "down_max_random95": float(np.percentile(sim[:, 4], 95)), "hist": hist, "autocorr": dict(zip(map(str, lags), ac)),
            "autocorr_noise95": float(2 / np.sqrt(len(x))), "vr": {str(q): vr(q) for q in (5, 10, 20)}}


def continuation(r: pd.Series, kmax: int = 6) -> dict[str, list[dict[str, Any]]]:
    x = r.dropna().to_numpy()
    s = x > 0
    base = float(s.mean())
    st = streaks(pd.Series(x))
    out: dict[str, list[dict[str, Any]]] = {"up": [], "down": []}
    for side, col in (("up", "up"), ("down", "dn")):
        c = st[col].to_numpy()
        pb = base if side == "up" else 1 - base
        for k in range(1, kmax + 1):
            idx = np.flatnonzero(c[:-1] >= k)
            if len(idx) < 8:
                break
            nxt = s[idx + 1] if side == "up" else ~s[idx + 1]
            n = len(idx)
            out[side].append({"k": k, "n": n, "p_same": float(nxt.mean()), "p_base": pb, "z": float((nxt.mean() - pb) / np.sqrt(pb * (1 - pb) / n)),
                              "next1": float(x[idx + 1].mean()),
                              "next3": float(np.mean([x[i + 1:i + 4].sum() for i in idx if i + 4 <= len(x)]))})
    return out


def first_hits(st: pd.DataFrame, col: str, k: int) -> np.ndarray:
    """Positions of the days a streak first reaches k (one per streak)."""
    c = st[col].to_numpy()
    return np.flatnonzero(c == k)


def event_test(r: pd.Series, k: int, hold: int, col: str = "dn", sims: int = 2000, seed: int = 0) -> dict[str, Any]:
    """Next `hold`-day return after the day a down (up) streak first reaches k, vs all days and vs shuffled orders."""
    x = r.to_numpy(float)
    ok = np.isfinite(x)
    st = streaks(r)
    idx = [i for i in first_hits(st, col, k) if i + hold < len(x) and ok[i + 1:i + 1 + hold].all()]
    if len(idx) < 3:
        return {"n": len(idx)}
    fwd = np.array([x[i + 1:i + 1 + hold].sum() for i in idx])
    allf = np.array([x[i + 1:i + 1 + hold].sum() for i in range(len(x) - hold) if ok[i + 1:i + 1 + hold].all()])
    rng = np.random.default_rng(seed)
    xs = x[ok]
    sim = []
    for _ in range(sims):
        y = pd.Series(rng.permutation(xs))
        ii = [i for i in first_hits(streaks(y), col, k) if i + hold < len(y)]
        if ii:
            yy = y.to_numpy()
            sim.append(np.mean([yy[i + 1:i + 1 + hold].sum() for i in ii]))
    sim = np.array(sim)
    a = float(fwd.mean())
    p = float(np.mean(sim >= a)) if col == "dn" else float(np.mean(sim <= a))
    return {"n": len(idx), "mean": a, "median": float(np.median(fwd)), "pos": float(np.mean(fwd > 0)), "all_days": float(allf.mean()), "p": p,
            "dates": [ms_to_str(int(r.index[i]), "%Y-%m-%d") for i in idx]}


def basket_test(C: pd.DataFrame, V: pd.DataFrame, idx_df: pd.DataFrame, k: int, hold: int, top_n: int, cost: float,
                col: str = "dn") -> dict[str, Any]:
    """Buy (for a down streak) the top_n alts by 7-day turnover, listed >= 30 days, at the close of the day the streak
    first reaches k; equal weight, hold `hold` days, net of round-trip cost. Baseline: the same basket every day."""
    st = streaks(idx_df["ret"])
    lr = np.log(C).diff()
    ages = ages_of(C)
    keep = [c for c in C.columns if c.split("-")[0] not in EXCL]
    to7 = V[keep].rolling(7, min_periods=5).sum()
    sgn = 1.0 if col == "dn" else -1.0

    def basket(i: int) -> float | None:
        if i + hold >= len(C):
            return None
        age = ages[keep].iloc[i]
        cand = to7.iloc[i][age >= MIN_AGE].dropna().sort_values(ascending=False).index[:top_n]
        fw = lr[cand].iloc[i + 1:i + 1 + hold].sum(min_count=hold)
        fw = fw.dropna()
        if len(fw) < max(5, top_n // 2):
            return None
        return float(sgn * np.expm1(fw).mean() - cost)

    ev = [v for v in (basket(i) for i in first_hits(st, col, k)) if v is not None]
    allv = [v for v in (basket(i) for i in range(30, len(C) - hold, 1)) if v is not None]
    if len(ev) < 3:
        return {"n": len(ev)}
    e, a = np.array(ev), np.array(allv)
    se = np.sqrt(e.var(ddof=1) / len(e) + a.var(ddof=1) / max(len(a), 1))
    return {"n": len(e), "mean": float(e.mean()), "median": float(np.median(e)), "win": float(np.mean(e > 0)), "all_days": float(a.mean()),
            "t_vs_all": float((e.mean() - a.mean()) / se) if se > 0 else None}


def rules_by_market(cfg: Config, st: pd.DataFrame) -> list[dict[str, Any]]:
    """Active discovered rules: per-trade net return split by the alt-index streak known at signal time."""
    from okxrev import discover
    res = discover.load(cfg) or {}
    p = cfg.paths.results / "discover_events.parquet"
    if not res or not p.exists():
        return []
    active = [r for r in res.get("results", []) if r["verdict"] in ("可用", "觀察中")]
    keys = [r.key for r in discover.enumerate_rules()]
    ids = {k: i for i, k in enumerate(keys)}
    want = {ids[r["key"]]: r for r in active if r["key"] in ids}
    if not want:
        return []
    import duckdb
    con = duckdb.connect()
    ev = con.execute(f"SELECT rule, ts, inst, exit, ret FROM read_parquet('{p.as_posix()}') WHERE rule IN ({','.join(map(str, want))})").df()
    con.close()
    day = ev["ts"] // D * D - D                         # last finished day before the signal
    sd = st.reindex(day.to_numpy())
    ev["dn"], ev["up"] = sd["dn"].to_numpy(), sd["up"].to_numpy()
    out = []
    for ri, r in want.items():
        d = ev[(ev["rule"] == ri) & (ev["exit"] == r["exit"])].dropna(subset=["dn"])
        if len(d) < 20:
            continue
        row = {"label": r["label"], "side": r["side"], "exit": r["exit"], "n": int(len(d)), "all": float(d["ret"].mean())}
        for name, m in (("dn3", d["dn"] >= 3), ("up3", d["up"] >= 3), ("other", (d["dn"] < 3) & (d["up"] < 3))):
            x = d.loc[m, "ret"]
            row[name] = {"n": int(len(x)), "mean": float(x.mean()) if len(x) else None, "win": float((x > 0).mean()) if len(x) else None}
        out.append(row)
    return out


def evaluate(cfg: Config, sims: int = 1000) -> dict[str, Any]:
    from okxrev import replay
    t0 = time.monotonic()
    cost = 2 * (float(cfg.metrics["fee_per_side"]) + float(cfg.metrics["slippage_per_side"]))
    periods: dict[str, Any] = {}
    main_st = None
    for name, path in (("old", replay.old_db_path(cfg)), ("main", cfg.paths.db)):
        if not path.exists():
            continue
        C, V = daily_panel(path)
        ix = alt_index(C)
        r = ix["ret"].dropna()
        if len(r) < 60:
            continue
        lo, hi = ms_to_str(int(r.index.min()), "%Y-%m-%d"), ms_to_str(int(r.index.max()), "%Y-%m-%d")
        log.info("regime: %s alt index %s .. %s (%d days)", name, lo, hi, len(r))
        ev = {f"{col}{k}_h{h}": event_test(ix["ret"], k, h, col, sims) for col in ("dn", "up") for k in KS for h in HOLDS}
        bk = {f"{col}{k}_h{h}_n{n}": basket_test(C, V, ix, k, h, n, cost, col) for col in ("dn", "up") for k in (3, 4) for h in HOLDS for n in TOP_N}
        periods[name] = {"range": [lo, hi], "runs": run_stats(r, sims), "continuation": continuation(r), "events": ev, "basket": bk}
        if name == "main":
            main_st = streaks(ix["ret"])
    btc = btc_daily(cfg)
    if len(btc):
        periods["btc"] = {"range": [ms_to_str(int(btc.index.min()), "%Y-%m-%d"), ms_to_str(int(btc.index.max()), "%Y-%m-%d")],
                          "runs": run_stats(btc, sims), "continuation": continuation(btc),
                          "events": {f"dn{k}_h{h}": event_test(btc, k, h, "dn", sims) for k in KS for h in HOLDS}}
        recent = btc[btc.index >= pd.Timestamp("2024-10-01", tz="UTC").value // 10**6]
        periods["btc_recent"] = {"range": [ms_to_str(int(recent.index.min()), "%Y-%m-%d"), ms_to_str(int(recent.index.max()), "%Y-%m-%d")],
                                 "events": {f"dn{k}_h{h}": event_test(recent, k, h, "dn", sims) for k in KS for h in HOLDS}}
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": 0.0, "periods": periods,
           "rules": rules_by_market(cfg, main_st) if main_st is not None else [],
           "traders": [{"id": t["id"], "label": t["label"]} for t in TRADER_SETS],
           "caveats": ["這個規律是同時看兩段期間找到的，所以沒有真正乾淨的盲測；交易員只能往後觀察",
                       "大盤指數是山寨幣中位數，實際交易用成交額前 N 名的一籃子，已扣手續費與滑價",
                       "舊資料只有現存的約 190 個老合約（倖存者偏差）",
                       "同時看了多個天數與持有時間（多重比較）"]}
    res["elapsed_sec"] = round(time.monotonic() - t0, 1)
    out = cfg.paths.results / "regime.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, default=_jd), encoding="utf-8")
    log.info("regime: done in %.0fs", res["elapsed_sec"])
    return res


def _jd(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    return str(o)


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "regime.json"
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null")) if p.exists() else None
    except ValueError:
        return None


def current(cfg: Config) -> dict[str, Any]:
    """Today's alt-index streak from the live database (last finished days)."""
    try:
        C, _ = daily_panel(cfg.paths.db, db.now_ms() - 60 * D)
    except (db.DbBusy, FileNotFoundError):
        return {}
    ix = alt_index(C, min_n=20)
    st = streaks(ix["ret"])
    if not len(st):
        return {}
    last = st.index[-1]
    return {"day": ms_to_str(int(last), "%Y-%m-%d"), "down_days": int(st["dn"].iloc[-1]), "up_days": int(st["up"].iloc[-1]),
            "last_ret": None if pd.isna(ix["ret"].iloc[-1]) else float(ix["ret"].iloc[-1]),
            "recent": [{"day": ms_to_str(int(d), "%Y-%m-%d"), "ret": None if pd.isna(v) else float(v)} for d, v in ix["ret"].tail(10).items()]}


# ---------------------------------------------------------------- paper traders
TRADER_SETS = [
    {"id": "mkt_rebound_k4", "k": 4, "top_n": 20, "label": "大盤連跌 4 天後做多成交額前 20 名山寨幣（持有 3 天，止損 20%）"},
    {"id": "mkt_rebound_k3", "k": 3, "top_n": 20, "label": "大盤連跌 3 天後做多成交額前 20 名山寨幣（持有 3 天，止損 20%）"},
]
EXIT = "3d_sl20pct"


def index_from_candles(candles: dict[str, pd.DataFrame], feats: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, dict[int, pd.Series]]:
    """Alt-index streaks per finished UTC day, and each day's ranking of alts by the last 24h turnover at the day's
    last bar, from the candles and features a tick (or the replay) has."""
    closes, ages, turn = {}, {}, {}
    for inst, g in candles.items():
        if inst.split("-")[0] in EXCL or not len(g):
            continue
        idx = g.index.to_numpy(np.int64)
        m = idx % D == D - BAR
        if not m.any():
            continue
        days = idx[m] - (D - BAR)
        closes[inst] = pd.Series(g["close"].to_numpy(float)[m], index=days)
        f = feats.get(inst)
        if f is not None and "age_d" in f:
            a = f["age_d"].reindex(idx[m]).to_numpy(float)
            ages[inst] = pd.Series(np.where(np.isfinite(a), a, 1e9), index=days)
            if "turnover24" in f:
                turn[inst] = pd.Series(f["turnover24"].reindex(idx[m]).to_numpy(float), index=days)
    if not closes:
        return pd.DataFrame(columns=["dn", "up"]), {}
    C = pd.DataFrame(closes).sort_index()
    full = np.arange(int(C.index.min()), int(C.index.max()) + D, D)
    C = C.reindex(full)
    A = pd.DataFrame(ages).reindex(index=full, columns=C.columns).fillna(1e9)
    ix = alt_index(C, A, min_n=20)
    T = pd.DataFrame(turn).reindex(index=full, columns=C.columns)
    rank = {int(d): T.loc[d][A.loc[d] >= MIN_AGE].dropna().sort_values(ascending=False) for d in full}
    return streaks(ix["ret"]), rank


class RegimeTrader(Trader):
    kind = "regime"

    def __init__(self, ts: dict[str, Any]):
        self.ts = ts
        self.trader_id = ts["id"]
        self.label = ts["label"]
        self.exit = EXIT
        self.st = pd.DataFrame(columns=["dn", "up"])
        self.rank: dict[int, pd.Series] = {}
        self._key: Any = None

    def params(self) -> dict[str, Any]:
        return {"down_days": self.ts["k"], "top_n": self.ts["top_n"], "exit": EXIT, "min_age_d": MIN_AGE}

    def prepare(self, ctx: Any) -> None:
        key = (id(ctx.candles), len(ctx.candles))
        if key != self._key:
            self.st, self.rank = index_from_candles(ctx.candles, ctx.feats)
            self._key = key

    def signals_batch(self, ctx: Any, items: list[tuple[str, pd.DataFrame, np.ndarray]]) -> list[Signal]:
        k = self.ts["k"]
        hit = set(int(d) for d in self.st.index[self.st["dn"].to_numpy() == k]) if len(self.st) else set()
        if not hit:
            return []
        targets = np.array(sorted(d + D - BAR for d in hit), dtype=np.int64)
        tops = {d: set(self.rank[d].index[: self.ts["top_n"]]) for d in hit if d in self.rank}
        out: list[Signal] = []
        for inst, f, bars in items:
            if inst.split("-")[0] in EXCL:
                continue
            b = np.asarray(bars, dtype=np.int64)
            for bb in b[np.isin(b, targets)]:
                day = int(bb) - (D - BAR)
                if inst not in tops.get(day, ()):
                    continue
                why = f"大盤（山寨幣中位數）連跌 {k} 天（到 {ms_to_str(day, '%Y-%m-%d')}），做多成交額前 {self.ts['top_n']} 名"
                out.append(Signal(self.trader_id, inst, int(bb), "long", EXIT, float(k), why,
                                  features={**ctx.snapshot(inst, int(bb)), "mkt_down_days": k}))
        return out


def traders(kinds: dict[str, bool]) -> list[RegimeTrader]:
    return [RegimeTrader(t) for t in TRADER_SETS] if kinds.get("regime", True) else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.regime")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--sims", type=int, default=1000)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "regime")
    if args.evaluate:
        res = evaluate(cfg, args.sims)
    else:
        res = load(cfg)
        if not res:
            print("no results yet: python -m okxrev.regime --evaluate")
            return 0
    pc = lambda v: "-" if v is None else f"{np.expm1(v) * 100:+.2f}%"
    for name, p in res["periods"].items():
        print(f"\n== {name} {p['range']}")
        if "runs" in p:
            r = p["runs"]
            print(f"  runs {r['runs']} (random {r['runs_random']:.0f}, p={r['runs_p']:.3f}); up mean {r['up_mean']:.2f}d (random {r['up_mean_random']:.2f}), "
                  f"down mean {r['down_mean']:.2f}d (random {r['down_mean_random']:.2f})")
        for key in ("dn3_h3", "dn4_h3", "up3_h3"):
            e = p["events"].get(key) or {}
            if e.get("n"):
                print(f"  {key}: n={e['n']} next {pc(e['mean'])} (all days {pc(e['all_days'])}) pos {e['pos']:.0%} p={e['p']:.3f}")
        for key in ("dn3_h3_n20", "dn4_h3_n20"):
            b = (p.get("basket") or {}).get(key) or {}
            if b.get("n"):
                print(f"  basket {key}: n={b['n']} net {b['mean'] * 100:+.2f}% (all days {b['all_days'] * 100:+.2f}%) win {b['win']:.0%} t={b['t_vs_all']:.2f}")
    print(f"\ncurrent: {current(cfg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
