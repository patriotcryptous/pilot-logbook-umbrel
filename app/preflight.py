"""Preflight Risk Check: live weather for an airport, runway winds, density altitude, sun times,
logbook readiness and a PAVE risk score.

Weather comes from the NOAA Aviation Weather Center data API (aviationweather.gov), fetched by the
server only when the pilot asks for a check. Everything else is worked out here, offline.
"""
import csv
import gzip
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import features
import logbook_core as core

AWC = "https://aviationweather.gov/api/data/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS preflight_checks (
    id INTEGER PRIMARY KEY, created_at TEXT, airport TEXT, station TEXT, profile TEXT,
    verdict TEXT, risk_total INTEGER, risk_level TEXT, summary TEXT, details_json TEXT
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()
_cache = {}


# ----------------------------------------------------------------------------- runways

@lru_cache(maxsize=1)
def _runways():
    out = {}
    with gzip.open(features.GEO / "runways.csv.gz", "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rec = {"length": int(float(r["length"] or 0)), "width": int(float(r["width"] or 0)),
                   "surface": r["surface"], "lighted": r["lighted"] == "1",
                   "ends": [(r["le"], r["le_hdg"]), (r["he"], r["he_hdg"])]}
            for k in r["keys"].split("|"):
                out.setdefault(k, []).append(rec)
    return out


def runways_for(ap):
    rw = _runways()
    for k in (ap["icao"], ap.get("lid"), ap.get("iata")):
        if k and k in rw:
            return rw[k]
    return []


SURFACES = {"ASP": "Asphalt", "ASPH": "Asphalt", "CON": "Concrete", "CONC": "Concrete", "GRS": "Grass",
            "TURF": "Turf", "GRE": "Gravel", "GRVL": "Gravel", "DIRT": "Dirt", "WATER": "Water"}


def surface_name(s):
    s = (s or "").upper()
    for k, v in SURFACES.items():
        if s.startswith(k):
            return v
    return s.title() or "Unknown"


def _end_heading(ident, hdg):
    try:
        if hdg not in (None, ""):
            return float(hdg) % 360
    except ValueError:
        pass
    return features.runway_heading(ident)


def runway_winds(runways, wdir, wspd, gust):
    """Headwind and crosswind on every runway end. Variable wind counts as all crosswind."""
    speed = max(wspd or 0, gust or 0)
    ends = []
    for rw in runways:
        for ident, hdg in rw["ends"]:
            h = _end_heading(ident, hdg)
            if h is None or not ident:
                continue
            if wdir in (None, "VRB") or speed == 0:
                head, cross = (0.0, float(speed) if wdir == "VRB" else 0.0)
            else:
                ang = math.radians(float(wdir) - h)
                head, cross = speed * math.cos(ang), abs(speed * math.sin(ang))
            ends.append({"ident": ident, "heading": round(h), "length": rw["length"], "width": rw["width"],
                         "surface": surface_name(rw["surface"]), "lighted": rw["lighted"],
                         "head": round(head, 1), "cross": round(cross, 1)})
    ends.sort(key=lambda e: (-e["head"], e["cross"], -e["length"]))
    if ends:
        ends[0]["best"] = True
    return ends


# ----------------------------------------------------------------------------- weather

def _get(path, params, timeout=8):
    url = AWC + path + "?" + urllib.parse.urlencode(params)
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "pilot-logbook (self-hosted umbrelOS app)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed NOAA host
        body = resp.read()
    data = json.loads(body) if body.strip() else []
    _cache[url] = (time.time(), data)
    return data


def _nearest(items, ap, max_nm):
    best = None
    for it in items:
        if it.get("lat") is None or it.get("lon") is None:
            continue
        d = features.leg_nm(ap, {"lat": it["lat"], "lon": it["lon"]})
        if d <= max_nm and (best is None or d < best[0]):
            best = (d, it)
    return best


