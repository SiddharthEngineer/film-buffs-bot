#!/usr/bin/env python3
"""Reconcile the Film Buffs Discord server toward server.yaml.

    python setup_server.py            # same as --plan: show what would change
    python setup_server.py --apply    # make the changes (asks to confirm)

Safe to re-run. Objects are tracked by key -> Discord ID in data/filmbuffs.db,
so renames in server.yaml rename in Discord instead of creating duplicates.
Unmanaged channels are only reported. The one exception is the `retire:` list
in server.yaml: channels named there by ID are deleted, with safety checks.
"""
from __future__ import annotations

import argparse
import asyncio
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

from filmbuffs.state import ROOT, State

REASON = "Film Buffs setup_server.py"
TEXTLIKE = (discord.ChannelType.text, discord.ChannelType.forum, discord.ChannelType.news)
SORT_ORDERS = {
    "latest_activity": discord.ForumOrderType.latest_activity,
    "creation_date": discord.ForumOrderType.creation_date,
}
TTY = sys.stdout.isatty()


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


OP_STYLE = {"+": "32", "~": "33", ">": "36", "-": "31", "!": "35"}


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------- config

def load_config(path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
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
    channel_keys = {ch["key"] for cat in cfg["categories"] for ch in cat["channels"]}
    for m in cfg.get("messages", []):
        unique(f"message:{m['key']}")
        if m["channel"] not in channel_keys:
            raise ConfigError(f"message {m['key']}: unknown channel {m['channel']!r}")
        for ref in re.findall(r"\{#([^{}]*)\}", m.get("description", "")):
            if ref not in channel_keys:
                hint = ref.replace("-", "_")
                raise ConfigError(
                    f"message {m['key']}: unknown channel ref {{#{ref}}} (use a channel key"
                    + (f", e.g. {{#{hint}}}" if hint in channel_keys else "") + ")")
    for r in cfg.get("retire", []):
        if not isinstance(r.get("id"), int) or not r.get("name"):
            raise ConfigError(f"retire entries need an integer id and a name: {r}")
    return cfg


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
        target_ids = [t.id for t in desired_ows]

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
                kwargs = dict(name=ch["name"], category=category,
                              overwrites=desired_ows, reason=REASON)
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
                self.state.set(key, "channel", c.id, {"targets": target_ids})
            self.add("+", f"#{ch['name']}", details, create)
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
        if self.cfg.get("managed_categories_first", True):
            want = managed_cats + [c for c in cats if c not in managed_cats]
            if [c.id for c in want] != [c.id for c in cats]:
                payload += [{"id": c.id, "position": i} for i, c in enumerate(want)]
                desc.append("categories: " + " | ".join(c.name for c in want))

        for cat in self.cfg["categories"]:
            cat_id = self.state.get(f"category:{cat['key']}")
            if cat_id not in by_id:
                continue
            # Discord sorts text-like and voice channels as separate groups.
            for group in (TEXTLIKE, (discord.ChannelType.voice, discord.ChannelType.stage_voice)):
                inside = sorted((c for c in channels if getattr(c, "category_id", None) == cat_id
                                 and c.type in group), key=key_of)
                managed = [by_id[i] for i in
                           (self.state.get(f"channel:{c['key']}") for c in cat["channels"])
                           if i in by_id and by_id[i] in inside]
                want = managed + [c for c in inside if c not in managed]
                if [c.id for c in want] != [c.id for c in inside]:
                    payload += [{"id": c.id, "position": i} for i, c in enumerate(want)]
                    desc.append(f"{cat['name']}: " + ", ".join(c.name for c in want))
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

    # messages --------------------------------------------------------------
    def render(self, m: dict, live: bool) -> tuple[discord.Embed, str]:
        desc = re.sub(r"\{#(\w+)\}", lambda x: self.channel_ref(x.group(1), live),
                      m.get("description", "").strip())
        embed = discord.Embed(title=m.get("title"), description=desc,
                              color=m.get("color"))
        digest = hashlib.sha256(json.dumps(embed.to_dict(), sort_keys=True)
                                .encode()).hexdigest()
        return embed, digest

    async def plan_message(self, m: dict) -> None:
        key = f"message:{m['key']}"
        ch_key = f"channel:{m['channel']}"
        ch_name = next(c["name"] for cat in self.cfg["categories"]
                       for c in cat["channels"] if c["key"] == m["channel"])
        channel = self.guild.get_channel(self.state.get(ch_key) or 0)
        msg_id = self.state.get(key)
        _, digest = self.render(m, live=False)

        existing = None
        if channel and msg_id:
            try:
                existing = await channel.fetch_message(msg_id)
            except discord.NotFound:
                self.notes.append(f"{key}: message {msg_id} was deleted; it will be reposted")

        async def run():
            ch = self.guild.get_channel(self.state.get(ch_key))
            embed, live_digest = self.render(m, live=True)
            msg = None
            if self.state.get(key):
                try:
                    msg = await ch.fetch_message(self.state.get(key))
                except discord.NotFound:
                    msg = None
            if msg:
                await msg.edit(content=None, embed=embed)
            else:
                msg = await ch.send(embed=embed)
            self.state.set(key, "message", msg.id, {"hash": live_digest})

        if existing is None:
            self.add("+", f'message "{m.get("title", m["key"])}" in #{ch_name}',
                     [first_line(m.get("description", ""))], run)
        elif self.state.get_extra(key).get("hash") != digest:
            self.add("~", f'message "{m.get("title", m["key"])}" in #{ch_name}',
                     ["content changed; edit in place"], run)

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

    # ---------------------------------------------------------------------
    async def build(self) -> None:
        for cat in self.cfg["categories"]:
            self.plan_category(cat)
        for cat in self.cfg["categories"]:
            for ch in cat["channels"]:
                self.plan_channel(cat, ch)
        self.plan_positions()
        for m in self.cfg.get("messages", []):
            await self.plan_message(m)
        await self.plan_retire()

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
          f"{counts['>']} to adopt/record, {counts['-']} to delete (retire list only).")


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
        finally:
            await client.close()

    await client.start(os.environ["DISCORD_TOKEN"], reconnect=False)
    return result["code"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
