"""Phases 10, 12 and 19: nomination pools, live ballots and rolling rankings.

There is one pool per vote: 'main' for the main server's weekly vote, and one
per private group (server.yaml `groups:`) for its monthly vote. They are fully
separate: a film can sit in several pools. Since Phase 19 each member has **one**
nomination per pool, and several members can nominate the same film. A pool's
ballot is every eligible nominated film, listed with all its nominators. When a
film wins it leaves that pool (its nominators can nominate something new).
Nominations roll over until withdrawn, replaced or won.

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

from .groups import MAIN

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

ELIGIBLE = ("backlog", "watched_solo")

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
    """A masked link to a movie's #pre-watch post, labelled with its title (or `text`).

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

    pool_rows: pool nominations (user_id, tmdb_id, nominated_at, title,
    status), one per member, any order. current: the ballot now as
    [(tmdb_id, user_id)] in order. Every eligible nominated film belongs on the
    ballot; its user_id is its first nominator. Returns (add, remove,
    owner_changes): films joining (in the order they were first nominated),
    films leaving, and films that stay but whose first nominator changed. Films
    already on the ballot keep their place; new ones join while there's room
    (Discord's 25-option dropdown).
    """
    want: dict[int, dict] = {}
    for r in sorted(pool_rows, key=lambda r: (r["nominated_at"], r["user_id"])):
        if r["tmdb_id"] not in want and ok(r):
            want[r["tmdb_id"]] = r
    remove = [t for t, _ in current if t not in want]
    owner_changes = [(t, want[t]["user_id"]) for t, u in current
                     if t in want and want[t]["user_id"] != u]
    kept = {t for t, _ in current if t in want}
    new = [r for t, r in want.items() if t not in kept]
    room = max(0, size - len(kept))
    add = [(r["tmdb_id"], r["user_id"]) for r in new[:room]]
    return add, remove, owner_changes


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
    async def nominate(self, user_id: int, tmdb_id: int) -> str:
        """Set the member's one nomination; a new one replaces the old one."""
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
            old = await db.nomination(user_id, grp)
            if old is not None and old["tmdb_id"] == tmdb_id:
                return f"**{display(movie)}** is already your nomination{self._here()}."
            others = [u for u in (await db.nominators(grp)).get(tmdb_id, []) if u != user_id]
            await db.set_nomination(user_id, tmdb_id, grp)
            await self.sync()
            log.info("%s nominated TMDB %s in %s (was %s)", user_id, tmdb_id, grp,
                     old["tmdb_id"] if old else None)
            summary = await self._summary(user_id)
        self.refresh_soon()
        was = ""
        if old is not None:
            was = f", replacing **{display(old) if old['title'] else 'your old nomination'}**"
        also = ""
        if others:
            n = len(others)
            also = (f" {n} other member{'s' * (n != 1)} nominated it too, so the ballot shows "
                    f"all of you.")
        return f"✅ Nominated **{display(movie)}**{self._here()}{was}.{also}\n{summary}"

    def _here(self) -> str:
        return f" for {self.name}" if self.is_group else ""

    async def unnominate(self, user_id: int) -> str:
        db = self.bot.db
        async with self.lock:
            old = await db.nomination(user_id, self.grp)
            if old is None:
                return f"You have no nomination{self._here()}."
            await db.set_nomination(user_id, None, self.grp)
            await self.sync()
        self.refresh_soon()
        name = display(old) if old["title"] else f"TMDB {old['tmdb_id']}"
        return f"✅ Withdrew **{name}**{self._here()}. **/nominate** picks a new one."

    async def _summary(self, user_id: int) -> str:
        db = self.bot.db
        r = await db.nomination(user_id, self.grp)
        head = f"**Your {self.name} nomination**" if self.is_group else "**Your nomination**"
        if r is None:
            return f"-# You have no nomination{self._here()}. **🔎 Nominate a film** adds one."
        on = {e["tmdb_id"] for e in await db.ballot_entries(self.grp)}
        ok = await self._ok()
        name = display(r) if r["title"] is not None else f"*(post deleted, TMDB {r['tmdb_id']})*"
        what = "monthly" if self.is_group else "weekly"
        if r["tmdb_id"] in on:
            others = len((await db.nominators(self.grp)).get(r["tmdb_id"], [])) - 1
            note = f" · 🗳️ on the {what} ballot"
            if others:
                note += f" (with {others} other nominator{'s' * (others != 1)})"
        elif not ok(r):
            if r["title"] is None:
                why = "post deleted"
            elif self.is_group:
                watched = await db.group_watched(self.grp)
                why = "already picked" if r["tmdb_id"] in watched else "not eligible"
            else:
                why = {"up_next": "Up Next", "watched": "watched together"}.get(
                    r["status"], "post deleted")
            note = f" · not eligible now ({why}); pick another film"
        else:
            note = " · waiting: the ballot is full (25 films); it joins when a spot opens"
        return (f"{head}: {name}{note}\n-# One nomination each. Nominating another film "
                f"replaces it; if it wins, you can nominate something new.")

    async def my_view(self, user_id: int, notice: str | None = None, here=None,
                      results: list[tuple[str, str]] | None = None):
        """(text, view) for the private nomination view: /nominate and the board's 📋 Nominate.
        `here`: the movie row of the post it's opened in (offers "Nominate <it>");
        `results`: TMDB (label, value) matches to pick from after a search."""
        text = await self._summary(user_id)
        if notice:
            text = f"{notice}\n{text}"
        return text, await self.my_controls(user_id, here, results)

    async def my_controls(self, user_id: int, here=None,
                          results: list[tuple[str, str]] | None = None):
        from .vote_ui import nominations_view
        mine = await self.bot.db.nomination(user_id, self.grp)
        if here is not None and mine is not None and mine["tmdb_id"] == here["tmdb_id"]:
            here = None         # already their nomination
        return nominations_view(mine is not None, self.grp, here, results)

    async def withdraw_button(self, user_id: int) -> str | None:
        """🗑️ Withdraw on the private view."""
        async with self.lock:
            if await self.bot.db.nomination(user_id, self.grp) is None:
                return "You have no nomination now."
            await self.bot.db.set_nomination(user_id, None, self.grp)
            await self.sync()
        self.refresh_soon()
        return "✅ Withdrawn."


class RankedTrack:
    """Picker logic shared by the weekly and the group monthly votes. Subclasses set
    `pool` (their PoolService), `track`, `emoji`, `label` and `unit`, and implement
    open_row(period, now), close_time(period) and title(period), plus the board
    methods below."""
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

    async def refresh_ballot(self, period: str | None = None) -> None:
        """Any ballot change: re-render the board (it always shows the open vote)."""
        await self.refresh_board()

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
            status = "*No films on the ballot yet. **Nominate** on the board adds one.*"
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

    # the board: the one message in #watch-planning (Phase 19) -------------------
    # Subclasses also set `accent` and implement board_channel(), voting_period()
    # and board_header(period, voters) -> (heading, details, footer).
    accent = 0xE5A00D

    @property
    def board_meta(self) -> str:
        return f"board_message:{self.pool.grp}"

    def board_channel(self):
        raise NotImplementedError

    async def voting_period(self) -> str | None:
        raise NotImplementedError

    def board_header(self, period: str | None, voters: int) -> tuple[str, str, str]:
        raise NotImplementedError

    async def legacy_ballot_id(self) -> int | None:
        """The open vote's old embed ballot, which becomes the board (edited into V2)."""
        return None

    async def board_lines(self, films: list, now: datetime | None = None) -> list[tuple[str, str]]:
        """(full, compact) text per film: the full one has nominators on a subtext line."""
        from .names import join_names, member_names
        now = now or datetime.now(timezone.utc)
        noms = await self.bot.db.nominators(self.pool.grp)
        ids = [u for e in films for u in noms.get(e["tmdb_id"], [e["user_id"]])]
        names = await member_names(self.bot, ids)
        out = []
        for i, e in enumerate(films, 1):
            try:
                fresh = now - datetime.fromisoformat(e["joined_at"]) < NEW_FOR
            except (TypeError, ValueError):
                fresh = False
            by = join_names([names[u] for u in noms.get(e["tmdb_id"], [e["user_id"]])])
            new = "🆕 " if fresh else ""
            link = self.link(e)
            out.append((f"{new}**{link}**\n-# nominated by {by}",
                        f"`{i:>2}.` {new}{link} · {by}"))
        return out

    async def render_board(self, now: datetime | None = None) -> tuple["discord.ui.LayoutView", str]:
        """(view, cache key) for the board."""
        from .vote_ui import board_buttons
        period = await self.voting_period()
        films = await self.ballot_films() if period else []
        voters = await self.voter_count() if period else 0
        heading, details, footer = self.board_header(period, voters)
        rows = await self.board_lines(films, now)
        body = fit_lines(rows, BOARD_TEXT - len(heading) - len(details) - len(footer) - 40)
        if period:
            films_text = (f"### 🎞️ On the ballot ({len(films)})\n{body}" if films else
                          "### 🎞️ On the ballot\n*No films yet. Be the first: click **Nominate**.*")
        else:
            films_text = ""
        parts: list = [discord.ui.TextDisplay(f"{heading}\n{details}".strip())]
        if films_text:
            parts += [discord.ui.Separator(visible=True), discord.ui.TextDisplay(films_text)]
        parts += [discord.ui.Separator(visible=True), discord.ui.TextDisplay(footer),
                  board_buttons(self.vote_id(period) if period else None, self.pool.grp)]
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*parts, accent_color=self.accent))
        return view, f"{period}|{heading}|{details}|{films_text}|{footer}"

    async def refresh_board(self) -> None:
        """Post or edit the board in place; it's the channel's only bot message."""
        lock = self.__dict__.setdefault("_board_lock", asyncio.Lock())
        async with lock:
            await self._refresh_board()

    async def _refresh_board(self) -> None:
        ch = self.board_channel()          # a TextChannel, or None
        if ch is None:
            return
        db = self.bot.db
        view, key = await self.render_board()
        raw = await db.get_meta(self.board_meta)
        mid = int(raw) if raw else await self.legacy_ballot_id()
        if mid and self._rendered.get(mid) == key:
            return
        none = discord.AllowedMentions.none()
        if mid:
            try:
                await ch.get_partial_message(mid).edit(content="", embed=None, view=view,
                                                       allowed_mentions=none)
            except discord.NotFound:
                log.warning("Board %s in #watch-planning was deleted; posting a new one", mid)
                mid = None
        if not mid:
            msg = await ch.send(view=view, allowed_mentions=none)
            mid = msg.id
            log.info("Posted the %s board %s", self.pool.grp, mid)
        if raw != str(mid):
            await db.set_meta(self.board_meta, str(mid))
        self._rendered[mid] = key

    async def board_url(self) -> str:
        raw = await self.bot.db.get_meta(self.board_meta)
        ch = self.board_channel()
        if not raw or ch is None:
            return ""
        return f"https://discord.com/channels/{self.bot.config.guild_id}/{ch.id}/{raw}"


BOARD_TEXT = 3800          # Components V2 allow 4,000 displayable characters per message


def fit_lines(rows: list[tuple[str, str]], budget: int) -> str:
    """The film list within `budget` characters: full lines if they fit, else
    compact ones, else as many compact ones as fit plus "…and N more"."""
    full = "\n".join(f for f, _ in rows)
    if len(full) <= budget:
        return full
    compact = [c for _, c in rows]
    text = "\n".join(compact)
    if len(text) <= budget:
        return text
    out: list[str] = []
    used = 0
    for i, line in enumerate(compact):
        more = f"\n-# …and {len(compact) - i} more: click **Vote** to see them all."
        if used + len(line) + 1 + len(more) > budget:
            return "\n".join(out) + more
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)
