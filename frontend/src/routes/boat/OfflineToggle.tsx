/**
 * Visible "No signal" toggle — PLAN.md Phase 5's locked demo beat. Flipping it calls
 * `setManualOffline` (BoatApp owns the actual state + effect wiring); this component is
 * presentation only.
 */
export function OfflineToggle({
  offline,
  browserOffline,
  onChange,
}: {
  offline: boolean;
  browserOffline: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="offline-toggle">
      <button
        type="button"
        role="switch"
        aria-checked={offline}
        className={`offline-toggle__switch${offline ? " offline-toggle__switch--on" : ""}`}
        onClick={() => onChange(!offline)}
      >
        <span className="offline-toggle__knob" />
      </button>
      <div className="offline-toggle__label">
        <span className="offline-toggle__title">{offline ? "No signal" : "Online"}</span>
        {browserOffline && !offline ? (
          <span className="offline-toggle__hint">device reports no network — will behave as No signal</span>
        ) : null}
      </div>
    </div>
  );
}
