"""The info card's version, and the old "Post-watch discussion" button.

Since Phase 18 every movie gets its #post-watch post together with its
#pre-watch post, and the card links to it in its text: cards have no buttons.
`SpoilerButton` stays registered so a card that hasn't been refreshed yet
(custom_id `spoilers:<tmdb_id>`) still works until the refresh removes it.
"""
from __future__ import annotations

import logging
import re

import discord

log = logging.getLogger(__name__)

# Bump when the card layout changes; old cards are refreshed once at startup.
# 2: "Post-watch discussion" label + no-spoilers reminder; 3: no 👍 footer;
# 4: no button, the reminder links the #post-watch post (Phase 18);
# 5: the whole reminder line is the link, no arrow
CARD_VERSION = 5
SPOILER_BUTTON_LABEL = "Post-watch discussion"
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
            f"{verb} the post-watch discussion → {thread.jump_url}\n"
            "-# ⚠️ Everything there may spoil the film.", ephemeral=True)

