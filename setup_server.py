#!/usr/bin/env python3
"""Reconcile the Film Buffs Discord server toward server.yaml.

    python setup_server.py            # same as --plan: show what would change
    python setup_server.py --apply    # make the changes (asks to confirm)

Safe to re-run. Objects are tracked by key -> Discord ID in data/filmbuffs.db,
so renames in server.yaml rename in Discord instead of creating duplicates.
Unmanaged channels are only reported. The one exception is the `retire:` list
in server.yaml: channels named there by ID are deleted, with safety checks,
`retire_roles:`, which deletes roles this script created (by key), and
`retire_messages:`, which deletes messages this script posted (by key; the
embed title must match), and `retire_managed:`, which deletes channels and
categories this script created (by key, once they're out of the layout).

Messages are embeds by default. `sections:` instead makes a Components V2
"page" (Phase 15): one accent-colored container, a text block per section
(normal markdown, headings included) and a divider between them. Discord caps a
V2 message's displayable text at 4000 characters (live-tested 2026-11-15).
`pin: true` keeps a message pinned.

Private groups (`groups:`, Phase 12) are expanded into a role, a category and
three hidden channels each; see filmbuffs/groups.py.
"""
from __future__ import annotations

import argparse
import asyncio
import traceback
import copy
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import discord
import yaml
from dotenv import load_dotenv

from filmbuffs import groups as grp
from filmbuffs.state import ROOT, State

REASON = "Film Buffs setup_server.py"
TEXTLIKE = (discord.ChannelType.text, discord.ChannelType.forum, discord.ChannelType.news)
SORT_ORDERS = {
    "latest_activity": discord.ForumOrderType.latest_activity,
    "creation_date": discord.ForumOrderType.creation_date,
}
TTY = sys.stdout.isatty()
PAGE_TEXT_LIMIT = 4000       # Components V2 displayable text (live-tested 2026-11-15)
PAGE_MAX_SECTIONS = 19       # 40 components: container + sections + dividers
MENTION_LEN = 22             # "<#" + a 19-digit snowflake + ">"
CHANNEL_REF = re.compile(r"\{#([^{}]*)\}")


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


OP_STYLE = {"+": "32", "~": "33", ">": "36", "-": "31", "!": "35"}


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------- config