def _visib(v):
    if v in (None, ""):
        return None
    s = str(v).replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def decode_metar(m):
    clouds = m.get("clouds") or []
    layers = [c["base"] for c in clouds if c.get("cover") in ("BKN", "OVC", "OVX") and c.get("base") is not None]
    if m.get("vertVis"):
        layers.append(m["vertVis"])
    altim_hpa = m.get("altim")
    wdir = m.get("wdir")
    if isinstance(wdir, str) and wdir.upper().startswith("VRB"):
        wdir = "VRB"
    return {"raw": m.get("rawOb", ""), "station": m.get("icaoId", ""), "name": m.get("name", ""),
            "obs": datetime.fromtimestamp(m["obsTime"], tz=timezone.utc) if m.get("obsTime") else None,
            "wdir": wdir, "wspd": m.get("wspd"), "gust": m.get("wgst"), "visib": _visib(m.get("visib")),
            "visib_text": str(m.get("visib") or ""), "ceiling": min(layers) if layers else None,
            "clouds": clouds, "temp": m.get("temp"), "dewp": m.get("dewp"),
            "altim": round(altim_hpa / 33.8639, 2) if altim_hpa else None,
            "category": m.get("fltCat") or "", "lat": m.get("lat"), "lon": m.get("lon")}


def weather_for(ap, now=None):
    """Latest METAR and TAF at the airport, or at the nearest reporting station within 30 nm."""
    now = now or datetime.now(timezone.utc)
    out = {"metar": None, "taf": None, "from_nearby": False, "distance": 0.0, "error": ""}
    try:
        data = _get("metar", {"ids": ap["icao"], "format": "json"})
        if data:
            out["metar"] = decode_metar(data[0])
        else:
            box = {"bbox": f'{ap["lat"] - .5:.2f},{ap["lon"] - .6:.2f},{ap["lat"] + .5:.2f},{ap["lon"] + .6:.2f}',
                   "format": "json"}
            near = _nearest(_get("metar", box), ap, 30)
            if near:
                out["metar"], out["from_nearby"], out["distance"] = decode_metar(near[1]), True, near[0]
        station = out["metar"]["station"] if out["metar"] else ap["icao"]
        tafs = _get("taf", {"ids": station, "format": "json"})
        if not tafs:
            box = {"bbox": f'{ap["lat"] - 1:.2f},{ap["lon"] - 1.2:.2f},{ap["lat"] + 1:.2f},{ap["lon"] + 1.2:.2f}',
                   "format": "json"}
            near = _nearest(_get("taf", box), ap, 60)
            if near:
                out["taf"] = {"raw": near[1].get("rawTAF", ""), "station": near[1].get("icaoId", ""),
                              "distance": near[0]}
        else:
            out["taf"] = {"raw": tafs[0].get("rawTAF", ""), "station": tafs[0].get("icaoId", ""), "distance": 0.0}
    except Exception as e:  # noqa: BLE001 - offline, timeout or a changed API: say so and carry on
        out["error"] = f"Couldn't reach aviationweather.gov ({type(e).__name__}). Enter the weather yourself below."
    if out["metar"] and out["metar"]["obs"]:
        out["metar"]["age_min"] = int((now - out["metar"]["obs"]).total_seconds() // 60)
    return out


def density_altitude(elev_ft, temp_c, altim_inhg):
    if elev_ft is None or temp_c is None or not altim_inhg:
        return None
    pa = elev_ft + (29.92 - altim_inhg) * 1000
    isa = 15 - 2 * pa / 1000
    return {"pressure_alt": round(pa), "density_alt": round(pa + 120 * (temp_c - isa)), "isa_dev": round(temp_c - isa)}


# ----------------------------------------------------------------------------- sun

def _sun_event(d, lat, lon, zenith, rising):
    """UTC time of a sun event (Almanac for Computers method), or None if it doesn't happen that day."""
    n = d.timetuple().tm_yday
    lng_hour = lon / 15
    t = n + ((6 if rising else 18) - lng_hour) / 24
    m = 0.9856 * t - 3.289
    sl = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(sl)))) % 360
    ra = (ra + (math.floor(sl / 90) * 90 - math.floor(ra / 90) * 90)) / 15
    sin_dec = 0.39782 * math.sin(math.radians(sl))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(math.radians(zenith)) - sin_dec * math.sin(math.radians(lat))) / (cos_dec * math.cos(math.radians(lat)))
    if cos_h > 1 or cos_h < -1:
        return None
    h = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    ut = (h / 15 + ra - 0.06571 * t - 6.622 - lng_hour) % 24
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(hours=ut)


