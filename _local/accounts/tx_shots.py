"""Desktop and phone screenshots of /transactions and /projections from the fictional demo DB (L22).

    python tx_shots.py OUT_DIR

Fictional data, frozen clock, mocked classifier, loopback only. Never reads a real database.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest import mock

from playwright.sync_api import sync_playwright

import tx_classifier as C
from browser_tx import KEY, launch, running
from tx_demo import seed_fictional
from tx_fixture import block_network


def transport(headers, body, timeout):
    items = json.loads(json.loads(body)["messages"][0]["content"])["merchants"]
    pick = {"metro transit pass": ("Transport", "🚇"), "boulangerie fictive": ("Dining", "🥐")}
    results = [dict(id=m["id"], category=pick.get(m["text"], ("Other", "🧾"))[0],
                    emoji=pick.get(m["text"], ("Other", "🧾"))[1], confidence=0.9) for m in items]
    return 200, json.dumps({"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "name": C.TOOL, "input": {"results": results}}]}).encode()


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp()
    path = Path(tmp).resolve() / "fictional.sqlite3"
    seed_fictional(path, categorize=True)
    with closing(sqlite3.connect(path)) as db, db:
        C.set_setting(db, "enabled", "1")
    with block_network(allow_loopback=True), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY}), \
            mock.patch.object(C, "TRANSPORT", transport), running(path) as origin, sync_playwright() as pw:
        C.run_classifier(str(path), hosted=False)
        chrome = launch(pw)
        for name, width, height in (("desktop", 1280, 900), ("phone", 390, 844)):
            page = chrome.new_page(viewport={"width": width, "height": height})
            page.goto(origin + "/transactions?month=2026-11")
            page.screenshot(path=str(out / f"transactions-{name}.png"), full_page=True)
            page.locator("details.classifier summary").click()      # collapsed by default; open it for this shot
            page.locator("details.classifier").screenshot(path=str(out / f"transactions-classifier-{name}.png"))
            page.locator("details.classifier summary").click()
            row = page.locator("tr.acct-row", has_text="Metro Transit")
            row.locator("summary", has_text="Edit").click()
            row.scroll_into_view_if_needed()
            page.screenshot(path=str(out / f"transactions-drawer-{name}.png"))
            page.goto(origin + "/projections")
            page.get_by_text("Advanced: targets, rates, display units").click()
            page.screenshot(path=str(out / f"projections-{name}.png"), full_page=True)
            page.close()
        chrome.close()
    print(*sorted(p.name for p in out.iterdir()), sep="\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "l22-shots")
