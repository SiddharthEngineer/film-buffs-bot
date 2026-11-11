"""Spoiler triggers: ||markup|| in posts, /spoiler, and the "Mark as spoiler" context menu."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..spoilers import SPOILER_RE, has_spoiler_markup

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def fully_spoilered(message: discord.Message) -> bool:
    """True if all text is already inside ||bars|| and every attachment is a spoiler."""
    text = SPOILER_RE.sub("", message.content or "").strip()
    return not text and all(a.is_spoiler() for a in message.attachments)


class Spoilers(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(name="Mark as spoiler",
                                                 callback=self.mark_as_spoiler)
        self.ctx_menu.guild_only = True
        bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @property
    def svc(self):
        return self.bot.spoilers

    async def _is_user_message(self, message: discord.Message) -> bool:
        if message.type not in USER_MESSAGE_TYPES or message.author.id == self.bot.user.id:
            return False
        if message.webhook_id and await self.svc.is_own_webhook_message(message):
            return False
        return True

    # trigger 1: ||markup|| -------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not isinstance(message.channel, discord.Thread) or not has_spoiler_markup(message):
            return
        if not await self._is_user_message(message):
            return
        if await self.svc.movie_for_thread(message.channel) is None:
            return
        await self.svc.mark(message.channel, message.id, source="message")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        message = payload.message
        if not isinstance(message.channel, discord.Thread) or not has_spoiler_markup(message):
            return
        if not await self._is_user_message(message):
            return
        if await self.svc.movie_for_thread(message.channel) is None:
            return
        await self.svc.mark(message.channel, message.id, source="message")

    # trigger 2: /spoiler ---------------------------------------------------
    @app_commands.command(name="spoiler",
                          description="Start the spoiler line here: messages after this may contain spoilers")
    @app_commands.guild_only()
    async def spoiler(self, interaction: discord.Interaction) -> None:
        thread = interaction.channel
        row = await self.svc.movie_for_thread(thread)
        if row is None:
            await interaction.response.send_message(
                "Use /spoiler inside a movie post in #movies.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        moved, info = await self.svc.mark_from_now(thread, interaction.user)
        if moved:
            await interaction.followup.send("⚠️ Spoiler line set. Spoil away below it.",
                                            ephemeral=True)
        else:
            link = (thread.get_partial_message(row["spoiler_divider_message_id"]).jump_url
                    if row["spoiler_divider_message_id"] else "")
            await interaction.followup.send(
                f"This post already has a spoiler line {link}. Everything after it is "
                "already spoiler territory.", ephemeral=True)

    # trigger 3: Mark as spoiler (retroactive) ------------------------------
    async def mark_as_spoiler(self, interaction: discord.Interaction,
                              message: discord.Message) -> None:
        thread = message.channel
        if await self.svc.movie_for_thread(thread) is None:
            await interaction.response.send_message(
                "Mark as spoiler only works inside movie posts in #movies.", ephemeral=True)
            return
        if message.id == thread.id:
            await interaction.response.send_message(
                "That's the movie's info card; it has no spoilers.", ephemeral=True)
            return
        if message.webhook_id and await self.svc.is_own_webhook_message(message):
            await interaction.response.send_message(
                "That message is already hidden as a spoiler.", ephemeral=True)
            return
        if message.author.bot or message.type not in USER_MESSAGE_TYPES:
            await interaction.response.send_message(
                "Only members' messages can be marked as spoilers.", ephemeral=True)
            return
        if message.poll or (message.stickers and not message.content and not message.attachments):
            await interaction.response.send_message(
                "Polls and stickers can't be reposted as spoilers.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if fully_spoilered(message):
            moved, _ = await self.svc.mark(thread, message.id, source="retro",
                                           actor=interaction.user)
            await interaction.followup.send(
                "That message was already hidden. " +
                ("The spoiler line now starts there." if moved else
                 "It's already below the spoiler line."), ephemeral=True)
            return
        try:
            status = await self.svc.repost_as_spoiler(message, interaction.user)
        except ValueError as e:
            await interaction.followup.send(f"❌ Couldn't repost it: {e}. Nothing was changed.",
                                            ephemeral=True)
            return
        await interaction.followup.send(f"🙈 Hidden and reposted. {status}", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Spoilers(bot))
