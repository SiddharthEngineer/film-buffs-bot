import pytest

from filmbuffs.tags import MAX_APPLIED_TAGS, compose_tag_keys, genre_tag_keys

GENRE_MAP = {"Action": "g_action", "War": "g_action", "Drama": "g_drama",
             "History": "g_drama", "Science Fiction": "g_scifi", "Thriller": "g_thriller",
             "Comedy": "g_comedy"}


def test_genres_keep_tmdb_order_and_dedupe():
    assert genre_tag_keys(["War", "Drama", "Action", "History", "Western?"], GENRE_MAP) == [
        "g_action", "g_drama"]


def test_backlog_gets_up_to_max_genres():
    genres = ["g_action", "g_drama", "g_scifi", "g_thriller"]
    assert compose_tag_keys("backlog", [], genres) == ["backlog", "g_action", "g_drama", "g_scifi"]


def test_flags_take_priority_over_genres_and_cap_is_five():
    genres = ["g_action", "g_drama", "g_scifi", "g_thriller"]
    keys = compose_tag_keys("watched", {"spoilers", "streaming", "in_theaters"}, genres)
    assert keys == ["watched", "spoilers", "in_theaters", "streaming", "g_action"]
    assert len(keys) <= MAX_APPLIED_TAGS


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        compose_tag_keys("bogus", [], [])
