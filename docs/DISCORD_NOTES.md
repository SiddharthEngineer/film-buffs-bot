# Verified platform behavior

Things checked against current docs or live tests while building. Re-check anything marked "re-verify" before relying on it.

## Discord
- **Docs moved** to `https://docs.discord.com/developers/...` (old `discord.com/developers/docs` URLs 301 there).
- **Forum posts:** creating one needs Send Messages on the forum ("Create Posts"); Create Public Threads is ignored. Works in a non-Community server (live-tested).
- **Forum tags:** max 20 per forum (`available_tags`), names ≤ 20 chars, **max 5 applied per post**. `moderated` tags can only be added/removed by members with Manage Threads.
- **Sending into an archived thread** (checked 2026-11-11, threads topic): a message un-archives it automatically unless it's locked; needs Send Messages in Threads. Locked threads need Manage Threads (the bot has it on both forums). Forum posts behave like text-channel threads.
- **Editing an archived thread** (tags, messages) fails unless it's un-archived in the same edit; `MovieService.retag` passes `archived=False`.
- **Channel permission overwrites** need Manage Roles, and a bot can only allow/deny permissions it has itself.
- **Scheduled events:** Create Events is enough to create and edit/delete the bot's own events (Manage Events only for others'). EXTERNAL events need `entity_metadata.location` (1–100 chars), `scheduled_end_time`, and `channel_id` null.
- **Webhooks:** can post into a forum thread with `thread_id` (auto-unarchives), but **only threads of the webhook's own channel**, so moving into `#movie-spoilers` needs a webhook on that forum. They can't send real replies (no `message_reference`). Allowed flags: SUPPRESS_EMBEDS, SUPPRESS_NOTIFICATIONS. Webhook names can't contain "discord" or "clyde".
- **Bot limits that shaped the spoiler redesign:** a bot cannot insert a message mid-thread, control where a client opens a thread (return visits jump to newest/unread), hide or edit other users' messages, or reveal multiple spoiler tags with one click.
- **Buttons** (checked 2026-11-11): custom_id 1–100 chars; link buttons (style 5) need a `url` (≤ 512 chars), have no custom_id and send no interaction; ≤ 5 buttons per action row; label ≤ 80 chars. A bot can edit components on its own messages (incl. a forum starter message).
- **File uploads** (checked 2026-11-11): 20 MiB per file by default (more with boosts), ~25 MiB per request, 10 files per message. discord.py 2.7's `Guild.filesize_limit` still reports 10 MiB for unboosted servers; the bot uses the larger of the two.
- **Message content in interactions:** messages delivered with a message context-menu command include their content even without the Message Content intent, so the bot no longer requests that intent.
- **Message content** (checked 2026-11-11, Create Message): up to 2000 characters. `#bot-logs` messages are cut to fit.
- **`default_member_permissions`** (checked 2026-11-11, application commands): members without the permission don't see the command in the picker; "Members with the Administrator permission can use all commands." Server admins can change per-command access in Server Settings → Integrations → Manage. `/admin` uses Manage Server and the bot re-checks at runtime.
- **Autocomplete** must answer within 3 s; max 25 choices; choice name/value ≤ 100 chars.
- **Poll answer limit** (10) and **max poll duration**: re-verify in Phase 5.
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
