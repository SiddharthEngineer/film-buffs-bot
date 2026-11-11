# Film Buffs bot

A custom Discord bot plus an infrastructure-as-code setup script for **Film Buffs**, a private Discord server where a group of friends picks, watches and talks about movies.

- **Movies** live as posts in a `#movies` forum, one per film, created by the bot from TMDB data.
- **Letterboxd** is where people log what they watch. The bot pulls those logs in and keeps everything in SQLite, so we can build engagement stats later.
- **Spoilers** are kept out of `#movies` entirely; each film can get a companion post in `#movie-spoilers` (being rebuilt, see the roadmap).
- **Weekly pick**: a Monday poll picks next week's movie from the most-👍'd backlog posts (planned).

| Doc | What's in it |
|---|---|
| [docs/STATUS.md](docs/STATUS.md) | What is built and live right now, known issues |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Remaining phases, agreed design decisions, open questions |
| [docs/DISCORD_NOTES.md](docs/DISCORD_NOTES.md) | Discord / TMDB / Letterboxd behavior we verified, and the limits that shaped the design |
| [CLAUDE.md](CLAUDE.md) | Working conventions for coding sessions on this repo |

## Stack

Python 3.12+ (the VPS runs 3.14), `discord.py` 2.7, `aiohttp`, `aiosqlite`, `feedparser`, `PyYAML`. Runs on the VPS as the systemd service `film-buffs-bot`, logging to journald. All stored times are UTC; display times are America/Chicago.

## Layout

```
filmbuffs/            the bot package (python -m filmbuffs)
  bot.py              bot class, startup, command sync (guild-only)
  config.py           .env + server.yaml + IDs from the state DB
  db.py               SQLite schema + migrations (data/filmbuffs.db)
  tmdb.py             TMDB client: search, details, autocomplete
  movies.py           movie posts in #movies (create, embed, tags)
  tags.py             which forum tags a post carries (5-tag limit)
  spoilers.py         spoiler system (current design is being replaced)
  cogs/               slash commands and listeners
setup_server.py       reconciles the Discord server to server.yaml
filmbuffs/state.py    which Discord objects setup_server.py manages
server.yaml           declared server layout (channels, perms, tags, messages)
scripts/              one-off tools (check_connection.py)
deploy/               systemd unit
tests/                pytest
```

## First-time setup

```bash
git clone git@github.com:SiddharthEngineer/film-buffs-bot.git ~/film-buffs-bot
cd ~/film-buffs-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env && chmod 600 .env   # then fill in the three values
```

`.env` needs `DISCORD_TOKEN`, `DISCORD_GUILD_ID` and `TMDB_API_KEY` (a TMDB v3 key or v4 read-access token both work).

Discord application settings: **Message Content** intent on, **Server Members** and **Presence** off, Public Bot off. Invite with scopes `bot applications.commands` and permissions integer `580835002641488` (View Channels, Manage Channels, Manage Roles, Manage Threads, Send Messages, Send Messages in Threads, Manage Messages, Manage Webhooks, Embed Links, Add Reactions, Read Message History, Send Polls, Create Events). Manage Roles is only there because Discord requires it to set channel permission overwrites.

Check the connection (read-only):

```bash
.venv/bin/python scripts/check_connection.py
```

## Changing the server layout

Edit `server.yaml`, then:

```bash
.venv/bin/python setup_server.py           # plan: show the diff, change nothing
.venv/bin/python setup_server.py --apply   # apply (asks to confirm)
```

- Every object has a stable `key`; the script tracks key → Discord ID in `data/filmbuffs.db`, so renames in YAML rename in Discord rather than duplicating. Never change a key.
- Only declared permission targets are managed; other overwrites are reported and left alone.
- Nothing is deleted except channels listed by ID under `retire:` (name must match, text channels with messages are refused unless `allow_messages: true`, categories only when empty).
- After an apply, run the plan again: it should say "No changes".

## Running the bot

```bash
sudo systemctl restart film-buffs-bot      # after pulling code changes
journalctl -u film-buffs-bot -f            # live logs
.venv/bin/python -m pytest -q tests        # unit tests
```

The unit file is `deploy/film-buffs-bot.service`; install changes with
`sudo install -m 644 deploy/film-buffs-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload`.

## Commands (live today)

| Command | Where | What it does |
|---|---|---|
| `/addmovie title:<pick from list> [pitch]` | any channel | Creates the movie's post in `#movies` (or links the existing one). Free text is rejected; pick from the autocomplete. |
| `/spoiler` | inside a movie post | Old spoiler-line design; to be replaced (see ROADMAP) |
| Apps → **Mark as spoiler** | any message in a movie post | Old design (reposts the message at the bottom); to be replaced |
