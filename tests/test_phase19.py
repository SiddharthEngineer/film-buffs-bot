"""Phase 19: one nomination each (duplicates combine), the #watch-planning board,
picks announced in #watch-log / the group's #chat, ping roles on by default,
names instead of raw mentions, and commands-only channels."""
import asyncio
from types import SimpleNamespace

import discord

import setup_server
from filmbuffs.cogs.members import default_pings, group_ping_changes
from filmbuffs.db import Database
from filmbuffs.names import FALLBACK, join_names, member_name
from filmbuffs.pool import fit_lines
from tests.test_phase10 import FakeChannel, MO, WK, local, make, settle


# ------------------------------------------------------------------ migration 16

def test_migration_16_keeps_one_nomination_each(tmp_path):
    async def run():
        db = Database(tmp_path / "t.db")
        await db.connect()
        await db.conn.executescript("""
            DROP TABLE pool_nominations;
            CREATE TABLE pool_nominations (grp TEXT, user_id INTEGER, rank INTEGER,
                tmdb_id INTEGER, nominated_at TEXT, PRIMARY KEY (grp, user_id, rank),
                UNIQUE (grp, tmdb_id));
            INSERT INTO pool_nominations VALUES
                ('main', 10, 1, 1, 'a'), ('main', 10, 2, 2, 'b'), ('main', 10, 3, 3, 'c'),
                ('main', 11, 1, 4, 'd'), ('main', 11, 2, 5, 'e'),
                ('osu', 10, 1, 6, 'f'), ('osu', 10, 2, 1, 'g');
            -- Film 1 is Up Next, so 10's ballot film is their #2.
            INSERT INTO ballot_entries VALUES ('main', 2, 10, 'x'), ('main', 4, 11, 'x'),
                ('osu', 6, 10, 'x');
            DELETE FROM schema_version WHERE version >= 16;
        """)
        await db.conn.commit()
        await db.close()
        db = Database(tmp_path / "t.db")
        await db.connect()
        main = [(r["user_id"], r["tmdb_id"]) for r in await db.pool()]
        osu = [(r["user_id"], r["tmdb_id"]) for r in await db.pool(grp="osu")]
        cols = [r[1] for r in await (await db.conn.execute(
            "PRAGMA table_info(pool_nominations)")).fetchall()]
        await db.close()
        return main, osu, cols
    main, osu, cols = asyncio.run(run())
    assert sorted(main) == [(10, 2), (11, 4)]
    assert osu == [(10, 6)]
    assert "rank" not in cols


# ------------------------------------------------------------------ the board

def texts(view) -> str:
    out = []

    def walk(item):
        if isinstance(item, discord.ui.TextDisplay):
            out.append(item.content)
        for child in getattr(item, "children", []):
            walk(child)
    for item in view.children:
        walk(item)
    return "\n".join(out)


def buttons(view) -> list[str]:
    ids = []

    def walk(item):
        if isinstance(item, discord.ui.ActionRow):
            ids.extend(c.custom_id for c in item.children)
        for child in getattr(item, "children", []):
            if not isinstance(item, discord.ui.ActionRow):
                walk(child)
    for item in view.children:
        walk(item)
    return ids


class Msg:
    def __init__(self, store, mid):
        self.store, self.id = store, mid

    async def edit(self, **kw):
        if self.id not in self.store.alive:
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
        self.store.edits.append((self.id, kw))


class BoardChannel:
    def __init__(self, alive=()):
        self.id = 5
        self.alive = set(alive)
        self.edits, self.sent = [], []

    def get_partial_message(self, mid):
        return Msg(self, mid)

    async def send(self, **kw):
        mid = 1000 + len(self.sent)
        self.alive.add(mid)
        self.sent.append(kw)
        return SimpleNamespace(id=mid)


def with_names(bot, names):
    bot.get_user = lambda uid: SimpleNamespace(display_name=names[uid]) if uid in names else None


