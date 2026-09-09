# CLAUDE.md — FORESHORE

Operational contract for Claude Code. Read before touching anything.

| Doc | What is in it |
|---|---|
| `docs/PS_REQUIREMENTS.md` | The 11 PS capability bullets, 8 sample queries, **live compliance status**. The acceptance checklist. |
| `docs/CONTEXT.md` | Background, competitive position, judge questions, what is / is not built |
| `docs/API.md` | HTTP + WebSocket contract |
| `docs/DECISIONS.md` | D1–D11 — engineering findings made during the build, with evidence |
| `docs/DEMO_SCRIPT.md` | Pre-flight, the 7-minute run, failure drills |
| `docs/DECK_CONTENT.md` | The 6-slide PDF content (30 Sept artifact) |
| `docs/BACKLOG.md` | Deferred work + field-research citations. Not a work plan. |

`PLAN.md` (phases 0–9) and `PLAN_V2.md` (phases 10–13) were retired on 2026-09-09 —
0–9 are built, 10–13 were not reachable in the time left. Both remain in git history, and
code comments citing "PLAN.md Phase N" are historical provenance, not a live pointer.
Anything still wanted from them is in `docs/BACKLOG.md`.

**Last verified against live sources: 2026-09-06.** Every endpoint below was probed
directly, not taken from documentation. Re-run `scripts/healthcheck.py` each morning —
operational endpoints move.

> **Reachable is not usable.** Two sources answered `200` with content that could not
> support the claim made from it, while the healthcheck said `8/8 OK` throughout. Both are
> handled below. When a source looks fine and the answer looks wrong, check what the layer
> actually *contains* before anything else — and check it more than once.

---

## What this is

**FORESHORE** — an agentic marine intelligence platform for small-boat fishermen on the
Palk Bay / Gulf of Mannar coast, plus a shore-side control console for fisheries and Coast
Guard operators.

Built for **Smart India Hackathon PS SIH26176 ("ORCA")**, submitted by ISRO / Department of
Space, filed under **Disaster Management**. That framing is not decoration: safety,
alerting and hazard avoidance outrank conversational polish in every design tradeoff. When
in doubt, favour the safety path.

| Date | Deliverable |
|---|---|
| ~9 Sept 2026 | Internal college round — **live demo + PPT** |
| 30 Sept 2026 | SIH portal — **6-slide PDF only**, no demo, reviewed by the PS owner |
| Oct / Nov 2026 | Screening, then finale shortlist |
| Dec 2026 | Grand Finale, 36 hours |

The 30 Sept artifact is judged with no conversation attached. The demo exists partly to
manufacture the screenshots and numbers that PDF needs.

---

## Model delegation policy

**Opus decides. Sonnet writes.**

Opus (main thread) owns: architecture and anything with a trade-off; **contract
definition** (`models.py`, tool signatures, config schemas, API shapes); **safety-critical
logic** (`verdict/douglas.py`, `verdict/ceiling.py`, the abstention path, geofence
classing); reviewing every subagent diff; phase sequencing, scope cuts, demo script, deck.

Sonnet subagents own everything else — implementation against a contract Opus has already
fixed. Natural units: one source adapter, one tool module, one UI route or component, tests
for an already-specified module.

```
Agent(subagent_type: "general-purpose", model: "sonnet", prompt: <brief>)
```
For surgical 1–2 file edits, `caveman:cavecrew-builder` is cheaper.

Every subagent brief must carry, verbatim: (1) exact file paths to create or modify,
(2) the contract — dataclasses, signatures, return types, (3) the acceptance test it must
satisfy, (4) the standing invariants below.

**Batch independent subagents in parallel.** Separate adapters, tools and UI components
have no interdependency — dispatch in one message, not serially.

Opus writes code only when: defining core contracts; the logic is safety-critical; a
subagent has failed the acceptance test twice; or briefing costs more than doing it.

---

## Non-negotiable invariants

Enforced in code, not left to model judgment. Do not weaken them to make a demo work.

