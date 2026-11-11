"""Phase 4: Letterboxd feed parsing, spoiler-free wording, and the posting flow."""
import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest

from filmbuffs import letterboxd as lbmod
from filmbuffs.db import Database
from filmbuffs.letterboxd import (AlreadyLinked, FeedResult, LetterboxdService, feed_line,
                                  movies_note, normalize_username, parse_feed,
                                  pick_tmdb_match, review_to_markdown, spoiler_embed, stars)

# Synthetic feed in Letterboxd's real format (checked against live feeds 2026-11-11).
ITEM = """<item><title>{title}</title><link>https://letterboxd.com/sam/film/{slug}/</link>
<guid isPermaLink="false">{guid}</guid><pubDate>{pub}</pubDate>
{extra}<letterboxd:filmTitle>{film}</letterboxd:filmTitle>
<letterboxd:filmYear>{year}</letterboxd:filmYear>{tmdb}
<description><![CDATA[ <p><img src="https://a.ltrbxd.com/p.jpg"/></p> {desc} ]]></description>
<dc:creator>Sam</dc:creator></item>"""


def item(guid, film="Parasite", year=2019, tmdb=496243, rating="4.5", like="Yes",
         rewatch="No", desc="<p>Watched on Monday November 9, 2026.</p>",
         pub="Tue, 10 Nov 2026 03:01:09 +1300"):
    extra = "<letterboxd:watchedDate>2026-11-09</letterboxd:watchedDate>"
    extra += f"<letterboxd:rewatch>{rewatch}</letterboxd:rewatch>"
    if rating:
        extra += f"<letterboxd:memberRating>{rating}</letterboxd:memberRating>"
    extra += f"<letterboxd:memberLike>{like}</letterboxd:memberLike>"
    return ITEM.format(title=f"{film}, {year}", slug=film.lower(), guid=guid, pub=pub,
                       extra=extra, film=film, year=year, desc=desc,
                       tmdb=f"<tmdb:movieId>{tmdb}</tmdb:movieId>" if tmdb else "")


def feed(*items):
    return ("<?xml version='1.0' encoding='utf-8'?><rss version=\"2.0\" "
            "xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:letterboxd=\"https://letterboxd.com\" "
            "xmlns:tmdb=\"https://themoviedb.org\"><channel><title>Letterboxd - Sam</title>"
            + "".join(items) + "</channel></rss>").encode()


LIST_ITEM = """<item><title>My list</title><link>https://letterboxd.com/sam/list/x/</link>
<guid isPermaLink="false">letterboxd-list-1</guid><pubDate>Tue, 10 Nov 2026 03:01:09 +1300</pubDate>
<description><![CDATA[ <p>a list</p> ]]></description></item>"""

REVIEW_HTML = ("<p><em>This review may contain spoilers.</em></p>"
               "<p>The <b>stairs</b> scene &amp; the *rain*.<br />Second line.</p>"
               '<p>See <a href="https://example.com/x">this</a>.</p>')


# ------------------------------------------------------------------ pure helpers

def test_parse_feed_skips_lists_and_reads_fields():
    entries = parse_feed(feed(
        item("letterboxd-watch-1", rewatch="Yes"),
        LIST_ITEM,
        item("letterboxd-review-2", film="Hen", year=2025, tmdb=1382513, rating=None,
             like="No", desc=REVIEW_HTML)))
    assert [e.guid for e in entries] == ["letterboxd-watch-1", "letterboxd-review-2"]
    w, r = entries
    assert (w.kind, w.tmdb_id, w.film_title, w.film_year) == ("watch", 496243, "Parasite", 2019)
    assert (w.rating, w.liked, w.rewatch, w.watched_date) == (4.5, True, True, "2026-11-09")
    assert w.review_text is None and w.published_at == "2026-11-09T14:01:09+00:00"
    assert w.url == "https://letterboxd.com/sam/film/parasite/"
    assert (r.kind, r.rating, r.liked, r.rewatch) == ("review", None, False, False)
    assert r.review_text and "stairs" in r.review_text


