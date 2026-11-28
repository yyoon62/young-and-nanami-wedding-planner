"""L31: Discord notifications and Pi health. Fictional data; a fake transport stands in for Discord."""
from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import daily_sync
import notify
import pi_health
from bridge import Problem
from hosted_guard import Refused
from server import serve
from test_daily_sync import Base

SYNC = "https://discord.com/api/webhooks/111111111111111111/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HEALTH = "https://discord.com/api/webhooks/333333333333333333/BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
USER = "222222222222222222"
CONFIG = notify.Config(SYNC, HEALTH, USER)
DEPLOY = Path(__file__).resolve().parent / "deploy"
SECRETISH = re.compile(r"AAAAAAAA|BBBBBBBB|\$\s?\d|USD|EUR|@[a-z0-9.-]+\.[a-z]{2,}|FICTIONAL BANK|Birch|Cedar|Maple|nickname", re.I)


class Sink:
    """Records what would be sent to Discord, or raises like a failing network."""
    def __init__(self, status=204, error=None):
        self.sent, self.status, self.error = [], status, error

    def __call__(self, url, body, timeout):
        if self.error:
            raise self.error
        self.sent.append((url, json.loads(body)))
        return self.status

    def texts(self, url=None):
        return [m["content"] for u, m in self.sent if url in (None, u)]


class Case(unittest.TestCase):
    def setUp(self):
        self.sink = Sink()
        patcher = patch.object(notify, "TRANSPORT", self.sink)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()


class Config(Case):
    def write(self, text, mode=0o600, dirmode=0o700):
        folder = self.dir / "cfg"
        folder.mkdir(exist_ok=True)
        os.chmod(folder, dirmode)
        path = folder / "notify.env"
        path.write_text(text)
        os.chmod(path, mode)
        return path

    def load(self, path):
        err = StringIO()
        with redirect_stderr(err):
            return notify.load(path), err.getvalue()

    def test_absent_file_means_off_and_silent(self):
        cfg, err = self.load(self.dir / "nothing.env")
        self.assertIsNone(cfg)
        self.assertEqual(err, "")

    def test_valid_file(self):
        cfg, err = self.load(self.write(f"DISCORD_SYNC_WEBHOOK={SYNC}\nDISCORD_HEALTH_WEBHOOK={HEALTH}\nDISCORD_PING_USER={USER}\n"))
        self.assertEqual(cfg, CONFIG)
        self.assertEqual(err, "")

    def test_unusable_files_turn_it_off_with_one_line_and_no_secret(self):
        good = f"DISCORD_SYNC_WEBHOOK={SYNC}\n"
        for label, path in {
            "world-readable": self.write(good, mode=0o644),
            "open directory": self.write(good, dirmode=0o755),
            "http url": self.write("DISCORD_SYNC_WEBHOOK=http://discord.com/api/webhooks/111111111111111111/AAAAAAAAAAAAAAAAAAAAAAAA\n"),
            "other host": self.write("DISCORD_SYNC_WEBHOOK=https://evil.example/api/webhooks/111111111111111111/AAAAAAAAAAAAAAAAAAAAAAAA\n"),
            "userinfo": self.write("DISCORD_SYNC_WEBHOOK=https://discord.com@evil.example/api/webhooks/111111111111111111/AAAAAAAAAAAAAAAAAAAAAAAA\n"),
            "extra path": self.write(f"DISCORD_SYNC_WEBHOOK={SYNC}/../x\n"),
            "unknown key": self.write(good + "OTHER=1\n"),
            "bad user": self.write(good + "DISCORD_PING_USER=me\n"),
            "duplicate": self.write(good + good),
        }.items():
            with self.subTest(label):
                cfg, err = self.load(path)
                self.assertIsNone(cfg)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("AAAAAAAA", err)

    def test_repr_never_shows_a_token(self):
        self.assertNotIn("AAAAAAAA", repr(CONFIG))
        self.assertNotIn("AAAAAAAA", str(CONFIG))


