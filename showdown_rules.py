"""Showdown's rulesets: the bans that tiers alone do not express.

formats-data.js says what tier a Pokemon is in. That is most of legality, but
not all of it — Showdown's real banlists live in config/formats.ts, and some of
them are species bans that no tier implies:

    [Gen 9] LC   bans Gastly, Scyther, Sneasel, Murkrow and 25 others,
                 every one of them tier "LC" in formats-data.js

Read the tier data alone and those 29 Pokemon sit on the draft board looking
perfectly legal. That is the gap this module closes.

It also collects the rules a pool cannot express at all — Sleep Clause, the
Baton Pass ban, the Shadow Tag ban — because a commissioner still needs to see
them, and a team validator will eventually need to enforce them.

Two files, because Showdown splits them:

    config/formats.ts   the formats, each with a ruleset and a banlist
    data/rulesets.ts    what a named rule like "Standard" actually expands to

Neither is JSON, and neither can be made into JSON: both are TypeScript modules
full of validator functions. So this does not try to evaluate them. It walks
the top-level object literals and lifts out the handful of declarative fields
that matter — name, ruleset, banlist, unbanlist, restricted — and ignores every
function body it steps over.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, NamedTuple

HERE = Path(__file__).resolve().parent

FORMATS_TS = str(HERE / "formats.ts")
RULESETS_TS = str(HERE / "rulesets.ts")

UPSTREAM_FORMATS_TS = (
    "https://raw.githubusercontent.com/smogon/pokemon-showdown/master/config/formats.ts"
)
UPSTREAM_RULESETS_TS = (
    "https://raw.githubusercontent.com/smogon/pokemon-showdown/master/data/rulesets.ts"
)

# The declarative fields worth lifting. Everything else in a format entry is
# either cosmetic (desc, section, column) or a function.
LIST_FIELDS = ("ruleset", "banlist", "unbanlist", "restricted")

STRING_LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")

# Tier names that can appear in a banlist. These are the tier bans, and they
# are already expressed by a format's ceiling — see `classify`.
TIER_TOKENS = {
    "AG", "Uber", "Ubers", "OU", "UUBL", "UU", "RUBL", "RU", "NUBL", "NU",
    "PUBL", "PU", "ZUBL", "ZU", "NFE", "LC", "CAP", "CAP NFE", "CAP LC",
    "DUber", "DUbers", "DOU", "DUU", "DBL",
    "ND AG", "ND Uber", "ND Ubers", "ND OU", "ND UUBL", "ND UU", "ND RUBL",
    "ND RU", "ND NFE", "ND LC",
}

# Showdown writes a base forme as "Vulpix-Base" where the dex just says
# "Vulpix". Nothing else uses the suffix.
BASE_FORME = re.compile(r"-Base$")

# Pseudo-tokens from the "Obtainable" ruleset. They read like bans but name a
# legality category rather than a Pokemon, move or item, and formats-data.js
# already expresses them through isNonstandard.
LEGALITY_TAGS = {
    "Unreleased", "Unobtainable", "Nonexistent", "Past", "Future",
    "CAP", "Custom", "LGPE", "Pokestar", "Missing",
}


class RulesetError(RuntimeError):
    """The TypeScript sources could not be read or parsed."""


class Entry(NamedTuple):
    """One parsed object literal — a format, or a named rule."""

    name: str
    fields: dict[str, list[str]]
    scalars: dict[str, str]


class Resolved(NamedTuple):
    """A format's rules, with every inherited ruleset flattened in."""

    clauses: list[str]
    bans: list[str]
    unbans: list[str]


# --------------------------------------------------------------- parsing


def _unquote(match: re.Match[str]) -> str:
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    return re.sub(r"\\(.)", r"\1", raw)


def strings_in(text: str) -> list[str]:
    """Every string literal in a fragment, unescaped. Order preserved."""
    return [_unquote(m) for m in STRING_LITERAL.finditer(text)]


