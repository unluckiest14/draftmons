/* The Planner tab: who you want, before the draft decides.
 *
 * The roster is hypothetical here. You take the season's pool, work out which
 * Pokémon fit inside the budget together, build sets on them and leave with a
 * paste — all of it a wishlist, because none of it is yours yet.
 *
 * Once the draft has run, the Roster tab is where the work goes: that one
 * starts from what you actually drafted, which is a different question and
 * was confusing to answer in one screen. This tab does not change after the
 * draft; a plan is still a plan, and looking back at what you wanted is worth
 * keeping.
 *
 * Plans are stored as references into the pool: a slot holds an `api_name` and
 * the set built on top of it, never a copy of the Pokémon's cost or stats. So
 * when the commissioner reprices the pool, every plan reprices with it — which
 * is the behaviour a planner needs, since the whole question it answers is
 * "does this fit in the budget".
 *
 * The slot card, the set editor and the paste are shared with the Roster tab
 * and live in teamsheet.js. What is left here is the part that is only true
 * before a draft: the budget, and a picker over the whole pool.
 *
 * There is no save button, because a scratchpad with a save button is a
 * scratchpad you lose work in.
 */

import { h, $, clear, typePip, placeholder, spriteFor, flashLabel } from './dom.js';
import * as plans from './plans.js';
import * as paste from './pokepaste.js';
import * as draft from './draft.js';
import * as queue from './queue.js';
import * as sheet from './teamsheet.js';

/* Set by init(). The planner reads the season, pool and format from the board
 * rather than fetching them again — one pool request per page is the point. */
let ctx = null;

const ui = {};

export function init(context) {
  ctx = context;
  ui.root = $('#planner');
  ui.list = $('#plan-items');
  ui.main = $('#plan-main');
  ui.newButton = $('#plan-new');
  ui.privacy = $('#plan-privacy');
  ui.exportButton = $('#plan-export');
  ui.importInput = $('#plan-import');
  ui.drawer = $('#plan-drawer');
  ui.scrim = $('#scrim');

  ui.newButton.addEventListener('click', () => {
    const season = ctx.season();
    if (!season) return;
    plans.createPlan(season.id, '', plans.PLAN);
    render();
  });

  ui.exportButton.addEventListener('click', () => downloadBackup(ui.exportButton));
  ui.importInput.addEventListener('change', restoreBackup);

  /* A create or a debounced save settling is the only thing that changes the
   * screen without a user action, so it is the only thing that needs this. */
  plans.onChange(() => {
    if (ui.root && !ui.root.hidden) renderStorageNote();
  });

  /* A plan edited and then abandoned mid-debounce would otherwise lose its
   * last keystrokes. Both events fire on a tab close or a navigation; either
   * one flushing is enough. */
  for (const event of ['pagehide', 'visibilitychange']) {
    window.addEventListener(event, () => {
      const season = ctx.season();
      if (season && document.visibilityState !== 'visible') plans.flushAll(season.id);
    });
  }

  renderStorageNote();
}

/* Where plans are being kept, and whether the last write landed.
 *
 * Worth a permanent line rather than a transient toast: "private" means
 * something different in each mode, and a player who has joined should be able
 * to see that their work is on the server rather than trust it.
 */
function renderStorageNote() {
  const season = ctx.season();
  clear(ui.privacy);
  ui.privacy.classList.remove('is-error');

  if (!season) return;
  const state = plans.saveState(season.id);

  if (state.mode === 'local') {
    if (!plans.storageWorks()) {
      ui.privacy.classList.add('is-error');
      ui.privacy.textContent =
        'This browser is blocking local storage, so plans cannot be saved. A private window usually causes this.';
      return;
    }
    ui.privacy.append(
      h('strong', { text: 'Saved in this browser only. ' }),
      document.createTextNode(
        'Join the league on the League tab to store plans against your team, so they '
        + 'follow you to another device.',
      ),
    );
    return;
  }

  const label = {
    saving: 'Saving…',
    saved: 'Saved to your team.',
    error: 'Could not save.',
  }[state.status];

  if (state.status === 'error') ui.privacy.classList.add('is-error');
  ui.privacy.append(
    h('strong', { text: `${label} ` }),
    document.createTextNode(
      state.status === 'error'
        ? `${state.message} Your changes are still on this screen — they will retry on the next edit.`
        : 'Private to you: no other player and not the commissioner can read these.',
    ),
  );

  const stranded = plans.localPlansFor(season.id);
  if (stranded.length) ui.privacy.append(uploadPrompt(season, stranded));
}

