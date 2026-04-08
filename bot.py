"""
RWA Discord Bot
Teams: Chicago Water | LA Galaxy WC | VK Jug Dubrovnik
"""

import os
import asyncio
import logging
import aiohttp
from datetime import datetime, timezone
from dotenv import load_dotenv

import discord
from discord.ext import commands
from discord import option

import firebase_admin
from firebase_admin import credentials, firestore

# ─────────────────────────────────────────────
# MASTER ADMINS
# ─────────────────────────────────────────────
ALLOWED_ADMIN_IDS = [1340332911668498473, 687733937367547929]

# ─────────────────────────────────────────────
# ENV + LOGGING
# ─────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rwa-bot")

DISCORD_TOKEN  = os.getenv("TOKEN")
LOG_CHANNEL    = os.getenv("LOG_CHANNEL_NAME", "mod-logs")
FIREBASE_CREDS = os.getenv("FIREBASE_CREDENTIALS", "firebase.json")

# ─────────────────────────────────────────────
# TEAMS CONFIG
# ─────────────────────────────────────────────
TEAMS = {
    "Chicago Water":    {"key": "chicago_water"},
    "LA Galaxy WC":     {"key": "la_galaxy_wc"},
    "VK Jug Dubrovnik": {"key": "vk_jug_dubrovnik"},
    "Free Agent":       {"key": "free_agent"},
}
TEAM_CHOICES = ["Chicago Water", "LA Galaxy WC", "VK Jug Dubrovnik"]

def resolve_team(name: str) -> dict | None:
    for k, v in TEAMS.items():
        if k.lower() == name.lower():
            return {"name": k, **v}
    return None

def ts() -> str:
    return datetime.now(timezone.utc).isoformat()

# ─────────────────────────────────────────────
# FIREBASE INIT
# ─────────────────────────────────────────────
def init_firebase() -> firestore.Client:
    import json, tempfile

    raw = os.getenv("FIREBASE_JSON")
    if raw:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
        tmp.write(raw)
        tmp.close()
        cred_path = tmp.name
    elif os.path.exists(FIREBASE_CREDS):
        cred_path = FIREBASE_CREDS
    else:
        raise FileNotFoundError(
            "No Firebase credentials found. Set FIREBASE_JSON secret or place firebase.json next to bot.py"
        )

    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    log.info("Firebase initialised")
    return firestore.client()

db: firestore.Client = init_firebase()

# ─────────────────────────────────────────────
# ROBLOX API HELPERS
# ─────────────────────────────────────────────
ROBLOX_USERS_URL   = "https://users.roblox.com/v1"
ROBLOX_THUMBS_URL  = "https://thumbnails.roblox.com/v1"
ROBLOX_FRIENDS_URL = "https://friends.roblox.com/v1"
ROBLOX_BADGES_URL  = "https://badges.roblox.com/v1"
ROBLOX_GROUPS_URL  = "https://groups.roblox.com/v1"

async def roblox_get(session: aiohttp.ClientSession, url: str) -> dict | list | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json()
            log.warning("Roblox API %s -> HTTP %s", url, r.status)
            return None
    except Exception as e:
        log.error("Roblox GET error: %s", e)
        return None

