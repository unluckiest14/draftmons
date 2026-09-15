#!/usr/bin/env python3
"""One command that walks three players through joining a season.

    python smoke_players.py

Writes to its own scratch database, never to draft.db, so it is safe to
re-run. Entirely offline: joining touches no external service.

This is the manual counterpart to test_players.py. The tests assert the
privacy properties; this prints them, so you can watch a player be refused
another player's plan rather than take a green dot's word for it.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRATCH = os.path.join(tempfile.gettempdir(), "draftmons_players.db")
os.environ["DRAFT_DB"] = SCRATCH
if os.path.exists(SCRATCH):
    os.remove(SCRATCH)

# Run from anywhere: put the project root on the path before importing the app.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient   # noqa: E402

from draftmons import app as main                         # noqa: E402
from draftmons.services import team_service                         # noqa: E402

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}  {label}{'  — ' + detail if detail else ''}")
    if not condition:
        failures.append(label)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def main_() -> int:
    client = TestClient(main.app)
    with client:
        print("\n=== 1. A commissioner opens a season for joining ===")
        season = client.post(
            "/seasons", json={"name": "Smoke League", "budget": 120, "roster_size": 10}
        ).json()
        opened = client.post(f"/seasons/{season['id']}/invite").json()
        code, admin = opened["join_code"], opened["admin_token"]

        print(f"    season     : {season['name']} (id {season['id']})")
        print(f"    join code  : {code}")
        print(f"    admin token: {admin[:16]}…")
        check("a join code is issued", len(code) == 8)
        check("an admin token is issued once", bool(admin) and admin.startswith("dma_"))
        check("reading the invite back does not re-issue the token",
              client.get(f"/seasons/{season['id']}/invite",
                         headers=auth(admin)).json()["admin_token"] is None)

        print("\n=== 2. Three players join, each choosing their own team name ===")
        tokens: dict[str, str] = {}
        for team_name, who in [
            ("Sinnoh Slammers", "Logan"),
            ("Hoenn Hurricanes", "Sam"),
            ("Kanto Kings", "Alex"),
        ]:
            response = client.post(
                f"/join/{code}", json={"team_name": team_name, "display_name": who}
            )
            body = response.json()
            tokens[team_name] = body["token"]
            print(f"    {response.status_code}  {team_name:<18} {who:<6} "
                  f"token {body['token'][:16]}…")
            check(f"{who} joined as {team_name}", response.status_code == 201)

        clash = client.post(f"/join/{code}", json={"team_name": "Sinnoh Slammers"})
        check("a duplicate team name is refused", clash.status_code == 409,
              clash.json()["detail"])

        print("\n=== 3. The lobby is public; the join code is not ===")
        lobby = client.get(f"/seasons/{season['id']}/lobby").json()
        print(f"    {lobby['season_name']}: {[t['name'] for t in lobby['teams']]}")
        check("the lobby needs no token", bool(lobby["teams"]))
        check("the lobby carries no join code", "join_code" not in str(lobby))
        check("the invite needs the admin token",
              client.get(f"/seasons/{season['id']}/invite").status_code == 401)

        print("\n=== 4. Each player saves a private plan ===")
        plans: dict[str, int] = {}
        for team_name, token in tokens.items():
            plan = client.post(
                "/me/plans",
                json={"name": f"{team_name} draft",
                      "body": {"slots": [{"api_name": "garchomp", "item": "Leftovers"}]}},
                headers=auth(token),
            ).json()
            plans[team_name] = plan["id"]
            print(f"    {team_name:<18} plan id {plan['id']}")
        check("each player sees exactly their own plan", all(
            [p["name"] for p in client.get("/me/plans", headers=auth(token)).json()]
            == [f"{team_name} draft"]
            for team_name, token in tokens.items()
        ))

        print("\n=== 5. Privacy: one player reaching for another's plan ===")
        logan, sam = tokens["Sinnoh Slammers"], tokens["Hoenn Hurricanes"]
        sams_plan = plans["Hoenn Hurricanes"]

        read = client.get(f"/me/plans/{sams_plan}", headers=auth(logan))
        wrote = client.put(f"/me/plans/{sams_plan}", json={"name": "pwned"}, headers=auth(logan))
        killed = client.delete(f"/me/plans/{sams_plan}", headers=auth(logan))
        print(f"    Logan reads   Sam's plan -> {read.status_code}  {read.json()['detail']}")
        print(f"    Logan writes  Sam's plan -> {wrote.status_code}")
        print(f"    Logan deletes Sam's plan -> {killed.status_code}")
        check("read is refused", read.status_code == 404)
        check("overwrite is refused", wrote.status_code == 404)
        check("delete is refused", killed.status_code == 404)

        intact = client.get(f"/me/plans/{sams_plan}", headers=auth(sam)).json()
        check("Sam's plan is untouched", intact["name"] == "Hoenn Hurricanes draft")

        absent = client.get("/me/plans/999999", headers=auth(logan))
        check("someone else's plan looks the same as a missing one",
              read.status_code == absent.status_code)

        print("\n=== 6. Privacy: crossing the player / commissioner line ===")
        checks = [
            ("a player cannot read the invite",
             client.get(f"/seasons/{season['id']}/invite", headers=auth(logan))),
            ("a player cannot rotate the code",
             client.post(f"/seasons/{season['id']}/invite", headers=auth(logan))),
            ("a player cannot list the roster",
             client.get(f"/seasons/{season['id']}/players", headers=auth(logan))),
            ("the commissioner cannot read plans",
             client.get("/me/plans", headers=auth(admin))),
            ("the commissioner is not a player",
             client.get("/me", headers=auth(admin))),
        ]
        for label, response in checks:
            check(label, response.status_code == 401, f"{response.status_code}")

        roster = client.get(f"/seasons/{season['id']}/players", headers=auth(admin)).json()
        print(f"    admin roster: {[(r['team_name'], r['display_name']) for r in roster]}")
        # Looks for the plans themselves, not the word "draft" — the roster
        # legitimately carries a draft_position column, which is not a leak.
        plan_traces = [f"{name} draft" for name in plans] + ["garchomp", "Leftovers"]
        check("the roster names no plans",
              not any(trace in str(roster) for trace in plan_traces))

        print("\n=== 7. A player owns their own team name ===")
        renamed = client.patch("/me", json={"team_name": "Sinnoh Superstars"},
                               headers=auth(logan))
        print(f"    renamed to: {renamed.json()['team']['name']}")
        check("a player renames their own team", renamed.status_code == 200)
        check("the public lobby follows", "Sinnoh Superstars" in str(
            client.get(f"/seasons/{season['id']}/lobby").json()))

        print("\n=== 8. Closing the season ===")
        closed = client.patch(f"/seasons/{season['id']}/invite",
                              json={"is_open": False}, headers=auth(admin)).json()
        late = client.post(f"/join/{code}", json={"team_name": "Too Late"})
        check("the season closes", closed["is_open"] is False)
        check("a latecomer is refused", late.status_code == 403, late.json()["detail"])
        check("players already inside are unaffected",
              client.get("/me", headers=auth(logan)).status_code == 200)

        print("\n=== 9. Leaving takes the team and the plans with it ===")
        client.delete("/me", headers=auth(logan))
        after = client.get(f"/seasons/{season['id']}/lobby").json()
        check("the token stops working",
              client.get("/me", headers=auth(logan)).status_code == 401)
        check("the team leaves the lobby",
              "Sinnoh Superstars" not in str(after),
              f"remaining: {[t['name'] for t in after['teams']]}")
        check("the other players are untouched",
              client.get("/me/plans", headers=auth(sam)).json()[0]["name"]
              == "Hoenn Hurricanes draft")

        print("\n=== 10. No response anywhere carries a token ===")
        surfaces = [
            client.get(f"/seasons/{season['id']}/lobby"),
            client.get(f"/seasons/{season['id']}/invite", headers=auth(admin)),
            client.get(f"/seasons/{season['id']}/players", headers=auth(admin)),
            client.get("/me", headers=auth(sam)),
            client.get("/me/plans", headers=auth(sam)),
            client.get(f"/seasons/{season['id']}/teams"),
        ]
        leaked = [r.url.path for r in surfaces
                  if "dmp_" in r.text or "dma_" in r.text or "hash" in r.text.lower()]
        check("no token or hash in any response", not leaked, str(leaked))

        capacity = team_service.MAX_TEAMS
        print(f"\n    (a season holds {capacity} teams; "
              f"{len(after['teams'])} in this one after Logan left)")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print(f"All checks passed. Scratch database: {SCRATCH}")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
