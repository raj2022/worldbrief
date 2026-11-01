from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import plugin as plugin_mod
from .catalog import Catalog
from .cluster import cluster
from .models import Article, canonical_url
from .paywall import PaywallFilter
from .quality import drop_junk
from .store import ArticleStore

log = logging.getLogger(__name__)

DEFAULT_SOURCES = Path(__file__).resolve().parent.parent / "sources.yaml"
SCHEMA_VERSION = 3
HEADLINES_PER_SECTION = 5      # stories per section embedded in index.json
ARTICLES_PER_STORY = 5         # articles per story in section files (?full=1 has them all)
BREAKING_MIN_SOURCES = 3       # independent outlets needed to call it breaking
BREAKING_WINDOW_HOURS = 3
NO_BREAKING_SECTIONS = {"opinion", "science", "culture"}
MORNING_MAX_STORIES = 15       # more than a screen, because the app filters by section
MORNING_PER_SECTION = 8
MORNING_MIN_STORIES = 3        # fewer than this waits for the next refresh
DEFAULT_MORNING_HOURS = (11,)  # UTC hours an edition comes out: 6am New York in winter


@dataclass
class SourceResult:
    name: str
    type: str
    section: str
    ok: bool
    count: int
    seconds: float
    error: str = ""


def load_sources(path: Path = DEFAULT_SOURCES, catalog: Catalog | None = None,
                 only: set[str] | None = None) -> list[plugin_mod.SourcePlugin]:
    """Build enabled plugins from sources.yaml, checked against the catalog.
    `only` limits to a set of section ids (e.g. {"world", "science"})."""
    catalog = catalog or Catalog.from_file()
    data = yaml.safe_load(Path(path).read_text()) or {}
    plugins = [plugin_mod.create(cfg) for cfg in data.get("sources", [])]
    problems = [msg for p in plugins
                for msg in catalog.check_source(p.name, p.category, p.topic, p.region)]
    if problems:
        raise ValueError("sources.yaml doesn't match sections.yaml:\n  " + "\n  ".join(problems))
    if only:
        unknown = only - set(catalog.by_id)
        if unknown:
            raise ValueError(f"unknown section(s): {sorted(unknown)}")
    return [p for p in plugins if p.enabled and (not only or p.category in only)]


def fetch_all(plugins, workers: int = 24) -> tuple[list[Article], list[SourceResult]]:
    """Run every plugin concurrently. One broken feed never stops the run."""
    articles: list[Article] = []
    results: list[SourceResult] = []

    def run(p):
        t0 = time.monotonic()
        try:
            return p, p.fetch(), None, time.monotonic() - t0
        except Exception as e:  # noqa: BLE001 — isolate any plugin failure
            return p, [], f"{type(e).__name__}: {e}", time.monotonic() - t0

    with ThreadPoolExecutor(workers) as ex:
        for fut in as_completed([ex.submit(run, p) for p in plugins]):
            p, items, err, secs = fut.result()
            if err:
                log.warning("source failed: %s — %s", p.name, err)
            articles.extend(items)
            results.append(SourceResult(p.name, p.type_name, p.category, err is None,
                                        len(items), round(secs, 2), err or ""))
    return articles, sorted(results, key=lambda r: (r.ok, r.section, r.name))


def dedupe(articles: list[Article]) -> list[Article]:
    """Same URL from two feeds → keep one. Keyed per section, so an article can
    still appear in both e.g. World and Climate if two section feeds carry it."""
    seen: dict[tuple[str, str], Article] = {}
    for a in articles:
        key = (a.category, canonical_url(a.url))
        old = seen.get(key)
        # A sport's own feed and the main sport feed often carry the same link.
        # Prefer the specific topic, so it doesn't depend on which loaded first.
        if (old is None or (not old.summary and a.summary)
                or (old.topic == "general" and a.topic not in ("", "general"))):
            seen[key] = a
    return list(seen.values())


