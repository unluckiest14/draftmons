"""Tests for the config/formats.ts layer. No network, no real files.

The fixtures here are deliberately shaped like the real sources — a format
that inherits another by name, a ruleset that expands to more rulesets, an
apostrophe inside a banlist entry — because every one of those shapes broke
a first attempt at the parser.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import format_service as fmt          # noqa: E402
import showdown_rules as sr           # noqa: E402

FORMATS_TS = """
// A comment with a URL in it: https://example.com/thing
export const Formats: import('../sim/dex-formats').FormatList = [
\t{
\t\tsection: "S/V Singles",
\t},
\t{
\t\tname: "[Gen 9] OU",
\t\tdesc: `Some prose with an apostrophe: don't parse me.`,
\t\tmod: 'gen9',
\t\truleset: ['Standard', 'Sleep Moves Clause', '!Sleep Clause Mod'],
\t\tbanlist: ['Uber', 'AG', 'Moody', 'King\\'s Rock', 'Baton Pass'],
\t\tonBegin() {
\t\t\tif (this.foo) { this.bar({ baz: '}' }); }
\t\t},
\t},
\t{
\t\tname: "[Gen 9] UU",
\t\tmod: 'gen9',
\t\truleset: ['[Gen 9] OU'],
\t\tbanlist: ['OU', 'UUBL', 'Drizzle'],
\t},
\t{
\t\tname: "[Gen 9] National Dex",
\t\tmod: 'gen9',
\t\truleset: ['Standard', '+Past'],
\t\tbanlist: ['ND Uber', 'Shedinja'],
\t},
];
"""

RULESETS_TS = """
export const Rulesets: import('../sim/dex-formats').FormatDataTable = {
\tstandard: {
\t\teffectType: 'ValidatorRule',
\t\tname: 'Standard',
\t\truleset: ['Obtainable', 'Species Clause'],
\t\tbanlist: ['Nonexistent'],
\t},
\tobtainable: {
\t\teffectType: 'ValidatorRule',
\t\tname: 'Obtainable',
\t\tbanlist: ['Unreleased', 'Unobtainable'],
\t\tonValidateSet(set) {
\t\t\treturn set.name === '}' ? ['no'] : null;
\t\t},
\t},
};
"""


@pytest.fixture()
def sources():
    return sr.parse_formats(FORMATS_TS), sr.parse_rulesets(RULESETS_TS)


# ------------------------------------------------------------- parsing


def test_function_bodies_are_stepped_over_not_parsed(sources):
    """A brace or a quote inside onBegin() must not end the format object."""
    formats, _ = sources
    assert set(formats) == {"[Gen 9] OU", "[Gen 9] UU", "[Gen 9] National Dex"}
    assert formats["[Gen 9] OU"].scalars["mod"] == "gen9"


def test_an_escaped_apostrophe_survives_a_banlist(sources):
    formats, _ = sources
    assert "King's Rock" in formats["[Gen 9] OU"].fields["banlist"]


def test_a_ruleset_entry_may_contain_brackets(sources):
    """"[Gen 9] OU" is a ruleset entry whose own text contains "]".

    A regex that stops at the first "]" returns an empty ruleset here, which
    silently drops the entire inherited banlist of every tier below OU.
    """
    formats, _ = sources
    assert formats["[Gen 9] UU"].fields["ruleset"] == ["[Gen 9] OU"]


# ---------------------------------------------------------- resolution


def test_a_format_inherits_the_bans_of_the_format_it_references(sources):
    formats, rulesets = sources
    resolved = sr.resolve("[Gen 9] UU", formats, rulesets)
    assert "Uber" in resolved.bans          # from OU
    assert "UUBL" in resolved.bans          # its own
    assert "Baton Pass" in resolved.bans    # from OU


def test_named_rules_are_expanded_and_recorded(sources):
    formats, rulesets = sources
    resolved = sr.resolve("[Gen 9] OU", formats, rulesets)
    assert "Standard" in resolved.clauses        # the container
    assert "Species Clause" in resolved.clauses  # a leaf with no definition
    assert "Obtainable" in resolved.clauses      # expanded, and still listed
    assert "Unreleased" in resolved.bans         # pulled up from Obtainable


def test_a_bang_prefix_removes_an_inherited_clause(sources):
    formats, rulesets = sources
    assert "Sleep Clause Mod" not in sr.resolve("[Gen 9] OU", formats, rulesets).clauses


def test_a_plus_prefix_is_an_unban(sources):
    formats, rulesets = sources
    assert sr.resolve("[Gen 9] National Dex", formats, rulesets).unbans == ["Past"]


def test_a_format_reference_is_inheritance_not_a_clause(sources):
    formats, rulesets = sources
    assert "[Gen 9] OU" not in sr.resolve("[Gen 9] UU", formats, rulesets).clauses


# ------------------------------------------------------- classification


def test_bans_are_sorted_into_tiers_species_tags_and_the_rest(sources):
    formats, rulesets = sources
    known = {"shedinja", "basculin", "gastly"}
    buckets = sr.classify(sr.resolve("[Gen 9] National Dex", formats, rulesets).bans, known)
    assert buckets["tier"] == ["ND Uber"]
    assert buckets["species"] == ["Shedinja"]
    assert "Unreleased" in buckets["tag"]       # a legality tag, not an item
    assert "Nonexistent" in buckets["tag"]


def test_a_forme_is_recognised_through_its_base_species():
    """formats-data.js only tiers formes it ranks, so the base is the fallback."""
    assert sr.is_species("Basculin-White-Striped", {"basculin"})
    assert sr.is_species("Diglett-Base", {"diglett"})
    assert not sr.is_species("King's Rock", {"basculin", "diglett"})
    assert not sr.is_species("Will-O-Wisp", {"basculin"})


# -------------------------------------------------------- applying bans


def test_a_species_ban_is_applied_by_exact_id_only():
    """Banning a forme must not take out the base species it was matched via.

    `is_species` is allowed to guess in order to label a token; `apply_bans` is
    not, and this is the case that separates them — banning
    Basculin-White-Striped while leaving Basculin on the board.
    """
    members = {"basculin": "ZU", "gastly": "NFE", "shedinja": "PU"}
    kept, removed = fmt.apply_bans(members, ["Basculin-White-Striped", "Gastly"], [])
    assert removed == ["gastly"]
    assert "basculin" in kept


def test_an_unban_outranks_a_ban():
    members = {"shedinja": "PU"}
    kept, removed = fmt.apply_bans(members, ["Shedinja"], ["Shedinja"])
    assert removed == [] and "shedinja" in kept


# ------------------------------------------- the real files, if present


real_sources = pytest.mark.skipif(
    not Path(sr.FORMATS_TS).exists(), reason="config/formats.ts not downloaded"
)


@real_sources
def test_the_real_formats_ts_covers_every_format_we_build():
    """A format whose Showdown name is wrong resolves to no rules at all.

    That failure is invisible at runtime — the pool still builds, just without
    the bans — so it is pinned here instead.
    """
    formats = sr.parse_formats(fmt.read_source(sr.FORMATS_TS))
    missing = [
        spec.showdown_name for spec in fmt.LADDER_FORMATS.values()
        if spec.showdown_name not in formats
    ]
    assert not missing, f"not found in formats.ts: {missing}"


@real_sources
def test_the_tier_bans_agree_with_every_ceiling():
    """formats.ts bans tiers by name; we ban them with a ceiling. Same answer.

    If Smogon restructures a ladder, this is what notices — the two models
    would drift apart silently otherwise.
    """
    formats = sr.parse_formats(fmt.read_source(sr.FORMATS_TS))
    rulesets = sr.parse_rulesets(fmt.read_source(sr.RULESETS_TS))
    _, species = fmt.parse_tiers(fmt.read_source(fmt.DEFAULT_SOURCE))

    for key, spec in fmt.LADDER_FORMATS.items():
        ladder = fmt.LADDERS[spec.ladder]
        resolved = sr.resolve(spec.showdown_name, formats, rulesets)
        banned = sr.classify(resolved.bans, set(species))["tier"]
        for token in banned:
            tier = token.removeprefix("ND ").replace("Ubers", "Uber")
            if tier not in ladder.order:
                continue
            assert fmt.rank(tier, ladder) < fmt.rank(spec.ceiling, ladder), (
                f"{key}: formats.ts bans {token!r} but the ceiling "
                f"{spec.ceiling!r} admits it"
            )
