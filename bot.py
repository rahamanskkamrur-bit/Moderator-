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
