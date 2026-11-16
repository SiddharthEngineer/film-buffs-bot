"""Phases 10 and 12: nomination pools, live ballots, the rolling weekly vote and
the private groups' monthly votes."""
import asyncio
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from filmbuffs.db import Database
from filmbuffs.groups import Group
from filmbuffs.monthly import (GroupVoteService, MonthSchedule, add_months, months_to_create,
                               next_month_action)
from filmbuffs.pool import PoolService, desired_entries, place
from filmbuffs.weekly import WeeklyService

TZ = ZoneInfo("America/Chicago")
WK = "2026-11-23"
MO = "2026-12"


def local(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


class FakeMovies:
    def __init__(self):
        self.retagged = []

    async def retag(self, tmdb_id):
        self.retagged.append(tmdb_id)

    async def find_post(self, tmdb_id):
        return None


async def make(tmp_path, n=8):
    """A DB with films 1..n (backlog; 7 watched together, 8 Up Next) and a bot
    with all three services wired up but no Discord."""
    db = Database(tmp_path / "t.db")
    await db.connect()
    for tid in range(1, n + 1):
        status = {7: "watched", 8: "up_next"}.get(tid, "backlog")
        await db.insert_movie(tmdb_id=tid, thread_id=100 + tid, starter_message_id=200 + tid,
                              title=f"Film {tid}", year=2000 + tid, status=status, flags=[],
                              genre_tags=[], added_by=1, pitch=None, source="addmovie")
    osu = Group(key="osu", name="OSU", role_name="OSU", first_month=date(2026, 12, 1),
                discussion_at=time(20, 0), role_id=555)
    cfg = SimpleNamespace(tz=TZ, channels={}, guild_id=1, first_watch_week=date(2026, 11, 23),
                          tie_break=["interest", "added"], roles={}, groups={"osu": osu})
    bot = SimpleNamespace(db=db, config=cfg, movies=FakeMovies(),
                          get_channel=lambda cid: None, get_guild=lambda gid: None)
    bot.pool = PoolService(bot)
    bot.weekly = WeeklyService(bot)
    bot.group_votes = {"osu": GroupVoteService(bot, osu)}
    bot.osu = bot.group_votes["osu"]
    return db, bot


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def row(tid, user, rank, status="backlog", at="2026-11-13T00:00:00+00:00", title="x"):
    return {"tmdb_id": tid, "user_id": user, "rank": rank, "status": status,
            "nominated_at": at, "title": title}


# ------------------------------------------------------------------ pure helpers

def test_desired_entries_takes_each_members_best_eligible():
    pool = [row(1, 10, 1, status="up_next"), row(2, 10, 2), row(3, 10, 3),
            row(4, 11, 1, title=None), row(5, 11, 2), row(6, 12, 1, status="watched")]
    add, remove, owners = desired_entries(pool, [])
    assert add == [(2, 10), (5, 11)] and remove == [] and owners == []
    # Already on the ballot: kept in place; gone ones removed.
    add, remove, owners = desired_entries(pool, [(5, 11), (9, 13)])
    assert add == [(2, 10)] and remove == [9]


def test_desired_entries_caps_and_keeps_existing_first():
    pool = [row(i, i, 1, at=f"2026-11-13T00:00:{i:02d}+00:00") for i in range(1, 31)]
    current = [(30, 30)]
    add, _, _ = desired_entries(pool, current)
    assert len(add) == 24 and (30, 30) not in add and add[0] == (1, 1)


def test_place_rules():
    assert place([], 5, None) == ([5], None, None)
    assert place([1, 2, 3], 5, None) == (None, None, "full")
    assert place([1, 2], 5, 1) == ([5, 1, 2], None, None)
    assert place([1, 2], 5, 3) == ([1, 2, 5], None, None)
    assert place([1], 5, 3) == ([1, 5], None, None)                    # no gaps
    assert place([1, 2, 3], 5, 2) == ([1, 5, 3], 2, None)              # full: replaces #2
    assert place([1, 2, 3], 3, 1) == ([3, 1, 2], None, None)           # move
    assert place([1, 2, 3], 2, 2)[2] == "same"
    assert place([1, 2, 3], 2, None)[2] == "same"


# ------------------------------------------------------------------ pool + ballot

def test_nominate_rules_and_ballot(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        p = bot.pool
        out = [await p.nominate(10, 1), await p.nominate(10, 2), await p.nominate(10, 3),
               await p.nominate(10, 4),                 # full
               await p.nominate(10, 4, rank=1),         # replaces #1
               await p.nominate(11, 2),                 # someone else's
               await p.nominate(11, 7),                 # watched together
               await p.nominate(11, 8),                 # up next
               await p.nominate(11, 5),
               await p.nominate(10, 3, rank=1)]         # reorder
        mine = [r["tmdb_id"] for r in await db.pool(10)]
        ballot = [(e["tmdb_id"], e["user_id"]) for e in await db.ballot_entries()]
        out.append(await p.unnominate(10, 3))
        out.append(await p.unnominate(10, 99))
        ballot2 = [(e["tmdb_id"], e["user_id"]) for e in await db.ballot_entries()]
        await settle()
        await db.close()
        return out, mine, ballot, ballot2
    out, mine, ballot, ballot2 = asyncio.run(run())
    assert out[0].startswith("✅ Nominated **Film 1 (2001)** to your #1")
    assert "already have 3" in out[3]
    assert "replacing **Film 1 (2001)**" in out[4]
    assert "already nominated by <@10>" in out[5]
    assert "watched together" in out[6] and "Up Next" in out[7]
    assert out[9].startswith("✅ Moved **Film 3 (2003)** to your #1")
    assert mine == [3, 4, 2]
    assert ballot == [(5, 11), (3, 10)] or ballot == [(3, 10), (5, 11)]
    assert "Withdrew **Film 3" in out[10] and "isn't one of your" in out[11]
    assert sorted(ballot2) == [(4, 10), (5, 11)]          # Film 4 moved up onto the ballot


def test_leaving_film_drops_from_that_tracks_rankings_only(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        await bot.pool.nominate(10, 1)
        await bot.pool.nominate(10, 2)
        await bot.pool.nominate(11, 3)
        await bot.pool.nominate(12, 4)
        await bot.osu.pool.nominate(10, 1)                 # same film in the OSU pool
        await bot.osu.pool.nominate(11, 4)
        await db.set_track_ranking("weekly", 20, [1, 3, 4])
        await db.set_track_ranking("group:osu", 20, [4, 1])
        await bot.pool.unnominate(10, 1)                   # Film 2 moves up, Film 1 leaves main
        w, m = await db.track_ranking("weekly", 20), await db.track_ranking("group:osu", 20)
        ballot = sorted(e["tmdb_id"] for e in await db.ballot_entries())
        osu = sorted(e["tmdb_id"] for e in await db.ballot_entries("osu"))
        await settle()
        await db.close()
        return w, m, ballot, osu
    w, m, ballot, osu = asyncio.run(run())
    assert w == [3, 4] and ballot == [2, 3, 4]
    assert m == [4, 1] and osu == [1, 4]                   # the OSU vote is untouched


# ------------------------------------------------------------------ weekly vote

async def open_week(db, bot):
    await db.create_week(WK)
    for uid, films in ((10, [1, 2]), (11, [3]), (12, [4])):
        for t in films:
            await bot.pool.nominate(uid, t)


def test_weekly_close_runs_irv_and_rankings_carry_over(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        await open_week(db, bot)                           # ballot: 1, 3, 4
        for uid, ranks in ((20, [1]), (21, [1]), (22, [3, 4]), (23, [3, 4]), (24, [4, 3])):
            await db.set_track_ranking("weekly", uid, ranks)
        picker_text, _ = \
            await bot.weekly.picker(WK, 22)
        await bot.weekly.close_vote(await db.get_week(WK), local(2026, 11, 22, 23, 59))
        w = dict(await db.get_week(WK))
        after = {u: await db.track_ranking("weekly", u) for u in (20, 22, 24)}
        snap = await db.all_rankings(WK)
        ballot = sorted(e["tmdb_id"] for e in await db.ballot_entries())
        pool11 = await db.pool(11)
        status = (await db.get_movie(3))["status"]
        await settle()
        await db.close()
        return picker_text, w, after, snap, ballot, pool11, status
    picker_text, w, after, snap, ballot, pool11, status = asyncio.run(run())
    assert "Your weekly ranking · watch week Nov 23–29" in picker_text
    # Round 1: 1=2, 3=2, 4=1 → 4 out, its ballot moves to 3 → 3 wins 3–2.
    assert w["state"] == "picked" and w["winner_tmdb_id"] == 3
    assert (w["winner_votes"], w["runner_up_votes"], w["total_votes"]) == (3, 2, 5)
    assert status == "up_next" and pool11 == []
    assert after == {20: [1], 22: [4], 24: [4]}           # winner dropped, rest carried over
    assert snap[22] == [3, 4]                              # snapshot kept for stats
    assert ballot == [1, 4]


def test_group_pool_is_independent(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        p = bot.osu.pool
        out = [await p.nominate(10, 7),                    # watched together: fine for a group
               await p.nominate(11, 8),                    # Up Next on the weekly: fine too
               await bot.pool.nominate(12, 1),
               await p.nominate(13, 1),                    # in the main pool too: fine
               await p.nominate(13, 2), await p.nominate(13, 3),
               await p.nominate(13, 4)]                    # full (3 per pool)
        main = sorted(e["tmdb_id"] for e in await db.ballot_entries())
        osu = sorted(e["tmdb_id"] for e in await db.ballot_entries("osu"))
        await settle()
        await db.close()
        return out, main, osu
    out, main, osu = asyncio.run(run())
    assert out[0].startswith("✅ Nominated **Film 7 (2007)** to your #1 for OSU")
    assert out[1].startswith("✅") and out[3].startswith("✅")
    assert "already have 3 nominations for OSU" in out[6]
    assert main == [1] and osu == [1, 7, 8]


def test_weekly_close_ignores_groups(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        await open_week(db, bot)                           # ballot: 1, 3, 4
        await db.create_month("osu", MO)
        await bot.osu.pool.nominate(30, 3)
        await db.set_track_ranking("group:osu", 30, [3])   # 3 leads the OSU vote
        for uid, ranks in ((20, [3, 1]), (21, [3, 1]), (22, [4, 1]), (23, [1])):
            await db.set_track_ranking("weekly", uid, ranks)
        await bot.weekly.close_vote(await db.get_week(WK), local(2026, 11, 22, 23, 59))
        w = dict(await db.get_week(WK))
        osu = [e["tmdb_id"] for e in await db.ballot_entries("osu")]
        await settle()
        await db.close()
        return w, osu
    w, osu = asyncio.run(run())
    # Film 3 leads OSU's vote, but the weekly count no longer sets it aside.
    assert w["winner_tmdb_id"] in (1, 3) and w["excluded_tmdb_id"] is None
    assert osu == [3]                                     # still on OSU's ballot


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw.get("allowed_mentions")))
        return SimpleNamespace(id=900 + len(self.sent))


def test_weekly_pick_is_announced_in_watch_planning(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        planning, ann = FakeChannel(), FakeChannel()
        chans = {1: planning, 2: ann}
        bot.config.channels = {"watch_planning": 1, "announcements": 2}
        bot.config.roles = {"weekly_ping": 77}
        bot.get_channel = chans.get
        bot.weekly.channel = lambda key: chans.get(bot.config.channels.get(key))
        bot.weekly.refresh_ballot = lambda week: asyncio.sleep(0)
        await open_week(db, bot)
        await db.set_track_ranking("weekly", 20, [3])
        await bot.weekly.close_vote(await db.get_week(WK), local(2026, 11, 22, 23, 59))
        w = dict(await db.get_week(WK))
        await settle()
        await db.close()
        return planning.sent, ann.sent, w
    planning, ann, w = asyncio.run(run())
    assert ann == []
    assert len(planning) == 1 and planning[0][0].startswith("<@&77> 🎬 **Weekly movie · Nov 23–29")
    assert "Winner of the ranked vote" in planning[0][0]
    assert [r.id for r in planning[0][1].roles] == [77] and w["announcement_id"] == 901


def test_weekly_close_edge_cases(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        await db.create_week(WK)
        await bot.weekly.close_vote(await db.get_week(WK), local(2026, 11, 22, 23, 59))
        empty = dict(await db.get_week(WK))
        await db.create_week("2026-11-30")
        await bot.pool.nominate(10, 2)
        await bot.weekly.close_vote(await db.get_week("2026-11-30"), local(2026, 11, 29, 23, 59))
        sole = dict(await db.get_week("2026-11-30"))
        await db.create_week("2026-12-07")
        await bot.pool.nominate(10, 1)
        await bot.pool.nominate(11, 3)
        await bot.weekly.close_vote(await db.get_week("2026-12-07"), local(2026, 12, 6, 23, 59))
        novotes = dict(await db.get_week("2026-12-07"))
        await settle()
        await db.close()
        return empty, sole, novotes
    empty, sole, novotes = asyncio.run(run())
    assert empty["state"] == "skipped" and empty["skip_reason"] == "nobody nominated a film"
    assert sole["state"] == "picked" and sole["winner_tmdb_id"] == 2 and sole["winner_votes"] is None
    assert novotes["state"] == "skipped" and novotes["skip_reason"] == "nobody voted"


def test_picker_marks_new_films_and_closes(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        await open_week(db, bot)
        svc = bot.weekly
        svc_now = local(2026, 11, 20, 12)
        orig = svc.voting_week
        svc.voting_week = lambda ws, now=None: orig(ws, now or svc_now)
        out = [await svc.change_ranking(WK, 20, "add", 1),
               await svc.change_ranking(WK, 20, "add", 1),
               await svc.change_ranking(WK, 20, "add", 2)]       # not on the ballot (backup)
        await db.conn.execute("UPDATE track_rankings SET voted_at='2026-01-01T00:00:00+00:00'")
        await db.conn.commit()
        text, view = await svc.picker(WK, 20)
        select = next(c for c in view.children if type(c).__name__ == "RankSelect").item
        emojis = {o.value: str(o.emoji) if o.emoji else None for o in select.options}
        await db.update_week(WK, state="picked")
        closed = await svc.change_ranking(WK, 20, "add", 3)
        await settle()
        await db.close()
        return out, text, emojis, closed
    out, text, emojis, closed = asyncio.run(run())
    assert out[0] is None and "already in your ranking" in out[1]
    assert "isn't on the ballot" in out[2]
    assert "🆕 2 films joined the ballot since you last ranked" in text
    assert emojis == {"3": "🆕", "4": "🆕"}
    assert "closed" in closed


# ------------------------------------------------------------------ monthly

def test_month_schedule():
    s = MonthSchedule(date(2026, 12, 1), TZ)
    assert s.vote_open == local(2026, 11, 1)
    assert s.vote_close == local(2026, 11, 30, 23, 59)
    assert s.watch_start == local(2026, 12, 1)
    assert s.watch_end == local(2026, 12, 31, 23, 59)
    assert s.discussion == local(2027, 1, 1, 20, 0)
    assert add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert add_months(date(2027, 1, 1), -1) == date(2026, 12, 1)
    first = date(2026, 12, 1)
    assert months_to_create(local(2026, 11, 13, 16), TZ, first, set()) == [first]
    assert months_to_create(local(2026, 11, 13, 16), TZ, first, {"2026-12"}) == []
    assert months_to_create(local(2026, 12, 1, 0, 0), TZ, first, {"2026-12"}) == [date(2027, 1, 1)]
    assert months_to_create(local(2026, 10, 13), TZ, first, set()) == []    # before the first
    assert months_to_create(local(2026, 10, 13), TZ, None, set()) == [date(2026, 11, 1)]
    assert next_month_action("voting", s, local(2026, 11, 30, 23, 58)) is None
    assert next_month_action("voting", s, local(2026, 11, 30, 23, 59)) == "close_vote"
    assert next_month_action("picked", s, local(2026, 12, 31, 23, 59)) == "end_month"


def test_group_close_crowns_without_touching_main(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        await open_week(db, bot)                           # main ballot: 1, 3, 4
        for uid, t in ((10, 1), (11, 4), (12, 7)):
            await bot.osu.pool.nominate(uid, t)            # OSU ballot: 1, 4, 7
        await db.create_month("osu", MO)
        for uid, ranks in ((30, [4]), (31, [4, 1]), (32, [1])):
            await db.set_track_ranking("group:osu", uid, ranks)
        await db.set_track_ranking("weekly", 20, [4, 1])
        await bot.osu.close_vote(await db.get_month("osu", MO), local(2026, 11, 30, 23, 59))
        m = dict(await db.get_month("osu", MO))
        weekly = await db.track_ranking("weekly", 20)
        main = sorted(e["tmdb_id"] for e in await db.ballot_entries())
        osu = sorted(e["tmdb_id"] for e in await db.ballot_entries("osu"))
        status = (await db.get_movie(4))["status"]
        opts = [(o["tmdb_id"], o["first_choices"]) for o in await db.month_options("osu", MO)]
        again = await bot.osu.pool.nominate(11, 4)
        await bot.osu.end_month(await db.get_month("osu", MO), local(2026, 12, 31, 23, 59))
        done = dict(await db.get_month("osu", MO))
        status2 = (await db.get_movie(4))["status"]
        await settle()
        await db.close()
        return m, weekly, main, osu, status, opts, again, done, status2, bot.movies.retagged
    m, weekly, main, osu, status, opts, again, done, status2, retagged = asyncio.run(run())
    assert m["state"] == "picked" and m["winner_tmdb_id"] == 4 and m["winner_votes"] == 2
    assert osu == [1, 7] and main == [1, 3, 4] and weekly == [4, 1]
    assert status == "backlog" and status2 == "backlog" and 4 not in retagged
    assert dict(opts) == {1: 1, 4: 2, 7: 0}
    assert "already picked **Film 4 (2004)** (December 2026)" in again
    assert done["state"] == "done"


def test_tick_opens_group_month_now_and_week_on_monday(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        await bot.weekly.tick(local(2026, 11, 13, 16))
        months = await db.all_months("osu")
        weeks = [r["week_start"] for r in await db.weeks_in_state("voting")]
        await bot.weekly.tick(local(2026, 11, 16, 0, 0))
        weeks2 = [r["week_start"] for r in await db.weeks_in_state("voting")]
        await bot.weekly.tick(local(2026, 11, 30, 23, 59))      # Dec vote closes: nobody nominated
        dec = (await db.get_month("osu", MO))["state"]
        await bot.weekly.tick(local(2026, 12, 1, 0, 0))
        months2 = await db.all_months("osu")
        main_months = await db.all_months("main")
        await settle()
        await db.close()
        return months, weeks, weeks2, dec, months2, main_months
    months, weeks, weeks2, dec, months2, main_months = asyncio.run(run())
    assert months == {"2026-12"} and weeks == []
    assert weeks2 == [WK]
    assert dec == "skipped"
    assert months2 == {"2026-12", "2027-01"} and main_months == set()


def test_group_admin_pick_and_clear(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        now = local(2026, 11, 20, 12)
        await bot.osu.pool.nominate(10, 1)
        await db.create_month("osu", MO)
        out = [await bot.osu.admin_pick(1, "next", 1, now),
               await bot.osu.admin_pick(1, "next", 8, now)]          # Up Next on the weekly: ok
        m = dict(await db.get_month("osu", MO))
        pool = await db.pool(10, "osu")
        out.append(await bot.osu.admin_clear(1, "next", now))
        status = (await db.get_movie(8))["status"]
        out.append(await bot.osu.admin_clear(1, "next", now))
        out.append(await bot.osu.pool.nominate(10, 8))               # cleared: allowed again
        await settle()
        await db.close()
        return out, m, pool, status
    out, m, pool, status = asyncio.run(run())
    assert out[0].startswith("✅ **Film 1 (2001)** is OSU's movie for December 2026")
    assert "closed voting early" in out[0]
    assert out[1].startswith("✅ **Film 8 (2008)** is OSU's movie")
    assert m["state"] == "picked" and m["winner_tmdb_id"] == 8 and m["override_by"] == 1
    assert pool == []
    assert out[2].startswith("✅ Cleared **Film 8 (2008)** for OSU, December 2026")
    assert status == "up_next" and out[3].startswith("❌") and out[4].startswith("✅")


def test_vote_ids_carry_the_group():
    from filmbuffs.vote_ui import MyNominationsButton, NominationButton, VoteButton, split_vote
    assert split_vote("osu:2026-12") == ("osu", "2026-12")
    assert split_vote("2026-11-23") == (None, "2026-11-23")
    assert VoteButton("osu:2026-12").item.custom_id == "vote:osu:2026-12"
    assert MyNominationsButton("osu").item.custom_id == "noms:osu:show"
    assert MyNominationsButton().item.custom_id == "noms:show"
    assert NominationButton("up", 2, "osu").item.custom_id == "noms:osu:up:2"
    from filmbuffs.vote_ui import PingButton, ballot_view
    assert PingButton().item.custom_id == "pings:main"
    assert [c.custom_id for c in ballot_view("osu:2026-12", "osu").children] == [
        "vote:osu:2026-12", "noms:osu:show", "pings:osu"]
    for cls, cid, want in ((VoteButton, "vote:osu:2026-12", "osu"),
                           (VoteButton, "vote:2026-11-23", None),
                           (NominationButton, "noms:up:2", None),
                           (NominationButton, "noms:osu:del:1", "osu"),
                           (MyNominationsButton, "noms:show", None)):
        m = cls.__discord_ui_compiled_template__.fullmatch(cid)
        assert m is not None and m["grp"] == want, cid


# ------------------------------------------------------------------ migration / config

def test_migration_moves_open_5r_ballot_into_the_pool(tmp_path):
    async def run():
        db = Database(tmp_path / "t.db")
        await db.connect()
        for t in ("pool_nominations", "ballot_entries", "track_rankings", "months",
                  "month_options", "month_votes"):
            await db.conn.execute(f"DROP TABLE {t}")
        await db.conn.execute("ALTER TABLE weeks DROP COLUMN excluded_tmdb_id")
        # Undo migration 12 too (it drops these again).
        await db.conn.execute("CREATE TABLE votes (tmdb_id INTEGER)")
        await db.conn.execute("ALTER TABLE week_options ADD COLUMN thumbs INTEGER")
        await db.conn.execute("ALTER TABLE movies DROP COLUMN poster_url")   # migration 14
        await db.conn.execute("DELETE FROM schema_version WHERE version >= 11")
        await db.conn.execute("INSERT INTO weeks(week_start, state) VALUES ('2026-11-23', 'ballot')")
        await db.conn.execute("INSERT INTO weeks(week_start, state) VALUES ('2026-11-16', 'picked')")
        await db.nominate("2026-11-23", 10, 5)
        await db.nominate("2026-11-16", 11, 6)
        await db.conn.commit()
        await db.close()
        db = Database(tmp_path / "t.db")
        await db.connect()
        pool = [(r["user_id"], r["rank"], r["tmdb_id"]) for r in await db.pool()]
        states = {r["week_start"]: r["state"] for r in await db.weeks_in_state("voting", "picked")}
        await db.close()
        return pool, states
    pool, states = asyncio.run(run())
    assert pool == [(10, 1, 5)]
    assert states == {"2026-11-16": "picked", "2026-11-23": "voting"}


def test_migration_13_moves_monthly_into_groups(tmp_path):
    async def run():
        db = Database(tmp_path / "t.db")
        await db.connect()
        # Back to schema 12 (Phase 10 tables, no grp columns).
        for t in ("pool_nominations", "ballot_entries", "months", "month_options", "month_votes"):
            await db.conn.execute(f"DROP TABLE {t}")
        await db.conn.executescript("""
            CREATE TABLE pool_nominations (user_id INTEGER, rank INTEGER, tmdb_id INTEGER UNIQUE,
                nominated_at TEXT, PRIMARY KEY (user_id, rank));
            CREATE TABLE ballot_entries (tmdb_id INTEGER PRIMARY KEY, user_id INTEGER,
                joined_at TEXT);
            CREATE TABLE months (month TEXT PRIMARY KEY, state TEXT, ballot_message_id INTEGER);
            CREATE TABLE month_options (month TEXT);
            CREATE TABLE month_votes (month TEXT);
            INSERT INTO pool_nominations VALUES (10, 1, 5, '2026-11-13T00:00:00+00:00');
            INSERT INTO ballot_entries VALUES (5, 10, '2026-11-13T00:00:00+00:00');
            INSERT INTO months VALUES ('2026-12', 'voting', 777);
            INSERT INTO track_rankings VALUES ('monthly', 20, 1, 5, 'x'), ('weekly', 20, 1, 5, 'x');
            ALTER TABLE movies DROP COLUMN poster_url;   -- migration 14
            DELETE FROM schema_version WHERE version >= 13;
        """)
        await db.conn.commit()
        await db.close()
        db = Database(tmp_path / "t.db")
        await db.connect()
        pool = [(r["grp"], r["user_id"], r["tmdb_id"]) for r in await db.pool()]
        ballot = [(e["grp"], e["tmdb_id"]) for e in await db.ballot_entries()]
        tracks = [r[0] for r in await (await db.conn.execute(
            "SELECT track FROM track_rankings")).fetchall()]
        months = await (await db.conn.execute("SELECT COUNT(*) FROM months")).fetchone()
        meta = await db.get_meta("retired_monthly_ballots")
        await db.close()
        return pool, ballot, tracks, months[0], meta
    pool, ballot, tracks, months, meta = asyncio.run(run())
    assert pool == [("main", 10, 5)] and ballot == [("main", 5)]
    assert tracks == ["weekly"] and months == 0 and meta == "777"


def test_server_yaml_roles_groups_and_buttons(tmp_path):
    import setup_server
    cfg = setup_server.load_config("server.yaml")
    assert {(r["key"], r["name"]) for r in cfg["roles"]} == {
        ("weekly_ping", "Weekly Pings"), ("group_osu", "OSU"), ("group_osu_ping", "OSU Pings")}
    assert [r["key"] for r in cfg["retire_roles"]] == ["monthly_ping"]
    assert all(not m.get("buttons") for m in cfg["messages"])   # 🔔 Pings lives on the ballots
    text = " ".join(m.get("description", "") + " ".join(m.get("sections") or [])
                    for m in cfg["messages"])
    assert "OSU" not in text                                     # no group is named publicly
    cats = [c["key"] for c in cfg["categories"]]
    assert cats.index("group_osu") == cats.index("admin") - 1
    osu = next(c for c in cfg["categories"] if c["key"] == "group_osu")
    assert [(c["key"], c["name"], c.get("type", "text")) for c in osu["channels"]] == [
        ("osu_watch_planning", "watch-planning", "text"), ("osu_chat", "chat", "text"),
        ("osu_voice", "voice", "voice")]
    for c in osu["channels"]:
        ows = c["_overwrites"]
        assert ows["@everyone"] == {"deny": ["view_channel"]}
        assert "view_channel" in ows["role:group_osu"]["allow"]
        assert "view_channel" in ows["@bot"]["allow"]
    bad = tmp_path / "s.yaml"
    bad.write_text(Path("server.yaml").read_text().replace("- key: osu", "- key: main", 1))
    with pytest.raises(setup_server.ConfigError, match="group key 'main'"):
        setup_server.load_config(bad)
