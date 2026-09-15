"""Pydantic models — the shape of everything crossing the HTTP boundary.

Three kinds, and keeping them separate matters:

  *Create*  what a client may send to make something. No id, no server-set
            fields, so a client cannot invent an id or backdate a created_at.
  *Update*  every field optional, because PATCH means "change these".
            `model_dump(exclude_unset=True)` then gives exactly the fields the
            client actually sent, which is what makes a partial update work.
  *Out*     what the server returns. Declaring it as a response_model means a
            renamed column fails here rather than silently sending null to the
            frontend.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------- seasons


class SeasonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    budget: int = Field(default=100, ge=1, le=10_000)
    roster_size: int = Field(default=8, ge=1, le=24)
    format_key: str | None = Field(default=None, max_length=40)


class SeasonUpdate(BaseModel):
    """Every field optional: this is a PATCH body.

    The budget and the roster size are the rules a draft runs under, so the
    service refuses to change them once one has started. They are still
    accepted here because before that they are ordinary settings, and a league
    set up a week early gets edited.
    """

    name: str | None = Field(default=None, min_length=1, max_length=80)
    budget: int | None = Field(default=None, ge=1, le=10_000)
    roster_size: int | None = Field(default=None, ge=1, le=24)

    @field_validator("name")
    @classmethod
    def strip_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A league needs a name.")
        return cleaned


class SeasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    budget: int
    roster_size: int
    format_key: str | None = None
    tier_snapshot: str | None = None
    pool_label: str | None = None


class LeagueOut(BaseModel):
    """One league on the "your leagues" list.

    Everything past the season's own columns is a count, and they are here so
    the list can be read rather than merely enumerated: two leagues named
    after the same cup are told apart by having 8 teams and a finished draft
    against 0 teams and an empty pool.
    """

    id: int
    name: str
    budget: int
    roster_size: int
    format_key: str | None = None
    pool_label: str | None = None
    created_at: str | None = None

    teams: int = 0
    pool_size: int = 0
    priced: int = Field(default=0, description="Pool entries with a cost above zero")
    draft_status: str = Field(default="setup", description="setup | live | complete")
    picks_made: int = 0
    picks_total: int = Field(default=0, description="Teams times rounds, once both are known")
    matches_played: int = 0
    claimed: bool = Field(default=False, description="Someone holds this league's admin token")
    is_open: bool = Field(default=False, description="Accepting players through its join code")


# ----------------------------------------------------------------- teams


class TeamCreate(BaseModel):
    """A team is the drafting identity. One per drafter, named by them.

    No password and no account, per the design doc — account friction is what
    drove people off the tools we looked at.
    """

    name: str = Field(min_length=1, max_length=60, description="Team name, e.g. 'Sinnoh Slammers'")
    owner: str | None = Field(default=None, max_length=40, description="Who runs it")
    logo_url: str | None = Field(default=None, max_length=500)

    @field_validator("name", "owner")
    @classmethod
    def strip_blank(cls, value: str | None) -> str | None:
        """Trim, and treat whitespace-only as absent rather than storing "   "."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("logo_url")
    @classmethod
    def must_be_http(cls, value: str | None) -> str | None:
        """A logo goes straight into an <img src>, so reject anything else.

        Blocks javascript: and data: URLs, which would otherwise be a stored
        XSS on a page every drafter loads.
        """
        if value in (None, ""):
            return None
        if not re.match(r"^https?://", value):
            raise ValueError("logo_url must start with http:// or https://")
        return value


