from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

REQUIRED: dict[str, list[str]] = {
    "paths": ["db", "logs", "results", "lock"],
    "okx": ["base_url", "timeout_sec", "max_requests_per_sec", "max_retries", "backoff_base_sec"],
    "universe": ["inst_type", "quote_suffix", "btc_inst", "exclude", "bar"],
    "sync": ["run_budget_sec", "backfill_start", "backfill_layers_days", "smoke_insts", "smoke_days"],
    "detector": ["vol_ma_bars", "pump", "red_candle", "cooldown_bars"],
    "metrics": ["entry", "horizons", "excursion_bars", "big_drop_threshold", "fee_per_side", "slippage_per_side"],
    "btc_regime": ["ret_4h_weak", "ret_4h_strong", "ema_bars"],
    "backtest": ["sweep", "baseline", "bootstrap"],
    "charts": ["enabled", "max_charts", "pre_hours", "post_hours", "dpi"],
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Paths:
    root: Path
    db: Path
    logs: Path
    results: Path
    lock: Path

    @property
    def charts(self) -> Path:
        return self.results / "charts"


@dataclass
class Config:
    raw: dict[str, Any]
    paths: Paths
    source: Path | None = None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def okx(self) -> dict[str, Any]:
        return self.raw["okx"]

    @property
    def universe(self) -> dict[str, Any]:
        return self.raw["universe"]

    @property
    def sync(self) -> dict[str, Any]:
        return self.raw["sync"]

    @property
    def detector(self) -> dict[str, Any]:
        return self.raw["detector"]

    @property
    def metrics(self) -> dict[str, Any]:
        return self.raw["metrics"]

    @property
    def btc_regime(self) -> dict[str, Any]:
        return self.raw["btc_regime"]

    @property
    def backtest(self) -> dict[str, Any]:
        return self.raw["backtest"]

    @property
    def charts(self) -> dict[str, Any]:
        return self.raw["charts"]

    @property
    def account(self) -> dict[str, Any]:
        return self.raw.get("account") or {}

    @property
    def account_db(self) -> Path:
        return _resolve(self.account.get("db", "data/account.duckdb"), self.paths.root)

    @property
    def paper(self) -> dict[str, Any]:
        return self.raw.get("paper") or {}

    @property
    def paper_db(self) -> Path:
        return _resolve(self.paper.get("db", "data/paper.duckdb"), self.paths.root)

    @property
    def models_dir(self) -> Path:
        return _resolve(self.paper.get("models_dir", "data/models"), self.paths.root)

    @property
    def backfill_start_ms(self) -> int:
        return parse_utc_ms(self.sync["backfill_start"])

    def copy_with(self, overrides: dict[str, Any]) -> "Config":
        raw = copy.deepcopy(self.raw)
        _deep_update(raw, overrides)
        return Config(raw=raw, paths=self.paths, source=self.source)


def _deep_update(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v


def parse_utc_ms(value: str | int | float | datetime) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_str(ms: int | float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(fmt)


def _resolve(p: str | Path, root: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (root / path).resolve()


def validate(raw: dict[str, Any]) -> None:
    missing: list[str] = []
    for section, keys in REQUIRED.items():
        sec = raw.get(section)
        if not isinstance(sec, dict):
            missing.append(section)
            continue
        missing.extend(f"{section}.{k}" for k in keys if k not in sec)
    if missing:
        raise ConfigError("config missing keys: " + ", ".join(missing))
    if raw["metrics"]["entry"] not in ("next_open", "event_close"):
        raise ConfigError("metrics.entry must be next_open or event_close")
    if raw["detector"]["red_candle"]["ret_basis"] not in ("prev_close", "open"):
        raise ConfigError("detector.red_candle.ret_basis must be prev_close or open")
    if raw["detector"]["pump"]["measure"] not in ("close_to_close", "low_to_close"):
        raise ConfigError("detector.pump.measure must be close_to_close or low_to_close")


def read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


ENV_KEYS = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")


def load_env(keys: tuple[str, ...], root: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(read_dotenv((root or REPO_ROOT) / ".env"))
    env.update({k: v for k, v in (os.environ if environ is None else environ).items() if k in keys and v})
    return {k: env[k].strip() for k in keys if env.get(k, "").strip()}


def load_credentials(root: Path | None = None, environ: dict[str, str] | None = None) -> tuple[str, str, str] | None:
    env = dict(read_dotenv((root or REPO_ROOT) / ".env"))
    env.update({k: v for k, v in (os.environ if environ is None else environ).items() if k in ENV_KEYS and v})
    vals = tuple(env.get(k, "").strip() for k in ENV_KEYS)
    if not all(vals):
        return None
    return vals  # type: ignore[return-value]


def load_config(path: str | Path | None = None, root: Path | None = None) -> Config:
    root = root or REPO_ROOT
    src = Path(path) if path else DEFAULT_CONFIG
    if not src.is_absolute():
        src = (root / src).resolve()
    with open(src, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    validate(raw)
    from okxrev import settings as _settings
    base = copy.deepcopy(raw)
    if not os.environ.get("OKXREV_NO_SETTINGS"):
        raw = _settings.apply(raw, _settings.load_overrides(root))
    p = raw["paths"]
    paths = Paths(
        root=root,
        db=_resolve(p["db"], root),
        logs=_resolve(p["logs"], root),
        results=_resolve(p["results"], root),
        lock=_resolve(p["lock"], root),
    )
    cfg = Config(raw=raw, paths=paths, source=src)
    cfg._cache["base_raw"] = base
    return cfg
