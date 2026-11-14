#!/usr/bin/env python3
"""
fetch_giovanni_maps.py - whole-Earth maps for Earth Trend Detective from NASA Giovanni (https://giovanni.gsfc.nasa.gov/giovanni/).

Giovanni's time-series API only serves points, so these maps use Giovanni's own "Time Averaged Map" service, the one the
Giovanni website runs when you choose "Time Averaged Map". For each layer and year the script:
  1. logs in to Giovanni with a NASA Earthdata Login (EARTHDATA_USERNAME / EARTHDATA_PASSWORD; never stored),
  2. asks Giovanni for the time-averaged map of that calendar year over the whole Earth (bbox -180,-90,180,90),
  3. downloads the netCDF file Giovanni produces (kept in the cache folder),
  4. converts units, averages to a coarser grid where needed, and draws a PNG with a fixed colour scale shared by all
     years of that layer (missing values are transparent). Nothing is interpolated or filled.
It writes data/global/<layer>_<year>.png and data/global.js (layer list, years, colour scales, units, provenance).

Usage
  EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=... python3 tools/fetch_giovanni_maps.py --out data --cache /path/to/cache [--layers prec,no2,rh,modis]
  python3 tools/fetch_giovanni_maps.py --probe VARIABLE_ID     # check that Giovanni can map a variable (one 2020 job)
Needs: numpy, pillow, netCDF4.
"""
import argparse, atexit, concurrent.futures as cf, datetime as dt, json, os, re, subprocess, sys, tempfile, threading, time, urllib.parse

G = "https://giovanni.gsfc.nasa.gov/giovanni"
CLIENT = "e2WVk8Pw6weeLUKZYOxvTQ"   # the Giovanni application in Earthdata Login (approve it once on your account)

# layer key: Giovanni variable, years, unit conversion, output unit, colour scale (value, colour), averaging block
LAYERS = {
    "prec": dict(gid="GPM_3IMERGM_07_precipitation", years=(1998, 2024), conv=lambda a: a * 24 * 365.25, unit="mm/yr", block=5,
                 name="GPM IMERG precipitation", dataset="GPM IMERG Final Precipitation L3 monthly 0.1° (GPM_3IMERGM v07)",
                 scale=[[0, "#F6F1E4"], [250, "#D9E9D2"], [500, "#9FD3C7"], [1000, "#4FA3C7"], [2000, "#2563A6"], [3000, "#3B2A88"], [4500, "#1E0F4F"]]),
    "no2": dict(gid="OMNO2d_003_ColumnAmountNO2TropCloudScreened", years=(2005, 2025), conv=lambda a: a / 1e15, unit="10¹⁵ molec/cm²", block=1,
                name="OMI tropospheric NO₂", dataset="OMI/Aura NO₂ Cloud-Screened Total and Tropospheric Column L3 daily 0.25° (OMNO2d v003)",
                scale=[[0, "#F4F1E8"], [1, "#F3E6B3"], [2, "#F2C46D"], [4, "#E8873F"], [8, "#BF4526"], [16, "#6E1423"]]),
    "rh": dict(gid="AIRS3STM_7_0_RelHumSurf_A", years=(2003, 2025), conv=lambda a: a, unit="%", block=1,
               name="AIRS surface relative humidity", dataset="AIRS/Aqua L3 Monthly Standard Physical Retrieval 1° (AIRS3STM v7.0)",
               scale=[[0, "#8C5A2B"], [25, "#C9A26B"], [45, "#E9DDBF"], [65, "#9ED3CB"], [80, "#2A8C86"], [100, "#0E4D57"]]),
    # MODIS: Giovanni has no MODIS vegetation (NDVI) data it can map (MOD13C2 is listed without files), so the
    # whole-Earth MODIS layer is aerosol optical depth from MODIS Terra.
    "aod": dict(gid="MOD08_M3_6_1_AOD_550_Dark_Target_Deep_Blue_Combined_Mean_Mean", years=(2001, 2025), conv=lambda a: a, unit="AOD at 550 nm (no unit)", block=1,
                name="MODIS aerosol optical depth", dataset="MODIS/Terra Aerosol Cloud Water Vapor Ozone Monthly L3 Global 1° (MOD08_M3 v6.1), Dark Target + Deep Blue combined",
                scale=[[0, "#F5F3EA"], [0.1, "#F3E7B8"], [0.2, "#EBC77A"], [0.35, "#D99A4E"], [0.5, "#B8642E"], [0.8, "#7A3416"], [1.2, "#3E1A0B"]]),
}
_tok = threading.local()
NETRC = [None]


