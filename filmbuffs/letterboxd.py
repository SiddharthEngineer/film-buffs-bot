"""Letterboxd: linked accounts, RSS polling, and posting new diary entries.

Each new entry produces three messages, in this order:
  1. #post-watch-discussion post: an embed with rating, ♥, rewatch, watched date, review.
  2. #movies post: a neutral one-liner ("X logged this") linking to (1). No rating.
  3. #letterboxd-feed: one line linking the #movies post. No rating, no review.
Every entry is stored before anything is posted, and each message ID is saved as
soon as it is sent, so a crash or Discord error resumes without duplicates.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING

import aiohttp
import discord
import feedparser

from .botlogs import StreakAlert
from .movies import truncate
from .tmdb import TMDBError, year_of

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

FEED_URL = "https://letterboxd.com/{username}/rss/"
PROFILE_URL = "https://letterboxd.com/{username}/"
EMBED_COLOR = 0x40BCF4
MAX_ATTEMPTS = 5
# Consecutive failed polls (one per round, ~15 min) before #bot-logs hears about it.
ALERT_AFTER = 4
REVIEW_LIMIT = 3800           # embed description is 4096; leave room for the header
SPOILER_WARNING = "This review may contain spoilers."
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_URL_RE = re.compile(r"letterboxd\.com/([A-Za-z0-9_]+)", re.IGNORECASE)


# ------------------------------------------------------------------ pure logic

def normalize_username(text: str) -> str | None:
    """`name`, `@name` or a profile/feed URL → lowercase username, else None."""
    text = (text or "").strip()
    m = _URL_RE.search(text)
    if m:
        text = m.group(1)
    text = text.lstrip("@").strip("/")
    return text.lower() if _USERNAME_RE.match(text) else None


@dataclass
class FeedEntry:
    guid: str
    kind: str                    # watch | review
    tmdb_id: int | None
    film_title: str
    film_year: int | None
    watched_date: str | None
    rating: float | None
    liked: bool
    rewatch: bool
    review_text: str | None
    url: str | None
    published_at: str | None

    @property
    def display(self) -> str:
        return f"{self.film_title} ({self.film_year})" if self.film_year else self.film_title

    def as_row(self, user_id: int, username: str, source: str, state: str) -> dict:
        return dict(guid=self.guid, discord_user_id=user_id, username=username,
                    kind=self.kind, tmdb_id=self.tmdb_id, film_title=self.film_title,
                    film_year=self.film_year, watched_date=self.watched_date,
                    rating=self.rating, liked=int(self.liked), rewatch=int(self.rewatch),
                    review_text=self.review_text, letterboxd_url=self.url,
                    published_at=self.published_at, source=source, state=state)


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_feed(body: bytes | str) -> list[FeedEntry]:
    """Diary entries (watches and reviews) in feed order. Lists are skipped."""
    out = []
    for e in feedparser.parse(body).entries:
        guid = e.get("id") or ""
        if guid.startswith("letterboxd-watch-"):
            kind = "watch"
        elif guid.startswith("letterboxd-review-"):
            kind = "review"
        else:
            continue
        published = None
        if e.get("published_parsed"):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).isoformat(
                timespec="seconds")
        review = review_to_markdown(e.get("summary", "")) if kind == "review" else None
        out.append(FeedEntry(
            guid=guid, kind=kind, tmdb_id=_int(e.get("tmdb_movieid")),
            film_title=(e.get("letterboxd_filmtitle") or e.get("title") or "?").strip(),
            film_year=_int(e.get("letterboxd_filmyear")),
            watched_date=(e.get("letterboxd_watcheddate") or "").strip() or None,
            rating=_float(e.get("letterboxd_memberrating")),
            liked=(e.get("letterboxd_memberlike") or "").strip().lower() == "yes",
            rewatch=(e.get("letterboxd_rewatch") or "").strip().lower() == "yes",
            review_text=review or None, url=e.get("link"), published_at=published))
    return out


class _ReviewHTML(HTMLParser):
    """Letterboxd review HTML → Discord markdown (text is markdown-escaped)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras: list[str] = []
        self.cur: list[str] = []
        self.href: str | None = None
        self.link_text: list[str] = []

    def _flush(self):
        text = re.sub(r"[ \t]+", " ", "".join(self.cur)).strip()
        text = "\n".join(line.strip() for line in text.split("\n"))
        if text:
            self.paras.append(text)
        self.cur = []

    def _emit(self, s: str):
        (self.link_text if self.href is not None else self.cur).append(s)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "blockquote", "ul", "ol", "div"):
            self._flush()
        elif tag == "br":
            self._emit("\n")
        elif tag == "li":
            self._flush()
            self._emit("• ")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag == "a":
            self.href = dict(attrs).get("href") or ""
            self.link_text = []

    def handle_endtag(self, tag):
        if tag in ("p", "blockquote", "li", "ul", "ol", "div"):
            self._flush()
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag == "a" and self.href is not None:
            text, href = "".join(self.link_text).strip(), self.href
            self.href = None
            if href.startswith(("http://", "https://")) and text:
                self.cur.append(f"[{text}]({href})")
            else:
                self.cur.append(text)

    def handle_data(self, data):
        self._emit(discord.utils.escape_markdown(data.replace("\n", " ")))


