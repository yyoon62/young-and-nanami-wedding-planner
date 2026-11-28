"""When did the balances last sync successfully? (L30a)

Derived from `source_links` (the sync already stamps `checked` on every account it refreshed), so there is no new
table and nothing extra to protect or back up. Only accounts the provider really refreshed count (Added /
Updated / Unchanged); "Missing or provider error; retained" and the other held statuses do not, so a bank that
stopped answering shows as stale instead of looking fresh.
"""
from contextlib import closing
from datetime import datetime, timezone
from html import escape
import sqlite3
import time

STALE_AFTER = 36 * 3600          # one daily run plus a generous retry window
REFRESHED = ("Added", "Updated", "Unchanged")
_FORMAT = "%Y-%m-%d %H:%M UTC"   # what bulk.py writes with strftime('%Y-%m-%d %H:%M UTC','now')


def last_ok(path):
    """Epoch seconds of the newest refreshed account, or None if it never synced (or the table is missing)."""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT max(checked) FROM source_links WHERE status IN (?,?,?)", REFRESHED).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(row[0], _FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def note(path, now=None):
    """Small HTML line for the Accounts page; static text plus a computed age, nothing user-supplied."""
    now = time.time() if now is None else now
    last = last_ok(path)
    if last is None:
        return '<span class="sync-note" title="No account has been refreshed from SimpleFIN yet.">🕒 Not synced yet</span>'
    age = now - last
    exact = escape(datetime.fromtimestamp(last, timezone.utc).strftime(_FORMAT))
    if age > STALE_AFTER:
        return (f'<span class="sync-note sync-stale" role="status" title="Last refresh: {exact}">'
                f'⚠️ Last synced {ago(age)}: balances may be out of date</span>')
    return f'<span class="sync-note" title="Last refresh: {exact}">🕒 Synced {ago(age)}</span>'
