# FORESHORE — Master Implementation Plan

**Target:** working live demo + 6-slide PDF for the internal SIH round, **~8 September 2026**.
Today is **30 August 2026**. Nine working days.

**Executor:** one developer (Amish) + Claude Code. Plan is written to be executed top-to-bottom
with minimal re-derivation: exact endpoints, exact file paths, exact contracts, exact acceptance
tests. Full scope — no features cut for solo capacity.

---

## Context

SIH 2026 PS **SIH26176 "ORCA"** — ISRO / Department of Space, Software, theme **Disaster
Management**. FORESHORE is an agentic marine intelligence platform for small-boat fishermen on
the Palk Bay / Gulf of Mannar coast, plus a shore-side console for fisheries and Coast Guard
operators. Two surfaces, one agent core.

A prior research pass probed every named data source live. Findings are load-bearing for this
plan and are not re-litigated here — see "Verified source table" below. The short version: the
data position is far stronger than the project docs assumed, **every source needed for the demo
works today with zero credentials**, and the two registrations in flight (IMD, MOSDAC) are
upside rather than dependencies.

---

## What actually wins — judge's perspective

Two different audiences, two different artifacts:

**Internal round (~8 Sept)** — college jury, live demo + PPT. Rewards a prototype that runs
without excuses. A stable narrow demo beats an ambitious broken one.

**Portal screening (30 Sept)** — the PS-owning organisation reviews the **6-slide PDF alone**.
No demo, no conversation. The PDF must survive an ISRO reviewer who knows exactly what is
reachable in the public domain.

Design consequence that shapes every phase below: **the demo exists to manufacture the proof
artifacts the PDF needs.** Every phase names the screenshot or number it must produce.

Five things that separate this from the ~80% of submissions that will be LangChain + a weather
API + five boxes labelled "agents":

1. **Real numbers with provenance.** Named endpoints, acquisition timestamps, resolutions.
   Most decks will say "we will use satellite data."
2. **The three-source disagreement.** IMD's human bulletin, INCOIS's 11 km assimilated model and
   Open-Meteo's 28 km global model disagree about Palk Bay today. Showing that, and explaining
   which one governs and why, is judgment — not retrieval.
3. **A real router.** A*/Dijkstra over a weighted cost field. "Is your routing real?" is an
   anticipated question and a fake router is instantly visible.
4. **The push path.** PS bullets say *proactive* and *when approaching*. A request-response
   chatbot fails them. Most teams will miss this.
5. **Refusal.** `DO_NOT_ADVISE` with a named human handoff, and an advisory ceiling that cannot
   be relaxed. Answers "what if you say it's safe and someone dies" with architecture rather
   than a disclaimer.

---

## Verified source table — use these, do not go looking for others

All probed live on 2026-08-30. **INCOIS and IMD GeoServer require a browser `User-Agent` and a
`Referer` header** — without them you get 403. This is the single most common way to lose a day.

| Purpose | Endpoint | Auth |
|---|---|---|
| **Advisory ceiling** | `mausam.imd.gov.in/Forecast/coastal_bulletin_new.php?id=6` (ACWC Chennai; `id` 1–7 = coastal offices) | none |
| District nowcast / lightning | `reactjs.imd.gov.in/geoserver/imd/wfs` → `imd:NowcastWarningDistrict` | none |
| AWS observations | same GeoServer → `imd:aws_data_layer` | none |
| Cyclone track | same GeoServer → `imd:Cyclone_Track_V` (empty when no active cyclone) | none |
| Cyclone cone + wind polygons | `gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC` then `/polygons/getgeometry?eventtype=TC&eventid=&episodeid=` → `Poly_Cones`, `Poly_Red/Orange/Green`, track LineStrings | none |
| **Official PFZ advisory lines** | `incois.gov.in/geoserver/PFZ_Automation/ows` → `PFZ_Automation:pfzlines` (carries `Year`, `Julian_day`) | none |
| PFZ sectors | `incois.gov.in/geoserver/PFZ_Sectors/ows` → `PFZ_Sectors:sector_new` (`SOUTH TAMILNADU`, `SEC006`) | none |
| Landing centres (harbour handoff) | `PFZ_LandingCentres:LandingCenters_29Apr2024` — 541+ named, district + lat/lon | none |
| Ecologically sensitive zones | `incois.gov.in/geoserver/MHW/ows` → `MHW:CORAL_REEF_DISS`, `MHW:SEAGRASS_ZONE_DISS`, `MHW:MANGROVE_ZONE_DISS` | none |
| Harmful algal bloom | `ABIS:HABSectors` (includes a sector `"Gulf of Manmar (GoM)"`) | none |
| **Waves — authoritative model** | `incois.gov.in/thredds/dodsC/osf/wave/WAVES_coast_YYYYMMDD.nc` — 0.1° ≈ 11 km, 65–95°E / 5–25°N, 56 × 3 h = 7 days; vars `SWH, SWELL, WP, SWP, SWHX/Y, SWELLX/Y`; MWW3 forced by ECMWF **with data assimilation** | none |
| Maximum wave height | `osf/mwh/MWH_coast_YYYYMMDD.nc` → `MAXW` | none |
| Currents / winds / SST / chlorophyll | `osf/currents/`, `osf/winds/`, `osf/sst/`, `osf/chl/` (VIIRS 4 km **3-day rolling composite** — cloud gaps already handled) | none |
| Catalogue for the above | `incois.gov.in/thredds/catalog/osf/<product>/catalog.xml` | none |
| Subsurface T/S, 2004→present | `erddap.incois.gov.in` → `incois_argo_10d_VAM` (current to 2026-07-30) | none |
| Tide, currents, cross-check waves | `marine-api.open-meteo.com/v1/marine` — `sea_level_height_msl`, `ocean_current_velocity/direction`, `wave_height`, `wind_wave_height`, `swell_wave_height`, `wave_period` | none |
| Wind / gusts / CAPE / visibility | `api.open-meteo.com/v1/forecast` | none |
| **IMBL — 4 treaty segments** | `geo.vliz.be/geoserver/MarineRegions/wfs` → `MarineRegions:eez_boundaries`, `CQL_FILTER=line_name LIKE '%Sri Lanka%'` | none |
| Bathymetry | `PFZ_Bathymetry:bathymetry`, `BathymteryImage:gebcobathymtery`, GEBCO WMS | none |
| ISRO basemap | `bhuvan-vec1.nrsc.gov.in/bhuvan/wms` | none |

