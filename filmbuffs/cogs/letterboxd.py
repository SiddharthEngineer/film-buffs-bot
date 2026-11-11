"""/letterboxd, /letterboxd-unlink, and the background feed poller."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..letterboxd import (PROFILE_URL, AlreadyLinked, LetterboxdError, UserNotFound,
                          normalize_username)

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)


class Letterboxd(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.poll.change_interval(minutes=self.bot.config.lb_poll_minutes)
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    @property
    def svc(self):
        return self.bot.letterboxd

    # poller ------------------------------------------------------------------
    @tasks.loop(minutes=15)
    async def poll(self) -> None:
        try:
            await self.svc.poll_all()
        except Exception:
            log.exception("Letterboxd poll round failed")

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # commands ----------------------------------------------------------------
    @app_commands.command(name="letterboxd",
                          description="Link your Letterboxd account (or see which one is linked)")
    @app_commands.describe(username="Your Letterboxd username or profile link")
    @app_commands.guild_only()
    async def letterboxd(self, interaction: discord.Interaction,
                         username: app_commands.Range[str, 1, 100] | None = None) -> None:
        uid = interaction.user.id
        if username is None:
            link = await self.bot.db.get_link(uid)
            if link is None:
                text = "You haven't linked a Letterboxd account. Run `/letterboxd username:<name>`."
            else:
                text = (f"Linked to **{link['username']}** "
                        f"(<{PROFILE_URL.format(username=link['username'])}>).")
                if link["last_error"]:
                    text += f"\n-# ⚠️ Last check failed: {link['last_error'][:200]}"
            await interaction.response.send_message(text, ephemeral=True)
            return

        name = normalize_username(username)
        if name is None:
            await interaction.response.send_message(
                "❌ That doesn't look like a Letterboxd username. Use the name from your "
                "profile link, e.g. `letterboxd.com/yourname`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.svc.link(uid, name)
        except AlreadyLinked:
            await interaction.followup.send(
                f"❌ **{name}** is already linked to another member. Ask an admin if that's wrong.",
                ephemeral=True)
            return
        except UserNotFound:
            await interaction.followup.send(
                f"❌ Letterboxd has no user **{name}**. Check the spelling in your profile link.",
                ephemeral=True)
            return
        except LetterboxdError as e:
            log.warning("Linking %s to %s failed: %s", uid, name, e)
            await interaction.followup.send(
                "❌ Couldn't reach Letterboxd right now. Please try again in a few minutes.",
                ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Linked to **{name}**. From now on, new diary entries show up in "
            f"{self._feed_mention()} and the movie's post (ratings and reviews go in its "
            "spoiler discussion). Entries you logged before now aren't posted.\n"
            f"-# Checked about every {self.bot.config.lb_poll_minutes:g} minutes. "
            "`/letterboxd-unlink` to stop.", ephemeral=True)

    @app_commands.command(name="letterboxd-unlink",
                          description="Stop posting your Letterboxd diary entries")
    @app_commands.guild_only()
    async def letterboxd_unlink(self, interaction: discord.Interaction) -> None:
        if await self.svc.unlink(interaction.user.id):
            text = "✅ Unlinked. The bot won't post your new Letterboxd entries any more."
        else:
            text = "You don't have a Letterboxd account linked."
        await interaction.response.send_message(text, ephemeral=True)

    def _feed_mention(self) -> str:
        cid = self.bot.config.channels.get("letterboxd_feed")
        return f"<#{cid}>" if cid else "#letterboxd-feed"


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Letterboxd(bot))
