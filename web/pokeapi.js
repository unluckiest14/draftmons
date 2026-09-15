/* Client-side PokeAPI access, for the detail view only.
 *
 * The board itself never touches PokeAPI: pool_entry already carries name,
 * cost, types, stats and sprites, so the grid renders from our own API in one
 * request. But abilities and movepool are not stored in pool_entry, and
 * hydrating 800 Pokemon with them at build time would mean storing a megabyte
 * of learnsets nobody has clicked on. So the detail panel fetches them on
 * demand, per Pokemon, and caches for the life of the page.
 *
 * Everything here is keyed on `api_name` — the hyphenated PokeAPI name the
 * backend already resolved and stored. That is the whole point of pool_entry
 * keeping it: the frontend never has to redo the Showdown -> PokeAPI join.
 */

const BASE = 'https://pokeapi.co/api/v2';

/* One promise per URL, not one payload per URL. Caching the promise means two
 * near-simultaneous callers (a fast double-click, a move that appears in two
 * Pokemon's movepools) share a single request instead of racing. */
const inflight = new Map();

function get(path) {
  if (!inflight.has(path)) {
    inflight.set(
      path,
      fetch(`${BASE}/${path}`).then((response) => {
        if (!response.ok) {
          // Drop the rejection from the cache so a transient failure can be
          // retried by reopening the panel, rather than being cached forever.
          inflight.delete(path);
          throw new Error(`PokeAPI ${response.status} for ${path}`);
        }
        return response.json();
      }),
    );
  }
  return inflight.get(path);
}

export const pokemon = (apiName) => get(`pokemon/${apiName}`);
export const ability = (name) => get(`ability/${name}`);
export const move = (name) => get(`move/${name}`);

// --------------------------------------------------------- generations

/* PokeAPI splits a generation into version groups, and a learnset is recorded
 * per version group. To answer "can this Pokemon learn Knock Off in the
 * format we draft", we need the groups belonging to the format's generation.
 *
 * Gen 9 is the common case and has exactly one group. The earlier entries
 * matter because a league can run an old-gen draft, and a Gen 5 movepool is
 * genuinely different from a Gen 9 one.
 */
const VERSION_GROUPS = {
  1: ['red-blue', 'yellow'],
  2: ['gold-silver', 'crystal'],
  3: ['ruby-sapphire', 'emerald', 'firered-leafgreen'],
  4: ['diamond-pearl', 'platinum', 'heartgold-soulsilver'],
  5: ['black-white', 'black-2-white-2'],
  6: ['x-y', 'omega-ruby-alpha-sapphire'],
  7: ['sun-moon', 'ultra-sun-ultra-moon', 'lets-go-pikachu-lets-go-eevee'],
  8: ['sword-shield', 'brilliant-diamond-and-shining-pearl', 'legends-arceus'],
  9: ['scarlet-violet'],
};

/* "gen9-ou" -> 9. Every format key the backend's LADDER produces is
 * gen<N>-<tier>, so the generation is derivable from the key and no extra
 * request is needed. Unknown shapes fall back to the current generation. */
export function generationOf(formatKey) {
  const match = /^gen(\d+)/.exec(formatKey || '');
  const gen = match ? Number(match[1]) : 9;
  return VERSION_GROUPS[gen] ? gen : 9;
}

export const LATEST_GEN = 9;
export const generations = () => Object.keys(VERSION_GROUPS).map(Number).sort((a, b) => a - b);

// ------------------------------------------------------------ movepool

const METHOD_LABELS = {
  'level-up': 'Level',
  machine: 'TM',
  egg: 'Egg',
  tutor: 'Tutor',
  'form-change': 'Form',
};

/* The movepool a format actually allows, out of the full cross-generation
 * learnset PokeAPI returns.
 *
 * A move can be learnable several ways in one generation — bred and then also
 * a TM in a later game of the same generation. We keep the cheapest single
 * description: the lowest level-up level if it levels up at all, otherwise the
 * first other method. Showing "Level 32 / TM / Egg" for one move is noise on a
 * screen whose job is "does this thing get Knock Off".
 */
export function movepoolFor(mon, generation) {
  const groups = new Set(VERSION_GROUPS[generation] || VERSION_GROUPS[LATEST_GEN]);
  const moves = [];

  for (const entry of mon.moves || []) {
    const details = (entry.version_group_details || []).filter((detail) =>
      groups.has(detail.version_group.name),
    );
    if (!details.length) continue;

    const levelUp = details
      .filter((detail) => detail.move_learn_method.name === 'level-up')
      .sort((a, b) => a.level_learned_at - b.level_learned_at)[0];
    const chosen = levelUp || details[0];
    const method = chosen.move_learn_method.name;

    moves.push({
      name: entry.move.name,
      label: prettify(entry.move.name),
      method,
      methodLabel: METHOD_LABELS[method] || prettify(method),
      level: method === 'level-up' ? chosen.level_learned_at : null,
    });
  }

  return moves.sort((a, b) => a.label.localeCompare(b.label));
}

/* Move type/category/power need one request per move, and a full movepool is
 * 100+ moves. So the panel renders names first and enriches after: this
 * resolves them a few at a time and reports each one as it lands, which keeps
 * the table usable immediately and never puts 120 requests on the wire at
 * once.
 *
 * `signal` is what makes closing the panel cheap — without it, clicking
 * through five Pokemon leaves five enrichment runs competing for the network.
 */
