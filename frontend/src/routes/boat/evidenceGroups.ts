/**
 * Grouping logic for the boat Evidence tab.
 *
 * The backend returns evidence as a flat list — one row per observation, sometimes 30+
 * of them for a single answer. Grouping happens here, entirely client-side, and is
 * presentation only: it never changes a value, adds one, or drops one. It only decides
 * how the rows the backend already sent are arranged on screen, so that "sources
 * disagree, one governs" (CLAUDE.md's technical centerpiece — "Do not average
 * disagreeing sources. Show them side by side and say which governs.") reads as one
 * glance instead of a 38-card scroll.
 *
 * A single `variable` (e.g. `significant_wave_height`) can appear more than once in the
 * same row list:
 *   - from different sources, disagreeing (INCOIS 0.56 m vs. Open-Meteo 1.10 m) — both
 *     are kept, side by side, never averaged.
 *   - from the SAME source at a different position/time (e.g. Open-Meteo `wind_speed` at
 *     the vessel vs. at the destination, 13.88 kn vs. 4.97 kn) — both are kept too,
 *     distinguished by `acquired_at`/order, never silently deduplicated.
 *
 * `groupEvidence` folds every row sharing a `variable` into one `EvidenceVariableGroup`
 * without deduplicating, dropping, or averaging anything: the number of readings across
 * every returned group always equals `rows.length`.
 */
import { readEvidenceAcquiredAt, readEvidenceDisplay, readEvidenceResolution } from "./format";

export type EvidenceCategory =
  | "warnings" // Official warnings & signals
  | "sea" // Sea state
  | "wind" // Wind
  | "weather" // Weather & visibility
  | "boundaries" // Boundaries & harbour
  | "other"; // anything unmatched — never drop a row

export interface EvidenceSourceReading {
  row: Record<string, unknown>; // the original row, untouched
  sourceName: string;
  authority: string;
  display: string;
  resolution: string;
  freshness: string;
  acquiredAt: string | null;
  isDerived: boolean;
  governs: boolean;
}

export interface EvidenceVariableGroup {
  variable: string; // raw variable key
  label: string; // human label, English, e.g. "Significant wave height"
  category: EvidenceCategory;
  readings: EvidenceSourceReading[]; // governing readings first, then the rest
  governs: boolean; // true if ANY reading governs
  disagreement: boolean; // true when >1 reading from DIFFERENT sources
}

export interface EvidenceCategoryGroup {
  category: EvidenceCategory;
  label: string; // English section title
  groups: EvidenceVariableGroup[];
}

const CATEGORY_ORDER: EvidenceCategory[] = ["warnings", "sea", "wind", "weather", "boundaries", "other"];

const CATEGORY_LABEL: Record<EvidenceCategory, string> = {
  warnings: "Official warnings & signals",
  sea: "Sea state",
  wind: "Wind",
  weather: "Weather & visibility",
  boundaries: "Boundaries & harbour",
  other: "Other",
};

/**
 * Known variable -> category. This static map is authoritative for every variable the
 * backend is documented to emit (CLAUDE.md / this task's brief). The keyword fallback
 * below exists only to catch a variable this map has not been told about yet — a row
 * must never be dropped for lack of a category.
 */
const VARIABLE_CATEGORY: Record<string, EvidenceCategory> = {
  // IMD descriptive fields
  wind_description: "wind",
  weather_description: "weather",
  visibility_description: "weather",
  sea_condition: "sea",
  port_signal: "warnings",
  storm_surge_tidal_warning: "warnings",
  // derived
  douglas_band: "sea",
  wave_steepness: "sea",
  // sea state (INCOIS + Open-Meteo/ECMWF)
  significant_wave_height: "sea",
  swell_wave_height: "sea",
  wind_wave_height: "sea",
  wave_period: "sea",
  swell_wave_period: "sea",
  // wind (Open-Meteo/ECMWF)
  wind_speed: "wind",
  wind_gust: "wind",
  wind_direction: "wind",
  // weather & visibility (Open-Meteo/ECMWF)
  precipitation: "weather",
  convective_available_potential_energy: "weather",
  visibility: "weather",
  air_temperature: "weather",
  pressure_msl: "weather",
  cloud_cover: "weather",
  // boundaries & harbour (INCOIS)
  geofence_distance: "boundaries",
  landing_centre_distance: "boundaries",
};

const HUMAN_LABEL: Record<string, string> = {
  wind_description: "Wind description",
  weather_description: "Weather description",
  visibility_description: "Visibility description",
  sea_condition: "IMD sea condition",
  port_signal: "Port signal",
  storm_surge_tidal_warning: "Storm surge / tidal warning",
  douglas_band: "Douglas sea state",
  wave_steepness: "Wave steepness",
  significant_wave_height: "Significant wave height",
  swell_wave_height: "Swell wave height",
  wind_wave_height: "Wind wave height",
  wave_period: "Wave period",
  swell_wave_period: "Swell wave period",
  wind_speed: "Wind speed",
  wind_gust: "Wind gust",
  wind_direction: "Wind direction",
  precipitation: "Precipitation",
  convective_available_potential_energy: "Instability (CAPE)",
  visibility: "Visibility",
  air_temperature: "Air temperature",
  pressure_msl: "Sea-level pressure",
  cloud_cover: "Cloud cover",
  geofence_distance: "Distance to nearest boundary",
  landing_centre_distance: "Distance to landing centre",
};

