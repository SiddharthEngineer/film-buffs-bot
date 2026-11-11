"""Posts errors to #bot-logs (admin-only), without letting a failing loop spam it.

- Every ERROR+ log record goes through a throttle: the same error (logger + message
  template + exception type) is posted at most once per `cooldown`; repeats are
  counted and mentioned the next time it's posted. On top of that, at most `burst`
  messages go out per `per` seconds; the rest are counted and summarized.
- A record logged with `extra={"botlogs": True}` is posted whatever its level
  (used for "recovered" notices).
- The full traceback always stays in journald; Discord gets the tail of it.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 2000      # Discord message content limit (checked 2026-11-11)
QUEUE_SIZE = 100
LEVEL_ICONS = {"CRITICAL": "🛑", "ERROR": "🔴", "WARNING": "🟡", "INFO": "🟢"}


# ------------------------------------------------------------------ pure logic

class LogThrottle:
    """Decides which errors get posted. Time is passed in, so it's easy to test."""

    def __init__(self, cooldown: float = 3600, burst: int = 5, per: float = 600):
        self.cooldown, self.burst, self.per = cooldown, burst, per
        self._last_sent: dict[tuple, float] = {}
        self._repeats: dict[tuple, int] = {}
        self._sent: deque[float] = deque()
        self.dropped = 0

    def check(self, key: tuple, now: float) -> tuple[bool, int]:
        """(post it?, how many repeats of this key were held back since it was last posted)."""
        last = self._last_sent.get(key)
        if last is not None and now - last < self.cooldown:
            self._repeats[key] = self._repeats.get(key, 0) + 1
            return False, 0
        while self._sent and now - self._sent[0] >= self.per:
            self._sent.popleft()
        if len(self._sent) >= self.burst:
            self.dropped += 1
            return False, 0
        self._sent.append(now)
        self._last_sent[key] = now
        return True, self._repeats.pop(key, 0)

    def take_dropped(self) -> int:
        n, self.dropped = self.dropped, 0
        return n


class StreakAlert:
    """Turns a run of failures into one alert, and the first success after it into
    one "recovered" notice. fail() is True exactly when the streak hits threshold."""

    def __init__(self, threshold: int):
        self.threshold = threshold
        self._count: dict = {}

    def fail(self, key) -> bool:
        self._count[key] = self._count.get(key, 0) + 1
        return self._count[key] == self.threshold

    def ok(self, key) -> bool:
        """True if this success ends a streak that had been alerted."""
        return self._count.pop(key, 0) >= self.threshold

    def count(self, key) -> int:
        return self._count.get(key, 0)


def record_key(record: logging.LogRecord) -> tuple:
    exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None
    return (record.name, str(record.msg), exc)


def format_entry(level: str, logger: str, message: str, traceback: str | None,
                 when: datetime, tz: ZoneInfo, repeats: int = 0, dropped: int = 0) -> str:
    """One #bot-logs message, always within Discord's 2000-character limit."""
    stamp = when.astimezone(tz).strftime("%b %d %H:%M %Z")
    head = f"{LEVEL_ICONS.get(level, '🔴')} **{level}** `{logger}` · {stamp}\n"
    notes = []
    if repeats:
        notes.append(f"repeated {repeats}× since last posted")
    if dropped:
        notes.append(f"{dropped} other error{'s' if dropped != 1 else ''} not posted")
    foot = f"\n-# {' · '.join(notes)}; see journalctl" if notes else ""
    body = message.strip() or "(no message)"
    body = body[:600] + ("…" if len(body) > 600 else "")
    text = head + body
    if traceback:
        room = MESSAGE_LIMIT - len(text) - len(foot) - len("\n```\n…\n```")
        tb = traceback.strip().replace("```", "'''")
        if room > 100:
            tail = tb if len(tb) <= room else "…" + tb[-(room - 1):]
            text += f"\n```\n{tail}\n```"
    return (text + foot)[:MESSAGE_LIMIT]


# ------------------------------------------------------------------ handler

class DiscordLogHandler(logging.Handler):
    """Logging handler that queues records for posting in #bot-logs.
    emit() may be called from any thread; all posting happens on the bot's loop."""

    def __init__(self, bot: "FilmBuffsBot", throttle: LogThrottle | None = None):
        super().__init__(level=logging.DEBUG)   # filtering happens in emit()
        self.bot = bot
        self.throttle = throttle or LogThrottle()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[str] | None = None
        self._task: asyncio.Task | None = None
        self._tb_formatter = logging.Formatter()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(QUEUE_SIZE)
        self._task = self._loop.create_task(self._sender(), name="botlogs-sender")
        logging.getLogger().addHandler(self)

    async def stop(self) -> None:
        logging.getLogger().removeHandler(self)
        if self._task:
            self._task.cancel()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == __name__:        # never post our own failures (no loops)
                return
            if record.levelno < logging.ERROR and not getattr(record, "botlogs", False):
                return
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            tb = None
            if record.exc_info and record.exc_info[0]:
                tb = self._tb_formatter.formatException(record.exc_info)
            item = (record_key(record), record.levelname, record.name, record.getMessage(),
                    tb, datetime.fromtimestamp(record.created, timezone.utc), record.created)
            loop.call_soon_threadsafe(self._enqueue, item)
        except Exception:
            self.handleError(record)

    def _enqueue(self, item) -> None:
        key, level, name, message, tb, when, created = item
        ok, repeats = self.throttle.check(key, created)
        if not ok:
            return
        text = format_entry(level, name, message, tb, when, self.bot.config.tz,
                            repeats=repeats, dropped=self.throttle.take_dropped())
        self.post(text)

    def post(self, text: str) -> None:
        """Queue a message for #bot-logs as is (no throttle). Call on the bot's loop."""
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(text[:MESSAGE_LIMIT])
        except asyncio.QueueFull:
            self.throttle.dropped += 1

    def _channel(self) -> discord.abc.Messageable | None:
        cid = self.bot.config.channels.get("bot_logs")
        return self.bot.get_channel(cid) if cid else None  # type: ignore[return-value]

    async def _sender(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            text = await self._queue.get()
            channel = self._channel()
            if channel is None:
                log.warning("#bot-logs not found; dropping: %s", text[:200])
                continue
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none(),
                                   suppress_embeds=True)
            except Exception:
                log.warning("Could not post to #bot-logs", exc_info=True)
                await asyncio.sleep(30)
