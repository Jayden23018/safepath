# 🚶 SafePath

**Walk the safer way around Penn.** SafePath overlays Philadelphia crime history and
user-reported sidewalk/accessibility issues onto the walking network around the
University of Pennsylvania, then routes you on the **safest** path between two points —
not just the shortest one — and shows you exactly *why*.

**Live demo:** [safepath-black.vercel.app](https://safepath-black.vercel.app)

Built for a Penn summer-program final project spanning three course modules:
**Pandas** (data prep) → **Graph** (routing) → **Flask + HTML** (web).

## Screenshot

Shortest (blue) vs. safest (green) route, with the high-risk segments the safe route
avoided highlighted in red:

![Route comparison map](docs/screenshot-home.png)

## Why it's interesting

Most map apps optimize for *shortest*. SafePath optimizes for *safest*, and makes the
tradeoff legible instead of hiding it in a black box:

- **Two routes, one graph.** Dijkstra runs twice on the same walking graph — once
  weighted by distance (shortest), once by distance *and* a crime-risk score derived
  from ~107k historical incidents (safest) — and both are drawn on the map together.
- **See the danger, not just avoid it.** A toggleable risk-colored street layer
  (green→red) and a raw crime-density heatmap make "where is this dangerous" visible
  at a glance, before you even plan a route.
- **The app explains its own reasoning.** Every route comes with a plain-language
  breakdown — how many high-risk blocks were avoided, how many meters of "still risky"
  walking remain, and what kind of incidents (theft, robbery, assault, …) were
  historically flagged nearby — surfaced in both the sidebar and a dismissible popup.
- **Reports change future routes.** Submit a sidewalk-damage or poor-lighting report
  and the very next route computed avoids that block — a real, meaningful `POST` that
  mutates persisted state, not just a form that vanishes into a database.

## What it does

- Loads the OSM walking graph within ~3.5 km of Penn (cached as GraphML + pickle).
- Buffers each street edge by 120 m and assigns nearby crime points to it with linear
  distance decay (closer points count more — a network-KDE-style spread so risk isn't
  confined to only the single nearest street).
- Scores every edge: severity-weighted, distance-decayed crime sum → **percentile-rank**
  normalization → `safety_weight = length × (1 + 1.5 · risk_norm)`. Rank normalization
  (not a length-divided density) keeps Dijkstra's full dynamic range and avoids
  penalizing short crosswalk segments just for being short.
- Runs Dijkstra **twice** on the same graph — once on `length` (shortest, blue),
  once on `safety_weight` (safest, green) — and renders both on a Leaflet/folium map,
  along with the segments the safe route avoided (red) and the high-risk segments it
  still can't route around (amber dashed).
- Search-by-name or pick-on-map for origin/destination, backed by a local index of
  landmarks, intersections, and points of interest — no external geocoder, works offline.
- A "Why this route?" popup opens on every computed route (dismissible, with a
  "don't show again" option), mirroring the sidebar's route comparison plus specific
  historical incident types (e.g. "Thefts (19), Robbery No Firearm (2)") pulled from
  the same crime-to-edge aggregation used to score the route — no extra database queries.
- Accepts user reports of sidewalk damage / missing ADA ramps / poor lighting /
  unsafe crossings. Reports persist to SQLite and bump the nearest edge's weight,
  so **submitted reports change future routes** (persistence + meaningful POST).

## Run it

The repo ships with a pre-built graph + risk database (`data/walk.graphml`,
`data/walk.pkl`, `data/safepath.db`), so a fresh clone runs immediately:

```bash
python3 -m pip install -r requirements.txt
python3 app.py                   # http://127.0.0.1:5000/  (or: python3 -m flask --app app run)
```

Pick `Market St Corridor` ↔ `Mantua (35th & Haverford)` (or `Van Pelt Library`) to see
the two routes diverge and the risk layers light up. Then submit a `sidewalk_damage`
report on the blue line and re-route — the green (safest) line shifts off the reported
block.

To rebuild everything from scratch with your own crime data (see below), run:

```bash
python3 prep_data.py             # rebuilds data/walk.graphml + data/walk.pkl + data/safepath.db
```

### Crime data

Crime CSVs are **not** bundled (too large, and licensed data). Download yearly incident
CSVs from [OpenDataPhilly — Crime Incidents](https://opendataphilly.org/datasets/crime-incidents/)
and drop them in `data/` as `crime_<YEAR>.csv`. Expected columns (auto-mapped):
`point_x` (lng), `point_y` (lat), `text_general_code`, `dispatch_date`, `dc_key`.

If no CSV is present, `prep_data.py` falls back to ~20 synthetic points so the
pipeline still runs. An optional live source is the
[PHL Carto SQL API](https://phl.carto.com/api/v2/sql) (table `incidents_part1_part2`,
no key) — documented here, not wired into the MVP.

## Self-checks

```bash
python3 prep_data.py    # H1 (normalization) + H4/H4b (buffer assignment) + H9 (intersection places)
python3 routing.py      # H2 (safety_weight formula) + H3 (divergence) + H6/H7 (hot segments)
python3 app.py --check  # H5 (divergence classification) + H8 (snap guard) + H10-H12 (search)
```
All `__main__` blocks use bare `assert` — no test framework.

## Project layout

```
final_project/
├── prep_data.py         # Pandas + Graph: CSV → edge_risk / crime_points / places → SQLite
├── routing.py            # Graph: shortest vs safest Dijkstra, report bumps, hot segments
├── app.py                 # Flask: GET / , POST /route /report, GET /search /about
├── templates/             # index.html (map + forms + explainer modal), about.html
├── static/                # style.css, app.js (layers, autocomplete, map picking, modal)
├── data/                  # walk.graphml / walk.pkl / safepath.db (committed) + *.csv (gitignored)
├── vercel.json             # Vercel Flask function config
└── requirements.txt
```

## Data sources & attribution

- **Crime incidents** — City of Philadelphia / Philadelphia Police Department, via
  [OpenDataPhilly](https://opendataphilly.org/datasets/crime-incidents/) and the
  [PHL Carto SQL API](https://phl.carto.com/api/v2/sql).
- **Walking network** — © OpenStreetMap contributors, via [OSMnx](https://osmnx.readthedocs.io/).
- **Sidewalk / ADA conditions** — user-submitted only; no open dataset used.
- **Map rendering** — [Folium](https://python-visualization.github.io/folium/) /
  [Leaflet](https://leafletjs.com), basemap tiles © [CARTO](https://carto.com/attribution).

## Limitations (read before trusting a route)

1. **Proxy, not prediction.** Historical crime density is not future risk.
2. **Snapshot, not real-time.** Crime data only updates when someone re-runs
   `prep_data.py` by hand; the Carto API is documented but not wired live. Routes
   themselves are computed fresh on every request, so a submitted report changes the
   very next route.
3. **Fixed area.** Only ~3.5 km around Penn.
4. **Anecdotal sidewalk data.** Reports are user-submitted, sparse, unverified.
5. **Simple model.** Distance-decayed weighting, single α, no time-of-day decomposition.
6. **Intersection double-counting.** The 120 m buffer overlaps at intersections,
   amplifying intersection risk *by design*.
7. **Length + weight only.** No turn penalties, signals, grade, or lighting.
8. **Severity weights are judgment calls**, not empirically derived.
9. **Report bump is a tuned constant.** A single report bumps one edge by
   `×(1 + 5.0 · severity)`. On dense urban grids a near-equivalent parallel street often
   exists, so a single report usually shifts the *node sequence* rather than the total
   length. Several reports along one block produce a clearly visible reroute.
10. **α = 1.5 is locked.** Riskiest edge = 2.5× true length; zero-crime edge = true length.
11. **Percentile normalization loses magnitude.** Risk is normalized by percentile rank, so the
    riskiest block and a merely-risky one become the same rank-distance apart — magnitude is
    discarded. The p90 high-risk threshold is itself rank-based, so the two are semantically
    consistent.
12. **No street-address search.** Place search is a local index of landmarks,
    intersections, and OSM points of interest — no external geocoder, so it won't find
    an arbitrary house number.
13. **Reports don't persist on Vercel.** The hosted deployment's filesystem is read-only
    outside `/tmp`, so `/report` writes to a per-instance copy of the database that's
    wiped on the next cold start. Locally (`python3 app.py`), reports persist normally.

## Not in this MVP (future work)

- External geocoder for arbitrary street addresses.
- Time-of-day / lighting-aware risk (currently a single static score per street).
- Persistent report storage on serverless hosting (needs an external database).
