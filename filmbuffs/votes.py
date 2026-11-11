"""👍 votes on movie info cards, and the pinned leaderboard message in #watch-planning.

Votes are 👍 reactions (any skin tone) on a movie post's first message, kept in
the `votes` table from reaction events. At startup the bot re-reads every card's
reactions (slowly, in the background) to catch anything missed while it was
down; that pass also removes the bot's own 👍 from older posts.

"Interest" in a film = distinct members who 👍'd it or have it on their uploaded
Letterboxd watchlist. The leaderboard, /top and the ballot rank by it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from .weekly import ELIGIBLE, interest_text, score_text

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

# Old #movies forum post (retired), and the current #watch-planning message.
META_THREAD, META_MESSAGE = "leaderboard_thread_id", "leaderboard_message_id"
META_LB = "leaderboard_planning_message_id"
GENRES_PER_TAG = 3
RECONCILE_PAUSE = 0.3


def is_vote(emoji) -> bool:
    return str(emoji).startswith("👍")


def ranked_line(i: int, r) -> str:
    mark = " ⏭️" if r["status"] == "up_next" else " ✅" if r["status"] == "watched" else ""
    return f"`{i:>2}.` <#{r['thread_id']}>{mark} · {score_text(r)}"


def leaderboard_embeds(top: list, by_genre: list[tuple[str, list]], movies_channel: int,
                       size: int) -> list[discord.Embed]:
    head = ("Films not yet watched together, ranked by how many members want to see them: "
            "👍 on the movie's first message in <#%d>, or 📋 the film on their uploaded "
            "Letterboxd watchlist (**/watchlist**). Someone with both counts once. "
            "⏭️ = Up Next.\nUse **/top** for one genre or to include watched films. "
            "Updates within a minute." % movies_channel)
    body = "\n".join(ranked_line(i, r) for i, r in enumerate(top, 1)) or \
        f"*No votes yet. 👍 the first message of the posts in <#{movies_channel}> you want to watch.*"
    main = discord.Embed(title=f"🏆 Top {size} most wanted", description=f"{head}\n\n{body}",
                         color=0xE5A00D)
    genre = discord.Embed(title="By genre", color=0xE5A00D)
    for name, rows in by_genre:
        if rows:
            genre.add_field(name=name, value="\n".join(
                f"<#{r['thread_id']}> · {score_text(r)}" for r in rows), inline=False)
    return [main, genre] if genre.fields else [main]


class VoteService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._rendered: str | None = None
        self._lb_lock = asyncio.Lock()
        self._pinned = False
        self._pin_warned = False
        self.reconciled = asyncio.Event()

    # reactions -------------------------------------------------------------
    def _relevant(self, payload) -> bool:
        return (payload.guild_id == self.bot.config.guild_id and is_vote(payload.emoji)
                and payload.user_id != self.bot.user.id)

    async def on_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not self._relevant(payload):
            return
        row = await self.bot.db.get_movie_by_starter(payload.message_id)
        if row is not None:
            await self.bot.db.add_vote(row["tmdb_id"], payload.user_id)

    async def on_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if not self._relevant(payload):
            return
        row = await self.bot.db.get_movie_by_starter(payload.message_id)
        if row is None:
            return
        # A member may have 👍 in several skin tones; only drop the vote when
        # none is left, so re-read the card.
        await self.sync_message(row)

    async def on_clear(self, message_id: int) -> None:
        row = await self.bot.db.get_movie_by_starter(message_id)
        if row is not None:
            await self.bot.db.remove_vote(row["tmdb_id"])

    async def sync_message(self, row, remove_own: bool = False) -> None:
        """Make stored votes match the card's 👍 reactions."""
        ch = self.bot.get_partial_messageable(row["thread_id"])
        try:
            msg = await ch.fetch_message(row["starter_message_id"])
        except discord.NotFound:
            return
        users: set[int] = set()
        for reaction in msg.reactions:
            if not is_vote(reaction.emoji):
                continue
            async for u in reaction.users(limit=None):
                if u.id == self.bot.user.id:
                    if remove_own:
                        try:
                            await msg.remove_reaction(reaction.emoji, u)
                        except discord.HTTPException:
                            log.warning("Could not remove the bot's 👍 from %s", row["thread_id"])
                else:
                    users.add(u.id)
        await self.bot.db.set_votes(row["tmdb_id"], users)

    async def reconcile(self) -> None:
        """Re-read every card's 👍 (and drop the bot's own). Runs once per start."""
        cur = await self.bot.db.conn.execute("SELECT * FROM movies ORDER BY added_at")
        rows = list(await cur.fetchall())
        log.info("Re-reading 👍 votes on %d movie posts", len(rows))
        failed = 0
        for row in rows:
            try:
                await self.sync_message(row, remove_own=True)
            except discord.HTTPException:
                failed += 1
                log.warning("Could not read votes for TMDB %s", row["tmdb_id"], exc_info=True)
            await asyncio.sleep(RECONCILE_PAUSE)
        log.info("Vote re-read done (%d failed)", failed)
        self.reconciled.set()

    # leaderboard -----------------------------------------------------------
    async def render(self) -> list[discord.Embed]:
        cfg, db = self.bot.config, self.bot.db
        statuses = ELIGIBLE + ("up_next",)
        top = await db.ranked_movies(statuses=statuses, limit=cfg.leaderboard_size, min_interest=1)
        by_genre = []
        for key, tag in cfg.tags.items():
            if tag.tmdb_genres:
                by_genre.append((tag.name, await db.ranked_movies(
                    statuses=statuses, genre=key, limit=GENRES_PER_TAG, min_interest=1)))
        return leaderboard_embeds(top, by_genre, cfg.channels["movies"], cfg.leaderboard_size)

    async def refresh_leaderboard(self) -> None:
        async with self._lb_lock:
            await self._retire_forum_post()
            await self._ensure_pinned()
            embeds = await self.render()
            text = "\n".join((e.description or "") + str([f.value for f in e.fields]) for e in embeds)
            if text == self._rendered:
                return
            db = self.bot.db
            ch = self.bot.weekly.channel("watch_planning")
            if ch is None:
                return
            mid = await db.get_meta(META_LB)
            if mid:
                try:
                    await ch.get_partial_message(int(mid)).edit(embeds=embeds)
                    self._rendered = text
                    return
                except discord.NotFound:
                    log.warning("Leaderboard message was deleted; posting a new one")
            msg = await ch.send(embeds=embeds, allowed_mentions=discord.AllowedMentions.none())
            await db.set_meta(META_LB, str(msg.id))
            self._pinned = False
            await self._ensure_pinned()
            self._rendered = text
            log.info("Posted the leaderboard message %s in #watch-planning", msg.id)

    async def _ensure_pinned(self) -> None:
        """Pin the leaderboard message (retried each refresh until it works)."""
        if self._pinned:
            return
        mid = await self.bot.db.get_meta(META_LB)
        ch = self.bot.weekly.channel("watch_planning")
        if not mid or ch is None:
            return
        try:
            msg = await ch.fetch_message(int(mid))
            if not msg.pinned:
                await msg.pin(reason="Leaderboard")
            self._pinned = True
        except discord.NotFound:
            pass
        except discord.Forbidden:
            if not self._pin_warned:
                self._pin_warned = True
                log.warning("Can't pin the leaderboard in #watch-planning: the bot's role needs "
                            "the Pin Messages permission. It retries every minute.",
                            extra={"botlogs": True})

    async def _retire_forum_post(self) -> None:
        """The leaderboard used to be a pinned #movies post; a forum has only one
        pin, which now goes to the week's movie. Delete the old post once."""
        db = self.bot.db
        tid = await db.get_meta(META_THREAD)
        if not tid:
            return
        try:
            thread = self.bot.get_channel(int(tid)) or await self.bot.fetch_channel(int(tid))
            await thread.delete(reason="Leaderboard moved to #watch-planning")
            log.info("Deleted the old leaderboard post %s in #movies", tid)
        except discord.NotFound:
            pass
        await db.set_meta(META_THREAD, None)
        await db.set_meta(META_MESSAGE, None)
