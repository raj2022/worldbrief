# WorldBrief backend

Free, live news and research from credible outlets worldwide, organized into
sections readers can pick. No paywalled links, no paid APIs.

```
sources.yaml ──► plugins (rss · arxiv · gdelt · hackernews · substack · hf_papers · your own)
                    │  polled every few minutes with ETag/Last-Modified (cheap & polite)
                    ▼
     rolling store ─► dedupe ─► paywall filter ─► per-section window ─► cluster into stories
                                                                          │
                                                                          ▼
        index.json  (catalog, breaking, top 5 per section, source directory)
        sections/<id>.json  (top, latest, by_topic, by_region)
```

## Live mode

```bash
pip install -r requirements.txt
python -m worldbrief validate            # check every feed responds (do this first)
python -m worldbrief serve --every 10    # refresh every 10 min and serve over HTTP
```

The app calls:

| Endpoint | What it returns |
|---|---|
| `GET /index.json` | Section catalog, breaking stories, top 5 per section, source directory |
| `GET /sections/<id>.json` | One section: `top` (ranked), `latest` (newest first), `by_topic`, `by_region`. Each story lists its 5 most recent articles; `source_count` is still the full count |
| `GET /sections/<id>.json?full=1` | The same section with every article |
| `GET /health` | Last refresh time, story counts, failing sources, source health |

See `sample_output/` for real examples of both files.

Responses are gzipped when the client sends `Accept-Encoding: gzip`, which
`URLSession` does by default. Every file has an ETag, computed on the
uncompressed body. When the app re-fetches on open and nothing has
changed, it gets a tiny `304` back, so "refresh on every open" costs almost
nothing. Responses say `max-age=60`, so the app is never more than about a
minute behind the server, and the server is never more than one refresh
interval behind publishers.

Why not every 30 seconds? Publishers update RSS every few minutes at most, and
polite polling (5–15 min, conditional GET) keeps feeds from blocking you. 10
minutes is the default; `--every 5` is fine for the world section alone.

**Deploying:** any small always-on host works (a $5 VPS, Fly.io, Railway,
Render). Run `serve` behind HTTPS. If you'd rather not run a server,
`python -m worldbrief run` from cron every 10 minutes writes the same files to
`dist/`, which you can upload to static hosting.

## Sections

`sections.yaml` defines what readers can pick. The catalog is embedded in
`index.json`, so adding a section there plus sources for it in `sources.yaml`
makes it show up in the app without an app update.

| Section | Sub-topics / filters | Kept for |
|---|---|---|
| World & Politics | region filter | 30 h |
| Opinion & Analysis | Editorials, Columns & Op-eds, Policy & Think Tanks, Expert Commentary, Long Reads, Newsletters + region filter | 7 days |
| Business & Economy | | 36 h |
| Technology | | 36 h |
| Science & Research | Physics, Space, AI & ML, Computer Science, Biology, Chemistry & Materials, Earth Science, Science News | 4 days |
| Health & Medicine | | 36 h |
| Climate & Environment | | 36 h |
| Sports | | 36 h |
| Culture & Arts | | 36 h |

`python -m worldbrief sections` lists them with source counts.

## Source labels

Every source has an `ownership` label that the app shows next to stories:

- `independent`: privately or reader-funded, editorially independent
- `public`: publicly funded with editorial independence (BBC, DW, ABC, NHK, RNZ...)
- `state_linked`: government-owned or government-aligned; kept only where it adds a
  perspective, always labelled
- `aggregator`: GDELT / Hacker News, where each link's publisher varies

State propaganda outlets (RT, Sputnik, Xinhua, CGTN, Press TV, KCNA, TeleSUR...)
are not included at all.

`limited_in` marks sites that limit free reading in some countries, such as
bbc.com, which has had a dynamic paywall for US visitors since June 2025
(select breaking news stays free). The app can deprioritize these for readers
in those countries.

## Adding sources

**An RSS feed:** one line in `sources.yaml`.

```yaml
- {type: rss, name: Premium Times, url: "https://www.premiumtimesng.com/feed", category: world, region: africa}
```

**Opinion pieces mixed into a main feed:** filter by URL.

```yaml
- {type: rss, name: Middle East Eye Opinion, url: "https://www.middleeasteye.net/rss",
   link_contains: ["/opinion/"], category: opinion, topic: columns, region: middle_east}
```

**A Substack newsletter:** paid-only posts are filtered out automatically.

```yaml
- {type: substack, name: Noahpinion, site: "https://www.noahpinion.blog", topic: newsletters}
```

**A new kind of source:** drop a file in `worldbrief/plugins/`.

```python
from ..plugin import SourcePlugin, register

@register("my_api")
class MyAPI(SourcePlugin):
    def fetch(self):
        data = self.http_get("https://example.org/api/latest").json()
        return [self.make_article(title=x["title"], url=x["url"]) for x in data]
```

## Rules the backend follows

- Stores only headlines and short feed snippets (≤ 400 characters), never full
  text, and every item links to the publisher. The court in AP v. Meltwater
  (2013) ruled against an aggregator that copied substantial article text, and
  EU press-publisher rights exempt links and very short extracts.
- Paywalled domains are dropped (`paywall.yaml`); review the list now and then.
- arXiv: at most one request every 3 seconds across all machines.
- Set your contact email in `USER_AGENT` (`worldbrief/plugin.py`).

## Tests

```bash
python -m pytest -q      # offline: fixtures, live store, server, ETags
```
