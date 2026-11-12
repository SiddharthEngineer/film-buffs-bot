"""Phase 8: full Letterboxd sync from the export (diff, apply, edits, removals)."""
import asyncio
from types import SimpleNamespace

import discord
import pytest

import filmbuffs.sync as syncmod
from filmbuffs.backfill import TitleResolver, parse_export
from filmbuffs.letterboxd import LetterboxdService
from filmbuffs.sync import Filters, apply_sync, build_sync_plan, preview_lines

from test_backfill import CSVS, UID, FakeTMDB, make_zip, new_db, run

EXPORTED = "letterboxd-sam-2026-11-10-12-00-utc.zip"


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


class Msg:
    def __init__(self, mid, content=None, embed=None):
        self.id, self.content, self.embed = mid, content, embed


class Partial:
    def __init__(self, chan, mid):
        self.chan, self.id = chan, mid

    async def edit(self, content=None, embed=None, **kw):
        m = self.chan.msgs.get(self.id)
        if m is None:
            raise not_found()
        if content is not None:
            m.content = content
        if embed is not None:
            m.embed = embed
        self.chan.edits.append(self.id)

    async def delete(self):
        if self.chan.msgs.pop(self.id, None) is None:
            raise not_found()


class Chan:
    _next = 20_000

    def __init__(self, client, cid, archived=False):
        self.id, self.archived, self.msgs, self.edits = cid, archived, {}, []
        self.jump_url = f"https://discord.com/channels/1/{cid}"
        client.channels[cid] = self

    def add(self, mid, **kw):
        self.msgs[mid] = Msg(mid, **kw)

    async def send(self, content=None, *, embed=None, **kw):
        Chan._next += 1
        self.add(Chan._next, content=content, embed=embed)
        return self.msgs[Chan._next]

    async def edit(self, archived=None, **kw):
        if archived is not None:
            self.archived = archived

    def get_partial_message(self, mid):
        if self.archived:
            raise AssertionError("edited a message in an archived thread")
        return Partial(self, mid)


class Client:
    """Just enough of the bot for apply_sync and LetterboxdService."""

    def __init__(self, db):
        self.db, self.tmdb = db, FakeTMDB()
        self.config = SimpleNamespace(guild_id=1, channels={"letterboxd_feed": 77})
        self.channels: dict[int, Chan] = {}
        self.created: list = []
        self.feed = Chan(self, 77)
        client = self

        class Movies:
            async def ensure_post(self, movie, *, added_by, pitch, status, source):
                if await db.get_movie(movie.id):
                    return client.channels[(await db.get_movie(movie.id))["thread_id"]], False
                client.created.append((movie.id, status, source))
                ch = Chan(client, 5000 + movie.id)
                await db.insert_movie(tmdb_id=movie.id, thread_id=ch.id, starter_message_id=1,
                                      title=movie.title, year=2000, status=status, flags=[],
                                      genre_tags=[], added_by=added_by, pitch=None, source=source)
                return ch, True

        class Spoilers:
            async def open(self, tid):
                row = await db.get_movie(tid)
                if row is None:
                    raise LookupError(tid)
                if row["spoiler_thread_id"]:
                    return client.channels[row["spoiler_thread_id"]], False
                ch = Chan(client, 9000 + tid)
                await db.set_spoiler_post(tid, ch.id, 1)
                return ch, True

        self.movies, self.spoilers = Movies(), Spoilers()
        self.letterboxd = LetterboxdService(self)
        member = SimpleNamespace(id=UID, display_name="Sam",
                                 display_avatar=SimpleNamespace(url="https://cdn/a.png"))

        async def get_member(uid):
            return member
        self.letterboxd._member = get_member

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise not_found()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(*a, **k):
        return None
    monkeypatch.setattr(syncmod.asyncio, "sleep", instant)


