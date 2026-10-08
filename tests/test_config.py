from okxrev.config import REPO_ROOT, load_config, parse_utc_ms


def test_load_default_config():
    cfg = load_config()
    assert cfg.paths.db.is_absolute()
    assert cfg.paths.db == (REPO_ROOT / "data" / "okx.duckdb").resolve()
    assert cfg.universe["btc_inst"] == "BTC-USDT-SWAP"
    assert cfg.metrics["horizons"]["24h"] == 288


def test_parse_utc_ms():
    assert parse_utc_ms("1970-01-01T00:00:00Z") == 0
    assert parse_utc_ms("2025-10-01") == 1759276800000


def test_copy_with_overrides():
    cfg = load_config()
    c2 = cfg.copy_with({"detector": {"red_candle": {"ret_max": -0.15}}})
    assert c2.detector["red_candle"]["ret_max"] == -0.15
    assert cfg.detector["red_candle"]["ret_max"] == -0.08
    assert c2.detector["red_candle"]["vol_ratio_min"] == 4.0