def sun_times(ap, d=None):
    tz = ZoneInfo(ap.get("tz") or "UTC")
    d = d or datetime.now(tz).date()

    def local(x):
        if not x:
            return None
        x = x.astimezone(tz)
        if x.date() != d:  # the formula can land on the neighbouring UTC day
            x = x + timedelta(days=(d - x.date()).days)
        return x

    rise = local(_sun_event(d, ap["lat"], ap["lon"], 90.833, True))
    sset = local(_sun_event(d, ap["lat"], ap["lon"], 90.833, False))
    dawn = local(_sun_event(d, ap["lat"], ap["lon"], 96, True))
    dusk = local(_sun_event(d, ap["lat"], ap["lon"], 96, False))
    return {"date": d, "tz": tz.key, "sunrise": rise, "sunset": sset, "civil_dawn": dawn, "civil_dusk": dusk,
            "lights_on": sset, "night_log": dusk,
            "night_currency": sset + timedelta(hours=1) if sset else None,
            "night_currency_end": rise - timedelta(hours=1) if rise else None}


# ----------------------------------------------------------------------------- readiness from the logbook

def readiness(db, prof, reminders, tail="", airport=None, today=None):
    today = today or date.today()
    facts = []

    def add(label, ok, text):
        facts.append({"label": label, "state": "ok" if ok is True else "bad" if ok is False else "info", "text": text})

    last = db.execute("SELECT MAX(date) FROM flights").fetchone()[0]
    if last:
        days = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
        add("Last flight", days <= 30, f"{days} day{'s' if days != 1 else ''} ago" + ("" if days <= 30 else ", consider flying with an instructor first"))
    h90 = db.execute("SELECT COALESCE(SUM(total),0) FROM flights WHERE date>?", ((today - timedelta(days=90)).isoformat(),)).fetchone()[0]
    add("Hours in the last 90 days", None, f"{h90:.1f} h")
    for k in core.currency(db, today)[:2]:
        add(f"Passengers by day ({k['label']})", k["day_ok"],
            f"current through {k['day_through'].strftime('%b %-d')}" if k["day_ok"] else f"not current ({k['day_count']} of 3 landings)")
        add(f"Passengers at night ({k['label']})", k["night_ok"],
            f"current through {k['night_through'].strftime('%b %-d')}" if k["night_ok"] else f"not current ({k['night_count']} of 3 full-stop landings)")
    for r in reminders:
        add(r["label"], r["ok"], f"{'valid' if r['ok'] else 'expired'} through {r['through'].strftime('%b %-d, %Y')}")
    solo = db.execute("SELECT expires FROM endorsements WHERE kind='solo' ORDER BY date DESC, id DESC LIMIT 1").fetchone()
    student = "student" in (prof.get("certificate") or "").lower()
    if solo or student:
        if solo and solo[0]:
            ok = solo[0] >= today.isoformat()
            add("Solo endorsement (61.87(n))", ok, f"{'good' if ok else 'expired'} through {datetime.strptime(solo[0], '%Y-%m-%d').strftime('%b %-d, %Y')}")
        else:
            add("Solo endorsement (61.87(n))", False, "none recorded on the Endorsements page")
    if tail:
        hrs, n = db.execute("SELECT COALESCE(SUM(total),0), COUNT(*) FROM flights WHERE aircraft=?", (tail,)).fetchone()
        model = db.execute("SELECT model FROM aircraft WHERE ident=?", (tail,)).fetchone()
        add(f"Time in {tail}", hrs >= 10, f"{hrs:.1f} h over {n} flight{'s' if n != 1 else ''}" + (f" ({model[0]})" if model and model[0] else ""))
    if airport:
        seen = db.execute("SELECT COUNT(*) FROM flights WHERE upper(from_apt)=? OR upper(to_apt)=? OR upper(route) LIKE ?",
                          (airport, airport, f"%{airport}%")).fetchone()[0]
        add(f"Flown at {airport}", seen > 0, f"{seen} flight{'s' if seen != 1 else ''} in your logbook" if seen else "first time at this airport")
    return facts


