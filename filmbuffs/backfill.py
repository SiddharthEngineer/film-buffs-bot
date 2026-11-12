"""Letterboxd export: parse the zip and match its films to TMDB.

Used by the export sync (filmbuffs/sync.py: /letterboxd sync and backfill.py).
Everything here is testable without Discord.

Export layout (Letterboxd → Settings → Data → Export your data):
  profile.csv     Username, ...
  diary.csv       Date, Name, Year, Letterboxd URI, Rating, Rewatch, Tags, Watched Date
  reviews.csv     Date, Name, Year, Letterboxd URI, Rating, Rewatch, Review, Tags, Watched Date
  watched.csv     Date, Name, Year, Letterboxd URI
  ratings.csv     Date, Name, Year, Letterboxd URI, Rating
  watchlist.csv   Date, Name, Year, Letterboxd URI
  likes/films.csv Date, Name, Year, Letterboxd URI
Columns are looked up by name, so extra or reordered columns are fine.
diary/reviews URIs point at the diary entry; the others point at the film.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .letterboxd import review_to_markdown
from .tmdb import TMDBError, year_of

FILES = ("profile.csv", "diary.csv", "reviews.csv", "watched.csv", "ratings.csv",
         "watchlist.csv", "likes/films.csv")
WATCHED_KINDS = ("watch", "review", "watched")
# Rough seconds per Discord action, including the script's own pauses.
SECS_PER_POST = 2.5
SECS_PER_REVIEW = 1.5
SECS_PER_LOOKUP = 0.1


class ExportError(Exception):
    pass


# ------------------------------------------------------------------ parsing

def norm_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").casefold()
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("&", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", t).split())


def title_key(title: str, year: int | None) -> str:
    return f"{norm_title(title)}|{year or ''}"


@dataclass
class ExportRow:
    kind: str                       # watch | review | watched | watchlist
    title: str
    year: int | None
    uri: str | None
    logged_date: str | None         # the export's "Date"
    watched_date: str | None = None
    rating: float | None = None
    rewatch: bool = False
    liked: bool = False
    review_text: str | None = None

    @property
    def key(self) -> str:
        return title_key(self.title, self.year)

    @property
    def display(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def date(self) -> str:
        return self.watched_date or self.logged_date or ""

    def guid(self, user_id: int) -> str:
        ident = self.uri or f"{self.key}|{self.date}"
        return f"export:{user_id}:{self.kind}:{ident}"


@dataclass
class Export:
    username: str | None
    rows: list[ExportRow]
    file_counts: dict[str, int]
    # When the export was made (UTC ISO), from the zip's name
    # ("letterboxd-<user>-2026-11-11-16-08-utc.zip"), else the end of the newest
    # day logged in it. Entries the bot saw after this aren't expected in it.
    exported_at: str | None = None

    @property
    def has_diary(self) -> bool:
        """The export includes the diary/watched files (so missing entries mean deleted)."""
        return any(n in self.file_counts for n in ("diary.csv", "watched.csv"))


def _read_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]] | None:
    """Rows of `name` (case-insensitive headers), or None if the file is absent.
    Files under deleted/ or orphaned/ are never used."""
    match = None
    for n in zf.namelist():
        parts = n.lower().split("/")
        if parts[0] in ("deleted", "orphaned") or (len(parts) > 1 and parts[1] in ("deleted", "orphaned")):
            continue
        if n.lower() == name or n.lower().endswith("/" + name):
            if match is None or len(n) < len(match):   # prefer the top-level file
                match = n
    if match is None:
        return None
    text = zf.read(match).decode("utf-8-sig")
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        out.append({(k or "").strip().lower(): (v or "").strip() for k, v in r.items()})
    return out


def _year(v: str | None) -> int | None:
    return int(v) if v and v.strip().isdigit() else None


def _rating(v: str | None) -> float | None:
    try:
        r = float(v) if v else None
    except ValueError:
        return None
    return r if r and 0 < r <= 5 else None


def _date(v: str | None) -> str | None:
    v = (v or "").strip()
    return v[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", v) else None


def export_review_markdown(text: str) -> str | None:
    """Export reviews are text with newlines and some HTML (<i>, <b>, links)."""
    if not text or not text.strip():
        return None
    html = text.replace("\r\n", "\n").replace("\n", "<br>")
    return review_to_markdown(html) or None


_EXPORT_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-utc", re.IGNORECASE)


def exported_at_from_name(name: str | None) -> str | None:
    m = _EXPORT_NAME_RE.search(name or "")
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:59+00:00"


def parse_export(path: str | Path | bytes, filename: str | None = None) -> Export:
    """Parse an export zip (a path, or the zip's bytes plus its file name)."""
    if isinstance(path, (bytes, bytearray)):
        source, label = io.BytesIO(path), filename or "that file"
    else:
        source, label = path, str(path)
        filename = filename or Path(path).name
    try:
        zf = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, FileNotFoundError, IsADirectoryError) as e:
        raise ExportError(f"can't open {label}: {e}") from None
    with zf:
        data = {name: _read_csv(zf, name) for name in FILES}
    if all(data[n] is None for n in FILES[1:]):
        raise ExportError(f"{label} doesn't look like a Letterboxd export (no diary/watched CSVs)")
    counts = {n: len(rows) for n, rows in data.items() if rows is not None}
    username = None
    if data["profile.csv"]:
        username = (data["profile.csv"][0].get("username") or "").lower() or None

    def base(r: dict, kind: str) -> ExportRow:
        return ExportRow(kind=kind, title=r.get("name", ""), year=_year(r.get("year")),
                         uri=r.get("letterboxd uri") or None, logged_date=_date(r.get("date")))

    liked = {title_key(r.get("name", ""), _year(r.get("year"))) for r in data["likes/films.csv"] or []}
    ratings = {title_key(r.get("name", ""), _year(r.get("year"))): _rating(r.get("rating"))
               for r in data["ratings.csv"] or []}

    # Diary entries, with reviews merged in (same entry URI, else same film + date).
    diary: list[ExportRow] = []
    by_uri: dict[str, ExportRow] = {}
    for r in data["diary.csv"] or []:
        row = base(r, "watch")
        row.watched_date = _date(r.get("watched date")) or row.logged_date
        row.rating = _rating(r.get("rating"))
        row.rewatch = (r.get("rewatch") or "").lower() == "yes"
        diary.append(row)
        if row.uri:
            by_uri[row.uri] = row
    for r in data["reviews.csv"] or []:
        text = export_review_markdown(r.get("review", ""))
        if not text:
            continue
        uri = r.get("letterboxd uri") or None
        wd = _date(r.get("watched date"))
        target = by_uri.get(uri) if uri else None
        if target is None:
            k = title_key(r.get("name", ""), _year(r.get("year")))
            target = next((d for d in diary if d.key == k and d.review_text is None
                           and (wd is None or d.watched_date == wd)), None)
        if target is None:
            target = base(r, "review")
            target.watched_date = wd
            target.rating = _rating(r.get("rating"))
            target.rewatch = (r.get("rewatch") or "").lower() == "yes"
            diary.append(target)
        target.kind = "review"
        target.review_text = text
        if target.rating is None:
            target.rating = _rating(r.get("rating"))

    rows = list(diary)
    logged = {d.key for d in diary}
    for r in data["watched.csv"] or []:
        row = base(r, "watched")
        if row.key in logged:
            continue
        row.rating = ratings.get(row.key)
        rows.append(row)
        logged.add(row.key)
    for row in rows:
        row.liked = row.key in liked
    for r in data["watchlist.csv"] or []:
        rows.append(base(r, "watchlist"))
    rows.sort(key=lambda x: (x.date, x.title))
    exported = exported_at_from_name(filename)
    if exported is None:
        newest = max((r.logged_date for r in rows if r.logged_date), default=None)
        exported = f"{newest}T23:59:59+00:00" if newest else None
    return Export(username, rows, counts, exported)


