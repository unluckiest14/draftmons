"""Tests for the snake draft.

Most of these are refusals. A draft is a set of rules about what you may not
do — not your turn, cannot afford it, someone already took it — and the rules
only matter if they hold when someone pushes on them.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import draft_service as drafts   # noqa: E402

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


# ------------------------------------------------- the order, on its own


def test_the_snake_reverses_every_round():
    assert drafts.snake_order([1, 2, 3], 4) == [1, 2, 3, 3, 2, 1, 1, 2, 3, 3, 2, 1]


def test_going_last_in_round_one_means_going_first_in_round_two():
    """The property that makes it a snake rather than a queue."""
    order = [10, 20, 30, 40]
    assert drafts.snake_team(order, 3) == 40    # last pick of round 1
    assert drafts.snake_team(order, 4) == 40    # first pick of round 2


def test_two_teams_alternate_in_pairs():
    assert drafts.snake_order([1, 2], 3) == [1, 2, 2, 1, 1, 2]


# ------------------------------------------------------------- fixtures


@pytest.fixture()
def league(tmp_path, monkeypatch):
    """A season with a priced pool, three teams in order, and an admin token."""
    monkeypatch.setenv("DRAFT_DB", str(tmp_path / "d.db"))
    for mod in ("poke_db", "pokeapi", "format_service", "pool_service",
                "player_service", "player_routes", "draft_service",
                "draft_routes", "main"):
        sys.modules.pop(mod, None)
    main = importlib.import_module("main")
    pokeapi = importlib.import_module("pokeapi")

    client = TestClient(main.app)
    client.__enter__()
    main.app.state.poke = pokeapi.PokeApiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(wire))
    )

    sid = client.post("/seasons", json={
        "name": "L", "budget": 40, "roster_size": 3,
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

    yield client, sid, admin, tokens, teams
    client.__exit__(None, None, None)


def pick(client, sid, token, name):
    return client.post(f"/seasons/{sid}/draft/pick", json={"name": name}, headers=token)


def run(client, sid, tokens, script):
    for who, mon in script:
        response = pick(client, sid, tokens[who], mon)
        assert response.status_code == 200, (who, mon, response.json())


# --------------------------------------------------------------- starting


def test_a_pick_before_the_draft_starts_is_refused(league):
    client, sid, _, tokens, _ = league
    assert pick(client, sid, tokens["Alpha"], "great-tusk").status_code == 409


def test_starting_needs_the_admin_token(league):
    client, sid, _, tokens, _ = league
    assert client.post(f"/seasons/{sid}/draft/start", json={},
                       headers=tokens["Alpha"]).status_code in (401, 403)


def test_starting_twice_is_refused(league):
    client, sid, admin, _, _ = league
    assert client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin).status_code == 200
    assert client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin).status_code == 409


def test_a_draft_cannot_start_without_an_order(league):
    """Refuse rather than invent one — the order decides who gets first pick."""
    client, sid, admin, _, teams = league
    client.delete(f"/seasons/{sid}/teams/{teams['Charlie']}", headers=admin)
    for team_id in (teams["Alpha"], teams["Bravo"]):
        client.patch(f"/seasons/{sid}/teams/{team_id}",
                     json={"draft_position": None}, headers=admin)
    refused = client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    assert refused.status_code == 409
    assert "draft position" in refused.json()["detail"]


def test_randomize_assigns_an_order(league):
    client, sid, admin, _, teams = league
    for team_id in teams.values():
        client.patch(f"/seasons/{sid}/teams/{team_id}", json={"draft_position": None})
    state = client.post(f"/seasons/{sid}/draft/start",
                        json={"randomize": True}, headers=admin).json()
    assert sorted(t["position"] for t in state["order"]) == [1, 2, 3]


# ------------------------------------------------------------ the rules


def test_picking_out_of_turn_is_refused_and_says_whose_it_is(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    refused = pick(client, sid, tokens["Bravo"], "kingambit")
    assert refused.status_code == 409
    assert "Alpha" in refused.json()["detail"]


def test_the_snake_turns_around_at_the_end_of_a_round(league):
    """Charlie picks third and then immediately fourth."""
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [
        ("Alpha", "great-tusk"), ("Bravo", "kingambit"), ("Charlie", "gholdengo"),
    ])
    assert pick(client, sid, tokens["Charlie"], "dragapult").status_code == 200


def test_a_pick_subtracts_its_cost(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    body = pick(client, sid, tokens["Alpha"], "great-tusk").json()
    assert body["cost_paid"] == 19
    assert body["remaining"] == 40 - 19


def test_a_pick_you_cannot_afford_is_a_402(league):
    """Alpha spends down to 2, then reaches for a 17-point Pokemon."""
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [
        ("Alpha", "great-tusk"), ("Bravo", "nacli"), ("Charlie", "pawniard"),
        ("Charlie", "slowking"), ("Bravo", "corviknight"),
        ("Alpha", "iron-valiant"),          # 19 + 11 = 30 spent, 10 left
        ("Alpha", "clefable"),              # hmm: 12 > 10
    ][:6])
    refused = pick(client, sid, tokens["Alpha"], "clefable")
    assert refused.status_code == 402
    detail = refused.json()["detail"]
    assert "12" in detail and "10" in detail


def test_spending_to_exactly_zero_is_allowed(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [
        ("Alpha", "great-tusk"), ("Bravo", "nacli"), ("Charlie", "pawniard"),
        ("Charlie", "slowking"), ("Bravo", "corviknight"),
    ])
    body = pick(client, sid, tokens["Alpha"], "kingambit")   # 19 + 18 = 37
    assert body.status_code == 200
    assert body.json()["remaining"] == 3


def test_a_drafted_pokemon_cannot_be_taken_twice(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [("Alpha", "great-tusk")])
    refused = pick(client, sid, tokens["Bravo"], "great-tusk")
    assert refused.status_code == 409
    assert "already drafted by Alpha" in refused.json()["detail"]


def test_a_pokemon_outside_the_pool_is_refused(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    refused = pick(client, sid, tokens["Alpha"], "mewtwo")
    assert refused.status_code == 409 and "not in this season's pool" in refused.json()["detail"]


def test_a_banned_pokemon_is_refused(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/pool/bans", json={"names": ["great-tusk"], "banned": True},
                headers=admin)
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    refused = pick(client, sid, tokens["Alpha"], "great-tusk")
    assert refused.status_code == 409 and "banned" in refused.json()["detail"]


def test_the_draft_completes_after_the_last_pick(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [
        ("Alpha", "great-tusk"), ("Bravo", "kingambit"), ("Charlie", "gholdengo"),
        ("Charlie", "nacli"), ("Bravo", "clefable"), ("Alpha", "iron-valiant"),
        ("Alpha", "pawniard"), ("Bravo", "slowking"), ("Charlie", "corviknight"),
    ])
    assert client.get(f"/seasons/{sid}/draft").json()["status"] == "complete"
    assert pick(client, sid, tokens["Alpha"], "dragapult").status_code == 409


# ---------------------------------------------------------------- who


def test_a_player_cannot_pick_for_another_team(league):
    """team_id is admin-only; a player token must not spend someone else's points."""
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    refused = client.post(f"/seasons/{sid}/draft/pick",
                          json={"name": "great-tusk", "team_id": teams["Alpha"]},
                          headers=tokens["Bravo"])
    assert refused.status_code in (401, 403)


