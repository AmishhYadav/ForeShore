# FORESHORE — Backlog & Deck Argument Store

Everything worth keeping from `PLAN_V2.md` that is **not** being built before the 8–9 Sept
demo. This is a reference and deck-argument store, not a work plan. `PLAN_V2.md` was
retired on 2026-09-09 as infeasible against the remaining time.

---

## 1. Field research — evidence for the deck

From research across CMFRI/ICAR/ICSF/INCOIS literature and post-Ockhi reviews. Full
citations in the research appendix; the load-bearing ones (citations for slides 3 and 6 —
must not be lost):

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
   when complexity is graduated. Removes the objection to the chart work in §3 below.
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

## 2. The innovation, deferred

### The finding that drives it

Pulled from **ISRO-IRNSS-ICD-MSG-INCOIS-1.2** (*Signal-in-Space ICD for INCOIS Messages via
NavIC Messaging Service*, U.R. Rao Satellite Centre, June 2020), message structure
extracted directly from the PDF. Verified, from ISRO's own document:

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
(§1 items 1 and 2 above) using the channel the PS owner already operates.

### The other half — the Decision Envelope

**This half IS now built.** `backend/foreshore/verdict/envelope.py` exists — the
Decision Envelope is not deferred, only the NavIC packet that would encode it is.

To fit a decision into 220 bits you must first *have* a decision object that is small,
self-contained and time-extended. So FORESHORE stops returning a verdict for an instant
and starts returning a **safe-operating window**.

For each 3-hour step across the INCOIS OSF 7-day horizon, for *this* vessel class:
the verdict, the **binding constraint** (which single threshold is saying no), and the
**margin** to it.

`sources/incois_thredds.py::series()` already returns exactly this series — `SWH`,
`SWELL`, `WP`, wind, current — with provenance, from one grid fetch. The thresholds are
already in `config/vessels.yaml`. The engine is already deterministic. This was assembly,
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
and a 48-hour lead time was irrelevant to them. And per §1 item 5 above, an envelope tells
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

## 3. Deferred work items

Drawn from PLAN_V2 Phases 11–13 and its "Self-documented defects to close" section. One
line each: what it is, and the file(s) it touches.

- **NavIC/GEMINI downlink packet** — `backend/foreshore/downlink/navic.py`: encode a
  `DecisionEnvelope` into a 220-bit payload laid out to ISRO-IRNSS-ICD-MSG-INCOIS-1.2,
  with segmentation via SegCount/SegID for long messages. Round-trip property test
  (`decode(encode(e)) == e`) across every vessel class × verdict × binding constraint.
  Honesty rule: encoding/decoding only, never a transmit claim.
- **CAP 1.2 emitter** — `backend/foreshore/downlink/cap.py`: every push alert also
  serialises as OASIS CAP 1.2 XML, the format NDMA's SACHET already ingests from
  IMD/INCOIS/CWC and redistributes over GAGAN and NavIC.
- **Chart components + time slider** — add a chart library to `frontend/package.json`
  plus components in `frontend/src`. Three charts: envelope band (verdict over the 7-day
  horizon, binding constraint on hover — headline visual for Slide 3), source
  disagreement (four wave-height series overlaid, governing one marked), productivity
  trend (multi-year Argo/Oceansat series, currently renders as plain text).
- **Trace inspector showing provenance for past sessions** — `console/TraceInspector.tsx`
  currently shows full provenance only for current-session queries
  (`console/TraceInspector.tsx:20-29`); needs evidence persisted alongside `TraceStep`.
- **Simulated fleet not relocating on region swap** — `console/RegionSwitcher.tsx:162`;
  the region-swap demo beat currently shows Gujarat boundaries with Palk Bay boats.
- **`AlertStore` in-memory only** — restart loses the queue; needs persistence.
- **Bhashini adapter stub / no backend speech path** — no backend speech path exists yet;
  Bhashini (`dhruva-api.bhashini.gov.in`) is the intended Tamil ASR/TTS integration.
