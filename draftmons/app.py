"""HTTP routes.

Deliberately thin. Every route does the same four things: get a connection,
call a service, translate a service exception into an HTTPException, return a
model. If a route grows real logic, that logic belongs in a service.

Error translation is the one thing routes own. A service raises TeamError
because something about the request was wrong; only the route knows that means
409 rather than 400 or 404. Keeping that mapping here is what lets the services
stay usable from the cron script, which has no HTTP at all.
"""

from __future__ import annotations

import base64
import binascii
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from draftmons.paths import WEB
from typing import Iterator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from draftmons.routes import draft_routes
from draftmons.services import format_service as fmt
from draftmons.routes import player_routes
from draftmons.services import player_service as players
from draftmons.services import pool_service
from draftmons.routes import replay_routes
from draftmons.routes import results_routes
from draftmons.services import season_service
from draftmons import showdown_items as items_data
from draftmons.services import team_logo
from draftmons.services import team_service
from draftmons.models import (
    BanUpdate,
    CostUpdate,
    CustomPool,
    CustomPoolResult,
    FormatOut,
    FormatRules,
    ItemOut,
    LogoOut,
    LogoUpload,
    LeagueOut,
    PoolAdd,
    PoolBuildResult,
    PoolEntryOut,
    PoolPaste,
    PoolPasteResult,
    SeasonCreate,
    SeasonOut,
    SeasonUpdate,
    TeamCreate,
    TeamOut,
    TeamUpdate,
)
# The one place the Authorization header is parsed; the season routes below
# reuse it so a commissioner's token means the same thing everywhere.
from draftmons.routes.player_routes import (
    bearer,
    require_season_admin,
    require_team_owner_or_admin,
)
from draftmons.poke_db import connect, init_db, transaction
from draftmons.pokeapi import PokeApiClient, PokeApiError


@asynccontextmanager
async def lifespan(app: FastAPI):
    applied = init_db()
    if applied:
        print(f"migrated: added {', '.join(applied)}")
    # One client for the app's lifetime. PokeApiClient's cache lives on the
    # instance, so a client per request would cache nothing and refetch the
    # same species hundreds of times during a pool build.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=5.0),
        headers={"User-Agent": "draftmons/0.1"},
    ) as http:
        app.state.poke = PokeApiClient(http)
        yield


app = FastAPI(title="Draft League", version="0.2.0", lifespan=lifespan)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# ------------------------------------------------------------- seasons


@app.post("/seasons", response_model=SeasonOut, status_code=201, tags=["seasons"])
def create_season(body: SeasonCreate) -> dict:
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO season (name, budget, roster_size, format_key) VALUES (?, ?, ?, ?)",
            (body.name, body.budget, body.roster_size, body.format_key),
        )
        return dict(
            conn.execute("SELECT * FROM season WHERE id = ?", (cursor.lastrowid,)).fetchone()
        )


