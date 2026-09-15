/* The draft tab: the shared board, and the one control that changes it.
 *
 * Everything on screen is derived from GET /draft/board, which is public — the
 * whole point of this tab is that every team's roster and remaining points are
 * visible to everyone, so there is nothing to hide behind a token.
 *
 * The token only decides what you can *do*: a player may pick when their team
 * is on the clock, a commissioner may start the draft and undo the last pick,
 * and a spectator holding neither still sees the same board.
 *
 * The server is the authority on every rule. This module greys out a Pokemon a
 * team cannot afford, but it does not decide anything — the pick goes to the
 * server and the server's refusal is what gets shown. Duplicating the rules
 * here would mean two implementations that drift.
 */

import { api } from './api.js';
import { h, $, clear, placeholder, spriteFor, typePip } from './dom.js';
import * as queue from './queue.js';
import * as session from './session.js';
import { logoFor } from './teamlogo.js';

let ctx = null;
let ui = {};
let board = null;
let poll = null;
let tick = null;
let search = '';

/* The picker's chrome is built once per turn and kept. It used to be rebuilt
 * on every keystroke and again on every five second poll, which destroyed the
 * search box mid-word — you could type one letter, lose focus, and had to
 * click back in for the next. The grid repaints; the box around it does not. */
let picker = null;

/* The Pokemon waiting on a confirmation, if the confirm panel is open. Clicking
 * a card used to draft it outright, which is how somebody ends up with a
 * Pokemon they only meant to look at. */
let confirming = null;
/* Seconds left, counted down locally between polls.
 *
 * Taken from the server's `seconds_left` rather than from its `deadline`
 * against Date.now(): a browser whose clock is a few minutes off would
 * otherwise show a deadline that has already passed, or one that never
 * arrives. A duration is the same number on every machine. */
let secondsLeft = null;
/* The order being edited, as team ids. Held separately from the board so a
 * half-finished reorder is not lost to a refresh, and so nothing is written
 * until the commissioner says so. */
let orderIds = null;
let dragging = null;

export function init(context) {
  ctx = context;
  ui = {
    root: $('#draft'),
    status: $('#draft-status'),
    actions: $('#draft-actions'),
    teams: $('#draft-teams'),
    picker: $('#draft-picker'),
    order: $('#draft-order'),
  };
}

/* ------------------------------------------------------------- loading */

export async function refresh() {
  const season = ctx.season();
  if (!season) {
    clear(ui.teams);
    clear(ui.status);
    clear(ui.actions);
    clear(ui.picker);
    ui.teams.append(placeholder('◇', 'No season selected', 'Pick a season to see its draft.'));
    return;
  }

  try {
    board = await api(`/seasons/${season.id}/draft/board`);
    secondsLeft = board.seconds_left;
    /* Anything drafted — by you or by anyone else — leaves your queue, so what
     * it offers is always something you can actually take. */
    queue.prune(season.id, board.teams.flatMap((team) => team.picks.map((p) => p.api_name)));
  } catch (error) {
    clear(ui.teams);
    ui.teams.append(placeholder('◇', 'No draft yet', error.message));
    watcher?.();
    return;
  }
  render();
  watcher?.();
}

/* A draft is several people acting on one shared board, so a stale view is
 * actively misleading — you would be looking at a Pokémon someone already
 * took. Polling runs while the window is visible and the draft is live,
 * whatever tab is on screen: browsing the pool during a draft is the normal
 * thing to do, and it is the tab that most needs to know your turn came up. */
export function startPolling() {
  stopPolling();
  poll = setInterval(() => {
    if (!document.hidden && board && board.status !== 'complete') refresh();
  }, 5000);

  /* The countdown ticks locally so it moves every second rather than in five
   * second jumps. It is display only — the server decides when a turn is
   * actually over, and this reaching zero just means it is worth asking. */
  tick = setInterval(() => {
    if (secondsLeft === null || secondsLeft === undefined) return;
    if (secondsLeft > 0) {
      secondsLeft -= 1;
      paintClock();
      // Hitting zero locally is the cue to go and find out what the server
      // did about it — defer, autopick, or nothing yet.
      if (secondsLeft === 0 && !document.hidden) refresh();
    }
  }, 1000);
}

