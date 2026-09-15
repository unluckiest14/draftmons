/* The draft board.
 *
 * Data flow, which is the thing to hold in your head:
 *
 *   GET /formats            -> what the league can draft from
 *   pick a format           -> GET /seasons/{id}/pool for that season
 *   click a Pokemon         -> PokeAPI, for the two things the pool does not
 *                              store: abilities and the movepool
 *
 * The pool arrives in one request and every filter and sort after that is
 * client-side. The backend supports filtering too, but a pool is at most a
 * couple of thousand rows and refetching on every keystroke would make the
 * search feel worse than doing it locally.
 *
 * The format matters past the pool contents: it decides which generation's
 * learnset the movepool is read from. A Gen 5 draft and a Gen 9 draft can hold
 * the same Pokemon at the same cost and still be different picks.
 */

import { api } from './api.js';
import { h, $, clear, typePip, placeholder, spriteFor } from './dom.js';
import * as poke from './pokeapi.js';
import * as planner from './planner.js';
import * as roster from './roster.js';
import * as sheet from './teamsheet.js';
import * as draft from './draft.js';
import * as setup from './setup.js';
import * as leagues from './leagues.js';
import * as league from './league.js';
import * as session from './session.js';
import * as standings from './standings.js';
import { loadRules } from './pokepaste.js';

/* Bump this whenever the frontend changes.
 *
 * Twice now a bug report has turned out to be a browser running a mix of old
 * and new modules, and there was no way to tell from the outside. It is logged
 * on boot and sits in the tooltip on the logo, so "which build are you on" is
 * answerable in two seconds instead of by guesswork. */
const BUILD = '2026-09-15-e';
console.info(`Draftmons build ${BUILD}`);
document.querySelector('.brand')?.setAttribute('title', `Draftmons build ${BUILD}`);

const params = new URLSearchParams(location.search);

/* `?season=2&format=gen9-ou` opens straight onto one board. Worth having
 * because a commissioner posts this link to a league chat, and because the
 * URL is then the thing that identifies what you are looking at. */
const WANTED = {
  season: params.get('season'),
  format: params.get('format'),
  mon: params.get('mon'),
  view: params.get('view'),
  tab: params.get('tab'),
};

/* Every tab name, in one place. The initial `?tab=` and showTab() both read
 * this, because when they had their own lists they drifted and a deep link to
 * a real tab quietly opened the board instead. */
const TABS = ['board', 'draft', 'planner', 'roster', 'stats', 'league'];

const ui = {
  seasonSelect: $('#season-select'),
  formatSelect: $('#format-select'),
  budget: $('#budget'),
  notice: $('#notice'),
  controls: $('#controls'),
  search: $('#search'),
  typeFilter: $('#type-filter'),
  sort: $('#sort'),
  costCap: $('#cost-cap'),
  costCapLabel: $('#cost-cap-label'),
  showBanned: $('#show-banned'),
  resultCount: $('#result-count'),
  board: $('#board'),
  turnBanner: $('#turn-banner'),
  turnAlert: $('#turn-alert'),
  detail: $('#detail'),
  scrim: $('#scrim'),
  themeToggle: $('#theme-toggle'),
  planner: $('#planner'),
  roster: $('#roster'),
  draft: $('#draft'),
  stats: $('#stats'),
  league: $('#league'),
  leagueTeams: $('#league-teams'),
};

const state = {
  seasons: [],
  formats: [],
  season: null,
  formatKey: '',
  pool: [],
  view: WANTED.view === 'table' ? 'table' : 'grid',
  /* Aborts the open panel's movepool enrichment. Clicking through the board
   * quickly would otherwise leave one background run per Pokemon visited, all
   * writing into a panel that is no longer on screen. */
  detailRun: null,
  openMon: null,
  tab: TABS.includes(WANTED.tab) ? WANTED.tab : 'board',
  /* The format's clauses and non-species bans, from GET /formats/{key}/rules.
   * Only the planner uses them, to flag a banned item or move in a set. */
  rules: null,
};

/* The one-line banner above the board: an empty pool, a format mismatch, a
 * build result. Board-only, so it stayed here when the generic helpers moved
 * out to dom.js. */
function showNotice(node, isError = false) {
  ui.notice.textContent = '';
  ui.notice.classList.toggle('is-error', isError);
  if (!node) {
    ui.notice.hidden = true;
    return;
  }
  ui.notice.append(node);
  ui.notice.hidden = false;
}

// -------------------------------------------------------------- seasons

/* Every league on the server, in one request. `GET /seasons` returns each with
 * its team, pool and draft counts, which is what lets the picker say more than
 * a name and the library say enough to tell two leagues apart.
 *
 * `keep` is for a reload that must not move you: renaming or deleting from the
 * library refreshes this list, and being thrown back to the newest league
 * every time would make the library unusable. */
