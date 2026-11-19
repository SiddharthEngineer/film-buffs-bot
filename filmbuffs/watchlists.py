"""Letterboxd watchlists of linked members (Phase 22), for /watchlist show.

A member's watchlist is read from their public watchlist pages
(`letterboxd.com/<username>/watchlist/page/N/`, allowed by Letterboxd's
robots.txt; 28 films a page as "Title (Year)"), matched to TMDB with the same
cached TitleResolver the export sync uses, and stored in `watchlist_items`
(source 'letterboxd'), replacing what was there. It's read when a member runs
/letterboxd link, by /watchlist show if older than FRESH_FOR, and for every
linked member once a day (refresh_all, from cogs/letterboxd.py), pages ~1 s
apart, with the bot's honest User-Agent. Nothing is posted and no #pre-watch
posts are created.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp

from .db import utcnow
from .letterboxd import LetterboxdError, UserNotFound

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

WATCHLIST_URL = "https://letterboxd.com/{username}/watchlist/"
PAGE_URL = "https://letterboxd.com/{username}/watchlist/page/{page}/"
SOURCE = "letterboxd"
FRESH_FOR = timedelta(hours=6)
MEMBER_PAUSE = 5.0          # seconds between members in the daily refresh
MAX_PAGES = 60              # 1,680 films
PAGE_DELAY = 1.0            # seconds between pages

ITEM_RE = re.compile(r'\sdata-item-name="([^"]*)"')
PAGE_RE = re.compile(r'/watchlist/page/(\d+)/')
NAME_RE = re.compile(r"^(.*?)(?: \((\d{4})\))?$")


def parse_name(name: str) -> tuple[str, int | None]:
    """'Black Bag (2025)' → ('Black Bag', 2025); no year → (name, None)."""
    m = NAME_RE.match(name.strip())
    return m.group(1), int(m.group(2)) if m.group(2) else None


def parse_page(body: str) -> tuple[list[tuple[str, int | None]], int]:
    """(films on the page, highest page number linked from it; 1 if none)."""
    films = list(dict.fromkeys(parse_name(html.unescape(n)) for n in ITEM_RE.findall(body)))
    pages = [int(p) for p in PAGE_RE.findall(body)]
    return films, max(pages, default=1)


async def fetch_page(session: aiohttp.ClientSession, username: str, page: int,
                     timeout: float = 20) -> str:
    url = (WATCHLIST_URL.format(username=username) if page == 1
           else PAGE_URL.format(username=username, page=page))
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 404:
                raise UserNotFound(f"no Letterboxd user {username!r}", 404)
            if r.status != 200:
                raise LetterboxdError(f"{url} returned HTTP {r.status}", r.status)
            return await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise LetterboxdError(f"{url} failed: {e!r}") from e


async def fetch_watchlist(session, username: str, delay: float = PAGE_DELAY,
                          max_pages: int = MAX_PAGES) -> list[tuple[str, int | None]]:
    films, last = parse_page(await fetch_page(session, username, 1))
    page = 2
    while page <= min(last, max_pages):
        await asyncio.sleep(delay)
        more, seen_last = parse_page(await fetch_page(session, username, page))
        films += more
        last = max(last, seen_last)
        page += 1
    return list(dict.fromkeys(films))


@dataclass
class Refresh:
    ok: bool                        # the stored watchlist is current
    error: str | None = None        # why it couldn't be read (stored data may be older)
    films: int = 0
    unmatched: list[str] = field(default_factory=list)


class WatchlistService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def fresh(self, user_id: int, username: str, now: datetime | None = None) -> bool:
        imp, _, _ = await self.bot.db.watchlist_summary(user_id)
        if imp is None or imp["source"] != SOURCE:
            return False
        if (await self.bot.db.watchlist_account(user_id) or "") != username.lower():
            return False
        now = now or datetime.now(timezone.utc)
        return now - datetime.fromisoformat(imp["imported_at"]) < FRESH_FOR

    async def refresh(self, user_id: int, username: str, force: bool = False) -> Refresh:
        """Re-read the member's watchlist from Letterboxd unless it's fresh."""
        async with self._locks[user_id]:
            if not force and await self.fresh(user_id, username):
                return Refresh(True)
            try:
                names = await fetch_watchlist(self.bot.session, username)
            except UserNotFound:
                return Refresh(False, f"Letterboxd has no user **{username}**")
            except LetterboxdError as e:
                log.warning("Reading the watchlist of %s failed: %s", username, e)
                return Refresh(False, f"Letterboxd didn't answer for **{username}**")
            from .backfill import TitleResolver
            resolver = TitleResolver(self.bot.db, self.bot.tmdb, concurrency=8)
            results = await asyncio.gather(*(resolver.resolve(t, y) for t, y in names),
                                           return_exceptions=True)
            items, unmatched = [], []
            for (title, year), res in zip(names, results):
                tid = None if isinstance(res, BaseException) else res[0]
                if tid:
                    items.append((tid, title, year))
                else:
                    unmatched.append(f"{title} ({year})" if year else title)
            await self.bot.db.replace_watchlist(user_id, items, len(names), unmatched,
                                                source=SOURCE, username=username)
            log.info("Read the watchlist of %s (%s): %d films, %d matched, %d TMDB lookups",
                     username, user_id, len(names), len(items), resolver.lookups)
            return Refresh(True, None, len(names), unmatched)

    async def refresh_all(self, pause: float = MEMBER_PAUSE) -> tuple[int, int]:
        """Re-read every linked member's watchlist (the daily refresh). Lists read in
        the last FRESH_FOR are left alone. Returns (read or fresh, failed)."""
        ok = failed = 0
        for i, link in enumerate(await self.bot.db.all_links()):
            if i:
                await asyncio.sleep(pause)
            try:
                res = await self.refresh(link["discord_user_id"], link["username"])
            except Exception:
                log.exception("Daily watchlist read for %s failed", link["username"])
                res = Refresh(False, "error")
            ok, failed = (ok + 1, failed) if res.ok else (ok, failed + 1)
        await self.bot.db.set_meta("watchlists_last_run", utcnow())
        log.info("Daily watchlist refresh: %d current, %d failed", ok, failed)
        return ok, failed