def load_config(path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    try:
        grp.expand(cfg)
    except grp.GroupConfigError as e:
        raise ConfigError(str(e)) from e
    presets = cfg.get("presets", {})
    valid = set(discord.Permissions.VALID_FLAGS)
    seen: set[str] = set()

    def unique(k: str) -> None:
        if k in seen:
            raise ConfigError(f"duplicate key {k!r}")
        seen.add(k)

    for cat in cfg["categories"]:
        unique(f"category:{cat['key']}")
        for ch in cat["channels"]:
            unique(f"channel:{ch['key']}")
            ch.setdefault("type", "text")
            if ch["type"] not in ("text", "forum", "voice"):
                raise ConfigError(f"{ch['key']}: type must be text, forum or voice")
            if ch["type"] == "voice" and "topic" in ch:
                raise ConfigError(f"{ch['key']}: voice channels have no topic")
            ows = copy.deepcopy(presets[ch["preset"]]) if "preset" in ch else {}
            ows.update(ch.get("overwrites") or {})
            for target, spec in ows.items():
                for p in spec.get("allow", []) + spec.get("deny", []):
                    if p not in valid:
                        raise ConfigError(f"{ch['key']}: unknown permission {p!r} for {target}")
            ch["_overwrites"] = ows
            if ch["type"] == "forum":
                tags = ch.get("tags", [])
                if len(tags) > 20:
                    raise ConfigError(f"{ch['key']}: Discord allows at most 20 forum tags")
                for t in tags:
                    unique(f"tag:{ch['key']}:{t['key']}")
                    if len(t["name"]) > 20:
                        raise ConfigError(f"tag {t['name']!r}: names are limited to 20 characters")
                if ch.get("default_sort") and ch["default_sort"] not in SORT_ORDERS:
                    raise ConfigError(f"{ch['key']}: default_sort must be one of {list(SORT_ORDERS)}")
                if len(ch.get("topic", "")) > 4096:
                    raise ConfigError(f"{ch['key']}: forum guidelines are limited to 4096 characters")
            elif len(ch.get("topic", "")) > 1024:
                raise ConfigError(f"{ch['key']}: text channel topics are limited to 1024 characters")
    role_keys = set()
    for r in cfg.get("roles", []):
        unique(f"role:{r['key']}")
        role_keys.add(r["key"])
        if not r.get("name"):
            raise ConfigError(f"role {r['key']}: needs a name")
    channel_keys = {ch["key"] for cat in cfg["categories"] for ch in cat["channels"]}
    for m in cfg.get("messages", []):
        unique(f"message:{m['key']}")
        if m["channel"] not in channel_keys:
            raise ConfigError(f"message {m['key']}: unknown channel {m['channel']!r}")
        sections = m.get("sections")
        texts = [m.get("description", "")] + list(sections or [])
        for ref in (r for t in texts for r in CHANNEL_REF.findall(t)):
            if ref not in channel_keys:
                hint = ref.replace("-", "_")
                raise ConfigError(
                    f"message {m['key']}: unknown channel ref {{#{ref}}} (use a channel key"
                    + (f", e.g. {{#{hint}}}" if hint in channel_keys else "") + ")")
        if sections is not None:
            if m.get("description") or m.get("title") or m.get("buttons"):
                raise ConfigError(f"message {m['key']}: a page (sections:) has no title, "
                                  "description or buttons; put the heading in the first section")
            if not sections or not all(isinstance(x, str) and x.strip() for x in sections):
                raise ConfigError(f"message {m['key']}: sections must be non-empty strings")
            if len(sections) > PAGE_MAX_SECTIONS:
                raise ConfigError(f"message {m['key']}: at most {PAGE_MAX_SECTIONS} sections")
            size = page_text_size(sections)
            if size > PAGE_TEXT_LIMIT:
                raise ConfigError(f"message {m['key']}: {size} characters with channel links; "
                                  f"Discord allows {PAGE_TEXT_LIMIT}")
        buttons = m.get("buttons") or []
        if len(buttons) > 5:
            raise ConfigError(f"message {m['key']}: at most 5 buttons")
        for b in buttons:
            if b.get("role") not in role_keys:
                raise ConfigError(f"message {m['key']}: button for unknown role {b.get('role')!r}")
            if not b.get("label") or len(b["label"]) > 80:
                raise ConfigError(f"message {m['key']}: button labels are 1-80 characters")
    message_keys = {m["key"] for m in cfg.get("messages", [])}
    for r in cfg.get("retire_messages", []):
        if not r.get("key") or not r.get("title") or r.get("channel") not in channel_keys:
            raise ConfigError(f"retire_messages entries need a key, a title and a channel key: {r}")
        if r["key"] in message_keys:
            raise ConfigError(f"message {r['key']!r} is both declared and retired")
    for r in cfg.get("retire", []):
        if not isinstance(r.get("id"), int) or not r.get("name"):
            raise ConfigError(f"retire entries need an integer id and a name: {r}")
    for r in cfg.get("retire_managed", []):
        key = r.get("key") or ""
        if not key.startswith(("channel:", "category:")) or not r.get("name"):
            raise ConfigError(f"retire_managed entries need a channel:/category: key and a name: {r}")
        if key in seen:
            raise ConfigError(f"{key!r} is both declared and retired")
    for r in cfg.get("retire_roles", []):
        if not r.get("key") or not r.get("name"):
            raise ConfigError(f"retire_roles entries need a key and a name: {r}")
        if r["key"] in role_keys:
            raise ConfigError(f"role {r['key']!r} is both declared and retired")
    for cat in cfg["categories"]:
        for ch in cat["channels"]:
            for target in ch["_overwrites"]:
                if target.startswith("role:") and target[5:] not in role_keys:
                    raise ConfigError(f"{ch['key']}: overwrite for unknown role key {target!r}")
    return cfg


class PendingRole:
    """A managed role the plan creates before any channel that refers to it."""
    def __init__(self, key: str, name: str):
        self.key, self.name, self.id = key, name, None


def build_overwrite(spec: dict) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite()
    for p in spec.get("allow", []):
        setattr(ow, p, True)
    for p in spec.get("deny", []):
        setattr(ow, p, False)
    return ow


def describe_overwrite(ow: discord.PermissionOverwrite | None) -> str:
    if ow is None or ow.is_empty():
        return "(none)"
    # discord.py iterates View Channel under its legacy alias "read_messages".
    alias = {"read_messages": "view_channel"}
    allow = sorted(alias.get(k, k) for k, v in ow if v is True)
    deny = sorted(alias.get(k, k) for k, v in ow if v is False)
    parts = []
    if allow:
        parts.append("allow " + ", ".join(allow))
    if deny:
        parts.append("deny " + ", ".join(deny))
    return "; ".join(parts)


def norm_emoji(e) -> str:
    """Compare unicode emoji ignoring the variation selector Discord may strip."""
    if e is None:
        return ""
    return str(getattr(e, "name", e) or "").replace("️", "")


def page_text_size(sections: list[str]) -> int:
    """Worst-case displayable length of a page: each {#key} counted as a full mention."""
    return sum(len(CHANNEL_REF.sub("x" * MENTION_LEN, t.strip())) for t in sections)


def build_page(sections: list[str], accent: int | None) -> discord.ui.LayoutView:
    """A Components V2 page: one container, text blocks split by dividers."""
    children: list[discord.ui.Item] = []
    for i, text in enumerate(sections):
        if i:
            children.append(discord.ui.Separator(visible=True,
                                                 spacing=discord.SeparatorSpacing.large))
        children.append(discord.ui.TextDisplay(text))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*children, accent_color=accent))
    return view


def first_line(text: str, n: int = 70) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= n else line[: n - 1] + "…"


# ----------------------------------------------------------------------- plan

@dataclass
class Change:
    op: str                 # + create, ~ update, > adopt, - delete (retire only)
    what: str
    details: list[str] = field(default_factory=list)
    run: Callable[[], Awaitable[None]] | None = None


