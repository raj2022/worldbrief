"""Live mode: rolling store, latest/breaking lists, conditional GETs, server."""
import gzip
import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from worldbrief import httpcache, pipeline
from worldbrief.__main__ import main
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


def test_refresher_keeps_last_digest_when_sources_fail(tmp_path):
    srcs = tmp_path / "s.yaml"
    srcs.write_text("sources:\n  - {type: rss, name: BBC World, url: 'https://f', category: world}\n")
    feed = (Path(__file__).parent / "fixtures" / "bbc.xml").read_bytes()
    out = tmp_path / "dist"
    with mock.patch.object(httpcache.SHARED, "get", return_value=httpcache.CachedResponse(feed, 200)):
        r = pipeline.Refresher(srcs, out)
        r.refresh(now=NOW)
    good = (out / "index.json").read_bytes()
    with mock.patch.object(httpcache.SHARED, "get", side_effect=OSError("network down")):
        try:
            r.refresh(now=NOW + timedelta(minutes=10))
            raise AssertionError("expected ThinDigestError")
        except pipeline.ThinDigestError:
            pass
    assert (out / "index.json").read_bytes() == good
    assert r.last["generated_at"] == json.loads(good)["generated_at"]


def test_check_publishable_thresholds():
    ok = {"stats": {"sources_ok": 5, "sources_failed": 5, "stories": 10}}
    pipeline.check_publishable(ok)
    for stats in ({"sources_ok": 4, "sources_failed": 6, "stories": 10},
                  {"sources_ok": 10, "sources_failed": 0, "stories": 0}):
        try:
            pipeline.check_publishable({"stats": stats})
            raise AssertionError(f"expected ThinDigestError for {stats}")
        except pipeline.ThinDigestError:
            pass


def test_cli_run_exits_nonzero_when_nothing_answers(tmp_path, capsys):
    srcs = tmp_path / "s.yaml"
    srcs.write_text("sources:\n  - {type: rss, name: BBC World, url: 'https://f', category: world}\n")
    with mock.patch.object(httpcache.SHARED, "get", side_effect=OSError("network down")):
        code = main(["--sources", str(srcs), "run", "--out-dir", str(tmp_path / "dist"),
                     "--store", str(tmp_path / "cache" / "store.json")])
    assert code == 1
    assert "not publishing" in capsys.readouterr().err
    assert not (tmp_path / "dist" / "index.json").exists()


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


