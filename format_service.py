"""Formats: parsing Showdown's data and keeping the database in step with it.

Two responsibilities that look similar but run at completely different times.

`refresh()` is the cron job's entry point. It reads Showdown's .js data, parses
it, works out what moved, and writes the format tables. Slow, deliberate, run
on a schedule.

Everything else is a query against those tables. Fast, runs inside requests.

Nothing here touches PokeAPI. Showdown says what is legal; PokeAPI supplies the
data; `pool_service` joins them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import showdown_rules as rules

# The file Showdown ships, kept in the project folder. Resolved against this
# module rather than the cwd so the cron job works from any directory.
DEFAULT_SOURCE = str(Path(__file__).resolve().parent / "formats-data.js")

# Where to re-download it when Smogon moves tiers.
UPSTREAM_SOURCE = "https://play.pokemonshowdown.com/data/formats-data.js"

# Smogon's singles ladder, most permissive first. A format admits its own tier
# and everything below it, so OU includes UUBL, UU, RU and so on but not Uber.
# The BL rungs are real tiers, not aliases: UUBL is OU-legal but UU-banned.
TIER_ORDER = [
    "AG", "Uber", "OU", "UUBL", "UU", "RUBL", "RU",
    "NUBL", "NU", "PUBL", "PU", "ZUBL", "ZU", "NFE", "LC",
]

# Tiers that are not a rung on any ladder. "Illegal" is Showdown's own marker
# for "not playable in this generation"; the CAP tiers (handled by prefix in
# `tier_of`) are the fan-designed Pokemon, which Showdown plays but PokeAPI
# has never heard of.
UNPLAYABLE = {"Illegal", "Unreleased", ""}


class Ladder(NamedTuple):
    """One of the three tier columns in formats-data.js, and how to read it.

    `field` is the key on a species record. `order` is that ladder's rungs,
    most permissive first. `allows` is the set of isNonstandard values the
    ladder still admits — empty for current-generation play, {"Past"} for
    National Dex, which is the entire point of National Dex.
    """

    field: str
    order: tuple[str, ...]
    allows: frozenset[str]


LADDERS: dict[str, Ladder] = {
    "singles": Ladder("tier", tuple(TIER_ORDER), frozenset()),
    # Doubles has three rungs. Everything Showdown does not rank for doubles
    # falls through to the bottom, which is what `rank` does with an unlisted
    # tier — so a doubles-unranked LC mon is DUU-legal, as it is on the ladder.
    "doubles": Ladder("doublesTier", ("DUber", "DOU", "DUU"), frozenset()),
    # National Dex stops at RU; it has no NU/PU/ZU. Past-generation Pokemon are
    # legal, which is why this is the one ladder with a non-empty `allows`.
    "natdex": Ladder(
        "natDexTier",
        ("AG", "Uber", "OU", "UUBL", "UU", "RUBL", "RU"),
        frozenset({"Past"}),
    ),
}


class FormatSpec(NamedTuple):
    label: str
    ladder: str
    ceiling: str
    # The format's name in config/formats.ts, when it differs from the label.
    # Only National Dex OU does: Showdown calls it plain "[Gen 9] National Dex".
    showdown: str = ""

    @property
    def showdown_name(self) -> str:
        return self.showdown or self.label


# format key -> what it is. The selectable list the frontend renders.
LADDER_FORMATS: dict[str, FormatSpec] = {
    "gen9-ag": FormatSpec("[Gen 9] Anything Goes", "singles", "AG"),
    "gen9-ubers": FormatSpec("[Gen 9] Ubers", "singles", "Uber"),
    "gen9-ou": FormatSpec("[Gen 9] OU", "singles", "OU"),
    "gen9-uu": FormatSpec("[Gen 9] UU", "singles", "UU"),
    "gen9-ru": FormatSpec("[Gen 9] RU", "singles", "RU"),
    "gen9-nu": FormatSpec("[Gen 9] NU", "singles", "NU"),
    "gen9-pu": FormatSpec("[Gen 9] PU", "singles", "PU"),
    "gen9-zu": FormatSpec("[Gen 9] ZU", "singles", "ZU"),
    "gen9-nfe": FormatSpec("[Gen 9] NFE", "singles", "NFE"),
    "gen9-lc": FormatSpec("[Gen 9] LC", "singles", "LC"),
    "gen9-doubles-ubers": FormatSpec("[Gen 9] Doubles Ubers", "doubles", "DUber"),
    "gen9-doubles-ou": FormatSpec("[Gen 9] Doubles OU", "doubles", "DOU"),
    "gen9-doubles-uu": FormatSpec("[Gen 9] Doubles UU", "doubles", "DUU"),
    "gen9-natdex-ag": FormatSpec("[Gen 9] National Dex AG", "natdex", "AG"),
    "gen9-natdex-ubers": FormatSpec("[Gen 9] National Dex Ubers", "natdex", "Uber"),
    "gen9-natdex-ou": FormatSpec(
        "[Gen 9] National Dex OU", "natdex", "OU", "[Gen 9] National Dex"
    ),
    "gen9-natdex-uu": FormatSpec("[Gen 9] National Dex UU", "natdex", "UU"),
    "gen9-natdex-ru": FormatSpec("[Gen 9] National Dex RU", "natdex", "RU"),
}

# The format whose membership is the full legal species list, so it is the
# baseline when diffing one refresh against the next.
BASELINE_FORMAT = "gen9-ag"


def apply_bans(
    members: dict[str, str], banned: list[str], unbanned: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Drop the species config/formats.ts bans outright. Exact ids only.

    This is the half of legality that tiers cannot express. Every one of the
    28 Pokemon [Gen 9] LC bans is tier "LC" in formats-data.js, so a pool built
    from tier data alone puts Gastly, Scyther, Sneasel and Murkrow on the board
    as legal picks.

    Matching is exact, deliberately — unlike `rules.is_species`, which is
    allowed to guess in order to *label* a token. Banning "Basculin-White-
    Striped" must not take out Basculin, and the only thing standing between
    those two is the refusal to fall back to a base species here.
    """
    banned_ids = {rules.normalise_species(token) for token in banned}
    spared = {rules.normalise_species(token) for token in unbanned}
    removed = sorted(
        sid for sid in members if sid in banned_ids and sid not in spared
    )
    gone = set(removed)
    return {sid: tier for sid, tier in members.items() if sid not in gone}, removed