def build_digest(articles: list[Article], results: list[SourceResult],
                 paywall: PaywallFilter, catalog: Catalog | None = None,
                 max_age_hours: float | None = None, now: datetime | None = None,
                 per_list: int = 40) -> dict:
    """Turn articles into the digest. Each section keeps items for its own
    `max_age_hours` (sections.yaml) unless an override is given."""
    catalog = catalog or Catalog.from_file()
    now = now or datetime.now(timezone.utc)

    def age_limit(section_id: str) -> float:
        if max_age_hours is not None:
            return max_age_hours
        sec = catalog.by_id.get(section_id)
        return sec.max_age_hours if sec else 36

    total = len(articles)
    articles = dedupe(articles)
    articles, paywalled = paywall.apply(articles)
    articles, junk = drop_junk(articles)
    articles = [a for a in articles
                if a.when is None or a.when >= now - timedelta(hours=age_limit(a.category))]

    stories = cluster(articles)
    decay = {s.id: max(24.0, age_limit(s.id) / 3) for s in catalog.sections}
    stories.sort(key=lambda st: st.score(now, decay.get(st.category, 24)), reverse=True)

    sections: dict[str, dict] = {
        s.id: {"id": s.id, "top": [], "latest": [], "by_topic": {}, "by_region": {}}
        for s in catalog.sections
    }
    dicts: dict[int, dict] = {}

    def push(bucket: list, item: dict):
        if len(bucket) < per_list:
            bucket.append(item)

    for st in stories:
        sec = sections.get(st.category)
        if sec is None:
            continue
        d = dicts[id(st)] = st.to_dict(now, decay[st.category])
        push(sec["top"], d)
        for t in st.topics:
            push(sec["by_topic"].setdefault(t, []), d)
        if catalog.by_id[st.category].by_region:
            for r in st.regions:
                push(sec["by_region"].setdefault(r, []), d)

    # "Latest": newest first — the live view when you open the app.
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    by_time = sorted((st for st in stories if st.category in sections),
                     key=lambda st: st.latest or epoch, reverse=True)
    for st in by_time:
        push(sections[st.category]["latest"], dicts[id(st)])

    # Breaking: several independent outlets on the same story in the last few hours.
    breaking = [dicts[id(st)] for st in by_time
                if st.category in sections and st.category not in NO_BREAKING_SECTIONS
                and len(st.sources) >= BREAKING_MIN_SOURCES and st.latest
                and st.latest >= now - timedelta(hours=BREAKING_WINDOW_HOURS)][:10]

    for sec in sections.values():
        sec["story_count"] = sum(1 for st in stories if st.category == sec["id"])
        sec["generated_at"] = now.isoformat()

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "catalog": catalog.to_list(),
        "breaking": breaking,
        "stats": {
            "fetched": total,
            "after_dedupe_and_paywall": len(articles),
            "paywalled_dropped": len(paywalled),
            "junk_dropped": len(junk),
            "stories": len(stories),
            "sources_ok": sum(r.ok for r in results),
            "sources_failed": sum(not r.ok for r in results),
        },
        "sections": sections,
        "source_health": [r.__dict__ for r in results],
    }


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)  # readers never see a half-written file


def _capped_story(st: dict) -> dict:
    return {**st, "articles": st["articles"][:ARTICLES_PER_STORY]}


def _capped_section(sec: dict) -> dict:
    """Copy of a section with each story's articles capped. Stories keep their
    full source_count, and the digest itself is left uncapped."""
    capped = {**sec, "top": [_capped_story(st) for st in sec["top"]],
              "latest": [_capped_story(st) for st in sec["latest"]]}
    for key in ("by_topic", "by_region"):
        capped[key] = {k: [_capped_story(st) for st in v] for k, v in sec[key].items()}
    return capped


def write_split(digest: dict, out_dir: Path) -> list[Path]:
    """Write the digest as small files so the app downloads only what the
    user picked:

        out_dir/index.json                 catalog + breaking + top headlines per section
        out_dir/sections/<id>.json         one section, articles capped per story
        out_dir/sections/<id>.full.json    the same section, uncapped
        out_dir/health.json                last refresh time and failing sources
    """
    out_dir = Path(out_dir)
    (out_dir / "sections").mkdir(parents=True, exist_ok=True)
    written = []
    for sid, sec in digest["sections"].items():
        p = out_dir / "sections" / f"{sid}.json"
        _atomic_write(p, _capped_section(sec))
        full = out_dir / "sections" / f"{sid}.full.json"
        _atomic_write(full, sec)
        written += [p, full]
    index = {k: v for k, v in digest.items() if k not in ("sections", "source_health")}
    index["headlines"] = {sid: sec["top"][:HEADLINES_PER_SECTION]
                          for sid, sec in digest["sections"].items()}
    index["section_files"] = {sid: f"sections/{sid}.json" for sid in digest["sections"]}
    p = out_dir / "index.json"
    _atomic_write(p, index)
    written.append(p)
    p = out_dir / "health.json"
    _atomic_write(p, health(digest))
    written.append(p)
    return written


