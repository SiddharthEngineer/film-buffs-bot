"""The spoiler line: one per movie post; everything above it is spoiler-free.

A spoiler *point* is a message ID (a Discord snowflake, so it is also a
position in time). The earliest point ever reported for a post wins: an
earlier point moves the line back, a later one changes nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from .db import utcnow
from .movies import SPOILER_FIELD

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

SPOILER_RE = re.compile(r"\|\|.+?\|\|", re.DOTALL)
WEBHOOK_NAME = "Film Buffs reposts"
MESSAGE_LIMIT = 2000


# ------------------------------------------------------------------ pure logic

def has_spoiler_markup(message: discord.Message) -> bool:
    """`||spoiler||` text, or an attachment marked as a spoiler."""
    if SPOILER_RE.search(message.content or ""):
        return True
    return any(a.is_spoiler() for a in message.attachments)


def should_move_marker(current: int | None, new_point: int) -> bool:
    """Earliest spoiler point wins."""
    return current is None or new_point < current


def format_ct(dt: datetime, tz) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%b %-d, %Y %-I:%M %p ") + local.tzname()


def wrap_spoiler(text: str, header: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Wrap text in spoiler bars, split across messages if needed.

    Existing `||` pairs are removed first, since spoilers cannot nest. The
    header (e.g. "-# marked as spoiler by …") goes on the first message only.
    """
    text = text.replace("||", "").strip()
    if not text:
        return [header] if header else []
    chunks: list[str] = []
    first_room = limit - len(header) - 1 - 4 if header else limit - 4
    room = first_room
    while text:
        if len(text) <= room:
            piece, text = text, ""
        else:
            cut = text.rfind("\n", 0, room)
            if cut < room // 2:
                cut = text.rfind(" ", 0, room)
            if cut < room // 2:
                cut = room
            piece, text = text[:cut], text[cut:].lstrip()
        body = f"||{piece}||"
        chunks.append(f"{header}\n{body}" if header and not chunks else body)
        room = limit - 4
    return chunks


def safe_webhook_username(name: str) -> str:
    """Webhook names can't contain 'discord' or 'clyde' and are max 80 chars."""
    for bad in ("discord", "clyde"):
        name = re.sub(bad, lambda m: m.group(0)[0] + "​" + m.group(0)[1:], name,
                      flags=re.IGNORECASE)
    return name[:80] or "Member"


# --------------------------------------------------------------------- service

