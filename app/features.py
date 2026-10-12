"""Training records, currency forecast, airports, personal minimums, costs and expiry alerts.

Pure helpers and queries; the web routes live in main.py. The only code here that talks to the
network is send_alert(), and only to the address the pilot typed into Settings.
"""
import csv
import gzip
import json
import math
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import logbook_core as core

GEO = Path(__file__).parent / "geo"

SCHEMA = """
CREATE TABLE IF NOT EXISTS endorsements (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'other', title TEXT NOT NULL, date TEXT NOT NULL,
    far_ref TEXT, cfi_name TEXT, cfi_cert TEXT, cfi_cert_expires TEXT,
    expires TEXT, notes TEXT, document_id INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS flight_costs (     -- one row per flight that has cost details
    flight_id INTEGER PRIMARY KEY,
    aircraft_rate REAL, instructor_rate REAL, fuel_gal REAL, fuel_cost REAL, other_cost REAL
);
CREATE TABLE IF NOT EXISTS alerts_sent (      -- so each reminder goes out once per threshold
    key TEXT PRIMARY KEY, sent_at TEXT
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def valid_date(s):
    try:
        _d(s)
        return True
    except (TypeError, ValueError):
        return False


def end_of_month(d, add_months=0):
    m = d.month - 1 + add_months
    y, m = d.year + m // 12, m % 12 + 1
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(ny, nm, 1) - timedelta(days=1)


def _json(prof, key, default):
    try:
        v = json.loads(prof.get(key) or "")
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


# ----------------------------------------------------------------------------- endorsements

# (key, title, FAR reference, how long it stays valid: None | ("days", n) | ("months_end", n))
ENDORSEMENT_KINDS = [
    ("presolo_knowledge", "Pre-solo aeronautical knowledge test", "61.87(b)", None),
    ("presolo_training", "Pre-solo flight training", "61.87(c)", None),
    ("solo", "Solo flight (90-day solo endorsement)", "61.87(n)", ("days", 90)),
    ("solo_night", "Solo flight at night", "61.87(o)", None),
    ("solo_other_airport", "Solo takeoffs and landings at another airport within 25 nm", "61.93(b)(1)", None),
    ("solo_xc_training", "Solo cross-country training", "61.93(c)(1)", None),
    ("solo_xc_flight", "Solo cross-country flight (each flight)", "61.93(c)(2)", None),
    ("solo_xc_repeated", "Repeated solo cross-country within 50 nm", "61.93(b)(2)", None),
    ("class_b", "Solo flight in Class B airspace", "61.95(a)", ("days", 90)),
    ("class_b_airport", "Solo flight at an airport in Class B airspace", "61.95(b)", ("days", 90)),
    ("knowledge_test", "Endorsement for the private pilot knowledge test", "61.35(a)(1), 61.103(d)", None),
    ("knowledge_result", "Knowledge test passed (score report)", "61.39(a)(1)", ("months_end", 24)),
    ("practical_test", "Endorsement for the private pilot practical test", "61.39(a)(6), 61.103(f), 61.107(b), 61.109",
     ("months_end", 2)),
    ("other", "Other endorsement", "", None),
]
ENDORSEMENT_BY_KEY = {k: (t, f, v) for k, t, f, v in ENDORSEMENT_KINDS}


def default_expiry(kind, when):
    """The usual end of validity for an endorsement given on `when` (a date), or None."""
    rule = ENDORSEMENT_BY_KEY.get(kind, (None, None, None))[2]
    if not rule:
        return None
    unit, n = rule
    if unit == "days":
        return when + timedelta(days=n)
    return end_of_month(when, n)


def endorsements(db, today=None):
    """Every endorsement, newest first, with its expiry state. Older ones of the same kind are 'superseded'."""
    today = today or date.today()
    rows = [dict(r) for r in db.execute("SELECT * FROM endorsements ORDER BY date DESC, id DESC")]
    seen = set()
    for r in rows:
        r["kind_label"] = ENDORSEMENT_BY_KEY.get(r["kind"], ("Other",))[0]
        r["superseded"] = r["kind"] in seen and r["kind"] not in ("other", "solo_xc_flight")
        seen.add(r["kind"])
        if r["expires"]:
            days = (_d(r["expires"]) - today).days
            r["state"] = "expired" if days < 0 else "soon" if days <= 30 else "ok"
            r["days_left"] = days
        else:
            r["state"], r["days_left"] = "none", None
    return rows


def parse_endorsement(form):
    errors = []
    kind = form.get("kind") or "other"
    if kind not in ENDORSEMENT_BY_KEY:
        kind = "other"
    title = (form.get("title") or "").strip()[:200] or ENDORSEMENT_BY_KEY[kind][0]
    when = (form.get("date") or "").strip()
    if not valid_date(when):
        errors.append("Enter the date of the endorsement.")
    v = {"kind": kind, "title": title, "date": when,
         "far_ref": (form.get("far_ref") or "").strip()[:80] or ENDORSEMENT_BY_KEY[kind][1],
         "cfi_name": (form.get("cfi_name") or "").strip()[:120],
         "cfi_cert": (form.get("cfi_cert") or "").strip()[:40],
         "cfi_cert_expires": (form.get("cfi_cert_expires") or "").strip(),
         "expires": (form.get("expires") or "").strip(),
         "notes": (form.get("notes") or "").strip()[:2000]}
    for key, label in (("cfi_cert_expires", "Instructor certificate expiry"), ("expires", "Valid through")):
        if v[key] and not valid_date(v[key]):
            errors.append(f"{label} must be a date.")
    if not v["expires"] and valid_date(when) and form.get("auto_expiry", "1") == "1":
        e = default_expiry(kind, _d(when))
        v["expires"] = e.isoformat() if e else ""
    try:
        v["document_id"] = int(form.get("document_id") or 0) or None
    except ValueError:
        v["document_id"] = None
    return v, errors


def save_endorsement(db, v, eid=None):
    cols = list(v)
    if eid:
        db.execute(f"UPDATE endorsements SET {','.join(c + '=?' for c in cols)} WHERE id=?", (*v.values(), eid))
    else:
        db.execute(f"INSERT INTO endorsements({','.join(cols)}, created_at) VALUES ({','.join('?' * len(cols))}, ?)",
                   (*v.values(), datetime.now().isoformat(timespec="seconds")))
    db.commit()


# ----------------------------------------------------------------------------- currency forecast

def currency_forecast(db, today=None, horizon=120):
    """For each aircraft class: landings in the rolling 90-day window for each of the next `horizon` days,
    assuming no more flying, plus the date currency lapses and what it takes to fix it now."""
    today = today or date.today()
    start = today - timedelta(days=90)
    out = []
    for k in core.currency(db, today):
        rows = db.execute(
            "SELECT f.date, COALESCE(f.all_landings,0), COALESCE(f.night_landings,0) FROM flights f "
            "LEFT JOIN aircraft a ON a.ident=f.aircraft WHERE COALESCE(a.aircraft_class,'')=? AND f.date>? AND f.date<=? "
            "ORDER BY f.date", (k["class"], start.isoformat(), today.isoformat())).fetchall()
        item = {"class": k["class"], "label": k["label"]}
        for name, idx, through in (("day", 1, k["day_through"]), ("night", 2, k["night_through"])):
            per_day = {}
            for r in rows:
                per_day[r[0]] = per_day.get(r[0], 0) + (r[idx] or 0)
            series = []
            for i in range(horizon + 1):
                d = today + timedelta(days=i)
                lo = (d - timedelta(days=90)).isoformat()
                series.append(sum(n for ds, n in per_day.items() if ds > lo))
            drops = []
            for ds in sorted(per_day):
                if per_day[ds]:
                    off = _d(ds) + timedelta(days=90)
                    drops.append({"date": off, "landings": per_day[ds], "flown": _d(ds)})
            have = series[0]
            item[name] = {"series": series, "have": have, "need": max(0, 3 - have), "current": have >= 3,
                          "through": through, "lapses": through + timedelta(days=1) if through else None,
                          "days_left": (through - today).days if through else None, "drops": drops}
        out.append(item)
    return out


# ----------------------------------------------------------------------------- airports and routes

@lru_cache(maxsize=1)
def _airports():
    by_icao, by_lid, by_iata = {}, {}, {}
    with gzip.open(GEO / "airports.csv.gz", "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rec = {"icao": r["icao"], "lid": r["lid"], "iata": r["iata"], "name": r["name"], "city": r["city"],
                   "subd": r["subd"], "country": r["country"], "lat": float(r["lat"]), "lon": float(r["lon"])}
            by_icao[r["icao"]] = rec
            if r["lid"]:
                by_lid.setdefault(r["lid"], rec)
            if r["iata"]:
                by_iata.setdefault(r["iata"], rec)
    return by_icao, by_lid, by_iata


def resolve_airport(code):
    """Look up an airport by ICAO (KLUK), FAA identifier (LUK, I67) or IATA code."""
    code = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,5}", code):
        return None
    by_icao, by_lid, by_iata = _airports()
    return (by_icao.get(code) or by_lid.get(code)
            or (by_icao.get("K" + code) if len(code) == 3 else None) or by_iata.get(code))


def route_points(row):
    """The airports one flight touched, in order: departure, route stops, destination."""
    toks = [t.upper() for t in re.split(r"[\s,\-/>]+", row["route"] or "") if t]
    seq = toks or []
    frm, to = (row["from_apt"] or "").upper(), (row["to_apt"] or "").upper()
    if frm and (not seq or seq[0] != frm):
        seq.insert(0, frm)
    if to and (not seq or seq[-1] != to):
        seq.append(to)
    return seq


def airport_stats(db, year=""):
    """Airports visited, landings attributed to each, and the legs flown between them.

    Landings are attributed like this: one landing at each intermediate stop on the route,
    and the rest at the destination. A logbook doesn't say exactly where each landing happened.
    """
    where, params = "1=1", []
    if re.fullmatch(r"\d{4}", year or ""):
        where, params = "substr(date,1,4)=?", [year]
    rows = db.execute(f"SELECT date, from_apt, to_apt, route, all_landings, day_landings, night_landings, "
                      f"landing_fs_day_towered, total FROM flights WHERE {where} ORDER BY date", params).fetchall()
    airports, legs, unknown = {}, {}, {}
    for r in rows:
        seq = route_points(r)
        res = []
        for code in seq:
            ap = resolve_airport(code)
            if ap:
                res.append((code, ap))
            else:
                unknown[code] = unknown.get(code, 0) + 1
        if not res:
            continue
        touched = set()
        for code, ap in res:
            a = airports.setdefault(ap["icao"], {"code": code, "ap": ap, "visits": 0, "landings": 0,
                                                 "first": r["date"], "last": r["date"]})
            if ap["icao"] not in touched:
                a["visits"] += 1
                touched.add(ap["icao"])
            a["last"] = r["date"]
        for (_, a1), (_, a2) in zip(res, res[1:]):
            if a1["icao"] != a2["icao"]:
                key = tuple(sorted((a1["icao"], a2["icao"])))
                legs[key] = legs.get(key, 0) + 1
        landings = r["all_landings"] or 0
        dest = res[-1][1]["icao"]
        stops = []
        for _, ap in res[1:-1]:
            if ap["icao"] != dest and ap["icao"] not in stops:
                stops.append(ap["icao"])
        for s in stops:
            if landings <= 1:
                break
            airports[s]["landings"] += 1
            landings -= 1
        airports[dest]["landings"] += landings
    ranked = sorted(airports.values(), key=lambda a: (-a["landings"], -a["visits"], a["code"]))
    towered = db.execute(f"SELECT COALESCE(SUM(landing_fs_day_towered),0), COALESCE(SUM(day_landings),0) "
                         f"FROM flights WHERE {where}", params).fetchone()
    return {"airports": ranked, "legs": legs, "unknown": sorted(unknown.items(), key=lambda x: -x[1]),
            "towered": towered[0], "day_full_stop": towered[1],
            "total_landings": sum(a["landings"] for a in ranked)}


# ----------------------------------------------------------------------------- personal minimums

MIN_FIELDS = [
    ("ceiling", "Lowest ceiling", "ft AGL", "min"),
    ("visibility", "Lowest visibility", "SM", "min"),
    ("wind", "Highest wind", "kt", "max"),
    ("gust", "Highest gust", "kt", "max"),
    ("gust_spread", "Biggest gust spread", "kt", "max"),
    ("crosswind", "Highest crosswind", "kt", "max"),
    ("runway", "Shortest runway", "ft", "min"),
]
MIN_PROFILES = [("solo", "Solo"), ("passengers", "With passengers"), ("dual", "With an instructor")]


def minimums_get(prof):
    m = _json(prof, "minimums", {})
    return {p: {k: (m.get(p, {}) or {}).get(k) for k, *_ in MIN_FIELDS} | {"night": bool((m.get(p) or {}).get("night"))}
            for p, _ in MIN_PROFILES}


def parse_minimums(form):
    out, errors = {}, []
    for p, plabel in MIN_PROFILES:
        out[p] = {"night": bool(form.get(f"{p}_night"))}
        for k, label, unit, _ in MIN_FIELDS:
            raw = (form.get(f"{p}_{k}") or "").strip().replace(",", "")
            if not raw:
                out[p][k] = None
                continue
            try:
                val = float(raw)
                if val < 0 or val > 100000:
                    raise ValueError
                out[p][k] = int(val) if val == int(val) else val
            except ValueError:
                errors.append(f"{plabel}: {label.lower()} must be a number.")
                out[p][k] = None
    return out, errors


def parse_metar(text):
    """Wind, gusts, visibility and ceiling from a METAR or TAF line. Returns what it could find."""
    t = " " + (text or "").upper() + " "
    out = {}
    m = re.search(r"\s(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\s", t)
    if m:
        out["wind_dir"] = m.group(1)
        out["wind"] = int(m.group(2))
        if m.group(3):
            out["gust"] = int(m.group(3))
    m = re.search(r"\s(P|M)?(?:(\d+)\s)?(\d+/\d+|\d+)SM\s", t)
    if m:
        whole = int(m.group(2) or 0)
        frac = m.group(3)
        if "/" in frac:
            a, b = frac.split("/")
            val = whole + int(a) / max(int(b), 1)
        else:
            val = whole + int(frac)
        out["visibility"] = round(val, 2)
    layers = [int(h) * 100 for _, h in re.findall(r"\s(BKN|OVC|VV)(\d{3})", t)]
    if layers:
        out["ceiling"] = min(layers)
    elif re.search(r"\s(CLR|SKC|NSC|NCD|CAVOK)\s", t) or re.search(r"\s(FEW|SCT)\d{3}", t):
        out["ceiling"] = None
        out["no_ceiling"] = True
    return out


def runway_heading(rwy):
    m = re.fullmatch(r"\s*(\d{1,3})\s*[LRC]?\s*", rwy or "", re.I)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 36:
        n *= 10
    return n % 360 if 0 < n <= 360 else None


def wind_components(wind_dir, speed, rwy_hdg):
    """(headwind, crosswind) in knots. Variable wind counts as all crosswind."""
    if speed is None or rwy_hdg is None:
        return None, None
    if wind_dir in (None, "", "VRB"):
        return 0.0, float(speed)
    ang = math.radians(int(wind_dir) - rwy_hdg)
    return round(speed * math.cos(ang), 1), round(abs(speed * math.sin(ang)), 1)


def check_minimums(mins, cond):
    """Compare conditions with one set of minimums. Every line says go, no-go or not checked."""
    lines = []

    def add(label, actual, limit, ok, unit):
        lines.append({"label": label, "actual": actual, "limit": limit, "unit": unit,
                      "state": "skip" if ok is None else ("go" if ok else "nogo")})

    def num(v):
        return None if v in (None, "") else v

    ceil, vis = num(cond.get("ceiling")), num(cond.get("visibility"))
    if cond.get("no_ceiling"):
        add("Ceiling", "None", mins.get("ceiling"), True if mins.get("ceiling") is not None else None, "ft")
    else:
        add("Ceiling", ceil, mins.get("ceiling"),
            None if ceil is None or mins.get("ceiling") is None else ceil >= mins["ceiling"], "ft")
    add("Visibility", vis, mins.get("visibility"),
        None if vis is None or mins.get("visibility") is None else vis >= mins["visibility"], "SM")
    wind, gust = num(cond.get("wind")), num(cond.get("gust"))
    add("Wind", wind, mins.get("wind"), None if wind is None or mins.get("wind") is None else wind <= mins["wind"], "kt")
    add("Gusts", gust, mins.get("gust"), None if mins.get("gust") is None or wind is None
        else (gust or wind) <= mins["gust"], "kt")
    spread = (gust - wind) if gust is not None and wind is not None else (0 if wind is not None else None)
    add("Gust spread", spread, mins.get("gust_spread"),
        None if spread is None or mins.get("gust_spread") is None else spread <= mins["gust_spread"], "kt")
    hdg = runway_heading(cond.get("runway_id"))
    head, xw = wind_components(cond.get("wind_dir"), max(wind or 0, gust or 0) if wind is not None else None, hdg)
    add("Crosswind" + (" (using gusts)" if gust else ""), xw, mins.get("crosswind"),
        None if xw is None or mins.get("crosswind") is None else xw <= mins["crosswind"], "kt")
    rl = num(cond.get("runway_length"))
    add("Runway length", rl, mins.get("runway"), None if rl is None or mins.get("runway") is None else rl >= mins["runway"], "ft")
    if cond.get("night"):
        add("Night", "Yes", "Allowed" if mins.get("night") else "Not allowed", bool(mins.get("night")), "")
    states = [x["state"] for x in lines]
    verdict = "nogo" if "nogo" in states else ("go" if "go" in states else "skip")
    return {"lines": lines, "verdict": verdict, "headwind": head, "crosswind": xw, "runway_hdg": hdg,
            "checked": sum(s != "skip" for s in states)}


# ----------------------------------------------------------------------------- costs

COST_FIELDS = [("aircraft_rate", "Aircraft rate ($/hr)"), ("instructor_rate", "Instructor rate ($/hr)"),
               ("fuel_gal", "Fuel added (gal)"), ("fuel_cost", "Fuel cost ($)"), ("other_cost", "Other costs ($)")]


def rates_get(prof):
    r = _json(prof, "rates", {})
    ac = r.get("aircraft") if isinstance(r.get("aircraft"), dict) else {}
    return {"instructor": r.get("instructor"), "aircraft": {k: v for k, v in ac.items() if isinstance(v, (int, float))}}


def parse_costs(form):
    """Cost fields from the flight form. Blank stays None (falls back to your default rates)."""
    out, errors = {}, []
    for k, label in COST_FIELDS:
        raw = (form.get(k) or "").strip().replace("$", "").replace(",", "")
        if not raw:
            out[k] = None
            continue
        try:
            v = round(float(raw), 2)
            if v < 0 or v > 1_000_000:
                raise ValueError
            out[k] = v
        except ValueError:
            errors.append(f"{label} must be a number.")
            out[k] = None
    return out, errors


def save_costs(db, flight_id, costs):
    if all(v is None for v in costs.values()):
        db.execute("DELETE FROM flight_costs WHERE flight_id=?", (flight_id,))
    else:
        db.execute("INSERT INTO flight_costs(flight_id, aircraft_rate, instructor_rate, fuel_gal, fuel_cost, other_cost) "
                   "VALUES (?,?,?,?,?,?) ON CONFLICT(flight_id) DO UPDATE SET aircraft_rate=excluded.aircraft_rate, "
                   "instructor_rate=excluded.instructor_rate, fuel_gal=excluded.fuel_gal, fuel_cost=excluded.fuel_cost, "
                   "other_cost=excluded.other_cost",
                   (flight_id, *[costs[k] for k, _ in COST_FIELDS]))
    db.commit()


def costs_for(db, flight_id):
    r = db.execute("SELECT * FROM flight_costs WHERE flight_id=?", (flight_id,)).fetchone()
    return dict(r) if r else {}


def billed_hours(r):
    hs, he = r["hobbs_start"] or 0, r["hobbs_end"] or 0
    if hs and he and 0 < he - hs < 24:
        return round(he - hs, 2), "Hobbs"
    return r["total"] or 0, "total time"


def flight_cost(r, c, rates):
    """Cost of one flight from its own entries, falling back to your default rates."""
    hours, _ = billed_hours(r)
    ar = c.get("aircraft_rate")
    ar = ar if ar is not None else rates["aircraft"].get(r["aircraft"] or "")
    ir = c.get("instructor_rate")
    ir = ir if ir is not None else rates.get("instructor")
    inst_hours = (r["dual_received"] or 0) + (r["ground_training"] or 0)
    aircraft = (ar or 0) * hours
    instructor = (ir or 0) * inst_hours if inst_hours else 0.0
    fuel, other = c.get("fuel_cost") or 0.0, c.get("other_cost") or 0.0
    return {"aircraft": aircraft, "instructor": instructor, "fuel": fuel, "other": other,
            "total": aircraft + instructor + fuel + other, "fuel_gal": c.get("fuel_gal") or 0.0,
            "missing_rate": ar is None and hours > 0, "missing_instr": ir is None and inst_hours > 0,
            "entered": bool(c)}


def cost_summary(db, rates, today=None, remaining_hours=None):
    """Spending totals. remaining_hours: flight hours still needed (for the projection)."""
    today = today or date.today()
    costs = {r["flight_id"]: dict(r) for r in db.execute("SELECT * FROM flight_costs")}
    rows = db.execute("SELECT * FROM flights ORDER BY date, id").fetchall()
    t = {"aircraft": 0.0, "instructor": 0.0, "fuel": 0.0, "other": 0.0, "total": 0.0, "fuel_gal": 0.0,
         "hours": 0.0, "fuel_hours": 0.0, "missing_rate": 0, "missing_instr": 0, "entered": 0, "flights": len(rows)}
    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append({"key": f"{y:04d}-{m:02d}", "year": y, "month": m, "total": 0.0, "aircraft": 0.0,
                       "instructor": 0.0, "fuel": 0.0, "other": 0.0})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    by_month = {x["key"]: x for x in months}
    by_ac, recent = {}, []
    for r in rows:
        c = flight_cost(r, costs.get(r["id"], {}), rates)
        for k in ("aircraft", "instructor", "fuel", "other", "total", "fuel_gal"):
            t[k] += c[k]
        t["hours"] += r["total"] or 0
        if c["fuel_gal"]:
            t["fuel_hours"] += r["total"] or 0
        t["missing_rate"] += c["missing_rate"]
        t["missing_instr"] += c["missing_instr"]
        t["entered"] += c["entered"]
        mm = by_month.get(r["date"][:7])
        if mm:
            for k in ("aircraft", "instructor", "fuel", "other", "total"):
                mm[k] += c[k]
        a = by_ac.setdefault(r["aircraft"] or "?", {"ident": r["aircraft"] or "?", "hours": 0.0, "total": 0.0})
        a["hours"] += r["total"] or 0
        a["total"] += c["total"]
        recent.append((r, c))
    t["per_hour"] = t["total"] / t["hours"] if t["hours"] else 0.0
    t["per_gal"] = t["fuel"] / t["fuel_gal"] if t["fuel_gal"] and t["fuel"] else 0.0
    t["gph"] = t["fuel_gal"] / t["fuel_hours"] if t["fuel_hours"] else 0.0
    remaining = max(0.0, 40 - t["hours"]) if remaining_hours is None else max(0.0, remaining_hours)
    return {"t": t, "months": months, "aircraft": sorted(by_ac.values(), key=lambda a: -a["total"]),
            "recent": list(reversed(recent))[:15], "remaining_hours": remaining,
            "projection": remaining * t["per_hour"]}


# ----------------------------------------------------------------------------- expiry alerts

def expiring_items(db, prof, today=None):
    """Everything with an end date that the pilot might want a reminder about."""
    today = today or date.today()
    items = []
    if prof.get("medical_expires") and valid_date(prof["medical_expires"]):
        items.append(("medical", "Medical certificate", _d(prof["medical_expires"])))
    review = prof.get("flight_review") or ""
    flagged = db.execute("SELECT MAX(date) FROM flights WHERE flag_flight_review=1").fetchone()[0]
    if flagged and flagged > review:
        review = flagged
    if review and valid_date(review):
        items.append(("review", "Flight review", core.flight_review_through(review)))
    for r in db.execute("SELECT id, title, expires FROM documents WHERE expires IS NOT NULL AND expires!=''"):
        if valid_date(r["expires"]):
            items.append((f"doc{r['id']}", r["title"], _d(r["expires"])))
    for r in endorsements(db, today):
        if r["expires"] and not r["superseded"] and valid_date(r["expires"]):
            items.append((f"end{r['id']}", r["title"], _d(r["expires"])))
        if r["cfi_cert_expires"] and not r["superseded"] and valid_date(r["cfi_cert_expires"]):
            items.append((f"cfi{r['cfi_cert'] or r['id']}", f"Instructor certificate ({r['cfi_name'] or 'CFI'})",
                          _d(r["cfi_cert_expires"])))
    for k in core.currency(db, today):
        if k["day_through"]:
            items.append((f"curday{k['class']}", f"Passenger currency, day ({k['label']})", k["day_through"]))
        if k["night_through"]:
            items.append((f"curnight{k['class']}", f"Passenger currency, night ({k['label']})", k["night_through"]))
    seen, out = set(), []
    for key, label, through in items:
        if key not in seen:
            seen.add(key)
            out.append({"key": key, "label": label, "through": through, "days": (through - today).days})
    return sorted(out, key=lambda x: x["through"])


def alert_settings(prof):
    a = _json(prof, "alerts", {})
    days = a.get("days") if isinstance(a.get("days"), list) else [30, 7, 0]
    return {"url": a.get("url") or "", "token": a.get("token") or "", "enabled": bool(a.get("enabled")),
            "days": sorted({int(d) for d in days if isinstance(d, (int, float)) and 0 <= d <= 365}, reverse=True) or [30, 7, 0],
            "last_run": a.get("last_run") or "", "last_error": a.get("last_error") or "",
            "last_sent": a.get("last_sent") or ""}


def parse_alert_settings(form, old):
    errors = []
    url = (form.get("url") or "").strip()
    if url and not re.fullmatch(r"https?://[^\s]{3,500}", url):
        errors.append("The alert address must start with http:// or https://.")
    token = (form.get("token") or "").strip()[:300]
    if form.get("clear_token"):
        token = ""
    elif not token:
        token = old.get("token", "")
    try:
        days = sorted({int(x) for x in re.split(r"[,\s]+", form.get("days") or "") if x.strip()}, reverse=True)
        if any(d < 0 or d > 365 for d in days):
            raise ValueError
    except ValueError:
        errors.append("Days before must be whole numbers from 0 to 365, separated by commas.")
        days = [30, 7, 0]
    enabled = bool(form.get("enabled")) and bool(url)
    return {"url": url, "token": token, "enabled": enabled, "days": days or [30, 7, 0],
            "last_run": old.get("last_run", ""), "last_error": old.get("last_error", ""),
            "last_sent": old.get("last_sent", "")}, errors


def send_alert(settings, title, message, high=False, timeout=10):
    """POST a plain-text message. Works with ntfy (ntfy.sh or the ntfy app on umbrelOS) and simple webhooks."""
    headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": "airplane",
               "Priority": "high" if high else "default", "Content-Type": "text/plain; charset=utf-8",
               "User-Agent": "pilot-logbook"}
    if settings.get("token"):
        headers["Authorization"] = "Bearer " + settings["token"]
    req = urllib.request.Request(settings["url"], data=message.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - address set by the pilot
        if resp.status >= 300:
            raise urllib.error.HTTPError(settings["url"], resp.status, "bad status", resp.headers, None)


def due_alerts(db, prof, settings, today=None):
    """Reminders that have reached one of the chosen thresholds and haven't gone out yet."""
    today = today or date.today()
    sent = {r[0] for r in db.execute("SELECT key FROM alerts_sent")}
    out = []
    for it in expiring_items(db, prof, today):
        if it["days"] < 0:
            continue
        hits = [d for d in settings["days"] if it["days"] <= d]
        if not hits:
            continue
        thr = min(hits)
        key = f"{it['key']}|{it['through'].isoformat()}|{thr}"
        if key in sent:
            continue
        # also mark the wider thresholds as done, so a late start sends one message, not three
        keys = [f"{it['key']}|{it['through'].isoformat()}|{d}" for d in hits]
        out.append(dict(it, threshold=thr, keys=keys))
    return out


