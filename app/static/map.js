/* Interactive route map for the Airports page. The only script in the app; it runs Leaflet (bundled)
   over OpenStreetMap or OpenTopoMap tiles. If it can't run, the page keeps the offline map. */
(function () {
  "use strict";
  var el = document.getElementById("route-map");
  var src = document.getElementById("map-data");
  if (!el || !src || !window.L) return;
  var d = JSON.parse(src.textContent || "null");
  if (!d || !d.airports || !d.airports.length) return;
  document.getElementById("map-wrap").classList.add("live");

  var css = getComputedStyle(document.documentElement);
  function v(name, fallback) { return (css.getPropertyValue(name) || "").trim() || fallback; }
  var MAGENTA = v("--magenta", "#b2227c"), CYAN = v("--cyan", "#0a7ea6"), HOME = "#F26A21",
      PANEL = v("--panel", "#ffffff"), SOFT = v("--neutral", "#9db0c4");

  var map = L.map(el, { zoomSnap: 0.5, worldCopyJump: true });
  var streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, className: "tiles-streets",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noreferrer">OpenStreetMap</a> contributors'
  }).addTo(map);
  var terrain = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 17, subdomains: "abc",
    attribution: 'Map data &copy; <a href="https://www.openstreetmap.org/copyright" rel="noreferrer">OpenStreetMap</a> contributors, SRTM | ' +
      'Style &copy; <a href="https://opentopomap.org" rel="noreferrer">OpenTopoMap</a> (CC-BY-SA)'
  });

  function el2(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function fmtDate(s) {
    var p = s.split("-"), m = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return m[+p[1] - 1] + " " + (+p[2]) + ", " + p[0];
  }

  // legs, drawn under the airports
  var legs = L.layerGroup().addTo(map);
  d.legs.sort(function (a, b) { return a.n - b.n; }).forEach(function (g) {
    L.polyline([g.a, g.b], { color: MAGENTA, weight: 2 + Math.min(g.n, 8) * 0.7, opacity: 0.85, lineCap: "round" })
      .bindTooltip(g.label + ": " + g.n + (g.n === 1 ? " flight" : " flights") + ", " + g.nm + " nm", { sticky: true })
      .addTo(legs);
  });

  // other airports nearby, off until switched on
  var others = L.layerGroup();
  d.others.forEach(function (a) {
    L.circleMarker([a.lat, a.lon], { radius: 3.5, color: SOFT, weight: 1, fillColor: SOFT, fillOpacity: 0.7 })
      .bindTooltip(a.code + " " + a.name).addTo(others);
  });

  // your airports
  var mine = L.layerGroup().addTo(map), home = null, pts = [];
  d.airports.slice().sort(function (a, b) { return a.visits - b.visits; }).forEach(function (a) {
    var ll = [a.lat, a.lon];
    pts.push(ll);
    if (a.home) home = ll;
    var m = L.circleMarker(ll, {
      radius: 6 + Math.min(Math.sqrt(a.visits) * 1.8, 10), color: PANEL, weight: 2,
      fillColor: a.home ? HOME : CYAN, fillOpacity: 1
    });
    m.bindTooltip(a.code, { permanent: true, direction: "right", offset: [8, 0], className: "apt-label" });
    var box = el2("div", "apt-pop");
    box.appendChild(el2("b", null, a.code));
    box.appendChild(el2("div", "apt-name", a.name));
    if (a.place) box.appendChild(el2("div", "apt-place", a.place));
    box.appendChild(el2("div", "apt-stats", a.visits + (a.visits === 1 ? " flight" : " flights") + " · " +
      a.landings + (a.landings === 1 ? " landing" : " landings")));
    box.appendChild(el2("div", "apt-dates", a.first === a.last ? "Visited " + fmtDate(a.first)
      : "First " + fmtDate(a.first) + " · last " + fmtDate(a.last)));
    m.bindPopup(box);
    m.addTo(mine);
  });

  // distance rings around the home field
  var rings = L.layerGroup();
  if (home) {
    [25, 50, 100].forEach(function (nm) {
      L.circle(home, { radius: nm * 1852, color: HOME, weight: 1.5, dashArray: "6 6", fill: false, interactive: false }).addTo(rings);
      var edge = L.latLng(home[0] + nm / 60, home[1]);
      L.marker(edge, { interactive: false, icon: L.divIcon({ className: "ring-label", html: "", iconSize: null }) })
        .bindTooltip(nm + " nm", { permanent: true, direction: "center", className: "ring-tip" }).addTo(rings);
    });
  }

  L.control.layers({ "Streets": streets, "Terrain": terrain },
    { "Routes": legs, "Your airports": mine, "Other airports": others, "Range rings (25/50/100 nm)": rings },
    { collapsed: true }).addTo(map);
  // scale bar in nautical miles
  var NmScale = L.Control.Scale.extend({
    _updateImperial: function (maxMeters) {
      var nm = maxMeters / 1852, n = this._getRoundNum(nm);
      this._updateScale(this._iScale, n + " nm", n / nm);
    }
  });
  new NmScale({ imperial: true, metric: false }).addTo(map);

  var bounds = L.latLngBounds(pts);
  if (pts.length === 1) map.setView(pts[0], 10);
  else map.fitBounds(bounds.pad(0.15));

  // a "fit all" button to get back after exploring
  var Fit = L.Control.extend({
    options: { position: "topleft" },
    onAdd: function () {
      var b = el2("button", "map-fit", "Fit");
      b.type = "button";
      b.title = "Show all your airports";
      L.DomEvent.disableClickPropagation(b);
      L.DomEvent.on(b, "click", function () {
        if (pts.length === 1) map.setView(pts[0], 10); else map.fitBounds(bounds.pad(0.15));
      });
      return b;
    }
  });
  new Fit().addTo(map);
})();
