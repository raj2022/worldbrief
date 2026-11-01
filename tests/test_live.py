"""Live mode: rolling store, latest/breaking lists, conditional GETs, server."""
import gzip
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from worldbrief import httpcache, pipeline
from worldbrief.catalog import Catalog
from worldbrief.models import Article
from worldbrief.paywall import PaywallFilter
from worldbrief.server import accepts_gzip, make_handler
from worldbrief.store import ArticleStore

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CAT = Catalog.from_file(ROOT / "sections.yaml")
PW = PaywallFilter.from_file(ROOT / "paywall.yaml")


@contextmanager
def running_server(out, digest):
    refresher = mock.Mock(out_dir=out, last=digest)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(out, refresher, 0, 60))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def status(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def art(title, url, source="S", hours_ago=1.0, category="world", published=True, topic=""):
    return Article(title=title, url=url, source=source, category=category, topic=topic,
                   published=NOW - timedelta(hours=hours_ago) if published else None)


def test_store_keeps_items_between_refreshes_and_ages_them_out(tmp_path):
    s = ArticleStore(tmp_path / "store.json")
    assert s.merge([art("A", "https://x/a"), art("B", "https://x/b", published=False)], NOW) == 2
    s.save()
    s2 = ArticleStore(tmp_path / "store.json")                  # survives a restart
    assert len(s2) == 2
    later = NOW + timedelta(minutes=10)
    assert s2.merge([art("A", "https://x/a")], later) == 0      # already known
    undated = next(a for a in s2.all() if a.title == "B")
    assert undated.first_seen == NOW and undated.when == NOW    # first_seen stands in for a missing date
    s2.prune({"world": 36}, NOW + timedelta(hours=40))
    assert len(s2) == 0


def test_store_ignores_corrupt_file(tmp_path):
    p = tmp_path / "store.json"
    p.write_text("{not json")
    assert len(ArticleStore(p)) == 0


def test_latest_is_newest_first_and_top_is_ranked():
    arts = [
        art("Brazil runoff vote tight presidential election", "https://a/1", "BBC", 5),
        art("Brazil presidential runoff election vote tight", "https://b/1", "DW", 5),
        art("Brazil runoff tight presidential vote election count", "https://c/1", "NPR", 5),
        art("Small earthquake rattles Chile coast", "https://d/1", "CBC", 0.2),
    ]
    d = pipeline.build_digest(arts, [], PW, CAT, now=NOW)
    world = d["sections"]["world"]
    assert world["latest"][0]["headline"].startswith("Small earthquake")   # newest
    assert world["top"][0]["source_count"] == 3                            # most covered


def test_breaking_needs_several_outlets_recently():
    fresh = [art("Major quake hits Istanbul region buildings collapse", f"https://{s}/q", s, 1)
             for s in ("bbc", "dw", "aljazeera")]
    stale = [art("Parliament passes budget reform bill vote", f"https://{s}/p", s, 10)
             for s in ("bbc", "dw", "aljazeera")]
    single = [art("Local mayor resigns over scandal", "https://bbc/m", "bbc", 0.5)]
    d = pipeline.build_digest(fresh + stale + single, [], PW, CAT, now=NOW)
    heads = [b["headline"] for b in d["breaking"]]
    assert len(heads) == 1 and "Istanbul" in heads[0]


def test_opinion_window_is_longer_than_news():
    old_news = art("Old news item about something", "https://n/1", hours_ago=72)
    old_column = art("Why the energy transition needs patience", "https://o/1", hours_ago=72,
                     category="opinion", topic="columns")
    d = pipeline.build_digest([old_news, old_column], [], PW, CAT, now=NOW)
    assert d["sections"]["world"]["top"] == []
    assert d["sections"]["opinion"]["top"][0]["headline"].startswith("Why the energy")


def test_http_cache_uses_304():
    cache = httpcache.HTTPCache()
    first = mock.Mock(status_code=200, content=b"<rss/>", headers={"ETag": '"v1"'})
    first.raise_for_status = lambda: None
    second = mock.Mock(status_code=304, content=b"", headers={})
    with mock.patch("requests.get", side_effect=[first, second]) as g:
        assert cache.get("https://f", None, {}, 5).content == b"<rss/>"
        r = cache.get("https://f", None, {}, 5)
        assert r.content == b"<rss/>" and r.from_cache
        assert g.call_args.kwargs["headers"]["If-None-Match"] == '"v1"'


def test_refresher_end_to_end(tmp_path):
    srcs = tmp_path / "s.yaml"
    srcs.write_text("sources:\n  - {type: rss, name: BBC World, url: 'https://f', category: world}\n")
    feed = (Path(__file__).parent / "fixtures" / "bbc.xml").read_bytes()
    resp = httpcache.CachedResponse(feed, 200)
    with mock.patch.object(httpcache.SHARED, "get", return_value=resp):
        r = pipeline.Refresher(srcs, tmp_path / "dist")
        d1 = r.refresh(now=NOW)
        d2 = r.refresh(now=NOW + timedelta(minutes=10))
    assert d1["stats"]["new_this_refresh"] == 2 and d2["stats"]["new_this_refresh"] == 0
    assert (tmp_path / "dist" / "index.json").exists()
    assert (tmp_path / "dist" / ".store.json").exists()


def test_server_serves_files_with_etag(tmp_path):
    out = tmp_path / "dist"
    digest = pipeline.build_digest([art("Hello world story", "https://h/1")], [], PW, CAT, now=NOW)
    pipeline.write_split(digest, out)
    refresher = mock.Mock(out_dir=out, last=digest)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(out, refresher, 0, 60))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with urllib.request.urlopen(base + "/index.json") as r:
            etag = r.headers["ETag"]
            assert json.loads(r.read())["schema_version"] == pipeline.SCHEMA_VERSION
        req = urllib.request.Request(base + "/index.json", headers={"If-None-Match": etag})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected 304")
        except urllib.error.HTTPError as e:
            assert e.code == 304
        with urllib.request.urlopen(base + "/sections/world.json") as r:
            assert json.loads(r.read())["id"] == "world"
        with urllib.request.urlopen(base + "/health") as r:
            assert json.loads(r.read())["ok"] is True
        for bad in ("/../sources.yaml", "/sections/../../x.json", "/.store.json"):
            try:
                urllib.request.urlopen(base + bad)
                raise AssertionError(bad)
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        httpd.shutdown()