def test_morning_endpoint(tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    refresher = mock.Mock(out_dir=out, last=None, briefer=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(out, refresher, 0, 60))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/morning.json"
    try:
        assert status(url) == 503                  # first edition not out yet
        (out / "morning.json").write_text('{"edition": "2026-11-01", "stories": []}')
        with urllib.request.urlopen(url) as r:
            assert json.loads(r.read())["edition"] == "2026-11-01" and r.headers["ETag"]
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
        assert status(base + "/sections/world.full.json") == 200   # as on static hosting


def test_short_titles_need_three_shared_words_to_merge():
    arts = [art("Make healthcare affordable for all people everywhere", "https://a/1", "A", 3),
            art("Healthcare for all", "https://b/1", "B", 2),
            art("[ LETTERS ]", "https://c/1", "C", 2),
            art("Trump letter lawsuit dismissed by judge in Miami", "https://d/1", "D", 3)]
    d = pipeline.build_digest(arts, [], PW, CAT, now=NOW)
    assert all(s["source_count"] == 1 for s in d["sections"]["world"]["top"])


def test_near_identical_headlines_still_merge():
    arts = [art("Judge dismisses Trump lawsuit against newspaper", "https://a/1", "A", 3),
            art("Judge dismisses Trump lawsuit over newspaper story", "https://b/1", "B", 2)]
    d = pipeline.build_digest(arts, [], PW, CAT, now=NOW)
    assert d["sections"]["world"]["top"][0]["source_count"] == 2


def test_junk_is_dropped_but_real_stories_stay():
    junk = [("Watch: Heidi Klum as giant severed hand", "https://bbc/news/videos/x"),
            ("LIVE: Rugby League World Cup: Tonga v Samoa", "https://rnz/1"),
            ("Democrats hold rally - live updates", "https://hill/2"),
            ("A normal title", "https://guardian/sport/live/2026/oct/31/x"),
            ("Photos: Youth-led rallies sweep Germany", "https://aj/3"),
            ("Protests continue – video", "https://g/4"),
            ("Latest news bulletin | November 1st, 2026 – Morning", "https://e/5"),
            ("[ LETTERS ]", "https://t/6"),
            ("Open Thread", "https://r/7"),
            ("Tottenham vs Crystal Palace LIVE streaming, TV channel, kick-off", "https://k/8"),
            ("How to watch the Asian Champions Trophy final", "https://k/9")]
    real = [("Live music returns to Lagos after long ban", "https://a/live-music"),
            ("Liverpool beat Arsenal to go top of the league", "https://a/football/liverpool"),
            ("The podcast boom has a problem", "https://a/opinion/podcast-boom")]
    arts = [art(t, u, str(i)) for i, (t, u) in enumerate(junk + real)]
    d = pipeline.build_digest(arts, [], PW, CAT, now=NOW)
    assert d["stats"]["junk_dropped"] == len(junk)
    assert len(d["sections"]["world"]["top"]) == len(real)


def test_specific_topic_beats_general_for_the_same_link():
    a = art("India beat Australia in thriller", "https://x/1", "BBC Sport", category="sports", topic="general")
    b = art("India beat Australia in thriller", "https://x/1", "BBC Cricket", category="sports", topic="cricket")
    for order in ([a, b], [b, a]):
        assert pipeline.dedupe(order)[0].topic == "cricket"


def test_future_dates_fall_back_to_first_seen():
    a = art("Story", "https://x/1", hours_ago=-2)
    a.first_seen = NOW
    assert a.when == NOW
    a.published = NOW + timedelta(minutes=5)     # small clock differences are fine
    assert a.when == a.published


def test_rss_parses_date_styles_feedparser_misses():
    from worldbrief.plugins.rss import _parse_date
    for text, day in (("Friday, October 23, 2026 - 18:13", 23), ("Fri, 10/23/2026 - 13:00", 23),
                      ("Oct 30, 2026", 30)):
        assert _parse_date({"published": text}).day == day
    assert _parse_date({"published": "someday"}) is None


def test_server_drops_stalled_clients(tmp_path):
    handler = make_handler(tmp_path, mock.Mock(out_dir=tmp_path, last=None), 0, 60)
    assert handler.timeout > 0
    handler.timeout = 0.2
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", httpd.server_port), timeout=3) as s:
            s.sendall(b"GET /health HTTP/1.1\r\n")  # never finishes the request
            assert s.recv(1024) == b""
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_keeps_store_out_of_published_files(tmp_path):
    out, store = tmp_path / "dist", tmp_path / ".cache" / "store.json"
    bad = mock.Mock(ok=False)
    bad.name = "Dead Feed"
    good = mock.Mock(ok=True)
    good.name = "Live Feed"
    story = art("Undated story still fresh", "https://h/fresh", published=False)
    with mock.patch.object(pipeline, "fetch_all", return_value=([story], [good, bad])):
        assert main(["run", "--out-dir", str(out), "--store", str(store)]) == 0
    assert store.exists()
    assert not list(out.glob(".*"))
    assert "Dead Feed" in (out / "health.json").read_text()
    health = json.loads((out / "health.json").read_text())
    assert health["ok"] and health["generated_at"] and health["failed_sources"] == ["Dead Feed"]
    assert (out / "sections" / "world.full.json").exists()


def test_server_serves_full_files_by_path(tmp_path):
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "world.full.json").write_text("{}")
    with running_server(tmp_path, {}) as base:
        with urllib.request.urlopen(base + "/sections/world.full.json") as r:
            assert r.status == 200


def test_refresher_skips_sources_fetched_recently(tmp_path):
    """Runs every 5 minutes refresh prices, but each feed is polled at most every 14."""
    srcs = tmp_path / "s.yaml"
    srcs.write_text("sources:\n  - {type: rss, name: BBC World, url: 'https://f', category: world}\n")
    feed = (Path(__file__).parent / "fixtures" / "bbc.xml").read_bytes()
    out = tmp_path / "dist"
    with mock.patch.object(httpcache.SHARED, "get", return_value=httpcache.CachedResponse(feed, 200)) as g:
        r = pipeline.Refresher(srcs, out, min_source_minutes=14)
        r.refresh(now=NOW)
        d = r.refresh(now=NOW + timedelta(minutes=5))       # too soon: not fetched
        assert g.call_count == 1
        assert d["source_health"][0]["skipped"] and d["stats"]["sources_failed"] == 0
        assert d["stats"]["stories"] == 2                     # stories come from the store
        # A new process (the next Actions run) reads the times from disk.
        r2 = pipeline.Refresher(srcs, out, min_source_minutes=14)
        r2.refresh(now=NOW + timedelta(minutes=10))
        assert g.call_count == 1
        r2.refresh(now=NOW + timedelta(minutes=15))
        assert g.call_count == 2


def test_failed_source_is_retried_on_the_next_run(tmp_path):
    srcs = tmp_path / "s.yaml"
    srcs.write_text("sources:\n"
                    "  - {type: rss, name: A, url: 'https://a', category: world}\n"
                    "  - {type: rss, name: B, url: 'https://b', category: world}\n")
    feed = (Path(__file__).parent / "fixtures" / "bbc.xml").read_bytes()
    calls = []

    def get(url, *a, **k):
        calls.append(url)
        if url == "https://b" and len(calls) <= 2:
            raise OSError("down")
        return httpcache.CachedResponse(feed, 200)

    with mock.patch.object(httpcache.SHARED, "get", side_effect=get):
        r = pipeline.Refresher(srcs, tmp_path / "dist", min_source_minutes=14)
        r.refresh(now=NOW)
        calls.clear()
        r.refresh(now=NOW + timedelta(minutes=5))
    assert calls == ["https://b"]
