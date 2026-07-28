"""Push new roles into the Notion Pipeline database as rows.

candidates.json stays the audit artifact; this is the copy Erik actually works from.

The data source schema is fetched once per run so property names and types come from
Notion itself rather than being hardcoded here. A property this module does not find is
skipped with a warning instead of failing the page.

The JD goes in the page body, not a property: Notion caps a rich text value at 2000
characters and a JD routinely runs longer, so it is chunked into paragraph blocks.

Auth comes from NOTION_TOKEN. With no token set the push is skipped, so local runs work
without credentials.
"""

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
DATA_SOURCE_ID = "d4c83a72-ebd1-4775-a852-f9f3e7faafb8"

REQUEST_TIMEOUT = 30
DELAY_SECONDS = 0.35  # Notion allows ~3 requests/second averaged over time.
BLOCK_CHARS = 1900  # Under Notion's 2000-character cap per rich text object.
MAX_BLOCKS_PER_CALL = 100  # Notion's cap on children per request.
PROPERTY_CHARS = 2000


def _values_for(role):
    """Logical property name -> value. Fit Score, Bucket and Network Score stay empty."""
    return {
        "Role Title": role.get("title") or "(untitled role)",
        "Company": role.get("company") or "",
        "JD Link": role.get("url") or "",
        "Source": "job-feed",
        "Submission Status": "Pending",
        "Stage": "Watching",
    }


def chunk_description(text, size=BLOCK_CHARS):
    """Split a description into <=size pieces, breaking on newlines where possible.

    Concatenating the pieces reproduces the input exactly — the scorer reads this, so
    nothing may be dropped.
    """
    if not text:
        return []
    chunks = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        split = window.rfind("\n")
        if split <= 0:
            split = window.rfind(" ")
        if split <= 0:
            split = size
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _paragraph(text):
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def description_blocks(text):
    return [_paragraph(chunk) for chunk in chunk_description(text)]


def format_value(value, prop_type, name):
    """Shape a value for whatever type the property actually is in Notion."""
    if value in (None, ""):
        return None
    if prop_type == "title":
        return {"title": [{"type": "text", "text": {"content": value[:PROPERTY_CHARS]}}]}
    # The API calls it rich_text; some surfaces report the same type as "text".
    if prop_type in ("rich_text", "text"):
        return {"rich_text": [{"type": "text", "text": {"content": value[:PROPERTY_CHARS]}}]}
    if prop_type == "url":
        return {"url": value}
    if prop_type in ("select", "status"):
        return {prop_type: {"name": value}}
    if prop_type == "multi_select":
        return {"multi_select": [{"name": value}]}
    log.warning("notion: property %r has unsupported type %r, skipping it", name, prop_type)
    return None


def build_properties(role, schema):
    """Map a role onto the data source's real properties, skipping any that are absent."""
    properties = {}
    for name, value in _values_for(role).items():
        if name not in schema:
            log.warning("notion: property %r not found in the Pipeline schema, skipping it", name)
            continue
        formatted = format_value(value, schema[name], name)
        if formatted is not None:
            properties[name] = formatted
    return properties


def _request(session, method, url, **kwargs):
    """Make one Notion call, pausing afterwards to stay under the rate limit.

    Returns the response, or None if the request could not be made at all.
    """
    try:
        response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        log.warning("notion: %s %s failed (%s)", method, url, exc)
        return None
    finally:
        time.sleep(DELAY_SECONDS)

    if response.status_code == 429:
        wait = min(float(response.headers.get("Retry-After", 1) or 1), 10)
        log.warning("notion: rate limited, retrying in %.1fs", wait)
        time.sleep(wait)
        try:
            response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            log.warning("notion: %s %s failed on retry (%s)", method, url, exc)
            return None
        finally:
            time.sleep(DELAY_SECONDS)
    return response


def _ok(response):
    return response is not None and 200 <= response.status_code < 300


def _log_failure(response, context):
    if response is None:
        return
    body = (response.text or "")[:300]
    log.warning("notion: %s -> HTTP %s %s", context, response.status_code, body)


def fetch_schema(session):
    """Read the data source once so we map onto its real property names and types."""
    response = _request(session, "GET", f"{API_ROOT}/data_sources/{DATA_SOURCE_ID}")
    if not _ok(response):
        _log_failure(response, "fetching the Pipeline data source")
        return None
    try:
        properties = response.json().get("properties", {})
    except ValueError:
        log.warning("notion: data source response was not JSON")
        return None
    schema = {name: prop.get("type") for name, prop in properties.items()}
    log.info("notion: Pipeline schema has %d properties", len(schema))
    return schema


def _append_blocks(session, page_id, blocks):
    """Append leftover body blocks past Notion's per-request children limit."""
    for start in range(0, len(blocks), MAX_BLOCKS_PER_CALL):
        batch = blocks[start : start + MAX_BLOCKS_PER_CALL]
        response = _request(
            session,
            "PATCH",
            f"{API_ROOT}/blocks/{page_id}/children",
            json={"children": batch},
        )
        if not _ok(response):
            _log_failure(response, f"appending {len(batch)} description blocks")
            return False
    return True


def create_page(session, schema, role):
    """Create one Pipeline row. Returns True on success, never raises."""
    blocks = description_blocks(role.get("description") or "")
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
        "properties": build_properties(role, schema),
        "children": blocks[:MAX_BLOCKS_PER_CALL],
    }

    response = _request(session, "POST", f"{API_ROOT}/pages", json=payload)
    if not _ok(response):
        _log_failure(response, f"creating row for {role.get('title')!r}")
        return False

    leftover = blocks[MAX_BLOCKS_PER_CALL:]
    if leftover:
        page_id = response.json().get("id")
        if not page_id or not _append_blocks(session, page_id, leftover):
            log.warning("notion: %r was created with a truncated description", role.get("title"))
            return False
    return True


def push_roles(roles):
    """Create a Pipeline row per role.

    Returns the set of urls that reached Notion, or None when the push is disabled
    because NOTION_TOKEN is unset — callers use None to mean "not attempted".
    """
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        log.warning("notion: NOTION_TOKEN is not set, skipping the push")
        return None
    if not roles:
        return set()

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
    )

    schema = fetch_schema(session)
    if schema is None:
        log.warning("notion: could not read the Pipeline schema, skipping the push")
        return set()

    pushed = set()
    for role in roles:
        if create_page(session, schema, role):
            pushed.add(role["url"])
            log.info("notion: + [%s] %s", role["company"], role["title"])
    log.info("notion: pushed %d of %d roles", len(pushed), len(roles))
    return pushed
