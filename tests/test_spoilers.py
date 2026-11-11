from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from filmbuffs.spoilers import (MESSAGE_LIMIT, format_ct, safe_webhook_username,
                                should_move_marker, wrap_spoiler)


def run_triggers(points):
    """Apply triggers in arrival order; return the marker after each one."""
    marker, history = None, []
    for p in points:
        if should_move_marker(marker, p):
            marker = p
        history.append(marker)
    return history


def test_first_trigger_sets_marker():
    assert run_triggers([500]) == [500]


def test_later_triggers_do_not_move_marker():
    assert run_triggers([500, 600, 900]) == [500, 500, 500]


def test_retroactive_earlier_mark_moves_marker_back():
    assert run_triggers([500, 700, 300, 400]) == [500, 500, 300, 300]


def test_same_point_twice_is_a_no_op():
    assert should_move_marker(500, 500) is False


def test_wrap_short_message():
    assert wrap_spoiler("Rosebud is the sled", "-# marked") == ["-# marked\n||Rosebud is the sled||"]


def test_wrap_removes_existing_bars():
    assert wrap_spoiler("he ||dies|| at the end", "") == ["||he dies at the end||"]


def test_wrap_splits_long_text_and_every_chunk_fits():
    text = ("word " * 900).strip()   # ~4500 chars
    chunks = wrap_spoiler(text, "-# marked as spoiler by <@1>")
    assert len(chunks) >= 3
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks)
    assert chunks[0].startswith("-# marked")
    assert all(c.rstrip().endswith("||") for c in chunks)
    rejoined = " ".join(c.split("\n")[-1].strip("|") for c in chunks)
    assert rejoined.split() == text.split()


def test_wrap_empty_text_with_header_only():
    assert wrap_spoiler("   ", "-# h") == ["-# h"]


def test_webhook_username_sanitised():
    name = safe_webhook_username("Discord Dan the clyde fan")
    assert "discord" not in name.lower() and "clyde" not in name.lower()
    assert len(safe_webhook_username("x" * 200)) == 80


def test_format_ct_handles_dst():
    tz = ZoneInfo("America/Chicago")
    assert format_ct(datetime(2026, 7, 1, 1, 5, tzinfo=timezone.utc), tz) == "Jun 30, 2026 8:05 PM CDT"
    assert format_ct(datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc), tz) == "Dec 1, 2026 12:00 PM CST"
