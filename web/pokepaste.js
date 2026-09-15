/* Showdown export format, and the format-legality checks around it.
 *
 * The paste is the thing a player actually leaves with: it imports into
 * Pokemon Showdown's teambuilder and into pokepast.es unchanged. Showdown's
 * parser is tolerant about field order and lenient about punctuation — it
 * strips non-alphanumerics before matching names — so "Never Melt Ice" and
 * "Never-Melt Ice" both resolve. That is what lets this module write names out
 * of PokeAPI's hyphenated ids without a display-name table.
 */

import { api } from './api.js';
import { prettify } from './pokeapi.js';

/* Every nature, and what it does: +10% to one stat, -10% to another.
 *
 * Five of the twenty-five are neutral — they name the same stat twice, so they
 * cancel. Those are listed with null rather than omitted, because "Hardy does
 * nothing" is information a player wants in the picker, not an absence.
 */
export const NATURES = {
  Adamant: ['atk', 'spa'], Bashful: null, Bold: ['def', 'atk'],
  Brave: ['atk', 'spe'], Calm: ['spd', 'atk'], Careful: ['spd', 'spa'],
  Docile: null, Gentle: ['spd', 'def'], Hardy: null, Hasty: ['spe', 'def'],
  Impish: ['def', 'spa'], Jolly: ['spe', 'spa'], Lax: ['def', 'spd'],
  Lonely: ['atk', 'def'], Mild: ['spa', 'def'], Modest: ['spa', 'atk'],
  Naive: ['spe', 'spd'], Naughty: ['atk', 'spd'], Quiet: ['spa', 'spe'],
  Quirky: null, Rash: ['spa', 'spd'], Relaxed: ['def', 'spe'],
  Sassy: ['spd', 'spe'], Serious: null, Timid: ['spe', 'atk'],
};

export const NATURE_NAMES = Object.keys(NATURES);

const STAT_LABEL = {
  hp: 'HP', atk: 'Atk', def: 'Def', spa: 'SpA', spd: 'SpD', spe: 'Spe',
};

/** "+Atk / -SpA", or "neutral" for the five that cancel out. */
export function natureEffect(nature) {
  const pair = NATURES[nature];
  if (!pair) return nature ? 'neutral' : '';
  return `+${STAT_LABEL[pair[0]]} / -${STAT_LABEL[pair[1]]}`;
}

/* Showdown's own order and abbreviations. EVs print in this order because a
 * paste that reads "252 Atk / 4 Def / 252 Spe" is what everyone recognises. */
export const EV_KEYS = [
  ['hp', 'HP'],
  ['atk', 'Atk'],
  ['def', 'Def'],
  ['spa', 'SpA'],
  ['spd', 'SpD'],
  ['spe', 'Spe'],
];

/* What a Showdown paste holds, and therefore what a lineup may not exceed.
   Not the league's roster size, which is how many you draft. */
export const BATTLE_TEAM_SIZE = 6;

export const EV_TOTAL_MAX = 508;
export const EV_STAT_MAX = 252;

/* 31 in everything is what a competitively bred Pokemon has, and what
 * Showdown assumes when a set says nothing. So 31 is the default here too,
 * and only a deliberate departure is written into the paste. */
export const IV_MAX = 31;
export const IV_DEFAULT = 31;

export const ivOf = (slot, key) =>
  (slot?.ivs?.[key] === undefined || slot.ivs[key] === null
    ? IV_DEFAULT : Number(slot.ivs[key]));

/* The spreads worth offering, and the base-stat shape each one suits.
 *
 * "Recommended" here means derived, not decreed: `recommendSpread` reads the
 * Pokemon's own base stats to decide whether it hits harder physically or
 * specially and whether it is built to take hits, then marks the matching
 * preset. A player is free to ignore it — these are starting points, and the
 * fourth EV goes somewhere defensive because 252/252/4 wastes nothing.
 */
