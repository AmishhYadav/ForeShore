# FORESHORE — Upgrade Plan (Phases 10–13)

**Target: 30 September 2026 SIH portal PDF.** ~4 weeks.
Constraints confirmed: software only, laptop only, secondary research only.

**Relation to `PLAN.md`.** `PLAN.md` is the phase 0–9 master plan and is the historical
record of what was built; it is not superseded and is not edited. This file covers
**phases 10–13**: closing the PS capability gaps that survived phase 9, and the one
differentiator the submission does not yet have.

**Status: approved, not started.** Implementation has not begun.

**Start timing (decided):** begin immediately, **additive only**. Every Phase 10 item is
a new file or a new optional field. Nothing on the 8 Sept demo path is modified until
after the internal round. `git status` must stay clean of changes to `orchestrator.py`,
`verdict/engine.py`, `verdict/ceiling.py`, `push/loop.py` and both UI routes until 9 Sept.

---

## Context

FORESHORE is built and working: ~29k lines, phases 0–7 complete, 217 passing tests,
16 provenance-emitting tools, 8 live source adapters, deterministic verdict engine +
advisory ceiling, A* router, 6 geofence classes, push loop, boat UI + shore console.

Two things prompt this upgrade.

**1. A literal re-read of the PS finds five capability gaps.** Not polish — bullets
written into the Expected Solution that the build does not meet (Part 1).

**2. The differentiators are all "we did the obvious thing properly."** Evidence panel,
real router, abstention, no-unsourced-numbers. Excellent engineering discipline, and it
wins a college round. But on 30 Sept six slides are read with no demo attached, next to
~100 other decks, by the organisation that wrote the PS. Discipline is hard to see on a
slide. It needs one idea that is *unmistakably* not in the other decks (Part 3).

**Sequencing note.** The internal round is ~8 Sept, three days out. Phase 10 is additive
only and cannot regress the working demo — see the start-timing rule at the top.

---

## Part 1 — PS compliance audit (code-verified)

| # | PS capability bullet | Status | Evidence |
|---|---|---|---|
| 1 | Understand intent in natural language | **MET** | `agents/planner.py`, 9 intents, EN + Tamil script + romanised |
| 2 | Auto-identify language, respond in same; Indian regional languages | **PARTIAL** | `agents/language.py` detects 8 Indic scripts; `synthesis.py` `VERDICT_COPY`/`LABELS` carry only `en`/`ta`/`gu`. A Malayalam query is *detected*, then answered in English. |
| 3 | **Contextual, multi-turn conversations to refine queries** | **NOT MET** | `POST /api/query` is stateless (`api/routes_query.py:62`). No session id, no history anywhere in the repo. `orchestrator.answer()` clears the evidence bus every call (`orchestrator.py:378`). Scenario support is one narrow case: two explicit `HH:MM` times (`DECISIONS.md` D10). **Largest single gap.** |
| 4 | Autonomously discover, retrieve, integrate datasets | **MET** | tool 16 `list_available_data` |
| 5 | Spatial/temporal/contextual reasoning across heterogeneous sources | **MET** | `verdict/engine.py::governing()`, three-source disagreement |
| 6 | Explainable recommendations via maps, **charts**, geospatial visualizations | **PARTIAL** | Maps yes. **No chart library in `frontend/package.json`, no chart component in `src`, no time-slider, no grid rendering.** The multi-year productivity diagnostic renders as *plain text*. A named PS noun is simply absent. |
| 7 | Proactive alerts for adverse **weather, high waves, lightning**, cyclones | **PARTIAL** | `push/loop.py` triggers on geofence + cyclone-polygon proximity **only**. No sea-state / wind / lightning threshold trigger. A boat sitting still while the forecast deteriorates past its own vessel limit gets no alert. Three of four named hazard types cannot fire a push. |
| 8 | Geofencing notifications | **MET** | 6 distinct classes, offline client-side check |
| 9 | Route optimisation, safe navigation, **operational planning** | **PARTIAL** | Routing is real and strong. System answers "can I go *now*" — never "when can I go", "how long have I got", "when must I turn back". |
| 10 | Recommendations with supporting evidence and reasoning | **MET** | Evidence panel, trace inspector, provenance invariant under test |