def test_review_markdown():
    md = review_to_markdown(REVIEW_HTML)
    assert "may contain spoilers" not in md          # warning is noise in a spoiler post
    assert "**stairs**" in md and "& the \\*rain\\*." in md   # member text is escaped
    assert "\nSecond line." in md
    assert "[this](https://example.com/x)" in md
    assert "img" not in md and "ltrbxd" not in md


def test_normalize_username():
    assert normalize_username("Sam_99") == "sam_99"
    assert normalize_username("@sam") == "sam"
    assert normalize_username("https://letterboxd.com/Sam/films/") == "sam"
    assert normalize_username("letterboxd.com/sam/rss/") == "sam"
    for bad in ("", "sam smith", "../etc", "sam?x=1"):
        assert normalize_username(bad) is None


def test_stars():
    assert stars(4.5) == "★★★★½" and stars(0.5) == "½" and stars(5.0) == "★★★★★"
    assert stars(None) == ""


def test_tmdb_fallback_only_takes_unambiguous_exact_match():
    res = [{"id": 1, "title": "Heat", "release_date": "1995-12-15"},
           {"id": 2, "title": "Heat", "release_date": "1986-03-14"},
           {"id": 3, "title": "Heat Wave", "release_date": "1995-01-01"}]
    assert pick_tmdb_match(res, "heat", 1995) == 1
    assert pick_tmdb_match(res, "Heat", None) is None       # ambiguous
    assert pick_tmdb_match(res, "Heat", 2001) is None


def test_spoiler_free_texts_never_show_rating_or_review():
    note = movies_note(7, "https://discord.com/channels/1/2/3", True, True)
    feed_msg = feed_line(7, "Parasite (2019)", "https://discord.com/channels/1/9")
    for text in (note, feed_msg):
        assert "★" not in text and "½" not in text and "♥" not in text
        assert "ewatch" not in text
    assert "<@7>" in note and "https://discord.com/channels/1/2/3" in note
    assert "Parasite (2019)" in feed_msg


def test_spoiler_embed_has_full_detail_and_fits():
    row = dict(film_title="Parasite", film_year=2019, rating=4.5, liked=1, rewatch=1,
               review_text="word " * 2000, letterboxd_url="https://letterboxd.com/sam/film/p/",
               username="sam", watched_date="2026-11-09")
    e = spoiler_embed(row, "Sam", None)
    assert "★★★★½" in e.description and "♥" in e.description and "Rewatch" in e.description
    assert len(e.description) <= 4096 and "full review" in e.description
    assert e.footer.text == "Watched Nov 9, 2026"
    unrated = spoiler_embed({**row, "rating": None, "liked": 0, "rewatch": 0,
                             "review_text": None}, "Sam", None)
    assert "No rating" in unrated.description


# ------------------------------------------------------------------ service

class FakeMessage:
    _next = 1000

    def __init__(self, channel, content=None, embed=None):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.channel, self.content, self.embed = channel, content, embed
        self.jump_url = f"https://discord.com/channels/1/{channel.id}/{self.id}"


class FakeChannel:
    def __init__(self, cid, fail_times=0):
        self.id = cid
        self.sent: list[FakeMessage] = []
        self.fail_times = fail_times
        self.jump_url = f"https://discord.com/channels/1/{cid}"

    async def send(self, content=None, *, embed=None, **kw):
        if self.fail_times:
            self.fail_times -= 1
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        m = FakeMessage(self, content, embed)
        self.sent.append(m)
        return m


class FakeMovies:
    def __init__(self, db):
        self.db, self.posts, self.created = db, {}, []

    async def ensure_post(self, movie, *, added_by, pitch, status, source):
        if movie.id in self.posts:
            return self.posts[movie.id], False
        ch = FakeChannel(5000 + movie.id)
        self.posts[movie.id] = ch
        self.created.append((movie.id, status, added_by))
        await self.db.insert_movie(tmdb_id=movie.id, thread_id=ch.id, starter_message_id=1,
                                   title=movie.title, year=movie.year, status=status, flags=[],
                                   genre_tags=[], added_by=added_by, pitch=None, source=source)
        return ch, True


class FakeSpoilers:
    def __init__(self, db):
        self.db, self.posts = db, {}

    async def open(self, tmdb_id):
        if tmdb_id not in self.posts:
            self.posts[tmdb_id] = FakeChannel(9000 + tmdb_id)
            await self.db.set_spoiler_post(tmdb_id, self.posts[tmdb_id].id, 1)
        return self.posts[tmdb_id], True


