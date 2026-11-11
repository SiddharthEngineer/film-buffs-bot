# Status (as of 2026-11-11)

## Done and live

### Phase 0: connection ✅
- Discord app "Film Buffs Bot" (Film Buffs Bot#4646) is in the Film Buffs server with exactly the 13 permissions in the README, no Administrator.
- Role order: `Moderator` (the owner's admin role) above the bot's role, above `@everyone`.
- Server is **not** a Community server; nothing so far needs it (forums work without it).
- `.env` on the VPS holds the token, guild ID and TMDB key (chmod 600, gitignored).

### Phase 1: server layout ✅
Built by `setup_server.py --apply` from `server.yaml`; a fresh `--plan` reports no changes.

```
📢 Info       #welcome-and-rules (read-only, bot-maintained welcome + rules embed)
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
- 52 unit tests passing (`uv run pytest -q`).

## Phase 3R: spoiler companion posts ✅ (live, owner-tested)
Code and tests are done (25 tests pass). The old spoiler line is retired: its cog was switched off first, and `filmbuffs/spoilers.py` + `filmbuffs/cogs/spoilers.py` were rewritten.
Deployed 2026-11-11. Live-tested from a script on a throwaway post (since deleted): button custom_id, lazy creation, back-link, card → link button, Backlog + Post-watch tags, reuse on second open, and the webhook move with the original deleted. Parasite's card was refreshed (button added, "Spoiler line" field gone, tags Backlog + genres).

- `server.yaml`: new forum `#movie-spoilers` (same permissions as `#movies`); the `spoilers` tag is renamed **🍿 Post-watch** (key and ID unchanged) and means "has a spoiler discussion"; `#movies` guidelines and the welcome message describe the new flow. Applied by the owner; `--plan` reports no changes.
- Info card gets a **🔓 Spoiler discussion** button (`filmbuffs/cards.py`, a persistent `DynamicItem`, `spoilers:<tmdb_id>`). The first click creates "Title (Year) — Spoilers" in `#movie-spoilers` (starter links back, no 👍), adds Post-watch, and turns the button into a link button. A per-movie lock means concurrent clicks create one post. Spoiler posts deleted by hand are forgotten and recreated on the next click.
- `/spoiler`: ephemeral link to the movie's spoiler post (creates it). Inside a spoiler post it links back to `#movies`.
- **Mark as spoiler** (anyone, in `#movies` posts): re-sends the message in the spoiler post via the "Film Buffs spoiler moves" webhook (on `#movie-spoilers`) as the author, with "↪ replying to" and "moved from the spoiler-free post by @X" lines, attachments re-uploaded; then deletes the original. Nothing is posted in `#movies`. If any send or the delete fails, the copies are deleted and the original stays.
- Migration 2: `spoiler_thread_id`, `spoiler_starter_message_id`, `card_version`; it clears the old spoiler-line columns and the `spoilers` flag (Parasite). On startup the bot refreshes old info cards once (adds the button, drops the "Spoiler line" field, retags).
- The bot no longer requests the Message Content intent.

## Phase 4: Letterboxd integration ✅ (deployed, awaiting owner test)
Deployed 2026-11-11. 39 unit tests pass. Not yet exercised end-to-end with a real linked account (no one is linked yet).

- `/letterboxd username:<name>` links an account (accepts `name`, `@name` or a profile/feed URL; checks the RSS feed exists). With no argument it shows the linked account and the last poll error, if any. `/letterboxd-unlink` stops posting. One Letterboxd account per member; entries stay in the DB after unlinking.
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

## Phase 6: Letterboxd backfill script ✅ (built, awaiting a real export)
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
- **First real dry run (owner's export, wokewatcher29, 2026-11-11):** 451 of 459 films matched (422 exact, 23 by votes, 3 ±1 year, 3 title variants). Unmatched: 6 TV series, 1 TV episode, Butterfly (2024) (ambiguous). 451 posts to create (360 Watched (solo), 91 Backlog), 105 reviews (103 new spoiler posts), ~26 min. Not applied yet.
- `--apply`: stores every row in `letterboxd_entries` (source `backfill`, state `imported` or `unmatched`). Diary rows matching an RSS entry (same film + watched date) are linked to it (`export_uri`) instead of stored twice. Creates missing `#movies` posts: Watched (solo) for watched films, Backlog for watchlist films. Existing posts are never retagged. Reviews go into spoiler discussions with an "imported from Letterboxd history" footer, skipping any already posted there. **Nothing is posted in `#movies` or `#letterboxd-feed`.**
- Resumable: stopping and re-running skips finished work. The dry run after an apply should show nothing left to do.
- Runs as a short second connection next to the live bot (like `setup_server.py`). There's a tiny race if someone clicks a movie's spoiler button at the very moment the backfill creates that film's spoiler post.
- Migration 4: `export_uri`, `logged_date` on entries; `tmdb_title_matches`.

## Tooling
- uv 0.12 manages the environment (2026-11-11): `pyproject.toml` + `uv.lock` replaced `requirements*.txt`; `.python-version` is 3.14. The systemd unit still runs `.venv/bin/python` (uv builds that venv), so no service change was needed.

## Known state / cleanup
- The old webhook "Film Buffs reposts" on `#movies` is unused now. It's harmless; delete it in Server Settings → Integrations → Webhooks if you like (the bot won't delete it without asking).
- `data/filmbuffs.db` is not backed up yet (nightly backup is Phase 7). It holds the setup-script state (key → Discord ID) as well as movies; losing it means `setup_server.py` would no longer recognize the channels it manages.
