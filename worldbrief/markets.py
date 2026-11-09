"""Market prices for the app's dashboard.

Reads the instruments in markets.yaml, asks Yahoo Finance's public chart data
for them in batches, and publishes `markets.json`:

    {
      "generated_at": "...",          # last time any price came in
      "groups": [{"id": "india", "title": "India", "quotes": [quote, ...]}, ...],
      "movers": [{"id": "india", "title": "India large caps",
                  "gainers": [quote, ...], "losers": [quote, ...]}, ...]
    }

A quote is {symbol, name, price, prev_close, change, change_pct, currency, unit,
decimals, market_time, day_high, day_low, year_high, year_low, spark, keywords}.
`spark` is today's price path, oldest first, for a small chart; `keywords` find
the instrument in headlines.

Prices are extras: a failure never stops the news. A symbol that doesn't
answer keeps its last good quote, which carries its own `market_time`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from .plugin import USER_AGENT

log = logging.getLogger(__name__)

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "markets.yaml"
SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
BATCH = 20            # symbols per request; Yahoo accepts up to 20
SPARK_POINTS = 48     # enough for a smooth line, small enough to download often
MOVERS_SHOWN = 5
TIMEOUT = 15


@dataclass
class Instrument:
    symbol: str
    name: str
    unit: str = ""
    decimals: int | None = None
    keywords: list[str] = field(default_factory=list)


@dataclass
class Group:
    id: str
    title: str
    quotes: list[Instrument]


@dataclass
class MoverList:
    id: str
    title: str
    symbols: list[str]


@dataclass
class MarketsConfig:
    groups: list[Group] = field(default_factory=list)
    movers: list[MoverList] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path = DEFAULT_FILE) -> "MarketsConfig":
        data = yaml.safe_load(Path(path).read_text()) or {}
        groups = [Group(g["id"], g["title"], [Instrument(**q) for q in g.get("quotes", [])])
                  for g in data.get("groups", [])]
        movers = [MoverList(m["id"], m["title"], [str(s) for s in m.get("symbols", [])])
                  for m in data.get("movers", [])]
        return cls(groups, movers)

    @property
    def group_symbols(self) -> list[str]:
        return [q.symbol for g in self.groups for q in g.quotes]

    @property
    def mover_symbols(self) -> list[str]:
        return list(dict.fromkeys(s for m in self.movers for s in m.symbols))


def _get(symbols: list[str], interval: str) -> list[dict]:
    r = requests.get(SPARK_URL, params={"symbols": ",".join(symbols), "range": "1d", "interval": interval},
                     headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return (r.json().get("spark") or {}).get("result") or []


def fetch(symbols: list[str], interval: str = "15m", get=None) -> dict[str, dict]:
    """Raw Yahoo results by symbol. A batch that fails is logged and skipped."""
    get = get or _get
    batches = [symbols[i:i + BATCH] for i in range(0, len(symbols), BATCH)]

    def one(batch):
        try:
            return get(batch, interval)
        except Exception as e:  # noqa: BLE001 — one batch must not sink the rest
            log.warning("market prices failed for %s…: %s", batch[0], e)
            return []

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(4) as ex:
        for results in ex.map(one, batches):
            for r in results:
                if r.get("symbol"):
                    out[r["symbol"]] = r
    return out


def _round(x, digits: int):
    return None if x is None else round(float(x), digits)


def _downsample(values: list[float], n: int) -> list[float]:
    if len(values) <= n:
        return values
    step = (len(values) - 1) / (n - 1)
    return [values[round(i * step)] for i in range(n)]


_COMPANY_SUFFIX = re.compile(r",?\s+(Limited|Ltd\.?|Inc\.?|Corporation|Corp\.?|plc|Co\.)$", re.I)


def company_name(meta: dict, symbol: str) -> str:
    """'Apollo Hospitals Enterprise' from 'Apollo Hospitals Enterprise Limited'."""
    name = (meta.get("longName") or meta.get("shortName") or "").strip()
    while name and _COMPANY_SUFFIX.search(name):
        name = _COMPANY_SUFFIX.sub("", name).strip()
    return name or symbol.split(".")[0]


def parse(result: dict, name: str = "", unit: str = "", decimals: int | None = None,
          keywords: list[str] | None = None) -> dict | None:
    """One Yahoo result as a quote, or None when it has no price."""
    resp = (result.get("response") or [None])[0] or {}
    meta = resp.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    digits = decimals if decimals is not None else int(meta.get("priceHint") or 2)
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = price - prev if prev else None
    closes = ((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    spark = [_round(c, digits + 2) for c in closes if c is not None]
    when = meta.get("regularMarketTime")
    symbol = result["symbol"]
    return {
        "symbol": symbol,
        "name": name or company_name(meta, symbol),
        "price": _round(price, digits + 2),
        "prev_close": _round(prev, digits + 2),
        "change": _round(change, digits + 2),
        "change_pct": _round(change / prev * 100, 3) if change is not None else None,
        "currency": meta.get("currency") or "",
        "unit": unit,
        "decimals": digits,
        "market_time": datetime.fromtimestamp(when, timezone.utc).isoformat() if when else None,
        "day_high": _round(meta.get("regularMarketDayHigh"), digits + 2),
        "day_low": _round(meta.get("regularMarketDayLow"), digits + 2),
        "year_high": _round(meta.get("fiftyTwoWeekHigh"), digits + 2),
        "year_low": _round(meta.get("fiftyTwoWeekLow"), digits + 2),
        "spark": _downsample(spark, SPARK_POINTS),
        "keywords": [k.lower() for k in keywords or []],
    }


def build(config: MarketsConfig, groups_raw: dict[str, dict], movers_raw: dict[str, dict],
          previous: dict | None, now: datetime) -> tuple[dict, int]:
    """The payload, and how many group quotes are fresh. Missing quotes fall
    back to `previous`; mover lists are only rebuilt from fresh prices."""
    old = {q["symbol"]: q for g in (previous or {}).get("groups", []) for q in g.get("quotes", [])}
    fresh = 0
    groups = []
    for g in config.groups:
        quotes = []
        for inst in g.quotes:
            q = parse(groups_raw[inst.symbol], inst.name, inst.unit, inst.decimals, inst.keywords) \
                if inst.symbol in groups_raw else None
            if q:
                fresh += 1
            else:
                q = old.get(inst.symbol)
                if q:   # keep the last price, under the name configured now
                    q = {**q, "name": inst.name, "unit": inst.unit, "keywords": [k.lower() for k in inst.keywords]}
            if q:
                quotes.append(q)
        if quotes:
            groups.append({"id": g.id, "title": g.title, "quotes": quotes})

    old_movers = {m["id"]: m for m in (previous or {}).get("movers", [])}
    movers = []
    for m in config.movers:
        quotes = [q for s in m.symbols if s in movers_raw
                  for q in [parse(movers_raw[s])] if q and q["change_pct"] is not None]
        if len(quotes) < len(m.symbols) / 2:   # too few to call the day's movers
            if m.id in old_movers:
                movers.append(old_movers[m.id])
            continue
        quotes.sort(key=lambda q: q["change_pct"], reverse=True)
        for q in quotes:
            q["spark"] = []   # lists show only the move
        movers.append({
            "id": m.id, "title": m.title,
            "gainers": [q for q in quotes if q["change_pct"] > 0][:MOVERS_SHOWN],
            "losers": [q for q in reversed(quotes) if q["change_pct"] < 0][:MOVERS_SHOWN],
        })

    generated = now.isoformat() if fresh else (previous or {}).get("generated_at", now.isoformat())
    return {"generated_at": generated, "groups": groups, "movers": movers}, fresh


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def publish(config: MarketsConfig, out_dir: Path, state_dir: Path, now: datetime,
            fetcher=fetch) -> int:
    """Fetch prices and write out_dir/markets.json. The payload is also kept in
    `state_dir`, because out_dir may be rebuilt from nothing on each run.
    Returns how many group quotes are fresh."""
    saved = Path(state_dir) / ".markets.json"
    try:
        previous = json.loads(saved.read_text())
    except (OSError, ValueError):
        previous = None
    groups_raw = fetcher(config.group_symbols, "15m")
    movers_raw = fetcher(config.mover_symbols, "1d") if config.movers else {}
    payload, fresh = build(config, groups_raw, movers_raw, previous, now)
    if not payload["groups"]:
        log.warning("no market prices, now or saved; not writing markets.json")
        return 0
    if fresh:
        _atomic_write(saved, payload)
    _atomic_write(Path(out_dir) / "markets.json", payload)
    if fresh < len(config.group_symbols):
        log.warning("market prices: %d/%d fresh", fresh, len(config.group_symbols))
    return fresh
