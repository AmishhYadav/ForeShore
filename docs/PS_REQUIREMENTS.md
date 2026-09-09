# FORESHORE — PS Requirements & Compliance Checklist

**PS SIH26176 · "ORCA — Marine EcOsystem Reasoning with Collaborative Agents"**
Indian Space Research Organisation · Department of Space
Category: Software · Theme: **Disaster Management**

Single acceptance checklist: "are we answering the problem statement?" should be
answerable from this file alone. Distilled from `PROJECT_CONTEXT.md` §2 and the Part 1
compliance audit in `PLAN_V2.md` (retired 2026-09-09), with statuses corrected against
the code as of 2026-09-09 — see "Open gaps" at the bottom for what is stale in PLAN_V2
and why.

---

## The 11 literal capability bullets

The PS "Expected Solution" section contains **11 explicit capability bullets**. Every one
is scoreable. Treat this as the acceptance checklist.

| # | Capability | Status / notes |
|---|---|---|
| 1 | Understand user intent in natural language | Core |
| 2 | **Auto-detect query language and respond in the same**, emphasis on Indian regional languages | Tamil primary; voice-first |
| 3 | Multi-turn contextual conversation, query refinement | Core |
| 4 | Autonomously discover, retrieve, integrate satellite / marine / met / geospatial datasets | Constrained by MOSDAC batch-only access — see PROJECT_CONTEXT.md §5 |
| 5 | Spatial, temporal, contextual reasoning across heterogeneous sources | Must be real geospatial computation, not LLM description |
| 6 | Explainable, evidence-based recommendations with maps, charts, geospatial viz, advisories | Evidence panel |
| 7 | **Proactive** alerts — adverse weather, high waves, lightning, cyclones | Requires a push path, not request-response |
| 8 | Geofencing notifications **when approaching** IMBL, restricted waters, MPAs, ecologically sensitive zones | Requires continuous position tracking |
| 9 | Route optimization, safe navigation, operational planning | Real path planning over a cost field |
| 10 | Deliver recommendations **together with** supporting evidence and reasoning | Stored reasoning traces |
| 11 | Demonstrate agentic principles: autonomous planning, reasoning, tool selection, execution, agent collaboration, explainable decisions | Architecture narrative |

The PS additionally *encourages* (does not require) a modular multi-agent architecture with
specialists for: planning, marine data discovery, weather intelligence, ocean analytics,
geospatial reasoning, risk assessment, visualization, reporting, and user interaction. This is
the vocabulary judges will use when reading the architecture slide — mirror it.

---

## Compliance status (as of 2026-09-09)

Base: PLAN_V2.md Part 1's code-verified audit. Corrections applied below where the code has
moved since PLAN_V2 was written — those rows are marked **NOW MET** / **PARTIAL, deliberate**
/ **NOT MET** with the 2026-09-09 rationale in place of the PLAN_V2 wording.

| # | Capability (short) | Status | Evidence |
|---|---|---|---|
| 1 | Understand intent in natural language | **MET** | `agents/planner.py`, 9 intents, EN + Tamil script + romanised |
| 2 | Auto-detect language, respond in same | **PARTIAL, deliberate** | Detection works (`agents/language.py`). Output is deliberately pinned English-only via `surface_languages` + `FORESHORE_LANGUAGE_LOCK` — see `CLAUDE.md` "The English-only pin" for why (template answers splice English tool strings verbatim, so a Tamil query would return half-translated safety copy). Not a defect; a documented decision. |
| 3 | Multi-turn contextual conversation, query refinement | **NOW MET** (2026-09-10 — was NOT MET, stale) | `QueryRequest`/`Query` carry `session_id`; `agents/orchestrator.py::answer` fills an omitted `lat`/`lon`/`vessel_class` from `ConversationStore.last()` (question shape only, never a reading — invariants 3/4) and appends the resolved turn after answering. Always returns a `session_id`; both UIs (`AnalystQuery.tsx`, `BoatApp.tsx`) echo it back on the next request. |
| 4 | Autonomously discover, retrieve, integrate datasets | **MET** | tool 16 `list_available_data` |
| 5 | Spatial/temporal/contextual reasoning across heterogeneous sources | **MET** | `verdict/engine.py::governing()`, three-source disagreement |
| 6 | Explainable recommendations via maps, **charts**, geospatial visualizations | **PARTIAL, improved 2026-09-10** | Maps are real (maplibre-gl). `routes/console/TimeSeriesChart.tsx` (dependency-free inline SVG, no new library) now renders `get_productivity_history`'s retrieved Argo subsurface-temperature series on the console — the sample query "why has fish productivity declined" now gets a real chart, not just prose. Chlorophyll/SST trends still ship as a fitted slope only (`tools/productivity.py` never keeps their raw point series), so those two remain text-only — a real gap, just a smaller one. |
| 7 | Proactive alerts for adverse **weather, high waves, lightning**, cyclones | **NOW MET** (PLAN_V2 said PARTIAL — stale) | `backend/foreshore/push/loop.py` refreshes a cached region weather picture per tick and fires threshold alerts via `_weather_triggers_for_vessel`; copy lives in `config/weather_alerts.yaml`; `backend/foreshore/push/weather_copy.py` formats it. A watchstander can also push a live alert by hand — `POST /api/alerts/broadcast` (added 2026-09-10) rides the same `WS /ws/alerts` transport, so it reaches every connected boat/console screen instantly, `Alert.kind="operator"` distinguishing it from the automated three. |
| 8 | Geofencing notifications | **MET** | 6 distinct classes, offline client-side check |
| 9 | Route optimisation, safe navigation, **operational planning** | **NOW MET** (PLAN_V2 said PARTIAL — stale) | The Decision Envelope closes "when can I go / how long have I got": `backend/foreshore/verdict/envelope.py`, `backend/foreshore/tools/envelope_tools.py::get_decision_envelope`. |
| 10 | Recommendations with supporting evidence and reasoning | **MET** | Evidence panel, trace inspector, provenance invariant under test |
| 11 | Demonstrate agentic principles | **MET** | Architecture narrative — see "ten cooperating agents" note below |

