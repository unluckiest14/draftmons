"""Tests for the replay analyzer.

The fixture below is a trimmed but real Showdown log, and it carries the four
things that actually make this hard: nicknames, a Rocky Helmet kill, hazard
chip that must credit nobody, and a Mega Evolution that renames a Pokemon
mid-battle.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_service as replays  # noqa: E402

LOG = """|player|p1|Ash|red|
|player|p2|Gary|blue|
|gen|7
|tier|[Gen 7] OU
|clearpoke
|poke|p1|Pelipper, M|item
|poke|p1|Ferrothorn, M|item
|poke|p1|Swampert, M|item
|poke|p2|Tyranitar, M|item
|poke|p2|Crustle, M|item
|poke|p2|Volcanion|item
|teampreview
|start
|switch|p1a: reppileP|Pelipper, M|100/100
|switch|p2a: Baby Mode|Tyranitar, M|100/100
|turn|1
|move|p1a: reppileP|Scald|p2a: Baby Mode
|-supereffective|p2a: Baby Mode
|-damage|p2a: Baby Mode|0 fnt
|faint|p2a: Baby Mode
|switch|p2a: Fraud Watch|Crustle, M|100/100
|turn|2
|move|p1a: reppileP|U-turn|p2a: Fraud Watch
|-damage|p2a: Fraud Watch|80/100
|switch|p1a: bentley demon|Ferrothorn, M|100/100
|move|p2a: Fraud Watch|Knock Off|p1a: bentley demon
|-damage|p1a: bentley demon|79/100
|-damage|p2a: Fraud Watch|0 fnt|[from] item: Rocky Helmet|[of] p1a: bentley demon
|faint|p2a: Fraud Watch
|switch|p2a: krakatoa|Volcanion|100/100
|turn|3
|switch|p1a: ‎|Swampert, M|100/100
|-damage|p1a: ‎|0 fnt|[from] Stealth Rock
|faint|p1a: ‎
|turn|4
|win|Ash
"""


@pytest.fixture()
def parsed():
    return replays.parse(LOG)


# ------------------------------------------------------------ the basics


def test_players_and_outcome(parsed):
    assert parsed["players"] == {"p1": "Ash", "p2": "Gary"}
    assert parsed["winner"] == "Ash"
    assert parsed["format"] == "[Gen 7] OU"
    assert parsed["turns"] == 4


def test_a_nicknamed_pokemon_is_reported_by_species(parsed):
    """Nothing in the fighting names a species — only the switch line does."""
    p1 = {mon["species"]: mon for mon in parsed["sides"][0]["pokemon"]}
    assert set(p1) == {"Pelipper", "Ferrothorn", "Swampert"}
    assert p1["Pelipper"]["nickname"] == "reppileP"


def test_a_pokemon_nicknamed_with_an_invisible_character_still_resolves(parsed):
    """A real replay nicknames a Conkeldurr to U+200E. It is still a Pokemon."""
    p1 = {mon["species"]: mon for mon in parsed["sides"][0]["pokemon"]}
    assert p1["Swampert"]["fainted"] is True


# ------------------------------------------------------- kill attribution


def test_a_direct_knockout_credits_the_attacker(parsed):
    p1 = {mon["species"]: mon for mon in parsed["sides"][0]["pokemon"]}
    assert p1["Pelipper"]["kills"] == 1        # Scald on Tyranitar


def test_recoil_damage_credits_the_pokemon_that_caused_it(parsed):
    """Crustle died to Rocky Helmet while attacking. The KO is Ferrothorn's.

    `[of]` is what names the responsible Pokemon; the obvious attacker — the
    one that used the move — is the one that *died*.
    """
    p1 = {mon["species"]: mon for mon in parsed["sides"][0]["pokemon"]}
    assert p1["Ferrothorn"]["kills"] == 1


def test_hazard_damage_credits_nobody(parsed):
    """Swampert died to Stealth Rock. No Pokemon knocked it out."""
    assert sum(m["kills"] for m in parsed["sides"][1]["pokemon"]) == 0
    assert parsed["sides"][1]["kills"] == 0


def test_the_totals_reconcile(parsed):
    """One side's kills are the other's deaths, minus anything unattributed."""
    ash, gary = parsed["sides"]
    assert ash["kills"] == 2 and ash["deaths"] == 1
    assert gary["kills"] == 0 and gary["deaths"] == 2
    assert ash["kills"] <= gary["deaths"]
    assert ash["remaining"] == 2


def test_a_pokemon_never_sent_out_still_appears(parsed):
    """Team preview is the only record of a Pokemon that sat on the bench."""
    gary = {mon["species"]: mon for mon in parsed["sides"][1]["pokemon"]}
    assert set(gary) == {"Tyranitar", "Crustle", "Volcanion"}
    assert gary["Volcanion"]["kills"] == 0 and gary["Volcanion"]["fainted"] is False


# ------------------------------------------------------------ mega forms


MEGA_LOG = """|player|p1|Ash|red|
|player|p2|Gary|blue|
|poke|p1|Golisopod, M|item
|poke|p2|Pelipper, M|item
|switch|p1a: Golisopod|Golisopod, M|100/100
|switch|p2a: Pelipper|Pelipper, M|100/100
|turn|1
|detailschange|p1a: Golisopod|Golisopod-Mega, M
|move|p1a: Golisopod|Liquidation|p2a: Pelipper
|-damage|p2a: Pelipper|0 fnt
|faint|p2a: Pelipper
|switch|p1a: Golisopod|Golisopod-Mega, M|80/100
|turn|2
|win|Ash
"""


def test_a_mega_evolution_is_one_pokemon_not_two():
    """Switching back in after Mega Evolving re-enters as the Mega's name.

    Keying on species counted Golisopod and Golisopod-Mega as two Pokemon —
    and taking the later name as the species would file the KOs under
    something no league ever drafted.
    """
    parsed = replays.parse(MEGA_LOG)
    team = parsed["sides"][0]["pokemon"]
    assert len(team) == 1
    assert team[0]["species"] == "Golisopod"
    assert team[0]["forms"] == ["Golisopod-Mega"]
    assert team[0]["kills"] == 1


# ------------------------------------------------------------------ urls


@pytest.mark.parametrize("given", [
    "https://replay.pokemonshowdown.com/gen7ou-2491876324",
    "http://replay.pokemonshowdown.com/gen7ou-2491876324",
    "replay.pokemonshowdown.com/gen7ou-2491876324",
    "https://replay.pokemonshowdown.com/gen7ou-2491876324.json",
    "https://replay.pokemonshowdown.com/gen7ou-2491876324/",
    "gen7ou-2491876324",
])
def test_every_shape_of_replay_link_resolves(given):
    assert replays.replay_id(given) == "gen7ou-2491876324"


@pytest.mark.parametrize("given", ["", "   ", "https://example.com/not-a-replay"])
def test_a_link_that_is_not_a_replay_is_refused(given):
    with pytest.raises(replays.ReplayError):
        replays.replay_id(given)


def test_a_log_with_no_pokemon_is_refused():
    with pytest.raises(replays.ReplayError):
        replays.parse("|player|p1|Ash|red|\n|win|Ash\n")


# ------------------------------------------------------ knockout history


def test_every_knockout_becomes_an_event(parsed):
    assert [e["turn"] for e in parsed["events"]] == [1, 2, 3]


def test_a_direct_knockout_reads_as_a_sentence(parsed):
    assert parsed["events"][0]["text"] == (
        "Gary's Tyranitar fainted from Scald by Ash's Pelipper."
    )


def test_an_indirect_knockout_says_so_and_still_names_the_scorer(parsed):
    """Recoil is not a normal KO, and a league writes it up differently."""
    event = parsed["events"][1]
    assert event["indirect"] is True
    assert event["cause"] == "Rocky Helmet"
    assert event["text"] == (
        "Gary's Crustle fainted indirectly from Rocky Helmet by Ash's Ferrothorn."
    )


def test_a_hazard_knockout_names_no_scorer(parsed):
    event = parsed["events"][2]
    assert event["killer"] is None
    assert event["cause"] == "Stealth Rock"
    assert event["text"] == "Ash's Swampert fainted indirectly from Stealth Rock."


def test_the_item_prefix_is_stripped_from_a_cause(parsed):
    """The log says "item: Rocky Helmet"; a reader does not need "item:"."""
    assert all(":" not in (e["cause"] or "") for e in parsed["events"])


def test_events_carry_both_sides_for_colouring(parsed):
    first = parsed["events"][0]
    assert first["victim_side"] == "p2" and first["killer_side"] == "p1"
    assert first["victim_player"] == "Gary" and first["killer_player"] == "Ash"


def test_the_event_count_matches_the_faint_count(parsed):
    deaths = sum(side["deaths"] for side in parsed["sides"])
    assert len(parsed["events"]) == deaths


def test_a_hazard_event_has_no_arrow_to_draw(parsed):
    """The visual view needs to know there is nobody to point from."""
    hazard = parsed["events"][2]
    assert hazard["killer_side"] is None
    # And the two that do have a source name both sides, so a direction exists.
    assert all(e["killer_side"] and e["victim_side"] for e in parsed["events"][:2])


def test_an_event_names_the_side_that_lost_the_pokemon(parsed):
    """Rows are tinted by the loser, so this is what the colour keys off."""
    assert [e["victim_side"] for e in parsed["events"]] == ["p2", "p2", "p1"]
