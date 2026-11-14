#!/usr/bin/env python3
"""
fetch_appeears.py - MODIS data retrieval for Earth Trend Detective.

Source: NASA AppEEARS (https://appeears.earthdatacloud.nasa.gov/), NASA LP DAAC. Uses the documented AppEEARS API.

Three requests ("tasks") are used:
  etd_ndvi_points          point samples, MOD13A3.061 1 km monthly NDVI, 2000-02 .. 2025-12, every site in sites.json
  etd_ndvi_july_area       area samples, MOD13A3.061 1 km monthly NDVI, July of 2001..2025, 1 deg box per featured place
  etd_truecolor_july_area  area samples, MOD09A1.061 bands 1/3/4 (500 m, 8-day), July of six years, same boxes

Steps
  submit    create the three tasks (AppEEARS queues them; they take from minutes to hours)
  status    show their state
  build     download finished results and write
              data/ndvi.js                         annual NDVI per site (a year needs all 12 monthly composites)
              data/imagery.js + data/imagery/*     PNG images per featured place and year

Processing rules (no value is invented)
  * NDVI outside [-0.2, 1] (including the fill value) is missing.
  * NDVI images: fixed colour palette applied to the NDVI value; missing pixels are grey.
  * True colour: for each pixel, the July 8-day composite with the lowest blue reflectance (the least cloudy one) is
    chosen and all three bands are taken from it; composites with a fill or out-of-range value are skipped. Then the
    same fixed brightening for every image: 255 * (reflectance / 0.4) ** 0.5, clipped. Missing pixels are grey.

Credentials: EARTHDATA_USERNAME / EARTHDATA_PASSWORD environment variables (NASA Earthdata Login). Never stored.

Usage
  EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=... python3 tools/fetch_appeears.py submit --sites tools/sites.json
  ... python3 tools/fetch_appeears.py status
  ... python3 tools/fetch_appeears.py build --sites tools/sites.json --out data --cache /path/to/cache
Needs: numpy, pillow, tifffile, imagecodecs (pip install numpy pillow tifffile imagecodecs).
"""
import argparse, base64, concurrent.futures as cf, csv, datetime as dt, json, os, re, sys, time, urllib.request

API = "https://appeears.earthdatacloud.nasa.gov/api"
HALF = 0.5                      # half-size of the image box in degrees
TC_YEARS = (2001, 2005, 2010, 2015, 2020, 2025)
NDVI_YEARS = (2001, 2025)
Y0, Y1 = 2000, 2025
NDVI_PALETTE = [[0.0, "#B08D5B"], [0.15, "#D9C58B"], [0.3, "#C8D77A"], [0.5, "#7DB54A"], [0.7, "#3C8A2E"], [0.9, "#14501B"]]
GREY = (201, 211, 214)
NAMES = ("etd_ndvi_points", "etd_ndvi_july_area", "etd_truecolor_july_area")


def call(path, tok=None, data=None, method=None, raw=False):
    h = {"Authorization": "Bearer " + tok} if tok else {}
    if data is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    r = urllib.request.urlopen(urllib.request.Request(API + path, data=data, headers=h, method=method), timeout=300)
    return r.read() if raw else json.load(r)


def login():
    u, p = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    if not u or not p:
        sys.exit("Set EARTHDATA_USERNAME and EARTHDATA_PASSWORD (NASA Earthdata Login).")
    req = urllib.request.Request(API + "/login", data=b"", method="POST",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()})
    return json.load(urllib.request.urlopen(req, timeout=120))["token"]


def safe(s):
    return re.sub(r"[^A-Za-z0-9]", "_", s)


def box(s):
    a, o = s["lat"], s["lon"]
    return [round(a - HALF, 4), round(o - HALF, 4), round(a + HALF, 4), round(o + HALF, 4)]  # S, W, N, E


