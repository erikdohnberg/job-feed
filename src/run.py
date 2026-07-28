"""Entry point: fetch -> filter -> dedup -> write candidates.json.

Run with:  python src/run.py

Reads  config/tokens.json, config/filters.json, state/seen.json
Writes candidates.json, state/seen.json
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import fetch_ats
from filter import apply_filters

log = logging.getLogger("run")

ROOT = Path(__file__).resolve().parents[1]
TOKENS_PATH = ROOT / "config" / "tokens.json"
FILTERS_PATH = ROOT / "config" / "filters.json"
SEEN_PATH = ROOT / "state" / "seen.json"
OUTPUT_PATH = ROOT / "candidates.json"


def load_json(path, default=None):
    """Read a JSON file, returning `default` if it is missing or unreadable."""
    try:
        with path.open() as handle:
            return json.load(handle)
    except FileNotFoundError:
        if default is None:
            raise
        log.warning("%s not found, using default", path.name)
        return default
    except ValueError:
        if default is None:
            raise
        log.warning("%s is not valid JSON, using default", path.name)
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load_seen():
    """Return the set of urls already emitted. Missing file means nothing seen yet."""
    data = load_json(SEEN_PATH, default={"urls": []})
    urls = data.get("urls", []) if isinstance(data, dict) else data
    return set(urls)


def dedup(rows, seen_urls):
    """Drop rows whose url is already in seen_urls, and any repeats within this run."""
    new = []
    batch = set()
    for row in rows:
        url = row.get("url")
        if not url or url in seen_urls or url in batch:
            continue
        batch.add(url)
        new.append(row)
    return new


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tokens = load_json(TOKENS_PATH)
    filters = load_json(FILTERS_PATH)

    rows = fetch_ats.fetch_all(tokens)
    passing = apply_filters(rows, filters)
    seen = load_seen()
    new = dedup(passing, seen)

    write_json(
        OUTPUT_PATH,
        {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "count": len(new),
            "roles": new,
        },
    )
    write_json(SEEN_PATH, {"urls": sorted(seen | {row["url"] for row in new})})

    log.info("fetched %d, passed filters %d, new %d", len(rows), len(passing), len(new))
    for row in new:
        log.info("  + [%s] %s — %s", row["company"], row["title"], row["location"] or "n/a")


if __name__ == "__main__":
    main()
