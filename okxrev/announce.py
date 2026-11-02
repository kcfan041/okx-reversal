"""OKX announcements and reward-pool campaigns (瓜分活動).

sync_announce  Called from every scheduled sync (time-capped, at most every `announce.poll_min` minutes): the first
               page of every announcement type in English (used for parsing) and Traditional Chinese (display), then
               a few pages of the one-off history backfill, then the pages of campaigns not parsed yet.
backfill       Runs the whole history at once (CLI / web); resumable, ~250 requests.

A campaign is an English 'latest-events' announcement whose title shares a reward pool ("... share 1,000,000 CT").
From the title we take the featured tokens and the kind (Flash / Airdrop Earn = rewards paid while it runs; Trade-to-
Earn = rewards paid after it ends); from the page we take the campaign period, the pre-subscription start and the
reward distribution time. Parsing problems are kept with a status so they show up in the web page, and
data/campaign_overrides.json ({url: {"tokens": [...], "start": "...", "end": "...", "skip": true}}) fixes them by hand.

Storage: data/announce.duckdb (announcements, campaigns, announce_state); the sync is its only writer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as _html
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from okxrev import db
from okxrev.config import Config, _resolve, load_config, ms_to_str, parse_utc_ms
from okxrev.log import setup_logging
from okxrev.okx_client import BudgetExceeded, OkxClient, OkxError, RetryExhausted

log = logging.getLogger("okxrev.announce")

HOUR = 3_600_000
DAY = 86_400_000
TYPES_PATH = "/api/v5/support/announcement-types"
LIST_PATH = "/api/v5/support/announcements"
LANGS = ("en-US", "zh-HK")
EVENTS = "latest-events"
EXCLUDE_TOKENS = {"USDT", "USDC", "USD", "USDG", "RLUSD", "BTC", "ETH", "OKB", "SOL", "XRP", "OKSOL", "XSPY"}
KIND_LABEL = {"flash": "閃賺", "flash_lite": "閃賺 Lite", "airdrop_earn": "空投賺幣", "t2e": "交易賺幣",
              "spot_t2e": "現貨交易賺幣", "perp_t2e": "合約交易賺幣", "other": "其他"}
TRADE_KINDS = ("t2e", "spot_t2e", "perp_t2e")

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    url VARCHAR PRIMARY KEY, lang VARCHAR, ann_type VARCHAR, title VARCHAR, p_time BIGINT, biz_time BIGINT, fetched BIGINT
);
CREATE TABLE IF NOT EXISTS campaigns (
    url VARCHAR PRIMARY KEY, p_time BIGINT, kind VARCHAR, title VARCHAR, tokens VARCHAR, start_ts BIGINT, end_ts BIGINT,
    pre_sub_ts BIGINT, dist_ts BIGINT, pool VARCHAR, period_raw VARCHAR, status VARCHAR, parsed_at BIGINT
);
CREATE TABLE IF NOT EXISTS announce_state (key VARCHAR PRIMARY KEY, value VARCHAR);
"""


def acfg(cfg: Config) -> dict[str, Any]:
    return {"enabled": True, "db": "data/announce.duckdb", "poll_min": 30, "max_sec": 30, **(cfg.raw.get("announce") or {})}


def db_path(cfg: Config) -> Path:
    return _resolve(acfg(cfg)["db"], cfg.paths.root)


def connect(cfg: Config, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = db_path(cfg)
    if read_only and not p.exists():
        raise FileNotFoundError(str(p))
    con = db.connect(p, read_only=read_only, wait_sec=1.0, max_tries=30) if read_only else _connect_rw(p)
    return con


def _connect_rw(p: Path) -> duckdb.DuckDBPyConnection:
    p.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for _ in range(30):
        try:
            con = duckdb.connect(str(p))
            con.execute(SCHEMA)
            return con
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            if not db._is_lock_error(e):
                raise
            last = e
            time.sleep(1.0)
    raise db.DbBusy(str(last))


# ---------------------------------------------------------------- parsing
MON = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DMY = re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]{2,8})\.?,?\s*(\d{4})?,?\s*(?:(\d{1,2}):(\d{2})\s*(am|pm)?)?", re.I)
_MDY = re.compile(r"(?:(\d{1,2}):(\d{2})\s*(am|pm)?\s*(?:UTC)?\s*(?:on\s*)?)?\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b,?\s*(\d{4})?,?\s*"
                  r"(?:at\s*)?(?:(\d{1,2}):(\d{2})\s*(am|pm)?)?", re.I)