@app.get("/seasons", response_model=list[LeagueOut], tags=["seasons"])
def list_seasons(db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """Every league on this server, newest first, with the counts that identify one.

    The route the board used to work around by probing ids 1 to 30 — which
    made 30 requests per load and could not see a league past the thirtieth.
    """
    return season_service.summary(db)


@app.get("/seasons/{season_id}", response_model=SeasonOut, tags=["seasons"])
def read_season(season_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    row = db.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"No season with id {season_id}")
    return dict(row)


def _owner_check(call):
    """Run a service call that may refuse on the admin token.

    A league with a claimed invite can only be changed by the holder of its
    token; one nobody has claimed is open to whoever finds it, which is the
    same bargain the join code already makes.
    """
    try:
        return call()
    except players.AuthError as exc:
        raise HTTPException(403, str(exc)) from exc
    except players.PlayerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except season_service.SeasonNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except season_service.SeasonError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.patch("/seasons/{season_id}", response_model=SeasonOut, tags=["seasons"])
def update_season(season_id: int, body: SeasonUpdate,
                  token: str | None = Depends(bearer)) -> dict:
    """Rename a league, or fix its budget and roster size before the draft."""
    with transaction() as conn:
        return dict(_owner_check(lambda: season_service.update(
            conn, season_id, body.model_dump(exclude_unset=True), token
        )))


@app.delete("/seasons/{season_id}", tags=["seasons"])
def delete_season(season_id: int, token: str | None = Depends(bearer)) -> dict:
    """Delete a league and everything in it. Returns what went with it.

    A body rather than a 204, because the caller wants to say "deleted the
    Sunday league, 8 teams and 771 Pokémon with it" — and after the row is
    gone there is nowhere left to count that from.
    """
    with transaction() as conn:
        return _owner_check(lambda: season_service.delete(conn, season_id, token))


# --------------------------------------------------------------- teams


@app.post("/seasons/{season_id}/teams", response_model=TeamOut, status_code=201, tags=["teams"], dependencies=[Depends(require_season_admin)])
def create_team(season_id: int, body: TeamCreate) -> dict:
    with transaction() as conn:
        try:
            return dict(team_service.create(
                conn, season_id, body.name, body.owner, body.logo_url
            ))
        except team_service.TeamNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except team_service.TeamError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.get("/seasons/{season_id}/teams", response_model=list[TeamOut], tags=["teams"])
def list_teams(season_id: int, db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return team_logo.decorate(db, season_id, team_service.list_teams(db, season_id))


@app.get("/seasons/{season_id}/teams/{team_id}", response_model=TeamOut, tags=["teams"])
def read_team(season_id: int, team_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        team = team_service.get(db, season_id, team_id)
    except team_service.TeamNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return team_logo.decorate(db, season_id, [team])[0]


@app.patch("/seasons/{season_id}/teams/{team_id}", response_model=TeamOut, tags=["teams"], dependencies=[Depends(require_team_owner_or_admin)])
def update_team(season_id: int, team_id: int, body: TeamUpdate) -> dict:
    # exclude_unset is what makes this a PATCH: it yields only the fields the
    # client actually sent, so an omitted field is left alone rather than
    # being overwritten with None.
    with transaction() as conn:
        try:
            return dict(team_service.update(
                conn, season_id, team_id, body.model_dump(exclude_unset=True)
            ))
        except team_service.TeamNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except team_service.TeamError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.delete("/seasons/{season_id}/teams/{team_id}", status_code=204, tags=["teams"], dependencies=[Depends(require_season_admin)])
def delete_team(season_id: int, team_id: int) -> Response:
    with transaction() as conn:
        try:
            team_service.delete(conn, season_id, team_id)
        except team_service.TeamNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


@app.post("/seasons/{season_id}/teams/order", response_model=list[TeamOut], tags=["teams"], dependencies=[Depends(require_season_admin)])
def set_team_order(season_id: int, order: list[int]) -> list[dict]:
    """Assign draft positions 1..N from a list of team ids.

    Deliberately unauthenticated, like the rest of the non-player API. Worth
    knowing that this means any player who joined could reorder the board;
    locking it is a decision to take across the whole API at once rather than
    on one route, since a padlock on one gate of an open fence buys nothing.
    """
    with transaction() as conn:
        draft = conn.execute(
            "SELECT status FROM draft WHERE season_id = ?", (season_id,)
        ).fetchone()
        if draft is not None and draft["status"] != "setup":
            # The snake reads its order from the positions. Rewriting them
            # mid-draft would hand the clock to a different team and leave the
            # picks already made attributed to the wrong slots.
            raise HTTPException(
                409, "The draft has started — the order cannot be changed now."
            )

        try:
            return [dict(row) for row in team_service.set_order(conn, season_id, order)]
        except team_service.TeamError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.put("/seasons/{season_id}/teams/{team_id}/logo", response_model=LogoOut, tags=["teams"], dependencies=[Depends(require_team_owner_or_admin)])
def upload_team_logo(season_id: int, team_id: int, body: LogoUpload) -> dict:
    """Upload a team's logo. Replaces whatever was there.

    Stored in the league's own database rather than linked, so it shows for
    every viewer of the draft board for as long as the league exists — a
    pasted link only works while someone else's host keeps serving it.
    """
    raw = body.data
    # A browser's FileReader hands back "data:image/png;base64,AAAA...", and
    # stripping the prefix here means the client does not have to.
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "That upload is not valid base64.") from exc

    with transaction() as conn:
        try:
            team = team_service.get(conn, season_id, team_id)
        except team_service.TeamNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            stored = team_logo.store(conn, team["id"], data)
        except team_logo.LogoError as exc:
            raise HTTPException(400, str(exc)) from exc

    return {
        "team_id": team_id,
        "logo": team_logo.url_for(season_id, team_id, stored["version"], None),
        **stored,
    }


@app.get("/seasons/{season_id}/teams/{team_id}/logo", tags=["teams"])
def read_team_logo(season_id: int, team_id: int,
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Serve a team's uploaded logo. Public, like the rest of the board."""
    if db.execute(
        "SELECT 1 FROM team WHERE id = ? AND season_id = ?", (team_id, season_id)
    ).fetchone() is None:
        raise HTTPException(404, "No such team in this season.")

    row = team_logo.fetch(db, team_id)
    if row is None:
        raise HTTPException(404, "That team has not uploaded a logo.")

    return Response(
        content=row["image"],
        media_type=row["content_type"],
        headers={
            # The URL carries a version, so a changed logo is a different URL
            # and this can be cached hard.
            "Cache-Control": "public, max-age=31536000, immutable",
            # The content type was decided by sniffing the bytes, not by the
            # uploader; nosniff stops a browser second-guessing it and
            # treating an image as something executable.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )


@app.delete("/seasons/{season_id}/teams/{team_id}/logo", status_code=204, tags=["teams"], dependencies=[Depends(require_team_owner_or_admin)])
def delete_team_logo(season_id: int, team_id: int) -> Response:
    """Remove an uploaded logo. Any pasted logo_url takes over again."""
    with transaction() as conn:
        try:
            team = team_service.get(conn, season_id, team_id)
        except team_service.TeamNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        if not team_logo.remove(conn, team["id"]):
            raise HTTPException(404, "That team has not uploaded a logo.")
    return Response(status_code=204)


# ------------------------------------------------------------- formats


@app.get("/formats", response_model=list[FormatOut], tags=["formats"])
def list_formats(db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    formats = fmt.list_formats(db)
    if not formats:
        raise HTTPException(
            503,
            "No formats loaded yet. Run: python update_formats.py",
        )
    return formats


@app.get("/formats/{format_key}/rules", response_model=FormatRules, tags=["formats"])
def read_format_rules(
    format_key: str, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """What Showdown enforces for this format beyond its species list.

    The pool already has the species bans applied — this is where a
    commissioner sees *why* Gastly is missing from LC, alongside the clauses
    and the move, ability and item bans a draft board cannot express.
    """
    record = fmt.get_format(db, format_key)
    if record is None:
        raise HTTPException(404, f"No format {format_key!r}. See GET /formats.")
    grouped = fmt.format_rules(db, format_key)
    return {
        "key": format_key,
        "label": record["label"],
        "showdown_name": record["showdown_name"],
        "clauses": grouped.get("clause", []),
        "banned_species": grouped.get("ban_species", []),
        "banned_tiers": grouped.get("ban_tier", []),
        "banned_other": grouped.get("ban_other", []),
        "complex_bans": grouped.get("ban_complex", []),
        "unbanned": grouped.get("unban", []),
    }


@app.get("/items", response_model=list[ItemOut], tags=["items"])
def list_items(
    format_key: str | None = Query(
        None, alias="format",
        description="Filter to what this format allows, and flag its banned items",
    ),
    include_banned: bool = Query(True, description="Keep banned items, flagged"),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """Held items a Pokemon can actually carry, each with a one-line description.

    Not PokeAPI's item list, which is every object in the games — bicycles, TMs,
    key items and all — and leaves a player scrolling past the Yellow Bike to
    find Leftovers. This is Showdown's own list, so everything in it is
    something you can hold in a battle.

    Pass `format` to narrow it further: Gen 9 OU drops the Mega stones and
    Z-crystals as Past, National Dex keeps them, and either way the format's own
    banned items come back flagged rather than silently missing.
    """
    catalogue = items_data.load_cached()
    if not catalogue:
        raise HTTPException(
            503,
            "Item data not loaded. Fetch it with: "
            "python update_formats.py --fetch-rules",
        )

    ladder, generation = "singles", 9
    banned: set[str] = set()
    if format_key:
        record = fmt.get_format(db, format_key)
        if record is None:
            raise HTTPException(404, f"No format {format_key!r}. See GET /formats.")
        ladder = record["ladder"]
        banned = {
            items_data.item_id(value)
            for value in fmt.format_rules(db, format_key).get("ban_other", [])
        }

    out = []
    for item in items_data.usable(catalogue, ladder, generation):
        is_banned = item.id in banned
        if is_banned and not include_banned:
            continue
        out.append({
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "spritenum": item.spritenum,
            "banned": is_banned,
            "category": (
                "mega" if item.mega_stone
                else "z-crystal" if item.z_crystal
                else "berry" if item.is_berry
                else None
            ),
            "users": item.users,
        })
    return out


@app.get("/formats/status", tags=["formats"])
def format_status(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """When the cron job last ran and what it saw. For a health check."""
    return {
        "snapshot": fmt.current_snapshot(db),
        "formats": len(fmt.list_formats(db)),
        "last_run": fmt.last_update(db),
    }


# ---------------------------------------------------------------- pool


@app.post(
    "/seasons/{season_id}/pool/from-format/{format_key}",
    response_model=PoolBuildResult,
    tags=["pool"],
    dependencies=[Depends(require_season_admin)],
)
async def build_pool(season_id: int, format_key: str, request: Request) -> dict:
    """Fill the pool from a format. Slow: about one PokeAPI call per Pokemon.

    Setup only. Never expose this to drafters.
    """
    with connect() as check:
        if check.execute("SELECT 1 FROM season WHERE id = ?", (season_id,)).fetchone() is None:
            raise HTTPException(404, f"No season with id {season_id}")

    with connect() as read:
        record = fmt.get_format(read, format_key)
        if record is None:
            raise HTTPException(404, f"No format {format_key!r}. See GET /formats.")
        species = fmt.format_species(read, format_key)

    # The PokeAPI work happens outside the write transaction: it takes seconds,
    # and SQLite's write lock is database-wide, so holding one across the fetch
    # would stall every other writer for the duration.
    try:
        fetched = await pool_service.fetch_pool(request.app.state.poke, species)
    except PokeApiError as exc:
        raise HTTPException(503, f"Can't reach PokeAPI: {exc}") from exc

    with transaction() as conn:
        result = pool_service.store_pool(
            conn, season_id, format_key, fetched, record["source_sha256"]
        )

    return {
        "format_key": format_key,
        "label": record["label"],
        "tier_snapshot": record["source_sha256"],
        **result,
    }


@app.post("/seasons/{season_id}/pool/entries", response_model=PoolEntryOut,
          status_code=201, tags=["pool"],
          dependencies=[Depends(require_season_admin)])
async def add_entry(season_id: int, body: PoolAdd, request: Request) -> dict:
    """Add one Pokemon by hand. Works whether or not the format includes it."""
    with connect() as check:
        if check.execute("SELECT 1 FROM season WHERE id = ?", (season_id,)).fetchone() is None:
            raise HTTPException(404, f"No season with id {season_id}")
    try:
        with transaction() as conn:
            return dict(await pool_service.add_one(
                conn, request.app.state.poke, season_id, body.name, body.cost
            ))
    except pool_service.PoolError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PokeApiError as exc:
        raise HTTPException(503, f"Can't reach PokeAPI: {exc}") from exc


@app.post("/seasons/{season_id}/pool/custom", response_model=CustomPoolResult, tags=["pool"],
          dependencies=[Depends(require_season_admin)])
async def build_custom_pool(season_id: int, body: CustomPool, request: Request) -> dict:
    """Build the pool from a dropped-in .txt or .json file instead of a format.

    The escape hatch from Showdown's tiers: a league that drafts from its own
    list — a spreadsheet export, last season's pool, a themed cup — gets the
    same board without having to bend its list into a format.

    Costs in the file are honoured; names without one land unpriced at 0, which
    is the normal case, because pricing is the commissioner's next step.
    """
    with connect() as check:
        if check.execute("SELECT 1 FROM season WHERE id = ?", (season_id,)).fetchone() is None:
            raise HTTPException(404, f"No season with id {season_id}")

    try:
        rows = pool_service.parse_species_list(body.content)
    except pool_service.PoolError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not rows:
        raise HTTPException(400, "No Pokémon found in that file.")

    try:
        # The PokeAPI work is done before the write transaction opens; see the
        # note on build_pool for why that matters.
        resolved = await pool_service.resolve_many(request.app.state.poke, rows)
    except PokeApiError as exc:
        raise HTTPException(503, f"Can't reach PokeAPI: {exc}") from exc

    with transaction() as conn:
        return pool_service.store_custom(
            conn, season_id, body.label, resolved, replace=body.replace
        )


@app.post("/seasons/{season_id}/pool/paste", response_model=PoolPasteResult, tags=["pool"],
          dependencies=[Depends(require_season_admin)])
async def paste_cost_list(season_id: int, body: PoolPaste, request: Request) -> dict:
    """Import the league's existing cost list, prices and all.

    The route out of the spreadsheet. Names that do not resolve come back in
    `unmatched` rather than being guessed at.
    """
    with connect() as check:
        if check.execute("SELECT 1 FROM season WHERE id = ?", (season_id,)).fetchone() is None:
            raise HTTPException(404, f"No season with id {season_id}")

    rows = pool_service.parse_cost_list(body.cost_list)
    if not rows:
        raise HTTPException(400, "No 'Name, cost' rows found. Each line needs a trailing number.")
    try:
        with transaction() as conn:
            return await pool_service.add_many(conn, request.app.state.poke, season_id, rows)
    except PokeApiError as exc:
        raise HTTPException(503, f"Can't reach PokeAPI: {exc}") from exc


@app.get("/seasons/{season_id}/pool", response_model=list[PoolEntryOut], tags=["pool"])
def read_pool(
    season_id: int,
    type: str | None = Query(None, description="filter to one type, e.g. dragon"),
    max_cost: int | None = Query(None, ge=0),
    q: str | None = Query(None, description="search display names"),
    include_banned: bool = False,
    unpriced_only: bool = False,
    limit: int = Query(1000, ge=1, le=2000),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    rows = pool_service.list_pool(
        db, season_id, type_=type, max_cost=max_cost, search=q,
        include_banned=include_banned, unpriced_only=unpriced_only, limit=limit,
    )
    return [dict(row) for row in rows]


@app.get("/seasons/{season_id}/pool/summary", tags=["pool"])
def pool_summary(season_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    return pool_service.summary(db, season_id)


@app.get("/seasons/{season_id}/pool/{entry_id}", response_model=PoolEntryOut, tags=["pool"])
def read_entry(season_id: int, entry_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    row = pool_service.get_entry(db, season_id, entry_id)
    if row is None:
        raise HTTPException(404, "That Pokémon isn't in this season's pool.")
    return dict(row)


@app.post("/seasons/{season_id}/pool/costs", tags=["pool"], dependencies=[Depends(require_season_admin)])
def set_costs(season_id: int, body: CostUpdate) -> dict:
    with transaction() as conn:
        return pool_service.set_costs(conn, season_id, body.costs)


@app.post("/seasons/{season_id}/pool/bans", tags=["pool"], dependencies=[Depends(require_season_admin)])
def set_bans(season_id: int, body: BanUpdate) -> dict:
    with transaction() as conn:
        return pool_service.set_banned(conn, season_id, body.names, body.banned)


# Joining, and the private per-player storage that comes with it. A router
# because those are the only routes in this API with an authentication step,
# and keeping them in one file keeps the public/private boundary legible.
app.include_router(player_routes.router)
app.include_router(draft_routes.router)
app.include_router(replay_routes.router)
# Results and the two leaderboards. Reads are public like the draft board;
# only the commissioner's token writes one.
app.include_router(results_routes.router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


# ----------------------------------------------------------- the board


# The frontend, served from this same app on purpose. Static files off a
# separate dev server would be a different origin, which would mean enabling
# CORS on an API that currently has no authentication on it. Same origin
# instead: the board fetches "/seasons/1/pool" with no base URL and no
# preflight.
#
# Mounted last. A mount matches by prefix and would shadow anything declared
# after it, so every route above keeps priority over /app.
WEB_DIR = WEB

class RevalidatingFiles(StaticFiles):
    """StaticFiles, but the browser has to ask whether its copy is still good.

    Starlette sends `etag` and `last-modified` and no `Cache-Control`, which
    leaves the browser free to guess how long a file stays fresh. It guesses
    from the age of the file, so a module edited a minute ago can be served
    from cache for minutes afterwards without ever asking.

    That is how you end up running half of one version: index.html revalidates
    and brings a new tab button, while app.js comes from cache and knows
    nothing about the tab it names, so clicking it falls through to the board.
    Exactly that happened during a live session.

    `no-cache` does not mean "do not cache" — it means "revalidate before
    using", so the browser still keeps the file and still gets a 304 for it.
    The cost is one conditional request per file per load, on a server that is
    usually on the same machine.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if WEB_DIR.is_dir():
    app.mount("/app", RevalidatingFiles(directory=WEB_DIR, html=True), name="board")

    @app.get("/", include_in_schema=False)
    def board() -> RedirectResponse:
        return RedirectResponse("/app/")

