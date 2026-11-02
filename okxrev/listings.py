"""Listing / delisting announcements and the coin's USDT swap price.

Event types (from English 'new listings' and 'delistings' announcements in data/announce.duckdb):

  spot_list     a coin that already has a USDT swap gets an OKX spot pair            ("list XDP/USDT ... for spot trading")
  xperp         a coin that already has a USDT swap gets a USD-margined X-Perp       ("list SANDUSD and PARTIUSD X-Perp")
  spot_delist   OKX delists a coin's spot pairs while its swap keeps trading         ("delist DORA, ICX ... spot trading pairs")
  perp_delist   OKX announces it will delist the coin's swap                          ("delist perpetual futures for ONEUSDT")
  perp_new      a new crypto swap listing (first days after the swap opens)          ("list perpetual futures for AXIS crypto")
  tradfi_new    a new stock / ETF swap listing                                        ("list TradFi perpetual futures for ...")

For the first four (coin already trading) the windows are relative to the announcement: 3 days before (leak /
anticipation), then 1, 3 and 7 days after; returns are vs age-matched controls (same rule as the campaign study),
with a placebo of random dates for the same coins. For new listings the windows start at the first hourly close of
the swap: 1, 3 and 7 days, raw and vs the median alt.
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

from okxrev import announce, campaigns as CP, db, regime
from okxrev.config import Config, load_config, ms_to_str
from okxrev.log import setup_logging

log = logging.getLogger("okxrev.listings")

H, D = CP.H, CP.D
STOP = {"OKX", "USDT", "USD", "USDC", "UTC", "API", "X", "TRADFI", "ETF", "IPO", "AND", "FOR", "THE", "NEW", "EQUITY", "PRE"}
EXISTING = ("spot_list", "xperp", "spot_delist", "perp_delist")
WIN = {"pre3": (-3 * D, 0), "d1": (0, D), "d3": (0, 3 * D), "d7": (0, 7 * D)}
NEW_WIN = {"d1": D, "d3": 3 * D, "d7": 7 * D}
TYPE_LABEL = {"spot_list": "已有合約、新上現貨", "xperp": "已有合約、新上 X-Perp（USD 本位）", "spot_delist": "下架現貨（合約照常）",
              "perp_delist": "公告下架合約", "perp_new": "新上加密貨幣合約", "tradfi_new": "新上美股／ETF 合約"}


def _ticks(s: str) -> list[str]:
    return [t for t in re.findall(r"\b[A-Z0-9]{2,14}\b", s) if t not in STOP and not t.isdigit()]


def classify(ann_type: str, title: str) -> tuple[str | None, list[str]]:
    t = title
    if ann_type == "announcements-delistings":
        if re.search(r"postpone|cancel|resum", t, re.I):
            return None, []
        m = re.search(r"delist (?:the )?perpetual futures for (.+)", t, re.I)
        if m:
            return "perp_delist", [x[:-4] if x.endswith("USDT") else x for x in _ticks(m.group(1))]
        m = re.search(r"delist (.+?) spot trading pairs?", t, re.I)
        if m and not re.search(r"selected|USD spot", m.group(1), re.I):
            return "spot_delist", _ticks(m.group(1))
        return None, []
    if ann_type != "announcements-new-listings" or re.search(r"Tokenized|USDC trading pairs|Adjust|pre-market|Pre-IPO", t, re.I):
        return None, []
    if re.search(r"X-Perp", t):
        if re.search(r"Equity", t):
            return None, []
        return "xperp", [x[:-3] for x in re.findall(r"\b([A-Z0-9]+USD)\b", t) if len(x) > 3]
    if re.search(r"delay|postpone", t, re.I):
        return None, []
    m = re.search(r"list ([A-Z0-9]+)(?:/USDT)?\s*(?:\([^)]*\))?\s*for spot trading", t)
    if m:
        return "spot_list", [m.group(1)]
    m = re.search(r"perpetual (?:futures )?for (.+?) crypto", t)
    if m:
        return "perp_new", _ticks(m.group(1))
    m = re.search(r"(?:TradFi perpetual futures for|perpetual futures for) (.+?)(?: equities)?$", t)
    if m and (re.search(r"TradFi|equities", t)):
        return "tradfi_new", _ticks(m.group(1))
    return None, []


def load_events(cfg: Config) -> pd.DataFrame:
    try:
        con = announce.connect(cfg, read_only=True)
    except (FileNotFoundError, db.DbBusy):
        return pd.DataFrame()
    try:
        a = con.execute("SELECT url, ann_type, title, p_time FROM announcements WHERE lang = 'en-US' AND ann_type IN "
                        "('announcements-new-listings', 'announcements-delistings')").df()
    finally:
        con.close()
    rows = []
    for r in a.itertuples(index=False):
        kind, toks = classify(r.ann_type, r.title or "")
        for t in dict.fromkeys(toks):
            rows.append({"url": r.url, "type": kind, "token": t, "inst_id": f"{t}-USDT-SWAP", "title": r.title, "p_time": int(r.p_time)})
    return pd.DataFrame(rows).dropna(subset=["type"]) if rows else pd.DataFrame()


def existing_study(panel: CP.Panel, ev: pd.DataFrame, draws: int = 200, seed: int = 3) -> tuple[pd.DataFrame, dict[str, Any]]:
    ev = ev[ev["type"].isin(EXISTING) & ev["inst_id"].isin(panel.ci)].copy()
    ev = ev[[panel.has(panel.ci[i], p - 7 * D) for i, p in zip(ev["inst_id"], ev["p_time"])]]
    ev = ev.drop_duplicates(["type", "inst_id", "p_time"]).sort_values("p_time").reset_index(drop=True)
    evx = (ev["p_time"].to_numpy(np.int64) - 7 * D, ev["p_time"].to_numpy(np.int64) + 7 * D,
           np.array([panel.ci[i] for i in ev["inst_id"]], dtype=np.int64))
    rows = []
    for r in ev.itertuples(index=False):
        k = panel.ci[r.inst_id]
        d = {"type": r.type, "token": r.token, "inst_id": r.inst_id, "title": r.title, "p_time": r.p_time}
        for w, (a, b) in WIN.items():
            a, b = r.p_time + a, r.p_time + b
            if b > panel.last_ts:
                continue
            busy = CP.busy_mask(panel, evx, a, b, pad=0)
            busy[k] = False
            x = CP.abnormal(panel, k, a, b, busy)
            d[f"{w}_raw"], d[f"{w}_ab"] = x["raw"], x["ab"]
        rows.append(d)
    R = pd.DataFrame(rows)
    rng = np.random.default_rng(seed)
    summ: dict[str, Any] = {}
    for t in EXISTING:
        g = R[R["type"] == t] if len(R) else R
        s = {w: {"raw": CP.stats(g.get(f"{w}_raw", pd.Series(dtype=float))), "ab": CP.stats(g.get(f"{w}_ab", pd.Series(dtype=float)))} for w in WIN}
        for w in ("d1", "d3"):
            col = f"{w}_ab"
            if col not in g or g[col].notna().sum() < 5:
                continue
            sub = g[g[col].notna()]
            ln = WIN[w][1] - WIN[w][0]
            means = []
            for _ in range(draws):
                vals = []
                for r in sub.itertuples(index=False):
                    k = panel.ci[r.inst_id]
                    lo, hi = int(panel.first[k]) + 7 * D, panel.last_ts - ln
                    if hi <= lo:
                        continue
                    a = int(rng.integers(lo // H, hi // H)) * H
                    if abs(a - r.p_time) < 14 * D:
                        continue
                    busy = CP.busy_mask(panel, evx, a, a + ln, pad=0)
                    busy[k] = False
                    v = CP.abnormal(panel, k, a, a + ln, busy)["ab"]
                    if np.isfinite(v):
                        vals.append(v)
                if vals:
                    means.append(np.mean(vals))
            pm = np.array(means)
            act = float(sub[col].mean())
            if len(pm):
                s[w]["placebo"] = {"mean": float(pm.mean()), "p": float(np.mean(pm <= act) if act < 0 else np.mean(pm >= act))}
        summ[t] = s
    return R, summ


def new_listing_study(panel: CP.Panel, ev: pd.DataFrame, list_time: dict[str, int]) -> tuple[pd.DataFrame, dict[str, Any]]:
    ev = ev[ev["type"].isin(("perp_new", "tradfi_new")) & ev["inst_id"].isin(panel.ci)].drop_duplicates(["inst_id"]).copy()
    rows = []
    for r in ev.itertuples(index=False):
        k = panel.ci[r.inst_id]
        t0 = int(panel.first[k]) + H                    # first hourly close of the swap
        lt = list_time.get(r.inst_id)
        if lt is not None and abs(lt - t0) > 2 * D:     # data starts long after listing: not a fresh listing in this database
            continue
        if t0 < r.p_time - D or t0 > r.p_time + 10 * D:
            continue
        d = {"type": r.type, "token": r.token, "inst_id": r.inst_id, "title": r.title, "p_time": r.p_time, "open_ts": t0,
             "lead_h": (t0 - r.p_time) / H}
        for w, ln in NEW_WIN.items():
            if t0 + ln > panel.last_ts:
                continue
            rr = panel.lret(t0, t0 + ln)
            if rr is None or not np.isfinite(rr[k]):
                continue
            alts = (~panel.excl) & np.isfinite(rr) & (panel.ages(t0) >= 30)
            d[f"{w}_raw"] = float(rr[k])
            d[f"{w}_vs_alts"] = float(rr[k] - np.median(rr[alts])) if alts.sum() >= 5 else np.nan
        rows.append(d)
    R = pd.DataFrame(rows)
    summ = {t: {w: {"raw": CP.stats(R[R["type"] == t].get(f"{w}_raw", pd.Series(dtype=float))),
                    "vs_alts": CP.stats(R[R["type"] == t].get(f"{w}_vs_alts", pd.Series(dtype=float)))} for w in NEW_WIN}
            for t in ("perp_new", "tradfi_new")} if len(R) else {}
    return R, summ


def evaluate(cfg: Config, draws: int = 200) -> dict[str, Any]:
    t0 = time.monotonic()
    ev = load_events(cfg)
    if ev.empty:
        raise RuntimeError("沒有公告資料（先執行 python -m okxrev.announce --backfill）")
    panel = CP.hourly_panel(cfg, int(ev["p_time"].min()) - 30 * D)
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        lt = {k: int(v) for k, v in con.execute("SELECT inst_id, list_time FROM instruments WHERE list_time IS NOT NULL").fetchall()}
    rx, sx = existing_study(panel, ev, draws)
    from okxrev import replay
    sb: dict[str, Any] = {}
    op = replay.old_db_path(cfg)
    if op.exists():
        po = CP.hourly_panel(cfg, int(ev["p_time"].min()) - 30 * D, path=op)
        rb, sb = existing_study(po, ev[ev["p_time"] < int(po.idx[-1])], draws)
        rb["period"] = "old"
    rn, sn = new_listing_study(panel, ev, lt)
    counts = ev.groupby("type")["token"].count().to_dict()
    res = {"generated_at": ms_to_str(db.now_ms()), "elapsed_sec": round(time.monotonic() - t0, 1), "data_from": ms_to_str(int(panel.idx[0]) if len(panel.idx) else 0),
           "types": TYPE_LABEL, "announced": counts, "existing": sx, "existing_old": sb, "new": sn,
           "events": pd.concat([rx, rn], ignore_index=True).replace({np.nan: None}).to_dict("records"),
           "caveats": ["K 線只有 2025-10 之後（主資料庫），更早的公告對不上價格", "對「已有合約」的類型：超額報酬 = 代幣 − 同齡、同期沒有同類公告的幣的中位數",
                       "新上市：從合約第一個小時收盤算起，沒有對照組，只和山寨幣中位數比；已下架的新幣沒有資料（倖存者偏差）"]}
    out = cfg.paths.results / "listings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, default=regime._jd), encoding="utf-8")
    return res


def load(cfg: Config) -> dict[str, Any] | None:
    p = cfg.paths.results / "listings.json"
    try:
        return json.loads(p.read_text(encoding="utf-8").replace("NaN", "null")) if p.exists() else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.listings")
    ap.add_argument("--config", default=None)
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--draws", type=int, default=200)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "listings")
    res = evaluate(cfg, args.draws) if args.evaluate else load(cfg)
    if not res:
        print("no results yet: python -m okxrev.listings --evaluate")
        return 0
    pc = lambda s: "-" if not s or not s.get("n") else f"{np.expm1(s['mean']) * 100:+6.2f}% (n={s['n']:3d}, t={s.get('t', float('nan')):+.1f})"
    print("announced:", res["announced"])
    for t, s in res["existing"].items():
        print(f"\n== {TYPE_LABEL[t]}")
        for w in WIN:
            pl = s[w].get("placebo") or {}
            print(f"  {w:5s} vs 同齡 {pc(s[w]['ab'])}  raw {pc(s[w]['raw'])}" + (f"  placebo p={pl['p']:.3f}" if pl else ""))
    for t, s in (res.get("existing_old") or {}).items():
        if s["d1"]["ab"].get("n"):
            print(f"\n== 舊資料期間（2024-09～2025-09） {TYPE_LABEL[t]}")
            for w in WIN:
                pl = s[w].get("placebo") or {}
                print(f"  {w:5s} vs 同齡 {pc(s[w]['ab'])}  raw {pc(s[w]['raw'])}" + (f"  placebo p={pl['p']:.3f}" if pl else ""))
    for t, s in res["new"].items():
        print(f"\n== {TYPE_LABEL[t]}")
        for w in NEW_WIN:
            print(f"  {w:5s} raw {pc(s[w]['raw'])}  vs 山寨幣中位數 {pc(s[w]['vs_alts'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