/* Plans made before joining are stranded in localStorage, where the planner no
 * longer reads from. Silently ignoring them looks exactly like data loss, so
 * they get an offer rather than a migration: copied up, never moved, and the
 * local copies are only cleared once the upload has actually succeeded.
 */
function uploadPrompt(season, stranded) {
  const button = h('button', {
    class: 'button',
    type: 'button',
    text: `Upload ${stranded.length} plan${stranded.length === 1 ? '' : 's'}`,
  });

  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Uploading…';
    try {
      const uploaded = await plans.uploadLocal(season.id);
      plans.clearLocal(season.id);
      render();
      alert(`${uploaded} plan${uploaded === 1 ? '' : 's'} moved to your team.`);
    } catch (problem) {
      button.disabled = false;
      button.textContent = `Upload ${stranded.length} plan${stranded.length === 1 ? '' : 's'}`;
      alert(`Could not upload: ${problem.message}\n\nYour local plans are untouched.`);
    }
  });

  return h('span', { class: 'upload-prompt' }, [
    h('span', {
      text: `${stranded.length} plan${stranded.length === 1 ? '' : 's'} from before you joined `
        + 'are still only in this browser.',
    }),
    button,
  ]);
}

/** Called by app.js whenever the season, pool or format changes. */
/** Called by app.js whenever the season, pool or format changes. */
export function refresh() {
  if (!ui.root || ui.root.hidden) return;
  render();

  /* Plans live on the server once you have joined, and nothing else fetches
   * them — without this the tab shows whatever localStorage happens to hold,
   * which in server mode is nothing. Painted first, then repainted when the
   * real list lands, so a switch is never blank. */
  const season = ctx.season();
  if (!season || plans.isLoaded(season.id)) return;
  plans.load(season.id).then((changed) => {
    if (changed && ui.root && !ui.root.hidden) render();
    renderStorageNote();
  });
}

export function closeOverlays() {
  sheet.closeOverlays();
}

/* What the shared team sheet needs from this tab: where a plan is stored, and
 * which tab to redraw once it changes. */
const host = {
  season: () => ctx.season(),
  entryFor: (apiName) => ctx.entryFor(apiName),
  rules: () => ctx.rules(),
  formatKey: () => ctx.formatKey(),
  save: (plan, changes) => plans.updatePlan(ctx.season().id, plan.id, changes),
  repaint: () => render(),
};

// ------------------------------------------------------------- plan list

function activePlan() {
  const season = ctx.season();
  if (!season) return null;
  const id = plans.activeIdFor(season.id, plans.PLAN);
  return plans.plansFor(season.id, plans.PLAN).find((plan) => plan.id === id) || null;
}

function render() {
  renderStorageNote();
  const season = ctx.season();
  clear(ui.list);
  clear(ui.main);

  if (!season) {
    ui.main.append(
      placeholder('◇', 'No season selected', 'Pick a season in the header to start planning.'),
    );
    return;
  }

  const all = plans.plansFor(season.id, plans.PLAN);
  const active = activePlan();

  for (const plan of all) {
    ui.list.append(planRow(plan, season, plan.id === active?.id));
  }
  if (!all.length) {
    ui.list.append(
      h('p', { class: 'plan-empty', text: 'No plans yet. “+ New” starts one.' }),
    );
  }

  /* Above the plan, and outside it: the queue belongs to the season and the
   * draft, not to whichever plan happens to be open. It has to stay visible
   * when you close a plan, or a queue that is still driving your draft would
   * vanish from the only screen that can edit it. */
  const queued = queueView(season);
  if (queued) ui.main.append(queued);

  if (!active) {
    ui.main.append(
      placeholder(
        '◇',
        'No plan open',
        'Create a plan, then add Pokémon from this season’s pool to it.',
      ),
    );
    return;
  }
  ui.main.append(planView(active, season));
}

