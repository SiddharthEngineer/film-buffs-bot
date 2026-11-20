"""Phase 25: nominate and vote reminders (owner, 2026-11-20).

Two reminders per open vote, at 9:00 AM America/Chicago:
  - 📋 **nominate**: three days before the vote's last day (weekly: Thursday;
    a group's monthly vote: the 27th/28th of 30/31-day months, the 25th in a
    28-day February), to members who have no eligible nomination in that pool;
  - 🗳️ **vote**: on the vote's last day (weekly: Sunday; monthly: the last day
    of the month), to members whose saved ranking doesn't cover their top
    three films on today's ballot (or every film, if there are fewer than three).

Only members with the vote's ping role get them (@Weekly Pings, or the group's
Pings role; group reminders also need the group role). A role ping would reach
everyone in the role, so each reminder is one message that @mentions just the
people who still need to act: the weekly one in #general, a group's in its
#chat (owner's choice over DMs, which Discord says bots shouldn't send to a
whole server). Nominations and rankings carry over from earlier votes and count.

Each reminder is sent once (bot_meta `reminder:<grp>:<period>:<kind>`). If the
bot was down at 9:00, it catches up within CATCH_UP, and never after the vote
closes.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

from .groups import MAIN

if TYPE_CHECKING:
    from .pool import RankedTrack

log = logging.getLogger(__name__)

AT = time(9, 0)                     # local (config.tz) time of both reminders
NOMINATE_DAYS_BEFORE = 3            # the nominate reminder: last day minus 3 days
CATCH_UP = timedelta(hours=12)      # send late after downtime, but not a day late
TOP = 3                             # "voted" = ranked your top 3 (or every film if fewer)
PER_MESSAGE = 50                    # mentions per message (Discord allows 100 user IDs)
META = "reminder:{grp}:{period}:{kind}"
KINDS = ("nominate", "vote")


def reminder_times(close: datetime, tz: ZoneInfo) -> dict[str, datetime]:
    """When each reminder is due for a vote closing at `close` (its last day's 11:59 PM)."""
    last = close.astimezone(tz).date()
    return {
        "nominate": datetime.combine(last - timedelta(days=NOMINATE_DAYS_BEFORE), AT, tzinfo=tz),
        "vote": datetime.combine(last, AT, tzinfo=tz),
    }


def due(at: datetime, close: datetime, now: datetime) -> bool:
    return at <= now < min(close, at + CATCH_UP)


def has_voted(ranking: list[int], ballot: list[int]) -> bool:
    """Ranked their top `TOP` films on today's ballot, or every film if there are fewer.
    Films no longer on the ballot don't count. An empty ballot needs no vote."""
    on = set(ballot)
    ranked = sum(1 for t in ranking if t in on)
    return ranked >= min(TOP, len(on))


def chunks(ids: list[int], n: int = PER_MESSAGE) -> list[list[int]]:
    return [ids[i:i + n] for i in range(0, len(ids), n)]


def reminder_text(kind: str, svc: "RankedTrack", mentions: str, closes: str, board: str) -> str:
    what = "this week's vote" if svc.pool.grp == MAIN else f"{svc.pool.name}'s monthly vote"
    where = f"[the board]({board})" if board else "the board in #watch-planning"
    if kind == "nominate":
        return (f"📋 {mentions}\nYou haven't nominated a film for {what} yet. Voting closes "
                f"**{closes}**: click **My nomination** on {where} to add one.\n"
                f"-# You're getting this because you have 🔔 Pings on; turn them off on the board.")
    return (f"🗳️ {mentions}\nLast day to vote in {what}: it closes **{closes}** and you "
            f"haven't ranked your top {TOP} yet. Click **Vote** on {where}.\n"
            f"-# You're getting this because you have 🔔 Pings on; turn them off on the board.")


class Reminders:
    """Sends the reminders for one vote service (WeeklyService or a GroupVoteService)."""

    def __init__(self, svc: "RankedTrack"):
        self.svc = svc

    @property
    def bot(self):
        return self.svc.bot

    def _ping_role_id(self) -> int | None:
        if self.svc.pool.grp == MAIN:
            return (getattr(self.bot.config, "roles", None) or {}).get("weekly_ping")
        return self.svc.group.ping_role_id

    def channel(self):
        if self.svc.pool.grp == MAIN:
            return self.svc.channel("general")
        ch = self.svc.channel("chat")
        return ch if isinstance(ch, discord.TextChannel) else None

    def subscribers(self) -> list[int]:
        """Members with the ping role (and, for a group, the group role), no bots."""
        guild = self.bot.get_guild(self.bot.config.guild_id)
        rid = self._ping_role_id()
        role = guild.get_role(rid) if guild and rid else None
        if role is None:
            return []
        need = None if self.svc.pool.grp == MAIN else self.svc.group.role_id
        return sorted(m.id for m in role.members
                      if not m.bot and (need is None or any(r.id == need for r in m.roles)))

    async def missing(self, kind: str, users: list[int]) -> list[int]:
        """Of `users`, those who still need to nominate / vote."""
        db = self.bot.db
        if kind == "nominate":
            ok = await self.svc.pool._ok()
            have = {r["user_id"] for r in await db.pool(grp=self.svc.pool.grp) if ok(r)}
            return [u for u in users if u not in have]
        ballot = [e["tmdb_id"] for e in await self.svc.ballot_films()]
        if not ballot:
            return []
        ranks = await db.all_track_rankings(self.svc.track)
        return [u for u in users if not has_voted(ranks.get(u, []), ballot)]

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        period = await self.svc.voting_period()
        if period is None:
            return
        close = self.svc.close_time(period)
        for kind, at in reminder_times(close, self.svc.tz).items():
            key = META.format(grp=self.svc.pool.grp, period=period, kind=kind)
            if not due(at, close, now) or await self.bot.db.get_meta(key):
                continue
            await self.send(kind, period, close)
            await self.bot.db.set_meta(key, now.isoformat(timespec="seconds"))

    async def send(self, kind: str, period: str, close: datetime) -> int:
        """Post the reminder; returns how many members it mentioned."""
        from .weekly import when
        who = await self.missing(kind, self.subscribers())
        grp = self.svc.pool.grp
        if not who:
            log.info("%s %s reminder for %s: nobody to remind", grp, kind, period)
            return 0
        ch = self.channel()
        if ch is None:
            log.warning("%s %s reminder: no channel to post in", grp, kind)
            return 0
        board = await self.svc.board_url()
        for part in chunks(who):
            text = reminder_text(kind, self.svc, " ".join(f"<@{u}>" for u in part),
                                 when(close, self.svc.tz), board)
            try:
                await ch.send(text, allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(u) for u in part]))
            except discord.HTTPException:
                log.exception("%s %s reminder failed", grp, kind)
                return 0
        log.info("%s %s reminder for %s: mentioned %d member(s)", grp, kind, period, len(who))
        return len(who)