# ----------------------------------------------------------------------------- PAVE risk assessment

# (key, group, label, points). Points follow the usual FAA-style flight risk assessment: add them up.
RISK_ITEMS = [
    ("ill", "P", "I'm ill or have symptoms", 5),
    ("meds", "P", "Taking medication that could affect me", 5),
    ("stress", "P", "Under stress or upset", 3),
    ("alcohol", "P", "Alcohol in the last 8 hours, or still feeling its effects", 99),
    ("fatigue", "P", "Tired, or less than 8 hours' sleep", 3),
    ("eating", "P", "Haven't eaten or I'm dehydrated", 1),
    ("rusty", "P", "No flying in the last 30 days", 3),
    ("new_type", "P", "Less than 10 hours in this aircraft", 2),
    ("new_airport", "P", "First time at this airport", 2),
    ("squawks", "A", "Open squawks or inoperative equipment", 3),
    ("wb", "A", "Weight and balance near limits, or not worked out", 3),
    ("fuel_tight", "A", "Planned to land with less than 1 hour of fuel", 3),
    ("fuel_legal", "A", "Fuel doesn't cover the trip plus the legal reserve (91.151)", 99),
    ("perf", "A", "Takeoff or landing distance over half the runway", 2),
    ("unfamiliar_avionics", "A", "Unfamiliar avionics or equipment", 2),
    ("night", "V", "Night flight", 2),
    ("mvfr", "V", "Marginal VFR at departure or destination", 3),
    ("ifr", "V", "IFR or worse at departure or destination", 5),
    ("crosswind", "V", "Crosswind over 80% of your limit", 2),
    ("density_alt", "V", "Density altitude over 5,000 ft", 2),
    ("terrain", "V", "Mountains, large water or few landing options en route", 2),
    ("airspace", "V", "Busy or unfamiliar airspace (Class B or C, TFRs)", 2),
    ("thunder", "V", "Thunderstorms or icing forecast near the route", 5),
    ("passengers", "E", "Passengers counting on the trip", 2),
    ("schedule", "E", "Must be somewhere at a set time", 3),
    ("get_home", "E", "Need to get back today", 3),
    ("no_plan_b", "E", "No backup plan if the weather turns", 3),
    ("pressure", "E", "Pressure from a checkride, stage check or rental deadline", 1),
]
RISK_GROUPS = [("P", "Pilot", "IMSAFE: illness, medication, stress, alcohol, fatigue, eating, plus recency."),
               ("A", "Aircraft", "Airworthy, equipped, fueled and within its limits for this flight."),
               ("V", "enVironment", "Weather, terrain, airspace, runways and lighting."),
               ("E", "External pressures", "Things that push you to go when you shouldn't.")]
RISK_POINTS = {k: p for k, _, _, p in RISK_ITEMS}


def fuel_check(flight_h, fob_gal, gph, night):
    if not flight_h or not fob_gal or not gph:
        return None
    reserve = 0.75 if night else 0.5
    need = (flight_h + reserve) * gph
    left_h = fob_gal / gph - flight_h
    return {"reserve_h": reserve, "required": round(need, 1), "left_h": round(left_h, 2),
            "legal": fob_gal >= need, "hour_left": left_h >= 1.0}


