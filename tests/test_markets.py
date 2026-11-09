import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worldbrief import markets, pipeline
from worldbrief.catalog import Catalog
from worldbrief.models import Article
from worldbrief.store import ArticleStore

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def result(symbol, price, prev, closes=(1.0, None, 2.0), **meta):
    return {"symbol": symbol, "response": [{
        "meta": {"regularMarketPrice": price, "chartPreviousClose": prev, "currency": "INR",
                 "regularMarketTime": 1794218487, "priceHint": 2, **meta},
        "indicators": {"quote": [{"close": list(closes)}]},
    }]}


CONFIG = markets.MarketsConfig(
    groups=[markets.Group("india", "India", [markets.Instrument("^NSEI", "Nifty 50"),
                                             markets.Instrument("^BSESN", "Sensex")]),
            markets.Group("bonds", "Bond yields", [markets.Instrument("^TNX", "US 10-year", "%", 3)])],
    movers=[markets.MoverList("in", "India large caps", ["A.NS", "B.NS", "C.NS"])],
)


def test_config_file_loads_and_has_every_kind_of_market():
    cfg = markets.MarketsConfig.from_file(ROOT / "markets.yaml")
    ids = [g.id for g in cfg.groups]
    assert {"india", "americas", "europe", "asia", "mea", "currencies", "commodities", "bonds", "crypto"} <= set(ids)
    assert len(ids) == len(set(ids))
    symbols = cfg.group_symbols
    assert len(symbols) == len(set(symbols)), "a symbol is listed twice"
    assert {m.id for m in cfg.movers} == {"india", "us"}


def test_parse_quote():
    q = markets.parse(result("^NSEI", 25044.65, 24986.4, closes=[24990.0, None, 25044.65]), "Nifty 50")
    assert q["name"] == "Nifty 50"
    assert q["change"] == 58.25
    assert q["change_pct"] == 0.233
    assert q["decimals"] == 2
    assert q["spark"] == [24990.0, 25044.65]          # gaps dropped
    assert q["market_time"] == "2026-11-09T10:01:27+00:00"
    assert markets.parse({"symbol": "X", "response": [{"meta": {}}]}) is None


def test_spark_is_downsampled_keeping_both_ends():
    q = markets.parse(result("BTC-USD", 2.0, 1.0, closes=list(range(200))))
    assert len(q["spark"]) == markets.SPARK_POINTS
    assert q["spark"][0] == 0 and q["spark"][-1] == 199


def test_company_names_lose_legal_suffixes():
    assert markets.company_name({"longName": "Apollo Hospitals Enterprise Limited"}, "APOLLOHOSP.NS") \
        == "Apollo Hospitals Enterprise"
    assert markets.company_name({"shortName": "Apple Inc."}, "AAPL") == "Apple"
    assert markets.company_name({}, "TCS.NS") == "TCS"


def test_build_groups_and_movers():
    raw = {"^NSEI": result("^NSEI", 101, 100), "^BSESN": result("^BSESN", 99, 100),
           "^TNX": result("^TNX", 4.88, 4.86)}
    movers = {"A.NS": result("A.NS", 110, 100), "B.NS": result("B.NS", 95, 100),
              "C.NS": result("C.NS", 101, 100)}
    payload, fresh = markets.build(CONFIG, raw, movers, None, NOW)
    assert fresh == 3
    assert [g["id"] for g in payload["groups"]] == ["india", "bonds"]
    assert payload["groups"][1]["quotes"][0]["unit"] == "%"
    assert payload["groups"][1]["quotes"][0]["decimals"] == 3
    m = payload["movers"][0]
    assert [q["symbol"] for q in m["gainers"]] == ["A.NS", "C.NS"]
    assert [q["symbol"] for q in m["losers"]] == ["B.NS"]
    assert m["gainers"][0]["spark"] == []


def test_missing_prices_keep_the_last_good_ones():
    first, _ = markets.build(CONFIG, {"^NSEI": result("^NSEI", 101, 100), "^BSESN": result("^BSESN", 99, 100)},
                             {s: result(s, 101, 100) for s in ["A.NS", "B.NS", "C.NS"]}, None, NOW)
    later = NOW + timedelta(minutes=15)
    # Only the Nifty answers now, and too few stocks for movers.
    second, fresh = markets.build(CONFIG, {"^NSEI": result("^NSEI", 102, 100)},
                                  {"A.NS": result("A.NS", 120, 100)}, first, later)
    assert fresh == 1
    quotes = {q["symbol"]: q for q in second["groups"][0]["quotes"]}
    assert quotes["^NSEI"]["price"] == 102
    assert quotes["^BSESN"]["price"] == 99           # kept from the last run
    assert second["movers"] == first["movers"]       # kept rather than built from one stock
    assert second["generated_at"] == later.isoformat()
    # Nothing answers: the payload is the last one, with its own time.
    third, fresh = markets.build(CONFIG, {}, {}, second, later + timedelta(minutes=15))
    assert fresh == 0 and third["generated_at"] == later.isoformat()


