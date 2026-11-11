"""/admin commands: a few ephemeral fixes for members with Manage Server or the
admin role (server.yaml `ops.admin_role`)."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)


def is_admin(member, admin_role: str) -> bool:
    """Manage Server (or Administrator), or the configured admin role."""
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.manage_guild or perms.administrator):
        return True
    return any(r.name == admin_role for r in getattr(member, "roles", []))


class NotAdmin(app_commands.CheckFailure):
    pass


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user, interaction.client.config.admin_role):
            raise NotAdmin()
        return True
    return app_commands.check(predicate)


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Admin(commands.GroupCog, group_name="admin", group_description="Admin fixes (ephemeral)"):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="resync",
                          description="Re-render a movie post's info card and re-apply its tags")
    @app_commands.describe(post="The movie's pre- or post-watch post (default: this post)")
    @admin_only()
    async def resync(self, interaction: discord.Interaction,
                     post: discord.Thread | None = None) -> None:
        thread_id = post.id if post else interaction.channel_id
        db = self.bot.db
        row = (await db.get_movie_by_thread(thread_id)
               or await db.get_movie_by_spoiler_thread(thread_id))
        if row is None:
            await interaction.response.send_message(
                "❌ That isn't a movie post. Run this inside one, or pick one with `post:`.",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        tmdb_id = row["tmdb_id"]
        movies = self.bot.movies
        if await movies.find_post(tmdb_id) is None:
            await interaction.followup.send(
                f"The pre-watch post for TMDB {tmdb_id} was deleted, so the bot forgot it. "
                "`/addmovie` will recreate it.", ephemeral=True)
            return
        await movies.update_card(tmdb_id)
        await movies.retag(tmdb_id)
        row = await db.get_movie(tmdb_id)
        await self.bot.spoilers.refresh_post(row)
        tags = movies.applied_tags(row["status"], json.loads(row["flags"]),
                                   json.loads(row["genre_tags"]))
        log.info("%s resynced TMDB %s", interaction.user.id, tmdb_id)
        await interaction.followup.send(
            f"✅ Resynced <#{row['thread_id']}>: info card re-rendered, tags "
            f"{', '.join(t.name for t in tags) or '(none)'}.", ephemeral=True)

    @app_commands.command(name="letterboxd-unlink",
                          description="Unlink a member's Letterboxd account")
    @app_commands.describe(member="Whose account to unlink")
    @admin_only()
    async def letterboxd_unlink(self, interaction: discord.Interaction,
                                member: discord.User) -> None:
        link = await self.bot.db.get_link(member.id)
        if link is None or not await self.bot.letterboxd.unlink(member.id):
            await interaction.response.send_message(
                f"{member.mention} has no Letterboxd account linked.", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
            return
        log.info("%s unlinked Letterboxd %s from %s", interaction.user.id,
                 link["username"], member.id)
        await interaction.response.send_message(
            f"✅ Unlinked **{link['username']}** from {member.mention}. Their stored "
            "entries stay; nothing new will be posted.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Admin(bot))
