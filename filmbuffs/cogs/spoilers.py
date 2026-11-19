"""Post-watch discussions: the "Mark as spoiler" context menu (/spoiler went in
Phase 20: the info card links the post-watch post).

Info cards link their #post-watch post in text (Phase 18); the old button's
handler, kept for cards not yet refreshed, lives in filmbuffs/cards.py.
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
        await self.svc.create_missing_posts()
        await self.bot.movies.refresh_old_cards()
        await self.svc.refresh_starters()
        await self.svc.rebuild_posts()

    # Mark as spoiler ---------------------------------------------------------
    async def mark_as_spoiler(self, interaction: discord.Interaction,
                              message: discord.Message) -> None:
        thread = interaction.channel
        row = await self.svc.movie_for_movies_post(thread)
        if row is None:
            text = ("This is already the post-watch discussion, so spoilers are fine here."
                    if await self.svc.movie_for_spoiler_post(thread) is not None else
                    "Mark as spoiler only works inside movie posts in #pre-watch.")
            await interaction.response.send_message(text, ephemeral=True)
            return
        refusal = None
        if message.id == thread.id:
            refusal = "That's the movie's info card; it has no spoilers."
        elif message.author.bot or message.webhook_id or message.type not in USER_MESSAGE_TYPES:
            refusal = "Only members' messages can be moved to the post-watch discussion."
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
            f"🙈 Moved to the post-watch discussion → {moved.jump_url}", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Spoilers(bot))
