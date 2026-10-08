from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from okxrev.config import ms_to_str

FEATURE_COLS = [
    "is_new_listing", "ret_1h_pre", "ret_6h_pre", "ret_24h_pre", "pump_vol_ratio_max",
    "red_ret_prev_close", "red_ret_body", "red_vol_ratio",
    "btc_ret_1h", "btc_ret_4h", "btc_below_ema", "btc_regime",
]


def horizon_names(m_cfg: dict[str, Any]) -> list[str]:
    return list(m_cfg["horizons"].keys())


def event_metrics(g: pd.DataFrame, idx: np.ndarray, m_cfg: dict[str, Any]) -> pd.DataFrame:
    idx = np.asarray(idx, dtype=np.int64)
    n = len(g)
    o = g["open"].to_numpy(float)
    h = g["high"].to_numpy(float)
    lo = g["low"].to_numpy(float)
    c = g["close"].to_numpy(float)
    ts = g.index.to_numpy(np.int64)
    horizons: dict[str, int] = {k: int(v) for k, v in m_cfg["horizons"].items()}
    ex = int(m_cfg["excursion_bars"])
    cost = 2 * (float(m_cfg["fee_per_side"]) + float(m_cfg["slippage_per_side"]))
    next_open = m_cfg["entry"] == "next_open"

    rows: list[dict[str, Any]] = []
    for t in idx:
        r: dict[str, Any] = {"event_ts": int(ts[t])}
        if next_open:
            entry = o[t + 1] if t + 1 < n else np.nan
        else:
            entry = c[t]
        r["entry_price"] = entry
        r["incomplete"] = bool(np.isnan(entry))
        for name, k in horizons.items():
            j = t + k
            r[f"fwd_ret_{name}"] = (c[j] / entry - 1) if (j < n and not np.isnan(entry)) else np.nan
        end = t + ex
        if np.isnan(entry) or end >= n:
            r.update(mae_24h=np.nan, mfe_24h=np.nan, mae_before_mfe=np.nan, bars_to_mfe=np.nan, new_high_24h=np.nan)
        else:
            wh = h[t + 1:end + 1]
            wl = lo[t + 1:end + 1]
            if np.isnan(wh).any():
                r["incomplete"] = True
            if np.all(np.isnan(wh)):
                r.update(mae_24h=np.nan, mfe_24h=np.nan, mae_before_mfe=np.nan, bars_to_mfe=np.nan, new_high_24h=np.nan)
            else:
                i_max = int(np.nanargmax(wh))
                i_min = int(np.nanargmin(wl))
                r["mae_24h"] = wh[i_max] / entry - 1
                r["mfe_24h"] = wl[i_min] / entry - 1
                r["mae_before_mfe"] = bool(i_max < i_min)
                r["bars_to_mfe"] = i_min + 1
                pre_lo = max(0, t - 288)
                pre_high = np.nanmax(h[pre_lo:t + 1])
                r["new_high_24h"] = bool(np.nanmax(wh) > pre_high)
        for name in horizons:
            fr = r[f"fwd_ret_{name}"]
            r[f"short_pnl_net_{name}"] = -fr - cost if not np.isnan(fr) else np.nan
        rows.append(r)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    feat = g.iloc[idx]
    out["is_new_listing"] = feat["is_new_listing"].to_numpy(bool)
    for col in ("ret_1h_pre", "ret_6h_pre", "ret_24h_pre", "pump_vol_ratio_max", "btc_ret_1h", "btc_ret_4h", "btc_below_ema", "btc_regime"):
        out[col] = feat[col].to_numpy()
    out["red_ret_prev_close"] = feat["ret_1"].to_numpy()
    out["red_ret_body"] = feat["body_ret"].to_numpy()
    out["red_vol_ratio"] = feat["vol_ratio"].to_numpy()
    out["event_time_utc"] = [ms_to_str(x) for x in out["event_ts"]]
    return out


