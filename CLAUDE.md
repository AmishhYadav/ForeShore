# CLAUDE.md — FORESHORE

Operational context for Claude Code. Read this before touching anything.
For full background, rationale, and the problem statement, see `PROJECT_CONTEXT.md`.

---

## What this is

**FORESHORE** — an agentic marine intelligence platform for small-boat fishermen on the
Palk Bay / Gulf of Mannar coast, plus a shore-side control console for fisheries and
Coast Guard operators.

Built for **Smart India Hackathon PS SIH26176 ("ORCA")**, submitted by ISRO / Department
of Space, filed under **Disaster Management**.

The disaster-management framing is not decoration. Safety, alerting, and hazard avoidance
outrank conversational polish in every design tradeoff. When in doubt, favour the safety path.

---

## Non-negotiable invariants

These are enforced in code, not left to model judgment. Do not weaken them to make a demo work.

1. **Advisory ceiling.** FORESHORE never issues a verdict more permissive than the governing
   IMD Coastal Bulletin for the area. It may be *more* cautious. Implement as a deterministic
   post-check on the final verdict object, after the LLM has produced it. If the check trips,
   the verdict is downgraded and the downgrade is logged and shown.

2. **Three verdicts only.** `GO` / `GO_WITH_CAUTION` / `DO_NOT_ADVISE`. `DO_NOT_ADVISE` is a
   designed outcome for missing, stale, or contradictory inputs — not an error state. It must
   hand off to a named human authority, never guess.

3. **No unsourced numbers.** Every quantitative claim in a response traces to a retrieved
   record with a source, an acquisition timestamp, and a spatial resolution. If a value has no
   provenance record, it does not appear in the answer. The LLM does not supply values from
   its own knowledge — ever.

4. **Staleness is surfaced, never hidden.** Every answer carries an evidence panel. Nothing is
   labelled "current" that isn't.

5. **Geofence classes are semantically distinct.** An international maritime boundary is a legal
   line you must not cross. A marine protected area is a conservation zone with restrictions.
   They get different alert copy, different lead times, different severity. Do not collapse them
   into one "restricted zone" type.

---

## Architecture shape

Two surfaces, one agent core. The agents, tools, and reasoning traces are shared; only the
renderer differs. This is the central architectural claim — preserve it.

```
  Boat UI (Tamil, voice-first)        Shore console (English, fleet view)
                \                                  /
                 \________  presentation  ________/
                                 |
                        Agent orchestration
                (planner → specialists → synthesis)
                                 |
                   Tool layer (typed, deterministic)
                                 |
        ┌────────────────────────┴────────────────────────┐
        │                                                 │
   Local geospatial store                        Live API clients
   (pre-ingested EO rasters,                     (IMD, Open-Meteo)
    boundary vectors)
        ▲
        │
   Scheduled ingestion (MOSDAC batch pull, reprojection, tiling)
```

Two paths, not one:

- **Request path** — user asks, agents answer.
- **Push path** — background evaluation loop over tracked vessel positions, firing proactive
  hazard and geofence-approach alerts.

The push path is a hard requirement (PS bullets 7 and 8 say *proactive* and *when approaching*).
A request-response-only system fails the problem statement. Build the loop early; it is the
thing most competing teams will miss.

---

## Data sources — what you can and cannot call live

**Verified by audit. Do not assume anything not listed here.**

### Live-callable inside an agent turn

| Source | Endpoint | Notes |
|---|---|---|
| IMD Coastal Bulletin | `api.imd.gov.in/api/v1/coastalbulletin` | Has a "South Tamilnadu coast" layer. **This is the advisory ceiling source.** |
| IMD Sea Area Bulletin | `api.imd.gov.in/api/v1/seabulletin` | Sea state, wind, visibility, synoptic |
| IMD Port Warning | `api.imd.gov.in/api/v1/portwarning` | |
| IMD Fishermen Warning | indexed as api-23 | Confirm exact path on first probe |
| IMD Cyclone Track | `api.imd.gov.in/api/v1/cyclone_track` | Observed + forecast, MSW, category |
| IMD Cyclone Wind Warning | `api.imd.gov.in/api/v1/cyclone_wind` | **GeoJSON MultiPolygon** per threshold (27/34/50/64 kt) |
| IMD Cone of Uncertainty | `api.imd.gov.in/api/v1/cyclone_cou` | **GeoJSON MultiPolygon** |
| IMD District Nowcast | `api.imd.gov.in/api/v1/districtnowcast` | Cloud-to-ground lightning probability bands |
| IMD AWS observations | `api.imd.gov.in/api/v1/aws_data?sid=25` | sid=25 is Tamil Nadu |
| IMD Sun/Moon | `api.imd.gov.in/api/v1/sunmoon?lat=&lon=` | Trip timing |
| Open-Meteo Marine | `marine-api.open-meteo.com` | Free, keyless, 10k calls/day, CC BY 4.0 |

The cyclone wind and cone endpoints return real polygons — feed them directly into the routing
cost field and the geofence engine as exclusion zones. Do not re-derive geometry from text.

**Caveat:** IMD documentation mentions IP whitelisting. Verify per-endpoint on first run. IMD
also asks for attribution and client-side caching — honour both.

### NOT live-callable — must be pre-ingested

