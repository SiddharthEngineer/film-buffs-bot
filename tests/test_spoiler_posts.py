"""Phase 3R: spoiler companion posts (lazy creation, moving messages).
Phase 16: `Title (Year)` titles, embed-only starters with posters, tags."""
import asyncio
import json
from types import SimpleNamespace

import discord
import pytest

from filmbuffs.config import TagDef
from filmbuffs.db import Database
from filmbuffs.spoilers import (MESSAGE_LIMIT, STARTER_VERSION, AlreadyMoved, SpoilerService,
                                batch_files, move_messages, safe_webhook_username,
                                spoiler_post_name, split_text, starter_embed,
                                starter_is_current)
from filmbuffs.tags import DECADE_TAGS, decade_tag_key, post_watch_tag_keys

MOVIES, SPOILERS = 100, 200


# ------------------------------------------------------------------ pure helpers

def test_post_name_and_starter_embed():
    assert spoiler_post_name("Parasite (2019)") == "Parasite (2019)"
    assert len(spoiler_post_name("x" * 300)) <= 100
    e = starter_embed("Parasite (2019)", "https://discord.com/channels/1/2", "https://img/p.jpg")
    assert "Post-watch discussion for Parasite (2019)" in e.description
    assert "Spoilers allowed" in e.description
    assert "[Back to the spoiler-free post in #movies](https://discord.com/channels/1/2)" \
        in e.description
    assert e.thumbnail.url == "https://img/p.jpg"
    assert e.title is None                        # the post title already says it
    assert starter_embed("P", "https://x", None).thumbnail.url is None


def test_starter_is_current_needs_no_text_and_same_embed():
    e = starter_embed("P (2019)", "https://x", "https://img/p.jpg")
    assert starter_is_current(SimpleNamespace(content="", embeds=[e]), e)
    assert not starter_is_current(SimpleNamespace(content="old text", embeds=[e]), e)
    assert not starter_is_current(SimpleNamespace(content="old text", embeds=[]), e)
    other = starter_embed("P (2019)", "https://x", None)
    assert not starter_is_current(SimpleNamespace(content="", embeds=[other]), e)


def test_decade_bucketing():
    assert decade_tag_key(None) is None and decade_tag_key(0) is None
    assert decade_tag_key(1927) == decade_tag_key(1979) == "d_pre1980"
    assert decade_tag_key(1980) == decade_tag_key(1989) == "d_1980s"
    assert decade_tag_key(1999) == "d_1990s"
    assert decade_tag_key(2000) == "d_2000s"
    assert decade_tag_key(2019) == "d_2010s"
    assert decade_tag_key(2024) == "d_2020s"
    assert decade_tag_key(2031) == "d_2020s"          # until a 2030s tag exists
    assert {decade_tag_key(y) for y in range(1900, 2040)} == set(DECADE_TAGS)


def test_post_watch_tags_never_exceed_five():
    five = ["g_drama", "g_crime", "g_thriller", "g_comedy", "g_horror"]
    assert post_watch_tag_keys(2019, five, True) == ["reviews", "d_2010s", "g_drama",
                                                     "g_crime", "g_thriller"]
    assert post_watch_tag_keys(2019, five, False) == ["d_2010s", "g_drama", "g_crime",
                                                      "g_thriller"]
    assert post_watch_tag_keys(None, ["g_drama"], False) == ["g_drama"]
    assert len(post_watch_tag_keys(1975, five, True, max_genres=5)) == 5
    # Only genre keys carry over from #movies (status/flag keys never do).
    assert post_watch_tag_keys(2001, ["watched", "g_drama"], False) == ["d_2000s", "g_drama"]


def test_server_yaml_post_watch_tags():
    import yaml
    from pathlib import Path
    layout = yaml.safe_load((Path(__file__).parent.parent / "server.yaml").read_text())
    chans = {c["key"]: c for cat in layout["categories"] for c in cat["channels"]}
    pw = chans["movie_spoilers"]
    assert pw["name"] == "post-watch"
    assert pw["topic"].startswith("⚠️ SPOILERS ALLOWED")
    keys = [t["key"] for t in pw["tags"]]
    assert len(keys) == 19 and all(t.get("moderated") for t in pw["tags"])
    assert set(DECADE_TAGS) | {"reviews"} <= set(keys)
    movie_genres = {(t["key"], t["name"]) for t in chans["movies"]["tags"]
                    if t["key"].startswith("g_")}
    assert {(t["key"], t["name"]) for t in pw["tags"] if t["key"].startswith("g_")} \
        == movie_genres and len(movie_genres) == 12


