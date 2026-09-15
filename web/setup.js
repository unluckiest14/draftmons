/* Creating a draft, without opening /docs.
 *
 * Everything a commissioner needs to go from nothing to a draft other people
 * can join, in the order they need it:
 *
 *   1  the league itself — name, points per player, roster size
 *   2  the pool          — a Showdown format, or their own list of Pokemon
 *   3  the prices        — which is the step that actually takes an evening
 *   4  the join code     — and the admin token, shown once
 *
 * Step 3 is why this is a wizard rather than a form. A Gen 9 OU pool is 771
 * Pokemon and pricing them one at a time is not a thing anyone will do, so
 * selection is multiple and bulk: filter to what you mean, select all of it,
 * type a number once. Everything else here is ordinary forms around that.
 *
 * The admin token is claimed here on purpose. POST /invite is claim-on-first-
 * use, so calling it immediately after creating the season makes whoever
 * created it the owner, rather than leaving the season unclaimed for whoever
 * opens the League tab first.
 */

import { api } from './api.js';
import { h, $, clear, spriteFor, typePip, copyText, flashLabel } from './dom.js';
import * as session from './session.js';

let ctx = null;
let ui = {};

/* What the wizard has built so far. Reset on every open, because a half
 * finished run must not leak into the next one. */
let draft = null;

const STEPS = ['League', 'Pool', 'Points', 'Share'];

export function init(context) {
  ctx = context;
  ui = { root: $('#setup'), body: $('#setup-body'), steps: $('#setup-steps') };

  $('#setup-open').addEventListener('click', open);
  $('#setup-close').addEventListener('click', close);
  ui.root.addEventListener('click', (event) => {
    if (event.target === ui.root) close();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !ui.root.hidden) close();
  });
}

export function open() {
  draft = { step: 0, season: null, invite: null, pool: [], selected: new Set(),
            filter: '', unpricedOnly: false };
  ui.root.hidden = false;
  render();
}

export function close() {
  ui.root.hidden = true;
  // A season created here exists whether or not the wizard was finished, so
  // the app reloads either way rather than pretending nothing happened.
  if (draft?.season) ctx.onCreated(draft.season);
  draft = null;
}

/* --------------------------------------------------------------- shell */

function render() {
  clear(ui.steps);
  STEPS.forEach((label, index) => {
    ui.steps.append(h('span', {
      class: ['setup-step', index === draft.step ? 'is-active' : '',
              index < draft.step ? 'is-done' : ''].filter(Boolean).join(' '),
      text: `${index + 1}. ${label}`,
    }));
  });

  clear(ui.body);
  ui.body.append([stepLeague, stepPool, stepPoints, stepShare][draft.step]());
}

function fail(message) {
  const existing = ui.body.querySelector('.setup-error');
  if (existing) existing.remove();
  if (message) ui.body.prepend(h('div', { class: 'setup-error', role: 'alert', text: message }));
}

/** Run an async action behind a button, so a slow call cannot be double-fired. */
async function busy(button, label, call) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try {
    await call();
    fail(null);
  } catch (error) {
    fail(error.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ------------------------------------------------------- 1. the league */

function stepLeague() {
  const name = h('input', { type: 'text', placeholder: 'Spring Cup', maxlength: '80' });
  const budget = h('input', { type: 'number', value: '100', min: '1', max: '10000' });
  const roster = h('input', { type: 'number', value: '8', min: '1', max: '24' });

  const create = h('button', { class: 'button is-primary', type: 'button', text: 'Create league' });
  create.addEventListener('click', () => busy(create, 'Creating…', async () => {
    if (!name.value.trim()) throw new Error('Give the league a name.');

    draft.season = await api('/seasons', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name.value.trim(),
        budget: Number(budget.value),
        roster_size: Number(roster.value),
      }),
    });

    /* Claim it straight away. POST /invite is claim-on-first-use, so doing it
     * now is what makes the person who created the league its commissioner
     * instead of whoever opens the League tab first. */
    draft.invite = await api(`/seasons/${draft.season.id}/invite`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (draft.invite.admin_token) {
      session.remember(draft.season.id, { admin: draft.invite.admin_token });
    }
    draft.step = 1;
    render();
  }));

  return h('div', { class: 'setup-pane' }, [
    h('p', { class: 'setup-lead', text: 'A league is one draft: a pool, a set of teams, and a budget each.' }),
    field('League name', name),
    field('Points per player', budget, 'The budget each team drafts with.'),
    field('Roster size', roster, 'How many Pokémon each team drafts — this is the number of rounds.'),
    h('div', { class: 'setup-actions' }, [create]),
  ]);
}

