"""Save the Anthropic API key for the transaction categoriser (L30b), without it ever appearing on a screen,
in a command line, in shell history, in the chat or in a log.

    python3 deploy/set_anthropic_key.py [--dir DATA_DIR] [--replace]

Run it as the service user in a real terminal (for example `ssh -t pi 'sudo -H -u acctdash python3 ...'`). It asks
for the key at a hidden prompt and writes DATA_DIR/anthropic.key (default ~/.local/share/planner-accounts), mode
0600 in a 0700 directory, exactly where tx_classifier.load_key looks. It will not replace an existing key unless
--replace is given, and it checks the key only for the right shape (it makes no network request). Only cleaned
merchant names are ever sent to Anthropic, and only when the page's "Auto-categorize with AI" box is ticked.
"""
import argparse
import getpass
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tx_classifier import KEY_FILE, _KEY_PATTERN

DEFAULT_DIR = Path.home() / ".local/share/planner-accounts"


class Refused(Exception):
    pass


def save(directory, key, replace=False):
    directory = Path(os.path.abspath(directory))
    if directory.resolve() != directory or any((p / ".git").exists() for p in (directory, *directory.parents)):
        raise Refused("The data directory must be outside Git and not behind a symlink.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Refused("The data directory must be owned by you with mode 0700.")
    key = key.strip()
    if not _KEY_PATTERN.fullmatch(key) or len(key) > 200:
        raise Refused("That does not look like an Anthropic API key (nothing was saved).")
    target = directory / KEY_FILE
    if os.path.lexists(target) and not replace:
        raise Refused(f"{target} already exists; use --replace to overwrite it.")
    fd, staging = tempfile.mkstemp(prefix=".anthropic-", dir=directory)       # 0600, same directory
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        os.replace(staging, target)                                           # atomic; never a half-written key
    except BaseException:
        if os.path.lexists(staging):
            os.unlink(staging)
        raise
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)             # fail instead of echoing
            key = getpass.getpass("Anthropic API key (hidden): ")
        target = save(args.dir, key, args.replace)
    except (Refused, getpass.GetPassWarning, OSError) as exc:
        print(f"Key not saved: {exc if isinstance(exc, Refused) else type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"Key saved to {target} (mode 600). It was not printed anywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
