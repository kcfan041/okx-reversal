import subprocess
import sys
import textwrap

import pandas as pd
import pytest

from okxrev import db


def candles(ts_list, base=100.0):
    return pd.DataFrame({
        "ts": ts_list,
        "open": base, "high": base + 1, "low": base - 1, "close": base,
        "vol": 1.0, "vol_ccy": 1.0, "vol_quote": 100.0,
    })


def test_write_idempotent(tmp_path):
    p = tmp_path / "t.duckdb"
    with db.open_db(p) as con:
        df = candles([0, 300000, 600000])
        db.write_batch(con, "AAA-USDT-SWAP", df)
        db.write_batch(con, "AAA-USDT-SWAP", df)
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 3
        db.write_batch(con, "AAA-USDT-SWAP", candles([600000, 900000], base=200.0))
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 4
        assert con.execute("SELECT close FROM candles WHERE ts = 600000").fetchone()[0] == 200.0
        st = con.execute("SELECT oldest_ts, newest_ts FROM sync_state").fetchone()
        assert st == (0, 900000)
        db.write_batch(con, "AAA-USDT-SWAP", candles([-300000]), backfill_done=True)
        st = con.execute("SELECT oldest_ts, newest_ts, backfill_done FROM sync_state").fetchone()
        assert st == (-300000, 900000, True)


def test_instruments_new_and_delisted(tmp_path):
    p = tmp_path / "t.duckdb"
    with db.open_db(p) as con:
        new, gone = db.upsert_instruments(con, [{"inst_id": "A"}, {"inst_id": "B"}], now=1)
        assert sorted(new) == ["A", "B"] and gone == []
        new, gone = db.upsert_instruments(con, [{"inst_id": "A"}, {"inst_id": "C"}], now=2)
        assert new == ["C"] and gone == ["B"]
        rows = dict(con.execute("SELECT inst_id, state FROM instruments").fetchall())
        assert rows == {"A": "live", "B": "delisted", "C": "live"}
        assert con.execute("SELECT first_seen FROM instruments WHERE inst_id='A'").fetchone()[0] == 1
        db.mark_delisted(con, "A", now=3)
        assert con.execute("SELECT backfill_done FROM sync_state WHERE inst_id='A'").fetchone()[0] is True


def test_load_candles_range(tmp_path):
    p = tmp_path / "t.duckdb"
    with db.open_db(p) as con:
        db.write_batch(con, "A", candles([0, 300000, 600000, 900000]))
        df = db.load_candles(con, "A", since=300000, until=900000)
        assert df["ts"].tolist() == [300000, 600000]
    with db.open_db(p, read_only=True) as ro:
        assert db.list_candle_insts(ro) == ["A"]


HOLDER = textwrap.dedent("""
    import sys, time, duckdb
    con = duckdb.connect(sys.argv[1], read_only=sys.argv[2] == "ro")
    print("ready", flush=True)
    time.sleep(float(sys.argv[3]))
    con.close()
""")


def _hold(path, mode, secs):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), mode, str(secs)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


def test_cross_process_wait_then_succeed(tmp_path):
    p = tmp_path / "t.duckdb"
    with db.open_db(p) as con:
        db.write_batch(con, "A", candles([0]))
    proc = _hold(p, "ro", 1.5)
    try:
        with db.open_db(p, wait_sec=0.5, max_tries=20) as con:
            db.write_batch(con, "A", candles([300000]))
    finally:
        proc.wait()
    proc = _hold(p, "rw", 1.5)
    try:
        with db.open_db(p, read_only=True, wait_sec=0.5, max_tries=20) as ro:
            assert ro.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 2
    finally:
        proc.wait()


def test_cross_process_busy_raises(tmp_path):
    p = tmp_path / "t.duckdb"
    with db.open_db(p):
        pass
    proc = _hold(p, "rw", 3)
    try:
        with pytest.raises(db.DbBusy):
            db.connect(p, read_only=True, wait_sec=0.1, max_tries=3)
    finally:
        proc.wait()