def login():
    """Earthdata Login OAuth for the Giovanni website (same steps as a browser); returns a cookie-jar path."""
    user, pw = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    if not user or not pw:
        sys.exit("Set EARTHDATA_USERNAME and EARTHDATA_PASSWORD (NASA Earthdata Login).")
    jar = tempfile.NamedTemporaryFile(delete=False, suffix=".jar").name
    # a private netrc file for this run only (result files need a second Earthdata redirect); removed at exit
    fd, netrc = tempfile.mkstemp(suffix=".netrc")
    os.chmod(netrc, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"machine urs.earthdata.nasa.gov login {user} password {pw}\n")
    atexit.register(lambda: [os.path.exists(p) and os.remove(p) for p in (netrc, jar)])
    NETRC[0] = netrc
    final = subprocess.run(["curl", "-sS", "-m", "120", "-L", "--netrc-file", netrc, "-c", jar, "-b", jar, "-o", "/dev/null", "-w", "%{url_effective}",
                            f"https://urs.earthdata.nasa.gov/oauth/authorize?response_type=code&client_id={CLIENT}&redirect_uri={G}/giovanni-login-redirect"],
                           capture_output=True, text=True).stdout
    m = re.search(r"[?&]code=([A-Za-z0-9_-]+)", final)
    if not m:
        sys.exit("Earthdata login for Giovanni failed (approve the Giovanni application on your Earthdata profile): " + final[:200])
    prof = subprocess.run(["curl", "-sS", "-m", "60", "-c", jar, "-b", jar, f"{G}/daac-bin/earthdataLogin.pl?code={m.group(1)}"], capture_output=True, text=True).stdout
    if '"uid"' not in prof:
        sys.exit("Giovanni did not accept the login: " + prof[:200])
    return jar


def get(jar, url, out=None, timeout=300):
    args = ["curl", "-sS", "-m", str(timeout), "-c", jar, "-b", jar, url]
    if out:   # result files redirect through Earthdata Login once per session: follow it with the run's netrc
        args[1:1] = ["-L", "--netrc-file", NETRC[0], "-o", out, "-w", "%{http_code}"]
        return subprocess.run(args, capture_output=True, text=True).stdout
    return subprocess.run(args, capture_output=True, text=True).stdout


def map_job(jar, gid, y0, y1):
    """Runs one Time Averaged Map job; returns the netCDF URL or raises with Giovanni's message."""
    q = urllib.parse.urlencode({"service": "TmAvMp", "starttime": f"{y0}-01-01T00:00:00Z", "endtime": f"{y1}-12-31T23:59:59Z",
                                "bbox": "-180,-90,180,90", "data": gid, "portal": "GIOVANNI", "format": "json"})
    d = json.loads(get(jar, f"{G}/daac-bin/service_manager.pl?{q}"))["session"]
    ids = (d["id"], d["resultset"][0]["id"], d["resultset"][0]["result"][0]["id"])
    for _ in range(400):
        time.sleep(4)
        r = json.loads(get(jar, f"{G}/daac-bin/service_manager.pl?session={ids[0]}&resultset={ids[1]}&result={ids[2]}&portal=GIOVANNI&format=json"))
        r = r["session"]["resultset"][0]["result"][0]
        st = r["status"][0]
        code, pct, msg = st["code"][0]["value"], float(st["percentComplete"][0]["value"]), re.sub("<[^>]+>", " ", st["message"][0]["value"])
        if code not in ("0", "1") or "failed" in msg.lower():
            raise RuntimeError(msg.strip()[:200])
        if pct >= 100:
            for fg in r.get("data", [{}])[0].get("fileGroup", []):
                for df in fg.get("dataFile", []):
                    for du in df.get("dataUrl", []):
                        if du["value"].endswith(".nc"):
                            return du["value"]
            raise RuntimeError("finished without a data file")
    raise RuntimeError("timed out")


def fetch_year(jar, key, year, cache):
    spec = LAYERS[key]
    path = os.path.join(cache, f"{key}_{year}.nc")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    last = ""
    for attempt in range(3):
        try:
            url = map_job(jar, spec["gid"], year, year)
            code = get(jar, url, out=path + ".tmp", timeout=900)
            if code == "200" and os.path.getsize(path + ".tmp") > 1000:
                os.replace(path + ".tmp", path)
                return path
            last = f"download HTTP {code}"
        except Exception as e:
            last = str(e)
        time.sleep(10 * (attempt + 1))
    print(f"  {key} {year}: FAILED ({last})", flush=True)
    return None


