from okxrev import db
from okxrev.backtest import EVENT_COLUMNS, run_backtest
from okxrev.config import Paths, load_config

from test_detector_metrics import EV, btc_df, pumped


def test_backtest_end_to_end(tmp_path):
    base = load_config()
    cfg = base.copy_with({"backtest": {"baseline": {"random_samples": 50}, "bootstrap": {"iterations": 50}}})
    cfg.paths = Paths(root=tmp_path, db=tmp_path / "t.duckdb", logs=tmp_path, results=tmp_path, lock=tmp_path / "l")
    with db.open_db(cfg.paths.db) as con:
        db.upsert_instruments(con, [{"inst_id": "BTC-USDT-SWAP"}, {"inst_id": "AAA-USDT-SWAP"}])
        db.write_batch(con, "BTC-USDT-SWAP", btc_df())
        db.write_batch(con, "AAA-USDT-SWAP", pumped())
    res = run_backtest(cfg, None, None, None, sweep=True)
    ev = res["events"]
    assert list(ev.columns[: len(EVENT_COLUMNS)]) == EVENT_COLUMNS
    pr = ev[ev["mode"] == "pump_red"]
    assert len(pr) == 1 and pr.iloc[0]["event_ts"] == int(pumped()["ts"].iloc[EV])
    s = res["summary"]
    assert s["modes"]["pump_red"]["n_events"] == 1
    assert s["baselines"]["random_any"]["n_events"] == 50
    assert len(res["sweep"]) == 12
