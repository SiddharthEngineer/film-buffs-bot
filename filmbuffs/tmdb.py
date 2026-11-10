"""Small TMDB client: search, movie details (with directors), and caching."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

import aiohttp

API = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

# Autocomplete choice values carry this prefix, so a typed title that happens
# to be all digits (e.g. "1917") is never mistaken for a TMDB ID.
CHOICE_PREFIX = "tmdb:"
_URL_RE = re.compile(r"themoviedb\.org/movie/(\d+)")


def parse_choice(value: str) -> int | None:
    """Return the TMDB ID from an autocomplete value or a TMDB movie URL, else None."""
    value = (value or "").strip()
    if value.startswith(CHOICE_PREFIX) and value[len(CHOICE_PREFIX):].isdigit():
        return int(value[len(CHOICE_PREFIX):])
    m = _URL_RE.search(value)
    return int(m.group(1)) if m else None


def year_of(date: str | None) -> int | None:
    return int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None


@dataclass
class Movie:
    id: int
    title: str
    year: int | None
    runtime: int | None
    overview: str
    poster_path: str | None
    genres: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> "Movie":
        crew = (d.get("credits") or {}).get("crew", [])
        return cls(
            id=d["id"],
            title=d.get("title") or d.get("original_title") or "Untitled",
            year=year_of(d.get("release_date")),
            runtime=d.get("runtime") or None,
            overview=d.get("overview") or "",
            poster_path=d.get("poster_path"),
            genres=[g["name"] for g in d.get("genres", [])],
            directors=[c["name"] for c in crew if c.get("job") == "Director"],
        )

    @property
    def display(self) -> str:
        """`Title (Year)`, used for post titles and poll options."""
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def tmdb_url(self) -> str:
        return f"https://www.themoviedb.org/movie/{self.id}"

    @property
    def letterboxd_url(self) -> str:
        return f"https://letterboxd.com/tmdb/{self.id}"

    @property
    def poster_url(self) -> str | None:
        return f"{IMAGE_BASE}{self.poster_path}" if self.poster_path else None

    @property
    def runtime_text(self) -> str | None:
        if not self.runtime:
            return None
        h, m = divmod(self.runtime, 60)
        return f"{h}h {m}m" if h else f"{m}m"


def choice_label(title: str, year: int | None, directors: list[str] | None) -> str:
    """Autocomplete label: `Title (Year) — Director`, max 100 chars."""
    label = f"{title} ({year})" if year else title
    if directors:
        label += " — " + ", ".join(directors[:2])
    return label if len(label) <= 100 else label[:99] + "…"


class TMDBError(Exception):
    pass


class TMDB:
    def __init__(self, session: aiohttp.ClientSession, key: str, cache_ttl: int = 3600):
        self.session = session
        self.params: dict[str, str] = {"language": "en-US"}
        self.headers: dict[str, str] = {"Accept": "application/json"}
        if key.startswith("eyJ"):  # v4 read-access token
            self.headers["Authorization"] = f"Bearer {key}"
        else:                      # v3 API key
            self.params["api_key"] = key
        self.cache_ttl = cache_ttl
        self._cache: dict[int, tuple[float, Movie | None]] = {}

    async def _get(self, path: str, timeout: float = 10, **params) -> dict | None:
        try:
            async with self.session.get(
                f"{API}{path}", params={**self.params, **params}, headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                if r.status == 404:
                    return None
                if r.status != 200:
                    raise TMDBError(f"TMDB {path} returned HTTP {r.status}")
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise TMDBError(f"TMDB {path} failed: {e!r}") from e

    async def search(self, query: str, year: int | None = None,
                     timeout: float = 10) -> list[dict]:
        params = {"query": query, "include_adult": "false"}
        if year:
            params["primary_release_year"] = str(year)
        data = await self._get("/search/movie", timeout=timeout, **params)
        return (data or {}).get("results", [])

    async def movie(self, tmdb_id: int, timeout: float = 10) -> Movie | None:
        """Movie details with directors, or None if TMDB has no such movie."""
        hit = self._cache.get(tmdb_id)
        if hit and time.monotonic() - hit[0] < self.cache_ttl:
            return hit[1]
        data = await self._get(f"/movie/{tmdb_id}", timeout=timeout,
                               append_to_response="credits")
        movie = Movie.from_api(data) if data else None
        self._cache[tmdb_id] = (time.monotonic(), movie)
        if len(self._cache) > 2000:  # keep memory bounded
            oldest = sorted(self._cache, key=lambda k: self._cache[k][0])[:500]
            for k in oldest:
                del self._cache[k]
        return movie

    async def autocomplete(self, query: str, limit: int = 10,
                           budget: float = 2.2) -> list[tuple[str, str]]:
        """(label, value) pairs for Discord autocomplete, within Discord's 3 s deadline.

        Directors need a details call per result; those run in parallel and any
        that are not back within the time budget are shown without a director.
        """
        start = time.monotonic()
        results = (await self.search(query, timeout=budget))[:limit]
        remaining = max(0.1, budget - (time.monotonic() - start))
        tasks = {r["id"]: asyncio.create_task(self.movie(r["id"], timeout=remaining))
                 for r in results}
        if tasks:
            await asyncio.wait(tasks.values(), timeout=remaining)
        out = []
        for r in results:
            t = tasks[r["id"]]
            directors = None
            if t.done() and not t.cancelled() and t.exception() is None and t.result():
                directors = t.result().directors
            elif not t.done():
                t.cancel()
            out.append((choice_label(r.get("title") or r.get("original_title", "?"),
                                     year_of(r.get("release_date")), directors),
                        f"{CHOICE_PREFIX}{r['id']}"))
        return out
