"""Weekly pick: nominations → ranked vote → watch week.

For a watch week starting Monday T (America/Chicago), everything is decided in
the week before it:
  nominations  T-7 Mon 12:00 AM → T-4 Thu 11:59 PM   /nominate (1 per member, swap or
               withdraw any time until the close). Only nominated films are on the
               ballot. A bot message in #watch-planning shows them live.
  vote         T-3 Fri 12:00 AM → T-1 Sun 11:59 PM   ranked choice on the frozen
               options: members rank as many as they like through a 🗳️ Vote button;
               counted by instant runoff at the close.
  watch        T Mon 12:00 AM → T+6 Sun 11:59 PM     winner tagged Up Next, server event,
               announcement; at the end it's tagged Watched and logged in #watch-log.
So every week runs this week's watch and next week's nominations/vote. Sunday
night ends this week's watch, then closes the vote for next week.

Each week is a row in `weeks` (ballot → ballot_closed → voting → picked → done,
or skipped); a 30 s tick moves rows along whenever a deadline has passed, so a
restart (or downtime) just resumes where it left off.
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

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

# Statuses that can be nominated / put on a ballot. Watched (solo) is fine:
# one member having seen it doesn't rule it out for the group.
ELIGIBLE = ("backlog", "watched_solo")
LABEL_LIMIT = 100          # Discord select option label
MAX_BALLOT = 25            # Discord select menu options: the most nominations a ballot holds


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
    def ballot_open(self) -> datetime:          # Mon 12:00 AM the week before
        return local_midnight(self.week - timedelta(days=7), self.tz)

    @property
    def vote_open(self) -> datetime:            # Fri 12:00 AM the week before
        return local_midnight(self.week - timedelta(days=3), self.tz)

    @property
    def ballot_close(self) -> datetime:         # Thu 11:59 PM
        return self.vote_open - timedelta(minutes=1)

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
    """Watch weeks whose ballot should exist now but has no row yet.

    A week is only started while its vote can still run (before the vote
    closes), so a long outage never opens a vote that ends straight away.
    """
    if first is None:
        return []
    cur = monday_of(now.astimezone(tz).date())
    out = []
    for k in (0, 1):
        week = cur + timedelta(days=7 * k)
        s = Schedule(week, tz)
        if week >= first and week not in existing and s.ballot_open <= now < s.vote_close:
            out.append(week)
    return out


def next_action(state: str, s: Schedule, now: datetime, pinned: bool = False) -> str | None:
    """What a week in `state` should do next at `now` (None = nothing yet)."""
    if state == "ballot" and now >= s.ballot_close:
        return "close_ballot"
    if state == "ballot_closed" and now >= s.vote_open:
        return "open_vote"
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


def display(row) -> str:
    return f"{row['title']} ({row['year']})" if row["year"] else row["title"]


def option_label(row) -> str:
    year = f" ({row['year']})" if row["year"] else ""
    return truncate(row["title"], LABEL_LIMIT - len(year)) + year


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ------------------------------------------------------------------ ballot / winner

def compose_ballot(nominations: list, ranked: list, size: int = MAX_BALLOT) -> list[dict]:
    """The ballot is the nominations only, in the order made (eligible films
    only). `ranked` supplies each film's interest/👍 for display and ties."""
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
    return options


def instant_runoff(ballots: list[list[int]], candidates: list[dict],
                   tie_break: list[str]) -> dict | None:
    """Count ranked ballots by instant runoff.

    ballots: each member's ranking (tmdb ids, best first; may be partial).
    candidates: dicts with tmdb_id, interest, thumbs, added_at.
    Each round every ballot counts for its highest-ranked film still in the
    race. More than half of the ballots still counting wins. Otherwise the
    film(s) with no votes go out together, or else the one with the fewest;
    ties for last go out by fewest 1st choices, then `tie_break` (least
    interest, fewest 👍, added last). Returns None if nobody voted, else
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
    """Round-by-round summary for #watch-planning."""
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


def interest_text(interest: int, thumbs: int, listed: int) -> str:
    parts = [f"👍 {thumbs}"] + ([f"📋 {listed}"] if listed else [])
    return " · ".join(parts)


