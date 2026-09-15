"""Tests for the four services. No network: PokeAPI is faked at the transport."""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

STUB = {
    "great-tusk": (["ground", "fighting"], [115, 131, 131, 53, 53, 87]),
    "kingambit": (["dark", "steel"], [100, 135, 120, 60, 85, 50]),
    "gholdengo": (["steel", "ghost"], [87, 60, 95, 133, 91, 84]),
    "clefable": (["fairy"], [95, 70, 73, 95, 90, 60]),
    "nacli": (["rock"], [55, 55, 75, 35, 35, 25]),
}
KEYS = ["hp", "attack", "defense", "special-attack", "special-defense", "speed"]

def sd_id(name: str) -> str:
    """PokeAPI name -> Showdown id. Showdown ids have no hyphens, and an
    unquoted key with a hyphen in it is not valid JavaScript — which is what
    makes this conversion necessary in the fixture, not just in the app."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


TIERS_JS = """exports.BattleFormatsData = {
%s
};
""" % "\n".join(
    # Enough species to clear the sanity check in parse_tiers, plus the ones
    # the stub can hydrate. Keyed by Showdown id, as the real file is.
    [f'\t{sd_id(name)}: {{ tier: "OU" }},' for name in STUB if name != "nacli"]
    + ['\tnacli: { tier: "LC" },']
    + [f'\tfiller{i}: {{ tier: "PU" }},' for i in range(900)]
    + ['\tkoraidon: { tier: "Uber" },',
       '\tvenusaurmega: { isNonstandard: "Past", tier: "Illegal", natDexTier: "UU" },',
       '\tvenusaurgmax: { isNonstandard: "Past", tier: "Illegal" },']
)


def wire(request: httpx.Request) -> httpx.Response:
    path = request.url.path.lstrip("/")
    if path.startswith("api/v2/"):
        path = path[len("api/v2/"):]
    if path.startswith("pokemon?") or path == "pokemon":
        return httpx.Response(200, json={"results": [{"name": n, "url": ""} for n in STUB]})
    name = path.split("/", 1)[1] if "/" in path else ""
    if name not in STUB:
        return httpx.Response(404, json={})
    types, stats = STUB[name]
    return httpx.Response(200, json={
        "name": name, "species": {"name": name, "url": ""},
        "types": [{"type": {"name": t}} for t in types],
        "stats": [{"stat": {"name": k}, "base_stat": v} for k, v in zip(KEYS, stats)],
        "sprites": {"front_default": f"https://s/{name}.png",
                    "other": {"official-artwork": {"front_default": f"https://a/{name}.png"}}},
    })


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_DB", str(tmp_path / "t.db"))
    for mod in ("poke_db", "pokeapi", "format_service", "pool_service", "team_service", "main"):
        sys.modules.pop(mod, None)
    main = importlib.import_module("main")
    pokeapi = importlib.import_module("pokeapi")

    tiers = tmp_path / "formats-data.js"
    tiers.write_text(TIERS_JS)

    client = TestClient(main.app)
    client.__enter__()
    main.app.state.poke = pokeapi.PokeApiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(wire))
    )
    yield client, str(tiers), importlib.import_module("format_service")
    client.__exit__(None, None, None)


def load_formats(app):
    """Run the cron job's refresh against the fixture tier file."""
    client, tiers, fmt = app
    from poke_db import transaction
    with transaction() as conn:
        return fmt.refresh(conn, tiers)


# ---------------------------------------------------------- format service


def test_refresh_populates_the_format_tables(app):
    client, _, fmt = app
    report = load_formats(app)
    assert report["changed"] is True
    assert report["formats"] == len(fmt.LADDER_FORMATS)

    formats = client.get("/formats").json()
    keys = {f["key"] for f in formats}
    assert "gen9-ou" in keys and "gen9-ubers" in keys
    # Every format in the catalogue is offered, on all three ladders.
    assert keys == set(fmt.LADDER_FORMATS)
    assert {f["ladder"] for f in formats} == {"singles", "doubles", "natdex"}


def test_formats_are_listed_ladder_by_ladder_not_alphabetically(app):
    load_formats(app)
    ladders = [f["ladder"] for f in app[0].get("/formats").json()]
    # Each ladder's formats are contiguous, and singles leads.
    assert ladders[0] == "singles"
    assert ladders == sorted(ladders, key=["singles", "doubles", "natdex"].index)


