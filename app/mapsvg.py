"""Server-rendered SVG: route map, currency forecast and monthly cost bars. No JavaScript, no map tiles."""
import gzip
import json
import math
from datetime import timedelta
from functools import lru_cache

from markupsafe import Markup, escape

from charts import MON, _nice, _x
from features import GEO, _airports


@lru_cache(maxsize=1)
def _state_arcs():
    return json.loads((GEO / "us-states-arcs.json").read_text())


@lru_cache(maxsize=1)
def _county_arcs():
    with gzip.open(GEO / "us-counties-arcs.json.gz", "rt") as f:
        return json.load(f)


def route_map(stats, W=820, H=520):
    """Airports you visited, sized by visits, joined by the legs you flew, over US state lines."""
    aps = stats["airports"]
    if not aps:
        return None
    by_icao = _airports()[0]
    lats = [a["ap"]["lat"] for a in aps]
    lons = [a["ap"]["lon"] for a in aps]
    lat_c, lon_c = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    k = math.cos(math.radians(lat_c))
    half_h = max((max(lats) - min(lats)) / 2 * 1.25, 0.35)
    half_w = max((max(lons) - min(lons)) / 2 * k * 1.15, 0.35)
    # match the viewBox shape
    if half_w / half_h < W / H:
        half_w = half_h * W / H
    else:
        half_h = half_w * H / W
    sc = (W / 2) / half_w

    def xy(lat, lon):
        return W / 2 + (lon - lon_c) * k * sc, H / 2 - (lat - lat_c) * sc

    out = [f'<svg class="chart map" viewBox="0 0 {W} {H}" role="img" aria-label="Map of the airports you have flown to">',
           f'<defs><clipPath id="mapclip"><rect width="{W}" height="{H}" rx="10"/></clipPath></defs>',
           f'<g clip-path="url(#mapclip)"><rect class="map-bg" fill="#f4f7fb" width="{W}" height="{H}"/>']
    lat_lo, lat_hi = lat_c - half_h * 1.1, lat_c + half_h * 1.1
    lon_lo, lon_hi = lon_c - half_w / k * 1.1, lon_c + half_w / k * 1.1
    layers = [("map-state", _state_arcs())]
    if half_h < 2.5:  # zoomed in to local flying: county lines help you find your way around
        layers.insert(0, ("map-county", _county_arcs()))
    for cls, arcs in layers:
        for arc in arcs:
            if not any(lon_lo <= p[0] <= lon_hi and lat_lo <= p[1] <= lat_hi for p in arc):
                continue
            pts = " ".join(f"{_x(x)},{_x(y)}" for x, y in (xy(p[1], p[0]) for p in arc))
            out.append(f'<polyline class="{cls}" fill="none" stroke="#9db0c4" stroke-width="{1 if cls == "map-state" else .6}" points="{pts}"/>')
    # legs
    for (a1, a2), n in sorted(stats["legs"].items(), key=lambda x: x[1]):
        p1, p2 = by_icao[a1], by_icao[a2]
        x1, y1 = xy(p1["lat"], p1["lon"])
        x2, y2 = xy(p2["lat"], p2["lon"])
        w = 1.6 + min(n, 8) * 0.55
        out.append(f'<line class="map-leg" stroke="#b2227c" x1="{_x(x1)}" y1="{_x(y1)}" x2="{_x(x2)}" y2="{_x(y2)}" stroke-width="{_x(w)}">'
                   f'<title>{escape(p1["lid"] or a1)} – {escape(p2["lid"] or a2)}: {n} time{"s" if n != 1 else ""}</title></line>')
    # airports, most visited drawn last so they sit on top
    top = aps[0]["ap"]["icao"]
    for a in sorted(aps, key=lambda a: a["visits"]):
        x, y = xy(a["ap"]["lat"], a["ap"]["lon"])
        r = 4.5 + min(math.sqrt(a["visits"]) * 1.6, 9)
        cls = "map-apt home" if a["ap"]["icao"] == top else "map-apt"
        tip = f'{a["code"]} {a["ap"]["name"]}: {a["visits"]} flight{"s" if a["visits"] != 1 else ""}, {a["landings"]} landings'
        out.append(f'<g class="{cls}"><title>{escape(tip)}</title><circle fill="{'#F26A21' if 'home' in cls else '#0a7ea6'}" stroke="#fff" stroke-width="2" cx="{_x(x)}" cy="{_x(y)}" r="{_x(r)}"/>'
                   f'<text class="map-label" x="{_x(x + r + 4)}" y="{_x(y + 4)}">{escape(a["code"])}</text></g>')
    # scale bar in nautical miles (1 degree of latitude = 60 nm)
    nm_px = sc / 60
    target = W * 0.18 / nm_px
    nice = next((n for n in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000) if n >= target), 1000)
    bar = nice * nm_px
    out.append(f'<g class="map-scale"><line x1="20" x2="{_x(20 + bar)}" y1="{H - 22}" y2="{H - 22}"/>'
               f'<line x1="20" x2="20" y1="{H - 27}" y2="{H - 17}"/><line x1="{_x(20 + bar)}" x2="{_x(20 + bar)}" y1="{H - 27}" y2="{H - 17}"/>'
               f'<text x="{_x(20 + bar / 2)}" y="{H - 30}" text-anchor="middle">{nice} nm</text></g>')
    out.append("</g></svg>")
    return Markup("".join(out))


