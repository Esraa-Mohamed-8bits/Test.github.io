#!/usr/bin/env python3
"""
fetch_giovanni.py - data retrieval for Earth Trend Detective.

Source: NASA Giovanni time-series API (https://api.giovanni.earthdata.nasa.gov/timeseries),
the programmatic interface of Giovanni (https://giovanni.gsfc.nasa.gov/giovanni/), NASA GES DISC.

What it does
  1. Signs in to Giovanni with a NASA Earthdata Login (free account). Credentials are read from the
     environment variables EARTHDATA_USERNAME and EARTHDATA_PASSWORD and are never written to disk.
     The account must have approved the Giovanni application once (Earthdata -> Applications).
  2. For every site in the site list, and for every point of a regular global grid, downloads the
     full time series of each variable in VARIABLES as the CSV the API returns (cached unchanged).
  3. Aggregates to monthly and annual values (rules below) and writes:
        (observations.js holds both the annual series and the monthly values of every site)
        data/observations.js      annual series + grid + provenance, loaded by index.html
        data/monthly/<site>.csv   monthly values per site, for auditing

Aggregation rules (no value is ever invented or interpolated)
  * A value equal to the file's "undef" value, or with magnitude > 1e20, is missing.
  * Daily -> monthly: mean of valid days; the month is missing if fewer than MIN_DAYS valid days.
  * Monthly -> annual: the year is missing unless all 12 months are present.
  * "mean" variables use the mean of the 12 months; "sum" variables use the sum of monthly totals.

Usage
  EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=... python3 tools/fetch_giovanni.py \
      --sites tools/sites.json --out data --cache /path/to/cache [--grid 5] [--workers 10]
"""
import argparse, calendar, concurrent.futures as cf, datetime as dt, json, math, os, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request

API = "https://api.giovanni.earthdata.nasa.gov"
T0, T1 = "1980-01-01T00:00:00", "2025-12-31T23:59:59"
Y0, Y1 = 1980, 2025

SEC_DAY = 86400.0
# id: (Giovanni variable id, source time step, annual statistic, monthly conversion, min valid days)
VARIABLES = {
    "t2m":    dict(gid="M2SDNXSLV_5_12_4_T2MMEAN", step="daily", stat="mean", conv=lambda v, d: v - 273.15, min_days=25),
    "ts":     dict(gid="GLDAS_NOAH025_M_2_1_AvgSurfT_inst", step="monthly", stat="mean", conv=lambda v, d: v - 273.15),
    "prec":   dict(gid="GPM_3IMERGM_07_precipitation", step="monthly", stat="sum", conv=lambda v, d: v * 24.0 * d),
    "soil":   dict(gid="GLDAS_NOAH025_M_2_1_RootMoist_inst", step="monthly", stat="mean", conv=lambda v, d: v),
    "et":     dict(gid="GLDAS_NOAH025_M_2_1_Evap_tavg", step="monthly", stat="sum", conv=lambda v, d: v * SEC_DAY * d),
    # GLDAS monthly *_acc variables are the mean 3-hourly accumulation: monthly total = value * 8 * days.
    "runoff": dict(gid="GLDAS_NOAH025_M_2_1_Qs_acc", step="monthly", stat="sum", conv=lambda v, d: v * 8.0 * d),
    "snow":   dict(gid="GLDAS_NOAH025_M_2_1_SWE_inst", step="monthly", stat="mean", conv=lambda v, d: v),
    # GLDAS_NOAH025_M_2_1_Wind_f_inst is deliberately NOT retrieved: at about a quarter of the sites the series steps
    # abruptly around 2011 (forcing change in GLDAS-2.1), which a trend test reports as a false trend.
    "rh":     dict(gid="AIRS3STM_7_0_RelHumSurf_A", step="monthly", stat="mean", conv=lambda v, d: v),
    "no2":    dict(gid="OMNO2d_003_ColumnAmountNO2TropCloudScreened", step="daily", stat="mean", conv=lambda v, d: v / 1e15, min_days=5),
}
GRID_VARS = ["t2m", "prec"]

_tok_lock = threading.Lock()
_seen_files = set()
_token = {"v": None, "t": 0}


def signin():
    """Giovanni sign-in: HTTP basic auth through the Earthdata Login OAuth redirect; returns a 24 h token."""
    user, pw = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    if not user or not pw:
        sys.exit("Set EARTHDATA_USERNAME and EARTHDATA_PASSWORD (NASA Earthdata Login).")
    with tempfile.NamedTemporaryFile() as jar, tempfile.NamedTemporaryFile("w", suffix=".netrc") as netrc:
        os.chmod(netrc.name, 0o600)
        netrc.write(f"machine urs.earthdata.nasa.gov login {user} password {pw}\n"
                    f"machine api.giovanni.earthdata.nasa.gov login {user} password {pw}\n")
        netrc.flush()
        out = subprocess.run(["curl", "-sS", "-m", "120", "-L", "--netrc-file", netrc.name, "-c", jar.name, "-b", jar.name,
                              API + "/signin"], capture_output=True, text=True).stdout
    try:
        return json.loads(out)["token"]
    except Exception:
        sys.exit("Giovanni sign-in failed: " + out[:300])