1. **Advisory ceiling.** Never issue a verdict more permissive than the governing IMD
   Coastal Bulletin for the area. May be *more* cautious. A deterministic post-check on the
   final verdict object, after the LLM produced it. If it trips, the verdict is downgraded
   and the downgrade is logged and shown.

2. **Three verdicts only.** `GO` / `GO_WITH_CAUTION` / `DO_NOT_ADVISE`. `DO_NOT_ADVISE` is
   a designed outcome for missing, stale or contradictory inputs — not an error state. It
   must hand off to a named human authority (nearest landing centre from
   `PFZ_LandingCentres`, plus Coast Guard 1554), never guess.

3. **No unsourced numbers.** Every quantitative claim traces to a retrieved record with a
   source, an acquisition timestamp and a spatial resolution. No provenance record → it
   does not appear in the answer. The LLM never supplies values from its own knowledge. A
   unit test asserts this.

4. **Staleness is surfaced, never hidden.** Every answer carries an evidence panel. Nothing
   is labelled "current" that isn't. The IMD bulletin's own validity is **12 hours**; past
   that it cannot authorise anything.

5. **Geofence classes are semantically distinct.** Five classes (below). Do not collapse
   them into one "restricted zone" type.

6. **Region config only.** No coordinate, boundary name or language code in application
   logic. "Does this only work for Tamil Nadu?" must be answered by a live config swap.

7. **`FORESHORE_MODE=live|fixture`.** Every adapter respects it. Fixture replays frozen
   snapshots from `data/fixtures/`. **Live is the default, fixture is the parachute** — a
   frozen bulletin reports itself expired two days later and is indistinguishable from a
   real expiry, so every answer carries `run_mode` and the console chips it.

8. **Every query goes through the model; the deterministic path is the net, not the plan.**
   `.env` loads at import (`config._load_env_file`; shell vars win; `FORESHORE_SKIP_DOTENV=1`
   opts out — the test suite sets it), model calls retry transient failures, and every
   answer reports `payloads.model.written_by` (`"model"`/`"template"`) with
   `degraded_reason`. A template answer still carries the same verdict, evidence and trace —
   it just stops being silent.

---

## Architecture shape

Two surfaces, one agent core. Agents, tools and traces are shared; only the renderer
differs. This is the central architectural claim — preserve it.

