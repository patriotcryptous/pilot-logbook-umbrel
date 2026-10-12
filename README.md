# Pilot Logbook for umbrelOS

A private, single-pilot logbook that runs on your own Umbrel. Import your ForeFlight logbook, add flights, file photos of your paper logbook pages, and keep your important documents in one place. No accounts, no cloud, no telemetry.

## Features

- **Dashboard:** totals, 90-day landing currency, flight review and medical reminders, hours per month, a running total, a year of flying days, and progress toward private pilot minimums. Currency and progress are reminders, not legal determinations.
- **ForeFlight in and out:** import your ForeFlight CSV and export it again, with every ForeFlight field stored and editable. Importing is safe to repeat.
- **Document Hangar:** store insurance, certificates, POH and manuals, notes, photos and videos, with categories, search, and expiry dates that warn you on the dashboard.
- **Endorsements and checkride packet:** log each endorsement with your instructor's details and when it expires, then download one PDF with your hours against the 61.109 minimums, endorsements, currency and documents.
- **Currency forecast:** see the day your day and night passenger currency runs out if you don't fly again.
- **Airports:** a route map of everywhere you've flown, landings by airport, and towered versus non-towered landings.
- **Personal minimums:** set your limits for solo, passenger and dual flights, then paste a METAR for a go/no-go check with the crosswind worked out.
- **Costs:** aircraft and instructor rates, fuel and other costs per flight, spending per month, and what the rest of your training should cost.
- **Expiry alerts:** optional phone notifications through ntfy before your medical, flight review, endorsements, documents or currency run out.
- **Paper scans, PDF and backups:** file scans of your paper logbook by date range, print a PDF logbook with page totals, back up or restore everything in one zip, and save backups on a schedule.

## Install on umbrelOS

1. Open the App Store, find **Community App Stores** (the menu location differs between umbrelOS versions), and add:
   `https://github.com/patriotcryptous/pilot-logbook-umbrel`
2. Install **Pilot Logbook** from the "Hangar" store.
3. Open the app, go to **Import & backup**, and upload your ForeFlight CSV.

The app has no login of its own. It relies on umbrelOS's app proxy.
