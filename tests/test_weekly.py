"""Phase 5: votes, watchlists, ballot, poll winner, weekly schedule."""
import asyncio
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from filmbuffs.db import Database
from filmbuffs.votes import VoteService, is_vote
from filmbuffs.watchlist import WatchlistError, parse_watchlist
from filmbuffs.weekly import (Schedule, WeeklyService, answer_label, compose_ballot, date_range,
                              log_line, next_action, pick_winner, weeks_to_create, when)

TZ = ZoneInfo("America/Chicago")


def local(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


# ------------------------------------------------------------------ calendar

def test_schedule_deadlines():
    s = Schedule(date(2026, 11, 23), TZ)
    assert s.ballot_open == local(2026, 11, 9)
    assert s.ballot_close == local(2026, 11, 15, 23, 59)
    assert s.poll_open == local(2026, 11, 16)
    assert s.poll_close == local(2026, 11, 22, 23, 59)
    assert s.watch_start == local(2026, 11, 23)
    assert s.watch_end == local(2026, 11, 29, 23, 59)


def test_schedule_across_dst():
    # DST starts Sun Mar 14 2027: the poll week is an hour shorter, deadlines stay local.
    s = Schedule(date(2027, 3, 15), TZ)
    assert s.poll_close == local(2027, 3, 14, 23, 59)
    assert s.poll_close.utcoffset() == timedelta(hours=-5)
    assert s.poll_open.utcoffset() == timedelta(hours=-6)


def test_bootstrap_opens_only_first_ballot():
    now = local(2026, 11, 11, 15)
    assert weeks_to_create(now, TZ, date(2026, 11, 23), set()) == [date(2026, 11, 23)]
    assert weeks_to_create(now, TZ, date(2026, 11, 23), {date(2026, 11, 23)}) == []
    assert weeks_to_create(now, TZ, None, set()) == []


def test_steady_state_creates_week_after_next_on_monday():
    existing = {date(2026, 11, 23), date(2026, 11, 30)}
    now = local(2026, 11, 23, 0, 0)
    assert weeks_to_create(now, TZ, date(2026, 11, 23), existing) == [date(2026, 12, 7)]


def test_outage_never_starts_a_week_whose_poll_is_over():
    # First watch week already started: too late for its poll.
    now = local(2026, 11, 24, 12)
    assert date(2026, 11, 23) not in weeks_to_create(now, TZ, date(2026, 11, 23), set())


def test_next_action_sequence():
    s = Schedule(date(2026, 11, 23), TZ)
    assert next_action("ballot", s, local(2026, 11, 15, 23, 58)) is None
    assert next_action("ballot", s, local(2026, 11, 15, 23, 59)) == "close_ballot"
    assert next_action("ballot_closed", s, local(2026, 11, 15, 23, 59)) is None
    assert next_action("ballot_closed", s, local(2026, 11, 16)) == "open_poll"
    assert next_action("poll", s, local(2026, 11, 22, 23, 59)) == "close_poll"
    assert next_action("picked", s, local(2026, 11, 28), pinned=True) is None
    assert next_action("picked", s, local(2026, 11, 29, 23, 59)) == "end_watch"
    assert next_action("done", s, local(2027, 1, 1)) is None


def test_formatting():
    assert date_range(date(2026, 11, 23)) == "Nov 23–29"
    assert date_range(date(2026, 11, 30)) == "Nov 30–Dec 6"
    assert when(local(2026, 11, 15, 23, 59), TZ) == "Sun Nov 15, 11:59 PM CT"
    assert when(local(2026, 11, 16), TZ) == "Mon Nov 16, 12:00 AM CT"
    assert log_line(date(2026, 11, 23), "Parasite (2019)", 5, 3) == \
        "Wk 48 · Nov 23–29 2026 · Parasite (2019) · won 5–3"
    assert log_line(date(2026, 11, 23), None, skipped="nobody voted").endswith("no pick (nobody voted)")
    long = {"title": "x" * 80, "year": 2001}
    assert len(answer_label(long)) <= 55 and answer_label(long).endswith(" (2001)")


# ------------------------------------------------------------------ ballot / winner

def mv(tid, interest=0, thumbs=0, status="backlog"):
    return {"tmdb_id": tid, "interest": interest, "thumbs": thumbs, "status": status}


def nom(tid, user, status="backlog"):
    return {"tmdb_id": tid, "user_id": user, "status": status}


def test_compose_ballot_nominations_first_then_most_wanted():
    ranked = [mv(1, 5, 5), mv(2, 4, 3), mv(3, 3), mv(4, 0), mv(5, 9, status="watched")]
    opts = compose_ballot([nom(3, 70), nom(9, 71)], ranked, 4)
    assert [(o["tmdb_id"], o["source"]) for o in opts] == \
        [(3, "nomination"), (9, "nomination"), (1, "top"), (2, "top")]
    assert opts[0]["nominated_by"] == 70 and opts[0]["interest"] == 3


def test_compose_ballot_drops_ineligible_and_zero_interest():
    ranked = [mv(1, 2), mv(2, 0)]
    opts = compose_ballot([nom(7, 70, status="up_next")], ranked, 10)
    assert [o["tmdb_id"] for o in opts] == [1]


def test_compose_ballot_caps_nominations_at_size():
    opts = compose_ballot([nom(i, i) for i in range(12)], [], 10)
    assert len(opts) == 10 and all(o["source"] == "nomination" for o in opts)


def res(tid, votes, interest=0, thumbs=0, added="2026-01-01"):
    return {"tmdb_id": tid, "votes": votes, "interest": interest, "thumbs": thumbs, "added_at": added}


def test_pick_winner_and_tie_breaks():
    order = ["interest", "thumbs", "added"]
    assert pick_winner([res(1, 0), res(2, 0)], order) is None
    assert pick_winner([], order) is None
    w, ru = pick_winner([res(1, 3), res(2, 5), res(3, 1)], order)
    assert (w["tmdb_id"], ru) == (2, 3)
    w, ru = pick_winner([res(1, 4, interest=2), res(2, 4, interest=3)], order)
    assert (w["tmdb_id"], ru) == (2, 4)
    w, _ = pick_winner([res(1, 4, 3, 1), res(2, 4, 3, 2)], order)
    assert w["tmdb_id"] == 2
    w, _ = pick_winner([res(1, 4, added="2026-02-01"), res(2, 4, added="2026-01-01")], order)
    assert w["tmdb_id"] == 2
    w, ru = pick_winner([res(1, 2)], order)
    assert (w["tmdb_id"], ru) == (1, 0)


# ------------------------------------------------------------------ watchlist files

CSV = "Date,Name,Year,Letterboxd URI\n2024-01-01,Parasite,2019,https://boxd.it/a\n" \
      "2024-01-02,Heat,1995,https://boxd.it/b\n2024-01-02,Heat,1995,https://boxd.it/b\n"


def test_parse_watchlist_csv_and_zip():
    assert parse_watchlist(("﻿" + CSV).encode(), "watchlist.csv") == \
        [("Parasite", 2019), ("Heat", 1995)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("deleted/watchlist.csv", "Date,Name,Year\n2024-01-01,Gone,2000\n")
        z.writestr("watchlist.csv", CSV)
        z.writestr("diary.csv", "x")
    assert parse_watchlist(buf.getvalue(), "letterboxd-me.zip") == [("Parasite", 2019), ("Heat", 1995)]


def test_parse_watchlist_rejects_other_files():
    for data, name in ((b"a,b\n1,2\n", "x.csv"), (b"PKnotazip", "x.zip")):
        try:
            parse_watchlist(data, name)
        except WatchlistError:
            pass
        else:
            raise AssertionError(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("diary.csv", "x")
    try:
        parse_watchlist(buf.getvalue(), "e.zip")
    except WatchlistError as e:
        assert "watchlist.csv" in str(e)


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


def fake_bot(db, poll_options=3):
    cfg = SimpleNamespace(tz=TZ, poll_options=poll_options, channels={}, guild_id=1,
                          first_watch_week=date(2026, 11, 23))
    return SimpleNamespace(db=db, config=cfg)


def test_nominate_rules(tmp_path):
    async def run():
        db = await seeded(tmp_path)
        svc = FakeWeekly(fake_bot(db, poll_options=2))
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
        svc = FakeWeekly(fake_bot(db, poll_options=3))
        opts = await svc.ballot_options("2026-11-23")
        await db.close()
        return [(o["tmdb_id"], o["source"]) for o in opts]
    assert asyncio.run(run()) == [(3, "nomination"), (2, "top"), (1, "top")]


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
