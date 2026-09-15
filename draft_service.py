"""The snake draft: whose turn it is, what a pick costs, and what it leaves.

A snake draft reverses direction every round. With three teams and the order
A, B, C, picks run:

    round 1   A  B  C
    round 2   C  B  A
    round 3   A  B  C

That is the whole point of the format — going last in round 1 means going
first in round 2 — and it means the team on the clock is a pure function of
how many picks have been made. Nothing stores "whose turn"; it is derived from
`count(picks)` every time, so it cannot drift out of step with the picks
themselves.

Two invariants are enforced by the schema rather than by checks here, because
two people picking at the same instant is the normal case at a draft, not an
edge case:

    UNIQUE (season_id, pick_no)        two picks cannot take the same slot
    UNIQUE (season_id, pool_entry_id)  a Pokemon cannot go to two teams

The checks in `pick` exist to produce a good error message. The constraints
exist to be correct. Both are needed: a check alone loses the race, and a
constraint alone says "UNIQUE constraint failed".

Costs are snapshotted onto the pick. A commissioner who reprices the pool
mid-draft must not retroactively change what anyone has already spent.
"""

from __future__ import annotations

import json
import random
import sqlite3

import team_logo
from datetime import datetime, timedelta, timezone
from typing import Any

SETUP, LIVE, COMPLETE = "setup", "live", "complete"

DEFAULT_PICK_SECONDS = 120

# How many expired turns one call will work through. Nothing runs in the
# background — the clock is evaluated when someone asks about the draft — so a
# league that closes its laptops overnight comes back to a pile of elapsed
# turns. Catching up is right, but doing it unbounded would let one page load
# auto-resolve an entire draft, so it stops and reports how far it got.
MAX_CATCHUP = 50

# Past this many missed turns, the gap is an absence rather than a stall, and
# resolving it turn by turn would be wrong: a league that closes its laptops
# overnight would come back to a draft that auto-picked itself to completion.
# Beyond the threshold the clock is simply restarted, so everyone gets a fresh
# turn and nobody loses a pick to having gone to bed.
STALE_AFTER_TURNS = 10

# SQLite's datetime('now') is UTC to the second, and every timestamp compared
# here comes from it.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], TIME_FORMAT)
    except ValueError:
        return None


class DraftError(Exception):
    """Something the caller did. The message is safe to show them."""


class NotYourTurn(DraftError):
    """A pick arrived from a team that is not on the clock."""


class CannotAfford(DraftError):
    """The team does not have the points."""


# ----------------------------------------------------------- the order


def snake_team(team_ids: list[int], pick_no: int) -> int:
    """Which team picks at `pick_no` (0-based). The snake, in three lines."""
    count = len(team_ids)
    round_no, index = divmod(pick_no, count)
    # Odd rounds run backwards. That reversal is the entire difference between
    # a snake draft and a straight one.
    return team_ids[index] if round_no % 2 == 0 else team_ids[count - 1 - index]


def snake_order(team_ids: list[int], rounds: int) -> list[int]:
    """The full pick order, front to back. Ignores deferrals — see round_order."""
    return [snake_team(team_ids, n) for n in range(len(team_ids) * rounds)]


def base_round(team_ids: list[int], round_index: int) -> list[int]:
    """One round of the plain snake, before anyone has run out of time."""
    return list(team_ids) if round_index % 2 == 0 else list(reversed(team_ids))


def deferred_in(conn: sqlite3.Connection, season_id: int, round_no: int) -> list[int]:
    """Teams sent to the back of this round, oldest deferral first.

    Ordered by rowid as well as time because datetime('now') is only accurate
    to the second, and two teams timing out in the same second must still have
    a defined order behind each other.
    """
    return [
        row["team_id"]
        for row in conn.execute(
            "SELECT team_id FROM draft_defer WHERE season_id = ? AND round_no = ? "
            "ORDER BY deferred_at, rowid",
            (season_id, round_no),
        )
    ]


def round_order(
    conn: sqlite3.Connection, season_id: int, team_ids: list[int], round_index: int
) -> list[int]:
    """This round's real order: the snake, with timed-out teams moved to the back.

    The deferral lives for one round only. `base_round` is recomputed from the
    plain snake every round, so the order resets by construction rather than by
    anything having to undo it.
    """
    base = base_round(team_ids, round_index)
    deferred = deferred_in(conn, season_id, round_index + 1)
    if not deferred:
        return base
    late = set(deferred)
    return [team for team in base if team not in late] + [
        team for team in deferred if team in set(base)
    ]


