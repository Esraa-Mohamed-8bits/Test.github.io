# Earth Trend Detective

NASA Space Apps 2026, challenge "Be an Earth System Trend Detective".

A single-page web app that lets you pick a country or featured place, choose an Earth-system variable, see its
time series, and test whether it has a trend (Sen's slope, Mann-Kendall, a t-test on the linear fit, 95% confidence
interval).

## Run it

No build step and no server-side code.

```bash
python3 -m http.server 8000
```

Then open `http://localhost:8000/` in a browser. Opening `index.html` directly from the folder also works.

The app reads only the files in this folder and makes no outside requests while it runs. Fonts are stored in `vendor/fonts/`.

## What is in the folder

| Path | What it is |
|---|---|
| `index.html` | The whole app (HTML, CSS, JavaScript). |
| `data/observations.js` | Annual and monthly series for every location, the global grid, and provenance. Written by `tools/fetch_giovanni.py`. |
| `data/ndvi.js` | Annual and monthly MODIS NDVI per location. Written by `tools/fetch_appeears.py`. |
| `data/calibration.js` | Width factors for the 95% prediction range. Written by `tools/calibrate.js`. |
| `data/imagery.js`, `data/imagery/` | MODIS images for the featured places. Written by `tools/fetch_appeears.py`. |
| `data/monthly/` | Monthly values per location as CSV, so any number in the app can be checked by hand. |
| `tools/fetch_giovanni.py` | Retrieval and aggregation script for NASA Giovanni. |
| `tools/fetch_appeears.py` | Retrieval and conversion script for NASA AppEEARS. |
| `tools/calibrate.js` | Backtests the projection at every place and calibrates its 95% range (Node.js). |
| `tools/sites.json` | The locations: the featured places and one reference point per country. |
| `vendor/` | Local copies of D3 7.9.0 (ISC licence), the Apache ECharts 4.9.0 world outlines (Apache-2.0) and, in `vendor/fonts/`, the Space Grotesk, Inter, JetBrains Mono and Fredoka fonts (SIL Open Font License 1.1, licences included). |
| `game.html` | Not included. The astronaut in the map corner links to it; place your game file next to `index.html`. Until then the app says the game isn't in the folder. |

## Features added in the latest version

- **Time ranges follow the data:** the year slider, its ticks, the period selector, Before & After, the climate stripes
  and every chart cover only the years the selected variable has data (at the selected place once it is loaded,
  otherwise across all stored places). Switching variable resets the period to that variable's full range.
- **Dashboard** (header button, or the Explore panel): every variable for one place on one screen. Headline figures,
  how strongly each variable is changing, every year at a glance, a tile per variable with its chart and projection,
  and a table of all statistics. Clicking a tile opens that variable in Explore.
- **Summary report:** one self-contained HTML file with key findings, projections, charts, all statistics, sources
  and methods. It opens in any browser and has a "Print / save as PDF" button.
- **Astronaut:** a bouncing, waving astronaut in the map corner (bottom-right on phones) links to `game.html`.
  Animations stop for visitors who ask their system for reduced motion.

## Data sources

Every environmental value and every satellite image comes from two of the challenge resources.

**NASA Giovanni** (`https://giovanni.gsfc.nasa.gov/giovanni/`), through its time-series API
(`https://api.giovanni.earthdata.nasa.gov/timeseries`), one request per point and variable:

| App variable | Giovanni variable | Dataset | Record used |
|---|---|---|---|
| Air temperature (2 m) | `M2SDNXSLV_5_12_4_T2MMEAN` | MERRA-2 daily statistics, 0.5° × 0.625° | 1980–2025 |
| Land surface (skin) temperature | `GLDAS_NOAH025_M_2_1_AvgSurfT_inst` | GLDAS Noah v2.1 monthly, 0.25° | 2000–2025 |
| Precipitation | `GPM_3IMERGM_07_precipitation` | GPM IMERG Final v07 monthly, 0.1° | 1998–2024 |
| Root-zone soil moisture | `GLDAS_NOAH025_M_2_1_RootMoist_inst` | GLDAS Noah v2.1 monthly, 0.25° | 2000–2025 |
| Evapotranspiration | `GLDAS_NOAH025_M_2_1_Evap_tavg` | GLDAS Noah v2.1 monthly, 0.25° | 2000–2025 |
| Surface runoff | `GLDAS_NOAH025_M_2_1_Qs_acc` | GLDAS Noah v2.1 monthly, 0.25° | 2000–2025 |
| Snow water equivalent | `GLDAS_NOAH025_M_2_1_SWE_inst` | GLDAS Noah v2.1 monthly, 0.25° | 2000–2025 |
| Relative humidity (surface) | `AIRS3STM_7_0_RelHumSurf_A` | AIRS/Aqua L3 monthly v7.0, 1° | 2003–2025 |
| Nitrogen dioxide (tropospheric) | `OMNO2d_003_ColumnAmountNO2TropCloudScreened` | OMI/Aura L3 daily v003, 0.25° | 2005–2025 |