def token(force=False):
    with _tok_lock:
        if force or not _token["v"] or time.time() - _token["t"] > 20 * 3600:
            _token["v"], _token["t"] = signin(), time.time()
        return _token["v"]


def fetch_raw(gid, lat, lon, cache):
    """Returns the API's CSV text for one variable and point, using the on-disk cache when present."""
    path = os.path.join(cache, gid, f"{lat:.3f}_{lon:.3f}.csv")
    _seen_files.add(path)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return open(path).read()
    q = urllib.parse.urlencode({"data": gid, "location": f"[{lat},{lon}]", "time": f"{T0}/{T1}"})
    last = ""
    for attempt in range(6):
        req = urllib.request.Request(f"{API}/timeseries?{q}", headers={"authorizationtoken": token()})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                txt = r.read().decode()
            if not txt.startswith("prod_name"):
                last = txt[:200]
                raise ValueError("unexpected body")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w") as f:
                f.write(txt)
            os.replace(path + ".tmp", path)
            return txt
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read()[:200]!r}"
            if e.code in (401,) or (e.code == 403 and b"token" in last.encode().lower()):
                token(force=True)
            elif e.code in (400, 404) or (e.code == 403 and "invalid" in last):
                break  # the API says this request can never succeed (e.g. point outside coverage)
        except Exception as e:  # timeouts, resets
            last = last or repr(e)
        time.sleep(2 + 4 * attempt)
    return "ERROR " + last


def parse(txt):
    """CSV -> (header dict, [(datetime, value-or-None)])."""
    if txt.startswith("ERROR"):
        return {"error": txt}, []
    head, rows, body = {}, [], False
    for line in txt.splitlines():
        if not line.strip():
            continue
        if line.startswith("Timestamp"):
            body = True
            continue
        k, _, v = line.partition(",")
        if not body:
            head[k] = v
            continue
        undef = float(head.get("undef", "nan"))
        try:
            x = float(v)
        except ValueError:
            x = None
        if x is not None and (math.isnan(x) or abs(x) > 1e20 or x == undef):
            x = None
        rows.append((dt.datetime.strptime(k[:10], "%Y-%m-%d"), x))
    return head, rows


def to_monthly(spec, rows):
    """{(year, month): converted value} following the aggregation rules in the module docstring."""
    out = {}
    if spec["step"] == "monthly":
        for t, x in rows:
            if x is not None:
                out[(t.year, t.month)] = spec["conv"](x, calendar.monthrange(t.year, t.month)[1])
        return out
    acc = {}
    for t, x in rows:
        if x is not None:
            acc.setdefault((t.year, t.month), []).append(x)
    for (y, m), xs in acc.items():
        if len(xs) >= spec["min_days"]:
            out[(y, m)] = spec["conv"](sum(xs) / len(xs), calendar.monthrange(y, m)[1])
    return out


def to_annual(spec, monthly):
    vals = []
    for y in range(Y0, Y1 + 1):
        ms = [monthly.get((y, m)) for m in range(1, 13)]
        if any(v is None for v in ms):
            vals.append(None)
        else:
            vals.append(sum(ms) if spec["stat"] == "sum" else sum(ms) / 12.0)
    return vals


def sig(v, n=5):
    return None if v is None else float(f"{v:.{n}g}")


def monthly_block(monthly):
    """Monthly values as {"start": first month index (year*12 + month-1), "vals": [...]}; null = missing month.
    The app uses these for the seasonal-cycle removal, the seasonal Mann-Kendall test and the prediction backtest."""
    keys = sorted(monthly)
    if not keys:
        return None
    i0, i1 = keys[0][0] * 12 + keys[0][1] - 1, keys[-1][0] * 12 + keys[-1][1] - 1
    return {"start": i0, "vals": [sig(monthly.get(divmod(i, 12)[0:1] + (divmod(i, 12)[1] + 1,)), 4) for i in range(i0, i1 + 1)]}


