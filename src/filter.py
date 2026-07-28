"""Filtering rules applied to normalized rows, driven by config/filters.json.

A row is kept when all of these hold:
  - the title looks like a product management role   (pm_terms)
  - the title carries a senior-or-above level marker (levels)
  - the title hits none of the negative_keywords
  - the location matches something in locations_allow

Not implemented yet — scaffold only.
"""


def is_pm_role(title, filters):
    raise NotImplementedError


def is_senior_level(title, filters):
    raise NotImplementedError


def has_negative_keyword(title, filters):
    raise NotImplementedError


def location_allowed(location, filters):
    raise NotImplementedError


def keep(row, filters):
    """True if the row survives every rule above."""
    raise NotImplementedError


def apply_filters(rows, filters):
    raise NotImplementedError
