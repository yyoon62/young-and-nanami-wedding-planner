"""Per-account rate and payment terms (L23). Exact decimals stored as base-10 text; additive table.

`paid_in_full` is NULL when it follows the account type (credit cards: on; everything else: off), so a
newly synced card is paid in full without a row. An explicit 0/1 is stored only when the user diverges.
Money here is entered per account currency; rates are percentage points (APR for debt, APY for cash).
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date
import re

ZERO = Decimal(0)
RATE_KINDS = ("fixed", "variable")
RATE_PATTERN = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3})?")
MONTH_PATTERN = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
VARIABLE_NOTE = "Variable rate: the projection assumes today's rate holds."


def install_terms(db):
    """Additive and idempotent; runs inside the accounts migration transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS account_terms (
        account_id INTEGER PRIMARY KEY, rate TEXT NOT NULL DEFAULT '',
        rate_kind TEXT NOT NULL DEFAULT 'fixed' CHECK(rate_kind IN ('fixed','variable')),
        promo_rate TEXT NOT NULL DEFAULT '', promo_end TEXT NOT NULL DEFAULT '',
        min_payment TEXT NOT NULL DEFAULT '', planned_payment TEXT NOT NULL DEFAULT '',
        paid_in_full INTEGER CHECK(paid_in_full IN (0,1)))""")
    # Own trigger: the existing per-table delete triggers are left exactly as they are.
    db.execute("""CREATE TRIGGER IF NOT EXISTS account_terms_delete AFTER DELETE ON accounts
        BEGIN DELETE FROM account_terms WHERE account_id=OLD.id; END""")


@dataclass(frozen=True)
class Terms:
    rate: Decimal | None = None
    rate_kind: str = "fixed"
    promo_rate: Decimal | None = None
    promo_end: str = ""                 # YYYY-MM: the last month at the promo rate
    min_payment: Decimal | None = None
    planned_payment: Decimal | None = None
    paid_in_full: bool | None = None    # None = follow the account type

    @property
    def payment(self):
        """Planned payment, else the minimum; None when neither is set (payment missing)."""
        return self.planned_payment if self.planned_payment is not None else self.min_payment


def _dec(text):
    return Decimal(text) if text != "" else None


def load_terms(db):
    """{account_id: Terms}."""
    return {row[0]: Terms(_dec(row[1]), row[2], _dec(row[3]), row[4], _dec(row[5]), _dec(row[6]),
                          None if row[7] is None else bool(row[7]))
            for row in db.execute("""SELECT account_id, rate, rate_kind, promo_rate, promo_end, min_payment,
                                     planned_payment, paid_in_full FROM account_terms""")}


def default_paid_in_full(kind, side, section_for):
    return side == "debt" and section_for(kind) == "Credit cards"


def is_paid_in_full(terms, kind, side, section_for):
    """Effective flag: only debt-side accounts can be paid in full; unset follows the account type."""
    if side != "debt":
        return False
    if terms is not None and terms.paid_in_full is not None:
        return terms.paid_in_full
    return default_paid_in_full(kind, side, section_for)


def _fmt(value):
    return "" if value is None else format(value, "f")


def parse_terms(raw, currency, side, places, section_for, kind):
    """Validate the drawer's terms fields -> dict of stored text columns. ValueError with a message.

    Only fields present in `raw` are returned, so a form that omits a field never clears it.
    """
    out = {}
    if "t_rate" in raw:
        text = raw["t_rate"].strip()
        label = "Interest rate (APR)" if side == "debt" else "Interest rate (APY)"
        if text and not (RATE_PATTERN.fullmatch(text) and Decimal(text) <= 100):
            raise ValueError(f"{label}: enter a percentage from 0 to 100 with up to 3 decimal places, or leave it blank.")
        out["rate"] = _fmt(Decimal(text)) if text else ""
    if "t_kind" in raw:
        if raw["t_kind"] not in RATE_KINDS:
            raise ValueError("Rate type: choose fixed or variable.")
        out["rate_kind"] = raw["t_kind"]
    if side == "debt" and raw.get("t_debt") == "1":   # debt fields are rendered (and so submitted) only for debt
        for key, column, label in (("t_promo_rate", "promo_rate", "Promotional rate"),):
            if key in raw:
                text = raw[key].strip()
                if text and not (RATE_PATTERN.fullmatch(text) and Decimal(text) <= 100):
                    raise ValueError(f"{label}: enter a percentage from 0 to 100 with up to 3 decimal places, or leave it blank.")
                out[column] = _fmt(Decimal(text)) if text else ""
        if "t_promo_end" in raw:
            text = raw["t_promo_end"].strip()
            if text and not MONTH_PATTERN.fullmatch(text):
                raise ValueError("Promotional rate ends: enter a month such as 2027-04, or leave it blank.")
            out["promo_end"] = text
        amount = r"[0-9]{1,15}" + (rf"(?:\.[0-9]{{1,{places}}})?" if places else "")
        for key, column, label in (("t_min", "min_payment", "Minimum payment"), ("t_planned", "planned_payment", "Planned payment")):
            if key in raw:
                text = raw[key].strip()
                if text and not re.fullmatch(amount, text):
                    raise ValueError(f"{label}: enter a nonnegative amount in {currency} (up to {places} decimal places, "
                                     "no commas), or leave it blank.")
                out[column] = "" if not text or Decimal(text) == 0 else format(Decimal(text), "f")
        promo_rate, promo_end = out.get("promo_rate"), out.get("promo_end")
        if (promo_rate is not None or promo_end is not None) and bool(promo_rate) != bool(promo_end):
            raise ValueError("Promotional rate: enter both the promotional rate and the month it ends, or neither.")
        if promo_rate and not out.get("rate", raw.get("t_rate", "").strip()):
            raise ValueError("Promotional rate: also enter the regular rate that applies after the promotion.")
        minimum, planned = out.get("min_payment"), out.get("planned_payment")
        if minimum and planned and Decimal(planned) < Decimal(minimum):
            raise ValueError("Planned payment: this is below the minimum payment; enter at least the minimum.")
        wanted = raw.get("t_pif") == "1"      # an unchecked box is simply absent from the form
        out["paid_in_full"] = None if wanted == default_paid_in_full(kind, side, section_for) else int(wanted)
    return out


def save_terms(db, account_id, values):
    """Upsert only the given columns (caller's transaction); no row is created for an empty change."""
    if not values:
        return
    db.execute("INSERT OR IGNORE INTO account_terms(account_id) VALUES (?)", (account_id,))
    columns = ", ".join(f"{name}=?" for name in values)
    db.execute(f"UPDATE account_terms SET {columns} WHERE account_id=?", (*values.values(), account_id))


def months_between(start_month, end_month):
    """Whole months from start to end ('YYYY-MM'); negative when end is earlier."""
    return (int(end_month[:4]) - int(start_month[:4])) * 12 + int(end_month[5:7]) - int(start_month[5:7])


def current_month(today=None):
    return (today or date.today()).isoformat()[:7]
