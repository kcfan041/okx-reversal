import pytest
import requests

from okxrev.okx_client import (
    InstrumentNotFound,
    OkxClient,
    OkxError,
    RateLimiter,
    RetryExhausted,
    parse_candles,
)

CFG = {
    "base_url": "https://example.invalid",
    "timeout_sec": 1,
    "max_requests_per_sec": 1000,
    "max_retries": 3,
    "backoff_base_sec": 1.0,
}


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        pass


def make(responses):
    sleeps = []
    c = OkxClient(CFG, sleep=sleeps.append)
    c.limiter = RateLimiter(1000, sleep=lambda d: None)
    c.session = FakeSession(responses)
    return c, sleeps


def test_rate_limiter_spacing():
    t = [0.0]
    slept = []

    def sleep(d):
        slept.append(d)
        t[0] += d

    rl = RateLimiter(5, clock=lambda: t[0], sleep=sleep)
    for _ in range(6):
        rl.acquire()
    assert t[0] == pytest.approx(1.0)
    assert len(slept) == 5


def test_retry_on_429_then_ok():
    c, sleeps = make([FakeResp(429, {}), FakeResp(200, {"code": "50011", "msg": "busy"}), FakeResp(200, {"code": "0", "data": [1]})])
    assert c.get("/x", {}) == [1]
    assert sleeps == [1.0, 2.0]
    assert c.n_requests == 3


def test_retry_on_connection_error_exhausts():
    c, sleeps = make([requests.ConnectionError("boom")] * 4)
    with pytest.raises(RetryExhausted):
        c.get("/x", {})
    assert sleeps == [1.0, 2.0, 4.0]


def test_not_found_no_retry():
    c, sleeps = make([FakeResp(200, {"code": "51001", "msg": "nope"})])
    with pytest.raises(InstrumentNotFound):
        c.get("/x", {})
    assert sleeps == []


def test_other_code_raises():
    c, _ = make([FakeResp(200, {"code": "51000", "msg": "bad param"})])
    with pytest.raises(OkxError):
        c.get("/x", {})


def test_separate_limiters_by_path():
    cfg = {**CFG, "history_requests_per_sec": 8, "market_requests_per_sec": 15, "history_limit": 300}
    c = OkxClient(cfg)
    assert c.limiter_for("/api/v5/market/history-candles").target == 8
    assert c.limiter_for("/api/v5/market/candles").target == 15
    assert c.limiter_for("/api/v5/public/instruments").target == 1000
    assert c.history_limit == 300


def test_limiter_fallback_to_default_rate():
    c = OkxClient(CFG)
    assert c.limiter_for("/api/v5/market/history-candles").target == 1000
    assert c.history_limit == 100


def test_history_uses_configured_limit():
    cfg = {**CFG, "history_limit": 300}
    c = OkxClient(cfg, sleep=lambda d: None)
    seen = {}

    class S(FakeSession):
        def get(self, url, params=None, timeout=None):
            seen.update(params)
            return FakeResp(200, {"code": "0", "data": []})

    c.session = S([])
    c.history_candles("X-USDT-SWAP", after=1)
    assert seen["limit"] == "300"


def test_limiter_throttle_and_recover():
    t = [0.0]
    rl = RateLimiter(10, clock=lambda: t[0], sleep=lambda d: t.__setitem__(0, t[0] + d))
    rl.on_limited(); rl.on_limited()
    assert rl.rate == 10
    rl.on_limited()
    assert rl.rate == pytest.approx(8)
    for _ in range(30):
        rl.on_limited()
    assert rl.rate == pytest.approx(5)
    assert rl.n_limited == 33
    t[0] += 61
    rl.acquire()
    assert rl.rate == pytest.approx(5.5)
    t[0] += 10
    rl.acquire()
    assert rl.rate == pytest.approx(5.5)
    for _ in range(20):
        t[0] += 61
        rl.acquire()
    assert rl.rate == pytest.approx(10)


def test_success_resets_consecutive():
    rl = RateLimiter(10)
    rl.on_limited(); rl.on_limited(); rl.on_success(); rl.on_limited()
    assert rl.rate == 10


def test_get_signals_limiter():
    c, _ = make([FakeResp(429, {}), FakeResp(429, {}), FakeResp(429, {}), FakeResp(200, {"code": "0", "data": []})])
    c.limiter = RateLimiter(10, sleep=lambda d: None)
    c.get("/x", {})
    assert c.limiter.rate == pytest.approx(8)
    assert c.n_limited == 3


def test_parse_candles_confirm_and_order():
    rows = [
        ["3000", "1", "2", "0.5", "1.5", "10", "10", "15", "0"],
        ["2000", "1", "2", "0.5", "1.5", "10", "10", "15", "1"],
        ["1000", "1", "2", "0.5", "1.5", "10", "10", "15", "1"],
    ]
    df = parse_candles(rows)
    assert df["ts"].tolist() == [1000, 2000]
    assert df["vol_quote"].tolist() == [15.0, 15.0]


def test_parse_candles_empty():
    df = parse_candles([])
    assert df.empty
    assert list(df.columns)[:5] == ["ts", "open", "high", "low", "close"]
