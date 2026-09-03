/**
 * MapLibre GL map: Bhuvan WMS raster basemap (ISRO/NRSC, keyless) with a hard-coded
 * solid-ocean fallback, geofence layers colour-coded by class/severity, the planned
 * route when present, and the user's own live device position (never labelled
 * "simulated" — that label is reserved for the fleet the console renders).
 *
 * The Bhuvan tile server is external and this sandbox may well not reach it: every tile
 * source is layered on top of an always-present solid-colour background layer, and any
 * tile error (or a load that never completes within a few seconds) removes the raster
 * layer rather than leaving a blank/broken map.
 */
import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { getHazards, getPfzDerived, getPfzOfficial } from "@shared/api";
import type { HazardsPayload, PfzDerivedPayload, PfzOfficialPayload, RegionInfo } from "@shared/types";
import { basemapCenterLngLat, severityColor, toLngLat } from "./format";
import type { OwnPosition } from "./useOwnPosition";

// Mirrors index.css's --ink-800 / --ink-700 — maplibre paint properties need literal
// colour values, not CSS custom properties, so these are duplicated deliberately.
const OCEAN_BG = "#0f2b40";

// Mirrors index.css's --pfz-official / --pfz-derived / --hazard-track — same reasoning
// as OCEAN_BG above (maplibre paint properties need literal colour values). Deliberately
// distinct from every geofence-severity colour and from the route line's --accent, so
// "official PFZ line", "derived PFZ zone" and "cyclone track" never read as the same
// thing as each other or as an existing layer (CLAUDE.md: never present a derived
// product as the official INCOIS advisory).
const PFZ_OFFICIAL_COLOR = "#2dd4bf";
const PFZ_DERIVED_COLOR = "#7c93ff";
const HAZARD_FILL_COLOR = "#e0a815"; // == index.css --severity-hazard: same meaning as the
// geofence layer's dynamic HAZARD_EXCLUSION class, so it deliberately reuses that colour.
const HAZARD_TRACK_COLOR = "#ff5da2";

const EMPTY_FC: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

/** A generous small-boat-scale bbox around the vessel's own position (CLAUDE.md's 0–50 nm
 * small motorised boat class) for the derived-PFZ / hazard queries — "around the vessel",
 * not the whole region. ~0.6° ≈ 65 km padding. */
function ownPositionBbox(lat: number, lon: number): [number, number, number, number] {
  const pad = 0.6;
  return [lon - pad, lat - pad, lon + pad, lat + pad];
}

function buildWmsTileUrl(wmsUrl: string, layer: string): string {
  const params = new URLSearchParams({
    service: "WMS",
    version: "1.1.1",
    request: "GetMap",
    layers: layer,
    styles: "",
    format: "image/png",
    transparent: "true",
    srs: "EPSG:3857",
    width: "256",
    height: "256",
  });
  // {bbox-epsg-3857} is maplibre/mapbox GL's own placeholder for WMS-style raster
  // sources — it substitutes each tile's bbox in EPSG:3857 at request time. (CLAUDE.md's
  // sibling brief spelled this `{bbox-3857}`; that literal token is not recognised by
  // maplibre and every tile request would 404 — corrected here, flagged in the report.)
  return `${wmsUrl}?${params.toString()}&bbox={bbox-epsg-3857}`;
}

/**
 * Load-tests one sample tile as a plain `<img>` before committing to the raster source —
 * the same loading mechanism maplibre itself uses for raster tiles (no CORS mode, unlike
 * `fetch()`, so this does not misreport a CORS-only restriction as "unreachable").
 * Verified against the live server while building this: it currently returns HTTP 200
 * with a WMS `ServiceExceptionReport` XML body on every request (a Postgres auth failure
 * on ISRO's own tile backend) — a failure `map.on('error')` may not reliably surface
 * per-tile, since the request itself "succeeds". An `<img>` decode failure catches this
 * class of failure the way a raw fetch()/HTTP-status check cannot.
 */
function probeBasemapTile(wmsUrl: string, layer: string): Promise<boolean> {
  return new Promise((resolve) => {
    const probeUrl = buildWmsTileUrl(wmsUrl, layer).replace(
      "{bbox-epsg-3857}",
      "8811321.4,1023372.6,8850350.5,1062401.7",
    );
    const img = new Image();
    const timer = setTimeout(() => {
      img.onload = null;
      img.onerror = null;
      resolve(false);
    }, 5000);
    img.onload = () => {
      clearTimeout(timer);
      resolve(true);
    };
    img.onerror = () => {
      clearTimeout(timer);
      resolve(false);
    };
    img.src = probeUrl;
  });
}

function polygonOnly(fc: GeoJSON.FeatureCollection): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features: fc.features.filter((f) => f.geometry?.type === "Polygon" || f.geometry?.type === "MultiPolygon") };
}

