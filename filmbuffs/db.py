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
    # Channels renamed to pre-/post-watch discussion; the "Post-watch" tag (flag
    # `spoilers`) is retired, so drop the flag. spoiler_thread_id still says
    # whether a post-watch discussion exists.
    """
    UPDATE movies SET flags = (SELECT json_group_array(value) FROM json_each(movies.flags)
                               WHERE value != 'spoilers');
    """,
    # Phase 5: votes, watchlists, weekly pick.
    #   votes: one row per member who 👍'd a movie's info card (bot excluded).
    #   watchlist_items: films on a member's uploaded Letterboxd watchlist (replaced
    #     on each /watchlist import; seeded from backfilled watchlist rows).
    #   Interest in a film = distinct members with a vote or a watchlist row.
    #   weeks: one row per watch week (week_start = its Monday, America/Chicago).
    #     state: ballot -> ballot_closed -> poll -> picked -> done, or skipped.
    #   nominations: one per member per ballot. week_options: the frozen ballot
    #     (poll answers), with interest at close and poll votes. poll_voters: who
    #     voted for what (stats). bot_meta: small key/value store.
    """
    CREATE INDEX movies_starter ON movies(starter_message_id);
    CREATE TABLE votes (
        tmdb_id   INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        voted_at  TEXT NOT NULL,
        PRIMARY KEY (tmdb_id, user_id)
    );
    CREATE TABLE watchlist_items (
        user_id      INTEGER NOT NULL,
        tmdb_id      INTEGER NOT NULL,
        title        TEXT,
        year         INTEGER,
        imported_at  TEXT NOT NULL,
        PRIMARY KEY (user_id, tmdb_id)
    );
    CREATE INDEX watchlist_items_tmdb ON watchlist_items(tmdb_id);
    CREATE TABLE watchlist_imports (
        user_id      INTEGER PRIMARY KEY,
        imported_at  TEXT NOT NULL,
        source       TEXT NOT NULL,          -- import | backfill | sync
        rows         INTEGER NOT NULL,
        matched      INTEGER NOT NULL,
        unmatched    TEXT NOT NULL DEFAULT '[]'   -- JSON: ["Title (Year)", ...]
    );
    INSERT OR IGNORE INTO watchlist_items(user_id, tmdb_id, title, year, imported_at)
        SELECT discord_user_id, tmdb_id, film_title, film_year, seen_at
        FROM letterboxd_entries WHERE kind = 'watchlist' AND tmdb_id IS NOT NULL;
    INSERT OR IGNORE INTO watchlist_imports(user_id, imported_at, source, rows, matched)
        SELECT discord_user_id, MAX(seen_at), 'backfill', COUNT(*), COUNT(tmdb_id)
        FROM letterboxd_entries WHERE kind = 'watchlist' GROUP BY discord_user_id;
    CREATE TABLE weeks (
        week_start          TEXT PRIMARY KEY,     -- YYYY-MM-DD, a Monday
        state               TEXT NOT NULL,
        ballot_message_id   INTEGER,
        ballot_opened_at    TEXT,
        ballot_closed_at    TEXT,
        poll_message_id     INTEGER,
        poll_opened_at      TEXT,
        poll_closed_at      TEXT,
        winner_tmdb_id      INTEGER,
        winner_votes        INTEGER,
        runner_up_votes     INTEGER,
        total_votes         INTEGER,
        event_id            INTEGER,
        announcement_id     INTEGER,
        log_message_id      INTEGER,
        skip_reason         TEXT,
        ended_at            TEXT
    );
    CREATE TABLE nominations (
        week_start    TEXT NOT NULL,
        user_id       INTEGER NOT NULL,
        tmdb_id       INTEGER NOT NULL,
        nominated_at  TEXT NOT NULL,
        PRIMARY KEY (week_start, user_id),
        UNIQUE (week_start, tmdb_id)
    );
    CREATE TABLE week_options (
        week_start    TEXT NOT NULL,
        position      INTEGER NOT NULL,       -- 1-based, = poll answer id
        tmdb_id       INTEGER NOT NULL,
        source        TEXT NOT NULL,          -- nomination | top
        nominated_by  INTEGER,
        interest      INTEGER NOT NULL,       -- at ballot close
        thumbs        INTEGER NOT NULL,
        poll_votes    INTEGER,
        PRIMARY KEY (week_start, position)
    );
    CREATE TABLE poll_voters (
        week_start  TEXT NOT NULL,
        tmdb_id     INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        PRIMARY KEY (week_start, tmdb_id, user_id)
    );
    CREATE TABLE bot_meta (
        key    TEXT PRIMARY KEY,
        value  TEXT
    );
    """,
    # Phase 5 follow-ups: admin override of a week's movie, pinning the week's
    # movie at watch start, and remembering the winner's status so an override
    # can put it back.
    """
    ALTER TABLE weeks ADD COLUMN override_by INTEGER;
    ALTER TABLE weeks ADD COLUMN winner_prev_status TEXT;
    ALTER TABLE weeks ADD COLUMN pinned_at TEXT;
    """,
    # Phase 8: each export upload (/letterboxd sync, backfill.py) is a full sync.
    #   state gains: removed (gone from Letterboxd; the row stays for stats, the
    #     bot's own messages for it are deleted) and merged (an RSS review-N item
    #     folded into the already-stored watch-N entry; kept so it isn't re-read).
    #   updated_at: last time a sync or the poller changed the row's details.
    #   embed_source: who posted the post-watch embed: feed (live poller) or
    #     history (backfill/sync, footer says "imported from Letterboxd history").
    #   letterboxd_syncs: one row per applied sync (counts as JSON), for stats.
    """
    ALTER TABLE letterboxd_entries ADD COLUMN updated_at TEXT;
    ALTER TABLE letterboxd_entries ADD COLUMN removed_at TEXT;
    ALTER TABLE letterboxd_entries ADD COLUMN embed_source TEXT;
    UPDATE letterboxd_entries
       SET embed_source = CASE WHEN movies_message_id IS NOT NULL THEN 'feed' ELSE 'history' END
     WHERE spoiler_message_id IS NOT NULL;
    CREATE TABLE letterboxd_syncs (
        id               INTEGER PRIMARY KEY,
        discord_user_id  INTEGER NOT NULL,
        username         TEXT NOT NULL,
        synced_at        TEXT NOT NULL,
        exported_at      TEXT,
        via              TEXT NOT NULL,          -- command | script
        counts           TEXT NOT NULL           -- JSON
    );
    """,
    # Phase 5R: nominations Mon–Thu, ranked-choice vote Fri–Sun, counted by
    # instant runoff. weeks.state: ballot -> ballot_closed -> voting -> picked ->
    # done (or skipped); the old `poll` state is gone. For ranked weeks the poll_*
    # columns are reused: poll_opened_at/poll_closed_at = voting opened/closed,
    # total_votes = members who ranked at least one film, winner_votes /
    # runner_up_votes = final-round counts, week_options.poll_votes = 1st choices.
    #   ranked_votes: one row per rank of each member's ballot (1 = best).
    #   vote_rounds: JSON list of rounds [{counts, out, exhausted}] (stats).
    """
    CREATE TABLE ranked_votes (
        week_start  TEXT NOT NULL,
        user_id     INTEGER NOT NULL,
        rank        INTEGER NOT NULL,
        tmdb_id     INTEGER NOT NULL,
        voted_at    TEXT NOT NULL,
        PRIMARY KEY (week_start, user_id, rank),
        UNIQUE (week_start, user_id, tmdb_id)
    );
    ALTER TABLE weeks ADD COLUMN vote_rounds TEXT;
    """,
    # Phase 9: In Theaters / Streaming from TMDB (US). availability is the last
    # classification as JSON {in_theaters, streaming: [provider names],
    # theatrical, digital}; the in_theaters/streaming flags in `flags` follow it.
    """
    ALTER TABLE movies ADD COLUMN availability TEXT;
    ALTER TABLE movies ADD COLUMN availability_checked_at TEXT;
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

    async def set_availability(self, tmdb_id: int, availability: str,
                               flags: list[str]) -> None:
        await self.conn.execute(
            "UPDATE movies SET availability=?, availability_checked_at=?, flags=? WHERE tmdb_id=?",
            (availability, utcnow(), json.dumps(sorted(set(flags))), tmdb_id))
        await self.conn.commit()

    async def all_movies(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM movies ORDER BY added_at")
        return list(await cur.fetchall())

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
                        "review_text", "kind", "rating", "liked", "rewatch", "watched_date",
                        "logged_date", "film_title", "film_year", "updated_at", "removed_at",
                        "embed_source"}

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

    async def record_sync(self, user_id: int, username: str, exported_at: str | None,
                          via: str, counts: dict) -> None:
        await self.conn.execute(
            "INSERT INTO letterboxd_syncs(discord_user_id, username, synced_at, exported_at, via, "
            "counts) VALUES (?,?,?,?,?,?)",
            (user_id, username, utcnow(), exported_at, via, json.dumps(counts)))
        await self.conn.commit()

    async def last_sync(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM letterboxd_syncs WHERE discord_user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,))
        return await cur.fetchone()

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

    # bot_meta --------------------------------------------------------------
    async def get_meta(self, key: str) -> str | None:
        cur = await self.conn.execute("SELECT value FROM bot_meta WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO bot_meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.conn.commit()

    # votes and interest ----------------------------------------------------
    async def get_movie_by_starter(self, message_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM movies WHERE starter_message_id=?",
                                      (message_id,))
        return await cur.fetchone()

    async def add_vote(self, tmdb_id: int, user_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO votes(tmdb_id, user_id, voted_at) VALUES (?,?,?)",
            (tmdb_id, user_id, utcnow()))
        await self.conn.commit()

    async def remove_vote(self, tmdb_id: int, user_id: int | None = None) -> None:
        """Remove one member's vote, or all votes for the movie (user_id None)."""
        if user_id is None:
            await self.conn.execute("DELETE FROM votes WHERE tmdb_id=?", (tmdb_id,))
        else:
            await self.conn.execute("DELETE FROM votes WHERE tmdb_id=? AND user_id=?",
                                    (tmdb_id, user_id))
        await self.conn.commit()

    async def set_votes(self, tmdb_id: int, user_ids: set[int]) -> None:
        """Make the stored votes match `user_ids` (keeps existing vote times)."""
        cur = await self.conn.execute("SELECT user_id FROM votes WHERE tmdb_id=?", (tmdb_id,))
        have = {r[0] for r in await cur.fetchall()}
        now = utcnow()
        await self.conn.executemany("DELETE FROM votes WHERE tmdb_id=? AND user_id=?",
                                    [(tmdb_id, u) for u in have - user_ids])
        await self.conn.executemany("INSERT INTO votes(tmdb_id, user_id, voted_at) VALUES (?,?,?)",
                                    [(tmdb_id, u, now) for u in user_ids - have])
        await self.conn.commit()

    # Interest = distinct members who 👍'd the post or have the film on their
    # uploaded watchlist. Ranked by interest, then 👍, then earliest added.
    _RANKED = """
        WITH interest AS (
            SELECT tmdb_id, user_id, 1 AS thumb, 0 AS listed FROM votes
            UNION ALL
            SELECT tmdb_id, user_id, 0, 1 FROM watchlist_items)
        SELECT m.*, COUNT(DISTINCT i.user_id) AS interest,
               COALESCE(SUM(i.thumb), 0) AS thumbs, COALESCE(SUM(i.listed), 0) AS listed
        FROM movies m LEFT JOIN interest i ON i.tmdb_id = m.tmdb_id
        {where}
        GROUP BY m.tmdb_id
        ORDER BY interest DESC, thumbs DESC, m.added_at ASC, m.tmdb_id ASC
    """

    async def ranked_movies(self, statuses: tuple[str, ...] | None = None,
                            genre: str | None = None, limit: int | None = None,
                            min_interest: int = 0) -> list[aiosqlite.Row]:
        conds, params = [], []
        if statuses:
            conds.append(f"m.status IN ({','.join('?' * len(statuses))})")
            params += statuses
        if genre:
            conds.append("EXISTS (SELECT 1 FROM json_each(m.genre_tags) WHERE value = ?)")
            params.append(genre)
        sql = self._RANKED.format(where=("WHERE " + " AND ".join(conds)) if conds else "")
        if min_interest:
            sql = f"SELECT * FROM ({sql}) WHERE interest >= ?"
            params.append(min_interest)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        cur = await self.conn.execute(sql, params)
        return list(await cur.fetchall())

    async def movie_interest(self, tmdb_id: int) -> tuple[int, int, int]:
        """(interest, thumbs, listed) for one movie."""
        cur = await self.conn.execute(
            f"SELECT interest, thumbs, listed FROM ({self._RANKED.format(where='WHERE m.tmdb_id = ?')})",
            (tmdb_id,))
        row = await cur.fetchone()
        return (row[0], row[1], row[2]) if row else (0, 0, 0)

    # watchlists ------------------------------------------------------------
    async def replace_watchlist(self, user_id: int, items: list[tuple[int, str, int | None]],
                                rows: int, unmatched: list[str], source: str = "sync") -> None:
        now = utcnow()
        await self.conn.execute("DELETE FROM watchlist_items WHERE user_id=?", (user_id,))
        await self.conn.executemany(
            "INSERT OR IGNORE INTO watchlist_items(user_id, tmdb_id, title, year, imported_at) "
            "VALUES (?,?,?,?,?)", [(user_id, t, title, year, now) for t, title, year in items])
        await self.conn.execute(
            "INSERT INTO watchlist_imports(user_id, imported_at, source, rows, matched, unmatched) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "imported_at=excluded.imported_at, source=excluded.source, rows=excluded.rows, "
            "matched=excluded.matched, unmatched=excluded.unmatched",
            (user_id, now, source, rows, len({i[0] for i in items}), json.dumps(unmatched)))
        await self.conn.commit()

    async def watchlist_without_posts(self, user_id: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT w.tmdb_id FROM watchlist_items w LEFT JOIN movies m ON m.tmdb_id = w.tmdb_id "
            "WHERE w.user_id=? AND m.tmdb_id IS NULL ORDER BY w.rowid", (user_id,))
        return [r[0] for r in await cur.fetchall()]

    async def clear_watchlist(self, user_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM watchlist_items WHERE user_id=?", (user_id,))
        await self.conn.execute("DELETE FROM watchlist_imports WHERE user_id=?", (user_id,))
        await self.conn.commit()
        return cur.rowcount

    async def watchlist_summary(self, user_id: int) -> tuple[aiosqlite.Row | None, int, int]:
        """(import row, films stored, films that have a #movies post)."""
        cur = await self.conn.execute("SELECT * FROM watchlist_imports WHERE user_id=?", (user_id,))
        imp = await cur.fetchone()
        cur = await self.conn.execute(
            "SELECT COUNT(*), COUNT(m.tmdb_id) FROM watchlist_items w "
            "LEFT JOIN movies m ON m.tmdb_id = w.tmdb_id WHERE w.user_id=?", (user_id,))
        n, posted = await cur.fetchone()
        return imp, n, posted

    # weekly pick -----------------------------------------------------------
    async def get_week(self, week_start: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM weeks WHERE week_start=?", (week_start,))
        return await cur.fetchone()

    async def weeks_in_state(self, *states: str) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            f"SELECT * FROM weeks WHERE state IN ({','.join('?' * len(states))}) "
            "ORDER BY week_start", states)
        return list(await cur.fetchall())

    async def create_week(self, week_start: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO weeks(week_start, state) VALUES (?, 'ballot')", (week_start,))
        await self.conn.commit()

    _WEEK_UPDATABLE = {"state", "ballot_message_id", "ballot_opened_at", "ballot_closed_at",
                       "poll_message_id", "poll_opened_at", "poll_closed_at", "winner_tmdb_id",
                       "winner_votes", "runner_up_votes", "total_votes", "event_id",
                       "announcement_id", "log_message_id", "skip_reason", "ended_at",
                       "override_by", "winner_prev_status", "pinned_at", "vote_rounds"}

    async def update_week(self, week_start: str, **fields) -> None:
        bad = set(fields) - self._WEEK_UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {bad}")
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE weeks SET {sets} WHERE week_start=?",
                                (*fields.values(), week_start))
        await self.conn.commit()

    async def nominations(self, week_start: str) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT n.*, m.title, m.year, m.status, m.thread_id FROM nominations n "
            "LEFT JOIN movies m ON m.tmdb_id = n.tmdb_id WHERE n.week_start=? "
            "ORDER BY n.nominated_at, n.rowid", (week_start,))
        return list(await cur.fetchall())

    async def nominate(self, week_start: str, user_id: int, tmdb_id: int) -> None:
        """Add or swap a member's nomination. Swapping keeps their place in line."""
        await self.conn.execute(
            "INSERT INTO nominations(week_start, user_id, tmdb_id, nominated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(week_start, user_id) DO UPDATE SET tmdb_id=excluded.tmdb_id",
            (week_start, user_id, tmdb_id, utcnow()))
        await self.conn.commit()

    async def withdraw_nomination(self, week_start: str, user_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM nominations WHERE week_start=? AND user_id=?", (week_start, user_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_options(self, week_start: str, options: list[dict]) -> None:
        await self.conn.execute("DELETE FROM week_options WHERE week_start=?", (week_start,))
        await self.conn.executemany(
            "INSERT INTO week_options(week_start, position, tmdb_id, source, nominated_by, "
            "interest, thumbs) VALUES (?,?,?,?,?,?,?)",
            [(week_start, i, o["tmdb_id"], o["source"], o.get("nominated_by"), o["interest"],
              o["thumbs"]) for i, o in enumerate(options, start=1)])
        await self.conn.commit()

    async def options(self, week_start: str) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT o.*, m.title, m.year, m.status, m.thread_id, m.added_at FROM week_options o "
            "LEFT JOIN movies m ON m.tmdb_id = o.tmdb_id WHERE o.week_start=? "
            "ORDER BY o.position", (week_start,))
        return list(await cur.fetchall())

    async def record_first_choices(self, week_start: str, first: dict[int, int]) -> None:
        await self.conn.executemany(
            "UPDATE week_options SET poll_votes=? WHERE week_start=? AND tmdb_id=?",
            [(n, week_start, tid) for tid, n in first.items()])
        await self.conn.commit()

    # ranked votes ------------------------------------------------------------
    async def ranking(self, week_start: str, user_id: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT tmdb_id FROM ranked_votes WHERE week_start=? AND user_id=? ORDER BY rank",
            (week_start, user_id))
        return [r[0] for r in await cur.fetchall()]

    async def add_rank(self, week_start: str, user_id: int, tmdb_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO ranked_votes(week_start, user_id, rank, tmdb_id, voted_at) "
            "SELECT ?, ?, COALESCE(MAX(rank), 0) + 1, ?, ? FROM ranked_votes "
            "WHERE week_start=? AND user_id=?",
            (week_start, user_id, tmdb_id, utcnow(), week_start, user_id))
        await self.conn.commit()

    async def undo_rank(self, week_start: str, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM ranked_votes WHERE week_start=? AND user_id=? AND rank = "
            "(SELECT MAX(rank) FROM ranked_votes WHERE week_start=? AND user_id=?)",
            (week_start, user_id, week_start, user_id))
        await self.conn.commit()

    async def clear_ranking(self, week_start: str, user_id: int) -> None:
        await self.conn.execute("DELETE FROM ranked_votes WHERE week_start=? AND user_id=?",
                                (week_start, user_id))
        await self.conn.commit()

    async def all_rankings(self, week_start: str) -> dict[int, list[int]]:
        cur = await self.conn.execute(
            "SELECT user_id, tmdb_id FROM ranked_votes WHERE week_start=? ORDER BY user_id, rank",
            (week_start,))
        out: dict[int, list[int]] = {}
        for uid, tid in await cur.fetchall():
            out.setdefault(uid, []).append(tid)
        return out

    async def voter_count(self, week_start: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM ranked_votes WHERE week_start=?", (week_start,))
        return (await cur.fetchone())[0]

    async def set_status(self, tmdb_id: int, status: str) -> None:
        await self.conn.execute("UPDATE movies SET status=? WHERE tmdb_id=?", (status, tmdb_id))
        await self.conn.commit()
