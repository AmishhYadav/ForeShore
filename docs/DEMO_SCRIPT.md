# FORESHORE — demo runbook

PLAN.md Phase 8: "Treat this as a real phase. A solo build that skips rehearsal loses
the round." This is that phase's concrete artifact — exact commands, exact queries,
what to say, what to point at, and what to do when something breaks on stage.

Run the whole thing in `FORESHORE_MODE=fixture` with the venue wifi physically off
(disable networking on the demo laptop, don't just trust the flag) at least once before
the real round. If it survives that, it survives a bad conference-hall network.

---

## Pre-flight (do this the morning of, every time)

```bash
cd /Users/amish/ForeShore
source .venv/bin/activate

# 1. Confirm every source is still reachable live — endpoints move.
FORESHORE_MODE=live python scripts/healthcheck.py

# 2. If anything changed shape, refreeze fixtures BEFORE turning the network off.
FORESHORE_MODE=live python scripts/freeze_fixtures.py

# 3. Confirm fixture mode is self-sufficient with the network off.
FORESHORE_MODE=fixture python scripts/healthcheck.py
```

Fixture freshness matters more than it looks: the advisory ceiling checks the governing
IMD bulletin's 12-hour validity window against the *time being asked about*, not against
wall-clock now (`docs/DECISIONS.md` D9). A bulletin frozen yesterday will not authorise
"tomorrow morning" today. **Refreeze fixtures the same day you demo, ideally within a
couple of hours of the slot**, and prefer asking about "now" / "tonight" over a fixed
clock time if the round is delayed.

## Start both surfaces

```bash
# Terminal 1 — backend
cd /Users/amish/ForeShore
source .venv/bin/activate
FORESHORE_MODE=fixture uvicorn foreshore.api.main:app --app-dir backend --port 8000

# Terminal 2 — frontend
cd /Users/amish/ForeShore/frontend
npm run dev
```

Open `/boat` and `/console` in two separate browser windows/tabs side by side — the
"same agent core, different renderer" beat (4:15) needs both visible at once.

No `ANTHROPIC_API_KEY` is required and none should be set for the rehearsed run: the
scripted deterministic fallback (`docs/DECISIONS.md` D7) produces the identical verdict,
evidence and trace, with plainer prose — one less live dependency on stage. Set the key
only if you specifically want to show the model's own prose.

---

## The 7-minute script

Exact query text below is what `scripts/freeze_fixtures.py` warmed into the fixture
snapshot — use it verbatim so the fixture-mode run always has something to answer.

| Time | Say / do | Query | Proves |
|---|---|---|---|
| 0:00 | Speak (or type) into `/boat` | **நாளை காலை கடலுக்குப் போகலாமா?** ("Is it safe to go out tomorrow morning?") | Tamil voice in, Tamil answer out, amber/red verdict |
| 0:45 | Open the evidence panel under the answer | — | IMD sea-condition descriptor vs INCOIS SWH vs Open-Meteo wave height, shown side by side, not averaged; ceiling downgrade shown if it fired |
| 1:45 | New query | **Where's the nearest fishing zone?** | Official INCOIS PFZ line, dated, plus the derived chlorophyll/SST-front cross-check beside it, clearly labelled derived |
| 2:30 | New query, or the routing panel if already surfaced | **Safest route to the fishing ground south-east of Rameswaram** | A* path visibly bends around the reef/IMBL; open the per-leg cost breakdown |
| 3:15 | Switch to the map, point at the vessel closing on the 1974 line | — | Proactive geofence alert fires unprompted. **Flip "No signal" in the boat UI — the alert keeps firing** (client-side geofence check, no network) |
| 4:15 | Switch browser tab to `/console` | — | Same fleet, same alert, arrived within ~5s over the same WebSocket event — the disaster-management surface, not a second app |
| 5:00 | In `/console`, click the trace inspector on the 0:00 answer | — | Every tool call, every provenance record, retrievable — this is a stored artifact, not post-hoc narration |
| 5:45 | Console analyst query box | **Why has fish productivity declined in this region over the past few years?** | Multi-year chlorophyll/SST/Argo trend narrative — the query a precomputed-advisory app structurally cannot answer |
| 6:30 | Console region switcher | Switch to Sir Creek & Gulf of Kutch | Live re-home — boundary layers, ceiling and language config all swap from one YAML file, proven with a real query against the new region, not a relabelled map |

Close on: **"SAMUDRA tells you what the advisory says. FORESHORE tells you what it means
for your boat, tonight, and why."**

### Bonus beat if there's time or a judge asks

Ask `/boat` (or console): **"What if I leave at 04:00 instead of 06:00?"** — two full,
independently-evaluated verdicts side by side with a plain-language diff and a
recommendation, never an LLM guessing at the difference. This is the PS's own
"explore scenarios" bullet, answered structurally rather than with an extra prompt.

---

## Failure drills — rehearse these deliberately, don't just hope they don't happen

| Drill | How to trigger | Expected |
|---|---|---|
| A source 403s / is unreachable | Unplug network mid-query in `FORESHORE_MODE=live`, or see `test_orchestrator.py::test_imd_bulletin_403_degrades_to_do_not_advise_with_named_handoff` | Degrades to `DO_NOT_ADVISE` with a named handoff (nearest landing centre + Coast Guard 1554) — never a stack trace on screen |
| No active cyclone | Just ask a hazard question on an ordinary day | `0 features` is rendered as "no active cyclone," a positive result, never as an error banner |
| LLM unavailable / times out | Unset `ANTHROPIC_API_KEY`, or let a configured key's call fail mid-turn | Same verdict, same evidence, same trace, plainer prose — `agents/runtime.py`'s scripted fallback and mid-run exception handling both degrade rather than crash |
| GPS unavailable | Deny location permission in the browser | Boat UI falls back to the region's first anchor port position, labelled as such in the footer ("using Rameswaram — no GPS fix") |
| Bulletin expired for the asked time | Ask about a time outside the frozen bulletin's 12 h validity window | `DO_NOT_ADVISE`, explicitly citing the expired/out-of-window bulletin, with a handoff — never a guess |
| Venue wifi dies mid-demo | Physically disable networking | Nothing changes — `FORESHORE_MODE=fixture` never touched the network to begin with |

If a judge asks "what happens when X breaks" for any X not listed above: the honest
answer is always "abstain to `DO_NOT_ADVISE` with a named handoff" — say that plainly
rather than improvising a specific mechanism you haven't verified.

---

## Rehearse three times, timed, before the real round

Time each run against the table above. If a beat consistently overruns, cut narration
around it rather than the beat itself — every beat maps to a scored PS bullet. First
`plan_route` call in a fresh process pays a one-time cost-field build (several seconds);
warm it by asking the routing question once during setup, before judges are watching.
