"""Ping roles on by default (Phase 19, owner 2026-11-18).

Members are notified when the weekly movie (or their group's monthly movie) is
picked, and otherwise only by mentions and posts they follow (the server's
default notifications are "Only @mentions", set by hand). So:
  - a new member gets @Weekly Pings when they join;
  - a member who gets a group's role also gets that group's "<role> Pings" role
    (and loses it when the group role is taken away);
  - once, every current member got the roles they're missing
    (bot_meta PINGS_BACKFILL_META).
Anyone can still turn pings off with 🔔 Pings on the board. Needs the Server
Members intent (on_member_join / on_member_update for other members).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

PINGS_BACKFILL_META = "pings_backfill_done"
REASON = "Pings on by default"


def default_pings(role_ids: set[int], config) -> set[int]:
    """The ping roles a member with these roles should have by default."""
    out = set()
    weekly = config.roles.get("weekly_ping")
    if weekly:
        out.add(weekly)
    for g in config.groups.values():
        if g.role_id in role_ids and g.ping_role_id:
            out.add(g.ping_role_id)
    return out


def group_ping_changes(before: set[int], after: set[int], config) -> tuple[set[int], set[int]]:
    """(ping roles to add, to remove) when a member's group roles change."""
    add, remove = set(), set()
    for g in config.groups.values():
        if not (g.role_id and g.ping_role_id):
            continue
        if g.role_id in after and g.role_id not in before:
            add.add(g.ping_role_id)
        elif g.role_id in before and g.role_id not in after:
            remove.add(g.ping_role_id)
    return add - after, remove & after


class Members(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._backfill())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    def _roles(self, guild: discord.Guild, ids: set[int]) -> list[discord.Role]:
        return [r for r in (guild.get_role(i) for i in ids) if r is not None]

    async def _change(self, member: discord.Member, add: set[int], remove: set[int] = frozenset(),
                      why: str = REASON) -> None:
        try:
            if add:
                await member.add_roles(*self._roles(member.guild, add), reason=why)
            if remove:
                await member.remove_roles(*self._roles(member.guild, remove), reason=why)
        except discord.Forbidden:
            log.error("Can't change ping roles for %s: the bot's role must be above them",
                      member.id)
        except discord.HTTPException:
            log.exception("Changing ping roles for %s failed", member.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild.id != self.bot.config.guild_id:
            return
        have = {r.id for r in member.roles}
        add = default_pings(have, self.bot.config) - have
        if add:
            await self._change(member, add)
            log.info("New member %s: added ping roles %s", member.id, sorted(add))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or after.guild.id != self.bot.config.guild_id:
            return
        add, remove = group_ping_changes({r.id for r in before.roles},
                                         {r.id for r in after.roles}, self.bot.config)
        if add or remove:
            await self._change(after, add, remove, "Group role changed")
            log.info("Member %s group roles changed: +%s -%s pings", after.id, sorted(add),
                     sorted(remove))

    async def _backfill(self) -> None:
        """Once: give every current member the ping roles they're missing."""
        await self.bot.wait_until_ready()
        db = self.bot.db
        if await db.get_meta(PINGS_BACKFILL_META):
            return
        guild = self.bot.get_guild(self.bot.config.guild_id)
        if guild is None:
            return
        if not guild.chunked:
            await guild.chunk()
        n = 0
        for member in list(guild.members):
            if member.bot:
                continue
            have = {r.id for r in member.roles}
            add = default_pings(have, self.bot.config) - have
            if add:
                await self._change(member, add, why="Pings on by default (one-time)")
                n += 1
                await asyncio.sleep(1)       # gentle on the role endpoint
        await db.set_meta(PINGS_BACKFILL_META, "1")
        log.info("Ping roles backfill: updated %d of %d members", n,
                 sum(1 for m in guild.members if not m.bot))


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Members(bot))
