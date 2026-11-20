"""Phase 25: nominate / vote reminders to ping-role members who still need to act."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from filmbuffs.reminders import Reminders, due, has_voted, reminder_times
from tests.test_phase10 import TZ, FakeChannel, MO, WK, local, make, open_week


# ------------------------------------------------------------------ pure helpers

def test_weekly_times_are_thursday_and_sunday_9am():
    t = reminder_times(local(2026, 11, 22, 23, 59), TZ)
    assert t["nominate"] == local(2026, 11, 19, 9)       # Thursday
    assert t["vote"] == local(2026, 11, 22, 9)           # Sunday


def test_monthly_times_follow_the_month_length():
    for close, nom, vote in [((2026, 11, 30), (2026, 11, 27), (2026, 11, 30)),
                             ((2026, 12, 31), (2026, 12, 28), (2026, 12, 31)),
                             ((2027, 1, 31), (2027, 1, 28), (2027, 1, 31)),
                             ((2027, 2, 28), (2027, 2, 25), (2027, 2, 28))]:
        t = reminder_times(local(*close, 23, 59), TZ)
        assert t["nominate"] == local(*nom, 9) and t["vote"] == local(*vote, 9)


def test_due_window():
    at, close = local(2026, 11, 22, 9), local(2026, 11, 22, 23, 59)
    assert not due(at, close, at - timedelta(minutes=1))
    assert due(at, close, at)
    assert due(at, close, at + timedelta(hours=11))
    assert not due(at, close, at + timedelta(hours=13))        # too late to catch up
    nom = local(2026, 11, 19, 9)
    assert not due(nom, close, nom + timedelta(days=1))        # a deploy on Friday sends nothing


def test_has_voted():
    assert has_voted([1, 2, 3], [1, 2, 3, 4, 5])
    assert not has_voted([1, 2], [1, 2, 3, 4])
    assert not has_voted([9, 1, 2], [1, 2, 3])                 # 9 left the ballot
    assert has_voted([2, 1], [1, 2])                           # fewer than 3: rank them all
    assert not has_voted([1], [1, 2])
    assert has_voted([], [])                                   # empty ballot: nothing to do


# ------------------------------------------------------------------ the service

def member(uid, *roles, bot=False):
    return SimpleNamespace(id=uid, bot=bot, roles=[SimpleNamespace(id=r) for r in roles])


def guild(roles: dict[int, list]):
    return SimpleNamespace(get_role=lambda rid: SimpleNamespace(members=roles[rid])
                           if rid in roles else None)


def mentioned(sent):
    return [[u.id for u in am.users] for _, am in sent]


def test_weekly_reminders(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        general = FakeChannel()
        bot.config.roles = {"weekly_ping": 77}
        # 10, 11, 12 nominated (open_week); 13 and 14 didn't; 15 has pings off; 16 is a bot.
        bot.get_guild = lambda gid: guild({77: [member(u, 77) for u in (10, 11, 12, 13, 14)]
                                           + [member(16, 77, bot=True)]})
        bot.weekly.channel = lambda key: general if key == "general" else None
        bot.weekly.voting_period = lambda: asyncio.sleep(0, WK)
        bot.weekly.board_url = lambda: asyncio.sleep(0, "https://board")
        await open_week(db, bot)                               # ballot: 1, 3, 4
        await db.set_track_ranking("weekly", 10, [1, 3, 4])
        await db.set_track_ranking("weekly", 11, [3, 1])       # only two of three
        await db.set_track_ranking("weekly", 13, [4, 3, 1])
        r = Reminders(bot.weekly)
        await r.tick(local(2026, 11, 19, 8, 59))               # not yet
        n0 = len(general.sent)
        await r.tick(local(2026, 11, 19, 9))
        await r.tick(local(2026, 11, 19, 9, 1))                # sent once only
        n1 = len(general.sent)
        await r.tick(local(2026, 11, 22, 9, 30))
        await r.tick(local(2026, 11, 22, 10))
        await db.close()
        return n0, n1, general.sent
    n0, n1, sent = asyncio.run(run())
    assert n0 == 0 and n1 == 1
    assert mentioned(sent) == [[13, 14], [11, 12, 14]]
    nom, vote = sent[0][0], sent[1][0]
    assert nom.startswith("📋 <@13> <@14>\nYou haven't nominated") and "https://board" in nom
    assert vote.startswith("🗳️ <@11> <@12> <@14>\nLast day to vote")
    assert "Sun, Nov 22" in vote or "Nov 22" in vote
    assert all(am.roles is False and am.everyone is False for _, am in sent)


def test_group_reminders_need_the_group_role(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        chat = FakeChannel()
        osu = bot.osu
        osu.group.ping_role_id = 556
        bot.get_guild = lambda gid: guild({556: [member(30, 555, 556), member(31, 555, 556),
                                                 member(32, 556)]})   # 32 lost the group role
        osu.channel = lambda part: None
        r = Reminders(osu)
        r.channel = lambda: chat
        osu.voting_period = lambda: asyncio.sleep(0, MO)
        osu.board_url = lambda: asyncio.sleep(0, "")
        await db.create_month("osu", MO)
        await osu.pool.nominate(30, 2)
        await r.tick(local(2026, 11, 27, 9))
        await r.tick(local(2026, 11, 30, 9))                  # 30 hasn't ranked; 31 neither
        await db.close()
        return chat.sent
    sent = asyncio.run(run())
    assert mentioned(sent) == [[31], [30, 31]]
    assert "OSU's monthly vote" in sent[0][0]


def test_vote_reminder_skips_an_empty_ballot(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        general = FakeChannel()
        bot.config.roles = {"weekly_ping": 77}
        bot.get_guild = lambda gid: guild({77: [member(10, 77)]})
        bot.weekly.channel = lambda key: general
        bot.weekly.voting_period = lambda: asyncio.sleep(0, WK)
        bot.weekly.board_url = lambda: asyncio.sleep(0, "")
        await db.create_week(WK)
        await Reminders(bot.weekly).tick(local(2026, 11, 22, 9))
        await db.close()
        return general.sent
    assert asyncio.run(run()) == []


def test_reminders_are_deleted_when_the_vote_closes(tmp_path):
    async def run():
        db, bot = await make(tmp_path)
        general = FakeChannel(id=5)
        bot.get_channel = {5: general}.get
        bot.config.roles = {"weekly_ping": 77}
        bot.get_guild = lambda gid: guild({77: [member(13, 77)]})
        bot.weekly.channel = lambda key: general
        bot.weekly.voting_period = lambda: asyncio.sleep(0, WK)
        bot.weekly.board_url = lambda: asyncio.sleep(0, "")
        await open_week(db, bot)
        r = Reminders(bot.weekly)
        await r.tick(local(2026, 11, 19, 9))
        await r.tick(local(2026, 11, 22, 9))
        await r.tick(local(2026, 11, 22, 23, 58))           # still open: kept
        kept = list(general.deleted)
        bot.weekly.voting_period = lambda: asyncio.sleep(0, None)
        await r.tick(local(2026, 11, 22, 23, 59))           # closed
        left = await db.get_meta("reminder_messages:main")
        await r.tick(local(2026, 11, 23, 0, 1))             # nothing twice
        await db.close()
        return kept, general.deleted, left
    kept, deleted, left = asyncio.run(run())
    assert kept == [] and deleted == [901, 902] and left is None


def test_group_pick_goes_to_its_watch_log(tmp_path):
    """Phase 26: a group's picks go to its own #watch-log, not its #chat."""
    async def run():
        db, bot = await make(tmp_path)
        planning, wlog, chat = FakeChannel(), FakeChannel(), FakeChannel()
        bot.osu.group.channels = {"watch_planning": 1, "watch_log": 2, "chat": 3}
        bot.osu.group.ping_role_id = 88
        bot.get_channel = {1: planning, 2: wlog, 3: chat}.get
        bot.osu.refresh_board = lambda: asyncio.sleep(0)
        await bot.osu.pool.nominate(10, 1)
        await db.create_month("osu", MO)
        await bot.osu.close_vote(await db.get_month("osu", MO), local(2026, 11, 30, 23, 59))
        await db.close()
        return planning.sent, wlog.sent, chat.sent
    planning, wlog, chat = asyncio.run(run())
    assert planning == [] and chat == []
    assert wlog[0][0].startswith("<@&88> 🌙 **OSU's movie for December 2026")


