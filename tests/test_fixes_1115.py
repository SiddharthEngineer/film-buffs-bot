"""Fixes of 2026-11-15: private replies without a view, masked post links,
/nominate doing /addmovie's work."""
import asyncio
from types import SimpleNamespace

from filmbuffs.cogs.movies import add_movie, post_note
from filmbuffs.pool import post_link
from filmbuffs.tmdb import Movie
from filmbuffs.vote_ui import send_private


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, **kwargs):
        # Like discord.py 2.7: a view of None crashes (view.is_finished()).
        if "view" in kwargs:
            assert kwargs["view"] is not None
        self.sent.append((content, kwargs))


def test_send_private_leaves_out_a_missing_view():
    inter = SimpleNamespace(response=FakeResponse())
    asyncio.run(send_private(inter, "You have no nominations.", None))
    asyncio.run(send_private(inter, "Your list", "VIEW"))
    (_, a), (_, b) = inter.response.sent
    assert "view" not in a and a["ephemeral"] is True
    assert b["view"] == "VIEW"


def test_post_link_is_a_masked_link_not_a_thread_mention():
    row = {"thread_id": 42, "title": "Film [Director's Cut]", "year": 2001}
    link = post_link(7, row)
    assert link == "[Film (Director's Cut) (2001)](https://discord.com/channels/7/42)"
    assert "<#" not in link
    assert post_link(7, row, "#movies post") == "[#movies post](https://discord.com/channels/7/42)"
    assert post_link(7, {"thread_id": None, "title": "X", "year": None}) == "*(post deleted)*"
    assert post_link(7, None) == "*(post deleted)*"
    # Markdown in titles doesn't leak into the message.
    assert post_link(7, {"thread_id": 1, "title": "*batteries*", "year": None}).startswith(
        r"[\*batteries\*]")


class FakeTMDB:
    async def movie(self, tmdb_id):
        return Movie(tmdb_id, "Heat", 1995, 170, "Plot", None, ["Crime"], ["Michael Mann"])


class FakeMovies:
    def __init__(self):
        self.calls = []

    async def ensure_post(self, movie, *, added_by, pitch, status, source):
        self.calls.append((movie.id, added_by, pitch, status, source))
        return SimpleNamespace(jump_url="https://discord.com/channels/1/99"), True


def test_add_movie_creates_the_post_with_the_pitch():
    bot = SimpleNamespace(tmdb=FakeTMDB(), movies=FakeMovies())
    movie, thread, created, err = asyncio.run(
        add_movie(bot, "member", "tmdb:949", "Best heist film", source="nominate"))
    assert err is None and created and movie.id == 949
    assert bot.movies.calls == [(949, "member", "Best heist film", "backlog", "nominate")]
    assert post_note(movie, thread, True, "x").startswith("✅ Added **Heat (1995)**")
    assert "pitch there" in post_note(movie, thread, False, "x")
    # Free text isn't a pick from the list: nothing is created.
    _, _, _, err = asyncio.run(add_movie(bot, "member", "heat", None, source="nominate"))
    assert err and "isn't a movie from the list" in err and len(bot.movies.calls) == 1