# ------------------------------------------------------------------ TMDB matching
#
# The export has only title + year (no director, no TMDB ID), so matching is:
#   - "Same year" means the first release year or any release in that year
#     (Hamilton: Disney+ 2020, first release 2025 on TMDB).
#   1. a single exact title (or original title) from the same year, unless a
#      film within a year either way clearly outweighs it by votes;
#   2. the clear favourite by TMDB votes among exact titles within a year
#      (at least MIN_VOTES and VOTE_RATIO× the runner-up), e.g. It (2017):
#      20,880 votes vs 3, or Obsession (2025 festival, 2026 release);
#   3. a single exact title within a year, then within two years;
#   4. only if no exact title exists: the same rules ignoring a leading
#      "The/A/An" and a TMDB subtitle after a colon ("title variant").
# Anything else is left for the owner to pick from the unmatched CSV.

MIN_VOTES = 50
VOTE_RATIO = 5
# Cached by an earlier, looser rule; re-resolved under the current one.
STALE_HOWS = {"only result"}


def _votes(r: dict) -> int:
    return int(r.get("vote_count") or 0)


def choose_match(results: list[dict], title: str, year: int | None,
                 released_in_year: set[int] | frozenset = frozenset(),
                 variants: bool = True) -> tuple[int | None, str]:
    """(tmdb_id, how) for a confident match, else (None, reason).
    `released_in_year`: IDs TMDB says had some release in `year`."""
    t = norm_title(title)
    uniq = list({r["id"]: r for r in results}.values())
    names = lambda r: (r.get("title") or "", r.get("original_title") or "")
    exact = [r for r in uniq if t in map(norm_title, names(r))]
    if exact or not variants:
        return _choose(exact, year, released_in_year)
    # Fallback: "The School of Rock", "Glass Onion: A Knives Out Mystery".
    loose = loose_title(title)
    variants = [r for r in uniq if any(loose in (loose_title(n), loose_title(n.split(":")[0]))
                                       for n in names(r) if n)]
    tid, how = _choose(variants, year, released_in_year)
    return (tid, "title variant") if tid else (None, how)


