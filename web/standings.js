/* The Stats tab: the standings, and the season's best Pokémon.
 *
 * Two leaderboards off the same two endpoints:
 *
 *   GET /seasons/{id}/standings            every team's record, week by week
 *   GET /seasons/{id}/leaderboard/pokemon   the top five by eliminations
 *
 * The standings are a table with a column per week because that is how a
 * league reads them — not "Alpha are 4-1" but "Alpha lost in week 3" — and a
 * record without the weeks under it cannot answer the second question. The
 * week cells hold a list rather than one result, because a make-up game means
 * a team can play twice in a week and a row whose marks do not add up to its
 * record is a table nobody trusts.
 *
 * Recording lives here too, folded away behind the commissioner's token. It is
 * the same reasoning as the draft: results have to be entered somewhere, and a
 * leaderboard whose only input is /docs is a leaderboard that stays empty.
 */

import { api } from './api.js';
import { h, $, clear, placeholder, spriteFor, typePip } from './dom.js';
import * as session from './session.js';
import * as replay from './replay.js';

let ctx = null;
const ui = {};

/* Bumped on every render. Switching season twice quickly leaves two fetches in
 * flight, and without this the slower one paints over the newer one. */
let run = 0;

/* Whether the commissioner has the result form open. Recording re-renders the
 * whole tab, and a form that folds itself away after every submission is one
 * that gets reopened once per match. */
let formOpen = false;

export function init(context) {
  ctx = context;
  ui.root = $('#stats');
  ui.main = $('#stats-main');
  // The replay panel lives inside this tab, so it is initialised from here
  // rather than adding another wiring line to app.js.
  replay.init({ onRecorded: () => render() });
}

export function refresh() {
  if (ui.root && !ui.root.hidden) render();
}

// ---------------------------------------------------------------- render

async function render() {
  const season = ctx.season();
  clear(ui.main);

  if (!season) {
    ui.main.append(placeholder('◇', 'No season selected', 'Pick a season in the header first.'));
    return;
  }

  const mine = ++run;
  ui.main.append(h('p', { class: 'join-note', text: 'Loading…' }));

  let table;
  let top;
  let matches;
  try {
    [table, top, matches] = await Promise.all([
      api(`/seasons/${season.id}/standings`),
      api(`/seasons/${season.id}/leaderboard/pokemon?limit=5`),
      api(`/seasons/${season.id}/matches`),
    ]);
  } catch (problem) {
    if (mine !== run) return;
    clear(ui.main);
    ui.main.append(h('section', { class: 'panel' }, [
      h('h2', { class: 'panel-title', text: 'Standings' }),
      h('p', { class: 'panel-error', text: problem.message }),
    ]));
    return;
  }
  if (mine !== run) return;

  clear(ui.main);
  // Filtered, because recordPanel returns null for anyone who is not the
  // commissioner and append() would render that null as the text "null".
  ui.main.append(...[
    standingsPanel(table),
    elimsPanel(top),
    resultsPanel(season, matches),
    // Reading a replay and typing a result by hand are the same job, so they
    // sit next to each other rather than on separate screens.
    replay.panel(season, table.teams, table.weeks),
    recordPanel(season, table.teams, table.weeks),
  ].filter(Boolean));
}

// ------------------------------------------------------------- standings

function standingsPanel(table) {
  const body = table.matches_played
    ? h('div', { class: 'standings-wrap' }, [standingsTable(table)])
    : h('p', { class: 'join-note', text:
        'No results yet. Once a match is recorded, every team gets a row here and '
        + 'a column for each week of the season.' });

  return h('section', { class: 'panel' }, [
    h('h2', { class: 'panel-title' }, [
      document.createTextNode('Standings'),
      h('span', { class: 'panel-badge', text: `${table.matches_played} played` }),
    ]),
    h('p', { class: 'panel-note', text:
      'Ordered by wins, then fewest losses, then Pokémon differential — the mons '
      + 'you had left standing, less the ones your opponents did.' }),
    body,
  ]);
}

