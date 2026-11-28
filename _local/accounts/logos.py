"""Local, zero-network SVG badges for financial institutions and loan servicers."""
import hashlib
from html import escape
import re


# (keywords, background, foreground, label_or_custom_svg_body)
_BRANDS = (
    (("bank of america", "bofa"), "#f4f6fa", "#012169",
     '<rect width="28" height="28" rx="7" fill="#f4f6fa" stroke="#d9e0ec"/>'
     '<path d="M5 13.5 14 9l9 4.5-3 .9-6-3-6 3z" fill="#012169"/>'
     '<path d="M6.5 17 14 13.2 21.5 17l-2.6 1L14 15.5 9.1 18z" fill="#e31837"/>'
     '<path d="M9 20.2 14 17.8l5 2.4-2 .8-3-1.4-3 1.4z" fill="#e31837"/>'),
    (("chase", "jpmorgan"), "#117aca", "#fff",
     '<rect width="28" height="28" rx="7" fill="#117aca"/>'
     '<path d="M11 6h6l5 5v6l-5 5h-6l-5-5v-6z" fill="none" stroke="#fff" stroke-width="2.4"/>'
     '<rect x="11.5" y="11.5" width="5" height="5" rx="1" fill="#fff"/>'),
    (("fidelity",), "#167a3e", "#fff",
     '<rect width="28" height="28" rx="7" fill="#167a3e"/>'
     '<circle cx="14" cy="14" r="7" fill="none" stroke="#b9e2c6" stroke-width="1.5"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="Georgia,serif" font-size="13" font-weight="700" fill="#fff">F</text>'),
    (("vanguard",), "#96151d", "#fff",
     '<rect width="28" height="28" rx="7" fill="#96151d"/>'
     '<text x="14" y="18.5" text-anchor="middle" font-family="Georgia,serif" font-size="14" font-weight="700" fill="#fff">V</text>'),
    (("schwab",), "#00a0df", "#fff",
     '<rect width="28" height="28" rx="7" fill="#00a0df"/>'
     '<text x="14" y="17.8" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10.5" font-weight="700" fill="#fff">CS</text>'),
    (("capital one",), "#004879", "#fff",
     '<rect width="28" height="28" rx="7" fill="#004879"/>'
     '<path d="M5 18c6-5 13-6 18-3" fill="none" stroke="#d03027" stroke-width="2.5" stroke-linecap="round"/>'
     '<text x="14" y="15.5" text-anchor="middle" font-family="system-ui,sans-serif" font-size="9.5" font-weight="700" fill="#fff">C1</text>'),
    (("citibank", "citi"), "#003b70", "#fff",
     '<rect width="28" height="28" rx="7" fill="#003b70"/>'
     '<path d="M9 11.5a6.5 6.5 0 0 1 10 0" fill="none" stroke="#e41c23" stroke-width="2.2" stroke-linecap="round"/>'
     '<text x="14" y="20" text-anchor="middle" font-family="system-ui,sans-serif" font-size="9.5" font-weight="700" fill="#fff">citi</text>'),
    (("wells fargo",), "#d71e28", "#fff",
     '<rect width="28" height="28" rx="7" fill="#d71e28"/>'
     '<rect x="1.5" y="1.5" width="25" height="25" rx="5.5" fill="none" stroke="#ffcd41" stroke-width="1.5"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="Georgia,serif" font-size="11" font-weight="700" fill="#fff">WF</text>'),
    (("american express", "amex"), "#016fd0", "#fff",
     '<rect width="28" height="28" rx="7" fill="#016fd0"/>'
     '<text x="14" y="17.2" text-anchor="middle" font-family="system-ui,sans-serif" font-size="7.5" font-weight="800" letter-spacing=".4" fill="#fff">AMEX</text>'),
    (("discover",), "#23233f", "#fff",
     '<rect width="28" height="28" rx="7" fill="#23233f"/>'
     '<circle cx="19.5" cy="18.5" r="3.5" fill="#f47216"/>'
     '<text x="12" y="18" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" font-weight="700" fill="#fff">D</text>'),
    (("sofi",), "#00a2c7", "#fff",
     '<rect width="28" height="28" rx="7" fill="#00a2c7"/>'
     '<text x="14" y="17.6" text-anchor="middle" font-family="system-ui,sans-serif" font-size="9.5" font-weight="700" fill="#fff">SoFi</text>'),
    (("ally",), "#5b1f69", "#fff",
     '<rect width="28" height="28" rx="7" fill="#5b1f69"/>'
     '<text x="14" y="17.6" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10" font-weight="700" fill="#fff">ally</text>'),
    (("marcus", "goldman"), "#23364d", "#fff",
     '<rect width="28" height="28" rx="7" fill="#23364d"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="Georgia,serif" font-size="12.5" font-weight="700" fill="#fff">M</text>'),
    (("u.s. bank", "us bank"), "#0c2074", "#fff",
     '<rect width="28" height="28" rx="7" fill="#0c2074"/>'
     '<path d="M4 22h20" stroke="#cf2a36" stroke-width="2.5"/>'
     '<text x="14" y="17" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10.5" font-weight="700" fill="#fff">US</text>'),
    (("pnc",), "#ef6c00", "#fff",
     '<rect width="28" height="28" rx="7" fill="#ef6c00"/>'
     '<text x="14" y="17.5" text-anchor="middle" font-family="system-ui,sans-serif" font-size="9" font-weight="800" fill="#fff">PNC</text>'),
    (("td bank",), "#008a00", "#fff",
     '<rect width="28" height="28" rx="7" fill="#008a00"/>'
     '<text x="14" y="17.8" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10.5" font-weight="800" fill="#fff">TD</text>'),
    (("nelnet",), "#1f6f4a", "#fff",
     '<rect width="28" height="28" rx="7" fill="#1f6f4a"/>'
     '<text x="14" y="18.2" text-anchor="middle" font-family="system-ui,sans-serif" font-size="13" font-weight="800" fill="#fff">N</text>'),
    (("aidvantage",), "#2b4b8c", "#fff",
     '<rect width="28" height="28" rx="7" fill="#2b4b8c"/>'
     '<text x="14" y="17.8" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10.5" font-weight="700" fill="#fff">Av</text>'),
    (("heartland", "ecsi"), "#1d4e89", "#fff",
     '<rect width="28" height="28" rx="7" fill="#1d4e89"/>'
     '<text x="14" y="17.8" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10" font-weight="700" fill="#fff">EC</text>'),
    (("uas", "university accounting"), "#3d5a80", "#fff",
     '<rect width="28" height="28" rx="7" fill="#3d5a80"/>'
     '<text x="14" y="17.5" text-anchor="middle" font-family="system-ui,sans-serif" font-size="9.5" font-weight="700" fill="#fff">UAS</text>'),
    (("mohela",), "#0f5c6e", "#fff",
     '<rect width="28" height="28" rx="7" fill="#0f5c6e"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" font-weight="700" fill="#fff">M</text>'),
    (("robinhood",), "#00c805", "#0b2e13",
     '<rect width="28" height="28" rx="7" fill="#00c805"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12.5" font-weight="800" fill="#0b2e13">R</text>'),
    (("wealthfront",), "#4840bb", "#fff",
     '<rect width="28" height="28" rx="7" fill="#4840bb"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11.5" font-weight="700" fill="#fff">W</text>'),
    (("betterment",), "#143c7a", "#fff",
     '<rect width="28" height="28" rx="7" fill="#143c7a"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" font-weight="700" fill="#ffd23f">B</text>'),
    (("coinbase",), "#0052ff", "#fff",
     '<rect width="28" height="28" rx="7" fill="#0052ff"/>'
     '<path d="M18.5 11.2a5.5 5.5 0 1 0 0 5.6" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/>'),
    (("paypal",), "#003087", "#fff",
     '<rect width="28" height="28" rx="7" fill="#003087"/>'
     '<text x="14" y="17.8" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10.5" font-weight="800" fill="#fff">PP</text>'),
    (("venmo",), "#008cff", "#fff",
     '<rect width="28" height="28" rx="7" fill="#008cff"/>'
     '<text x="14" y="18" text-anchor="middle" font-family="Georgia,serif" font-size="13" font-weight="700" fill="#fff">V</text>'),
)