def health(digest: dict | None) -> dict:
    d = digest or {}
    return {
        "ok": digest is not None,
        "generated_at": d.get("generated_at"),
        "stats": d.get("stats"),
        "failed_sources": [r["name"] for r in d.get("source_health", []) if not r["ok"]],
    }


def morning_due(last: datetime | None, now: datetime, hours: tuple[int, ...]) -> bool:
    """True if an edition time has passed since the last edition."""
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours]
    slots = today + [s - timedelta(days=1) for s in today]
    latest_slot = max(s for s in slots if s <= now)
    return last is None or last < latest_slot


def write_morning(digest: dict, out_dir: Path, state_dir: Path, hours: tuple[int, ...],
                  now: datetime) -> bool:
    """Publish the Morning brief: the highest-ranked stories that have a brief.
    It's a snapshot, so it reads like one edition rather than shifting with every
    refresh. The edition is kept in `state_dir`, because out_dir may be rebuilt
    from nothing on each run, and copied to out_dir every time. Returns True if
    this run made a new edition."""
    saved = Path(state_dir) / ".morning.json"
    try:
        edition = json.loads(saved.read_text())
        last = datetime.fromisoformat(edition["generated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        edition, last = None, None

    created = False
    if morning_due(last, now, hours):
        stories = []
        for sec in digest["sections"].values():
            stories += [st for st in sec["top"] if st.get("brief")][:MORNING_PER_SECTION]
        # Too few means the API was down at edition time, so try again next refresh.
        if len(stories) >= MORNING_MIN_STORIES:
            stories.sort(key=lambda st: st["score"], reverse=True)
            edition = {
                "edition": now.date().isoformat(),
                "generated_at": now.isoformat(),
                "stories": [_capped_story(st) for st in stories[:MORNING_MAX_STORIES]],
            }
            saved.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(saved, edition)
            created = True
    if edition:
        _atomic_write(Path(out_dir) / "morning.json", edition)
    return created


class Refresher:
    """One refresh cycle: fetch → merge into the rolling store → digest → files.
    The live server calls this every few minutes."""

    def __init__(self, sources_path: Path = DEFAULT_SOURCES, out_dir: Path = Path("dist"),
                 store_path: Path | None = None, paywall_path: Path | None = None,
                 only: set[str] | None = None, briefer=None,
                 morning_hours: tuple[int, ...] = DEFAULT_MORNING_HOURS):
        self.catalog = Catalog.from_file()
        self.plugins = load_sources(sources_path, self.catalog, only)
        self.paywall = PaywallFilter.from_file(paywall_path) if paywall_path else PaywallFilter.from_file()
        self.out_dir = Path(out_dir)
        self.store = ArticleStore(store_path if store_path else self.out_dir / ".store.json")
        self.briefer = briefer         # None leaves briefs off; see briefs.py
        self.morning_hours = morning_hours
        self.last: dict | None = None

    def refresh(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        articles, results = fetch_all(self.plugins)
        windows = {s.id: s.max_age_hours for s in self.catalog.sections}
        # Skip items already older than their section's window, so they don't
        # get stored, pruned and counted as "new" again on every refresh.
        fresh = [a for a in dedupe(articles)
                 if a.when is None or a.when >= now - timedelta(hours=windows.get(a.category, 36))]
        new = self.store.merge(fresh, now)
        self.store.prune(windows, now)
        self.store.save()
        digest = build_digest(self.store.all(), results, self.paywall, self.catalog, now=now)
        digest["stats"]["new_this_refresh"] = new
        digest["sources"] = [p.info() for p in self.plugins]
        if self.briefer:
            try:
                digest["stats"]["briefs"] = self.briefer.update(digest, now)
            except Exception:  # noqa: BLE001 — the news still has to go out
                log.exception("briefs failed; publishing without new ones")
        write_split(digest, self.out_dir)
        if self.briefer:
            write_morning(digest, self.out_dir, self.briefer.path.parent, self.morning_hours, now)
        self.last = digest
        return digest


def run(sources_path: Path = DEFAULT_SOURCES, paywall_path: Path | None = None,
        only: set[str] | None = None, **kw) -> dict:
    """One-off run without the rolling store (e.g. for a single JSON file)."""
    catalog = Catalog.from_file()
    plugins = load_sources(sources_path, catalog, only)
    articles, results = fetch_all(plugins)
    pw = PaywallFilter.from_file(paywall_path) if paywall_path else PaywallFilter.from_file()
    digest = build_digest(articles, results, pw, catalog, **kw)
    digest["sources"] = [p.info() for p in plugins]
    return digest