def test_an_unranked_pokemon_falls_to_the_bottom_of_the_doubles_ladder(app):
    """Showdown leaves doublesTier off most LC/NFE mons; they are still legal.

    sim/dex-species.ts falls back to the singles tier, and a tier the doubles
    ladder does not rank sits at the bottom — so Nacli is Doubles UU legal.
    """
    load_formats(app)
    from poke_db import connect
    import format_service as fmt
    assert "nacli" in fmt.format_species(connect(), "gen9-doubles-uu")


def test_national_dex_admits_past_pokemon_that_standard_play_bans(app):
    load_formats(app)
    from poke_db import connect
    import format_service as fmt
    conn = connect()
    assert "venusaurmega" not in fmt.format_species(conn, "gen9-ou")
    assert "venusaurmega" in fmt.format_species(conn, "gen9-natdex-ou")
    # A Past form National Dex does not rank stays out — no natDexTier means
    # the singles "Illegal" stands. This is what keeps Gmax forms off the board.
    assert "venusaurgmax" not in fmt.format_species(conn, "gen9-natdex-ag")


def test_a_parenthesised_tier_is_ranked_not_banned(app):
    """"(OU)" means ranked-but-not-independently-used. It is not a ban."""
    _, _, fmt = app
    natdex = fmt.LADDERS["natdex"]
    record = {"tier": "Illegal", "isNonstandard": "Past", "natDexTier": "(OU)"}
    assert fmt.tier_of(record, natdex) == "OU"
    # Legal at its own rung and every more permissive one, banned below it.
    assert fmt.is_legal(record, natdex, "OU")
    assert fmt.is_legal(record, natdex, "Uber")
    assert not fmt.is_legal(record, natdex, "UU")


