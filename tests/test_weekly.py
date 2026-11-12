"""Phase 5/5R: votes, watchlists, ballot, ranked vote (instant runoff), weekly schedule."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from filmbuffs.db import Database
from filmbuffs.votes import VoteService, is_vote
from filmbuffs.weekly import (Schedule, WeeklyService, compose_ballot, date_range, instant_runoff,
                              how_picked, log_line, next_action, option_label, ordinal, runoff_text,
                              weeks_to_create, when)

TZ = ZoneInfo("America/Chicago")


def local(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


# ------------------------------------------------------------------ calendar

def test_schedule_deadlines():
    s = Schedule(date(2026, 11, 23), TZ)
    assert s.ballot_open == local(2026, 11, 16)            # Mon
    assert s.ballot_close == local(2026, 11, 19, 23, 59)   # Thu
    assert s.vote_open == local(2026, 11, 20)              # Fri
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
    assert next_action("ballot", s, local(2026, 11, 19, 23, 58)) is None
    assert next_action("ballot", s, local(2026, 11, 19, 23, 59)) == "close_ballot"
    assert next_action("ballot_closed", s, local(2026, 11, 19, 23, 59)) is None
    assert next_action("ballot_closed", s, local(2026, 11, 20)) == "open_vote"
    assert next_action("voting", s, local(2026, 11, 22, 23, 58)) is None
    assert next_action("voting", s, local(2026, 11, 22, 23, 59)) == "close_vote"
    assert next_action("picked", s, local(2026, 11, 28), pinned=True) is None
    assert next_action("picked", s, local(2026, 11, 29, 23, 59)) == "end_watch"
    assert next_action("done", s, local(2027, 1, 1)) is None


def test_open_ballot_from_old_schedule_carries_over():
    # The 2026-11-23 ballot was opened on Nov 9 under the old schedule; it just
    # stays open until Thu Nov 19 and is voted on Nov 20–22.
    s = Schedule(date(2026, 11, 23), TZ)
    assert next_action("ballot", s, local(2026, 11, 12, 15)) is None
    assert next_action("ballot", s, local(2026, 11, 15, 23, 59)) is None


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

def mv(tid, interest=0, thumbs=0, status="backlog"):
    return {"tmdb_id": tid, "interest": interest, "thumbs": thumbs, "status": status}


def nom(tid, user, status="backlog"):
    return {"tmdb_id": tid, "user_id": user, "status": status}


def test_compose_ballot_is_nominations_only():
    ranked = [mv(1, 5, 5), mv(2, 4, 3), mv(3, 3), mv(4, 0), mv(5, 9, status="watched")]
    opts = compose_ballot([nom(3, 70), nom(9, 71)], ranked)
    assert [(o["tmdb_id"], o["source"]) for o in opts] == [(3, "nomination"), (9, "nomination")]
    assert opts[0]["nominated_by"] == 70 and opts[0]["interest"] == 3
    assert compose_ballot([], ranked) == []                 # no fill from most wanted


def test_compose_ballot_drops_ineligible():
    opts = compose_ballot([nom(7, 70, status="up_next"), nom(8, 71, status="watched"),
                           nom(9, 72, status="watched_solo")], [])
    assert [o["tmdb_id"] for o in opts] == [9]


def test_compose_ballot_caps_at_dropdown_limit():
    opts = compose_ballot([nom(i, i) for i in range(30)], [])
    assert len(opts) == 25


def cand(tid, interest=0, thumbs=0, added="2026-01-01T00:00:00+00:00"):
    return {"tmdb_id": tid, "interest": interest, "thumbs": thumbs, "added_at": added}


ORDER = ["interest", "thumbs", "added"]


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
    # Then fewer 👍.
    assert instant_runoff(two, [cand(1, 2, 1), cand(2, 2, 4)], ORDER)["winner"] == 2
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


def test_interest_counts_each_member_once(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        await db.set_votes(1, {10, 11})
        await db.set_votes(2, {10, 11, 12})
        await db.replace_watchlist(10, [(1, "Film 1", 2001)], 1, [])     # same member as a 👍
        await db.replace_watchlist(13, [(1, "Film 1", 2001), (99, "No post", 1999)], 2, ["X"])
        ranked = await db.ranked_movies(statuses=("backlog", "watched_solo"))
        top = [(r["tmdb_id"], r["interest"], r["thumbs"], r["listed"]) for r in ranked]
        drama = await db.ranked_movies(genre="g_drama")
        wanted = await db.ranked_movies(min_interest=1, limit=1)
        summary = await db.watchlist_summary(13)
        await db.close()
        return top, [r["tmdb_id"] for r in drama], [r["tmdb_id"] for r in wanted], summary
    top, drama, wanted, (imp, n, posted) = asyncio.run(run())
    # Film 1: 👍 10, 11 + watchlist 10 (dupe), 13 → 3 members; Film 2: 3 members, 3 👍 wins tie.
    assert top[0] == (2, 3, 3, 0) and top[1] == (1, 3, 2, 2)
    assert top[2][0] == 3 and len(top) == 3                # watched / up_next filtered out
    assert drama == [1] and wanted == [2]
    assert (n, posted, imp["matched"]) == (2, 1, 2)


def test_set_votes_and_remove(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        await db.set_votes(1, {10, 11})
        await db.add_vote(1, 12)
        await db.set_votes(1, {11, 12, 13})
        a = await db.movie_interest(1)
        await db.remove_vote(1, 13)
        b = await db.movie_interest(1)
        await db.remove_vote(1)
        c = await db.movie_interest(1)
        await db.close()
        return a, b, c
    assert asyncio.run(run()) == ((3, 3, 0), (2, 2, 0), (0, 0, 0))


def test_migration_seeds_watchlist_from_backfill(tmp_path):
    async def run():
        db = Database(tmp_path / "t.db")
        await db.connect()
        # Simulate a pre-Phase 5 database: roll back to schema 5 and re-run 6.
        for t in ("votes", "watchlist_items", "watchlist_imports", "weeks", "nominations",
                  "week_options", "poll_voters", "bot_meta"):
            await db.conn.execute(f"DROP TABLE {t}")
        await db.conn.execute("DROP INDEX movies_starter")
        await db.conn.execute("DROP TABLE letterboxd_syncs")       # migration 8
        await db.conn.execute("DROP TABLE ranked_votes")           # migration 9
        for col in ("updated_at", "removed_at", "embed_source"):
            await db.conn.execute(f"ALTER TABLE letterboxd_entries DROP COLUMN {col}")
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
                          tie_break=["interest", "thumbs", "added"])
    return SimpleNamespace(db=db, config=cfg)


def test_nominate_rules(tmp_path, monkeypatch):
    import filmbuffs.weekly
    monkeypatch.setattr(filmbuffs.weekly, "MAX_BALLOT", 2)

    async def run():
        db = await seeded(tmp_path)
        svc = FakeWeekly(fake_bot(db))
        out = [await svc.nominate(10, 1)]                     # no ballot yet
        await db.create_week("2026-11-23")
        out.append(await svc.nominate(10, 1))                 # ok
        out.append(await svc.nominate(10, 1))                 # already yours
        out.append(await svc.nominate(11, 1))                 # someone else's
        out.append(await svc.nominate(11, 4))                 # watched together
        out.append(await svc.nominate(11, 5))                 # up next
        out.append(await svc.nominate(11, 3))                 # watched (solo) is fine
        out.append(await svc.nominate(12, 2))                 # ballot full
        out.append(await svc.nominate(10, 2))                 # swap keeps the slot
        noms = [(n["user_id"], n["tmdb_id"]) for n in await db.nominations("2026-11-23")]
        out.append(await svc.unnominate(11))
        out.append(await svc.unnominate(11))
        await db.close()
        return out, noms
    out, noms = asyncio.run(run())
    assert out[0].startswith("❌ No ballot")
    assert out[1].startswith("✅ Nominated **Film 1 (2001)**")
    assert "already your nomination" in out[2]
    assert "already nominated by <@10>" in out[3]
    assert "watched together" in out[4] and "Up Next" in out[5]
    assert out[6].startswith("✅")
    assert out[7].startswith("❌ The ballot already has 2")
    assert out[8].startswith("✅") and "instead of <#101>" in out[8]
    assert noms == [(10, 2), (11, 3)]
    assert out[9].startswith("✅") and out[10].startswith("You have no")


def test_ballot_options_from_db(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        await db.create_week("2026-11-23")
        await db.set_votes(1, {10})
        await db.set_votes(2, {10, 11})
        await db.set_votes(5, {10, 11, 12})                   # Up Next: never on a ballot
        await db.nominate("2026-11-23", 20, 3)
        await db.nominate("2026-11-23", 21, 5)                # Up Next: dropped
        await db.nominate("2026-11-23", 22, 1)
        svc = FakeWeekly(fake_bot(db))
        opts = await svc.ballot_options("2026-11-23")
        await db.close()
        return [(o["tmdb_id"], o["source"], o["interest"]) for o in opts]
    # Only nominations, in order; Film 2 (most 👍, not nominated) isn't on it.
    assert asyncio.run(run()) == [(3, "nomination", 0), (1, "nomination", 1)]


# ------------------------------------------------------------------ votes

def test_vote_emoji_and_bot_ignored(tmp_path):
    assert is_vote("👍") and is_vote("👍🏽") and not is_vote("👎")

    async def run():
        db = await seeded(tmp_path)
        bot = fake_bot(db)
        bot.config.guild_id = 1
        bot.user = SimpleNamespace(id=999)
        svc = VoteService(bot)
        ev = lambda uid, mid=201, emoji="👍", guild=1: SimpleNamespace(
            guild_id=guild, emoji=emoji, user_id=uid, message_id=mid)
        for e in (ev(10), ev(999), ev(11, emoji="👎"), ev(12, mid=555), ev(13, guild=2),
                  ev(14, emoji="👍🏿")):
            await svc.on_add(e)
        got = await db.movie_interest(1)
        await svc.on_clear(201)
        cleared = await db.movie_interest(1)
        await db.close()
        return got, cleared
    assert asyncio.run(run()) == ((2, 2, 0), (0, 0, 0))


# ------------------------------------------------------------------ follow-ups

def test_watch_start_pins_once():
    s = Schedule(date(2026, 11, 23), TZ)
    assert next_action("picked", s, local(2026, 11, 22, 23, 59)) is None
    assert next_action("picked", s, local(2026, 11, 23)) == "start_watch"
    assert next_action("picked", s, local(2026, 11, 23), pinned=True) is None
    # Down all week: just end it.
    assert next_action("picked", s, local(2026, 11, 30)) == "end_watch"


def test_override_log_line_and_scores():
    from filmbuffs.weekly import score_text
    assert log_line(date(2026, 11, 23), "Heat (1995)").endswith("Heat (1995) · picked by an admin")
    assert score_text({"interest": 1, "thumbs": 1, "listed": 0}) == "👍 1"
    assert score_text({"interest": 3, "thumbs": 2, "listed": 2}) == "**3** want it (👍 2 · 📋 2)"


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


# ------------------------------------------------------------------ ranked vote (5R)

WK = "2026-11-23"
DURING = local(2026, 11, 21, 12)       # Sat, voting open


class VotingWeekly(FakeWeekly):
    """Pins 'now' inside the vote so tests don't depend on the clock."""
    now = DURING

    async def voting_week(self, week_start, now=None):
        return await super().voting_week(week_start, now or self.now)