class FakeTMDB:
    async def movie(self, tmdb_id):
        return SimpleNamespace(id=tmdb_id, title=f"Film {tmdb_id}", year=2019,
                               display=f"Film {tmdb_id} (2019)")

    async def search(self, title, year=None):
        return []


MEMBER = SimpleNamespace(id=42, display_name="Sam",
                         display_avatar=SimpleNamespace(url="https://cdn/a.png"))


async def make_service(tmp_path, feeds, member=MEMBER):
    db = Database(tmp_path / "t.db")
    await db.connect()
    OPEN_DBS.append(db)
    feed_ch = FakeChannel(77)
    bot = SimpleNamespace(
        db=db, tmdb=FakeTMDB(), session=None,
        config=SimpleNamespace(channels={"letterboxd_feed": 77}, guild_id=1, lb_jitter=(0, 0)),
        get_channel=lambda cid: feed_ch if cid == 77 else None)
    bot.movies, bot.spoilers = FakeMovies(db), FakeSpoilers(db)
    svc = LetterboxdService(bot)

    async def fake_fetch(username, link=None):
        return FeedResult(parse_feed(feeds[username]))

    async def fake_member(uid):
        return member

    svc._fetch = fake_fetch
    svc._member = fake_member
    return svc, bot, feed_ch, db


OPEN_DBS: list = []


async def closing(run):
    try:
        await run()
    finally:
        while OPEN_DBS:
            await OPEN_DBS.pop().close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(*a, **k):
        return None
    monkeypatch.setattr(lbmod.asyncio, "sleep", instant)


def test_link_marks_existing_entries_seen_and_posts_nothing(tmp_path):
    async def run():
        feeds = {"sam": feed(item("letterboxd-watch-1"), item("letterboxd-watch-2", tmdb=27205))}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        assert await svc.link(42, "sam") == 2
        assert await svc.poll_user(42) == 0
        assert feed_ch.sent == [] and bot.movies.created == []
        row = await db.get_entry("letterboxd-watch-1")
        assert row["state"] == "baseline" and row["rating"] == 4.5   # still stored for stats
    asyncio.run(closing(run))


def test_new_entries_post_three_messages_in_order_once(tmp_path):
    async def run():
        feeds = {"sam": feed(item("letterboxd-watch-1"))}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        await svc.link(42, "sam")
        # Feed is newest first: 3 (newer, review of a new film) then 2.
        feeds["sam"] = feed(
            item("letterboxd-review-3", tmdb=27205, desc="<p>Loved the ending.</p>",
                 pub="Wed, 11 Nov 2026 03:00:00 +1300"),
            item("letterboxd-watch-2", pub="Tue, 10 Nov 2026 03:00:00 +1300"),
            item("letterboxd-watch-1"))
        assert await svc.poll_user(42) == 2
        assert await svc.poll_user(42) == 0                     # nothing twice

        # Auto-created posts start as Watched (solo), added by the member.
        assert bot.movies.created == [(496243, "watched_solo", 42), (27205, "watched_solo", 42)]
        spoiler = bot.spoilers.posts[27205].sent
        assert len(spoiler) == 1 and "Loved the ending." in spoiler[0].embed.description
        assert "★★★★½" in spoiler[0].embed.description
        note = bot.movies.posts[27205].sent
        assert len(note) == 1 and spoiler[0].jump_url in note[0].content
        assert "★" not in note[0].content and "Loved" not in note[0].content
        # Feed lines in logging order.
        assert [m.content for m in feed_ch.sent] == [
            feed_line(42, "Film 496243 (2019)", bot.movies.posts[496243].jump_url),
            feed_line(42, "Film 27205 (2019)", bot.movies.posts[27205].jump_url)]
        row = await db.get_entry("letterboxd-review-3")
        assert row["state"] == "posted" and row["feed_message_id"] == feed_ch.sent[1].id
    asyncio.run(closing(run))


