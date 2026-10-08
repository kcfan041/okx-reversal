from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

BAR_MS = 5 * 60 * 1000
MODES = ("red_only", "pump_red", "pump_red_btcweak")

BTC_COLS = ["btc_close", "btc_ret_1h", "btc_ret_4h", "btc_ema", "btc_below_ema", "btc_regime"]


def to_grid(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.set_index(pd.Index([], name="ts", dtype="int64"))
    df = df.sort_values("ts").drop_duplicates("ts", keep="last")
    lo, hi = int(df["ts"].iloc[0]), int(df["ts"].iloc[-1])
    grid = pd.Index(np.arange(lo, hi + BAR_MS, BAR_MS, dtype="int64"), name="ts")
    return df.set_index("ts").reindex(grid)


def btc_features(btc: pd.DataFrame, regime_cfg: dict[str, Any]) -> pd.DataFrame:
    g = to_grid(btc)
    close = g["close"].ffill(limit=3)
    out = pd.DataFrame(index=g.index)
    out["btc_close"] = close
    out["btc_ret_1h"] = close / close.shift(12) - 1
    out["btc_ret_4h"] = close / close.shift(48) - 1
    ema = close.ewm(span=int(regime_cfg["ema_bars"]), adjust=False, ignore_na=True).mean()
    out["btc_ema"] = ema.where(close.notna())
    below = close < out["btc_ema"]
    out["btc_below_ema"] = below.where(close.notna() & out["btc_ema"].notna())
    r4 = out["btc_ret_4h"]
    regime = np.where(
        r4.isna(), "unknown",
        np.where(r4 <= regime_cfg["ret_4h_weak"], "weak",
                 np.where(r4 >= regime_cfg["ret_4h_strong"], "strong", "neutral")),
    )
    out["btc_regime"] = regime
    return out


def _window_ret(num: pd.Series, den: pd.Series) -> pd.Series:
    return num / den - 1


def prepare(
    df: pd.DataFrame,
    det_cfg: dict[str, Any],
    btc_feat: pd.DataFrame | None = None,
    list_time: int | None = None,
) -> pd.DataFrame:
    g = to_grid(df)
    if g.empty:
        return g
    pump = det_cfg["pump"]
    n_ma = int(det_cfg["vol_ma_bars"])
    vlb = int(pump["vol_lookback_bars"])
    measure = pump["measure"]

    close, open_, low = g["close"], g["open"], g["low"]
    vq = g["vol_quote"]
    g["ret_1"] = close / close.shift(1) - 1
    g["body_ret"] = close / open_ - 1
    g["vol_ma"] = vq.shift(1).rolling(n_ma, min_periods=n_ma).mean()
    g["vol_ratio"] = vq / g["vol_ma"]

    prev_close = close.shift(1)
    full = lambda s, n: s.rolling(n, min_periods=n)

    def pre_ret(bars: int) -> pd.Series:
        span = bars + 1
        cnt = full(prev_close.notna().astype(float), span).sum()
        if measure == "low_to_close":
            den = full(low.shift(1), span).min()
        else:
            den = close.shift(bars + 1)
        r = _window_ret(prev_close, den)
        return r.where(cnt == span)

    g["ret_1h_pre"] = pre_ret(12)
    g["ret_6h_pre"] = pre_ret(72)
    g["ret_24h_pre"] = pre_ret(288)
    g["is_new_listing"] = False

    pos = np.arange(len(g))
    if pump.get("allow_new_listing", False) and list_time is not None:
        first_ts = int(g.index[0])
        if first_ts <= int(list_time) + 288 * BAR_MS:
            min_bars = int(pump["new_listing_min_bars"])
            mask = (pos < 289) & (pos >= min_bars)
            if mask.any():
                complete = prev_close.notna().astype(float).cumsum().to_numpy() == pos
                if measure == "low_to_close":
                    den = low.shift(1).expanding().min()
                else:
                    den = pd.Series(float(open_.iloc[0]), index=g.index)
                alt = _window_ret(prev_close, den)
                use = mask & complete & np.isnan(g["ret_24h_pre"].to_numpy())
                g.loc[use, "ret_24h_pre"] = alt[use]
                g.loc[use, "is_new_listing"] = True

    g["pump_vol_ratio_max"] = g["vol_ratio"].shift(1).rolling(vlb, min_periods=vlb).max()

    if btc_feat is not None and not btc_feat.empty:
        b = btc_feat.reindex(g.index)
        for c in BTC_COLS:
            g[c] = b[c]
        g["btc_regime"] = g["btc_regime"].fillna("unknown")
    else:
        for c in BTC_COLS:
            g[c] = np.nan
        g["btc_regime"] = "unknown"
    return g


def pump_mask(g: pd.DataFrame, det_cfg: dict[str, Any]) -> np.ndarray:
    p = det_cfg["pump"]
    r24 = g["ret_24h_pre"].to_numpy()
    r6 = g["ret_6h_pre"].to_numpy()
    vmax = g["pump_vol_ratio_max"].to_numpy()
    with np.errstate(invalid="ignore"):
        price = (r24 > p["ret_24h_min"]) | (r6 > p["ret_6h_min"])
        vol = vmax >= p["vol_ratio_min"]
    return price & vol


def red_mask(g: pd.DataFrame, det_cfg: dict[str, Any], ret_max: float | None = None, vol_ratio_min: float | None = None) -> np.ndarray:
    rc = det_cfg["red_candle"]
    ret_max = rc["ret_max"] if ret_max is None else ret_max
    vol_ratio_min = rc["vol_ratio_min"] if vol_ratio_min is None else vol_ratio_min
    col = "ret_1" if rc["ret_basis"] == "prev_close" else "body_ret"
    r = g[col].to_numpy()
    v = g["vol_ratio"].to_numpy()
    with np.errstate(invalid="ignore"):
        return (r <= ret_max) & (v >= vol_ratio_min)


def apply_cooldown(mask: np.ndarray, cooldown: int) -> np.ndarray:
    out: list[int] = []
    last = -10**18
    for i in np.flatnonzero(mask):
        if i - last > cooldown:
            out.append(int(i))
            last = i
    return np.asarray(out, dtype=np.int64)


def mode_masks(g: pd.DataFrame, det_cfg: dict[str, Any], red: np.ndarray | None = None, pump: np.ndarray | None = None) -> dict[str, np.ndarray]:
    red = red_mask(g, det_cfg) if red is None else red
    pump = pump_mask(g, det_cfg) if pump is None else pump
    weak = (g["btc_regime"] == "weak").to_numpy()
    return {
        "red_only": red,
        "pump_red": pump & red,
        "pump_red_btcweak": pump & red & weak,
    }


def detect(g: pd.DataFrame, det_cfg: dict[str, Any]) -> dict[str, np.ndarray]:
    if g.empty:
        return {m: np.asarray([], dtype=np.int64) for m in MODES}
    cd = int(det_cfg["cooldown_bars"])
    return {m: apply_cooldown(mask, cd) for m, mask in mode_masks(g, det_cfg).items()}
