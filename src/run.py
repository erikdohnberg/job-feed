"""Entry point: fetch -> filter -> dedup -> write candidates.json.

Run with:  python src/run.py

Reads  config/tokens.json, config/filters.json, state/seen.json
Writes candidates.json, state/seen.json

Not implemented yet — scaffold only.
"""


def load_json(path):
    raise NotImplementedError


def write_json(path, data):
    raise NotImplementedError


def dedup(rows, seen_urls):
    """Drop rows whose url is already in seen_urls."""
    raise NotImplementedError


def main():
    raise NotImplementedError


if __name__ == "__main__":
    main()