def many_outlets(n=7):
    """One story covered by n outlets; outlet 0 is the newest report."""
    return [art("Brazil runoff vote tight presidential election", f"https://o{i}/1", f"Outlet {i}", 1 + i)
            for i in range(n)]


def test_section_files_cap_articles_but_keep_source_count(tmp_path):
    digest = pipeline.build_digest(many_outlets(), [], PW, CAT, now=NOW)
    pipeline.write_split(digest, tmp_path)
    world = json.loads((tmp_path / "sections" / "world.json").read_text())
    for story in (world["top"][0], world["latest"][0], world["by_region"]["global"][0]):
        assert len(story["articles"]) == pipeline.ARTICLES_PER_STORY
        assert story["source_count"] == 7
        assert [a["url"] for a in story["articles"]] == [f"https://o{i}/1" for i in range(5)]   # newest first
    full = json.loads((tmp_path / "sections" / "world.full.json").read_text())
    assert len(full["top"][0]["articles"]) == 7
    assert len(digest["sections"]["world"]["top"][0]["articles"]) == 7     # digest stays uncapped


def test_section_cap_applies_to_topics_too(tmp_path):
    arts = [art("Quantum computer breaks factoring record again", f"https://q{i}/1", f"Lab {i}", 1 + i,
                category="science", topic="physics") for i in range(7)]
    pipeline.write_split(pipeline.build_digest(arts, [], PW, CAT, now=NOW), tmp_path)
    science = json.loads((tmp_path / "sections" / "science.json").read_text())
    assert len(science["by_topic"]["physics"][0]["articles"]) == pipeline.ARTICLES_PER_STORY