class Post(Case):
    def test_plain_message_cannot_ping_anyone(self):
        self.assertTrue(notify.post(CONFIG, "sync", "✅ hello @everyone <@999>"))
        url, body = self.sink.sent[0]
        self.assertEqual(url, SYNC)
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_alert_pings_only_the_configured_user(self):
        notify.post(CONFIG, "health", "🚨 x", ping=True)
        url, body = self.sink.sent[0]
        self.assertEqual(url, HEALTH)
        self.assertTrue(body["content"].startswith(f"<@{USER}> "))
        self.assertEqual(body["allowed_mentions"], {"users": [USER]})

    def test_long_text_is_cut_and_unknown_or_unset_channels_send_nothing(self):
        notify.post(CONFIG, "sync", "x" * 5000)
        self.assertLessEqual(len(self.sink.sent[0][1]["content"]), notify.LIMIT)
        self.assertFalse(notify.post(CONFIG, "nowhere", "x"))
        self.assertFalse(notify.post(notify.Config(sync=SYNC), "health", "x"))
        self.assertEqual(len(self.sink.sent), 1)

    def test_failures_never_raise_and_print_only_the_type(self):
        for error in (TimeoutError("GET https://discord.com/api/webhooks/1/SECRETTOKEN timed out"),
                      HTTPError(SYNC, 429, "rate limited SECRETTOKEN", {}, None), OSError("down")):
            self.sink.error = error
            err = StringIO()
            with redirect_stderr(err):
                self.assertFalse(notify.post(CONFIG, "sync", "x"))
            self.assertNotIn("SECRETTOKEN", err.getvalue())
            self.assertNotIn("https://", err.getvalue())
            self.assertIn(type(error).__name__, err.getvalue())
        self.sink.error = None
        self.sink.status = 500
        self.assertFalse(notify.post(CONFIG, "sync", "x"))


class Templates(Case):
    def test_sync_text(self):
        self.assertEqual(notify.sync_ok_text("scheduled", ["Synced: 22 unchanged."], 2), "✅ Sync (scheduled): 22 unchanged · requests 2/24")
        self.assertEqual(notify.sync_ok_text("button", ["Synced: 1 added, 2 updated, 20 unchanged."]),
                         "✅ Sync (button): 1 new · 2 updated · 20 unchanged")
        held = notify.sync_ok_text("scheduled", ["Synced: 20 unchanged, 2 held."], 3)
        self.assertTrue(held.startswith("⚠️"))
        self.assertIn("2 held", held)
        self.assertEqual(notify.sync_ok_text("button", []), "✅ Sync (button): done")

    def test_provider_warnings_and_names_can_never_reach_a_message(self):
        notices = ["Synced: 3 unchanged.", "FICTIONAL BANK needs reauthorization for jane@example.com balance $1,234.56 (Birch)"]
        text = notify.sync_ok_text("scheduled", notices, 1)
        self.assertNotRegex(text, SECRETISH)
        self.assertEqual(text, "✅ Sync (scheduled): 3 unchanged · requests 1/24")

    def test_reasons_are_fixed_phrases(self):
        self.assertEqual(notify.reason_for(Problem("Daily guard: 24 SimpleFIN requests in the past 24 hours.")),
                         "daily request limit reached (24 per 24 h)")
        self.assertEqual(notify.reason_for(Problem("Bridge says https://user:SECRETTOKEN@x")), "SimpleFIN problem (see the Pi log)")
        self.assertEqual(notify.reason_for(Refused("table account_transactions holds 3 rows")),
                         "transaction data found on the host (start-up check)")
        odd = notify.reason_for(RuntimeError("GET https://user:SECRETTOKEN@x failed"))
        self.assertEqual(odd, "unexpected error (RuntimeError)")


