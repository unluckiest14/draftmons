"""Players: joining a season, and the private storage that comes with it.

The problem this solves. A draft league is a handful of people who each want
their own space in a shared season, and the design doc rules out accounts —
account friction is what drove people off the tools this replaces. So there are
no usernames and no passwords. Joining a season hands back one bearer token,
and holding that token *is* being that player.

What "private" means here, precisely:

  public        the pool, the format, the list of team names in a season
  private       everything a player writes — their draft plans
  secret        the tokens themselves, which exist only in the client

Privacy is enforced by scoping, not by filtering. Every private query takes a
`player_id` and puts it in the WHERE clause, so a player asking for plan 7
when plan 7 belongs to someone else gets "no such plan" — the row is not
fetched and then hidden, it is never selected. That is the difference between a
privacy bug and a privacy property: there is no code path that reads another
player's rows and then decides not to return them.

Nothing here trusts an id from the client to identify a player. The only thing
that identifies a player is the token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from typing import Any

import team_service

# No 0/O/1/I/l: a join code gets read out on a voice call and typed by someone
# who is half paying attention.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 8

# Prefixes make a leaked token recognisable in a log or a paste, and let the
# API reject an admin token used as a player token before it touches the
# database.
PLAYER_PREFIX = "dmp_"
ADMIN_PREFIX = "dma_"

# 32 bytes of entropy. The length of the token is what makes a single SHA-256
# sufficient; see the schema comment on season_invite.
TOKEN_BYTES = 32

MAX_PLAN_BYTES = 256 * 1024   # a plan is a dozen Pokemon; this is generous


class PlayerError(Exception):
    """Something the caller did. The message is safe to show them."""


class PlayerNotFound(PlayerError):
    """No player, team or plan matching what was asked for."""


class AuthError(PlayerError):
    """A missing, malformed or unknown token."""


class SeasonFull(PlayerError):
    """The season has as many teams as a draft holds."""


# --------------------------------------------------------------- tokens


def _mint(prefix: str) -> tuple[str, str]:
    """A new token and its hash. The token is returned to the caller once."""
    token = prefix + secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_join_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


# --------------------------------------------------------------- invites


def get_invite(conn: sqlite3.Connection, season_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM season_invite WHERE season_id = ?", (season_id,)
    ).fetchone()


def open_season(
    conn: sqlite3.Connection, season_id: int, admin_token: str | None = None
) -> dict[str, Any]:
    """Open a season for joining, or rotate the code of one already open.

    First call claims the season and returns an admin token. Later calls need
    that token, so a player who joined cannot rotate the code out from under
    the commissioner. Rotating invalidates the old code and nothing else —
    players who already joined keep their tokens and their plans.
    """
    if conn.execute("SELECT 1 FROM season WHERE id = ?", (season_id,)).fetchone() is None:
        raise PlayerNotFound(f"No season with id {season_id}")

    existing = get_invite(conn, season_id)
    code = new_join_code()

    if existing is None:
        admin, admin_digest = _mint(ADMIN_PREFIX)
        conn.execute(
            "INSERT INTO season_invite (season_id, join_code, admin_hash) VALUES (?, ?, ?)",
            (season_id, code, admin_digest),
        )
        return {"join_code": code, "admin_token": admin, "rotated": False}

    require_admin(conn, season_id, admin_token)
    conn.execute(
        "UPDATE season_invite SET join_code = ?, is_open = 1 WHERE season_id = ?",
        (code, season_id),
    )
    # No new admin token: the caller proved they already hold it.
    return {"join_code": code, "admin_token": None, "rotated": True}


def require_admin(conn: sqlite3.Connection, season_id: int, admin_token: str | None) -> sqlite3.Row:
    """The season's invite row, if the token is its admin token."""
    invite = get_invite(conn, season_id)
    if invite is None:
        raise PlayerNotFound(f"Season {season_id} is not open for joining.")
    if not admin_token or not secrets.compare_digest(
        invite["admin_hash"], hash_token(admin_token)
    ):
        raise AuthError("That is not this season's admin token.")
    return invite


def set_open(
    conn: sqlite3.Connection, season_id: int, is_open: bool, admin_token: str | None
) -> sqlite3.Row:
    """Close a season once everyone is in, so a leaked code stops working."""
    require_admin(conn, season_id, admin_token)
    conn.execute(
        "UPDATE season_invite SET is_open = ? WHERE season_id = ?",
        (1 if is_open else 0, season_id),
    )
    return get_invite(conn, season_id)


# ---------------------------------------------------------------- joining


