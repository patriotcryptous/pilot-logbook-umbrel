# Pilot Logbook for umbrelOS

A private, single-pilot logbook that runs on your own Umbrel.

## Why

Your logbook is the record of every hour you've earned, and it should have a backup you control.

- **Back up your paper logbook.** Photograph your pages and file them by date, and enter or import your flights, so a lost, stolen or water-damaged logbook isn't the end of your records.
- **Own your copy of your data.** If you log in ForeFlight, your records live in their cloud, tied to your account and subscription. Pilot Logbook keeps a complete copy on hardware you own: import your ForeFlight CSV whenever you like, and export it back out in ForeFlight's own format at any time.
- **Private by design.** No accounts, no cloud, no telemetry. Your logbook never leaves your Umbrel. The only outside connections are the map tiles your browser loads on the Airports page, the airport code sent to the NOAA Aviation Weather Center when you run a Preflight Risk Check, a weekly check of the eCFR and faa.gov for new editions of the FAR and AIM (you can turn this off), and the expiry alerts, if you turn them on.

## Features

- **Dashboard:** totals, 90-day landing currency, flight review and medical reminders, hours per month, a running total, a year of flying days, and progress toward your next certificate. Currency and progress are reminders, not legal determinations.
- **Progress:** switch between sport, private, instrument and commercial (airplane) requirements, with the flights that must meet a distance or route standard checked from your logbook.
- **ForeFlight in and out:** import your ForeFlight CSV and export it again, with every ForeFlight field stored and editable. Importing is safe to repeat, flights you edited in ForeFlight are spotted so you can replace the old copy, and the dashboard reminds you when your last import is two weeks old.
- **Document Hangar:** store insurance, certificates, POH and manuals, notes, photos and videos, with categories, search, and expiry dates that warn you on the dashboard. The current FAR (from the eCFR) and AIM (from faa.gov) are downloaded on first start and kept up to date. Paper logbook scans and import and backup live here too.
- **Reports:** your hours laid out like the IACRA (Form 8710-1) pilot time grid, a logbook check that flags entries an examiner or insurer would question, and an insurance summary (total, PIC, recent and make-and-model time) as a PDF.
- **Quick add:** a short phone-friendly form for logging the basics right after you land.
- **Endorsements and checkride packet:** log each endorsement with your instructor's details and when it expires, then download one PDF with your hours against the 61.109 minimums, endorsements, currency and documents.
- **Currency forecast:** see the day your day and night passenger currency runs out if you don't fly again.
- **Airports:** an interactive route map of everywhere you've flown (zoom, street or terrain view, nearby airports, range rings), landings by airport, and towered versus non-towered landings.
- **Preflight Risk Check:** one step at a time. Enter an airport for the live METAR and TAF (or the nearest station's), density altitude, the best runway's headwind and crosswind, and a go/no-go against your personal minimums; then answer the PAVE questions one by one (the weather, your logbook and your fuel numbers answer some for you), tick through the 91.103, ARROW and inspection checklists, and finish with your risk score. Checks can be saved to a history.
- **Costs:** aircraft and instructor rates, fuel and other costs per flight, spending per month, and what the rest of your training should cost.
- **Expiry alerts:** optional phone notifications through ntfy before your medical, flight review, endorsements, documents or currency run out.
- **Paper scans, PDF and backups:** file scans of your paper logbook by date range, print a PDF logbook with page totals, back up or restore everything in one zip, and save backups on a schedule.

## Install on umbrelOS

1. Open the App Store, find **Community App Stores** (the menu location differs between umbrelOS versions), and add:
   `https://github.com/patriotcryptous/pilot-logbook-umbrel`
2. Install **Pilot Logbook** from the "Hangar" store.
3. Open the app, go to **Import & backup**, and upload your ForeFlight CSV.

The app has no login of its own. It relies on umbrelOS's app proxy.

## Support

Found a bug or have an idea? [Open an issue](https://github.com/patriotcryptous/pilot-logbook-umbrel/issues/new/choose). Please don't attach your logbook, exports or documents: this repository is public.

## Disclaimer

Pilot Logbook is a personal record-keeping aid, provided as is, with no warranty of any kind. Currency, progress toward certificate minimums, endorsement expiry, personal minimums, weather, crosswind, density altitude, fuel, risk scores and cost figures are calculated from what you entered and can be wrong. They are reminders, not legal determinations: confirm your eligibility and currency with your instructor, examiner or the FAA, and check the current regulations yourself. Keep your original logbook and endorsements, and keep a backup of this app's data somewhere other than your Umbrel.

This project isn't affiliated with, endorsed by or sponsored by ForeFlight or the FAA. ForeFlight is a trademark of its owner. Airport data comes from [airportsdata](https://github.com/mborsetti/airportsdata) (MIT License) and offline map boundaries from [us-atlas](https://github.com/topojson/us-atlas) (US Census Bureau data, ISC License). The interactive map uses [Leaflet](https://leafletjs.com) (BSD 2-Clause), with tiles from [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors and [OpenTopoMap](https://opentopomap.org) (CC-BY-SA). Weather comes from the [NOAA Aviation Weather Center](https://aviationweather.gov) and runway data from [OurAirports](https://ourairports.com/data/) (public domain). Always get an official weather briefing before you fly.

## License

Pilot Logbook is released under the [MIT License](LICENSE). Bundled third-party pieces keep their own licenses: the Barlow fonts (SIL Open Font License 1.1, `app/static/fonts`), Leaflet (BSD 2-Clause, `app/static/leaflet`), airport data from airportsdata (MIT) and map boundaries from us-atlas (ISC, US Census Bureau data) in `app/geo`, and runway data from OurAirports (public domain). The FAR and AIM the app downloads are US government works.

## Credits

Built by [patriotcryptous](https://github.com/patriotcryptous) with [Claude](https://claude.ai), Anthropic's AI model, which wrote much of the code.
