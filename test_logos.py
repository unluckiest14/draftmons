"""Tests for team logos.

The requirement is "it shows up for all users", so the tests that matter are
the ones about visibility: an uploaded logo is served to a caller holding no
token at all, and it reaches every view a team appears in.

The rest is refusals. The bytes are served back from this origin with a
content type this app chose, which makes an upload endpoint a place where
getting validation wrong has consequences beyond a broken picture.
"""

from __future__ import annotations

import base64
import importlib
import struct
import sys
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import team_logo  # noqa: E402


def png(width: int = 8, height: int = 8) -> bytes:
    """A real, decodable PNG — so the magic-byte check is exercised honestly."""
    raw = b"".join(b"\x00" + b"\xc8\x28\x3c" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture()
def league(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_DB", str(tmp_path / "logo.db"))
    for mod in ("poke_db", "team_logo", "team_service", "player_service",
                "player_routes", "draft_service", "draft_routes", "main"):
        sys.modules.pop(mod, None)
    main = importlib.import_module("main")

    with TestClient(main.app) as client:
        sid = client.post("/seasons", json={
            "name": "L", "budget": 40, "roster_size": 2,
        }).json()["id"]
        invite = client.post(f"/seasons/{sid}/invite", json={}).json()
        joined = client.post(f"/join/{invite['join_code']}",
                             json={"team_name": "Sinnoh Slammers"}).json()
        # Uploading and removing a logo is the owning player's right, so every
        # request in these tests is made as that player by default. A test that
        # cares about the unauthenticated case clears the header per request.
        client.headers["Authorization"] = f"Bearer {joined['token']}"
        # Deleting a team is a commissioner act, not the player's, so the few
        # tests that need it reach for this rather than the default header.
        client.admin_headers = {"Authorization": f"Bearer {invite['admin_token']}"}
        yield client, sid, joined["team"]["id"]


def put(client, sid, tid, data):
    return client.put(f"/seasons/{sid}/teams/{tid}/logo", json={"data": data})


# ------------------------------------------------------------ the bytes


def test_sniffing_reads_the_bytes_not_the_claim():
    assert team_logo.sniff(png()) == "image/png"
    assert team_logo.sniff(b"\xff\xd8\xff" + b"x" * 40) == "image/jpeg"
    assert team_logo.sniff(b"GIF89a" + b"x" * 40) == "image/gif"
    assert team_logo.sniff(b"RIFF1234WEBP" + b"x" * 40) == "image/webp"


def test_an_svg_is_refused_because_it_can_carry_scripts():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(team_logo.LogoError, match="SVG"):
        team_logo.sniff(svg)


def test_a_non_image_is_refused():
    with pytest.raises(team_logo.LogoError):
        team_logo.sniff(b"just some text, honestly")


def test_an_oversized_image_is_refused():
    with pytest.raises(team_logo.LogoError, match="limit"):
        team_logo.sniff(b"\x89PNG\r\n\x1a\n" + b"x" * (team_logo.MAX_BYTES + 1))


# --------------------------------------------------------- the round trip


def test_an_uploaded_logo_comes_back_byte_for_byte(league):
    client, sid, tid = league
    image = png()
    body = put(client, sid, tid, b64(image)).json()
    assert body["content_type"] == "image/png"

    served = client.get(f"/seasons/{sid}/teams/{tid}/logo")
    assert served.status_code == 200
    assert served.content == image
    assert served.headers["content-type"] == "image/png"


def test_the_served_logo_cannot_be_mime_sniffed_into_something_else(league):
    """The type was decided here, so the browser must not second-guess it."""
    client, sid, tid = league
    put(client, sid, tid, b64(png()))
    headers = client.get(f"/seasons/{sid}/teams/{tid}/logo").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-disposition"] == "inline"


def test_a_data_url_from_the_browser_is_accepted(league):
    """FileReader hands back "data:image/png;base64,…" — take it as-is."""
    client, sid, tid = league
    assert put(client, sid, tid, f"data:image/png;base64,{b64(png())}").status_code == 200


def test_bad_base64_is_a_400_not_a_crash(league):
    client, sid, tid = league
    assert put(client, sid, tid, "!!!not base64!!!").status_code == 400


def test_an_svg_upload_is_a_400_with_a_reason(league):
    client, sid, tid = league
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    refused = put(client, sid, tid, b64(svg))
    assert refused.status_code == 400 and "SVG" in refused.json()["detail"]


# ------------------------------------------------- visible to everyone


def test_the_logo_is_served_to_a_caller_with_no_token(league):
    """The point of the feature: everyone sees it, not just the uploader."""
    client, sid, tid = league
    put(client, sid, tid, b64(png()))
    # No Authorization header anywhere in this request.
    assert client.get(f"/seasons/{sid}/teams/{tid}/logo").status_code == 200


def test_every_view_of_a_team_carries_the_logo_url(league):
    client, sid, tid = league
    put(client, sid, tid, b64(png()))
    expected = f"/seasons/{sid}/teams/{tid}/logo?v=1"

    assert client.get(f"/seasons/{sid}/teams").json()[0]["logo"] == expected
    assert client.get(f"/seasons/{sid}/teams/{tid}").json()["logo"] == expected
    board = client.get(f"/seasons/{sid}/draft/board").json()
    assert board["teams"][0]["logo"] == expected


def test_replacing_a_logo_changes_its_url(league):
    """A cached copy of the old one must not be what everyone keeps seeing."""
    client, sid, tid = league
    first = put(client, sid, tid, b64(png(8, 8))).json()
    second = put(client, sid, tid, b64(png(16, 16))).json()

    assert second["version"] == first["version"] + 1
    assert second["logo"] != first["logo"]
    assert client.get(f"/seasons/{sid}/teams").json()[0]["logo"] == second["logo"]


def test_a_team_with_no_logo_reports_none(league):
    client, sid, tid = league
    assert client.get(f"/seasons/{sid}/teams").json()[0]["logo"] is None
    assert client.get(f"/seasons/{sid}/teams/{tid}/logo").status_code == 404


def test_an_upload_wins_over_a_pasted_link(league):
    client, sid, tid = league
    client.patch(f"/seasons/{sid}/teams/{tid}",
                 json={"logo_url": "https://example.com/old.png"})
    assert client.get(f"/seasons/{sid}/teams").json()[0]["logo"] \
        == "https://example.com/old.png"

    put(client, sid, tid, b64(png()))
    assert client.get(f"/seasons/{sid}/teams").json()[0]["logo"] \
        == f"/seasons/{sid}/teams/{tid}/logo?v=1"


def test_removing_an_upload_falls_back_to_the_pasted_link(league):
    client, sid, tid = league
    client.patch(f"/seasons/{sid}/teams/{tid}",
                 json={"logo_url": "https://example.com/old.png"})
    put(client, sid, tid, b64(png()))

    assert client.delete(f"/seasons/{sid}/teams/{tid}/logo").status_code == 204
    assert client.get(f"/seasons/{sid}/teams").json()[0]["logo"] \
        == "https://example.com/old.png"


def test_uploading_to_someone_elses_team_is_refused_before_it_is_looked_up(league):
    """403, not 404, and deliberately so.

    The owner check runs before the handler, so a caller learns "not yours"
    without learning whether that team exists — team ids would otherwise be
    enumerable by anyone with any player token. Serving a logo stays public and
    still 404s, because a missing image is not a secret.
    """
    client, sid, _ = league
    assert put(client, sid, 999, b64(png())).status_code == 403
    assert client.get(f"/seasons/{sid}/teams/999/logo").status_code == 404


def test_deleting_a_team_takes_its_logo_with_it(league):
    client, sid, tid = league
    put(client, sid, tid, b64(png()))
    client.delete(f"/seasons/{sid}/teams/{tid}", headers=client.admin_headers)
    assert client.get(f"/seasons/{sid}/teams/{tid}/logo").status_code == 404
