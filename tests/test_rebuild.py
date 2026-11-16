"""Phase 17: recreate post-watch posts that carry an undeletable
"changed the post title" notice."""
import asyncio
import json
from types import SimpleNamespace

import discord

from filmbuffs.spoilers import (REBUILD_KEY, REBUILD_OLD_KEY, REBUILD_VERSION, STARTER_VERSION,
                                foreign_messages, post_activity)
from test_spoiler_posts import (SPOILERS, FakeService, FakeThread, add_review, make_service,
                                tag_names)

T = discord.MessageType


class Msg:
    def __init__(self, mid, type=T.default, embeds=(), webhook=False):
        self.id, self.type, self.embeds = mid, type, list(embeds)
        self.webhook_id = 1 if webhook else None


class HistThread(FakeThread):
    """A thread with history, sends, delete and the pinned flag."""
    next_id = 20000

    def __init__(self, tid, name="", pinned=False):
        super().__init__(tid, SPOILERS, name=name)
        self.history_msgs, self.sent = [], []
        self.deleted = False
        self.flags = SimpleNamespace(pinned=pinned)

    def history(self, limit=None, oldest_first=True):
        async def gen():
            for m in list(self.history_msgs):
                yield m
        return gen()

    async def send(self, embeds=(), **kw):
        HistThread.next_id += 1
        m = Msg(HistThread.next_id, embeds=embeds)
        self.sent.append(m)
        self.history_msgs.append(m)
        return m

    async def delete(self, reason=None):
        self.deleted = True

    async def edit(self, **kw):
        await super().edit(**kw)
        if "pinned" in kw:
            self.flags.pinned = kw["pinned"]


class Note:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeLetterboxd:
    def __init__(self):
        self.notes = {}

    async def _thread(self, tid):
        return SimpleNamespace(get_partial_message=lambda mid: self.notes.setdefault(mid, Note()))

    def _jump(self, cid, mid):
        return f"https://discord.com/channels/1/{cid}/{mid}"


class RebuildService(FakeService):
    async def _create_post(self, display, movies_url, poster_url, tags):
        self.created += 1
        t = HistThread(10000 + self.created, name=display)
        t.applied_tags = list(tags)
        self.bot.threads[t.id] = t
        return t, SimpleNamespace(id=t.id)


async def setup(tmp_path, *, notice=True, extra=(), pinned=False):
    svc, movies_post, db = await make_service(tmp_path)
    svc = RebuildService(svc.bot)
    svc.bot.letterboxd = FakeLetterboxd()
    old = HistThread(8000, name="Parasite (2019)", pinned=pinned)
    old.history_msgs = [Msg(8000, embeds=["starter"])]
    if notice:
        old.history_msgs.append(Msg(8001, type=T.channel_name_change))
    old.history_msgs += [Msg(8002, embeds=["review A"]), Msg(8003, embeds=["review B"])]
    old.history_msgs += list(extra)
    svc.bot.threads[old.id] = old
    await db.set_spoiler_post(496243, old.id, 8000)
    await add_review(db, "a", 496243, 8002)
    await add_review(db, "b", 496243, 8003)
    await db.conn.execute("UPDATE letterboxd_entries SET movies_message_id=600 WHERE guid='a'")
    await db.conn.commit()
    await db.set_meta("post_watch_starter_version", STARTER_VERSION)
    return svc, old, db


def test_foreign_messages_and_activity_order():
    hist = [Msg(1), Msg(2, type=T.channel_name_change), Msg(3), Msg(4, webhook=True),
            Msg(5, type=T.pins_add), Msg(6, type=T.reply)]
    assert [m.id for m in foreign_messages(hist, 1, {3})] == [4, 6]
    rows = [{"last_embed": None, "spoiler_thread_id": 50},
            {"last_embed": 90, "spoiler_thread_id": 10}, {"last_embed": 20, "spoiler_thread_id": 30}]
    assert [r["spoiler_thread_id"] for r in sorted(rows, key=post_activity)] == [30, 50, 10]


