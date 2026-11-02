import json

import pytest

from okxrev import announce as A
from okxrev.config import Paths, load_config, parse_utc_ms


def ms(s):
    return parse_utc_ms(s)


@pytest.mark.parametrize("line,year,want", [
    ("October 29, 2026, 10:00 to November 4, 2026, 10:00 (UTC)", 2026, ["2026-10-29T10:00", "2026-11-04T10:00"]),
    ("October 28, 2026 07:00 – November 3, 2026 07:00 (UTC)*", 2026, ["2026-10-28T07:00", "2026-11-03T07:00"]),
    ("11:00 am UTC on September 18, 2025 - 11:00 am UTC on September 21, 2025", 2025, ["2025-09-18T11:00", "2025-09-21T11:00"]),
    ("22 May 2026, 10:00 to 1 Jun 2026, 10:00 (UTC)", 2026, ["2026-05-22T10:00", "2026-06-01T10:00"]),
    ("13 May 2026, 18:00 to 23 May 2026, 18:00 (UTC+8)", 2026, ["2026-05-13T10:00", "2026-05-23T10:00"]),
    ("Dec 30, 11:00 pm to Jan 2, 2027, 01:00", 2026, ["2026-12-30T23:00", "2027-01-02T01:00"]),
])
def test_parse_dates(line, year, want):
    assert A.parse_dates(line, year) == [ms(w) for w in want]


def test_parse_period_picks_campaign_line_and_rolls_year():
    text = "Published on Dec 20, 2026\nDay 1 (Dec 30, 10:00–16:00 UTC)\nCampaign Period: Dec 30, 10:00 to Jan 3, 10:00 (UTC)\n"
    a, b, raw = A.parse_period(text, 2026)
    assert (a, b) == (ms("2026-12-30T10:00"), ms("2027-01-03T10:00"))
    assert raw.startswith("Dec 30")
    assert A.parse_period("no dates here", 2026) == (None, None, None)


@pytest.mark.parametrize("title,toks,kind", [
    ("OKX × XDP Perpetual Trade-to-Earn: Trade to Share 15,000,000 XDP", ["XDP"], "perp_t2e"),
    ("OKX Flash Earn Lite (CT) is Now Live: Subscribe BTC, ETH, OKB or CT to Share 1,000,000 CT", ["CT"], "flash_lite"),
    ("OKX × Gensyn (AI) Spot Trade-to-Earn: Trade and Share 500,000 USDT", ["AI"], "spot_t2e"),
    ("OKX Flash Earn is now live! Get started with IP, SOPH & HUMA and share 820,000 worth of rewards.", ["IP", "SOPH", "HUMA"], "flash"),
    ("OKX Flash Earn (USDT) is Now Live! Get started with BTC and ETH and share 1,000,000 USDT", [], "flash"),
    ("OKX × Unified Tokenized Stock Spot Trading Competition: Trade to Share 700 XSPY", [], "spot_t2e"),
    ("OKX × CP Trade to Earn: Trade CP to Share a 10,000,000 CP Reward Pool", ["CP"], "t2e"),
    ("OKX Airdrop Earn is now live! Get started with Lombard (BARD) and share 640,000 BARD", ["BARD"], "airdrop_earn"),
    ("OKX Flash Earn is now live! Get started with DoubleZero(2Z) and share 8,000,000 2Z", ["2Z"], "flash"),
])
def test_tokens_and_kind(title, toks, kind):
    assert A.parse_tokens(title) == toks
    assert A.parse_kind(title) == kind