def strip_comments(text: str) -> str:
    """Remove // and /* */ comments, leaving string and template literals alone.

    Shares its reasoning with format_service.strip_comments: this cannot be a
    regex, because `//` occurs inside the URLs in these files' comments and an
    apostrophe occurs inside their prose.
    """
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if quote:
            out.append(char)
            if char == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
        elif char in "\"'`":
            quote = char
            out.append(char)
            i += 1
        elif char == "/" and i + 1 < n and text[i + 1] == "/":
            i = text.find("\n", i)
            if i == -1:
                break
        elif char == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def top_level_objects(text: str, start: int) -> Iterator[str]:
    """Yield each `{...}` sitting one level inside the container at `start`.

    A depth counter rather than a parser, which is the whole trick: a format's
    `onBegin()` body is full of braces and quotes, but it lives at a deeper
    depth, so it is stepped over as part of its enclosing object and never
    looked at.
    """
    depth = 0
    quote: str | None = None
    escaped = False
    begin = -1
    for i in range(start, len(text)):
        char = text[i]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'`":
            quote = char
            continue
        if char in "{[":
            depth += 1
            if depth == 2 and char == "{":
                begin = i
        elif char in "}]":
            depth -= 1
            if depth == 1 and begin != -1:
                yield text[begin:i + 1]
                begin = -1
            elif depth == 0:
                return


def array_after(block: str, field: str) -> str | None:
    """The body of `field: [...]`, matched by bracket depth rather than regex.

    A regex stopping at the first `]` is wrong here, and quietly so: a ruleset
    entry may name another format, and that name is "[Gen 9] OU" — brackets
    and all. Cutting at the first `]` returned an empty list for every format
    that inherits another, which is most of the ladder below OU.
    """
    found = re.search(rf"\b{field}:\s*\[", block)
    if not found:
        return None
    start = found.end()
    depth = 1
    quote: str | None = None
    escaped = False
    for i in range(start, len(block)):
        char = block[i]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'`":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return block[start:i]
    return None


def parse_entry(block: str) -> Entry | None:
    """Lift the declarative fields out of one object literal."""
    name_match = re.search(r"\bname:\s*(['\"])((?:[^\\]|\\.)*?)\1", block)
    if not name_match:
        return None
    name = re.sub(r"\\(.)", r"\1", name_match.group(2))

    fields: dict[str, list[str]] = {}
    for field in LIST_FIELDS:
        body = array_after(block, field)
        if body is not None:
            fields[field] = strings_in(body)

    scalars: dict[str, str] = {}
    for key in ("mod", "gameType"):
        found = re.search(rf"\b{key}:\s*(['\"])(.*?)\1", block)
        if found:
            scalars[key] = found.group(2)

    return Entry(name=name, fields=fields, scalars=scalars)


def parse_module(raw: str, declaration: re.Pattern[str], label: str) -> dict[str, Entry]:
    """Parse one TypeScript module into {entry name: Entry}."""
    text = strip_comments(raw)
    match = declaration.search(text)
    if not match:
        raise RulesetError(f"could not find the {label} declaration — wrong file?")

    entries: dict[str, Entry] = {}
    for block in top_level_objects(text, match.end() - 1):
        entry = parse_entry(block)
        if entry is not None:
            entries[entry.name] = entry
    if not entries:
        raise RulesetError(f"parsed no entries out of {label}")
    return entries


FORMATS_DECL = re.compile(r"export\s+const\s+Formats[^=]*=\s*\[")
RULESETS_DECL = re.compile(r"export\s+const\s+Rulesets[^=]*=\s*\{")


def parse_formats(raw: str) -> dict[str, Entry]:
    return parse_module(raw, FORMATS_DECL, "Formats")


def parse_rulesets(raw: str) -> dict[str, Entry]:
    return parse_module(raw, RULESETS_DECL, "Rulesets")


# ------------------------------------------------------------ resolution