function standingsTable(table) {
  const head = h('tr', {}, [
    h('th', { class: 'rank-cell', text: '#' }),
    h('th', { text: 'Team' }),
    h('th', { class: 'cell-num', text: 'W' }),
    h('th', { class: 'cell-num', text: 'L' }),
    // A draw is rare enough that the column is only worth its width once one
    // has happened.
    table.teams.some((team) => team.draws) ? h('th', { class: 'cell-num', text: 'D' }) : null,
    h('th', { class: 'cell-num', text: 'Diff' }),
    ...table.weeks.map((week) => h('th', { class: 'week-cell', text: `W${week}` })),
  ]);

  const anyDraws = table.teams.some((team) => team.draws);
  const rows = table.teams.map((team) => h('tr', {}, [
    h('td', { class: 'rank-cell', text: String(team.rank) }),
    h('td', { class: 'cell-name' }, [
      h('span', { text: team.name }),
      team.owner ? h('span', { class: 'standings-owner', text: team.owner }) : null,
    ]),
    h('td', { class: 'cell-num', text: String(team.wins) }),
    h('td', { class: 'cell-num', text: String(team.losses) }),
    anyDraws ? h('td', { class: 'cell-num', text: String(team.draws) }) : null,
    h('td', { class: 'cell-num', text: signed(team.differential) }),
    // by_week lines up with table.weeks, one cell per column, so a team that
    // did not play in a week gets an empty cell rather than a shifted row.
    ...team.by_week.map((cell) => h('td', { class: 'week-cell' },
      cell.length
        ? cell.map((played) => resultChip(played))
        : [h('span', { class: 'week-empty', text: '·' })])),
  ]));

  return h('table', { class: 'standings' }, [
    h('thead', {}, [head]),
    h('tbody', {}, rows),
  ]);
}

function resultChip(played) {
  const label = { W: 'Won', L: 'Lost', D: 'Drew' }[played.result] || played.result;
  return h('span', {
    class: `result-chip is-${played.result.toLowerCase()}`,
    text: played.result,
    title: `${label} ${played.score} vs ${played.opponent}`,
  });
}

const signed = (value) => (value > 0 ? `+${value}` : String(value));

// -------------------------------------------------------- top of the pool

function elimsPanel(top) {
  if (!top.length) {
    return h('section', { class: 'panel' }, [
      h('h2', { class: 'panel-title', text: 'Top Pokémon' }),
      h('p', { class: 'join-note', text:
        'Nothing has taken a KO yet. Eliminations are entered with each result.' }),
    ]);
  }

  const best = top[0].elims;
  return h('section', { class: 'panel' }, [
    h('h2', { class: 'panel-title', text: 'Top Pokémon' }),
    h('p', { class: 'panel-note', text: 'The season\'s five best by total eliminations.' }),
    h('ol', { class: 'elim-list' }, top.map((row) => h('li', { class: 'elim-row' }, [
      h('span', { class: 'elim-rank', text: String(row.rank) }),
      spriteFor(row, 'elim-sprite'),
      h('div', { class: 'elim-ident' }, [
        h('span', { class: 'elim-name', text: row.display_name }),
        h('div', { class: 'elim-meta' }, [
          row.team_name ? h('span', { class: 'elim-team', text: row.team_name }) : null,
          ...row.types.map((type) => typePip(type)),
        ]),
      ]),
      h('div', { class: 'elim-score' }, [
        h('span', { class: 'elim-count', text: String(row.elims) }),
        h('span', { class: 'elim-of', text: row.elims === 1 ? 'elim' : 'elims' }),
        h('span', { class: 'elim-matches', text:
          `${row.matches} ${row.matches === 1 ? 'match' : 'matches'}` }),
      ]),
      // Scaled against the leader rather than against a fixed number: the bar
      // is there to show the gap to first, which is the only comparison a
      // five-row leaderboard supports.
      h('div', { class: 'elim-bar' }, [
        h('div', { class: 'elim-fill', style: `width:${Math.round((row.elims / best) * 100)}%` }),
      ]),
    ]))),
  ]);
}

// --------------------------------------------------------------- results

function resultsPanel(season, matches) {
  const admin = session.isAdmin(season.id);
  const body = matches.length
    ? h('ul', { class: 'match-list' }, matches.map((match) => matchRow(season, match, admin)))
    : h('p', { class: 'join-note', text: 'No matches recorded yet.' });

  const panel = h('details', { class: 'panel' }, [
    h('summary', { class: 'panel-title' }, [
      document.createTextNode('Results'),
      h('span', { class: 'panel-badge', text: String(matches.length) }),
    ]),
    h('div', { class: 'panel-body' }, [body]),
  ]);
  if (matches.length && matches.length <= 8) panel.open = true;
  return panel;
}

