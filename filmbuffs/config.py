"""Runtime config: secrets from .env, layout from server.yaml, IDs from the state DB."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from .state import DB_PATH, ROOT, State

# Status tags the bot sets. Exactly one of STATUS_TAGS is on a post at a time.
STATUS_TAGS = ("backlog", "up_next", "watched", "watched_solo")
FLAG_TAGS = ("in_theaters", "streaming")


@dataclass
class TagDef:
    key: str
    name: str
    id: int
    tmdb_genres: list[str] = field(default_factory=list)


class Config:
    def __init__(self) -> None:
        load_dotenv(ROOT / ".env")
        self.token = os.environ["DISCORD_TOKEN"]
        self.guild_id = int(os.environ["DISCORD_GUILD_ID"])
        self.tmdb_key = os.environ["TMDB_API_KEY"].strip()
        self.db_path = DB_PATH

        with open(ROOT / "server.yaml", encoding="utf-8") as f:
            self.layout = yaml.safe_load(f)
        self.tz = ZoneInfo(self.layout.get("timezone", "America/Chicago"))
        self.max_genre_tags = int(self.layout.get("max_genre_tags", 3))
        lb = self.layout.get("letterboxd") or {}
        self.lb_poll_minutes = float(lb.get("poll_minutes", 15))
        lo, hi = lb.get("jitter_seconds", [3, 12])
        self.lb_jitter = (float(lo), float(hi))

        state = State()
        self.channels: dict[str, int] = {}
        forum_spec = None
        for cat in self.layout["categories"]:
            for ch in cat["channels"]:
                cid = state.get(f"channel:{ch['key']}")
                if cid:
                    self.channels[ch["key"]] = cid
                if ch["key"] == "movies":
                    forum_spec = ch
        if "movies" not in self.channels or forum_spec is None:
            raise RuntimeError("The #pre-watch-discussion forum (key: movies) is not set up. Run setup_server.py --apply first.")
        if "movie_spoilers" not in self.channels:
            raise RuntimeError("The #post-watch-discussion forum (key: movie_spoilers) is not set up. Run setup_server.py --apply first.")

        self.tags: dict[str, TagDef] = {}
        for t in forum_spec.get("tags", []):
            tid = state.get(f"tag:movies:{t['key']}")
            if tid is None:
                raise RuntimeError(f"Forum tag {t['key']!r} is not set up. Run setup_server.py --apply.")
            self.tags[t["key"]] = TagDef(t["key"], t["name"], tid, t.get("tmdb_genres", []))
        for k in STATUS_TAGS + FLAG_TAGS:
            if k not in self.tags:
                raise RuntimeError(f"server.yaml is missing the {k!r} forum tag")

        # TMDB genre name -> genre tag key, in server.yaml order.
        self.genre_map: dict[str, str] = {}
        for t in self.tags.values():
            for g in t.tmdb_genres:
                self.genre_map.setdefault(g, t.key)
        state.db.close()
