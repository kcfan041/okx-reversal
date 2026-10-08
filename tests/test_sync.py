from okxrev.config import load_config
from okxrev.sync import DAY_MS, InstState, is_done_for, layer_targets


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
