"""Bot database (same SQLite file as the setup state). All times stored as UTC ISO-8601."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

# Append-only list of migrations; index + 1 is the schema version.
MIGRATIONS = [
    """
    CREATE TABLE movies (
        tmdb_id                     INTEGER PRIMARY KEY,
        thread_id                   INTEGER NOT NULL UNIQUE,
        starter_message_id          INTEGER NOT NULL,
        title                       TEXT NOT NULL,
        year                        INTEGER,
        status                      TEXT NOT NULL DEFAULT 'backlog',
        flags                       TEXT NOT NULL DEFAULT '[]',   -- JSON: spoilers, in_theaters, streaming
        genre_tags                  TEXT NOT NULL DEFAULT '[]',   -- JSON: genre tag keys, TMDB order
        added_by                    INTEGER,                      -- Discord user ID
        added_at                    TEXT NOT NULL,
        pitch                       TEXT,
        source                      TEXT NOT NULL,                -- addmovie | letterboxd | backfill
        spoiler_marker_message_id   INTEGER,                      -- first spoiler message
        spoiler_divider_message_id  INTEGER,
        spoiler_marked_at           TEXT
    );
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self._migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _migrate(self) -> None:
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = await (await self.conn.execute("SELECT MAX(version) FROM schema_version")).fetchone()
        current = row[0] or 0
        for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            await self.conn.executescript(sql)
            await self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (i,))
        await self.conn.commit()

    # movies ----------------------------------------------------------------
    async def get_movie(self, tmdb_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,))
        return await cur.fetchone()

    async def get_movie_by_thread(self, thread_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM movies WHERE thread_id=?", (thread_id,))
        return await cur.fetchone()

    async def insert_movie(self, *, tmdb_id: int, thread_id: int, starter_message_id: int,
                           title: str, year: int | None, status: str, flags: list[str],
                           genre_tags: list[str], added_by: int | None, pitch: str | None,
                           source: str) -> None:
        await self.conn.execute(
            "INSERT INTO movies(tmdb_id, thread_id, starter_message_id, title, year, status, "
            "flags, genre_tags, added_by, added_at, pitch, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tmdb_id, thread_id, starter_message_id, title, year, status, json.dumps(flags),
             json.dumps(genre_tags), added_by, utcnow(), pitch, source))
        await self.conn.commit()

    async def delete_movie(self, tmdb_id: int) -> None:
        await self.conn.execute("DELETE FROM movies WHERE tmdb_id=?", (tmdb_id,))
        await self.conn.commit()