def submit(a):
    tok, sites = login(), json.load(open(a.sites))
    feat = [s for s in sites if s["id"].startswith("f_")]
    def feature(s):
        S, W, N, E = box(s)
        return {"type": "Feature", "properties": {"name": s["id"]}, "geometry": {"type": "Polygon", "coordinates": [[[W, S], [E, S], [E, N], [W, N], [W, S]]]}}
    geo = {"type": "FeatureCollection", "features": [feature(s) for s in feat]}
    out = {"format": {"type": "geotiff"}, "projection": "geographic"}
    tasks = [
        {"task_type": "point", "task_name": NAMES[0], "params": {"dates": [{"startDate": "02-01-2000", "endDate": "12-31-2025"}],
            "layers": [{"product": "MOD13A3.061", "layer": "_1_km_monthly_NDVI"}],
            "coordinates": [{"id": str(i), "category": s["id"], "latitude": s["lat"], "longitude": s["lon"]} for i, s in enumerate(sites)]}},
        {"task_type": "area", "task_name": NAMES[1], "params": {"dates": [{"startDate": "07-01", "endDate": "07-31", "recurring": True, "yearRange": list(NDVI_YEARS)}],
            "layers": [{"product": "MOD13A3.061", "layer": "_1_km_monthly_NDVI"}], "output": out, "geo": geo}},
        {"task_type": "area", "task_name": NAMES[2], "params": {"dates": [{"startDate": f"07-01-{y}", "endDate": f"07-31-{y}"} for y in TC_YEARS],
            "layers": [{"product": "MOD09A1.061", "layer": l} for l in ("sur_refl_b01", "sur_refl_b03", "sur_refl_b04")], "output": out, "geo": geo}},
    ]
    for t in tasks:
        print(t["task_name"], call("/task", tok, t))


def latest(tok):
    """Most recent task of each name."""
    found = {}
    for t in call("/task", tok):
        if t["task_name"] in NAMES and t["task_name"] not in found:
            found[t["task_name"]] = t
    return found


def status(a):
    for n, t in latest(login()).items():
        print(n, t["status"], t["task_id"], t.get("completed"))


def bundle(tok, task, cache, want):
    """Download the files of a finished task whose name matches `want`; returns local paths."""
    d = os.path.join(cache, task["task_id"])
    os.makedirs(d, exist_ok=True)
    files = [f for f in call(f"/bundle/{task['task_id']}", tok)["files"] if want(os.path.basename(f["file_name"]))]

    def get(f):
        p = os.path.join(d, os.path.basename(f["file_name"]))
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            for attempt in range(4):
                try:
                    blob = call(f"/bundle/{task['task_id']}/{f['file_id']}", tok, raw=True)
                    with open(p + ".tmp", "wb") as fh:
                        fh.write(blob)
                    os.replace(p + ".tmp", p)
                    break
                except Exception:
                    time.sleep(3 + 5 * attempt)
        return p if os.path.exists(p) else None

    with cf.ThreadPoolExecutor(8) as ex:
        return [p for p in ex.map(get, files) if p]


