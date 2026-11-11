"""The Film Buffs bot."""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .backup import latest_backup
from .botlogs import DiscordLogHandler
from .config import Config
from .db import MIGRATIONS, Database
from .letterboxd import LetterboxdService
from .movies import MovieService
from .spoilers import SpoilerService
from .tmdb import TMDB
from .votes import VoteService
from .weekly import WeeklyService

log = logging.getLogger("filmbuffs")

VERSION = "0.6.1"
USER_AGENT = f"FilmBuffsBot/{VERSION} (private Discord bot)"
EXTENSIONS = ["filmbuffs.cogs.movies", "filmbuffs.cogs.spoilers", "filmbuffs.cogs.letterboxd",
              "filmbuffs.cogs.ops", "filmbuffs.cogs.admin", "filmbuffs.cogs.weekly"]


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5, cwd=Path(__file__).resolve().parent)
        return out.stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


class FilmBuffsBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         help_command=None,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.config = config
        self.db = Database(config.db_path)
        self.session: aiohttp.ClientSession | None = None
        self.tmdb: TMDB | None = None
        self.movies = MovieService(self)
        self.spoilers = SpoilerService(self)
        self.letterboxd = LetterboxdService(self)
        self.votes = VoteService(self)
        self.weekly = WeeklyService(self)
        self.botlogs = DiscordLogHandler(self)
        self._announced = False

    async def setup_hook(self) -> None:
        self.botlogs.start()
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
        if not self._announced:       # on_ready also fires after reconnects
            self._announced = True
            self.botlogs.post(self.startup_notice())

    def startup_notice(self) -> str:
        last = latest_backup(self.config.backups_dir)
        if last is None:
            backup = "no backup yet (one runs now)"
        else:
            age = datetime.now(timezone.utc) - last[0]
            backup = (f"last backup {last[0].astimezone(self.config.tz):%b %d %H:%M %Z}"
                      + (" (over a day old; one runs now)" if age.days >= 1 else ""))
        hh, mm = self.config.backup_time
        return (f"🟢 **Bot started** · v{VERSION} · commit `{git_commit()}` · "
                f"schema {len(MIGRATIONS)} · {backup}\n"
                f"-# Nightly backup at {hh:02d}:{mm:02d} {self.config.tz.key}, "
                f"keeping {self.config.backup_keep_days} days.")

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "🔒 That command is for admins only."
        else:
            cmd = interaction.command.qualified_name if interaction.command else "?"
            where = getattr(interaction.channel, "name", interaction.channel_id)
            log.error("/%s failed (user %s in #%s)", cmd, interaction.user.id, where,
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
        await self.botlogs.stop()
        await super().close()
        if self.session:
            await self.session.close()
        await self.db.close()