class SpoilerService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._webhook: discord.Webhook | None = None

    async def movie_for_thread(self, channel) -> "dict | None":
        if not isinstance(channel, discord.Thread):
            return None
        if channel.parent_id != self.bot.config.channels["movies"]:
            return None
        return await self.bot.db.get_movie_by_thread(channel.id)

    async def webhook(self) -> discord.Webhook:
        """The bot's own webhook on #movies (created once, reused)."""
        if self._webhook is None:
            forum = self.bot.movies.forum
            for wh in await forum.webhooks():
                if wh.user and wh.user.id == self.bot.user.id and wh.name == WEBHOOK_NAME:
                    self._webhook = wh
                    break
            else:
                self._webhook = await forum.create_webhook(
                    name=WEBHOOK_NAME, reason="Reposting messages marked as spoilers")
        return self._webhook

    async def is_own_webhook_message(self, message: discord.Message) -> bool:
        return bool(message.webhook_id) and message.webhook_id == (await self.webhook()).id

    async def mark_from_now(self, thread: discord.Thread,
                            actor: discord.abc.User) -> tuple[bool, str]:
        """/spoiler: everything after a new divider may contain spoilers."""
        now_point = discord.utils.time_snowflake(discord.utils.utcnow())
        return await self.mark(thread, now_point, source="command", actor=actor)

    async def mark(self, thread: discord.Thread, point_id: int, *, source: str,
                   actor: discord.abc.User | None = None) -> tuple[bool, str]:
        """Report a spoiler at `point_id` in a movie post.

        Returns (moved, message): moved is True if the line was created or moved
        back. `point_id` may be a deleted message (retroactive marks); links
        then point at the nearest messages that still exist.
        """
        async with self._locks[thread.id]:
            row = await self.bot.db.get_movie_by_thread(thread.id)
            if row is None:
                return False, "not a movie post"
            tmdb_id = row["tmdb_id"]

            flags = set(json.loads(row["flags"]))
            if "spoilers" not in flags:
                flags.add("spoilers")
                await self.bot.db.set_flags(tmdb_id, list(flags))
                await self.bot.movies.retag(tmdb_id)

            current = row["spoiler_marker_message_id"]
            if not should_move_marker(current, point_id):
                return False, f"line already at or before this point (source={source})"

            tz = self.bot.config.tz
            point_time = discord.utils.snowflake_time(point_id)
            old_divider = row["spoiler_divider_message_id"]
            first_spoiler = await self._first_at_or_after(thread, point_id, skip=old_divider)
            last_clean = await self._last_before(thread, point_id)

            if source == "command":
                body = "Everything below this line may contain spoilers."
            elif first_spoiler is not None:
                body = f"Messages from {first_spoiler.jump_url} onward may contain spoilers."
            else:
                body = "Everything below this line may contain spoilers."
            by = f" (marked by {actor.mention})" if actor and source in ("retro", "command") else ""
            divider = await thread.send(
                f"⚠️ **SPOILER LINE** — {format_ct(point_time, tz)}. {body}{by}",
                allowed_mentions=discord.AllowedMentions.none())

            if source == "command":
                point_id = divider.id  # "from now on" means after the divider itself

            await self.bot.db.set_spoiler_marker(
                tmdb_id, marker_id=point_id, divider_id=divider.id,
                marked_at=point_time.isoformat(timespec="seconds"))
            if old_divider and old_divider != divider.id:
                try:
                    await thread.get_partial_message(old_divider).delete()
                except discord.NotFound:
                    pass

            await self._update_starter(thread, row["starter_message_id"], last_clean, tz)
            log.info("Spoiler line in %s (%s) set at %s via %s",
                     thread.id, row["title"], point_id, source)
            return True, divider.jump_url

    async def _first_at_or_after(self, thread: discord.Thread, point_id: int,
                                 skip: int | None = None):
        async for m in thread.history(after=discord.Object(point_id - 1), limit=5,
                                      oldest_first=True):
            if m.id != skip:  # the old divider is about to be deleted
                return m
        return None

    async def _last_before(self, thread: discord.Thread, point_id: int):
        async for m in thread.history(before=discord.Object(point_id), limit=1):
            return m
        return None

    async def _update_starter(self, thread: discord.Thread, starter_id: int,
                              last_clean: discord.Message | None, tz) -> None:
        starter = await thread.fetch_message(starter_id)
        if not starter.embeds:
            return
        embed = starter.embeds[0]
        target = last_clean or starter
        line = (f"✅ Spoiler-free discussion ends here → {target.jump_url} "
                f"({format_ct(target.created_at, tz)})")
        idx = next((i for i, f in enumerate(embed.fields) if f.name == SPOILER_FIELD), None)
        if idx is None:
            embed.add_field(name=SPOILER_FIELD, value=line, inline=False)
        else:
            embed.set_field_at(idx, name=SPOILER_FIELD, value=line, inline=False)
        await starter.edit(embed=embed)

    # retroactive -------------------------------------------------------------
    async def repost_as_spoiler(self, message: discord.Message,
                                actor: discord.abc.User) -> str:
        """Repost `message` hidden behind spoiler bars as its author, delete it,
        and move the spoiler line. Returns a short status for the actor."""
        thread = message.channel
        files = []
        limit = message.guild.filesize_limit if message.guild else 10 * 1024 * 1024
        for a in message.attachments:
            if a.size > limit:
                raise ValueError(f"attachment {a.filename} is too large to re-upload")
            files.append(await a.to_file(spoiler=True))

        header = f"-# marked as spoiler by {actor.mention}"
        ref = message.reference
        if ref and ref.message_id:
            replied = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            who = replied.author.display_name if replied else "a message"
            link = (replied.jump_url if replied else
                    f"https://discord.com/channels/{message.guild.id}/{thread.id}/{ref.message_id}")
            header = f"-# ↪ replying to {who}: {link}\n{header}"
        chunks = wrap_spoiler(message.content or "", header)
        if not chunks:
            chunks = [header]

        wh = await self.webhook()
        author = message.author
        kwargs = dict(username=safe_webhook_username(author.display_name),
                      avatar_url=author.display_avatar.url, thread=thread, wait=True,
                      allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            await wh.send(chunk, files=files if last else discord.utils.MISSING, **kwargs)

        point_id = message.id
        await message.delete()
        moved, info = await self.mark(thread, point_id, source="retro", actor=actor)
        return "Spoiler line moved back to this message." if moved else \
            "It was already below the spoiler line, so the line stays where it is."
