"""Phase 18: simpler server. #movies → #pre-watch, the card links its #post-watch
post in text (no button), every movie gets both posts, no pitches,
retire_managed: for channels and categories the setup script created."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import setup_server
from filmbuffs import spoilers as spoilers_mod
from filmbuffs.db import Database
from filmbuffs.movies import REMINDER_NAME, MovieService
from filmbuffs.state import State
from tests.test_spoiler_posts import FakeBot, FakeService, FakeStarter, FakeThread, MOVIES


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def quick(_delay, *a, **kw):
        await real(0)
    monkeypatch.setattr(spoilers_mod.asyncio, "sleep", quick)


async def add(db, tmdb_id, thread_id, title):
    await db.insert_movie(tmdb_id=tmdb_id, thread_id=thread_id, starter_message_id=thread_id,
                          title=title, year=2001, status="backlog", flags=[], genre_tags=[],
                          added_by=1, pitch=None, source="addmovie")


# ------------------------------------------------------------- post-watch backfill

def test_create_missing_posts_oldest_first_and_only_once(tmp_path):
    async def run():
        db = Database(tmp_path / "t.db")
        await db.connect()
        await add(db, 1, 600, "Newer")
        await add(db, 2, 500, "Older")
        await add(db, 3, 700, "Has one")
        await add(db, 4, 800, "Pre-watch post gone")
        posts = {i: FakeThread(t, MOVIES) for i, t in ((1, 600), (2, 500), (3, 700))}
        svc = FakeService(FakeBot(db, posts))
        await svc.open(3)                       # already has its post-watch post
        await svc.create_missing_posts()
        made = svc.created
        await svc.create_missing_posts()        # nothing left to do
        rows = {i: await db.get_movie(i) for i in (1, 2, 3, 4)}
        await db.close()
        return svc, made, rows
    svc, made, rows = asyncio.run(run())
    assert made == 3 and svc.created == 3
    # Oldest #pre-watch post first: Older (500) before Newer (600).
    assert rows[2]["spoiler_thread_id"] < rows[1]["spoiler_thread_id"]
    assert rows[4]["spoiler_thread_id"] is None
    assert svc.bot.movies.cards == [3, 2, 1]   # each card relinked once
    # Backfilled posts are archived (active-thread cap); the one opened by hand isn't.
    archived = [t.archived for t in sorted(svc.bot.threads.values(), key=lambda t: t.id)]
    assert archived == [False, True, True]


# ------------------------------------------------------------- MovieService

class Movies(MovieService):
    def __init__(self, bot, created):
        super().__init__(bot)
        self._created = created

    async def _ensure_post(self, movie, *, added_by, status, source):
        return SimpleNamespace(id=1), self._created


class Spoilers:
    def __init__(self, fail=False):
        self.opened, self.fail = [], fail

    async def open(self, tmdb_id):
        self.opened.append(tmdb_id)
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        return SimpleNamespace(id=2), True


@pytest.mark.parametrize("created,fail,opened", [(True, False, [7]), (False, False, []),
                                                 (True, True, [7])])
def test_new_post_gets_its_post_watch_post(created, fail, opened):
    bot = SimpleNamespace(spoilers=Spoilers(fail))
    svc = Movies(bot, created)
    thread, was_created = asyncio.run(svc.ensure_post(SimpleNamespace(id=7), added_by=1,
                                                      status="backlog", source="t"))
    assert was_created is created and bot.spoilers.opened == opened   # a failure isn't raised


def test_update_card_links_post_watch_and_drops_button(tmp_path):
    async def run():
        db = Database(tmp_path / "t.db")
        await db.connect()
        await add(db, 9, 900, "Film")
        await db.set_spoiler_post(9, 4242, 4243)
        thread = FakeThread(900, MOVIES)
        thread.archived = True
        e = discord.Embed(title="Film (2001)")
        e.add_field(name="Pitch", value="old pitch")
        e.add_field(name=REMINDER_NAME, value="… **🔓 Post-watch discussion** (button below).")
        thread.messages[900] = starter = FakeStarter(900, embeds=[e])
        bot = SimpleNamespace(db=db, config=SimpleNamespace(guild_id=1))
        svc = MovieService(bot)

        async def find_post(_):
            return thread
        svc.find_post = find_post
        await svc.update_card(9)
        row = await db.get_movie(9)
        await db.close()
        return starter, row, thread
    starter, row, thread = asyncio.run(run())
    assert [e.get("archived") for e in thread.edits] == [False, True]   # put back
    (edit,) = starter.edits
    assert edit["view"] is None
    assert [f.name for f in edit["embed"].fields] == [REMINDER_NAME]
    assert edit["embed"].fields[-1].value == (
        "[Already seen it? Discussion in #post-watch](https://discord.com/channels/1/4242)")
    assert row["card_version"] == 6


# ------------------------------------------------------------- server.yaml

def test_layout():
    cfg = setup_server.load_config("server.yaml")
    layout = [(c["name"], [ch["name"] for ch in c["channels"]]) for c in cfg["categories"]]
    assert layout == [
        ("🎬 Film Buffs", ["welcome", "general", "watch-planning", "Voice"]),
        ("🍿 Movies", ["pre-watch", "post-watch"]),
        ("📚 More", ["letterboxd-feed", "watch-log", "ideas"]),
        ("🎓 OSU", ["watch-planning", "chat", "voice"]),
        ("🔧 Admin", ["bot-logs"]),
    ]
    keys = {ch["key"] for c in cfg["categories"] for ch in c["channels"]}
    assert {"movies", "movie_spoilers", "watch_party"} <= keys     # keys never change
    assert [r["key"] for r in cfg["retire_managed"]] == [
        "channel:announcements", "channel:trailers", "channel:wrapped", "channel:discussions",
        "category:chat", "category:planning", "category:voice"]
    forum = next(ch for c in cfg["categories"] for ch in c["channels"] if ch["key"] == "movies")
    assert "button" not in forum["topic"] and "pitch" not in forum["topic"]


@pytest.mark.parametrize("old,new,err", [
    ('{key: "channel:wrapped",       name: wrapped}', '{key: "channel:ideas", name: ideas}',
     "both declared and retired"),
    ('{key: "channel:wrapped",       name: wrapped}', '{key: "wrapped", name: wrapped}',
     "channel:/category: key"),
])
def test_retire_managed_config_errors(tmp_path, old, new, err):
    src = Path("server.yaml").read_text()
    assert old in src
    p = tmp_path / "s.yaml"
    p.write_text(src.replace(old, new, 1))
    with pytest.raises(setup_server.ConfigError, match=err):
        setup_server.load_config(p)


# ------------------------------------------------------------- retire_managed plan

class Text(discord.TextChannel):
    def __init__(self, cid, name, category_id=None, messages=0):
        self.id, self.name, self.category_id, self._type = cid, name, category_id, 0
        self._n, self.deleted = messages, False

    async def history(self, limit=None):  # type: ignore[override]
        for i in range(min(self._n, limit or self._n)):
            yield i

    async def delete(self, reason=None):  # type: ignore[override]
        self.deleted = True


class Category(discord.CategoryChannel):
    def __init__(self, cid, name, guild):
        self.id, self.name, self._g, self.deleted = cid, name, guild, False

    @property
    def channels(self):  # type: ignore[override]
        return [c for c in self._g.chans.values() if getattr(c, "category_id", None) == self.id]

    async def delete(self, reason=None):  # type: ignore[override]
        self.deleted = True


class Guild:
    def __init__(self):
        self.chans = {}

    def get_channel(self, cid):
        return self.chans.get(cid)

    async def fetch_channel(self, cid):
        return self.chans[cid]

    async def fetch_channels(self):
        return [c for c in self.chans.values() if not c.deleted]


def reconciler(tmp_path, retire, chans):
    cfg = {"categories": [{"key": "info", "name": "Info",
                           "channels": [{"key": "general", "name": "general"}]}],
           "retire_managed": retire}
    state = State(tmp_path / "s.db")
    guild = Guild()
    for key, obj in chans.items():
        guild.chans[obj.id] = obj if not callable(obj) else obj(guild)
        state.set(key, key.split(":")[0], obj.id)
    return setup_server.Reconciler(guild, cfg, state), guild, state


def test_retire_managed_deletes_channels_then_category_and_forgets_keys(tmp_path):
    g = Guild()
    cat = Category(1, "💬 Chat", g)
    gen, tr = Text(2, "general", 1), Text(3, "trailers-and-news", 1)
    r, guild, state = reconciler(tmp_path, [
        {"key": "category:chat", "name": "💬 Chat"},
        {"key": "channel:trailers", "name": "trailers-and-news"}],
        {"category:chat": cat, "channel:general": gen, "channel:trailers": tr})
    g.chans = guild.chans
    asyncio.run(r.plan_retire_managed())
    assert [(c.op, c.what) for c in r.changes] == [
        ("-", "text channel 'trailers-and-news'"), ("-", 'category "💬 Chat"')]
    gen.category_id = None                     # #general was moved out by an earlier change
    for c in r.changes:
        asyncio.run(c.run())
    assert tr.deleted and cat.deleted and not gen.deleted
    assert state.get("channel:trailers") is None and state.get("category:chat") is None


def test_retire_managed_refuses_messages_wrong_name_and_undeclared_children(tmp_path):
    r, *_ = reconciler(tmp_path, [{"key": "channel:announcements", "name": "announcements"}],
                       {"channel:announcements": Text(3, "announcements", messages=1)})
    with pytest.raises(setup_server.ConfigError, match="has messages"):
        asyncio.run(r.plan_retire_managed())
    (tmp_path / "b").mkdir()
    r, *_ = reconciler(tmp_path / "b", [{"key": "channel:announcements", "name": "news"}],
                       {"channel:announcements": Text(3, "announcements")})
    with pytest.raises(setup_server.ConfigError, match="refusing to delete"):
        asyncio.run(r.plan_retire_managed())
    (tmp_path / "c").mkdir()
    g = Guild()
    r, guild, _ = reconciler(tmp_path / "c", [{"key": "category:chat", "name": "💬 Chat"}],
                             {"category:chat": Category(1, "💬 Chat", g)})
    guild.chans[5] = Text(5, "someone's channel", 1)     # unmanaged, would be orphaned
    g.chans = guild.chans
    with pytest.raises(setup_server.ConfigError, match="still holds"):
        asyncio.run(r.plan_retire_managed())


def test_retire_managed_forgets_what_is_already_gone(tmp_path):
    r, guild, state = reconciler(tmp_path, [{"key": "channel:wrapped", "name": "wrapped"}], {})
    state.set("channel:wrapped", "channel", 99)
    asyncio.run(r.plan_retire_managed())
    (change,) = r.changes
    assert change.op == ">"
    asyncio.run(change.run())
    assert state.get("channel:wrapped") is None


def test_group_vote_is_described_as_independent():
    cfg = setup_server.load_config("server.yaml")
    wp = next(ch for c in cfg["categories"] if c.get("_group") == "osu"
              for ch in c["channels"] if ch["name"] == "watch-planning")
    assert "completely independent from the server's weekly vote" in wp["topic"]
    assert len(wp["topic"]) <= 1024
    # Phase 19: the board says it in short; /help has the details.
    assert "Separate from the server's weekly vote." in Path("filmbuffs/monthly.py").read_text()
    from filmbuffs.help import GROUP_NOTE
    assert "separate from the weekly vote" in GROUP_NOTE
