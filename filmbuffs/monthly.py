"""Group monthly movie (Phases 10, 12): each private group's own vote.

Every group in server.yaml `groups:` has one GroupVoteService. For month M
(America/Chicago), from the group's own nomination pool:
  vote        1st of M-1 12:00 AM → last day of M-1 11:59 PM (instant runoff).
              The next month's vote opens as soon as this one closes.
  watch       any time during M.
  discussion  a voice event in the group's #voice on the 1st of M+1 at the
              group's discussion_time. Admins can move it (edit the event in
              Discord, or /admin discussion); the bot never resets its time.
  end         at the end of M the month is marked done.

The board is the one message in the group's #watch-planning; picks and notices go to
the group's #chat (Phase 19) and ping only the group's
opt-in ping role. A group's pick never changes the film's tags or status in #pre-watch, is
never logged in #watch-log and never announced in #announcements. The group
can't pick the same film twice (its months with a winner are its history);
other groups and the weekly vote don't matter.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

from .db import utcnow
from .movies import truncate
from .pool import PoolService, RankedTrack, display, shared_lock
from .weekly import (SOLE_NOMINATION, how_picked, instant_runoff, local_midnight, runoff_text,
                     when, won_text)

if TYPE_CHECKING:
    from .bot import FilmBuffsBot
    from .groups import Group

log = logging.getLogger(__name__)
esc = discord.utils.escape_markdown


def add_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)


def month_key(d: date) -> str:
    return f"{d:%Y-%m}"


def parse_month(key: str) -> date:
    return date.fromisoformat(key + "-01")


def month_name(d: date | str) -> str:
    d = parse_month(d) if isinstance(d, str) else d
    return f"{d:%B %Y}"


@dataclass(frozen=True)
class MonthSchedule:
    month: date                    # first day of the watch month
    tz: ZoneInfo
    discussion_at: time = time(20, 0)

    @property
    def vote_open(self) -> datetime:          # 1st of the month before, 12:00 AM
        return local_midnight(add_months(self.month, -1), self.tz)

    @property
    def watch_start(self) -> datetime:
        return local_midnight(self.month, self.tz)

    @property
    def vote_close(self) -> datetime:         # last day of the month before, 11:59 PM
        return self.watch_start - timedelta(minutes=1)

    @property
    def watch_end(self) -> datetime:          # last day of the month, 11:59 PM
        return local_midnight(add_months(self.month, 1), self.tz) - timedelta(minutes=1)

    @property
    def discussion(self) -> datetime:         # default: the 1st of the next month
        return datetime.combine(add_months(self.month, 1), self.discussion_at, tzinfo=self.tz)


def months_to_create(now: datetime, tz: ZoneInfo, first: date | None,
                     existing: set[str], discussion_at: time = time(20, 0)) -> list[date]:
    """The month whose vote should be open now, if it has no row yet (none before
    `first`, the group's first month; no `first` means start right away)."""
    local = now.astimezone(tz).date()
    m = add_months(local.replace(day=1), 1)
    s = MonthSchedule(m, tz, discussion_at)
    if (first is None or m >= first) and month_key(m) not in existing and s.vote_open <= now < s.vote_close:
        return [m]
    return []


def next_month_action(state: str, s: MonthSchedule, now: datetime) -> str | None:
    if state == "voting" and now >= s.vote_close:
        return "close_vote"
    if state == "picked" and now >= s.watch_end:
        return "end_month"
    return None


class GroupVoteService(RankedTrack):
    emoji = "🌙"
    unit = "month"

    def __init__(self, bot: "FilmBuffsBot", group: "Group"):
        self.bot = bot
        self.group = group
        self.grp = group.key
        self.track = group.track
        self.label = f"{group.name} monthly"
        self.pool = PoolService(bot, group.key, group.track, group.name)
        self.lock = shared_lock(bot)
        self._rendered: dict[int, str] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def tz(self) -> ZoneInfo:
        return self.bot.config.tz

    @property
    def cfg(self):
        return self.bot.config

    def sched(self, month: str | date) -> MonthSchedule:
        d = parse_month(month) if isinstance(month, str) else month
        return MonthSchedule(d, self.tz, self.group.discussion_at)

    def channel(self, part: str):
        cid = self.group.channels.get(part)
        return self.bot.get_channel(cid) if cid else None

    def ping(self) -> tuple[str, discord.AllowedMentions]:
        """The group's opt-in ping role (members who clicked 🔔 Pings)."""
        rid = self.group.ping_role_id
        if not rid:
            return "", discord.AllowedMentions.none()
        return f"<@&{rid}> ", discord.AllowedMentions(everyone=False, users=False,
                                                       roles=[discord.Object(rid)])

    async def _safe(self, coro, what: str):
        try:
            return await coro
        except discord.HTTPException:
            log.exception("%s monthly movie: %s failed", self.grp, what)
            return None

    async def _post(self, text: str, what: str, mentions=None):
        """Group news goes to the group's #chat (Phase 19: #watch-planning is just
        the board)."""
        ch = self.channel("chat")
        if ch is None:
            return None
        return await self._safe(ch.send(truncate(text, 2000),
                                        allowed_mentions=mentions or discord.AllowedMentions.none()),
                                what)

    # RankedTrack -----------------------------------------------------------------
    def close_time(self, period: str) -> datetime:
        return self.sched(period).vote_close

    def title(self, period: str) -> str:
        return month_name(period)

    async def open_row(self, period: str, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        row = await self.bot.db.get_month(self.grp, period)
        if row is None or row["state"] != "voting" or now >= self.sched(period).vote_close:
            return None
        return row

    async def refresh_open(self) -> None:
        await self.refresh_board()

    # tick (called by WeeklyService.tick with the lock held) ------------------------
    async def tick_locked(self, now: datetime) -> None:
        db = self.bot.db
        await self.pool.sync()
        for row in await db.months_in_state(self.grp, "voting", "picked"):
            await self._advance(row["month"], now)
        for m in months_to_create(now, self.tz, self.group.first_month,
                                  await db.all_months(self.grp), self.group.discussion_at):
            await db.create_month(self.grp, month_key(m))
            log.info("Opened the %s monthly vote for %s", self.grp, month_key(m))
            await self._safe(self.opened(month_key(m)), "monthly vote notice")

    async def _advance(self, month: str, now: datetime) -> None:
        for _ in range(3):
            row = await self.bot.db.get_month(self.grp, month)
            action = next_month_action(row["state"], self.sched(month), now)
            if action is None:
                return
            try:
                await getattr(self, action)(row, now)
            except Exception:
                log.exception("%s monthly movie: %s failed for %s", self.grp, action, month)
                return

    async def opened(self, month: str) -> None:
        """The board switches to the new vote; no separate notice (Phase 19)."""
        await self.refresh_board()

    # board -------------------------------------------------------------------------
    accent = 0x5865F2

    def board_channel(self):
        ch = self.channel("watch_planning")
        return ch if isinstance(ch, discord.TextChannel) else None

    async def voting_period(self) -> str | None:
        now = datetime.now(timezone.utc)
        open_ = [r["month"] for r in await self.bot.db.months_in_state(self.grp, "voting")
                 if now < self.sched(r["month"]).vote_close]
        return min(open_) if open_ else None

    async def legacy_ballot_id(self) -> int | None:
        period = await self.voting_period()
        row = await self.bot.db.get_month(self.grp, period) if period else None
        return row["ballot_message_id"] if row else None

    def where(self) -> str:
        vc = self.group.channels.get("voice")
        return f"in <#{vc}>" if vc else "in the group's voice channel"

    def board_header(self, period: str | None, voters: int) -> tuple[str, str, str]:
        g = self.group.name
        footer = ("-# Separate from the server's weekly vote. **/nominate** a film here, then "
                  "**Vote**. **/help** explains how it works.")
        if period is None:
            return (f"## 🌙 {g} monthly movie", "No vote is open right now.", footer)
        s = self.sched(period)
        who = f"**{voters}** member{' has' if voters == 1 else 's have'} voted"
        return (f"## 🌙 {g}'s movie for {month_name(period)}",
                f"🗳️ Voting closes **{when(s.vote_close, self.tz)}** · {who}.\n"
                f"🍿 Watch it any time in {s.month:%B}, then talk it over on "
                f"**{when(s.discussion, self.tz)}** {self.where()}.", footer)

    # counting ----------------------------------------------------------------------
    async def _candidates(self, films) -> list[dict]:
        out = []
        for f in films:
            out.append({"tmdb_id": f["tmdb_id"],
                        "interest": await self.bot.db.movie_interest(f["tmdb_id"]),
                        "added_at": f["added_at"], "nominated_by": f["user_id"]})
        return out

    async def _ballots(self, ids: set[int]) -> dict[int, list[int]]:
        everyone = await self.bot.db.all_track_rankings(self.track)
        out = {u: [t for t in ts if t in ids] for u, ts in everyone.items()}
        return {u: ts for u, ts in out.items() if ts}

    async def close_vote(self, row, now: datetime) -> None:
        month = row["month"]
        db = self.bot.db
        films = await self.ballot_films()
        cands = await self._candidates(films)
        ballots = await self._ballots({c["tmdb_id"] for c in cands})
        result = instant_runoff(list(ballots.values()), cands, self.cfg.tie_break) \
            if len(cands) > 1 else None
        await db.snapshot_month(self.grp, month, cands, ballots,
                                result["first_choices"] if result else {})
        await db.update_month(self.grp, month, vote_closed_at=utcnow(), total_votes=len(ballots))
        by_id = {f["tmdb_id"]: f for f in films}
        if not cands:
            await self.skip(row, "nobody nominated a film")
            return
        if len(cands) == 1:
            await self._crown(row, by_id[cands[0]["tmdb_id"]], now, how=SOLE_NOMINATION,
                              line="It was the only film on the ballot, so it wins without a vote.")
            return
        if result is None:
            await self.skip(row, "nobody voted")
            return
        w = by_id[result["winner"]]
        won, ru, n_rounds = result["winner_votes"], result["runner_up_votes"], len(result["rounds"])
        voters = result["voters"]
        how = won_text(won, ru, n_rounds)
        names = {t: display(f) for t, f in by_id.items()}
        await db.update_month(self.grp, month, vote_rounds=json.dumps(result["rounds"]),
                              winner_votes=won, runner_up_votes=ru, total_votes=voters)
        await self._crown(
            row, w, now, how=how,
            line=(f"Winner of the vote ({how}, {voters} voter{'s' * (voters != 1)}).\n-# Instant "
                  f"runoff: each round, every ballot counts for its highest-ranked film still in "
                  f"the race.\n" + runoff_text(result, names)))

    async def _crown(self, row, w, now: datetime, *, how: str, line: str) -> None:
        """The month's film: it leaves the pool; event in #voice; announced in
        #chat. Its #pre-watch post is left alone."""
        month = row["month"]
        db = self.bot.db
        tmdb_id = w["tmdb_id"]
        await db.update_month(self.grp, month, winner_tmdb_id=tmdb_id)
        await self.pool.picked(tmdb_id)
        winner = {"tmdb_id": tmdb_id, "title": display(w), "thread_id": w["thread_id"]}
        event = await self._safe(self.create_event(month, winner, now), "create event")
        await db.update_month(self.grp, month, state="picked", event_id=event.id if event else None)
        msg = await self._safe(self.announce(month, winner, line, event), "announce")
        if msg:
            await db.update_month(self.grp, month, announcement_id=msg.id)
        log.info("%s monthly movie %s: %s, %s", self.grp, month, winner["title"], how)
        await self._safe(self.refresh_board(), "board refresh")

    async def announce(self, month: str, winner: dict, line: str, event):
        s = self.sched(month)
        ping, mentions = self.ping()
        start = event.start_time if event else s.discussion
        nxt = add_months(s.month, 1)
        url = f"https://discord.com/channels/{self.cfg.guild_id}/{winner['thread_id']}"
        text = (f"{ping}🌙 **{self.group.name}'s movie for {month_name(month)}: "
                f"[{esc(winner['title'])}]({url})**\n"
                f"Watch it any time in {s.month:%B}, then join the discussion on "
                f"**{when(start, self.tz)}** {self.where()}."
                + (f"\n📅 Event: {event.url}" if event else "")
                + f"\n{line}\n-# The vote for {month_name(nxt)} is open now.")
        return await self._post(text, "announcement", mentions)

    async def create_event(self, month: str, winner: dict, now: datetime,
                           start: datetime | None = None):
        s = self.sched(month)
        guild = self.bot.get_guild(self.cfg.guild_id)
        vc = self.channel("voice")
        start = start or s.discussion
        if guild is None or vc is None or start <= now + timedelta(minutes=5):
            return None
        url = f"https://discord.com/channels/{guild.id}/{winner['thread_id']}"
        return await guild.create_scheduled_event(
            name=truncate(f"🌙 {self.group.name} movie discussion: {winner['title']}", 100),
            start_time=start,
            end_time=start + timedelta(minutes=self.group.discussion_minutes),
            channel=vc, entity_type=discord.EntityType.voice,
            privacy_level=discord.PrivacyLevel.guild_only,
            description=truncate(f"{self.group.name}'s movie for {month_name(month)}: "
                                 f"{winner['title']}. Watch it any time in {s.month:%B}, then "
                                 f"come talk about it. Spoilers welcome. Post: {url}", 1000),
            reason=f"{self.group.name} monthly movie")

    async def end_month(self, row, now: datetime) -> None:
        await self.bot.db.update_month(self.grp, row["month"], state="done", ended_at=utcnow())
        log.info("%s monthly movie %s ended", self.grp, row["month"])

    async def skip(self, row, reason: str) -> None:
        month = row["month"]
        await self.bot.db.update_month(self.grp, month, state="skipped", skip_reason=reason)
        log.info("%s monthly movie %s skipped: %s", self.grp, month, reason)
        await self._post(f"⏭️ No {self.group.name} movie for {month_name(month)}: {reason}.",
                         "skip notice")
        await self._safe(self.refresh_board(), "board refresh")

    # admin -------------------------------------------------------------------------
    def which_month(self, which: str, now: datetime) -> str:
        local = now.astimezone(self.tz).date().replace(day=1)
        return month_key(local if which == "this" else add_months(local, 1))

    async def _event(self, event_id: int | None):
        if not event_id:
            return None
        guild = self.bot.get_guild(self.cfg.guild_id)
        if guild is None:
            return None
        try:
            return guild.get_scheduled_event(event_id) or await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            return None

    async def admin_pick(self, admin_id: int, which: str, tmdb_id: int,
                         now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        db = self.bot.db
        month = self.which_month(which, now)
        name = month_name(month)
        g = self.group.name
        async with self.lock:
            movie = await db.get_movie(tmdb_id)
            if movie is None:
                return "❌ That movie has no post."
            row = await db.get_month(self.grp, month)
            if row and row["winner_tmdb_id"] == tmdb_id and row["state"] == "picked":
                return f"**{display(movie)}** is already {g}'s movie for {name}."
            watched = await db.group_watched(self.grp)
            if tmdb_id in watched and watched[tmdb_id] != month:
                return f"❌ {g} already picked **{display(movie)}** ({month_name(watched[tmdb_id])})."
            if row is None:
                await db.create_month(self.grp, month)
                row = await db.get_month(self.grp, month)
            if row["state"] == "done":
                return f"❌ {name} is already over."
            notes = []
            if row["state"] == "voting":
                films = await self.ballot_films()
                ballots = await self._ballots({f["tmdb_id"] for f in films})
                await db.snapshot_month(self.grp, month, await self._candidates(films), ballots, {})
                n = len(ballots)
                notes.append(f"closed voting early ({n} ranking{'s' * (n != 1)} kept; they carry "
                             f"over to the next vote)")
                await db.update_month(self.grp, month, vote_closed_at=utcnow(), total_votes=n)
            await db.update_month(self.grp, month, state="picked", winner_tmdb_id=tmdb_id,
                                  winner_votes=None, runner_up_votes=None, override_by=admin_id,
                                  skip_reason=None)
            await self.pool.picked(tmdb_id)
            winner = {"tmdb_id": tmdb_id, "title": display(movie), "thread_id": movie["thread_id"]}
            event = await self._event(row["event_id"])
            if event is not None:      # keep its time (an admin may have moved it)
                event = await self._safe(event.edit(
                    name=truncate(f"🌙 {g} movie discussion: {winner['title']}", 100),
                    reason="Monthly pick changed by an admin"), "edit event")
            else:
                event = await self._safe(self.create_event(month, winner, now), "create event")
            await db.update_month(self.grp, month, event_id=event.id if event else None)
            await self._safe(self.announce(month, winner, "Picked by an admin.", event), "announce")
            log.info("%s picked TMDB %s for %s %s", admin_id, tmdb_id, self.grp, month)
        await self._safe(self.refresh_board(), "board refresh")
        self.pool.refresh_soon()
        extra = f" ({'; '.join(notes)})" if notes else ""
        return f"✅ **{display(movie)}** is {g}'s movie for {name}{extra}."

    async def admin_clear(self, admin_id: int, which: str, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        db = self.bot.db
        month = self.which_month(which, now)
        name = month_name(month)
        g = self.group.name
        async with self.lock:
            row = await db.get_month(self.grp, month)
            if row is None or row["state"] != "picked" or not row["winner_tmdb_id"]:
                return f"❌ {g} has no movie set for {name}."
            tmdb_id = row["winner_tmdb_id"]
            movie = await db.get_movie(tmdb_id)
            notes = []
            event = await self._event(row["event_id"])
            if event is not None:
                try:
                    await event.delete(reason="Monthly pick cleared by an admin")
                    notes.append("event deleted")
                except discord.HTTPException:
                    log.exception("%s monthly movie: delete event failed", self.grp)
            await db.update_month(self.grp, month, state="skipped",
                                  skip_reason="cleared by an admin", winner_tmdb_id=None,
                                  event_id=None, override_by=admin_id)
            title = display(movie) if movie else f"TMDB {tmdb_id}"
            await self._post(f"⏭️ **{title}** is no longer {g}'s movie for {name}.", "clear notice")
        await self._safe(self.refresh_board(), "board refresh")
        self.pool.refresh_soon()
        notes.append(f"{g} can nominate it again")
        return f"✅ Cleared **{title}** for {g}, {name} ({'; '.join(notes)})."

    async def reschedule(self, which: str, start_local: datetime,
                         now: datetime | None = None) -> str:
        """Move the group's discussion event (start_local is aware, America/Chicago)."""
        now = now or datetime.now(timezone.utc)
        month = self.which_month(which, now)
        row = await self.bot.db.get_month(self.grp, month)
        g = self.group.name
        if row is None or row["state"] not in ("picked", "done") or not row["winner_tmdb_id"]:
            return f"❌ {g} has no movie for {month_name(month)} yet."
        if start_local <= now:
            return "❌ That time is in the past."
        event = await self._event(row["event_id"])
        end = start_local + timedelta(minutes=self.group.discussion_minutes)
        if event is None:
            movie = await self.bot.db.get_movie(row["winner_tmdb_id"])
            winner = {"tmdb_id": movie["tmdb_id"], "title": display(movie),
                      "thread_id": movie["thread_id"]}
            event = await self.create_event(month, winner, now, start=start_local)
            if event is None:
                return f"❌ Couldn't create the event (is {g}'s voice channel set up?)."
            await self.bot.db.update_month(self.grp, month, event_id=event.id)
            return f"✅ Created {g}'s discussion event for **{when(start_local, self.tz)}**: {event.url}"
        await event.edit(start_time=start_local, end_time=end, reason="Rescheduled by an admin")
        return (f"✅ Moved {g}'s {month_name(month)} discussion to "
                f"**{when(start_local, self.tz)}**: {event.url}")

    # /schedule ------------------------------------------------------------------------
    async def schedule_lines(self, user_id: int, now: datetime) -> list[str]:
        db = self.bot.db
        out = [f"**🌙 {self.group.name} monthly movie**"]
        local = now.astimezone(self.tz).date().replace(day=1)
        for m, label in ((local, "This month"), (add_months(local, 1), "Next month")):
            key = month_key(m)
            row = await db.get_month(self.grp, key)
            head = f"{label} ({m:%B}): "
            if row is None:
                first = self.group.first_month
                out.append(head + (f"no movie (it starts {month_name(first)})."
                                   if first and m < first else "no movie."))
                continue
            st = row["state"]
            if st in ("picked", "done"):
                mv = await db.get_movie(row["winner_tmdb_id"])
                name = f"**{self.link(mv)}**" if mv else "the picked film"
                event = await self._event(row["event_id"])
                start = event.start_time if event else self.sched(key).discussion
                out.append(head + f"{name} ({how_picked(row)}). Discussion "
                           f"{when(start, self.tz)} {self.where()}.")
            elif st == "skipped":
                out.append(head + f"no movie ({row['skip_reason']}).")
            else:
                on = {e["tmdb_id"] for e in await self.ballot_films()}
                ranked = [t for t in await db.track_ranking(self.track, user_id) if t in on]
                url = await self.board_url()
                you = (f" You've ranked {len(ranked)} film{'s' * (len(ranked) != 1)}."
                       if ranked else " You haven't ranked anything yet.")
                out.append(head + f"voting open until {when(self.sched(key).vote_close, self.tz)}"
                           + (f" → {url}" if url else "") + f".{you}")
        return out
