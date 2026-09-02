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
import type { RegionInfo } from "@shared/types";
import { basemapCenterLngLat, severityColor, toLngLat } from "./format";
import type { OwnPosition } from "./useOwnPosition";

// Mirrors index.css's --ink-800 / --ink-700 — maplibre paint properties need literal
// colour values, not CSS custom properties, so these are duplicated deliberately.
const OCEAN_BG = "#0f2b40";

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
  const centeredOnceRef = useRef(false);
  const bhuvanFailedRef = useRef(false);
  const [basemapFailed, setBasemapFailed] = useState(false);
  const [mapReady, setMapReady] = useState(false);

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

  return (
    <div className="map-view">
      <div ref={containerRef} className="map-view__canvas" />
      {!region ? <div className="map-view__overlay">Loading chart…</div> : null}
      {basemapFailed ? <div className="map-view__badge">Chart imagery unavailable — showing plain chart</div> : null}
    </div>
  );
}
