# 🚶 SafePath

Philly walking-safety router for the Penn summer-program final project. Overlays a
"safety data layer" (crime history + user-reported sidewalk/accessibility issues) onto
the walking network around the University of Pennsylvania, then finds a **safer** route —
not just the shortest one.

Spans the three course modules: **Pandas** (data prep) → **Graph** (routing) →
**Flask + HTML** (web).

## What it does

- Loads the OSM walking graph within ~1.5 km of Penn (cached as GraphML).
- Buffers each street edge by 30 m and counts crime points inside each buffer.
- Scores every edge: severity-weighted, distance-decayed crime sum → percentile-rank
  normalize → `safety_weight = length × (1 + 1.5 · risk_norm)`. (Not divided by edge length:
  the buffer means a short segment and a long street in the same crime field collect nearly
  equal crime mass, so dividing by length would measure "edge shortness", not danger.)
- Runs Dijkstra **twice** on the same graph — once on `length` (shortest, blue),
  once on `safety_weight` (safest, green) — and renders both on a folium map.
- Accepts user reports of sidewalk damage / missing ADA ramps / poor lighting /
  unsafe crossings. Reports persist to SQLite and bump the nearest edge's weight,
  so **submitted reports change future routes** (persistence + meaningful POST).

## Run it

```bash
python3 -m pip install -r requirements.txt
python3 prep_data.py            # builds data/walk.graphml + data/safepath.db
python3 -m flask --app app run  # http://127.0.0.1:5000/
```

Pick `Market St Corridor` ↔ `Locust Walk` (or `Van Pelt Library`) to see the two
routes diverge. Then submit a `sidewalk_damage` report on the blue line and re-route —
the green (safest) line shifts off the reported block.

### Crime data

Crime CSVs are **not** bundled (too large). Download yearly incident CSVs from
[OpenDataPhilly — Crime Incidents](https://opendataphilly.org/datasets/crime-incidents/)
and drop them in `data/` as `crime_<YEAR>.csv`. Expected columns (auto-mapped):
`point_x` (lng), `point_y` (lat), `text_general_code`, `dispatch_date`, `dc_key`.

If no CSV is present, `prep_data.py` falls back to ~20 synthetic points so the
pipeline still runs. An optional live source is the
[PHL Carto SQL API](https://phl.carto.com/api/v2/sql) (table `incidents_part1_part2`,
no key) — documented here, not wired into the MVP.

## Self-checks

```bash
python3 prep_data.py   # H1 (normalization) + H4/H4b (buffer assignment)
python3 routing.py     # H2 (safety_weight formula) + H3 (route divergence)
```
All `__main__` blocks use bare `assert` — no test framework.

## Project layout

```
final_project/
├── prep_data.py        # Pandas + Graph: CSV → edge_risk → SQLite
├── routing.py          # Graph: shortest vs safest, report bumps
├── app.py              # Flask: GET / POST /route /report /about
├── templates/          # index.html (map + forms), about.html
├── static/style.css
├── data/               # .gitignored: walk.graphml, safepath.db, *.csv
└── requirements.txt
```

## Data sources & attribution

- **Crime incidents** — City of Philadelphia / Philadelphia Police Department, via
  [OpenDataPhilly](https://opendataphilly.org/datasets/crime-incidents/) and the
  [PHL Carto SQL API](https://phl.carto.com/api/v2/sql).
- **Walking network** — © OpenStreetMap contributors, via [OSMnx](https://osmnx.readthedocs.io/).
- **Sidewalk / ADA conditions** — user-submitted only; no open dataset used.

## Limitations (read before trusting a route)

1. **Proxy, not prediction.** Historical crime density is not future risk.
2. **Snapshot, not real-time.** Local CSV; the Carto API is documented but not wired live.
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

## Not in this MVP (future work)

- Railway/Render deployment, click-to-pick coordinates on the map, crime heat-map overlay.
