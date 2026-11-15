"""Weekly and group monthly picks: /nominate, /unnominate, /nominations, /schedule,
/watchlist show|clear, the ranked-vote buttons and the 30 s tick.

The vote commands act on the channel's context: in a private group's category
(Phase 12) they use that group's pool and monthly vote; anywhere else, the
main server's weekly vote."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..tmdb import TMDBError
from .movies import add_movie, post_note
from ..vote_ui import DYNAMIC_ITEMS, in_group, send_private

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

LEADERBOARD_META = "leaderboard_planning_message_id"
RETIRED_MONTHLY_META = "retired_monthly_ballots"
NOT_MEMBER = "🔒 That's only for members of this group."


class Weekly(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)
        self.tick.start()
        self._retire_task = asyncio.create_task(self._retire_leaderboard())
        self._retire_monthly_task = asyncio.create_task(self._retire_main_monthly())

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)
        self.tick.cancel()
        self._retire_task.cancel()
        self._retire_monthly_task.cancel()

    async def _retire_leaderboard(self) -> None:
        """👍 no longer count (owner, 2026-11-15): delete the bot's pinned "🏆 Top 25
        most wanted" / "By genre" message in #watch-planning once."""
        await self.bot.wait_until_ready()
        db = self.bot.db
        mid = await db.get_meta(LEADERBOARD_META)
        ch = self.bot.weekly.channel("watch_planning")
        if not mid or ch is None:
            return
        try:
            await ch.get_partial_message(int(mid)).delete()
            log.info("Deleted the old leaderboard message %s in #watch-planning", mid)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception("Could not delete the old leaderboard message %s", mid)
            return
        await db.set_meta(LEADERBOARD_META, None)

    async def _retire_main_monthly(self) -> None:
        """The main server's monthly vote is gone (owner, 2026-11-15): delete its
        🌙 ballot and the bot's "🌙 Monthly vote … is open" notices in #watch-planning once."""
        await self.bot.wait_until_ready()
        db = self.bot.db
        ids = await db.get_meta(RETIRED_MONTHLY_META)
        ch = self.bot.weekly.channel("watch_planning")
        if not ids or ch is None:
            return
        try:
            for mid in ids.split(","):
                try:
                    await ch.get_partial_message(int(mid)).delete()
                    log.info("Deleted the main monthly ballot %s in #watch-planning", mid)
                except discord.NotFound:
                    pass
            async for m in ch.history(limit=500):
                if m.author.id == self.bot.user.id and m.content.startswith("🌙 **Monthly vote for"):
                    await m.delete()
                    log.info("Deleted the main monthly vote notice %s", m.id)
        except discord.HTTPException:
            log.exception("Could not delete the main monthly vote messages")
            return
        await db.set_meta(RETIRED_MONTHLY_META, None)

    def _context(self, interaction: discord.Interaction):
        """(pool, group vote or None, error) for the channel the command was used in."""
        grp = self.bot.group_of(interaction.channel)
        if grp is None:
            return self.bot.pool, None, None
        svc = self.bot.group_votes.get(grp)
        if svc is None:
            return None, None, "That group isn't set up yet."
        if not in_group(self.bot, interaction.user, grp):
            return None, None, NOT_MEMBER
        return svc.pool, svc, None

    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        try:
            await self.bot.weekly.tick()
        except Exception:
            log.exception("Weekly pick tick failed")

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    # helpers ---------------------------------------------------------------
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
                          description="Nominate a movie for this channel's ballot (adds its post if needed)")
    @app_commands.describe(title="Start typing and pick the movie (default: the post you're in)",
                           rank="Where it goes in your list (1 = on the ballot). With 3 already, "
                                "it replaces that one",
                           pitch="Why we should watch it (optional; shown on a new post)")
    @app_commands.guild_only()
    async def nominate(self, interaction: discord.Interaction, title: str | None = None,
                       rank: app_commands.Range[int, 1, 3] | None = None,
                       pitch: app_commands.Range[str, 1, 1000] | None = None) -> None:
        pool, group, err = self._context(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = ""
        if group is not None and not title:
            await interaction.followup.send("Pick a movie with `title:`.", ephemeral=True)
            return
        if title:
            # Everything /addmovie does: TMDB lookup, and a #movies post if there's none.
            movie, thread, created, err = await add_movie(self.bot, interaction.user, title,
                                                          pitch, source="nominate")
            if err:
                await interaction.followup.send(err, ephemeral=True)
                return
            tmdb_id = movie.id
            note = post_note(movie, thread, created, pitch) + "\n"
        else:
            row = await self.bot.db.get_movie_by_thread(interaction.channel_id)
            if row is None:
                await interaction.followup.send(
                    "Pick a movie with `title:`, or run this inside its post in #movies.",
                    ephemeral=True)
                return
            tmdb_id = row["tmdb_id"]
            if pitch:
                note = "-# A pitch only goes on a new post; post it here as a message instead.\n"
        reply = await pool.nominate(interaction.user.id, tmdb_id, rank)
        await interaction.followup.send((note + reply)[:2000], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @nominate.autocomplete("title")
    async def _nominate_title(self, interaction: discord.Interaction, current: str):
        return await self._title_autocomplete(interaction, current)

    @app_commands.command(name="unnominate", description="Withdraw one of your nominations")
    @app_commands.describe(film="Which of your nominations")
    @app_commands.guild_only()
    async def unnominate(self, interaction: discord.Interaction, film: str) -> None:
        pool, _, err = self._context(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        try:
            tmdb_id = int(film)
        except ValueError:
            await interaction.response.send_message(
                "❌ Pick one of your nominations from the list.", ephemeral=True)
            return
        await interaction.response.send_message(
            await pool.unnominate(interaction.user.id, tmdb_id), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @unnominate.autocomplete("film")
    async def _unnominate_film(self, interaction: discord.Interaction, current: str):
        pool, _, err = self._context(interaction)
        if err:
            return []
        cur = current.lower()
        out = []
        for r in await self.bot.db.pool(interaction.user.id, pool.grp):
            name = f"#{r['rank']} " + (f"{r['title']} ({r['year']})" if r["title"] else
                                       f"TMDB {r['tmdb_id']}")
            if cur in name.lower():
                out.append(app_commands.Choice(name=name[:100], value=str(r["tmdb_id"])))
        return out

    @app_commands.command(name="nominations",
                          description="Your nominations (private): reorder or withdraw them")
    @app_commands.guild_only()
    async def nominations(self, interaction: discord.Interaction) -> None:
        pool, _, err = self._context(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        text, view = await pool.my_view(interaction.user.id)
        await send_private(interaction, text, view)

    @app_commands.command(name="schedule",
                          description="What's on, the open vote and your nominations")
    @app_commands.guild_only()
    async def schedule(self, interaction: discord.Interaction) -> None:
        pool, group, err = self._context(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if group is None:
            text = await self.bot.weekly.schedule_text(interaction.user.id)
        else:
            from datetime import timezone
            lines = await group.schedule_lines(interaction.user.id,
                                               datetime.now(timezone.utc))
            text = "\n".join(lines + ["", await pool._summary(interaction.user.id)])
        await interaction.response.send_message(text[:2000], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    watchlist = app_commands.Group(name="watchlist", guild_only=True,
                                   description="Your uploaded Letterboxd watchlist")

    @watchlist.command(name="show", description="What the bot has from your watchlist upload")
    async def watchlist_show(self, interaction: discord.Interaction) -> None:
        imp, n, posted = await self.bot.db.watchlist_summary(interaction.user.id)
        if imp is None and n == 0:
            await interaction.response.send_message(
                "You haven't uploaded a watchlist. **/letterboxd sync** takes your Letterboxd "
                "export zip (optional). Its films get a post in #movies if they have none, and "
                "only break ties in the votes.", ephemeral=True)
            return
        when = f"<t:{int(datetime.fromisoformat(imp['imported_at']).timestamp())}:D>" if imp else "?"
        unmatched = json.loads(imp["unmatched"]) if imp else []
        await interaction.response.send_message(
            f"📋 Uploaded {when}: {n} films, {posted} with a post."
            + (f" {len(unmatched)} couldn't be matched." if unmatched else "")
            + "\n-# **/letterboxd sync** again to update it, **/watchlist clear** to remove it.",
            ephemeral=True)

    @watchlist.command(name="clear", description="Remove your uploaded watchlist")
    async def watchlist_clear(self, interaction: discord.Interaction) -> None:
        n = await self.bot.db.clear_watchlist(interaction.user.id)
        await interaction.response.send_message(
            f"✅ Removed {n} films." if n
            else "You have no uploaded watchlist.", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Weekly(bot))
