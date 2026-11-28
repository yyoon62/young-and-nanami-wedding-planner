"""Local cached official institution logos (served from 127.0.0.1, never committed to Git)."""
import hashlib
from html import escape
from http.client import HTTPSConnection
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, urljoin
from urllib.request import Request, urlopen


_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

_KNOWN_DOMAINS = (
    (("bank of america", "bofa"), "bankofamerica.com"),
    (("chase", "jpmorgan"), "chase.com"),
    (("fidelity",), "fidelity.com"),
    (("vanguard",), "vanguard.com"),
    (("schwab",), "schwab.com"),
    (("capital one",), "capitalone.com"),
    (("citibank", "citi"), "citi.com"),
    (("wells fargo",), "wellsfargo.com"),
    (("american express", "amex"), "americanexpress.com"),
    (("discover",), "discover.com"),
    (("sofi",), "sofi.com"),
    (("ally",), "ally.com"),
    (("marcus",), "marcus.com"),
    (("goldman",), "goldmansachs.com"),
    (("u.s. bank", "us bank"), "usbank.com"),
    (("pnc",), "pnc.com"),
    (("td bank",), "td.com"),
    (("nelnet",), "nelnet.studentaid.gov"),
    (("aidvantage",), "aidvantage.studentaid.gov"),
    (("heartland", "ecsi"), "heartland.ecsi.net"),
    (("uas", "university accounting"), "uasconnect.com"),
    (("mohela",), "mohela.studentaid.gov"),
    (("robinhood",), "robinhood.com"),
    (("wealthfront",), "wealthfront.com"),
    (("betterment",), "betterment.com"),
    (("coinbase",), "coinbase.com"),
    (("paypal",), "paypal.com"),
    (("venmo",), "venmo.com"),
    (("empower",), "empower.com"),
    (("e*trade", "etrade"), "etrade.com"),
    (("tiaa",), "tiaa.org"),
    (("principal",), "principal.com"),
    (("healthequity",), "healthequity.com"),
    (("optum",), "optumbank.com"),
    (("navy federal",), "navyfederal.org"),
    (("barclays",), "barclaysus.com"),
    (("synchrony",), "synchrony.com"),
    (("hsbc",), "hsbc.com"),
    (("becu",), "becu.org"),
    (("apple card", "apple"), "apple.com"),
    (("cit bank",), "cit.com"),
)

KNOWN_DOMAIN_SET = {d for _, d in _KNOWN_DOMAINS}

_FALLBACK_PALETTE = (
    ("#e9eee4", "#3f5d42"),
    ("#f7e6e8", "#8e4351"),
    ("#e6eef6", "#27537a"),
    ("#f6ecd9", "#7a5a1e"),
    ("#ece8f5", "#4c3e7c"),
    ("#e4f0ef", "#245c5a"),
)

_IMAGE_TYPES = (
    ("png", b"\x89PNG\r\n\x1a\n", "image/png"),
    ("jpg", b"\xff\xd8\xff", "image/jpeg"),
    ("ico", b"\x00\x00\x01\x00", "image/x-icon"),
    ("webp", b"RIFF", "image/webp"),
)


def sanitize_domain(raw):
    if not isinstance(raw, str):
        return ""
    s = raw.strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = (urlsplit(s).hostname or "").lower()
    else:
        s = s.split("/", 1)[0].split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s if _DOMAIN_RE.fullmatch(s) and len(s) <= 120 else ""


def domain_for(institution, saved_domain=""):
    clean = sanitize_domain(saved_domain)
    if clean:
        return clean
    low = (institution or "").lower()
    for keys, domain in _KNOWN_DOMAINS:
        if any(k in low for k in keys):
            return domain
    return ""


def initials(institution):
    words = [w for w in re.findall(r"[A-Za-z0-9]+", institution or "") if w.lower() not in {"the", "of", "and"}]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].upper()
    return "?"


def fallback_svg(institution):
    low = (institution or "").lower()
    digest = hashlib.sha256(low.encode()).digest()[0]
    bg, fg = _FALLBACK_PALETTE[digest % len(_FALLBACK_PALETTE)]
    abbr = escape(initials(institution))
    return (f'<svg class="inst-logo" viewBox="0 0 28 28" width="28" height="28" aria-hidden="true">'
            f'<rect width="28" height="28" rx="7" fill="{bg}"/>'
            f'<text x="14" y="17.6" text-anchor="middle" font-family="system-ui,sans-serif"'
            f' font-size="10.5" font-weight="700" fill="{fg}">{abbr}</text></svg>')


def detect_image(data):
    if not isinstance(data, (bytes, bytearray)) or not 16 <= len(data) <= 131072:
        return None
    for ext, magic, mime in _IMAGE_TYPES:
        if data.startswith(magic):
            if ext == "webp" and data[8:12] != b"WEBP":
                continue
            return ext, mime
    return None


def _default_fetch(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            return None
        return resp.read(131073)


def hosted_fetch(url):
    """A Bridge-provided domain may select a favicon, never an outbound network target."""
    for _ in range(3):
        u = urlsplit(url)
        if (u.scheme != "https" or not re.fullmatch(r"www\.google\.com|t[0-3]\.gstatic\.com", u.netloc)
                or not u.path.startswith(("/s2/favicons", "/faviconV2"))):
            return None
        conn = HTTPSConnection(u.netloc, timeout=5)
        try:
            conn.request("GET", u.path + ("?" + u.query if u.query else ""))
            response = conn.getresponse()
            if response.status == 200:
                return response.read(131073)
            if response.status not in (301, 302, 303, 307, 308) or not response.getheader("Location"):
                return None
            url = urljoin(url, response.getheader("Location"))
        finally:
            conn.close()
    return None


def cached_logo(logo_dir, domain, fetcher=_default_fetch, *, hosted=False):
    """Return (bytes, content_type) from local cache, fetching once over HTTPS if missing."""
    clean = sanitize_domain(domain)
    if not clean or not logo_dir:
        return None
    base = Path(logo_dir)
    for ext, _, mime in _IMAGE_TYPES:
        p = base / f"{clean}.{ext}"
        if p.is_file() and not p.is_symlink():
            return p.read_bytes(), mime
    if fetcher is None:
        return None
    # Prefer direct favicon for government/specialized portals where Google S2 may 404,
    # otherwise try crisp 64px Google favicon first and fall back to direct /favicon.ico.
    urls = (f"https://www.google.com/s2/favicons?domain={clean}&sz=64",
            f"https://{clean}/favicon.ico")
    for url in urls:
        try:
            raw = fetcher(url)
        except Exception:
            continue
        detected = detect_image(raw)
        if not detected:
            continue
        ext, mime = detected
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if hosted:
            # Simultaneous authenticated requests must not share a staging name.
            with tempfile.NamedTemporaryFile(dir=base, prefix=f".{clean}.", delete=False) as temp:
                tmp = Path(temp.name)
                try:
                    temp.write(raw)
                    temp.close()
                    tmp.replace(base / f"{clean}.{ext}")
                finally:
                    tmp.unlink(missing_ok=True)
        else:
            tmp = base / f".{clean}.{ext}.tmp"
            tmp.write_bytes(raw)
            tmp.chmod(0o600)
            tmp.replace(base / f"{clean}.{ext}")
        return bytes(raw), mime
    return None


def institution_logo(institution, saved_domain="", use_cached=False):
    domain = domain_for(institution, saved_domain) if use_cached else ""
    if domain:
        return (f'<span class="logo-frame">'
                f'<img class="inst-logo-img" src="/logo/{escape(domain)}" width="22" height="22" alt="">'
                f'</span>')
    return fallback_svg(institution)
