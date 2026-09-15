"""HTTP routes for results, the standings and the elimination leaderboard.

Same shape as draft_routes: thin routes whose only job is to turn a service
exception into the right status code and read the bearer token.

Who may do what:

    record / delete a result   the season's admin token
    standings / leaderboard    nobody — they are the league's shop window

Only the commissioner writes results, and that is not a security position so
much as an editorial one: a standings table that any player can post into is a
standings table with two versions of last night's match in it. Reading is open
to everyone, including people who are not in the league at all, for the same
reason the draft board is.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from draftmons.services import player_service as players
from draftmons.services import results_service as results
from draftmons.routes.player_routes import bearer
from draftmons.poke_db import connect, transaction

router = APIRouter(prefix="/seasons/{season_id}", tags=["results"])


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------- models


class ElimLine(BaseModel):
    """One Pokemon's stat line in one match."""

    team_id: int = Field(description="Which side it fought for. One of the two in the match.")
    name: str = Field(min_length=1, max_length=60,
                      description="PokeAPI name ('great-tusk') or Showdown id ('greattusk')")
    elims: int = Field(default=0, ge=0, le=24,
                       description="KOs it took. A line with 0 is accepted and dropped.")


class MatchCreate(BaseModel):
    """One played match. The stat line comes with it rather than after it.

    Recording the eliminations in the same request is what keeps the two
    leaderboards from disagreeing: there is no window in which a result exists
    with nothing under it, and no second request that can be forgotten.
    """

    week_no: int = Field(ge=1, le=52, description="Which week of the season")
    home_team_id: int
    away_team_id: int
    winner_team_id: int | None = Field(
        default=None, description="One of the two teams, or null for a draw"
    )
    home_score: int = Field(default=0, ge=0, le=24, description="Pokémon left standing")
    away_score: int = Field(default=0, ge=0, le=24)
    replay_url: str | None = Field(default=None, max_length=500)
    elims: list[ElimLine] = Field(default_factory=list, max_length=48)

    @field_validator("replay_url")
    @classmethod
    def must_be_http(cls, value: str | None) -> str | None:
        """The URL is rendered as a link, so reject anything that is not one."""
        if value in (None, ""):
            return None
        if not re.match(r"^https?://", value):
            raise ValueError("replay_url must start with http:// or https://")
        return value


def _authorized(call):
    """Run an auth check, turning its refusal into the right status code.

    Identical to draft_routes' — a missing token is a 401 and a wrong one a
    403 — and duplicated rather than shared because the two routers would
    otherwise import each other for four lines.
    """
    try:
        return call()
    except players.AuthError as exc:
        status = 401 if "needs" in str(exc) else 403
        raise HTTPException(status, str(exc)) from exc
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except players.PlayerError as exc:
        raise HTTPException(403, str(exc)) from exc


# -------------------------------------------------------------- results


@router.post("/matches", status_code=201)
def record_match(season_id: int, body: MatchCreate,
                 token: str | None = Depends(bearer)) -> dict:
    """Record a played match and what each Pokémon did in it."""
    with transaction() as conn:
        _authorized(lambda: players.require_admin(conn, season_id, token))
        try:
            return results.record(
                conn, season_id,
                week_no=body.week_no,
                home_team_id=body.home_team_id,
                away_team_id=body.away_team_id,
                winner_team_id=body.winner_team_id,
                home_score=body.home_score,
                away_score=body.away_score,
                replay_url=body.replay_url,
                elims=[line.model_dump() for line in body.elims],
            )
        except results.ResultNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except results.ResultError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.get("/matches")
def list_matches(
    season_id: int,
    week: int | None = Query(None, ge=1, le=52, description="Only this week's matches"),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """Every result recorded so far, newest week first. Public."""
    try:
        return results.list_matches(db, season_id, week)
    except results.ResultNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/matches/{match_id}", status_code=204)
def delete_match(season_id: int, match_id: int,
                 token: str | None = Depends(bearer)) -> Response:
    """Undo a result. The way a mistyped score is corrected: delete, re-record."""
    with transaction() as conn:
        _authorized(lambda: players.require_admin(conn, season_id, token))
        try:
            results.delete_match(conn, season_id, match_id)
        except results.ResultNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


# ---------------------------------------------------------- leaderboards


@router.get("/standings")
def read_standings(season_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """The table: every team's record, and its result in each week."""
    try:
        return results.standings(db, season_id)
    except results.ResultNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/leaderboard/pokemon")
def read_pokemon_leaderboard(
    season_id: int,
    limit: int = Query(5, ge=1, le=50, description="How many to return"),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """The season's Pokémon ranked by total eliminations. Top five by default."""
    try:
        return results.top_pokemon(db, season_id, limit)
    except results.ResultNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
