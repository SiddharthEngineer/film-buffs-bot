# Working on the Film Buffs bot

Read `docs/STATUS.md` (what's live) and `docs/ROADMAP.md` (what's next, agreed decisions, open questions) before starting. `docs/DISCORD_NOTES.md` lists platform limits already verified.

## How the owner wants work done
- **One phase at a time.** At the end of each phase: stop, summarize, say exactly what (if anything) the owner must do by hand, and wait for a go-ahead.
- **Ask before** anything destructive (deleting channels/messages/posts the owner made) or anything that exposes a port publicly.
- If a requirement turns out impossible or awkward in Discord's API, **stop and propose the closest alternative**; don't silently change the design.
- Before relying on a Discord limit (poll duration/answers, tag counts, modal/autocomplete behavior, webhooks in threads), check the current docs at `https://docs.discord.com/developers/...` and record it in `docs/DISCORD_NOTES.md`.
- The owner tests UI in Discord by hand. You can live-test bot code paths on a clearly labelled throwaway post and delete it afterwards.
- Keep `docs/STATUS.md` and `docs/ROADMAP.md` current at the end of each phase.

## Conventions
- Python 3.12+, discord.py 2.7, aiohttp, aiosqlite, PyYAML, feedparser. Venv in `.venv/`.
- Secrets only in `.env` (chmod 600, gitignored). Never commit `data/` (SQLite: setup state + bot data).
- Store UTC in the DB; display America/Chicago (`config.tz`).
- Schema changes go in `filmbuffs/db.py` `MIGRATIONS` (append-only).
- Server layout changes go through `server.yaml` + `setup_server.py` (plan, then apply), never by hand in code.
- Movie post tags are always recomputed from the DB row via `MovieService.retag` (5-tag limit; status/flags beat genres).
- External HTTP uses the honest User-Agent `FilmBuffsBot/<ver> (private Discord bot)`. Never spoof a browser.
- Respect Discord rate limits (discord.py handles 429s; throttle bulk post creation explicitly).
- After code changes: `.venv/bin/python -m pytest -q tests`, then `sudo systemctl restart film-buffs-bot` and check `journalctl -u film-buffs-bot`.
- Commits: author is the owner (Siddharth); end messages with the Claude co-author line.
