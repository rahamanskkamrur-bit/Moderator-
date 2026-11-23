# =====================================================================
# PART 1 OF 2 — PASTE THIS FIRST AT THE TOP OF YOUR GITHUB FILE
# =====================================================================
import asyncio
import collections
import datetime
import json
import os
import re
import time
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

# --- 1. CONFIGURATION, RULE FILTERS & PERSISTENT STORAGE ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
CFG_FILE = "automod_config.json"
STRIKE_FILE = "automod_strikes.json"
START_TIME = time.time()
BOT_STATUS = "Starting..."

# Severe violations trigger an INSTANT AUTO-BAN
INSTANT_BAN_WORDS = [
    "free nitro", "steamcommunnity", "dlscord", "token grab",
    "doxx", "ddos", "nuke server", "raid server", "cp link",
]

# Standard violations add a Strike (Timeout -> Auto-Kick -> Auto-Ban)
DEFAULT_BAD_WORDS = [
    "discord.gg/", "discord.com/invite/", "discordapp.com/invite/",
    "kys", "kill yourself", "retard", "faggot",
]

SPAM_TRACKER = collections.defaultdict(lambda: collections.deque(maxlen=6))
CMD_PREFIXES = ("!", "?", ".", "-", "$", ">", ";")


def load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


GUILD_CFG = load_json(CFG_FILE)
STRIKES = load_json(STRIKE_FILE)


def get_cfg(gid: int) -> dict:
    return GUILD_CFG.setdefault(
        str(gid),
        {
            "log_channel_id": 0,
            "staff_role_id": 0,
            "kick_strikes": 3,
            "ban_strikes": 5,
            "enforce_on_staff": True,
            "bad_words": list(DEFAULT_BAD_WORDS),
        },
    )


def get_strikes(gid: int, uid: int) -> int:
    return int(STRIKES.get(f"{gid}:{uid}", 0))


def add_strike(gid: int, uid: int, amt: int = 1) -> int:
    key = f"{gid}:{uid}"
    STRIKES[key] = int(STRIKES.get(key, 0)) + amt
    save_json(STRIKE_FILE, STRIKES)
    return STRIKES[key]


def reset_strikes(gid: int, uid: int) -> None:
    STRIKES.pop(f"{gid}:{uid}", None)
    save_json(STRIKE_FILE, STRIKES)


def is_staff(member: discord.Member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    p = member.guild_permissions
    if p.administrator or p.ban_members or p.kick_members or p.moderate_members:
        return True
    srid = get_cfg(member.guild.id).get("staff_role_id", 0)
    return bool(srid and any(r.id == srid for r in member.roles))


async def send_log(guild: discord.Guild, emb: discord.Embed, view: discord.ui.View = None) -> None:
    if not guild:
        return
    ch = guild.get_channel(get_cfg(guild.id).get("log_channel_id", 0))
    if ch:
        try:
            await ch.send(embed=emb, view=view)
        except Exception:
            pass


async def fetch_actor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int = None) -> tuple[str, str]:
    try:
        async for entry in guild.audit_logs(limit=4, action=action):
            if (discord.utils.utcnow() - entry.created_at).total_seconds() <= 12:
                if target_id is None or (entry.target and entry.target.id == target_id):
                    who = f"{entry.user.mention} (`{entry.user}`)" if entry.user else "Unknown"
                    return who, (entry.reason or "No reason provided")
    except Exception:
        pass
    return "Unknown *(Enable View Audit Log)*", "No reason provided"


# --- 2. 24/7 RENDER KEEP-ALIVE WEB SERVER & AUTO-PINGER ---
async def run_web():
    app = web.Application()
    health = lambda r: web.Response(text=f"AutoMod & Logger 24/7 | Up: {int((time.time()-START_TIME)//60)}m | {BOT_STATUS}")
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"🌐 Keep-alive server running on 0.0.0.0:{port}")


