from okxrev.config import load_config
from okxrev.sync import DAY_MS, InstState, backfill_pages_per_inst, is_done_for, layer_targets


def test_backfill_pages_per_inst():
    assert backfill_pages_per_inst({"backfill_bars_per_inst": 2000}, 300) == 7
    assert backfill_pages_per_inst({"backfill_bars_per_inst": 2000}, 100) == 20
    assert backfill_pages_per_inst({}, 300) == 7
    assert backfill_pages_per_inst({"backfill_bars_per_inst": 10}, 300) == 1


def test_layer_targets_order_and_floor():
    cfg = load_config()
    start = cfg.backfill_start_ms
    now = start + 400 * DAY_MS
    t = layer_targets(cfg, now)
    assert t == [now - 30 * DAY_MS, now - 90 * DAY_MS, now - 180 * DAY_MS, start]
    now = start + 60 * DAY_MS
    t = layer_targets(cfg, now)
    assert t[0] == now - 30 * DAY_MS
    assert t[1:] == [start, start, start]


def test_is_done_for():
    s = InstState("X", "live", list_time=1000, oldest_ts=5000, newest_ts=9000, backfill_done=False)
    assert not is_done_for(s, 4000)
    assert is_done_for(s, 6000)
    s.oldest_ts = 1000
    assert is_done_for(s, 0)
    s2 = InstState("Y", "live", list_time=None, oldest_ts=None, newest_ts=None, backfill_done=False)
    assert not is_done_for(s2, 0)
    s2.backfill_done = True
    assert is_done_for(s2, 0)


def test_freshness_and_coverage(tmp_path):
    import duckdb
    from okxrev import db, derivs, heatdata
    from okxrev.config import Paths
    from okxrev.sync import collect_status, freshness
    cfg = load_config().copy_with({"heat": {"watchlist": ["A-USDT-SWAP"]}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "okx.duckdb", logs=tmp_path / "l", results=tmp_path / "r", lock=tmp_path / "k")
    H = 3_600_000
    now = 1_800_000_000_000
    with db.open_db(cfg.paths.db) as con:
        derivs.ensure_schema(con)
        heatdata.ensure_schema(con)
        for i in ("A-USDT-SWAP", "B-USDT-SWAP"):
            con.executemany("INSERT INTO funding VALUES (?, ?, 0.0001, 0.0001)", [(i, now - 10 * H - k * 8 * H) for k in range(5)])
            con.execute("INSERT INTO open_interest VALUES (?, '1H', ?, 1, 1, 1)", [i, now - (1 if i == "A-USDT-SWAP" else 9) * H])
            con.execute("INSERT INTO open_interest VALUES (?, '5m', ?, 1, 1, 1)", [i, now - 10 * 60_000])
        f = {x["key"]: x for x in freshness(cfg, con, now)}
    assert f["funding"]["late"] == 2 and f["funding"]["median_h"] == 10
    assert f["oi_1h"]["late"] == 1 and f["oi_1h"]["max_h"] == 9
    assert f["oi_5m"]["n"] == 1
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "A-USDT-SWAP"}])
    s = collect_status(cfg)
    assert "coverage" in s and "freshness" in s