function matchRow(season, match, admin) {
  const winner = match.winner_team_id;
  const side = (team) => h('span', {
    class: team.id === winner ? 'match-team is-winner' : 'match-team',
    text: team.name,
  });

  const stat = match.elims.length
    ? h('div', { class: 'match-elims' }, match.elims.map((line) =>
      h('span', { class: 'match-elim', text: `${line.display_name} ×${line.elims}` })))
    : null;

  return h('li', { class: 'match-row' }, [
    h('div', { class: 'match-head' }, [
      h('span', { class: 'match-week', text: `Week ${match.week_no}` }),
      side(match.home),
      h('span', { class: 'match-score', text: `${match.home.score}–${match.away.score}` }),
      side(match.away),
      match.winner_team_id === null
        ? h('span', { class: 'match-draw', text: 'draw' })
        : null,
      match.replay_url
        ? h('a', { class: 'match-replay', href: match.replay_url, target: '_blank',
                   rel: 'noopener noreferrer', text: 'replay' })
        : null,
      admin ? h('button', {
        class: 'match-delete',
        type: 'button',
        title: 'Delete this result',
        text: '✕',
        onclick: (event) => removeMatch(season, match, event.currentTarget),
      }) : null,
    ]),
    stat,
  ]);
}

async function removeMatch(season, match, button) {
  if (!window.confirm(
    `Delete week ${match.week_no}, ${match.home.name} vs ${match.away.name}?\n\n`
    + 'Its eliminations go with it, and both leaderboards will change.',
  )) return;

  button.disabled = true;
  try {
    await api(`/seasons/${season.id}/matches/${match.id}`, {
      method: 'DELETE',
      headers: session.headers(season.id, { admin: true }),
    });
    render();
  } catch (problem) {
    button.disabled = false;
    window.alert(`Could not delete that result: ${problem.message}`);
  }
}

// ------------------------------------------------------ recording a result

/* Commissioner-only, and folded away like the league screen's own admin panel:
 * most people opening this tab are reading the table, not writing to it. */
function recordPanel(season, teams, weeks) {
  if (!session.isAdmin(season.id)) return null;
  if (teams.length < 2) {
    return h('section', { class: 'panel is-admin' }, [
      h('h2', { class: 'panel-title', text: 'Record a result' }),
      h('p', { class: 'join-note', text: 'Two teams have to have joined before there is a match to record.' }),
    ]);
  }

  const week = h('input', { class: 'join-code', type: 'number', min: '1', max: '52',
                            value: String(nextWeek(weeks)), 'aria-label': 'Week' });
  const home = teamSelect(teams, teams[0].team_id);
  const away = teamSelect(teams, teams[1].team_id);
  const homeScore = h('input', { class: 'join-code', type: 'number', min: '0', max: '24',
                                 value: '0', 'aria-label': 'Home score' });
  const awayScore = h('input', { class: 'join-code', type: 'number', min: '0', max: '24',
                                 value: '0', 'aria-label': 'Away score' });
  const winner = h('select', { class: 'compact', 'aria-label': 'Winner' });
  const replay = h('input', { class: 'join-team', type: 'url', placeholder: 'https://replay.pokemonshowdown.com/…',
                              'aria-label': 'Replay link' });
  const homeElims = elimsField();
  const awayElims = elimsField();
  const error = h('p', { class: 'panel-error', hidden: true, role: 'alert' });
  const submit = h('button', { class: 'button', type: 'submit', text: 'Record result' });

  /* The winner list is rebuilt whenever a side changes, so it can only ever
   * offer the two teams actually in the match — the same rule the API
   * enforces, applied where the mistake would be made. */
  const syncWinner = () => {
    const chosen = winner.value;
    clear(winner);
    winner.append(h('option', { value: '', text: 'Draw — nobody won' }));
    for (const id of [home.value, away.value]) {
      const team = teams.find((row) => String(row.team_id) === id);
      if (team) winner.append(h('option', { value: id, text: team.name }));
    }
    // Defaults to the home team, not to the draw at the top of the list: a
    // draw is the rare case, and a default nobody notices is a wrong result.
    winner.value = chosen && [...winner.options].some((option) => option.value === chosen)
      ? chosen : home.value;
    homeElims.label.textContent = `${nameOf(teams, home.value)} eliminations`;
    awayElims.label.textContent = `${nameOf(teams, away.value)} eliminations`;
  };
  home.addEventListener('change', syncWinner);
  away.addEventListener('change', syncWinner);
  syncWinner();

  const form = h('form', {
    class: 'result-form',
    onsubmit: async (event) => {
      event.preventDefault();
      error.hidden = true;

      if (home.value === away.value) {
        return fail(error, away, 'A team cannot play itself. Pick two different teams.');
      }
      let lines;
      try {
        lines = [
          ...parseElims(homeElims.input.value, Number(home.value)),
          ...parseElims(awayElims.input.value, Number(away.value)),
        ];
      } catch (problem) {
        return fail(error, homeElims.input, problem.message);
      }

      submit.disabled = true;
      try {
        await api(`/seasons/${season.id}/matches`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...session.headers(season.id, { admin: true }),
          },
          body: JSON.stringify({
            week_no: Number(week.value),
            home_team_id: Number(home.value),
            away_team_id: Number(away.value),
            winner_team_id: winner.value ? Number(winner.value) : null,
            home_score: Number(homeScore.value),
            away_score: Number(awayScore.value),
            replay_url: replay.value.trim() || null,
            elims: lines,
          }),
        });
        render();
      } catch (problem) {
        submit.disabled = false;
        fail(error, week, problem.message);
      }
      return undefined;
    },
  }, [
    h('div', { class: 'result-grid' }, [
      labelled('Week', week),
      labelled('Home', home),
      labelled('Score', homeScore),
      labelled('Away', away),
      labelled('Score', awayScore),
      labelled('Winner', winner),
    ]),
    labelled('Replay link', replay, 'Optional'),
    h('div', { class: 'result-elims' }, [
      h('label', { class: 'join-field' }, [homeElims.label, homeElims.input]),
      h('label', { class: 'join-field' }, [awayElims.label, awayElims.input]),
    ]),
    h('p', { class: 'join-note', text:
      'One Pokémon per line, "Great Tusk, 2". Anything with no KOs can be left out.' }),
    error,
    submit,
  ]);

  const panel = h('details', { class: 'panel is-admin' }, [
    h('summary', { class: 'panel-title' }, [
      document.createTextNode('Record a result'),
      h('span', { class: 'panel-badge', text: 'commissioner' }),
    ]),
    h('div', { class: 'panel-body' }, [form]),
  ]);
  panel.open = formOpen;
  panel.addEventListener('toggle', () => { formOpen = panel.open; });
  return panel;
}