_FALLBACK_PALETTE = (
    ("#e9eee4", "#3f5d42"),
    ("#f7e6e8", "#8e4351"),
    ("#e6eef6", "#27537a"),
    ("#f6ecd9", "#7a5a1e"),
    ("#ece8f5", "#4c3e7c"),
    ("#e4f0ef", "#245c5a"),
)


def initials(institution):
    words = [w for w in re.findall(r"[A-Za-z0-9]+", institution or "") if w.lower() not in {"the", "of", "and"}]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].upper()
    return "?"


def institution_logo(institution):
    low = (institution or "").lower()
    for keys, _, _, body in _BRANDS:
        if any(k in low for k in keys):
            return f'<svg class="inst-logo" viewBox="0 0 28 28" width="28" height="28" aria-hidden="true">{body}</svg>'
    digest = hashlib.sha256(low.encode()).digest()[0]
    bg, fg = _FALLBACK_PALETTE[digest % len(_FALLBACK_PALETTE)]
    abbr = escape(initials(institution))
    return (f'<svg class="inst-logo" viewBox="0 0 28 28" width="28" height="28" aria-hidden="true">'
            f'<rect width="28" height="28" rx="7" fill="{bg}"/>'
            f'<text x="14" y="17.6" text-anchor="middle" font-family="system-ui,sans-serif"'
            f' font-size="10.5" font-weight="700" fill="{fg}">{abbr}</text></svg>')
