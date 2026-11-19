"""/help: a private list of every command (Phase 15). See filmbuffs/help.py."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..help import help_text
from ..movies import EMBED_COLOR
from .admin import is_admin

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot


class Help(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    @app_commands.command(name="help", description="Every command, in case you forgot one")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction) -> None:
        cmds = list(self.bot.tree.get_commands()) + list(
            self.bot.tree.get_commands(type=discord.AppCommandType.message))
        text = help_text(cmds, getattr(self.bot, "command_ids", {}),
                         is_admin(interaction.user, self.bot.config.admin_role),
                         self.bot.config.channels.get("welcome"),
                         self.bot.config.channels.get("watch_log"))
        embed = discord.Embed(title="📖 Commands", description=text[:4096], color=EMBED_COLOR)
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Help(bot))