def test_rebuild_recreates_post_with_embeds_links_and_pin(tmp_path):
    async def run():
        svc, old, db = await setup(tmp_path, pinned=True)
        await svc.rebuild_posts()
        row = await db.get_movie(496243)
        a, b = await db.get_entry("a"), await db.get_entry("b")
        done = await db.get_meta(REBUILD_KEY), await db.get_meta(REBUILD_OLD_KEY)
        new = svc.bot.threads[row["spoiler_thread_id"]]
        await svc.rebuild_posts()                         # done: nothing more is created
        await db.close()
        return svc, old, new, row, a, b, done
    svc, old, new, row, a, b, done = asyncio.run(run())
    assert old.deleted and svc.created == 1
    assert new.name == "Parasite (2019)" and row["spoiler_starter_message_id"] == new.id
    assert [m.embeds for m in new.sent] == [["review A"], ["review B"]]   # same order
    assert (a["spoiler_message_id"], b["spoiler_message_id"]) == (new.sent[0].id, new.sent[1].id)
    assert "reviews" in tag_names(new)
    assert new.flags.pinned                              # this week's pin moves over
    note = svc.bot.letterboxd.notes[600].edits[-1]["content"]
    assert f"/{new.id}/{new.sent[0].id}" in note
    assert svc.bot.movies.cards == [496243]              # card button points at the new post
    assert done == (REBUILD_VERSION, None)


def test_clean_post_is_left_alone(tmp_path):
    async def run():
        svc, old, db = await setup(tmp_path, notice=False)
        await svc.rebuild_posts()
        r = await db.get_meta(REBUILD_KEY)
        await db.close()
        return svc, old, r
    svc, old, r = asyncio.run(run())
    assert svc.created == 0 and not old.deleted and r == REBUILD_VERSION


def test_post_with_member_messages_is_kept_and_retried(tmp_path):
    async def run():
        svc, old, db = await setup(tmp_path, extra=[Msg(8004)])
        await svc.rebuild_posts()
        r = await db.get_meta(REBUILD_KEY)
        await db.close()
        return svc, old, r
    svc, old, r = asyncio.run(run())
    assert svc.created == 0 and not old.deleted
    assert r is None                                     # not marked done: looked at again


def test_waits_for_phase16_refresh(tmp_path):
    async def run():
        svc, old, db = await setup(tmp_path)
        await db.set_meta("post_watch_starter_version", None)
        await svc.rebuild_posts()
        await db.close()
        return svc
    assert asyncio.run(run()).created == 0


def test_reply_during_rebuild_keeps_old_post(tmp_path):
    async def run():
        svc, old, db = await setup(tmp_path)
        real_send = HistThread.send

        async def send_and_reply(self, **kw):
            old.history_msgs.append(Msg(8999))           # someone replies in the old post
            return await real_send(self, **kw)
        HistThread.send = send_and_reply
        try:
            await svc.rebuild_posts()
        finally:
            HistThread.send = real_send
        await db.close()
        return old
    assert not asyncio.run(run()).deleted


def test_resume_after_cut_off_rebuild(tmp_path):
    async def run():
        svc, old, db = await setup(tmp_path)
        new = HistThread(15000)   # older than anything sent below, like snowflakes
        svc.bot.threads[new.id] = new
        # Cut off after the new post was recorded and review A moved.
        await db.set_spoiler_post(496243, new.id, new.id)
        moved = await new.send(embeds=["review A"])
        await db.update_entry("a", spoiler_message_id=moved.id)
        await db.set_meta(REBUILD_OLD_KEY, json.dumps(
            {"tmdb_id": 496243, "old": old.id, "pinned": False}))
        await svc.rebuild_posts()
        a, b = await db.get_entry("a"), await db.get_entry("b")
        state = await db.get_meta(REBUILD_OLD_KEY)
        await db.close()
        return svc, old, new, a, b, state
    svc, old, new, a, b, state = asyncio.run(run())
    assert [m.embeds for m in new.sent] == [["review A"], ["review B"]]
    assert (a["spoiler_message_id"], b["spoiler_message_id"]) == (new.sent[0].id, new.sent[1].id)
    assert old.deleted and state is None and svc.created == 0