/* --------------------------------------------------------- 2. the pool */

function stepPool() {
  const formats = ctx.formats();
  const select = h('select', {}, [
    h('option', { value: '', text: 'Choose a format…' }),
    ...['singles', 'doubles', 'natdex'].map((ladder) => h('optgroup',
      { label: { singles: 'Singles', doubles: 'Doubles', natdex: 'National Dex' }[ladder] },
      formats.filter((format) => format.ladder === ladder).map((format) =>
        h('option', { value: format.key, text: `${format.label} — ${format.species_count}` })))),
  ]);

  const build = h('button', { class: 'button is-primary', type: 'button', text: 'Load this format' });
  build.addEventListener('click', () => busy(build, 'Loading from PokéAPI…', async () => {
    if (!select.value) throw new Error('Pick a format first.');
    const result = await api(
      `/seasons/${draft.season.id}/pool/from-format/${select.value}`,
      { method: 'POST', headers: session.headers(draft.season.id, { admin: true }) },
    );
    await loadPool(`${result.label} — ${result.added} Pokémon`);
  }));

  const file = h('input', { type: 'file', accept: '.txt,.json,.csv,text/plain,application/json' });
  const drop = h('label', { class: 'setup-drop' }, [
    h('span', { class: 'setup-drop-icon', text: '⇩' }),
    h('span', { text: 'Drop a .txt or .json list, or click to choose' }),
    h('span', { class: 'setup-hint', text: 'One Pokémon per line, optionally "Name, cost". A Showdown export works too.' }),
    file,
  ]);

  const useFile = async (chosen) => {
    if (!chosen) return;
    // Read here rather than posting a multipart upload: the API takes the text,
    // which keeps it to one content type and needs no extra dependency.
    const content = await chosen.text();
    const result = await api(`/seasons/${draft.season.id}/pool/custom`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...session.headers(draft.season.id, { admin: true }),
      },
      body: JSON.stringify({ content, label: chosen.name, replace: true }),
    });
    let note = `${chosen.name} — ${result.added} Pokémon`;
    if (result.unmatched.length) {
      note += `. Not recognised: ${result.unmatched.map((row) => row.name).join(', ')}`;
    }
    await loadPool(note);
  };

  file.addEventListener('change', () => busy(build, 'Reading…', () => useFile(file.files[0])));
  for (const event of ['dragover', 'dragenter']) {
    drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.add('is-over'); });
  }
  for (const event of ['dragleave', 'drop']) {
    drop.addEventListener(event, () => drop.classList.remove('is-over'));
  }
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    busy(build, 'Reading…', () => useFile(e.dataTransfer.files[0]));
  });

  return h('div', { class: 'setup-pane' }, [
    h('p', { class: 'setup-lead', text: 'Where the draftable Pokémon come from. A format applies every Showdown ban for you.' }),
    field('Showdown format', select),
    h('div', { class: 'setup-actions' }, [build]),
    h('div', { class: 'setup-or', text: 'or bring your own list' }),
    drop,
  ]);
}

async function loadPool(note) {
  draft.pool = await api(`/seasons/${draft.season.id}/pool?include_banned=true&limit=2000`);
  draft.poolNote = note;
  draft.selected = new Set();
  draft.step = 2;
  render();
}

/* ------------------------------------------------------- 3. the points */

function shown() {
  const needle = draft.filter.trim().toLowerCase();
  return draft.pool.filter((entry) =>
    (!needle || entry.display_name.toLowerCase().includes(needle))
    && (!draft.unpricedOnly || !entry.cost));
}

