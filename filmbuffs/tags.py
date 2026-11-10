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