def join(
    conn: sqlite3.Connection,
    join_code: str,
    team_name: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Join a season by code, taking a team name. Returns the token once.

    The team name is the one thing the player chooses, and it is public — a
    league where you cannot see who else is in it is not a league. Everything
    the player goes on to write is private.

    The uniqueness of the name is the database's UNIQUE (season_id, name), via
    team_service, which already turns that into a sentence a person can act on.
    """
    invite = conn.execute(
        "SELECT * FROM season_invite WHERE join_code = ?", (join_code.strip().upper(),)
    ).fetchone()
    if invite is None:
        raise PlayerNotFound("That join code does not match any season.")
    if not invite["is_open"]:
        raise PlayerError("This season has closed for joining. Ask for a new code.")

    season_id = invite["season_id"]
    try:
        team = team_service.create(conn, season_id, team_name, owner=display_name)
    except team_service.TeamError as exc:
        # MAX_TEAMS and the duplicate-name case both land here; both messages
        # are already written for the player.
        raise SeasonFull(str(exc)) from exc

    token, digest = _mint(PLAYER_PREFIX)
    cursor = conn.execute(
        "INSERT INTO player (season_id, team_id, token_hash, display_name) "
        "VALUES (?, ?, ?, ?)",
        (season_id, team["id"], digest, display_name),
    )
    return {
        "token": token,
        "player": conn.execute(
            "SELECT * FROM player WHERE id = ?", (cursor.lastrowid,)
        ).fetchone(),
        "team": team,
    }


def issue_token(
    conn: sqlite3.Connection, season_id: int, team_id: int, admin_token: str | None
) -> dict[str, Any]:
    """Mint a player token for a team that already exists. Commissioner only.

    Two situations, one mechanism. A commissioner who ran the draft from one
    laptop, picking for the table, holds an admin token and no team — but one
    of those teams is theirs, and every part of this app that asks "which team
    are you" reads a player token. And a player who lost theirs is, by the
    design of this system, locked out forever: there is no email to reset
    against and the hash cannot be reversed.

    Both are answered by letting the person who runs the league issue one.
    That is not a hole in the model — the commissioner can already delete the
    team outright — but it is a real transfer of control, so it says what it
    did: an existing token for that team stops working the moment this
    returns, and the caller has to be told.
    """
    require_admin(conn, season_id, admin_token)

    team = conn.execute(
        "SELECT * FROM team WHERE id = ? AND season_id = ?", (team_id, season_id)
    ).fetchone()
    if team is None:
        raise PlayerNotFound(f"No team {team_id} in season {season_id}.")

    token, digest = _mint(PLAYER_PREFIX)
    existing = conn.execute(
        "SELECT * FROM player WHERE team_id = ? AND season_id = ?", (team_id, season_id)
    ).fetchone()

    if existing is None:
        # A team with no player behind it: made by the commissioner rather
        # than by somebody joining. This gives it its first token.
        conn.execute(
            "INSERT INTO player (season_id, team_id, token_hash, display_name) "
            "VALUES (?, ?, ?, ?)",
            (season_id, team_id, digest, team["owner"]),
        )
    else:
        conn.execute(
            "UPDATE player SET token_hash = ?, last_seen_at = NULL WHERE id = ?",
            (digest, existing["id"]),
        )

    return {
        "token": token,
        "team": team,
        # The one fact the caller cannot work out for itself, and the one that
        # decides whether this needs a warning in front of it.
        "rotated": existing is not None,
    }


def authenticate(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row:
    """Token -> player row. The only way a request becomes a player.

    Lookup is by hash on a UNIQUE column, so an unknown token costs one index
    probe and reveals nothing but "no".
    """
    if not token:
        raise AuthError("This endpoint needs a player token. Send: Authorization: Bearer <token>")
    if not token.startswith(PLAYER_PREFIX):
        # An admin token is not a player and must never resolve to one.
        raise AuthError("That is not a player token.")

    row = conn.execute(
        "SELECT * FROM player WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    if row is None:
        raise AuthError("That player token is not valid. It may have left the season.")
    return row


def touch(conn: sqlite3.Connection, player_id: int) -> None:
    """Record that the player is around. Best-effort; never fails a request."""
    conn.execute(
        "UPDATE player SET last_seen_at = datetime('now') WHERE id = ?", (player_id,)
    )


def leave(conn: sqlite3.Connection, player: sqlite3.Row) -> None:
    """Delete the player, their team and their plans.

    The plans go with them by ON DELETE CASCADE, deliberately: they are private
    to a player who no longer exists, so there is nobody left who is allowed to
    read them.
    """
    conn.execute("DELETE FROM player WHERE id = ?", (player["id"],))
    conn.execute("DELETE FROM team WHERE id = ?", (player["team_id"],))


def rename_team(conn: sqlite3.Connection, player: sqlite3.Row, name: str) -> sqlite3.Row:
    """A player renames their own team, and only their own."""
    return team_service.update(conn, player["season_id"], player["team_id"], {"name": name})


def set_display_name(
    conn: sqlite3.Connection, player: sqlite3.Row, display_name: str | None
) -> sqlite3.Row:
    conn.execute(
        "UPDATE player SET display_name = ? WHERE id = ?", (display_name, player["id"])
    )
    # Kept in step with team.owner, which is what the public roster shows.
    conn.execute(
        "UPDATE team SET owner = ? WHERE id = ?", (display_name, player["team_id"])
    )
    return conn.execute("SELECT * FROM player WHERE id = ?", (player["id"],)).fetchone()


# ------------------------------------------------------------- the lobby


def lobby(conn: sqlite3.Connection, season_id: int) -> dict[str, Any]:
    """Who is in this season. Public, and deliberately thin.

    Team names and draft order only. No tokens, no plan counts, no "last seen":
    a roster is public because you need it to run a draft, and anything beyond
    that is the player's business.
    """
    season = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    if season is None:
        raise PlayerNotFound(f"No season with id {season_id}")
    invite = get_invite(conn, season_id)

    return {
        "season_id": season_id,
        "season_name": season["name"],
        "budget": season["budget"],
        "roster_size": season["roster_size"],
        "format_key": season["format_key"],
        "is_open": bool(invite["is_open"]) if invite else False,
        "capacity": team_service.MAX_TEAMS,
        "teams": [
            {
                "id": row["id"],
                "name": row["name"],
                "owner": row["owner"],
                "draft_position": row["draft_position"],
            }
            for row in team_service.list_teams(conn, season_id)
        ],
    }


def roster(conn: sqlite3.Connection, season_id: int, admin_token: str | None) -> list[dict]:
    """The commissioner's view: who joined and when. Still no tokens."""
    require_admin(conn, season_id, admin_token)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT p.id, p.display_name, p.created_at, p.last_seen_at, "
            "       t.name AS team_name, t.draft_position "
            "FROM player p JOIN team t ON t.id = p.team_id "
            "WHERE p.season_id = ? ORDER BY p.id",
            (season_id,),
        )
    ]


