/* A team sheet: Pokémon slots, the set built on each, and the paste.
 *
 * Two tabs draw one of these and they ask different questions of it:
 *
 *   Planner   who do I want, out of the pool, inside the budget
 *   Roster    which of the ones I drafted am I bringing, and built how
 *
 * Everything that is the same either way lives here — the card, the set
 * editor, the notes, the export — because the set editor alone is three
 * hundred lines and a second copy of it is how the two tabs would drift apart
 * within a week. Everything that differs is passed in.
 *
 * `host` is how a tab lends this module its context and its writes:
 *
 *   season()            the season being planned
 *   entryFor(api_name)  the pool entry, for cost, types, sprite, stats
 *   rules()             the format's clauses and bans, for flagging a set
 *   formatKey()         which generation's movepool the editor offers
 *   save(plan, changes) persist — the tab owns its storage
 *   repaint()           redraw the tab after an edit
 *
 * Nothing here reads the pool directly or decides what a slot means; that is
 * the tab's business, and keeping it that way is what lets the same card show
 * a cost paid at the draft in one tab and a pool price in the other.
 */

import { h, $, clear, typePip, spriteFor, copyText, flashLabel } from './dom.js';
import * as poke from './pokeapi.js';
import * as paste from './pokepaste.js';

const ui = {};

/* Aborts the editor's PokeAPI work when it is closed, or reopened on another
 * Pokémon. Without it, clicking through six slots leaves six fetches racing to
 * write into a panel that has moved on. */
let editorRun = null;

export function init() {
  ui.drawer = $('#plan-drawer');
  ui.scrim = $('#scrim');
}

export function closeOverlays() {
  editorRun?.abort();
  editorRun = null;
  if (ui.drawer) {
    ui.drawer.hidden = true;
    clear(ui.drawer);
  }
}

// ----------------------------------------------------------- the problems

export function problemsView(problems) {
  return h(
    'ul',
    { class: 'problems' },
    problems.map((problem) =>
      h('li', { class: problem.level === 'error' ? 'problem is-error' : 'problem' }, [
        h('span', { class: 'problem-mark', text: problem.level === 'error' ? '!' : '?' }),
        h('span', { text: problem.text }),
      ]),
    ),
  );
}

// --------------------------------------------------------------- the card

/* One Pokémon on the sheet.
 *
 * `options` is the whole difference between the two tabs:
 *   costOf(slot, entry)   what to show in the corner badge
 *   classesFor(slot)      extra state classes — benched, not yours
 *   corner(slot, index)   the button top-right: remove, or bench
 *   footer(slot, index)   a row under the card — the Planner queues from here
 */
export function slotCard(host, plan, index, options = {}) {
  const slot = plan.slots[index];
  const entry = host.entryFor(slot.api_name);
  const rules = host.rules();
  const moves = (slot.moves || []).filter(Boolean);

  const cost = options.costOf ? options.costOf(slot, entry) : (entry?.cost || 0);
  const classes = ['slot', ...(entry ? [] : ['is-missing']), ...(options.classesFor?.(slot) || [])];

  const detail = [
    slot.item
      ? h('span', {
          class: paste.isBanned(slot.item, rules) ? 'slot-item is-banned' : 'slot-item',
          text: slot.item,
        })
      : h('span', { class: 'slot-item is-empty', text: 'no item' }),
    slot.ability ? h('span', { class: 'slot-ability', text: slot.ability }) : null,
  ];

  return h('div', { class: classes.join(' ') }, [
    h('button', {
      class: 'slot-open',
      type: 'button',
      title: `Edit ${entry?.display_name || slot.api_name}`,
      onclick: () => openSetEditor(host, plan, index),
    }, [
      h('div', { class: 'slot-top' }, [
        spriteFor(entry || {}, 'slot-sprite'),
        h('div', { class: 'slot-ident' }, [
          h('span', {
            class: 'slot-name',
            text: slot.nickname || entry?.display_name || poke.prettify(slot.api_name),
          }),
          h('span', { class: 'slot-types' },
            (entry?.types || []).map((type) => typePip(type))),
        ]),
        h('span', {
          class: cost ? 'slot-cost' : 'slot-cost is-unpriced',
          text: entry ? (cost ? String(cost) : '—') : '?',
        }),
      ]),
      h('div', { class: 'slot-detail' }, detail),
      moves.length
        ? h('div', { class: 'slot-moves' }, moves.map((move) =>
            h('span', {
              class: paste.isBanned(move, rules) ? 'slot-move is-banned' : 'slot-move',
              text: move,
            }),
          ))
        : h('div', { class: 'slot-moves' }, [
            h('span', { class: 'slot-move is-empty', text: 'no moves set' }),
          ]),
    ]),
    options.corner ? options.corner(slot, index) : null,
    options.footer ? options.footer(slot, index) : null,
  ]);
}

