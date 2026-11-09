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
| `GET /morning.json` | The latest brief, a new edition every 3 hours |
| `GET /markets.json` | Prices for the app's market dashboard (see Markets below) |
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

## Deploying

The digest is hosted for free on GitHub Pages. `.github/workflows/refresh.yml`
runs `python -m worldbrief run` and publishes `dist/`. GitHub's own cron rarely
fires for this repo, so `ticker.yml` starts a refresh every 5 minutes. It runs
for 5.5 hours, then starts its own replacement. Its 2-hourly cron restarts it if
that chain ever breaks. Every run updates market prices, but `--min-source-minutes 14`
skips a feed fetched in the last 14 minutes, so publishers are still polled about
every 15 minutes. Skipped feeds' stories come from the store. The app
reads it from `https://raj2022.github.io/worldbrief/`:

| File | What it holds |
|---|---|
| `index.json` | Section catalog, breaking stories, top 5 per section, source directory |
| `sections/<id>.json` | One section, with each story's 5 most recent articles |
| `sections/<id>.full.json` | The same section with every article |
| `health.json` | Last refresh time, story counts, failing sources |
| `markets.json` | Market prices, today's price path and the day's biggest movers |

The rolling store sits in `.cache/`, outside the published files. Actions cache
carries it between runs, and GitHub removes a cache after 7 days unused. If the
workflow stays off longer than that, the store starts again from the current feeds.

Scheduled runs can start late when GitHub is busy, so news can be about 20 minutes
behind publishers, and prices about 5 minutes behind Yahoo.

`keepalive.yml` stops GitHub disabling the schedules after 60 days without activity.

Each run tests the code before fetching anything. If a test fails, the run stops and
the last good digest stays published, so a bad push can't replace the news. The app
says "May be out of date" once the digest is over an hour old.

To run a refresh now, use `gh workflow run refresh.yml`. Publishers see the repo URL
as the contact. To use an email, set a `WORLDBRIEF_CONTACT` Actions variable.

The latest edition stays in `.cache/` with the store, and `morning.json` is published
with the other files. A new edition comes out every 3 hours (`WORLDBRIEF_MORNING_HOURS_UTC`
changes the hours), and the app names it Morning, Afternoon, Evening or Night brief
from the reader's local time.

## Markets

`markets.yaml` lists what the app's market dashboard shows: indices for India
(including mid caps, small caps and sectors), the Americas, Europe, Asia-Pacific, the
Middle East and Africa, plus currencies, commodities, US Treasury yields and crypto,
and two lists of large stocks for the day's top gainers and losers. Every refresh
fetches them from Yahoo Finance's public chart data, which needs no key and is about
15 minutes delayed for most exchanges.

Prices never hold up the news. If Yahoo doesn't answer, each instrument keeps its last
good price, saved in `.cache/.markets.json`, with its own trade time. Run
`python -m worldbrief markets` to check every symbol answers.

Business & Economy has subtopics for market news, the economy and central banks,
commodities and currencies, crypto, and Market Commentary: editorials, columns and
investing blogs. Commentary stays for a week, because a subtopic can set its own
`max_age_hours` in `sections.yaml`.

`Dockerfile` and `fly.toml` run the live `serve` mode on a server instead, if you
ever need refreshes faster than this. Feeds can block data-center addresses that
accept a home connection, so check `failed_sources` in `health.json` after changes.

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
| Health & Medicine | Health News, Medical Research, Fitness & Wellbeing, Nutrition, Mental Health | 7 days |
| Climate & Environment | | 36 h |
| Sports | Top Sports, Cricket, Kabaddi, Football, Tennis, F1 & Motorsport, Basketball & US Sports, Olympics & Athletics, Rugby & Golf | 3 days |
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

## Briefs

Every story covered by 2 or more outlets gets two extra fields, built in pure Python
by `worldbrief/extract.py` with no API and no model:

- `brief`: the 2 or 3 most central sentences across the outlets' snippets, under 400
  characters, with `model: "extractive"` and an empty `why_it_matters`. A story has
  no `brief` if its snippets hold no complete sentence that adds to the headline, and
  one sentence if that's all they offer.
- `framing`: up to 4 headlines from different outlets, preferring different regions
  and ownership types, each with `source`, `ownership`, `region`, `headline` and `url`.

The iOS app adds a summary on the phone itself where Apple Intelligence is available.

AI briefs from the Anthropic API are optional and off. They need
`pip install -r requirements-ai.txt` and `ANTHROPIC_API_KEY`, and they replace the free
`brief` on the stories they cover. The GitHub workflow installs neither, so it never
calls a paid API.

## Rules the backend follows

- Stores only headlines and short feed snippets (≤ 400 characters), never full
  text, and every item links to the publisher. The court in AP v. Meltwater
  (2013) ruled against an aggregator that copied substantial article text, and
  EU press-publisher rights exempt links and very short extracts.
- Briefs cost nothing and are built only from those headlines and snippets. See Briefs.
- Paywalled domains are dropped (`paywall.yaml`); review the list now and then.
- Journals (`type: journal`) are the exception: they're chosen by hand and link to free
  abstract pages, so they pass that filter. Each one's `note` says if the full text is
  open access. Papers never group into stories, and they rank below reporting.
- Live blogs, videos, galleries, podcasts and recurring pages are dropped
  (`worldbrief/quality.py`), so every headline opens something readable.
- arXiv: at most one request every 3 seconds across all machines.
- `WORLDBRIEF_CONTACT` sets the contact sent to publishers.

## Tests

```bash
python -m pytest -q      # offline: fixtures, live store, server, ETags
```