Also:

- **PS title is "ORCA — Marine EcOsystem Reasoning with Collaborative Agents"**, and PS
  catalogues paraphrase it as *"ten cooperating AI agents."* `tools/registry.py:25-36`
  declares exactly **10** specialists; `agents/specialists.py` defines **8**.
  `PlanningAgent` exists only as a trace label; `UserInteraction` is referenced nowhere.
  `GET /api/architecture` therefore advertises two agents that do not exist. Given the PS
  title, this is the first thing a judge counts.
- **Stakeholders:** PS names five (fishermen, researchers, coastal authorities, disaster
  management, maritime operators). Two built — a deliberate, defensible cut
  (`PROJECT_CONTEXT.md:400`). "Researchers" is nearly free given the archival
  Argo/Oceansat pipeline already exists.

### Self-documented defects to close

- Trace inspector shows full provenance **only for current-session queries**
  (`console/TraceInspector.tsx:20-29`) — needs evidence persisted alongside `TraceStep`.
- Simulated fleet **does not relocate on region swap** (`RegionSwitcher.tsx:162`) — the
  region-swap beat shows Gujarat boundaries with Palk Bay boats.
- `AlertStore` in-memory only; restart loses the queue.
- Bhashini adapter is a stub; no backend speech path.
- `docs/artifacts/` is **empty** — zero screenshots, no PDF.

---

## Part 2 — What the field actually lacks (evidence-backed)

From research across CMFRI/ICAR/ICSF/INCOIS literature and post-Ockhi reviews. Full
citations in the research appendix; the load-bearing ones:

1. **The communication gap is the #1 documented pain.** Formal channels (mobile, VHF)
   die at 10–20 km. Fishing and hazard grounds are 100–150 km out. This single mismatch
   is the stated justification for GEMINI, DAT-SG *and* Nabhmitra — three separate
   national programmes — and it is still unsolved: GEMINI is **one-way only** (no
   emergency recall; satellite phones are banned in India for security reasons), and
   DAT-SG has ~20,000 units against a ~900,000 population.
2. **Ockhi's failure was last-mile, not forecasting.** IMD issued bulletins; what reached
   people was "seas are rough, winds 70 km/h" — not actionable. Boats already weeks into
   a trip were unreachable, so 24–48 h lead time "holds no relevance." Casualty counts
   for one district range from 12 to 400 depending on source.
3. **Crossings into Sri Lankan waters are often not deliberate.** Documented case: 14 TN
   fishermen drifted across the IMBL while repairing an engine failure; drift with gear
   deployed and engine idle runs ~3 km/h. The boundary is *visible* on GPS but nothing
   *interrupts* a fisherman's attention as he drifts. ~529 arrested (2024), ~346 + 44
   trawlers seized (2025). At least six academic IMBL-alert prototypes since 2017; none
   fielded.
4. **PFZ advisories are pelagic-only and coarse.** Peer-reviewed: positive catch
   relationship for ring-seine/gillnet/trolling, "negligible" for bottom trawling.
   Format is a port name plus a general lat/long. A Mangaluru fisherman: must visit the
   Fisheries Department office to get it — it is not available live at sea. Measured
   adoption ~30% (Odisha field survey, 18 landing centres).
5. **Non-compliance is economic, not informational.** *"Should we starve to death, or die
   at sea."* And: *"Every year we get 70–80 warnings. If we adhere to each, a fisherman
   will not be able to go fishing at all."* A tool that only says NO more loudly does
   nothing. **This is the strongest argument for the innovation below**: fishermen need
   to be told *when they CAN go*, not only when they cannot.
6. **No single Indian tool unifies PFZ + weather + boundary + distress.** Stated
   verbatim in the academic literature reviewing the app landscape.
7. **Low literacy does not mean charts are unusable.** A structured-interview study of
   semi-literate Indonesian tuna fishers found they read graphs, tables and maps fine
   when complexity is graduated. Removes the objection to Part 3's chart work.
8. **Prior art to name and beat:** *Jal Anveshak* (arXiv 2411.10050) — a fine-tuned
   Llama-2 fishing-zone chatbot. It has the LLM emit predictions directly, with no
   provenance, no ceiling, no abstention. It is the exact inverse of FORESHORE's
   invariant, and it is an unreleased preprint. Cite it as the contrast case.
