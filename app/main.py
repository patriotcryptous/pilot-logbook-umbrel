"""Pilot Logbook - self-hosted, single-pilot web app for umbrelOS.

No accounts (umbrelOS's app proxy already sits in front), no telemetry, no outbound requests,
no JavaScript. Pages are rendered on the server, charts are inline SVG.
"""
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

import charts
import hangar
import logbook_core as core
import pdfexport

BASE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", "/data"))
SCANS = DATA / "scans"
DOCS = DATA / "documents"
THUMBS = DOCS / "_thumbs"
TMP = DATA / "tmp"
DB_PATH = DATA / "logbook.db"
MAX_CSV = 20 * 1024 * 1024
MAX_SCAN = 40 * 1024 * 1024
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "4096")) * 1024 * 1024   # per upload request, Hangar and restore
MAX_RESTORE = MAX_UPLOAD
PAGE_SIZE = 100
SCAN_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".pdf": "application/pdf"}

app = FastAPI(title="Pilot Logbook", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ----------------------------------------------------------------------------- plumbing

def prepare_dirs():
    """Create storage folders; uploads spool to disk under DATA/tmp so large videos never fill memory."""
    DATA.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(TMP, ignore_errors=True)  # leftovers from an interrupted upload or backup
    for d in (SCANS, DOCS, THUMBS, TMP):
        d.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(TMP)


prepare_dirs()


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def db():
    c = connect()
    try:
        yield c
    finally:
        c.close()


with db() as _c:
    core.ensure_schema(_c)


@app.middleware("http")
async def security(request: Request, call_next):
    token = request.cookies.get("hl_csrf") or secrets.token_urlsafe(32)
    request.state.csrf = token
    cl = request.headers.get("content-length", "")
    if request.method == "POST" and cl.isdigit() and int(cl) > MAX_UPLOAD + 1024 * 1024:
        resp = render(request, "error.html", status=413,
                      message=f"That upload is larger than the {MAX_UPLOAD // (1024 * 1024 * 1024)} GB limit.")
        return resp
    resp = await call_next(request)
    if "hl_csrf" not in request.cookies:
        resp.set_cookie("hl_csrf", token, httponly=True, samesite="strict", max_age=31536000)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'self'; font-src 'self'; "
            "form-action 'self'; base-uri 'none'")
    return resp


def check_csrf(request: Request, token: str):
    cookie = request.cookies.get("hl_csrf", "")
    if not token or not cookie or not hmac.compare_digest(token, cookie):
        raise HTTPException(403, "Security token missing or expired. Go back, reload the page and try again.")


NAV = [("dash", "/", "Dashboard", "grid"), ("logbook", "/logbook", "Logbook", "book"),
       ("add", "/flights/new", "Add flight", "plus"), ("progress", "/progress", "Progress", "target"),
       ("hangar", "/hangar", "Document Hangar", "folder"), ("scans", "/scans", "Paper scans", "image"), ("data", "/data", "Import & backup", "database"),
       ("settings", "/settings", "Settings", "sliders")]


def nav_key(path):
    if path == "/":
        return "dash"
    if path == "/flights/new":
        return "add"
    if path.startswith("/flights") or path.startswith("/logbook"):
        return "logbook"
    for key, href, _, _ in NAV[3:]:
        if path.startswith(href):
            return key
    return "more" if path == "/more" else ""


def settings_ctx(c):
    prof = core.profile_get(c)
    return prof, core.enabled_features(prof), core.time_format(prof)


def render(request: Request, name: str, status: int = 200, **ctx):
    ctx.update(csrf=request.state.csrf, path=request.url.path, nav=NAV, active=nav_key(request.url.path),
               theme=request.cookies.get("hl_theme", ""))
    ctx.setdefault("tf", "decimal1")
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def back(path: str, **params):
    qs = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(path + ("?" + qs if qs else ""), status_code=303)


@pass_context
def f_hrs(ctx, v):
    return core.fmt_hours(v, ctx.get("tf", "decimal1"))


@pass_context
def f_hb(ctx, v):
    return "" if not v else core.fmt_hours(v, ctx.get("tf", "decimal1"))


def f_ib(v):
    return "" if not v else str(int(v))


def f_route(r):
    toks = [t for t in re.split(r"[\s,\-]+", r["route"] or "") if t]
    if toks:
        return " \u2192 ".join(toks)
    return " \u2192 ".join(x for x in (r["from_apt"], r["to_apt"]) if x)


def f_date(d):
    if not d:
        return ""
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.strftime("%b %-d, %Y")


def f_short(d):
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.strftime("%b %-d")