/* Escape closes it, the same as the board's detail panel and both drawers. A
 * modal that only a mouse can dismiss is one people click through by accident,
 * which is the problem this panel exists to solve. */
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && confirming) closeConfirm();
});

export function stopPolling() {
  if (poll) clearInterval(poll);
  if (tick) clearInterval(tick);
  poll = null;
  tick = null;
}

function clockText(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

/** Repaint just the countdown, so a ticking second does not rebuild the board. */
function paintClock() {
  const node = ui.status.querySelector('.draft-clock');
  if (!node || secondsLeft === null) return;
  node.textContent = clockText(secondsLeft);
  node.classList.toggle('is-urgent', secondsLeft <= 15);
}

/* -------------------------------------------------------------- render */

function render() {
  renderStatus();
  renderActions();
  renderOrder();
  renderTeams();
  renderPicker();
}

/* ---------------------------------------------------------- draft order */

/* Who picks first, and the snake follows from it. Only editable before the
 * draft starts: the order is what the snake reads to decide whose turn it is,
 * so rewriting it mid-draft would hand the clock to a different team and leave
 * the picks already made sitting in the wrong slots. The server refuses that
 * too — this just does not offer it. */
function renderOrder() {
  const seasonId = ctx.season()?.id;
  const editable = board.status === 'setup' && session.isAdmin(seasonId);
  ui.order.hidden = !editable;
  clear(ui.order);
  if (!editable) {
    orderIds = null;
    return;
  }

  const ids = board.teams.map((team) => team.id);
  // Rebuild when the teams themselves changed — somebody joined or left —
  // rather than on every render, which would undo a reorder in progress.
  if (!orderIds || orderIds.length !== ids.length
      || orderIds.some((id) => !ids.includes(id))) {
    orderIds = ids;
  }

  const byId = new Map(board.teams.map((team) => [team.id, team]));
  const list = h('ol', { class: 'order-list' });

  orderIds.forEach((id, index) => {
    const team = byId.get(id);
    const row = h('li', {
      class: 'order-row', draggable: 'true',
      ondragstart: (event) => {
        dragging = id;
        event.dataTransfer.effectAllowed = 'move';
      },
      ondragover: (event) => {
        event.preventDefault();
        if (dragging === null || dragging === id) return;
        move(orderIds.indexOf(dragging), index);
      },
      ondragend: () => { dragging = null; render(); },
    }, [
      h('span', { class: 'order-pos', text: String(index + 1) }),
      team ? logoFor(team, 'team-logo is-small') : null,
      h('span', { class: 'order-name', text: team?.name || `Team ${id}` }),
      team?.owner ? h('span', { class: 'order-owner', text: team.owner }) : null,
      // Buttons as well as dragging: dragging does not work on a phone, and
      // cannot be driven from a keyboard.
      h('button', {
        class: 'icon-button', type: 'button', title: 'Move up', text: '↑',
        disabled: index === 0 ? true : null,
        onclick: () => { move(index, index - 1); render(); },
      }),
      h('button', {
        class: 'icon-button', type: 'button', title: 'Move down', text: '↓',
        disabled: index === orderIds.length - 1 ? true : null,
        onclick: () => { move(index, index + 1); render(); },
      }),
    ]);
    list.append(row);
  });

  const unset = (board.unordered_team_ids || []).length;
  const save = h('button', { class: 'button is-primary', type: 'button', text: 'Save order' });
  save.addEventListener('click', () => act(() => api(
    `/seasons/${seasonId}/teams/order`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...session.headers(seasonId, { admin: true }) },
      body: JSON.stringify(orderIds),
    },
  )));

  ui.order.append(
    h('div', { class: 'order-head' }, [
      h('h3', { class: 'order-title', text: 'Draft order' }),
      h('span', {
        class: 'draft-note',
        text: unset
          ? `${unset} team${unset === 1 ? '' : 's'} still unplaced — set the order before starting.`
          : 'Round 1 runs top to bottom; round 2 runs back up.',
      }),
    ]),
    list,
    h('div', { class: 'order-actions' }, [
      h('button', {
        class: 'button', type: 'button', text: 'Shuffle',
        onclick: () => { shuffle(orderIds); render(); },
      }),
      h('button', {
        class: 'button', type: 'button', text: 'Reverse',
        onclick: () => { orderIds.reverse(); render(); },
      }),
      save,
    ]),
  );
}

