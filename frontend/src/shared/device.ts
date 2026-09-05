/**
 * Utility functions for device and environment detection.
 */

const MOBILE_UA_REGEX =
  /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i;

/**
 * Returns true if the client is running on a mobile or small touch device.
 */
export function isMobileDevice(): boolean {
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    return false;
  }

  // Check user agent
  const userAgentMatches = MOBILE_UA_REGEX.test(navigator.userAgent);
  if (userAgentMatches) {
    return true;
  }

  // Check screen width and touch capability (e.g. mobile emulation or responsive viewports)
  const isNarrowScreen = window.innerWidth < 768;
  const hasTouchCapability =
    (typeof navigator.maxTouchPoints === "number" && navigator.maxTouchPoints > 0) ||
    window.matchMedia("(pointer: coarse)").matches;

  return isNarrowScreen && hasTouchCapability;
}
