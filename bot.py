import os
import time
import asyncio
import sqlite3
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
DB_PATH = "bot_data.sqlite3"
STARTED_AT = time.time()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.moderation = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or(PREFIX),
                   intents=intents, help_command=None, case_insensitive=True)

# ---------- Database ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL, reason TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY, log_channel_id INTEGER,
            welcome_channel_id INTEGER, welcome_message TEXT)""")

def get_setting(guild_id, key):
    with db() as con:
        row = con.execute(f"SELECT {key} FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
        return row[key] if row else None

def set_setting(guild_id, key, value):
    allowed = {"log_channel_id", "welcome_channel_id", "welcome_message"}
    if key not in allowed:
        raise ValueError("Invalid setting")
    with db() as con:
        con.execute("INSERT OR IGNORE INTO settings(guild_id) VALUES(?)", (guild_id,))
        con.execute(f"UPDATE settings SET {key}=? WHERE guild_id=?", (value, guild_id))

async def send_log(guild, title, description, color=discord.Color.blurple()):
    channel_id = get_setting(guild.id, "log_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        try:
            await channel.send(embed=discord.Embed(title=title, description=description,
                                                   color=color, timestamp=discord.utils.utcnow()))
        except discord.Forbidden:
            pass

def reason_or_default(reason):
    return reason or "No reason provided."

def bot_can_act(ctx, member):
    if member.id == ctx.guild.owner_id:
        return False
    if member.top_role >= ctx.guild.me.top_role:
        return False
    return True

async def resolve_member(ctx, user):
    if isinstance(user, discord.Member):
        return user
    return ctx.guild.get_member(user.id)

# ---------- Events ----------
@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as exc:
        print(f"Slash sync failed: {exc}")
    print(f"Logged in as {bot.user} | {len(bot.guilds)} servers")

@bot.event
async def on_member_join(member):
    channel_id = get_setting(member.guild.id, "welcome_channel_id")
    channel = member.guild.get_channel(channel_id) if channel_id else None
    if channel:
        template = get_setting(member.guild.id, "welcome_message") or "Welcome {user} to **{server}**!"
        await channel.send(template.replace("{user}", member.mention).replace("{server}", member.guild.name))
    await send_log(member.guild, "Member joined", f"{member.mention} (`{member.id}`)")

@bot.event
async def on_member_remove(member):
    await send_log(member.guild, "Member left", f"{member} (`{member.id}`)")

@bot.event
async def on_member_ban(guild, user):
    await send_log(guild, "Member banned", f"{user} (`{user.id}`)", discord.Color.red())

@bot.event
async def on_message_delete(message):
    if message.guild and not message.author.bot:
        content = message.content[:1500] or "[No text content]"
        await send_log(message.guild, "Message deleted",
                       f"Author: {message.author.mention}\\nChannel: {message.channel.mention}\\nContent: {content}",
                       discord.Color.orange())

@bot.event
async def on_message_edit(before, after):
    if before.guild and not before.author.bot and before.content != after.content:
        await send_log(before.guild, "Message edited",
                       f"Author: {before.author.mention}\\nChannel: {before.channel.mention}\\n"
                       f"Before: {before.content[:700]}\\nAfter: {after.content[:700]}",
                       discord.Color.orange())
    await bot.process_commands(after)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        msg = "You don't have permission to use that command."
    elif isinstance(error, commands.BotMissingPermissions):
        msg = "I don't have the required permissions for that action."
    elif isinstance(error, commands.MissingRequiredArgument):
        msg = f"Missing argument: `{error.param.name}`."
    elif isinstance(error, commands.BadArgument):
        msg = "I couldn't understand one of those arguments. Check the command usage."
    elif isinstance(error, commands.CommandOnCooldown):
        msg = f"Please wait {error.retry_after:.1f}s before using that again."
    else:
        print(f"Command error: {repr(error)}")
        msg = "Something went wrong while running that command."
    try:
        await ctx.send(msg, ephemeral=True)
    except TypeError:
        await ctx.send(msg)

# ---------- Moderation ----------
@bot.hybrid_command(name="ban", description="Ban a member from this server.")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = None):
    if not bot_can_act(ctx, member):
        return await ctx.send("I can't ban the server owner or someone with an equal/higher role.", ephemeral=True)
    reason = reason_or_default(reason)
    await member.ban(reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
    await ctx.send(f"🔨 Banned **{member}**. Reason: {reason}")
    await send_log(ctx.guild, "Ban", f"**{member}** banned by {ctx.author.mention}\\nReason: {reason}", discord.Color.red())

@bot.hybrid_command(name="unban", description="Unban a user by ID.")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx, user_id: str, *, reason: str = None):
    try:
        uid = int(user_id)
        user = await bot.fetch_user(uid)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason_or_default(reason)}")
        await ctx.send(f"Unbanned **{user}**.")
        await send_log(ctx.guild, "Unban", f"{user} unbanned by {ctx.author.mention}")
    except (ValueError, discord.NotFound):
        await ctx.send("That user ID is invalid or isn't banned.", ephemeral=True)

@bot.hybrid_command(name="kick", description="Kick a member.")
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = None):
    if not bot_can_act(ctx, member):
        return await ctx.send("I can't kick that member.", ephemeral=True)
    reason = reason_or_default(reason)
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.send(f"👢 Kicked **{member}**. Reason: {reason}")
    await send_log(ctx.guild, "Kick", f"{member} kicked by {ctx.author.mention}\\nReason: {reason}", discord.Color.orange())

@bot.hybrid_command(name="timeout", description="Timeout a member for a number of minutes.")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], *, reason: str = None):
    if not bot_can_act(ctx, member):
        return await ctx.send("I can't timeout that member.", ephemeral=True)
    reason = reason_or_default(reason)
    await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
    await ctx.send(f"⏳ Timed out **{member}** for {minutes} minute(s). Reason: {reason}")
    await send_log(ctx.guild, "Timeout", f"{member} timed out by {ctx.author.mention} for {minutes}m\\n{reason}", discord.Color.orange())

@bot.hybrid_command(name="untimeout", description="Remove a member's timeout.")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member, *, reason: str = None):
    if not bot_can_act(ctx, member):
        return await ctx.send("I can't remove that member's timeout.", ephemeral=True)
    await member.timeout(None, reason=f"{ctx.author}: {reason_or_default(reason)}")
    await ctx.send(f"Removed timeout from **{member}**.")

@bot.hybrid_command(name="softban", description="Ban and immediately unban a member to remove recent messages.")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def softban(ctx, member: discord.Member, *, reason: str = None):
    if not bot_can_act(ctx, member):
        return await ctx.send("I can't softban that member.", ephemeral=True)
    await member.ban(reason=f"Softban by {ctx.author}: {reason_or_default(reason)}", delete_message_seconds=86400)
    await ctx.guild.unban(member, reason="Softban complete")
    await ctx.send(f"Softbanned **{member}** (removed up to 24 hours of messages).")

@bot.hybrid_command(name="warn", description="Give a member a warning.")
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason: str = None):
    reason = reason_or_default(reason)
    with db() as con:
        con.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
                    (ctx.guild.id, member.id, ctx.author.id, reason))
    await ctx.send(f"⚠️ Warned **{member}**. Reason: {reason}")
    await send_log(ctx.guild, "Warning", f"{member.mention} warned by {ctx.author.mention}\\n{reason}", discord.Color.orange())

@bot.hybrid_command(name="warnings", description="View a member's warnings.")
@commands.has_permissions(moderate_members=True)
async def warnings(ctx, member: discord.Member):
    with db() as con:
        rows = con.execute("SELECT id, moderator_id, reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
                           (ctx.guild.id, member.id)).fetchall()
    if not rows:
        return await ctx.send(f"**{member}** has no warnings.")
    embed = discord.Embed(title=f"Warnings for {member}", color=discord.Color.orange())
    for row in rows:
        embed.add_field(name=f"#{row['id']} • {row['created_at']}", value=f"{row['reason']}\\nModerator ID: `{row['moderator_id']}`", inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="clearwarnings", description="Clear all warnings for a member.")
@commands.has_permissions(moderate_members=True)
async def clearwarnings(ctx, member: discord.Member):
    with db() as con:
        cur = con.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    await ctx.send(f"Cleared {cur.rowcount} warning(s) for **{member}**.")

@bot.hybrid_command(name="purge", description="Delete a number of recent messages.")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(ctx, amount: app_commands.Range[int, 1, 100]):
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Deleted {max(0, len(deleted)-1)} message(s).")
    await asyncio.sleep(4)
    await msg.delete()

# ---------- Channel controls ----------
@bot.hybrid_command(name="lock", description="Prevent @everyone from sending messages here.")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def lock(ctx, channel: Optional[discord.TextChannel] = None):
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}")
    await ctx.send(f"🔒 Locked {channel.mention}.")

@bot.hybrid_command(name="unlock", description="Allow @everyone to send messages here again.")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def unlock(ctx, channel: Optional[discord.TextChannel] = None):
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}")
    await ctx.send(f"🔓 Unlocked {channel.mention}.")

@bot.hybrid_command(name="hide", description="Hide a channel from @everyone.")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def hide(ctx, channel: Optional[discord.TextChannel] = None):
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.view_channel = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(f"🙈 Hid {channel.mention}.")

@bot.hybrid_command(name="unhide", description="Make a channel visible to @everyone.")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def unhide(ctx, channel: Optional[discord.TextChannel] = None):
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.view_channel = None
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(f"👀 Made {channel.mention} visible.")

@bot.hybrid_command(name="slowmode", description="Set channel slowmode in seconds.")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: app_commands.Range[int, 0, 21600], channel: Optional[discord.TextChannel] = None):
    channel = channel or ctx.channel
    await channel.edit(slowmode_delay=seconds)
    await ctx.send(f"Set {channel.mention} slowmode to **{seconds}s**.")

@bot.hybrid_command(name="renamechannel", description="Rename a text channel.")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def renamechannel(ctx, channel: discord.TextChannel, *, name: str):
    await channel.edit(name=name[:100])
    await ctx.send(f"Renamed channel to **{channel.name}**.")

@bot.hybrid_command(name="nuke", description="Recreate this channel (deletes its messages and settings).")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def nuke(ctx):
    old = ctx.channel
    new = await old.clone(reason=f"Channel recreated by {ctx.author}")
    await new.edit(position=old.position)
    await old.delete(reason=f"Channel recreated by {ctx.author}")
    await new.send(f"💥 Channel recreated by {ctx.author.mention}.")

# ---------- Roles ----------
@bot.hybrid_command(name="roleadd", description="Give a role to a member.")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def roleadd(ctx, member: discord.Member, role: discord.Role):
    if role >= ctx.guild.me.top_role or role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("Role hierarchy prevents that action.", ephemeral=True)
    await member.add_roles(role, reason=f"Role added by {ctx.author}")
    await ctx.send(f"Added {role.mention} to {member.mention}.")

@bot.hybrid_command(name="roleremove", description="Remove a role from a member.")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def roleremove(ctx, member: discord.Member, role: discord.Role):
    if role >= ctx.guild.me.top_role or (role >= ctx.author.top_role and ctx.author != ctx.guild.owner):
        return await ctx.send("Role hierarchy prevents that action.", ephemeral=True)
    await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
    await ctx.send(f"Removed {role.mention} from {member.mention}.")

@bot.hybrid_command(name="rolecreate", description="Create a role.")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def rolecreate(ctx, *, name: str):
    role = await ctx.guild.create_role(name=name[:100], reason=f"Created by {ctx.author}")
    await ctx.send(f"Created role {role.mention}.")

@bot.hybrid_command(name="roledelete", description="Delete a role.")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def roledelete(ctx, role: discord.Role):
    if role >= ctx.guild.me.top_role or (role >= ctx.author.top_role and ctx.author != ctx.guild.owner):
        return await ctx.send("Role hierarchy prevents that action.", ephemeral=True)
    name = role.name
    await role.delete(reason=f"Deleted by {ctx.author}")
    await ctx.send(f"Deleted role **{name}**.")

# ---------- User/server info ----------
@bot.hybrid_command(name="userinfo", description="Show information about a member.")
async def userinfo(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    embed = discord.Embed(title=str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User ID", value=member.id)
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown")
    embed.add_field(name="Top role", value=member.top_role.mention)
    embed.add_field(name="Bot", value="Yes" if member.bot else "No")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="avatar", description="Show a user's avatar.")
async def avatar(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member}'s avatar")
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="serverinfo", description="Show server information.")
async def serverinfo(ctx):
    g = ctx.guild
    embed = discord.Embed(title=g.name, color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=str(g.owner))
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Channels", value=len(g.channels))
    embed.add_field(name="Roles", value=len(g.roles))
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D"))
    await ctx.send(embed=embed)

@bot.hybrid_command(name="membercount", description="Show the server member count.")
async def membercount(ctx):
    await ctx.send(f"👥 This server has **{ctx.guild.member_count}** members.")

@bot.hybrid_command(name="roleinfo", description="Show information about a role.")
async def roleinfo(ctx, role: discord.Role):
    embed = discord.Embed(title=role.name, color=role.color)
    embed.add_field(name="ID", value=role.id)
    embed.add_field(name="Members", value=len(role.members))
    embed.add_field(name="Position", value=role.position)
    embed.add_field(name="Mentionable", value=role.mentionable)
    await ctx.send(embed=embed)

# ---------- Utility and configuration ----------
@bot.hybrid_command(name="ping", description="Check bot latency.")
async def ping(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

@bot.hybrid_command(name="uptime", description="Show how long the bot has been online.")
async def uptime(ctx):
    seconds = int(time.time() - STARTED_AT)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    await ctx.send(f"⏱️ Uptime: **{hours}h {minutes}m {seconds}s**")

@bot.hybrid_command(name="botinfo", description="Show bot information.")
async def botinfo(ctx):
    embed = discord.Embed(title="Bot information", color=discord.Color.blurple())
    embed.add_field(name="Python library", value=f"discord.py {discord.__version__}")
    embed.add_field(name="Servers", value=len(bot.guilds))
    embed.add_field(name="Latency", value=f"{bot.latency*1000:.0f}ms")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="say", description="Make the bot send a message.")
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message: str):
    await ctx.send(message[:2000], allowed_mentions=discord.AllowedMentions.none())

@bot.hybrid_command(name="announce", description="Send an announcement to a channel.")
@commands.has_permissions(manage_guild=True)
async def announce(ctx, channel: discord.TextChannel, *, message: str):
    embed = discord.Embed(title="📢 Announcement", description=message[:4000], color=discord.Color.blurple())
    embed.set_footer(text=f"Announcement by {ctx.author}")
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    await ctx.send(f"Announcement sent to {channel.mention}.")

@bot.hybrid_command(name="embed", description="Send a simple embed to a channel.")
@commands.has_permissions(manage_messages=True)
async def embed_cmd(ctx, channel: discord.TextChannel, title: str, *, description: str):
    e = discord.Embed(title=title[:256], description=description[:4000], color=discord.Color.blurple())
    await channel.send(embed=e)
    await ctx.send(f"Embed sent to {channel.mention}.")

@bot.hybrid_command(name="poll", description="Create a yes/no poll.")
async def poll(ctx, *, question: str):
    if len(question) > 250:
        return await ctx.send("Keep the poll question under 250 characters.")
    msg = await ctx.send(embed=discord.Embed(title="📊 Poll", description=question, color=discord.Color.blurple()))
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")

@bot.hybrid_command(name="setnick", description="Change a member's nickname.")
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def setnick(ctx, member: discord.Member, *, nickname: str = None):
    if not bot_can_act(ctx, member):
        return await ctx.send("I can't change that member's nickname.", ephemeral=True)
    await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
    await ctx.send(f"Updated nickname for {member.mention}.")

@bot.hybrid_command(name="setlogs", description="Set the moderation log channel.")
@commands.has_permissions(manage_guild=True)
async def setlogs(ctx, channel: discord.TextChannel):
    set_setting(ctx.guild.id, "log_channel_id", channel.id)
    await ctx.send(f"Log channel set to {channel.mention}.")

@bot.hybrid_command(name="setwelcome", description="Set the welcome channel and optional message.")
@commands.has_permissions(manage_guild=True)
async def setwelcome(ctx, channel: discord.TextChannel, *, message: str = "Welcome {user} to **{server}**!"):
    set_setting(ctx.guild.id, "welcome_channel_id", channel.id)
    set_setting(ctx.guild.id, "welcome_message", message[:1000])
    await ctx.send(f"Welcome channel set to {channel.mention}. Use `{{user}}` and `{{server}}` in the message.")

@bot.hybrid_command(name="help", description="Show the bot command list.")
async def help_command(ctx):
    groups = {
        "Moderation": "`ban`, `unban`, `kick`, `timeout`, `untimeout`, `softban`, `warn`, `warnings`, `clearwarnings`, `purge`",
        "Channels": "`lock`, `unlock`, `hide`, `unhide`, `slowmode`, `renamechannel`, `nuke`",
        "Roles": "`roleadd`, `roleremove`, `rolecreate`, `roledelete`",
        "Information": "`userinfo`, `avatar`, `serverinfo`, `membercount`, `roleinfo`, `botinfo`, `uptime`, `ping`",
        "Utility / Setup": "`say`, `announce`, `embed`, `poll`, `setnick`, `setlogs`, `setwelcome`, `help`"
    }
    e = discord.Embed(title="Command list", description=f"Prefix: `{PREFIX}` • Slash commands are also available.", color=discord.Color.blurple())
    for name, commands_text in groups.items():
        e.add_field(name=name, value=commands_text, inline=False)
    await ctx.send(embed=e)

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token.")
init_db()
bot.run(TOKEN)
