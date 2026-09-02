/**
 * Fleet map — MapLibre GL over the Bhuvan (ISRO/NRSC) WMS raster basemap, the whole
 * simulated fleet colour-coded by `last_verdict`, and every geofence class (static +
 * the dynamic HAZARD_EXCLUSION cyclone/hazard geometry) colour-coded by severity.
 *
 * Two deliberate choices worth calling out:
 *
 * - The basemap is an external ISRO server this sandbox may not be able to reach. A
 *   plain `background` layer painted the maritime "ocean" ink colour sits underneath
 *   the raster source; any tile that fails to load just leaves that ground colour
 *   showing through instead of a blank/broken tile, so the map never looks "broken"
 *   even fully offline. Map construction itself is also wrapped in try/catch, and a
 *   constructor failure (e.g. no WebGL) renders a plain fallback panel rather than a
 *   crash.
 * - Vessels render as DOM markers (`maplibregl.Marker`), not a GL symbol layer, so the
 *   "SIMULATED" labelling on every marker is plain HTML/CSS text — it does not depend
 *   on a glyphs/font server being reachable the way GL `text-field` rendering would.
 *   Geofences stay on GL fill/line layers since they need no text glyphs of their own.
 */
import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { RegionInfo, VerdictLevel, VesselState } from "@shared/types";
import { formatTimeAgo, geofenceClassLabel, severityVar, verdictLabel, verdictVar } from "./format";

interface FleetMapProps {
  region: RegionInfo | null;
  vessels: VesselState[];
  geofences: GeoJSON.FeatureCollection | null;
}

interface Basemap {
  wms_url?: string;
  layer?: string;
  attribution?: string;
  center?: [number, number]; // [lat, lon] per docs/API.md region payload
  zoom?: number;
}

const EMPTY_FC: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

function resolveVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function verdictHex(level: VerdictLevel | null | undefined): string {
  switch (level) {
    case "GO":
      return resolveVar("--verdict-go", "#1fa37a");
    case "GO_WITH_CAUTION":
      return resolveVar("--verdict-caution", "#e0a815");
    case "DO_NOT_ADVISE":
      return resolveVar("--verdict-stop", "#d9483f");
    default:
      return resolveVar("--ink-500", "#2c7196");
  }
}

function row(label: string, value: string): HTMLTableRowElement {
  const tr = document.createElement("tr");
  const th = document.createElement("th");
  th.textContent = label;
  const td = document.createElement("td");
  td.textContent = value;
  tr.append(th, td);
  return tr;
}

/** shared/types.ts's `VesselState` interface only names vessel_id/name/lat/lon/
 *  heading_deg/speed_kn/vessel_class/is_simulated/updated_at — `last_verdict`,
 *  `home_port` and `crew` (all present in every `/api/fleet` and WS "vessels" payload,
 *  per backend/foreshore/models.py's `VesselState.to_dict()` and this brief) are only
 *  reachable through its `[key: string]: unknown` index signature there, a gap in the
 *  shared contract left as-is per the brief (see final report). This local type
 *  documents the verified runtime shape so the rest of this file can use it directly. */
type FullVessel = VesselState & {
  last_verdict: VerdictLevel | null;
  home_port: string | null;
  crew: number | null;
};

function buildVesselPopup(vessel: VesselState): HTMLElement {
  const v = vessel as FullVessel;
  const wrap = document.createElement("div");
  wrap.className = "fm-popup";

  const title = document.createElement("div");
  title.className = "fm-popup__title";
  title.textContent = v.name;
  wrap.appendChild(title);

  const sim = document.createElement("div");
  sim.className = "fm-popup__sim";
  sim.textContent = "SIMULATED VESSEL — no public real-time AIS feed for Indian small boats.";
  wrap.appendChild(sim);

  const table = document.createElement("table");
  table.appendChild(row("Verdict", verdictLabel(v.last_verdict)));
  table.appendChild(row("Class", v.vessel_class));
  table.appendChild(row("Heading", `${Math.round(v.heading_deg)}°`));
  table.appendChild(row("Speed", `${v.speed_kn.toFixed(1)} kn`));
  table.appendChild(row("Home port", v.home_port ?? "—"));
  table.appendChild(row("Crew", v.crew != null ? String(v.crew) : "—"));
  table.appendChild(row("Position updated", formatTimeAgo(v.updated_at)));
  wrap.appendChild(table);

  return wrap;
}

