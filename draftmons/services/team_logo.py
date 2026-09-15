"""Team logos: validating an uploaded image, and storing it.

Uploads are the point. A logo pasted as a URL only shows for everyone if the
host it lives on stays up and allows hotlinking, which is not something a
league can rely on; bytes in the league's own database show for everyone for as
long as the league exists.

The validation here is the interesting part. The browser downscales and
re-encodes through a canvas before uploading, which already normalises the
format — but the server cannot assume the browser was the one that called it,
so everything is re-checked:

  * the magic bytes, not the declared content type. A client can claim
    "image/png" about anything, and the declared type is what a browser trusts
    when deciding how to handle the response.
  * the size, because the row is served to every viewer of the draft board.
  * SVG is rejected outright. It is an image to a designer and a script host to
    a browser, and an <svg> served from this origin could run JavaScript with
    the league's cookies.
"""

from __future__ import annotations

import sqlite3

# 512 KB. The browser sends a 256px PNG, which is tens of KB; this is the
# ceiling for anything that did not come from the browser.
MAX_BYTES = 512 * 1024

# Magic-byte prefixes for the formats worth accepting. Every one of these is a
# raster format a browser renders without executing anything.
SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


class LogoError(ValueError):
    """The upload was not something safe to serve back. Message is user-facing."""


def sniff(data: bytes) -> str:
    """The real content type of these bytes, or raise.

    Deliberately ignores whatever the caller said the type was: the declared
    type is what the browser acts on, so it has to be one this function
    decided, not one the uploader chose.
    """
    if not data:
        raise LogoError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise LogoError(
            f"That image is {len(data) // 1024} KB; the limit is {MAX_BYTES // 1024} KB."
        )

    for prefix, content_type in SIGNATURES:
        if data.startswith(prefix):
            return content_type

    # WebP is RIFF....WEBP, so the marker is not at offset 0.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    head = data.lstrip()[:200].lower()
    if head.startswith(b"<svg") or b"<svg" in head:
        raise LogoError(
            "SVG logos are not accepted: an SVG can carry scripts. "
            "Save it as a PNG and upload that."
        )
    raise LogoError("That does not look like a PNG, JPEG, GIF or WebP image.")


def store(conn: sqlite3.Connection, team_id: int, data: bytes) -> dict[str, object]:
    """Replace a team's logo. Returns what was stored."""
    content_type = sniff(data)
    conn.execute(
        "INSERT INTO team_logo (team_id, content_type, image, version) "
        "VALUES (?, ?, ?, 1) "
        "ON CONFLICT(team_id) DO UPDATE SET "
        "  content_type = excluded.content_type, image = excluded.image, "
        # Bumped rather than reset, so every replacement gets a URL no cache
        # has seen before.
        "  version = team_logo.version + 1, updated_at = datetime('now')",
        (team_id, content_type, data),
    )
    row = conn.execute(
        "SELECT content_type, version, LENGTH(image) AS bytes FROM team_logo "
        "WHERE team_id = ?", (team_id,),
    ).fetchone()
    return {"content_type": row["content_type"], "version": row["version"],
            "bytes": row["bytes"]}


def fetch(conn: sqlite3.Connection, team_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT content_type, image, version FROM team_logo WHERE team_id = ?",
        (team_id,),
    ).fetchone()


def remove(conn: sqlite3.Connection, team_id: int) -> bool:
    return conn.execute(
        "DELETE FROM team_logo WHERE team_id = ?", (team_id,)
    ).rowcount > 0


def versions(conn: sqlite3.Connection, season_id: int) -> dict[int, int]:
    """{team_id: version} for a season, to build logo URLs without the bytes."""
    return {
        row["team_id"]: row["version"]
        for row in conn.execute(
            "SELECT l.team_id, l.version FROM team_logo l "
            "JOIN team t ON t.id = l.team_id WHERE t.season_id = ?",
            (season_id,),
        )
    }


def decorate(conn: sqlite3.Connection, season_id: int, rows) -> list[dict]:
    """Team rows as dicts, each with the `logo` URL filled in.

    One query for the whole season's versions rather than one per team: a
    league is a dozen teams, but this runs behind the draft board, which
    everyone refreshes.
    """
    known = versions(conn, season_id)
    out = []
    for row in rows:
        record = dict(row)
        record["logo"] = url_for(
            season_id, record["id"], known.get(record["id"]), record.get("logo_url")
        )
        out.append(record)
    return out


def url_for(season_id: int, team_id: int, version: int | None, logo_url: str | None) -> str | None:
    """The single URL a client should put in an <img src>.

    An upload wins over a pasted link when a team has both, because uploading
    is the more deliberate act — and because the upload is the one guaranteed
    to still resolve next season.
    """
    if version is not None:
        return f"/seasons/{season_id}/teams/{team_id}/logo?v={version}"
    return logo_url or None
