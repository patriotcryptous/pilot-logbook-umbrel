"""Server-rendered SVG widgets. No JavaScript, no external requests; colours come from CSS classes."""
import calendar
from datetime import date, datetime, timedelta

from markupsafe import Markup, escape

import logbook_core as core

NICE = [1, 2, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250, 300, 400, 500,
        600, 800, 1000, 1500, 2000, 3000, 5000, 10000]
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _nice(v):
    v = max(v, 1.0)
    return next((n for n in NICE if n >= v), int(v) + 1)


def _x(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


def monthly_bars(months, tf):
    """Hours per month for the last 12 months, stacked dual / solo / other."""
    W, H, L, R, T, B = 640, 232, 38, 8, 14, 40
    ymax = _nice(max((m["total"] for m in months), default=1) * 1.05)
    plot_w, plot_h = W - L - R, H - T - B
    slot = plot_w / len(months)
    bw = slot * 0.58
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Hours flown per month over the last 12 months" preserveAspectRatio="xMidYMid meet">']
    for i in range(3):
        val = ymax * i / 2
        y = T + plot_h - plot_h * val / ymax
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{_x(y)}" y2="{_x(y)}"/>')
        out.append(f'<text class="axis" x="{L - 8}" y="{_x(y + 4)}" text-anchor="end">{core.fmt_hours(val, "decimal1").rstrip("0").rstrip(".") if tf != "hhmm" else int(val)}</text>')
    for i, m in enumerate(months):
        x = L + i * slot + (slot - bw) / 2
        base = T + plot_h
        tip = (f'{MON[m["month"] - 1]} {m["year"]}: {core.fmt_hours(m["total"], tf)} h'
               f' ({core.fmt_hours(m["dual"], tf)} dual, {core.fmt_hours(m["solo"], tf)} solo)')
        out.append(f'<g class="bar"><title>{escape(tip)}</title>'
                   f'<rect class="hit" x="{_x(L + i * slot)}" y="{T}" width="{_x(slot)}" height="{plot_h}"/>')
        if m["total"] <= 0:
            out.append(f'<rect class="c-empty" x="{_x(x)}" y="{_x(base - 2)}" width="{_x(bw)}" height="2"/>')
        y = base
        for cls, key in (("c-dual", "dual"), ("c-solo", "solo"), ("c-other", "other")):
            h = plot_h * m[key] / ymax
            if h > 0:
                y -= h
                out.append(f'<rect class="{cls}" x="{_x(x)}" y="{_x(y)}" width="{_x(bw)}" height="{_x(h)}"/>')
        out.append("</g>")
        lx = L + i * slot + slot / 2
        out.append(f'<text class="axis" x="{_x(lx)}" y="{H - 20}" text-anchor="middle">{MON[m["month"] - 1]}</text>')
        if i == 0 or m["month"] == 1:
            out.append(f'<text class="axis year" x="{_x(lx)}" y="{H - 6}" text-anchor="middle">{m["year"]}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def cumulative_line(points, tf):
    """Running total of hours by date. Magenta dots mark flights with solo time."""
    W, H, L, R, T, B = 640, 232, 38, 14, 14, 30
    if not points:
        return Markup("")
    d0 = datetime.strptime(points[0][0], "%Y-%m-%d").date()
    d1 = datetime.strptime(points[-1][0], "%Y-%m-%d").date()
    span = max((d1 - d0).days, 30)
    ymax = _nice(points[-1][1] * 1.08)
    pw, ph = W - L - R, H - T - B

    def X(d):
        return L + pw * ((datetime.strptime(d, "%Y-%m-%d").date() - d0).days) / span

    def Y(v):
        return T + ph - ph * v / ymax

    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Cumulative flight hours, now {core.fmt_hours(points[-1][1], tf)}" preserveAspectRatio="xMidYMid meet">']
    for i in range(3):
        val = ymax * i / 2
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{_x(Y(val))}" y2="{_x(Y(val))}"/>')
        out.append(f'<text class="axis" x="{L - 8}" y="{_x(Y(val) + 4)}" text-anchor="end">{int(round(val))}</text>')
    pts = [(X(p[0]), Y(p[1])) for p in points]
    line = " ".join(f"{_x(x)},{_x(y)}" for x, y in pts)
    area = f"{_x(pts[0][0])},{_x(Y(0))} {line} {_x(pts[-1][0])},{_x(Y(0))}"
    out.append(f'<polygon class="area" points="{area}"/><polyline class="line" points="{line}"/>')
    for (x, y), p in zip(pts, points):
        if p[2]:
            out.append(f'<circle class="dot-solo" cx="{_x(x)}" cy="{_x(y)}" r="4"><title>'
                       f'{escape(p[0])}: {core.fmt_hours(p[1], tf)} h total, includes solo time</title></circle>')
    ex, ey = pts[-1]
    out.append(f'<circle class="dot-end" cx="{_x(ex)}" cy="{_x(ey)}" r="5"/>')
    anchor = "end" if ex > W - 70 else "start"
    out.append(f'<text class="end-label" x="{_x(ex + (-10 if anchor == "end" else 10))}" y="{_x(ey - 10)}" '
               f'text-anchor="{anchor}">{core.fmt_hours(points[-1][1], tf)} h</text>')
    for i in range(5):  # five evenly spaced date labels
        d = d0 + timedelta(days=span * i / 4)
        anchor = "start" if i == 0 else ("end" if i == 4 else "middle")
        out.append(f'<text class="axis" x="{_x(L + pw * i / 4)}" y="{H - 8}" text-anchor="{anchor}">'
                   f'{MON[d.month - 1]} {d.year}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def heatmap(heat, today, tf):
    """A year of flying, one cell per day (Sunday first)."""
    S, G = 11, 3
    P = S + G
    L, T = 30, 20
    wd = (today.weekday() + 1) % 7
    start = today - timedelta(days=wd + 52 * 7)
    W, H = L + 53 * P, T + 7 * P
    out = [f'<svg class="chart heat" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Days flown in the last year" preserveAspectRatio="xMinYMid meet">']
    for r, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append(f'<text class="axis" x="0" y="{T + r * P + S - 1}">{name}</text>')
    last_label = -9
    d = start
    for col in range(53):
        for row in range(7):
            day = start + timedelta(days=col * 7 + row)
            if day > today:
                continue
            if day.day <= 7 and row == 0 and col - last_label >= 3:
                out.append(f'<text class="axis" x="{L + col * P}" y="12">{MON[day.month - 1]}</text>')
                last_label = col
            h = heat.get(day.isoformat(), 0.0)
            lvl = 0 if h <= 0 else 1 if h <= 1.0 else 2 if h <= 1.6 else 3 if h <= 2.5 else 4
            tip = f"{MON[day.month - 1]} {day.day}, {day.year}: " + (f"{core.fmt_hours(h, tf)} h" if h else "no flights")
            out.append(f'<rect class="l{lvl}" x="{L + col * P}" y="{T + row * P}" width="{S}" height="{S}" rx="2">'
                       f'<title>{escape(tip)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))


def ring(frac, big, small, cls):
    """Circular gauge. frac is 0..1 of the ring to fill."""
    r, c = 40, 2 * 3.14159265 * 40
    frac = max(0.0, min(1.0, frac))
    return Markup(
        f'<svg class="ring {cls}" viewBox="0 0 100 100" role="img" aria-label="{escape(big)} {escape(small)}">'
        f'<circle class="ring-bg" cx="50" cy="50" r="{r}"/>'
        f'<circle class="ring-fg" cx="50" cy="50" r="{r}" stroke-dasharray="{_x(c * frac)} {_x(c)}" '
        f'transform="rotate(-90 50 50)"/>'
        f'<text class="ring-big" x="50" y="52" text-anchor="middle">{escape(big)}</text>'
        f'<text class="ring-small" x="50" y="67" text-anchor="middle">{escape(small)}</text></svg>')
