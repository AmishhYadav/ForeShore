# FORESHORE

**Marine foresight for the small-boat fleet.**

An agentic marine intelligence platform for small-boat fishermen on the Palk Bay / Gulf of
Mannar coast, with a shore-side control console for fisheries and Coast Guard operators.
Two surfaces, one agent core.

Built for **Smart India Hackathon 2026**, problem statement **SIH26176 "ORCA"** — Indian Space
Research Organisation / Department of Space. Theme: Disaster Management.

---

## The idea in one line

> SAMUDRA tells you what the advisory says. FORESHORE tells you what it means for your boat,
> tonight, and why.

INCOIS already ships a multilingual app that delivers precomputed advisories. What it
structurally cannot do is answer an arbitrary question by correlating sources, plan a route over
a weather-and-exclusion cost field, diagnose *why* fish productivity declined, show the evidence
behind a recommendation, or refuse to answer and explain why. The problem statement asks for all
five. FORESHORE is the reasoning layer above what SAMUDRA already publishes.

## What it does

- **Answers in Tamil, by voice.** Language is auto-detected and mirrored — the user is on a boat
  at night with wet hands.
- **Gives a verdict, not a number.** `GO` / `GO_WITH_CAUTION` / `DO_NOT_ADVISE`, with an
  advisory-ceiling invariant that can never be more permissive than IMD's human-issued coastal
  bulletin, enforced as a deterministic post-check.
- **Shows its evidence.** Every answer carries a panel naming each source, its authority, its
  acquisition time, its spatial resolution and its freshness. Nothing is labelled "current" that
  isn't.
- **Plans real routes.** A\* over a weighted cost field — wave height, wind, currents, wave
  steepness, bathymetry, cyclone exclusion polygons, and a soft penalty approaching the
  international maritime boundary. Never LLM-generated waypoints.
- **Pushes, rather than waits.** A background loop over tracked vessel positions fires
  geofence-approach and hazard alerts. Geofence proximity runs client-side from GNSS, so it
  still works with no signal.
- **Distinguishes its geofences.** The 1974 India–Sri Lanka *historic waters* boundary, the 1976
  maritime boundary, the Gulf of Mannar marine national park, and ecologically sensitive coral
  and seagrass zones are four different legal and practical regimes, not one "restricted zone".

## Design commitments

- **No unsourced numbers.** Every quantitative claim traces to a retrieved record with a source,
  a timestamp and a resolution. The model never supplies values from its own knowledge.
- **Disagreement is shown, not averaged.** IMD's bulletin, INCOIS's 11 km assimilated wave model
  and Open-Meteo's 28 km global model routinely disagree about a shallow bay. The system
  displays all three and explains which governs.
- **Refusal is a designed outcome.** `DO_NOT_ADVISE` handles missing, stale or contradictory
  input, and hands off to a named authority rather than guessing.
- **Region-agnostic by config.** No coordinate, boundary name or language code appears in
  application logic.

## Data

Every source the demo depends on is public and keyless. Full verified table in `CLAUDE.md`.

| | |
|---|---|
| **IMD** | Coastal bulletin (the advisory ceiling), district nowcast and lightning, AWS observations, cyclone track |
| **INCOIS** | Official PFZ advisory lines, Ocean State Forecast (MWW3 forced by ECMWF with data assimilation, 11 km coastal nest), currents, winds, SST, chlorophyll, coral/seagrass/mangrove zones, harmful algal bloom sectors, landing centres, Argo subsurface series back to 2004 |
| **Open-Meteo** | Tide, currents, cross-check waves, wind, gusts, CAPE |
| **GDACS** | Cyclone cone of uncertainty and severity polygons |
| **Marine Regions** | India–Sri Lanka maritime boundary, as four treaty-typed segments carrying their own agreement citations |
| **GEBCO / Bhuvan** | Bathymetry and ISRO basemap |

## Stack

Python (FastAPI, xarray, shapely, numpy) · PostGIS · TypeScript/React · MapLibre GL ·
Anthropic Claude with a hand-rolled planner → specialists → synthesis loop over typed,
deterministic tools.

## Repository

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Operational context, invariants, verified endpoints, conventions |
| `PROJECT_CONTEXT.md` | Background, problem statement analysis, competitive position |
| `PLAN.md` | Phased implementation plan — contracts, execution order, demo script, risk register |

## Status

Pre-implementation. Data sources verified live 2026-08-30; architecture and contracts locked;
build phases defined in `PLAN.md`.
