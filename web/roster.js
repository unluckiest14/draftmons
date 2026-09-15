/* The Roster tab: the team you actually drafted, and who you are bringing.
 *
 * The split from the Planner is the one the league asked for out loud. They
 * are different jobs done at different times:
 *
 *   Planner   before the draft. Who do you want, out of the whole pool,
 *             inside the budget. Nothing here is yours yet.
 *   Roster    after it. The roster is fact — you own what you drafted and
 *             nothing else — so the work is building sets on them and picking
 *             which six you are bringing this week.
 *
 * A lineup is therefore not "which Pokémon" but "which of mine, built how",
 * and keeping several per season is how a league week works: one per opponent.
 * Every Pokémon you drafted is on every lineup from the moment it is drafted;
 * the ones you are bringing are the ticked ones, and only those reach the
 * paste.
 *
 * Whose roster is read from the token this browser holds. A commissioner who
 * ran the draft for the table holds an admin token and no team, so this asks
 * rather than guessing — the draft board is public, so a chosen roster shows
 * nothing that was not already on it.
 */

import { api } from './api.js';
import { h, $, clear, placeholder, spriteFor, typePip } from './dom.js';
import * as poke from './pokeapi.js';
import * as plans from './plans.js';
import * as paste from './pokepaste.js';
import * as session from './session.js';
import * as sheet from './teamsheet.js';

let ctx = null;
const ui = {};

/* What this browser drafted in the current season, or null when there is no
 * draft, no team, or no picks yet. Re-read when the tab is opened rather than
 * kept in step with the draft tab's polling: a roster changes a handful of
 * times in an evening and then never again. */
let roster = null;

/* Every team in the season that drafted something, for the chooser. */
let options = [];

/* Guards two overlapping loads: switching season twice quickly leaves two
 * board requests in flight and the slower one must not win. */
let run = 0;

const TEAM_KEY = 'draftmons.roster-team.v1';

/* Seasons whose old plans have already been sorted into lineups. See
 * claimLineups() for what that means and why it runs once. */
const MIGRATED_KEY = 'draftmons.lineups-claimed.v1';

export function init(context) {
  ctx = context;
  ui.root = $('#roster');
  ui.list = $('#lineup-items');
  ui.main = $('#roster-main');
  ui.newButton = $('#lineup-new');

  ui.newButton.addEventListener('click', () => {
    const season = ctx.season();
    if (!season) return;
    plans.createPlan(season.id, '', plans.LINEUP);
    render();
  });
}

export function refresh() {
  if (!ui.root || ui.root.hidden) return;
  // Paint from what is already known, then repaint when the two things this
  // tab needs land. Waiting on them first leaves it blank on every switch.
  render();

  const season = ctx.season();
  /* Lineups live on the server once you have joined, so they have to be
   * fetched before this tab can show any — or claim the old ones. */
  const lineups = season && !plans.isLoaded(season.id)
    ? plans.load(season.id)
    : Promise.resolve(false);

  Promise.all([loadBoard(), lineups]).then(([boardMoved, plansLanded]) => {
    if ((boardMoved || plansLanded) && ui.root && !ui.root.hidden) render();
  });
}

export function closeOverlays() {
  sheet.closeOverlays();
}

// ------------------------------------------------------------ the roster

function storedTeam(seasonId) {
  try {
    return JSON.parse(localStorage.getItem(TEAM_KEY) || '{}')[String(seasonId)] ?? null;
  } catch {
    return null;
  }
}

function rememberTeam(seasonId, teamId) {
  try {
    const all = JSON.parse(localStorage.getItem(TEAM_KEY) || '{}');
    if (teamId === null) delete all[String(seasonId)];
    else all[String(seasonId)] = teamId;
    localStorage.setItem(TEAM_KEY, JSON.stringify(all));
  } catch {
    /* private window; the choice just will not survive a reload */
  }
}

/* What the view depends on, in one string, so a poll that changed nothing does
 * not repaint over what someone is reading. */
const signature = () =>
  `${roster?.teamId ?? '-'}:${roster?.picks.length ?? 0}:${options.map((t) => t.id).join(',')}`;

