"""Encrypted online snapshot or offline restore; invoke via backup-sqlite.sh."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hosted_auth import AUTH_TABLES, db_path, external, offline_lock, private, state_lock
from hosted_guard import describe, violations, EXPORT_COMMAND
from server import DEFAULT_DB


TABLES = {"accounts", "balance_history", "source_links", "fetch_attempts"}
GPG = ["gpg", "--no-options", "--batch", "--no-tty", "--quiet"]


@contextmanager
def readonly(path):
    # On a WAL database, a mode=ro connection can leave sidecars behind if it
    # is the last connection. Allow SQLite housekeeping but no SQL writes.
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        yield db


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


def checked_image(db, current=None):
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA secure_delete=ON")
    names = tables(db)
    if not TABLES <= names or db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Snapshot failed dashboard schema or integrity checks.")
    found = violations(db)
    if found:  # the timer runs without the service: never archive (or restore) data the host must not hold
        raise ValueError(f"Snapshot refused: {describe(found)}. Transaction data must not be on the host; "
                         f"install a file made by {EXPORT_COMMAND} instead.")
    with db:
        for table in AUTH_TABLES:
            if table in names:
                db.execute(f"DELETE FROM {table}")
        if current and current.exists():
            with readonly(current) as existing:
                pins = (existing.execute("SELECT email, sub FROM hosted_identities").fetchall()
                        if "hosted_identities" in tables(existing) else [])
            db.execute("""CREATE TABLE IF NOT EXISTS hosted_identities (
                email TEXT PRIMARY KEY NOT NULL, sub TEXT UNIQUE NOT NULL)""")
            for email, sub in pins:
                db.execute("INSERT OR IGNORE INTO hosted_identities VALUES (?,?)", (email, sub))
                if not db.execute("SELECT 1 FROM hosted_identities WHERE email=? AND sub=?",
                                  (email, sub)).fetchone():
                    raise ValueError("Backup identity pins conflict with the current database.")
    # Purge deleted auth material from free pages before the image leaves memory.
    db.execute("VACUUM")
    return db.serialize()


def online_image(path):
    with readonly(path) as source, closing(sqlite3.connect(":memory:")) as copied:
        source.backup(copied)
        raw = copied.serialize()
    # The online backup already contains the committed WAL data, with no separate
    # sidecar; SQLite's serialize nevertheless retains WAL-mode header bytes.
    # Convert that standalone image to rollback-journal format before deserialize.
    if raw[18:20] == b"\x02\x02":
        raw = raw[:18] + b"\x01\x01" + raw[20:]
    with closing(sqlite3.connect(":memory:")) as snapshot:
        snapshot.deserialize(raw)
        return checked_image(snapshot)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup(path, dry_run):
    destination = external(os.environ["ACCOUNTS_BACKUP_DIR"])
    private(destination, directory=True)
    if destination == path.parent or path.parent in destination.parents:
        raise ValueError("Backup destination must be separate from the database directory.")
    recipient = os.environ.get("ACCOUNTS_BACKUP_RECIPIENT", "").upper()
    if not re.fullmatch(r"(?:[A-F0-9]{40}|[A-F0-9]{64})", recipient):
        raise ValueError("Set ACCOUNTS_BACKUP_RECIPIENT to the full recovery key fingerprint.")
    raw = online_image(path)
    # Choose the same MDC archive format on both Bookworm and newer GnuPG versions.
    command = [*GPG, "--rfc4880", "--force-mdc", "--trust-model", "always", "--recipient", recipient, "--encrypt"]
    if dry_run:
        subprocess.run(command, input=raw, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print("Dry-run: SQLite snapshot and encryption verified; no file written.")
        return
    date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination / f"accounts-{date}-{secrets.token_hex(4)}.sqlite3.gpg"
    fd, stage = tempfile.mkstemp(prefix=".accounts-encrypted-", dir=destination)
    try:
        with os.fdopen(fd, "wb") as output:
            subprocess.run(command, input=raw, stdout=output, stderr=subprocess.DEVNULL, check=True)
            if output.tell() == 0:
                raise ValueError("No ciphertext produced.")
            os.fsync(output.fileno())
        os.link(stage, target)  # Publish atomically without replacing a backup.
    finally:
        os.unlink(stage)
    sync_directory(destination)
    print(f"Encrypted backup written: {target.name}")


def restore(path, archive, dry_run):
    archive = external(archive)
    private(archive)
    with offline_lock(path):
        if any(os.path.lexists(str(path) + ext) for ext in ("-wal", "-shm", "-journal")):
            raise ValueError("SQLite sidecars exist. Close all database users; do not delete sidecars.")
        # GPG may produce partial plaintext before rejecting corruption. Do not use
        # any output until successful exit and then SQLite integrity validation.
        result = subprocess.run([*GPG, "--status-fd", "2", "--decrypt", str(archive)],
                                capture_output=True, check=True)
        status = set(result.stderr.splitlines())
        if not {b"[GNUPG:] DECRYPTION_OKAY", b"[GNUPG:] GOODMDC"} <= status:
            raise ValueError("Expected a successfully decrypted, integrity-protected GPG archive.")
        with closing(sqlite3.connect(":memory:")) as snapshot:
            snapshot.deserialize(result.stdout)
            image = checked_image(snapshot, path)
        if dry_run:
            print("Dry-run: decryption, SQLite integrity, and auth reset verified; database unchanged.")
            return
        fd, stage = tempfile.mkstemp(prefix=".accounts-restore-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(image)
                output.flush()
                os.fsync(output.fileno())
            os.replace(stage, path)
            sync_directory(path.parent)
        finally:
            if os.path.lexists(stage):
                os.unlink(stage)
        print("Restored dashboard database; sessions and sign-in challenges revoked.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--archive", type=Path, help="Restore from this encrypted archive")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if bool(args.archive) != (args.action == "restore"):
        parser.error("Exactly the restore action needs --archive.")
    try:
        home = external(os.environ.get("GNUPGHOME", str(Path.home() / ".gnupg")))
        private(home, directory=True)
        path = db_path(args.db)
        with state_lock(path, "backup", "Another backup or restore is in progress."):
            if args.action == "backup":
                private(path)
                backup(path, args.dry_run)
            else:
                restore(path, args.archive, args.dry_run)
    except KeyError:
        sys.exit("Set ACCOUNTS_BACKUP_DIR to the private external backup directory.")
    except ValueError as exc:
        sys.exit(str(exc))
    except (OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        sys.exit(f"Backup/restore failed ({type(exc).__name__}); check key, permissions and SQLite state.")


if __name__ == "__main__":
    main()
