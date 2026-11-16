"""Phase 15: the #welcome page (Components V2), pin, retire_messages, /help."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

import setup_server
from filmbuffs.help import help_text
from filmbuffs.state import State


# ------------------------------------------------------------------ server.yaml
def test_welcome_is_one_pinned_page():
    cfg = setup_server.load_config("server.yaml")
    welcome = [m for m in cfg["messages"] if m["channel"] == "welcome"]
    assert [m["key"] for m in welcome] == ["welcome_rules"]       # same key: converted in place
    page = welcome[0]
    assert page["pin"] is True and not page.get("title") and not page.get("description")
    assert page["sections"][0].startswith("# 🎬 Welcome to Film Buffs")
    assert setup_server.page_text_size(page["sections"]) <= setup_server.PAGE_TEXT_LIMIT
    text = "\n".join(page["sections"])
    for ref in ("{#movies}", "{#movie_spoilers}", "{#watch_planning}"):
        assert ref in text
    assert "/help" in text and "Groups" not in text and "pitch" not in text
    assert "Recap" not in text and "watchlist and entire review history" in text
    assert cfg["retire_messages"] == [{"key": "how_to", "title": "📖 How to", "channel": "welcome"}]
    ch = next(c for cat in cfg["categories"] for c in cat["channels"] if c["key"] == "welcome")
    assert ch["name"] == "welcome" and ch["preset"] == "read_only"


def _cfg_with(tmp_path, old: str, new: str):
    src = Path("server.yaml").read_text()
    assert old in src
    p = tmp_path / "s.yaml"
    p.write_text(src.replace(old, new, 1))
    return p


@pytest.mark.parametrize("old,new,err", [
    ("    pin: true\n    sections:", "    pin: true\n    title: x\n    sections:", "has no title"),
    ("        Disagree about movies, not about people.",
     "        Disagree about movies, not about people." + "x" * 3000, "Discord allows 4000"),
    ("{#watch_planning}", "{#watch-planning}", "unknown channel ref"),
    ('channel: welcome}', 'channel: nowhere}', "need a key, a title and a channel"),
    ("retire_messages:\n  - {key: how_to", "retire_messages:\n  - {key: welcome_rules", "both declared and retired"),
])
def test_page_config_errors(tmp_path, old, new, err):
    with pytest.raises(setup_server.ConfigError, match=err):
        setup_server.load_config(_cfg_with(tmp_path, old, new))


def test_page_size_counts_mentions_at_full_length():
    assert setup_server.page_text_size(["ab {#x} cd"]) == 6 + setup_server.MENTION_LEN


def test_build_page_layout():
    view = setup_server.build_page(["# A", "## B", "## C"], 0xE5A00D)
    assert view.has_components_v2()
    (container,) = view.to_components()
    assert container["type"] == 17 and container["accent_color"] == 0xE5A00D
    kinds = [c["type"] for c in container["components"]]
    assert kinds == [10, 14, 10, 14, 10]                  # text, divider, text, divider, text
    assert container["components"][2]["content"] == "## B"


# ---------------------------------------------------------- plan with fakes
class FakeMsg:
    def __init__(self, mid, channel, *, title=None, v2=False, pinned=False):
        self.id, self.channel, self.pinned = mid, channel, pinned
        self.embeds = [discord.Embed(title=title)] if title else []
        self.flags = SimpleNamespace(components_v2=v2)
        self.deleted, self.edits = False, []

    async def delete(self):
        self.deleted = True

    async def edit(self, **kw):
        self.edits.append(kw)
        return self

    async def pin(self, reason=None):
        self.pinned = True


class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name, self.messages = cid, name, {}

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
        return self.messages[mid]


class FakeGuild:
    def __init__(self, *channels):
        self.channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self.channels.get(cid)


def _reconciler(tmp_path, msgs):
    cfg = setup_server.load_config("server.yaml")
    ch = FakeChannel(10, "welcome")
    state = State(tmp_path / "state.db")
    state.set("channel:welcome", "channel", 10)
    guild = FakeGuild(ch)
    keys = [c["key"] for cat in cfg["categories"] for c in cat["channels"] if c["key"] != "welcome"]
    for i, k in enumerate(keys, start=100):          # every channel exists, for {#key} links
        guild.channels[i] = FakeChannel(i, k)
        state.set(f"channel:{k}", "channel", i)
    for key, msg in msgs.items():
        ch.messages[msg.id] = msg
        msg.channel = ch
        state.set(f"message:{key}", "message", msg.id, {"hash": "old"})
    return setup_server.Reconciler(guild, cfg, state), state


def test_plan_converts_embed_welcome_in_place_and_pins(tmp_path):
    old = FakeMsg(1, None, title="🎬 Welcome to Film Buffs")
    r, state = _reconciler(tmp_path, {"welcome_rules": old})
    page = next(m for m in r.cfg["messages"] if m["key"] == "welcome_rules")
    asyncio.run(r.plan_message(page))
    (change,) = r.changes
    assert change.op == "~" and "Welcome to Film Buffs" in change.what
    assert "embed → Components V2 page (same message)" in change.details and "pin it" in change.details
    asyncio.run(change.run())
    (edit,) = old.edits
    assert edit["content"] == "" and edit["embed"] is None and edit["view"].has_components_v2()
    assert old.pinned and state.get("message:welcome_rules") == 1
    # Up to date now: nothing more to do.
    old.flags.components_v2 = True
    r.changes.clear()
    asyncio.run(r.plan_message(page))
    assert r.changes == []


def test_retire_messages_deletes_matching_title_only(tmp_path):
    how = FakeMsg(2, None, title="📖 How to")
    r, state = _reconciler(tmp_path, {"how_to": how})
    asyncio.run(r.plan_retire_messages())
    (change,) = r.changes
    assert change.op == "-" and "📖 How to" in change.what
    asyncio.run(change.run())
    assert how.deleted and state.get("message:how_to") is None

    other = FakeMsg(3, None, title="Something else")
    (tmp_path / "b").mkdir()
    r, _ = _reconciler(tmp_path / "b", {"how_to": other})
    with pytest.raises(setup_server.ConfigError, match="refusing to delete"):
        asyncio.run(r.plan_retire_messages())
    assert not other.deleted


def test_retire_messages_forgets_a_message_already_gone(tmp_path):
    r, state = _reconciler(tmp_path, {})
    state.set("message:how_to", "message", 99)
    asyncio.run(r.plan_retire_messages())
    (change,) = r.changes
    assert change.op == ">"
    asyncio.run(change.run())
    assert state.get("message:how_to") is None


# ----------------------------------------------------------------------- /help
async def _cb(interaction):  # pragma: no cover - never called
    pass


async def _menu(interaction: discord.Interaction, message: discord.Message):  # pragma: no cover
    pass


def _commands():
    nominate = app_commands.Command(name="nominate", description="Put a film up for the vote", callback=_cb)
    lb = app_commands.Group(name="letterboxd", description="Letterboxd")
    lb.add_command(app_commands.Command(name="link", description="Link your account", callback=_cb))
    admin = app_commands.Group(name="admin", description="Admin",
                               default_permissions=discord.Permissions(manage_guild=True))
    admin.add_command(app_commands.Command(name="pick", description="Set a movie", callback=_cb))
    mystery = app_commands.Command(name="zzz", description="Something new", callback=_cb)
    spoiler_menu = app_commands.ContextMenu(name="Mark as spoiler", callback=_menu)
    ideas_menu = app_commands.ContextMenu(name="Add to ideas", callback=_menu)
    ideas_menu.default_permissions = discord.Permissions(manage_guild=True)
    return [nominate, lb, admin, mystery, spoiler_menu, ideas_menu]


def test_help_for_members_hides_admin_commands():
    text = help_text(_commands(), {"nominate": 11, "letterboxd": 22}, admin=False, welcome_channel_id=5)
    assert "</nominate:11>: put a film up for the vote" in text
    assert "</letterboxd link:22>: link your account" in text
    assert "Apps → Mark as spoiler" in text
    assert "admin" not in text and "Add to ideas" not in text and "🔧 Admin" not in text
    assert "📚 More" in text and "**/zzz**" in text              # unlisted commands still show
    assert "group's monthly vote" in text                         # under Picking movies
    assert text.endswith("<#5>.")
    assert text.index("🎬 Movies") < text.index("🗳️ Picking") < text.index("📒 Letterboxd")


def test_help_for_admins_lists_admin_section():
    text = help_text(_commands(), {}, admin=True, welcome_channel_id=None)
    assert "🔧 Admin" in text and "**/admin pick**: set a movie" in text
    assert "Apps → Add to ideas" in text and text.endswith("#welcome.")


def test_help_fits_and_covers_the_real_bot_commands():
    """Every member command the real cogs declare shows in /help, and it fits an embed.
    (The /admin group cog and the context menus, added in cog __init__, are covered above.)"""
    import importlib
    from discord.ext import commands as ext_commands
    from filmbuffs.bot import EXTENSIONS

    cmds = []
    for ext in EXTENSIONS:
        mod = importlib.import_module(ext)
        for cls in vars(mod).values():
            if (isinstance(cls, type) and issubclass(cls, ext_commands.Cog)
                    and cls.__module__ == ext and not cls.__cog_is_app_commands_group__):
                cmds += cls.__cog_app_commands__
    names = {c.name for c in cmds}
    assert {"help", "nominate", "nominations", "unnominate", "schedule", "letterboxd",
            "watchlist", "addmovie", "spoiler"} <= names
    text = help_text(cmds, {}, admin=True, welcome_channel_id=1)
    assert len(text) <= 4096 and "📚 More" not in text       # every command has a section
    for n in names - {"help"}:                 # /help doesn't list itself
        assert f"/{n}" in text