def f_tags(r):
    tags = []
    if (r["solo"] or 0) > 0:
        tags.append(("solo", "Solo"))
    if (r["dual_received"] or 0) > 0:
        tags.append(("dual", "Dual"))
    if (r["xc"] or 0) > 0:
        tags.append(("xc", "Cross-country"))
    if (r["night"] or 0) > 0:
        tags.append(("night", "Night"))
    if (r["actual_instr"] or 0) + (r["sim_instr"] or 0) > 0:
        tags.append(("ifr", "Instrument"))
    if (r["sim_flight"] or 0) > 0:
        tags.append(("sim", "Simulator"))
    if not tags and (r["ground_training"] or 0) > 0 and not (r["total"] or 0):
        tags.append(("ground", "Ground"))
    return tags


templates.env.filters.update(hrs=f_hrs, hb=f_hb, ib=f_ib, route=f_route, pretty_date=f_date, short_date=f_short,
                             tags=f_tags)



ICONS = {
    "grid": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
            '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "book": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "more": '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
    "play": '<path d="M7 4.5v15l12-7.5z"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
}


def icon(name):
    return Markup(f'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{ICONS[name]}</svg>')


templates.env.globals["icon"] = icon


async def read_limited(f: UploadFile, limit: int) -> bytes:
    data = await f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"That file is larger than the {limit // (1024 * 1024)} MB limit.")
    return data


# ----------------------------------------------------------------------------- dashboard

def reminders_for(c, prof):
    out, today = [], date.today()
    review = prof.get("flight_review") or ""
    flagged = c.execute("SELECT MAX(date) FROM flights WHERE flag_flight_review=1").fetchone()[0]
    if flagged and flagged > review:
        review = flagged
    if review:
        thru = core.flight_review_through(review)
        out.append({"label": "Flight review", "through": thru, "ok": thru >= today, "left": (thru - today).days})
    if prof.get("medical_expires"):
        thru = datetime.strptime(prof["medical_expires"], "%Y-%m-%d").date()
        out.append({"label": "Medical", "through": thru, "ok": thru >= today, "left": (thru - today).days})
    return out


@app.get("/")
def dashboard(request: Request):
    today = date.today()
    with db() as c:
        prof, _, tf = settings_ctx(c)
        d = core.dashboard(c, today)
        cur = core.currency(c, today)
        rem = reminders_for(c, prof)
        prog = core.progress_private_asel(c)
        hsum = hangar.summary(c, DOCS, today)
    rings = []
    for k in cur:
        rings.append({
            "label": k["label"], "day_count": k["day_count"], "night_count": k["night_count"],
            "day": charts.ring(k["day_left"] / 90 if k["day_ok"] else 0,
                               str(k["day_left"]) if k["day_ok"] else str(k["day_count"]),
                               "days left" if k["day_ok"] else "of 3 landings", "ok" if k["day_ok"] else "bad"),
            "night": charts.ring(k["night_left"] / 90 if k["night_ok"] else 0,
                                 str(k["night_left"]) if k["night_ok"] else str(k["night_count"]),
                                 "days left" if k["night_ok"] else "of 3 landings", "ok" if k["night_ok"] else "idle"),
            "day_ok": k["day_ok"], "night_ok": k["night_ok"],
            "day_through": k["day_through"], "night_through": k["night_through"]})
    ctx = dict(d=d, t=d["totals"], hsum=hsum, rings=rings, reminders=rem, prog=prog["tracked"][:6], tf=tf,
               monthly=charts.monthly_bars(d["months"], tf), cumul=charts.cumulative_line(d["cumulative"], tf),
               heat=charts.heatmap(d["heat"], today, tf), first_name=(prof.get("name") or "").split(" ")[0])
    return render(request, "dashboard.html", **ctx)


# ----------------------------------------------------------------------------- logbook table

EXTRA_COLS = [("sic", "SIC", "sic"), ("picus", "PICUS", "picus"), ("multi_pilot", "Multi-pilot", "multi_pilot"),
              ("ifr", "IFR", "ifr"), ("nvg", "NVG", "nvg"), ("dual_given", "Dual given", "dual_given"),
              ("sim_flight", "Sim", "sim_flight"), ("ground_training", "Ground", "ground_training"),
              ("ground_given", "Ground given", "ground_given"), ("examiner", "Examiner", "examiner"),
              ("distance", "Dist. nm", "distance"), ("holds", "Holds", "holds")]


