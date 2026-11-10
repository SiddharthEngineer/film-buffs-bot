"""Phase 0: read-only connection check. Changes nothing in the server.

Logs in, prints the guild, its channels and roles, the bot's guild-level
permissions versus what it needs, and checks the TMDB key. Then exits.
"""
import asyncio
import os
import sys

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()

# What the bot needs, and why. Must match the invite URL.
REQUIRED = {
    "view_channel": "see channels",
    "manage_channels": "create channels, edit forum tags (setup script)",
    "manage_roles": "set channel permission overwrites (setup script)",
    "manage_threads": "apply moderated forum tags",
    "send_messages": "post messages; create forum posts",
    "send_messages_in_threads": "post inside movie threads",
    "manage_messages": "delete originals for 'Mark as spoiler'",
    "manage_webhooks": "repost as author for 'Mark as spoiler'",
    "embed_links": "movie and review embeds",
    "add_reactions": "backlog 👍 on starter messages",
    "read_message_history": "find spoiler points, build jump links",
    "send_polls": "weekly pick poll",
    "create_events": "watch-week scheduled events",
}


async def check_tmdb() -> str:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        return "TMDB: no key set yet (skipped)"
    # Support either a v3 API key or a v4 read-access token (a long JWT).
    params, headers = {}, {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.themoviedb.org/3/movie/603",
                         params=params, headers=headers) as r:
            if r.status != 200:
                return f"TMDB: FAILED (HTTP {r.status})"
            data = await r.json()
            return f"TMDB: OK (movie 603 = {data.get('title')})"


async def main() -> int:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        print("DISCORD_TOKEN and DISCORD_GUILD_ID must be set in .env")
        return 2

    intents = discord.Intents.default()
    intents.message_content = True  # fails to log in if not enabled in the portal
    client = discord.Client(intents=intents)
    result = {"code": 1}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(guild_id))
            if guild is None:
                print(f"Bot is not in guild {guild_id}. Guilds it is in: "
                      f"{[(g.name, g.id) for g in client.guilds]}")
                return
            me = guild.me
            print(f"Logged in as {client.user} (id {client.user.id})")
            print(f"Guild: {guild.name} (id {guild.id}), members: {guild.member_count}\n")

            print("Channels:")
            for cat, chans in guild.by_category():
                print(f"  [{cat.name if cat else 'no category'}]"
                      + (f" id={cat.id}" if cat else ""))
                for c in chans:
                    print(f"    {c.type.name:<10} #{c.name}  id={c.id}")

            print("\nRoles (top first):")
            for r in sorted(guild.roles, key=lambda r: r.position, reverse=True):
                flag = "  <- bot" if r in me.roles else ""
                print(f"  {r.position:>3}  {r.name}  id={r.id}"
                      f"  managed={r.managed}{flag}")

            perms = me.guild_permissions
            print(f"\nBot top role: {me.top_role.name} (position {me.top_role.position})")
            print("Effective guild permissions:")
            missing = []
            for name, why in REQUIRED.items():
                ok = getattr(perms, name)
                print(f"  {'OK ' if ok else 'MISSING'}  {name:<26} {why}")
                if not ok:
                    missing.append(name)
            if perms.administrator:
                print("  NOTE: bot has Administrator; not needed, consider removing it.")

            print("\n" + await check_tmdb())
            print("\nResult:", "all required permissions present"
                  if not missing else f"missing {missing}")
            result["code"] = 0 if not missing else 1
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.PrivilegedIntentsRequired:
        print("Message Content intent is not enabled in the Developer Portal.")
        return 1
    except discord.LoginFailure:
        print("Login failed: token is invalid.")
        return 1
    return result["code"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