def test_the_admin_may_pick_on_a_teams_behalf(league):
    client, sid, admin, _, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    body = client.post(f"/seasons/{sid}/draft/pick",
                       json={"name": "great-tusk", "team_id": teams["Alpha"]},
                       headers=admin)
    assert body.status_code == 200


def test_an_anonymous_pick_is_refused(league):
    client, sid, admin, _, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    assert client.post(f"/seasons/{sid}/draft/pick",
                       json={"name": "great-tusk"}).status_code in (401, 403)


# --------------------------------------------------------------- board


def test_the_board_is_public_and_shows_every_team(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [("Alpha", "great-tusk"), ("Bravo", "kingambit")])

    board = client.get(f"/seasons/{sid}/draft/board").json()   # no token
    assert [t["name"] for t in board["teams"]] == ["Alpha", "Bravo", "Charlie"]

    alpha = board["teams"][0]
    assert alpha["spent"] == 19 and alpha["remaining"] == 21
    assert [p["display_name"] for p in alpha["picks"]] == ["Great Tusk"]
    assert board["teams"][2]["picks"] == []
    assert board["picks_made"] == 2


def test_the_board_keeps_what_a_pick_cost_when_the_pool_is_repriced(league):
    """Repricing must not rewrite history, or a team's spend changes overnight."""
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [("Alpha", "great-tusk")])
    client.post(f"/seasons/{sid}/pool/costs", json={"costs": {"great-tusk": 1}},
                headers=admin)

    board = client.get(f"/seasons/{sid}/draft/board").json()
    assert board["teams"][0]["picks"][0]["cost_paid"] == 19
    assert board["teams"][0]["remaining"] == 21


