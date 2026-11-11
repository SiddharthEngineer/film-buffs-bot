"""Weekly pick, votes and watchlists: /nominate, /unnominate, /schedule, /top,
/watchlist, 👍 reaction tracking, the leaderboard post and the weekly tick."""
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
from ..votes import ranked_line
from ..watchlist import WatchlistError, import_watchlist, parse_watchlist
from ..weekly import ELIGIBLE

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

POST_SECONDS = 3.0      # pause between posts created from a watchlist import


class Weekly(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._importing: set[int] = set()

    async def cog_load(self) -> None:
        self.tick.start()
        self.leaderboard.start()
        self._reconcile_task = asyncio.create_task(self._reconcile())

    async def cog_unload(self) -> None:
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
                          description="This week's movie, next week's poll and the open ballot")
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

    @watchlist.command(name="import",
                       description="Upload your Letterboxd export zip (or its watchlist.csv)")
    @app_commands.describe(file="Letterboxd → Settings → Data → Export your data; upload the zip")
    async def watchlist_import(self, interaction: discord.Interaction,
                               file: discord.Attachment) -> None:
        uid = interaction.user.id
        if uid in self._importing:
            await interaction.response.send_message("⏳ Your previous upload is still being read.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._importing.add(uid)
        try:
            try:
                rows = parse_watchlist(await file.read(), file.filename)
            except WatchlistError as e:
                await interaction.followup.send(f"❌ Couldn't read it: {e}.", ephemeral=True)
                return
            if not rows:
                await interaction.followup.send("Your watchlist in that file is empty. Nothing "
                                                "was changed.", ephemeral=True)
                return
            if len(rows) > 50:
                await interaction.followup.send(
                    f"⏳ Matching {len(rows)} films with TMDB. This can take a minute or two.",
                    ephemeral=True)
            matched, unmatched = await import_watchlist(self.bot.db, self.bot.tmdb, uid, rows)
            _, n, posted = await self.bot.db.watchlist_summary(uid)
            log.info("%s imported a watchlist: %d rows, %d matched", uid, len(rows), len(matched))
            msg = (f"✅ Watchlist saved: {len(rows)} films, {n} matched; they count as your "
                   f"interest (📋) in the rankings and ballot. {posted} already have a post in "
                   f"<#{self.bot.config.channels['movies']}>.\n-# This replaces your previous upload. "
                   f"It only changes when you upload again; your live Letterboxd watchlist isn't "
                   f"read.")
            if unmatched:
                shown = ", ".join(unmatched[:15]) + (" …" if len(unmatched) > 15 else "")
                msg += f"\n-# Couldn't match {len(unmatched)}: {shown}"
            missing = await self.bot.db.watchlist_without_posts(uid)
            if missing:
                mins = max(1, round(len(missing) * POST_SECONDS / 60))
                msg += (f"\n📋 Creating **{len(missing)}** missing posts (tagged Backlog, added by "
                        f"you), about {mins} min. I'll say here when it's done.")
            await interaction.followup.send(msg[:2000], ephemeral=True)
            if missing:
                await self._create_posts(interaction, missing)
        finally:
            self._importing.discard(uid)

    async def _create_posts(self, interaction: discord.Interaction, tmdb_ids: list[int]) -> None:
        """Create #movies posts for watchlist films that have none, gently."""
        made, failed = 0, 0
        for tmdb_id in tmdb_ids:
            try:
                movie = await self.bot.tmdb.movie(tmdb_id)
                if movie is None:
                    failed += 1
                    continue
                _, created = await self.bot.movies.ensure_post(
                    movie, added_by=interaction.user.id, pitch=None, status="backlog",
                    source="watchlist")
                made += created
            except (TMDBError, discord.HTTPException):
                failed += 1
                log.warning("Watchlist post for TMDB %s failed", tmdb_id, exc_info=True)
            await asyncio.sleep(POST_SECONDS)
        log.info("Watchlist import by %s: %d posts created, %d failed",
                 interaction.user.id, made, failed)
        text = f"✅ Created {made} posts from your watchlist." + (
            f" {failed} failed; run **/watchlist import** again to retry them." if failed else "")
        try:   # the interaction token lasts 15 minutes
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            pass

    @watchlist.command(name="show", description="What the bot has from your watchlist upload")
    async def watchlist_show(self, interaction: discord.Interaction) -> None:
        imp, n, posted = await self.bot.db.watchlist_summary(interaction.user.id)
        if imp is None and n == 0:
            await interaction.response.send_message(
                "You haven't uploaded a watchlist. **/watchlist import** takes your Letterboxd "
                "export zip. (It's optional: 👍 on a movie's post counts the same.)", ephemeral=True)
            return
        when = f"<t:{int(datetime.fromisoformat(imp['imported_at']).timestamp())}:D>" if imp else "?"
        unmatched = json.loads(imp["unmatched"]) if imp else []
        await interaction.response.send_message(
            f"📋 Uploaded {when}: {n} films, {posted} with a post (counting as interest)."
            + (f" {len(unmatched)} couldn't be matched." if unmatched else "")
            + "\n-# **/watchlist import** again to update it, **/watchlist clear** to remove it.",
            ephemeral=True)

    @watchlist.command(name="clear", description="Remove your uploaded watchlist")
    async def watchlist_clear(self, interaction: discord.Interaction) -> None:
        n = await self.bot.db.clear_watchlist(interaction.user.id)
        await interaction.response.send_message(
            f"✅ Removed {n} films. Your 👍 votes are unchanged." if n
            else "You have no uploaded watchlist.", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Weekly(bot))