function buildGeofencePopup(props: Record<string, unknown>): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "fm-popup";

  const titleField = props["title"];
  const nameEn =
    (typeof titleField === "object" && titleField !== null
      ? (titleField as Record<string, unknown>)["en"]
      : undefined) ??
    props["name_en"] ??
    props["name"] ??
    geofenceClassLabel(props["geofence_class"] as string | undefined);

  const title = document.createElement("div");
  title.className = "fm-popup__title";
  title.textContent = String(nameEn);
  wrap.appendChild(title);

  if (props["dynamic"]) {
    const tag = document.createElement("div");
    tag.className = "fm-popup__sim";
    tag.textContent = "Dynamic hazard geometry (cyclone / high-wave cell).";
    wrap.appendChild(tag);
  }

  const table = document.createElement("table");
  table.appendChild(row("Class", geofenceClassLabel(props["geofence_class"] as string | undefined)));
  table.appendChild(row("Severity", String(props["severity"] ?? "—")));
  if (props["warn_nm"] != null) table.appendChild(row("Warn at", `${props["warn_nm"]} nm`));
  if (props["critical_nm"] != null) table.appendChild(row("Critical at", `${props["critical_nm"]} nm`));
  if (props["treaty"]) table.appendChild(row("Treaty", String(props["treaty"])));
  wrap.appendChild(table);

  return wrap;
}

interface MarkerEntry {
  marker: maplibregl.Marker;
  popup: maplibregl.Popup;
  el: HTMLDivElement;
}