function move(from, to) {
  if (from < 0 || to < 0 || to >= orderIds.length) return;
  orderIds.splice(to, 0, orderIds.splice(from, 1)[0]);
}

/** Fisher-Yates, in place. Array.sort(() => Math.random() - 0.5) is not a shuffle. */
function shuffle(items) {
  for (let i = items.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [items[i], items[j]] = [items[j], items[i]];
  }
}

function myTurn() {
  const mine = session.myTeamId(ctx.season()?.id);
  return Boolean(mine) && board?.on_clock_team_id === mine && board?.status === 'live';
}

/* What the rest of the app needs to know about the draft without owning it.
 *
 * The Pool tab is the one people browse during a draft — that is what it is
 * for — and the first live session showed the cost of it being unable to say
 * "it is your pick" or to act on one: everybody clicked a Pokémon there,
 * expected to draft it, and had to be told to go to another tab.
 */
export function turnState() {
  const season = ctx.season();
  if (!season || !board || board.status !== 'live') {
    return { live: false, myTurn: false, remaining: 0, onClock: null, taken: new Set() };
  }
  const me = board.teams.find((team) => team.id === session.myTeamId(season.id));
  return {
    live: true,
    myTurn: myTurn(),
    remaining: me?.remaining ?? 0,
    teamName: me?.name || null,
    onClock: teamName(board.on_clock_team_id),
    round: board.round_no,
    seconds: board.seconds_left,
    /* Which pick of the draft this is. The "your turn" alert keys off it so it
     * fires once when the turn arrives rather than on every poll. */
    pickNumber: board.picks_made,
    taken: new Set(board.teams.flatMap((team) => team.picks.map((pick) => pick.api_name))),
  };
}

/** Draft a Pokémon from another tab. Opens the same confirmation panel. */
export function pickFrom(entry) {
  const season = ctx.season();
  const me = board?.teams.find((team) => team.id === session.myTeamId(season?.id));
  if (!me || !myTurn()) return false;
  openConfirm(entry, me);
  return true;
}

/* One listener, for the tab that paints a "your pick" banner off the back of
 * the poll. A second subscriber would want a Set; there is no second one. */
let watcher = null;

export function onChange(fn) {
  watcher = fn;
}

function teamName(id) {
  return board.teams.find((team) => team.id === id)?.name || '—';
}

function renderStatus() {
  clear(ui.status);
  const { status, picks_made: made, picks_total: total, round_no: round } = board;

  if (status === 'setup') {
    ui.status.append(
      h('span', { class: 'draft-pill is-setup', text: 'Not started' }),
      h('span', { class: 'draft-note', text: `${board.team_count} teams · ${board.rounds} rounds · ${board.budget} points each` }),
    );
    return;
  }

  if (status === 'complete') {
    ui.status.append(
      h('span', { class: 'draft-pill is-done', text: 'Draft complete' }),
      h('span', { class: 'draft-note', text: `${made} picks made` }),
    );
    return;
  }

  const onClock = teamName(board.on_clock_team_id);
  ui.status.append(
    h('span', { class: myTurn() ? 'draft-pill is-you' : 'draft-pill is-live',
                text: myTurn() ? 'Your pick' : `${onClock} is picking` }),
    h('span', { class: 'draft-note', text: `Round ${round} · pick ${made + 1} of ${total}` }),
  );

  // Who is coming up, so a player can plan a round ahead rather than refresh.
  if (secondsLeft !== null && secondsLeft !== undefined) {
    ui.status.append(h('span', {
      class: secondsLeft <= 15 ? 'draft-clock is-urgent' : 'draft-clock',
      text: clockText(secondsLeft),
      title: 'Time left on this pick',
    }));
  }

  const upcoming = (board.upcoming || []).slice(1, 4).map(teamName);
  if (upcoming.length) {
    ui.status.append(h('span', { class: 'draft-next', text: `next: ${upcoming.join(' → ')}` }));
  }

  // What the clock did while nobody was looking. Without this a pick simply
  // appears on someone else's roster with no explanation.
  for (const event of board.clock_events || []) {
    ui.status.append(h('span', { class: 'draft-event', text: event.detail }));
  }

  const late = board.deferred_this_round || [];
  if (late.length) {
    ui.status.append(h('span', {
      class: 'draft-note',
      text: `picking last this round: ${late.map(teamName).join(', ')}`,
    }));
  }
}

