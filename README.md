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
  "generated_at": "2026-07-28T11:00:04Z",
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
      "description": "…full plain-text description…"
    }
  ]
}
```

`roles` holds only what is **new since the last run** — a quiet day is `count: 0`, not a repeat of yesterday. The file is left untouched when the set has not changed, so `generated_at` marks the last time the feed actually moved.

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

## Schedule

`.github/workflows/job-feed.yml` runs at 11:00 UTC Monday–Friday, and on demand via
**Actions → job-feed → Run workflow**. It commits `candidates.json` and `state/seen.json`
only when they change.

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
