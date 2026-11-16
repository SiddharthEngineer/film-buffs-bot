"""Post-watch discussions: one companion post per movie in #post-watch.

#movies posts stay spoiler-free. A movie's post-watch post is
created lazily (the info card button, /spoiler, Mark as spoiler, or a Letterboxd
entry) and never more than once. "Mark as spoiler" moves a message out of a
#movies post into the post-watch post: it is re-sent there via webhook as its
author, then the original is deleted. Nothing is ever reposted in the #movies
post, so the order there never changes.

Internal names (channel key movie_spoilers, "spoiler post", spoilers:<id>
custom_ids) predate the rename and are kept so existing IDs keep working.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

import discord

from .movies import EMBED_COLOR, truncate
from .tags import post_watch_tag_keys
from .tmdb import TMDBError

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

WEBHOOK_NAME = "Film Buffs spoiler moves"
MESSAGE_LIMIT = 2000
# Discord: 20 MiB per file by default (more with boosts), 25 MiB per request, 10 files per message.
DEFAULT_FILE_LIMIT = 20 * 1024 * 1024
REQUEST_LIMIT = 25 * 1024 * 1024
MAX_FILES = 10


# ------------------------------------------------------------------ pure logic

def spoiler_post_name(display: str) -> str:
    """`Title (Year)`, like the #movies post (Phase 16 dropped "— Post-watch")."""
    return truncate(display, 100)


# Bump when the title, starter or tags change; existing post-watch posts are
# rewritten once at startup.
#   2: links back to "#movies" (was "pre-watch discussion")
#   3: Phase 16: title `Title (Year)`, embed-only starter with poster, tags
STARTER_VERSION = "3"

# Bump to recreate post-watch posts that carry a "changed the post title"
# notice (which can't be deleted). 1: the Phase 16 rollout's renames.
REBUILD_VERSION = "1"
REBUILD_KEY = "post_watch_rebuild_version"
REBUILD_OLD_KEY = "post_watch_rebuild_current"   # JSON {tmdb_id, old, pinned} mid-rebuild
# Messages people (or the bot's commands) write; everything else is a system notice.
USER_TYPES = {discord.MessageType.default.value, discord.MessageType.reply.value,
              discord.MessageType.chat_input_command.value,
              discord.MessageType.context_menu_command.value}


def foreign_messages(history, starter_id: int | None, embed_ids: set[int]) -> list:
    """Messages in a post-watch post that a rebuild couldn't carry over: anything
    people wrote (or the spoiler-move webhook sent), i.e. every non-system
    message except the starter and the stored Letterboxd embeds."""
    return [m for m in history if m.type.value in USER_TYPES
            and m.id != starter_id and m.id not in embed_ids]


def post_activity(row) -> int:
    """Sort key: newest message the bot knows of in the post (snowflakes grow
    with time), so rebuilding in this order keeps the "latest activity" order."""
    return max(row["last_embed"] or 0, row["spoiler_thread_id"])


def starter_description(display: str, movies_url: str) -> str:
    return (f"⚠️ **Post-watch discussion for {display}. Spoilers allowed.** "
            f"Everything here may spoil the film, so only read on once you've seen it.\n\n"
            f"← [Back to the spoiler-free post in #movies]({movies_url})")


def starter_embed(display: str, movies_url: str, poster_url: str | None) -> discord.Embed:
    """The starter is this embed and no plain text: a forum's post list shows a
    starter's text but not its embeds, so the list shows just title + poster
    (like #movies)."""
    e = discord.Embed(description=starter_description(display, movies_url), color=EMBED_COLOR)
    if poster_url:
        e.set_thumbnail(url=poster_url)
    return e


def starter_is_current(message, embed: discord.Embed) -> bool:
    """Does an existing starter already match `embed` (and carry no text)?"""
    if message.content or not message.embeds:
        return False
    have = message.embeds[0]
    return (have.description == embed.description
            and (have.thumbnail.url or None) == (embed.thumbnail.url or None))


def split_text(text: str, first_room: int, room: int = MESSAGE_LIMIT) -> list[str]:
    """Split text into chunks: the first fits `first_room`, the rest `room`.
    Prefers breaking at newlines, then spaces."""
    text = text.strip()
    chunks: list[str] = []
    limit = first_room
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
        limit = room
    return chunks


