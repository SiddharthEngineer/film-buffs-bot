"""Weekly pick, votes and watchlists: /nominate, /unnominate, /schedule, /top,
/watchlist show|clear, 👍 reaction tracking, the leaderboard post, the ranked-vote
buttons and the weekly tick."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..tmdb import TMDBError, parse_choice
from ..vote_ui import DYNAMIC_ITEMS
from ..votes import ranked_line
from ..weekly import ELIGIBLE

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)


class Weekly(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)
        self.tick.start()
        self.leaderboard.start()
        self._reconcile_task = asyncio.create_task(self._reconcile())

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)
        self.tick.cancel()
        self.leaderboard.cancel()
        self._reconcile_task.cancel()

    async def _reconcile(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.bot.votes.reconcile()
        except Exception:
            log.exception("Re-reading 👍 votes failed")
            self.bot.votes.reconciled.set()

    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        try:
            await self.bot.weekly.tick()
        except Exception:
            log.exception("Weekly pick tick failed")

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()
        await self.bot.votes.reconciled.wait()   # rank with up-to-date votes

    @tasks.loop(seconds=60)
    async def leaderboard(self) -> None:
        try:
            await self.bot.votes.refresh_leaderboard()
        except Exception:
            log.exception("Leaderboard refresh failed")

    @leaderboard.before_loop
    async def _before_leaderboard(self) -> None:
        await self.bot.wait_until_ready()
        await self.bot.votes.reconciled.wait()

    # reactions -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.bot.votes.on_add(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.bot.votes.on_remove(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        await self.bot.votes.on_clear(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        row = await self.bot.db.get_movie_by_starter(payload.message_id)
        if row is not None:
            await self.bot.votes.sync_message(row)

    # helpers ---------------------------------------------------------------
    async def _movie_from(self, interaction: discord.Interaction, title: str | None):
        """(tmdb_id, error). Uses the autocomplete pick, else the movie post we're in."""
        if title:
            tmdb_id = parse_choice(title)
            if tmdb_id is None:
                return None, (f"❌ **{discord.utils.escape_markdown(title)[:100]}** isn't a movie "
                              "from the list. Start typing and pick one.")
            if await self.bot.db.get_movie(tmdb_id) is None:
                try:
                    movie = await self.bot.tmdb.movie(tmdb_id)
                except TMDBError:
                    return None, "❌ Couldn't reach TMDB. Please try again in a minute."
                if movie is None:
                    return None, f"❌ TMDB has no movie with ID {tmdb_id}."
                await self.bot.movies.ensure_post(movie, added_by=interaction.user, pitch=None,
                                                  status="backlog", source="nominate")
            return tmdb_id, None
        row = await self.bot.db.get_movie_by_thread(interaction.channel_id)
        if row is None:
            return None, "Pick a movie with `title:`, or run this inside its post in #movies."
        return row["tmdb_id"], None

    async def _title_autocomplete(self, interaction: discord.Interaction, current: str):
        query = current.strip()
        if len(query) < 2:
            return []
        try:
            pairs = await self.bot.tmdb.autocomplete(query)
        except TMDBError as e:
            log.warning("Autocomplete search failed: %s", e)
            return []
        return [app_commands.Choice(name=label, value=value) for label, value in pairs[:25]]

    # commands --------------------------------------------------------------
    @app_commands.command(name="nominate",
                          description="Nominate a movie for the open weekly ballot (one per member)")
    @app_commands.describe(title="Start typing and pick the movie (default: the post you're in)")
    @app_commands.guild_only()
    async def nominate(self, interaction: discord.Interaction, title: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        tmdb_id, err = await self._movie_from(interaction, title)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        reply = await self.bot.weekly.nominate(interaction.user.id, tmdb_id)
        await interaction.followup.send(reply, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @nominate.autocomplete("title")
    async def _nominate_title(self, interaction: discord.Interaction, current: str):
        return await self._title_autocomplete(interaction, current)

    @app_commands.command(name="unnominate", description="Withdraw your nomination from the open ballot")
    @app_commands.guild_only()
    async def unnominate(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            await self.bot.weekly.unnominate(interaction.user.id), ephemeral=True)

    @app_commands.command(name="schedule",
                          description="This week's movie and next week's nominations or vote")
    @app_commands.guild_only()
    async def schedule(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            await self.bot.weekly.schedule_text(interaction.user.id), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="top", description="The most-wanted movies, ranked by 👍 and watchlists")
    @app_commands.describe(genre="Only this genre", include_watched="Also list films already watched together")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction, genre: str | None = None,
                  include_watched: bool = False) -> None:
        cfg = self.bot.config
        if genre and (genre not in cfg.tags or not cfg.tags[genre].tmdb_genres):
            await interaction.response.send_message("❌ Pick a genre from the list.", ephemeral=True)
            return
        statuses = None if include_watched else ELIGIBLE + ("up_next",)
        rows = await self.bot.db.ranked_movies(statuses=statuses, genre=genre, limit=25,
                                               min_interest=1)
        what = cfg.tags[genre].name if genre else "all genres"
        body = "\n".join(ranked_line(i, r) for i, r in enumerate(rows, 1)) or "*No votes yet.*"
        await interaction.response.send_message(
            f"**🏆 Most wanted · {what}**\n-# 👍 votes plus 📋 uploaded watchlists, each member "
            f"once. ⏭️ Up Next · ✅ watched together\n{body}"[:2000], ephemeral=True)

    @top.autocomplete("genre")
    async def _genre_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = current.lower()
        return [app_commands.Choice(name=t.name, value=k) for k, t in self.bot.config.tags.items()
                if t.tmdb_genres and cur in t.name.lower()][:25]

    watchlist = app_commands.Group(name="watchlist", guild_only=True,
                                   description="Your uploaded Letterboxd watchlist (counts as interest)")

    @watchlist.command(name="show", description="What the bot has from your watchlist upload")
    async def watchlist_show(self, interaction: discord.Interaction) -> None:
        imp, n, posted = await self.bot.db.watchlist_summary(interaction.user.id)
        if imp is None and n == 0:
            await interaction.response.send_message(
                "You haven't uploaded a watchlist. **/letterboxd sync** takes your Letterboxd "
                "export zip. Each film on it counts toward the rankings like your 👍 would "
                "(shown as 📋); no 👍 is added to the posts. It's optional: a 👍 on a movie's "
                "post works too.", ephemeral=True)
            return
        when = f"<t:{int(datetime.fromisoformat(imp['imported_at']).timestamp())}:D>" if imp else "?"
        unmatched = json.loads(imp["unmatched"]) if imp else []
        await interaction.response.send_message(
            f"📋 Uploaded {when}: {n} films, {posted} with a post (counting as interest)."
            + (f" {len(unmatched)} couldn't be matched." if unmatched else "")
            + "\n-# **/letterboxd sync** again to update it, **/watchlist clear** to remove it.",
            ephemeral=True)

    @watchlist.command(name="clear", description="Remove your uploaded watchlist")
    async def watchlist_clear(self, interaction: discord.Interaction) -> None:
        n = await self.bot.db.clear_watchlist(interaction.user.id)
        await interaction.response.send_message(
            f"✅ Removed {n} films. Your 👍 votes are unchanged." if n
            else "You have no uploaded watchlist.", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Weekly(bot))
