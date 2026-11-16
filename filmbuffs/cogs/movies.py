"""/addmovie: add a movie to the backlog as a post in #pre-watch."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..tmdb import Movie, TMDBError, parse_choice

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)


class Movies(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    @app_commands.command(name="addmovie",
                          description="Add a movie to the backlog without nominating it (creates its post)")
    @app_commands.describe(title="Start typing, then pick the movie from the list")
    @app_commands.guild_only()
    async def addmovie(self, interaction: discord.Interaction, title: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        movie, thread, created, err = await add_movie(self.bot, interaction.user, title,
                                                      source="addmovie")
        if err:
            await interaction.followup.send(err + " No post was created.", ephemeral=True)
            return
        await interaction.followup.send(post_note(movie, thread, created), ephemeral=True)

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


async def add_movie(bot: "FilmBuffsBot", user: discord.abc.User, title: str,
                    source: str) -> tuple[Movie | None, discord.Thread | None, bool, str | None]:
    """The /addmovie part, shared with /nominate: read the autocomplete pick, look the
    film up on TMDB and make sure it has a #pre-watch post (Backlog, added by `user`).
    Returns (movie, post, created, None), or (None, None, False, error)."""
    tmdb_id = parse_choice(title)
    if tmdb_id is None:
        return None, None, False, (
            f"❌ **{discord.utils.escape_markdown(title)[:100]}** isn't a movie from the list. "
            "Start typing the title, wait for suggestions, and pick one.")
    try:
        movie = await bot.tmdb.movie(tmdb_id)
    except TMDBError:
        log.exception("TMDB lookup failed for %s", tmdb_id)
        return None, None, False, "❌ Couldn't reach TMDB right now. Please try again in a minute."
    if movie is None:
        return None, None, False, f"❌ TMDB has no movie with ID {tmdb_id}."
    thread, created = await bot.movies.ensure_post(movie, added_by=user,
                                                   status="backlog", source=source)
    return movie, thread, created, None


def post_note(movie: Movie, thread: discord.Thread, created: bool) -> str:
    if created:
        return f"✅ Added **{movie.display}** → {thread.jump_url}"
    return f"**{movie.display}** already has a post → {thread.jump_url}"


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Movies(bot))
