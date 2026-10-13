"""Reports: the IACRA (Form 8710-1) record of pilot time, a logbook check, and an insurance hours summary.
Also the ForeFlight changed-flight review used after an import."""
import io
import json
from datetime import date, datetime, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import features
import logbook_core as core

# ----------------------------------------------------------------------------- IACRA / 8710-1 record of pilot time

CATEGORY_ROWS = [
    ("airplane", "Airplanes", "COALESCE(a.aircraft_class,'') LIKE 'airplane%'"),
    ("rotorcraft", "Rotorcraft", "COALESCE(a.aircraft_class,'') LIKE 'rotorcraft%'"),
    ("powered_lift", "Powered lift", "COALESCE(a.aircraft_class,'') LIKE 'powered%lift%'"),
    ("glider", "Gliders", "COALESCE(a.aircraft_class,'') LIKE 'glider%'"),
    ("lta", "Lighter than air", "COALESCE(a.aircraft_class,'') LIKE 'lighter%'"),
]
COLUMNS_8710 = [
    ("total", "Total", "f.total"),
    ("instruction", "Instruction received", "f.dual_received"),
    ("solo", "Solo", "f.solo"),
    ("pic", "PIC", "f.pic"),
    ("sic", "SIC", "f.sic"),
    ("xc_instruction", "Cross-country instruction received", "MIN(f.xc, f.dual_received)"),
    ("xc_solo", "Cross-country solo", "MIN(f.xc, f.solo)"),
    ("xc_pic", "Cross-country PIC", "MIN(f.xc, f.pic)"),
    ("xc_sic", "Cross-country SIC", "MIN(f.xc, f.sic)"),
    ("instrument", "Instrument", "COALESCE(f.actual_instr,0) + COALESCE(f.sim_instr,0)"),
    ("night_instruction", "Night instruction received", "MIN(f.night, f.dual_received)"),
    ("night_ldg_instruction", "Night takeoffs/landings (instruction)",
     "CASE WHEN f.dual_received > 0 THEN COALESCE(f.night_landings,0) ELSE 0 END"),
    ("night_pic", "Night PIC", "MIN(f.night, f.pic)"),
    ("night_sic", "Night SIC", "MIN(f.night, f.sic)"),
    ("night_ldg_pic", "Night takeoffs/landings (PIC/SIC)",
     "CASE WHEN f.dual_received = 0 AND (f.pic > 0 OR f.sic > 0) THEN COALESCE(f.night_landings,0) ELSE 0 END"),
    ("flights", "Number of flights", "1"),
]
COUNT_COLUMNS = {"night_ldg_instruction", "night_ldg_pic", "flights"}


def hours_8710(db):
    """Totals laid out like the 'Record of Pilot Time' on FAA Form 8710-1 / IACRA."""
    sel = ", ".join(f"COALESCE(SUM({expr}),0) AS {key}" for key, _, expr in COLUMNS_8710)
    rows = []
    for key, label, cond in CATEGORY_ROWS:
        r = db.execute(f"SELECT {sel} FROM flights f LEFT JOIN aircraft a ON a.ident=f.aircraft "
                       f"WHERE {cond} AND COALESCE(f.total,0) > 0", ()).fetchone()
        rows.append({"key": key, "label": label, "values": dict(r)})
    sim = db.execute("SELECT COALESCE(SUM(sim_flight),0), "
                     "COALESCE(SUM(CASE WHEN sim_flight > 0 THEN MIN(sim_flight, COALESCE(sim_instr,0)) ELSE 0 END),0), "
                     "COALESCE(SUM(CASE WHEN sim_flight > 0 THEN 1 ELSE 0 END),0) FROM flights").fetchone()
    unclassed = db.execute("SELECT COUNT(*), COALESCE(SUM(f.total),0) FROM flights f LEFT JOIN aircraft a "
                           "ON a.ident=f.aircraft WHERE COALESCE(a.aircraft_class,'')='' AND COALESCE(f.total,0)>0").fetchone()
    return {"rows": [r for r in rows if r["values"]["flights"]] or rows[:1], "columns": COLUMNS_8710,
            "counts": COUNT_COLUMNS, "sim": {"total": sim[0], "instrument": sim[1], "sessions": sim[2]},
            "unclassed": {"flights": unclassed[0], "hours": unclassed[1]}}


# ----------------------------------------------------------------------------- logbook check

def _solo_windows(db):
    rows = db.execute("SELECT date, expires FROM endorsements WHERE kind='solo' ORDER BY date").fetchall()
    return [(r[0], r[1] or "9999-12-31") for r in rows]


