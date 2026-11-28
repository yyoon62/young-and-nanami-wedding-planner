"""Detect a checking/savings debit that pays a linked credit card (L26 part 9).

A debit counts when its text names the ISSUER of a linked card account (the card's institution, or a short alias
such as "amex") AND carries a payment hint ("payment", "autopay", "epay", "credit card"...). It becomes the built-in
category "Card payment" by default; a manual category, a merchant rule or a manual "not a transfer" choice beats it.
Zelle and other person-to-person text is never touched here (the classifier never sees it either).

Counterexamples this guards: an institution that is both your bank and your card issuer (a bare "CHASE" fee on the
checking account is not a card payment: it needs a card word); a merchant that merely shares a word with an issuer
(no payment hint); generic words such as "bank" or "credit" naming nobody.
"""
from decimal import Decimal
import re

BANK_KINDS = ("checking", "savings", "saving")
CARD_KINDS = ("credit card", "credit cards")
GENERIC = frozenset(("bank", "banks", "credit", "card", "cards", "union", "financial", "trust", "services", "national",
                     "federal", "savings", "capital", "group", "holdings", "company", "usa", "the", "of", "and"))
ALIASES = {"american express": ("amex",), "citibank": ("citi",), "bank of america": ("bofa", "boa"),
           "jpmorgan chase": ("chase",), "synchrony": ("synchrony", "syf")}
PAYMENT_HINT = re.compile(r"\b(?:pay\w*|pmt|epay\w*|autopay|crcardpmt|crd|card|credit|bill|ach)\b")
CARD_WORD = re.compile(r"\b(?:card|crd|credit|cc|crcardpmt|visa|mastercard)\b")
_NON = re.compile(r"[^a-z0-9]+")


def norm(text):
    return " ".join(_NON.sub(" ", str(text or "").casefold()).split())


def card_issuer_terms(db):
    """[(term, issuer institution normalized)] for every included credit-card account."""
    terms, seen = [], set()
    for (institution,) in db.execute("""SELECT DISTINCT institution FROM accounts
                                         WHERE excluded=0 AND lower(kind) IN ('credit card','credit cards')"""):
        phrase = norm(institution)
        if not phrase:
            continue
        found = {phrase, *(w for w in phrase.split() if len(w) >= 4 and w not in GENERIC)}
        for name, extra in ALIASES.items():
            if name in phrase:
                found.update(extra)
        for term in found:
            if (term, phrase) not in seen:
                seen.add((term, phrase))
                terms.append((term, phrase))
    return tuple(terms)


def names_card_issuer(row, terms):
    """True for a posted checking/savings debit whose description/payee names a card issuer plus a payment hint."""
    try:
        if row.get("pending") or str(row.get("kind", "")).lower() not in BANK_KINDS or Decimal(row["amount"]) >= 0:
            return False
    except Exception:
        return False
    text = norm(f'{row.get("description", "")} {row.get("payee", "")}')
    if not text:
        return False
    own = norm(row.get("institution", ""))
    for term, issuer in terms:
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text):
            hint = CARD_WORD if issuer == own else PAYMENT_HINT
            if hint.search(re.sub(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", " ", text)):
                return True
    return False
