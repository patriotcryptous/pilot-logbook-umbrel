"""Core of the self-hosted pilot logbook.

One field registry (FIELDS) drives the database columns, ForeFlight import/export, the entry form
and the configurable-fields settings, so every one of ForeFlight's 65 flight columns is stored,
editable and exported again. Single pilot, stdlib only.
"""
import csv
import hashlib
import io
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone


# ----------------------------------------------------------------------------- field registry

def _f(key, ff, label, kind, section, feature=None, hint=""):
    return {"key": key, "ff": ff, "label": label, "kind": kind, "section": section,
            "feature": feature, "hint": hint}


# Listed in ForeFlight's own column order (this is the export order).
FIELDS = [
    _f("date", "Date", "Date", "date", "flight"),
    _f("aircraft", "AircraftID", "Aircraft", "text", "flight"),
    _f("from_apt", "From", "From", "text", "flight"),
    _f("to_apt", "To", "To", "text", "flight"),
    _f("route", "Route", "Route", "text", "flight", "route"),
    _f("time_out", "TimeOut", "Out", "time", "start_end", "out_off_on_in"),
    _f("time_off", "TimeOff", "Off", "time", "start_end", "out_off_on_in"),
    _f("time_on", "TimeOn", "On", "time", "start_end", "out_off_on_in"),
    _f("time_in", "TimeIn", "In", "time", "start_end", "out_off_on_in"),
    _f("on_duty", "OnDuty", "On duty", "time", "start_end", "duty"),
    _f("off_duty", "OffDuty", "Off duty", "time", "start_end", "duty"),
    _f("total", "TotalTime", "Total time", "hours", "time"),
    _f("pic", "PIC", "Pilot in command", "hours", "time"),
    _f("sic", "SIC", "Second in command", "hours", "time", "sic"),
    _f("night", "Night", "Night", "hours", "time"),
    _f("solo", "Solo", "Solo", "hours", "time", "solo"),
    _f("xc", "CrossCountry", "Cross-country", "hours", "time"),
    _f("picus", "PICUS", "PIC under supervision", "hours", "time", "picus"),
    _f("multi_pilot", "MultiPilot", "Multi-pilot", "hours", "time", "multi_pilot"),
    _f("ifr", "IFR", "IFR", "hours", "time", "ifr"),
    _f("examiner", "Examiner", "Examiner duration", "hours", "time", "examiner"),
    _f("nvg", "NVG", "Night vision goggles", "hours", "time", "nvg"),
    _f("nvg_ops", "NVG Ops", "NVG operations", "int", "time", "nvg"),
    _f("distance", "Distance", "Distance (nm)", "num", "time", "distance"),
    _f("actual_instr", "ActualInstrument", "Actual instrument", "hours", "time", "actual_instr"),
    _f("sim_instr", "SimulatedInstrument", "Simulated instrument", "hours", "time", "sim_instr"),
    _f("hobbs_start", "HobbsStart", "Hobbs start", "num", "start_end", "hobbs"),
    _f("hobbs_end", "HobbsEnd", "Hobbs end", "num", "start_end", "hobbs"),
    _f("tach_start", "TachStart", "Tach start", "num", "start_end", "tach"),
    _f("tach_end", "TachEnd", "Tach end", "num", "start_end", "tach"),
    _f("holds", "Holds", "Holds", "int", "time", "holds"),
    *[_f(f"approach{i}", f"Approach{i}", f"Approach {i}", "text", "time", "approaches") for i in range(1, 7)],
    _f("dual_given", "DualGiven", "Dual given", "hours", "time", "dual_given"),
    _f("dual_received", "DualReceived", "Dual received", "hours", "time", "dual_received"),
    _f("sim_flight", "SimulatedFlight", "Simulated flight", "hours", "time", "sim_flight"),
    _f("ground_training", "GroundTraining", "Ground training", "hours", "time", "ground_training"),
    _f("ground_given", "GroundTrainingGiven", "Ground given", "hours", "time", "ground_given"),
    _f("instructor", "InstructorName", "Instructor", "text", "people", "instructor"),
    _f("instructor_comments", "InstructorComments", "Instructor comments", "text", "people", "instructor"),
    *[_f(f"person{i}", f"Person{i}", f"Person {i}", "text", "people", "people") for i in range(1, 7)],
    _f("comments", "PilotComments", "Remarks", "text", "people"),
    _f("flag_flight_review", "Flight Review (FAA)", "Flight review", "flag", "events", "events"),
    _f("flag_ipc", "IPC (FAA)", "Instrument proficiency check", "flag", "events", "events"),
    _f("flag_checkride", "Checkride (FAA)", "Checkride", "flag", "events", "events"),
    _f("flag_6158", "FAA 61.58 (FAA)", "PIC proficiency check (61.58)", "flag", "events", "events"),
    _f("flag_nvg_prof", "NVG Proficiency (FAA)", "NVG proficiency", "flag", "events", "events"),
    _f("takeoff_day", "Takeoff Day", "Day takeoffs, non-towered", "int", "landings", "towered"),
    _f("takeoff_day_towered", "Takeoff Day Towered", "Day takeoffs, towered", "int", "landings", "towered"),
    _f("landing_fs_day", "Landing Full-Stop Day", "Day landings, non-towered", "int", "landings", "towered"),
    _f("landing_fs_day_towered", "Landing Full-Stop Day Towered", "Day landings, towered", "int", "landings", "towered"),
    _f("day_takeoffs", "DayTakeoffs", "Day takeoffs", "int", "landings", "takeoffs"),
    _f("day_landings", "DayLandingsFullStop", "Day full-stop landings", "int", "landings"),
    _f("night_takeoffs", "NightTakeoffs", "Night takeoffs", "int", "landings", "takeoffs"),
    _f("night_landings", "NightLandingsFullStop", "Night full-stop landings", "int", "landings"),
    _f("all_landings", "AllLandings", "All landings", "int", "landings", "all_landings"),
]
BY_KEY = {f["key"]: f for f in FIELDS}
BY_FF = {f["ff"]: f for f in FIELDS}
FF_FLIGHT_HEADER = [f["ff"] for f in FIELDS]
assert len(FIELDS) == 65 and len(BY_KEY) == 65
FLIGHT_KEYS = [f["key"] for f in FIELDS]
NULLABLE = {"takeoff_day", "takeoff_day_towered", "landing_fs_day", "landing_fs_day_towered"}
HOURS_KEYS = [f["key"] for f in FIELDS if f["kind"] == "hours"]
# Fields derived from landings: only shown in the form when their feature is switched on.
DERIVED = {"day_takeoffs", "night_takeoffs", "all_landings", "takeoff_day", "takeoff_day_towered",
           "landing_fs_day", "landing_fs_day_towered"}