def score_text(r) -> str:
    """'👍 3' when only votes count; '**4** want it (👍 3 · 📋 2)' with watchlists
    (a member with both counts once, so the parts can add up to more)."""
    if not r["listed"]:
        return f"👍 {r['thumbs']}"
    return f"**{r['interest']}** want it ({interest_text(r['interest'], r['thumbs'], r['listed'])})"


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


# ------------------------------------------------------------------ service

class WeeklyService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self.lock = asyncio.Lock()
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
            # next week's vote (winner → Up Next).
            for row in await db.weeks_in_state("ballot", "ballot_closed", "voting", "picked"):
                await self._advance(row["week_start"], now)
            existing = {date.fromisoformat(r["week_start"]) for r in await self._all_weeks()}
            for week in weeks_to_create(now, self.tz, self.bot.config.first_watch_week, existing):
                await db.create_week(week.isoformat())
                log.info("Opened the ballot for watch week %s", week)
                await self._advance(week.isoformat(), now)
            for row in await db.weeks_in_state("ballot", "ballot_closed", "voting"):
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
        return compose_ballot(await db.nominations(week_start), ranked)

    async def render_ballot(self, week_start: str) -> discord.Embed:
        db = self.bot.db
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
        rng = date_range(s.week)
        st = row["state"]
        if st == "ballot":
            head = (f"Nominate with **/nominate** until **{when(s.ballot_close, self.tz)}**: one film "
                    f"per member, run it again to swap, **/unnominate** to withdraw. Only "
                    f"nominated films are on the ballot. Voting opens here "
                    f"**{when(s.vote_open, self.tz)}**.")
        elif st == "ballot_closed":
            head = (f"🔒 Nominations are closed. Voting opens here "
                    f"**{when(s.vote_open, self.tz)}**.")
        elif st == "voting":
            n = await db.voter_count(week_start)
            head = (f"🗳️ **Voting is open until {when(s.vote_close, self.tz)}.** Click **Vote** "
                    f"and rank as many films as you like, best first. Your lower picks only "
                    f"count if your higher ones are knocked out (instant runoff), so ranking "
                    f"more never hurts your favorite.\n"
                    f"**{n} member{' has' if n == 1 else 's have'} voted so far.** Results are "
                    f"posted when voting closes.")
        elif row["override_by"] and row["winner_tmdb_id"]:
            m = movies.get(row["winner_tmdb_id"])
            head = (f"🎬 An admin picked {self.post_link(m['thread_id'] if m else None)} for this "
                    f"week, so voting closed early.")
        elif st == "skipped":
            head = f"⏭️ No pick for this week: {row['skip_reason']}."
        elif row["winner_tmdb_id"] and row["winner_votes"] is None:
            m = movies.get(row["winner_tmdb_id"])
            head = (f"🏁 {self.post_link(m['thread_id'] if m else None)} was the only nomination, "
                    f"so it wins without a vote.")
        else:
            m = movies.get(row["winner_tmdb_id"])
            n = row["total_votes"] or 0
            head = (f"🏁 Voting closed. {self.post_link(m['thread_id'] if m else None)} "
                    f"{how_picked(row)} ({n} voter{'s' * (n != 1)}).")
        body = "\n".join(lines) or "*No nominations yet. Use **/nominate** to add one.*"
        e = discord.Embed(title=f"🗳️ Ballot · watch week {rng}",
                          description=f"{head}\n\n**Nominated films ({len(options)})**\n{body}",
                          color=0xE5A00D)
        e.set_footer(text="👍 / 📋 = members who 👍'd the movie's post or have it on their "
                          "uploaded watchlist, shown for reference.")
        return e

    async def refresh_ballot(self, week_start: str) -> None:
        from .vote_ui import ballot_view
        row = await self.bot.db.get_week(week_start)
        ch = self.channel("watch_planning")
        if row is None or ch is None:
            return
        embed = await self.render_ballot(week_start)
        voting = row["state"] == "voting"
        view = ballot_view(week_start) if voting else None
        key = f"{voting}|{embed.description}"
        if not row["ballot_message_id"]:
            msg = await ch.send(embed=embed, view=view,
                                allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.update_week(week_start, ballot_message_id=msg.id,
                                          ballot_opened_at=utcnow())
            self._rendered[msg.id] = key
            return
        if self._rendered.get(row["ballot_message_id"]) == key:
            return
        try:
            await ch.get_partial_message(row["ballot_message_id"]).edit(
                embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            log.warning("Ballot message for %s was deleted; posting a new one", week_start)
            await self.bot.db.update_week(week_start, ballot_message_id=None)
            return await self.refresh_ballot(week_start)
        self._rendered[row["ballot_message_id"]] = key

    def ballot_url(self, row) -> str:
        cfg = self.bot.config
        return (f"https://discord.com/channels/{cfg.guild_id}/{cfg.channels['watch_planning']}/"
                f"{row['ballot_message_id']}") if row["ballot_message_id"] else ""

    async def close_ballot(self, row, now: datetime) -> None:
        week = row["week_start"]
        options = await self.ballot_options(week)
        await self.bot.db.set_options(week, options)
        await self.bot.db.update_week(week, state="ballot_closed", ballot_closed_at=utcnow())
        log.info("Ballot for %s closed with %d option(s)", week, len(options))
        if not options:
            await self.skip(row, "nobody nominated a film")
            return
        if len(options) == 1:
            o = (await self.bot.db.options(week))[0]
            if o["title"] is not None:          # its post still exists
                s = self.sched(week)
                await self._crown(
                    row, o, now, how=SOLE_NOMINATION,
                    announce="It was the only nomination, so it wins without a vote.",
                    planning=(f"🏁 **{display(o)}** was the only nomination, so it wins without "
                              f"a vote and is up next ({date_range(s.week)}) → <#{o['thread_id']}>"),
                    poll_closed_at=utcnow(), total_votes=0)
                return
        await self.refresh_ballot(week)

    # vote ------------------------------------------------------------------
    async def open_vote(self, row, now: datetime) -> None:
        week = row["week_start"]
        s = self.sched(week)
        if now >= s.vote_close:
            await self.skip(row, "the bot was offline during the vote")
            return
        await self.bot.db.update_week(week, state="voting", poll_opened_at=utcnow())
        log.info("Voting for %s opened", week)
        await self.refresh_ballot(week)
        row = await self.bot.db.get_week(week)
        planning = self.channel("watch_planning")
        if planning:
            link = self.ballot_url(row)
            await self._safe(planning.send(
                f"🗳️ **Voting is open for watch week {date_range(s.week)}** until "
                f"**{when(s.vote_close, self.tz)}**. Click **Vote** on the ballot and rank as many "
                f"films as you like" + (f" → {link}" if link else "."),
                allowed_mentions=discord.AllowedMentions.none()), "vote notice")

    async def voting_week(self, week_start: str, now: datetime | None = None):
        """The week row if its vote is open right now, else None."""
        now = now or datetime.now(timezone.utc)
        row = await self.bot.db.get_week(week_start)
        if row is None or row["state"] != "voting" or now >= self.sched(week_start).vote_close:
            return None
        return row

    async def picker(self, week_start: str, user_id: int, notice: str | None = None):
        """(text, view) for a member's private ranking picker."""
        from .vote_ui import picker_view
        s = self.sched(week_start)
        options = [o for o in await self.bot.db.options(week_start) if o["title"] is not None]
        names = {o["tmdb_id"]: option_label(o) for o in options}
        mine = [t for t in await self.bot.db.ranking(week_start, user_id) if t in names]
        lines = [f"`{i}.` {names[t]}" for i, t in enumerate(mine, 1)]
        left = [o for o in options if o["tmdb_id"] not in mine]
        if not mine:
            status = "*Nothing ranked yet. Pick your 1st choice below.*"
        elif left:
            status = "\n".join(lines) + f"\n-# Saved. Add a {ordinal(len(mine) + 1)} choice if you like."
        else:
            status = "\n".join(lines) + "\n-# Saved. You've ranked every film."
        text = (f"🗳️ **Your ranking · watch week {date_range(s.week)}**\n{status}\n"
                f"-# Every change is saved right away. You can change it until "
                f"{when(s.vote_close, self.tz)}. Your 2nd choice only counts if your 1st is "
                f"knocked out, and so on.")
        if notice:
            text = f"{notice}\n{text}"
        choices = [(o["tmdb_id"], names[o["tmdb_id"]]) for o in left]
        return text, picker_view(week_start, choices, len(mine) + 1, bool(mine))

    async def change_ranking(self, week_start: str, user_id: int, op: str,
                             tmdb_id: int | None = None) -> str | None:
        """Apply add/undo/clear. Returns an error line, or None when it worked."""
        db = self.bot.db
        if await self.voting_week(week_start) is None:
            return "❌ Voting for that week is closed."
        if op == "add":
            valid = {o["tmdb_id"] for o in await db.options(week_start) if o["title"] is not None}
            if tmdb_id not in valid:
                return "❌ That film isn't on this ballot."
            if tmdb_id in await db.ranking(week_start, user_id):
                return "That film is already in your ranking."
            await db.add_rank(week_start, user_id, tmdb_id)
        elif op == "undo":
            await db.undo_rank(week_start, user_id)
        elif op == "clear":
            await db.clear_ranking(week_start, user_id)
        self._later(self.refresh_ballot(week_start), "ballot refresh")
        return None

    def _later(self, coro, what: str) -> None:
        task = asyncio.create_task(self._safe(coro, what))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close_vote(self, row, now: datetime) -> None:
        week = row["week_start"]
        db = self.bot.db
        options = [o for o in await db.options(week) if o["title"] is not None]  # post deleted
        candidates = []
        for o in options:
            interest, thumbs, _ = await db.movie_interest(o["tmdb_id"])
            candidates.append({"tmdb_id": o["tmdb_id"], "interest": interest, "thumbs": thumbs,
                               "added_at": o["added_at"]})
        ballots = await db.all_rankings(week)
        result = instant_runoff(list(ballots.values()), candidates, self.bot.config.tie_break)
        if result is None:
            await db.update_week(week, poll_closed_at=utcnow(), total_votes=0)
            await self.skip(row, "nobody voted")
            return
        await db.record_first_choices(week, result["first_choices"])
        await db.update_week(week, poll_closed_at=utcnow(), total_votes=result["voters"],
                             vote_rounds=json.dumps(result["rounds"]))
        by_id = {o["tmdb_id"]: o for o in options}
        w = by_id[result["winner"]]
        won, ru, n_rounds = result["winner_votes"], result["runner_up_votes"], len(result["rounds"])
        voters = result["voters"]
        names = {tid: display(o) for tid, o in by_id.items()}
        how = won_text(won, ru, n_rounds)
        await self._crown(
            row, w, now, how=how,
            announce=f"Winner of the ranked vote ({how}, {voters} voter{'s' * (voters != 1)}).",
            planning=(f"🏁 **Voting closed: {display(w)} wins** and is up next "
                      f"({date_range(self.sched(week).week)}) → <#{w['thread_id']}>\n"
                      f"-# {voters} voter{'s' * (voters != 1)}. Instant runoff: each round, "
                      f"every ballot counts for its highest-ranked film still in the race.\n"
                      + runoff_text(result, names)),
            winner_votes=won, runner_up_votes=ru)

    async def _crown(self, row, w, now: datetime, *, how: str, announce: str, planning: str,
                     **fields) -> None:
        """Make option row `w` the week's movie: Up Next, event, announcement,
        a note in #watch-planning, state picked."""
        week = row["week_start"]
        db = self.bot.db
        winner = {"tmdb_id": w["tmdb_id"], "title": display(w), "thread_id": w["thread_id"]}
        prev = await db.get_movie(winner["tmdb_id"])
        await db.update_week(week, winner_tmdb_id=winner["tmdb_id"],
                             winner_prev_status=prev["status"] if prev else None, **fields)
        await db.set_status(winner["tmdb_id"], "up_next")
        await self._safe(self.bot.movies.retag(winner["tmdb_id"]), "retag winner")
        event = await self._safe(self.create_event(week, winner, now), "create event")
        s = self.sched(week)
        event_link = f"\n📅 Event: {event.url}" if event else ""
        text = (f"🎬 **Watch week {date_range(s.week)}: {winner['title']}**\n"
                f"{announce} Watch it any time from **{when(s.watch_start, self.tz)}** to "
                f"**{when(s.watch_end, self.tz)}**, then talk about it in the post-watch "
                f"discussion.\nPost: <#{winner['thread_id']}>{event_link}")
        ann = self.channel("announcements")
        ann_msg = await self._safe(ann.send(text, allowed_mentions=discord.AllowedMentions.none()),
                                   "announce") if ann else None
        ch = self.channel("watch_planning")
        if ch:
            await self._safe(ch.send(truncate(planning, 2000),
                                     allowed_mentions=discord.AllowedMentions.none()), "pick notice")
        await db.update_week(week, state="picked", event_id=event.id if event else None,
                             announcement_id=ann_msg.id if ann_msg else None)
        log.info("Watch week %s: %s, %s", week, winner["title"], how)
        await self._safe(self.refresh_ballot(week), "ballot refresh")

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
        line = log_line(date.fromisoformat(week), title, how_picked(row))
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
                return f"❌ **{display(movie)}** is already Up Next for another week."
            if row is None:
                await db.create_week(ws)
                row = await db.get_week(ws)
            if row["state"] == "done":
                return "❌ That week is already over."
            notes = []
            if row["state"] == "voting":
                n = await db.voter_count(ws)
                notes.append(f"closed voting early ({n} ballot{'s' * (n != 1)} kept for stats)")
                await db.update_week(ws, poll_closed_at=utcnow(), total_votes=n)
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
            if mine is None and len(noms) >= MAX_BALLOT:
                return (f"❌ The ballot already has {len(noms)} nominations, the most it can hold "
                        f"(Discord's dropdown limit). You can still vote on them from Friday.")
            await db.nominate(ws, user_id, tmdb_id)
            was = f" (instead of <#{mine['thread_id']}>)" if mine else ""
            log.info("%s nominated TMDB %s for %s", user_id, tmdb_id, ws)
        await self._safe(self.refresh_ballot(ws), "ballot refresh")
        return (f"✅ Nominated **{display(movie)}**{was} for watch week "
                f"{date_range(date.fromisoformat(ws))}. Nominations close "
                f"{when(self.sched(ws).ballot_close, self.tz)}; voting runs until "
                f"{when(self.sched(ws).vote_close, self.tz)}.")

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
        for k, label in ((0, "This week"), (1, "Next week")):
            week = cur + timedelta(days=7 * k)
            row = await db.get_week(week.isoformat())
            head = f"**{label} ({date_range(week)})**: "
            s = self.sched(week)
            if row is None:
                if cfg.first_watch_week and week < cfg.first_watch_week:
                    out.append(head + "no pick (the weekly pick starts "
                               f"{date_range(cfg.first_watch_week)}).")
                elif now < s.ballot_open:
                    out.append(head + f"nominations open {when(s.ballot_open, self.tz)}.")
                else:
                    out.append(head + "no pick.")
                continue
            st = row["state"]
            link = f" → {self.ballot_url(row)}" if row["ballot_message_id"] else ""
            if st in ("picked", "done"):
                m = await db.get_movie(row["winner_tmdb_id"])
                name = f"**{display(m)}** · <#{m['thread_id']}>" if m else "the picked film"
                out.append(head + f"{name} ({how_picked(row)}).")
            elif st == "skipped":
                out.append(head + f"no pick ({row['skip_reason']}).")
            elif st == "voting":
                ranked = await db.ranking(row["week_start"], user_id)
                you = (f" You've ranked {len(ranked)} film{'s' * (len(ranked) != 1)}."
                       if ranked else " You haven't voted yet: click **Vote** on the ballot.")
                out.append(head + f"voting open until {when(s.vote_close, self.tz)}{link}.{you}")
            elif st == "ballot_closed":
                out.append(head + f"nominations closed; voting opens {when(s.vote_open, self.tz)} "
                           f"in <#{planning}>.")
            else:
                noms = await db.nominations(row["week_start"])
                mine = next((n for n in noms if n["user_id"] == user_id), None)
                you = (f" Your nomination: <#{mine['thread_id']}>." if mine
                       else " You haven't nominated anything; use **/nominate**.")
                out.append(head + f"nominations open until {when(s.ballot_close, self.tz)}, "
                           f"voting {when(s.vote_open, self.tz)} → {when(s.vote_close, self.tz)}"
                           f"{link}.{you}")
        return "\n".join(out)
