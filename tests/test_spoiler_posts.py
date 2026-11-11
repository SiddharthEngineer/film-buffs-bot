"""Phase 3R: spoiler companion posts (lazy creation, moving messages)."""
import asyncio
import json
from types import SimpleNamespace

import discord
import pytest

from filmbuffs.db import Database
from filmbuffs.spoilers import (MESSAGE_LIMIT, AlreadyMoved, SpoilerService, batch_files,
                                move_messages, safe_webhook_username, spoiler_post_name,
                                split_text, starter_text)

MOVIES, SPOILERS = 100, 200


# ------------------------------------------------------------------ pure helpers

def test_post_name_and_starter():
    assert spoiler_post_name("Parasite (2019)") == "Parasite (2019) — Post-watch"
    assert len(spoiler_post_name("x" * 300)) <= 100
    text = starter_text("Parasite (2019)", "https://discord.com/channels/1/2")
    assert "Post-watch discussion for Parasite (2019)" in text and "Spoilers allowed" in text
    assert "https://discord.com/channels/1/2" in text


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
    def __init__(self, tid, parent_id):
        self.id, self.parent_id = tid, parent_id
        self.jump_url = f"https://discord.com/channels/1/{tid}"
        self.messages = {}

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


class FakeBot:
    def __init__(self, db, posts):
        self.db, self.movies = db, FakeMovies(posts)
        self.config = SimpleNamespace(channels={"movies": MOVIES, "movie_spoilers": SPOILERS},
                                      guild_id=1)
        self.threads = {}

    def get_channel(self, cid):
        return self.threads.get(cid)


class FakeService(SpoilerService):
    """Creates fake spoiler threads; counts creations."""
    def __init__(self, bot):
        super().__init__(bot)
        self.created = 0
        self.wh = FakeWebhook()

    async def _create_post(self, display, movies_url):
        self.created += 1
        await asyncio.sleep(0.01)   # let concurrent callers interleave
        t = FakeThread(9000 + self.created, SPOILERS)
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
                          genre_tags=["g_comedy"], added_by=1, pitch=None, source="addmovie")
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