# Hours that can never exceed the flight's total time.
SUBSET_OF_TOTAL = ["pic", "sic", "night", "solo", "xc", "picus", "multi_pilot", "ifr", "examiner", "nvg",
                   "actual_instr", "sim_instr", "dual_received", "dual_given"]

# What the entry form shows, section by section, in a sensible order.
SECTIONS = [
    ("flight", "When and where", ["date", "aircraft", "from_apt", "to_apt", "route"]),
    ("time", "Time", ["total", "dual_received", "pic", "solo", "xc", "night", "actual_instr", "sim_instr", "ifr",
                      "sic", "picus", "multi_pilot", "dual_given", "sim_flight", "ground_training", "ground_given",
                      "examiner", "nvg", "nvg_ops", "distance", "holds",
                      "approach1", "approach2", "approach3", "approach4", "approach5", "approach6"]),
    ("landings", "Landings", ["day_landings", "night_landings", "all_landings", "day_takeoffs", "night_takeoffs",
                              "takeoff_day", "takeoff_day_towered", "landing_fs_day", "landing_fs_day_towered"]),
    ("start_end", "Start and end", ["time_out", "time_off", "time_on", "time_in", "hobbs_start", "hobbs_end",
                                    "tach_start", "tach_end", "on_duty", "off_duty"]),
    ("people", "People and remarks", ["instructor", "instructor_comments", "person1", "person2", "person3",
                                      "person4", "person5", "person6", "comments"]),
    ("events", "Proficiency events", ["flag_flight_review", "flag_ipc", "flag_checkride", "flag_6158",
                                      "flag_nvg_prof"]),
]

