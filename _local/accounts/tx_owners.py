"""Per-transaction owner (L26): the Accounts page's Young / Nanami pills, applied to one transaction.

`account_transactions.owner_override` is NULL (follow the account) or Young / Nanami / Joint. The effective
owner is COALESCE(override, account owner). Clicking a pill toggles exactly like the Accounts page (both
active = Joint); a result equal to the account's owner, or "Unspecified", clears the override instead of
storing a no-op. This module has no import from server.py (which imports the transaction modules);
`toggle_person` is a copy, kept equal by test_tx_owner.
"""
PEOPLE = ("Young", "Nanami")
OWNERS = PEOPLE           # what a pill click may name
STORABLE = ("Young", "Nanami", "Joint")


def toggle_person(current, person):
    """Same semantics as server.toggle_person: click toggles a person; both active = Joint."""
    has = set()
    if current in ("Young", "Joint"):
        has.add("Young")
    if current in ("Nanami", "Joint"):
        has.add("Nanami")
    if person in has:
        has.remove(person)
    else:
        has.add(person)
    if has == set(PEOPLE):
        return "Joint"
    return next(iter(has), "Unspecified")


def effective(override, account_owner):
    return override if override in STORABLE else account_owner


def stored_override(account_owner, wanted):
    """What to store so the row ends up as `wanted`: NULL when that is the account's owner or Unspecified."""
    return None if wanted in (account_owner, "Unspecified") or wanted not in STORABLE else wanted


def apply_owner(db, row, person):
    """Pill click (`person` = Young/Nanami) or reset (`person` None) for one row dict carrying
    row_id, owner_override and account_owner. Returns the new effective owner."""
    account_owner = row["account_owner"]
    if person is None:
        new = None
    else:
        new = stored_override(account_owner, toggle_person(effective(row["owner_override"], account_owner), person))
    db.execute("UPDATE account_transactions SET owner_override=? WHERE row_id=?", (new, row["row_id"]))
    return effective(new, account_owner)


def effective_owner_of(db, row_id):
    row = db.execute("""SELECT COALESCE(t.owner_override, a.owner) FROM account_transactions t
                        JOIN accounts a ON a.id=t.account_id WHERE t.row_id=?""", (row_id,)).fetchone()
    return row[0] if row else None