def test_board_lists_every_nominator_and_has_the_buttons(tmp_path, monkeypatch):
    async def run():
        db, bot = await make(tmp_path)
        with_names(bot, {10: "Siddharth", 11: "Nikhil", 12: "Logan_Mart"})
        monkeypatch.setattr("filmbuffs.weekly.datetime", FrozenDT)
        await db.create_week(WK)
        await bot.pool.nominate(10, 2)
        await bot.pool.nominate(11, 2)               # The Departed, twice
        await bot.pool.nominate(12, 3)
        await db.set_track_ranking("weekly", 20, [2])
        view, _ = await bot.weekly.render_board()
        await db.update_week(WK, state="picked")      # no open vote
        closed, _ = await bot.weekly.render_board()
        await settle()
        await db.close()
        return view, closed
    view, closed = asyncio.run(run())
    text = texts(view)
    assert "## 🎬 Weekly vote · watch week Nov 23–29" in text
    assert "**1** member has voted" in text
    assert "### 🎞️ On the ballot (2)" in text
    assert "**[Film 2 (2002)](https://discord.com/channels/1/102)**\n-# nominated by Siddharth and Nikhil" in text
    assert "nominated by Logan\\_Mart" in text       # markdown escaped
    assert "<@" not in text                           # no raw mentions
    assert buttons(view) == [f"vote:{WK}", "noms:show", "pings:main"]
    assert "No vote is open right now." in texts(closed)
    assert buttons(closed) == ["noms:show", "pings:main"]
    assert isinstance(view.children[0], discord.ui.Container)


class FrozenDT(__import__("datetime").datetime):
    @classmethod
    def now(cls, tz=None):
        return local(2026, 11, 20, 12).astimezone(tz)


def test_refresh_board_adopts_the_old_ballot_and_edits_in_place(tmp_path, monkeypatch):
    async def run():
        db, bot = await make(tmp_path)
        monkeypatch.setattr("filmbuffs.weekly.datetime", FrozenDT)
        await db.create_week(WK)
        await db.update_week(WK, ballot_message_id=77)
        ch = BoardChannel(alive={77})
        bot.weekly.board_channel = lambda: ch
        await bot.weekly.refresh_board()              # the old embed ballot becomes the board
        meta1 = await db.get_meta("board_message:main")
        await bot.weekly.refresh_board()              # nothing changed: no edit
        n_edits = len(ch.edits)
        await bot.pool.nominate(10, 1)
        await settle()
        await bot.weekly.refresh_board()
        ch.alive.clear()                              # someone deleted it
        await bot.pool.nominate(11, 3)
        await settle()
        await bot.weekly.refresh_board()
        meta2 = await db.get_meta("board_message:main")
        await db.close()
        return ch, meta1, n_edits, meta2
    ch, meta1, n_edits, meta2 = asyncio.run(run())
    assert meta1 == "77" and n_edits == 1
    first = ch.edits[0]
    assert first[0] == 77 and first[1]["content"] == "" and first[1]["embed"] is None
    assert isinstance(first[1]["view"], discord.ui.LayoutView)
    assert len(ch.sent) == 1 and meta2 == "1000"


def test_fit_lines_falls_back_to_compact_then_truncates():
    rows = [(f"**[Film {i}](url)**\n-# nominated by Someone", f"`{i:>2}.` [Film {i}](url) · Someone")
            for i in range(1, 26)]
    full = fit_lines(rows, 10_000)
    assert full.count("\n-# nominated by") == 25
    compact = fit_lines(rows, 1000)
    assert "-# nominated by" not in compact and compact.count("\n") == 24
    short = fit_lines(rows, 300)
    assert len(short) <= 300 and short.endswith("more: click **Vote** to see them all.")


def test_board_fits_25_long_titles(tmp_path, monkeypatch):
    async def run():
        db, bot = await make(tmp_path, n=30)
        monkeypatch.setattr("filmbuffs.weekly.datetime", FrozenDT)
        await db.conn.execute("UPDATE movies SET status='backlog', title=? ",
                              ("A Very Long Film Title That Goes On and On Forever",))
        await db.conn.commit()
        with_names(bot, {u: "A Long Display Name Here" for u in range(100, 140)})
        await db.create_week(WK)
        for i in range(1, 30):
            await bot.pool.nominate(100 + i, i)
            await bot.pool.nominate(130 + (i % 10), i)
        view, _ = await bot.weekly.render_board()
        await settle()
        await db.close()
        return view
    view = asyncio.run(run())
    assert len(texts(view)) <= 4000


# ------------------------------------------------------------------ announcements