def _hm(h: str | None, m: str | None, ap: str | None) -> tuple[int, int]:
    hh, mm = int(h or 0), int(m or 0)
    if ap and ap.lower() == "pm" and hh < 12:
        hh += 12
    if ap and ap.lower() == "am" and hh == 12:
        hh = 0
    return hh, mm


def parse_dates(text: str, year: int) -> list[int]:
    """Every date/time in a line as UTC ms, in order of appearance. Handles '13 May 2026, 18:00', 'October 29, 2026,
    10:00', '11:00 am UTC on September 18, 2025'; a missing year is `year`; '(UTC+8)' anywhere shifts all by -8h."""
    off = 8 * HOUR if re.search(r"UTC\s*\+\s*8", text) else 0
    out: list[int] = []
    for m in _DMY.finditer(text):
        mon = m.group(2)[:3].lower()
        if mon in MON:
            hh, mm = _hm(m.group(4), m.group(5), m.group(6))
            out.append(_ms(int(m.group(3) or year), MON[mon], int(m.group(1)), hh, mm) - off)
    if len(out) >= 2:
        return out
    out = []
    for m in _MDY.finditer(text):
        mon = m.group(4)[:3].lower()
        if mon not in MON:
            continue
        hh, mm = _hm(m.group(1), m.group(2), m.group(3)) if m.group(1) else _hm(m.group(7), m.group(8), m.group(9))
        out.append(_ms(int(m.group(6) or year), MON[mon], int(m.group(5)), hh, mm) - off)
    return out


