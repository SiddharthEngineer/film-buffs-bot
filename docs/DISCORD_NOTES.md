# Verified platform behavior

Things checked against current docs or live tests while building. Re-check anything marked "re-verify" before relying on it.

## Discord
- **Docs moved** to `https://docs.discord.com/developers/...` (old `discord.com/developers/docs` URLs 301 there).
- **Forum posts:** creating one needs Send Messages on the forum ("Create Posts"); Create Public Threads is ignored. Works in a non-Community server (live-tested).
- **Forum tags:** max 20 per forum (`available_tags`), names ≤ 20 chars, **max 5 applied per post**. `moderated` tags can only be added/removed by members with Manage Threads.
- **Sending into an archived thread** (checked 2026-11-11, threads topic): a message un-archives it automatically unless it's locked; needs Send Messages in Threads. Locked threads need Manage Threads (the bot has it on both forums). Forum posts behave like text-channel threads.
- **Editing an archived thread** (tags, messages) fails unless it's un-archived in the same edit; `MovieService.retag` passes `archived=False`.
- **Pinning messages** needs the separate **Pin Messages** permission (`1 << 51`); Manage Messages alone gets 403 50013 (seen live 2026-11-11). Pinning a forum *post* (thread flag) only needs Manage Threads.
- **Channel permission overwrites** need Manage Roles, and a bot can only allow/deny permissions it has itself.
- **Scheduled events:** Create Events is enough to create and edit/delete the bot's own events (Manage Events only for others'). EXTERNAL events need `entity_metadata.location` (1–100 chars), `scheduled_end_time`, and `channel_id` null.
- **Webhooks:** can post into a forum thread with `thread_id` (auto-unarchives), but **only threads of the webhook's own channel**, so moving into `#movie-spoilers` needs a webhook on that forum. They can't send real replies (no `message_reference`). Allowed flags: SUPPRESS_EMBEDS, SUPPRESS_NOTIFICATIONS. Webhook names can't contain "discord" or "clyde".
- **Bot limits that shaped the spoiler redesign:** a bot cannot insert a message mid-thread, control where a client opens a thread (return visits jump to newest/unread), hide or edit other users' messages, or reveal multiple spoiler tags with one click.
- **Buttons** (checked 2026-11-11): custom_id 1–100 chars; link buttons (style 5) need a `url` (≤ 512 chars), have no custom_id and send no interaction; ≤ 5 buttons per action row; label ≤ 80 chars. A bot can edit components on its own messages (incl. a forum starter message).
- **File uploads** (checked 2026-11-11): 20 MiB per file by default (more with boosts), ~25 MiB per request, 10 files per message. discord.py 2.7's `Guild.filesize_limit` still reports 10 MiB for unboosted servers; the bot uses the larger of the two.
- **Message content in interactions:** messages delivered with a message context-menu command include their content even without the Message Content intent, so the bot no longer requests that intent.
- **Message content** (checked 2026-11-11, Create Message): up to 2000 characters. `#bot-logs` messages are cut to fit.
- **`default_member_permissions`** (checked 2026-11-11, application commands): members without the permission don't see the command in the picker; "Members with the Administrator permission can use all commands." Server admins can change per-command access in Server Settings → Integrations → Manage. `/admin` uses Manage Server and the bot re-checks at runtime.
- **Subcommands** (checked 2026-11-12, application commands): "Using subcommands or subcommand groups will make your base command unusable." So `/letterboxd username:` couldn't stay next to `/letterboxd sync`; `/letterboxd` became a group (`link`, `sync`, `unlink`). Nesting: one level of groups only.
- **Interaction tokens** (checked 2026-11-12, receiving and responding): the first response must come within 3 s; the token then works for 15 minutes (followups, editing the original response). A button click's UPDATE_MESSAGE (type 7) edits the message the button is on. The docs don't say outright that `@original` then edits that message through the click's token; discord.py documents it does, and `/letterboxd sync` relies on it (to be confirmed in the owner's live test). Long syncs stop editing after 14 min and send the final note by DM instead.
- **Select menus** (checked 2026-11-12, components reference): string select ≤ 25 options, option label/value/description ≤ 100 chars, placeholder ≤ 150, `max_values` ≤ 25. One select per action row; legacy messages allow 5 rows, so at most 5 selects (Components V2 allows 40 components in total). The docs don't say whether multi-select `values` come in click order, so ranking can't rely on it; the ranked-vote picker takes one pick per click instead. Modals hold 1–5 components and can contain string selects (inside a Label).
- **Autocomplete** must answer within 3 s; max 25 choices; choice name/value ≤ 100 chars.
- **Polls** (checked 2026-11-11, poll resource): max 10 answers, answer text ≤ 55 chars, question ≤ 300, duration in hours up to 32 days (default 24). **A poll message can't be edited after it's sent**, so options can't change; that's why the ballot is frozen before the poll opens. Expire Poll ends it immediately, but only for your own polls. After ending, a background job does a final tally; `is_finalized` turns true when it's done (no stated delay), so the bot waits briefly before reading counts. Voters per answer: `GET .../polls/{message}/answers/{id}`, 100 per page. Apps can't vote.
- **Forum sort orders** (checked 2026-11-11): only latest activity (0) and creation date (1). **No sort by reactions**, hence the pinned leaderboard post and `/top`.
- **Pinned forum posts:** the `PINNED` thread flag works in forum/media channels (needs Manage Threads). A pinned thread doesn't auto-archive; archiving it clears the pin. **Only one post per forum can be pinned**: pinning another silently unpins the first (live-tested 2026-11-11 on a throwaway post; not in the docs). That's why the leaderboard is a pinned *message* in `#watch-planning` (text channels allow many pinned messages) and the forum pin goes to the week's movie.
- **Scheduled events** (checked 2026-11-11): EXTERNAL events go ACTIVE at their start time and COMPLETED at their end time automatically. Name 1–100 chars, description ≤ 1000, location ≤ 100. Max 100 SCHEDULED/ACTIVE events per server.
- **Reactions** are in the non-privileged `GUILD_MESSAGE_REACTIONS` intent (in `Intents.default()`): add, remove, remove-all, remove-emoji events. A bot can only add **its own** reaction; it can't react on a member's behalf.
- Text channel topic ≤ 1024 chars; forum guidelines ≤ 4096.
- Message Content intent is needed for anything reading message text in listeners (bots in < 100 servers just toggle it on).

