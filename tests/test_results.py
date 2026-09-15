"""Tests for results, the standings and the elimination leaderboard.

Two halves. The first is arithmetic — a week of matches has to come out of the
standings as the record the league would write down by hand, in the order it
would write it. The second is refusals, because everything a leaderboard shows
is a claim about what happened, and the only place to stop a wrong one is the
moment it is recorded.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

POOL = (
    "Great Tusk, 19\nKingambit, 18\nGholdengo, 17\nDragapult, 16\n"
    "Clefable, 12\nIron Valiant, 11\nCorviknight, 10\nSlowking, 9\n"
    "Nacli, 2\nPawniard, 1\n"
)
STUB = {
    "great-tusk": ["ground", "fighting"], "kingambit": ["dark", "steel"],
    "gholdengo": ["steel", "ghost"], "dragapult": ["dragon", "ghost"],
    "clefable": ["fairy"], "iron-valiant": ["fairy", "fighting"],
    "corviknight": ["flying", "steel"], "slowking": ["water", "psychic"],
    "nacli": ["rock"], "pawniard": ["dark", "steel"],
}
KEYS = ["hp", "attack", "defense", "special-attack", "special-defense", "speed"]


def wire(request: httpx.Request) -> httpx.Response:
    path = request.url.path.split("api/v2/")[-1]
    if path.startswith("pokemon?") or path == "pokemon":
        return httpx.Response(200, json={"results": [{"name": n} for n in STUB]})
    name = path.split("/", 1)[1] if "/" in path else ""
    if name not in STUB:
        return httpx.Response(404, json={})
    return httpx.Response(200, json={
        "name": name,
        "types": [{"type": {"name": t}} for t in STUB[name]],
        "stats": [{"stat": {"name": k}, "base_stat": 90} for k in KEYS],
        "sprites": {"front_default": f"https://s/{name}.png",
                    "other": {"official-artwork": {"front_default": None}}},
    })


@pytest.fixture()
def league(tmp_path, monkeypatch):
    """A season past its draft: three teams, a priced pool, a roster each."""
    monkeypatch.setenv("DRAFT_DB", str(tmp_path / "d.db"))
    # No module reloading here any more. It existed to make poke_db pick up a
    # new DRAFT_DB, which connect() now reads per call — and once these modules
    # lived in a package, popping them from sys.modules stopped reloading them
    # anyway (the parent package keeps an attribute for the old object) while
    # happily producing a second copy of the app for the fixture to configure
    # and the test to miss.
    main = importlib.import_module("draftmons.app")
    pokeapi = importlib.import_module("draftmons.pokeapi")

    client = TestClient(main.app)
    client.__enter__()
    main.app.state.poke = pokeapi.PokeApiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(wire))
    )

    sid = client.post("/seasons", json={
        "name": "L", "budget": 40, "roster_size": 2,
    }).json()["id"]
    client.post(f"/seasons/{sid}/pool/custom", json={"content": POOL})

    invite = client.post(f"/seasons/{sid}/invite", json={}).json()
    admin = {"Authorization": f"Bearer {invite['admin_token']}"}
    tokens = {}
    for name in ("Alpha", "Bravo", "Charlie"):
        body = client.post(f"/join/{invite['join_code']}",
                           json={"team_name": name}).json()
        tokens[name] = {"Authorization": f"Bearer {body['token']}"}

    teams = {t["name"]: t["id"] for t in client.get(f"/seasons/{sid}/teams").json()}
    client.post(f"/seasons/{sid}/teams/order",
                json=[teams["Alpha"], teams["Bravo"], teams["Charlie"]], headers=admin)

    # A finished draft, so the leaderboard has rosters to attribute KOs to.
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    for who, mon in [
        ("Alpha", "great-tusk"), ("Bravo", "kingambit"), ("Charlie", "gholdengo"),
        ("Charlie", "dragapult"), ("Bravo", "clefable"), ("Alpha", "iron-valiant"),
    ]:
        assert client.post(f"/seasons/{sid}/draft/pick", json={"name": mon},
                           headers=tokens[who]).status_code == 200

    yield client, sid, admin, tokens, teams
    client.__exit__(None, None, None)


def post(client, sid, admin, **body):
    """Record a match. Teams are named, because ids read as noise in a test."""
    return client.post(f"/seasons/{sid}/matches", json=body, headers=admin)


def played(teams, week, home, away, winner, score=(2, 0), elims=()):
    return {
        "week_no": week,
        "home_team_id": teams[home],
        "away_team_id": teams[away],
        "winner_team_id": None if winner is None else teams[winner],
        "home_score": score[0],
        "away_score": score[1],
        "elims": [{"team_id": teams[who], "name": mon, "elims": n} for who, mon, n in elims],
    }


def table(client, sid):
    return {row["name"]: row for row in client.get(f"/seasons/{sid}/standings").json()["teams"]}


# ------------------------------------------------------------- recording


def test_recording_a_match_needs_the_admin_token(league):
    client, sid, _, tokens, teams = league
    refused = post(client, sid, tokens["Alpha"], **played(teams, 1, "Alpha", "Bravo", "Alpha"))
    assert refused.status_code in (401, 403)


def test_a_recorded_match_comes_back_with_its_stat_line(league):
    client, sid, admin, _, teams = league
    body = post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha",
        elims=[("Alpha", "great-tusk", 2), ("Bravo", "kingambit", 1)],
    )).json()

    assert body["week_no"] == 1
    assert body["winner_name"] == "Alpha"
    assert [(line["display_name"], line["elims"]) for line in body["elims"]] == [
        ("Great Tusk", 2), ("Kingambit", 1),
    ]


def test_a_line_with_no_eliminations_is_dropped_rather_than_stored(league):
    """A form sends a row per Pokemon that played. Zeros are not stat lines."""
    client, sid, admin, _, teams = league
    body = post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha",
        elims=[("Alpha", "great-tusk", 2), ("Alpha", "iron-valiant", 0)],
    )).json()
    assert [line["display_name"] for line in body["elims"]] == ["Great Tusk"]


def test_a_showdown_id_names_the_same_pokemon_as_a_pokeapi_name(league):
    client, sid, admin, _, teams = league
    body = post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha", elims=[("Alpha", "greattusk", 3)],
    )).json()
    assert body["elims"][0]["api_name"] == "great-tusk"


# -------------------------------------------------------------- refusals


def test_a_team_cannot_play_itself(league):
    client, sid, admin, _, teams = league
    refused = post(client, sid, admin, **played(teams, 1, "Alpha", "Alpha", "Alpha"))
    assert refused.status_code == 409
    assert "itself" in refused.json()["detail"]


def test_the_winner_has_to_have_been_in_the_match(league):
    client, sid, admin, _, teams = league
    refused = post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Charlie"))
    assert refused.status_code == 409
    assert "Alpha" in refused.json()["detail"] and "Bravo" in refused.json()["detail"]


def test_the_same_match_cannot_be_recorded_twice(league):
    """The guard against a double submission quietly handing out an extra win."""
    client, sid, admin, _, teams = league
    assert post(client, sid, admin,
                **played(teams, 1, "Alpha", "Bravo", "Alpha")).status_code == 201
    again = post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha"))
    assert again.status_code == 409
    assert "already has" in again.json()["detail"]


def test_a_kill_cannot_be_credited_to_a_team_that_did_not_draft_the_mon(league):
    client, sid, admin, _, teams = league
    refused = post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha", elims=[("Alpha", "kingambit", 2)],
    ))
    assert refused.status_code == 409
    assert "drafted by Bravo" in refused.json()["detail"]


def test_a_kill_cannot_be_credited_to_a_team_that_did_not_play(league):
    client, sid, admin, _, teams = league
    refused = post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha", elims=[("Charlie", "gholdengo", 2)],
    ))
    assert refused.status_code == 409
    assert "did not play" in refused.json()["detail"]


def test_an_unknown_pokemon_is_a_404_and_names_itself(league):
    client, sid, admin, _, teams = league
    refused = post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha", elims=[("Alpha", "mr-mimee", 2)],
    ))
    assert refused.status_code == 404
    assert "mr-mimee" in refused.json()["detail"]


def test_a_rejected_stat_line_takes_the_whole_match_with_it(league):
    """Half a result is worse than none: the standings would count the win."""
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha",
        elims=[("Alpha", "great-tusk", 2), ("Alpha", "mr-mimee", 1)],
    ))
    assert client.get(f"/seasons/{sid}/matches").json() == []
    assert table(client, sid)["Alpha"]["wins"] == 0


def test_a_replay_link_has_to_be_a_link(league):
    client, sid, admin, _, teams = league
    body = played(teams, 1, "Alpha", "Bravo", "Alpha")
    body["replay_url"] = "javascript:alert(1)"
    assert post(client, sid, admin, **body).status_code == 422


# ------------------------------------------------------------- standings


def test_a_win_and_a_loss_land_on_the_right_teams(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha", score=(2, 0)))

    rows = table(client, sid)
    assert (rows["Alpha"]["wins"], rows["Alpha"]["losses"]) == (1, 0)
    assert (rows["Bravo"]["wins"], rows["Bravo"]["losses"]) == (0, 1)
    assert rows["Alpha"]["differential"] == 2
    assert rows["Bravo"]["differential"] == -2


def test_a_match_with_no_winner_is_a_draw_for_both(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", None, score=(1, 1)))

    rows = table(client, sid)
    assert rows["Alpha"]["draws"] == rows["Bravo"]["draws"] == 1
    assert rows["Alpha"]["wins"] == rows["Bravo"]["wins"] == 0
    assert rows["Alpha"]["played"] == 1


def test_every_team_is_on_the_table_before_it_has_played(league):
    """A league one week in still wants to see everybody."""
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha"))
    assert set(table(client, sid)) == {"Alpha", "Bravo", "Charlie"}


def test_the_weeks_are_columns_and_each_row_lines_up_with_them(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha"))
    post(client, sid, admin, **played(teams, 3, "Alpha", "Charlie", "Charlie"))

    body = client.get(f"/seasons/{sid}/standings").json()
    assert body["weeks"] == [1, 3]

    alpha = next(row for row in body["teams"] if row["name"] == "Alpha")
    assert [cell[0]["result"] for cell in alpha["by_week"]] == ["W", "L"]
    assert alpha["by_week"][1][0]["opponent"] == "Charlie"

    # Bravo did not play in week 3, so its cell for that week is empty rather
    # than missing — the row still has one entry per column.
    bravo = next(row for row in body["teams"] if row["name"] == "Bravo")
    assert [len(cell) for cell in bravo["by_week"]] == [1, 0]


def test_two_matches_in_one_week_both_show_in_that_week(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha"))
    post(client, sid, admin, **played(teams, 1, "Alpha", "Charlie", "Alpha"))

    alpha = table(client, sid)["Alpha"]
    assert alpha["wins"] == 2
    assert [cell["result"] for cell in alpha["by_week"][0]] == ["W", "W"]


def test_the_table_is_ordered_by_wins_then_losses_then_differential(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Bravo", "Charlie", "Bravo", score=(1, 0)))
    post(client, sid, admin, **played(teams, 2, "Alpha", "Charlie", "Alpha", score=(3, 0)))
    post(client, sid, admin, **played(teams, 2, "Bravo", "Alpha", None, score=(1, 1)))

    body = client.get(f"/seasons/{sid}/standings").json()
    # Alpha and Bravo are both 1-0-1; Alpha's +3 beats Bravo's +1.
    assert [row["name"] for row in body["teams"]] == ["Alpha", "Bravo", "Charlie"]
    assert [row["rank"] for row in body["teams"]] == [1, 2, 3]


def test_deleting_a_result_takes_its_win_back_out_of_the_table(league):
    client, sid, admin, _, teams = league
    match = post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha", elims=[("Alpha", "great-tusk", 3)],
    )).json()

    assert client.delete(f"/seasons/{sid}/matches/{match['id']}",
                         headers=admin).status_code == 204
    assert table(client, sid)["Alpha"]["wins"] == 0
    # And the stat line went with it, rather than leaving a mon on the
    # leaderboard for a match that no longer exists.
    assert client.get(f"/seasons/{sid}/leaderboard/pokemon").json() == []


def test_deleting_a_result_needs_the_admin_token(league):
    client, sid, admin, tokens, teams = league
    match = post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha")).json()
    refused = client.delete(f"/seasons/{sid}/matches/{match['id']}", headers=tokens["Alpha"])
    assert refused.status_code in (401, 403)


def test_the_standings_are_public(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha"))
    assert client.get(f"/seasons/{sid}/standings").status_code == 200


def test_standings_for_a_season_that_does_not_exist_are_a_404(league):
    client, _, _, _, _ = league
    assert client.get("/seasons/999/standings").status_code == 404


# ---------------------------------------------------- the elim leaderboard


def test_eliminations_add_up_across_matches(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha", elims=[("Alpha", "great-tusk", 2)],
    ))
    post(client, sid, admin, **played(
        teams, 2, "Alpha", "Charlie", "Alpha", elims=[("Alpha", "great-tusk", 3)],
    ))

    top = client.get(f"/seasons/{sid}/leaderboard/pokemon").json()
    assert (top[0]["display_name"], top[0]["elims"], top[0]["matches"]) == ("Great Tusk", 5, 2)
    assert top[0]["team_name"] == "Alpha"
    assert top[0]["rank"] == 1


def test_the_leaderboard_is_the_top_five_by_default(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha", elims=[
        ("Alpha", "great-tusk", 6), ("Alpha", "iron-valiant", 5),
        ("Bravo", "kingambit", 4), ("Bravo", "clefable", 3),
    ]))
    post(client, sid, admin, **played(teams, 2, "Charlie", "Bravo", "Charlie", elims=[
        ("Charlie", "gholdengo", 2), ("Charlie", "dragapult", 1),
    ]))

    top = client.get(f"/seasons/{sid}/leaderboard/pokemon").json()
    assert len(top) == 5
    assert [row["display_name"] for row in top] == [
        "Great Tusk", "Iron Valiant", "Kingambit", "Clefable", "Gholdengo",
    ]
    assert [row["rank"] for row in top] == [1, 2, 3, 4, 5]
    assert client.get(f"/seasons/{sid}/leaderboard/pokemon?limit=2").json()[-1][
        "display_name"] == "Iron Valiant"


def test_a_tie_on_eliminations_is_broken_by_the_fewer_matches(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha", elims=[
        ("Alpha", "great-tusk", 1), ("Bravo", "kingambit", 2),
    ]))
    post(client, sid, admin, **played(teams, 2, "Alpha", "Charlie", "Alpha", elims=[
        ("Alpha", "great-tusk", 1),
    ]))

    top = client.get(f"/seasons/{sid}/leaderboard/pokemon").json()
    assert [(row["display_name"], row["matches"]) for row in top] == [
        ("Kingambit", 1), ("Great Tusk", 2),
    ]


def test_a_pokemon_with_no_eliminations_is_not_on_the_leaderboard(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(teams, 1, "Alpha", "Bravo", "Alpha", elims=[
        ("Alpha", "great-tusk", 1), ("Alpha", "iron-valiant", 0),
    ]))
    assert [row["display_name"]
            for row in client.get(f"/seasons/{sid}/leaderboard/pokemon").json()] == ["Great Tusk"]


def test_the_leaderboard_carries_what_the_board_needs_to_draw_a_row(league):
    client, sid, admin, _, teams = league
    post(client, sid, admin, **played(
        teams, 1, "Alpha", "Bravo", "Alpha", elims=[("Alpha", "great-tusk", 1)],
    ))
    row = client.get(f"/seasons/{sid}/leaderboard/pokemon").json()[0]
    assert row["types"] == ["ground", "fighting"]
    assert row["sprite_url"].endswith("great-tusk.png")
    assert row["cost"] == 19