def test_a_form_only_pokemon_takes_its_typing_from_the_form(app):
    """PokeAPI serves Silvally's memories under `pokemon-form`, not `pokemon`.

    The base species holds the stats and reports Normal; only the form knows
    it is Fire. Getting this wrong puts eighteen identical Normal-type
    Silvallys on the board, so it is worth pinning down without the network.
    """
    import asyncio
    import pool_service as ps
    from pokeapi import PokeApiClient

    def forms(request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("api/v2/")[-1]
        if path == "pokemon/silvally":
            return httpx.Response(200, json={
                "name": "silvally",
                "types": [{"type": {"name": "normal"}}],
                "stats": [{"stat": {"name": k}, "base_stat": 95} for k in KEYS],
                "sprites": {"front_default": "https://s/silvally.png",
                            "other": {"official-artwork":
                                      {"front_default": "https://a/silvally.png"}}},
            })
        if path == "pokemon-form/silvally-fire":
            return httpx.Response(200, json={
                "name": "silvally-fire",
                "types": [{"type": {"name": "fire"}}],
                "sprites": {"front_default": "https://s/silvally-fire.png"},
            })
        return httpx.Response(404, json={})

    client = PokeApiClient(httpx.AsyncClient(transport=httpx.MockTransport(forms)))
    entry = asyncio.run(ps.hydrate(client, "silvally-fire"))

    assert entry["types"] == ["fire"]           # from the form
    assert entry["bst"] == 95 * 6               # from the base species
    assert entry["api_name"] == "silvally-fire"  # keeps its own identity
    assert entry["sprite_url"] == "https://s/silvally-fire.png"
    # A form has no official artwork of its own; fall back rather than blank.
    assert entry["artwork_url"] == "https://a/silvally.png"


def test_every_overlay_form_names_a_base_species(app):
    """A typo in FORM_OVERLAYS would 404 at build time, silently dropping a mon."""
    import pool_service as ps
    for form, base in ps.FORM_OVERLAYS.items():
        assert form.startswith(f"{base}-"), (form, base)
        assert ps.showdown_id(form) not in ps.FORM_ALIASES


def test_cap_and_illegal_are_never_legal_on_any_ladder(app):
    _, _, fmt = app
    for ladder in fmt.LADDERS.values():
        for tier in ("CAP", "CAP LC", "CAP NFE", "Illegal"):
            assert not fmt.is_legal({"tier": tier}, ladder, ladder.order[0])


def test_refresh_is_a_no_op_when_the_hash_is_unchanged(app):
    load_formats(app)
    second = load_formats(app)
    assert second["changed"] is False
    assert second["moves"] == []


def test_uber_and_nonstandard_are_excluded_from_ou(app):
    load_formats(app)
    from poke_db import connect
    import format_service as fmt
    species = fmt.format_species(connect(), "gen9-ou")
    assert "koraidon" not in species        # Uber
    assert "venusaurmega" not in species    # isNonstandard: Past
    assert "greattusk" in species           # Showdown id, not the PokeAPI name


def test_a_tier_move_is_detected_and_logged(app):
    client, tiers, fmt = app
    load_formats(app)
    Path(tiers).write_text(TIERS_JS.replace('kingambit: { tier: "OU" }',
                                            'kingambit: { tier: "Uber" }'))
    report = load_formats(app)
    assert report["changed"] is True
    assert {"species": "kingambit", "from": "OU", "to": "Uber"} in report["moves"]

    status = client.get("/formats/status").json()
    assert status["last_run"]["changed"] == 1


def test_formats_endpoint_is_503_before_the_cron_job_runs(app):
    assert app[0].get("/formats").status_code == 503


# ------------------------------------------------------------ team service


def season(client) -> int:
    return client.post("/seasons", json={"name": "L", "budget": 60, "roster_size": 3}).json()["id"]


def test_team_crud_round_trip(app):
    client = app[0]
    sid = season(client)

    made = client.post(f"/seasons/{sid}/teams", json={"name": "Sinnoh Slammers", "owner": "Logan"})
    assert made.status_code == 201
    team_id = made.json()["id"]

    assert client.get(f"/seasons/{sid}/teams/{team_id}").json()["name"] == "Sinnoh Slammers"
    assert len(client.get(f"/seasons/{sid}/teams").json()) == 1

    assert client.delete(f"/seasons/{sid}/teams/{team_id}").status_code == 204
    assert client.get(f"/seasons/{sid}/teams/{team_id}").status_code == 404


def test_duplicate_team_name_is_a_409_with_a_readable_message(app):
    client = app[0]
    sid = season(client)
    client.post(f"/seasons/{sid}/teams", json={"name": "Dupes"})
    second = client.post(f"/seasons/{sid}/teams", json={"name": "Dupes"})
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]
    assert "UNIQUE" not in second.json()["detail"]   # not the raw SQLite message


def test_patch_changes_only_what_was_sent(app):
    client = app[0]
    sid = season(client)
    team_id = client.post(
        f"/seasons/{sid}/teams", json={"name": "Keep Me", "owner": "Logan"}
    ).json()["id"]

    client.patch(f"/seasons/{sid}/teams/{team_id}",
                 json={"logo_url": "https://i.imgur.com/a.png"})
    after = client.get(f"/seasons/{sid}/teams/{team_id}").json()
    assert after["name"] == "Keep Me"       # untouched
    assert after["owner"] == "Logan"        # untouched
    assert after["logo_url"].endswith("a.png")


def test_a_non_http_logo_is_rejected_before_it_reaches_the_database(app):
    client = app[0]
    sid = season(client)
    bad = client.post(f"/seasons/{sid}/teams",
                      json={"name": "XSS", "logo_url": "javascript:alert(1)"})
    assert bad.status_code == 422
    assert client.get(f"/seasons/{sid}/teams").json() == []


def test_team_limit_is_enforced(app):
    client = app[0]
    sid = season(client)
    for i in range(10):
        assert client.post(f"/seasons/{sid}/teams", json={"name": f"T{i}"}).status_code == 201
    full = client.post(f"/seasons/{sid}/teams", json={"name": "Eleventh"})
    assert full.status_code == 409
    assert "full" in full.json()["detail"]


