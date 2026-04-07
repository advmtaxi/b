"""
RWA Discord Bot — Single-file version
Integrates Roblox API + Firebase Firestore
Teams: Chicago Water | LA Galaxy WC | VK Jug Dubrovnik
Default team: Free Agent
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
# ENV + LOGGING
# ─────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rwa-bot")

DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL     = os.getenv("LOG_CHANNEL_NAME", "bot-logs")
FIREBASE_CREDS  = os.getenv("FIREBASE_CREDENTIALS", "firebase.json")

# ─────────────────────────────────────────────
# TEAMS CONFIG
# ─────────────────────────────────────────────
TEAMS = {
    "Chicago Water": {
        "key":   "chicago_water",
        "emoji": "<:ChicagoWater:1491161716711755970>",
    },
    "LA Galaxy WC": {
        "key":   "la_galaxy_wc",
        "emoji": "<:LA_Galaxy_WC:1491162603609653431>",
    },
    "VK Jug Dubrovnik": {
        "key":   "vk_jug_dubrovnik",
        "emoji": "<:VKJugDubrovnik:1490635388237381664>",
    },
    "Free Agent": {
        "key":   "free_agent",
        "emoji": "🆓",
    },
}
TEAM_CHOICES = ["Chicago Water", "LA Galaxy WC", "VK Jug Dubrovnik"]

def resolve_team(name: str) -> dict | None:
    """Case-insensitive team lookup."""
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
    """Initialise Firebase from JSON file or env-injected JSON string."""
    import json, tempfile

    raw = os.getenv("FIREBASE_JSON")          # Hugging Face secret (full JSON)
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
    log.info("Firebase initialised ✅")
    return firestore.client()

db: firestore.Client = init_firebase()

# ─────────────────────────────────────────────
# ROBLOX API HELPERS
# ─────────────────────────────────────────────
ROBLOX_USERS_URL   = "https://users.roblox.com/v1"
ROBLOX_THUMBS_URL  = "https://thumbnails.roblox.com/v1"
ROBLOX_FRIENDS_URL = "https://friends.roblox.com/v1"
ROBLOX_GROUPS_URL  = "https://groups.roblox.com/v1"
ROBLOX_BADGES_URL  = "https://badges.roblox.com/v1"

async def roblox_get(session: aiohttp.ClientSession, url: str) -> dict | list | None:
    """GET a Roblox API endpoint, returning parsed JSON or None on error."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json()
            log.warning("Roblox API %s → HTTP %s", url, r.status)
            return None
    except Exception as e:
        log.error("Roblox GET error: %s", e)
        return None

async def get_roblox_id(session: aiohttp.ClientSession, username: str) -> int | None:
    """Resolve a Roblox username → user ID."""
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
    """Return full Roblox profile dict for a given username, or None."""
    uid = await get_roblox_id(session, username)
    if not uid:
        return None

    user_data = await roblox_get(session, f"{ROBLOX_USERS_URL}/users/{uid}")
    if not user_data:
        return None

    # Avatar headshot
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
# ─────────────────────────────────────────────
def fb_get_user_by_roblox(roblox_username: str) -> tuple[str | None, dict | None]:
    """Find a Firebase user record by Roblox username. Returns (doc_id, data)."""
    query = (
        db.collection("users")
        .where("roblox_username_lower", "==", roblox_username.lower())
        .limit(1)
        .stream()
    )
    for doc in query:
        return doc.id, doc.to_dict()
    return None, None

def fb_upsert_user(discord_id: str, data: dict):
    """Create or merge a user record in Firestore under users/{discord_id}."""
    ref = db.collection("users").document(str(discord_id))
    data["updated_at"] = ts()
    ref.set(data, merge=True)

def fb_add_history(discord_id: str, entry: dict):
    """Append a history entry to users/{discord_id}/history."""
    entry["timestamp"] = ts()
    db.collection("users").document(str(discord_id)).collection("history").add(entry)

def fb_get_history(discord_id: str) -> list:
    docs = (
        db.collection("users")
        .document(str(discord_id))
        .collection("history")
        .order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(20)
        .stream()
    )
    return [d.to_dict() for d in docs]

def fb_get_team_members(team_name: str) -> list:
    query = db.collection("users").where("team", "==", team_name).stream()
    return [d.to_dict() for d in query]

