"""Work out which ATS each company is on, and its exact board slug.

Reports only. This never touches config/tokens.json — copy the confident matches in
by hand.

    python src/resolve_ats.py                       # every company in COMPANIES
    python src/resolve_ats.py "Alexi" "Q4" "League"  # just the names given

Writes ats_resolution.csv and prints the table, a ready-to-paste tokens block of the
MATCH-confidence slugs, and a list of companies that need a manual look.
"""

import csv
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger("resolve")

COMPANIES = [
    "Alexi", "Varicent", "League", "Dialogue", "Super.com", "Jane App", "Datadog", "Q4",
    "Float", "Trellis AgTech", "Wisedocs", "Relay", "Manifest Climate", "BrainBox AI",
    "Real Matters", "PolicyMe", "Altus Group", "Carbon Upcycling", "Later", "Felix Health",
    "Xanadu", "Convex Energy", "EnergyPal", "Wealthsimple", "Clio", "BioRender", "PocketHealth",
    "Cohere", "Shopify", "Overstory", "Instacart", "Dropbox", "Vena Solutions", "Tali AI",
    "Prenuvo", "ecobee", "Workday",
]

# Hand-supplied slugs, tried before the generated candidates. Use these when a company's
# board slug cannot be derived from its name (legal-entity names, "inc" suffixes, rebrands).
SLUG_OVERRIDES = {
    "Alexi": ["alexsei"],
    "Q4": ["q4inc", "q4-inc"],
    "Wisedocs": ["wisedocsai", "wisedocs-inc"],
    "League": ["leagueinc", "getleague", "league-inc"],
    "Dialogue": ["dialoguetech", "dialoguehealth", "dialogue-health"],
    "BrainBox AI": ["brainboxai", "brainbox-ai", "brainbox-technologies"],
}

GREENHOUSE_BOARD = "https://boards-api.greenhouse.io/v1/boards/{slug}"
GREENHOUSE_JOBS = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_POSTINGS = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_BOARD = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

DELAY_SECONDS = 0.4
REQUEST_TIMEOUT = 15
USER_AGENT = "job-feed-resolver/0.1 (+https://github.com/erikdohnberg/job-feed)"
OUTPUT_CSV = Path(__file__).resolve().parents[1] / "ats_resolution.csv"

# A trailing word that is part of the brand rather than the board slug.
DROP_SUFFIXES = {"ai", "app", "inc", "io", "hq", "labs", "health", "technologies", "solutions", "co"}


# --- slug candidates -------------------------------------------------------


def _tokens(name):
    return [t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if t]


def candidate_slugs(name):
    """Ordered, deduped slug guesses for a company name, likeliest first."""
    tokens = _tokens(name)
    if not tokens:
        return []

    trimmed = tokens[:-1] if len(tokens) > 1 and tokens[-1] in DROP_SUFFIXES else tokens

    candidates = [
        "".join(tokens),  # strip all non-alphanumeric: "Super.com" -> "supercom"
        "-".join(tokens),  # spaces and dots as hyphens: "Jane App" -> "jane-app"
        tokens[0],  # first word only: "BrainBox AI" -> "brainbox"
        "".join(trimmed),  # trailing brand word dropped: "BrainBox AI" -> "brainbox"
        "-".join(trimmed),
        # Lever's path is case-sensitive: jobs.lever.co/PocketHealth resolves and
        # jobs.lever.co/pockethealth 404s. Keep the original casing as a fallback.
        re.sub(r"[^A-Za-z0-9]", "", name),
        "-".join(re.split(r"[^A-Za-z0-9]+", name.strip())),
    ]

    # Hand-supplied slugs go first, in the order given.
    ordered = []
    for candidate in SLUG_OVERRIDES.get(name, []) + candidates:
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _is_weak(name, slug):
    """True when the slug is a shortening rather than the whole company name.

    A shortened slug ("later", "relay", "float") can easily belong to some other
    company, so a hit on one is not on its own evidence of a match.
    """
    tokens = _tokens(name)
    full = {"".join(tokens), "-".join(tokens)}
    return slug.lower() not in full


# --- transport -------------------------------------------------------------


def _get(session, url):
    """GET a url, retrying once on a network error. Returns a response or None."""
    for attempt in (1, 2):
        try:
            return session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 2:
                log.warning("  %s failed twice (%s)", url, exc)
                return None
            log.warning("  %s failed (%s), retrying", url, exc)
            time.sleep(DELAY_SECONDS)
        finally:
            time.sleep(DELAY_SECONDS)
    return None


def _json(response):
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


# --- per-ATS probes --------------------------------------------------------
# Each returns (evidence, open_roles) on a hit, or None when the slug is unknown.


def probe_greenhouse(session, slug):
    payload = _json(_get(session, GREENHOUSE_BOARD.format(slug=slug)))
    if not isinstance(payload, dict):
        return None
    name = payload.get("name") or ""
    jobs = _json(_get(session, GREENHOUSE_JOBS.format(slug=slug)))
    count = len(jobs.get("jobs", [])) if isinstance(jobs, dict) else 0
    return name or "(board has no name)", count