async function loadSeasons({ keep = false } = {}) {
  try {
    state.seasons = await leagues.load();
  } catch (problem) {
    state.seasons = [];
    showNotice(h('span', { class: 'notice-text', text: problem.message }), true);
  }

  ui.seasonSelect.textContent = '';
  if (!state.seasons.length) {
    /* Reachable by deleting the last league, so the app has to empty itself
       rather than keep painting the one that is gone. */
    ui.seasonSelect.append(h('option', { value: '', text: 'No leagues yet' }));
    state.season = null;
    state.pool = [];
    ui.budget.hidden = true;
    await loadPool();
    league.refresh();
    standings.refresh();
    return;
  }
  for (const season of state.seasons) {
    ui.seasonSelect.append(
      h('option', { value: String(season.id), text: `${season.name} · ${season.budget} pts` }),
    );
  }

  const byId = (id) => state.seasons.find((season) => season.id === Number(id));
  if (keep && state.season) {
    const still = byId(state.season.id);
    if (still) {
      // Re-point at the fresh row so a rename shows without a full reselect.
      state.season = still;
      ui.seasonSelect.value = String(still.id);
      return;
    }
    // The league you were in has been deleted, so fall through and land on
    // whatever is left rather than holding a pointer to nothing.
  }
  /* A link beats what this browser was last in, which beats the newest league:
   * a shared `?season=` URL has to open the league it names. */
  selectSeason(byId(WANTED.season) || byId(leagues.lastOpened()) || state.seasons[0]);
}

function selectSeason(season) {
  if (!ui.detail.hidden) closeDetail();
  state.season = season;
  ui.seasonSelect.value = String(season.id);
  leagues.rememberOpened(season.id);
  ui.budget.hidden = false;
  ui.budget.querySelector('.budget-value').textContent =
    `${season.budget} pts · ${season.roster_size} mons`;

  /* A season remembers the format its pool was built from. Preselecting it
   * means the movepool generation is right on first load without anyone
   * touching the format picker. */
  loadLeagueTeams(season.id);
  draft.refresh();
  league.refresh();
  standings.refresh();
  roster.refresh();

  const known = (key) => Boolean(key) && state.formats.some((format) => format.key === key);
  const chosen = (known(WANTED.format) && WANTED.format)
    || (known(season.format_key) && season.format_key)
    || '';
  ui.formatSelect.value = chosen;
  selectFormat(chosen);
}

/* The league's own teams, offered as suggestions for a plan's author field.
 * Not required for anything — a player can type whatever they like — so a
 * failure here is silent. */
async function loadLeagueTeams(seasonId) {
  ui.leagueTeams.textContent = '';
  try {
    for (const team of await api(`/seasons/${seasonId}/teams`)) {
      ui.leagueTeams.append(h('option', { value: team.name }));
    }
  } catch {
    /* no teams endpoint data; the field stays free text */
  }
}

// -------------------------------------------------------------- formats

async function loadFormats() {
  ui.formatSelect.textContent = '';
  try {
    state.formats = await api('/formats');
  } catch (error) {
    state.formats = [];
    /* 503 is the documented "the cron job has not run" case, and it is the
     * state a fresh checkout is in, so it gets a real instruction rather than
     * a stack trace. The board still works: a season with a pool can be
     * browsed, and the generation picker below covers the movepool. */
    ui.formatSelect.append(h('option', { value: '', text: 'No formats loaded' }));
    showNotice(
      h('div', { class: 'notice-text' }, [
        h('strong', { text: 'No formats in the database yet. ' }),
        document.createTextNode('Run '),
        h('code', { text: 'python update_formats.py' }),
        document.createTextNode(
          error.status === 503
            ? ' to pull the current tier list from Showdown, then reload.'
            : ` — the formats request failed: ${error.message}`,
        ),
      ]),
      error.status !== 503,
    );
    return;
  }

  ui.formatSelect.append(h('option', { value: '', text: 'Choose a format…' }));
  for (const format of state.formats) {
    ui.formatSelect.append(
      h('option', {
        value: format.key,
        text: `${format.label} · ${format.species_count} mons`,
      }),
    );
  }
}

function selectFormat(key) {
  state.formatKey = key;
  syncUrl(state.openMon);
  loadPool();

  /* Rules are only used by the planner, so this is not awaited: the board must
   * not wait on it, and the planner re-renders when it lands. */
  state.rules = null;
  loadRules(key).then((rules) => {
    if (state.formatKey !== key) return;   // a newer pick won the race
    state.rules = rules;
    planner.refresh();
  });
}

/* replaceState, not pushState: switching format is not a navigation, and
 * filling the back button with board states would make Back useless. */
function syncUrl(mon) {
  const next = new URLSearchParams(location.search);
  if (state.season) next.set('season', String(state.season.id));
  if (state.formatKey) next.set('format', state.formatKey);
  else next.delete('format');
  if (mon) next.set('mon', mon);
  else next.delete('mon');
  if (state.view === 'table') next.set('view', 'table');
  else next.delete('view');
  if (state.tab !== 'board') next.set('tab', state.tab);
  else next.delete('tab');
  history.replaceState(null, '', `${location.pathname}?${next}`);
}

// ----------------------------------------------------------------- pool

async function loadPool() {
  if (!state.season) {
    ui.controls.hidden = true;
    ui.board.textContent = '';
    ui.board.append(placeholder('◇', 'No season selected', 'Create a season, then build its pool from a format.'));
    return;
  }

  ui.board.textContent = '';
  ui.board.append(
    h('div', { class: 'skeleton-grid' }, Array.from({ length: 24 }, () => h('div', { class: 'skeleton' }))),
  );

  try {
    state.pool = await api(
      `/seasons/${state.season.id}/pool?include_banned=true&limit=2000`,
    );
  } catch (error) {
    state.pool = [];
    ui.controls.hidden = true;
    ui.board.textContent = '';
    ui.board.append(placeholder('!', 'Could not load the pool', error.message));
    return;
  }

  buildTypeFilter();
  configureCostCap();
  // Board-only, and the same rule showTab applies: loading a pool while the
  // draft or the stats tab is open must not put the board's filters above it.
  ui.controls.hidden = state.tab !== 'board' || state.pool.length === 0;
  noticeForPoolState();
  render();
  openWantedMon();
  planner.refresh();
  roster.refresh();
}