def test_group_pick_and_notices_go_to_its_chat(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        planning, chat = FakeChannel(), FakeChannel()
        bot.osu.group.channels = {"watch_planning": 1, "chat": 2}
        bot.osu.group.ping_role_id = 88
        bot.get_channel = {1: planning, 2: chat}.get
        bot.osu.refresh_board = lambda: asyncio.sleep(0)
        await bot.osu.pool.nominate(10, 1)
        await db.create_month("osu", MO)
        await bot.osu.close_vote(await db.get_month("osu", MO), local(2026, 11, 30, 23, 59))
        await settle()
        await db.close()
        return planning.sent, chat.sent
    planning, chat = asyncio.run(run())
    assert planning == []
    assert chat[0][0].startswith("<@&88> 🌙 **OSU's movie for December 2026")


def test_admin_clear_crosses_out_the_log_line(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        wlog = BoardChannel(alive={901})
        bot.weekly.channel = lambda key: wlog if key == "watch_log" else None
        bot.weekly.refresh_board = lambda: asyncio.sleep(0)
        await db.create_week(WK, state="picked")
        await db.update_week(WK, winner_tmdb_id=3, log_message_id=901)
        await db.set_status(3, "up_next")
        out = await bot.weekly.admin_clear(1, "next", local(2026, 11, 20, 12))
        await db.close()
        return out, wlog.edits
    out, edits = asyncio.run(run())
    assert out.startswith("✅ Cleared **Film 3 (2003)**")
    assert edits[0][0] == 901
    assert edits[0][1]["content"] == "~~Weekly movie · Nov 23–29~~ (Film 3 (2003) was cleared by an admin)"


# ------------------------------------------------------------------ ping roles

def cfg():
    osu = SimpleNamespace(role_id=555, ping_role_id=556)
    return SimpleNamespace(roles={"weekly_ping": 77}, groups={"osu": osu})


def test_default_pings():
    assert default_pings(set(), cfg()) == {77}
    assert default_pings({555}, cfg()) == {77, 556}


def test_group_ping_follows_the_group_role():
    assert group_ping_changes(set(), {555}, cfg()) == ({556}, set())
    assert group_ping_changes(set(), {555, 556}, cfg()) == (set(), set())   # already has it
    assert group_ping_changes({555, 556}, {556}, cfg()) == (set(), {556})
    assert group_ping_changes({1}, {1, 2}, cfg()) == (set(), set())


# ------------------------------------------------------------------ names

def test_names():
    assert join_names(["A"]) == "A" and join_names(["A", "B"]) == "A and B"
    assert join_names(["A", "B", "C"]) == "A, B and C"

    async def run():
        member = SimpleNamespace(display_name="Logan*")
        guild = SimpleNamespace(get_member=lambda uid: member if uid == 1 else None)
        calls = []

        async def fetch_user(uid):
            calls.append(uid)
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
        bot = SimpleNamespace(config=SimpleNamespace(guild_id=9), get_guild=lambda gid: guild,
                              get_user=lambda uid: None, fetch_user=fetch_user)
        return [await member_name(bot, 1), await member_name(bot, 2),
                await member_name(bot, 2)], calls
    names, calls = asyncio.run(run())
    assert names == ["Logan\\*", FALLBACK, FALLBACK] and calls == [2]   # looked up once


# ------------------------------------------------------------------ server.yaml

def test_commands_only_channels():
    c = setup_server.load_config("server.yaml")
    chans = {ch["key"]: ch for cat in c["categories"] for ch in cat["channels"]}
    for key in ("welcome", "watch_planning", "watch_log", "letterboxd_feed"):
        assert chans[key].get("preset") == "read_only", key
    assert c["presets"]["read_only"]["@everyone"]["deny"][0] == "send_messages"
    for key in ("general", "ideas"):
        assert "preset" not in chans[key]
    osu = chans["osu_watch_planning"]["overwrites"]["role:group_osu"]
    assert "send_messages" in osu["deny"] and "view_channel" in osu["allow"]
    assert "deny" not in chans["osu_chat"]["overwrites"]["role:group_osu"]
    welcome = " ".join(next(m for m in c["messages"] if m["key"] == "welcome_rules")["sections"])
    assert "up to 3" not in welcome and "{#watch_log}" in welcome


def test_one_nomination_text_everywhere():
    from pathlib import Path
    for f in ("filmbuffs/weekly.py", "filmbuffs/monthly.py", "filmbuffs/groups.py", "server.yaml"):
        assert "up to 3" not in Path(f).read_text(), f
