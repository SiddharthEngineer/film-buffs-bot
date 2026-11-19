"""/help (Phase 15): every command a member can use, grouped, one line each.

Built from the bot's registered command tree, so new commands show up without
editing this file; SECTIONS only decides which heading they go under (anything
unlisted lands in "More"). Admin-only commands (default permission Manage
Server) are listed only for admins.
"""
from __future__ import annotations

from discord import app_commands

# Heading → top-level command names, in display order.
SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("🎬 Movies and spoilers", ("addmovie", "spoiler", "Mark as spoiler")),
    ("🗳️ Picking movies", ("nominate", "nominations", "unnominate", "schedule")),
    ("📒 Letterboxd", ("letterboxd", "watchlist")),
    ("🔧 Admin", ("admin", "Add to ideas")),
]
MORE = "📚 More"
# Context menus have no description of their own.
MENU_TEXT = {
    "Mark as spoiler": "move a message out of a #pre-watch post into its post-watch discussion",
    "Add to ideas": "add a message in #ideas to the pinned ideas list",
}
def voting_notes(watch_log: str = "#watch-log") -> list[str]:
    """How nominating and voting work (Phase 19: moved here from the boards in
    #watch-planning, which now show just the ballot)."""
    return [
        "-# **Nominating:** one film each; nominating again swaps it. Several people can "
        "nominate the same film, and the ballot lists them all. If yours wins, you can "
        "nominate something new.",
        "-# **Voting:** click **Vote** on the board in #watch-planning and rank as many films "
        "as you like, best first. It's instant runoff: a lower pick only counts if your higher "
        "ones are knocked out. Your ranking carries over to the next vote. 🆕 marks films that "
        "joined the ballot in the last 2 days.",
        f"-# **Results:** the weekly vote closes Sunday at 11:59 PM CT. The winner is announced "
        f"in {watch_log}, pinging @Weekly Pings, and gets a server event. **🔔 Pings** on the "
        f"board turns the ping on or off.",
    ]


GROUP_NOTE = ("-# **Groups:** in a group's channels, **/nominate**, **/nominations**, "
              "**/unnominate** and **/schedule** act on that group's monthly vote. It's separate "
              "from the weekly vote, closes on the last day of the month, and its pick is "
              "announced in the group's #chat.")


def is_admin_only(cmd) -> bool:
    perms = getattr(cmd, "default_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


def _leaves(cmd):
    """(full name, description) for a command or every subcommand of a group."""
    if isinstance(cmd, app_commands.Group):
        for sub in cmd.walk_commands():
            if isinstance(sub, app_commands.Command):
                yield sub.qualified_name, sub.description
    else:
        yield cmd.name, getattr(cmd, "description", "")


def command_line(cmd, ids: dict[str, int]) -> list[str]:
    if isinstance(cmd, app_commands.ContextMenu):
        what = MENU_TEXT.get(cmd.name, "")
        return [f"• Right-click a message → **Apps → {cmd.name}**" + (f": {what}" if what else "")]
    out = []
    cid = ids.get(cmd.name)
    for name, desc in _leaves(cmd):
        label = f"</{name}:{cid}>" if cid else f"**/{name}**"
        out.append(f"• {label}" + (f": {desc[0].lower()}{desc[1:]}" if desc else ""))
    return out


def help_text(commands, ids: dict[str, int], admin: bool, welcome_channel_id: int | None,
              watch_log_channel_id: int | None = None) -> str:
    by_name = {c.name: c for c in commands if c.name != "help"}   # they're already in it
    placed: set[str] = set()
    blocks: list[str] = []
    for heading, names in SECTIONS + [(MORE, tuple(sorted(set(by_name) - {
            n for _, ns in SECTIONS for n in ns})))]:
        lines = []
        for n in names:
            cmd = by_name.get(n)
            if cmd is None or n in placed or (is_admin_only(cmd) and not admin):
                continue
            placed.add(n)
            lines += command_line(cmd, ids)
        if heading.startswith("🗳️") and lines:
            log_ref = f"<#{watch_log_channel_id}>" if watch_log_channel_id else "#watch-log"
            lines += voting_notes(log_ref) + [GROUP_NOTE]
        if lines:
            blocks.append(f"**{heading}**\n" + "\n".join(lines))
    tail = (f"New here? The whole tour is in <#{welcome_channel_id}>." if welcome_channel_id
            else "New here? The whole tour is in #welcome.")
    return "\n\n".join(blocks + [tail])