**MOSDAC** (Oceansat-3 OCM chlorophyll, INSAT-3D/3DR/3DS SST). The thing ISRO calls "API based
Access" is a batch downloader: `mdapi.zip` → edit `config.json` → run `mdapi.py`. It returns
granule files, not point values.

- Search works unauthenticated. **Download requires approved account credentials.**
- Cap: 5,000 files per user per day.
- Supports `boundingBox` as `"minLon,minLat,maxLon,maxLat"` and `startTime`/`endTime` as `YYYY-MM-DD`.
- `datasetId` is the exact product name from the catalog browser (e.g. `E06OCM_L2C_AD`).
- Account tiers: NRT users get real-time; **General users get L2+ in near-real-time and L1 with
  a 3-day latency.** Assume General tier.

Never attempt a MOSDAC call from inside an agent turn. Agents query the local store only.

### Gap — no public API found

**INCOIS** (PFZ advisories, Ocean State Forecast). No documented public API. There is an Esri
Geoportal at `incois.gov.in/geoportal/sharing/rest/` that may expose OGC services — unprobed.

Until proven otherwise, **derive PFZ-equivalent zones locally** from chlorophyll + SST fronts.
Label derived zones unambiguously as an indicative derived product, never as the official INCOIS
PFZ advisory. This distinction must survive into the UI copy.

### Boundaries

- Baseline: Marine Regions (Flanders Marine Institute) — World EEZ v12 with boundary polylines,
  12NM territorial seas v4, 24NM contiguous zones v4. GeoPackage/Shapefile/KML, plus WFS at
  `geo.vliz.be/geoserver`.
- Preferred for IMBL: digitize from the **1974 and 1976 India–Sri Lanka maritime boundary
  agreement coordinate lists**. Better provenance than a generic shapefile. Store the source
  citation alongside the geometry.
- Gulf of Mannar Marine National Park: WDPA / Protected Planet. **Unverified — check geometry
  quality before relying on it.**

### Known data weakness — handle explicitly

Open-Meteo's global wave model runs at ~28 km resolution (the 5 km model covers Europe only).
Palk Bay is ~30–100 nm wide and shallow. A 28 km cell is coarse there.

**Therefore:** the IMD Coastal Bulletin sea-state descriptor is the authoritative sea state.
The model provides gradient and trend between bulletin issuances only, and always appears in the
evidence panel tagged with its resolution. Do not let a model wave number drive a `GO` verdict
on its own.

---

## Region config

Everything region-specific lives in config. No hardcoded coordinates, boundary names, or
language codes anywhere in application logic. A judge will ask "does this only work for Tamil
Nadu?" — the answer must be a config file swap, demonstrated live.

```
region: palk_bay_gom
bbox: [78.0, 8.0, 80.6, 10.9]   # approximate, refine against actual coverage
anchor_ports: Rameswaram, Nagapattinam, Tuticorin
primary_language: ta
fallback_language: en
imd_coastal_layer: "South Tamilnadu coast"
imd_state_id: 25
```

---

## Conventions

- **Python** for ingestion, geospatial processing, agents. **TypeScript/React** for both UIs.
- Geospatial: PostGIS for vectors, COG or equivalent tiling for rasters. Store CRS explicitly;
  everything in EPSG:4326 unless there's a stated reason otherwise.
- Tools are **typed and deterministic**. Spatial operations are real geospatial computation —
  nearest-polygon, raster thresholding, path planning over a cost field. The LLM selects and
  sequences tools; it does not perform the geometry or the arithmetic.
- **Routing uses A\* or Dijkstra over a weighted grid** (significant wave height, wind, currents,
  exclusion polygons, bathymetry). Never LLM-generated waypoints. This is a credibility tripwire
  — a fake router is instantly visible to an ISRO judge.
- Every tool call and its result is persisted as a reasoning trace, retrievable and renderable.
  Explainability is a stored artifact, not a post-hoc LLM narration.
- Ingestion jobs are idempotent and record granule acquisition time on write.

---

## Do not

- Do not call MOSDAC synchronously from an agent.
- Do not let the LLM emit a numeric value that has no provenance record.
- Do not fabricate PFZ polygons or present derived zones as official INCOIS advisories.
- Do not build the request path only — the push/alert loop is a scored requirement.
- Do not collapse IMBL and MPA into one geofence type.
- Do not hardcode region specifics.
- Do not "fix" a failing demo by relaxing the advisory ceiling or the abstention path.
- Do not add features not traceable to a PS capability bullet. Scope creep costs marks.

---

## Open unknowns (resolve, don't assume)

1. MOSDAC account approval turnaround — **longest-lead blocker, register immediately**
2. Whether IMD endpoints require IP whitelisting, and for which
3. Whether the INCOIS geoportal exposes usable PFZ / OSF layers
4. Exact MOSDAC `datasetId` strings for OCM chlorophyll and INSAT SST, and real cadence over bbox
5. Actual cloud-free coverage frequency for OCM over Palk Bay — measure over two weeks; this
   number defines the staleness thresholds and belongs on a slide
6. WDPA geometry quality for Gulf of Mannar
7. Tamil ASR accuracy on fishing-domain vocabulary (species names, tide terms, "PFZ")

Staleness thresholds, confidence bands, and alert lead times are **not yet defined**. Derive them
from measured data cadence (item 5), do not invent them.
