"""/letterboxd link | sync | unlink, and the background feed poller."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..backfill import Export, ExportError, TitleResolver, parse_export, title_key
from ..letterboxd import (PROFILE_URL, AlreadyLinked, LetterboxdError, UserNotFound,
                          normalize_username)
from ..sync import (SyncPlan, apply_sync, build_sync_plan, change_lines, fmt_duration,
                    preview_lines)
from ..tmdb import TMDBError

if TYPE_CHECKING:
    from ..bot import FilmBuffsBot

log = logging.getLogger(__name__)

MAX_UPLOAD = 25 * 1024 * 1024
PREVIEW_TIMEOUT = 600          # seconds the Apply button stays usable
TOKEN_LIFETIME = 14 * 60       # interaction tokens last 15 minutes; stop editing before that
PROGRESS_EVERY = 5.0           # seconds between progress edits


class Letterboxd(commands.Cog):
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._syncing: set[int] = set()

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
    letterboxd = app_commands.Group(name="letterboxd", guild_only=True,
                                    description="Your Letterboxd account and history")

    @letterboxd.command(name="link",
                        description="Link your Letterboxd account (or see which one is linked)")
    @app_commands.describe(username="Your Letterboxd username or profile link")
    async def link(self, interaction: discord.Interaction,
                   username: app_commands.Range[str, 1, 100] | None = None) -> None:
        uid = interaction.user.id
        if username is None:
            await interaction.response.send_message(await self._status(uid), ephemeral=True)
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
            "post-watch discussion). Entries you logged before now aren't posted; "
            "**/letterboxd sync** with your export brings in your history.\n"
            f"-# Checked about every {self.bot.config.lb_poll_minutes:g} minutes. "
            "**/letterboxd unlink** to stop.", ephemeral=True)

    async def _status(self, uid: int) -> str:
        link = await self.bot.db.get_link(uid)
        if link is None:
            text = ("You haven't linked a Letterboxd account. Run **/letterboxd link** "
                    "with your username.")
        else:
            text = (f"Linked to **{link['username']}** "
                    f"(<{PROFILE_URL.format(username=link['username'])}>).")
            if link["last_error"]:
                text += f"\n-# ⚠️ Last check failed: {link['last_error'][:200]}"
        last = await self.bot.db.last_sync(uid)
        if last is not None:
            ts = int(datetime.fromisoformat(last["synced_at"]).timestamp())
            text += f"\n-# Last export sync: <t:{ts}:D> ({last['username']})."
        return text

    @letterboxd.command(name="unlink", description="Stop posting your Letterboxd diary entries")
    async def unlink(self, interaction: discord.Interaction) -> None:
        if await self.svc.unlink(interaction.user.id):
            text = "✅ Unlinked. The bot won't post your new Letterboxd entries any more."
        else:
            text = "You don't have a Letterboxd account linked."
        await interaction.response.send_message(text, ephemeral=True)

    @letterboxd.command(name="sync",
                        description="Upload your Letterboxd export zip to sync your history, "
                                    "edits and watchlist")
    @app_commands.describe(file="Letterboxd → Settings → Data → Export your data; upload the zip")
    async def sync(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        uid = interaction.user.id
        if uid in self._syncing:
            await interaction.response.send_message(
                "⏳ Your previous sync is still running. Try again when it's done.",
                ephemeral=True)
            return
        if file.size > MAX_UPLOAD:
            await interaction.response.send_message(
                "❌ That file is too big for a Letterboxd export.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._syncing.add(uid)
        handed_off = False
        try:
            try:
                export = await asyncio.to_thread(parse_export, await file.read(), file.filename)
            except ExportError as e:
                await interaction.followup.send(
                    f"❌ Couldn't read it: {e}. Upload the whole zip from Letterboxd → Settings "
                    "→ Data → Export your data.", ephemeral=True)
                return
            problem = await self._check_owner(uid, export)
            if problem:
                await interaction.followup.send(problem, ephemeral=True)
                return
            plan = await self._plan(interaction, uid, export)
            if plan is None:
                return
            if plan.nothing_to_do:
                await interaction.followup.send(
                    "✅ Already in sync: nothing new, changed or removed since last time."
                    + (f"\n-# {plan.counts()['unmatched_films']} films couldn't be matched on "
                       "TMDB; they're stored without posts." if plan.unmatched else ""),
                    ephemeral=True)
                return
            view = SyncView(self, interaction.user, export)
            view.message = await interaction.followup.send(
                preview_text(plan, self.bot.config.tz), view=view, ephemeral=True, wait=True)
            handed_off = True          # the view clears _syncing when it's done
        finally:
            if not handed_off:
                self._syncing.discard(uid)

    async def _check_owner(self, uid: int, export: Export) -> str | None:
        if not export.username:
            return ("❌ That zip has no `profile.csv`, so I can't tell whose export it is. "
                    "Upload the whole zip from Letterboxd.")
        other = await self.bot.db.get_link_by_username(export.username)
        if other is not None and other["discord_user_id"] != uid:
            return (f"❌ That export is from **{export.username}**, which is linked to another "
                    "member. Upload your own export.")
        mine = await self.bot.db.get_link(uid)
        if mine is not None and mine["username"].lower() != export.username:
            return (f"❌ You're linked to **{mine['username']}**, but that export is from "
                    f"**{export.username}**. Upload the export of your linked account.")
        return None

    async def _plan(self, interaction: discord.Interaction, uid: int,
                    export: Export) -> SyncPlan | None:
        db = self.bot.db
        keys = {(r.title, r.year) for r in export.rows}
        uncached = 0
        for t, y in keys:
            uncached += await db.get_title_match(title_key(t, y)) is None
        if uncached > 40:
            await interaction.followup.send(
                f"⏳ Matching {uncached} films with TMDB. This can take a minute or two.",
                ephemeral=True)
        try:
            return await build_sync_plan(db, TitleResolver(db, self.bot.tmdb, concurrency=4),
                                         export, uid, export.username)
        except TMDBError:
            log.warning("TMDB failed during a Letterboxd sync for %s", uid, exc_info=True)
            await interaction.followup.send(
                "❌ TMDB isn't answering right now. Nothing was changed; try again in a few "
                "minutes.", ephemeral=True)
            return None

    async def run_sync(self, interaction: discord.Interaction, export: Export) -> None:
        """After Apply: re-plan (things may have moved since the preview), apply,
        and keep the ephemeral message updated. The final note goes to the
        member by DM if the interaction has expired by then."""
        uid = interaction.user.id
        started = time.monotonic()
        header = f"⏳ **Syncing {export.username}…**"
        last_edit = 0.0

        async def show(text: str, force: bool = False) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - started > TOKEN_LIFETIME or (not force and now - last_edit < PROGRESS_EVERY):
                return
            last_edit = now
            try:
                await interaction.edit_original_response(content=f"{header}\n{text}", view=None)
            except discord.HTTPException:
                pass

        try:
            plan = await self._plan(interaction, uid, export)
            if plan is None:
                return
            await show(f"About {fmt_duration(plan.est_seconds())}. You can dismiss this; "
                       "I'll post a new note here when it's done (or DM you if it takes "
                       "over 15 minutes).", force=True)
            res = await apply_sync(self.bot, plan, via="command", progress=show,
                                   lock=self.svc._locks[uid])
        except Exception:
            log.exception("Letterboxd sync for %s failed", uid)
            await self._final(interaction, started, "⚠️ The sync stopped with an error (the "
                              "admins can see it). What's done is saved; run **/letterboxd sync** "
                              "again to finish.")
            return
        finally:
            self._syncing.discard(uid)
        lines = [f"✅ **Letterboxd sync done** ({export.username})"]
        bits = [(res.stored, "new rows stored"), (res.updated, "updated"),
                (res.removed, "removed"), (res.posts_created, "#movies posts created"),
                (res.reviews_posted, "reviews posted"), (res.embeds_edited, "embeds edited"),
                (res.messages_deleted, "messages deleted")]
        lines.append(" · ".join(f"{n} {label}" for n, label in bits if n) or "Nothing to change.")
        if plan.watchlist is not None:
            lines.append(f"📋 Watchlist: {len({t for t, _, _ in plan.watchlist})} films. Each "
                         "counts toward the rankings and ballot like your 👍 would (shown as 📋 "
                         "in /top and the leaderboard); no 👍 is added to the posts.")
        if res.failed:
            lines.append(f"⚠️ {res.failed} Discord steps failed; run **/letterboxd sync** again "
                         "to retry them.")
        await self._final(interaction, started, "\n".join(lines))

    async def _final(self, interaction: discord.Interaction, started: float, text: str) -> None:
        """The final note as a *new* message: editing the progress message isn't
        enough, because a dismissed ephemeral message still accepts edits (no
        error) but the member never sees them. Past the 15-minute interaction
        window, or if the followup fails, it goes by DM."""
        if time.monotonic() - started < TOKEN_LIFETIME:
            try:
                await interaction.edit_original_response(
                    content=f"{text.splitlines()[0]} · details in the note below.", view=None)
            except discord.HTTPException:
                pass
            try:
                await interaction.followup.send(text, ephemeral=True)
                return
            except discord.HTTPException:
                log.info("Sync note followup to %s failed; sending a DM", interaction.user.id)
        try:
            await interaction.user.send(text)
        except discord.HTTPException:
            log.info("Couldn't send the sync note to %s: %s", interaction.user.id, text)

    def _feed_mention(self) -> str:
        cid = self.bot.config.channels.get("letterboxd_feed")
        return f"<#{cid}>" if cid else "#letterboxd-feed"


def preview_text(plan: SyncPlan, tz) -> str:
    when = ""
    if plan.export.exported_at:
        dt = datetime.fromisoformat(plan.export.exported_at).astimezone(tz)
        when = f" (export from {dt:%b} {dt.day}, {dt.year})"
    lines = [f"📓 **Letterboxd sync for {plan.username}**{when}", *preview_lines(plan)]
    examples = change_lines(plan)
    if examples:
        lines += ["", *examples]
    lines.append(f"-# About {fmt_duration(plan.est_seconds())}. Nothing changes until you "
                 "press **Apply**.")
    text = "\n".join(lines)
    return text if len(text) <= 2000 else text[:1990] + "…"


class SyncView(discord.ui.View):
    def __init__(self, cog: Letterboxd, user: discord.abc.User, export: Export):
        super().__init__(timeout=PREVIEW_TIMEOUT)
        self.cog, self.user, self.export = cog, user, export
        self.message: discord.WebhookMessage | None = None
        self.started = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success, emoji="✅")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.started:
            await interaction.response.defer()
            return
        self.started = True
        self.stop()
        await interaction.response.edit_message(
            content=f"⏳ **Syncing {self.export.username}…** starting.", view=None)
        await self.cog.run_sync(interaction, self.export)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.started:
            await interaction.response.defer()
            return
        self.started = True
        self.stop()
        self.cog._syncing.discard(self.user.id)
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.",
                                                view=None)

    async def on_timeout(self) -> None:
        if self.started:
            return
        self.cog._syncing.discard(self.user.id)
        if self.message is not None:
            try:
                await self.message.edit(content="⌛ This preview expired. Nothing was changed; "
                                        "run **/letterboxd sync** again.", view=None)
            except discord.HTTPException:
                pass


async def setup(bot: "FilmBuffsBot") -> None:
    await bot.add_cog(Letterboxd(bot))