def forecast_chart(series, today, label, uid="fc", W=560, H=230):
    """Landings left in the rolling 90-day window for each coming day, as a stepped area:
    green while you're current, red once you'd drop below 3, with the lapse date marked."""
    import re as _re
    uid = _re.sub(r"[^a-z0-9]", "", uid.lower()) or "fc"
    L, R, T, B = 34, 18, 30, 30
    pw, ph = W - L - R, H - T - B
    top = max(series + [3])
    ymax = next(n for n in (5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150, 200, 10 ** 6) if n >= top * 1.08)
    n = len(series)
    step = pw / (n - 1)
    X = lambda i: L + i * step  # noqa: E731
    Y = lambda v: T + ph - ph * v / ymax  # noqa: E731
    lapse = next((i for i, v in enumerate(series) if v < 3), None)

    # stepped outline: hold each day's value until the next day
    pts = [(X(0), Y(series[0]))]
    for i in range(1, n):
        if series[i] != series[i - 1]:
            pts.append((X(i), Y(series[i - 1])))
            pts.append((X(i), Y(series[i])))
    pts.append((X(n - 1), Y(series[-1])))
    line = "M" + " L".join(f"{_x(x)},{_x(y)}" for x, y in pts)
    area = line + f" L{_x(X(n - 1))},{_x(T + ph)} L{_x(L)},{_x(T + ph)} Z"
    split = X(lapse) if lapse is not None else W

    o = [f'<svg class="chart forecast" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">',
         f'<defs><linearGradient id="g-ok-{uid}" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0" class="stop-ok" stop-color="#12744d" stop-opacity=".42"/>'
         f'<stop offset="1" class="stop-ok" stop-color="#12744d" stop-opacity=".02"/></linearGradient>'
         f'<linearGradient id="g-bad-{uid}" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0" class="stop-bad" stop-color="#b3261e" stop-opacity=".35"/>'
         f'<stop offset="1" class="stop-bad" stop-color="#b3261e" stop-opacity=".02"/></linearGradient>'
         f'<clipPath id="c-ok-{uid}"><rect x="0" y="0" width="{_x(split)}" height="{H}"/></clipPath>'
         f'<clipPath id="c-bad-{uid}"><rect x="{_x(split)}" y="0" width="{_x(max(W - split, 0))}" height="{H}"/></clipPath></defs>']
    # recessive grid: baseline and top
    for v in (0, ymax):
        o.append(f'<line class="grid" stroke="#d6dfea" x1="{L}" x2="{W - R}" y1="{_x(Y(v))}" y2="{_x(Y(v))}"/>'
                 f'<text class="axis" fill="#526579" x="{L - 8}" y="{_x(Y(v) + 4)}" text-anchor="end">{v}</text>')
    # month ticks
    for i in range(n):
        d = today + timedelta(days=i)
        if d.day == 1:
            o.append(f'<line class="tick" stroke="#d6dfea" x1="{_x(X(i))}" x2="{_x(X(i))}" y1="{T + ph}" y2="{T + ph + 5}"/>'
                     f'<text class="axis" fill="#526579" x="{_x(X(i))}" y="{H - 10}" text-anchor="middle">{MON[d.month - 1]}</text>')
    o.append(f'<text class="axis" fill="#526579" x="{L}" y="{H - 10}">Today</text>')
    # the area and line, green then red
    for kind in ("ok", "bad"):
        o.append(f'<g clip-path="url(#c-{kind}-{uid})"><path fill="url(#g-{kind}-{uid})" d="{area}"/>'
                 f'<path class="fc-line {kind}" fill="none" stroke="{"#12744d" if kind == "ok" else "#b3261e"}" '
                 f'stroke-width="2.5" stroke-linejoin="round" d="{line}"/></g>')
    # the 3-landing line
    o.append(f'<line class="fc-need" stroke="#915200" stroke-dasharray="5 4" x1="{L}" x2="{W - R}" y1="{_x(Y(3))}" y2="{_x(Y(3))}"/>'
             f'<text class="fc-need-label" fill="#915200" x="{W - R}" y="{_x(Y(3) - 6)}" text-anchor="end">3 needed</text>')
    # lapse marker
    if lapse is not None and lapse > 0:
        d = today + timedelta(days=lapse)
        x = X(lapse)
        txt = "Lapses " + d.strftime("%b %-d")
        tw = 7.2 * len(txt) + 16
        bx = min(max(x - tw / 2, L), W - R - tw)
        o.append(f'<line class="fc-lapse" stroke="#b3261e" stroke-dasharray="3 3" x1="{_x(x)}" x2="{_x(x)}" y1="{T - 4}" y2="{T + ph}"/>'
                 f'<rect class="fc-pill" fill="#b3261e" x="{_x(bx)}" y="{T - 26}" width="{_x(tw)}" height="20" rx="10"/>'
                 f'<text class="fc-pill-text" fill="#fff" x="{_x(bx + tw / 2)}" y="{T - 12}" text-anchor="middle">{txt}</text>')
    # today's value
    y0 = Y(series[0])
    o.append(f'<circle class="fc-dot {"ok" if series[0] >= 3 else "bad"}" fill="{"#12744d" if series[0] >= 3 else "#b3261e"}" '
             f'cx="{L}" cy="{_x(y0)}" r="5"/>'
             f'<text class="fc-now" fill="#0d1b2d" x="{L + 10}" y="{_x(max(y0 - 8, 14))}">{series[0]} now</text>')
    # hover: one invisible strip per day
    for i, v in enumerate(series):
        d = today + timedelta(days=i)
        o.append(f'<rect class="hit" fill="transparent" x="{_x(X(i) - step / 2)}" y="{T}" width="{_x(step)}" height="{ph}">'
                 f'<title>{d.strftime("%a %b %-d")}: {v} landing{"s" if v != 1 else ""} in the last 90 days'
                 f'{"" if v >= 3 else " (not current)"}</title></rect>')
    o.append("</svg>")
    return Markup("".join(o))


