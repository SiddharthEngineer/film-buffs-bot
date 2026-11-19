"""Member names as plain text (Phase 19).

A `<@id>` mention inside an embed or a Components V2 message only shows a name
if the reader's client already has that member loaded. The phone app often
doesn't (a new member saw a raw ID on the ballot), so bot pages write names as
text instead. Names come from the member cache (the Server Members intent is on
since Phase 19), then the user cache, then one API call per user, remembered.
"""
from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

FALLBACK = "a former member"


def _cache(bot) -> dict[int, str]:
    cache = getattr(bot, "_user_names", None)
    if cache is None:
        cache = {}
        try:
            bot._user_names = cache
        except AttributeError:
            pass
    return cache


async def member_name(bot, user_id: int) -> str:
    """The member's server display name (escaped for markdown)."""
    get_guild = getattr(bot, "get_guild", None)
    guild = get_guild(bot.config.guild_id) if get_guild else None
    member = guild.get_member(user_id) if guild else None
    if member is not None:
        return discord.utils.escape_markdown(member.display_name)
    cache = _cache(bot)
    if user_id in cache:
        return cache[user_id]
    user = getattr(bot, "get_user", lambda _: None)(user_id)
    if user is None and hasattr(bot, "fetch_user"):
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            log.warning("Couldn't look up user %s", user_id)
    name = discord.utils.escape_markdown(user.display_name) if user else FALLBACK
    cache[user_id] = name
    return name


async def member_names(bot, user_ids) -> dict[int, str]:
    return {u: await member_name(bot, u) for u in dict.fromkeys(user_ids)}


def join_names(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