def build(a):
    import numpy as np
    from PIL import Image
    import tifffile
    tok, sites = login(), json.load(open(a.sites))
    feat = [s for s in sites if s["id"].startswith("f_")]
    tasks = latest(tok)
    today = dt.date.today().isoformat()
    os.makedirs(a.out, exist_ok=True)

    # ---- NDVI point series -----------------------------------------------------------------
    t = tasks.get(NAMES[0])
    if t and t["status"] == "done":
        (p,) = bundle(tok, t, a.cache, lambda n: n.endswith("results.csv"))
        monthly = {}
        for row in csv.DictReader(open(p)):
            col = next(k for k in row if k.endswith("_1_km_monthly_NDVI"))
            try:
                v = float(row[col])
            except ValueError:
                continue
            if -0.2 <= v <= 1.0:
                y, m = int(row["Date"][:4]), int(row["Date"][5:7])
                monthly.setdefault(row["Category"], {})[(y, m)] = v
        out = {}
        for s in sites:
            mo = monthly.get(s["id"], {})
            vals = []
            for y in range(Y0, Y1 + 1):
                ms = [mo.get((y, m)) for m in range(1, 13)]
                vals.append(None if any(v is None for v in ms) else round(sum(ms) / 12, 4))
            i = next((k for k, v in enumerate(vals) if v is not None), None)
            if i is not None:
                j = max(k for k, v in enumerate(vals) if v is not None)
                out[s["id"]] = {"start": Y0 + i, "vals": vals[i:j + 1]}
                k0, k1 = min(mo), max(mo)
                i0, i1 = k0[0] * 12 + k0[1] - 1, k1[0] * 12 + k1[1] - 1
                out[s["id"]]["monthly"] = {"start": i0, "vals": [None if mo.get((i // 12, i % 12 + 1)) is None else round(mo[(i // 12, i % 12 + 1)], 4) for i in range(i0, i1 + 1)]}
            # append NDVI to the monthly audit file of the site
            mp = os.path.join(a.out, "monthly", safe(s["id"]) + "_ndvi.csv")
            os.makedirs(os.path.dirname(mp), exist_ok=True)
            with open(mp, "w") as fh:
                fh.write(f"# {s['name']} ({s['lat']}, {s['lon']}). MOD13A3.061 1 km monthly NDVI, point sample from NASA AppEEARS. Missing months omitted.\nyear,month,ndvi\n")
                for (y, m) in sorted(mo):
                    fh.write(f"{y},{m},{mo[(y, m)]:.4f}\n")
        with open(os.path.join(a.out, "ndvi.js"), "w") as fh:
            fh.write("/* Generated by tools/fetch_appeears.py from NASA AppEEARS (MOD13A3.061). Do not edit by hand. */\n")
            fh.write("window.ETD_NDVI=" + json.dumps({"retrieved": (t.get("completed") or today)[:10], "task_id": t["task_id"], "product": "MOD13A3.061", "layer": "_1_km_monthly_NDVI", "sites": out}, separators=(",", ":")) + ";\n")
        print("ndvi.js:", len(out), "sites with at least one complete year")
    else:
        print("NDVI point task not finished:", t and t["status"])

    # ---- images ----------------------------------------------------------------------------
    man = {"retrieved": today, "half_box_deg": HALF, "layers": {}, "sites": {s["id"]: {"bbox": box(s)} for s in feat}}
    pal_v = np.array([p[0] for p in NDVI_PALETTE])
    pal_c = np.array([[int(p[1][i:i + 2], 16) for i in (1, 3, 5)] for p in NDVI_PALETTE], float)

    def parse(name):  # ..._doyYYYYDDDhhmmss_aid0001.tif -> (year, doy, feature index)
        m = re.search(r"_doy(\d{4})(\d{3})\d*_aid(\d+)\.tif$", name)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)) - 1) if m else None

    def save(img, sid, key, year):
        d = os.path.join(a.out, "imagery", safe(sid))
        os.makedirs(d, exist_ok=True)
        Image.fromarray(img.astype("uint8")).save(os.path.join(d, f"{key}_{year}.png"), optimize=True)

    t = tasks.get(NAMES[1])
    if t and t["status"] == "done":
        years = set()
        for p in bundle(tok, t, a.cache, lambda n: n.endswith(".tif") and "_NDVI_" in n):
            k = parse(os.path.basename(p))
            if not k or k[2] >= len(feat):
                continue
            v = tifffile.imread(p).astype(float) * 0.0001
            ok = (v >= -0.2) & (v <= 1.0)
            if ok.mean() < 0.05:
                continue  # nothing usable in this composite (e.g. open water only): no image rather than a blank one
            x = np.clip(v, pal_v[0], pal_v[-1])
            rgb = np.stack([np.interp(x, pal_v, pal_c[:, c]) for c in range(3)], -1)
            rgb[~ok] = GREY
            save(rgb, feat[k[2]]["id"], "ndvi", k[0])
            man["sites"][feat[k[2]]["id"]].setdefault("ndvi", []).append(k[0])
            years.add(k[0])
        if years:
            man["layers"]["ndvi"] = {"product": "MOD13A3.061", "layer": "_1_km_monthly_NDVI", "task_id": t["task_id"], "years": sorted(years), "palette": NDVI_PALETTE}
        print("NDVI images:", len(years), "years")
    else:
        print("NDVI area task not finished:", t and t["status"])

    t = tasks.get(NAMES[2])
    if t and t["status"] == "done":
        stack = {}
        for p in bundle(tok, t, a.cache, lambda n: n.endswith(".tif") and "sur_refl_b0" in n):
            k = parse(os.path.basename(p))
            band = re.search(r"sur_refl_(b0\d)", p).group(1)
            if not k or k[2] >= len(feat):
                continue
            v = tifffile.imread(p).astype(float) * 0.0001
            v[(v < -0.01) | (v > 1.6)] = np.nan      # outside the product's valid range (includes the fill value)
            stack.setdefault((k[2], k[0]), {}).setdefault(band, {})[k[1]] = np.clip(v, 0, None)
        years = set()
        for (fi, year), bands in sorted(stack.items()):
            if not all(b in bands for b in ("b01", "b04", "b03")):
                continue
            days = sorted(set(bands["b01"]) & set(bands["b04"]) & set(bands["b03"]))
            if not days:
                continue
            cube = np.stack([np.stack([bands[b][d] for b in ("b01", "b04", "b03")], -1) for d in days])  # composite, y, x, rgb
            blue = np.where(np.isnan(cube).any(-1), np.inf, cube[..., 2])
            pick = blue.argmin(0)                      # per pixel: the composite with the lowest blue reflectance
            rgb = np.take_along_axis(cube, pick[None, ..., None], 0)[0]
            bad = np.isinf(blue.min(0))
            if bad.mean() > 0.95:
                continue
            rgb = 255 * np.clip(np.nan_to_num(rgb) / 0.4, 0, 1) ** 0.5
            rgb[bad] = GREY
            save(rgb, feat[fi]["id"], "truecolor", year)
            man["sites"][feat[fi]["id"]].setdefault("truecolor", []).append(year)
            years.add(year)
        if years:
            man["layers"]["truecolor"] = {"product": "MOD09A1.061", "layers": ["sur_refl_b01", "sur_refl_b04", "sur_refl_b03"], "task_id": t["task_id"], "years": sorted(years)}
        print("true-colour images:", len(years), "years")
    else:
        print("true-colour area task not finished:", t and t["status"])

    done_dates = [tasks[n]["completed"][:10] for n in NAMES[1:] if tasks.get(n) and tasks[n].get("completed")]
    if done_dates:
        man["retrieved"] = max(done_dates)  # when AppEEARS finished the image requests, not when this build ran
    for s in man["sites"].values():
        for k in ("ndvi", "truecolor"):
            if k in s:
                s[k] = sorted(set(s[k]))
    with open(os.path.join(a.out, "imagery.js"), "w") as fh:
        fh.write("/* Generated by tools/fetch_appeears.py from NASA AppEEARS (MOD13A3.061, MOD09A1.061). Do not edit by hand. */\n")
        fh.write("window.ETD_IMAGERY=" + (json.dumps(man, separators=(",", ":")) if man["layers"] else "null") + ";\n")
    print("imagery.js written; layers:", list(man["layers"]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit"); s.add_argument("--sites", required=True); s.set_defaults(f=submit)
    s = sub.add_parser("status"); s.set_defaults(f=status)
    s = sub.add_parser("build"); s.add_argument("--sites", required=True); s.add_argument("--out", required=True); s.add_argument("--cache", required=True); s.set_defaults(f=build)
    a = ap.parse_args()
    a.f(a)
