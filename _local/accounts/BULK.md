# One-click balance sync (local only)

**Try it without an account.** From `_local/accounts`, run `python3 server.py --demo`. Open the printed loopback address and click **↻ Sync balances** right on the Accounts page. Demo mode uses a disposable database with a seeded manual card and 12 fictional source accounts; it never reads Bridge credentials or touches the normal database.

- **No batch review wizard.** Clicking **↻ Sync balances** fetches balances from SimpleFIN Bridge and immediately creates new rows or updates linked ones in place.
- **Automatic sign detection.** Negative balances import as **Debt**; positive or zero balances import as **Asset**. Click the **Asset / Debt** pill on any row to override; future syncs preserve your override.
- **Inline owner tagging.** Click **Young**, **Nanami**, or **Joint** right on any row (click the active pill again to clear to Unspecified).
- **Exclude without deleting.** Click **Exclude** on any row to remove it from totals and move it into the collapsed **Excluded accounts** section. Excluded accounts stay excluded on future syncs; click **Include** to restore.

### Optional SimpleFIN Bridge setup

Run `python3 bridge.py setup` in a private terminal and paste your one-time Setup Token at the hidden prompt. It saves the Access URL unencrypted in `~/.local/share/planner-accounts/bridge.access` (`0600` inside a `0700` directory outside Git). Then start the dashboard with `python3 server.py --bridge`. Opening the page does not fetch; only clicking **↻ Sync balances** makes a balances-only v2 HTTPS request (guarded at 24 attempts per rolling 24 hours).

### Verification (fictional data only)

```sh
cd _local/accounts
python3 -m unittest discover -v
"$TMPDIR/l05-browser-venv/bin/python" -m unittest -v browser_bulk browser_save browser_preview
```