## TMDB
- v3 API key (`api_key` param) or v4 read token (`Authorization: Bearer`) both work for `/3` endpoints.
- `/movie/{id}?append_to_response=credits` gives runtime, genres and crew (directors) in one call.
- Search + parallel detail calls for 10 results: ~0.3–0.5 s from the VPS.

## Letterboxd
- **RSS works for bots** with an honest User-Agent: `https://letterboxd.com/<user>/rss/` returns 200 (`application/rss+xml`, behind Cloudflare).
- RSS items include `<tmdb:movieId>`, plus `letterboxd:filmTitle` etc.
- **Regular web pages return 403 to non-browser clients** (including `/tmdb/<id>`). Don't try to get around it (no browser User-Agent spoofing); Claude Code's auto mode blocks it as bot-check circumvention, and it isn't polite.
- **Feed format** (checked 2026-11-11 on two public feeds): ~50 newest items; GUIDs `letterboxd-watch-<n>`, `letterboxd-review-<n>` and `letterboxd-list-<n>` (lists have no film fields; skip them). Diary items carry `letterboxd:watchedDate`, `rewatch` (Yes/No), `filmTitle`, `filmYear`, `memberRating` (absent when unrated), `memberLike` (Yes/No) and `tmdb:movieId`. Review text is HTML in `<description>` after a poster `<img>`; spoiler reviews start with "This review may contain spoilers."
- **No caching headers:** the feed sends `cache-control: no-store` and no ETag/Last-Modified, so conditional GETs don't help (the bot still sends them if they appear). Unknown users get 404.
- `https://letterboxd.com/tmdb/<id>` redirects to the film page in a real browser (confirmed by the owner).
- **List and watchlist feeds are blocked** (checked 2026-11-11): `/<user>/list/<slug>/rss/`, `/<user>/lists/rss/` and `/<user>/watchlist/rss/` return Cloudflare's 403 challenge to the bot's honest User-Agent, while `/<user>/rss/` still works. A new list does show up in the main feed as a `letterboxd-list-<n>` item, but with only the first few films, no TMDB IDs, and only once. So watchlists come from the member's data export (`/letterboxd sync` since Phase 8; was `/watchlist import`).
- **Official API:** private beta, access by application (letterboxd.com/api-beta). Its pages also block automated readers, so its terms weren't checked from here.
