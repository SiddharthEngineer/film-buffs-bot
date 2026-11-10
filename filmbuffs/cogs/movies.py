"""/addmovie: add a movie to the backlog as a post in #movies."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..tmdb import TMDBError, parse_choice

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)


class Movies(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    @app_commands.command(name="addmovie", description="Add a movie to the backlog (creates its post in #movies)")
    @app_commands.describe(title="Start typing, then pick the movie from the list",
                           pitch="Why we should watch it (optional)")
    @app_commands.guild_only()
    async def addmovie(self, interaction: discord.Interaction, title: str,
                       pitch: app_commands.Range[str, 1, 1000] | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        tmdb_id = parse_choice(title)
        if tmdb_id is None:
            await interaction.followup.send(
                f"❌ **{discord.utils.escape_markdown(title)[:100]}** isn't a movie from the list. "
                "Start typing the title, wait for suggestions, and pick one. No post was created.",
                ephemeral=True)
            return
        try:
            movie = await self.bot.tmdb.movie(tmdb_id)
        except TMDBError:
            log.exception("TMDB lookup failed for %s", tmdb_id)
            await interaction.followup.send(
                "❌ Couldn't reach TMDB right now. Please try again in a minute. No post was created.",
                ephemeral=True)
            return
        if movie is None:
            await interaction.followup.send(
                f"❌ TMDB has no movie with ID {tmdb_id}. No post was created.", ephemeral=True)
            return

        thread, created = await self.bot.movies.ensure_post(
            movie, added_by=interaction.user, pitch=pitch, status="backlog", source="addmovie")
        if created:
            await interaction.followup.send(
                f"✅ Added **{movie.display}** → {thread.jump_url}", ephemeral=True)
        else:
            extra = " Post your pitch there as a message." if pitch else ""
            await interaction.followup.send(
                f"**{movie.display}** already has a post → {thread.jump_url}{extra}", ephemeral=True)

    @addmovie.autocomplete("title")
    async def title_autocomplete(self, interaction: discord.Interaction,
                                 current: str) -> list[app_commands.Choice[str]]:
        query = current.strip()
        if len(query) < 2:
            return []
        try:
            pairs = await self.bot.tmdb.autocomplete(query)
        except TMDBError as e:
            log.warning("Autocomplete search failed: %s", e)
            return []
        return [app_commands.Choice(name=label, value=value) for label, value in pairs[:25]]


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Movies(bot))