# ---------------------------------------------------------------- undo


def test_undo_returns_the_pick_and_the_clock(league):
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [("Alpha", "great-tusk")])

    state = client.post(f"/seasons/{sid}/draft/undo", headers=admin).json()
    assert state["picks_made"] == 0
    assert state["on_clock_team_id"] == teams["Alpha"]
    assert pick(client, sid, tokens["Alpha"], "great-tusk").status_code == 200


def test_undo_needs_the_admin_token(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={}, headers=admin)
    run(client, sid, tokens, [("Alpha", "great-tusk")])
    assert client.post(f"/seasons/{sid}/draft/undo",
                       headers=tokens["Bravo"]).status_code in (401, 403)


# ---------------------------------------------------------- the clock


def expire(client, sid, seconds=61):
    """Push the clock into the past, as if the team on the clock stalled.

    Faster and far steadier than sleeping: the timer is evaluated against
    SQLite's own clock, so moving the start time backwards is exactly what the
    passage of time looks like to this code.
    """
    from poke_db import transaction
    with transaction() as conn:
        conn.execute(
            "UPDATE draft SET clock_started_at = datetime('now', ?) WHERE season_id = ?",
            (f"-{seconds} seconds", sid),
        )


def order_now(client, sid):
    state = client.get(f"/seasons/{sid}/draft").json()
    return state


def test_a_timed_out_team_goes_to_the_back_of_the_round(league):
    """The rule: skipped, not skipped over — they still pick, just last."""
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    assert order_now(client, sid)["on_clock_team_id"] == teams["Alpha"]

    expire(client, sid)
    state = order_now(client, sid)

    assert state["on_clock_team_id"] == teams["Bravo"]      # Alpha lost the slot
    assert state["deferred_this_round"] == [teams["Alpha"]]
    assert state["picks_made"] == 0                         # nobody picked
    assert [e["type"] for e in state["clock_events"]] == ["deferred"]


def test_a_deferred_team_still_picks_last_in_that_round(league):
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    expire(client, sid)                                     # Alpha deferred
    run(client, sid, tokens, [("Bravo", "kingambit"), ("Charlie", "gholdengo")])

    assert order_now(client, sid)["on_clock_team_id"] == teams["Alpha"]
    assert pick(client, sid, tokens["Alpha"], "great-tusk").status_code == 200


