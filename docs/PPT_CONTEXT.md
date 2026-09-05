# FORESHORE — Full Project Context for the PPT

This is a working reference, not a slide draft. It exists so that when you build the deck
yourself, you have every fact, number, table and decision we made in one place and don't
have to re-derive or re-remember any of it. Organized by topic, not by slide — pull from
whatever section a given slide needs.

**Read the "What is NOT built yet" section (near the end) before you write any claim about
the NavIC packet, charts, or multi-turn conversation — those are a written plan
(`PLAN_V2.md`), not shipped code. Everything else in this document describes what actually
exists in the repo today, verified against the source on 2026-09-05.**

---

## 1. Identity, submission, deadlines

- **Product name:** FORESHORE — "Marine foresight for the small-boat fleet."
  - *Fore-* (ahead, in advance — forecast, forewarn, foresight) + *shore*. Also the real
    coastal term for the strip between high and low tide, where small boats launch from.
  - "ORCA" is ISRO's own backronym for the problem statement, not a product name — every
    team on this PS is "working on ORCA." Using ORCA as your identity makes you
    indistinguishable in judges' notes. Do not backronym FORESHORE; the word already means
    something true.
  - Outstanding: no confirmed check that no other maritime software owns "Foreshore" —
    `Foreshore Technology` sells dredge-monitoring software, none in fisheries advisory.
- **Problem statement:** SIH26176, "ORCA — Marine EcOsystem Reasoning with Collaborative
  Agents."
- **Submitting organisation:** Indian Space Research Organisation (ISRO) / Department of
  Space.
- **Category:** Software. **Theme:** Disaster Management (a third-party PS catalogue lists
  it under "Miscellaneous" instead — check the SIH portal directly before finalizing the
  disaster-management framing on the deck).
- **Tagline (use on the title slide):** *"SAMUDRA tells you what the advisory says.
  FORESHORE tells you what it means for your boat, tonight, and why."*

**Deadlines:**

| Date | Deliverable |
|---|---|
| ~8 Sept 2026 | Internal college round — live demo + PPT |
| 30 Sept 2026 | SIH portal — 6-slide PDF only, no demo, reviewed by the PS owner (ISRO) |
| Oct/Nov 2026 | Screening, then finale shortlist |
| Dec 2026 | Grand Finale, 36 hours |

The 30 Sept artifact is judged with no conversation attached — the demo you're building the
PPT around exists partly to manufacture the screenshots and numbers that PDF needs.

---

## 2. The problem, and why FORESHORE isn't redundant with SAMUDRA

**The incumbent:** INCOIS's SAMUDRA app (launched Aug 2023, SAMUDRA 2.0 announced) already
broadcasts PFZ advisories and Ocean State Forecasts in eight coastal languages, with
official INCOIS authority FORESHORE does not have. If your pitch is "multilingual app
giving fishermen PFZ and safety alerts," a judge from ISRO/MoES will ask why this exists —
and there's no good answer to that framing. **Assume the question is coming; address it
head-on on slide 1 or 2.**

**The wedge — what SAMUDRA structurally cannot do, because it's a delivery channel for
precomputed advisories, not a reasoning system:**
1. Correlate multiple conflicting sources to answer an arbitrary natural-language question.
2. Evaluate what a reading means for a *specific hull type* (1.5 m waves: safe for a
   trawler, potentially lethal for a small fibreglass vallam).
3. Compute an optimized route around moving storm cells and shallow coral heads.
4. Answer a diagnostic scientific question ("why has productivity declined here?").
5. Explain *why* a recommendation was made, or safely refuse when data is stale/missing.

Every one of those is explicitly in the PS's "Expected Solution." **The PS is ISRO asking
for the reasoning layer above what SAMUDRA already publishes.** This is the single most
important framing sentence for the deck.

**Reality-at-sea points that justify specific design choices (good for the impact slide):**
- **IMBL dilemma:** the India–Sri Lanka boundary in Palk Bay is only 12–15 nm from
  Rameswaram. Drifting across during engine trouble or night trawling risks arrest and
  boat seizure — 500+ fishermen detained in recent years (per PROJECT_CONTEXT), and more
  specific sourced figures below in §14 (Part 2 research). A line on a chart isn't enough;
  fishermen need predictive ETA warnings that interrupt attention.
- **Offshore connectivity cliff:** cellular/VHF/4G die at 10–15 km offshore. FORESHORE is
  offline-first: geofences and cached routes live in IndexedDB; proximity alerts fire
  client-side from GNSS with zero network.
- **Wet hands, voice modality:** typing coordinates at 3 AM on a rolling deck is
  unrealistic. FORESHORE is voice-first, Tamil in and out.

**Why Palk Bay / Gulf of Mannar specifically (highest-conviction regional call):**
IMBL salience (a live national problem, not a theoretical polygon) + adjacent Gulf of
Mannar Marine National Park (a second, legally distinct geofence class in the same demo
frame) + Bay of Bengal cyclone exposure (ties to Disaster Management theme) + best Tamil
ASR/TTS support of any coastal Indian language + genuinely complex shallow/reef bathymetry
(gives the router real work to do). Runners-up considered and rejected: Gujarat/Sir Creek
(weaker language tooling, no adjacent MPA — kept as the region-swap proof, not the primary
demo region); Kerala (no international boundary — guts the geofencing story).

