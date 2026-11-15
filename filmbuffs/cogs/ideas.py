"""#ideas (Phase 14): the admin-only "Add to ideas" message menu and the pinned list
refresh at startup. /admin idea done|drop live in cogs/admin.py."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from ..ideas import TEXT_MAX, message_url, one_line
from .admin import admin_only

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def add_refusal(message: discord.Message, ideas_channel_id: int | None,
                list_message_id: int | None) -> str | None:
    """Why a message can't be added to the ideas list (None if it can)."""
    if ideas_channel_id is None:
        return "#ideas isn't set up yet (run setup_server.py --apply, then restart the bot)."
    if message.channel.id != ideas_channel_id:
        return f"Add to ideas only works on messages in <#{ideas_channel_id}>."
    if list_message_id is not None and message.id == list_message_id:
        return "That's the ideas list itself."
    if message.author.bot or message.webhook_id or message.type not in USER_MESSAGE_TYPES:
        return "Only members' messages can be added as ideas."
    return None


class IdeaModal(discord.ui.Modal, title="Add to ideas"):
    def __init__(self, cog: "Ideas", message: discord.Message):
        super().__init__()
        self.cog, self.message = cog, message
        self.text = discord.ui.TextInput(
            label="The idea, in one line", max_length=TEXT_MAX, required=True,
            default=one_line(message.clean_content) or None,
            placeholder="Shorten it if the message rambles")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.add(interaction, self.message, self.text.value)


class Ideas(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(name="Add to ideas",
                                                 callback=self.add_to_ideas)
        self.ctx_menu.guild_only = True
        # Hidden from members in the Apps menu; the admin_only() check is the real gate.
        self.ctx_menu.default_permissions = discord.Permissions(manage_guild=True)
        bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        try:
            await self.bot.ideas.refresh()
        except discord.HTTPException:
            log.exception("Couldn't refresh the #ideas list at startup")

    @admin_only()
    async def add_to_ideas(self, interaction: discord.Interaction,
                           message: discord.Message) -> None:
        mid = await self.bot.db.get_meta("ideas_list_message")
        refusal = add_refusal(message, self.bot.config.channels.get("ideas"),
                              int(mid) if mid else None)
        if refusal is None:
            existing = await self.bot.db.get_idea_by_message(message.id)
            if existing is not None:
                refusal = (f"That message is already idea #{existing['id']} "
                           f"({existing['status']}).")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.send_modal(IdeaModal(self, message))

    async def add(self, interaction: discord.Interaction, message: discord.Message,
                  text: str) -> None:
        if not one_line(text):
            await interaction.response.send_message("❌ The idea needs some text.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            idea_id = await self.bot.ideas.add(message, text, interaction.user.id)
        except aiosqlite.IntegrityError:
            await interaction.followup.send("That message was just added by someone else.",
                                            ephemeral=True)
            return
        url = message_url(self.bot.config.guild_id, message.channel.id, message.id)
        await interaction.followup.send(f"💡 Added idea #{idea_id} ([message]({url})) to the "
                                        "pinned list.", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Ideas(bot))
