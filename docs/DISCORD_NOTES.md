# Verified platform behavior

Things checked against current docs or live tests while building. Re-check anything marked "re-verify" before relying on it.

## Discord
- **Docs moved** to `https://docs.discord.com/developers/...` (old `discord.com/developers/docs` URLs 301 there).
- **Forum posts:** creating one needs Send Messages on the forum ("Create Posts"); Create Public Threads is ignored. Works in a non-Community server (live-tested).
- **Forum tags:** max 20 per forum (`available_tags`), names ≤ 20 chars, **max 5 applied per post**. `moderated` tags can only be added/removed by members with Manage Threads.
- **Editing an archived thread** (tags, messages) fails unless it's un-archived in the same edit; `MovieService.retag` passes `archived=False`.
- **Channel permission overwrites** need Manage Roles, and a bot can only allow/deny permissions it has itself.
- **Scheduled events:** Create Events is enough to create and edit/delete the bot's own events (Manage Events only for others'). EXTERNAL events need `entity_metadata.location` (1–100 chars), `scheduled_end_time`, and `channel_id` null.
- **Webhooks:** can post into a forum thread with `thread_id` (auto-unarchives). They can't send real replies (no `message_reference`). Allowed flags: SUPPRESS_EMBEDS, SUPPRESS_NOTIFICATIONS. Webhook names can't contain "discord" or "clyde".
- **Bot limits that shaped the spoiler redesign:** a bot cannot insert a message mid-thread, control where a client opens a thread (return visits jump to newest/unread), hide or edit other users' messages, or reveal multiple spoiler tags with one click.
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
- `https://letterboxd.com/tmdb/<id>` redirects to the film page in a real browser (confirmed by the owner).