def test_move_messages_short():
    assert move_messages("he was the killer", "-# moved") == ["-# moved\nhe was the killer"]


def test_move_messages_keeps_spoiler_bars_and_empty_text():
    assert move_messages("||twist||", "-# h") == ["-# h\n||twist||"]
    assert move_messages("   ", "-# h") == ["-# h"]


def test_move_messages_long_text_fits_and_round_trips():
    text = ("word " * 900).strip()
    header = "-# ↪ replying to Sam: https://x\n-# moved from the spoiler-free post by <@1>"
    chunks = move_messages(text, header)
    assert len(chunks) >= 3
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks)
    assert chunks[0].startswith(header)
    rejoined = " ".join([chunks[0][len(header) + 1:]] + chunks[1:])
    assert rejoined.split() == text.split()


def test_split_text_without_spaces_hard_cuts():
    assert split_text("a" * 25, 10, 10) == ["a" * 10, "a" * 10, "a" * 5]


def test_batch_files_respects_count_and_size():
    mb = 1024 * 1024
    assert batch_files([]) == []
    assert batch_files([1] * 12) == [list(range(10)), [10, 11]]
    assert batch_files([15 * mb, 15 * mb, 5 * mb]) == [[0], [1, 2]]


def test_webhook_username_sanitised():
    name = safe_webhook_username("Discord Dan the clyde fan")
    assert "discord" not in name.lower() and "clyde" not in name.lower()
    assert len(safe_webhook_username("x" * 200)) == 80


# ------------------------------------------------------------------ fakes

class FakeThread:
    def __init__(self, tid, parent_id, name="", tags=()):
        self.id, self.parent_id, self.name = tid, parent_id, name
        self.jump_url = f"https://discord.com/channels/1/{tid}"
        self.messages = {}
        self.applied_tags = list(tags)
        self.archived = False
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.name = kw.get("name", self.name)
        self.applied_tags = kw.get("applied_tags", self.applied_tags)
        self.archived = kw.get("archived", self.archived)

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "gone")
        return self.messages[mid]


class FakeMovies:
    def __init__(self, posts):
        self.posts, self.cards, self.retags = posts, [], []

    async def find_post(self, tmdb_id):
        return self.posts.get(tmdb_id)

    async def update_card(self, tmdb_id):
        self.cards.append(tmdb_id)

    async def retag(self, tmdb_id):
        self.retags.append(tmdb_id)


PW_TAG_KEYS = (["reviews"] + list(DECADE_TAGS)
               + ["g_action", "g_comedy", "g_crime", "g_drama", "g_thriller"])
PW_TAGS = {k: TagDef(k, k, 700 + i) for i, k in enumerate(PW_TAG_KEYS)}


class FakeForum:
    def get_tag(self, tid):
        return SimpleNamespace(id=tid) if tid in {t.id for t in PW_TAGS.values()} else None


class FakeTMDB:
    def __init__(self):
        self.calls = 0

    async def movie(self, tmdb_id):
        self.calls += 1
        return SimpleNamespace(poster_url=f"https://image.tmdb.org/t/p/w500/{tmdb_id}.jpg")


class FakeStarter:
    def __init__(self, mid, content="", embeds=()):
        self.id, self.content, self.embeds = mid, content, list(embeds)
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        if "content" in kw:
            self.content = kw["content"] or ""
        if "embed" in kw:
            self.embeds = [kw["embed"]]


class FakeBot:
    def __init__(self, db, posts):
        self.db, self.movies = db, FakeMovies(posts)
        self.config = SimpleNamespace(channels={"movies": MOVIES, "movie_spoilers": SPOILERS},
                                      guild_id=1, max_genre_tags=3, post_watch_tags=PW_TAGS,
                                      post_watch_tags_ready=True)
        self.threads = {}
        self.tmdb = FakeTMDB()

    def get_channel(self, cid):
        return self.threads.get(cid)


class FakeService(SpoilerService):
    """Creates fake spoiler threads; counts creations."""
    def __init__(self, bot):
        super().__init__(bot)
        self.created = 0
        self.wh = FakeWebhook()

    @property
    def forum(self):
        return FakeForum()

    async def _create_post(self, display, movies_url, poster_url, tags):
        self.created += 1
        await asyncio.sleep(0.01)   # let concurrent callers interleave
        t = FakeThread(9000 + self.created, SPOILERS, name=display, tags=tags)
        t.starter = starter_embed(display, movies_url, poster_url)
        self.bot.threads[t.id] = t
        return t, SimpleNamespace(id=t.id)

    async def webhook(self):
        return self.wh


