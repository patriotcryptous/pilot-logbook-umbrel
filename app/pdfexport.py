"""Printable logbook PDF: chronological pages with page totals, amount forwarded and total to date."""
import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROWS_PER_PAGE = 16
NUM_COLS = [("Total", "total"), ("Dual\nrcvd", "dual_received"), ("PIC", "pic"), ("Solo", "solo"),
            ("X-C", "xc"), ("Night", "night"), ("Instr.", "_instr"), ("Ldgs", "all_landings")]

INK = colors.HexColor("#10243A")
RULE = colors.HexColor("#8FA3B5")
BAND = colors.HexColor("#DCE7EF")


FMT = lambda v: "%.1f" % v  # replaced per export with the pilot's chosen time format


def _fmt(v, landings=False):
    if landings:
        return str(int(v)) if v else ""
    return FMT(v) if v else ""


def _sum(rows):
    s = {k: 0.0 for _, k in NUM_COLS}
    for r in rows:
        for _, k in NUM_COLS:
            s[k] += (r["actual_instr"] + r["sim_instr"]) if k == "_instr" else (r[k] or 0)
    return s


def build_pdf(flights, pilot_name="", fmt=None):
    global FMT
    FMT = fmt or (lambda v: "%.1f" % v)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=0.45 * inch,
                            rightMargin=0.45 * inch, topMargin=0.55 * inch, bottomMargin=0.5 * inch,
                            title="Pilot logbook", author=pilot_name or "Pilot")
    small = ParagraphStyle("s", fontName="Helvetica", fontSize=7.5, leading=9, textColor=INK)
    head = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=7, leading=8.5, textColor=INK, alignment=1)
    title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=11, textColor=INK, spaceAfter=6)

    pages = [flights[i:i + ROWS_PER_PAGE] for i in range(0, len(flights), ROWS_PER_PAGE)] or [[]]
    story, forwarded = [], {k: 0.0 for _, k in NUM_COLS}
    for n, chunk in enumerate(pages, 1):
        story.append(Paragraph(f"Pilot logbook{' - ' + pilot_name if pilot_name else ''}   "
                               f"<font size=8>page {n} of {len(pages)}</font>", title))
        header = [Paragraph("Date", head), Paragraph("Aircraft", head), Paragraph("Route", head),
                  Paragraph("Remarks", head)] + [Paragraph(h.replace("\n", "<br/>"), head) for h, _ in NUM_COLS]
        data = [header]
        for r in chunk:
            route = " - ".join(t for t in (r["route"] or "").replace(",", " ").replace("-", " ").split()) \
                or " - ".join(x for x in (r["from_apt"], r["to_apt"]) if x)
            data.append([r["date"], r["aircraft"] or "", Paragraph(route, small),
                         Paragraph((r["comments"] or "")[:160], small)] +
                        [_fmt((r["actual_instr"] + r["sim_instr"]) if k == "_instr" else r[k] or 0,
                              k == "all_landings") for _, k in NUM_COLS])
        page, running = _sum(chunk), None
        running = {k: forwarded[k] + page[k] for k in page}
        nrows = len(data)
        for label, vals in (("This page", page), ("Brought forward", forwarded), ("Total to date", running)):
            data.append(["", "", "", label] +
                        [_fmt(vals[k], k == "all_landings") or (FMT(0) if k != "all_landings" else "0")
                         for _, k in NUM_COLS])
        forwarded = running
        widths = [0.8 * inch, 0.75 * inch, 1.55 * inch, 2.7 * inch] + [0.52 * inch] * len(NUM_COLS)
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 7.5), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
            ("BACKGROUND", (0, 0), (-1, 0), BAND), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (4, 1), (-1, -1), "RIGHT"), ("ALIGN", (3, nrows), (3, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, nrows - 1), 0.4, RULE), ("BOX", (0, 0), (-1, -1), 0.8, INK),
            ("LINEABOVE", (0, nrows), (-1, nrows), 1.2, INK),
            ("FONT", (3, nrows), (-1, -1), "Helvetica-Bold", 7.5),
            ("LINEBEFORE", (4, 0), (4, -1), 0.8, INK), ("LINEBEFORE", (12, 0), (12, -1), 0.8, INK),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story += [t, Spacer(1, 6),
                  Paragraph(f"Generated {date.today().isoformat()} from a self-hosted logbook. "
                            "Instr. = actual + simulated instrument.", small)]
        if n < len(pages):
            from reportlab.platypus import PageBreak
            story.append(PageBreak())
    doc.build(story)
    return buf.getvalue()