**NASA AppEEARS** (`https://appeears.earthdatacloud.nasa.gov/`), through its API:

| Use in the app | Product and layer | Request type |
|---|---|---|
| NDVI time series | `MOD13A3.061`, 1 km monthly NDVI | point samples, every location |
| NDVI images (July, 2001–2025) | `MOD13A3.061`, 1 km monthly NDVI | area samples, 1° box per featured place |
| True-colour images (July of six years) | `MOD09A1.061`, bands 1, 4, 3 | area samples, same boxes |

The retrieval date is stored in each data file and shown in the app next to every variable ("Source information")
and on the Data & Methods page.

## Processing rules

Nothing is interpolated, gap-filled or simulated in the observation pipeline.

- A value equal to the file's fill value is missing.
- Daily to monthly: mean of the valid days. Air temperature needs at least 25 valid days in the month, NO₂ at least 5.
- Monthly to annual: a year is kept only when all 12 months are present. Otherwise it is blank in the app.
- Totals (precipitation, evapotranspiration, runoff) are summed from monthly totals; other variables are averaged.
- Anomalies are differences from the 2005–2020 mean, the period every dataset covers.
- Whole-planet figures are area-weighted means of the grid samples and are labelled as estimates.
- The retrieval date shown in the app is the date NASA served the data (Giovanni's `Request_time`, AppEEARS's
  completion time), not the date the bundle was rebuilt.

## Preprocessing (in the app, `index.html` section 4b)

- **Quality control:** monthly values outside physically possible limits are removed and counted, and that year's
  annual value is blanked. None of the current values falls outside the limits.
- **Seasonal cycle removal:** each month minus the mean of the same calendar month over 2005–2020 (all years if fewer
  than 8 are available).
- **Unusual months:** robust z-score (median and MAD) above 3.5 is circled on the chart and kept, never deleted.
- **Break check:** Pettitt change-point test on the yearly values; flagged when p < 0.01, a single jump fits much
  better than a straight line (under 60% of its error), and the jump exceeds 2.5 standard deviations. Tuned on this
  dataset: it flags the known artificial jump in GLDAS wind at about one place in five and other variables at 0–4% of
  places. Flagged series get a warning, a button to analyse only the part after the break, and no projection.

## Trend analysis

- Yearly values: Mann-Kendall (with trend-free pre-whitening when needed), Sen's slope and 95% interval, linear fit
  with t-test.
- Monthly values: seasonal Mann-Kendall (Hirsch et al., 1982) with the Hirsch–Slack (1984) correction for correlation
  between months when at least 10 complete years exist; seasonal Sen's slope and interval; a trend for each calendar
  month. The app explains when the yearly and monthly tests agree or disagree.

## Projection ("Where it is heading")

- Trend line: seasonal Sen's slope of the monthly values (converted to yearly units), level from the median of
  value − slope × year.
- 95% prediction range: Student-t interval around the line, widened for lag-1 autocorrelation through an effective
  sample size, then calibrated (below).
- Horizon: a third of the record, at most 15 years.
- Backtest: refit without the last five years, predict them, and compare with the long-term average. The projection
  is shown only when the trend line has the smaller error and no break is flagged.
- Calibration: `tools/calibrate.js` runs that backtest at every place. Before calibration the stated 95% range held
  80–98% of hidden years depending on the variable; each variable's range is scaled so that 95% of hidden years fall
  inside it. The factors are fitted on the same backtests they are judged on, so 95% is an in-sample figure.

