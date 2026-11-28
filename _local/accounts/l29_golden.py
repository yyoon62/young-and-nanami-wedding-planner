"""L29 golden numbers: what the Projection panel shows (numbers, not layout) for a fixed fictional DB.

    python l29_golden.py write l29_golden.json     # regenerate from whatever page serves the projection NOW
Generated once at baseline 043c5a9 from GET /projections and committed; later commits only compare against it.
"""
from collections import Counter
from html import unescape
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlencode

CASES = (("Household", "USD"), ("Young", "USD"), ("Nanami", "USD"), ("Nanami", "EUR"))
MONEY = re.compile(r"[A-Z]{3} -?[\d,]+(?:\.\d+)?|-?\d+(?:\.\d+)?%")


def text(fragment):
    return unescape(re.sub(r"<[^>]+>", " ", fragment))


def tokens(fragment):
    return MONEY.findall(text(fragment))


def section(html, label):
    found = re.search(rf'<section[^>]*aria-labelledby="{label}".*?</section>', html, re.S)
    return found[0] if found else ""


def regions(html):
    """Numbers per region, independent of where the panel sits on the page or how its prose is arranged."""
    annual = re.search(r"<details><summary>Show annual breakdown</summary>(.*?)</details>", html, re.S)
    budget = re.search(r"Monthly take-home income:.*?</p>", html, re.S)
    form = re.search(r'<form class="edit-form projection-form".*?</form>', html, re.S)
    notes = re.findall(r'<p class="notice" role="note">.*?</p>', html, re.S)
    return {
        "cards": [[text(p).strip() for p in found] for found in
                  re.findall(r'<div class="card"><small>(.*?)</small><strong>(.*?)</strong><div>(.*?)</div></div>', html, re.S)
                  if "Start:" in found[1]],
        "annual": tokens(annual[1]) if annual else [],
        "budget": tokens(budget[0]) if budget else [],
        "notes": sorted(t for n in notes for t in tokens(n)),
        "suggest": tokens(section(html, "projection-suggestion")),
        "debt": tokens(section(html, "debt-heading")),
        "asset": tokens(section(html, "asset-heading")),
        "form": [kv for kv in re.findall(r'name="(\w+)"[^>]*? value="([^"]*)"', form[0]) if kv[0] != "token"] if form else [],
    }


def collect(srv, fetch, url):
    out = {}
    for view, currency in CASES:
        status, _, body = fetch(srv, url(view, currency))
        assert status == 200, (view, currency, status)
        out[f"{view}-{currency}"] = regions(body)
    return out


def baseline_url(view, currency):
    return "/projections?" + urlencode({"view": view, "currency": currency})


if __name__ == "__main__":
    from l29_fixture import fetch, running, seed
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp).resolve() / "fictional.sqlite3"
        seed(db)
        with running(db) as srv:
            data = collect(srv, fetch, baseline_url)
    Path(sys.argv[2]).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print({k: {r: len(v) for r, v in d.items()} for k, d in data.items()})