/* A ?mon= link can only be honoured after the pool loads, since the panel is
 * built from a pool entry. Consumed once: a later format change must not
 * reopen a panel the reader already closed. */
function openWantedMon() {
  if (!WANTED.mon) return;
  const entry = state.pool.find(
    (item) => item.api_name === WANTED.mon || item.showdown_id === WANTED.mon,
  );
  WANTED.mon = null;
  if (entry) openDetail(entry);
}

/* The pool is per-season and built by an explicit setup action, so a format
 * can be selected while the season's pool is empty or was built from a
 * different format. Both are worth saying out loud — silently showing the
 * wrong 800 Pokemon is the failure mode to avoid. */
function noticeForPoolState() {
  const format = state.formats.find((item) => item.key === state.formatKey);

  if (state.pool.length === 0) {
    showNotice(
      h('div', { class: 'notice-text' }, [
        h('strong', { text: `${state.season.name} has an empty pool. ` }),
        document.createTextNode(
          format
            ? `Build it from ${format.label} — one PokeAPI call per Pokémon, so it takes a minute.`
            : 'Choose a format to build it from, or paste a cost list via the API.',
        ),
      ]),
    );
    if (format) ui.notice.append(buildPoolButton(format));
    return;
  }

  if (state.formatKey && state.season.format_key && state.formatKey !== state.season.format_key) {
    showNotice(
      h('div', { class: 'notice-text' }, [
        h('strong', { text: 'Format mismatch. ' }),
        document.createTextNode(
          `This pool was built from ${state.season.format_key}, but ${state.formatKey} is selected. ` +
            'Costs and legality below are still the built pool; only the movepool generation follows your pick.',
        ),
      ]),
      true,
    );
    return;
  }

  showNotice(null);
}

/* The pool build is a commissioner action the backend explicitly warns not to
 * expose to drafters. It stays behind a button that names its cost, and the
 * button disables itself while it runs — a second click would start a second
 * few-hundred-request build. */
function buildPoolButton(format) {
  const button = h('button', {
    class: 'button',
    type: 'button',
    text: `Build pool from ${format.label}`,
  });

  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Building…';
    try {
      const result = await api(
        `/seasons/${state.season.id}/pool/from-format/${format.key}`,
        { method: 'POST', headers: session.headers(state.season.id, { admin: true }) },
      );
      const refreshed = await api(`/seasons/${state.season.id}`);
      const index = state.seasons.findIndex((item) => item.id === refreshed.id);
      if (index !== -1) state.seasons[index] = refreshed;
      state.season = refreshed;
      await loadPool();
      if (result.unresolved?.length) {
        ui.notice.hidden = false;
        ui.notice.append(
          h('div', { class: 'notice-text' }, [
            h('strong', { text: `${result.added} added. ` }),
            document.createTextNode(
              `${result.unresolved.length} Showdown ids had no PokeAPI match: ${result.unresolved.slice(0, 8).join(', ')}`,
            ),
          ]),
        );
      }
    } catch (error) {
      button.disabled = false;
      button.textContent = `Build pool from ${format.label}`;
      showNotice(h('div', { class: 'notice-text', text: `Build failed: ${error.message}` }), true);
    }
  });

  return button;
}

function buildTypeFilter() {
  const present = new Set();
  for (const entry of state.pool) for (const type of entry.types || []) present.add(type);

  const current = ui.typeFilter.value;
  ui.typeFilter.textContent = '';
  ui.typeFilter.append(h('option', { value: '', text: 'All types' }));
  for (const type of [...present].sort()) {
    ui.typeFilter.append(h('option', { value: type, text: poke.prettify(type) }));
  }
  if (present.has(current)) ui.typeFilter.value = current;
}

function configureCostCap() {
  const max = Math.max(1, ...state.pool.map((entry) => entry.cost || 0));
  ui.costCap.max = String(max);
  ui.costCap.value = String(max);
  updateCostCapLabel();
}

function updateCostCapLabel() {
  const value = Number(ui.costCap.value);
  ui.costCapLabel.textContent =
    value >= Number(ui.costCap.max) ? 'Max cost: any' : `Max cost: ${value}`;
}

// ---------------------------------------------------------- the board

function visibleEntries() {
  const query = ui.search.value.trim().toLowerCase();
  const type = ui.typeFilter.value;
  const cap = Number(ui.costCap.value);
  const uncapped = cap >= Number(ui.costCap.max);
  const showBanned = ui.showBanned.checked;

  const rows = state.pool.filter((entry) => {
    if (!showBanned && entry.banned) return false;
    if (type && !(entry.types || []).includes(type)) return false;
    if (!uncapped && (entry.cost || 0) > cap) return false;
    if (query && !entry.display_name.toLowerCase().includes(query)) return false;
    return true;
  });

  const sorters = {
    'cost-desc': (a, b) => (b.cost || 0) - (a.cost || 0) || (b.bst || 0) - (a.bst || 0),
    'cost-asc': (a, b) => (a.cost || 0) - (b.cost || 0) || (b.bst || 0) - (a.bst || 0),
    'name-asc': (a, b) => a.display_name.localeCompare(b.display_name),
    'bst-desc': (a, b) => (b.bst || 0) - (a.bst || 0),
  };
  return rows.sort(sorters[ui.sort.value] || sorters['cost-desc']);
}

