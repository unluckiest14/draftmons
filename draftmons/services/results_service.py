"""Match results, the standings, and the elimination leaderboard.

The draft decides who owns what; this decides who is winning. Two views come
out of the same two tables:

  standings()     every team, its record, and what it did in each week
  top_pokemon()   the Pokemon with the most eliminations in the season

Both are read far more often than they are written — a league checks the table
after every match night and records a handful of results a week — so the write
side does the work. `record` refuses anything the leaderboards would otherwise
report as fact: a team playing itself, a winner who was not in the match, a
Pokemon credited to a team that does not own it. A standings table nobody
trusts is worse than no standings table, and the only place to keep it
trustworthy is at the moment a result goes in.

Every refusal names what it found. The caller is a commissioner typing a score
into a form, and "Great Tusk was drafted by Bravo, not Alpha" tells them what
to fix where "invalid team" does not.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from draftmons.services import pool_service

# A result the standings show, and what it is worth in the "record" column.
WIN, LOSS, DRAW = "W", "L", "D"


class ResultError(Exception):
    """Something the caller did. The message is safe to show them."""


class ResultNotFound(ResultError):
    """No such season or match."""


# --------------------------------------------------------------- writing


def _season(conn: sqlite3.Connection, season_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    if row is None:
        raise ResultNotFound(f"No season with id {season_id}")
    return row


def _teams(conn: sqlite3.Connection, season_id: int) -> dict[int, sqlite3.Row]:
    return {
        row["id"]: row
        for row in conn.execute(
            "SELECT * FROM team WHERE season_id = ? "
            "ORDER BY draft_position IS NULL, draft_position, id",
            (season_id,),
        )
    }


def record(
    conn: sqlite3.Connection,
    season_id: int,
    *,
    week_no: int,
    home_team_id: int,
    away_team_id: int,
    winner_team_id: int | None = None,
    home_score: int = 0,
    away_score: int = 0,
    replay_url: str | None = None,
    elims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record one match and its stat line, or explain why it cannot be.

    The database enforces the same shape rules through CHECK constraints, and
    on purpose: these checks exist for the error message, those exist because
    a result inserted by a script or by hand has to be as sound as one that
    came through here.
    """
    _season(conn, season_id)
    teams = _teams(conn, season_id)

    for label, team_id in (("home", home_team_id), ("away", away_team_id)):
        if team_id not in teams:
            raise ResultNotFound(f"No {label} team {team_id} in season {season_id}.")
    if home_team_id == away_team_id:
        raise ResultError(f"{teams[home_team_id]['name']} cannot play itself.")
    if winner_team_id is not None and winner_team_id not in (home_team_id, away_team_id):
        raise ResultError(
            "The winner has to be one of the two teams in the match: "
            f"{teams[home_team_id]['name']} or {teams[away_team_id]['name']}."
        )

    lines = _resolve_elims(conn, season_id, teams, home_team_id, away_team_id, elims or [])

    try:
        cursor = conn.execute(
            "INSERT INTO match_result (season_id, week_no, home_team_id, away_team_id, "
            "                          winner_team_id, home_score, away_score, replay_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (season_id, week_no, home_team_id, away_team_id,
             winner_team_id, home_score, away_score, replay_url),
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE (season_id, week_no, home_team_id, away_team_id). Translate,
        # rather than leak a constraint name at someone fixing a typo.
        raise ResultError(
            f"Week {week_no} already has {teams[home_team_id]['name']} vs "
            f"{teams[away_team_id]['name']}. Delete that result first if you are "
            "correcting it."
        ) from exc

    match_id = cursor.lastrowid
    for team_id, entry, count in lines:
        conn.execute(
            "INSERT INTO match_elim (match_id, team_id, pool_entry_id, elims) "
            "VALUES (?, ?, ?, ?)",
            (match_id, team_id, entry["id"], count),
        )
    return get_match(conn, season_id, match_id)


