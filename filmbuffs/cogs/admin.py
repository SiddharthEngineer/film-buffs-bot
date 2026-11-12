"""/admin commands: a few ephemeral fixes for members with Manage Server or the
admin role (server.yaml `ops.admin_role`)."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..tmdb import TMDBError, parse_choice

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
    @app_commands.describe(post="The movie's #movies or post-watch post (default: this post)")
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
                f"The #movies post for TMDB {tmdb_id} was deleted, so the bot forgot it. "
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

    @app_commands.command(name="pick", description="Set this or next week's movie (closes an open vote early)")
    @app_commands.describe(week="Which watch week", title="Start typing and pick the movie")
    @app_commands.choices(week=[app_commands.Choice(name="This week", value="this"),
                                app_commands.Choice(name="Next week", value="next")])
    @admin_only()
    async def pick(self, interaction: discord.Interaction, week: app_commands.Choice[str],
                   title: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        tmdb_id = parse_choice(title)
        if tmdb_id is None:
            await interaction.followup.send("❌ Pick the movie from the autocomplete list.",
                                            ephemeral=True)
            return
        if await self.bot.db.get_movie(tmdb_id) is None:
            try:
                movie = await self.bot.tmdb.movie(tmdb_id)
            except TMDBError:
                await interaction.followup.send("❌ Couldn't reach TMDB. Try again in a minute.",
                                                ephemeral=True)
                return
            if movie is None:
                await interaction.followup.send(f"❌ TMDB has no movie with ID {tmdb_id}.",
                                                ephemeral=True)
                return
            await self.bot.movies.ensure_post(movie, added_by=interaction.user, pitch=None,
                                              status="backlog", source="admin")
        reply = await self.bot.weekly.admin_pick(interaction.user.id, week.value, tmdb_id)
        await interaction.followup.send(reply, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @pick.autocomplete("title")
    async def _pick_title(self, interaction: discord.Interaction, current: str):
        query = current.strip()
        if len(query) < 2:
            return []
        try:
            pairs = await self.bot.tmdb.autocomplete(query)
        except TMDBError:
            return []
        return [app_commands.Choice(name=label, value=value) for label, value in pairs[:25]]

    @app_commands.command(name="availability",
                          description="Re-check In Theaters / Streaming for every movie now")
    @admin_only()
    async def availability(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("%s started an availability check", interaction.user.id)
        report = await self.bot.availability.run()
        await interaction.followup.send(f"✅ In Theaters / Streaming: {report.summary()}.",
                                        ephemeral=True)

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