function render() {
  const rows = visibleEntries();

  const banned = state.pool.filter((entry) => entry.banned).length;
  const unpriced = state.pool.filter((entry) => !entry.cost).length;
  const parts = [`${rows.length} of ${state.pool.length} Pokémon`];
  if (unpriced) parts.push(`${unpriced} unpriced`);
  if (banned) parts.push(`${banned} banned`);
  ui.resultCount.textContent = parts.join(' · ');

  ui.board.textContent = '';
  if (!state.pool.length) {
    ui.board.append(
      placeholder('◇', 'Nothing in this pool yet', 'Build it from a format, or paste the league cost list.'),
    );
    return;
  }
  if (!rows.length) {
    ui.board.append(placeholder('∅', 'No matches', 'Loosen the filters to see more of the pool.'));
    return;
  }
  ui.board.append(state.view === 'grid' ? gridOf(rows) : tableOf(rows));
}

function costPill(entry) {
  return h('span', {
    class: entry.cost ? 'card-cost' : 'card-cost is-unpriced',
    text: entry.cost ? String(entry.cost) : '—',
    title: entry.cost ? `${entry.cost} points` : 'No cost set',
  });
}

function gridOf(rows) {
  return h(
    'div',
    { class: 'grid' },
    rows.map((entry) =>
      h(
        'button',
        {
          class: entry.banned ? 'card is-banned' : 'card',
          type: 'button',
          title: entry.banned ? `${entry.display_name} — banned` : entry.display_name,
          onclick: () => openDetail(entry),
        },
        [
          costPill(entry),
          spriteFor(entry, 'card-sprite'),
          h('span', { class: 'card-name', text: entry.display_name }),
          h('span', { class: 'card-types' }, (entry.types || []).map((type) => typePip(type))),
        ],
      ),
    ),
  );
}

function tableOf(rows) {
  const head = h('thead', {}, [
    h('tr', {}, [
      h('th', { text: '' }),
      h('th', { text: 'Pokémon' }),
      h('th', { text: 'Types' }),
      h('th', { class: 'cell-num', text: 'Cost' }),
      h('th', { class: 'cell-num', text: 'BST' }),
      h('th', { text: 'Tier' }),
    ]),
  ]);

  const body = h(
    'tbody',
    {},
    rows.map((entry) =>
      h(
        'tr',
        {
          class: entry.banned ? 'is-banned' : null,
          tabindex: '0',
          onclick: () => openDetail(entry),
          onkeydown: (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              openDetail(entry);
            }
          },
        },
        [
          h('td', {}, [spriteFor(entry, 'row-sprite')]),
          h('td', { class: 'cell-name', text: entry.display_name }),
          h('td', { class: 'cell-types' }, (entry.types || []).map((type) => typePip(type))),
          h('td', { class: 'cell-num cell-cost', text: entry.cost ? String(entry.cost) : '—' }),
          h('td', { class: 'cell-num', text: entry.bst ? String(entry.bst) : '—' }),
          h('td', { text: entry.banned ? 'banned' : entry.tier || '—' }),
        ],
      ),
    ),
  );

  return h('div', { class: 'table-wrap' }, [h('table', { class: 'pool' }, [head, body])]);
}

// -------------------------------------------------------- detail panel

const STAT_LABELS = {
  hp: 'HP',
  attack: 'Attack',
  defense: 'Defense',
  'special-attack': 'Sp. Atk',
  'special-defense': 'Sp. Def',
  speed: 'Speed',
};

function openDetail(entry) {
  state.openMon = entry.api_name;
  syncUrl(entry.api_name);
  state.detailRun?.abort();
  const run = new AbortController();
  state.detailRun = run;

  const generation = poke.generationOf(state.formatKey || state.season?.format_key);
  const formatLabel =
    state.formats.find((item) => item.key === state.formatKey)?.label ||
    state.formatKey ||
    `Gen ${generation}`;

  ui.detail.textContent = '';
  ui.detail.append(detailHead(entry), detailBody(entry, generation, formatLabel, run.signal));
  ui.detail.hidden = false;
  ui.scrim.hidden = false;
  document.body.style.overflow = 'hidden';
  ui.detail.scrollTop = 0;
  ui.detail.querySelector('.detail-close')?.focus();
}

function closeDetail() {
  state.openMon = null;
  syncUrl(null);
  state.detailRun?.abort();
  state.detailRun = null;
  ui.detail.hidden = true;
  ui.scrim.hidden = true;
  ui.detail.textContent = '';
  document.body.style.overflow = '';
}

function detailHead(entry) {
  const tags = [
    h('span', {
      class: entry.cost ? 'pill is-cost' : 'pill',
      text: entry.cost ? `${entry.cost} pts` : 'unpriced',
    }),
    ...(entry.types || []).map((type) => typePip(type)),
    entry.tier ? h('span', { class: 'pill', text: entry.tier }) : null,
    entry.banned ? h('span', { class: 'pill is-banned', text: 'banned' }) : null,
  ];

  const art = entry.artwork_url || entry.sprite_url;

  return h('div', { class: 'detail-head' }, [
    art
      ? h('img', { class: 'detail-art', src: art, alt: entry.display_name, decoding: 'async' })
      : h('div', { class: 'detail-art' }),
    h('div', { class: 'detail-ident' }, [
      h('h2', { class: 'detail-name', text: entry.display_name }),
      h('div', { class: 'detail-sub', text: entry.api_name }),
      h('div', { class: 'detail-tags' }, tags),
      h('div', { class: 'detail-draft-slot' }, [draftAction(entry)]),
    ]),
    h('button', {
      class: 'detail-close',
      type: 'button',
      'aria-label': 'Close',
      text: '✕',
      onclick: closeDetail,
    }),
  ]);
}

