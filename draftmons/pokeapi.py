"""The one place that talks to pokeapi.co.

Building a pool is a few hundred GETs against a single host, so the three
things that make that safe — a concurrency cap, a response cache, and turning
a 404 into something callers can catch — belong here rather than being
re-derived at every call site.

The cache is per-instance and in memory. That is enough to make `build_pool`
cheap, because a category's species share forms and the same species is often
asked for twice in one build. It does not survive a restart; the persistent
api_cache table that formats.py's docstring assumes is not in
database_schema.sql yet.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

BASE_URL = "https://pokeapi.co/api/v2/"

# PokeAPI publishes no hard rate limit and asks callers to be considerate. Ten
# in flight builds a gen-9 pool in well under a minute without leaning on them.
MAX_IN_FLIGHT = 10


class PokeApiError(RuntimeError):
    """Any failure talking to PokeAPI."""


class PokeApiNotFound(PokeApiError):
    """PokeAPI has no resource under that name.

    Its own error, not a generic PokeApiError, because a 404 is routine — a
    species whose default form is named differently — while everything else
    means the pool build should stop.
    """


class PokeApiClient:
    """An httpx client plus a semaphore plus a dict.

    Pass an existing AsyncClient to share a connection pool with the rest of
    the app (see the lifespan in main.py); pass nothing and this owns one.
    Only a client it created itself gets closed on `aclose`, so handing in the
    app's shared client cannot close it out from under other requests.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_in_flight: int = MAX_IN_FLIGHT,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            headers={"User-Agent": "draftmons/0.1"},
        )
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._cache: dict[str, Any] = {}

    async def get(self, path: str) -> dict[str, Any]:
        """GET one PokeAPI resource by path, e.g. "generation/generation-ix"."""
        key = path.strip("/")
        if key in self._cache:
            return self._cache[key]

        async with self._semaphore:
            # Re-check: another task may have fetched this while we queued for
            # a slot. Without this, 240 concurrent hydrates can each miss on
            # the same species and fetch it 240 times.
            if key in self._cache:
                return self._cache[key]
            try:
                # An absolute URL, so an injected client needs no base_url set.
                response = await self._client.get(f"{BASE_URL}{key}")
            except httpx.HTTPError as exc:
                raise PokeApiError(f"GET {key} failed: {exc}") from exc

        if response.status_code == 404:
            raise PokeApiNotFound(key)
        if response.status_code >= 400:
            raise PokeApiError(f"GET {key} returned {response.status_code}")

        payload = response.json()
        self._cache[key] = payload
        return payload

    async def pokemon(self, name: str) -> dict[str, Any]:
        """A form: stats, types, sprites."""
        return await self.get(f"pokemon/{name}")

    async def species(self, name: str) -> dict[str, Any]:
        """A species: the is_legendary and is_mythical flags live only here."""
        return await self.get(f"pokemon-species/{name}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PokeApiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
