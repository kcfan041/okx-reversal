"""Does holding high Simple Earn rate coins pay? Three tests on the hourly lending rates from okxrev.earn.

Prices are daily closes of the USDT swaps (old candle DB before the main DB starts). Earn per day = the day's mean
"lendingRate" (what lenders actually got) / 365, credited in coins, so it compounds with the price.

1. Basket: every STEP_D days take the N coins with the highest mean rate over the previous 7 days (age >= 30 days,
   7-day median quote volume >= MIN_VQ), equal weight, hold to the next re-pick with earn. Main setting fixed in
   advance: N = 10. Compared with the same basket without earn, the lowest-rate N, every eligible coin, random N
   coins (SIMS runs) and BTC. Two exits on top of the main basket: sell a coin at the day's close when its rate spikes
   (SPIKE_APY and SPIKE_X times its previous 30-day median), or when OKX announces a campaign for it (okxrev.announce);
   the money stays in cash until the next re-pick. The spike exit is compared with selling random held coin-days at the
   same rate (SIMS runs), since going to cash early alone helps in a falling market.
2. Rate spikes as events: the coin's return before and after (1/7/30/90 days), minus the alt index (median daily log
   return of all coins), against random days of the same coins (placebo).
3. Cross-section: every 7 days, quintiles of the 7-day mean rate; next 7 / 30 days return minus the median coin.
4. The reverse: short the main basket (1x collateral, liquidated when a coin doubles) without carry, paying the lending
   rate (a floor for a margin short's borrow cost), half long every eligible coin / half short, random short N coins
   (SIMS runs); swap funding only from when its history starts (OKX keeps ~3 months).
"""
from __future__ import annotations

import json
import logging
import time
import warnings
from typing import Any

import numpy as np
import pandas as pd

from okxrev import db, earn, regime
from okxrev.config import Config, ms_to_str

log = logging.getLogger("okxrev.earn_study")

D = 86_400_000
STEP_D = 30
NS = (5, 10, 20)
MAIN_N = 10
MIN_AGE_D = 30
MIN_VQ = 1_000_000
SPIKE_APY = 1.0
SPIKE_X = 3.0
COOLDOWN_D = 30
HORIZONS = (1, 7, 30, 90)
SIMS = 200
PLACEBO = 200


# ---------------------------------------------------------------- data
def panels(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(close, quote volume, earn rate) per UTC day x earn coin on one day grid, plus the close of every swap."""
    from okxrev import replay
    Cm, Vm = regime.daily_panel(cfg.paths.db)
    old = replay.old_db_path(cfg)
    if old.exists():
        Co, Vo = regime.daily_panel(old)
        C, V = Cm.combine_first(Co), Vm.combine_first(Vo)
    else:
        C, V = Cm, Vm
    with db.open_db(earn.db_path(cfg), read_only=True, wait_sec=1.0, max_tries=180) as con:     # the back-fill holds it ~1 min per coin
        a = con.execute(f"SELECT ccy, (ts // {D}) * {D} AS d, avg(lending_rate) AS r FROM lending_rate GROUP BY 1, 2").df()
    a["inst"] = a["ccy"] + "-USDT-SWAP"
    A = a[a["inst"].isin(C.columns)].pivot(index="d", columns="inst", values="r")
    days = np.arange(int(max(C.index.min(), A.index.min())), int(C.index.max()) + D, D)
    cols = sorted(A.columns)
    return C.reindex(index=days, columns=cols), V.reindex(index=days, columns=cols), A.reindex(index=days, columns=cols), C


def funding_panel(cfg: Config, C: pd.DataFrame) -> pd.DataFrame:
    """Swap funding summed per UTC day x coin (> 0: longs paid shorts); NaN before OKX's ~3 months of history."""
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=1.0, max_tries=60) as con:
        f = con.execute(f"SELECT inst_id, (ts // {D}) * {D} AS d, sum(coalesce(realized, rate)) AS f FROM funding GROUP BY 1, 2").df()
    if f.empty:
        return pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    return f.pivot(index="d", columns="inst_id", values="f").reindex(index=C.index, columns=C.columns)