The four IMBL segments, each carrying its treaty name and date as attributes:

| line_id | Treaty | Extent | Note |
|---|---|---|---|
| **1306** | 1974-06-28 — Boundary in **Historic Waters** | 9.10–10.08°N | **Palk Bay — the Rameswaram line** |
| 1307 | 1976-03-23 — Gulf of Mannar & Bay of Bengal | 5.00–9.10°N | |
| 1310 | 1976-03-23 | 10.08–11.44°N | |
| 1311 | 1976-11-22 supplementary | 4.79–5.00°N | |

1306 is a **historic-waters** boundary — a different legal regime from the 1976 maritime
boundary. Keep them as distinct geofence classes; do not merge.

**Do not:** call MOSDAC from inside an agent turn; let the LLM emit a number without a
provenance record; present derived PFZ as the official INCOIS advisory; collapse geofence
classes; hardcode region specifics; relax the ceiling to make a demo pass.

---

## Locked decisions

| Question | Decision |
|---|---|
| PFZ sourcing | Official INCOIS `pfzlines` primary; own chlorophyll + SST-front derivation as visible cross-check |
| ISRO EO ingestion | MOSDAC registered; pipeline built source-agnostic against INCOIS. ISRO granules are an upgrade, not a dependency |
| Connectivity | Design for offline, demo online. Geofence polygons + decision envelope cached client-side; geofence fires from GNSS with no signal |
| Milestone | Internal hackathon ~8 Sept — live demo **and** PPT |
| Orchestration | Hand-rolled planner → specialists → synthesis over Anthropic tool use. **Not LangChain/LangGraph** — full control of the stored trace, fewer unknowns, and it differentiates from the field |
| Persistence | PostGIS for vectors (one docker-compose service). Grids in xarray/NetCDF on disk. Routing and thresholding in numpy — not in the database |

---

## Architecture

```
  Boat UI (Tamil, voice-first)          Shore console (English, fleet view)
                \                                    /
                 \__________  presentation  ________/
                                  |
                    Agent orchestration  (planner → specialists → synthesis)
                                  |
                    Tool layer  (typed, deterministic, provenance-emitting)
                                  |
        ┌─────────────────────────┴─────────────────────────┐
   Local store                                        Source adapters
   PostGIS vectors · NetCDF grids · trace store        IMD · INCOIS · Open-Meteo · GDACS · VLIZ
                                  ▲
                    Scheduled ingestion + snapshot cache
```

Two paths, both mandatory:
- **Request path** — user asks, agents answer.
- **Push path** — background loop over tracked vessel positions firing proactive hazard and
  geofence-approach alerts. PS bullets 7 and 8. Build it early; it is what most teams miss.

### Repo layout