function planRow(plan, season, isActive) {
  const spend = totalCost(plan);
  const row = h('div', { class: isActive ? 'plan-row is-active' : 'plan-row' }, [
    h('button', {
      class: 'plan-row-open',
      type: 'button',
      onclick: () => {
        plans.setActive(season.id, plan.id, plans.PLAN);
        render();
      },
    }, [
      h('span', { class: 'plan-row-name', text: plan.name }),
      h('span', {
        class: 'plan-row-meta',
        text: `${(plan.slots || []).length}/${season.roster_size} · ${spend}/${season.budget} pts`,
      }),
    ]),
    h('button', {
      class: 'plan-row-action',
      type: 'button',
      title: 'Duplicate',
      text: '⧉',
      onclick: () => {
        plans.duplicatePlan(season.id, plan.id);
        render();
      },
    }),
    h('button', {
      class: 'plan-row-action is-danger',
      type: 'button',
      title: 'Delete',
      text: '✕',
      onclick: () => {
        if (!confirm(`Delete “${plan.name}”? This cannot be undone.`)) return;
        plans.deletePlan(season.id, plan.id);
        render();
      },
    }),
  ]);
  return row;
}

// ------------------------------------------------------------- plan view

const totalCost = (plan) =>
  (plan.slots || []).reduce((sum, slot) => sum + (ctx.entryFor(slot.api_name)?.cost || 0), 0);

function save(plan, changes) {
  const season = ctx.season();
  plans.updatePlan(season.id, plan.id, changes);
}

function planView(plan, season) {
  const spend = totalCost(plan);
  const problems = paste.checkPlan(plan, {
    season,
    entryFor: ctx.entryFor,
    rules: ctx.rules(),
  });

  const name = h('input', {
    class: 'plan-name',
    value: plan.name,
    'aria-label': 'Plan name',
    maxlength: '60',
    onchange: (event) => {
      save(plan, { name: event.target.value.trim() || 'Untitled plan' });
      render();
    },
  });

  const author = h('input', {
    class: 'plan-author',
    value: plan.author || '',
    placeholder: 'Your team name (optional)',
    'aria-label': 'Author',
    maxlength: '60',
    list: 'league-teams',
    onchange: (event) => save(plan, { author: event.target.value.trim() }),
  });

  return h('div', { class: 'plan' }, [
    h('div', { class: 'plan-head' }, [name, author]),
    meterView(plan, season, spend),
    problems.length ? sheet.problemsView(problems) : null,
    slotsView(plan, season),
    sheet.notesView(host, plan),
    sheet.exportView(host, plan, {
      title: `${plan.name} — ${season.name}`,
      note: 'imports into Showdown’s teambuilder as-is',
    }),
  ]);
}

/* Two bars, because a plan can fail either way independently: eight Pokémon
 * that cost too much, or five that cost nothing like enough. */
function meterView(plan, season, spend) {
  const count = (plan.slots || []).length;
  const overBudget = spend > season.budget;
  const overRoster = count > season.roster_size;

  const bar = (used, limit, over) =>
    h('div', { class: 'meter-track' }, [
      h('div', {
        class: over ? 'meter-fill is-over' : 'meter-fill',
        style: `width:${Math.min(100, (used / Math.max(1, limit)) * 100)}%`,
      }),
    ]);

  return h('div', { class: 'meter-row' }, [
    h('div', { class: 'meter' }, [
      h('div', { class: 'meter-label' }, [
        h('span', { class: overBudget ? 'meter-value is-over' : 'meter-value', text: `${spend}` }),
        h('span', { class: 'meter-of', text: `/ ${season.budget} points` }),
        h('span', {
          class: 'meter-left',
          text: overBudget
            ? `${spend - season.budget} over`
            : `${season.budget - spend} left`,
        }),
      ]),
      bar(spend, season.budget, overBudget),
    ]),
    h('div', { class: 'meter' }, [
      h('div', { class: 'meter-label' }, [
        h('span', { class: overRoster ? 'meter-value is-over' : 'meter-value', text: `${count}` }),
        h('span', { class: 'meter-of', text: `/ ${season.roster_size} Pokémon` }),
      ]),
      bar(count, season.roster_size, overRoster),
    ]),
  ]);
}

