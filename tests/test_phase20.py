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
from tests.test_phase10 import WK, FakeChannel, make, settle


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
    assert [p.name for p in show.parameters] == [f"user{i}" for i in range(1, 7)]
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


# ------------------------------------------------------------------ Phase 21

def test_here_film_finds_post_watch_posts_too(tmp_path):
    from filmbuffs.vote_ui import here_film

    async def run():
        db, bot = await make(tmp_path)
        await db.set_spoiler_post(2, 502, 602)
        out = []
        for cid in (102, 502, 999, None):
            out.append(await here_film(SimpleNamespace(client=bot, channel_id=cid)))
        await db.close()
        return out
    pre, post, other, none = asyncio.run(run())
    assert pre["tmdb_id"] == 2 and post["tmdb_id"] == 2 and other is None and none is None


def person(uid, name):
    return SimpleNamespace(id=uid, display_name=name)


def test_watchlist_reports(tmp_path):
    from filmbuffs.cogs.weekly import Weekly

    async def run():
        db, bot = await make(tmp_path)
        await db.replace_watchlist(10, [(1, "Film 1", 2001), (2, "Film 2", 2002),
                                        (900, "Zodiac", 2007)], 3, [])
        await db.replace_watchlist(11, [(2, "Film 2", 2002), (900, "Zodiac", 2007),
                                        (901, "Alien", 1979)], 3, [])
        await db.replace_watchlist(12, [(900, "Zodiac", 2007), (901, "Alien", 1979)], 2, [])
        cog = Weekly(bot)
        a, b, c = person(10, "Ann"), person(11, "Bo"), person(12, "Cy")
        one = await cog.watchlist_report([a], 10)
        other = await cog.watchlist_report([b], 10)
        two = await cog.watchlist_report([a, c], 10)
        three = await cog.watchlist_report([a, b, c], 10)
        await db.close()
        return one, other, two, three
    one, other, two, three = asyncio.run(run())
    assert one.title == "📋 Your watchlist: 3 films"
    assert "[Film 1 (2001)](https://discord.com/channels/1/101)" in one.description
    assert "• Zodiac (2007)" in one.description                  # no post: plain text
    assert other.title == "📋 Bo's watchlist: 3 films"
    # Two people: only what's on both (the intersection).
    assert two.title == "📋 On both watchlists: 1 film"
    assert "Zodiac" in two.description and "Film 1" not in two.description
    assert "Alien" not in two.description
    # Three: films on 2+ of them, most shared first, with whose.
    assert three.title == "📋 Watchlist overlap: 3 films on 2+ of 3 watchlists"
    lines = three.description.splitlines()[1:]
    assert lines[0] == "• Zodiac (2007) · **3/3** · Ann, Bo and Cy"
    assert lines[1] == "• Alien (1979) · **2/3** · Bo and Cy"
    assert lines[2].startswith("• [Film 2 (2002)]") and lines[2].endswith("· **2/3** · Ann and Bo")
    assert len(lines) == 3                                          # Film 1 is on one list


def test_watchlist_missing_text():
    from filmbuffs.cogs.weekly import missing_text
    me = person(10, "Ann")
    assert "You haven't linked your Letterboxd account yet" in missing_text([me], 10, False)
    assert missing_text([person(11, "Bo")], 10, True).startswith("❌ Bo hasn't linked")
    assert missing_text([me, person(11, "Bo")], 10, True).startswith("❌ You and Bo haven't")


def test_sync_needs_the_linked_account(tmp_path):
    from filmbuffs.cogs.letterboxd import Letterboxd

    async def run():
        db, bot = await make(tmp_path)
        cog = Letterboxd.__new__(Letterboxd)
        cog.bot = bot
        export = SimpleNamespace(username="annie")
        out = [await cog._check_owner(10, export)]                  # not linked
        await db.set_link(10, "Annie")
        out.append(await cog._check_owner(10, export))              # same, any case
        await db.set_link(10, "other")
        out.append(await cog._check_owner(10, export))              # different account
        out.append(await cog._check_owner(10, SimpleNamespace(username=None)))
        await db.close()
        return out
    unlinked, ok, other, nofile = asyncio.run(run())
    assert unlinked.startswith("❌ Link your Letterboxd account first: **/letterboxd link** annie")
    assert ok is None
    assert other.startswith("❌ You're linked to **other**, but that export is from **annie**")
    assert "profile.csv" in nofile


