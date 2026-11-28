"""Pi health for Discord (L31): a daily digest and an hourly problem check, both to #pi-health.

    python3 pi_health.py digest     # one summary message, no ping
    python3 pi_health.py check      # silent unless a problem STARTS (pings), persists 24 h (pings again) or ENDS (no ping)

Only numbers and fixed phrases are posted: uptime, load, temperature, disk and memory use, which of OUR services
and timers are healthy, ages of the newest backup and last good sync, SimpleFIN requests used. Never balances,
account or institution names, or amounts. Nothing is posted when notify.env is absent. Always exits 0: a missing
Discord must not turn a health check into a failing unit (the unit's journal says what happened).
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import notify
from sync_status import STALE_AFTER, ago, last_ok

DB = Path.home() / ".local/share/planner-accounts/accounts.sqlite3"
STATE = Path.home() / ".local/share/planner-accounts/health-state.json"
UNITS = ("accounts-dashboard.service", "accounts-sync.timer", "accounts-backup.timer")
OURS = re.compile(r"accounts-[a-z-]+\.(?:service|timer)")
DISK_LIMIT, MEM_LIMIT, TEMP_LIMIT = 80, 90, 80      # percent, percent, degrees C
REPEAT_AFTER = 24 * 3600


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return None


def unit_states():
    """{'accounts-dashboard.service': 'active', ...} via the user manager; 'unknown' when it cannot be asked."""
    states = {}
    for unit in UNITS:
        try:
            out = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=10)
            states[unit] = out.stdout.strip() or "unknown"
        except Exception:
            states[unit] = "unknown"
    return states


def failed_units():
    try:
        out = subprocess.run(["systemctl", "--user", "--failed", "--no-legend", "--plain"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    names = [line.split()[0] for line in out.splitlines() if line.split()]
    return sorted(n for n in names if OURS.fullmatch(n))      # our unit names only


def dashboard_up(url="http://127.0.0.1:8765/"):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def newest_backup(directory):
    """(age_seconds, size_bytes) of the newest *.gpg archive, or (None, None)."""
    try:
        files = [f for f in Path(directory).glob("accounts-*.sqlite3.gpg") if f.is_file()]
    except (OSError, TypeError):
        return None, None
    if not files:
        return None, None
    newest = max(files, key=lambda f: f.stat().st_mtime)
    return time.time() - newest.stat().st_mtime, newest.stat().st_size


def probe(db=DB, backup_dir=None, *, clock=time.time):
    """Everything the digest and the check need, as plain numbers and states."""
    facts = {"now": clock()}
    up = _read("/proc/uptime")
    facts["uptime"] = float(up.split()[0]) if up else None
    try:
        facts["load"] = os.getloadavg()[0]
    except OSError:
        facts["load"] = None
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    facts["temp"] = int(temp) / 1000 if temp and temp.strip().isdigit() else None
    usage = shutil.disk_usage("/")
    facts["disk_pct"], facts["disk_gb"] = round(100 * usage.used / usage.total), round(usage.total / 1e9)
    mem = _read("/proc/meminfo")
    total = re.search(r"MemTotal:\s+(\d+)", mem or "")
    avail = re.search(r"MemAvailable:\s+(\d+)", mem or "")
    facts["mem_pct"] = round(100 * (1 - int(avail[1]) / int(total[1]))) if total and avail else None
    facts["dashboard"] = dashboard_up()
    facts["units"] = unit_states()
    facts["failed"] = failed_units()
    facts["backup_age"], facts["backup_bytes"] = newest_backup(backup_dir or os.environ.get("ACCOUNTS_BACKUP_DIR", ""))
    last = last_ok(db)
    facts["sync_age"] = None if last is None else facts["now"] - last
    facts["requests"] = notify.requests_used(db)
    return facts


def problems(f):
    """{key: fixed phrase} for what is wrong right now. Keys are stable so state can track them."""
    found = {}
    if not f.get("dashboard"):
        found["dashboard"] = "Dashboard is not answering on the Pi"
    if f.get("disk_pct") is not None and f["disk_pct"] > DISK_LIMIT:
        found["disk"] = f"Disk is {f['disk_pct']}% full"
    if f.get("mem_pct") is not None and f["mem_pct"] > MEM_LIMIT:
        found["memory"] = f"Memory use is {f['mem_pct']}%"
    if f.get("temp") is not None and f["temp"] > TEMP_LIMIT:
        found["temp"] = f"CPU temperature is {f['temp']:.0f}°C"
    if f.get("sync_age") is None:
        found["sync"] = "No account has ever synced"
    elif f["sync_age"] > STALE_AFTER:
        found["sync"] = f"Last good sync was {ago(f['sync_age'])}"
    if f.get("backup_age") is None:
        found["backup"] = "No backup archive found"
    elif f["backup_age"] > STALE_AFTER:
        found["backup"] = f"Newest backup is {ago(f['backup_age'])}"
    if f.get("failed"):
        found["units"] = "Failed services: " + ", ".join(f["failed"])
    return found


def _uptime(seconds):
    days, rest = divmod(int(seconds), 86400)
    return f"{days}d {rest // 3600}h" if days else f"{rest // 3600}h {rest % 3600 // 60}m"


def digest_text(f):
    stamp = datetime.fromtimestamp(f["now"]).strftime("%a %-d %b %H:%M")
    dot = lambda ok: "🟢" if ok else "🔴"
    unit = lambda name: dot(f["units"].get(name) == "active")
    lines = [f"🩺 Pi health · {stamp}"]
    bits = []
    if f.get("uptime") is not None:
        bits.append(f"⏱️ up {_uptime(f['uptime'])}")
    if f.get("load") is not None:
        bits.append(f"load {f['load']:.2f}")
    if f.get("temp") is not None:
        bits.append(f"🌡️ {f['temp']:.0f}°C")
    if bits:
        lines.append("• " + " · ".join(bits))
    mem = f" · 🧠 memory {f['mem_pct']}%" if f.get("mem_pct") is not None else ""
    lines.append(f"• 💾 disk {f['disk_pct']}% of {f['disk_gb']} GB{mem}")
    lines.append(f"• {dot(f['dashboard'])} dashboard · {unit('accounts-sync.timer')} sync timer · "
                 f"{unit('accounts-backup.timer')} backup timer")
    backup = ("none found" if f.get("backup_age") is None
              else f"{ago(f['backup_age'])} ({max(1, round(f['backup_bytes'] / 1024))} KB)")
    sync = "never" if f.get("sync_age") is None else ago(f["sync_age"])
    used = f" · requests {f['requests']}/24" if f.get("requests") is not None else ""
    lines.append(f"• 🗄️ newest backup {backup} · 🔄 last sync {sync}{used}")
    found = problems(f)
    lines.append("• ⚠️ " + "; ".join(found.values()) if found else "• ✅ nothing needs attention")
    return "\n".join(lines)


def _load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        return {k: float(v) for k, v in data.get("open", {}).items()}, {k: float(v) for k, v in data.get("alerted", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}, {}


def _save_state(path, opened, alerted):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".health-state-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"open": opened, "alerted": alerted}, stream)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def check(config, f, state=STATE):
    """Compare now with the saved state and post only what changed. Returns the list of messages posted."""
    now = f["now"]
    opened, alerted = _load_state(state)
    found = problems(f)
    posted = []
    started = {k: v for k, v in found.items() if k not in opened}
    repeating = {k: v for k, v in found.items() if k in opened and now - alerted.get(k, opened[k]) >= REPEAT_AFTER}
    if started or repeating:
        lines = [f"🚨 {text}" for text in started.values()] + [f"🔁 Still: {text}" for text in repeating.values()]
        text = "\n".join(lines)
        if notify.post(config, "health", text, ping=True):      # remember it only once Discord took it: else retry next hour
            posted.append(text)
            for k in (*started, *repeating):
                alerted[k] = now
            for k in started:
                opened[k] = now
    ended = [k for k in opened if k not in found]
    if ended:
        text = "✅ Recovered: " + ", ".join(sorted(ended))
        if notify.post(config, "health", text):
            posted.append(text)
            for k in ended:
                opened.pop(k), alerted.pop(k, None)
    _save_state(state, opened, alerted)
    return posted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("digest", "check"))
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args(argv)
    config = notify.load()
    if not config:
        print("pi_health: notifications are off (no usable notify.env); nothing sent.")
        return 0
    facts = probe(args.db)
    if args.action == "digest":
        sent = notify.post(config, "health", digest_text(facts))
        print("pi_health: digest sent." if sent else "pi_health: digest not sent.")
    else:
        posted = check(config, facts)
        print(f"pi_health: {len(posted)} message(s) sent; {len(problems(facts))} problem(s) open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