# Switchable features, grouped like ForeFlight's "Configure Fields" screen.
# (key, label, group, hint, on by default)
FEATURES = [
    ("route", "Route", "General", "", True),
    ("all_landings", "All landings", "General", "Counts landings that were not full stops, too.", True),
    ("takeoffs", "Takeoffs", "General", "Day and night takeoffs, when different from your landings.", False),
    ("towered", "Towered and non-towered", "General", "Splits takeoffs and landings by airport type.", False),
    ("instructor", "Instructor", "General", "Name and comments.", True),
    ("people", "Other people", "General", "Passengers and crew.", False),
    ("events", "Proficiency events", "General", "Flight review, IPC, checkride, 61.58, NVG.", False),
    ("hobbs", "Hobbs", "Start and end", "", False),
    ("tach", "Tach", "Start and end", "", False),
    ("out_off_on_in", "Out / Off / On / In", "Start and end", "", False),
    ("duty", "Duty (on and off)", "Start and end", "", False),
    ("sic", "SIC", "Times", "Second in command, co-pilot, first officer.", False),
    ("solo", "Solo", "Times", "", True),
    ("multi_pilot", "Multi-pilot", "Times", "", False),
    ("picus", "PICUS", "Times", "Pilot in command under supervision.", False),
    ("distance", "Distance", "Cross-country", "", True),
    ("actual_instr", "Actual instrument", "Instrument", "", True),
    ("sim_instr", "Simulated instrument", "Instrument", "", True),
    ("ifr", "IFR", "Instrument", "", False),
    ("holds", "Holds", "Instrument", "", True),
    ("approaches", "Approaches", "Instrument", "Up to six per flight.", False),
    ("dual_given", "Dual given", "Training", "Instructor, CFI.", True),
    ("dual_received", "Dual received", "Training", "", True),
    ("sim_flight", "Simulated flight", "Training", "Time in a simulator or training device.", True),
    ("ground_given", "Ground given", "Training", "", False),
    ("ground_training", "Ground training", "Training", "", True),
    ("examiner", "Examiner duration", "Training", "", False),
    ("nvg", "Night vision goggles", "Night vision goggles", "", False),
]
FEATURE_DEFAULTS = {k for k, _, _, _, on in FEATURES if on}
TIME_FORMATS = {"decimal1": "Decimal (N.N)", "decimal2": "Decimal (N.NN)", "hhmm": "Hours and minutes (H:MM)"}


def _empty(f, v):
    if v is None or v == "":
        return True
    return f["kind"] in ("hours", "num", "int", "flag") and not v


def visible_fields(enabled, row=None):
    """Optional fields to show for a flight: switched on, or already holding data in this row."""
    out = set()
    for f in FIELDS:
        if f["feature"] is None:
            out.add(f["key"])
        elif f["feature"] in enabled:
            out.add(f["key"])
        elif row is not None and f["key"] not in DERIVED and not _empty(f, row[f["key"]]):
            out.add(f["key"])
    return out


# ----------------------------------------------------------------------------- schema + migration

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (          -- provenance: every upload is kept verbatim
    id INTEGER PRIMARY KEY,
    filename TEXT, sha256 TEXT UNIQUE, imported_at TEXT,
    raw BLOB,
    flights_new INTEGER, flights_skipped INTEGER
);
CREATE TABLE IF NOT EXISTS aircraft (
    ident TEXT PRIMARY KEY, type_code TEXT, year TEXT, make TEXT, model TEXT,
    gear_type TEXT, engine_type TEXT, equip_type TEXT, aircraft_class TEXT,
    complex TEXT, high_perf TEXT, taa TEXT, pressurized TEXT
);
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT UNIQUE,                  -- dedupe key; 'manual:<uuid>' for hand entries
    import_id INTEGER REFERENCES imports(id),
    extra_json TEXT,                          -- any column ForeFlight adds that we don't model yet
    date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS flights_date ON flights(date);