def test_index_drops_source_health_but_health_keeps_it(tmp_path):
    results = [pipeline.SourceResult("Dead", "rss", "world", False, 0, 0.1, "boom")]
    digest = pipeline.build_digest(many_outlets(), results, PW, CAT, now=NOW)
    pipeline.write_split(digest, tmp_path)
    assert "source_health" not in json.loads((tmp_path / "index.json").read_text())
    with running_server(tmp_path, digest) as base:
        with urllib.request.urlopen(base + "/health") as r:
            assert json.loads(r.read())["failed_sources"] == ["Dead"]


def test_accepts_gzip():
    assert accepts_gzip("gzip") and accepts_gzip("gzip, deflate, br") and accepts_gzip("br, GZIP;q=0.5")
    assert not accepts_gzip(None) and not accepts_gzip("deflate") and not accepts_gzip("gzip;q=0")
    assert not accepts_gzip("gzip; q=0.0")


def test_server_gzips_only_when_asked_and_etag_ignores_encoding(tmp_path):
    digest = pipeline.build_digest(many_outlets(), [], PW, CAT, now=NOW)
    pipeline.write_split(digest, tmp_path)
    with running_server(tmp_path, digest) as base:
        for path in ("/index.json", "/sections/world.json"):
            with urllib.request.urlopen(urllib.request.Request(
                    base + path, headers={"Accept-Encoding": "identity"})) as r:
                plain, plain_etag = r.read(), r.headers["ETag"]
                assert r.headers["Content-Encoding"] is None
                assert r.headers["Vary"] == "Accept-Encoding"
            zipped_req = urllib.request.Request(base + path, headers={"Accept-Encoding": "gzip"})
            with urllib.request.urlopen(zipped_req) as r:
                zipped = r.read()
                assert r.headers["Content-Encoding"] == "gzip"
                assert r.headers["ETag"] == plain_etag
                assert int(r.headers["Content-Length"]) == len(zipped) < len(plain)
            assert gzip.decompress(zipped) == plain
            # The ETag from either encoding revalidates, with or without gzip.
            for enc in ("gzip", "identity"):
                assert status(base + path, {"If-None-Match": plain_etag, "Accept-Encoding": enc}) == 304
        assert status(base + "/index.json", {"If-None-Match": '"stale"', "Accept-Encoding": "gzip"}) == 200
        with urllib.request.urlopen(urllib.request.Request(
                base + "/health", headers={"Accept-Encoding": "gzip"})) as r:
            assert json.loads(gzip.decompress(r.read()))["ok"] is True


def test_server_head_reports_gzip_length_without_a_body(tmp_path):
    digest = pipeline.build_digest(many_outlets(), [], PW, CAT, now=NOW)
    pipeline.write_split(digest, tmp_path)
    with running_server(tmp_path, digest) as base:
        req = urllib.request.Request(base + "/index.json", method="HEAD",
                                     headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Encoding"] == "gzip" and r.read() == b""
            assert int(r.headers["Content-Length"]) > 0


def test_full_param_returns_uncapped_section(tmp_path):
    digest = pipeline.build_digest(many_outlets(), [], PW, CAT, now=NOW)
    pipeline.write_split(digest, tmp_path)
    with running_server(tmp_path, digest) as base:
        def fetch(path):
            with urllib.request.urlopen(base + path) as r:
                return json.loads(r.read()), r.headers["ETag"]

        capped, capped_etag = fetch("/sections/world.json")
        full, full_etag = fetch("/sections/world.json?full=1")
        assert len(capped["top"][0]["articles"]) == 5 and len(full["top"][0]["articles"]) == 7
        assert capped_etag != full_etag
        assert status(base + "/sections/world.json?full=1", {"If-None-Match": full_etag}) == 304
        assert status(base + "/sections/world.json?full=1", {"If-None-Match": capped_etag}) == 200
        assert len(fetch("/sections/world.json?full=0")[0]["top"][0]["articles"]) == 5
        assert fetch("/index.json?full=1")[1] == fetch("/index.json")[1]       # sections only
        assert status(base + "/sections/world.full.json") == 404               # only via ?full=1
