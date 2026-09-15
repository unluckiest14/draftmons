/* The draft queue: who you want next, in order.
 *
 * The complaint this answers came from the first live draft — "I've done
 * drafts before and I just get so annoyed cause it takes so long". You have
 * already decided most of your picks in the Planner; the queue carries that
 * decision into the draft so your turn is one confirmation instead of a search
 * through eight hundred Pokémon.
 *
 * Deliberately not an auto-pick. The queue offers; you still confirm, because
 * a draft moves and the Pokémon you queued three rounds ago may be the wrong
 * call now — or gone. What it removes is the digging, not the choice.
 *
 * Kept in this browser rather than on the server. It is a scratchpad for the
 * hour the draft takes, on the device you are drafting from, and putting it on
 * the server would mean a write per reorder for something nobody else can see.
 */

const KEY = 'draftmons.queue.v1';

function readAll() {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function writeAll(all) {
  try {
    localStorage.setItem(KEY, JSON.stringify(all));
    return true;
  } catch {
    /* private window; the queue lasts the session and no longer */
    return false;
  }
}

/* Listeners, so the Planner and the Draft tab agree without either importing
 * the other. Both are on screen in the same minute during a draft. */
const listeners = new Set();

export function onChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function announce() {
  for (const fn of listeners) fn();
}

/** The queued api_names for a season, in the order they will be offered. */
export function listFor(seasonId) {
  const list = readAll()[String(seasonId)];
  return Array.isArray(list) ? list : [];
}

export const isQueued = (seasonId, apiName) => listFor(seasonId).includes(apiName);

/** Where it sits in the queue, 1-based, or 0 when it is not queued. */
export const positionOf = (seasonId, apiName) => listFor(seasonId).indexOf(apiName) + 1;

function save(seasonId, list) {
  const all = readAll();
  if (list.length) all[String(seasonId)] = list;
  else delete all[String(seasonId)];
  const ok = writeAll(all);
  announce();
  return ok;
}

/** Add to the end. Queueing something already queued is a no-op, not a move. */
export function add(seasonId, apiName) {
  const list = listFor(seasonId);
  if (list.includes(apiName)) return list;
  const next = [...list, apiName];
  save(seasonId, next);
  return next;
}

export function remove(seasonId, apiName) {
  const next = listFor(seasonId).filter((name) => name !== apiName);
  save(seasonId, next);
  return next;
}

export function toggle(seasonId, apiName) {
  return isQueued(seasonId, apiName) ? remove(seasonId, apiName) : add(seasonId, apiName);
}

/** Move one entry up or down by `delta`. Order is the whole point of a queue. */
export function move(seasonId, apiName, delta) {
  const list = listFor(seasonId);
  const from = list.indexOf(apiName);
  if (from < 0) return list;
  const to = Math.max(0, Math.min(list.length - 1, from + delta));
  if (to === from) return list;

  const next = [...list];
  next.splice(from, 1);
  next.splice(to, 0, apiName);
  save(seasonId, next);
  return next;
}

export function clear(seasonId) {
  save(seasonId, []);
}

/* Drop anything already drafted — by you or by anybody else.
 *
 * Called by the draft tab every time the board comes back, so what the queue
 * offers is always something you can actually take. A queue that keeps
 * offering a Pokémon somebody else took two rounds ago is worse than no queue:
 * it costs you the seconds you were trying to save.
 */
export function prune(seasonId, takenNames) {
  const list = listFor(seasonId);
  const taken = takenNames instanceof Set ? takenNames : new Set(takenNames);
  const next = list.filter((name) => !taken.has(name));
  if (next.length === list.length) return list;
  save(seasonId, next);
  return next;
}