def _resolve_elims(
    conn: sqlite3.Connection,
    season_id: int,
    teams: dict[int, sqlite3.Row],
    home_team_id: int,
    away_team_id: int,
    elims: list[dict[str, Any]],
) -> list[tuple[int, sqlite3.Row, int]]:
    """Turn submitted stat lines into (team_id, pool_entry, elims) triples.

    Resolved before the match row is inserted, so a mistyped Pokemon name
    fails the whole submission rather than leaving a result on the board with
    half a stat line under it.
    """
    resolved: list[tuple[int, sqlite3.Row, int]] = []
    seen: set[int] = set()

    for line in elims:
        team_id = line["team_id"]
        name = str(line["name"]).strip()
        count = int(line.get("elims", 0))

        if team_id not in (home_team_id, away_team_id):
            raise ResultError(
                f"{name or 'That Pokémon'} is credited to a team that did not play "
                "in this match."
            )
        entry = conn.execute(
            "SELECT * FROM pool_entry WHERE season_id = ? AND (api_name = ? OR showdown_id = ?)",
            (season_id, name, pool_service.showdown_id(name)),
        ).fetchone()
        if entry is None:
            raise ResultNotFound(f"{name!r} is not in this season's pool.")
        if entry["id"] in seen:
            raise ResultError(
                f"{entry['display_name']} is listed twice. Add its eliminations up "
                "into one line."
            )
        seen.add(entry["id"])

        # A drafted Pokemon belongs to exactly one team, so crediting its KOs
        # to another is a typo that the leaderboard would then attribute to
        # the wrong roster for the rest of the season. Undrafted mons are left
        # alone: a free agent still has to score for somebody.
        owner = conn.execute(
            "SELECT team_id FROM draft_pick WHERE season_id = ? AND pool_entry_id = ?",
            (season_id, entry["id"]),
        ).fetchone()
        if owner is not None and owner["team_id"] != team_id:
            drafted_by = teams.get(owner["team_id"])
            raise ResultError(
                f"{entry['display_name']} was drafted by "
                f"{drafted_by['name'] if drafted_by else 'another team'}, not "
                f"{teams[team_id]['name']}."
            )

        # Zero is not an error — a form sends a line per Pokemon that played —
        # but it is not worth a row either.
        if count:
            resolved.append((team_id, entry, count))
    return resolved


def delete_match(conn: sqlite3.Connection, season_id: int, match_id: int) -> None:
    """Remove a result and its stat line. The way a mistake is corrected.

    The elim rows go with it through ON DELETE CASCADE, which is why deleting
    and re-recording is the fix rather than editing a row in place.
    """
    found = conn.execute(
        "SELECT 1 FROM match_result WHERE id = ? AND season_id = ?", (match_id, season_id)
    ).fetchone()
    if found is None:
        raise ResultNotFound(f"No match {match_id} in season {season_id}.")
    conn.execute("DELETE FROM match_result WHERE id = ?", (match_id,))


# --------------------------------------------------------------- reading


def _elims_by_match(
    conn: sqlite3.Connection, season_id: int
) -> dict[int, list[dict[str, Any]]]:
    """Every stat line in the season, grouped by match. One query, not N."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT x.match_id, x.team_id, x.elims, e.api_name, e.display_name, e.sprite_url "
        "FROM match_elim x "
        "JOIN match_result m ON m.id = x.match_id "
        "JOIN pool_entry e ON e.id = x.pool_entry_id "
        "WHERE m.season_id = ? ORDER BY x.elims DESC, e.display_name",
        (season_id,),
    ):
        grouped.setdefault(row["match_id"], []).append({
            "team_id": row["team_id"],
            "api_name": row["api_name"],
            "display_name": row["display_name"],
            "sprite_url": row["sprite_url"],
            "elims": row["elims"],
        })
    return grouped


def list_matches(
    conn: sqlite3.Connection, season_id: int, week_no: int | None = None
) -> list[dict[str, Any]]:
    """Every result, newest week first. Public; this is the league's history."""
    _season(conn, season_id)
    lines = _elims_by_match(conn, season_id)
    sql = (
        "SELECT m.*, h.name AS home_name, a.name AS away_name, w.name AS winner_name "
        "FROM match_result m "
        "JOIN team h ON h.id = m.home_team_id "
        "JOIN team a ON a.id = m.away_team_id "
        "LEFT JOIN team w ON w.id = m.winner_team_id "
        "WHERE m.season_id = ?"
    )
    args: list[Any] = [season_id]
    if week_no is not None:
        sql += " AND m.week_no = ?"
        args.append(week_no)
    sql += " ORDER BY m.week_no DESC, m.id DESC"

    return [
        {
            "id": row["id"],
            "week_no": row["week_no"],
            "home": {"id": row["home_team_id"], "name": row["home_name"],
                     "score": row["home_score"]},
            "away": {"id": row["away_team_id"], "name": row["away_name"],
                     "score": row["away_score"]},
            "winner_team_id": row["winner_team_id"],
            "winner_name": row["winner_name"],
            "replay_url": row["replay_url"],
            "recorded_at": row["recorded_at"],
            "elims": lines.get(row["id"], []),
        }
        for row in conn.execute(sql, args)
    ]


def get_match(conn: sqlite3.Connection, season_id: int, match_id: int) -> dict[str, Any]:
    for match in list_matches(conn, season_id):
        if match["id"] == match_id:
            return match
    raise ResultNotFound(f"No match {match_id} in season {season_id}.")


