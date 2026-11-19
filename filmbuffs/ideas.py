"""The ideas list in #ideas (Phase 14).

Members chat about ideas in #ideas; admins curate the list with the
"Add to ideas" message menu, /admin idea add (typed in their own words, Phase 20)
and /admin idea done|drop. The bot keeps one
pinned message listing every active idea (masked links to the messages they
came from) and edits it in place on every change and at startup. Its ID is
bot_meta 'ideas_list_message'.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

META_LIST = "ideas_list_message"
TEXT_MAX = 200          # stored text (and the Add to ideas modal's max length)
LINE_TEXT_MAX = 120     # idea text shown per line in the list
CHOICE_MAX = 100        # Discord: autocomplete choice name ≤ 100 chars
DESCRIPTION_MAX = 4096  # Discord: embed description ≤ 4096 chars
TITLE = "💡 Ideas"
INTRO = ("Ideas for the server and the bot. Post yours in this channel and talk them over; "
         "admins add them to this list.")
EMPTY = "_No active ideas right now._"
COLOR = 0xE5A00D


def one_line(text: str, limit: int = TEXT_MAX) -> str:
    """Whitespace collapsed, no masked-link brackets, at most `limit` chars."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return shorten(text.replace("[", "(").replace("]", ")"), limit)


def clean_text(text: str, limit: int = TEXT_MAX) -> str:
    """As stored and shown: one line with markdown escaped, at most `limit` chars."""
    return shorten(discord.utils.escape_markdown(one_line(text, 10_000), as_needed=False), limit)


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rstrip()
    # Don't end on a lone escape backslash (it would eat the ellipsis).
    while cut.endswith("\\"):
        cut = cut[:-1]
    return cut + "…"


def message_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def idea_line(idea, guild_id: int, names: dict[int, str] | None = None) -> str:
    text = shorten(idea["text"], LINE_TEXT_MAX)
    if idea["message_id"] and idea["channel_id"]:
        line = f"• [{text}]({message_url(guild_id, idea['channel_id'], idea['message_id'])})"
    else:
        line = f"• {text}"
    if idea["suggested_by"]:
        # A plain name: a mention in an embed is a raw ID on phones (Phase 19).
        who = (names or {}).get(idea["suggested_by"])
        line += f" · {who}" if who else f" · <@{idea['suggested_by']}>"
    else:
        line += " · 🛠️ planned"
    return line


def render_list(ideas, guild_id: int, names: dict[int, str] | None = None) -> discord.Embed:
    """The pinned list. Lines that don't fit in the description are summed up at the end."""
    lines = [idea_line(i, guild_id, names) for i in ideas]
    body = INTRO + "\n"
    if not lines:
        return discord.Embed(title=TITLE, description=body + "\n" + EMPTY, color=COLOR)
    for n, line in enumerate(lines):
        left = len(lines) - n - 1
        # Each line must fit together with the "…and N more" note it may need after it.
        note = f"\n…and {left} more" if left else ""
        if len(body) + 1 + len(line) + len(note) > DESCRIPTION_MAX:
            body += f"\n…and {left + 1} more"     # fits: the previous line checked it
            break
        body += "\n" + line
    embed = discord.Embed(title=TITLE, description=body, color=COLOR)
    embed.set_footer(text=f"{len(lines)} active idea{'s' if len(lines) != 1 else ''}")
    return embed


def choice_name(idea) -> str:
    return shorten(f"#{idea['id']} {idea['text']}".replace("\\", ""), CHOICE_MAX)


class IdeasService:
    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._rendered: dict | None = None
        self._pin_warned = False

    def channel(self) -> discord.TextChannel | None:
        cid = self.bot.config.channels.get("ideas")
        ch = self.bot.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def add(self, message: discord.Message, text: str, by: int) -> int:
        idea_id = await self.bot.db.add_idea(
            text=clean_text(text), suggested_by=message.author.id,
            channel_id=message.channel.id, message_id=message.id, added_by=by)
        log.info("%s added idea #%s from message %s", by, idea_id, message.id)
        await self.refresh()
        return idea_id

    async def add_text(self, text: str, suggested_by: int, by: int) -> int:
        """/admin idea add: an idea typed by an admin, with no source message."""
        idea_id = await self.bot.db.add_idea(
            text=clean_text(text), suggested_by=suggested_by,
            channel_id=None, message_id=None, added_by=by)
        log.info("%s added idea #%s (typed, by %s)", by, idea_id, suggested_by)
        await self.refresh()
        return idea_id

    async def close(self, idea_id: int, status: str, by: int) -> bool:
        ok = await self.bot.db.close_idea(idea_id, status, by)
        if ok:
            log.info("%s marked idea #%s %s", by, idea_id, status)
            await self.refresh()
        return ok

    async def refresh(self) -> None:
        """Edit the pinned list in place (post and pin a new one if it's gone)."""
        async with self._lock:
            ch = self.channel()
            if ch is None:
                return
            db = self.bot.db
            from .names import member_names
            ideas = await db.active_ideas()
            names = await member_names(self.bot, [i["suggested_by"] for i in ideas
                                                  if i["suggested_by"]])
            embed = render_list(ideas, self.bot.config.guild_id, names)
            mid = await db.get_meta(META_LIST)
            if mid and embed.to_dict() == self._rendered:
                return
            if mid:
                try:
                    await ch.get_partial_message(int(mid)).edit(embed=embed)
                    self._rendered = embed.to_dict()
                    await self._ensure_pinned(ch, int(mid))
                    return
                except discord.NotFound:
                    log.warning("The #ideas list message was deleted; posting a new one")
            msg = await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await db.set_meta(META_LIST, str(msg.id))
            self._rendered = embed.to_dict()
            log.info("Posted the ideas list message %s in #ideas", msg.id)
            await self._ensure_pinned(ch, msg.id)

    async def _ensure_pinned(self, ch: discord.TextChannel, mid: int) -> None:
        try:
            msg = await ch.fetch_message(mid)
            if not msg.pinned:
                await msg.pin(reason="Ideas list")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            if not self._pin_warned:
                self._pin_warned = True
                log.warning("Can't pin the ideas list in #ideas: the bot needs the Pin Messages "
                            "permission there.", extra={"botlogs": True})
