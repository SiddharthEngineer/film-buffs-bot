"""Buttons and private pickers for the ranked votes and nominations (Phases 5R, 10).

Each ballot message (🎬 weekly in #watch-planning, 🌙 monthly in each private
group's #watch-planning) has a 🗳️ Vote button, a 📋 My nominations button and
a 🔔 Pings button (toggles @Weekly Movie, or the group's opt-in ping role). Vote sends the member a private
(ephemeral) picker: one dropdown with the films they haven't ranked yet, so
each pick becomes their next rank, plus Undo last / Clear. Discord can't fit
a dropdown per rank (5 per message) and doesn't promise multi-select values
come in click order, hence one pick at a time. Every change is saved straight
away.

All components are DynamicItems keyed by the vote: a main watch week
(`vote:2026-11-23`) or a group's month (`vote:osu:2026-12`), so they keep
working after a restart; state lives in the DB, not the view. Nomination
buttons carry the group the same way (`noms:show`, `noms:osu:show`). Group
components only work for members with the group's role. Old main monthly
buttons (`vote:2026-12`, Phase 10) just say the vote is closed. The role buttons on the How to
page (`role:weekly_ping`) are posted by setup_server.py and handled here.
"""
from __future__ import annotations

import logging
import re

import discord

from .pool import ordinal

log = logging.getLogger(__name__)

GRP = r"(?:(?P<grp>[a-z][a-z0-9]*):)?"
PERIOD = GRP + r"(?P<period>\d{4}-\d{2}(?:-\d{2})?)"


def vote_id(match: re.Match[str]) -> str:
    return f"{match['grp']}:{match['period']}" if match["grp"] else match["period"]


def split_vote(vid: str) -> tuple[str | None, str]:
    grp, _, period = vid.rpartition(":")
    return grp or None, period


def service(client, vid: str):
    """The vote service for a vote id: a main week (YYYY-MM-DD) or a group's
    month (key:YYYY-MM). None for a vote that no longer exists."""
    grp, period = split_vote(vid)
    if grp:
        return getattr(client, "group_votes", {}).get(grp)
    return client.weekly if len(period) == 10 else None


def pool_for(client, grp: str | None):
    if grp and grp != "main":
        svc = getattr(client, "group_votes", {}).get(grp)
        return svc.pool if svc else None
    return client.pool


def in_group(client, user, grp: str | None) -> bool:
    """Group votes and nominations are only for members with the group's role."""
    if not grp or grp == "main":
        return True
    g = client.config.groups.get(grp)
    return g is not None and any(r.id == g.role_id for r in getattr(user, "roles", []))


async def check(interaction: discord.Interaction, grp: str | None, svc) -> bool:
    if svc is None:
        await interaction.response.send_message("That vote is closed.", ephemeral=True)
        return False
    if not in_group(interaction.client, interaction.user, grp):
        await interaction.response.send_message("🔒 That's only for members of this group.",
                                                ephemeral=True)
        return False
    return True


async def send_private(interaction: discord.Interaction, text: str,
                       view: discord.ui.View | None) -> None:
    """Ephemeral reply with an optional view. discord.py's send_message crashes on
    view=None (it calls view.is_finished()), unlike edit_message, where None
    removes the components; the pickers return None when there's nothing to click
    (no nominations, nothing left to rank)."""
    kwargs = {"view": view} if view is not None else {}
    await interaction.response.send_message(text, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none(),
                                            **kwargs)


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"vote:{PERIOD}"):
    def __init__(self, vid: str):
        super().__init__(discord.ui.Button(label="Vote", emoji="🗳️",
                                           style=discord.ButtonStyle.primary,
                                           custom_id=f"vote:{vid}"))
        self.vid = vid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(vote_id(match))

    async def callback(self, interaction: discord.Interaction) -> None:
        grp, period = split_vote(self.vid)
        svc = service(interaction.client, self.vid)
        if not await check(interaction, grp, svc):
            return
        if await svc.open_row(period) is None:
            await interaction.response.send_message("That vote is closed.", ephemeral=True)
            return
        text, view = await svc.picker(period, interaction.user.id)
        await send_private(interaction, text, view)


async def _apply(interaction: discord.Interaction, vid: str, op: str, tmdb_id: int | None = None):
    grp, period = split_vote(vid)
    svc = service(interaction.client, vid)
    if not await check(interaction, grp, svc):
        return
    error = await svc.change_ranking(period, interaction.user.id, op, tmdb_id)
    if error and "closed" in error:
        await interaction.response.edit_message(content=error, view=None)
        return
    text, view = await svc.picker(period, interaction.user.id, notice=error)
    await interaction.response.edit_message(content=text, view=view,
                                            allowed_mentions=discord.AllowedMentions.none())


class RankSelect(discord.ui.DynamicItem[discord.ui.Select], template=rf"rank:{PERIOD}"):
    def __init__(self, vid: str, choices: list[tuple] | None = None, next_rank: int = 1):
        options = [discord.SelectOption(label=c[1], value=str(c[0]),
                                        emoji="🆕" if len(c) > 2 and c[2] else None)
                   for c in choices or []]
        super().__init__(discord.ui.Select(
            custom_id=f"rank:{vid}", placeholder=f"Pick your {ordinal(next_rank)} choice…",
            min_values=1, max_values=1,
            options=options or [discord.SelectOption(label="-", value="0")]))
        self.vid = vid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(vote_id(match))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tmdb_id = int(self.item.values[0])
        except (IndexError, ValueError):
            tmdb_id = 0
        await _apply(interaction, self.vid, "add", tmdb_id)


class UndoButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"rank-undo:{PERIOD}"):
    def __init__(self, vid: str):
        super().__init__(discord.ui.Button(label="Undo last", emoji="↩️",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"rank-undo:{vid}"))
        self.vid = vid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(vote_id(match))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _apply(interaction, self.vid, "undo")


class ClearButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"rank-clear:{PERIOD}"):
    def __init__(self, vid: str):
        super().__init__(discord.ui.Button(label="Clear", emoji="🗑️",
                                           style=discord.ButtonStyle.danger,
                                           custom_id=f"rank-clear:{vid}"))
        self.vid = vid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(vote_id(match))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _apply(interaction, self.vid, "clear")


# nominations -------------------------------------------------------------------
def _noms(grp: str | None) -> str:
    return "noms:" if not grp or grp == "main" else f"noms:{grp}:"


class MyNominationsButton(discord.ui.DynamicItem[discord.ui.Button],
                          template=r"noms:" + GRP + r"show"):
    def __init__(self, grp: str | None = None):
        super().__init__(discord.ui.Button(label="My nominations", emoji="📋",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"{_noms(grp)}show"))
        self.grp = grp

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["grp"])

    async def callback(self, interaction: discord.Interaction) -> None:
        pool = pool_for(interaction.client, self.grp)
        if not await check(interaction, self.grp, pool):
            return
        text, view = await pool.my_view(interaction.user.id)
        await send_private(interaction, text, view)


class NominationButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"noms:" + GRP + r"(?P<op>up|del):(?P<rank>[1-3])"):
    def __init__(self, op: str, rank: int, grp: str | None = None):
        label = f"Move #{rank} up" if op == "up" else f"Withdraw #{rank}"
        super().__init__(discord.ui.Button(
            label=label, emoji="⬆️" if op == "up" else "🗑️",
            style=discord.ButtonStyle.secondary if op == "up" else discord.ButtonStyle.danger,
            custom_id=f"{_noms(grp)}{op}:{rank}"))
        self.op, self.rank, self.grp = op, rank, grp

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["op"], int(match["rank"]), match["grp"])

    async def callback(self, interaction: discord.Interaction) -> None:
        pool = pool_for(interaction.client, self.grp)
        if not await check(interaction, self.grp, pool):
            return
        notice = await pool.change(interaction.user.id, self.op, self.rank)
        text, view = await pool.my_view(interaction.user.id, notice=notice)
        await interaction.response.edit_message(content=text, view=view,
                                                allowed_mentions=discord.AllowedMentions.none())


# ping roles ----------------------------------------------------------------------
async def toggle_role(interaction: discord.Interaction, rid: int | None, why: str) -> None:
    role = interaction.guild.get_role(rid) if (rid and interaction.guild) else None
    member = interaction.user
    if role is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("That role isn't set up yet.", ephemeral=True)
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason=why)
            msg = f"🔕 Removed **@{role.name}**. You won't be pinged when a movie is picked."
        else:
            await member.add_roles(role, reason=why)
            msg = f"🔔 Added **@{role.name}**: you'll be pinged when a movie is picked. Click again to stop."
    except discord.Forbidden:
        log.error("Can't change role %s: the bot's role must be above it with Manage Roles",
                  role.name)
        msg = "⚠️ The bot isn't allowed to change that role. The admins were told."
    await interaction.response.send_message(msg, ephemeral=True)


class PingButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"pings:(?P<grp>[a-z][a-z0-9]*)"):
    """🔔 Pings on a ballot: main → @Weekly Movie; a group → its opt-in ping role."""
    def __init__(self, grp: str | None = None):
        grp = grp or "main"
        super().__init__(discord.ui.Button(label="Pings", emoji="🔔",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"pings:{grp}"))
        self.grp = grp

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["grp"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.config
        if self.grp == "main":
            await toggle_role(interaction, cfg.roles.get("weekly_ping"), "Pings button")
            return
        g = cfg.groups.get(self.grp)
        if g is None or not in_group(interaction.client, interaction.user, self.grp):
            await interaction.response.send_message("🔒 That's only for members of this group.",
                                                    ephemeral=True)
            return
        await toggle_role(interaction, g.ping_role_id, "Pings button")


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"role:(?P<key>\w+)"):
    """Toggles an opt-in ping role (`role:<key>`, posted by setup_server.py on a
    message's `buttons:`; none are configured since Phase 12b, kept for old messages)."""
    def __init__(self, key: str):
        super().__init__(discord.ui.Button(label=key, custom_id=f"role:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await toggle_role(interaction, interaction.client.config.roles.get(self.key),
                          "Ping role button")


DYNAMIC_ITEMS = (VoteButton, RankSelect, UndoButton, ClearButton, MyNominationsButton,
                 NominationButton, PingButton, RoleButton)


def ballot_view(vid: str, grp: str | None = None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VoteButton(vid))
    view.add_item(MyNominationsButton(grp))
    view.add_item(PingButton(grp))
    return view


def picker_view(vid: str, choices: list[tuple], next_rank: int,
                has_ranking: bool) -> discord.ui.View | None:
    # A timeout only drops the in-memory copy; clicks after it (or after a
    # restart) still reach the DynamicItems above.
    view = discord.ui.View(timeout=600)
    if choices:
        view.add_item(RankSelect(vid, choices[:25], next_rank))
    if has_ranking:
        view.add_item(UndoButton(vid))
        view.add_item(ClearButton(vid))
    return view if view.children else None


def nominations_view(n: int, grp: str | None = None) -> discord.ui.View | None:
    if n == 0:
        return None
    view = discord.ui.View(timeout=600)
    for r in range(2, n + 1):
        view.add_item(NominationButton("up", r, grp))
    for r in range(1, n + 1):
        b = NominationButton("del", r, grp)
        b.item.row = 1
        view.add_item(b)
    return view