async def get_roblox_id(session: aiohttp.ClientSession, username: str) -> int | None:
    payload = {"usernames": [username], "excludeBannedUsers": False}
    try:
        async with session.post(
            f"{ROBLOX_USERS_URL}/usernames/users",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                data = await r.json()
                users = data.get("data", [])
                return users[0]["id"] if users else None
    except Exception as e:
        log.error("get_roblox_id error: %s", e)
    return None

async def get_roblox_profile(session: aiohttp.ClientSession, username: str) -> dict | None:
    uid = await get_roblox_id(session, username)
    if not uid:
        return None

    user_data = await roblox_get(session, f"{ROBLOX_USERS_URL}/users/{uid}")
    if not user_data:
        return None

    thumb_data = await roblox_get(
        session,
        f"{ROBLOX_THUMBS_URL}/users/avatar-headshot?userIds={uid}&size=420x420&format=Png&isCircular=false",
    )
    avatar_url = None
    if thumb_data and thumb_data.get("data"):
        avatar_url = thumb_data["data"][0].get("imageUrl")

    return {
        "id":          uid,
        "username":    user_data.get("name"),
        "display":     user_data.get("displayName"),
        "description": user_data.get("description", ""),
        "created":     user_data.get("created", ""),
        "banned":      user_data.get("isBanned", False),
        "avatar_url":  avatar_url,
    }

async def get_roblox_badges(session: aiohttp.ClientSession, uid: int) -> list:
    data = await roblox_get(session, f"{ROBLOX_BADGES_URL}/users/{uid}/badges?limit=10")
    return data.get("data", []) if data else []

async def get_roblox_friends(session: aiohttp.ClientSession, uid: int) -> list:
    data = await roblox_get(session, f"{ROBLOX_FRIENDS_URL}/users/{uid}/friends")
    return data.get("data", []) if data else []

async def get_roblox_groups(session: aiohttp.ClientSession, uid: int) -> list:
    data = await roblox_get(session, f"{ROBLOX_GROUPS_URL}/users/{uid}/groups/roles")
    return data.get("data", []) if data else []

# ─────────────────────────────────────────────
# FIREBASE HELPERS
# Schema: users/{roblox_id} -> { roblox_id, roblox_username, roblox_username_lower, team, suspended, suspended_reason }
# ─────────────────────────────────────────────
def fb_get_user(roblox_id: int) -> dict | None:
    doc = db.collection("users").document(str(roblox_id)).get()
    return doc.to_dict() if doc.exists else None

def fb_get_user_by_username(roblox_username: str) -> dict | None:
    query = (
        db.collection("users")
        .where("roblox_username_lower", "==", roblox_username.lower())
        .limit(1)
        .stream()
    )
    for doc in query:
        return doc.to_dict()
    return None

def fb_upsert_user(roblox_id: int, data: dict):
    ref = db.collection("users").document(str(roblox_id))
    ref.set(data, merge=True)

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Bot(intents=intents)

# ─────────────────────────────────────────────
# LOG HELPER
# ─────────────────────────────────────────────
async def send_log(guild: discord.Guild, embed: discord.Embed):
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL)
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass

# ─────────────────────────────────────────────
# /profile
# ─────────────────────────────────────────────
@bot.slash_command(name="profile", description="Look up a Roblox user's profile.")
@option("roblox_user", description="Roblox username")
async def cmd_profile(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_user)

    if not profile:
        await ctx.followup.send(f"Couldn't find Roblox user **{roblox_user}**.", ephemeral=True)
        return

    record = fb_get_user(profile["id"])
    team   = record.get("team", "Free Agent") if record else "Free Agent"
    suspended = record.get("suspended", False) if record else False
    joined = profile["created"][:10] if profile["created"] else "Unknown"

    embed = discord.Embed(
        title=f"{profile['username']}",
        url=f"https://www.roblox.com/users/{profile['id']}/profile",
        color=discord.Color.blurple(),
    )
    if profile["display"] and profile["display"] != profile["username"]:
        embed.title += f"  ({profile['display']})"

    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Roblox ID",  value=str(profile["id"]), inline=True)
    embed.add_field(name="Joined",     value=joined,              inline=True)
    embed.add_field(name="Team",       value=team,                inline=True)

    status_parts = []
    if profile["banned"]:
        status_parts.append("Banned on Roblox")
    if suspended:
        status_parts.append("Suspended")
    if status_parts:
        embed.add_field(name="Status", value=" | ".join(status_parts), inline=False)

    if profile["description"]:
        embed.add_field(name="Bio", value=profile["description"][:200], inline=False)

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# /altcheck
# ─────────────────────────────────────────────
@bot.slash_command(name="altcheck", description="Run an alt-account check on a Roblox user.")
@option("roblox_user", description="Roblox username")
async def cmd_altcheck(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_user)
        if not profile:
            await ctx.followup.send(f"Couldn't find Roblox user **{roblox_user}**.", ephemeral=True)
            return

        uid     = profile["id"]
        badges  = await get_roblox_badges(session, uid)
        friends = await get_roblox_friends(session, uid)
        groups  = await get_roblox_groups(session, uid)

    flags: list[str] = []
    created = profile.get("created", "")
    if created:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
        if age_days < 30:
            flags.append(f"Account created {age_days} day(s) ago")
    if len(badges) < 3:
        flags.append(f"Only {len(badges)} badge(s)")
    if len(friends) < 5:
        flags.append(f"Only {len(friends)} friend(s)")
    if not groups:
        flags.append("No group memberships")
    if profile.get("banned"):
        flags.append("Account is banned on Roblox")

    risk = "HIGH RISK" if len(flags) >= 3 else ("MEDIUM" if len(flags) >= 1 else "CLEAN")
    color = discord.Color.red() if risk == "HIGH RISK" else (
        discord.Color.yellow() if risk == "MEDIUM" else discord.Color.green()
    )

    embed = discord.Embed(
        title=f"Alt Check — {profile['username']}",
        color=color,
    )
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Risk",    value=risk,              inline=True)
    embed.add_field(name="Badges",  value=str(len(badges)),  inline=True)
    embed.add_field(name="Friends", value=str(len(friends)), inline=True)
    embed.add_field(name="Groups",  value=str(len(groups)),  inline=True)

    if flags:
        embed.add_field(name="Flags", value="\n".join(f"- {f}" for f in flags), inline=False)
    else:
        embed.add_field(name="Result", value="No flags — account looks legitimate.", inline=False)

    if groups:
        gnames = ", ".join(g["group"]["name"] for g in groups[:5])
        embed.add_field(name="Groups", value=gnames, inline=False)

    embed.set_footer(text=f"Checked by {ctx.author}")
    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# /badges
