from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

from okxrev.detector import pump_mask, red_mask

FEATURE_COLS = ["r1h", "r6h", "r24h", "vol_ratio", "vmax_1h", "pump", "red_recent", "pump_red_recent",
                "range_pos", "btc_regime", "btc_ret_1h"]


def bar_features(g: pd.DataFrame, det_cfg: dict[str, Any]) -> pd.DataFrame:
    """Features known at the close of bar t (decision at close t, entry at open t+1)."""
    c, h, lo = g["close"], g["high"], g["low"]
    f = pd.DataFrame(index=g.index)
    f["r1h"] = c / c.shift(12) - 1
    f["r6h"] = c / c.shift(72) - 1
    f["r24h"] = c / c.shift(288) - 1
    f["vol_ratio"] = g["vol_ratio"]
    f["vmax_1h"] = g["vol_ratio"].rolling(12, min_periods=1).max()
    red = red_mask(g, det_cfg)
    pump = pump_mask(g, det_cfg)
    f["pump"] = pump
    f["red_recent"] = pd.Series(red.astype(float), index=g.index).rolling(12, min_periods=1).max() > 0
    f["pump_red_recent"] = pd.Series((red & pump).astype(float), index=g.index).rolling(288, min_periods=1).max() > 0
    hi24 = h.rolling(288, min_periods=72).max()
    lo24 = lo.rolling(288, min_periods=72).min()
    f["range_pos"] = (c - lo24) / (hi24 - lo24)
    f["btc_regime"] = g["btc_regime"] if "btc_regime" in g else "unknown"
    f["btc_ret_1h"] = g["btc_ret_1h"] if "btc_ret_1h" in g else np.nan
    return f


Cond = Callable[[dict[str, np.ndarray]], np.ndarray]


def _num(k: str, op: str, v: float) -> Cond:
    def fn(f: dict[str, np.ndarray]) -> np.ndarray:
        x = np.asarray(f[k], dtype=float)
        with np.errstate(invalid="ignore"):
            return (x >= v) if op == ">=" else (x <= v)
    return fn


def _flag(k: str) -> Cond:
    return lambda f: np.asarray(f[k], dtype=bool)


def _eq(k: str, v: str) -> Cond:
    return lambda f: np.asarray(f[k]) == v


CONDITIONS: dict[str, tuple[str, str, Cond]] = {
    "r24h>=+30%": ("r24h", "前24h漲幅 ≥ +30%", _num("r24h", ">=", 0.30)),
    "r24h>=+10%": ("r24h", "前24h漲幅 ≥ +10%", _num("r24h", ">=", 0.10)),
    "r24h<=-10%": ("r24h", "前24h跌幅 ≤ -10%", _num("r24h", "<=", -0.10)),
    "r24h<=-30%": ("r24h", "前24h跌幅 ≤ -30%", _num("r24h", "<=", -0.30)),
    "r1h>=+5%": ("r1h", "前1h漲幅 ≥ +5%", _num("r1h", ">=", 0.05)),
    "r1h<=-5%": ("r1h", "前1h跌幅 ≤ -5%", _num("r1h", "<=", -0.05)),
    "vol>=3x": ("vol", "近1h出現 ≥3 倍量", _num("vmax_1h", ">=", 3.0)),
    "red_k_1h": ("red", "近1h出現巨大紅K", _flag("red_recent")),
    "pump": ("pump", "處於 pump 狀態", _flag("pump")),
    "pump_red_24h": ("pump", "近24h出現 pump_red 訊號", _flag("pump_red_recent")),
    "range_top20": ("range", "位於24h區間頂部20%", _num("range_pos", ">=", 0.8)),
    "range_bot20": ("range", "位於24h區間底部20%", _num("range_pos", "<=", 0.2)),
    "btc_strong": ("btc", "BTC 強勢", _eq("btc_regime", "strong")),
    "btc_neutral": ("btc", "BTC 中性", _eq("btc_regime", "neutral")),
    "btc_weak": ("btc", "BTC 弱勢", _eq("btc_regime", "weak")),
}


def as_arrays(f: pd.DataFrame) -> dict[str, np.ndarray]:
    return {c: f[c].to_numpy() for c in f.columns}


def rule_mask(f: dict[str, np.ndarray], conds: tuple[str, ...]) -> np.ndarray:
    n = len(next(iter(f.values())))
    m = np.ones(n, dtype=bool)
    for c in conds:
        m &= CONDITIONS[c][2](f)
    return m


def rule_label(side: str, conds: tuple[str, ...]) -> str:
    s = "做多" if side == "long" else "做空"
    return s + "：" + (" + ".join(CONDITIONS[c][1] for c in conds) if conds else "任何時候")
