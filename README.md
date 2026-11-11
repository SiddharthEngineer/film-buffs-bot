# Film Buffs bot

A custom Discord bot plus an infrastructure-as-code setup script for **Film Buffs**, a private Discord server where a group of friends picks, watches and talks about movies.

- **Movies** live as posts in the `#pre-watch-discussion` forum, one per film, created by the bot from TMDB data.
- **Letterboxd** is where people log what they watch. The bot pulls those logs in and keeps everything in SQLite, so we can build engagement stats later.
- **Spoilers** are kept out of `#pre-watch-discussion` entirely; each film gets a companion post in `#post-watch-discussion` (created the first time someone opens it).
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
  movies.py           movie posts in #pre-watch-discussion (create, embed, tags)
  tags.py             which forum tags a post carries (5-tag limit)
  spoilers.py         post-watch discussions (companion posts, Mark as spoiler)
  letterboxd.py       Letterboxd RSS: linking, polling, posting entries
  backfill.py         matching logic for backfill.py (Letterboxd export import)
  backup.py           nightly SQLite backups + 14-day retention
  botlogs.py          posts errors to #bot-logs (throttled)
  cogs/               slash commands, listeners, background loops
backfill.py           imports a Letterboxd export (dry run by default)
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
uv sync                                  # creates .venv from pyproject.toml + uv.lock
cp .env.example .env && chmod 600 .env   # then fill in the three values
```

`.env` needs `DISCORD_TOKEN`, `DISCORD_GUILD_ID` and `TMDB_API_KEY` (a TMDB v3 key or v4 read-access token both work).

Discord application settings: **Message Content** intent on, **Server Members** and **Presence** off, Public Bot off. Invite with scopes `bot applications.commands` and permissions integer `580835002641488` (View Channels, Manage Channels, Manage Roles, Manage Threads, Send Messages, Send Messages in Threads, Manage Messages, Manage Webhooks, Embed Links, Add Reactions, Read Message History, Send Polls, Create Events). Manage Roles is only there because Discord requires it to set channel permission overwrites.

Check the connection (read-only):

```bash
uv run scripts/check_connection.py
```

## Changing the server layout

Edit `server.yaml`, then:

```bash
uv run setup_server.py           # plan: show the diff, change nothing
uv run setup_server.py --apply   # apply (asks to confirm)
```

- Every object has a stable `key`; the script tracks key → Discord ID in `data/filmbuffs.db`, so renames in YAML rename in Discord rather than duplicating. Never change a key.
- Only declared permission targets are managed; other overwrites are reported and left alone.
- Nothing is deleted except channels listed by ID under `retire:` (name must match, text channels with messages are refused unless `allow_messages: true`, categories only when empty).
- After an apply, run the plan again: it should say "No changes".

## Running the bot

```bash
sudo systemctl restart film-buffs-bot      # after pulling code changes
journalctl -u film-buffs-bot -f            # live logs
uv run pytest -q                           # unit tests
```

Dependencies live in `pyproject.toml` (locked in `uv.lock`); add one with `uv add <pkg>`. The service runs `.venv/bin/python` directly, so it doesn't need uv at runtime; run `uv sync` and restart after dependency changes.

The unit file is `deploy/film-buffs-bot.service`; install changes with
`sudo install -m 644 deploy/film-buffs-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload`.

## Commands (live today)

| Command | Where | What it does |
|---|---|---|
| `/addmovie title:<pick from list> [pitch]` | any channel | Creates the movie's post in `#pre-watch-discussion` (or links the existing one). Free text is rejected; pick from the autocomplete. |
| `/spoiler` | inside a movie post | Links the movie's post-watch discussion (creates it if needed); inside a post-watch post, links back. |
| Apps → **Mark as spoiler** | any message in a pre-watch post | Moves the message into the movie's post-watch discussion as its author, then deletes the original. |
| `/letterboxd [username]` | any channel | Links your Letterboxd account, or shows which one is linked. `/letterboxd-unlink` stops posting. |

## Admin cheat sheet

`/admin` commands are only shown to members with **Manage Server** (Administrators, so the Moderator role, always count). The bot also accepts the role named in `server.yaml` → `ops.admin_role`. Replies are ephemeral.

| Command | What it does |
|---|---|
| `/admin resync [post]` | Re-renders a movie's info card (button, no-spoilers reminder) and re-applies its tags from the database; also refreshes its post-watch post's title and starter. Run it inside the pre- or post-watch post, or pick the post. Use it after editing a movie row by hand, or if a card or its tags look wrong. |
| `/admin letterboxd-unlink member:<member>` | Unlinks someone's Letterboxd account (e.g. a wrong link or they asked). Their stored entries stay. |

**#bot-logs** (admins only) gets:
- 🟢 a startup notice: version, git commit, schema version, last backup.
- 🔴 every ERROR from the bot, with the end of the traceback. The same error is posted at most once an hour (later posts say how many repeats were held back), and at most 5 messages go out per 10 minutes. Full tracebacks are always in `journalctl -u film-buffs-bot`.
- Letterboxd: a single error when a feed fails 4 polls in a row (about an hour) or Letterboxd refuses 4 rounds in a row, and a 🟡 notice when it works again. One-off failures only go to the journal. An entry that fails to post 5 times is given up on and reported.

**Backups:** every night at 04:00 America/Chicago (`server.yaml` → `ops`), the bot snapshots `data/filmbuffs.db` into `backups/filmbuffs-<UTC time>.db` with SQLite's online backup API, checks it, and deletes backups older than 14 days (the newest is always kept). If the newest backup is over a day old when the bot starts, it makes one right away. Each backup is a complete, ordinary SQLite file.

```bash
ls -lh backups/                                              # what's there
sqlite3 backups/<file>.db "SELECT COUNT(*) FROM movies"      # peek inside one
```

Restoring (this replaces the live database; the bot must be stopped):

```bash
sudo systemctl stop film-buffs-bot
mv data/filmbuffs.db data/filmbuffs.db.broken; rm -f data/filmbuffs.db-wal data/filmbuffs.db-shm
cp backups/<file>.db data/filmbuffs.db
sudo systemctl start film-buffs-bot
```

Backups live on the same disk as the bot. For off-machine copies, pull `backups/` from another computer now and then (e.g. `scp` or `rsync`).

**Other handy checks:**

```bash
journalctl -u film-buffs-bot --since today        # today's logs
journalctl -u film-buffs-bot -p err               # errors only
uv run setup_server.py                            # should say "No changes"
```