def test_channel_positions_are_one_sequence_across_categories():
    """Phase 26: the live positions that per-category numbering couldn't fix."""
    import discord
    import setup_server

    def cat(cid, pos, name):
        c = object.__new__(discord.CategoryChannel)
        c.id, c.position, c.name = cid, pos, name
        return c

    T, V = discord.ChannelType.text, discord.ChannelType.voice
    ch = lambda cid, cat_id, pos, name, t=T: SimpleNamespace(id=cid, category_id=cat_id,
                                                            position=pos, name=name, type=t)
    cats = [cat(1, 0, "🎬 Film Buffs"), cat(2, 1, "📚 More"), cat(3, 2, "🏈 OSU/Canton")]
    chans = cats + [
        ch(10, 1, 0, "welcome"), ch(11, 1, 1, "general"), ch(14, 1, 1, "watch-log"),
        ch(12, 1, 2, "watch-planning"), ch(13, 1, 0, "Voice", V),
        ch(20, 2, 0, "letterboxd-feed"), ch(21, 2, 2, "ideas"),
        ch(30, 3, 11, "watch-planning"), ch(31, 3, 12, "chat"), ch(33, 3, 14, "watch-log"),
        ch(32, 3, 2, "voice", V), ch(99, None, 0, "server-hub")]
    cfg = {"managed_categories_first": True, "categories": [
        {"key": "info", "name": "🎬 Film Buffs", "channels": [
            {"key": "welcome"}, {"key": "general"}, {"key": "watch_planning"},
            {"key": "watch_log"}, {"key": "watch_party"}]},
        {"key": "recap", "name": "📚 More", "channels": [{"key": "letterboxd_feed"},
                                                         {"key": "ideas"}]},
        {"key": "group_osu", "name": "🏈 OSU/Canton", "channels": [
            {"key": "osu_watch_planning"}, {"key": "osu_watch_log"}, {"key": "osu_chat"},
            {"key": "osu_voice"}]}]}
    ids = {"category:info": 1, "category:recap": 2, "category:group_osu": 3,
           "channel:welcome": 10, "channel:general": 11, "channel:watch_planning": 12,
           "channel:watch_log": 14, "channel:watch_party": 13, "channel:letterboxd_feed": 20,
           "channel:ideas": 21, "channel:osu_watch_planning": 30, "channel:osu_watch_log": 33,
           "channel:osu_chat": 31, "channel:osu_voice": 32}
    r = object.__new__(setup_server.Reconciler)
    r.cfg, r.state = cfg, SimpleNamespace(get=ids.get)
    payload, _ = r.desired_positions(chans)
    pos = {p["id"]: p["position"] for p in payload}
    for c in chans:
        if c.id in pos:
            c.position = pos[c.id]
    order = lambda cid: [c.name for c in sorted(
        (c for c in chans if getattr(c, "category_id", "x") == cid and c.type == T),
        key=lambda c: (c.position, c.id))]
    assert order(1) == ["welcome", "general", "watch-planning", "watch-log"]
    assert order(3) == ["watch-planning", "watch-log", "chat"]
    texts = sorted(pos[c.id] for c in chans if c.id in pos and getattr(c, "type", None) == T)
    assert texts == list(range(len(texts)))                     # one server-wide sequence
    assert r.desired_positions(chans)[0] == []                  # converged
