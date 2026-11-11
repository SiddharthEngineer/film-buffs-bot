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
    # Phase 3R: spoiler discussions are separate posts in #movie-spoilers. The old
    # spoiler-line columns stay (unused). card_version lets the bot refresh old
    # info cards (button, no "Spoiler line" field) once at startup.
    """
    ALTER TABLE movies ADD COLUMN spoiler_thread_id INTEGER;
    ALTER TABLE movies ADD COLUMN spoiler_starter_message_id INTEGER;
    ALTER TABLE movies ADD COLUMN card_version INTEGER NOT NULL DEFAULT 0;
    CREATE UNIQUE INDEX movies_spoiler_thread ON movies(spoiler_thread_id);
    UPDATE movies SET spoiler_marker_message_id = NULL, spoiler_divider_message_id = NULL,
                      spoiler_marked_at = NULL,
                      flags = (SELECT json_group_array(value) FROM json_each(movies.flags)
                               WHERE value != 'spoilers');
    """,
    # Phase 4: Letterboxd. One linked account per member. Every feed entry the bot
    # sees is kept with full detail (for stats), including ones it never posted.
    #   state: pending  -> still to post (retried each poll)
    #          posted   -> all three messages sent
    #          baseline -> already in the feed when the account was linked
    #          unmatched / skipped / failed -> not posted (see error)
    """
    CREATE TABLE letterboxd_links (
        discord_user_id   INTEGER PRIMARY KEY,
        username          TEXT NOT NULL UNIQUE COLLATE NOCASE,
        linked_at         TEXT NOT NULL,
        etag              TEXT,
        last_modified     TEXT,
        last_checked_at   TEXT,
        last_ok_at        TEXT,
        last_error        TEXT
    );
    CREATE TABLE letterboxd_entries (
        guid                TEXT PRIMARY KEY,
        discord_user_id     INTEGER NOT NULL,
        username            TEXT NOT NULL,
        kind                TEXT NOT NULL,          -- watch | review
        tmdb_id             INTEGER,
        film_title          TEXT,
        film_year           INTEGER,
        watched_date        TEXT,                   -- YYYY-MM-DD (the member's own date)
        rating              REAL,                   -- 0.5..5.0, NULL = unrated
        liked               INTEGER NOT NULL DEFAULT 0,
        rewatch             INTEGER NOT NULL DEFAULT 0,
        review_text         TEXT,                   -- Discord markdown
        letterboxd_url      TEXT,
        published_at        TEXT,                   -- UTC
        seen_at             TEXT NOT NULL,          -- UTC
        source              TEXT NOT NULL,          -- rss | backfill
        state               TEXT NOT NULL,
        attempts            INTEGER NOT NULL DEFAULT 0,
        error               TEXT,
        movies_message_id   INTEGER,
        spoiler_message_id  INTEGER,
        feed_message_id     INTEGER
    );
    CREATE INDEX letterboxd_entries_user ON letterboxd_entries(discord_user_id, published_at);
    CREATE INDEX letterboxd_entries_tmdb ON letterboxd_entries(tmdb_id);
    CREATE INDEX letterboxd_entries_state ON letterboxd_entries(state);
    """,
    # Phase 6: backfill from a Letterboxd export zip. export_uri is the row's
    # "Letterboxd URI" (a diary entry for diary/reviews, the film for watched/
    # watchlist); RSS entries matched to an export row get it too, so re-runs
    # never store or post the same thing twice. logged_date = the export's "Date".
    # kind gains: watched (no diary entry), watchlist. state gains: imported.
    # tmdb_title_matches caches title+year -> TMDB lookups (hits only).
    """
    ALTER TABLE letterboxd_entries ADD COLUMN export_uri TEXT;
    ALTER TABLE letterboxd_entries ADD COLUMN logged_date TEXT;
    CREATE INDEX letterboxd_entries_export ON letterboxd_entries(discord_user_id, export_uri);
    CREATE TABLE tmdb_title_matches (
        title_key    TEXT PRIMARY KEY,      -- normalized "title|year"
        tmdb_id      INTEGER NOT NULL,
        how          TEXT NOT NULL,         -- exact | exact±1 | only result | override
        resolved_at  TEXT NOT NULL
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

    async def set_flags(self, tmdb_id: int, flags: list[str]) -> None:
        await self.conn.execute("UPDATE movies SET flags=? WHERE tmdb_id=?",
                                (json.dumps(sorted(set(flags))), tmdb_id))
        await self.conn.commit()

    async def get_movie_by_spoiler_thread(self, thread_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM movies WHERE spoiler_thread_id=?",
                                      (thread_id,))
        return await cur.fetchone()

    async def set_spoiler_post(self, tmdb_id: int, thread_id: int | None,
                               starter_message_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE movies SET spoiler_thread_id=?, spoiler_starter_message_id=? WHERE tmdb_id=?",
            (thread_id, starter_message_id, tmdb_id))
        await self.conn.commit()

    async def movies_needing_card(self, version: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM movies WHERE card_version < ? ORDER BY added_at", (version,))
        return list(await cur.fetchall())

    async def set_card_version(self, tmdb_id: int, version: int) -> None:
        await self.conn.execute("UPDATE movies SET card_version=? WHERE tmdb_id=?",
                                (version, tmdb_id))
        await self.conn.commit()

    async def delete_movie(self, tmdb_id: int) -> None:
        await self.conn.execute("DELETE FROM movies WHERE tmdb_id=?", (tmdb_id,))
        await self.conn.commit()

    # letterboxd ------------------------------------------------------------
    async def get_link(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM letterboxd_links WHERE discord_user_id=?", (user_id,))
        return await cur.fetchone()

    async def get_link_by_username(self, username: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM letterboxd_links WHERE username=?", (username,))
        return await cur.fetchone()

    async def all_links(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM letterboxd_links ORDER BY linked_at")
        return list(await cur.fetchall())

    async def set_link(self, user_id: int, username: str) -> None:
        """Link (or re-link) a member. Feed cache headers reset with the account."""
        await self.conn.execute(
            "INSERT INTO letterboxd_links(discord_user_id, username, linked_at) VALUES (?,?,?) "
            "ON CONFLICT(discord_user_id) DO UPDATE SET username=excluded.username, "
            "linked_at=excluded.linked_at, etag=NULL, last_modified=NULL, last_error=NULL",
            (user_id, username, utcnow()))
        await self.conn.commit()

    async def delete_link(self, user_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM letterboxd_links WHERE discord_user_id=?", (user_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def record_poll(self, user_id: int, *, ok: bool, error: str | None = None,
                          etag: str | None = None, last_modified: str | None = None) -> None:
        now = utcnow()
        if ok:
            await self.conn.execute(
                "UPDATE letterboxd_links SET last_checked_at=?, last_ok_at=?, last_error=NULL, "
                "etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified) "
                "WHERE discord_user_id=?", (now, now, etag, last_modified, user_id))
        else:
            await self.conn.execute(
                "UPDATE letterboxd_links SET last_checked_at=?, last_error=? "
                "WHERE discord_user_id=?", (now, error, user_id))
        await self.conn.commit()

    async def known_guids(self, guids: list[str]) -> set[str]:
        if not guids:
            return set()
        marks = ",".join("?" * len(guids))
        cur = await self.conn.execute(
            f"SELECT guid FROM letterboxd_entries WHERE guid IN ({marks})", guids)
        return {r[0] for r in await cur.fetchall()}

    async def insert_entry(self, e: dict) -> bool:
        """Insert a feed entry; False if its GUID is already known."""
        cols = ("guid", "discord_user_id", "username", "kind", "tmdb_id", "film_title",
                "film_year", "watched_date", "rating", "liked", "rewatch", "review_text",
                "letterboxd_url", "published_at", "source", "state", "export_uri",
                "logged_date")
        cur = await self.conn.execute(
            f"INSERT OR IGNORE INTO letterboxd_entries({','.join(cols)}, seen_at) "
            f"VALUES ({','.join('?' * len(cols))}, ?)",
            tuple(e.get(c) for c in cols) + (utcnow(),))
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_entry(self, guid: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM letterboxd_entries WHERE guid=?", (guid,))
        return await cur.fetchone()

    async def pending_entries(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM letterboxd_entries WHERE discord_user_id=? AND state='pending' "
            "ORDER BY published_at, guid", (user_id,))
        return list(await cur.fetchall())

    _ENTRY_UPDATABLE = {"tmdb_id", "state", "attempts", "error", "movies_message_id",
                        "spoiler_message_id", "feed_message_id", "export_uri",
                        "review_text"}

    async def update_entry(self, guid: str, **fields) -> None:
        bad = set(fields) - self._ENTRY_UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {bad}")
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE letterboxd_entries SET {sets} WHERE guid=?",
                                (*fields.values(), guid))
        await self.conn.commit()

    async def user_entries(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM letterboxd_entries WHERE discord_user_id=? "
            "ORDER BY published_at, guid", (user_id,))
        return list(await cur.fetchall())

    async def get_title_match(self, key: str) -> tuple[int, str] | None:
        """(tmdb_id, how) for a cached title match."""
        cur = await self.conn.execute(
            "SELECT tmdb_id, how FROM tmdb_title_matches WHERE title_key=?", (key,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def set_title_match(self, key: str, tmdb_id: int, how: str) -> None:
        await self.conn.execute(
            "INSERT INTO tmdb_title_matches(title_key, tmdb_id, how, resolved_at) "
            "VALUES (?,?,?,?) ON CONFLICT(title_key) DO UPDATE SET tmdb_id=excluded.tmdb_id, "
            "how=excluded.how, resolved_at=excluded.resolved_at",
            (key, tmdb_id, how, utcnow()))
        await self.conn.commit()
