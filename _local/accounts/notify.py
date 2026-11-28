"""Discord notifications from the Pi (L31): one webhook per channel, best effort, counts and statuses only.

    ~/.config/planner-accounts/notify.env   (0600, in a 0700 directory, outside Git; absent = notifications off)
        DISCORD_SYNC_WEBHOOK=https://discord.com/api/webhooks/<id>/<token>     # #accounts-sync: every sync
        DISCORD_HEALTH_WEBHOOK=https://discord.com/api/webhooks/<id>/<token>   # #pi-health: digest + alerts
        DISCORD_PING_USER=<numeric user id>                                    # who an alert pings

Rules this module keeps (tests pin them):
* A webhook can only post to its one channel. The groot bot token is never used here and never on the Pi.
* Messages are built ONLY from the templates below and from integers or fixed phrases. Provider warnings, account
  or institution names, balances, amounts and e-mail addresses are never passed in, so they cannot leak.
* Never raises, never delays a sync by more than the timeout, and a send failure prints only the error's type
  (a URL or token can appear in an exception's text).
* Library code does nothing unless handed a `Config`; only the `__main__` entry points load the file, so tests and
  the Mac never post by accident.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import urllib.request

from hosted_auth import check_private, external, private

CONFIG = Path.home() / ".config/planner-accounts/notify.env"
KEYS = ("DISCORD_SYNC_WEBHOOK", "DISCORD_HEALTH_WEBHOOK", "DISCORD_PING_USER")
URL = re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/[0-9]{15,25}/[A-Za-z0-9_-]{20,120}")
TIMEOUT = 5
LIMIT = 1800                      # Discord allows 2000 characters per message


@dataclass(frozen=True)
class Config:
    sync: str = ""
    health: str = ""
    user: str = ""

    def __repr__(self):           # never show a webhook token in a log or a traceback
        return f"Config(sync={'set' if self.sync else 'off'}, health={'set' if self.health else 'off'}, user={'set' if self.user else 'off'})"


def load(path=None):
    """The configuration, or None when the file is absent or unusable (one stderr line says why; no secrets)."""
    path = Path(path) if path else CONFIG
    if not os.path.lexists(path):
        return None
    try:
        path = external(path)
        private(path.parent, directory=True)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            check_private(os.fstat(stream.fileno()))
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("file too large")
        env = {}
        for line in raw.decode("ascii").splitlines():
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if key not in KEYS or not sep or key in env:
                raise ValueError("unexpected line")
            env[key] = value
        sync, health, user = (env.get(k, "") for k in KEYS)
        for url in (sync, health):
            if url and not URL.fullmatch(url):
                raise ValueError("not a Discord webhook URL")
        if user and not re.fullmatch(r"[0-9]{15,25}", user):
            raise ValueError("ping user must be a numeric id")
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"notify: notifications off ({type(exc).__name__}: check the file's owner, mode 0600 and lines)", file=sys.stderr)
        return None
    return Config(sync, health, user) if (sync or health) else None


def _transport(url, body, timeout):
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "DiscordBot (accounts-pi-notify, 1)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


TRANSPORT = _transport            # tests replace this; nothing else touches the network


def post(config, channel, text, *, ping=False):
    """Send one message. channel is "sync" or "health". True only if Discord accepted it; never raises."""
    try:
        url = config.sync if channel == "sync" else config.health if channel == "health" else ""
        if not url:
            return False
        text = str(text)[:LIMIT]
        if ping and config.user:
            payload = {"content": f"<@{config.user}> {text}", "allowed_mentions": {"users": [config.user]}}
        else:
            payload = {"content": text, "allowed_mentions": {"parse": []}}
        return 200 <= TRANSPORT(url, json.dumps(payload).encode(), TIMEOUT) < 300
    except Exception as exc:
        print(f"notify: not sent ({type(exc).__name__})", file=sys.stderr)
        return False


_threads = []


def background(fn, *args, **kwargs):
    """Run `fn` without holding up a web request. Daemon thread; `wait()` is for tests and process exit."""
    thread = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    _threads.append(thread)
    return thread


def wait(timeout=TIMEOUT + 2):
    for thread in list(_threads):
        thread.join(timeout)
    _threads.clear()


# ---- whitelisted message templates -------------------------------------------------------------------------

_COUNTS = re.compile(r"(\d+) (added|updated|unchanged|held)")
_ORDER = ("added", "updated", "unchanged", "held")


def counts(notices):
    """{'added': n, ...} from the first line of Batch.sync()'s notices ('Synced: 2 updated, 20 unchanged.')."""
    first = notices[0] if notices else ""
    return {word: int(n) for n, word in _COUNTS.findall(first)}


def requests_used(db_path):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT count(*) FROM fetch_attempts WHERE at > ?", (time.time() - 86400,)).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return None


def sync_ok_text(trigger, notices, used=None):
    c = counts(notices)
    parts = [f"{c[w]} {'new' if w == 'added' else w}" for w in _ORDER if c.get(w)]
    held = c.get("held", 0)
    text = f"{'⚠️' if held else '✅'} Sync ({trigger}): " + (" · ".join(parts) if parts else "done")
    if used is not None:
        text += f" · requests {used}/24"
    if held:
        text += " · held = an account was not refreshed"
    return text


def reason_for(exc):
    """A fixed phrase for an exception; unexpected errors give only their type."""
    from bridge import Problem
    from hosted_guard import Refused
    if isinstance(exc, Refused):
        return "transaction data found on the host (start-up check)"
    if isinstance(exc, Problem):
        return ("daily request limit reached (24 per 24 h)" if str(exc).startswith("Daily guard")
                else "SimpleFIN problem (see the Pi log)")
    if isinstance(exc, sqlite3.Error):
        return "database problem"
    return f"unexpected error ({type(exc).__name__})"


NOTHING_REFRESHED = "no account was refreshed"


def sync_failed_text(trigger, reason):
    return f"❌ Sync failed ({trigger}): {reason}"


def report_sync(config, trigger, ok, *, notices=(), reason="", db_path=None):
    """Post one sync result to #accounts-sync; a failure also pings #pi-health. Never raises."""
    if not config:
        return
    try:
        used = requests_used(db_path) if db_path else None
        if ok:
            post(config, "sync", sync_ok_text(trigger, notices, used))
        else:
            post(config, "sync", sync_failed_text(trigger, reason))
            post(config, "health", f"🚨 {sync_failed_text(trigger, reason)}", ping=True)
    except Exception as exc:
        print(f"notify: not sent ({type(exc).__name__})", file=sys.stderr)