async function loadBoard() {
  const season = ctx.season();
  const mine = ++run;
  const before = signature();

  if (!season) {
    roster = null;
    options = [];
    return before !== signature();
  }

  let board;
  try {
    board = await api(`/seasons/${season.id}/draft/board`);
  } catch {
    // No draft yet, or the server is unreachable. Either way there is no
    // roster to build a lineup from.
    if (mine !== run) return false;
    roster = null;
    options = [];
    return before !== signature();
  }
  if (mine !== run) return false;

  // Forfeited picks are rows on the board with no Pokémon in them, and a team
  // that drafted nothing is not a roster anybody can plan.
  options = board.teams
    .map((team) => ({
      ...team,
      picks: (team.picks || []).filter((pick) => !pick.forfeited && pick.api_name),
    }))
    .filter((team) => team.picks.length);

  const ownTeamId = session.myTeamId(season.id);
  const wanted = ownTeamId ?? storedTeam(season.id);
  const team = options.find((row) => row.id === wanted) || null;

  roster = team ? {
    teamId: team.id,
    teamName: team.name,
    picks: team.picks.map((pick) => pick.api_name),
    costOf: new Map(team.picks.map((pick) => [pick.api_name, pick.cost_paid])),
    spent: team.spent,
    budget: board.budget,
    status: board.status,
    // Chosen rather than owned, which decides whether the banner offers a way
    // to change it.
    chosen: team.id !== ownTeamId,
  } : null;

  return before !== signature();
}

const owns = (apiName) => Boolean(roster?.picks.includes(apiName));

/* What the shared team sheet needs from this tab. `save` and `repaint` are the
 * two halves it cannot know: where a lineup is stored, and which tab to
 * redraw afterwards. */
const host = {
  season: () => ctx.season(),
  entryFor: (apiName) => ctx.entryFor(apiName),
  rules: () => ctx.rules(),
  formatKey: () => ctx.formatKey(),
  save: (plan, changes) => plans.updatePlan(ctx.season().id, plan.id, changes),
  repaint: () => render(),
};

// ---------------------------------------------------------------- render

function render() {
  const season = ctx.season();
  clear(ui.list);
  clear(ui.main);

  if (!season) {
    ui.main.append(
      placeholder('◇', 'No season selected', 'Pick a league in the header to see its rosters.'),
    );
    return;
  }

  if (!roster) {
    ui.main.append(options.length ? chooser() : nothingDrafted());
    return;
  }

  claimLineups(season.id);
  const lineups = plans.plansFor(season.id, plans.LINEUP);
  // Every Pokémon you own joins every lineup, so the counts in the sidebar
  // describe the lineup they label.
  for (const lineup of lineups) adoptDraftedPicks(lineup);

  const active = activeLineup();
  for (const lineup of lineups) {
    ui.list.append(lineupRow(lineup, season, lineup.id === active?.id));
  }
  if (!lineups.length) {
    ui.list.append(h('p', { class: 'plan-empty', text:
      '“+ New” puts your drafted team on a sheet, ready to pick six from.' }));
  }

  if (!active) {
    ui.main.append(
      banner(),
      placeholder('◇', 'No lineup open',
        'Create one, and your drafted Pokémon are on the sheet ready to pick from.'),
    );
    return;
  }
  ui.main.append(lineupView(active, season));
}

function activeLineup() {
  const season = ctx.season();
  const id = plans.activeIdFor(season.id, plans.LINEUP);
  return plans.plansFor(season.id, plans.LINEUP).find((plan) => plan.id === id) || null;
}

function nothingDrafted() {
  return placeholder(
    '◇',
    'Nothing drafted yet',
    'This is where your team lives once the draft has run. Until then, the Planner '
    + 'is where you work out who you want.',
  );
}

/* For anyone the draft board cannot identify — a commissioner who drafted for
 * the table, or someone opening a league they are not in. */