def clock_team(
    conn: sqlite3.Connection, season_id: int, team_ids: list[int], picks_made: int
) -> tuple[int, int, int]:
    """(team on the clock, round index, position within the round)."""
    round_index, position = divmod(picks_made, len(team_ids))
    return round_order(conn, season_id, team_ids, round_index)[position], round_index, position


def ordered_teams(conn: sqlite3.Connection, season_id: int) -> list[sqlite3.Row]:
    """The teams that have a draft position, in that order.

    The snake reads its order from here, so a team with no position is not in
    the draft yet — which is exactly what makes `start` refuse rather than
    guess. For showing people who has joined, use `all_teams`.
    """
    return conn.execute(
        "SELECT * FROM team WHERE season_id = ? AND draft_position IS NOT NULL "
        "ORDER BY draft_position",
        (season_id,),
    ).fetchall()


def all_teams(conn: sqlite3.Connection, season_id: int) -> list[sqlite3.Row]:
    """Every team, positioned ones first and the rest behind them.

    Joining a season does not assign a draft position — the commissioner sets
    the order — so in a league that has just filled up every team has a NULL
    position. Filtering those out of the board meant the one screen a
    commissioner uses to set the order showed "no teams yet" while three
    people sat in the lobby.
    """
    return conn.execute(
        "SELECT * FROM team WHERE season_id = ? "
        "ORDER BY draft_position IS NULL, draft_position, id",
        (season_id,),
    ).fetchall()


# ------------------------------------------------------------- reading


def get_draft(conn: sqlite3.Connection, season_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM draft WHERE season_id = ?", (season_id,)
    ).fetchone()


def pick_count(conn: sqlite3.Connection, season_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM draft_pick WHERE season_id = ?", (season_id,)
    ).fetchone()["n"]


def spent_by_team(conn: sqlite3.Connection, season_id: int) -> dict[int, int]:
    """{team_id: points spent}. Derived from the picks, never stored."""
    return {
        row["team_id"]: row["spent"]
        for row in conn.execute(
            "SELECT team_id, SUM(cost_paid) AS spent FROM draft_pick "
            "WHERE season_id = ? GROUP BY team_id",
            (season_id,),
        )
    }


def _upcoming(
    conn: sqlite3.Connection,
    season_id: int,
    team_ids: list[int],
    made: int,
    rounds: int,
    limit: int,
) -> list[int]:
    """The next few teams to pick, honouring this round's deferrals."""
    if not team_ids:
        return []
    out: list[int] = []
    total = len(team_ids) * rounds
    for n in range(made, min(made + limit, total)):
        team, _, _ = clock_team(conn, season_id, team_ids, n)
        out.append(team)
    return out