def probe_lever(session, slug):
    payload = _json(_get(session, LEVER_POSTINGS.format(slug=slug)))
    if not isinstance(payload, list):
        return None
    if not payload:
        return "(valid slug, no open roles)", 0
    first = payload[0] if isinstance(payload[0], dict) else {}
    location = ((first.get("categories") or {}).get("location")) or "n/a"
    return f"{first.get('text') or '(untitled)'} — {location}", len(payload)


def probe_ashby(session, slug):
    payload = _json(_get(session, ASHBY_BOARD.format(slug=slug)))
    if not isinstance(payload, dict) or "jobs" not in payload:
        return None
    jobs = payload.get("jobs") or []
    if not jobs:
        return "(valid slug, no open roles)", 0
    first = jobs[0] if isinstance(jobs[0], dict) else {}
    return f"{first.get('title') or '(untitled)'} — {first.get('location') or 'n/a'}", len(jobs)


PROBES = {"greenhouse": probe_greenhouse, "lever": probe_lever, "ashby": probe_ashby}


# --- confidence ------------------------------------------------------------


def _normalize(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def corroborates(company, evidence):
    """True when the evidence text names the company we asked for."""
    left, right = _normalize(company), _normalize(evidence)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    return any(len(t) >= 4 and t in right for t in _tokens(company))


def classify(company, slug, evidence):
    """MATCH when we can stand behind the slug, MISMATCH when it needs a human.

    Greenhouse returns the board's own display name, so it can be checked directly.
    Lever and Ashby return only postings, which rarely name the company — there the
    slug being the full company name is the evidence, and a shortened slug that
    nothing corroborates is treated as MISMATCH so it lands in the manual list.
    """
    if corroborates(company, evidence):
        return "MATCH"
    return "MATCH" if not _is_weak(company, slug) else "MISMATCH"


# --- driver ----------------------------------------------------------------


def resolve(company, session):
    """Rows for every ATS this company resolves on, or one NONE row."""
    rows = []
    for ats, probe in PROBES.items():
        for slug in candidate_slugs(company):
            result = probe(session, slug)
            if result is None:
                continue
            evidence, count = result
            confidence = classify(company, slug, evidence)
            log.info("  %-11s %-22s %-9s %s", ats, slug, confidence, evidence[:60])
            rows.append(
                {
                    "company": company,
                    "ats": ats,
                    "slug": slug,
                    "board_name_or_sample": evidence,
                    "open_roles": count,
                    "confidence": confidence,
                }
            )
            break  # first candidate that resolves wins for this ATS
    if not rows:
        log.info("  no board found on any ATS")
        rows.append(
            {
                "company": company,
                "ats": "",
                "slug": "",
                "board_name_or_sample": "",
                "open_roles": 0,
                "confidence": "NONE",
            }
        )
    return rows


def print_table(rows):
    print(f"\n{'company':<20}{'ats':<12}{'slug':<20}{'roles':>6}  {'conf':<9}evidence")
    print("-" * 118)
    for row in rows:
        print(
            f"{row['company']:<20}{row['ats'] or '-':<12}{row['slug'] or '-':<20}"
            f"{row['open_roles']:>6}  {row['confidence']:<9}{row['board_name_or_sample'][:44]}"
        )


def print_tokens_block(rows):
    block = {"greenhouse": [], "lever": [], "ashby": []}
    for row in rows:
        if row["confidence"] == "MATCH" and row["ats"] in block:
            block[row["ats"]].append(row["slug"])
    print("\n=== tokens.json block (MATCH only — paste in by hand) ===")
    print(json.dumps({k: sorted(set(v)) for k, v in block.items()}, indent=2))


def print_manual_list(rows):
    flagged = {}
    for row in rows:
        if row["confidence"] == "NONE":
            flagged.setdefault(row["company"], []).append("no board found on any ATS")
        elif row["confidence"] == "MISMATCH":
            # Call out overrides: the slug came from SLUG_OVERRIDES, so it resolving is
            # worth more than the confidence rule alone can express.
            source = "override slug" if row["slug"] in SLUG_OVERRIDES.get(row["company"], []) else "unconfirmed"
            flagged.setdefault(row["company"], []).append(
                f"{row['ats']}/{row['slug']} — {source}: {row['board_name_or_sample'][:60]}"
            )
    print("\n=== needs manual check ===")
    if not flagged:
        print("  (none)")
    for company, reasons in flagged.items():
        print(f"  {company}")
        for reason in reasons:
            print(f"      {reason}")


def main(argv=None):
    """Resolve every company in COMPANIES, or just the names passed on the command line."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argv = sys.argv[1:] if argv is None else argv
    targets = argv or COMPANIES

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    rows = []
    for company in targets:
        log.info("%s", company)
        rows.extend(resolve(company, session))

    with OUTPUT_CSV.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["company", "ats", "slug", "board_name_or_sample", "open_roles", "confidence"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print_table(rows)
    print_tokens_block(rows)
    print_manual_list(rows)
    print(f"\nWrote {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
