# job-feed
A small, deterministic service: a GitHub Action runs daily, pulls new senior PM roles from company ATS APIs, and commits a `candidates.json` that Cowork reads and scores. No LLM in the pipeline, no database, no proxies — just public JSON GETs, filtering, and dedup.