def test_existing_backlog_post_is_reused_not_retagged(tmp_path):
    async def run():
        feeds = {"sam": feed()}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        existing = FakeChannel(1234)
        bot.movies.posts[496243] = existing
        await svc.link(42, "sam")
        feeds["sam"] = feed(item("letterboxd-watch-1"))
        assert await svc.poll_user(42) == 1
        assert bot.movies.created == [] and len(existing.sent) == 1
    asyncio.run(closing(run))


def test_failure_mid_way_resumes_without_duplicates(tmp_path):
    async def run():
        feeds = {"sam": feed()}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        await svc.link(42, "sam")
        feeds["sam"] = feed(item("letterboxd-watch-1"))
        feed_ch.fail_times = 1                                   # feed line fails once
        assert await svc.poll_user(42) == 0
        row = await db.get_entry("letterboxd-watch-1")
        assert row["state"] == "pending" and row["attempts"] == 1 and row["error"]
        assert await svc.poll_user(42) == 1
        assert len(bot.spoilers.posts[496243].sent) == 1         # not re-sent
        assert len(bot.movies.posts[496243].sent) == 1
        assert len(feed_ch.sent) == 1
    asyncio.run(closing(run))


def test_gives_up_after_max_attempts(tmp_path):
    async def run():
        feeds = {"sam": feed()}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        await svc.link(42, "sam")
        feeds["sam"] = feed(item("letterboxd-watch-1"))
        feed_ch.fail_times = 99
        for _ in range(lbmod.MAX_ATTEMPTS + 2):
            await svc.poll_user(42)
        row = await db.get_entry("letterboxd-watch-1")
        assert row["state"] == "failed" and row["attempts"] == lbmod.MAX_ATTEMPTS
    asyncio.run(closing(run))


def test_unmatched_and_member_left_are_not_posted(tmp_path):
    async def run():
        feeds = {"sam": feed()}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        await svc.link(42, "sam")
        feeds["sam"] = feed(item("letterboxd-watch-9", film="Obscure", tmdb=None))
        assert await svc.poll_user(42) == 0
        assert (await db.get_entry("letterboxd-watch-9"))["state"] == "unmatched"

        async def gone(uid):
            return None
        svc._member = gone
        feeds["sam"] = feed(item("letterboxd-watch-10"))
        assert await svc.poll_user(42) == 0
        assert (await db.get_entry("letterboxd-watch-10"))["state"] == "skipped"
        assert feed_ch.sent == []
    asyncio.run(closing(run))


def test_account_can_only_be_linked_once_and_unlink(tmp_path):
    async def run():
        feeds = {"sam": feed(item("letterboxd-watch-1"))}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        await svc.link(42, "sam")
        with pytest.raises(AlreadyLinked):
            await svc.link(43, "SAM")
        assert await svc.link(42, "sam") == 0                    # re-link is fine, no dupes
        assert await svc.unlink(42) and not await svc.unlink(42)
        assert await svc.poll_user(42) == 0
    asyncio.run(closing(run))


def test_poll_failures_alert_once_and_recover(tmp_path, caplog):
    """Phase 7: a feed that keeps failing logs one ERROR (→ #bot-logs) at the
    threshold, then one 'works again' notice; Letterboxd refusals stop the round."""
    async def run():
        feeds = {"sam": feed()}
        svc, bot, feed_ch, db = await make_service(tmp_path, feeds)
        await svc.link(42, "sam")
        good_fetch = svc._fetch
        status = {"code": 404}

        async def failing(username, link=None):
            raise lbmod.LetterboxdError("HTTP error", status["code"])
        svc._fetch = failing

        def errors():
            return [r for r in caplog.records if r.levelno >= 40]
        with caplog.at_level(logging.INFO):
            for _ in range(lbmod.ALERT_AFTER + 3):
                await svc.poll_all()
            assert len(errors()) == 1 and "failed 4 polls in a row" in errors()[0].getMessage()
            svc._fetch = good_fetch
            await svc.poll_all()
            assert [r.getMessage() for r in caplog.records
                    if getattr(r, "botlogs", False)] == ["Letterboxd feed sam (member 42) works again"]

            caplog.clear()
            status["code"] = 429
            svc._fetch = failing
            for _ in range(lbmod.ALERT_AFTER):
                await svc.poll_all()
            assert len(errors()) == 1 and "refused 4 polling rounds" in errors()[0].getMessage()
    asyncio.run(closing(run))
