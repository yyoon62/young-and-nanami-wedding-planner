# Private accounts dashboard on a Raspberry Pi

Use this runbook only after review. The Pi serves an HTTP backend on **127.0.0.1:8765**; **Tailscale Serve** terminates HTTPS and makes the site reachable only inside the tailnet. **Never enable Funnel**, port forwarding, a public proxy, or a GitHub Pages link to this dashboard. Google sign-in independently gates the app: either approved member can view and edit the **whole household dashboard**. Account owner filters are not permissions.

> **Move your data with the scrubbed export (section 2a), never by copying the dashboard database file.** `--hosted` hides the Transactions tab (every route, including its POSTs and sync, returns 404), but hiding is not removing: a database file that contains transaction rows keeps them in plaintext at rest on the Pi, and every encrypted backup would contain them (verified with a sentinel row in the L21 rehearsal, section 7). Two safeguards now exist (L24): `deploy/export_scrubbed.py` builds a **new** file holding balances only, and `--hosted` **refuses to start** on any database that holds rows outside a short balance-level allowlist (the backup and restore tool apply the same rule). The export is the only supported way to bring existing data to the Pi. Bridge re-sync on the Pi is the other, independent data path.

## 1. Set up the Pi and client devices

1. Flash a current **64-bit Raspberry Pi OS Lite** image (Bookworm or newer, Python 3.11+). In Raspberry Pi Imager choose a neutral hostname (for example `accounts-pi`), a dedicated non-root service user, and SSH key authentication if you need remote shell access. Boot on a trusted network, update packages and check that time sync works (`timedatectl status`); incorrect time breaks TLS and Google token verification.
2. **Check, do not blanket-install.** `dpkg -s git python3-venv gnupg ca-certificates curl` should all report `install ok installed`; install **only what is missing**. On an already-provisioned shared Pi, `apt install` of packages that are already present also *upgrades* them (and their dependencies), so do not run it as a habit, and do not run `apt update`, `upgrade` or `autoremove` as part of this runbook. Check `python3 -c 'import sqlite3; assert hasattr(sqlite3.Connection,"serialize") and hasattr(sqlite3.Connection,"deserialize")'`. Deploy a reviewed release commit containing `_local/accounts/` **without giving the Pi any GitHub credential**: from the Mac, `git archive` the commit and unpack it as the service user (no `.git` directory is needed on the Pi):

   ```sh
   # on the Mac, in the planner repo; COMMIT is the reviewed release commit
   COPYFILE_DISABLE=1 git archive --format=tar --prefix=planner/ COMMIT _local/accounts \
     | ssh pi 'sudo -H -u acctdash tar -x -C /home/acctdash'
   ```

   (Replace `acctdash` with your service user. If the service user is also your administrator account, drop `sudo -H -u …`.) Record the commit hash; to update, stop the service, unpack a newer commit over `~/planner`, restart. Do not clone or copy the notes repo or other personal data to the Pi. Install the hosted-only verifier into its dedicated venv (`--only-binary=:all:` makes a missing wheel fail loudly instead of trying to compile; `cryptography` ships `cp311` `aarch64` wheels):

   ```sh
   umask 077
   python3 -m venv "$HOME/.local/share/planner-accounts-venv"
   "$HOME/.local/share/planner-accounts-venv/bin/python" -m pip install --only-binary=:all: \
     -r "$HOME/planner/_local/accounts/requirements-hosted.txt"
   ```

   Keep the venv private and update it with reviewed releases. Ordinary local mode and the backup tool do not need the venv or any Python packages. Do not run the hosted service with system Python.