class TeamUpdate(BaseModel):
    """Every field optional: this is a PATCH body."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    owner: str | None = Field(default=None, max_length=40)
    logo_url: str | None = Field(default=None, max_length=500)
    draft_position: int | None = Field(default=None, ge=1, le=24)

    _strip = field_validator("name", "owner")(TeamCreate.strip_blank.__func__)
    _http = field_validator("logo_url")(TeamCreate.must_be_http.__func__)


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    season_id: int
    name: str
    owner: str | None = None
    logo_url: str | None = None
    # The URL to actually put in an <img src>: the upload endpoint when the
    # team has uploaded a logo, otherwise whatever link they pasted. One field
    # so a client never has to decide between the two.
    logo: str | None = None
    draft_position: int | None = None


class LogoUpload(BaseModel):
    """An image, base64-encoded.

    Posted as JSON rather than multipart so the API keeps one content type and
    needs no extra dependency — the same choice the custom-pool upload makes.
    The browser downscales and re-encodes the image before it gets here, so
    what arrives is usually a few tens of kilobytes.
    """

    data: str = Field(min_length=1, description="base64 image bytes, with or without a data: prefix")


class LogoOut(BaseModel):
    team_id: int
    logo: str
    content_type: str
    bytes: int
    version: int


# ------------------------------------------------------------------ pool


class PoolEntryOut(BaseModel):
    """One Pokemon on the draft board.

    `types` and `stats` are stored as JSON strings because SQLite has no list
    or object type. The validators below decode them, so callers never see the
    storage detail.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    api_name: str
    showdown_id: str
    display_name: str
    cost: int
    types: list[str] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    bst: int | None = None
    sprite_url: str | None = None
    artwork_url: str | None = None
    tier: str | None = None
    banned: bool = False

    @field_validator("types", "stats", mode="before")
    @classmethod
    def decode_json(cls, value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("banned", mode="before")
    @classmethod
    def int_to_bool(cls, value: Any) -> Any:
        return bool(value)


class PoolAdd(BaseModel):
    """Add one Pokemon by hand, outside the format."""

    name: str = Field(min_length=1, max_length=60,
                      description="PokeAPI name ('great-tusk') or Showdown id ('greattusk')")
    cost: int | None = Field(default=None, ge=0,
                             description="Leave unset to keep an existing price")


class PoolPaste(BaseModel):
    """A cost list pasted out of the league spreadsheet."""

    cost_list: str = Field(min_length=1, description="One 'Name, cost' per line")


class CustomPool(BaseModel):
    """A Pokemon list the commissioner dropped in, instead of a format.

    The file is read in the browser and posted as text, so this takes no
    multipart upload and the API keeps one content type.
    """

    content: str = Field(min_length=1, description="The .txt or .json file's contents")
    label: str = Field(default="Custom pool", max_length=80,
                       description="What to call it on the board")
    replace: bool = Field(default=True,
                          description="Clear the existing pool first, rather than adding to it")


class CustomPoolResult(BaseModel):
    label: str
    submitted: int
    added: int
    priced: int
    removed: int = Field(default=0, description="Entries cleared by `replace`")
    unmatched: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Names with no PokeAPI match — fix the spelling and resubmit",
    )


class PoolPasteResult(BaseModel):
    submitted: int
    added: int
    unmatched: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Rows with no PokeAPI match — fix the spelling and resubmit",
    )


class CostUpdate(BaseModel):
    """api_name (or showdown_id) -> point cost."""

    costs: dict[str, int] = Field(min_length=1)

    @field_validator("costs")
    @classmethod
    def non_negative(cls, value: dict[str, int]) -> dict[str, int]:
        bad = [k for k, v in value.items() if v < 0]
        if bad:
            raise ValueError(f"costs cannot be negative: {', '.join(sorted(bad)[:5])}")
        return value


class BanUpdate(BaseModel):
    names: list[str] = Field(min_length=1, description="api_name or showdown_id")
    banned: bool = True


class PoolBuildResult(BaseModel):
    format_key: str
    label: str
    tier_snapshot: str
    added: int
    priced: int
    unresolved: list[str] = Field(
        default_factory=list,
        description="Showdown ids with no PokeAPI match — add to FORM_ALIASES",
    )


# --------------------------------------------------------------- formats


class FormatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    ladder: str = "singles"
    tier_ceiling: str
    species_count: int
    species_banned: int = 0
    showdown_name: str | None = None
    source_sha256: str
    built_at: str


class ItemOut(BaseModel):
    """One held item a player can actually put on a Pokemon."""

    id: str
    name: str
    description: str = ""
    spritenum: int = Field(
        default=-1,
        description="Cell in Showdown's itemicons sheet; -1 when the item has no icon",
    )
    banned: bool = Field(default=False, description="Banned by this format's rules")
    category: str | None = Field(
        default=None, description="berry | mega | z-crystal, for grouping"
    )
    users: list[str] = Field(
        default_factory=list,
        description="The only Pokemon this does anything for, when it is species-locked",
    )


class FormatRules(BaseModel):
    """Everything Showdown enforces for a format beyond the species list.

    The pool already has the species bans applied, so `banned_species` is here
    to explain an absence rather than to be filtered on. The rest — clauses,
    move and ability bans — a draft tool cannot enforce by picking Pokemon, so
    it surfaces them for the commissioner and for a future team validator.
    """

    key: str
    label: str
    showdown_name: str | None = None
    clauses: list[str] = Field(default_factory=list)
    banned_species: list[str] = Field(default_factory=list)
    banned_tiers: list[str] = Field(default_factory=list)
    banned_other: list[str] = Field(default_factory=list)
    complex_bans: list[str] = Field(default_factory=list)
    unbanned: list[str] = Field(default_factory=list)
