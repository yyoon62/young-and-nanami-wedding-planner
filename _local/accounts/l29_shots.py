"""L29 before/after screenshots: desktop 1366 + phone 390, fictional temp DB only, loopback-only network.

    python l29_shots.py before OUT_DIR   # baseline 043c5a9: Accounts + the old /projections page
    python l29_shots.py after  OUT_DIR   # L29: History tab + ?chart=projection, same fixture, same names
Run from _local/accounts with the browser venv. Writes <mode>-<name>-<desktop|phone>.png and prints page metrics.
"""
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright

from browser_tx import launch
from l29_fixture import running, seed
from tx_fixture import block_network

# name -> (path before, path after, database)
SHOTS = {
    "accounts-history": ("/", "/", "plain"),
    "accounts-history-nanami-eur": ("/?view=Nanami", "/?view=Nanami&currency=EUR", "plain"),
    "projection-household-usd": ("/projections", "/?chart=projection", "plain"),
    "projection-nanami-eur": ("/projections?view=Nanami&currency=EUR", "/?view=Nanami&currency=EUR&chart=projection", "plain"),
    "projection-backdated": (None, "/?chart=projection", "backdated"),
    "projection-error": ("/projections", "/?chart=projection", "plain"),
}


def main(mode, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp()).resolve()
    dbs = {"plain": tmp / "plain.sqlite3", "backdated": tmp / "backdated.sqlite3"}
    seed(dbs["plain"])
    seed(dbs["backdated"], backdated=True)
    with block_network(allow_loopback=True), sync_playwright() as pw:
        chrome = launch(pw)
        for which, db in dbs.items():
            with running(db) as srv:
                origin = f"http://127.0.0.1:{srv.server_port}"
                for width, label in ((1366, "desktop"), (390, "phone")):
                    page = chrome.new_page(viewport={"width": width, "height": 900 if width == 1366 else 844})
                    for shot, (before, after, needs) in SHOTS.items():
                        target = before if mode == "before" else after
                        if needs != which or target is None:
                            continue
                        page.goto(origin + target)
                        if shot == "projection-error":      # an invalid horizon, saved through the page's own form
                            page.fill("input[name=years]", "-1")
                            page.get_by_role("button", name="Save assumptions").click()
                            page.wait_for_load_state()
                        metrics = page.evaluate("""() => { const c = document.querySelector('.net-chart');
                            const b = c ? c.getBoundingClientRect() : null;
                            return [document.documentElement.scrollHeight, document.documentElement.scrollWidth,
                                    b ? Math.round(b.bottom + scrollY) : null]; }""")
                        page.screenshot(path=str(out / f"{mode}-{shot}-{label}.png"), full_page=True)
                        if shot == "projection-error":      # the form itself, with the refused values and the message
                            page.locator("section[aria-labelledby=projection-assumptions]").screenshot(path=str(out / f"{mode}-{shot}-form-{label}.png"))
                        page.evaluate("window.scrollTo(0, 0)")
                        page.screenshot(path=str(out / f"{mode}-{shot}-{label}-first-screen.png"))
                        print(f"{mode:6} {shot:28} {label:7} scrollHeight={metrics[0]:5} scrollWidth={metrics[1]:4} "
                              f"chartBottom={metrics[2]}")
                    page.close()
        chrome.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "l29-shots")
