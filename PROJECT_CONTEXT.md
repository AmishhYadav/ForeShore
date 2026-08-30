# FORESHORE — Project Context

**SIH26176 · ORCA** — Marine EcOsystem Reasoning with Collaborative Agents
Indian Space Research Organisation · Department of Space
Category: Software · Theme: **Disaster Management**

> **FORESHORE** — Marine foresight for the small-boat fleet.

Full context document. Companion to `CLAUDE.md`, which holds the operational subset for
Claude Code sessions.

---

## 1. Naming

"ORCA" is ISRO's own backronym for the problem statement, not a product name. Every team
working SIH26176 is working on "ORCA" — using it as identity makes the submission
indistinguishable in judges' notes.

**FORESHORE** is the product name. *Fore-* (ahead, in advance: forecast, forewarn, foresight)
+ *shore*. A foreshore is also the real coastal term for the strip between high and low tide —
where small boats actually launch from.

Deck treatment:

```
SIH26176 · ORCA
FORESHORE — Marine foresight for the small-boat fleet
```

Do not backronym it. The word already means something true; bolting letters onto it converts a
genuine name into a visible gimmick.

*Outstanding:* confirm no existing maritime/marine software product owns the name.

---

## 2. Problem statement — the literal requirements

The PS "Expected Solution" section contains **11 explicit capability bullets**. Every one is
scoreable. Treat this as the acceptance checklist.

| # | Capability | Status / notes |
|---|---|---|
| 1 | Understand user intent in natural language | Core |
| 2 | **Auto-detect query language and respond in the same**, emphasis on Indian regional languages | Tamil primary; voice-first |
| 3 | Multi-turn contextual conversation, query refinement | Core |
| 4 | Autonomously discover, retrieve, integrate satellite / marine / met / geospatial datasets | Constrained by MOSDAC batch-only access — see §5 |
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

### The eight sample queries

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

### Buried constraints most teams will skim

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

## 3. Locked decisions

| Question | Decision |
|---|---|
| Primary user | Small-boat motorized fisherman, 0–50 nm, day-to-3-day trips |
| Secondary user | District Fisheries / Coast Guard control-room operator |
| Region | Palk Bay + Gulf of Mannar (Rameswaram → Nagapattinam), region-agnostic by config |
| Language | Tamil primary, English fallback, voice-first input and output |
| Staleness | Evidence panel on every answer: source, acquisition time, resolution, freshness state |
| Safety | `GO` / `GO_WITH_CAUTION` / `DO_NOT_ADVISE` + advisory-ceiling invariant |
| Product framing | Two surfaces, one shared agent core |

### Why two users

Fisherman-first is obvious. Adding the shore-side operator is not, and it earns its place:

- The PS is filed under **Disaster Management**. A control-room view *is* the disaster-management
  artifact — fleet positions against an approaching cyclone track, who's inside a geofence, who
  hasn't acknowledged an alert. Without it the submission is a consumer app in the wrong category.
- It gives the hard analytical queries a home. "Why has productivity declined here" is not a
  question a fisherman asks a phone at 4am. Splitting the surfaces makes each coherent.
- It produces the strongest architecture claim available: same agent core, same tools, same
  reasoning traces, two radically different presentations — demonstrable in thirty seconds.

Cost is low. Only the renderer changes.

### Why Palk Bay

Highest-conviction call in the project.

- **IMBL salience.** The India–Sri Lanka boundary sits close enough to shore that crossing it is
  an ordinary navigational hazard, not a theoretical one, and detentions of Indian fishermen are
  a recurring documented problem. Converts the geofencing bullet from "we drew a polygon" into a
  live national problem an ISRO judge needs no explanation for.
- **Gulf of Mannar Marine National Park** is adjacent — a real, gazetted MPA in the same demo
  frame as the IMBL. Two geofence classes with genuinely different semantics.
- **Bay of Bengal cyclone exposure** aligns with the Disaster Management theme.
- **Tamil has the best ASR/TTS support** of any coastal Indian language — de-risks bullet 2.
- **Shallow, reef-strewn, bathymetrically complex** — a straight line is genuinely wrong there,
  so the path planner has real work to do.

Runners-up: Gujarat / Sir Creek (loses on language tooling and MPA adjacency); Kerala (no
international boundary — guts the geofencing bullet).