/** The corner button that takes a Pokémon off the sheet. Planner-shaped. */
export function removeButton(host, plan, index, label = 'Remove from plan') {
  return h('button', {
    class: 'slot-remove',
    type: 'button',
    title: label,
    text: '✕',
    onclick: () => {
      const next = [...plan.slots];
      next.splice(index, 1);
      host.save(plan, { slots: next });
      // The open editor addresses a slot by index, and every index after this
      // one has just moved.
      closeOverlays();
      ui.scrim.hidden = true;
      host.repaint();
    },
  });
}

// -------------------------------------------------------------- the notes

export function notesView(host, plan, placeholder) {
  return h('div', { class: 'plan-notes' }, [
    h('label', { class: 'field-label', for: 'plan-note-field', text: 'Notes' }),
    h('textarea', {
      id: 'plan-note-field',
      rows: '2',
      placeholder: placeholder || 'Why this team — matchups, backups, who to trade for.',
      onchange: (event) => host.save(plan, { notes: event.target.value }),
      text: plan.notes || '',
    }),
  ]);
}

// ------------------------------------------------------------- the export

/* The paste, and the three ways out of the app.
 *
 * `options` carries what only the tab knows: the title an opponent will read,
 * what to say when there is nothing to export yet, and the author to put on a
 * paste when the player has not typed one.
 */
export function exportView(host, plan, options = {}) {
  const text = paste.planToPaste(plan, host.entryFor);

  const preview = h('textarea', {
    class: 'paste-preview',
    rows: '8',
    readonly: true,
    spellcheck: 'false',
    'aria-label': 'Showdown paste',
    text: text || options.emptyText || '# Add a Pokémon to build a paste.',
  });

  const copyButton = h('button', { class: 'button', type: 'button', text: 'Copy paste' });
  copyButton.addEventListener('click', async () => {
    const ok = await copyText(text);
    flashLabel(copyButton, ok ? 'Copied' : 'Copy failed');
  });

  const downloadButton = h('button', {
    class: 'button is-quiet',
    type: 'button',
    text: 'Download .txt',
    onclick: () => downloadText(`${slugify(plan.name)}.txt`, text),
  });

  /* The only thing on these tabs that leaves the device, so it says so in as
   * many words and never fires without a click. */
  const sendButton = h('button', {
    class: 'button is-quiet',
    type: 'button',
    text: 'Open in PokePaste ↗',
    onclick: () => {
      if (!text) return;
      if (!confirm(
        'This uploads the team to pokepast.es, a public site, and opens it in a new tab.\n\n'
        + 'Anyone with the link can read it. Your plans stay private otherwise.',
      )) return;
      paste.sendToPokepaste({
        paste: text,
        title: options.title || plan.name,
        author: plan.author || options.author || '',
        notes: plan.notes || '',
      });
    },
  });

  return h('div', { class: 'section export' }, [
    h('h3', { class: 'section-title' }, [
      document.createTextNode('Showdown paste'),
      h('span', {
        class: 'section-note',
        text: options.note || 'imports into Showdown’s teambuilder as-is',
      }),
    ]),
    preview,
    h('div', { class: 'export-actions' }, [
      copyButton,
      downloadButton,
      sendButton,
      h('span', { class: 'export-note', text: 'Copy and download stay on this device.' }),
    ]),
  ]);
}

export const slugify = (value) =>
  String(value || 'plan').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'plan';

