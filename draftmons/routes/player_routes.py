"""HTTP routes for joining a season, and for a player's private storage.

A router rather than more routes in main.py, for two reasons. These endpoints
are the only ones in the API with an authentication step, so keeping them
together makes the boundary between public and private legible in one file.
And the models live here beside them: they are about tokens and plan bodies,
which nothing else in the API touches.

Routes stay as thin as main.py's. The one thing they own is the same thing
main.py's own routes own — turning a service exception into the right status
code — plus reading the bearer token off the request, which is the only place
that header is parsed.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from draftmons.services import player_service as players
from draftmons.poke_db import connect, transaction

router = APIRouter()


def row(record: sqlite3.Row | None) -> dict | None:
    """A sqlite3.Row as a dict, for a response_model to validate.

    Rows support key access but not attribute access, so Pydantic's
    from_attributes cannot read one — it reports every field as missing. Every
    route in main.py converts for the same reason.
    """
    return dict(record) if record is not None else None


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# auto_error=False because these routes raise their own 401 with a message
# that says what to do about it, and because a missing token on an admin route
# has to be told apart from a wrong one. Declaring the scheme at all is what
# puts the Authorize button in /docs, which turns "test the join flow" from
# retyping a header twelve times into pasting a token once.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="PlayerToken",
    description="The token from POST /join/{code}, or a season's admin token.",
)


def bearer(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str | None:
    """The token out of `Authorization: Bearer <token>`, or None.

    In a header and never in the URL or the body: query strings end up in
    access logs, browser history and Referer headers, and a token that leaks
    that way is a player's whole identity.
    """
    if credentials is None or not credentials.credentials:
        return None
    return credentials.credentials.strip()


def current_player(
    token: str | None = Depends(bearer),
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    """The player making this request. Every private route depends on this."""
    try:
        player = players.authenticate(db, token)
    except players.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc

    # Separate connection, because `db` here is read-only by convention and
    # this is a write. Failing to record a heartbeat must not fail the request.
    try:
        with transaction() as conn:
            players.touch(conn, player["id"])
    except sqlite3.Error:
        pass
    return player


# ---------------------------------------------------- commissioner gates

# Applied with `dependencies=[Depends(...)]` on the route decorator rather than
# as a handler argument, so a route gains a lock without its body changing at
# all — which keeps this out of the way of whoever owns those handlers.


def require_season_admin(
    season_id: int,
    token: str | None = Depends(bearer),
    db: sqlite3.Connection = Depends(get_db),
) -> None:
    """Commissioner-only, for anything that rewrites a whole league.

    Same bargain as season_service._require_owner, deliberately: a league
    nobody has opened for joining is open to whoever finds it, and once
    someone claims it by opening it, only that token can change it. Keeping
    the two identical matters — a pool you can re-cost but a season you cannot
    rename would be a strange half-lock.

    Without this, a public URL lets any passer-by re-price 771 Pokémon or
    trigger an 800-request pool rebuild.
    """
    if players.get_invite(db, season_id) is None:
        return
    try:
        players.require_admin(db, season_id, token)
    except players.AuthError as exc:
        raise HTTPException(403, f"{exc} This needs the league's commissioner token.") from exc
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def require_team_owner_or_admin(
    season_id: int,
    team_id: int,
    token: str | None = Depends(bearer),
    db: sqlite3.Connection = Depends(get_db),
) -> None:
    """The team's own player, or the commissioner.

    A logo and a team name belong to the player who drafts under them, so
    locking these to the commissioner would mean asking them to upload
    everyone's badge. The owner test is the player token resolving to a player
    whose team_id is this team — never a team_id supplied by the caller.
    """
    if players.get_invite(db, season_id) is None:
        return

    if token and token.startswith(players.PLAYER_PREFIX):
        try:
            player = players.authenticate(db, token)
        except players.AuthError:
            player = None
        if player and player["team_id"] == team_id and player["season_id"] == season_id:
            return

    try:
        players.require_admin(db, season_id, token)
    except players.AuthError as exc:
        raise HTTPException(
            403, "This needs the team's own player token, or the league's commissioner token."
        ) from exc
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# --------------------------------------------------------------- models


class InviteOut(BaseModel):
    season_id: int
    join_code: str
    is_open: bool
    joined: int
    capacity: int
    # Present exactly once, in the response that creates the invite. Never
    # returned by a read, because a token you can fetch again is not a secret.
    admin_token: str | None = None
    rotated: bool = False


class OpenUpdate(BaseModel):
    is_open: bool


class JoinRequest(BaseModel):
    """What a player sends to join. The team name is the only required field."""

    team_name: str = Field(min_length=1, max_length=60,
                           description="Public. Unique within the season.")
    display_name: str | None = Field(default=None, max_length=40,
                                     description="Who you are, for the roster. Optional.")

    @field_validator("team_name", "display_name")
    @classmethod
    def strip_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @field_validator("team_name")
    @classmethod
    def name_required(cls, value: str | None) -> str:
        if not value:
            raise ValueError("team_name cannot be blank")
        return value


class TeamBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    owner: str | None = None
    draft_position: int | None = None


class PlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    season_id: int
    team_id: int
    display_name: str | None = None
    created_at: str | None = None


class JoinResult(BaseModel):
    """The token appears here and nowhere else, ever again."""

    token: str = Field(description="Save this. It cannot be recovered or reissued.")
    player: PlayerOut
    team: TeamBrief


class TokenIssued(BaseModel):
    """A player token for an existing team, shown once like every other."""

    token: str = Field(description="Save this. It cannot be recovered.")
    team: TeamBrief
    rotated: bool = Field(
        description="An earlier token for this team existed and has just stopped working",
    )


class MeOut(BaseModel):
    player: PlayerOut
    team: TeamBrief
    season_id: int


class MeUpdate(BaseModel):
    team_name: str | None = Field(default=None, min_length=1, max_length=60)
    display_name: str | None = Field(default=None, max_length=40)

    _strip = field_validator("team_name", "display_name")(JoinRequest.strip_blank.__func__)


class LobbyOut(BaseModel):
    season_id: int
    season_name: str
    budget: int
    roster_size: int
    format_key: str | None = None
    is_open: bool
    capacity: int
    teams: list[TeamBrief]


class PlanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: str | None = None
    updated_at: str | None = None


class PlanOut(PlanSummary):
    """`body` is whatever the client stored. The server does not interpret it."""

    body: Any = {}

    @field_validator("body", mode="before")
    @classmethod
    def decode(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    body: Any = Field(default_factory=dict)


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    body: Any = None


# -------------------------------------------------------------- invites


def _invite_out(conn: sqlite3.Connection, season_id: int, **extra) -> dict:
    invite = players.get_invite(conn, season_id)
    joined = conn.execute(
        "SELECT COUNT(*) AS n FROM player WHERE season_id = ?", (season_id,)
    ).fetchone()["n"]
    return {
        "season_id": season_id,
        "join_code": invite["join_code"],
        "is_open": bool(invite["is_open"]),
        "joined": joined,
        "capacity": players.team_service.MAX_TEAMS,
        **extra,
    }


@router.post("/seasons/{season_id}/invite", response_model=InviteOut, tags=["players"])
def open_or_rotate(
    season_id: int, admin: str | None = Depends(bearer)
) -> dict:
    """Open a season for joining, or rotate its code.

    The first call claims the season and returns an admin token once. After
    that this needs it, so a player who joined cannot rotate the code.
    """
    with transaction() as conn:
        try:
            result = players.open_season(conn, season_id, admin)
        except players.AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except players.PlayerNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return _invite_out(
            conn, season_id,
            admin_token=result["admin_token"], rotated=result["rotated"],
        )


@router.post("/seasons/{season_id}/teams/{team_id}/token",
             response_model=TokenIssued, tags=["players"])
def issue_team_token(season_id: int, team_id: int,
                     admin: str | None = Depends(bearer)) -> dict:
    """Issue a player token for a team that already exists. Commissioner only.

    The way a commissioner who drafted for the table takes their own team on
    this browser, and the way a player who lost their token gets back in —
    the only way, since nothing here can reverse a hash.

    Rotates: any token already issued for that team stops working. The
    response says whether that happened so the caller can warn before, not
    apologise after.
    """
    with transaction() as conn:
        try:
            issued = players.issue_token(conn, season_id, team_id, admin)
            # The team comes back as a Row, which a response_model cannot read
            # by attribute; every route here converts for the same reason.
            return {**issued, "team": row(issued["team"])}
        except players.AuthError as exc:
            status = 401 if "needs" in str(exc) else 403
            raise HTTPException(status, str(exc)) from exc
        except players.PlayerNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except players.PlayerError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.get("/seasons/{season_id}/invite", response_model=InviteOut, tags=["players"])
def read_invite(
    season_id: int,
    admin: str | None = Depends(bearer),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """The current code. Admin only — the code is what lets someone in."""
    try:
        players.require_admin(db, season_id, admin)
    except players.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return _invite_out(db, season_id)


@router.patch("/seasons/{season_id}/invite", response_model=InviteOut, tags=["players"])
def set_invite_open(
    season_id: int, body: OpenUpdate, admin: str | None = Depends(bearer)
) -> dict:
    """Close the season once everyone is in, or reopen it for a latecomer."""
    with transaction() as conn:
        try:
            players.set_open(conn, season_id, body.is_open, admin)
        except players.AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except players.PlayerNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return _invite_out(conn, season_id)


@router.get("/seasons/{season_id}/players", tags=["players"])
def read_roster(
    season_id: int,
    admin: str | None = Depends(bearer),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """Who joined and when. Admin only, and still no tokens in the response."""
    try:
        return players.roster(db, season_id, admin)
    except players.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# --------------------------------------------------------------- joining


@router.post("/join/{join_code}", response_model=JoinResult, status_code=201, tags=["players"])
def join_season(join_code: str, body: JoinRequest) -> dict:
    """Join a season with a code and a team name.

    The response carries the only copy of the player's token. There is no
    recovery path by design — no account means no email to reset against — so
    the client must save it. Losing it means asking the commissioner for a new
    code and joining again under a new team name.
    """
    with transaction() as conn:
        try:
            result = players.join(conn, join_code, body.team_name, body.display_name)
            return {
                "token": result["token"],
                "player": row(result["player"]),
                "team": row(result["team"]),
            }
        except players.PlayerNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except players.SeasonFull as exc:
            raise HTTPException(409, str(exc)) from exc
        except players.PlayerError as exc:
            raise HTTPException(403, str(exc)) from exc


@router.get("/seasons/{season_id}/lobby", response_model=LobbyOut, tags=["players"])
def read_lobby(season_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Who is in this season. Public: a draft needs a visible roster."""
    try:
        return players.lobby(db, season_id)
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# ------------------------------------------------------------------- me


