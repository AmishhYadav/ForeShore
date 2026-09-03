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
import { getHazards, getPfzDerived, getPfzOfficial } from "@shared/api";
import type {
  HazardsPayload,
  PfzDerivedPayload,
  PfzOfficialPayload,
  RegionInfo,
  VerdictLevel,
  VesselState,
} from "@shared/types";
import { formatTimeAgo, geofenceClassLabel, severityVar, verdictLabel, verdictVar } from "./format";

interface FleetMapProps {
  region: RegionInfo | null;
  vessels: VesselState[];
  geofences: GeoJSON.FeatureCollection | null;
  /** Optional external recentre target — [lat, lon], same order as RegionInfo.basemap's
   * own `center`. The map only constructs once (see the "map construction" effect below,
   * gated on `!mapRef.current`), so a later region swap needs this explicit prop to move
   * the already-built map; RegionSwitcher.tsx is the only current caller, passing the
   * new region's own basemap.center/zoom after a swap. Added deliberately minimally per
   * this task's brief — a single prop pair plus the one reactive effect below, no other
   * change to this component. */
  center?: [number, number];
  zoom?: number;
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

export default function FleetMap({ region, vessels, geofences, center, zoom }: FleetMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<Map<string, MarkerEntry>>(new Map());
  const geofencesRef = useRef<GeoJSON.FeatureCollection | null>(geofences);
  const flownToRef = useRef<string | null>(null);
  const [ready, setReady] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [tileFailed, setTileFailed] = useState(false);
  const [pfzOfficial, setPfzOfficial] = useState<PfzOfficialPayload | null>(null);
  const [pfzDerived, setPfzDerived] = useState<PfzDerivedPayload | null>(null);
  const [hazards, setHazards] = useState<HazardsPayload | null>(null);

  geofencesRef.current = geofences;

  // -- map construction (once region is known) ---------------------------------------
  useEffect(() => {
    if (!containerRef.current || mapRef.current || !region) return;
    const basemap = (region.basemap ?? {}) as Basemap;
    // [lat, lon] — same convention as the `center` prop and basemap.center itself; kept
    // as its own value (rather than reusing initialCenter below) so the key this seeds
    // flownToRef with is byte-for-byte the same format the recentre effect's key uses.
    const rawCenter: [number, number] = basemap.center ?? [9.3, 79.2];
    const initialCenter: [number, number] = [rawCenter[1], rawCenter[0]]; // maplibre wants [lng, lat]
    const initialZoom = basemap.zoom ?? 7;
    // This effect only ever fires once per mounted map (gated on `!mapRef.current`
    // above), using whatever `region` it saw first. Record that starting point as
    // "already flown to" so the recentre effect below doesn't replay an identical flyTo
    // the moment `ready` flips true.
    flownToRef.current = `${rawCenter[0]},${rawCenter[1]},${initialZoom}`;
    const oceanColor = resolveVar("--ink-900", "#0b1f30");
    const pfzOfficialColor = resolveVar("--pfz-official", "#2dd4bf");
    const pfzDerivedColor = resolveVar("--pfz-derived", "#7c93ff");
    const hazardTrackColor = resolveVar("--hazard-track", "#ff5da2");

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
        center: initialCenter,
        zoom: initialZoom,
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
        const hazard = resolveVar("--severity-hazard", "#e0a815");

        // Hazard exclusion polygons + derived PFZ zones sit below the geofence layers
        // (added next) so geofence hover/click stays on top; the official PFZ line and
        // the cyclone track (added further below) sit above everything since they're
        // thin, high-priority lines that must stay visible over the fills.
        map.addSource("hazard-polygons", { type: "geojson", data: EMPTY_FC });
        map.addLayer({
          id: "hazard-fill",
          type: "fill",
          source: "hazard-polygons",
          paint: { "fill-color": hazard, "fill-opacity": 0.25 },
        });
        map.addLayer({
          id: "hazard-line",
          type: "line",
          source: "hazard-polygons",
          paint: { "line-color": hazard, "line-width": 2, "line-opacity": 0.9 },
        });

        map.addSource("pfz-derived", { type: "geojson", data: EMPTY_FC });
        map.addLayer({
          id: "pfz-derived-fill",
          type: "fill",
          source: "pfz-derived",
          paint: { "fill-color": pfzDerivedColor, "fill-opacity": 0.18 },
        });
        map.addLayer({
          id: "pfz-derived-line",
          type: "line",
          source: "pfz-derived",
          paint: { "line-color": pfzDerivedColor, "line-width": 1.5, "line-dasharray": [2, 2], "line-opacity": 0.9 },
        });

        map.addSource("geofences", { type: "geojson", data: geofencesRef.current ?? EMPTY_FC });

        const legal = resolveVar("--severity-legal", "#d9483f");
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

        map.addSource("pfz-official", { type: "geojson", data: EMPTY_FC });
        map.addLayer({
          id: "pfz-official-line",
          type: "line",
          source: "pfz-official",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": pfzOfficialColor, "line-width": 3, "line-opacity": 0.95 },
        });

        map.addSource("hazard-track", { type: "geojson", data: EMPTY_FC });
        map.addLayer({
          id: "hazard-track-line",
          type: "line",
          source: "hazard-track",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": hazardTrackColor, "line-width": 2.5, "line-dasharray": [3, 1.5], "line-opacity": 0.95 },
        });

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

  // -- fetch official/derived PFZ + hazards for the whole active region -----------------
  // Self-contained fetch, mirroring the fetch-and-refresh lifecycle already used for
  // geofences elsewhere in this codebase (fetch on mount, .catch -> console.warn, leave
  // the layer empty rather than erroring) — kept local to this component since this data
  // is map-only. Keyed on `region.region_id` so a region swap re-fires it, whatever
  // upstream mechanism changed the `region` prop.
  useEffect(() => {
    if (!region) return;
    const [minLon, minLat, maxLon, maxLat] = region.bbox;
    const centerLat = (minLat + maxLat) / 2;
    const centerLon = (minLon + maxLon) / 2;
    let cancelled = false;

    getPfzOfficial(centerLat, centerLon)
      .then((res) => {
        if (!cancelled) setPfzOfficial(res.payload);
      })
      .catch((err) => console.warn("[FleetMap] failed to fetch official PFZ line:", err));

    getPfzDerived({ bbox: region.bbox })
      .then((res) => {
        if (!cancelled) setPfzDerived(res.payload);
      })
      .catch((err) => console.warn("[FleetMap] failed to fetch derived PFZ zones:", err));

    getHazards({ bbox: region.bbox })
      .then((res) => {
        if (!cancelled) setHazards(res.payload);
      })
      .catch((err) => console.warn("[FleetMap] failed to fetch hazards:", err));

    return () => {
      cancelled = true;
    };
  }, [region?.region_id]);

  // -- PFZ / hazard layer data updates ---------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const officialData: GeoJSON.FeatureCollection = pfzOfficial?.geometry
      ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: pfzOfficial.geometry }] }
      : EMPTY_FC;
    (map.getSource("pfz-official") as maplibregl.GeoJSONSource | undefined)?.setData(officialData);
    (map.getSource("pfz-derived") as maplibregl.GeoJSONSource | undefined)?.setData(pfzDerived?.zones ?? EMPTY_FC);
    (map.getSource("hazard-polygons") as maplibregl.GeoJSONSource | undefined)?.setData({
      type: "FeatureCollection",
      features: hazards?.polygons ?? [],
    });
    (map.getSource("hazard-track") as maplibregl.GeoJSONSource | undefined)?.setData(hazards?.cyclone_track ?? EMPTY_FC);
  }, [pfzOfficial, pfzDerived, hazards, ready]);

  // -- external recentre (region swap) -------------------------------------------------
  // The map only ever constructs once (see above); a region swap after that needs an
  // explicit fly-to rather than a rebuild. `flownToRef` both skips the redundant flyTo
  // this effect would otherwise fire the instant `ready` flips true (construction already
  // centred on this same point) and skips repeat flights to a `center`/`zoom` pair this
  // component has already flown to.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !center) return;
    const key = `${center[0]},${center[1]},${zoom ?? ""}`;
    if (flownToRef.current === key) return;
    flownToRef.current = key;
    map.flyTo({ center: [center[1], center[0]], zoom: zoom ?? map.getZoom(), duration: 1400 });
  }, [center, zoom, ready]);

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

  const officialNote = !pfzOfficial
    ? "Loading official PFZ line…"
    : pfzOfficial.geometry
      ? `Official INCOIS PFZ line — advisory dated ${pfzOfficial.advisory_date ?? "unknown date"}`
      : "No official PFZ line published for this sector today.";

  const derivedNote = !pfzDerived
    ? "Loading indicative fishing-zone estimate…"
    : `${pfzDerived.disclaimer}${
        !pfzDerived.chlorophyll_available && pfzDerived.chlorophyll_reason ? ` (${pfzDerived.chlorophyll_reason})` : ""
      }`;

  const hazardNote = !hazards
    ? "Checking for active cyclone hazard…"
    : hazards.no_active_hazard
      ? "No active cyclone hazard in this area."
      : "Active cyclone hazard — exclusion area and track shown on the map.";

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
        <div className="fm-legend__group">
          <div className="fm-legend__heading">Fishing zones</div>
          <LegendSwatch color="var(--pfz-official)" label="Official INCOIS PFZ line" />
          <LegendSwatch color="var(--pfz-derived)" label="FORESHORE-derived (indicative)" dashed />
          <div className="fm-legend__note">{officialNote}</div>
          <div className="fm-legend__note">{derivedNote}</div>
        </div>
        <div className="fm-legend__group">
          <div className="fm-legend__heading">Cyclone hazard</div>
          <LegendSwatch color={severityVar("hazard")} label="Exclusion area" />
          <LegendSwatch color="var(--hazard-track)" label="Track" />
          <div className="fm-legend__note">{hazardNote}</div>
        </div>
      </div>
    </div>
  );
}

function LegendSwatch({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <div className="fm-legend__item">
      <span
        className={`fm-legend__swatch${dashed ? " fm-legend__swatch--dashed" : ""}`}
        style={dashed ? { borderColor: color } : { background: color }}
      />
      <span>{label}</span>
    </div>
  );
}