**Why the shore console exists (not just a fisherman app):** the PS is filed under Disaster
Management — a control-room view (fleet vs. an approaching cyclone track, who's in a
geofence, who hasn't acknowledged an alert) *is* the disaster-management artifact. Without
it, the submission reads as a consumer app in the wrong category. It also gives the hardest
analytical queries ("why has productivity declined") a natural home — not a question a
fisherman asks a phone at 4 AM. Cost is low: only the renderer differs; the agent core,
tools and reasoning traces are fully shared between both surfaces.

---

## 3. The five non-negotiable invariants (put these on slide 2, verbatim in spirit)

1. **Advisory ceiling.** FORESHORE never issues a verdict more permissive than the
   governing IMD Coastal Bulletin for that sector. It may be more cautious, never less.
   Enforced as a deterministic post-check on the final verdict object in code
   (`verdict/ceiling.py`), not requested in a prompt.
2. **Three verdicts only:** `GO` / `GO_WITH_CAUTION` / `DO_NOT_ADVISE`. `DO_NOT_ADVISE` is
   a *designed outcome* for missing, stale, or contradictory input — not an error state —
   and it must hand off to a named human authority (nearest landing centre, or Coast Guard
   1554), never guess.
3. **No unsourced numbers.** Every quantitative claim traces to a retrieved `Observation`
   with a `Provenance` record (source, acquisition timestamp, spatial resolution). The LLM
   never supplies a value from its own knowledge — enforced under unit test
   (`test_provenance.py`).
4. **Staleness is surfaced, never hidden.** Every answer carries an evidence panel; nothing
   is labelled "current" that isn't. The IMD bulletin's own validity window is 12 hours —
   past that it cannot authorise anything (see Decision D9 in §13 for a subtlety here).
5. **Dual-mode execution.** `FORESHORE_MODE=live` (real keyless public APIs) or
   `FORESHORE_MODE=fixture` (frozen demo snapshot) — every adapter respects it, so a live
   demo cannot die on venue wifi.

Two more that matter architecturally but are less "sloganeable":
- **Geofence classes are semantically distinct** — never collapsed into one "restricted
  zone" type (5 static + 1 dynamic; see §7).
- **Region config only** — no coordinate, boundary name, or language code lives in
  application logic. "Does this only work for Tamil Nadu?" is answered with a live YAML
  swap, not a rebuild.

---

## 4. High-level architecture — "two surfaces, one agent core"

```
  Boat UI (Tamil, voice-first, PWA)          Shore console (English, fleet control room)
                \                                          /
                 \______________ presentation layer ______/
                                     |
                    Agent orchestration (planner -> specialists -> synthesis)
                                     |
                    Tool layer (typed, deterministic, provenance-emitting)
                                     |
        ┌────────────────────────────┴────────────────────────────┐
   Local store                                              Source adapters
   File-backed GeoJSON+STRtree vectors                       IMD · INCOIS · Open-Meteo ·
   NetCDF/xarray grids · JSONL trace store                    GDACS · Marine Regions ·
   (PostGIS/Postgres optional accelerator, never required)     GEBCO/Bhuvan
                                     ▲
                    Scheduled ingestion + snapshot cache (data/cache, data/fixtures)
```

Two mandatory paths:
- **Request path** — user asks (voice or text), agents answer, full trace persisted.
- **Push path** — background daemon scans tracked vessel positions on a tick (60s live /
  5s demo), fires proactive hazard + geofence-approach alerts over WebSocket before anyone
  asks. This is the PS's "proactive" and "when approaching" bullets — a pure
  request-response chatbot fails both, and it's the thing most competing teams will miss.

Both paths run through **the same agent core, same tools, same trace store** — only the
renderer differs by surface. This is the central architectural claim; it's demonstrable
live in under a minute (ask the same question type on `/boat` and `/console`, or watch one
alert arrive on both over the same WebSocket event).

---

## 5. The specialist agents — what's actually implemented vs. what's advertised

**Implemented today (`backend/foreshore/agents/specialists.py`): 8 real `Specialist`
objects**, each an `AgentRuntime` instance with a hard-enforced restricted tool subset (a
specialist that asks for a tool outside its mandate is refused by the runtime — this is
what makes the "collaboration" structural rather than five labelled boxes on a slide):

| Specialist | Role | Tool mandate |
|---|---|---|
| `MarineDataDiscovery` | What data exists, how good is it | `list_available_data` |
| `WeatherIntelligence` | Wind, gusts, visibility, lightning, cyclone warnings | `get_weather`, `get_lightning_nowcast`, `get_hazard_alerts` |
| `OceanAnalytics` | Sea state, tide, currents, productivity, derived PFZ | `get_sea_state`, `get_tide`, `get_currents`, `derive_pfz_zones`, `get_productivity_history` |
| `GeospatialReasoning` | Boundaries, zones, nearest safe harbour | `find_nearest_pfz`, `check_geofences`, `get_exclusion_zones`, `nearest_harbour` |
| `RiskAssessment` | Turn evidence into one of 3 verdicts | `get_governing_advisory`, `evaluate_verdict` |
| `RoutingAgent` | Plan a passage over the weighted cost field | `plan_route`, `get_exclusion_zones` |
| `VisualizationAgent` | Decide what map/panels should show | `check_geofences`, `get_exclusion_zones`, `find_nearest_pfz` |
| `ReportingAgent` | Compose the operator-facing situational report | `get_governing_advisory`, `get_hazard_alerts`, `nearest_harbour` |

**Known, self-documented gap — say this carefully on the slide, don't let a judge find it
first:** `tools/registry.py` declares a `SPECIALISTS` tuple of **10** names (matching the
PS's own "ten cooperating AI agents" framing), including `PlanningAgent` and
`UserInteraction` — but only 8 are defined as real `Specialist` objects with prompts and
tool mandates. `PlanningAgent`'s work is real and load-bearing (`agents/planner.py` does
deterministic intent classification and plan assembly) but it exists only as a trace label,
not a registered specialist; `UserInteraction` (language mirroring, synthesis, the
no-unsourced-numbers audit) is likewise real work done in `agents/language.py` and
`agents/synthesis.py`, but not wrapped as a `Specialist`. **Recommendation for the deck:**
either (a) count 8 specialists honestly and describe planning/synthesis as the
orchestration layer that coordinates them, or (b) close this gap in code before 30 Sept
(it's a `PLAN_V2.md` Phase 10 item, ~1 subagent-afternoon of work) so the "10 specialists"
claim is literally true. Do not claim 10 on a slide while the registry still only wires up
8 — an ISRO reviewer who opens the repo (or asks "list your ten agents") will find this in
seconds.

**Orchestration loop** (`agents/orchestrator.py::answer()`):
1. Planner classifies intent (9 canonical intent classes, keyword-cue based — deliberately
   resilient to Tamil ASR transcription noise, not exact-match dependent) and assembles an
   ordered `PlanStep` list.
2. Deterministic evidence-gathering phase: each planned tool is called, observations +
   provenance land on an evidence bus.
3. Optional specialist reasoning phase (only if `use_model=True` and a model is
   configured): specialists read the evidence bus and can call their own subset tools to
   fill gaps.
4. Verdict engine + advisory ceiling run last, deterministically, regardless of whether a
   model was used at all.
5. Synthesis composes the answer in the matched language, audits for unsourced numbers,
   runs an editorial polish pass that can never move a number/verdict/handoff/language.

**Runs with zero API key.** `agents/runtime.py` provides `ScriptedClient` — a deterministic
fallback that runs the identical tool-use loop and produces the identical verdict, evidence
and trace with plainer prose when no `ANTHROPIC_API_KEY` is set, or if a configured key's
call fails mid-turn. This is Decision D7 (§13) — it removes the LLM from the safety-critical
path entirely: the verdict is computed from `config/vessels.yaml` thresholds over sourced
observations, and the LLM (when present) may only make the answer more cautious, never more
permissive, which is enforced in code, not prompted. `FORESHORE_LLM_PROVIDER` also supports
routing to a free NVIDIA NIM endpoint (`nvidia`) for testing without Anthropic spend —
same runtime loop, same trace, same tool schemas, only the wire format differs.

---

## 6. The 16 deterministic, provenance-emitting tools

Every tool returns a `ToolResult` with typed `Observation` records, each carrying a
`Provenance`. The LLM selects and sequences tools; it never performs geometry or
arithmetic itself.

| # | Tool | Upstream source(s) | Specialist(s) | What it does |
|---|---|---|---|---|
| 1 | `get_governing_advisory` | IMD ACWC Chennai Coastal Bulletin | RiskAssessment, Reporting | Current bulletin: sea condition text, port signal, storm-surge warnings, validity window |
| 2 | `get_sea_state` | IMD bulletin + INCOIS 11 km OSF + Open-Meteo 28 km | OceanAnalytics | Returns **all three** wave/swell estimates unreconciled, each tagged with resolution |
| 3 | `get_weather` | Open-Meteo Forecast, IMD AWS | WeatherIntelligence | Wind speed/gust, precipitation, visibility, CAPE |
| 4 | `get_lightning_nowcast` | IMD GeoServer WFS `NowcastWarningDistrict` | WeatherIntelligence | District-level cloud-to-ground lightning probability bands (<30%, 30–60%, >60%) |
| 5 | `get_tide` | Open-Meteo Marine `sea_level_height_msl` | OceanAnalytics | 24 h tidal height series, high/low water times, current phase |
| 6 | `get_currents` | INCOIS THREDDS `osf/currents`, Open-Meteo | OceanAnalytics | Surface current speed + drift direction |
| 7 | `find_nearest_pfz` | INCOIS WFS `PFZ_Automation:pfzlines` | GeospatialReasoning, Viz | Official INCOIS PFZ advisory line: bearing, distance, Julian day |
| 8 | `derive_pfz_zones` | INCOIS SST frontal gradients (+ chlorophyll where available) | OceanAnalytics | FORESHORE's own indicative PFZ derivation, always tagged `is_derived=True` (see Decision D1) |
| 9 | `check_geofences` | Vector store: Marine Regions, WDPA, INCOIS MHW | GeospatialReasoning, Viz | Multi-class boundary check: distance, bearing, closing ETA along track |
| 10 | `get_exclusion_zones` | GDACS cyclone cones, wave-nest, MPAs | RoutingAgent, Geospatial | Impassable geometries for the router (cyclone cones, gale cells, MPAs, IMBL buffer) |
| 11 | `plan_route` | Cost field (`astar.py`), GEBCO, INCOIS | RoutingAgent | A* optimal passage avoiding hazards; per-leg cost breakdown, detour % |
| 12 | `get_hazard_alerts` | GDACS API, IMD GeoServer cyclone track | WeatherIntelligence, Reporting | Active cyclone episodes: coordinates, intensity, coastal warnings |
| 13 | `get_productivity_history` | INCOIS ERDDAP (Oceansat-2 OCM, Argo T/S) | OceanAnalytics | Multi-year chlorophyll/SST/thermocline trend — the diagnostic query |
| 14 | `nearest_harbour` | INCOIS Landing Centres (541+ ports) + `handoff_contacts.yaml` | GeospatialReasoning, Reporting | Nearest gazetted landing centre, VHF channel, district, contact |
| 15 | `evaluate_verdict` | Evidence bus + `verdict/engine.py` | RiskAssessment | Compiles evidence against vessel limits, applies the advisory ceiling |
| 16 | `list_available_data` | Local ingestion registry + live catalogues | MarineDataDiscovery | System health / data discovery: active sources, latency, resolution, granule age |

---

## 7. Data sources — 8 live adapters, all public, all keyless

| Authority | Endpoint(s) | Resolution / cadence | Notes |
|---|---|---|---|
| **IMD ACWC Chennai** | `mausam.imd.gov.in/Forecast/coastal_bulletin_new.php?id=6` | Regional text, 12 h validity | The advisory ceiling source. `id` 1–7 = the 7 coastal offices |
| **IMD GeoServer** | `reactjs.imd.gov.in/geoserver/imd/wfs` — `NowcastWarningDistrict`, `aws_data_layer`, `Cyclone_Track_V` | District-level, real-time | Rejects `BBOX`+`CQL_FILTER` together (Decision D3) |
| **INCOIS GeoServer** | `incois.gov.in/geoserver/...` — `PFZ_Automation:pfzlines`, `PFZ_LandingCentres`, `MHW:*` (coral/seagrass/mangrove), `ABIS:HABSectors` | 1:150,000, gazetted centres | Official PFZ lines carry `Year`/`Julian_day` |
| **INCOIS THREDDS** | `incois.gov.in/thredds/dodsC/osf/wave/`, `.../currents/`, `.../winds/`, `.../sst/` | 0.1° (~11 km), 3-hourly, 7-day horizon | The authoritative wave model (MWW3/ECMWF, **with data assimilation**); ~1–2 day lag |
| **INCOIS ERDDAP** | `erddap.incois.gov.in` — `incois_argo_10d_VAM`, `incois_oceansat2_datasets`, `IRS_chlorophyll_datasets` | 1° grid / archival series | ISRO Oceansat-2 OCM (2011–2020) + IRS-P4 OCM (2003–2006) + Argo (2004–present) — the productivity diagnostic's real ISRO-instrument provenance |
| **Open-Meteo Marine + Forecast** | `marine-api.open-meteo.com/v1/marine`, `api.open-meteo.com/v1/forecast` | ~28 km global, hourly | Cross-check waves/tide/currents; wind/gust/CAPE/visibility. Free, keyless, CC BY 4.0, ~10,000 calls/day |
| **GDACS / JRC** | `gdacs.org/gdacsapi/...` | Vector polygons, real-time push | Cyclone cone-of-uncertainty + wind-threshold polygons (27/34/50/64 kt); 0 features on a quiet day is a valid non-error result |
| **Marine Regions (VLIZ)** | `geo.vliz.be/geoserver/MarineRegions/wfs` — `eez_boundaries`, filtered `line_name LIKE '%Sri Lanka%'` | Treaty-defined, 1974 & 1976 | Line 1306 = 1974 historic-waters agreement; lines 1307/1310/1311 = 1976 maritime-boundary agreements. Each carries its treaty name + date as a WFS attribute |
| **GEBCO / NRSC Bhuvan** | `wms.gebco.net/mapserv`, `bhuvan-vec1.nrsc.gov.in/bhuvan/wms` | 15 arc-second | GEBCO bathymetry feeds the router; Bhuvan is the ISRO basemap, with GEBCO as a documented live fallback when Bhuvan's tile server returns a `ServiceExceptionReport` (a Postgres auth failure on ISRO's own backend — observed, not hypothetical) |

**Critical operational note for the deck's feasibility slide:** INCOIS and IMD GeoServer
both reject automated clients with `403 Forbidden` unless a browser-like `User-Agent` and
`Referer` header are present. `sources/base.py` handles this on every request. This is
the single most common way a team loses a day probing these endpoints — worth a one-liner
on the risk slide as evidence of real hands-on integration work.

**Registration-gated, not depended on (upside only):**
- `api.imd.gov.in` — Bearer-token REST, 7-field registration, no documents. Every field it
  offers is already reachable keyless via the endpoints above.
- MOSDAC — batch downloader (`mdapi.py`), not a live API; 5,000 files/user/day cap; General
  tier gets Level-2+ NRT / Level-1 with 3-day latency. Never called synchronously from an
  agent turn by design. Buys ISRO-product provenance (Oceansat-3 OCM, INSAT SST) that
  INCOIS's own products don't already cover — genuinely nice-to-have, not load-bearing.
- Bhashini (`dhruva-api.bhashini.gov.in`) — Tamil ASR/TTS, Government of India stack.
  **Currently a stub** in the codebase (no wired backend speech path yet) — the boat UI
  uses the browser's Web Speech API for voice today.

---

## 8. Geofence engine — 6 semantically distinct classes

FORESHORE deliberately does not collapse boundaries into one generic "restricted zone" —
the legal, financial and conservation consequences of crossing each are different, and the
warning copy, thresholds and severity all differ per class. Copy is authored in both
English and Tamil in `config/geofence.yaml`, never hardcoded in application logic.

| Class | Source | Severity | Warn / Critical | Consequence |
|---|---|---|---|---|
| `IMBL_HISTORIC_WATERS` | Marine Regions line 1306, 1974 agreement — the Palk Bay/Rameswaram line | legal, hard | 2.0 nm (~20 min) / 0.5 nm (~5 min) | Arrest, boat seizure by Sri Lankan authorities |
| `IMBL_MARITIME_BOUNDARY` | Marine Regions lines 1307/1310/1311, 1976 agreements | legal, hard | 2.0 nm / 0.5 nm | Crossing into sovereign Sri Lankan EEZ |
| `MPA` | Gulf of Mannar Marine National Park (WDPA — unverified against Protected Planet directly) | restricted | 1.0 nm (~10 min) / 0.25 nm (~2.5 min) | Trawling/anchoring ban — conservation fine, not a border |
| `ECO_SENSITIVE` | INCOIS MHW coral reef / seagrass / mangrove polygons | advisory | 0.5 nm / 0.1 nm | Habitat protection — avoid anchor drop |
| `USER_DEFINED` | Operator/user-drawn (PS: "other predefined operational boundaries") | configurable | configurable | Whatever the operator defines |
| `HAZARD_EXCLUSION` (dynamic) | GDACS cyclone cones, severe storm cells | hazard | 5.0 nm (~45 min) / 2.0 nm (~18 min) | Dangerous sea state — course reversal |

**Engine mechanics worth a technical slide bullet:** ETA-to-boundary is computed by
projecting the vessel's dead-reckoned track forward in **40 discrete steps** and measuring
each against the fence *geometry* (not a single endpoint-distance check) — Decision D6
found the naive version reports "not closing" for a boat that crosses the boundary and
keeps going, because the endpoint of a 1-hour projection can be further away than the
start even while the boat spent the whole hour inside foreign waters. Closure slower than
0.25 kn is suppressed as meridian-convergence noise. The check degrades **per class**, not
wholesale, if one static layer (e.g. INCOIS's occasionally-503ing mangrove layer) is
temporarily unavailable — Decision D8. Runs fully offline client-side via `@turf/turf` for
the boat UI (no network required for a safety-critical proximity check), and server-side
identically for the console.

---

## 9. Douglas sea-state scale mapping (why the ceiling is enforceable)

IMD publishes sea condition as a **descriptor string**, not a number
(`"MODERATE; BECOMING ROUGH IN GUST"`, `"SMOOTH TO SLIGHT"`). `verdict/douglas.py` parses
**all** descriptors present in a compound string and takes the **worst** band — never
averages.

| IMD descriptor | Douglas band | Hs range (m) | Small-boat (`small_motorised`) default cap |
|---|---|---|---|
| SMOOTH | 2 | 0.10–0.50 | GO |
| SLIGHT | 3 | 0.50–1.25 | GO |
| MODERATE | 4 | 1.25–2.50 | GO_WITH_CAUTION |
| ROUGH | 5 | 2.50–4.00 | DO_NOT_ADVISE |
| VERY ROUGH | 6 | 4.00–6.00 | DO_NOT_ADVISE |
| HIGH | 7 | 6.00–9.00 | DO_NOT_ADVISE |
| VERY HIGH | 8 | 9.00–14.00 | DO_NOT_ADVISE |
| PHENOMENAL | 9 | >14.00 | DO_NOT_ADVISE |

Hard overrides that cap independently of sea state (in `verdict/ceiling.py`):
- Port signal ≠ NIL → cap at `GO_WITH_CAUTION`.
- Storm surge / tidal warning naming the user's district → cap at `GO_WITH_CAUTION`, or
  `DO_NOT_ADVISE` if swell period ≥ 15 s (the **kallakkadal** signature — long-period
  swell in a shallow bay).
- Bulletin outside its 12 h validity window, *or* not covering the time being asked about
  → `DO_NOT_ADVISE` (two distinct rules and two distinct messages as of Decision D9 —
  `bulletin_expired` vs. `bulletin_does_not_cover_departure`).
- Any required safety input missing → `DO_NOT_ADVISE` with named handoff.

---

## 10. Vessel classes (`config/vessels.yaml` — never hardcoded)

Three classes, each with its own Douglas-band-to-verdict ceiling table and its own hard
limits (wave height, wind, gust, wave steepness, visibility, long-period swell):

| Class | Range | LOA | Max verdict progression | `hs_go_m` / `hs_caution_m` | `wind_go_kn` / `wind_caution_kn` |
|---|---|---|---|---|---|
| `fibreglass_catamaran` (vallam) | 0–12 nm | 6.0 m | GO to SLIGHT only, DO_NOT_ADVISE ≥ MODERATE | 0.75 / 1.25 | 12 / 18 |
| `small_motorised` (default) | 0–50 nm | 9.0 m | GO to SLIGHT, CAUTION at MODERATE, refuse ≥ ROUGH | 1.25 / 2.5 | 15 / 22 |
| `mechanised_trawler` | 0–200 nm | 15.0 m | GO through MODERATE, CAUTION at ROUGH, refuse ≥ VERY ROUGH | 2.5 / 4.0 | 22 / 30 |

Steepness (Hs / wavelength, wavelength ≈ 1.56·Tp²) is tracked separately because a short,
steep sea is the real small-boat killer, not height alone — a 1.5 m sea at 4 s is more
dangerous than a 2.5 m sea at 12 s. This feeds both the ceiling and the A* router's cost
field.

---

## 11. Routing — real A* over a weighted cost field, not LLM-generated waypoints

`routing/costfield.py` builds a discrete grid (0.01° ≈ 1.1 km) with per-cell cost:

```
cost = w_base + w_hs·(Hs/hs_max)² + w_wind·(wind/wind_max)²
     + w_current·adverse_component + w_shallow·shallow_penalty(depth)
     + w_steep·steepness_penalty(Hs,period) + w_imbl·proximity_penalty(dist)
     = INF   if land / inside IMBL hard buffer / inside an exclusion polygon
```

Weights (`config/routing.yaml`, derived from measured cadence + capsize literature, not
invented): `hs=4.0` (quadratic — wave energy ~H²), `steep=5.0` (dominant for small hulls),
`imbl=6.0` soft penalty ramping inside a 3.0 nm soft buffer with a hard 0.3 nm INF buffer
the router will never shave. `routing/astar.py` runs an 8-connected search with an
admissible haversine heuristic (`h = great-circle distance × minimum cell cost` — never
overestimates, guaranteeing optimality). The cost field is cached per region/vessel/hour
(a perf optimisation — first call in a fresh process pays a several-second one-time build;
warm it before judges are watching, per the demo runbook).

Returns: waypoints, per-leg cost breakdown, `total_distance_nm`/`direct_distance_nm`/
`detour_pct`, ETA, feasibility flag + failure reason if infeasible, and `avoided` — the
named hazards/boundaries the route routed around, which is the literal "why it bends"
explanation surfaced in the UI.

---

## 12. Region configuration — proving "not just Tamil Nadu"

Two fully working region files, zero code differences between them:

- `config/regions/palk_bay_gom.yaml` — **primary demo region.** bbox `[78.0, 8.0, 80.6,
  10.9]`, IMD office id 6 (ACWC Chennai, "South Tamilnadu coast"), INCOIS PFZ sector
  `SEC006`, anchor ports Rameswaram/Nagapattinam/Tuticorin, languages `[en, ta]`.
- `config/regions/gujarat_sir_creek.yaml` — **region-swap proof.** bbox `[68.0, 21.8, 71.5,
  24.2]`, IMD office id 3 (CWC Ahmedabad — and IMD's own bulletin literally spells it
  **"North Gujrath coast"**, not "Gujarat," verified live — Decision D4), INCOIS PFZ
  sector `SEC001`, anchor ports Okha/Porbandar/Jakhau, languages `[en, gu]`.

Both currently ship `primary_language: en` for UI chrome (alert copy, legends) — a
deliberate interim choice (mixed-language chrome was unreadable); query-language
**detection and mirroring** still works per-request regardless (a Tamil query gets a Tamil
answer) because `languages` still lists `ta`/`gu`. Worth stating plainly if asked rather
than implying full bilingual chrome exists today.

Both include a documented, live fallback: ISRO's own Bhuvan WMS tile server currently
returns a `ServiceExceptionReport` (a Postgres auth failure on ISRO's backend) for every
`GetMap` request, so the basemap degrades to GEBCO's bathymetric WMS — named in config,
not hardcoded in a component, and not a hypothetical failure mode.

---

## 13. Decisions and findings made during the build (evidence, in case a judge probes)

These are exactly the "gaps found, then filled" narrative — good material for a
feasibility or technical-approach slide bullet, and good rehearsed answers if a judge asks
"how did you actually verify this."

- **D1 — Chlorophyll basin mismatch.** INCOIS's own OSF `chl` THREDDS feed (`osf/chl`)
  turns out to cover the **Pacific Islands** (~130–215°E), not the Indian Ocean — found by
  direct inspection, not assumed. Response: tool 8 (derived PFZ) uses INCOIS SST frontal
  gradients instead (one of INCOIS's own two operational PFZ signals), adding chlorophyll
  only when available for the date and saying so explicitly when it isn't. Tool 13
  (productivity diagnostic) uses INCOIS ERDDAP's **ISRO Oceansat-2/IRS-P4 OCM** archives
  instead — genuinely better suited (multi-year) and gives real ISRO instrument
  provenance rather than a claimed one.
- **D2 — `osf/mwh` (max wave height) returning all-NaN.** Confirmed upstream outage, not a
  bug. The adapter degrades that one product only; the authoritative `wave` product (SWH,
  SWELL, WP, SWP) stays healthy.
- **D3 — IMD GeoServer rejects `BBOX` + `CQL_FILTER` together**, for any combination.
  Each fetcher now picks exactly one filter strategy.
- **D4 — IMD coastal office ids probed for all 7 offices**, and IMD's own non-standard
  spelling of "Gujrath" captured verbatim — the region-swap demo depends on this.
- **D5 — PostGIS is an accelerator, never a dependency.** The file-backed GeoJSON + shapely
  STRtree vector store is complete on its own; Postgres is used only when
  `FORESHORE_PG_DSN` is set. No demo beat depends on Docker being up.
- **D6 — geofence ETA sampled along the track against the geometry**, not a single
  endpoint-distance check (see §8).
- **D7 — the verdict is correct with zero LLM calls.** Deterministic thresholds compute it;
  the LLM (when present) can only make it more cautious, enforced in code. Removes the API
  from the demo's safety-critical path and makes the safety argument reviewable, not just
  trustable.
- **D8 — geofence checks degrade per-class, not wholesale**, when one static layer (e.g. a
  flaky INCOIS mangrove endpoint) is temporarily down — otherwise a habitat-layer 503 was
  silently masking the ability to check the *legal* IMBL boundary.
- **D9 — a bulletin can't authorise a trip outside its own validity window**, and — subtly
  — the ceiling's stated *reason* for abstaining must be true, not just its verdict. Fixed
  2026-09-05: the ceiling now tracks wall-clock "now" separately from the user's named
  departure time, so it never claims a still-current bulletin has "expired 7 hours ago."
  **This has a direct demo consequence:** the frozen fixture's bulletin validity window
  must actually cover whatever time the rehearsed opening query asks about, or that query
  will (correctly) abstain instead of giving the intended amber verdict.
- **D10 — scenario comparison ("what if I leave at 04:00 instead of 06:00") runs the real
  answer pipeline twice**, independently, never an LLM asked to imagine the difference.
  Fires only when the query names two explicit `HH:MM` times.
- **D11 — two fixture-mode determinism bugs**, both from accidentally keying a cache/fixture
  entry on a value derived from calling the clock twice. One caused `openmeteo_marine` to
  go missing on ~half of otherwise-identical fixture-mode calls; the other silently dropped
  INCOIS's own 11 km wave model from **every** real query in fixture mode (the evidence
  panel's centrepiece source, gone, deterministically, until found). Both fixed by removing
  the clock from the cache key entirely. Good illustration, if asked, of how seriously
  "no unsourced numbers" / "staleness surfaced accurately" were taken as engineering
  constraints, not just a marketing line.

---

## 14. Frontend surfaces

### Boat UI (`/boat`) — fisherman-facing PWA
- Voice-first: tap-to-talk Tamil input via the browser's Web Speech API (Bhashini adapter
  is currently a stub, not wired — see §7), spoken readback on demand.
- Single-glance verdict card: green `GO`, amber `GO_WITH_CAUTION`, red `DO_NOT_ADVISE`.
- Bottom navigation splitting content across **Ask / Map / Evidence** tabs.
- `MapView.tsx` — MapLibre GL map with layers, `RouteSummary.tsx` — per-leg cost display,
  `EvidencePanel.tsx` — the sourced-disagreement panel, `AlertBanner.tsx` — proactive
  alert banner, `ScenarioCompare.tsx` — the two-departure-time diff view,
  `OfflineToggle.tsx` + `useOwnPosition.ts` + `useProximityAlerts.ts` — client-side offline
  GPS geofence checking via `@turf/turf`, independent of network.
- Falls back to the region's first anchor port position (labelled as such) if GPS
  permission is denied.

### Shore console (`/console`) — Coast Guard / Fisheries control room
- `FleetMap.tsx` — MapLibre fleet view, vessel risk-state colour coding, cyclone
  cone/track overlay.
- `AlertQueue.tsx` — live push queue with acknowledgment state.
- `TraceInspector.tsx` — full reasoning-trace tree per query, tool-by-tool, with
  provenance. **Known limitation:** only renders fully for the current session's own
  queries today; historical trace replay needs evidence persisted alongside `TraceStep`
  (a named `PLAN_V2.md` gap, not yet closed).
- `ArchitecturePanel.tsx` — renders `GET /api/architecture`'s specialist list live.
- `AnalystQuery.tsx` — free-text box for deep diagnostic queries (the productivity
  question lives here).
- `RegionSwitcher.tsx` — live region swap. **Known limitation:** the simulated fleet does
  not currently relocate on region swap, so switching to Gujarat still shows Palk Bay
  boat positions — rehearse around this or fix before demo day.

### Landing page (`/`) — pure presentation, no backend calls
Recently rebuilt (2026-09-05 commits) into an immersive single-page design:
- Custom WebGL fragment-shader background (animated noise/lightning effect, hand-written
  GLSL, not a library) with scroll-reactive uniforms.
- Snap-scrolling sections, reveal-mask animations, glass-morphism cards, and (per the
  commit history) a bit-allocation diagram section — presumably previewing the NavIC
  220-bit packet idea from `PLAN_V2.md`. **Verify before presenting:** if this diagram
  depicts the NavIC encoder as functioning, confirm it's clearly labelled conceptual/
  planned, since the encoder itself (`downlink/navic.py`) does not exist in the backend
  yet (see §15).
- Adaptive routing: `App.tsx`'s `HomeRoute` auto-redirects mobile viewports (up to 640px
  wide, per the latest commit) straight to `/boat`, bypassing the landing page, unless the
  visitor explicitly opts in via `?landing=true` or a prior session flag. Desktop visitors
  see the landing page. This means: on a phone, the product opens directly into the tool;
  the marketing/explainer page is a desktop-only front door.
- Deployed to Vercel (`vercel.json` + `frontend/vercel.json`) with SPA rewrite rules so
  client-side routing works on refresh/deep-link.

**Frontend stack:** React 18.3, TypeScript 5.6, Vite 5.4, react-router-dom 6, MapLibre GL
4.7, `@turf/turf` 7 (client-side geospatial), `vite-plugin-pwa` (installable PWA, service
worker present in `frontend/dist`). **No chart library is installed** — see §15, this is a
named, currently-unmet PS bullet (charts).

---

## 15. What is NOT built yet — read this before writing slide claims

`PLAN_V2.md` is an **approved-but-not-started** upgrade plan (its own header says exactly
that) covering Phases 10–13, targeting the 30 Sept PDF. Verified directly against the repo
on 2026-09-05: **none of Phase 10–13 has been implemented.** `git log` shows the most recent
work is landing-page/mobile-routing polish (Sept 5) on top of Phase 0–9, which finished the
demo-ready system described in §§1–14 above. There is no `backend/foreshore/downlink/`
directory, no `envelope.py`, no chart library, no session/conversation store, no AIFS
integration — grepped directly, confirmed absent.

**Do not put these on a slide as shipped work.** They're either (a) genuinely good ideas
worth attempting before 30 Sept if time allows, or (b) worth naming explicitly as roadmap /
future work, which is itself a legitimate and honest slide category. What's actually
missing, from `PLAN_V2.md`'s own PS compliance audit (code-verified, not guessed):

| PS capability | Status | Gap, specifically |
|---|---|---|
| Auto-detect language, Indian regional languages | **Partial** | 8 Indic scripts detected; only `en`/`ta`/`gu` have UI copy. A Malayalam query is detected, then silently answered in English — not stated as a limitation anywhere in the UI |
| Multi-turn contextual conversation | **Not met** | `POST /api/query` is fully stateless — no session id, no history. This is called out as the single largest gap. Scenario comparison (two explicit times) is the only multi-turn-shaped behavior that exists |
| Explainable recommendations via **charts** | **Partial** | Maps: yes. No chart library anywhere in the frontend. The multi-year productivity diagnostic — the PS's own hardest sample query — renders as plain text, not a chart |
| Proactive alerts for weather/**waves**/lightning/cyclones | **Partial** | The push loop triggers on geofence proximity + cyclone-polygon proximity only. A boat sitting still while the sea state deteriorates past its own vessel's limit currently gets no push alert — 3 of 4 named hazard types can't fire one |
| Route optimisation + **operational planning** | **Partial** | Routing itself is real and strong. The system answers "can I go now," never "when can I go," "how long have I got," or "when must I turn back" |
| "Ten cooperating AI agents" (PS's own framing) | **Gap** | 10 named in the registry, 8 actually implemented (§5) |

**The unbuilt "innovation" idea in `PLAN_V2.md` Part 3** (worth understanding even though
it's unbuilt, in case you want to attempt it or just narrate it as vision): compiling the
full agentic verdict down into a **220-bit payload that fits ISRO's own existing NavIC
messaging ICD unchanged** (`ISRO-IRNSS-ICD-MSG-INCOIS-1.2` — Message ID 20/21 already
allocated for PFZ/warnings, 34 spare bits in the current High Wave Alert payload). The
pitch: "reasoning ashore, decision aboard" — the expensive multi-source reasoning happens
on a shore server; what crosses the satellite link to a boat with no signal is a *decision*
(verdict + binding constraint + margin), not raw data. This requires first building a
"decision envelope" (a verdict-over-time series across the INCOIS 7-day horizon, per
vessel class) as the thing that gets bit-packed. **Genuinely differentiated and well
researched — the ICD was read directly from ISRO's own PDF — but zero code exists for it.**
If you want this on the deck, say "proposed" or "designed" honestly, or scope a focused
build before the portal deadline. Never claim a working transmit/encode capability that
isn't demonstrable.

**Self-documented defects, independent of the phase-10 wishlist** (small, worth knowing,
not necessarily slide-worthy):
- Trace inspector only renders full provenance for the current session's own queries.
- Simulated fleet doesn't relocate on region swap (see §14).
- `AlertStore` is in-memory only — a server restart loses the alert queue.
- Bhashini adapter is a stub; no backend speech path (browser Web Speech API is what
  actually runs today).
- `docs/artifacts/` (the screenshot folder the deck references) is currently **empty** —
  no screenshots have been captured yet. This is probably your next concrete to-do before
  drafting slides 3–5, which each call for a specific screenshot (see §17).

---

## 16. Codebase facts (for a feasibility/credibility slide)

- **Backend:** Python, FastAPI, ~18,200 lines across `backend/foreshore/` (agents, api,
  geofence, push, routing, sources, store, tools, verdict — 9 packages).
- **Tests:** 240 tests collected and passing (`pytest backend/tests`), ~4,860 lines of test
  code, run entirely in `FORESHORE_MODE=fixture` (offline, deterministic, no network).
- **Frontend:** TypeScript/React, ~6,400 lines across `frontend/src/` (boat route, console
  route, landing route, shared contract layer).
- **8 live source adapters**, all keyless (§7). **16 provenance-emitting tools** (§6).
  **8 implemented specialist agents** (§5, with a known naming gap vs. the PS's "10").
  **6 geofence classes** (§8). **3 vessel classes** (§10). **2 fully working region
  configs** (§12).
- **19 REST/WS endpoints** total (verified directly against route decorators): `/health`,
  `/api/query`, `/api/route`, `/api/verdict`, `/api/geofence/check`, `/api/pfz/official`,
  `/api/pfz/derived`, `/api/hazards`, `/api/fleet`, `/api/alerts`,
  `/api/alerts/{id}/ack`, `/api/region`, `/api/region/active`, `/api/architecture`,
  `/api/catalogue`, `/api/traces`, `/api/trace/{id}`, `/api/layers`,
  `/api/layers/{id}`, `/api/geofences.geojson`, plus `WS /ws/alerts`. (`docs/API.md`
  documents a useful subset in full request/response detail — read that for wire shapes
  when you need an exact JSON example for a slide.)
- **Data:** 10 committed static GeoJSON layers (`data/static/` — IMBL treaties, MPA,
  coral/seagrass/mangrove, bathymetry, coastline, landing centres, PFZ sectors), 251
  frozen fixture files across all 8 sources (`data/fixtures/`) for network-off demo mode.
- Optional PostGIS acceleration via `docker-compose.yml` — never required for any demo
  beat (Decision D5).

---

## 17. The demo script (7 minutes, timed) — screenshot-producing beats for the deck

Run with `FORESHORE_MODE=fixture`, venue wifi physically disabled, per
`docs/DEMO_SCRIPT.md`. Refreeze fixtures (`scripts/freeze_fixtures.py`) the same day,
because the ceiling's bulletin-validity check is time-sensitive (Decision D9).

| Time | Query / action | Proves | Screenshot for the deck |
|---|---|---|---|
| 0:00 | Tamil voice: *"நாளை காலை கடலுக்குப் போகலாமா?"* ("safe to go out tomorrow morning?") | Tamil voice in/out, amber/red verdict | — |
| 0:45 | Open the evidence panel | 3-source sea-state disagreement, ceiling downgrade if fired | `evidence_panel_disagreement.png` |
| 1:45 | "Where's the nearest fishing zone?" | Official INCOIS PFZ line + derived cross-check, both dated/labelled | — |
| 2:30 | "Safest route to the fishing ground south-east of Rameswaram" | A* route bending around reef/IMBL, cost breakdown | `route_cost_breakdown.png` |
| 3:15 | Vessel closing on the 1974 line; toggle "no signal" | Proactive geofence alert fires; keeps firing offline | — |
| 4:15 | Switch to `/console` | Same alert arrives ~5s later over the same WebSocket — one core, two surfaces | `console_fleet_cyclone.png` |
| 5:00 | Console trace inspector on the 0:00 answer | Full stored reasoning trace, not post-hoc narration | `trace_inspector.png` |
| 5:45 | Console analyst query: "why has fish productivity declined here?" | Multi-year chlorophyll/SST/Argo trend — the query SAMUDRA can't answer | — |
| 6:30 | Console region switcher → Sir Creek/Gulf of Kutch | Live re-home from one YAML file | `region_swap.png` |
| — | (also capture) | `scripts/healthcheck.py` all-green live output | `healthcheck_table.png` |

Bonus beat if time allows: "What if I leave at 04:00 instead of 06:00?" — two independent
full verdicts, plain-language diff, never an LLM guessing at the difference.

**All 6 target screenshots above are currently missing from `docs/artifacts/`** — capturing
them is the concrete next step before slide layout.

---

## 18. The official 6-slide structure (already drafted in `docs/DECK_CONTENT.md`)

The SIH portal deck template is fixed at 6 slides including title, PDF only. Content
already drafted per slide (use this as your outline, not a rewrite target — you're
building the visual deck from this):

1. **Title** — SIH26176 · ORCA, FORESHORE name + tagline, Disaster Management, ISRO/DoS.
2. **Proposed solution** — two-surfaces-one-core diagram, three verdicts, refusal as a
   designed outcome, the advisory ceiling, "what makes this not a chatbot wrapper."
3. **Technical approach** — 5+ specialist agents with restricted tool subsets (cite the
   real count from §5), the evidence-panel disagreement screenshot, the A* cost formula,
   named live endpoints (not "we will use satellite data").
4. **Feasibility & viability** — the healthcheck screenshot, a named risk/mitigation table
   (§7's data-source risks, ASR error rate, venue-wifi fixture mode), region-agnostic proof.
5. **Impact & benefits** — console screenshot, the IMBL detention problem named
   specifically with the two distinct treaty-boundary classes, the push path, the
   productivity diagnostic, three vessel classes protected.
6. **Research & references** — treaty citations (1974/1976, line numbers), INCOIS OSF
   methodology (MWW3/ECMWF with assimilation), IMD bulletin + Douglas scale, GEBCO/Bhuvan,
   GDACS, ISRO Oceansat-2/IRS-P4 OCM provenance.

If you choose to adopt the `PLAN_V2.md` "reasoning ashore, decision aboard" framing (§15),
that plan proposes reshaping slides 1–5 around it — but only do that if you actually build
enough of Phase 10–11 first that the claim is true; otherwise keep the structure above,
which describes exactly what's shipped.

---

## 19. Anticipated judge questions — rehearsed answers

| Question | Answer |
|---|---|
| Why does this exist when SAMUDRA already does this? | SAMUDRA delivers precomputed advisories; FORESHORE reasons across sources, plans routes, answers diagnostic questions, and shows its evidence — all things SAMUDRA structurally cannot do (§2) |
| Where did that number come from and when was it measured? | Evidence panel on every answer: source, acquisition time, resolution, freshness (§3, invariant 3–4) |
| What if you say it's safe and someone dies? | Advisory ceiling invariant + `DO_NOT_ADVISE` abstention with a named human handoff — architecture, not a disclaimer (§3, invariant 1–2) |
| Does this only work for Tamil Nadu? | Live config-file swap, demonstrated on stage against Gujarat/Sir Creek (§12) |
| How accurate is your wave forecast in a shallow bay? | We know the global model is ~28 km and coarse here — that's exactly why the IMD human-issued coastal bulletin, not the model, is the governing ceiling source (§7, §9) |
| Are those real agents, or five boxes on a slide? | Tool-subset restriction is enforced by the runtime, not suggested by a prompt; every tool call and result is a stored, retrievable trace (§5, §6) — but be ready to give the honest count of 8, not 10 (§5) |
| Is your routing real? | A* over a weighted cost field including cyclone polygons, bathymetry, current, and boundary proximity — not LLM-generated waypoints (§11) |
| Where did your maritime boundary come from? | The 1974 and 1976 India–Sri Lanka agreements, via Marine Regions, each segment carrying its own treaty name and date as a data attribute (§8, §13 D4) |
| List your ten agents | Be honest: 8 are implemented with tool mandates; planning and synthesis are real orchestration work not yet wrapped as named `Specialist` objects (§5) — or close this gap in code before the question can be asked |
| What happens when a source is down / unreachable? | Degrades to `DO_NOT_ADVISE` with a named handoff — never a stack trace, never a guess (see the failure drills in `docs/DEMO_SCRIPT.md`) |

---

## 20. Quick reference — commands to reproduce any number or screenshot above

```bash
# Verify every live source is still reachable (the healthcheck screenshot)
FORESHORE_MODE=live python scripts/healthcheck.py

# Re-freeze fixtures before a demo (mandatory same-day, per Decision D9)
FORESHORE_MODE=live python scripts/freeze_fixtures.py

# Run the full test suite (the "240 tests" number)
.venv/bin/pytest backend/tests

# Launch both surfaces for screenshots
FORESHORE_MODE=fixture uvicorn foreshore.api.main:app --app-dir backend --port 8000
cd frontend && npm run dev
# /boat, /console, / (landing) at localhost:5173; FastAPI docs at localhost:8000/docs
```

---

*This document was compiled by reading the current state of the repository directly —
README.md, PROJECT_CONTEXT.md, PLAN.md, PLAN_V2.md, docs/DECK_CONTENT.md,
docs/DEMO_SCRIPT.md, docs/DECISIONS.md, docs/API.md, every config/*.yaml, and the actual
source code (specialists.py, registry.py, models.py, App.tsx, LandingPage.tsx, route
decorators, git log, and a live pytest collection) — not copied from the docs alone. Where
the docs and the code disagreed (specialist count, phase-10 feature status), the code was
treated as the source of truth and the discrepancy is called out explicitly above.*
