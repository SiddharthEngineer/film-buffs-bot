"""/admin commands: a few ephemeral fixes for members with Manage Server or the
optional admin role (server.yaml `ops.admin_role`; null = none)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..ideas import choice_name
from ..pool import post_link
from ..tmdb import TMDBError, parse_choice

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

SLOTS = [app_commands.Choice(name="This week / month", value="this"),
         app_commands.Choice(name="Next week / month", value="next")]
GROUP_HELP = "main (the weekly vote) or a private group (its monthly vote)"


def is_admin(member, admin_role: str | None = None) -> bool:
    """Manage Server (or Administrator), or the configured admin role if any."""
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.manage_guild or perms.administrator):
        return True
    if not admin_role:
        return False
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
    @app_commands.describe(post="The movie's #pre-watch or post-watch post (default: this post)")
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
                f"The #pre-watch post for TMDB {tmdb_id} was deleted, so the bot forgot it. "
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
            f"✅ Resynced {post_link(self.bot.config.guild_id, row)}: info card re-rendered, tags "
            f"{', '.join(t.name for t in tags) or '(none)'}.", ephemeral=True)

    def _service(self, group: str):
        """The vote service for /admin's group option, or None if unknown."""
        if group == "main":
            return self.bot.weekly
        return self.bot.group_votes.get(group)

    async def _group_choices(self, interaction: discord.Interaction, current: str):
        out = [app_commands.Choice(name="main (weekly vote)", value="main")]
        out += [app_commands.Choice(name=f"{g.pool.name} (monthly vote)", value=k)
                for k, g in self.bot.group_votes.items()]
        cur = current.lower()
        return [c for c in out if cur in c.name.lower()][:25]

    async def _unknown_group(self, interaction: discord.Interaction, group: str) -> bool:
        if self._service(group) is not None:
            return False
        await interaction.response.send_message(
            f"❌ Unknown group {group!r}. Pick one from the list.", ephemeral=True)
        return True

    @app_commands.command(name="pick", description="Set a week's or month's movie (closes an open vote early)")
    @app_commands.describe(group=GROUP_HELP, slot="This or next week (main) / month (a group)",
                           title="Start typing and pick the movie")
    @app_commands.choices(slot=SLOTS)
    @admin_only()
    async def pick(self, interaction: discord.Interaction, group: str,
                   slot: app_commands.Choice[str], title: str) -> None:
        if await self._unknown_group(interaction, group):
            return
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
            await self.bot.movies.ensure_post(movie, added_by=interaction.user,
                                              status="backlog", source="admin")
        reply = await self._service(group).admin_pick(interaction.user.id, slot.value, tmdb_id)
        await interaction.followup.send(reply, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="clear", description="Undo a weekly or monthly pick (no movie then)")
    @app_commands.describe(group=GROUP_HELP, slot="This or next week (main) / month (a group)")
    @app_commands.choices(slot=SLOTS)
    @admin_only()
    async def clear(self, interaction: discord.Interaction, group: str,
                    slot: app_commands.Choice[str]) -> None:
        if await self._unknown_group(interaction, group):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = await self._service(group).admin_clear(interaction.user.id, slot.value)
        await interaction.followup.send(reply, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="discussion",
                          description="Move a group's monthly discussion event (Central time)")
    @app_commands.describe(group="Which private group", month="Whose discussion",
                           date="YYYY-MM-DD", time="HH:MM, 24-hour (default 20:00)")
    @app_commands.choices(month=[app_commands.Choice(name="This month's movie", value="this"),
                                 app_commands.Choice(name="Next month's movie", value="next")])
    @admin_only()
    async def discussion(self, interaction: discord.Interaction, group: str,
                         month: app_commands.Choice[str], date: str, time: str = "20:00") -> None:
        svc = self.bot.group_votes.get(group)
        if svc is None:
            await interaction.response.send_message(
                "❌ Pick a private group from the list (the weekly vote has no discussion event).",
                ephemeral=True)
            return
        tz = self.bot.config.tz
        try:
            start = datetime.strptime(f"{date.strip()} {time.strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            await interaction.response.send_message(
                "❌ Use `date:` like 2027-01-02 and `time:` like 19:30.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = await svc.reschedule(month.value, start)
        log.info("%s rescheduled the %s %s discussion to %s", interaction.user.id, group,
                 month.value, start)
        await interaction.followup.send(reply, ephemeral=True)

    @pick.autocomplete("group")
    async def _pick_group(self, interaction: discord.Interaction, current: str):
        return await self._group_choices(interaction, current)

    @clear.autocomplete("group")
    async def _clear_group(self, interaction: discord.Interaction, current: str):
        return await self._group_choices(interaction, current)

    @discussion.autocomplete("group")
    async def _discussion_group(self, interaction: discord.Interaction, current: str):
        return [c for c in await self._group_choices(interaction, current) if c.value != "main"]

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

    # /admin idea done|drop (Phase 14) -----------------------------------------
    idea = app_commands.Group(name="idea", description="Take an idea off the #ideas list")

    async def _close_idea(self, interaction: discord.Interaction, idea: str, status: str) -> None:
        row = await self.bot.db.get_idea(int(idea)) if idea.strip().isdigit() else None
        if row is None or row["status"] != "active":
            await interaction.response.send_message(
                "❌ Pick an active idea from the list.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.bot.ideas.close(row["id"], status, interaction.user.id):
            await interaction.followup.send("That idea was just closed by someone else.",
                                            ephemeral=True)
            return
        verb = "✅ Marked done" if status == "done" else "🗑️ Dropped"
        await interaction.followup.send(f"{verb}: #{row['id']} {row['text']}. It's off the "
                                        "pinned list.", ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @idea.command(name="done", description="Mark an idea as done (built)")
    @app_commands.describe(idea="Start typing and pick the idea")
    @admin_only()
    async def idea_done(self, interaction: discord.Interaction, idea: str) -> None:
        await self._close_idea(interaction, idea, "done")

    @idea.command(name="drop", description="Drop an idea (not doing it)")
    @app_commands.describe(idea="Start typing and pick the idea")
    @admin_only()
    async def idea_drop(self, interaction: discord.Interaction, idea: str) -> None:
        await self._close_idea(interaction, idea, "dropped")

    async def _idea_choices(self, interaction: discord.Interaction, current: str):
        cur = current.lower().strip()
        return [app_commands.Choice(name=choice_name(r), value=str(r["id"]))
                for r in await self.bot.db.active_ideas()
                if cur in f"#{r['id']} {r['text']}".lower()][:25]

    @idea_done.autocomplete("idea")
    async def _idea_done_ac(self, interaction: discord.Interaction, current: str):
        return await self._idea_choices(interaction, current)

    @idea_drop.autocomplete("idea")
    async def _idea_drop_ac(self, interaction: discord.Interaction, current: str):
        return await self._idea_choices(interaction, current)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Admin(bot))
