"""Live server: refreshes every few minutes and serves the digest.

    python -m worldbrief serve --port 8080 --every 10

Endpoints (what the iOS app calls):
    GET /index.json            catalog, breaking, top headlines per section
    GET /sections/<id>.json    one section; each story lists at most 5 articles
    GET /sections/<id>.json?full=1    the same section, every article
    GET /health                last refresh time and source health

Every file carries an ETag, so when the app re-fetches on open and nothing
changed it gets a tiny 304 instead of the whole file. Responses are gzipped for
clients that accept it; the ETag is computed on the uncompressed body, so it
doesn't depend on the encoding.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .pipeline import Refresher

log = logging.getLogger(__name__)
_ALLOWED = re.compile(r"^/(index\.json|sections/[a-z0-9_]+\.json)$")


def accepts_gzip(header: str | None) -> bool:
    """True if an Accept-Encoding header allows gzip (`gzip;q=0` refuses it)."""
    for part in (header or "").lower().split(","):
        name, _, params = part.partition(";")
        if name.strip() == "gzip":
            return not re.fullmatch(r"\s*q=0(\.0*)?\s*", params)
    return False


@lru_cache(maxsize=32)
def _gzip(body: bytes) -> bytes:
    """Files only change on refresh, so most requests reuse the last result."""
    return gzip.compress(body, mtime=0)


def make_handler(out_dir: Path, refresher: Refresher, started: float, max_age: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WorldBrief/0.3"

        def log_message(self, fmt, *args):  # route through logging
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype="application/json", etag=None):
            compress = status == 200 and accepts_gzip(self.headers.get("Accept-Encoding"))
            if compress:
                body = _gzip(body)
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", f"public, max-age={max_age}")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Access-Control-Allow-Origin", "*")
            if etag:
                self.send_header("ETag", etag)
            if compress:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            if path == "/health":
                d = refresher.last or {}
                body = json.dumps({
                    "ok": refresher.last is not None,
                    "generated_at": d.get("generated_at"),
                    "uptime_seconds": int(time.time() - started),
                    "stats": d.get("stats"),
                    "failed_sources": [r["name"] for r in d.get("source_health", []) if not r["ok"]],
                }).encode()
                return self._send(200, body)
            if not _ALLOWED.match(path):
                return self._send(404, b'{"error":"not found"}')
            f = out_dir / path.lstrip("/")
            if path.startswith("/sections/") and parse_qs(url.query).get("full") == ["1"]:
                f = f.with_name(f"{f.stem}.full.json")
            if not f.exists():
                # First refresh still running.
                return self._send(503, b'{"error":"warming up, retry in a few seconds"}')
            body = f.read_bytes()
            etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", f"public, max-age={max_age}")
                self.send_header("Vary", "Accept-Encoding")
                self.end_headers()
                return
            return self._send(200, body, etag=etag)

    return Handler


def refresh_loop(refresher: Refresher, every_minutes: float, stop: threading.Event):
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            d = refresher.refresh()
            s = d["stats"]
            log.info("refreshed: %d stories, %d new, %d/%d sources ok, %.1fs",
                     s["stories"], s.get("new_this_refresh", 0), s["sources_ok"],
                     s["sources_ok"] + s["sources_failed"], time.monotonic() - t0)
        except Exception:  # noqa: BLE001 — keep serving the last good digest
            log.exception("refresh failed; serving previous digest")
        # Small jitter so we don't hit every publisher at the exact same second.
        stop.wait(every_minutes * 60 * random.uniform(0.9, 1.1))


def serve(refresher: Refresher, host="0.0.0.0", port=8080, every_minutes: float = 10,
          client_cache_seconds: int = 60) -> None:
    stop = threading.Event()
    t = threading.Thread(target=refresh_loop, args=(refresher, every_minutes, stop), daemon=True)
    t.start()
    handler = make_handler(refresher.out_dir, refresher, time.time(), client_cache_seconds)
    httpd = ThreadingHTTPServer((host, port), handler)
    log.warning("serving on http://%s:%d — refreshing every %s min (%s)",
                host, port, every_minutes, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