def resolve(
    name: str,
    formats: dict[str, Entry],
    rulesets: dict[str, Entry],
) -> Resolved:
    """Flatten a format's rules, following every ruleset it inherits.

    Inheritance is real and load-bearing: [Gen 9] UU's whole definition is
    `ruleset: ['[Gen 9] OU'], banlist: ['OU', 'UUBL']`, so without following
    the reference UU would come back with no clauses and a two-line banlist.

    Directives are applied in the order Showdown lists them, because order is
    what makes an unban work — National Dex inherits Standard NatDex and then
    `+Past` re-admits what it excluded. A later directive wins.
    """
    clauses: list[str] = []
    bans: list[str] = []
    unbans: list[str] = []
    visiting: set[str] = set()

    def add(target: list[str], value: str) -> None:
        for other in (bans, unbans):
            if other is not target and value in other:
                other.remove(value)
        if value not in target:
            target.append(value)

    def walk(rule_name: str) -> None:
        if rule_name in visiting:
            return          # Showdown's rulesets do refer to each other
        visiting.add(rule_name)

        if rule_name in formats:
            # A format reference is inheritance, not a clause of its own:
            # [Gen 9] UU is "OU, plus these bans", and calling "[Gen 9] OU" a
            # clause of UU would be nonsense in the rules list.
            entry = formats[rule_name]
        else:
            # Every named rule is worth listing, whether or not rulesets.ts
            # expands it — "Sleep Clause Mod" is implemented in code and has no
            # sub-ruleset, but it is exactly what a commissioner needs to see.
            if rule_name not in clauses:
                clauses.append(rule_name)
            entry = rulesets.get(rule_name)
            if entry is None:
                return

        for item in entry.fields.get("ruleset", []):
            if item.startswith("!"):
                stripped = item[1:]
                if stripped in clauses:
                    clauses.remove(stripped)
            elif item.startswith("+"):
                add(unbans, item[1:])
            elif item.startswith("-"):
                add(bans, item[1:])
            else:
                walk(item)

        for item in entry.fields.get("banlist", []):
            add(bans, item)
        for item in entry.fields.get("unbanlist", []):
            add(unbans, item)
        # `restricted` is a softer category (one per team, not zero), so it is
        # reported as a clause rather than folded into the bans.
        for item in entry.fields.get("restricted", []):
            note = f"Restricted: {item}"
            if note not in clauses:
                clauses.append(note)

    walk(name)
    return Resolved(clauses=clauses, bans=bans, unbans=unbans)


# --------------------------------------------------------- classification


def normalise_species(token: str) -> str:
    """A ban token as a Showdown species id, if it looks like one."""
    return re.sub(r"[^a-z0-9]", "", BASE_FORME.sub("", token).lower())


def is_species(token: str, known_species: set[str]) -> bool:
    """Is this ban token a Pokemon?

    Exact match first, then the base species of a forme. The second test is
    needed because formats-data.js only tiers formes it ranks separately:
    Basculin-White-Striped is a real banned Pokemon but the tier file knows
    only "basculin", so an exact match alone files it under moves and the ban
    is never applied.

    The bias is deliberate. A move mistaken for a Pokemon bans nothing, since
    no pool member will match it; a Pokemon mistaken for a move leaves an
    illegal pick on the board.
    """
    if normalise_species(token) in known_species:
        return True
    base = BASE_FORME.sub("", token).split("-")[0]
    return bool(base) and normalise_species(base) in known_species


def classify(
    tokens: list[str], known_species: set[str]
) -> dict[str, list[str]]:
    """Split a banlist into what it actually bans.

    The species test is the data itself — is this token a Pokemon Showdown has
    a tier for — rather than a hand-kept list, which is the only way it stays
    right when Smogon bans something new. Whatever is left is a move, ability
    or item, and is kept as-is for display.
    """
    out: dict[str, list[str]] = {
        "tier": [], "species": [], "complex": [], "tag": [], "other": [],
    }
    for token in tokens:
        if token in TIER_TOKENS:
            out["tier"].append(token)
        elif token in LEGALITY_TAGS:
            out["tag"].append(token)
        elif any(op in token for op in (">", "++", "+")):
            # e.g. "Baton Pass > 1", "Drizzle ++ Swift Swim"
            out["complex"].append(token)
        elif is_species(token, known_species):
            out["species"].append(token)
        else:
            out["other"].append(token)
    return out