async def seed(db, client):
    """Before the first sync: the RSS poller saw Heat (baseline, with a review) and
    posted a Parasite rewatch live (embed, #movies note, feed line)."""
    await db.insert_movie(tmdb_id=496243, thread_id=500, starter_message_id=1, title="Parasite",
                          year=2019, status="backlog", flags=[], genre_tags=[], added_by=1,
                          pitch=None, source="addmovie")
    movies_post = Chan(client, 500, archived=True)
    spoiler_post = Chan(client, 600, archived=True)
    await db.set_spoiler_post(496243, 600, 1)
    base = dict(discord_user_id=UID, username="sam", source="rss", liked=0, rewatch=0,
                letterboxd_url="https://letterboxd.com/sam/film/x/")
    await db.insert_entry(dict(base, guid="letterboxd-review-1", kind="review", tmdb_id=949,
                               film_title="Heat", film_year=1995, watched_date="2026-11-09",
                               rating=4.0, review_text="Coffee shop scene (RSS).",
                               state="baseline", published_at="2026-11-09T20:00:00+00:00"))
    await db.insert_entry(dict(base, guid="letterboxd-watch-2", kind="watch", tmdb_id=496243,
                               film_title="Parasite", film_year=2019, watched_date="2025-06-10",
                               rating=5.0, state="posted",
                               published_at="2025-06-10T20:00:00+00:00"))
    spoiler_post.add(999, embed="old embed")
    movies_post.add(888, content="old note")
    client.feed.add(777, content="feed line")
    await db.update_entry("letterboxd-watch-2", spoiler_message_id=999, movies_message_id=888,
                          feed_message_id=777, embed_source="feed")


async def plan(db, tmp_path, files=CSVS, name=EXPORTED, filters=Filters()):
    ex = parse_export(make_zip(tmp_path / name, files))
    return await build_sync_plan(db, TitleResolver(db, FakeTMDB()), ex, UID, "sam", filters)


def edited(files, name, old, new):
    out = dict(files)
    out[name] = out[name].replace(old, new)
    assert out[name] != files[name]
    return out


def test_first_sync_diff_and_rss_matching(tmp_path):
    async def go():
        db = await new_db(tmp_path)
        await seed(db, Client(db))
        p = await plan(db, tmp_path)
        c = p.counts()
        # Heat's RSS entry ↔ d3 (review edited since), Parasite rewatch ↔ d2 (♥ + rewatch
        # missing in the feed copy): matched by film + watched date.
        assert c["linked_rss"] == 2 and c["diary_changed"] == 2
        assert c["diary_new"] == 2 and c["watched_new"] == 1 and c["diary_removed"] == 0
        assert [r.title for r, _, _ in p.unmatched] == ["Mystery Film"]
        assert p.new_posts == {949: ("watched_solo", "2026-11-09"),
                               194: ("watched_solo", "2022-02-02"),
                               693134: ("backlog", "2025-01-01")}
        assert [(i.row.title, i.row.date) for i in p.reviews] == [
            ("Parasite", "2024-03-01"), ("Heat", "2026-11-09")]
        assert [(i.row.uri, sorted(i.changes)) for i in p.edits] == [
            ("https://boxd.it/d2", ["liked", "rewatch"])]
        assert c["watchlist_films"] == 1 and c["watchlist_added"] == 1
        text = "\n".join(preview_lines(p))
        assert "2 new · 2 changed" in text and "Unmatched on TMDB:** 1 films" in text
        assert "3 #movies posts to create (Watched (solo) 2, Backlog 1)" in text
    run(go)


