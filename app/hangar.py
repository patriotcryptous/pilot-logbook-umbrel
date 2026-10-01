"""Document Hangar: private storage for insurance, certificates, manuals, notes, photos and videos.

Pure helpers and queries; the web routes live in main.py. Files are stored under random names,
only allow-listed types are accepted, and nothing here talks to the network.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps

CATEGORIES = [
    ("insurance", "Insurance"),
    ("pilot", "Pilot certificates and medical"),
    ("aircraft", "Aircraft papers"),
    ("manuals", "POH and manuals"),
    ("training", "Training and endorsements"),
    ("notes", "Notes"),
    ("photos", "Photos"),
    ("videos", "Videos"),
    ("other", "Other"),
]
CAT_LABEL = dict(CATEGORIES)
CAT_HINT = {
    "insurance": "Policies, certificates of insurance, renewals",
    "pilot": "Pilot certificate, medical, CFI certificate, BasicMed",
    "aircraft": "Registration, airworthiness, weight and balance, maintenance logs",
    "manuals": "POH or AFM, checklists, supplements, avionics guides",
    "training": "Syllabus, endorsements, test results, instructor notes",
    "notes": "Anything you want to jot down",
    "photos": "Pictures of paperwork, your aircraft, flights",
    "videos": "Flight and training videos",
    "other": "Everything else",
}

# extension -> (kind, content type, safe to show inside the browser)
_TEXT = "text/plain; charset=utf-8"
_OFFICE = "application/vnd.openxmlformats-officedocument."
TYPES = {
    ".pdf": ("pdf", "application/pdf", True),
    ".jpg": ("image", "image/jpeg", True), ".jpeg": ("image", "image/jpeg", True),
    ".png": ("image", "image/png", True), ".webp": ("image", "image/webp", True),
    ".gif": ("image", "image/gif", True),
    ".heic": ("photo-file", "image/heic", False), ".heif": ("photo-file", "image/heif", False),
    ".mp4": ("video", "video/mp4", True), ".m4v": ("video", "video/x-m4v", True),
    ".mov": ("video", "video/quicktime", True), ".webm": ("video", "video/webm", True),
    ".mp3": ("audio", "audio/mpeg", True), ".m4a": ("audio", "audio/mp4", True),
    ".wav": ("audio", "audio/wav", True),
    ".txt": ("text", _TEXT, True), ".md": ("text", _TEXT, True), ".csv": ("text", _TEXT, True),
    ".json": ("text", _TEXT, True), ".log": ("text", _TEXT, True),
    ".doc": ("office", "application/msword", False),
    ".docx": ("office", _OFFICE + "wordprocessingml.document", False),
    ".xls": ("office", "application/vnd.ms-excel", False),
    ".xlsx": ("office", _OFFICE + "spreadsheetml.sheet", False),
    ".ppt": ("office", "application/vnd.ms-powerpoint", False),
    ".pptx": ("office", _OFFICE + "presentationml.presentation", False),
    ".rtf": ("office", "application/rtf", False), ".odt": ("office", "application/octet-stream", False),
    ".ods": ("office", "application/octet-stream", False), ".odp": ("office", "application/octet-stream", False),
    ".pages": ("office", "application/octet-stream", False), ".numbers": ("office", "application/octet-stream", False),
    ".key": ("office", "application/octet-stream", False),
    ".zip": ("archive", "application/zip", False),
}
KIND_LABEL = {"pdf": "PDF", "image": "Photo", "photo-file": "Photo", "video": "Video", "audio": "Audio",
              "text": "Text file", "office": "Document", "archive": "Archive", "note": "Note"}


def type_of(name):
    """(kind, content type, inline) for a file name, or None when the type isn't allowed."""
    return TYPES.get(Path(name or "").suffix.lower())


def auto_category(kind):
    return {"image": "photos", "photo-file": "photos", "video": "videos"}.get(kind, "other")


def human_size(n):
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
        n /= 1024


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def expiry_info(expires, today=None):
    """How close an expiry date is. state: none | expired | soon (30 days) | upcoming (90 days) | ok."""
    if not expires:
        return {"state": "none", "days": None, "text": ""}
    today = today or date.today()
    d = _d(expires)
    days = (d - today).days
    if days < 0:
        text = "Expired yesterday" if days == -1 else f"Expired {-days} days ago"
        return {"state": "expired", "days": days, "text": text}
    if days == 0:
        return {"state": "soon", "days": 0, "text": "Expires today"}
    if days <= 30:
        return {"state": "soon", "days": days, "text": f"Expires in {days} day{'s' if days != 1 else ''}"}
    if days <= 90:
        return {"state": "upcoming", "days": days, "text": f"Expires in {days} days"}
    return {"state": "ok", "days": days, "text": "Expires " + d.strftime("%b %-d, %Y")}