export function MapView({
  region,
  position,
  geofenceGeoJson,
  route,
}: {
  region: RegionInfo | null;
  position: OwnPosition;
  geofenceGeoJson: GeoJSON.FeatureCollection | null;
  route: Record<string, unknown> | null;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const geofenceLayersAddedRef = useRef(false);
  const routeLayerAddedRef = useRef(false);
  const pfzOfficialLayerAddedRef = useRef(false);
  const pfzDerivedLayerAddedRef = useRef(false);
  const hazardLayerAddedRef = useRef(false);
  const hazardTrackLayerAddedRef = useRef(false);
  const centeredOnceRef = useRef(false);
  const bhuvanFailedRef = useRef(false);
  const [basemapFailed, setBasemapFailed] = useState(false);
  const [mapReady, setMapReady] = useState(false);
  const [pfzOfficial, setPfzOfficial] = useState<PfzOfficialPayload | null>(null);
  const [pfzDerived, setPfzDerived] = useState<PfzDerivedPayload | null>(null);
  const [hazards, setHazards] = useState<HazardsPayload | null>(null);

  // -- create the map once region config is known --------------------------------------
  useEffect(() => {
    if (!region || !containerRef.current || mapRef.current) return;

    const center = basemapCenterLngLat(region.basemap as Record<string, unknown>, region.bbox);
    const zoomRaw = (region.basemap as Record<string, unknown>)?.zoom;
    const zoom = typeof zoomRaw === "number" ? zoomRaw : 8;

    let map: maplibregl.Map;
    try {
      map = new maplibregl.Map({
        container: containerRef.current,
        style: {
          version: 8,
          sources: {},
          layers: [{ id: "ocean-bg", type: "background", paint: { "background-color": OCEAN_BG } }],
        },
        center,
        zoom,
        attributionControl: false,
      });
    } catch (err) {
      console.warn("[MapView] failed to construct maplibre map:", err);
      return;
    }
    mapRef.current = map;

    const attribution = String((region.basemap as Record<string, unknown>)?.attribution ?? "");
    map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: attribution }));
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    function dropBhuvan() {
      if (bhuvanFailedRef.current) return;
      bhuvanFailedRef.current = true;
      try {
        if (map.getLayer("bhuvan-layer")) map.removeLayer("bhuvan-layer");
        if (map.getSource("bhuvan")) map.removeSource("bhuvan");
      } catch {
        /* map may already be torn down */
      }
      setBasemapFailed(true);
    }

    map.on("error", (e) => {
      // The only network-fetched source on this map is the Bhuvan WMS raster (geofence
      // and route sources are handed inline GeoJSON data, no fetch involved) — any
      // 'error' event here is treated as "the ISRO tile server is unreachable".
      console.warn("[MapView] maplibre error (likely Bhuvan WMS unreachable):", e?.error ?? e);
      dropBhuvan();
    });

    map.on("load", () => {
      const wmsUrl = String((region.basemap as Record<string, unknown>)?.wms_url ?? "");
      const layer = String((region.basemap as Record<string, unknown>)?.layer ?? "");
      if (wmsUrl && layer) {
        probeBasemapTile(wmsUrl, layer).then((ok) => {
          if (bhuvanFailedRef.current) return; // torn down or already failed meanwhile
          if (!ok) {
            setBasemapFailed(true);
            return;
          }
          try {
            map.addSource("bhuvan", {
              type: "raster",
              tiles: [buildWmsTileUrl(wmsUrl, layer)],
              tileSize: 256,
            });
            map.addLayer({ id: "bhuvan-layer", type: "raster", source: "bhuvan", paint: { "raster-opacity": 1 } });
            // Belt-and-braces on top of the probe above: if the source still isn't
            // loaded after a few seconds, treat it as unreachable rather than risk a
            // silently blank layer.
            setTimeout(() => {
              if (!bhuvanFailedRef.current && !map.isSourceLoaded?.("bhuvan")) dropBhuvan();
            }, 6000);
          } catch (err) {
            console.warn("[MapView] could not add Bhuvan source:", err);
            dropBhuvan();
          }
        });
      } else {
        setBasemapFailed(true);
      }
      setMapReady(true);
    });

    return () => {
      map.remove();
      mapRef.current = null;
      markerRef.current = null;
      geofenceLayersAddedRef.current = false;
      routeLayerAddedRef.current = false;
      pfzOfficialLayerAddedRef.current = false;
      pfzDerivedLayerAddedRef.current = false;
      hazardLayerAddedRef.current = false;
      hazardTrackLayerAddedRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [region]);

  // -- own position marker ---------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !position.ready) return;

    if (!markerRef.current) {
      const el = document.createElement("div");
      el.className = "own-position-marker";
      el.innerHTML = '<span class="own-position-marker__dot"></span><span class="own-position-marker__label">You</span>';
      markerRef.current = new maplibregl.Marker({ element: el, anchor: "bottom" });
    }
    markerRef.current.setLngLat(toLngLat([position.lat, position.lon])).addTo(map);

    if (!centeredOnceRef.current) {
      centeredOnceRef.current = true;
      map.easeTo({ center: toLngLat([position.lat, position.lon]), zoom: Math.max(map.getZoom(), 9) });
    }
  }, [mapReady, position.ready, position.lat, position.lon]);

  // -- geofence layers ---------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !geofenceGeoJson) return;

    const apply = () => {
      if (!geofenceLayersAddedRef.current) {
        map.addSource("geofences", { type: "geojson", data: polygonOnly(geofenceGeoJson) });
        map.addSource("geofences-lines", { type: "geojson", data: geofenceGeoJson });
        map.addLayer({
          id: "geofence-fill",
          type: "fill",
          source: "geofences",
          paint: { "fill-color": ["coalesce", ["get", "colour"], severityColor(undefined)], "fill-opacity": 0.22 },
        });
        map.addLayer({
          id: "geofence-line",
          type: "line",
          source: "geofences-lines",
          paint: {
            "line-color": ["coalesce", ["get", "colour"], severityColor(undefined)],
            "line-width": 2.5,
            "line-opacity": 0.9,
          },
        });
        geofenceLayersAddedRef.current = true;
      } else {
        (map.getSource("geofences") as maplibregl.GeoJSONSource | undefined)?.setData(polygonOnly(geofenceGeoJson));
        (map.getSource("geofences-lines") as maplibregl.GeoJSONSource | undefined)?.setData(geofenceGeoJson);
      }
    };

    if (map.isStyleLoaded()) apply();
    else map.once("idle", apply);
  }, [mapReady, geofenceGeoJson]);

  // -- route line ---------------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;

    const waypoints = route?.waypoints;
    if (!Array.isArray(waypoints) || waypoints.length < 2) {
      if (routeLayerAddedRef.current) {
        (map.getSource("route") as maplibregl.GeoJSONSource | undefined)?.setData({
          type: "FeatureCollection",
          features: [],
        });
      }
      return;
    }

    const coords = (waypoints as [number, number][]).map(toLngLat);
    const data: GeoJSON.FeatureCollection = {
      type: "FeatureCollection",
      features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords } }],
    };

    const apply = () => {
      if (!routeLayerAddedRef.current) {
        map.addSource("route", { type: "geojson", data });
        map.addLayer({
          id: "route-line",
          type: "line",
          source: "route",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#ff9d3c", "line-width": 4, "line-dasharray": [0.1, 1.4] },
        });
        routeLayerAddedRef.current = true;
      } else {
        (map.getSource("route") as maplibregl.GeoJSONSource | undefined)?.setData(data);
      }
    };

    if (map.isStyleLoaded()) apply();
    else map.once("idle", apply);
  }, [mapReady, route]);

  // -- fetch PFZ official/derived + hazards around the vessel's own position -----------
  // Self-contained fetch, mirroring the fetch-and-refresh lifecycle BoatApp.tsx already
  // uses for geofences (fetch on mount / on the relevant change, .catch -> console.warn,
  // leave the layer empty rather than erroring) — kept local to this component since
  // this data is map-only and not needed elsewhere in the tree.
  useEffect(() => {
    if (!position.ready) return;
    let cancelled = false;

    getPfzOfficial(position.lat, position.lon)
      .then((res) => {
        if (!cancelled) setPfzOfficial(res.payload);
      })
      .catch((err) => console.warn("[MapView] failed to fetch official PFZ line:", err));

    const bbox = ownPositionBbox(position.lat, position.lon);
    getPfzDerived({ bbox })
      .then((res) => {
        if (!cancelled) setPfzDerived(res.payload);
      })
      .catch((err) => console.warn("[MapView] failed to fetch derived PFZ zones:", err));

    getHazards({ bbox })
      .then((res) => {
        if (!cancelled) setHazards(res.payload);
      })
      .catch((err) => console.warn("[MapView] failed to fetch hazards:", err));

    return () => {
      cancelled = true;
    };
  }, [position.ready, position.lat, position.lon]);

  // -- official PFZ line: solid, one consistent colour ------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;

    const data: GeoJSON.FeatureCollection = pfzOfficial?.geometry
      ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: pfzOfficial.geometry }] }
      : EMPTY_FC;

    const apply = () => {
      if (!pfzOfficialLayerAddedRef.current) {
        map.addSource("pfz-official", { type: "geojson", data });
        map.addLayer({
          id: "pfz-official-line",
          type: "line",
          source: "pfz-official",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": PFZ_OFFICIAL_COLOR, "line-width": 3, "line-opacity": 0.95 },
        });
        pfzOfficialLayerAddedRef.current = true;
      } else {
        (map.getSource("pfz-official") as maplibregl.GeoJSONSource | undefined)?.setData(data);
      }
    };

    if (map.isStyleLoaded()) apply();
    else map.once("idle", apply);
  }, [mapReady, pfzOfficial]);

  // -- derived PFZ zones: fill + dashed border, deliberately unlike the solid official
  // line above — CLAUDE.md: never presented as the official INCOIS advisory -------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;

    const data = pfzDerived?.zones ?? EMPTY_FC;

    const apply = () => {
      if (!pfzDerivedLayerAddedRef.current) {
        map.addSource("pfz-derived", { type: "geojson", data });
        map.addLayer({
          id: "pfz-derived-fill",
          type: "fill",
          source: "pfz-derived",
          paint: { "fill-color": PFZ_DERIVED_COLOR, "fill-opacity": 0.18 },
        });
        map.addLayer({
          id: "pfz-derived-line",
          type: "line",
          source: "pfz-derived",
          paint: { "line-color": PFZ_DERIVED_COLOR, "line-width": 1.5, "line-dasharray": [2, 2], "line-opacity": 0.9 },
        });
        pfzDerivedLayerAddedRef.current = true;
      } else {
        (map.getSource("pfz-derived") as maplibregl.GeoJSONSource | undefined)?.setData(data);
      }
    };

    if (map.isStyleLoaded()) apply();
    else map.once("idle", apply);
  }, [mapReady, pfzDerived]);

  // -- hazard exclusion polygons + cyclone track: two separate layers, deliberately
  // distinct styles — PLAN.md Phase 6 / CLAUDE.md: "cyclone track and cone overlaid" as
  // two distinct visual things, not one -----------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;

    const polyData: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: hazards?.polygons ?? [] };
    const trackData: GeoJSON.FeatureCollection = hazards?.cyclone_track ?? EMPTY_FC;

    const apply = () => {
      if (!hazardLayerAddedRef.current) {
        map.addSource("hazard-polygons", { type: "geojson", data: polyData });
        map.addLayer({
          id: "hazard-fill",
          type: "fill",
          source: "hazard-polygons",
          paint: { "fill-color": HAZARD_FILL_COLOR, "fill-opacity": 0.25 },
        });
        map.addLayer({
          id: "hazard-line",
          type: "line",
          source: "hazard-polygons",
          paint: { "line-color": HAZARD_FILL_COLOR, "line-width": 2, "line-opacity": 0.9 },
        });
        hazardLayerAddedRef.current = true;
      } else {
        (map.getSource("hazard-polygons") as maplibregl.GeoJSONSource | undefined)?.setData(polyData);
      }

      if (!hazardTrackLayerAddedRef.current) {
        map.addSource("hazard-track", { type: "geojson", data: trackData });
        map.addLayer({
          id: "hazard-track-line",
          type: "line",
          source: "hazard-track",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": HAZARD_TRACK_COLOR, "line-width": 2.5, "line-dasharray": [3, 1.5], "line-opacity": 0.95 },
        });
        hazardTrackLayerAddedRef.current = true;
      } else {
        (map.getSource("hazard-track") as maplibregl.GeoJSONSource | undefined)?.setData(trackData);
      }
    };

    if (map.isStyleLoaded()) apply();
    else map.once("idle", apply);
  }, [mapReady, hazards]);

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

  return (
    <div className="map-view-wrap">
      <div className="map-view">
        <div ref={containerRef} className="map-view__canvas" />
        {!region ? <div className="map-view__overlay">Loading chart…</div> : null}
        {basemapFailed ? <div className="map-view__badge">Chart imagery unavailable — showing plain chart</div> : null}
        <div className="map-view__legend">
          <div className="map-view__legend-item">
            <span className="map-view__swatch" style={{ background: PFZ_OFFICIAL_COLOR }} />
            Official PFZ line
          </div>
          <div className="map-view__legend-item">
            <span className="map-view__swatch map-view__swatch--derived" style={{ borderColor: PFZ_DERIVED_COLOR }} />
            Derived PFZ (indicative)
          </div>
          <div className="map-view__legend-item">
            <span className="map-view__swatch map-view__swatch--fill" style={{ background: HAZARD_FILL_COLOR }} />
            Hazard exclusion
          </div>
          <div className="map-view__legend-item">
            <span className="map-view__swatch" style={{ background: HAZARD_TRACK_COLOR }} />
            Cyclone track
          </div>
        </div>
      </div>
      <div className="map-view__notes">
        <div className="map-view__note">{officialNote}</div>
        <div className="map-view__note map-view__note--derived">{derivedNote}</div>
        <div className="map-view__note">{hazardNote}</div>
      </div>
    </div>
  );
}