class Reconciler:
    def __init__(self, guild: discord.Guild, cfg: dict, state: State):
        self.guild = guild
        self.cfg = cfg
        self.state = state
        self.changes: list[Change] = []
        self.notes: list[str] = []
        self.adopted: set[int] = set()
        self.pending_create = False   # anything new means positions are rechecked at apply time

    # helpers ---------------------------------------------------------------
    def add(self, op, what, details=None, run=None):
        self.changes.append(Change(op, what, details or [], run))

    def lookup(self, key: str):
        cid = self.state.get(key)
        obj = self.guild.get_channel(cid) if cid else None
        if cid and obj is None:
            self.notes.append(f"{key}: tracked channel {cid} no longer exists; it will be recreated")
        return obj

    def adopt(self, name: str, kind: type, label: str):
        managed = self.state.all_ids()
        matches = [c for c in self.guild.channels
                   if isinstance(c, kind) and c.name == name
                   and c.id not in managed and c.id not in self.adopted]
        if len(matches) > 1:
            raise ConfigError(f"{label}: {len(matches)} channels named {name!r}; adopt is ambiguous")
        if matches:
            self.adopted.add(matches[0].id)
            return matches[0]
        return None

    def target(self, name: str):
        """An overwrite target: @everyone, @bot, role:<key> (a managed role) or a role name."""
        if name.startswith("role:"):
            key = name[5:]
            rid = self.state.get(f"role:{key}")
            role = self.guild.get_role(rid) if rid else None
            if role is None:
                r = next(r for r in self.cfg.get("roles", []) if r["key"] == key)
                role = discord.utils.get(self.guild.roles, name=r["name"])
                if role is None or role.id in self.state.all_ids():
                    return PendingRole(key, r["name"])
            return role
        if name == "@everyone":
            return self.guild.default_role
        if name == "@bot":
            role = self.guild.self_role
            if role is None:
                raise ConfigError("the bot has no role of its own in this server")
            return role
        role = discord.utils.get(self.guild.roles, name=name)
        if role is None:
            raise ConfigError(f"role {name!r} does not exist (roles are not managed)")
        return role

    def channel_ref(self, key: str, live: bool) -> str:
        cid = self.state.get(f"channel:{key}")
        if cid and self.guild.get_channel(cid):
            return f"<#{cid}>"
        if live:
            raise RuntimeError(f"channel {key} does not exist yet")
        name = next(c["name"] for cat in self.cfg["categories"]
                    for c in cat["channels"] if c["key"] == key)
        return f"#{name}"

    # categories ------------------------------------------------------------
    def plan_category(self, cat: dict) -> None:
        key = f"category:{cat['key']}"
        name = cat["name"]
        obj = self.lookup(key)
        if obj is None and cat.get("adopt"):
            obj = self.adopt(cat["adopt"], discord.CategoryChannel, key)
            if obj:
                self.add(">", f'category "{obj.name}"', [f"start managing as {key}"],
                         self._record(key, "category", obj.id))
        if obj is None:
            self.pending_create = True

            async def create():
                c = await self.guild.create_category(name, reason=REASON)
                self.state.set(key, "category", c.id)
            self.add("+", f'category "{name}"', [], create)
            return
        if obj.name != name:
            async def rename(obj=obj):
                await obj.edit(name=name, reason=REASON)
            self.add("~", f'category "{obj.name}"', [f"name: {obj.name!r} → {name!r}"], rename)

    def _record(self, key, kind, cid, extra=None):
        async def run():
            self.state.set(key, kind, cid, extra)
        return run

    # channels --------------------------------------------------------------
    def plan_channel(self, cat: dict, ch: dict) -> None:
        key = f"channel:{ch['key']}"
        is_forum = ch["type"] == "forum"
        kind = {"text": discord.TextChannel, "forum": discord.ForumChannel,
                "voice": discord.VoiceChannel}[ch["type"]]
        cat_key = f"category:{cat['key']}"
        desired_ows = {self.target(t): build_overwrite(s) for t, s in ch["_overwrites"].items()}
        pending = [t for t in desired_ows if isinstance(t, PendingRole)]
        target_ids = [t.id for t in desired_ows if not isinstance(t, PendingRole)]

        def resolved():
            """Overwrites with roles created earlier in this apply filled in."""
            out = {}
            for t, ow in desired_ows.items():
                if isinstance(t, PendingRole):
                    t = self.guild.get_role(self.state.get(f"role:{t.key}") or 0)
                    if t is None:
                        raise RuntimeError("a role this channel needs wasn't created")
                out[t] = ow
            return out

        obj = self.lookup(key)
        if obj is None and ch.get("adopt"):
            obj = self.adopt(ch["adopt"], discord.abc.GuildChannel, key)
            if obj:
                self.add(">", f"#{obj.name}", [f"start managing existing channel as {key}"],
                         self._record(key, "channel", obj.id, {"targets": []}))

        if obj is None:
            self.pending_create = True
            details = [f"type: {ch['type']}, in {cat['name']}"]
            if ch.get("topic"):
                details.append(f"{'guidelines' if is_forum else 'topic'}: {first_line(ch['topic'])}")
            for t, ow in desired_ows.items():
                details.append(f"{t.name}: {describe_overwrite(ow)}")
            if is_forum:
                if ch.get("default_sort"):
                    details.append(f"default sort: {ch['default_sort']}")
                details.append("tags: " + ", ".join(
                    f"{t.get('emoji', '')}{t['name']}{' (moderated)' if t.get('moderated') else ''}"
                    for t in ch.get("tags", [])))

            async def create():
                category = self.guild.get_channel(self.state.get(cat_key))
                ows = resolved()
                kwargs = dict(name=ch["name"], category=category,
                              overwrites=ows, reason=REASON)
                if "topic" in ch:
                    kwargs["topic"] = ch["topic"].strip()
                if is_forum:
                    if ch.get("default_sort"):
                        kwargs["default_sort_order"] = SORT_ORDERS[ch["default_sort"]]
                    kwargs["available_tags"] = [
                        discord.ForumTag(name=t["name"], emoji=t.get("emoji"),
                                         moderated=bool(t.get("moderated")))
                        for t in ch.get("tags", [])]
                    c = await self.guild.create_forum(**kwargs)
                    self._record_tags(ch, c)
                elif ch["type"] == "voice":
                    c = await self.guild.create_voice_channel(**kwargs)
                else:
                    c = await self.guild.create_text_channel(**kwargs)
                self.state.set(key, "channel", c.id, {"targets": [t.id for t in ows]})
            self.add("+", f"#{ch['name']}", details, create)
            return

        if pending:
            self.notes.append(f"#{obj.name}: role(s) {', '.join(t.name for t in pending)} will be "
                              "created; run --apply again to set this channel's permissions")
            return

        if not isinstance(obj, kind):
            raise ConfigError(f"{key}: #{obj.name} is a {obj.type.name} channel, "
                              f"server.yaml says {ch['type']}")

        edits: dict = {}
        details: list[str] = []
        if obj.name != ch["name"]:
            edits["name"] = ch["name"]
            details.append(f"name: {obj.name!r} → {ch['name']!r}")
        if "topic" in ch and (obj.topic or "") != ch["topic"].strip():
            edits["topic"] = ch["topic"].strip()
            details.append(f"topic: {first_line(obj.topic or '(none)')!r} → "
                           f"{first_line(ch['topic'])!r}")
        cat_id = self.state.get(cat_key)
        cat_obj = self.guild.get_channel(cat_id) if cat_id else None
        if cat_obj is None or obj.category_id != cat_obj.id:
            edits["category"] = None  # resolved at run time (category may be new)
            details.append(f"category: {obj.category.name if obj.category else '(none)'} "
                           f"→ {cat['name']}")

        # Overwrites: only declared targets (and ones we declared before) are managed.
        prev_targets = set(self.state.get_extra(key).get("targets", []))
        merged = dict(obj.overwrites)
        ow_changed = False
        for t, ow in desired_ows.items():
            cur = obj.overwrites.get(t)
            if cur is None or cur.pair() != ow.pair():
                merged[t] = ow
                ow_changed = True
                details.append(f"{t.name}: {describe_overwrite(cur)} → {describe_overwrite(ow)}")
        for t, cur in obj.overwrites.items():
            if t.id in target_ids:
                continue
            if t.id in prev_targets:
                del merged[t]
                ow_changed = True
                details.append(f"{t.name}: remove overwrite (no longer declared)")
            else:
                self.notes.append(f"#{obj.name}: overwrite for {t.name} is unmanaged; left alone")
        if ow_changed:
            edits["overwrites"] = merged

        if is_forum and ch.get("default_sort"):
            want = SORT_ORDERS[ch["default_sort"]]
            if obj.default_sort_order != want:
                edits["default_sort_order"] = want
                cur = obj.default_sort_order.name if obj.default_sort_order else "(unset)"
                details.append(f"default sort: {cur} → {ch['default_sort']}")

        if edits or set(target_ids) != prev_targets:
            async def update(obj=obj, edits=edits):
                if "category" in edits:
                    edits["category"] = self.guild.get_channel(self.state.get(cat_key))
                if edits:
                    await obj.edit(**edits, reason=REASON)
                self.state.set(key, "channel", obj.id, {"targets": target_ids})
            if edits:
                self.add("~", f"#{obj.name}", details, update)
            else:
                self.add(">", f"#{obj.name}", ["record managed overwrite targets"], update)

        if is_forum:
            self.plan_tags(ch, obj)

    def _record_tags(self, ch: dict, channel: discord.ForumChannel) -> None:
        by_name = {t.name: t.id for t in channel.available_tags}
        for t in ch.get("tags", []):
            if t["name"] in by_name:
                self.state.set(f"tag:{ch['key']}:{t['key']}", "tag", by_name[t["name"]],
                               {"channel": channel.id})

    def plan_tags(self, ch: dict, obj: discord.ForumChannel) -> None:
        current = {t.id: t for t in obj.available_tags}
        managed_tag_ids = {self.state.get(f"tag:{ch['key']}:{t['key']}")
                           for t in ch.get("tags", [])} - {None}
        used: set[int] = set()
        final: list[discord.ForumTag] = []
        details: list[str] = []
        need_record = False
        # retire_tags: [{key, name}] removes a tag the bot used to manage. The name
        # must still match, as a guard against removing the wrong one.
        declared = {t["key"] for t in ch.get("tags", [])}
        retiring: dict[int, dict] = {}
        for r in ch.get("retire_tags", []):
            if r.get("key") in declared:
                raise ConfigError(f"#{obj.name}: tag {r.get('key')!r} is both declared and retired")
            tid = self.state.get(f"tag:{ch['key']}:{r.get('key')}")
            if tid in current:
                if current[tid].name != r.get("name"):
                    raise ConfigError(f"#{obj.name}: retired tag {r.get('key')!r} is named "
                                      f"{current[tid].name!r}, not {r.get('name')!r}; check it")
                retiring[tid] = r
        for t in ch.get("tags", []):
            tkey = f"tag:{ch['key']}:{t['key']}"
            existing = current.get(self.state.get(tkey) or 0)
            if existing is None:
                existing = next((x for x in obj.available_tags
                                 if x.name == t["name"] and x.id not in managed_tag_ids
                                 and x.id not in used), None)
                if existing:
                    need_record = True
            new = discord.ForumTag(name=t["name"], emoji=t.get("emoji"),
                                   moderated=bool(t.get("moderated")))
            if existing:
                new.id = existing.id
                used.add(existing.id)
                diffs = []
                if existing.name != new.name:
                    diffs.append(f"name {existing.name!r} → {new.name!r}")
                if norm_emoji(existing.emoji) != norm_emoji(t.get("emoji")):
                    diffs.append(f"emoji {norm_emoji(existing.emoji) or '(none)'} → "
                                 f"{t.get('emoji') or '(none)'}")
                if existing.moderated != new.moderated:
                    diffs.append(f"moderated {existing.moderated} → {new.moderated}")
                if diffs:
                    details.append(f"tag {existing.name}: " + ", ".join(diffs))
            else:
                details.append(f"+ tag {t.get('emoji', '')}{t['name']}"
                               f"{' (moderated)' if t.get('moderated') else ''}")
            final.append(new)
        for x in obj.available_tags:
            if x.id in retiring:
                details.append(f"- tag {x.name} (retire_tags; it comes off every post)")
            elif x.id not in used:
                final.append(x)
                self.notes.append(f"#{obj.name}: tag {x.name!r} is unmanaged; left alone")
        if len(final) > 20:
            raise ConfigError(f"#{obj.name}: {len(final)} tags including unmanaged ones; limit is 20")
        if details or need_record:
            async def update(obj=obj, final=final):
                if details:
                    await obj.edit(available_tags=final, reason=REASON)
                fresh = await self.guild.fetch_channel(obj.id)
                self._record_tags(ch, fresh)
            self.add("~" if details else ">", f"#{obj.name} tags",
                     details or ["start managing existing tags"], update)

    # positions -------------------------------------------------------------
    def desired_positions(self, channels) -> tuple[list[dict], list[str]]:
        """Return (payload, description) to put managed things in declared order."""
        payload: list[dict] = []
        desc: list[str] = []
        by_id = {c.id: c for c in channels}
        key_of = lambda c: (c.position, c.id)

        cats = sorted((c for c in channels if isinstance(c, discord.CategoryChannel)), key=key_of)
        managed_cats = [by_id[i] for i in
                        (self.state.get(f"category:{c['key']}") for c in self.cfg["categories"])
                        if i in by_id]
        cat_seq = cats
        if self.cfg.get("managed_categories_first", True):
            want = managed_cats + [c for c in cats if c not in managed_cats]
            cat_seq = want
            if [c.id for c in want] != [c.id for c in cats]:
                payload += [{"id": c.id, "position": i} for i, c in enumerate(want)]
                desc.append("categories: " + " | ".join(c.name for c in want))

        # Channel positions are one sequence per type group across the whole server
        # (not per category): numbering each category from 0 collides with other
        # categories and Discord reshuffles ties by ID (seen live 2026-11-20). So when
        # any managed category is out of order, renumber that whole type group.
        cat_order = {c.id: i for i, c in enumerate(cat_seq)}
        spec = {self.state.get(f"category:{c['key']}"): c for c in self.cfg["categories"]}
        # Discord sorts text-like and voice channels as separate groups.
        for group in (TEXTLIKE, (discord.ChannelType.voice, discord.ChannelType.stage_voice)):
            members = [c for c in channels
                       if not isinstance(c, discord.CategoryChannel) and c.type in group]
            wrong = False
            ordered: dict[int | None, list] = {}
            for cid in [None, *sorted(cat_order, key=cat_order.get)]:
                inside = sorted((c for c in members if getattr(c, "category_id", None) == cid),
                                key=key_of)
                cat = spec.get(cid) if cid is not None else None
                if cat is not None:
                    managed = [by_id[i] for i in
                               (self.state.get(f"channel:{c['key']}") for c in cat["channels"])
                               if i in by_id and by_id[i] in inside]
                    want_in = managed + [c for c in inside if c not in managed]
                    if [c.id for c in want_in] != [c.id for c in inside]:
                        wrong = True
                        desc.append(f"{cat['name']}: " + ", ".join(c.name for c in want_in))
                    inside = want_in
                ordered[cid] = inside
            if wrong:
                flat = [c for cs in ordered.values() for c in cs]
                flat += [c for c in members if c not in flat]      # categories not listed
                payload += [{"id": c.id, "position": i} for i, c in enumerate(flat)]
        return payload, desc

    def plan_positions(self) -> None:
        async def run():
            channels = await self.guild.fetch_channels()
            payload, _ = self.desired_positions(channels)
            if payload:
                await self.guild._state.http.bulk_channel_update(
                    self.guild.id, payload, reason=REASON)

        if self.pending_create or self.adopted:
            self.add("~", "channel order",
                     ["managed categories on top, in declared order; unmanaged ones keep "
                      "their order below" if self.cfg.get("managed_categories_first", True)
                      else "category order left alone",
                      "channels inside managed categories in declared order",
                      "(exact moves computed after the creates above)"], run)
            return
        payload, desc = self.desired_positions(self.guild.channels)
        if payload:
            self.add("~", "channel order", desc, run)

    # roles -----------------------------------------------------------------
    def plan_role(self, r: dict) -> None:
        key = f"role:{r['key']}"
        rid = self.state.get(key)
        role = self.guild.get_role(rid) if rid else None
        if rid and role is None:
            self.notes.append(f"{key}: tracked role {rid} no longer exists; it will be recreated")
        mentionable = bool(r.get("mentionable", False))
        if role is None:
            managed = self.state.all_ids()
            same = [x for x in self.guild.roles if x.name == r["name"] and x.id not in managed]
            if len(same) > 1:
                raise ConfigError(f"role {r['name']!r}: {len(same)} roles with that name")
            if same:
                role = same[0]
                self.add(">", f'role "{role.name}"', ["existing role, recorded by key"],
                         self._record_role(key, role.id))
        if role is None:
            async def create():
                new = await self.guild.create_role(name=r["name"], mentionable=mentionable,
                                                   reason=REASON)
                self.state.set(key, "role", new.id)
            self.add("+", f'role "{r["name"]}"', [f"mentionable: {mentionable}"], create)
            return
        edits = {}
        if role.name != r["name"]:
            edits["name"] = r["name"]
        if role.mentionable != mentionable:
            edits["mentionable"] = mentionable
        if edits:
            async def update(role=role, edits=edits):
                await role.edit(reason=REASON, **edits)
            self.add("~", f'role "{role.name}"', [f"{k}: {v}" for k, v in edits.items()], update)
        me = self.guild.me.top_role if self.guild.me else None
        if me is not None and role >= me:
            self.notes.append(f'role "{role.name}" is above the bot\'s role; move the bot\'s '
                              "role above it so the role buttons work")

    def _record_role(self, key, rid):
        async def run():
            self.state.set(key, "role", rid)
        return run

    # messages --------------------------------------------------------------
    def render(self, m: dict, live: bool):
        """(embed or None, page sections or None, digest). Pages render as Components V2."""
        sub = lambda t: CHANNEL_REF.sub(lambda x: self.channel_ref(x.group(1), live), t.strip())
        if m.get("sections") is not None:
            sections = [sub(t) for t in m["sections"]]
            if live and sum(map(len, sections)) > PAGE_TEXT_LIMIT:
                raise RuntimeError(f"message {m['key']}: over {PAGE_TEXT_LIMIT} characters")
            payload = {"page": sections, "color": m.get("color"), "pin": bool(m.get("pin"))}
            embed = None
        else:
            sections = None
            embed = discord.Embed(title=m.get("title"), description=sub(m.get("description", "")),
                                  color=m.get("color"))
            payload = embed.to_dict()
            if m.get("buttons"):
                payload = {"embed": payload, "buttons": m["buttons"]}
            if m.get("pin"):
                payload = {"embed": payload, "pin": True}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True)
                                .encode()).hexdigest()
        return embed, sections, digest

    @staticmethod
    def view(m: dict) -> discord.ui.View | None:
        """Role buttons. The running bot handles clicks (custom_id role:<key>)."""
        if not m.get("buttons"):
            return None
        v = discord.ui.View(timeout=None)
        for b in m["buttons"]:
            v.add_item(discord.ui.Button(label=b["label"], emoji=b.get("emoji"),
                                         style=discord.ButtonStyle.secondary,
                                         custom_id=f"role:{b['role']}"))
        return v

    @staticmethod
    def label(m: dict) -> str:
        if m.get("sections"):
            return first_line(m["sections"][0]).lstrip("# ").strip()
        return m.get("title", m["key"])

    async def plan_message(self, m: dict) -> None:
        key = f"message:{m['key']}"
        ch_key = f"channel:{m['channel']}"
        ch_name = next(c["name"] for cat in self.cfg["categories"]
                       for c in cat["channels"] if c["key"] == m["channel"])
        channel = self.guild.get_channel(self.state.get(ch_key) or 0)
        msg_id = self.state.get(key)
        _, _, digest = self.render(m, live=False)
        page = m.get("sections") is not None

        existing = None
        if channel and msg_id:
            try:
                existing = await channel.fetch_message(msg_id)
            except discord.NotFound:
                self.notes.append(f"{key}: message {msg_id} was deleted; it will be reposted")

        async def run():
            ch = self.guild.get_channel(self.state.get(ch_key))
            embed, sections, live_digest = self.render(m, live=True)
            msg = None
            if self.state.get(key):
                try:
                    msg = await ch.fetch_message(self.state.get(key))
                except discord.NotFound:
                    msg = None
            none = discord.AllowedMentions.none()
            if sections is not None:
                # Editing an embed message into V2 is allowed (the flag can only be
                # set, never cleared), so the message keeps its ID and place.
                view = build_page(sections, m.get("color"))
                if msg:
                    msg = await msg.edit(content="", embed=None, view=view, allowed_mentions=none)
                else:
                    msg = await ch.send(view=view, allowed_mentions=none)
            else:
                view = self.view(m)
                if msg:
                    msg = await msg.edit(content=None, embed=embed, view=view)
                else:
                    msg = await ch.send(embed=embed, view=view)
            if m.get("pin") and not msg.pinned:
                await msg.pin(reason=REASON)
            self.state.set(key, "message", msg.id, {"hash": live_digest})

        what = f'message "{self.label(m)}" in #{ch_name}'
        if existing is None:
            first = m["sections"][0] if page else m.get("description", "")
            self.add("+", what, [first_line(first)] + (["pinned"] if m.get("pin") else []), run)
            return
        details = []
        if self.state.get_extra(key).get("hash") != digest:
            details.append("content changed; edit in place")
        if page and not existing.flags.components_v2:
            details.append("embed → Components V2 page (same message)")
        if m.get("pin") and not existing.pinned:
            details.append("pin it")
        if details:
            self.add("~", what, details, run)

    async def plan_retire_messages(self) -> None:
        """Delete messages this script posted, listed under `retire_messages:` (key, title, channel)."""
        for r in self.cfg.get("retire_messages", []):
            key = f"message:{r['key']}"
            msg_id = self.state.get(key)
            if not msg_id:
                continue
            ch = self.guild.get_channel(self.state.get(f"channel:{r['channel']}") or 0)
            msg = None
            if ch is not None:
                try:
                    msg = await ch.fetch_message(msg_id)
                except discord.NotFound:
                    msg = None
            if msg is None:
                async def forget(key=key):
                    self.state.delete(key)
                self.add(">", f'message "{r["title"]}"', ["already deleted; forget its key"], forget)
                continue
            title = msg.embeds[0].title if msg.embeds else None
            if title != r["title"]:
                raise ConfigError(f"retire_messages {r['key']}: message title is {title!r}, not "
                                  f"{r['title']!r}; refusing to delete")

            async def run(msg=msg, key=key):
                await msg.delete()
                self.state.delete(key)
            self.add("-", f'message "{r["title"]}" in #{msg.channel.name}', [], run)

    # retire ----------------------------------------------------------------
    async def plan_retire(self) -> None:
        """Delete channels listed by ID under `retire:`. Runs after everything else."""
        managed = self.state.all_ids() | self.adopted
        retiring = {r["id"] for r in self.cfg.get("retire", [])}
        # Channels first, then categories, so categories are empty when deleted.
        entries = sorted(self.cfg.get("retire", []), key=lambda r: isinstance(
            self.guild.get_channel(r["id"]), discord.CategoryChannel))
        for r in entries:
            obj = self.guild.get_channel(r["id"])
            if obj is None:
                continue  # already gone
            if obj.name != r["name"]:
                raise ConfigError(f"retire {r['id']}: name is {obj.name!r}, not {r['name']!r}; "
                                  "refusing to delete")
            if obj.id in managed:
                raise ConfigError(f"retire {obj.name!r}: it is managed; remove it from the "
                                  "layout first")
            details = []
            if isinstance(obj, discord.CategoryChannel):
                staying = [c.name for c in obj.channels
                           if c.id not in managed and c.id not in retiring]
                if staying:
                    raise ConfigError(f"retire category {obj.name!r}: still holds {staying}")
                label = f'category "{obj.name}"'
                details.append("empty once the moves and deletes above are done")
            else:
                label = f"{obj.type.name} channel {obj.name!r}"
                if isinstance(obj, discord.abc.Messageable):
                    has = [m async for m in obj.history(limit=1)]
                    if has and not r.get("allow_messages"):
                        raise ConfigError(f"retire {obj.name!r}: it has messages; add "
                                          "allow_messages: true to delete it anyway")
                    details.append("has messages (allowed)" if has else "no messages")

            async def run(cid=obj.id):
                fresh = await self.guild.fetch_channel(cid)
                if isinstance(fresh, discord.CategoryChannel):
                    children = [c for c in await self.guild.fetch_channels()
                                if getattr(c, "category_id", None) == cid]
                    if children:
                        raise RuntimeError(f"category still holds {[c.name for c in children]}")
                await fresh.delete(reason=REASON)
            self.add("-", label, details, run)

    async def plan_retire_managed(self) -> None:
        """Delete channels/categories this script created, listed under `retire_managed:`
        by key (no longer declared). Same safety checks as `retire:`; the key is
        forgotten once it's gone. Runs after the moves, channels before categories."""
        entries = []
        for r in self.cfg.get("retire_managed", []):
            cid = self.state.get(r["key"])
            obj = self.guild.get_channel(cid) if cid else None
            if obj is None:
                if cid:
                    async def forget(key=r["key"]):
                        self.state.delete(key)
                    self.add(">", r["name"], ["already deleted; forget its key"], forget)
                continue
            if obj.name != r["name"]:
                raise ConfigError(f"retire_managed {r['key']}: name is {obj.name!r}, not "
                                  f"{r['name']!r}; refusing to delete")
            entries.append((r, obj))
        retiring = {obj.id for _, obj in entries}
        declared = {self.state.get(f"channel:{ch['key']}")
                    for cat in self.cfg["categories"] for ch in cat["channels"]}
        entries.sort(key=lambda e: isinstance(e[1], discord.CategoryChannel))
        for r, obj in entries:
            details = []
            if isinstance(obj, discord.CategoryChannel):
                staying = [c.name for c in obj.channels
                           if c.id not in declared and c.id not in retiring]
                if staying:
                    raise ConfigError(f"retire category {obj.name!r}: still holds {staying}")
                label = f'category "{obj.name}"'
                details.append("empty once the moves and deletes above are done")
            else:
                label = f"{obj.type.name} channel {obj.name!r}"
                if isinstance(obj, discord.abc.Messageable) and not isinstance(obj, discord.VoiceChannel):
                    has = [m async for m in obj.history(limit=1)]
                    if has and not r.get("allow_messages"):
                        raise ConfigError(f"retire_managed {obj.name!r}: it has messages; add "
                                          "allow_messages: true to delete it anyway")
                    details.append("has messages (allowed)" if has else "no messages")

            async def run(cid=obj.id, key=r["key"]):
                fresh = await self.guild.fetch_channel(cid)
                if isinstance(fresh, discord.CategoryChannel):
                    children = [c for c in await self.guild.fetch_channels()
                                if getattr(c, "category_id", None) == cid]
                    if children:
                        raise RuntimeError(f"category still holds {[c.name for c in children]}")
                await fresh.delete(reason=REASON)
                self.state.delete(key)
            self.add("-", label, details, run)

    def plan_retire_roles(self) -> None:
        """Delete roles this script created, listed under `retire_roles:` (key + name)."""
        for r in self.cfg.get("retire_roles", []):
            key = f"role:{r['key']}"
            rid = self.state.get(key)
            role = self.guild.get_role(rid) if rid else None
            if role is None:
                if rid:
                    async def forget(key=key):
                        self.state.delete(key)
                    self.add(">", f'role "{r["name"]}"', ["already deleted; forget its key"], forget)
                continue
            if role.name != r["name"]:
                raise ConfigError(f"retire_roles {r['key']}: role is named {role.name!r}, not "
                                  f"{r['name']!r}; refusing to delete")
            n = len(role.members)

            async def run(role=role, key=key):
                await role.delete(reason=REASON)
                self.state.delete(key)
            self.add("-", f'role "{role.name}"',
                     [f"{n} member{'s' * (n != 1)} have it" if n else "nobody has it"
                      if self.guild.chunked else "(member count unknown)"], run)

    # ---------------------------------------------------------------------
    async def build(self) -> None:
        # Roles first: group channels' permissions refer to group roles.
        for r in self.cfg.get("roles", []):
            self.plan_role(r)
        for cat in self.cfg["categories"]:
            self.plan_category(cat)
        for cat in self.cfg["categories"]:
            for ch in cat["channels"]:
                self.plan_channel(cat, ch)
        self.plan_positions()
        for m in self.cfg.get("messages", []):
            await self.plan_message(m)
        await self.plan_retire_messages()
        await self.plan_retire_managed()
        await self.plan_retire()
        self.plan_retire_roles()

        managed = (self.state.all_ids() | self.adopted
                   | {r["id"] for r in self.cfg.get("retire", [])})
        for c in sorted(self.guild.channels, key=lambda c: (c.position, c.id)):
            if c.id not in managed:
                where = f" in {c.category.name}" if getattr(c, "category", None) else ""
                kind = "category" if isinstance(c, discord.CategoryChannel) else f"{c.type.name} channel"
                self.notes.append(f"{kind} {c.name!r}{where} is unmanaged; left alone")


