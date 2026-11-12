# Roadmap

Work in phases. At the end of each phase: stop, summarize, list anything the owner must do by hand, and wait for a go-ahead.

Phases 0–8 are built (see [STATUS.md](STATUS.md)); Phases 9–10 below are planned and approved by the owner (2026-11-11), not started. Phase 6 was built before Phase 5 at the owner's request. The original Phase 3 was built and then redesigned with the owner; Phase 3R below replaces it.

## Guiding decisions (agreed with the owner)

1. **`#movies` is always spoiler-free.** Spoilers live in a companion post per movie in the post-watch forum. No in-post spoiler line, no dividers. (2026-11-11: `#movie-spoilers` → `#post-watch-discussion`, the 🍿 Post-watch tag retired; `#movies` was briefly `#pre-watch-discussion` and is `#movies` again.)
2. **Never reorder messages inside a post.** No repost-at-the-bottom tricks.
3. **Star ratings are spoilers.** So are liked ♥, rewatch flags and review text. The spoiler-free side may only say *that* someone logged a film ("Siddharth logged this on Letterboxd"), never how they rated it.
4. **Letterboxd is the source of truth for who watched what.** Nobody logs watches in Discord; the bot pulls from Letterboxd.
5. **SQLite tracks everything** (movies, Letterboxd entries with full detail, weekly picks, votes) so engagement stats can be built later.
6. Prefer native Discord features (forum tags, polls, scheduled events, context menus, buttons). If something is impossible or awkward in Discord's API, stop and propose the closest alternative instead of silently changing the design.

---

## Phase 3R: spoiler companion posts (built; see STATUS.md)

Replace the in-post spoiler line with a separate spoiler post per movie.

**Server (`server.yaml`, then plan/apply):**
- Add forum `#movie-spoilers` in 🎬 Movies, same permissions as `#movies` (only the bot creates posts; everyone can reply).
- Repurpose the `spoilers` tag in `#movies` to mean "this movie has a spoiler discussion". Keep the tag **key** `spoilers` so its Discord ID survives; only the display name/emoji change. Owner picked **🍿 Post-watch**.
- Update the `#movies` guidelines and the welcome message to describe the new flow.

**Behavior:**
- Every movie info card gets a **🔓 Spoiler discussion** button.
  - First click when none exists: create "Title (Year) — Spoilers" in `#movie-spoilers`, reply ephemerally with the link, then edit the info card so the button becomes a direct link button.
  - Use a persistent component (`discord.ui.DynamicItem` with a `custom_id` like `spoilers:<tmdb_id>`) so buttons keep working after restarts.
- The spoiler post's starter message: "⚠️ Spoilers for Title (Year). Everything here may spoil the film. ← Back to the spoiler-free post" (with link). No 👍 reaction; votes stay on the `#movies` post.
- Apply the repurposed tag to the `#movies` post when its spoiler post is created.
- **Mark as spoiler** (message context menu, anyone, in `#movies` posts): move the message into the movie's spoiler post (create it if needed) via webhook with the author's display name + avatar, attachments re-uploaded, a small "moved from the spoiler-free post by @X" note and reply context as a "↪ replying to" line; then delete the original. Do **not** repost anything in `#movies`. Owner confirmed moving (2026-11-11).
- `/spoiler` becomes "open this movie's spoiler post" (create if needed, ephemeral link), usable inside a `#movies` post or its spoiler post.
- Remove: `||markup||` listener, dividers, the "Spoiler line" embed field, earliest-wins marker logic.

**Data:**
- Migration: add `spoiler_thread_id`, `spoiler_starter_message_id` to `movies`; stop using `spoiler_marker_message_id`, `spoiler_divider_message_id`, `spoiler_marked_at` (leave or drop).
- One-off cleanup for Parasite (see STATUS.md "Known state").

**Done when:** a member can open any `#movies` post without seeing spoilers, click one button to reach that movie's spoiler post, and "Mark as spoiler" moves a message out without touching the order of anything else. Tests cover lazy creation (no duplicates under concurrent clicks) and the move path.

---

## Phase 4: Letterboxd integration (built; see STATUS.md)

- `/letterboxd username:<name>` links a Discord user to a Letterboxd account (validate `https://letterboxd.com/<username>/rss/` exists). `/letterboxd-unlink`.
- Poll every linked feed every ~15 minutes, politely: conditional GET (ETag / Last-Modified), the bot's honest User-Agent (`FilmBuffsBot/x.y (private Discord bot)`), jitter between feeds.
- RSS items carry `<tmdb:movieId>` (verified). Use it; fall back to TMDB title+year search only if missing.
- Track seen GUIDs so nothing posts twice. On first link, mark existing entries as seen (no flood; history comes from backfill in Phase 6).
- **Store every entry in SQLite** with full detail, for stats: user, GUID, TMDB ID, watched date, rating, liked, rewatch, review text, Letterboxd URL, published time, and the Discord message IDs the bot posted.
- For each new entry:
  - Find the movie post by TMDB ID; if none, auto-create it (same format as `/addmovie`, added_by = that user) tagged `Watched (solo)`.
  - **Spoiler-free `#movies` post:** a one-line neutral note, e.g. "📓 Siddharth logged this on Letterboxd. Their rating and review are in the spoiler discussion → link". No stars, no ♥, no review.
  - **`#movie-spoilers` post** (create if needed): an embed with user, star rating, liked ♥, rewatch flag, watched date, review text and a link to the Letterboxd review.
  - **`#letterboxd-feed`:** one line linking the `#movies` post, no rating and no review text.