class SyncHooks(Base):
    def setUp(self):
        super().setUp()
        self.sink = Sink()
        patcher = patch.object(notify, "TRANSPORT", self.sink)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_by_default(self):
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.sink.sent, [])

    def test_successful_run_posts_one_quiet_line_to_the_sync_channel(self):
        self.assertEqual(self.run_sync(config=CONFIG), 0)
        self.assertEqual(self.sink.texts(HEALTH), [])
        self.assertEqual(self.sink.texts(SYNC), ["✅ Sync (scheduled): 12 new · requests 1/24"])
        self.assertNotRegex(self.sink.texts(SYNC)[0], SECRETISH)
        self.assertEqual(self.sink.sent[0][1]["allowed_mentions"], {"parse": []})

    def test_failure_posts_to_both_and_pings_from_health(self):
        class Broken(type(self.provider)):
            def fetch(self):
                raise Problem("Bridge says https://user:SECRETTOKEN@x")
        self.assertEqual(self.run_sync(Broken(), config=CONFIG), 1)
        self.assertEqual(self.sink.texts(SYNC), ["❌ Sync failed (scheduled): SimpleFIN problem (see the Pi log)"])
        health = [m for u, m in self.sink.sent if u == HEALTH]
        self.assertEqual(len(health), 1)
        self.assertTrue(health[0]["content"].startswith(f"<@{USER}> 🚨 ❌ Sync failed"))
        for _, m in self.sink.sent:
            self.assertNotIn("SECRETTOKEN", m["content"])

    def test_nothing_refreshed_and_guard_and_missing_db_are_reported(self):
        self.assertEqual(self.run_sync(), 0)
        class Silent(type(self.provider)):
            def fetch(self):
                return dict(connections=[], accounts=[], errlist=[])
        self.sink.sent.clear()
        import time
        self.assertEqual(self.run_sync(Silent(), config=CONFIG, clock=lambda: time.time() + 3 * 3600), 1)
        self.assertIn("no account was refreshed", self.sink.texts(SYNC)[0])
        self.sink.sent.clear()
        self.assertEqual(daily_sync.run(self.dir / "absent.sqlite3", self.provider, err=lambda m: None, config=CONFIG), 1)
        self.assertIn("database problem", self.sink.texts(SYNC)[0])

    def test_a_dead_discord_changes_nothing_about_the_sync(self):
        self.sink.error = OSError("network down")
        with redirect_stderr(StringIO()):
            self.assertEqual(self.run_sync(config=CONFIG), 0)
        self.assertEqual(self.sql("SELECT count(*) FROM accounts"), [(13,)])

    def test_button_posts_too_without_waiting_for_discord(self):
        server = serve(self.path, 0, self.provider)
        server.notify = CONFIG
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/", timeout=5) as page:
            token = re.search(r'name="token" value="([^"]+)"', page.read().decode())[1]
        opener = build_opener(type("NoRedirect", (HTTPRedirectHandler,), {"redirect_request": lambda *a, **k: None}))
        try:
            opener.open(Request(base + "/sync", data=urlencode({"token": token}).encode()), timeout=10)
        except HTTPError as redirect:
            self.assertEqual(redirect.code, 303)
        notify.wait()
        self.assertEqual(self.sink.texts(SYNC), ["✅ Sync (button): 12 new · requests 1/24"])


def facts(**over):
    base = dict(now=1_800_000_000.0, uptime=3 * 86400 + 4 * 3600, load=0.12, temp=48.0, disk_pct=11, disk_gb=128, mem_pct=22,
                dashboard=True, units={"accounts-dashboard.service": "active", "accounts-sync.timer": "active",
                                       "accounts-backup.timer": "active"},
                failed=[], backup_age=7 * 3600, backup_bytes=9000, sync_age=3600, requests=2)
    base.update(over)
    return base


