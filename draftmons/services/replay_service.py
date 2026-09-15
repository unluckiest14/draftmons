"""Reading a Pokemon Showdown replay, and counting what happened in it.

The point is to stop someone transcribing a battle by hand. A draft league
records, per match, who won and which Pokemon got which knockouts; all of that
is already in the replay, and typing it out is both tedious and the place
mistakes enter a leaderboard that then reports them as fact all season.

Showdown's battle log is a line protocol. The lines that matter here:

    |player|p1|PkNo|...              which side is whose
    |poke|p1|Pelipper, M|item        team preview, before any nicknames
    |switch|p1a: reppileP|Pelipper, M, shiny|100/100
    |move|p1a: reppileP|Scald|p2a: Baby Mode
    |-damage|p2a: Baby Mode|0 fnt
    |faint|p2a: Baby Mode
    |win|PkNo

Two things make this harder than it looks.

**Nicknames.** Players rename their Pokemon, so nothing in the fighting refers
to a species. "reppileP" is a Pelipper and "bentley demon" is a Ferrothorn, and
one real replay nicknames a Conkeldurr to a single invisible character. The
only way to know is the `|switch|` line, which carries both. So identity is
tracked per side as nickname -> species, rebuilt every time something switches
in.

**Who gets the kill.** The last thing to damage a Pokemon is what killed it,
and that is not always the obvious attacker:

    |-damage|p2a: Fraud Watch|0 fnt|[from] item: Rocky Helmet|[of] p1a: bentley demon

Crustle died to recoil off Ferrothorn's Rocky Helmet while attacking it, and
the KO belongs to Ferrothorn. `[of]` names the Pokemon responsible whenever
Showdown knows one. When there is no `[of]` and no `[from]`, the killer is
whoever used the most recent move at that target. When there is a `[from]` but
no `[of]` — Stealth Rock, sandstorm, burn — nothing killed it that a league
would credit, and the KO is left unattributed rather than guessed at.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from typing import Any, NamedTuple

# https://replay.pokemonshowdown.com/gen7ou-2491876324  ->  gen7ou-2491876324
REPLAY_URL = re.compile(
    r"^(?:https?://)?(?:[\w-]+\.)*replay\.pokemonshowdown\.com/"
    r"(?:[\w-]+/)?([A-Za-z0-9-]+?)(?:\.(?:json|log))?/?$"
)
BARE_ID = re.compile(r"^[a-z0-9]+-\d+(?:-[a-z0-9]+)?$", re.IGNORECASE)

# "p1a: reppileP" -> ("p1", "reppileP"). The letter is the slot, which matters
# in doubles and not at all for counting.
SLOT = re.compile(r"^(p[12])[a-c]?:\s*(.*)$")

# Damage that came from something other than a move, e.g. "[from] Stealth Rock"
# or "[from] item: Rocky Helmet".
FROM_TAG = re.compile(r"\[from\]\s*([^|]+)")
OF_TAG = re.compile(r"\[of\]\s*([^|]+)")

TIMEOUT = 20


class ReplayError(RuntimeError):
    """The replay could not be fetched or made sense of. User-facing message."""


class Mon(NamedTuple):
    species: str
    nickname: str
    kills: int
    fainted: bool


# --------------------------------------------------------------- fetching


def replay_id(url: str) -> str:
    """The replay id out of a URL, or the id itself if that is what was given."""
    text = (url or "").strip()
    if not text:
        raise ReplayError("Paste a Showdown replay link.")

    match = REPLAY_URL.match(text)
    if match:
        return match.group(1)
    if BARE_ID.match(text):
        return text
    raise ReplayError(
        "That does not look like a Showdown replay link. It should be like "
        "https://replay.pokemonshowdown.com/gen9ou-1234567890"
    )


def fetch(url: str) -> dict[str, Any]:
    """Download a replay. Returns Showdown's JSON, log included."""
    ident = replay_id(url)
    endpoint = f"https://replay.pokemonshowdown.com/{ident}.json"
    request = urllib.request.Request(endpoint, headers={"User-Agent": "draftmons/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReplayError(
                f"Showdown has no replay {ident!r}. Private replays cannot be read — "
                "upload it publicly, or paste the log instead."
            ) from exc
        raise ReplayError(f"Showdown returned {exc.code} for that replay.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ReplayError(f"Could not reach Showdown: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayError("Showdown did not return a readable replay.") from exc

    if not payload.get("log"):
        raise ReplayError("That replay has no battle log in it.")
    return payload


# ---------------------------------------------------------------- parsing


def _species(raw: str) -> str:
    """"Pelipper, M, shiny" -> "Pelipper". Gender and shininess are noise here."""
    return raw.split(",")[0].strip()


def _cause(source: "re.Match[str] | None") -> str | None:
    """"[from] item: Rocky Helmet" -> "Rocky Helmet".

    Showdown prefixes the kind of thing onto the name — item, ability, move —
    and a reader does not need telling that Rocky Helmet is an item.
    """
    if source is None:
        return None
    text = source.group(1).strip()
    for prefix in ("item:", "ability:", "move:"):
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return text or None


def describe(event: dict[str, Any]) -> str:
    """One knockout as a sentence, the way a league writes it up."""
    victim = f"{event['victim_player']}'s {event['victim']}"
    if not event.get("cause"):
        return f"{victim} fainted."

    how = "fainted indirectly from" if event["indirect"] else "fainted from"
    line = f"{victim} {how} {event['cause']}"
    if event.get("killer"):
        line += f" by {event['killer_player']}'s {event['killer']}"
    return line + "."


def parse(log: str) -> dict[str, Any]:
    """Count a battle. Returns players, per-Pokemon kills, deaths and the winner."""
    # side -> species seen in team preview, including anything never sent out.
    preview: dict[str, list[str]] = {"p1": [], "p2": []}
    # side -> nickname -> record.
    #
    # Keyed by nickname, not species, because species is not stable: a Mega
    # Evolution rewrites it mid-battle, and keying on it counted Golisopod and
    # Golisopod-Mega as two separate Pokemon — one that survived and one that
    # fainted. The nickname survives the change.
    mons: dict[str, dict[str, dict[str, Any]]] = {"p1": {}, "p2": {}}
    players: dict[str, str] = {}
    # (side, nickname) of whatever last damaged each Pokemon.
    last_hit: dict[tuple[str, str], tuple[str, str] | None] = {}
    # What did that damage: ("Scald", False) for a move, ("Rocky Helmet", True)
    # for anything the log attributes with [from]. The flag is what separates
    # "fainted from Scald" from "fainted indirectly from Rocky Helmet".
    last_cause: dict[tuple[str, str], tuple[str, bool]] = {}
    last_move: tuple[str, str] | None = None
    turn = 0
    events: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"turns": 0, "format": None, "winner": None, "tie": False}

    def ensure(side: str, nickname: str, species: str = "") -> dict[str, Any]:
        record = mons[side].get(nickname)
        if record is None:
            record = {"species": species or nickname, "nickname": nickname,
                      "forms": [], "kills": 0, "fainted": False,
                      # Until a switch names it, `species` is only the
                      # nickname standing in for one.
                      "named": bool(species)}
            mons[side][nickname] = record
        elif species and species != record["species"]:
            # A form change. The base species stays the identity — a league
            # drafted Golisopod, not Golisopod-Mega — and the form is noted.
            if species not in record["forms"]:
                record["forms"].append(species)
        return record

    def resolve(target: str) -> tuple[str, str] | None:
        """"p1a: reppileP" -> ("p1", "reppileP")."""
        match = SLOT.match(target.strip())
        if not match:
            return None
        return match.group(1), match.group(2).strip()

    for line in log.split("\n"):
        if not line.startswith("|"):
            continue
        parts = line.split("|")[1:]
        if not parts:
            continue
        tag, args = parts[0], parts[1:]

        if tag == "player" and len(args) >= 2 and args[1]:
            players[args[0]] = args[1]

        elif tag == "poke" and len(args) >= 2:
            # Team preview: every Pokemon brought, including ones never sent
            # out. Without this a Pokemon that sat on the bench is missing
            # from the stat line entirely.
            preview.setdefault(args[0], []).append(_species(args[1]))

        elif tag in ("switch", "drag") and len(args) >= 2:
            match = SLOT.match(args[0])
            if match:
                record = ensure(match.group(1), match.group(2).strip())
                species = _species(args[1])
                if not record["named"]:
                    # First sighting fixes the base species. Later switches are
                    # ignored for this: a Pokemon that Mega Evolves and then
                    # switches back in re-enters as "Golisopod-Mega", and
                    # taking that as its species would file the KOs under a
                    # Pokemon no league ever drafted.
                    record["species"] = species
                    record["named"] = True
                elif species != record["species"] and species not in record["forms"]:
                    record["forms"].append(species)

        elif tag in ("replace", "detailschange") and len(args) >= 2:
            # `replace` is Illusion dropping — the species was a lie until now,
            # so it is corrected rather than recorded as a form.
            # `detailschange` is a Mega or Primal, which is a form.
            match = SLOT.match(args[0])
            if match:
                side, nickname = match.group(1), match.group(2).strip()
                species = _species(args[1])
                record = ensure(side, nickname)
                if tag == "replace":
                    record["species"] = species
                    record["named"] = True
                elif species not in record["forms"]:
                    record["forms"].append(species)

        elif tag == "move" and len(args) >= 3:
            user = resolve(args[0])
            target = resolve(args[2]) if len(args) > 2 else None
            last_move = user
            if user and target:
                last_hit[target] = user
                last_cause[target] = (args[1], False)

        elif tag in ("-damage", "-crit", "-supereffective") and args:
            hurt = resolve(args[0])
            if hurt is None:
                continue
            source = FROM_TAG.search(line)
            culprit = OF_TAG.search(line)
            if culprit:
                # "[of] p1a: bentley demon" — Showdown naming who is
                # responsible for indirect damage. This is the Rocky Helmet
                # and Leech Seed case, and it is the right answer.
                last_hit[hurt] = resolve(culprit.group(1))
                last_cause[hurt] = (_cause(source), True)
            elif source:
                # Hazards, weather, status: real damage with nobody to credit.
                last_hit[hurt] = None
                last_cause[hurt] = (_cause(source), True)
            elif last_move and last_move != hurt:
                last_hit[hurt] = last_move

        elif tag == "faint" and args:
            died = resolve(args[0])
            if died is None:
                continue
            victim = ensure(*died)
            victim["fainted"] = True
            killer = last_hit.get(died)
            # A Pokemon cannot knock itself out for credit — recoil and
            # Life Orb are deaths with no killer, not self-KOs.
            scored = killer if (killer and killer != died) else None
            if scored:
                ensure(*scored)["kills"] += 1

            cause, indirect = last_cause.get(died, (None, False))
            events.append({
                "turn": turn,
                "victim_side": died[0],
                "victim": victim["species"],
                "killer_side": scored[0] if scored else None,
                "killer": ensure(*scored)["species"] if scored else None,
                "cause": cause,
                "indirect": indirect,
            })

        elif tag == "turn" and args:
            turn = int(args[0]) if args[0].isdigit() else turn
            meta["turns"] = max(meta["turns"], turn)

        elif tag == "tier" and args:
            meta["format"] = args[0]

        elif tag == "win" and args:
            meta["winner"] = args[0]

        elif tag == "tie":
            meta["tie"] = True

    sides = []
    for side in ("p1", "p2"):
        # Anything in team preview that never came out still belongs on the
        # stat line, at nothing scored and nothing lost.
        seen = {record["species"] for record in mons[side].values()}
        for species in preview.get(side, []):
            if species not in seen:
                mons[side].setdefault(species, {
                    "species": species, "nickname": "", "forms": [],
                    "kills": 0, "fainted": False, "named": True,
                })
                seen.add(species)
        team = sorted(
            ({k: v for k, v in record.items() if k != "named"}
             for record in mons[side].values()),
            key=lambda m: m["species"],
        )
        sides.append({
            "side": side,
            "player": players.get(side, side),
            "won": bool(meta["winner"]) and players.get(side) == meta["winner"],
            "kills": sum(m["kills"] for m in team),
            "deaths": sum(1 for m in team if m["fainted"]),
            # Pokemon left standing, which is how a draft league writes a score.
            "remaining": sum(1 for m in team if not m["fainted"]),
            "pokemon": team,
        })

    if not any(side["pokemon"] for side in sides):
        raise ReplayError("No Pokémon were found in that replay log.")

    for event in events:
        event["victim_player"] = players.get(event["victim_side"], event["victim_side"])
        event["killer_player"] = (
            players.get(event["killer_side"]) if event["killer_side"] else None
        )
        event["text"] = describe(event)

    return {**meta, "players": players, "sides": sides, "events": events}


def analyze(url: str) -> dict[str, Any]:
    """Fetch and count, in one call."""
    payload = fetch(url)
    result = parse(payload["log"])
    result["replay_id"] = payload.get("id")
    result["replay_url"] = f"https://replay.pokemonshowdown.com/{payload.get('id')}"
    result["format"] = result.get("format") or payload.get("format")
    result["uploaded_at"] = payload.get("uploadtime")
    return result


# ------------------------------------------------- matching it to a league


def _pool_index(conn: sqlite3.Connection, season_id: int) -> dict[str, sqlite3.Row]:
    """{showdown id: pool entry} for a season, for matching species by name."""
    return {
        row["showdown_id"]: row
        for row in conn.execute(
            "SELECT * FROM pool_entry WHERE season_id = ?", (season_id,)
        )
    }


def _owners(conn: sqlite3.Connection, season_id: int) -> dict[int, int]:
    """{pool_entry_id: team_id} from the draft picks."""
    return {
        row["pool_entry_id"]: row["team_id"]
        for row in conn.execute(
            "SELECT pool_entry_id, team_id FROM draft_pick "
            "WHERE season_id = ? AND pool_entry_id IS NOT NULL",
            (season_id,),
        )
    }


def propose(
    conn: sqlite3.Connection, season_id: int, analysis: dict[str, Any]
) -> dict[str, Any]:
    """Work out which league teams played, and build a recordable stat line.

    Sides are matched by their Pokemon, not by their Showdown username. A
    player's Showdown handle has nothing to do with their team name and
    changes whenever they feel like it, but the six Pokemon they brought were
    drafted by exactly one team — so counting whose picks appear on each side
    identifies it, and does so even for someone playing under a new alt.
    """
    pool = _pool_index(conn, season_id)
    owners = _owners(conn, season_id)
    team_names = {
        row["id"]: row["name"]
        for row in conn.execute("SELECT id, name FROM team WHERE season_id = ?", (season_id,))
    }

    sides = []
    for side in analysis["sides"]:
        votes: dict[int, int] = {}
        matched, unknown = [], []

        for mon in side["pokemon"]:
            entry = pool.get(re.sub(r"[^a-z0-9]", "", mon["species"].lower()))
            if entry is None:
                unknown.append(mon["species"])
                continue
            owner = owners.get(entry["id"])
            if owner is not None:
                votes[owner] = votes.get(owner, 0) + 1
            matched.append({
                "species": mon["species"],
                "api_name": entry["api_name"],
                "pool_entry_id": entry["id"],
                "sprite_url": entry["sprite_url"],
                "team_id": owner,
                "kills": mon["kills"],
                "fainted": mon["fainted"],
            })

        best = max(votes.items(), key=lambda pair: pair[1], default=(None, 0))
        sides.append({
            **{k: v for k, v in side.items() if k != "pokemon"},
            "team_id": best[0],
            "team_name": team_names.get(best[0]),
            # How much of the side's team was actually drafted by them. A low
            # number is the signal that the guess is worth checking.
            "confidence": best[1],
            "pokemon": matched,
            "not_in_pool": unknown,
        })

    # The knockout history names species; the board wants pictures. Resolved
    # here rather than in the browser because the pool already holds a sprite
    # for every drafted Pokemon, and a second lookup per event would be a
    # PokeAPI round trip for something already sitting in the database.
    art = {
        mon["species"]: mon["sprite_url"]
        for side in sides for mon in side["pokemon"] if mon["sprite_url"]
    }
    for event in analysis.get("events", []):
        event["victim_sprite"] = art.get(event.get("victim"))
        event["killer_sprite"] = art.get(event.get("killer"))

    home, away = sides[0], sides[1]
    winner_id = next(
        (side["team_id"] for side in sides if side["won"] and side["team_id"]), None
    )

    problems = []
    for side in sides:
        if side["team_id"] is None:
            problems.append(
                f"{side['player']}'s team could not be matched to anyone in this "
                "league — none of their Pokémon were drafted here."
            )
        if side["not_in_pool"]:
            problems.append(
                f"Not in this season's pool: {', '.join(side['not_in_pool'])}."
            )
    if home["team_id"] and home["team_id"] == away["team_id"]:
        problems.append(
            f"Both sides matched {home['team_name']}. Pick the teams by hand."
        )

    return {
        "analysis": analysis,
        "sides": sides,
        "home_team_id": home["team_id"],
        "away_team_id": away["team_id"],
        "winner_team_id": winner_id,
        # The schema's score is Pokemon left standing, which is how a draft
        # league writes one.
        "home_score": home["remaining"],
        "away_score": away["remaining"],
        "replay_url": analysis.get("replay_url"),
        "elims": [
            {"team_id": side["team_id"], "name": mon["api_name"], "elims": mon["kills"]}
            for side in sides
            for mon in side["pokemon"]
            if side["team_id"] and mon["kills"]
        ],
        "problems": problems,
        "ready": not problems,
    }


# ------------------------------------------------------- the KO history


def store_events(
    conn: sqlite3.Connection, match_id: int, events: list[dict[str, Any]]
) -> int:
    """Save a match's knockout history. Replaces whatever was there."""
    conn.execute("DELETE FROM match_event WHERE match_id = ?", (match_id,))
    conn.executemany(
        "INSERT INTO match_event (match_id, ordinal, turn_no, victim_side, victim, "
        "  killer_side, killer, cause, indirect, text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (match_id, index, event.get("turn") or 0,
             event.get("victim_side"), event.get("victim"),
             event.get("killer_side"), event.get("killer"),
             event.get("cause"), 1 if event.get("indirect") else 0,
             event.get("text") or "")
            for index, event in enumerate(events)
        ],
    )
    return len(events)


def match_events(
    conn: sqlite3.Connection, match_id: int, season_id: int | None = None
) -> list[dict[str, Any]]:
    """One match's knockout history, in the order it happened.

    Sprites are looked up rather than stored: art is a property of the Pokemon,
    not of the match, and freezing a URL into every event row would leave the
    history pointing at stale images the day the pool is rebuilt.
    """
    events = [
        {**dict(row), "indirect": bool(row["indirect"])}
        for row in conn.execute(
            "SELECT turn_no, victim_side, victim, killer_side, killer, cause, "
            "       indirect, text FROM match_event "
            "WHERE match_id = ? ORDER BY ordinal",
            (match_id,),
        )
    ]
    if season_id is None or not events:
        return events

    art = {
        row["showdown_id"]: row["sprite_url"]
        for row in conn.execute(
            "SELECT showdown_id, sprite_url FROM pool_entry WHERE season_id = ?",
            (season_id,),
        )
    }
    key = lambda name: re.sub(r"[^a-z0-9]", "", (name or "").lower())   # noqa: E731
    for event in events:
        event["victim_sprite"] = art.get(key(event.get("victim")))
        event["killer_sprite"] = art.get(key(event.get("killer")))
    return events
