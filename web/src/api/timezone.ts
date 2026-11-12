// Read the device's IANA timezone (e.g. "Europe/Amsterdam", "Asia/Seoul")
// from the browser. iOS Safari and modern browsers return whatever the OS
// is currently set to, so a travelling user automatically reports the zone
// they're actually in once their device updates.
//
// We send this to the backend on every /ask/stream request so the agent can
// render "now" and the get_current_time tool in the user's local clock,
// rather than a server-side default that can't follow them.

/**
 * Return the device's IANA timezone, or `undefined` if the browser
 * does not support `Intl` (vanishingly rare on modern devices, but be
 * defensive — never throw from a request-shaping helper).
 */
export function getDeviceTimezone(): string | undefined {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    if (typeof tz === 'string' && tz.length > 0) {
      return tz
    }
  } catch {
    // Intl unavailable — fall through and let the backend use its default.
  }
  return undefined
}
