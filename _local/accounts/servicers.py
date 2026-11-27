"""Local statement shortcuts. No file, database, network, or POST handling."""

_PROVIDERS = {
    # “Managing Your Loans”, https://images.aidvantage.studentaid.gov/new-to-aidvantage/
    "aidvantage": ("Aidvantage", "Use Current Balance, not Monthly Payment Amount."),
    # “Who is University Accounting Service?” and “I didn’t receive my billing statement.”
    # https://www.uasconnect.com/faq
    "uas": ("UAS", "UAS Connect has billing statements and account information. "
            "UAS bills both federal and private loans."),
    # https://heartland.ecsi.net/tpl/tpl_account-overview.html (public, unpopulated template)
    "heartland-ecsi": ("Heartland ECSI", "Look under Billing &amp; Payments for statements. "
                      "Neither Amount Due nor Payoff Amount for All Loan(s) is a single-loan balance."),
}


def guide():
    choices = "".join(
        f'<li><a href="/new?servicer={slug}">{name}</a> — {hint}</li>'
        for slug, (name, hint) in _PROVIDERS.items()
    )
    return f"""<a href="/">← Accounts</a><h2>Record a student loan from a statement</h2>
<p>Find the outstanding balance for one loan and the date that balance is as of.
Don't use a payment due date, a payoff quote, or a total for several loans.
If the balance or its date is unclear, check the loan detail before saving.</p>
<h3>Already recorded this loan?</h3>
<p><a href="/">Find the loan in Your accounts</a> and choose Edit. Match the account name,
not just the servicer: several loans can have the same servicer, and a loan can change
servicers. Update its balance and as-of date together rather than adding it again.</p>
<h3>First snapshot for this loan</h3>
<p>Choose the servicer on your statement. Enter a distinct nickname (not an account number),
currency, balance and as-of date. Nothing is saved until you choose Save account.</p>
<ul>{choices}</ul>
<h3>Have federal My Aid Data instead?</h3>
<p>Only if you've confirmed this specific loan is included,
<a href="/federal-preview">preview the federal snapshot</a>. The preview is read-only;
it does not fill or save an account. If coverage is unknown, use the statement route above.</p>"""


def servicer_get(target, *, page, form, token):
    """After the GET Host check: return (status, HTML) or None for baseline routes.

    Pass the original request target, not a decoded query dict (which can lose
    repeated keys). Bare /new, edits, removals and the preview stay unchanged.
    """
    if target == "/servicers":
        return 200, page(guide())
    path, sep, query = target.partition("?")
    if path != "/new" or not sep:
        return None
    # The three links above are the entire preset vocabulary. No request text
    # becomes a field value, form action, or error message.
    for slug, (name, _) in _PROVIDERS.items():
        if query == f"servicer={slug}":
            return 200, form({"institution": name, "kind": "Student loan", "side": "debt"},
                             "/new", token)
    return 400, page('<h2>Unrecognized account shortcut</h2>'
                     '<p><a href="/servicers">Choose a servicer</a> or '
                     '<a href="/new">open the ordinary account form</a>.</p>')
