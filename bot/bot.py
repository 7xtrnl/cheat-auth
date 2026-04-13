"""
Discord bot for the cheat authentication system.
Uses HTTP calls to the FastAPI backend — Railway compatible.

Required env vars (set in Railway dashboard):
  DISCORD_TOKEN   – your bot token
  GUILD_ID        – your server's guild ID
  BUYER_ROLE_ID   – role ID to assign on /create (default: 1489193048083791882)
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
from discord.ext import commands

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])
BUYER_ROLE_ID = int(os.environ.get("BUYER_ROLE_ID", "1489193048083791882"))
BACKEND_URL   = os.environ["BACKEND_URL"].rstrip("/")
ADMIN_SECRET  = os.environ["ADMIN_SECRET"]
ADMIN_ROLE_ID = os.environ.get("ADMIN_ROLE_ID")  # Optional: Admin role for restricted commands

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

    def is_admin(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions"""
        if ADMIN_ROLE_ID:
            role = interaction.guild.get_role(int(ADMIN_ROLE_ID))
            return role in interaction.user.roles if role else False
        # Fallback to checking if user has manage permissions
        return interaction.user.guild_permissions.manage_guild

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

    # Assign Buyer role (hardcoded default + env override both supported)
    try:
        guild  = interaction.guild
        member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
        role   = guild.get_role(BUYER_ROLE_ID)
        if role is None:
            # Role not in cache, try fetching all roles
            await guild.fetch_roles()
            role = guild.get_role(BUYER_ROLE_ID)
        if role and member:
            await member.add_roles(role, reason="License key redeemed")
            print(f"[Bot] Assigned role {BUYER_ROLE_ID} to {interaction.user}")
        else:
            print(f"[Bot] Could not find role {BUYER_ROLE_ID} or member {interaction.user}")
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


# ── /resethwid ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="resethwid", description="Reset your HWID (unlink your current device)")
async def cmd_resethwid(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    data, status = await api_post("/api/user/reset_hwid", {
        "discord_id": str(interaction.user.id)
    })
    
    if status != 200:
        detail = data.get("detail", "Failed to reset HWID")
        embed = discord.Embed(title="❌ HWID Reset Failed", description=detail, color=0xef4444)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    embed = discord.Embed(
        title="✅ HWID Reset Successfully",
        description="Your device has been unlinked. You can now log in from a new device.",
        color=0x00e5ff
    )
    embed.add_field(name="Note", value="Your HWID will lock again on your next login.", inline=False)
    embed.set_footer(text="Use this command if you changed PCs or reinstalled Windows")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /resetpassword ──────────────────────────────────────────────────────────────
@bot.tree.command(name="resetpassword", description="Reset your account password")
@app_commands.describe(
    new_password="Your new password (min 6 characters)"
)
async def cmd_resetpassword(interaction: discord.Interaction, new_password: str):
    await interaction.response.defer(ephemeral=True)
    
    if len(new_password) < 6:
        embed = discord.Embed(title="❌ Invalid Password", description="Password must be at least 6 characters.", color=0xef4444)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    data, status = await api_post("/api/user/reset_password", {
        "discord_id": str(interaction.user.id),
        "new_password": new_password
    })
    
    if status != 200:
        detail = data.get("detail", "Failed to reset password")
        embed = discord.Embed(title="❌ Password Reset Failed", description=detail, color=0xef4444)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    embed = discord.Embed(
        title="✅ Password Reset Successfully",
        description=f"Your password has been changed.",
        color=0x00e5ff
    )
    embed.add_field(name="Username", value=f"`{data.get('username', '—')}`", inline=True)
    embed.add_field(name="New Password", value=f"||`{new_password}`||", inline=True)
    embed.set_footer(text="Keep your password safe • Do not share with anyone")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /message ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="message", description="Send a direct message to a user")
