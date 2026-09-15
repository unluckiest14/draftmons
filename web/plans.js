/* Private draft plans, stored on the server once a player has joined.
 *
 * Two backing stores, chosen per season by whether this browser holds a player
 * token:
 *
 *   server   joined a season -> GET/PUT /me/plans, scoped to the player by
 *            the token. Plans follow the player to any device and survive a
 *            cleared cache, which is what a league running over weeks needs.
 *   local    not joined -> localStorage, as before. Someone reading the board
 *            without joining has no identity to store a plan against, and a
 *            planner that refuses to open until you join would be worse.
 *
 * Privacy is unchanged by the move. The server scopes every plan query by
 * player id, so no other player and not even the commissioner can read them
 * (see player_service.py, and the tests that assert it). "Private" now means
 * private on the server rather than never leaving the browser — a real change,
 * and the UI says which mode it is in.
 *
 * The interface stayed synchronous on purpose. The editor saves on every
 * keystroke, and awaiting a round trip per character would be unusable, so
 * writes land in an in-memory cache immediately and flush to the server behind
 * a debounce. `load()` is the one async entry point; `saveState()` is how the
 * UI admits when a flush is in flight or has failed.
 *
 * Everything is keyed by season, because a plan is only meaningful against the
 * pool and budget it was built for.
 */

import { api } from './api.js';
import * as session from './session.js';

const KEY = 'draftmons-plans-v1';

/* The two kinds of team sheet. Exported because both tabs name their own. */
export const PLAN = 'plan';
export const LINEUP = 'lineup';

/** Untagged plans predate the split, and every one of them was a plan. */
export const kindOf = (plan) => plan?.kind || PLAN;

/* One shape for the whole local store:
 *   { seasons: { "<id>": { plans: [Plan], activeId: string|null } } }
 *
 * A Plan:
 *   { id, name, kind, author, notes, createdAt, updatedAt, slots: [Slot] }
 *
 * `kind` is which tab owns it, because the two ask different questions of the
 * same shape. A "plan" is the Planner's: who you want, drawn from the pool,
 * against a budget. A "lineup" is the Roster's: which of the Pokemon you
 * actually drafted you are bringing this week, and how they are built. They
 * are stored together and filtered apart, so one sync and one backup covers
 * both. A plan written before the split has no kind and is a "plan", which is
 * what every plan was.
 *
 * A Slot — one Pokemon on the planned team. `api_name` is the join back to
 * the pool entry, which is where cost, types and sprite come from; nothing
 * that the pool already knows is duplicated here.
 *   { api_name, include, item, ability, tera, nature, evs: {hp,atk,...}, moves: [4] }
 *
 * `include` is what a plan means once the draft has happened. The roster is
 * then fixed — you own what you drafted — so a plan stops being "which
 * Pokemon" and becomes "which six of mine, built how". Every drafted Pokemon
 * gets a slot; the ones you are actually bringing are the included ones, and
 * only those reach the paste.
 */

function read() {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === 'object' && parsed.seasons) return parsed;
  } catch {
    /* Corrupt or unreadable storage is treated as empty rather than fatal:
     * losing plans is bad, but a planner that will not open at all is worse,
     * and the player can still re-import from a backup. */
  }
  return { seasons: {} };
}

function write(store) {
  try {
    localStorage.setItem(KEY, JSON.stringify(store));
    return true;
  } catch {
    /* Quota exceeded, or a private window that denies storage. The caller
     * surfaces this: a planner that silently forgets is worse than one that
     * says it cannot save. */
    return false;
  }
}

export function storageWorks() {
  try {
    localStorage.setItem(`${KEY}-probe`, '1');
    localStorage.removeItem(`${KEY}-probe`);
    return true;
  } catch {
    return false;
  }
}

const bucket = (store, seasonId) => {
  const key = String(seasonId);
  if (!store.seasons[key]) store.seasons[key] = { plans: [], activeId: null };
  return store.seasons[key];
};

