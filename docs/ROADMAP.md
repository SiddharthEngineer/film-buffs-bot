# Roadmap

Work in phases. At the end of each phase: stop, summarize, list anything the owner must do by hand, and wait for a go-ahead.

Phases 0–10, 5R, 12 and 12b are built and live (see [STATUS.md](STATUS.md)). Phase 12 (private groups; the monthly movie moved out of the main server) went live 2026-11-15 and waits for the owner's `setup_server.py --apply`, a restart and a hands-on test. Phase 11 (stats page, private views, Wrapped) is defined but not planned or started. Phase 6 was built before Phase 5 at the owner's request. The original Phase 3 was built and then redesigned with the owner; Phase 3R below replaces it.

**2026-11-15 (owner):** 👍 reactions no longer count for anything; the leaderboard, `/top` and vote tracking are removed. `/nominate` also creates the movie's post (with a pitch), like `/addmovie`. See STATUS "Fixes of 2026-11-15".

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

## Phase 10: rolling nominations, one always-open vote, monthly movie (built; see STATUS.md)

Owner (2026-11-13): replace Phase 5R's split week (nominate Mon–Thu, vote Fri–Sun) with **one period where nominations and votes update as they happen**.

- Each member keeps up to **3 active nominations, ranked 1–3** (owner, 2026-11-13), in **one shared pool** used by both the weekly and the monthly ballot (below).
- **The weekly ballot uses one nomination per member**, their highest-ranked one that's still eligible. The other two are backups: when a member's film wins (either track) or is otherwise removed, their next nomination moves up and next week's ballot updates by itself. This also keeps the ballot within the 25-option dropdown limit (one film per member).
- **Nominations roll over** from week to week until the member withdraws them or the film wins. Most of the ballot is expected to carry over, so voting can start right away.
- **Voting is open the whole week**, closing Sun 11:59 PM. Members can rank and re-rank any time, so they can react to nominations that arrive later in the week.
- Still ranked choice (IRV, same counting and tie-breaks as 5R); partial rankings are already fine, so a late nomination is just unranked on older ballots.
- The private member view from Phase 11 could double as "my nominations + my ranking".
- **Managing nominations** (owner, 2026-11-13; exact UI not decided): **managing them only by typing commands is fine** (e.g. `/nominate film: rank:`, `/unnominate`, and a command that shows "your nominations 1–3" privately). Buttons in `#watch-planning` that open a private (ephemeral) view are a nice extra, not required. Discord limit: buttons and modals can't offer the TMDB search-as-you-type that `/nominate` has, so adding a film stays a command either way. Check the current modal docs before deciding.
- To decide when planned: whether rankings also roll over to the next week; what happens to a ranking when a film is withdrawn or nominated late (drop it and close the gap? notify voters?); whether there's a late-nomination cutoff for fairness; whether "N members have voted" stays the only thing shown while open; how the switch-over from 5R works mid-cycle; and updated `/schedule`, ballot, topic and welcome text.
- Comes before the stats phase (owner, 2026-11-13).

### Monthly movie (owner, 2026-11-13)

Interest in the club varies, so there are two tracks: a **weekly movie** for invested members and a **monthly movie** that casual members can plan around.

Agreed (owner, 2026-11-13):
- The monthly movie is **on top of** the weekly movies; the weekly track never pauses. In the monthly film's week, invested members have two films.
- **No separate nominations:** both tracks draw from the same pool of up to 3 active nominations per member.
- `#watch-planning` always has **two ballots open: next week and next month**, both using the ranked-choice (IRV) picker.
- **The monthly vote takes precedence.** The weekly pick may never take the film that is **currently winning the monthly vote**; the weekly gets second pick. Proposed mechanism (Claude, to confirm): when the weekly vote closes, count the monthly vote as it stands; if its leader is on the weekly ballot, that film is dropped from the weekly count and its votes go to the voters' next choices. The rule only applies at the weekly close; the monthly leader is never shown during the week, because votes are secret until each vote closes and removing it live would reveal the monthly standings.
- **The monthly ballot also uses each member's #1** (highest-ranked eligible) nomination (owner, 2026-11-13), so both ballots show the same films; voters rank them separately for each. Because the monthly vote runs for weeks, its ballot changes as weekly winners leave and backups move up. A film that joins mid-month is just unranked on existing monthly ballots, so voters may want a nudge to look again.
- **A film picked for the weekly leaves the monthly vote** right away: it drops off the monthly ballot and out of everyone's monthly rankings (later ranks move up), and its nominator's next nomination moves up. Same when the monthly winner is picked: it leaves the pool.

