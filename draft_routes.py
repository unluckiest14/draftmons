"""HTTP routes for the draft.

Same shape as player_routes: thin routes whose only job is to turn a service
exception into the right status code, plus reading the bearer token.

Who may do what:

    start / undo / reset   the season's admin token
    pick                   the player whose team is on the clock,
                           or the admin, picking on someone's behalf
    state / board          nobody — the board is public, which is the point

Letting the admin pick for a team is not an afterthought. Drafts happen on a
voice call with someone's laptop shut, and a commissioner who cannot enter a
pick for an absent player has to stop the draft.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import draft_service as drafts
import player_service as players
from player_routes import bearer
from poke_db import connect, transaction

router = APIRouter(prefix="/seasons/{season_id}/draft", tags=["draft"])


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


class StartDraft(BaseModel):
    randomize: bool = Field(
        default=False,
        description="Shuffle the draft order instead of using the positions already set",
    )
    pick_seconds: int = Field(
        default=drafts.DEFAULT_PICK_SECONDS, ge=0, le=86_400,
        description="Seconds on the clock per pick. 0 turns the timer off.",
    )


class MakePick(BaseModel):
    name: str = Field(min_length=1, max_length=60,
                      description="PokeAPI name ('great-tusk') or Showdown id ('greattusk')")
    team_id: int | None = Field(
        default=None,
        description="Admin only: pick on behalf of this team",
    )


def _authorized(call):
    """Run an auth check, turning its refusal into the right status code.

    player_service raises rather than returning, and an uncaught AuthError is
    a 500 that tells the caller nothing. Every route here goes through this so
    a missing token reads as 401 and a wrong one as 403.
    """
    try:
        return call()
    except players.AuthError as exc:
        # "needs a token" is a 401; "this is not the admin token" is a 403.
        status = 401 if "needs" in str(exc) else 403
        raise HTTPException(status, str(exc)) from exc
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except players.PlayerError as exc:
        raise HTTPException(403, str(exc)) from exc


def _resolve_team(conn: sqlite3.Connection, season_id: int, body: MakePick,
                  token: str | None) -> int:
    """Whose pick this is, according to the token rather than the request body.

    A player token picks for that player's own team and nothing else. Only an
    admin token may name a team, and only then is `team_id` read at all — so a
    player cannot spend another team's points by passing their id.
    """
    if body.team_id is not None:
        _authorized(lambda: players.require_admin(conn, season_id, token))
        return body.team_id
    player = _authorized(lambda: players.authenticate(conn, token))
    if player["season_id"] != season_id:
        raise HTTPException(403, "That token belongs to a different season.")
    return player["team_id"]


def _tick(season_id: int) -> list[dict]:
    """Resolve any expired pick timer before answering.

    These two routes are GETs that can write, which is unusual enough to say
    why: there is no scheduler in this app, so the clock has to be evaluated by
    whoever next asks about the draft. The alternative is a background worker
    for a process that may have nobody watching it.

    A failure here is swallowed on purpose. The timer is a convenience; being
    unable to read the board because a deferral could not be written would be
    a worse outcome than a turn expiring late.
    """
    try:
        with transaction() as conn:
            return drafts.enforce_clock(conn, season_id)
    except sqlite3.Error:
        return []


@router.get("")
def read_state(season_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Whose turn it is, how long they have left, and how far along the draft is."""
    events = _tick(season_id)
    try:
        return {**drafts.state(db, season_id), "clock_events": events}
    except drafts.DraftError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/board")
def read_board(season_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Every team and every pick. Public on purpose — this is the shared view."""
    events = _tick(season_id)
    try:
        return {**drafts.board(db, season_id), "clock_events": events}
    except drafts.DraftError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/start")
def start_draft(season_id: int, body: StartDraft,
                token: str | None = Depends(bearer)) -> dict:
    with transaction() as conn:
        _authorized(lambda: players.require_admin(conn, season_id, token))
        try:
            return drafts.start(
                conn, season_id,
                randomize=body.randomize, pick_seconds=body.pick_seconds,
            )
        except drafts.DraftError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/pick")
def make_pick(season_id: int, body: MakePick,
              token: str | None = Depends(bearer)) -> dict:
    with transaction() as conn:
        team_id = _resolve_team(conn, season_id, body, token)
        # Before the pick, not after: a pick that arrives a second late must
        # lose to the timer, or being slow would pay off whenever nobody
        # happened to be watching the board.
        drafts.enforce_clock(conn, season_id)
        try:
            return drafts.pick(conn, season_id, team_id, body.name)
        except drafts.NotYourTurn as exc:
            raise HTTPException(409, str(exc)) from exc
        except drafts.CannotAfford as exc:
            # 402 says exactly what went wrong, and lets the frontend tell a
            # "you cannot afford this" apart from every other refusal without
            # matching on the message text.
            raise HTTPException(402, str(exc)) from exc
        except drafts.DraftError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/undo")
def undo_pick(season_id: int, token: str | None = Depends(bearer)) -> dict:
    with transaction() as conn:
        _authorized(lambda: players.require_admin(conn, season_id, token))
        try:
            return drafts.undo(conn, season_id)
        except drafts.DraftError as exc:
            raise HTTPException(409, str(exc)) from exc