def decorate(row, docs_dir, today=None):
    """A database row plus the display details every page needs."""
    d = dict(row)
    t = type_of(d.get("original_name") or d.get("filename") or "")
    d["tkind"] = "note" if d["kind"] == "note" else (t[0] if t else "other")
    d["type_label"] = KIND_LABEL.get(d["tkind"], "File")
    d["ext"] = Path(d.get("original_name") or "").suffix.lstrip(".").upper()[:5] if d["kind"] == "file" else "NOTE"
    d["cat_label"] = CAT_LABEL.get(d["category"], "Other")
    d["size_h"] = human_size(d.get("size"))
    d["exp"] = expiry_info(d.get("expires"), today)
    d["exists"] = d["kind"] == "note" or bool(d.get("filename") and (Path(docs_dir) / d["filename"]).is_file())
    d["has_thumb"] = d["tkind"] == "image" and d["exists"]
    d["when"] = (d.get("created_at") or "")[:10]
    return d


def safe_path(docs_dir, name):
    """Resolve a stored file name, refusing anything that would leave the documents folder."""
    root = Path(docs_dir).resolve()
    p = (root / (name or "x")).resolve()
    if root not in p.parents:
        raise ValueError("bad path")
    return p


def make_thumb(src, dst, size=480):
    """Write a small JPEG preview of an image. Returns False when the image can't be read."""
    try:
        Image.MAX_IMAGE_PIXELS = 120_000_000
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, "JPEG", quality=82)
        return True
    except Exception:  # noqa: BLE001 - any unreadable or oversized image just gets no preview
        Path(dst).unlink(missing_ok=True)
        return False


# ----------------------------------------------------------------------------- queries

def now():
    return datetime.now(timezone.utc).isoformat()


def create(db, **v):
    ts = now()
    cur = db.execute(
        "INSERT INTO documents(kind, title, category, aircraft, expires, notes, filename, original_name, mime, size,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (v.get("kind", "file"), v["title"], v["category"], v.get("aircraft") or "", v.get("expires") or "",
         v.get("notes") or "", v.get("filename") or "", v.get("original_name") or "", v.get("mime") or "",
         v.get("size") or 0, ts, ts))
    db.commit()
    return cur.lastrowid


def update(db, did, **v):
    cols = [k for k in ("title", "category", "aircraft", "expires", "notes", "filename", "original_name", "mime", "size")
            if k in v]
    db.execute(f"UPDATE documents SET {','.join(c + '=?' for c in cols)}, updated_at=? WHERE id=?",
               (*[v[c] for c in cols], now(), did))
    db.commit()


def get(db, did):
    return db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()


def delete(db, did):
    db.execute("DELETE FROM documents WHERE id=?", (did,))
    db.commit()


def listing(db, cat="", q="", aircraft="", sort="new", page=1, per=60):
    where, params = ["1=1"], []
    if cat in CAT_LABEL:
        where.append("category=?")
        params.append(cat)
    if aircraft:
        where.append("aircraft=?")
        params.append(aircraft)
    if q.strip():
        where.append("(title LIKE ? OR notes LIKE ? OR original_name LIKE ?)")
        params += [f"%{q.strip()}%"] * 3
    order = {"name": "lower(title), id", "expiry": "(expires IS NULL OR expires=''), expires, id DESC"}.get(
        sort, "created_at DESC, id DESC")
    w = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM documents WHERE {w}", params).fetchone()[0]
    rows = db.execute(f"SELECT * FROM documents WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                      (*params, per, (max(page, 1) - 1) * per)).fetchall()
    return rows, total


def category_counts(db):
    counts = {k: 0 for k, _ in CATEGORIES}
    for k, n in db.execute("SELECT category, COUNT(*) FROM documents GROUP BY category"):
        counts[k if k in counts else "other"] += n
    return counts


def attention(db, today=None, days=30):
    """Documents that are expired or expire within `days`."""
    today = today or date.today()
    return db.execute(
        "SELECT * FROM documents WHERE expires IS NOT NULL AND expires!='' AND expires<=? ORDER BY expires",
        ((today + timedelta(days=days)).isoformat(),)).fetchall()


def summary(db, docs_dir, today=None, days=60, limit=4):
    """What the dashboard widget shows."""
    today = today or date.today()
    n, size = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM documents").fetchone()
    soon = [decorate(r, docs_dir, today) for r in attention(db, today, days)]
    return {"count": n, "size": human_size(size), "expiring": soon[:limit], "expiring_total": len(soon)}
