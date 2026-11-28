"""Transaction categories, the shared cleaned merchant key and read-time category precedence.

Precedence (effective category, never stored on rows): manual row edit > live user
merchant rule > legacy stamp > built-in default (positive payroll = Income) > classifier cache > none. Classifier output lives only in
`classifier_cache`; it is never written onto `account_transactions`.
"""
from decimal import Decimal, InvalidOperation
from tx_transfers import names_card_issuer
import re
import unicodedata

BUILTIN = ("Groceries", "Dining", "Transport", "Housing", "Utilities", "Shopping", "Entertainment",
           "Health", "Travel", "Subscriptions", "Insurance", "Education", "Gifts & Donations",
           "Personal Care", "Fees & Taxes", "Other")
# Money moving between your own accounts is not spending. These are built in (and may be picked by the classifier
# for clearly payment-like text); a transaction whose effective category is one of them is a transfer: it is left out
# of income, spending and the category bar.
TRANSFER_LIKE = ("Card payment", "Loan payment", "Savings & investing", "Transfer")
TRANSFER_EMOJIS = {"Card payment": "↔️", "Loan payment": "🏦", "Savings & investing": "📈", "Transfer": "🔁"}
INCOME = "Income"
# Categories the classifier may never assign: Income changes totals, so only the user (or the
# deterministic payroll default below) can set it. Kept out of BUILTIN so the model's list is unchanged.
NEVER_ASSIGNED = frozenset((INCOME,))
DEFAULT_EMOJI = {
    "Groceries": "🛒", "Dining": "🍽️", "Transport": "🚇", "Housing": "🏠", "Utilities": "💡",
    "Shopping": "🛍️", "Entertainment": "🎬", "Health": "🩺", "Travel": "✈️", "Subscriptions": "📺",
    "Insurance": "🛡️", "Education": "🎓", "Gifts & Donations": "🎁", "Personal Care": "🧴",
    "Fees & Taxes": "🧾", "Other": "📦", INCOME: "💵",
    **TRANSFER_EMOJIS,
}
TRANSFER_EMOJI, UNCATEGORIZED_EMOJI, CUSTOM_EMOJI = "🔄", "🏷️", "📌"
PAYROLL = re.compile(r"\b(?:payroll|direct dep(?:osit)?|dir dep)\b")
RESERVED = frozenset(("uncategorized", "needs review"))
MAX_CATEGORIES = 60
NAME_LIMIT = 48
KEY_LIMIT = 48
MIN_KEY = 3

_PREFIX = re.compile(
    r"^(?:pos(?: debit| purchase)?|debit card purchase|purchase authorized on|checkcard|"
    r"recurring(?: payment)?|(?:sq|tst|sp|pp|paypal)\s*\*|ebay o)\s*")
_URLISH = re.compile(r"https?://\S+|\S+@\S+|www\.\S+")
_SPLIT = re.compile(r"[^\w&'.\-]+|_")


def clean_merchant(raw):
    """The one merchant key shared by rules and the classifier cache; also what is sent.

    NFKC, casefold, strip processor prefixes, drop every token containing a digit (dates,
    store numbers, refs, last-4, phones), collapse whitespace, cap at 48 characters. It does
    NOT remove names of people, employers or places: those remain readable.
    """
    if not isinstance(raw, str):
        return ""
    s = _URLISH.sub(" ", unicodedata.normalize("NFKC", raw).casefold())
    for _ in range(3):
        stripped = _PREFIX.sub("", s.strip())
        if stripped == s.strip():
            break
        s = stripped
    tokens = []
    for token in _SPLIT.split(s):
        token = token.strip(".-'")
        if token and not any(c.isdigit() for c in token) and token.isprintable():
            tokens.append(token)
    key = " ".join(tokens)[:KEY_LIMIT].strip()
    return key if len(key) >= MIN_KEY else ""


def normalize_name(name):
    """Validate a user-typed category name; ValueError with a displayable message."""
    if not isinstance(name, str):
        raise ValueError("Enter a category name.")
    name = " ".join(unicodedata.normalize("NFKC", name).split())
    if not name:
        raise ValueError("Enter a category name.")
    if len(name) > NAME_LIMIT or not name.isprintable():
        raise ValueError("Use a printable category name of up to 48 characters.")
    if name.casefold() in RESERVED:
        raise ValueError("That name is reserved.")
    return name


