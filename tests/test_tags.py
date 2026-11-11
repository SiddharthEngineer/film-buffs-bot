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
    keys = compose_tag_keys("watched", {"streaming", "in_theaters"}, genres)
    assert keys == ["watched", "in_theaters", "streaming", "g_action", "g_drama"]
    # The retired Post-watch flag never becomes a tag, even if an old row has it.
    assert "spoilers" not in compose_tag_keys("backlog", {"spoilers"}, genres)
    assert len(keys) <= MAX_APPLIED_TAGS


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        compose_tag_keys("bogus", [], [])


def test_card_reminder_is_last_and_replaces_old_fields():
    import discord
    from filmbuffs.movies import LEGACY_SPOILER_FIELD, REMINDER_NAME, set_reminder
    e = discord.Embed(title="x")
    e.add_field(name=LEGACY_SPOILER_FIELD, value="old")
    e.add_field(name="Pitch", value="p")
    e.add_field(name=REMINDER_NAME, value="older text")
    set_reminder(set_reminder(e))
    assert [f.name for f in e.fields] == ["Pitch", REMINDER_NAME]
    assert "Post-watch discussion" in e.fields[-1].value