```
  Boat UI (Tamil-ready, voice-first)     Shore console (English, fleet view)
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
- **Push path** — background loop over tracked vessel positions firing proactive hazard,
  weather-threshold and geofence-approach alerts. The PS says *proactive* and *when
  approaching*; a request-response-only system fails the problem statement.

Ten specialists mirror the PS's own vocabulary (`agents/specialists.py`,
`tools/registry.py::SPECIALISTS`). Each gets a **restricted tool subset** — restriction is
what makes the collaboration real rather than cosmetic.

---

## Data sources — verified live

**INCOIS and IMD GeoServer require a browser `User-Agent` and a `Referer` header.** Without
them you get 403. Single most common way to lose a day.

Everything below is keyless.

| Purpose | Endpoint |
|---|---|
| **Advisory ceiling** | `mausam.imd.gov.in/Forecast/coastal_bulletin_new.php?id=6` (ACWC Chennai; `id` 1–7 = coastal offices) |
| District nowcast / lightning | `reactjs.imd.gov.in/geoserver/imd/wfs` → `imd:NowcastWarningDistrict` |
| AWS observations | same GeoServer → `imd:aws_data_layer` |
| Cyclone track | same GeoServer → `imd:Cyclone_Track_V` (0 features when no active cyclone — valid, not an error) |
| Cyclone cone + wind polygons | `gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC`, then `/polygons/getgeometry?eventtype=TC&eventid=&episodeid=` → `Poly_Cones`, `Poly_Red/Orange/Green`, track LineStrings |
| **Official PFZ advisory lines** — ⚠ age-check required | `incois.gov.in/geoserver/PFZ_Automation/ows` → `PFZ_Automation:pfzlines` (carries `Year`, `Julian_day`) |
| PFZ sectors | `PFZ_Sectors:sector_new` — `SOUTH TAMILNADU` = `SEC006` |
| Landing centres (harbour handoff) | `PFZ_LandingCentres:LandingCenters_29Apr2024` — 541+ named, district + lat/lon |
| Ecologically sensitive zones | `incois.gov.in/geoserver/MHW/ows` → `MHW:CORAL_REEF_DISS`, `MHW:SEAGRASS_ZONE_DISS`, `MHW:MANGROVE_ZONE_DISS` |
| Harmful algal bloom | `ABIS:HABSectors` (includes `"Gulf of Manmar (GoM)"`) |
| **Waves — authoritative model** | `incois.gov.in/thredds/dodsC/osf/wave/WAVES_coast_YYYYMMDD.nc` |
| Maximum wave height | `osf/mwh/MWH_coast_YYYYMMDD.nc` → `MAXW` |
| Currents / winds / SST | `osf/currents/`, `osf/winds/`, `osf/sst/` (**not** `osf/chl`) |
| **Chlorophyll now** | `coastwatch.pfeg.noaa.gov/erddap` → `nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily` (DINEOF gap-filled, 1/12°, ~3 d lag); cross-check `erdMH1chla1day_R2022NRT` (MODIS-Aqua, 1/24°) |
| **Chlorophyll, decadal, ISRO sensor** | `erddap.incois.gov.in` → `incois_oceansat2_datasets` `CHL` — Oceansat-2 OCM, 2011-02-02→2020-05-01, closed archive, lat 0.1–27.9 / lon 46.7–99.3 |
| **SST, decadal + published anomaly** | `coastwatch.pfeg.noaa.gov/erddap` → `ncdcOisst21Agg` (`sst`, `anom`), daily 0.25°, 1981→present |
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

### Two layers that answer 200 and are still unusable here

**`osf/chl` does not cover India.** It is a VIIRS 4 km 3-day composite of the **Pacific
Islands Countries** — filenames say so (`…-4KM-PICountries-CHL.nc`) and so does the grid:
`lat -25.979 .. 18.021`, `lon 129.979 .. 215.021`. Palk Bay is 78–80.6°E. The NCSS `400`
`incois_thredds` fast-fails on is that miss, not a transient. Chlorophyll now comes from a
fallback chain: INCOIS `osf/chl` first (it still wins in a basin that grid covers), then
NOAA gap-filled VIIRS, then MODIS-Aqua. `payload["chlorophyll_source"]` names whichever
answered. Full account: `docs/DECISIONS.md` D1.

**`PFZ_Automation:pfzlines` swings between today's advisory and a 2021 one.** Probed twice
on 2026-09-06: at 10:40 UTC, 65 features nationally, every one `Year=2021, Julian_day=248`;
at 18:10 UTC, 79 features, every one `Year=2026, Julian_day=249` — that same day. The layer
is live, but in the window before the day's advisory is published it serves five-year-old
content **under identical field names and with no error of any kind**. `GetCapabilities`
shows no second layer to prefer. This is worse than a dead source, because it is right most
of the time. `find_nearest_pfz` checks the advisory's age against
`PFZ_ADVISORY_MAX_AGE_DAYS` (7 days, sized to the source's ~3-per-week cadence) and, past
that, returns `partial=True, missing=["incois_pfzlines_current"]` with a summary leading on
the age. The line is still returned — it is real and official — but never as an answer to
"where is the zone today". **Do not remove this check because the layer looks current when
you test it.** It looked current to me too, eight hours after it did not.

### Registration-gated (upside, not dependencies)

- **IMD API** (`api.imd.gov.in`) — Bearer token, not IP whitelisting. Clean JSON instead of
  HTML/WFS parsing. Every field it provides is already reachable keyless.
- **MOSDAC** — batch downloader, not a live API. Never call it from inside an agent turn.
  Oceansat-3 OCM would put *live* chlorophyll over Indian waters back on an ISRO
  instrument, which for a Department-of-Space PS is the single best provenance upgrade
  available. Still batch-only, so it feeds a scheduled ingest, never an agent turn.
- **Bhashini** (`dhruva-api.bhashini.gov.in`) — Tamil ASR/TTS. Same alignment argument.

---

## Geofence classes — five, distinct

| Class | Source | Severity | Warn / critical |
|---|---|---|---|
| `IMBL_HISTORIC_WATERS` | line_id **1306**, 1974-06-28 agreement, 9.10–10.08°N — the Palk Bay / Rameswaram line | legal, hard | 2.0 / 0.5 nm |
| `IMBL_MARITIME_BOUNDARY` | line_ids 1307 / 1310 / 1311, 1976 agreements | legal, hard | 2.0 / 0.5 nm |
| `MPA` | Gulf of Mannar Marine National Park | restricted | 1.0 / 0.25 nm |
| `ECO_SENSITIVE` | INCOIS `MHW` coral / seagrass / mangrove | advisory | 0.5 nm |
| `USER_DEFINED` | user-drawn — PS: *"other predefined operational boundaries"* | configurable | configurable |

Plus dynamic `HAZARD_EXCLUSION` from cyclone polygons and high-wave cells.

1306 is a **historic-waters** boundary — a different legal regime from the 1976 maritime
boundary. Distinct copy, distinct lead distances, distinct severity. Each Marine Regions
segment carries its treaty name and date as attributes, so "where did your maritime
boundary come from?" is answered from the data itself.

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
  `DO_NOT_ADVISE` if swell period ≥ 15 s (long-period swell in a shallow bay is the
  kallakkadal signature)
- bulletin older than its 12 h validity → `DO_NOT_ADVISE`
- any required input missing → `DO_NOT_ADVISE` with handoff

Vessel thresholds live in `config/vessels.yaml`, never in code. For a 0–50 nm small
motorised boat: `GO` only up to SLIGHT, `GO_WITH_CAUTION` up to MODERATE, `DO_NOT_ADVISE`
at ROUGH+.

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

`config/regions/gujarat_sir_creek.yaml` is kept working purely to demonstrate the swap.

---

## Conventions

- **Python** for ingestion, geospatial processing, agents. **TypeScript/React** for both
  UIs. PostGIS for vectors, NetCDF/xarray for grids. Routing and thresholding in numpy, not
  in the database. EPSG:4326 unless stated.
- Tools are **typed and deterministic**. Spatial operations are real geospatial computation
  — nearest-polygon, raster thresholding, path planning over a cost field. The LLM selects
  and sequences tools; it does not perform the geometry or the arithmetic.
- **Routing is A\* over a weighted grid** (Hs, wind, currents, steepness, bathymetry,
  exclusion polygons, soft IMBL proximity penalty). Never LLM-generated waypoints — a fake
  router is instantly visible to an ISRO judge. Return the per-leg cost breakdown so the UI
  can explain *why* the route bends.
- **Agent orchestration is hand-rolled** over Anthropic tool use — not LangChain/LangGraph.
  Full control of the stored trace, fewer unknowns, and it differentiates from the field.
- Every tool call and result is persisted as a reasoning trace, retrievable and renderable.
  Explainability is a stored artifact, not post-hoc LLM narration.
- Ingestion jobs are idempotent and record granule acquisition time on write.
- Language is **auto-detected, never a dropdown** — script block first, then a romanised
  fishing-domain lexicon (`agents/language.py`). **Output is English-only right now** — see
  the pin below.

### LLM providers

`FORESHORE_LLM_PROVIDER` selects the wire format: `anthropic` (production, native shape),
`gemini` or `nvidia`. The latter two are OpenAI-compatible and share one adapter —
`runtime.py`'s `OpenAICompatibleClient`, subclassed per provider. Same `AgentRuntime` loop,
same trace, same tool schemas whichever answers. No key for the selected provider degrades
to `ScriptedClient`. Facts worth not rediscovering:

- `gemini-2.5-flash` is **retired for newly-issued keys** — 404 pointing at
  `gemini-3.6-flash`. That is the default.
- Gemini spends thinking tokens out of `max_tokens`, so answers came back cut off
  mid-number. `GeminiClient.budget()` adds headroom. `reasoning_effort: "none"` is 400'd by
  3.6-flash; `minimal`/`low`/`medium`/`high` work.
- One query is several model calls, which walks into Gemini's free-tier per-minute quota.
  NIM's free tier absorbs it.
- Tool `parameters` must carry `type: "object"`; Gemini 400s on unknown JSON-Schema
  keywords and on `required: []`. `_sanitise_schema` filters for the strictest provider.

### Answer shaping

- **Answer kind.** `planner.classify_answer_kind` labels every utterance `ADVISORY` or
  `INFORMATIONAL`, deterministically, from cue words. It governs **presentation only** —
  the safety spine, the verdict and the ceiling run identically for both. `ADVISORY` leads
  with the verdict. `INFORMATIONAL` answers the question, with the verdict attached as
  framed safety context. Without this, "which vessels are closest to the IMBL?" was
  answered "Do not go."
- **The model path is audited like the template path.** `synthesis.enforce_answer_contract`
  runs on model-written prose, after synthesis and again after polish: restores a dropped
  verdict sentence, restores a dropped named handoff (invariant 2), reframes a bare verdict
  opener on an informational answer. `answers_the_question` falls back to the template when
  a model handed an informational question writes about the verdict instead — checked on
  numeric-token overlap, because here the substance of a finding is its numbers. Every
  repair is recorded on `payloads.contract_repairs`, never hidden.
- **Tool summaries are user-facing prose**, spliced verbatim into the answer. No `summary`
  may contain an enum code (`MPA`, `BREACH`, `DO_NOT_ADVISE`), an internal tool name, an
  exception class or a file path. Those belong on `error` or in `payload`.
- **Final editor pass** (`agents/synthesis.py::polish_answer`) runs after the verdict, the
  evidence audit and the ceiling. Readability only: any candidate that introduces a number,
  changes the verdict, drops the named handoff or switches language is discarded and the
  unpolished text ships, reason recorded on `payloads.polish`. `FORESHORE_POLISH=off`
  disables the model half. Polish is never load-bearing.
- **Prompt echo.** `_synthesis_prompt` is three labelled blocks — QUESTION / WHAT IS TRUE /
  WHAT TO DO — because a 30B model copied interleaved directives into the answer.
  `is_prompt_echo` is the deterministic net: an answer containing a brief-only phrase is
  discarded for the template.
- **Conversational front door.** `agents/conversation.py` sorts every utterance
  deterministically before the planner — distress, capability, concept, smalltalk,
  out-of-scope, or `OPERATIONAL` (handed to the planner unchanged). A verdict offered to
  someone who said "hello" is not cautious, it is wrong.

### The English-only pin

Two region-config keys, deliberately distinct:

| Key | Means |
|---|---|
| `languages: [en, ta]` | what the region **knows** — what `detect` may resolve to, what copy tables exist for |
| `surface_languages: [en]` | what may **reach a screen** — rendered copy, alert bodies, tool payloads |

`surface_languages` is the single gate. It defaults to `[primary_language]`, so a region
that never declares it cannot leak a half-translated surface. Enforced at four points:
`tools/geofence_tools.py` (`payload["messages"]` built per surface language),
`models.py::Alert._localised` (`en` always present — a geofence breach must stay legible),
`api/routes_reference.py::_region_dict` (local names fall back to English twins), and
`agents/orchestrator.py` (`FORESHORE_LANGUAGE_LOCK`, defaults `en`).

**Why:** with no model reachable the answer is built by `synthesis.template_answer`, which
splices tool observation strings in verbatim — and those are generated in English *inside
the tools*. A Tamil query would return a Tamil headline wrapped around English bulletin and
geofence text. Half-translated safety copy is worse than English safety copy.

Nothing is deleted: `ta`/`gu` stay in `languages`, `VERDICT_COPY`, `config/geofence.yaml`
and `config/weather_alerts.yaml`; `planner.py` keeps its Tamil intent keywords;
`backend/tests/test_language.py` holds detection to its contract. Lifting the pin means
`surface_languages` **plus** `FORESHORE_LANGUAGE_LOCK` **plus** localising the tool-level
strings — all three, or the mixed output returns immediately.

---

## Do not

- Do not call MOSDAC synchronously from an agent.
- Do not let the LLM emit a numeric value with no provenance record.
- Do not present derived PFZ zones as the official INCOIS advisory.
- Do not trust a `200` as evidence a source is usable. Check what the layer *contains* — its
  dates and its extent — before building on it.
- Do not plan a route to a destination nobody named by defaulting it to the origin. A 0.0 nm
  route is a router that did not run, presented as a route. Region config carries
  `fishing_grounds`; with none configured, plan no route and say so.
- Do not build the request path only — the push/alert loop is a scored requirement.
- Do not collapse the geofence classes.
- Do not hardcode region specifics.
- Do not average disagreeing sources. Show them side by side and say which governs.
- Do not dress CAPE up as a lightning probability. Open-Meteo `lightning_potential` is null
  over India; if the IMD nowcast is unavailable, say so and abstain.
- Do not claim real-time AIS. There is no public feed for Indian small boats — label
  simulated vessel positions as simulated.
- Do not "fix" a failing demo by relaxing the advisory ceiling or the abstention path.
- Do not add features not traceable to a PS capability bullet. Scope creep costs marks.
- Do not skip `User-Agent` / `Referer` on INCOIS and IMD GeoServer calls.

---

## Query latency — measured, not guessed

96% of a query's wall clock is model calls, not data. Tools total ~0.3 s warm; the
`data/cache` TTL is 600 s, so **pre-warm before a demo** — cold INCOIS OSF NetCDF grids are
the one expensive fetch.

Baseline was 30.5 s / 8 sequential model calls. Now ~15 s mean, 17 s worst, 5 calls:

| Lever | Where | Win |
|---|---|---|
| Specialists run concurrently | `orchestrator.answer`, `ThreadPoolExecutor` | 16 s → slowest one |
| Their evidence is seeded, not re-fetched | `runtime.run(prior_results=…)` | 3 turns → 1 per specialist |
| `SPECIALIST_MAX_TURNS = 2` | `orchestrator` | bounds a model that re-reads a tool |
| `DEFAULT_SPECIALIST_TIMEOUT_S = 12` | `orchestrator` | one slow specialist stops setting the floor |
| Polish folded into `SYNTHESIS_SYSTEM` | `synthesis` | one less call |

Escape hatches, all env: `FORESHORE_SPECIALISTS=serial`, `FORESHORE_SPECIALIST_TIMEOUT_S`,
`FORESHORE_POLISH=on|off|auto`, `FORESHORE_LLM_ATTEMPTS`.

Concurrency means shared state needs locks — `TraceStore` writes and the evidence bus
(`tools/verdict_tools.py`) both have one. Specialist results are merged in **plan order,
never completion order**, so the trace a judge reads is identical run to run.

### Streaming

`POST /api/query/stream` — same body and same result as `/api/query`, delivered as SSE:
`status` (phase + detail) → `token` (text deltas) → `done` (the full outcome) or `error`.

Only the synthesis turn streams, and only because it declares no tools. **Streamed tokens
are a draft.** Every deterministic guard runs after the last delta: the unsourced-number
audit, `enforce_answer_contract`, the ceiling wording. `done.text` is authoritative and the
client replaces rather than appends. Both surfaces label it a draft while it streams.

---

## Open unknowns

1. IMD API key approval turnaround — not blocking, keyless fallbacks verified
2. MOSDAC account approval turnaround — not blocking
3. Tamil ASR accuracy on fishing-domain vocabulary. Realistic WER 15–20%, worse in domain.
   Needs mitigation design — lexicon biasing plus spoken readback confirmation
4. Routing cost-field weights, confidence bands, geofence lead distances — derive from
   measured cadence (OSF ~2-day lag, chlorophyll 3-day composite, bulletin 12 h validity).
   Do not invent
5. Offshore connectivity beyond ~10–12 km. Geofence proximity needs no network and runs
   client-side; hazard push does. GEMINI/GAGAN and NavIC messaging are the real channel

`Foreshore Technology` sells dredge monitoring software — none in fisheries advisory, none
Indian. Fine for SIH; do not claim the name is unowned.
