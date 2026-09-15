"""Held items, as Showdown actually implements them.

PokeAPI's /item endpoint lists roughly two thousand things, because it lists
everything the games contain: bicycles, TMs, key items, mail, fossils. Almost
none of them can be held in a battle, so an item picker built from it asks a
player to scroll past the Yellow Bike to reach Leftovers.

Showdown's own data/items.ts is the authoritative answer to "what can a
Pokemon hold", and it is much smaller — 583 entries, no bicycles. Its companion
data/text/items.ts carries a one-line shortDesc for every one of them, which is
the description this module hands to the picker.

Parsed the same way as config/formats.ts: both files are TypeScript modules
full of event handlers, so the object literals are walked by brace depth and
only the declarative fields are lifted out.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

from showdown_rules import RulesetError, strip_comments, top_level_objects

HERE = Path(__file__).resolve().parent

ITEMS_TS = str(HERE / "items.ts")
ITEMS_TEXT_TS = str(HERE / "items-text.ts")

UPSTREAM_ITEMS_TS = (
    "https://raw.githubusercontent.com/smogon/pokemon-showdown/master/data/items.ts"
)
UPSTREAM_ITEMS_TEXT_TS = (
    "https://raw.githubusercontent.com/smogon/pokemon-showdown/master/data/text/items.ts"
)

ITEMS_DECL = re.compile(r"export\s+const\s+Items[^=]*=\s*\{")
ITEMS_TEXT_DECL = re.compile(r"export\s+const\s+ItemsText[^=]*=\s*\{")

# isNonstandard values each kind of format still admits. National Dex is the
# whole reason this is not simply "reject anything nonstandard": Mega stones and
# Z-crystals are tagged Past, and they are the point of the format.
#
# "Future" is never admitted — those are items from a generation Showdown has
# data for but does not yet play.
ALLOWED_NONSTANDARD: dict[str, frozenset[str]] = {
    "singles": frozenset(),
    "doubles": frozenset(),
    "natdex": frozenset({"Past", "Unobtainable"}),
}


class Item(NamedTuple):
    id: str
    name: str
    description: str
    gen: int
    # Position in Showdown's itemicons sprite sheet: 24x24 cells, 16 per row.
    # Carried through so the picker can show what an item looks like without a
    # second source of item art to keep in step with this one.
    spritenum: int
    nonstandard: str | None
    is_pokeball: bool
    is_berry: bool
    mega_stone: bool
    z_crystal: bool
    users: list[str]


def item_id(name: str) -> str:
    """Showdown's id for an item name. "King's Rock" -> "kingsrock"."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _string(block: str, field: str) -> str | None:
    match = re.search(rf"\b{field}:\s*(['\"])((?:[^\\]|\\.)*?)\1", block)
    return re.sub(r"\\(.)", r"\1", match.group(2)) if match else None


def _number(block: str, field: str) -> int | None:
    match = re.search(rf"\b{field}:\s*(-?\d+)", block)
    return int(match.group(1)) if match else None


def _present(block: str, field: str) -> bool:
    return re.search(rf"^\s*{field}:", block, re.MULTILINE) is not None


def parse_items(raw: str) -> dict[str, Item]:
    """data/items.ts -> {item id: Item}, descriptions left blank."""
    text = strip_comments(raw)
    match = ITEMS_DECL.search(text)
    if not match:
        raise RulesetError("could not find the Items declaration — wrong file?")

    items: dict[str, Item] = {}
    for block in top_level_objects(text, match.end() - 1):
        name = _string(block, "name")
        if not name:
            continue
        users = re.search(r"\bitemUser:\s*\[([^\]]*)\]", block)
        items[item_id(name)] = Item(
            id=item_id(name),
            name=name,
            description="",
            gen=_number(block, "gen") or 0,
            spritenum=_number(block, "spritenum") if _number(block, "spritenum") is not None else -1,
            nonstandard=_string(block, "isNonstandard"),
            is_pokeball=_present(block, "isPokeball"),
            is_berry=_present(block, "isBerry"),
            mega_stone=_present(block, "megaStone"),
            z_crystal=_present(block, "zMove"),
            users=[u for u in re.findall(r"['\"]([^'\"]+)['\"]", users.group(1))] if users else [],
        )
    return items


def parse_descriptions(raw: str) -> dict[str, str]:
    """data/text/items.ts -> {item id: shortDesc}."""
    text = strip_comments(raw)
    match = ITEMS_TEXT_DECL.search(text)
    if not match:
        raise RulesetError("could not find the ItemsText declaration — wrong file?")

    out: dict[str, str] = {}
    for block in top_level_objects(text, match.end() - 1):
        name = _string(block, "name")
        if not name:
            continue
        # shortDesc is the one-liner; desc is the full rules text, which some
        # entries have instead when the behaviour needs a paragraph.
        out[item_id(name)] = _string(block, "shortDesc") or _string(block, "desc") or ""
    return out


def load(items_raw: str, text_raw: str) -> dict[str, Item]:
    """Both files, joined on item id."""
    descriptions = parse_descriptions(text_raw)
    return {
        key: item._replace(description=descriptions.get(key, ""))
        for key, item in parse_items(items_raw).items()
    }


def usable(
    items: dict[str, Item], ladder: str = "singles", generation: int = 9
) -> list[Item]:
    """The items a player can actually put on a Pokemon in this kind of format.

    Three exclusions, and each drops a different kind of unusable thing:

      * Poke Balls — Showdown carries them so a set can record which ball a
        Pokemon was caught in, which is cosmetic. Nothing holds one in battle.
      * items from a later generation than the format plays.
      * isNonstandard, unless the ladder admits it. This is what keeps Mega
        stones out of Gen 9 OU while leaving them in National Dex.
    """
    allowed = ALLOWED_NONSTANDARD.get(ladder, frozenset())
    return sorted(
        (
            item for item in items.values()
            if not item.is_pokeball
            and item.gen <= generation
            and (item.nonstandard is None or item.nonstandard in allowed)
        ),
        key=lambda item: item.name,
    )


# --------------------------------------------------------------- caching

_CACHE: dict[str, Item] | None = None
_MISSING = False


def load_cached() -> dict[str, Item]:
    """Parse the item files once per process. Empty dict if they are absent.

    Missing files are not fatal, for the same reason config/formats.ts being
    missing is not: the rest of the app works, and an item picker that falls
    back to free text is a degraded picker rather than a broken planner.
    """
    global _CACHE, _MISSING
    if _CACHE is not None:
        return _CACHE
    if _MISSING:
        return {}
    try:
        items_raw = Path(ITEMS_TS).read_text(encoding="utf-8", errors="replace")
        text_raw = Path(ITEMS_TEXT_TS).read_text(encoding="utf-8", errors="replace")
        loaded = load(items_raw, text_raw)
        # The sanity check belongs here rather than in the parser: this is the
        # only place a *wrong file* can be handed in, and the parser itself has
        # to stay usable on a handful of entries for tests.
        if len(loaded) < 400:
            raise RulesetError(
                f"only {len(loaded)} items in {ITEMS_TS} — is that really "
                "Showdown's data/items.ts?"
            )
        _CACHE = loaded
    except (OSError, RulesetError):
        _MISSING = True
        return {}
    return _CACHE
