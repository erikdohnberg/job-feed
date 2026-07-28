"""Fetchers for each ATS. One function per provider, each returning normalized rows.

Normalized row shape (a plain dict):

    {
        "source":    "greenhouse" | "lever" | "ashby",
        "company":   str,   # board token/slug, or the company name the API gives us
        "title":     str,
        "location":  str,
        "url":       str,   # canonical posting URL, used as the dedup key
        "posted_at": str,   # ISO 8601 if the API provides it, else ""
        "description": str, # plain-ish text, may be HTML from the API
    }

Not implemented yet — scaffold only.
"""


def fetch_greenhouse(token):
    """GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"""
    raise NotImplementedError


def fetch_lever(slug):
    """GET https://api.lever.co/v0/postings/{slug}?mode=json"""
    raise NotImplementedError


def fetch_ashby(slug):
    """GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"""
    raise NotImplementedError


def fetch_all(tokens):
    """Run every fetcher over config/tokens.json and return one flat list of rows."""
    raise NotImplementedError