**"Ten cooperating agents" note:** PS title is "ORCA — Marine EcOsystem Reasoning with
Collaborative Agents", and PS catalogues paraphrase it as *"ten cooperating AI agents."*
**NOW MET** (PLAN_V2 said the catalogue advertised two agents — `PlanningAgent`,
`UserInteraction` — that did not exist; stale). `backend/foreshore/agents/specialists.py`
now defines all 10, matching `tools/registry.py::SPECIALISTS`.

**Stakeholders:** PS names five (fishermen, researchers, coastal authorities, disaster
management, maritime operators). Two built — a deliberate, defensible cut
(`PROJECT_CONTEXT.md` §8, "Scope discipline"). "Researchers" is nearly free given the
archival Argo/Oceansat pipeline already exists.

---

## The eight sample queries

These are effectively the demo script. Assume a judge picks two at random and asks for a live run.

| Query | What it actually tests | Difficulty |
|---|---|---|
| Nearest Potential Fishing Zone today | Retrieval + nearest-polygon | Low |
| Is it safe to venture out tomorrow morning? | Forecast fusion + **judgment under uncertainty** | High — liability |
| Tide, weather, sea conditions near my location | Multi-source fusion | Low |
| Lightning or cyclone alerts in my area | Nowcast + alerting | Medium |
| Regions with high chlorophyll and favourable SST | Raster thresholding — effectively PFZ derivation | Medium |
| Safest route given weather and sea state | **Path planning over a cost field** | High — algorithmic |
| Why has fish productivity declined in a region? | **Causal / diagnostic, multi-year series** | Highest |
| Which zones to avoid (hazard or geofencing) | Spatial exclusion + geofence reasoning | Medium |

Two decide the competition:

- **The route query** is the only genuinely algorithmic one. Done properly it is cheap technical
  credibility; faked, it is instant credibility loss.
- **"Why has productivity declined"** is diagnostic rather than retrieval — SST anomaly,
  chlorophyll trend, upwelling indices, monsoon timing, possibly fishing effort. Almost no team
  will handle it, and it speaks directly to ISRO's scientific mission.

---

## Buried constraints most teams will skim

- **"Proactive" and "when approaching"** — a chatbot cannot satisfy bullets 7 and 8. Hidden
  architectural requirement.
- **"Available in the public domain"** — you are explicitly constrained to public data. No
  privileged ISRO feed. Judges know exactly what's reachable.
- **"Automatically identifying the language"** — auto-detect and mirror, not a language dropdown.
  User is at sea with wet hands: the realistic modality is voice.
- **Explainability appears three times** (bullets 6, 10, 11, plus the Description). Repetition is
  emphasis. To an ISRO judge, "explainable" means provenance: which sensor, what acquisition
  time, what resolution, how stale.

---

## Open gaps as of 2026-09-10

- **Charts (bullet 6), partial.** The productivity diagnostic now charts (see #6 above);
  chlorophyll and SST trends still don't carry a raw series server-side to chart, only a
  fitted slope. Would need `tools/productivity.py`'s `_compute_chl_trend`/`_compute_sst_trend`
  to keep their point series the way the Argo path already does.
- **Regional-language output (bullet 2), deliberate pin.** `agents/language.py` detects; output
  stays English-only via `surface_languages` + `FORESHORE_LANGUAGE_LOCK` in
  `agents/orchestrator.py`. Lifting this is a documented decision, not an oversight — see
  `CLAUDE.md` "The English-only pin" for the exact three conditions required to lift it.