Build region-agnostic, demo region-deep.

### Why voice-first

The user is on a boat, at night, with wet hands and an inexpensive phone. Text input is the wrong
modality and any judge who knows the context knows it. It also satisfies bullet 2 better than
translation does — detect on the audio, respond in kind. And it creates a demo moment that costs
nothing: someone speaks Tamil into a phone and the system answers in Tamil with a map.

### Why staleness is surfaced

Optical chlorophyll has cloud gaps. PFZ advisories are issued on a schedule, not continuously.
Forecast skill degrades with horizon. Every team hits this; most will paper over it.

A system that says *"the most recent cloud-free chlorophyll field covering your area is 38 hours
old; I've weighted SST more heavily and lowered confidence accordingly"* is more credible than one
claiming live everything — and it turns the biggest data weakness into the clearest demonstration
of the PS's thrice-stated explainability requirement.

### The safety invariant

Three verdicts, and only three. `DO_NOT_ADVISE` is a designed outcome for missing, stale, or
contradictory inputs, and hands off to a named human authority rather than guessing.

One hard rule, stated on a slide:

> **FORESHORE will never issue a more permissive verdict than the governing INCOIS or IMD advisory
> for that area.** It may be more cautious. It can never be less.

Enforced in code as a deterministic post-check on the final verdict, not left to LLM judgment.
The audit confirmed this is directly implementable: `api.imd.gov.in/api/v1/coastalbulletin`
carries a "South Tamilnadu coast" layer issued by ACWC Chennai.

You *will* be asked "what if your system says it's safe and someone dies." The invariant plus the
abstention path is the answer, and it is far better than a disclaimer.

---

## 4. Competitive position

### The solution ~80% of teams will build

LangChain/LangGraph, a hosted LLM, three REST calls to a commercial weather API, a Leaflet map,
Google Translate on the output, and a slide with five labelled boxes called "agents." Demo: "is it
safe to go fishing?" → "Yes, waves are 1.2m."

It collapses on one question: *"Where did that number come from, and when was it measured?"*

### The real incumbent — SAMUDRA

**This is the threat that matters and it must be addressed head-on.**

INCOIS launched the SAMUDRA mobile app in August 2023. It is now available in eight coastal
languages and serves as a single interface to INCOIS services: real-time alerts for tsunamis,
storm surges, high waves and swell surges; PFZ advisories guiding fishermen toward probable fish
aggregation areas; and five-day Ocean State Forecasts. **SAMUDRA 2.0** was announced at INCOIS
Foundation Day as a multilingual advisory and early-warning app for fishermen and maritime users,
offering PFZ, TUNA advisories and small-vessel alerts.

So "multilingual mobile app giving fishermen PFZ and safety alerts" is **already shipped** — by
the agency that owns the data, in eight languages, with official authority FORESHORE does not have.

If the pitch is that sentence, a judge from ISRO or MoES will ask why this exists, and there is no
good answer. **Assume the question is coming.**

### The wedge

SAMUDRA is a *delivery channel for precomputed advisories*. It publishes what INCOIS already
decided. It structurally cannot:

- Answer an arbitrary natural-language question requiring correlation across sources
- Plan a route over a weather-and-exclusion cost field
- Answer a diagnostic question — *why* productivity declined
- Show the reasoning and evidence behind a recommendation
- Refuse to answer, and explain why

Every one of those is explicitly in the Expected Solution. **The PS is ISRO asking for the
reasoning layer above what SAMUDRA already does.**

This retroactively justifies two earlier calls: the shore-side console and the diagnostic query
are exactly what SAMUDRA cannot do. They are not nice-to-haves. Do not cut them.

Line for the deck, near the front:

> SAMUDRA tells you what the advisory says. FORESHORE tells you what it means for your boat,
> tonight, and why.

### Reading ISRO's intent

ISRO/SAC has spent a decade building EO products — Oceansat-3 OCM chlorophyll, INSAT-3D/3DR/3DS
SST and lightning, SCATSAT winds, altimetry — and distribution portals like MOSDAC, Bhuvan and
VEDAS. Utilization outside the research community is low. Separately, ISRO has real institutional
investment in fishermen safety, and the IMBL problem is politically live.