```
foreshore/
  docker-compose.yml               # postgis only
  config/
    regions/palk_bay_gom.yaml      # demo region
    regions/gujarat_sir_creek.yaml # region-swap proof
    vessels.yaml                   # vessel class thresholds
  data/
    static/                        # committed GeoJSON (boundaries, MPA, eco zones)
    cache/                         # gitignored live snapshots
    fixtures/                      # committed frozen demo snapshot
  backend/foreshore/
    config.py  models.py           # Provenance, Observation, Verdict, Alert, TraceStep
    sources/  base.py imd_bulletin.py imd_geoserver.py incois_wfs.py
              incois_thredds.py incois_erddap.py openmeteo.py gdacs.py marine_regions.py
    store/    vectors.py grids.py traces.py cache.py
    tools/    registry.py + one module per tool
    verdict/  douglas.py ceiling.py engine.py
    routing/  costfield.py astar.py
    geofence/ classes.py engine.py
    push/     loop.py vessels.py alerts.py
    agents/   planner.py specialists.py synthesis.py runtime.py
    api/      main.py routes_query.py routes_fleet.py routes_trace.py ws.py
  frontend/src/
    boat/  console/  shared/
  scripts/
    fetch_static.py ingest.py healthcheck.py freeze_fixtures.py simulate_fleet.py
```

### Core contracts — define these first, everything else depends on them

```python
# models.py
Freshness = Literal["live", "recent", "stale", "expired"]

@dataclass(frozen=True)
class Provenance:
    source_id: str              # "imd_coastal_bulletin"
    source_name: str            # "IMD ACWC Chennai Coastal Weather Bulletin"
    authority: str              # IMD | INCOIS | ECMWF/Open-Meteo | JRC/GDACS | VLIZ | derived
    url: str
    acquired_at: datetime       # when WE fetched it
    issued_at: datetime | None  # when the source issued it
    valid_from: datetime | None
    valid_to: datetime | None
    spatial_resolution_m: float | None
    freshness: Freshness
    is_derived: bool = False    # True => never label as an official advisory

@dataclass(frozen=True)
class Observation:
    variable: str               # "significant_wave_height"
    value: float | str
    unit: str
    lat: float; lon: float
    valid_time: datetime
    provenance: Provenance

@dataclass
class Verdict:
    level: Literal["GO", "GO_WITH_CAUTION", "DO_NOT_ADVISE"]
    reasons: list[str]
    evidence: list[Observation]
    ceiling_applied: bool
    ceiling_source: Provenance | None
    downgraded_from: str | None      # set when the post-check bit
    handoff: Handoff | None          # required when DO_NOT_ADVISE
```

**Invariant, enforced in code not prompt:** every `Observation` carries a `Provenance`. The
synthesis agent may only cite values present in the evidence list. A response containing a
number with no matching `Observation` fails a unit test.

---

## Model delegation policy

**Belongs in `CLAUDE.md` — copy it there before implementation starts.** Recorded here so the
plan is self-contained.

**Opus decides. Sonnet writes.**

### Opus (main thread) owns
- Architecture and design decisions; anything with a trade-off
- **Contract definition** — `models.py`, tool signatures, config schemas, API shapes. Everything
  downstream depends on these, so they are written once, carefully, by the model holding the
  full context
- **Safety-critical logic** — `verdict/douglas.py`, `verdict/ceiling.py`, the abstention path.
  These encode the invariants the whole submission rests on; they are not delegated
- Reviewing every subagent diff before it counts as done
- Phase sequencing, scope cuts, the demo script, the deck

### Sonnet subagents own
Everything else. Implementation against a contract Opus has already fixed.

Natural units of delegation — one subagent each:
- a single source adapter (`sources/imd_bulletin.py`, `sources/gdacs.py`, …)
- a single tool module
- a single UI route or component
- `astar.py`, `costfield.py`, the vessel simulator, the healthcheck script
- tests for an already-specified module

### Mechanics
```
Agent(subagent_type: "general-purpose", model: "sonnet", prompt: <brief>)
```
For surgical 1–2 file edits, `caveman:cavecrew-builder` is cheaper.

Every subagent brief must carry, verbatim:
1. exact file path(s) to create or modify
2. the contract — dataclasses, function signatures, return types
3. the acceptance test it must satisfy
4. the standing constraints: every value carries a `Provenance`; no region specifics in
   application logic; browser `User-Agent` + `Referer` on every INCOIS/IMD GeoServer call;
   `FORESHORE_MODE` respected

**Batch independent subagents in parallel** — separate source adapters, separate tools, separate
UI components have no interdependency and should be dispatched in one message, not serially.

### Opus writes code only when
- defining the core contracts everything else builds against
- the logic is safety-critical (ceiling, verdict, abstention, geofence classing)
- a subagent has failed the acceptance test twice
- the change is small enough that briefing costs more than doing it

---

# Execution phases

Each phase lists tasks, the files they touch, an acceptance test, and the **demo/deck artifact**
it must produce. Do not advance until the acceptance test passes.

---

## Phase 0 — Foundations · tonight, 30 Aug · ~2 h

1. **Registrations** (do first, they queue while you build):
   - SIH portal team registration via your college SPOC — *hardest deadline you have*
   - IMD: `api.imd.gov.in/public/register.php` (7 fields, no documents)
   - MOSDAC: `mosdac.gov.in/internal/registration` (organisation = your college, no documents)
   - Bhashini/ULCA: `bhashini.gov.in/ulca/user/register`
   - Anthropic API key
