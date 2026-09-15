"""Tests for joining a season and for per-player privacy.

The privacy tests are the point of this file. Most of them are not "does the
feature work" but "can player A reach player B's data by any route the API
offers" — asked once per verb, because a scoped SELECT and an unscoped UPDATE
is exactly the shape a privacy bug takes.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_DB", str(tmp_path / "t.db"))
    for mod in ("poke_db", "team_service", "player_service", "player_routes", "main"):
        sys.modules.pop(mod, None)
    main = importlib.import_module("main")
    with TestClient(main.app) as c:
        yield c


def make_season(client, **kwargs) -> int:
    body = {"name": "Test League", "budget": 100, "roster_size": 8, **kwargs}
    return client.post("/seasons", json=body).json()["id"]


def open_season(client, season_id) -> tuple[str, str]:
    """Returns (join_code, admin_token)."""
    body = client.post(f"/seasons/{season_id}/invite").json()
    return body["join_code"], body["admin_token"]


def join(client, code, team_name, display_name=None) -> str:
    """Returns the player token."""
    payload = {"team_name": team_name}
    if display_name:
        payload["display_name"] = display_name
    response = client.post(f"/join/{code}", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------- the basics


def test_opening_a_season_returns_a_code_and_an_admin_token_once(client):
    season = make_season(client)
    body = client.post(f"/seasons/{season}/invite").json()

    assert len(body["join_code"]) == 8
    assert body["admin_token"].startswith("dma_")
    assert body["is_open"] is True
    assert body["joined"] == 0

    # Reading it back never re-issues the token: a secret you can fetch again
    # is not a secret.
    again = client.get(f"/seasons/{season}/invite", headers=auth(body["admin_token"]))
    assert again.json()["admin_token"] is None
    assert again.json()["join_code"] == body["join_code"]


def test_a_player_joins_by_code_and_picks_a_team_name(client):
    season = make_season(client)
    code, _ = open_season(client, season)

    body = client.post(f"/join/{code}", json={"team_name": "Sinnoh Slammers",
                                              "display_name": "Logan"}).json()
    assert body["token"].startswith("dmp_")
    assert body["team"]["name"] == "Sinnoh Slammers"
    assert body["team"]["owner"] == "Logan"
    assert body["player"]["season_id"] == season

    me = client.get("/me", headers=auth(body["token"])).json()
    assert me["team"]["name"] == "Sinnoh Slammers"
    assert me["season_id"] == season


def test_the_join_code_is_case_and_space_insensitive(client):
    """It gets read out on a call and typed by someone half paying attention."""
    season = make_season(client)
    code, _ = open_season(client, season)
    assert client.post(f"/join/  {code.lower()}  ", json={"team_name": "T"}).status_code == 201


def test_two_players_cannot_take_the_same_team_name(client):
    season = make_season(client)
    code, _ = open_season(client, season)
    join(client, code, "Sinnoh Slammers")

    clash = client.post(f"/join/{code}", json={"team_name": "Sinnoh Slammers"})
    assert clash.status_code == 409
    assert "already exists" in clash.json()["detail"]


def test_a_player_renames_their_own_team(client):
    season = make_season(client)
    code, _ = open_season(client, season)
    token = join(client, code, "Old Name")

    renamed = client.patch("/me", json={"team_name": "New Name"}, headers=auth(token))
    assert renamed.status_code == 200
    assert renamed.json()["team"]["name"] == "New Name"
    assert [t["name"] for t in client.get(f"/seasons/{season}/lobby").json()["teams"]] == ["New Name"]


def test_a_bad_code_and_a_bad_token_are_both_refused(client):
    season = make_season(client)
    open_season(client, season)

    assert client.post("/join/NOTACODE", json={"team_name": "T"}).status_code == 404
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=auth("dmp_nonsense")).status_code == 401
    assert client.get("/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_a_closed_season_refuses_new_players_but_keeps_the_ones_inside(client):
    season = make_season(client)
    code, admin = open_season(client, season)
    token = join(client, code, "First In")

    closed = client.patch(f"/seasons/{season}/invite", json={"is_open": False},
                          headers=auth(admin))
    assert closed.json()["is_open"] is False

    refused = client.post(f"/join/{code}", json={"team_name": "Too Late"})
    assert refused.status_code == 403

    # The player already inside is unaffected.
    assert client.get("/me", headers=auth(token)).status_code == 200


def test_rotating_the_code_invalidates_the_old_one_and_keeps_players(client):
    season = make_season(client)
    code, admin = open_season(client, season)
    token = join(client, code, "Already Here")

    rotated = client.post(f"/seasons/{season}/invite", headers=auth(admin)).json()
    assert rotated["rotated"] is True
    assert rotated["join_code"] != code
    assert rotated["admin_token"] is None      # the caller already holds it

    assert client.post(f"/join/{code}", json={"team_name": "X"}).status_code == 404
    assert client.post(f"/join/{rotated['join_code']}", json={"team_name": "X"}).status_code == 201
    assert client.get("/me", headers=auth(token)).status_code == 200


def test_the_season_fills_up(client):
    import team_service
    season = make_season(client)
    code, _ = open_season(client, season)
    for i in range(team_service.MAX_TEAMS):
        join(client, code, f"Team {i}")

    full = client.post(f"/join/{code}", json={"team_name": "One Too Many"})
    assert full.status_code == 409
    assert str(team_service.MAX_TEAMS) in full.json()["detail"]


# ------------------------------------------------------- the admin token


def test_a_player_token_cannot_act_as_the_admin_token(client):
    """The whole point of prefixing them. A player must not rotate the code."""
    season = make_season(client)
    code, _ = open_season(client, season)
    player = join(client, code, "Just A Player")

    assert client.get(f"/seasons/{season}/invite", headers=auth(player)).status_code == 401
    assert client.post(f"/seasons/{season}/invite", headers=auth(player)).status_code == 401
    assert client.get(f"/seasons/{season}/players", headers=auth(player)).status_code == 401
    assert client.patch(f"/seasons/{season}/invite", json={"is_open": False},
                        headers=auth(player)).status_code == 401


def test_an_admin_token_cannot_act_as_a_player(client):
    season = make_season(client)
    _, admin = open_season(client, season)
    assert client.get("/me", headers=auth(admin)).status_code == 401
    assert client.get("/me/plans", headers=auth(admin)).status_code == 401


def test_one_seasons_admin_token_does_not_open_another(client):
    first, second = make_season(client), make_season(client, name="Other League")
    _, admin_one = open_season(client, first)
    open_season(client, second)
    assert client.get(f"/seasons/{second}/invite", headers=auth(admin_one)).status_code == 401


def test_the_invite_code_is_not_public(client):
    """Anyone who can read the code can join, so a read needs the admin token."""
    season = make_season(client)
    open_season(client, season)
    assert client.get(f"/seasons/{season}/invite").status_code == 401
    assert client.get(f"/seasons/{season}/players").status_code == 401

    # The lobby is public, and carries no code.
    lobby = client.get(f"/seasons/{season}/lobby")
    assert lobby.status_code == 200
    assert "join_code" not in lobby.text


# ----------------------------------------------------------- the payload


def test_no_response_ever_carries_a_token_hash(client):
    season = make_season(client)
    code, admin = open_season(client, season)
    token = join(client, code, "Sinnoh Slammers", "Logan")
    client.post("/me/plans", json={"name": "P", "body": {"x": 1}}, headers=auth(token))

    for response in (
        client.get(f"/seasons/{season}/lobby"),
        client.get(f"/seasons/{season}/invite", headers=auth(admin)),
        client.get(f"/seasons/{season}/players", headers=auth(admin)),
        client.get("/me", headers=auth(token)),
        client.get("/me/plans", headers=auth(token)),
        client.get(f"/seasons/{season}/teams"),
    ):
        assert "token_hash" in response.text or True   # readability of the next line
        assert "hash" not in response.text.lower(), response.url
        assert "dmp_" not in response.text, response.url
        assert "dma_" not in response.text, response.url


# --------------------------------------------------------- private plans


@pytest.fixture()
def two_players(client):
    """Alice and Bob in the same season, each with one plan."""
    season = make_season(client)
    code, admin = open_season(client, season)
    alice = join(client, code, "Alice's Aggron")
    bob = join(client, code, "Bob's Blaziken")

    alice_plan = client.post("/me/plans", json={"name": "Rain", "body": {"slots": ["politoed"]}},
                             headers=auth(alice)).json()
    bob_plan = client.post("/me/plans", json={"name": "Sand", "body": {"slots": ["tyranitar"]}},
                           headers=auth(bob)).json()
    return client, season, admin, (alice, alice_plan), (bob, bob_plan)


def test_a_plan_round_trips_with_its_body_intact(two_players):
    client, _, _, (alice, plan), _ = two_players
    fetched = client.get(f"/me/plans/{plan['id']}", headers=auth(alice)).json()
    assert fetched["name"] == "Rain"
    assert fetched["body"] == {"slots": ["politoed"]}


def test_a_player_sees_only_their_own_plans(two_players):
    client, _, _, (alice, _), (bob, _) = two_players
    assert [p["name"] for p in client.get("/me/plans", headers=auth(alice)).json()] == ["Rain"]
    assert [p["name"] for p in client.get("/me/plans", headers=auth(bob)).json()] == ["Sand"]


def test_a_player_cannot_read_another_players_plan(two_players):
    client, _, _, (alice, _), (_, bob_plan) = two_players
    stolen = client.get(f"/me/plans/{bob_plan['id']}", headers=auth(alice))
    absent = client.get("/me/plans/999999", headers=auth(alice))

    # A plan that exists but is not Alice's must be indistinguishable from one
    # that does not exist, or the status code becomes a way to count how many
    # plans the rest of the league has. Only the echoed id differs, and that
    # was Alice's own input.
    assert stolen.status_code == absent.status_code == 404
    strip_id = lambda body: re.sub(r"\d+", "N", body["detail"])
    assert strip_id(stolen.json()) == strip_id(absent.json())


def test_a_player_cannot_overwrite_another_players_plan(two_players):
    client, _, _, (alice, _), (bob, bob_plan) = two_players
    attempt = client.put(f"/me/plans/{bob_plan['id']}",
                         json={"name": "Owned", "body": {"slots": []}}, headers=auth(alice))
    assert attempt.status_code == 404

    intact = client.get(f"/me/plans/{bob_plan['id']}", headers=auth(bob)).json()
    assert intact["name"] == "Sand"
    assert intact["body"] == {"slots": ["tyranitar"]}


def test_a_player_cannot_delete_another_players_plan(two_players):
    client, _, _, (alice, _), (bob, bob_plan) = two_players
    assert client.delete(f"/me/plans/{bob_plan['id']}", headers=auth(alice)).status_code == 404
    assert client.get(f"/me/plans/{bob_plan['id']}", headers=auth(bob)).status_code == 200


def test_the_commissioner_cannot_read_a_players_plans_either(two_players):
    """Admin is not a super-user. Private means private from everyone."""
    client, season, admin, _, _ = two_players
    assert client.get("/me/plans", headers=auth(admin)).status_code == 401
    assert "Rain" not in client.get(f"/seasons/{season}/players", headers=auth(admin)).text


def test_updating_a_plan_can_change_the_name_the_body_or_both(two_players):
    client, _, _, (alice, plan), _ = two_players
    renamed = client.put(f"/me/plans/{plan['id']}", json={"name": "Rain v2"},
                         headers=auth(alice)).json()
    assert renamed["name"] == "Rain v2"
    assert renamed["body"] == {"slots": ["politoed"]}      # untouched

    rebodied = client.put(f"/me/plans/{plan['id']}", json={"body": {"slots": ["pelipper"]}},
                          headers=auth(alice)).json()
    assert rebodied["name"] == "Rain v2"                   # untouched
    assert rebodied["body"] == {"slots": ["pelipper"]}

    assert client.put(f"/me/plans/{plan['id']}", json={}, headers=auth(alice)).status_code == 400


def test_an_oversized_plan_is_refused(two_players):
    client, _, _, (alice, _), _ = two_players
    huge = {"name": "Huge", "body": {"junk": "x" * (300 * 1024)}}
    refused = client.post("/me/plans", json=huge, headers=auth(alice))
    assert refused.status_code == 400
    assert "KB" in refused.json()["detail"]


def test_leaving_takes_the_team_and_the_plans_with_it(two_players):
    client, season, _, (alice, alice_plan), (bob, _) = two_players
    assert client.delete("/me", headers=auth(alice)).status_code == 204

    # Token dead, team gone from the public roster, plan unreachable by anyone.
    assert client.get("/me", headers=auth(alice)).status_code == 401
    names = [t["name"] for t in client.get(f"/seasons/{season}/lobby").json()["teams"]]
    assert names == ["Bob's Blaziken"]

    # Bob is untouched, and Alice's freed name is available again.
    assert client.get("/me/plans", headers=auth(bob)).json()[0]["name"] == "Sand"


def test_a_freed_team_name_can_be_taken_by_the_next_player(client):
    season = make_season(client)
    code, _ = open_season(client, season)
    first = join(client, code, "Sinnoh Slammers")
    client.delete("/me", headers=auth(first))
    assert client.post(f"/join/{code}", json={"team_name": "Sinnoh Slammers"}).status_code == 201


def test_plans_are_scoped_per_player_not_per_season(client):
    """The same person joining two seasons keeps two separate sets of plans."""
    first, second = make_season(client), make_season(client, name="Second League")
    code_one, _ = open_season(client, first)
    code_two, _ = open_season(client, second)
    token_one = join(client, code_one, "Same Name")
    token_two = join(client, code_two, "Same Name")   # fine: different season

    client.post("/me/plans", json={"name": "For League One", "body": {}}, headers=auth(token_one))
    assert [p["name"] for p in client.get("/me/plans", headers=auth(token_two)).json()] == []


# ------------------------------------------- taking a team you already own

"""The commissioner's way in.

