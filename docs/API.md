# FORESHORE HTTP + WebSocket contract

Fixed here so the backend and both front ends can be built against it in parallel. Both
surfaces call the **same** endpoints — the boat UI and the shore console differ only in
which of them they use and how they render the result. That is the central architectural
claim of the submission, so no endpoint may be surface-specific.

Base URL `http://localhost:8000`. Everything is JSON, UTF-8, `EPSG:4326`, times ISO-8601
with an explicit offset. Errors carry `{"error": {"code", "message", "detail"}}`.

An endpoint never invents a value. Where an input is missing the response says so
(`missing`, `partial`) rather than returning a shorter, more confident answer.

---

## Request path

### `POST /api/query`

The whole agent path: plan, specialists, verdict, ceiling, synthesis.

```jsonc
// request
{
  "text": "நாளை காலை கடலுக்கு போகலாமா?",   // required
  "lat": 9.2876, "lon": 79.3129,            // optional; defaults to the region's first anchor port
  "when": "2026-09-01T00:30:00Z",           // optional; else parsed from the text
  "vessel_class": "small_motorised",        // optional; else the region default
  "heading_deg": 120, "speed_kn": 4.5,      // optional; enables closing-ETA on geofences
  "destination": [8.95, 79.55],             // optional; triggers routing
  "language": null,                          // optional; null = auto-detect and mirror
  "region_id": null,                         // optional; null = configured default
  "surface": "boat",                        // "boat" | "console"
  "use_model": true
}
```

Response is `QueryOutcome.to_dict()` from `agents/orchestrator.py`:

```jsonc
{
  "query_id": "…",
  "language": "ta",
  "text": "…",                        // the answer, in the detected language
  "verdict": {                        // null only when no verdict could be evaluated
    "level": "GO" | "GO_WITH_CAUTION" | "DO_NOT_ADVISE",
    "reasons": ["…"],
    "ceiling_applied": true,
    "downgraded_from": "GO_WITH_CAUTION",
    "ceiling_notes": ["…"],            // human-readable audit of every ceiling rule that fired
    "ceiling_source": { /* Provenance */ },
    "handoff": { "authority_name": "…", "authority_type": "landing_centre",
                 "contact": "…", "contact_label": "Harbour control",
                 "contact_verified": false,               // true only for a real published
                                                          // number (Coast Guard 1554);
                                                          // false = demo directory entry,
                                                          // render as text, never tel:
                 "vhf_channel": "Ch 16", "district": "…",
                 "distance_nm": 0.5,
                 "alternates": [ /* 0-2 further named centres, same shape */ ] },
                                                          // required when DO_NOT_ADVISE
    "valid_from": "…", "valid_to": "…"
  },
  "evidence": [ { /* Observation, each with its Provenance */ } ],
  "trace": [ { /* TraceStep */ } ],
  "route": { /* Route.to_dict(), or null */ },
  "payloads": {
    "evidence_panel": [ { "variable", "display", "source_name", "authority",
                          "resolution", "freshness", "acquired_at",
                          "is_derived", "governs" } ],
    // one row per Observation (agents/synthesis.py's EvidenceRow.to_dict()); `display`
    // is the value already formatted with its unit ("0.59 m", "MODERATE") — there is no
    // separate `value`/`unit` pair on the wire. `resolution` is a formatted string
    // ("11 km" or "point/text"), not raw metres, and there is no `issued_at` here —
    // freshness is precomputed server-side into the `freshness` field instead.
    "labels": { /* UI strings in the answer's language */ },
    "verdict_copy": { /* headline + one-line reason in that language */ },
    "template_text": "…",     // the no-model answer, before any prose generation
    "unpolished_text": "…",   // the answer before the final editor pass
    "polish": { "applied": true, "reason": null },
    // The editor pass (agents/synthesis.py's `polish_answer`) rewrites the finished
    // answer for readability only. Any rewrite that moves a number, the verdict, the
    // named handoff or the language is discarded and `reason` says which check failed —
    // `unpolished_text` is then what `text` already contains.
    "<tool_name>": { /* that tool's payload, e.g. disagreements, proximities, zones */ }
  },
  "unsourced_numbers": [],            // non-empty means synthesis stripped something — show it
  "plan": { "steps": [ { "specialist", "tool", "args", "why" } ] },
  "specialists_used": ["OceanAnalytics", "RiskAssessment"],
  "missing": ["incois_osf_mwh"],
  "duration_ms": 3740,
  "architecture": [ { "name", "role", "ps_capability", "tools" } ],
  "scenario": null                    // see "Scenario exploration" below
}
```

The UI must render `evidence_panel` under every answer, must show `downgraded_from`
whenever `ceiling_applied` is true, and must show the `handoff` whenever the level is
`DO_NOT_ADVISE`. A `DO_NOT_ADVISE` is a designed outcome, not an error state — never
render it as a failure.

#### Scenario exploration (PLAN.md Phase 7 item 4)

When the question text names **two** explicit `HH:MM` departure times and no explicit
`when` was sent in the request — e.g. *"what if I leave at 04:00 instead of 06:00"*, the
PS's own example — `scenario` is populated instead of staying `null`:

