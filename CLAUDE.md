# CLAUDE.md — FORESHORE

Operational context for Claude Code. Read this before touching anything.

- Background, rationale, competitive position: `PROJECT_CONTEXT.md`
- Phased implementation plan with contracts, endpoints and demo script: `PLAN.md`

**Last verified against live sources: 2026-08-30.** Every endpoint below was probed directly,
not taken from documentation. Re-run `scripts/healthcheck.py` each morning — operational
endpoints move.

---

## What this is

**FORESHORE** — an agentic marine intelligence platform for small-boat fishermen on the
Palk Bay / Gulf of Mannar coast, plus a shore-side control console for fisheries and
Coast Guard operators.

Built for **Smart India Hackathon PS SIH26176 ("ORCA")**, submitted by ISRO / Department
of Space, filed under **Disaster Management**.

The disaster-management framing is not decoration. Safety, alerting, and hazard avoidance
outrank conversational polish in every design tradeoff. When in doubt, favour the safety path.

### Deadlines

| Date | Deliverable |
|---|---|
| ~8 Sept 2026 | Internal college round — **live demo + PPT** |
| 30 Sept 2026 | SIH portal — **6-slide PDF only**, no demo, reviewed by the PS owner |
| Oct / Nov 2026 | Screening, then finale shortlist |
| Dec 2026 | Grand Finale, 36 hours |

The 30 Sept artifact is judged with no conversation attached. The demo exists partly to
manufacture the screenshots and numbers that PDF needs.

---

## Model delegation policy

**Opus decides. Sonnet writes.**

### Opus (main thread) owns
- Architecture and design decisions; anything with a trade-off
- **Contract definition** — `models.py`, tool signatures, config schemas, API shapes.
  Everything downstream depends on these, so they are written once, carefully, by the model
  holding full context
- **Safety-critical logic** — `verdict/douglas.py`, `verdict/ceiling.py`, the abstention path,
  geofence classing. These encode the invariants the submission rests on; not delegated
- Reviewing every subagent diff before it counts as done
- Phase sequencing, scope cuts, demo script, deck

### Sonnet subagents own
Everything else — implementation against a contract Opus has already fixed. Natural units:
one source adapter, one tool module, one UI route or component, `astar.py`, `costfield.py`,
the vessel simulator, the healthcheck script, tests for an already-specified module.

```
Agent(subagent_type: "general-purpose", model: "sonnet", prompt: <brief>)
```
For surgical 1–2 file edits, `caveman:cavecrew-builder` is cheaper.

Every subagent brief must carry, verbatim:
1. exact file path(s) to create or modify
2. the contract — dataclasses, function signatures, return types
3. the acceptance test it must satisfy
4. the standing constraints listed under "Invariants" below

**Batch independent subagents in parallel.** Separate source adapters, separate tools and
separate UI components have no interdependency — dispatch them in one message, not serially.

### Opus writes code only when
- defining the core contracts everything else builds against
- the logic is safety-critical
- a subagent has failed the acceptance test twice
- the change is small enough that briefing costs more than doing it

---

## Non-negotiable invariants

Enforced in code, not left to model judgment. Do not weaken them to make a demo work.

1. **Advisory ceiling.** FORESHORE never issues a verdict more permissive than the governing
   IMD Coastal Bulletin for the area. It may be *more* cautious. Implement as a deterministic
   post-check on the final verdict object, after the LLM has produced it. If the check trips,
   the verdict is downgraded and the downgrade is logged and shown.

2. **Three verdicts only.** `GO` / `GO_WITH_CAUTION` / `DO_NOT_ADVISE`. `DO_NOT_ADVISE` is a
   designed outcome for missing, stale, or contradictory inputs — not an error state. It must
   hand off to a named human authority (nearest landing centre from `PFZ_LandingCentres`, plus
   Coast Guard 1554), never guess.

3. **No unsourced numbers.** Every quantitative claim traces to a retrieved record with a
   source, an acquisition timestamp, and a spatial resolution. If a value has no provenance
   record, it does not appear in the answer. The LLM never supplies values from its own
   knowledge. A unit test asserts this.

