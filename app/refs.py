"""FAR/AIM in the Document Hangar, kept current from the official sources.

- FAR: the parts a private pilot uses, fetched as XML from the eCFR (ecfr.gov, the Office of the Federal
  Register's continuously updated edition) and typeset into one bookmarked PDF.
- AIM: the FAA's current consolidated PDF (basic edition with all changes), from faa.gov.

Both are US government works in the public domain. The server checks for new editions about once a week
when the pilot has this switched on (the default), and only downloads what has changed.
"""
import io
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle

ECFR = "https://www.ecfr.gov/api/versioner/v1/"
AIM_PAGE = "https://www.faa.gov/air_traffic/publications/"
UA = "pilot-logbook (self-hosted umbrelOS app)"

# (title, part, what it covers)
FAR_PARTS = [
    (14, 1, "Definitions and abbreviations"),
    (14, 43, "Maintenance, preventive maintenance, rebuilding and alteration"),
    (14, 61, "Certification: pilots, flight instructors and ground instructors"),
    (14, 67, "Medical standards and certification"),
    (14, 68, "Requirements for operating certain small aircraft without a medical certificate (BasicMed)"),
    (14, 73, "Special use airspace"),
    (14, 91, "General operating and flight rules"),
    (14, 103, "Ultralight vehicles"),
    (14, 107, "Small unmanned aircraft systems"),
    (14, 141, "Pilot schools"),
    (49, 830, "NTSB: notification and reporting of aircraft accidents or incidents"),
]

INK = colors.HexColor("#10243A")
SOFT = colors.HexColor("#526579")
S = {
    "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=26, leading=31, textColor=INK, spaceAfter=10),
    "part": ParagraphStyle("p", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=INK, spaceBefore=4, spaceAfter=8),
    "subpart": ParagraphStyle("sp", fontName="Helvetica-Bold", fontSize=11.5, leading=14.5, textColor=INK, spaceBefore=10, spaceAfter=4),
    "section": ParagraphStyle("s", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=INK, spaceBefore=9, spaceAfter=3),
    "hd": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=INK, spaceBefore=5, spaceAfter=2),
    "p": ParagraphStyle("b", fontName="Times-Roman", fontSize=9.5, leading=12.2, textColor=colors.black, spaceAfter=3),
    "small": ParagraphStyle("sm", fontName="Helvetica", fontSize=7, leading=9, textColor=SOFT, spaceAfter=3),
    "cell": ParagraphStyle("c", fontName="Times-Roman", fontSize=8, leading=10),
    "toc": ParagraphStyle("toc", fontName="Helvetica", fontSize=10, leading=15, textColor=INK),
}


# ----------------------------------------------------------------------------- fetching

def _open(url, timeout=60, tries=4):
    """GET with gzip and a few patient retries: the eCFR rate-limits bursts of requests."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed government hosts
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    data = gzip.decompress(data)
                return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(5 * (i + 1) ** 2, 60))
    raise last


def ecfr_versions():
    """{title number: 'YYYY-MM-DD' the eCFR is current as of}."""
    data = json.loads(_open(ECFR + "titles.json", timeout=30))
    return {t["number"]: t["up_to_date_as_of"] for t in data["titles"] if t["number"] in (14, 49)}


def far_version(versions):
    return max(versions.values())


def latest_aim_url():
    html = _open(AIM_PAGE, timeout=30).decode("utf-8", "replace")
    links = re.findall(r'href="([^"]*AIM_B(?:asic|sc)[^"]*\.pdf)"', html, re.I)
    if not links:
        raise ValueError("couldn't find the AIM on faa.gov")
    href = links[0]
    return href if href.startswith("http") else urllib.request.urljoin(AIM_PAGE, href)


def download_aim(url, dest: Path, limit=200 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    size = 0
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:  # noqa: S310
        while chunk := resp.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("the AIM download is unexpectedly large")
            out.write(chunk)
    with dest.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError("faa.gov didn't return a PDF")
    return size


# ----------------------------------------------------------------------------- typesetting the FAR

def _markup(el):
    """An XML element's mixed content as ReportLab paragraph markup."""
    out = [escape(el.text or "")]
    for ch in el:
        inner = _markup(ch)
        tag = ch.tag
        if tag in ("I",) or (tag == "E" and ch.get("T") in ("03", "04", "51", "52", "53", "54", None)):
            out.append(f"<i>{inner}</i>")
        elif tag == "E" and ch.get("T") in ("01", "02"):
            out.append(f"<b>{inner}</b>")
        elif tag in ("SU", "SUP"):
            out.append(f"<super>{inner}</super>")
        elif tag in ("FTREF", "FTNT"):
            pass
        else:
            out.append(inner)
        out.append(escape(ch.tail or ""))
    return re.sub(r"\s+", " ", "".join(out)).strip().replace("\u2044", "/")


_LEVELS = [re.compile(r"^\(([a-z]{1,2})\)"), re.compile(r"^\((\d+)\)"), re.compile(r"^\(([ivxl]+)\)"),
           re.compile(r"^\(([A-Z])\)")]


def _indent(text, prev):
    """Paragraph depth from its label: (a) 0, (1) 1, (i) 2, (A) 3. (i) can be a letter or a numeral."""
    t = re.sub(r"<[^>]+>", "", text)
    if _LEVELS[1].match(t):
        return 1
    if _LEVELS[3].match(t):
        return 3
    m0, m2 = _LEVELS[0].match(t), _LEVELS[2].match(t)
    if m2 and (not m0 or prev >= 1):
        return 2
    if m0:
        return 0
    return prev if prev >= 0 else 0