@app.get("/logbook")
def logbook(request: Request, year: str = "", aircraft: str = "", q: str = "", page: int = 1):
    where, params = ["1=1"], []
    if re.fullmatch(r"\d{4}", year):
        where.append("substr(date,1,4)=?")
        params.append(year)
    if aircraft:
        where.append("aircraft=?")
        params.append(aircraft)
    if q.strip():
        where.append("(comments LIKE ? OR route LIKE ? OR instructor LIKE ?)")
        params += [f"%{q.strip()}%"] * 3
    w = " AND ".join(where)
    page = max(page, 1)
    with db() as c:
        prof, enabled, tf = settings_ctx(c)
        total_rows = c.execute(f"SELECT COUNT(*) FROM flights WHERE {w}", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM flights WHERE {w} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
                         (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
        sums = core.totals(c, w, params)
        all_sums = core.totals(c)
        years = [r[0] for r in c.execute("SELECT DISTINCT substr(date,1,4) FROM flights ORDER BY 1 DESC")]
        tails = [r[0] for r in c.execute("SELECT DISTINCT aircraft FROM flights WHERE aircraft!='' ORDER BY 1")]
    # Optional columns appear when their field is switched on and your logbook has data in them.
    extra = [(k, h) for k, h, feat in EXTRA_COLS if feat in enabled and all_sums.get(k, 0) > 0]
    return render(request, "logbook.html", tf=tf, rows=rows, sums=sums, all_sums=all_sums, years=years, tails=tails,
                  year=year, aircraft=aircraft, q=q, page=page, pages=max(1, -(-total_rows // PAGE_SIZE)),
                  total_rows=total_rows, extra=extra, filtered=bool(year or aircraft or q.strip()))


# ----------------------------------------------------------------------------- flight form

def build_sections(visible):
    out = []
    for sid, title, keys in core.SECTIONS:
        fields = [core.BY_KEY[k] for k in keys if k in visible]
        if fields:
            out.append({"id": sid, "title": title, "fields": fields})
    return out


def field_strings(row, tf):
    """Form-ready strings for each field of a stored flight."""
    out = {}
    for f in core.FIELDS:
        v = row[f["key"]]
        k = f["kind"]
        if k == "hours":
            out[f["key"]] = core.fmt_hours(v, "hhmm" if tf == "hhmm" else "decimal2" if tf == "decimal2" else "decimal1") if v else ""
        elif k == "num":
            out[f["key"]] = ("%.2f" % v) if v else ""
        elif k == "int":
            out[f["key"]] = str(v) if v else ""
        elif k == "flag":
            out[f["key"]] = "1" if v else ""
        else:
            out[f["key"]] = v or ""
    return out


def parse_flight(form, visible):
    """Validate the form and return (values for the visible fields, new-aircraft info, errors)."""
    errors, v = [], {}
    label = lambda k: core.BY_KEY[k]["label"]  # noqa: E731
    for key in visible:
        f = core.BY_KEY[key]
        raw = (form.get(key) or "").strip()
        k = f["kind"]
        if key == "date":
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d").date()
                if parsed > date.today() + timedelta(days=1):
                    errors.append("The date is in the future.")
            except ValueError:
                errors.append("Enter the flight date.")
            v[key] = raw
        elif key == "aircraft":
            tail = raw.upper()
            new_tail = (form.get("new_tail") or "").strip().upper()
            v[key] = new_tail or tail
            if not v[key]:
                errors.append("Choose an aircraft or enter a new tail number.")
            elif new_tail and not re.fullmatch(r"[A-Z0-9\-]{1,12}", new_tail):
                errors.append("Tail numbers use letters, digits and dashes only.")
        elif k == "hours":
            try:
                val = core.parse_hours(raw)
                if val < 0 or val > 99:
                    raise ValueError
            except ValueError:
                errors.append(f"{label(key)} must be hours, like 1.3 or 1:18.")
                val = 0.0
            v[key] = val
        elif k == "num":
            try:
                val = round(float(raw.replace(",", ".")), 2) if raw else 0.0
                if val < 0 or val > 1_000_000:
                    raise ValueError
            except ValueError:
                errors.append(f"{label(key)} must be a number.")
                val = 0.0
            v[key] = val
        elif k == "int":
            try:
                val = int(raw) if raw else None
                if val is not None and (val < 0 or val > 999):
                    raise ValueError
            except ValueError:
                errors.append(f"{label(key)} must be a whole number.")
                val = None
            v[key] = val
        elif k == "flag":
            v[key] = 1 if form.get(key) else 0
        else:
            v[key] = raw[:20 if k == "time" else 500]
    for key in ("from_apt", "to_apt"):
        if key in v:
            v[key] = v[key].upper()[:8]
    if "total" in v and not errors:
        if v["total"] <= 0 and not v.get("ground_training") and not v.get("sim_flight"):
            errors.append("Enter the total flight time (or ground or simulator time).")
        for key in core.SUBSET_OF_TOTAL:
            if key in v and v[key] > v["total"] + 1e-9:
                errors.append(f"{label(key)} ({v[key]:.1f}) is more than total time ({v['total']:.1f}).")
    for a, b in (("hobbs_start", "hobbs_end"), ("tach_start", "tach_end")):
        if a in v and b in v and v[b] and v[a] and v[b] < v[a]:
            errors.append(f"{label(b)} is less than {label(a).lower()}.")
    dl, nl = v.get("day_landings") or 0, v.get("night_landings") or 0
    if "day_landings" in v:
        v["day_landings"] = dl
    if "night_landings" in v:
        v["night_landings"] = nl
    if "all_landings" in v:
        if v["all_landings"] is None:
            v["all_landings"] = dl + nl
        elif v["all_landings"] < dl + nl:
            errors.append("All landings can't be fewer than your full-stop landings.")
    if "day_takeoffs" in v and v["day_takeoffs"] is None:
        v["day_takeoffs"] = dl
    if "night_takeoffs" in v and v["night_takeoffs"] is None:
        v["night_takeoffs"] = nl
    new = {"tail": (form.get("new_tail") or "").strip().upper(), "make": (form.get("new_make") or "").strip()[:60],
           "model": (form.get("new_model") or "").strip()[:60],
           "cls": (form.get("new_class") or "airplane_single_engine_land").strip()[:60]}
    return v, new, errors


def form_context(c):
    tails = [dict(r) for r in c.execute("SELECT ident, make, model FROM aircraft ORDER BY ident")]
    classes = sorted({r[0] for r in c.execute("SELECT DISTINCT aircraft_class FROM aircraft WHERE aircraft_class!=''")}
                     | {"airplane_single_engine_land", "airplane_multi_engine_land",
                        "airplane_single_engine_sea", "airplane_multi_engine_sea"})
    return {"tails": tails, "classes": classes}


@app.get("/flights/new")
def new_flight(request: Request):
    with db() as c:
        _, enabled, tf = settings_ctx(c)
        ctx = form_context(c)
        last = c.execute("SELECT * FROM flights ORDER BY date DESC, id DESC LIMIT 1").fetchone()
    f = {"date": date.today().isoformat()}
    if last:
        f.update(aircraft=last["aircraft"], from_apt=last["from_apt"], to_apt=last["to_apt"])
    return render(request, "flight_form.html", tf=tf, f=f, errors=[], editing=None,
                  sections=build_sections(core.visible_fields(enabled)), **ctx)


@app.post("/flights/new")
async def create_flight(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    with db() as c:
        _, enabled, tf = settings_ctx(c)
        ctx = form_context(c)
        visible = core.visible_fields(enabled)
        v, new, errors = parse_flight(form, visible)
        if errors:
            return render(request, "flight_form.html", status=422, tf=tf, f=dict(form), errors=errors, editing=None,
                          sections=build_sections(visible), **ctx)
        if new["tail"]:
            core.ensure_aircraft(c, new["tail"], new["make"], new["model"], new["cls"])
        core.add_flight(c, v)
    return RedirectResponse("/logbook", status_code=303)


@app.get("/flights/{fid}/edit")
def edit_flight(request: Request, fid: int):
    with db() as c:
        _, enabled, tf = settings_ctx(c)
        row = c.execute("SELECT * FROM flights WHERE id=?", (fid,)).fetchone()
        if not row:
            raise HTTPException(404, "That flight doesn't exist.")
        ctx = form_context(c)
    return render(request, "flight_form.html", tf=tf, f=field_strings(row, tf), errors=[], editing=fid,
                  sections=build_sections(core.visible_fields(enabled, row)), **ctx)


@app.post("/flights/{fid}/edit")
async def save_flight(request: Request, fid: int):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    with db() as c:
        _, enabled, tf = settings_ctx(c)
        row = c.execute("SELECT * FROM flights WHERE id=?", (fid,)).fetchone()
        if not row:
            raise HTTPException(404, "That flight doesn't exist.")
        ctx = form_context(c)
        visible = core.visible_fields(enabled, row)  # fields not shown keep their stored values
        v, new, errors = parse_flight(form, visible)
        if errors:
            return render(request, "flight_form.html", status=422, tf=tf, f=dict(form), errors=errors, editing=fid,
                          sections=build_sections(visible), **ctx)
        if new["tail"]:
            core.ensure_aircraft(c, new["tail"], new["make"], new["model"], new["cls"])
        core.update_flight(c, fid, v)
    return RedirectResponse("/logbook", status_code=303)


@app.post("/flights/{fid}/delete")
async def remove_flight(request: Request, fid: int):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    with db() as c:
        core.delete_flight(c, fid)
    return RedirectResponse("/logbook", status_code=303)


# ----------------------------------------------------------------------------- progress

@app.get("/progress")
def progress(request: Request):
    with db() as c:
        _, _, tf = settings_ctx(c)
        p = core.progress_private_asel(c)
    return render(request, "progress.html", p=p, tf=tf)


# ----------------------------------------------------------------------------- settings

@app.get("/settings")
def settings(request: Request, saved: int = 0):
    with db() as c:
        prof, enabled, tf = settings_ctx(c)
    review = prof.get("flight_review")
    groups = {}
    for key, label, group, hint, _ in core.FEATURES:
        groups.setdefault(group, []).append({"key": key, "label": label, "hint": hint, "on": key in enabled})
    return render(request, "settings.html", prof=prof, tf=tf, groups=groups, formats=core.TIME_FORMATS,
                  review_through=core.flight_review_through(review) if review else None, saved=bool(saved))


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    vals = {key: (form.get(key) or "").strip()[:120] for key in ("name", "certificate", "medical_class")}
    for key in ("flight_review", "medical_expires"):
        raw = (form.get(key) or "").strip()
        try:
            if raw:
                datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(422, "Dates must be in YYYY-MM-DD form.")
        vals[key] = raw
    tf = form.get("time_format") or "decimal1"
    vals["time_format"] = tf if tf in core.TIME_FORMATS else "decimal1"
    vals["features"] = ",".join(k for k, *_ in core.FEATURES if form.get(f"feat_{k}"))
    with db() as c:
        core.profile_set(c, vals)
    return back("/settings", saved="1")


@app.get("/profile")
def profile_redirect():
    return RedirectResponse("/settings", status_code=301)


@app.get("/more")
def more(request: Request):
    return render(request, "more.html")


@app.post("/theme")
async def set_theme(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    choice = form.get("theme", "")
    nxt = form.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    resp = RedirectResponse(nxt, status_code=303)
    if choice in ("light", "dark"):
        resp.set_cookie("hl_theme", choice, max_age=31536000, samesite="strict")
    else:
        resp.delete_cookie("hl_theme")
    return resp


# ----------------------------------------------------------------------------- scans

def _scan_path(name: str) -> Path:
    p = (SCANS / name).resolve()
    if SCANS.resolve() not in p.parents:
        raise HTTPException(404)
    return p


@app.get("/scans")
def scans(request: Request, error: str = ""):
    with db() as c:
        rows = c.execute("SELECT * FROM scans ORDER BY COALESCE(NULLIF(date_from,''),'9999') , id").fetchall()
    return render(request, "scans.html", rows=rows, types=SCAN_TYPES, error=error)


@app.post("/scans")
async def upload_scan(request: Request, csrf: str = Form(""), files: list[UploadFile] = File(...),
                      date_from: str = Form(""), date_to: str = Form(""), note: str = Form("")):
    check_csrf(request, csrf)
    for d in (date_from, date_to):
        if d:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                return back("/scans", error="Dates must be valid.")
    saved = 0
    with db() as c:
        for f in files:
            if not f.filename:
                continue
            ext = Path(f.filename).suffix.lower()
            if ext not in SCAN_TYPES:
                return back("/scans", error="Upload JPG, PNG, WebP or PDF files.")
            try:
                data = await read_limited(f, MAX_SCAN)
            except ValueError:
                return back("/scans", error="Each file must be under 40 MB.")
            name = secrets.token_hex(12) + ext
            (SCANS / name).write_bytes(data)
            c.execute("INSERT INTO scans(filename, original_name, size, date_from, date_to, note, uploaded_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (name, Path(f.filename).name[:200], len(data), date_from, date_to, note.strip()[:300],
                       datetime.now(timezone.utc).isoformat()))
            saved += 1
        c.commit()
    if not saved:
        return back("/scans", error="Choose at least one file.")
    return RedirectResponse("/scans", status_code=303)


@app.get("/scans/{sid}/file")
def scan_file(sid: int):
    with db() as c:
        r = c.execute("SELECT filename FROM scans WHERE id=?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404)
    p = _scan_path(r["filename"])
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type=SCAN_TYPES.get(p.suffix.lower(), "application/octet-stream"))


@app.post("/scans/{sid}/delete")
async def delete_scan(request: Request, sid: int):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    with db() as c:
        r = c.execute("SELECT filename FROM scans WHERE id=?", (sid,)).fetchone()
        if r:
            _scan_path(r["filename"]).unlink(missing_ok=True)
            c.execute("DELETE FROM scans WHERE id=?", (sid,))
            c.commit()
    return RedirectResponse("/scans", status_code=303)


# ----------------------------------------------------------------------------- Document Hangar

HANGAR_PER = 60
NOTE_MAX = 20000


async def store_upload(f: UploadFile, ext: str):
    """Stream an upload to disk under a random name. Returns (stored name, size in bytes)."""
    name = secrets.token_hex(12) + ext
    dest = DOCS / name
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await f.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise ValueError(f"is larger than the {MAX_UPLOAD // (1024 * 1024 * 1024)} GB limit")
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if hangar.type_of(name)[0] == "image":
        hangar.make_thumb(dest, THUMBS / (name + ".jpg"))
    return name, size


def drop_files(row):
    """Delete a stored file and its preview image."""
    name = row["filename"]
    if name:
        try:
            hangar.safe_path(DOCS, name).unlink(missing_ok=True)
        except ValueError:
            pass
        (THUMBS / (name + ".jpg")).unlink(missing_ok=True)


def doc_meta(category, aircraft, expires):
    """Validate the fields shared by every Hangar form."""
    errors, exp = [], (expires or "").strip()
    if exp:
        try:
            datetime.strptime(exp, "%Y-%m-%d")
        except ValueError:
            errors.append("The expiry date isn't valid.")
    ac = (aircraft or "").strip().upper()[:12]
    return {"aircraft": ac, "expires": exp}, errors


def hangar_form_ctx(c):
    tails = [r[0] for r in c.execute("SELECT ident FROM aircraft ORDER BY ident")]
    return {"cats": hangar.CATEGORIES, "hints": hangar.CAT_HINT, "tails": tails, "max_gb": MAX_UPLOAD // (1024 ** 3)}


@app.get("/hangar")
def hangar_home(request: Request, cat: str = "", q: str = "", aircraft: str = "", sort: str = "new", page: int = 1,
                msg: str = "", error: str = ""):
    today = date.today()
    with db() as c:
        rows, total = hangar.listing(c, cat, q, aircraft, sort, page, HANGAR_PER)
        counts = hangar.category_counts(c)
        attn = [hangar.decorate(r, DOCS, today) for r in hangar.attention(c, today)]
        info = hangar.summary(c, DOCS, today)
        tails = sorted({r[0] for r in c.execute("SELECT ident FROM aircraft")}
                       | {r[0] for r in c.execute("SELECT DISTINCT aircraft FROM documents WHERE aircraft!=''")})
    docs = [hangar.decorate(r, DOCS, today) for r in rows]
    filtered = bool(cat or q.strip() or aircraft)
    return render(request, "hangar.html", docs=docs, total=total, counts=counts, cats=hangar.CATEGORIES, attn=attn,
                  info=info, tails=tails, cat=cat, q=q, aircraft=aircraft, sort=sort, page=page,
                  pages=max(1, -(-total // HANGAR_PER)), filtered=filtered, msg=msg, error=error)


@app.get("/hangar/add")
def hangar_add_form(request: Request, error: str = ""):
    with db() as c:
        ctx = hangar_form_ctx(c)
    return render(request, "hangar_add.html", error=error, **ctx)


@app.post("/hangar/add")
async def hangar_add(request: Request, csrf: str = Form(""), files: list[UploadFile] = File(...),
                     category: str = Form("auto"), aircraft: str = Form(""), expires: str = Form(""),
                     title: str = Form(""), notes: str = Form("")):
    check_csrf(request, csrf)
    meta, errors = doc_meta(category, aircraft, expires)
    if errors:
        return back("/hangar/add", error=errors[0])
    chosen = [f for f in files if f.filename]
    if not chosen:
        return back("/hangar/add", error="Choose at least one file.")
    added, skipped = [], []
    with db() as c:
        for f in chosen:
            orig = Path(f.filename).name[:200]
            t = hangar.type_of(orig)
            if not t:
                skipped.append(f"{orig} (this file type isn't supported)")
                continue
            try:
                name, size = await store_upload(f, Path(orig).suffix.lower())
            except ValueError as e:
                skipped.append(f"{orig} ({e})")
                continue
            except OSError:
                skipped.append(f"{orig} (couldn't be saved, the disk may be full)")
                continue
            cat = category if category in hangar.CAT_LABEL else hangar.auto_category(t[0])
            nice = Path(orig).stem.replace("_", " ").strip() or orig
            did = hangar.create(c, title=(title.strip()[:200] if title.strip() and len(chosen) == 1 else nice[:200]),
                                category=cat, notes=notes.strip()[:5000], filename=name, original_name=orig,
                                mime=t[1], size=size, **meta)
            added.append(did)
    if len(added) == 1 and not skipped:
        return back(f"/hangar/{added[0]}", msg="Saved to your Hangar.")
    msg = f"Added {len(added)} file{'s' if len(added) != 1 else ''}." if added else ""
    err = ("Skipped: " + "; ".join(skipped) + ".") if skipped else ""
    return back("/hangar" if added else "/hangar/add", msg=msg, error=err)


@app.get("/hangar/note/new")
def hangar_note_form(request: Request):
    with db() as c:
        ctx = hangar_form_ctx(c)
    return render(request, "hangar_note.html", errors=[], f={"category": "notes"}, **ctx)


@app.post("/hangar/note/new")
async def hangar_note_create(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    title = (form.get("title") or "").strip()[:200]
    body = (form.get("notes") or "").strip()[:NOTE_MAX]
    cat = form.get("category") if form.get("category") in hangar.CAT_LABEL else "notes"
    meta, errors = doc_meta(cat, form.get("aircraft"), form.get("expires"))
    if not title:
        errors.append("Give the note a title.")
    if errors:
        with db() as c:
            ctx = hangar_form_ctx(c)
        return render(request, "hangar_note.html", status=422, errors=errors, f=dict(form), **ctx)
    with db() as c:
        did = hangar.create(c, kind="note", title=title, category=cat, notes=body, **meta)
    return back(f"/hangar/{did}", msg="Note saved.")


def _doc_or_404(c, did):
    row = hangar.get(c, did)
    if not row:
        raise HTTPException(404, "That document doesn't exist.")
    return row


@app.get("/hangar/{did}")
def hangar_doc(request: Request, did: int, msg: str = "", error: str = ""):
    with db() as c:
        row = _doc_or_404(c, did)
        ctx = hangar_form_ctx(c)
    doc = hangar.decorate(row, DOCS)
    preview = ""
    if doc["tkind"] == "text" and doc["exists"] and doc["size"] <= 256 * 1024:
        preview = hangar.safe_path(DOCS, doc["filename"]).read_text(encoding="utf-8", errors="replace")
    return render(request, "hangar_doc.html", doc=doc, preview=preview, msg=msg, error=error, **ctx)


@app.post("/hangar/{did}")
async def hangar_doc_save(request: Request, did: int, csrf: str = Form(""), title: str = Form(""),
                          category: str = Form("other"), aircraft: str = Form(""), expires: str = Form(""),
                          notes: str = Form(""), file: UploadFile = File(None)):
    check_csrf(request, csrf)
    with db() as c:
        row = _doc_or_404(c, did)
        meta, errors = doc_meta(category, aircraft, expires)
        if not title.strip():
            errors.append("The title can't be empty.")
        if errors:
            return back(f"/hangar/{did}", error=errors[0])
        vals = dict(title=title.strip()[:200], category=category if category in hangar.CAT_LABEL else "other",
                    notes=notes.strip()[:NOTE_MAX if row["kind"] == "note" else 5000], **meta)
        if row["kind"] == "file" and file is not None and file.filename:
            orig = Path(file.filename).name[:200]
            t = hangar.type_of(orig)
            if not t:
                return back(f"/hangar/{did}", error="That replacement file type isn't supported.")
            try:
                name, size = await store_upload(file, Path(orig).suffix.lower())
            except ValueError as e:
                return back(f"/hangar/{did}", error=f"{orig} {e}.")
            except OSError:
                return back(f"/hangar/{did}", error="The replacement couldn't be saved. The disk may be full.")
            drop_files(row)
            vals.update(filename=name, original_name=orig, mime=t[1], size=size)
        hangar.update(c, did, **vals)
    return back(f"/hangar/{did}", msg="Saved.")


@app.post("/hangar/{did}/delete")
async def hangar_doc_delete(request: Request, did: int):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    with db() as c:
        row = hangar.get(c, did)
        if row:
            drop_files(row)
            hangar.delete(c, did)
    return back("/hangar", msg="Deleted.")


@app.get("/hangar/{did}/file")
def hangar_file(did: int, download: int = 0):
    with db() as c:
        row = hangar.get(c, did)
    if not row or row["kind"] != "file":
        raise HTTPException(404, "That file doesn't exist.")
    try:
        p = hangar.safe_path(DOCS, row["filename"])
    except ValueError:
        raise HTTPException(404, "That file doesn't exist.")
    t = hangar.type_of(row["original_name"] or row["filename"])
    if not p.is_file() or not t:
        raise HTTPException(404, "The file for this entry is missing.")
    show = "inline" if t[2] and not download else "attachment"
    return FileResponse(p, media_type=t[1], filename=row["original_name"] or p.name, content_disposition_type=show)


@app.get("/hangar/{did}/thumb")
def hangar_thumb(did: int):
    with db() as c:
        row = hangar.get(c, did)
    if not row or row["kind"] != "file":
        raise HTTPException(404)
    try:
        src = hangar.safe_path(DOCS, row["filename"])
    except ValueError:
        raise HTTPException(404)
    thumb = THUMBS / (row["filename"] + ".jpg")
    if not thumb.is_file():  # e.g. after a restore: build previews as they are first needed
        if not src.is_file() or not hangar.make_thumb(src, thumb):
            raise HTTPException(404)
    return FileResponse(thumb, media_type="image/jpeg")


# ----------------------------------------------------------------------------- data: import / export / backup

@app.get("/data")
def data_page(request: Request, msg: str = "", error: str = ""):
    with db() as c:
        imports = c.execute("SELECT id, filename, imported_at, flights_new, flights_skipped FROM imports "
                            "ORDER BY id DESC").fetchall()
        n = c.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    return render(request, "data.html", imports=imports, n=n, msg=msg, error=error)


@app.post("/data/import")
async def import_csv(request: Request, csrf: str = Form(""), file: UploadFile = File(...)):
    check_csrf(request, csrf)
    try:
        raw = await read_limited(file, MAX_CSV)
        with db() as c:
            res = core.import_foreflight(c, raw, Path(file.filename or "logbook.csv").name)
    except ValueError as e:
        return back("/data", error=str(e))
    if res.get("note"):
        msg = res["note"]
    else:
        msg = f"Imported {res['new']} new flight{'s' if res['new'] != 1 else ''}"
        if res["skipped"]:
            msg += f"; skipped {res['skipped']} already in your logbook"
        msg += "."
    return back("/data", msg=msg)


@app.get("/export/foreflight.csv")
def export_csv():
    with db() as c:
        text = core.export_foreflight_csv(c)
    name = f"logbook-{date.today().isoformat()}.csv"
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/export/logbook.pdf")
def export_pdf():
    with db() as c:
        prof, _, tf = settings_ctx(c)
        rows = c.execute("SELECT * FROM flights ORDER BY date, id").fetchall()
    pdf = pdfexport.build_pdf(rows, prof.get("name", ""), lambda v: core.fmt_hours(v, tf))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="logbook-{date.today().isoformat()}.pdf"'})


@app.get("/export/backup.zip")
def backup(documents: int = 1):
    """Everything in one zip. ?documents=0 leaves out the Document Hangar files to keep it small."""
    tmp = Path(tempfile.mkdtemp())
    snap = tmp / "logbook.db"
    src = connect()
    dst = sqlite3.connect(snap)
    src.backup(dst)  # consistent snapshot even if a write is in flight
    dst.close()
    src.close()
    zpath = tmp / "backup.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, "logbook.db")
        z.writestr("backup.json", json.dumps({"app": "pilot-logbook", "version": 2, "documents": bool(documents)}))
        for p in sorted(SCANS.iterdir()):
            if p.is_file():
                z.write(p, f"scans/{p.name}")
        if documents:
            for p in sorted(DOCS.iterdir()):
                if p.is_file():  # already-compressed media: store, don't waste time deflating
                    z.write(p, f"documents/{p.name}", compress_type=zipfile.ZIP_STORED)
    label = "backup" if documents else "backup-no-documents"
    return FileResponse(zpath, media_type="application/zip",
                        filename=f"logbook-{label}-{date.today().isoformat()}.zip",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


@app.post("/data/restore")
async def restore(request: Request, csrf: str = Form(""), confirm: str = Form(""), file: UploadFile = File(...)):
    check_csrf(request, csrf)
    if confirm != "yes":
        return back("/data", error="Tick the box to confirm the restore.")
    work = Path(tempfile.mkdtemp())
    try:
        zpath = work / "in.zip"
        size = 0
        with zpath.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_RESTORE:
                    raise ValueError("That backup is too large.")
                out.write(chunk)
        try:
            z = zipfile.ZipFile(zpath)
        except zipfile.BadZipFile:
            raise ValueError("That isn't a backup zip from this app.")
        names = z.namelist()
        if "logbook.db" not in names:
            raise ValueError("That zip has no logbook.db. Use a backup made by this app.")
        new_scans, new_docs = work / "scans", work / "documents"
        new_scans.mkdir()
        new_docs.mkdir()
        z.extract("logbook.db", work)
        has_docs = False
        for n in names:
            for prefix, dest, allowed in (("scans/", new_scans, SCAN_TYPES), ("documents/", new_docs, hangar.TYPES)):
                if n.startswith(prefix) and not n.endswith("/"):
                    base = Path(n).name  # never trust paths inside the zip
                    if not re.fullmatch(r"[A-Za-z0-9._-]+", base) or Path(base).suffix.lower() not in allowed:
                        continue
                    with z.open(n) as srcf, (dest / base).open("wb") as dstf:
                        shutil.copyfileobj(srcf, dstf)
                    has_docs = has_docs or prefix == "documents/"
        try:
            has_docs = has_docs or bool(json.loads(z.read("backup.json")).get("documents"))
        except (KeyError, ValueError):
            pass
        test = sqlite3.connect(work / "logbook.db")
        try:
            ok = test.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            test.execute("SELECT COUNT(*) FROM flights")
            test.execute("SELECT COUNT(*) FROM aircraft")
        except sqlite3.DatabaseError:
            ok = False
        finally:
            test.close()
        if not ok:
            raise ValueError("That database file is damaged or isn't a logbook.")
        keep_docs = None
        if not has_docs:  # a backup without documents must not wipe the Hangar you have now
            with db() as c:
                keep_docs = [dict(r) for r in c.execute("SELECT * FROM documents")]
        old = SCANS.with_name("scans.old")
        shutil.rmtree(old, ignore_errors=True)
        SCANS.rename(old)
        new_scans.rename(SCANS)
        if has_docs:
            old_docs = DOCS.with_name("documents.old")
            shutil.rmtree(old_docs, ignore_errors=True)
            DOCS.rename(old_docs)
            new_docs.rename(DOCS)
            THUMBS.mkdir(exist_ok=True)
            shutil.rmtree(old_docs, ignore_errors=True)
        os.replace(work / "logbook.db", DB_PATH)
        shutil.rmtree(old, ignore_errors=True)
        with db() as c:
            core.ensure_schema(c)  # bring older backups up to the current schema
            if keep_docs is not None:
                c.execute("DELETE FROM documents")
                for r in keep_docs:
                    c.execute(f"INSERT INTO documents({','.join(r)}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
                c.commit()
    except ValueError as e:
        shutil.rmtree(work, ignore_errors=True)
        return back("/data", error=str(e))
    shutil.rmtree(work, ignore_errors=True)
    return back("/data", msg="Backup restored.")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return render(request, "error.html", status=exc.status_code, message=exc.detail)