2. Repo init, `uv`/`poetry` env, `docker-compose up` PostGIS with PostGIS extension enabled.
3. Write `models.py` in full — every dataclass above. Nothing else starts until this exists.
4. Write `config.py` + `config/regions/palk_bay_gom.yaml`:
   ```yaml
   region_id: palk_bay_gom
   bbox: [78.0, 8.0, 80.6, 10.9]
   anchor_ports: [Rameswaram, Nagapattinam, Tuticorin]
   primary_language: ta
   fallback_language: en
   imd_coastal_office_id: 6          # ACWC Chennai
   incois_pfz_sector: SEC006         # SOUTH TAMILNADU
   ```
   **No coordinate, boundary name or language code may appear anywhere in application logic.**
   A judge will ask "does this only work for Tamil Nadu?" — the answer is a live file swap.
5. `store/cache.py` — every source fetch writes `{payload, fetched_at, url}` to
   `data/cache/<source_id>/<iso8601>.json`. Add `FORESHORE_MODE=live|fixture` from day one.
   In `fixture` mode all adapters replay from `data/fixtures/`. **This is what makes the live
   demo immune to venue wifi.** Retrofitting it later costs a day.

**Acceptance:** `python -c "from foreshore.config import load; print(load('palk_bay_gom'))"`
prints the config; PostGIS accepts a connection.

---

## Phase 1 — Data spine · 31 Aug · full day

Goal: every source in the verified table returns typed `Observation`s with real `Provenance`,
in both live and fixture mode.

1. `sources/base.py` — `Source` protocol: `fetch() -> raw`, `parse(raw) -> list[Observation]`,
   `provenance(raw) -> Provenance`. A shared `httpx` client that **always** sends
   `User-Agent: Mozilla/5.0 …Chrome/126…` and a plausible `Referer`. Retry + cache-on-success.
2. `sources/imd_bulletin.py` — fetch `coastal_bulletin_new.php?id={office_id}`, parse the
   `South Tamilnadu coast` block into `Wind, Weather, Visibility, SeaCondition, PortSignal,
   StormSurgeTidalWarning, TimeOfIssue`. Bulletin `Validity` is **12 h** — set `valid_to`
   accordingly; this is a hard bound on ceiling freshness.
3. `sources/imd_geoserver.py` — WFS GeoJSON for `imd:NowcastWarningDistrict` (filter district
   via CQL), `imd:aws_data_layer`, `imd:Cyclone_Track_V`. Handle 0 features as *valid* — no
   active cyclone is not an error.
4. `sources/incois_wfs.py` — generic WFS client, then named fetchers for `pfzlines`,
   `sector_new`, `LandingCenters_29Apr2024`, `CORAL_REEF_DISS`, `SEAGRASS_ZONE_DISS`,
   `MANGROVE_ZONE_DISS`, `HABSectors`. Bbox-filter to region.
5. `sources/incois_thredds.py` — resolve latest available date from `catalog.xml` (do not assume
   today; observed lag is ~2 days), then OPeNDAP-subset to bbox via `xarray.open_dataset` on the
   `dodsC` URL. Products: `wave` (SWH/SWELL/WP/SWP), `mwh` (MAXW), `currents`, `winds`, `sst`,
   `chl`. Cache the subset as local NetCDF.
6. `sources/openmeteo.py`, `sources/gdacs.py`, `sources/marine_regions.py`,
   `sources/incois_erddap.py`.
7. `scripts/fetch_static.py` — one-shot pull of everything that does not change; write to
   `data/static/` and **commit it**: 4 IMBL segments, coral/seagrass/mangrove, PFZ sectors,
   landing centres, GEBCO bathymetry subset, coastline/land mask.
8. `scripts/healthcheck.py` — hits every source, prints a table: source, HTTP, latency, feature
   or grid count, `issued_at`, freshness. **Run this every morning.**

**Acceptance:** `healthcheck.py` shows all-green live; `FORESHORE_MODE=fixture healthcheck.py`
all-green with the network off.

**Artifact for the deck:** the healthcheck table screenshot. It is the single most credible
image in the submission — it proves you touched the data. Goes on *Feasibility and Viability*.

---

## Phase 2 — Tool layer + verdict engine · 1 Sept · full day

This is the technical core. The LLM selects and sequences tools; it never performs geometry or
arithmetic.

### `verdict/douglas.py` — the mapping your docs never defined

IMD publishes `Sea Condition` as a **descriptor string**, not a number. The ceiling is
unenforceable without a deterministic mapping:

| Descriptor | Douglas | Hs band (m) |
|---|---|---|
| SMOOTH | 2 | 0.10 – 0.50 |
| SLIGHT | 3 | 0.50 – 1.25 |
| MODERATE | 4 | 1.25 – 2.50 |
| ROUGH | 5 | 2.50 – 4.00 |
| VERY ROUGH | 6 | 4.00 – 6.00 |
| HIGH | 7 | 6.00 – 9.00 |