def auto_risks(ctx):
    """Risk items the app can tick for you from the weather, the logbook and the fuel numbers."""
    auto = {}
    w = (ctx.get("wx") or {}).get("metar")
    if w:
        cat = (w.get("category") or "").upper()
        if cat == "MVFR":
            auto["mvfr"] = "METAR shows marginal VFR"
        if cat in ("IFR", "LIFR"):
            auto["ifr"] = f"METAR shows {cat}"
    if ctx.get("night"):
        auto["night"] = "You marked this as a night flight"
    lim = (ctx.get("mins") or {}).get("crosswind")
    xw = ctx.get("crosswind")
    if lim and xw is not None and xw > 0.8 * lim:
        auto["crosswind"] = f"{xw:.0f} kt crosswind against your {lim:g} kt limit"
    da = ctx.get("da")
    if da and da["density_alt"] > 5000:
        auto["density_alt"] = f"Density altitude {da['density_alt']:,} ft"
    for f in ctx.get("facts", []):
        if f["label"] == "Last flight" and f["state"] == "bad":
            auto["rusty"] = f["text"].split(",")[0]
        if f["label"].startswith("Time in ") and f["state"] == "bad":
            auto["new_type"] = f["text"]
        if f["label"].startswith("Flown at ") and f["state"] == "bad":
            auto["new_airport"] = "First time at this airport"
    fc = ctx.get("fuel")
    if fc:
        if not fc["legal"]:
            auto["fuel_legal"] = f"Need {fc['required']} gal including the {int(fc['reserve_h'] * 60)}-minute reserve"
        elif not fc["hour_left"]:
            auto["fuel_tight"] = f"About {fc['left_h']:.1f} h of fuel left at landing"
    rl = ctx.get("runway_len")
    if ctx.get("tod") and rl and ctx["tod"] > rl / 2:
        auto["perf"] = f"{ctx['tod']:,} ft needed on a {rl:,} ft runway"
    return auto


def score_risks(checked):
    total = sum(RISK_POINTS[k] for k in checked if k in RISK_POINTS)
    nogo = [k for k in checked if RISK_POINTS.get(k) == 99]
    if nogo:
        level = "high"
    elif total >= 20:
        level = "high"
    elif total >= 10:
        level = "caution"
    else:
        level = "low"
    shown = sum(RISK_POINTS[k] for k in checked if RISK_POINTS.get(k, 0) < 99)
    return {"total": shown, "level": level, "nogo": nogo,
            "text": {"low": "Low risk. Fly your normal plan and keep watching for changes.",
                     "caution": "Elevated risk. Lower the risk you can (fuel, timing, route, an instructor along) or wait.",
                     "high": "High risk. Don't go as planned: change the plan, wait, or talk it through with your instructor."}[level]}


CHECKLISTS = [
    ("Preflight action (FAR 91.103)", [
        "Weather reports and forecasts for the whole route and time",
        "NOTAMs and TFRs for departure, destination and the route",
        "Fuel requirements, including the 91.151 reserve",
        "Alternatives if the flight can't be completed as planned",
        "Known traffic delays from ATC",
        "Runway lengths at each airport you'll use",
        "Takeoff and landing distances for today's weight, temperature and wind",
    ]),
    ("Required documents (ARROW)", [
        "Airworthiness certificate, displayed",
        "Registration certificate",
        "Radio station license (only for international flights)",
        "Operating limitations: POH/AFM, placards and markings",
        "Weight and balance for this aircraft",
    ]),
    ("Inspections current", [
        "Annual within the last 12 calendar months",
        "100-hour, if the aircraft is used for hire or flight instruction",
        "Transponder check within 24 calendar months (91.413)",
        "ELT inspection within 12 calendar months and battery in date (91.207)",
        "Airworthiness directives complied with",
    ]),
]

LINKS = [
    ("Flight Service briefing", "https://www.1800wxbrief.com"),
    ("Aviation Weather Center", "https://aviationweather.gov"),
    ("NOTAM search", "https://notams.aim.faa.gov/notamSearch/"),
    ("Temporary flight restrictions", "https://tfr.faa.gov"),
]


# ----------------------------------------------------------------------------- one question at a time