class Health(Case):
    def setUp(self):
        super().setUp()
        self.state = self.dir / "health-state.json"

    def test_healthy_facts_have_no_problems_and_each_threshold_trips(self):
        self.assertEqual(pi_health.problems(facts()), {})
        cases = {"dashboard": dict(dashboard=False), "disk": dict(disk_pct=81), "memory": dict(mem_pct=91),
                 "temp": dict(temp=81.0), "sync": dict(sync_age=37 * 3600), "backup": dict(backup_age=37 * 3600),
                 "units": dict(failed=["accounts-sync.service"])}
        for key, over in cases.items():
            with self.subTest(key):
                self.assertEqual(list(pi_health.problems(facts(**over))), [key])
        self.assertIn("sync", pi_health.problems(facts(sync_age=None)))
        self.assertIn("backup", pi_health.problems(facts(backup_age=None, backup_bytes=None)))

    def test_digest_text(self):
        text = pi_health.digest_text(facts())
        for part in ("🩺 Pi health", "up 3d 4h", "🌡️ 48°C", "disk 11% of 128 GB", "memory 22%", "🟢 dashboard", "newest backup 7 h ago (9 KB)",
                     "last sync 1 h ago", "requests 2/24", "✅ nothing needs attention"):
            self.assertIn(part, text)
        self.assertNotRegex(text, SECRETISH)
        bad = pi_health.digest_text(facts(dashboard=False, units={"accounts-sync.timer": "failed"}))
        self.assertIn("🔴 dashboard", bad)
        self.assertIn("⚠️ Dashboard is not answering", bad)
        self.assertLess(len(pi_health.digest_text(facts(backup_age=None))), 1000)

    def test_check_alerts_once_repeats_after_a_day_and_says_when_recovered(self):
        post = lambda **over: pi_health.check(CONFIG, facts(**over), self.state)
        self.assertEqual(post(), [])
        first = post(dashboard=False, now=1_800_000_100.0)
        self.assertEqual(len(first), 1)
        self.assertIn("🚨 Dashboard is not answering", first[0])
        self.assertTrue(self.sink.sent[-1][1]["content"].startswith(f"<@{USER}>"))
        self.assertEqual(post(dashboard=False, now=1_800_003_700.0), [])                 # an hour later: quiet
        again = post(dashboard=False, now=1_800_000_100.0 + 24 * 3600)
        self.assertIn("🔁 Still: Dashboard is not answering", again[0])
        n = len(self.sink.sent)
        done = post(now=1_800_000_100.0 + 25 * 3600)
        self.assertEqual(done, ["✅ Recovered: dashboard"])
        self.assertNotIn("<@", self.sink.sent[-1][1]["content"])                           # recovery does not ping
        self.assertEqual(len(self.sink.sent), n + 1)
        self.assertEqual(post(now=1_800_000_100.0 + 26 * 3600), [])
        self.assertEqual(stat_mode(self.state), 0o600)

    def test_two_new_problems_are_one_message(self):
        sent = pi_health.check(CONFIG, facts(disk_pct=90, temp=85.0), self.state)
        self.assertEqual(len(sent), 1)
        self.assertIn("Disk is 90% full", sent[0])
        self.assertIn("CPU temperature is 85°C", sent[0])

    def test_a_down_discord_means_the_alert_is_retried_next_hour(self):
        self.sink.error = OSError("down")
        with redirect_stderr(StringIO()):
            self.assertEqual(pi_health.check(CONFIG, facts(dashboard=False), self.state), [])
        self.sink.error = None
        again = pi_health.check(CONFIG, facts(dashboard=False, now=1_800_003_600.0), self.state)
        self.assertEqual(len(again), 1)

    def test_corrupt_state_is_ignored(self):
        self.state.write_text("{not json")
        self.assertEqual(len(pi_health.check(CONFIG, facts(dashboard=False), self.state)), 1)

    def test_failed_units_keeps_only_ours(self):
        fake = type("R", (), {"stdout": "cron.service\naccounts-sync.service\nevil;rm.service\naccounts-backup.timer\n"})
        with patch.object(pi_health.subprocess, "run", return_value=fake):
            self.assertEqual(pi_health.failed_units(), ["accounts-backup.timer", "accounts-sync.service"])
        with patch.object(pi_health.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(pi_health.failed_units(), [])
            self.assertEqual(set(pi_health.unit_states().values()), {"unknown"})

    def test_newest_backup(self):
        self.assertEqual(pi_health.newest_backup(self.dir), (None, None))
        self.assertEqual(pi_health.newest_backup(""), (None, None))
        old, new = self.dir / "accounts-1.sqlite3.gpg", self.dir / "accounts-2.sqlite3.gpg"
        old.write_bytes(b"x" * 10)
        new.write_bytes(b"y" * 20)
        os.utime(old, (1_000_000, 1_000_000))
        (self.dir / "other.txt").write_text("z")
        age, size = pi_health.newest_backup(self.dir)
        self.assertEqual(size, 20)
        self.assertLess(age, 60)

    def test_probe_runs_anywhere_and_main_is_silent_without_a_config(self):
        f = pi_health.probe(self.dir / "absent.sqlite3", self.dir)
        for key in ("now", "disk_pct", "dashboard", "units", "failed", "backup_age", "sync_age", "requests"):
            self.assertIn(key, f)
        self.assertIsNone(f["sync_age"])
        with patch.object(pi_health.notify, "load", return_value=None):
            out = StringIO()
            with patch("sys.stdout", out):
                self.assertEqual(pi_health.main(["digest"]), 0)
        self.assertIn("notifications are off", out.getvalue())
        self.assertEqual(self.sink.sent, [])


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


class Units(unittest.TestCase):
    def test_units(self):
        for name, action, calendar in (("digest", "digest", "OnCalendar=*-*-* 07:00:00"), ("check", "check", "OnCalendar=hourly")):
            service = (DEPLOY / f"accounts-health-{name}.service").read_text()
            timer = (DEPLOY / f"accounts-health-{name}.timer").read_text()
            self.assertIn(f"pi_health.py {action}", service)
            self.assertIn("Type=oneshot", service)
            self.assertIn("UMask=0077", service)
            self.assertIn("EnvironmentFile=-%h/.config/planner-accounts/backup.env", service)
            for text in (service, timer):
                self.assertNotIn("discord.com", text)
                self.assertNotIn("webhook", text.lower())
            self.assertIn(calendar, timer)
            self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
