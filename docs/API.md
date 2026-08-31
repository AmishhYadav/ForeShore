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
    "ceiling_source": { /* Provenance */ },
    "handoff": { "authority_name": "…", "authority_type": "landing_centre",
                 "contact": "…", "distance_nm": 0.5 },   // required when DO_NOT_ADVISE
    "valid_from": "…", "valid_to": "…"
  },
  "evidence": [ { /* Observation, each with its Provenance */ } ],
  "trace": [ { /* TraceStep */ } ],
  "route": { /* Route.to_dict(), or null */ },
  "payloads": {
    "evidence_panel": [ { "variable", "value", "unit", "source_name", "authority",
                          "acquired_at", "issued_at", "resolution_m", "freshness",
                          "is_derived", "governs" } ],
    "labels": { /* UI strings in the answer's language */ },
    "verdict_copy": { /* headline + one-line reason in that language */ },
    "<tool_name>": { /* that tool's payload, e.g. disagreements, proximities, zones */ }
  },
  "unsourced_numbers": [],            // non-empty means synthesis stripped something — show it
  "plan": { "steps": [ { "specialist", "tool", "args", "why" } ] },
  "specialists_used": ["OceanAnalytics", "RiskAssessment"],
  "missing": ["incois_osf_mwh"],
  "duration_ms": 3740,
  "architecture": [ { "name", "role", "ps_capability", "tools" } ]
}
```

The UI must render `evidence_panel` under every answer, must show `downgraded_from`
whenever `ceiling_applied` is true, and must show the `handoff` whenever the level is
`DO_NOT_ADVISE`. A `DO_NOT_ADVISE` is a designed outcome, not an error state — never
render it as a failure.

### `POST /api/route`

Thin passthrough to tool 11. `{origin_lat, origin_lon, dest_lat, dest_lon, departure?,
vessel_class?}` → the tool's `ToolResult.to_dict()`, whose payload carries `route`, the
per-leg `cost_breakdown` and `why_it_bends`.

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
{ "type": "alert",    "alert": { /* Alert */ } }
{ "type": "vessels",  "vessels": [ /* VesselState */ ], "ts": "…" }
{ "type": "verdict",  "vessel_id": "…", "level": "…" }
{ "type": "hello",    "interval_s": 5, "mode": "fixture", "region_id": "palk_bay_gom" }
```

Client → server: `{"type": "ack", "alert_id": "…", "by": "…"}`,
`{"type": "subscribe", "vessel_ids": [...]}` (omit for all).

---

## Reference and explainability

| Endpoint | Returns |
|---|---|
| `GET /health` | `{mode, region_id, sources: [{source_id, ok, latency_ms, issued_at, freshness}], tools_unavailable}` — the healthcheck table, as JSON |
| `GET /api/region` | region config the UI needs: `bbox`, `anchor_ports`, `languages`, `basemap`, `districts`, `vessel_classes` |
| `GET /api/architecture` | specialists + the tool catalogue, for the console's architecture panel |
| `GET /api/catalogue` | tool 16's payload: every source, its variables, resolution, cadence, availability |
| `GET /api/traces?limit=20` | recent queries: `{query_id, text, language, verdict, ts, steps}` |
| `GET /api/trace/{query_id}` | the full trace tree, tool by tool, with every provenance record |
| `GET /api/layers` | available static layer ids + metadata |
| `GET /api/layers/{layer_id}` | that layer as GeoJSON |
| `GET /api/geofences.geojson?classes=` | geofence layers as GeoJSON, tagged by class, with each class's copy and lead distances |

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
