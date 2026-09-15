/* The replay analyzer panel, on the Stats tab.
 *
 * The alternative to typing a stat line out of a battle by hand: paste the
 * Showdown replay, and the kills, deaths and score come out of the log.
 *
 * Deliberately two steps. The server reads the replay and *proposes* a result
 * — which league team each side was, which Pokemon scored what — and this shows
 * that proposal before anything is written. The team match is inferred from
 * which side brought whose drafted Pokemon, which is reliable but is still an
 * inference, and a wrong one would put a fabricated result in the standings
 * for the rest of the season. So a commissioner sees it first, and can
 * override either side.
 */

import { api } from './api.js';
import { h, $, clear } from './dom.js';
import * as session from './session.js';

let ctx = null;
let proposal = null;
let open = false;

export function init(context) {
  ctx = context;
}

/* The knockout history, in two views of the same events.
 *
 * Text is one sentence per faint. Visual puts each side in its own column and
 * draws the knockout across the gap — which is faster to read down, because
 * the direction of every arrow is the answer to "who is winning".
 *
 * Both are tinted by which side *lost* the Pokemon rather than which side
 * scored. The thing a reader scans for is their own team's losses, so the
 * colour answers "how did my week go" before the words are read.
 */
let koView = 'visual';

export function eventsList(events, sides) {
  const label = { p1: sides?.[0]?.player || 'Side 1', p2: sides?.[1]?.player || 'Side 2' };
  const body = h('div', { class: 'ko-body' });

  const paint = () => {
    clear(body);
    body.append(koView === 'text' ? textView(events) : visualView(events, label));
  };

  const tab = (name, text) => h('button', {
    class: koView === name ? 'ko-tab is-active' : 'ko-tab',
    type: 'button', text,
    onclick: () => {
      koView = name;
      for (const button of head.querySelectorAll('.ko-tab')) {
        button.classList.toggle('is-active', button.textContent === (name === 'text' ? 'Text' : 'Visual'));
      }
      paint();
    },
  });

  const head = h('div', { class: 'ko-head' }, [
    h('h4', { class: 'ko-title', text: 'Events' }),
    h('div', { class: 'ko-tabs' }, [tab('visual', 'Visual'), tab('text', 'Text')]),
  ]);

  paint();
  return h('div', { class: 'ko-panel' }, [head, body]);
}

function textView(events) {
  return h('ol', { class: 'ko-list' }, events.map((event) => h('li', {
    class: `ko-row ${event.victim_side === 'p1' ? 'is-p1' : 'is-p2'}`,
  }, [
    h('span', { class: 'ko-turn', text: `Turn ${event.turn ?? event.turn_no}` }),
    h('span', { class: 'ko-text', text: event.text }),
  ])));
}

/** A sprite and a name, or just a name when the Pokemon is not in the pool. */
function actor(species, sprite, isVictim) {
  return h('div', { class: isVictim ? 'ko-actor is-victim' : 'ko-actor' }, [
    sprite
      ? h('img', { class: 'ko-sprite', src: sprite, alt: '', loading: 'lazy' })
      : h('span', { class: 'ko-sprite is-empty', 'aria-hidden': 'true' }),
    h('span', { class: 'ko-name', text: species || '—' }),
  ]);
}

function visualView(events, label) {
  const rows = events.map((event) => {
    const victimSide = event.victim_side;
    const killerSide = event.killer_side;
    // Columns are fixed per player, so which cell a Pokemon lands in is
    // decided by its side and never by whether it won the exchange.
    const cell = { p1: null, p2: null };
    cell[victimSide] = actor(event.victim, event.victim_sprite, true);
    if (killerSide) cell[killerSide] = actor(event.killer, event.killer_sprite, false);

    // Nothing to point from when hazards did it, so the cause stands alone.
    const arrow = h('div', {
      class: [
        'ko-arrow',
        event.indirect ? 'is-indirect' : '',
        killerSide ? (victimSide === 'p2' ? 'points-right' : 'points-left') : 'no-source',
      ].filter(Boolean).join(' '),
    }, [
      h('span', { class: 'ko-move', text: event.cause || 'fainted' }),
      killerSide ? h('span', { class: 'ko-arrowhead', 'aria-hidden': 'true' }) : null,
    ]);

    return h('li', { class: `ko-vrow ${victimSide === 'p1' ? 'is-p1' : 'is-p2'}` }, [
      h('span', { class: 'ko-turn', text: `T${event.turn ?? event.turn_no}` }),
      h('div', { class: 'ko-lane' }, [
        cell.p1 || h('div', { class: 'ko-actor is-blank' }),
        arrow,
        cell.p2 || h('div', { class: 'ko-actor is-blank' }),
      ]),
    ]);
  });

  return h('div', { class: 'ko-visual' }, [
    h('div', { class: 'ko-cols' }, [
      h('span', { class: 'ko-col is-p1', text: label.p1 }),
      h('span', { class: 'ko-col is-p2', text: label.p2 }),
    ]),
    h('ol', { class: 'ko-list' }, rows),
  ]);
}