async def voting_setup(tmp_path):
    db = await seeded(tmp_path)
    await db.create_week(WK)
    await db.set_options(WK, [{"tmdb_id": t, "source": "top", "interest": 0, "thumbs": 0}
                              for t in (1, 2, 3)])
    await db.update_week(WK, state="voting")
    bot = fake_bot(db)
    bot.movies = FakeMoviesSvc()
    bot.get_guild = lambda gid: None
    bot.get_channel = lambda cid: None
    return db, VotingWeekly(bot)


def test_ranking_db_ops(tmp_path):
    async def run():
        db, _ = await voting_setup(tmp_path)
        for t in (2, 1, 2):                          # duplicate ignored
            await db.add_rank(WK, 10, t)
        await db.add_rank(WK, 11, 3)
        a = await db.ranking(WK, 10)
        await db.undo_rank(WK, 10)
        await db.add_rank(WK, 10, 3)
        b = await db.ranking(WK, 10)
        everyone, n = await db.all_rankings(WK), await db.voter_count(WK)
        await db.clear_ranking(WK, 10)
        c, n2 = await db.ranking(WK, 10), await db.voter_count(WK)
        await db.close()
        return a, b, everyone, n, c, n2
    a, b, everyone, n, c, n2 = asyncio.run(run())
    assert a == [2, 1] and b == [2, 3]
    assert everyone == {10: [2, 3], 11: [3]} and n == 2
    assert c == [] and n2 == 1