def _ms(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return int(dt.datetime(y, mo, d, min(h, 23), min(mi, 59), tzinfo=dt.timezone.utc).timestamp() * 1000)


def page_text(html: str) -> str:
    """Readable text of an OKX help page (the <article> part, one line per block)."""
    i = html.find("<article")
    t = html[i:] if i >= 0 else html
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", t, flags=re.S)
    t = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</tr>|</td>|</div>", "\n", t)
    t = _html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


PERIOD_PATTERNS = [r"campaign period[^\n:]*:([^\n]+)", r"\n\s*Time:([^\n]+)", r"(?:event|campaign) time[^\n:]*:([^\n]+)", r"\bperiod[^\n:]*:([^\n]+)"]


def parse_period(text: str, year: int) -> tuple[int | None, int | None, str | None]:
    for pat in PERIOD_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            ds = parse_dates(m.group(1), year)
            if len(ds) >= 2:
                a, b = ds[0], ds[1]
                if b < a:
                    b = _add_year(b)
                return a, b, m.group(1).strip()[:160]
    return None, None, None


def _add_year(ms: int) -> int:
    d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return int(d.replace(year=d.year + 1).timestamp() * 1000)


def _first_date(text: str, pat: str, year: int) -> int | None:
    m = re.search(pat, text, re.I)
    if not m:
        return None
    ds = parse_dates(m.group(1), year)
    return ds[0] if ds else None


_TICK = r"[A-Z0-9]{2,10}"


def parse_tokens(title: str) -> list[str]:
    """Featured tokens from a campaign title, majors and stablecoins removed, in order."""
    toks: list[str] = []
    m = re.search(r"OKX\s*[x×]\s*([A-Za-z0-9]+)(?:\s*\((" + _TICK + r")\))?", title)
    if m:
        toks.append(m.group(2) or m.group(1))
    toks += re.findall(r"\((" + _TICK + r")\)", title)
    m = re.search(r"Get started with (" + _TICK + r"(?:\s*(?:,|&|and)\s*" + _TICK + r")+)\b", title)
    if m:
        toks += re.split(r"\s*(?:,|&|and)\s*", m.group(1))
    m = re.search(r"[Ss]hare (?:a )?[\d,.]+ (" + _TICK + r")\b", title)
    if m:
        toks.append(m.group(1))
    return [t for t in dict.fromkeys(toks) if re.fullmatch(_TICK, t) and t not in EXCLUDE_TOKENS]


def parse_kind(title: str) -> str:
    if re.search(r"Perpetual Trade", title, re.I):
        return "perp_t2e"
    if re.search(r"Spot Trade|Trading Competition", title, re.I):
        return "spot_t2e"
    if re.search(r"Trade[- ]to[- ]Earn", title, re.I):
        return "t2e"
    if re.search(r"Airdrop Earn", title, re.I):
        return "airdrop_earn"
    if re.search(r"Flash Earn Lite", title, re.I):
        return "flash_lite"
    if re.search(r"Flash Earn", title, re.I):
        return "flash"
    return "other"


def is_campaign(ann_type: str, title: str, lang: str = "en-US") -> bool:
    return lang == "en-US" and ann_type == EVENTS and bool(re.search(r"\bshare\b", title, re.I))


def parse_campaign(title: str, p_time: int, text: str | None) -> dict[str, Any]:
    year = dt.datetime.fromtimestamp(p_time / 1000, dt.timezone.utc).year
    kind = parse_kind(title)
    toks = parse_tokens(title)
    out: dict[str, Any] = {"kind": kind, "tokens": toks, "start_ts": None, "end_ts": None, "pre_sub_ts": None, "dist_ts": None,
                           "pool": None, "period_raw": None}
    m = re.search(r"[Ss]hare (?:a )?([\d,.]+ [A-Za-z0-9]+)", title)
    out["pool"] = m.group(1) if m else None
    if text:
        a, b, raw = parse_period(text, year)
        out.update(start_ts=a, end_ts=b, period_raw=raw)
        out["pre_sub_ts"] = _first_date(text, r"start subscribing from ([^\n(]+)", year)
        out["dist_ts"] = _first_date(text, r"distributed (?:by|on|before) ([^\n]+)", year)
    if kind == "other":
        out["status"] = "skip"
    elif not toks:
        out["status"] = "no_token"
    elif text is None:
        out["status"] = "no_page"
    elif out["start_ts"] is None:
        out["status"] = "no_period"
    else:
        out["status"] = "ok"
    return out


# ---------------------------------------------------------------- fetching
def fetch_types(client: OkxClient) -> list[str]:
    return [d["annType"] for d in client.get(TYPES_PATH, {})]


def fetch_page(client: OkxClient, ann_type: str, page: int, lang: str) -> tuple[list[dict[str, Any]], int]:
    data = client.get(LIST_PATH, {"annType": ann_type, "page": str(page)}, headers={"Accept-Language": lang})
    if not data:
        return [], 0
    blk = data[0]
    return list(blk.get("details") or []), int(blk.get("totalPage") or 0)


def fetch_html(client: OkxClient, url: str) -> str:
    client._check_budget()
    client.limiter.acquire()
    client.n_requests += 1
    r = client.session.get(url, timeout=client.timeout, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"})
    r.raise_for_status()
    return r.text


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- store
def get_state(con: duckdb.DuckDBPyConnection, key: str, default: Any = None) -> Any:
    r = con.execute("SELECT value FROM announce_state WHERE key = ?", [key]).fetchone()
    return json.loads(r[0]) if r else default


def set_state(con: duckdb.DuckDBPyConnection, key: str, value: Any) -> None:
    con.execute("INSERT OR REPLACE INTO announce_state VALUES (?, ?)", [key, json.dumps(value)])


def upsert(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], ann_type: str, lang: str, now: int) -> int:
    """Insert announcements; returns how many were new."""
    new = 0
    for d in rows:
        url = d.get("url")
        if not url:
            continue
        if not con.execute("SELECT 1 FROM announcements WHERE url = ?", [url]).fetchone():
            new += 1
        con.execute("INSERT OR REPLACE INTO announcements VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [url, lang, d.get("annType") or ann_type, d.get("title"), _int(d.get("pTime")), _int(d.get("businessPTime")), now])
        if is_campaign(d.get("annType") or ann_type, d.get("title") or "", lang):
            con.execute("INSERT INTO campaigns (url, p_time, kind, title, status) VALUES (?, ?, ?, ?, 'pending') ON CONFLICT DO NOTHING",
                        [url, _int(d.get("pTime")), parse_kind(d.get("title") or ""), d.get("title")])
    return new


def load_overrides(cfg: Config) -> dict[str, dict[str, Any]]:
    p = cfg.paths.root / "data" / "campaign_overrides.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("campaign_overrides.json is not valid JSON")
        return {}


def apply_override(c: dict[str, Any], ov: dict[str, Any] | None) -> dict[str, Any]:
    if not ov:
        return c
    c = dict(c)
    if ov.get("skip"):
        c["status"] = "skip"
        return c
    if ov.get("tokens") is not None:
        c["tokens"] = [str(t).upper() for t in ov["tokens"]]
    for k in ("start", "end"):
        if ov.get(k):
            c[f"{k}_ts"] = parse_utc_ms(ov[k])
    if ov.get("kind"):
        c["kind"] = ov["kind"]
    if c.get("tokens") and c.get("start_ts") and c.get("end_ts") and c.get("status") != "skip":
        c["status"] = "ok"
    c["override"] = True
    return c


def save_campaign(con: duckdb.DuckDBPyConnection, url: str, title: str, p_time: int, c: dict[str, Any], now: int) -> None:
    con.execute("INSERT OR REPLACE INTO campaigns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [url, p_time, c["kind"], title, ",".join(c.get("tokens") or []), c.get("start_ts"), c.get("end_ts"), c.get("pre_sub_ts"),
                 c.get("dist_ts"), c.get("pool"), c.get("period_raw"), c["status"], now])


# ---------------------------------------------------------------- sync
class Syncer:
    def __init__(self, cfg: Config, client: OkxClient, con: duckdb.DuckDBPyConnection, deadline: float | None):
        self.cfg, self.client, self.con, self.deadline = cfg, client, con, deadline
        self.stats = {"pages": 0, "new": 0, "parsed": 0, "errors": 0}

    def time_left(self) -> bool:
        return self.deadline is None or time.monotonic() < self.deadline

    def types(self) -> list[str]:
        ts = get_state(self.con, "types")
        if not ts:
            ts = fetch_types(self.client)
            set_state(self.con, "types", ts)
        return ts

    def poll_first_pages(self, now: int) -> None:
        for t in self.types():
            for lang in LANGS:
                if not self.time_left():
                    return
                rows, total = fetch_page(self.client, t, 1, lang)
                self.stats["pages"] += 1
                self.stats["new"] += upsert(self.con, rows, t, lang, now)
                bf = get_state(self.con, f"bf:{t}:{lang}")
                if bf is None:
                    set_state(self.con, f"bf:{t}:{lang}", {"next": 2, "total": total, "done": total <= 1})

    def backfill_pages(self, now: int, max_pages: int | None = None) -> bool:
        """Older pages of every (type, language); True when everything is done."""
        n = 0
        for t in self.types():
            for lang in LANGS:
                key = f"bf:{t}:{lang}"
                st = get_state(self.con, key) or {"next": 1, "total": None, "done": False}
                while not st["done"]:
                    if not self.time_left() or (max_pages is not None and n >= max_pages):
                        return False
                    rows, total = fetch_page(self.client, t, int(st["next"]), lang)
                    n += 1
                    self.stats["pages"] += 1
                    self.stats["new"] += upsert(self.con, rows, t, lang, now)
                    st = {"next": int(st["next"]) + 1, "total": total, "done": not rows or int(st["next"]) >= total}
                    set_state(self.con, key, st)
        return True

    def parse_pending(self, now: int, max_pages: int | None = None) -> None:
        """Fetch and parse campaign pages: new ones, and ones that failed (retried at most daily)."""
        ov = load_overrides(self.cfg)
        rows = self.con.execute("SELECT url, title, p_time, status, parsed_at FROM campaigns ORDER BY p_time DESC").fetchall()
        n = 0
        for url, title, p_time, status, parsed_at in rows:
            if status in ("ok", "skip", "no_token") and url not in ov:
                continue
            if status not in ("pending", None) and parsed_at and now - int(parsed_at) < DAY and url not in ov:
                continue
            if not self.time_left() or (max_pages is not None and n >= max_pages):
                return
            c = parse_campaign(title, int(p_time), None)
            text = None
            if c["status"] != "skip" and (c["tokens"] or url in ov):
                try:
                    text = page_text(fetch_html(self.client, url))
                    n += 1
                except BudgetExceeded:
                    return
                except Exception as e:
                    self.stats["errors"] += 1
                    log.info("campaign page %s: %s", url, e)
            c = apply_override(parse_campaign(title, int(p_time), text), ov.get(url))
            save_campaign(self.con, url, title, int(p_time), c, now)
            self.stats["parsed"] += 1


def sync_announce(cfg: Config, client: OkxClient, deadline: float | None = None, max_sec: float | None = None,
                  force: bool = False) -> dict[str, Any]:
    ac = acfg(cfg)
    if not ac.get("enabled", True):
        return {"skipped": "disabled"}
    max_sec = float(ac.get("max_sec", 30)) if max_sec is None else max_sec
    cap = time.monotonic() + max_sec
    deadline = cap if deadline is None else min(deadline, cap)
    now = db.now_ms()
    try:
        con = connect(cfg)
    except db.DbBusy:
        log.warning("announce sync skipped: database busy")
        return {"skipped": "busy"}
    try:
        last = get_state(con, "last_poll", 0)
        if not force and now - int(last or 0) < int(ac.get("poll_min", 30)) * 60_000:
            return {"skipped": "recent"}
        s = Syncer(cfg, client, con, deadline)
        try:
            s.poll_first_pages(now)
            set_state(con, "last_poll", now)
            s.parse_pending(now)
            s.backfill_pages(now)
            s.parse_pending(now)
        except (BudgetExceeded, RetryExhausted):
            pass
        except OkxError as e:
            s.stats["errors"] += 1
            log.warning("announce sync: %s", e)
        log.info("announce sync: %s", s.stats)
        return s.stats
    finally:
        con.close()


def backfill(cfg: Config, max_minutes: float | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    client = OkxClient(cfg.okx, deadline=deadline)
    con = connect(cfg)
    try:
        now = db.now_ms()
        s = Syncer(cfg, client, con, deadline)
        try:
            s.poll_first_pages(now)
            set_state(con, "last_poll", now)
            done = s.backfill_pages(now)
            s.parse_pending(now)
        except (BudgetExceeded, RetryExhausted):
            done = False
        return {**s.stats, "done": done}
    finally:
        con.close()
        client.close()


# ---------------------------------------------------------------- reading
def load_campaigns(cfg: Config, status: str | None = "ok") -> pd.DataFrame:
    """Campaign rows (one per announcement) with Chinese titles attached; empty frame when there is no database."""
    cols = ["url", "p_time", "kind", "title", "tokens", "start_ts", "end_ts", "pre_sub_ts", "dist_ts", "pool", "period_raw", "status", "parsed_at", "title_zh"]
    try:
        con = connect(cfg, read_only=True)
    except (FileNotFoundError, db.DbBusy):
        return pd.DataFrame(columns=cols)
    try:
        q = "SELECT * FROM campaigns" + (" WHERE status = ?" if status else "") + " ORDER BY p_time DESC"
        df = con.execute(q, [status] if status else []).df()
        zh = con.execute("SELECT p_time, title FROM announcements WHERE lang = 'zh-HK' AND ann_type = ?", [EVENTS]).df()
    except duckdb.Error:
        return pd.DataFrame(columns=cols)
    finally:
        con.close()
    zh = zh.groupby("p_time")["title"].agg(lambda s: s.iloc[0] if len(s) == 1 else None)
    df["title_zh"] = df["p_time"].map(zh)
    for c in ("start_ts", "end_ts", "pre_sub_ts", "dist_ts", "parsed_at"):
        df[c] = df[c].astype("float64")
    df = df.astype({c: object for c in ("tokens", "pool", "period_raw", "title_zh")}).where(df.notna(), None)
    for c in ("start_ts", "end_ts", "pre_sub_ts", "dist_ts", "parsed_at"):
        df[c] = df[c].astype("float64")
    return df


def campaign_tokens(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (campaign, token) with inst_id and group ('earn' paid while running, 'trade' paid after)."""
    rows = []
    for r in df.itertuples(index=False):
        for t in str(r.tokens or "").split(","):
            if t:
                rows.append({**r._asdict(), "token": t, "inst_id": f"{t}-USDT-SWAP", "grp": "trade" if r.kind in TRADE_KINDS else "earn"})
    return pd.DataFrame(rows)


def recent(cfg: Config, n: int = 50, lang: str = "zh-HK", ann_type: str | None = None) -> list[dict[str, Any]]:
    try:
        con = connect(cfg, read_only=True)
    except (FileNotFoundError, db.DbBusy):
        return []
    try:
        q = "SELECT ann_type, title, p_time, url FROM announcements WHERE lang = ?" + (" AND ann_type = ?" if ann_type else "") + " ORDER BY p_time DESC LIMIT ?"
        return con.execute(q, [lang, ann_type, n] if ann_type else [lang, n]).df().to_dict("records")
    finally:
        con.close()


def status(cfg: Config) -> dict[str, Any]:
    try:
        con = connect(cfg, read_only=True)
    except FileNotFoundError:
        return {"exists": False}
    except db.DbBusy:
        return {"exists": True, "busy": True}
    try:
        n = dict(con.execute("SELECT lang, COUNT(*) FROM announcements GROUP BY 1").fetchall())
        lo, hi = con.execute("SELECT MIN(p_time), MAX(p_time) FROM announcements").fetchone()
        st = dict(con.execute("SELECT status, COUNT(*) FROM campaigns GROUP BY 1").fetchall())
        bf = [get_state(con, k) for (k,) in con.execute("SELECT key FROM announce_state WHERE key LIKE 'bf:%'").fetchall()]
        last = get_state(con, "last_poll")
        bad = con.execute("SELECT url, title, status, period_raw FROM campaigns WHERE status NOT IN ('ok', 'skip', 'no_token') ORDER BY p_time DESC").df()
    finally:
        con.close()
    return {"exists": True, "announcements": n, "from": lo, "to": hi, "campaigns": st, "last_poll": last,
            "backfill_done": bool(bf) and all(b and b.get("done") for b in bf), "problems": bad.to_dict("records")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m okxrev.announce")
    ap.add_argument("--config", default=None)
    ap.add_argument("--backfill", action="store_true", help="download every announcement page and parse campaigns")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--reparse", action="store_true", help="parse every campaign page again (after a parser fix or override)")
    ap.add_argument("--campaigns", action="store_true", help="list parsed campaigns")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs, "announce")
    if args.reparse:
        con = connect(cfg)
        try:
            con.execute("UPDATE campaigns SET status = 'pending', parsed_at = NULL")
        finally:
            con.close()
    if args.backfill or args.reparse:
        print(backfill(cfg, args.max_minutes))
    if args.campaigns:
        df = load_campaigns(cfg, status=None)
        for r in df.itertuples(index=False):
            print(f"{ms_to_str(r.p_time)}  {r.status:9s} {r.kind:12s} {str(r.tokens or '-'):14s} "
                  f"{ms_to_str(r.start_ts) if pd.notna(r.start_ts) else '-':16s} ~ {ms_to_str(r.end_ts) if pd.notna(r.end_ts) else '-':16s}  {r.title[:70]}")
    st = status(cfg)
    print({k: v for k, v in st.items() if k != "problems"})
    for p in st.get("problems", []):
        print(f"  [{p['status']}] {p['title'][:80]}  {p.get('period_raw') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