function renderActions() {
  clear(ui.actions);
  const season = ctx.season();
  const seasonId = season.id;

  if (!session.isPlayer(seasonId) && !session.isAdmin(seasonId)) {
    ui.actions.append(h('button', {
      class: 'button', type: 'button', text: 'Join this draft',
      onclick: () => ctx.openJoin(),
    }));
  } else {
    const held = session.forSeason(seasonId);
    ui.actions.append(h('span', {
      class: 'draft-identity',
      text: held.team ? `You are ${held.team.name}` : 'Commissioner',
    }));
  }

  if (!session.isAdmin(seasonId)) return;

  if (board.status === 'setup') {
    // The timer is set once, when the draft opens, because changing it
    // mid-draft would move a deadline someone is already racing.
    const timer = h('select', { class: 'compact', title: 'Time on the clock per pick' },
      [['0', 'No timer'], ['60', '1 min'], ['120', '2 min'], ['300', '5 min'],
       ['600', '10 min'], ['86400', '24 hours']].map(([value, label]) =>
        h('option', { value, text: label, selected: value === '120' ? true : null })));

    ui.actions.append(timer, h('button', {
      class: 'button is-primary', type: 'button', text: 'Start draft',
      onclick: () => act(() => api(`/seasons/${seasonId}/draft/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...session.headers(seasonId, { admin: true }) },
        body: JSON.stringify({ randomize: false, pick_seconds: Number(timer.value) }),
      })),
    }));
  }
  if (board.picks_made > 0) {
    ui.actions.append(h('button', {
      class: 'button', type: 'button', text: 'Undo last pick',
      onclick: () => act(() => api(`/seasons/${seasonId}/draft/undo`, {
        method: 'POST',
        headers: session.headers(seasonId, { admin: true }),
      })),
    }));
  }
}

function renderTeams() {
  clear(ui.teams);
  if (!board.teams.length) {
    ui.teams.append(placeholder('◇', 'No teams yet', 'Teams appear here as people join.'));
    return;
  }

  for (const team of board.teams) {
    const onClock = team.id === board.on_clock_team_id && board.status === 'live';
    const mine = team.id === session.myTeamId(ctx.season()?.id);
    const deferred = (board.deferred_this_round || []).includes(team.id);

    const slots = [];
    for (const pick of team.picks) {
      if (pick.forfeited) {
        slots.push(h('li', { class: 'draft-pick is-forfeit' }, [
          h('span', { class: 'draft-pick-name', text: 'Skipped — out of time' }),
        ]));
        continue;
      }
      slots.push(h('li', { class: 'draft-pick' }, [
        spriteFor(pick, 'draft-pick-sprite'),
        h('span', { class: 'draft-pick-name', text: pick.display_name }),
        h('span', { class: 'draft-pick-types' }, (pick.types || []).map(typePip)),
        h('span', { class: 'draft-pick-cost', text: String(pick.cost_paid) }),
      ]));
    }
    // Empty slots make a half-built roster legible at a glance.
    for (let i = team.picks.length; i < board.roster_size; i += 1) {
      slots.push(h('li', { class: 'draft-pick is-empty', text: `Round ${i + 1}` }));
    }

    ui.teams.append(h('section', {
      class: ['draft-team', onClock ? 'is-on-clock' : '', mine ? 'is-mine' : '']
        .filter(Boolean).join(' '),
    }, [
      h('header', { class: 'draft-team-head' }, [
        h('span', { class: 'draft-team-pos', text: `#${team.draft_position ?? '—'}` }),
        logoFor(team),
        h('h3', { class: 'draft-team-name', text: team.name }),
        deferred ? h('span', { class: 'draft-team-late', text: 'timed out — picks last',
                               title: 'Ran out of time; moved to the back of this round' }) : null,
        team.owner ? h('span', { class: 'draft-team-owner', text: team.owner }) : null,
        h('span', {
          class: team.remaining <= 0 ? 'draft-team-points is-out' : 'draft-team-points',
          text: `${team.remaining} left`,
        }),
      ]),
      h('ul', { class: 'draft-slots' }, slots),
    ]));
  }
}