def audit(db, today=None):
    """Things in the logbook an examiner or insurer would question, worst first."""
    today = today or date.today()
    issues = []

    def add(sev, kind, r, text):
        issues.append({"severity": sev, "kind": kind, "id": r["id"] if r is not None else None,
                       "date": r["date"] if r is not None else "", "aircraft": (r["aircraft"] if r is not None else "") or "",
                       "text": text})

    rows = db.execute("SELECT * FROM flights ORDER BY date, id").fetchall()
    windows = _solo_windows(db)
    any_endorsements = bool(db.execute("SELECT 1 FROM endorsements LIMIT 1").fetchone())
    seen = {}
    for r in rows:
        tot = r["total"] or 0
        if r["date"] > today.isoformat():
            add("error", "Future date", r, "Dated in the future.")
        if tot <= 0 and not (r["sim_flight"] or 0) and not (r["ground_training"] or 0):
            add("warning", "No time logged", r, "No flight, simulator or ground time.")
        for key in core.SUBSET_OF_TOTAL:
            v = r[key] or 0
            if v > tot + 1e-9:
                add("error", "More than total time", r,
                    f'{core.BY_KEY[key]["label"]} ({v:.1f}) is more than total time ({tot:.1f}).')
        if (r["dual_received"] or 0) > 0 and not (r["instructor"] or "").strip():
            add("info", "No instructor name", r, "Dual received with no instructor name in the logbook entry. "
                "Your instructor's signature and certificate number belong in your paper logbook (61.51(h)).")
        if (r["solo"] or 0) > 0 and (r["dual_received"] or 0) > 0 and (r["solo"] or 0) + (r["dual_received"] or 0) > tot + 1e-9:
            add("warning", "Solo and dual overlap", r, "Solo plus dual received is more than total time.")
        if (r["xc"] or 0) > 0:
            g = features.flight_geometry(r)
            if not g["points"] or len({ap["icao"] for _, ap in g["points"]}) < 2:
                if not (r["distance"] or 0):
                    add("warning", "Cross-country without a route", r,
                        "Cross-country time, but no route, destination or distance to show where you went.")
            else:
                far = max((features.leg_nm(g["points"][0][1], ap) for _, ap in g["points"][1:]), default=0)
                if far <= 50 and not (r["distance"] or 0) > 50:
                    add("info", "Short cross-country", r,
                        f"Farthest point is {far:.0f} nm from departure. For the private and instrument "
                        "requirements, a cross-country needs a landing more than 50 nm straight-line away (61.1).")
        landings = (r["day_landings"] or 0) + (r["night_landings"] or 0)
        if (r["all_landings"] or 0) and landings > (r["all_landings"] or 0):
            add("warning", "Landings don't add up", r,
                f"{landings} full-stop landings but only {r['all_landings']} landings in all.")
        if (r["landing_fs_day_towered"] or 0) > (r["day_landings"] or 0):
            add("warning", "Landings don't add up", r, "More towered landings than day full-stop landings.")
        hs, he = r["hobbs_start"] or 0, r["hobbs_end"] or 0
        if hs and he and abs((he - hs) - tot) > 0.3:
            add("info", "Hobbs doesn't match", r, f"Hobbs shows {he - hs:.1f} h but total time is {tot:.1f} h.")
        if (r["solo"] or 0) > 0 and any_endorsements and windows:
            if not any(start <= r["date"] <= end for start, end in windows):
                add("warning", "Solo outside endorsement dates", r,
                    "Solo flight on a date no recorded 90-day solo endorsement covers (61.87(n)).")
        key = (r["date"], r["aircraft"], round(tot, 2), (r["from_apt"] or "").upper(), (r["to_apt"] or "").upper(),
               (r["route"] or "").upper())
        if key in seen and tot > 0:
            add("warning", "Possible duplicate", r, f"Same date, aircraft, route and time as another flight (#{seen[key]}).")
        seen.setdefault(key, r["id"])
    if rows and any((r["solo"] or 0) > 0 for r in rows) and not windows:
        issues.append({"severity": "info", "kind": "No solo endorsements", "id": None, "date": "", "aircraft": "",
                       "text": "You have solo flights but no solo endorsements recorded. Add them on the Endorsements page "
                               "so solo dates can be checked."})
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["severity"]], i["kind"], i["date"]))
    groups = {}
    for i in issues:
        groups.setdefault((i["severity"], i["kind"]), []).append(i)
    return {"issues": issues, "groups": [{"severity": s, "kind": k, "entries": v} for (s, k), v in groups.items()],
            "counts": {s: sum(1 for i in issues if i["severity"] == s) for s in order}, "flights": len(rows)}


# ----------------------------------------------------------------------------- insurance summary

def _like(col, *words):
    return "(" + " OR ".join(f"lower(COALESCE(a.{col},'')) LIKE '%{w}%'" for w in words) + ")"