ASSIGNMENT = re.compile(
    r"^\s*(?:(?:const|let|var)\s+)?(?:exports\.|module\.exports\.)?(\w+)\s*=\s*"
)


class FormatSourceError(RuntimeError):
    """The Showdown data could not be read or parsed."""


# ------------------------------------------------------------- parsing


def read_source(source: str) -> str:
    """Fetch a URL or read a local path, whichever it looks like."""
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "draftmons/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise FormatSourceError(f"could not fetch {source}: {exc}") from exc

    path = Path(source)
    if not path.exists():
        raise FormatSourceError(f"no such file: {source}")
    return path.read_text(encoding="utf-8", errors="replace")


def strip_comments(text: str) -> str:
    """Remove // and /* */ comments while leaving string literals alone.

    Must be a stateful pass, not a regex: `//.*` would eat the slashes inside
    "https://...". And it must run before brace matching, because the real file
    contains `// can't be used in battle` — an unpaired apostrophe inside a
    comment, which makes any quote-tracking brace matcher read the rest of the
    file as one enormous string.
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


def _matching_brace(text: str) -> int:
    if not text.startswith("{"):
        raise FormatSourceError(f"expected an object literal, got {text[:40]!r}")
    depth, quote, escaped = 0, None, False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise FormatSourceError("unbalanced braces — file may be truncated")


def js_to_dict(literal: str) -> dict[str, Any]:
    """Parse a JavaScript object literal.

    Uses json5 when installed, which handles all of JS object syntax. The
    fallback normalises the three things Showdown's files actually use;
    verified to give an identical result on the real file.
    """
    try:
        import json5  # type: ignore
    except ImportError:
        pass
    else:
        return json5.loads(literal)

    text = re.sub(
        r"'((?:[^'\\]|\\.)*)'",
        lambda m: json.dumps(m.group(1).replace("\\'", "'")),
        literal,
    )
    text = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatSourceError(
            f"parse failed after normalising: {exc}. Try: pip install json5"
        ) from exc


def parse_tiers(raw: str) -> tuple[str, dict[str, dict[str, Any]]]:
    """Showdown .js text -> (export name, {species_id: {tier, ...}})."""
    body = strip_comments(raw).lstrip()
    match = ASSIGNMENT.match(body)
    if not match:
        raise FormatSourceError(f"no assignment at the top of the file: {body[:60]!r}")

    literal = body[match.end():].strip()
    data = js_to_dict(literal[: _matching_brace(literal) + 1])

    species = {
        name: record
        for name, record in data.items()
        if isinstance(record, dict) and record.get("tier")
    }
    if len(species) < 800:
        raise FormatSourceError(
            f"only {len(species)} species have a tier — wrong file? "
            "Expected formats-data.js, not pokedex.js."
        )
    return match.group(1), species


def tier_of(record: dict[str, Any], ladder: Ladder) -> str:
    """A species' tier on one ladder, normalised.

    Two pieces of Showdown convention are applied here. A species with no
    entry in its ladder's column falls back to the singles `tier`, exactly as
    sim/dex-species.ts does — without that, the 329 Gen 9 Pokemon that carry no
    doublesTier would drop out of the doubles formats entirely. And a
    parenthesised tier such as "(OU)" means ranked-but-not-independently-used,
    not banned, so the parentheses come off: Mega Garchomp is National Dex OU.
    """
    raw = record.get(ladder.field) or record.get("tier") or ""
    return str(raw).strip().strip("()")


def rank(tier: str, ladder: Ladder) -> int:
    """How restricted a tier is: lower is more permissive.

    A tier the ladder does not list ranks at the bottom rather than raising.
    That is the correct reading, not a fallback — an unranked Pokemon is legal
    everywhere on that ladder, which is how DUU absorbs every doubles-unranked
    LC mon.
    """
    return ladder.order.index(tier) if tier in ladder.order else len(ladder.order)


def is_legal(record: dict[str, Any], ladder: Ladder, ceiling: str) -> bool:
    """Does this species pass every Showdown restriction for this format?

    Three separate gates, and all three matter:

      * the tier has to be a playable one — "Illegal" is Showdown's marker for
        a Pokemon that does not exist this generation, and the CAP tiers are
        fan designs that PokeAPI cannot supply art or stats for;
      * isNonstandard has to be one the ladder admits, which keeps Past-gen
        Pokemon, Gmax forms and event oddities out of standard play while
        letting National Dex have the Past ones;
      * the tier must not outrank the format's ceiling, which is the actual
        tier ban — Uber is banned from OU, DUber from DOU.
    """
    tier = tier_of(record, ladder)
    if tier in UNPLAYABLE or tier.startswith("CAP"):
        return False
    nonstandard = record.get("isNonstandard")
    if nonstandard and nonstandard not in ladder.allows:
        return False
    return rank(tier, ladder) >= rank(ceiling, ladder)


def species_for(
    species: dict[str, dict[str, Any]],
    ceiling: str,
    ladder: str | Ladder = "singles",
) -> dict[str, str]:
    """{species_id: tier} for everything a format with this ceiling admits."""
    resolved = LADDERS[ladder] if isinstance(ladder, str) else ladder
    return {
        name: tier_of(record, resolved)
        for name, record in species.items()
        if is_legal(record, resolved, ceiling)
    }


# ------------------------------------------------------------- database


def current_snapshot(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT source_sha256 FROM format LIMIT 1").fetchone()
    return row["source_sha256"] if row else None


def stored_tiers(conn: sqlite3.Connection) -> dict[str, str]:
    """{species_id: tier} across all formats, for diffing against a new build."""
    return {
        row["showdown_id"]: row["tier"]
        for row in conn.execute(
            "SELECT showdown_id, tier FROM format_species WHERE format_key = ?",
            (BASELINE_FORMAT,),
        )
    }


def diff_tiers(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
    """What moved. This is what a commissioner needs before deciding to update."""
    return [
        {"species": name, "from": before[name], "to": after[name]}
        for name in sorted(before.keys() & after.keys())
        if before[name] != after[name]
    ]


def load_rules() -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Parse config/formats.ts and data/rulesets.ts, or None if absent.

    Missing files are not fatal. The tier data alone still produces a usable
    pool, and refusing to build one because a second source is missing would
    be the wrong trade — but the caller is told, and `rules_applied` in the
    refresh report says which kind of build this was.
    """
    try:
        formats = rules.parse_formats(read_source(rules.FORMATS_TS))
        rulesets = rules.parse_rulesets(read_source(rules.RULESETS_TS))
    except (FormatSourceError, rules.RulesetError):
        return None
    return formats, rulesets