class Heading(Paragraph):
    def __init__(self, text, style, outline=None, level=0):
        super().__init__(text, style)
        self.outline, self.level = outline, level


class Doc(BaseDocTemplate):
    def __init__(self, buf, title, footer, **kw):
        super().__init__(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.7 * inch,
                         bottomMargin=0.7 * inch, title=title, author="Office of the Federal Register (eCFR)", **kw)
        self.footer = footer
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="f")
        self.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=self._decorate)])
        self._n, self._lvl = 0, -1

    def _decorate(self, canv, doc):
        canv.saveState()
        canv.setFont("Helvetica", 7)
        canv.setFillColor(SOFT)
        canv.drawString(self.leftMargin, 0.42 * inch, self.footer)
        canv.drawRightString(letter[0] - self.rightMargin, 0.42 * inch, str(doc.page))
        canv.restoreState()

    def afterFlowable(self, f):
        if isinstance(f, Heading) and f.outline:
            self._n += 1
            key = f"h{self._n}"
            self.canv.bookmarkPage(key)
            level = min(f.level, self._lvl + 1)  # a part with no subparts goes straight to sections
            self._lvl = level
            self.canv.addOutlineEntry(f.outline, key, level=level, closed=level >= 1)


def _table(el, width):
    rows = []
    for tr in el.iter("TR"):
        cells = [Paragraph(_markup(c), S["cell"]) for c in tr if c.tag in ("TD", "TH", "ENT")]
        if cells:
            rows.append(cells)
    if not rows:
        return None
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    t = Table(rows, colWidths=[width / n] * n, repeatRows=1 if el.find(".//TH") is not None else 0)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, SOFT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


def _flow(el, story, width, state):
    tag = el.tag
    if tag in ("DIV1", "DIV2", "DIV3", "DIV4", "DIV5", "DIV6", "DIV7", "DIV8", "DIV9"):
        head = el.find("HEAD")
        text = _markup(head) if head is not None else ""
        if tag == "DIV5":
            story.append(Heading(text, S["part"], re.sub(r"<[^>]+>", "", text), 0))
        elif tag in ("DIV6", "DIV9"):
            story.append(Heading(text, S["subpart"], re.sub(r"<[^>]+>", "", text), 1))
        elif tag == "DIV7":
            story.append(Paragraph(text, S["hd"]))
        elif tag == "DIV8":
            story.append(Heading(text, S["section"], re.sub(r"<[^>]+>", "", text), 2))
        state["prev"] = -1
        for ch in el:
            if ch.tag != "HEAD":
                _flow(ch, story, width, state)
        return
    if tag in ("P", "FP"):
        text = _markup(el)
        if not text:
            return
        lvl = _indent(text, state.get("prev", -1))
        state["prev"] = lvl
        st = ParagraphStyle(f"p{lvl}", parent=S["p"], leftIndent=lvl * 14)
        story.append(Paragraph(text, st))
    elif tag in ("HD1", "HD2", "HD3", "HED", "TTITLE"):
        story.append(Paragraph(_markup(el), S["hd"]))
    elif tag in ("CITA", "SOURCE", "AUTH", "SECAUTH", "EDNOTE", "EFFDNOT", "APPRO"):
        text = _markup(el)
        if text:
            story.append(Paragraph(text, S["small"]))
    elif tag in ("TABLE", "GPOTABLE"):
        t = _table(el, width)
        if t:
            story.append(Spacer(1, 3))
            story.append(t)
            story.append(Spacer(1, 5))
    elif tag in ("GPH", "GID", "FTNT", "PSPACE"):
        return
    else:
        for ch in el:
            _flow(ch, story, width, state)


def build_far_pdf(parts_xml, version, footer=None):
    """parts_xml: list of (title, part, description, xml bytes). Returns PDF bytes."""
    buf = io.BytesIO()
    footer = footer or f"Federal Aviation Regulations from the eCFR, current as of {version}. Not an official legal edition."
    doc = Doc(buf, f"FAR (eCFR {version})", footer)
    story = [Spacer(1, 1.6 * inch), Paragraph("Federal Aviation Regulations", S["title"]),
             Paragraph(f"Selected parts of 14 CFR and 49 CFR part 830, from the eCFR current as of {version}.", S["p"]),
             Spacer(1, 18)]
    for title, part, desc, _ in parts_xml:
        story.append(Paragraph(f"<b>{title} CFR part {part}</b> &nbsp; {escape(desc)}", S["toc"]))
    story += [Spacer(1, 24),
              Paragraph("Assembled automatically by your Pilot Log from www.ecfr.gov, the Office of the Federal "
                        "Register's continuously updated online edition of the Code of Federal Regulations. It is "
                        "authoritative but not an official legal edition of the CFR. Use the bookmarks to jump to a "
                        "part, subpart or section. Check ecfr.gov for amendments after the date above.", S["small"])]
    for title, part, desc, xml in parts_xml:
        story.append(PageBreak())
        root = ET.fromstring(xml)
        _flow(root, story, doc.width, {"prev": -1})
    doc.build(story)
    return buf.getvalue()


def fetch_far(versions, pause=2.0):
    out = []
    for title, part, desc in FAR_PARTS:
        xml = _open(f"{ECFR}full/{versions[title]}/title-{title}.xml?part={part}", timeout=120)
        out.append((title, part, desc, xml))
        time.sleep(pause)
    return out


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
