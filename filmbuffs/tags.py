"""Which forum tags a movie post should carry.

Discord allows at most 5 tags per post. Status and flag tags always win;
genre tags fill whatever room is left (up to max_genre_tags). Tags are always
recomputed from the DB row, so adding e.g. Spoilers later may drop a genre.
"""
from __future__ import annotations

from .config import FLAG_TAGS, STATUS_TAGS

MAX_APPLIED_TAGS = 5


def genre_tag_keys(tmdb_genres: list[str], genre_map: dict[str, str]) -> list[str]:
    """Map TMDB genre names (in TMDB's order) to unique genre tag keys."""
    out: list[str] = []
    for g in tmdb_genres:
        key = genre_map.get(g)
        if key and key not in out:
            out.append(key)
    return out


def compose_tag_keys(status: str, flags: list[str] | set[str], genres: list[str],
                     max_genres: int = 3) -> list[str]:
    if status not in STATUS_TAGS:
        raise ValueError(f"unknown status {status!r}")
    keys = [status] + [f for f in FLAG_TAGS if f in flags]
    room = min(max_genres, MAX_APPLIED_TAGS - len(keys))
    keys += genres[:max(room, 0)]
    return keys


# ------------------------------------------------------------ #post-watch (Phase 16)

REVIEWS_TAG = "reviews"
DECADE_TAGS = ("d_pre1980", "d_1980s", "d_1990s", "d_2000s", "d_2010s", "d_2020s")
GENRE_TAG_PREFIX = "g_"


def decade_tag_key(year: int | None) -> str | None:
    """One decade tag per post: Pre-1980, 1980s … 2020s (later years count as 2020s
    until a newer tag is added). None when the year is unknown."""
    if not year:
        return None
    if year < 1980:
        return "d_pre1980"
    return f"d_{min(year, 2029) // 10 * 10}s"


def post_watch_tag_keys(year: int | None, genres: list[str], has_reviews: bool,
                        max_genres: int = 3) -> list[str]:
    """Tags for a post-watch post: 📓 Reviews, the decade, then up to max_genres
    genres (the #movies genre keys, in TMDB order). At most 5, so nothing is cut
    with the default 3 genres."""
    keys = [REVIEWS_TAG] if has_reviews else []
    decade = decade_tag_key(year)
    if decade:
        keys.append(decade)
    room = min(max_genres, MAX_APPLIED_TAGS - len(keys))
    keys += [g for g in genres if g.startswith(GENRE_TAG_PREFIX)][:max(room, 0)]
    return keys
