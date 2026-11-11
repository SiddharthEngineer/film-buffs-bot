"""Spoiler discussions: /spoiler and the "Mark as spoiler" context menu.

The info card's "Spoiler discussion" button lives in filmbuffs/cards.py.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..cards import SpoilerButton
from ..spoilers import AlreadyMoved

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


class Spoilers(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(name="Mark as spoiler",
                                                 callback=self.mark_as_spoiler)
        self.ctx_menu.guild_only = True
        bot.tree.add_command(self.ctx_menu)
        bot.add_dynamic_items(SpoilerButton)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        self.bot.remove_dynamic_items(SpoilerButton)

    @property
    def svc(self):
        return self.bot.spoilers

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.bot.movies.refresh_old_cards()

    # /spoiler ----------------------------------------------------------------
    @app_commands.command(name="spoiler",
                          description="Open this movie's spoiler discussion (use inside a #movies post)")
    @app_commands.guild_only()
    async def spoiler(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        row = await self.svc.movie_for_spoiler_post(channel)
        if row is not None:
            movies_post = await self.bot.movies.find_post(row["tmdb_id"])
            back = f" The spoiler-free post is {movies_post.jump_url}." if movies_post else ""
            await interaction.response.send_message(
                f"You're already in the spoiler discussion.{back}", ephemeral=True)
            return
        row = await self.svc.movie_for_movies_post(channel)
        if row is None:
            await interaction.response.send_message(
                "Use /spoiler inside a movie's post in #movies.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        thread, created = await self.svc.open(row["tmdb_id"])
        verb = "Opened" if created else "Here's"
        await interaction.followup.send(
            f"{verb} the spoiler discussion → {thread.jump_url}\n"
            "-# ⚠️ Everything there may spoil the film.", ephemeral=True)

    # Mark as spoiler ---------------------------------------------------------
    async def mark_as_spoiler(self, interaction: discord.Interaction,
                              message: discord.Message) -> None:
        thread = interaction.channel
        row = await self.svc.movie_for_movies_post(thread)
        if row is None:
            text = ("This is already the spoiler discussion, so nothing needs hiding here."
                    if await self.svc.movie_for_spoiler_post(thread) is not None else
                    "Mark as spoiler only works inside movie posts in #movies.")
            await interaction.response.send_message(text, ephemeral=True)
            return
        refusal = None
        if message.id == thread.id:
            refusal = "That's the movie's info card; it has no spoilers."
        elif message.author.bot or message.webhook_id or message.type not in USER_MESSAGE_TYPES:
            refusal = "Only members' messages can be moved to the spoiler discussion."
        elif message.poll:
            refusal = "Polls can't be moved. Ask the author to delete it instead."
        elif not (message.content or message.attachments):
            refusal = "Stickers and empty messages can't be moved."
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            moved = await self.svc.move(message, interaction.user, row["tmdb_id"])
        except AlreadyMoved:
            await interaction.followup.send(
                "That message is already gone (someone may have moved it just now).",
                ephemeral=True)
            return
        except ValueError as e:
            await interaction.followup.send(f"❌ Couldn't move it: {e}. Nothing was changed.",
                                            ephemeral=True)
            return
        await interaction.followup.send(
            f"🙈 Moved to the spoiler discussion → {moved.jump_url}", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Spoilers(bot))
