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
           f'<g clip-path="url(#mapclip)"><rect class="map-bg" width="{W}" height="{H}"/>']
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
            out.append(f'<polyline class="{cls}" points="{pts}"/>')
    # legs
    for (a1, a2), n in sorted(stats["legs"].items(), key=lambda x: x[1]):
        p1, p2 = by_icao[a1], by_icao[a2]
        x1, y1 = xy(p1["lat"], p1["lon"])
        x2, y2 = xy(p2["lat"], p2["lon"])
        w = 1.6 + min(n, 8) * 0.55
        out.append(f'<line class="map-leg" x1="{_x(x1)}" y1="{_x(y1)}" x2="{_x(x2)}" y2="{_x(y2)}" stroke-width="{_x(w)}">'
                   f'<title>{escape(p1["lid"] or a1)} – {escape(p2["lid"] or a2)}: {n} time{"s" if n != 1 else ""}</title></line>')
    # airports, most visited drawn last so they sit on top
    top = aps[0]["ap"]["icao"]
    for a in sorted(aps, key=lambda a: a["visits"]):
        x, y = xy(a["ap"]["lat"], a["ap"]["lon"])
        r = 4.5 + min(math.sqrt(a["visits"]) * 1.6, 9)
        cls = "map-apt home" if a["ap"]["icao"] == top else "map-apt"
        tip = f'{a["code"]} {a["ap"]["name"]}: {a["visits"]} flight{"s" if a["visits"] != 1 else ""}, {a["landings"]} landings'
        out.append(f'<g class="{cls}"><title>{escape(tip)}</title><circle cx="{_x(x)}" cy="{_x(y)}" r="{_x(r)}"/>'
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


def forecast_bars(series, today, label, W=640, H=170):
    """Landings in the rolling 90-day window for each coming day, green while you're current."""
    L, R, T, B = 30, 8, 12, 30
    ymax = max(4, max(series) + 1)
    pw, ph = W - L - R, H - T - B
    slot = pw / len(series)
    out = [f'<svg class="chart forecast" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">']
    for v in sorted({0, 3, ymax}):
        y = T + ph - ph * v / ymax
        cls = "grid need" if v == 3 else "grid"
        out.append(f'<line class="{cls}" x1="{L}" x2="{W - R}" y1="{_x(y)}" y2="{_x(y)}"/>'
                   f'<text class="axis" x="{L - 6}" y="{_x(y + 4)}" text-anchor="end">{v}</text>')
    for i, v in enumerate(series):
        d = today + timedelta(days=i)
        h = ph * v / ymax
        x = L + i * slot
        cls = "fc-ok" if v >= 3 else "fc-bad"
        tip = f'{d.strftime("%b %-d")}: {v} landing{"s" if v != 1 else ""} in the last 90 days'
        if h > 0:
            out.append(f'<rect class="{cls}" x="{_x(x)}" y="{_x(T + ph - h)}" width="{_x(max(slot - .4, .6))}" height="{_x(h)}">'
                       f'<title>{tip}</title></rect>')
        if d.day == 1 or i == 0:
            out.append(f'<text class="axis" x="{_x(x)}" y="{H - 10}">{"Today" if i == 0 else MON[d.month - 1]}</text>')
    out.append("</svg>")
    return Markup("".join(out))


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