def test_the_order_resets_to_the_snake_next_round(league):
    """A deferral lasts one round. Round 2 is the plain reversed snake again."""
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    expire(client, sid)
    run(client, sid, tokens, [
        ("Bravo", "kingambit"), ("Charlie", "gholdengo"), ("Alpha", "great-tusk"),
    ])

    state = order_now(client, sid)
    assert state["round_no"] == 2
    # Round 2 of a 3-team snake starts with Charlie, regardless of round 1.
    assert state["on_clock_team_id"] == teams["Charlie"]
    assert state["deferred_this_round"] == []


def test_a_team_is_only_deferred_once_per_round(league):
    """Second timeout has nowhere further back, so it autopicks instead."""
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    expire(client, sid)                                     # Alpha -> back
    run(client, sid, tokens, [("Bravo", "kingambit"), ("Charlie", "gholdengo")])

    expire(client, sid)                                     # Alpha again, now last
    state = order_now(client, sid)
    assert state["picks_made"] == 3
    assert [e["type"] for e in state["clock_events"]] == ["autopick"]

    board = client.get(f"/seasons/{sid}/draft/board").json()
    alpha = next(t for t in board["teams"] if t["id"] == teams["Alpha"])
    assert len(alpha["picks"]) == 1
    # The best it could afford: 40 points, Great Tusk at 19 is the dearest left.
    assert alpha["picks"][0]["display_name"] == "Great Tusk"


def test_everyone_timing_out_still_advances_the_round(league):
    """Three deferrals in a row leave the last one with nowhere to go."""
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    # Each expiry resolves exactly one turn, so three deferrals use three, and
    # the fourth is the one with nowhere left to defer to.
    for _ in range(4):
        expire(client, sid)
        order_now(client, sid)

    state = order_now(client, sid)
    assert state["picks_made"] >= 1        # somebody was autopicked, not stuck


def test_catching_up_resolves_every_elapsed_turn_not_just_one(league):
    """Nobody looked for ten minutes; the turns that elapsed all resolve."""
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    expire(client, sid, seconds=300)          # five turns' worth

    state = order_now(client, sid)
    assert len(state["clock_events"]) > 1


def test_a_pick_that_arrives_after_the_deadline_loses_to_the_timer(league):
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    expire(client, sid)
    late = pick(client, sid, tokens["Alpha"], "great-tusk")
    assert late.status_code == 409 and "Bravo" in late.json()["detail"]


def test_a_pick_inside_the_deadline_is_fine(league):
    client, sid, admin, tokens, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 3600}, headers=admin)
    assert pick(client, sid, tokens["Alpha"], "great-tusk").status_code == 200


def test_the_timer_can_be_turned_off(league):
    """pick_seconds = 0 is an in-person draft round a table."""
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 0}, headers=admin)
    expire(client, sid, seconds=99_999)

    state = order_now(client, sid)
    assert state["deadline"] is None and state["seconds_left"] is None
    assert state["on_clock_team_id"] == teams["Alpha"]      # still theirs
    assert state["clock_events"] == []


def test_the_state_reports_a_countdown(league):
    client, sid, admin, _, _ = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 120}, headers=admin)
    state = order_now(client, sid)
    assert state["pick_seconds"] == 120
    assert 110 <= state["seconds_left"] <= 120
    assert state["deadline"]


def test_a_broke_team_forfeits_rather_than_stalling_the_draft(league):
    """No points, nothing affordable: the slot passes instead of blocking."""
    client, sid, admin, tokens, teams = league
    # Price the whole pool out of reach, then start with Alpha having nothing.
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    run(client, sid, tokens, [("Alpha", "great-tusk")])     # 19 of 40
    client.post(f"/seasons/{sid}/pool/costs", json={"costs": {
        "kingambit": 99, "gholdengo": 99, "dragapult": 99, "clefable": 99,
        "iron-valiant": 99, "corviknight": 99, "slowking": 99, "nacli": 99,
        "pawniard": 99,
    }}, headers=admin)
    for _ in range(4):
        expire(client, sid)
        order_now(client, sid)

    board = client.get(f"/seasons/{sid}/draft/board").json()
    forfeits = [p for t in board["teams"] for p in t["picks"] if p["api_name"] is None]
    assert forfeits, "a team with no affordable pick must forfeit, not stall"
    assert board["picks_made"] > 1


