# Pilot Logbook for umbrelOS

A private, single-pilot logbook that runs on your own Umbrel. Import your ForeFlight logbook, add flights,
file photos of your paper logbook pages, and keep your important documents in one place. No accounts, no cloud,
no telemetry, no JavaScript. Fonts are bundled, so the app never contacts another server.

## Features

- **Dashboard:** totals, 90-day landing currency per aircraft class (FAR 61.57), flight review and medical
  reminders (FAR 61.56), hours per month, running total, a year of flying days, aircraft, recent flights, and
  progress toward private pilot minimums (FAR 61.109(a)). Light and dark themes.
- **ForeFlight in and out:** all 65 ForeFlight columns are stored, editable and exported again. Importing is safe
  to repeat. Choose which fields appear on the entry form under Settings, and pick decimal (N.N or N.NN) or H:MM time.
- **Document Hangar:** private storage for insurance, certificates, POH and manuals, notes, photos and videos,
  with categories, search, an optional aircraft link, expiry dates that warn you on the dashboard, and
  replace-with-newer-copy for renewals. Only allow-listed file types are accepted (no web pages or scripts).
  Up to 4 GB per upload (change with `MAX_UPLOAD_MB`).
- **Paper scans, PDF and backups:** scans filed by date range, a printable PDF logbook with page totals, and a full
  backup and restore (zip).

## Install on umbrelOS

1. Open the App Store, find **Community App Stores** (the menu location differs between umbrelOS versions), and add:
   `https://github.com/patriotcryptous/pilot-logbook-umbrel`
2. Install **Pilot Logbook** from the "Hangar" store.
3. Open the app, go to **Import & backup**, and upload your ForeFlight CSV.

The app has no login of its own. It relies on umbrelOS's app proxy, so don't expose its port directly.

## Your data

Everything is in `/data` inside the container (`${APP_DATA_DIR}/data` on the Umbrel): `logbook.db` (SQLite),
`scans/` and `documents/`. **Download backup** produces a zip of all of it. Keep a copy off the device.

- A full backup includes every Document Hangar file, so it is as large as the Hangar and needs about that much
  free disk space while it is built. "Without documents" makes a small logbook-only backup, and restoring one
  leaves your current Hangar alone.
- Very large video uploads also depend on the Umbrel app proxy accepting them.

## Notes and limits

- ForeFlight has no public sync API, so backup is export then import. A flight you edit in ForeFlight imports as a
  new entry; delete the old one.
- Currency counts every logged landing as flown by you and does not special-case tailwheel or type ratings.
  Progress toward 61.109 estimates cross-country, night and instrument lines from overlapping logged categories.
  These are reminders, not legal determinations.
- Bundled fonts: Barlow, SIL Open Font License 1.1 (see `app/static/fonts/README.txt`).

## Run it on your computer

```sh
cd app
python3.12 -m venv .venv && source .venv/bin/activate   # Python 3.12 recommended
pip install -r requirements.txt
DATA_DIR=./data uvicorn main:app --port 8080            # then open http://localhost:8080
python -m pytest tests -q                               # tests
```

## Publishing a new version

Bump `version:` in `hangar-pilot-logbook/umbrel-app.yml` and the image tag in
`hangar-pilot-logbook/docker-compose.yml`, commit and push, then tag and push the tag. The included GitHub Action
builds `linux/amd64` and `linux/arm64` images and publishes them to GitHub Container Registry:

```sh
git tag v1.2.4 && git push origin v1.2.4
```