# ------------------------------------------------------------------------ main

def print_plan(guild: discord.Guild, r: Reconciler) -> None:
    print(color(f"Film Buffs setup — {guild.name} ({guild.id})", "1"))
    print()
    if not r.changes:
        print("No changes. The server matches server.yaml.")
    for c in r.changes:
        print(color(f"  {c.op} {c.what}", OP_STYLE.get(c.op, "0")))
        for d in c.details:
            print(f"        {d}")
    if r.notes:
        print()
        print(color("Not managed / notes:", "1"))
        for n in r.notes:
            print(color(f"  ! {n}", OP_STYLE["!"]))
    counts = {op: sum(1 for c in r.changes if c.op == op) for op in "+~>-"}
    print()
    print(f"Plan: {counts['+']} to create, {counts['~']} to change, "
          f"{counts['>']} to adopt/record, {counts['-']} to delete (retire lists only).")


async def apply_plan(r: Reconciler) -> int:
    for i, c in enumerate(r.changes, 1):
        print(color(f"[{i}/{len(r.changes)}] {c.op} {c.what}", OP_STYLE.get(c.op, "0")),
              flush=True)
        try:
            await c.run()
        except (discord.HTTPException, RuntimeError) as e:
            print(color(f"    FAILED: {e}", "31"))
            print("Stopped. Everything done so far is recorded; fix the problem and re-run.")
            return 1
    print(color("Apply complete. Re-run with --plan to confirm there are no changes left.", "1"))
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="show changes (default)")
    mode.add_argument("--apply", action="store_true", help="make the changes")
    ap.add_argument("--yes", action="store_true", help="with --apply, skip the confirmation")
    ap.add_argument("--config", default=str(ROOT / "server.yaml"))
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    try:
        cfg = load_config(args.config)
    except (ConfigError, KeyError) as e:
        print(f"server.yaml error: {e}")
        return 2

    client = discord.Client(intents=discord.Intents(guilds=True))
    result = {"code": 1}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(os.environ["DISCORD_GUILD_ID"]))
            if guild is None:
                print("The bot is not in the configured guild.")
                return
            r = Reconciler(guild, cfg, State())
            await r.build()
            print_plan(guild, r)
            if not args.apply or not r.changes:
                result["code"] = 0
                return
            if not args.yes:
                print()
                answer = await asyncio.to_thread(input, "Apply these changes? Type 'yes': ")
                if answer.strip() != "yes":
                    print("Cancelled.")
                    result["code"] = 0
                    return
            print()
            result["code"] = await apply_plan(r)
        except ConfigError as e:
            print(f"server.yaml error: {e}")
            result["code"] = 2
        except Exception:
            # discord.py would log this to an unconfigured logger, i.e. nowhere.
            traceback.print_exc()
            result["code"] = 1
        finally:
            await client.close()

    await client.start(os.environ["DISCORD_TOKEN"], reconnect=False)
    return result["code"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
