"""Desktop (1366) and phone (390) screenshots of the L23 terms drawer and Projections debts (fictional DB only).

    python terms_shots.py OUT_DIR
"""
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright

from browser_tx import launch, running
from terms_demo import seed_terms_demo
from tx_fixture import block_network


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp()).resolve()
    partial, complete = tmp / "partial.sqlite3", tmp / "complete.sqlite3"
    seed_terms_demo(partial)
    seed_terms_demo(complete, complete=True)
    with block_network(allow_loopback=True), sync_playwright() as pw:
        chrome = launch(pw)
        for name, width in (("desktop", 1366), ("phone", 390)):
            with running(partial) as origin:
                page = chrome.new_page(viewport={"width": width, "height": 900})
                page.goto(origin + "/")
                page.locator("tr.acct-row", has_text="Maple Student Loan B").get_by_role("link", name="Edit").click()
                drawer = page.locator("tr.edit-drawer:not([hidden])")
                drawer.scroll_into_view_if_needed()
                page.screenshot(path=str(out / f"accounts-terms-drawer-{name}.png"), full_page=True)
                page.goto(origin + "/projections")
                page.screenshot(path=str(out / f"projections-debts-rate-missing-{name}.png"), full_page=True)
                page.close()
            with running(complete) as origin:
                page = chrome.new_page(viewport={"width": width, "height": 900})
                page.goto(origin + "/projections")
                page.locator("summary", has_text="Schedule: Cedar Rewards Card").click()
                page.screenshot(path=str(out / f"projections-debts-{name}.png"), full_page=True)
                page.locator("section[aria-labelledby=debt-heading]").screenshot(
                    path=str(out / f"projections-debts-section-{name}.png"))
                page.locator("section[aria-labelledby=asset-heading]").screenshot(
                    path=str(out / f"projections-cash-apy-section-{name}.png"))
                page.close()
        chrome.close()
    print(*sorted(p.name for p in out.iterdir()), sep="\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "l23-shots")
