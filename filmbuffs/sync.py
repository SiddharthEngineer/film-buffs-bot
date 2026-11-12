"""Letterboxd export sync: each upload of a member's export is a full sync.

Used by /letterboxd sync (filmbuffs/cogs/letterboxd.py) and by backfill.py
(the owner's shell wrapper). The upload is compared with what's stored for that
member, then applied:

  new      rows are stored; reviews get a post-watch embed ("imported from
           Letterboxd history"); films without a #movies post get one
           (Watched (solo), or Backlog for watchlist films), throttled.
  changed  rating / ♥ / rewatch / watched date / review edited on Letterboxd:
           the row is updated and the bot's own embed (and #movies note, if the
           poller posted one) is edited in place. Nothing is reposted or moved.
  removed  diary entries deleted on Letterboxd: the bot deletes its own messages
           for them (embed, #movies note, #letterboxd-feed line) and marks the
           row `removed`. The row is kept for stats.
  watchlist replaced.

Matching stored rows to export rows: by the export's "Letterboxd URI" (a
boxd.it link: the diary entry for diary/reviews, the film for watched/watchlist
rows). RSS entries get that URI the first time they're matched, by film +
watched date. Only this member's stored export/RSS rows are compared; RSS
entries never matched to the export, and anything the bot saw after the export
was made, are never treated as deleted.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import discord

from .backfill import Export, ExportRow, TitleResolver
from .db import utcnow
from .tmdb import TMDBError

log = logging.getLogger(__name__)

DIARY = ("watch", "review")
STATUS_NAMES = {"watched_solo": "Watched (solo)", "backlog": "Backlog"}
# Fields compared per kind of row. Discord-visible ones (in the embed) are EMBED_FIELDS.
COMPARED = {
    "diary": ("kind", "rating", "liked", "rewatch", "watched_date", "review_text"),
    "watched": ("rating", "liked"),
    "watchlist": (),
}
EMBED_FIELDS = {"rating", "liked", "rewatch", "watched_date", "review_text"}
# Pauses between Discord actions (seconds), on top of discord.py's rate limiting.
POST_PAUSE = 2.0
REVIEW_PAUSE = 1.0
EDIT_PAUSE = 0.7
SECS_PER_POST = 2.5
SECS_PER_REVIEW = 1.5
SECS_PER_EDIT = 1.0
# A sync that would remove more than this share of the member's diary entries
# gets a warning in the preview (a wrong or partial export).
REMOVAL_WARN_SHARE = 0.25


def group_of(kind: str) -> str:
    return "diary" if kind in DIARY else kind


@dataclass
class Filters:
    """Limits what is posted or created on Discord (every row is still stored).
    Only backfill.py sets these."""
    since: str | None = None          # YYYY-MM-DD
    min_rating: float | None = None
    only_watchlist: bool = False
    no_watchlist: bool = False
    store_only: bool = False

    def acts_on(self, row: ExportRow) -> bool:
        if self.store_only:
            return False
        if row.kind == "watchlist":
            if self.no_watchlist:
                return False
        else:
            if self.only_watchlist:
                return False
            if self.min_rating is not None and (row.rating or 0) < self.min_rating:
                return False
        if self.since and row.date and row.date < self.since:
            return False
        return True


def row_values(row: ExportRow) -> dict:
    return dict(kind=row.kind, rating=row.rating, liked=int(row.liked),
                rewatch=int(row.rewatch), watched_date=row.watched_date,
                review_text=row.review_text)


def diff(row: ExportRow, stored) -> dict[str, tuple]:
    """{field: (stored, export)} for fields that differ."""
    new = row_values(row)
    out = {}
    for f in COMPARED[group_of(row.kind)]:
        if f == "kind" and group_of(stored["kind"]) == "diary":
            if stored["kind"] != new["kind"]:
                out[f] = (stored["kind"], new["kind"])
            continue
        old = stored[f]
        if f in ("liked", "rewatch"):
            old = int(old or 0)
        if old != new[f]:
            out[f] = (old, new[f])
    return out


@dataclass
class SyncItem:
    row: ExportRow
    tmdb_id: int | None
    how: str
    stored: object | None = None         # the stored row this export row matches
    via_rss: bool = False                # matched an RSS entry by film + date (not linked yet)
    changes: dict[str, tuple] = field(default_factory=dict)
    acts: bool = False                   # filters allow Discord work for it

    @property
    def status(self) -> str:
        if self.stored is None or self.stored["state"] == "removed":
            return "new"
        if self.changes or (self.tmdb_id and not self.stored["tmdb_id"]):
            return "changed"
        return "same"

    @property
    def group(self) -> str:
        return group_of(self.row.kind)


@dataclass
class SyncPlan:
    user_id: int
    username: str
    export: Export
    items: list[SyncItem] = field(default_factory=list)
    removed: list = field(default_factory=list)         # stored rows gone from Letterboxd
    deletes: list = field(default_factory=list)         # removed rows with bot messages
    unmatched: list[tuple[ExportRow, str, str]] = field(default_factory=list)
    new_posts: dict[int, tuple[str, str]] = field(default_factory=dict)  # tmdb → (status, date)
    reviews: list[SyncItem] = field(default_factory=list)   # embeds to post
    edits: list[SyncItem] = field(default_factory=list)     # embeds to edit
    watchlist: list[tuple[int, str, int | None]] | None = None   # None: no watchlist.csv
    watchlist_rows: int = 0
    watchlist_unmatched: list[str] = field(default_factory=list)
    watchlist_before: set[int] = field(default_factory=set)
    not_in_export: int = 0           # RSS entries the export doesn't have (left alone)
    stored_diary: int = 0
    tmdb_lookups: int = 0
    store_only: bool = False

    def _n(self, status: str, group: str) -> int:
        return sum(1 for i in self.items if i.group == group and i.status == status)

    @property
    def linked(self) -> int:
        return sum(1 for i in self.items if i.via_rss)

    def unmatched_films(self) -> dict[str, tuple[ExportRow, str]]:
        return {r.key: (r, why) for r, why, _ in self.unmatched}

    def counts(self) -> dict[str, int]:
        after = {t for t, _, _ in self.watchlist or []}
        return {
            "diary_new": self._n("new", "diary"),
            "diary_changed": self._n("changed", "diary"),
            "diary_removed": sum(1 for r in self.removed if group_of(r["kind"]) == "diary"),
            "diary_same": self._n("same", "diary"),
            "watched_new": self._n("new", "watched"),
            "watched_changed": self._n("changed", "watched"),
            "watched_removed": sum(1 for r in self.removed if r["kind"] == "watched"),
            "linked_rss": self.linked,
            "not_in_export": self.not_in_export,
            "unmatched_films": len(self.unmatched_films()),
            "posts_to_create": len(self.new_posts),
            "reviews_to_post": len(self.reviews),
            "embeds_to_edit": len(self.edits),
            "entries_to_clean_up": len(self.deletes),
            "watchlist_films": len(after),
            "watchlist_added": len(after - self.watchlist_before) if self.watchlist is not None else 0,
            "watchlist_removed": len(self.watchlist_before - after) if self.watchlist is not None else 0,
        }

    def removal_warning(self) -> bool:
        n = sum(1 for r in self.removed if group_of(r["kind"]) == "diary")
        return n >= 5 and n > REMOVAL_WARN_SHARE * max(1, self.stored_diary)

    def est_seconds(self) -> float:
        return (len(self.new_posts) * SECS_PER_POST + len(self.reviews) * SECS_PER_REVIEW
                + (len(self.edits) + len(self.deletes)) * SECS_PER_EDIT)

    @property
    def discord_steps(self) -> int:
        return len(self.deletes) + len(self.edits) + len(self.new_posts) + len(self.reviews)

    @property
    def nothing_to_do(self) -> bool:
        c = self.counts()
        return not any(c[k] for k in ("diary_new", "diary_changed", "diary_removed",
                                      "watched_new", "watched_changed", "watched_removed",
                                      "linked_rss", "watchlist_added", "watchlist_removed")) \
            and not self.discord_steps


async def build_sync_plan(db, resolver: TitleResolver, export: Export, user_id: int,
                          username: str, filters: Filters = Filters()) -> SyncPlan:
    plan = SyncPlan(user_id, username, export, store_only=filters.store_only)
    keys = list({(r.title, r.year) for r in export.rows})
    results = dict(zip(keys, await asyncio.gather(*(resolver.resolve(t, y) for t, y in keys))))
    plan.tmdb_lookups = resolver.lookups

    entries = [e for e in await db.user_entries(user_id) if e["state"] != "merged"]
    by_guid = {e["guid"]: e for e in entries}
    by_uri = {(group_of(e["kind"]), e["export_uri"]): e for e in entries if e["export_uri"]}
    rss: dict[tuple, list] = defaultdict(list)
    for e in entries:
        if (e["source"] == "rss" and not e["export_uri"] and e["kind"] in DIARY
                and e["state"] != "removed"):
            rss[(e["tmdb_id"], e["watched_date"])].append(e)
    plan.stored_diary = sum(1 for e in entries
                            if e["kind"] in DIARY and e["state"] != "removed")
    taken: set[str] = set()

    for row in export.rows:
        tid, how, cands = results[(row.title, row.year)]
        item = SyncItem(row, tid, how)
        group = group_of(row.kind)
        stored = by_uri.get((group, row.uri)) if row.uri else None
        if stored is None:
            stored = by_guid.get(row.guid(user_id))
        if stored is not None and stored["guid"] in taken:
            stored = None                     # a duplicate row in the export
        if stored is None and tid and group == "diary":
            queue = rss.get((tid, row.watched_date))
            while queue and stored is None:
                cand = queue.pop(0)
                if cand["guid"] not in taken:
                    stored, item.via_rss = cand, True
        if stored is not None:
            taken.add(stored["guid"])
            item.stored = stored
            item.changes = diff(row, stored)
        item.acts = bool(tid) and filters.acts_on(row)
        plan.items.append(item)
        if not tid:
            plan.unmatched.append((row, how, cands))

    # Gone from Letterboxd: stored rows of the parts this export covers that it no
    # longer has, and which the bot saw before the export was made.
    covered = set()
    if export.has_diary:
        covered |= {"diary", "watched"}
    if "watchlist.csv" in export.file_counts:
        covered.add("watchlist")
    cutoff = export.exported_at
    for e in entries:
        if e["guid"] in taken or group_of(e["kind"]) not in covered:
            continue
        if e["state"] == "removed":
            if any(e[c] for c in ("spoiler_message_id", "movies_message_id", "feed_message_id")):
                plan.deletes.append(e)        # an earlier sync stopped half-way
            continue
        if not e["export_uri"]:
            plan.not_in_export += 1           # an RSS entry never matched: leave it alone
            continue
        seen = e["published_at"] or e["seen_at"]
        if cutoff and seen and seen > cutoff:
            continue
        plan.removed.append(e)
        if any(e[c] for c in ("spoiler_message_id", "movies_message_id", "feed_message_id")):
            plan.deletes.append(e)

    # Discord work for what's in the export.
    movie_rows: dict[int, object] = {}
    for item in plan.items:
        if not item.acts:
            continue
        tid = item.tmdb_id
        if tid not in movie_rows:
            movie_rows[tid] = await db.get_movie(tid)
        if movie_rows[tid] is None:
            status = "backlog" if item.row.kind == "watchlist" else "watched_solo"
            prev = plan.new_posts.get(tid)
            if prev is None or (prev[0] == "backlog" and status == "watched_solo"):
                plan.new_posts[tid] = (status, item.row.date)
        if item.group != "diary":
            continue
        stored = item.stored
        if stored is not None and stored["state"] == "pending":
            continue                          # the poller is about to post it
        if stored is not None and stored["spoiler_message_id"] and stored["state"] != "removed":
            if EMBED_FIELDS & set(item.changes):
                plan.edits.append(item)
        elif item.row.review_text:
            plan.reviews.append(item)
    plan.reviews.sort(key=lambda i: (i.row.date, i.row.title))

    # Watchlist: replaced by the export's.
    if "watchlist.csv" in export.file_counts:
        wl = [i for i in plan.items if i.row.kind == "watchlist"]
        plan.watchlist = [(i.tmdb_id, i.row.title, i.row.year) for i in wl if i.tmdb_id]
        plan.watchlist_rows = len(wl)
        plan.watchlist_unmatched = [i.row.display for i in wl if not i.tmdb_id]
        cur = await db.conn.execute("SELECT tmdb_id FROM watchlist_items WHERE user_id=?",
                                    (user_id,))
        plan.watchlist_before = {r[0] for r in await cur.fetchall()}
    return plan


# ------------------------------------------------------------------ apply

Progress = Callable[[str], Awaitable[None]]


@dataclass
class SyncResult:
    stored: int = 0
    updated: int = 0
    removed: int = 0
    posts_created: int = 0
    reviews_posted: int = 0
    embeds_edited: int = 0
    messages_deleted: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


async def _store(db, plan: SyncPlan, res: SyncResult) -> None:
    """The database half: fast, no Discord calls."""
    uid, now = plan.user_id, utcnow()
    for item in plan.items:
        row, stored = item.row, item.stored
        state = "imported" if item.tmdb_id else "unmatched"
        if stored is None:
            res.stored += await db.insert_entry(dict(
                guid=row.guid(uid), discord_user_id=uid, username=plan.username, kind=row.kind,
                tmdb_id=item.tmdb_id, film_title=row.title, film_year=row.year,
                watched_date=row.watched_date, rating=row.rating, liked=int(row.liked),
                rewatch=int(row.rewatch), review_text=row.review_text, letterboxd_url=row.uri,
                published_at=row.logged_date, source="export", state=state,
                export_uri=row.uri, logged_date=row.logged_date))
            continue
        fields: dict = {k: new for k, (_, new) in item.changes.items()}
        if item.via_rss:
            fields["export_uri"] = row.uri
            fields["logged_date"] = row.logged_date
        if item.tmdb_id and not stored["tmdb_id"]:
            fields["tmdb_id"] = item.tmdb_id
        if stored["state"] in ("removed", "unmatched", "baseline") and stored["state"] != state:
            fields["state"] = state
        if stored["state"] == "removed":
            fields["removed_at"] = None
        if item.changes:
            fields["updated_at"] = now
        if fields:
            await db.update_entry(stored["guid"], **fields)
            res.updated += 1
    for e in plan.removed:
        await db.update_entry(e["guid"], state="removed", removed_at=now)
        res.removed += 1
    if plan.watchlist is not None:
        await db.replace_watchlist(uid, plan.watchlist, plan.watchlist_rows,
                                   plan.watchlist_unmatched)


async def apply_sync(client, plan: SyncPlan, *, via: str,
                     progress: Progress | None = None,
                     lock: asyncio.Lock | None = None) -> SyncResult:
    """Apply a plan. `client` is the bot (or backfill.py's client): it needs
    .db, .tmdb, .movies, .spoilers, .letterboxd, .config and get_guild().
    Safe to stop and re-run: every Discord step records its result as it goes,
    and a fresh plan skips what's done. `lock` (the poller's per-member lock) is
    held while the database is updated, not during the slower Discord part."""
    db, uid = client.db, plan.user_id
    res = SyncResult()
    say = progress or (lambda text: asyncio.sleep(0))
    async with lock or contextlib.nullcontext():
        await _store(db, plan, res)
    await say(f"Stored: {res.stored} new rows, {res.updated} updated, {res.removed} removed.")

    if not plan.store_only and plan.discord_steps:
        member = await client.letterboxd._member(uid)
        name = member.display_name if member else plan.username
        avatar = member.display_avatar.url if member else None
        lb = client.letterboxd
        total, done = plan.discord_steps, 0

        async def step(label: str, coro) -> bool:
            nonlocal done
            done += 1
            try:
                await coro
                return True
            except (discord.HTTPException, TMDBError, LookupError) as e:
                res.failed += 1
                log.warning("Letterboxd sync for %s: %s failed: %r", uid, label, e)
                return False
            finally:
                if done % 10 == 0 or done == total:
                    await say(f"Discord: {done}/{total} done.")

        # 1. Entries deleted on Letterboxd: the bot's own messages go.
        for e in plan.deletes:
            async def delete(e=e):
                res.messages_deleted += await lb.delete_messages(await db.get_entry(e["guid"]))
            await step(f"deleting messages for {e['guid']}", delete())
            await asyncio.sleep(EDIT_PAUSE)

        # 2. Edited entries: embeds (and #movies notes) edited in place.
        for item in plan.edits:
            async def edit(item=item):
                row = await db.get_entry(item.stored["guid"])
                if row["spoiler_message_id"] and await lb.refresh_messages(row, member):
                    res.embeds_edited += 1
                elif row["review_text"] and row["tmdb_id"]:
                    await lb.post_review(row, name, avatar)    # embed was deleted by hand
                    res.reviews_posted += 1
            await step(f"editing {item.row.display}", edit())
            await asyncio.sleep(EDIT_PAUSE)

        # 3. Missing #movies posts, oldest first.
        for tid, (status, _) in sorted(plan.new_posts.items(), key=lambda kv: kv[1][1]):
            async def post(tid=tid, status=status):
                movie = await client.tmdb.movie(tid)
                if movie is None:
                    raise LookupError(f"TMDB {tid} no longer exists")
                _, created = await client.movies.ensure_post(
                    movie, added_by=uid, pitch=None, status=status, source="letterboxd-sync")
                res.posts_created += created
                if created:
                    await asyncio.sleep(POST_PAUSE)
            await step(f"creating the post for TMDB {tid}", post())

        # 4. Reviews into post-watch discussions, oldest first.
        for item in plan.reviews:
            async def review(item=item):
                guid = item.stored["guid"] if item.stored is not None else item.row.guid(uid)
                row = await db.get_entry(guid)
                if row is None or row["spoiler_message_id"] or not row["tmdb_id"]:
                    return                    # posted meanwhile (poller) or not matched
                await lb.post_review(row, name, avatar)
                res.reviews_posted += 1
                await asyncio.sleep(REVIEW_PAUSE)
            await step(f"posting the review of {item.row.display}", review())

    counts = plan.counts() | {f"done_{k}": v for k, v in res.as_dict().items()}
    await db.record_sync(uid, plan.username, plan.export.exported_at, via, counts)
    log.info("Letterboxd sync for %s (%s) via %s: %s", uid, plan.username, via, res.as_dict())
    return res


# ------------------------------------------------------------------ text

def fmt_duration(secs: float) -> str:
    m, s = divmod(int(secs + 0.5), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def _parts(*pairs: tuple[int, str]) -> str:
    return " · ".join(f"{n} {label}" for n, label in pairs if n) or "nothing"


def preview_lines(plan: SyncPlan) -> list[str]:
    """The numbers, shared by the command's preview and backfill.py."""
    c = plan.counts()
    lines = [
        "**Diary entries:** " + _parts((c["diary_new"], "new"), (c["diary_changed"], "changed"),
                                       (c["diary_removed"], "removed"))
        + (f" ({c['diary_same']} unchanged)" if c["diary_same"] else ""),
    ]
    if c["watched_new"] or c["watched_changed"] or c["watched_removed"]:
        lines.append("**Watched films without a diary entry:** " + _parts(
            (c["watched_new"], "new"), (c["watched_changed"], "changed"),
            (c["watched_removed"], "gone")))
    if plan.watchlist is not None:
        lines.append(f"**Watchlist:** {c['watchlist_films']} films"
                     + (f" (+{c['watchlist_added']} / −{c['watchlist_removed']})"
                        if c["watchlist_added"] or c["watchlist_removed"] else " (no change)"))
    by_status: dict[str, int] = defaultdict(int)
    for status, _ in plan.new_posts.values():
        by_status[status] += 1
    posts = ", ".join(f"{STATUS_NAMES[k]} {v}" for k, v in by_status.items())
    if plan.store_only:
        lines.append("**Discord:** nothing (store only)")
    else:
        lines.append("**Discord:** " + _parts(
            (c["posts_to_create"], f"#movies posts to create ({posts})" if posts else ""),
            (c["reviews_to_post"], "reviews to post"),
            (c["embeds_to_edit"], "embeds to edit"),
            (c["entries_to_clean_up"], "removed entries' messages to delete")))
    if c["unmatched_films"]:
        films = plan.unmatched_films()
        shown = ", ".join(r.display for r, _ in list(films.values())[:10])
        more = f" and {len(films) - 10} more" if len(films) > 10 else ""
        lines.append(f"**Unmatched on TMDB:** {c['unmatched_films']} films (stored, not posted): "
                     f"{shown}{more}")
    if c["linked_rss"]:
        lines.append(f"-# {c['linked_rss']} entries the bot already saw in your feed were "
                     "matched to the export.")
    if plan.removal_warning():
        lines.append(f"⚠️ This removes **{c['diary_removed']}** of your "
                     f"{plan.stored_diary} stored diary entries. If that's not what you "
                     "expect, cancel and check it's your latest full export.")
    return lines


def change_lines(plan: SyncPlan, limit: int = 8) -> list[str]:
    """A few examples of what changed, for the preview."""
    out = []
    labels = {"rating": "rating", "liked": "♥", "rewatch": "rewatch",
              "watched_date": "date", "review_text": "review", "kind": "review"}
    for item in plan.items:
        if item.status == "changed" and item.group == "diary" and item.changes:
            what = sorted({labels[k] for k in item.changes})
            out.append(f"✏️ {discord.utils.escape_markdown(item.row.display)}: {', '.join(what)}")
    for e in plan.removed:
        if group_of(e["kind"]) == "diary":
            title = f"{e['film_title']} ({e['film_year']})" if e["film_year"] else e["film_title"]
            out.append(f"🗑️ {discord.utils.escape_markdown(title or '?')}"
                       + (f", watched {e['watched_date']}" if e["watched_date"] else ""))
    if len(out) > limit:
        out = out[:limit] + [f"… and {len(out) - limit} more"]
    return out