function chooser() {
  const select = h('select', { class: 'compact', 'aria-label': 'Team to plan for' }, [
    h('option', { value: '', text: 'Choose a team…' }),
    ...options.map((team) =>
      h('option', { value: String(team.id), text: `${team.name} · ${team.picks.length} drafted` })),
  ]);
  select.addEventListener('change', () => {
    if (!select.value) return;
    rememberTeam(ctx.season().id, Number(select.value));
    refresh();
  });

  return h('div', { class: 'roster-offer' }, [
    h('span', { class: 'roster-offer-text', text:
      'You are not down as a player in this league. Pick whose roster to build:' }),
    select,
  ]);
}

// ----------------------------------------------------------- the lineups

/* Give every drafted Pokémon a slot, keeping the sets already built.
 *
 * Appended in pick order, and only ever appended: a Pokémon you own cannot be
 * taken off a lineup, only left out of it. That is what stops this from
 * fighting the player — a remove button here would put the slot back on the
 * next render.
 */
function adoptDraftedPicks(lineup) {
  const slots = lineup.slots || [];
  const owned = new Set(roster.picks);
  const have = new Set(slots.map((slot) => slot.api_name));
  const missing = roster.picks.filter((apiName) => !have.has(apiName));

  /* A Pokémon on the sheet that you do not own — a lineup carried over from
   * another team, or a trade that went the other way — cannot be brought, so
   * it comes out of the lineup rather than being deleted behind your back. */
  const benched = slots.map((slot) =>
    !owned.has(slot.api_name) && plans.isIncluded(slot) ? { ...slot, include: false } : slot);

  const changed = missing.length || benched.some((slot, index) => slot !== slots[index]);
  if (!changed) return;

  host.save(lineup, {
    /* Benched on arrival. Every Pokémon you drafted goes onto the sheet, but
     * none of them are in the lineup until you say so — a new lineup that
     * pre-selects a whole roster is one you have to empty before you can
     * fill it, and with more than six drafted it starts over the limit.
     *
     * `include: false` is set explicitly rather than by changing
     * `plans.emptySlot`, which the Planner shares and where selecting by
     * default is the right behaviour. */
    slots: [
      ...benched,
      ...missing.map((apiName) => ({ ...plans.emptySlot(apiName), include: false })),
    ],
  });
  // save() wrote through plans.js; re-read so the caller draws what was saved.
  Object.assign(lineup, plans.plansFor(ctx.season().id, plans.LINEUP)
    .find((row) => row.id === lineup.id) || lineup);
}

/* Sort plans that are really lineups onto this tab, once per season.
 *
 * There was a version of this app where the Planner did both jobs: it adopted
 * your drafted Pokémon into whatever plan was open and let you bench them. The
 * sheets people built then are lineups by every measure except the label, and
 * leaving them on the Planner would mean the work done in that week is on the
 * wrong tab forever.
 *
 * Two fingerprints, both of which only that version produced:
 *   * the sheet holds every Pokémon you drafted, because it adopted them all
 *   * or some slot is explicitly benched, which only the lineup UI ever wrote
 *
 * It runs once per season and records that it did. A plan you write tomorrow
 * that happens to match your roster is yours to keep where you put it —
 * reclassifying someone's work on a schedule would be worse than never doing
 * it at all.
 */
function claimLineups(seasonId) {
  let done;
  try {
    done = JSON.parse(localStorage.getItem(MIGRATED_KEY) || '{}');
  } catch {
    done = {};
  }
  if (done[String(seasonId)]) return 0;
  /* Only once the plans are actually here. Claiming against a cache that has
   * not loaded would find nothing, record itself as done, and strand the very
   * sheets it exists to move. */
  if (!plans.isLoaded(seasonId) || plans.loadError(seasonId)) return 0;

  const owned = new Set(roster.picks);
  const moved = plans.plansFor(seasonId, plans.PLAN).filter((plan) => {
    const slots = plan.slots || [];
    if (!slots.length) return false;
    const names = new Set(slots.map((slot) => slot.api_name));
    const hasWholeRoster = roster.picks.every((apiName) => names.has(apiName));
    const wasBenched = slots.some((slot) => slot.include === false);
    return hasWholeRoster || (wasBenched && slots.some((slot) => owned.has(slot.api_name)));
  });

  for (const plan of moved) plans.updatePlan(seasonId, plan.id, { kind: plans.LINEUP });

  try {
    localStorage.setItem(MIGRATED_KEY, JSON.stringify({ ...done, [String(seasonId)]: true }));
  } catch {
    /* No storage: it will run again next load, which is harmless — the plans
     * it would move are already lineups and no longer match. */
  }
  return moved.length;
}

