"""Reward-pool campaigns (瓜分活動) and the featured token's price: event study, placebo, market-direction splits,
plus the campaign paper traders and alerts.

Research (`evaluate`, results/campaigns.json)
  One event per (campaign, token) whose USDT swap has candles. Prices are hourly closes from the 5m candles.
  Windows: anno = announcement -> start, dur = start -> end, post3 / post7 = end -> end + 3 / 7 days.
  raw = token log return over the window; ab = raw minus the median of age-matched controls over the same window
  (other swaps without a campaign within 14 days of the window, aged between max(14, age/2) and max(2 x age, 60) days
  at the window start, BTC/ETH excluded). Also vs BTC and vs all alts. Volatility / volume during the campaign are
  compared with the 7 days before the announcement (log ratio, minus the same ratio for the controls).
  Placebo: the same tokens and window lengths at random dates away from their campaigns; p = share of placebo means
  at least as extreme as the actual mean. Main windows: Flash/Airdrop Earn (rewards paid while it runs) -> dur;
  Trade-to-Earn (rewards paid after it ends) -> post3.
  Market direction: BTC and alt-market (median of alts listed >= 30 days) returns in the 7 days before the window
  (known at the start) and during it (after the fact); up/down splits with a permutation test, and OLS of the token
  return on the market return (alpha = expected token return when the market is flat).

Paper traders (kind "campaign", live only: the announcement API has no campaigns in the old blind-test period)
  camp_earn       short the token at the first bar open at/after a Flash/Airdrop Earn start, exit at its end
  camp_t2e_post   short at a Trade-to-Earn end, hold 3 days
  *_ctrl          same time, same hold, a deterministic age-matched control coin without a campaign
  All with a 20% stop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from okxrev import announce, db
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging
from okxrev.paper import Signal, Trader

log = logging.getLogger("okxrev.campaigns")

H = 3_600_000
D = 24 * H
BAR = db.BAR_MS
WINDOWS = ("anno", "dur", "post3", "post7")
MAIN = {"earn": "dur", "trade": "post3"}
GROUP_LABEL = {"earn": "閃賺／空投賺幣（活動期間發獎勵）", "trade": "交易賺幣（結束後發獎勵）"}
WINDOW_LABEL = {"anno": "公告到開始", "dur": "活動期間", "post3": "結束後 3 天", "post7": "結束後 7 天"}
MIN_N = 8
STOP = 0.20


# ---------------------------------------------------------------- hourly panel
@dataclass
class Panel:
    idx: np.ndarray          # hour starts (ms)
    cols: list[str]
    L: np.ndarray            # log close, forward-filled  [hour, inst]
    LR: np.ndarray           # hourly log return (NaN where the coin had no bar)
    V: np.ndarray            # hourly quote volume
    first: np.ndarray        # first hour with data per inst

    def __post_init__(self) -> None:
        self.ci = {c: i for i, c in enumerate(self.cols)}
        self.last_ts = int(self.idx[-1]) + H if len(self.idx) else 0
        self.excl = np.array([c.split("-")[0] in ("BTC", "ETH", "USDC") for c in self.cols])

    def pos(self, t: int) -> int:
        """Index of the last hourly bar that ends at or before t (its close is the price at t)."""
        return int(np.searchsorted(self.idx, t - H, side="right")) - 1

    def lret(self, a: int, b: int) -> np.ndarray | None:
        i, j = self.pos(a), self.pos(b)
        if i < 0 or j <= i:
            return None
        return self.L[j] - self.L[i]

    def ages(self, t: int) -> np.ndarray:
        return (t - self.first) / D

    def has(self, k: int, a: int) -> bool:
        return bool(self.first[k] <= a - H)

    def vol(self, a: int, b: int, need: float = 0.8) -> tuple[np.ndarray, np.ndarray]:
        """Daily-scaled std of hourly log returns and mean hourly volume over [a, b); NaN when coverage < need."""
        m = (self.idx >= a) & (self.idx < b)
        n = int(m.sum())
        if n < 3:
            nan = np.full(len(self.cols), np.nan)
            return nan, nan
        lr, v = self.LR[m], self.V[m]
        ok = np.isfinite(lr).sum(0) >= need * n
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            s = np.nanstd(lr, axis=0, ddof=1) * np.sqrt(24)
            mv = np.nanmean(v, axis=0)
        s[~ok], mv[~ok] = np.nan, np.nan
        return s, mv


def hourly_panel(cfg: Config, since: int, path: Any = None) -> Panel:
    with db.open_db(path or cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        h = con.execute(f"""
            SELECT inst_id, (ts // {H}) * {H} AS h, arg_max(close, ts) AS c, SUM(vol_quote) AS vq
            FROM candles WHERE ts >= ? AND inst_id LIKE '%-USDT-SWAP' GROUP BY 1, 2""", [since]).df()
    C = h.pivot(index="h", columns="inst_id", values="c").sort_index()
    V = h.pivot(index="h", columns="inst_id", values="vq").reindex(C.index)
    full = np.arange(int(C.index.min()), int(C.index.max()) + H, H)
    C, V = C.reindex(full), V.reindex(full)
    logc = np.log(C)
    first = np.array([int(full[np.argmax(C[c].notna().to_numpy())]) for c in C.columns], dtype=np.int64)
    return Panel(full.astype(np.int64), list(C.columns), logc.ffill().to_numpy(), logc.diff().to_numpy(), V.to_numpy(), first)


# ---------------------------------------------------------------- events
def load_events(cfg: Config, panel: Panel | None = None) -> pd.DataFrame:
    ev = announce.campaign_tokens(announce.load_campaigns(cfg))
    if ev.empty:
        return ev
    ev = ev.dropna(subset=["start_ts", "end_ts"])
    if panel is not None:
        ev = ev[ev["inst_id"].isin(panel.ci)]
    for k in ("p_time", "start_ts", "end_ts"):
        ev[k] = ev[k].astype(np.int64)
    return ev.sort_values("start_ts").reset_index(drop=True)


def ev_index(panel: Panel, ev: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (ev["p_time"].to_numpy(np.int64), ev["end_ts"].to_numpy(np.int64),
            np.array([panel.ci.get(i, -1) for i in ev["inst_id"]], dtype=np.int64))


def busy_mask(panel: Panel, evx: tuple[np.ndarray, np.ndarray, np.ndarray], a: int, b: int, pad: int = 14 * D) -> np.ndarray:
    """Coins with a campaign (announcement to end) overlapping [a - pad, b + pad]."""
    p, e, k = evx
    m = np.zeros(len(panel.cols), dtype=bool)
    sel = (p < b + pad) & (e > a - pad) & (k >= 0)
    m[k[sel]] = True
    return m


def controls(panel: Panel, k: int, a: int, busy: np.ndarray) -> np.ndarray:
    ages = panel.ages(a)
    age = ages[k]
    return (~busy) & (~panel.excl) & (ages >= max(14.0, age / 2)) & (ages <= max(age * 2, 60.0))


def abnormal(panel: Panel, k: int, a: int, b: int, busy: np.ndarray) -> dict[str, float]:
    out = {"raw": np.nan, "ab": np.nan, "ab_btc": np.nan, "ab_alt": np.nan, "n_ctrl": 0}
    r = panel.lret(a, b)
    if r is None or not np.isfinite(r[k]) or not panel.has(k, a):
        return out
    out["raw"] = float(r[k])
    m = controls(panel, k, a, busy) & np.isfinite(r)
    m[k] = False
    if m.sum() >= 5:
        out["ab"], out["n_ctrl"] = float(r[k] - np.median(r[m])), int(m.sum())
    alts = (~panel.excl) & np.isfinite(r) & (panel.ages(a) >= 30)
    alts[k] = False
    if alts.sum() >= 5:
        out["ab_alt"] = float(r[k] - np.median(r[alts]))
    b_ = panel.ci.get("BTC-USDT-SWAP")
    if b_ is not None and np.isfinite(r[b_]):
        out["ab_btc"] = float(r[k] - r[b_])
    return out


def market(panel: Panel, a: int, b: int) -> tuple[float, float]:
    r = panel.lret(a, b)
    if r is None:
        return np.nan, np.nan
    b_ = panel.ci.get("BTC-USDT-SWAP")
    alts = (~panel.excl) & np.isfinite(r) & (panel.ages(a) >= 30)
    return (float(r[b_]) if b_ is not None else np.nan), (float(np.median(r[alts])) if alts.sum() >= 5 else np.nan)


def window(r: Any, w: str) -> tuple[int, int]:
    return {"anno": (r.p_time, r.start_ts), "dur": (r.start_ts, r.end_ts), "post3": (r.end_ts, r.end_ts + 3 * D),
            "post7": (r.end_ts, r.end_ts + 7 * D)}[w]


def event_rows(panel: Panel, ev: pd.DataFrame) -> pd.DataFrame:
    evx = ev_index(panel, ev)
    rows = []
    for r in ev.itertuples(index=False):
        k = panel.ci[r.inst_id]
        d: dict[str, Any] = {"url": r.url, "token": r.token, "inst_id": r.inst_id, "kind": r.kind, "grp": r.grp, "title": r.title,
                             "p_time": r.p_time, "start_ts": r.start_ts, "end_ts": r.end_ts, "days": (r.end_ts - r.start_ts) / D,
                             "age_d": float(panel.ages(r.start_ts)[k])}
        for w in WINDOWS:
            a, b = window(r, w)
            if b > panel.last_ts or b <= a:
                continue
            busy = busy_mask(panel, evx, a, b)
            busy[k] = False
            for key, v in abnormal(panel, k, a, b, busy).items():
                d[f"{w}_{key}"] = v
        a, b = window(r, MAIN[r.grp])
        if b <= panel.last_ts:
            d["btc_pre"], d["alt_pre"] = market(panel, a - 7 * D, a)
            d["btc_win"], d["alt_win"] = market(panel, a, b)
        if r.end_ts <= panel.last_ts and panel.has(k, r.p_time - 7 * D):
            s0, v0 = panel.vol(r.p_time - 7 * D, r.p_time)
            s1, v1 = panel.vol(r.start_ts, r.end_ts)
            busy = busy_mask(panel, evx, r.p_time - 7 * D, r.end_ts)
            busy[k] = False
            m = controls(panel, k, r.start_ts, busy) & np.isfinite(s0) & np.isfinite(s1) & (s0 > 0) & (v0 > 0) & np.isfinite(v1)
            m[k] = False
            if np.isfinite(s0[k]) and np.isfinite(s1[k]) and s0[k] > 0 and v0[k] > 0 and m.sum() >= 5:
                with np.errstate(divide="ignore", invalid="ignore"):
                    d["vol_ratio"] = float(np.log(s1[k] / s0[k]))
                    d["vol_ratio_ab"] = d["vol_ratio"] - float(np.median(np.log(s1[m] / s0[m])))
                    d["v_ratio"] = float(np.log(v1[k] / v0[k])) if v1[k] > 0 else np.nan
                    lv = np.log(v1[m] / v0[m])
                    d["v_ratio_ab"] = d["v_ratio"] - float(np.median(lv[np.isfinite(lv)])) if v1[k] > 0 else np.nan
        rows.append(d)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- statistics
def stats(x: Any, boot: int = 2000, seed: int = 0) -> dict[str, Any]:
    x = pd.Series(x, dtype=float).dropna().to_numpy()
    n = len(x)
    if n == 0:
        return {"n": 0}
    out: dict[str, Any] = {"n": n, "mean": float(x.mean()), "median": float(np.median(x)), "neg": float(np.mean(x < 0))}
    if n >= 3 and x.std(ddof=1) > 0:
        out["t"] = float(x.mean() / (x.std(ddof=1) / np.sqrt(n)))
        rng = np.random.default_rng(seed)
        bs = x[rng.integers(0, n, (boot, n))].mean(1)
        out["ci95"] = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]
    return out


def placebo(panel: Panel, ev: pd.DataFrame, rows: pd.DataFrame, grp: str, w: str, draws: int = 300, seed: int = 7) -> dict[str, Any]:
    """Same tokens and window lengths at random hours at least 7 days after listing, not overlapping any campaign of
    that token (announcement - 7 days to end + 10 days)."""
    col = f"{w}_ab"
    sub = rows[(rows["grp"] == grp)] if col in rows else rows.iloc[0:0]
    sub = sub[sub[col].notna()] if col in sub else sub
    if len(sub) < 3:
        return {"n": int(len(sub))}
    actual = float(sub[col].mean())
    evx = ev_index(panel, ev)
    rng = np.random.default_rng(seed)
    blocked = {t: [(r.p_time - 7 * D, r.end_ts + 10 * D) for r in g.itertuples(index=False)] for t, g in ev.groupby("token")}
    lens = [(r.inst_id, r.token, int(r.end_ts - r.start_ts) if w == "dur" else int(window(r, w)[1] - window(r, w)[0])) for r in sub.itertuples(index=False)]
    means = []
    for _ in range(draws):
        vals = []
        for inst, tok, ln in lens:
            k = panel.ci[inst]
            lo, hi = int(panel.first[k]) + 7 * D, panel.last_ts - ln
            if hi <= lo:
                continue
            for _try in range(30):
                a = int(rng.integers(lo // H, hi // H)) * H
                if any(a < y and a + ln > x for x, y in blocked.get(tok, [])):
                    continue
                busy = busy_mask(panel, evx, a, a + ln)
                busy[k] = False
                v = abnormal(panel, k, a, a + ln, busy)["ab"]
                if np.isfinite(v):
                    vals.append(v)
                break
        if vals:
            means.append(float(np.mean(vals)))
    pm = np.array(means)
    if not len(pm):
        return {"n": int(len(sub)), "actual": actual}
    p = float(np.mean(pm <= actual)) if actual < 0 else float(np.mean(pm >= actual))
    return {"n": int(len(sub)), "actual": actual, "placebo_mean": float(pm.mean()), "placebo_sd": float(pm.std()), "p": p, "draws": len(pm)}


def perm_p(a: np.ndarray, b: np.ndarray, n: int = 5000, seed: int = 1) -> float | None:
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return None
    rng = np.random.default_rng(seed)
    d = abs(a.mean() - b.mean())
    z = np.concatenate([a, b])
    c = 0
    for _ in range(n):
        rng.shuffle(z)
        c += abs(z[:len(a)].mean() - z[len(a):].mean()) >= d - 1e-12
    return c / n


def ols(y: np.ndarray, x: np.ndarray) -> dict[str, Any] | None:
    m = np.isfinite(y) & np.isfinite(x)
    y, x = y[m], x[m]
    n = len(y)
    if n < 5 or x.std() == 0:
        return None
    A = np.c_[np.ones(n), x]
    b = np.linalg.lstsq(A, y, rcond=None)[0]
    e = y - A @ b
    s2 = e @ e / (n - 2)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(A.T @ A)))
    sst = (y - y.mean()) @ (y - y.mean())
    return {"n": n, "alpha": float(b[0]), "beta": float(b[1]), "t_alpha": float(b[0] / se[0]), "t_beta": float(b[1] / se[1]),
            "r2": float(1 - (e @ e) / sst) if sst > 0 else None}


REGIME_VARS = {"btc_pre": "BTC 前 7 天", "alt_pre": "大盤前 7 天", "btc_win": "BTC 同期", "alt_win": "大盤同期"}


def regimes(rows: pd.DataFrame, grp: str) -> dict[str, Any]:
    w = MAIN[grp]
    d = rows[rows["grp"] == grp]
    out: dict[str, Any] = {"window": w, "splits": [], "ols": []}
    if f"{w}_raw" not in d:
        return out
    d = d[d[f"{w}_raw"].notna()]
    for var, label in REGIME_VARS.items():
        if var not in d:
            continue
        up, dn = d[d[var] > 0], d[d[var] <= 0]
        row = {"var": var, "label": label}
        for y in ("raw", "ab"):
            col = f"{w}_{y}"
            row[f"up_{y}"], row[f"down_{y}"] = stats(up[col]), stats(dn[col])
            row[f"p_{y}"] = perm_p(up[col].to_numpy(float), dn[col].to_numpy(float))
        out["splits"].append(row)
    for y, x in (("raw", "btc_win"), ("raw", "alt_win"), ("raw", "btc_pre"), ("ab", "btc_win")):
        if x in d:
            r = ols(d[f"{w}_{y}"].to_numpy(float), d[x].to_numpy(float))
            if r:
                out["ols"].append({"y": y, "x": x, **r})
    return out


def curves(panel: Panel, ev: pd.DataFrame, rows: pd.DataFrame, step_h: int = 2, min_n: int = 3) -> dict[str, Any]:
    """Mean abnormal cumulative log return (vs age-matched controls) by hour around the start and the end."""
    out: dict[str, Any] = {}
    evx = ev_index(panel, ev)
    spans = {"start": (-7 * 24, 10 * 24), "end": (-5 * 24, 7 * 24)}
    for grp in ("earn", "trade"):
        for anchor, (h0, h1) in spans.items():
            hours = np.arange(h0, h1 + 1, step_h)
            A = []
            for r in rows[rows["grp"] == grp].itertuples(index=False):
                k = panel.ci[r.inst_id]
                t0 = r.start_ts if anchor == "start" else r.end_ts
                i0 = panel.pos(t0)
                if i0 < 0:
                    continue
                busy = busy_mask(panel, evx, t0 + h0 * H, t0 + h1 * H)
                busy[k] = False
                m0 = controls(panel, k, t0, busy)
                m0[k] = False
                ys = np.full(len(hours), np.nan)
                for j, hh in enumerate(hours):
                    t = t0 + int(hh) * H
                    if t > panel.last_ts or not panel.has(k, min(t, t0)):
                        continue
                    i = panel.pos(t)
                    if i < 0:
                        continue
                    rr = panel.L[i] - panel.L[i0]
                    m = m0 & np.isfinite(rr)
                    if np.isfinite(rr[k]) and m.sum() >= 5:
                        ys[j] = rr[k] - np.median(rr[m])
                A.append(ys)
            if not A:
                continue
            M = np.array(A)
            n = np.isfinite(M).sum(0)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                mu = np.nanmean(M, 0)
                se = np.nanstd(M, 0, ddof=1) / np.sqrt(np.maximum(n, 1))
            keep = n >= min_n
            out[f"{grp}_{anchor}"] = {"h": hours[keep].tolist(), "mean": mu[keep].tolist(), "lo": (mu - 1.96 * se)[keep].tolist(),
                                      "hi": (mu + 1.96 * se)[keep].tolist(), "n": n[keep].tolist()}
    return out


def verdict(pl: dict[str, Any]) -> str:
    if pl.get("n", 0) < MIN_N:
        return "樣本不足"
    p = pl.get("p")
    if p is None:
        return "樣本不足"
    return "顯著" if p < 0.05 else "不顯著"


def evaluate(cfg: Config, draws: int = 300) -> dict[str, Any]:
    t0 = time.monotonic()
    ev0 = load_events(cfg)
    if ev0.empty:
        raise RuntimeError("沒有已解析的瓜分活動（先執行 python -m okxrev.announce --backfill）")
    since = int(ev0["p_time"].min()) - 120 * D
    panel = hourly_panel(cfg, since)
    ev = load_events(cfg, panel)
    log.info("campaigns: %d events (%d campaigns), panel %d hours x %d coins", len(ev), ev["url"].nunique(), len(panel.idx), len(panel.cols))
    rows = event_rows(panel, ev)
    summary: dict[str, Any] = {}
    for grp in ("earn", "trade"):
        g = rows[rows["grp"] == grp]
        summary[grp] = {w: {y: stats(g.get(f"{w}_{y}", pd.Series(dtype=float))) for y in ("raw", "ab", "ab_btc", "ab_alt")} for w in WINDOWS}
        for c in ("vol_ratio", "vol_ratio_ab", "v_ratio", "v_ratio_ab"):
            summary[grp][c] = stats(g.get(c, pd.Series(dtype=float)))
    log.info("campaigns: placebo (%d draws)", draws)
    plac = {f"{grp}_{w}": placebo(panel, ev, rows, grp, w, draws) for grp in ("earn", "trade") for w in ("dur", "post3")}
    main = {grp: {"window": MAIN[grp], "verdict": verdict(plac[f"{grp}_{MAIN[grp]}"]), "placebo": plac[f"{grp}_{MAIN[grp]}"],
                  "stats": summary[grp][MAIN[grp]]["ab"]} for grp in ("earn", "trade")}
    res = {
        "generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1),
        "data_to": ms_to_str(panel.last_ts), "n_events": int(len(rows)), "n_campaigns": int(ev["url"].nunique()),
        "groups": GROUP_LABEL, "windows": WINDOW_LABEL, "main": main, "summary": summary, "placebo": plac,
        "regimes": {grp: regimes(rows, grp) for grp in ("earn", "trade")},
        "curves": curves(panel, ev, rows),
        "events": rows.replace({np.nan: None}).to_dict("records"),
        "caveats": [
            "樣本少（交易賺幣能用的只有十幾筆），而且同時看了多個時段與指標，有多重比較的風險；請以隨機日期對照的 p 值為主",
            "同一個幣可能連續辦好幾檔活動（期間重疊）",
            "用永續合約價格；很多交易賺幣是現貨活動，且常在上市當天開始，這些沒有活動前資料可比",
            "已下架的幣沒有資料（倖存者偏差）；未計入做空的資金費",
            "公告 API 的活動只到 2025-09",
        ],
    }
    out = cfg.paths.results / "campaigns.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, default=_json_default), encoding="utf-8")
    rows.to_csv(cfg.paths.results / "campaigns.csv", index=False)
    log.info("campaigns: done in %.0fs -> %s", time.monotonic() - t0, out)
    return res


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        f = float(o)
        return None if not np.isfinite(f) else f
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "campaigns.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null"))
    except ValueError:
        return None


# ---------------------------------------------------------------- live status
def phase(r: Any, now: int) -> str:
    if now < r.start_ts:
        return "upcoming"
    if now < r.end_ts:
        return "running"
    return "ended"


def live_status(cfg: Config, now: int | None = None, days: int = 30) -> list[dict[str, Any]]:
    """Campaign tokens announced or running in the last `days`, with the return so far: from the start (or from the
    announcement if not started) to the latest bar, raw and vs BTC / vs the median alt."""
    now = now or db.now_ms()
    ev = announce.campaign_tokens(announce.load_campaigns(cfg))
    if ev.empty:
        return []
    ev = ev.dropna(subset=["start_ts", "end_ts"])
    ev = ev[(ev["end_ts"] >= now - days * D) | (ev["p_time"] >= now - days * D)]
    if ev.empty:
        return []
    out = []
    try:
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=1.0, max_tries=10) as con:
            last = con.execute("SELECT MAX(ts) FROM candles WHERE inst_id = ?", [cfg.universe["btc_inst"]]).fetchone()[0]
            cache: dict[int, dict[str, float]] = {}

            def closes(t: int) -> dict[str, float]:
                """Latest close per coin among bars that end at or before t (within 2 hours)."""
                if t not in cache:
                    cache[t] = dict(con.execute("SELECT inst_id, arg_max(close, ts) FROM candles WHERE ts <= ? AND ts >= ? GROUP BY 1",
                                                [t - BAR, t - 2 * H]).fetchall())
                return cache[t]

            cur = closes(min(int(now), int(last) + BAR)) if last else {}
            for r in ev.itertuples(index=False):
                ph = phase(r, now)
                base = closes(int(r.start_ts if ph != "upcoming" else r.p_time))
                ref = closes(int(r.end_ts)) if ph == "ended" else cur
                d = {"url": r.url, "token": r.token, "inst_id": r.inst_id, "kind": r.kind, "grp": r.grp, "title": r.title,
                     "title_zh": getattr(r, "title_zh", None), "p_time": int(r.p_time), "start_ts": int(r.start_ts), "end_ts": int(r.end_ts),
                     "dist_ts": None if pd.isna(r.dist_ts) else int(r.dist_ts), "phase": ph, "pool": r.pool, "from": "start" if ph != "upcoming" else "announce"}
                b0, b1 = base.get(r.inst_id), ref.get(r.inst_id)
                if b0 and b1:
                    d["ret"] = float(np.log(b1 / b0))
                    btc = cfg.universe["btc_inst"]
                    if base.get(btc) and ref.get(btc):
                        d["vs_btc"] = d["ret"] - float(np.log(ref[btc] / base[btc]))
                    alts = [np.log(ref[i] / base[i]) for i in base if i in ref and i != r.inst_id and base[i] and ref[i]
                            and i.split("-")[0] not in ("BTC", "ETH", "USDC")]
                    if len(alts) >= 5:
                        d["vs_alts"] = d["ret"] - float(np.median(alts))
                out.append(d)
    except (db.DbBusy, FileNotFoundError):
        return []
    return sorted(out, key=lambda x: -x["start_ts"])


# ---------------------------------------------------------------- paper traders
TRADER_SETS = [
    {"id": "camp_earn", "grp": "earn", "anchor": "start", "hold": None, "ctrl": False,
     "label": "閃賺／空投賺幣：活動開始做空、結束出場（止損 20%）"},
    {"id": "camp_earn_ctrl", "grp": "earn", "anchor": "start", "hold": None, "ctrl": True,
     "label": "閃賺對照：同一時間做空同齡、沒有活動的幣"},
    {"id": "camp_t2e_post", "grp": "trade", "anchor": "end", "hold": 3 * D, "ctrl": False,
     "label": "交易賺幣：活動結束做空、持有 3 天（止損 20%）"},
    {"id": "camp_t2e_post_ctrl", "grp": "trade", "anchor": "end", "hold": 3 * D, "ctrl": True,
     "label": "交易賺幣對照：同一時間做空同齡、沒有活動的幣"},
]
EXIT_NAME = "camp_sl20"


def signal_bar(t: int) -> int:
    """The bar whose close is the last one before t: entering at the next bar's open fills at the first open >= t."""
    return -(-t // BAR) * BAR - BAR


def pick_control(token_inst: str, t: int, insts: list[str], ages: dict[str, float], busy: set[str]) -> str | None:
    """Deterministic age-matched coin without a campaign around t (same rule as the research controls)."""
    age = ages.get(token_inst)
    if age is None:
        return None
    cands = sorted(i for i in insts if i != token_inst and i not in busy and i.split("-")[0] not in ("BTC", "ETH", "USDC")
                   and ages.get(i) is not None and max(14.0, age / 2) <= ages[i] <= max(age * 2, 60.0))
    if not cands:
        return None
    h = int.from_bytes(hashlib.sha1(f"{token_inst}|{t}".encode()).digest()[:8], "big")
    return cands[h % len(cands)]


class CampaignTrader(Trader):
    kind = "campaign"
    source = None  # tests can replace the campaign loader: callable(cfg) -> DataFrame from announce.campaign_tokens

    def __init__(self, ts: dict[str, Any]):
        self.ts = ts
        self.trader_id = ts["id"]
        self.label = ts["label"]
        self.exit = EXIT_NAME
        self.ev = pd.DataFrame()

    def params(self) -> dict[str, Any]:
        return {"group": self.ts["grp"], "anchor": self.ts["anchor"], "hold_h": None if self.ts["hold"] is None else self.ts["hold"] // H,
                "stop": STOP, "control": self.ts["ctrl"]}

    def prepare(self, ctx: Any) -> None:
        src = type(self).source or (lambda cfg: announce.campaign_tokens(announce.load_campaigns(cfg)))
        ev = src(ctx.cfg)
        self.ev = ev.dropna(subset=["start_ts", "end_ts"]) if len(ev) else ev

    def signals_batch(self, ctx: Any, items: list[tuple[str, pd.DataFrame, np.ndarray]]) -> list[Signal]:
        if self.ev is None or self.ev.empty:
            return []
        new = {inst: set(np.asarray(bars, dtype=np.int64).tolist()) for inst, _, bars in items}
        ev = self.ev[self.ev["grp"] == self.ts["grp"]]
        out: list[Signal] = []
        for r in ev.itertuples(index=False):
            t = int(r.start_ts if self.ts["anchor"] == "start" else r.end_ts)
            hold = int(r.end_ts - r.start_ts) if self.ts["hold"] is None else int(self.ts["hold"])
            sb = signal_bar(t)
            if r.inst_id not in new or sb not in new[r.inst_id] or hold < BAR:
                continue
            inst = r.inst_id
            if self.ts["ctrl"]:
                ages = {}
                for i, f in ctx.feats.items():
                    if "age_d" in f and sb in f.index:
                        v = f.at[sb, "age_d"]
                        if v is not None and np.isfinite(v):
                            ages[i] = float(v)
                busy = set(self.ev[(self.ev["p_time"] < t + 14 * D) & (self.ev["end_ts"] > t - 14 * D)]["inst_id"])
                inst = pick_control(r.inst_id, t, sorted(new), ages, busy)
                if inst is None or sb not in new.get(inst, ()):
                    continue
            feats = {**ctx.snapshot(inst, sb), "campaign": r.title, "campaign_url": r.url, "token": r.token, "kind": r.kind,
                     "start": int(r.start_ts), "end": int(r.end_ts), "_hold_bars": max(1, hold // BAR), "_stop_frac": STOP}
            what = announce.KIND_LABEL.get(r.kind, r.kind)
            when = "開始" if self.ts["anchor"] == "start" else "結束"
            why = (f"{r.token} {what}{when}（{ms_to_str(t)}）" + (f"，對照幣 {inst}" if self.ts["ctrl"] else "") +
                   f"；持有 {hold / H:.0f} 小時、止損 {STOP * 100:.0f}%")
            out.append(Signal(self.trader_id, inst, sb, "short", EXIT_NAME, 0.0, why, features=feats))
        return out


def traders(cfg: Config, kinds: dict[str, bool]) -> list[CampaignTrader]:
    if not kinds.get("campaign", True) or not announce.acfg(cfg).get("enabled", True):
        return []
    return [CampaignTrader(ts) for ts in TRADER_SETS]


# ---------------------------------------------------------------- alerts
def live_alerts(cfg: Config, con: Any, now: int) -> int:
    """Alerts (heat_alerts) for campaigns announced in the last day and Trade-to-Earn campaigns that just ended,
    with what the research found for that kind."""
    if not ((cfg.paper or {}).get("campaign") or {}).get("alerts", True) or not announce.acfg(cfg).get("enabled", True):
        return 0
    from okxrev import heat
    con.execute(heat.ALERT_SCHEMA)
    ev = announce.campaign_tokens(announce.load_campaigns(cfg))
    if ev.empty:
        return 0
    res = load(cfg) or {}
    main = res.get("main") or {}

    def finding(grp: str) -> str:
        m = main.get(grp) or {}
        s, pl = m.get("stats") or {}, m.get("placebo") or {}
        if not s.get("n"):
            return ""
        return (f"歷史上{WINDOW_LABEL[MAIN[grp]]}平均跑輸同齡幣 {-np.expm1(s['mean']) * 100:.1f}%（{s['n']} 筆，隨機日期對照 p={pl.get('p', float('nan')):.3f}，"
                f"{m.get('verdict')}）" if s.get("mean") is not None and s["mean"] < 0 else
                f"歷史上{WINDOW_LABEL[MAIN[grp]]}平均 {np.expm1(s.get('mean', 0)) * 100:+.1f}%（{s['n']} 筆，{m.get('verdict')}）")

    n = 0
    for r in ev.dropna(subset=["start_ts", "end_ts"]).itertuples(index=False):
        what = announce.KIND_LABEL.get(r.kind, r.kind)
        if now - D <= r.p_time <= now:
            if not con.execute("SELECT 1 FROM heat_alerts WHERE inst_id = ? AND kind = 'campaign' AND ts >= ?", [r.inst_id, int(r.p_time)]).fetchone():
                det = (f"{r.token} {what}公告：{ms_to_str(r.start_ts)} ～ {ms_to_str(r.end_ts)}，獎池 {r.pool or '-'}。" + finding(r.grp) +
                       "。研究結果，不是交易建議。")
                con.execute("INSERT OR REPLACE INTO heat_alerts VALUES (?, ?, 'campaign', NULL, ?)", [now, r.inst_id, det])
                n += 1
        if r.grp == "trade" and r.end_ts <= now <= r.end_ts + 6 * H:
            if not con.execute("SELECT 1 FROM heat_alerts WHERE inst_id = ? AND kind = 'campaign_end' AND ts >= ?", [r.inst_id, int(r.end_ts)]).fetchone():
                det = f"{r.token} {what}已結束（{ms_to_str(r.end_ts)}），接下來發獎勵。" + finding("trade") + "。研究結果，不是交易建議。"
                con.execute("INSERT OR REPLACE INTO heat_alerts VALUES (?, ?, 'campaign_end', NULL, ?)", [now, r.inst_id, det])
                n += 1
    return n


# ---------------------------------------------------------------- CLI
def print_summary(res: dict[str, Any]) -> None:
    print(f"generated {res['generated_at']}  data to {res['data_to']}  {res['n_events']} events / {res['n_campaigns']} campaigns")
    pc = lambda v: "-" if v is None else f"{v * 100:+.1f}%"
    for grp in ("earn", "trade"):
        print(f"\n== {GROUP_LABEL[grp]}  main window: {WINDOW_LABEL[MAIN[grp]]} -> {res['main'][grp]['verdict']}")
        for w in WINDOWS:
            s = res["summary"][grp][w]["ab"]
            pl = res["placebo"].get(f"{grp}_{w}") or {}
            print(f"  {WINDOW_LABEL[w]:8s} n={s.get('n', 0):3d} mean={pc(s.get('mean'))} median={pc(s.get('median'))} neg={s.get('neg', 0) * 100:3.0f}% "
                  f"t={s.get('t', float('nan')):+.2f}" + (f"  placebo p={pl['p']:.3f}" if pl.get("p") is not None else ""))
        for o in res["regimes"][grp]["ols"]:
            print(f"  OLS {o['y']} ~ {o['x']}: alpha={pc(o['alpha'])} (t={o['t_alpha']:+.2f}) beta={o['beta']:+.2f} (t={o['t_beta']:+.2f})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.campaigns")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true", help="run the event study and write results/campaigns.json")
    ap.add_argument("--draws", type=int, default=300, help="placebo draws")
    ap.add_argument("--live", action="store_true", help="campaigns of the last 30 days with the return so far")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "campaigns")
    if args.evaluate:
        print_summary(evaluate(cfg, args.draws))
    elif not args.live:
        res = load(cfg)
        print_summary(res) if res else print("no results yet: python -m okxrev.campaigns --evaluate")
    if args.live:
        for d in live_status(cfg):
            print(f"{d['phase']:8s} {d['token']:6s} {d['kind']:10s} {ms_to_str(d['start_ts'])} ~ {ms_to_str(d['end_ts'])} "
                  f"ret={d.get('ret', float('nan')) * 100:+.1f}% vs alts={d.get('vs_alts', float('nan')) * 100:+.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