- **`docs/artifacts/` empty** — zero screenshots, no PDF; capture at final quality once
  the demo is stable.
- **Researchers-as-stakeholder surface** — PS names five stakeholders (fishermen,
  researchers, coastal authorities, disaster management, maritime operators); only two
  are built. "Researchers" is nearly free given the archival Argo/Oceansat pipeline
  already exists (`PROJECT_CONTEXT.md` §8).
- **Multi-turn conversation wiring** — `Query.session_id` + `ConversationStore` (JSONL,
  mirroring `TraceStore`'s pattern — file authoritative, Postgres optional). Carry
  forward: last position, last vessel class, last `when`, last verdict, last envelope.
  Pronoun/ellipsis resolution ("what about tomorrow?", "and for my brother's vallam?")
  resolved deterministically in the planner, not by an LLM re-reading history. Hard rule:
  history may supply *context*, never *values* — every number still comes from a fresh
  tool call with fresh provenance.
- **AIFS as a fourth independent weather source** — `api.open-meteo.com` serves
  `ecmwf_aifs_025` (ECMWF's operational AI model, 0.25°, 15-day, 6-hourly, keyless).
  Add as a cross-check source, never governing; extends the existing three-source
  disagreement panel to four. Note: GraphCast is **not** served by Open-Meteo — claim
  only AIFS.
- **Envelope-edge push trigger** — `push/loop.py` gains a sea-state/wind trigger: fire
  when a tracked vessel's envelope transitions to a worse level within its horizon.
- **W3C PROV-conformant trace** — map the existing trace onto PROV-DM: evidence =
  entities, tool calls = activities, specialists + the ceiling = agents. Serialisation
  change, not new capability.
- **Contrastive + counterfactual cards** — templated off the existing `Threshold` table
  and `CeilingResult`; rendered in both surfaces and, if the NavIC packet lands, as a
  4-bit binding-constraint enum. No new LLM call.
- **`shared/types.ts` reconciliation** — reconcile with the live backend.
- **Language degrade-visibly rule** — either add `ml`/`te` copy, or make
  detection-without-copy degrade visibly ("detected Malayalam; answering in English —
  Malayalam copy not yet available") instead of silently falling back to English.
- **Abstention + explanation eval** — a held-out scenario set (missing bulletin, expired
  bulletin, contradictory sources, no GPS, no cyclone) scoring abstention correctness and
  whether the stated binding constraint is the true dominant cause.

**Cut order if time is lost:** CAP 1.2 emitter, then W3C PROV-conformant trace, then
abstention + explanation eval — in that order. Never cut the NavIC/GEMINI downlink
packet; without it this is a competent project with no wedge.

---

## 4. What changes on the six slides

Applies **only if** the deferred work above lands before 30 Sept.

| Slide | Change |
|---|---|
| 1 Title | Use the PS's full title: *ORCA — Marine EcOsystem Reasoning with Collaborative Agents*. Add the new one-liner from §2 above. |
| 2 Solution | Lead with **"Reasoning ashore, decision aboard."** Two surfaces, one core, **one 220-bit downlink**. |
| 3 Technical | The **envelope band chart** as the hero image. The NavIC bit-layout diagram beside ISRO's own sub-frame structure, cited to ISRO-IRNSS-ICD-MSG-INCOIS-1.2. Cite Fish & Fisheries 2026 for why the LLM never does arithmetic. |
| 4 Feasibility | Healthcheck table, now four independent forecast sources including ECMWF AIFS, keyless. The round-trip decode test as proof the packet is real. |
| 5 Impact | Ockhi's last-mile failure; the 10–20 km vs 100–150 km gap; GEMINI is one-way; DAT-SG reaches ~2%. Arrest figures. Then: this is the gap the 220-bit packet targets. |
| 6 References | Add ISRO ICD, the 1974/1976 treaties, CMFRI PFZ-adoption studies, Fish & Fisheries 2026, the maritime-XAI papers, W3C PROV, OASIS CAP 1.2. |

---

## Risks and unverified items (carried from PLAN_V2 Part 7)

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
