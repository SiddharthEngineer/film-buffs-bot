"""/watchlist import: a member's Letterboxd watchlist from their data export.

The bot can't read Letterboxd watchlists or lists itself (those pages and feeds
block bots), so members upload `watchlist.csv` or the whole export zip. Each
upload replaces their previous one. Films on it count as interest (📋) next to
👍 votes, for films that have a #movies post. No posts are created.
"""
from __future__ import annotations

import asyncio
import csv
import io
import zipfile

from .backfill import TitleResolver
from .tmdb import TMDBError

MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 5000


class WatchlistError(Exception):
    pass


def _rows_from_csv(text: str) -> list[tuple[str, int | None]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in {f.strip().lower() for f in reader.fieldnames}:
        raise WatchlistError("that CSV has no `Name` column; upload `watchlist.csv` from your "
                             "Letterboxd export (or the whole zip)")
    out = []
    for r in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if not r.get("name"):
            continue
        year = int(r["year"]) if r.get("year", "").isdigit() else None
        out.append((r["name"], year))
    return out


def parse_watchlist(data: bytes, filename: str) -> list[tuple[str, int | None]]:
    """(title, year) rows from a Letterboxd export zip or its watchlist.csv."""
    if len(data) > MAX_BYTES:
        raise WatchlistError("that file is too big for a Letterboxd export")
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise WatchlistError("that zip file can't be opened") from None
        with zf:
            names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "watchlist.csv"
                     and not n.startswith(("deleted/", "orphaned/"))]
            if not names:
                raise WatchlistError("that zip has no `watchlist.csv`; is it a Letterboxd export?")
            text = zf.read(min(names, key=len)).decode("utf-8-sig", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    rows = _rows_from_csv(text)
    if len(rows) > MAX_ROWS:
        raise WatchlistError(f"that watchlist has {len(rows)} films; the limit is {MAX_ROWS}")
    return list(dict.fromkeys(rows))


async def import_watchlist(db, tmdb, user_id: int, rows: list[tuple[str, int | None]]
                           ) -> tuple[list[tuple[int, str, int | None]], list[str]]:
    """Match rows to TMDB and replace the member's watchlist. Returns (matched, unmatched)."""
    resolver = TitleResolver(db, tmdb, concurrency=4)

    async def one(title: str, year: int | None):
        try:
            tid, how, _ = await resolver.resolve(title, year)
        except TMDBError:
            return title, year, None, "TMDB error"
        return title, year, tid, how

    results = await asyncio.gather(*(one(t, y) for t, y in rows))
    matched = [(tid, t, y) for t, y, tid, _ in results if tid]
    unmatched = [f"{t} ({y})" if y else t for t, y, tid, _ in results if not tid]
    await db.replace_watchlist(user_id, matched, len(rows), unmatched)
    return matched, unmatched