4. **Staleness is surfaced, never hidden.** Every answer carries an evidence panel. Nothing is
   labelled "current" that isn't. The IMD bulletin's own validity is **12 hours** — past that
   it cannot authorise anything.

5. **Geofence classes are semantically distinct.** Five classes, listed below. Do not collapse
   them into one "restricted zone" type.

6. **Region config only.** No coordinate, boundary name or language code in application logic.
   "Does this only work for Tamil Nadu?" must be answered by a live config file swap.

7. **`FORESHORE_MODE=live|fixture`.** Every source adapter respects it. Fixture mode replays
   frozen snapshots from `data/fixtures/`, so a live demo cannot die on venue wifi.

---

## Architecture shape

Two surfaces, one agent core. Agents, tools and reasoning traces are shared; only the renderer
differs. This is the central architectural claim — preserve it.

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
- **Push path** — background loop over tracked vessel positions, firing proactive hazard and
  geofence-approach alerts.

The push path is a hard requirement (PS bullets say *proactive* and *when approaching*). A
request-response-only system fails the problem statement. Build the loop early; it is the thing
most competing teams will miss.

Specialists mirror the PS's own vocabulary — planning, marine data discovery, weather
intelligence, ocean analytics, geospatial reasoning, risk assessment, visualization, reporting,
user interaction. Each gets a **restricted tool subset**; restriction is what makes the
collaboration real rather than cosmetic.

---

## Data sources — verified live

**INCOIS and IMD GeoServer require a browser `User-Agent` and a `Referer` header.** Without
them you get 403. This is the single most common way to lose a day.

### Everything below is keyless

| Purpose | Endpoint |
|---|---|
| **Advisory ceiling** | `mausam.imd.gov.in/Forecast/coastal_bulletin_new.php?id=6` (ACWC Chennai; `id` 1–7 = coastal offices) |
| District nowcast / lightning | `reactjs.imd.gov.in/geoserver/imd/wfs` → `imd:NowcastWarningDistrict` |
| AWS observations | same GeoServer → `imd:aws_data_layer` |
| Cyclone track | same GeoServer → `imd:Cyclone_Track_V` (0 features when no active cyclone — valid, not an error) |
| Cyclone cone + wind polygons | `gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC`, then `/polygons/getgeometry?eventtype=TC&eventid=&episodeid=` → `Poly_Cones`, `Poly_Red/Orange/Green`, track LineStrings |
| **Official PFZ advisory lines** | `incois.gov.in/geoserver/PFZ_Automation/ows` → `PFZ_Automation:pfzlines` (carries `Year`, `Julian_day`) |
| PFZ sectors | `PFZ_Sectors:sector_new` — `SOUTH TAMILNADU` = `SEC006` |
| Landing centres (harbour handoff) | `PFZ_LandingCentres:LandingCenters_29Apr2024` — 541+ named, district + lat/lon |
| Ecologically sensitive zones | `incois.gov.in/geoserver/MHW/ows` → `MHW:CORAL_REEF_DISS`, `MHW:SEAGRASS_ZONE_DISS`, `MHW:MANGROVE_ZONE_DISS` |
| Harmful algal bloom | `ABIS:HABSectors` (includes `"Gulf of Manmar (GoM)"`) |
| **Waves — authoritative model** | `incois.gov.in/thredds/dodsC/osf/wave/WAVES_coast_YYYYMMDD.nc` |
| Maximum wave height | `osf/mwh/MWH_coast_YYYYMMDD.nc` → `MAXW` |
| Currents / winds / SST / chlorophyll | `osf/currents/`, `osf/winds/`, `osf/sst/`, `osf/chl/` |
| THREDDS catalogue | `incois.gov.in/thredds/catalog/osf/<product>/catalog.xml` |
| Subsurface T/S 2004→present | `erddap.incois.gov.in` → `incois_argo_10d_VAM` |
| Tide, currents, cross-check waves | `marine-api.open-meteo.com/v1/marine` — `sea_level_height_msl`, `ocean_current_velocity/direction`, `wave_height`, `wind_wave_height`, `swell_wave_height`, `wave_period` |
| Wind / gusts / CAPE / visibility | `api.open-meteo.com/v1/forecast` |
| **IMBL — 4 treaty segments** | `geo.vliz.be/geoserver/MarineRegions/wfs` → `MarineRegions:eez_boundaries`, `CQL_FILTER=line_name LIKE '%Sri Lanka%'` |
| Bathymetry | `PFZ_Bathymetry:bathymetry`, `BathymteryImage:gebcobathymtery`, GEBCO WMS |
| ISRO basemap | `bhuvan-vec1.nrsc.gov.in/bhuvan/wms` |