export default function FleetMap({ region, vessels, geofences }: FleetMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<Map<string, MarkerEntry>>(new Map());
  const geofencesRef = useRef<GeoJSON.FeatureCollection | null>(geofences);
  const [ready, setReady] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [tileFailed, setTileFailed] = useState(false);

  geofencesRef.current = geofences;

  // -- map construction (once region is known) ---------------------------------------
  useEffect(() => {
    if (!containerRef.current || mapRef.current || !region) return;
    const basemap = (region.basemap ?? {}) as Basemap;
    const center: [number, number] = basemap.center
      ? [basemap.center[1], basemap.center[0]]
      : [79.2, 9.3];
    const zoom = basemap.zoom ?? 7;
    const oceanColor = resolveVar("--ink-900", "#0b1f30");

    const rasterTileUrl =
      basemap.wms_url && basemap.layer
        ? `${basemap.wms_url}?service=WMS&version=1.1.1&request=GetMap&layers=${encodeURIComponent(
            basemap.layer,
          )}&styles=&format=image/png&transparent=true&srs=EPSG:3857&bbox={bbox-epsg-3857}&width=256&height=256`
        : null;

    const style: maplibregl.StyleSpecification = {
      version: 8,
      sources: rasterTileUrl
        ? {
            bhuvan: {
              type: "raster",
              tiles: [rasterTileUrl],
              tileSize: 256,
              attribution: basemap.attribution ?? "",
            },
          }
        : {},
      layers: [
        { id: "ocean-bg", type: "background", paint: { "background-color": oceanColor } },
        ...(rasterTileUrl
          ? ([
              {
                id: "bhuvan-raster",
                type: "raster",
                source: "bhuvan",
                paint: { "raster-opacity": 0.85 },
              },
            ] as maplibregl.LayerSpecification[])
          : []),
      ],
    };

    try {
      const map = new maplibregl.Map({
        container: containerRef.current,
        style,
        center,
        zoom,
      });
      mapRef.current = map;
      map.addControl(new maplibregl.NavigationControl(), "top-right");

      map.on("error", () => {
        // Tile-fetch failures (venue wifi, ISRO server unreachable) land here per-tile;
        // the background ocean colour underneath already covers for it visually — this
        // only flags the degraded state with a small banner, it never throws or blanks
        // the map. Only relevant when a raster source was actually configured.
        if (rasterTileUrl) setTileFailed(true);
      });

      map.on("load", () => {
        map.addSource("geofences", { type: "geojson", data: geofencesRef.current ?? EMPTY_FC });

        const legal = resolveVar("--severity-legal", "#d9483f");
        const hazard = resolveVar("--severity-hazard", "#e0a815");
        const restricted = resolveVar("--severity-restricted", "#c77bd6");
        const advisory = resolveVar("--severity-advisory", "#4fa3d1");
        const severityMatch: maplibregl.ExpressionSpecification = [
          "match",
          ["get", "severity"],
          "legal_hard",
          legal,
          "hazard",
          hazard,
          "restricted",
          restricted,
          "advisory",
          advisory,
          advisory,
        ];
        const severityWidth: maplibregl.ExpressionSpecification = [
          "match",
          ["get", "severity"],
          "legal_hard",
          2.6,
          "hazard",
          2.2,
          1.4,
        ];

        map.addLayer({
          id: "geofence-fill",
          type: "fill",
          source: "geofences",
          // Verified against a live /api/geofences.geojson response: MPA is a plain
          // Polygon but the ECO_SENSITIVE habitats (coral/seagrass/mangrove) come back
          // as MultiPolygon — a filter of just "Polygon" would silently drop every one
          // of those fills.
          filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
          paint: { "fill-color": severityMatch, "fill-opacity": 0.2 },
        });
        map.addLayer({
          id: "geofence-line",
          type: "line",
          source: "geofences",
          paint: { "line-color": severityMatch, "line-width": severityWidth, "line-opacity": 0.95 },
        });

        for (const layerId of ["geofence-fill", "geofence-line"]) {
          map.on("mouseenter", layerId, () => {
            map.getCanvas().style.cursor = "pointer";
          });
          map.on("mouseleave", layerId, () => {
            map.getCanvas().style.cursor = "";
          });
          map.on("click", layerId, (e) => {
            const feature = e.features?.[0];
            if (!feature) return;
            new maplibregl.Popup({ closeButton: true, maxWidth: "280px" })
              .setLngLat(e.lngLat)
              .setDOMContent(buildGeofencePopup((feature.properties ?? {}) as Record<string, unknown>))
              .addTo(map);
          });
        }

        setReady(true);
      });
    } catch (err) {
      setMapError(err instanceof Error ? err.message : String(err));
    }

    return () => {
      for (const entry of markersRef.current.values()) entry.marker.remove();
      markersRef.current.clear();
      mapRef.current?.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [region]);

  // -- geofence data updates -----------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const source = map.getSource("geofences") as maplibregl.GeoJSONSource | undefined;
    source?.setData(geofences ?? EMPTY_FC);
  }, [geofences, ready]);

  // -- vessel markers: create/update/remove, keyed by vessel_id ------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const seen = new Set<string>();

    for (const raw of vessels) {
      const v = raw as FullVessel;
      seen.add(v.vessel_id);
      const existing = markersRef.current.get(v.vessel_id);
      const color = verdictHex(v.last_verdict);

      if (existing) {
        existing.marker.setLngLat([v.lon, v.lat]);
        const dot = existing.el.querySelector<HTMLElement>(".fm-vessel-dot");
        const heading = existing.el.querySelector<HTMLElement>(".fm-vessel-heading");
        const label = existing.el.querySelector<HTMLElement>(".fm-vessel-label");
        if (dot) dot.style.background = color;
        if (heading) heading.style.transform = `rotate(${v.heading_deg}deg)`;
        if (label) label.textContent = `${v.name} · SIM`;
        existing.popup.setDOMContent(buildVesselPopup(v));
        continue;
      }

      const el = document.createElement("div");
      el.className = "fm-vessel-marker";
      el.innerHTML = `
        <span class="fm-vessel-heading" style="transform: rotate(${v.heading_deg}deg)"></span>
        <span class="fm-vessel-dot" style="background:${color}"></span>
        <span class="fm-vessel-label">${v.name} · SIM</span>
      `;
      const popup = new maplibregl.Popup({ closeButton: true, offset: 14, maxWidth: "280px" }).setDOMContent(
        buildVesselPopup(v),
      );
      const marker = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat([v.lon, v.lat])
        .setPopup(popup)
        .addTo(map);
      markersRef.current.set(v.vessel_id, { marker, popup, el });
    }

    for (const [id, entry] of markersRef.current) {
      if (!seen.has(id)) {
        entry.marker.remove();
        markersRef.current.delete(id);
      }
    }
  }, [vessels, ready]);

  if (mapError) {
    return (
      <div className="fm-fallback">
        <p className="fm-fallback__title">Map unavailable in this browser ({mapError}).</p>
        <p>Fleet is still tracked — {vessels.length} simulated vessel(s) reporting.</p>
      </div>
    );
  }

  return (
    <div className="fm-wrap">
      <div ref={containerRef} className="fm-canvas" />
      {!ready && <div className="fm-loading">Loading chart…</div>}
      {tileFailed && (
        <div className="fm-tile-warning">Bhuvan basemap tiles unreachable — showing plain chart colour.</div>
      )}
      <div className="fm-banner">SIMULATED FLEET — no public real-time AIS feed for Indian small boats.</div>
      <div className="fm-legend">
        <div className="fm-legend__group">
          <div className="fm-legend__heading">Vessel risk</div>
          <LegendSwatch color="var(--verdict-go)" label="GO" />
          <LegendSwatch color="var(--verdict-caution)" label="GO WITH CAUTION" />
          <LegendSwatch color="var(--verdict-stop)" label="DO NOT ADVISE" />
          <LegendSwatch color="var(--ink-500)" label="No verdict yet" />
        </div>
        <div className="fm-legend__group">
          <div className="fm-legend__heading">Geofence severity</div>
          <LegendSwatch color={severityVar("legal_hard")} label="Legal (IMBL)" />
          <LegendSwatch color={severityVar("hazard")} label="Hazard exclusion" />
          <LegendSwatch color={severityVar("restricted")} label="Restricted (MPA)" />
          <LegendSwatch color={severityVar("advisory")} label="Advisory (eco-sensitive)" />
        </div>
      </div>
    </div>
  );
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <div className="fm-legend__item">
      <span className="fm-legend__swatch" style={{ background: color }} />
      <span>{label}</span>
    </div>
  );
}