def test_publish_writes_and_survives_an_outage(tmp_path):
    out, state = tmp_path / "dist", tmp_path / "cache"
    up = lambda symbols, interval: {s: result(s, 101, 100) for s in symbols}
    down = lambda symbols, interval: {}
    assert markets.publish(CONFIG, out, state, NOW, fetcher=up) == 3
    first = json.loads((out / "markets.json").read_text())
    (out / "markets.json").unlink()                  # out_dir is rebuilt on each run
    assert markets.publish(CONFIG, out, state, NOW + timedelta(minutes=15), fetcher=down) == 0
    assert json.loads((out / "markets.json").read_text()) == first


def test_publish_writes_nothing_without_any_prices(tmp_path):
    assert markets.publish(CONFIG, tmp_path / "dist", tmp_path, NOW, fetcher=lambda s, i: {}) == 0
    assert not (tmp_path / "dist" / "markets.json").exists()


def test_fetch_skips_a_failing_batch():
    def get(symbols, interval):
        if "S0" in symbols:
            raise OSError("blocked")
        return [result(s, 1, 1) for s in symbols]
    got = markets.fetch([f"S{i}" for i in range(45)], get=get)
    assert len(got) == 25 and "S0" not in got and "S44" in got


# ── business subtopics and their windows ───────────────────────

def test_business_sources_cover_every_subtopic():
    catalog = Catalog.from_file(ROOT / "sections.yaml")
    plugins = pipeline.load_sources(ROOT / "sources.yaml", catalog)
    business = [p for p in plugins if p.category == "business"]
    assert {p.topic for p in business} == catalog.by_id["business"].subtopic_ids
    assert sum(p.topic == "commentary" for p in business) >= 8


def test_commentary_stays_longer_than_business_news():
    catalog = Catalog.from_file(ROOT / "sections.yaml")
    assert catalog.max_age("business", "commentary") == 168
    assert catalog.max_age("business", "markets") == catalog.by_id["business"].max_age_hours == 48
    assert catalog.topic_ages() == {"business/commentary": 168}


def test_store_prunes_by_topic_window(tmp_path):
    def art(i, topic, hours_old):
        return Article(title=f"t{i}", url=f"https://x.com/{i}", source="S", category="business",
                       topic=topic, published=NOW - timedelta(hours=hours_old))
    store = ArticleStore(tmp_path / "s.json")
    store.merge([art(1, "commentary", 100), art(2, "markets", 100), art(3, "markets", 10)], NOW)
    store.prune({"business": 48}, NOW, max_age_by_topic={"business/commentary": 168})
    assert sorted(a.topic for a in store.all()) == ["commentary", "markets"]
    assert all(a.title != "t2" for a in store.all())


def test_market_columns_stay_out_of_the_brief(tmp_path):
    def story(sid, topics, score):
        return {"id": sid, "topics": topics, "score": score, "brief": {"summary": "s"}, "articles": []}
    digest = {"sections": {
        "business": {"top": [story("col", ["commentary"], 9), story("mix", ["commentary", "markets"], 8),
                             story("mkt", ["markets"], 7)]},
        "world": {"top": [story("w", [], 6)]},
    }}
    pipeline.write_morning(digest, tmp_path, tmp_path, (0,), NOW)
    ids = [s["id"] for s in json.loads((tmp_path / "morning.json").read_text())["stories"]]
    assert ids == ["mix", "mkt", "w"]


def test_keywords_reach_the_quote_and_survive_a_fallback():
    cfg = markets.MarketsConfig([markets.Group("c", "Commodities", [
        markets.Instrument("GC=F", "Gold", "$/oz", keywords=["Gold", "bullion"])])], [])
    first, _ = markets.build(cfg, {"GC=F": result("GC=F", 4100, 4000)}, {}, None, NOW)
    assert first["groups"][0]["quotes"][0]["keywords"] == ["gold", "bullion"]
    again, _ = markets.build(cfg, {}, {}, first, NOW)
    assert again["groups"][0]["quotes"][0]["keywords"] == ["gold", "bullion"]