# ----------------------------------------------------------- private plans

# Every function below takes the player row, never a player id from the
# client, and every statement filters on it. That is the whole privacy
# mechanism, and it is why these are five nearly identical small functions
# rather than one generic helper with an id argument.


def list_plans(conn: sqlite3.Connection, player: sqlite3.Row) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, created_at, updated_at FROM player_plan "
        "WHERE player_id = ? ORDER BY updated_at DESC",
        (player["id"],),
    ).fetchall()


def get_plan(conn: sqlite3.Connection, player: sqlite3.Row, plan_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM player_plan WHERE id = ? AND player_id = ?",
        (plan_id, player["id"]),
    ).fetchone()
    if row is None:
        # Deliberately the same error whether the plan is missing or belongs to
        # someone else. Distinguishing them would let a player enumerate how
        # many plans the rest of the league has.
        raise PlayerNotFound(f"No plan {plan_id} of yours.")
    return row


def _check_body(body: Any) -> str:
    """Plans are opaque JSON to the server, but not unbounded."""
    encoded = json.dumps(body)
    if len(encoded.encode()) > MAX_PLAN_BYTES:
        raise PlayerError(f"That plan is larger than {MAX_PLAN_BYTES // 1024} KB.")
    return encoded


def create_plan(
    conn: sqlite3.Connection, player: sqlite3.Row, name: str, body: Any
) -> sqlite3.Row:
    cursor = conn.execute(
        "INSERT INTO player_plan (player_id, name, body) VALUES (?, ?, ?)",
        (player["id"], name, _check_body(body)),
    )
    return get_plan(conn, player, cursor.lastrowid)


def update_plan(
    conn: sqlite3.Connection,
    player: sqlite3.Row,
    plan_id: int,
    name: str | None,
    body: Any,
) -> sqlite3.Row:
    get_plan(conn, player, plan_id)   # raises PlayerNotFound, scoped to owner

    changes: dict[str, Any] = {"updated_at": None}
    if name is not None:
        changes["name"] = name
    if body is not None:
        changes["body"] = _check_body(body)
    if len(changes) == 1:
        raise PlayerError("Nothing to update. Send a name, a body, or both.")

    assignments = ", ".join(
        "updated_at = datetime('now')" if key == "updated_at" else f"{key} = ?"
        for key in changes
    )
    values = [value for key, value in changes.items() if key != "updated_at"]
    conn.execute(
        f"UPDATE player_plan SET {assignments} WHERE id = ? AND player_id = ?",
        (*values, plan_id, player["id"]),
    )
    return get_plan(conn, player, plan_id)


def delete_plan(conn: sqlite3.Connection, player: sqlite3.Row, plan_id: int) -> None:
    get_plan(conn, player, plan_id)   # raises PlayerNotFound, scoped to owner
    conn.execute(
        "DELETE FROM player_plan WHERE id = ? AND player_id = ?", (plan_id, player["id"])
    )