- A solo log never changes an existing post's tag (owner, 2026-11-11: Backlog stays Backlog). Only posts auto-created from a log start as `Watched (solo)`.

### Phase 4 follow-ups (owner, 2026-11-11)
- **Edit tracking:** editing an old entry doesn't bring it back to the top of the RSS feed (owner, 2026-11-11), so the poller can't see edits. The export sync (Phase 8) covers them.
- **Watch → review:** not confirmed yet. Letterboxd gives each feed item an ID (`letterboxd-watch-N` or `letterboxd-review-N`); if adding a review later swaps the ID, the poller would treat it as a new entry and post the film twice. To check: log a film with no review, wait for the bot to post it, then add a review. Phase 8 guards against it anyway (same number → update, not repost).

## Phase 5: weekly pick, votes, watchlists (built; see STATUS.md)

Redesigned with the owner on 2026-11-11 (replaces the original "Monday poll from the top 10").
- **Three-week pipeline**, every stage a full Mon 12:00 AM → Sun 11:59 PM week (America/Chicago). For watch week T: ballot in week T-2, poll in week T-1, watching in week T. Every week runs one of each.
- **Ballot** (live bot message in `#watch-planning`): `/nominate` one film per member (swap by re-running, `/unnominate`), first come first served up to 10. The rest of the 10 slots are the most-wanted eligible films. Discord polls can't be edited, so the ballot is frozen at Sun 11:59 PM and becomes the poll.
- **Poll**: native, 10 answers, ended by the bot at Sun 11:59 PM. Winner by votes, then interest, then 👍, then earliest added (`weekly.tie_break`). Winner → Up Next, external server event for its watch week, announcement. Zero votes or fewer than 2 options → skip, with a notice and a `#watch-log` line.
- **Watch week end**: Watched tag, `#watch-log` line `Wk <ISO> · <range> · <Title (Year)> · won X–Y`.
- **Interest** = distinct members who 👍'd the info card or have the film on their uploaded watchlist. The bot no longer adds its own 👍 (removed from old posts).
- **Sorting by 👍**: Discord forums can't sort by reactions, so a pinned 🏆 leaderboard message in `#watch-planning` plus `/top [genre]`.
- **Pins** (owner, 2026-11-11): a forum allows one pinned post, so `#movies` pins the current week's movie and `#post-watch-discussion` pins its post-watch discussion, both from the start of the watch week until the next one.
- **Admin override:** `/admin pick` sets this or next week's movie, ending an open poll early.
- **Watchlists**: `/watchlist import` of the Letterboxd export (the bot can't read lists or watchlists from Letterboxd). Optional; `/addmovie` and 👍 are the main way. The earlier "link one public list" idea was dropped. Films without a post get one, tagged Backlog (owner, 2026-11-11).
- **No `.ics` feed** (owner: Discord is the single place for this).
- Watched (solo) films can be nominated and picked.
- `/schedule` shows this week's movie, next week's poll and the open ballot.
- If Letterboxd grants API access (owner emailed them 2026-11-11), `/watchlist import` would be replaced by account linking.

## Phase 6: Letterboxd backfill script (built; see STATUS.md)

Standalone `backfill.py`, run by hand.
- Input: a Letterboxd data export zip (`watched.csv`, `diary.csv`, `reviews.csv`, `ratings.csv`, `watchlist.csv`) + the Discord user it belongs to. (The username-only RSS mode was not built: the export covers everything, and a linked account's recent entries are already stored as baseline, which the backfill links to.)
- Exports have title, year, Letterboxd URI, not TMDB IDs: resolve via TMDB search (title + year), cache results, and write `backfill_unmatched.csv` for anything ambiguous instead of guessing.
- Watched/diary → ensure a post exists (new ones `Watched (solo)`). Watchlist → ensure a post exists tagged `Backlog` unless group-watched. Reviews → into the movie's **spoiler post**, deduped against what the RSS poller already posted. Everything goes into the SQLite entries table. Mark imported RSS GUIDs as seen.
- `--dry-run` (default) reports counts by tag, reviews to post, unmatched, and estimated runtime under rate limits; `--apply` executes; resumable; throttled. Filters: `--only-watchlist`, `--min-rating`, `--since YYYY-MM-DD`.
- **Show the owner dry-run numbers before applying.** It may create hundreds of posts.

## Phase 7: ops & docs (built; see STATUS.md)

- Nightly SQLite backup to `backups/` (keep the newest 3; was 14 days until 2026-11-11) with SQLite's online backup API, run in the bot at 04:00 Chicago, with a catch-up at startup.
- Failures to `#bot-logs` (throttled logging handler + error hooks), plus a startup notice.
- `/admin resync`, `/admin letterboxd-unlink`.
- Tests: backup retention, log throttle/dedup, poller alerts, admin check. (TMDB matching, spoiler-post creation and RSS dedupe were already covered in earlier phases; schedule/tie-break tests belong to Phase 5.)
- README admin cheat sheet.

Possible later: off-machine backup copies (needs a destination from the owner); more `/admin` commands if a need shows up.

## Phase 8: full Letterboxd sync from the export (built; see STATUS.md)

Owner request (2026-11-11): each export upload is a full sync, so edits on Letterboxd (rating, review, liked, rewatch, date) reach Discord. The RSS feed can't do this; it never shows edits.
- **Command shape (owner, 2026-11-12):** Discord makes a command with subcommands unusable on its own, so `/letterboxd` is now a group: `/letterboxd link [username]`, `/letterboxd sync file:`, `/letterboxd unlink` (was `/letterboxd username:` and `/letterboxd-unlink`). `/admin letterboxd-unlink` is unchanged.
- **Deleted entries (owner, 2026-11-12):** all three of the bot's messages for the entry go (post-watch embed, `#movies` note, `#letterboxd-feed` line), not only the embed, so nothing links to a deleted embed.
- **One command for everyone:** `/letterboxd sync file:<export zip>` replaces `/watchlist import`, which is removed (owner, 2026-11-11). `/watchlist show` and `/watchlist clear` stay. It reuses the backfill's parsing and TMDB matching (`backfill.py` becomes a thin wrapper for the owner's use from the shell).
- **Diff, then apply.** The upload is compared with what's stored for that member:
  - **New** entries: stored; reviews get a post-watch embed; films with no `#movies` post get one (Watched (solo) / Backlog, same rules as the backfill), throttled.
  - **Changed** entries: the stored row is updated and the bot's own post-watch embed is **edited in place** (no reordering, no reposting). RSS entries are matched to export rows by their `boxd.it` link, as the backfill does now.
  - **Gone** entries (deleted on Letterboxd): the bot deletes its own post-watch embed for that entry and marks the entry `removed` (owner, 2026-11-11). The row is kept for stats history. Only stored export/RSS entries of that member are compared, never other members' messages.
  - **Watchlist:** replaced, as today.
- **Preview first:** the bot replies (ephemeral) with counts (new / changed / removed / posts to create / unmatched) and **Apply** / **Cancel** buttons; long runs report progress and a final note.
- **Poller guard:** a `review-N` item whose `watch-N` is already stored updates that entry instead of posting the film again.
- Tests: diff (new/changed/gone, RSS ↔ export matching), embed edit, preview counts.

## Phase 9: In Theaters / Streaming tags from TMDB (planned)

Owner (2026-11-11): set them automatically. Both tags already exist as bot-only flag tags.
- Daily job: one TMDB call per movie (`append_to_response=release_dates,watch/providers`, region US), ~450 calls, well under TMDB's limits.
- **In Theaters:** a US theatrical release in the last 60 days and no US digital release yet.
- **Streaming:** on at least one US subscription service (TMDB's `flatrate`; rent/buy doesn't count).
- Info card gets a line like "📺 Streaming on Netflix, Max" (TMDB's provider data comes from JustWatch, which must be credited, e.g. in the card footer). Tags change through `MovieService.retag`; only changed posts are edited.
- Tags use up the 5-tag budget before genres, so a film In Theaters or Streaming shows one fewer genre.

## Phase 10: engagement stats (planned)

From the SQLite data already stored: who watched what, rating spreads (per member, per film, group vs. solo), most active posts, weekly-pick participation (nominations, poll voters, turnout). Shape (slash commands, a monthly post, or both) to be agreed with the owner first.

## Later ideas
- Off-machine backup copies (needs a destination from the owner).

## Open questions for the owner
Answered 2026-11-11: no `.ics` feed; Mark as spoiler moves the message; a solo Letterboxd log keeps an existing post's tags; channels renamed to pre-/post-watch discussion and the Post-watch tag retired.

Answered 2026-11-11: In Theaters / Streaming are set automatically (Phase 9); backups keep the newest 3.

Answered 2026-11-11 (Phase 8): entries deleted on Letterboxd have their post-watch embed deleted; `/watchlist import` is removed in favour of `/letterboxd sync`.

Answered 2026-11-12 (Phase 8): `/letterboxd` becomes a group (`link`/`sync`/`unlink`); a deleted entry's `#movies` note and feed line are deleted too.

No open questions right now. Phase 10 (stats) needs a short design talk before it's built.
