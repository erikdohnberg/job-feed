# job-feed

A small, deterministic service: a GitHub Action runs on weekday mornings, pulls new senior PM roles from company ATS APIs, and commits a `candidates.json` that a separate scorer reads. No LLM in the pipeline, no database, no proxies — just public JSON GETs, filtering, and dedup.

Each run does four things:

1. **Fetch** every board in `config/tokens.json` across Greenhouse, Lever, and Ashby.
2. **Filter** to senior-level product roles in allowed locations, per `config/filters.json`.
3. **Dedup** against `state/seen.json`, so a role is only ever emitted once.
4. **Write** the survivors to `candidates.json` and add their URLs to the seen set.

## Where the output lands

`candidates.json` at the repo root, committed back by the Action:

```json
{
  "generated_at": "2026-07-28T06:00:04Z",
  "window_days": 7,
  "count": 2,
  "roles": [
    {
      "company": "wealthsimple",
      "ats": "ashby",
      "title": "Staff Product Manager, Crypto",
      "location": "Remote (Canada)",
      "remote": true,
      "url": "https://jobs.ashbyhq.com/wealthsimple/...",
      "posted_at": "2026-07-25T14:02:11Z",
      "first_seen": "2026-07-26T06:00:03Z",
      "description": "…full plain-text description…"
    }
  ]
}
```

`roles` is a **rolling window**: every role first seen in the last `WINDOW_DAYS`
(7, set in `src/run.py`), newest first, not just the roles found on the latest run. A
role stays in the feed for a week, so nothing is missed by skipping a morning.

Two things take a role out of the window: its `first_seen` date ageing past 7 days, or
the posting coming down off its board — a closed role stops being emitted even if it was
first seen inside the window. `posted_at` is the board's own date; `first_seen` is when
this service first emitted it, and drives the window.

The file is left untouched when the role set has not changed, so `generated_at` marks
the last time the feed actually moved.

## Adding or removing companies

Edit `config/tokens.json` — the value is the board's slug, which you can read off the company's careers URL:

| ATS | Careers URL looks like | Put this in `tokens.json` |
| --- | --- | --- |
| Greenhouse | `boards.greenhouse.io/overstory` | `"overstory"` under `greenhouse` |
| Lever | `jobs.lever.co/pockethealth` | `"pockethealth"` under `lever` |
| Ashby | `jobs.ashbyhq.com/wealthsimple` | `"wealthsimple"` under `ashby` |

```json
{
  "greenhouse": ["overstory", "instacart", "dropbox"],
  "lever": [],
  "ashby": ["wealthsimple"]
}
```

Removing a token stops future fetches from that board; roles already emitted stay in `state/seen.json`. A bad or dead slug logs a warning naming the token and returns zero rows — it never fails the run — so check the Action log after adding one.

## Filtering

A role is kept if its title contains a `pm_terms` entry **and** a `levels` entry, it is remote or matches `locations_allow`, and it hits none of the `negative_keywords` or the hard exclusions in `src/filter.py` (`associate`, `junior`, `intern`, `apm`). Exclusions match on whole words, so "International" is not read as "intern".

Widen or narrow the feed by editing `config/filters.json`; no code change needed.

## Data sources

All public, unauthenticated JSON GETs:

| ATS | Endpoint |
| --- | --- |
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` |

## Run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/run.py
```

Python 3.12, `requests` only. HTML stripping uses the standard library.

`python src/fetch_ats.py` hits one board per ATS and prints the first normalized row — handy for checking a newly added token.

## Checking a batch of new tokens

After adding tokens, run a dry run before the next scheduled run. It fetches and filters
but writes nothing, so a bad slug or an over-loose filter shows up before it reaches
`candidates.json`:

```bash
python src/run.py --dry-run
```

```
ats         company                   fetched  passed
-----------------------------------------------------
greenhouse  overstory                      12       2
greenhouse  instacart                     124      13
lever       pockethealth                    0       0   <- nothing fetched
ashby       wealthsimple                   39       2
-----------------------------------------------------
total                                     175      17
```

`<- nothing fetched` means a dead or misspelled slug. A company whose `passed` count
looks implausibly high is a sign the filters need tightening.

## Notion push

Every new role is also created as a row in the Notion **Pipeline** database. `candidates.json`
remains the debug/audit artifact; the Pipeline rows are the working copy.

Auth is a Notion internal integration token in `NOTION_TOKEN`, supplied to the Action from
the repository secret of the same name. With no token set the push is skipped and the rest
of the run proceeds, so local runs need no credentials.

`src/notion_push.py` reads the data source schema once per run and maps onto whatever
property names and types Notion reports, so a renamed property is skipped with a warning
rather than failing the row. The mapping:

| Pipeline property | Value |
| --- | --- |
| Role Title (title) | role title |
| Company (text) | board token |
| JD Link (url) | posting url |
| Source (select) | `job-feed` |
| Submission Status (select) | `Pending` |
| Stage (select) | `Watching` |

Fit Score, Bucket and Network Score are left empty for the scorer to fill.

The JD goes into the **page body** as paragraph blocks chunked to 1900 characters, since a
Notion property caps out at 2000. Chunks reassemble to the original exactly, and a
description longer than 100 blocks is appended in follow-up calls rather than truncated.

Failures never break the run: a non-2xx logs a warning and moves on, and a role that did
not reach Notion is deliberately **not** recorded in `state/seen.json`, so the next run
retries it instead of dropping it.

## Regional duplicates

Some companies open one requisition per region for the same job — Instacart posts a US
copy and a Canada copy, each with its own URL, so URL-keyed dedup cannot tell they are
the same role. Each run therefore keeps one row per `(company, title)`, preferring the
posting whose location matches `PREFERRED_LOCATIONS` in `src/run.py` (`canada`,
`toronto`, `ontario`); ties break on URL so the choice is stable between runs. Dropped
copies are logged:

```
collapsed regional copy: [instacart] Senior Product Manager, Ads Quality — United States - Remote
```

A role posted in only one region is never affected. The dropped copy's URL is not added
to the seen set, so if the preferred posting later closes, the remaining one is emitted.

## Dedup state

`state/seen.json` maps every emitted url to the date it was first seen:

```json
{ "urls": { "https://jobs.ashbyhq.com/wealthsimple/...": "2026-07-28T11:00:04Z" } }
```

Entries older than 60 days (`SEEN_TTL_DAYS` in `src/run.py`) are pruned on each run to
keep the file small. A role still open after 60 days will therefore be emitted a second
time.

## Schedule

`.github/workflows/job-feed.yml` runs daily at 06:00 UTC — 02:00 in Toronto while EDT is
in effect, 01:00 once EST resumes, since GitHub cron is UTC only and does not follow
daylight saving. Change the `cron:` line to `0 7 * * *` to hold 02:00 through the winter
instead. It also runs on demand via **Actions → job-feed → Run workflow**, and commits
`candidates.json` and `state/seen.json` only when they change.

## Layout

```
config/tokens.json          board tokens/slugs per ATS
config/filters.json         pm_terms, levels, negative_keywords, locations_allow
src/fetch_ats.py            one fetcher per ATS -> normalized rows
src/filter.py               level / location / negative filtering
src/run.py                  orchestrate: fetch -> filter -> dedup -> write
state/seen.json             persisted dedup set (urls already emitted)
candidates.json             output the scorer reads
```