def bootstrap_ci(x: np.ndarray, iterations: int, seed: int, alpha: float = 0.05) -> tuple[float, float]:
    x = x[~np.isnan(x)]
    if len(x) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    means = np.empty(iterations)
    chunk = max(1, 2_000_000 // len(x))
    for s in range(0, iterations, chunk):
        e = min(iterations, s + chunk)
        means[s:e] = x[rng.integers(0, len(x), size=(e - s, len(x)))].mean(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def _f(v: Any) -> float | None:
    if v is None:
        return None
    v = float(v)
    return None if np.isnan(v) else round(v, 6)


def horizon_stats(ev: pd.DataFrame, name: str, m_cfg: dict[str, Any], boot: dict[str, Any] | None) -> dict[str, Any]:
    col = f"fwd_ret_{name}"
    x = ev[col].to_numpy(float) if col in ev else np.array([])
    valid = ev[~np.isnan(x)] if len(x) else ev.iloc[0:0]
    x = x[~np.isnan(x)]
    out: dict[str, Any] = {
        "n": int(len(x)),
        "n_insts": int(valid["inst_id"].nunique()) if "inst_id" in valid and len(valid) else 0,
        "n_days": int(pd.to_datetime(valid["event_ts"], unit="ms").dt.date.nunique()) if len(valid) else 0,
    }
    if len(x) == 0:
        return out
    out.update(
        mean=_f(x.mean()), median=_f(np.median(x)), std=_f(x.std(ddof=1)) if len(x) > 1 else None,
        bear_rate=_f((x < 0).mean()),
        short_pnl_net_mean=_f(valid[f"short_pnl_net_{name}"].mean()),
    )
    if boot:
        lo, hi = bootstrap_ci(x, int(boot["iterations"]), int(boot["seed"]))
        out["mean_ci95"] = [_f(lo), _f(hi)]
    return out


def summarize(ev: pd.DataFrame, m_cfg: dict[str, Any], boot: dict[str, Any] | None, by_group: bool = True) -> dict[str, Any]:
    ev = ev[~ev["incomplete"].astype(bool)] if "incomplete" in ev and len(ev) else ev
    res: dict[str, Any] = {"n_events": int(len(ev))}
    if len(ev) == 0:
        return res
    res["n_insts"] = int(ev["inst_id"].nunique()) if "inst_id" in ev else 0
    res["n_days"] = int(pd.to_datetime(ev["event_ts"], unit="ms").dt.date.nunique())
    res["horizons"] = {name: horizon_stats(ev, name, m_cfg, boot) for name in horizon_names(m_cfg)}
    r24 = ev["fwd_ret_24h"].dropna() if "fwd_ret_24h" in ev else pd.Series(dtype=float)
    res["big_drop_rate_24h"] = _f((r24 <= m_cfg["big_drop_threshold"]).mean()) if len(r24) else None
    nh = ev["new_high_24h"].dropna().astype(bool)
    res["new_high_rate_24h"] = _f(nh.mean()) if len(nh) else None
    mae = ev["mae_24h"].dropna()
    mfe = ev["mfe_24h"].dropna()
    if len(mae):
        res.update(mae_mean=_f(mae.mean()), mae_median=_f(mae.median()), mae_p90=_f(mae.quantile(0.9)))
    if len(mfe):
        res.update(mfe_mean=_f(mfe.mean()), mfe_median=_f(mfe.median()))
    mbm = ev["mae_before_mfe"].dropna().astype(bool)
    res["mae_before_mfe_rate"] = _f(mbm.mean()) if len(mbm) else None
    if by_group:
        res["by_btc_regime"] = {}
        for reg in ("strong", "neutral", "weak", "unknown"):
            sub = ev[ev["btc_regime"] == reg]
            if len(sub):
                res["by_btc_regime"][reg] = summarize(sub, m_cfg, None, by_group=False)
        months = pd.to_datetime(ev["event_ts"], unit="ms").dt.strftime("%Y-%m")
        res["by_month"] = {
            m: {"n": int(len(sub)), "fwd_ret_24h_mean": _f(sub["fwd_ret_24h"].mean()) if sub["fwd_ret_24h"].notna().any() else None}
            for m, sub in ev.groupby(months)
        }
    return res