ORCA reads as: *"make our Earth Observation stack usable by someone who cannot open a NetCDF file,
and point it at the fisherman-safety problem."*

Practical implication: a solution built on commercial weather APIs while ignoring INSAT and
Oceansat signals that you didn't read the room. Treat ISRO products as first-class and show their
provenance in the UI.

---

## 5. Data source audit — findings

Verified by direct source inspection. Distinguishes what is confirmed from what still needs
hands-on testing.

### Confirmed: MOSDAC is not a live API

The thing ISRO calls "API based Access" is a Python batch downloader — download `mdapi.zip`, fill
in `config.json` with credentials and a `datasetId`, run `mdapi.py`.

- Search works without login; **download requires approved account credentials**
- Cap of 5,000 files per user per day
- Supports `boundingBox` (`minLon,minLat,maxLon,maxLat`), `startTime`/`endTime` (`YYYY-MM-DD`),
  `count` (max 100), `gId`
- `datasetId` = exact product name from the catalog browser (e.g. `E06OCM_L2C_AD`,
  `3SIMG_L1B_STD`)
- Returns granule files, not point values
- **Two user tiers:** NRT users get real-time data; General users get Level-2 and onwards in
  near-real-time and Level-1 with a 3-day latency. Standing orders for recurring NRT pulls are
  described as available to privileged users only. Assume General tier.
- Registration requires email approval — **unknown turnaround, longest-lead blocker**

**Consequence:** scheduled ingestion pipeline, local raster store, agents query the local store.

This is an advantage, not a setback. It forces a real ingest → normalize → index → tool-layer
architecture rather than "agent calls REST endpoint," and it makes the evidence panel trivial to
populate because granule acquisition time is known exactly. Put it on the architecture slide
deliberately.

### Confirmed: IMD is the real-time backbone

`api.imd.gov.in` — proper JSON REST, fully documented, containing almost exactly what the PS asks
for. Full endpoint table in `CLAUDE.md` §"Data sources".

Highlights:

- **Coastal Bulletin** carries a "South Tamilnadu coast" layer from ACWC Chennai — the advisory
  ceiling source, directly implementing the safety invariant
- **Cyclone Wind Warning** and **Cone of Uncertainty** return real GeoJSON MultiPolygons (wind
  thresholds 27/34/50/64 kt) — these drop straight into the routing cost field and geofence engine
- **District/Station Nowcast** carries explicit cloud-to-ground lightning probability bands
  (<30%, 30–60%, >60%) — satisfies the lightning requirement with a native Indian source
- **Fishermen Warning**, **Port Warning**, **Sea Area Bulletin** — named endpoints matching PS
  language
- **AWS observations** for Tamil Nadu (`sid=25`) — ground truth for validation

*Caveat:* IMD documentation mentions IP whitelisting; attribution and client-side caching are
requested. Verify per-endpoint.

### Gap: INCOIS has no documented public API

PFZ and Ocean State Forecast are INCOIS products delivered through SAMUDRA and the web portal.
An Esri Geoportal exists at `incois.gov.in/geoportal/sharing/rest/` — unprobed.

**Response — a net win:** derive PFZ-equivalent zones from OCM chlorophyll + SST fronts, labelled
unambiguously as a derived indicative product.

- Directly answers the PS query about high chlorophyll and favourable SST — that query *is* asking
  for PFZ derivation
- Chlorophyll + SST-front derivation is ISRO/INCOIS's own methodology; an ISRO judge recognizes it
  as legitimate rather than as a workaround
- Converts a data gap into visible analytical depth
- Honesty about derivation is exactly the provenance discipline the PS demands

If the geoportal probe finds real PFZ layers, use them and keep derivation as a cross-check.
Showing both agreeing is a strong demo beat.

### Weakness: wave height resolution

Open-Meteo Marine is free, keyless, JSON, CORS-enabled, ~10,000 calls/day non-commercial, CC BY 4.0.
But the global wave model runs at ~28 km resolution; the 5 km model covers Europe only.

Palk Bay is ~30–100 nm wide and shallow. A 28 km cell is genuinely coarse there. Presenting a model
number as truth would be indefensible under questioning.

**Design response:** IMD Coastal Bulletin sea-state descriptor is authoritative. The model supplies
gradient and trend between issuances, always tagged with its resolution in the evidence panel.
Anyone asking "how accurate is your wave forecast in a shallow bay" gets a better answer than most
teams will have.