def test_setting_the_order_assigns_positions_and_allows_a_swap(app):
    client = app[0]
    sid = season(client)
    a = client.post(f"/seasons/{sid}/teams", json={"name": "A"}).json()["id"]
    b = client.post(f"/seasons/{sid}/teams", json={"name": "B"}).json()["id"]

    first = client.post(f"/seasons/{sid}/teams/order", json=[a, b]).json()
    assert [t["draft_position"] for t in first] == [1, 2]

    # Swapping would violate UNIQUE(season_id, draft_position) without the
    # clear-then-set in set_order.
    swapped = client.post(f"/seasons/{sid}/teams/order", json=[b, a]).json()
    assert [(t["name"], t["draft_position"]) for t in swapped] == [("B", 1), ("A", 2)]


def test_a_partial_order_is_refused(app):
    client = app[0]
    sid = season(client)
    a = client.post(f"/seasons/{sid}/teams", json={"name": "A"}).json()["id"]
    client.post(f"/seasons/{sid}/teams", json={"name": "B"})
    bad = client.post(f"/seasons/{sid}/teams/order", json=[a])
    assert bad.status_code == 409
    assert "exactly once" in bad.json()["detail"]


# ------------------------------------------------------------ pool service


def build(client, sid, key="gen9-ou"):
    return client.post(f"/seasons/{sid}/pool/from-format/{key}")


def test_building_a_pool_stores_the_new_columns(app):
    client = app[0]
    load_formats(app)
    sid = season(client)

    result = build(client, sid).json()
    # All five: OU admits everything from UUBL down to LC, so nacli counts.
    assert result["added"] == 5
    assert result["tier_snapshot"]

    entry = client.get(f"/seasons/{sid}/pool").json()[0]
    assert entry["showdown_id"] == "greattusk" or entry["showdown_id"] in {
        "kingambit", "gholdengo", "clefable"
    }
    assert entry["stats"]["hp"] > 0      # stats decoded from JSON
    assert entry["bst"] == sum(entry["stats"].values())
    assert entry["artwork_url"].startswith("https://a/")
    assert entry["tier"] == "OU"
    assert entry["banned"] is False


