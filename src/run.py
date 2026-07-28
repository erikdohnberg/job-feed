"""Entry point: fetch -> filter -> dedup -> write candidates.json.

Run with:  python src/run.py
           python src/run.py --dry-run    # fetch and filter, print counts, write nothing

Reads  config/tokens.json, config/filters.json, state/seen.json
Writes candidates.json, state/seen.json
"""

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_ats
from filter import apply_filters

log = logging.getLogger("run")

ROOT = Path(__file__).resolve().parents[1]
TOKENS_PATH = ROOT / "config" / "tokens.json"
FILTERS_PATH = ROOT / "config" / "filters.json"
SEEN_PATH = ROOT / "state" / "seen.json"
OUTPUT_PATH = ROOT / "candidates.json"

# Urls first seen longer ago than this are dropped from state/seen.json to keep it small.
SEEN_TTL_DAYS = 60

# Some companies open one req per region for the same job (Instacart posts a US and a
# Canada copy). When that happens, keep the posting whose location matches one of these.
PREFERRED_LOCATIONS = ("canada", "toronto", "ontario")


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
    """Return {url: first-seen timestamp} for urls already emitted.

    Missing file means nothing has been seen yet. The original format was a plain
    list of urls with no dates; those are migrated by stamping them as first seen
    now, which is the earliest date we can honestly claim.
    """
    data = load_json(SEEN_PATH, default={"urls": {}})
    urls = data.get("urls", {}) if isinstance(data, dict) else data
    if isinstance(urls, list):
        log.info("migrating %d seen urls to the dated format", len(urls))
        stamp = _now()
        return {url: stamp for url in urls}
    return dict(urls)


def prune_seen(seen, now=None):
    """Drop urls first seen more than SEEN_TTL_DAYS ago."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=SEEN_TTL_DAYS)
    kept = {}
    for url, first_seen in seen.items():
        try:
            stamped = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
        except ValueError:
            # Unreadable date: keep the url so we do not re-emit it, and reset its clock.
            kept[url] = _now()
            continue
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        if stamped >= cutoff:
            kept[url] = first_seen
    return kept


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_candidates(roles):
    """Write candidates.json, leaving the file untouched when the role set is unchanged.

    Rewriting it every run would churn generated_at, so the scheduled Action would
    produce a commit every weekday even on days when nothing new turns up.
    """
    existing = load_json(OUTPUT_PATH, default={})
    if isinstance(existing, dict) and existing.get("roles") == roles:
        log.info("candidates.json unchanged (%d roles), leaving it as is", len(roles))
        return
    write_json(OUTPUT_PATH, {"generated_at": _now(), "count": len(roles), "roles": roles})


def _role_key(row):
    """Same company, same title = the same job, however many regional copies exist."""
    return row.get("company", ""), " ".join((row.get("title") or "").lower().split())


def _location_rank(row):
    """Sort key: preferred-region postings first, then url so the choice is stable."""
    location = (row.get("location") or "").lower()
    preferred = any(term in location for term in PREFERRED_LOCATIONS)
    return (0 if preferred else 1, row.get("url") or "")


def collapse_regional_duplicates(rows):
    """Keep one row per (company, title), preferring a PREFERRED_LOCATIONS posting."""
    grouped = {}
    for row in rows:
        grouped.setdefault(_role_key(row), []).append(row)

    kept = []
    for group in grouped.values():
        group.sort(key=_location_rank)
        for dropped in group[1:]:
            log.info(
                "  collapsed regional copy: [%s] %s — %s",
                dropped["company"],
                dropped["title"],
                dropped["location"] or "n/a",
            )
        kept.append(group[0])
    return kept


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


def print_report(tokens, rows, passing):
    """Per-company fetched/passed counts, for sanity-checking tokens and filters."""
    fetched_by = {}
    passed_by = {}
    for row in rows:
        fetched_by[(row["ats"], row["company"])] = fetched_by.get((row["ats"], row["company"]), 0) + 1
    for row in passing:
        passed_by[(row["ats"], row["company"])] = passed_by.get((row["ats"], row["company"]), 0) + 1

    # Drive off the config so a board that returned nothing still shows up as a zero.
    boards = [(ats, token) for ats in fetch_ats.FETCHERS for token in tokens.get(ats, [])]
    boards += [key for key in fetched_by if key not in boards]

    print(f"\n{'ats':<12}{'company':<24}{'fetched':>9}{'passed':>8}")
    print("-" * 53)
    for ats, company in boards:
        fetched = fetched_by.get((ats, company), 0)
        passed = passed_by.get((ats, company), 0)
        flag = "   <- nothing fetched" if fetched == 0 else ""
        print(f"{ats:<12}{company:<24}{fetched:>9}{passed:>8}{flag}")
    print("-" * 53)
    print(f"{'total':<36}{len(rows):>9}{len(passing):>8}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Fetch, filter, and emit senior PM roles.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and filter, print per-company counts, and write nothing",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tokens = load_json(TOKENS_PATH)
    filters = load_json(FILTERS_PATH)

    rows = fetch_ats.fetch_all(tokens)
    passing = apply_filters(rows, filters)
    collapsed = collapse_regional_duplicates(passing)
    seen = load_seen()
    new = dedup(collapsed, set(seen))

    if args.dry_run:
        print_report(tokens, rows, passing)
        print(
            f"\n{len(passing)} passed, {len(collapsed)} after collapsing regional copies, "
            f"{len(new)} would be new. Dry run — nothing written."
        )
        return

    write_candidates(new)

    stamp = _now()
    seen.update({row["url"]: stamp for row in new})
    kept = prune_seen(seen)
    dropped = len(seen) - len(kept)
    if dropped:
        log.info("pruned %d urls first seen over %d days ago", dropped, SEEN_TTL_DAYS)
    write_json(SEEN_PATH, {"urls": dict(sorted(kept.items()))})

    log.info(
        "fetched %d, passed filters %d, after collapsing regional copies %d, new %d",
        len(rows),
        len(passing),
        len(collapsed),
        len(new),
    )
    for row in new:
        log.info("  + [%s] %s — %s", row["company"], row["title"], row["location"] or "n/a")


if __name__ == "__main__":
    main()