# ─────────────────────────────────────────────
@bot.slash_command(name="badges", description="List Roblox badges for a user.")
@option("roblox_user", description="Roblox username")
async def cmd_badges(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        uid = await get_roblox_id(session, roblox_user)
        if not uid:
            await ctx.followup.send(f"Couldn't find user **{roblox_user}**.", ephemeral=True)
            return
        badges = await get_roblox_badges(session, uid)

    embed = discord.Embed(
        title=f"Badges — {roblox_user}",
        description=f"{len(badges)} badge(s) (showing up to 10)",
        color=discord.Color.blurple(),
    )
    for b in badges[:10]:
        embed.add_field(
            name=b.get("name", "Unknown"),
            value=b.get("description", "")[:80] or "No description",
            inline=True,
        )
    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# /friends
# ─────────────────────────────────────────────
@bot.slash_command(name="friends", description="List Roblox friends for a user.")
@option("roblox_user", description="Roblox username")
async def cmd_friends(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        uid = await get_roblox_id(session, roblox_user)
        if not uid:
            await ctx.followup.send(f"Couldn't find user **{roblox_user}**.", ephemeral=True)
            return
        friends = await get_roblox_friends(session, uid)

    embed = discord.Embed(
        title=f"Friends — {roblox_user}",
        description=f"{len(friends)} friend(s) total",
        color=discord.Color.blurple(),
    )
    if len(friends) > 200:
        embed.add_field(name="Note", value="Very high friend count — possible bot account.", inline=False)

    lines = "\n".join(f["name"] for f in friends[:20])
    embed.add_field(name="List (up to 20)", value=lines or "None", inline=False)
    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# /team view
# ─────────────────────────────────────────────
team_group = bot.create_group("team", "Team commands")

@team_group.command(name="view", description="View members of a team.")
@option("team_name", description="Team name", choices=TEAM_CHOICES)
async def cmd_team_view(ctx: discord.ApplicationContext, team_name: str):
    await ctx.defer()
    query = db.collection("users").where("team", "==", team_name).stream()
    members = [d.to_dict() for d in query]

    embed = discord.Embed(
        title=team_name,
        description=f"{len(members)} member(s)",
        color=discord.Color.green(),
    )
    if members:
        lines = "\n".join(m["roblox_username"] for m in members[:30])
        embed.add_field(name="Members", value=lines, inline=False)
    else:
        embed.description = "No members on this team yet."

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# /rank  (admin only)
# ─────────────────────────────────────────────
@bot.slash_command(name="rank", description="Assign a player to a team. (Admin only)")
@option("roblox_name", description="Roblox username")
@option("team_name",   description="Team to assign", choices=TEAM_CHOICES)
async def cmd_rank(ctx: discord.ApplicationContext, roblox_name: str, team_name: str):
    await ctx.defer(ephemeral=True)
    if ctx.author.id not in ALLOWED_ADMIN_IDS:
        await ctx.followup.send("You don't have permission to do that.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"Couldn't find Roblox user **{roblox_name}**.", ephemeral=True)
        return

    fb_upsert_user(profile["id"], {
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "team":                  team_name,
    })

    embed = discord.Embed(title="Player Assigned", color=discord.Color.green())
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Player", value=profile["username"], inline=True)
    embed.add_field(name="Team",   value=team_name,           inline=True)
    embed.add_field(name="By",     value=str(ctx.author),     inline=True)
    embed.set_footer(text=ts()[:10])

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# /unrank  (admin only)
# ─────────────────────────────────────────────
@bot.slash_command(name="unrank", description="Move a player back to Free Agent. (Admin only)")
@option("roblox_name", description="Roblox username")
async def cmd_unrank(ctx: discord.ApplicationContext, roblox_name: str):
    await ctx.defer(ephemeral=True)
    if ctx.author.id not in ALLOWED_ADMIN_IDS:
        await ctx.followup.send("You don't have permission to do that.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"Couldn't find Roblox user **{roblox_name}**.", ephemeral=True)
        return

    record   = fb_get_user(profile["id"])
    prev_team = record.get("team", "Unknown") if record else "Unknown"

    fb_upsert_user(profile["id"], {
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "team":                  "Free Agent",
    })

    embed = discord.Embed(title="Player Unranked", color=discord.Color.orange())
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Player",        value=profile["username"], inline=True)
    embed.add_field(name="Previous Team", value=prev_team,           inline=True)
    embed.add_field(name="By",            value=str(ctx.author),     inline=True)
    embed.set_footer(text=ts()[:10])

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# /suspend  (admin only)
# ─────────────────────────────────────────────
@bot.slash_command(name="suspend", description="Suspend a player. (Admin only)")
@option("roblox_name", description="Roblox username")
@option("reason",      description="Reason for suspension", required=False)
async def cmd_suspend(ctx: discord.ApplicationContext, roblox_name: str, reason: str = "No reason provided"):
    await ctx.defer(ephemeral=True)
    if ctx.author.id not in ALLOWED_ADMIN_IDS:
        await ctx.followup.send("You don't have permission to do that.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"Couldn't find Roblox user **{roblox_name}**.", ephemeral=True)
        return

    record = fb_get_user(profile["id"])
    if record and record.get("suspended"):
        await ctx.followup.send(f"**{roblox_name}** is already suspended.", ephemeral=True)
        return

    fb_upsert_user(profile["id"], {
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "suspended":             True,
        "suspended_reason":      reason,
    })

    embed = discord.Embed(title="Player Suspended", color=discord.Color.red())
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Player", value=profile["username"], inline=True)
    embed.add_field(name="By",     value=str(ctx.author),     inline=True)
    embed.add_field(name="Reason", value=reason,              inline=False)
    embed.set_footer(text=ts()[:10])

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# /unsuspend  (admin only)
# ─────────────────────────────────────────────
@bot.slash_command(name="unsuspend", description="Lift a player's suspension. (Admin only)")
@option("roblox_name", description="Roblox username")
async def cmd_unsuspend(ctx: discord.ApplicationContext, roblox_name: str):
    await ctx.defer(ephemeral=True)
    if ctx.author.id not in ALLOWED_ADMIN_IDS:
        await ctx.followup.send("You don't have permission to do that.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"Couldn't find Roblox user **{roblox_name}**.", ephemeral=True)
        return

    record = fb_get_user(profile["id"])
    if record and not record.get("suspended"):
        await ctx.followup.send(f"**{roblox_name}** is not currently suspended.", ephemeral=True)
        return

    fb_upsert_user(profile["id"], {
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "suspended":             False,
        "suspended_reason":      "",
    })

    embed = discord.Embed(title="Suspension Lifted", color=discord.Color.green())
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Player", value=profile["username"], inline=True)
    embed.add_field(name="By",     value=str(ctx.author),     inline=True)
    embed.set_footer(text=ts()[:10])

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Connected to %d guild(s)", len(bot.guilds))
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="RWA")
    )

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    log.error("Command error in /%s: %s", ctx.command, error)
    msg = "You don't have permission to run this command." if isinstance(error, discord.errors.CheckFailure) else str(error)
    try:
        await ctx.followup.send(f"Error: {msg[:200]}", ephemeral=True)
    except Exception:
        pass

# ─────────────────────────────────────────────
# KEEPALIVE  (Hugging Face Spaces)
# ─────────────────────────────────────────────
async def keepalive():
    from aiohttp import web
    from backend_verify_routes import register_routes

    async def health(_request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    register_routes(app, db)  # hooks in all /auth/* routes

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 7860).start()
    log.info("Server running on :7860")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
async def main():
    await keepalive()
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN is not set.")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