Proposed by Claude, still to confirm:
- **A real watch party:** the monthly film gets a fixed date and time (e.g. the last Saturday of the month, evening) in the Watch Party voice channel, as a Discord scheduled event with a start time. The weekly event spans the whole week.
- **Longer lead time:** the monthly vote closes well before the watch party (e.g. about 2 weeks), so casual members can find, rent or buy the film.
- Voters who had ranked a film that left the monthly ballot may need a heads-up.
- Option: limit the monthly ballot to films that are easy to watch (TMDB's Streaming / In Theaters / rent-or-buy data from Phase 9).
- **Opt-in ping roles**, so casual members only hear about what they care about: e.g. `@Weekly Movie` and `@Monthly Movie`, self-assigned with buttons. Announcements ping the matching role instead of everyone. Roles go through `server.yaml`; the bot already has Manage Roles, but its role must sit above these roles.
- **Labelling:** "🌙 Monthly movie" vs "🎬 Weekly movie" in the announcement, event name, `#watch-log` line and `/schedule`. Possibly a forum tag (19 of 20 tag slots are used, so one is left).
- Later variation: a rotating member curates the monthly options.
- To decide: whether the "monthly leader" check at the weekly close is right; watch-party day/time; when the monthly vote opens and closes; whether the monthly ballot is limited to easy-to-watch films; role names and where the role buttons go; how `/admin pick` and `/admin clear` handle the monthly movie; stats for each track (Phase 11).

### Phase 10 decisions (owner, 2026-11-13, before the build)
- **Rankings carry over** from one vote to the next on each track. A film that leaves the ballot (won either track, withdrawn, its nominator's #1 changed) drops out of every ranking and later ranks move up. A film that joins is unranked; the picker marks it 🆕 ("N films joined since you last ranked"). No DMs.
- **No late-nomination cutoff**: whatever is on the ballot at Sun 11:59 PM is counted.
- **"N members have a ranking"** stays the only thing shown while a vote is open.
- **Monthly schedule:** month M is voted on during M-1 and **closes on the last day of M-1 at 11:59 PM** (the next month's vote opens right away). The film is watched any time during M. **Discussion: a voice event in Watch Party on the 1st of M+1 at 8 PM CT** (2 h). Admins can move it (edit the event in Discord, or `/admin discussion`); the bot never resets its time. At the end of M the film is tagged Watched and logged.
- **First monthly movie: December 2026** (vote open now → Mon Nov 30 11:59 PM; discussion Fri Jan 1 2027, 8 PM).
- **Monthly leader rule confirmed:** at each weekly close, the film currently winning the monthly vote is left out of the weekly count (its votes go to next choices); it stays on the ballot. Never shown while voting. When both close at the same minute (a month ending on a Sunday), the monthly vote is counted first.
- **Ping roles:** `@Weekly Movie` / `@Monthly Movie`, mentionable, created through `server.yaml` `roles:`; toggle buttons on the 📖 How to page. Weekly pick announcements ping `@Weekly Movie`, monthly ones `@Monthly Movie`.
- Claude's defaults (not asked): no easy-to-watch filter on the monthly ballot; no new forum tag (the monthly film uses Up Next and isn't pinned, since a forum has one pin and the weekly film has it); nominating = commands plus a private 📋 My nominations view with ⬆️/🗑️ buttons; `/admin pick|clear` take a `slot` (this/next week or month).

## Phase 12: private groups with their own monthly movie (built; see STATUS.md)

Owner (2026-11-15): the monthly movie moves out of the main server into **private groups** of friends. The first group is **OSU**. More can be added later (e.g. high-school friends, a partner's friends). The main server keeps only the weekly vote. Each group's vote is fully independent of the main vote and of every other group.

### Main server
- **Weekly only.** The monthly track, the monthly-leader rule at the weekly close, the 🌙 ballot in `#watch-planning` and the monthly wording in topics and the How to page are removed.
- **The December main monthly vote is deleted**, along with its ballot message. Nobody had ranked it.
- The two existing pool nominations stay with the main weekly vote.
- `@Monthly Movie` role: see the open questions below.

### Groups (config)
- A `groups:` list in `server.yaml`, one entry per group (`key`, display name, role name). From each entry, `setup_server.py` makes:
  - a **role**, which is both the membership and the group's ping;
  - a **category**, hidden from `@everyone` and visible to the role and `@bot`;
  - a **planning** channel (ballot, results, announcements), a **chat** channel and a **voice** channel.

  Adding a group means adding an entry, then `--plan` / `--apply`.
- **Membership is the Discord role.** The owner assigns it in Discord (Server Settings → Roles, or right-click a member → Roles). There is no bot command for it.
- **Admins can't be left out.** Discord always shows every channel to the server owner and to any role with Administrator, including Moderator. Docs checked 2026-11-15: Administrator "bypasses channel permission overwrites" and the owner gets all permissions. The owner can mute or hide a group's category in their own client.
- **Context comes from the channel.** `/nominate`, `/unnominate`, `/nominations`, `/schedule` and the ballot buttons act on a group when used in a channel (or a thread of one) in that group's category. Anywhere else, they act on the main server. Ballot button IDs carry the group key.

### Group voting
- Each group has its **own pool** (up to 3 ranked nominations per member, separate from the main pool and other groups), its **own ballot** (each member's #1) and its own rolling rankings.
- It uses the **same monthly schedule as Phase 10**:
  - month M is voted on all through M-1 and closes on the last day of M-1 at 11:59 PM;
  - the film is watched during M;
  - discussion is a voice event in the **group's voice channel** on the 1st of M+1 at 8 PM CT.
- **Eligibility for a group's ballot:** any film with a `#movies` post that **this group** hasn't already watched. A film's main status (Backlog, Watched, …) doesn't matter. Films the main weekly or another group already watched are fine, and the same film can sit in several pools at once.
- **Watch history lives in the DB** (one row per group per monthly winner). It isn't read back from the planning channel, where messages can be edited or deleted and reading them back means paging through the whole history. The result posts in the planning channel are the visible log.
- **Privacy: nothing about a group shows outside its category.**
  - No `#watch-log` line, no `#announcements` post, no tag or status change and no pin on the `#movies` post.
  - `/schedule` outside the group doesn't mention it.
  - No group stats in Phase 11.
  - Nominating a film that has no post creates the normal `#movies` post, which looks like any other add and doesn't name the group.
  - Members are free to talk in the public movie threads; that's their choice.
- **`/admin pick|clear|discussion`** take a `group:` (`main` or any configured group key, with autocomplete). Main uses this/next week; groups use this/next month.

### Data
- An append-only migration adds `group_key` (`main` for existing rows) to the pool, ballot, rankings and months tables, and a group watch-history table.
- The same migration deletes the main December month and its (empty) rankings.

### Rollout
- **First OSU vote:** December 2026, opens on deploy, closes **Mon Nov 30, 11:59 PM CT**. Discussion Fri Jan 1 2027, 8 PM, in the OSU voice channel.
- After the owner's `setup_server.py --apply`: restart, then assign `@OSU` to the friends.

### To verify during the build (record in DISCORD_NOTES)
- That a VOICE event in a hidden channel is invisible to members without View Channel. The docs list View Channel as the "Get" permission for such events, which implies it is hidden, but they don't say the events list filters by it. This has to be checked with an account that has neither the role nor Administrator, since the owner sees everything.
- That two text channels may share a name (Discord allows it in practice; the docs say nothing about uniqueness).

### Decided (owner, 2026-11-15)
- **Channel names:** every group gets `#watch-planning`, `#chat` and `#voice` inside its own category (duplicate names are allowed; keys tell them apart). Only OSU for now.
- **`@Monthly Movie` is deleted** (with its How to button); a fresh `@OSU` role is created.

## Phase 12b: picks announced in #watch-planning, ping buttons on the ballots (built; see STATUS.md)

Owner (2026-11-15):
- **No pick posts in `#announcements`.** Each pick is announced in its own `#watch-planning`: the main one for the weekly movie, a group's for its monthly movie. That result post pings the matching role. Group picks already work this way. For the weekly, the `#announcements` post and the `#watch-planning` note become one post in `#watch-planning` that pings `@Weekly Movie`. Applies to the weekly close, a sole nomination winning, and `/admin pick`. `#announcements` stays for server news; its topic is updated to say so.
- **Ping buttons move out of `#welcome-and-rules`.** The Pings paragraph and the role buttons come off 📖 How to.
- **Opt-in group pings.** Each group gets a second role, `@<Name> Pings` (e.g. `@OSU Pings`, key `group_<key>_ping`, mentionable), created by `setup_server.py` from `groups:`. Group announcements ping it instead of the membership role, so members choose whether they're pinged. The button only works for members of the group.
- **No pinned instructions message** (owner: the channels are fine as they are).

Where the buttons go (owner confirmed 2026-11-15): a **🔔 Pings** toggle button on each open ballot, next to 🗳️ Vote and 📋 My nominations. It toggles `@Weekly Movie` on the weekly ballot and `@<Name> Pings` on a group's. A ballot is always open, and each new one gets the button. The reply is private: "🔔 Added / 🔕 Removed".

## Phase 11: stats page, private member views, Film Buffs Wrapped (defined, not planned)

Owner (2026-11-13): each member's own stats stay **private**; **group-wide totals are public**. No website: everything happens inside Discord (a web page would need Discord login, a domain with HTTPS and a public port, which is more to run and secure than a private server needs). Details still to be worked out with the owner before a step-by-step plan is written.

- **New stats channel** (name TBD, e.g. `#stats` or `#engagement`), added through `server.yaml` + `setup_server.py`. Read-only for members, like `#welcome-and-rules`: the bot keeps a few messages up to date and edits them in place, so the channel reads like a page. Group totals only, never per-member numbers. Possible content: films logged this month/year, group watches, hours watched, top genres, most-discussed posts, weekly-pick turnout.
- **Private member view:** a `/me` command and/or a "📋 My stats" button on the stats page opens a reply only that member can see (ephemeral). It shows their own stats so far, and could also show their active nominations, saved ranking and linked Letterboxd account (see Phase 10). Ephemeral replies disappear when dismissed or after a reload, so members reopen them; all data lives in SQLite.
- **Film Buffs Wrapped** (end of year, like Spotify Wrapped): a "🎁 Your <year> Wrapped" button on the stats page opens a private slideshow with ◀ ▶ buttons, one stat per slide. A **"Share"** button posts the member's Wrapped publicly only if they click it. Slides are optional image cards (would add an image library such as Pillow). Slide ideas from the first talk:
  - films logged, rewatches, top genres, hours watched
  - average rating vs. the group's
  - "taste twin" (the member whose ratings match most closely); owner liked it, but it names another member, so it needs a closer look
  - most contrarian rating
  - nominations made and won; how often their 1st choice won; group-watch turnout
  - a group Wrapped post in the stats channel at the same time
- **Data gaps to close early** (so a full year is there by December):
  - **Runtime** isn't stored. Fetch it from TMDB (the Phase 9 daily call can include it).
  - **Chat activity** isn't counted. If "most active in discussions" is wanted, start counting messages per member per post (author and time only; no message content needed), and backfill from post history.
- Everything else (Letterboxd entries with ratings, nominations, ranked votes, weeks) is already in SQLite.
- Rating stats must keep guiding decision 3 (ratings are spoilers): fine in a member's private view, and as group-wide averages, but never film-by-film ratings on the public page without thinking about spoilers.
- Probably two pieces: the stats page + private view first, Wrapped in December.

## Later ideas
- Off-machine backup copies (needs a destination from the owner).

## Open questions for the owner
Answered 2026-11-11: no `.ics` feed; Mark as spoiler moves the message; a solo Letterboxd log keeps an existing post's tags; channels renamed to pre-/post-watch discussion and the Post-watch tag retired.

Answered 2026-11-11: In Theaters / Streaming are set automatically (Phase 9); backups keep the newest 3.

Answered 2026-11-11 (Phase 8): entries deleted on Letterboxd have their post-watch embed deleted; `/watchlist import` is removed in favour of `/letterboxd sync`.

Answered 2026-11-12 (Phase 8): `/letterboxd` becomes a group (`link`/`sync`/`unlink`); a deleted entry's `#movies` note and feed line are deleted too.

Settled 2026-11-12: the Phase 4 watch → review question (the Phase 8 poller guard covers both cases).

Answered 2026-11-13: member stats stay private (ephemeral views), group totals go in a new read-only, bot-maintained stats channel; Wrapped happens inside Discord, not on a website; "favorites" isn't a separate feature, it becomes several rolling nominations per member (Phase 10).

Answered 2026-11-13: the voting phase comes first (renumbered to Phase 10; stats is Phase 11).

Answered 2026-11-13 (Phase 10): rankings roll over; no late cutoff; monthly vote closes on the last day of the prior month; discussion on the 1st at 8 PM in Watch Party; first monthly movie December; monthly leader rule confirmed; ping roles with buttons on the How to page.

Answered 2026-11-15 (Phase 12): monthly moves into private, role-gated groups (OSU first); main is weekly-only; group repeats allowed except a film that group already watched; no group stats; no `#watch-log` for groups; `/admin` takes a group.

Answered 2026-11-15 (Phase 12): group channels are `#watch-planning` / `#chat` / `#voice`; `@Monthly Movie` is deleted and `@OSU` created fresh.

Answered 2026-11-15 (Phase 12b): no pinned instructions; picks announced in each `#watch-planning`, not `#announcements`; opt-in group ping roles; 🔔 Pings button on each ballot.

Open: Phase 11 needs more brainstorming before a plan is written (see its points). Phase 12 leftover: whether to remind a group a few days before its discussion.
