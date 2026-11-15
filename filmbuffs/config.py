"""Runtime config: secrets from .env, layout from server.yaml, IDs from the state DB."""
from __future__ import annotations

import os
from datetime import date
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from . import groups as grp
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
        grp.expand(self.layout)        # group roles, categories and channels (Phase 12)
        self.tz = ZoneInfo(self.layout.get("timezone", "America/Chicago"))
        self.max_genre_tags = int(self.layout.get("max_genre_tags", 3))
        lb = self.layout.get("letterboxd") or {}
        self.lb_poll_minutes = float(lb.get("poll_minutes", 15))
        lo, hi = lb.get("jitter_seconds", [3, 12])
        self.lb_jitter = (float(lo), float(hi))
        ops = self.layout.get("ops") or {}
        hh, mm = str(ops.get("backup_time", "04:00")).split(":")
        self.backup_time = (int(hh), int(mm))
        self.backup_keep = int(ops.get("backup_keep", 3))
        self.backups_dir = ROOT / "backups"
        # Members with this role (or Manage Server / Administrator) can use /admin
        # commands. Optional: null or missing = no role (Phase 13).
        role = ops.get("admin_role")
        self.admin_role: str | None = (str(role).strip() or None) if role is not None else None

        # Weekly pick (Phase 5R). Each watch week's nominations run Mon–Thu of the
        # week before it and its ranked vote Fri–Sun. Nothing runs before first_watch_week.
        wk = self.layout.get("weekly") or {}
        first = wk.get("first_watch_week")
        self.first_watch_week: date | None = (
            first if isinstance(first, date) else date.fromisoformat(first) if first else None)
        if self.first_watch_week and self.first_watch_week.weekday() != 0:
            raise RuntimeError("weekly.first_watch_week in server.yaml must be a Monday")
        self.tie_break: list[str] = list(wk.get("tie_break", ["interest", "added"]))
        for k in self.tie_break:
            if k not in ("interest", "added"):
                raise RuntimeError(f"weekly.tie_break: unknown key {k!r} (👍 no longer count, "
                                   "so `thumbs` is gone)")

        state = State()
        self.channels: dict[str, int] = {}
        forum_spec = None
        post_watch_spec = None
        for cat in self.layout["categories"]:
            for ch in cat["channels"]:
                cid = state.get(f"channel:{ch['key']}")
                if cid:
                    self.channels[ch["key"]] = cid
                if ch["key"] == "movies":
                    forum_spec = ch
                elif ch["key"] == "movie_spoilers":
                    post_watch_spec = ch
        if "movies" not in self.channels or forum_spec is None:
            raise RuntimeError("The #movies forum (key: movies) is not set up. Run setup_server.py --apply first.")
        if "movie_spoilers" not in self.channels:
            raise RuntimeError("The #post-watch forum (key: movie_spoilers) is not set up. Run setup_server.py --apply first.")

        # Opt-in ping roles (Phase 10), by key (weekly_ping), plus group roles.
        self.roles: dict[str, int] = {}
        for r in self.layout.get("roles") or []:
            rid = state.get(f"role:{r['key']}")
            if rid:
                self.roles[r["key"]] = rid

        # Private groups (Phase 12), each with its own monthly movie. A group
        # whose channels aren't set up yet (setup_server.py --apply) is left out.
        self.groups: dict[str, grp.Group] = {}
        self.group_by_category: dict[int, str] = {}
        for g in grp.parse(self.layout):
            g.role_id = self.roles.get(grp.role_key(g.key))
            g.ping_role_id = self.roles.get(grp.ping_role_key(g.key))
            g.category_id = state.get(f"category:{grp.category_key(g.key)}")
            g.channels = {p: self.channels[grp.channel_key(g.key, p)] for p in grp.PARTS
                          if grp.channel_key(g.key, p) in self.channels}
            if g.category_id and "watch_planning" in g.channels:
                self.groups[g.key] = g
                self.group_by_category[g.category_id] = g.key

        self.tags: dict[str, TagDef] = {}
        for t in forum_spec.get("tags", []):
            tid = state.get(f"tag:movies:{t['key']}")
            if tid is None:
                raise RuntimeError(f"Forum tag {t['key']!r} is not set up. Run setup_server.py --apply.")
            self.tags[t["key"]] = TagDef(t["key"], t["name"], tid, t.get("tmdb_genres", []))
        for k in STATUS_TAGS + FLAG_TAGS:
            if k not in self.tags:
                raise RuntimeError(f"server.yaml is missing the {k!r} forum tag")

        # #post-watch tags (Phase 16): Reviews, the #movies genre keys, decades.
        # Unlike #movies, missing IDs don't stop the bot (the forum may not have
        # its tags yet, before setup_server.py --apply); post_watch_tags_ready says
        # whether every declared tag exists, and the one-off post-watch refresh
        # only marks itself done once they do.
        self.post_watch_tags: dict[str, TagDef] = {}
        self.post_watch_tags_ready = True
        for t in (post_watch_spec or {}).get("tags", []):
            tid = state.get(f"tag:movie_spoilers:{t['key']}")
            if tid is None:
                self.post_watch_tags_ready = False
                continue
            self.post_watch_tags[t["key"]] = TagDef(t["key"], t["name"], tid)
        movie_genres = {k for k in self.tags if k.startswith("g_")}
        pw_genres = {t["key"] for t in (post_watch_spec or {}).get("tags", [])
                     if t["key"].startswith("g_")}
        if pw_genres and pw_genres != movie_genres:
            raise RuntimeError("server.yaml: the #post-watch genre tags must use the same keys "
                               "as the #movies genre tags")

        # TMDB genre name -> genre tag key, in server.yaml order.
        self.genre_map: dict[str, str] = {}
        for t in self.tags.values():
            for g in t.tmdb_genres:
                self.genre_map.setdefault(g, t.key)
        state.db.close()
