"""The Film Buffs bot."""
from __future__ import annotations

import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .db import Database
from .movies import MovieService
from .tmdb import TMDB

log = logging.getLogger("filmbuffs")

USER_AGENT = "FilmBuffsBot/0.2 (private Discord bot)"
EXTENSIONS = ["filmbuffs.cogs.movies"]


class FilmBuffsBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True   # spoiler markup detection (Phase 3)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         help_command=None,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.config = config
        self.db = Database(config.db_path)
        self.session: aiohttp.ClientSession | None = None
        self.tmdb: TMDB | None = None
        self.movies = MovieService(self)

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        self.tmdb = TMDB(self.session, self.config.tmdb_key)
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.tree.on_error = self.on_app_command_error
        # Commands are registered to this one server only, so changes show up instantly.
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d commands: %s", len(synced), ", ".join(c.name for c in synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s; guilds: %s", self.user, [g.name for g in self.guilds])

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        log.error("Command %s failed", interaction.command and interaction.command.name,
                  exc_info=error)
        msg = "⚠️ Something went wrong. The error was logged for the admins."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def close(self) -> None:
        await super().close()
        if self.session:
            await self.session.close()
        await self.db.close()