Descriptors arrive compound — `"MODERATE; BECOMING ROUGH IN GUST"`, `"SMOOTH TO SLIGHT"`.
Parse **all** descriptors present and take the **worst** band. Never average.

### `verdict/ceiling.py` — the invariant

Deterministic post-check on the finished verdict object, after the LLM has produced it:

```
worst_band = parse_sea_condition(bulletin)
max_allowed = vessel_profile.max_verdict_for(worst_band)   # from config/vessels.yaml
if verdict.level is more permissive than max_allowed:
    verdict.downgraded_from = verdict.level
    verdict.level = max_allowed
    verdict.ceiling_applied = True
```

Hard overrides that cap independently of sea state:
- `PortSignal != NIL` → cap at `GO_WITH_CAUTION`
- `StormSurgeTidalWarning` naming the user's district → cap at `GO_WITH_CAUTION`, and
  `DO_NOT_ADVISE` if swell period ≥ 15 s (long-period swell in a shallow bay is the
  kallakkadal signature)
- bulletin older than its 12 h validity → `DO_NOT_ADVISE` (stale ceiling cannot authorise)
- any required input missing → `DO_NOT_ADVISE` with handoff

`DO_NOT_ADVISE` is a designed outcome, never an error state, and must hand off to a **named**
authority — nearest landing centre from `PFZ_LandingCentres`, plus Coast Guard 1554.

For a 0–50 nm small motorised boat the defaults are: `GO` only up to SLIGHT,
`GO_WITH_CAUTION` up to MODERATE, `DO_NOT_ADVISE` at ROUGH and above. Put these in
`config/vessels.yaml` per vessel class — never in code.

### Tools — all typed, all provenance-emitting

| # | Tool | Returns |
|---|---|---|
| 1 | `get_governing_advisory(lat, lon)` | bulletin fields + parsed Douglas band |
| 2 | `get_sea_state(lat, lon, t)` | **all three sources side by side**, each with provenance and resolution |
| 3 | `get_weather(lat, lon, t)` | wind, gusts, precip, CAPE, visibility |
| 4 | `get_lightning_nowcast(district)` | IMD nowcast categories, `toi`/`vupto` |
| 5 | `get_tide(lat, lon, window)` | sea level series + next high/low |
| 6 | `get_currents(lat, lon, t)` | speed, direction |
| 7 | `find_nearest_pfz(lat, lon)` | **official** INCOIS line, distance, bearing, advisory date |
| 8 | `derive_pfz_zones(bbox, t)` | chl + SST-front thresholding → polygons, `is_derived=True` |
| 9 | `check_geofences(lat, lon, heading, speed)` | per-class distance, bearing, ETA to breach |
| 10 | `get_exclusion_zones(t)` | cyclone polygons, high-wave cells, MPA, IMBL buffer |
| 11 | `plan_route(origin, dest, departure, vessel)` | waypoints, distance, ETA, cost breakdown |
| 12 | `get_hazard_alerts(bbox)` | GDACS + IMD warnings |
| 13 | `get_productivity_history(bbox, years)` | chl/SST/Argo series + trend statistics |
| 14 | `nearest_harbour(lat, lon)` | named landing centre + distance |
| 15 | `evaluate_verdict(evidence)` | `Verdict` with ceiling applied |

Tool 2 is the demo's centrepiece. It must return all three readings **unreconciled**, and the
synthesis layer must explain which governs. Do not average them.

**Acceptance:** `pytest` — each tool returns typed output with populated provenance; ceiling
unit tests cover every descriptor and every override; a test asserts that no tool can return a
bare float without provenance.

**Artifact:** a printed evidence panel for Rameswaram showing IMD `MODERATE; BECOMING ROUGH IN
GUST` vs INCOIS `SWH 0.594 m` vs Open-Meteo `1.18 m`, verdict downgraded by the ceiling. This
is your *Technical Approach* slide.

---

## Phase 3 — Agent core + reasoning traces · 2 Sept · full day

Mirror the PS's own vocabulary — these are the words judges will read on your architecture
slide: planning, marine data discovery, weather intelligence, ocean analytics, geospatial
reasoning, risk assessment, visualization, reporting, user interaction.

1. `agents/runtime.py` — Anthropic tool-use loop: submit tool schemas, execute calls, feed
   results back, stop on final answer. ~200 lines. Every call appends a `TraceStep`.
2. `agents/planner.py` — decomposes intent into an ordered plan of
   `{specialist, tool, args, why}`. The `why` string is displayed in the UI — it is the visible
   evidence of autonomous planning.