const newId = () =>
  `p${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

export const emptySlot = (apiName) => ({
  api_name: apiName,
  include: true,
  item: '',
  ability: '',
  tera: '',
  nature: '',
  evs: {},
  moves: ['', '', '', ''],
});

/** Whether a slot goes in the paste.
 *
 * Absent means yes, which is what makes plans written before lineups existed
 * keep working: every Pokemon in them was on the team by definition.
 */
export const isIncluded = (slot) => slot?.include !== false;

// ------------------------------------------------------------- the cache

/* seasonId -> { plans, loaded, mode }. Reads come from here so that render()
 * can stay synchronous; writes go here first and to the server after. */
const cache = new Map();

/** 'server' when this browser holds a player token for the season. */
export const modeFor = (seasonId) =>
  session.isPlayer(seasonId) ? 'server' : 'local';

function entryFor(seasonId) {
  const key = String(seasonId);
  if (!cache.has(key)) {
    cache.set(key, { plans: null, loaded: false, mode: modeFor(seasonId) });
  }
  return cache.get(key);
}

/* Subscribers are told when a background flush changes something the UI shows:
 * a temporary id becoming a real one, or a save failing. */
const listeners = new Set();
export function onChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
const announce = () => listeners.forEach((fn) => fn());

// --------------------------------------------------- server <-> local shape

/* The server stores a plan as a name plus an opaque JSON body. Everything
 * except the name lives in the body, so adding a field to a plan needs no
 * migration — see the player_plan comment in database_schema.sql. */
const toLocal = (record) => ({
  id: String(record.id),
  name: record.name,
  kind: record.body?.kind || PLAN,
  author: record.body?.author || '',
  notes: record.body?.notes || '',
  slots: record.body?.slots || [],
  createdAt: record.created_at || record.body?.createdAt || null,
  updatedAt: record.updated_at || null,
});

const toBody = (plan) => ({
  kind: plan.kind || PLAN,
  author: plan.author || '',
  notes: plan.notes || '',
  slots: plan.slots || [],
  createdAt: plan.createdAt || null,
});

// ------------------------------------------------------------------ load

/** Prime the cache for a season. The one async entry point.
 *
 * Returns true when the cached plans changed, so a caller that already painted
 * from localStorage knows whether to repaint.
 */
export async function load(seasonId) {
  const entry = entryFor(seasonId);
  const mode = modeFor(seasonId);
  const before = JSON.stringify(entry.plans);
  entry.mode = mode;

  if (mode === 'local') {
    entry.plans = bucket(read(), seasonId).plans;
    entry.loaded = true;
    return JSON.stringify(entry.plans) !== before;
  }

  try {
    /* The list endpoint omits bodies deliberately — it is a list, not a sync —
     * so each plan is fetched for its slots. A player has a handful of plans,
     * not hundreds, and the alternative is a list route that ships every
     * body on every tab switch. */
    const summaries = await api('/me/plans', { headers: session.headers(seasonId) });
    const full = await Promise.all(
      summaries.map((row) =>
        api(`/me/plans/${row.id}`, { headers: session.headers(seasonId) }).catch(() => null),
      ),
    );
    entry.plans = full.filter(Boolean).map(toLocal);
    entry.loaded = true;
    entry.error = null;
  } catch (problem) {
    /* Offline or a dead token. Keep whatever is cached and say so rather than
     * showing an empty planner, which reads as "your plans are gone". */
    entry.error = problem.message;
    entry.loaded = entry.plans !== null;
    if (entry.plans === null) entry.plans = [];
  }
  return JSON.stringify(entry.plans) !== before;
}

/** Whether this season's plans have been fetched into the cache yet.
 *
 * The tabs check it so a tab switch does not refetch every plan body; the
 * cache is primed once per season and kept up to date by the writes. */
export const isLoaded = (seasonId) => entryFor(seasonId).loaded;

/** Any message from the last load, for the UI to show. */
export const loadError = (seasonId) => entryFor(seasonId).error || null;

// ------------------------------------------------------------- reading

export function plansFor(seasonId, kind = null) {
  const entry = entryFor(seasonId);
  /* Before load() lands, fall back to whatever localStorage holds. In server
   * mode that is usually empty, but it means the first paint of a tab switch
   * is never blank for a local-mode player. */
  const all = entry.plans === null ? bucket(read(), seasonId).plans : entry.plans;
  return kind ? all.filter((plan) => kindOf(plan) === kind) : all;
}

/* The active plan is a per-browser UI preference, not data, so it stays in
 * localStorage in both modes — which device you last looked at a plan on is
 * nobody else's business and not worth a round trip. */
export function activeIdFor(seasonId, kind = null) {
  const plans = plansFor(seasonId, kind);
  const slot = bucket(read(), seasonId);
  /* Per kind, so opening a lineup does not close the plan you had open in the
   * other tab. `activeId` is where the Planner's used to live on its own, and
   * is still read for it so nobody's open plan changes under the upgrade. */
  const remembered = kind
    ? slot.activeIds?.[kind] ?? (kind === PLAN ? slot.activeId : null)
    : slot.activeId;
  if (remembered && plans.some((plan) => plan.id === remembered)) return remembered;
  return plans[0]?.id || null;
}

export function setActive(seasonId, planId, kind = PLAN) {
  const store = read();
  const slot = bucket(store, seasonId);
  slot.activeIds = { ...(slot.activeIds || {}), [kind]: planId };
  if (kind === PLAN) slot.activeId = planId;
  return write(store);
}

// ------------------------------------------------------------- flushing

/* planId -> timer. One pending flush per plan, coalesced: the editor fires a
 * save on every keystroke and each should not become a request. */
const timers = new Map();
const inFlight = new Set();
const failures = new Map();

const FLUSH_DELAY = 700;

/** What the UI should say about saving. */
export function saveState(seasonId) {
  if (modeFor(seasonId) === 'local') return { mode: 'local' };
  if (failures.size) return { mode: 'server', status: 'error', message: [...failures.values()][0] };
  if (inFlight.size || timers.size) return { mode: 'server', status: 'saving' };
  return { mode: 'server', status: 'saved' };
}

function scheduleFlush(seasonId, planId) {
  clearTimeout(timers.get(planId));
  timers.set(planId, setTimeout(() => {
    timers.delete(planId);
    flush(seasonId, planId);
  }, FLUSH_DELAY));
  announce();
}

async function flush(seasonId, planId) {
  const plan = plansFor(seasonId).find((item) => item.id === planId);
  // A plan deleted while its flush was pending has nothing to write.
  if (!plan || String(planId).startsWith('tmp_')) return;

  inFlight.add(planId);
  try {
    await api(`/me/plans/${planId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...session.headers(seasonId) },
      body: JSON.stringify({ name: plan.name, body: toBody(plan) }),
    });
    failures.delete(planId);
  } catch (problem) {
    failures.set(planId, problem.message);
  } finally {
    inFlight.delete(planId);
    announce();
  }
}