def alert_text(it):
    d = it["days"]
    when = "today" if d == 0 else "tomorrow" if d == 1 else f"in {d} days"
    return (f"{it['label']}: expires {when}",
            f"{it['label']} is good through {it['through'].strftime('%b %-d, %Y')}. "
            "Open your Pilot Logbook for details.")


def mark_sent(db, keys):
    now = datetime.now().isoformat(timespec="seconds")
    db.executemany("INSERT OR IGNORE INTO alerts_sent(key, sent_at) VALUES (?,?)", [(k, now) for k in keys])
    db.commit()


# ----------------------------------------------------------------------------- specific-flight requirements (61.109(a))

def leg_nm(a, b):
    """Great-circle distance between two airports in nautical miles."""
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(h)))


def flight_geometry(row):
    """Airports, legs and distances for one flight. Distance you typed wins over the computed one."""
    pts = []
    for code in route_points(row):
        ap = resolve_airport(code)
        if ap and (not pts or pts[-1][1]["icao"] != ap["icao"]):
            pts.append((code, ap))
    legs = [(a[0], b[0], leg_nm(a[1], b[1])) for a, b in zip(pts, pts[1:])]
    computed = sum(n for *_, n in legs)
    entered = row["distance"] or 0
    return {"points": pts, "legs": legs, "computed": computed, "entered": entered,
            "distance": entered if entered > 0 else computed,
            "source": "entered" if entered > 0 else ("route" if legs else "none"),
            "longest_leg": max((n for *_, n in legs), default=0.0),
            "stops": len({ap["icao"] for _, ap in pts[1:]})}


