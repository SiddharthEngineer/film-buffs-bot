"""Phase 14: the #ideas list (DB, rendering, the pinned message) and the 📊 Recap layout."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import discord
import pytest

from filmbuffs.cogs.admin import NotAdmin, admin_only
from filmbuffs.cogs.ideas import Ideas, add_refusal
from filmbuffs.db import MIGRATIONS, Database
from filmbuffs.ideas import (CHOICE_MAX, DESCRIPTION_MAX, EMPTY, META_LIST, TEXT_MAX,
                             IdeasService, choice_name, clean_text, idea_line, render_list)

GUILD = 100
IDEAS_CH = 200


async def opened(tmp_path) -> Database:
    db = Database(tmp_path / "t.db")
    await db.connect()
    return db


def msg(mid=1, channel=IDEAS_CH, author=7, bot=False, webhook=None,
        type_=discord.MessageType.default):
    return SimpleNamespace(id=mid, channel=SimpleNamespace(id=channel),
                           author=SimpleNamespace(id=author, bot=bot), webhook_id=webhook,
                           type=type_)


def idea(i, text="An idea", by=7, message=True):
    return {"id": i, "text": text, "suggested_by": by,
            "channel_id": IDEAS_CH if message else None, "message_id": 1000 + i if message else None}


# ------------------------------------------------------------------ DB

def test_migration_seeds_planned_ideas(tmp_path):
    async def run():
        db = await opened(tmp_path)
        rows = await db.active_ideas()
        # Replaying the migration (as the old-schema tests do) neither fails nor re-seeds.
        sql = next(m for m in MIGRATIONS if "CREATE TABLE IF NOT EXISTS ideas" in m)
        await db.conn.executescript(sql)
        again = await db.active_ideas()
        await db.close()
        assert len(again) == len(rows)
        return rows
    rows = asyncio.run(run())
    assert [r["id"] for r in rows] == [1, 2, 3, 4]
    texts = " | ".join(r["text"] for r in rows)
    assert len(rows) == 4
    for word in ("Stats page", "/me", "Wrapped", "Off-machine backups"):
        assert word in texts
    assert all(r["suggested_by"] is None and r["message_id"] is None for r in rows)


def test_add_close_and_duplicates(tmp_path):
    async def run():
        db = await opened(tmp_path)
        a = await db.add_idea(text="Trivia night", suggested_by=7, channel_id=IDEAS_CH,
                              message_id=55, added_by=1)
        with pytest.raises(aiosqlite.IntegrityError):
            await db.add_idea(text="again", suggested_by=7, channel_id=IDEAS_CH,
                              message_id=55, added_by=1)
        by_msg = await db.get_idea_by_message(55)
        active = [r["id"] for r in await db.active_ideas()]
        done = await db.close_idea(a, "done", 1)
        again = await db.close_idea(a, "dropped", 1)
        row = await db.get_idea(a)
        with pytest.raises(ValueError):
            await db.close_idea(1, "active", 1)
        after = [r["id"] for r in await db.active_ideas()]
        await db.close()
        return a, by_msg, active, done, again, row, after
    a, by_msg, active, done, again, row, after = asyncio.run(run())
    assert by_msg["id"] == a and by_msg["status"] == "active"
    assert active[-1] == a                       # oldest first; the seeds come before it
    assert done is True and again is False       # only active ideas can be closed
    assert row["status"] == "done" and row["closed_by"] == 1 and row["closed_at"]
    assert a not in after and len(after) == 4


# ------------------------------------------------------------------ rendering

def test_clean_text():
    assert clean_text("Movie\n\n  [night]  *please*") == r"Movie (night) \*please\*"
    long = clean_text("x" * 500)
    assert len(long) == TEXT_MAX and long.endswith("…")
    # A cut never leaves a dangling escape before the ellipsis.
    assert not clean_text("*" * 300).endswith("\\…")


def test_idea_lines_link_to_messages():
    line = idea_line(idea(3, "Trivia night"), GUILD)
    assert line == f"• [Trivia night](https://discord.com/channels/{GUILD}/{IDEAS_CH}/1003) · <@7>"
    planned = idea_line(idea(1, "Wrapped", by=None, message=False), GUILD)
    assert planned == "• Wrapped · 🛠️ planned"


def test_render_empty_and_small():
    empty = render_list([], GUILD)
    assert EMPTY in empty.description
    e = render_list([idea(1), idea(2, by=None, message=False)], GUILD)
    assert e.description.count("\n•") == 2 and "more" not in e.description
    assert e.footer.text == "2 active ideas"


def test_render_stays_within_discord_limits():
    many = [idea(i, clean_text("word " * 100)) for i in range(1, 200)]
    e = render_list(many, GUILD)
    d = e.description
    assert len(d) <= DESCRIPTION_MAX
    shown = d.count("\n•")
    assert 0 < shown < len(many)
    assert d.endswith(f"…and {len(many) - shown} more")
    assert len(e) <= 6000                         # all embed text in one message
    assert e.footer.text == f"{len(many)} active ideas"
    # Exactly at the edge: whichever line is last, the note still fits.
    for n in range(20, 40):
        assert len(render_list(many[:n], GUILD).description) <= DESCRIPTION_MAX


def test_choice_names_fit_autocomplete():
    name = choice_name({"id": 12, "text": clean_text("*bold* " * 50)})
    assert len(name) <= CHOICE_MAX and name.startswith("#12 ") and "\\" not in name


# ------------------------------------------------------------------ admin checks

def test_add_refusals():
    assert add_refusal(msg(), IDEAS_CH, None) is None
    assert "isn't set up" in add_refusal(msg(), None, None)
    assert f"<#{IDEAS_CH}>" in add_refusal(msg(channel=999), IDEAS_CH, None)
    assert "ideas list itself" in add_refusal(msg(mid=5, bot=True), IDEAS_CH, 5)
    assert "members' messages" in add_refusal(msg(bot=True), IDEAS_CH, None)
    assert "members' messages" in add_refusal(msg(webhook=9), IDEAS_CH, None)
    assert "members' messages" in add_refusal(
        msg(type_=discord.MessageType.pins_add), IDEAS_CH, None)


def member(manage_guild=False):
    perms = SimpleNamespace(manage_guild=manage_guild, administrator=False)
    return SimpleNamespace(guild_permissions=perms, roles=[])


def interaction_for(user):
    return SimpleNamespace(user=user, client=SimpleNamespace(
        config=SimpleNamespace(admin_role=None)))


def run_checks(checks, user):
    async def run():
        for c in checks:
            await c(interaction_for(user))
    asyncio.run(run())


def test_idea_commands_are_admin_only():
    added = []
    Ideas(SimpleNamespace(tree=SimpleNamespace(add_command=added.append)))
    ctx = added[0]
    assert ctx.name == "Add to ideas" and ctx.type is discord.AppCommandType.message
    assert ctx.default_permissions == discord.Permissions(manage_guild=True)
    assert ctx.guild_only
    from filmbuffs.cogs.admin import Admin
    group = Admin(SimpleNamespace()).__cog_app_commands_group__
    subs = group.get_command("idea")
    assert {c.name for c in subs.commands} == {"done", "drop"}
    for cmd in [ctx, *subs.commands]:
        with pytest.raises(NotAdmin):
            run_checks(cmd.checks, member())
        run_checks(cmd.checks, member(manage_guild=True))
    # No member-facing /idea add (owner, 2026-11-15).
    assert group.get_command("add") is None and subs.get_command("add") is None
    assert admin_only  # the shared gate is reused, not copied


# ------------------------------------------------------------------ pinned message

class FakeMessage:
    def __init__(self, mid, embed):
        self.id, self.embed, self.pinned, self.edits = mid, embed, False, 0

    async def pin(self, reason=None):
        self.pinned = True


class FakePartial:
    def __init__(self, ch, mid):
        self.ch, self.mid = ch, mid

    async def edit(self, embed):
        m = self.ch.messages.get(self.mid)
        if m is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "Unknown Message")
        m.embed, m.edits = embed, m.edits + 1


class FakeChannel(discord.TextChannel):
    def __init__(self):            # no Discord state; only what IdeasService uses
        self.messages: dict[int, FakeMessage] = {}
        self.next_id = 900

    async def send(self, embed, allowed_mentions=None):
        self.next_id += 1
        m = self.messages[self.next_id] = FakeMessage(self.next_id, embed)
        return m

    def get_partial_message(self, mid):
        return FakePartial(self, mid)

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "Unknown Message")
        return self.messages[mid]


def test_list_message_is_posted_pinned_and_edited_in_place(tmp_path):
    async def run():
        db = await opened(tmp_path)
        ch = FakeChannel()
        bot = SimpleNamespace(db=db, config=SimpleNamespace(channels={"ideas": IDEAS_CH},
                                                            guild_id=GUILD),
                              get_channel=lambda cid: ch if cid == IDEAS_CH else None,
                              get_user=lambda uid: SimpleNamespace(display_name=f"user_{uid}"))
        svc = IdeasService(bot)
        await svc.refresh()                                  # startup: post + pin
        first = int(await db.get_meta(META_LIST))
        await svc.refresh()                                  # nothing changed: no edit
        edits_idle = ch.messages[first].edits
        author = SimpleNamespace(id=7)
        source = SimpleNamespace(id=55, channel=SimpleNamespace(id=IDEAS_CH), author=author)
        new_id = await svc.add(source, "Trivia\nnight", by=1)
        edits_added = ch.messages[first].edits
        text_added = ch.messages[first].embed.description
        await svc.close(new_id, "done", by=1)
        text_closed = ch.messages[first].embed.description
        del ch.messages[first]                               # someone deleted it
        await svc.close(1, "dropped", by=1)
        second = int(await db.get_meta(META_LIST))
        await db.close()
        return ch, first, edits_idle, edits_added, text_added, text_closed, second
    ch, first, edits_idle, edits_added, text_added, text_closed, second = asyncio.run(run())
    assert edits_idle == 0 and edits_added == 1
    assert f"[Trivia night](https://discord.com/channels/{GUILD}/{IDEAS_CH}/55) · user\\_7" in text_added   # a name, not a mention
    assert "Trivia" not in text_closed
    assert second != first and ch.messages[second].pinned
    assert "Stats page" not in ch.messages[second].embed.description   # idea 1 dropped


def test_refresh_without_channel_is_a_no_op(tmp_path):
    async def run():
        db = await opened(tmp_path)
        bot = SimpleNamespace(db=db, config=SimpleNamespace(channels={}, guild_id=GUILD),
                              get_channel=lambda cid: None)
        await IdeasService(bot).refresh()
        meta = await db.get_meta(META_LIST)
        await db.close()
        return meta
    assert asyncio.run(run()) is None


# ------------------------------------------------------------------ server.yaml

def test_recap_category_layout():
    import setup_server
    cfg = setup_server.load_config("server.yaml")
    cats = [c["key"] for c in cfg["categories"]]
    # Phase 18: 📚 More (key recap) above 🔧 Admin; private group categories go between them.
    assert cats.index("movies") < cats.index("recap") < cats.index("admin")
    assert all(cats.index("recap") < cats.index(k) < cats.index("admin")
               for k in cats if k.startswith("group_"))
    recap = next(c for c in cfg["categories"] if c["key"] == "recap")
    assert recap["name"] == "📚 More"
    assert [c["key"] for c in recap["channels"]] == ["letterboxd_feed", "watch_log", "ideas"]
    ideas = recap["channels"][-1]
    assert ideas["name"] == "ideas"
    assert "@everyone" not in ideas["_overwrites"]            # members can post
    assert "pin_messages" in ideas["_overwrites"]["@bot"]["allow"]


def test_positions_put_recap_above_admin(tmp_path):
    """desired_positions moves a newly created 📊 Recap into its declared slot."""
    import setup_server
    cfg = setup_server.load_config("server.yaml")
    keys = [c["key"] for c in cfg["categories"]]
    ids = {k: 10 + i for i, k in enumerate(keys)}
    names = {c["key"]: c["name"] for c in cfg["categories"]}

    def cat(key, pos):
        c = object.__new__(discord.CategoryChannel)
        c.id, c.position, c.name = ids[key], pos, names[key]
        return c

    # Live order: every managed category in place, except Recap, created last at the bottom.
    live = [k for k in keys if k != "recap"] + ["recap"]
    channels = [cat(k, i) for i, k in enumerate(live)]
    state = SimpleNamespace(get=lambda key: ids.get(key.removeprefix("category:"))
                            if key.startswith("category:") else None)
    r = object.__new__(setup_server.Reconciler)
    r.cfg, r.state = cfg, state
    payload, desc = r.desired_positions(channels)
    order = [p["id"] for p in sorted(payload, key=lambda p: p["position"])]
    assert order == [ids[k] for k in keys]
    assert desc[0].endswith("🍿 Movies | 📚 More | 🎓 OSU | 🔧 Admin")
