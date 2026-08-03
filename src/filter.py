"""Filtering rules applied to normalized rows, driven by config/filters.json.

A role is kept when all of these hold, case-insensitively:

  - the title contains a pm_term
  - the title contains a level term
  - remote is true, OR location + title contains a locations_allow term
  - the title contains none of the negative_keywords
  - the title contains none of HARD_EXCLUDES

Term matching is plain substring for the positive rules (so "sr " and "sr." work as
written), and word-boundary for the exclusions — otherwise "intern" would throw out
every "International" role and "apm" would fire inside unrelated words.
"""

import re

# Excluded regardless of what config/filters.json says.
HARD_EXCLUDES = ("associate", "junior", "intern", "apm")


def _contains_any(text, terms):
    """Case-insensitive substring match against any term."""
    lowered = (text or "").lower()
    return any(term.lower() in lowered for term in terms)


def _contains_any_word(text, terms):
    """Case-insensitive whole-word match against any term."""
    lowered = (text or "").lower()
    return any(re.search(rf"\b{re.escape(term.lower())}\b", lowered) for term in terms)


def is_pm_role(title, filters):
    return _contains_any(title, filters.get("pm_terms", []))


def is_senior_level(title, filters):
    return _contains_any(title, filters.get("levels", []))


def location_allowed(row, filters):
    if row.get("remote"):
        return True
    haystack = f"{row.get('location') or ''} {row.get('title') or ''}"
    return _contains_any(haystack, filters.get("locations_allow", []))


def has_negative_keyword(title, filters):
    return _contains_any_word(title, filters.get("negative_keywords", [])) or _contains_any_word(
        title, HARD_EXCLUDES
    )


def keep(row, filters):
    """True if the row survives every rule above.

    The level rule is optional. Companies like Cohere post senior work under a plain
    "Product Manager" title, so requiring a level term drops roles worth seeing. With
    require_level false the hard exclusions still block associate/junior/intern/apm,
    and anything under-levelled that slips through is caught by the scorer's HF5.
    """
    title = row.get("title") or ""
    if not is_pm_role(title, filters):
        return False
    if filters.get("require_level", True) and not is_senior_level(title, filters):
        return False
    return location_allowed(row, filters) and not has_negative_keyword(title, filters)


def apply_filters(rows, filters):
    return [row for row in rows if keep(row, filters)]
