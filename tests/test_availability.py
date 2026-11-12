import asyncio
import json
from datetime import date
from types import SimpleNamespace

import discord

from filmbuffs.availability import (FIELD_NAME, JUSTWATCH_CREDIT, Availability,
                                    AvailabilityService, card_line, check_movies, classify,
                                    merge_flags)
from filmbuffs.db import Database
from filmbuffs.movies import REMINDER_NAME, VOTE_FOOTER, build_embed, set_availability
from filmbuffs.tmdb import Movie, TMDBError

TODAY = date(2026, 11, 12)


def rel(day: str, type_: int) -> dict:
    return {"release_date": f"{day}T00:00:00.000Z", "type": type_, "certification": "",
            "note": "", "iso_639_1": "", "descriptors": []}


def tmdb(us_releases=(), other=None, providers=None) -> dict:
    results = [{"iso_3166_1": "US", "release_dates": list(us_releases)}]
    if other:
        results.append({"iso_3166_1": "GB", "release_dates": other})
    return {"id": 1, "release_dates": {"results": results},
            "watch/providers": {"results": providers or {}}}


def prov(name: str, prio: int) -> dict:
    return {"provider_name": name, "display_priority": prio, "provider_id": prio,
            "logo_path": "/x.png"}


# ------------------------------------------------------------------ In Theaters

def test_wide_release_within_window_is_in_theaters():
    av = classify(tmdb([rel("2026-10-01", 3)]), TODAY)
    assert av.in_theaters and not av.streaming_tag and av.first_release == "2026-10-01"
    assert av.flags == ["in_theaters"]


def test_limited_release_counts_and_later_wide_release_is_same_run():
    av = classify(tmdb([rel("2026-10-20", 2), rel("2026-11-01", 3)]), TODAY)
    assert av.in_theaters and av.first_release == "2026-10-20"


def test_window_edges():
    assert classify(tmdb([rel("2026-11-12", 3)]), TODAY).in_theaters          # opens today
    assert classify(tmdb([rel("2026-09-13", 3)]), TODAY).in_theaters          # 60 days ago
    assert not classify(tmdb([rel("2026-09-12", 3)]), TODAY).in_theaters      # 61 days ago


def test_future_theatrical_release_is_not_yet_in_theaters():
    assert classify(tmdb([rel("2026-11-13", 3)]), TODAY) == Availability()


def test_re_releases_are_ignored():
    # Rocky: 1976 run, 50th-anniversary re-release in the window, no digital date on TMDB.
    av = classify(tmdb([rel("1976-11-20", 1), rel("1976-12-03", 3), rel("2026-11-07", 3)]), TODAY)
    assert av.flags == [] and av.first_release == "1976-12-03"


def test_old_film_with_only_restoration_dates_is_not_new():
    data = tmdb([rel("2026-10-01", 3), rel("2026-11-10", 4)])
    data["release_date"] = "1975-08-15"
    assert classify(data, TODAY).flags == []
    data["release_date"] = "2026-05-01"     # premiered abroad first, still recent
    assert classify(data, TODAY).flags == ["streaming"]


def test_festival_premiere_does_not_start_the_clock():
    av = classify(tmdb([rel("2025-09-01", 1), rel("2026-10-01", 3)]), TODAY)
    assert av.in_theaters


def test_digital_release_moves_it_to_streaming():
    av = classify(tmdb([rel("2026-10-01", 3), rel("2026-11-10", 4)]), TODAY)
    assert not av.in_theaters and av.streaming_tag and av.digital == "2026-11-10"
    assert av.flags == ["streaming"]


def test_rent_or_buy_means_its_out_of_theaters():
    us = {"rent": [prov("Amazon Video", 11)]}
    av = classify(tmdb([rel("2026-10-01", 3)], providers={"US": us}), TODAY)
    assert av.flags == ["streaming"] and av.streaming == []


def test_scheduled_digital_release_does_not_count_yet():
    av = classify(tmdb([rel("2026-10-01", 3), rel("2026-11-20", 4)]), TODAY)
    assert av.in_theaters and av.digital is None


def test_left_theaters_but_not_at_home_yet_gets_neither():
    assert classify(tmdb([rel("2026-08-01", 3)]), TODAY).flags == []


def test_premiere_physical_tv_and_other_regions_ignored():
    data = tmdb([rel("2026-11-01", 1), rel("2026-11-01", 5), rel("2026-11-01", 6)],
                other=[rel("2026-11-01", 3)])
    assert classify(data, TODAY) == Availability()