def fb_get_staff_roles(guild_id: str) -> list:
    doc = db.collection("guild_config").document(str(guild_id)).get()
    if doc.exists:
        return doc.to_dict().get("staff_roles", [])
    return []

def fb_add_staff_role(guild_id: str, role_name: str):
    ref = db.collection("guild_config").document(str(guild_id))
    doc = ref.get()
    roles = doc.to_dict().get("staff_roles", []) if doc.exists else []
    if role_name.lower() not in [r.lower() for r in roles]:
        roles.append(role_name)
    ref.set({"staff_roles": roles, "updated_at": ts()}, merge=True)

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Bot(intents=intents)

# ─────────────────────────────────────────────
# PERMISSION GUARD
# ─────────────────────────────────────────────
async def has_staff_permission(ctx: discord.ApplicationContext) -> bool:
    """Returns True if the user is admin or holds a configured staff role."""
    if ctx.author.guild_permissions.administrator:
        return True
    staff_roles = fb_get_staff_roles(str(ctx.guild_id))
    user_role_names = [r.name.lower() for r in ctx.author.roles]
    return any(sr.lower() in user_role_names for sr in staff_roles)

# ─────────────────────────────────────────────
# LOG HELPER
# ─────────────────────────────────────────────
async def send_log(guild: discord.Guild, embed: discord.Embed):
    """Post an embed to the configured log channel."""
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL)
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /profile
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
@bot.slash_command(name="profile", description="View a Roblox profile and store it in the database.")
@option("roblox_user", description="Roblox username to look up")
async def cmd_profile(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_user)

    if not profile:
        await ctx.followup.send(f"❌ Couldn't find Roblox user **{roblox_user}**.", ephemeral=True)
        return

    discord_id = str(ctx.author.id)
    # Load existing record to preserve team/rank
    existing_ref = db.collection("users").document(discord_id).get()
    existing      = existing_ref.to_dict() if existing_ref.exists else {}

    record = {
        "discord_id":            discord_id,
        "discord_username":      str(ctx.author),
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "roblox_display":        profile["display"],
        "roblox_join_date":      profile["created"],
        "banned_on_roblox":      profile["banned"],
        "team":                  existing.get("team", "Free Agent"),
        "rank":                  existing.get("rank", ""),
        "suspended":             existing.get("suspended", False),
        "avatar_url":            profile["avatar_url"],
    }
    fb_upsert_user(discord_id, record)

    team_info = TEAMS.get(record["team"], TEAMS["Free Agent"])
    joined    = profile["created"][:10] if profile["created"] else "Unknown"

    embed = discord.Embed(
        title=f"{profile['username']}  ({profile['display']})",
        url=f"https://www.roblox.com/users/{profile['id']}/profile",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="🆔 Roblox ID",   value=str(profile["id"]),    inline=True)
    embed.add_field(name="📅 Joined",       value=joined,               inline=True)
    embed.add_field(name="🚫 Banned",       value=str(profile["banned"]), inline=True)
    embed.add_field(
        name="⚽ Team",
        value=f"{team_info['emoji']} {record['team']}",
        inline=True,
    )
    embed.add_field(name="🏅 Rank",        value=record["rank"] or "None", inline=True)
    embed.add_field(
        name="⛔ Suspended",
        value="Yes" if record["suspended"] else "No",
        inline=True,
    )
    if profile["description"]:
        embed.add_field(name="📝 Bio", value=profile["description"][:200], inline=False)
    embed.set_footer(text=f"Requested by {ctx.author} • Stored in Firebase")

    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /team view
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
team_group = bot.create_group("team", "Team management commands")

