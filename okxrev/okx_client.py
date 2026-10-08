from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
import requests

log = logging.getLogger("okxrev.okx")

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote"]
CODE_RATE_LIMIT = {"50011", "50061"}
CODE_INST_NOT_FOUND = {"51001"}


class OkxError(Exception):
    def __init__(self, code: str, msg: str, path: str = ""):
        super().__init__(f"OKX error {code}: {msg} ({path})")
        self.code = code
        self.msg = msg
        self.path = path


class InstrumentNotFound(OkxError):
    pass


class RetryExhausted(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class RateLimiter:
    def __init__(
        self,
        rate_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        name: str = "",
        min_ratio: float = 0.5,
        throttle_after: int = 3,
        recover_after_sec: float = 60.0,
    ):
        self.name = name
        self.target = float(rate_per_sec)
        self.rate = self.target
        self.min_rate = self.target * min_ratio
        self.throttle_after = throttle_after
        self.recover_after_sec = recover_after_sec
        self._next = 0.0
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._consec = 0
        self._last_limited = -1e18
        self._last_adjust = 0.0
        self.n_limited = 0

    @property
    def interval(self) -> float:
        return 1.0 / self.rate

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            self._maybe_recover(now)
            wait = self._next - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._next = max(now, self._next) + self.interval

    def _maybe_recover(self, now: float) -> None:
        if self.rate >= self.target:
            return
        if now - self._last_limited >= self.recover_after_sec and now - self._last_adjust >= self.recover_after_sec:
            old = self.rate
            self.rate = min(self.target, self.rate * 1.1)
            self._last_adjust = now
            log.info("rate limiter %s: recover %.2f -> %.2f req/s", self.name, old, self.rate)

    def on_limited(self) -> None:
        with self._lock:
            now = self._clock()
            self.n_limited += 1
            self._consec += 1
            self._last_limited = now
            if self._consec >= self.throttle_after and self.rate > self.min_rate:
                old = self.rate
                self.rate = max(self.min_rate, self.rate * 0.8)
                self._consec = 0
                self._last_adjust = now
                log.warning("rate limiter %s: throttled %.2f -> %.2f req/s", self.name, old, self.rate)

    def on_success(self) -> None:
        self._consec = 0


@dataclass
class Instrument:
    inst_id: str
    uly: str
    ct_val: float | None
    list_time: int | None
    exp_time: int | None
    state: str


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return int(v)


def parse_candles(rows: list[list[str]], confirmed_only: bool = True) -> pd.DataFrame:
    recs = []
    for r in rows:
        if confirmed_only and len(r) > 8 and r[8] != "1":
            continue
        recs.append((
            int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
            _to_float(r[5]), _to_float(r[6]), _to_float(r[7]),
        ))
    df = pd.DataFrame.from_records(recs, columns=CANDLE_COLUMNS)
    if not df.empty:
        df = df.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    df["ts"] = df["ts"].astype("int64")
    return df


class OkxClient:
    def __init__(self, okx_cfg: dict[str, Any], deadline: float | None = None, sleep: Callable[[float], None] = time.sleep):
        self.base_url = okx_cfg["base_url"].rstrip("/")
        self.timeout = float(okx_cfg["timeout_sec"])
        self.max_retries = int(okx_cfg["max_retries"])
        self.backoff_base = float(okx_cfg["backoff_base_sec"])
        default_rate = float(okx_cfg["max_requests_per_sec"])
        self.limiters = {
            "history": RateLimiter(okx_cfg.get("history_requests_per_sec", default_rate), sleep=sleep, name="history"),
            "market": RateLimiter(okx_cfg.get("market_requests_per_sec", default_rate), sleep=sleep, name="market"),
            "default": RateLimiter(default_rate, sleep=sleep, name="default"),
        }
        self.history_limit = int(okx_cfg.get("history_limit", 100))
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "okx-reversal/0.1"
        proxy = okx_cfg.get("proxy")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.deadline = deadline
        self._sleep = sleep
        self.n_requests = 0
        self.n_retries = 0

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "OkxClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _check_budget(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise BudgetExceeded()

    @property
    def limiter(self) -> RateLimiter:
        return self.limiters["default"]

    @limiter.setter
    def limiter(self, value: RateLimiter) -> None:
        for k in self.limiters:
            self.limiters[k] = value

    def limiter_for(self, path: str) -> RateLimiter:
        if path.endswith("/history-candles"):
            return self.limiters["history"]
        if path.endswith("/market/candles"):
            return self.limiters["market"]
        return self.limiters["default"]

    @property
    def n_limited(self) -> int:
        return sum(lim.n_limited for lim in {id(v): v for v in self.limiters.values()}.values())

    def get(self, path: str, params: dict[str, Any]) -> list[Any]:
        last_err: str = ""
        limiter = self.limiter_for(path)
        for attempt in range(self.max_retries + 1):
            self._check_budget()
            limiter.acquire()
            self.n_requests += 1
            retry = False
            limited = False
            try:
                resp = self.session.get(self.base_url + path, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                retry = True
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}"
                    retry = True
                    limited = resp.status_code == 429
                else:
                    try:
                        body = resp.json()
                    except ValueError:
                        last_err = f"HTTP {resp.status_code} non-JSON"
                        if resp.status_code >= 400:
                            raise OkxError(str(resp.status_code), resp.text[:200], path)
                        retry = True
                    else:
                        code = str(body.get("code", ""))
                        if code == "0":
                            limiter.on_success()
                            return body.get("data", [])
                        msg = str(body.get("msg", ""))
                        if code in CODE_RATE_LIMIT:
                            last_err = f"code {code} {msg}"
                            retry = True
                            limited = True
                        elif code in CODE_INST_NOT_FOUND:
                            raise InstrumentNotFound(code, msg, path)
                        else:
                            raise OkxError(code, msg, path)
            if limited:
                limiter.on_limited()
            if retry and attempt < self.max_retries:
                delay = self.backoff_base * (2 ** attempt)
                self.n_retries += 1
                log.debug("retry %s %s in %.1fs: %s", path, params, delay, last_err)
                if self.deadline is not None and time.monotonic() + delay >= self.deadline:
                    raise BudgetExceeded()
                self._sleep(delay)
        raise RetryExhausted(f"{path} {params}: {last_err}")

    def instruments(self, inst_type: str = "SWAP") -> list[Instrument]:
        data = self.get("/api/v5/public/instruments", {"instType": inst_type})
        out = []
        for d in data:
            out.append(Instrument(
                inst_id=d["instId"],
                uly=d.get("uly", ""),
                ct_val=_to_float(d.get("ctVal")),
                list_time=_to_int(d.get("listTime")),
                exp_time=_to_int(d.get("expTime")),
                state=d.get("state", ""),
            ))
        return out

    def candles(self, inst_id: str, bar: str = "5m", limit: int = 300, after: int | None = None, before: int | None = None) -> pd.DataFrame:
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        return parse_candles(self.get("/api/v5/market/candles", params))

    def history_candles(self, inst_id: str, bar: str = "5m", limit: int | None = None, after: int | None = None, before: int | None = None) -> tuple[pd.DataFrame, int]:
        limit = self.history_limit if limit is None else limit
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        rows = self.get("/api/v5/market/history-candles", params)
        return parse_candles(rows), len(rows)