def test_missing_data_is_empty():
    for data in ({}, {"release_dates": None, "watch/providers": None},
                 {"release_dates": {"results": []}, "watch/providers": {"results": {}}},
                 tmdb([{"release_date": "", "type": 3}, {"release_date": None, "type": 3},
                       {"release_date": "garbage", "type": 3}])):
        av = classify(data, TODAY)
        assert av == Availability() and av.flags == []


# ------------------------------------------------------------------ Streaming

def test_streaming_tag_only_for_films_from_the_last_year():
    us = {"flatrate": [prov("Netflix", 1)]}
    recent = classify(tmdb([rel("2025-11-12", 3)], providers={"US": us}), TODAY)
    old = classify(tmdb([rel("2025-11-11", 3)], providers={"US": us}), TODAY)
    assert recent.flags == ["streaming"]
    assert old.flags == [] and old.streaming == ["Netflix"]     # card still lists it


def test_streaming_original_with_only_a_digital_release():
    av = classify(tmdb([rel("2026-11-01", 4)]), TODAY)
    assert av.flags == ["streaming"] and av.first_release == "2026-11-01"


def test_free_and_ads_services_are_not_home_or_listed():
    us = {"free": [prov("Kanopy", 40)], "ads": [prov("Tubi", 50)]}
    av = classify(tmdb([rel("2026-10-01", 3)], providers={"US": us}), TODAY)
    assert av.in_theaters and av.streaming == []


def test_subscription_list_ordered_by_priority_and_deduped():
    us = {"flatrate": [prov("Max", 5), prov("Netflix", 1), prov("Max", 5)],
          "rent": [prov("Apple TV Store", 2)]}
    assert classify(tmdb(providers={"US": us}), TODAY).streaming == ["Netflix", "Max"]


def test_other_region_providers_ignored():
    gb = {"flatrate": [prov("BBC iPlayer", 1)]}
    assert classify(tmdb(providers={"GB": gb}), TODAY).streaming == []


# ------------------------------------------------------------------ card

def test_card_line():
    assert card_line(None) is None
    assert card_line(Availability()) is None
    assert card_line(Availability(streaming=["Netflix", "Max"])) == "📺 Streaming on Netflix, Max"
    six = Availability(in_theaters=True, streaming=list("ABCDEF"))
    assert card_line(six) == "🎟️ In theaters now\n📺 Streaming on A, B, C, D and 2 more"


def test_card_hides_addon_channels_and_ad_tiers():
    av = Availability(streaming=["Netflix", "Netflix Standard with Ads", "HBO Max",
                                 "HBO Max Amazon Channel", "Philo"])
    assert card_line(av) == "📺 Streaming on Netflix, HBO Max, Philo"
    # Only available through an add-on channel: still shown.
    assert card_line(Availability(streaming=["Starz Amazon Channel"])) == (
        "📺 Streaming on Starz Amazon Channel")


def test_card_field_sits_above_reminder_with_justwatch_credit():
    movie = Movie(1, "Film", 2026, 100, "Plot", None, ["Drama"], ["Someone"])
    e = build_embed(movie, 5, None, Availability(streaming=["Netflix"]))
    names = [f.name for f in e.fields]
    assert names[-2:] == [FIELD_NAME, REMINDER_NAME]
    assert JUSTWATCH_CREDIT in e.footer.text and e.footer.text.startswith(VOTE_FOOTER)
    # Re-rendering replaces the field; losing availability drops field and credit.
    set_availability(e, Availability(streaming=["Max"]))
    assert [f.value for f in e.fields if f.name == FIELD_NAME] == ["📺 Streaming on Max"]
    set_availability(e, Availability())
    assert FIELD_NAME not in [f.name for f in e.fields]
    assert e.footer.text == VOTE_FOOTER and e.fields[-1].name == REMINDER_NAME


def test_merge_flags_keeps_other_flags():
    assert merge_flags(["in_theaters", "other"], Availability(streaming_tag=True)) == [
        "other", "streaming"]


# ------------------------------------------------------------------ only changed posts are edited

class FakeMovies:
    def __init__(self):
        self.cards: list[int] = []
        self.retags: list[int] = []

    async def update_card(self, tmdb_id):
        self.cards.append(tmdb_id)

    async def retag(self, tmdb_id):
        self.retags.append(tmdb_id)