@team_group.command(name="view", description="View all members of a team.")
@option("team_name", description="Team to view", choices=TEAM_CHOICES)
async def cmd_team_view(ctx: discord.ApplicationContext, team_name: str):
    await ctx.defer()
    team = resolve_team(team_name)
    members = fb_get_team_members(team_name)

    embed = discord.Embed(
        title=f"{team['emoji']}  {team['name']}",
        color=discord.Color.green(),
        description=f"**{len(members)}** registered member(s)",
    )

    if members:
        ranked   = [m for m in members if m.get("rank")]
        unranked = [m for m in members if not m.get("rank")]

        if ranked:
            ranked.sort(key=lambda x: x.get("rank", ""))
            lines = "\n".join(
                f"• **{m['roblox_username']}** — {m['rank']}" for m in ranked[:20]
            )
            embed.add_field(name="🏅 Ranked", value=lines or "None", inline=False)
        if unranked:
            lines = "\n".join(f"• {m['roblox_username']}" for m in unranked[:20])
            embed.add_field(name="👤 Unranked", value=lines or "None", inline=False)
    else:
        embed.description = "No members found for this team."

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /rank
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
@bot.slash_command(name="rank", description="Assign a rank and team to a Roblox user. (Staff only)")
@option("roblox_name", description="Roblox username")
@option("team_name",   description="Team to assign",  choices=TEAM_CHOICES)
@option("rank_title",  description="Rank/role title (e.g. Starter, Pro, Captain)")
async def cmd_rank(
    ctx: discord.ApplicationContext,
    roblox_name: str,
    team_name: str,
    rank_title: str,
):
    await ctx.defer(ephemeral=True)
    if not await has_staff_permission(ctx):
        await ctx.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"❌ Roblox user **{roblox_name}** not found.", ephemeral=True)
        return

    doc_id, existing = fb_get_user_by_roblox(roblox_name)
    discord_id = doc_id or f"roblox_{profile['id']}"

    record = {
        "discord_id":            discord_id,
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "team":                  team_name,
        "rank":                  rank_title,
        "avatar_url":            profile["avatar_url"],
    }
    fb_upsert_user(discord_id, record)
    fb_add_history(discord_id, {
        "action":     "rank",
        "team":       team_name,
        "rank":       rank_title,
        "by":         str(ctx.author),
        "by_discord": str(ctx.author.id),
    })

    team = TEAMS.get(team_name, TEAMS["Free Agent"])
    embed = discord.Embed(
        title="✅ Rank Assigned",
        color=discord.Color.green(),
    )
    embed.add_field(name="👤 Player",  value=profile["username"], inline=True)
    embed.add_field(name="⚽ Team",    value=f"{team['emoji']} {team_name}", inline=True)
    embed.add_field(name="🏅 Rank",   value=rank_title,          inline=True)
    embed.add_field(name="📋 By",     value=str(ctx.author),     inline=True)
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.set_footer(text=f"Logged to Firebase • {ts()[:10]}")

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /unrank  (resets team → Free Agent)
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
@bot.slash_command(name="unrank", description="Remove a user's rank and set them to Free Agent. (Staff only)")
@option("roblox_name", description="Roblox username to unrank")
async def cmd_unrank(ctx: discord.ApplicationContext, roblox_name: str):
    await ctx.defer(ephemeral=True)
    if not await has_staff_permission(ctx):
        await ctx.followup.send("❌ You don't have permission.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"❌ Roblox user **{roblox_name}** not found.", ephemeral=True)
        return

    doc_id, existing = fb_get_user_by_roblox(roblox_name)
    discord_id = doc_id or f"roblox_{profile['id']}"

    prev_team = existing.get("team", "Free Agent") if existing else "Unknown"
    prev_rank = existing.get("rank", "None") if existing else "None"

    fb_upsert_user(discord_id, {
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "team":                  "Free Agent",
        "rank":                  "",
    })
    fb_add_history(discord_id, {
        "action":     "unrank",
        "prev_team":  prev_team,
        "prev_rank":  prev_rank,
        "by":         str(ctx.author),
        "by_discord": str(ctx.author.id),
    })

    embed = discord.Embed(
        title="🔄 User Unranked → Free Agent",
        color=discord.Color.orange(),
    )
    embed.add_field(name="👤 Player",       value=profile["username"], inline=True)
    embed.add_field(name="⚽ Previous Team", value=prev_team,          inline=True)
    embed.add_field(name="🏅 Previous Rank", value=prev_rank,          inline=True)
    embed.add_field(name="📋 By",           value=str(ctx.author),    inline=True)
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.set_footer(text=f"Now: 🆓 Free Agent • {ts()[:10]}")

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /addstaffrole
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
@bot.slash_command(name="addstaffrole", description="Add a Discord role that can run staff commands. (Admin only)")
@option("role_name", description="Discord role name to grant staff access")
async def cmd_addstaffrole(ctx: discord.ApplicationContext, role_name: str):
    await ctx.defer(ephemeral=True)
    if not ctx.author.guild_permissions.administrator:
        await ctx.followup.send("❌ Only server admins can configure staff roles.", ephemeral=True)
        return

    # Validate the role actually exists in this guild
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.followup.send(f"❌ Role **{role_name}** not found in this server.", ephemeral=True)
        return

    fb_add_staff_role(str(ctx.guild_id), role_name)

    embed = discord.Embed(
        title="✅ Staff Role Added",
        description=f"**{role_name}** can now use staff commands.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Set by {ctx.author} • {ts()[:10]}")
    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /suspend
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
@bot.slash_command(name="suspend", description="Suspend a Roblox user and record it. (Staff only)")
@option("roblox_name", description="Roblox username to suspend")
@option("reason",      description="Reason for suspension", required=False)
async def cmd_suspend(
    ctx: discord.ApplicationContext,
    roblox_name: str,
    reason: str = "No reason provided",
):
    await ctx.defer(ephemeral=True)
    if not await has_staff_permission(ctx):
        await ctx.followup.send("❌ You don't have permission.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"❌ Roblox user **{roblox_name}** not found.", ephemeral=True)
        return

    doc_id, existing = fb_get_user_by_roblox(roblox_name)
    discord_id = doc_id or f"roblox_{profile['id']}"

    if existing and existing.get("suspended"):
        await ctx.followup.send(f"⚠️ **{roblox_name}** is already suspended.", ephemeral=True)
        return

    fb_upsert_user(discord_id, {
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "suspended":             True,
        "suspended_reason":      reason,
        "suspended_by":          str(ctx.author.id),
        "suspended_at":          ts(),
    })
    fb_add_history(discord_id, {
        "action":     "suspend",
        "reason":     reason,
        "by":         str(ctx.author),
        "by_discord": str(ctx.author.id),
    })

    embed = discord.Embed(
        title="⛔ User Suspended",
        color=discord.Color.red(),
    )
    embed.add_field(name="👤 Player",  value=profile["username"], inline=True)
    embed.add_field(name="📋 By",     value=str(ctx.author),     inline=True)
    embed.add_field(name="📝 Reason", value=reason,              inline=False)
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.set_footer(text=f"Logged to Firebase • {ts()[:10]}")

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /unsuspend
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
@bot.slash_command(name="unsuspend", description="Lift a suspension from a Roblox user. (Staff only)")
@option("roblox_name", description="Roblox username to unsuspend")
async def cmd_unsuspend(ctx: discord.ApplicationContext, roblox_name: str):
    await ctx.defer(ephemeral=True)
    if not await has_staff_permission(ctx):
        await ctx.followup.send("❌ You don't have permission.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_name)

    if not profile:
        await ctx.followup.send(f"❌ Roblox user **{roblox_name}** not found.", ephemeral=True)
        return

    doc_id, existing = fb_get_user_by_roblox(roblox_name)
    discord_id = doc_id or f"roblox_{profile['id']}"

    if existing and not existing.get("suspended"):
        await ctx.followup.send(f"⚠️ **{roblox_name}** is not currently suspended.", ephemeral=True)
        return

    fb_upsert_user(discord_id, {
        "roblox_id":             profile["id"],
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "suspended":             False,
        "suspended_reason":      "",
        "unsuspended_by":        str(ctx.author.id),
        "unsuspended_at":        ts(),
    })
    fb_add_history(discord_id, {
        "action":     "unsuspend",
        "by":         str(ctx.author),
        "by_discord": str(ctx.author.id),
    })

    embed = discord.Embed(
        title="✅ Suspension Lifted",
        color=discord.Color.green(),
    )
    embed.add_field(name="👤 Player", value=profile["username"], inline=True)
    embed.add_field(name="📋 By",    value=str(ctx.author),     inline=True)
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.set_footer(text=f"Logged to Firebase • {ts()[:10]}")

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   /altcheck
# ════════════════════════════════════════════
# ─────────────────────────────────────────────
@bot.slash_command(name="altcheck", description="Run an alt-account check on a Roblox user.")
@option("roblox_user", description="Roblox username to check")
async def cmd_altcheck(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    if not await has_staff_permission(ctx):
        await ctx.followup.send("❌ You don't have permission.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_user)
        if not profile:
            await ctx.followup.send(f"❌ Roblox user **{roblox_user}** not found.", ephemeral=True)
            return

        uid      = profile["id"]
        badges   = await get_roblox_badges(session, uid)
        friends  = await get_roblox_friends(session, uid)
        groups   = await get_roblox_groups(session, uid)

    # ── ALT DETECTION FLAGS ──────────────────
    flags: list[str] = []
    created = profile.get("created", "")
    if created:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
        if age_days < 30:
            flags.append(f"🚩 Account created **{age_days}** days ago (very new)")
    if len(badges) < 3:
        flags.append(f"🚩 Only **{len(badges)}** badge(s) (low activity)")
    if len(friends) < 5:
        flags.append(f"🚩 Only **{len(friends)}** friend(s)")
    if not groups:
        flags.append("🚩 No group memberships")
    if profile.get("banned"):
        flags.append("🚩 Account is **banned** on Roblox")

    flag_level = "🔴 HIGH RISK" if len(flags) >= 3 else ("🟡 MEDIUM" if len(flags) >= 1 else "🟢 CLEAN")

    # ── STORE IN FIREBASE ────────────────────
    doc_id, _ = fb_get_user_by_roblox(roblox_user)
    discord_id = doc_id or f"roblox_{uid}"
    fb_upsert_user(discord_id, {
        "roblox_id":             uid,
        "roblox_username":       profile["username"],
        "roblox_username_lower": profile["username"].lower(),
        "altcheck": {
            "flags":        flags,
            "badge_count":  len(badges),
            "friend_count": len(friends),
            "group_count":  len(groups),
            "flag_level":   flag_level,
            "checked_by":   str(ctx.author.id),
            "checked_at":   ts(),
        },
    })

    # ── EMBED ────────────────────────────────
    embed = discord.Embed(
        title=f"🔍 Alt-Check: {profile['username']}",
        color=discord.Color.red() if "HIGH" in flag_level else (
            discord.Color.yellow() if "MEDIUM" in flag_level else discord.Color.green()
        ),
    )
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="🔎 Risk Level",    value=flag_level,       inline=False)
    embed.add_field(name="🏅 Badges",        value=str(len(badges)),  inline=True)
    embed.add_field(name="👥 Friends",       value=str(len(friends)), inline=True)
    embed.add_field(name="🏠 Groups",        value=str(len(groups)),  inline=True)

    if flags:
        embed.add_field(name="⚠️ Flags", value="\n".join(flags), inline=False)
    else:
        embed.add_field(name="✅ No flags", value="Account looks legitimate.", inline=False)

    if groups:
        gnames = ", ".join(g["group"]["name"] for g in groups[:5])
        embed.add_field(name="🏠 Group List", value=gnames, inline=False)

    embed.set_footer(text=f"Checked by {ctx.author} • Saved to Firebase")
    await ctx.followup.send(embed=embed)
    await send_log(ctx.guild, embed)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   EXTRA COMMANDS
# ════════════════════════════════════════════
# ─────────────────────────────────────────────

# /leaderboard
@bot.slash_command(name="leaderboard", description="Top ranked members of a team.")
@option("team_name", description="Team to show", choices=TEAM_CHOICES)
async def cmd_leaderboard(ctx: discord.ApplicationContext, team_name: str):
    await ctx.defer()
    members = fb_get_team_members(team_name)
    ranked  = [m for m in members if m.get("rank")]
    ranked.sort(key=lambda x: x.get("rank", ""))

    team   = resolve_team(team_name)
    embed  = discord.Embed(
        title=f"{team['emoji']}  {team_name} Leaderboard",
        color=discord.Color.gold(),
    )
    if ranked:
        lines = "\n".join(
            f"**{i+1}.** {m['roblox_username']} — {m['rank']}"
            for i, m in enumerate(ranked[:20])
        )
        embed.description = lines
    else:
        embed.description = "No ranked members yet."
    embed.set_footer(text=f"Total ranked: {len(ranked)}")
    await ctx.followup.send(embed=embed)

# /badges
@bot.slash_command(name="badges", description="List Roblox badges for a user.")
@option("roblox_user", description="Roblox username")
async def cmd_badges(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        uid = await get_roblox_id(session, roblox_user)
        if not uid:
            await ctx.followup.send(f"❌ User **{roblox_user}** not found.", ephemeral=True)
            return
        badges = await get_roblox_badges(session, uid)

    embed = discord.Embed(
        title=f"🏅 Badges — {roblox_user}",
        color=discord.Color.blurple(),
        description=f"{len(badges)} badge(s) found (showing up to 10)",
    )
    for b in badges[:10]:
        embed.add_field(name=b.get("name", "Unknown"), value=b.get("description", "")[:80] or "—", inline=True)
    await ctx.followup.send(embed=embed)

# /friends
@bot.slash_command(name="friends", description="List Roblox friends for a user.")
@option("roblox_user", description="Roblox username")
async def cmd_friends(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        uid = await get_roblox_id(session, roblox_user)
        if not uid:
            await ctx.followup.send(f"❌ User **{roblox_user}** not found.", ephemeral=True)
            return
        friends = await get_roblox_friends(session, uid)

    embed = discord.Embed(
        title=f"👥 Friends — {roblox_user}",
        color=discord.Color.blurple(),
        description=f"{len(friends)} friend(s) total (showing up to 15)",
    )
    if len(friends) > 200:
        embed.add_field(name="⚠️ Warning", value="High friend count — possible bot/alt activity.", inline=False)
    lines = "\n".join(f"• {f['name']}" for f in friends[:15])
    embed.add_field(name="Friends", value=lines or "None", inline=False)
    await ctx.followup.send(embed=embed)

# /history
@bot.slash_command(name="history", description="Show rank/suspend history for a Roblox user.")
@option("roblox_user", description="Roblox username")
async def cmd_history(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    doc_id, _ = fb_get_user_by_roblox(roblox_user)
    if not doc_id:
        await ctx.followup.send(f"❌ No Firebase record for **{roblox_user}**.", ephemeral=True)
        return

    history = fb_get_history(doc_id)
    embed   = discord.Embed(
        title=f"📋 History — {roblox_user}",
        color=discord.Color.blurple(),
    )
    if history:
        for entry in history[:10]:
            action = entry.get("action", "?").upper()
            when   = entry.get("timestamp", "")[:10]
            by     = entry.get("by", "Unknown")
            detail = ""
            if action == "RANK":
                detail = f"→ {entry.get('team')} / {entry.get('rank')}"
            elif action == "UNRANK":
                detail = f"← was {entry.get('prev_team')} / {entry.get('prev_rank')}"
            elif action == "SUSPEND":
                detail = f"Reason: {entry.get('reason', '')}"
            embed.add_field(
                name=f"[{when}] {action}",
                value=f"{detail}\nBy: {by}",
                inline=False,
            )
    else:
        embed.description = "No history found."
    await ctx.followup.send(embed=embed)

# /teamstats
@bot.slash_command(name="teamstats", description="Stats overview for a team.")
@option("team_name", description="Team name", choices=TEAM_CHOICES)
async def cmd_teamstats(ctx: discord.ApplicationContext, team_name: str):
    await ctx.defer()
    members   = fb_get_team_members(team_name)
    ranked    = [m for m in members if m.get("rank")]
    suspended = [m for m in members if m.get("suspended")]
    team      = resolve_team(team_name)

    embed = discord.Embed(
        title=f"{team['emoji']}  {team_name} — Team Stats",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="👥 Total Members",  value=str(len(members)),   inline=True)
    embed.add_field(name="🏅 Ranked",         value=str(len(ranked)),    inline=True)
    embed.add_field(name="⛔ Suspended",      value=str(len(suspended)), inline=True)

    ranks: dict[str, int] = {}
    for m in ranked:
        r = m.get("rank", "Unknown")
        ranks[r] = ranks.get(r, 0) + 1
    if ranks:
        breakdown = "\n".join(f"• {r}: **{c}**" for r, c in sorted(ranks.items()))
        embed.add_field(name="📊 Rank Breakdown", value=breakdown, inline=False)

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.followup.send(embed=embed)

# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info("✅ Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("🔗 Connected to %d guild(s)", len(bot.guilds))
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="RWA Rankings 🏆",
        )
    )

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    """Global slash command error handler."""
    log.error("Command error in /%s: %s", ctx.command, error)
    msg = str(error)
    if isinstance(error, discord.errors.CheckFailure):
        msg = "You don't have permission to run this command."
    try:
        await ctx.followup.send(f"❌ Error: {msg[:200]}", ephemeral=True)
    except Exception:
        pass

# ─────────────────────────────────────────────
# KEEPALIVE  (for Hugging Face Spaces)
# ─────────────────────────────────────────────
async def keepalive():
    """Tiny HTTP server so HF Spaces doesn't think the app is dead."""
    from aiohttp import web

    async def health(_request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 7860)
    await site.start()
    log.info("Keepalive server running on :7860")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
async def main():
    await keepalive()
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN is not set! Check your .env / Secrets.")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
