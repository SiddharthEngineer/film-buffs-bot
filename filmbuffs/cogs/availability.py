"""Daily In Theaters / Streaming check (Phase 9), at a fixed local time."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

RUN_AT = time(5, 0)   # local (config.tz); after the 04:00 backup


class Availability(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.daily.change_interval(time=RUN_AT.replace(tzinfo=self.bot.config.tz))
        self.daily.start()
        # Catch up if the bot was down at run time (or this is the first run).
        self._catch_up_task = asyncio.create_task(self._catch_up())

    async def cog_unload(self) -> None:
        self.daily.cancel()
        self._catch_up_task.cancel()

    @tasks.loop(time=RUN_AT)
    async def daily(self) -> None:
        try:
            await self.bot.availability.run()
        except Exception:
            log.exception("Daily In Theaters / Streaming check failed")

    async def _catch_up(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)   # let startup card refreshes and the poller settle first
        last = await self.bot.db.get_meta("availability_last_run")
        if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=24):
            return
        try:
            await self.bot.availability.run()
        except Exception:
            log.exception("Catch-up In Theaters / Streaming check failed")


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Availability(bot))