def insurance(db, today=None):
    today = today or date.today()
    d90, d365 = (today - timedelta(days=90)).isoformat(), (today - timedelta(days=365)).isoformat()
    j = "FROM flights f LEFT JOIN aircraft a ON a.ident=f.aircraft"

    def s(expr, cond="1=1", params=()):
        return db.execute(f"SELECT COALESCE(SUM({expr}),0) {j} WHERE {cond}", params).fetchone()[0]

    lines = [
        ("Total flight time", s("f.total")),
        ("Pilot in command", s("f.pic")),
        ("Last 90 days", s("f.total", "f.date>?", (d90,))),
        ("Last 12 months", s("f.total", "f.date>?", (d365,))),
        ("PIC in the last 12 months", s("f.pic", "f.date>?", (d365,))),
        ("Single-engine land", s("f.total", "COALESCE(a.aircraft_class,'') LIKE 'airplane%single%land%'")),
        ("Multi-engine", s("f.total", "COALESCE(a.aircraft_class,'') LIKE '%multi%'")),
        ("Retractable gear", s("f.total", _like("gear_type", "retract"))),
        ("Tailwheel", s("f.total", _like("gear_type", "tail", "conventional"))),
        ("Complex", s("f.total", "upper(COALESCE(a.complex,'')) IN ('TRUE','1','YES')")),
        ("High performance", s("f.total", "upper(COALESCE(a.high_perf,'')) IN ('TRUE','1','YES')")),
        ("Turbine", s("f.total", _like("engine_type", "turbo", "jet"))),
        ("Night", s("f.night")),
        ("Actual instrument", s("f.actual_instr")),
        ("Dual received", s("f.dual_received")),
        ("Landings", s("COALESCE(f.all_landings,0)")),
    ]
    models = []
    for r in db.execute(f"SELECT COALESCE(NULLIF(a.make,''),'') AS make, COALESCE(NULLIF(a.model,''), f.aircraft) AS model, "
                        f"COALESCE(SUM(f.total),0) AS total, COALESCE(SUM(f.pic),0) AS pic, "
                        f"COALESCE(SUM(CASE WHEN f.date>? THEN f.total ELSE 0 END),0) AS last12, "
                        f"COALESCE(SUM(CASE WHEN f.date>? THEN f.total ELSE 0 END),0) AS last90, "
                        f"MAX(f.date) AS last {j} GROUP BY 1, 2 ORDER BY total DESC", (d365, d90)):
        models.append(dict(r))
    tails = [dict(r) for r in db.execute(
        f"SELECT f.aircraft AS ident, COALESCE(a.model,'') AS model, COALESCE(SUM(f.total),0) AS total, MAX(f.date) AS last "
        f"{j} WHERE f.aircraft!='' GROUP BY f.aircraft ORDER BY total DESC")]
    return {"lines": lines, "models": models, "tails": tails, "as_of": today}


