/* The League tab: joining a season, and what you are once you have.
 *
 * This exists so nobody has to open /docs to get into a league. It covers both
 * halves of that, because covering only one leaves the other on the docs page:
 * a commissioner opens the season and gets a code to share, and a player
 * enters that code and picks a team name.
 *
 * The token is the hard part of the design. Joining returns one, it is the
 * player's whole identity, and the backend cannot reissue it — no accounts
 * means no email to reset against. So it is shown once, deliberately, with
 * copy and download next to it, and the screen says plainly what losing it
 * costs. Everything else here is ordinary forms; that one panel is the reason
 * this is a screen rather than a pair of window.prompt() calls.
 *
 * Identity itself lives in session.js, which is shared with the draft tab.
 * Nothing here keeps its own copy.
 */

import { api } from './api.js';
import { h, $, clear, placeholder, copyText, flashLabel } from './dom.js';
import * as session from './session.js';
import * as logos from './teamlogo.js';

let ctx = null;
const ui = {};

/* Set after a successful join or open, and cleared once acknowledged. The one
 * piece of state this module keeps, because a token that has scrolled off
 * screen is a token the player has lost. */
let freshSecret = null;

export function init(context) {
  ctx = context;
  ui.root = $('#league');
  ui.main = $('#league-main');
}

export function refresh() {
  if (ui.root && !ui.root.hidden) render();
}

/** Called by the draft tab's "Join this draft" button. */
export function focusJoin() {
  ctx.showTab('league');
  // The field only exists after a render, and the render is synchronous.
  $('#join-code')?.focus();
}

// ---------------------------------------------------------------- render

function render() {
  const season = ctx.season();
  clear(ui.main);

  if (!season) {
    ui.main.append(placeholder('◇', 'No season selected', 'Pick a season in the header first.'));
    return;
  }

  const held = session.forSeason(season.id);

  if (freshSecret) {
    ui.main.append(secretPanel(freshSecret));
  }

  ui.main.append(
    held.player ? playerPanel(season, held) : joinPanel(season),
    lobbyPanel(season),
    commissionerPanel(season, held),
  );
}

// ------------------------------------------------------------- the token

/* Shown once, and it takes up the top of the screen until dismissed. The
 * dismissal is a checkbox rather than a plain close button: the player has to
 * state that they saved it, because the alternative is losing a seat in a
 * season that has a fixed number of them. */
function secretPanel({ kind, token, teamName }) {
  const label = kind === 'admin' ? 'commissioner token' : 'player token';

  const field = h('input', {
    class: 'secret-value',
    value: token,
    readonly: true,
    spellcheck: 'false',
    'aria-label': label,
    onclick: (event) => event.target.select(),
  });

  const copyButton = h('button', { class: 'button', type: 'button', text: 'Copy' });
  copyButton.addEventListener('click', async () => {
    flashLabel(copyButton, (await copyText(token)) ? 'Copied' : 'Copy failed');
  });

  const confirm = h('input', { type: 'checkbox', id: 'secret-saved' });
  const done = h('button', {
    class: 'button is-quiet',
    type: 'button',
    text: 'Done',
    disabled: true,
    onclick: () => {
      freshSecret = null;
      render();
    },
  });
  confirm.addEventListener('change', () => {
    done.disabled = !confirm.checked;
  });

  return h('section', { class: 'panel is-secret' }, [
    h('h2', { class: 'panel-title', text: `Save your ${label}` }),
    h('p', { class: 'panel-note' }, [
      document.createTextNode(
        kind === 'admin'
          ? 'This is the only time it is shown. It is what lets you rotate the join code, start the draft and close the season. '
          : `You joined as ${teamName}. This is the only time your token is shown. `,
      ),
      h('strong', { text: 'It cannot be recovered or reissued.' }),
      document.createTextNode(
        ' This browser has saved it, so you do not need it day to day — but if you clear site data or move to another device, this is the only way back in.',
      ),
    ]),
    h('div', { class: 'secret-row' }, [
      field,
      copyButton,
      h('button', {
        class: 'button is-quiet',
        type: 'button',
        text: 'Download',
        onclick: () => downloadToken(kind, token, teamName),
      }),
    ]),
    h('label', { class: 'check secret-check' }, [
      confirm,
      h('span', { text: 'I have saved this somewhere safe' }),
    ]),
    done,
  ]);
}