def _route_label(row):
    seq = route_points(row)
    return " → ".join(seq) if seq else "no route"


def specific_requirements(db, today=None):
    """The 61.109(a) items that need one particular flight, checked from distance, route and landings."""
    today = today or date.today()
    single = ("f.aircraft IN (SELECT ident FROM aircraft WHERE aircraft_class LIKE 'airplane%single%')")
    rows = db.execute(f"SELECT f.* FROM flights f WHERE {single} ORDER BY f.date").fetchall()
    out = []

    # (a)(2)(i) one night cross-country over 100 nm total distance, with an instructor
    best = None
    for r in rows:
        if (r["night"] or 0) > 0 and (r["xc"] or 0) > 0 and (r["dual_received"] or 0) > 0:
            g = flight_geometry(r)
            if not best or g["distance"] > best[1]["distance"]:
                best = (r, g)
    met = bool(best and best[1]["distance"] > 100)
    if best:
        detail = (f'{"Met" if met else "Longest so far"}: {best[0]["date"]}, {_route_label(best[0])}, '
                  f'{best[1]["distance"]:.0f} nm ({"distance you entered" if best[1]["source"] == "entered" else "from the route"}).')
    else:
        detail = "No flight yet with night, cross-country and dual received time all logged."
    out.append({"label": "Night cross-country with an instructor, over 100 nm total", "far": "61.109(a)(2)(i)",
                "met": met, "detail": detail, "date": best[0]["date"] if met else None})

    # (a)(5)(ii) solo cross-country: 150 nm total, full-stop landings at three points, one leg over 50 nm
    best, best_score = None, -1
    for r in rows:
        if (r["solo"] or 0) <= 0 or (r["xc"] or 0) <= 0:
            continue
        g = flight_geometry(r)
        fs = (r["day_landings"] or 0) + (r["night_landings"] or 0)
        parts = {"distance": g["distance"] >= 150, "points": g["stops"] >= 3 and fs >= 3,
                 "leg": g["longest_leg"] > 50}
        score = sum(parts.values()) + min(g["distance"], 150) / 1000
        if score > best_score:
            best, best_score = (r, g, parts, fs), score
    if best:
        r, g, parts, fs = best
        met = all(parts.values())
        bits = [f'{g["distance"]:.0f} nm total{"" if parts["distance"] else " (needs 150)"}',
                f'{g["stops"]} landing point{"s" if g["stops"] != 1 else ""} and {fs} full-stop landing{"s" if fs != 1 else ""}'
                + ("" if parts["points"] else " (needs 3 points)"),
                (f'longest leg {g["longest_leg"]:.0f} nm' + ("" if parts["leg"] else " (needs one over 50)"))
                if g["legs"] else "longest leg unknown: put the airports in the route"]
        detail = f'{"Met" if met else "Closest so far"}: {r["date"]}, {_route_label(r)}. ' + "; ".join(bits) + "."
    else:
        met, detail = False, "No solo cross-country flight logged yet."
    out.append({"label": "Solo cross-country of 150 nm, full-stop landings at 3 points, one leg over 50 nm",
                "far": "61.109(a)(5)(ii)", "met": met, "detail": detail, "date": best[0]["date"] if met else None})

    # (a)(4) 3 hours of training within the 2 calendar months before the month of the test
    start = end_of_month(today, -3) + timedelta(days=1)
    h = sum((r["dual_received"] or 0) for r in rows if start.isoformat() <= r["date"] <= today.isoformat())
    out.append({"label": "3 hours of instruction preparing for the practical test", "far": "61.109(a)(4)",
                "met": h >= 3, "have": h, "need": 3, "hours": True,
                "detail": f'If your checkride is this month: {h:.1f} h of dual since {start.strftime("%b %-d")}. '
                          "Your instructor confirms it was test preparation."})
    return out


