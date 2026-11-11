"""Nightly database backup (in-process, at a fixed local time)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from ..backup import latest_backup, run_backup

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)


class Ops(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        hh, mm = self.bot.config.backup_time
        self.nightly.change_interval(time=time(hh, mm, tzinfo=self.bot.config.tz))
        self.nightly.start()
        # Catch up if the bot was down at backup time (or this is the first run).
        self._catch_up_task = asyncio.create_task(self._catch_up())

    async def cog_unload(self) -> None:
        self.nightly.cancel()
        self._catch_up_task.cancel()

    async def backup_now(self) -> str:
        """Run one backup + prune. Returns a one-line summary."""
        cfg = self.bot.config
        async with self._lock:
            path, pruned = await asyncio.to_thread(
                run_backup, cfg.db_path, cfg.backups_dir, cfg.backup_keep)
        size = path.stat().st_size / 1024
        summary = f"{path.name} ({size:,.0f} KiB)"
        if pruned:
            summary += f"; pruned {len(pruned)} (keeping the newest {cfg.backup_keep})"
        log.info("Database backup written: %s", summary)
        return summary

    @tasks.loop(time=time(4, 0))
    async def nightly(self) -> None:
        try:
            await self.backup_now()
        except Exception:
            log.exception("Nightly database backup failed")

    async def _catch_up(self) -> None:
        await self.bot.wait_until_ready()
        last = latest_backup(self.bot.config.backups_dir)
        if last is None or datetime.now(timezone.utc) - last[0] > timedelta(hours=24):
            try:
                await self.backup_now()
            except Exception:
                log.exception("Catch-up database backup failed")


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Ops(bot))
