"""Fetchers for each ATS. One function per provider, each returning normalized rows.

Normalized row shape (a plain dict), identical across all three providers:

    {
        "company":     str,   # the board token/slug from config/tokens.json
        "ats":         str,   # "greenhouse" | "lever" | "ashby"
        "title":       str,
        "location":    str,
        "remote":      bool,
        "url":         str,   # apply/posting URL, used as the dedup key
        "posted_at":   str,   # ISO 8601 UTC, or "" when the board gives us nothing
        "description": str,   # plain text
    }

Every fetcher is failure-tolerant: a non-200, a missing board, or a malformed
response logs a warning naming the token and returns an empty list, so one bad
board never takes down the run.
"""

import html
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

REQUEST_TIMEOUT = 20
DELAY_SECONDS = 0.5
USER_AGENT = "job-feed/0.1 (+https://github.com/erikdohnberg/job-feed)"

# Substrings that mean "remote" when a board gives us no explicit flag.
REMOTE_HINTS = ("remote", "anywhere", "distributed", "work from home", "wfh")


# --- text helpers ----------------------------------------------------------

# Tags that should become a line break so paragraphs and bullets stay readable.
_BLOCK_BREAK = re.compile(r"(?i)<\s*/?\s*(br|p|div|li|tr|h[1-6]|ul|ol)\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_INLINE_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_RUN = re.compile(r"\n{3,}")


def clean_text(text):
    """Collapse whitespace in already-plain text without touching paragraph breaks."""
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _INLINE_WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip()


def strip_html(raw):
    """Turn an HTML description into plain text.

    Greenhouse returns `content` as *escaped* HTML (`&lt;p&gt;...`), so we unescape
    once up front to expose the tags, and once more at the end to resolve entities
    that were left in the visible text (`&amp;`, `&nbsp;`).
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    return clean_text(html.unescape(text))


def _iso(value):
    """Normalize a board's timestamp to ISO 8601 UTC. Returns "" if there is none.

    Accepts ISO strings (Greenhouse, Ashby) and millisecond epochs (Lever, as either
    a number or a numeric string).
    """
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            seconds = int(float(value)) / 1000
            return _stamp(datetime.fromtimestamp(seconds, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text  # keep whatever the board gave us rather than dropping it
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _stamp(parsed)


def _stamp(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def infer_remote(location, title, flag=None, workplace_type=None):
    """Trust the board's own remote signal when there is one, else read the text."""
    if isinstance(flag, bool):
        return flag
    if workplace_type:
        return str(workplace_type).strip().lower() == "remote"
    haystack = f"{location} {title}".lower()
    return any(hint in haystack for hint in REMOTE_HINTS)


# --- transport -------------------------------------------------------------


def _get_json(url, label):
    """GET a board and return parsed JSON, or None on any failure. Never raises."""
    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if response.status_code != 200:
            log.warning("%s: HTTP %s from %s", label, response.status_code, url)
            return None
        try:
            return response.json()
        except ValueError:
            log.warning("%s: response was not valid JSON", label)
            return None
    except requests.RequestException as exc:
        log.warning("%s: request failed (%s)", label, exc)
        return None
    finally:
        time.sleep(DELAY_SECONDS)


# --- fetchers --------------------------------------------------------------


def fetch_greenhouse(token):
    """Greenhouse board: response is {"jobs": [...]}."""
    label = f"greenhouse/{token}"
    payload = _get_json(GREENHOUSE_URL.format(token=token), label)
    if payload is None:
        return []
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        log.warning("%s: no .jobs array in response", label)
        return []

    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = (job.get("title") or "").strip()
        location = ((job.get("location") or {}).get("name") or "").strip()
        url = job.get("absolute_url") or ""
        if not (title and url):
            log.warning("%s: skipping a job with no title or url", label)
            continue
        rows.append(
            {
                "company": token,
                "ats": "greenhouse",
                "title": title,
                "location": location,
                "remote": infer_remote(location, title),
                "url": url,
                "posted_at": _iso(job.get("updated_at") or job.get("first_published")),
                "description": strip_html(job.get("content")),
            }
        )
    log.info("%s: %d postings", label, len(rows))
    return rows


def fetch_lever(slug):
    """Lever board: response is a top-level array of postings."""
    label = f"lever/{slug}"
    payload = _get_json(LEVER_URL.format(slug=slug), label)
    if payload is None:
        return []
    if not isinstance(payload, list):
        log.warning("%s: expected a top-level array in response", label)
        return []

    rows = []
    for job in payload:
        if not isinstance(job, dict):
            continue
        title = (job.get("text") or "").strip()
        categories = job.get("categories") or {}
        location = (categories.get("location") or "").strip()
        url = job.get("hostedUrl") or job.get("applyUrl") or ""
        if not (title and url):
            log.warning("%s: skipping a posting with no title or url", label)
            continue
        description = job.get("descriptionPlain")
        rows.append(
            {
                "company": slug,
                "ats": "lever",
                "title": title,
                "location": location,
                # Lever exposes workplaceType ("remote"/"onsite"/"hybrid") on most boards.
                "remote": infer_remote(location, title, workplace_type=job.get("workplaceType")),
                "url": url,
                "posted_at": _iso(job.get("createdAt")),
                "description": clean_text(description) if description else strip_html(job.get("description")),
            }
        )
    log.info("%s: %d postings", label, len(rows))
    return rows


def fetch_ashby(slug):
    """Ashby job board: response is {"jobs": [...]}."""
    label = f"ashby/{slug}"
    payload = _get_json(ASHBY_URL.format(slug=slug), label)
    if payload is None:
        return []
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        log.warning("%s: no .jobs array in response", label)
        return []

    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("isListed") is False:
            continue  # not public on the board yet
        title = (job.get("title") or "").strip()
        location = (job.get("location") or "").strip()
        url = job.get("jobUrl") or job.get("applyUrl") or ""
        if not (title and url):
            log.warning("%s: skipping a job with no title or url", label)
            continue
        description = job.get("descriptionPlain")
        rows.append(
            {
                "company": slug,
                "ats": "ashby",
                "title": title,
                "location": location,
                "remote": infer_remote(location, title, flag=job.get("isRemote")),
                "url": url,
                "posted_at": _iso(job.get("publishedAt")),
                "description": clean_text(description) if description else strip_html(job.get("descriptionHtml")),
            }
        )
    log.info("%s: %d postings", label, len(rows))
    return rows


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_all(tokens):
    """Run every fetcher over config/tokens.json and return one flat list of rows."""
    rows = []
    for ats, fetcher in FETCHERS.items():
        for token in tokens.get(ats, []):
            rows.extend(fetcher(token))
    return rows


if __name__ == "__main__":
    # Smoke test: hit one real board per ATS and print the row count and first row.
    import pathlib

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = json.loads((pathlib.Path(__file__).resolve().parents[1] / "config" / "tokens.json").read_text())

    for ats, fetcher in FETCHERS.items():
        tokens = config.get(ats) or []
        if not tokens:
            print(f"\n=== {ats}: no tokens configured ===")
            continue
        token = tokens[0]
        rows = fetcher(token)
        print(f"\n=== {ats}/{token}: {len(rows)} rows ===")
        if rows:
            preview = dict(rows[0])
            preview["description"] = preview["description"][:400] + (
                " …" if len(preview["description"]) > 400 else ""
            )
            print(json.dumps(preview, indent=2))
