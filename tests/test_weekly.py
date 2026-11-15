"""Phase 5/5R: watchlists, ballot, ranked vote (instant runoff), weekly schedule."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from filmbuffs.db import Database
from filmbuffs.pool import PoolService
from filmbuffs.weekly import (Schedule, WeeklyService, date_range, instant_runoff,
                              how_picked, log_line, next_action, option_label, ordinal, runoff_text,
                              weeks_to_create, when)

TZ = ZoneInfo("America/Chicago")


def local(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


# ------------------------------------------------------------------ calendar

def test_schedule_deadlines():
    s = Schedule(date(2026, 11, 23), TZ)
    assert s.vote_open == s.ballot_open == local(2026, 11, 16)   # Mon the week before
    assert s.vote_close == local(2026, 11, 22, 23, 59)     # Sun
    assert s.watch_start == local(2026, 11, 23)
    assert s.watch_end == local(2026, 11, 29, 23, 59)


def test_schedule_across_dst():
    # DST starts Sun Mar 14 2027, inside the vote: deadlines stay local.
    s = Schedule(date(2027, 3, 15), TZ)
    assert s.vote_close == local(2027, 3, 14, 23, 59)
    assert s.vote_close.utcoffset() == timedelta(hours=-5)
    assert s.vote_open.utcoffset() == timedelta(hours=-6)
    # DST ends Sun Nov 7 2027.
    s = Schedule(date(2027, 11, 8), TZ)
    assert s.vote_close == local(2027, 11, 7, 23, 59)
    assert s.vote_close.utcoffset() == timedelta(hours=-6)


def test_bootstrap_opens_first_ballot_the_monday_before():
    first = date(2026, 11, 23)
    assert weeks_to_create(local(2026, 11, 12, 15), TZ, first, set()) == []
    assert weeks_to_create(local(2026, 11, 16), TZ, first, set()) == [first]
    assert weeks_to_create(local(2026, 11, 21), TZ, first, set()) == [first]   # late: still votes
    assert weeks_to_create(local(2026, 11, 16), TZ, first, {first}) == []
    assert weeks_to_create(local(2026, 11, 16), TZ, None, set()) == []


def test_steady_state_creates_next_week_on_monday():
    existing = {date(2026, 11, 23)}
    now = local(2026, 11, 23, 0, 0)
    assert weeks_to_create(now, TZ, date(2026, 11, 23), existing) == [date(2026, 11, 30)]


def test_outage_never_starts_a_week_whose_vote_is_over():
    now = local(2026, 11, 24, 12)          # first watch week already started
    assert date(2026, 11, 23) not in weeks_to_create(now, TZ, date(2026, 11, 23), set())


def test_next_action_sequence():
    s = Schedule(date(2026, 11, 23), TZ)
    assert next_action("voting", s, local(2026, 11, 16)) is None
    assert next_action("voting", s, local(2026, 11, 22, 23, 58)) is None
    assert next_action("voting", s, local(2026, 11, 22, 23, 59)) == "close_vote"
    assert next_action("picked", s, local(2026, 11, 28), pinned=True) is None
    assert next_action("picked", s, local(2026, 11, 29, 23, 59)) == "end_watch"
    assert next_action("done", s, local(2027, 1, 1)) is None
    assert next_action("ballot", s, local(2027, 1, 1)) is None     # 5R states are migrated


def test_formatting():
    assert date_range(date(2026, 11, 23)) == "Nov 23–29"
    assert date_range(date(2026, 11, 30)) == "Nov 30–Dec 6"
    assert when(local(2026, 11, 15, 23, 59), TZ) == "Sun Nov 15, 11:59 PM CT"
    assert when(local(2026, 11, 16), TZ) == "Mon Nov 16, 12:00 AM CT"
    assert log_line(date(2026, 11, 23), "Parasite (2019)", "won 5–3") == \
        "Wk 48 · Nov 23–29 2026 · Parasite (2019) · won 5–3"
    row = {"override_by": None, "winner_votes": 4, "runner_up_votes": 3,
           "vote_rounds": json.dumps([{}, {}, {}])}
    assert how_picked(row) == "won 4–3 after 3 rounds"
    assert how_picked({**row, "winner_votes": None}) == "the only nomination, so no vote"
    assert how_picked({**row, "override_by": 7}) == "picked by an admin"
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"]
    assert log_line(date(2026, 11, 23), None, skipped="nobody voted").endswith("no pick (nobody voted)")
    long = {"title": "x" * 150, "year": 2001}
    assert len(option_label(long)) <= 100 and option_label(long).endswith(" (2001)")


# ------------------------------------------------------------------ ballot / winner

def cand(tid, interest=0, added="2026-01-01T00:00:00+00:00"):
    return {"tmdb_id": tid, "interest": interest, "added_at": added}


ORDER = ["interest", "added"]


def test_irv_nobody_voted():
    assert instant_runoff([], [cand(1), cand(2)], ORDER) is None
    assert instant_runoff([[], [99]], [cand(1), cand(2)], ORDER) is None   # only unknown films


def test_irv_majority_in_round_one():
    r = instant_runoff([[1, 2], [1], [2, 1]], [cand(1), cand(2), cand(3)], ORDER)
    assert r["winner"] == 1 and len(r["rounds"]) == 1
    assert (r["winner_votes"], r["runner_up_votes"], r["voters"]) == (2, 1, 3)
    assert r["first_choices"] == {1: 2, 2: 1, 3: 0}


def test_irv_transfers_beat_plurality():
    # A leads on 1st choices (4) but B wins once C's voters move to their 2nd.
    ballots = [[1]] * 4 + [[2, 3]] * 3 + [[3, 2]] * 2
    r = instant_runoff(ballots, [cand(1), cand(2), cand(3)], ORDER)
    assert r["winner"] == 2
    assert [rd["out"] for rd in r["rounds"]] == [[3], []]
    assert r["rounds"][1]["counts"] == {1: 4, 2: 5}
    assert (r["winner_votes"], r["runner_up_votes"]) == (5, 4)


def test_irv_drops_all_zero_vote_films_together():
    ballots = [[1], [1], [2], [3, 2]]
    r = instant_runoff(ballots, [cand(i) for i in range(1, 6)], ORDER)
    assert sorted(r["rounds"][0]["out"]) == [4, 5]
    # Round 2: 1=2, 2=1, 3=1 → no majority (2 of 4). 2 and 3 tie for last with
    # one 1st choice each and equal everything else: the higher id goes out.
    assert r["rounds"][1]["out"] == [3]
    # Round 3: 3's ballot moves to 2 → 2–2. A tie between the last two: 2 has
    # fewer 1st choices, so 1 wins without an extra one-film round.
    assert r["rounds"][2]["counts"] == {1: 2, 2: 2} and r["rounds"][2]["out"] == [2]
    assert r["winner"] == 1 and len(r["rounds"]) == 3
    assert (r["winner_votes"], r["runner_up_votes"]) == (2, 2)


def test_irv_partial_ballot_runs_out():
    ballots = [[1], [1], [2], [2], [3]]
    r = instant_runoff(ballots, [cand(1, interest=5), cand(2), cand(3)], ORDER)
    # 3 goes out; its ballot has no other choice, so it's used up. 1 vs 2 is 2–2
    # with 4 ballots counting: no majority, tie → 2 goes (less interest).
    assert r["rounds"][1]["exhausted"] == 1
    assert r["winner"] == 1 and len(r["rounds"]) == 2


def test_irv_tie_breaks_for_who_goes_out():
    two = [[1], [2]]
    # Equal 1st choices: lower interest goes out.
    assert instant_runoff(two, [cand(1, interest=3), cand(2, interest=1)], ORDER)["winner"] == 1
    # Then the film added last goes out.
    late, early = "2026-06-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"
    assert instant_runoff(two, [cand(1, added=late), cand(2, added=early)], ORDER)["winner"] == 2
    # Fewest 1st choices beats everything else: 3 has none but most interest.
    ballots = [[1, 3], [2, 3]] + [[1]] + [[2]]
    r = instant_runoff(ballots, [cand(1), cand(2), cand(3, interest=9)], ORDER)
    assert r["rounds"][0]["out"] == [3]


def test_irv_single_voter_and_duplicates_ignored():
    r = instant_runoff([[2, 2, 1]], [cand(1), cand(2)], ORDER)
    assert r["winner"] == 2 and r["voters"] == 1 and r["winner_votes"] == 1


def test_runoff_text():
    ballots = [[1]] * 4 + [[2, 3]] * 3 + [[3, 2]] * 2
    r = instant_runoff(ballots, [cand(1), cand(2), cand(3)], ORDER)
    text = runoff_text(r, {1: "A", 2: "B", 3: "C"})
    assert text.splitlines() == ["**Round 1:** A **4** · B **3** · C **2**", "-# Out: C",
                                 "**Round 2:** B **5** · A **4**"]


# ------------------------------------------------------------------ database

async def seeded(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    for tid, status, genres in ((1, "backlog", '["g_drama"]'), (2, "backlog", '["g_horror"]'),
                                (3, "watched_solo", "[]"), (4, "watched", "[]"),
                                (5, "up_next", "[]")):
        await db.insert_movie(tmdb_id=tid, thread_id=100 + tid, starter_message_id=200 + tid,
                              title=f"Film {tid}", year=2000 + tid, status=status, flags=[],
                              genre_tags=[], added_by=1, pitch=None, source="addmovie")
        await db.conn.execute("UPDATE movies SET genre_tags=? WHERE tmdb_id=?", (genres, tid))
    await db.conn.commit()
    return db


def test_interest_is_watchlists_only(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        await db.replace_watchlist(10, [(1, "Film 1", 2001)], 1, [])
        await db.replace_watchlist(13, [(1, "Film 1", 2001), (99, "No post", 1999)], 2, ["X"])
        one, two = await db.movie_interest(1), await db.movie_interest(2)
        summary = await db.watchlist_summary(13)
        tables = {r[0] for r in await (await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        cols = {r[1] for r in await (await db.conn.execute(
            "PRAGMA table_info(week_options)")).fetchall()}
        await db.close()
        return one, two, summary, tables, cols
    one, two, (imp, n, posted), tables, cols = asyncio.run(run())
    assert (one, two) == (2, 0)
    assert (n, posted, imp["matched"]) == (2, 1, 2)
    assert "votes" not in tables and "thumbs" not in cols      # 👍 tracking dropped (migration 12)


def test_migration_seeds_watchlist_from_backfill(tmp_path):
    async def run():
        db = Database(tmp_path / "t.db")
        await db.connect()
        # Simulate a pre-Phase 5 database: roll back to schema 5 and re-run 6.
        for t in ("watchlist_items", "watchlist_imports", "weeks", "nominations",
                  "week_options", "poll_voters", "bot_meta"):           # (votes: migration 12)
            await db.conn.execute(f"DROP TABLE {t}")
        await db.conn.execute("DROP TABLE letterboxd_syncs")       # migration 8
        await db.conn.execute("DROP TABLE ranked_votes")           # migration 9
        for t in ("pool_nominations", "ballot_entries", "track_rankings", "months",
                  "month_options", "month_votes"):                 # migration 11
            await db.conn.execute(f"DROP TABLE {t}")
        for col in ("updated_at", "removed_at", "embed_source"):
            await db.conn.execute(f"ALTER TABLE letterboxd_entries DROP COLUMN {col}")
        for col in ("availability", "availability_checked_at",      # migration 10
                    "poster_url"):                                   # migration 14
            await db.conn.execute(f"ALTER TABLE movies DROP COLUMN {col}")
        await db.conn.execute("DELETE FROM schema_version WHERE version >= 6")
        for guid, tid in (("a", 1), ("b", None)):
            await db.insert_entry({"guid": guid, "discord_user_id": 10, "username": "me",
                                   "kind": "watchlist", "tmdb_id": tid, "film_title": "F",
                                   "film_year": 2000, "source": "backfill", "state": "imported",
                                   "liked": 0, "rewatch": 0})
        await db.conn.commit()
        await db.close()
        db = Database(tmp_path / "t.db")
        await db.connect()
        imp, n, _ = await db.watchlist_summary(10)
        await db.close()
        return imp, n
    imp, n = asyncio.run(run())
    assert n == 1 and imp["source"] == "backfill" and imp["rows"] == 2 and imp["matched"] == 1


# ------------------------------------------------------------------ nominations

class FakeWeekly(WeeklyService):
    async def refresh_ballot(self, week_start):
        self.refreshed = getattr(self, "refreshed", 0) + 1


def fake_bot(db):
    cfg = SimpleNamespace(tz=TZ, channels={}, guild_id=1,
                          first_watch_week=date(2026, 11, 23),
                          tie_break=["interest", "added"])
    bot = SimpleNamespace(db=db, config=cfg)
    bot.pool = PoolService(bot)
    return bot


# ------------------------------------------------------------------ follow-ups

def test_watch_start_pins_once():
    s = Schedule(date(2026, 11, 23), TZ)
    assert next_action("picked", s, local(2026, 11, 22, 23, 59)) is None
    assert next_action("picked", s, local(2026, 11, 23)) == "start_watch"
    assert next_action("picked", s, local(2026, 11, 23), pinned=True) is None
    # Down all week: just end it.
    assert next_action("picked", s, local(2026, 11, 30)) == "end_watch"


def test_override_log_line():
    assert log_line(date(2026, 11, 23), "Heat (1995)").endswith("Heat (1995) · picked by an admin")


class FakeMoviesSvc:
    def __init__(self):
        self.retagged = []

    async def retag(self, tmdb_id):
        self.retagged.append(tmdb_id)

    async def find_post(self, tmdb_id):
        return None


def test_admin_pick_sets_and_replaces(tmp_path):
    now = local(2026, 11, 11, 15)          # week of Nov 9; "next" = Nov 16

    async def run():
        db = await seeded(tmp_path)
        bot = fake_bot(db)
        bot.movies = FakeMoviesSvc()
        bot.get_guild = lambda gid: None
        bot.get_channel = lambda cid: None
        svc = FakeWeekly(bot)
        out = [await svc.admin_pick(1, "next", 3, now)]           # Watched (solo) film
        st = [(await db.get_movie(3))["status"]]
        out.append(await svc.admin_pick(1, "next", 3, now))
        out.append(await svc.admin_pick(1, "next", 1, now))       # replace
        st += [(await db.get_movie(3))["status"], (await db.get_movie(1))["status"]]
        out.append(await svc.admin_pick(1, "this", 1, now))       # Up Next elsewhere
        w = await db.get_week("2026-11-16")
        await db.close()
        return out, st, dict(w)
    out, st, w = asyncio.run(run())
    assert out[0].startswith("✅ **Film 3 (2003)** is the movie for Nov 16–22")
    assert "already the movie" in out[1]
    assert "Film 3 (2003) went back to watched solo" in out[2]
    assert "already Up Next" in out[3]
    assert st == ["up_next", "watched_solo", "up_next"]
    assert w["state"] == "picked" and w["winner_tmdb_id"] == 1 and w["override_by"] == 1
    assert w["winner_prev_status"] == "backlog" and w["winner_votes"] is None




def test_admin_clear_undoes_pick(tmp_path):
    now = local(2026, 11, 11, 15)          # week of Nov 9

    async def run():
        db = await seeded(tmp_path)
        bot = fake_bot(db)
        bot.movies = FakeMoviesSvc()
        bot.get_guild = lambda gid: None
        bot.get_channel = lambda cid: None
        svc = FakeWeekly(bot)
        none = await svc.admin_clear(1, "this", now)
        await svc.admin_pick(1, "this", 3, now)                   # Watched (solo) film
        await db.update_week("2026-11-09", pinned_at=None)       # no Discord in tests
        out = await svc.admin_clear(1, "this", now)
        w = dict(await db.get_week("2026-11-09"))
        status = (await db.get_movie(3))["status"]
        await svc.tick(local(2026, 11, 16, 0, 5))                 # week end: no log, stays skipped
        after = (await db.get_week("2026-11-09"))["state"]
        repick = await svc.admin_pick(1, "this", 3, now)          # can still set one later
        await db.close()
        return none, out, w, status, after, repick
    none, out, w, status, after, repick = asyncio.run(run())
    assert none.startswith("❌ No movie is set for Nov 9–15")
    assert out.startswith("✅ Cleared **Film 3 (2003)** for Nov 9–15 (back to watched solo")
    assert status == "watched_solo"
    assert w["state"] == "skipped" and w["winner_tmdb_id"] is None and w["event_id"] is None
    assert after == "skipped"
    assert repick.startswith("✅ **Film 3 (2003)** is the movie for Nov 9–15")
def test_welcome_refs_must_be_channel_keys(tmp_path):
    import pytest
    import setup_server
    text = Path("server.yaml").read_text()
    assert "{#watch-planning}" not in text
    setup_server.load_config("server.yaml")                       # the real file is valid
    bad = tmp_path / "s.yaml"
    bad.write_text(text.replace("{#watch_planning}", "{#watch-planning}", 1))
    with pytest.raises(setup_server.ConfigError, match=r"\{#watch_planning\}"):
        setup_server.load_config(bad)