def map_payload(stats, nearby_limit=500):
    """JSON for the interactive map: your airports, the legs between them and other airports around them."""
    aps = stats["airports"]
    if not aps:
        return "null"
    by_icao = _airports()[0]
    home = aps[0]["ap"]["icao"] if aps else None
    mine = [{"code": a["code"], "icao": a["ap"]["icao"], "name": a["ap"]["name"],
             "place": ", ".join(x for x in (a["ap"]["city"], a["ap"]["subd"]) if x),
             "lat": a["ap"]["lat"], "lon": a["ap"]["lon"], "visits": a["visits"], "landings": a["landings"],
             "first": a["first"], "last": a["last"], "home": a["ap"]["icao"] == home} for a in aps]
    legs = []
    for (i1, i2), n in stats["legs"].items():
        p1, p2 = by_icao[i1], by_icao[i2]
        legs.append({"a": [p1["lat"], p1["lon"]], "b": [p2["lat"], p2["lon"]], "n": n,
                     "label": f'{p1["lid"] or i1} – {p2["lid"] or i2}', "nm": round(leg_nm(p1, p2))})
    lats = [m["lat"] for m in mine]
    lons = [m["lon"] for m in mine]
    pad = 0.6
    lo_lat, hi_lat, lo_lon, hi_lon = min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad
    c_lat, c_lon = (lo_lat + hi_lat) / 2, (lo_lon + hi_lon) / 2
    seen = {m["icao"] for m in mine}
    near = [ap for ap in by_icao.values()
            if ap["icao"] not in seen and lo_lat <= ap["lat"] <= hi_lat and lo_lon <= ap["lon"] <= hi_lon
            and "heliport" not in ap["name"].lower()]
    near.sort(key=lambda ap: (ap["lat"] - c_lat) ** 2 + (ap["lon"] - c_lon) ** 2)
    others = [{"code": ap["lid"] or ap["icao"], "name": ap["name"], "lat": ap["lat"], "lon": ap["lon"]}
              for ap in near[:nearby_limit]]
    return json.dumps({"airports": mine, "legs": legs, "others": others}, separators=(",", ":")).replace("</", "<\\/")
