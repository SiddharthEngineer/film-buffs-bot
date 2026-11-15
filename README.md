# Film Buffs bot

A custom Discord bot plus an infrastructure-as-code setup script for **Film Buffs**, a private Discord server where a group of friends picks, watches and talks about movies.

- **Movies** live as posts in the `#movies` forum, one per film, created by the bot from TMDB data.
- **Letterboxd** is where people log what they watch. The bot pulls those logs in and keeps everything in SQLite, so we can build engagement stats later.
- **Spoilers** are kept out of `#movies` entirely; each film gets a companion post in `#post-watch-discussion` (created the first time someone opens it).
- **Weekly movie**: members keep up to 3 ranked nominations (`/nominate`); each member's highest one that's still available is on the ballot in `#watch-planning`. The 🎬 weekly vote is always open and closes Sunday night (ranked choice, instant runoff; rankings carry over week to week), then the watch week with a server event. Picks are announced in `#watch-planning`; opt-in `@Weekly Movie` pings with the 🔔 Pings button on the ballot. Reactions on posts don't count as votes.
- **Private groups** (`server.yaml` `groups:`): a role-gated category per group of friends (`#watch-planning`, `#chat`, `#voice`) with its own nominations and its own 🌙 monthly vote: it closes on the last day of the month before, the film is watched during its month and discussed in the group's `#voice` on the 1st. Its picks are announced in its `#watch-planning`, pinging an opt-in `@<Group> Pings` role (🔔 Pings on its ballot). Nothing about a group shows outside its category; a group can pick any film it hasn't picked before.

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
  weekly.py           weekly pick: ballot, ranked vote (instant runoff), watch week
  vote_ui.py          the Vote button and the private ranking picker
  tags.py             which forum tags a post carries (5-tag limit)
  spoilers.py         post-watch discussions (companion posts, Mark as spoiler)
  letterboxd.py       Letterboxd RSS: linking, polling, posting entries
  backfill.py         Letterboxd export parsing + TMDB title matching
  sync.py             full sync from a Letterboxd export (/letterboxd sync, backfill.py)
  backup.py           nightly SQLite backups, newest 3 kept
  botlogs.py          posts errors to #bot-logs (throttled)
  cogs/               slash commands, listeners, background loops
backfill.py           shell version of /letterboxd sync for the owner (dry run by default)
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

Discord application settings: **Message Content** intent on, **Server Members** and **Presence** off, Public Bot off. Invite with scopes `bot applications.commands` and permissions integer `2832634816326736` (View Channels, Manage Channels, Manage Roles, Manage Threads, Send Messages, Send Messages in Threads, Manage Messages, Pin Messages, Manage Webhooks, Embed Links, Add Reactions, Read Message History, Send Polls, Create Events). Pin Messages (added in Phase 5; used for the forum pins) is a separate permission from Manage Messages now. Manage Roles is only there because Discord requires it to set channel permission overwrites.

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

## Commands

