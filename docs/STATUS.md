# Status (as of 2026-11-12)

> **Channel names (2026-11-11):** `#movies` (key `movies`; was briefly `#pre-watch-discussion`) and **`#post-watch-discussion`** (key `movie_spoilers`, was `#movie-spoilers`). Older sections below use the names of their time.

## Done and live

### Phase 0: connection ✅
- Discord app "Film Buffs Bot" (Film Buffs Bot#4646) is in the Film Buffs server with exactly the 13 permissions in the README, no Administrator.
- Role order: `Moderator` (the owner's admin role) above the bot's role, above `@everyone`.
- Server is **not** a Community server; nothing so far needs it (forums work without it).
- `.env` on the VPS holds the token, guild ID and TMDB key (chmod 600, gitignored).

### Phase 1: server layout ✅
Built by `setup_server.py --apply` from `server.yaml`; a fresh `--plan` reports no changes.

```
📢 Info       #welcome-and-rules (read-only, bot-maintained "🎬 Welcome" embed, then a "📖 How to" embed)
              #announcements (read-only)
💬 Chat       #general (the server's original channel, adopted), #trailers-and-news
🎬 Movies     #movies (forum; only the bot creates posts, everyone can reply)
📅 Planning   #watch-planning, #watch-log (read-only), #letterboxd-feed (read-only)
🔊 Voice      Watch Party, Discussions (the original voice channels, adopted + renamed)
🔧 Admin      #bot-logs (hidden from @everyone; Moderator sees it as admin)
```

- The original `#games`, `#music` and the default "Text Channels"/"Voice Channels" categories were deleted via the `retire:` list (they were empty).
- `#movies` tags (19 of 20): status tags, all moderated (only the bot applies them): Backlog, Up Next, Watched, Watched (solo), Post-watch (was Spoilers), In Theaters, Streaming. Genre tags: Action, Adventure, Animation, Comedy, Crime, Documentary, Drama, Fantasy, Horror, Romance, Sci-Fi, Thriller (mapping from TMDB genres is in `server.yaml`).
- Forum default sort: latest activity. Guidelines text explains `/addmovie`, 👍 voting and the spoiler discussions.

### Phase 2: `/addmovie` ✅ (user-tested)
- TMDB autocomplete shows `Title (Year) — Director`; choice values are `tmdb:<id>` so a typed numeric title like "1917" is never read as an ID. Pasted TMDB movie URLs are also accepted. Anything else is rejected ephemerally and no post is created.
- Duplicate → ephemeral link to the existing post. Posts deleted by hand are forgotten, and the next `/addmovie` recreates them.
- Post: title `Title (Year)`, embed with poster thumbnail, year, runtime, director(s), genres, synopsis, TMDB + Letterboxd links (`https://letterboxd.com/tmdb/<id>`, confirmed working in a browser), added by, pitch. Tags: Backlog + up to `max_genre_tags` (3) genre tags. The bot adds 👍 (the backlog vote).
- Autocomplete answers in ~0.3–0.5 s (Discord's limit is 3 s).

### Ops already in place
- systemd unit `film-buffs-bot` (enabled, `Restart=on-failure`, runs as `dev`, `ProtectSystem=strict` with write access only to `data/` and `backups/`).
- Commands are synced to the guild only (instant updates).
- 53 unit tests passing (`uv run pytest -q`).

## Phase 3R: spoiler companion posts ✅ (live, owner-tested)
Code and tests are done (25 tests pass). The old spoiler line is retired: its cog was switched off first, and `filmbuffs/spoilers.py` + `filmbuffs/cogs/spoilers.py` were rewritten.
Deployed 2026-11-11. Live-tested from a script on a throwaway post (since deleted): button custom_id, lazy creation, back-link, card → link button, Backlog + Post-watch tags, reuse on second open, and the webhook move with the original deleted. Parasite's card was refreshed (button added, "Spoiler line" field gone, tags Backlog + genres).

- `server.yaml`: new forum `#movie-spoilers` (same permissions as `#movies`); the `spoilers` tag is renamed **🍿 Post-watch** (key and ID unchanged) and means "has a spoiler discussion"; `#movies` guidelines and the welcome message describe the new flow. Applied by the owner; `--plan` reports no changes.
- Info card gets a **🔓 Spoiler discussion** button (`filmbuffs/cards.py`, a persistent `DynamicItem`, `spoilers:<tmdb_id>`). The first click creates "Title (Year) — Spoilers" in `#movie-spoilers` (starter links back, no 👍), adds Post-watch, and turns the button into a link button. A per-movie lock means concurrent clicks create one post. Spoiler posts deleted by hand are forgotten and recreated on the next click.
- `/spoiler`: ephemeral link to the movie's spoiler post (creates it). Inside a spoiler post it links back to `#movies`.
- **Mark as spoiler** (anyone, in `#movies` posts): re-sends the message in the spoiler post via the "Film Buffs spoiler moves" webhook (on `#movie-spoilers`) as the author, with "↪ replying to" and "moved from the spoiler-free post by @X" lines, attachments re-uploaded; then deletes the original. Nothing is posted in `#movies`. If any send or the delete fails, the copies are deleted and the original stays.
- Migration 2: `spoiler_thread_id`, `spoiler_starter_message_id`, `card_version`; it clears the old spoiler-line columns and the `spoilers` flag (Parasite). On startup the bot refreshes old info cards once (adds the button, drops the "Spoiler line" field, retags).
- The bot no longer requests the Message Content intent.

## Phase 4: Letterboxd integration ✅ (live)
Deployed 2026-11-11. 39 unit tests pass. The owner's account (wokewatcher29) is linked and polling cleanly. The 49 entries in the feed at link time are tied to the backfilled export entries (state `imported`); only Butterfly (2026 review) stays `baseline`, because the export's Butterfly (2024) was ambiguous on TMDB.
- Owner checks (2026-11-11): editing an old entry doesn't move it back to the top of the feed, so edits are never seen by the poller (the export sync in ROADMAP Phase 8 handles them). Two reviews of the same film are two diary entries and both land in the same post-watch discussion (e.g. Everything Everywhere All at Once: 2.5★ in 2022, 4★ rewatch in 2026). That is intended.

- `/letterboxd username:<name>` (now `/letterboxd link`, Phase 8) links an account (accepts `name`, `@name` or a profile/feed URL; checks the RSS feed exists). With no argument it shows the linked account and the last poll error, if any. `/letterboxd-unlink` stops posting. One Letterboxd account per member; entries stay in the DB after unlinking.
- On link, everything already in the feed is stored as `baseline` and never posted (history comes from the Phase 6 backfill).
- Poller (`filmbuffs/cogs/letterboxd.py`) runs every `letterboxd.poll_minutes` (15) and fetches feeds one by one with a random 3–12 s pause between them (`server.yaml`). On HTTP 403/429/5xx it stops the round and tries again next time. Conditional GET headers are sent if Letterboxd ever returns them (it currently doesn't).
- Each new diary entry (watch or review; lists are skipped), oldest first:
  1. `#movie-spoilers` post (created if needed): embed with stars, ♥, 🔁 rewatch, watched date, review text (as markdown, truncated with a link if very long).
  2. `#movies` post: "📓 @X logged this on Letterboxd. Their rating and review are in the spoiler discussion → link". No rating, no review. Mentions don't ping.
  3. `#letterboxd-feed`: "📓 @X logged **Title (Year)** → #movies post".
- No `#movies` post yet → it's created like `/addmovie` (added by that member) and tagged **Watched (solo)**. An existing post's tags are never changed by a solo log (owner decision 2026-11-11: Backlog stays Backlog).
- Missing `tmdb:movieId` → TMDB title+year search, only an unambiguous exact match is used; otherwise the entry is stored as `unmatched` and not posted.
- Migration 3: `letterboxd_links` and `letterboxd_entries` (full detail: rating, liked, rewatch, review, dates, URLs, the three Discord message IDs, state). Each message ID is saved as it's sent, so a failure resumes without duplicates; after 5 failed attempts an entry is marked `failed`. Entries for members who left the server are `skipped`.
- `#letterboxd-feed` topic updated via `server.yaml` (applied; `--plan` clean). User-Agent is now `FilmBuffsBot/0.4`.

## Phase 6: Letterboxd backfill script ✅ (applied 2026-11-11)
Built 2026-11-11, ahead of Phase 5 at the owner's request. 52 unit tests pass. Dry-run tested against live TMDB with a synthetic export; `--apply` was tested with fakes plus a read-only Discord connection check. The CSV format is confirmed by a dry run on the owner's real export (below); `--apply` hasn't been run yet.

```
uv run backfill.py data/imports/<export>.zip              # dry run (default)
uv run backfill.py data/imports/<export>.zip --apply      # asks to confirm
  --user <Discord ID>   default: whoever linked the export's username with /letterboxd
  --since YYYY-MM-DD  --min-rating 4  --only-watchlist | --no-watchlist  --store-only
  --overrides <filled-in unmatched CSV>
```
- Reads `diary.csv`, `reviews.csv` (merged into their diary entries), `watched.csv` (films with no diary entry), `ratings.csv`, `likes/films.csv`, `watchlist.csv`, `profile.csv`. Ignores `deleted/` and `orphaned/`.
- The export has only title + year (no director, no TMDB ID; Letterboxd film pages block bots). Matching rules, in `filmbuffs/backfill.py`:
  - "Same year" means the first release year **or any release in that year** (Hamilton: Disney+ 2020, TMDB first release 2025).
  - A single exact title (or original title) from the same year wins, unless a film within ±1 year clearly outweighs it by votes.
  - Several exact titles: take the clear favourite by TMDB votes (≥ 50 and ≥ 5× the runner-up), e.g. It (2017) 20,880 vs 3. Otherwise ambiguous.
  - Then a single exact title within ±1, then ±2 years (festival vs. release dates).
  - If there's no exact title and no TV series with that exact name: the same rules ignoring a leading The/A/An and a TMDB subtitle after a colon ("title variant": The School of Rock, Glass Onion: A Knives Out Mystery).
  - TV series (WandaVision, Chernobyl, …) are reported as "TV series (not supported)".
- The dry run prints how each film matched, writes `data/backfill_matches-<username>.csv` (every non-exact match with TMDB title, year and director, for a quick check) and `data/backfill_unmatched-<username>.csv` (top candidates as `ID · Title (Year) · Director · votes`). Fill in `tmdb_id` (an ID or TMDB URL) and re-run with `--overrides`. Hits are cached in `tmdb_title_matches`.
- The summary names the member by Discord display name and handle (looked up over the REST API, no gateway).
- **Applied (owner's export, 2026-11-11):** all 451 `#movies` posts exist (359 Watched (solo), 91 Backlog, plus 1 from `/addmovie`), 415 export entries stored as `imported`, 104 reviews in post-watch discussions, 93 watchlist films seeded. The 8 unmatched titles (TV + Butterfly) are stored as `unmatched`.
- **First real dry run (owner's export, wokewatcher29, 2026-11-11):** 451 of 459 films matched (422 exact, 23 by votes, 3 ±1 year, 3 title variants). Unmatched: 6 TV series, 1 TV episode, Butterfly (2024) (ambiguous). 451 posts to create (360 Watched (solo), 91 Backlog), 105 reviews (103 new spoiler posts), ~26 min. Not applied yet.
- `--apply`: stores every row in `letterboxd_entries` (source `backfill`, state `imported` or `unmatched`). Diary rows matching an RSS entry (same film + watched date) are linked to it (`export_uri`) instead of stored twice. Creates missing `#movies` posts: Watched (solo) for watched films, Backlog for watchlist films. Existing posts are never retagged. Reviews go into spoiler discussions with an "imported from Letterboxd history" footer, skipping any already posted there. **Nothing is posted in `#movies` or `#letterboxd-feed`.**
- Resumable: stopping and re-running skips finished work. The dry run after an apply should show nothing left to do.
- Runs as a short second connection next to the live bot (like `setup_server.py`). There's a tiny race if someone clicks a movie's spoiler button at the very moment the backfill creates that film's spoiler post.
- Migration 4: `export_uri`, `logged_date` on entries; `tmdb_title_matches`.

## Pre-/post-watch rename ✅ (live, 2026-11-11)
Owner request, done before running the backfill so its new posts get the new titles.
- Forums renamed via `server.yaml`: `#pre-watch-discussion` (key `movies`) and `#post-watch-discussion` (key `movie_spoilers`). New guidelines: pre-watch opens with "🚫 NO SPOILERS in this channel"; post-watch with "⚠️ SPOILERS ALLOWED". Welcome message and `#letterboxd-feed` topic reworded.
- The **🍿 Post-watch tag is retired** (removed from the forum and every post; the info card button already shows whether a post-watch discussion exists). `setup_server.py` gained `retire_tags:` (per forum, `{key, name}`, name must match) to do this. 18 of 20 tags used. Migration 5 drops the old `spoilers` flag from movie rows.
- Info cards (CARD_VERSION 2): button reads **🔓 Post-watch discussion**, and a last field "🚫 No spoilers in this post" reminds members where spoilers go. Existing cards and post-watch posts were refreshed at startup.
- Post-watch posts are titled `Title (Year) — Post-watch`; the starter says "Post-watch discussion for … Spoilers allowed" with a link back. All bot wording (/spoiler, Mark as spoiler, Letterboxd notes, backfill output) says pre-/post-watch discussion. Internal names (`spoiler_*` columns, `spoilers:<id>` button IDs) are unchanged so existing buttons keep working.
- Checked live: Parasite shows Backlog + genres, the reminder field and new button; its post-watch post was renamed.

## Phase 7: ops & docs ✅ (live 2026-11-11)
Deployed 2026-11-11: clean restart, startup notice seen in `#bot-logs`, first backups written (the latest after the backfill, with all 451 movies). `/admin pick` used by the owner (see Phase 5). 68 unit tests pass (`uv run pytest -q`). Bot version 0.5 (`FilmBuffsBot/0.5` User-Agent). No schema change.

- **Nightly backup** (`filmbuffs/backup.py`, `filmbuffs/cogs/ops.py`): a `discord.ext.tasks` loop at 04:00 America/Chicago (`server.yaml` → `ops.backup_time`) copies `data/filmbuffs.db` to `backups/filmbuffs-<UTC>Z.db` with SQLite's online backup API (consistent even while the bot or backfill writes), switches the copy to a single-file journal, runs `PRAGMA quick_check`, then renames it into place. Only the newest 3 backups are kept (`ops.backup_keep`, changed from 14 days on 2026-11-11 to save disk; each is ~0.5 MB); at least one is always kept. If the newest backup is over 24 h old at startup, one runs immediately (so the first deploy makes one).
  - **Why in the bot, not a systemd timer:** no extra unit for the owner to install with sudo, it already runs inside the service's sandbox (write access to `data/` and `backups/` only), and a failed backup reaches `#bot-logs` through the same handler. The trade-off (no backup while the bot is down) is covered by the startup catch-up.
- **`#bot-logs`** (`filmbuffs/botlogs.py`): a root logging handler posts ERROR+ records (and anything logged with `extra={"botlogs": True}`) with the tail of the traceback, Chicago time. Throttle: the same error (logger + message template + exception type) at most once an hour, with a "repeated N×" note; at most 5 messages per 10 minutes, the rest counted in the next message. The handler never posts its own failures. Startup notice on first `on_ready`: version, git commit, schema, last backup.
  - Covered: app command errors (now with command, user and channel), unhandled errors in events, views and task loops (discord.py logs them at ERROR), backup failures, and the Letterboxd poller.
  - Letterboxd: one ERROR when a feed fails 4 polls in a row or Letterboxd refuses 4 rounds in a row, one "works again" notice on recovery. Per-attempt entry posting failures are now WARNING (journal only); the 5th (giving up) is an ERROR.
  - Admin permission failures (`/admin` by a non-admin) get an ephemeral "admins only" and are not logged as errors.
- **`/admin`** (`filmbuffs/cogs/admin.py`), a command group with `default_member_permissions` = Manage Server, plus a runtime check (Manage Server, Administrator, or the `ops.admin_role` role, Moderator). Both ephemeral:
  - `/admin resync [post]`: `update_card` + `retag`, and refreshes the post-watch post. Works from the pre- or post-watch post.
  - `/admin letterboxd-unlink member:` unlinks a member's account (entries stay).
- README has an admin cheat sheet (commands, what lands in `#bot-logs`, backups and how to restore). The README command table was also brought up to date.

## Phase 5: weekly pick, votes, watchlists ✅ (live, owner-tested 2026-11-11)
*The ballot → poll → watch schedule below was replaced by Phase 5R (2026-11-12).*
Owner tested `/nominate`, `/schedule`, `/top` and the pinned leaderboard. `/admin pick` used for real: **Wildwood (2026)** is this week's movie (week of Nov 9; Up Next, event created, both posts pinned).
Bot version 0.6.1, schema 7. 90 unit tests pass. Design and owner decisions are in ROADMAP.md (Phase 5).

- **Rename:** `#pre-watch-discussion` → **`#movies`** via `server.yaml` (applied; `--plan` clean). Bot wording says "#movies post" / "spoiler-free post". The 105 post-watch starters were rewritten once to link back to "#movies" (tracked by `bot_meta.post_watch_starter_version`). `#watch-planning` topic and the welcome message describe the weekly pick and `/watchlist`.
- **Votes** (`filmbuffs/votes.py`): 👍 (any skin tone) on a movie's first message, kept in `votes` from reaction events. Every startup re-reads all cards' reactions in the background (~0.3 s apart) to catch missed events; that pass also removed the bot's own 👍 from old posts. New posts no longer get one.
- **Interest** = distinct members with a 👍 or the film on their uploaded watchlist (each counts once). Ranking: interest, then 👍, then earliest added.
- **Leaderboard:** a pinned bot message "🏆 Top 25 most wanted" in `#watch-planning`: top 25 not yet watched together (Up Next marked ⏭️), plus the top 3 per genre. Refreshed every minute if anything changed; reposted (and pinned) if deleted (`bot_meta.leaderboard_planning_message_id`). It started as a pinned `#movies` post, but a forum allows only one pinned post, and that pin now goes to the week's movie; the bot deleted the old post.
- **Scores** read `👍 3` when only votes count, or `**4** want it (👍 3 · 📋 2)` when watchlists are involved (one member with both counts once, so the parts can add up to more than the total).
- **`/top [genre] [include_watched]`**: ephemeral top 25.
- **`/watchlist import file:`** (replaced by `/letterboxd sync` in Phase 8) takes the Letterboxd export zip or its `watchlist.csv`, matches titles with the backfill's TMDB rules (cached), and replaces the member's stored watchlist. Films without a `#movies` post get one (Backlog, added by that member, source `watchlist`), one every ~3 s, with an ephemeral "done" note if it finishes within Discord's 15-minute reply window. `/watchlist show`, `/watchlist clear`. The owner's watchlist (from the backfill) was seeded by migration 6.
- **Weekly pick** (`filmbuffs/weekly.py`, `filmbuffs/cogs/weekly.py`, `server.yaml` → `weekly`): rows in `weeks` move ballot → ballot_closed → poll → picked → done (or skipped) on a 30 s tick, so restarts resume. First watch week **Nov 23–29** (`weekly.first_watch_week`): its ballot opened at deploy and closes **Sun Nov 15, 11:59 PM CT**; poll Nov 16–22.
  - `/nominate [title]` (autocomplete, or the post you're in; creates the post if it has none), `/unnominate`, `/schedule`.
  - Ballot message in `#watch-planning` updates live (every 30 s if changed). Poll answers are `Title (Year)` (≤ 55 chars); the poll message lists the posts. Poll duration is set an hour past Sunday 11:59 PM so it never ends early; the bot ends it at 11:59 PM, waits up to a minute for the final tally, stores counts and voters.
  - Winner: Up Next tag, external event "🎬 Watch week: Title" (location = the post link), announcement in `#announcements`, result line in `#watch-planning`. Watch week end: Watched tag + `#watch-log` line. Skipped weeks also get a notice and a log line.
  - **Pins:** when a watch week starts (Mon 12:00 AM), its movie's `#movies` post and its post-watch discussion (created if needed) are pinned in their forums. A forum has one pin, so each new week replaces the last; a skipped week leaves the previous pin.
  - **`/admin pick week:<This week|Next week> title:`** sets that week's movie: ends an open poll early (votes kept), puts a replaced winner back to its old status, edits or creates the event, announces "picked by an admin", and pins right away if the week has started. `#watch-log` says "picked by an admin". Migration 7: `override_by`, `winner_prev_status`, `pinned_at` on `weeks`.
  - Stored for stats: `weeks`, `nominations`, `week_options` (interest at close, poll votes), `poll_voters`.

## Phase 5R: one-week nominate → ranked vote ✅ (live 2026-11-12; owner applied setup_server.py; the vote picker is tested Fri 2026-11-13 when voting opens)
Bot version 0.8, schema 9. 112 unit tests pass. Replaces Phase 5's ballot → poll → watch pipeline (ROADMAP Phase 5R); 👍 votes, the leaderboard, `/top`, pins and `/admin pick` are unchanged.

- **Schedule** (`filmbuffs/weekly.py`): for watch week T, nominations run Mon 12:00 AM → Thu 11:59 PM of the week before, the vote Fri 12:00 AM → Sun 11:59 PM. States: ballot → ballot_closed → **voting** → picked → done (or skipped). Each week now runs one watch and one nominate/vote cycle; `/schedule` shows this week and next week.
- **Transition (owner, 2026-11-12):** `weekly.first_watch_week` moved to **2026-11-16**. The open ballot (opened Nov 9 for Nov 23, 1 nomination: Ramayana: Rise of a Legend) was moved by hand to watch week **Nov 16–22** (`weeks` and `nominations` rows re-keyed; copy of the DB before the move: `data/pre-5r-week-move.db`). Its nominations close **Thu Nov 12, 11:59 PM CT** and it's voted on **Fri Nov 13 → Sun Nov 15**; with only one nomination at the close, it wins without a vote. The Nov 23–29 ballot opens Mon Nov 16. Wildwood stays the Nov 9–15 movie.
- **Vote UI** (`filmbuffs/vote_ui.py`): while voting is open the ballot message has a **🗳️ Vote** button. It opens a private picker: "Your ranking" so far, one dropdown with the films not yet ranked ("Pick your 3rd choice…"), **Undo last** and **Clear**. Each pick is saved at once (`ranked_votes`). All components are DynamicItems keyed by week (`vote:`, `rank:`, `rank-undo:`, `rank-clear:` + date), so they work after restarts; a click after the vote closes says so and removes the controls. The ballot shows only "N members have voted" until the close. Picks are refused if the film isn't on the ballot or already ranked.
- **Count** (`instant_runoff`): each round every ballot counts for its highest-ranked film still in; more than half of the ballots still counting wins. Otherwise films with no votes go out together, or the one with the fewest; ties for last go out by fewest 1st choices, then `weekly.tie_break` (least interest, fewest 👍, added last). If the last two tie, that same rule decides. Nobody voted → skipped.
- **Results:** announcement ("Won the ranked vote: won 3–2 after 2 rounds (5 voters)"), a round-by-round summary in `#watch-planning`, the ballot embed says who won, `#watch-log` line `… · won 3–2 after 2 rounds`.
- **Stored:** migration 9 adds `ranked_votes(week_start, user_id, rank, tmdb_id, voted_at)` and `weeks.vote_rounds` (JSON rounds). For ranked weeks the `poll_*` columns are reused: opened/closed times, `total_votes` = voters, `winner_votes`/`runner_up_votes` = final round, `week_options.poll_votes` = 1st choices.
- **Nominations only** (owner, 2026-11-12): the ballot is exactly the eligible nominated films, in nomination order; the most-wanted list no longer fills it, and `weekly.poll_options` is gone. A nomination can be swapped or withdrawn until Thu 11:59 PM; after that `/nominate` says no ballot is open. Cap: 25 nominations (Discord's dropdown limit). At the Thursday close: no nominations → the week is skipped; **exactly one → it wins without a vote** (owner, 2026-11-12): Up Next, event, announcement "the only nomination", stored with `winner_votes` NULL and `total_votes` 0, `#watch-log` says "the only nomination, so no vote". `weekly.min_options` is gone.
- **`/admin pick`** during voting closes the vote early (ballots kept, count in `total_votes`).
- **Welcome links fixed:** `{#watch-planning}`, `{#letterboxd-feed}`, `{#watch-log}` used channel names with hyphens, which `setup_server.py`'s `{#key}` pattern (`\w+`) never matched, so they were posted as plain text, and validation used the same pattern so it didn't complain. They now use the keys, and `load_config` rejects any `{#…}` that isn't a channel key (suggesting `_` for `-`). Welcome text and `#watch-planning` topic describe the new week. **Not applied yet:** needs `setup_server.py` plan/apply.

## Phase 8: full Letterboxd sync from the export ✅ (live, owner-tested 2026-11-12)
Bot version 0.7, schema 8. 94 unit tests pass (`uv run pytest -q`). Owner decisions (2026-11-12): `/letterboxd` is a command group, and a deleted entry loses all three bot messages.

- **Commands:** `/letterboxd link [username]` (no username: shows the link, last poll error and last sync), `/letterboxd sync file:`, `/letterboxd unlink`. These replace `/letterboxd username:` and `/letterboxd-unlink`; Discord makes a base command unusable once it has subcommands. `/watchlist import` is gone (`filmbuffs/watchlist.py` deleted); `/watchlist show` and `clear` stay and point to `/letterboxd sync`.
- **`/letterboxd sync`** (`filmbuffs/sync.py`, `filmbuffs/cogs/letterboxd.py`): reads the zip (≤ 25 MB, must have `profile.csv`). The export's username must not be linked to another member, and must match the uploader's own link if they have one. It matches titles (cached; a "Matching N films" note if over 40 are new), then replies with an ephemeral preview: diary entries new/changed/removed, watched-only films, watchlist (+/−), Discord work (posts to create by status, reviews to post, embeds to edit, removed entries' messages to delete), unmatched films, up to 8 examples (✏️ edited, 🗑️ deleted) and an estimate. **Apply** / **Cancel** (10 min, uploader only). If nothing differs it just says "Already in sync". Apply re-plans first (things may have changed since the preview), then edits the message with progress about every 5 s. The final note goes in the same message, or by DM if the 15-minute interaction window has passed. One sync per member at a time. **Fix (2026-11-12, owner test):** the final note is now always sent as a new ephemeral message (plus a DM after 15 minutes). Before, it only edited the progress message, and a dismissed ephemeral message silently accepts edits, so the owner never saw it.
- **Diff:** export rows are matched to stored rows by their `boxd.it` URI (diary entry URI for diary/reviews; film URI for watched-only and watchlist rows), then by the old `export:` GUID. RSS entries that aren't linked yet are matched by TMDB ID + watched date, and get the URI. Compared fields: rating, ♥, rewatch, watched date, review text (and watch↔review). Watched-only rows compare rating and ♥.
  - **New:** stored as `imported` (source `export`), or `unmatched`. Reviews get a post-watch embed ("imported from Letterboxd history"). Films without a `#movies` post get one: Watched (solo), or Backlog for watchlist films, source `letterboxd-sync`, 2 s apart. Nothing is posted in `#movies` or `#letterboxd-feed`.
  - **Changed:** the row is updated (`updated_at`), and the bot's own embed is edited in place. The thread is un-archived first, because Discord refuses edits in archived threads. If the poller posted the entry live, its `#movies` note is re-worded too ("Their rating and review are…"). An embed someone deleted by hand is posted again.
  - **Removed:** a stored entry that has a URI, is missing from the export, and was seen before the export was made (time from the zip name `…-YYYY-MM-DD-HH-MM-utc.zip`, else the end of the newest logged day). Its embed, `#movies` note and feed line are deleted (only the bot's own messages, IDs cleared as each goes) and it becomes `removed` (`removed_at`), so the row is kept for stats. A zip without `diary.csv`/`watched.csv` never removes diary rows. RSS entries never matched to the export are left alone (counted as "not in export"). The preview warns if a sync would remove ≥ 5 entries and over 25% of the member's diary.
  - **Watchlist:** replaced (`watchlist_imports.source = sync`); watchlist rows in `letterboxd_entries` are added or marked removed the same way.
  - Every Discord step is recorded as it happens, so re-running a sync resumes. Failures are counted, logged as WARNING and reported in the final note. The DB part runs under the poller's per-member lock; the Discord part doesn't, so it never stalls a poll round.
- **Watchlist wording (2026-11-12):** the owner read "counts like a 👍" as a literal reaction. The bot can't add reactions for a member, so the texts now say each watchlist film counts toward the rankings like that member's 👍 would, shown as 📋 in `/top` and the leaderboard (welcome message, `/watchlist show`, sync note, README).
- **Owner's first live sync (2026-11-12, newer export):** 22 entries changed (21 star ratings re-rated on Letterboxd, plus Resident Evil's review), 20 embeds edited, 6 watchlist films added (99 total), 4 Backlog posts created (Clayface, Fjord, Wicker, Wild Horse Nine), 0 failures.
- **Poller guard:** when a `letterboxd-review-N` item appears and `letterboxd-watch-N` is already stored (a review added to an entry already logged), the watch row is updated (kind, review, rating, ♥, rewatch, date). The review item is stored as `merged` so it's never read again. The existing embed and `#movies` note are edited in place. If the poller hasn't posted the entry yet (pending), it just posts the updated row. Imported/baseline entries without an embed get one posted. No second note or feed line.
- **Migration 8:** `updated_at`, `removed_at`, `embed_source` (`feed` | `history`, which decides the "imported" footer when an embed is re-rendered; backfilled for existing embeds) on `letterboxd_entries`; new `letterboxd_syncs` table (one row per applied sync, counts as JSON).
- **`backfill.py`** is now a thin wrapper around the same plan/apply for the shell (same flags: `--user`, `--since`, `--min-rating`, `--only-watchlist`/`--no-watchlist`, `--store-only`, `--overrides`, `--apply`, `--yes`). Its dry run prints the same preview plus up to 30 examples. Planning moved out of `filmbuffs/backfill.py`, which now only parses and matches.
- **Dry run on the owner's real export** (2026-11-11 zip, against a copy of the live DB): 0 new, **1 changed** (Resident Evil (2026): the review was edited on Letterboxd after the feed saw it, so 1 embed to edit), 0 removed, 196 unchanged, watchlist 93 (no change), 8 unmatched (the known TV series + Butterfly). That confirms the RSS and export review text convert identically for the other 42 reviews.

## Phase 9: In Theaters / Streaming from TMDB ✅ (deployed 2026-11-12)
Bot version 0.9, schema 10. 138 unit tests pass. Rules narrowed with the owner after the first dry run (2026-11-12): the tags are only about **new films**, still only in theaters or out for home viewing. Re-releases are ignored, since they depend on the theater chain.
- **Check** (`filmbuffs/availability.py`): one TMDB call per movie (`/movie/{id}?append_to_response=release_dates,watch/providers`; response keys `release_dates` and `watch/providers`, verified live), 0.25 s apart. US only.
  - **First release:** the earliest US theatrical (type 2 limited, 3) or digital (4) release dated today or earlier. Premieres/festivals (1), physical (5) and TV (6) don't count, and later theatrical dates (anniversary or one-night re-releases) are ignored. Future dates are ignored too, so a scheduled digital release doesn't count early.
  - **In Theaters:** the first release was theatrical, within the last 60 days (today included), and the film isn't watchable at home yet: no past digital release and no subscription, rent or buy provider.
  - **Streaming:** the first release was within the last 365 days, the film isn't In Theaters, and it's watchable at home (a past digital release, or a subscription/rent/buy provider). A film that left theaters but isn't out at home yet gets neither tag.
  - A film whose TMDB release date (worldwide) is over a year old is never new, even if its US dates are recent (Sholay, 1975, has only 2025 restoration dates; fixed right after the first live run).
  - Older films get neither tag, but their card still lists subscription services (owner: on every card). `free` (e.g. Kanopy) and `ads` services aren't listed.
- **When:** daily at 05:00 Chicago (`filmbuffs/cogs/availability.py`), plus a catch-up a minute after startup if the last run (`bot_meta.availability_last_run`) is over 24 h old. `/admin availability` runs it now. New posts (`/addmovie`, Letterboxd, sync, backfill) get their availability before they're created, so they need no later edit. If TMDB fails then, the daily run fills it in.
- **Edits:** a post is retagged only when its flags change, and its card is edited only when the card text changes (via `MovieService.retag` / `update_card`, 1 s apart). Other changes (dates, a provider hidden from the card) only update the DB.
- **Card:** a "Where to watch (US)" field above the no-spoilers reminder: "🎟️ In theaters now" and/or "📺 Streaming on Netflix, HBO Max, Philo and 2 more" (max 4 names). Add-on channels ("… Amazon Channel") and "with Ads" plans are left out of the card unless they're the only option. When the field is shown, the footer adds "Streaming data: JustWatch" (TMDB's terms). No `card_version` bump: only cards whose text changes are edited.
- **Stored:** migration 10 adds `movies.availability` (JSON: in_theaters, streaming_tag, subscription services, first release and first digital date) and `availability_checked_at`. The `in_theaters`/`streaming` entries in `movies.flags` follow it.
- **Dry runs against live TMDB** (2026-11-12, copies of the live DB, no Discord): 457 movies, 0 errors. First rules: 14 In Theaters (3 of them re-releases: Rocky, American Psycho, Moonlight) and 385 Streaming (84%, including live-TV bundles), which the owner found too broad. **Narrowed rules: 11 In Theaters** (Wildwood, Ramayana, Clayface, Fjord, Wicker, Wild Horse Nine, Digger, Littermates, Whalefall, You Can See Everything, The Only Living Pickpocket in New York) and **24 Streaming**, so 35 posts retagged. 396 cards get the "Where to watch" field. The first run takes about 8–10 min with the 1 s pauses and starts by itself a minute after the deploy (the catch-up). Later runs only touch what changed. **Live (2026-11-12):** the first run took 14 min: 35 retagged, 396 cards edited, 0 errors. After the old-film guard, a re-run removed Streaming from 4 old films (e.g. Sholay). Now **11 In Theaters, 20 Streaming**.

## Tooling
- uv 0.12 manages the environment (2026-11-11): `pyproject.toml` + `uv.lock` replaced `requirements*.txt`; `.python-version` is 3.14. The systemd unit still runs `.venv/bin/python` (uv builds that venv), so no service change was needed.

## Known state / cleanup
- Webhooks: only "Film Buffs spoiler moves" on `#post-watch-discussion` remains (used by Mark as spoiler). The unused "Film Buffs reposts" on `#movies` (original Phase 3) was deleted at the owner's request on 2026-11-11. Server Settings → Integrations → Webhooks shows 0 because Discord's server-wide list leaves out webhooks on forum channels; the API lists them.
- `data/filmbuffs.db` holds the setup-script state (key → Discord ID) as well as movies; losing it means `setup_server.py` would no longer recognize the channels it manages. It is backed up nightly to `backups/` (Phase 7), but only on this machine; copy `backups/` elsewhere now and then for an off-machine copy.

## Welcome page split (2026-11-12) ✅
- `#welcome-and-rules` now has two bot-maintained embeds from `server.yaml` `messages:`. First, **🎬 Welcome to Film Buffs** (key `welcome_rules`, edited in place, same message ID): the owner's friendly overview (the weekly loop, pre-/post-watch discussions, Letterboxd, upcoming stats, ideas welcome, be nice). Second, **📖 How to** (new key `how_to`): the old detailed instructions (movies and 👍, weekly pick, spoiler policy, Letterboxd commands).
- Applied with `setup_server.py --apply`; `--plan` reports no changes. Order holds because messages are applied in YAML order and the first one keeps its ID. If either message is deleted, reposting it would put it out of order.