3. Follow the [official Tailscale Linux install](https://tailscale.com/download/linux), then `sudo tailscale up` and authorize the Pi in your household tailnet. In the Tailscale admin console enable **MagicDNS** and [HTTPS certificates](https://tailscale.com/kb/1153/enabling-https). HTTPS certificate transparency can expose the device’s DNS name; do not put names or financial details in it.
4. Invite the second member to the **same tailnet**. Install Tailscale on each phone (App Store/Play Store) and Mac, sign in to the invited/authorized tailnet account, and connect. Those identities assign network access; they need not be the Google identities used by the app. Ensure tailnet policy permits the two member identities to reach the Pi on TCP 443 and no other access than intended. If using tags and `grants`, grant the two identities `tcp:443` on the Pi tag and remove any broader rule that would also match that tag; check Tailscale policy tests. Keep administrator SSH access separate.
5. On the Pi run:

   ```sh
   sudo tailscale serve --bg 8765
   sudo tailscale serve status
   ```

   Record the exact `https://accounts-pi.<tailnet-name>.ts.net` origin printed by Serve, **without** `/`. Verify the listener is tailnet-only and proxies `http://127.0.0.1:8765`. A backend error until the app starts is expected. Do not publish the name in Git. For rollback, stop the service and run `sudo tailscale serve --https=443 off` on this dedicated Serve endpoint. Serve’s background config survives reboot; verify it after restarting.

## 2. Configure Google and private state

1. In [Google Auth Platform](https://console.cloud.google.com/auth/clients), configure Branding and Audience, then create a **Web application** OAuth client. For members outside one Workspace organization, choose External; when in Testing, add both approved accounts under Test users as applicable. Only the basic OpenID Connect scopes (`openid`, `email`, `profile`) are needed. Do not request bank scopes, refresh tokens, or enable extra APIs.
2. Put the **exact Serve origin** in **Authorized JavaScript origins**. No path, trailing slash, wildcard, LAN IP, localhost, alternate short name, or redirect URI for the popup callback. The app needs only the client ID; do not put the OAuth client secret on the Pi. Google’s validation of your actual `*.ts.net` origin, branding or authorized-domain requirements, and the browser popup all need to be checked before importing any real records. If Google disallows the origin or requires a domain you do not control, **stop**; do not use a public proxy, bypass the gate, or enter a different origin and hope that it works. Review [Google’s setup reference](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid).
3. As the non-root dashboard user, prepare state:

   ```sh
   umask 077
   install -d -m 700 "$HOME/.config/planner-accounts" \
     "$HOME/.local/share/planner-accounts" "$HOME/.config/systemd/user"
   ```

   Create the file privately **before** entering any settings. The command below refuses to overwrite an existing file. Edit in a private terminal with an editor that does not make public backup files:

   ```sh
   (umask 077; set -C; : > "$HOME/.config/planner-accounts/hosted.env")
   nano "$HOME/.config/planner-accounts/hosted.env"
   ls -l "$HOME/.config/planner-accounts/hosted.env"
   ```

   Expect a service-user-owned regular file (`-rw-------`), no links, outside Git. `--hosted-env` checks the directory and the opened file before reading its values and before entering Bridge/SQLite. The unit does **not** load it as a systemd `EnvironmentFile`; don't source it as shell code. Use four literal `KEY=value` lines (no quotes, `export`, or interpolations). The following are nonworking examples:

   ```ini
   GOOGLE_CLIENT_ID=123456-fictional.apps.googleusercontent.com
   LOANS_MEMBERS=memberalpha@gmail.com,member.beta@workspace.example
   LOANS_ORIGIN=https://accounts-pi.example-tailnet.ts.net
   LOANS_SESSION_KEY=REPLACE_WITH_RANDOM_64_HEX_DIGITS
   ```

   Generate the session key locally with `python3 -c 'import secrets; print(secrets.token_hex(32))'` in a private terminal; don't put it in command history or logs. List exactly two distinct Google-hosted primary mailboxes. Use the dotless canonical Gmail spelling (`@gmail.com` only, no `+` alias); the Google-signed spelling may contain dots. For Workspace use the actual primary address, not an alias or a consumer Google Account registered with third-party email. Confirm the two identities on the first sign-ins: the app pins each email to its stable Google `sub` on first verified exchange. Subsequent subject changes fail closed; do not edit pins casually. Rotate the session key and restart if sessions may be compromised; remove obsolete allowlist entries and restart before re-enrolling anyone.
4. First run the release checks with **no Bridge credential and a separate, fictional database**. To test real GIS later on this fresh Pi without enrolling Bridge, run:

   ```sh
   "$HOME/.local/share/planner-accounts-venv/bin/python" "$HOME/planner/_local/accounts/server.py" \
     --port 8765 --hosted --hosted-env "$HOME/.config/planner-accounts/hosted.env" \
     --db /absolute/private/smoke/fictional.sqlite3
   ```

   Replace the smoke DB path with your own, inside **its own `0700` directory owned by the service user** (for example `install -d -m 700 "$HOME/smoke"`); a database directly under a shared directory such as `/tmp` is refused. Stop this process before starting the service. The data directory must be owned by the app user and `0700`, SQLite file `0600`, outside Git with no symlinks. Never reuse the hosted DB in an ordinary unhosted server on the same proxied port.
5. Once approved for actual use, follow [Bridge setup](../BULK.md) using `python3 "$HOME/planner/_local/accounts/bridge.py" setup` and the private hidden prompt. This saves the revocable Access URL at `~/.local/share/planner-accounts/bridge.access` (`0600`); never put it in the unit, env file, logs or Git. It is not included in SQLite backups. Enroll again on recovery if needed; revoke old credentials when retiring the original Pi.

   The shipped unit does **not** pass `--bridge`: `server.py --bridge` exits when `bridge.access` is missing, and `Restart=on-failure` would restart it every 10 seconds forever. Only after enrolment, enable Bridge with a drop-in (the first, empty `ExecStart=` is required to clear the original):

   ```sh
   systemctl --user edit accounts-dashboard.service   # or write the file below by hand, mode 0600
   # ~/.config/systemd/user/accounts-dashboard.service.d/bridge.conf
   #   [Service]
   #   ExecStart=
   #   ExecStart=%h/.local/share/planner-accounts-venv/bin/python %h/planner/_local/accounts/server.py --port 8765 --bridge --hosted --hosted-env %h/.config/planner-accounts/hosted.env
   systemctl --user daemon-reload && systemctl --user restart accounts-dashboard.service
   ```

   With Bridge enabled, any signed-in member can press **Sync balances** (balances only). Transactions are never synced when hosted. Remove the drop-in and restart to disable.

## 2a. Move your existing balances to the Pi (scrubbed export)

Do this once, **before the first start of the real service** (section 3), if you want your balances, balance history, account terms, Bridge links and projection settings on the Pi without re-entering them. It is optional: an empty Pi database plus Bridge re-sync also works (history then starts from the first sync).

**What the export is.** `deploy/export_scrubbed.py` creates a brand-new SQLite file with the app's own schema and copies in rows from an allowlist of tables only: `accounts`, `balance_history`, `source_links`, `account_terms`, `projection_assumptions`. Transactions, merchant rules, classifier data, sign-in state and any table added in future are not read into it (a new table is excluded until someone adds it to `hosted_guard.EXPORT_TABLES` on purpose). It is not a copy of your file with rows deleted, so nothing deleted earlier can linger in it. Columns are copied by name; any column your source has that this version does not know is listed in the summary, not silently dropped. The source is opened read-only and never changed, so the dashboard may keep running while you export.

**1. On the Mac** (from your planner checkout; the default source is `~/.local/share/planner-accounts/accounts.sqlite3`; add `--db PATH` only for another file):

```sh
cd _local/accounts
mkdir -p -m 700 ~/private-export
tmutil addexclusion ~/private-export                       # keep the plaintext file out of Time Machine
python3 deploy/export_scrubbed.py --out ~/private-export/accounts-export.sqlite3
```

The command refuses to overwrite an existing file, to write inside a Git checkout, or through a symlink; on any failure it prints one line and leaves no partial file. The output is mode `0600`. Read the printed summary: row counts for the five copied tables, the excluded tables with their row counts, and one line about assumptions. **Projection assumptions** may contain monthly income and spending figures that you accepted from a transaction-based suggestion; they are totals, not transactions, and cannot be told apart from numbers you typed. If you do not want them on the Pi add `--no-assumptions` (the Pi then starts with the default assumptions). **The export holds your real balances.** Put it only in `~/private-export`; never in Git, in the notes repo, or in iCloud Drive, Dropbox, Desktop or Documents.

**2. Transfer** (over Tailscale or your LAN) while the dashboard service and backup timer are **not running** (before their first start nothing needs stopping; on a Pi that already ran them, stop both with the `--machine` prefix from section 3). Check there is no database yet (this procedure never replaces one), copy the file, install it for the service user, and compare checksums:

```sh
# on the Pi, as the administrator account
sudo test ! -e /home/acctdash/.local/share/planner-accounts/accounts.sqlite3 && echo "no database yet: ok"
# on the Mac
scp ~/private-export/accounts-export.sqlite3 pi:accounts-export.sqlite3
shasum -a 256 ~/private-export/accounts-export.sqlite3
# on the Pi, as the administrator account
sudo install -o acctdash -g acctdash -m 600 accounts-export.sqlite3 /home/acctdash/.local/share/planner-accounts/accounts.sqlite3
sudo sha256sum /home/acctdash/.local/share/planner-accounts/accounts.sqlite3
sudo ls -l /home/acctdash/.local/share/planner-accounts/      # expect -rw------- acctdash acctdash
```

(`pi` is your ssh host alias. If the Pi already has a database, move it aside deliberately first; do not overwrite it. These Pi commands were written from the rehearsal's account layout and have not been run on the Pi in L24.) The two checksums must match; `install -m 600` sets the mode the app requires (a `0600` file in a `0700` directory).

**3. Delete the export** as soon as the checksums match: on the Mac `rm ~/private-export/accounts-export.sqlite3` and empty the Trash, and on the Pi `rm ~/accounts-export.sqlite3` (the administrator's staging copy; the installed file is separate). The installed file is the Pi's live database and is covered by section 4's backups.

**4. Start** with `systemctl --user start accounts-dashboard.service` (section 3). The guard passes, the app's own migration adds its empty tables, and the first sign-in pins each Google identity. Check balances and the history chart against the Mac. If you later enrol Bridge on the Pi (section 2.5), the exported links should make the first sync report existing accounts as updated or unchanged, not added: `source_links` keys depend on the Bridge host and path, not on the token, so this is expected, but it is the same duplicate check as the release checklist and was **not** verified against a real Bridge. The Bridge credential itself is never part of the export.

**The guard.** On every `--hosted` start, before the app opens the database for writing, the server reads the file read-only and lists any table outside the allowlist that holds rows (the built-in category names the app seeds itself, and the Pi's own sign-in and quota tables, are allowed). If any exist it exits with status 1 and prints which table and how many rows, for example `Hosted startup refused: table account_transactions holds 3 rows. Transaction data must not be on the host. On the machine that holds your data run python3 deploy/export_scrubbed.py --out FILE and install that file instead.` The file is left byte-identical and nothing is migrated. `deploy/backup-sqlite.sh` applies the same rule to the snapshot it archives or restores, so the nightly timer cannot archive such a database either. Do **not** respond by deleting the offending rows on the Pi; build a fresh export and install it.

**Why the export is the only supported transfer path.** The guard counts rows, not bytes. A file in which transactions were deleted has empty tables but can still hold their text in SQLite free pages, so it passes the guard. To close that gap a hosted start compacts the file (SQLite `VACUUM`) when it has free pages and logs `Hosted startup: compacted the database file (N free pages removed).`; that purges SQLite's own pages but cannot reach copies a filesystem or flash storage may keep elsewhere. Backups are unaffected (they compact in memory before encrypting). A file made by the export has no such history, so it is the one path with a guarantee. Copying the DB file, restoring an old archive, or `sqlite3 .dump | sqlite3` are not supported.

## 3. Install the user service

After stopping the fictional smoke listener, run as the service user (adjust unit paths if your code checkout differs). **If the service user is a separate, non-login account on a shared machine** (as in the rehearsal), it has no `sudo` and no D-Bus session, so the commands below do not work from `sudo -u` or `su`. Have an administrator account do the two privileged steps once, and drive units from the administrator account with `--machine`:

```sh
sudo adduser --system --group --shell /bin/bash --home /home/acctdash acctdash   # a system UID is hidden from the login greeter
sudo loginctl enable-linger acctdash       # starts the user manager now and at boot; needs dbus-user-session
sudo systemctl --user --machine=acctdash@.host status    # then use this prefix instead of plain `systemctl --user`
sudo journalctl _SYSTEMD_USER_UNIT=accounts-dashboard.service -o cat   # a system UID has no per-user journal: `journalctl --user` finds nothing
sudo systemctl --user --machine=acctdash@.host mask --now pipewire.socket pipewire-pulse.socket pipewire.service pipewire-pulse.service wireplumber.service filter-chain.service
```

The last command is recommended on Raspberry Pi OS desktop images: starting the service user's manager also starts that user's audio stack (pipewire/wireplumber, which even tries to register Bluetooth profiles). The dashboard does not need any of it. If the user manager does not start for a system UID, recreate the account with a regular UID (`adduser --disabled-password`) and expect it to appear on a graphical login screen (this did not happen in the rehearsal: a system UID worked). Linger starting the service at boot is untested until the machine is rebooted. The commands below assume you are inside the service user's own session:

```sh
cd "$HOME/planner/_local/accounts"
install -m 644 deploy/accounts-dashboard.service ~/.config/systemd/user/
# linger was enabled above, once, by an administrator (a dedicated service user has no sudo)
systemctl --user daemon-reload
systemd-analyze --user verify "$HOME/.config/systemd/user/accounts-dashboard.service"
systemctl --user enable --now accounts-dashboard.service
systemctl --user status accounts-dashboard.service
ss -ltn 'sport = :8765'
curl -si http://127.0.0.1:8765/new
```

Expect only `127.0.0.1:8765`, unauthenticated `/new` to return 401, and `/` to show only sign-in. Use `journalctl --user -u accounts-dashboard.service` locally to troubleshoot; don't share account or setup values. A `403` on submit usually means the browser used a URL other than the exact configured origin or its form belonged to a different/expired session. Access the canonical URL directly in a full browser, not an in-app webview.

The sign-in page alone permits the GIS script, iframe, stylesheet, and connection endpoints, with COOP `same-origin-allow-popups`. No One Tap or automatic sign-in is enabled. Protected pages do not load GIS; browser assets and cacheable logos are behind the same login gate and sent `no-store`. A hosted logo cache miss may send the institution domain (not the balance or household identity) to Google’s favicon service; it never fetches a provider-supplied host directly. If that lookup is blocked or fails, the app uses a monogram. Federal preview processes the file in the browser only. Sessions expire in one hour; starting sign-in creates a five-minute, browser-bound challenge, and logout revokes the server-side session. Signing out of this app does not sign the user out of Google or every other device.

## 3a. Daily balance sync (so a day is never missed because nobody clicked)

`daily_sync.py` is the **Sync balances** button without a browser: same `Batch.sync()`, same 24-requests-per-24-hours guard (kept in the database, shared with the web server), balances only, never transactions. `--hosted` adds the same checks as the server (private paths, refuses a database holding transaction rows). It needs the Bridge credential from section 2.5. Install as the service user, then enable the timer:

```sh
install -m 644 ~/planner/_local/accounts/deploy/accounts-sync.service ~/planner/_local/accounts/deploy/accounts-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start accounts-sync.service      # one real sync now (1 SimpleFIN request); expect "Synced: N unchanged/updated."
systemctl --user enable --now accounts-sync.timer
systemctl --user list-timers accounts-sync.timer
```

(From the administrator account use the `--machine=acctdash@.host` prefix from section 3, and `sudo journalctl _SYSTEMD_USER_UNIT=accounts-sync.service -o cat` for the log.)

- **When:** about 06:30 daily. `Persistent=true`: if the Pi was off or rebooting at that time, the missed run happens at boot.
- **Failures are loud:** any problem prints one line (never a traceback or URL) and exits 1; the unit retries up to 3 times, 20 minutes apart, then stays failed (`systemctl --user --failed`). A run in which no account was really refreshed also fails, so a bank that stopped answering is not reported as success.
- **On the page:** the Accounts page shows `🕒 Synced 3 h ago` beside **Sync balances**, and `⚠️ Last synced N days ago: balances may be out of date` once it is older than 36 hours (derived from `source_links.checked`; no extra table).
- **Budget:** each run is one SimpleFIN request (plus up to 3 retries on failure); the shared limit is 24 per day.
- Remove by `systemctl --user disable --now accounts-sync.timer`.

## 4. Encrypted, online SQLite backups

1. Generate a dedicated **passphrase-protected OpenPGP encryption key** on a trusted recovery device, export **only its public key**, and verify the full fingerprint out of band. Retain the private recovery key and passphrase in separate safe places. On the Pi, import the public key (`gpg --import /path/to/recovery-public.asc`) under the service user. The script selects the pinned full fingerprint; it will not fall back to another key or to plaintext. Backups are encrypted, not signed by the Pi, so restore only archives whose custody you trust.
2. **Warning — silent skip.** `accounts-backup.service` has `ConditionPathIsMountPoint=/mnt/accounts-backup`. If the drive is not mounted, systemd *skips the unit and reports success*: `systemctl --user start` returns 0, `status` says "condition failed", and the timer keeps firing. A healthy-looking timer can therefore mean **no backups at all**. Check recently: the journal (`Encrypted backup written: …` appears only when it really ran; a skipped run logs nothing) and the age of the newest archive, e.g. `find /mnt/accounts-backup/accounts -name 'accounts-*.sqlite3.gpg' -mtime -2 | grep -q . && echo recent || echo STALE`. Do this weekly until a loud-failure follow-up exists.

   Mount a private external POSIX filesystem at `/mnt/accounts-backup` (e.g. a USB drive configured by UUID) and create a service-user-owned `0700` `accounts` directory on the **mounted volume**. The timer checks the mount point to avoid quietly writing to the root disk; confirm mount success and protect the volume from other users. Copy **only encrypted archives** off-device as well if the external drive normally stays plugged into the Pi. Keep multiple generations; monitor available space and last successful archive. There is no automatic retention/pruning.
3. Create `~/.config/planner-accounts/backup.env` mode `0600`:

   ```ini
   ACCOUNTS_BACKUP_DIR=/mnt/accounts-backup/accounts
   ACCOUNTS_BACKUP_RECIPIENT=REPLACE_WITH_FULL_RECOVERY_KEY_FINGERPRINT
   ```

   `GNUPGHOME` is honoured (it must be an absolute, non-symlinked `0700` directory outside Git); the unit does not set it, so put `GNUPGHOME=…` in `backup.env` only if you use a non-default home. As the service user, export those settings in your shell for a manual test:

   ```sh
   cd "$HOME/planner/_local/accounts"
   ./deploy/backup-sqlite.sh backup --dry-run
   install -m 644 deploy/accounts-backup.service deploy/accounts-backup.timer ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user start accounts-backup.service
   systemctl --user enable --now accounts-backup.timer
   systemctl --user list-timers accounts-backup.timer
   journalctl --user -u accounts-backup.service
   ```

   Expect a `0600` `accounts-*.sqlite3.gpg` archive and **no staging plaintext backup**. Snapshots use SQLite's online backup API (including committed WAL data), not a copy of a live `.sqlite3` pathname. SQLite schema/integrity is checked in memory and sign-in sessions/challenges are scrubbed before encryption. The Pi's *working* SQLite DB and Bridge credential are plaintext at rest; consider disk/swap encryption and physical access as separate controls. Only DB state is backed up (accounts, history, Bridge links, quota and Google identity pins, not credentials, env files or favicon cache).

## 5. Rehearse restore before relying on backups

On a trusted recovery host with the code and private GPG key, use a scratch `0700` directory outside Git. If the GPG agent needs unlocking, first run `gpg --output /dev/null --decrypt "$ARCHIVE"` interactively in a private terminal. `ARCHIVE` below is an absolute, non-symlink path to a `0600` encrypted archive:

```sh
cd "$HOME/planner/_local/accounts"
umask 077
rehearsal=$(mktemp -d)
# On macOS, resolve the temp directory with `cd "$rehearsal" && pwd -P` first.
./deploy/backup-sqlite.sh restore --archive "$ARCHIVE" --db "$rehearsal/fictional.sqlite3" --dry-run
./deploy/backup-sqlite.sh restore --archive "$ARCHIVE" --db "$rehearsal/fictional.sqlite3"
```

Rehearse with a fictional archive and check exact amounts, dates, history and auth state. Remove the private scratch after review. For a real restore stop `accounts-dashboard.service`, `accounts-backup.timer` and any in-flight `accounts-backup.service`, and close any other DB clients. Do not remove SQLite sidecars to bypass checks; let SQLite checkpoint/close them first. Preserve an encrypted copy of the current DB for rollback. If your recovery host is separate, securely transfer the idle DB into private recovery scratch first so its newer identity pins can be merged; run dry-run and restore targeting that copy, then use a protected private transfer and same-filesystem atomic rename into the stopped Pi’s `~/.local/share/planner-accounts/accounts.sqlite3` (verify owner and mode `0600`). Alternatively restore directly on a trusted stopped Pi if the recovery key is available securely, for example via hardware token; don't leave it there. Restore validates decryption and SQLite before replacing the target, rejects conflicting pins, clears sessions and pending challenges, and preserves history and links. If the current DB is corrupt and pins cannot be read, quarantine it privately and verify the approved identities out of band before accepting a backup without the latest pins. A cancelled or failed restore does not intentionally publish an incomplete DB; an abrupt power loss before rename can leave a `0600` `.accounts-restore-*` staging file beside the DB. Review and remove any leftover staging file after verifying the restored DB. Restart, confirm fresh sign-in is required, and re-enable the timer.

## 6. Release checklist (before real records)

- Run the fixture-only checks from `_local/accounts` (at L24 the stdlib suite is 191 tests and `test_hosted` adds 30 in the hosted venv; with no `cryptography` installed the hosted ones skip, so a green run without the venv proves less). On macOS keep temp paths short (`TMPDIR=/tmp/x`): gpg-agent's socket path limit is ~104 characters and `test_backup` fails otherwise. After merging any task that touches Transactions, Projections or `server.py`, re-run `python -m unittest test_hosted.HostedTransactionsGated`: it signs in as each member and proves no transaction sentinel appears on any hosted route. Use a temporary venv with `-r requirements-hosted.txt` for `python -m unittest discover -v` (OpenSSL-signed fixtures and real GPG when installed). Run the unchanged Chrome suite with `"$TMPDIR/l05-browser-venv/bin/python" -m unittest -v browser_bulk browser_save browser_preview browser_history`. Separately use a temporary venv with hosted requirements plus Playwright for `python -m unittest -v browser_hosted`; it stubs GIS, not the dashboard.
- **Real GIS / `.ts.net`:** From separate phone and Mac browsers on the tailnet, verify Google accepts the exact HTTPS origin and returns from its popup (try iOS Safari and desktop/mobile Chrome used by the household). Both approved Google users must see the same fictional dashboard; a different Google account cannot gain access even if its device can reach Serve. Check no console CSP violations for Google, the editor, chart, or preview. Do not weaken CSP or expose via Funnel to fix sign-in. [Google's GIS credential reference](https://developers.google.com/identity/gsi/web/reference/js-reference#credential) documents a one-hour ID-token lifetime; the app checks issue, not-before when present, and expiry, independently of dashboard session lifetime.
- **Tailnet ACL:** Verify approved tailnet users can reach only the intended endpoint and a disallowed tailnet identity cannot. With Tailscale disconnected/off-tailnet, verify the dashboard cannot be reached. Inspect Serve status and confirm Funnel is not enabled.
- Before login, `curl -si "$ORIGIN/"` should return only the sign-in page; `curl -si "$ORIGIN/logo/chase.com"` and requests to `/new`, `/style.css`, `/dashboard.js` should be 401 with `Cache-Control: no-store`. After sign-in test edit/save, view history, and sign out; reload a protected route and ensure it denies access. In HTTPS response headers check session cookie has `__Host-`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no `Domain`; check logout clears it with the same attributes.
- **Pi / systemd / backups:** Verify the units on Pi with `systemd-analyze --user verify`, reboot-test Serve, `systemctl --user` startup and the mounted backup timer, and rehearse a dry-run and encrypted restore with fictional SQLite on the target OS. Confirm rollback steps and recovery key availability.
- **Real Bridge:** Only after the above checks and explicit approval, provision Bridge separately. Verify a real refresh against source balances/dates and repeat once to check for duplicates; the offline tests do not establish a working account connection.

## 7. Rehearsal record: what was installed, what it proved, how to remove it

A fictional-data rehearsal of sections 1.2, 3, 4 and 5 ran on a shared Raspberry Pi (Debian 12 aarch64, Python 3.11.2, SQLite 3.40.1, GnuPG 2.2.40) from planner commit `c1d6f67` (branch `accounts-pi-rehearsal`). Tailscale stayed off, no Google client, recovery key, Bridge credential, backup drive or real data was involved.

**Installed (all removed afterwards):** a system user `acctdash` (UID 112, home `/home/acctdash`, linger on); under it `~/planner` (code from `git archive`), `~/.local/share/planner-accounts-venv` (`cryptography` 48.0.1, `cffi` 2.1.1, `pycparser` 3.12 from `cp311` aarch64 wheels, 41 MB), `~/.config/planner-accounts/{hosted.env,backup.env}` with fictional values, a fictional database from `deploy/seed_fictional.py` (since L24 seed with `--no-transactions`, start the dashboard, then add the sentinel rows with `--add-transactions` to repeat the gating checks: a hosted start refuses a database that already holds them), the two units plus a rehearsal drop-in, and `/tmp/l21-*` (a throwaway GPG home and encrypted archives). **No apt package was installed or upgraded** (everything was already present; blanket `apt install` would have upgraded 17 packages).

**How to remove it (administrator account; narrow sudo only):**

```sh
M='sudo systemctl --user --machine=acctdash@.host'
$M disable --now accounts-backup.timer accounts-dashboard.service
sudo -H -u acctdash sh -c 'export XDG_RUNTIME_DIR=/run/user/$(id -u); for g in /tmp/l21-*/gnupg*; do GNUPGHOME=$g gpgconf --kill all; done; rm -rf /tmp/l21-*'
sudo loginctl disable-linger acctdash
sudo loginctl terminate-user acctdash
sudo userdel -r acctdash              # also removes the matching group
getent passwd acctdash; getent group acctdash; ls -d /home/acctdash /run/user/* /var/lib/systemd/linger/*   # verify
```

Do not run `journalctl --rotate` or `--vacuum`: the journal is shared. A system UID has no per-user journal file; only its lines in the shared `system.journal` remain until normal rotation. Comparing before/after captures (listening ports, `tailscaled` state, hashes of `sshd_config`, NetworkManager config, `/etc/hosts`, the dpkg package list, the login user's units including `emacs-tty`) showed no difference after teardown.

**Findings that shaped the units/runbook:** the dashboard unit must not carry `--bridge` (without `bridge.access` it exits 1 immediately); the backup unit silently skips without the mount (verified: `start` returns 0, `ConditionResult=no`, no archive) and works with a drop-in `[Unit]` / `ConditionPathIsMountPoint=` that clears the condition (rehearsal only; never ship it); `systemd-analyze --user verify` is clean (the inert `After=network-online.target` is accepted); two `test_hosted` Startup tests assumed umask 022 and a Git checkout and were fixed; and transaction rows seeded in the database survive backup and restore (the sentinel row was present in the restored copy), so the rule was to never copy the database file. **Since L24** such a database is refused at hosted start and by backup, and data moves with the scrubbed export (section 2a).

**Not verified by the rehearsal (needs Young or real hardware):** reboot and linger-at-boot, the mount-gated backup timer with a real drive, Tailscale Serve and tailnet ACLs, real Google sign-in and the exact `.ts.net` origin, `__Host-`/`Secure` cookie behaviour over HTTPS, a passphrase-protected recovery key (the throwaway key had no passphrase), and a real Bridge refresh.