/* The pool, filtered to what is still available and priced against what the
 * picking team can actually spend. Only rendered when it is your turn: a
 * pick button nobody can press is noise on a board people stare at for an
 * hour. */
function renderPicker() {
  const mine = myTurn();
  ui.picker.hidden = !mine;

  if (!mine) {
    // Your turn ended — by picking, by the clock, or by an undo. Anything you
    // were part-way through choosing is no longer yours to choose.
    clear(ui.picker);
    picker = null;
    closeConfirm();
    return;
  }

  if (!picker) buildPicker();
  paintPicker();
}

/* Built once when your turn starts. Everything in here survives a poll, which
 * is the whole point: the five second refresh must not reach into what you are
 * typing or where you have scrolled. */
function buildPicker() {
  clear(ui.picker);

  const title = h('h3', { class: 'picker-title' });
  const box = h('input', {
    class: 'picker-search',
    type: 'search',
    value: search,
    placeholder: 'Search by name or type…',
    autocomplete: 'off',
    'aria-label': 'Search the pool',
    // Repaints the grid only. Rebuilding the shell here is what used to throw
    // away the focused input between one letter and the next.
    oninput: (event) => {
      search = event.target.value;
      paintPicker();
    },
  });
  const count = h('p', { class: 'picker-more' });
  const grid = h('div', { class: 'picker-grid' });
  const queued = h('div', { class: 'queue-strip' });

  ui.picker.append(h('div', { class: 'picker-head' }, [title, box]), queued, grid, count);
  picker = { title, box, grid, count, queued };
  if (!document.getElementById('draft')?.hidden) box.focus();
}

/* How many cards to build at once. The whole pool is 771 and a grid that long
 * is slow to paint and useless to read — but a hard 60 was worse: sorted by
 * cost, a pool priced flat shows A to C and nothing else, which is exactly
 * what happened on the first live draft. */
const PICKER_LIMIT = 200;

