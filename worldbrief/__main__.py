"""CLI.

    python -m worldbrief serve --every 10             # LIVE: refresh every 10 min + HTTP API
    python -m worldbrief run                          # one refresh -> dist/ (for cron)
    python -m worldbrief run --only world,science     # just these sections
    python -m worldbrief run --single-file digest.json
    python -m worldbrief validate [--only sports]     # check sources respond
    python -m worldbrief sections                     # list the section catalog
    python -m worldbrief plugins                      # list plugin types
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import pipeline, plugin
from .catalog import Catalog


def _only(value: str | None) -> set[str] | None:
    return {s.strip() for s in value.split(",") if s.strip()} if value else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worldbrief")
    ap.add_argument("--sources", type=Path, default=pipeline.DEFAULT_SOURCES)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="fetch sources and write the digest")
    r.add_argument("--out-dir", type=Path, default=Path("dist"),
                   help="writes index.json + sections/<id>.json (default: dist/)")
    r.add_argument("--single-file", type=Path, default=None,
                   help="write everything to one JSON file instead")
    r.add_argument("--only", help="comma-separated section ids, e.g. world,science")
    r.add_argument("--paywall", type=Path, default=None)
    r.add_argument("--max-age-hours", type=float, default=None,
                   help="override every section's window (single-file mode only)")

    sv = sub.add_parser("serve", help="live mode: refresh on a timer and serve the digest over HTTP")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--every", type=float, default=10, help="minutes between refreshes (default 10)")
    sv.add_argument("--out-dir", type=Path, default=Path("dist"))
    sv.add_argument("--only", help="comma-separated section ids")

    v = sub.add_parser("validate", help="fetch each source once and report health")
    v.add_argument("--only", help="comma-separated section ids")

    sub.add_parser("sections", help="list sections users can pick")
    sub.add_parser("plugins", help="list available plugin types")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if args.cmd == "plugins":
        print("\n".join(plugin.available_types()))
        return 0

    if args.cmd == "sections":
        catalog = Catalog.from_file()
        plugins = pipeline.load_sources(args.sources, catalog)
        for s in catalog.sections:
            n = sum(p.category == s.id for p in plugins)
            extra = f"  topics: {', '.join(t['id'] for t in s.subtopics)}" if s.subtopics else ""
            extra += "  [by region]" if s.by_region else ""
            print(f"{'●' if s.default else '○'} {s.id:<11} {s.title:<22} {n:>2} sources{extra}")
        return 0

    if args.cmd == "validate":
        plugins = pipeline.load_sources(args.sources, only=_only(args.only))
        _, results = pipeline.fetch_all(plugins)
        width = max(len(x.name) for x in results)
        for x in results:
            status = f"OK   {x.count:>3} items" if x.ok and x.count else ("EMPTY        " if x.ok else "FAIL         ")
            print(f"{x.section:<11} {x.name:<{width}}  {status}  {x.seconds:>5}s  {x.error[:80]}")
        bad = [x for x in results if not x.ok or not x.count]
        print(f"\n{len(results) - len(bad)}/{len(results)} sources healthy")
        return 1 if bad else 0

    if args.cmd == "serve":
        from .server import serve
        logging.getLogger().setLevel(logging.INFO)
        refresher = pipeline.Refresher(args.sources, args.out_dir, only=_only(args.only))
        serve(refresher, args.host, args.port, args.every)
        return 0

    if args.single_file:
        digest = pipeline.run(args.sources, args.paywall, only=_only(args.only),
                              max_age_hours=args.max_age_hours)
        args.single_file.write_text(json.dumps(digest, ensure_ascii=False, indent=2))
        where = args.single_file
    else:
        # Uses the rolling store in out_dir, so running this from cron every
        # few minutes behaves like the live server.
        digest = pipeline.Refresher(args.sources, args.out_dir, paywall_path=args.paywall,
                                    only=_only(args.only)).refresh()
        where = f"{args.out_dir}/ (index.json + {len(digest['sections'])} section files)"
    s = digest["stats"]
    print(f"wrote {where}: {s['stories']} stories from {s['sources_ok']} sources "
          f"({s['sources_failed']} failed, {s['paywalled_dropped']} paywalled links dropped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