3. `agents/specialists.py` — each specialist is the same runtime with a **restricted tool
   subset**: WeatherIntel {3,4,12}, OceanAnalytics {2,5,6,8,13}, GeospatialReasoning {7,9,10,14},
   RiskAssessment {1,15}, Routing {11}. Restriction is what makes collaboration real rather
   than cosmetic.
4. `agents/synthesis.py` — composes the answer **in the detected language**, attaches the
   evidence panel, applies the ceiling post-check last.
5. `store/traces.py` — persist every step: `{step_id, parent, agent, tool, args,
   result_digest, provenance_ids, duration_ms, ts}`. Retrievable by `query_id`.
6. Language detection on the inbound text/ASR transcript; respond in kind. Auto-detect and
   mirror — **never a language dropdown** (PS bullet 2 is explicit).

**Acceptance:** "நாளை காலை கடலுக்கு போகலாமா?" returns a Tamil answer with ≥4 evidence
entries and a retrievable trace of ≥6 steps across ≥3 specialists.

**Artifact:** the trace inspector screenshot. Answers "are those real agents or five boxes on a
slide?"

---

## Phase 4 — Router, geofence, push loop · 3 Sept · full day

### `routing/costfield.py` + `astar.py`

Grid at 0.01° (~1.1 km) over the region bbox. Per-cell cost:

```
cost = w_base
     + w_hs      * (Hs / hs_max)^2
     + w_wind    * (wind / wind_max)^2
     + w_current * adverse_current_component
     + w_shallow * shallow_penalty(depth)          # GEBCO
     + w_steep   * steepness_penalty(Hs, period)   # short-period steep sea is the real danger
     + w_imbl    * proximity_penalty(dist_to_IMBL) # soft, rises near the line
     = INF  if land, inside IMBL, or inside an exclusion polygon
```

A* with 8-connectivity and a haversine heuristic scaled by minimum cell cost. Weights live in
config and are documented. **Never LLM-generated waypoints** — a fake router is instantly
visible to an ISRO judge.

Return not just the path but the **cost breakdown per leg**, so the UI can say *why* the route
bulges — that is what makes it legible as real optimisation.

### `geofence/classes.py` — five classes, semantically distinct

| Class | Source | Severity | Warn / critical | Copy |
|---|---|---|---|---|
| `IMBL_HISTORIC_WATERS` | line 1306 (1974) | legal, hard | 2.0 / 0.5 nm | "You are approaching the 1974 India–Sri Lanka historic waters boundary. Crossing risks detention." |
| `IMBL_MARITIME_BOUNDARY` | lines 1307/1310/1311 (1976) | legal, hard | 2.0 / 0.5 nm | maritime boundary wording |
| `MPA` | Gulf of Mannar National Park | restricted | 1.0 / 0.25 nm | conservation restrictions, not a legal border |
| `ECO_SENSITIVE` | coral / seagrass / mangrove | advisory | 0.5 nm | avoid anchoring and trawling |
| `USER_DEFINED` | user-drawn | configurable | configurable | PS: *"other predefined operational boundaries"* |

Plus dynamic `HAZARD_EXCLUSION` from cyclone polygons and high-wave cells.

Distinct copy, distinct lead distances, distinct severity, in Tamil and English. **Do not
collapse these into one "restricted zone" type** — the distinction is the point.

### `push/loop.py` — the path most teams will not build

Every 60 s (5 s in demo mode), for each tracked vessel: project position forward on current
heading and speed, compute distance and ETA to each geofence, sample hazard cells along the
projection, emit alerts with dedupe and acknowledgement state. Push over WebSocket.

`push/vessels.py` — simulator: 8 boats on scripted tracks out of Rameswaram and Nagapattinam,
**one deliberately closing on the 1974 line**, one heading into a high-wave cell. Label
simulated positions as simulated in the UI — there is no public real-time AIS for Indian small
boats and pretending otherwise is the kind of thing that unravels under questioning.

**Acceptance:** a route from Rameswaram to a fishing ground south-east visibly bends around the
IMBL and a shallow reef rather than running straight; the push loop fires a
`IMBL_HISTORIC_WATERS` warning at 2 nm and escalates at 0.5 nm.

**Artifact:** route map with cost breakdown, and the geofence alert. Two of your six slides.

---

## Phase 5 — Boat UI + voice + offline · 4 Sept · full day

React + Vite + TypeScript, MapLibre GL, Bhuvan WMS basemap (ISRO cartography, keyless).

1. Route `/boat`. Big verdict card — green / amber / red — with the one-line reason in Tamil.
2. **Evidence panel** below every answer: source, authority, acquisition time, resolution,
   freshness chip. Nothing is labelled "current" that is not.
3. Voice-first. `shared/voice.ts` with an adapter interface:
   - **Fast path:** browser Web Speech API `ta-IN` — zero setup, works on stage.
   - **Credible path:** Bhashini `dhruva-api.bhashini.gov.in` ASR + TTS.
   Build the interface first, ship Web Speech, add Bhashini when the key lands. Say "Bhashini"
   on the deck only once it is actually wired.
