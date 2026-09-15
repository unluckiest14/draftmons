"""Teams: create, read, update, delete.

A team is a drafter's identity in a season — their name and their logo. No
account and no password, per the design doc: account friction is what drove
people off the other tools.

Routes stay thin by keeping the two things that need care here rather than in
main.py. First, the database's UNIQUE constraints raise IntegrityError with a
message written for a DBA; callers want "A team called 'Sinnoh Slammers'
already exists in this season". Second, a PATCH must update only the fields the
client actually sent, which means building the UPDATE from
model_dump(exclude_unset=True) rather than from every field on the model.
"""

from __future__ import annotations

import sqlite3
from typing import Any

MAX_TEAMS = 10   # "~10 people max per draft", from the requirements


class TeamError(Exception):
    """Something the caller did. The message is safe to show them."""


class TeamNotFound(TeamError):
    """No such team in this season."""


def _season_exists(conn: sqlite3.Connection, season_id: int) -> bool:
    return conn.execute("SELECT 1 FROM season WHERE id = ?", (season_id,)).fetchone() is not None


def create(
    conn: sqlite3.Connection,
    season_id: int,
    name: str,
    owner: str | None = None,
    logo_url: str | None = None,
) -> sqlite3.Row:
    if not _season_exists(conn, season_id):
        raise TeamNotFound(f"No season with id {season_id}")

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM team WHERE season_id = ?", (season_id,)
    ).fetchone()["n"]
    if count >= MAX_TEAMS:
        raise TeamError(f"A draft holds {MAX_TEAMS} teams and this season is full.")

    try:
        cursor = conn.execute(
            "INSERT INTO team (season_id, name, owner, logo_url) VALUES (?, ?, ?, ?)",
            (season_id, name, owner, logo_url),
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE (season_id, name). Translate rather than leak the constraint.
        raise TeamError(f"A team called {name!r} already exists in this season.") from exc
    return get(conn, season_id, cursor.lastrowid)


def get(conn: sqlite3.Connection, season_id: int, team_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM team WHERE id = ? AND season_id = ?", (team_id, season_id)
    ).fetchone()
    if row is None:
        raise TeamNotFound(f"No team {team_id} in season {season_id}")
    return row


def list_teams(conn: sqlite3.Connection, season_id: int) -> list[sqlite3.Row]:
    """Draft order first, then creation order for teams without a position."""
    return conn.execute(
        "SELECT * FROM team WHERE season_id = ? "
        "ORDER BY draft_position IS NULL, draft_position, id",
        (season_id,),
    ).fetchall()


def update(
    conn: sqlite3.Connection, season_id: int, team_id: int, fields: dict[str, Any]
) -> sqlite3.Row:
    """Partial update. `fields` should already be exclude_unset from the model.

    An empty dict is an error, not a no-op: a PATCH with nothing in it means
    the client sent a body it thought was meaningful and it wasn't.
    """
    get(conn, season_id, team_id)   # raises TeamNotFound

    allowed = {"name", "owner", "logo_url", "draft_position"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        raise TeamError(f"Nothing to update. Send one of: {', '.join(sorted(allowed))}.")

    assignments = ", ".join(f"{column} = ?" for column in changes)
    try:
        conn.execute(
            f"UPDATE team SET {assignments} WHERE id = ? AND season_id = ?",
            (*changes.values(), team_id, season_id),
        )
    except sqlite3.IntegrityError as exc:
        detail = str(exc)
        if "draft_position" in detail:
            raise TeamError(
                f"Draft position {changes.get('draft_position')} is already taken "
                "in this season."
            ) from exc
        raise TeamError(f"A team called {changes.get('name')!r} already exists.") from exc
    return get(conn, season_id, team_id)


def delete(conn: sqlite3.Connection, season_id: int, team_id: int) -> None:
    get(conn, season_id, team_id)   # raises TeamNotFound
    conn.execute("DELETE FROM team WHERE id = ? AND season_id = ?", (team_id, season_id))


def set_order(conn: sqlite3.Connection, season_id: int, order: list[int]) -> list[sqlite3.Row]:
    """Assign draft positions 1..N from a list of team ids.

    Positions are cleared first because UNIQUE (season_id, draft_position)
    would otherwise reject any reordering that swaps two teams.
    """
    existing = {row["id"] for row in list_teams(conn, season_id)}
    if set(order) != existing:
        missing = sorted(existing - set(order))
        extra = sorted(set(order) - existing)
        raise TeamError(
            "The order must list every team in this season exactly once."
            + (f" Missing: {missing}." if missing else "")
            + (f" Not in this season: {extra}." if extra else "")
        )

    conn.execute("UPDATE team SET draft_position = NULL WHERE season_id = ?", (season_id,))
    for position, team_id in enumerate(order, start=1):
        conn.execute("UPDATE team SET draft_position = ? WHERE id = ?", (position, team_id))
    return list_teams(conn, season_id)