def test_the_snapshot_is_pinned_on_the_season(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    build(client, sid)

    read = client.get(f"/seasons/{sid}").json()
    assert read["format_key"] == "gen9-ou"
    assert read["tier_snapshot"]


def test_unresolvable_species_are_reported(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    # The stub serves five names; gen9-ou has 900+ species, so most cannot
    # resolve and every one of them must be named rather than dropped.
    assert len(build(client, sid).json()["unresolved"]) > 100


def test_rebuilding_keeps_ids_and_costs(app):
    """The whole reason for ON CONFLICT DO UPDATE instead of INSERT OR REPLACE."""
    client = app[0]
    load_formats(app)
    sid = season(client)
    build(client, sid)

    before = {e["api_name"]: e["id"] for e in client.get(f"/seasons/{sid}/pool").json()}
    client.post(f"/seasons/{sid}/pool/costs", json={"costs": {"great-tusk": 19}})

    build(client, sid)
    after = client.get(f"/seasons/{sid}/pool").json()
    assert {e["api_name"]: e["id"] for e in after} == before
    assert next(e for e in after if e["api_name"] == "great-tusk")["cost"] == 19


def test_costs_accept_either_name_form_and_report_unknowns(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    build(client, sid)

    result = client.post(f"/seasons/{sid}/pool/costs", json={
        "costs": {"great-tusk": 19, "kingambit": 18, "notamon": 5}
    }).json()
    assert result["updated"] == 2
    assert result["unknown"] == ["notamon"]
    assert result["unpriced"] == 3


def test_negative_costs_are_rejected(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    build(client, sid)
    bad = client.post(f"/seasons/{sid}/pool/costs", json={"costs": {"great-tusk": -5}})
    assert bad.status_code == 422


def test_bans_hide_an_entry_from_the_default_pool(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    build(client, sid)

    client.post(f"/seasons/{sid}/pool/bans", json={"names": ["greattusk"], "banned": True})
    visible = {e["api_name"] for e in client.get(f"/seasons/{sid}/pool").json()}
    assert "great-tusk" not in visible

    with_banned = client.get(f"/seasons/{sid}/pool?include_banned=true").json()
    assert any(e["api_name"] == "great-tusk" and e["banned"] for e in with_banned)


def test_pool_filters(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    build(client, sid)
    client.post(f"/seasons/{sid}/pool/costs", json={"costs": {"great-tusk": 19, "clefable": 8}})

    steel = client.get(f"/seasons/{sid}/pool?type=steel").json()
    assert {e["api_name"] for e in steel} == {"kingambit", "gholdengo"}
    # 19 is excluded; clefable at 8 and the three still at 0 are not.
    assert len(client.get(f"/seasons/{sid}/pool?max_cost=10").json()) == 4
    assert [e["api_name"] for e in client.get(f"/seasons/{sid}/pool?q=Tusk").json()] == ["great-tusk"]
    assert len(client.get(f"/seasons/{sid}/pool?unpriced_only=true").json()) == 3


def test_pool_summary(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    build(client, sid)
    client.post(f"/seasons/{sid}/pool/costs", json={"costs": {"great-tusk": 19}})

    summary = client.get(f"/seasons/{sid}/pool/summary").json()
    assert summary == {"total": 5, "priced": 1, "unpriced": 4, "banned": 0, "max_cost": 19}


def test_building_for_an_unknown_format_or_season_is_a_404(app):
    client = app[0]
    load_formats(app)
    sid = season(client)
    assert build(client, sid, "gen9-nonsense").status_code == 404
    assert build(client, 999).status_code == 404


# -------------------------------------------------------- adding by hand


def test_add_one_by_pokeapi_name(app):
    client = app[0]
    sid = season(client)
    made = client.post(f"/seasons/{sid}/pool/entries",
                       json={"name": "great-tusk", "cost": 19})
    assert made.status_code == 201
    entry = made.json()
    assert entry["api_name"] == "great-tusk"
    assert entry["showdown_id"] == "greattusk"
    assert entry["cost"] == 19
    assert entry["stats"]["hp"] == 115


def test_add_one_by_showdown_id(app):
    """FORM_ALIASES maps greattusk -> great-tusk, so both spellings work."""
    client = app[0]
    sid = season(client)
    made = client.post(f"/seasons/{sid}/pool/entries", json={"name": "greattusk"})
    assert made.status_code == 201
    assert made.json()["api_name"] == "great-tusk"


def test_adding_works_without_a_format_loaded(app):
    """The commissioner override: no cron job needed, no format required."""
    client = app[0]
    sid = season(client)
    assert client.get("/formats").status_code == 503
    assert client.post(f"/seasons/{sid}/pool/entries",
                       json={"name": "clefable"}).status_code == 201


def test_re_adding_does_not_wipe_an_existing_price(app):
    client = app[0]
    sid = season(client)
    client.post(f"/seasons/{sid}/pool/entries", json={"name": "great-tusk", "cost": 19})
    # No cost this time: refreshing the data must leave the price alone.
    client.post(f"/seasons/{sid}/pool/entries", json={"name": "great-tusk"})
    entry = client.get(f"/seasons/{sid}/pool").json()[0]
    assert entry["cost"] == 19


def test_adding_an_unknown_name_is_a_404_with_a_useful_hint(app):
    client = app[0]
    sid = season(client)
    bad = client.post(f"/seasons/{sid}/pool/entries", json={"name": "Chien Pow"})
    assert bad.status_code == 404
    assert "hyphenated" in bad.json()["detail"]


def test_parse_cost_list_handles_commas_tabs_and_spaces(app):
    import pool_service
    assert pool_service.parse_cost_list("Great Tusk, 19") == [("Great Tusk", 19)]
    assert pool_service.parse_cost_list("Great Tusk\t19") == [("Great Tusk", 19)]
    assert pool_service.parse_cost_list("great-tusk 19") == [("great-tusk", 19)]
    # A header row has no trailing number, so it is skipped.
    assert pool_service.parse_cost_list("Pokemon,Cost\ngreat-tusk,19") == [("great-tusk", 19)]


def test_pasting_a_cost_list_imports_prices_and_reports_misses(app):
    client = app[0]
    sid = season(client)
    result = client.post(f"/seasons/{sid}/pool/paste", json={"cost_list":
        "Pokemon,Cost\ngreat-tusk,19\nkingambit,18\nclefable,11\nChien Pow,9\n"
    }).json()

    assert result["submitted"] == 4
    assert result["added"] == 3
    assert [u["name"] for u in result["unmatched"]] == ["Chien Pow"]

    pool = client.get(f"/seasons/{sid}/pool").json()
    assert [(e["api_name"], e["cost"]) for e in pool] == [
        ("great-tusk", 19), ("kingambit", 18), ("clefable", 11)
    ]


# ------------------------------------------------- custom format files


def test_a_custom_txt_list_builds_a_pool(app):
    """One file, several shapes of line, because that is how lists arrive."""
    client = app[0]
    sid = season(client)
    body = client.post(f"/seasons/{sid}/pool/custom", json={
        "content": (
            "# Spring Cup\n"
            "- Great Tusk, 19\n"
            "kingambit\t18\n"
            "Gholdengo 17\n"
            "clefable\n"
            "NotAPokemon, 5\n"
        ),
        "label": "Spring Cup",
    }).json()

    assert body["added"] == 4
    assert body["priced"] == 3            # clefable came with no cost
    assert [u["name"] for u in body["unmatched"]] == ["NotAPokemon"]
    assert body["label"] == "Spring Cup"

    names = {e["display_name"] for e in client.get(f"/seasons/{sid}/pool").json()}
    assert "Great Tusk" in names and "Clefable" in names


def test_a_custom_json_list_builds_a_pool(app):
    client = app[0]
    sid = season(client)
    body = client.post(f"/seasons/{sid}/pool/custom", json={
        "content": '[{"name": "Great Tusk", "cost": 19}, "kingambit"]',
    }).json()
    assert body["added"] == 2 and body["priced"] == 1


def test_a_custom_pool_marks_the_season_and_keeps_its_label(app):
    client = app[0]
    sid = season(client)
    client.post(f"/seasons/{sid}/pool/custom",
                json={"content": "great-tusk", "label": "Spring Cup"})
    record = client.get(f"/seasons/{sid}").json()
    assert record["format_key"] == "custom"
    assert record["pool_label"] == "Spring Cup"


def test_re_dropping_a_file_replaces_rather_than_merges(app):
    """A corrected file is a correction, not a second half-pool."""
    client = app[0]
    sid = season(client)
    client.post(f"/seasons/{sid}/pool/custom", json={"content": "great-tusk\nkingambit"})
    second = client.post(f"/seasons/{sid}/pool/custom", json={"content": "clefable"}).json()

    assert second["removed"] == 2
    names = [e["display_name"] for e in client.get(f"/seasons/{sid}/pool").json()]
    assert names == ["Clefable"]


def test_replace_false_adds_to_the_existing_pool(app):
    client = app[0]
    sid = season(client)
    client.post(f"/seasons/{sid}/pool/custom", json={"content": "great-tusk"})
    client.post(f"/seasons/{sid}/pool/custom",
                json={"content": "clefable", "replace": False})
    assert len(client.get(f"/seasons/{sid}/pool").json()) == 2


def test_a_broken_json_file_is_a_400_that_says_so(app):
    """It must not fall through to the line reader and invent a Pokemon."""
    client = app[0]
    sid = season(client)
    broken = client.post(f"/seasons/{sid}/pool/custom", json={"content": '["Great Tusk"'})
    assert broken.status_code == 400
    assert "not valid JSON" in broken.json()["detail"]


def test_an_empty_custom_file_is_a_400(app):
    client = app[0]
    sid = season(client)
    assert client.post(f"/seasons/{sid}/pool/custom",
                       json={"content": "# only a comment"}).status_code == 400


def test_a_custom_pool_for_an_unknown_season_is_a_404(app):
    assert app[0].post("/seasons/999/pool/custom",
                       json={"content": "great-tusk"}).status_code == 404


def test_a_name_resolves_however_the_league_spells_it(app):
    """"Great Tusk", "great-tusk" and "greattusk" are one Pokemon."""
    client = app[0]
    sid = season(client)
    body = client.post(f"/seasons/{sid}/pool/custom", json={
        "content": "Great Tusk\ngreat-tusk\ngreattusk",
    }).json()
    assert body["added"] == 3            # all three resolved
    assert len(client.get(f"/seasons/{sid}/pool").json()) == 1   # onto one row


def test_an_empty_paste_is_a_400(app):
    client = app[0]
    sid = season(client)
    bad = client.post(f"/seasons/{sid}/pool/paste", json={"cost_list": "just some notes\n"})
    assert bad.status_code == 400
