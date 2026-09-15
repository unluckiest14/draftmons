"""HTTP routes for reading a replay into the stats tab.

Two endpoints on purpose, and the split is the whole design:

    preview   reads the replay and says what it found. Writes nothing.
    record    does that, then files it as a match result.

A replay is parsed heuristically — which league team a side belongs to is
inferred from the Pokemon they brought — so there has to be a step where a
commissioner sees the guess before it becomes a row in the standings. Preview
is that step. It is also safe to call repeatedly while someone fixes a link.

Everything `record` writes goes through results_service, the same path a
hand-entered result takes. The replay is a faster way to fill the form, not a
second way into the database with its own rules.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from draftmons.services import replay_service as replays
from draftmons.services import results_service as results
from draftmons.poke_db import connect, transaction

router = APIRouter(prefix="/seasons/{season_id}/replay", tags=["replay"])


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


class ReplayIn(BaseModel):
    url: str = Field(min_length=1, description="A replay link, or just its id")


class ReplayRecordIn(ReplayIn):
    week_no: int = Field(ge=1, le=99, description="Which week this match belongs to")
    # Overrides for the cases the matcher cannot get right on its own: a
    # brand-new team with no picks yet, or two sides that matched the same one.
    home_team_id: int | None = None
    away_team_id: int | None = None


@router.post("/preview")
def preview_replay(season_id: int, body: ReplayIn,
                   db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Read a replay and show what would be recorded. Writes nothing."""
    try:
        analysis = replays.analyze(body.url)
    except replays.ReplayError as exc:
        raise HTTPException(400, str(exc)) from exc
    return replays.propose(db, season_id, analysis)


@router.post("/record")
def record_replay(season_id: int, body: ReplayRecordIn) -> dict[str, Any]:
    """Read a replay and file it as this week's result."""
    try:
        analysis = replays.analyze(body.url)
    except replays.ReplayError as exc:
        raise HTTPException(400, str(exc)) from exc

    with transaction() as conn:
        proposal = replays.propose(conn, season_id, analysis)

        # An override replaces the guess for that side, and re-points every
        # stat line that belonged to it — otherwise the Pokemon would be
        # credited to a team that results_service then rejects as not theirs.
        for key, override in (("home_team_id", body.home_team_id),
                              ("away_team_id", body.away_team_id)):
            if override is None:
                continue
            index = 0 if key == "home_team_id" else 1
            was = proposal["sides"][index]["team_id"]
            proposal["sides"][index]["team_id"] = override
            proposal[key] = override
            for line in proposal["elims"]:
                if line["team_id"] == was:
                    line["team_id"] = override
            if proposal["winner_team_id"] == was:
                proposal["winner_team_id"] = override

        if proposal["home_team_id"] is None or proposal["away_team_id"] is None:
            raise HTTPException(
                409,
                "Could not tell which league teams played. "
                + " ".join(proposal["problems"])
                + " Send home_team_id and away_team_id to say who they were.",
            )

        try:
            match = results.record(
                conn, season_id,
                week_no=body.week_no,
                home_team_id=proposal["home_team_id"],
                away_team_id=proposal["away_team_id"],
                winner_team_id=proposal["winner_team_id"],
                home_score=proposal["home_score"],
                away_score=proposal["away_score"],
                replay_url=proposal["replay_url"],
                elims=[line for line in proposal["elims"] if line["team_id"]],
            )
        except results.ResultNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except results.ResultError as exc:
            raise HTTPException(409, str(exc)) from exc

        # Saved inside the same transaction as the result: a match whose
        # history silently failed to write would look complete and not be.
        replays.store_events(conn, match["id"], analysis.get("events", []))

    return {
        "match": match,
        "analysis": analysis,
        "events": analysis.get("events", []),
        "warnings": proposal["problems"],
    }


@router.get("/events/{match_id}")
def read_match_events(season_id: int, match_id: int,
                      db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """The knockout-by-knockout history of a match already recorded."""
    owned = db.execute(
        "SELECT 1 FROM match_result WHERE id = ? AND season_id = ?",
        (match_id, season_id),
    ).fetchone()
    if owned is None:
        raise HTTPException(404, "No such match in this season.")
    return {
        "match_id": match_id,
        "events": replays.match_events(db, match_id, season_id),
    }
