"""Read-only tools the chat AI can call. Every tool returns a compact JSON-serialisable dict.

Rules: tools never write to any database or file and never call trading endpoints. Large outputs are summarised
(K-lines are aggregated, tables are truncated) so a single call stays small.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

import duckdb
import numpy as np
import pandas as pd

from okxrev import db
from okxrev.config import Config, ms_to_str, parse_utc_ms

log = logging.getLogger("okxrev.chat")

BAR = db.BAR_MS
HOUR = 3_600_000
DAY = 86_400_000
INST_RE = re.compile(r"^[A-Z0-9]{1,20}(-[A-Z0-9]{1,10}){1,2}$")


class ToolError(Exception):
    pass


def norm_inst(x: Any) -> str:
    s = str(x or "").strip().upper()
    if not s:
        raise ToolError("缺少合約代號")
    if "-" not in s:
        s = f"{s}-USDT-SWAP"
    elif s.endswith("-USDT"):
        s = f"{s}-SWAP"
    if not INST_RE.match(s):
        raise ToolError(f"合約代號格式錯誤：{x}")
    return s


def _t(ms: Any) -> str | None:
    try:
        return ms_to_str(int(ms)) + " UTC" if ms is not None and not pd.isna(ms) else None
    except (TypeError, ValueError):
        return None


def _r(v: Any, d: int = 6) -> Any:
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if not np.isfinite(f):
        return None
    return round(f, d)


def _rec(d: dict[str, Any], d_: int = 6) -> dict[str, Any]:
    return {k: (_rec(v, d_) if isinstance(v, dict) else [_r(x, d_) if not isinstance(x, (dict, list)) else x for x in v]
                if isinstance(v, list) else _r(v, d_)) for k, v in d.items()}


def _time_arg(v: Any, default: int) -> int:
    if v in (None, ""):
        return default
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hd])\s*ago", s)
    if m:
        return db.now_ms() - int(float(m.group(1)) * (HOUR if m.group(2) == "h" else DAY))
    try:
        return parse_utc_ms(s.replace(" UTC", "").replace(" ", "T") if "T" not in s else s)
    except ValueError:
        raise ToolError(f"時間格式錯誤：{v}（請用 2026-10-12 或 2026-10-12T08:00 或 '24h ago'）")


def _pct(a: float, b: float) -> float | None:
    return _r(a / b - 1, 5) if a and b and np.isfinite(a) and np.isfinite(b) and b != 0 else None


class Toolbox:
    """Holds config, caches and the tool registry. One instance per chat request."""

    def __init__(self, cfg: Config, allow_private: bool = True, allow_live: bool = True):
        self.cfg = cfg
        self.allow_private = allow_private
        self.allow_live = allow_live
        self._client = None
        self._priv = None
        self._feat_cache: dict[str, pd.DataFrame] = {}
        self._trades: pd.DataFrame | None = None

    # ------------------------------------------------------------ helpers
    def client(self):
        from okxrev.okx_client import OkxClient
        if self._client is None:
            self._client = OkxClient(self.cfg.okx)
        return self._client

    def priv(self):
        from okxrev import account
        if self._priv is None:
            self._priv = account.make_client(self.cfg)
        return self._priv

    def close(self) -> None:
        for c in (self._client, self._priv):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass

    def candles(self, inst: str, since: int | None, until: int | None = None) -> pd.DataFrame:
        from okxrev import replay
        frames = []
        with db.open_db(self.cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
            frames.append(db.load_candles(con, inst, since, until))
        oldp = replay.old_db_path(self.cfg)
        if oldp.exists() and (since is None or since < (int(frames[0]["ts"].min()) if len(frames[0]) else db.now_ms())):
            try:
                with db.open_db(oldp, read_only=True, wait_sec=1.0, max_tries=10) as con:
                    frames.append(db.load_candles(con, inst, since, until))
            except (db.DbBusy, FileNotFoundError):
                pass
        df = pd.concat([f for f in frames if len(f)], ignore_index=True) if any(len(f) for f in frames) else frames[0]
        if len(df):
            df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        return df

    def features_at(self, inst: str, until: int | None = None) -> pd.DataFrame:
        """Full feature frame (same as paper trading) for the last ~10 days up to `until`."""
        key = f"{inst}|{until}"
        if key in self._feat_cache:
            return self._feat_cache[key]
        from okxrev.derivs import attach, load_funding, load_oi
        from okxrev.detector import btc_features, prepare
        from okxrev.discover import features, market_breadth_recent
        end = until or db.now_ms()
        since = end - 10 * DAY
        raw = self.candles(inst, since, end + BAR)
        if len(raw) < 300:
            raise ToolError(f"{inst} 在這段時間沒有足夠的 K 線資料")
        btc = btc_features(self.candles(self.cfg.universe["btc_inst"], since - DAY, end + BAR), self.cfg.btc_regime)
        with db.open_db(self.cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
            meta = db.sync_overview(con).set_index("inst_id")
            fund, oi = load_funding(con, inst), load_oi(con, inst)
            insts = db.list_candle_insts(con)
        lt = meta.at[inst, "list_time"] if inst in meta.index else None
        lt = None if lt is None or pd.isna(lt) else int(lt)
        g = prepare(raw, self.cfg.detector, btc, list_time=lt)
        try:
            br = market_breadth_recent(self.cfg, insts, since)
        except Exception:
            br = None
        f = features(g, self.cfg.detector, br, lt, attach(g, fund, oi))
        f["close"] = g["close"]
        self._feat_cache[key] = f
        return f

    def trades(self) -> pd.DataFrame:
        if self._trades is None:
            from okxrev import account
            self._trades = account.load_trades(self.cfg, sources=account.SOURCES)
            p = self.cfg.paths.results / "trades_enriched.csv"
            if len(self._trades) and p.exists():
                e = pd.read_csv(p)
                extra = [c for c in ("mae", "mfe", "capture", "post_72", "f_r24h", "f_btc_regime", "entry_range_pos", "revenge") if c in e]
                self._trades = self._trades.merge(e[["trade_id", *extra]], on="trade_id", how="left")
        return self._trades

    def _json(self, name: str) -> dict[str, Any] | None:
        p = self.cfg.paths.results / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    # ------------------------------------------------------------ tools
    def market_list(self, query: str | None = None, sort: str = "heat", limit: int = 20) -> dict[str, Any]:
        """List contracts: filter by keyword; sort by heat score, 24h change, or turnover."""
        snap = self._json("heat_snapshot.json") or {"rows": []}
        rows = snap["rows"]
        if query:
            q = str(query).upper()
            rows = [r for r in rows if q in r["inst_id"]]
        key = {"heat": "score", "ret_24h": "ret_24h", "drop_24h": "ret_24h"}.get(sort, "score")
        rows = sorted(rows, key=lambda r: (r.get(key) is None, -(r.get(key) or 0) if sort != "drop_24h" else (r.get(key) or 0)))
        limit = max(1, min(int(limit or 20), 50))
        return {"as_of": _t(snap.get("ts")), "n_total": len(snap["rows"]),
                "rows": [{"inst": r["inst_id"], "heat": _r(r.get("score"), 1), "rank": _r(r.get("rank")), "ret_24h": _r(r.get("ret_24h"), 4),
                          "funding": _r(r.get("funding")), "oi_chg_24h": _r(r.get("oi_chg_24h"), 3), "cooling": r.get("cooling")} for r in rows[:limit]]}

    def price_summary(self, inst: str, start: Any = None, end: Any = None, bar: str = "1h", max_rows: int = 60) -> dict[str, Any]:
        """K-line summary for a period: stats + up to `max_rows` aggregated bars (5m/15m/1h/4h/1d)."""
        inst = norm_inst(inst)
        now = db.now_ms()
        e = _time_arg(end, now)
        s = _time_arg(start, e - 3 * DAY)
        if e - s > 400 * DAY:
            raise ToolError("時間區間太長（上限 400 天）")
        g = self.candles(inst, s, e)
        if g.empty:
            raise ToolError(f"{inst} 在 {_t(s)} ～ {_t(e)} 沒有 K 線資料（資料最早約 2024-09，且只有現存合約）")
        ms = {"5m": BAR, "15m": 3 * BAR, "1h": HOUR, "4h": 4 * HOUR, "1d": DAY}.get(bar, HOUR)
        max_rows = max(5, min(int(max_rows or 60), 120))
        while (e - s) / ms > max_rows and ms < DAY:
            ms = {BAR: 3 * BAR, 3 * BAR: HOUR, HOUR: 4 * HOUR, 4 * HOUR: DAY}[ms]
        k = (g["ts"] // ms) * ms
        agg = g.groupby(k).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("vol_quote", "sum"))
        agg = agg.tail(max_rows)
        c = g["close"].to_numpy(float)
        lr = np.diff(np.log(c))
        hi_i, lo_i = int(g["high"].idxmax()), int(g["low"].idxmin())
        return {"inst": inst, "from": _t(int(g["ts"].iloc[0])), "to": _t(int(g["ts"].iloc[-1])), "bar": {BAR: "5m", 3 * BAR: "15m", HOUR: "1h", 4 * HOUR: "4h", DAY: "1d"}[ms],
                "open": _r(g["open"].iloc[0]), "close": _r(c[-1]), "change": _pct(c[-1], g["open"].iloc[0]),
                "high": _r(g["high"].max()), "high_at": _t(int(g["ts"].iloc[hi_i])), "low": _r(g["low"].min()), "low_at": _t(int(g["ts"].iloc[lo_i])),
                "max_drawdown_from_high": _pct(c[hi_i:].min(), g["high"].iloc[hi_i]) if hi_i < len(c) else None,
                "volatility_5m": _r(lr.std(), 5) if len(lr) > 2 else None, "turnover_usdt": _r(g["vol_quote"].sum(), 0),
                "bars": [[ms_to_str(int(t))[5:], _r(r.o), _r(r.h), _r(r.l), _r(r.c), _r(r.v, 0)] for t, r in agg.iterrows()],
                "bars_columns": ["time(UTC MM-DD HH:MM)", "open", "high", "low", "close", "turnover"]}

    def indicators(self, inst: str, at: Any = None) -> dict[str, Any]:
        """All model/rule features for a coin at a time (default now): returns, volume ratio, range position, ATR,
        BTC regime, market breadth, funding, OI change, listing age, pump/red-candle flags."""
        inst = norm_inst(inst)
        until = _time_arg(at, db.now_ms()) if at else None
        f = self.features_at(inst, until)
        f = f[f.index <= (until or db.now_ms())]
        row = f.iloc[-1]
        keys = ["close", "r1h", "r6h", "r24h", "r3d", "ret5", "vol_ratio", "vmax_1h", "pos24", "pos7d", "new_high24", "new_low24", "dist_ema",
                "vol1d", "vol_rank", "atr", "turnover24", "pump", "red_k", "pump_red", "pump_red_24h", "btc_regime", "btc_r1h", "btc_r4h",
                "breadth", "funding", "oi_chg", "age_d"]
        return {"inst": inst, "bar": _t(int(f.index[-1])), "values": {k: (row[k] if isinstance(row[k], str) else _r(row[k], 5)) for k in keys if k in row},
                "glossary": "r1h/r6h/r24h/r3d=前1h/6h/24h/3d報酬；vol_ratio=本根量/20根均量；vmax_1h=近1h最大量比；pos24/pos7d=在24h/7日區間位置(0底1頂)；"
                            "dist_ema=距日均線；vol_rank=日波動/週波動；atr=14根5分K ATR佔價格；turnover24=24h成交額；pump=暴漲狀態；red_k=巨大紅K；"
                            "btc_regime=BTC 4h 強/中/弱；breadth=全市場24h上漲比例；funding=最新資金費率；oi_chg=OI 24h變化；age_d=上市天數"}

    def heat(self, inst: str, days: int = 5) -> dict[str, Any]:
        """Overheat score (0-100), five dimensions, per-indicator 30-day percentiles, cooling flags, recent trend, market rank."""
        from okxrev import heat as H
        inst = norm_inst(inst)
        d = H.coin_detail(self.cfg, inst, max(1, min(int(days or 5), 15)))
        if d.get("empty"):
            raise ToolError(f"{inst} 沒有足夠資料計算過熱分數")
        snap = self._json("heat_snapshot.json") or {"rows": []}
        me = next((r for r in snap["rows"] if r["inst_id"] == inst), {})
        step = max(1, len(d["t"]) // 24)
        trend = [[ms_to_str(t * 1000)[5:], d["series"]["score"][i], bool(d["cooling"][i])] for i, t in enumerate(d["t"])][::-step][::-1]
        return {"inst": inst, "score_now": _r(d["now"]["score"], 1), "market_rank": _r(me.get("rank")), "n_market": len(snap["rows"]),
                "dimensions": {d["dims"][k]["label"] + ("（參考）" if d["dims"][k]["experimental"] else ""): _r(d["now"][k], 1) for k in d["dims"]},
                "indicators": {v["label"]: {"pct_30d": _r(v["pct"], 1), "value": _r(v["raw"], 5)} for v in d["now"]["indicators"].values()},
                "cooling_now": bool(d["cooling"][-1]) if d["cooling"] else None, "trend": trend,
                "note": "百分位是這個幣自己過去 30 天的位置；情緒類只供參考；過熱分數本身的預測力還在驗證（見 heat_validation）"}

    def heat_validation(self) -> dict[str, Any]:
        """Whether the overheat score has predicted declines historically (discovery + blind test results)."""
        r = self._json("heat.json")
        if not r:
            return {"status": "尚未完成驗證（歷史資料補完後執行）"}
        return {"discovery": r["discovery"], "blind": r["blind"], "notes": r.get("notes"),
                "results": [{"condition": x["name"], "verdict": x["verdict"], "hold": x["best_h"],
                             "discovery_short_day_mean": _r(x[x["best_h"]]["disc"].get("day_mean"), 4), "discovery_n": x[x["best_h"]]["disc"].get("n"),
                             "blind_short_day_mean": _r(x[x["best_h"]]["blind"].get("day_mean"), 4), "blind_n": x[x["best_h"]]["blind"].get("n"),
                             "baseline_blind": _r(x[x["best_h"]]["base_blind"], 4)} for x in r["results"]]}

    def derivatives(self, inst: str, hours: int = 48) -> dict[str, Any]:
        """Funding rate history, open interest (1H), taker buy/sell volume (1H), premium vs index, account and top-trader
        long/short ratio, and recent liquidations for a coin."""
        inst = norm_inst(inst)
        hours = max(6, min(int(hours or 48), 24 * 14))
        since = db.now_ms() - hours * HOUR
        out: dict[str, Any] = {"inst": inst, "hours": hours}
        with db.open_db(self.cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
            def q(sql: str, args: list[Any]) -> pd.DataFrame:
                try:
                    return con.execute(sql, args).df()
                except duckdb.CatalogException:
                    return pd.DataFrame()
            f = q("SELECT ts, rate FROM funding WHERE inst_id = ? AND ts >= ? ORDER BY ts", [inst, since - 2 * DAY])
            oi = q("SELECT ts, oi_usd FROM open_interest WHERE inst_id = ? AND period = '1H' AND ts >= ? ORDER BY ts", [inst, since])
            tk = q("SELECT ts, buy, sell FROM taker_volume WHERE inst_id = ? AND ts >= ? ORDER BY ts", [inst, since])
            mk = q("SELECT ts, mark, idx FROM mark_index WHERE inst_id = ? AND ts >= ? ORDER BY ts", [inst, since])
            ls = q("SELECT ts, acct, top FROM ls_ratio WHERE inst_id = ? AND ts >= ? ORDER BY ts", [inst, since])
            lq = q("SELECT ts, pos_side, px, sz FROM liquidations WHERE inst_id = ? AND ts >= ? ORDER BY ts DESC", [inst, since])
        step = lambda df: df.iloc[:: max(1, len(df) // 16)] if len(df) else df
        out["funding_rate"] = [[_t(r.ts), _r(r.rate, 6)] for r in f.tail(12).itertuples()]
        if len(oi):
            out["open_interest_usd"] = {"first": _r(oi["oi_usd"].iloc[0], 0), "last": _r(oi["oi_usd"].iloc[-1], 0),
                                        "change": _pct(oi["oi_usd"].iloc[-1], oi["oi_usd"].iloc[0]),
                                        "series": [[ms_to_str(int(r.ts))[5:], _r(r.oi_usd, 0)] for r in step(oi).itertuples()]}
        if len(tk):
            tk["ratio"] = tk["buy"] / tk["sell"]
            out["taker_buy_sell"] = {"ratio_total": _r(tk["buy"].sum() / tk["sell"].sum(), 3), "ratio_last_6h": _r(tk["buy"].tail(6).sum() / tk["sell"].tail(6).sum(), 3),
                                     "series": [[ms_to_str(int(r.ts))[5:], _r(r.buy, 0), _r(r.sell, 0)] for r in step(tk).itertuples()]}
        if len(mk):
            mk["prem"] = mk["mark"] / mk["idx"] - 1
            out["premium"] = {"last": _r(mk["prem"].iloc[-1], 5), "mean": _r(mk["prem"].mean(), 5), "max": _r(mk["prem"].max(), 5)}
        if len(ls):
            out["long_short_ratio"] = {"account_last": _r(ls["acct"].iloc[-1], 3), "top_trader_last": _r(ls["top"].iloc[-1], 3),
                                       "account_first": _r(ls["acct"].iloc[0], 3), "since": _t(int(ls["ts"].iloc[0]))}
        if len(lq):
            lq["usd"] = lq["px"] * lq["sz"]
            out["liquidations"] = {"since": _t(int(lq["ts"].min())), "short_count": int((lq["pos_side"] == "short").sum()),
                                   "long_count": int((lq["pos_side"] == "long").sum()), "latest": [[_t(r.ts), r.pos_side, _r(r.px), _r(r.sz)] for r in lq.head(8).itertuples()],
                                   "note": "張數 × 價格不等於 USDT 金額（需乘合約面值）；只有近期累積的資料"}
        return out

    def live_market(self, inst: str) -> dict[str, Any]:
        """Real-time ticker, top-of-book depth, current funding and open interest from OKX (public data)."""
        if not self.allow_live:
            raise ToolError("即時查詢已停用")
        inst = norm_inst(inst)
        c = self.client()
        t = c.get("/api/v5/market/ticker", {"instId": inst})[0]
        bk = c.get("/api/v5/market/books", {"instId": inst, "sz": "20"})[0]
        out = {"inst": inst, "time": _t(int(t["ts"])), "last": _r(t["last"]), "open_24h": _r(t["open24h"]), "change_24h": _pct(float(t["last"]), float(t["open24h"])),
               "high_24h": _r(t["high24h"]), "low_24h": _r(t["low24h"]), "turnover_24h_ccy": _r(t.get("volCcy24h"), 0),
               "best_bid": _r(bk["bids"][0][0]), "best_ask": _r(bk["asks"][0][0]),
               "depth20_bid_sz": _r(sum(float(x[1]) for x in bk["bids"]), 0), "depth20_ask_sz": _r(sum(float(x[1]) for x in bk["asks"]), 0)}
        try:
            fr = c.get("/api/v5/public/funding-rate", {"instId": inst})[0]
            out["funding_rate_now"] = _r(fr.get("fundingRate"), 6)
            out["next_funding_time"] = _t(int(fr["fundingTime"]))
            oi = c.get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst})[0]
            out["open_interest_usd"] = _r(oi.get("oiUsd"), 0)
        except Exception:
            pass
        return out

    def rules_status(self, inst: str | None = None) -> dict[str, Any]:
        """Entry rules from discovery with their verdicts and blind-test scores; if `inst` is given, whether each
        rule's condition currently holds for that coin."""
        d = self._json("discover.json") or {"results": []}
        rp = None
        try:
            from okxrev import replay
            rp = replay.compare(self.cfg)
        except Exception:
            rp = None
        blind = {}
        if rp and rp.get("exists"):
            for r in rp["leaderboard"]:
                p = r.get("params") or {}
                if p.get("key"):
                    blind[(p["key"], p.get("exit"))] = {"n": r["C"].get("n"), "day_mean": _r(r["C"].get("day_mean"), 4),
                                                        "vs_random_t": _r((r.get("vs_random") or {}).get("t"), 2),
                                                        "significant": (r.get("vs_random") or {}).get("significant")}
        active = [r for r in d["results"] if r["verdict"] != "無效"]
        now_hits = {}
        if inst:
            from okxrev import discover as D
            inst = norm_inst(inst)
            rules = {x.key: x for x in D.enumerate_rules()}
            f = self.features_at(inst)
            tail = f.iloc[-3:]
            fa = {k: tail[k].to_numpy() for k in tail.columns}
            for r in active:
                rule = rules.get(r["key"])
                if rule is not None:
                    now_hits[r["key"] + "|" + r["exit"]] = bool(rule.mask(fa).any())
        return {"inst": inst, "rules": [{"label": r["label"], "exit": r["exit"], "verdict_discovery": r["verdict"],
                                         "valid_day_mean": _r(r["valid"].get("day_mean"), 4), "holdout_day_mean": _r(r["holdout"].get("day_mean"), 4),
                                         "blind_test": blind.get((r["key"], r["exit"])),
                                         **({"condition_now": now_hits.get(r["key"] + "|" + r["exit"])} if inst else {})} for r in active],
                "note": "盲測（2024-10～2025-09）顯著勝過隨機的只有 rule03/rule04（暴漲在頂＋BTC弱勢＋爆量 做空）"}

    def rule_history(self, inst: str, rule: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Past triggers of the discovered rules on this coin (from live paper trading and the 2024-25 blind test),
        with their outcomes."""
        inst = norm_inst(inst)
        out: dict[str, Any] = {"inst": inst}
        from okxrev import paper, replay
        for name, path in (("live_paper", self.cfg.paper_db), ("blind_test", replay.out_db_path(self.cfg))):
            if not path.exists():
                continue
            try:
                con = duckdb.connect(str(path), read_only=True)
            except Exception:
                continue
            try:
                q = ("SELECT trader_id, side, entry_ts, exit_ts, exit_reason, ret_net FROM positions WHERE inst_id = ? AND book = 'C' "
                     "AND state = 'closed' AND trader_id NOT LIKE 'random%'")
                args: list[Any] = [inst]
                if rule:
                    q += " AND trader_id LIKE ?"
                    args.append(f"%{rule}%")
                df = con.execute(q + " ORDER BY entry_ts DESC", args).df()
            finally:
                con.close()
            out[name] = {"n": int(len(df)), "mean_ret": _r(df["ret_net"].mean(), 4) if len(df) else None,
                         "win_rate": _r((df["ret_net"] > 0).mean(), 3) if len(df) else None,
                         "by_trader": df.groupby("trader_id")["ret_net"].agg(["count", "mean"]).round(4).reset_index().to_dict("records") if len(df) else [],
                         "latest": [[r.trader_id, r.side, _t(r.entry_ts), r.exit_reason, _r(r.ret_net, 4)] for r in df.head(max(1, min(int(limit or 20), 40))).itertuples()]}
        return out

    def pump_events(self, inst: str | None = None, limit: int = 15) -> dict[str, Any]:
        """Historical pump / giant-red-candle events (backtest) with forward returns; optionally for one coin."""
        p = self.cfg.paths.results / "events.csv"
        if not p.exists():
            return {"status": "沒有回測事件"}
        ev = pd.read_csv(p)
        if inst:
            ev = ev[ev["inst_id"] == norm_inst(inst)]
        ev = ev.sort_values("event_ts", ascending=False)
        cols = ["mode", "inst_id", "event_time_utc", "ret_24h_pre", "red_ret_prev_close", "red_vol_ratio", "btc_regime", "fwd_ret_1h", "fwd_ret_6h", "fwd_ret_24h", "mae_24h", "mfe_24h"]
        cols = [c for c in cols if c in ev]
        agg = ev.groupby("mode")["fwd_ret_24h"].agg(["count", "mean"]).round(4).reset_index().to_dict("records") if len(ev) else []
        return {"n": int(len(ev)), "by_mode_24h": agg, "latest": [{k: _r(v, 4) for k, v in r.items()} for r in ev[cols].head(max(1, min(int(limit or 15), 40))).to_dict("records")],
                "note": "fwd_ret 為事件後價格變化（做空要取負號）"}

    def my_trades(self, inst: str | None = None, source: str | None = None, since: Any = None, limit: int = 30) -> dict[str, Any]:
        """Your trade history (manual / bot / copy) with stats; optionally for one coin, a source, and since a date."""
        if not self.allow_private:
            raise ToolError("未授權讀取個人帳戶")
        from okxrev import account
        t = self.trades()
        if t.empty:
            return {"n": 0}
        srcs = (source,) if source in ("manual", "bot", "copy") else None
        f = account.filter_trades(t, _time_arg(since, 0) if since else None, None, None, None if not inst else norm_inst(inst).split("-")[0], None, srcs)
        s = account.summarize_trades(f) if len(f) else {"overall": {"n": 0}}
        cols = [c for c in ("source", "inst_id", "side", "open_ts", "close_ts", "hold_min", "lever", "notional_usd", "ret_net", "pnl_usd", "fee_usd",
                            "close_type", "mae", "mfe", "capture", "post_72", "f_r24h", "f_btc_regime") if c in f]
        rows = f.sort_values("close_ts", ascending=False).head(max(1, min(int(limit or 30), 80)))[cols]
        return {"filter": {"inst": inst, "source": source, "since": since}, "overall": _rec(s["overall"], 4),
                "by_side": [_rec(x, 4) for x in s.get("by_side", [])], "by_source": [_rec(x, 4) for x in s.get("by_source", [])],
                "monthly": [_rec(x, 4) for x in s.get("monthly", [])][-12:],
                "trades": [{k: (_t(v) if k.endswith("_ts") else _r(v, 4)) for k, v in r.items()} for r in rows.to_dict("records")],
                "glossary": "mae/mfe=持倉期間最大逆向/順向幅度；capture=實際拿到最大浮盈的比例；post_72=出場後 6h 價格往原方向走多少；f_r24h=進場前 24h 漲跌"}

    def trading_review(self) -> dict[str, Any]:
        """Diagnosis of your manual trading: issues ranked by impact, strengths, counterfactual exits, and the
        optimized exit rules / cooldown from the exit study."""
        if not self.allow_private:
            raise ToolError("未授權讀取個人帳戶")
        r = self._json("review.json") or {}
        e = self._json("exits.json") or {}
        out = {"n_trades": r.get("n_trades"), "overall": _rec((r.get("summary") or {}).get("overall", {}), 4),
               "issues": [{"title": i["title"], "finding": i["finding"], "impact_usd": _r(i["impact_usd"], 2), "advice": i["advice"]} for i in r.get("issues", [])][:10],
               "strengths": r.get("strengths", [])[:8], "counterfactuals": [_rec(c, 3) for c in r.get("counterfactuals", [])][:8],
               "exit_study": {g: {"recommended": v["recommended"]["text"], "actual_mean": _r(v["actual"]["mean_ret"], 4),
                                  "rule_mean": _r(v["recommended"]["mean_ret"], 4), "oos_mean": _r(v["recommended"]["oos_mean"], 4)}
                              for g, v in (e.get("groups") or {}).items()},
               "cooldown": {k: (e.get("cooldown") or {}).get(k, {}).get("recommended") for k in ("after_loss", "after_win")}}
        return out

    def bots(self, inst: str | None = None) -> dict[str, Any]:
        """Your trading bots (grid, DCA, smart portfolio): per-bot results, market context, and findings."""
        if not self.allow_private:
            raise ToolError("未授權讀取個人帳戶")
        b = self._json("bots.json") or {}
        base = norm_inst(inst).split("-")[0] if inst else None
        live_rows, live_err = self._running_bots()
        if base:
            live_rows = [x for x in live_rows if base in str(x.get("inst_id", ""))]
        live_ids = {x.get("algo_id") for x in live_rows}
        rows = [x for x in b.get("bots", []) if x.get("algo_id") not in live_ids and (not base or base in str(x.get("inst_id", "")))]
        keep = ["type_label", "inst_id", "direction", "lever", "investment", "start", "end", "hours", "state", "pnl", "grid_profit", "float_profit",
                "unrealized", "min_px", "max_px", "mkt_move", "mkt_broke", "pre_ret_24h", "post_after_6h", "stop_reason"]
        return {"running_now_live": live_rows, "running_live_errors": live_err,
                "running_note": "running_now_live 為剛向 OKX 即時查詢的運行中機器人（以此為準）；total_pnl 為 OKX 總損益（含手續費、資金費），"
                                "float_profit 為網格浮動損益（含已部分平倉的價差），pos_upl 只是目前持倉相對持倉均價的未實現損益",
                "history_synced_at": _t(int((self.cfg.paths.results / "bots.json").stat().st_mtime * 1000)) if (self.cfg.paths.results / "bots.json").exists() else None,
                "n_total_history": b.get("n"), "pnl_total_history": _r(b.get("pnl"), 2),
                "findings": b.get("findings", []), "by_direction": b.get("by_direction"),
                "history_bots": [{k: (_t(x[k]) if k in ("start", "end") else _r(x.get(k), 4)) for k in keep if k in x} for x in rows][:30]}

    def _running_bots(self) -> tuple[list[dict[str, Any]], list[str]]:
        if not self.allow_live:
            return [], ["即時資料已停用"]
        from okxrev import live
        try:
            snap = live.running_bots(self.cfg, self.priv())
        except Exception as e:
            return [], [f"{type(e).__name__}: {str(e)[:120]}"]
        rows = [{k: (_t(v) if k == "start" else v if isinstance(v, str) else _r(v, 6)) for k, v in x.items()} for x in snap.get("bots", [])]
        return rows, snap.get("errors", [])

    def positions(self) -> dict[str, Any]:
        """Your current OKX positions (read-only key) with the exit plan: stop / take-profit / trailing levels,
        time stop, liquidation price, and coins held by bots (smart portfolio)."""
        if not self.allow_private or not self.allow_live:
            raise ToolError("未授權讀取即時帳戶")
        from okxrev import live
        r = live.live_plan(self.cfg)
        keep = ["inst_id", "side", "size", "entry_px", "mark_px", "lever", "liq_px", "cur_ret", "peak_ret", "sl_px", "tp_px", "active_stop_px",
                "deadline_ts", "minutes_left", "rule_text", "warnings", "error", "notional_usd", "open_ts"]
        return {"ok": r.get("ok"), "message": r.get("message"),
                "positions": [{k: (_t(p[k]) if k.endswith("_ts") else _r(p[k], 6) if not isinstance(p.get(k), (list, str)) else p[k]) for k in keep if k in p}
                              for p in r.get("positions", [])],
                "bot_holdings": [_rec(h, 6) for h in r.get("bot_holdings", [])],
                "running_bots": [{k: (_t(v) if k == "start" else v if isinstance(v, str) else _r(v, 6)) for k, v in x.items()} for x in r.get("running_bots", [])],
                "cooldown": r.get("cooldown"),
                "note": "positions 只含手動持倉；網格／DCA 的倉位在 running_bots（由機器人管理）"}

    def paper_trading(self, inst: str | None = None, source: str = "live") -> dict[str, Any]:
        """Paper-trading leaderboard (live forward test or the 2024-25 blind test) and, for a coin, its open/closed
        simulated positions."""
        from okxrev import paper, replay
        path = self.cfg.paper_db if source != "blind" else replay.out_db_path(self.cfg)
        if not path.exists():
            return {"status": "沒有資料"}
        con = duckdb.connect(str(path), read_only=True)
        try:
            lb = paper.leaderboard(con)
            pos = pd.DataFrame()
            if inst:
                pos = con.execute("SELECT trader_id, side, state, entry_ts, entry_px, exit_ts, exit_reason, ret_net FROM positions "
                                  "WHERE inst_id = ? AND book = 'C' ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 30", [norm_inst(inst)]).df()
        finally:
            con.close()
        board = [{"trader": r["trader_id"], "kind": r["kind"], "n": r["C"].get("n"), "days": r["C"].get("n_days"), "day_mean": _r(r["C"].get("day_mean"), 4),
                  "win_rate": _r(r["C"].get("win_rate"), 3), "vs_random_t": _r((r.get("vs_random") or {}).get("t"), 2),
                  "significant": (r.get("vs_random") or {}).get("significant")} for r in lb]
        board.sort(key=lambda x: -(x["day_mean"] or -9) if x["n"] else 9)
        return {"source": source, "leaderboard": board[:25],
                "coin_positions": [{k: (_t(v) if k.endswith("_ts") else _r(v, 4)) for k, v in r.items()} for r in pos.to_dict("records")] if inst else None}

    def similar_situations(self, inst: str, scope: str = "market", horizon_h: int = 24, k: int = 30) -> dict[str, Any]:
        """Find past moments (whole market or this coin) whose state is closest to this coin's current state
        (returns, volume, range position, volatility, BTC regime) and summarise what happened next."""
        from okxrev import discover as D
        inst = norm_inst(inst)
        f = self.features_at(inst)
        cur = f.iloc[-1]
        cols = ["r1h", "r6h", "r24h", "r3d", "vmax_1h", "pos24", "pos7d", "dist_ema", "vol_rank"]
        x0 = np.array([float(cur[c]) for c in cols])
        if not np.all(np.isfinite(x0)):
            raise ToolError("目前特徵不完整，無法比對")
        ev_path = self.cfg.paths.results / "discover_events.parquet"
        hk = {1: "1h", 6: "6h", 24: "24h", 72: "3d"}.get(int(horizon_h or 24), "24h")
        lib = self._situation_library()
        if lib is None or lib.empty:
            raise ToolError("相似情境資料庫尚未建立")
        if scope == "coin":
            lib = lib[lib["inst"] == inst]
        lib = lib[lib["regime"] == cur.get("btc_regime", "neutral")] if (lib["regime"] == cur.get("btc_regime")).sum() > 200 else lib
        X = lib[cols].to_numpy(float)
        sd = np.nanstd(X, axis=0)
        sd[sd == 0] = 1
        dist = np.sqrt(np.nansum(((X - x0) / sd) ** 2, axis=1))
        k = max(5, min(int(k or 30), 100))
        idx = np.argsort(dist)[: k * 4]
        picked = lib.iloc[idx].copy()
        picked["dist"] = dist[idx]
        picked["day"] = picked["ts"] // DAY
        picked = picked.drop_duplicates(["inst", "day"]).head(k)
        fwd = picked[f"fwd_{hk}"].dropna()
        return {"inst": inst, "now": {c: _r(cur[c], 4) for c in cols} | {"btc_regime": cur.get("btc_regime")}, "scope": scope, "horizon": hk,
                "n_matches": int(len(picked)), "forward_return": {"mean": _r(fwd.mean(), 4), "median": _r(fwd.median(), 4), "up_rate": _r((fwd > 0).mean(), 3),
                                                                  "p10": _r(fwd.quantile(0.1), 4), "p90": _r(fwd.quantile(0.9), 4)},
                "matches": [[r.inst, _t(r.ts), _r(r.dist, 2), _r(r.r24h, 3), _r(getattr(r, f"fwd_{hk}"), 4)] for r in picked.head(15).itertuples()],
                "matches_columns": ["inst", "time", "distance", "r24h_then", f"price_change_next_{hk}"],
                "note": "依特徵標準化距離挑選；同幣同日只取一筆；報酬為價格變化（未扣成本，做空取負號）"}

    def _situation_library(self) -> pd.DataFrame | None:
        """Hourly sample of features + forward returns across the market (cached to parquet; refreshed weekly in a
        background low-priority process so chat requests never wait on it)."""
        p = self.cfg.paths.results / "situations.parquet"
        stale = not p.exists() or time.time() - p.stat().st_mtime > 7 * 86400
        if stale:
            _spawn_library_build(self.cfg)
        if not p.exists():
            raise ToolError("相似情境資料庫第一次建立中（約 2 分鐘，背景執行），請稍後再問")
        return pd.read_parquet(p)

    def simulate_trade(self, inst: str, side: str, entry_time: Any = None, entry_price: float | None = None, stop_pct: float | None = None,
                       take_profit_pct: float | None = None, hold_hours: float = 24, lever: float = 1) -> dict[str, Any]:
        """What-if on real candles: enter long/short at a time (next 5m open) or price, with optional stop / take-profit
        and max holding time; returns exit, P&L incl. fees, MAE/MFE, liquidation check."""
        inst = norm_inst(inst)
        if side not in ("long", "short"):
            raise ToolError("side 必須是 long 或 short")
        t0 = _time_arg(entry_time, db.now_ms() - int(float(hold_hours or 24) * HOUR) - HOUR)
        hold = max(1, min(float(hold_hours or 24), 24 * 14))
        g = self.candles(inst, t0, t0 + int(hold * HOUR) + 2 * BAR)
        if len(g) < 2:
            raise ToolError("這段時間沒有 K 線")
        e_px = float(entry_price) if entry_price else float(g["open"].iloc[1] if len(g) > 1 else g["open"].iloc[0])
        s = 1 if side == "long" else -1
        fee = float(self.cfg.metrics["fee_per_side"]) + float(self.cfg.metrics["slippage_per_side"])
        lev = max(1.0, float(lever or 1))
        liq = 0.9 / lev if lev > 1 else None
        mae = mfe = 0.0
        exit_px, reason, exit_ts = None, None, None
        for r in g.iloc[1:].itertuples():
            adv = s * (e_px - (r.low if s > 0 else r.high)) / e_px
            fav = s * ((r.high if s > 0 else r.low) - e_px) / e_px
            mae, mfe = max(mae, adv), max(mfe, fav)
            if liq is not None and adv >= liq:
                exit_px, reason, exit_ts = e_px * (1 - s * liq), "強平", r.ts
                break
            if stop_pct and adv >= float(stop_pct):
                exit_px, reason, exit_ts = e_px * (1 - s * float(stop_pct)), "止損", r.ts
                break
            if take_profit_pct and fav >= float(take_profit_pct):
                exit_px, reason, exit_ts = e_px * (1 + s * float(take_profit_pct)), "止盈", r.ts
                break
        if exit_px is None:
            last = g.iloc[-1]
            exit_px, reason, exit_ts = float(last["close"]), "時間到" if len(g) * BAR >= hold * HOUR else "資料結束（尚未到期）", int(last["ts"])
        ret = s * (exit_px / e_px - 1) - 2 * fee
        if reason == "強平":
            ret = -1.0 / lev
        return {"inst": inst, "side": side, "entry_time": _t(int(g["ts"].iloc[1] if len(g) > 1 else g["ts"].iloc[0])), "entry_price": _r(e_px),
                "exit_time": _t(exit_ts), "exit_price": _r(exit_px), "exit_reason": reason, "return_net_unlevered": _r(ret if reason != "強平" else s * (exit_px / e_px - 1), 4),
                "return_on_margin": _r(ret * lev if reason != "強平" else -1.0, 4), "lever": lev, "mae": _r(mae, 4), "mfe": _r(mfe, 4),
                "note": "以 5 分 K 判斷；同一根同時碰到止損與止盈時以止損計；未計資金費"}

    def project_overview(self) -> dict[str, Any]:
        """What data and analyses this project has (coverage, dates, sizes) — use when unsure what is available."""
        from okxrev import heatdata, replay
        out: dict[str, Any] = {}
        try:
            with db.open_db(self.cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=30) as con:
                lo, hi, n = con.execute("SELECT MIN(oldest_ts), MAX(newest_ts), COUNT(*) FROM sync_state WHERE newest_ts IS NOT NULL").fetchone()
                out["candles_5m"] = {"insts": int(n), "from": _t(lo), "to": _t(hi)}
        except Exception as e:
            out["candles_5m"] = str(e)[:80]
        out["data_notes"] = ["K 線：現存 USDT 永續，2025-10 以後在主資料庫，2024-09～2025-09 舊資料（約 190 個老合約）另存，price_summary 會自動合併",
                             "資金費率約 2026-06 之後；OI 小時資料約 2026-09 之後、日資料約一年",
                             "主動買賣量與合約溢價正在補到 2024-09；多空比與爆倉只從 2026-10 開始累積",
                             "已下架的幣沒有資料（回測有存活者偏差）"]
        oldp = replay.old_db_path(self.cfg)
        out["old_candles_blind_test"] = {"period": [replay.rcfg(self.cfg)["start"], replay.rcfg(self.cfg)["end"]],
                                         "size_mb": round(oldp.stat().st_size / 1e6) if oldp.exists() else None}
        try:
            with db.open_db(self.cfg.paths.db, read_only=True, wait_sec=0.5, max_tries=3) as con:
                r = con.execute("SELECT stream, COUNT(*), SUM(done::INT) FROM heat_state WHERE stream LIKE 'fill:%' GROUP BY 1").fetchall()
            out["heat_history_backfill"] = {s: f"{int(d or 0)}/{n}" for s, n, d in r}
        except Exception:
            out["heat_history_backfill"] = "忙碌中"
        try:
            from okxrev import announce
            st = announce.status(self.cfg)
            out["announcements"] = {"from": _t(st.get("from")), "to": _t(st.get("to")), "campaigns": st.get("campaigns")} if st.get("exists") else None
        except Exception:
            out["announcements"] = None
        for name in ("discover.json", "heat.json", "review.json", "exits.json", "bots.json", "summary.json", "campaigns.json"):
            p = self.cfg.paths.results / name
            out[name] = _t(int(p.stat().st_mtime * 1000)) if p.exists() else None
        out["watchlist"] = (self.cfg.raw.get("heat") or {}).get("watchlist")
        out["fine_data"] = "1m K 線約 2 年、1s K 線約 7 個月（新幣從上市起），即時向 OKX 抓取；逐筆成交只有最近 500 筆"
        return out

    # ------------------------------------------------------------ fine data / event lab
    def fine_candles(self, inst: str, bar: str = "1m", start: Any = None, end: Any = None, max_rows: int = 60) -> dict[str, Any]:
        """1s / 1m candles summary + micro-structure metrics (last 1/5/15 minutes of the range)."""
        from okxrev import fine
        inst = norm_inst(inst)
        if bar not in ("1s", "1m"):
            raise ToolError("bar 只能是 1s 或 1m")
        now = db.now_ms()
        end_ms = min(_time_arg(end, now), now)
        dflt = 15 * 60_000 if bar == "1s" else 6 * HOUR
        start_ms = _time_arg(start, end_ms - dflt)
        try:
            df = fine.candles(self.cfg, inst, bar, start_ms, end_ms, client=self.client() if self.allow_live else None)
        except fine.RangeError as e:
            raise ToolError(str(e))
        if df.empty:
            raise ToolError(f"{inst} 這段時間沒有 {bar} 資料（1s 只有約 7 個月、新幣從上市起）")
        m = fine.micro(df, bar)
        step = max(1, int(np.ceil(len(df) / max(10, min(int(max_rows), 120)))))
        agg = df.assign(k=np.arange(len(df)) // step).groupby("k").agg(ts=("ts", "first"), o=("open", "first"), h=("high", "max"), l=("low", "min"),
                                                                       c=("close", "last"), turnover=("vol_quote", "sum"))
        rows = [[_t(r.ts) if bar == "1m" else fine.fmt_t(int(r.ts)), _r(r.o), _r(r.h), _r(r.l), _r(r.c), _r(r.turnover, 0)] for r in agg.itertuples()]
        return {"inst": inst, "bar": bar, "from": fine.fmt_t(start_ms), "to": fine.fmt_t(end_ms), "n_bars": int(len(df)), "rows_per_line": step,
                "columns": ["time", "open", "high", "low", "close", "turnover_usdt"], "rows": rows, "summary": fine.summary_line(m),
                "metrics": _rec({**{k: v for k, v in m.items() if k not in ("windows",)}, **{f"w{k}": v for k, v in m.get("windows", {}).items()}}),
                "times_note": "range_high_at / range_low_at 等為毫秒時間戳（UTC）",
                "range_high_time": fine.fmt_t(m.get("range_high_at")), "range_low_time": fine.fmt_t(m.get("range_low_at"))}

    def recent_trades(self, inst: str) -> dict[str, Any]:
        """Last 500 public trades: buy/sell share, trade rate, big trades."""
        from okxrev import fine
        inst = norm_inst(inst)
        if not self.allow_live:
            raise ToolError("即時資料已停用")
        tr = fine.trades(self.cfg, inst, client=self.client())
        if tr.empty:
            raise ToolError("沒有成交資料")
        f = fine.trade_flow(tr)
        f["big_latest"] = [[fine.fmt_t(t), s, _r(p), _r(z)] for t, s, p, z in f.get("big_latest", [])]
        f["from"], f["to"] = fine.fmt_t(f["from"]), fine.fmt_t(f["to"])
        f["note"] = "OKX 公開成交只提供最近 500 筆；大單 = 名目金額 ≥ 這批平均的 10 倍"
        return {"inst": inst, **_rec(f)}

    def analyze_event(self, inst: str, at: Any) -> dict[str, Any]:
        """Dissect a pump-then-crash around `at`: base / peak / crash times, shape, 1m/1s detail, suggested template."""
        from okxrev import events_lab as L
        inst = norm_inst(inst)
        try:
            a = L.analyze_event(self.cfg, inst, _time_arg(at, db.now_ms()), with_fine=self.allow_live)
        except ValueError as e:
            raise ToolError(str(e))
        out = {"inst": inst, "times": {k: _t(v) for k, v in a["bars"].items()}, "prices": _rec(a["prices"]), "shape": _rec(a["shape"], 4),
               "after_trigger_long_return": _rec(a["after_trigger"], 4), "age_days_at_peak": _r(a.get("age_d_at_peak"), 1),
               "template": a["template"], "template_desc": a["template_desc"]}
        fn = a.get("fine") or {}
        if fn.get("m1"):
            m1 = dict(fn["m1"])
            for k in ("peak_ts", "low_after_ts", "low_before_ts"):
                m1[k] = _t(m1.get(k))
            out["m1"] = _rec(m1, 4)
        if fn.get("s1"):
            out["s1"] = _rec({k: v for k, v in fn["s1"].items() if k != "range"}, 4)
        d = a.get("derivs") or {}
        if d.get("funding"):
            out["funding"] = [[_t(x["ts"]), _r(x["rate"], 6)] for x in d["funding"][-6:]]
        if d.get("oi"):
            oi = d["oi"]
            out["oi_change"] = _pct(oi[-1]["oi_usd"], oi[0]["oi_usd"])
        if d.get("liquidations"):
            liq = pd.DataFrame(d["liquidations"])
            out["liquidations"] = {k: int(v) for k, v in liq.groupby("pos_side")["sz"].count().items()}
        out["note"] = "trigger = 從高點跌到樣板跌幅門檻的第一根 5 分 K（只用當下已知資料）；after_trigger 為下一根開盤做多的報酬（做空取負）"
        return out

    def find_similar_events(self, inst: str, at: Any, k: int = 30) -> dict[str, Any]:
        """Market episodes (5m, 2 years) shaped like this event, with what happened after the trigger."""
        from okxrev import events_lab as L
        inst = norm_inst(inst)
        try:
            a = L.analyze_event(self.cfg, inst, _time_arg(at, db.now_ms()), with_fine=False)
            s = L.find_similar(self.cfg, a, k=max(10, min(int(k), 200)))
        except FileNotFoundError:
            raise ToolError("事件索引尚未建立（事件實驗室 → 建立索引）")
        except ValueError as e:
            raise ToolError(str(e))
        rows = [{"inst": r["inst"], "trigger": _t(r["ts"]), "peak": _t(r["pk_ts"]), "rise": _r(r["rise"], 3), "drop_at_trigger": _r(r["drop"], 3),
                 "short_1h": _r(r["S_1h"], 4), "short_6h": _r(r["S_6h"], 4), "short_24h": _r(r["S_24h"], 4), "period": r["period"],
                 "dist": _r(r["dist"], 2)} for r in s["rows"][:k]]
        return {"event": {"inst": inst, "peak": _t(a["bars"]["peak"]), "trigger": _t(a["bars"]["trigger"]), "shape": _rec(a["shape"], 3)},
                "n": s["n"], "summary_net_after_costs": [_rec(x, 4) for x in s["summary"]], "closest": rows, "note": s["note"] + "；報酬已扣手續費與滑價"}

    def test_template(self, template: dict[str, Any] | None = None, inst: str | None = None, at: Any = None) -> dict[str, Any]:
        """Historical results of a template (discovery + validation periods; never the blind period)."""
        from okxrev import events_lab as L
        if template is None:
            if not inst:
                raise ToolError("請給 template，或給 inst + at 讓系統從事件產生樣板")
            a = L.analyze_event(self.cfg, norm_inst(inst), _time_arg(at, db.now_ms()), with_fine=False)
            template = a["template"]
        try:
            r = L.test_template(self.cfg, template, blind=False, examples=10)
        except FileNotFoundError:
            raise ToolError("事件索引尚未建立")
        except ValueError as e:
            raise ToolError(str(e))
        combos = []
        for c in r["combos"]:
            combos.append({"side": c["side"], "exit": c["exit"], "verdict": c["verdict"],
                           **{f"{p}_{k}": _r((c.get(p) or {}).get(k), 4) for p in ("disc", "valid", "blind") if c.get(p) for k in ("n", "n_days", "day_mean", "win_rate")},
                           "random_valid": _r(c.get("base_valid"), 4)})
        return {"template": r["template"], "desc": r["desc"], "n": r["n"], "n_by_period": r["n_by_period"], "periods": r["periods"],
                "blind_shown": r["blind_shown"], "best_by_discovery": r["best"], "combos": combos,
                "groups": [_rec(g, 4) for g in r["groups"]], "recent_examples": [{"inst": e["inst"], "time": _t(e["ts"]), "ret": _r(e["ret"], 4)} for e in r["examples"][:10]],
                "note": "disc=找參數期、valid=驗證期；盲測期（2024-10～2025-09）只能由使用者在介面上按一次，AI 不會動用"}

    def campaigns(self, inst: str | None = None) -> dict[str, Any]:
        """OKX reward-pool campaigns (瓜分活動): research result (abnormal return by window, placebo, market direction),
        campaigns of one coin (with per-window results) or the recent ones with the return so far."""
        from okxrev import announce, campaigns as CP
        res = CP.load(self.cfg) or {}
        out: dict[str, Any] = {"notes": "超額報酬 = 代幣報酬 − 同齡、同期沒有活動的幣的中位數（對數報酬）；p 為隨機日期對照"}
        if res:
            out["research"] = {"generated_at": res.get("generated_at"), "n_events": res.get("n_events"),
                               "main": {g: {"window": CP.WINDOW_LABEL[m["window"]], "verdict": m["verdict"], "abnormal": _rec(m.get("stats") or {}, 4),
                                            "placebo_p": _r((m.get("placebo") or {}).get("p"), 3)} for g, m in (res.get("main") or {}).items()},
                               "by_window": {g: {CP.WINDOW_LABEL[w]: _rec(v["ab"], 4) for w, v in res["summary"][g].items() if w in CP.WINDOWS}
                                             for g in res.get("summary", {})},
                               "market_direction": {g: [{"split": x["label"], "up": _rec(x["up_raw"], 4), "down": _rec(x["down_raw"], 4), "p": _r(x["p_raw"], 3)}
                                                        for x in r.get("splits", [])] + [{"ols": f"{o['y']}~{o['x']}", "alpha": _r(o["alpha"], 4), "t_alpha": _r(o["t_alpha"], 2),
                                                                                         "beta": _r(o["beta"], 2)} for o in r.get("ols", [])]
                                                    for g, r in (res.get("regimes") or {}).items()},
                               "groups": res.get("groups"), "caveats": res.get("caveats")}
        else:
            out["research"] = "尚未執行研究（瓜分活動分頁按「重跑研究」）"
        if inst:
            inst = norm_inst(inst)
            evs = [e for e in res.get("events", []) if e.get("inst_id") == inst]
            out["coin_history"] = [{"title": e["title"], "kind": announce.KIND_LABEL.get(e["kind"], e["kind"]), "start": _t(e["start_ts"]), "end": _t(e["end_ts"]),
                                    **{f"{CP.WINDOW_LABEL[w]}_超額": _r(e.get(f"{w}_ab"), 4) for w in CP.WINDOWS}} for e in evs]
            out["live"] = [d for d in CP.live_status(self.cfg, days=60) if d["inst_id"] == inst]
        else:
            out["live"] = CP.live_status(self.cfg, days=30)[:20]
        for d in out.get("live", []):
            for k in ("p_time", "start_ts", "end_ts", "dist_ts"):
                d[k] = _t(d.get(k))
            for k in ("ret", "vs_btc", "vs_alts"):
                d[k] = _r(d.get(k), 4)
        return out

    def research(self, topic: str) -> dict[str, Any]:
        """Other studies: market streaks, cross-sectional momentum, taker / premium divergence, listing announcements,
        human-like exits, isolated vs cross margin, shared-capital pool, Simple Earn rates. Compact summary of the stored result."""
        import importlib
        mods = {"market_streak": "okxrev.regime", "cross_section": "okxrev.xsec", "flow_premium": "okxrev.flow", "listings": "okxrev.listings",
                "exits": "okxrev.exit_lab", "margin": "okxrev.margin", "pool": "okxrev.pool", "earn": "okxrev.earn"}
        if topic not in mods:
            raise ToolError(f"topic 必須是 {', '.join(mods)}")
        mod = importlib.import_module(mods[topic])
        r = mod.load(self.cfg)
        if not r:
            return {"status": "尚未執行（更多研究分頁按「重跑」）"}
        out: dict[str, Any] = {"generated_at": r.get("generated_at"), "caveats": r.get("caveats"), "note": "報酬是對數報酬"}
        if topic == "market_streak":
            out["current"] = mod.current(self.cfg)
            per = {}
            for k, p in r["periods"].items():
                ev = {e: {x: _r(v, 4) for x, v in p["events"][e].items() if x != "dates"} for e in ("dn3_h3", "dn4_h1", "dn4_h3", "dn4_h5", "up3_h3", "up4_h3") if e in p["events"]}
                per[k] = {"range": p.get("range"), "events": ev,
                          "runs": {x: _r(p["runs"].get(x), 3) for x in ("up_mean", "up_mean_random", "down_mean", "down_mean_random", "runs_p")} if p.get("runs") else None,
                          "basket_top20": {b: _rec(p.get("basket", {}).get(b) or {}, 4) for b in ("dn3_h3_n20", "dn4_h3_n20")}}
            out["periods"] = per
            out["rules_by_market"] = [{k: x[k] for k in ("label", "exit", "n", "all", "dn3", "up3")} for x in r.get("rules", [])][:15]
        elif topic == "cross_section":
            out["segments"] = r["segments"]
            out["combos"] = [{"lookback_d": c["lookback_d"], "hold_d": c["hold_d"], "side": c["side"], "verdict": c["verdict"],
                              **{s: _r((c.get(s) or {}).get(c["key"]), 4) for s in ("train", "valid", "blind")}} for c in r["combos"]]
        elif topic == "flow_premium":
            out["segments"] = r["segments"]
            out["events"] = [{"name": e["name"], "label": e["label"], "hold_h": e["hold_h"], "verdict": e["verdict"],
                              **{s: {k: _r((e.get(s) or {}).get(k), 4) for k in ("n", "mean", "ctrl_mean", "t_vs_ctrl")} for s in ("train", "valid", "blind")}}
                             for e in r["events"] if e["verdict"] != "資料不足"]
        elif topic == "listings":
            out["types"] = r["types"]
            out["existing"] = {t: {w: {"vs_age_matched": _rec(v[w]["ab"], 4), "placebo": v[w].get("placebo")} for w in v} for t, v in r["existing"].items()}
            out["new"] = {t: {w: {"raw": _rec(v[w]["raw"], 4), "vs_alts": _rec(v[w]["vs_alts"], 4)} for w in v} for t, v in r["new"].items()}
        elif topic == "exits":
            out["summary"] = r["summary"]
            out["improved_rules"] = [{"trader_id": x["trader_id"], "best": x["best"], "improves": x["improves"]} for x in r["rules"] if x["improves"]]
        elif topic == "margin":
            out["setup"] = {k: r.get(k) for k in ("margin_frac", "max_open", "mmr", "levers")}
            out["traders"] = {src: [{"trader_id": x["trader_id"], "n": x["n"], "mean": _r(x["mean"], 4), "mae_p95": _r(x["mae_p95"], 3), "safe_lever": x["safe_lever"],
                                     **{f"{L}x": {"isolated_ret": _r(x["levers"][L]["isolated"]["ret"], 3), "isolated_liq": x["levers"][L]["isolated"]["liquidations"],
                                                  "cross_ret": _r(x["levers"][L]["cross"].get("ret"), 3), "cross_blown": x["levers"][L]["cross"].get("blown_at")}
                                        for L in ("5", "10", "20")}} for x in sorted(rows, key=lambda y: -y["mean"])[:15]] for src, rows in r["sources"].items()}
        elif topic == "pool":
            out["note"] = "報酬是簡單百分比"
            out["params"] = r["params"]
            keys = ("ret", "max_dd", "underwater_days", "worst_week", "worst_30d", "pos_weeks", "trades", "halts", "halves")
            out["sources"] = {src: {"range": s["range"], "runs": [{"k": x["k"], "look_d": x["look"], "main": x["main"], **{k: _r(x.get(k), 4) for k in keys},
                                                                   "random_members": _rec(x["random_members"], 4),
                                                                   **({c: {k: _r(x[c].get(k), 4) for k in keys} for c in ("no_brakes", "all", "random_entries")} if x["main"] else {}),
                                                                   **({"top_members": x.get("by_member", [])[:8], "last_picks": x.get("picks", [])[-4:]} if x["main"] else {})}
                                                                  for x in s["runs"]]} for src, s in r["sources"].items()}
            mlr = r.get("model") or {}
            if mlr.get("blind"):
                b, keys2 = mlr["blind"], keys + ("ret_ex_best_week",)
                out["model_filter"] = {
                    "what": f"每月重訓 LightGBM 替候選訊號打分數（標籤：止損後賺錢），只接分數前 {mlr['params']['main_f']:.0%}，丟進同一套資金池規則；從 {b['oos_start']} 開始逐月樣本外",
                    "runs": {k: {kk: _r(v.get(kk), 4) for kk in keys2} for k, v in (("pool_main", b["runs"]["pool_main"]), ("pool_plus_model", b["runs"]["pool_model"]),
                                                                                  ("all_signals", b["runs"]["all"]), *((f"model_top_{float(f):.0%}", x) for f, x in b["runs"]["model"].items()))},
                    "random_same_size": _rec(b["random_same"], 4), "per_trade": _rec(b["per_trade"], 4),
                    "months": [{k: _r(m.get(k), 4) for k in ("month", "auc", "all_mean", "sel_mean", "n_sel", "n")} for m in b["months"]],
                    "top_features": [{"feature": x["feature"], "gain": _r(x["gain"], 3)} for x in b["importance"][:10]],
                    "live": ({"range": mlr["live"]["range"], "auc": _r(mlr["live"]["auc"], 3), "per_trade": _rec(mlr["live"]["per_trade"], 4),
                              "pool_model_ret": _r(mlr["live"]["runs"]["model"]["ret"], 4), "pool_all_ret": _r(mlr["live"]["runs"]["all"]["ret"], 4),
                              "random_same_size": _rec(mlr["live"]["random_same"], 4)} if mlr.get("live") else None)}
        elif topic == "earn":
            out["note"] = "報酬是簡單百分比；利率是年化；利息假設全部借出（上限）"
            out["range"], out["params"] = r["range"], r["params"]
            b = r["baskets"]
            out["baskets"] = {"start": b["start"], "random_top_n": _rec(b["random"], 4), "random_exit_vs_spike_exit": _rec(b.get("random_exit") or {}, 4),
                              "runs": {k: {kk: _r(v.get(kk), 4) for kk in ("ret", "max_dd", "avg_rate", "pos_periods", "worst_period", "halves", "exits")}
                                       for k, v in b["runs"].items()},
                              "last_pick": b.get("last_pick")}
            sp = r.get("spikes") or {}
            out["rate_spikes"] = {k: _rec(v, 4) if isinstance(v, dict) else v for k in ("n", "coins", "by", "placebo", "after_pump", "no_pump") if (v := sp.get(k)) is not None}
            out["rate_spikes"]["recent"] = (sp.get("recent") or [])[:10]
            out["quintiles"] = _rec(r.get("quintiles") or {}, 4)
            sh = r.get("short") or {}
            if sh.get("runs"):
                out["short"] = {"note": "反向：做空同一籃（1 倍保證金）；short_margin／hedge_margin 付借出利率當成本下限；*_w 是有資金費率的期間；offsets 是晚幾天開始的同一設定",
                                "runs": {k: {kk: _r(v.get(kk), 4) for kk in ("ret", "max_dd", "pos_periods", "worst_period", "halves")} for k, v in sh["runs"].items()},
                                "random_short": _rec(sh["random"], 4), "window": _rec(sh.get("window") or {}, 4),
                                "offsets": [_rec(o, 4) for o in sh.get("offsets") or []]}
        return out

    def custom_rules(self, rule_id: str | None = None) -> dict[str, Any]:
        """Rules saved from the event lab: definition, test results, recent triggers and paper results."""
        from okxrev import custom_rules as CR
        rules = CR.load(self.cfg)
        if rule_id:
            rules = [r for r in rules if r["id"] == rule_id]
        out = []
        for r in rules:
            hits = CR.recent_hits(self.cfg, r["id"], 20)
            closed = [h for h in hits if h.get("ret_net") is not None and not pd.isna(h.get("ret_net"))]
            out.append({"id": r["id"], "name": r["name"], "desc": r.get("desc"), "side": r["side"], "exit": r["exit"], "active": r.get("active", True),
                        "created": _t(r.get("created")), "verdict": CR.verdict_label(r), "result": _rec(r.get("result") or {}, 4),
                        "live_triggers": [{"inst": h["inst_id"], "time": _t(h["bar_ts"]), "state": h.get("state"), "ret": _r(h.get("ret_net"), 4)} for h in hits[:10]],
                        "live_closed": len(closed), "live_mean": _r(float(np.mean([h["ret_net"] for h in closed])), 4) if closed else None})
        return {"n": len(out), "rules": out}



# ---------------------------------------------------------------- situation library
def _spawn_library_build(cfg: Config) -> None:
    from filelock import FileLock, Timeout
    lock = FileLock(str(cfg.paths.results / "situations.lock"), timeout=0)
    try:
        lock.acquire()
        lock.release()
    except Timeout:
        return
    try:
        from okxrev.web.tasks import background_popen, venv_python
        background_popen(cfg, [venv_python(cfg, windowless=True), "-m", "okxrev.chat_tools", "--build-library"])
    except OSError as e:
        log.warning("could not start library build: %s", e)


def build_situation_library(cfg: Config, stride_h: int = 1) -> pd.DataFrame:
    """Hourly snapshots of key features + forward price changes for every coin (live DB period)."""
    from okxrev.detector import btc_features, prepare
    from okxrev.discover import features
    cols = ["r1h", "r6h", "r24h", "r3d", "vmax_1h", "pos24", "pos7d", "dist_ema", "vol_rank"]
    with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
        insts = db.list_candle_insts(con)
        btc = btc_features(db.load_candles(con, cfg.universe["btc_inst"]), cfg.btc_regime)
    rows = []
    for inst in insts:
        with db.open_db(cfg.paths.db, read_only=True, wait_sec=2.0, max_tries=60) as con:
            raw = db.load_candles(con, inst)
        if len(raw) < 2000:
            continue
        g = prepare(raw, cfg.detector, btc)
        f = features(g, cfg.detector, None, None, None)
        c = g["close"].to_numpy(float)
        idx = g.index.to_numpy(np.int64)
        pick = np.flatnonzero(idx % (stride_h * HOUR) == 0)
        pick = pick[pick > 900]
        if not len(pick):
            continue
        d = pd.DataFrame({k: f[k].to_numpy()[pick] for k in cols})
        d["regime"] = f["btc_regime"].to_numpy()[pick]
        d["inst"] = inst
        d["ts"] = idx[pick]
        for name, k in (("1h", 12), ("6h", 72), ("24h", 288), ("3d", 864)):
            j = pick + k
            d[f"fwd_{name}"] = np.where(j < len(c), c[np.clip(j, 0, len(c) - 1)] / c[pick] - 1, np.nan)
        rows.append(d.dropna(subset=cols))
    lib = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    for col in cols + [c for c in lib.columns if c.startswith("fwd_")]:
        lib[col] = lib[col].astype("float32")
    p = cfg.paths.results / "situations.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp.parquet")
    lib.to_parquet(tmp, index=False)
    tmp.replace(p)
    return lib


def _main() -> int:
    import argparse
    import sys
    from filelock import FileLock, Timeout
    from okxrev.config import load_config
    from okxrev.log import setup_logging
    ap = argparse.ArgumentParser(prog="python -m okxrev.chat_tools")
    ap.add_argument("--build-library", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    setup_logging(cfg.paths.logs, "chat", console=False)
    if a.build_library:
        lock = FileLock(str(cfg.paths.results / "situations.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            return 0
        try:
            lib = build_situation_library(cfg)
            log.info("situation library: %d rows", len(lib))
        finally:
            lock.release()
    return 0


# ---------------------------------------------------------------- registry
def _p(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
INST = {"type": "string", "description": "合約代號，例如 CT-USDT-SWAP 或 CT"}
TIME = {"type": "string", "description": "UTC 時間，例如 2026-10-12、2026-10-12T08:00，或 '24h ago'、'3d ago'"}

TOOLS: dict[str, dict[str, Any]] = {
    "project_overview": {"desc": "這個專案有哪些資料與分析、各自涵蓋的時間（不確定能查什麼時先用）", "params": _p({})},
    "market_list": {"desc": "全市場合約清單／過熱排行，可用關鍵字篩選，依過熱分數、24h 漲幅或跌幅排序",
                    "params": _p({"query": S, "sort": {"type": "string", "enum": ["heat", "ret_24h", "drop_24h"]}, "limit": I})},
    "price_summary": {"desc": "某合約某段時間的 K 線摘要（自動彙總成最多 120 根）與統計：漲跌、高低點及時間、回撤、波動、成交額",
                      "params": _p({"inst": INST, "start": TIME, "end": TIME, "bar": {"type": "string", "enum": ["5m", "15m", "1h", "4h", "1d"]}, "max_rows": I}, ["inst"])},
    "indicators": {"desc": "某合約在某時間點（預設現在）的所有規則／模型特徵：各週期報酬、量比、區間位置、ATR、BTC 環境、全市場廣度、資金費率、OI 變化、暴漲/大紅K 旗標",
                   "params": _p({"inst": INST, "at": TIME}, ["inst"])},
    "heat": {"desc": "過熱分數（0-100）、五大類、各指標 30 日百分位、降溫訊號、近幾天走勢、全市場排名",
             "params": _p({"inst": INST, "days": I}, ["inst"])},
    "heat_validation": {"desc": "過熱分數歷史上有沒有預測力（找門檻期＋盲測結果）", "params": _p({})},
    "derivatives": {"desc": "衍生品數據歷史：資金費率、OI、主動買賣量、合約溢價、多空帳戶比、大戶多空比、爆倉",
                    "params": _p({"inst": INST, "hours": I}, ["inst"])},
    "live_market": {"desc": "即時：最新價、24h 高低、掛單簿前 20 檔買賣量、目前資金費率、OI（OKX 公開 API）", "params": _p({"inst": INST}, ["inst"])},
    "rules_status": {"desc": "進場探索找到的規則清單、探索與盲測成績；給 inst 時另外回傳每條規則目前是否成立",
                     "params": _p({"inst": INST})},
    "rule_history": {"desc": "規則交易員過去在這個幣的觸發與結果（即時紙上交易、2024-25 盲測）",
                     "params": _p({"inst": INST, "rule": {"type": "string", "description": "例如 rule04"}, "limit": I}, ["inst"])},
    "pump_events": {"desc": "回測的暴漲／巨大紅 K 事件與事件後 1h/6h/24h 報酬，可指定合約", "params": _p({"inst": INST, "limit": I})},
    "my_trades": {"desc": "使用者的交易紀錄與統計（手動／機器人／跟單），可依合約、來源、起始日期篩選；含 MAE/MFE、出場效率",
                  "params": _p({"inst": INST, "source": {"type": "string", "enum": ["manual", "bot", "copy"]}, "since": TIME, "limit": I})},
    "trading_review": {"desc": "使用者手動交易的診斷：操作問題（依影響排序）、優勢、反事實出場試算、最佳化出場規則、冷卻建議", "params": _p({})},
    "bots": {"desc": "使用者的機器人：運行中的網格／DCA 即時狀態（OKX 即時查詢：損益、網格收益、浮動損益、區間、持倉均價、張數、強平價、保證金率），以及歷史機器人逐一結果與歸納發現，可指定合約",
             "params": _p({"inst": INST})},
    "positions": {"desc": "使用者目前在 OKX 的真實持倉（唯讀）與出場計畫：止損/止盈/移動停利價、時間停損、強平價、機器人持幣、冷卻狀態", "params": _p({})},
    "paper_trading": {"desc": "紙上交易排行（source=live 即時；blind 為 2024-25 舊資料盲測），給 inst 時附上該幣的模擬單",
                      "params": _p({"inst": INST, "source": {"type": "string", "enum": ["live", "blind"]}})},
    "similar_situations": {"desc": "找出歷史上跟這個幣「現在狀態」最相似的時間點（全市場或只看這個幣），統計之後 1h/6h/24h/3d 的走勢",
                           "params": _p({"inst": INST, "scope": {"type": "string", "enum": ["market", "coin"]}, "horizon_h": {"type": "integer", "enum": [1, 6, 24, 72]}, "k": I}, ["inst"])},
    "simulate_trade": {"desc": "用真實 K 線試算：某時間（或價格）做多/做空，加止損、止盈、最長持有、槓桿，結果如何",
                       "params": _p({"inst": INST, "side": {"type": "string", "enum": ["long", "short"]}, "entry_time": TIME, "entry_price": N,
                                     "stop_pct": {"type": "number", "description": "例如 0.03 = 3%"}, "take_profit_pct": N, "hold_hours": N, "lever": N},
                                    ["inst", "side"])},
    "fine_candles": {"desc": "1 秒或 1 分 K 線摘要與微結構：最近 1/5/15 分鐘漲跌、量比、影線、離 VWAP、最大量與最大單根漲跌（1s 最長 2 小時、1m 最長 3 天）",
                     "params": _p({"inst": INST, "bar": {"type": "string", "enum": ["1s", "1m"]}, "start": TIME, "end": TIME, "max_rows": I}, ["inst"])},
    "recent_trades": {"desc": "即時逐筆成交（最近 500 筆）：主動買賣比例、每秒成交筆數、大單買賣", "params": _p({"inst": INST}, ["inst"])},
    "analyze_event": {"desc": "拆解某個急漲後爆跌事件：起漲、高點、崩跌時間、漲跌幅與速度、1m/1s 細節、資金費率/OI/爆倉，並產生可測試的樣板",
                      "params": _p({"inst": INST, "at": TIME}, ["inst", "at"])},
    "find_similar_events": {"desc": "在全市場 2 年 5 分 K 中找形狀相似的事件，統計觸發後做空/做多 1h/6h/24h 的淨報酬並與隨機比較",
                            "params": _p({"inst": INST, "at": TIME, "k": I}, ["inst", "at"])},
    "test_template": {"desc": "測試樣板（急漲後爆跌條件）的歷史成績：多空 × 多種出場，找參數期與驗證期分開，含依成交額/BTC 環境分組；不會動用盲測期",
                      "params": _p({"template": {"type": "object", "description": "欄位：rise_min, rise_max, rise_bars_max(5分K根數), drop_min, drop_max, drop_bars_max, retrace_min, vol_min, ret_bar_max, turnover_min, turnover_max, age_max_d, btc(陣列 weak/neutral/strong), cooldown"},
                                    "inst": INST, "at": TIME})},
    "campaigns": {"desc": "OKX 瓜分活動（閃賺／空投賺幣／交易賺幣）：活動代幣在各時段相對同齡幣的超額報酬研究、隨機日期對照、大盤方向分組；給 inst 時列出這個幣的歷次活動與結果，否則列出最近的活動與到目前為止的報酬",
                  "params": _p({"inst": INST})},
    "research": {"desc": "其他研究結果：market_streak（大盤連漲連跌與連跌後反彈）、cross_section（橫斷面動能／反轉）、flow_premium（主動買賣與合約溢價背離）、listings（上幣／下架公告）、exits（人性化出場 vs 時間出場）、margin（全倉／逐倉與安全槓桿）、pool（共用資金池：交易員依過去績效輪流進池，對照隨機挑成員；另含模型篩選：逐筆打分數只接高分訊號）、earn（簡單賺幣利率：買年化高的幣放賺幣能不能補跌幅、反向做空這些幣、年化暴衝之後的走勢）",
                 "params": _p({"topic": {"type": "string", "enum": ["market_streak", "cross_section", "flow_premium", "listings", "exits", "margin", "pool", "earn"]}}, ["topic"])},
    "custom_rules": {"desc": "使用者在事件實驗室存的自訂規則：定義、測試與盲測結果、即時觸發與紙上交易結果", "params": _p({"rule_id": S})},
}


def schemas() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": k, "description": v["desc"], "parameters": v["params"]}} for k, v in TOOLS.items()]


def call(box: Toolbox, name: str, args: dict[str, Any], max_chars: int = 12000) -> tuple[str, bool]:
    """Run a tool; return (json text truncated to max_chars, ok)."""
    if name not in TOOLS:
        return json.dumps({"error": f"沒有這個工具：{name}"}, ensure_ascii=False), False
    fn: Callable[..., dict[str, Any]] = getattr(box, name)
    allowed = set(TOOLS[name]["params"]["properties"])
    kwargs = {k: v for k, v in (args or {}).items() if k in allowed and v not in (None, "")}
    try:
        res = fn(**kwargs)
        ok = True
    except ToolError as e:
        res, ok = {"error": str(e)}, False
    except db.DbBusy:
        res, ok = {"error": "資料庫忙碌中，請稍後再試"}, False
    except Exception as e:
        log.exception("tool %s failed", name)
        res, ok = {"error": f"{type(e).__name__}: {str(e)[:200]}"}, False
    text = json.dumps(res, ensure_ascii=False, default=lambda o: _r(o) if not isinstance(o, (list, dict)) else str(o))
    if len(text) > max_chars:
        text = text[: max_chars - 60] + f'..."（已截斷，原長 {len(text)} 字元，請縮小範圍）"'
    return text, ok


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main())