4. Map: vessel position, PFZ lines, geofences by class, route, hazard polygons.
5. **Offline** (locked decision). Service worker + IndexedDB caching geofence polygons, the last
   decision envelope with its validity window, the last route, and pre-rendered TTS phrases.
   `geofence/check` runs **client-side** from `navigator.geolocation` — proximity needs no
   network. Add a visible "No signal" toggle for the demo.

**Acceptance:** speak Tamil into a phone on the same LAN → Tamil spoken answer + map + evidence
panel. Flip "No signal" → geofence alert still fires.

**Artifact:** the phone-in-hand screenshot. This is the emotional centre of the pitch.

---

## Phase 6 — Shore console · 5 Sept · full day

Route `/console`, English, same agent core, different renderer. **This is the disaster-management
artifact** — without it the submission is a consumer app filed in the wrong theme.

1. Fleet map: all simulated vessels, colour-coded by risk, cyclone track and cone overlaid.
2. Alert queue: who is inside a geofence, who has been warned, who has not acknowledged.
3. **Trace inspector**: click any answer → the full reasoning trace, tool by tool, with each
   provenance record.
4. Analyst query box for the hard questions.
5. A visible "same agent core, different renderer" toggle — demonstrable in thirty seconds and
   the strongest architecture claim available.

**Acceptance:** an alert raised by the push loop appears on the console within 5 s of firing on
the boat UI, from the same event.

**Artifact:** console screenshot with cyclone cone + fleet. Your *Impact* slide.

---

## Phase 7 — Diagnostic query, derivation, region swap · 6 Sept · full day

The differentiators. Almost no competing team will attempt the first one.

1. **"Why has fish productivity declined in this region?"** — tool 13. Pull multi-year series:
   chlorophyll trend, SST anomaly, Argo subsurface warming and stratification from
   `incois_argo_10d_VAM` (2004→present), monsoon timing, HAB events from `ABIS`. Compute trends
   offline and cache — the data is multi-year and does not change, so the analysis may be
   precomputed and the agent narrates over stored series. That is honest and cheap.
   This is the query SAMUDRA structurally cannot answer, and it speaks to ISRO's scientific
   mission.
2. **PFZ derivation cross-check** — tool 8. Chlorophyll + SST-front thresholding, rendered
   *beside* the official INCOIS lines. Label derived zones unambiguously as an indicative
   derived product. Both agreeing is a strong beat; both disagreeing is a better one if you can
   explain it.
3. **Region swap** — load `gujarat_sir_creek.yaml` live and show the system re-home. Answers
   "does this only work for Tamil Nadu?" in fifteen seconds.
