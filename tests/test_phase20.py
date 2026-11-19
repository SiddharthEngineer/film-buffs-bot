"""Phase 20: /nominate and /vote open the same private views as the board's buttons
(the nomination view can search and nominate), /spoiler, /nominations and
/unnominate are gone, /admin idea add, and /watchlist show compares members."""
import asyncio
from types import SimpleNamespace

import discord

from filmbuffs.cogs.weekly import fit_list
from filmbuffs.ideas import IdeasService, render_list
from filmbuffs.vote_ui import (DYNAMIC_ITEMS, NominateFindButton, NominateHereButton,
                               NominatePickSelect, NominationButton, nominations_view)
from tests.test_phase10 import make, settle


def ids(view) -> list[str]:
    return [c.custom_id for c in view.children]


def matches(cls, custom_id: str) -> bool:
    return cls.__discord_ui_compiled_template__.fullmatch(custom_id) is not None


# ------------------------------------------------------------------ nomination view

def test_nominations_view_offers_search_this_film_and_withdraw():
    here = {"tmdb_id": 42, "title": "Paris, Texas"}
    assert ids(nominations_view(False)) == ["noms:find"]
    assert nominations_view(False).children[0].item.label == "Nominate a film"
    view = nominations_view(True, "osu", here, results=[("Heat (1995)", "tmdb:949")])
    assert ids(view) == ["noms:osu:pick", "noms:osu:find", "noms:osu:here:42", "noms:osu:del:1"]
    assert view.children[0].item.options[0].value == "tmdb:949"
    assert view.children[1].item.label == "Change nomination"
    assert view.children[2].item.label == "Nominate Paris, Texas"


def test_nomination_custom_ids_route_to_one_item_each():
    cases = {"noms:find": NominateFindButton, "noms:osu:find": NominateFindButton,
             "noms:pick": NominatePickSelect, "noms:osu:pick": NominatePickSelect,
             "noms:here:42": NominateHereButton, "noms:osu:here:42": NominateHereButton,
             "noms:del:1": NominationButton}
    for cid, cls in cases.items():
        hits = [c for c in DYNAMIC_ITEMS if matches(c, cid)]
        assert hits == [cls], cid


def test_long_title_button_label_fits():
    b = NominateHereButton(1, None, "x" * 200)
    assert len(b.item.label) == 80 and b.item.label.endswith("…")


def test_my_view_hides_this_film_when_it_is_already_yours(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        here = await db.get_movie_by_thread(102)        # Film 2's post
        _, before = await bot.pool.my_view(10, here=here)
        await bot.pool.nominate(10, 2)
        text, after = await bot.pool.my_view(10, notice="hi", here=here)
        _, other = await bot.pool.my_view(10, here=await db.get_movie_by_thread(103))
        await settle()
        await db.close()
        return before, text, after, other
    before, text, after, other = asyncio.run(run())
    assert ids(before) == ["noms:find", "noms:here:2"]
    assert text.startswith("hi\n**Your nomination**: Film 2 (2002)")
    assert ids(after) == ["noms:find", "noms:del:1"]
    assert ids(other) == ["noms:find", "noms:here:3", "noms:del:1"]


# ------------------------------------------------------------------ /watchlist show

def test_shared_watchlist_is_the_intersection(tmp_path):
    async def run():
        db, _ = await make(tmp_path)
        await db.replace_watchlist(10, [(1, "Film 1", 2001), (2, "Film 2", 2002),
                                        (900, "Zodiac", 2007)], 3, [])
        await db.replace_watchlist(11, [(2, "Film 2", 2002), (900, "Zodiac", 2007),
                                        (901, "Alien", 1979)], 3, [])
        await db.replace_watchlist(12, [(900, "Zodiac", 2007), (2, "Film 2", 2002)],
                                   2, [])
        two = await db.shared_watchlist([10, 11])
        three = await db.shared_watchlist([10, 11, 12, 10])     # duplicates ignored
        none = await db.shared_watchlist([10, 13])
        await db.close()
        return two, three, none
    two, three, none = asyncio.run(run())
    assert [(r["tmdb_id"], r["title"], r["thread_id"]) for r in two] == [
        (2, "Film 2", 102), (900, "Zodiac", None)]
    assert [r["tmdb_id"] for r in three] == [2, 900]
    assert none == []


def test_fit_list_cuts_with_a_count():
    assert fit_list("H", ["a", "b"], 100) == "H\na\nb"
    out = fit_list("H", ["x" * 10] * 10, 40)
    assert len(out) <= 40 and out.endswith("more")
    assert out.count("x" * 10) + int(out.rsplit(" ", 2)[1]) == 10


# ------------------------------------------------------------------ /admin idea add

def test_typed_idea_has_no_link_and_names_its_author(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        bot.ideas = IdeasService(bot)
        idea_id = await bot.ideas.add_text("  More  [horror]\nnights ", 10, 10)
        row = await db.get_idea(idea_id)
        await db.close()
        return row
    row = asyncio.run(run())
    assert row["text"] == "More (horror) nights"
    assert row["message_id"] is None and row["suggested_by"] == 10 and row["added_by"] == 10
    embed = render_list([row], 1, {10: "Siddharth"})
    assert "• More (horror) nights · Siddharth" in embed.description


# ------------------------------------------------------------------ the commands

def test_command_set():
    from filmbuffs.cogs.spoilers import Spoilers
    from filmbuffs.cogs.weekly import Weekly
    names = {c.name for c in Weekly.__cog_app_commands__}
    assert {"nominate", "vote", "schedule", "watchlist"} <= names
    assert not {"nominations", "unnominate"} & names
    assert not [c for c in Spoilers.__cog_app_commands__ if c.name == "spoiler"]
    nominate = next(c for c in Weekly.__cog_app_commands__ if c.name == "nominate")
    assert [p.name for p in nominate.parameters] == ["title"]
    assert not nominate.parameters[0].required
    wl = next(c for c in Weekly.__cog_app_commands__ if c.name == "watchlist")
    show = wl.get_command("show")
    assert [p.name for p in show.parameters] == ["user1", "user2", "user3", "user4"]
    assert all(not p.required and p.type is discord.AppCommandOptionType.user
               for p in show.parameters)


def test_every_description_fits_discord():
    # Command, option and choice descriptions are capped at 100 chars by Discord.
    from types import SimpleNamespace
    from filmbuffs.cogs.admin import Admin
    from filmbuffs.cogs.weekly import Weekly
    cmds = list(Weekly.__cog_app_commands__) + [Admin(SimpleNamespace()).__cog_app_commands_group__]
    seen = 0
    for top in cmds:
        for c in [top, *getattr(top, "walk_commands", lambda: [])()]:
            assert 1 <= len(c.description) <= 100, c.qualified_name
            for p in getattr(c, "parameters", []):
                assert len(p.description) <= 100, (c.qualified_name, p.name)
            seen += 1
    assert seen > 10
