"""The button row under a movie's info card in #movies.

Before a spoiler discussion exists, the card has a persistent "Spoiler
discussion" button (custom_id `spoilers:<tmdb_id>`, so it keeps working after
restarts). Once the discussion exists, the card gets a plain link button.
"""
from __future__ import annotations

import logging
import re

import discord

log = logging.getLogger(__name__)

# Bump when the card layout changes; old cards are refreshed once at startup.
CARD_VERSION = 1
SPOILER_BUTTON_LABEL = "Spoiler discussion"
SPOILER_BUTTON_EMOJI = "🔓"


class SpoilerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"spoilers:(?P<tmdb_id>\d+)"):
    def __init__(self, tmdb_id: int):
        super().__init__(discord.ui.Button(
            label=SPOILER_BUTTON_LABEL, emoji=SPOILER_BUTTON_EMOJI,
            style=discord.ButtonStyle.secondary, custom_id=f"spoilers:{tmdb_id}"))
        self.tmdb_id = tmdb_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str], /) -> "SpoilerButton":
        return cls(int(match["tmdb_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        try:
            thread, created = await bot.spoilers.open(self.tmdb_id)
        except LookupError:
            await interaction.followup.send("This movie isn't in the bot's records any more.",
                                            ephemeral=True)
            return
        verb = "Opened" if created else "Here's"
        await interaction.followup.send(
            f"{verb} the spoiler discussion → {thread.jump_url}\n"
            "-# ⚠️ Everything there may spoil the film.", ephemeral=True)


def card_view(tmdb_id: int, spoiler_url: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if spoiler_url:
        view.add_item(discord.ui.Button(label=SPOILER_BUTTON_LABEL, emoji=SPOILER_BUTTON_EMOJI,
                                        style=discord.ButtonStyle.link, url=spoiler_url))
    else:
        view.add_item(SpoilerButton(tmdb_id))
    return view