class FakeWebhook:
    def __init__(self, fail_on=None):
        self.sent, self.fail_on = [], fail_on

    async def send(self, content=None, *, files=discord.utils.MISSING, **kw):
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        m = FakeSent(len(self.sent), content, [] if files is discord.utils.MISSING else files, kw)
        self.sent.append(m)
        return m


class FakeSent:
    def __init__(self, i, content, files, kw):
        self.id, self.content, self.files, self.kw = 5000 + i, content, files, kw
        self.jump_url = f"https://x/{self.id}"
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeAttachment:
    def __init__(self, name, size):
        self.filename, self.size = name, size

    def is_spoiler(self):
        return False

    async def to_file(self, spoiler=False):
        return f"file:{self.filename}"


class FakeMessage:
    def __init__(self, thread, mid, content="", attachments=(), reference=None, fail_delete=False):
        self.id, self.channel, self.content = mid, thread, content
        self.attachments = list(attachments)
        self.reference = reference
        self.guild = SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024)
        self.author = SimpleNamespace(display_name="Sam",
                                      display_avatar=SimpleNamespace(url="https://a/sam.png"))
        self.jump_url = f"https://discord.com/channels/1/{thread.id}/{mid}"
        self.fail_delete = fail_delete
        thread.messages[mid] = self

    async def delete(self):
        if self.fail_delete:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
        del self.channel.messages[self.id]


ACTOR = SimpleNamespace(id=7, mention="<@7>")


async def make_service(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    movies_post = FakeThread(555, MOVIES)
    await db.insert_movie(tmdb_id=496243, thread_id=555, starter_message_id=555,
                          title="Parasite", year=2019, status="backlog", flags=[],
                          genre_tags=["g_comedy", "g_thriller", "g_drama"], added_by=1,
                          pitch=None, source="addmovie")
    bot = FakeBot(db, {496243: movies_post})
    return FakeService(bot), movies_post, db


# ------------------------------------------------------------------ lazy creation

def test_concurrent_opens_create_one_post(tmp_path):
    async def run():
        svc, _, db = await make_service(tmp_path)
        results = await asyncio.gather(*(svc.open(496243) for _ in range(5)))
        row = await db.get_movie(496243)
        await db.close()
        return svc, results, row
    svc, results, row = asyncio.run(run())
    assert svc.created == 1
    assert len({t.id for t, _ in results}) == 1
    assert [c for _, c in results].count(True) == 1
    assert row["spoiler_thread_id"] == results[0][0].id
    assert "spoilers" not in json.loads(row["flags"])     # the Post-watch tag is retired
    assert svc.bot.movies.cards == [496243]


def test_deleted_spoiler_post_is_recreated(tmp_path):
    async def run():
        svc, _, db = await make_service(tmp_path)
        first, _ = await svc.open(496243)
        del svc.bot.threads[first.id]          # deleted by hand
        svc.bot.fetch_channel = _not_found
        second, created = await svc.open(496243)
        await db.close()
        return first, second, created
    first, second, created = asyncio.run(run())
    assert created and second.id != first.id


async def _not_found(cid):
    raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "gone")


def test_open_without_movie_post_raises(tmp_path):
    async def run():
        svc, _, db = await make_service(tmp_path)
        svc.bot.movies.posts.clear()
        try:
            with pytest.raises(LookupError):
                await svc.open(496243)
        finally:
            await db.close()
        return svc
    assert asyncio.run(run()).created == 0


def test_migration_clears_old_spoiler_line(tmp_path):
    import sqlite3
    from filmbuffs.db import MIGRATIONS
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(MIGRATIONS[0])
    c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    c.execute("INSERT INTO schema_version VALUES (1)")
    c.execute("INSERT INTO movies(tmdb_id, thread_id, starter_message_id, title, status, flags, "
              "added_at, source, spoiler_marker_message_id, spoiler_divider_message_id) "
              "VALUES (1, 2, 2, 'P', 'backlog', '[\"spoilers\",\"streaming\"]', 'x', 'addmovie', 5, 6)")
    c.commit(); c.close()

    async def run():
        db = Database(path)
        await db.connect()
        row = await db.get_movie(1)
        await db.close()
        return row
    row = asyncio.run(run())
    assert json.loads(row["flags"]) == ["streaming"]
    assert row["spoiler_marker_message_id"] is None and row["spoiler_divider_message_id"] is None
    assert row["spoiler_thread_id"] is None and row["card_version"] == 0