export function downloadText(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const link = h('a', { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  // Revoking immediately can race the download in some browsers; a tick is enough.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------- the set editor

/* Everything here is optional — a sheet is useful before any of it is filled
 * in — and everything saves as you type.
 *
 * Abilities and the legal movepool come from PokeAPI, filtered to the
 * generation the season's format runs, so an old-gen league is not offered a
 * Gen 9 move it cannot use. They arrive after the panel opens; the fields are
 * free text with autocomplete rather than selects, so the panel is usable
 * immediately and stays usable if PokeAPI is unreachable.
 */
export function openSetEditor(host, plan, index) {
  const slot = plan.slots[index];
  const entry = host.entryFor(slot.api_name);
  const rules = host.rules();
  const generation = poke.generationOf(host.formatKey() || host.season()?.format_key);

  editorRun?.abort();
  const run = new AbortController();
  editorRun = run;

  const update = (changes) => {
    Object.assign(plan.slots[index], changes);
    host.save(plan, { slots: plan.slots });
    host.repaint();
  };

  /* IVs, which are 31 unless someone deliberately drops one. The two that
   * matter in practice are 0 Atk on a special attacker, to take less from
   * Foul Play and confusion, and 0 Spe under Trick Room. */
  const ivFields = paste.EV_KEYS.map(([key, label]) =>
    h('label', { class: 'ev-field' }, [
      h('span', { text: label }),
      h('input', {
        type: 'number', min: '0', max: String(paste.IV_MAX), step: '1',
        value: String(paste.ivOf(plan.slots[index], key)),
        oninput: (event) => {
          const raw = event.target.value;
          const value = raw === '' ? paste.IV_DEFAULT
            : Math.max(0, Math.min(paste.IV_MAX, Number(raw) || 0));
          const ivs = { ...(plan.slots[index].ivs || {}) };
          // 31 is the default, so storing it would only bloat the plan.
          if (value === paste.IV_DEFAULT) delete ivs[key];
          else ivs[key] = value;
          plan.slots[index].ivs = ivs;
          host.save(plan, { slots: plan.slots });
        },
      }),
    ]));

  const resetIvs = h('button', {
    class: 'button', type: 'button', text: 'All 31',
    onclick: () => update({ ivs: {} }),
  });

  const moveList = h('datalist', { id: 'slot-moves' });
  const abilityList = h('datalist', { id: 'slot-abilities' });

  /* What the chosen item does, kept under the field. The item list is the one
   * part of a set a player is most likely not to know by heart, and a name on
   * its own — "Covert Cloak", "Clear Amulet" — does not say. */
  const itemNote = h('span', { class: 'editor-note' });
  const itemSelectedIcon = h('span', { class: 'item-icon is-empty' });
  let itemCatalogue = [];

  const describeItem = (value) => {
    clear(itemNote);
    const found = paste.findItem(itemCatalogue, value);
    paintIcon(itemSelectedIcon, found ? found.spritenum : -1);
    if (!value) {
      itemNote.append(document.createTextNode('No item. Type to search Showdown’s list.'));
      return;
    }
    if (!found) {
      itemNote.append(document.createTextNode(
        itemCatalogue.length
          ? 'Not a held item Showdown recognises in this format.'
          : 'held item',
      ));
      return;
    }
    if (found.banned) {
      itemNote.append(h('strong', { class: 'is-banned', text: 'Banned here. ' }));
    }
    itemNote.append(document.createTextNode(found.description || 'held item'));
    if (found.users?.length) {
      itemNote.append(h('em', { text: `  Only for ${found.users.join(', ')}.` }));
    }
  };

  /* A real dropdown rather than a <datalist>, because a datalist cannot show
   * an image and the icons are half the point: an item list is far quicker to
   * scan by picture than by name. The input stays free text, so anything
   * Showdown accepts can still be typed straight in. */
  const itemResults = h('div', { class: 'item-results', hidden: true });

  const showItems = (query) => {
    clear(itemResults);
    const needle = query.trim().toLowerCase();
    const matches = itemCatalogue
      .filter((item) => !needle || item.name.toLowerCase().includes(needle))
      .slice(0, 50);
    itemResults.hidden = matches.length === 0;

    for (const item of matches) {
      itemResults.append(h('button', {
        class: item.banned ? 'item-option is-banned' : 'item-option',
        type: 'button',
        // mousedown, not click: the input's blur fires first and would hide
        // this panel before a click ever landed on it.
        onmousedown: (event) => {
          event.preventDefault();
          itemField.value = item.name;
          itemResults.hidden = true;
          describeItem(item.name);
          update({ item: item.name });
        },
      }, [
        itemIcon(item.spritenum),
        h('span', { class: 'item-option-name', text: item.name }),
        h('span', { class: 'item-option-desc', text: item.description || '' }),
      ]));
    }
  };

  const itemField = h('input', {
    class: paste.isBanned(slot.item, rules) ? 'is-banned' : null,
    value: slot.item || '',
    placeholder: 'Leftovers, Choice Band…',
    autocomplete: 'off',
    oninput: (event) => { describeItem(event.target.value.trim()); showItems(event.target.value); },
    onfocus: (event) => showItems(event.target.value),
    onblur: () => { itemResults.hidden = true; },
    onchange: (event) => update({ item: event.target.value.trim() }),
  });
  describeItem(slot.item);

  const nicknameField = h('input', {
    type: 'text',
    value: slot.nickname || '',
    maxlength: '18',
    placeholder: entry?.display_name || 'no nickname',
    autocomplete: 'off',
    // Written on change rather than on input so a re-render does not steal
    // focus mid-word, the same reason the EV fields write straight through.
    onchange: (event) => update({ nickname: event.target.value.trim() }),
  });

  const abilityField = h('input', {
    class: paste.isBanned(slot.ability, rules) ? 'is-banned' : null,
    value: slot.ability || '',
    placeholder: 'loading…',
    list: 'slot-abilities',
    autocomplete: 'off',
    onchange: (event) => update({ ability: event.target.value.trim() }),
  });

  const teraField = h('select', {
    onchange: (event) => update({ tera: event.target.value }),
  }, [
    h('option', { value: '', text: '— none —' }),
    ...poke.TYPES.map((type) =>
      h('option', { value: type, text: poke.prettify(type), selected: slot.tera === type }),
    ),
  ]);

  /* Every nature with its own effect spelled out. A name alone does not say
   * anything — "Jolly" means nothing until you know it is +Spe / -SpA — and
   * the five neutral ones are marked rather than hidden, because choosing one
   * on purpose is a real thing to do. */
  const natureNote = h('span', { class: 'editor-note' });
  const paintNature = (value) => {
    natureNote.textContent = value
      ? paste.natureEffect(value)
      : 'No nature: every stat unmodified.';
    natureNote.classList.toggle('is-neutral', Boolean(value) && !paste.NATURES[value]);
  };

  const natureField = h('select', {
    onchange: (event) => { paintNature(event.target.value); update({ nature: event.target.value }); },
  }, [
    h('option', { value: '', text: '— none —' }),
    ...paste.NATURE_NAMES.map((nature) =>
      h('option', {
        value: nature,
        text: paste.NATURES[nature] ? `${nature}  ${paste.natureEffect(nature)}` : `${nature}  (neutral)`,
        selected: slot.nature === nature,
      }),
    ),
  ]);
  paintNature(slot.nature);

  const evInputs = {};
  const evTotalLabel = h('span', { class: 'ev-total' });
  const paintEvTotal = () => {
    const total = paste.evTotal(plan.slots[index].evs);
    evTotalLabel.textContent = `${total} / ${paste.EV_TOTAL_MAX}`;
    evTotalLabel.classList.toggle('is-over', total > paste.EV_TOTAL_MAX);
    evTotalLabel.title = paste.spreadText(plan.slots[index].evs) || 'No EVs';
  };

  /* Starting spreads. The recommended one is picked from this Pokemon's own
   * base stats rather than being the same for everything, which is the only
   * thing that makes a recommendation worth showing. */
  const recommended = paste.recommendSpread(entry);
  const presetButtons = paste.EV_PRESETS.map((preset) => h('button', {
    class: preset.id === recommended ? 'ev-preset is-recommended' : 'ev-preset',
    type: 'button',
    title: `${paste.spreadText(preset.evs)} · usually ${preset.nature}`,
    text: preset.id === recommended ? `${preset.name} ★` : preset.name,
    onclick: () => applySpread(preset.evs),
  }));

  /* Applying a spread has to move the boxes on screen, not just the data
   * behind them.
   *
   * `update()` repaints the roster list, not this drawer — the drawer is built
   * once and its <input>s keep whatever value they were created with. So a
   * preset used to change the stored EVs while the fields carried on showing
   * the old numbers, and the total, which is computed from storage, then
   * disagreed with everything visible: type 252/252/2 over a spread that
   * still had a stray 4 in it and the meter reads 510 for six digits that add
   * up to 506.
   *
   * Every stat is written, including the ones the spread leaves at zero, so
   * nothing survives from the spread before it. */
  const applySpread = (evs) => {
    const next = {};
    for (const [key] of paste.EV_KEYS) {
      if (Number(evs[key]) > 0) next[key] = Number(evs[key]);
    }
    plan.slots[index].evs = next;
    host.save(plan, { slots: plan.slots });
    for (const [key, field] of Object.entries(evInputs)) {
      field.value = next[key] ? String(next[key]) : '';
    }
    paintEvTotal();
    host.repaint();
  };

  const evFields = paste.EV_KEYS.map(([key, label]) => {
    const field = h('input', {
      type: 'number',
      min: '0',
      max: String(paste.EV_STAT_MAX),
      // 1, not 4: a spread may legitimately use a 2-point remainder, and a
      // step of 4 makes the arrows skip straight past it.
      step: '1',
      value: slot.evs?.[key] ? String(slot.evs[key]) : '',
      placeholder: '0',
      oninput: (event) => {
        const value = Math.max(0, Math.min(paste.EV_STAT_MAX, Number(event.target.value) || 0));
        const evs = { ...plan.slots[index].evs };
        if (value) evs[key] = value;
        else delete evs[key];
        // Written straight through rather than via update(), so a re-render
        // does not steal focus from the field being typed in.
        plan.slots[index].evs = evs;
        host.save(plan, { slots: plan.slots });
        paintEvTotal();
      },
    });
    // Kept so a preset can write into the boxes, not only into the data.
    evInputs[key] = field;
    return h('label', { class: 'ev-field' }, [h('span', { text: label }), field]);
  });
  paintEvTotal();

  const moveFields = [0, 1, 2, 3].map((moveIndex) =>
    h('input', {
      class: paste.isBanned(slot.moves?.[moveIndex], rules) ? 'is-banned' : null,
      value: slot.moves?.[moveIndex] || '',
      placeholder: `Move ${moveIndex + 1}`,
      list: 'slot-moves',
      autocomplete: 'off',
      'aria-label': `Move ${moveIndex + 1}`,
      onchange: (event) => {
        const moves = [...(plan.slots[index].moves || ['', '', '', ''])];
        moves[moveIndex] = event.target.value.trim();
        update({ moves });
      },
    }),
  );

  openDrawer([
    drawerHead(
      entry?.display_name || poke.prettify(slot.api_name),
      entry?.cost ? `${entry.cost} points · ${plan.name}` : plan.name,
      entry,
    ),
    h('div', { class: 'drawer-body editor' }, [
      editorField('Nickname', nicknameField, 'Optional. Shows in the paste as "Nickname (Species)".'),
      editorField('Item', h('div', { class: 'item-combo' }, [
        h('div', { class: 'item-input-row' }, [itemSelectedIcon, itemField]),
        itemResults,
      ]), itemNote),
      editorField('Ability', abilityField),
      h('div', { class: 'editor-pair' }, [
        editorField('Tera type', teraField),
        editorField('Nature', natureField, natureNote),
      ]),
      h('div', { class: 'editor-block' }, [
        h('div', { class: 'editor-label-row' }, [
          h('span', { class: 'field-label', text: 'EVs' }),
          evTotalLabel,
        ]),
        h('div', { class: 'ev-presets' }, presetButtons),
        h('div', { class: 'ev-grid' }, evFields),
      ]),
      h('div', { class: 'editor-block' }, [
        h('div', { class: 'editor-label-row' }, [
          h('span', { class: 'field-label', text: 'IVs' }),
          resetIvs,
        ]),
        h('div', { class: 'ev-grid' }, ivFields),
      ]),
      h('div', { class: 'editor-block' }, [
        h('span', { class: 'field-label', text: `Moves · Gen ${generation} legal` }),
        h('div', { class: 'move-grid' }, moveFields),
      ]),
      moveList,
      abilityList,
    ]),
  ]);

  hydrateEditor(host, {
    slot, generation, moveList, abilityList, abilityField,
    // The catalogue arrives after the field is on screen, so the note redraws
    // once it lands rather than staying blank for a Pokemon already chosen.
    onItems: (catalogue) => {
      itemCatalogue = catalogue;
      describeItem(itemField.value.trim());
    },
  }, run.signal);
}

async function hydrateEditor(host, parts, signal) {
  // Items are independent of the Pokemon, so they can load in parallel with it.
  paste.itemCatalogue(host.formatKey() || host.season()?.format_key).then((catalogue) => {
    if (signal.aborted) return;
    // No datalist for items any more: the dropdown built in the editor shows
    // an icon and a description per row, which a datalist cannot.
    parts.onItems?.(catalogue);
  });

  let mon;
  try {
    mon = await poke.pokemon(parts.slot.api_name);
  } catch {
    if (!signal.aborted) parts.abilityField.placeholder = 'PokeAPI unreachable — type it in';
    return;
  }
  if (signal.aborted) return;

  const abilities = (mon.abilities || []).map((entry) => poke.prettify(entry.ability.name));
  fillDatalist(parts.abilityList, abilities);
  parts.abilityField.placeholder = abilities[0] ? `e.g. ${abilities[0]}` : 'none listed';

  fillDatalist(
    parts.moveList,
    poke.movepoolFor(mon, parts.generation).map((move) => move.label),
  );
}

export function fillDatalist(list, values) {
  clear(list);
  for (const value of values) {
    // A string, or {value, label}. The label is what the browser shows beside
    // the name in the dropdown, which is where an item's description goes.
    if (typeof value === 'string') list.append(h('option', { value }));
    else list.append(h('option', { value: value.value, label: value.label || null }));
  }
}

/* Showdown ships every item icon in one sheet: 24x24 cells, sixteen per row,
 * addressed by the `spritenum` the API passes through from data/items.ts.
 * One request for the whole catalogue, and no second source of item art to
 * keep in step with the item list itself. */
const ITEM_SHEET = 'https://play.pokemonshowdown.com/sprites/itemicons-sheet.png';

function paintIcon(node, spritenum) {
  if (spritenum === undefined || spritenum === null || spritenum < 0) {
    node.className = 'item-icon is-empty';
    node.style.backgroundPosition = '';
    return;
  }
  node.className = 'item-icon';
  node.style.backgroundImage = `url(${ITEM_SHEET})`;
  node.style.backgroundPosition =
    `-${(spritenum % 16) * 24}px -${Math.floor(spritenum / 16) * 24}px`;
}

function itemIcon(spritenum) {
  const node = h('span', { class: 'item-icon' });
  paintIcon(node, spritenum);
  return node;
}

export function editorField(label, control, note) {
  return h('div', { class: 'editor-block' }, [
    h('span', { class: 'field-label', text: label }),
    control,
    // A string for a static hint, or an element the caller keeps updating —
    // which is how the item field shows what the chosen item actually does.
    typeof note === 'string' ? h('span', { class: 'editor-note', text: note })
      : note || null,
  ]);
}

export function drawerHead(title, subtitle, entry) {
  const art = entry?.artwork_url || entry?.sprite_url;
  return h('div', { class: 'drawer-head' }, [
    art ? h('img', { class: 'drawer-art', src: art, alt: '', decoding: 'async' }) : null,
    h('div', { class: 'drawer-ident' }, [
      h('h2', { class: 'drawer-title', text: title }),
      subtitle ? h('div', { class: 'drawer-sub', text: subtitle }) : null,
    ]),
    h('button', {
      class: 'detail-close',
      type: 'button',
      'aria-label': 'Close',
      text: '✕',
      onclick: () => {
        closeOverlays();
        ui.scrim.hidden = true;
      },
    }),
  ]);
}

export function openDrawer(children) {
  clear(ui.drawer);
  ui.drawer.append(...children);
  ui.drawer.hidden = false;
  ui.scrim.hidden = false;
  ui.drawer.scrollTop = 0;
}
