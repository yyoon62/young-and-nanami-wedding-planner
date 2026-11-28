"""Per-account APY for cash and savings accounts (L23 B2).

Only accounts WITH a rate get their own growth; the rest stay in the blended cash / savings pools exactly as
before and are flagged "rate missing" on Accounts and Projections. New monthly savings go to the blended
savings pool (the "new savings" line); a shortfall draws cash-bucket sources first, then savings-bucket
sources, lowest APY first within each bucket (the blended pool counts at its assumed rate).
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from projections import BUCKETS, STALE_DAYS


@dataclass(frozen=True)
class AssetAcct:
    key: int
    name: str
    institution: str
    bucket: str                 # "cash" | "hysa"
    balance: Decimal
    asof: str
    stale: bool
    apy: Decimal | None         # None = rate missing (stays in the blended pool)
    rate_kind: str = "fixed"


def asset_rows(db, view, currency):
    return db.execute("""SELECT id, institution, nickname, kind, balance, asof FROM accounts
        WHERE excluded = 0 AND side = 'asset' AND currency = ? AND (? = 'Household' OR owner IN (?, 'Joint'))
        ORDER BY institution COLLATE NOCASE, nickname COLLATE NOCASE, id""", (currency, view, view)).fetchall()


def make_assets(rows, terms_by_id, section_for, today):
    """-> [AssetAcct] for cash/savings buckets; the caller uses `apy is not None` ones as own-rate accounts."""
    out = []
    for key, institution, nickname, kind, balance, asof in rows:
        bucket = BUCKETS.get(section_for(kind))
        if bucket not in ("cash", "hysa"):
            continue
        terms = terms_by_id.get(key)
        stale = (today - date.fromisoformat(asof)).days > STALE_DAYS
        out.append(AssetAcct(key, nickname, institution, bucket, Decimal(balance), asof, stale,
                             terms.rate if terms else None, terms.rate_kind if terms else "fixed"))
    return out
