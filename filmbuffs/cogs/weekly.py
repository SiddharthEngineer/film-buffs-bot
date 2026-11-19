"""Weekly and group monthly picks: /nominate, /vote, /schedule, /watchlist show|clear,
the ranked-vote buttons and the 30 s tick. /nominate and /vote open the same private
views as the board's 📋 My nomination and 🗳️ Vote buttons (Phase 20).

The vote commands act on the channel's context: in a private group's category
(Phase 12) they use that group's pool and monthly vote; anywhere else, the
main server's weekly vote."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..movies import EMBED_COLOR
from ..names import join_names
from ..pool import display, post_link
from ..tmdb import TMDBError
from .movies import add_movie, post_note
from ..vote_ui import DYNAMIC_ITEMS, here_film, in_group, send_private

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

LEADERBOARD_META = "leaderboard_planning_message_id"
RETIRED_MONTHLY_META = "retired_monthly_ballots"
PLANNING_CLEANED_META = "planning_cleaned:"     # + pool grp
NOT_MEMBER = "🔒 That's only for members of this group."


def fit_list(head: str, lines: list[str], budget: int) -> str:
    """`head` and as many lines as fit in `budget` chars, then "…and N more"."""
    body = head
    for n, line in enumerate(lines):
        left = len(lines) - n - 1
        note = f"\n…and {left} more" if left else ""
        if len(body) + 1 + len(line) + len(note) > budget:
            return body + f"\n…and {left + 1} more"
        body += "\n" + line
    return body


class Weekly(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)
        self.tick.start()
        self._retire_task = asyncio.create_task(self._retire_leaderboard())
        self._retire_monthly_task = asyncio.create_task(self._retire_main_monthly())
        self._clean_task = asyncio.create_task(self._clean_planning())

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)
        self.tick.cancel()
        self._retire_task.cancel()
        self._retire_monthly_task.cancel()
        self._clean_task.cancel()

    async def _retire_leaderboard(self) -> None:
        """👍 no longer count (owner, 2026-11-15): delete the bot's pinned "🏆 Top 25
        most wanted" / "By genre" message in #watch-planning once."""
        await self.bot.wait_until_ready()
        db = self.bot.db
        mid = await db.get_meta(LEADERBOARD_META)
        ch = self.bot.weekly.channel("watch_planning")
        if not mid or ch is None:
            return
        try:
            await ch.get_partial_message(int(mid)).delete()
            log.info("Deleted the old leaderboard message %s in #watch-planning", mid)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception("Could not delete the old leaderboard message %s", mid)
            return
        await db.set_meta(LEADERBOARD_META, None)

    async def _retire_main_monthly(self) -> None:
        """The main server's monthly vote is gone (owner, 2026-11-15): delete its
        🌙 ballot and the bot's "🌙 Monthly vote … is open" notices in #watch-planning once."""
        await self.bot.wait_until_ready()
        db = self.bot.db
        ids = await db.get_meta(RETIRED_MONTHLY_META)
        ch = self.bot.weekly.channel("watch_planning")
        if not ids or ch is None:
            return
        try:
            for mid in ids.split(","):
                try:
                    await ch.get_partial_message(int(mid)).delete()
                    log.info("Deleted the main monthly ballot %s in #watch-planning", mid)
                except discord.NotFound:
                    pass
            async for m in ch.history(limit=500):
                if m.author.id == self.bot.user.id and m.content.startswith("🌙 **Monthly vote for"):
                    await m.delete()
                    log.info("Deleted the main monthly vote notice %s", m.id)
        except discord.HTTPException:
            log.exception("Could not delete the main monthly vote messages")
            return
        await db.set_meta(RETIRED_MONTHLY_META, None)

    async def _clean_planning(self) -> None:
        """Phase 19: each #watch-planning keeps only the board. Once per channel,
        delete the bot's older messages there (past ballots, pick announcements,
        "vote is open" notices). Members' messages are left alone and counted."""
        await self.bot.wait_until_ready()
        db = self.bot.db
        for svc in [self.bot.weekly, *self.bot.group_votes.values()]:
            key = PLANNING_CLEANED_META + svc.pool.grp
            ch = svc.board_channel()
            if ch is None or await db.get_meta(key):
                continue
            try:
                await svc.refresh_board()
                board = int(await db.get_meta(svc.board_meta) or 0)
                deleted = members = 0
                async for m in ch.history(limit=None):
                    if m.id == board:
                        continue
                    if m.author.id != self.bot.user.id:
                        members += m.type == discord.MessageType.default
                        continue
                    await m.delete()
                    deleted += 1
                    await asyncio.sleep(1)
            except discord.HTTPException:
                log.exception("Cleaning #watch-planning (%s) failed; will retry next start",
                              svc.pool.grp)
                continue
            await db.set_meta(key, "1")
            log.info("#watch-planning (%s): deleted %d old bot message(s); %d member "
                     "message(s) left alone", svc.pool.grp, deleted, members)

    def _context(self, interaction: discord.Interaction):
        """(pool, group vote or None, error) for the channel the command was used in."""
        grp = self.bot.group_of(interaction.channel)
        if grp is None:
            return self.bot.pool, None, None
        svc = self.bot.group_votes.get(grp)
        if svc is None:
            return None, None, "That group isn't set up yet."
        if not in_group(self.bot, interaction.user, grp):
            return None, None, NOT_MEMBER
        return svc.pool, svc, None

    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        try:
            await self.bot.weekly.tick()
        except Exception:
            log.exception("Weekly pick tick failed")

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    # helpers ---------------------------------------------------------------
    async def _title_autocomplete(self, interaction: discord.Interaction, current: str):
        query = current.strip()
        if len(query) < 2:
            return []
        try:
            pairs = await self.bot.tmdb.autocomplete(query)
        except TMDBError as e:
            log.warning("Autocomplete search failed: %s", e)
            return []
        return [app_commands.Choice(name=label, value=value) for label, value in pairs[:25]]

    # commands --------------------------------------------------------------
    @app_commands.command(name="nominate",
                          description="See your nomination, or nominate a movie for the ballot")
    @app_commands.describe(title="Start typing and pick the movie (replaces yours). "
                                 "Leave it out to see yours and pick there")
    @app_commands.guild_only()
    async def nominate(self, interaction: discord.Interaction, title: str | None = None) -> None:
        pool, _, err = self._context(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        here = await here_film(interaction)
        if not title:
            # Same private view as the board's 📋 My nomination button.
            text, view = await pool.my_view(interaction.user.id, here=here)
            await send_private(interaction, text, view)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Everything /addmovie does: TMDB lookup, and a #pre-watch post if there's none.
        movie, thread, created, err = await add_movie(self.bot, interaction.user, title,
                                                      source="nominate")
        if err:
            text, view = await pool.my_view(interaction.user.id, notice=err, here=here)
        else:
            reply = await pool.nominate(interaction.user.id, movie.id)
            text = post_note(movie, thread, created) + "\n" + reply
            view = await pool.my_controls(interaction.user.id, here=here)
        await interaction.followup.send(text[:2000], view=view, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @nominate.autocomplete("title")
    async def _nominate_title(self, interaction: discord.Interaction, current: str):
        return await self._title_autocomplete(interaction, current)

    @app_commands.command(name="vote", description="Rank the films on the open ballot (private)")
    @app_commands.guild_only()
    async def vote(self, interaction: discord.Interaction) -> None:
        _, group, err = self._context(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        svc = group or self.bot.weekly
        period = await svc.voting_period()
        if period is None:
            await interaction.response.send_message(
                "No vote is open right now. **/schedule** shows what's on.", ephemeral=True)
            return
        # Same private picker as the board's 🗳️ Vote button.
        text, view = await svc.picker(period, interaction.user.id)
        await send_private(interaction, text, view)

    @app_commands.command(name="schedule",
                          description="What's on, the open vote and your nomination")
    @app_commands.guild_only()
    async def schedule(self, interaction: discord.Interaction) -> None:
        pool, group, err = self._context(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if group is None:
            text = await self.bot.weekly.schedule_text(interaction.user.id)
        else:
            from datetime import timezone
            lines = await group.schedule_lines(interaction.user.id,
                                               datetime.now(timezone.utc))
            text = "\n".join(lines + ["", await pool._summary(interaction.user.id)])
        await interaction.response.send_message(text[:2000], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    watchlist = app_commands.Group(name="watchlist", guild_only=True,
                                   description="Your uploaded Letterboxd watchlist")

    @watchlist.command(name="show",
                       description="Your watchlist upload, or the films on everyone's watchlists")
    @app_commands.describe(user1="Someone to compare with: shows the films on all your watchlists",
                           user2="Another person to compare with", user3="Another person",
                           user4="Another person")
    async def watchlist_show(self, interaction: discord.Interaction,
                             user1: discord.Member | None = None,
                             user2: discord.Member | None = None,
                             user3: discord.Member | None = None,
                             user4: discord.Member | None = None) -> None:
        others = [m for m in (user1, user2, user3, user4) if m is not None]
        if not others:
            await self._watchlist_mine(interaction)
            return
        people = list({m.id: m for m in [interaction.user, *others]}.values())
        if len(people) == 1:
            await self._watchlist_mine(interaction)
            return
        await self._watchlist_shared(interaction, people)

    async def _watchlist_mine(self, interaction: discord.Interaction) -> None:
        imp, n, posted = await self.bot.db.watchlist_summary(interaction.user.id)
        if imp is None and n == 0:
            await interaction.response.send_message(
                "You haven't uploaded a watchlist. **/letterboxd sync** takes your Letterboxd "
                "export zip (optional). Its films get a post in #pre-watch if they have none, and "
                "only break ties in the votes.", ephemeral=True)
            return
        when = f"<t:{int(datetime.fromisoformat(imp['imported_at']).timestamp())}:D>" if imp else "?"
        unmatched = json.loads(imp["unmatched"]) if imp else []
        await interaction.response.send_message(
            f"📋 Uploaded {when}: {n} films, {posted} with a post."
            + (f" {len(unmatched)} couldn't be matched." if unmatched else "")
            + "\n-# **/letterboxd sync** again to update it, **/watchlist clear** to remove it. "
            "Add people to **/watchlist show** to see the films on all your watchlists.",
            ephemeral=True)

    async def _watchlist_shared(self, interaction: discord.Interaction, people: list) -> None:
        """The films on every listed member's uploaded watchlist (you're always included)."""
        db = self.bot.db
        missing = []
        for m in people:
            _, n, _ = await db.watchlist_summary(m.id)
            if n == 0:
                missing.append("You" if m.id == interaction.user.id else m.display_name)
        if missing:
            await interaction.response.send_message(
                f"{join_names(missing)} {'have' if len(missing) > 1 or missing == ['You'] else 'has'}"
                " no uploaded watchlist, so there's nothing to compare. **/letterboxd sync** "
                "takes a Letterboxd export zip.", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
            return
        films = await db.shared_watchlist([m.id for m in people])
        names = join_names(["you"] + [m.display_name for m in people
                                      if m.id != interaction.user.id])
        title = f"📋 On every watchlist: {len(films)} film{'s' * (len(films) != 1)}"
        if not films:
            body = f"No film is on all the watchlists of {names}."
        else:
            gid = self.bot.config.guild_id
            lines = [f"• {post_link(gid, f)}" if f["thread_id"] else f"• {display(f)}"
                     for f in films]
            body = fit_list(f"Films on the watchlists of {names}:", lines, 4000)
        embed = discord.Embed(title=title[:256], description=body, color=EMBED_COLOR)
        embed.set_footer(text="From uploaded Letterboxd watchlists (/letterboxd sync)")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @watchlist.command(name="clear", description="Remove your uploaded watchlist")
    async def watchlist_clear(self, interaction: discord.Interaction) -> None:
        n = await self.bot.db.clear_watchlist(interaction.user.id)
        await interaction.response.send_message(
            f"✅ Removed {n} films." if n
            else "You have no uploaded watchlist.", ephemeral=True)


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Weekly(bot))
