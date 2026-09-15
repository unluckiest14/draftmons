/* Your leagues: every one this server holds, kept and switchable.
 *
 * A league here is a season — one pool, one set of teams, one draft — so
 * running a second one has always been a matter of creating a second season.
 * What was missing was any way back to the first. There was no list route, so
 * the picker probed ids 1 to 30 on every load, showed nothing but a name, and
 * could not see a league past the thirtieth.
 *
 * This is the library that replaces that: `GET /seasons` once, a card per
 * league with the counts that tell two of them apart, and the three things you
 * do to a saved thing — open it, rename it, throw it away.
 *
 * Which league you were last in is remembered here rather than on the server,
 * because it is a fact about this browser and not about the league. The
 * backend has no accounts to hang it on, and two people sharing a server
 * should not drag each other between leagues.
 */

import { api } from './api.js';
import { h, $, clear } from './dom.js';
import * as session from './session.js';

const LAST_KEY = 'draftmons.league.v1';

let ctx = null;
const ui = {};

/* The last list fetched, so the topbar's count and the modal agree without
 * two requests. Refreshed whenever the library opens or the app reloads. */
let known = [];

export function init(context) {
  ctx = context;
  ui.root = $('#library');
  ui.body = $('#library-body');
  ui.count = $('#library-count');

  $('#library-open').addEventListener('click', open);
  $('#library-close').addEventListener('click', close);
  ui.root.addEventListener('click', (event) => {
    if (event.target === ui.root) close();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !ui.root.hidden) close();
  });
}

export function open() {
  ui.root.hidden = false;
  render();
}

export function close() {
  ui.root.hidden = true;
}

/** The id this browser was last in, or null. Read by app.js on boot. */
export function lastOpened() {
  try {
    const saved = Number(localStorage.getItem(LAST_KEY));
    return Number.isInteger(saved) && saved > 0 ? saved : null;
  } catch {
    return null;
  }
}

export function rememberOpened(seasonId) {
  try {
    localStorage.setItem(LAST_KEY, String(seasonId));
  } catch {
    /* private window; the app just opens on the newest league next time */
  }
}

/** Fetch the list and update the topbar count. Called by app.js on load. */
export async function load() {
  known = await api('/seasons');
  paintCount();
  return known;
}

function paintCount() {
  if (ui.count) ui.count.textContent = known.length ? String(known.length) : '';
}

// ---------------------------------------------------------------- render

async function render() {
  clear(ui.body);
  ui.body.append(h('p', { class: 'library-note', text: 'Loading…' }));

  try {
    await load();
  } catch (problem) {
    clear(ui.body);
    ui.body.append(h('div', { class: 'setup-error', role: 'alert', text: problem.message }));
    return;
  }

  clear(ui.body);
  if (!known.length) {
    ui.body.append(
      h('p', { class: 'library-note', text:
        'No leagues yet. Creating one sets up its pool, its prices and a join code '
        + 'to hand round.' }),
      newButton(),
    );
    return;
  }

  const current = ctx.current()?.id ?? null;
  ui.body.append(
    h('div', { class: 'library-list' }, known.map((league) => card(league, current))),
    newButton(),
  );
}

function newButton() {
  return h('div', { class: 'library-foot' }, [
    h('button', {
      class: 'button is-primary',
      type: 'button',
      text: '+ New league',
      onclick: () => {
        close();
        ctx.openSetup();
      },
    }),
    h('span', { class: 'library-note', text:
      'Leagues live on the server, so everyone you share the address with sees the '
      + 'same list.' }),
  ]);
}

