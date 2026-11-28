#!/bin/sh
# This wrapper never sources an env file or places a secret on the command line.
set -eu
umask 077
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/backup_sqlite.py" "$@"
