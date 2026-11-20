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