def trim(vals):
    """Drop leading missing years; returns (start year, values) or (None, [])."""
    i = next((k for k, v in enumerate(vals) if v is not None), None)
    if i is None:
        return None, []
    j = max(k for k, v in enumerate(vals) if v is not None)
    return Y0 + i, [sig(v) for v in vals[i:j + 1]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--grid", type=float, default=5.0, help="grid spacing in degrees (0 = no grid)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--only", default="", help="comma-separated variable ids (default: all)")
    a = ap.parse_args()
    sites = json.load(open(a.sites))
    only = [v for v in a.only.split(",") if v] or list(VARIABLES)
    jobs = [(s["lat"], s["lon"], v) for s in sites for v in only]
    grid_pts = []
    if a.grid:
        n_lat, n_lon = int(round(180 / a.grid)), int(round(360 / a.grid))
        grid_pts = [(-90 + a.grid * (i + .5), -180 + a.grid * (j + .5)) for i in range(n_lat) for j in range(n_lon)]
        jobs += [(la, lo, v) for la, lo in grid_pts for v in GRID_VARS if v in only]
    print(f"{len(jobs)} requests ({len(sites)} sites, {len(grid_pts)} grid points)", flush=True)
    token()
    done, errs = 0, 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(fetch_raw, VARIABLES[v]["gid"], la, lo, a.cache) for la, lo, v in jobs]
        for f in cf.as_completed(futs):
            done += 1
            errs += f.result().startswith("ERROR")
            if done % 200 == 0:
                print(f"  {done}/{len(jobs)} done, {errs} failed", flush=True)
    print(f"downloads finished: {errs} failed of {len(jobs)}", flush=True)

    # ---- aggregate -------------------------------------------------------------------------
    meta, out_sites = {}, {}
    os.makedirs(os.path.join(a.out, "monthly"), exist_ok=True)
    for s in sites:
        rec = {"lat": s["lat"], "lon": s["lon"], "series": {}, "monthly": {}, "cells": {}, "missing": {}}
        monthly_all = {}
        for v in only:
            spec = VARIABLES[v]
            head, rows = parse(fetch_raw(spec["gid"], s["lat"], s["lon"], a.cache))
            if "error" in head:
                rec["missing"][v] = "request failed: " + head["error"][6:120]
                continue
            meta.setdefault(v, {k: head.get(k, "") for k in ("prod_name", "doi", "param_short_name", "param_name", "unit", "lat_resolution", "lon_resolution")})
            monthly = to_monthly(spec, rows)
            monthly_all[v] = monthly
            start, vals = trim(to_annual(spec, monthly))
            if start is None:
                rec["missing"][v] = "no complete year of valid data at this grid cell (e.g. ocean cell for a land-only product)"
                continue
            rec["series"][v] = {"start": start, "vals": vals}
            rec["monthly"][v] = monthly_block(monthly)
            rec["cells"][v] = [float(head.get("lat", "nan")), float(head.get("lon", "nan"))]
        out_sites[s["id"]] = rec
        keys = sorted({k for m in monthly_all.values() for k in m})
        safe = "".join(c if c.isalnum() else "_" for c in s["id"])
        with open(os.path.join(a.out, "monthly", safe + ".csv"), "w") as f:
            f.write(f"# {s['name']} ({s['lat']}, {s['lon']}). Monthly values derived from the NASA Giovanni time-series API by tools/fetch_giovanni.py. Empty = missing.\n")
            f.write("year,month," + ",".join(v for v in only if v in monthly_all) + "\n")
            for (y, m) in keys:
                f.write(f"{y},{m}," + ",".join("" if monthly_all[v].get((y, m)) is None else f"{monthly_all[v][(y, m)]:.5g}" for v in only if v in monthly_all) + "\n")

    grid = None
    if grid_pts:
        grid = {"step": a.grid, "lat0": -90 + a.grid / 2, "lon0": -180 + a.grid / 2, "nlat": n_lat, "nlon": n_lon, "vars": {}}
        for v in GRID_VARS:
            if v not in only:
                continue
            spec, cells, starts = VARIABLES[v], [], []
            for la, lo in grid_pts:
                head, rows = parse(fetch_raw(spec["gid"], la, lo, a.cache))
                ann = [] if "error" in head else to_annual(spec, to_monthly(spec, rows))
                cells.append(ann)
                starts += [Y0 + k for k, x in enumerate(ann) if x is not None][:1]
            start = min(starts) if starts else Y0
            # one row per grid point, years start..Y1, rounded; null = missing
            grid["vars"][v] = {"start": start, "cells": [[None if (not c or c[y - Y0] is None) else round(c[y - Y0], 2 if v == "t2m" else 0) for y in range(start, Y1 + 1)] for c in cells]}

    # retrieval date = the latest "Request_time" Giovanni wrote into the raw files (not the date this build ran)
    req = [l.split(",", 1)[1][:10] for p in _seen_files for l in open(p).read().splitlines()[:20] if l.startswith("Request_time,")]
    payload = {"retrieved": max(req) if req else dt.date.today().isoformat(), "api": API + "/timeseries", "period_requested": [T0, T1],
               "year_end": Y1, "variables": {v: dict(meta.get(v, {}), giovanni_id=VARIABLES[v]["gid"], source_step=VARIABLES[v]["step"], annual_stat=VARIABLES[v]["stat"]) for v in only},
               "sites": out_sites, "grid": grid}
    with open(os.path.join(a.out, "observations.js"), "w") as f:
        f.write("/* Generated by tools/fetch_giovanni.py from the NASA Giovanni time-series API. Do not edit by hand. */\n")
        f.write("window.ETD_DATA=" + json.dumps(payload, separators=(",", ":")) + ";\n")
    print("wrote", os.path.join(a.out, "observations.js"), os.path.getsize(os.path.join(a.out, "observations.js")), "bytes")


if __name__ == "__main__":
    main()