### INCOIS OSF coastal wave nest — the authoritative model

```
grid    0.1° ≈ 11 km   (301 × 201; 65–95°E, 5–25°N)
time    56 steps × 3 h = 7 days
vars    SWH, SWELL, WP, SWP, SWHX/Y, SWELLX/Y
source  Mww3 / ECMWF / With_Data_assimilation   (NetCDF history attribute)
lag     ~2 days
```

`osf/chl` is a VIIRS 4 km **3-day rolling composite** — INCOIS has already handled the optical
cloud-gap problem. Do not rebuild it.

### Registration-gated (upside, not dependencies)

- **IMD API** (`api.imd.gov.in`) — Bearer token, **not** IP whitelisting. Registration is 7
  fields, no documents. Gives clean JSON instead of HTML/WFS parsing. Every field it provides is
  already reachable keyless.
- **MOSDAC** — batch downloader, not a live API. Registration is a plain form, no documents.
  Never call it from inside an agent turn. Buys ISRO-product provenance (Oceansat-3 OCM,
  INSAT SST); buys no capability that INCOIS does not already provide.
- **Bhashini** (`dhruva-api.bhashini.gov.in`) — Tamil ASR/TTS. Government of India language
  stack; same alignment argument as ISRO products.

---

## Geofence classes — five, distinct

| Class | Source | Severity | Warn / critical |
|---|---|---|---|
| `IMBL_HISTORIC_WATERS` | line_id **1306**, 1974-06-28 agreement, 9.10–10.08°N — **the Palk Bay / Rameswaram line** | legal, hard | 2.0 / 0.5 nm |
| `IMBL_MARITIME_BOUNDARY` | line_ids 1307 / 1310 / 1311, 1976 agreements | legal, hard | 2.0 / 0.5 nm |
| `MPA` | Gulf of Mannar Marine National Park | restricted | 1.0 / 0.25 nm |
| `ECO_SENSITIVE` | INCOIS `MHW` coral / seagrass / mangrove | advisory | 0.5 nm |
| `USER_DEFINED` | user-drawn — PS: *"other predefined operational boundaries"* | configurable | configurable |

Plus dynamic `HAZARD_EXCLUSION` from cyclone polygons and high-wave cells.

1306 is a **historic-waters** boundary — a different legal regime from the 1976 maritime
boundary. Distinct copy, distinct lead distances, distinct severity, in Tamil and English.

Each Marine Regions segment carries its treaty name and date as attributes, so "where did your
maritime boundary come from?" is answered from the data itself. Digitising treaty coordinate
lists by hand is unnecessary.

---

## Douglas sea-state mapping

IMD publishes `Sea Condition` as a **descriptor string**, not a number. The ceiling is
unenforceable without this mapping:

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

Hard overrides that cap independently of sea state:
- `PortSignal != NIL` → cap at `GO_WITH_CAUTION`
- `StormSurgeTidalWarning` naming the user's district → cap at `GO_WITH_CAUTION`, and
  `DO_NOT_ADVISE` if swell period ≥ 15 s (long-period swell in a shallow bay is the kallakkadal
  signature)
- bulletin older than its 12 h validity → `DO_NOT_ADVISE`
- any required input missing → `DO_NOT_ADVISE` with handoff