function teamSelect(teams, selected) {
  const select = h('select', { class: 'compact' },
    teams.map((team) => h('option', { value: String(team.team_id), text: team.name })));
  select.value = String(selected);
  return select;
}

function elimsField() {
  return {
    label: h('span', { class: 'field-label', text: 'Eliminations' }),
    input: h('textarea', { class: 'result-elim-input', rows: '4',
                           placeholder: 'Great Tusk, 2\nKingambit, 1' }),
  };
}

function labelled(label, control, note) {
  return h('label', { class: 'join-field' }, [
    h('span', { class: 'field-label', text: label }),
    control,
    note ? h('span', { class: 'join-note', text: note }) : null,
  ]);
}

const nameOf = (teams, id) =>
  teams.find((team) => String(team.team_id) === String(id))?.name || 'Team';

/* "Great Tusk, 2" per line: the same shape as the cost list the pool is built
 * from, split the same way — on the last comma or tab, else on the last space —
 * so there is one paste format to learn rather than two.
 *
 * A line that will not parse is an error rather than a skip. In a cost list an
 * unreadable line is a header; here it is a Pokémon whose eliminations would
 * go missing with nobody the wiser. */
function parseElims(text, teamId) {
  const lines = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;

    let cut = Math.max(line.lastIndexOf(','), line.lastIndexOf('\t'));
    if (cut < 0) cut = line.lastIndexOf(' ');
    const name = cut < 0 ? '' : line.slice(0, cut).trim();
    // "x2" and "×2" are how a stat line gets written by hand, so they parse.
    const count = cut < 0 ? '' : line.slice(cut + 1).trim().replace(/^[x×]\s*/i, '');

    if (!name || !/^\d+$/.test(count)) {
      throw new Error(
        `Could not read "${line}". Each line needs a name and a number, like "Great Tusk, 2".`,
      );
    }
    lines.push({ team_id: teamId, name, elims: Number(count) });
  }
  return lines;
}

function fail(errorNode, focusNode, message) {
  errorNode.textContent = message;
  errorNode.hidden = false;
  focusNode.focus();
  return undefined;
}

/* Default the form to the week after the latest one recorded: results are
 * entered in order, a week at a time, and the week is the field most likely to
 * be left at whatever it was. */
function nextWeek(weeks) {
  return (weeks.length ? weeks[weeks.length - 1] : 0) + 1;
}