function lineupRow(lineup, season, isActive) {
  const bringing = (lineup.slots || []).filter(plans.isIncluded).length;
  return h('div', { class: isActive ? 'plan-row is-active' : 'plan-row' }, [
    h('button', {
      class: 'plan-row-open',
      type: 'button',
      onclick: () => {
        plans.setActive(season.id, lineup.id, plans.LINEUP);
        render();
      },
    }, [
      h('span', { class: 'plan-row-name', text: lineup.name }),
      h('span', { class: 'plan-row-meta',
                  text: `${bringing}/${paste.BATTLE_TEAM_SIZE} bringing` }),
    ]),
    h('button', {
      class: 'plan-row-action',
      type: 'button',
      title: 'Duplicate',
      text: '⧉',
      onclick: () => {
        plans.duplicatePlan(season.id, lineup.id);
        render();
      },
    }),
    h('button', {
      class: 'plan-row-action is-danger',
      type: 'button',
      title: 'Delete',
      text: '✕',
      onclick: () => {
        if (!window.confirm(`Delete “${lineup.name}”? The sets on it go too.`)) return;
        plans.deletePlan(season.id, lineup.id);
        render();
      },
    }),
  ]);
}

function lineupView(lineup, season) {
  const problems = paste.checkPlan(lineup, {
    season,
    entryFor: ctx.entryFor,
    rules: ctx.rules(),
    roster: roster.picks,
  });

  const name = h('input', {
    class: 'plan-name',
    value: lineup.name,
    'aria-label': 'Lineup name',
    maxlength: '60',
    onchange: (event) => {
      host.save(lineup, { name: event.target.value.trim() || 'Untitled lineup' });
      render();
    },
  });

  return h('div', { class: 'plan' }, [
    h('div', { class: 'plan-head' }, [name]),
    banner(),
    bringingMeter(lineup),
    problems.length ? sheet.problemsView(problems) : null,
    slotsView(lineup),
    sheet.notesView(host, lineup,
      'What this lineup is for — the matchup, the lead, what you are afraid of.'),
    sheet.exportView(host, lineup, {
      title: `${roster.teamName} — ${lineup.name} — ${season.name}`,
      author: roster.teamName,
      emptyText: '# Nobody in the lineup yet — tick who you are bringing.',
      note: 'the Pokémon you are bringing — imports into the teambuilder as-is',
    }),
  ]);
}

/* Whose team this is and what it cost. Stated once at the top because every
 * number below it — no budget bar, no add button — only makes sense if you
 * know the roster is settled. */
function banner() {
  return h('div', { class: 'roster-banner' }, [
    roster.chosen ? teamChooser() : h('span', { class: 'roster-team', text: roster.teamName }),
    h('span', { class: 'roster-fact', text: `${roster.picks.length} drafted` }),
    h('span', { class: 'roster-fact', text: `${roster.spent} of ${roster.budget} pts spent` }),
    h('span', { class: 'roster-note', text: roster.status === 'complete'
      ? `${roster.chosen ? 'This' : 'Your'} roster is final. Build sets, then tick who is coming.`
      : 'The draft is still running — anything drafted from here joins every lineup.' }),
    roster.chosen
      ? h('button', {
          class: 'roster-clear',
          type: 'button',
          text: 'pick another team',
          onclick: () => {
            rememberTeam(ctx.season().id, null);
            refresh();
          },
        })
      : null,
  ]);
}

/* A commissioner builds for whoever they are helping this week, so the team is
 * a control rather than a label. */
function teamChooser() {
  const select = h('select', { class: 'compact roster-select', 'aria-label': 'Team' },
    options.map((team) => h('option', { value: String(team.id), text: team.name })));
  select.value = String(roster.teamId);
  select.addEventListener('change', () => {
    rememberTeam(ctx.season().id, Number(select.value));
    refresh();
  });
  return select;
}

