"""Weekly pick: ballot → poll → watch week, one stage per week.

For a watch week starting Monday T (America/Chicago):
  ballot  T-14 Mon 12:00 AM → T-7 Sun 11:59 PM   /nominate (1 per member, first 10
          come first served) + the most-wanted backlog films fill the rest.
          A bot message in #watch-planning shows the 10 options live.
  poll    T-7 Mon 12:00 AM → T-1 Sun 11:59 PM   native Discord poll with the
          frozen ballot (polls can't be edited once posted, hence the freeze).
  watch   T Mon 12:00 AM → T+6 Sun 11:59 PM     winner tagged Up Next, server event,
          announcement; at the end it's tagged Watched and logged in #watch-log.
So every week three things run at once: this week's watch, next week's poll and
the ballot for the week after. Sunday night closes all three in that order.

Each week is a row in `weeks`; a 30 s tick moves rows along whenever a deadline
has passed, so a restart (or downtime) just resumes where it left off.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

from .db import utcnow
from .movies import truncate

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

# Statuses that can be nominated / put on a ballot. Watched (solo) is fine:
# one member having seen it doesn't rule it out for the group.
ELIGIBLE = ("backlog", "watched_solo")
ANSWER_LIMIT = 55          # Discord poll answer text
QUESTION_LIMIT = 300
MAX_POLL_HOURS = 32 * 24
FINALIZE_TRIES, FINALIZE_WAIT = 6, 10.0


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
    def ballot_open(self) -> datetime:
        return local_midnight(self.week - timedelta(days=14), self.tz)

    @property
    def poll_open(self) -> datetime:
        return local_midnight(self.week - timedelta(days=7), self.tz)

    @property
    def ballot_close(self) -> datetime:        # Sun 11:59 PM before the poll week
        return self.poll_open - timedelta(minutes=1)

    @property
    def watch_start(self) -> datetime:
        return local_midnight(self.week, self.tz)

    @property
    def poll_close(self) -> datetime:          # Sun 11:59 PM before the watch week
        return self.watch_start - timedelta(minutes=1)

    @property
    def watch_end(self) -> datetime:           # Sun 11:59 PM of the watch week
        return local_midnight(self.week + timedelta(days=7), self.tz) - timedelta(minutes=1)


def weeks_to_create(now: datetime, tz: ZoneInfo, first: date | None,
                    existing: set[date]) -> list[date]:
    """Watch weeks whose ballot should exist now but has no row yet.

    A week is only started while its poll can still run (before its poll
    closes), so a long outage never posts a poll that ends straight away.
    """
    if first is None:
        return []
    cur = monday_of(now.astimezone(tz).date())
    out = []
    for k in (0, 1, 2):
        week = cur + timedelta(days=7 * k)
        s = Schedule(week, tz)
        if week >= first and week not in existing and s.ballot_open <= now < s.poll_close:
            out.append(week)
    return out


def next_action(state: str, s: Schedule, now: datetime, pinned: bool = False) -> str | None:
    """What a week in `state` should do next at `now` (None = nothing yet)."""
    if state == "ballot" and now >= s.ballot_close:
        return "close_ballot"
    if state == "ballot_closed" and now >= s.poll_open:
        return "open_poll"
    if state == "poll" and now >= s.poll_close:
        return "close_poll"
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


def display(row) -> str:
    return f"{row['title']} ({row['year']})" if row["year"] else row["title"]


def answer_label(row) -> str:
    year = f" ({row['year']})" if row["year"] else ""
    return truncate(row["title"], ANSWER_LIMIT - len(year)) + year


# ------------------------------------------------------------------ ballot / winner

def compose_ballot(nominations: list, ranked: list, size: int) -> list[dict]:
    """Nominations first (in the order made, eligible ones only), then the
    most-wanted eligible films with at least one interested member."""
    options: list[dict] = []
    seen: set[int] = set()
    by_id = {r["tmdb_id"]: r for r in ranked}
    for n in nominations:
        if len(options) >= size:
            break
        if n["status"] not in ELIGIBLE or n["tmdb_id"] in seen:
            continue
        r = by_id.get(n["tmdb_id"])
        options.append({"tmdb_id": n["tmdb_id"], "source": "nomination",
                        "nominated_by": n["user_id"],
                        "interest": r["interest"] if r else 0,
                        "thumbs": r["thumbs"] if r else 0})
        seen.add(n["tmdb_id"])
    for r in ranked:
        if len(options) >= size:
            break
        if r["tmdb_id"] in seen or r["status"] not in ELIGIBLE or r["interest"] < 1:
            continue
        options.append({"tmdb_id": r["tmdb_id"], "source": "top", "nominated_by": None,
                        "interest": r["interest"], "thumbs": r["thumbs"]})
        seen.add(r["tmdb_id"])
    return options


def pick_winner(results: list[dict], tie_break: list[str]) -> tuple[dict, int] | None:
    """results: dicts with tmdb_id, votes, interest, thumbs, added_at.
    Returns (winner, runner-up votes), or None if nobody voted."""
    if not results or sum(r["votes"] for r in results) == 0:
        return None

    def key(r):
        k = [-r["votes"]]
        for t in tie_break:
            k.append(r["added_at"] if t == "added" else -r[t])
        k.append(r["tmdb_id"])
        return tuple(k)

    ordered = sorted(results, key=key)
    return ordered[0], (ordered[1]["votes"] if len(ordered) > 1 else 0)


def interest_text(interest: int, thumbs: int, listed: int) -> str:
    parts = [f"👍 {thumbs}"] + ([f"📋 {listed}"] if listed else [])
    return " · ".join(parts)


def score_text(r) -> str:
    """'👍 3' when only votes count; '**4** want it (👍 3 · 📋 2)' with watchlists
    (a member with both counts once, so the parts can add up to more)."""
    if not r["listed"]:
        return f"👍 {r['thumbs']}"
    return f"**{r['interest']}** want it ({interest_text(r['interest'], r['thumbs'], r['listed'])})"


def log_line(week: date, title: str | None, won: int | None = None, runner_up: int | None = None,
             skipped: str | None = None) -> str:
    iso = week.isocalendar()
    head = f"Wk {iso.week} · {date_range(week)} {week.year}"
    if skipped:
        return f"{head} · no pick ({skipped})"
    if won is None:
        return f"{head} · {title} · picked by an admin"
    return f"{head} · {title} · won {won}–{runner_up}"


# ------------------------------------------------------------------ service

class WeeklyService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self.lock = asyncio.Lock()
        self._rendered: dict[int, str] = {}     # message id -> last rendered ballot

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

    def post_link(self, thread_id: int | None) -> str:
        return f"<#{thread_id}>" if thread_id else "*(post deleted)*"

    async def open_ballot_week(self):
        rows = await self.bot.db.weeks_in_state("ballot")
        return rows[-1] if rows else None

    # tick ------------------------------------------------------------------
    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        async with self.lock:
            db = self.bot.db
            # Oldest first, so Sunday night ends this week's watch, then closes
            # next week's poll (winner → Up Next), then freezes the ballot.
            for row in await db.weeks_in_state("ballot", "ballot_closed", "poll", "picked"):
                await self._advance(row["week_start"], now)
            existing = {date.fromisoformat(r["week_start"]) for r in await self._all_weeks()}
            for week in weeks_to_create(now, self.tz, self.bot.config.first_watch_week, existing):
                await db.create_week(week.isoformat())
                log.info("Opened the ballot for watch week %s", week)
                await self._advance(week.isoformat(), now)
            for row in await db.weeks_in_state("ballot"):
                await self.refresh_ballot(row["week_start"])

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

    # ballot ----------------------------------------------------------------
    async def ballot_options(self, week_start: str) -> list[dict]:
        db = self.bot.db
        ranked = await db.ranked_movies(statuses=ELIGIBLE)
        return compose_ballot(await db.nominations(week_start), ranked, self.bot.config.poll_options)

    async def render_ballot(self, week_start: str) -> discord.Embed:
        db, cfg = self.bot.db, self.bot.config
        s = self.sched(week_start)
        row = await db.get_week(week_start)
        movies = {r["tmdb_id"]: r for r in await db.ranked_movies()}
        stored = [dict(o) for o in await db.options(week_start)]
        options = stored if row["state"] != "ballot" else await self.ballot_options(week_start)
        lines = []
        for i, o in enumerate(options, 1):
            m = movies.get(o["tmdb_id"])
            score = score_text(m) if m else ""
            who = f" · nominated by <@{o['nominated_by']}>" if o["source"] == "nomination" else ""
            lines.append(f"`{i:>2}.` {self.post_link(m['thread_id'] if m else None)} · {score}{who}")
        n_nom = sum(1 for o in options if o["source"] == "nomination")
        rng = date_range(s.week)
        if row["state"] == "ballot":
            head = (f"Nominate with **/nominate** until **{when(s.ballot_close, self.tz)}**: one film "
                    f"per member, run it again to swap, **/unnominate** to withdraw. Nominations go "
                    f"on the poll first (first come, first served, up to {cfg.poll_options}); the "
                    f"most-wanted films fill the rest. The poll opens here "
                    f"**{when(s.poll_open, self.tz)}**.")
        elif row["override_by"] and row["winner_tmdb_id"]:
            m = movies.get(row["winner_tmdb_id"])
            head = (f"🎬 An admin picked {self.post_link(m['thread_id'] if m else None)} for this "
                    f"week, so voting closed early.")
        elif row["state"] == "skipped":
            head = f"⏭️ No pick for this week: {row['skip_reason']}."
        else:
            poll = (f" → https://discord.com/channels/{cfg.guild_id}/{cfg.channels['watch_planning']}"
                    f"/{row['poll_message_id']}") if row["poll_message_id"] else ""
            head = f"🔒 Ballot closed. These films are on the poll{poll}."
        body = "\n".join(lines) or "*Nothing yet. Nominate a film, or 👍 some posts in <#%d>.*" % cfg.channels["movies"]
        e = discord.Embed(title=f"🗳️ Ballot · watch week {rng}",
                          description=f"{head}\n\n**Options ({len(options)}/{cfg.poll_options}, "
                                      f"{n_nom} nominated)**\n{body}",
                          color=0xE5A00D)
        e.set_footer(text="Most wanted = members who 👍'd the movie's post or have it on their "
                          "uploaded watchlist (📋), each counted once.")
        return e

    async def refresh_ballot(self, week_start: str) -> None:
        row = await self.bot.db.get_week(week_start)
        ch = self.channel("watch_planning")
        if row is None or ch is None:
            return
        embed = await self.render_ballot(week_start)
        if not row["ballot_message_id"]:
            msg = await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.update_week(week_start, ballot_message_id=msg.id,
                                          ballot_opened_at=utcnow())
            self._rendered[msg.id] = embed.description
            return
        if self._rendered.get(row["ballot_message_id"]) == embed.description:
            return
        try:
            await ch.get_partial_message(row["ballot_message_id"]).edit(
                embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            log.warning("Ballot message for %s was deleted; posting a new one", week_start)
            await self.bot.db.update_week(week_start, ballot_message_id=None)
            return await self.refresh_ballot(week_start)
        self._rendered[row["ballot_message_id"]] = embed.description

    async def close_ballot(self, row, now: datetime) -> None:
        week = row["week_start"]
        options = await self.ballot_options(week)
        await self.bot.db.set_options(week, options)
        await self.bot.db.update_week(week, state="ballot_closed", ballot_closed_at=utcnow())
        log.info("Ballot for %s closed with %d option(s)", week, len(options))
        if len(options) < self.bot.config.min_options:
            await self.skip(row, f"only {len(options)} film(s) on the ballot")
            return
        await self.refresh_ballot(week)

    # poll ------------------------------------------------------------------
    async def open_poll(self, row, now: datetime) -> None:
        week = row["week_start"]
        s = self.sched(week)
        ch = self.channel("watch_planning")
        if ch is None:
            raise RuntimeError("#watch-planning not found")
        if now >= s.poll_close:
            await self.skip(row, "the bot was offline during the poll week")
            return
        options = await self.bot.db.options(week)
        hours = min(max(math.ceil((s.poll_close - now).total_seconds() / 3600) + 1, 1),
                    MAX_POLL_HOURS)
        poll = discord.Poll(question=truncate(f"🎬 Which movie should we watch {date_range(s.week)}?",
                                              QUESTION_LIMIT),
                            duration=timedelta(hours=hours), multiple=False)
        lines = []
        for i, o in enumerate(options, 1):
            poll.add_answer(text=answer_label(o))
            who = f" · nominated by <@{o['nominated_by']}>" if o["source"] == "nomination" else ""
            lines.append(f"`{i:>2}.` {self.post_link(o['thread_id'])}{who}")
        content = (f"🗳️ **Vote for watch week {date_range(s.week)}.** The poll closes "
                   f"**{when(s.poll_close, self.tz)}**; ties go to the most-wanted film, then "
                   f"the one added first.\n" + "\n".join(lines))
        msg = await ch.send(content=truncate(content, 2000), poll=poll,
                            allowed_mentions=discord.AllowedMentions.none())
        await self.bot.db.update_week(week, state="poll", poll_message_id=msg.id,
                                      poll_opened_at=utcnow())
        log.info("Poll for %s posted (%s, %d h)", week, msg.id, hours)
        await self.refresh_ballot(week)

    async def _final_poll(self, row) -> discord.Message:
        ch = self.channel("watch_planning")
        msg = await ch.fetch_message(row["poll_message_id"])
        if msg.poll and not msg.poll.is_finalised():
            try:
                msg = await msg.end_poll()
            except discord.HTTPException as e:   # already ended on its own
                log.info("end_poll for %s: %s", row["week_start"], e)
        for _ in range(FINALIZE_TRIES):
            if msg.poll is None or msg.poll.is_finalised():
                break
            await asyncio.sleep(FINALIZE_WAIT)
            msg = await ch.fetch_message(row["poll_message_id"])
        return msg

    async def _collect_poll(self, row) -> dict[int, int] | None:
        """End the poll (if needed) and store its counts and voters. None if the
        poll message is gone."""
        try:
            msg = await self._final_poll(row)
        except discord.NotFound:
            return None
        votes: dict[int, int] = {}
        voters: dict[int, list[int]] = {}
        for answer in (msg.poll.answers if msg.poll else []):
            votes[answer.id] = answer.vote_count
            if answer.vote_count:
                voters[answer.id] = [u.id async for u in answer.voters(limit=None)]
        await self.bot.db.record_poll_results(row["week_start"], votes, voters)
        return votes

    async def close_poll(self, row, now: datetime) -> None:
        week = row["week_start"]
        db = self.bot.db
        votes = await self._collect_poll(row)
        if votes is None:
            await self.skip(row, "the poll message was deleted")
            return
        options = {o["position"]: o for o in await db.options(week)}
        results = []
        for pos, o in options.items():
            if o["title"] is None:      # post deleted since the ballot closed
                continue
            interest, thumbs, _ = await db.movie_interest(o["tmdb_id"])
            results.append({"tmdb_id": o["tmdb_id"], "votes": votes.get(pos, 0),
                            "interest": interest, "thumbs": thumbs, "added_at": o["added_at"],
                            "title": display(o), "thread_id": o["thread_id"]})
        total = sum(votes.values())
        picked = pick_winner(results, self.bot.config.tie_break)
        await db.update_week(week, poll_closed_at=utcnow(), total_votes=total)
        if picked is None:
            await self.skip(row, "nobody voted")
            return
        winner, runner_up = picked
        prev = await db.get_movie(winner["tmdb_id"])
        await db.update_week(week, winner_tmdb_id=winner["tmdb_id"],
                             winner_votes=winner["votes"], runner_up_votes=runner_up,
                             winner_prev_status=prev["status"] if prev else None)
        await db.set_status(winner["tmdb_id"], "up_next")
        await self._safe(self.bot.movies.retag(winner["tmdb_id"]), "retag winner")
        event = await self._safe(self.create_event(week, winner, now), "create event")
        s = self.sched(week)
        event_link = f"\n📅 Event: {event.url}" if event else ""
        text = (f"🎬 **Watch week {date_range(s.week)}: {winner['title']}**\n"
                f"Won the poll {winner['votes']}–{runner_up} ({total} vote{'s' * (total != 1)}). "
                f"Watch it any time from **{when(s.watch_start, self.tz)}** to "
                f"**{when(s.watch_end, self.tz)}**, then talk about it in the post-watch "
                f"discussion.\nPost: <#{winner['thread_id']}>{event_link}")
        ann = self.channel("announcements")
        ann_msg = await self._safe(ann.send(text, allowed_mentions=discord.AllowedMentions.none()),
                                   "announce") if ann else None
        planning = self.channel("watch_planning")
        if planning:
            await self._safe(planning.send(
                f"🏁 Poll closed: **{winner['title']}** wins {winner['votes']}–{runner_up} and is up "
                f"next ({date_range(s.week)}) → <#{winner['thread_id']}>",
                allowed_mentions=discord.AllowedMentions.none()), "poll result")
        await db.update_week(week, state="picked", event_id=event.id if event else None,
                             announcement_id=ann_msg.id if ann_msg else None)
        log.info("Watch week %s: %s won %d–%d", week, winner["title"], winner["votes"], runner_up)

    async def create_event(self, week: str, winner: dict, now: datetime):
        s = self.sched(week)
        guild = self.bot.get_guild(self.bot.config.guild_id)
        if guild is None or now >= s.watch_end:
            return None
        start = max(s.watch_start, now + timedelta(minutes=2))
        url = f"https://discord.com/channels/{guild.id}/{winner['thread_id']}"
        return await guild.create_scheduled_event(
            name=truncate(f"🎬 Watch week: {winner['title']}", 100),
            start_time=start, end_time=s.watch_end,
            entity_type=discord.EntityType.external, location=url,
            privacy_level=discord.PrivacyLevel.guild_only,
            description=truncate(f"This week's movie: {winner['title']}. Watch it any time this "
                                 f"week, then join the post-watch discussion. Post: {url}", 1000),
            reason="Weekly pick")

    # watch week ------------------------------------------------------------
    async def start_watch(self, row, now: datetime) -> None:
        """Pin the week's movie in #movies and its post-watch discussion in
        #post-watch-discussion. A forum has one pinned post, so this replaces
        last week's pins."""
        tmdb_id = row["winner_tmdb_id"]
        post = await self.bot.movies.find_post(tmdb_id) if tmdb_id else None
        if post is not None:
            await self._safe(post.edit(archived=False, pinned=True, reason="This week's movie"),
                             "pin #movies post")
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
        line = log_line(date.fromisoformat(week), title,
                        None if row["override_by"] else row["winner_votes"], row["runner_up_votes"])
        log_msg = await self._log(line)
        await db.update_week(week, state="done", ended_at=utcnow(),
                             log_message_id=log_msg.id if log_msg else None)
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
        rng = date_range(date.fromisoformat(week))
        planning = self.channel("watch_planning")
        if planning:
            await self._safe(planning.send(f"⏭️ No pick for watch week {rng}: {reason}."),
                             "skip notice")
        log_msg = await self._log(log_line(date.fromisoformat(week), None, skipped=reason))
        if log_msg:
            await self.bot.db.update_week(week, log_message_id=log_msg.id)
        await self._safe(self.refresh_ballot(week), "ballot refresh")

    async def _safe(self, coro, what: str):
        try:
            return await coro
        except discord.HTTPException:
            log.exception("Weekly pick: %s failed", what)
            return None

    # admin override ----------------------------------------------------------
    async def admin_pick(self, admin_id: int, which: str, tmdb_id: int,
                         now: datetime | None = None) -> str:
        """Set this or next week's movie, ending its poll early if it's open."""
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
                return f"❌ **{display(movie)}** is already Up Next for another week."
            if row is None:
                await db.create_week(ws)
                row = await db.get_week(ws)
            if row["state"] == "done":
                return "❌ That week is already over."
            notes = []
            if row["state"] == "poll":
                votes = await self._collect_poll(row)
                if votes is not None:
                    notes.append(f"ended the poll early ({sum(votes.values())} votes, kept for stats)")
                await db.update_week(ws, poll_closed_at=utcnow(),
                                     total_votes=sum(votes.values()) if votes else 0)
            elif row["state"] in ("ballot", "ballot_closed"):
                notes.append("closed the ballot early")
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
            await self._safe(self.bot.movies.retag(tmdb_id), "retag pick")
            winner = {"tmdb_id": tmdb_id, "title": display(movie), "thread_id": movie["thread_id"]}
            event = None
            if row["event_id"]:
                event = await self._safe(self.update_event(row["event_id"], ws, winner), "edit event")
            if event is None:
                event = await self._safe(self.create_event(ws, winner, now), "create event")
            await db.update_week(ws, event_id=event.id if event else None)
            event_link = f"\n📅 Event: {event.url}" if event else ""
            text = (f"🎬 **Watch week {date_range(week)}: {display(movie)}** (picked by an admin)\n"
                    f"Watch it any time until **{when(s.watch_end, self.tz)}**, then talk about it "
                    f"in the post-watch discussion.\nPost: <#{movie['thread_id']}>{event_link}")
            ann = self.channel("announcements")
            if ann:
                await self._safe(ann.send(text, allowed_mentions=discord.AllowedMentions.none()),
                                 "announce")
            planning = self.channel("watch_planning")
            if planning:
                await self._safe(planning.send(
                    f"🎬 An admin picked **{display(movie)}** for {date_range(week)} → "
                    f"<#{movie['thread_id']}>", allowed_mentions=discord.AllowedMentions.none()),
                    "pick notice")
            row = await db.get_week(ws)
            if now >= s.watch_start:
                await self.start_watch(row, now)
                notes.append("pinned it in both forums")
            log.info("%s picked TMDB %s for %s", admin_id, tmdb_id, ws)
        if row["ballot_message_id"]:
            await self._safe(self.refresh_ballot(ws), "ballot refresh")
        extra = f" ({'; '.join(notes)})" if notes else ""
        return f"✅ **{display(movie)}** is the movie for {date_range(week)}{extra}."

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
    async def nominate(self, user_id: int, tmdb_id: int) -> str:
        """Nominate (or swap to) a film on the open ballot. Returns a reply line."""
        async with self.lock:
            db = self.bot.db
            week = await self.open_ballot_week()
            if week is None:
                return "❌ No ballot is open right now."
            ws = week["week_start"]
            movie = await db.get_movie(tmdb_id)
            if movie is None:
                return "❌ That movie has no post."
            if movie["status"] not in ELIGIBLE:
                what = {"up_next": "is already Up Next", "watched": "was already watched together"}
                return f"❌ **{display(movie)}** {what.get(movie['status'], 'can’t be nominated')}."
            noms = await db.nominations(ws)
            mine = next((n for n in noms if n["user_id"] == user_id), None)
            other = next((n for n in noms if n["tmdb_id"] == tmdb_id), None)
            if other and other["user_id"] == user_id:
                return f"**{display(movie)}** is already your nomination."
            if other:
                return (f"**{display(movie)}** is already nominated by <@{other['user_id']}>, so "
                        f"it's on the ballot. 👍 its post to back it.")
            if mine is None and len(noms) >= self.bot.config.poll_options:
                return (f"❌ The ballot already has {len(noms)} nominations (the most it can hold). "
                        f"👍 the films you want instead; the most-wanted ones also make the poll.")
            await db.nominate(ws, user_id, tmdb_id)
            was = f" (instead of <#{mine['thread_id']}>)" if mine else ""
            log.info("%s nominated TMDB %s for %s", user_id, tmdb_id, ws)
        await self._safe(self.refresh_ballot(ws), "ballot refresh")
        return (f"✅ Nominated **{display(movie)}**{was} for watch week "
                f"{date_range(date.fromisoformat(ws))}. It goes on the poll when the ballot "
                f"closes {when(self.sched(ws).ballot_close, self.tz)}.")

    async def unnominate(self, user_id: int) -> str:
        async with self.lock:
            week = await self.open_ballot_week()
            if week is None or not await self.bot.db.withdraw_nomination(week["week_start"], user_id):
                return "You have no nomination on the open ballot."
        await self._safe(self.refresh_ballot(week["week_start"]), "ballot refresh")
        return "✅ Nomination withdrawn."

    async def schedule_text(self, user_id: int, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        db, cfg = self.bot.db, self.bot.config
        cur = monday_of(now.astimezone(self.tz).date())
        planning = cfg.channels.get("watch_planning")
        out = []
        for k, label in ((0, "This week"), (1, "Next week"), (2, "Week after")):
            week = cur + timedelta(days=7 * k)
            row = await db.get_week(week.isoformat())
            head = f"**{label} ({date_range(week)})**: "
            if row is None:
                if cfg.first_watch_week and week < cfg.first_watch_week:
                    out.append(head + "no pick (the weekly pick starts "
                               f"{date_range(cfg.first_watch_week)}).")
                else:
                    out.append(head + "no pick.")
                continue
            st = row["state"]
            s = self.sched(week)
            if st in ("picked", "done"):
                m = await db.get_movie(row["winner_tmdb_id"])
                name = f"**{display(m)}** · <#{m['thread_id']}>" if m else "the picked film"
                out.append(head + f"{name} (won {row['winner_votes']}–{row['runner_up_votes']}).")
            elif st == "skipped":
                out.append(head + f"no pick ({row['skip_reason']}).")
            elif st == "poll":
                link = f"https://discord.com/channels/{cfg.guild_id}/{planning}/{row['poll_message_id']}"
                out.append(head + f"poll open until {when(s.poll_close, self.tz)} → {link}")
            elif st == "ballot_closed":
                out.append(head + f"poll opens {when(s.poll_open, self.tz)} in <#{planning}>.")
            else:
                noms = await db.nominations(row["week_start"])
                mine = next((n for n in noms if n["user_id"] == user_id), None)
                you = (f" Your nomination: <#{mine['thread_id']}>." if mine
                       else " You haven't nominated anything; use **/nominate**.")
                link = (f" → https://discord.com/channels/{cfg.guild_id}/{planning}/"
                        f"{row['ballot_message_id']}") if row["ballot_message_id"] else ""
                out.append(head + f"ballot open until {when(s.ballot_close, self.tz)}{link}.{you}")
        return "\n".join(out)