function paintPicker() {
  const me = board.teams.find((team) => team.id === session.myTeamId(ctx.season().id));
  const taken = new Set(board.teams.flatMap((team) => team.picks.map((p) => p.api_name)));
  const query = search.trim().toLowerCase();

  const available = ctx.pool()
    .filter((entry) => !entry.banned && !taken.has(entry.api_name))
    // Type as well as name: "grass" is what someone types when they want a
    // grass type, and matching only names sends them away empty.
    .filter((entry) => !query
      || entry.display_name.toLowerCase().includes(query)
      || (entry.types || []).some((type) => type.toLowerCase().includes(query)))
    .sort((a, b) => b.cost - a.cost || a.display_name.localeCompare(b.display_name));

  picker.title.textContent = `Your pick — ${me.remaining} points left`;
  paintQueue(me, taken);

  // Repainting resets the scroll to the top otherwise, which during a poll
  // looks like the page jumping under you mid-scroll.
  const scrolled = picker.grid.scrollTop;
  clear(picker.grid);

  const season = ctx.season();
  for (const entry of available.slice(0, PICKER_LIMIT)) {
    const afford = entry.cost <= me.remaining;
    const rank = queue.positionOf(season.id, entry.api_name);
    picker.grid.append(h('button', {
      class: [
        'picker-card',
        ...(afford ? [] : ['is-unaffordable']),
        ...(rank ? ['is-queued'] : []),
      ].join(' '),
      type: 'button',
      title: afford ? `${entry.display_name} — ${entry.cost} points`
        : `${entry.display_name} costs ${entry.cost}; you have ${me.remaining}`,
      // Opens the Pokemon rather than drafting it. The pick is one more click,
      // deliberately: this list is scrolled fast and a misfire is permanent.
      onclick: () => openConfirm(entry, me),
    }, [
      spriteFor(entry, 'picker-sprite'),
      h('span', { class: 'picker-name', text: entry.display_name }),
      rank ? h('span', { class: 'picker-queued', text: `#${rank}` }) : null,
      h('span', { class: 'picker-cost', text: String(entry.cost) }),
    ]));
  }
  picker.grid.scrollTop = scrolled;

  const shown = Math.min(available.length, PICKER_LIMIT);
  picker.count.textContent = available.length > PICKER_LIMIT
    ? `Showing ${shown} of ${available.length} — search to narrow it down.`
    : `${available.length} available`;

  if (!available.length) {
    picker.grid.append(placeholder(
      '∅',
      query ? 'Nothing matches' : 'Nothing left to pick',
      query ? 'Try another name or a type.' : 'Every Pokémon has been drafted.',
    ));
  }
}

/* --------------------------------------------------------------- acting */

/* What you queued in the Planner, offered in order.
 *
 * The draft is the moment the queue exists for: you decided most of this
 * beforehand, and digging a Pokémon out of eight hundred while a clock runs is
 * the part everybody complained about. Still one confirmation each — the
 * queue removes the search, not the choice.
 */
function paintQueue(me, taken) {
  const season = ctx.season();
  clear(picker.queued);

  const names = queue.listFor(season.id).filter((name) => !taken.has(name));
  if (!names.length) {
    picker.queued.hidden = true;
    return;
  }
  picker.queued.hidden = false;

  picker.queued.append(h('span', { class: 'queue-strip-label', text: 'From your queue' }));
  for (const [index, apiName] of names.slice(0, 6).entries()) {
    const entry = ctx.pool().find((row) => row.api_name === apiName);
    if (!entry) continue;
    const afford = entry.cost <= me.remaining;
    picker.queued.append(h('button', {
      class: afford ? 'queue-chip' : 'queue-chip is-unaffordable',
      type: 'button',
      title: afford
        ? `${entry.display_name} — ${entry.cost} points`
        : `${entry.display_name} costs ${entry.cost}; you have ${me.remaining}`,
      onclick: () => openConfirm(entry, me),
    }, [
      h('span', { class: 'queue-chip-rank', text: String(index + 1) }),
      spriteFor(entry, 'queue-chip-sprite'),
      h('span', { class: 'queue-chip-name', text: entry.display_name }),
      h('span', { class: 'queue-chip-cost', text: String(entry.cost) }),
    ]));
  }
}

/* The confirmation step.
 *
 * Both of the first two people to use this drafted something by accident: the
 * card was the button, and one click spent a pick. So the card now opens the
 * Pokemon — everything the board would have shown, without leaving the draft —
 * and the pick is a second, deliberate click.
 *
 * Built from the pool entry alone, no PokeAPI call, so it opens instantly on a
 * clock that is counting down.
 */
