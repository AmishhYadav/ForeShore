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
import * as turf from "@turf/turf";
import { getHazards, getLayerGeoJson, getPfzDerived, getPfzOfficial } from "@shared/api";
import type {
  HazardsPayload,
  PfzDerivedPayload,
  PfzOfficialPayload,
  RegionInfo,
  VerdictLevel,
  VesselState,
} from "@shared/types";
import { formatTimeAgo, geofenceClassLabel, severityVar, verdictLabel } from "./format";

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
  /** Vessel currently focused in the console (see ConsoleApp). Its marker is highlighted
   *  and the map eases to it when the selection arrives from the alert queue. */
  selectedVesselId?: string | null;
  onSelectVessel?: (vesselId: string) => void;
}

interface Basemap {
  wms_url?: string;
  layer?: string;
  attribution?: string;
  center?: [number, number]; // [lat, lon] per docs/API.md region payload
  zoom?: number;
  // `/api/region`'s basemap payload already carries this (verified live) for exactly
  // the case below — the primary ISRO/Bhuvan WMS answering with a 200 but a
  // ServiceException body instead of a tile. Previously unread by this component.
  fallback?: { wms_url?: string; layer?: string; attribution?: string; label?: string };
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

function wmsTileUrl(wmsUrl: string, layer: string): string {
  return `${wmsUrl}?service=WMS&version=1.1.1&request=GetMap&layers=${encodeURIComponent(
    layer,
  )}&styles=&format=image/png&transparent=true&srs=EPSG:3857&bbox={bbox-epsg-3857}&width=256&height=256`;
}

/** Simple top-down hull glyph, nose pointing up (0deg = north) so it can be rotated
 *  in place by heading. Filled by the caller with the verdict colour. Kept as a single
 *  small inline SVG string (not a GL symbol layer) for the same reason the rest of this
 *  file uses DOM markers for vessels: no glyph/font server dependency.
 *
 *  The viewBox is sized to the hull's own bounding box (20x24, no empty margin) so the
 *  SVG's centre and the hull's centre are the same point. That matters twice over: it is
 *  the point MapLibre anchors on the vessel's lat/lon, and it is the point the heading
 *  rotation turns about. */
const BOAT_ICON_W = 20;
const BOAT_ICON_H = 24;

function boatIconSvg(fill: string): string {
  return `<svg viewBox="0 0 ${BOAT_ICON_W} ${BOAT_ICON_H}" width="${BOAT_ICON_W}" height="${BOAT_ICON_H}" xmlns="http://www.w3.org/2000/svg">
    <path d="M10 0 L18 18 Q10 24 2 18 Z" fill="${fill}" stroke="rgba(6,19,31,0.85)" stroke-width="1.5"/>
  </svg>`;
}

/** Legend swatch for the landing centres — the same small teal dot the GL circle layer
 *  draws. The previous glyph carried a downward arrow over a baseline, which at 16px read
 *  as a download icon rather than a harbour, and repeating it 137 times along the coast
 *  made that worse. */
function harbourIconSvg(): string {
  return `<svg viewBox="0 0 20 20" width="16" height="16" xmlns="http://www.w3.org/2000/svg">
    <circle cx="10" cy="10" r="4" fill="#2dd4bf" fill-opacity="0.75" stroke="var(--marine-navy)" stroke-width="1.2"/>
  </svg>`;
}

/** FORESHORE-derived PFZ zones come straight off the SST grid (~9km cells, per
 *  /api/pfz/derived's own provenance) with no polygon simplification, so each zone
 *  renders as an unsimplified staircase of grid-cell edges — real data, but it reads as
 *  a jagged/blocky "random diagram" rather than a zone. Smoothing is display-only:
 *  `turf.simplify` runs client-side on the already-fetched geometry, the analysis
 *  itself is untouched, and the tolerance (~0.02deg, ~2km at this latitude) is well
 *  under the 9km cell size so it can't merge or split zones, only round their corners. */
function smoothDerivedZones(fc: GeoJSON.FeatureCollection): GeoJSON.FeatureCollection {
  try {
    return {
      ...fc,
      features: fc.features.map((f) => turf.simplify(f, { tolerance: 0.02, highQuality: true })),
    };
  } catch (err) {
    console.warn("[FleetMap] failed to simplify derived PFZ zones, using raw geometry:", err);
    return fc;
  }
}

function buildHarbourPopup(props: Record<string, unknown>): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "fm-popup";
  const title = document.createElement("div");
  title.className = "fm-popup__title";
  title.textContent = String(props["LC_NAME"] ?? "Landing centre");
  wrap.appendChild(title);
  const table = document.createElement("table");
  table.appendChild(row("District", String(props["DIST_NAME"] ?? "—")));
  table.appendChild(row("Sector", String(props["SECTOR_NAM"] ?? "—")));
  wrap.appendChild(table);
  return wrap;
}