async def setup_db(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    for i in range(1, 6):
        await db.insert_movie(tmdb_id=i, thread_id=100 + i, starter_message_id=100 + i,
                              title=f"Film {i}", year=2026, status="backlog", flags=[],
                              genre_tags=[], added_by=None, pitch=None, source="addmovie")
    return db


STREAMING = tmdb([rel("2026-06-01", 3)], providers={"US": {"flatrate": [prov("Netflix", 1)]}})
OLD_ON_MAX = tmdb([rel("2001-06-01", 3)], providers={"US": {"flatrate": [prov("Max", 1)]}})
THEATERS = tmdb([rel("2026-11-01", 3)])


NETFLIX = Availability(streaming_tag=True, streaming=["Netflix"], first_release="2026-06-01")


def test_old_film_gets_card_line_but_no_tag(tmp_path):
    async def run():
        db = await setup_db(tmp_path)
        report = await check_movies([await db.get_movie(1)], lambda i: _ret(OLD_ON_MAX),
                                    TODAY, pause=0)
        await db.close()
        return report
    (change,) = asyncio.run(run()).changes
    assert change.card_changed and not change.flags_changed


def test_only_changed_posts_are_edited(tmp_path):
    async def run():
        db = await setup_db(tmp_path)
        # 1: already Streaming on Netflix (stored)  -> unchanged, no edits
        # 2: newly streaming                         -> card + tags
        # 3: nothing, never checked                  -> stored, no edits
        # 4: was Streaming on Netflix, now on Max     -> card only (tag stays)
        # 5: TMDB fails                              -> untouched
        await db.set_availability(1, NETFLIX.to_json(), ["streaming"])
        await db.set_availability(4, NETFLIX.to_json(), ["streaming"])
        responses = {1: STREAMING, 2: STREAMING, 3: tmdb(),
                     4: tmdb([rel("2026-06-01", 3)],
                             providers={"US": {"flatrate": [prov("Max", 1)]}})}

        async def fetch(tmdb_id):
            if tmdb_id == 5:
                raise TMDBError("boom")
            return responses[tmdb_id]

        movies = FakeMovies()
        bot = SimpleNamespace(db=db, movies=movies, tmdb=None,
                              config=SimpleNamespace(tz=None))
        svc = AvailabilityService(bot)
        svc.fetch, svc.fetch_pause, svc.edit_pause = fetch, 0, 0
        svc.today = lambda: TODAY
        report = await svc.run()
        rows = {i: await db.get_movie(i) for i in range(1, 6)}
        last = await db.get_meta("availability_last_run")
        await db.close()
        return report, movies, rows, last

    report, movies, rows, last = asyncio.run(run())
    assert movies.cards == [2, 4]
    assert movies.retags == [2]
    assert [c.tmdb_id for c in report.changes] == [2, 4]
    assert report.checked == 4 and report.errors == 1
    assert report.tally()["streaming +"] == 1
    assert json.loads(rows[2]["flags"]) == ["streaming"]
    assert rows[3]["availability"] is not None and rows[3]["availability_checked_at"]
    assert Availability.from_json(rows[4]["availability"]).streaming == ["Max"]
    assert rows[5]["availability"] is None
    assert last


def test_losing_a_flag_retags_and_dry_run_touches_nothing(tmp_path):
    async def run():
        db = await setup_db(tmp_path)
        await db.set_availability(1, Availability(in_theaters=True).to_json(), ["in_theaters"])
        rows = [await db.get_movie(1), await db.get_movie(2)]
        out_now = tmdb([rel("2026-08-01", 3)])   # left the 60-day window
        report = await check_movies(rows, lambda i: _ret({1: out_now, 2: THEATERS}[i]),
                                    TODAY, pause=0)
        stored = await db.get_movie(1)
        await db.close()
        return report, stored

    report, stored = asyncio.run(run())
    assert report.tally() == {"in_theaters +": 1, "in_theaters −": 1,
                              "streaming +": 0, "streaming −": 0}
    assert all(c.flags_changed and c.card_changed for c in report.changes)
    assert json.loads(stored["flags"]) == ["in_theaters"]    # dry run: DB untouched


async def _ret(value):
    return value


def test_edit_failure_is_counted_and_run_continues(tmp_path):
    async def run():
        db = await setup_db(tmp_path)

        class Failing(FakeMovies):
            async def update_card(self, tmdb_id):
                if tmdb_id == 1:
                    raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
                await super().update_card(tmdb_id)

        movies = Failing()
        bot = SimpleNamespace(db=db, movies=movies, config=SimpleNamespace(tz=None))
        svc = AvailabilityService(bot)
        svc.fetch = lambda i: _ret(STREAMING if i <= 2 else tmdb())
        svc.fetch_pause = svc.edit_pause = 0
        svc.today = lambda: TODAY
        report = await svc.run()
        await db.close()
        return report, movies

    report, movies = asyncio.run(run())
    assert report.edit_failures == 1 and movies.cards == [2]