@app_commands.describe(
    user="The user to message",
    message="The message to send"
)
async def cmd_message(interaction: discord.Interaction, user: discord.User, message: str):
    if not bot.is_admin(interaction):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    
    try:
        await user.send(message)
        embed = discord.Embed(
            title="✅ Message Sent",
            description=f"Message sent to {user.mention}",
            color=0x00e5ff
        )
        embed.add_field(name="Message", value=message, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        print(f"[Bot] Admin {interaction.user} sent DM to {user}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Could not send message - user has DMs disabled.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /announce ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="announce", description="Send an announcement to a channel")
@app_commands.describe(
    channel="The channel to announce in",
    message="The announcement message",
    ping="Whether to ping @everyone or @here"
)
async def cmd_announce(interaction: discord.Interaction, channel: discord.TextChannel, message: str, ping: bool = False):
    if not bot.is_admin(interaction):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    
    try:
        if ping:
            message = f"@everyone\n{message}"
        
        await channel.send(message)
        embed = discord.Embed(
            title="✅ Announcement Sent",
            description=f"Announcement sent to {channel.mention}",
            color=0x00e5ff
        )
        embed.add_field(name="Message", value=message, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        print(f"[Bot] Admin {interaction.user} announced in {channel}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ No permission to send messages in that channel.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /role ───────────────────────────────────────────────────────────────────────
@bot.tree.command(name="role", description="Manage user roles")
@app_commands.describe(
    action="Add or remove role",
    user="The user to manage",
    role="The role to manage"
)
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove")
])
async def cmd_role(interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.Member, role: discord.Role):
    if not bot.is_admin(interaction):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    
    try:
        if action.value == "add":
            await user.add_roles(role)
            embed = discord.Embed(
                title="✅ Role Added",
                description=f"Added {role.mention} to {user.mention}",
                color=0x00e5ff
            )
            print(f"[Bot] Admin {interaction.user} added role {role.name} to {user}")
        else:
            await user.remove_roles(role)
            embed = discord.Embed(
                title="✅ Role Removed",
                description=f"Removed {role.mention} from {user.mention}",
                color=0x00e5ff
            )
            print(f"[Bot] Admin {interaction.user} removed role {role.name} from {user}")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ No permission to manage that role.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /userinfo ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="userinfo", description="Get information about a user")
@app_commands.describe(user="The user to get info about (defaults to yourself)")
async def cmd_userinfo(interaction: discord.Interaction, user: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    
    if user is None:
        user = interaction.user
    
    # Get user's account info from backend
    data, status = await api_get("/api/user_info", {"discord_id": str(user.id)})
    
    embed = discord.Embed(
        title=f"👤 {user.display_name}",
        description=f"ID: {user.id}",
        color=0x00e5ff
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    
    # Discord info
    embed.add_field(name="Joined Server", value=user.joined_at.strftime("%Y-%m-%d") if user.joined_at else "Unknown", inline=True)
    embed.add_field(name="Account Created", value=user.created_at.strftime("%Y-%m-%d") if user.created_at else "Unknown", inline=True)
    embed.add_field(name="Roles", value=", ".join([role.mention for role in user.roles[1:]]) if len(user.roles) > 1 else "None", inline=False)
    
    # Account info if exists
    if status == 200:
        is_banned = data.get("is_banned", False)
        username = data.get("username", "—")
        expiry = data.get("expiry_date", "—")
        lifetime = data.get("is_lifetime", False)
        hwid_bound = data.get("hwid_bound", False)
        last_login = data.get("last_login") or "Never"
        
        if lifetime:
            expiry_display = "♾ Lifetime"
        elif expiry and expiry != "Lifetime":
            try:
                exp_dt = datetime.fromisoformat(expiry)
                remaining = (exp_dt - datetime.utcnow()).days
                expiry_display = f"📅 {expiry[:10]} ({'expired' if remaining < 0 else f'{remaining}d left'})"
            except Exception:
                expiry_display = expiry
        else:
            expiry_display = expiry
        
        embed.add_field(name="Cheat Account", value=f"`{username}`", inline=True)
        embed.add_field(name="Account Status", value="🔴 **BANNED**" if is_banned else "🟢 Active", inline=True)
        embed.add_field(name="Account Expiry", value=expiry_display, inline=True)
        embed.add_field(name="HWID Status", value="🔒 Device locked" if hwid_bound else "🔓 Not bound", inline=True)
        embed.add_field(name="Last Login", value=last_login[:10] if "T" in str(last_login) else last_login, inline=True)
    elif status == 404:
        embed.add_field(name="Cheat Account", value="❌ No account found", inline=False)
    
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /test ───────────────────────────────────────────────────────────────────────
@bot.tree.command(name="test", description="Test bot functionality")
async def cmd_test(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✅ Bot Working",
        description=f"Bot is online and responding!\nGuild ID: {GUILD_ID}\nBot ID: {bot.user.id}",
        color=0x00e5ff
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /help ───────────────────────────────────────────────────────────────────────
@bot.tree.command(name="help", description="Show available commands")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Bot Commands",
        description="Available commands for the cheat authentication system",
        color=0x00e5ff
    )
    
    user_commands = """
**User Commands:**
• `/create <key> <username> <password>` - Create your cheat account
• `/panel` - View your account information
• `/checkkey <key>` - Check if a license key is valid
• `/resethwid` - Reset your HWID (unlink device)
• `/resetpassword <new_password>` - Reset your password
• `/userinfo [user]` - Get user information
• `/test` - Test bot functionality
• `/help` - Show this help message
"""
    
    admin_commands = """
**Admin Commands:**
• `/message <user> <message>` - Send a direct message
• `/announce <channel> <message> [ping]` - Send an announcement
• `/role <add|remove> <user> <role>` - Manage user roles
"""
    
    embed.add_field(name="🔓 User Commands", value=user_commands, inline=False)
    
    if bot.is_admin(interaction):
        embed.add_field(name="🔐 Admin Commands", value=admin_commands, inline=False)
    else:
        embed.add_field(name="🔐 Admin Commands", value="*Admin commands require special permissions*", inline=False)
    
    embed.set_footer(text="Use commands with proper permissions • Contact support for help")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Bot action poll (for panel's "Send Message" / "Manage Role") ──────────────
@tasks.loop(seconds=5)
async def poll_actions():
    try:
        data, status = await api_get("/admin/bot/poll", {"admin_secret": ADMIN_SECRET})
        print(f"[Bot] Poll response: status={status}, data={data}")
        if status != 200 or not isinstance(data, list):
            print(f"[Bot] Poll failed: status={status}, data_type={type(data)}")
            return
        if len(data) == 0:
            return  # No actions to process
        print(f"[Bot] Processing {len(data)} actions")
        for action in data:
            try:
                if action["type"] == "send_message":
                    # Use fetch_channel as fallback if not in cache
                    ch = bot.get_channel(int(action["channel_id"]))
                    if ch is None:
                        try:
                            ch = await bot.fetch_channel(int(action["channel_id"]))
                        except discord.NotFound:
                            print(f"[Bot] Channel {action['channel_id']} not found")
                            continue
                        except discord.Forbidden:
                            print(f"[Bot] No access to channel {action['channel_id']}")
                            continue
                    await ch.send(action["message"])
                    print(f"[Bot] Message sent to channel {action['channel_id']}")

                elif action["type"] == "manage_role":
                    guild  = bot.get_guild(GUILD_ID)
                    member = guild.get_member(int(action["discord_id"])) or await guild.fetch_member(int(action["discord_id"]))
                    role   = guild.get_role(int(action["role_id"]))
                    if role is None:
                        await guild.fetch_roles()
                        role = guild.get_role(int(action["role_id"]))
                    if member and role:
                        if action["action"] == "add":
                            await member.add_roles(role)
                        else:
                            await member.remove_roles(role)
                        print(f"[Bot] Role {action['action']} {role.name} for {member}")
                    else:
                        print(f"[Bot] Could not find member or role for manage_role action")

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