# key -> (question, the answer that adds the risk, a short hint)
QUESTIONS = {
    "ill": ("Are you feeling ill, or do you have any symptoms?", "y", "IMSAFE: Illness. Even a cold can block your ears in a descent."),
    "meds": ("Are you taking any medication that could affect you?", "y", "IMSAFE: Medication, including over-the-counter. Ask an AME if you're not sure."),
    "stress": ("Are you under stress or upset about anything?", "y", "IMSAFE: Stress from work, family or money follows you into the cockpit."),
    "alcohol": ("Have you had alcohol in the last 8 hours, or do you still feel its effects?", "y", "IMSAFE: Alcohol. FAR 91.17 makes this a no-go."),
    "fatigue": ("Are you tired, or did you get less than 8 hours of sleep?", "y", "IMSAFE: Fatigue."),
    "eating": ("Have you skipped a meal, or are you dehydrated?", "y", "IMSAFE: Eating and hydration."),
    "rusty": ("Has it been more than 30 days since you last flew?", "y", "Recency. Rusty skills show up first in the pattern."),
    "new_type": ("Do you have less than 10 hours in this aircraft?", "y", "Time in type."),
    "new_airport": ("Is this your first time at this airport?", "y", "Study the airport diagram, pattern altitude and entry first."),
    "squawks": ("Does the aircraft have open squawks or inoperative equipment?", "y", "FAR 91.213 decides whether you can fly with it inoperative."),
    "wb": ("Is weight and balance near the limits, or not worked out yet?", "y", "Work it out with today's fuel and passengers."),
    "fuel_tight": ("Will you land with less than 1 hour of fuel?", "y", "A common personal minimum is to land with an hour in the tanks."),
    "fuel_legal": ("Is your fuel short of the trip plus the legal reserve (30 minutes by day, 45 at night)?", "y", "FAR 91.151 makes this a no-go."),
    "perf": ("Is the takeoff or landing distance more than half the runway?", "y", "Use the POH for today's weight, temperature and wind."),
    "unfamiliar_avionics": ("Is the avionics or equipment unfamiliar to you?", "y", "A new panel adds workload when you least want it."),
    "mvfr": ("Is the weather marginal VFR at your departure or destination?", "y", "Ceiling 1,000 to 3,000 ft or visibility 3 to 5 miles."),
    "ifr": ("Is the weather IFR or worse at your departure or destination?", "y", "Ceiling below 1,000 ft or visibility below 3 miles."),
    "crosswind": ("Is the crosswind more than 80% of your personal limit?", "y", "Check the runway you'll actually use."),
    "density_alt": ("Is the density altitude over 5,000 ft?", "y", "Hot, high and humid means longer takeoff rolls and weaker climbs."),
    "terrain": ("Will you cross mountains, large water or areas with few landing options?", "y", "Plan where you'd go if the engine quit."),
    "airspace": ("Will you fly in busy or unfamiliar airspace, such as Class B or C, or near a TFR?", "y", "Check NOTAMs and TFRs for the route."),
    "thunder": ("Are thunderstorms or icing forecast near your route?", "y", "Give storms 20 miles; stay out of visible moisture below freezing."),
    "passengers": ("Are passengers counting on this trip?", "y", "External pressure: brief them that the flight might not happen."),
    "schedule": ("Do you have to be somewhere at a set time?", "y", "External pressure."),
    "get_home": ("Do you need to get back today?", "y", "Get-home-itis is behind many weather accidents."),
    "no_plan_b": ("Are you without a backup plan if the weather turns, such as an alternate airport, a hotel or a ride home?", "y", "Decide your plan B before you leave."),
    "pressure": ("Is a checkride, stage check or rental deadline pushing you to go?", "y", "External pressure."),
}


def known_keys(ctx):
    """Risk items the app can answer from data, so it doesn't ask them."""
    k = {"night"}
    if (ctx.get("wx") or {}).get("metar"):
        k |= {"mvfr", "ifr"}
    if ctx.get("crosswind") is not None and (ctx.get("mins") or {}).get("crosswind"):
        k.add("crosswind")
    if ctx.get("da"):
        k.add("density_alt")
    labels = [f["label"] for f in ctx.get("facts", [])]
    if "Last flight" in labels:
        k.add("rusty")
    if any(x.startswith("Time in ") for x in labels):
        k.add("new_type")
    if any(x.startswith("Flown at ") for x in labels):
        k.add("new_airport")
    if ctx.get("fuel"):
        k |= {"fuel_legal", "fuel_tight"}
    if ctx.get("tod") and ctx.get("runway_len"):
        k.add("perf")
    return k
