"""Movie posts in the #pre-watch forum (spoiler-free). Shared by /addmovie, Letterboxd and backfill."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

import discord

from .availability import FIELD_NAME as WATCH_FIELD, JUSTWATCH_CREDIT, Availability, card_line
from .cards import CARD_VERSION
from .tags import compose_tag_keys, genre_tag_keys
from .tmdb import Movie

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

EMBED_COLOR = 0xE5A00D
# Field added by the old (pre-3R) spoiler-line design; removed from cards on refresh.
LEGACY_SPOILER_FIELD = "Spoiler line"
# Last field on every info card: the no-spoilers reminder, linking the #post-watch post.
REMINDER_NAME = "🚫 No spoilers in this post"
# Field the pre-Phase 18 /addmovie and /nominate `pitch:` added; dropped on refresh.
PITCH_NAME = "Pitch"


def reminder_value(spoiler_url: str | None) -> str:
    # Only missing for a moment while a new movie's #post-watch post is created.
    text = "Already seen it? Discussion in #post-watch"
    return f"[{text}]({spoiler_url})" if spoiler_url else f"{text}."


def set_reminder(e: discord.Embed, spoiler_url: str | None) -> discord.Embed:
    """Put the no-spoilers reminder last on the card (replacing any older copy)."""
    for i in reversed([i for i, f in enumerate(e.fields)
                       if f.name in (REMINDER_NAME, LEGACY_SPOILER_FIELD, PITCH_NAME)]):
        e.remove_field(i)
    e.add_field(name=REMINDER_NAME, value=reminder_value(spoiler_url), inline=False)
    return e


def set_availability(e: discord.Embed, av: Availability | None,
                     spoiler_url: str | None = None) -> discord.Embed:
    """Put the "Where to watch (US)" field just above the reminder (or drop it), and
    credit JustWatch in the footer whenever the field is shown (TMDB's terms)."""
    for i in reversed([i for i, f in enumerate(e.fields) if f.name == WATCH_FIELD]):
        e.remove_field(i)
    text = card_line(av)
    if text:
        e.add_field(name=WATCH_FIELD, value=text, inline=False)
    # The footer used to say "👍 this message to vote for it"; 👍 no longer count.
    if text:
        e.set_footer(text=JUSTWATCH_CREDIT)
    else:
        e.remove_footer()
    return set_reminder(e, spoiler_url)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"


def added_by_text(added_by: discord.abc.User | int | str) -> str:
    """Plain name (Phase 19): `<@id>` in an embed shows as a raw ID on phones that
    haven't loaded the member. A bare ID (no name known) still falls back to it."""
    if isinstance(added_by, str):
        return added_by
    if isinstance(added_by, int):
        return f"<@{added_by}>"
    return discord.utils.escape_markdown(added_by.display_name)


def set_added_by(e: discord.Embed, name: str) -> discord.Embed:
    for i, f in enumerate(e.fields):
        if f.name == "Added by":
            e.set_field_at(i, name=f.name, value=name, inline=f.inline)
    return e


def build_embed(movie: Movie, added_by: discord.abc.User | int | str | None,
                availability: Availability | None = None) -> discord.Embed:
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
        e.add_field(name="Added by", value=added_by_text(added_by), inline=True)
    return set_availability(e, availability)


class MovieService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def forum(self) -> discord.ForumChannel:
        ch = self.bot.get_channel(self.bot.config.channels["movies"])
        if not isinstance(ch, discord.ForumChannel):
            raise RuntimeError("#pre-watch forum not found")
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
                          status: str, source: str) -> tuple[discord.Thread, bool]:
        """Return (post, created). Creates the post if the movie has none yet, and
        its #post-watch post right after (Phase 18: every movie has both)."""
        thread, created = await self._ensure_post(movie, added_by=added_by, status=status,
                                                  source=source)
        if created:
            spoilers = getattr(self.bot, "spoilers", None)
            if spoilers is not None:
                try:
                    await spoilers.open(movie.id)   # also links it on the card
                except (discord.HTTPException, LookupError, RuntimeError):
                    # The startup backfill (create_missing_posts) retries it.
                    log.exception("Could not create the #post-watch post for TMDB %s", movie.id)
        return thread, created

    async def _ensure_post(self, movie: Movie, *, added_by: discord.abc.User | int | None,
                           status: str, source: str) -> tuple[discord.Thread, bool]:
        async with self._locks[movie.id]:
            existing = await self.find_post(movie.id)
            if existing is not None:
                return existing, False

            genres = genre_tag_keys(movie.genres, self.bot.config.genre_map)
            # In Theaters / Streaming up front, so a new post needs no later edit.
            # If TMDB fails, the daily availability check fills them in.
            svc = getattr(self.bot, "availability", None)
            av = await svc.for_new_post(movie.id) if svc else None
            flags: list[str] = av.flags if av else []
            shown = added_by
            if isinstance(added_by, int):
                from .names import member_name
                shown = await member_name(self.bot, added_by)
            created = await self.forum.create_thread(
                name=truncate(movie.display, 100),
                embed=build_embed(movie, shown, av),
                applied_tags=self.applied_tags(status, flags, genres),
                reason=f"Movie post ({source})",
            )
            thread, message = created.thread, created.message
            uid = added_by if isinstance(added_by, int) or added_by is None else added_by.id
            await self.bot.db.insert_movie(
                tmdb_id=movie.id, thread_id=thread.id, starter_message_id=message.id,
                title=movie.title, year=movie.year, status=status, flags=flags,
                genre_tags=genres, added_by=uid, pitch=None, source=source,
                poster_url=movie.poster_url or "")
            await self.bot.db.set_card_version(movie.id, CARD_VERSION)
            if av is not None:
                await self.bot.db.set_availability(movie.id, av.to_json(), flags)
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
        if {t.id for t in tags} == {t.id for t in thread.applied_tags}:
            return
        # Archived posts must be un-archived in the same edit, or Discord refuses it.
        extra = {"archived": False} if thread.archived else {}
        await thread.edit(applied_tags=tags, reason="Movie status change", **extra)

    def spoiler_url(self, row) -> str | None:
        if not row["spoiler_thread_id"]:
            return None
        return f"https://discord.com/channels/{self.bot.config.guild_id}/{row['spoiler_thread_id']}"

    async def update_card(self, tmdb_id: int) -> None:
        """Re-render the info card: where to watch, the no-spoilers reminder with
        its #post-watch link (drops the old button and legacy fields)."""
        row = await self.bot.db.get_movie(tmdb_id)
        thread = await self.find_post(tmdb_id)
        if row is None or thread is None:
            return
        was_archived = thread.archived
        if was_archived:
            await thread.edit(archived=False, reason="Updating the movie's info card")
        starter = await thread.fetch_message(row["starter_message_id"])
        kwargs: dict = {"view": None}   # removes the pre-Phase 18 button
        if starter.embeds:
            e = starter.embeds[0]
            if row["added_by"]:
                from .names import member_name
                set_added_by(e, await member_name(self.bot, row["added_by"]))
            kwargs["embed"] = set_availability(e, Availability.from_json(row["availability"]),
                                               self.spoiler_url(row))
        await starter.edit(**kwargs)
        await self.bot.db.set_card_version(tmdb_id, CARD_VERSION)
        if was_archived:
            # Put it back: guilds have a cap on active threads (DISCORD_NOTES), and
            # bulk refreshes would otherwise un-archive every post at once.
            await thread.edit(archived=True, reason="Info card updated")

    async def refresh_old_cards(self) -> None:
        """One-off per CARD_VERSION bump: bring existing posts' cards and tags up to date."""
        rows = await self.bot.db.movies_needing_card(CARD_VERSION)
        if rows:
            log.info("Refreshing %d info card(s) to version %d", len(rows), CARD_VERSION)
        for row in rows:
            try:
                await self.update_card(row["tmdb_id"])
                await self.retag(row["tmdb_id"])
                await self.bot.spoilers.refresh_post(row)
            except discord.HTTPException:
                log.exception("Could not refresh the card for TMDB %s", row["tmdb_id"])
            await asyncio.sleep(1)   # gentle on rate limits for bulk edits
