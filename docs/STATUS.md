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
- `#movies` tags (19 of 20): status tags, all moderated (only the bot applies them): Backlog, Up Next, Watched, Watched (solo), Spoilers, In Theaters, Streaming. Genre tags: Action, Adventure, Animation, Comedy, Crime, Documentary, Drama, Fantasy, Horror, Romance, Sci-Fi, Thriller (mapping from TMDB genres is in `server.yaml`).
- Forum default sort: latest activity. Guidelines text explains `/addmovie` and 👍 voting. **The guidelines and the welcome message still describe the old spoiler-line design** and need updating with Phase 3R.

### Phase 2: `/addmovie` ✅ (user-tested)
- TMDB autocomplete shows `Title (Year) — Director`; choice values are `tmdb:<id>` so a typed numeric title like "1917" is never read as an ID. Pasted TMDB movie URLs are also accepted. Anything else is rejected ephemerally and no post is created.
- Duplicate → ephemeral link to the existing post. Posts deleted by hand are forgotten, and the next `/addmovie` recreates them.
- Post: title `Title (Year)`, embed with poster thumbnail, year, runtime, director(s), genres, synopsis, TMDB + Letterboxd links (`https://letterboxd.com/tmdb/<id>`, confirmed working in a browser), added by, pitch. Tags: Backlog + up to `max_genre_tags` (3) genre tags. The bot adds 👍 (the backlog vote).
- Autocomplete answers in ~0.3–0.5 s (Discord's limit is 3 s).

### Ops already in place
- systemd unit `film-buffs-bot` (enabled, `Restart=on-failure`, runs as `dev`, `ProtectSystem=strict` with write access only to `data/` and `backups/`).
- Commands are synced to the guild only (instant updates).
- 19 unit tests passing (`tests/`).

## Built but being replaced

### Phase 3 (old design): in-post spoiler line ⚠️ superseded
Code: `filmbuffs/spoilers.py`, `filmbuffs/cogs/spoilers.py`. **It is still live.**

What it does: `||markup||`, `/spoiler` or "Mark as spoiler" sets a single spoiler line per post (earliest point wins), posts a divider, adds a "spoiler-free ends here" field to the info card, and adds the Spoilers tag. "Mark as spoiler" deletes the original message and **reposts it at the bottom of the thread** via webhook.

Why it's being replaced: reposting reorders the conversation, and Discord can't insert a divider mid-thread, control where a reader lands when opening a post, or hide other people's messages in place. So an in-post line can't keep returning readers safe. See ROADMAP Phase 3R.

## Known state / cleanup for the next session
- **Disable the old spoiler behavior first** (the `filmbuffs.cogs.spoilers` extension) so nobody reorders another post before Phase 3R lands.
- DB row for Parasite (TMDB 496243) still has `flags=["spoilers"]`, a spoiler marker and divider ID (1569940896399630407) from testing, and its info card has a "Spoiler line" field. The owner deleted the test messages in that post. Phase 3R's migration should clear the flag/marker, remove the field and retag the post.
- The webhook "Film Buffs reposts" exists on `#movies` (created by the old design); Phase 3R can reuse it or create one on `#movie-spoilers`.
- `data/filmbuffs.db` is not backed up yet (nightly backup is Phase 7). It holds the setup-script state (key → Discord ID) as well as movies; losing it means `setup_server.py` would no longer recognize the channels it manages.