export default function FleetMap({
  region,
  vessels,
  geofences,
  center,
  zoom,
  selectedVesselId = null,
  onSelectVessel,
}: FleetMapProps) {
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
  const [coastline, setCoastline] = useState<GeoJSON.FeatureCollection | null>(null);
  const [landingCentres, setLandingCentres] = useState<GeoJSON.FeatureCollection | null>(null);
  const [usingFallbackBasemap, setUsingFallbackBasemap] = useState(false);
  const [legendOpen, setLegendOpen] = useState(true);
  const fallbackAppliedRef = useRef(false);
  /** Held in a ref so the marker create/update effect never has to re-run (and so tear
   *  down and rebuild every marker) just because the parent handed down a new callback
   *  identity. The listener registered on a marker element reads through this. */
  const onSelectVesselRef = useRef(onSelectVessel);
  onSelectVesselRef.current = onSelectVessel;
  /** Guards the ease-to-vessel effect so it fires once per selection, not on every
   *  position tick of an already-selected vessel. */
  const easedToVesselRef = useRef<string | null>(null);
  /** The single popup allowed open at any moment — see `showPopup`. */
  const activePopupRef = useRef<maplibregl.Popup | null>(null);

  /** Open `popup`, closing whatever was open before it.
   *
   *  Vessels, landing centres and geofences all raise popups, and left to themselves they
   *  stack: MapLibre closes a popup on a bare map click, but a click that lands on
   *  another feature just opens a second box, so a vessel card and a harbour card end up
   *  overlapping each other. Routing every open through here makes "one box at a time" a
   *  property of the map rather than something each call site has to remember. */
  const showPopup = (map: maplibregl.Map, popup: maplibregl.Popup, lngLat: maplibregl.LngLatLike) => {
    if (activePopupRef.current && activePopupRef.current !== popup) {
      activePopupRef.current.remove();
    }
    popup.setLngLat(lngLat).addTo(map);
    activePopupRef.current = popup;
  };

  geofencesRef.current = geofences;

  // A raster basemap is only *expected* if the region actually declares one; `tileFailed`
  // then reports whether it (and its declared fallback) gave up. The local vector
  // coastline stands in for the basemap in exactly those two cases and is otherwise off —
  // see the visibility effect below.
  const declaresRasterBasemap = Boolean(
    (region?.basemap as Basemap | undefined)?.wms_url && (region?.basemap as Basemap | undefined)?.layer,
  );
  const showCoastlineVectors = !declaresRasterBasemap || tileFailed;

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
      basemap.wms_url && basemap.layer ? wmsTileUrl(basemap.wms_url, basemap.layer) : null;

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
        // -- Fixed-zoom chart -----------------------------------------------------------
        // This map pans but does not zoom. Every zoom entry point is turned off at the
        // handler level (below) and the zoom range is then pinned to a single value
        // (further below), so nothing — gesture, keyboard, control, or a stray
        // programmatic camera call — can change the scale.
        //
        // The reason is DOM-marker placement. Vessels have to be DOM markers (they carry
        // the "SIMULATED" HTML label and a heading rotation), and MapLibre positions a
        // DOM marker by writing an inline pixel `transform` from JS on every move frame,
        // while the chart itself is composited on the GPU. Across a zoom those two are
        // driven by different clocks, and the markers visibly slide against the chart
        // underneath them. At a fixed scale there is no such divergence: a pan moves
        // chart and markers by the same translation, so a boat stays on its coordinate.
        //
        // The scale is chosen to frame the whole region (see the camera fit below), which
        // is the operational view anyway — a shore operator watches a fleet across a bay,
        // not one hull at berth. Detail per vessel is in the popup and the alert queue,
        // neither of which needs zoom.
        scrollZoom: false,
        boxZoom: false,
        doubleClickZoom: false,
        touchZoomRotate: false,
        dragRotate: false,
        pitchWithRotate: false,
        // Pitch is off for the same reason as zoom: tilting the chart changes the
        // projection under the DOM markers without moving the markers with it.
        touchPitch: false,
        // Off because the keyboard handler's +/- and shift-arrow bindings are zoom; its
        // arrow-key panning goes with them. Panning stays available by drag.
        keyboard: false,
        // Panning is the one camera interaction kept.
        dragPan: true,
      });
      mapRef.current = map;
      // No NavigationControl: its whole purpose is the zoom-in/zoom-out pair, and rotate
      // is disabled above, so it would only offer controls that do nothing.

      // Always frame the whole targeted region on first load, regardless of what the
      // basemap tiles end up doing — this is the primary fix for the map not reading as
      // "this region" at a glance. `region.bbox` is [minLon, minLat, maxLon, maxLat].
      // `cameraForBounds` is used rather than `fitBounds` because the resulting zoom is
      // needed as a value, not just applied: it becomes the locked scale.
      const [bMinLon, bMinLat, bMaxLon, bMaxLat] = region.bbox;
      const camera = map.cameraForBounds(
        [
          [bMinLon, bMinLat],
          [bMaxLon, bMaxLat],
        ],
        { padding: 64 },
      );
      // A further half-step out past the fitted bounds, so the region sits inside a
      // margin of surrounding coast and open water rather than filling the pane edge to
      // edge — the wider view, with the southern end of the region fully in frame.
      const lockedZoom = Math.max(0, (camera?.zoom ?? initialZoom) - 0.5);
      map.jumpTo({ center: camera?.center ?? initialCenter, zoom: lockedZoom });
      // Collapse the zoom range onto that single value. Any later camera call that names
      // a zoom (the region-swap flyTo, say) is clamped straight back to it.
      map.setMinZoom(lockedZoom);
      map.setMaxZoom(lockedZoom);

      map.on("error", () => {
        if (!rasterTileUrl) return;
        // The ISRO Bhuvan WMS has been observed answering with HTTP 200 but a WMS
        // ServiceException body (server-side rendering failure, not a network/CORS
        // issue) instead of a PNG tile — MapLibre surfaces that as a source "error"
        // per tile. Swap once to the region's declared fallback (GEBCO bathymetric
        // WMS) rather than just flagging degraded; the ocean-colour background still
        // covers the gap while the swap happens, so nothing looks broken either way.
        const fb = basemap.fallback;
        if (!fallbackAppliedRef.current && fb?.wms_url && fb.layer) {
          fallbackAppliedRef.current = true;
          const fallbackUrl = wmsTileUrl(fb.wms_url, fb.layer);
          if (map.getLayer("bhuvan-raster")) map.removeLayer("bhuvan-raster");
          if (map.getSource("bhuvan")) map.removeSource("bhuvan");
          map.addSource("bhuvan-fallback", {
            type: "raster",
            tiles: [fallbackUrl],
            tileSize: 256,
            attribution: fb.attribution ?? "",
          });
          // Inserted below the coastline layer (added next, right after this handler
          // is registered) so it stays the bottom-most visual layer either way.
          map.addLayer(
            { id: "bhuvan-fallback-raster", type: "raster", source: "bhuvan-fallback", paint: { "raster-opacity": 0.7 } },
            map.getLayer("coastline-fill") ? "coastline-fill" : undefined,
          );
          setUsingFallbackBasemap(true);
        } else if (fallbackAppliedRef.current || !fb) {
          setTileFailed(true);
        }
      });

      map.on("load", () => {
        const hazard = resolveVar("--severity-hazard", "#e0a815");
        const landFill = resolveVar("--land-fill", "#16405c");
        const landLine = resolveVar("--land-line", "#2c7196");

        // Local vector coastline (Natural Earth 10m land, no external server involved) so
        // the region still reads as land-and-sea, and the geofence/PFZ geometry does not
        // float over nothing, when the Bhuvan/GEBCO raster is degraded or absent.
        //
        // It is a FALLBACK ONLY, hidden whenever the raster basemap is actually drawing —
        // see the visibility effect below. The data is clipped to the region box plus a
        // small pad (verified live: [77.5, 7.5, 81.1, 11.4] against a region bbox of
        // [78.0, 8.0, 80.6, 10.9]), so painting it over a working basemap washes every
        // landmass inside that box with 55% navy and leaves the land outside it untouched.
        // The result is two hard-edged dark rectangles cutting across India and Sri Lanka
        // along the clip bounds — the clip is invisible on its own, but not underneath a
        // translucent fill.
        map.addSource("coastline", { type: "geojson", data: EMPTY_FC });
        map.addLayer({
          id: "coastline-fill",
          type: "fill",
          source: "coastline",
          paint: { "fill-color": landFill, "fill-opacity": 0.55 },
        });
        map.addLayer({
          id: "coastline-line",
          type: "line",
          source: "coastline",
          paint: { "line-color": landLine, "line-width": 1, "line-opacity": 0.8 },
        });

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
        // IMBL_HISTORIC_WATERS (the 1974 Palk Bay line) and IMBL_MARITIME_BOUNDARY (the
        // 1976 lines) are both `severity: legal_hard` — same colour/width above — but
        // CLAUDE.md is explicit that they're a different legal regime and must stay
        // visually distinct, not collapsed into one "international boundary" look.
        // `line-dasharray` has no data-driven support in the style spec (constants/zoom
        // only — a `["get", ...]`-based expression on it fails style validation), so the
        // two boundary kinds are split into two constant-dasharray layers on the same
        // source rather than one layer with an expression: historic waters stays solid,
        // maritime boundary gets a long dash, the convention treaty maps use to
        // separate boundary kinds.
        const isMaritimeBoundary: maplibregl.ExpressionSpecification = [
          "==",
          ["get", "geofence_class"],
          "IMBL_MARITIME_BOUNDARY",
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
        // A dark casing under just the legal_hard (international boundary) lines gives
        // them the double-stroke look political maps use for hard borders, so they read
        // as "boundary" rather than just another coloured line at a glance.
        map.addLayer({
          id: "geofence-legal-casing",
          type: "line",
          source: "geofences",
          filter: ["==", ["get", "severity"], "legal_hard"],
          paint: { "line-color": "#000000", "line-width": 4.6, "line-opacity": 0.35 },
        });
        map.addLayer({
          id: "geofence-line",
          type: "line",
          source: "geofences",
          filter: ["!", isMaritimeBoundary],
          paint: { "line-color": severityMatch, "line-width": severityWidth, "line-opacity": 0.95 },
        });
        map.addLayer({
          id: "geofence-line-imbl-maritime",
          type: "line",
          source: "geofences",
          filter: isMaritimeBoundary,
          // Constant, not the severityMatch expression — this filter already guarantees
          // every feature here is severity `legal_hard`.
          paint: { "line-color": legal, "line-width": 2.6, "line-opacity": 0.95, "line-dasharray": [4, 2] },
        });

        for (const layerId of [
          "geofence-fill",
          "geofence-line",
          "geofence-line-imbl-maritime",
          "geofence-legal-casing",
        ]) {
          map.on("mouseenter", layerId, () => {
            map.getCanvas().style.cursor = "pointer";
          });
          map.on("mouseleave", layerId, () => {
            map.getCanvas().style.cursor = "";
          });
          map.on("click", layerId, (e) => {
            const feature = e.features?.[0];
            if (!feature) return;
            const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "280px" }).setDOMContent(
              buildGeofencePopup((feature.properties ?? {}) as Record<string, unknown>),
            );
            showPopup(map, popup, e.lngLat);
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

        // Landing centres are a GL circle layer, deliberately NOT `maplibregl.Marker`
        // DOM elements. There are ~137 of them in this region alone, and MapLibre
        // re-projects and rewrites the inline transform of every DOM marker on every
        // single `move` frame (plus a per-marker requestAnimationFrame for its opacity
        // check). At ~140 markers that work no longer fits in a frame, so the DOM
        // markers fall behind the GPU-composited map during a zoom — the map appears to
        // slide out from under its own markers, including the vessels. A circle layer
        // renders on the GPU with the rest of the map, costs zero JS per frame, and is
        // pinned to its coordinate at every zoom level by construction.
        //
        // The vessels stay DOM markers on purpose (there are only six, and they need the
        // per-marker "SIMULATED" HTML label and heading rotation described at the top of
        // this file) — six is nowhere near the budget that caused this.
        map.addSource("landing-centres", { type: "geojson", data: EMPTY_FC });
        map.addLayer({
          id: "landing-centres",
          type: "circle",
          source: "landing-centres",
          // ~137 landing centres line one stretch of coast, so at the (now fixed) region
          // scale they sit roughly one every 2 km. Drawn small and dim on purpose: they
          // read as a stipple marking "landing centres all along this coast" rather than
          // 137 markers competing with the six vessels for attention. Each is still a
          // click target — the popup names the centre, its district and its sector, which
          // is what a DO_NOT_ADVISE handoff needs. Nothing is filtered out: which harbour
          // is nearest is a safety answer, not a decluttering decision.
          paint: {
            "circle-radius": 2.6,
            "circle-color": resolveVar("--pfz-official", "#2dd4bf"),
            "circle-opacity": 0.55,
            "circle-stroke-color": resolveVar("--marine-navy", "#0b1f30"),
            "circle-stroke-width": 0.8,
            "circle-stroke-opacity": 0.8,
          },
        });

        map.on("mouseenter", "landing-centres", () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", "landing-centres", () => {
          map.getCanvas().style.cursor = "";
        });
        map.on("click", "landing-centres", (e) => {
          const feature = e.features?.[0];
          if (!feature) return;
          const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "240px" }).setDOMContent(
            buildHarbourPopup((feature.properties ?? {}) as Record<string, unknown>),
          );
          showPopup(map, popup, e.lngLat);
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

    getLayerGeoJson("coastline")
      .then((fc) => {
        if (!cancelled) setCoastline(fc);
      })
      .catch((err) => console.warn("[FleetMap] failed to fetch coastline layer:", err));

    getLayerGeoJson("landing_centres")
      .then((fc) => {
        if (!cancelled) setLandingCentres(fc);
      })
      .catch((err) => console.warn("[FleetMap] failed to fetch landing centres layer:", err));

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
    (map.getSource("pfz-derived") as maplibregl.GeoJSONSource | undefined)?.setData(
      pfzDerived?.zones ? smoothDerivedZones(pfzDerived.zones) : EMPTY_FC,
    );
    (map.getSource("hazard-polygons") as maplibregl.GeoJSONSource | undefined)?.setData({
      type: "FeatureCollection",
      features: hazards?.polygons ?? [],
    });
    (map.getSource("hazard-track") as maplibregl.GeoJSONSource | undefined)?.setData(hazards?.cyclone_track ?? EMPTY_FC);
  }, [pfzOfficial, pfzDerived, hazards, ready]);

  // -- coastline layer data update -------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("coastline") as maplibregl.GeoJSONSource | undefined)?.setData(coastline ?? EMPTY_FC);
  }, [coastline, ready]);

  // -- coastline visibility: fallback only -----------------------------------------------
  // Shown only when no raster basemap is drawing, either because the region declares no
  // WMS at all or because both the primary and its fallback failed. With a working
  // basemap the land is already rendered, so this layer would add nothing but the clip-box
  // rectangles described where the layer is created.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const visibility = showCoastlineVectors ? "visible" : "none";
    for (const layerId of ["coastline-fill", "coastline-line"]) {
      if (map.getLayer(layerId)) map.setLayoutProperty(layerId, "visibility", visibility);
    }
  }, [showCoastlineVectors, ready]);

  // -- landing centre layer data update --------------------------------------------------
  // These are a GL circle layer, NOT DOM markers — see the `landing-centres` layer in the
  // map-load handler for why that distinction is load-bearing.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("landing-centres") as maplibregl.GeoJSONSource | undefined)?.setData(
      landingCentres ?? EMPTY_FC,
    );
  }, [landingCentres, ready]);

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
        const path = existing.el.querySelector<SVGPathElement>(".fm-vessel-boat path");
        const label = existing.el.querySelector<HTMLElement>(".fm-vessel-label");
        // Heading rides a CSS custom property rather than an inline `transform`, so the
        // hover-grow rule can compose `rotate(var(--fm-heading)) scale(...)` instead of
        // having to `!important`-override the rotation (which made a hovered boat snap
        // to north).
        existing.el.style.setProperty("--fm-heading", `${v.heading_deg}deg`);
        if (path) path.setAttribute("fill", color);
        if (label) label.textContent = `${v.name} · SIM`;
        existing.popup.setDOMContent(buildVesselPopup(v));
        // The popup is no longer bound to the marker via `setPopup` (the one-at-a-time
        // gate owns opening it), so it does not ride along automatically. Keep an open
        // one over its vessel as the push loop advances the position.
        if (activePopupRef.current === existing.popup) {
          existing.popup.setLngLat([v.lon, v.lat]);
        }
        continue;
      }

      const el = document.createElement("div");
      el.className = "fm-vessel-marker";
      el.style.setProperty("--fm-heading", `${v.heading_deg}deg`);
      // Selection AND the popup are both driven from this one listener, rather than
      // letting `Marker.setPopup` toggle the popup on its own. MapLibre's own toggle
      // knows nothing about the harbour and geofence popups, so it would happily leave a
      // vessel card open on top of a landing-centre card. Routing through `showPopup`
      // keeps exactly one box open across all three kinds. `stopPropagation` keeps this
      // click from also reaching the map, where it would immediately close the popup we
      // are opening.
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        onSelectVesselRef.current?.(v.vessel_id);
        const entry = markersRef.current.get(v.vessel_id);
        const current = mapRef.current;
        if (entry && current) showPopup(current, entry.popup, entry.marker.getLngLat());
      });
      // Ring, hull and label are ALL absolutely positioned inside a marker box that is
      // exactly the hull glyph's size (see console.css). MapLibre anchors this box's
      // centre on the vessel's lat/lon with a `translate(-50%,-50%)`, so anything left in
      // normal flow — the label used to be — grows the box and pushes the hull off the
      // real position by a fixed *pixel* amount. A fixed pixel offset is a shrinking
      // *geographic* offset as you zoom in, which is why the boats appeared to slide
      // across the chart while zooming. Keep every child out of flow.
      el.innerHTML = `
        <span class="fm-vessel-sim-ring" title="Simulated position — no live AIS feed"></span>
        <span class="fm-vessel-boat">${boatIconSvg(color)}</span>
        <span class="fm-vessel-label"></span>
      `;
      const labelEl = el.querySelector<HTMLElement>(".fm-vessel-label");
      if (labelEl) labelEl.textContent = `${v.name} · SIM`;
      const popup = new maplibregl.Popup({ closeButton: true, offset: 14, maxWidth: "280px" }).setDOMContent(
        buildVesselPopup(v),
      );
      // Deliberately NOT `.setPopup(popup)` — the element listener above owns opening it,
      // so every popup on this map goes through the same one-at-a-time gate.
      const marker = new maplibregl.Marker({
        element: el,
        anchor: "center",
        subpixelPositioning: true,
      })
        .setLngLat([v.lon, v.lat])
        .addTo(map);
      markersRef.current.set(v.vessel_id, { marker, popup, el });
    }

    for (const [id, entry] of markersRef.current) {
      if (!seen.has(id)) {
        if (activePopupRef.current === entry.popup) {
          entry.popup.remove();
          activePopupRef.current = null;
        }
        entry.marker.remove();
        markersRef.current.delete(id);
      }
    }
  }, [vessels, ready]);

  // -- selection highlight -------------------------------------------------------------
  // Kept in its own effect (a class toggle on existing elements) rather than folded into
  // the marker effect above, so changing the selection never touches marker geometry.
  useEffect(() => {
    for (const [id, entry] of markersRef.current) {
      entry.el.classList.toggle("fm-vessel-marker--selected", id === selectedVesselId);
      entry.el.classList.toggle(
        "fm-vessel-marker--dimmed",
        selectedVesselId != null && id !== selectedVesselId,
      );
    }
  }, [selectedVesselId, vessels, ready]);

  // -- ease to the selected vessel -----------------------------------------------------
  // Only when the selection actually changes: a selected vessel keeps moving on every
  // push tick, and chasing it would take control of the map away from the operator.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (selectedVesselId == null) {
      easedToVesselRef.current = null;
      return;
    }
    if (easedToVesselRef.current === selectedVesselId) return;
    const vessel = vessels.find((v) => v.vessel_id === selectedVesselId);
    if (!vessel) return;
    easedToVesselRef.current = selectedVesselId;
    // Pan only — the chart is fixed-zoom (see map construction), so this recentres on the
    // vessel at the same scale rather than diving toward it.
    map.easeTo({ center: [vessel.lon, vessel.lat], duration: 900 });
  }, [selectedVesselId, vessels, ready]);

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
        <div className="fm-tile-warning">Basemap tiles unreachable — showing plain chart colour and coastline only.</div>
      )}
      {usingFallbackBasemap && !tileFailed && (
        <div className="fm-tile-warning fm-tile-warning--info">
          ISRO Bhuvan basemap unavailable — showing GEBCO bathymetric chart instead.
        </div>
      )}
      <div className="fm-banner">SIMULATED FLEET — no public real-time AIS feed for Indian small boats.</div>
      <div className={`fm-legend${legendOpen ? "" : " fm-legend--collapsed"}`}>
        <button
          type="button"
          className="fm-legend__toggle"
          aria-expanded={legendOpen}
          onClick={() => setLegendOpen((open) => !open)}
        >
          <span className="fm-legend__toggle-label">Legend</span>
          <span className="fm-legend__chevron" aria-hidden="true" />
        </button>
        {legendOpen && (
          <>
            <div className="fm-legend__body">
              <div className="fm-legend__group">
                <div className="fm-legend__heading">Vessel</div>
                <LegendSwatch color="var(--verdict-go)" label="Go" />
                <LegendSwatch color="var(--verdict-caution)" label="Caution" />
                <LegendSwatch color="var(--verdict-stop)" label="Do not advise" />
                <LegendSwatch color="var(--ink-500)" label="No verdict" />
              </div>
              <div className="fm-legend__group">
                <div className="fm-legend__heading">Boundary</div>
                <LegendSwatch
                  color={severityVar("legal_hard")}
                  label="Historic waters 1974"
                  title="IMBL_HISTORIC_WATERS — legally hard, solid line with a dark casing."
                />
                <LegendSwatch
                  color={severityVar("legal_hard")}
                  label="Maritime boundary 1976"
                  dashed
                  title="IMBL_MARITIME_BOUNDARY — legally hard, a different treaty regime from the 1974 line."
                />
              </div>
              <div className="fm-legend__group">
                <div className="fm-legend__heading">Zone</div>
                <LegendSwatch color={severityVar("hazard")} label="Hazard exclusion" />
                <LegendSwatch color={severityVar("restricted")} label="Marine park" />
                <LegendSwatch color={severityVar("advisory")} label="Eco-sensitive" />
              </div>
              <div className="fm-legend__group">
                <div className="fm-legend__heading">Fishing zone</div>
                <LegendSwatch color="var(--pfz-official)" label="Official PFZ" title={officialNote} />
                <LegendSwatch color="var(--pfz-derived)" label="Derived PFZ" dashed title={derivedNote} />
              </div>
              <div className="fm-legend__group">
                <div className="fm-legend__heading">Cyclone</div>
                <LegendSwatch color={severityVar("hazard")} label="Exclusion area" title={hazardNote} />
                <LegendSwatch color="var(--hazard-track)" label="Track" title={hazardNote} />
              </div>
              <div className="fm-legend__group">
                <div className="fm-legend__heading">Reference</div>
                <LegendSwatch color="var(--land-fill, #16405c)" label="Coastline" title="Natural Earth 10m land, held locally." />
                <div className="fm-legend__item" title="INCOIS landing centres — click one for its district and sector.">
                  <span className="fm-legend__harbour-icon" dangerouslySetInnerHTML={{ __html: harbourIconSvg() }} />
                  <span className="fm-legend__label">Landing centre</span>
                </div>
              </div>
            </div>
            {/* CLAUDE.md invariant 4 — staleness is surfaced, never hidden. The legend
                itself is swatch+name only; the two things that actually go stale (the
                official PFZ advisory's own date and whether a cyclone is active) stay
                visible here as short values, with the full sentence on hover. */}
            <div className="fm-legend__status">
              <span
                className={`fm-legend__stat${pfzOfficial?.geometry ? "" : " fm-legend__stat--muted"}`}
                title={officialNote}
              >
                PFZ {pfzOfficial ? (pfzOfficial.advisory_date ?? (pfzOfficial.geometry ? "undated" : "none today")) : "…"}
              </span>
              <span
                className={`fm-legend__stat${hazards && !hazards.no_active_hazard ? " fm-legend__stat--alert" : ""}`}
                title={hazardNote}
              >
                {hazards ? (hazards.no_active_hazard ? "No cyclone" : "Cyclone active") : "Cyclone …"}
              </span>
              <span className="fm-legend__stat" title="Simulated positions — dashed ring on every marker, not live AIS.">
                {vessels.length} sim
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function LegendSwatch({
  color,
  label,
  dashed,
  title,
}: {
  color: string;
  label: string;
  dashed?: boolean;
  title?: string;
}) {
  return (
    <div className="fm-legend__item" title={title}>
      <span
        className={`fm-legend__swatch${dashed ? " fm-legend__swatch--dashed" : ""}`}
        style={dashed ? { borderColor: color } : { background: color }}
      />
      <span className="fm-legend__label">{label}</span>
    </div>
  );
}
