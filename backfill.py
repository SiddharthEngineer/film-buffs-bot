#!/usr/bin/env python3
"""Import a member's Letterboxd history from their data export zip.

    python backfill.py data/imports/letterboxd-sam-2026-11-11.zip            # dry run
    python backfill.py data/imports/letterboxd-sam-2026-11-11.zip --apply    # do it

The member exports from Letterboxd → Settings → Data → Export your data.

What it does (with --apply):
  • Stores every diary entry, review, watched film and watchlist film in the
    database (for stats), linking rows the RSS poller already saw so nothing is
    stored or posted twice.
  • Creates missing #movies posts: watched films as Watched (solo), watchlist
    films as Backlog. Existing posts are never retagged.
  • Posts each review in the movie's post-watch discussion (created if needed),
    unless that review is already there. Nothing is posted in #movies or
    #letterboxd-feed, so history doesn't flood the channels.

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

from filmbuffs.backfill import (ExportError, Filters, Plan, TitleResolver, build_plan,
                                parse_export, read_overrides, review_of, write_unmatched)
from filmbuffs.bot import USER_AGENT
from filmbuffs.config import Config
from filmbuffs.db import Database
from filmbuffs.letterboxd import spoiler_embed
from filmbuffs.movies import MovieService
from filmbuffs.spoilers import SpoilerService
from filmbuffs.state import ROOT
from filmbuffs.tmdb import TMDB, TMDBError

STATUS_NAMES = {"watched_solo": "Watched (solo)", "backlog": "Backlog"}
POST_PAUSE = 2.0
REVIEW_PAUSE = 1.0


def fmt_duration(secs: float) -> str:
    m, s = divmod(int(secs + 0.5), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


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


async def write_spot_check(path: Path, plan: Plan, tmdb: TMDB) -> int:
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


def print_plan(plan: Plan, export, filters: Filters, unmatched_path: Path | None,
               who: str, spot_check: tuple[Path, int] | None = None) -> None:
    print(f"\nLetterboxd account {plan.username} → Discord member {who}")
    files = ", ".join(f"{n} {c}" for n, c in export.file_counts.items() if n != "profile.csv")
    print(f"  Files: {files}")
    kinds: dict[str, int] = {}
    for i in plan.items:
        kinds[i.row.kind] = kinds.get(i.row.kind, 0) + 1
    print(f"  Rows after merging: {len(plan.items)} "
          f"({', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))})")
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
    reasons: dict[str, int] = {}
    for row, why, _ in {r.key: (r, why, c) for r, why, c in plan.unmatched}.values():
        reasons[why] = reasons.get(why, 0) + 1
    if reasons:
        print(f"  Unmatched films: {', '.join(f'{k} {v}' for k, v in reasons.items())}")
    if spot_check and spot_check[1]:
        print(f"  Matches worth a glance ({spot_check[1]}, not an exact title + year): "
              f"{spot_check[0]}")
    print("\nDatabase")
    print(f"  {plan.to_store} new rows to store, {plan.already_stored} already stored, "
          f"{plan.linked_to_rss} linked to entries the RSS poller saw")
    print("\nDiscord" + (" (nothing: --store-only)" if filters.store_only else ""))
    by_status = plan.posts_by_status()
    print(f"  #movies posts to create: {len(plan.new_posts)}"
          + (f" ({', '.join(f'{STATUS_NAMES[k]} {v}' for k, v in by_status.items())})"
             if by_status else ""))
    print(f"  Existing #movies posts (left as they are): {len(plan.existing_posts)}")
    print(f"  Reviews to post in post-watch discussions: {len(plan.reviews)} "
          f"({len(plan.new_spoiler_posts)} new post-watch posts); "
          f"{plan.reviews_already} already posted")
    if plan.filtered:
        print(f"  Rows not acted on (filters or no TMDB match): {plan.filtered}")
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


async def apply_plan(client: BackfillClient, plan: Plan) -> int:
    db, uid = client.db, plan.user_id
    guild = client.get_guild(client.config.guild_id)
    try:
        member = await guild.fetch_member(uid)
        name, avatar = member.display_name, member.display_avatar.url
    except discord.NotFound:
        name, avatar = plan.username, None
        print(f"Note: Discord user {uid} isn't in the server; reviews will show {name!r}.")

    # 1. Store rows and link RSS entries (fast, no Discord calls).
    stored = linked = 0
    for item in plan.items:
        row = item.row
        if item.existing is not None:
            continue
        if item.rss_match is not None:
            await db.update_entry(item.rss_match["guid"], export_uri=row.uri)
            linked += 1
            continue
        stored += await db.insert_entry(dict(
            guid=row.guid(uid), discord_user_id=uid, username=plan.username, kind=row.kind,
            tmdb_id=item.tmdb_id, film_title=row.title, film_year=row.year,
            watched_date=row.watched_date, rating=row.rating, liked=int(row.liked),
            rewatch=int(row.rewatch), review_text=row.review_text, letterboxd_url=row.uri,
            published_at=row.logged_date, source="backfill",
            state="imported" if item.tmdb_id else "unmatched",
            export_uri=row.uri, logged_date=row.logged_date))
    print(f"Stored {stored} rows, linked {linked} RSS entries.")

    # 2. Missing #movies posts, oldest first.
    todo = sorted(plan.new_posts.items(), key=lambda kv: kv[1][1])
    for n, (tid, (status, _)) in enumerate(todo, 1):
        movie = await client.tmdb.movie(tid)
        if movie is None:
            print(f"[post {n}/{len(todo)}] TMDB {tid} no longer exists; skipped")
            continue
        _, created = await client.movies.ensure_post(
            movie, added_by=uid, pitch=None, status=status, source="backfill")
        print(f"[post {n}/{len(todo)}] {movie.display}: "
              f"{'created as ' + STATUS_NAMES[status] if created else 'already exists'}",
              flush=True)
        if created:
            await asyncio.sleep(POST_PAUSE)

    # 3. Reviews into spoiler discussions, oldest first.
    for n, item in enumerate(plan.reviews, 1):
        linked_row = item.existing or item.rss_match
        guid = linked_row["guid"] if linked_row is not None else item.row.guid(uid)
        entry = await db.get_entry(guid)
        if entry["spoiler_message_id"]:
            continue                        # done in an earlier, interrupted run
        if not entry["review_text"]:
            await db.update_entry(guid, review_text=review_of(item))
            entry = await db.get_entry(guid)
        post, _ = await client.spoilers.open(item.tmdb_id)
        msg = await post.send(embed=spoiler_embed(entry, name, avatar, imported=True),
                              allowed_mentions=discord.AllowedMentions.none())
        await db.update_entry(guid, spoiler_message_id=msg.id)
        if entry["state"] == "baseline":
            await db.update_entry(guid, state="imported")
        print(f"[review {n}/{len(plan.reviews)}] {item.row.display} → {post.name}", flush=True)
        await asyncio.sleep(REVIEW_PAUSE)

    # 4. RSS baseline rows linked to this export count as imported now.
    for item in plan.items:
        if item.rss_match is not None and item.rss_match["state"] == "baseline":
            await db.update_entry(item.rss_match["guid"], state="imported")
    print("Backfill complete. Re-run the dry run to confirm nothing is left.")
    return 0


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
                plan = await build_plan(db, TitleResolver(db, tmdb, overrides), export,
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
            print_plan(plan, export, filters, out if plan.unmatched else None, who, spot)

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
