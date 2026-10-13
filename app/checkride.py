"""Checkride packet: one PDF with experience against 61.109(a), endorsements, currency and key documents."""
import io
from datetime import date
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Flowable
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

INK = colors.HexColor("#10243A")
RULE = colors.HexColor("#8FA3B5")
BAND = colors.HexColor("#DCE7EF")
GREEN = colors.HexColor("#12744D")
RED = colors.HexColor("#B3261E")
AMBER = colors.HexColor("#915200")
ORANGE = colors.HexColor("#F26A21")

S = {
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK, spaceAfter=4),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=INK, spaceBefore=12, spaceAfter=6),
    "p": ParagraphStyle("p", fontName="Helvetica", fontSize=9, leading=12, textColor=INK),
    "small": ParagraphStyle("s", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#526579")),
    "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=INK),
    "head": ParagraphStyle("hd", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=INK),
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class Box(Flowable):
    """An empty tick box, drawn rather than typed so it shows in every PDF viewer."""

    def wrap(self, *_):
        return 9, 9

    def draw(self):
        self.canv.setStrokeColor(INK)
        self.canv.setLineWidth(0.8)
        self.canv.rect(0, 0, 9, 9, stroke=1, fill=0)


def _table(data, widths, status_col=None):
    rows = [[Paragraph(str(c), S["head"]) for c in data[0]]]
    for r in data[1:]:
        rows.append([c if not isinstance(c, str) else Paragraph(c, S["cell"]) for c in r])
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("BOX", (0, 0), (-1, -1), 0.8, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _status(text, color):
    return Paragraph(f'<font color="{color.hexval()}"><b>{text}</b></font>', S["cell"])


def build(prof, progress, totals, endorsements, documents, currency, reminders, aircraft, fmt, docs_dir=None,
          include_images=True, today=None):
    today = today or date.today()
    buf = io.BytesIO()
    name = prof.get("name") or "Pilot"
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"Checkride packet - {name}", author=name)
    W = letter[0] - 1.2 * inch
    st = []

    def hrs(v):
        return fmt(v) if v else fmt(0)

    st.append(Paragraph("Checkride packet", S["h1"]))
    st.append(Paragraph(f"<b>{name}</b> &nbsp; {prof.get('certificate') or ''}"
                        f"{' &nbsp;&middot;&nbsp; Medical: ' + prof['medical_class'] if prof.get('medical_class') else ''}"
                        f" &nbsp;&middot;&nbsp; Prepared {today.strftime('%B %-d, %Y')}", S["p"]))
    st.append(Spacer(1, 4))
    st.append(Paragraph("A summary from a self-hosted pilot logbook to bring to the practical test. "
                        "Your logbook, endorsements and certificates are the official records; your instructor "
                        "and examiner decide eligibility.", S["small"]))

    # totals
    st.append(Paragraph("Flight time", S["h2"]))
    cells = [("Total", totals["total"]), ("Dual received", totals["dual_received"]), ("PIC", totals["pic"]),
             ("Solo", totals["solo"]), ("X-country", totals["xc"]), ("Night", totals["night"]),
             ("Instrument", (totals["sim_instr"] or 0) + (totals["actual_instr"] or 0)),
             ("Landings", None)]
    row1 = [Paragraph(k, S["head"]) for k, _ in cells]
    row2 = [Paragraph(f"<b>{hrs(v)}</b>" if v is not None else f"<b>{int(totals['landings'])}</b>", S["cell"])
            for _, v in cells]
    t = Table([row1, row2], colWidths=[W / len(cells)] * len(cells))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), BAND), ("BOX", (0, 0), (-1, -1), 0.8, INK),
                           ("INNERGRID", (0, 0), (-1, -1), 0.4, RULE), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    st.append(t)
    st.append(Spacer(1, 3))
    st.append(Paragraph(f"{int(totals['flights'])} flights logged. Aircraft flown: " +
                        (", ".join(f"{a['ident']} ({hrs(a['hours'])} h)" for a in aircraft[:8]) or "none"), S["small"]))

    # 61.109
    st.append(Paragraph(f"Aeronautical experience: {progress.get('title', 'private pilot ASEL')} (FAR {progress.get('far', '61.109(a)')})", S["h2"]))
    data = [["Requirement", "Logged", "Minimum", "Status"]]
    for label, have, need, unit in progress["tracked"]:
        val = hrs(have) if unit == "hrs" else str(int(have))
        data.append([label, val, f"{need}{' h' if unit == 'hrs' else ''}",
                     _status("Met", GREEN) if have >= need else _status("Not yet", AMBER)])
    st.append(_table(data, [W * .52, W * .16, W * .16, W * .16]))
    if progress.get("checks"):
        st.append(Spacer(1, 6))
        data = [["Specific flight", "From your logbook", "Status"]]
        for chk in progress["checks"]:
            data.append([f'{chk["label"]} ({chk["far"]})', Paragraph(chk["detail"], S["small"]),
                         _status("Met", GREEN) if chk["met"] else _status("Not yet", AMBER)])
        st.append(_table(data, [W * .34, W * .50, W * .16]))
    st.append(Paragraph("Cross-country, night and instrument training lines are estimated from the overlap of the "
                        "categories logged on each flight.", S["small"]))

    # endorsements
    st.append(Paragraph("Endorsements", S["h2"]))
    if endorsements:
        data = [["Date", "Endorsement", "FAR", "Instructor", "Valid through"]]
        for e in endorsements:
            if e["superseded"]:
                status = _status("Superseded", RULE)
            elif e["expires"]:
                status = _status(e["expires"] + (" (expired)" if e["state"] == "expired" else ""),
                                 RED if e["state"] == "expired" else AMBER if e["state"] == "soon" else GREEN)
            else:
                status = "No expiry"
            cfi = e["cfi_name"] or ""
            if e["cfi_cert"]:
                cfi += f"<br/>{e['cfi_cert']}" + (f" exp. {e['cfi_cert_expires']}" if e["cfi_cert_expires"] else "")
            data.append([e["date"], e["title"], e["far_ref"] or "", cfi, status])
        st.append(_table(data, [W * .12, W * .36, W * .16, W * .20, W * .16]))
    else:
        st.append(Paragraph("No endorsements recorded in the logbook app.", S["p"]))

    # currency and dates
    st.append(Paragraph("Currency and certificates", S["h2"]))
    data = [["Item", "Status", "Good through"]]
    for r in reminders:
        data.append([r["label"], _status("Valid", GREEN) if r["ok"] else _status("Expired", RED),
                     r["through"].strftime("%b %-d, %Y")])
    for k in currency:
        for nm in ("day", "night"):
            ok, thru, cnt = k[f"{nm}_ok"], k[f"{nm}_through"], k[f"{nm}_count"]
            data.append([f"Passenger currency, {nm} ({k['label']})",
                         _status("Current", GREEN) if ok else _status(f"{cnt} of 3 landings", AMBER),
                         thru.strftime("%b %-d, %Y") if thru else "-"])
    st.append(_table(data, [W * .5, W * .25, W * .25]) if len(data) > 1 else
              Paragraph("Add your medical and flight review dates in Settings.", S["p"]))

    # documents
    st.append(Paragraph("Documents on file", S["h2"]))
    if documents:
        data = [["Document", "Category", "Aircraft", "Expires"]]
        for d in documents:
            exp = d["expires"] or "-"
            if d["exp"]["state"] == "expired":
                exp = _status(exp + " (expired)", RED)
            data.append([d["title"], d["cat_label"], d["aircraft"] or "", exp])
        st.append(_table(data, [W * .44, W * .26, W * .14, W * .16]))
    else:
        st.append(Paragraph("No pilot, training, aircraft or insurance documents in the Document Hangar.", S["p"]))
    checklist = ["Government-issued photo ID", "Student pilot certificate", "Medical certificate (or BasicMed paperwork)",
                 "Knowledge test report", "Logbook with endorsements", "IACRA application ID",
                 "Aircraft documents: airworthiness, registration, POH/AFM, weight and balance",
                 "Aircraft maintenance records: annual, 100-hour if required, ADs, transponder, ELT",
                 "Examiner fee", "Charts, plotter, E6B and the cross-country plan the examiner assigned"]
    st.append(Paragraph("Bring to the checkride", S["h2"]))
    st.append(_table([["", "Item"]] + [[Box(), c] for c in checklist], [W * .06, W * .94]))

    # endorsement photos
    if include_images and docs_dir:
        pics = []
        for e in endorsements:
            d = e.get("doc")
            if d and d.get("filename") and Path(d.get("original_name") or "").suffix.lower() in IMAGE_EXT:
                p = Path(docs_dir) / d["filename"]
                if p.is_file():
                    pics.append((e, p))
        for e, p in pics:
            try:
                with PILImage.open(p) as im:
                    w, h = im.size
            except Exception:  # noqa: BLE001 - a broken image shouldn't stop the packet
                continue
            maxw, maxh = W, letter[1] - 2.4 * inch
            scale = min(maxw / w, maxh / h)
            st.append(PageBreak())
            st.append(KeepTogether([Paragraph(f"{e['title']} ({e['date']})", S["h2"]),
                                    Image(str(p), width=w * scale, height=h * scale)]))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(ORANGE)
        canvas.rect(0.6 * inch, letter[1] - 0.42 * inch, W, 3, stroke=0, fill=1)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#526579"))
        canvas.drawString(0.6 * inch, 0.38 * inch, f"Checkride packet - {name}")
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.38 * inch, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(st, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
