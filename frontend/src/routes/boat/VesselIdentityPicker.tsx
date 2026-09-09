/**
 * "Which tracked vessel is this phone?" — lets a demo device identify as one of the
 * simulated fleet so a console broadcast targeted at that vessel (`POST
 * /api/alerts/broadcast`) reaches this exact screen, not just a fleet-wide one. Backs
 * `usePushAlerts.ts`'s `vesselId` scoping. Persisted in the URL (`?vessel_id=`) so the
 * identity survives a refresh and can be shared/bookmarked for a specific demo phone.
 */
import { useEffect, useState } from "react";
import { getFleet } from "@shared/api";
import type { VesselState } from "@shared/types";

export function VesselIdentityPicker({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (vesselId: string | null) => void;
}) {
  const [vessels, setVessels] = useState<VesselState[]>([]);

  useEffect(() => {
    let cancelled = false;
    getFleet()
      .then((res) => {
        if (!cancelled) setVessels(res.vessels);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  if (vessels.length === 0) return null;

  return (
    <label className="vessel-identity" title="Identify this device as one tracked vessel, so a console alert sent to it reaches this screen specifically.">
      <span className="vessel-identity__label">Identify as</span>
      <select
        className="vessel-identity__select"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">Any vessel (fleet-wide alerts only)</option>
        {vessels.map((v) => (
          <option key={v.vessel_id} value={v.vessel_id}>
            {v.name}
          </option>
        ))}
      </select>
    </label>
  );
}