/** The panel, or null when this browser is not the commissioner. */
export function panel(season, teams, weeks) {
  if (!session.isAdmin(season.id)) return null;

  const url = h('input', {
    type: 'url', class: 'replay-url',
    placeholder: 'https://replay.pokemonshowdown.com/gen9ou-1234567890',
    autocomplete: 'off',
  });
  const week = h('input', {
    type: 'number', min: '1', max: '99', class: 'replay-week',
    value: String((weeks?.length ? Math.max(...weeks) : 0) + 1),
  });
  const result = h('div', { class: 'replay-result' });
  const status = h('p', { class: 'panel-error', hidden: true, role: 'alert' });

  const say = (message) => {
    status.textContent = message || '';
    status.hidden = !message;
  };

  const analyze = h('button', { class: 'button', type: 'button', text: 'Analyze' });
  const record = h('button', {
    class: 'button is-primary', type: 'button', text: 'Record result', hidden: true,
  });

  const paint = () => {
    clear(result);
    record.hidden = true;
    if (!proposal) return;

    const a = proposal.analysis;
    result.append(h('div', { class: 'replay-meta' }, [
      h('span', { text: a.format || 'Unknown format' }),
      h('span', { text: `${a.turns} turns` }),
      a.winner ? h('span', { text: `${a.winner} won` }) : h('span', { text: 'Draw' }),
    ]));

    for (const [index, side] of proposal.sides.entries()) {
      // The override: a side whose Pokemon are not drafted here cannot be
      // matched, and a new team has no picks to match on at all.
      const picker = h('select', {
        class: 'replay-team',
        onchange: (event) => {
          proposal.sides[index].team_id = Number(event.target.value) || null;
        },
      }, [
        h('option', { value: '', text: '— not matched —' }),
        ...teams.map((team) => h('option', {
          value: String(team.team_id ?? team.id),
          text: team.name,
          selected: (team.team_id ?? team.id) === side.team_id,
        })),
      ]);

      result.append(h('div', { class: 'replay-side' }, [
        h('div', { class: 'replay-side-head' }, [
          h('strong', { text: side.player }),
          h('span', { class: 'replay-kd', text: `${side.kills} KO · ${side.deaths} lost · ${side.remaining} left` }),
          picker,
          // How much of the side was actually drafted by the matched team.
          // Six of six is certain; one of six is a coincidence worth checking.
          h('span', {
            class: side.confidence >= 4 ? 'replay-conf is-good' : 'replay-conf',
            text: `${side.confidence}/${side.pokemon.length + side.not_in_pool.length} matched`,
          }),
        ]),
        h('ul', { class: 'replay-mons' }, side.pokemon.map((mon) => h('li', {
          class: mon.fainted ? 'replay-mon is-fainted' : 'replay-mon',
        }, [
          h('span', { class: 'replay-mon-name', text: mon.species }),
          h('span', { class: 'replay-mon-kills', text: mon.kills ? `${mon.kills} KO` : '—' }),
        ]))),
        side.not_in_pool.length
          ? h('p', { class: 'replay-warn', text: `Not in this pool: ${side.not_in_pool.join(', ')}` })
          : null,
      ]));
    }

    if (a.events?.length) result.append(eventsList(a.events, proposal.sides));

    for (const problem of proposal.problems) {
      result.append(h('p', { class: 'replay-warn', text: problem }));
    }
    record.hidden = false;
  };

  analyze.addEventListener('click', async () => {
    say('');
    proposal = null;
    clear(result);
    analyze.disabled = true;
    analyze.textContent = 'Reading replay…';
    try {
      proposal = await api(`/seasons/${season.id}/replay/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...session.headers(season.id, { admin: true }) },
        body: JSON.stringify({ url: url.value.trim() }),
      });
      paint();
    } catch (error) {
      say(error.message);
    } finally {
      analyze.disabled = false;
      analyze.textContent = 'Analyze';
    }
  });

  record.addEventListener('click', async () => {
    say('');
    record.disabled = true;
    record.textContent = 'Recording…';
    try {
      await api(`/seasons/${season.id}/replay/record`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...session.headers(season.id, { admin: true }) },
        body: JSON.stringify({
          url: url.value.trim(),
          week_no: Number(week.value) || 1,
          // Sent whatever the server guessed, so an override in the picker is
          // what gets written rather than the guess being silently re-made.
          home_team_id: proposal.sides[0].team_id,
          away_team_id: proposal.sides[1].team_id,
        }),
      });
      proposal = null;
      url.value = '';
      ctx.onRecorded();
    } catch (error) {
      say(error.message);
    } finally {
      record.disabled = false;
      record.textContent = 'Record result';
    }
  });

  const box = h('details', { class: 'panel is-admin' }, [
    h('summary', { class: 'panel-title' }, [
      document.createTextNode('Analyze a replay'),
      h('span', { class: 'panel-badge', text: 'commissioner' }),
    ]),
    h('p', { class: 'join-note', text: 'Paste a Showdown replay and the kills, losses and score are read out of the battle log.' }),
    h('div', { class: 'replay-row' }, [
      h('label', { class: 'field' }, [
        h('span', { class: 'field-label', text: 'Replay link' }), url,
      ]),
      h('label', { class: 'field' }, [
        h('span', { class: 'field-label', text: 'Week' }), week,
      ]),
      analyze,
    ]),
    status,
    result,
    record,
  ]);
  box.open = open;
  box.addEventListener('toggle', () => { open = box.open; });
  return box;
}
