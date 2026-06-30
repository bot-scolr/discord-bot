"""
Discord Bot - نظام الموافقة على الرومات
=========================================
الوضع 1: اسم روم جديد - يُنشأ الروم بعد الموافقة
الوضع 2: اسم روم موجود - يُضاف المحتوى للروم الموجود بعد الموافقة
"""

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
# LOGGING SETUP
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
logger = logging.getLogger("RoomBot")

# =============================================================================
# CONFIGURATION
# =============================================================================
TOKEN                = os.getenv("DISCORD_TOKEN", "")
ADMIN_CHANNEL_ID     = int(os.getenv("ADMIN_CHANNEL_ID", "0"))
REQUEST_CHANNEL_ID   = int(os.getenv("REQUEST_CHANNEL_ID", "0"))
APPROVED_CATEGORY_ID = int(os.getenv("APPROVED_CATEGORY_ID", "0"))

# =============================================================================
# BOT SETUP
# =============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =============================================================================
# GLOBAL STATE
# =============================================================================
processing_lock:    asyncio.Lock    = asyncio.Lock()
processed_messages: set[int]       = set()
pending_requests:   dict[int, dict] = {}   # {admin_msg_id: request_data}

PENDING_FILE = Path("pending_requests.json")

def _save_pending() -> None:
    try:
        PENDING_FILE.write_text(
            json.dumps({str(k): v for k, v in pending_requests.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error(f"[SAVE] فشل حفظ الطلبات: {exc}")

def _load_pending() -> None:
    if not PENDING_FILE.exists():
        return
    try:
        raw = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        pending_requests.update({int(k): v for k, v in raw.items()})
        logger.info(f"[LOAD] تم تحميل {len(pending_requests)} طلب من الملف.")
    except Exception as exc:
        logger.error(f"[LOAD] فشل تحميل الطلبات: {exc}")

# =============================================================================
# HELPERS
# =============================================================================

def sanitize_channel_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\s\u0600-\u06FF\-]", "", name)
    name = re.sub(r"\s+", "-", name)
    name = name.strip("-")
    return name[:100] or "روم-جديد"


def get_attachment_type_label(attachment: discord.Attachment) -> str:
    ct = (attachment.content_type or "").lower()
    fn = attachment.filename.lower()
    if ct.startswith("image/gif") or fn.endswith(".gif"):
        return "GIF"
    if ct.startswith("image/"):
        return "صورة"
    if ct.startswith("video/"):
        return "فيديو"
    if ct.startswith("audio/") or fn.endswith((".ogg", ".wav", ".mp3", ".m4a", ".flac")):
        return "صوت/فويس"
    return "ملف"


def build_attachment_summary(attachments: list[dict]) -> str:
    if not attachments:
        return "_لا توجد مرفقات_"
    counts: dict[str, int] = {}
    for att in attachments:
        counts[att["type_label"]] = counts.get(att["type_label"], 0) + 1
    return "\n".join(f"{lbl}: **{cnt}**" for lbl, cnt in counts.items())


def find_existing_channel(
    guild: discord.Guild, room_name: str
) -> Optional[discord.TextChannel]:
    normalized = room_name.lower().replace(" ", "-")
    for ch in guild.text_channels:
        if ch.name.lower() == normalized:
            return ch
    return None


async def _notify_member(
    guild: discord.Guild,
    member_id: int,
    approved: bool,
    room_name: str,
    channel: Optional[discord.TextChannel],
    reviewer: discord.Member,
) -> None:
    member = guild.get_member(member_id)
    if not member:
        logger.warning(f"[DM] العضو {member_id} غير موجود في السيرفر.")
        return

    if approved:
        embed = discord.Embed(
            title="تمت الموافقة على طلبك",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="الروم",   value=room_name,                                  inline=True)
        embed.add_field(name="الرابط",  value=channel.mention if channel else "—",         inline=True)
        embed.add_field(name="المراجع", value=str(reviewer),                               inline=True)
        embed.set_footer(text=f"السيرفر: {guild.name}",
                         icon_url=guild.icon.url if guild.icon else None)
    else:
        embed = discord.Embed(
            title="تم رفض طلبك",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="الروم المطلوب", value=room_name,      inline=True)
        embed.add_field(name="المراجع",       value=str(reviewer),  inline=True)
        embed.set_footer(text=f"السيرفر: {guild.name}",
                         icon_url=guild.icon.url if guild.icon else None)

    try:
        await member.send(embed=embed)
        logger.info(f"[DM] ارسل اشعار {'قبول' if approved else 'رفض'} لـ {member}")
    except discord.Forbidden:
        logger.warning(f"[DM] {member} اغلق الرسائل الخاصة.")
    except Exception as exc:
        logger.error(f"[DM] خطأ في ارسال DM لـ {member}: {exc}")


WATERMARK_TEXT = "© Depth Of School"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _add_watermark(raw: bytes, filename: str) -> bytes:
    """أضف نص حقوق في ثلاث مواضع: فوق يمين، وسط، تحت يسار."""
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        w, h = img.size

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)

        font_size = max(16, w // 28)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size
            )
        except Exception:
            font = ImageFont.load_default()

        bbox   = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margin = 12

        positions = [
            (w - tw - margin,       margin),            # فوق يمين
            ((w - tw) // 2,         (h - th) // 2),     # وسط
            (margin,                h - th - margin),   # تحت يسار
        ]

        for (x, y) in positions:
            # ظل
            draw.text((x + 2, y + 2), WATERMARK_TEXT, font=font, fill=(0, 0, 0, 160))
            # النص الأبيض
            draw.text((x,     y    ), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 210))

        out = Image.alpha_composite(img, overlay).convert("RGB")
        buf = io.BytesIO()
        ext = Path(filename).suffix.lower()
        fmt = "JPEG" if ext in (".jpg", ".jpeg") else "PNG"
        out.save(buf, format=fmt, quality=92)
        buf.seek(0)
        return buf.read()
    except Exception as exc:
        logger.warning(f"[WATERMARK] فشل إضافة الحقوق على {filename}: {exc}")
        return raw


async def _download_attachment(session: aiohttp.ClientSession, att: dict) -> Optional[discord.File]:
    """حمّل المرفق وأضف الحقوق إن كان صورة، ثم أرجعه كـ discord.File."""
    for url_key in ("url", "proxy_url"):
        url = att.get(url_key)
        if not url:
            continue
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    logger.warning(f"[ATTACH] HTTP {resp.status} لـ {att['filename']} ({url_key})")
                    continue
                raw = await resp.read()

                # أضف الحقوق المائية للصور فقط (ليس الفيديو أو الصوت)
                ext = Path(att["filename"]).suffix.lower()
                if ext in IMAGE_EXTS and att.get("type_label") in ("صورة", "GIF"):
                    raw = _add_watermark(raw, att["filename"])

                logger.info(f"[ATTACH] {att['filename']} ({att['type_label']})")
                return discord.File(fp=io.BytesIO(raw), filename=att["filename"])
        except asyncio.TimeoutError:
            logger.warning(f"[ATTACH] Timeout - {att['filename']} ({url_key})")
        except Exception as exc:
            logger.error(f"[ATTACH] خطأ - {att['filename']} ({url_key}): {exc}")
    return None


async def send_all_content(
    channel: discord.TextChannel,
    data: dict,
    guild: discord.Guild,
    *,
    is_addition: bool = False,
) -> None:
    """أرسل الكلام + جميع المرفقات في رسالة وحدة (أو دفعات إن تجاوزت 10)."""

    text = data["description"] or ""

    # جهّز الملفات أولاً
    files: list[discord.File] = []
    async with aiohttp.ClientSession() as session:
        for att in data["attachments"]:
            f = await _download_attachment(session, att)
            if f:
                files.append(f)
            else:
                logger.error(f"[ATTACH] فشل تحميل {att['filename']}")

    # الرسالة الأولى: إما embed (روم جديد) أو نص + أول 10 ملفات
    BATCH = 10
    first_files  = files[:BATCH]
    extra_files  = files[BATCH:]

    # كلام فوق + ملفات — بدون أي embed أو header
    await channel.send(
        content=text or None,
        files=first_files if first_files else discord.utils.MISSING,
    )

    # دفعات إضافية لو كان في أكثر من 10 ملفات
    for i in range(0, len(extra_files), BATCH):
        await channel.send(files=extra_files[i : i + BATCH])

# =============================================================================
# APPROVAL VIEW
# =============================================================================

class ApprovalView(discord.ui.View):

    def __init__(self, admin_msg_id: int = 0):
        super().__init__(timeout=None)
        self.admin_msg_id = admin_msg_id

    @discord.ui.button(
        label="قبول",
        style=discord.ButtonStyle.success,
        custom_id="room_approve",
    )
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)

        data = pending_requests.get(interaction.message.id)
        if not data:
            await interaction.followup.send(
                "لم يتم العثور على بيانات الطلب. ربما اُعيد تشغيل البوت.",
                ephemeral=True,
            )
            return

        await self._update_embed(
            interaction,
            status="تمت الموافقة",
            color=discord.Color.green(),
            reviewer=interaction.user,
        )
        await self._handle_approval(interaction, data)

    @discord.ui.button(
        label="رفض",
        style=discord.ButtonStyle.danger,
        custom_id="room_reject",
    )
    async def reject_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)

        data = pending_requests.get(interaction.message.id)
        if not data:
            await interaction.followup.send(
                "لم يتم العثور على بيانات الطلب.",
                ephemeral=True,
            )
            return

        await self._update_embed(
            interaction,
            status="مرفوض",
            color=discord.Color.red(),
            reviewer=interaction.user,
        )
        pending_requests.pop(interaction.message.id, None)
        _save_pending()
        logger.info(
            f"[REJECT] {interaction.user} رفض طلب '{data['room_name']}' "
            f"(وضع: {data['mode']}) من {data['member_name']}"
        )
        await interaction.followup.send("تم رفض الطلب.", ephemeral=True)
        await _notify_member(
            guild=interaction.guild,
            member_id=data["member_id"],
            approved=False,
            room_name=data["room_name"],
            channel=None,
            reviewer=interaction.user,
        )

    async def _update_embed(
        self,
        interaction: discord.Interaction,
        status: str,
        color: discord.Color,
        reviewer: discord.Member,
    ):
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0]
        embed.color = color

        new_fields = []
        for field in embed.fields:
            if field.name == "الحالة":
                new_fields.append({"name": "الحالة", "value": status, "inline": True})
            else:
                new_fields.append(
                    {"name": field.name, "value": field.value, "inline": field.inline}
                )
        embed.clear_fields()
        for f in new_fields:
            embed.add_field(**f)

        embed.add_field(name="المراجع",      value=reviewer.mention,                                  inline=True)
        embed.add_field(name="وقت المراجعة", value=discord.utils.format_dt(datetime.now(timezone.utc), "F"), inline=True)
        await interaction.message.edit(embed=embed, view=self)

    async def _handle_approval(self, interaction: discord.Interaction, data: dict):
        guild = interaction.guild
        mode  = data["mode"]

        if mode == "existing":
            channel = guild.get_channel(data["existing_channel_id"])
            if not channel:
                channel = find_existing_channel(guild, data["room_name"])

            if not channel:
                await interaction.followup.send(
                    f"الروم `{data['room_name']}` لم يُعد موجوداً.",
                    ephemeral=True,
                )
                pending_requests.pop(interaction.message.id, None)
                return

            await send_all_content(channel, data, guild, is_addition=True)
            pending_requests.pop(interaction.message.id, None)
            _save_pending()
            await interaction.followup.send(
                f"تم اضافة المحتوى الى: {channel.mention}", ephemeral=True
            )
            logger.info(
                f"[APPROVE-ADD] {interaction.user} اضاف محتوى لـ #{channel.name} "
                f"من {data['member_name']}"
            )
            await _notify_member(
                guild=guild,
                member_id=data["member_id"],
                approved=True,
                room_name=data["room_name"],
                channel=channel,
                reviewer=interaction.user,
            )

        else:
            try:
                category: Optional[discord.CategoryChannel] = None
                if APPROVED_CATEGORY_ID:
                    category = guild.get_channel(APPROVED_CATEGORY_ID)

                channel = await guild.create_text_channel(
                    name=data["room_name"],
                    category=category,
                    topic=(data["description"] or "")[:1024] or None,
                    reason=f"طلب موافق عليه من {data['member_name']}",
                )
                logger.info(f"[CREATE] #{channel.name} (ID: {channel.id})")

            except discord.Forbidden:
                logger.error("[CREATE] لا يوجد صلاحية لانشاء الروم.")
                await interaction.followup.send(
                    "البوت لا يملك صلاحية انشاء الروم.", ephemeral=True
                )
                return
            except Exception as exc:
                logger.exception(f"[CREATE] خطأ: {exc}")
                await interaction.followup.send(f"حدث خطأ: {exc}", ephemeral=True)
                return

            await send_all_content(channel, data, guild, is_addition=False)
            pending_requests.pop(interaction.message.id, None)
            _save_pending()
            await interaction.followup.send(
                f"تم انشاء الروم: {channel.mention}", ephemeral=True
            )
            logger.info(
                f"[APPROVE-NEW] {interaction.user} انشأ #{channel.name} "
                f"من {data['member_name']}"
            )
            await _notify_member(
                guild=guild,
                member_id=data["member_id"],
                approved=True,
                room_name=data["room_name"],
                channel=channel,
                reviewer=interaction.user,
            )

