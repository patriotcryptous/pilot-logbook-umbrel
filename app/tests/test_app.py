"""End-to-end tests. Run from app/:  python -m pytest tests -q
Set LOGBOOK_FIXTURE to a ForeFlight CSV to also test against a real export."""
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import pytest

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import logbook_core as core  # noqa: E402
import main  # noqa: E402

FIXTURE = os.environ.get("LOGBOOK_FIXTURE", "")
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")

HEADER = ",".join(core.FF_FLIGHT_HEADER)


def make_csv(rows):
    """Build a ForeFlight-style CSV from dicts keyed by ForeFlight column names."""
    n = len(core.FF_FLIGHT_HEADER)
    pad = lambda cells: ",".join(cells + [""] * (n - len(cells)))  # noqa: E731
    lines = [pad(["ForeFlight Logbook Import", "This row is required for importing into ForeFlight. Do not delete or modify."]),
             pad([]), pad(["Aircraft Table"]),
             pad(["AircraftID", "TypeCode", "Year", "Make", "Model", "GearType", "EngineType", "equipType (FAA)",
                  "aircraftClass (FAA)", "complexAircraft (FAA)", "taa (FAA)", "highPerformance (FAA)", "pressurized (FAA)"]),
             pad(["N1TEST", "C172", "1979", "Cessna Aircraft", "172 Skyhawk", "fixed_tricycle", "Piston", "aircraft",
                  "airplane_single_engine_land"]),
             pad([]), pad(["Flights Table "]), HEADER]
    for r in rows:
        cells = []
        for h in core.FF_FLIGHT_HEADER:
            v = str(r.get(h, ""))
            cells.append('"%s"' % v.replace('"', '""') if ("," in v or '"' in v) else v)
        lines.append(",".join(cells))
    return ("\n".join(lines) + "\n").encode()


MINI = make_csv([
    {"Date": "2026-09-01", "AircraftID": "N1TEST", "From": "KAAA", "To": "KAAA", "Route": "KAAA KBBB KAAA", "TotalTime": "1.5",
     "CrossCountry": "1.5", "DualReceived": "1.5", "Distance": "42.50", "HobbsStart": "1200.10", "HobbsEnd": "1201.60",
     "PilotComments": "Pattern work, XC", "DayTakeoffs": "3", "DayLandingsFullStop": "3", "AllLandings": "3"},
    {"Date": "2026-09-01", "AircraftID": "N1TEST", "From": "KAAA", "To": "KAAA", "TotalTime": "0.5", "PIC": "0.5", "Solo": "0.5",
     "PilotComments": "First Solo", "DayTakeoffs": "3", "DayLandingsFullStop": "3", "AllLandings": "3",
     "Landing Full-Stop Day Towered": "2", "Takeoff Day Towered": "2"},
])


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app, follow_redirects=False) as c:
        c.get("/")  # get csrf cookie
        yield c


def token(c):
    return c.cookies.get("hl_csrf")


def n_flights(client):
    return len(set(re.findall(r"/flights/(\d+)/edit", client.get("/logbook").text)))


def test_empty_pages_render(client):
    for path in ["/", "/logbook", "/progress", "/scans", "/settings", "/data", "/flights/new", "/more"]:
        assert client.get(path).status_code == 200, path
    assert "Your logbook is empty" in client.get("/").text
    assert client.get("/profile").status_code == 301