export async function enrichMoves(moves, onResolved, signal, concurrency = 6) {
  const queue = [...moves];

  const worker = async () => {
    while (queue.length) {
      if (signal?.aborted) return;
      const item = queue.shift();
      try {
        const detail = await move(item.name);
        if (signal?.aborted) return;
        onResolved(item.name, {
          type: detail.type?.name || null,
          category: detail.damage_class?.name || null,
          power: detail.power,
          accuracy: detail.accuracy,
          pp: detail.pp,
        });
      } catch {
        // A single move failing to resolve is not worth failing the panel
        // over; the row keeps its name and shows blank stats.
      }
    }
  };

  await Promise.all(Array.from({ length: concurrency }, worker));
}

// ----------------------------------------------------------- abilities

/* Abilities are 2-4 per Pokemon, so unlike moves they can all be fetched at
 * once. The effect text is the reason to bother: "Protosynthesis" tells a
 * drafter nothing the name did not already. */
export async function abilitiesFor(mon) {
  const slots = (mon.abilities || []).map((slot) => ({
    name: slot.ability.name,
    label: prettify(slot.ability.name),
    hidden: slot.is_hidden,
  }));

  const described = await Promise.all(
    slots.map(async (slot) => {
      try {
        const detail = await ability(slot.name);
        return { ...slot, text: effectText(detail) };
      } catch {
        return { ...slot, text: null };
      }
    }),
  );

  // Hidden ability last, matching how every Pokemon resource lists them.
  return described.sort((a, b) => Number(a.hidden) - Number(b.hidden));
}

/* PokeAPI gives a long `effect_entries` and a short `flavor_text_entries`.
 * The short form reads better in a panel; the long one is the fallback
 * because a handful of abilities have no flavor text in English. */
function effectText(detail) {
  const short = (detail.effect_entries || []).find((entry) => entry.language.name === 'en');
  if (short?.short_effect) return short.short_effect;
  const flavor = (detail.flavor_text_entries || []).find((entry) => entry.language.name === 'en');
  return flavor ? flavor.flavor_text.replace(/\s+/g, ' ') : null;
}

// ---------------------------------------------------------- type chart

/* Attacker -> defender -> multiplier, listing only the non-1x cases.
 * Gen 6+ chart, which covers every generation this tool targets.
 *
 * This is static because it has to be: computing it would mean 18 requests to
 * /type on first open, to reproduce a table that has not changed since 2013.
 */
const CHART = {
  normal: { rock: 0.5, ghost: 0, steel: 0.5 },
  fire: { fire: 0.5, water: 0.5, grass: 2, ice: 2, bug: 2, rock: 0.5, dragon: 0.5, steel: 2 },
  water: { fire: 2, water: 0.5, grass: 0.5, ground: 2, rock: 2, dragon: 0.5 },
  electric: { water: 2, electric: 0.5, grass: 0.5, ground: 0, flying: 2, dragon: 0.5 },
  grass: {
    fire: 0.5, water: 2, grass: 0.5, poison: 0.5, ground: 2,
    flying: 0.5, bug: 0.5, rock: 2, dragon: 0.5, steel: 0.5,
  },
  ice: { fire: 0.5, water: 0.5, grass: 2, ice: 0.5, ground: 2, flying: 2, dragon: 2, steel: 0.5 },
  fighting: {
    normal: 2, ice: 2, poison: 0.5, flying: 0.5, psychic: 0.5,
    bug: 0.5, rock: 2, ghost: 0, dark: 2, steel: 2, fairy: 0.5,
  },
  poison: { grass: 2, poison: 0.5, ground: 0.5, rock: 0.5, ghost: 0.5, steel: 0, fairy: 2 },
  ground: { fire: 2, electric: 2, grass: 0.5, poison: 2, flying: 0, bug: 0.5, rock: 2, steel: 2 },
  flying: { electric: 0.5, grass: 2, fighting: 2, bug: 2, rock: 0.5, steel: 0.5 },
  psychic: { fighting: 2, poison: 2, psychic: 0.5, dark: 0, steel: 0.5 },
  bug: {
    fire: 0.5, grass: 2, fighting: 0.5, poison: 0.5, flying: 0.5,
    psychic: 2, ghost: 0.5, dark: 2, steel: 0.5, fairy: 0.5,
  },
  rock: { fire: 2, ice: 2, fighting: 0.5, ground: 0.5, flying: 2, bug: 2, steel: 0.5 },
  ghost: { normal: 0, psychic: 2, ghost: 2, dark: 0.5 },
  dragon: { dragon: 2, steel: 0.5, fairy: 0 },
  dark: { fighting: 0.5, psychic: 2, ghost: 2, dark: 0.5, fairy: 0.5 },
  steel: { fire: 0.5, water: 0.5, electric: 0.5, ice: 2, rock: 2, steel: 0.5, fairy: 2 },
  fairy: { fire: 0.5, fighting: 2, poison: 0.5, dragon: 2, dark: 2, steel: 0.5 },
};

export const TYPES = Object.keys(CHART).sort();

/* How much each attacking type does to this Pokemon, as a multiplier.
 * Dual types multiply, which is what produces the 4x weaknesses that decide
 * half the picks in a draft league. */
export function matchups(types) {
  const result = {};
  for (const attacker of TYPES) {
    const multiplier = types.reduce(
      (total, defender) => total * (CHART[attacker]?.[defender] ?? 1),
      1,
    );
    if (multiplier !== 1) result[attacker] = multiplier;
  }
  return result;
}

export const prettify = (name) =>
  String(name || '')
    .split('-')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