def test_apply_then_resync_does_nothing(tmp_path):
    async def go():
        db = await new_db(tmp_path)
        client = Client(db)
        await seed(db, client)
        p = await plan(db, tmp_path)
        res = await apply_sync(client, p, via="test")
        assert res.failed == 0 and res.posts_created == 3 and res.reviews_posted == 2
        assert res.embeds_edited == 1
        assert sorted(client.created) == [(194, "watched_solo", "letterboxd-sync"),
                                          (949, "watched_solo", "letterboxd-sync"),
                                          (693134, "backlog", "letterboxd-sync")]
        # Heat's review: the export's (edited) text, as history.
        heat = await db.get_entry("letterboxd-review-1")
        assert heat["state"] == "imported" and heat["export_uri"] == "https://boxd.it/d3"
        assert heat["review_text"] == "Coffee shop scene." and heat["embed_source"] == "history"
        embed = client.channels[9949].msgs[heat["spoiler_message_id"]].embed
        assert "Coffee shop scene." in embed.description and "imported" in embed.footer.text
        # The live Parasite entry: embed and note edited in place (thread un-archived).
        rw = await db.get_entry("letterboxd-watch-2")
        assert rw["liked"] == 1 and rw["rewatch"] == 1 and rw["updated_at"]
        post = client.channels[600]
        assert post.edits == [999] and not post.archived
        assert "♥" in post.msgs[999].embed.description
        assert "imported" not in post.msgs[999].embed.footer.text
        assert client.channels[500].edits == [888]
        assert "Their rating is" in client.channels[500].msgs[888].content
        assert len(client.feed.msgs) == 1                   # nothing new in the feed
        unmatched = await db.get_entry(f"export:{UID}:watch:https://boxd.it/d4")
        assert unmatched["state"] == "unmatched"
        assert (await db.last_sync(UID))["via"] == "test"

        again = await plan(db, tmp_path)
        assert again.nothing_to_do, again.counts()
        res = await apply_sync(client, again, via="test")
        assert res.stored == res.updated == res.reviews_posted == res.posts_created == 0
    run(go)


def test_edits_and_deletions_on_letterboxd(tmp_path):
    async def go():
        db = await new_db(tmp_path)
        client = Client(db)
        await seed(db, client)
        await apply_sync(client, await plan(db, tmp_path), via="test")
        d1 = await db.get_entry(f"export:{UID}:review:https://boxd.it/d1")
        d1_msg = d1["spoiler_message_id"]
        para = client.channels[600]
        n_msgs = len(para.msgs)

        # On Letterboxd: d1's review and rating edited, the d2 rewatch deleted.
        files = edited(CSVS, "reviews.csv", "So good.", "Even better the second time.")
        files = edited(files, "diary.csv", "2024-03-02,Parasite,2019,https://boxd.it/d1,4.5",
                       "2024-03-02,Parasite,2019,https://boxd.it/d1,5")
        files = edited(files, "diary.csv",
                       "2025-06-10,Parasite,2019,https://boxd.it/d2,5,Yes,,2025-06-10\n", "")
        p = await plan(db, tmp_path, files, "letterboxd-sam-2026-11-12-12-00-utc.zip")
        c = p.counts()
        assert (c["diary_new"], c["diary_changed"], c["diary_removed"]) == (0, 1, 1)
        assert [i.row.uri for i in p.edits] == ["https://boxd.it/d1"]
        assert sorted(p.edits[0].changes) == ["rating", "review_text"]
        assert [e["guid"] for e in p.deletes] == ["letterboxd-watch-2"]
        assert not p.reviews and not p.new_posts

        res = await apply_sync(client, p, via="test")
        assert res.failed == 0 and res.embeds_edited == 1 and res.messages_deleted == 3
        # Edited in place: same message, new text; nothing reposted.
        assert para.msgs[d1_msg].embed.description.startswith("**★★★★★")
        assert "Even better" in para.msgs[d1_msg].embed.description
        # The rewatch: all three bot messages gone, row kept as removed.
        assert 999 not in para.msgs and 888 not in client.channels[500].msgs
        assert 777 not in client.feed.msgs
        assert len(para.msgs) == n_msgs - 1
        rw = await db.get_entry("letterboxd-watch-2")
        assert rw["state"] == "removed" and rw["removed_at"]
        assert rw["spoiler_message_id"] is rw["movies_message_id"] is rw["feed_message_id"] is None
        assert (await plan(db, tmp_path, files, "letterboxd-sam-2026-11-12-12-00-utc.zip")
                ).nothing_to_do
    run(go)