export const EV_PRESETS = [
  { id: 'phys-sweep', name: 'Physical sweeper', nature: 'Adamant or Jolly',
    evs: { atk: 252, spe: 252, hp: 4 } },
  { id: 'spec-sweep', name: 'Special sweeper', nature: 'Modest or Timid',
    evs: { spa: 252, spe: 252, hp: 4 } },
  { id: 'bulky-phys', name: 'Bulky physical', nature: 'Adamant',
    evs: { hp: 252, atk: 252, spd: 4 } },
  { id: 'bulky-spec', name: 'Bulky special', nature: 'Modest',
    evs: { hp: 252, spa: 252, spd: 4 } },
  { id: 'phys-wall', name: 'Physical wall', nature: 'Impish or Bold',
    evs: { hp: 252, def: 252, spd: 4 } },
  { id: 'spec-wall', name: 'Special wall', nature: 'Careful or Calm',
    evs: { hp: 252, spd: 252, def: 4 } },
];

/** Which preset this Pokemon's base stats point at. */
export function recommendSpread(entry) {
  const base = entry?.stats || {};
  const stat = (name) => Number(base[name]) || 0;
  const physical = stat('attack') >= stat('special-attack');
  const offence = Math.max(stat('attack'), stat('special-attack')) + stat('speed');
  const bulk = stat('hp') + stat('defense') + stat('special-defense');

  const punch = Math.max(stat('attack'), stat('special-attack'));

  /* A wall is bulky, slow, *and* hits softly. The third test is the one that
   * earns its place: without it a bulky attacker like Kingambit — 135 Attack
   * behind 100/120/85 — reads as a wall on bulk alone and gets told to dump
   * 252 into Defence rather than into the stat it actually wins with. */
  if (bulk > offence + 60 && stat('speed') <= 75 && punch < 100) {
    return stat('defense') >= stat('special-defense') ? 'phys-wall' : 'spec-wall';
  }
  // Fast enough to move first against most things: hit and outspeed.
  if (stat('speed') >= 80) return physical ? 'phys-sweep' : 'spec-sweep';
  return physical ? 'bulky-phys' : 'bulky-spec';
}

export const evTotal = (evs) =>
  EV_KEYS.reduce((sum, [key]) => sum + (Number(evs?.[key]) || 0), 0);

// --------------------------------------------------------------- items

/* Showdown's held items, from the backend's /items route.
 *
 * Not PokeAPI's item index, which is every object in the games: bicycles, TMs,
 * key items, fossils, mail. Roughly 2200 entries of which a Pokemon can hold
 * maybe a tenth, so a player looking for Leftovers scrolled past the Yellow
 * Bike to find it.
 *
 * The backend reads Showdown's own data/items.ts instead, so everything here
 * is holdable, and pairs it with the one-line description Showdown ships for
 * each. Asking per format also drops what that format cannot use — Mega stones
 * are Past in Gen 9 OU and present in National Dex — and flags the items the
 * format bans outright.
 */
const itemCache = new Map();

export function itemCatalogue(formatKey) {
  const key = formatKey || '';
  if (!itemCache.has(key)) {
    const query = key ? `?format=${encodeURIComponent(key)}` : '';
    itemCache.set(
      key,
      api(`/items${query}`).catch(() => {
        // Autocomplete is a convenience; losing it must not break the editor,
        // and dropping the entry lets a later open retry rather than caching
        // the failure for the life of the page.
        itemCache.delete(key);
        return [];
      }),
    );
  }
  return itemCache.get(key);
}

/** The catalogue entry for a typed item name, matched the lenient way. */
export function findItem(catalogue, value) {
  if (!value) return null;
  const wanted = normalize(value);
  return catalogue.find((item) => normalize(item.name) === wanted) || null;
}

// ------------------------------------------------------------ the paste