Whoever ran the draft from one laptop holds an admin token and no team, but
one of those teams is theirs — and everything that asks "which team are you"
reads a player token. Issuing one is also the only route back for a player who
lost theirs, which is why it rotates and says so.
"""


def test_the_commissioner_can_issue_a_token_for_a_team(client):
    season = make_season(client)
    code, admin = open_season(client, season)
    join(client, code, "Jersey Jawns")
    team = client.get(f"/seasons/{season}/teams").json()[0]

    issued = client.post(f"/seasons/{season}/teams/{team['id']}/token", headers=auth(admin))
    assert issued.status_code == 200
    body = issued.json()
    assert body["team"]["name"] == "Jersey Jawns"
    assert body["rotated"] is True
    assert body["token"].startswith("dmp_")


def test_the_issued_token_is_that_team(client):
    season = make_season(client)
    code, admin = open_season(client, season)
    join(client, code, "Jersey Jawns")
    team = client.get(f"/seasons/{season}/teams").json()[0]

    token = client.post(f"/seasons/{season}/teams/{team['id']}/token",
                        headers=auth(admin)).json()["token"]
    me = client.get("/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["team"]["id"] == team["id"]
    assert me.json()["season_id"] == season


def test_issuing_stops_the_old_token_working(client):
    """The transfer of control is real, which is why the response flags it."""
    season = make_season(client)
    code, admin = open_season(client, season)
    old = join(client, code, "Jersey Jawns")
    team = client.get(f"/seasons/{season}/teams").json()[0]

    client.post(f"/seasons/{season}/teams/{team['id']}/token", headers=auth(admin))
    assert client.get("/me", headers=auth(old)).status_code == 401


def test_a_team_with_no_player_behind_it_gets_a_first_token(client):
    """A team the commissioner made by hand has nobody holding it yet."""
    season = make_season(client)
    _, admin = open_season(client, season)
    team = client.post(f"/seasons/{season}/teams", json={"name": "Bench Team"},
                       headers=auth(admin)).json()

    issued = client.post(f"/seasons/{season}/teams/{team['id']}/token", headers=auth(admin))
    assert issued.status_code == 200
    assert issued.json()["rotated"] is False
    assert client.get("/me", headers=auth(issued.json()["token"])).json()["team"]["id"] == team["id"]


def test_issuing_needs_the_admin_token(client):
    season = make_season(client)
    code, _ = open_season(client, season)
    player = join(client, code, "Jersey Jawns")
    team = client.get(f"/seasons/{season}/teams").json()[0]

    assert client.post(f"/seasons/{season}/teams/{team['id']}/token").status_code in (401, 403)
    refused = client.post(f"/seasons/{season}/teams/{team['id']}/token", headers=auth(player))
    assert refused.status_code in (401, 403)
    # And the player who holds it is untouched by the attempt.
    assert client.get("/me", headers=auth(player)).status_code == 200


def test_issuing_for_a_team_in_another_season_is_a_404(client):
    mine = make_season(client)
    other = make_season(client, name="Other League")
    _, admin = open_season(client, mine)
    code_other, _ = open_season(client, other)
    join(client, code_other, "Elsewhere")
    stranger = client.get(f"/seasons/{other}/teams").json()[0]

    refused = client.post(f"/seasons/{mine}/teams/{stranger['id']}/token", headers=auth(admin))
    assert refused.status_code == 404


# --------------------------------------------------- the commissioner gates

# These cover the lock added so the API can be exposed publicly. The threat is
# concrete: without it, anyone holding the URL can re-price 771 Pokemon, delete
# a team mid-draft, or trigger an 800-request pool rebuild.


@pytest.fixture()
def claimed(client):
    """A league someone has claimed, plus one player in it."""
    season = make_season(client)
    code, admin = open_season(client, season)
    player = join(client, code, "Sinnoh Slammers")
    team_id = client.get("/me", headers=auth(player)).json()["team"]["id"]
    return client, season, admin, player, team_id


SETUP_CALLS = [
    ("post", "/seasons/{s}/teams", {"name": "Sneaky"}),
    ("post", "/seasons/{s}/teams/order", []),
    ("post", "/seasons/{s}/pool/costs", {"costs": {"garchomp": 1}}),
    ("post", "/seasons/{s}/pool/bans", {"names": ["garchomp"], "banned": True}),
    ("post", "/seasons/{s}/pool/entries", {"name": "garchomp"}),
    ("post", "/seasons/{s}/pool/paste", {"cost_list": "Garchomp, 5"}),
    ("post", "/seasons/{s}/pool/custom", {"content": "Garchomp", "replace": True}),
    ("post", "/seasons/{s}/pool/from-format/gen9-lc", None),
]


@pytest.mark.parametrize("method,path,body", SETUP_CALLS)
def test_league_setup_is_refused_without_the_commissioner_token(claimed, method, path, body):
    client, season, _, _, _ = claimed
    call = getattr(client, method)
    assert call(path.format(s=season), json=body).status_code == 403


@pytest.mark.parametrize("method,path,body", SETUP_CALLS)
def test_a_players_token_does_not_unlock_league_setup(claimed, method, path, body):
    """The people most likely to try are the ones already in the league."""
    client, season, _, player, _ = claimed
    call = getattr(client, method)
    assert call(path.format(s=season), json=body, headers=auth(player)).status_code == 403


def test_deleting_a_team_needs_the_commissioner_token(claimed):
    client, season, admin, player, team_id = claimed
    path = f"/seasons/{season}/teams/{team_id}"
    assert client.delete(path).status_code == 403
    assert client.delete(path, headers=auth(player)).status_code == 403
    assert client.delete(path, headers=auth(admin)).status_code == 204


def test_an_unclaimed_league_is_still_open_to_set_up(client):
    """Nobody has opened it, so there is no token to hold — same bargain as
    the join code, and the same as season_service._require_owner already makes.
    Without this, a fresh install could not be set up at all."""
    season = make_season(client)
    assert client.post(f"/seasons/{season}/teams", json={"name": "A"}).status_code == 201
    assert client.post(f"/seasons/{season}/pool/costs",
                       json={"costs": {"garchomp": 1}}).status_code == 200


def test_a_player_may_rename_their_own_team_but_not_anothers(claimed):
    client, season, admin, player, team_id = claimed
    code = client.get(f"/seasons/{season}/invite", headers=auth(admin)).json()["join_code"]
    other = join(client, code, "Hoenn Hurricanes")
    other_team = client.get("/me", headers=auth(other)).json()["team"]["id"]

    mine = client.patch(f"/seasons/{season}/teams/{team_id}",
                        json={"name": "Renamed"}, headers=auth(player))
    assert mine.status_code == 200

    theirs = client.patch(f"/seasons/{season}/teams/{other_team}",
                          json={"name": "Stolen"}, headers=auth(player))
    assert theirs.status_code == 403
    # The commissioner may rename anyone's.
    assert client.patch(f"/seasons/{season}/teams/{other_team}",
                        json={"name": "Fixed"}, headers=auth(admin)).status_code == 200


def test_reading_a_league_never_needs_a_token(claimed):
    """The board, the roster and the standings stay public: a draft board
    nobody else can see is a spreadsheet."""
    client, season, _, _, team_id = claimed
    for path in (
        "/seasons",
        f"/seasons/{season}",
        f"/seasons/{season}/pool",
        f"/seasons/{season}/pool/summary",
        f"/seasons/{season}/lobby",
        f"/seasons/{season}/teams",
        f"/seasons/{season}/teams/{team_id}",
        f"/seasons/{season}/draft/board",
    ):
        assert client.get(path).status_code == 200, path
