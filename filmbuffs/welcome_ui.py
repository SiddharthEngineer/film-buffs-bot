"""Buttons on the #welcome page, so nobody has to type a command.

setup_server.py posts the page from server.yaml; a section can carry `buttons:`,
each with an `action` from WELCOME_ACTIONS and the custom ID `welcome:<action>`.
The running bot handles the clicks here (a DynamicItem, so they survive restarts):

- letterboxd_link: a modal asking for the username (prefilled with the linked one),
  then the same flow as /letterboxd link.
- letterboxd_sync: a modal with a file upload for the export zip, then the same
  flow as /letterboxd sync (preview with Apply / Cancel).
- weekly_pings: toggles @Weekly Pings (same as 🔔 Pings on the weekly board).
- group_pings: toggles the ping role of the clicker's private group; with several
  groups, a private reply with one button per group.
"""
from __future__ import annotations

import logging
import re

import discord

log = logging.getLogger(__name__)

WELCOME_ACTIONS = ("letterboxd_link", "letterboxd_sync", "weekly_pings", "group_pings")


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog("Letterboxd")


class LinkModal(discord.ui.Modal, title="Link your Letterboxd account"):
    def __init__(self, current: str | None):
        super().__init__()
        self.username = discord.ui.TextInput(
            label="Letterboxd username or profile link", max_length=100, required=True,
            default=current, placeholder="e.g. yourname or letterboxd.com/yourname")
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _cog(interaction).link_account(interaction, self.username.value.strip())


class SyncModal(discord.ui.Modal, title="Sync your Letterboxd history"):
    def __init__(self):
        super().__init__()
        self.upload = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(discord.ui.Label(
            text="Your Letterboxd export (.zip)",
            description="Letterboxd → Settings → Data → Export your data, then upload the zip",
            component=self.upload))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        files = self.upload.values
        if not files:
            await interaction.response.send_message("❌ No file came through. Try again.",
                                                    ephemeral=True)
            return
        await _cog(interaction).sync_export(interaction, files[0])


class WelcomeButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"welcome:(?P<action>[a-z_]+)"):
    def __init__(self, action: str, label: str | None = None, emoji: str | None = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label or action, emoji=emoji, style=style,
                                           custom_id=f"welcome:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = getattr(self, f"_{self.action}", None)
        if self.action not in WELCOME_ACTIONS or handler is None:
            await interaction.response.send_message("That button doesn't do anything any more.",
                                                    ephemeral=True)
            return
        await handler(interaction)

    async def _letterboxd_link(self, interaction: discord.Interaction) -> None:
        link = await interaction.client.db.get_link(interaction.user.id)
        await interaction.response.send_modal(LinkModal(link["username"] if link else None))

    async def _letterboxd_sync(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        if interaction.user.id in cog._syncing:
            await interaction.response.send_message(
                "⏳ Your previous sync is still running. Try again when it's done.",
                ephemeral=True)
            return
        if await interaction.client.db.get_link(interaction.user.id) is None:
            await interaction.response.send_message(
                "❌ Link your Letterboxd account first with **🔗 Link Letterboxd**, then sync.",
                ephemeral=True)
            return
        await interaction.response.send_modal(SyncModal())

    async def _weekly_pings(self, interaction: discord.Interaction) -> None:
        from .vote_ui import toggle_role
        await toggle_role(interaction, interaction.client.config.roles.get("weekly_ping"),
                          "Weekly Ping button")

    async def _group_pings(self, interaction: discord.Interaction) -> None:
        from .vote_ui import PingButton, in_group, toggle_role
        groups = [g for g in interaction.client.config.groups.values()
                  if in_group(interaction.client, interaction.user, g.key)]
        if not groups:
            await interaction.response.send_message(
                "You're not in a private group, so there's nothing to turn on.", ephemeral=True)
            return
        if len(groups) == 1:
            await toggle_role(interaction, groups[0].ping_role_id, "Group Ping button")
            return
        view = discord.ui.View(timeout=None)
        for g in groups[:25]:
            b = PingButton(g.key)
            b.item.label = f"{g.name} Pings"[:80]
            view.add_item(b)
        await interaction.response.send_message("Which group's pings?", view=view,
                                                ephemeral=True)
