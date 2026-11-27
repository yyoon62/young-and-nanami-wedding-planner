# Wedding venues app

A static web app (no build step) for looking through venue photos together and
voting Love / Maybe / Pass. Votes and notes sync through a Google Sheet.

```
index.html  styles.css  app.js     the app
config.js                          Apps Script URL + shared key
data/venues.json                   venue facts (edit this to add venues)
photos/<slug>/*.jpg                photos
apps-script/Code.gs                backend that stores votes in a Google Sheet
```

Test locally: `python3 -m http.server 8000` in this folder, then open http://localhost:8000.
Without a sync URL, votes are saved only in that browser.

## Local financial accounts dashboard

Run `python3 _local/accounts/server.py` (Python 3.9+), then open
<http://127.0.0.1:8765/>. Enter checking, investment, retirement, loan,
credit card or custom accounts manually. Use the statement balance and its
as-of date. For investments or crypto enter the account's value in its
valuation currency, not a share or token count. Totals show assets, debts and
net worth by currency without applying exchange rates.

Balances are kept in `~/.local/share/planner-accounts/accounts.sqlite3`, outside
this repo. Stop with Ctrl-C; rerun the command to reopen the same database.
Use `--db /path/to/another.sqlite3` for a separate dataset. This local app
uses no third-party scripts, sign-in, sync or bank connections.

**Federal loan preview:** From the dashboard choose **Preview federal My Aid Data**,
open the StudentAid.gov → My Aid → Download My Aid Data plain-text export, then
choose a loan by its order in that file. The browser reads it locally (8 MiB
limit); there is no upload, account matching, payoff calculation, or save step.
Cancel to discard the preview. A missing value is not zero. The export may
cover several federal servicers; it is not a private-loan or live Nelnet connection.

Layout reference: Federal Student Aid, [NSLDS Download My Aid Data File Layout,
Oct. 31, 2022](https://fsapartners.ed.gov/sites/default/files/2023-02/DownloadMyAidDataFileLayout.pdf),
p. 4 (ordered `name:value` lines and repeating subsections), pp. 7–9
(Student totals, Loan Type Total vs. Loan), Appendix A p. 18 (sample labels).
`Type Code` starts a **total**, not a loan. `Loan Type Code` starts each **loan**;
the following description and award ID are not unique selection keys. Loan
positions 14–17 are `Loan Outstanding Principal Balance`,
`Loan Outstanding Principal Balance as of Date`, `Loan Outstanding Interest Balance`,
and `Loan Outstanding Interest Balance as of Date`. Each date belongs only to
its named amount in the same block. The parser stops accepting balance fields
at any other subsection (e.g. Loan Status, Loan Contact, Grant); only a new
loan start reopens the block. Repeated/out-of-order recognized fields stop the
preview rather than overwriting or filling another loan. Unrecognized layouts
may therefore be rejected. No personal-field values are needed to select a loan.

Verification: `python3 -m unittest discover -s _local/accounts -v` uses temporary
databases. Optional `python3 _local/accounts/browser_preview.py -v` requires
Playwright for Python and installed Chrome; it runs headless with an isolated
browser context, a temporary database and generated fictional text, not your export.

## Set up syncing (about 5 minutes)

1. Create a new Google Sheet (any name, for example "Wedding votes").
2. In the sheet: **Extensions > Apps Script**. Delete the sample code and paste in `apps-script/Code.gs`.
3. Change `SHARED_KEY` at the top to a random string, and use the same string in `config.js`.
4. **Deploy > New deployment > Web app**. Set *Execute as*: **Me**, *Who has access*: **Anyone**. Authorize when asked.
5. Copy the web app URL (ends in `/exec`) into `APPS_SCRIPT_URL` in `config.js`.
6. Reload the app. The badge at the top should say "synced". Votes appear as rows on a "Votes" tab in the sheet.

Both of you open the same site and pick your name once. Votes refresh every 30 seconds.

If you change `Code.gs` later, use **Deploy > Manage deployments > Edit > New version**, or the URL keeps running the old code.

## Requesting new venues

Either of you can open the **Request a venue** tab, enter a venue name (plus an optional
link and note) and send it. Requests are saved to a "Requests" tab in the Google Sheet
and show up for both of you with a status: new, researching, added or declined.

To process requests, ask Claude to "check venue requests". Claude reads the list from the
sync URL in `config.js`, researches pricing and photos, adds the venue to `data/venues.json`,
and marks the request as added. You can also change the Status column in the sheet by hand.

If you updated `Code.gs` after the first deployment, redeploy it as a new version
(Deploy > Manage deployments > Edit > New version) so the request feature is live.

## Hosting: read this before publishing

**GitHub Pages sites are public on the internet even when the repo is private** (only
GitHub Enterprise Cloud can restrict a Pages site). Anyone with the URL could see the
photos, price estimates and notes, and could find the sync URL and key in `config.js`.

| Option | Cost | Private? |
|---|---|---|
| Cloudflare Pages + Cloudflare Access (email login for you two) | Free | Yes |
| GitHub Pages | Free (private repos need a paid plan) | No, site is public |
| Open `index.html` locally with a local server | Free | Yes, but only on your machine |

Recommended: keep the code in a private GitHub repo and deploy it with Cloudflare Pages,
then add a Cloudflare Access policy that allows only your two email addresses.

The app also sets `noindex` so search engines are asked to skip it, but that is not security.

## Editing

- Add or change venues in `data/venues.json` (fields: slug, name, location, area, type, siteFee, estimate, note, links, photos).
- Put photos in `photos/<slug>/` and list them in the venue's `photos` array.
- Photos come from venue websites and Here Comes The Guide. Keep the repo private; they are for personal planning only.

## Tip: links that pick your name

Open the site with `?as=Young` or `?as=Nanami` at the end of the URL
(for example `https://your-site.example/?as=Nanami`) to skip the "Who are you?" prompt.
