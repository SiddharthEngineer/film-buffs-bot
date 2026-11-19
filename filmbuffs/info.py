"""/info (Phase 23): how the server works, in a few private embeds.

/help is just the list of commands; /info explains the systems behind them:
the weekly movie (nominating, voting, results), the movie posts, Letterboxd
(link, sync, watchlist reports), and, for members of a private group only, that
group's monthly vote. Built from config so channel and command links are live.
"""
from __future__ import annotations

from typing import Callable

import discord

from .movies import EMBED_COLOR

Ref = Callable[[str], str]


def channel_ref(channels: dict[str, int]) -> Ref:
    def ref(key: str) -> str:
        cid = channels.get(key)
        return f"<#{cid}>" if cid else "#" + key.replace("_", "-")
    return ref


def command_ref(ids: dict[str, int]) -> Ref:
    """`</letterboxd link:id>` (clickable) when the top-level command's ID is known."""
    def cmd(name: str) -> str:
        cid = ids.get(name.split()[0])
        return f"</{name}:{cid}>" if cid else f"**/{name}**"
    return cmd


def info_sections(ch: Ref, cmd: Ref, poll_minutes: float,
                  groups: list[str]) -> list[tuple[str, str]]:
    """(title, text) per section. `groups`: names of the member's private groups."""
    sections = [
        ("🗳️ The weekly movie", "\n".join([
            "**Nominating.** In " + ch("watch_planning") + ", click **My nomination** (or use "
            + cmd("nominate") + ") to see your nomination and pick a film. Everyone gets "
            "**one**; picking another swaps it. Several people can nominate the same film, and "
            "the board lists them all. Any film tagged Backlog or Watched (solo) can be "
            "nominated, not one that's Up Next or that we already watched together. The ballot "
            "holds 25 films; extra nominations wait for a spot. If your film wins, nominate "
            "something new.",
            "",
            "**Voting.** Click **Vote** (or use " + cmd("vote") + ") and rank as many films as "
            "you like, best first. It's **instant runoff**: each round, every ballot counts for "
            "its highest-ranked film still in the race, and the film with the fewest votes is "
            "knocked out until one has a majority. Ties go out by fewest 1st choices, then "
            "fewest members with it on their Letterboxd watchlist, then most recently added. "
            "Your ranking carries over to the next week; 🆕 marks films that joined the "
            "ballot in the last 2 days.",
            "",
            "**Results.** The vote closes **Sunday at 11:59 PM CT**. The winner is announced "
            "in " + ch("watch_log") + " (pinging @Weekly Pings), gets a server event, and is "
            "pinned in " + ch("movies") + " and " + ch("movie_spoilers") + " for its watch "
            "week (Monday to Sunday). If nobody nominated or voted, the week is skipped. "
            + cmd("schedule") + " shows this and next week at a glance.",
        ])),
        ("🍿 Movie posts", "\n".join([
            "Every movie has **two** posts, made by the bot from TMDB:",
            "• " + ch("movies") + " is **spoiler-free**: trailers, hype, first impressions.",
            "• " + ch("movie_spoilers") + " is for after you've watched it: reviews and hot "
            "takes. Each post links to the other.",
            "**Tags:** 📋 Backlog (not watched together yet), ⏭️ Up Next (this or next week's "
            "pick), ✅ Watched (together), 👤 Watched (solo) (someone logged it on Letterboxd), "
            "plus genres. 🎟️ In Theaters and 📺 Streaming are for new films only, checked "
            "against TMDB (US) every morning. Post-watch posts get 📓 Reviews once a review "
            "is posted there.",
            cmd("addmovie") + " adds a movie's posts without nominating it. Spoiled something "
            "in " + ch("movies") + "? Right-click (long-press on mobile) the message → "
            "**Apps → Mark as spoiler** moves it to the post-watch post, under its author's "
            "name.",
        ])),
        ("📒 Letterboxd", "\n".join([
            "**Link** your account with " + cmd("letterboxd link") + " and your username; "
            "it's what connects you to Letterboxd. The bot reads your **public diary** about "
            f"every {poll_minutes:g} minutes. Each new diary entry shows up in three places: "
            "a line in " + ch("letterboxd_feed") + ", a spoiler-free \"logged this\" note "
            "on the movie's " + ch("movies") + " post, and your rating, ♥, rewatch and review "
            "in its " + ch("movie_spoilers") + " post (the only place ratings and reviews "
            "appear). A film with no posts gets them (as Watched (solo)). Adding a review to an "
            "entry later updates the same message. Lists aren't posted, and entries from "
            "before you linked aren't either. " + cmd("letterboxd unlink") + " stops it.",
            "",
            "**Sync** (optional): " + cmd("letterboxd sync") + " takes your Letterboxd export "
            "zip (Letterboxd → Settings → Data → Export your data) and brings in your whole "
            "history: older entries are stored, reviews are posted in post-watch posts, films "
            "you logged or want to watch get posts, and entries you edited or deleted on "
            "Letterboxd are updated or removed here. It shows a preview first and changes "
            "nothing until you press **Apply**. The export must be from the account you linked.",
            "",
            "**Watchlists:** the bot reads your **public watchlist** when you link and again "
            "every day (a private watchlist can't be read). " + cmd("watchlist show") + " "
            "re-reads anything older than 6 hours. "
            "On its own it shows yours; name **one** person for theirs, **two** for the films "
            "on both lists, **three or more** for every film on at least two of their lists, "
            "with whose. Watchlists also break ties in the weekly vote.",
        ])),
        ("📚 More", "\n".join([
            "• **Pings:** you're pinged when the weekly movie (or your group's movie) is "
            "picked; **🔔 Pings** on the board in " + ch("watch_planning") + " turns it off "
            "or on. Posts you follow notify you as your own Discord settings say.",
            "• **Ideas** for the server or the bot? Post them in " + ch("ideas") + "; the "
            "pinned list there tracks the ones being considered.",
            "• " + cmd("help") + " lists every command; " + ch("welcome") + " has the short "
            "tour.",
        ])),
    ]
    for name in groups:
        sections.insert(1, (f"🌙 {name}'s monthly movie", "\n".join([
            f"{name} has its own private category with its own **monthly** vote, completely "
            "separate from the weekly one, with its own nominations. In the group's channels, "
            "**My nomination**, **Vote**, " + cmd("nominate") + ", " + cmd("vote") + " and "
            + cmd("schedule") + " act on the group's vote. It closes on the **last day of the "
            "month**; the pick is announced in the group's #chat (pinging its Pings role), "
            "watched during that month and discussed in the group's voice channel on the 1st. "
            "A group can pick any film it hasn't picked before.",
        ])))
    return sections


def info_embeds(ch: Ref, cmd: Ref, poll_minutes: float,
                groups: list[str]) -> list[discord.Embed]:
    embeds = [discord.Embed(title=title, description=text[:4096], color=EMBED_COLOR)
              for title, text in info_sections(ch, cmd, poll_minutes, groups)]
    embeds[0].set_author(name="ℹ️ How Film Buffs works")
    return embeds