def loose_title(title: str) -> str:
    t = norm_title(title)
    for article in ("the ", "a ", "an "):
        if t.startswith(article):
            return t[len(article):]
    return t


def _choose(exact: list[dict], year: int | None,
            released_in_year: set[int] | frozenset) -> tuple[int | None, str]:
    if not exact:
        return None, "no match"
    def favourite(pool):
        if len(pool) < 2:
            return None
        ranked = sorted(pool, key=_votes, reverse=True)
        top, second = _votes(ranked[0]), _votes(ranked[1])
        return ranked[0] if top >= MIN_VOTES and top >= VOTE_RATIO * second else None

    if year is None:
        if len(exact) == 1:
            return exact[0]["id"], "exact"
        fav = favourite(exact)
        return (fav["id"], "most votes") if fav else (None, "ambiguous")

    def dist(r):
        if r["id"] in released_in_year:
            return 0
        y = year_of(r.get("release_date"))
        return abs(y - year) if y is not None else 99

    same = [r for r in exact if dist(r) == 0]
    near = [r for r in exact if dist(r) <= 1]
    fav = favourite(near)
    if len(same) == 1 and (fav is None or fav is same[0]):
        return same[0]["id"], "exact"
    if fav is not None:
        return fav["id"], "most votes"
    if len(same) > 1 or len(near) > 1:
        return None, "ambiguous"
    if len(near) == 1:
        return near[0]["id"], "exact±1"
    far = [r for r in exact if dist(r) <= 2]
    if len(far) == 1:
        return far[0]["id"], "exact±2"
    return None, "no match"


def is_tv(results: list[dict], title: str, year: int | None) -> bool:
    t = norm_title(title)
    for r in results:
        if t in (norm_title(r.get("name") or ""), norm_title(r.get("original_name") or "")):
            y = year_of(r.get("first_air_date"))
            if year is None or (y is not None and abs(y - year) <= 1):
                return True
    return False