```jsonc
"scenario": {
  "options": [
    { "label": "Leave at 04:00", "when": "2026-09-04T04:00:00+00:00",
      "outcome": { /* a full QueryOutcome, exactly this same shape, recursively */ } },
    { "label": "Leave at 06:00", "when": "2026-09-04T06:00:00+00:00",
      "outcome": { /* … */ } }
  ],
  "differences": ["…"],               // plain-language bullets of what actually changed
  "recommended_index": 0              // which option, 0 or 1
}
```

Both options are complete, independent answers — same tools, same ceiling, same
evidence discipline as an ordinary query, run twice, never an LLM asked to imagine the
difference. The top-level `verdict`/`text`/`evidence_panel` of the response *is*
`scenario.options[0].outcome`'s own — so a client that ignores `scenario` entirely still
renders a correct answer for the earlier of the two times. A request that supplies an
explicit `when` never triggers this — an explicit instant always means "answer for
exactly this", not an unrequested comparison.

### `POST /api/route`

Thin passthrough to tool 11. `{origin_lat, origin_lon, dest_lat, dest_lon, departure?,
vessel_class?}` → the tool's `ToolResult.to_dict()`, whose `payload.route` is
`Route.to_dict()`: `waypoints`, `legs` (each with its own `cost_breakdown`), a top-level
aggregate `cost_breakdown`, `total_distance_nm`/`direct_distance_nm`/`detour_pct`,
`total_eta_seconds`, `feasible`, `failure_reason`, `evidence`, and `avoided` — the names
of hazards/boundaries routed around, which is the "why it bends" explanation (there is
no separate `why_it_bends` field).

### `GET /api/verdict?lat&lon&vessel_class&when`

Tool 15 alone, for the boat UI's verdict card refresh without a full agent turn.

### `POST /api/geofence/check`

Tool 9 alone: `{lat, lon, heading_deg?, speed_kn?, classes?}`. The boat UI also runs this
check client-side when offline; this endpoint is the online path and must return the same
class semantics.

---

## Fleet and push path

### `GET /api/fleet`

`{"vessels": [VesselState…], "generated_at": …}`. Every simulated vessel carries
`is_simulated: true` and the UI must label it as simulated — there is no public real-time
AIS for Indian small boats.

### `GET /api/alerts?vessel_id=&active=true&since=`

`{"alerts": [Alert…]}` — each with `title_en/ta`, `body_en/ta`, `level`, `geofence_class`,
`distance_nm`, `eta_seconds`, `acknowledged_at`, `evidence`.

### `POST /api/alerts/{alert_id}/ack`  → the updated `Alert`.

### `WS /ws/alerts`

Server pushes, client never has to poll:

```jsonc
{ "type": "hello",    "interval_s": 5, "mode": "fixture", "region_id": "palk_bay_gom" }  // sent once, on connect
{ "type": "alert",    "alert": { /* Alert */ } }
{ "type": "vessels",  "vessels": [ /* VesselState */ ], "ts": "…" }
```

Per-vessel verdict rides along in `vessels[i].last_verdict` (a `VesselState` field) rather
than its own message type — nothing pushes a standalone `{"type": "verdict", ...}`
message.

Client → server: `{"type": "ack", "alert_id": "…", "by": "…"}`,
`{"type": "subscribe", "vessel_ids": [...]}` (omit for all).

---

## Reference and explainability

| Endpoint | Returns |
|---|---|
| `GET /health` | `{mode, region_id, sources: [{source_id, ok, latency_ms, issued_at, freshness}], tools_unavailable, checked_at}` — the healthcheck table, as JSON. `note` is present only when the region or source probe itself failed |
| `GET /api/region?region_id=` | region config the UI needs: `region_id`, `display_name_en/local`, `bbox`, `anchor_ports`, `primary_language`, `fallback_language`, `languages`, `basemap`, `districts`, `vessel_classes` |
| `GET /api/architecture` | `{specialists: [{name, role, ps_capability, tools}]}` — each specialist's own restricted tool subset, for the console's architecture panel. The full tool catalogue (schemas, descriptions) is `/api/catalogue`, not this |
| `GET /api/catalogue` | tool 16's payload: every source, its variables, resolution, cadence, availability |
| `GET /api/traces?limit=20` | recent queries: `{query_id, started_at, agents, step_count, tools}` — one row per query, newest first. Not the steps themselves; fetch those from `/api/trace/{query_id}` |
| `GET /api/trace/{query_id}` | `{query_id, steps}` — the full trace tree, tool by tool, nested `{step, children}` by `parent_id`; each step carries its own `provenance_ids` (not the full provenance records — cross-reference against the query's `evidence`/`evidence_panel`) |
| `GET /api/layers` | available static layer ids + metadata |
| `GET /api/layers/{layer_id}` | that layer as GeoJSON |
| `GET /api/geofences.geojson?classes=&region_id=` | geofence layers as GeoJSON, tagged by class, with each class's copy and lead distances |

`GET /health` must answer while sources are down — it reports the failure, it does not
become one.

---

## Rules that bind every endpoint

1. No number without a provenance record reachable from the same response.
2. `FORESHORE_MODE=fixture` changes no shape — the same endpoints answer from frozen
   snapshots with the network off.
3. Language is mirrored from the request text, never selected from a dropdown.
4. Nothing is labelled "current" that is not: freshness travels with every reading.
5. Geofence classes stay distinct end to end — the wire format carries the class, never a
   flattened "restricted zone".