def install_categories(db):
    """Additive and idempotent; runs inside the caller's migration transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS transaction_categories (
        name TEXT PRIMARY KEY COLLATE NOCASE, builtin INTEGER NOT NULL DEFAULT 0,
        sort INTEGER NOT NULL DEFAULT 0)""")
    db.executemany("INSERT OR IGNORE INTO transaction_categories(name, builtin, sort) VALUES (?,1,?)",
                   ((name, i) for i, name in enumerate(BUILTIN)))
    # Income is built in but never classifier-assigned. A custom "Income" typed before this
    # existed (any casing) is promoted in place; its transactions and rules keep resolving.
    db.execute("INSERT OR IGNORE INTO transaction_categories(name, builtin, sort) VALUES (?,1,?)",
               (INCOME, len(BUILTIN)))
    db.execute("UPDATE transaction_categories SET name=?, builtin=1, sort=? WHERE name=? AND builtin=0",
               (INCOME, len(BUILTIN), INCOME))
    for i, name in enumerate(TRANSFER_LIKE):      # L26: built-in transfer-like categories (an old custom one is promoted)
        db.execute("INSERT OR IGNORE INTO transaction_categories(name, builtin, sort) VALUES (?,1,?)", (name, 100 + i))
        db.execute("UPDATE transaction_categories SET name=?, builtin=1, sort=? WHERE name=? AND builtin=0",
                   (name, 100 + i, name))
    legacy = {r[0] for r in db.execute("""SELECT category FROM account_transactions WHERE category<>''
        UNION SELECT category FROM transaction_merchant_rules WHERE category<>''""")}
    for name in sorted(legacy, key=str.casefold):
        try:
            clean = normalize_name(name)
        except ValueError:
            continue
        db.execute("INSERT OR IGNORE INTO transaction_categories(name, builtin, sort) VALUES (?,0,1000)",
                   (clean,))


def list_categories(db):
    return [r[0] for r in db.execute("""SELECT name FROM transaction_categories
        ORDER BY builtin DESC, CASE WHEN builtin=1 THEN sort END, name COLLATE NOCASE""")]


def builtin_categories(db):
    """Categories the classifier may assign: built in, minus NEVER_ASSIGNED."""
    return [r[0] for r in db.execute(
        "SELECT name FROM transaction_categories WHERE builtin=1 ORDER BY sort") if r[0] not in NEVER_ASSIGNED]


_TRANSFER_FOLDED = frozenset(n.casefold() for n in TRANSFER_LIKE)


def is_transfer_category(name):
    return isinstance(name, str) and name.casefold() in _TRANSFER_FOLDED


def default_emoji(category, transfer=False):
    """Static emoji for every row: its category's, a transfer mark, or a generic one (never model output)."""
    if transfer:
        return TRANSFER_EMOJI
    if not category:
        return UNCATEGORIZED_EMOJI
    return DEFAULT_EMOJI.get(canonical_builtin(category), CUSTOM_EMOJI)


def canonical_builtin(category):
    folded = category.casefold()
    return next((n for n in DEFAULT_EMOJI if n.casefold() == folded), category)


def canonical(db, name):
    row = db.execute("SELECT name FROM transaction_categories WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def add_category(db, name):
    name = normalize_name(name)
    if canonical(db, name) is not None:
        raise ValueError("That category already exists.")
    if db.execute("SELECT count(*) FROM transaction_categories").fetchone()[0] >= MAX_CATEGORIES:
        raise ValueError("Too many categories; reuse an existing one.")
    db.execute("INSERT INTO transaction_categories(name, builtin, sort) VALUES (?,0,1000)", (name,))
    return name


def load_rules(db):
    """Live category rules keyed exactly and by cleaned merchant (account+source+currency)."""
    exact, clean = {}, {}
    for account, source, currency, merchant, category, key in db.execute(
            """SELECT account_id, source, currency, merchant, category, clean_key
               FROM transaction_merchant_rules WHERE category<>'' ORDER BY merchant"""):
        exact[(account, source, currency, merchant)] = category
        key = key if key is not None else clean_merchant(merchant)
        if key:
            clean.setdefault((account, source, currency, key), category)
    return exact, clean


def load_cache(db):
    """{clean_key: (category, status, emoji)}; empty before the classifier tables exist."""
    try:
        return {k: (c, s, e) for k, c, s, e in db.execute(
            "SELECT clean_key, category, status, emoji FROM classifier_cache WHERE status IN ('suggested','review')")}
    except Exception:
        return {}


def _is_payroll(row):
    """Built-in default: a positive payroll or direct-deposit credit is Income (lowest precedence but the cache)."""
    try:
        if not Decimal(row.get("amount", "0")) > 0:
            return False
    except (InvalidOperation, TypeError):
        return False
    text = " ".join(str(row.get(k) or "") for k in ("description", "payee", "merchant"))
    return bool(PAYROLL.search(unicodedata.normalize("NFKC", text).casefold()))


def resolve(row, rules, cache, canon, issuers=()):
    """(effective category, source). `canon` maps casefolded name -> list name. `issuers` are the card issuers of
    the linked card accounts (tx_transfers.card_issuer_terms): a checking debit that names one is a Card payment by
    default (below a manual choice, a rule and a saved stamp; above the classifier)."""
    stored = row["category"]
    if row["category_manual"]:
        return canon.get(stored.casefold(), stored), "manual"
    exact, clean = rules
    base = (row["account_id"], row["source"], row["currency"])
    key = clean_merchant(row["merchant"])
    found = exact.get((*base, row["merchant"])) or (clean.get((*base, key)) if key else None)
    if found:
        return canon.get(found.casefold(), found), "rule"
    if stored:
        return canon.get(stored.casefold(), stored), "stamp"
    if _is_payroll(row):
        return canon.get(INCOME.casefold(), INCOME), "default"
    if issuers and names_card_issuer(row, issuers):
        return canon.get("card payment", "Card payment"), "default"
    hit = cache.get(key) if key else None
    if hit:
        category, status = hit[0], hit[1]
        if status == "suggested" and category:
            return category, "suggested"
        return "", "review"
    return "", "none"