function openConfirm(entry, me) {
  closeConfirm();

  const afford = entry.cost <= me.remaining;
  const after = me.remaining - entry.cost;

  const draftButton = h('button', {
    class: 'button is-primary',
    type: 'button',
    text: `Draft ${entry.display_name}`,
    onclick: () => {
      closeConfirm();
      submitPick(entry.api_name);
    },
  });

  const scrim = h('div', {
    class: 'confirm-scrim',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': `Draft ${entry.display_name}?`,
    onclick: (event) => { if (event.target === scrim) closeConfirm(); },
  }, [
    h('div', { class: 'confirm-panel' }, [
      h('div', { class: 'confirm-head' }, [
        spriteFor({ ...entry, sprite_url: entry.artwork_url || entry.sprite_url }, 'confirm-art'),
        h('div', { class: 'confirm-ident' }, [
          h('h3', { class: 'confirm-name', text: entry.display_name }),
          h('div', { class: 'confirm-types' },
            (entry.types || []).map((type) => typePip(type))),
          h('div', { class: 'confirm-tags' }, [
            h('span', { class: 'confirm-cost', text: `${entry.cost} pts` }),
            entry.tier ? h('span', { class: 'confirm-tier', text: entry.tier }) : null,
            entry.bst ? h('span', { class: 'confirm-bst', text: `BST ${entry.bst}` }) : null,
          ]),
        ]),
      ]),
      statsView(entry.stats || {}),
      h('p', {
        class: afford ? 'confirm-note' : 'confirm-note is-over',
        text: afford
          ? `${me.remaining} points now, ${after} after this pick.`
          : `This costs ${entry.cost} and you have ${me.remaining}. The draft will refuse it.`,
      }),
      h('div', { class: 'confirm-actions' }, [
        draftButton,
        h('button', { class: 'button is-quiet', type: 'button', text: 'Back to the pool',
                      onclick: () => closeConfirm() }),
      ]),
    ]),
  ]);

  document.body.append(scrim);
  confirming = { entry, scrim };
  draftButton.focus();
}

function closeConfirm() {
  confirming?.scrim?.remove();
  confirming = null;
}

/* The six base stats, from what the pool already stores. The reason this panel
 * exists rather than a bare "are you sure": the thing you want before spending
 * a pick is the Pokemon, not a yes/no. */
function statsView(stats) {
  const entries = Object.entries(stats);
  if (!entries.length) return null;

  const LABELS = {
    hp: 'HP', attack: 'Atk', defense: 'Def',
    'special-attack': 'SpA', 'special-defense': 'SpD', speed: 'Spe',
  };
  return h('div', { class: 'confirm-stats' }, entries.map(([key, value]) => h('div', {
    class: 'confirm-stat',
  }, [
    h('span', { class: 'confirm-stat-name', text: LABELS[key] || key }),
    h('span', { class: 'confirm-stat-value', text: String(value) }),
    h('div', { class: 'confirm-stat-track' }, [
      // 200 rather than 255: almost nothing goes past it, and scaling to the
      // theoretical maximum makes every ordinary stat look identical.
      h('div', { class: 'confirm-stat-fill',
                 style: `width:${Math.min(100, (value / 200) * 100)}%` }),
    ]),
  ])));
}

async function submitPick(apiName) {
  const seasonId = ctx.season().id;
  await act(() => api(`/seasons/${seasonId}/draft/pick`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...session.headers(seasonId) },
    body: JSON.stringify({ name: apiName }),
  }));
  /* A search is about the pick you just made. Carrying "tox" into your next
   * turn hides the rest of the pool behind a filter you have forgotten
   * setting, which is exactly how the first live draft went. */
  search = '';
}

/** Run a mutation, then reload the board. Failures are shown, never swallowed. */
async function act(call) {
  try {
    await call();
    flash(null);
  } catch (error) {
    // The server's message is the useful one — "Kingambit costs 18 and you
    // have 12 points left" — so it is shown verbatim rather than replaced.
    flash(error.message);
  }
  await refresh();
}

function flash(message) {
  const existing = ui.root.querySelector('.draft-error');
  if (existing) existing.remove();
  if (!message) return;
  ui.root.prepend(h('div', { class: 'draft-error', role: 'alert', text: message }));
}

/* Joining lives on the league screen, not here.
 *
 * It used to be two window.prompt() calls, which could not show the player
 * their token — and the token cannot be reissued, so a join that only flashes
 * it past you is a seat waiting to be lost. The league tab has the form, the
 * save-your-token step and the commissioner's code, so this button hands off
 * to it rather than keeping a second, worse join flow alive here.
 */
