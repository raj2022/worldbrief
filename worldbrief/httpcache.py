"""Conditional GET cache (ETag / Last-Modified).

When we poll feeds every few minutes, most haven't changed. Sending
If-None-Match / If-Modified-Since lets the publisher answer "304 Not
Modified" with no body, which is faster for us and polite to them.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import requests


@dataclass
class CachedResponse:
    content: bytes
    status_code: int
    from_cache: bool = False

    def json(self):
        import json
        return json.loads(self.content)

    def raise_for_status(self):  # parity with requests.Response
        pass


class HTTPCache:
    def __init__(self):
        self._entries: dict[str, tuple[str | None, str | None, bytes]] = {}
        self._lock = threading.Lock()
        self.hits = 0

    @staticmethod
    def _key(url: str, params: dict | None) -> str:
        return url + ("?" + "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else "")

    def get(self, url: str, params: dict | None, headers: dict, timeout: float) -> CachedResponse:
        key = self._key(url, params)
        with self._lock:
            entry = self._entries.get(key)
        h = dict(headers)
        if entry:
            etag, last_mod, _ = entry
            if etag:
                h["If-None-Match"] = etag
            if last_mod:
                h["If-Modified-Since"] = last_mod
        r = requests.get(url, params=params, headers=h, timeout=timeout)
        if r.status_code == 304 and entry:
            with self._lock:
                self.hits += 1
            return CachedResponse(entry[2], 200, from_cache=True)
        r.raise_for_status()
        etag, last_mod = r.headers.get("ETag"), r.headers.get("Last-Modified")
        if etag or last_mod:
            with self._lock:
                self._entries[key] = (etag, last_mod, r.content)
        return CachedResponse(r.content, r.status_code)


# One cache for the process: the live server reuses it across refreshes.
SHARED = HTTPCache()
