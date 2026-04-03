"""
Discord bot for the cheat authentication system.
Uses HTTP calls to the FastAPI backend — Railway compatible.

Required env vars (set in Railway dashboard):
  DISCORD_TOKEN   – your bot token
  GUILD_ID        – your server's guild ID
  BUYER_ROLE_ID   – role ID to assign on /create
  BACKEND_URL     – full URL of your backend Railway service, e.g. https://auth-backend.up.railway.app
  ADMIN_SECRET    – must match backend ADMIN_SECRET
"""

import os
import aiohttp
import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import tasks

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])
BUYER_ROLE_ID = int(os.environ["BUYER_ROLE_ID"])
BACKEND_URL   = os.environ["BACKEND_URL"].rstrip("/")
ADMIN_SECRET  = os.environ["ADMIN_SECRET"]

# ── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True

class AuthBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"[Bot] Slash commands synced to guild {GUILD_ID}")
        poll_actions.start()

    async def on_ready(self):
        print(f"[Bot] Logged in as {self.user} ({self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="auth system")
        )

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

bot = AuthBot()

# ── HTTP helpers ───────────────────────────────────────────────────────────────
async def api_post(path: str, data: dict) -> dict:
    url = f"{BACKEND_URL}{path}"
    async with bot.session.post(url, json=data) as r:
        return await r.json(), r.status

async def api_get(path: str, params: dict = {}) -> dict:
    url = f"{BACKEND_URL}{path}"
    async with bot.session.get(url, params=params) as r:
        return await r.json(), r.status

# ── /create ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="create", description="Redeem a license key and create your account")
@app_commands.describe(
    key="Your license key (CHEAT-XXXXX-XXXXX-XXXXX-XXXXX)",
    username="Username for the cheat client (3-20 chars)",
    password="Password for the cheat client (min 6 chars)"
)
async def cmd_create(interaction: discord.Interaction, key: str, username: str, password: str):
    await interaction.response.defer(ephemeral=True)

    data, status = await api_post("/api/redeem_key", {
        "key": key.strip().upper(),
        "username": username.strip(),
        "password": password,
        "discord_id": str(interaction.user.id),
        "discord_name": str(interaction.user),
    })

    if status != 200:
        detail = data.get("detail", "Unknown error")
        embed = discord.Embed(title="❌ Failed", description=detail, color=0xef4444)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Assign Buyer role
    try:
        guild  = interaction.guild
        member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
        role   = guild.get_role(BUYER_ROLE_ID)
        if role and member:
            await member.add_roles(role, reason="License key redeemed")
    except Exception as e:
        print(f"[Bot] Role assign failed: {e}")

    expiry_str = data.get("expiry", "Lifetime")
    embed = discord.Embed(
        title="✅ Account Created",
        description="Your cheat client account is ready.",
        color=0x00e5ff
    )
    embed.add_field(name="Username", value=f"`{data['username']}`", inline=True)
    embed.add_field(name="Password", value=f"||`{password}`||", inline=True)
    embed.add_field(name="Expiry",   value=f"📅 {expiry_str}" if expiry_str != "Lifetime" else "♾ Lifetime", inline=True)
    embed.add_field(name="HWID", value="🔓 Unbound — locks on first cheat login", inline=False)
    embed.set_footer(text="Keep your credentials safe • Do not share this message")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /panel ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="panel", description="View your account info")
