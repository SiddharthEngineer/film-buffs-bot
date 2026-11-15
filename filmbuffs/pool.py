"""Phases 10 and 12: nomination pools, live ballots and rolling rankings.

There is one pool per vote: 'main' for the main server's weekly vote, and one
per private group (server.yaml `groups:`) for its monthly vote. They are fully
separate: a film can sit in several pools, and each member keeps up to 3
active nominations, ranked 1–3, in each pool. A pool's ballot is each member's
highest-ranked nomination that is still eligible. When a film wins it leaves
that pool, and its nominator's next nomination moves up onto the ballot by
itself. Nominations roll over until withdrawn or won.

Eligible: on the main ballot, a film tagged Backlog or Watched (solo); on a
group's ballot, any film with a post that the group hasn't picked before.

Rankings are kept per track ('weekly', 'group:<key>') and roll over from one
vote to the next. A film that leaves a ballot drops out of that track's
rankings and the later ranks move up; a film that joins is just unranked.

`RankedTrack` is the picker logic both vote services share.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from .db import utcnow
from .groups import MAIN

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

ELIGIBLE = ("backlog", "watched_solo")
MAX_NOMINATIONS = 3
MAX_BALLOT = 25            # Discord select menu options: the most films a ballot holds
NEW_FOR = timedelta(hours=48)    # 🆕 next to films that joined the ballot recently
LABEL_LIMIT = 100


def shared_lock(bot) -> asyncio.Lock:
    """One lock for everything that changes the pool, ballots or votes."""
    lock = getattr(bot, "vote_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bot.vote_lock = lock
    return lock


def display(row) -> str:
    return f"{row['title']} ({row['year']})" if row["year"] else row["title"]


def post_url(guild_id: int, thread_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def post_link(guild_id: int, row, text: str | None = None) -> str:
    """A masked link to a movie's #movies post, labelled with its title (or `text`).

    Not a `<#thread_id>` mention: the phone app shows a forum post it hasn't
    loaded (most of them are archived) as "#unknown"; a link always shows its
    label and opens the post."""
    if row is None or not row["thread_id"]:
        return "*(post deleted)*"
    label = text or discord.utils.escape_markdown(display(row))
    label = label.replace("[", "(").replace("]", ")")   # brackets would end the label
    return f"[{label}]({post_url(guild_id, row['thread_id'])})"


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def option_label(row) -> str:
    year = f" ({row['year']})" if row["year"] else ""
    return truncate(row["title"], LABEL_LIMIT - len(year)) + year


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def eligible(row) -> bool:
    """Main weekly ballot: the film has a post and is Backlog or Watched (solo)."""
    return row["title"] is not None and row["status"] in ELIGIBLE


def group_eligible(watched) -> "callable":
    """A group's ballot: the film has a post and the group hasn't picked it before."""
    return lambda row: row["title"] is not None and row["tmdb_id"] not in watched


def desired_entries(pool_rows, current: list[tuple[int, int]],
                    size: int = MAX_BALLOT, ok=eligible) -> tuple[list[tuple[int, int]], list[int],
                                                     list[tuple[int, int]]]:
    """Work out the ballot from the pool.

    pool_rows: pool nominations (user_id, rank, tmdb_id, nominated_at, title,
    status), any order. current: the ballot now as [(tmdb_id, user_id)] in
    order. Returns (add, remove, owner_changes): films joining (in the order
    their nominations were made), films leaving, and films that stay but now
    belong to another member. Films already on the ballot keep their place;
    new ones join while there's room (Discord's 25-option dropdown).
    """
    best: dict[int, dict] = {}
    for r in sorted(pool_rows, key=lambda r: (r["user_id"], r["rank"])):
        if r["user_id"] not in best and ok(r):
            best[r["user_id"]] = r
    want = {r["tmdb_id"]: r for r in best.values()}
    remove = [t for t, _ in current if t not in want]
    owner_changes = [(t, want[t]["user_id"]) for t, u in current
                     if t in want and want[t]["user_id"] != u]
    kept = {t for t, _ in current if t in want}
    new = sorted((r for t, r in want.items() if t not in kept),
                 key=lambda r: (r["nominated_at"], r["user_id"]))
    room = max(0, size - len(kept))
    add = [(r["tmdb_id"], r["user_id"]) for r in new[:room]]
    return add, remove, owner_changes


def place(current: list[int], tmdb_id: int, rank: int | None,
          limit: int = MAX_NOMINATIONS) -> tuple[list[int] | None, int | None, str | None]:
    """Put a film into a member's list. Returns (new list, film it replaced, error).

    No rank: added at the end, if there's room. A rank: inserted there (later
    ones move down) while there's room; with a full list it replaces the film
    at that rank. A film already in the list moves to the rank."""
    if tmdb_id in current:
        if rank is None or current.index(tmdb_id) == min(rank, len(current)) - 1:
            return None, None, "same"
        rest = [t for t in current if t != tmdb_id]
        rest.insert(min(rank, len(current)) - 1, tmdb_id)
        return rest, None, None
    if rank is None:
        if len(current) >= limit:
            return None, None, "full"
        return current + [tmdb_id], None, None
    if len(current) < limit:
        out = list(current)
        out.insert(min(rank, len(current) + 1) - 1, tmdb_id)
        return out, None, None
    out = list(current)
    replaced = out[rank - 1]
    out[rank - 1] = tmdb_id
    return out, replaced, None


class PoolService:
    """One nomination pool and its live ballot: grp 'main' (weekly) or a group key."""

    def __init__(self, bot: "FilmBuffsBot", grp: str = MAIN, track: str = "weekly",
                 name: str | None = None):
        self.bot = bot
        self.grp = grp
        self.track = track
        self.name = name            # the group's name; None for the main server
        self.lock = shared_lock(bot)
        self._tasks: set[asyncio.Task] = set()

    @property
    def is_group(self) -> bool:
        return self.grp != MAIN

    async def _ok(self):
        if not self.is_group:
            return eligible
        return group_eligible(await self.bot.db.group_watched(self.grp))

    def _vote(self):
        """The vote service this pool feeds."""
        if self.is_group:
            return getattr(self.bot, "group_votes", {}).get(self.grp)
        return getattr(self.bot, "weekly", None)

    # ballot ------------------------------------------------------------------
    async def sync(self) -> bool:
        """Bring the ballot in line with the pool (call with the lock held).
        Films that left drop out of this track's rankings. Returns True if anything changed."""
        db = self.bot.db
        current = [(r["tmdb_id"], r["user_id"]) for r in await db.ballot_entries(self.grp)]
        add, remove, owners = desired_entries(await db.pool(grp=self.grp), current,
                                              ok=await self._ok())
        if not (add or remove or owners):
            return False
        await db.set_ballot_entries(add, remove, owners, grp=self.grp)
        await db.drop_from_rankings(remove, self.track)
        if add or remove:
            log.info("Ballot %s: +%s -%s", self.grp, [t for t, _ in add], remove)
        return True

    async def entries(self) -> list:
        """The live ballot (films whose post still exists)."""
        return [e for e in await self.bot.db.ballot_entries(self.grp) if e["title"] is not None]

    async def picked(self, tmdb_id: int) -> None:
        """A film was picked: it leaves this pool and its ballot (call with the lock held)."""
        await self.bot.db.remove_from_pool(tmdb_id, self.grp)
        await self.sync()

    def refresh_soon(self) -> None:
        svc = self._vote()
        if svc is not None:
            self._later(svc.refresh_open(), f"{self.grp} ballot refresh")

    def _later(self, coro, what: str) -> None:
        async def run():
            try:
                await coro
            except discord.HTTPException:
                log.exception("%s failed", what)
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # members -------------------------------------------------------------------
    async def nominate(self, user_id: int, tmdb_id: int, rank: int | None = None) -> str:
        db = self.bot.db
        grp = self.grp
        async with self.lock:
            movie = await db.get_movie(tmdb_id)
            if movie is None:
                return "❌ That movie has no post."
            if self.is_group:
                watched = await db.group_watched(grp)
                if tmdb_id in watched:
                    from .monthly import month_name
                    return (f"❌ {self.name} already picked **{display(movie)}** "
                            f"({month_name(watched[tmdb_id])}).")
            elif movie["status"] not in ELIGIBLE:
                what = {"up_next": "is already Up Next", "watched": "was already watched together"}
                return f"❌ **{display(movie)}** {what.get(movie['status'], 'can’t be nominated')}."
            owner = await db.pool_owner(tmdb_id, grp)
            if owner is not None and owner != user_id:
                return (f"**{display(movie)}** is already nominated by <@{owner}>. Rank it "
                        f"on the ballot to back it.")
            mine = await db.pool(user_id, grp)
            ids = [r["tmdb_id"] for r in mine]
            new, replaced, err = place(ids, tmdb_id, rank)
            if err == "same":
                return f"**{display(movie)}** is already your #{ids.index(tmdb_id) + 1}."
            if err == "full":
                return (f"❌ You already have {MAX_NOMINATIONS} nominations{self._here()}. Add "
                        f"`rank:` to replace one, or **/unnominate** one first. **/nominations** "
                        f"shows them.")
            at = {r["tmdb_id"]: r["nominated_at"] for r in mine}
            now = utcnow()
            await db.set_pool(user_id, [(t, at.get(t, now)) for t in new], grp)
            await self.sync()
            log.info("%s nominated TMDB %s in %s (list now %s)", user_id, tmdb_id, grp, new)
            gone = await db.get_movie(replaced) if replaced else None
            summary = await self._summary(user_id)
        self.refresh_soon()
        where = f"your #{new.index(tmdb_id) + 1}"
        was = f", replacing **{display(gone)}**" if gone else ""
        verb = "Moved" if tmdb_id in ids else "Nominated"
        return f"✅ {verb} **{display(movie)}** to {where}{self._here()}{was}.\n{summary}"

    def _here(self) -> str:
        return f" for {self.name}" if self.is_group else ""

    async def unnominate(self, user_id: int, tmdb_id: int) -> str:
        db = self.bot.db
        async with self.lock:
            mine = await db.pool(user_id, self.grp)
            if tmdb_id not in [r["tmdb_id"] for r in mine]:
                return (f"That film isn't one of your nominations{self._here()}. **/nominations** "
                        f"shows them.")
            await db.set_pool(user_id, [(r["tmdb_id"], r["nominated_at"]) for r in mine
                                        if r["tmdb_id"] != tmdb_id], self.grp)
            await self.sync()
            movie = await db.get_movie(tmdb_id)
            summary = await self._summary(user_id)
        self.refresh_soon()
        name = display(movie) if movie else f"TMDB {tmdb_id}"
        return f"✅ Withdrew **{name}**{self._here()}.\n{summary}"

    async def change(self, user_id: int, op: str, rank: int) -> str | None:
        """Buttons on the private list: 'up' (swap with the one above) or 'del'."""
        db = self.bot.db
        async with self.lock:
            mine = [(r["tmdb_id"], r["nominated_at"]) for r in await db.pool(user_id, self.grp)]
            if not 1 <= rank <= len(mine) or (op == "up" and rank == 1):
                return "That nomination has changed; here's your list now."
            if op == "up":
                mine[rank - 2], mine[rank - 1] = mine[rank - 1], mine[rank - 2]
            else:
                del mine[rank - 1]
            await db.set_pool(user_id, mine, self.grp)
            await self.sync()
        self.refresh_soon()
        return None

    async def _summary(self, user_id: int) -> str:
        db = self.bot.db
        rows = await db.pool(user_id, self.grp)
        head = f"**Your {self.name} nominations**" if self.is_group else "**Your nominations**"
        if not rows:
            return (f"-# You have no nominations{self._here()}. **/nominate** adds one"
                    + (" (run it in this group's channels)." if self.is_group else "."))
        on = {e["tmdb_id"] for e in await db.ballot_entries(self.grp)}
        ok = await self._ok()
        watched = await db.group_watched(self.grp) if self.is_group else {}
        lines = []
        for r in rows:
            name = display(r) if r["title"] is not None else f"*(post deleted, TMDB {r['tmdb_id']})*"
            note = ""
            if r["tmdb_id"] in on:
                note = " · 🗳️ on the ballot"
            elif not ok(r):
                if r["title"] is None:
                    why = "post deleted"
                elif self.is_group:
                    why = "already picked" if r["tmdb_id"] in watched else "not eligible"
                else:
                    why = {"up_next": "Up Next", "watched": "watched together"}.get(
                        r["status"], "post deleted")
                note = f" · not eligible now ({why})"
            lines.append(f"`{r['rank']}.` {name}{note}")
        tail = ""
        if not any(r["tmdb_id"] in on for r in rows) and any(ok(r) for r in rows):
            tail = "\n-# The ballot is full (25 films); yours joins when a spot opens."
        what = "monthly" if self.is_group else "weekly"
        return (head + "\n" + "\n".join(lines) +
                f"\n-# Your highest eligible one is on the {what} ballot. If it wins, the next "
                f"one moves up." + tail)

    async def my_view(self, user_id: int, notice: str | None = None):
        from .vote_ui import nominations_view
        text = await self._summary(user_id)
        n = len(await self.bot.db.pool(user_id, self.grp))
        if notice:
            text = f"{notice}\n{text}"
        return text, nominations_view(n, self.grp)