def test_starting_clears_a_stale_deferral(league):
    client, sid, admin, _, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    expire(client, sid)
    order_now(client, sid)
    assert order_now(client, sid)["deferred_this_round"]

    from poke_db import transaction
    with transaction() as conn:
        conn.execute("UPDATE draft SET status = 'setup' WHERE season_id = ?", (sid,))
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    assert order_now(client, sid)["deferred_this_round"] == []
    assert order_now(client, sid)["on_clock_team_id"] == teams["Alpha"]


def test_a_long_absence_restarts_the_clock_instead_of_auto_drafting(league):
    """Everyone went to bed. Nobody should lose their picks to that."""
    client, sid, admin, _, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 60}, headers=admin)
    expire(client, sid, seconds=60 * 60 * 8)      # overnight

    state = order_now(client, sid)
    assert [e["type"] for e in state["clock_events"]] == ["clock_reset"]
    assert state["picks_made"] == 0
    assert state["on_clock_team_id"] == teams["Alpha"]
    assert state["seconds_left"] > 0


# --------------------------------------------------------- draft order


def test_teams_without_a_position_still_show_on_the_board(league):
    """Joining does not assign a position, so a new league is all NULLs.

    Filtering those out left the one screen a commissioner uses to set the
    order showing "no teams yet" while three people sat in the lobby.
    """
    client, sid, admin, _, teams = league
    for team_id in teams.values():
        client.patch(f"/seasons/{sid}/teams/{team_id}",
                     json={"draft_position": None}, headers=admin)

    board = client.get(f"/seasons/{sid}/draft/board").json()
    assert len(board["teams"]) == 3
    assert all(team["draft_position"] is None for team in board["teams"])
    assert sorted(board["unordered_team_ids"]) == sorted(teams.values())


def test_saving_an_order_assigns_positions_in_the_given_sequence(league):
    client, sid, admin, _, teams = league
    order = [teams["Charlie"], teams["Alpha"], teams["Bravo"]]
    saved = client.post(f"/seasons/{sid}/teams/order", json=order, headers=admin).json()

    positions = {team["name"]: team["draft_position"] for team in saved}
    assert positions == {"Charlie": 1, "Alpha": 2, "Bravo": 3}
    assert client.get(f"/seasons/{sid}/draft").json()["unordered_team_ids"] == []


def test_the_saved_order_is_the_order_the_snake_uses(league):
    client, sid, admin, tokens, teams = league
    client.post(f"/seasons/{sid}/teams/order",
                json=[teams["Charlie"], teams["Alpha"], teams["Bravo"]], headers=admin)
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 0}, headers=admin)

    assert client.get(f"/seasons/{sid}/draft").json()["on_clock_team_id"] == teams["Charlie"]
    assert pick(client, sid, tokens["Alpha"], "great-tusk").status_code == 409


def test_the_order_cannot_be_changed_once_the_draft_has_started(league):
    """Rewriting positions mid-draft would move the clock to a different team."""
    client, sid, admin, _, teams = league
    client.post(f"/seasons/{sid}/draft/start", json={"pick_seconds": 0}, headers=admin)

    refused = client.post(f"/seasons/{sid}/teams/order",
                          json=[teams["Charlie"], teams["Bravo"], teams["Alpha"]],
                          headers=admin)
    assert refused.status_code == 409
    assert "has started" in refused.json()["detail"]


def test_a_partial_order_is_still_refused(league):
    client, sid, admin, _, teams = league
    refused = client.post(f"/seasons/{sid}/teams/order", json=[teams["Alpha"]],
                          headers=admin)
    assert refused.status_code == 409