function stepPoints() {
  const matching = shown();
  const amount = h('input', { type: 'number', value: '10', min: '0', max: '10000',
                              class: 'setup-amount' });

  const search = h('input', {
    type: 'search', value: draft.filter, placeholder: 'Filter by name…',
    oninput: (event) => { draft.filter = event.target.value; render(); },
  });
  const unpriced = h('label', { class: 'setup-check' }, [
    h('input', {
      type: 'checkbox', checked: draft.unpricedOnly ? true : null,
      onchange: (event) => { draft.unpricedOnly = event.target.checked; render(); },
    }),
    h('span', { text: 'Unpriced only' }),
  ]);

  /* Selecting the filtered set rather than only what is rendered is the whole
   * trick: filter to "everything unpriced", select it, type one number. A
   * grid capped at 300 rows would otherwise silently price only the first
   * 300 of 771. */
  const selectAll = h('button', {
    class: 'button', type: 'button', text: `Select all ${matching.length}`,
    onclick: () => {
      for (const entry of matching) draft.selected.add(entry.api_name);
      render();
    },
  });
  const clearSel = h('button', {
    class: 'button', type: 'button', text: 'Clear',
    onclick: () => { draft.selected = new Set(); render(); },
  });

  const apply = h('button', {
    class: 'button is-primary', type: 'button',
    text: `Set ${draft.selected.size} to…`,
  });
  apply.addEventListener('click', () => busy(apply, 'Saving…', async () => {
    if (!draft.selected.size) throw new Error('Select some Pokémon first.');
    const cost = Number(amount.value);
    const costs = Object.fromEntries([...draft.selected].map((name) => [name, cost]));
    // One request for the whole selection; the API takes a map.
    await api(`/seasons/${draft.season.id}/pool/costs`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...session.headers(draft.season.id, { admin: true }),
      },
      body: JSON.stringify({ costs }),
    });
    for (const entry of draft.pool) {
      if (draft.selected.has(entry.api_name)) entry.cost = cost;
    }
    draft.selected = new Set();
    render();
  }));

  const grid = h('div', { class: 'setup-grid' });
  for (const entry of matching.slice(0, 300)) {
    const picked = draft.selected.has(entry.api_name);
    grid.append(h('button', {
      class: `setup-mon${picked ? ' is-picked' : ''}${entry.cost ? '' : ' is-unpriced'}`,
      type: 'button',
      onclick: () => {
        if (picked) draft.selected.delete(entry.api_name);
        else draft.selected.add(entry.api_name);
        render();
      },
    }, [
      spriteFor(entry, 'setup-mon-sprite'),
      h('span', { class: 'setup-mon-name', text: entry.display_name }),
      h('span', { class: 'setup-mon-cost', text: entry.cost ? String(entry.cost) : '—' }),
    ]));
  }

  const unset = draft.pool.filter((entry) => !entry.cost).length;
  const next = h('button', {
    class: 'button is-primary', type: 'button',
    text: unset ? `Continue with ${unset} unpriced` : 'Continue',
    onclick: () => { draft.step = 3; render(); },
  });

  return h('div', { class: 'setup-pane' }, [
    h('p', { class: 'setup-lead', text: draft.poolNote || 'Price the pool.' }),
    h('p', { class: 'setup-hint', text: 'Click Pokémon to select them, then set them all to one value. Unpriced Pokémon cost 0, which makes them free to draft.' }),
    h('div', { class: 'setup-toolbar' }, [search, unpriced, selectAll, clearSel]),
    h('div', { class: 'setup-toolbar' }, [amount, apply]),
    grid,
    matching.length > 300
      ? h('p', { class: 'setup-hint', text: `Showing 300 of ${matching.length}. "Select all" covers every match, not just those shown.` })
      : null,
    h('div', { class: 'setup-actions' }, [next]),
  ]);
}

/* -------------------------------------------------------- 4. the share */

