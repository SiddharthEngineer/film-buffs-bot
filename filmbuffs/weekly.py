"""Weekly pick (Phase 10): rolling nominations → one always-open ranked vote → watch week.

Members have one nomination each in the main pool (see pool.py; private
groups have pools of their own). The ballot is every eligible nominated film,
and it updates as nominations change. #watch-planning holds one message, the
board (pool.py RankedTrack), edited in place (Phase 19).

For a watch week starting Monday T (America/Chicago):
  vote   T-7 Mon 12:00 AM → T-1 Sun 11:59 PM   ranked choice (instant runoff) on
         whatever is on the ballot at the close. Rankings carry over from the
         week before; a film that joins late is just unranked.
  watch  T Mon 12:00 AM → T+6 Sun 11:59 PM     winner tagged Up Next, server event,
         announcement in #watch-log pinging @Weekly Pings (that line is the log
         entry); at the end it's tagged Watched.
So every week runs this week's watch and next week's vote. Sunday night ends
this week's watch, then closes the vote for next week.

The weekly vote is independent of the private groups' monthly votes
(monthly.py): a film can be on both, and picks don't affect each other.

Each week is a row in `weeks` (voting → picked → done, or skipped); a 30 s tick
moves rows along whenever a deadline has passed, so a restart (or downtime)
just resumes where it left off.
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
from .pool import (ELIGIBLE, LABEL_LIMIT, MAX_BALLOT, RankedTrack, display, option_label,  # noqa: F401
                   ordinal, shared_lock)

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)
esc = discord.utils.escape_markdown


# ------------------------------------------------------------------ calendar

def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def local_midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0), tzinfo=tz)


@dataclass(frozen=True)
class Schedule:
    """Deadlines (aware datetimes) for the watch week starting `week`."""
    week: date
    tz: ZoneInfo

    @property
    def vote_open(self) -> datetime:            # Mon 12:00 AM the week before
        return local_midnight(self.week - timedelta(days=7), self.tz)

    ballot_open = vote_open

    @property
    def watch_start(self) -> datetime:
        return local_midnight(self.week, self.tz)

    @property
    def vote_close(self) -> datetime:           # Sun 11:59 PM before the watch week
        return self.watch_start - timedelta(minutes=1)

    @property
    def watch_end(self) -> datetime:            # Sun 11:59 PM of the watch week
        return local_midnight(self.week + timedelta(days=7), self.tz) - timedelta(minutes=1)


def weeks_to_create(now: datetime, tz: ZoneInfo, first: date | None,
                    existing: set[date]) -> list[date]:
    """Watch weeks whose vote should be open now but have no row yet.

    A week is only started before its vote closes, so a long outage never
    opens a vote that ends straight away.
    """
    if first is None:
        return []
    cur = monday_of(now.astimezone(tz).date())
    out = []
    for k in (0, 1):
        week = cur + timedelta(days=7 * k)
        s = Schedule(week, tz)
        if week >= first and week not in existing and s.vote_open <= now < s.vote_close:
            out.append(week)
    return out


def next_action(state: str, s: Schedule, now: datetime, pinned: bool = False) -> str | None:
    """What a week in `state` should do next at `now` (None = nothing yet)."""
    if state == "voting" and now >= s.vote_close:
        return "close_vote"
    if state == "picked" and now >= s.watch_end:
        return "end_watch"
    if state == "picked" and now >= s.watch_start and not pinned:
        return "start_watch"
    return None


def date_range(week: date) -> str:
    end = week + timedelta(days=6)
    if end.month == week.month:
        return f"{week:%b} {week.day}–{end.day}"
    return f"{week:%b} {week.day}–{end:%b} {end.day}"


def when(dt: datetime, tz: ZoneInfo) -> str:
    d = dt.astimezone(tz)
    return f"{d:%a %b} {d.day}, {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'} CT"


def role_ping(bot, key: str) -> tuple[str, discord.AllowedMentions]:
    """('<@&id> ', allowed mentions) for an opt-in ping role, or ('', none)."""
    rid = (getattr(bot.config, "roles", None) or {}).get(key)
    if not rid:
        return "", discord.AllowedMentions.none()
    return f"<@&{rid}> ", discord.AllowedMentions(everyone=False, users=False,
                                                   roles=[discord.Object(rid)])


# ------------------------------------------------------------------ winner

def instant_runoff(ballots: list[list[int]], candidates: list[dict],
                   tie_break: list[str]) -> dict | None:
    """Count ranked ballots by instant runoff.

    ballots: each member's ranking (tmdb ids, best first; may be partial).
    candidates: dicts with tmdb_id, interest, added_at.
    Each round every ballot counts for its highest-ranked film still in the
    race. More than half of the ballots still counting wins. Otherwise the
    film(s) with no votes go out together, or else the one with the fewest;
    ties for last go out by fewest 1st choices, then `tie_break` (least
    interest, i.e. on fewest uploaded watchlists, then added last). Returns None if nobody voted, else
    {winner, rounds: [{counts, out, exhausted}], first_choices, voters,
     winner_votes, runner_up_votes}.
    """
    info = {c["tmdb_id"]: c for c in candidates}
    clean = []
    for b in ballots:
        seen, keep = set(), []
        for t in b:
            if t in info and t not in seen:
                seen.add(t)
                keep.append(t)
        if keep:
            clean.append(keep)
    if not clean:
        return None
    first = {t: 0 for t in info}
    for b in clean:
        first[b[0]] += 1

    def out_key(t):   # sorts the film to drop first
        c = info[t]
        k = [first[t]]
        for name in tie_break:
            k.append(_neg_added(c["added_at"]) if name == "added" else c[name])
        k.append(-t)
        return tuple(k)

    remaining = set(info)
    rounds = []
    while True:
        counts = {t: 0 for t in remaining}
        exhausted = 0
        for b in clean:
            top = next((t for t in b if t in remaining), None)
            if top is None:
                exhausted += 1
            else:
                counts[top] += 1
        active = len(clean) - exhausted
        leader = max(counts.values())
        if len(remaining) == 1 or leader * 2 > active:
            winner = max(remaining, key=lambda t: counts[t])   # unique: a majority, or the last one
            rounds.append({"counts": counts, "out": [], "exhausted": exhausted})
            others = sorted((n for t, n in counts.items() if t != winner), reverse=True)
            return {"winner": winner, "rounds": rounds, "first_choices": first,
                    "voters": len(clean), "winner_votes": counts[winner],
                    "runner_up_votes": others[0] if others else 0}
        zero = [t for t in remaining if counts[t] == 0]
        if zero:
            out = sorted(zero, key=out_key)
        else:
            low = min(counts.values())
            out = [min((t for t in remaining if counts[t] == low), key=out_key)]
        rounds.append({"counts": counts, "out": out, "exhausted": exhausted})
        remaining -= set(out)
        if len(remaining) == 1 and not zero:
            # The last two were tied: the tie-break above decided it.
            winner = next(iter(remaining))
            return {"winner": winner, "rounds": rounds, "first_choices": first,
                    "voters": len(clean), "winner_votes": counts[winner],
                    "runner_up_votes": counts[out[0]]}


def _neg_added(added_at: str | None) -> float:
    """Sort key making the most recently added film come first (it goes out first)."""
    try:
        return -datetime.fromisoformat(added_at).timestamp()
    except (TypeError, ValueError):
        return 0.0


def runoff_text(result: dict, names: dict[int, str]) -> str:
    """Round-by-round summary for the pick announcement."""
    lines = []
    for i, r in enumerate(result["rounds"], 1):
        counts = sorted(r["counts"].items(), key=lambda kv: (-kv[1], names.get(kv[0], "")))
        shown = " · ".join(f"{names.get(t, t)} **{n}**" for t, n in counts if n)
        line = f"**Round {i}:** {shown}"
        if r["exhausted"]:
            line += f" ({r['exhausted']} ballot{'s' * (r['exhausted'] != 1)} used up)"
        if r["out"]:
            line += "\n-# Out: " + ", ".join(names.get(t, str(t)) for t in r["out"])
        lines.append(line)
    return "\n".join(lines)


def won_text(won: int, runner_up: int | None, rounds: int | None = None) -> str:
    text = f"won {won}–{runner_up or 0}"
    return text + (f" after {rounds} rounds" if rounds and rounds > 1 else "")


SOLE_NOMINATION = "the only nomination, so no vote"


def how_picked(row) -> str:
    """How a picked week's movie was chosen, from its `weeks` row."""
    if row["override_by"]:
        return "picked by an admin"
    if row["winner_votes"] is None:
        return SOLE_NOMINATION
    rounds = len(json.loads(row["vote_rounds"])) if row["vote_rounds"] else None
    return won_text(row["winner_votes"], row["runner_up_votes"], rounds)