def test_page_text_and_campaign_parse():
    html = ("<html><nav>menu</nav><article><h1>OKX × CT Trade-to-Earn</h1><p>Campaign Period: October 15, 2026, 10:00 to "
            "October 22, 2026, 10:00 (UTC)</p><script>x='October 1, 2020'</script><p>Rewards will be distributed by "
            "October 24, 2026, 10:00 (UTC).</p></article></html>")
    text = A.page_text(html)
    assert "menu" not in text and "2020" not in text
    c = A.parse_campaign("OKX × CT Trade-to-Earn: Trade to Share 500,000 CT", ms("2026-10-15T08:52"), text)
    assert c["status"] == "ok" and c["tokens"] == ["CT"] and c["pool"] == "500,000 CT"
    assert (c["start_ts"], c["end_ts"], c["dist_ts"]) == (ms("2026-10-15T10:00"), ms("2026-10-22T10:00"), ms("2026-10-24T10:00"))
    assert A.parse_campaign("OKX × CT Trade-to-Earn: Trade to Share 500,000 CT", 0, "nothing")["status"] == "no_period"
    assert A.parse_campaign("The Beautiful Game Campaign: Share BTC Rewards", 0, None)["status"] == "skip"


class FakeClient:
    """Two types, two pages each per language; page HTML served for campaign urls."""

    def __init__(self):
        self.calls = []
        self.html = []

    def get(self, path, params, private=False, headers=None):
        self.calls.append((path, dict(params), (headers or {}).get("Accept-Language")))
        if path == A.TYPES_PATH:
            return [{"annType": "latest-events"}, {"annType": "announcements-new-listings"}]
        t, p, lang = params["annType"], int(params["page"]), headers["Accept-Language"]
        rows = [{"annType": t, "title": (f"OKX × AAA Trade-to-Earn: Trade to Share 1,000 AAA" if t == "latest-events" and p == 2 and lang == "en-US"
                                         else f"{t} {lang} p{p}"),
                 "url": f"https://x/{t}/{lang}/{p}", "pTime": str(1_790_000_000_000 - p * 1000), "businessPTime": ""}]
        return [{"details": rows, "totalPage": "2"}]


@pytest.fixture()
def cfg(tmp_path):
    c = load_config()
    c.paths = Paths(root=tmp_path, db=tmp_path / "data" / "okx.duckdb", logs=tmp_path / "logs", results=tmp_path / "results",
                    lock=tmp_path / "data" / "sync.lock")
    c.raw["announce"] = {"db": "data/announce.duckdb", "poll_min": 30}
    return c


def test_syncer_backfill_resumes_and_parses(cfg, monkeypatch):
    page = ("<article><p>Campaign Period: September 1, 2026, 10:00 to September 8, 2026, 10:00 (UTC)</p></article>")
    monkeypatch.setattr(A, "fetch_html", lambda client, url: page)
    fc = FakeClient()
    con = A.connect(cfg)
    s = A.Syncer(cfg, fc, con, None)
    s.poll_first_pages(1)
    assert con.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 4
    assert s.backfill_pages(2, max_pages=1) is False
    assert s.backfill_pages(3) is True
    assert con.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 8
    n_calls = len(fc.calls)
    assert s.backfill_pages(4) is True and len(fc.calls) == n_calls
    s.parse_pending(5)
    row = con.execute("SELECT status, tokens, start_ts, end_ts FROM campaigns").fetchone()
    assert row == ("ok", "AAA", ms("2026-09-01T10:00"), ms("2026-09-08T10:00"))
    con.close()
    df = A.campaign_tokens(A.load_campaigns(cfg))
    assert df[["token", "inst_id", "grp"]].values.tolist() == [["AAA", "AAA-USDT-SWAP", "trade"]]


def test_sync_respects_poll_interval_and_override(cfg, monkeypatch):
    monkeypatch.setattr(A, "fetch_html", lambda client, url: "<article>no period</article>")
    fc = FakeClient()
    r1 = A.sync_announce(cfg, fc)
    assert r1["pages"] > 0
    assert A.sync_announce(cfg, fc) == {"skipped": "recent"}
    st = A.status(cfg)
    assert st["campaigns"] == {"no_period": 1} and st["backfill_done"]
    url = st["problems"][0]["url"]
    (cfg.paths.root / "data" / "campaign_overrides.json").write_text(
        json.dumps({url: {"start": "2026-09-01T10:00", "end": "2026-09-05T10:00"}}), encoding="utf-8")
    A.sync_announce(cfg, fc, force=True)
    assert A.status(cfg)["campaigns"] == {"ok": 1}