def refresh(
    conn: sqlite3.Connection,
    source: str = DEFAULT_SOURCE,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """The cron job. Fetch, parse, diff, write.

    Returns a report either way. When the upstream hash is unchanged it writes
    nothing but still logs the run, so "the cron job is alive and there was no
    change" is distinguishable from "the cron job stopped running".

    `force` rebuilds even when the hash matches — for when the parser changed
    rather than the data.
    """
    raw = read_source(source)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    existing = current_snapshot(conn)

    if digest == existing and not force:
        conn.execute(
            "INSERT INTO format_update (source, source_sha256, changed) VALUES (?, ?, 0)",
            (source, digest),
        )
        return {"changed": False, "source_sha256": digest, "moves": [], "formats": 0}

    export_name, species = parse_tiers(raw)
    before = stored_tiers(conn)
    after = species_for(species, "AG")   # AG admits everything, so it is the full map
    moves = diff_tiers(before, after)

    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources = load_rules()
    known_species = set(species)
    banned_total = 0
    written = 0

    for key, spec in LADDER_FORMATS.items():
        members = species_for(species, spec.ceiling, spec.ladder)
        removed: list[str] = []
        buckets: dict[str, list[str]] = {}
        resolved = None

        if sources is not None:
            formats_ts, rulesets_ts = sources
            resolved = rules.resolve(spec.showdown_name, formats_ts, rulesets_ts)
            buckets = rules.classify(resolved.bans, known_species)
            members, removed = apply_bans(
                members, buckets["species"], resolved.unbans
            )
        banned_total += len(removed)

        conn.execute(
            "INSERT INTO format (key, label, ladder, tier_ceiling, species_count, "
            "  species_banned, showdown_name, source_sha256, built_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  label = excluded.label, ladder = excluded.ladder, "
            "  tier_ceiling = excluded.tier_ceiling, "
            "  species_count = excluded.species_count, "
            "  species_banned = excluded.species_banned, "
            "  showdown_name = excluded.showdown_name, "
            "  source_sha256 = excluded.source_sha256, built_at = excluded.built_at",
            (key, spec.label, spec.ladder, spec.ceiling, len(members),
             len(removed), spec.showdown_name, digest, built_at),
        )

        conn.execute("DELETE FROM format_rule WHERE format_key = ?", (key,))
        if resolved is not None:
            conn.executemany(
                "INSERT OR IGNORE INTO format_rule (format_key, kind, value) "
                "VALUES (?, ?, ?)",
                [(key, "clause", value) for value in resolved.clauses]
                + [(key, "unban", value) for value in resolved.unbans]
                + [
                    (key, f"ban_{bucket}", value)
                    for bucket in ("species", "tier", "other", "complex")
                    for value in buckets.get(bucket, [])
                ],
            )
        # Replace the membership wholesale: a Pokemon leaving a tier has to
        # disappear from the format, and an upsert alone would leave it behind.
        conn.execute("DELETE FROM format_species WHERE format_key = ?", (key,))
        conn.executemany(
            "INSERT INTO format_species (format_key, showdown_id, tier) VALUES (?, ?, ?)",
            [(key, name, tier) for name, tier in sorted(members.items())],
        )
        written += 1

    conn.execute(
        "INSERT INTO format_update (source, source_sha256, changed, moves) VALUES (?, ?, 1, ?)",
        (source, digest, json.dumps(moves)),
    )
    return {
        "changed": True,
        "export": export_name,
        "source_sha256": digest,
        "previous_sha256": existing,
        "moves": moves,
        "formats": written,
        # False means config/formats.ts was missing and this build is tier-only
        # — every format still works, but LC and NFE will be over-inclusive.
        "rules_applied": sources is not None,
        "species_banned": banned_total,
    }


# ------------------------------------------------------------- queries


def list_formats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The selectable format list, grouped ladder by ladder.

    Ordered the way a commissioner reads it — singles, then doubles, then
    National Dex, each most permissive first — rather than alphabetically,
    which would put Doubles UU above Gen 9 OU.
    """
    ladder_rank = {name: i for i, name in enumerate(LADDERS)}
    catalogue = list(LADDER_FORMATS)
    return sorted(
        (dict(row) for row in conn.execute("SELECT * FROM format")),
        key=lambda row: (
            ladder_rank.get(row["ladder"], len(ladder_rank)),
            catalogue.index(row["key"]) if row["key"] in catalogue else len(catalogue),
        ),
    )


def get_format(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM format WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def format_species(conn: sqlite3.Connection, key: str) -> dict[str, str]:
    """{showdown_id: tier} for one format."""
    return {
        row["showdown_id"]: row["tier"]
        for row in conn.execute(
            "SELECT showdown_id, tier FROM format_species WHERE format_key = ?", (key,)
        )
    }


def format_rules(conn: sqlite3.Connection, key: str) -> dict[str, list[str]]:
    """Everything config/formats.ts says about one format, grouped by kind."""
    grouped: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT kind, value FROM format_rule WHERE format_key = ? ORDER BY kind, value",
        (key,),
    ):
        grouped.setdefault(row["kind"], []).append(row["value"])
    return grouped


def last_update(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM format_update ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["moves"] = json.loads(record["moves"])
    return record
