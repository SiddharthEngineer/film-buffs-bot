"""Buttons and the private picker for the weekly ranked vote (Phase 5R).

The ballot message in #watch-planning gets a 🗳️ Vote button while voting is
open. Clicking it sends the member a private (ephemeral) picker: one dropdown
with the films they haven't ranked yet, so each pick becomes their next rank,
plus Undo last / Clear. Discord can't fit a dropdown per rank (5 per message)
and doesn't promise multi-select values come in click order, hence one pick
at a time. Every change is saved straight away.

All components are DynamicItems keyed by the watch week (`vote:2026-11-23`), so
they keep working after a restart; state lives in the DB, not the view.
"""
from __future__ import annotations

import logging
import re

import discord

from .weekly import ordinal

log = logging.getLogger(__name__)

WEEK = r"(?P<week>\d{4}-\d{2}-\d{2})"


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"vote:{WEEK}"):
    def __init__(self, week: str):
        super().__init__(discord.ui.Button(label="Vote", emoji="🗳️",
                                           style=discord.ButtonStyle.primary,
                                           custom_id=f"vote:{week}"))
        self.week = week

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        weekly = interaction.client.weekly
        if await weekly.voting_week(self.week) is None:
            await interaction.response.send_message("Voting for that week is closed.",
                                                    ephemeral=True)
            return
        text, view = await weekly.picker(self.week, interaction.user.id)
        await interaction.response.send_message(text, view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def _apply(interaction: discord.Interaction, week: str, op: str, tmdb_id: int | None = None):
    weekly = interaction.client.weekly
    error = await weekly.change_ranking(week, interaction.user.id, op, tmdb_id)
    if error and "closed" in error:
        await interaction.response.edit_message(content=error, view=None)
        return
    text, view = await weekly.picker(week, interaction.user.id, notice=error)
    await interaction.response.edit_message(content=text, view=view,
                                            allowed_mentions=discord.AllowedMentions.none())


class RankSelect(discord.ui.DynamicItem[discord.ui.Select], template=rf"rank:{WEEK}"):
    def __init__(self, week: str, choices: list[tuple[int, str]] | None = None, next_rank: int = 1):
        options = [discord.SelectOption(label=label, value=str(tid)) for tid, label in choices or []]
        super().__init__(discord.ui.Select(
            custom_id=f"rank:{week}", placeholder=f"Pick your {ordinal(next_rank)} choice…",
            min_values=1, max_values=1,
            options=options or [discord.SelectOption(label="-", value="0")]))
        self.week = week

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tmdb_id = int(self.item.values[0])
        except (IndexError, ValueError):
            tmdb_id = 0
        await _apply(interaction, self.week, "add", tmdb_id)


class UndoButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"rank-undo:{WEEK}"):
    def __init__(self, week: str):
        super().__init__(discord.ui.Button(label="Undo last", emoji="↩️",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"rank-undo:{week}"))
        self.week = week

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _apply(interaction, self.week, "undo")


class ClearButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"rank-clear:{WEEK}"):
    def __init__(self, week: str):
        super().__init__(discord.ui.Button(label="Clear", emoji="🗑️",
                                           style=discord.ButtonStyle.danger,
                                           custom_id=f"rank-clear:{week}"))
        self.week = week

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _apply(interaction, self.week, "clear")


DYNAMIC_ITEMS = (VoteButton, RankSelect, UndoButton, ClearButton)


def ballot_view(week: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VoteButton(week))
    return view


def picker_view(week: str, choices: list[tuple[int, str]], next_rank: int,
                has_ranking: bool) -> discord.ui.View | None:
    # A timeout only drops the in-memory copy; clicks after it (or after a
    # restart) still reach the DynamicItems above.
    view = discord.ui.View(timeout=600)
    if choices:
        view.add_item(RankSelect(week, choices, next_rank))
    if has_ranking:
        view.add_item(UndoButton(week))
        view.add_item(ClearButton(week))
    return view if view.children else None
