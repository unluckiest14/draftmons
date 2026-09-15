"""Pools: everything about a season's draft board.

This is where the two data sources meet. `format_service` says which Pokemon
are legal; `pokeapi` supplies their stats, types and sprites; this module joins
them and owns the pool tables.

The join is the interesting part. Showdown ids strip every non-alphanumeric
character, PokeAPI hyphenates:

    Showdown   greattusk   landorustherian   mrmime
    PokeAPI    great-tusk  landorus-therian  mr-mime

Stripping is lossy, so Showdown -> PokeAPI cannot be computed. The map is built
in the direction that works: ask PokeAPI for its index of every Pokemon, strip
each name the same way Showdown would, and key on that. Deterministic, no
guessing. The exceptions are default forms and truncated descriptors, which
live in FORM_ALIASES; anything still unmatched is reported, never dropped
silently.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from typing import Any, NamedTuple

from pokeapi import PokeApiClient, PokeApiNotFound

# Showdown id -> PokeAPI name, for the cases stripping cannot reach.
FORM_ALIASES: dict[str, str] = {
    # Default forms: Showdown uses the bare species name, PokeAPI suffixes it.
    "deoxys": "deoxys-normal", "wormadam": "wormadam-plant",
    "giratina": "giratina-altered", "shaymin": "shaymin-land",
    "basculin": "basculin-red-striped", "darmanitan": "darmanitan-standard",
    "tornadus": "tornadus-incarnate", "thundurus": "thundurus-incarnate",
    "landorus": "landorus-incarnate", "enamorus": "enamorus-incarnate",
    "keldeo": "keldeo-ordinary", "meloetta": "meloetta-aria",
    "meowstic": "meowstic-male", "aegislash": "aegislash-shield",
    "pumpkaboo": "pumpkaboo-average", "gourgeist": "gourgeist-average",
    "zygarde": "zygarde-50", "oricorio": "oricorio-baile",
    "lycanroc": "lycanroc-midday", "wishiwashi": "wishiwashi-solo",
    "minior": "minior-red-meteor", "mimikyu": "mimikyu-disguised",
    "toxtricity": "toxtricity-amped", "eiscue": "eiscue-ice",
    "indeedee": "indeedee-male", "morpeko": "morpeko-full-belly",
    "urshifu": "urshifu-single-strike", "basculegion": "basculegion-male",
    "oinkologne": "oinkologne-male", "maushold": "maushold-family-of-four",
    "squawkabilly": "squawkabilly-green-plumage", "palafin": "palafin-zero",
    "tatsugiri": "tatsugiri-curly", "dudunsparce": "dudunsparce-two-segment",
    # No gimmighoul entry: PokeAPI names the chest form plainly "gimmighoul",
    # so the stripped index already matches it. Aliasing it to the descriptive
    # "gimmighoul-chest" that Showdown implies is a 404.
    # Hyphens PokeAPI keeps inside a single word.
    "mimejr": "mime-jr", "typenull": "type-null", "porygonz": "porygon-z",
    "jangmoo": "jangmo-o", "hakamoo": "hakamo-o", "kommoo": "kommo-o",
    "hooh": "ho-oh", "nidoranf": "nidoran-f", "nidoranm": "nidoran-m",
    "greattusk": "great-tusk", "screamtail": "scream-tail",
    "brutebonnet": "brute-bonnet", "fluttermane": "flutter-mane",
    "slitherwing": "slither-wing", "sandyshocks": "sandy-shocks",
    "irontreads": "iron-treads", "ironbundle": "iron-bundle",
    "ironhands": "iron-hands", "ironjugulis": "iron-jugulis",
    "ironmoth": "iron-moth", "ironthorns": "iron-thorns",
    "ironvaliant": "iron-valiant", "ironleaves": "iron-leaves",
    "ironboulder": "iron-boulder", "ironcrown": "iron-crown",
    "roaringmoon": "roaring-moon", "walkingwake": "walking-wake",
    "gougingfire": "gouging-fire", "ragingbolt": "raging-bolt",
    "wochien": "wo-chien", "chienpao": "chien-pao",
    "tinglu": "ting-lu", "chiyu": "chi-yu",
    # Showdown drops a trailing descriptor PokeAPI keeps.
    "taurospaldeacombat": "tauros-paldea-combat-breed",
    "taurospaldeablaze": "tauros-paldea-blaze-breed",
    "taurospaldeaaqua": "tauros-paldea-aqua-breed",
    "ogerponwellspring": "ogerpon-wellspring-mask",
    "ogerponhearthflame": "ogerpon-hearthflame-mask",
    "ogerponcornerstone": "ogerpon-cornerstone-mask",
    # Showdown writes a female form "-f"; PokeAPI spells it out.
    "basculegionf": "basculegion-female", "indeedeef": "indeedee-female",
    "oinkolognef": "oinkologne-female",
    # Showdown's default is the bare species, but PokeAPI only ships the
    # gendered forms for these three, so the bare name 404s.
    "frillish": "frillish-male", "jellicent": "jellicent-male",
    "pyroar": "pyroar-male",
    # Descriptors the two sources spell differently.
    "darmanitangalar": "darmanitan-galar-standard",
    "necrozmadawnwings": "necrozma-dawn", "necrozmaduskmane": "necrozma-dusk",
    "rockruffdusk": "rockruff-own-tempo",
    # The movie-cap Pikachus, which PokeAPI suffixes "-cap".
    **{f"pikachu{cap}": f"pikachu-{cap}-cap" for cap in
       ("original", "hoenn", "sinnoh", "unova", "kalos", "alola", "partner", "world")},
}

# Formes PokeAPI serves only under `pokemon-form`, never `pokemon`.
#
# A Silvally memory or a Genesect drive changes the Pokemon's type but not its
# base stats, and PokeAPI models exactly that: `pokemon-form/silvally-fire`
# exists and carries the Fire typing, while `pokemon/silvally-fire` is a 404
# because there are no separate stats to serve. `hydrate` therefore reads the
# stats from the base species and the typing from the form.
#
# Leaving these out is not an option: they are legal, distinctly-typed picks,
# and collapsing all eighteen Silvallys onto one Normal-type row would put a
# single entry on the board under whichever memory happened to be written last.
FORM_OVERLAYS: dict[str, str] = {
    **{f"silvally-{kind}": "silvally" for kind in (
        "bug", "dark", "dragon", "electric", "fairy", "fighting", "fire",
        "flying", "ghost", "grass", "ground", "ice", "poison", "psychic",
        "rock", "steel", "water",
    )},
    **{f"genesect-{drive}": "genesect" for drive in
       ("burn", "chill", "douse", "shock")},
}


class PoolError(Exception):
    """A pool operation the caller should be told about, with a usable message."""


def showdown_id(name: str) -> str:
    """Strip a name the way Showdown builds its ids. Lossy; a lookup key only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ------------------------------------------------------- the name join


