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

## Set up syncing (about 5 minutes)

1. Create a new Google Sheet (any name, for example "Wedding votes").
2. In the sheet: **Extensions > Apps Script**. Delete the sample code and paste in `apps-script/Code.gs`.
3. Change `SHARED_KEY` at the top to a random string, and use the same string in `config.js`.
4. **Deploy > New deployment > Web app**. Set *Execute as*: **Me**, *Who has access*: **Anyone**. Authorize when asked.
5. Copy the web app URL (ends in `/exec`) into `APPS_SCRIPT_URL` in `config.js`.
6. Reload the app. The badge at the top should say "synced". Votes appear as rows on a "Votes" tab in the sheet.

Both of you open the same site and pick your name once. Votes refresh every 30 seconds.

If you change `Code.gs` later, use **Deploy > Manage deployments > Edit > New version**, or the URL keeps running the old code.

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