## Rebuilding the data

You need a free NASA Earthdata Login (`https://urs.earthdata.nasa.gov/`). Once, while logged in, approve the
Giovanni application for your account; the first sign-in attempt prints the approval link if it is missing.

```bash
export EARTHDATA_USERNAME=your_username
```

```bash
export EARTHDATA_PASSWORD=your_password
```

```bash
python3 tools/fetch_giovanni.py --sites tools/sites.json --out data --cache .cache/giovanni --grid 5
```

```bash
pip install numpy pillow tifffile imagecodecs
```

```bash
python3 tools/fetch_appeears.py submit --sites tools/sites.json
```

```bash
python3 tools/fetch_appeears.py status
```

```bash
python3 tools/fetch_appeears.py build --sites tools/sites.json --out data --cache .cache/appeears
```

Then recalibrate the prediction ranges (needs Node.js):

```bash
node tools/calibrate.js
```

The Giovanni run makes about 7,300 requests and takes roughly an hour. AppEEARS queues its requests; they can take
from minutes to several hours before `build` has anything to download. Credentials are read from the environment and
are never written to disk.

## Statistics

Implemented in `index.html` (section "4. STATISTICS") and cross-checked against `scipy` and `pymannkendall` on real
series from this dataset: Mann-Kendall S, Z and p, Sen's slope and its 95% interval, the linear-fit slope, standard
error, p-value and R², and the trend-free pre-whitened Mann-Kendall test all agree.

The monthly methods were checked the same way: seasonal Mann-Kendall S, variance, Z and p, the Hirsch–Slack corrected
Z and p, and the seasonal Sen's slope agree with `pymannkendall`; the Pettitt statistic and p-value agree with
`pyhomogeneity`; the Student-t quantile agrees with `scipy` to 1e-12.

## Data-quality decisions

- **Wind speed is not included.** The only wind variable the Giovanni API offered at monthly resolution
  (`GLDAS_NOAH025_M_2_1_Wind_f_inst`) jumps abruptly around 2011 at about a quarter of the locations. That is a change
  in the model's input data, not in the weather, and a trend test would report it as a significant trend. The other
  variables were scanned for the same kind of step at every year and none showed a systematic one.
- **One dataset version per series.** No series splices two products or versions.
- **No global records are typed into the code.** The earlier hardcoded temperature, CO₂ and sea-ice arrays were
  removed. Whole-planet figures are computed from the Giovanni grid samples and labelled as estimates.

## Challenge resources not used, and why

| Resource | Reason |
|---|---|
| GES DISC Hydrology Time Series | Retired by NASA; the address redirects to a discontinuation notice. The same GLDAS variables come from Giovanni. |
| NOAA Climate Data Online | The API needs a personal NOAA token (requested by email at `https://www.ncdc.noaa.gov/cdo-web/token`). |
| KNMI Climate Explorer | The site asks visitors to confirm they are human before use, so it was not accessed by script. |
| CSA RADARSAT Constellation Mission, SAR tutorial | Radar imagery needing specialised processing; not a time series of an Earth-system variable. |
| GEO.ca | Canadian catalogue; no global variable for this app. |
| CSA SWOT page | SWOT launched in December 2022; too short a record for a trend test. |
| CSA ecosystems article | Background reading, no data. |

The SeaWiFS chlorophyll paper and the two statistics articles are method references, not data sources.

## Known limits

- Series exist for 232 fixed locations (17 featured places and one reference point per country). Clicking elsewhere
  selects the country's reference point, or the featured place within 50 km.
- GLDAS variables are land-only. 37 locations (small islands, and the Lake Chad point, which GLDAS treats as water)
  have no skin temperature, soil moisture, evapotranspiration, runoff or snow values.
- The globe layer exists for air temperature and precipitation only, one sample every 5°.
- MODIS images exist for the featured places only, for July.
- Country outlines come from the Apache ECharts world map. They are a drawing aid, not an environmental dataset.
- The Solutions Lab cites public guidance pages (US EPA, WHO, IPCC, Ramsar) as reading links. No data is taken from them,
  and its 2050 curves are labelled as simulated.
