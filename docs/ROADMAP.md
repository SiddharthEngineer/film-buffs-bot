# Roadmap

Work in phases. At the end of each phase: stop, summarize, list anything the owner must do by hand, and wait for a go-ahead.

Phases 0–9 and 5R are built and live (see [STATUS.md](STATUS.md)); only 5R's vote picker waits for the first vote (Fri 2026-11-13). Phase 9 went live 2026-11-12; Phase 10 is planned, not started. Phase 6 was built before Phase 5 at the owner's request. The original Phase 3 was built and then redesigned with the owner; Phase 3R below replaces it.

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
- **Watch → review:** settled (owner, 2026-11-12) without a live test. Whether or not Letterboxd swaps `letterboxd-watch-N` for `letterboxd-review-N` when a review is added later, the Phase 8 poller guard updates the stored entry and edits its messages in place instead of posting the film twice, so the answer no longer matters.

## Phase 5: weekly pick, votes, watchlists (built; see STATUS.md)

*The ballot → poll → watch pipeline below was replaced by Phase 5R; 👍 votes, the leaderboard, `/top`, pins and `/admin pick` stay.*

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

## Phase 5R: one-week nominate → ranked vote (built; see STATUS.md)

Owner (2026-11-12): two weeks from nominating to watching is too long, especially for films in theaters. Replaces Phase 5's ballot → poll → watch pipeline.
- **Schedule** (America/Chicago). For watch week T (Mon–Sun), everything happens in week T-1:
  - **Nominations** Mon 12:00 AM → Thu 11:59 PM. `/nominate` / `/unnominate` as now (one per member; swap or withdraw any time until Thu 11:59 PM). **Only nominated films are on the ballot** (owner, 2026-11-12): no fill from the most-wanted list, and no configurable size. The only cap is 25 nominations, Discord's dropdown limit.
  - **Ranked vote** Fri 12:00 AM → Sun 11:59 PM, on the options frozen at Thu 11:59 PM.
  - **Watch week** T: unchanged (Up Next, server event, announcement, pins, `#watch-log` line at the end).
  - Each week runs one watch and one nominate/vote cycle. The `weeks` row keeps its states with new times; `poll` → `voting`. `weekly.poll_options` is removed.
- **Ranked choice, instant runoff** (owner). Rankings can be changed any time until Sun 11:59 PM. Members rank **as many options as they like, up to all of them** (1st, 2nd, 3rd, …). Partial rankings are fine; ranking more only matters if your top picks are knocked out.
- **Voting UI** (native polls can't rank): a **🗳️ Vote** button on the ballot message opens an ephemeral picker. One dropdown lists the films not yet ranked; picking one adds it as the next rank and the message updates to show "Your ranking: 1. … 2. …". Buttons: **Undo last**, **Clear**. Every change is saved immediately (no Submit to forget). Clicking Vote again shows the saved ranking. Changes allowed until Sun 11:59 PM. (A message can't hold 10 dropdowns, and multi-select order isn't guaranteed to be click order, so it's one pick at a time.)
- **Secret until close:** the ballot shows only "N members have voted" while voting is open.
- **Counting (IRV):** each round, every ballot counts for its highest-ranked film still in the race. A film with more than half of the non-exhausted ballots wins. Otherwise films with no votes go out together, or else the one with the fewest; ties for last are broken by fewest 1st choices, then `weekly.tie_break` (lowest interest, 👍, latest added goes out first). If the last two tie, the same rule decides which goes out. Zero voters → skip. At the Thursday close, no nominations → skip; a single nomination wins without a vote (owner, 2026-11-12).
- **Results** in `#watch-planning` at close: the winner plus a short round-by-round summary (e.g. "Round 2: *Film* out, 2 votes moved to *Other*"). `#watch-log` line: `Wk <ISO> · <range> · <Title (Year)> · won X–Y after R rounds` (final-round counts; "after R rounds" left out when round 1 decides it).
- **DB** (append-only migration): `ranked_votes(week_start, user_id, rank, tmdb_id, voted_at)`, one row per rank; the round-by-round result stored on the week for stats. Old poll columns/`poll_voters` stay for history.
- **Transition:** the open ballot (one nomination) was moved to watch week 2026-11-16 at the owner's request, and `first_watch_week` with it: nominations close Thu 11-12, voting Fri 11-13 → Sun 11-15. No poll is ever posted.
- **Text:** `/schedule`, the ballot embed, the `#watch-planning` topic and the welcome message describe the new week.
- **Welcome links fix** (folded in): `{#watch-planning}`, `{#letterboxd-feed}`, `{#watch-log}` use channel *names* with hyphens; the `{#key}` regex is `\w+`, so they never matched and were posted as plain text (validation used the same regex, so it didn't complain). Use the keys (`watch_planning`, `letterboxd_feed`, `watch_log`) and make validation reject any `{#…}` that isn't a channel key. Then plan/apply.
- **Tests:** schedule boundaries (Thu/Fri/Sun cutoffs, DST), IRV (majority in round 1, eliminations, exhausted ballots, every tie path, single voter), picker add/undo/clear, transition of an open ballot, unknown `{#…}` refs rejected.
- **Owner by hand:** run through the picker in Discord once voting opens (or on a throwaway test week), and run `setup_server.py` plan/apply for the topic and welcome text.

## Phase 9: In Theaters / Streaming tags from TMDB (built; see STATUS.md)

Owner (2026-11-11): set them automatically. Both tags already exist as bot-only flag tags.
- Daily job: one TMDB call per movie (`append_to_response=release_dates,watch/providers`, region US), ~450 calls, well under TMDB's limits.
- **In Theaters:** a US theatrical release in the last 60 days and no US digital release yet.
- **Streaming:** on at least one US subscription service (TMDB's `flatrate`; rent/buy doesn't count).
- Info card gets a line like "📺 Streaming on Netflix, Max" (TMDB's provider data comes from JustWatch, which must be credited, e.g. in the card footer). Tags change through `MovieService.retag`; only changed posts are edited.
- Tags use up the 5-tag budget before genres, so a film In Theaters or Streaming shows one fewer genre.
- **Narrowed by the owner after the first dry run (2026-11-12):** the tags are only for new films. In Theaters = first US run within 60 days and not out at home; re-releases are ignored (they vary by theater chain). Streaming = first released within the last year and now watchable at home (digital release, subscription, rent or buy). Every card still lists subscription services ("📺 Streaming on …") even without the tag.

## Phase 10: engagement stats (planned)

From the SQLite data already stored: who watched what, rating spreads (per member, per film, group vs. solo), most active posts, weekly-pick participation (nominations, voters, turnout, how far down rankings were used). Shape (slash commands, a monthly post, or both) to be agreed with the owner first.

## Later ideas
- Off-machine backup copies (needs a destination from the owner).

## Open questions for the owner
Answered 2026-11-11: no `.ics` feed; Mark as spoiler moves the message; a solo Letterboxd log keeps an existing post's tags; channels renamed to pre-/post-watch discussion and the Post-watch tag retired.

Answered 2026-11-11: In Theaters / Streaming are set automatically (Phase 9); backups keep the newest 3.

Answered 2026-11-11 (Phase 8): entries deleted on Letterboxd have their post-watch embed deleted; `/watchlist import` is removed in favour of `/letterboxd sync`.

Answered 2026-11-12 (Phase 8): `/letterboxd` becomes a group (`link`/`sync`/`unlink`); a deleted entry's `#movies` note and feed line are deleted too.

Settled 2026-11-12: the Phase 4 watch → review question (the Phase 8 poller guard covers both cases).

No open questions right now. Phase 10 (stats) needs a short design talk before it's built.
