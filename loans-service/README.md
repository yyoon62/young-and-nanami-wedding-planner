# Two-member manual accounts service

Host this FastAPI service on its own HTTPS origin. GitHub Pages serves the
public landing page, not this service or its data. No lender connection, live
balance, import or migration. Each record is a signed decimal amount, currency
and observed calendar date. A name can occur twice; replaying the same create
submission cannot insert it twice. No conversion or mixed-currency total.

### Verify locally (no Google account, cloud or existing database)

From `loans-service/` with Python 3.11+:

```sh
T=$(mktemp -d); python3 -m venv "$T/venv"; . "$T/venv/bin/activate"
pip install -r requirements-test.txt
PYTHONDONTWRITEBYTECODE=1 python -m unittest -v
# Optional: CHROMIUM=/absolute/path/to/an/installed/chromium-or-chrome
pip install playwright
CHROMIUM="$CHROMIUM" PYTHONDONTWRITEBYTECODE=1 python smoke_ui.py -v
```

Dependency installation needs a package source. Tests themselves use a temporary
private SQLite DB and ephemeral signing key; Google's ID-token library verifies
locally signed tokens against a substituted cert transport. The browser smoke
test routes only the fictional dashboard origin to ASGI and blocks external
requests; it exercises the credential callback, **not** Google's sign-in UI.

### Service configuration (future operator)

* `GOOGLE_CLIENT_ID`: Google's Web application client ID; register the exact
  service origin as an authorized JavaScript origin.
* `LOANS_MEMBERS`: exactly two different primary Gmail/Workspace emails,
  comma-separated. The first verified login pins email ↔ Google `sub` in SQLite.
  No silent rebinding after an email reassignment. A genuine identity change
  requires a reviewed recovery procedure. Restart after configuration changes.
* `LOANS_ORIGIN`: exact browser origin, e.g. `https://accounts.example.test`,
  no trailing slash. Only explicit loopback origins may use HTTP for dev.
* `LOANS_SESSION_KEY`: persistent random 32-byte hex key from a secret store
  (`openssl rand -hex 32`). `LOANS_DB`: absolute SQLite path on private durable
  local disk **outside Git**, in a dedicated mode-0700 directory. Keep all real
  configuration and backups outside repositories.

Install `requirements.txt`. For a same-host TLS proxy run
`uvicorn main:app --host 127.0.0.1 --no-proxy-headers` (single worker).
Accept only the configured HTTPS host at the proxy. No direct public access
to Uvicorn. The app does not infer scheme or trusted origin from `Host` or
forwarded headers: cookie Secure and Origin checks use configuration.

### Access rules

* Google's library verifies signature, issuer, client audience and token time;
  require boolean verified email and Gmail or managed Workspace. Exchange each
  credential once. The one-hour signed, opaque, host-only HttpOnly/Lax cookie
  (`__Host-` and Secure on HTTPS) also needs an unexpired SQLite session and
  current membership on every read/write. Logout deletes that server session;
  a copied cookie stops working. No bearer tokens in browser storage.
* **Every mutation**, including login/logout, requires exact `Origin` and
  `X-Dashboard-Action: 1`. No CORS opt-in. A sibling origin is same-site but
  not same-origin; Lax alone would not protect it.
* Create takes neither owner nor sharing state; amounts are stored as integer
  cents for the four supported two-decimal currencies. SQL reads filter on
  owner sub or explicit recipient; every update/delete/share filters on owner
  **and** ID, in the session-check transaction. Sharing grants no write right.
  Unshare removes the row from the recipient's next read, not from memories or
  screenshots. Membership changes drop obsolete sessions and shares on startup.

### Hosting decision & release gates

| Option | Persistence and cost |
| --- | --- |
| Pi + Tailscale HTTPS | Best fit **if both members accept tailnet access** and a compatible HTTPS origin can be registered with Google. Local disk suits SQLite. Budget power, reliable disk, off-device backups and operator time. |
| Pi + Cloudflare Tunnel | Outbound tunnel, no inbound router port; TLS at the edge. Public hostname needs stronger ingress monitoring/rate limits. Budget domain, power and plan-dependent fees. |
| Cloud Run | Managed HTTPS and scale-to-zero; compute may be inexpensive at household volume, but database, backups and egress can cost more. Container disk is ephemeral even at one instance. **Not deployable as-is:** move to supported durable transactional storage (e.g. Cloud SQL) and retest concurrency/ownership. Do not put SQLite on an object-store mount. |

Before deployment: choose host and exact origin; verify TLS/proxy isolation,
body/rate limits, secret permissions, clock and private logging. Use SQLite's
online backup API (or stop service and snapshot the database), encrypt backups
off-device and test restoration in isolation. A restore can resurrect old
shares/sessions: rotate the signing key, clear sessions and sharing grants,
reconcile membership/deletions and require fresh sharing before serving traffic.
Then validate actual GIS sign-in for both members, stranger rejection, browser
logout and persistence across the chosen host's restart. No host is provisioned.

### Lender integration contract (deferred)

Sign-in does not connect lenders. Account discovery must be private to the
person linking the lender. Show individual loans unchecked; publish only the
IDs the owner explicitly confirms via **Share with both of us**. Verify
ownership on the server at confirmation. Return only shared lender names,
balances, currencies and refresh timestamps to the household. Keep lender
tokens server-side and support unsharing. A SimpleFIN setup token and the
resulting Access URL are credentials: redeem only server-side, never log,
commit, return or embed either in the browser; encrypt per-owner credentials
with a separate managed key and plan revocation/rotation before implementing.