def review_to_markdown(html: str) -> str:
    p = _ReviewHTML()
    p.feed(html or "")
    p.close()
    p._flush()
    paras = [x for x in p.paras
             if x.strip("*_ ").replace("\\", "") != SPOILER_WARNING]
    return "\n\n".join(paras).strip()


def stars(rating: float | None) -> str:
    if rating is None:
        return ""
    halves = max(0, min(10, round(rating * 2)))
    return "★" * (halves // 2) + ("½" if halves % 2 else "")


def nice_date(iso: str | None) -> str | None:
    try:
        d = date.fromisoformat(iso) if iso else None
    except ValueError:
        return iso
    return f"{d:%b} {d.day}, {d.year}" if d else None


def pick_tmdb_match(results: list[dict], title: str, year: int | None) -> int | None:
    """Fallback when a feed item has no TMDB ID: only an unambiguous exact match."""
    t = title.casefold().strip()
    hits = [r["id"] for r in results
            if (r.get("title") or "").casefold().strip() == t
            and (year is None or year_of(r.get("release_date")) == year)]
    return hits[0] if len(hits) == 1 else None


def spoiler_embed(row, member_name: str, avatar_url: str | None,
                  imported: bool = False) -> discord.Embed:
    """The full entry, for the movie's spoiler post."""
    display = (f"{row['film_title']} ({row['film_year']})" if row["film_year"]
               else row["film_title"])
    bits = []
    if row["rating"] is not None:
        bits.append(stars(row["rating"]))
    if row["liked"]:
        bits.append("♥")
    if row["rewatch"]:
        bits.append("🔁 Rewatch")
    header = "  ·  ".join(bits) or "*No rating*"
    desc = f"**{header}**"
    review = row["review_text"]
    if review:
        if len(review) > REVIEW_LIMIT:
            review = truncate(review, REVIEW_LIMIT)
            if row["letterboxd_url"]:
                review += f"\n[Read the full review on Letterboxd]({row['letterboxd_url']})"
        desc += "\n\n" + review
    e = discord.Embed(title=truncate(display, 256), url=row["letterboxd_url"],
                      description=desc, color=EMBED_COLOR)
    e.set_author(name=f"{member_name} on Letterboxd",
                 url=PROFILE_URL.format(username=row["username"]), icon_url=avatar_url)
    watched = nice_date(row["watched_date"])
    footer = f"Watched {watched}" if watched else "Logged on Letterboxd"
    if imported:
        footer += " · imported from Letterboxd history"
    e.set_footer(text=footer)
    return e


def movies_note(user_id: int, spoiler_url: str, has_rating: bool, has_review: bool) -> str:
    """Spoiler-free: says *that* someone logged the film, never how they rated it."""
    if has_rating and has_review:
        what = "Their rating and review are"
    elif has_review:
        what = "Their review is"
    elif has_rating:
        what = "Their rating is"
    else:
        what = "Their diary entry is"
    return (f"📓 <@{user_id}> logged this on Letterboxd. "
            f"{what} in the post-watch discussion → {spoiler_url}")


def feed_line(user_id: int, display: str, movies_url: str) -> str:
    return f"📓 <@{user_id}> logged **{discord.utils.escape_markdown(display)}** → {movies_url}"


# ------------------------------------------------------------------ fetching

class LetterboxdError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class UserNotFound(LetterboxdError):
    pass


class AlreadyLinked(Exception):
    """That Letterboxd account is already linked to another member."""


@dataclass
class FeedResult:
    entries: list[FeedEntry] | None      # None: not modified (304)
    etag: str | None = None
    last_modified: str | None = None


async def fetch_feed(session: aiohttp.ClientSession, username: str, *,
                     etag: str | None = None, last_modified: str | None = None,
                     timeout: float = 20) -> FeedResult:
    headers = {"Accept": "application/rss+xml, application/xml;q=0.9"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    url = FEED_URL.format(username=username)
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 304:
                return FeedResult(None, etag, last_modified)
            if r.status == 404:
                raise UserNotFound(f"no Letterboxd user {username!r}", 404)
            if r.status != 200:
                raise LetterboxdError(f"{url} returned HTTP {r.status}", r.status)
            body = await r.read()
            return FeedResult(parse_feed(body), r.headers.get("ETag"),
                              r.headers.get("Last-Modified"))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise LetterboxdError(f"{url} failed: {e!r}") from e


# --------------------------------------------------------------------- service

class LetterboxdService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.streaks = StreakAlert(ALERT_AFTER)

    @property
    def feed_channel(self) -> discord.TextChannel | None:
        cid = self.bot.config.channels.get("letterboxd_feed")
        return self.bot.get_channel(cid) if cid else None  # type: ignore[return-value]

    async def _fetch(self, username: str, link=None) -> FeedResult:
        return await fetch_feed(self.bot.session, username,
                                etag=link["etag"] if link else None,
                                last_modified=link["last_modified"] if link else None)

    # linking -----------------------------------------------------------------
    async def link(self, user_id: int, username: str) -> int:
        """Link a member's account. Everything already in the feed is stored as
        `baseline` (never posted). Returns how many entries that was."""
        async with self._locks[user_id]:
            other = await self.bot.db.get_link_by_username(username)
            if other is not None and other["discord_user_id"] != user_id:
                raise AlreadyLinked(username)
            result = await self._fetch(username)
            await self.bot.db.set_link(user_id, username)
            n = 0
            for entry in result.entries or []:
                n += await self.bot.db.insert_entry(
                    entry.as_row(user_id, username, "rss", "baseline"))
            await self.bot.db.record_poll(user_id, ok=True, etag=result.etag,
                                          last_modified=result.last_modified)
            log.info("Linked %s to Letterboxd %s (%d existing entries marked seen)",
                     user_id, username, n)
            return n

    async def unlink(self, user_id: int) -> bool:
        async with self._locks[user_id]:
            return await self.bot.db.delete_link(user_id)

    # polling -----------------------------------------------------------------
    async def poll_all(self) -> None:
        """One polling round. Persistent failures (a feed that keeps failing, or
        Letterboxd refusing every round) are reported once to #bot-logs, plus a
        notice when they recover; single failures only go to the journal."""
        links = await self.bot.db.all_links()
        for i, link in enumerate(links):
            uid = link["discord_user_id"]
            if i:
                await asyncio.sleep(random.uniform(*self.bot.config.lb_jitter))
            try:
                await self.poll_user(uid)
            except LetterboxdError as e:
                if e.status in (403, 429) or (e.status or 0) >= 500:
                    log.warning("Letterboxd said HTTP %s; skipping the rest of this round",
                                e.status)
                    if self.streaks.fail("round"):
                        log.error("Letterboxd has refused %d polling rounds in a row "
                                  "(last: %s). Nothing is being posted.", ALERT_AFTER, e)
                    return
                if self.streaks.fail(uid):
                    log.error("Letterboxd feed %s (member %s) has failed %d polls in a row: %s",
                              link["username"], uid, ALERT_AFTER, e)
                continue
            except Exception:
                log.exception("Polling Letterboxd for %s failed", uid)
                continue
            if self.streaks.ok(uid):
                log.warning("Letterboxd feed %s (member %s) works again",
                            link["username"], uid, extra={"botlogs": True})
        if links and self.streaks.ok("round"):
            log.warning("Letterboxd polling works again", extra={"botlogs": True})

    async def poll_user(self, user_id: int) -> int:
        """Fetch one member's feed and post anything new. Returns entries posted."""
        async with self._locks[user_id]:
            link = await self.bot.db.get_link(user_id)
            if link is None:
                return 0
            try:
                result = await self._fetch(link["username"], link)
            except LetterboxdError as e:
                await self.bot.db.record_poll(user_id, ok=False, error=str(e))
                log.warning("Letterboxd feed for %s (%s): %s", link["username"], user_id, e)
                raise
            await self.bot.db.record_poll(user_id, ok=True, etag=result.etag,
                                          last_modified=result.last_modified)
            new = 0
            # Feeds are newest first; store oldest first so they post in order.
            for entry in reversed(result.entries or []):
                new += await self.bot.db.insert_entry(
                    entry.as_row(user_id, link["username"], "rss", "pending"))
            if new:
                log.info("%d new Letterboxd entr%s for %s", new, "y" if new == 1 else "ies",
                         link["username"])
            posted = 0
            pending = await self.bot.db.pending_entries(user_id)
            member = await self._member(user_id) if pending else None
            for row in pending:
                if member is None:
                    await self.bot.db.update_entry(row["guid"], state="skipped",
                                                   error="member is not in the server")
                    continue
                posted += await self.publish(row, member)
                await asyncio.sleep(1)   # gentle on rate limits when several arrive at once
            return posted

    async def _member(self, user_id: int) -> discord.Member | None:
        guild = self.bot.get_guild(self.bot.config.guild_id)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    # posting -----------------------------------------------------------------
    async def resolve_tmdb(self, row) -> int | None:
        if row["tmdb_id"]:
            return row["tmdb_id"]
        results = await self.bot.tmdb.search(row["film_title"], row["film_year"])
        return pick_tmdb_match(results, row["film_title"], row["film_year"])

    async def publish(self, row, member: discord.Member) -> bool:
        """Post one pending entry. Returns True when it is fully posted.
        Failures leave it pending (retried next poll) until MAX_ATTEMPTS."""
        guid = row["guid"]
        attempts = row["attempts"] + 1
        await self.bot.db.update_entry(guid, attempts=attempts)
        try:
            await self._publish(row, member)
        except (discord.HTTPException, TMDBError, LookupError) as e:
            state = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            await self.bot.db.update_entry(guid, state=state, error=repr(e)[:500])
            if state == "failed":     # only the final attempt goes to #bot-logs
                log.error("Gave up posting Letterboxd entry %s (%s %s) after %d attempts",
                          guid, row["film_title"], row["film_year"], attempts, exc_info=True)
            else:
                log.warning("Posting Letterboxd entry %s failed (attempt %d, will retry)",
                            guid, attempts, exc_info=True)
            return False
        return (await self.bot.db.get_entry(guid))["state"] == "posted"

    async def _publish(self, row, member: discord.Member) -> None:
        db, guid = self.bot.db, row["guid"]
        tmdb_id = await self.resolve_tmdb(row)
        movie = await self.bot.tmdb.movie(tmdb_id) if tmdb_id else None
        if movie is None:
            await db.update_entry(guid, state="unmatched",
                                  error="no TMDB match for the feed item")
            log.warning("Letterboxd entry %s (%s %s) has no TMDB match; not posted",
                        guid, row["film_title"], row["film_year"])
            return
        if tmdb_id != row["tmdb_id"]:
            await db.update_entry(guid, tmdb_id=tmdb_id)

        # A solo log never changes an existing post's tags (owner, 2026-11-11);
        # only posts created from a log start as Watched (solo).
        movies_post, _ = await self.bot.movies.ensure_post(
            movie, added_by=member.id, pitch=None, status="watched_solo", source="letterboxd")
        row = await db.get_entry(guid)
        quiet = discord.AllowedMentions.none()

        spoiler_url = None
        if row["spoiler_message_id"]:
            mrow = await db.get_movie(tmdb_id)
            if mrow and mrow["spoiler_thread_id"]:
                spoiler_url = (f"https://discord.com/channels/{self.bot.config.guild_id}/"
                               f"{mrow['spoiler_thread_id']}/{row['spoiler_message_id']}")
        if spoiler_url is None:
            spoiler_post, _ = await self.bot.spoilers.open(tmdb_id)
            msg = await spoiler_post.send(
                embed=spoiler_embed(row, member.display_name, member.display_avatar.url),
                allowed_mentions=quiet)
            await db.update_entry(guid, spoiler_message_id=msg.id)
            spoiler_url = msg.jump_url

        if not row["movies_message_id"]:
            msg = await movies_post.send(
                movies_note(member.id, spoiler_url, row["rating"] is not None,
                            bool(row["review_text"])),
                allowed_mentions=quiet, suppress_embeds=True)
            await db.update_entry(guid, movies_message_id=msg.id)

        if not row["feed_message_id"]:
            feed = self.feed_channel
            if feed is None:
                log.error("#letterboxd-feed not found; skipping the feed line for %s", guid)
            else:
                msg = await feed.send(feed_line(member.id, movie.display, movies_post.jump_url),
                                      allowed_mentions=quiet, suppress_embeds=True)
                await db.update_entry(guid, feed_message_id=msg.id)

        await db.update_entry(guid, state="posted", error=None)
        log.info("Posted Letterboxd entry %s (%s by %s)", guid, movie.display, member.id)