def test_security_headers(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp and "font-src 'self'" in csp
    assert "<script" not in r.text and "style=" not in r.text  # nothing the CSP would block
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code == 404
    assert client.get("/static/fonts/barlow-latin-400-normal.woff2").status_code == 200


def test_logo_assets_served_and_referenced(client):
    html = client.get("/").text
    assert 'href="/static/favicon.svg"' in html and 'href="/static/apple-touch-icon.png"' in html
    svg = client.get("/static/favicon.svg")
    assert svg.status_code == 200 and "<svg" in svg.text and "#F26A21" in svg.text
    assert client.get("/static/apple-touch-icon.png").content[:4] == b"\x89PNG"
    assert "%2300" not in html and "#ee62ba" not in html  # the old magenta mark is gone
    umbrel = (Path(__file__).resolve().parents[2] / "hangar-pilot-logbook" / "icon.svg").read_text()
    assert 'rx=' not in umbrel.split("<rect", 2)[1].split("/>")[0]  # Umbrel rounds the icon itself


def test_post_requires_csrf(client):
    assert client.post("/data/import", data={"csrf": "wrong"}, files={"file": ("a.csv", b"x")}).status_code == 403
    assert client.post("/flights/new", data={"date": "2026-01-01"}).status_code == 403
    assert client.post("/settings", data={}).status_code == 403
    assert client.post("/theme", data={"theme": "dark"}).status_code == 403


def test_import_keeps_every_field_and_dedupes(client):
    r = client.post("/data/import", data={"csrf": token(client)}, files={"file": ("mini.csv", MINI, "text/csv")})
    assert r.status_code == 303 and "Imported+2+new" in r.headers["location"].replace("%20", "+")
    r = client.post("/data/import", data={"csrf": token(client)}, files={"file": ("mini.csv", MINI)})
    assert "already+imported" in r.headers["location"]
    page = client.get("/logbook").text
    assert page.count("N1TEST") >= 2 and "First Solo" in page
    # fields outside the default form still import (Hobbs, distance, towered landings)
    c = main.connect()
    row = c.execute("SELECT hobbs_start, hobbs_end, distance, landing_fs_day_towered FROM flights WHERE comments LIKE 'Pattern%'").fetchone()
    assert (row[0], row[1], row[2]) == (1200.1, 1201.6, 42.5)
    assert c.execute("SELECT landing_fs_day_towered FROM flights WHERE comments='First Solo'").fetchone()[0] == 2
    c.close()


def test_dashboard_widgets(client):
    html = client.get("/").text
    for needle in ["Total time", "Currency", "Hours per month", "Total hours over time", "Flying days", "Aircraft",
                   "Recent flights", "Private pilot progress", "<svg"]:
        assert needle in html, needle
    assert "N1TEST" in html


def test_bad_import_rejected(client):
    r = client.post("/data/import", data={"csrf": token(client)}, files={"file": ("x.csv", b"a,b\n1,2\n")})
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_add_validation_and_save(client):
    base = {"csrf": token(client), "date": "2026-09-10", "aircraft": "N1TEST", "total": "1.0", "pic": "1.5"}
    r = client.post("/flights/new", data=base)
    assert r.status_code == 422 and "more than total time" in r.text
    ok = dict(base, pic="1.0", solo="1.0", night="0.5", night_landings="3", day_landings="1")
    assert client.post("/flights/new", data=ok).status_code == 303
    assert "Sep 10, 2026" in client.get("/logbook").text
    r = client.post("/flights/new", data=dict(ok, date="2099-01-01"))
    assert r.status_code == 422 and "future" in r.text
    c = main.connect()  # takeoffs and all-landings follow the landings you entered
    row = c.execute("SELECT day_takeoffs, night_takeoffs, all_landings FROM flights WHERE date='2026-09-10'").fetchone()
    assert tuple(row) == (1, 3, 4)
    c.close()


def test_hours_accept_minutes(client):
    d = {"csrf": token(client), "date": "2026-09-14", "aircraft": "N1TEST", "total": "1:30", "day_landings": "1"}
    assert client.post("/flights/new", data=d).status_code == 303
    c = main.connect()
    assert c.execute("SELECT total FROM flights WHERE date='2026-09-14'").fetchone()[0] == 1.5
    c.close()
    assert client.post("/flights/new", data=dict(d, total="1:75")).status_code == 422


def test_new_aircraft_inline(client):
    d = {"csrf": token(client), "date": "2026-09-11", "new_tail": "n99xy", "new_make": "Van's", "new_model": "RV-12iS",
         "total": "0.8", "day_landings": "2"}
    assert client.post("/flights/new", data=d).status_code == 303
    assert '<td class="tail">N99XY</td>' in client.get("/logbook").text


def test_edit_keeps_fields_not_on_the_form(client):
    """Hobbs/distance aren't switched on, but they hold data, so they show, survive and stay editable."""
    c = main.connect()
    fid = c.execute("SELECT id FROM flights WHERE comments LIKE 'Pattern%'").fetchone()[0]
    c.close()
    html = client.get(f"/flights/{fid}/edit").text
    assert 'name="hobbs_start"' in html and 'value="1200.10"' in html
    other = client.get("/flights/new").text
    assert 'name="hobbs_start"' not in other  # not switched on, no data: stays out of the way
    r = client.post(f"/flights/{fid}/edit", data={
        "csrf": token(client), "date": "2026-09-02", "aircraft": "N1TEST", "total": "2.0", "dual_received": "2.0",
        "hobbs_start": "1200.10", "hobbs_end": "1202.10", "day_landings": "3", "distance": "42.5"})
    assert r.status_code == 303
    c = main.connect()
    row = c.execute("SELECT date, total, hobbs_end, all_landings, day_takeoffs FROM flights WHERE id=?", (fid,)).fetchone()
    assert tuple(row) == ("2026-09-02", 2.0, 1202.1, 3, 3)
    c.close()
    bad = client.post(f"/flights/{fid}/edit", data={"csrf": token(client), "date": "2026-09-02", "aircraft": "N1TEST",
                                                   "total": "2.0", "hobbs_start": "1200", "hobbs_end": "1100"})
    assert bad.status_code == 422 and "Hobbs end" in bad.text
    assert client.get("/flights/99999/edit").status_code == 404


def test_delete(client):
    html = client.get("/logbook").text
    fid = int(re.search(r"/flights/(\d+)/edit", html).group(1))
    assert client.post(f"/flights/{fid}/delete", data={"csrf": token(client)}).status_code == 303
    assert f"/flights/{fid}/edit" not in client.get("/logbook").text


def test_filters_and_search(client):
    assert '<td class="tail">N99XY</td>' not in client.get("/logbook?aircraft=N1TEST").text
    assert '<td class="tail">N99XY</td>' in client.get("/logbook?aircraft=N99XY").text
    assert "First Solo" in client.get("/logbook?q=solo").text
    assert "No flights match" in client.get("/logbook?q=zzzzzz").text


def test_settings_features_and_time_format(client):
    r = client.post("/settings", data={"csrf": token(client), "name": "Test Pilot", "flight_review": "2025-01-15",
                                       "medical_expires": "2030-01-01", "time_format": "hhmm",
                                       "feat_hobbs": "1", "feat_solo": "1", "feat_dual_received": "1", "feat_route": "1"})
    assert r.status_code == 303
    form = client.get("/flights/new").text
    assert 'name="hobbs_start"' in form and 'name="sim_instr"' not in form  # switched on / off as chosen
    assert "0:00" in form                                                  # H:MM placeholder
    dash = client.get("/").text
    assert "Welcome back, Test" in dash and "1:30" in client.get("/logbook").text or "Jan 31, 2027" in dash
    assert "Jan 31, 2027" in dash
    assert client.post("/settings", data={"csrf": token(client), "flight_review": "nope"}).status_code == 422
    client.post("/settings", data={"csrf": token(client), "time_format": "decimal1", "name": "Test Pilot",
                                   **{f"feat_{k}": "1" for k in core.FEATURE_DEFAULTS}})


def test_theme_cookie(client):
    r = client.post("/theme", data={"csrf": token(client), "theme": "dark", "next": "/logbook"})
    assert r.status_code == 303 and r.headers["location"] == "/logbook" and "hl_theme=dark" in r.headers["set-cookie"]
    r = client.post("/theme", data={"csrf": token(client), "theme": "dark", "next": "//evil.example"})
    assert r.headers["location"] == "/"
    client.cookies.set("hl_theme", "dark")
    assert 'data-theme="dark"' in client.get("/").text
    client.post("/theme", data={"csrf": token(client), "theme": ""})
    client.cookies.delete("hl_theme")


def test_scans(client):
    r = client.post("/scans", data={"csrf": token(client), "date_from": "2026-01-01", "note": "p1"},
                    files=[("files", ("page.png", PNG, "image/png"))])
    assert r.status_code == 303
    html = client.get("/scans").text
    sid = int(re.search(r"/scans/(\d+)/file", html).group(1))
    f = client.get(f"/scans/{sid}/file")
    assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content == PNG
    r = client.post("/scans", data={"csrf": token(client)}, files=[("files", ("evil.svg", b"<svg/>", "image/svg+xml"))])
    assert "error=" in r.headers["location"]
    assert client.get("/scans/999/file").status_code == 404


def test_exports(client):
    csv_text = client.get("/export/foreflight.csv").text
    assert csv_text.startswith("ForeFlight Logbook Import") and "Flights Table" in csv_text
    assert csv_text.count(HEADER) == 1  # all 65 ForeFlight columns, in order
    pdf = client.get("/export/logbook.pdf")
    assert pdf.content[:4] == b"%PDF"
    z = zipfile.ZipFile(io.BytesIO(client.get("/export/backup.zip").content))
    assert "logbook.db" in z.namelist() and any(n.startswith("scans/") for n in z.namelist())


def test_export_reimports_with_every_field():
    """Round trip: nothing is lost going out and back in, including fields that aren't on the form."""
    a = sqlite3.connect(":memory:"); a.row_factory = sqlite3.Row; core.ensure_schema(a)
    core.import_foreflight(a, MINI)
    core.add_flight(a, {"date": "2026-09-20", "aircraft": "N1TEST", "total": 1.0, "day_landings": 2})
    out = core.export_foreflight_csv(a)
    b = sqlite3.connect(":memory:"); b.row_factory = sqlite3.Row; core.ensure_schema(b)
    assert core.import_foreflight(b, out.encode())["new"] == 3
    cols = ",".join(core.FLIGHT_KEYS)
    key = lambda r: (r[0], str(r[core.FLIGHT_KEYS.index("comments")]), r[core.FLIGHT_KEYS.index("total")])  # noqa: E731
    ra = sorted(a.execute(f"SELECT {cols} FROM flights").fetchall(), key=key)
    rb = sorted(b.execute(f"SELECT {cols} FROM flights").fetchall(), key=key)
    for x, y in zip(ra, rb):
        for k in core.FLIGHT_KEYS:
            xv, yv = x[k], y[k]
            if k in ("takeoff_day", "landing_fs_day") and not xv:
                continue  # exporter derives these for hand-entered flights, on purpose
            if core.BY_KEY[k]["kind"] in ("text", "time"):
                xv, yv = xv or "", yv or ""
            elif not xv:
                xv, yv = xv or 0, yv or 0
            assert xv == yv, (k, xv, yv)


def test_old_database_upgrades_in_place():
    """A database from the first version (fewer columns, extra fields parked in extra_json) migrates cleanly."""
    d = sqlite3.connect(":memory:")
    d.executescript("""
    CREATE TABLE aircraft (ident TEXT PRIMARY KEY, type_code TEXT, year TEXT, make TEXT, model TEXT, gear_type TEXT,
        engine_type TEXT, aircraft_class TEXT, complex INTEGER, high_perf INTEGER, taa INTEGER, pressurized INTEGER);
    CREATE TABLE flights (id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, import_id INTEGER, date TEXT NOT NULL, aircraft TEXT,
        from_apt TEXT, to_apt TEXT, route TEXT, total REAL DEFAULT 0, pic REAL DEFAULT 0, sic REAL DEFAULT 0, night REAL DEFAULT 0,
        solo REAL DEFAULT 0, xc REAL DEFAULT 0, actual_instr REAL DEFAULT 0, sim_instr REAL DEFAULT 0, dual_received REAL DEFAULT 0,
        dual_given REAL DEFAULT 0, sim_flight REAL DEFAULT 0, ground_training REAL DEFAULT 0, day_landings INTEGER DEFAULT 0,
        night_landings INTEGER DEFAULT 0, all_landings INTEGER DEFAULT 0, instructor TEXT, comments TEXT, extra_json TEXT);
    INSERT INTO flights(fingerprint, date, aircraft, total, extra_json) VALUES ('x', '2025-05-05', 'N1', 1.2,
        '{"Distance": "12.30", "HobbsStart": "100.50", "Holds": "2", "SomeFutureColumn": "keep me"}');
    """)
    d.row_factory = sqlite3.Row
    core.ensure_schema(d)
    r = d.execute("SELECT distance, hobbs_start, holds, extra_json FROM flights").fetchone()
    assert (r["distance"], r["hobbs_start"], r["holds"]) == (12.3, 100.5, 2)
    assert json.loads(r["extra_json"]) == {"SomeFutureColumn": "keep me"}  # unknown columns are kept for export
    assert "SomeFutureColumn" in core.export_foreflight_csv(d)
    core.ensure_schema(d)  # running it again changes nothing


def test_currency_and_review_math():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row; core.ensure_schema(db)
    db.execute("INSERT INTO aircraft(ident, aircraft_class) VALUES ('N1', 'airplane_single_engine_land')")
    for d, n in (("2026-08-01", 1), ("2026-08-10", 1), ("2026-09-20", 2)):
        db.execute("INSERT INTO flights(fingerprint, date, aircraft, total, all_landings, day_landings) VALUES (?,?,?,?,?,?)",
                   (d, d, "N1", 1.0, n, n))
    cur = core.currency(db, date(2026, 10, 1))[0]
    assert cur["day_ok"] and cur["day_count"] == 4 and cur["day_through"] == date(2026, 11, 7)  # 3rd-newest landing Aug 10 + 89
    assert not cur["night_ok"]
    assert core.currency(db, date(2026, 12, 20))[0]["day_ok"] is False
    assert core.flight_review_through("2025-01-15") == date(2027, 1, 31)
    assert core.flight_review_through("2024-12-31") == date(2026, 12, 31)


def test_backup_restore_roundtrip_and_hostile_zip(client):
    before = n_flights(client)
    backup = client.get("/export/backup.zip").content
    client.post("/flights/new", data={"csrf": token(client), "date": "2026-09-13", "aircraft": "N1TEST", "total": "1.0"})
    assert n_flights(client) == before + 1
    r = client.post("/data/restore", data={"csrf": token(client)}, files={"file": ("b.zip", backup)})
    assert "confirm" in r.headers["location"].lower()
    r = client.post("/data/restore", data={"csrf": token(client), "confirm": "yes"}, files={"file": ("b.zip", backup)})
    assert "restored" in r.headers["location"].lower()
    assert n_flights(client) == before
    assert client.get("/scans").text.count("/file") >= 1  # scans came back

    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("logbook.db", b"not a database")
        z.writestr("scans/../../escape.png", PNG)
    r = client.post("/data/restore", data={"csrf": token(client), "confirm": "yes"}, files={"file": ("b.zip", evil.getvalue())})
    assert "error=" in r.headers["location"]
    assert not (Path(os.environ["DATA_DIR"]).parent / "escape.png").exists()
    assert n_flights(client) == before  # untouched after failed restore
    assert client.post("/data/restore", data={"csrf": token(client), "confirm": "yes"},
                       files={"file": ("n.zip", b"nope")}).headers["location"].count("error=") == 1


@pytest.mark.skipif(not FIXTURE, reason="no real export supplied")
def test_real_export_round_trip():
    raw = Path(FIXTURE).read_bytes()
    a = sqlite3.connect(":memory:"); a.row_factory = sqlite3.Row; core.ensure_schema(a)
    core.import_foreflight(a, raw)
    b = sqlite3.connect(":memory:"); b.row_factory = sqlite3.Row; core.ensure_schema(b)
    core.import_foreflight(b, core.export_foreflight_csv(a).encode())
    assert core.totals(a) == core.totals(b)
    oa, of = core.parse_foreflight(raw.decode("utf-8-sig"))
    na, nf = core.parse_foreflight(core.export_foreflight_csv(a))
    k = lambda r: (r["Date"], r["PilotComments"].strip('" '), r["TotalTime"], r["PIC"])  # noqa: E731
    for x, y in zip(sorted(of, key=k), sorted(nf, key=k)):
        for h in core.FF_FLIGHT_HEADER:
            xv, yv = x.get(h, "").strip('" '), y.get(h, "").strip('" ')
            try:
                assert float(xv or 0) == float(yv or 0), h
            except ValueError:
                assert xv == yv, h


# ============================================================================ Document Hangar

import hangar  # noqa: E402


def _jpeg():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1600, 1200), (40, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def _upload(client, name, data, mime="application/octet-stream", **form):
    return client.post("/hangar/add", data={"csrf": token(client), "category": "auto", **form},
                       files=[("files", (name, data, mime))])


def _doc_id(location):
    return int(re.search(r"/hangar/(\d+)", location).group(1))


def test_hangar_pages_and_csrf(client):
    for path in ["/hangar", "/hangar/add", "/hangar/note/new"]:
        assert client.get(path).status_code == 200, path
    assert "Your Hangar is empty" in client.get("/hangar").text
    assert client.post("/hangar/add", data={"csrf": "bad"}, files=[("files", ("a.pdf", b"x"))]).status_code == 403
    assert client.post("/hangar/note/new", data={"title": "x"}).status_code == 403
    assert client.post("/hangar/1/delete", data={}).status_code == 403
    assert client.get("/hangar/9999").status_code == 404


def test_hangar_rejects_dangerous_types(client):
    for name in ("page.html", "logo.svg", "run.sh", "tool.exe", "x.js", "noext"):
        r = _upload(client, name, b"<script>alert(1)</script>")
        assert r.status_code == 303 and "error=" in r.headers["location"], name
    assert "Your Hangar is empty" in client.get("/hangar").text


def test_hangar_upload_serve_and_thumbnail(client):
    r = _upload(client, "Insurance_policy_2026.pdf", b"%PDF-1.4 test", "application/pdf", expires="2026-10-20", aircraft="N1TEST")
    assert r.status_code == 303
    pid = _doc_id(r.headers["location"])
    page = client.get(f"/hangar/{pid}").text
    assert "Insurance policy 2026" in page and "Open PDF" in page and "N1TEST" in page
    f = client.get(f"/hangar/{pid}/file")
    assert f.status_code == 200 and f.headers["content-type"] == "application/pdf"
    assert f.headers["content-disposition"].startswith("inline") and f.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/hangar/{pid}/file?download=1").headers["content-disposition"].startswith("attachment")
    # photos get the Photos category by themselves and a real thumbnail
    jid = _doc_id(_upload(client, "cockpit.jpg", _jpeg(), "image/jpeg").headers["location"])
    assert "Photos" in client.get(f"/hangar/{jid}").text
    th = client.get(f"/hangar/{jid}/thumb")
    assert th.status_code == 200 and th.headers["content-type"] == "image/jpeg" and len(th.content) < len(_jpeg())
    # plain text shows its content in the page, safely escaped
    tid = _doc_id(_upload(client, "notes.txt", b"V speeds <b>Vx 62</b>", "text/plain").headers["location"])
    html = client.get(f"/hangar/{tid}").text
    assert "V speeds &lt;b&gt;Vx 62&lt;/b&gt;" in html
    assert client.get(f"/hangar/{tid}/file").headers["content-type"].startswith("text/plain")
    # Office files always download rather than open in the browser
    oid = _doc_id(_upload(client, "logbook.xlsx", b"PK\x03\x04 test").headers["location"])
    assert client.get(f"/hangar/{oid}/file").headers["content-disposition"].startswith("attachment")


def test_hangar_video_supports_range_requests(client):
    vid = _doc_id(_upload(client, "flight.mp4", bytes(range(256)) * 40, "video/mp4").headers["location"])
    assert "Videos" in client.get(f"/hangar/{vid}").text and "<video" in client.get(f"/hangar/{vid}").text
    r = client.get(f"/hangar/{vid}/file", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-range"].startswith("bytes 0-99/")


def test_hangar_multiple_files_and_partial_failure(client):
    r = client.post("/hangar/add", data={"csrf": token(client), "category": "manuals"},
                    files=[("files", ("POH.pdf", b"%PDF-1.4 a")), ("files", ("W&B.pdf", b"%PDF-1.4 b")), ("files", ("bad.html", b"x"))])
    loc = r.headers["location"]
    assert loc.startswith("/hangar?") and "Added+2+files" in loc and "bad.html" in loc
    page = client.get("/hangar?cat=manuals").text
    assert "POH" in page and "W&amp;B" in page


def test_hangar_notes(client):
    r = client.post("/hangar/note/new", data={"csrf": token(client), "title": "Pattern altitudes", "notes": "1000 AGL <ok>",
                                              "category": "notes", "expires": "2027-01-01"})
    nid = _doc_id(r.headers["location"])
    page = client.get(f"/hangar/{nid}").text
    assert "Pattern altitudes" in page and "1000 AGL &lt;ok&gt;" in page
    assert client.get(f"/hangar/{nid}/file").status_code == 404
    assert client.post("/hangar/note/new", data={"csrf": token(client), "title": "  "}).status_code == 422
    r = client.post(f"/hangar/{nid}", data={"csrf": token(client), "title": "Pattern altitudes v2", "category": "training",
                                            "notes": "updated", "expires": ""})
    assert r.status_code == 303 and "Pattern altitudes v2" in client.get(f"/hangar/{nid}").text


def test_hangar_edit_replace_and_delete_remove_files(client):
    pid = _doc_id(_upload(client, "renewal.pdf", b"%PDF-1.4 old", "application/pdf", expires="2026-01-01").headers["location"])
    row = main.hangar.get(main.connect(), pid)
    old_path = main.DOCS / row["filename"]
    assert old_path.exists()
    r = client.post(f"/hangar/{pid}", data={"csrf": token(client), "title": "Policy 2027", "category": "insurance",
                                            "expires": "2027-12-31", "aircraft": "N1TEST", "notes": "renewed"},
                    files={"file": ("renewal-2027.pdf", b"%PDF-1.4 new copy")})
    assert r.status_code == 303
    row = main.hangar.get(main.connect(), pid)
    assert row["title"] == "Policy 2027" and row["expires"] == "2027-12-31" and row["original_name"] == "renewal-2027.pdf"
    assert not old_path.exists() and (main.DOCS / row["filename"]).read_bytes() == b"%PDF-1.4 new copy"
    bad = client.post(f"/hangar/{pid}", data={"csrf": token(client), "title": "x", "category": "insurance", "expires": "13/45"})
    assert "error=" in bad.headers["location"]
    bad = client.post(f"/hangar/{pid}", data={"csrf": token(client), "title": "x", "category": "insurance"},
                      files={"file": ("x.html", b"<b>")})
    assert "error=" in bad.headers["location"]
    new_path = main.DOCS / row["filename"]
    assert client.post(f"/hangar/{pid}/delete", data={"csrf": token(client)}).status_code == 303
    assert not new_path.exists() and client.get(f"/hangar/{pid}").status_code == 404


def test_hangar_search_filter_and_sort(client):
    assert "cockpit" in client.get("/hangar?q=cockpit").text
    photos = client.get("/hangar?cat=photos").text
    assert "cockpit" in photos and "keepme" not in photos and "renewal" not in photos
    assert "cockpit" not in client.get("/hangar?cat=insurance").text
    assert "Nothing matches" in client.get("/hangar?q=zzzzzz").text
    assert client.get("/hangar?sort=expiry").status_code == 200 and client.get("/hangar?sort=name").status_code == 200


def test_expiry_math_and_dashboard_widget(client):
    t = date(2026, 10, 1)
    assert hangar.expiry_info("2026-09-20", t)["state"] == "expired"
    assert hangar.expiry_info("2026-10-31", t)["state"] == "soon"
    assert hangar.expiry_info("2026-11-15", t)["state"] == "upcoming"
    assert hangar.expiry_info("2027-06-01", t) == {"state": "ok", "days": 243, "text": "Expires Jun 1, 2027"}
    assert hangar.expiry_info("", t)["state"] == "none"
    soon = date.today().isoformat()
    _upload(client, "medical.pdf", b"%PDF-1.4 m", "application/pdf", expires=soon, title="Medical certificate")
    dash = client.get("/").text
    assert "Document Hangar" in dash and "Medical certificate" in dash and "Expires today" in dash
    assert "Medical certificate" in client.get("/hangar").text and "expired or expiring soon" in client.get("/hangar").text


def test_hangar_thumbnail_rebuilds_after_restore(client):
    jid = _doc_id(_upload(client, "again.jpg", _jpeg(), "image/jpeg").headers["location"])
    row = main.hangar.get(main.connect(), jid)
    thumb = main.THUMBS / (row["filename"] + ".jpg")
    thumb.unlink()
    assert client.get(f"/hangar/{jid}/thumb").status_code == 200 and thumb.exists()
    garbage = _doc_id(_upload(client, "broken.png", b"not really a png", "image/png").headers["location"])
    assert client.get(f"/hangar/{garbage}/thumb").status_code == 404


def test_backup_and_restore_include_the_hangar(client):
    n_before = len(set(re.findall(r'href="/hangar/(\d+)"', client.get("/hangar").text)))
    full = client.get("/export/backup.zip").content
    z = zipfile.ZipFile(io.BytesIO(full))
    assert any(n.startswith("documents/") for n in z.namelist()) and json.loads(z.read("backup.json"))["documents"] is True
    small = client.get("/export/backup.zip?documents=0").content
    zs = zipfile.ZipFile(io.BytesIO(small))
    assert not any(n.startswith("documents/") for n in zs.namelist()) and json.loads(zs.read("backup.json"))["documents"] is False
    # add a document, then restore the FULL backup: the new document goes away, the old ones come back
    extra = _doc_id(_upload(client, "temp.pdf", b"%PDF-1.4 temp", "application/pdf").headers["location"])
    r = client.post("/data/restore", data={"csrf": token(client), "confirm": "yes"}, files={"file": ("b.zip", full)})
    assert "restored" in r.headers["location"].lower()
    assert client.get(f"/hangar/{extra}").status_code == 404
    assert len(set(re.findall(r'href="/hangar/(\d+)"', client.get("/hangar").text))) == n_before
    # restoring a logbook-only backup must NOT wipe the Hangar that is there now
    keep = _doc_id(_upload(client, "keepme.pdf", b"%PDF-1.4 keep", "application/pdf").headers["location"])
    r = client.post("/data/restore", data={"csrf": token(client), "confirm": "yes"}, files={"file": ("s.zip", small)})
    assert "restored" in r.headers["location"].lower()
    page = client.get(f"/hangar/{keep}")
    assert page.status_code == 200 and "keepme" in page.text
    assert client.get(f"/hangar/{keep}/file").content == b"%PDF-1.4 keep"


def test_restore_ignores_hostile_document_entries(client):
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        db_path = main.DB_PATH
        zf.write(db_path, "logbook.db")
        zf.writestr("documents/../../escape.pdf", b"%PDF-1.4")
        zf.writestr("documents/shell.sh", b"echo hi")
        zf.writestr("documents/ok.pdf", b"%PDF-1.4 ok")
    r = client.post("/data/restore", data={"csrf": token(client), "confirm": "yes"}, files={"file": ("e.zip", evil.getvalue())})
    assert "restored" in r.headers["location"].lower()
    assert not (Path(os.environ["DATA_DIR"]).parent / "escape.pdf").exists()
    assert not (main.DOCS / "shell.sh").exists() and (main.DOCS / "ok.pdf").exists()


def test_upload_size_guard(client):
    r = client.post("/hangar/add", headers={"content-length": str(main.MAX_UPLOAD + 5 * 1024 * 1024)},
                    data={"csrf": token(client)}, files=[("files", ("a.pdf", b"x"))])
    assert r.status_code == 413 and "limit" in r.text


def test_old_database_without_documents_table_upgrades():
    d = sqlite3.connect(":memory:")
    d.execute("CREATE TABLE profile (key TEXT PRIMARY KEY, value TEXT)")
    d.row_factory = sqlite3.Row
    core.ensure_schema(d)
    assert d.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