async def pokeapi_index(client: PokeApiClient) -> dict[str, str]:
    """{showdown_id: pokeapi_name} for every Pokemon PokeAPI serves. One call.

    The `pokemon` listing is the whole index bar the form-only entries, which
    are folded in afterwards so `resolve` needs to know nothing about them.
    """
    payload = await client.get("pokemon?limit=100000")
    index = {showdown_id(row["name"]): row["name"] for row in payload["results"]}
    index.update({showdown_id(form): form for form in FORM_OVERLAYS})
    return index


def resolve(
    species: dict[str, str], index: dict[str, str]
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """{showdown_id: (pokeapi_name, tier)}, plus the ids that matched nothing.

    The alias table is checked first: "deoxys" is a valid key in both the alias
    table and the stripped index, and only the alias is right.
    """
    resolved: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []
    for sid, tier in species.items():
        if sid in FORM_ALIASES:
            resolved[sid] = (FORM_ALIASES[sid], tier)
        elif sid in index:
            resolved[sid] = (index[sid], tier)
        else:
            unresolved.append(sid)
    return resolved, sorted(unresolved)


# ---------------------------------------------------------- hydration


async def hydrate(client: PokeApiClient, api_name: str) -> dict[str, Any] | None:
    """One PokeAPI call per Pokemon. No species call — Showdown already ruled.

    Returns the fields the draft board and the detail view actually use. Two
    sprite sizes on purpose: official artwork is a few hundred KB, so a grid of
    200 of them is a multi-megabyte page load on someone's phone.

    A form-only entry costs a second call: its stats come from the base species
    and its typing from `pokemon-form`, because that is how PokeAPI splits a
    Silvally memory. It keeps the form's own name, so the eighteen Silvallys
    stay eighteen rows rather than collapsing onto one.
    """
    base_name = FORM_OVERLAYS.get(api_name)
    try:
        mon = await client.pokemon(base_name or api_name)
    except PokeApiNotFound:
        return None

    name = api_name if base_name else mon["name"]
    types = [t["type"]["name"] for t in mon.get("types", [])]
    sprites = mon.get("sprites") or {}
    form_sprites: dict[str, Any] = {}

    if base_name:
        try:
            form = await client.get(f"pokemon-form/{api_name}")
        except PokeApiNotFound:
            return None
        # The whole reason for the second call — anything else would report
        # every Silvally as Normal.
        types = [t["type"]["name"] for t in form.get("types", [])] or types
        form_sprites = form.get("sprites") or {}

    stats = {s["stat"]["name"]: s["base_stat"] for s in mon.get("stats", [])}
    artwork = ((sprites.get("other") or {}).get("official-artwork") or {}).get("front_default")
    return {
        "api_name": name,
        "showdown_id": showdown_id(name),
        "display_name": name.replace("-", " ").title(),
        "types": types,
        "stats": stats,
        "bst": sum(stats.values()) or None,
        # A form has its own sprite but no official artwork, so the detail view
        # falls back to the base species' render rather than showing nothing.
        "sprite_url": form_sprites.get("front_default") or sprites.get("front_default"),
        "artwork_url": artwork,
    }


# --------------------------------------------------------------- build


class FetchedPool(NamedTuple):
    """The PokeAPI half of a build, ready to be written."""

    items: list[dict[str, Any]]
    tiers: dict[str, str]
    unresolved: list[str]


async def fetch_pool(
    client: PokeApiClient, format_species: dict[str, str]
) -> FetchedPool:
    """Every PokeAPI call a build needs, and no database handle on purpose.

    Roughly one call per Pokemon — ~770 for Gen 9 OU, ~1200 for National Dex —
    which is seconds of network even with the client's concurrency cap. The
    caller opens its write transaction *after* this returns, because SQLite
    takes a database-wide write lock and holding one across this fetch would
    stall every other writer for the duration.
    """
    index = await pokeapi_index(client)
    resolved, unresolved = resolve(format_species, index)

    results = await asyncio.gather(
        *(hydrate(client, name) for name, _ in resolved.values()),
        return_exceptions=True,
    )
    return FetchedPool(
        items=[item for item in results if isinstance(item, dict)],
        tiers={name: tier for name, tier in resolved.values()},
        unresolved=unresolved,
    )


def store_pool(
    conn: sqlite3.Connection,
    season_id: int,
    format_key: str,
    fetched: FetchedPool,
    tier_snapshot: str,
) -> dict[str, Any]:
    """Write a fetched pool. Pure SQL, so the write lock is held for milliseconds.

    Costs are left at whatever they already were, and default to 0 for new
    rows. Point values need human judgement, and rebuilding a pool must not
    silently wipe the prices someone spent an evening setting.
    """
    tiers = fetched.tiers
    added = 0
    for item in fetched.items:
        conn.execute(
            "INSERT INTO pool_entry (season_id, api_name, showdown_id, display_name, "
            "  types, stats, bst, sprite_url, artwork_url, tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            # DO UPDATE, not INSERT OR REPLACE. REPLACE deletes the row and
            # reinserts it with a new id, orphaning any pick that references
            # it — and it would reset cost to the default, losing the pricing.
            "ON CONFLICT (season_id, api_name) DO UPDATE SET "
            "  showdown_id = excluded.showdown_id, display_name = excluded.display_name, "
            "  types = excluded.types, stats = excluded.stats, bst = excluded.bst, "
            "  sprite_url = excluded.sprite_url, artwork_url = excluded.artwork_url, "
            "  tier = excluded.tier",
            (
                season_id, item["api_name"], item["showdown_id"], item["display_name"],
                json.dumps(item["types"]), json.dumps(item["stats"]), item["bst"],
                item["sprite_url"], item["artwork_url"], tiers.get(item["api_name"]),
            ),
        )
        added += 1

    conn.execute(
        "UPDATE season SET format_key = ?, tier_snapshot = ? WHERE id = ?",
        (format_key, tier_snapshot, season_id),
    )
    priced = conn.execute(
        "SELECT COUNT(*) AS n FROM pool_entry WHERE season_id = ? AND cost > 0",
        (season_id,),
    ).fetchone()["n"]

    return {"added": added, "priced": priced, "unresolved": fetched.unresolved}


async def add_one(
    conn: sqlite3.Connection,
    client: PokeApiClient,
    season_id: int,
    name: str,
    cost: int | None = None,
) -> sqlite3.Row:
    """Add a single Pokemon by PokeAPI name or Showdown id.

    Deliberately does not check the season's format. This is the commissioner's
    override: for a Pokemon the format missed, one that came back in
    `unresolved`, or a house rule that allows something off-tier. The format
    decides the default pool, not what the commissioner is permitted to do.
    """
    candidates = [name.lower(), FORM_ALIASES.get(showdown_id(name), "")]
    item = None
    for candidate in filter(None, candidates):
        item = await hydrate(client, candidate)
        if item:
            break
    if item is None:
        raise PoolError(
            f"PokeAPI has no Pokemon called {name!r}. "
            "Names are hyphenated there: 'great-tusk', not 'greattusk' or 'Great Tusk'."
        )

    conn.execute(
        "INSERT INTO pool_entry (season_id, api_name, showdown_id, display_name, "
        "  types, stats, bst, sprite_url, artwork_url, cost) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (season_id, api_name) DO UPDATE SET "
        "  types = excluded.types, stats = excluded.stats, bst = excluded.bst, "
        "  sprite_url = excluded.sprite_url, artwork_url = excluded.artwork_url, "
        # Only overwrite the cost when one was supplied, so re-adding a Pokemon
        # to refresh its data does not wipe a price someone already set.
        "  cost = CASE WHEN ? IS NULL THEN pool_entry.cost ELSE excluded.cost END",
        (
            season_id, item["api_name"], item["showdown_id"], item["display_name"],
            json.dumps(item["types"]), json.dumps(item["stats"]), item["bst"],
            item["sprite_url"], item["artwork_url"], cost or 0, cost,
        ),
    )
    return conn.execute(
        "SELECT * FROM pool_entry WHERE season_id = ? AND api_name = ?",
        (season_id, item["api_name"]),
    ).fetchone()


# A line that is purely a list marker: "1.", "-", "*", "•".
LIST_MARKER = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")

# "Great Tusk 19" ends in a cost; "Porygon2" does not. The space is what
# separates them, which is why this needs the \s and cannot just look for a
# trailing digit.
TRAILING_COST = re.compile(r"^(.*\S)\s+(-?\d+)$")


def parse_species_list(text: str) -> list[tuple[str, int | None]]:
    """Parse a dropped-in custom format file into (name, cost) pairs.

    A commissioner's Pokemon list arrives in whatever shape their league
    already keeps it, so this accepts all of them rather than making them
    convert first:

        JSON  ["Great Tusk", "Kingambit"]
              [{"name": "Great Tusk", "cost": 19}, ...]
              {"Great Tusk": 19, "Kingambit": 18}
        text  Great Tusk
              Great Tusk, 19
              Great Tusk\t19
              Great Tusk 19
              - Great Tusk          (bullets and "1." numbering)
              Great Tusk @ Booster Energy   (a Showdown export)

    The cost is optional throughout — `None` means "not priced yet", which is
    the normal state for a custom format, since pricing is a separate step the
    commissioner does afterwards.
    """
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        # Anything opening with a bracket was meant as JSON, so a parse failure
        # is a broken file, not a text list. Falling through to the line reader
        # would "succeed" with a single Pokemon named `["Great Tusk",]`, and
        # the commissioner would be left hunting for a typo in a name they
        # never typed.
        return _parse_json_list(stripped)

    rows: list[tuple[str, int | None]] = []
    for line in stripped.splitlines():
        line = line.split("#", 1)[0].split("//", 1)[0].strip()
        if not line:
            continue
        line = LIST_MARKER.sub("", line)
        # A Showdown export line: "Great Tusk @ Booster Energy".
        line = line.split("@", 1)[0].strip()
        if not line:
            continue

        name, cost = line, None
        for separator in (",", "\t"):
            if separator in line:
                head, _, tail = line.rpartition(separator)
                tail = tail.strip()
                if head.strip() and tail.lstrip("-").isdigit():
                    name, cost = head.strip(), int(tail)
                else:
                    name = line.replace("\t", " ").strip()
                break
        else:
            match = TRAILING_COST.match(line)
            if match:
                name, cost = match.group(1), int(match.group(2))

        if name:
            rows.append((name, cost))
    return rows


def _parse_json_list(text: str) -> list[tuple[str, int | None]]:
    """The three JSON shapes. Raises PoolError on a file that will not parse.

    json5 first, when it is installed, because a hand-maintained Pokemon list
    picks up trailing commas and unquoted keys, and rejecting one over a comma
    helps nobody.
    """
    data: Any = None
    try:
        import json5  # type: ignore
    except ImportError:
        json5 = None

    for loader in (json5.loads if json5 else None, json.loads):
        if loader is None:
            continue
        try:
            data = loader(text)
            break
        except Exception:
            continue
    else:
        raise PoolError(
            "That file starts with '[' or '{' but is not valid JSON. "
            "Check for a missing comma or bracket, or save it as a plain "
            "text list with one Pokemon per line."
        )

    rows: list[tuple[str, int | None]] = []
    if isinstance(data, dict):
        # Either {"Great Tusk": 19} or a wrapper like {"pokemon": [...]}.
        for key in ("pokemon", "species", "pool", "list", "names"):
            if isinstance(data.get(key), list):
                return _parse_json_list(json.dumps(data[key]))
        if not data:
            raise PoolError("That JSON file is empty.")
        for name, cost in data.items():
            rows.append((str(name), cost if isinstance(cost, int) else None))
        return rows

    if not isinstance(data, list):
        raise PoolError(
            "JSON must be a list of names, a list of objects with a \"name\", "
            "or an object mapping name to cost."
        )

    for item in data:
        if isinstance(item, str):
            rows.append((item, None))
        elif isinstance(item, dict):
            name = next(
                (item[k] for k in ("name", "pokemon", "species", "id") if item.get(k)),
                None,
            )
            if name is None:
                continue
            cost = next(
                (item[k] for k in ("cost", "points", "price", "value")
                 if isinstance(item.get(k), int)),
                None,
            )
            rows.append((str(name), cost))
    return rows


class ResolvedList(NamedTuple):
    """A custom list after PokeAPI has been asked about every name."""

    items: list[tuple[dict[str, Any], int | None]]
    unmatched: list[dict[str, Any]]


async def resolve_many(
    client: PokeApiClient, rows: list[tuple[str, int | None]]
) -> ResolvedList:
    """Look up every name in a custom list. No database handle, by design.

    Resolution goes through the same stripped index the format build uses, so
    a custom list may spell a name any of the ways a league actually writes
    one — "Great Tusk", "great-tusk" or "greattusk" all land on the same
    Pokemon.
    """
    index = await pokeapi_index(client)

    async def one(name: str) -> dict[str, Any] | None:
        sid = showdown_id(name)
        candidate = FORM_ALIASES.get(sid) or index.get(sid) or name.strip().lower()
        return await hydrate(client, candidate)

    fetched = await asyncio.gather(
        *(one(name) for name, _ in rows), return_exceptions=True
    )

    items: list[tuple[dict[str, Any], int | None]] = []
    unmatched: list[dict[str, Any]] = []
    for (name, cost), result in zip(rows, fetched):
        if isinstance(result, dict):
            items.append((result, cost))
        else:
            unmatched.append({"name": name, "cost": cost, "tried": showdown_id(name)})
    return ResolvedList(items=items, unmatched=unmatched)


def store_custom(
    conn: sqlite3.Connection,
    season_id: int,
    label: str,
    resolved: ResolvedList,
    *,
    replace: bool = True,
) -> dict[str, Any]:
    """Write a custom pool. Pure SQL, so the write lock is held briefly.

    `replace` clears the season's existing entries first, which is what makes
    re-dropping a corrected file behave like a correction rather than a merge.
    It is destructive on purpose and the API defaults it to on, because the
    alternative — silently accumulating two half-pools — is the confusing one.
    """
    removed = 0
    if replace:
        removed = conn.execute(
            "DELETE FROM pool_entry WHERE season_id = ?", (season_id,)
        ).rowcount

    priced = 0
    for item, cost in resolved.items:
        conn.execute(
            "INSERT INTO pool_entry (season_id, api_name, showdown_id, display_name, "
            "  types, stats, bst, sprite_url, artwork_url, cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (season_id, api_name) DO UPDATE SET "
            "  types = excluded.types, stats = excluded.stats, bst = excluded.bst, "
            "  sprite_url = excluded.sprite_url, artwork_url = excluded.artwork_url, "
            "  cost = CASE WHEN ? IS NULL THEN pool_entry.cost ELSE excluded.cost END",
            (
                season_id, item["api_name"], item["showdown_id"], item["display_name"],
                json.dumps(item["types"]), json.dumps(item["stats"]), item["bst"],
                item["sprite_url"], item["artwork_url"], cost or 0, cost,
            ),
        )
        if cost:
            priced += 1

    conn.execute(
        "UPDATE season SET format_key = 'custom', tier_snapshot = NULL, "
        "  pool_label = ? WHERE id = ?",
        (label, season_id),
    )
    return {
        "label": label,
        "submitted": len(resolved.items) + len(resolved.unmatched),
        "added": len(resolved.items),
        "priced": priced,
        "removed": removed,
        "unmatched": resolved.unmatched,
    }


def parse_cost_list(text: str) -> list[tuple[str, int]]:
    """Parse a pasted cost list: "Great Tusk, 19" per line.

    Accepts commas, tabs, or the last whitespace-separated field as the cost,
    because a spreadsheet export is whatever the spreadsheet felt like that
    day. Lines without a trailing number are skipped as headers or notes.
    """
    rows: list[tuple[str, int]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for separator in (",", "\t"):
            if separator in line:
                name, _, cost = line.rpartition(separator)
                break
        else:
            name, _, cost = line.rpartition(" ")
        name, cost = name.strip(), cost.strip()
        if name and cost.lstrip("-").isdigit():
            rows.append((name, int(cost)))
    return rows


async def add_many(
    conn: sqlite3.Connection,
    client: PokeApiClient,
    season_id: int,
    rows: list[tuple[str, int | None]],
) -> dict[str, Any]:
    """Add a whole pasted cost list. Unmatched names are reported, never guessed.

    This is the path out of the spreadsheet: paste the list the league already
    has, keep the point values, and let the commissioner fix the handful of
    names that do not resolve. Silently substituting a similar-looking Pokemon
    would not be found until someone drafted it.
    """
    added, unmatched = [], []
    for name, cost in rows:
        try:
            entry = await add_one(conn, client, season_id, name, cost)
        except PoolError:
            unmatched.append({"name": name, "cost": cost, "tried": showdown_id(name)})
            continue
        added.append(entry["api_name"])
    return {"submitted": len(rows), "added": len(added), "unmatched": unmatched}


# --------------------------------------------------------------- reads


def list_pool(
    conn: sqlite3.Connection,
    season_id: int,
    *,
    type_: str | None = None,
    max_cost: int | None = None,
    search: str | None = None,
    include_banned: bool = False,
    unpriced_only: bool = False,
    limit: int = 1000,
) -> list[sqlite3.Row]:
    """The filtered board. Filtering in SQL, because a pool can be 800 rows."""
    sql = ["SELECT * FROM pool_entry WHERE season_id = ?"]
    args: list[Any] = [season_id]

    if not include_banned:
        sql.append("AND banned = 0")
    if max_cost is not None:
        sql.append("AND cost <= ?")
        args.append(max_cost)
    if unpriced_only:
        sql.append("AND cost <= 0")
    if search:
        sql.append("AND display_name LIKE ?")
        args.append(f"%{search}%")
    if type_:
        # types is a JSON array in a text column, so this is a substring match
        # on the encoded form. Exact enough because type names never overlap
        # as substrings of one another.
        sql.append("AND types LIKE ?")
        args.append(f'%"{type_}"%')

    sql.append("ORDER BY cost DESC, bst DESC, display_name LIMIT ?")
    args.append(min(limit, 2000))
    return conn.execute(" ".join(sql), args).fetchall()


def get_entry(conn: sqlite3.Connection, season_id: int, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pool_entry WHERE id = ? AND season_id = ?", (entry_id, season_id)
    ).fetchone()


def summary(conn: sqlite3.Connection, season_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "  SUM(CASE WHEN cost > 0 THEN 1 ELSE 0 END) AS priced, "
        "  SUM(banned) AS banned, MAX(cost) AS max_cost "
        "FROM pool_entry WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    return {
        "total": row["total"],
        "priced": row["priced"] or 0,
        "unpriced": row["total"] - (row["priced"] or 0),
        "banned": row["banned"] or 0,
        "max_cost": row["max_cost"] or 0,
    }


# -------------------------------------------------------------- writes


def _locate(conn: sqlite3.Connection, season_id: int, name: str) -> sqlite3.Row | None:
    """Find an entry by api_name or showdown_id, so callers can use either."""
    return conn.execute(
        "SELECT id FROM pool_entry WHERE season_id = ? AND (api_name = ? OR showdown_id = ?)",
        (season_id, name, showdown_id(name)),
    ).fetchone()


def set_costs(conn: sqlite3.Connection, season_id: int, costs: dict[str, int]) -> dict[str, Any]:
    """Price the pool. Unknown names are reported rather than ignored."""
    updated, unknown = 0, []
    for name, cost in costs.items():
        row = _locate(conn, season_id, name)
        if row is None:
            unknown.append(name)
            continue
        conn.execute("UPDATE pool_entry SET cost = ? WHERE id = ?", (cost, row["id"]))
        updated += 1
    return {"updated": updated, "unknown": sorted(unknown), **summary(conn, season_id)}


def set_banned(
    conn: sqlite3.Connection, season_id: int, names: list[str], banned: bool
) -> dict[str, Any]:
    """House rules on top of the format."""
    updated, unknown = 0, []
    for name in names:
        row = _locate(conn, season_id, name)
        if row is None:
            unknown.append(name)
            continue
        conn.execute(
            "UPDATE pool_entry SET banned = ? WHERE id = ?", (1 if banned else 0, row["id"])
        )
        updated += 1
    return {"updated": updated, "unknown": sorted(unknown), "banned": banned}