CREATE TABLE IF NOT EXISTS scans (            -- paper logbook pages
    id INTEGER PRIMARY KEY, filename TEXT, original_name TEXT, size INTEGER,
    date_from TEXT, date_to TEXT, note TEXT, uploaded_at TEXT
);
CREATE TABLE IF NOT EXISTS profile (          -- single pilot: key/value
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS documents (        -- Document Hangar: files and notes
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'file',        -- 'file' or 'note'
    title TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'other',
    aircraft TEXT, expires TEXT,
    notes TEXT,                               -- description for a file, the text of a note
    filename TEXT, original_name TEXT, mime TEXT, size INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS documents_cat ON documents(category);
"""


def _sql_type(f):
    k = f["kind"]
    if k in ("hours", "num"):
        return "REAL DEFAULT 0"
    if k in ("int", "flag"):
        return "INTEGER" if f["key"] in NULLABLE else "INTEGER DEFAULT 0"
    return "TEXT"


def ensure_schema(db):
    """Create tables and add any missing columns, so older databases upgrade in place."""
    db.executescript(BASE_SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(flights)")}
    added = []
    for f in FIELDS:
        if f["key"] not in have:
            db.execute(f"ALTER TABLE flights ADD COLUMN {f['key']} {_sql_type(f)}")
            added.append(f["key"])
    if "equip_type" not in {r[1] for r in db.execute("PRAGMA table_info(aircraft)")}:
        db.execute("ALTER TABLE aircraft ADD COLUMN equip_type TEXT")
    if added:  # an older database kept these values in extra_json; move them into real columns
        for fid, extra in db.execute(
                "SELECT id, extra_json FROM flights WHERE extra_json IS NOT NULL AND extra_json NOT IN ('', '{}')"
        ).fetchall():
            d = json.loads(extra)
            moved = {}
            for name in list(d):
                f = BY_FF.get(name)
                if f and f["key"] in added:
                    moved[f["key"]] = parse_value(f, d.pop(name))
            if moved:
                db.execute(f"UPDATE flights SET extra_json=?, {','.join(k + '=?' for k in moved)} WHERE id=?",
                           (json.dumps(d), *moved.values(), fid))
    db.commit()


# ----------------------------------------------------------------------------- number handling

def parse_hours(raw):
    """'1.3', '1,3' and '1:18' all work. Returns hours as a float rounded to 0.01."""
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return 0.0
    if ":" in s:
        h, _, m = s.partition(":")
        h, m = (h or "0"), (m or "0")
        if not (h.isdigit() and m.isdigit()) or int(m) >= 60:
            raise ValueError(raw)
        return round(int(h) + int(m) / 60.0, 2)
    return round(float(s), 2)


def parse_value(f, raw):
    """Convert a CSV/form string into the Python value stored for this field."""
    kind = f["kind"]
    raw = "" if raw is None else str(raw).strip()
    if kind == "text":
        return raw.strip('"').strip()
    if kind in ("time", "date"):
        return raw
    if kind in ("hours", "num"):
        try:
            return parse_hours(raw) if kind == "hours" else (round(float(raw.replace(",", ".")), 2) if raw else 0.0)
        except ValueError:
            return 0.0
    if not raw:
        return None if f["key"] in NULLABLE else 0
    try:
        return int(float(raw))
    except ValueError:
        return None if f["key"] in NULLABLE else 0


def _dec(v):
    s = "%.2f" % (v or 0)
    return s[:-1] if s.endswith("0") else s  # 1.70 -> 1.7, 0.25 stays 0.25: lossless to two places


def fmt_hours(v, tf="decimal1"):
    v = v or 0.0
    if tf == "hhmm":
        mins = int(round(v * 60))
        return "%d:%02d" % (mins // 60, mins % 60)
    return "%.2f" % v if tf == "decimal2" else "%.1f" % v


def fmt_ff(f, v):
    k = f["kind"]
    if k == "hours":
        return _dec(v)
    if k == "num":
        return "%.2f" % (v or 0)
    if k == "flag":
        return "1" if v else ""
    if k == "int":
        return "" if v is None else str(int(v))
    return "" if v is None else str(v)


# ----------------------------------------------------------------------------- import

def parse_foreflight(text):
    """Return (aircraft_rows, flight_rows) as lists of dicts keyed by ForeFlight header names.

    The export is two tables in one CSV, each preceded by a title row and a header row, with
    trailing empty columns. Sections are located by their header rows, not by line number.
    """
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"), newline="")))
    if not rows or not rows[0] or not rows[0][0].startswith("ForeFlight Logbook Import"):
        raise ValueError("This doesn't look like a ForeFlight logbook export. "
                         "In ForeFlight, export your logbook as CSV and upload that file.")

    def section(first_col):
        i = next((n for n, r in enumerate(rows) if r and r[0].strip() == first_col), None)
        if i is None:
            return []
        hdr = [h.strip() for h in rows[i]]
        while hdr and not hdr[-1]:
            hdr.pop()
        out = []
        for r in rows[i + 1:]:
            if not r or not r[0].strip():
                break  # blank row ends the section
            out.append({h: (r[k].strip() if k < len(r) else "") for k, h in enumerate(hdr)})
        return out

    return section("AircraftID"), section("Date")


def fingerprint(flight, occurrence):
    """Stable hash of ALL columns + occurrence number.

    Date+aircraft is NOT unique (a dual flight and a first solo can share a day and tail).
    Hashing every column means a flight edited in ForeFlight imports as a new row rather than
    silently overwriting; the occurrence counter keeps genuinely identical rows distinct.
    """
    blob = "\x1f".join(f"{k}={flight[k]}" for k in sorted(flight)) + f"#{occurrence}"
    return hashlib.sha256(blob.encode()).hexdigest()


AIRCRAFT_COLS = [("ident", "AircraftID"), ("type_code", "TypeCode"), ("year", "Year"), ("make", "Make"),
                 ("model", "Model"), ("gear_type", "GearType"), ("engine_type", "EngineType"),
                 ("equip_type", "equipType (FAA)"), ("aircraft_class", "aircraftClass (FAA)"),
                 ("complex", "complexAircraft (FAA)"), ("taa", "taa (FAA)"),
                 ("high_perf", "highPerformance (FAA)"), ("pressurized", "pressurized (FAA)")]


def import_foreflight(db, raw_bytes, filename="logbook.csv"):
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("That file isn't a UTF-8 CSV. Export the logbook from ForeFlight as CSV.")
    aircraft, flights = parse_foreflight(text)
    sha = hashlib.sha256(raw_bytes).hexdigest()
    if db.execute("SELECT 1 FROM imports WHERE sha256=?", (sha,)).fetchone():
        return {"new": 0, "skipped": len(flights), "note": "This exact file was already imported."}

    cur = db.execute(
        "INSERT INTO imports(filename, sha256, imported_at, raw) VALUES (?,?,?,?)",
        (filename, sha, datetime.now(timezone.utc).isoformat(), raw_bytes))
    import_id = cur.lastrowid

    for a in aircraft:
        vals = {col: (a.get(ff) or "").strip() for col, ff in AIRCRAFT_COLS}
        db.execute(f"INSERT OR REPLACE INTO aircraft({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                   tuple(vals.values()))

    new = skipped = 0
    seen = {}
    for f in flights:
        base = tuple(sorted(f.items()))
        seen[base] = seen.get(base, 0) + 1
        fp = fingerprint(f, seen[base])
        vals = {fld["key"]: parse_value(fld, f.get(fld["ff"])) for fld in FIELDS}
        extra = {k: v for k, v in f.items() if k not in BY_FF and v not in ("", "0", "0.0", "0.00")}
        try:
            db.execute(
                f"INSERT INTO flights(fingerprint, import_id, extra_json, {','.join(vals)}) "
                f"VALUES (?,?,?,{','.join('?' * len(vals))})",
                (fp, import_id, json.dumps(extra), *vals.values()))
            new += 1
        except sqlite3.IntegrityError:
            skipped += 1
    db.execute("UPDATE imports SET flights_new=?, flights_skipped=? WHERE id=?", (new, skipped, import_id))
    db.commit()
    return {"new": new, "skipped": skipped}


# ----------------------------------------------------------------------------- export

def export_foreflight_csv(db):
    """CSV in ForeFlight's import layout, with every column it uses."""
    n = len(FF_FLIGHT_HEADER)
    pad = lambda row: list(row) + [""] * (n - len(row))  # noqa: E731
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(pad(["ForeFlight Logbook Import",
                    "This row is required for importing into ForeFlight. Do not delete or modify."]))
    w.writerow([""] * n)
    w.writerow(pad(["Aircraft Table"]))
    w.writerow(pad([ff for _, ff in AIRCRAFT_COLS]))
    for a in db.execute("SELECT * FROM aircraft ORDER BY ident"):
        w.writerow(pad(["" if a[c] in (None, 0, "0") else str(a[c]) for c, _ in AIRCRAFT_COLS]))
    w.writerow([""] * n)
    w.writerow(["Flights Table "] + [" "] * (n - 6) + ["Deprecated: Do not edit manually"] * 5)

    rows = db.execute("SELECT * FROM flights ORDER BY date DESC, id DESC").fetchall()
    extra_cols = []
    for r in rows:
        for k in json.loads(r["extra_json"] or "{}"):
            if k not in extra_cols:
                extra_cols.append(k)
    w.writerow(FF_FLIGHT_HEADER + extra_cols)
    for r in rows:
        v = {f["key"]: r[f["key"]] for f in FIELDS}
        # ForeFlight's newer per-airport columns: derive them for flights that never had them.
        if (r["fingerprint"] or "").startswith("manual:"):  # imported rows are exported exactly as received
            if v["takeoff_day"] is None and v["takeoff_day_towered"] is None:
                v["takeoff_day"] = v["day_takeoffs"] or 0
            if v["landing_fs_day"] is None and v["landing_fs_day_towered"] is None:
                v["landing_fs_day"] = v["day_landings"] or 0
        extra = json.loads(r["extra_json"] or "{}")
        w.writerow([fmt_ff(f, v[f["key"]]) for f in FIELDS] + [extra.get(c, "") for c in extra_cols])
    return out.getvalue()


# ----------------------------------------------------------------------------- flights CRUD

def _derive(values, old):
    """Keep takeoffs and all-landings in step with landings when those fields aren't being edited."""
    old = old or {}
    dl, nl = values.get("day_landings", old.get("day_landings") or 0), values.get("night_landings", old.get("night_landings") or 0)
    d_dl, d_nl = dl - (old.get("day_landings") or 0), nl - (old.get("night_landings") or 0)
    if "day_takeoffs" not in values:
        values["day_takeoffs"] = max((old.get("day_takeoffs") or 0) + d_dl, 0)
    if "night_takeoffs" not in values:
        values["night_takeoffs"] = max((old.get("night_takeoffs") or 0) + d_nl, 0)
    if "all_landings" not in values:
        values["all_landings"] = max((old.get("all_landings") or 0) + d_dl + d_nl, dl + nl)
    return values


def add_flight(db, values):
    values = _derive(dict(values), None)
    cols = [k for k in values if k in BY_KEY]
    db.execute(f"INSERT INTO flights(fingerprint, extra_json, {','.join(cols)}) "
               f"VALUES (?,?,{','.join('?' * len(cols))})",
               ("manual:" + uuid.uuid4().hex, "{}", *[values[c] for c in cols]))
    db.commit()


def update_flight(db, flight_id, values):
    old = db.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
    values = _derive(dict(values), dict(old) if old else None)
    cols = [k for k in values if k in BY_KEY]
    db.execute(f"UPDATE flights SET {','.join(c + '=?' for c in cols)} WHERE id=?",
               (*[values[c] for c in cols], flight_id))
    db.commit()


def delete_flight(db, flight_id):
    db.execute("DELETE FROM flights WHERE id=?", (flight_id,))
    db.commit()


def ensure_aircraft(db, ident, make="", model="", aircraft_class=""):
    ident = ident.strip().upper()
    if ident and not db.execute("SELECT 1 FROM aircraft WHERE ident=?", (ident,)).fetchone():
        db.execute("INSERT INTO aircraft(ident, make, model, aircraft_class) VALUES (?,?,?,?)",
                   (ident, make.strip(), model.strip(), aircraft_class.strip()))
        db.commit()


# ----------------------------------------------------------------------------- profile + settings

def profile_get(db):
    return {r[0]: r[1] for r in db.execute("SELECT key, value FROM profile")}


def profile_set(db, values):
    for k, v in values.items():
        db.execute("INSERT INTO profile(key, value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    db.commit()


def enabled_features(prof):
    if "features" in prof:
        return {x for x in prof["features"].split(",") if x}
    return set(FEATURE_DEFAULTS)


def time_format(prof):
    tf = prof.get("time_format", "decimal1")
    return tf if tf in TIME_FORMATS else "decimal1"


# ----------------------------------------------------------------------------- summaries

SUM_SQL = """COALESCE(COUNT(*),0) AS flights, COALESCE(SUM(total),0) AS total, COALESCE(SUM(pic),0) AS pic,
    COALESCE(SUM(solo),0) AS solo, COALESCE(SUM(xc),0) AS xc, COALESCE(SUM(night),0) AS night,
    COALESCE(SUM(dual_received),0) AS dual_received, COALESCE(SUM(dual_given),0) AS dual_given,
    COALESCE(SUM(sim_instr),0) AS sim_instr, COALESCE(SUM(actual_instr),0) AS actual_instr,
    COALESCE(SUM(ground_training),0) AS ground_training, COALESCE(SUM(all_landings),0) AS landings,
    COALESCE(SUM(night_landings),0) AS night_landings, COALESCE(SUM(sic),0) AS sic,
    COALESCE(SUM(picus),0) AS picus, COALESCE(SUM(multi_pilot),0) AS multi_pilot,
    COALESCE(SUM(ifr),0) AS ifr, COALESCE(SUM(nvg),0) AS nvg, COALESCE(SUM(sim_flight),0) AS sim_flight,
    COALESCE(SUM(ground_given),0) AS ground_given, COALESCE(SUM(examiner),0) AS examiner,
    COALESCE(SUM(distance),0) AS distance, COALESCE(SUM(holds),0) AS holds"""


def totals(db, where="1=1", params=()):
    return dict(db.execute(f"SELECT {SUM_SQL} FROM flights WHERE {where}", params).fetchone())


def class_label(c):
    return c.replace("_", " ").capitalize() if c else "Class not set"


def _parse(d):
    return datetime.strptime(d, "%Y-%m-%d").date()


def currency(db, today=None):
    """FAR 61.57(a)/(b) landing currency per aircraft class, from the logged landings.

    Assumes you were the sole manipulator for every landing you logged, and does not treat
    tailwheel or type-rating cases specially. The 'good through' date is counted conservatively.
    """
    today = today or date.today()
    start = (today - timedelta(days=90)).isoformat()
    out = []
    classes = [r[0] for r in db.execute(
        "SELECT DISTINCT COALESCE(a.aircraft_class,'') FROM flights f "
        "LEFT JOIN aircraft a ON a.ident=f.aircraft ORDER BY 1")]
    for c in classes:
        rows = db.execute(
            "SELECT f.date, f.all_landings, f.night_landings FROM flights f "
            "LEFT JOIN aircraft a ON a.ident=f.aircraft "
            "WHERE COALESCE(a.aircraft_class,'')=? AND f.date>? AND f.date<=? ORDER BY f.date DESC",
            (c, start, today.isoformat())).fetchall()

        def status(idx):
            n = sum(r[idx] or 0 for r in rows)
            through = None
            if n >= 3:
                have = 0
                for r in rows:  # newest first; the date that makes the 3rd-most-recent landing
                    have += r[idx] or 0
                    if have >= 3:
                        through = _parse(r[0]) + timedelta(days=89)
                        break
            return n, through

        dn, dthrough = status(1)
        nn, nthrough = status(2)
        out.append({"class": c, "label": class_label(c), "day_count": dn, "day_ok": dn >= 3,
                    "day_through": dthrough, "day_left": (dthrough - today).days if dthrough else 0,
                    "night_count": nn, "night_ok": nn >= 3, "night_through": nthrough,
                    "night_left": (nthrough - today).days if nthrough else 0})
    return out


def flight_review_through(last):
    """FAR 61.56(c): valid through the end of the 24th calendar month after the review month."""
    d = _parse(last)
    m = d.month + 24
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(ny, nm, 1) - timedelta(days=1)


def progress_private_asel(db):
    """Progress toward FAR 61.109(a) (airplane single-engine) from logged hours.

    Cross-country, night and instrument items are approximations: the log has no 'dual
    cross-country' column, so those use the overlap of the logged categories on each flight.
    """
    cond = "f.aircraft IN (SELECT ident FROM aircraft WHERE aircraft_class LIKE 'airplane%single%')"
    q = lambda expr: db.execute(f"SELECT COALESCE(SUM({expr}),0) FROM flights f WHERE {cond}").fetchone()[0]  # noqa: E731
    return {
        "tracked": [
            ("Total flight time", db.execute("SELECT COALESCE(SUM(total),0) FROM flights").fetchone()[0], 40, "hrs"),
            ("Flight training with an instructor", q("dual_received"), 20, "hrs"),
            ("Solo flight", q("solo"), 10, "hrs"),
            ("Cross-country flight training", q("MIN(xc, dual_received)"), 3, "hrs"),
            ("Solo cross-country", q("MIN(xc, solo)"), 5, "hrs"),
            ("Night flight training", q("MIN(night, dual_received)"), 3, "hrs"),
            ("Night takeoffs and landings (full stop)", q("night_landings"), 10, "ldg"),
            ("Instrument training", q("sim_instr + actual_instr"), 3, "hrs"),
            ("Solo landings at towered airports", q("CASE WHEN solo > 0 THEN COALESCE(landing_fs_day_towered, 0) ELSE 0 END"), 3, "ldg"),
        ],
        "untracked": [
            "One night cross-country over 100 nm total distance",
            "One solo cross-country of 150 nm with full-stop landings at three points and one leg over 50 nm",
            "3 hours of training with an instructor in the 2 calendar months before the practical test",
        ],
    }


def dashboard(db, today=None):
    """Everything the dashboard widgets need, computed in one pass."""
    today = today or date.today()
    t = totals(db)
    rows = db.execute("SELECT date, aircraft, total, dual_received, solo FROM flights ORDER BY date, id").fetchall()
    last = rows[-1]["date"] if rows else None

    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append({"key": f"{y:04d}-{m:02d}", "year": y, "month": m, "dual": 0.0, "solo": 0.0, "other": 0.0})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    by_month = {mm["key"]: mm for mm in months}

    cum, cumulative, heat = 0.0, [], {}
    ac = {}
    horizon = today - timedelta(days=371)
    last30 = 0.0
    for r in rows:
        tot, du, so = r["total"] or 0.0, r["dual_received"] or 0.0, r["solo"] or 0.0
        cum += tot
        if cumulative and cumulative[-1][0] == r["date"]:
            cumulative[-1] = (r["date"], cum, cumulative[-1][2] or so > 0)
        else:
            cumulative.append((r["date"], cum, so > 0))
        mm = by_month.get(r["date"][:7])
        if mm:
            mm["dual"] += du
            mm["solo"] += so
            mm["other"] += max(tot - du - so, 0.0)
        d = _parse(r["date"])
        if d >= horizon:
            heat[r["date"]] = heat.get(r["date"], 0.0) + tot
        if (today - d).days < 30 and d <= today:
            last30 += tot
        a = ac.setdefault(r["aircraft"] or "?", {"ident": r["aircraft"] or "?", "hours": 0.0, "flights": 0})
        a["hours"] += tot
        a["flights"] += 1
    for mm in months:
        mm["total"] = mm["dual"] + mm["solo"] + mm["other"]
    models = {r[0]: r[1] for r in db.execute("SELECT ident, model FROM aircraft")}
    aircraft = sorted(ac.values(), key=lambda a: -a["hours"])
    for a in aircraft:
        a["model"] = models.get(a["ident"], "")
    recent = db.execute("SELECT * FROM flights ORDER BY date DESC, id DESC LIMIT 6").fetchall()
    return {"totals": t, "last": last, "days_since": (today - _parse(last)).days if last else None,
            "last30": last30, "months": months, "cumulative": cumulative, "heat": heat,
            "aircraft": aircraft, "recent": recent}
