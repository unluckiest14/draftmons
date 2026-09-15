"""Tests for the held-item catalogue.

The point of this layer is subtraction: PokeAPI lists ~2200 items and a
Pokemon can hold maybe a tenth of them, so most of what is worth pinning down
is what must *not* appear in the picker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftmons import showdown_items as si        # noqa: E402

ITEMS_TS = """
export const Items: import('../sim/dex-items').ItemDataTable = {
\tleftovers: {
\t\tname: "Leftovers",
\t\tonResidual(pokemon) {
\t\t\tthis.heal(pokemon.baseMaxhp / 16);   // a brace and a slash
\t\t},
\t\tnum: 234,
\t\tgen: 2,
\t},
\tvenusaurite: {
\t\tname: "Venusaurite",
\t\tmegaStone: { "Venusaur": "Venusaur-Mega" },
\t\titemUser: ["Venusaur"],
\t\tnum: 659,
\t\tgen: 6,
\t\tisNonstandard: "Past",
\t},
\tpokeball: {
\t\tname: "Poke Ball",
\t\tisPokeball: true,
\t\tnum: 4,
\t\tgen: 1,
\t},
\tkingsrock: {
\t\tname: "King's Rock",
\t\tnum: 221,
\t\tgen: 2,
\t},
\tfuturething: {
\t\tname: "Future Thing",
\t\tnum: 999,
\t\tgen: 9,
\t\tisNonstandard: "Future",
\t},
\tlumberry: {
\t\tname: "Lum Berry",
\t\tisBerry: true,
\t\tnum: 157,
\t\tgen: 3,
\t},
};
"""

ITEMS_TEXT_TS = """
export const ItemsText: { [id: IDEntry]: ItemText } = {
\tleftovers: {
\t\tname: "Leftovers",
\t\tshortDesc: "At the end of every turn, holder restores 1/16 of its max HP.",
\t\theal: "  {POKEMON} restored a little HP!",
\t},
\tvenusaurite: {
\t\tname: "Venusaurite",
\t\tshortDesc: "If held by a Venusaur, this item allows it to Mega Evolve.",
\t},
\tpokeball: { name: "Poke Ball", shortDesc: "A device for catching wild Pokemon." },
\tkingsrock: { name: "King's Rock", shortDesc: "Holder's attacks may cause flinching." },
\tfuturething: { name: "Future Thing", shortDesc: "Not playable yet." },
\tlumberry: { name: "Lum Berry", shortDesc: "Holder cures itself of any status." },
};
"""


@pytest.fixture()
def catalogue():
    return si.load(ITEMS_TS, ITEMS_TEXT_TS)


def test_items_join_to_their_descriptions(catalogue):
    assert catalogue["leftovers"].description.startswith("At the end of every turn")
    assert catalogue["kingsrock"].name == "King's Rock"


def test_a_function_body_does_not_end_the_entry(catalogue):
    """onResidual contains a brace and a slash; the walker must step over it."""
    assert catalogue["leftovers"].gen == 2
    assert len(catalogue) == 6


def test_pokeballs_are_not_held_items(catalogue):
    """Showdown carries them to record which ball a Pokemon came in."""
    names = [item.name for item in si.usable(catalogue)]
    assert "Poke Ball" not in names
    assert "Leftovers" in names


def test_past_items_are_out_of_standard_play_and_into_national_dex(catalogue):
    assert "Venusaurite" not in [i.name for i in si.usable(catalogue, "singles")]
    assert "Venusaurite" in [i.name for i in si.usable(catalogue, "natdex")]


def test_future_items_are_never_usable(catalogue):
    for ladder in ("singles", "doubles", "natdex"):
        assert "Future Thing" not in [i.name for i in si.usable(catalogue, ladder)]


def test_generation_caps_the_list(catalogue):
    assert "Leftovers" not in [i.name for i in si.usable(catalogue, "singles", generation=1)]


def test_every_usable_item_carries_a_description(catalogue):
    assert all(item.description for item in si.usable(catalogue, "natdex"))


# ----------------------------------------------- the real files, via HTTP


real_items = pytest.mark.skipif(
    not Path(si.ITEMS_TS).exists(), reason="data/items.ts not downloaded"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_DB", str(tmp_path / "items.db"))
    # No module reloading here any more. It existed to make poke_db pick up a
    # new DRAFT_DB, which connect() now reads per call — and once these modules
    # lived in a package, popping them from sys.modules stopped reloading them
    # anyway (the parent package keeps an attribute for the old object) while
    # happily producing a second copy of the app for the fixture to configure
    # and the test to miss.
    import importlib
    main = importlib.import_module("draftmons.app")
    from draftmons.services import format_service as fmt
    from draftmons.poke_db import transaction
    with TestClient(main.app) as started:
        with transaction() as conn:
            fmt.refresh(conn)
        yield started


@real_items
def test_the_real_catalogue_holds_no_bicycles_or_machines(client):
    names = [item["name"] for item in client.get("/items").json()]
    assert names, "no items returned"
    junk = [n for n in names if "Bike" in n or "Bicycle" in n or n.startswith("TM")]
    assert not junk, f"unusable items leaked into the picker: {junk}"
    assert "Leftovers" in names and "Heavy-Duty Boots" in names


@real_items
def test_the_format_decides_which_items_exist(client):
    ou = client.get("/items?format=gen9-ou").json()
    nd = client.get("/items?format=gen9-natdex-ou").json()
    assert not any(i["category"] == "mega" for i in ou)
    assert any(i["category"] == "mega" for i in nd)
    assert len(nd) > len(ou)


@real_items
def test_a_formats_banned_items_come_back_flagged_not_missing(client):
    """A player needs to see that King's Rock exists and is banned."""
    items = {i["name"]: i for i in client.get("/items?format=gen9-ou").json()}
    assert items["King's Rock"]["banned"] is True
    assert items["Leftovers"]["banned"] is False


@real_items
def test_banned_items_can_be_filtered_out_entirely(client):
    kept = client.get("/items?format=gen9-ou&include_banned=false").json()
    assert not any(i["banned"] for i in kept)


@real_items
def test_an_unknown_format_is_a_404(client):
    assert client.get("/items?format=nope").status_code == 404