/** Write every pending change now. For leaving the tab. */
export async function flushAll(seasonId) {
  const pending = [...timers.keys()];
  for (const planId of pending) {
    clearTimeout(timers.get(planId));
    timers.delete(planId);
  }
  await Promise.all(pending.map((planId) => flush(seasonId, planId)));
}

// ------------------------------------------------------------- writing

/* Every mutation updates the cache synchronously and then persists, so the UI
 * redraws from the change immediately. In local mode "persists" is a
 * localStorage write and cannot really fail; in server mode it is a request
 * that can, which is what saveState() exists to report. */

function persistLocal(seasonId, plans, activeId) {
  const store = read();
  const slot = bucket(store, seasonId);
  slot.plans = plans;
  if (activeId !== undefined) slot.activeId = activeId;
  return write(store);
}

export function createPlan(seasonId, name, kind = PLAN) {
  const entry = entryFor(seasonId);
  const plans = plansFor(seasonId);
  const mine = plansFor(seasonId, kind);
  const now = new Date().toISOString();
  const plan = {
    id: modeFor(seasonId) === 'server' ? `tmp_${newId()}` : newId(),
    name: name || `${kind === LINEUP ? 'Week' : 'Plan'} ${mine.length + 1}`,
    kind,
    author: '',
    notes: '',
    createdAt: now,
    updatedAt: now,
    slots: [],
  };

  entry.plans = [...plans, plan];
  setActive(seasonId, plan.id, kind);

  if (modeFor(seasonId) === 'local') {
    persistLocal(seasonId, entry.plans, plan.id);
    return plan;
  }

  /* Created optimistically with a temporary id so the caller gets a plan back
   * synchronously, then reconciled with the id the server assigns. Until that
   * lands, flush() skips the plan rather than PUTting to /me/plans/tmp_… */
  api('/me/plans', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...session.headers(seasonId) },
    body: JSON.stringify({ name: plan.name, body: toBody(plan) }),
  })
    .then((saved) => {
      const wasActive = activeIdFor(seasonId) === plan.id;
      plan.id = String(saved.id);
      if (wasActive) setActive(seasonId, plan.id);
      failures.delete(plan.id);
      announce();
    })
    .catch((problem) => {
      failures.set(plan.id, problem.message);
      announce();
    });

  return plan;
}

/** Apply `changes` to one plan. Returns the saved plan, or null if it is gone. */
export function updatePlan(seasonId, planId, changes) {
  const entry = entryFor(seasonId);
  const plans = plansFor(seasonId);
  const plan = plans.find((item) => item.id === planId);
  if (!plan) return null;

  Object.assign(plan, changes, { updatedAt: new Date().toISOString() });
  entry.plans = plans;

  if (modeFor(seasonId) === 'local') {
    return persistLocal(seasonId, plans) ? plan : null;
  }
  scheduleFlush(seasonId, planId);
  return plan;
}

