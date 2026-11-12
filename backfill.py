#!/usr/bin/env python3
"""Sync a member's Letterboxd history from their data export zip (shell version
of /letterboxd sync, for the owner).

    uv run backfill.py data/imports/letterboxd-sam-2026-11-11-16-08-utc.zip           # dry run
    uv run backfill.py data/imports/letterboxd-sam-2026-11-11-16-08-utc.zip --apply   # do it

The member exports from Letterboxd → Settings → Data → Export your data.

It runs the same full sync as /letterboxd sync (filmbuffs/sync.py):
  • New rows are stored; reviews go into the movie's post-watch discussion
    ("imported from Letterboxd history"); films without a #movies post get one
    (Watched (solo), or Backlog for watchlist films). Nothing is posted in
    #movies or #letterboxd-feed.
  • Entries edited on Letterboxd are updated and the bot's embed edited in place.
  • Entries deleted on Letterboxd: the bot deletes its own messages for them and
    marks them removed (kept for stats).
  • The watchlist is replaced.

Dry run is the default: it resolves titles on TMDB, prints the numbers, and
writes the films it couldn't match unambiguously to an "unmatched" CSV. Fill in
that CSV's tmdb_id column (an ID or TMDB URL) and pass it back with --overrides.
Safe to stop and re-run: finished work is recorded and skipped.

Filters only limit what is posted or created; every row is still stored.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import sys
from pathlib import Path

import aiohttp
import discord

from filmbuffs.backfill import (ExportError, TitleResolver, parse_export, read_overrides,
                                write_unmatched)
from filmbuffs.bot import USER_AGENT
from filmbuffs.config import Config
from filmbuffs.db import Database
from filmbuffs.letterboxd import LetterboxdService
from filmbuffs.movies import MovieService
from filmbuffs.spoilers import SpoilerService
from filmbuffs.state import ROOT
from filmbuffs.sync import (Filters, SyncPlan, apply_sync, build_sync_plan, change_lines,
                            fmt_duration, preview_lines)
from filmbuffs.tmdb import TMDB, TMDBError

async def discord_name(token: str, guild_id: int, user_id: int) -> str | None:
    """`Display name (@handle)` over the REST API (no gateway), or None."""
    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(token)
        try:
            m = await client.http.get_member(guild_id, user_id)
            u = m["user"]
            shown = m.get("nick") or u.get("global_name") or u["username"]
            return f"{shown} (@{u['username']})"
        except discord.NotFound:
            u = await client.fetch_user(user_id)
            return f"{u.global_name or u.name} (@{u.name}, not in the server)"
    except discord.HTTPException:
        return None
    finally:
        await client.close()


async def write_spot_check(path: Path, plan: SyncPlan, tmdb: TMDB) -> int:
    """Films matched by anything other than an exact title + year, for a quick look."""
    picked = {}
    for i in plan.items:
        if i.tmdb_id and i.how not in ("exact", "override"):
            picked.setdefault(i.row.key, i)
    if not picked:
        return 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["letterboxd_title", "letterboxd_year", "how", "tmdb_id", "tmdb_title",
                    "tmdb_year", "director"])
        for i in picked.values():
            movie = await tmdb.movie(i.tmdb_id)
            w.writerow([i.row.title, i.row.year or "", i.how, i.tmdb_id,
                        movie.title if movie else "?", movie.year if movie else "",
                        ", ".join(movie.directors[:2]) if movie else ""])
    return len(picked)


def plain(line: str) -> str:
    return line.replace("**", "").replace("-# ", "")


def print_plan(plan: SyncPlan, unmatched_path: Path | None, who: str,
               spot_check: tuple[Path, int] | None = None) -> None:
    export = plan.export
    print(f"\nLetterboxd account {plan.username} → Discord member {who}")
    files = ", ".join(f"{n} {c}" for n, c in export.file_counts.items() if n != "profile.csv")
    print(f"  Files: {files}")
    print(f"  Exported: {export.exported_at or 'unknown'}")
    films: dict[str, str] = {}
    for i in plan.items:
        films.setdefault(i.row.key, i.how if i.tmdb_id else "unmatched")
    hows: dict[str, int] = {}
    for how in films.values():
        hows[how] = hows.get(how, 0) + 1
    matched = sum(v for k, v in hows.items() if k != "unmatched")
    detail = ", ".join(f"{k} {v}" for k, v in sorted(hows.items(), key=lambda kv: -kv[1])
                       if k != "unmatched")
    print(f"  TMDB: {matched} of {len(films)} films matched ({detail}); "
          f"{plan.tmdb_lookups} looked up this run")
    if spot_check and spot_check[1]:
        print(f"  Matches worth a glance ({spot_check[1]}, not an exact title + year): "
              f"{spot_check[0]}")
    print()
    for line in preview_lines(plan) + change_lines(plan, limit=30):
        print("  " + plain(line))
    print(f"  Estimated time: {fmt_duration(plan.est_seconds())}")
    if plan.unmatched and unmatched_path:
        print(f"\nUnmatched films (with candidates and directors) written to {unmatched_path}")
        print("  Fill in tmdb_id for any you want, then re-run with --overrides that file.")


class BackfillClient(discord.Client):
    """Just enough of FilmBuffsBot for MovieService and SpoilerService."""

    def __init__(self, config: Config, db: Database, tmdb: TMDB):
        super().__init__(intents=discord.Intents(guilds=True),
                         allowed_mentions=discord.AllowedMentions.none())
        self.config, self.db, self.tmdb = config, db, tmdb
        self.movies = MovieService(self)
        self.spoilers = SpoilerService(self)
        self.letterboxd = LetterboxdService(self)


async def apply_plan(client: BackfillClient, plan: SyncPlan) -> int:
    async def progress(text: str) -> None:
        print(text, flush=True)
    res = await apply_sync(client, plan, via="script", progress=progress)
    print(f"Sync complete: {res.as_dict()}")
    if res.failed:
        print(f"⚠️  {res.failed} Discord steps failed (see the log); re-run to retry them.")
    else:
        print("Re-run the dry run to confirm nothing is left.")
    return 1 if res.failed else 0


def parse_user(text: str | None) -> int | None:
    m = re.fullmatch(r"<?@?!?(\d{15,21})>?", (text or "").strip())
    return int(m.group(1)) if m else None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip", help="the member's Letterboxd export zip")
    ap.add_argument("--user", help="Discord user ID (default: whoever linked the export's "
                                   "Letterboxd username with /letterboxd)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="with --apply, skip the confirmation")
    ap.add_argument("--since", help="only post/create for entries on or after YYYY-MM-DD")
    ap.add_argument("--min-rating", type=float, help="only post/create for watched films "
                                                     "rated at least this (0.5–5)")
    wl = ap.add_mutually_exclusive_group()
    wl.add_argument("--only-watchlist", action="store_true", help="only create watchlist posts")
    wl.add_argument("--no-watchlist", action="store_true", help="skip watchlist posts")
    ap.add_argument("--store-only", action="store_true",
                    help="store rows in the database but post/create nothing")
    ap.add_argument("--overrides", help="a filled-in unmatched CSV (title, year, tmdb_id)")
    ap.add_argument("--unmatched-out", help="where to write unmatched films "
                                            "(default: data/backfill_unmatched-<username>.csv)")
    args = ap.parse_args()

    if args.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        ap.error("--since must be YYYY-MM-DD")
    filters = Filters(since=args.since, min_rating=args.min_rating,
                      only_watchlist=args.only_watchlist, no_watchlist=args.no_watchlist,
                      store_only=args.store_only)
    try:
        export = parse_export(args.zip)
    except ExportError as e:
        print(f"❌ {e}")
        return 2

    config = Config()
    db = Database(config.db_path)
    await db.connect()
    try:
        uid = parse_user(args.user)
        if args.user and uid is None:
            print(f"❌ --user must be a Discord user ID, got {args.user!r}")
            return 2
        link = await db.get_link_by_username(export.username) if export.username else None
        if uid is None:
            if link is None:
                print(f"❌ No member has linked Letterboxd account {export.username!r}. "
                      "Pass --user <Discord ID>.")
                return 2
            uid = link["discord_user_id"]
        elif link is not None and link["discord_user_id"] != uid:
            print(f"⚠️  {export.username!r} is linked to Discord user "
                  f"{link['discord_user_id']}, not {uid}. Check --user.")
            return 2
        username = export.username or (await db.get_link(uid) or {"username": "?"})["username"]

        overrides = read_overrides(args.overrides) if args.overrides else {}
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            tmdb = TMDB(session, config.tmdb_key)
            print(f"Matching {len({(r.title, r.year) for r in export.rows})} films on TMDB…",
                  flush=True)
            try:
                plan = await build_sync_plan(db, TitleResolver(db, tmdb, overrides), export,
                                             uid, username, filters)
            except TMDBError as e:
                print(f"❌ TMDB failed: {e}. Nothing was changed; try again.")
                return 1
            out = Path(args.unmatched_out or ROOT / "data" / f"backfill_unmatched-{username}.csv")
            if plan.unmatched:
                write_unmatched(out, plan.unmatched)
            check = out.with_name(f"backfill_matches-{username}.csv")
            spot = (check, await write_spot_check(check, plan, tmdb))
            who = await discord_name(config.token, config.guild_id, uid) or f"ID {uid}"
            print_plan(plan, out if plan.unmatched else None, who, spot)

            if not args.apply:
                print("\nDry run: nothing was changed. Re-run with --apply to do it.")
                return 0
            if not args.yes:
                answer = await asyncio.to_thread(input, "\nApply? Type 'yes': ")
                if answer.strip() != "yes":
                    print("Cancelled.")
                    return 0

            client = BackfillClient(config, db, tmdb)
            result = {"code": 1}

            @client.event
            async def on_ready():
                try:
                    result["code"] = await apply_plan(client, plan)
                except (discord.HTTPException, TMDBError, LookupError) as e:
                    logging.getLogger("backfill").exception("Backfill stopped")
                    print(f"❌ Stopped: {e!r}. Finished work is recorded; re-run to continue.")
                finally:
                    await client.close()

            await client.start(config.token, reconnect=False)
            return result["code"]
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.ERROR)   # voice/intents notices are noise here
    sys.exit(asyncio.run(main()))
