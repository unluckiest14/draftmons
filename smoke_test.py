#!/usr/bin/env python3
"""One command that exercises the whole backend against real data.

    python smoke_test.py              # quick: builds the 225-Pokemon LC pool
    python smoke_test.py gen9-ou      # any format key
    python smoke_test.py --all        # every format, no pool build (fast, offline)

Writes to a scratch database, never to draft.db, so it is safe to re-run.
Needs the network for the pool step, because that is the half of the system
that talks to PokeAPI. Everything before it is offline.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

SCRATCH = os.path.join(tempfile.gettempdir(), "draftmons_smoke.db")
os.environ["DRAFT_DB"] = SCRATCH
if os.path.exists(SCRATCH):
    os.remove(SCRATCH)

from fastapi.testclient import TestClient   # noqa: E402

import format_service as fmt                # noqa: E402
import main                                 # noqa: E402
from poke_db import init_db, transaction    # noqa: E402

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}  {label}{'  — ' + detail if detail else ''}")
    if not condition:
        failures.append(label)


def main_() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    every = "--all" in sys.argv
    format_key = argv[0] if argv else "gen9-lc"

    print("\n=== 1. Formats: parsing the files in this folder ===")
    print(f"  tiers : {fmt.DEFAULT_SOURCE}")
    import showdown_rules as rules
    print(f"  rules : {rules.FORMATS_TS}")

    init_db()   # create the tables before anything reads them
    with transaction() as conn:
        report = fmt.refresh(conn)
    check("formats-data.js parsed and format tables built", report["changed"])
    check("config/formats.ts applied (bans + clauses)", report["rules_applied"],
          f"{report['species_banned']} species banned beyond their tier")

    client = TestClient(main.app)
    with client:
        formats = client.get("/formats").json()
        check(f"{len(formats)} formats offered", len(formats) == 18)
        print()
        for f in formats:
            extra = f"  (-{f['species_banned']} banlist)" if f["species_banned"] else ""
            print(f"    {f['ladder']:8} {f['key']:22} {f['species_count']:5} legal{extra}")

        print("\n=== 2. Rules: what Showdown enforces beyond the species list ===")
        rules_body = client.get(f"/formats/{format_key}/rules").json()
        check("rules endpoint answers", bool(rules_body["clauses"]),
              f"{len(rules_body['clauses'])} clauses")
        print(f"    clauses      : {', '.join(rules_body['clauses'][:6])} ...")
        print(f"    banned tiers : {rules_body['banned_tiers']}")
        print(f"    banned moves/abilities/items: {len(rules_body['banned_other'])}")
        if rules_body["banned_species"]:
            print(f"    banned species: {len(rules_body['banned_species'])}")

        if every:
            print("\n  --all: skipping the pool build (that is the network half).")
            return finish()

        print(f"\n=== 3. Pool: building {format_key} from PokeAPI ===")
        season = client.post("/seasons", json={
            "name": "Smoke Test League", "budget": 100, "roster_size": 8,
        }).json()
        sid = season["id"]

        started = time.time()
        built = client.post(f"/seasons/{sid}/pool/from-format/{format_key}")
        if built.status_code != 200:
            check("pool build", False, f"HTTP {built.status_code}: {built.text[:200]}")
            return finish()
        built = built.json()
        elapsed = time.time() - started

        check("pool built from the selected format", built["added"] > 0,
              f"{built['added']} Pokemon in {elapsed:.1f}s")
        check("every Pokemon resolved to PokeAPI", not built["unresolved"],
              f"{len(built['unresolved'])} unresolved")

        entries = client.get(f"/seasons/{sid}/pool?limit=3").json()
        check("entries carry types, stats and sprites", bool(
            entries and entries[0]["types"] and entries[0]["stats"]
            and entries[0]["sprite_url"]
        ))
        print()
        for e in entries:
            print(f"    {e['display_name']:22} {str(e['types']):26} "
                  f"bst={e['bst']:4} tier={e['tier']}")

        print("\n=== 4. Draft setup: teams, order, costs ===")
        for name in ("Sinnoh Slammers", "Kanto Kings", "Hoenn Hurricanes"):
            client.post(f"/seasons/{sid}/teams", json={"name": name})
        teams = client.get(f"/seasons/{sid}/teams").json()
        check("teams created", len(teams) == 3)

        order = client.post(f"/seasons/{sid}/teams/order",
                            json=[t["id"] for t in reversed(teams)]).json()
        check("draft order assigned", all(t["draft_position"] for t in order))

        dupe = client.post(f"/seasons/{sid}/teams", json={"name": "Kanto Kings"})
        check("duplicate team name rejected", dupe.status_code == 409)

        sample = client.get(f"/seasons/{sid}/pool?limit=2").json()
        priced = client.post(f"/seasons/{sid}/pool/costs", json={
            "costs": {sample[0]["api_name"]: 19, sample[1]["api_name"]: 15},
        }).json()
        check("costs applied", priced["updated"] == 2)

        pasted = client.post(f"/seasons/{sid}/pool/paste", json={
            "cost_list": "Pikachu, 7\nNotARealPokemon, 3",
        }).json()
        check("cost list pasted, bad names reported not guessed",
              pasted["added"] == 1 and len(pasted["unmatched"]) == 1,
              f"unmatched: {[u['name'] for u in pasted['unmatched']]}")

        banned = client.post(f"/seasons/{sid}/pool/bans", json={
            "names": [sample[0]["api_name"]], "banned": True,
        }).json()
        check("house-rule ban hides a Pokemon", banned["updated"] == 1)

        summary = client.get(f"/seasons/{sid}/pool/summary").json()
        check("summary reports the board", summary["total"] > 0,
              f"{summary['total']} total, {summary['priced']} priced, "
              f"{summary['banned']} banned")

    return finish()


def finish() -> int:
    print()
    if failures:
        print(f"FAILED — {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    print(f"\nScratch database: {SCRATCH}  (draft.db was not touched)")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