def alt_index(C: pd.DataFrame, full: pd.DataFrame) -> np.ndarray:
    """Median daily log return over all swaps (not only the earn coins), aligned to C's days."""
    lr = np.log(full).diff()
    keep = [k for k in full.columns if k.split("-")[0] not in regime.EXCL]
    return lr[keep].median(axis=1).reindex(C.index).fillna(0.0).to_numpy(float)


def campaign_days(cfg: Config, C: pd.DataFrame) -> dict[str, set[int]]:
    from okxrev import announce
    df = announce.load_campaigns(cfg)
    if df.empty:
        return {}
    out: dict[str, set[int]] = {}
    for r in announce.campaign_tokens(df).itertuples(index=False):
        if r.inst_id in C.columns and r.p_time:
            out.setdefault(r.inst_id, set()).add(int(r.p_time) // D * D)
    return out


# ---------------------------------------------------------------- helpers
def spikes(A: np.ndarray) -> np.ndarray:
    """Boolean days x coins: rate >= SPIKE_APY and >= SPIKE_X x the previous 30-day median (>= 20 days of data)."""
    med = pd.DataFrame(A).rolling(30, min_periods=20).median().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        return (A >= SPIKE_APY) & (A >= SPIKE_X * med)


def eligible(C: np.ndarray, V: np.ndarray, A: np.ndarray, i: int) -> tuple[np.ndarray, np.ndarray]:
    """Coins usable at the close of day i-1 and their mean rate over days i-7..i-1."""
    if i < MIN_AGE_D + 1:
        return np.array([], dtype=int), np.array([])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)      # all-NaN columns
        rate = np.nanmean(A[i - 7:i], axis=0)
        vq = np.nanmedian(V[i - 7:i], axis=0)
    ok = np.isfinite(C[i - 1]) & np.isfinite(C[i - 1 - MIN_AGE_D]) & np.isfinite(rate) & (np.nan_to_num(vq) >= MIN_VQ)
    idx = np.flatnonzero(ok)
    return idx, rate[idx]


def basket(C: np.ndarray, A: np.ndarray, reb: list[int], members: list[np.ndarray], earn_on: bool = True,
           exit_mask: np.ndarray | None = None, end: int | None = None) -> dict[str, Any]:
    """Daily value of an equal-weight basket re-picked at rows `reb` (held from the close of row-1)."""
    end = end or len(C)
    value, vals, rets, n_exit = 1.0, [], [], 0
    earn_sum, earn_n = 0.0, 0
    for k, i in enumerate(reb):
        j_end = reb[k + 1] if k + 1 < len(reb) else end
        mem = members[k]
        start_val = value
        if not len(mem):
            vals.extend([value] * (j_end - i))
            rets.append(0.0)
            continue
        v = np.full(len(mem), value / len(mem))
        live = np.ones(len(mem), bool)
        last = C[i - 1, mem].copy()
        for j in range(i, j_end):
            px = C[j, mem]
            ok = live & np.isfinite(px) & np.isfinite(last)
            g = np.where(ok, px / np.where(ok, last, 1.0), 1.0)       # missing price: value frozen (delisted coins stay at the last close)
            if earn_on:
                r = np.where(live, np.nan_to_num(A[j, mem]), 0.0)
                g = g * (1 + r / 365)
                earn_sum, earn_n = earn_sum + float(r[live].sum()), earn_n + int(live.sum())
            v = v * g
            last = np.where(np.isfinite(px), px, last)
            if exit_mask is not None:
                hit = live & exit_mask[j, mem]
                n_exit += int(hit.sum())
                live &= ~hit
            vals.append(float(v.sum()))
        value = float(v.sum())
        rets.append(value / start_val - 1)
    ev = np.array(vals)
    peak = np.maximum.accumulate(ev)
    out = {"ret": value - 1, "max_dd": float((1 - ev / peak).max()) if len(ev) else 0.0, "periods": len(reb),
           "pos_periods": float(np.mean(np.array(rets) > 0)) if rets else None, "worst_period": float(min(rets)) if rets else None,
           "avg_rate": earn_sum / earn_n if earn_n else None, "exits": n_exit, "vals": ev}
    if len(ev) > 2:
        mid = len(ev) // 2
        out["halves"] = [float(ev[mid] - 1), float(ev[-1] / ev[mid] - 1)]
    return out