function card(league, currentId) {
  const isCurrent = league.id === currentId;
  const held = session.forSeason(league.id);

  const openIt = () => {
    rememberOpened(league.id);
    ctx.select(league.id);
    close();
  };

  return h('article', {
    class: isCurrent ? 'library-card is-current' : 'library-card',
  }, [
    h('div', { class: 'library-main' }, [
      h('div', { class: 'library-head' }, [
        // The name is the button: clicking a card's title to open it is what
        // everyone tries first, and a row of three buttons is not a library.
        h('button', { class: 'library-name', type: 'button', text: league.name,
                      onclick: openIt }),
        isCurrent ? h('span', { class: 'library-badge is-current', text: 'open' }) : null,
        held.admin ? h('span', { class: 'library-badge', text: 'commissioner' }) : null,
        held.team ? h('span', { class: 'library-badge', text: held.team.name }) : null,
      ]),
      h('div', { class: 'library-meta' }, [
        h('span', { text: `${league.budget} pts · ${league.roster_size} mons` }),
        league.pool_label || league.format_key
          ? h('span', { text: league.pool_label || league.format_key })
          : null,
        league.created_at ? h('span', { text: made(league.created_at) }) : null,
      ]),
      h('div', { class: 'library-stats' }, [
        stat(`${league.teams} ${league.teams === 1 ? 'team' : 'teams'}`),
        stat(poolLine(league), league.pool_size === 0 ? 'is-warn' : ''),
        stat(draftLine(league), `is-${league.draft_status}`),
        league.matches_played
          ? stat(`${league.matches_played} played`)
          : null,
      ]),
    ]),
    h('div', { class: 'library-actions' }, [
      h('button', { class: 'button', type: 'button', text: isCurrent ? 'Open' : 'Switch',
                    onclick: openIt }),
      h('button', { class: 'button is-quiet', type: 'button', text: 'Rename',
                    onclick: (event) => rename(league, event.currentTarget) }),
      h('button', { class: 'button is-quiet is-danger', type: 'button', text: 'Delete',
                    onclick: (event) => remove(league, event.currentTarget) }),
    ]),
  ]);
}

function stat(text, extra = '') {
  return h('span', { class: `library-stat ${extra}`.trim(), text });
}

function poolLine(league) {
  if (!league.pool_size) return 'no pool yet';
  const unpriced = league.pool_size - league.priced;
  return unpriced
    ? `${league.pool_size} mons · ${unpriced} unpriced`
    : `${league.pool_size} mons`;
}

function draftLine(league) {
  if (league.draft_status === 'complete') return 'draft done';
  if (league.draft_status === 'live') {
    return `drafting ${league.picks_made}/${league.picks_total}`;
  }
  return 'not drafted';
}

/* Dates come back as SQLite's "YYYY-MM-DD HH:MM:SS" in UTC, which Safari will
 * not parse and which no reader wants in full. The date alone is enough to
 * tell last spring's league from this one. */
function made(stamp) {
  const [date] = String(stamp).split(' ');
  const parsed = new Date(`${date}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return date;
  return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

// ------------------------------------------------------ rename and delete

async function rename(league, button) {
  const wanted = window.prompt(`Rename "${league.name}" to:`, league.name);
  if (wanted === null) return;
  const name = wanted.trim();
  if (!name || name === league.name) return;

  button.disabled = true;
  try {
    await api(`/seasons/${league.id}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        ...session.headers(league.id, { admin: true }),
      },
      body: JSON.stringify({ name }),
    });
    await ctx.reload();
    render();
  } catch (problem) {
    button.disabled = false;
    window.alert(refusal(problem, 'rename'));
  }
}

async function remove(league, button) {
  if (!window.confirm(
    `Delete "${league.name}"?\n\n`
    + `Its pool of ${league.pool_size}, its ${league.teams} `
    + `${league.teams === 1 ? 'team' : 'teams'}, the draft and every result go with `
    + 'it. This cannot be undone.',
  )) return;

  button.disabled = true;
  try {
    await api(`/seasons/${league.id}`, {
      method: 'DELETE',
      headers: session.headers(league.id, { admin: true }),
    });
  } catch (problem) {
    button.disabled = false;
    window.alert(refusal(problem, 'delete'));
    return;
  }

  // Deleting the league you are looking at is handled by the reload: it lands
  // on whatever is left, or empties the app when that was the last one.
  await ctx.reload();
  render();
}

/* A 403 here means one specific thing — somebody else opened this league and
 * holds its token — and saying so beats repeating the server's line about
 * which token this is not. */
function refusal(problem, verb) {
  if (problem.status === 403) {
    return `Only this league's commissioner can ${verb} it. If that is you on `
      + 'another machine, paste your commissioner token on the League tab first.';
  }
  return problem.message;
}