9. **Empirical support for the core architecture choice:** *"Automating Ecological and
   Fisheries Modelling With Agentic AI"* (Fish and Fisheries, 2026) tested coding agents
   on three canonical fisheries workflows and found they **produce logically flawed code
   and inconsistent answers without expert oversight.** This is published evidence for
   FORESHORE's rule that the LLM selects and sequences tools but never does the
   arithmetic. Put it on Slide 3 — it converts a design opinion into a cited finding.

---

## Part 3 — The innovation

### The finding that drives it

I pulled **ISRO-IRNSS-ICD-MSG-INCOIS-1.2** (*Signal-in-Space ICD for INCOIS Messages via
NavIC Messaging Service*, U.R. Rao Satellite Centre, June 2020) and extracted the message
structure directly from the PDF. Verified, from ISRO's own document:

```
Sub-frame, 292 bits (before FEC + sync):
  TLM 8 | TOWC 17 | RESERVED 5 | MESSAGE ID 6 | DATA 220 | RESERVED 6 | CRC 24 | Tail 6
  50 symbols/s, 600-symbol sub-frame  ->  12 s per sub-frame
  64 Message IDs available; INCOIS already allocated:
      ID 20 = Potential Fishing Zone / TUNA-PFZ
      ID 21 = warnings (Tsunami / Cyclone / High Wave)
  Service IDs:  High wave 0111 | Cyclone 1111 | Tsunami 0011 | No Warning 1100

High Wave Alert, the full 220-bit payload as ISRO defines it today:
  ServiceID 4 | SegCount 4 | SegID 4 | HWA1Clear 1 | HWA2Clear 1 | SPARE 34
  | PortName1 8 | HWAmsg1 78 | PortName2 8 | HWAmsg2 78

  HWA message (78 bits):
    Region 4 | Site-1 8 | Site-2 8 | WaveHt min 8 | WaveHt max 8
    | Current min 6 | Current max 6 | Date 16 | Time 11 | Validity 1 | MsgText 2
```

Read what that payload actually carries: a region, two site codes, a wave-height range,
a current-speed range, a timestamp, and **two bits of message text**. It is a broadcast
of **conditions**, area-wide, one-way, and identical for every boat in the region. It has
**34 spare bits**.

But FORESHORE's entire thesis is that a condition is not a decision. 1.4 m is `GO` for a
mechanised trawler and `DO_NOT_ADVISE` for a vallam — that is literally what
`config/vessels.yaml` encodes. The channel ISRO already flies to fishing boats cannot
carry a decision, because it was designed to carry a measurement.

### The idea

> **Reasoning ashore. Decision aboard.**
>
> FORESHORE compiles its full agentic reasoning — every source, every threshold, the
> binding constraint and the handoff — down to a **220-bit payload that fits ISRO's
> existing NavIC sub-frame unchanged**. Same ICD, same bit budget, same 12-second
> cadence, same allocated Message ID space. The expensive reasoning happens ashore where
> there is compute and connectivity. What crosses the satellite link is the *decision*,
> not the data.

This is defensible in a way almost nothing else in a hackathon deck is: you either opened
the ICD and did the bit-packing, or you did not. It cannot be hand-waved, cannot be
faked in a slide, and is invisible to any team that did not read ISRO's own document.
It is software-only and laptop-only. And it answers the #1 documented gap in the field
(Part 2 items 1 and 2) using the channel the PS owner already operates.

### The other half — the Decision Envelope

To fit a decision into 220 bits you must first *have* a decision object that is small,
self-contained and time-extended. So FORESHORE stops returning a verdict for an instant
and starts returning a **safe-operating window**.

For each 3-hour step across the INCOIS OSF 7-day horizon, for *this* vessel class:
the verdict, the **binding constraint** (which single threshold is saying no), and the
**margin** to it.

`sources/incois_thredds.py::series()` already returns exactly this series — `SWH`,
`SWELL`, `WP`, wind, current — with provenance, from one grid fetch. The thresholds are
already in `config/vessels.yaml`. The engine is already deterministic. This is assembly,
not new science.