function sentenceCase(s: string): string {
  if (!s) return s;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function fallbackLabel(variable: string): string {
  return sentenceCase(variable.replace(/_/g, " "));
}

/**
 * Keyword fallback for a variable the static map above does not know about. Checked top
 * to bottom, first match wins. Never drop a row: anything matching nothing falls through
 * to "other".
 */
function fallbackCategory(variable: string): EvidenceCategory {
  const v = variable.toLowerCase();
  if (v.includes("wave") || v.includes("sea_") || v.includes("douglas") || v.includes("swell")) return "sea";
  if (v.includes("wind") || v.includes("gust")) return "wind";
  if (
    v.includes("visibility") ||
    v.includes("precip") ||
    v.includes("cloud") ||
    v.includes("temperature") ||
    v.includes("pressure") ||
    v.includes("convective")
  ) {
    return "weather";
  }
  if (v.includes("geofence") || v.includes("landing_centre") || v.includes("harbour")) return "boundaries";
  if (
    v.includes("port_signal") ||
    v.includes("storm_surge") ||
    v.includes("warning") ||
    v.includes("nowcast") ||
    v.includes("cyclone")
  ) {
    return "warnings";
  }
  return "other";
}

function categoryOf(variable: string): EvidenceCategory {
  return VARIABLE_CATEGORY[variable] ?? fallbackCategory(variable);
}

function labelOf(variable: string): string {
  return HUMAN_LABEL[variable] ?? fallbackLabel(variable);
}

function toReading(row: Record<string, unknown>): EvidenceSourceReading {
  return {
    row,
    sourceName: typeof row.source_name === "string" && row.source_name ? row.source_name : "unknown source",
    authority: typeof row.authority === "string" && row.authority ? row.authority : "unknown authority",
    display: readEvidenceDisplay(row),
    resolution: readEvidenceResolution(row),
    freshness: typeof row.freshness === "string" && row.freshness ? row.freshness : "unknown",
    acquiredAt: readEvidenceAcquiredAt(row),
    isDerived: Boolean(row.is_derived),
    governs: Boolean(row.governs),
  };
}

/**
 * Group a flat evidence row list by `variable`, then bucket each resulting group into a
 * category, ordered for the panel. Pure function — no React, no side effects, no
 * mutation of the input rows. Every row in `rows` appears in exactly one reading in the
 * output; `rows.length` always equals the sum of `readings.length` across every group.
 */
export function groupEvidence(rows: Record<string, unknown>[]): EvidenceCategoryGroup[] {
  const byVariable = new Map<string, EvidenceSourceReading[]>();
  const orderOfFirstAppearance: string[] = [];

  for (const row of rows ?? []) {
    const variable = typeof row?.variable === "string" && row.variable ? row.variable : "unknown";
    if (!byVariable.has(variable)) {
      byVariable.set(variable, []);
      orderOfFirstAppearance.push(variable);
    }
    byVariable.get(variable)!.push(toReading(row));
  }

  const variableGroups: EvidenceVariableGroup[] = orderOfFirstAppearance.map((variable) => {
    const allReadings = byVariable.get(variable)!;
    const governingReadings = allReadings.filter((r) => r.governs);
    const otherReadings = allReadings.filter((r) => !r.governs);
    const readings = [...governingReadings, ...otherReadings];
    const distinctSources = new Set(readings.map((r) => r.sourceName));
    return {
      variable,
      label: labelOf(variable),
      category: categoryOf(variable),
      readings,
      governs: governingReadings.length > 0,
      disagreement: distinctSources.size > 1,
    };
  });

  const byCategory = new Map<EvidenceCategory, EvidenceVariableGroup[]>();
  for (const group of variableGroups) {
    if (!byCategory.has(group.category)) byCategory.set(group.category, []);
    byCategory.get(group.category)!.push(group);
  }

  // Within a category: governing groups first, then disagreements, then the rest —
  // each bucket alphabetical by label.
  const rank = (g: EvidenceVariableGroup): number => (g.governs ? 0 : g.disagreement ? 1 : 2);

  const result: EvidenceCategoryGroup[] = [];
  for (const category of CATEGORY_ORDER) {
    const groups = byCategory.get(category);
    if (!groups || groups.length === 0) continue;
    groups.sort((a, b) => {
      const r = rank(a) - rank(b);
      if (r !== 0) return r;
      return a.label.localeCompare(b.label);
    });
    result.push({ category, label: CATEGORY_LABEL[category], groups });
  }
  return result;
}
