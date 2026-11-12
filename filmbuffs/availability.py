"""In Theaters / Streaming flags from TMDB (Phase 9).

One TMDB call per movie (`/movie/{id}?append_to_response=release_dates,watch/providers`),
classified for one region (US). The tags only describe **new films** (owner,
2026-11-12): is it still only in theaters, or has it left theaters for home?

- **First release:** the earliest US theatrical (type 2 limited, 3 wide) or digital
  (type 4) release dated today or earlier. Premieres/festivals (1), physical (5)
  and TV (6) don't count. Later theatrical dates are re-releases (anniversaries,
  one-night events), which vary by theater chain, so they're ignored. A film whose
  own TMDB release date is over `RECENT_DAYS` old is never new, even when TMDB only
  has US dates for a restoration (e.g. Sholay, 1975: US dates start in 2025).
- **In Theaters:** first release was theatrical, within the last `THEATER_WINDOW_DAYS`
  (today included), and the film isn't watchable at home yet: no digital release
  dated today or earlier and no subscription/rent/buy provider.
- **Streaming:** first release within the last `RECENT_DAYS`, not In Theaters, and
  watchable at home (a past digital release, or a subscription/rent/buy provider).
- Older films get neither tag. The card still lists subscription services for
  every film ("📺 Streaming on …"); free and ads-only services aren't listed.

TMDB's watch-provider data comes from JustWatch, which must be credited wherever
it's shown (TMDB's terms), so cards with the field credit JustWatch in the footer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Awaitable, Callable

from .config import FLAG_TAGS
from .db import utcnow

if TYPE_CHECKING:
    from .bot import FilmBuffsBot

log = logging.getLogger(__name__)

REGION = "US"
THEATER_WINDOW_DAYS = 60
RECENT_DAYS = 365
THEATRICAL_TYPES = (2, 3)   # 2 = theatrical (limited), 3 = theatrical
DIGITAL_TYPE = 4
HOME_PROVIDER_KINDS = ("flatrate", "rent", "buy")
MAX_PROVIDERS_SHOWN = 4

# Card field (spoiler-free; sits just above the no-spoilers reminder).
FIELD_NAME = "Where to watch (US)"
JUSTWATCH_CREDIT = "Streaming data: JustWatch"


@dataclass
class Availability:
    in_theaters: bool = False                              # In Theaters tag
    streaming_tag: bool = False                            # Streaming tag (new films only)
    streaming: list[str] = field(default_factory=list)     # subscription services, TMDB order
    first_release: str | None = None   # earliest past US theatrical/digital date (YYYY-MM-DD)
    digital: str | None = None         # earliest past US digital date

    @property
    def flags(self) -> list[str]:
        out = []
        if self.in_theaters:
            out.append("in_theaters")
        if self.streaming_tag:
            out.append("streaming")
        return out

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> "Availability | None":
        if not text:
            return None
        d = json.loads(text)
        return cls(in_theaters=bool(d.get("in_theaters")),
                   streaming_tag=bool(d.get("streaming_tag")),
                   streaming=list(d.get("streaming") or []),
                   first_release=d.get("first_release"), digital=d.get("digital"))


def _day(value: str | None) -> date | None:
    """TMDB release dates are ISO timestamps (`2019-10-11T00:00:00.000Z`); keep the day."""
    if not value or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _names(entries: list[dict]) -> list[str]:
    names: list[str] = []
    for p in sorted(entries, key=lambda p: p.get("display_priority", 999)):
        name = (p.get("provider_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def classify(data: dict, today: date, region: str = REGION) -> Availability:
    """Availability from a TMDB movie-details response with both appends."""
    theatrical: list[date] = []
    digital: list[date] = []
    for country in (data.get("release_dates") or {}).get("results") or []:
        if country.get("iso_3166_1") != region:
            continue
        for rel in country.get("release_dates") or []:
            day = _day(rel.get("release_date"))
            if day is None or day > today:
                continue    # undated or still in the future
            if rel.get("type") in THEATRICAL_TYPES:
                theatrical.append(day)
            elif rel.get("type") == DIGITAL_TYPE:
                digital.append(day)
    first_theatrical = min(theatrical, default=None)
    first_digital = min(digital, default=None)
    first = min(d for d in (first_theatrical, first_digital) if d) if (theatrical or digital) else None

    providers = ((data.get("watch/providers") or {}).get("results") or {}).get(region) or {}
    subscription = _names(providers.get("flatrate") or [])
    at_home = first_digital is not None or any(providers.get(k) for k in HOME_PROVIDER_KINDS)

    age = (today - first).days if first else None
    primary = _day(data.get("release_date"))
    if primary is not None and (today - primary).days > RECENT_DAYS:
        age = None    # an old film (restoration / first US run decades later)
    in_theaters = (age is not None and first == first_theatrical
                   and age <= THEATER_WINDOW_DAYS and not at_home)
    streaming_tag = age is not None and age <= RECENT_DAYS and not in_theaters and at_home

    return Availability(
        in_theaters=in_theaters, streaming_tag=streaming_tag, streaming=subscription,
        first_release=first.isoformat() if first else None,
        digital=first_digital.isoformat() if first_digital else None)


# Add-on channels sold through another store (e.g. "HBO Max Amazon Channel"); the
# card leaves them out unless they're the only way to stream the film.
_ADDON_SUFFIXES = ("Amazon Channel", "Apple TV Channel", "Roku Premium Channel")


def display_providers(names: list[str]) -> list[str]:
    """Card-friendly provider list: no add-on channels, no "with Ads" plan when the
    ad-free plan is listed too. The stored list stays complete (for stats)."""
    out = [n for n in names if not n.endswith(_ADDON_SUFFIXES)]
    out = [n for n in out
           if not (" with Ads" in n and any(o != n and n.startswith(o.split(" Standard")[0])
                                            for o in out))]
    return out or list(names)


def card_line(av: Availability | None) -> str | None:
    """The card's "Where to watch" text, or None when there's nothing to show."""
    if av is None:
        return None
    lines = []
    if av.in_theaters:
        lines.append("🎟️ In theaters now")
    if av.streaming:
        names = display_providers(av.streaming)
        shown = names[:MAX_PROVIDERS_SHOWN]
        more = len(names) - len(shown)
        text = "📺 Streaming on " + ", ".join(shown)
        if more:
            text += f" and {more} more"
        lines.append(text)
    return "\n".join(lines) or None


def merge_flags(current: list[str], av: Availability) -> list[str]:
    """Replace the availability flags, keep any other flag (sorted, like db.set_flags)."""
    kept = [f for f in current if f not in FLAG_TAGS]
    return sorted(set(kept + av.flags))


@dataclass
class Change:
    tmdb_id: int
    title: str
    old: Availability | None
    new: Availability
    flags_changed: bool
    card_changed: bool


@dataclass
class RunReport:
    checked: int = 0
    missing: int = 0            # TMDB has no such movie any more
    errors: int = 0             # TMDB unreachable / HTTP error
    changes: list[Change] = field(default_factory=list)
    edited_tags: int = 0
    edited_cards: int = 0
    edit_failures: int = 0

    def tally(self) -> dict[str, int]:
        """How many posts gain / lose each tag."""
        out = {"in_theaters +": 0, "in_theaters −": 0, "streaming +": 0, "streaming −": 0}
        for c in self.changes:
            old = set(c.old.flags) if c.old else set()
            for f in FLAG_TAGS:
                if f in c.new.flags and f not in old:
                    out[f"{f} +"] += 1
                elif f in old and f not in c.new.flags:
                    out[f"{f} −"] += 1
        return out

    def summary(self) -> str:
        t = self.tally()
        return (f"checked {self.checked}, {len(self.changes)} changed "
                f"(In Theaters +{t['in_theaters +']}/−{t['in_theaters −']}, "
                f"Streaming +{t['streaming +']}/−{t['streaming −']}); "
                f"{self.edited_tags} retagged, {self.edited_cards} cards edited"
                + (f", {self.edit_failures} edit failures" if self.edit_failures else "")
                + (f", {self.errors} TMDB errors" if self.errors else "")
                + (f", {self.missing} not on TMDB" if self.missing else ""))


def diff(row, new: Availability) -> Change | None:
    """What changes for one movie row (None if nothing a member would see changes)."""
    old = Availability.from_json(row["availability"])
    current_flags = json.loads(row["flags"])
    flags_changed = merge_flags(current_flags, new) != sorted(set(current_flags))
    card_changed = card_line(old) != card_line(new)
    if not flags_changed and not card_changed and old is not None and old == new:
        return None
    return Change(row["tmdb_id"], row["title"], old, new, flags_changed, card_changed)


Fetch = Callable[[int], Awaitable[dict | None]]


async def check_movies(rows, fetch: Fetch, today: date, *, pause: float = 0.25,
                       apply: Callable[[Change], Awaitable[None]] | None = None,
                       report: RunReport | None = None) -> RunReport:
    """Fetch and classify each row; `apply` is called for every change (None = dry run).

    Rows whose stored availability changes only in detail (dates) are still
    passed to `apply`, so the DB stays current; `apply` decides what to edit.
    """
    from .tmdb import TMDBError
    report = report or RunReport()
    for i, row in enumerate(rows):
        if i and pause:
            await asyncio.sleep(pause)
        try:
            data = await fetch(row["tmdb_id"])
        except TMDBError as e:
            report.errors += 1
            log.warning("Availability: TMDB %s failed: %s", row["tmdb_id"], e)
            continue
        report.checked += 1
        if data is None:
            report.missing += 1
            continue
        change = diff(row, classify(data, today))
        if change is None:
            continue
        if change.flags_changed or change.card_changed:
            report.changes.append(change)
        if apply is not None:
            await apply(change)
    return report


class AvailabilityService:
    """Runs the daily check against the live server."""

    def __init__(self, bot: "FilmBuffsBot"):
        self.bot = bot
        self._lock = asyncio.Lock()
        self.fetch_pause = 0.25   # between TMDB calls (~450 movies → ~2 min)
        self.edit_pause = 1.0     # after each Discord edit

    async def fetch(self, tmdb_id: int) -> dict | None:
        return await self.bot.tmdb._get(f"/movie/{tmdb_id}",
                                        append_to_response="release_dates,watch/providers")

    async def for_new_post(self, tmdb_id: int) -> Availability | None:
        """Availability for a post about to be created (None if TMDB fails)."""
        from .tmdb import TMDBError
        try:
            data = await self.fetch(tmdb_id)
        except TMDBError as e:
            log.warning("Availability for new post TMDB %s failed: %s", tmdb_id, e)
            return None
        return classify(data, self.today()) if data else None

    def today(self) -> date:
        return datetime.now(self.bot.config.tz).date()

    async def apply_change(self, change: Change, report: RunReport) -> None:
        db, movies = self.bot.db, self.bot.movies
        row = await db.get_movie(change.tmdb_id)
        if row is None:
            return
        await db.set_availability(change.tmdb_id, change.new.to_json(),
                                  merge_flags(json.loads(row["flags"]), change.new))
        if not (change.flags_changed or change.card_changed):
            return
        import discord
        try:
            if change.card_changed:
                await movies.update_card(change.tmdb_id)
                report.edited_cards += 1
            if change.flags_changed:
                await movies.retag(change.tmdb_id)
                report.edited_tags += 1
        except discord.HTTPException:
            report.edit_failures += 1
            log.exception("Availability: could not update the post for TMDB %s", change.tmdb_id)
        await asyncio.sleep(self.edit_pause)   # gentle on rate limits for bulk edits

    async def run(self) -> RunReport:
        async with self._lock:
            rows = await self.bot.db.all_movies()
            report = RunReport()

            async def apply(change: Change) -> None:
                await self.apply_change(change, report)

            await check_movies(rows, self.fetch, self.today(), pause=self.fetch_pause,
                               apply=apply, report=report)
            await self.bot.db.set_meta("availability_last_run", utcnow())
            log.info("Availability check: %s", report.summary())
            return report