One object, and it closes four gaps at once:

| It yields | Closes |
|---|---|
| A verdict band over time = **a chart** | PS bullet 6 (charts — absent) |
| "Latest safe departure", "**turn back by 11:20**", "next GO window opens Thu 14:00" | PS bullet 9 (operational planning — absent) |
| An alert when the envelope's **edge moves** | PS bullet 7 (weather/wave push triggers — absent) |
| Binding constraint + margin = a **counterfactual** | The explainability emphasis (×3 in the PS) |
| A 24 h envelope bit-packs into ~60 bits | Makes the NavIC packet possible at all |

The turn-back time is the direct Ockhi lesson: the boats that died were already at sea,
and a 48-hour lead time was irrelevant to them. And per Part 2 item 5, an envelope tells
a fisherman **when he CAN go** — which is the thing that makes an advisory economically
survivable instead of one more warning to ignore.

### Explainability, made concrete

The envelope makes explanation *structural* rather than narrated. Every verdict gains
two deterministic, templated lines — no extra LLM call, both read off the existing
threshold table:

- **Contrastive:** *"Capped by the storm-surge warning for Ramanathapuram — not by sea
  state. Sea state alone would have been GO_WITH_CAUTION."*
- **Counterfactual:** *"Hs is 1.62 m; your GO limit is 1.25 m. You need 0.37 m less. It
  drops below at 14:00 tomorrow."*

Both forms are validated by 2025–26 maritime-XAI research: mariners want contrastive,
domain-native explanations, not model mechanics, and want confidence in maritime-familiar
terms. This is the difference between "here is a trace" and "here is *why*, and *what
would change it*."

### One-line deck framing

> Every existing system tells a fisherman **what the sea is doing**.
> FORESHORE tells him **what he should do, when the window opens, and what would have to
> change for the answer to be different** — and compresses that answer small enough to
> reach a boat with no signal, over the satellite India already flies.

---

## Part 4 — Work plan

Delegation follows `CLAUDE.md`: Opus fixes contracts and safety logic; Sonnet subagents
implement against them. Batch independent subagents in parallel.

### Phase 10 · Week 1 (5–11 Sept) — additive only, cannot break the 8 Sept demo

**W1.1 — Decision envelope contract + engine** *(Opus writes: safety-critical)*
- `backend/foreshore/verdict/envelope.py` — new. `DecisionEnvelope`, `EnvelopeStep`
  (`when`, `level`, `binding_constraint`, `margin`, `margin_unit`, `evidence_ids`).
- Reuses `verdict/engine.py::evaluate()` per step — must **not** re-implement thresholds
  or the ceiling. The ceiling applies per step, from the bulletin valid at that step;
  steps beyond bulletin validity are `DO_NOT_ADVISE` by D9, and that is correct, not a bug.
- Derived fields: `latest_safe_departure`, `turn_back_by` (given route ETA),
  `next_go_window`.
- Contract added to `models.py`.

**W1.2 — Tool 17 `get_decision_envelope`** *(Sonnet)*
- Registered against `RiskAssessment` + a new `PlanningAgent`. Provenance-emitting like
  every other tool; one `series()` fetch, not 56 point fetches.

**W1.3 — Complete the ten specialists** *(Sonnet)*
- Define `PlanningAgent` (tools: `get_decision_envelope`, `list_available_data`) and
  `UserInteraction` (clarification + readback) in `agents/specialists.py`.
- PS title says "Collaborative Agents"; the catalogue must not advertise agents that
  don't exist.

**W1.4 — Multi-turn conversation** *(Opus fixes contract, Sonnet implements)*
- `Query.session_id` + `ConversationStore` (JSONL, mirroring `TraceStore`'s pattern —
  file authoritative, Postgres optional).