Vessel thresholds live in `config/vessels.yaml`, never in code. For a 0–50 nm small motorised
boat: `GO` only up to SLIGHT, `GO_WITH_CAUTION` up to MODERATE, `DO_NOT_ADVISE` at ROUGH+.

---

## Region config

```yaml
region_id: palk_bay_gom
bbox: [78.0, 8.0, 80.6, 10.9]
anchor_ports: [Rameswaram, Nagapattinam, Tuticorin]
primary_language: ta
fallback_language: en
imd_coastal_office_id: 6          # ACWC Chennai
incois_pfz_sector: SEC006         # SOUTH TAMILNADU
```

Keep a second region file (`gujarat_sir_creek.yaml`) working purely to demonstrate the swap.

---

## Conventions

- **Python** for ingestion, geospatial processing, agents. **TypeScript/React** for both UIs.
- Geospatial: PostGIS for vectors (one docker-compose service), NetCDF/xarray for grids.
  Routing and thresholding in numpy — not in the database. Everything EPSG:4326 unless stated.
- Tools are **typed and deterministic**. Spatial operations are real geospatial computation —
  nearest-polygon, raster thresholding, path planning over a cost field. The LLM selects and
  sequences tools; it does not perform the geometry or the arithmetic.
- **Routing uses A\* over a weighted grid** (Hs, wind, currents, steepness, bathymetry,
  exclusion polygons, soft IMBL proximity penalty). Never LLM-generated waypoints. This is a
  credibility tripwire — a fake router is instantly visible to an ISRO judge. Return the
  per-leg cost breakdown so the UI can explain *why* the route bends.
- **Agent orchestration is hand-rolled** over Anthropic tool use — not LangChain/LangGraph.
  Full control of the stored trace, fewer unknowns, and it differentiates from the field.
- Every tool call and result is persisted as a reasoning trace, retrievable and renderable.
  Explainability is a stored artifact, not post-hoc LLM narration.
- Ingestion jobs are idempotent and record granule acquisition time on write.
- Language is **auto-detected and mirrored** — never a dropdown.

---

## Do not

- Do not call MOSDAC synchronously from an agent.
- Do not let the LLM emit a numeric value with no provenance record.
- Do not present derived PFZ zones as the official INCOIS advisory.
- Do not build the request path only — the push/alert loop is a scored requirement.
- Do not collapse the geofence classes.
- Do not hardcode region specifics.
- Do not average disagreeing sources. Show them side by side and say which governs.
- Do not dress CAPE up as a lightning probability. Open-Meteo `lightning_potential` is null over
  India; if the IMD nowcast is unavailable, say so and abstain.
- Do not claim real-time AIS. There is no public feed for Indian small boats — label simulated
  vessel positions as simulated.
- Do not "fix" a failing demo by relaxing the advisory ceiling or the abstention path.
- Do not add features not traceable to a PS capability bullet. Scope creep costs marks.
- Do not skip `User-Agent` / `Referer` on INCOIS and IMD GeoServer calls.

---

## Open unknowns

Most of the original list is resolved. What remains:

1. IMD API key approval turnaround — not blocking, keyless fallbacks verified
2. MOSDAC account approval turnaround — not blocking
3. Tamil ASR accuracy on fishing-domain vocabulary (species names, tide terms, "PFZ").
   Realistic WER 15–20%, worse in domain. Needs mitigation design — lexicon biasing plus spoken
   readback confirmation — not just measurement
4. Routing cost-field weights, confidence bands, geofence lead distances — derive from measured
   cadence (OSF ~2-day lag, chlorophyll 3-day composite, bulletin 12 h validity). Do not invent
5. Offshore connectivity beyond ~10–12 km. Geofence proximity needs no network and runs
   client-side; hazard push does. GEMINI/GAGAN and NavIC messaging are the real-world channel

The name "Foreshore" is not clear in marine software — `Foreshore Technology` sells dredge
monitoring software. None in fisheries advisory, none Indian. Fine for SIH; do not claim the
name is unowned.