function downloadToken(kind, token, teamName) {
  const season = ctx.season();
  const body = [
    `Draftmons ${kind === 'admin' ? 'commissioner' : 'player'} token`,
    `League : ${season.name} (season ${season.id})`,
    teamName ? `Team   : ${teamName}` : null,
    `Token  : ${token}`,
    '',
    'This token is your identity in this league. It cannot be reissued.',
    'Keep it private: anyone holding it can act as you.',
  ].filter(Boolean).join('\n');

  const url = URL.createObjectURL(new Blob([body], { type: 'text/plain;charset=utf-8' }));
  const link = h('a', { href: url, download: `draftmons-${kind}-token.txt` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// -------------------------------------------------------------- joining

function joinPanel(season) {
  const code = h('input', {
    id: 'join-code',
    class: 'join-code',
    placeholder: 'ABCD2345',
    maxlength: '12',
    autocomplete: 'off',
    spellcheck: 'false',
    'aria-label': 'Join code',
    // Codes get read out loud and typed in lower case; the API is forgiving
    // about that and so is the field, which just shows it back as it will be
    // sent.
    oninput: (event) => {
      event.target.value = event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
    },
  });

  const teamName = h('input', {
    class: 'join-team',
    placeholder: 'Sinnoh Slammers',
    maxlength: '60',
    autocomplete: 'off',
    'aria-label': 'Team name',
  });

  const who = h('input', {
    class: 'join-who',
    placeholder: 'Your name (optional)',
    maxlength: '40',
    autocomplete: 'off',
    'aria-label': 'Your name',
  });

  const error = h('p', { class: 'panel-error', hidden: true, role: 'alert' });
  const submit = h('button', { class: 'button', type: 'submit', text: 'Join league' });

  const form = h('form', {
    class: 'join-form',
    onsubmit: async (event) => {
      event.preventDefault();
      error.hidden = true;

      if (!code.value.trim()) return fail(error, code, 'Enter the join code your commissioner gave you.');
      if (!teamName.value.trim()) return fail(error, teamName, 'Pick a name for your team.');

      submit.disabled = true;
      submit.textContent = 'Joining…';
      try {
        const result = await api(`/join/${code.value.trim()}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            team_name: teamName.value.trim(),
            display_name: who.value.trim() || null,
          }),
        });

        /* The token is stored and also shown. Stored so the player never
         * thinks about it again; shown because storage is one cleared cache
         * away from gone and the seat cannot be reissued. */
        const saved = session.remember(season.id, {
          player: result.token,
          team: result.team,
        });
        freshSecret = {
          kind: 'player',
          token: result.token,
          teamName: result.team.name,
          storageFailed: !saved,
        };
        ctx.onIdentityChange();
        render();
      } catch (problem) {
        submit.disabled = false;
        submit.textContent = 'Join league';
        fail(error, code, joinMessage(problem));
      }
    },
  }, [
    h('div', { class: 'join-grid' }, [
      field('Join code', code, 'From your commissioner'),
      field('Team name', teamName, 'Public, and unique in this league'),
      field('Your name', who, 'Optional, shown on the roster'),
    ]),
    error,
    submit,
  ]);

  return h('section', { class: 'panel' }, [
    h('h2', { class: 'panel-title', text: `Join ${season.name}` }),
    h('p', { class: 'panel-note' }, [
      document.createTextNode('Your team name is public. Everything you go on to plan is '),
      h('strong', { text: 'private to you' }),
      document.createTextNode(' — the other players and the commissioner cannot read it.'),
    ]),
    form,
    returningPlayer(season),
  ]);
}

/* The way back in.
 *
 * A player's token lives in this browser's storage, which makes returning week
 * to week automatic — and makes a cleared cache or a second device look
 * exactly like never having joined. Without this, such a player is locked out
 * of a team they still own: the seat is taken, the name is taken, and joining
 * again is the only option, which is not one.
 *
 * So the token doubles as a sign-in. It is the only credential the system has,
 * which is the whole point of telling players to save it.
 */
function returningPlayer(season) {
  const input = h('input', {
    class: 'secret-value',
    placeholder: 'dmp_…',
    autocomplete: 'off',
    spellcheck: 'false',
    'aria-label': 'Player token',
  });
  const error = h('p', { class: 'panel-error', hidden: true, role: 'alert' });
  const submit = h('button', { class: 'button is-quiet', type: 'submit', text: 'Sign in' });

  const form = h('form', {
    class: 'adopt-form',
    onsubmit: async (event) => {
      event.preventDefault();
      error.hidden = true;
      const token = input.value.trim();
      if (!token) return;

      submit.disabled = true;
      try {
        /* Verified against the server before being stored, so a typo cannot
         * leave the UI believing it is signed in. /me also reports which
         * season the token belongs to, which is worth checking: one person
         * may hold tokens for several leagues, and storing a token under the
         * wrong season would silently point the draft tab at another team. */
        const me = await api('/me', { headers: { Authorization: `Bearer ${token}` } });
        if (me.season_id !== season.id) {
          throw Object.assign(
            new Error(`That token belongs to a different league (season ${me.season_id}), not ${season.name}.`),
            { handled: true },
          );
        }
        session.remember(season.id, { player: token, team: me.team });
        ctx.onIdentityChange();
        render();
      } catch (problem) {
        submit.disabled = false;
        fail(error, input, problem.handled || problem.offline
          ? problem.message
          : problem.status === 401
            ? 'That token is not valid. It may belong to a player who left the league.'
            : problem.message);
      }
    },
  }, [
    h('div', { class: 'secret-row' }, [input, submit]),
    error,
  ]);

  return h('details', { class: 'returning' }, [
    h('summary', { class: 'returning-summary', text: 'Already joined? Sign in with your token' }),
    h('div', { class: 'returning-body' }, [
      h('p', { class: 'join-note', text:
        'Use this on a new device, or after clearing your browser data. Your token is '
        + 'the only way back into a team you already own — there is no password to reset.' }),
      form,
    ]),
  ]);
}

/* The API's own messages are already written for a player, so they are shown
 * as-is. These two get a sentence of context the API cannot know: that the
 * code goes stale, and that a season has a fixed number of seats. */
function joinMessage(problem) {
  // An unreachable server is not a bad code, and must not be reported as one.
  if (problem.offline) return problem.message;
  if (problem.status === 404) {
    return `${problem.message} Codes change when the commissioner rotates them — ask for the current one.`;
  }
  if (problem.status === 409) {
    return `${problem.message} Pick a different name.`;
  }
  return problem.message;
}

function fail(errorNode, focusNode, message) {
  errorNode.textContent = message;
  errorNode.hidden = false;
  focusNode.focus();
}

function field(label, control, note) {
  return h('label', { class: 'join-field' }, [
    h('span', { class: 'field-label', text: label }),
    control,
    note ? h('span', { class: 'join-note', text: note }) : null,
  ]);
}

// ------------------------------------------------------- once you are in

function playerPanel(season, held) {
  const name = h('input', {
    class: 'join-team',
    value: held.team?.name || '',
    maxlength: '60',
    'aria-label': 'Team name',
  });
  const error = h('p', { class: 'panel-error', hidden: true, role: 'alert' });

  const rename = h('button', { class: 'button', type: 'submit', text: 'Rename' });

  const form = h('form', {
    class: 'identity-form',
    onsubmit: async (event) => {
      event.preventDefault();
      error.hidden = true;
      const wanted = name.value.trim();
      if (!wanted || wanted === held.team?.name) return;

      rename.disabled = true;
      try {
        const me = await api('/me', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', ...session.headers(season.id) },
          body: JSON.stringify({ team_name: wanted }),
        });
        session.remember(season.id, { team: me.team });
        ctx.onIdentityChange();
        render();
      } catch (problem) {
        rename.disabled = false;
        fail(error, name, problem.message);
      }
    },
  }, [
    h('div', { class: 'identity-row' }, [
      field('Your team', name, 'Renaming shows up for everyone'),
      rename,
    ]),
    error,
    // The logo is stored on the server, so it appears on everyone's board.
    logos.logoEditor(season, held.team, ctx.onIdentityChange),
  ]);

  return h('section', { class: 'panel' }, [
    h('h2', { class: 'panel-title' }, [
      document.createTextNode('You are '),
      h('span', { class: 'identity-name', text: held.team?.name || 'a player' }),
    ]),
    h('p', { class: 'panel-note', text: `In ${season.name}. This browser holds your player token.` }),
    form,
    h('div', { class: 'panel-actions' }, [
      h('button', {
        class: 'button is-quiet',
        type: 'button',
        text: 'Leave league',
        onclick: () => leave(season, held),
      }),
      h('span', {
        class: 'join-note',
        text: 'Leaving deletes your team and your saved plans, and frees your seat.',
      }),
    ]),
  ]);
}

async function leave(season, held) {
  const team = held.team?.name || 'your team';
  if (!window.confirm(
    `Leave ${season.name}?\n\n${team} and everything you have planned on the server `
    + 'will be deleted. Your token stops working. This cannot be undone.',
  )) return;

  try {
    await api('/me', { method: 'DELETE', headers: session.headers(season.id) });
  } catch (problem) {
    // A token the server has already forgotten is still worth clearing here.
    if (problem.status !== 401) {
      window.alert(`Could not leave: ${problem.message}`);
      return;
    }
  }
  session.forget(season.id);
  ctx.onIdentityChange();
  render();
}

// ---------------------------------------------------------- who is here

function lobbyPanel(season) {
  const body = h('div', { class: 'lobby-body' }, [h('p', { class: 'join-note', text: 'Loading…' })]);

  /* The lobby is public and needs no token, which is the point: someone
   * deciding whether to join can see who is already in. */
  api(`/seasons/${season.id}/lobby`)
    .then((lobby) => {
      clear(body);
      const mine = session.forSeason(season.id).team?.id;

      body.append(h('p', { class: 'lobby-count' }, [
        h('strong', { text: `${lobby.teams.length} of ${lobby.capacity}` }),
        document.createTextNode(` seats taken · ${lobby.is_open ? 'open to join' : 'closed'}`),
      ]));

      if (!lobby.teams.length) {
        body.append(h('p', { class: 'join-note', text: 'Nobody has joined yet.' }));
        return;
      }
      body.append(h('ul', { class: 'lobby-list' }, lobby.teams.map((team) =>
        h('li', { class: team.id === mine ? 'lobby-row is-me' : 'lobby-row' }, [
          h('span', {
            class: 'lobby-position',
            text: team.draft_position ? `#${team.draft_position}` : '—',
          }),
          h('span', { class: 'lobby-name', text: team.name }),
          team.owner ? h('span', { class: 'lobby-owner', text: team.owner }) : null,
          team.id === mine ? h('span', { class: 'lobby-you', text: 'you' }) : null,
        ]),
      )));
    })
    .catch((problem) => {
      clear(body);
      body.append(h('p', { class: 'panel-error', text: problem.message }));
    });

  return h('section', { class: 'panel' }, [
    h('h2', { class: 'panel-title', text: 'Who is in' }),
    body,
  ]);
}