def test_change_ranking_and_picker(tmp_path):
    async def run():
        db, svc = await voting_setup(tmp_path)
        out = [await svc.change_ranking(WK, 10, "add", 2),
               await svc.change_ranking(WK, 10, "add", 2),       # already ranked
               await svc.change_ranking(WK, 10, "add", 4),       # not on the ballot
               await svc.change_ranking(WK, 10, "add", 1)]
        text, view = await svc.picker(WK, 10)
        select = next(c for c in view.children if type(c).__name__ == "RankSelect").item
        left = [o.value for o in select.options]
        await svc.change_ranking(WK, 10, "undo")
        mid = await db.ranking(WK, 10)
        await svc.change_ranking(WK, 10, "clear")
        empty_text, empty_view = await svc.picker(WK, 10)
        await db.update_week(WK, state="picked")
        closed = await svc.change_ranking(WK, 10, "add", 3)
        await asyncio.sleep(0)
        await db.close()
        return out, text, left, select.placeholder, mid, empty_text, empty_view, closed
    out, text, left, placeholder, mid, empty_text, empty_view, closed = asyncio.run(run())
    assert out[0] is None and "already in your ranking" in out[1]
    assert "isn't on this ballot" in out[2] and out[3] is None
    assert "`1.` Film 2 (2002)\n`2.` Film 1 (2001)" in text and "3rd choice" in text
    assert left == ["3"] and placeholder == "Pick your 3rd choice…"
    assert mid == [2]
    assert "Nothing ranked yet" in empty_text
    assert [type(c).__name__ for c in empty_view.children] == ["RankSelect"]   # no Undo/Clear
    assert "closed" in closed