function slotsView(plan, season) {
  const slots = plan.slots || [];
  const cards = slots.map((slot, index) => sheet.slotCard(host, plan, index, {
    corner: (row, at) => sheet.removeButton(host, plan, at),
    footer: (row) => queueButton(row.api_name),
  }));

  /* One trailing add button rather than a fixed grid of empty slots: rosters
   * are 8 by default but a plan mid-build is not wrong for being short. */
  if (slots.length < season.roster_size) {
    cards.push(
      h('button', {
        class: 'slot-add',
        type: 'button',
        onclick: () => openPicker(plan),
      }, [
        h('span', { class: 'slot-add-plus', text: '+' }),
        h('span', { text: 'Add Pokémon' }),
      ]),
    );
  }

  return h('div', { class: 'slots' }, cards);
}

/* Queueing, from the plan you already built.
 *
 * The point of a plan is that you have decided most of this already. Queueing
 * carries that decision into the draft, where the picker offers the top of the
 * queue instead of making you search for a Pokémon you named an hour ago.
 */
function queueButton(apiName) {
  const season = ctx.season();
  const position = queue.positionOf(season.id, apiName);
  return h('button', {
    class: position ? 'slot-queue is-queued' : 'slot-queue',
    type: 'button',
    title: position
      ? `Queued #${position} for the draft — click to take it out`
      : 'Queue this for the draft',
    onclick: () => {
      queue.toggle(season.id, apiName);
      render();
    },
  }, [
    h('span', { class: 'slot-queue-mark', text: position ? String(position) : '＋' }),
    h('span', { text: position ? 'queued' : 'queue for draft' }),
  ]);
}

/* The queue itself, above the plan. Order is the whole point, so it is
 * reorderable — the draft offers these top down. */
function queueView(season) {
  const names = queue.listFor(season.id);
  if (!names.length) return null;

  return h('div', { class: 'queue-panel' }, [
    h('div', { class: 'queue-head' }, [
      h('span', { class: 'field-label', text: 'Draft queue' }),
      h('span', { class: 'queue-note', text:
        'The draft offers these in order when it is your turn. Nothing is picked '
        + 'without you confirming it.' }),
      h('button', {
        class: 'roster-clear',
        type: 'button',
        text: 'clear',
        onclick: () => {
          queue.clear(season.id);
          render();
        },
      }),
    ]),
    h('ol', { class: 'queue-list' }, names.map((apiName, index) => {
      const entry = ctx.entryFor(apiName);
      return h('li', { class: 'queue-row' }, [
        h('span', { class: 'queue-rank', text: String(index + 1) }),
        spriteFor(entry || {}, 'queue-sprite'),
        h('span', { class: 'queue-name',
                    text: entry?.display_name || apiName }),
        entry?.cost ? h('span', { class: 'queue-cost', text: String(entry.cost) }) : null,
        h('div', { class: 'queue-actions' }, [
          h('button', {
            class: 'queue-move', type: 'button', text: '↑', title: 'Move up',
            disabled: index === 0,
            onclick: () => { queue.move(season.id, apiName, -1); render(); },
          }),
          h('button', {
            class: 'queue-move', type: 'button', text: '↓', title: 'Move down',
            disabled: index === names.length - 1,
            onclick: () => { queue.move(season.id, apiName, 1); render(); },
          }),
          h('button', {
            class: 'queue-move is-danger', type: 'button', text: '✕', title: 'Unqueue',
            onclick: () => { queue.remove(season.id, apiName); render(); },
          }),
        ]),
      ]);
    })),
  ]);
}

function downloadBackup(button) {
  sheet.downloadText(`draftmons-plans-${new Date().toISOString().slice(0, 10)}.json`, plans.exportAll());
  flashLabel(button, 'Saved');
}

async function restoreBackup(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const added = await plans.importAll(await file.text(), {
      merge: true,
      seasonId: ctx.season()?.id ?? null,
    });
    alert(`${added} plan${added === 1 ? '' : 's'} imported.`);
    render();
  } catch (error) {
    alert(`Could not import that file: ${error.message}`);
  } finally {
    // Reset, so choosing the same file twice fires change twice.
    event.target.value = '';
  }
}

