import aiohttp
from aiohttp import web
from aiohttp_session import setup as session_setup, get_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import base64
import os
import json
from urllib.parse import urlencode

# Constants
DISCORD_AUTH_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_URL = "https://discord.com/api/users/@me"
DISCORD_REDIRECT = "https://verycooldaysman-rwa.hf.space/auth/discord/callback"

ROBLOX_AUTH_URL = "https://apis.roblox.com/oauth/v1/authorize"
ROBLOX_TOKEN_URL = "https://apis.roblox.com/oauth/v1/token"
ROBLOX_REDIRECT = "https://verycooldaysman-rwa.hf.space/auth/roblox/callback"
ROBLOX_CLIENT_ID = os.getenv("ROBLOX_CLIENT_ID")
ROBLOX_CLIENT_SECRET = os.getenv("ROBLOX_CLIENT_SECRET")

FRONTEND_URL = "https://rwaverify.vercel.app"

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    
    resp.headers["Access-Control-Allow-Origin"] = FRONTEND_URL
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

async def discord_login(request):
    params = urlencode({
        "client_id": os.getenv("DISCORD_CLIENT_ID"),
        "redirect_uri": DISCORD_REDIRECT,
        "response_type": "code",
        "scope": "identify",
    })
    raise web.HTTPFound(f"{DISCORD_AUTH_URL}?{params}")

async def discord_callback(request):
    code = request.rel_url.query.get("code")
    if not code:
        raise web.HTTPBadRequest(reason="Missing code")

    async with aiohttp.ClientSession() as s:
        async with s.post(DISCORD_TOKEN_URL, data={
            "client_id": os.getenv("DISCORD_CLIENT_ID"),
            "client_secret": os.getenv("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT,
        }) as r:
            tokens = await r.json()
            if "access_token" not in tokens:
                raise web.HTTPBadRequest(reason="Failed to exchange Discord code")
            
            async with s.get(DISCORD_API_URL,
                             headers={"Authorization": f"Bearer {tokens['access_token']}"}) as user_r:
                user = await user_r.json()

    session = await get_session(request)
    session["discord_id"] = str(user["id"])
    session["discord_username"] = user["username"]
    
    # Redirect back to frontend verify step 2
    raise web.HTTPFound(f"{FRONTEND_URL}/verify?step=2")

async def roblox_login(request):
    session = await get_session(request)
    if not session.get("discord_id"):
        raise web.HTTPUnauthorized(reason="Complete Discord login first")
    
    params = urlencode({
        "client_id": ROBLOX_CLIENT_ID,
        "redirect_uri": ROBLOX_REDIRECT,
        "response_type": "code",
        "scope": "openid profile",
    })
    raise web.HTTPFound(f"{ROBLOX_AUTH_URL}?{params}")

async def roblox_callback(request, db):
    code = request.rel_url.query.get("code")
    if not code:
        raise web.HTTPBadRequest(reason="Missing code")
        
    session = await get_session(request)
    if not session.get("discord_id"):
        raise web.HTTPUnauthorized(reason="Session expired or invalid")

    async with aiohttp.ClientSession() as s:
        async with s.post(ROBLOX_TOKEN_URL, data={
            "client_id": ROBLOX_CLIENT_ID,
            "client_secret": ROBLOX_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": ROBLOX_REDIRECT,
        }) as r:
            tokens = await r.json()
            if "id_token" not in tokens:
                raise web.HTTPBadRequest(reason="Failed to exchange Roblox code")

    # Decode id_token JWT
    payload = tokens["id_token"].split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    roblox_id = str(claims["sub"])
    roblox_username = claims.get("preferred_username", "unknown")

    # Load existing Firebase record to preserve team/suspended
    doc_ref = db.collection("users").document(roblox_id)
    existing_doc = doc_ref.get()
    existing = existing_doc.to_dict() if existing_doc.exists else {}

    # Update/Create record
    doc_ref.set({
        "roblox_id":             int(roblox_id),
        "roblox_username":       roblox_username,
        "roblox_username_lower": roblox_username.lower(),
        "discord_id":            session.get("discord_id"),
        "discord_username":      session.get("discord_username"),
        "verified":              True,
        "team":                  existing.get("team", "Free Agent"),
        "suspended":             existing.get("suspended", False),
        "suspended_reason":      existing.get("suspended_reason", ""),
    }, merge=True)

    session["roblox_id"] = roblox_id
    session["roblox_username"] = roblox_username
    
    raise web.HTTPFound(f"{FRONTEND_URL}/success")

async def auth_me(request):
    session = await get_session(request)
    if not session.get("discord_id"):
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    return web.json_response({
        "discord_id":       session.get("discord_id"),
        "discord_username": session.get("discord_username"),
        "roblox_id":        session.get("roblox_id"),
        "roblox_username":  session.get("roblox_username"),
    })

async def auth_logout(request):
    session = await get_session(request)
    session.clear()
    return web.Response(text="OK")

def register_routes(app, db):
    # Session setup
    secret_str = os.getenv("SESSION_SECRET")
    if not secret_str:
        # Fallback for dev, but should be set in env
        secret_str = base64.urlsafe_b64encode(os.urandom(32)).decode()
    
    secret = base64.urlsafe_b64decode(secret_str)
    session_setup(app, EncryptedCookieStorage(
        secret, 
        cookie_name="rwa_session", 
        max_age=3600,
        samesite="None",
        secure=True
    ))
    
    # Middleware
    app.middlewares.append(cors_middleware)
    
    # Routes
    app.router.add_get("/auth/discord", discord_login)
    app.router.add_get("/auth/discord/callback", discord_callback)
    app.router.add_get("/auth/roblox", roblox_login)
    app.router.add_get("/auth/roblox/callback", lambda r: roblox_callback(r, db))
    app.router.add_get("/auth/me", auth_me)
    app.router.add_post("/auth/logout", auth_logout)
