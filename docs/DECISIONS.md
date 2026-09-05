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

---

## D8 — A partly-missing geofence layer set degrades per class, not wholesale

**Finding.** `check_geofences` abstained entirely whenever *any* required static layer was
absent. INCOIS's MHW GeoServer returns intermittent 503s (observed 2026-08-31 for
`MANGROVE_ZONE_DISS`), so a flaky **advisory** habitat layer was masking the **legal**
boundary check — the system could not say a vessel was 0.2 nm from the 1974 line because
it had no mangrove polygons.

**Decision.** Abstain wholesale only when *nothing* is checkable. Otherwise check every
layer that is present, mark the result `partial`, and name the classes that went
unchecked. Missing layers are split by consequence: an absent `IMBL_*` layer means the
maritime boundary itself is unverified and is reported as a missing input, while an absent
`ECO_SENSITIVE` layer degrades the advice and is reported as a note. "No fences nearby"
and "cannot check" remain different answers, which was the original point of the guard.

---

## D9 — A bulletin cannot authorise a trip outside its own validity window

**Finding.** Asking "can I go out tomorrow morning?" against today's bulletin correctly
yields `DO_NOT_ADVISE`: the IMD Coastal Bulletin's validity is 12 h, and tomorrow morning
falls outside it.

**Amended 2026-09-05.** The original implementation reached that verdict by passing the
*time being asked about* into the ceiling as its wall clock, which meant a bulletin that
was still perfectly current reported itself as having "expired 7.0 hours ago". The verdict
was right and the stated reason was false — and that reason is what the fisherman reads.

`CeilingInput` now carries the two separately: `now` (wall clock — is the bulletin
FORESHORE holds still current?) and `target_time` (the departure the user named — does the
bulletin's window even cover it?). They fire different rules, `bulletin_expired` and
`bulletin_does_not_cover_departure`, with different copy. Both still cap at
`DO_NOT_ADVISE`; the abstention is unchanged, only the sentence is now true. Covered by
`backend/tests/test_ceiling.py::test_a_current_bulletin_is_not_reported_as_expired_when_asked_about_later`.

**Consequence for the demo.** The Phase 8 demo script's opening beat asks about tomorrow
morning and expects an amber verdict. Against a live bulletin that beat will abstain, which
is correct behaviour and a weak opening. The frozen fixture must therefore be a bulletin
whose validity window covers the time the demo query asks about — or the query asks about
the window the governing bulletin actually covers. Do not "fix" this by relaxing the
staleness rule.

---

## D10 — Scenario exploration re-uses `answer()` twice rather than adding a second pipeline

**Finding.** PLAN.md Phase 7 item 4 asks for "what if I leave at 04:00 instead of
06:00" as "a re-plan over the same evidence with a diffed verdict." The planner already
classified a `scenario` intent (`INTENT_CUES["scenario"]`) but nothing downstream ever
acted on it — no time-comparison logic existed anywhere in `agents/` before this.

**Decision.** `agents/planner.py::resolve_scenario_times` triggers only when the
utterance names **two** explicit `HH:MM` times — the exact shape of the PS's own
example — never on "earlier"/"later" cues alone, since those name no second instant to
compare against. When it fires, `agents/orchestrator.py::answer()` runs itself
recursively once per candidate time (`_build_scenario`), each a complete, independent,
`use_model=False` answer — same tools, same ceiling, same evidence discipline as an
ordinary query, never an LLM asked to imagine how the two might differ. The outer
response *is* the earlier option's own `QueryOutcome` with a `scenario` field attached,
so a client that ignores `scenario` entirely still renders a correct, complete answer.
An explicit `when` on the request always suppresses scenario detection — a caller who
names an exact instant gets an answer for exactly that instant, never an unrequested
comparison. See `docs/API.md`'s "Scenario exploration" section for the wire shape.

**What we do not do.** We do not have the LLM compare the two times qualitatively —
every difference reported is read off two real, independently-computed `Verdict`
objects. We do not run specialist reasoning for either option, since the comparison
itself is the deliverable, not extra prose, and skipping it keeps a scenario question
fast and fully deterministic even with a model configured.

---

## D11 — Two fixture-mode determinism bugs, both from keying a cache on "now"

**Finding.** Calling `get_sea_state` repeatedly for "right now", in
`FORESHORE_MODE=fixture`, non-deterministically reported `openmeteo_marine` as missing
on roughly half of otherwise-identical calls, and — through the real request path (an
orchestrator-resolved, explicit `when`, which is what every actual query sends, never
`None`) — reported `incois_osf_wave`/`incois_osf_mwh` as missing **every time**, not
flakily. Both broke CLAUDE.md's invariant 7 in the same way: a value derived from
calling the clock twice, independently, became part of a cache/fixture key.

1. `sources/openmeteo.py::_window_for` computed `delta_h` against its own fresh
   `utcnow()` call, a few microseconds after `.at()` had already resolved its own "now"
   for `when`. The two nearly-simultaneous clock reads occasionally disagreed by a hair,
   flipping `_window_for`'s sign branch and changing the `forecast_hours`/`past_hours`
   request params — which are part of the fixture key. Only one of the two possible
   values was ever actually frozen.
2. `sources/incois_thredds.py::_binary_key` hashed a `time_start`/`time_end` window
   (`at ± 6h`) into the grid-fetch cache/fixture key. Every real query supplies an
   explicit, freshly-resolved `when` (`agents/planner.py` always sets one), so this
   window is unique to the microsecond on almost every call — the key essentially never
   repeats, so a frozen fixture for it essentially never exists. This is the more
   serious of the two: it silently dropped INCOIS's own 11 km assimilated model — the
   source CLAUDE.md calls authoritative and the evidence panel's centrepiece — from
   *every* real query in fixture mode, deterministically, not just flakily.

**Decision.** Both fixed the same way: stop deriving a cache/fixture key from anything
that depends on when "now" happened to be read. `_window_for` now takes `now` as an
explicit argument from its caller instead of reading the clock itself; `.at()` captures
exactly one `now` and reuses it as both the reference instant and (when the caller asked
for "now") as `when` itself. `_binary_key` no longer hashes `time_start`/`time_end` at
all — cache/fixture identity is `(urlPath, raw_vars, bbox)`, since `urlPath` already
names the specific day's file (the actual determinant of what data exists); the ±6h
window remains on the *live* NCSS request only, as the bandwidth-optimisation it always
was, never as a second axis of cache identity. `cache_ttl_s` (1 h) stays comfortably
inside the ±6h live-request window, so this does not introduce a live-mode staleness
risk it didn't already have.

**What we do not do.** We do not round either value to a coarse time bucket as a
compromise — a bucket boundary is still a boundary, and a demo run straddling it hours
after the morning `freeze_fixtures.py` would fail the exact same way for the exact same
reason. The fix removes the clock from the key entirely rather than making the flakiness
rarer.

**Consequence for `data/fixtures/`.** Every previously-frozen `incois_osf_*` and
`openmeteo_marine` blob was keyed the old (broken) way and is now orphaned; not one of
them still matches. `scripts/freeze_fixtures.py` was re-run in live mode after this fix
landed and the whole snapshot was refrozen — do this again after touching either
adapter's request-building logic, not just after touching the ceiling or verdict paths.
