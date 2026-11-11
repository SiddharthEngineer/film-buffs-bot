# Roadmap

Work in phases. At the end of each phase: stop, summarize, list anything the owner must do by hand, and wait for a go-ahead.

Phases 0–2 are done (see [STATUS.md](STATUS.md)). The original Phase 3 was built and then redesigned with the owner; Phase 3R below replaces it.

## Guiding decisions (agreed with the owner)

1. **`#movies` is always spoiler-free.** Spoilers live in a companion post per movie in a new `#movie-spoilers` forum. No in-post spoiler line, no dividers.
2. **Never reorder messages inside a post.** No repost-at-the-bottom tricks.
3. **Star ratings are spoilers.** So are liked ♥, rewatch flags and review text. The spoiler-free side may only say *that* someone logged a film ("Siddharth logged this on Letterboxd"), never how they rated it.
4. **Letterboxd is the source of truth for who watched what.** Nobody logs watches in Discord; the bot pulls from Letterboxd.
5. **SQLite tracks everything** (movies, Letterboxd entries with full detail, weekly picks, votes) so engagement stats can be built later.
6. Prefer native Discord features (forum tags, polls, scheduled events, context menus, buttons). If something is impossible or awkward in Discord's API, stop and propose the closest alternative instead of silently changing the design.

---

## Phase 3R: spoiler companion posts (next up)

Replace the in-post spoiler line with a separate spoiler post per movie.

**Server (`server.yaml`, then plan/apply):**
- Add forum `#movie-spoilers` in 🎬 Movies, same permissions as `#movies` (only the bot creates posts; everyone can reply).
- Repurpose the `spoilers` tag in `#movies` to mean "this movie has a spoiler discussion". Keep the tag **key** `spoilers` so its Discord ID survives; only the display name/emoji change. **Name still to pick** with the owner. Suggestions: "Spoiler Talk" (🔓), "Post-watch" (🍿), "Discussed" (💬).
- Update the `#movies` guidelines and the welcome message to describe the new flow.

**Behavior:**
- Every movie info card gets a **🔓 Spoiler discussion** button.
  - First click when none exists: create "Title (Year) — Spoilers" in `#movie-spoilers`, reply ephemerally with the link, then edit the info card so the button becomes a direct link button.
  - Use a persistent component (`discord.ui.DynamicItem` with a `custom_id` like `spoilers:<tmdb_id>`) so buttons keep working after restarts.
- The spoiler post's starter message: "⚠️ Spoilers for Title (Year). Everything here may spoil the film. ← Back to the spoiler-free post" (with link). No 👍 reaction; votes stay on the `#movies` post.
- Apply the repurposed tag to the `#movies` post when its spoiler post is created.
- **Mark as spoiler** (message context menu, anyone, in `#movies` posts): move the message into the movie's spoiler post (create it if needed) via webhook with the author's display name + avatar, attachments re-uploaded, a small "moved from the spoiler-free post by @X" note and reply context as a "↪ replying to" line; then delete the original. Do **not** repost anything in `#movies`. Confirm with the owner whether moving is acceptable or whether it should instead ask the author to edit in `||bars||`; the owner chose this option ("A") but stressed "never rearrange messages".
- `/spoiler` becomes "open this movie's spoiler post" (create if needed, ephemeral link), usable inside a `#movies` post or its spoiler post.
- Remove: `||markup||` listener, dividers, the "Spoiler line" embed field, earliest-wins marker logic.

**Data:**
- Migration: add `spoiler_thread_id`, `spoiler_starter_message_id` to `movies`; stop using `spoiler_marker_message_id`, `spoiler_divider_message_id`, `spoiler_marked_at` (leave or drop).
- One-off cleanup for Parasite (see STATUS.md "Known state").

**Done when:** a member can open any `#movies` post without seeing spoilers, click one button to reach that movie's spoiler post, and "Mark as spoiler" moves a message out without touching the order of anything else. Tests cover lazy creation (no duplicates under concurrent clicks) and the move path.