# ------------------------------------------------------------------ Phase 16

def tag_names(thread):
    by_id = {t.id: k for k, t in PW_TAGS.items()}
    return [by_id[t.id] for t in thread.applied_tags]


async def add_review(db, guid, tmdb_id, spoiler_message_id, text="the twist!"):
    await db.conn.execute(
        "INSERT INTO letterboxd_entries(guid, discord_user_id, username, kind, tmdb_id, "
        "review_text, seen_at, source, state, spoiler_message_id) "
        "VALUES (?, 1, 'sam', 'review', ?, ?, 'x', 'rss', 'posted', ?)",
        (guid, tmdb_id, text, spoiler_message_id))
    await db.conn.commit()


def test_open_creates_tagged_post_with_poster(tmp_path):
    async def run():
        svc, _, db = await make_service(tmp_path)
        thread, _ = await svc.open(496243)
        row = await db.get_movie(496243)
        await db.close()
        return svc, thread, row
    svc, thread, row = asyncio.run(run())
    assert thread.name == "Parasite (2019)"
    assert tag_names(thread) == ["d_2010s", "g_comedy", "g_thriller", "g_drama"]
    assert thread.starter.thumbnail.url.endswith("/496243.jpg")
    assert row["poster_url"].endswith("/496243.jpg")       # backfilled once and stored
    assert svc.bot.tmdb.calls == 1


def test_poster_stored_at_creation_is_not_looked_up(tmp_path):
    async def run():
        svc, _, db = await make_service(tmp_path)
        await db.set_poster(496243, "https://img/stored.jpg")
        thread, _ = await svc.open(496243)
        await db.set_poster(496243, "")                     # TMDB has none
        none = await svc.poster_url(await db.get_movie(496243))
        await db.close()
        return svc, thread, none
    svc, thread, none = asyncio.run(run())
    assert thread.starter.thumbnail.url == "https://img/stored.jpg"
    assert none is None and svc.bot.tmdb.calls == 0


async def old_style_post(svc, db):
    """A post as Phase 3R made it: "— Post-watch" title, text starter, no tags."""
    t = FakeThread(8000, SPOILERS, name="Parasite (2019) — Post-watch")
    t.archived = True
    starter = FakeStarter(8001, content="⚠️ **Post-watch discussion for Parasite (2019).**")
    t.messages[starter.id] = starter
    svc.bot.threads[t.id] = t
    await db.set_spoiler_post(496243, t.id, starter.id)
    return t, starter


def test_refresh_rewrites_title_starter_and_tags_once(tmp_path):
    async def run():
        svc, movies_post, db = await make_service(tmp_path)
        t, starter = await old_style_post(svc, db)
        await add_review(db, "r1", 496243, 4242)
        await svc.refresh_starters()
        first_edits = (list(t.edits), list(starter.edits))
        version = await db.get_meta("post_watch_starter_version")
        await db.set_meta("post_watch_starter_version", None)
        await svc.refresh_starters()                     # already current: no edits
        await db.close()
        return t, starter, first_edits, version, movies_post
    t, starter, (t_edits, s_edits), version, movies_post = asyncio.run(run())
    assert version == STARTER_VERSION
    assert len(t_edits) == 1                             # name + tags + unarchive in one call
    assert t_edits[0]["archived"] is False
    assert t.name == "Parasite (2019)"
    assert tag_names(t) == ["reviews", "d_2010s", "g_comedy", "g_thriller", "g_drama"]
    assert len(s_edits) == 1 and s_edits[0]["content"] is None
    assert starter.content == "" and len(starter.embeds) == 1
    assert movies_post.jump_url in starter.embeds[0].description
    assert starter.embeds[0].thumbnail.url.endswith("/496243.jpg")
    assert len(t.edits) == 1 and len(starter.edits) == 1   # second run changed nothing


def test_refresh_not_marked_done_until_tags_exist(tmp_path):
    async def run():
        svc, _, db = await make_service(tmp_path)
        await old_style_post(svc, db)
        svc.bot.config.post_watch_tags_ready = False
        await svc.refresh_starters()
        version = await db.get_meta("post_watch_starter_version")
        await db.close()
        return version
    assert asyncio.run(run()) is None