def render(key, path, out_png):
    import numpy as np
    from netCDF4 import Dataset
    from PIL import Image
    spec = LAYERS[key]
    with Dataset(path) as nc:
        var = next(v for n, v in nc.variables.items() if n not in ("lat", "lon", "time", "time_bnds", "lat_bnds", "lon_bnds", "bnds") and v.ndim >= 2)
        a = np.ma.filled(var[:].astype(float), np.nan).squeeze()
        lat = np.array(nc.variables["lat"][:])
        lon = np.array(nc.variables["lon"][:])
    if a.shape == (lon.size, lat.size):
        a = a.T
    if lat[0] < lat[-1]:                       # rows must run north to south
        a, lat = a[::-1], lat[::-1]
    if lon[0] > lon[-1]:
        a, lon = a[:, ::-1], lon[::-1]
    a = spec["conv"](a)
    b = spec["block"]
    if b > 1:                                 # block mean; a block with any missing cell is missing
        h, w = (a.shape[0] // b) * b, (a.shape[1] // b) * b
        a = a[:h, :w].reshape(h // b, b, w // b, b).mean(axis=(1, 3))
    vs = np.array([s[0] for s in spec["scale"]], float)
    cs = np.array([[int(s[1][i:i + 2], 16) for i in (1, 3, 5)] for s in spec["scale"]], float)
    ok = np.isfinite(a)
    x = np.clip(np.where(ok, a, vs[0]), vs[0], vs[-1])
    rgb = np.stack([np.interp(x, vs, cs[:, c]) for c in range(3)], -1)
    rgba = np.concatenate([rgb, np.where(ok, 255, 0)[..., None]], -1).astype("uint8")
    Image.fromarray(rgba, "RGBA").save(out_png, optimize=True)
    return dict(w=int(a.shape[1]), h=int(a.shape[0]), lat0=float(lat[0]), lat1=float(lat[-1]), lon0=float(lon[0]), lon1=float(lon[-1]),
                min=float(np.nanmin(a)) if ok.any() else None, max=float(np.nanmax(a)) if ok.any() else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--cache", default=".cache/giovanni_maps")
    ap.add_argument("--layers", default=",".join(LAYERS))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--probe", default="")
    a = ap.parse_args()
    jar = login()
    if a.probe:
        try:
            y = int(os.environ.get("PROBE_YEAR", "2020"))
            print("OK", map_job(jar, a.probe, y, y))
        except Exception as e:
            print("FAILED", e)
        return
    os.makedirs(a.cache, exist_ok=True)
    os.makedirs(os.path.join(a.out, "global"), exist_ok=True)
    keys = [k for k in a.layers.split(",") if k in LAYERS]
    jobs = [(k, y) for k in keys for y in range(LAYERS[k]["years"][0], LAYERS[k]["years"][1] + 1)]
    print(len(jobs), "map jobs", flush=True)
    with cf.ThreadPoolExecutor(a.workers) as ex:
        paths = dict(zip(jobs, ex.map(lambda j: fetch_year(jar, j[0], j[1], a.cache), jobs)))
    man_path = os.path.join(a.out, "global.js")
    man = {"retrieved": dt.date.today().isoformat(), "layers": {}}
    stamps = []
    for k in keys:
        spec = LAYERS[k]
        years, geo = [], None
        for y in range(spec["years"][0], spec["years"][1] + 1):
            p = paths.get((k, y))
            if not p:
                continue
            geo = render(k, p, os.path.join(a.out, "global", f"{k}_{y}.png"))
            years.append(y)
            stamps.append(dt.date.fromtimestamp(os.path.getmtime(p)).isoformat())
        if years:
            man["layers"][k] = {"giovanni_id": spec["gid"], "name": spec["name"], "dataset": spec["dataset"], "unit": spec["unit"], "years": years,
                                "scale": spec["scale"], "grid": geo, "averaged": "calendar-year time-averaged map (Giovanni Time Averaged Map service)"}
            print(f"{k}: {len(years)} years rendered", flush=True)
    if stamps:
        man["retrieved"] = max(stamps)            # when Giovanni produced the files, not when this build ran
    with open(man_path, "w") as f:
        f.write("/* Generated by tools/fetch_giovanni_maps.py from NASA Giovanni time-averaged maps. Do not edit by hand. */\n")
        f.write("window.ETD_GLOBAL=" + json.dumps(man, separators=(",", ":")) + ";\n")
    print("wrote", man_path)


if __name__ == "__main__":
    main()
