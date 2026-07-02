"""
Discord Bot - حماية + مودريشن
================================
- منع Threads تلقائياً (باند فوري)
- أوامر بدون بادئة: برا، طرد، اص، تايم، فك، اسم، رول، تل، ق، ف، warn، warns، clearwarns، clear، userinfo، serverinfo
"""

import json
import logging
import os
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import commands

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ModBot")

# =============================================================================
# CONFIG
# =============================================================================
TOKEN = os.getenv("DISCORD_TOKEN", "")

# =============================================================================
# WARNINGS STORAGE
# =============================================================================
WARNS_FILE = Path("warnings.json")

def _load_warns() -> dict:
    if not WARNS_FILE.exists():
        return {}
    try:
        return json.loads(WARNS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_warns(data: dict) -> None:
    WARNS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# =============================================================================
# BOT SETUP
# =============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members         = True
intents.guilds          = True

bot = commands.Bot(command_prefix="\x00", intents=intents, help_command=None)

# =============================================================================
# HELPERS
# =============================================================================

def _is_mod(member: discord.Member) -> bool:
    p = member.guild_permissions
    return p.administrator or p.ban_members or p.kick_members or p.moderate_members

def _mod_embed(title: str, color: discord.Color, **fields) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    for name, value in fields.items():
        embed.add_field(name=name, value=str(value), inline=True)
    return embed

def _parse_duration(text: str) -> int | None:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units:
        try:
            return int(text[:-1]) * units[text[-1]]
        except ValueError:
            return None
    try:
        return int(text) * 60
    except ValueError:
        return None

async def _dm(member: discord.Member, embed: discord.Embed) -> None:
    try:
        await member.send(embed=embed)
    except Exception:
        pass

def _parse_args(content: str):
    """تقسيم الرسالة لقائمة كلمات."""
    try:
        return shlex.split(content)
    except ValueError:
        return content.split()

async def _resolve_member(guild: discord.Guild, mention_or_id: str) -> discord.Member | None:
    """جيب العضو من المنشن أو الـ ID."""
    text = mention_or_id.strip("<@!>")
    try:
        uid = int(text)
        return guild.get_member(uid) or await guild.fetch_member(uid)
    except Exception:
        return None

async def _resolve_role(guild: discord.Guild, mention_or_id: str) -> discord.Role | None:
    text = mention_or_id.strip("<@&>")
    try:
        rid = int(text)
        return guild.get_role(rid)
    except Exception:
        return None

async def _no_perm(message: discord.Message):
    await message.reply("ما عندك صلاحية.", delete_after=4)

async def _usage(message: discord.Message, text: str):
    await message.reply(f"الاستخدام: `{text}`", delete_after=5)

# =============================================================================
# EVENTS
# =============================================================================

@bot.event
async def on_ready():
    logger.info(f"البوت جاهز: {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="السيرفر"
    ))