def insurance_pdf(data, prof, reminders, fmt):
    buf = io.BytesIO()
    name = prof.get("name") or "Pilot"
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.7 * inch, title=f"Pilot experience - {name}", author=name)
    ink = colors.HexColor("#10243A")
    band = colors.HexColor("#DCE7EF")
    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=ink, spaceAfter=4)
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=ink, spaceBefore=12, spaceAfter=5)
    p = ParagraphStyle("p", fontName="Helvetica", fontSize=9, leading=12, textColor=ink)
    small = ParagraphStyle("s", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#526579"))
    W = letter[0] - 1.4 * inch

    def table(rows, widths, header=True):
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
        st = [("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("TEXTCOLOR", (0, 0), (-1, -1), ink),
              ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#8FA3B5")), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        if header:
            st += [("BACKGROUND", (0, 0), (-1, 0), band), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5)]
        t.setStyle(TableStyle(st))
        return t

    story = [Paragraph("Pilot experience summary", h1),
             Paragraph(f"<b>{name}</b> &nbsp; {prof.get('certificate') or ''}"
                       f"{' &middot; Medical: ' + prof['medical_class'] if prof.get('medical_class') else ''}"
                       f" &middot; As of {data['as_of'].strftime('%B %-d, %Y')}", p),
             Paragraph("Hours", h2)]
    half = (len(data["lines"]) + 1) // 2
    left, right = data["lines"][:half], data["lines"][half:]
    rows = []
    for i in range(half):
        a = left[i]
        b = right[i] if i < len(right) else ("", None)
        fa = a[1] if a[0] == "Landings" else fmt(a[1])
        fb = "" if b[1] is None else (str(int(b[1])) if b[0] == "Landings" else fmt(b[1]))
        rows.append([a[0], str(int(fa)) if a[0] == "Landings" else fa, b[0], fb])
    t = Table(rows, colWidths=[W * .32, W * .16, W * .34, W * .18])
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("TEXTCOLOR", (0, 0), (-1, -1), ink),
                           ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#8FA3B5"))]))
    story.append(t)
    story.append(Paragraph("By make and model", h2))
    story.append(table([["Make and model", "Total", "PIC", "Last 12 months", "Last 90 days", "Last flown"]] +
                       [[f"{m['make']} {m['model']}".strip(), fmt(m["total"]), fmt(m["pic"]), fmt(m["last12"]),
                         fmt(m["last90"]), m["last"] or ""] for m in data["models"]],
                       [W * .34, W * .12, W * .12, W * .15, W * .13, W * .14]))
    if reminders:
        story.append(Paragraph("Currency", h2))
        story.append(table([["Item", "Status", "Good through"]] +
                           [[r["label"], "Valid" if r["ok"] else "Expired", r["through"].strftime("%b %-d, %Y")] for r in reminders],
                           [W * .5, W * .2, W * .3]))
    story += [Spacer(1, 10), Paragraph("Totals come from the pilot's self-hosted logbook. Retractable, tailwheel, complex, "
                                       "high-performance and turbine time depend on how each aircraft is set up in the "
                                       "logbook's aircraft list.", small)]
    doc.build(story)
    return buf.getvalue()


# ----------------------------------------------------------------------------- ForeFlight changes

SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_changes (    -- an imported flight that looks like an edited copy of an older one
    id INTEGER PRIMARY KEY, new_id INTEGER, old_id INTEGER, import_id INTEGER, created_at TEXT
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


def detect_changes(db, import_id, file_fps, new_ids):
    """After an import: pair each newly added flight with an older imported flight from the same day and aircraft
    that's no longer in ForeFlight's export. That's almost always a flight edited in ForeFlight."""
    if not new_ids:
        return 0
    dates = [r[0] for r in db.execute(f"SELECT date FROM flights WHERE id IN ({','.join('?' * len(new_ids))})", new_ids)]
    lo = min(dates)
    olds = db.execute("SELECT * FROM flights WHERE import_id IS NOT NULL AND import_id != ? AND date >= ? "
                      "AND fingerprint NOT LIKE 'manual:%'", (import_id, lo)).fetchall()
    gone = [o for o in olds if o["fingerprint"] not in file_fps]
    used, n = set(), 0
    for nid in new_ids:
        nr = db.execute("SELECT * FROM flights WHERE id=?", (nid,)).fetchone()
        for o in gone:
            if o["id"] in used:
                continue
            if o["date"] == nr["date"] and (o["aircraft"] or "") == (nr["aircraft"] or ""):
                db.execute("INSERT INTO flight_changes(new_id, old_id, import_id, created_at) VALUES (?,?,?,?)",
                           (nid, o["id"], import_id, datetime.now().isoformat(timespec="seconds")))
                used.add(o["id"])
                n += 1
                break
    db.commit()
    return n


def pending_changes(db):
    out = []
    for ch in db.execute("SELECT * FROM flight_changes ORDER BY id").fetchall():
        new = db.execute("SELECT * FROM flights WHERE id=?", (ch["new_id"],)).fetchone()
        old = db.execute("SELECT * FROM flights WHERE id=?", (ch["old_id"],)).fetchone()
        if not new or not old:
            db.execute("DELETE FROM flight_changes WHERE id=?", (ch["id"],))
            continue
        diffs = []
        for f in core.FIELDS:
            a, b = old[f["key"]], new[f["key"]]
            if (a or 0 if f["kind"] in ("hours", "num", "int", "flag") else a or "") != \
               (b or 0 if f["kind"] in ("hours", "num", "int", "flag") else b or ""):
                diffs.append({"label": f["label"], "old": a, "new": b})
        out.append({"id": ch["id"], "new": new, "old": old, "diffs": diffs})
    db.commit()
    return out


def resolve_change(db, cid, keep):
    """keep='new' replaces the old flight with ForeFlight's edited one; keep='both' leaves both."""
    ch = db.execute("SELECT * FROM flight_changes WHERE id=?", (cid,)).fetchone()
    if not ch:
        return False
    if keep == "new":
        # carry over anything entered only in this app (costs) to the new copy
        db.execute("UPDATE OR IGNORE flight_costs SET flight_id=? WHERE flight_id=?", (ch["new_id"], ch["old_id"]))
        db.execute("DELETE FROM flight_costs WHERE flight_id=?", (ch["old_id"],))
        db.execute("DELETE FROM flights WHERE id=?", (ch["old_id"],))
    db.execute("DELETE FROM flight_changes WHERE id=?", (cid,))
    db.commit()
    return True


def last_import(db):
    r = db.execute("SELECT MAX(imported_at) FROM imports").fetchone()[0]
    if not r:
        return None
    try:
        return datetime.fromisoformat(r).date()
    except ValueError:
        return None