# =============================================================================
# EVENT: on_ready
# =============================================================================

@bot.event
async def on_ready():
    _load_pending()
    logger.info(f"البوت جاهز: {bot.user} (ID: {bot.user.id})")
    logger.info(f"   روم الادارة    : {ADMIN_CHANNEL_ID}")
    logger.info(f"   روم الطلبات    : {REQUEST_CHANNEL_ID}")
    logger.info(f"   الكاتيقوري     : {APPROVED_CATEGORY_ID or 'غير محدد'}")
    bot.add_view(ApprovalView())

# =============================================================================
# EVENT: on_message
# =============================================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.channel.id != REQUEST_CHANNEL_ID:
        return

    # تجاهل الرسائل الفارغة (بدون نص ومرفقات)
    if not message.content.strip() and not message.attachments:
        return

    async with processing_lock:
        if message.id in processed_messages:
            logger.warning(f"[SKIP] رسالة مكررة: {message.id}")
            return
        processed_messages.add(message.id)

    await handle_room_request(message)

# =============================================================================
# HANDLER: معالجة الطلب
# =============================================================================

async def handle_room_request(message: discord.Message):
    admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if not admin_channel:
        logger.error(f"[ERROR] روم الادارة غير موجود: {ADMIN_CHANNEL_ID}")
        return

    # ── تحليل الرسالة ─────────────────────────────────────────────────────
    # السطر الأول: اسم الروم
    # باقي الأسطر: الكلام / الوصف
    lines = [l for l in (message.content or "").strip().splitlines() if l.strip()]
    raw_name    = lines[0].strip() if lines else f"روم-{message.author.display_name}"
    description = "\n".join(lines[1:]) if len(lines) > 1 else ""
    room_name   = sanitize_channel_name(raw_name)

    existing_channel = find_existing_channel(message.guild, room_name)
    mode = "existing" if existing_channel else "new"

    attachments_data = [
        {
            "filename":     att.filename,
            "url":          att.url,
            "proxy_url":    att.proxy_url,
            "size":         att.size,
            "type_label":   get_attachment_type_label(att),
            "content_type": att.content_type or "",
        }
        for att in message.attachments
    ]

    if mode == "existing":
        embed_title = "طلب اضافة محتوى لروم موجود"
        embed_color = discord.Color.blue()
        mode_label  = f"اضافة الى {existing_channel.mention}"
    else:
        embed_title = "طلب انشاء روم جديد"
        embed_color = discord.Color.gold()
        mode_label  = "انشاء روم جديد"

    embed = discord.Embed(
        title=embed_title,
        color=embed_color,
        timestamp=message.created_at,
    )
    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(name="العضو",       value=message.author.mention,                           inline=True)
    embed.add_field(name="الروم",       value=f"`{room_name}`",                                 inline=True)
    embed.add_field(name="الحالة",      value="بانتظار المراجعة",                               inline=True)
    embed.add_field(name="النوع",       value=mode_label,                                       inline=False)
    embed.add_field(name="وقت الارسال", value=discord.utils.format_dt(message.created_at, "F"), inline=False)
    embed.add_field(
        name="الكلام",
        value=(description or message.content or "_لا يوجد نص_")[:1024],
        inline=False,
    )
    embed.add_field(name="المرفقات", value=build_attachment_summary(attachments_data), inline=True)
    embed.add_field(name="العدد",    value=str(len(attachments_data)),                 inline=True)
    embed.set_footer(text=f"Message ID: {message.id} - {message.guild.name}")

    view      = ApprovalView()
    admin_msg = await admin_channel.send(embed=embed, view=view)

    view.admin_msg_id = admin_msg.id
    pending_requests[admin_msg.id] = {
        "member_id":           message.author.id,
        "member_name":         message.author.display_name,
        "room_name":           room_name,
        "description":         description,
        "text":                message.content or "",
        "attachments":         attachments_data,
        "mode":                mode,
        "existing_channel_id": existing_channel.id if existing_channel else None,
    }
    _save_pending()

    logger.info(
        f"[REQUEST] من {message.author} | روم: {room_name} | "
        f"وضع: {mode} | مرفقات: {len(attachments_data)}"
    )

    if attachments_data:
        await admin_channel.send(
            f"معاينة المرفقات ({len(attachments_data)}) - {room_name}"
        )
        for att in message.attachments:
            try:
                file  = await att.to_file(use_cached=True)
                label = get_attachment_type_label(att)
                await admin_channel.send(content=f"{label} - `{att.filename}`", file=file)
            except Exception as exc:
                logger.warning(f"[PREVIEW] تعذّر ارسال {att.filename}: {exc}")
                await admin_channel.send(
                    f"تعذّر معاينة `{att.filename}` - [رابط مباشر]({att.url})"
                )

    try:
        await message.delete()
        logger.info(f"[DELETE] تم حذف رسالة الطلب {message.id} من روم الطلبات")
    except Exception as exc:
        logger.warning(f"[DELETE] تعذّر حذف الرسالة {message.id}: {exc}")