/* The button that was missing. Opening a Pokémon here during your turn and
 * finding no way to take it is what sent everyone hunting for the other tab;
 * the pick itself still goes through the draft's own confirmation panel, so
 * there is one place that spends a pick and one set of rules about it. */
function draftAction(entry) {
  const turn = draft.turnState();
  if (!turn.live || !turn.myTurn) return null;

  if (turn.taken.has(entry.api_name)) {
    return h('div', { class: 'detail-draft' }, [
      h('span', { class: 'detail-draft-note', text: 'Already drafted.' }),
    ]);
  }
  if (entry.banned) {
    return h('div', { class: 'detail-draft' }, [
      h('span', { class: 'detail-draft-note', text: 'Banned in this league.' }),
    ]);
  }

  const afford = (entry.cost || 0) <= turn.remaining;
  return h('div', { class: 'detail-draft' }, [
    h('button', {
      class: 'button is-primary',
      type: 'button',
      text: `Draft ${entry.display_name}`,
      onclick: () => {
        closeDetail();
        draft.pickFrom(entry);
      },
    }),
    h('span', {
      class: afford ? 'detail-draft-note' : 'detail-draft-note is-over',
      text: afford
        ? `${turn.remaining} points left, ${turn.remaining - (entry.cost || 0)} after this`
        : `Costs ${entry.cost}; you have ${turn.remaining}`,
    }),
  ]);
}

function section(title, note, children) {
  return h('section', { class: 'section' }, [
    h('h3', { class: 'section-title' }, [
      document.createTextNode(title),
      note ? h('span', { class: 'section-note', text: note }) : null,
    ]),
    ...[].concat(children),
  ]);
}

function detailBody(entry, generation, formatLabel, signal) {
  const statsBox = h('div', {}, [statsView(entry.stats)]);
  const abilitiesBox = h('div', {}, [loadingLine('Loading abilities…')]);
  const movesBox = h('div', {}, [loadingLine(`Loading Gen ${generation} movepool…`)]);

  const body = h('div', { class: 'detail-body' }, [
    section('Base stats', entry.bst ? `BST ${entry.bst}` : null, statsBox),
    section('Type matchups', 'damage taken', matchupsView(entry.types || [])),
    section('Abilities', null, abilitiesBox),
    section('Movepool', `${formatLabel} · Gen ${generation}`, movesBox),
  ]);

  hydrateDetail(entry, generation, { statsBox, abilitiesBox, movesBox }, signal);
  return body;
}

function loadingLine(text) {
  return h('p', { class: 'ability-text' }, [h('span', { class: 'spinner' }), document.createTextNode(` ${text}`)]);
}

/* One PokeAPI request per Pokemon covers both remaining sections, so they are
 * filled from a single fetch. Stats are refilled too: pool rows built before
 * the `stats` column existed carry '{}', and the fetch already has them. */
async function hydrateDetail(entry, generation, boxes, signal) {
  let mon;
  try {
    mon = await poke.pokemon(entry.api_name);
  } catch (error) {
    if (signal.aborted) return;
    const message = `Could not reach PokeAPI: ${error.message}`;
    boxes.abilitiesBox.textContent = '';
    boxes.abilitiesBox.append(h('p', { class: 'ability-text', text: message }));
    boxes.movesBox.textContent = '';
    boxes.movesBox.append(h('p', { class: 'ability-text', text: message }));
    return;
  }
  if (signal.aborted) return;

  if (!Object.keys(entry.stats || {}).length) {
    const stats = Object.fromEntries((mon.stats || []).map((s) => [s.stat.name, s.base_stat]));
    boxes.statsBox.textContent = '';
    boxes.statsBox.append(statsView(stats));
  }

  renderMovepool(boxes.movesBox, poke.movepoolFor(mon, generation), signal);

  const abilities = await poke.abilitiesFor(mon);
  if (signal.aborted) return;
  boxes.abilitiesBox.textContent = '';
  boxes.abilitiesBox.append(abilitiesView(abilities));
}

function statsView(stats) {
  const entries = Object.entries(stats || {});
  if (!entries.length) return h('p', { class: 'ability-text', text: 'No stats recorded.' });

  const order = Object.keys(STAT_LABELS);
  entries.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));

  const rows = entries.map(([key, value]) =>
    h('div', { class: 'stat-row' }, [
      h('span', { class: 'stat-name', text: STAT_LABELS[key] || poke.prettify(key) }),
      h('span', { class: 'stat-value', text: String(value) }),
      h('div', { class: 'stat-track' }, [
        h('div', {
          class: 'stat-fill',
          /* 200 rather than 255 as full scale: almost nothing has a base stat
             above 200, so scaling to 255 makes every bar look mediocre and
             flattens the differences that matter. */
          style: `width:${Math.min(100, (value / 200) * 100)}%;background:${statColor(value)}`,
        }),
      ]),
    ]),
  );

  const total = entries.reduce((sum, [, value]) => sum + value, 0);
  rows.push(
    h('div', { class: 'stat-row stat-total' }, [
      h('span', { class: 'stat-name', text: 'Total' }),
      h('span', { class: 'stat-value', text: String(total) }),
      h('span', {}),
    ]),
  );

  return h('div', {}, rows);
}

