"""Phase 7: backup naming/retention, #bot-logs throttling and formatting, admin check."""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from filmbuffs.backup import (backup_name, backups_to_prune, latest_backup,
                              parse_backup_time, run_backup)
from filmbuffs.botlogs import (MESSAGE_LIMIT, LogThrottle, StreakAlert, format_entry,
                               record_key)
from filmbuffs.cogs.admin import is_admin

UTC = timezone.utc
CHI = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------- backups

def test_backup_name_roundtrip():
    when = datetime(2026, 11, 12, 4, 0, tzinfo=CHI)
    name = backup_name(when)
    assert name == "filmbuffs-20261112T100000Z.db"
    assert parse_backup_time(name) == when


def test_parse_ignores_other_files():
    for n in ("notes.txt", "filmbuffs.db", "filmbuffs-20261112T100000Z.db.partial",
              "filmbuffs-2026-11-12.db"):
        assert parse_backup_time(n) is None


def test_prune_keeps_14_days():
    names = [backup_name(NOW - timedelta(days=d)) for d in range(20)]
    pruned = backups_to_prune(names + ["notes.txt"], NOW, 14)
    assert pruned == sorted(backup_name(NOW - timedelta(days=d)) for d in range(15, 20))
    # Exactly 14 days old is kept (strictly older is pruned).
    assert backup_name(NOW - timedelta(days=14)) not in pruned


def test_prune_always_keeps_newest():
    old = [backup_name(NOW - timedelta(days=d)) for d in (30, 40, 50)]
    assert backups_to_prune(old, NOW, 14) == sorted(old[1:])
    assert backups_to_prune([], NOW, 14) == []


def test_run_backup_is_consistent_snapshot(tmp_path):
    src = tmp_path / "live.db"
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.commit()          # still open: data sits in the WAL, like the live bot
    dest = tmp_path / "backups"
    dest.mkdir()
    stale = backup_name(NOW - timedelta(days=20))
    (dest / stale).write_bytes(b"old")
    (dest / "filmbuffs-20261101T000000Z.db.partial").write_bytes(b"crashed")

    path, pruned = run_backup(src, dest, 14, now=NOW)
    conn.close()
    assert pruned == [stale]
    assert sorted(p.name for p in dest.iterdir()) == [path.name]   # single file, no -wal
    b = sqlite3.connect(path)
    assert b.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100
    assert b.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    b.close()
    assert latest_backup(dest) == (NOW, path)


# ---------------------------------------------------------------- throttle

def test_same_error_posted_once_per_cooldown():
    t = LogThrottle(cooldown=60, burst=100, per=600)
    k = ("filmbuffs.x", "boom %s", "ValueError")
    assert t.check(k, 0) == (True, 0)
    for s in range(1, 6):
        assert t.check(k, s) == (False, 0)
    assert t.check(k, 61) == (True, 5)      # posted again, mentions the 5 held back
    assert t.check(k, 62) == (False, 0)


def test_different_errors_are_independent():
    t = LogThrottle(cooldown=60, burst=100, per=600)
    assert t.check(("a",), 0)[0] and t.check(("b",), 0)[0]


def test_burst_limit_drops_and_counts():
    t = LogThrottle(cooldown=60, burst=3, per=600)
    sent = [t.check((f"e{i}",), i)[0] for i in range(10)]
    assert sent == [True] * 3 + [False] * 7
    assert t.take_dropped() == 7 and t.take_dropped() == 0
    # A dropped error isn't marked as sent, so it gets through once the window passes.
    assert t.check(("e5",), 601) == (True, 0)


def test_streak_alert_once_then_recovery():
    s = StreakAlert(3)
    assert [s.fail("u") for _ in range(5)] == [False, False, True, False, False]
    assert s.ok("u") is True
    assert s.ok("u") is False            # already reset
    s.fail("v")
    assert s.ok("v") is False            # never alerted, so no recovery notice


# ---------------------------------------------------------------- formatting

def test_record_key_groups_by_template_and_exception():
    def rec(msg, args, exc):
        return logging.LogRecord("filmbuffs.lb", logging.ERROR, __file__, 1, msg, args,
                                 (exc, exc(), None) if exc else None)
    a = record_key(rec("Polling %s failed", (1,), ValueError))
    b = record_key(rec("Polling %s failed", (2,), ValueError))
    c = record_key(rec("Polling %s failed", (2,), KeyError))
    assert a == b != c


def test_format_entry_fits_and_keeps_traceback_tail():
    tb = "Traceback (most recent call last):\n" + "  frame\n" * 1000 + "ValueError: the end"
    text = format_entry("ERROR", "filmbuffs.x", "x" * 5000, tb, NOW, CHI, repeats=3, dropped=1)
    assert len(text) <= MESSAGE_LIMIT
    assert "ValueError: the end" in text
    assert "repeated 3×" in text and "1 other error not posted" in text
    assert "Nov 20 04:00 CST" in text


def test_format_entry_escapes_code_fences():
    text = format_entry("ERROR", "x", "msg", "```inject```", NOW, CHI)
    assert text.count("```") == 2


# ---------------------------------------------------------------- admin check

def _member(manage=False, admin=False, roles=()):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_guild=manage, administrator=admin),
        roles=[SimpleNamespace(name=r) for r in roles])


def test_is_admin():
    assert is_admin(_member(manage=True), "Moderator")
    assert is_admin(_member(admin=True), "Moderator")
    assert is_admin(_member(roles=["Moderator"]), "Moderator")
    assert not is_admin(_member(roles=["@everyone", "Film Buff"]), "Moderator")
    assert not is_admin(SimpleNamespace(), "Moderator")     # a User outside the guild


def test_handler_posts_errors_once_and_ignores_warnings():
    import asyncio

    from filmbuffs.botlogs import DiscordLogHandler

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kw):
            self.sent.append(text)

    async def run():
        ch = Channel()

        async def ready():
            return None
        bot = SimpleNamespace(config=SimpleNamespace(channels={"bot_logs": 5}, tz=CHI),
                              get_channel=lambda cid: ch if cid == 5 else None,
                              wait_until_ready=ready)
        h = DiscordLogHandler(bot, LogThrottle(cooldown=3600, burst=5, per=600))
        h.start()
        try:
            lg = logging.getLogger("filmbuffs.test_handler")
            lg.warning("just a warning")
            lg.warning("came back", extra={"botlogs": True})
            for i in range(20):                       # a failing loop
                try:
                    raise ValueError(f"boom {i}")
                except ValueError:
                    lg.exception("Loop step %d failed", i)
            logging.getLogger("filmbuffs.botlogs").error("own failure")   # never posted
            h.post("🟢 started")
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            await h.stop()
        assert len(ch.sent) == 3
        assert "🟢 started" in ch.sent
        assert any("came back" in m for m in ch.sent)
        assert any("Loop step 0 failed" in m and "ValueError: boom 0" in m for m in ch.sent)
    asyncio.run(run())
