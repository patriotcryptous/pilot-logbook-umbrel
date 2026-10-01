# Pilot Logbook for umbrelOS

A private, single-pilot logbook that runs on your own Umbrel. Import your ForeFlight logbook, add flights, file
photos of your paper logbook pages, and keep one backup you control. No accounts, no cloud, no telemetry.

**Features**
- Dashboard: totals, 90-day landing currency per aircraft class (FAR 61.57), flight review and medical reminders
  (FAR 61.56), hours per month, running total, a year of flying days, aircraft, recent flights and progress toward
  private pilot minimums (FAR 61.109(a)). Light and dark themes.
- All 65 ForeFlight columns are stored, editable and exported again. Choose which fields appear on the entry form
  under Settings, as in ForeFlight's Configure Fields, and pick decimal (N.N or N.NN) or H:MM time.
- Document Hangar: private storage for insurance, certificates, POH and manuals, notes, photos and videos, with
  categories, search, optional aircraft link, expiry dates that warn you on the dashboard, and replace-with-newer-copy
  for renewals. Only allow-listed file types are accepted (no web pages or scripts). Up to 4 GB per upload
  (change with `MAX_UPLOAD_MB`).
- ForeFlight CSV import (safe to repeat) and export in ForeFlight's own layout, printable PDF with page totals,
  paper-logbook scans filed by date range, full backup and restore (zip).
- No accounts, no cloud, no telemetry, no JavaScript. Fonts are bundled, so the app never contacts another server.

## Install on your Umbrel

1. Fill in your details (works on macOS and Linux). Use your GitHub username:
   ```sh
   sh setup.sh patriotcryptous "patriotcryptous"
   ```
2. Check that none of your own data is about to be committed (this repo must be public). This should print nothing:
   ```sh
   git init -b main && git add -A && git ls-files | grep -E '\.db$|\.csv$|\.venv|/data/|documents' | grep -v '\.gitkeep'
   ```
3. Commit and publish to a **public** GitHub repository (with the GitHub CLI):
   ```sh
   git commit -m "Pilot Logbook for umbrelOS"
   gh repo create pilot-logbook-umbrel --public --source=. --push
   ```
4. Build the image. Pushing a version tag runs the included GitHub Action, which builds `linux/amd64` and
   `linux/arm64` images and publishes them to GitHub Container Registry (about 5 to 10 minutes):
   ```sh
   git tag v1.2.3 && git push origin v1.2.3
   ```
   When the Action is green, open your GitHub profile, Packages, `hangar-pilot-logbook`, Package settings, and set
   visibility to **Public** so Umbrel can pull it.
5. In umbrelOS, open the App Store, find **Community App Stores** (the menu location differs between versions),
   and add `https://github.com/patriotcryptous/pilot-logbook-umbrel`. Install **Pilot Logbook** from the
   "Hangar" store.
6. Open the app, go to **Import & backup**, and upload your ForeFlight CSV.

To ship a new version later: bump `version:` in `umbrel-app.yml` (this release is 1.2.3), point `image:` in
`docker-compose.yml` at the new tag, commit, then tag `vX.Y.Z` and push the tag.

## Run it locally

```sh
cd app
python3.12 -m venv .venv && source .venv/bin/activate   # Python 3.12 recommended
pip install -r requirements.txt
DATA_DIR=./data uvicorn main:app --port 8080            # then open http://localhost:8080
python -m pytest tests -q                         # tests (add LOGBOOK_FIXTURE=/path/to/export.csv to use a real export)
```

## Where your data lives

Everything is in `/data` inside the container (`${APP_DATA_DIR}/data` on the Umbrel): `logbook.db` (SQLite), `scans/` and `documents/`. The **Download backup** button produces a zip of both. Keep a copy off the device.

## Notes and limits

- A full backup includes every Hangar file, so it is as large as the Hangar and needs about that much free disk space
  while it is built. "Without documents" makes a small logbook-only backup, and restoring one leaves your Hangar alone.
  Very large video uploads also depend on the Umbrel app proxy accepting them, which I could not test.
- Bundled fonts: Barlow, SIL Open Font License 1.1 (see `app/static/fonts/README.txt`).
- ForeFlight has no public sync API, so backup is export then import. A flight you edit in ForeFlight imports
  as a new entry; delete the old one.
- Currency treats every logged landing as flown by you and does not special-case tailwheel or type ratings.
  Progress toward 61.109 estimates cross-country, night and instrument lines from overlapping logged categories.
  These are reminders, not legal determinations.
- No authentication of its own: it relies on umbrelOS's app proxy. Don't expose the app port directly.