def log_line(week: date, title: str | None, how: str | None = None,
             skipped: str | None = None) -> str:
    iso = week.isocalendar()
    head = f"Wk {iso.week} · {date_range(week)} {week.year}"
    if skipped:
        return f"{head} · no pick ({skipped})"
    return f"{head} · {title} · {how or 'picked by an admin'}"


def pick_text(week: date, link: str, how: str, event_url: str | None, start: str, end: str,
              detail: str = "") -> str:
    """The #watch-log announcement of a weekly pick (Phase 19)."""
    iso = week.isocalendar()
    text = (f"🎬 **Wk {iso.week} · {date_range(week)} {week.year}: {link}** · {how}\n"
            f"Watch it any time from **{start}** to **{end}**, then talk about it in "
            f"its #post-watch post.")
    if event_url:
        text += f"\n📅 [Event]({event_url})"
    lines = [ln.removeprefix("-# ").strip() for ln in detail.splitlines()]
    text += "".join(f"\n-# {ln}" for ln in lines if ln)
    return text


# ------------------------------------------------------------------ service

class WeeklyService(RankedTrack):
    track = "weekly"
    emoji = "🗳️"
    label = "weekly"
    unit = "week"

    @property
    def pool(self):
        return self.bot.pool

    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self.lock = shared_lock(bot)
        self._rendered: dict[int, str] = {}     # message id -> last rendered ballot
        self._tasks: set[asyncio.Task] = set()

    @property
    def tz(self) -> ZoneInfo:
        return self.bot.config.tz

    def channel(self, key: str) -> discord.TextChannel | None:
        cid = self.bot.config.channels.get(key)
        ch = self.bot.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    def sched(self, week_start: str | date) -> Schedule:
        d = week_start if isinstance(week_start, date) else date.fromisoformat(week_start)
        return Schedule(d, self.tz)

    # RankedTrack ---------------------------------------------------------------
    def close_time(self, period: str) -> datetime:
        return self.sched(period).vote_close

    def title(self, period: str) -> str:
        return f"watch week {date_range(date.fromisoformat(period))}"

    async def open_row(self, period: str, now: datetime | None = None):
        return await self.voting_week(period, now)

    async def voting_week(self, week_start: str, now: datetime | None = None):
        """The week row if its vote is open right now, else None."""
        now = now or datetime.now(timezone.utc)
        row = await self.bot.db.get_week(week_start)
        if row is None or row["state"] != "voting" or now >= self.sched(week_start).vote_close:
            return None
        return row

    async def refresh_open(self) -> None:
        await self.refresh_board()

    def ref(self, key: str) -> str:
        """A channel mention (normal channels render fine on phones), or #name."""
        cid = self.bot.config.channels.get(key)
        return f"<#{cid}>" if cid else "#" + key.replace("_", "-")

    # tick ------------------------------------------------------------------
    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        async with self.lock:
            db = self.bot.db
            await self.pool.sync()         # statuses can change elsewhere (admin, Letterboxd)
            groups = list(getattr(self.bot, "group_votes", {}).values())
            for g in groups:               # private groups' monthly votes
                try:
                    await g.tick_locked(now)
                except Exception:
                    log.exception("Group %s tick failed", g.grp)
            # Oldest first, so Sunday night ends this week's watch, then closes
            # next week's vote (winner → Up Next).
            for row in await db.weeks_in_state("voting", "picked"):
                await self._advance(row["week_start"], now)
            existing = {date.fromisoformat(r["week_start"]) for r in await self._all_weeks()}
            for week in weeks_to_create(now, self.tz, self.bot.config.first_watch_week, existing):
                await db.create_week(week.isoformat())
                log.info("Opened the vote for watch week %s", week)
                await self.opened(week.isoformat())
                await self._advance(week.isoformat(), now)
        await self.refresh_open()
        for g in groups:
            await g.refresh_open()

    async def _all_weeks(self):
        cur = await self.bot.db.conn.execute("SELECT week_start FROM weeks")
        return await cur.fetchall()

    async def _advance(self, week_start: str, now: datetime) -> None:
        for _ in range(5):
            row = await self.bot.db.get_week(week_start)
            action = next_action(row["state"], self.sched(week_start), now,
                                 pinned=bool(row["pinned_at"]))
            if action is None:
                return
            try:
                await getattr(self, action)(row, now)
            except Exception:
                log.exception("Weekly pick: %s failed for watch week %s", action, week_start)
                return

    async def opened(self, week_start: str) -> None:
        """A new weekly vote just opened (rankings carry over). The board switches
        to it; no separate notice (Phase 19: #watch-planning is one message)."""
        await self.refresh_board()

    # board -----------------------------------------------------------------
    def board_channel(self):
        return self.channel("watch_planning")

    async def voting_period(self) -> str | None:
        rows = await self.bot.db.weeks_in_state("voting")
        now = datetime.now(timezone.utc)
        open_ = [r["week_start"] for r in rows if now < self.sched(r["week_start"]).vote_close]
        return min(open_) if open_ else None

    async def legacy_ballot_id(self) -> int | None:
        period = await self.voting_period()
        row = await self.bot.db.get_week(period) if period else None
        return row["ballot_message_id"] if row else None

    def board_header(self, period: str | None, voters: int) -> tuple[str, str, str]:
        footer = ("-# **My nomination** to nominate a film, then **Vote** to rank. "
                  "**/help** explains how it works.")
        if period is None:
            return ("## 🎬 Weekly vote", "No vote is open right now.", footer)
        s = self.sched(period)
        who = f"**{voters}** member{' has' if voters == 1 else 's have'} voted"
        return (f"## 🎬 Weekly vote · watch week {date_range(s.week)}",
                f"🗳️ Voting closes **{when(s.vote_close, self.tz)}** · {who}. "
                f"Results go to {self.ref('watch_log')}.", footer)

    # vote ------------------------------------------------------------------
    async def close_vote(self, row, now: datetime) -> None:
        week = row["week_start"]
        db = self.bot.db
        films = await self.ballot_films()
        candidates = []
        for f in films:
            candidates.append({"tmdb_id": f["tmdb_id"],
                               "interest": await db.movie_interest(f["tmdb_id"]),
                               "added_at": f["added_at"], "source": "nomination",
                               "nominated_by": f["user_id"]})
        await db.set_options(week, candidates)
        ids = {c["tmdb_id"] for c in candidates}
        everyone = await db.all_track_rankings(self.track)
        ballots = {u: [t for t in ts if t in ids] for u, ts in everyone.items()}
        ballots = {u: ts for u, ts in ballots.items() if ts}
        await db.snapshot_week_rankings(week, ballots)
        await db.update_week(week, ballot_closed_at=utcnow())
        if not candidates:
            await db.update_week(week, poll_closed_at=utcnow(), total_votes=0)
            await self.skip(row, "nobody nominated a film")
            return
        by_id = {o["tmdb_id"]: o for o in await db.options(week)}
        if len(candidates) == 1:
            w = by_id[candidates[0]["tmdb_id"]]
            await self._crown(
                row, w, now, how=SOLE_NOMINATION,
                line="",
                poll_closed_at=utcnow(), total_votes=len(ballots))
            return
        result = instant_runoff(list(ballots.values()), candidates, self.bot.config.tie_break)
        if result is None:
            await db.update_week(week, poll_closed_at=utcnow(), total_votes=0)
            await self.skip(row, "nobody voted")
            return
        await db.record_first_choices(week, result["first_choices"])
        await db.update_week(week, poll_closed_at=utcnow(), total_votes=result["voters"],
                             vote_rounds=json.dumps(result["rounds"]))
        w = by_id[result["winner"]]
        won, ru, n_rounds = result["winner_votes"], result["runner_up_votes"], len(result["rounds"])
        voters = result["voters"]
        names = {tid: display(o) for tid, o in by_id.items()}
        how = won_text(won, ru, n_rounds)
        await self._crown(
            row, w, now, how=how,
            line=(f"{voters} voter{'s' * (voters != 1)}. Instant runoff: each round, every "
                  f"ballot counts for its highest-ranked film still in the race.\n"
                  + runoff_text(result, names)),
            winner_votes=won, runner_up_votes=ru)

    async def _crown(self, row, w, now: datetime, *, how: str, line: str, **fields) -> None:
        """Make option row `w` the week's movie: Up Next, event, an announcement in
        #watch-log pinging @Weekly Pings (Phase 19; was #watch-planning),
        state picked. It leaves the pool."""
        week = row["week_start"]
        db = self.bot.db
        winner = {"tmdb_id": w["tmdb_id"], "title": display(w), "thread_id": w["thread_id"]}
        prev = await db.get_movie(winner["tmdb_id"])
        await db.update_week(week, winner_tmdb_id=winner["tmdb_id"],
                             winner_prev_status=prev["status"] if prev else None, **fields)
        await db.set_status(winner["tmdb_id"], "up_next")
        await self.pool.picked(winner["tmdb_id"])
        await self._safe(self.bot.movies.retag(winner["tmdb_id"]), "retag winner")
        event = await self._safe(self.create_event(week, winner, now), "create event")
        ann_msg = await self.announce(week, winner, how, event, line)
        await db.update_week(week, state="picked", event_id=event.id if event else None,
                             announcement_id=ann_msg.id if ann_msg else None,
                             log_message_id=ann_msg.id if ann_msg else None)
        log.info("Watch week %s: %s, %s", week, winner["title"], how)
        await self._safe(self.refresh_board(), "board refresh")

    async def announce(self, week: str, winner: dict, how: str, event, detail: str = ""):
        """The pick, in #watch-log, pinging @Weekly Pings (Phase 19: #watch-planning
        is just the board). This line is also the week's log entry."""
        s = self.sched(week)
        ping, mentions = role_ping(self.bot, "weekly_ping")
        text = pick_text(date.fromisoformat(week), self.link(winner, esc(winner["title"])), how,
                         event.url if event else None, when(s.watch_start, self.tz),
                         when(s.watch_end, self.tz), detail)
        ch = self.channel("watch_log")
        if ch is None:
            return None
        return await self._safe(ch.send(truncate(ping + text, 2000), allowed_mentions=mentions),
                                "announce")

    async def backfill_log(self) -> int:
        """Weeks picked before Phase 19 were announced in #watch-planning (since
        cleaned up), so #watch-log has no line for them until the week ends. Post the
        pick line there now, without a ping, and make it the week's log entry."""
        n = 0
        for row in await self.bot.db.weeks_in_state("picked"):
            if row["log_message_id"] or not row["winner_tmdb_id"]:
                continue
            movie = await self.bot.db.get_movie(row["winner_tmdb_id"])
            if movie is None:
                continue
            week = row["week_start"]
            s = self.sched(week)
            event_url = (f"https://discord.com/events/{self.bot.config.guild_id}/{row['event_id']}"
                         if row["event_id"] else None)
            text = pick_text(date.fromisoformat(week), self.link(movie, esc(display(movie))),
                             how_picked(row), event_url, when(s.watch_start, self.tz),
                             when(s.watch_end, self.tz))
            msg = await self._log(truncate(text, 2000))
            if msg is None:
                continue
            await self.bot.db.update_week(week, log_message_id=msg.id)
            log.info("Posted the missing #watch-log line for week %s (%s)", week, display(movie))
            n += 1
        return n

    async def strike_log(self, row, why: str) -> None:
        """A pick was replaced or cleared: cross out its #watch-log line."""
        mid = row["log_message_id"]
        ch = self.channel("watch_log")
        if not mid or ch is None:
            return
        rng = date_range(date.fromisoformat(row["week_start"]))
        await self._safe(ch.get_partial_message(mid).edit(
            content=f"~~Weekly movie · {rng}~~ ({why})",
            allowed_mentions=discord.AllowedMentions.none()), "strike log line")

    async def create_event(self, week: str, winner: dict, now: datetime):
        s = self.sched(week)
        guild = self.bot.get_guild(self.bot.config.guild_id)
        if guild is None or now >= s.watch_end:
            return None
        start = max(s.watch_start, now + timedelta(minutes=2))
        url = f"https://discord.com/channels/{guild.id}/{winner['thread_id']}"
        return await guild.create_scheduled_event(
            name=truncate(f"🎬 Weekly movie: {winner['title']}", 100),
            start_time=start, end_time=s.watch_end,
            entity_type=discord.EntityType.external, location=url,
            privacy_level=discord.PrivacyLevel.guild_only,
            description=truncate(f"This week's movie: {winner['title']}. Watch it any time this "
                                 f"week, then join the post-watch discussion. Post: {url}", 1000),
            reason="Weekly pick")

    # watch week ------------------------------------------------------------
    async def start_watch(self, row, now: datetime) -> None:
        """Pin the week's movie in #pre-watch and its post-watch discussion in
        #post-watch. A forum has one pinned post, so this replaces
        last week's pins."""
        tmdb_id = row["winner_tmdb_id"]
        post = await self.bot.movies.find_post(tmdb_id) if tmdb_id else None
        if post is not None:
            await self._safe(post.edit(archived=False, pinned=True, reason="This week's movie"),
                             "pin #pre-watch post")
            try:
                spoiler, _ = await self.bot.spoilers.open(tmdb_id)
                await self._safe(spoiler.edit(archived=False, pinned=True,
                                              reason="This week's movie"), "pin post-watch post")
            except LookupError:
                pass
        await self.bot.db.update_week(row["week_start"], pinned_at=utcnow())
        log.info("Watch week %s started; pinned TMDB %s", row["week_start"], tmdb_id)

    async def end_watch(self, row, now: datetime) -> None:
        week = row["week_start"]
        db = self.bot.db
        tmdb_id = row["winner_tmdb_id"]
        movie = await db.get_movie(tmdb_id) if tmdb_id else None
        if movie is not None:
            await db.set_status(tmdb_id, "watched")
            await self._safe(self.bot.movies.retag(tmdb_id), "retag watched")
        title = display(movie) if movie else f"TMDB {tmdb_id}"
        log_id = row["log_message_id"]
        if not log_id:      # picked before Phase 19: announced in #watch-planning
            log_msg = await self._log(log_line(date.fromisoformat(week), title, how_picked(row)))
            log_id = log_msg.id if log_msg else None
        await db.update_week(week, state="done", ended_at=utcnow(), log_message_id=log_id)
        log.info("Watch week %s ended: %s", week, title)

    async def _log(self, line: str):
        ch = self.channel("watch_log")
        if ch is None:
            return None
        return await self._safe(ch.send(line, allowed_mentions=discord.AllowedMentions.none()),
                                "watch-log")

    async def skip(self, row, reason: str) -> None:
        week = row["week_start"]
        await self.bot.db.update_week(week, state="skipped", skip_reason=reason)
        log.info("Watch week %s skipped: %s", week, reason)
        log_msg = await self._log(log_line(date.fromisoformat(week), None, skipped=reason))
        if log_msg:
            await self.bot.db.update_week(week, log_message_id=log_msg.id)
        await self._safe(self.refresh_board(), "board refresh")

    async def _safe(self, coro, what: str):
        try:
            return await coro
        except discord.HTTPException:
            log.exception("Weekly pick: %s failed", what)
            return None

    # admin override ----------------------------------------------------------
    async def admin_pick(self, admin_id: int, which: str, tmdb_id: int,
                         now: datetime | None = None) -> str:
        """Set this or next week's movie, closing its vote early if it's open."""
        now = now or datetime.now(timezone.utc)
        db = self.bot.db
        cur = monday_of(now.astimezone(self.tz).date())
        week = cur if which == "this" else cur + timedelta(days=7)
        ws, s = week.isoformat(), self.sched(week)
        async with self.lock:
            movie = await db.get_movie(tmdb_id)
            if movie is None:
                return "❌ That movie has no post."
            row = await db.get_week(ws)
            if row and row["winner_tmdb_id"] == tmdb_id and row["state"] == "picked":
                return f"**{display(movie)}** is already the movie for {date_range(week)}."
            if movie["status"] == "up_next":
                return f"❌ **{display(movie)}** is already Up Next (this or next week)."
            if row is None:
                await db.create_week(ws, state="picked")
                row = await db.get_week(ws)
            if row["state"] == "done":
                return "❌ That week is already over."
            notes = []
            if row["state"] == "voting":
                rankings = await db.all_track_rankings(self.track)
                on = {e["tmdb_id"] for e in await self.ballot_films()}
                kept = {u: [t for t in ts if t in on] for u, ts in rankings.items()}
                kept = {u: ts for u, ts in kept.items() if ts}
                await db.snapshot_week_rankings(ws, kept)
                n = len(kept)
                notes.append(f"closed voting early ({n} ranking{'s' * (n != 1)} kept for stats; "
                             f"they carry over to next week)")
                await db.update_week(ws, poll_closed_at=utcnow(), total_votes=n)
            if row["state"] == "picked" and row["winner_tmdb_id"]:
                old = await db.get_movie(row["winner_tmdb_id"])
                if old is not None:
                    await db.set_status(old["tmdb_id"], row["winner_prev_status"] or "backlog")
                    await self._safe(self.bot.movies.retag(old["tmdb_id"]), "retag old winner")
                    notes.append(f"{display(old)} went back to "
                                 f"{(row['winner_prev_status'] or 'backlog').replace('_', ' ')}")
            await db.update_week(ws, state="picked", winner_tmdb_id=tmdb_id, winner_votes=None,
                                 runner_up_votes=None, override_by=admin_id,
                                 winner_prev_status=movie["status"], skip_reason=None,
                                 pinned_at=None)
            await db.set_status(tmdb_id, "up_next")
            await self.pool.picked(tmdb_id)
            await self._safe(self.bot.movies.retag(tmdb_id), "retag pick")
            winner = {"tmdb_id": tmdb_id, "title": display(movie), "thread_id": movie["thread_id"]}
            event = None
            if row["event_id"]:
                event = await self._safe(self.update_event(row["event_id"], ws, winner), "edit event")
            if event is None:
                event = await self._safe(self.create_event(ws, winner, now), "create event")
            await self.strike_log(row, "replaced by an admin pick")
            ann = await self.announce(ws, winner, "picked by an admin", event)
            await db.update_week(ws, event_id=event.id if event else None,
                                 announcement_id=ann.id if ann else None,
                                 log_message_id=ann.id if ann else None)
            row = await db.get_week(ws)
            if now >= s.watch_start:
                await self.start_watch(row, now)
                notes.append("pinned it in both forums")
            log.info("%s picked TMDB %s for %s", admin_id, tmdb_id, ws)
        await self._safe(self.refresh_board(), "board refresh")
        self.pool.refresh_soon()
        extra = f" ({'; '.join(notes)})" if notes else ""
        return f"✅ **{display(movie)}** is the movie for {date_range(week)}{extra}."

    async def admin_clear(self, admin_id: int, which: str, now: datetime | None = None) -> str:
        """Undo this or next week's pick: the movie goes back to its old status,
        its pins and the bot's event go, its #watch-log line is crossed out, and
        the week becomes skipped. /admin pick can still set a movie for it afterwards."""
        now = now or datetime.now(timezone.utc)
        db = self.bot.db
        cur = monday_of(now.astimezone(self.tz).date())
        week = cur if which == "this" else cur + timedelta(days=7)
        ws = week.isoformat()
        async with self.lock:
            row = await db.get_week(ws)
            if row is None or row["state"] != "picked" or not row["winner_tmdb_id"]:
                return f"❌ No movie is set for {date_range(week)}."
            tmdb_id = row["winner_tmdb_id"]
            movie = await db.get_movie(tmdb_id)
            notes = []
            if movie is not None:
                prev = row["winner_prev_status"] or "backlog"
                await db.set_status(tmdb_id, prev)
                await self._safe(self.bot.movies.retag(tmdb_id), "retag cleared pick")
                notes.append(f"back to {prev.replace('_', ' ')}")
                if row["pinned_at"]:
                    post = await self.bot.movies.find_post(tmdb_id)
                    if post is not None and post.flags.pinned:
                        await self._safe(post.edit(pinned=False, reason="Pick cleared"),
                                         "unpin #pre-watch post")
                    spoiler = await self.bot.spoilers.find_spoiler_post(movie)
                    if spoiler is not None and spoiler.flags.pinned:
                        await self._safe(spoiler.edit(pinned=False, reason="Pick cleared"),
                                         "unpin post-watch post")
                    notes.append("unpinned")
            if row["event_id"]:
                guild = self.bot.get_guild(self.bot.config.guild_id)
                try:
                    event = await guild.fetch_scheduled_event(row["event_id"])
                    await event.delete(reason="Weekly pick cleared by an admin")
                    notes.append("event deleted")
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.exception("Weekly pick: delete event failed")
            await db.update_week(ws, state="skipped", skip_reason="cleared by an admin",
                                 winner_tmdb_id=None, winner_prev_status=None, event_id=None,
                                 pinned_at=None, override_by=admin_id)
            title = display(movie) if movie else f"TMDB {tmdb_id}"
            await self.strike_log(row, f"{esc(title)} was cleared by an admin")
            log.info("%s cleared the pick (TMDB %s) for %s", admin_id, tmdb_id, ws)
        return f"✅ Cleared **{title}** for {date_range(week)} ({'; '.join(notes)})."

    async def update_event(self, event_id: int, week: str, winner: dict):
        guild = self.bot.get_guild(self.bot.config.guild_id)
        try:
            event = await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            return None
        if event.status not in (discord.EventStatus.scheduled, discord.EventStatus.active):
            return None
        url = f"https://discord.com/channels/{guild.id}/{winner['thread_id']}"
        return await event.edit(
            name=truncate(f"🎬 Watch week: {winner['title']}", 100), location=url,
            end_time=self.sched(week).watch_end,
            description=truncate(f"This week's movie: {winner['title']}. Watch it any time this "
                                 f"week, then join the post-watch discussion. Post: {url}", 1000),
            reason="Weekly pick changed by an admin")

    # members ---------------------------------------------------------------
    async def schedule_text(self, user_id: int, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        db, cfg = self.bot.db, self.bot.config
        cur = monday_of(now.astimezone(self.tz).date())
        out = ["**🎬 Weekly movie**"]
        for k, label in ((0, "This week"), (1, "Next week")):
            week = cur + timedelta(days=7 * k)
            row = await db.get_week(week.isoformat())
            head = f"{label} ({date_range(week)}): "
            s = self.sched(week)
            if row is None:
                if cfg.first_watch_week and week < cfg.first_watch_week:
                    out.append(head + "no pick (the weekly pick starts "
                               f"{date_range(cfg.first_watch_week)}).")
                elif now < s.vote_open:
                    out.append(head + f"voting opens {when(s.vote_open, self.tz)}.")
                else:
                    out.append(head + "no pick.")
                continue
            st = row["state"]
            if st in ("picked", "done"):
                m = await db.get_movie(row["winner_tmdb_id"])
                name = f"**{self.link(m)}**" if m else "the picked film"
                out.append(head + f"{name} ({how_picked(row)}).")
            elif st == "skipped":
                out.append(head + f"no pick ({row['skip_reason']}).")
            elif st == "voting":
                on = {e["tmdb_id"] for e in await self.ballot_films()}
                ranked = [t for t in await db.track_ranking(self.track, user_id) if t in on]
                you = (f" You've ranked {len(ranked)} film{'s' * (len(ranked) != 1)}."
                       if ranked else " You haven't ranked anything yet: click **Vote** on the "
                       "ballot.")
                url = await self.board_url()
                link = f" → {url}" if url else ""
                out.append(head + f"voting open until {when(s.vote_close, self.tz)}{link}.{you}")
        out.append("")
        out.append(await self.pool._summary(user_id))
        return "\n".join(out)
