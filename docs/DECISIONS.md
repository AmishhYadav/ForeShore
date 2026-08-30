# FORESHORE — decisions taken during implementation

Decisions made while building that were not settled in `PLAN.md`, with the evidence that
forced them. Each one is written so it can be defended in a judging Q&A.

---

## D1 — Chlorophyll: the INCOIS OSF `chl` feed does not cover this basin

**Finding.** `incois.gov.in/thredds/catalog/osf/chl/` publishes exactly two rolling files,
named `VIIRS-SNPP-Roll-<start>-<end>-4KM-PICountries-CHL.nc`. `PICountries` is *Pacific
Islands Countries*: the published grid spans roughly 130–215°E. Palk Bay is at 79°E. The
product is real, current and 4 km — and it is for the wrong ocean. Probed 2026-08-31.

**Consequence.** The plan assumed this feed for both the derived-PFZ cross-check (tool 8)
and the productivity diagnostic (tool 13). It cannot serve either.

**Decision.**

*Tool 8, derived PFZ cross-check* — derive from **INCOIS SST frontal gradients**
(`osf/sst`, `SST_NIO_YYYYMMDD.nc`, live, ~1–2 day lag), which is one of the two signals
INCOIS's own operational PFZ derivation uses. Where a chlorophyll field is available for
the date it is used as the second signal; where it is not, the tool says so explicitly and
derives on the front alone. It never silently drops a signal, and the output stays
labelled derived and indicative.

*Tool 13, productivity diagnostic* — use **ISRO ocean-colour chlorophyll** from INCOIS
ERDDAP instead, which turns out to be better suited than the OSF feed ever was:

| dataset | instrument | coverage | extent | variables |
|---|---|---|---|---|
| `incois_oceansat2_datasets` | **Oceansat-2 OCM (ISRO)** | 2011-02-02 → 2020-05-01 | 46.7–99.3°E, 0.1–27.9°N | `CHL`, `KD490`, `TSM` |
| `IRS_chlorophyll_datasets` | **IRS-P4 OCM (ISRO)** | 2003-01-05 → 2006-03-21 | 60.0–103.9°E, 0.02–26.0°N | `CHLOROPHYLL` |
| `incois_argo_10d_VAM` | Argo objective analysis | 2004-01-10 → 2026-07-30 | 30.5–119.5°E, ±29.5°N | `TEMP`, `SAL` |

The diagnostic is a multi-year question, so archival coverage is what it actually needs.
This also gives the submission genuine **ISRO instrument provenance** rather than a claim
of it — the PS is filed by the Department of Space, and Oceansat OCM is their sensor.

**What we do not do.** We do not present a chlorophyll number for today that we cannot
source, and we do not substitute a different basin's data. Where the series ends, the
narrative says where it ends.

---

## D2 — `osf/mwh` (maximum wave height) is returning all-NaN

**Finding.** Both catalog dates available on 2026-08-31 (20260828, 20260829) contain
`MAXW` arrays that are NaN across the entire published domain. This is an upstream
outage, not a subsetting bug.

**Decision.** `mwh` degrades as a single product; the adapter stays healthy on the
authoritative `wave` product (`SWH`, `SWELL`, `WP`, `SWP`), which is populated. Maximum
wave height is reported as unavailable rather than back-filled from `SWH`. Re-probe each
morning via `scripts/healthcheck.py`; if it returns, nothing needs changing.

---

## D3 — IMD GeoServer rejects `BBOX` and `CQL_FILTER` together

**Finding.** `reactjs.imd.gov.in` returns `"bbox and cql_filter both specified but are
mutually exclusive"` for **any** combination, including an attribute-only CQL filter.

**Decision.** Each named fetcher picks exactly one: BBOX for region-scoped nowcast and AWS
queries, an unscoped CQL filter when a specific district is named, and no filter at all
for the cyclone track — a cyclone that matters is usually still outside the regional bbox
while it is approaching.

---

## D4 — IMD coastal office ids, and IMD's own spelling

**Finding.** Probed all seven office ids. `id=6` is ACWC Chennai (North/South Tamilnadu
coast). Gujarat is `id=3`, CWC Ahmedabad — and IMD spells its coast blocks **"North
Gujrath coast"** / **"South Gujrath coast"**, not "Gujarat".

**Decision.** Both live in `config/regions/gujarat_sir_creek.yaml`. The region-swap demo
depends on this being right, and it is the kind of detail that makes a swap look real
rather than staged.

---

## D5 — PostGIS is an accelerator, not a dependency

**Finding.** Docker was not running when the store layer was built, and a venue demo
cannot depend on it.

**Decision.** `VectorStore` has two backends behind one interface. The **file** backend
(committed GeoJSON in `data/static/` indexed with a shapely STRtree) is the default and is
complete on its own; PostGIS is used when `FORESHORE_PG_DSN` answers. `docker-compose.yml`
ships the service and it is running, so the PostGIS path is demonstrable — but no demo
beat depends on it. The trace store behaves the same way: JSONL is authoritative for
reads, Postgres is a mirror.

---

## D6 — Geofence ETA is sampled along the track, against the geometry

**Finding.** The obvious implementation — project the vessel forward one hour and compare
the endpoint distance — reports "not closing" for a boat that crosses the boundary and
keeps going, because it ends the hour further away than it started. That is exactly the
vessel about to be arrested.

**Decision.** Walk the projected track in 40 steps, measure each step against the fence
**geometry** (the nearest point slides along a long boundary on an oblique approach), and
report time to closest approach. Closure slower than 0.25 kn is suppressed, because a boat
running parallel to a meridian-aligned boundary closes on it very slightly as the
meridians converge, and an ETA for that is noise. ETAs beyond four projection horizons are
not shown at all rather than shown as a number nobody will act on.

---

## D7 — The system produces a correct verdict with no LLM at all

**Decision.** The verdict is computed deterministically from `config/vessels.yaml`
thresholds over sourced observations; the LLM may make it **more** cautious and never more
permissive, and that is enforced in `verdict/engine.py` rather than requested in a prompt.
The advisory ceiling then runs last. With no `ANTHROPIC_API_KEY`, `agents/runtime.py`
falls back to a deterministic scripted client that runs the same loop over the same tools
and produces the same trace; only the prose is poorer.

Two reasons. It removes the API from the demo's critical path, and it makes the safety
argument checkable: the decision is in code a reviewer can read, not in a prompt they
have to trust.
