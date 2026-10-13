import pandas as pd
import pytest

from okxrev import account, live, portfolio

from test_account import env  # noqa: F401

N = [0]


def fill(ts, inst, base_chg, base_bal, px, cl="O1", ord_id=None, fee=0.0):
    N[0] += 1
    o = ord_id or f"o{N[0]}"
    base, _ = inst.split("-")
    return [
        {"bill_id": str(N[0] * 2), "ts": ts, "ccy": base, "inst_type": "SPOT", "inst_id": inst, "type": "2", "sub_type": "1" if base_chg > 0 else "2",
         "bal_chg": base_chg, "bal": base_bal, "sz": abs(base_chg), "px": px, "fee": fee, "pnl": 0.0, "ord_id": o, "cl_ord_id": cl, "exec_type": "T"},
        {"bill_id": str(N[0] * 2 + 1), "ts": ts, "ccy": "USDT", "inst_type": "SPOT", "inst_id": inst, "type": "2", "sub_type": "2" if base_chg > 0 else "1",
         "bal_chg": -base_chg * px, "bal": 1000.0, "sz": abs(base_chg * px), "px": px, "fee": 0.0, "pnl": 0.0, "ord_id": o, "cl_ord_id": cl, "exec_type": "T"},
    ]


def bills_df():
    rows = []
    rows += fill(1000, "AAA-USDT", 10.0, 110.0, 1.0)
    rows += fill(1100, "BBB-USDT", 5.0, 5.0, 2.0, cl="Oman")
    rows += fill(2000, "AAA-USDT", -20.0, 90.0, 1.5)
    rows += fill(3000, "AAA-USDT", 5.0, 95.0, 1.2)
    rows += fill(10_000_000, "CCC-USDT", 4.0, 4.0, 10.0, cl="Orun")
    rows += fill(10_060_000, "DDD-USDT", 2.0, 2.0, 50.0, cl="Orun")
    rows += fill(11_000_000, "CCC-USDT", -4.0, 0.0, 11.0, cl="Orun")
    rows += fill(11_000_000, "DDD-USDT", -2.0, 0.0, 45.0, cl="Orun")
    rows += fill(12_000_000, "EEE-USDT", 3.0, 3.0, 10.0, cl=None)
    rows += fill(12_100_000, "EEE-USDT", -3.0, 0.0, 12.0, cl=None)
    return pd.DataFrame(rows)


SNAP = pd.DataFrame([
    {"ccy": "AAA", "stgy_eq": 95.0, "eq": 95.0, "eq_usd": 95 * 2.0},
    {"ccy": "BBB", "stgy_eq": 0.0, "eq": 5.0, "eq_usd": 10.0},
    {"ccy": "USDT", "stgy_eq": 3.0, "eq": 1000.0, "eq_usd": 1000.0},
])


def test_chain_linking_splits_flows():
    b = bills_df()
    rows = fill(1500, "AAA-USDT", 1.0, 1.0, 1.1, cl=None)
    ch = portfolio.build_chains(pd.concat([b[b["ccy"] == "AAA"], pd.DataFrame(rows[:1])]))
    assert ch.groupby("chain").size().sort_values().tolist() == [1, 3]


def test_running_portfolio_cost_and_pnl():
    res = portfolio.reconstruct(bills_df(), SNAP)
    r = res["running"]
    assert r and [c["ccy"] for c in r["coins"]] == ["AAA"]
    a = r["coins"][0]
    assert a["qty_open"] == pytest.approx(100) and a["px_open"] == 1.0
    assert a["realized"] == pytest.approx(30 - 20 * 1.0)
    cost_left = 90 * 1.0 + 5 * 1.2
    assert a["avg_cost"] == pytest.approx(cost_left / 95)
    assert a["unrealized"] == pytest.approx(95 * 2.0 - cost_left)
    assert r["cash_usdt"] == 3.0 and r["pnl"] == pytest.approx(r["realized"] + r["unrealized"])


def test_finished_runs_grouped_and_manual_excluded():
    res = portfolio.reconstruct(bills_df(), SNAP)
    runs = res["runs"]
    assert len(runs) == 1 and sorted(runs[0]["ccys"]) == ["CCC", "DDD"]
    assert runs[0]["realized"] == pytest.approx(4 * 1 + 2 * -5)
    res2 = portfolio.reconstruct(bills_df(), SNAP, bot_windows=[("CCC-USDT", 10_000_000, 11_000_000), ("DDD-USDT", 10_000_000, 11_000_000)])
    assert res2["runs"] == []


def test_store_and_load_trades_unrealized(env):  # noqa: F811
    con = account.connect(env.account_db)
    b = bills_df()
    con.executemany("INSERT INTO bills VALUES (" + ", ".join(["?"] * 16) + ")", [tuple(r) for r in b[[
        "bill_id", "ts", "ccy", "inst_type", "inst_id", "type", "sub_type", "bal_chg", "bal", "sz", "px", "fee", "pnl", "ord_id", "cl_ord_id", "exec_type"]].itertuples(index=False)])
    con.executemany("INSERT INTO strategy_balances VALUES (?, ?, ?, ?, ?)", [(5, r.ccy, r.stgy_eq, r.eq, r.eq_usd) for r in SNAP.itertuples(index=False)])
    portfolio.store(con)
    portfolio.store(con)
    assert con.execute("SELECT COUNT(*) FROM bots WHERE algo_type = 'smart_portfolio'").fetchone()[0] == 2
    con.close()
    t = account.load_trades(env, sources=("bot",))
    run = t[t["is_open"]].iloc[0]
    assert run["bot_type"] == "smart_portfolio" and run["unrealized_usd"] > 0
    s = account.summarize_trades(t)["overall"]
    assert s["unrealized"] == pytest.approx(run["unrealized_usd"]) and s["n_open"] == 1


def test_live_spot_excludes_strategy_balance():
    bal = [{"details": [
        {"ccy": "AAA", "eq": "95", "stgyEq": "95", "eqUsd": "190", "frozenBal": "95"},
        {"ccy": "SOL", "eq": "2", "stgyEq": "0.5", "eqUsd": "200", "openAvgPx": "90", "frozenBal": "0.5"},
    ]}]
    spot = live.parse_spot(bal, pd.DataFrame())
    assert [s["inst_id"] for s in spot] == ["SOL-USDT"]
    assert spot[0]["size"] == pytest.approx(1.5) and spot[0]["notional_usd"] == pytest.approx(150) and spot[0]["frozen"] == 0