class TitleResolver:
    """title+year → TMDB ID. Hits are cached in the DB; misses are retried each run."""

    def __init__(self, db, tmdb, overrides: dict[str, int] | None = None, concurrency: int = 8):
        self.db, self.tmdb = db, tmdb
        self.overrides = overrides or {}
        self.sem = asyncio.Semaphore(concurrency)
        self.lookups = 0

    async def resolve(self, title: str, year: int | None) -> tuple[int | None, str, str]:
        """(tmdb_id, how, candidates). `candidates` is filled for misses only."""
        key = title_key(title, year)
        if key in self.overrides:
            await self.db.set_title_match(key, self.overrides[key], "override")
            return self.overrides[key], "override", ""
        cached = await self.db.get_title_match(key)
        if cached and cached[1] not in STALE_HOWS:
            return cached[0], cached[1], ""
        async with self.sem:
            self.lookups += 1
            results: list[dict] = []
            released: set[int] = set()
            if year:
                results += await self.tmdb.search(title, year)
                any_year = await self.tmdb.search(title, year, any_release=True)
                released = {r["id"] for r in results + any_year}
                results += any_year
            tid, how = choose_match(results, title, year, released, variants=False)
            if tid is None and how == "no match":
                results += await self.tmdb.search(title)
                tid, how = choose_match(results, title, year, released, variants=False)
            if tid is None and how == "no match":
                # An exact-name TV series beats a film that merely starts with
                # the title (Chernobyl ≠ "Chernobyl: Zone of Exclusion").
                if is_tv(await self.tmdb.search_tv(title), title, year):
                    how = "TV series (not supported)"
                else:
                    tid, how = choose_match(results, title, year, released)
            cands = "" if tid else await self.describe(results, title)
        if tid:
            await self.db.set_title_match(key, tid, how)
        return tid, how, cands

    async def describe(self, results: list[dict], title: str, n: int = 3) -> str:
        """Top candidates as `ID · Title (Year) · Director · N votes`, for picking by hand."""
        t = norm_title(title)
        uniq = list({r["id"]: r for r in results}.values())
        uniq.sort(key=lambda r: (t not in (norm_title(r.get("title") or ""),
                                           norm_title(r.get("original_title") or "")),
                                 -_votes(r)))
        out = []
        for r in uniq[:n]:
            try:
                movie = await self.tmdb.movie(r["id"])
            except TMDBError:
                movie = None
            director = ", ".join(movie.directors[:2]) if movie and movie.directors else "?"
            out.append(f"{r['id']} · {r.get('title')} ({(r.get('release_date') or '')[:4]}) · "
                       f"{director} · {_votes(r)} votes")
        return "; ".join(out)


def read_overrides(path: str | Path) -> dict[str, int]:
    """title_key → TMDB ID from a filled-in unmatched CSV (rows with a tmdb_id)."""
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            tid = (r.get("tmdb_id") or "").strip()
            m = re.search(r"themoviedb\.org/movie/(\d+)", tid)
            tid = m.group(1) if m else tid
            if tid.isdigit():
                out[title_key(r.get("title", ""), _year(r.get("year")))] = int(tid)
    return out


def write_unmatched(path: str | Path, unmatched: list[tuple[ExportRow, str, str]]) -> None:
    seen = set()
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["title", "year", "kinds", "reason", "tmdb_candidates", "letterboxd_uri",
                    "tmdb_id"])
        grouped: dict[str, list] = defaultdict(list)
        for row, reason, cands in unmatched:
            grouped[row.key].append((row, reason, cands))
        for key, group in grouped.items():
            if key in seen:
                continue
            seen.add(key)
            row, reason, cands = group[0]
            kinds = ",".join(sorted({r.kind for r, _, _ in group}))
            w.writerow([row.title, row.year or "", kinds, reason, cands, row.uri or "", ""])


__all__ = ["Export", "ExportError", "ExportRow", "TitleResolver", "TMDBError", "choose_match",
           "parse_export", "read_overrides", "write_unmatched", "WATCHED_KINDS"]