def cost_bars(months, W=640, H=232):
    L, R, T, B = 52, 8, 14, 40
    ymax = _nice(max((m["total"] for m in months), default=1) * 1.05)
    pw, ph = W - L - R, H - T - B
    slot = pw / len(months)
    bw = slot * 0.58
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Spending per month over the last 12 months">']
    for i in range(3):
        val = ymax * i / 2
        y = T + ph - ph * val / ymax
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{_x(y)}" y2="{_x(y)}"/>'
                   f'<text class="axis" x="{L - 8}" y="{_x(y + 4)}" text-anchor="end">${val:,.0f}</text>')
    for i, m in enumerate(months):
        x = L + i * slot + (slot - bw) / 2
        base = T + ph
        tip = (f'{MON[m["month"] - 1]} {m["year"]}: ${m["total"]:,.0f} (aircraft ${m["aircraft"]:,.0f}, '
               f'instructor ${m["instructor"]:,.0f}, fuel ${m["fuel"]:,.0f}, other ${m["other"]:,.0f})')
        out.append(f'<g class="bar"><title>{escape(tip)}</title>'
                   f'<rect class="hit" x="{_x(L + i * slot)}" y="{T}" width="{_x(slot)}" height="{ph}"/>')
        if m["total"] <= 0:
            out.append(f'<rect class="c-empty" x="{_x(x)}" y="{_x(base - 2)}" width="{_x(bw)}" height="2"/>')
        y = base
        for cls, key in (("c-dual", "aircraft"), ("c-solo", "instructor"), ("c-fuel", "fuel"), ("c-other", "other")):
            h = ph * m[key] / ymax
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