/** One Pokemon, in Showdown export format. */
export function slotToPaste(slot, entry) {
  const species = entry?.display_name || prettify(slot.api_name);
  // Showdown's form is "Nickname (Species)", and the species stays in the
  // parentheses so the set still imports as the right Pokemon.
  const nickname = (slot.nickname || '').trim();
  const head = nickname && nickname !== species ? `${nickname} (${species})` : species;
  const lines = [slot.item ? `${head} @ ${slot.item}` : head];

  if (slot.ability) lines.push(`Ability: ${slot.ability}`);
  if (slot.tera) lines.push(`Tera Type: ${prettify(slot.tera)}`);

  /* Level is deliberately omitted. Showdown sets it from the format — LC is
   * level 5, most singles is 100 — so writing a level here would override the
   * format with a guess. */

  const evs = EV_KEYS.filter(([key]) => Number(slot.evs?.[key]) > 0)
    .map(([key, label]) => `${Number(slot.evs[key])} ${label}`);
  if (evs.length) lines.push(`EVs: ${evs.join(' / ')}`);

  /* Only IVs that are not 31 are written. Showdown fills in 31 for anything
   * unstated, so listing all six would be noise on every set — and the ones
   * that matter (0 Atk on a special attacker, 0 Spe under Trick Room) are
   * exactly the ones that survive this filter. */
  const ivs = EV_KEYS
    .filter(([key]) => ivOf(slot, key) !== IV_MAX)
    .map(([key, label]) => `${ivOf(slot, key)} ${label}`);
  if (ivs.length) lines.push(`IVs: ${ivs.join(' / ')}`);

  if (slot.nature) lines.push(`${slot.nature} Nature`);

  for (const move of slot.moves || []) {
    if (move) lines.push(`- ${move}`);
  }

  return lines.join('\n');
}

/** A whole plan. Blank line between Pokemon, which is what Showdown expects. */
export function planToPaste(plan, entryFor) {
  /* Only the Pokemon in the lineup. After the draft a plan holds every
   * Pokemon you own — more than the six a battle takes — so the paste is the
   * subset you marked as bringing, not the whole roster. */
  return (plan.slots || [])
    .filter((slot) => slot?.include !== false)
    .map((slot) => slotToPaste(slot, entryFor(slot.api_name)))
    .join('\n\n');
}

// ------------------------------------------------------- sending it out

/* pokepast.es takes a plain form POST at /create with these four fields —
 * confirmed against its own homepage form, not guessed.
 *
 * A form submission rather than fetch() on purpose: it is cross-origin, and
 * pokepaste sends no CORS headers, so fetch could post but never read the
 * resulting URL. A form targeted at a new tab hands the player the created
 * paste directly.
 *
 * This is the one action in the planner that leaves the device, so it is
 * always an explicit click and the UI says so.
 */
export function sendToPokepaste({ paste, title, author, notes }) {
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = 'https://pokepast.es/create';
  form.target = '_blank';
  form.rel = 'noopener';
  form.hidden = true;

  for (const [name, value] of Object.entries({ paste, title, author, notes })) {
    const field = document.createElement('input');
    field.type = 'hidden';
    field.name = name;
    field.value = value || '';
    form.append(field);
  }

  document.body.append(form);
  form.submit();
  form.remove();
}

// ------------------------------------------------------------ legality

/* Showdown matches names by stripping everything that is not a letter or a
 * digit, so this is the comparison to use against a ban list: it makes
 * "King's Rock", "kings rock" and "Kings-Rock" the same string. */
const normalize = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');

/** Format rules from GET /formats/{key}/rules, or null when unavailable. */
export async function loadRules(formatKey) {
  if (!formatKey) return null;
  try {
    return await api(`/formats/${formatKey}/rules`);
  } catch {
    /* The endpoint is new and the rule tables may not be populated yet. The
     * planner works without it; it just cannot flag banned items. */
    return null;
  }
}

/* Everything wrong with a plan, as a flat list of {level, text}.
 *
 * `level` is 'error' for something the league's own settings forbid — over
 * budget, too many Pokemon, a banned pick — and 'warn' for something that
 * needs a human: a Pokemon that has dropped out of the pool, a duplicate item
 * in a format with Item Clause.
 *
 * Nothing here blocks editing. A plan is a scratchpad, and a player working
 * out what fits wants to see "3 points over" rather than be stopped from
 * typing.
 */
/* `roster` turns this from a draft-budget check into a lineup check.
 *
 * Before the draft the questions are "can I afford these together" and "is
 * that too many". After it, both are settled — the draft enforced them, and
 * the points are spent — so asking them again would flag every legal team
 * that used its budget. What is left to get wrong is the lineup: bringing
 * seven, or bringing something you do not own.
 */