// ------------------------------------------------------- commissioner

/* Folded away by default. Most people opening this screen are joining, not
 * running the league, and a join code field next to an "open the season"
 * button is how someone opens a second season by accident. */
function commissionerPanel(season, held) {
  const body = h('div', { class: 'panel-body' });
  const panel = h('details', { class: 'panel is-admin' }, [
    h('summary', { class: 'panel-title' }, [
      document.createTextNode('Running this league?'),
      held.admin ? h('span', { class: 'panel-badge', text: 'commissioner' }) : null,
    ]),
    body,
  ]);
  if (held.admin) panel.open = true;

  if (!held.admin) {
    body.append(
      h('p', { class: 'panel-note', text:
        `Open ${season.name} for joining to get a code to share. Whoever does this `
        + 'gets the commissioner token for the season, so do it once, on your own machine.' }),
      h('div', { class: 'panel-actions' }, [
        h('button', {
          class: 'button', type: 'button', text: 'Open this season for joining',
          onclick: (event) => openSeason(season, event.target),
        }),
        h('span', { class: 'join-note', text: 'Already opened it elsewhere? Paste the token below.' }),
      ]),
      adoptForm(season),
    );
    return panel;
  }

  body.append(codePanel(season), takeTeamPanel(season, held));
  return panel;
}