async def self_ping():
    await asyncio.sleep(25)
    url = os.environ.get("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{os.environ.get('PORT', 10000)}/"
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                async with s.get(url, timeout=10):
                    pass
            except Exception:
                pass
            await asyncio.sleep(240)


# --- 3. QUICK MODERATOR ACTION BUTTONS ON LOG EMBEDS ---
class ModActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _uid(self, emb: discord.Embed):
        m = re.search(r"ID:\s*`(\d+)`", emb.description or "")
        return int(m.group(1)) if m else None

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.secondary, emoji="👢", custom_id="am:kick")
    async def b_kick(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        uid = self._uid(i.message.embeds[0]) if i.message.embeds else None
        m = i.guild.get_member(uid) if uid else None
        if not m:
            return await i.response.send_message("⚠️ User is not in the server.", ephemeral=True)
        if m.id == i.guild.owner_id:
            return await i.response.send_message("❌ Discord blocks kicking the Server Owner.", ephemeral=True)
        try:
            await m.kick(reason=f"Kicked via Auto-Mod log by {i.user}")
            await i.response.send_message(f"👢 {i.user.mention} kicked {m} (`{uid}`).")
        except discord.Forbidden:
            await i.response.send_message("❌ Drag my Bot Role to **#1 Top** in Server Settings ➔ Roles!", ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, emoji="🔨", custom_id="am:ban")
    async def b_ban(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        uid = self._uid(i.message.embeds[0]) if i.message.embeds else None
        if not uid:
            return await i.response.send_message("⚠️ User ID not found.", ephemeral=True)
        if uid == i.guild.owner_id:
            return await i.response.send_message("❌ Discord blocks banning the Server Owner.", ephemeral=True)
        try:
            await i.guild.ban(discord.Object(id=uid), reason=f"Banned via Auto-Mod log by {i.user}")
            await i.response.send_message(f"🔨 {i.user.mention} banned User ID `{uid}`.")
        except discord.Forbidden:
            await i.response.send_message("❌ Drag my Bot Role to **#1 Top** in Server Settings ➔ Roles!", ephemeral=True)

    @discord.ui.button(label="Unmute & Reset Strikes", style=discord.ButtonStyle.success, emoji="🔓", custom_id="am:unmute")
    async def b_unmute(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        uid = self._uid(i.message.embeds[0]) if i.message.embeds else None
        if uid:
            reset_strikes(i.guild.id, uid)
            m = i.guild.get_member(uid)
            if m:
                try:
                    await m.timeout(None, reason=f"Cleared by {i.user}")
                except Exception:
                    pass
            await i.response.send_message(f"🔓 {i.user.mention} cleared timeout & strikes for `<@{uid}>`.")


# --- 4. AUTO-MOD BOT CORE ---
class AutoModLoggerBot(commands.Bot):
    def __init__(self, msg_i: bool = True, mem_i: bool = True):
        intents = discord.Intents.default()
        intents.message_content = msg_i
        intents.members = mem_i
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self._synced = False

    async def setup_hook(self):
        self.add_view(ModActionView())
        if not self._synced:
            try:
                await self.tree.sync()
                self._synced = True
            except Exception as e:
                print(f"⚠️ Sync notice: {e}")

    async def on_ready(self):
        global BOT_STATUS
        BOT_STATUS = f"ONLINE as {self.user}"
        print(f"✅ Auto-Mod & Full Logger ONLINE as {self.user} (ID: {self.user.id})")
        try:
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Server Rules & Logs 🛡️"))
        except Exception:
            pass

    async def punish_violator(self, msg: discord.Message, reason: str, instant_ban: bool = False):
        guild, member = msg.guild, msg.author
        cfg = get_cfg(guild.id)
        kick_at, ban_at = int(cfg.get("kick_strikes", 3)), int(cfg.get("ban_strikes", 5))
        try:
            await msg.delete()
        except Exception:
            pass

        strikes = add_strike(guild.id, member.id, ban_at if instant_ban else 1)
        is_owner = member.id == guild.owner_id
        above_bot = member.top_role >= guild.me.top_role
        color = discord.Color.orange()

        if instant_ban or strikes >= ban_at:
            color = discord.Color.dark_red()
            if is_owner:
                act = f"🚨 **OWNER VIOLATION LOGGED** (`{strikes}/{ban_at}` — Discord blocks banning Owner)"
            elif above_bot:
                act = f"⚠️ **HIGH-RANK VIOLATION** (`{strikes}/{ban_at}` — Move Bot Role to #1 Top!)"
            else:
                try:
                    await member.send(f"🔨 You were **AUTO-BANNED** from **{guild.name}**.\n**Reason:** {reason} (Strike `{strikes}/{ban_at}`)")
                except Exception:
                    pass
                try:
                    await guild.ban(member, reason=f"Auto-Mod Ban: {reason}", delete_message_seconds=86400)
                    act = f"🔨 **AUTO-BANNED** (Strike `{strikes}/{ban_at}`)"
                except discord.Forbidden:
                    act = "⚠️ Auto-Ban Failed (Move Bot Role to #1 Top & grant Ban Members)"
        elif strikes >= kick_at:
            color = discord.Color.red()
            if is_owner:
                act = f"🚨 **OWNER VIOLATION LOGGED** (`{strikes}/{ban_at}` — Discord blocks kicking Owner)"
            elif above_bot:
                act = f"⚠️ **HIGH-RANK VIOLATION** (`{strikes}/{ban_at}` — Move Bot Role to #1 Top!)"
            else:
                try:
                    await member.send(f"👢 You were **AUTO-KICKED** from **{guild.name}**.\n**Reason:** {reason} (Strike `{strikes}/{ban_at}`)")
                except Exception:
                    pass
                try:
                    await member.kick(reason=f"Auto-Mod Kick: {reason}")
                    act = f"👢 **AUTO-KICKED** (Strike `{strikes}/{ban_at}`)"
                except discord.Forbidden:
                    act = "⚠️ Auto-Kick Failed (Move Bot Role to #1 Top & grant Kick Members)"
        else:
            mins = 5 if strikes == 1 else 15
            if is_owner or above_bot or member.guild_permissions.administrator:
                act = f"⚠️ **High-Rank Strike** (`{strikes}` — Kick at `{kick_at}`, Ban at `{ban_at}`)"
            else:
                try:
                    await member.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=mins), reason=f"Auto-Mod: {reason}")
                    act = f"🔇 **Timed Out `{mins}m`** (Strike `{strikes}` — Kick at `{kick_at}`, Ban at `{ban_at}`)"
                except Exception:
                    act = f"⚠️ **Warned** (Strike `{strikes}` — Kick at `{kick_at}`, Ban at `{ban_at}`)"

        try:
            await msg.channel.send(
                embed=discord.Embed(title="🛡️ Auto-Moderation Action", description=f"{member.mention} broke a server rule (`{reason}`).\n{act}", color=color),
                delete_after=10,
            )
        except Exception:
            pass

        log_emb = discord.Embed(
            title="🚨 AUTO-MOD RULE ENFORCEMENT",
            description=(
                f"👤 **Offender:** {member.mention} (`{member}` | ID: `{member.id}`)\n"
                f"📍 **Channel:** {msg.channel.mention}\n⚠️ **Violation:** `{reason}`\n"
                f"⚖️ **Action Taken:** {act}\n💬 **Message:**\n```{(msg.content or '[No Text]')[:450]}```"
            ),
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        await send_log(guild, log_emb, ModActionView())

    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild:
            return
        content = (msg.content or "").strip()

        if len(content) > 1 and content.startswith(CMD_PREFIXES) and not content[1].isspace():
            emb = discord.Embed(
                title="⌨️ Prefix Command Used",
                description=f"👤 **User:** {msg.author.mention} (`{msg.author.id}`)\n📍 **Channel:** {msg.channel.mention}\n💻 **Command:** `{content[:500]}`",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            await send_log(msg.guild, emb)

        await self.process_commands(msg)

        cfg = get_cfg(msg.guild.id)
        if is_staff(msg.author) and not cfg.get("enforce_on_staff", True):
            return

        low = content.lower()
        for w in INSTANT_BAN_WORDS:
            if w in low:
                return await self.punish_violator(msg, f"Severe Scam/Raid Violation ('{w}')", instant_ban=True)
        for w in cfg.get("bad_words", DEFAULT_BAD_WORDS):
            if w and w.lower() in low:
                return await self.punish_violator(msg, f"Blocked Word / Invite Link ('{w}')")
        if len(msg.mentions) >= 5:
            return await self.punish_violator(msg, f"Mass Mention Spam ({len(msg.mentions)} pings)")

        now = time.time()
        dq = SPAM_TRACKER[(msg.guild.id, msg.author.id)]
        dq.append(now)
        if len(dq) >= 5 and (dq[-1] - dq[-5]) <= 5.0:
            dq.clear()
            return await self.punish_violator(msg, "Rapid Chat Flood (5+ msgs in 5s)")

# =====================================================================
# PART 2 OF 2 — PASTE THIS DIRECTLY BELOW PART 1
# =====================================================================
def register_all_logs(bot: AutoModLoggerBot):
    """Registers all live chat, command, member, channel, role, server & voice loggers."""

    @bot.listen()
    async def on_message_delete(msg: discord.Message):
        if not msg.guild or msg.author.bot:
            return
        atts = "\n".join(f"• {a.filename}: {a.url}" for a in msg.attachments)
        emb = discord.Embed(
            title="🗑️ Message Deleted",
            description=(
                f"👤 **Author:** {msg.author.mention} (`{msg.author.id}`)\n"
                f"📍 **Channel:** {msg.channel.mention}\n"
                f"💬 **Content:**\n```{(msg.content or '[Empty / Attachment Only]')[:800]}```"
                + (f"\n📎 **Attachments:**\n{atts[:400]}" if atts else "")
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        await send_log(msg.guild, emb)

    @bot.listen()
    async def on_bulk_message_delete(messages: list[discord.Message]):
        if not messages or not messages[0].guild:
            return
        emb = discord.Embed(
            title="🧹 Bulk Messages Purged",
            description=f"📍 **Channel:** {messages[0].channel.mention}\n🗑️ **Total Deleted:** `{len(messages)} messages`",
            color=discord.Color.dark_orange(),
            timestamp=discord.utils.utcnow(),
        )
        await send_log(messages[0].guild, emb)

    @bot.listen()
    async def on_message_edit(before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        emb = discord.Embed(
            title="✏️ Message Edited",
            description=f"👤 **Author:** {before.author.mention} (`{before.author.id}`)\n📍 **Channel:** {before.channel.mention} • [Jump]({after.jump_url})",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        emb.add_field(name="Before", value=f"```{(before.content or '[Empty]')[:450]}```", inline=False)
        emb.add_field(name="After", value=f"```{(after.content or '[Empty]')[:450]}```", inline=False)
        await send_log(before.guild, emb)

    @bot.listen()
    async def on_app_command_completion(i: discord.Interaction, cmd):
        if not i.guild:
            return
        opts = ", ".join(f"{o.get('name')}={o.get('value')}" for o in i.data.get("options", [])) if i.data else ""
        emb = discord.Embed(
            title="⚡ Slash Command Executed",
            description=f"👤 **User:** {i.user.mention} (`{i.user.id}`)\n📍 **Channel:** {i.channel.mention if i.channel else 'Unknown'}\n💻 **Command:** `/{cmd.name}` {f'`({opts[:300]})`' if opts else ''}",
            color=discord.Color.teal(),
            timestamp=discord.utils.utcnow(),
        )
        await send_log(i.guild, emb)

    @bot.listen()
    async def on_member_join(m: discord.Member):
        age = (discord.utils.utcnow() - m.created_at).days
        emb = discord.Embed(title="📥 Member Joined", description=f"👤 **User:** {m.mention} (`{m.id}`)\n📅 **Account Age:** `{age}d` • 👥 **Members:** `{m.guild.member_count}`", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        await send_log(m.guild, emb)

    @bot.listen()
    async def on_member_remove(m: discord.Member):
        actor, reason = await fetch_actor(m.guild, discord.AuditLogAction.kick, m.id)
        kicked = not actor.startswith("Unknown")
        emb = discord.Embed(
            title="👢 Member Kicked" if kicked else "🚪 Member Left",
            description=f"👤 **User:** {m} (`{m.id}`)" + (f"\n🛡️ **By:** {actor}\n📝 **Reason:** {reason}" if kicked else ""),
            color=discord.Color.red() if kicked else discord.Color.dark_grey(),
            timestamp=discord.utils.utcnow(),
        )
        await send_log(m.guild, emb)

    @bot.listen()
    async def on_member_ban(g: discord.Guild, u: discord.User):
        actor, reason = await fetch_actor(g, discord.AuditLogAction.ban, u.id)
        await send_log(g, discord.Embed(title="🔨 Member Banned", description=f"👤 **User:** {u} (`{u.id}`)\n🛡️ **By:** {actor}\n📝 **Reason:** {reason}", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_member_unban(g: discord.Guild, u: discord.User):
        actor, reason = await fetch_actor(g, discord.AuditLogAction.unban, u.id)
        await send_log(g, discord.Embed(title="🔓 Member Unbanned", description=f"👤 **User:** {u} (`{u.id}`)\n🛡️ **By:** {actor}\n📝 **Reason:** {reason}", color=discord.Color.green(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_member_update(before: discord.Member, after: discord.Member):
        ch = []
        if before.nick != after.nick:
            ch.append(f"🏷️ **Nick:** `{before.nick or before.name}` ➔ `{after.nick or after.name}`")
        add_r = [r.mention for r in after.roles if r not in before.roles]
        rem_r = [r.mention for r in before.roles if r not in after.roles]
        if add_r:
            ch.append(f"➕ **Roles Added:** {', '.join(add_r)}")
        if rem_r:
            ch.append(f"➖ **Roles Removed:** {', '.join(rem_r)}")
        if before.timed_out_until != after.timed_out_until:
            ch.append(f"🔇 **Timed Out Until:** <t:{int(after.timed_out_until.timestamp())}:R>" if after.timed_out_until else "🔓 **Timeout Cleared**")
        if ch:
            await send_log(after.guild, discord.Embed(title="👤 Member Updated", description=f"👤 **Member:** {after.mention} (`{after.id}`)\n" + "\n".join(ch), color=discord.Color.blurple(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_guild_channel_create(c: discord.abc.GuildChannel):
        actor, _ = await fetch_actor(c.guild, discord.AuditLogAction.channel_create, c.id)
        await send_log(c.guild, discord.Embed(title="📁 Channel Created", description=f"📍 {c.mention} (`#{c.name}`)\n🛡️ **By:** {actor}", color=discord.Color.green(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_guild_channel_delete(c: discord.abc.GuildChannel):
        actor, _ = await fetch_actor(c.guild, discord.AuditLogAction.channel_delete, c.id)
        await send_log(c.guild, discord.Embed(title="🗑️ Channel Deleted", description=f"📍 `#{c.name}` (`{c.id}`)\n🛡️ **By:** {actor}", color=discord.Color.red(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_guild_channel_update(b: discord.abc.GuildChannel, a: discord.abc.GuildChannel):
        ch = []
        if b.name != a.name:
            ch.append(f"✏️ **Name:** `#{b.name}` ➔ `#{a.name}`")
        if getattr(b, "topic", None) != getattr(a, "topic", None):
            ch.append(f"📝 **Topic:** `{(a.topic or 'None')[:200]}`")
        if b.overwrites != a.overwrites:
            ch.append("🔐 **Permissions Modified**")
        if ch:
            actor, _ = await fetch_actor(a.guild, discord.AuditLogAction.channel_update, a.id)
            await send_log(a.guild, discord.Embed(title="⚙️ Channel Updated", description=f"📍 {a.mention}\n🛡️ **By:** {actor}\n" + "\n".join(ch), color=discord.Color.gold(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_guild_role_create(r: discord.Role):
        actor, _ = await fetch_actor(r.guild, discord.AuditLogAction.role_create, r.id)
        await send_log(r.guild, discord.Embed(title="🎭 Role Created", description=f"🏷️ {r.mention} (`{r.name}`)\n🛡️ **By:** {actor}", color=discord.Color.green(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_guild_role_delete(r: discord.Role):
        actor, _ = await fetch_actor(r.guild, discord.AuditLogAction.role_delete, r.id)
        await send_log(r.guild, discord.Embed(title="🗑️ Role Deleted", description=f"🏷️ `{r.name}` (`{r.id}`)\n🛡️ **By:** {actor}", color=discord.Color.red(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_guild_role_update(b: discord.Role, a: discord.Role):
        ch = []
        if b.name != a.name:
            ch.append(f"✏️ **Name:** `{b.name}` ➔ `{a.name}`")
        if b.color != a.color:
            ch.append(f"🎨 **Color:** `{b.color}` ➔ `{a.color}`")
        if b.permissions != a.permissions:
            ch.append("🔐 **Permissions Changed**")
        if ch:
            actor, _ = await fetch_actor(a.guild, discord.AuditLogAction.role_update, a.id)
            await send_log(a.guild, discord.Embed(title="⚙️ Role Updated", description=f"🏷️ {a.mention}\n🛡️ **By:** {actor}\n" + "\n".join(ch), color=discord.Color.gold(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_guild_update(b: discord.Guild, a: discord.Guild):
        ch = []
        if b.name != a.name:
            ch.append(f"🏛️ **Name:** `{b.name}` ➔ `{a.name}`")
        if b.icon != a.icon:
            ch.append("🖼️ **Server Icon Changed**")
        if b.verification_level != a.verification_level:
            ch.append(f"🛡️ **Verification:** `{b.verification_level}` ➔ `{a.verification_level}`")
        if ch:
            actor, _ = await fetch_actor(a, discord.AuditLogAction.guild_update, a.id)
            await send_log(a, discord.Embed(title="🏛️ Server Settings Updated", description=f"🛡️ **By:** {actor}\n" + "\n".join(ch), color=discord.Color.purple(), timestamp=discord.utils.utcnow()))

    @bot.listen()
    async def on_voice_state_update(m: discord.Member, b: discord.VoiceState, a: discord.VoiceState):
        if m.bot or b.channel == a.channel:
            return
        txt = f"🔊 Joined {a.channel.mention}" if not b.channel else (f"🔇 Left {b.channel.mention}" if not a.channel else f"🔀 {b.channel.mention} ➔ {a.channel.mention}")
        await send_log(m.guild, discord.Embed(title="🎙️ Voice Activity", description=f"👤 {m.mention} (`{m.id}`)\n{txt}", color=discord.Color.blurple(), timestamp=discord.utils.utcnow()))


# --- 5. SLASH COMMANDS ---
def reg_cmds(bot: AutoModLoggerBot):
    @bot.tree.command(name="mod_setup", description="Set Log Channel, Staff Role, Strike Limits & High-Rank AutoMod")
    async def s_setup(i: discord.Interaction, log_channel: discord.TextChannel, staff_role: discord.Role, strikes_for_kick: int = 3, strikes_for_ban: int = 5, enforce_on_high_ranks: bool = True):
        if not i.user.guild_permissions.administrator:
            return await i.response.send_message("❌ Administrators only.", ephemeral=True)
        cfg = get_cfg(i.guild.id)
        cfg.update({"log_channel_id": log_channel.id, "staff_role_id": staff_role.id, "kick_strikes": max(1, strikes_for_kick), "ban_strikes": max(strikes_for_kick + 1, strikes_for_ban), "enforce_on_staff": enforce_on_high_ranks})
        save_json(CFG_FILE, GUILD_CFG)
        emb = discord.Embed(title="✅ Auto-Mod & Full Server Logger Ready!", description=f"📜 **Logs:** {log_channel.mention}\n🛡️ **Staff:** {staff_role.mention}\n⚔️ **High-Rank Mod:** `{'YES' if enforce_on_high_ranks else 'NO'}`\n👢 **Kick:** `{cfg['kick_strikes']}` | 🔨 **Ban:** `{cfg['ban_strikes']}`\n\n📌 *Drag Bot Role to **#1 Top**!*", color=discord.Color.green())
        await i.response.send_message(embed=emb, ephemeral=True)

    @bot.tree.command(name="add_badword", description="Add a blocked word or link")
    async def s_addw(i: discord.Interaction, word: str):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        cfg, w = get_cfg(i.guild.id), word.strip().lower()
        if w not in cfg["bad_words"]:
            cfg["bad_words"].append(w)
            save_json(CFG_FILE, GUILD_CFG)
        await i.response.send_message(f"✅ Blocked `{w}`!", ephemeral=True)

    @bot.tree.command(name="remove_badword", description="Remove a blocked word")
    async def s_remw(i: discord.Interaction, word: str):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        cfg, w = get_cfg(i.guild.id), word.strip().lower()
        cfg["bad_words"] = [x for x in cfg["bad_words"] if x.lower() != w]
        save_json(CFG_FILE, GUILD_CFG)
        await i.response.send_message(f"🗑️ Unblocked `{w}`.", ephemeral=True)

    @bot.tree.command(name="warn", description="Warn a player (+1 Strike — Auto-Kicks/Bans at limit)")
    async def s_warn(i: discord.Interaction, member: discord.Member, reason: str):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        cfg, st = get_cfg(i.guild.id), add_strike(i.guild.id, member.id, 1)
        extra = ""
        if member.id != i.guild.owner_id and member.top_role < i.guild.me.top_role:
            if st >= cfg["ban_strikes"]:
                await member.ban(reason=f"Reached {st} strikes: {reason}")
                extra = "\n🔨 **Threshold Reached — Auto-Banned!**"
            elif st >= cfg["kick_strikes"]:
                await member.kick(reason=f"Reached {st} strikes: {reason}")
                extra = "\n👢 **Threshold Reached — Auto-Kicked!**"
        emb = discord.Embed(title="⚠️ Official Warning", description=f"👤 {member.mention} (ID: `{member.id}`)\n🛡️ {i.user.mention}\n📌 {reason}\n📊 Strikes: `{st}/{cfg['ban_strikes']}`{extra}", color=discord.Color.gold())
        await i.response.send_message(content=member.mention, embed=emb)
        await send_log(i.guild, emb, ModActionView())

    @bot.tree.command(name="warnings", description="Check a player's strikes")
    async def s_warns(i: discord.Interaction, member: discord.Member):
        st, cfg = get_strikes(i.guild.id, member.id), get_cfg(i.guild.id)
        await i.response.send_message(f"📊 {member.mention}: **`{st}`** strike(s) *(Kick `{cfg['kick_strikes']}`, Ban `{cfg['ban_strikes']}`)*.", ephemeral=True)

    @bot.tree.command(name="clear_warnings", description="Reset a player's strikes")
    async def s_clr(i: discord.Interaction, member: discord.Member):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        reset_strikes(i.guild.id, member.id)
        await i.response.send_message(f"🔓 Cleared strikes for {member.mention}.", ephemeral=True)

    @bot.tree.command(name="kick", description="Kick any member (including high ranks if Bot Role #1)")
    async def s_kick(i: discord.Interaction, member: discord.Member, reason: str = "Rule violation"):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        if member.id == i.guild.owner_id:
            return await i.response.send_message("❌ Discord blocks kicking the Server Owner.", ephemeral=True)
        if member.top_role >= i.guild.me.top_role:
            return await i.response.send_message("❌ Drag Bot Role to **#1 Top** in Server Settings ➔ Roles!", ephemeral=True)
        await member.kick(reason=f"{reason} (by {i.user})")
        await i.response.send_message(f"👢 Kicked **{member}** (`{reason}`).")

    @bot.tree.command(name="ban", description="Ban any member (including high ranks if Bot Role #1)")
    async def s_ban(i: discord.Interaction, member: discord.Member, reason: str = "Rule violation"):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        if member.id == i.guild.owner_id:
            return await i.response.send_message("❌ Discord blocks banning the Server Owner.", ephemeral=True)
        if member.top_role >= i.guild.me.top_role:
            return await i.response.send_message("❌ Drag Bot Role to **#1 Top** in Server Settings ➔ Roles!", ephemeral=True)
        await member.ban(reason=f"{reason} (by {i.user})")
        await i.response.send_message(f"🔨 Banned **{member}** (`{reason}`).")

    @bot.tree.command(name="unban", description="Unban a player by User ID")
    async def s_unban(i: discord.Interaction, user_id: str, reason: str = "Appeal approved"):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        await i.guild.unban(discord.Object(id=int(user_id.strip())), reason=f"{reason} (by {i.user})")
        await i.response.send_message(f"🔓 Unbanned User ID `{user_id}`.")

    @bot.tree.command(name="purge", description="Delete 1-100 recent messages")
    async def s_purge(i: discord.Interaction, amount: int):
        if not is_staff(i.user):
            return await i.response.send_message("❌ Staff only.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        deleted = await i.channel.purge(limit=max(1, min(amount, 100)))
        await i.followup.send(f"🧹 Deleted **{len(deleted)}** messages!", ephemeral=True)


# --- 6. MAIN ENTRYPOINT (RENDER 24/7 + 429 AUTO-RETRY) ---
async def main():
    global BOT_STATUS
    await run_web()
    asyncio.create_task(self_ping())
    if not BOT_TOKEN:
        BOT_STATUS = "MISSING BOT_TOKEN"
        print("❌ ERROR: Add BOT_TOKEN in Render Environment Variables!")
        while True:
            await asyncio.sleep(3600)

    msg_i, mem_i, wait_s = True, True, 90
    while True:
        bot = AutoModLoggerBot(msg_i, mem_i)
        register_all_logs(bot)
        reg_cmds(bot)
        try:
            await bot.start(BOT_TOKEN)
            break
        except discord.PrivilegedIntentsRequired:
            print("⚠️ Enable Message Content & Server Members Intents in Developer Portal!")
            await bot.close()
            if mem_i:
                mem_i = False
            elif msg_i:
                msg_i = False
            else:
                await asyncio.sleep(60)
        except discord.HTTPException as e:
            await bot.close()
            if e.status == 429:
                BOT_STATUS = f"429 Rate Limit - retrying in {wait_s}s"
                await asyncio.sleep(wait_s)
                wait_s = min(wait_s + 60, 300)
            else:
                await asyncio.sleep(60)
        except Exception as e:
            await bot.close()
            BOT_STATUS = f"Reconnecting ({type(e).__name__})"
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