function statColor(value) {
  if (value >= 130) return 'var(--good)';
  if (value >= 95) return 'var(--accent)';
  if (value >= 65) return 'var(--warn)';
  return 'var(--danger)';
}

/* Grouped by multiplier rather than listed per type: what a drafter reads off
 * this is "what kills it", and the 4x line is the one that decides picks. */
function matchupsView(types) {
  if (!types.length) return h('p', { class: 'ability-text', text: 'No typing recorded.' });

  const chart = poke.matchups(types);
  const groups = new Map();
  for (const [type, multiplier] of Object.entries(chart)) {
    if (!groups.has(multiplier)) groups.set(multiplier, []);
    groups.get(multiplier).push(type);
  }

  const labels = { 4: '4×', 2: '2×', 0.5: '½×', 0.25: '¼×', 0: '0×' };
  const lines = [...groups.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([multiplier, list]) =>
      h('div', { class: 'matchup-line' }, [
        h('span', {
          class: `matchup-key${multiplier === 4 ? ' x4' : multiplier === 0 ? ' x0' : ''}`,
          text: labels[multiplier] || `${multiplier}×`,
        }),
        ...list.sort().map((type) => typePip(type)),
      ]),
    );

  return h('div', { class: 'matchups' }, lines.length ? lines : [
    h('p', { class: 'ability-text', text: 'Neutral to everything.' }),
  ]);
}

function abilitiesView(abilities) {
  if (!abilities.length) return h('p', { class: 'ability-text', text: 'No abilities listed.' });

  return h(
    'div',
    {},
    abilities.map((item) =>
      h('div', { class: 'ability' }, [
        h('div', { class: 'ability-head' }, [
          h('span', { class: 'ability-name', text: item.label }),
          item.hidden ? h('span', { class: 'ability-hidden', text: 'hidden' }) : null,
        ]),
        h('p', { class: 'ability-text', text: item.text || 'No description available.' }),
      ]),
    ),
  );
}

/* The movepool renders in two passes. Names, levels and learn methods come
 * out of the Pokemon request already in hand, so the table is complete and
 * scrollable immediately. Type, category and power need one request per move,
 * so they stream in and fill the cells they belong to. */
function renderMovepool(box, moves, signal) {
  box.textContent = '';

  if (!moves.length) {
    box.append(
      h('p', {
        class: 'ability-text',
        text: 'Nothing learnable in this generation — this form may not exist in the format.',
      }),
    );
    return;
  }

  const search = h('input', { type: 'search', placeholder: `Filter ${moves.length} moves…`, autocomplete: 'off' });
  const methodSelect = h('select', { class: 'compact' }, [
    h('option', { value: '', text: 'All sources' }),
    ...[...new Set(moves.map((move) => move.method))].sort().map((method) =>
      h('option', {
        value: method,
        text: moves.find((move) => move.method === method).methodLabel,
      }),
    ),
  ]);

  const tbody = h('tbody', {});
  const rowsByMove = new Map();

  const paint = () => {
    const query = search.value.trim().toLowerCase();
    const method = methodSelect.value;
    tbody.textContent = '';
    rowsByMove.clear();

    for (const move of moves) {
      if (query && !move.label.toLowerCase().includes(query)) continue;
      if (method && move.method !== method) continue;

      const cells = {
        type: h('td', {}),
        category: h('td', { class: 'move-cat' }),
        power: h('td', { class: 'move-num' }),
        accuracy: h('td', { class: 'move-num' }),
      };
      const row = h('tr', {}, [
        h('td', { class: 'move-name', text: move.label }),
        cells.type,
        cells.category,
        cells.power,
        cells.accuracy,
        h('td', {
          class: 'move-how',
          text: move.level ? `Lv ${move.level}` : move.methodLabel,
        }),
      ]);
      rowsByMove.set(move.name, cells);
      if (move.detail) fillMoveCells(cells, move.detail);
      tbody.append(row);
    }
  };

  search.addEventListener('input', paint);
  methodSelect.addEventListener('change', paint);
  paint();

  box.append(
    h('div', { class: 'move-filter' }, [search, methodSelect]),
    h('div', { class: 'move-table-wrap' }, [
      h('table', { class: 'moves' }, [
        h('thead', {}, [
          h('tr', {}, [
            h('th', { text: 'Move' }),
            h('th', { text: 'Type' }),
            h('th', { text: 'Cat' }),
            h('th', { class: 'move-num', text: 'Pow' }),
            h('th', { class: 'move-num', text: 'Acc' }),
            h('th', { text: 'Source' }),
          ]),
        ]),
        tbody,
      ]),
    ]),
  );

  poke.enrichMoves(
    moves,
    (name, detail) => {
      /* Stash on the move so a re-filter after enrichment keeps the data —
       * paint() rebuilds the rows from `moves`, not from the DOM. */
      const move = moves.find((item) => item.name === name);
      if (move) move.detail = detail;
      const cells = rowsByMove.get(name);
      if (cells) fillMoveCells(cells, detail);
    },
    signal,
  );
}