### Solved: boundaries, with a free upgrade

- Marine Regions (Flanders Marine Institute): World EEZ v12 including boundary polylines, 12NM
  territorial seas v4, 24NM contiguous zones v4 — GeoPackage/Shapefile/KML, WFS at
  `geo.vliz.be/geoserver`
- **Upgrade:** the India–Sri Lanka maritime boundary is defined by agreements signed in 1974
  (Adam's Bridge / Palk Strait) and 1976 (Gulf of Mannar and Bay of Bengal), with a separate
  trijunction agreement involving Maldives. These publish explicit coordinate lists. Digitize from
  the treaty text.

"Where did your IMBL come from?" answered with "the 1974 and 1976 agreements" beats "a global EEZ
download" by a wide margin, and costs an afternoon.

- Gulf of Mannar Marine National Park: WDPA / Protected Planet — **unverified**

### Bhuvan

WMS/WMTS OGC services for basemap and thematic layers. API at `bhuvan-app1.nrsc.gov.in/api` with
**access tokens that expire daily** — needs refresh logic. Bhoonidhi (`bhoonidhi.nrsc.gov.in`) has
a documented API for EO product download.

Worth using for ISRO-branded cartography in front of this sponsor.

### Feasibility verdict

**Buildable, but not the way most teams will assume.** There is no single live ISRO marine API to
wrap. The credible architecture is a scheduled EO ingestion pipeline feeding a local geospatial
store, with IMD's REST API as the real-time alerting and authority layer on top, and derived
analytics filling the INCOIS gap.

That is more engineering than a chatbot-over-APIs — which is precisely why it survives scrutiny
that other submissions won't.

---

## 6. Anticipated judge questions

| Question | Answer |
|---|---|
| Why does this exist when SAMUDRA already does this? | SAMUDRA delivers precomputed advisories. FORESHORE reasons across sources, plans routes, answers diagnostic questions, and shows its evidence. See §4. |
| Where did that number come from and when was it measured? | Evidence panel — source, acquisition time, resolution, freshness, on every answer. |
| What if you tell someone it's safe and they die? | Advisory ceiling invariant + `DO_NOT_ADVISE` abstention path with named human handoff. |
| Does this only work for Tamil Nadu? | Config file swap, demonstrated live. |
| How accurate is your wave forecast in a shallow bay? | Model is ~28 km — we know, and we anchor the verdict to IMD's human-issued coastal bulletin instead. |
| Are those real agents or five boxes on a slide? | Stored reasoning traces, per-tool-call, retrievable and rendered. |
| Is your routing real? | A\*/Dijkstra over a weighted cost field including IMD cyclone wind polygons and exclusion zones. |
| Where did your maritime boundary come from? | The 1974 and 1976 India–Sri Lanka agreements. |

---

## 7. Open items

Ordered by lead time, longest first.

1. **MOSDAC registration** — email approval, unknown turnaround, blocks all EO ingest. **Do today.**
2. **IMD IP whitelisting** — probe each marine endpoint unauthenticated; if gated, request now
3. **INCOIS geoportal probe** — determines whether PFZ is retrievable or must be derived
4. **MOSDAC catalog** — exact `datasetId` strings for OCM chlorophyll and INSAT SST; real cadence
   over the bbox
5. **Cloud-gap measurement** — two weeks of OCM over Palk Bay, measure usable cover frequency.
   **This number defines the staleness thresholds and belongs on a slide.**
6. **Boundaries** — Marine Regions download + treaty coordinate digitization; WDPA for Gulf of Mannar
7. **Tamil ASR** on fishing-domain vocabulary
8. **Name check** — confirm no existing maritime product owns "Foreshore"

### Not yet defined — derive, do not invent

- Staleness thresholds per data type (depends on item 5)
- Confidence band definitions
- Geofence alert lead times and distances
- Routing cost-field weights

---

## 8. Scope discipline

What has been deliberately given up: national coverage, four of the five stakeholder types named
in the PS, and any claim to being a general-purpose marine platform.

That is the right trade. Depth on one corridor with two sharp personas beats breadth on everything,
because depth is demonstrable in eight minutes and breadth isn't.

Do not add features that don't trace to a PS capability bullet.
