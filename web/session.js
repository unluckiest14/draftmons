/* Who this browser is, per season.
 *
 * The backend has no accounts by design: joining a season returns one bearer
 * token, and holding that token *is* being that player. So "logging in" here
 * is entering a join code once, and "staying logged in" is keeping the token.
 *
 * It lives in localStorage, which carries the consequence the backend already
 * states: the token cannot be reissued. Clearing site data, or opening the
 * league on a phone, is a new browser that has to join again — and a season
 * only has so many seats. The UI says so at the point where it matters rather
 * than burying it here.
 *
 * Tokens are per season, because one person may be a player in one league and
 * the commissioner of another, and the admin token is stored separately from
 * the player token for the same reason.
 */

const KEY = 'draftmons.session.v1';

function readAll() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || '{}');
  } catch {
    // Unreadable or unavailable storage is the same as not having joined.
    return {};
  }
}

function writeAll(all) {
  try {
    localStorage.setItem(KEY, JSON.stringify(all));
    return true;
  } catch {
    return false;
  }
}

/** What this browser holds for one season: {player, admin, team} or {}. */
export function forSeason(seasonId) {
  return readAll()[String(seasonId)] || {};
}

export function remember(seasonId, patch) {
  const all = readAll();
  const key = String(seasonId);
  all[key] = { ...(all[key] || {}), ...patch };
  return writeAll(all);
}

export function forget(seasonId) {
  const all = readAll();
  delete all[String(seasonId)];
  writeAll(all);
}

/** Authorization header for a season, preferring the admin token when asked. */
export function headers(seasonId, { admin = false } = {}) {
  const held = forSeason(seasonId);
  const token = admin ? held.admin : held.player || held.admin;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export const isPlayer = (seasonId) => Boolean(forSeason(seasonId).player);
export const isAdmin = (seasonId) => Boolean(forSeason(seasonId).admin);

/** The team this browser drafts for, or null if it is only spectating. */
export const myTeamId = (seasonId) => forSeason(seasonId).team?.id ?? null;