- Carry forward: last position, last vessel class, last `when`, last verdict, last
  envelope. Pronoun/ellipsis resolution ("what about tomorrow?", "and for my brother's
  vallam?") resolved **deterministically in the planner**, not by an LLM re-reading
  history.
- Hard rule: history may supply *context*, never *values*. Every number still comes from
  a fresh tool call with fresh provenance. A cached number must never survive into a new
  answer — that would breach invariant 3 and invariant 4 simultaneously.

**W1.5 — AIFS as a fourth independent source** *(Sonnet)*
- **Verified myself:** `api.open-meteo.com` serves `ecmwf_aifs_025` — ECMWF's operational
  AI model, 0.25°, 15-day, 6-hourly, **keyless**. (Note: GraphCast is *not* on that
  endpoint; the research overstated this. Do not claim GraphCast.)
- Add as a cross-check source, never governing. Extends the existing three-source
  disagreement panel to four, and gives an honest "an AI weather model is a fourth
  opinion" line — **without** any claim to run a foundation model on a laptop, which
  would be exactly the kind of instantly-visible fake `CLAUDE.md` already warns about
  for routing.
- Source spread across the four feeds becomes a **confidence band** on the envelope.
  NOAA research: explicit uncertainty raises trust; colour-only warnings lower it.

### Phase 11 · Week 2 (11–18 Sept) — the NavIC packet and the charts

**W2.1 — `backend/foreshore/downlink/` — the ICD-conformant packet** *(Opus defines the
bit schema; Sonnet writes encode/decode + property tests)*
- `navic.py`: encode a `DecisionEnvelope` into a 220-bit payload laid out to
  ISRO-IRNSS-ICD-MSG-INCOIS-1.2, with segmentation via SegCount/SegID for long messages.
- Round-trip property test: `decode(encode(e)) == e` for every vessel class × verdict ×
  binding constraint, plus explicit loss accounting (what precision the bit budget costs
  — state it, never hide it).
- **Honesty rule, non-negotiable:** we do not transmit. The claim is *ICD-conformant
  encoding*, demonstrated by a decoder — never "we broadcast over NavIC." Label it
  exactly that way in the UI and on the slide. Overclaiming here would destroy the
  credibility the rest of the submission is built on.

**W2.2 — CAP 1.2 emitter** *(Sonnet)*
- `downlink/cap.py` — every push alert also serialises as OASIS CAP 1.2 XML, the format
  NDMA's SACHET already ingests from IMD/INCOIS/CWC and redistributes over GAGAN and
  NavIC. Same thesis as W2.1, shore-side: speak the pipes India already built.

**W2.3 — Charts** *(Sonnet)*
- Add a chart library. Three charts, all reading data that already exists:
  1. **Envelope band** — verdict over the 7-day horizon, binding constraint on hover.
     This is the headline visual and belongs on Slide 3.
  2. **Source disagreement** — four wave-height series overlaid, governing one marked.
  3. **Productivity trend** — the multi-year Argo/Oceansat series that currently renders
     as text.
- Graduated complexity per the Indonesian tuna-fisher study: boat UI gets the band alone;
  console gets band + constraint + spread.

**W2.4 — Envelope-edge push trigger** *(Sonnet)*
- `push/loop.py` gains a sea-state/wind trigger: fire when a tracked vessel's envelope
  transitions to a worse level within its horizon. Closes PS bullet 7 properly.

### Phase 12 · Week 3 (18–25 Sept) — explainability, hardening, evidence

**W3.1 — Contrastive + counterfactual cards** *(Opus — touches the ceiling's output
contract)*
- Templated off the existing `Threshold` table and `CeilingResult`. No new LLM call.
  Rendered in both surfaces and in the NavIC packet as a 4-bit binding-constraint enum.

**W3.2 — W3C PROV-conformant trace** *(Sonnet)*
- Map the existing trace onto PROV-DM: evidence = entities, tool calls = activities,
  specialists + the ceiling = agents. Serialisation change, not new capability. Converts
  "we log everything" into a named-standard conformance claim.

**W3.3 — Close the known defects** *(Sonnet, parallel batch)*
- Persist the evidence panel with `TraceStep` so historical traces render fully.
- Relocate the simulated fleet on region swap.
- Persist `AlertStore`.
- Reconcile `shared/types.ts` with the live backend.
- Language: either add `ml`/`te` copy, or make detection-without-copy degrade *visibly*
  ("detected Malayalam; answering in English — Malayalam copy not yet available"). Silent
  fallback is a broken claim; a stated limitation is not.

**W3.4 — Abstention + explanation eval** *(Sonnet)*
- A held-out scenario set (missing bulletin, expired bulletin, contradictory sources,
  no GPS, no cyclone) scoring abstention correctness and whether the stated binding
  constraint is the true dominant cause. Report the number on the slide.
- Framed as risk mitigation, not a solved problem: the XAI literature is genuinely mixed
  on whether explanations improve *calibrated* trust, and some show they increase
  uninformed over-reliance. Saying so is a strength, not a hedge.

### Phase 13 · Week 4 (25–30 Sept) — the artifact

**W4.1** — Re-freeze fixtures (mandatory after touching any adapter — `DECISIONS.md` D11).
**W4.2** — Capture every screenshot at final quality into `docs/artifacts/`. Currently empty.
**W4.3** — Rewrite `docs/DECK_CONTENT.md` around the new thesis, build the 6-slide PDF.
**W4.4** — Three timed end-to-end rehearsals, network off.

---

## Part 5 — What changes on the six slides

| Slide | Change |
|---|---|
| 1 Title | Use the PS's full title: *ORCA — Marine EcOsystem Reasoning with Collaborative Agents*. Add the new one-liner from Part 3. |
| 2 Solution | Lead with **"Reasoning ashore, decision aboard."** Two surfaces, one core, **one 220-bit downlink**. |
| 3 Technical | The **envelope band chart** as the hero image. The NavIC bit-layout diagram beside ISRO's own sub-frame structure, cited to ISRO-IRNSS-ICD-MSG-INCOIS-1.2. Cite Fish & Fisheries 2026 for why the LLM never does arithmetic. |
| 4 Feasibility | Healthcheck table, now four independent forecast sources including ECMWF AIFS, keyless. The round-trip decode test as proof the packet is real. |
| 5 Impact | Ockhi's last-mile failure; the 10–20 km vs 100–150 km gap; GEMINI is one-way; DAT-SG reaches ~2%. Arrest figures. Then: this is the gap the 220-bit packet targets. |
| 6 References | Add ISRO ICD, the 1974/1976 treaties, CMFRI PFZ-adoption studies, Fish & Fisheries 2026, the maritime-XAI papers, W3C PROV, OASIS CAP 1.2. |

---

## Part 6 — Verification

- `pytest backend/tests` — existing 217 stay green. New: envelope step-wise ceiling
  application; NavIC encode/decode round-trip across every vessel class × verdict ×
  constraint; conversation history never supplies a number (extends the existing
  `test_provenance.py` invariant); CAP 1.2 schema validation.
- `scripts/healthcheck.py` green in both modes, now including AIFS.
- Full demo in `FORESHORE_MODE=fixture`, **network physically off**.
- Adversarial pass: ask something unanswerable, confirm `DO_NOT_ADVISE` + named handoff.
- New drill: encode an envelope, decode it in a separate process, diff against the
  original. That diff is a demo beat and a slide.

---

## Part 7 — Risks and things I could not verify

- **Theme discrepancy.** A third-party PS catalogue files SIH26176 under *Miscellaneous*,
  not *Disaster Management* as `CLAUDE.md` states. **Check the SIH portal directly** —
  it could change which rubric applies and how hard to lean on the disaster framing.
- **ICD tables 9 and 10** (region codes, site codes) use embedded subset fonts and did not
  extract. Open the PDF in a viewer before finalising the bit schema; it is saved locally.
  Our own enums are unaffected — only interop with INCOIS's existing site table is.
- **GraphCast is not served by Open-Meteo.** AIFS is. Claim only AIFS.
- **No Palk-Bay-specific PFZ complaint quotes exist** in the literature — the complaint
  studies are Kerala/Karnataka/Odisha. State that as a gap rather than passing Karnataka
  quotes off as Palk Bay's.
- **Unverified and dropped:** the "34% of TN fishermen deaths are drownings" figure had no
  traceable primary source. Do not use it.
- **Scope risk.** Phase 10 is the load-bearing week. If it slips, cut W2.2 (CAP), W3.2
  (PROV) and W3.4 (eval) — in that order. Never cut W2.1; without the packet this is a
  competent project with no wedge.