@bot.event
async def on_thread_create(thread: discord.Thread):
    guild    = thread.guild
    owner_id = thread.owner_id
    if not owner_id:
        return
    if thread.owner and thread.owner.bot:
        return
    logger.info(f"[THREAD] #{thread.name} | owner={owner_id}")
    try:
        await thread.delete()
    except Exception as exc:
        logger.error(f"[DELETE] {exc}")
    try:
        member = guild.get_member(owner_id) or await guild.fetch_member(owner_id)
        if member and member.bot:
            return
        target = member or discord.Object(id=owner_id)
        await guild.ban(target, reason="إنشاء Thread محظور", delete_message_days=0)
        logger.info(f"[BAN-THREAD] {owner_id}")
    except Exception as exc:
        logger.error(f"[BAN-THREAD] {exc}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    args = _parse_args(message.content)
    if not args:
        return

    cmd  = args[0].lower()
    rest = args[1:]
    guild  = message.guild
    author = message.author

    # ── برا / ban ──────────────────────────────────────────────────────────
    if cmd in ("برا", "ban"):
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "برا @عضو [سبب]")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        if member.top_role >= author.top_role and author != guild.owner:
            return await message.reply("رتبة العضو أعلى منك.", delete_after=4)
        reason = " ".join(rest[1:]) or "لا يوجد سبب"
        await _dm(member, _mod_embed("تم بانادك", discord.Color.red(),
                                      السيرفر=guild.name, السبب=reason, المشرف=str(author)))
        await guild.ban(member, reason=reason, delete_message_days=0)
        await message.channel.send(embed=_mod_embed("تم الباند", discord.Color.red(),
                                                     العضو=str(member), السبب=reason, المشرف=str(author)), delete_after=8)
        logger.info(f"[BAN] {member} | {reason}")

    # ── unban ──────────────────────────────────────────────────────────────
    elif cmd == "unban":
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "unban [ID] [سبب]")
        try:
            user = await bot.fetch_user(int(rest[0]))
            reason = " ".join(rest[1:]) or "لا يوجد سبب"
            await guild.unban(user, reason=reason)
            await message.channel.send(embed=_mod_embed("تم رفع الباند", discord.Color.green(),
                                                         العضو=str(user), السبب=reason, المشرف=str(author)), delete_after=8)
        except Exception:
            await message.reply("ID غير صحيح أو العضو غير موجود في الباند.", delete_after=4)

    # ── طرد / kick ─────────────────────────────────────────────────────────
    elif cmd in ("طرد", "kick"):
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "طرد @عضو [سبب]")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        if member.top_role >= author.top_role and author != guild.owner:
            return await message.reply("رتبة العضو أعلى منك.", delete_after=4)
        reason = " ".join(rest[1:]) or "لا يوجد سبب"
        await _dm(member, _mod_embed("تم طردك", discord.Color.orange(),
                                      السيرفر=guild.name, السبب=reason, المشرف=str(author)))
        await member.kick(reason=reason)
        await message.channel.send(embed=_mod_embed("تم الطرد", discord.Color.orange(),
                                                     العضو=str(member), السبب=reason, المشرف=str(author)), delete_after=8)
        logger.info(f"[KICK] {member} | {reason}")

    # ── اص / تايم / mute ───────────────────────────────────────────────────
    elif cmd in ("اص", "تايم", "mute"):
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "اص @عضو [مدة] [سبب]   (10m/2h/1d)")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        if member.top_role >= author.top_role and author != guild.owner:
            return await message.reply("رتبة العضو أعلى منك.", delete_after=4)
        duration = rest[1] if len(rest) > 1 else "10m"
        reason   = " ".join(rest[2:]) or "لا يوجد سبب"
        secs = _parse_duration(duration)
        if not secs or secs > 2419200:
            return await message.reply("مدة غير صحيحة. مثال: 10m، 2h، 1d", delete_after=4)
        until = discord.utils.utcnow() + timedelta(seconds=secs)
        await member.timeout(until, reason=reason)
        await _dm(member, _mod_embed("تم إسكاتك", discord.Color.yellow(),
                                      السيرفر=guild.name, المدة=duration, السبب=reason, المشرف=str(author)))
        await message.channel.send(embed=_mod_embed("تم الإسكات", discord.Color.yellow(),
                                                     العضو=str(member), المدة=duration, السبب=reason, المشرف=str(author)), delete_after=8)
        logger.info(f"[MUTE] {member} | {duration} | {reason}")

    # ── فك / unmute ────────────────────────────────────────────────────────
    elif cmd in ("فك", "unmute"):
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "فك @عضو")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        await member.timeout(None)
        await message.channel.send(embed=_mod_embed("تم رفع الإسكات", discord.Color.green(),
                                                     العضو=str(member), المشرف=str(author)), delete_after=8)
        logger.info(f"[UNMUTE] {member}")

    # ── اسم / nickname ─────────────────────────────────────────────────────
    elif cmd in ("اسم", "nick"):
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "اسم @عضو [الاسم الجديد]")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        new_nick = " ".join(rest[1:]).strip() or None
        await member.edit(nick=new_nick)
        text = f"`{new_nick}`" if new_nick else "تم إزالة النك نيم"
        await message.channel.send(embed=_mod_embed("تم تغيير الاسم", discord.Color.blurple(),
                                                     العضو=str(member), الاسم=text, المشرف=str(author)), delete_after=8)
        logger.info(f"[NICK] {member} → {new_nick}")

    # ── رول / addrole ──────────────────────────────────────────────────────
    elif cmd in ("رول", "addrole"):
        if not _is_mod(author): return await _no_perm(message)
        if len(rest) < 2: return await _usage(message, "رول @عضو @رول")
        member = await _resolve_member(guild, rest[0])
        role   = await _resolve_role(guild, rest[1])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        if not role:   return await message.reply("الرول غير موجود.", delete_after=4)
        if role >= author.top_role and author != guild.owner:
            return await message.reply("لا تستطيع إعطاء رول أعلى منك.", delete_after=4)
        await member.add_roles(role, reason=f"بواسطة {author}")
        await message.channel.send(embed=_mod_embed("تم إعطاء الرول", discord.Color.green(),
                                                     العضو=str(member), الرول=role.mention, المشرف=str(author)), delete_after=8)
        logger.info(f"[ROLE+] {member} ← {role.name}")

    # ── تل / removerole ────────────────────────────────────────────────────
    elif cmd in ("تل", "removerole"):
        if not _is_mod(author): return await _no_perm(message)
        if len(rest) < 2: return await _usage(message, "تل @عضو @رول")
        member = await _resolve_member(guild, rest[0])
        role   = await _resolve_role(guild, rest[1])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        if not role:   return await message.reply("الرول غير موجود.", delete_after=4)
        if role >= author.top_role and author != guild.owner:
            return await message.reply("لا تستطيع سحب رول أعلى منك.", delete_after=4)
        await member.remove_roles(role, reason=f"بواسطة {author}")
        await message.channel.send(embed=_mod_embed("تم سحب الرول", discord.Color.orange(),
                                                     العضو=str(member), الرول=role.mention, المشرف=str(author)), delete_after=8)
        logger.info(f"[ROLE-] {member} ✗ {role.name}")

    # ── ق / lock ───────────────────────────────────────────────────────────
    elif cmd in ("ق", "lock"):
        if not _is_mod(author): return await _no_perm(message)
        reason = " ".join(rest) or "لا يوجد سبب"
        ow = message.channel.overwrites_for(guild.default_role)
        ow.send_messages = False
        await message.channel.set_permissions(guild.default_role, overwrite=ow, reason=reason)
        await message.channel.send(embed=_mod_embed("تم قفل الروم", discord.Color.red(),
                                                     الروم=message.channel.mention, السبب=reason, المشرف=str(author)), delete_after=8)
        logger.info(f"[LOCK] #{message.channel.name}")

    # ── ف / unlock ─────────────────────────────────────────────────────────
    elif cmd in ("ف", "unlock"):
        if not _is_mod(author): return await _no_perm(message)
        reason = " ".join(rest) or "لا يوجد سبب"
        ow = message.channel.overwrites_for(guild.default_role)
        ow.send_messages = None
        await message.channel.set_permissions(guild.default_role, overwrite=ow, reason=reason)
        await message.channel.send(embed=_mod_embed("تم فتح الروم", discord.Color.green(),
                                                     الروم=message.channel.mention, السبب=reason, المشرف=str(author)), delete_after=8)
        logger.info(f"[UNLOCK] #{message.channel.name}")

    # ── warn ───────────────────────────────────────────────────────────────
    elif cmd == "warn":
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "warn @عضو [سبب]")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        reason = " ".join(rest[1:]) or "لا يوجد سبب"
        warns  = _load_warns()
        warns.setdefault(str(member.id), []).append({
            "reason": reason, "by": str(author), "at": str(datetime.now(timezone.utc))
        })
        _save_warns(warns)
        count = len(warns[str(member.id)])
        await _dm(member, _mod_embed("تلقيت تحذيراً", discord.Color.yellow(),
                                      السيرفر=guild.name, السبب=reason,
                                      المشرف=str(author), **{"إجمالي تحذيراتك": count}))
        await message.channel.send(embed=_mod_embed("تم التحذير", discord.Color.yellow(),
                                                     العضو=str(member), السبب=reason,
                                                     **{"إجمالي التحذيرات": count}, المشرف=str(author)), delete_after=8)
        logger.info(f"[WARN] {member} | #{count}")

    # ── warns ──────────────────────────────────────────────────────────────
    elif cmd == "warns":
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "warns @عضو")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        w = _load_warns().get(str(member.id), [])
        if not w:
            return await message.reply(f"{member.mention} ليس لديه تحذيرات.")
        embed = discord.Embed(title=f"تحذيرات {member.display_name}",
                              color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        for i, x in enumerate(w, 1):
            embed.add_field(name=f"#{i}", value=f"السبب: {x['reason']}\nبواسطة: {x['by']}", inline=False)
        await message.channel.send(embed=embed, delete_after=8)

    # ── clearwarns ─────────────────────────────────────────────────────────
    elif cmd == "clearwarns":
        if not _is_mod(author): return await _no_perm(message)
        if not rest: return await _usage(message, "clearwarns @عضو")
        member = await _resolve_member(guild, rest[0])
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        warns = _load_warns()
        warns.pop(str(member.id), None)
        _save_warns(warns)
        await message.channel.send(embed=_mod_embed("تم مسح التحذيرات", discord.Color.green(),
                                                     العضو=str(member), المشرف=str(author)), delete_after=8)

    # ── clear ──────────────────────────────────────────────────────────────
    elif cmd == "clear":
        if not _is_mod(author): return await _no_perm(message)
        try:
            amount = int(rest[0]) if rest else 10
        except ValueError:
            return await _usage(message, "clear [عدد]")
        if amount < 1 or amount > 500:
            return await message.reply("العدد بين 1 و 500.", delete_after=4)
        deleted = await message.channel.purge(limit=amount)
        msg = await message.channel.send(f"تم حذف {len(deleted)} رسالة.", delete_after=4)
        logger.info(f"[CLEAR] {len(deleted)} في #{message.channel.name}")

    # ── userinfo ───────────────────────────────────────────────────────────
    elif cmd == "userinfo":
        member = await _resolve_member(guild, rest[0]) if rest else author
        if not member: return await message.reply("العضو غير موجود.", delete_after=4)
        embed = discord.Embed(title=str(member), color=member.color, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID",            value=member.id,                                        inline=True)
        embed.add_field(name="الرتبة",        value=member.top_role.mention,                          inline=True)
        embed.add_field(name="انضم للسيرفر", value=discord.utils.format_dt(member.joined_at, "D"),   inline=True)
        embed.add_field(name="أنشأ الحساب",  value=discord.utils.format_dt(member.created_at, "D"),  inline=True)
        embed.add_field(name="بوت؟",         value="نعم" if member.bot else "لا",                    inline=True)
        embed.add_field(name="التحذيرات",    value=str(len(_load_warns().get(str(member.id), []))),   inline=True)
        await message.channel.send(embed=embed, delete_after=8)

    # ── serverinfo ─────────────────────────────────────────────────────────
    elif cmd == "serverinfo":
        g = guild
        embed = discord.Embed(title=g.name, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="ID",         value=g.id,                                         inline=True)
        embed.add_field(name="الأعضاء",    value=g.member_count,                               inline=True)
        embed.add_field(name="الرومات",    value=len(g.text_channels)+len(g.voice_channels),   inline=True)
        embed.add_field(name="الرتب",      value=len(g.roles),                                 inline=True)
        embed.add_field(name="أُنشئ في",   value=discord.utils.format_dt(g.created_at, "D"),   inline=True)
        embed.add_field(name="الأونر",     value=g.owner.mention if g.owner else "—",          inline=True)
        await message.channel.send(embed=embed, delete_after=8)

    # ── help / مساعدة ──────────────────────────────────────────────────────
    elif cmd in ("help", "مساعدة"):
        embed = discord.Embed(title="أوامر البوت", color=discord.Color.blurple(),
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="برا @عضو [سبب]",          value="باند عضو",                inline=False)
        embed.add_field(name="unban [ID] [سبب]",         value="رفع الباند",              inline=False)
        embed.add_field(name="طرد @عضو [سبب]",          value="طرد عضو",                 inline=False)
        embed.add_field(name="اص @عضو [مدة] [سبب]",     value="إسكات (10m/2h/1d)",      inline=False)
        embed.add_field(name="فك @عضو",                 value="رفع الإسكات",             inline=False)
        embed.add_field(name="اسم @عضو [اسم]",          value="تغيير النك نيم",           inline=False)
        embed.add_field(name="رول @عضو @رول",           value="إعطاء رول",               inline=False)
        embed.add_field(name="تل @عضو @رول",            value="سحب رول",                 inline=False)
        embed.add_field(name="ق [سبب]",                 value="قفل الروم",               inline=False)
        embed.add_field(name="ف [سبب]",                 value="فتح الروم",               inline=False)
        embed.add_field(name="warn @عضو [سبب]",         value="تحذير عضو",              inline=False)
        embed.add_field(name="warns @عضو",              value="عرض التحذيرات",           inline=False)
        embed.add_field(name="clearwarns @عضو",         value="مسح التحذيرات",           inline=False)
        embed.add_field(name="clear [عدد]",             value="مسح رسائل",              inline=False)
        embed.add_field(name="userinfo [@عضو]",         value="معلومات عضو",            inline=False)
        embed.add_field(name="serverinfo",              value="معلومات السيرفر",         inline=False)
        await message.channel.send(embed=embed, delete_after=8)


# =============================================================================
# RUN
# =============================================================================
if not TOKEN:
    logger.critical("DISCORD_TOKEN غير موجود!")
else:
    logger.info("تشغيل البوت...")
    bot.run(TOKEN)