def test_backfill_log_posts_the_missing_pick_line(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        wlog = FakeChannel()
        bot.weekly.channel = lambda key: wlog if key == "watch_log" else None
        await db.create_week(WK, state="picked")
        await db.update_week(WK, winner_tmdb_id=3, winner_votes=1, runner_up_votes=0,
                             vote_rounds='[{"counts": {}}]', event_id=555)
        n = await bot.weekly.backfill_log()
        again = await bot.weekly.backfill_log()
        row = await db.get_week(WK)
        await db.close()
        return n, again, row, wlog.sent
    n, again, row, sent = asyncio.run(run())
    assert (n, again) == (1, 0) and row["log_message_id"] == 901
    text, mentions = sent[0]
    assert text.startswith("🎬 **Wk 2 · Nov 23–29 2026: [Film 3 (2003)](https://discord.com/channels/1/103)** · won 1–0")
    assert "Watch it any time from **" in text and "https://discord.com/events/1/555" in text
    assert "@" not in text and mentions.roles is False                # no ping


# ------------------------------------------------------------------ Phase 22: linked watchlists

PAGE = """<div class="poster" data-item-name="Black Bag (2025)" data-item-slug="black-bag-2025"
 data-item-full-display-name="Black Bag (2025)"></div>
<div data-item-name="Am&eacute;lie (2001)"></div><div data-item-name="Untitled"></div>
<div data-item-name="Black Bag (2025)"></div>
<a href="/ann/watchlist/page/2/">2</a><a href="/ann/watchlist/page/3/">3</a>"""


def test_parse_watchlist_page():
    from filmbuffs.watchlists import parse_name, parse_page
    films, last = parse_page(PAGE)
    assert films == [("Black Bag", 2025), ("Amélie", 2001), ("Untitled", None)]
    assert last == 3
    assert parse_name("Mission: Impossible (Part One) (2023)") == ("Mission: Impossible (Part One)", 2023)
    assert parse_page("<html></html>") == ([], 1)


class FakeResp:
    def __init__(self, status, body=""):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self.body


class FakeSession:
    def __init__(self, pages, status=200):
        self.pages, self.status, self.urls = pages, status, []

    def get(self, url, **kw):
        self.urls.append(url)
        n = int(url.rstrip("/").rsplit("/", 1)[1]) if "/page/" in url else 1
        return FakeResp(self.status, self.pages.get(n, ""))


def test_refresh_reads_every_page_and_caches(tmp_path, monkeypatch):
    import filmbuffs.watchlists as wl
    from filmbuffs.watchlists import WatchlistService
    monkeypatch.setattr(wl, "PAGE_DELAY", 0)

    class Resolver:
        lookups = 0

        def __init__(self, *a, **kw):
            pass

        async def resolve(self, title, year):
            return ({"Zodiac": 900, "Alien": 901}.get(title), "exact", "")
    monkeypatch.setattr("filmbuffs.backfill.TitleResolver", Resolver)

    async def run():
        db, bot = await make(tmp_path)
        bot.session = FakeSession({
            1: '<i data-item-name="Zodiac (2007)"></i><a href="/ann/watchlist/page/2/">2</a>',
            2: '<i data-item-name="Alien (1979)"></i><i data-item-name="Nope Film (2030)"></i>'})
        bot.tmdb = None
        svc = WatchlistService(bot)
        first = await svc.refresh(10, "Ann")
        again = await svc.refresh(10, "ann")                     # fresh: no fetch
        urls = list(bot.session.urls)
        other = await svc.refresh(10, "bob")                     # another account: re-read
        items = await db.watchlist_overlap([10], 1)
        bot.session = FakeSession({}, status=404)
        gone = await svc.refresh(11, "nobody")
        await db.close()
        return first, again, urls, other, items, gone
    first, again, urls, other, items, gone = asyncio.run(run())
    assert first.ok and first.films == 3 and first.unmatched == ["Nope Film (2030)"]
    assert again.ok and again.films == 0
    assert urls == ["https://letterboxd.com/Ann/watchlist/",
                    "https://letterboxd.com/Ann/watchlist/page/2/"]
    assert other.films == 3
    assert [r["tmdb_id"] for r in items] == [901, 900]          # by title
    assert not gone.ok and "no user **nobody**" in gone.error


# ------------------------------------------------------------------ Phase 23: /info

def test_info_sections_and_groups():
    from filmbuffs.info import channel_ref, command_ref, info_embeds
    ch, cmd = channel_ref({"watch_planning": 3, "ideas": 4}), command_ref({"letterboxd": 22})
    member = info_embeds(ch, cmd, 15, [])
    titles = [e.title for e in member]
    assert titles == ["🗳️ The weekly movie", "🍿 Movie posts", "📒 Letterboxd", "📚 More"]
    assert member[0].author.name == "ℹ️ How Film Buffs works"
    assert "<#3>" in member[0].description and "**/vote**" in member[0].description
    lb = member[2].description
    assert "</letterboxd link:22>" in lb and "about every 15 minutes" in lb
    assert "must be from the account you linked" in lb and "**three or more**" in lb
    assert "/admin" not in " ".join(e.description for e in member)
    grouped = info_embeds(ch, cmd, 15, ["OSU"])
    assert grouped[1].title == "🌙 OSU's monthly movie"
    total = sum(len(e.title) + len(e.description) + len(e.author.name or "") for e in grouped)
    assert total < 6000                                                  # Discord's total


def test_admin_commands_are_hidden_from_members():
    # Discord hides commands with default permissions from members without them.
    from filmbuffs.cogs.admin import Admin
    from filmbuffs.cogs.help import Help
    group = Admin(SimpleNamespace()).__cog_app_commands_group__
    assert group.default_permissions == discord.Permissions(manage_guild=True)
    for c in Help.__cog_app_commands__:
        assert c.default_permissions is None                            # /help, /info: everyone
    assert {c.name for c in Help.__cog_app_commands__} == {"help", "info"}


def test_daily_refresh_reads_every_linked_member(tmp_path, monkeypatch):
    from filmbuffs.watchlists import Refresh, WatchlistService

    async def run():
        db, bot = await make(tmp_path)
        await db.set_link(10, "ann")
        await db.set_link(11, "bob")
        await db.set_link(12, "cat")
        svc = WatchlistService(bot)
        seen = []

        async def refresh(uid, username, force=False):
            seen.append((uid, username, force))
            if username == "bob":
                raise RuntimeError("boom")                       # one failure doesn't stop it
            return Refresh(username == "ann", None if username == "ann" else "down")
        svc.refresh = refresh
        result = await svc.refresh_all(pause=0)
        last = await db.get_meta("watchlists_last_run")
        await db.close()
        return result, seen, last
    result, seen, last = asyncio.run(run())
    assert result == (1, 2)
    assert seen == [(10, "ann", False), (11, "bob", False), (12, "cat", False)]  # fresh lists skipped inside refresh
    assert last is not None