def state(conn: sqlite3.Connection, season_id: int) -> dict[str, Any]:
    """Everything a client needs to render the clock."""
    season = conn.execute(
        "SELECT * FROM season WHERE id = ?", (season_id,)
    ).fetchone()
    if season is None:
        raise DraftError(f"No season with id {season_id}")

    draft = get_draft(conn, season_id)
    teams = ordered_teams(conn, season_id)
    team_ids = [team["id"] for team in teams]
    made = pick_count(conn, season_id)
    rounds = draft["rounds"] if draft else season["roster_size"]
    total = len(team_ids) * rounds

    on_clock = None
    if draft and draft["status"] == LIVE and team_ids and made < total:
        on_clock, _, _ = clock_team(conn, season_id, team_ids, made)

    deadline = deadline_of(draft) if draft else None
    round_no = (made // len(team_ids) + 1) if team_ids and made < total else None

    return {
        "season_id": season_id,
        "status": draft["status"] if draft else SETUP,
        # Teams still waiting for a draft position. Non-empty means `start`
        # will refuse, so the UI can say what is missing before anyone tries.
        "unordered_team_ids": [
            row["id"] for row in conn.execute(
                "SELECT id FROM team WHERE season_id = ? AND draft_position IS NULL "
                "ORDER BY id", (season_id,),
            )
        ],
        "rounds": rounds,
        "teams": len(team_ids),
        "picks_made": made,
        "picks_total": total,
        "round_no": round_no,
        "on_clock_team_id": on_clock,
        "pick_seconds": draft["pick_seconds"] if draft else None,
        "deadline": deadline.strftime(TIME_FORMAT) + "Z" if deadline else None,
        # Sent alongside the deadline so a client can count down without
        # trusting its own clock to agree with the server's.
        "seconds_left": (
            max(0, int((deadline - now_utc()).total_seconds())) if deadline else None
        ),
        "deferred_this_round": (
            deferred_in(conn, season_id, round_no) if round_no else []
        ),
        "order": [
            {"id": team["id"], "name": team["name"], "position": team["draft_position"]}
            for team in teams
        ],
        # Built from the live round order, not the plain snake, or the preview
        # would still show a timed-out team in the slot they lost.
        "upcoming": _upcoming(conn, season_id, team_ids, made, rounds, 8),
        "started_at": draft["started_at"] if draft else None,
        "completed_at": draft["completed_at"] if draft else None,
    }


def board(conn: sqlite3.Connection, season_id: int) -> dict[str, Any]:
    """Every team with every pick it has made. The public draft board.

    One query for the picks rather than one per team: a full board is a few
    dozen rows, and N+1 queries behind a view everyone refreshes is the kind
    of thing that is fine until the draft actually starts.
    """
    season = conn.execute(
        "SELECT * FROM season WHERE id = ?", (season_id,)
    ).fetchone()
    if season is None:
        raise DraftError(f"No season with id {season_id}")

    budget = season["budget"]
    picks: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(
        # LEFT JOIN, because a forfeited slot has no pool_entry. An inner join
        # drops those rows, which would hide the fact that a team lost a pick
        # and leave the board's pick count disagreeing with the draft's.
        "SELECT p.*, e.api_name, e.display_name, e.types, e.bst, e.sprite_url, e.tier "
        "FROM draft_pick p LEFT JOIN pool_entry e ON e.id = p.pool_entry_id "
        "WHERE p.season_id = ? ORDER BY p.pick_no",
        (season_id,),
    ):
        picks.setdefault(row["team_id"], []).append({
            "pick_no": row["pick_no"],
            "round_no": row["round_no"],
            "api_name": row["api_name"],
            # A forfeited slot still occupies a round, so it is shown as one
            # rather than omitted.
            "display_name": row["display_name"] or "— skipped —",
            "forfeited": row["pool_entry_id"] is None,
            # Stored as JSON text, because SQLite has no list type. Decoded
            # here so the board hands the client a list like every other route.
            "types": json.loads(row["types"] or "[]"),
            "bst": row["bst"],
            "sprite_url": row["sprite_url"],
            "tier": row["tier"],
            "cost_paid": row["cost_paid"],
            "picked_at": row["picked_at"],
        })

    teams = []
    logo_versions = team_logo.versions(conn, season_id)
    for team in all_teams(conn, season_id):
        mine = picks.get(team["id"], [])
        spent = sum(pick["cost_paid"] for pick in mine)
        teams.append({
            "id": team["id"],
            "name": team["name"],
            "owner": team["owner"],
            "logo_url": team["logo_url"],
            # One field the board can put straight into an <img src>: the
            # upload when there is one, the pasted link otherwise.
            "logo": team_logo.url_for(
                season_id, team["id"], logo_versions.get(team["id"]), team["logo_url"]
            ),
            "draft_position": team["draft_position"],
            "picks": mine,
            "spent": spent,
            "remaining": budget - spent,
            "roster_full": len(mine) >= season["roster_size"],
        })

    # `state` also has a "teams" key — its team *count* — so spreading it over
    # this dict replaced the list of teams with an integer. Merged explicitly
    # rather than with **, so a future key added to state cannot silently
    # shadow a field of the board again.
    summary = state(conn, season_id)
    summary.pop("teams", None)
    return {
        "budget": budget,
        "roster_size": season["roster_size"],
        "team_count": len(teams),
        "teams": teams,
        **summary,
    }


# --------------------------------------------------------------- clock


def deadline_of(draft: sqlite3.Row | None) -> datetime | None:
    """When the team on the clock runs out of time, or None if untimed."""
    if draft is None or not draft["pick_seconds"]:
        return None
    started = parse_time(draft["clock_started_at"])
    return started + timedelta(seconds=draft["pick_seconds"]) if started else None


def _record(
    conn: sqlite3.Connection,
    season_id: int,
    team_id: int,
    pick_no: int,
    round_no: int,
    entry: sqlite3.Row | None,
    total: int,
    *,
    clock_from: datetime | None = None,
) -> None:
    """Write one pick — or one forfeited slot, when `entry` is None.

    Every way a slot gets filled comes through here: a player picking, an
    autopick, a forfeit. That is deliberate — completing the draft and
    restarting the clock have to happen identically however the slot was
    filled, and three copies of that is three chances to forget one.
    """
    conn.execute(
        "INSERT INTO draft_pick (season_id, pick_no, round_no, team_id, "
        "  pool_entry_id, cost_paid) VALUES (?, ?, ?, ?, ?, ?)",
        (season_id, pick_no, round_no, team_id,
         entry["id"] if entry else None, entry["cost"] if entry else 0),
    )

    if pick_no + 1 >= total:
        conn.execute(
            "UPDATE draft SET status = ?, completed_at = datetime('now'), "
            "  clock_started_at = NULL WHERE season_id = ?",
            (COMPLETE, season_id),
        )
        return

    # Catching up on several expired turns advances the clock by exactly one
    # interval each time rather than to "now", so a league that was away for an
    # hour resolves the turns that actually elapsed instead of collapsing them
    # into one.
    conn.execute(
        "UPDATE draft SET clock_started_at = ? WHERE season_id = ?",
        ((clock_from or now_utc()).strftime(TIME_FORMAT), season_id),
    )


def _available(
    conn: sqlite3.Connection, season_id: int, max_cost: int
) -> sqlite3.Row | None:
    """The best Pokemon a team could still take at this budget."""
    return conn.execute(
        "SELECT * FROM pool_entry WHERE season_id = ? AND banned = 0 AND cost <= ? "
        "  AND id NOT IN ("
        "    SELECT pool_entry_id FROM draft_pick "
        "    WHERE season_id = ? AND pool_entry_id IS NOT NULL) "
        "ORDER BY cost DESC, bst DESC, display_name LIMIT 1",
        (season_id, max_cost, season_id),
    ).fetchone()


def enforce_clock(conn: sqlite3.Connection, season_id: int) -> list[dict[str, Any]]:
    """Apply any pick timers that have run out. Returns what it did.

    Nothing schedules this — there is no background worker — so it runs at the
    top of every read and every pick. The consequence is worth being plain
    about: a timer expires when somebody next looks at the draft, not on the
    exact second. For a draft where everyone is watching the board that is
    indistinguishable; for one nobody has open, the turns resolve in a burst
    when the first person returns.

    The rule, in order:

      * out of time, and not yet deferred this round, and somebody is left to
        go behind -> sent to the back of the round.
      * out of time with nowhere further back -> autopick the best Pokemon the
        team can still afford.
      * out of time, at the back, and cannot afford anything at all -> the slot
        is forfeited, because the draft cannot wait on a team with no legal
        move.
    """
    events: list[dict[str, Any]] = []
    for _ in range(MAX_CATCHUP):
        draft = get_draft(conn, season_id)
        if draft is None or draft["status"] != LIVE:
            break
        deadline = deadline_of(draft)
        if deadline is None or now_utc() < deadline:
            break

        overdue = (now_utc() - deadline).total_seconds() / draft["pick_seconds"]
        if overdue > STALE_AFTER_TURNS:
            conn.execute(
                "UPDATE draft SET clock_started_at = datetime('now') WHERE season_id = ?",
                (season_id,),
            )
            events.append({
                "type": "clock_reset",
                "detail": (
                    f"The draft sat idle for about {int(overdue)} turns, so the "
                    "clock was restarted rather than auto-picking through them."
                ),
            })
            break

        teams = ordered_teams(conn, season_id)
        team_ids = [team["id"] for team in teams]
        if not team_ids:
            break
        made = pick_count(conn, season_id)
        total = len(team_ids) * draft["rounds"]
        if made >= total:
            break

        team_id, round_index, position = clock_team(conn, season_id, team_ids, made)
        round_no = round_index + 1
        name = next((t["name"] for t in teams if t["id"] == team_id), str(team_id))
        already = team_id in deferred_in(conn, season_id, round_no)
        last_in_round = position == len(team_ids) - 1

        if not already and not last_in_round:
            conn.execute(
                "INSERT OR IGNORE INTO draft_defer (season_id, round_no, team_id) "
                "VALUES (?, ?, ?)",
                (season_id, round_no, team_id),
            )
            conn.execute(
                "UPDATE draft SET clock_started_at = ? WHERE season_id = ?",
                (deadline.strftime(TIME_FORMAT), season_id),
            )
            events.append({
                "type": "deferred", "team_id": team_id, "team": name,
                "round_no": round_no,
                "detail": f"{name} ran out of time and picks last this round.",
            })
            continue

        season = conn.execute(
            "SELECT budget, roster_size FROM season WHERE id = ?", (season_id,)
        ).fetchone()
        spent = conn.execute(
            "SELECT COALESCE(SUM(cost_paid), 0) AS spent FROM draft_pick "
            "WHERE season_id = ? AND team_id = ?",
            (season_id, team_id),
        ).fetchone()["spent"]
        entry = _available(conn, season_id, season["budget"] - spent)

        _record(conn, season_id, team_id, made, round_no, entry, total,
                clock_from=deadline)
        events.append({
            "type": "autopick" if entry else "forfeit",
            "team_id": team_id, "team": name, "round_no": round_no,
            "api_name": entry["api_name"] if entry else None,
            "display_name": entry["display_name"] if entry else None,
            "cost_paid": entry["cost"] if entry else 0,
            "detail": (
                f"{name} ran out of time; {entry['display_name']} was picked "
                f"automatically."
                if entry else
                f"{name} ran out of time and could afford nothing left, so the "
                "pick was skipped."
            ),
        })
    return events


# ------------------------------------------------------------- writing


def start(
    conn: sqlite3.Connection,
    season_id: int,
    *,
    randomize: bool = False,
    pick_seconds: int = DEFAULT_PICK_SECONDS,
) -> dict[str, Any]:
    """Open the draft. Refuses rather than guessing at a missing order."""
    season = conn.execute(
        "SELECT * FROM season WHERE id = ?", (season_id,)
    ).fetchone()
    if season is None:
        raise DraftError(f"No season with id {season_id}")

    existing = get_draft(conn, season_id)
    if existing and existing["status"] == LIVE:
        raise DraftError("This draft has already started.")
    if existing and existing["status"] == COMPLETE:
        raise DraftError("This draft is finished. Reset it before starting again.")

    all_teams = conn.execute(
        "SELECT * FROM team WHERE season_id = ? ORDER BY id", (season_id,)
    ).fetchall()
    if len(all_teams) < 2:
        raise DraftError("A draft needs at least two teams.")

    if randomize or any(team["draft_position"] is None for team in all_teams):
        if not randomize:
            missing = [t["name"] for t in all_teams if t["draft_position"] is None]
            raise DraftError(
                "These teams have no draft position: " + ", ".join(missing)
                + ". Set the order first, or start with randomize=true."
            )
        shuffled = list(all_teams)
        random.shuffle(shuffled)
        # Clear first: draft_position is UNIQUE per season, so assigning into
        # occupied slots collides partway through.
        conn.execute(
            "UPDATE team SET draft_position = NULL WHERE season_id = ?", (season_id,)
        )
        for position, team in enumerate(shuffled, start=1):
            conn.execute(
                "UPDATE team SET draft_position = ? WHERE id = ?", (position, team["id"])
            )

    if conn.execute(
        "SELECT COUNT(*) AS n FROM pool_entry WHERE season_id = ? AND banned = 0",
        (season_id,),
    ).fetchone()["n"] == 0:
        raise DraftError("The pool is empty. Build it from a format first.")

    # A fresh start clears any deferral left over from a previous run, or the
    # first round would begin with someone already sent to the back.
    conn.execute("DELETE FROM draft_defer WHERE season_id = ?", (season_id,))
    conn.execute(
        "INSERT INTO draft (season_id, status, rounds, pick_seconds, "
        "  clock_started_at, started_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now')) "
        "ON CONFLICT(season_id) DO UPDATE SET "
        "  status = excluded.status, rounds = excluded.rounds, "
        "  pick_seconds = excluded.pick_seconds, "
        "  clock_started_at = excluded.clock_started_at, "
        "  started_at = excluded.started_at, completed_at = NULL",
        (season_id, LIVE, season["roster_size"], max(0, pick_seconds)),
    )
    return state(conn, season_id)


def pick(
    conn: sqlite3.Connection,
    season_id: int,
    team_id: int,
    name: str,
) -> dict[str, Any]:
    """Draft one Pokemon for one team, or explain why not.

    Every check here is also a rule of the league, so each one names what it
    found rather than returning a bare refusal — "that costs 19 and you have
    12" is actionable where "cannot afford" is not.
    """
    season = conn.execute(
        "SELECT * FROM season WHERE id = ?", (season_id,)
    ).fetchone()
    if season is None:
        raise DraftError(f"No season with id {season_id}")

    draft = get_draft(conn, season_id)
    if draft is None or draft["status"] == SETUP:
        raise DraftError("This draft has not started yet.")
    if draft["status"] == COMPLETE:
        raise DraftError("This draft is finished.")

    teams = ordered_teams(conn, season_id)
    team_ids = [team["id"] for team in teams]
    made = pick_count(conn, season_id)
    total = len(team_ids) * draft["rounds"]
    if made >= total:
        raise DraftError("Every pick has been made.")

    on_clock, round_index, _ = clock_team(conn, season_id, team_ids, made)
    if team_id != on_clock:
        whose = next((t["name"] for t in teams if t["id"] == on_clock), "another team")
        raise NotYourTurn(f"It is {whose}'s pick.")

    entry = conn.execute(
        "SELECT * FROM pool_entry WHERE season_id = ? AND (api_name = ? OR showdown_id = ?)",
        (season_id, name, name.lower().replace(" ", "").replace("-", "")),
    ).fetchone()
    if entry is None:
        raise DraftError(f"{name!r} is not in this season's pool.")
    if entry["banned"]:
        raise DraftError(f"{entry['display_name']} is banned in this season.")

    taken = conn.execute(
        "SELECT t.name FROM draft_pick p JOIN team t ON t.id = p.team_id "
        "WHERE p.season_id = ? AND p.pool_entry_id = ?",
        (season_id, entry["id"]),
    ).fetchone()
    if taken is not None:
        raise DraftError(f"{entry['display_name']} was already drafted by {taken['name']}.")

    mine = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_paid), 0) AS spent "
        "FROM draft_pick WHERE season_id = ? AND team_id = ?",
        (season_id, team_id),
    ).fetchone()
    if mine["n"] >= season["roster_size"]:
        raise DraftError(f"Your roster is full at {season['roster_size']} Pokémon.")

    remaining = season["budget"] - mine["spent"]
    cost = entry["cost"]
    if cost > remaining:
        raise CannotAfford(
            f"{entry['display_name']} costs {cost} and you have {remaining} "
            f"point{'s' if remaining != 1 else ''} left."
        )

    _record(conn, season_id, team_id, made, round_index + 1, entry, total)

    after = state(conn, season_id)
    return {
        "pick_no": made,
        # This pick's own round. Deliberately not taken from `after`, whose
        # round_no is the *next* pick's — merging the two overwrote the round a
        # pick was made in with the round that follows it, and reported None
        # for the pick that ended the draft.
        "round_no": round_index + 1,
        "team_id": team_id,
        "api_name": entry["api_name"],
        "display_name": entry["display_name"],
        "cost_paid": cost,
        "remaining": remaining - cost,
        "status": after["status"],
        "on_clock_team_id": after["on_clock_team_id"],
        "next_round_no": after["round_no"],
        "picks_made": after["picks_made"],
        "picks_total": after["picks_total"],
    }


def undo(conn: sqlite3.Connection, season_id: int) -> dict[str, Any]:
    """Take back the most recent pick. The commissioner's eraser.

    Only the last one, which keeps pick_no contiguous — the snake reads the
    team on the clock straight off the pick count, so a hole anywhere in that
    sequence would hand the clock to the wrong team for the rest of the draft.
    """
    last = conn.execute(
        "SELECT * FROM draft_pick WHERE season_id = ? ORDER BY pick_no DESC LIMIT 1",
        (season_id,),
    ).fetchone()
    if last is None:
        raise DraftError("No picks to undo.")

    conn.execute("DELETE FROM draft_pick WHERE id = ?", (last["id"],))
    # An undo after the final pick reopens the draft, or the board stays
    # frozen on "complete" with a slot empty. The clock restarts from now: the
    # team getting its turn back should get a full turn, not the remains of
    # the one that expired.
    conn.execute(
        "UPDATE draft SET status = ?, completed_at = NULL, "
        "  clock_started_at = datetime('now') WHERE season_id = ?",
        (LIVE, season_id),
    )
    return state(conn, season_id)
