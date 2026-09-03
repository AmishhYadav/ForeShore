# FORESHORE — 6-slide deck content

Drop straight into the SIH-mandated template (unmodifiable layout, 6 slides max including
title, uploaded as PDF only). Points and diagrams, not paragraphs — PLAN.md's own rule.
Screenshots referenced below should be captured per `docs/DEMO_SCRIPT.md` and dropped into
`docs/artifacts/`.

---

## Slide 1 — Title

- **SIH26176 · ORCA**
- **FORESHORE — Marine foresight for the small-boat fleet**
- Theme: Disaster Management · ISRO / Department of Space
- Team name / ID
- One line under the title: *"SAMUDRA tells you what the advisory says. FORESHORE tells
  you what it means for your boat, tonight, and why."*

---

## Slide 2 — Proposed Solution

- **Two surfaces, one agent core.** Diagram:
  ```
  Boat UI (Tamil, voice-first)    Shore console (English, fleet view)
              \                              /
               \____ presentation layer ____/
                          |
             Agent orchestration (planner -> specialists -> synthesis)
                          |
             Tool layer (typed, deterministic, provenance-emitting)
  ```
- **Three verdicts only:** `GO` / `GO_WITH_CAUTION` / `DO_NOT_ADVISE` — never a bare
  probability, never a hedge.
- **Refusal is a designed outcome**, not an error: `DO_NOT_ADVISE` hands off to a named
  human authority (nearest landing centre + Coast Guard 1554), never guesses.
- **Advisory ceiling invariant:** FORESHORE can be more cautious than the IMD Coastal
  Bulletin, never more permissive — enforced as a deterministic code check, not a prompt.
- What makes this different from a chatbot wrapper: real numbers with provenance, the
  three-source disagreement shown (not averaged), a real A* router, a proactive push
  path, and a system that can say no.

---

## Slide 3 — Technical Approach

- **Agent architecture**, in the PS's own vocabulary — five specialists, each with a
  restricted tool subset (restriction is what makes the collaboration real):
  - WeatherIntel · OceanAnalytics · GeospatialReasoning · RiskAssessment · Routing
- **Evidence panel screenshot** here: the three-source disagreement for Rameswaram —
  IMD `MODERATE; BECOMING ROUGH IN GUST` vs INCOIS `SWH 0.59 m` vs Open-Meteo `1.18 m`,
  verdict downgraded by the ceiling, shown with acquisition time + resolution for each.
- **A\* router**, not LLM-generated waypoints:
  ```
  cost = w_base + w_hs*(Hs/hs_max)^2 + w_wind*(wind/wind_max)^2
       + w_current*adverse_component + w_shallow*shallow_penalty(depth)
       + w_steep*steepness_penalty(Hs,period) + w_imbl*proximity_penalty(dist)
       = INF  if land / inside IMBL / inside an exclusion polygon
  ```
  8-connected grid, haversine heuristic, per-leg cost breakdown returned so the UI can
  explain *why* the route bends.
- **Named, live endpoints** (not "we will use satellite data"): IMD ACWC Chennai
  coastal bulletin, INCOIS PFZ_Automation `pfzlines`, INCOIS OSF wave nest (MWW3 forced
  by ECMWF with data assimilation, 11 km), Open-Meteo marine + forecast, GDACS cyclone
  polygons, Marine Regions IMBL segments — every one keyless.

---

## Slide 4 — Feasibility and Viability

- **The healthcheck table screenshot** — every source live, keyless, with resolution and
  cadence. This is the single most credible image in the deck: it proves the data was
  actually touched, not assumed. (`FORESHORE_MODE=live python scripts/healthcheck.py`)
