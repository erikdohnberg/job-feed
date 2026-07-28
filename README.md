# job-feed

A small, deterministic service: a GitHub Action runs daily, pulls new senior PM roles from company ATS APIs, and commits a `candidates.json` that Cowork reads and scores. No LLM in the pipeline, no database, no proxies — just public JSON GETs, filtering, and dedup.

## Layout

```
config/tokens.json          board tokens/slugs per ATS
config/filters.json         pm_terms, levels, negative_keywords, locations_allow
src/fetch_ats.py            one fetcher per ATS -> normalized rows
src/filter.py               level / location / negative filtering
src/run.py                  orchestrate: fetch -> filter -> dedup -> write
state/seen.json             persisted dedup set (urls already emitted)
candidates.json             output the scorer reads
.github/workflows/job-feed.yml
requirements.txt
```

## Data sources

All public, unauthenticated JSON GETs:

| ATS | Endpoint |
| --- | --- |
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/run.py
```

Python 3.12, `requests` only.

## How dedup works

`state/seen.json` holds every posting URL that has already been emitted. Each run drops
rows whose URL is already in that set, writes the survivors to `candidates.json`, and
adds them to the set. Both files are committed by the Action, so state carries across runs.

## Filtering

A role reaches `candidates.json` only if its title contains a `pm_terms` entry **and** a
`levels` entry, it is remote or matches `locations_allow`, and it hits none of the
`negative_keywords` or the hard exclusions in `src/filter.py` (`associate`, `junior`,
`intern`, `apm`). Exclusions match on whole words, so "International" is not read as
"intern".
