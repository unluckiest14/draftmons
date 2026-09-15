"""Leagues: the list of them, and renaming or deleting one.

A season *is* a league here — one pool, one set of teams, one draft — so
running a second league means creating a second season. That has always
worked; what has not is finding them again. There was no list route, so the
board probed ids 1 to 30 on every load and anything past that was invisible.

`summary` is the fix and the shape of the whole feature: one row per league
with enough on it to tell them apart at a glance — how big the pool is, how
many teams joined, whether the draft has run — because a list of names alone
does not answer "which one was the Sunday league".

Who may rename or delete one follows the invite. A league whose invite has
been claimed is somebody's, and only that token may change it. A league with
no invite has no owner: anyone can claim it by opening it, so refusing to let
them rename it would be theatre.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from draftmons.services import player_service as players


class SeasonError(Exception):
    """Something the caller did. The message is safe to show them."""


class SeasonNotFound(SeasonError):
    """No such league."""


def get(conn: sqlite3.Connection, season_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    if row is None:
        raise SeasonNotFound(f"No season with id {season_id}")
    return row


# ------------------------------------------------------------- the list


def summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every league, newest first, each with the counts that identify it.

    The counts are correlated subqueries rather than a pile of GROUP BY joins:
    a league has several one-to-many children and joining them all in one
    statement multiplies the rows, which is how a pool of 771 turns into a
    team count of 771. There are tens of leagues at most, so the cost of
    doing it the readable way is nothing.
    """
    rows = conn.execute(
        "SELECT s.*, "
        "  (SELECT COUNT(*) FROM team t WHERE t.season_id = s.id)        AS teams, "
        "  (SELECT COUNT(*) FROM pool_entry p WHERE p.season_id = s.id)  AS pool_size, "
        "  (SELECT COUNT(*) FROM pool_entry p "
        "     WHERE p.season_id = s.id AND p.cost > 0)                   AS priced, "
        "  (SELECT COUNT(*) FROM draft_pick k WHERE k.season_id = s.id)  AS picks_made, "
        "  (SELECT COUNT(*) FROM match_result m WHERE m.season_id = s.id) AS matches_played, "
        "  d.status AS draft_status, d.rounds AS rounds, "
        "  i.is_open AS is_open, i.season_id AS claimed "
        "FROM season s "
        "LEFT JOIN draft d ON d.season_id = s.id "
        "LEFT JOIN season_invite i ON i.season_id = s.id "
        "ORDER BY s.created_at DESC, s.id DESC"
    ).fetchall()

    leagues = []
    for row in rows:
        # No draft row means the draft has not been set up, which is the same
        # state a draft row in 'setup' is in. Collapsing them here means the
        # frontend has one lifecycle to render rather than a status and a
        # separate "does it exist" flag.
        status = row["draft_status"] or "setup"
        # Rounds are copied onto the draft when it starts, so before that the
        # roster size is what the draft will be.
        rounds = row["rounds"] or row["roster_size"]
        leagues.append({
            "id": row["id"],
            "name": row["name"],
            "budget": row["budget"],
            "roster_size": row["roster_size"],
            "format_key": row["format_key"],
            "pool_label": row["pool_label"],
            "created_at": row["created_at"],
            "teams": row["teams"],
            "pool_size": row["pool_size"],
            "priced": row["priced"],
            "draft_status": status,
            "picks_made": row["picks_made"],
            "picks_total": row["teams"] * rounds,
            "matches_played": row["matches_played"],
            # Whether anyone has claimed the league, and whether the door is
            # open. A league nobody has claimed can still be renamed by
            # whoever finds it; see the module docstring.
            "claimed": row["claimed"] is not None,
            "is_open": bool(row["is_open"]) if row["is_open"] is not None else False,
        })
    return leagues


# ---------------------------------------------------------- one of them


def _require_owner(conn: sqlite3.Connection, season_id: int, admin_token: str | None) -> None:
    """Let this through if the league is unclaimed, or the token owns it."""
    if players.get_invite(conn, season_id) is None:
        return
    players.require_admin(conn, season_id, admin_token)


def update(
    conn: sqlite3.Connection,
    season_id: int,
    fields: dict[str, Any],
    admin_token: str | None = None,
) -> sqlite3.Row:
    """Rename a league, or change what it drafts for.

    Renaming is always allowed — it is a label. The budget and the roster size
    are not, once the draft has started: they are the rules everyone drafted
    under, and changing them halfway means the points already spent were spent
    against a different game.
    """
    get(conn, season_id)
    _require_owner(conn, season_id, admin_token)

    allowed = {"name", "budget", "roster_size"}
    changes = {key: value for key, value in fields.items() if key in allowed}
    if not changes:
        raise SeasonError(f"Nothing to update. Send one of: {', '.join(sorted(allowed))}.")

    locked = changes.keys() & {"budget", "roster_size"}
    if locked:
        draft = conn.execute(
            "SELECT status FROM draft WHERE season_id = ?", (season_id,)
        ).fetchone()
        if draft is not None and draft["status"] != "setup":
            raise SeasonError(
                f"The draft has already started, so {' and '.join(sorted(locked))} "
                "cannot change. Rename it if you like."
            )

    assignments = ", ".join(f"{column} = ?" for column in changes)
    conn.execute(
        f"UPDATE season SET {assignments} WHERE id = ?", (*changes.values(), season_id)
    )
    return get(conn, season_id)


def delete(
    conn: sqlite3.Connection, season_id: int, admin_token: str | None = None
) -> dict[str, Any]:
    """Delete a league and everything in it. Returns what went with it.

    Everything under a season cascades — pool, teams, players and their plans,
    the draft, the results — so this is one DELETE. The counts are read first
    and handed back because the caller is a confirmation dialog, and "this
    deletes 771 Pokémon, 8 teams and a finished draft" is the only version of
    that question worth asking.
    """
    get(conn, season_id)
    _require_owner(conn, season_id, admin_token)

    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM team WHERE season_id = ?)         AS teams, "
        "       (SELECT COUNT(*) FROM pool_entry WHERE season_id = ?)   AS pool_size, "
        "       (SELECT COUNT(*) FROM draft_pick WHERE season_id = ?)   AS picks, "
        "       (SELECT COUNT(*) FROM match_result WHERE season_id = ?) AS matches",
        (season_id, season_id, season_id, season_id),
    ).fetchone()

    conn.execute("DELETE FROM season WHERE id = ?", (season_id,))
    return {
        "deleted": season_id,
        "teams": counts["teams"],
        "pool_size": counts["pool_size"],
        "picks": counts["picks"],
        "matches": counts["matches"],
    }