def test_close_vote_picks_runoff_winner(tmp_path):
    async def run():
        db, svc = await voting_setup(tmp_path)
        for uid, ranks in ((10, [1]), (11, [1]), (12, [2, 3]), (13, [2, 3]), (14, [3, 2])):
            for t in ranks:
                await db.add_rank(WK, uid, t)
        row = await db.get_week(WK)
        await svc.close_vote(row, local(2026, 11, 22, 23, 59))
        w = dict(await db.get_week(WK))
        status = (await db.get_movie(2))["status"]
        firsts = {o["tmdb_id"]: o["poll_votes"] for o in await db.options(WK)}
        sched = await svc.schedule_text(10, local(2026, 11, 23, 9))
        await db.close()
        return w, status, firsts, svc.bot.movies.retagged, sched
    w, status, firsts, retagged, sched = asyncio.run(run())
    # Round 1: 1=2, 2=2, 3=1 → 3 out, its ballot moves to 2 → 2 wins 3–2.
    assert w["state"] == "picked" and w["winner_tmdb_id"] == 2
    assert (w["winner_votes"], w["runner_up_votes"], w["total_votes"]) == (3, 2, 5)
    assert len(json.loads(w["vote_rounds"])) == 2
    assert status == "up_next" and retagged == [2]
    assert firsts == {1: 2, 2: 2, 3: 1}
    assert "Film 2 (2002)" in sched and "won 3–2 after 2 rounds" in sched


def test_close_vote_with_no_votes_skips(tmp_path):
    async def run():
        db, svc = await voting_setup(tmp_path)
        await svc.close_vote(await db.get_week(WK), local(2026, 11, 22, 23, 59))
        w = await db.get_week(WK)
        await db.close()
        return w["state"], w["skip_reason"]
    assert asyncio.run(run()) == ("skipped", "nobody voted")


def test_admin_pick_closes_open_vote(tmp_path):
    async def run():
        db, svc = await voting_setup(tmp_path)
        await db.add_rank(WK, 10, 1)
        msg = await svc.admin_pick(1, "next", 3, local(2026, 11, 21, 12))
        w = await db.get_week(WK)
        await db.close()
        return msg, w["state"], w["total_votes"]
    msg, state, total = asyncio.run(run())
    assert "closed voting early (1 ballot kept for stats)" in msg
    assert state == "picked" and total == 1


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


def test_nominations_only_change_while_open(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        await db.create_week(WK)
        svc = FakeWeekly(fake_bot(db))
        ok = await svc.nominate(10, 1)
        swap = await svc.nominate(10, 2)
        await db.update_week(WK, state="ballot_closed")          # Thu 11:59 PM passed
        late = [await svc.nominate(10, 3), await svc.unnominate(10)]
        noms = [(n["user_id"], n["tmdb_id"]) for n in await db.nominations(WK)]
        await db.close()
        return ok, swap, late, noms
    ok, swap, late, noms = asyncio.run(run())
    assert ok.startswith("✅") and swap.startswith("✅")
    assert late[0].startswith("❌ No ballot") and late[1].startswith("You have no")
    assert noms == [(10, 2)]


def test_sole_nomination_wins_without_a_vote(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        await db.create_week(WK)
        await db.nominate(WK, 10, 3)
        bot = fake_bot(db)
        bot.movies = FakeMoviesSvc()
        bot.get_guild = lambda gid: None
        bot.get_channel = lambda cid: None
        svc = FakeWeekly(bot)
        await svc.tick(local(2026, 11, 19, 23, 59))               # Thu close
        w = dict(await db.get_week(WK))
        status = (await db.get_movie(3))["status"]
        sched = await svc.schedule_text(10, local(2026, 11, 20, 9))
        await db.close()
        return w, status, sched
    w, status, sched = asyncio.run(run())
    assert w["state"] == "picked" and w["winner_tmdb_id"] == 3 and w["total_votes"] == 0
    assert w["winner_votes"] is None and w["override_by"] is None
    assert status == "up_next"
    assert "Film 3 (2003)" in sched and "the only nomination, so no vote" in sched


def test_no_nominations_skips(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        await db.create_week(WK)
        await db.set_votes(1, {10, 11})                          # 👍 alone never makes the ballot
        bot = fake_bot(db)
        bot.get_channel = lambda cid: None
        svc = FakeWeekly(bot)
        await svc.tick(local(2026, 11, 19, 23, 59))
        w = await db.get_week(WK)
        await db.close()
        return w["state"], w["skip_reason"]
    assert asyncio.run(run()) == ("skipped", "nobody nominated a film")
