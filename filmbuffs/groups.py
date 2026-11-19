"""Private groups (Phase 12): role-gated friend groups, each with its own monthly movie.

A group is one entry under `groups:` in server.yaml. `expand()` turns each
entry into the layout setup_server.py manages: a membership role, an opt-in
ping role (`<role> Pings`, given with the group role since Phase 19 and toggled
with the 🔔 Pings button on the group's board; group announcements in #chat ping
it), a category, and #watch-planning (commands only), #chat and #voice channels that
only the role and the bot can see. Discord always shows them to the server
owner and to Administrator roles; that can't be turned off.

Everything a group does stays in its category: its own nomination pool, ballot,
rankings and monthly vote. Nothing about it appears in the public channels, and
its picks never change a film's tags or status in #pre-watch.

This module is pure (no discord.py) so setup_server.py and the bot share it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time

MAIN = "main"                  # the main server's weekly vote
KEY_RE = re.compile(r"^[a-z][a-z0-9]{0,19}$")
RESERVED = {MAIN, "up", "del", "show", "weekly"}     # would clash with button IDs
PARTS = ("watch_planning", "chat", "voice")

BOT_TEXT = ["view_channel", "send_messages", "embed_links", "read_message_history"]
BOT_VOICE = ["view_channel", "connect"]


class GroupConfigError(Exception):
    pass


def role_key(key: str) -> str:
    return f"group_{key}"


def ping_role_key(key: str) -> str:
    return f"group_{key}_ping"


def category_key(key: str) -> str:
    return f"group_{key}"


def channel_key(key: str, part: str) -> str:
    return f"{key}_{part}"


def track(key: str) -> str:
    """The rankings track for a group's vote (track_rankings.track)."""
    return f"group:{key}"


def expand(cfg: dict) -> None:
    """Add each group's role, category and channels to a loaded server.yaml (in place).
    Group categories go above the Admin category (or last if there is none)."""
    groups = cfg.get("groups") or []
    seen: set[str] = set()
    roles = cfg.setdefault("roles", [])
    cats = cfg["categories"]
    at = next((i for i, c in enumerate(cats) if c.get("key") == "admin"), len(cats))
    for g in groups:
        key = g.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key) or key in RESERVED:
            raise GroupConfigError(f"group key {key!r}: use 1-20 lowercase letters/digits, "
                                   f"starting with a letter (not {', '.join(sorted(RESERVED))})")
        if key in seen:
            raise GroupConfigError(f"duplicate group key {key!r}")
        seen.add(key)
        if not g.get("name"):
            raise GroupConfigError(f"group {key}: needs a name")
        role_name = g.get("role") or g["name"]
        roles.append({"key": role_key(key), "name": role_name, "mentionable": True,
                      "_group": key})
        roles.append({"key": ping_role_key(key), "name": g.get("ping_role") or f"{role_name} Pings",
                      "mentionable": True, "_group": key})
        who = f"role:{role_key(key)}"
        hidden = {"@everyone": {"deny": ["view_channel"]}}
        cats.insert(at, {
            "key": category_key(key),
            "name": g.get("category") or g["name"],
            "_group": key,
            "channels": [
                {"key": channel_key(key, "watch_planning"), "name": "watch-planning",
                 "topic": (f"{g['name']}'s monthly movie. Voting here is completely independent "
                           f"from the server's weekly vote. Click My nomination on the 🌙 board to "
                           f"nominate a film (one each), then Vote and rank them. The vote closes on the last day of the month; "
                           f"the discussion is in the voice channel on the 1st. Commands only "
                           f"here: chat in #chat. Only {role_name} members can see this category."),
                 # Phase 19: commands only (the text box stays; members just can't post).
                 "overwrites": {**hidden, who: {"allow": ["view_channel"],
                                                "deny": ["send_messages", "send_messages_in_threads",
                                                         "create_public_threads",
                                                         "create_private_threads"]},
                                "@bot": {"allow": BOT_TEXT}}},
                {"key": channel_key(key, "chat"), "name": "chat",
                 "topic": f"Chat for {g['name']}.",
                 "overwrites": {**hidden, who: {"allow": ["view_channel"]},
                                "@bot": {"allow": BOT_TEXT}}},
                {"key": channel_key(key, "voice"), "name": "voice", "type": "voice",
                 "overwrites": {**hidden, who: {"allow": ["view_channel", "connect"]},
                                "@bot": {"allow": BOT_VOICE}}},
            ],
        })
        at += 1


@dataclass
class Group:
    key: str
    name: str
    role_name: str
    first_month: date | None = None
    discussion_at: time = time(20, 0)
    discussion_minutes: int = 120
    role_id: int | None = None
    ping_role_id: int | None = None
    category_id: int | None = None
    channels: dict[str, int] = field(default_factory=dict)   # part -> channel id

    @property
    def track(self) -> str:
        return track(self.key)


def parse(cfg: dict) -> list[Group]:
    """Group settings from a raw server.yaml dict (IDs are filled in by Config)."""
    out = []
    for g in cfg.get("groups") or []:
        fm = g.get("first_month")
        if isinstance(fm, date):
            fm = fm.replace(day=1)
        elif fm:
            fm = date.fromisoformat(f"{fm}-01")
        hh, mm = str(g.get("discussion_time", "20:00")).split(":")
        out.append(Group(key=g["key"], name=g["name"], role_name=g.get("role") or g["name"],
                         first_month=fm or None, discussion_at=time(int(hh), int(mm)),
                         discussion_minutes=int(g.get("discussion_minutes", 120))))
    return out