def move_messages(content: str, header: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """The text messages for a moved message: header on the first one."""
    body = split_text(content or "", limit - len(header) - 1, limit)
    if not body:
        return [header]
    return [f"{header}\n{body[0]}"] + body[1:]


def batch_files(sizes: list[int], max_total: int = REQUEST_LIMIT,
                max_count: int = MAX_FILES) -> list[list[int]]:
    """Group attachment indexes into requests within Discord's per-request limits."""
    batches: list[list[int]] = []
    cur: list[int] = []
    total = 0
    for i, size in enumerate(sizes):
        if cur and (total + size > max_total or len(cur) >= max_count):
            batches.append(cur)
            cur, total = [], 0
        cur.append(i)
        total += size
    if cur:
        batches.append(cur)
    return batches


def safe_webhook_username(name: str) -> str:
    """Webhook names can't contain 'discord' or 'clyde' and are max 80 chars."""
    for bad in ("discord", "clyde"):
        name = re.sub(bad, lambda m: m.group(0)[0] + "​" + m.group(0)[1:], name,
                      flags=re.IGNORECASE)
    return name[:80] or "Member"


def display_of(row) -> str:
    return f"{row['title']} ({row['year']})" if row["year"] else row["title"]


class AlreadyMoved(Exception):
    """The message was already moved (or deleted) by the time we got to it."""


# --------------------------------------------------------------------- service

class SpoilerService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._open_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._move_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._webhook: discord.Webhook | None = None
        self._rebuilding = False

    @property
    def forum(self) -> discord.ForumChannel:
        ch = self.bot.get_channel(self.bot.config.channels["movie_spoilers"])
        if not isinstance(ch, discord.ForumChannel):
            raise RuntimeError("#post-watch forum not found")
        return ch

    # lookups -----------------------------------------------------------------
    async def movie_for_movies_post(self, channel):
        if not isinstance(channel, discord.Thread):
            return None
        if channel.parent_id != self.bot.config.channels["movies"]:
            return None
        return await self.bot.db.get_movie_by_thread(channel.id)

    async def movie_for_spoiler_post(self, channel):
        if not isinstance(channel, discord.Thread):
            return None
        if channel.parent_id != self.bot.config.channels["movie_spoilers"]:
            return None
        return await self.bot.db.get_movie_by_spoiler_thread(channel.id)

    async def find_spoiler_post(self, row) -> discord.Thread | None:
        """The movie's spoiler post, or None. Forgets posts that were deleted by hand."""
        tid = row["spoiler_thread_id"]
        if not tid:
            return None
        thread = self.bot.get_channel(tid)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(tid)
            except discord.NotFound:
                log.warning("Spoiler post %s for TMDB %s was deleted; forgetting it",
                            tid, row["tmdb_id"])
                await self.bot.db.set_spoiler_post(row["tmdb_id"], None, None)
                return None
        return thread  # type: ignore[return-value]

    # lazy creation -----------------------------------------------------------
    async def open(self, tmdb_id: int) -> tuple[discord.Thread, bool]:
        """Return (spoiler post, created). Creates it at most once per movie.

        Raises LookupError if the movie (or its #movies post) no longer exists.
        """
        async with self._open_locks[tmdb_id]:
            movies_post = await self.bot.movies.find_post(tmdb_id)
            row = await self.bot.db.get_movie(tmdb_id)
            if movies_post is None or row is None:
                raise LookupError(tmdb_id)
            existing = await self.find_spoiler_post(row)
            if existing is not None:
                return existing, False

            display = display_of(row)
            thread, starter = await self._create_post(
                display, movies_post.jump_url, await self.poster_url(row),
                await self.applied_tags(row))
            await self.bot.db.set_spoiler_post(tmdb_id, thread.id, starter.id)
            log.info("Created spoiler post %s for TMDB %s (%s)", thread.id, tmdb_id, display)
            # Cosmetic follow-up: the post exists and is recorded even if this fails.
            for step in (self.bot.movies.update_card,):
                try:
                    await step(tmdb_id)
                except discord.HTTPException:
                    log.exception("%s failed after creating spoiler post for TMDB %s",
                                  step.__name__, tmdb_id)
            return thread, True

    # poster and tags ----------------------------------------------------------
    async def poster_url(self, row) -> str | None:
        """The movie's poster. Rows from before Phase 16 have none stored; it is
        looked up on TMDB once and saved ('' when TMDB has none)."""
        if row["poster_url"] is not None:
            return row["poster_url"] or None
        if getattr(self.bot, "tmdb", None) is None:
            return None
        try:
            movie = await self.bot.tmdb.movie(row["tmdb_id"])
        except TMDBError:
            log.warning("TMDB lookup for the poster of %s failed; trying again later",
                        row["tmdb_id"], exc_info=True)
            return None
        url = (movie.poster_url if movie else None) or ""
        await self.bot.db.set_poster(row["tmdb_id"], url)
        return url or None

    async def tag_keys(self, row) -> list[str]:
        return post_watch_tag_keys(row["year"], json.loads(row["genre_tags"] or "[]"),
                                   await self.bot.db.has_posted_review(row["tmdb_id"]),
                                   self.bot.config.max_genre_tags)

    async def applied_tags(self, row) -> list[discord.ForumTag]:
        """The post-watch tags for a movie, recomputed from the DB. Tags that
        aren't set up yet (setup_server.py --apply) are left out."""
        defs = getattr(self.bot.config, "post_watch_tags", {})
        forum = self.forum
        tags = []
        for k in await self.tag_keys(row):
            tag = forum.get_tag(defs[k].id) if k in defs else None
            if tag is not None:
                tags.append(tag)
            else:
                log.warning("#post-watch tag %s is missing; run setup_server.py --apply", k)
        return tags

    async def retag(self, tmdb_id: int) -> None:
        """Re-apply the post-watch post's tags from the DB (after a Letterboxd
        review is posted or deleted). Never raises for Discord errors."""
        try:
            row = await self.bot.db.get_movie(tmdb_id)
            thread = await self.find_spoiler_post(row) if row else None
            if thread is None:
                return
            tags = await self.applied_tags(row)
            if {t.id for t in tags} == {t.id for t in thread.applied_tags}:
                return
            # Archived posts must be un-archived in the same edit, or Discord refuses it.
            extra = {"archived": False} if thread.archived else {}
            await thread.edit(applied_tags=tags, reason="Post-watch tags", **extra)
        except (discord.HTTPException, RuntimeError):
            log.exception("Could not retag the post-watch post for TMDB %s", tmdb_id)

    async def refresh_post(self, row) -> None:
        """Bring an existing post-watch post's title, tags and starter up to date."""
        thread = await self.find_spoiler_post(row)
        movies_post = await self.bot.movies.find_post(row["tmdb_id"])
        if thread is None or movies_post is None:
            return
        row = await self.bot.db.get_movie(row["tmdb_id"]) or row
        display = display_of(row)
        edits: dict = {}
        name = spoiler_post_name(display)
        if thread.name != name:
            edits["name"] = name
        tags = await self.applied_tags(row)
        if {t.id for t in tags} != {t.id for t in thread.applied_tags}:
            edits["applied_tags"] = tags
        if edits:
            # One edit, un-archiving in the same call (Discord refuses edits to
            # archived threads otherwise). Pinning is untouched.
            await thread.edit(archived=False, reason="Post-watch title and tags", **edits)
        if row["spoiler_starter_message_id"]:
            embed = starter_embed(display, movies_post.jump_url, await self.poster_url(row))
            try:
                starter = await thread.fetch_message(row["spoiler_starter_message_id"])
            except discord.NotFound:
                return
            if not starter_is_current(starter, embed):
                if not edits and getattr(thread, "archived", False):
                    await thread.edit(archived=False, reason="Updating the starter message")
                # content=None clears the old plain-text starter.
                await starter.edit(content=None, embed=embed,
                                   allowed_mentions=discord.AllowedMentions.none())

    async def refresh_starters(self) -> None:
        """Once per STARTER_VERSION: rewrite existing post-watch titles, starters and
        tags (backfilling posters from TMDB). ~1 s per post. Only marked done once
        every #post-watch tag is set up, so running before setup_server.py
        --apply just repeats next startup."""
        db = self.bot.db
        if await db.get_meta("post_watch_starter_version") == STARTER_VERSION:
            return
        cur = await db.conn.execute(
            "SELECT * FROM movies WHERE spoiler_thread_id IS NOT NULL ORDER BY added_at")
        rows = list(await cur.fetchall())
        log.info("Refreshing %d post-watch starter message(s)", len(rows))
        for row in rows:
            try:
                await self.refresh_post(row)
            except discord.HTTPException:
                log.exception("Could not refresh post-watch post for TMDB %s", row["tmdb_id"])
            await asyncio.sleep(1)
        if getattr(self.bot.config, "post_watch_tags_ready", True):
            await db.set_meta("post_watch_starter_version", STARTER_VERSION)
        else:
            log.warning("Some #post-watch tags aren't set up (run setup_server.py --apply); "
                        "the post-watch refresh runs again at the next start")

    # one-time rebuild (title-change notices) ---------------------------------
    # Renaming a forum post leaves a "changed the post title" system message in
    # it that nobody can delete (DISCORD_NOTES). The Phase 16 rollout left one
    # in every post, so each such post is recreated once with its final title:
    # new post, the review embeds re-sent in order, DB and links updated, old
    # post deleted. Posts with messages the bot doesn't own are left alone.

    async def rebuild_posts(self) -> None:
        """Once per REBUILD_VERSION, after the Phase 16 refresh is done."""
        db = self.bot.db
        if self._rebuilding or await db.get_meta(REBUILD_KEY) == REBUILD_VERSION:
            return
        if await db.get_meta("post_watch_starter_version") != STARTER_VERSION:
            return                      # tags not set up yet; titles may still change
        self._rebuilding = True
        try:
            await self._resume_rebuild()
            cur = await db.conn.execute(
                "SELECT m.*, (SELECT MAX(e.spoiler_message_id) FROM letterboxd_entries e "
                "WHERE e.tmdb_id = m.tmdb_id) AS last_embed "
                "FROM movies m WHERE m.spoiler_thread_id IS NOT NULL")
            rows = sorted(await cur.fetchall(), key=post_activity)
            log.info("Checking %d post-watch post(s) for title-change notices", len(rows))
            done = kept = 0
            for row in rows:
                try:
                    result = await self.rebuild_post(row)
                except (discord.HTTPException, RuntimeError):
                    log.exception("Could not rebuild the post-watch post for TMDB %s",
                                  row["tmdb_id"])
                    kept += 1
                    continue
                if result == "rebuilt":
                    done += 1
                    await asyncio.sleep(1)
                elif result == "kept":
                    kept += 1
            log.info("Post-watch rebuild: %d post(s) recreated, %d left as they were",
                     done, kept)
            if not kept:
                await db.set_meta(REBUILD_KEY, REBUILD_VERSION)
        finally:
            self._rebuilding = False

    async def rebuild_post(self, row) -> str:
        """Recreate one post if it has a title-change notice. Returns "rebuilt",
        "clean" (nothing to do) or "kept" (skipped; logged)."""
        db, tmdb_id = self.bot.db, row["tmdb_id"]
        async with self._open_locks[tmdb_id]:
            old = await self.find_spoiler_post(row)
            movies_post = await self.bot.movies.find_post(tmdb_id)
            if old is None or movies_post is None:
                return "clean"
            history = [m async for m in old.history(limit=None, oldest_first=True)]
            if not any(m.type == discord.MessageType.channel_name_change for m in history):
                return "clean"
            entries = await self._embedded_entries(tmdb_id)
            others = foreign_messages(history, row["spoiler_starter_message_id"],
                                      {e["spoiler_message_id"] for e in entries})
            if others:
                log.warning("Post-watch post %s (TMDB %s) has %d message(s) the bot didn't "
                            "post; not rebuilding it", old.id, tmdb_id, len(others))
                return "kept"
            row = await db.get_movie(tmdb_id) or row
            await db.set_meta(REBUILD_OLD_KEY, json.dumps(
                {"tmdb_id": tmdb_id, "old": old.id, "pinned": bool(old.flags.pinned)}))
            display = display_of(row)
            new, starter = await self._create_post(
                display, movies_post.jump_url, await self.poster_url(row),
                await self.applied_tags(row))
            await db.set_spoiler_post(tmdb_id, new.id, starter.id)
            await self._finish_rebuild(tmdb_id, old, new, bool(old.flags.pinned), history)
        log.info("Rebuilt post-watch post for TMDB %s (%s): %s → %s",
                 tmdb_id, display, old.id, new.id)
        return "rebuilt"

    async def _embedded_entries(self, tmdb_id: int) -> list:
        cur = await self.bot.db.conn.execute(
            "SELECT * FROM letterboxd_entries WHERE tmdb_id=? AND spoiler_message_id IS NOT NULL "
            "ORDER BY spoiler_message_id", (tmdb_id,))
        return list(await cur.fetchall())

    async def _finish_rebuild(self, tmdb_id: int, old, new, pinned: bool,
                              history: list | None) -> None:
        """Move the entries whose embed is still in the old post into the new
        one, then delete the old post, pin and refresh the card. Safe to repeat
        after a crash (entries already moved are newer than the new post)."""
        db = self.bot.db
        if old is not None and history is None:
            history = [m async for m in old.history(limit=None, oldest_first=True)]
        by_id = {m.id: m for m in history or ()}
        quiet = discord.AllowedMentions.none()
        for e in await self._embedded_entries(tmdb_id):
            mid = e["spoiler_message_id"]
            if mid >= new.id:
                continue                                # already in the new post
            src = by_id.get(mid)
            if src is None or not src.embeds:
                await db.update_entry(e["guid"], spoiler_message_id=None)
                continue
            msg = await new.send(embeds=src.embeds, allowed_mentions=quiet)
            await db.update_entry(e["guid"], spoiler_message_id=msg.id)
            await self._relink_movies_note(e, new.id, msg.id)
        if old is not None:
            # A reply that arrived mid-rebuild would be lost: keep the old post then.
            latest = [m async for m in old.history(limit=None, oldest_first=True)]
            moved = {m.id for m in history or ()}
            late = [m for m in latest if m.id not in moved and m.type.value in USER_TYPES]
            if late:
                log.error("Post-watch post %s got %d new message(s) during the rebuild; "
                          "kept it next to the new post %s", old.id, len(late), new.id)
            else:
                await old.delete(reason="Recreated without the title-change notice")
        await db.set_meta(REBUILD_OLD_KEY, None)
        if pinned:
            await new.edit(pinned=True, reason="This week's movie")
        await self.retag(tmdb_id)
        try:
            await self.bot.movies.update_card(tmdb_id)
        except discord.HTTPException:
            log.exception("Card refresh after rebuilding the post-watch post for TMDB %s failed",
                          tmdb_id)

    async def _relink_movies_note(self, entry, thread_id: int, message_id: int) -> None:
        """Point the entry's spoiler-free #movies note at its new embed."""
        from .letterboxd import movies_note
        if not entry["movies_message_id"]:
            return
        mrow = await self.bot.db.get_movie(entry["tmdb_id"])
        lb = self.bot.letterboxd
        try:
            post = await lb._thread(mrow["thread_id"] if mrow else None)
            if post is None:
                return
            text = movies_note(entry["discord_user_id"], lb._jump(thread_id, message_id),
                               entry["rating"] is not None, bool(entry["review_text"]))
            await post.get_partial_message(entry["movies_message_id"]).edit(
                content=text, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            await self.bot.db.update_entry(entry["guid"], movies_message_id=None)
        except discord.HTTPException:
            log.exception("Could not relink the #movies note for Letterboxd entry %s",
                          entry["guid"])

    async def _resume_rebuild(self) -> None:
        """Finish a post whose rebuild was cut off (restart mid-way)."""
        db = self.bot.db
        raw = await db.get_meta(REBUILD_OLD_KEY)
        if not raw:
            return
        state = json.loads(raw)
        row = await db.get_movie(state["tmdb_id"])
        if row is None or row["spoiler_thread_id"] in (None, state["old"]):
            await db.set_meta(REBUILD_OLD_KEY, None)    # new post never recorded; redo it
            return
        new = await self.find_spoiler_post(row)
        if new is None:
            await db.set_meta(REBUILD_OLD_KEY, None)
            return
        old = self.bot.get_channel(state["old"])
        if old is None:
            try:
                old = await self.bot.fetch_channel(state["old"])
            except discord.NotFound:
                old = None
        log.warning("Resuming the cut-off rebuild of post-watch post %s (TMDB %s)",
                    state["old"], state["tmdb_id"])
        await self._finish_rebuild(state["tmdb_id"], old, new, state["pinned"], None)

    async def _create_post(self, display: str, movies_url: str, poster_url: str | None,
                           tags: list[discord.ForumTag]):
        created = await self.forum.create_thread(
            name=spoiler_post_name(display), embed=starter_embed(display, movies_url, poster_url),
            applied_tags=tags, allowed_mentions=discord.AllowedMentions.none(),
            reason="Post-watch discussion")
        return created.thread, created.message

    # moving a message --------------------------------------------------------
    async def webhook(self) -> discord.Webhook:
        """The bot's own webhook on #post-watch (created once, reused)."""
        if self._webhook is None:
            forum = self.forum
            for wh in await forum.webhooks():
                if wh.user and wh.user.id == self.bot.user.id and wh.name == WEBHOOK_NAME:
                    self._webhook = wh
                    break
            else:
                self._webhook = await forum.create_webhook(
                    name=WEBHOOK_NAME, reason="Moving messages marked as spoilers")
        return self._webhook

    def file_limit(self, guild: discord.Guild | None) -> int:
        return max(guild.filesize_limit if guild else 0, DEFAULT_FILE_LIMIT)

    async def move(self, message: discord.Message, actor: discord.abc.User,
                   tmdb_id: int) -> discord.Message:
        """Move a message from a #movies post into the movie's spoiler post.

        Returns the first moved copy. Raises AlreadyMoved if the original is gone,
        ValueError if it can't be moved (nothing is changed then).
        """
        limit = self.file_limit(message.guild)
        for a in message.attachments:
            if a.size > limit:
                raise ValueError(f"the attachment {a.filename} is too large to re-upload")

        async with self._move_locks[message.id]:
            try:
                message = await message.channel.fetch_message(message.id)
            except discord.NotFound:
                raise AlreadyMoved from None

            spoiler_post, _ = await self.open(tmdb_id)

            header = [f"-# moved from the spoiler-free #movies post by {actor.mention}"]
            ref = message.reference
            if ref and ref.message_id:
                # DeletedReferencedMessage (reply to a deleted message) has no author.
                replied = ref.resolved if getattr(ref.resolved, "author", None) else None
                who = replied.author.display_name if replied else "a message"
                link = (replied.jump_url if replied else
                        f"https://discord.com/channels/{message.guild.id}/"
                        f"{message.channel.id}/{ref.message_id}")
                header.insert(0, f"-# ↪ replying to {who}: {link}")
            texts = move_messages(message.content or "", "\n".join(header))

            files = [await a.to_file(spoiler=a.is_spoiler()) for a in message.attachments]
            batches = batch_files([a.size for a in message.attachments])

            wh = await self.webhook()
            author = message.author
            kwargs = dict(username=safe_webhook_username(author.display_name),
                          avatar_url=author.display_avatar.url, thread=spoiler_post,
                          wait=True, allowed_mentions=discord.AllowedMentions.none())
            sent: list[discord.WebhookMessage] = []
            try:
                for i, text in enumerate(texts):
                    last = i == len(texts) - 1
                    first_files = [files[j] for j in batches[0]] if last and batches else []
                    sent.append(await wh.send(text, files=first_files or discord.utils.MISSING,
                                              **kwargs))
                for batch in batches[1:]:
                    sent.append(await wh.send(files=[files[j] for j in batch], **kwargs))
                await message.delete()
            except discord.HTTPException:
                # Don't leave a half-moved message or a duplicate behind.
                for m in sent:
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        log.exception("Could not roll back moved copy %s", m.id)
                raise
            log.info("Moved message %s from %s to spoiler post %s (by %s)",
                     message.id, message.channel.id, spoiler_post.id, actor.id)
            return sent[0]