---

## Phase 4: Letterboxd integration

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
- Open question: when one person logs a film that's still `Backlog` (not group-watched), should the tag change? Original spec only covers auto-created posts (`Watched (solo)`). Ask the owner.

## Phase 5: weekly pick, events, history

Unchanged from the original spec except where noted. Times are America/Chicago.
- **Mon 12:00 AM:** native poll in `#watch-planning` for next week's movie. Options = top backlog posts by 👍 on the starter message, excluding `Watched`/`Up Next`, up to the poll answer limit (10; re-verify). Labels `Title (Year)`; the poll message also links each post.
- **Sun 11:59 PM:** end the poll explicitly via the API. Tie-break (configurable in `server.yaml`): higher 👍, then earliest added. Retag winner `Up Next` (remove `Backlog`). Create scheduled event "🎬 Watch week: <Title>" (EXTERNAL; needs `entity_metadata.location` and `scheduled_end_time`) spanning the next Mon 12:00 AM → Sun 11:59 PM, linking the post; announce in `#announcements`.
- **When a watch week ends:** retag `Watched`, append to `#watch-log`: `Wk <ISO week> · <date range> · <Title (Year)> · won <votes>–<runner-up votes>`.
- Sunday night does both (current week ends + next poll closes) in one job, in a safe order.
- Zero votes or empty backlog → post a notice and skip the week.
- `/schedule` (ephemeral): current poll, this week's movie, next week's movie.
- `.ics` calendar of all watch weeks. **Ask the owner for domain/port and whether there's a reverse proxy before exposing anything.**
- Store polls, options, vote counts and results in SQLite (stats).

## Phase 6: Letterboxd backfill script

Standalone `backfill.py`, run by hand.
- Input: a Letterboxd data export zip (`watched.csv`, `diary.csv`, `reviews.csv`, `ratings.csv`, `watchlist.csv`) + the Discord user it belongs to; or just a username to use RSS (recent entries only).
- Exports have title, year, Letterboxd URI, not TMDB IDs: resolve via TMDB search (title + year), cache results, and write `backfill_unmatched.csv` for anything ambiguous instead of guessing.
- Watched/diary → ensure a post exists (new ones `Watched (solo)`). Watchlist → ensure a post exists tagged `Backlog` unless group-watched. Reviews → into the movie's **spoiler post**, deduped against what the RSS poller already posted. Everything goes into the SQLite entries table. Mark imported RSS GUIDs as seen.
- `--dry-run` (default) reports counts by tag, reviews to post, unmatched, and estimated runtime under rate limits; `--apply` executes; resumable; throttled. Filters: `--only-watchlist`, `--min-rating`, `--since YYYY-MM-DD`.
- **Show the owner dry-run numbers before applying.** It may create hundreds of posts.

## Phase 7: ops & docs

- Nightly SQLite backup to `backups/` (keep 14 days). Use SQLite's online backup API, not a file copy.
- Post failures to `#bot-logs` (logging handler + error hooks), plus a startup notice.
- Admin commands as needed (e.g. re-sync a post's info card or tags, unlink someone's Letterboxd).
- Tests: TMDB matching, spoiler-post creation, schedule/tie-break logic, RSS dedupe.
- README admin cheat sheet; keep STATUS.md current.

## Later ideas
- Engagement stats from the SQLite data (who watched what, rating spreads, most-active posts, weekly-pick participation).
- In Theaters / Streaming tags: the original spec listed them but gave no rule. Option: set them from TMDB release dates and US watch providers. **Ask the owner** before building.

## Open questions for the owner
1. Name and emoji for the repurposed `spoilers` tag ("has a spoiler discussion").
2. Mark as spoiler: move the message to the spoiler post (current plan), or ask the author to edit instead?
3. Phase 4: should a solo Letterboxd log change an existing `Backlog` post's tag?
4. Phase 5: domain/port/reverse proxy for the `.ics` feed.
5. In Theaters / Streaming: automate from TMDB, or leave manual/unused?
