"""Movie posts in the #movies forum. Shared by /addmovie, Letterboxd and backfill."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

import discord

from .tags import compose_tag_keys, genre_tag_keys
from .tmdb import Movie

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

EMBED_COLOR = 0xE5A00D
VOTE_EMOJI = "👍"
SPOILER_FIELD = "Spoiler line"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"


def build_embed(movie: Movie, added_by: discord.abc.User | int | None, pitch: str | None,
                spoiler_line: str | None = None) -> discord.Embed:
    e = discord.Embed(title=movie.display, url=movie.tmdb_url,
                      description=truncate(movie.overview, 600) or "*No synopsis on TMDB.*",
                      color=EMBED_COLOR)
    if movie.poster_url:
        e.set_thumbnail(url=movie.poster_url)
    e.add_field(name="Year", value=str(movie.year or "—"), inline=True)
    e.add_field(name="Runtime", value=movie.runtime_text or "—", inline=True)
    e.add_field(name="Director" if len(movie.directors) <= 1 else "Directors",
                value=", ".join(movie.directors) or "—", inline=True)
    e.add_field(name="Genres", value=", ".join(movie.genres) or "—", inline=False)
    e.add_field(name="Links",
                value=f"[TMDB]({movie.tmdb_url}) · [Letterboxd]({movie.letterboxd_url})",
                inline=False)
    if added_by is not None:
        uid = added_by if isinstance(added_by, int) else added_by.id
        e.add_field(name="Added by", value=f"<@{uid}>", inline=True)
    if pitch:
        e.add_field(name="Pitch", value=truncate(pitch, 1024), inline=False)
    if spoiler_line:
        e.add_field(name=SPOILER_FIELD, value=spoiler_line, inline=False)
    e.set_footer(text=f"{VOTE_EMOJI} this message to vote for it in the weekly pick")
    return e


class MovieService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def forum(self) -> discord.ForumChannel:
        ch = self.bot.get_channel(self.bot.config.channels["movies"])
        if not isinstance(ch, discord.ForumChannel):
            raise RuntimeError("#movies forum not found")
        return ch

    async def find_post(self, tmdb_id: int) -> discord.Thread | None:
        """The existing post for a movie. Forgets posts that were deleted by hand."""
        row = await self.bot.db.get_movie(tmdb_id)
        if row is None:
            return None
        thread = self.bot.get_channel(row["thread_id"])
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(row["thread_id"])
            except discord.NotFound:
                log.warning("Post for TMDB %s (thread %s) was deleted; forgetting it",
                            tmdb_id, row["thread_id"])
                await self.bot.db.delete_movie(tmdb_id)
                return None
        return thread  # type: ignore[return-value]

    def applied_tags(self, status: str, flags: list[str], genres: list[str]) -> list[discord.ForumTag]:
        keys = compose_tag_keys(status, flags, genres, self.bot.config.max_genre_tags)
        tags = []
        for k in keys:
            tag = self.forum.get_tag(self.bot.config.tags[k].id)
            if tag is not None:
                tags.append(tag)
            else:
                log.error("Forum tag %s (%s) is missing; re-run setup_server.py",
                          k, self.bot.config.tags[k].id)
        return tags

    async def ensure_post(self, movie: Movie, *, added_by: discord.abc.User | int | None,
                          pitch: str | None, status: str, source: str,
                          ) -> tuple[discord.Thread, bool]:
        """Return (post, created). Creates the post if the movie has none yet."""
        async with self._locks[movie.id]:
            existing = await self.find_post(movie.id)
            if existing is not None:
                return existing, False

            genres = genre_tag_keys(movie.genres, self.bot.config.genre_map)
            flags: list[str] = []
            created = await self.forum.create_thread(
                name=truncate(movie.display, 100),
                embed=build_embed(movie, added_by, pitch),
                applied_tags=self.applied_tags(status, flags, genres),
                reason=f"Movie post ({source})",
            )
            thread, message = created.thread, created.message
            uid = added_by if isinstance(added_by, int) or added_by is None else added_by.id
            await self.bot.db.insert_movie(
                tmdb_id=movie.id, thread_id=thread.id, starter_message_id=message.id,
                title=movie.title, year=movie.year, status=status, flags=flags,
                genre_tags=genres, added_by=uid, pitch=pitch, source=source)
            try:
                await message.add_reaction(VOTE_EMOJI)
            except discord.HTTPException:
                log.exception("Could not add vote reaction to %s", thread.id)
            log.info("Created post %s for TMDB %s (%s) via %s",
                     thread.id, movie.id, movie.display, source)
            return thread, True

    async def retag(self, tmdb_id: int) -> None:
        """Re-apply tags from the DB row (after a status or flag change)."""
        row = await self.bot.db.get_movie(tmdb_id)
        thread = await self.find_post(tmdb_id)
        if row is None or thread is None:
            return
        tags = self.applied_tags(row["status"], json.loads(row["flags"]),
                                 json.loads(row["genre_tags"]))
        await thread.edit(applied_tags=tags, reason="Movie status change")