/* Choosing a Pokémon, out of this league's pool and nothing else.
 *
 * The pool is the one the board already loaded for the selected season, so it
 * is priced with that league's costs and holds exactly what that league
 * drafts from — a plan is only meaningful against those.
 *
 * Three things the draft's own picker taught this one, after the first live
 * session: search matches types as well as names, because "grass" is what
 * people type when they want a grass type; the list says how much of it you
 * are looking at; and anything already drafted in this league is marked, so a
 * plan made mid-draft does not quietly fill up with Pokémon somebody else
 * already owns.
 */
const PICKER_LIMIT = 250;

function openPicker(plan) {
  const season = ctx.season();
  const onPlan = new Set((plan.slots || []).map((slot) => slot.api_name));
  const remaining = season.budget - totalCost(plan);
  // Empty unless a draft has actually run in this league.
  const drafted = draft.turnState().taken;

  const search = h('input', {
    type: 'search',
    placeholder: 'Search by name or type…',
    autocomplete: 'off',
    'aria-label': 'Search the pool',
  });
  const affordableOnly = h('input', { type: 'checkbox' });
  const hideDrafted = h('input', { type: 'checkbox' });
  const count = h('p', { class: 'picker-more' });
  const list = h('div', { class: 'picker-list' });

  const paint = () => {
    const query = search.value.trim().toLowerCase();
    clear(list);
    clear(count);

    const rows = ctx
      .pool()
      .filter((entry) => !entry.banned)
      .filter((entry) => !query
        || entry.display_name.toLowerCase().includes(query)
        || (entry.types || []).some((type) => type.toLowerCase().includes(query)))
      .filter((entry) => !affordableOnly.checked || (entry.cost || 0) <= remaining)
      .filter((entry) => !hideDrafted.checked || !drafted.has(entry.api_name))
      .sort((a, b) => (b.cost || 0) - (a.cost || 0) || a.display_name.localeCompare(b.display_name));

    if (!rows.length) {
      list.append(placeholder('∅', 'Nothing matches', 'Try another name or a type.'));
      return;
    }

    for (const entry of rows.slice(0, PICKER_LIMIT)) {
      const already = onPlan.has(entry.api_name);
      const gone = drafted.has(entry.api_name);
      list.append(
        h('button', {
          class: [
            'picker-row',
            ...(already ? ['is-taken'] : []),
            ...(gone && !already ? ['is-drafted'] : []),
          ].join(' '),
          type: 'button',
          // Only what is already on this plan is refused. A Pokémon somebody
          // else drafted is still worth planning around — a trade, or the
          // next season — so it is marked, not withheld.
          disabled: already,
          onclick: () => {
            save(plan, { slots: [...(plan.slots || []), plans.emptySlot(entry.api_name)] });
            closeOverlays();
            ui.scrim.hidden = true;
            render();
          },
        }, [
          spriteFor(entry, 'picker-sprite'),
          h('span', { class: 'picker-name', text: entry.display_name }),
          h('span', { class: 'picker-types' }, (entry.types || []).map((type) => typePip(type))),
          h('span', {
            class: (entry.cost || 0) > remaining ? 'picker-cost is-over' : 'picker-cost',
            text: entry.cost ? String(entry.cost) : '—',
          }),
          h('span', { class: 'picker-flag',
                      text: already ? 'on plan' : (gone ? 'drafted' : '') }),
        ]),
      );
    }

    const shown = Math.min(rows.length, PICKER_LIMIT);
    count.textContent = rows.length > PICKER_LIMIT
      ? `Showing ${shown} of ${rows.length} — search to narrow it down.`
      : `${rows.length} in this league's pool`;
  };

  search.addEventListener('input', paint);
  affordableOnly.addEventListener('change', paint);
  hideDrafted.addEventListener('change', paint);
  paint();

  sheet.openDrawer([
    sheet.drawerHead(
      'Add a Pokémon',
      `${season.name} · ${remaining} points left in ${plan.name}`,
    ),
    h('div', { class: 'drawer-body' }, [
      h('div', { class: 'picker-controls' }, [
        search,
        h('label', { class: 'check' }, [affordableOnly, h('span', { text: 'Only what I can afford' })]),
        drafted.size
          ? h('label', { class: 'check' }, [hideDrafted, h('span', { text: 'Hide drafted' })])
          : null,
      ]),
      count,
      list,
    ]),
  ]);
  search.focus();
}