def test_never_removes_newer_or_unmatched_rss_entries(tmp_path):
    async def go():
        db = await new_db(tmp_path)
        client = Client(db)
        await seed(db, client)
        await apply_sync(client, await plan(db, tmp_path), via="test")
        base = dict(discord_user_id=UID, username="sam", source="rss", kind="watch", liked=0,
                    rewatch=0, film_title="Film", film_year=2000, state="posted")
        # Logged after the export was made (and already linked by a later sync).
        await db.insert_entry(dict(base, guid="letterboxd-watch-8", tmdb_id=8,
                                   watched_date="2026-11-11", export_uri="https://boxd.it/n8",
                                   published_at="2026-11-11T09:00:00+00:00"))
        # Never matched to any export row (e.g. TMDB picked a different film).
        await db.insert_entry(dict(base, guid="letterboxd-watch-9", tmdb_id=9,
                                   watched_date="2026-01-01",
                                   published_at="2026-01-01T09:00:00+00:00"))
        p = await plan(db, tmp_path)               # exported 2026-11-10 12:00 UTC
        assert p.nothing_to_do and p.not_in_export == 1 and not p.removed
    run(go)


def test_watchlist_only_zip_never_removes_diary(tmp_path):
    async def go():
        db = await new_db(tmp_path)
        client = Client(db)
        await seed(db, client)
        await apply_sync(client, await plan(db, tmp_path), via="test")
        only_wl = {k: v for k, v in CSVS.items() if k in ("profile.csv", "watchlist.csv")}
        only_wl = edited(only_wl, "watchlist.csv", "\n2025-01-01,Dune",
                         "\n2025-01-01,Heat,1995,https://boxd.it/h\n2025-01-01,Dune")
        p = await plan(db, tmp_path, only_wl, "letterboxd-sam-2026-11-12-12-00-utc.zip")
        c = p.counts()
        assert c["diary_removed"] == c["watched_removed"] == 0 and not p.deletes
        assert c["watchlist_films"] == 2 and c["watchlist_added"] == 1
    run(go)


def test_mass_removal_is_flagged(tmp_path):
    async def go():
        db = await new_db(tmp_path)
        client = Client(db)
        rows = "".join(f"2024-01-{d:02d},Heat,1995,https://boxd.it/x{d},3,,,2024-01-{d:02d}\n"
                       for d in range(1, 11))
        files = edited(CSVS, "diary.csv", "2026-11-09,Heat", rows + "2026-11-09,Heat")
        await apply_sync(client, await plan(db, tmp_path, files), via="test")
        p = await plan(db, tmp_path, CSVS, "letterboxd-sam-2026-11-12-12-00-utc.zip")
        assert p.counts()["diary_removed"] == 10 and p.removal_warning()
        assert "⚠️ This removes **10**" in "\n".join(preview_lines(p))
    run(go)


def test_store_only_filter_touches_nothing_on_discord(tmp_path):
    async def go():
        db = await new_db(tmp_path)
        client = Client(db)
        await seed(db, client)
        p = await plan(db, tmp_path, filters=Filters(store_only=True))
        assert not p.new_posts and not p.reviews
        res = await apply_sync(client, p, via="test")
        assert res.stored == 4 and not client.created and client.channels[600].edits == []
    run(go)


def test_final_note_is_a_new_message_not_just_an_edit(monkeypatch):
    """A dismissed ephemeral message still accepts edits, so the note must be sent anew."""
    import filmbuffs.cogs.letterboxd as cogmod
    sent = {"edit": [], "followup": [], "dm": []}

    async def edit(content=None, **kw):
        sent["edit"].append(content)

    async def followup(text, **kw):
        sent["followup"].append(text)

    async def dm(text):
        sent["dm"].append(text)
    inter = SimpleNamespace(edit_original_response=edit, followup=SimpleNamespace(send=followup),
                            user=SimpleNamespace(id=UID, send=dm))
    cog = cogmod.Letterboxd(SimpleNamespace())
    asyncio.run(cog._final(inter, cogmod.time.monotonic(), "✅ done\ndetails"))
    assert sent["followup"] == ["✅ done\ndetails"] and not sent["dm"] and sent["edit"]
    # Past the interaction window: by DM.
    asyncio.run(cog._final(inter, cogmod.time.monotonic() - 3600, "late"))
    assert sent["dm"] == ["late"]
