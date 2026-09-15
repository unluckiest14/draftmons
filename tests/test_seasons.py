"""Tests for the league list, and for renaming or deleting one.

Running several leagues off one server has always been possible; keeping them
has not, because nothing listed them. So most of what matters here is that the
list says enough to tell two leagues apart, and that the destructive half of
managing them cannot be aimed at somebody else's.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

POOL = "Great Tusk, 19\nKingambit, 18\nGholdengo, 17\nDragapult, 16\n"
STUB = {
    "great-tusk": ["ground", "fighting"], "kingambit": ["dark", "steel"],
    "gholdengo": ["steel", "ghost"], "dragapult": ["dragon", "ghost"],
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
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_DB", str(tmp_path / "d.db"))
    # No module reloading here any more. It existed to make poke_db pick up a
    # new DRAFT_DB, which connect() now reads per call — and once these modules
    # lived in a package, popping them from sys.modules stopped reloading them
    # anyway (the parent package keeps an attribute for the old object) while
    # happily producing a second copy of the app for the fixture to configure
    # and the test to miss.
    main = importlib.import_module("draftmons.app")
    pokeapi = importlib.import_module("draftmons.pokeapi")

    http = TestClient(main.app)
    http.__enter__()
    main.app.state.poke = pokeapi.PokeApiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(wire))
    )
    yield http
    http.__exit__(None, None, None)


def make(client, name, **fields):
    return client.post("/seasons", json={"name": name, **fields}).json()


def leagues(client):
    return {row["name"]: row for row in client.get("/seasons").json()}


# ------------------------------------------------------------- the list


def test_the_list_is_empty_before_any_league_exists(client):
    assert client.get("/seasons").json() == []


def test_every_league_is_listed(client):
    for name in ("Spring Cup", "Sunday League", "Mock"):
        make(client, name)
    assert set(leagues(client)) == {"Spring Cup", "Sunday League", "Mock"}


def test_the_newest_league_is_first(client):
    """A commissioner opens the one they just made far more often than the rest."""
    for name in ("First", "Second", "Third"):
        make(client, name)
    # created_at is a whole second, so same-second rows fall back to the id.
    assert [row["name"] for row in client.get("/seasons").json()][0] == "Third"


def test_past_the_thirtieth_league_is_still_listed(client):
    """The id-probing the list route replaced could not see this one."""
    for index in range(31):
        make(client, f"League {index}")
    assert "League 30" in leagues(client)


def test_a_league_carries_the_counts_that_tell_it_apart(client):
    empty = make(client, "Empty")
    full = make(client, "Full", budget=40, roster_size=2)
    client.post(f"/seasons/{full['id']}/pool/custom", json={"content": POOL})
    invite = client.post(f"/seasons/{full['id']}/invite", json={}).json()
    for team in ("Alpha", "Bravo"):
        client.post(f"/join/{invite['join_code']}", json={"team_name": team})

    rows = leagues(client)
    assert (rows["Full"]["teams"], rows["Full"]["pool_size"], rows["Full"]["priced"]) == (2, 4, 4)
    assert rows["Full"]["draft_status"] == "setup"
    assert rows["Full"]["claimed"] is True
    assert rows["Full"]["is_open"] is True

    assert (rows["Empty"]["teams"], rows["Empty"]["pool_size"]) == (0, 0)
    assert rows["Empty"]["claimed"] is False
    assert empty["id"] != full["id"]


def test_a_pool_does_not_multiply_the_team_count(client):
    """The counts are subqueries for exactly this reason."""
    season = make(client, "Joined", budget=40, roster_size=2)
    client.post(f"/seasons/{season['id']}/pool/custom", json={"content": POOL})
    invite = client.post(f"/seasons/{season['id']}/invite", json={}).json()
    client.post(f"/join/{invite['join_code']}", json={"team_name": "Alpha"})

    row = leagues(client)["Joined"]
    assert row["teams"] == 1
    assert row["pool_size"] == 4


def test_the_list_follows_a_draft_from_setup_to_finished(client):
    season = make(client, "Running", budget=40, roster_size=2)
    sid = season["id"]
    client.post(f"/seasons/{sid}/pool/custom", json={"content": POOL})
    invite = client.post(f"/seasons/{sid}/invite", json={}).json()
    admin = {"Authorization": f"Bearer {invite['admin_token']}"}
    tokens = {}
    for name in ("Alpha", "Bravo"):
        joined = client.post(f"/join/{invite['join_code']}", json={"team_name": name}).json()
        tokens[name] = {"Authorization": f"Bearer {joined['token']}"}
    teams = {t["name"]: t["id"] for t in client.get(f"/seasons/{sid}/teams").json()}
    client.post(f"/seasons/{sid}/teams/order", json=[teams["Alpha"], teams["Bravo"]],
                headers=admin)

    assert leagues(client)["Running"]["draft_status"] == "setup"

    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    row = leagues(client)["Running"]
    assert row["draft_status"] == "live"
    assert (row["picks_made"], row["picks_total"]) == (0, 4)

    for who, mon in [("Alpha", "great-tusk"), ("Bravo", "kingambit"),
                     ("Bravo", "gholdengo"), ("Alpha", "dragapult")]:
        assert client.post(f"/seasons/{sid}/draft/pick", json={"name": mon},
                           headers=tokens[who]).status_code == 200

    row = leagues(client)["Running"]
    assert row["draft_status"] == "complete"
    assert (row["picks_made"], row["picks_total"]) == (4, 4)


def test_played_matches_show_on_the_list(client):
    season = make(client, "Played", budget=40, roster_size=2)
    sid = season["id"]
    invite = client.post(f"/seasons/{sid}/invite", json={}).json()
    admin = {"Authorization": f"Bearer {invite['admin_token']}"}
    for team in ("Alpha", "Bravo"):
        client.post(f"/join/{invite['join_code']}", json={"team_name": team})
    teams = {t["name"]: t["id"] for t in client.get(f"/seasons/{sid}/teams").json()}
    client.post(f"/seasons/{sid}/matches", json={
        "week_no": 1, "home_team_id": teams["Alpha"], "away_team_id": teams["Bravo"],
        "winner_team_id": teams["Alpha"],
    }, headers=admin)

    assert leagues(client)["Played"]["matches_played"] == 1


# --------------------------------------------------------------- renaming


def test_an_unclaimed_league_can_be_renamed_by_anyone(client):
    """Nobody owns it — its invite has never been opened — so nobody is shut out."""
    season = make(client, "Untitled")
    renamed = client.patch(f"/seasons/{season['id']}", json={"name": "Spring Cup"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Spring Cup"


def test_renaming_a_claimed_league_needs_its_token(client):
    season = make(client, "Mine")
    client.post(f"/seasons/{season['id']}/invite", json={})
    refused = client.patch(f"/seasons/{season['id']}", json={"name": "Yours"})
    assert refused.status_code == 403
    assert leagues(client)["Mine"]["name"] == "Mine"


def test_the_commissioner_can_rename_their_own_league(client):
    season = make(client, "Mine")
    invite = client.post(f"/seasons/{season['id']}/invite", json={}).json()
    admin = {"Authorization": f"Bearer {invite['admin_token']}"}
    assert client.patch(f"/seasons/{season['id']}", json={"name": "Spring Cup"},
                        headers=admin).json()["name"] == "Spring Cup"


def test_a_blank_name_is_refused(client):
    season = make(client, "Mine")
    assert client.patch(f"/seasons/{season['id']}", json={"name": "   "}).status_code == 422


def test_the_budget_can_be_fixed_before_the_draft_starts(client):
    season = make(client, "Spring Cup", budget=100)
    assert client.patch(f"/seasons/{season['id']}",
                        json={"budget": 120}).json()["budget"] == 120


def test_the_budget_is_frozen_once_the_draft_has_started(client):
    """Points already spent were spent against the old number."""
    season = make(client, "Spring Cup", budget=40, roster_size=2)
    sid = season["id"]
    client.post(f"/seasons/{sid}/pool/custom", json={"content": POOL})
    invite = client.post(f"/seasons/{sid}/invite", json={}).json()
    admin = {"Authorization": f"Bearer {invite['admin_token']}"}
    for team in ("Alpha", "Bravo"):
        client.post(f"/join/{invite['join_code']}", json={"team_name": team})
    teams = [t["id"] for t in client.get(f"/seasons/{sid}/teams").json()]
    client.post(f"/seasons/{sid}/teams/order", json=teams, headers=admin)
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)

    refused = client.patch(f"/seasons/{sid}", json={"budget": 500}, headers=admin)
    assert refused.status_code == 409
    assert "already started" in refused.json()["detail"]
    # Renaming is a label, so it still works.
    assert client.patch(f"/seasons/{sid}", json={"name": "Spring Cup 2"},
                        headers=admin).status_code == 200


def test_an_empty_patch_says_what_to_send(client):
    season = make(client, "Mine")
    refused = client.patch(f"/seasons/{season['id']}", json={})
    assert refused.status_code == 409
    assert "budget" in refused.json()["detail"]


# --------------------------------------------------------------- deleting


def test_deleting_a_claimed_league_needs_its_token(client):
    season = make(client, "Mine")
    client.post(f"/seasons/{season['id']}/invite", json={})
    assert client.delete(f"/seasons/{season['id']}").status_code == 403
    assert "Mine" in leagues(client)


def test_deleting_takes_the_whole_league_with_it(client):
    season = make(client, "Doomed", budget=40, roster_size=2)
    sid = season["id"]
    client.post(f"/seasons/{sid}/pool/custom", json={"content": POOL})
    invite = client.post(f"/seasons/{sid}/invite", json={}).json()
    admin = {"Authorization": f"Bearer {invite['admin_token']}"}
    for team in ("Alpha", "Bravo"):
        client.post(f"/join/{invite['join_code']}", json={"team_name": team})

    gone = client.delete(f"/seasons/{sid}", headers=admin)
    assert gone.status_code == 200
    assert (gone.json()["teams"], gone.json()["pool_size"]) == (2, 4)

    assert client.get("/seasons").json() == []
    assert client.get(f"/seasons/{sid}").status_code == 404
    assert client.get(f"/seasons/{sid}/teams").json() == []
    assert client.get(f"/seasons/{sid}/pool").json() == []


def test_deleting_one_league_leaves_the_others_alone(client):
    keep = make(client, "Keep")
    drop = make(client, "Drop")
    client.post(f"/seasons/{drop['id']}/pool/custom", json={"content": POOL})
    client.delete(f"/seasons/{drop['id']}")

    assert set(leagues(client)) == {"Keep"}
    assert client.get(f"/seasons/{keep['id']}").status_code == 200


def test_deleting_a_league_that_is_not_there_is_a_404(client):
    assert client.delete("/seasons/999").status_code == 404