# =============================================================================
# COMMANDS
# =============================================================================

@bot.command(name="ping")
async def cmd_ping(ctx: commands.Context):
    await ctx.send(f"Pong! `{round(bot.latency * 1000)}ms`")


@bot.command(name="status")
@commands.has_permissions(administrator=True)
async def cmd_status(ctx: commands.Context):
    embed = discord.Embed(title="حالة البوت", color=discord.Color.blurple())
    embed.add_field(name="طلبات معلقة",   value=str(len(pending_requests)),    inline=True)
    embed.add_field(name="رسائل معالجة",  value=str(len(processed_messages)),  inline=True)
    embed.add_field(name="Latency",        value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="روم الادارة",   value=f"<#{ADMIN_CHANNEL_ID}>",      inline=True)
    embed.add_field(name="روم الطلبات",   value=f"<#{REQUEST_CHANNEL_ID}>",    inline=True)
    await ctx.send(embed=embed)


@bot.command(name="clear_pending")
@commands.has_permissions(administrator=True)
async def cmd_clear_pending(ctx: commands.Context):
    count = len(pending_requests)
    pending_requests.clear()
    await ctx.send(f"تم مسح `{count}` طلب معلق من الذاكرة.")

# =============================================================================
# MAIN
# =============================================================================

def main():
    if not TOKEN:
        logger.critical("DISCORD_TOKEN غير محدد!")
        raise SystemExit(1)
    if not ADMIN_CHANNEL_ID:
        logger.critical("ADMIN_CHANNEL_ID غير محدد!")
        raise SystemExit(1)
    if not REQUEST_CHANNEL_ID:
        logger.critical("REQUEST_CHANNEL_ID غير محدد!")
        raise SystemExit(1)

    logger.info("تشغيل البوت...")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