def short_basket(C: np.ndarray, reb: list[int], members: list[np.ndarray], carry: np.ndarray | None = None) -> dict[str, Any]:
    """Equal-weight short re-opened at rows `reb` with 1x collateral: a coin's slot is w * (2 - p / p0) plus the carry it
    received (carry[j] = fraction of the notional w * p / p0 per day, negative = paid); at zero it is liquidated and stays 0."""
    value, vals, rets = 1.0, [], []
    for k, i in enumerate(reb):
        j_end = reb[k + 1] if k + 1 < len(reb) else len(C)
        mem = members[k]
        start_val = value
        if not len(mem):
            vals.extend([value] * (j_end - i))
            rets.append(0.0)
            continue
        w = value / len(mem)
        p0 = C[i - 1, mem].copy()
        last, acc = p0.copy(), np.zeros(len(mem))
        live = np.ones(len(mem), bool)
        for j in range(i, j_end):
            last = np.where(np.isfinite(C[j, mem]), C[j, mem], last)      # missing price: frozen at the last close
            rel = last / p0
            if carry is not None:
                acc = acc + np.where(live, rel * np.nan_to_num(carry[j, mem]), 0.0)
            slot = np.where(live, 2 - rel + acc, 0.0)
            live &= slot > 0
            v = w * np.maximum(slot, 0.0)
            vals.append(float(v.sum()))
        value = vals[-1]
        rets.append(value / start_val - 1)
    ev = np.array(vals)
    peak = np.maximum.accumulate(ev)
    return {"ret": value - 1, "max_dd": float((1 - ev / peak).max()) if len(ev) else 0.0, "periods": len(reb),
            "pos_periods": float(np.mean(np.array(rets) > 0)) if rets else None, "worst_period": float(min(rets)) if rets else None,
            "vals": ev, **({"halves": [float(ev[len(ev) // 2] - 1), float(ev[-1] / ev[len(ev) // 2] - 1)]} if len(ev) > 2 else {})}


def hedge(reb: list[int], long_vals: np.ndarray, short_vals: np.ndarray) -> dict[str, Any]:
    """Half the money in each leg, re-split at every re-pick."""
    s0 = reb[0]
    out, h = [], 1.0
    for k, i in enumerate(reb):
        a, b = i - s0, (reb[k + 1] if k + 1 < len(reb) else s0 + len(long_vals)) - s0
        l0 = long_vals[a - 1] if a else 1.0
        x0 = short_vals[a - 1] if a else 1.0
        seg = h * (0.5 * long_vals[a:b] / l0 + 0.5 * short_vals[a:b] / x0)
        out.extend(seg.tolist())
        h = float(seg[-1]) if len(seg) else h
    ev = np.array(out)
    peak = np.maximum.accumulate(ev)
    return {"ret": h - 1, "max_dd": float((1 - ev / peak).max()) if len(ev) else 0.0, "vals": ev,
            **({"halves": [float(ev[len(ev) // 2] - 1), float(ev[-1] / ev[len(ev) // 2] - 1)]} if len(ev) > 2 else {})}


def curve(days: np.ndarray, vals: np.ndarray) -> dict[str, list[Any]]:
    step = max(1, len(vals) // 400)
    return {"t": [int(t) // 1000 for t in days[::step]], "v": [round(float(v), 4) for v in vals[::step]]}


def stats(a: np.ndarray) -> dict[str, Any]:
    a = a[np.isfinite(a)]
    if not len(a):
        return {"n": 0}
    return {"n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)), "win": float((a > 0).mean()),
            "t": float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))) if len(a) > 2 and a.std() > 0 else None}


# ---------------------------------------------------------------- tests
def test_baskets(C: np.ndarray, V: np.ndarray, A: np.ndarray, days: np.ndarray, spk: np.ndarray, camp: np.ndarray, btc: np.ndarray | None) -> dict[str, Any]:
    first = MIN_AGE_D + 8
    reb = list(range(first, len(days), STEP_D))
    elig = [eligible(C, V, A, i) for i in reb]
    top = {n: [idx[np.argsort(-r)[:n]] for idx, r in elig] for n in NS}
    bot = [idx[np.argsort(r)[:MAIN_N]] for idx, r in elig]
    alls = [idx for idx, _ in elig]
    runs: dict[str, Any] = {}
    for n in NS:
        runs[f"top{n}"] = basket(C, A, reb, top[n])
    runs["top_no_earn"] = basket(C, A, reb, top[MAIN_N], earn_on=False)
    runs["top_spike_exit"] = basket(C, A, reb, top[MAIN_N], exit_mask=spk)
    runs["top_campaign_exit"] = basket(C, A, reb, top[MAIN_N], exit_mask=camp)
    runs["bottom"] = basket(C, A, reb, bot)
    runs["all"] = basket(C, A, reb, alls)
    rng = np.random.default_rng(0)
    sims = np.array([basket(C, A, reb, [rng.choice(idx, size=min(MAIN_N, len(idx)), replace=False) if len(idx) else idx for idx, _ in elig])["ret"]
                     for _ in range(SIMS)])
    held = np.zeros_like(spk)                  # control for the spike exit: sell random held coin-days at the same rate
    for k, i in enumerate(reb):
        held[i:reb[k + 1] if k + 1 < len(reb) else len(C), top[MAIN_N][k]] = True
    p_exit = float((spk & held).sum() / held.sum()) if held.any() else 0.0
    xs = np.array([basket(C, A, reb, top[MAIN_N], exit_mask=rng.random(spk.shape) < p_exit)["ret"] for _ in range(SIMS)])
    d0 = days[reb[0]:]
    out = {"start": ms_to_str(int(days[reb[0]]), "%Y-%m-%d"), "end": ms_to_str(int(days[-1]), "%Y-%m-%d"), "rebalances": len(reb),
           "eligible_median": int(np.median([len(i) for i in alls])) if alls else 0,
           "random": {"p05": float(np.quantile(sims, 0.05)), "p50": float(np.median(sims)), "p95": float(np.quantile(sims, 0.95)),
                      "beat": float((sims < runs[f"top{MAIN_N}"]["ret"]).mean()), "sims": SIMS},
           "random_exit": {"p05": float(np.quantile(xs, 0.05)), "p50": float(np.median(xs)), "p95": float(np.quantile(xs, 0.95)),
                           "beat": float((xs < runs["top_spike_exit"]["ret"]).mean()), "exit_prob": p_exit, "sims": SIMS}}
    if btc is not None:
        b = btc[reb[0] - 1:]
        b = pd.Series(b).ffill().to_numpy()
        bv = b[1:] / b[0]
        peak = np.maximum.accumulate(bv)
        runs["btc"] = {"ret": float(bv[-1] - 1), "max_dd": float((1 - bv / peak).max()), "vals": bv}
    out["curves"] = {k: curve(d0, runs[k]["vals"]) for k in (f"top{MAIN_N}", "top_no_earn", "all", "btc") if k in runs}
    out["runs"] = {k: {kk: vv for kk, vv in v.items() if kk != "vals"} for k, v in runs.items()}
    last_idx, last_r = elig[-1]
    o = np.argsort(-last_r)[:MAIN_N]
    out["last_pick"] = {"t": ms_to_str(int(days[reb[-1]]), "%Y-%m-%d"), "coins": [{"inst": None, "rate": float(last_r[k]), "col": int(last_idx[k])} for k in o]}
    return out


def test_short(C: np.ndarray, V: np.ndarray, A: np.ndarray, F: np.ndarray, days: np.ndarray) -> dict[str, Any]:
    """The reverse: short the same top-rate basket. Whole period without carry, and paying the lending rate (what a margin
    short pays at the least: borrowers pay more than lenders get); from the first full month with funding history, with the
    swap funding actually paid or received, next to the same window without it."""
    first = MIN_AGE_D + 8
    reb = list(range(first, len(days), STEP_D))
    elig = [eligible(C, V, A, i) for i in reb]
    top = [idx[np.argsort(-r)[:MAIN_N]] for idx, r in elig]
    runs: dict[str, Any] = {"short": short_basket(C, reb, top), "short_margin": short_basket(C, reb, top, carry=-np.nan_to_num(A) / 365)}
    longs = basket(C, A, reb, [idx for idx, _ in elig])
    runs["hedge"] = hedge(reb, longs["vals"], runs["short"]["vals"])
    runs["hedge_margin"] = hedge(reb, longs["vals"], runs["short_margin"]["vals"])
    rng = np.random.default_rng(2)
    sims = np.array([short_basket(C, reb, [rng.choice(idx, size=min(MAIN_N, len(idx)), replace=False) if len(idx) else idx for idx, _ in elig])["ret"]
                     for _ in range(SIMS)])
    out: dict[str, Any] = {"start": ms_to_str(int(days[reb[0]]), "%Y-%m-%d"),
                           "random": {"p05": float(np.quantile(sims, 0.05)), "p50": float(np.median(sims)), "p95": float(np.quantile(sims, 0.95)),
                                      "beat": float((sims < runs["short"]["ret"]).mean()), "sims": SIMS}}
    has_f = np.isfinite(F).any(axis=1)
    f0 = int(np.argmax(has_f)) if has_f.any() else len(days)
    wreb = [i for i in reb if i - 1 >= f0]
    if wreb:
        k0 = reb.index(wreb[0])
        wtop = top[k0:]
        runs["short_w"] = short_basket(C, wreb, wtop)
        runs["short_w_funding"] = short_basket(C, wreb, wtop, carry=F)
        runs["short_w_margin"] = short_basket(C, wreb, wtop, carry=-np.nan_to_num(A) / 365)
        held = [(j, c) for k, i in enumerate(wreb) for j in range(i, wreb[k + 1] if k + 1 < len(wreb) else len(C)) for c in wtop[k]]
        fv = np.array([F[j, c] for j, c in held])
        av = np.array([A[j, c] for j, c in held])
        out["window"] = {"start": ms_to_str(int(days[wreb[0]]), "%Y-%m-%d"), "rebalances": len(wreb),
                         "funding_cover": float(np.isfinite(fv).mean()) if len(fv) else 0.0,
                         "funding_apy": float(np.nanmean(fv) * 365) if np.isfinite(fv).any() else None,   # >0: shorts received
                         "lending_apy": float(np.nanmean(av)) if np.isfinite(av).any() else None}
    offs = []                                  # the same main setting started 5, 10, ... days later: how much is the start date
    for off in range(0, STEP_D, 5):
        rb = list(range(first + off, len(days), STEP_D))
        el = [eligible(C, V, A, i) for i in rb]
        sm = short_basket(C, rb, [idx[np.argsort(-r)[:MAIN_N]] for idx, r in el], carry=-np.nan_to_num(A) / 365)
        hm = hedge(rb, basket(C, A, rb, [idx for idx, _ in el])["vals"], sm["vals"])
        offs.append({"offset": off, "short_margin": sm["ret"], "short_margin_dd": sm["max_dd"], "hedge_margin": hm["ret"], "hedge_margin_dd": hm["max_dd"]})
    out["offsets"] = offs
    d0 = days[reb[0]:]
    out["curves"] = {k: curve(d0, runs[k]["vals"]) for k in ("short", "short_margin", "hedge", "hedge_margin")}
    out["runs"] = {k: {kk: vv for kk, vv in v.items() if kk != "vals"} for k, v in runs.items()}
    return out


def test_spikes(C: np.ndarray, A: np.ndarray, M: np.ndarray, spk: np.ndarray) -> dict[str, Any]:
    T, N = C.shape
    cumM = np.concatenate([[0.0], np.cumsum(M)])
    L = np.log(C)
    ev: list[tuple[int, int]] = []
    for c in range(N):
        last = -10**9
        for j in np.flatnonzero(spk[:, c]):
            if j - last >= COOLDOWN_D:
                ev.append((j, c))
                last = j

    def measure(pairs: list[tuple[int, int]]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        js = np.array([p[0] for p in pairs], dtype=int)
        cs = np.array([p[1] for p in pairs], dtype=int)
        for h in HORIZONS:
            ok = js + h < T
            raw = np.full(len(js), np.nan)
            ab = np.full(len(js), np.nan)
            raw[ok] = L[js[ok] + h, cs[ok]] - L[js[ok], cs[ok]]
            ab[ok] = raw[ok] - (cumM[js[ok] + h + 1] - cumM[js[ok] + 1])
            out[f"fwd{h}"], out[f"abn{h}"] = np.expm1(raw), np.expm1(ab)
        ok = js - 7 >= 0
        p7 = np.full(len(js), np.nan)
        p7[ok] = L[js[ok], cs[ok]] - L[js[ok] - 7, cs[ok]]
        pa = np.full(len(js), np.nan)
        pa[ok] = p7[ok] - (cumM[js[ok] + 1] - cumM[js[ok] - 6])
        out["past7"], out["past7_abn"] = np.expm1(p7), np.expm1(pa)
        return out

    if not ev:
        return {"n": 0}
    act = measure(ev)
    rng = np.random.default_rng(1)
    near = np.zeros_like(spk)
    for j, c in ev:
        near[max(0, j - COOLDOWN_D):j + COOLDOWN_D, c] = True
    valid = np.isfinite(C) & np.isfinite(A) & ~near
    pool_by_c = {c: np.flatnonzero(valid[7:T - 30, c]) + 7 for c in {c for _, c in ev}}
    plac: dict[str, list[float]] = {k: [] for k in ("abn7", "abn30", "abn90", "past7_abn")}
    for _ in range(PLACEBO):
        pairs = [(int(rng.choice(pool_by_c[c])), c) for _, c in ev if len(pool_by_c[c])]
        m = measure(pairs)
        for k in plac:
            plac[k].append(float(np.nanmean(m[k])))
    res: dict[str, Any] = {"n": len(ev), "coins": len({c for _, c in ev}), "by": {k: stats(v) for k, v in act.items()}, "placebo": {}}
    for k, v in plac.items():
        a = np.array(v)
        m = float(np.nanmean(act[k]))
        res["placebo"][k] = {"actual": m, "p50": float(np.median(a)), "p05": float(np.quantile(a, 0.05)), "p95": float(np.quantile(a, 0.95)),
                             "share_below": float((a < m).mean())}
    pumped = act["past7"] > 0.3
    res["after_pump"] = {"n": int(pumped.sum()), "abn30": stats(act["abn30"][pumped]), "abn90": stats(act["abn90"][pumped])}
    res["no_pump"] = {"n": int((~pumped).sum()), "abn30": stats(act["abn30"][~pumped]), "abn90": stats(act["abn90"][~pumped])}
    res["events"] = ev
    return res


def test_quintiles(C: np.ndarray, V: np.ndarray, A: np.ndarray) -> dict[str, Any]:
    T = len(C)
    L = np.log(C)
    rows = []
    for i in range(MIN_AGE_D + 8, T - 7, 7):
        idx, r = eligible(C, V, A, i)
        if len(idx) < 25:
            continue
        q = pd.qcut(pd.Series(r).rank(method="first"), 5, labels=False).to_numpy()
        for h in (7, 30):
            if i - 1 + h >= T:
                continue
            f = L[i - 1 + h, idx] - L[i - 1, idx]
            ok = np.isfinite(f)
            if ok.sum() < 25:
                continue
            ex = f - np.median(f[ok])
            rows += [{"i": i, "h": h, "q": int(qq) + 1, "ex": float(np.expm1(e)), "rate": float(rr)} for qq, e, rr, o in zip(q, ex, r, ok) if o]
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    out: dict[str, Any] = {"weeks": int(df["i"].nunique()), "quintiles": []}
    for qq in range(1, 6):
        s = df[df["q"] == qq]
        out["quintiles"].append({"q": qq, "rate_median": float(s["rate"].median()),
                                 "ex7": float(s[s["h"] == 7]["ex"].mean()), "ex30": float(s[s["h"] == 30]["ex"].mean())})
    w = df[df["h"] == 7].groupby(["i", "q"])["ex"].mean().unstack()
    spread = (w[5] - w[1]).dropna().to_numpy()
    out["spread7"] = stats(spread)
    return out


# ---------------------------------------------------------------- run
def evaluate(cfg: Config) -> dict[str, Any]:
    t0 = time.monotonic()
    Cdf, Vdf, Adf, full = panels(cfg)
    days = Cdf.index.to_numpy(np.int64)
    insts = list(Cdf.columns)
    C, V, A = Cdf.to_numpy(float), Vdf.to_numpy(float), Adf.to_numpy(float)
    M = alt_index(Cdf, full)
    btc = full["BTC-USDT-SWAP"].reindex(days).to_numpy(float) if "BTC-USDT-SWAP" in full else None
    spk = spikes(A)
    cd = campaign_days(cfg, Cdf)
    camp = np.zeros_like(spk)
    pos = {d: k for k, d in enumerate(days)}
    for inst, ds in cd.items():
        c = insts.index(inst)
        for d in ds:
            if d in pos:
                camp[pos[d], c] = True
    log.info("earn study: %d coins with earn and a swap, %s .. %s", len(insts), ms_to_str(int(days[0]), "%Y-%m-%d"), ms_to_str(int(days[-1]), "%Y-%m-%d"))
    bk = test_baskets(C, V, A, days, spk, camp, btc)
    for c in bk["last_pick"]["coins"]:
        c["inst"] = insts[c.pop("col")]
    sp = test_spikes(C, A, M, spk)
    ev = sp.pop("events", [])
    sp["recent"] = [{"t": ms_to_str(int(days[j]), "%Y-%m-%d"), "inst": insts[c], "rate": float(A[j, c]),
                     "past7": float(C[j, c] / C[j - 7, c] - 1) if j >= 7 else None,
                     "fwd30": float(C[j + 30, c] / C[j, c] - 1) if j + 30 < len(C) else None} for j, c in sorted(ev, key=lambda x: -x[0])[:40]]
    qt = test_quintiles(C, V, A)
    sh = test_short(C, V, A, funding_panel(cfg, Cdf).to_numpy(float), days)
    cover = np.isfinite(A).sum(axis=0)
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1),
           "range": [ms_to_str(int(days[0]), "%Y-%m-%d"), ms_to_str(int(days[-1]), "%Y-%m-%d")], "coins": len(insts),
           "coins_with_rates": int((cover > 0).sum()), "campaign_coins": len(cd),
           "params": {"step_d": STEP_D, "ns": list(NS), "main_n": MAIN_N, "min_age_d": MIN_AGE_D, "min_vq": MIN_VQ, "spike_apy": SPIKE_APY,
                      "spike_x": SPIKE_X, "cooldown_d": COOLDOWN_D, "sims": SIMS, "placebo": PLACEBO},
           "baskets": bk, "spikes": sp, "quintiles": qt, "short": sh,
           "caveats": ["利息假設持有的幣全部借出、照當天實際借出利率（lendingRate）計息；實際上不一定全部借得出去，高利率常常只有一小部分額度，所以利息是上限",
                       "價格用永續合約的日收盤代替現貨；沒算買賣手續費與滑價",
                       f"主要設定（年化最高的前 {MAIN_N} 個、每 {STEP_D} 天換一次）是事先定的；5、20 個與兩種賣出規則只是參考",
                       "2025-09 以前的價格只有現存的老合約（倖存者偏差，會讓持有看起來比實際好）；下架的幣價格停在最後一天，也是偏樂觀",
                       "利率歷史只有 OKX 目前還有簡單賺幣的幣；已經下架的賺幣幣種不在裡面",
                       "反向（做空）：照 1 倍保證金算，某個幣漲一倍那格就爆倉歸零；整段沒有資金費率資料（OKX 只留約 3 個月），用借出利率當做空成本的下限（借幣的人付的比借出的人拿的多），有資金費率的那幾個月拿來對照",
                       "做空的結果偏保守的地方：已下架的幣（多半是跌掉的）不在資料裡；偏樂觀的地方：沒算手續費、滑價，也沒考慮高年化的幣借不到或合約深度不夠",
                       "「OKX 公告活動就賣」只能用抓得到的活動公告（多半是新幣）；年化高的老幣幾乎沒有公告，所以這條規則很少或完全沒有觸發"]}
    p = cfg.paths.results / "earn.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    log.info("earn study done in %.0fs", res["elapsed_sec"])
    return res


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "earn.json"
    try:
        res = json.loads(p.read_text(encoding="utf-8").replace("NaN", "null").replace("Infinity", "null")) if p.exists() else None
    except ValueError:
        return None
    if res is not None:
        try:
            res["data"] = earn.status(cfg)
        except Exception:  # noqa: BLE001 - the backfill may hold the database
            res["data"] = None
    return res