function fillMoveCells(cells, detail) {
  cells.type.textContent = '';
  if (detail.type) cells.type.append(typePip(detail.type));
  cells.category.textContent = detail.category || '—';
  cells.power.textContent = detail.power ?? '—';
  cells.accuracy.textContent = detail.accuracy ? `${detail.accuracy}%` : '—';
}

// ------------------------------------------------------------- wiring

ui.seasonSelect.addEventListener('change', () => {
  const season = state.seasons.find((item) => String(item.id) === ui.seasonSelect.value);
  if (season) selectSeason(season);
});

ui.formatSelect.addEventListener('change', () => selectFormat(ui.formatSelect.value));

for (const control of [ui.search, ui.typeFilter, ui.sort, ui.showBanned]) {
  control.addEventListener('input', render);
}
ui.costCap.addEventListener('input', () => {
  updateCostCapLabel();
  render();
});

for (const button of document.querySelectorAll('.view-button')) {
  button.addEventListener('click', () => {
    state.view = button.dataset.view;
    markActiveView();
    syncUrl(state.openMon);
    render();
  });
}

/* "It's your turn", said once and loudly.
 *
 * Asked for twice on the first draft call — "maybe there should be a nice big
 * pop-up that says your turn" / "that would really help" — because a draft is
 * an hour of waiting and the moment it stops being your turn to wait is easy
 * to miss entirely. The banner tells you if you are looking; this tells you if
 * you are not.
 *
 * Shown once per turn, on the edge rather than on a timer: it appears when the
 * draft goes from not-your-turn to your-turn, and never again for that same
 * pick, so a poll every five seconds cannot make it flash. Dismissing it does
 * not dismiss the turn — the banner stays, and so does the clock.
 */
let alertedPick = null;

function paintTurnAlert() {
  const turn = draft.turnState();

  if (!turn.live || !turn.myTurn) {
    // Your turn ended. Take the alert with it: a "your pick" over somebody
    // else's turn is worse than no alert at all.
    if (!turn.myTurn) closeTurnAlert();
    return;
  }
  // One alert per pick. `pickNumber` changes when the draft moves on, which is
  // what makes this fire once rather than every poll.
  if (alertedPick === turn.pickNumber) return;
  alertedPick = turn.pickNumber;
  openTurnAlert(turn);
}

function openTurnAlert(turn) {
  clear(ui.turnAlert);
  ui.turnAlert.hidden = false;

  ui.turnAlert.append(h('div', { class: 'turn-alert-card' }, [
    h('div', { class: 'turn-alert-flash', text: 'Your pick' }),
    h('p', { class: 'turn-alert-line' }, [
      h('strong', { text: turn.teamName || 'You' }),
      document.createTextNode(` — round ${turn.round}, ${turn.remaining} points left.`),
    ]),
    turn.seconds
      ? h('p', { class: 'turn-alert-clock', text: `${turn.seconds} seconds on the clock.` })
      : h('p', { class: 'turn-alert-clock', text: 'No timer on this draft — take your time.' }),
    h('div', { class: 'turn-alert-actions' }, [
      h('button', {
        class: 'button is-primary',
        type: 'button',
        text: 'Make your pick',
        onclick: () => {
          closeTurnAlert();
          showTab('draft');
        },
      }),
      h('button', {
        class: 'button is-quiet',
        type: 'button',
        text: 'Keep looking',
        onclick: closeTurnAlert,
      }),
    ]),
  ]));

  ui.turnAlert.querySelector('.button')?.focus();
}

function closeTurnAlert() {
  ui.turnAlert.hidden = true;
  clear(ui.turnAlert);
}

/* "It is your pick", on the pool.
 *
 * The Pool tab and the Draft tab were the confusing pair in the first live
 * session: people browsed here, clicked a Pokémon expecting to take it, and
 * had to be told the picking happens elsewhere. Now the pool says when it is
 * your turn and lets you pick without leaving — the Draft tab is still where
 * the order, the clock and everybody's picks live.
 */
function paintTurnBanner() {
  const turn = draft.turnState();
  const show = turn.live && state.tab === 'board';
  ui.turnBanner.hidden = !show;
  if (!show) return;

  clear(ui.turnBanner);
  if (turn.myTurn) {
    ui.turnBanner.className = 'turn-banner is-mine';
    ui.turnBanner.append(
      h('span', { class: 'turn-pill', text: 'Your pick' }),
      h('span', { class: 'turn-text', text:
        `${turn.remaining} points left. Open any Pokémon below and draft it from there.` }),
      h('button', {
        class: 'button is-quiet',
        type: 'button',
        text: 'Go to the draft',
        onclick: () => showTab('draft'),
      }),
    );
    return;
  }
  ui.turnBanner.className = 'turn-banner';
  ui.turnBanner.append(
    h('span', { class: 'turn-text', text: `Draft in progress — ${turn.onClock} is picking.` }),
    h('button', {
      class: 'button is-quiet',
      type: 'button',
      text: 'Watch the draft',
      onclick: () => showTab('draft'),
    }),
  );
}

/* The open panel keeps up with the draft: your turn can arrive while you are
 * reading a Pokémon's movepool, and the button has to turn up without closing
 * what you were looking at. */
function refreshDraftAction() {
  if (ui.detail.hidden || !state.openMon) return;
  const holder = ui.detail.querySelector('.detail-draft-slot');
  const entry = state.pool.find((row) => row.api_name === state.openMon);
  if (!holder || !entry) return;
  clear(holder);
  const action = draftAction(entry);
  if (action) holder.append(action);
}