async function openSeason(season, button) {
  button.disabled = true;
  button.textContent = 'Opening…';
  try {
    const invite = await api(`/seasons/${season.id}/invite`, {
      method: 'POST',
      headers: session.headers(season.id, { admin: true }),
    });
    session.remember(season.id, { admin: invite.admin_token });
    freshSecret = { kind: 'admin', token: invite.admin_token };
    ctx.onIdentityChange();
    render();
  } catch (problem) {
    button.disabled = false;
    button.textContent = 'Open this season for joining';
    window.alert(
      problem.status === 401
        ? 'This season is already open and someone else holds its commissioner token. '
          + 'Paste that token below to manage it from this browser.'
        : problem.message,
    );
  }
}

/* Taking one of the league's teams as your own.
 *
 * A commissioner who ran the draft for the table holds an admin token and no
 * team, and every part of this app that asks "which team are you" — the
 * planner's roster, the draft tab's turn, your name on the board — reads a
 * player token. So the commissioner issues themselves one.
 *
 * The same button is how a player who lost their token gets back in, which is
 * the only route there is: nothing here can reverse a hash. It costs whoever
 * held the old token their access, so that is said before rather than after.
 */
function takeTeamPanel(season, held) {
  const select = h('select', { class: 'compact', 'aria-label': 'Team to take' }, [
    h('option', { value: '', text: 'Loading teams…' }),
  ]);
  const take = h('button', { class: 'button is-quiet', type: 'button', text: 'Take this team',
                             disabled: true });
  const error = h('p', { class: 'panel-error', hidden: true, role: 'alert' });

  api(`/seasons/${season.id}/teams`)
    .then((teams) => {
      clear(select);
      if (!teams.length) {
        select.append(h('option', { value: '', text: 'Nobody has joined yet' }));
        return;
      }
      select.append(h('option', { value: '', text: 'Choose a team…' }));
      for (const team of teams) {
        select.append(h('option', {
          value: String(team.id),
          text: team.id === held.team?.id ? `${team.name} — already yours` : team.name,
        }));
      }
      take.disabled = false;
    })
    .catch((problem) => fail(error, select, problem.message));

  take.addEventListener('click', async () => {
    error.hidden = true;
    const teamId = Number(select.value);
    if (!teamId) return fail(error, select, 'Pick a team first.');

    const name = select.selectedOptions[0].textContent;
    if (!window.confirm(
      `Take ${name} on this browser?\n\n`
      + 'You get a player token for it, and this browser plays as that team. Any '
      + 'token already issued for it stops working, so whoever holds one is signed '
      + 'out and will need a new one from you.',
    )) return;

    take.disabled = true;
    try {
      const issued = await api(`/seasons/${season.id}/teams/${teamId}/token`, {
        method: 'POST',
        headers: session.headers(season.id, { admin: true }),
      });
      const saved = session.remember(season.id, { player: issued.token, team: issued.team });
      /* Shown once, like every other token this app hands out — the panel at
       * the top of the screen, not a line in a form nobody rereads. */
      freshSecret = {
        kind: 'player',
        token: issued.token,
        teamName: issued.team.name,
        storageFailed: !saved,
      };
      ctx.onIdentityChange();
      render();
    } catch (problem) {
      take.disabled = false;
      fail(error, select, problem.message);
    }
    return undefined;
  });

  return h('div', { class: 'panel-body take-team' }, [
    h('h3', { class: 'section-title', text: 'Play as one of these teams' }),
    h('p', { class: 'panel-note', text:
      'If one of these is yours, take it here: this browser then drafts, plans and '
      + 'shows up as that team instead of only running the league.' }),
    h('div', { class: 'secret-row' }, [select, take]),
    error,
  ]);
}