class RankedTrack:
    """Picker logic shared by the weekly and the group monthly votes. Subclasses set
    `pool` (their PoolService), `track`, `emoji`, `label` and `unit`, and implement
    open_row(period, now), close_time(period), title(period) and refresh_ballot(period)."""
    track = ""
    emoji = "🗳️"
    label = ""          # "weekly", "OSU monthly"
    unit = "week"       # what the next vote is: next week's / next month's
    pool: PoolService

    async def open_row(self, period: str, now: datetime | None = None):
        raise NotImplementedError

    def close_time(self, period: str) -> datetime:
        raise NotImplementedError

    def title(self, period: str) -> str:
        raise NotImplementedError

    async def refresh_ballot(self, period: str) -> None:
        raise NotImplementedError

    def link(self, row, text: str | None = None) -> str:
        return post_link(self.bot.config.guild_id, row, text)

    async def ballot_films(self) -> list:
        return await self.pool.entries()

    async def voter_count(self) -> int:
        on = {e["tmdb_id"] for e in await self.ballot_films()}
        ranks = await self.bot.db.all_track_rankings(self.track)
        return sum(1 for ts in ranks.values() if any(t in on for t in ts))

    async def picker(self, period: str, user_id: int, notice: str | None = None):
        """(text, view) for a member's private ranking picker."""
        from .vote_ui import picker_view
        from .weekly import when
        db = self.bot.db
        films = await self.ballot_films()
        names = {e["tmdb_id"]: option_label(e) for e in films}
        mine = [t for t in await db.track_ranking(self.track, user_id) if t in names]
        last = await db.last_ranked_at(self.track, user_id)
        lines = [f"`{i}.` {names[t]}" for i, t in enumerate(mine, 1)]
        left = [e for e in films if e["tmdb_id"] not in mine]
        new = {e["tmdb_id"] for e in left if last and e["joined_at"] > last}
        if not films:
            status = "*No films on the ballot yet. **/nominate** one.*"
        elif not mine:
            status = "*Nothing ranked yet. Pick your 1st choice below.*"
        elif left:
            status = "\n".join(lines) + f"\n-# Saved. Add a {ordinal(len(mine) + 1)} choice if you like."
        else:
            status = "\n".join(lines) + "\n-# Saved. You've ranked every film."
        if new:
            status += (f"\n🆕 {len(new)} film{'s' * (len(new) != 1)} joined the ballot since you "
                       f"last ranked (marked 🆕 in the list).")
        text = (f"{self.emoji} **Your {self.label} ranking · {self.title(period)}**\n{status}\n"
                f"-# Every change is saved right away and carries over to the next "
                f"{self.unit}'s vote. This vote closes "
                f"{when(self.close_time(period), self.tz)}. Your 2nd choice only counts if your "
                f"1st is knocked out, and so on.")
        if notice:
            text = f"{notice}\n{text}"
        choices = [(e["tmdb_id"], names[e["tmdb_id"]], e["tmdb_id"] in new) for e in left]
        return text, picker_view(self.vote_id(period), choices, len(mine) + 1, bool(mine))

    def vote_id(self, period: str) -> str:
        """What the vote buttons carry: the period, prefixed with the group key."""
        return period if self.pool.grp == MAIN else f"{self.pool.grp}:{period}"

    async def change_ranking(self, period: str, user_id: int, op: str,
                             tmdb_id: int | None = None) -> str | None:
        """Apply add/undo/clear. Returns an error line, or None when it worked."""
        db = self.bot.db
        if await self.open_row(period) is None:
            return "❌ That vote is closed."
        async with self.lock:
            valid = [e["tmdb_id"] for e in await self.ballot_films()]
            mine = [t for t in await db.track_ranking(self.track, user_id) if t in valid]
            if op == "add":
                if tmdb_id not in valid:
                    return "❌ That film isn't on the ballot any more."
                if tmdb_id in mine:
                    return "That film is already in your ranking."
                mine.append(tmdb_id)
            elif op == "undo":
                mine = mine[:-1]
            elif op == "clear":
                mine = []
            await db.set_track_ranking(self.track, user_id, mine)
        self._later(self.refresh_ballot(period), "ballot refresh")
        return None

    def _later(self, coro, what: str) -> None:
        async def run():
            try:
                await coro
            except discord.HTTPException:
                log.exception("%s failed", what)
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def ballot_lines(self, films: list, movies: dict, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        lines = []
        for i, e in enumerate(films, 1):
            try:
                fresh = now - datetime.fromisoformat(e["joined_at"]) < NEW_FOR
            except (TypeError, ValueError):
                fresh = False
            link = self.link(e)
            lines.append(f"`{i:>2}.` {'🆕 ' if fresh else ''}{link} · "
                         f"nominated by <@{e['user_id']}>")
        return lines