async def cmd_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data, status = await api_get("/api/user_info", {"discord_id": str(interaction.user.id)})

    if status == 404:
        embed = discord.Embed(
            title="❌ No Account",
            description="You don't have an account yet.\nUse `/create <key> <username> <password>` to get started.",
            color=0xef4444
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if status != 200:
        await interaction.followup.send(embed=discord.Embed(title="❌ Error", description="Could not fetch account.", color=0xef4444), ephemeral=True)
        return

    is_banned  = data.get("is_banned", False)
    expiry     = data.get("expiry_date", "—")
    lifetime   = data.get("is_lifetime", False)
    last_login = data.get("last_login") or "Never"
    created    = data.get("created_at", "—")

    # Format expiry
    if lifetime:
        expiry_display = "♾ Lifetime"
    elif expiry and expiry != "Lifetime":
        try:
            exp_dt    = datetime.fromisoformat(expiry)
            remaining = (exp_dt - datetime.utcnow()).days
            expiry_display = f"📅 {expiry[:10]} ({'expired' if remaining < 0 else f'{remaining}d left'})"
        except Exception:
            expiry_display = expiry
    else:
        expiry_display = expiry

    embed = discord.Embed(
        title="👤 Your Account",
        color=0xef4444 if is_banned else 0x00e5ff
    )
    embed.add_field(name="Status",   value="🔴 **BANNED**" if is_banned else "🟢 Active", inline=True)
    embed.add_field(name="Username", value=f"`{data.get('username','—')}`", inline=True)
    embed.add_field(name="Expiry",   value=expiry_display, inline=True)
    embed.add_field(name="HWID",     value="🔒 Device locked" if data.get("hwid_bound") else "🔓 Not bound", inline=True)
    embed.add_field(name="Last Login", value=last_login[:10] if "T" in str(last_login) else last_login, inline=True)
    embed.add_field(name="Member Since", value=created[:10] if created else "—", inline=True)
    embed.set_footer(text="Contact support to reset HWID or extend subscription")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /checkkey ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="checkkey", description="Check if a license key is valid")
@app_commands.describe(key="The license key to check")
async def cmd_checkkey(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)
    # We just try to get all keys and match — or use a quick trick via redeem with a dummy
    # Better: add a lightweight check endpoint. For now use admin/keys filtered client-side.
    data, status = await api_get("/admin/keys", {"admin_secret": ADMIN_SECRET})
    if status != 200:
        await interaction.followup.send("Could not check key.", ephemeral=True)
        return

    match = next((k for k in data if k["key"] == key.strip().upper()), None)
    if not match:
        embed = discord.Embed(title="❌ Not Found", description="That key does not exist.", color=0xef4444)
    elif match["is_used"]:
        embed = discord.Embed(title="🔑 Key Status", color=0xef4444)
        embed.add_field(name="Key",    value=f"`{match['key']}`", inline=False)
        embed.add_field(name="Status", value="❌ Already redeemed", inline=True)
        if match.get("username"):
            embed.add_field(name="Used by", value=match["username"], inline=True)
    else:
        embed = discord.Embed(title="🔑 Key Status", color=0x10b981)
        embed.add_field(name="Key",    value=f"`{match['key']}`", inline=False)
        embed.add_field(name="Status", value="✅ Available", inline=True)
        embed.add_field(name="Type",   value="♾ Lifetime" if match["is_lifetime"] else f"📅 {match['expiry_days']} days", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Bot action poll (for panel's "Send Message" / "Manage Role") ──────────────
@tasks.loop(seconds=5)
async def poll_actions():
    try:
        data, status = await api_get("/admin/bot/poll", {"admin_secret": ADMIN_SECRET})
        if status != 200 or not isinstance(data, list):
            return
        for action in data:
            try:
                if action["type"] == "send_message":
                    ch = bot.get_channel(int(action["channel_id"]))
                    if ch:
                        await ch.send(action["message"])
                    else:
                        print(f"[Bot] Channel {action['channel_id']} not found")

                elif action["type"] == "manage_role":
                    guild  = bot.get_guild(GUILD_ID)
                    member = guild.get_member(int(action["discord_id"])) or await guild.fetch_member(int(action["discord_id"]))
                    role   = guild.get_role(int(action["role_id"]))
                    if member and role:
                        if action["action"] == "add":
                            await member.add_roles(role)
                        else:
                            await member.remove_roles(role)
            except Exception as e:
                print(f"[Bot] Action error: {e}")
    except Exception as e:
        print(f"[Bot] Poll error: {e}")

@poll_actions.before_loop
async def before_poll():
    await bot.wait_until_ready()


if __name__ == "__main__":
    print("[Bot] Starting...")
    bot.run(DISCORD_TOKEN)