export function checkPlan(plan, { season, entryFor, rules, roster = null }) {
  const problems = [];
  const all = plan.slots || [];
  // Only the Pokemon being brought can break a rule about the team.
  const slots = roster ? all.filter((slot) => slot?.include !== false) : all;

  const banned = new Set((rules?.banned_other || []).map(normalize));
  const clauses = new Set((rules?.clauses || []).map(normalize));
  const bannedLabel = new Map(
    (rules?.banned_other || []).map((name) => [normalize(name), name]),
  );

  if (roster) {
    // --- the lineup, against what was drafted
    const owned = new Set(roster);
    for (const slot of all) {
      if (!owned.has(slot.api_name)) {
        problems.push({
          level: 'warn',
          text: `${entryFor(slot.api_name)?.display_name || prettify(slot.api_name)} is not on `
            + 'your drafted roster.',
        });
      }
    }
    if (slots.length > BATTLE_TEAM_SIZE) {
      problems.push({
        level: 'error',
        text: `${slots.length} Pokémon in the lineup — a Showdown team holds ${BATTLE_TEAM_SIZE}.`,
      });
    }
    if (!slots.length) {
      problems.push({ level: 'warn', text: 'Nothing in the lineup yet, so the paste is empty.' });
    }
  } else {
    // --- budget and roster, straight off the season
    const spend = slots.reduce((sum, slot) => sum + (entryFor(slot.api_name)?.cost || 0), 0);
    if (season && spend > season.budget) {
      problems.push({
        level: 'error',
        text: `${spend} points spent, ${season.budget} available — ${spend - season.budget} over.`,
      });
    }
    if (season && slots.length > season.roster_size) {
      problems.push({
        level: 'error',
        text: `${slots.length} Pokémon, roster size is ${season.roster_size}.`,
      });
    }
  }

  // --- the pool. Cost only matters before the draft: afterwards the pick is
  // made and what it went for is history, so an unpriced entry is not a
  // problem with the lineup.
  for (const slot of slots) {
    const entry = entryFor(slot.api_name);
    if (!entry) {
      problems.push({
        level: 'warn',
        text: `${prettify(slot.api_name)} is not in this season's pool any more.`,
      });
    } else if (entry.banned) {
      problems.push({ level: 'error', text: `${entry.display_name} is banned in this season.` });
    } else if (!entry.cost && !roster) {
      problems.push({ level: 'warn', text: `${entry.display_name} has no cost set yet.` });
    }
  }

  // --- Species Clause, which every standard format carries
  const seen = new Map();
  for (const slot of slots) {
    seen.set(slot.api_name, (seen.get(slot.api_name) || 0) + 1);
  }
  for (const [apiName, count] of seen) {
    if (count > 1) {
      problems.push({
        level: 'error',
        text: `${entryFor(apiName)?.display_name || prettify(apiName)} is on the team ${count} times.`,
      });
    }
  }

  // --- Item Clause, only when the format actually has it
  if (clauses.has('itemclause')) {
    const items = new Map();
    for (const slot of slots) {
      if (!slot.item) continue;
      items.set(normalize(slot.item), (items.get(normalize(slot.item)) || 0) + 1);
    }
    for (const [, count] of items) {
      if (count > 1) {
        problems.push({ level: 'warn', text: 'Item Clause: two Pokémon hold the same item.' });
        break;
      }
    }
  }

  // --- item, ability and move bans from the format's own rule list
  for (const slot of slots) {
    const name = entryFor(slot.api_name)?.display_name || prettify(slot.api_name);
    for (const [label, value] of [
      ['item', slot.item],
      ['ability', slot.ability],
      ...(slot.moves || []).filter(Boolean).map((move) => ['move', move]),
    ]) {
      if (value && banned.has(normalize(value))) {
        problems.push({
          level: 'error',
          text: `${name}: ${bannedLabel.get(normalize(value)) || value} is banned in this format (${label}).`,
        });
      }
    }
  }

  return problems;
}

/** True when this exact value is on the format's ban list. For per-field marks. */
export function isBanned(value, rules) {
  if (!value || !rules?.banned_other?.length) return false;
  return rules.banned_other.some((name) => normalize(name) === normalize(value));
}