@router.get("/me", response_model=MeOut, tags=["players"])
def read_me(
    player: sqlite3.Row = Depends(current_player),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    team = db.execute("SELECT * FROM team WHERE id = ?", (player["team_id"],)).fetchone()
    return {"player": row(player), "team": row(team), "season_id": player["season_id"]}


@router.patch("/me", response_model=MeOut, tags=["players"])
def update_me(body: MeUpdate, player: sqlite3.Row = Depends(current_player)) -> dict:
    """Rename my team, or change who I am on the roster. Mine only."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "Send team_name, display_name, or both.")

    with transaction() as conn:
        try:
            if "team_name" in changes:
                players.rename_team(conn, player, changes["team_name"])
            if "display_name" in changes:
                player = players.set_display_name(conn, player, changes["display_name"])
        except players.team_service.TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

        team = conn.execute(
            "SELECT * FROM team WHERE id = ?", (player["team_id"],)
        ).fetchone()
        return {"player": row(player), "team": row(team), "season_id": player["season_id"]}


@router.delete("/me", status_code=204, tags=["players"])
def leave_season(player: sqlite3.Row = Depends(current_player)) -> Response:
    """Leave, taking my team and my plans with me. Not reversible."""
    with transaction() as conn:
        players.leave(conn, player)
    return Response(status_code=204)


# --------------------------------------------------------- private plans


@router.get("/me/plans", response_model=list[PlanSummary], tags=["players"])
def list_my_plans(
    player: sqlite3.Row = Depends(current_player),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """My plans, newest first. Bodies omitted — this is a list, not a sync."""
    return [row(record) for record in players.list_plans(db, player)]


@router.post("/me/plans", response_model=PlanOut, status_code=201, tags=["players"])
def create_my_plan(body: PlanCreate, player: sqlite3.Row = Depends(current_player)) -> dict:
    with transaction() as conn:
        try:
            return row(players.create_plan(conn, player, body.name, body.body))
        except players.PlayerError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get("/me/plans/{plan_id}", response_model=PlanOut, tags=["players"])
def read_my_plan(
    plan_id: int,
    player: sqlite3.Row = Depends(current_player),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    try:
        return row(players.get_plan(db, player, plan_id))
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/me/plans/{plan_id}", response_model=PlanOut, tags=["players"])
def write_my_plan(
    plan_id: int, body: PlanUpdate, player: sqlite3.Row = Depends(current_player)
) -> dict:
    with transaction() as conn:
        try:
            return row(players.update_plan(conn, player, plan_id, body.name, body.body))
        except players.PlayerNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except players.PlayerError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.delete("/me/plans/{plan_id}", status_code=204, tags=["players"])
def delete_my_plan(plan_id: int, player: sqlite3.Row = Depends(current_player)) -> Response:
    with transaction() as conn:
        try:
            players.delete_plan(conn, player, plan_id)
        except players.PlayerNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)