export function deletePlan(seasonId, planId) {
  const entry = entryFor(seasonId);
  const gone = plansFor(seasonId).find((plan) => plan.id === planId);
  const kind = kindOf(gone);
  const remaining = plansFor(seasonId).filter((plan) => plan.id !== planId);
  entry.plans = remaining;

  clearTimeout(timers.get(planId));
  timers.delete(planId);
  failures.delete(planId);

  /* Falls back within the same kind: deleting a lineup must not leave the
   * Planner pointing at it, or vice versa. */
  const nextActive = remaining.find((plan) => kindOf(plan) === kind)?.id || null;
  if (activeIdFor(seasonId, kind) === planId) setActive(seasonId, nextActive, kind);

  if (modeFor(seasonId) === 'local') return persistLocal(seasonId, remaining, nextActive);

  api(`/me/plans/${planId}`, { method: 'DELETE', headers: session.headers(seasonId) })
    .catch((problem) => {
      failures.set(planId, problem.message);
      announce();
    });
  return true;
}

export function duplicatePlan(seasonId, planId) {
  const source = plansFor(seasonId).find((plan) => plan.id === planId);
  if (!source) return null;

  /* structuredClone so the copy shares no nested slot or evs object with the
   * original — editing the duplicate must not edit what it came from. */
  const copy = createPlan(seasonId, `${source.name} copy`, kindOf(source));
  return updatePlan(seasonId, copy.id, {
    author: source.author,
    notes: source.notes,
    slots: structuredClone(source.slots || []),
  });
}

// ------------------------------------------------- moving local -> server

/** Local plans for a season that the server does not have. For the prompt. */
export const localPlansFor = (seasonId) => bucket(read(), seasonId).plans;

/** Upload this browser's local plans for a season to the player's account.
 *
 * Copies rather than moves: the local plans are left alone, so a failed or
 * half-finished upload cannot lose work. The player clears them afterwards if
 * they want to, which is also what makes the prompt safe to accept.
 */
export async function uploadLocal(seasonId) {
  if (modeFor(seasonId) !== 'server') {
    throw new Error('Join the league before uploading plans to it.');
  }
  const local = localPlansFor(seasonId);
  let uploaded = 0;
  for (const plan of local) {
    await api('/me/plans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...session.headers(seasonId) },
      body: JSON.stringify({ name: plan.name, body: toBody(plan) }),
    });
    uploaded += 1;
  }
  await load(seasonId);
  return uploaded;
}

/** Forget this browser's local copies for one season. After a successful upload. */
export function clearLocal(seasonId) {
  const store = read();
  bucket(store, seasonId).plans = [];
  return write(store);
}

// -------------------------------------------------------------- backup

/* Still worth having in server mode: a file on disk is the one copy that
 * survives losing the token, which cannot be reissued. */
export function exportAll() {
  const store = read();
  for (const [key, entry] of cache) {
    if (entry.plans) bucket(store, key).plans = entry.plans;
  }
  return JSON.stringify(store, null, 2);
}

export async function importAll(json, { merge = true, seasonId = null } = {}) {
  const incoming = JSON.parse(json);
  if (!incoming || typeof incoming !== 'object' || !incoming.seasons) {
    throw new Error('That file is not a Draftmons plan backup.');
  }

  /* In server mode a restore has to go through the API, or it would land in
   * localStorage where the planner is no longer reading from. */
  if (seasonId !== null && modeFor(seasonId) === 'server') {
    let added = 0;
    for (const imported of Object.values(incoming.seasons)) {
      for (const plan of imported.plans || []) {
        await api('/me/plans', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...session.headers(seasonId) },
          body: JSON.stringify({ name: plan.name, body: toBody(plan) }),
        });
        added += 1;
      }
    }
    await load(seasonId);
    return added;
  }

  if (!merge) return write(incoming);

  const store = read();
  let added = 0;
  for (const [seasonKey, imported] of Object.entries(incoming.seasons)) {
    const slot = bucket(store, seasonKey);
    for (const plan of imported.plans || []) {
      // Re-id on the way in, so importing a backup onto the browser it came
      // from adds copies instead of silently overwriting the live plans.
      slot.plans.push({ ...plan, id: newId() });
      added += 1;
    }
  }
  if (!write(store)) return 0;
  cache.delete(String(seasonId));
  return added;
}
