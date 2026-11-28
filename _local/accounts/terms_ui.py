"""Accounts-page pieces for per-account terms: drawer fields and the "rate missing" flags."""
from html import escape

from account_terms import VARIABLE_NOTE, is_paid_in_full

RATE_SECTIONS = ("Checking", "Savings", "Other")   # asset sections whose APY is modelled (cash and savings)


def flags(row, terms, section_for):
    """Short labels for the account row: what is unknown or assumed. Excluded accounts get none."""
    if row["excluded"]:
        return []
    kind, side = row["kind"], row["side"]
    out = []
    if side == "debt":
        if is_paid_in_full(terms, kind, side, section_for):
            return [("paid in full", "Paid in full each month: no interest or payoff is modelled.", False)]
        if terms is None or terms.rate is None:
            out.append(("rate missing", "No rate: Projections falls back to the blended debt assumption.", True))
        if terms is None or terms.payment is None:
            out.append(("payment missing", "No planned or minimum payment: Projections uses the blended payment target.", True))
    elif section_for(kind) == "Savings" and (terms is None or terms.rate is None):
        out.append(("rate missing", "No APY: Projections uses the blended savings growth assumption.", True))
    return out


def flag_html(row, terms, section_for):
    return "".join(f' <span class="{"rate-flag" if warn else "terms-pill"}" title="{escape(title)}">{escape(label)}</span>'
                   for label, title, warn in flags(row, terms, section_for))


def _values(row, terms, posted):
    """Field values: what was just posted (drawer reopened with an error) else what is stored."""
    if posted:
        return {k: posted.get(k, "") for k in ("t_rate", "t_kind", "t_promo_rate", "t_promo_end", "t_min", "t_planned")} | {
            "t_pif": posted.get("t_pif") == "1"}
    shown = lambda value: "" if value is None else format(value, "f")
    t = terms
    return dict(t_rate=shown(t and t.rate), t_kind=(t.rate_kind if t else "fixed"), t_promo_rate=shown(t and t.promo_rate),
                t_promo_end=(t.promo_end if t else ""), t_min=shown(t and t.min_payment),
                t_planned=shown(t and t.planned_payment), t_pif=None)


def drawer_fields(row, terms, section_for, posted=None, side=None, currency=None):
    """The terms block inside the account drawer; shows debt or cash fields by the account's current side."""
    side = side or row["side"]
    v = _values(row, terms, posted)
    if side == "debt" and v["t_pif"] is None:
        v["t_pif"] = is_paid_in_full(terms, row["kind"], side, section_for)
    ident = f'terms-{row["id"]}'
    cur = escape(currency or row["currency"])
    kinds = "".join(f'<option value="{k}"{" selected" if v["t_kind"] == k else ""}>{label}</option>'
                    for k, label in (("fixed", "Fixed"), ("variable", "Variable")))

    def field(name, label, extra="", kind="text"):
        mode = ' inputmode="decimal"' if kind == "text" else ""
        return (f'<label>{label}<input type="{kind}" name="{name}"{mode} value="{escape(str(v[name]))}"{extra}></label>')

    if side == "debt":
        body = (
            '<input type="hidden" name="t_debt" value="1">'
            f'<label class="check"><input type="checkbox" name="t_pif" value="1"{" checked" if v["t_pif"] else ""}> '
            'Paid in full every month (no interest, no payoff modelled)</label>'
            '<div class="drawer-grid">'
            + field("t_rate", "Interest rate, APR %", ' placeholder="e.g. 6.500"')
            + f'<label>Rate type<select name="t_kind">{kinds}</select></label>'
            + field("t_promo_rate", "Promotional rate % (optional)", ' placeholder="e.g. 0"')
            + field("t_promo_end", "Promo ends (last month at that rate)", ' placeholder="YYYY-MM"', "month")
            + field("t_min", f"Minimum payment ({cur})")
            + field("t_planned", f"Planned payment ({cur}, blank = minimum)")
            + '</div>'
            f'<p class="acct-sub">{escape(VARIABLE_NOTE)} Credit cards default to paid in full. '
            'With no rate, Projections uses its blended assumption and flags the account.</p>')
    elif section_for(row["kind"]) in RATE_SECTIONS:
        body = ('<div class="drawer-grid">' + field("t_rate", "Interest rate, APY %", ' placeholder="e.g. 4.000"')
                + f'<label>Rate type<select name="t_kind">{kinds}</select></label></div>'
                f'<p class="acct-sub">{escape(VARIABLE_NOTE)} Blank uses the blended assumption in Projections.</p>')
    else:
        body = '<p class="acct-sub">Rates are not modelled for this account type.</p>'
    return (f'<fieldset class="terms-fields" id="{ident}"><legend>Rate and payment terms</legend>'
            f'<input type="hidden" name="terms" value="1">{body}</fieldset>')