| Command | Where | What it does |
|---|---|---|
| `/addmovie title:<pick from list> [pitch]` | any channel | Creates the movie's post in `#movies` (or links the existing one) without nominating it. Free text is rejected; pick from the autocomplete. |
| `/nominate [title] [rank] [pitch]` | any channel (no title: inside a movie post) | Does what `/addmovie` does (creates the post if there's none, with the pitch), then adds the film to your nominations (up to 3, ranked 1–3). No rank: goes last. A rank inserts it there; with 3 already, it replaces that one. Nominating one of yours again with a rank moves it. Your highest eligible one is on the ballot. In a private group's channels, this uses that group's nominations instead (`title:` required). |
| `/unnominate film:` | any channel | Withdraws one of your nominations (autocomplete lists yours). |
| `/nominations` (or **📋 My nominations** on a ballot) | any channel | Your nominations, privately, with ⬆️ move-up and 🗑️ withdraw buttons. |
| **🗳️ Vote** on the 🎬 or a group's 🌙 ballot | `#watch-planning` (main or a group's) | Any time: rank as many films as you like, best first, in a private picker (Undo last / Clear). Each vote's rankings are separate and carry over to its next vote; films that leave the ballot drop out and later ranks move up. 🆕 marks films that joined since you last ranked. |
| **🔔 Pings** on a ballot | `#watch-planning` (main or a group's) | Toggles `@Weekly Movie` on the weekly ballot, or the group's `@<Group> Pings` on a group's ballot (members only). Group membership roles are assigned by an admin in Discord. |
| `/schedule` | any channel | This and next week's movie or vote and your nominations. In a group's channels: that group's this and next month's movie or vote (with the discussion time) and your nominations there. |
| `/watchlist show` · `clear` | any channel | What the bot has from your last export upload; clear removes it. |
| `/spoiler` | inside a movie post | Links the movie's post-watch discussion (creates it if needed); inside a post-watch post, links back. |
| Apps → **Mark as spoiler** | any message in a `#movies` post | Moves the message into the movie's post-watch discussion as its author, then deletes the original. |
| `/letterboxd link [username]` · `unlink` | any channel | Links your Letterboxd account (no username: shows which one is linked and your last sync). `unlink` stops posting. |
| `/letterboxd sync file:` | any channel | Upload your Letterboxd export zip. Shows a preview (new / changed / removed entries, posts to create), then **Apply** or **Cancel**. Stores your history, edits the bot's embeds for entries you edited, deletes the bot's messages for entries you deleted, replaces your watchlist (watchlist films only break ties in the votes). |

## Admin cheat sheet

`/admin` commands are only shown to members with **Manage Server** (Administrators, so the Moderator role, always count). The bot also accepts the role named in `server.yaml` → `ops.admin_role`. Replies are ephemeral.

| Command | What it does |
|---|---|
| `/admin resync [post]` | Re-renders a movie's info card (button, no-spoilers reminder) and re-applies its tags from the database; also refreshes its post-watch post's title and starter. Run it inside the `#movies` or post-watch post, or pick the post. Use it after editing a movie row by hand, or if a card or its tags look wrong. |
| `/admin availability` | Re-checks In Theaters / Streaming for every movie against TMDB now (it also runs daily at 05:00 Chicago) and replies with what changed. Takes a few minutes. |
| `/admin pick group:<main or a group> slot:<This/Next> title:` | Sets that week's (main) or month's (group) movie. Closes an open vote early (its rankings are kept), updates the event and announces it. Main: puts a previously picked film back to its old tag, and pins it right away if the week has started. Groups never change tags. |
| `/admin clear group: slot:<This/Next>` | Undoes that week's or month's pick: no movie then, the event is removed. Main: the film goes back to its old tag and its pins are removed, and the week shows "no pick (cleared by an admin)". A group can nominate a cleared film again. `/admin pick` can still set a film later. |
| `/admin discussion group: month:<This/Next month's movie> date:YYYY-MM-DD [time:HH:MM]` | Moves a group's discussion event (Central time, default 20:00). Editing the event in Discord works too; the bot never resets its time. |
| `/admin letterboxd-unlink member:<member>` | Unlinks someone's Letterboxd account (e.g. a wrong link or they asked). Their stored entries stay. |

**#bot-logs** (admins only) gets:
- 🟢 a startup notice: version, git commit, schema version, last backup.
- 🔴 every ERROR from the bot, with the end of the traceback. The same error is posted at most once an hour (later posts say how many repeats were held back), and at most 5 messages go out per 10 minutes. Full tracebacks are always in `journalctl -u film-buffs-bot`.
- Letterboxd: a single error when a feed fails 4 polls in a row (about an hour) or Letterboxd refuses 4 rounds in a row, and a 🟡 notice when it works again. One-off failures only go to the journal. An entry that fails to post 5 times is given up on and reported.

**Backups:** every night at 04:00 America/Chicago (`server.yaml` → `ops`), the bot snapshots `data/filmbuffs.db` into `backups/filmbuffs-<UTC time>.db` with SQLite's online backup API, checks it, and keeps only the newest 3 (`ops.backup_keep`). If the newest backup is over a day old when the bot starts, it makes one right away. Each backup is a complete, ordinary SQLite file.

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

**Weekly pick:** timings live in `server.yaml` → `weekly` (restart the bot after changing them). Each week's state is in the `weeks` table:

```bash
sqlite3 data/filmbuffs.db "SELECT week_start, state, winner_tmdb_id, skip_reason FROM weeks"
```

If a step fails (e.g. Discord was down), it's retried every 30 seconds and the error goes to `#bot-logs`. Deleting a ballot message in `#watch-planning` makes the bot post a new one.

**Other handy checks:**

```bash
journalctl -u film-buffs-bot --since today        # today's logs
journalctl -u film-buffs-bot -p err               # errors only
uv run setup_server.py                            # should say "No changes"
```