/* For the commissioner on a second machine, or after clearing site data. The
 * token is the only proof, so pasting it is the only way back in. */
function adoptForm(season) {
  const input = h('input', {
    class: 'secret-value',
    placeholder: 'dma_…',
    autocomplete: 'off',
    spellcheck: 'false',
    'aria-label': 'Commissioner token',
  });
  const error = h('p', { class: 'panel-error', hidden: true, role: 'alert' });

  return h('form', {
    class: 'adopt-form',
    onsubmit: async (event) => {
      event.preventDefault();
      error.hidden = true;
      const token = input.value.trim();
      if (!token) return;

      try {
        // Verified against the server before being stored, so a typo does not
        // leave the UI thinking it is the commissioner.
        await api(`/seasons/${season.id}/invite`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        session.remember(season.id, { admin: token });
        ctx.onIdentityChange();
        render();
      } catch (problem) {
        fail(error, input, problem.status === 401
          ? 'That token does not manage this season.'
          : problem.message);
      }
    },
  }, [
    h('div', { class: 'secret-row' }, [
      input,
      h('button', { class: 'button is-quiet', type: 'submit', text: 'Use token' }),
    ]),
    error,
  ]);
}

function codePanel(season) {
  const body = h('div', { class: 'panel-body' }, [
    h('p', { class: 'join-note', text: 'Loading…' }),
  ]);

  const load = () => {
    api(`/seasons/${season.id}/invite`, { headers: session.headers(season.id, { admin: true }) })
      .then((invite) => {
        clear(body);
        const codeField = h('input', {
          class: 'secret-value is-code',
          value: invite.join_code,
          readonly: true,
          'aria-label': 'Join code',
          onclick: (event) => event.target.select(),
        });

        const copyButton = h('button', { class: 'button', type: 'button', text: 'Copy code' });
        copyButton.addEventListener('click', async () => {
          flashLabel(copyButton, (await copyText(invite.join_code)) ? 'Copied' : 'Copy failed');
        });

        body.append(
          h('p', { class: 'panel-note', text:
            'Share this code with your players. They enter it above — they never need '
            + 'the docs page or your commissioner token.' }),
          h('div', { class: 'secret-row' }, [codeField, copyButton]),
          h('p', { class: 'lobby-count' }, [
            h('strong', { text: `${invite.joined} of ${invite.capacity}` }),
            document.createTextNode(` joined · ${invite.is_open ? 'open' : 'closed'}`),
          ]),
          h('div', { class: 'panel-actions' }, [
            h('button', {
              class: 'button is-quiet', type: 'button',
              text: invite.is_open ? 'Close to new players' : 'Reopen',
              onclick: (event) => setOpen(season, !invite.is_open, event.target, load),
            }),
            h('button', {
              class: 'button is-quiet', type: 'button', text: 'New code',
              onclick: (event) => rotate(season, event.target, load),
            }),
            h('span', { class: 'join-note', text:
              'A new code stops the old one working. Players already in keep their seats.' }),
          ]),
        );
      })
      .catch((problem) => {
        clear(body);
        body.append(h('p', { class: 'panel-error', text: problem.message }));
      });
  };
  load();
  return body;
}

async function setOpen(season, isOpen, button, reload) {
  button.disabled = true;
  try {
    await api(`/seasons/${season.id}/invite`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...session.headers(season.id, { admin: true }) },
      body: JSON.stringify({ is_open: isOpen }),
    });
    reload();
  } catch (problem) {
    button.disabled = false;
    window.alert(problem.message);
  }
}

async function rotate(season, button, reload) {
  if (!window.confirm(
    'Issue a new join code?\n\nThe current code stops working immediately. '
    + 'Players who have already joined are unaffected.',
  )) return;
  button.disabled = true;
  try {
    await api(`/seasons/${season.id}/invite`, {
      method: 'POST',
      headers: session.headers(season.id, { admin: true }),
    });
    reload();
  } catch (problem) {
    button.disabled = false;
    window.alert(problem.message);
  }
}