function stepShare() {
  const code = draft.invite?.join_code || '';
  const token = draft.invite?.admin_token || '';
  const link = `${location.origin}${location.pathname}?season=${draft.season.id}&tab=league`;

  const done = h('button', {
    class: 'button is-primary', type: 'button', text: 'Open the draft board',
    onclick: () => close(),
  });

  /* Taking a team here is what lets the commissioner save anything.
   *
   * Creating a league hands back an admin token and nothing else — no team,
   * and no player identity. Rosters and lineups are stored per player and
   * scoped by a player token, so a commissioner without one cannot save to
   * the server at all; the planner quietly falls back to this browser's
   * localStorage and the work is gone with the cache.
   *
   * Most commissioners play in their own league anyway, so this is offered
   * rather than required — a commissioner who is only running the draft for
   * a table does not need a team and should not be given one. */
  const teamName = h('input', {
    type: 'text', maxlength: '60', placeholder: 'Your team name',
  });
  const playNote = h('span', { class: 'setup-hint' });
  const play = h('button', { class: 'button', type: 'button', text: 'Take a team' });

  play.addEventListener('click', () => busy(play, 'Joining…', async () => {
    const wanted = teamName.value.trim();
    if (!wanted) throw new Error('Give your team a name first.');
    const joined = await api(`/join/${code}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ team_name: wanted }),
    });
    session.remember(draft.season.id, { player: joined.token, team: joined.team });
    clear(playNote);
    playNote.append(document.createTextNode(
      `You are ${joined.team.name}. Your rosters now save to the league, not just this browser.`,
    ));
    teamName.disabled = true;
    play.disabled = true;
  }));

  return h('div', { class: 'setup-pane' }, [
    h('p', { class: 'setup-lead', text: `${draft.season.name} is ready. Share the code and people can join.` }),
    copyRow('Join code', code, 'Players enter this on the League tab.'),
    copyRow('Invite link', link),
    token ? h('div', { class: 'setup-token' }, [
      h('strong', { text: 'Your commissioner token' }),
      // Said plainly because the backend genuinely cannot reissue it: there
      // are no accounts, so there is no email to reset against.
      h('p', { class: 'setup-hint', text: 'Saved in this browser. It is shown once and cannot be reissued — keep a copy, or you lose the ability to start the draft and undo picks.' }),
      h('code', { class: 'setup-code', text: token }),
      h('div', { class: 'setup-actions' }, [
        copyButton('Copy token', token),
        h('button', {
          class: 'button', type: 'button', text: 'Download',
          onclick: () => downloadToken(token, code),
        }),
      ]),
    ]) : null,
    h('div', { class: 'setup-field' }, [
      h('span', { class: 'field-label', text: 'Playing in it yourself?' }),
      h('div', { class: 'setup-copyrow' }, [teamName, play]),
      h('span', { class: 'setup-hint', text: 'Running a league does not give you a team. Take one if you are drafting too — it is also what lets your rosters save to the league rather than to this browser.' }),
      playNote,
    ]),
    h('div', { class: 'setup-actions' }, [done]),
  ]);
}

function downloadToken(token, code) {
  const body = `Draftmons — ${draft.season.name}\nSeason: ${draft.season.id}\n`
    + `Join code: ${code}\nCommissioner token: ${token}\n`;
  const url = URL.createObjectURL(new Blob([body], { type: 'text/plain' }));
  const link = h('a', { href: url, download: `draftmons-${draft.season.id}.txt` });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/* -------------------------------------------------------------- pieces */

function field(label, control, hint) {
  return h('label', { class: 'setup-field' }, [
    h('span', { class: 'field-label', text: label }),
    control,
    hint ? h('span', { class: 'setup-hint', text: hint }) : null,
  ]);
}

function copyButton(label, value) {
  const button = h('button', { class: 'button', type: 'button', text: label });
  button.addEventListener('click', async () => {
    flashLabel(button, (await copyText(value)) ? 'Copied' : 'Copy failed');
  });
  return button;
}

function copyRow(label, value, hint) {
  return h('div', { class: 'setup-field' }, [
    h('span', { class: 'field-label', text: label }),
    h('div', { class: 'setup-copyrow' }, [
      h('code', { class: 'setup-code', text: value }),
      copyButton('Copy', value),
    ]),
    hint ? h('span', { class: 'setup-hint', text: hint }) : null,
  ]);
}