/* One bar, not two. The points are spent and the roster is fixed, so the only
 * thing left to be over or under is how many you are bringing. */
function bringingMeter(lineup) {
  const slots = lineup.slots || [];
  const bringing = slots.filter(plans.isIncluded).length;
  const over = bringing > paste.BATTLE_TEAM_SIZE;

  return h('div', { class: 'meter-row' }, [
    h('div', { class: 'meter' }, [
      h('div', { class: 'meter-label' }, [
        h('span', { class: over ? 'meter-value is-over' : 'meter-value', text: `${bringing}` }),
        h('span', { class: 'meter-of', text: `/ ${paste.BATTLE_TEAM_SIZE} in the lineup` }),
        h('span', { class: 'meter-left', text: `${slots.length} on the roster sheet` }),
      ]),
      h('div', { class: 'meter-track' }, [
        h('div', {
          class: over ? 'meter-fill is-over' : 'meter-fill',
          style: `width:${Math.min(100, (bringing / paste.BATTLE_TEAM_SIZE) * 100)}%`,
        }),
      ]),
    ]),
  ]);
}

/* The roster as a table, not a wall of cards.
 *
 * What a roster is read for is short: what did each Pokemon cost, is it in the
 * lineup, and what does it look like. A card per Pokemon spreads those four
 * facts over a block the height of a paragraph, so eight of them do not fit on
 * a screen together and the costs never line up under each other. A row each
 * puts the whole team in one glance with the costs in a column that adds up.
 *
 * The set editor is still a click away — the row opens it — so nothing is
 * lost by shrinking, only by scrolling less.
 */
function slotsView(lineup) {
  const slots = lineup.slots || [];
  const costOf = (slot) => roster.costOf.get(slot.api_name)
    ?? ctx.entryFor(slot.api_name)?.cost ?? 0;

  const rows = slots.map((slot, index) => {
    const entry = ctx.entryFor(slot.api_name);
    const included = plans.isIncluded(slot);
    const mine = owns(slot.api_name);

    return h('li', {
      class: [
        'roster-row',
        included ? 'is-in' : 'is-benched',
        mine ? '' : 'is-unowned',
      ].filter(Boolean).join(' '),
    }, [
      mine
        ? benchButton(lineup, slot, index)
        : sheet.removeButton(host, lineup, index, 'Not on your roster — remove'),
      // What it went for, not what it lists at: the pool can be repriced
      // after a draft and the pick still cost what it cost.
      h('span', { class: 'roster-cost', text: String(costOf(slot)) }),
      h('button', {
        class: 'roster-open',
        type: 'button',
        title: `Edit ${entry?.display_name || slot.api_name}`,
        onclick: () => sheet.openSetEditor(host, lineup, index),
      }, [
        spriteFor(entry || {}, 'roster-sprite'),
        h('span', { class: 'roster-name',
          text: slot.nickname || entry?.display_name || poke.prettify(slot.api_name) }),
        h('span', { class: 'roster-types' },
          (entry?.types || []).map((type) => typePip(type, true))),
      ]),
    ]);
  });

  const spent = slots.reduce((sum, slot) => sum + costOf(slot), 0);

  return h('div', { class: 'roster-table' }, [
    h('div', { class: 'roster-head' }, [
      h('span', { class: 'roster-cost', text: 'Cost' }),
      h('span', { class: 'roster-name', text: 'Pokémon' }),
    ]),
    h('ol', { class: 'roster-rows' }, rows),
    h('div', { class: 'roster-total' }, [
      h('span', { class: 'roster-cost', text: String(spent) }),
      h('span', { class: 'roster-name', text: `Total · ${slots.length} drafted` }),
    ]),
  ]);
}

function benchButton(lineup, slot, index) {
  const included = plans.isIncluded(slot);
  return h('button', {
    class: included ? 'slot-bench is-in' : 'slot-bench',
    type: 'button',
    title: included ? 'Bench — leave out of the paste' : 'Bring — add to the paste',
    text: included ? '✓' : '+',
    onclick: () => {
      const next = [...lineup.slots];
      next[index] = { ...slot, include: !included };
      host.save(lineup, { slots: next });
      render();
    },
  });
}
