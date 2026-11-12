"""Phase 6: Letterboxd export backfill (parsing, TMDB matching, planning, apply)."""
import asyncio
import csv
import importlib.util
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from filmbuffs.backfill import (ExportError, TitleResolver, choose_match, exported_at_from_name,
                                norm_title, parse_export, read_overrides, title_key,
                                write_unmatched)
from filmbuffs.db import Database

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("backfill_script", ROOT / "backfill.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

UID = 42

CSVS = {
    "profile.csv": "Date Joined,Username,Given Name\n2020-01-01,Sam,Sam\n",
    "diary.csv": (
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
        "2024-03-02,Parasite,2019,https://boxd.it/d1,4.5,,,2024-03-01\n"
        "2025-06-10,Parasite,2019,https://boxd.it/d2,5,Yes,,2025-06-10\n"
        "2026-11-09,Heat,1995,https://boxd.it/d3,4,,,2026-11-09\n"
        "2023-01-05,Mystery Film,2001,https://boxd.it/d4,2,,,2023-01-05\n"),
    "reviews.csv": (
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n"
        '2024-03-02,Parasite,2019,https://boxd.it/d1,4.5,,"The <i>stairs</i>.\n\nSo good.",,2024-03-01\n'
        "2026-11-09,Heat,1995,https://boxd.it/d3,4,,Coffee shop scene.,,2026-11-09\n"),
    "watched.csv": (
        "Date,Name,Year,Letterboxd URI\n"
        "2024-03-02,Parasite,2019,https://boxd.it/f1\n"
        "2022-02-02,Amélie,2001,https://boxd.it/f2\n"),
    "ratings.csv": "Date,Name,Year,Letterboxd URI,Rating\n2022-02-02,Amélie,2001,https://boxd.it/f2,3.5\n",
    "watchlist.csv": "Date,Name,Year,Letterboxd URI\n2025-01-01,Dune: Part Two,2024,https://boxd.it/f3\n",
    "likes/films.csv": "Date,Name,Year,Letterboxd URI\n2024-03-02,Parasite,2019,https://boxd.it/f1\n",
    "deleted/diary.csv": "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
                         "2020-01-01,Gone,2000,https://boxd.it/x,1,,,2020-01-01\n",
}

TMDB_DB = {
    "parasite": [{"id": 496243, "title": "Parasite", "release_date": "2019-05-30"},
                 {"id": 1, "title": "Parasite", "release_date": "1982-03-12"}],
    "heat": [{"id": 949, "title": "Heat", "release_date": "1995-12-15"}],
    "amélie": [{"id": 194, "title": "Amélie", "original_title": "Le Fabuleux Destin d'Amélie Poulain",
                "release_date": "2001-04-25"}],
    "dune: part two": [{"id": 693134, "title": "Dune: Part Two", "release_date": "2024-02-27"}],
    "chernobyl": [{"id": 793445, "title": "Chernobyl: Zone of Exclusion", "release_date": "2019-02-01"}],
    "mystery film": [{"id": 7, "title": "Mystery Film", "release_date": "2001-01-01"},
                     {"id": 8, "title": "Mystery Film", "release_date": "2001-06-01"}],
}


def make_zip(path, files=CSVS, prefix=""):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(prefix + name, text)
    return path


class FakeTMDB:
    def __init__(self):
        self.calls = 0

    async def search(self, title, year=None, timeout=10, any_release=False):
        self.calls += 1
        res = TMDB_DB.get(title.casefold(), [])
        if any_release:
            return [r for r in res if year in r.get("releases", ())]
        return [r for r in res if year is None or r["release_date"].startswith(str(year))]

    async def search_tv(self, title, timeout=10):
        shows = {"WandaVision": "2021-01-15", "Chernobyl": "2019-05-06"}
        return [{"id": 1, "name": title, "first_air_date": shows[title]}] if title in shows else []

    async def movie(self, tid):
        return SimpleNamespace(id=tid, title=f"Film {tid}", year=2000, directors=["Dir"],
                               display=f"Film {tid} (2000)")


OPEN = []


async def new_db(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    OPEN.append(db)
    return db


def run(coro_fn):
    async def wrapper():
        try:
            await coro_fn()
        finally:
            while OPEN:
                await OPEN.pop().close()
    asyncio.run(wrapper())


# ------------------------------------------------------------------ parsing

def test_parse_merges_files(tmp_path):
    ex = parse_export(make_zip(tmp_path / "e.zip"))
    assert ex.username == "sam"
    assert ex.file_counts["diary.csv"] == 4 and "deleted/diary.csv" not in ex.file_counts
    by = {(r.kind, r.uri): r for r in ex.rows}
    p1 = by[("review", "https://boxd.it/d1")]
    assert p1.watched_date == "2024-03-01" and p1.rating == 4.5 and p1.liked
    assert p1.review_text == "The *stairs*.\n\nSo good."
    p2 = by[("watch", "https://boxd.it/d2")]
    assert p2.rewatch and p2.rating == 5 and p2.liked and p2.review_text is None
    # Parasite is in watched.csv too, but its diary entries cover it.
    assert not any(r.kind == "watched" and r.title == "Parasite" for r in ex.rows)
    am = by[("watched", "https://boxd.it/f2")]
    assert am.rating == 3.5 and not am.liked and am.watched_date is None
    assert by[("watchlist", "https://boxd.it/f3")].title == "Dune: Part Two"
    assert not any(r.title == "Gone" for r in ex.rows)      # deleted/ is ignored
    assert [r.date for r in ex.rows] == sorted(r.date for r in ex.rows)


def test_parse_handles_nested_folder_and_rejects_junk(tmp_path):
    ex = parse_export(make_zip(tmp_path / "n.zip", prefix="letterboxd-sam/"))
    assert len(ex.rows) == 6
    with pytest.raises(ExportError):
        parse_export(make_zip(tmp_path / "j.zip", {"readme.txt": "hi"}))
    with pytest.raises(ExportError):
        parse_export(tmp_path / "missing.zip")


def test_review_without_diary_entry_is_kept(tmp_path):
    files = dict(CSVS)
    files["reviews.csv"] += "2021-01-01,Heat,1995,https://boxd.it/r9,3,,Old review.,,\n"
    ex = parse_export(make_zip(tmp_path / "e.zip", files))
    assert any(r.uri == "https://boxd.it/r9" and r.kind == "review" for r in ex.rows)


# ------------------------------------------------------------------ matching

def test_norm_title():
    assert norm_title("Amélie") == norm_title("amelie")
    assert norm_title("Coyote vs. Acme") == "coyote vs acme"
    assert title_key("Heat", 1995) == "heat|1995"


def test_choose_match():
    res = TMDB_DB["parasite"]
    assert choose_match(res, "Parasite", 2019) == (496243, "exact")
    assert choose_match(res, "Parasite", 2020) == (496243, "exact±1")
    assert choose_match(res, "Parasite", None) == (None, "ambiguous")
    assert choose_match(TMDB_DB["mystery film"], "Mystery Film", 2001) == (None, "ambiguous")
    assert choose_match(TMDB_DB["amélie"], "Le Fabuleux Destin d'Amélie Poulain", 2001)[0] == 194
    # A lone non-matching title is never taken any more.
    assert choose_match([{"id": 5, "title": "Other", "release_date": "2010"}], "X", 2010) == (None, "no match")
    assert choose_match([], "X", 2010) == (None, "no match")


def r(i, title, date, votes=0, **kw):
    return {"id": i, "title": title, "release_date": date, "vote_count": votes, **kw}


def test_choose_match_votes_years_and_releases():
    # Clear favourite by votes (It 2017: 20,880 vs 3).
    it = [r(346364, "It", "2017-09-06", 20880), r(501007, "It", "2017-01-01", 3)]
    assert choose_match(it, "It", 2017) == (346364, "most votes")
    # Too close, or too few votes overall: left for the owner.
    assert choose_match([r(1, "Butterfly", "2024", 4), r(2, "ButterFly", "2024", 1)],
                        "Butterfly", 2024) == (None, "ambiguous")
    assert choose_match([r(1, "Odd", "2020", 400), r(2, "Odd", "2020", 100)],
                        "Odd", 2020) == (None, "ambiguous")
    # Favourite a year off (Obsession: 2025 festival, 2026 release).
    ob = [r(1339713, "Obsession", "2026-05-15", 6580), r(1502633, "Obsession", "2025", 1),
          r(1436161, "Obsession", "2025", 0)]
    assert choose_match(ob, "Obsession", 2025) == (1339713, "most votes")
    assert choose_match(ob[1:2], "Obsession", 2025) == (1502633, "exact")
    # A lone same-year match that is also the favourite is just "exact".
    assert choose_match(it[:1] + [r(9, "It", "2016", 2)], "It", 2017) == (346364, "exact")
    # Any release in the year counts as that year (Hamilton: Disney+ 2020).
    ham = [r(556574, "Hamilton", "2025-09-05", 174), r(17629, "Hamilton", "1998-01-30", 20)]
    assert choose_match(ham, "Hamilton", 2020, {556574}) == (556574, "exact")
    assert choose_match(ham, "Hamilton", 2020) == (None, "no match")
    # Title variants, only when there is no exact title.
    assert choose_match([r(1584, "The School of Rock", "2003-10-03", 6472)],
                        "School of Rock", 2003) == (1584, "title variant")
    assert choose_match([r(661374, "Glass Onion: A Knives Out Mystery", "2022-11-23", 7384)],
                        "Glass Onion", 2022) == (661374, "title variant")
    assert choose_match([r(1, "The Onion Field", "2022", 9)], "Onion", 2022) == (None, "no match")
    # Two years off, only when it's the single exact title.
    assert choose_match([r(999243, "Hell of a Summer", "2025-04-04", 204)],
                        "Hell of a Summer", 2023) == (999243, "exact±2")


def test_resolver_caches_hits_and_honours_overrides(tmp_path):
    async def go():
        db, tmdb = await new_db(tmp_path), FakeTMDB()
        r = TitleResolver(db, tmdb)
        assert (await r.resolve("Heat", 1995))[0] == 949
        calls = tmdb.calls
        assert (await TitleResolver(db, tmdb).resolve("Heat", 1995))[:2] == (949, "exact")
        assert tmdb.calls == calls
        assert (await r.resolve("Mystery Film", 2001))[0] is None
        o = TitleResolver(db, tmdb, {title_key("Mystery Film", 2001): 8})
        assert (await o.resolve("Mystery Film", 2001))[:2] == (8, "override")
    run(go)


def test_resolver_labels_tv_and_redoes_stale_cache(tmp_path):
    async def go():
        db, tmdb = await new_db(tmp_path), FakeTMDB()
        tid, how, cands = await TitleResolver(db, tmdb).resolve("WandaVision", 2021)
        assert tid is None and how == "TV series (not supported)"
        tid, how, _ = await TitleResolver(db, tmdb).resolve("Chernobyl", 2019)
        assert tid is None and how == "TV series (not supported)"
        _, how, cands = await TitleResolver(db, tmdb).resolve("Mystery Film", 2001)
        assert how == "ambiguous" and "7 · Mystery Film (2001) · Dir · 0 votes" in cands
        await db.set_title_match(title_key("Mystery Film", 2001), 7, "only result")
        assert (await TitleResolver(db, tmdb).resolve("Mystery Film", 2001))[0] is None
    run(go)


def test_unmatched_csv_round_trips_to_overrides(tmp_path):
    ex = parse_export(make_zip(tmp_path / "e.zip"))
    row = next(r for r in ex.rows if r.title == "Mystery Film")
    out = tmp_path / "u.csv"
    write_unmatched(out, [(row, "ambiguous", "7 Mystery Film (2001)")])
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert rows[0]["title"] == "Mystery Film" and rows[0]["tmdb_id"] == ""
    rows[0]["tmdb_id"] = "https://www.themoviedb.org/movie/8-mystery-film"
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert read_overrides(out) == {"mystery film|2001": 8}


def test_export_time_from_name_or_newest_entry(tmp_path):
    assert exported_at_from_name("letterboxd-sam-2026-11-11-16-08-utc.zip") == \
        "2026-11-11T16:08:59+00:00"
    assert exported_at_from_name("export.zip") is None
    ex = parse_export(make_zip(tmp_path / "letterboxd-sam-2026-11-12-01-02-utc.zip"))
    assert ex.exported_at == "2026-11-12T01:02:59+00:00" and ex.has_diary
    data = make_zip(tmp_path / "e.zip").read_bytes()
    ex = parse_export(data, "renamed.zip")
    assert ex.exported_at == "2026-11-09T23:59:59+00:00"     # newest "Date" in the export
    assert len(ex.rows) == 6
    only_wl = {k: v for k, v in CSVS.items() if k in ("profile.csv", "watchlist.csv")}
    assert not parse_export(make_zip(tmp_path / "w.zip", only_wl)).has_diary


def test_parse_user():
    assert script.parse_user("123456789012345678") == 123456789012345678
    assert script.parse_user("<@123456789012345678>") == 123456789012345678
    assert script.parse_user("sam") is None