def standings(conn: sqlite3.Connection, season_id: int) -> dict[str, Any]:
    """Every team's record, and what it did week by week.

    `weeks` is the column order and `by_week` lines up with it, one cell per
    week per team. A cell is a list because a week can hold two matches for
    the same team — a make-up game, a double header — and dropping one to keep
    the shape simple would leave the row's W-L not adding up to its record.

    Teams with no matches yet are still listed, at 0-0. A league that has
    played one week still wants to see everybody on the table.
    """
    season = _season(conn, season_id)
    teams = _teams(conn, season_id)

    records: dict[int, dict[str, Any]] = {
        team_id: {
            "team_id": team_id,
            "name": team["name"],
            "owner": team["owner"],
            "logo_url": team["logo_url"],
            "wins": 0, "losses": 0, "draws": 0, "played": 0,
            # Pokemon left standing, for and against. The tiebreak most draft
            # leagues use, and the one number a score column is good for.
            "score_for": 0, "score_against": 0,
            "by_week": {},
        }
        for team_id, team in teams.items()
    }

    weeks: set[int] = set()
    matches = list_matches(conn, season_id)
    for match in matches:
        weeks.add(match["week_no"])
        for side, other in (("home", "away"), ("away", "home")):
            mine, theirs = match[side], match[other]
            record_row = records.get(mine["id"])
            if record_row is None:
                continue   # a team deleted since; its matches went with it
            if match["winner_team_id"] is None:
                outcome = DRAW
                record_row["draws"] += 1
            elif match["winner_team_id"] == mine["id"]:
                outcome = WIN
                record_row["wins"] += 1
            else:
                outcome = LOSS
                record_row["losses"] += 1
            record_row["played"] += 1
            record_row["score_for"] += mine["score"]
            record_row["score_against"] += theirs["score"]
            record_row["by_week"].setdefault(match["week_no"], []).append({
                "match_id": match["id"],
                "result": outcome,
                "opponent_id": theirs["id"],
                "opponent": theirs["name"],
                "score": f"{mine['score']}-{theirs['score']}",
            })

    order = sorted(weeks)
    table = []
    for row in records.values():
        row["differential"] = row["score_for"] - row["score_against"]
        # Flattened against the week order so the frontend renders a row by
        # walking one list, instead of looking every week up by a JSON key
        # that has become a string on the way over.
        row["by_week"] = [row["by_week"].get(week, []) for week in order]
        table.append(row)

    # Wins first, then fewest losses — which separates a 3-0 team from a 3-2
    # one when both have played a different number of matches — then the score
    # differential, then the name so the order never wobbles between reads.
    table.sort(key=lambda row: (-row["wins"], row["losses"], -row["differential"], row["name"]))
    for position, row in enumerate(table, start=1):
        row["rank"] = position

    return {
        "season_id": season_id,
        "season_name": season["name"],
        "weeks": order,
        "matches_played": len(matches),
        "teams": table,
    }


def top_pokemon(
    conn: sqlite3.Connection, season_id: int, limit: int = 5
) -> list[dict[str, Any]]:
    """The season's leaders by total eliminations.

    Grouped by Pokemon rather than by (Pokemon, team): a pool entry belongs to
    one season and `record` refuses to credit a drafted mon to anyone but the
    team that drafted it, so the team here is single-valued by construction.

    `matches` comes along with the total because five KOs over one match and
    five over five are not the same performance, and a leaderboard that shows
    only the total invites exactly that comparison.
    """
    _season(conn, season_id)
    rows = conn.execute(
        "SELECT e.id, e.api_name, e.display_name, e.sprite_url, e.types, e.cost, e.tier, "
        "       SUM(x.elims)              AS elims, "
        "       COUNT(DISTINCT x.match_id) AS matches, "
        "       COALESCE(dt.id, xt.id)     AS team_id, "
        "       COALESCE(dt.name, xt.name) AS team_name "
        "FROM match_elim x "
        "JOIN match_result m ON m.id = x.match_id "
        "JOIN pool_entry e ON e.id = x.pool_entry_id "
        # The drafting team is the season's own answer to "whose is this";
        # the elim row's team is the fallback for a mon nobody drafted.
        "LEFT JOIN draft_pick dp ON dp.season_id = m.season_id AND dp.pool_entry_id = e.id "
        "LEFT JOIN team dt ON dt.id = dp.team_id "
        "LEFT JOIN team xt ON xt.id = x.team_id "
        "WHERE m.season_id = ? "
        "GROUP BY e.id "
        "HAVING SUM(x.elims) > 0 "
        # Fewest matches breaks a tie: the same total in less time is the
        # better line, and without it two tied mons swap places between reads.
        "ORDER BY elims DESC, matches ASC, e.display_name ASC "
        "LIMIT ?",
        (season_id, limit),
    ).fetchall()

    return [
        {
            "rank": position,
            "pool_entry_id": row["id"],
            "api_name": row["api_name"],
            "display_name": row["display_name"],
            "sprite_url": row["sprite_url"],
            "types": json.loads(row["types"] or "[]"),
            "cost": row["cost"],
            "tier": row["tier"],
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "elims": row["elims"],
            "matches": row["matches"],
        }
        for position, row in enumerate(rows, start=1)
    ]