def test_retag_follows_reviews(tmp_path):
    async def run():
        svc, _, db = await make_service(tmp_path)
        thread, _ = await svc.open(496243)
        before = tag_names(thread)
        await add_review(db, "r1", 496243, 4242)
        await add_review(db, "w1", 496243, 4243, text="")    # a log without review text
        await svc.retag(496243)
        with_review = tag_names(thread)
        await db.update_entry("r1", spoiler_message_id=None)  # review deleted
        await svc.retag(496243)
        after = tag_names(thread)
        await db.close()
        return before, with_review, after
    before, with_review, after = asyncio.run(run())
    assert "reviews" not in before and "reviews" not in after
    assert with_review[0] == "reviews" and len(with_review) == 5


def test_migration_14_adds_poster_url(tmp_path):
    async def run():
        db = Database(tmp_path / "m.db")
        await db.connect()
        cols = [r[1] for r in await (await db.conn.execute("PRAGMA table_info(movies)")).fetchall()]
        version = (await (await db.conn.execute("SELECT MAX(version) FROM schema_version")).fetchone())[0]
        await db.close()
        return cols, version
    cols, version = asyncio.run(run())
    assert "poster_url" in cols and version >= 14


# ------------------------------------------------------------------ moving

def test_move_sends_as_author_then_deletes(tmp_path):
    async def run():
        svc, post, db = await make_service(tmp_path)
        replied = SimpleNamespace(author=SimpleNamespace(display_name="Alex"),
                                  jump_url="https://discord.com/channels/1/555/40")
        ref = SimpleNamespace(message_id=40, resolved=replied)
        msg = FakeMessage(post, 41, "the dad did it", [FakeAttachment("a.png", 100)], ref)
        moved = await svc.move(msg, ACTOR, 496243)
        await db.close()
        return svc, post, moved
    svc, post, moved = asyncio.run(run())
    assert 41 not in post.messages                       # original deleted
    assert len(svc.wh.sent) == 1 and moved is svc.wh.sent[0]
    m = svc.wh.sent[0]
    lines = m.content.split("\n")
    assert lines[0] == "-# ↪ replying to Alex: https://discord.com/channels/1/555/40"
    assert lines[1] == "-# moved from the spoiler-free #movies post by <@7>"
    assert lines[2] == "the dad did it"
    assert m.files == ["file:a.png"]
    assert m.kw["username"] == "Sam" and m.kw["avatar_url"] == "https://a/sam.png"
    assert m.kw["thread"].parent_id == SPOILERS
    assert svc.created == 1                              # spoiler post created on demand


def test_move_twice_only_moves_once(tmp_path):
    async def run():
        svc, post, db = await make_service(tmp_path)
        msg = FakeMessage(post, 42, "spoiler")
        results = await asyncio.gather(svc.move(msg, ACTOR, 496243),
                                       svc.move(msg, ACTOR, 496243), return_exceptions=True)
        await db.close()
        return svc, results
    svc, results = asyncio.run(run())
    assert len(svc.wh.sent) == 1
    assert sum(isinstance(r, AlreadyMoved) for r in results) == 1


def test_move_rolls_back_if_original_cannot_be_deleted(tmp_path):
    async def run():
        svc, post, db = await make_service(tmp_path)
        msg = FakeMessage(post, 43, "x " * 1500, fail_delete=True)
        with pytest.raises(discord.Forbidden):
            await svc.move(msg, ACTOR, 496243)
        await db.close()
        return svc, post
    svc, post = asyncio.run(run())
    assert 43 in post.messages
    assert len(svc.wh.sent) == 2 and all(m.deleted for m in svc.wh.sent)


def test_move_rolls_back_on_partial_send(tmp_path):
    async def run():
        svc, post, db = await make_service(tmp_path)
        svc.wh = FakeWebhook(fail_on=1)
        msg = FakeMessage(post, 44, "y " * 1500)
        with pytest.raises(discord.HTTPException):
            await svc.move(msg, ACTOR, 496243)
        await db.close()
        return svc, post
    svc, post = asyncio.run(run())
    assert 44 in post.messages and all(m.deleted for m in svc.wh.sent)


def test_move_refuses_oversized_attachment_without_changes(tmp_path):
    async def run():
        svc, post, db = await make_service(tmp_path)
        msg = FakeMessage(post, 45, "", [FakeAttachment("big.mov", 30 * 1024 * 1024)])
        with pytest.raises(ValueError):
            await svc.move(msg, ACTOR, 496243)
        await db.close()
        return svc, post
    svc, post = asyncio.run(run())
    assert 45 in post.messages and svc.wh.sent == [] and svc.created == 0