4. **Scenario exploration** — the PS asks twice for it ("explore scenarios", "explore related
   scenarios") and no competing team will notice. Support "what if I leave at 04:00 instead of
   06:00" as a re-plan over the same evidence with a diffed verdict.

**Acceptance:** the productivity question returns a causal narrative with ≥3 quantified drivers,
each traceable to a provenance record.

---

## Phase 8 — Hardening, demo script, rehearsal · 7 Sept · full day

**Treat this as a real phase. A solo build that skips rehearsal loses the round.**

1. `scripts/freeze_fixtures.py` — freeze a complete snapshot into `data/fixtures/` and commit it.
   Choose a day whose bulletin carries the **swell surge alert for Ramanathapuram** — that is
   your strongest single narrative and you should not depend on the weather reproducing it.
2. Run the entire demo in `FORESHORE_MODE=fixture`, network **off**. Fix everything that breaks.
3. Failure drills: LLM API times out; a source returns 403; GPS unavailable; no active cyclone.
   Every one must degrade to a stated, sensible outcome — usually `DO_NOT_ADVISE` with a handoff.
   Judges probe exactly these.
4. Capture every deck screenshot at final quality.
5. **Rehearse the run three times end to end, timed.**

### Demo script — 7 minutes

| Time | Beat | Proves |
|---|---|---|
| 0:00 | Tamil voice: *"Is it safe to go out tomorrow morning?"* → Tamil spoken answer, amber verdict | bullets 1, 2, 3 |
| 0:45 | Open evidence panel — IMD `MODERATE→ROUGH` vs INCOIS `0.59 m` vs Open-Meteo `1.18 m`; ceiling downgraded the verdict; swell surge alert named | bullets 5, 6, 10 — **the thesis** |
| 1:45 | "Where's the nearest fishing zone?" → official INCOIS PFZ line, dated today, derived zones beside it | bullet 4 |
| 2:30 | "Safest route there" → path bends around reef and IMBL; open the cost breakdown | bullet 9 |
| 3:15 | Boat drifts toward the 1974 line → proactive alert. **Flip "No signal" — alert still fires** | bullets 7, 8 + the offline answer |
| 4:15 | Switch to console: fleet, cyclone cone, alert queue, unacknowledged vessel | Disaster Management framing |
| 5:00 | Open the trace inspector on the first answer — every tool call, every provenance record | bullet 11 |
| 5:45 | "Why has productivity declined here?" → causal narrative | the SAMUDRA wedge |
| 6:30 | Swap region config → system re-homes to Gujarat | "does this only work for TN?" |

Close on the line: **"SAMUDRA tells you what the advisory says. FORESHORE tells you what it
means for your boat, tonight, and why."**

---

## Phase 9 — The PDF · 8 Sept · morning

Six slides maximum including the title, on the unmodifiable SIH template, **uploaded as PDF —
no other format is accepted**.

| Slide | Content |
|---|---|
| 1 Title | `SIH26176 · ORCA` / `FORESHORE — Marine foresight for the small-boat fleet` / theme Disaster Management / team ID |
| 2 Proposed Solution | Two surfaces, one agent core diagram. The SAMUDRA wedge line near the top. The three verdicts and the abstention path |
| 3 Technical Approach | Agent architecture in the PS's own vocabulary; the evidence panel screenshot with the three-source disagreement; A* cost-field formula; named endpoints |
| 4 Feasibility & Viability | **The healthcheck table.** Every source live, keyless, with resolutions and cadence. Risks: IMD/MOSDAC approval latency → keyless fallbacks already working; wave resolution → 11 km INCOIS nest with assimilation; offshore connectivity → offline geofence + GEMINI/GAGAN |
| 5 Impact & Benefits | Console screenshot with cyclone cone and fleet; IMBL detention problem; Ramanathapuram swell surge case; who is protected and how |
| 6 Research & References | 1974 and 1976 treaty citations with URLs; INCOIS OSF/MWW3-ECMWF provenance string; IMD ACWC Chennai; Douglas scale; GEBCO; Marine Regions |

Avoid paragraphs. Points, diagrams, screenshots.

---

## Execution order and parallelism

Strict critical path — Phase N gates Phase N+1:

```
0 Foundations → 1 Data spine → 2 Tools + verdict → 3 Agent core
                                     ↓
                              4 Router + geofence + push
                                     ↓
                          5 Boat UI ──→ 6 Console ──→ 7 Differentiators
                                     ↓
                          8 Hardening + rehearsal → 9 PDF
```

Batch these while long work runs — they have no upstream dependency and should be dispatched in
parallel rather than serially:
- All of `scripts/fetch_static.py` (Phase 1) can run while Phase 2 tool code is being written.
- Frontend scaffolding, Tailwind, MapLibre setup, and the config YAMLs can be generated any time
  after Phase 0.
- Deck screenshots accumulate from Phase 2 onward — capture as you go, never at the end.

**Daily discipline:** start each morning with `scripts/healthcheck.py`. Endpoints move; you want
to discover that at 09:00, not during the demo.

---

## Scope tiers — the cut line if a day is lost

Full scope is the goal. If a day slips, cut from the bottom, never from the top.

- **Tier A — the submission fails without these:** verdict + ceiling + evidence panel; geofence
  with distinct classes + push loop; real router; Tamil voice on at least the safety query; both
  UIs minimally.
- **Tier B:** official PFZ, tide/weather/sea fusion, lightning and cyclone alerts, console fleet
  richness, region swap.
- **Tier C:** productivity diagnostic, PFZ derivation cross-check, offline toggle, scenario
  exploration.

Tier C items are the differentiators, so protect them by finishing A and B early — not by
starting with C.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Venue wifi fails during the demo | `FORESHORE_MODE=fixture` — rehearsed with the network off (Phase 8) |
| An endpoint changes or 403s | Every source is a swappable adapter; morning healthcheck; fixtures already frozen |
| IMD / MOSDAC approval never arrives | Keyless fallbacks verified for every needed field; nothing on the critical path depends on them |
| Tamil ASR mangles fishing vocabulary | Domain lexicon biasing + spoken readback confirmation; intent classification robust to transcription error rather than exact-match |
| Lightning source unavailable | IMD GeoServer nowcast is primary. If down: say so and abstain. **Never dress CAPE up as a lightning probability** — Open-Meteo `lightning_potential` is null over India |
| LLM latency makes the demo drag | Pre-warm; cache the demo queries; stream partial traces so the UI is never blank |
| Solo capacity overruns | Cut by tier, never by weakening the ceiling or the abstention path |

---

## Verification

- `pytest backend/tests` — provenance completeness, Douglas mapping across every descriptor,
  ceiling overrides, no-unsourced-numbers assertion, A* admissibility on a known grid.
- `scripts/healthcheck.py` — live and fixture, both all-green.
- End-to-end: run the Phase 8 demo script start to finish with the network disabled.
- Adversarial pass: ask the system a question it cannot answer and confirm it returns
  `DO_NOT_ADVISE` with a named handoff rather than guessing.