- Risks, named plainly, each with a working mitigation already built:
  | Risk | Mitigation |
  |---|---|
  | IMD/MOSDAC registration approval latency | Every field already reachable keyless; registrations are upside, not a dependency |
  | Coarse global wave models | INCOIS's own 11 km assimilated coastal nest is the governing source, not a 28 km global model |
  | Offshore connectivity beyond ~10-12 km | Geofence proximity runs client-side from GNSS, no network required; push/hazard alerts use the same channel INCOIS's own SAMUDRA app depends on |
  | Venue wifi failure (internal round) | `FORESHORE_MODE=fixture` — rehearsed with the network physically off |
  | Tamil ASR error rate on fishing vocabulary (measured ~15-20%) | Deterministic keyword-cue intent classification, robust to transcription noise, not exact-match dependent |
- **Region-agnostic by config** — no coordinate, boundary name or language code in
  application logic; a second region (`gujarat_sir_creek.yaml`) swaps live in the demo.

---

## Slide 5 — Impact and Benefits

- **Console screenshot** here: fleet map, cyclone cone overlay, alert queue with an
  unacknowledged vessel — the disaster-management artifact, not a consumer app filed in
  the wrong theme.
- **The IMBL detention problem, named specifically:** the 1974 India-Sri Lanka historic
  waters boundary (Palk Bay) and the 1976 maritime boundary are kept as *distinct* legal
  geofence classes with distinct lead distances (2.0 nm warn / 0.5 nm critical) — this is
  the single most common way a small boat from this coast is detained.
- **The push path**, proactive, not request-response: background scan over tracked
  vessels, geofence-approach and hazard alerts fired before anyone asks — the PS's own
  "proactive" and "when approaching" bullets.
- **The productivity diagnostic** — "why has fish productivity declined here?" answered
  from a multi-year chlorophyll/SST/Argo-subsurface trend, with ISRO Oceansat-2 OCM
  instrument provenance. The query a precomputed advisory app cannot structurally answer.
- Who is protected: small motorised boats, vallams and mechanised trawlers alike (three
  vessel classes, distinct thresholds from measured capsize-risk literature, in
  `config/vessels.yaml` — never hardcoded).

---

## Slide 6 — Research and References

- IMBL treaty citations: 1974-06-28 Agreement on the Boundary in Historic Waters
  (Palk Bay, line 1306); 1976-03-23 and 1976-11-22 agreements (Gulf of Mannar / Bay of
  Bengal, lines 1307/1310/1311) — via Marine Regions (`geo.vliz.be`), each segment
  carrying its own agreement name and date as a data attribute.
- INCOIS Ocean State Forecast: MWW3 wave model forced by ECMWF, **with data
  assimilation** — `incois.gov.in/thredds`, 0.1° (~11 km) coastal nest, 3-hourly, 7-day.
- IMD ACWC Chennai Coastal Bulletin — `mausam.imd.gov.in`, 12 h validity, the system's
  advisory ceiling.
- Douglas sea-state scale (WMO) — the descriptor-to-Hs-band mapping that makes the
  ceiling enforceable against IMD's own text bulletin.
- GEBCO bathymetry; ISRO Bhuvan basemap (`bhuvan-vec1.nrsc.gov.in`).
- GDACS (JRC) cyclone track and cone-of-uncertainty polygons.
- ISRO Oceansat-2 OCM / IRS-P4 OCM ocean-colour chlorophyll via INCOIS ERDDAP — the
  productivity diagnostic's own instrument provenance.

---

## Screenshot checklist (capture per `docs/DEMO_SCRIPT.md`, save to `docs/artifacts/`)

- [ ] `healthcheck_table.png` — `scripts/healthcheck.py` live output, all-green
- [ ] `evidence_panel_disagreement.png` — three-source sea-state disagreement + ceiling downgrade
- [ ] `route_cost_breakdown.png` — A* route bending around IMBL/reef, cost panel open
- [ ] `console_fleet_cyclone.png` — console fleet map with cyclone cone + alert queue
- [ ] `trace_inspector.png` — full reasoning trace with provenance for one answer
- [ ] `region_swap.png` — live proof query against the swapped Gujarat/Sir Creek region