function markActiveView() {
  for (const button of document.querySelectorAll('.view-button')) {
    button.classList.toggle('is-active', button.dataset.view === state.view);
  }
}
markActiveView();

/* Both drawers share the scrim, so the scrim closes whichever is open. */
function closeAllOverlays() {
  if (!ui.detail.hidden) closeDetail();
  planner.closeOverlays();
  roster.closeOverlays();
  ui.scrim.hidden = true;
}

ui.scrim.addEventListener('click', closeAllOverlays);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !ui.turnAlert.hidden) {
    closeTurnAlert();
    return;
  }
  if (event.key === 'Escape' && !ui.scrim.hidden) closeAllOverlays();
});

ui.themeToggle.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  ui.themeToggle.textContent = next === 'light' ? '☀' : '☾';
  try {
    localStorage.setItem('draftmons-theme', next);
  } catch {
    /* private window; the theme just will not persist */
  }
});

try {
  const saved = localStorage.getItem('draftmons-theme');
  if (saved) {
    document.documentElement.dataset.theme = saved;
    ui.themeToggle.textContent = saved === 'light' ? '☀' : '☾';
  }
} catch {
  /* no storage access; stays on the default dark theme */
}

// ---------------------------------------------------------------- tabs

/* The board and the planner are two views of the same season and pool, so
 * switching tabs changes nothing but which one is on screen. The pool is
 * fetched once either way. */
function showTab(name) {
  state.tab = TABS.includes(name) ? name : 'board';
  const onPlanner = state.tab === 'planner';
  const onRoster = state.tab === 'roster';
  const onDraft = state.tab === 'draft';
  const onStats = state.tab === 'stats';
  const onLeague = state.tab === 'league';
  const onBoard = state.tab === 'board';

  closeAllOverlays();
  ui.planner.hidden = !onPlanner;
  ui.roster.hidden = !onRoster;
  ui.draft.hidden = !onDraft;
  ui.stats.hidden = !onStats;
  ui.league.hidden = !onLeague;
  ui.board.hidden = !onBoard;
  // The board's filters mean nothing on any other tab, and the pool may be
  // empty on the board itself.
  ui.controls.hidden = !onBoard || state.pool.length === 0;

  for (const tab of document.querySelectorAll('.tab')) {
    const active = tab.dataset.tab === state.tab;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', String(active));
  }

  syncUrl(state.openMon);
  paintTurnBanner();
  if (onPlanner) planner.refresh();
  if (onRoster) roster.refresh();
  if (onStats) standings.refresh();
  if (onLeague) league.refresh();

  /* The poll runs for as long as a draft is live, whatever tab is on screen —
   * the pool has to know your turn came up while you were browsing it. Opening
   * the draft tab still forces a read rather than waiting for the next tick. */
  if (onDraft) draft.refresh();
  draft.startPolling();
}

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => showTab(tab.dataset.tab));
}

/* The planner reads the board's state through accessors rather than being
 * handed a snapshot, so it always sees the current pool without app.js having
 * to push updates into it on every change. */
draft.onChange(() => {
  paintTurnBanner();
  paintTurnAlert();
  refreshDraftAction();
});

draft.init({
  season: () => state.season,
  pool: () => state.pool,
  /* The draft tab's "Join this draft" button hands off to the league screen,
   * so there is one join flow rather than two that can disagree. */
  openJoin: () => league.focusJoin(),
});

setup.init({
  formats: () => state.formats,
  /* The wizard creates a season through the API; the app has to notice. The
   * list comes back from the server rather than having the new league pushed
   * into it, so its counts are right the moment it appears in the library. */
  onCreated: async (season) => {
    await loadSeasons({ keep: true });
    const created = state.seasons.find((item) => item.id === season.id);
    if (created) selectSeason(created);
  },
});

leagues.init({
  current: () => state.season,
  select: (id) => {
    const wanted = state.seasons.find((season) => season.id === id);
    if (wanted) selectSeason(wanted);
  },
  /* Renaming or deleting changes the list, not which league you are in — so
   * the reload keeps the current one where it still exists. */
  reload: () => loadSeasons({ keep: true }),
  openSetup: () => setup.open(),
});

league.init({
  season: () => state.season,
  showTab,
  /* Joining, leaving or renaming changes who the draft tab thinks you are, so
   * it re-reads rather than waiting for its next poll. */
  onIdentityChange: () => {
    if (state.season) loadLeagueTeams(state.season.id);
    draft.refresh();
    // Holding the admin token is what puts the result form on the stats tab.
    standings.refresh();
  },
});

standings.init({ season: () => state.season });

/* One shared drawer and one shared set editor behind both tabs. */
sheet.init();

roster.init({
  season: () => state.season,
  entryFor: (apiName) => state.pool.find((entry) => entry.api_name === apiName) || null,
  rules: () => state.rules,
  formatKey: () => state.formatKey,
});

planner.init({
  season: () => state.season,
  pool: () => state.pool,
  formatKey: () => state.formatKey,
  rules: () => state.rules,
  entryFor: (apiName) => state.pool.find((entry) => entry.api_name === apiName) || null,
});

/* Formats first: selectSeason checks the loaded formats to decide whether the
 * season's stored format_key can be preselected. */
await loadFormats();
await loadSeasons();
showTab(state.tab);
