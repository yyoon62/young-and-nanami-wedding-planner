"""Write a static, fully fictional Accounts page for screenshots: python3 accounts_demo_page.py [out_dir].

Renders the real dashboard against a temporary fictional database (never the real one) with well-known institution
names, then inlines the CSS, JS and logos into a single index.html. The only network use is fetching each public
institution favicon once; no account data is sent. Open it from disk; the edit drawers and menus work, saving does nothing.
"""
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from threading import Thread
import base64
from urllib.request import urlopen

from account_terms import save_terms
from server import init_db, serve
from tx_demo import TODAY, frozen_today

ROOT = Path(__file__).resolve().parent

# Real, public institutions (so their logos show) with entirely made-up names of products, owners and balances.
ACCOUNTS = (
    ("Chase", "Total Checking", "Checking", "asset", "6420.55", "Joint", None),
    ("Bank of America", "Young Checking", "Checking", "asset", "2180.10", "Young", None),
    ("Capital One", "Nanami Checking", "Checking", "asset", "1895.72", "Nanami", None),
    ("Marcus by Goldman Sachs", "High-Yield Savings", "Savings", "asset", "24500.00", "Joint",
     dict(rate="4.250", rate_kind="variable")),
    ("Ally Bank", "Wedding Fund", "Savings", "asset", "11200.00", "Joint", dict(rate="4.250", rate_kind="variable")),
    ("Fidelity", "Young 401(k)", "Retirement", "asset", "86400.00", "Young", None),
    ("Vanguard", "Nanami Roth IRA", "Retirement", "asset", "52300.00", "Nanami", None),
    ("Charles Schwab", "Index Fund", "Investments", "asset", "31750.00", "Joint", None),
    ("American Express", "Rewards Card", "Credit card", "debt", "1240.37", "Young", dict(rate="24.990", paid_in_full=1)),
    ("Chase", "Travel Card", "Credit card", "debt", "685.20", "Nanami", dict(rate="21.490", paid_in_full=1)),
    ("Nelnet", "Student Loan A", "Student loan", "debt", "14800.00", "Young",
     dict(rate="5.500", min_payment="160.00", planned_payment="200.00")),
    ("Aidvantage", "Student Loan B", "Student loan", "debt", "6350.00", "Young",
     dict(rate="6.800", min_payment="85.00", planned_payment="100.00")),
)


DEMO_BANNER = ('<p class="mode-banner demo" role="note" style="text-align:center;font-size:.95rem">'
               '<strong style="letter-spacing:.08em">DEMO</strong> · fictional accounts and balances · '
               'not real data · for screenshots only</p>')
DEMO_RIBBON = ('<div aria-hidden="true" style="position:fixed;top:22px;right:-46px;z-index:50;width:190px;'
               'transform:rotate(45deg);background:#d9a21b;color:#2b2320;text-align:center;font:700 13px/26px '
               'system-ui,sans-serif;letter-spacing:.2em;box-shadow:0 1px 4px rgba(0,0,0,.25)">DEMO</div>')


def seed(path):
    init_db(path)
    with closing(sqlite3.connect(path)) as db, db:
        for institution, nickname, kind, side, balance, owner, terms in ACCOUNTS:
            account = db.execute(
                """INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner,update_method)
                   VALUES (?,?,?,?,'USD',?,?,?,'Fictional')""",
                (institution, nickname, kind, side, balance, (TODAY - timedelta(days=1)).isoformat(), owner)).lastrowid
            if terms:
                save_terms(db, account, terms)
            # Gentle month-end history so the net worth chart has a shape.
            for back in range(1, 7):
                asof = (date(TODAY.year, TODAY.month, 1) - timedelta(days=30 * (back - 1) + 1)).isoformat()
                drift = 1 - 0.012 * back * (1 if side == "asset" else -0.5)
                db.execute("INSERT OR IGNORE INTO balance_history (account_id,asof,balance,currency,side) "
                           "VALUES (?,?,?,?,?)", (account, asof, f"{float(balance) * drift:.2f}", "USD", side))


def render(path, logo_dir):
    server = serve(path, 0, logo_dir=logo_dir)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        with frozen_today():
            html = urlopen(base + "/").read().decode()
        for domain in sorted(set(re.findall(r'src="/logo/([^"]+)"', html))):
            with urlopen(f"{base}/logo/{domain}") as response:
                uri = f"data:{response.headers.get_content_type()};base64,{base64.b64encode(response.read()).decode()}"
            html = html.replace(f'src="/logo/{domain}"', f'src="{uri}"')
        css = urlopen(base + "/style.css").read().decode()
        js = urlopen(base + "/dashboard.js").read().decode()
    finally:
        server.shutdown()
        server.server_close()
    html = html.replace('<link rel="stylesheet" href="/style.css">', f"<style>{css}</style>")
    html = re.sub(r'<script defer src="/dashboard.js"></script>', "", html)
    html = html.replace("</main></html>", f"</main><script>{js}</script>"
                        "<script>document.addEventListener('submit',e=>e.preventDefault());"
                        "document.addEventListener('click',e=>{const a=e.target.closest('a');"
                        "if(a&&a.getAttribute('href')&&a.getAttribute('href')[0]==='/')e.preventDefault();});"
                        "</script></html>")
    html = re.sub(r'name="token" value="[^"]*"', 'name="token" value="demo"', html)
    html = re.sub(r'<p class="mode-banner[^>]*>.*?</p>', DEMO_BANNER, html, count=1, flags=re.S)
    html = html.replace("<title>", "<title>DEMO · ", 1).replace("<main>", DEMO_RIBBON + "<main>", 1)
    return html


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "demo_pages")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "demo.sqlite3"
        seed(db_path)
        (out / "index.html").write_text(render(db_path, Path(tmp) / "logos"))
    print(f"Wrote {out / 'index.html'}")
