import discord
from discord.ext import commands
import os
import io
import aiohttp
import asyncio
import tempfile
import uuid
import time
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.environ.get("DISCORD_TOKEN")

REQUEST_CHANNEL_ID = 1520402264441884672
LOG_CHANNEL_ID = 1520405217210929162
CATEGORY_ID = 1508218078058774555
ADMIN_USER_ID = 1483889802876158014
REVIEW_CHANNEL_ID = 1520402893126111473

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

# ── حماية التكرار ────────────────────────────────────────────────────────────
processed_messages = set()  # message_id → مُعالج مسبقاً في قناة الطلبات
processing_lock = asyncio.Lock()
accepting_requests = set()  # request_id → جاري القبول الآن (حماية ضغطتين متزامنتين)
accepted_requests = set()  # request_id → تمّ قبوله بالفعل (حماية الضغط مرة ثانية)

file_store = {}  # request_id → {"files": [...]}
raw_snapshot_cache = {}  # message_id (int) → list of raw attachment dicts من message_snapshots
waiting_for_name = set()  # user_id → ينتظر رد على سؤال اسم الروم

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".m4a", ".aac", ".opus"}

MAX_MSG_SIZE = 8 * 1024 * 1024  # 8 MB حد الرسالة الواحدة
MAX_MSG_FILES = 10  # أقصى ملفات في رسالة واحدة


# ── تحميل الملف ──────────────────────────────────────────────────────────────
async def download_file(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()


# ── الخط ──────────────────────────────────────────────────────────────────────
def find_font(size: int):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/nix/store/1bkxf69hxs39mi6l5bxs89ym1qpyjzq-dejavu-fonts-2.37/share/fonts/truetype/DejaVuSans-Bold.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ── واترمارك الصور ────────────────────────────────────────────────────────────
def add_watermark(img_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        draw = ImageDraw.Draw(img)

        text = "© Depth Of School"
        font_size = max(22, img.width // 20)
        font = find_font(font_size)

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        m = max(12, img.width // 50)

        positions = [
            (m, img.height // 5 - text_h // 2),
            (img.width // 2 - text_w // 2, img.height // 2 - text_h // 2),
            (img.width - text_w - m, img.height * 4 // 5 - text_h // 2),
        ]

        for x, y in positions:
            draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 150))
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 200))

        output = io.BytesIO()
        img.convert("RGB").save(output, format="JPEG", quality=95)
        return output.getvalue()
    except Exception as e:
        print(f"فشل الواترمارك: {e}")
        return img_bytes


# ── واترمارك الفيديو ──────────────────────────────────────────────────────────
async def add_video_watermark(video_bytes: bytes, ext: str) -> bytes:
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        fontfile_arg = f":fontfile={font_path}" if os.path.exists(font_path) else ""

        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f_in:
            f_in.write(video_bytes)
            in_path = f_in.name

        out_path = in_path.replace(f".{ext}", "_wm.mp4")
        text = "© Depth Of School"
        base = "fontsize=w/22:fontcolor=white@0.85:shadowcolor=black@0.6:shadowx=2:shadowy=2"
        drawtext = (
            f"drawtext=text='{text}'{fontfile_arg}:{base}:x=20:y=h/5-text_h/2,"
            f"drawtext=text='{text}'{fontfile_arg}:{base}:x=(w-text_w)/2:y=(h-text_h)/2,"
            f"drawtext=text='{text}'{fontfile_arg}:{base}:x=w-text_w-20:y=h*4/5-text_h/2"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            in_path,
            "-vf",
            drawtext,
            "-c:a",
            "copy",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            out_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        os.unlink(in_path)

        if proc.returncode != 0:
            print(f"ffmpeg error: {stderr.decode()[-500:]}")
            return video_bytes

        with open(out_path, "rb") as f:
            result = f.read()
        os.unlink(out_path)
        print(f"واترمارك الفيديو جاهز ({len(result)} bytes)")
        return result

    except Exception as e:
        print(f"فشل واترمارك الفيديو: {e}")
        return video_bytes


# ── تحديد نوع الملف ──────────────────────────────────────────────────────────
def detect_file_type(filename: str, content_type: str):
    fn = filename.lower()
    ct = (content_type or "").lower()

    if any(fn.endswith(ext) for ext in IMAGE_EXTS):
        return "image"
    if any(fn.endswith(ext) for ext in VIDEO_EXTS):
        return "video"
    if any(fn.endswith(ext) for ext in AUDIO_EXTS):
        return "audio"
    if "image" in ct:
        return "image"
    if "video" in ct:
        return "video"
    if "audio" in ct or "ogg" in ct:
        return "audio"
    return None


# ── معالجة المرفقات ───────────────────────────────────────────────────────────
async def process_attachments(attachments):
    """
    يُعالج المرفقات ويُرجع:
      raw_files       : [(bytes, filename), ...]  — للمعاينة في قناة المراجعة
      processed_files : [{"bytes": ..., "filename": ...}, ...]  — للإرسال للروم
    الترتيب محفوظ، ولا نسختين لنفس الملف.
    """
    raw_files = []
    processed_files = []
    img_count = vid_count = aud_count = 0

    for i, attachment in enumerate(attachments):
        file_type = detect_file_type(
            attachment.filename, getattr(attachment, "content_type", "") or ""
        )
        if file_type is None:
            print(f"تخطي ملف غير مدعوم: {attachment.filename}")
            continue

        try:
            print(
                f"تحميل {i + 1}/{len(attachments)}: {attachment.filename} ({file_type})"
            )
            raw_bytes = await download_file(attachment.url)

            orig_ext = (
                attachment.filename.rsplit(".", 1)[-1].lower()
                if "." in attachment.filename
                else (
                    "ogg"
                    if file_type == "audio"
                    else ("mp4" if file_type == "video" else "jpg")
                )
            )

            if file_type == "image":
                img_count += 1
                processed = add_watermark(raw_bytes)
                stored_filename = f"image_{img_count}.jpg"
                raw_filename = f"preview_{i + 1}.{orig_ext}"

            elif file_type == "video":
                vid_count += 1
                print(f"إضافة واترمارك على الفيديو {vid_count}...")
                processed = await add_video_watermark(raw_bytes, orig_ext)
                stored_filename = f"video_{vid_count}.mp4"
                raw_filename = f"preview_{i + 1}.{orig_ext}"

            else:  # audio
                aud_count += 1
                processed = raw_bytes  # الصوت بدون واترمارك
                stored_filename = f"voice_{aud_count}.{orig_ext}"
                raw_filename = f"preview_{i + 1}.{orig_ext}"

            raw_files.append((raw_bytes, raw_filename))
            processed_files.append({"bytes": processed, "filename": stored_filename})
            print(f"جاهز: {stored_filename} ({len(processed)} bytes)")

        except Exception as e:
            print(f"فشل معالجة {attachment.filename}: {e}")

    return raw_files, processed_files


# ── كائن وهمي لمرفقات REST ────────────────────────────────────────────────────
class _FakeAttachment:
    def __init__(self, data: dict):
        self.id = str(data.get("id", ""))
        self.filename = data.get("filename", "voice.ogg")
        self.url = data.get("url", "")
        self.content_type = data.get("content_type", "")


# ── جمع جميع المرفقات بدون تكرار ────────────────────────────────────────────
async def fetch_all_attachments(message) -> list:
    """
    يجمع جميع مرفقات الرسالة بدون تكرار باستخدام id أو url كمفتاح فريد.
    يدعم: المرفقات المباشرة، الفوروارد (عبر raw_snapshot_cache)، والريبلاي.
    """
    seen = set()
    result = []

    def add(att):
        att_id = str(getattr(att, "id", "") or "").strip()
        att_url = str(getattr(att, "url", "") or "").strip()
        key = att_id if att_id else att_url
        if key and key not in seen:
            seen.add(key)
            result.append(att)

    # 1) مرفقات مباشرة
    for att in message.attachments:
        add(att)

    # 2) فوروارد — من الكاش الذي ملأه patch_parsers
    for att_data in raw_snapshot_cache.pop(message.id, []):
        fake = _FakeAttachment(att_data)
        add(fake)
        print(f"[SNAP] فوروارد: {fake.filename} | {fake.content_type}")

    # 3) ريبلاي — reference.resolved أو REST API
    if message.reference and message.reference.message_id:
        ref_ch_id = message.reference.channel_id
        ref_msg_id = message.reference.message_id

        if message.reference.resolved:
            for att in message.reference.resolved.attachments:
                add(att)
            print(f"[REF] resolved: {len(message.reference.resolved.attachments)} مرفق")

        # REST fallback إذا لم تأتِ المرفقات عبر resolved
        if not result:
            try:
                orig = await bot.http.get_message(ref_ch_id, ref_msg_id)
                for att_data in orig.get("attachments", []):
                    fake = _FakeAttachment(att_data)
                    add(fake)
                    print(f"[REF-API] {fake.filename} | {fake.content_type}")
            except Exception as e:
                print(f"[REF-API] فشل ({ref_ch_id}/{ref_msg_id}): {e}")

    print(f"[ATT] إجمالي مرفقات الرسالة {message.id}: {len(result)}")
    return result


# ── اعتراض MESSAGE_CREATE لدعم الفوروارد ────────────────────────────────────
def patch_parsers(connection):
    original = connection.parsers.get("MESSAGE_CREATE")

    def patched_create(data):
        msg_id = int(data.get("id", 0))
        snapshots = data.get("message_snapshots", [])
        if snapshots:
            att_list = []
            for snap in snapshots:
                for att in snap.get("message", {}).get("attachments", []):
                    att_list.append(att)
            if att_list:
                raw_snapshot_cache[msg_id] = att_list
                print(
                    f"[SNAP] رسالة {msg_id}: {len(att_list)} مرفق محفوظ من forward snapshots"
                )
        if original:
            original(data)

    connection.parsers["MESSAGE_CREATE"] = patched_create
    print("[PATCH] تم تصحيح MESSAGE_CREATE parser لدعم message_snapshots")


# ── مساعد: البحث عن قناة بالاسم ──────────────────────────────────────────────
def find_channel_by_name(guild: discord.Guild, name: str):
    name_clean = name.strip().replace(" ", "-").lower()
    for ch in guild.text_channels:
        if ch.name.lower() in (name_clean, name.strip().lower()):
            return ch
    return None


# ── إرسال الملفات على دفعات (حسب حدود Discord) ───────────────────────────────
async def send_files_in_chunks(
    channel, files_data: list, extra_content: str = "", view=None
):
    """
    يُرسل قائمة الملفات للقناة مع احترام:
      - حد 10 ملفات لكل رسالة
      - حد 8 MB لكل رسالة
    الأزرار تُرفق فقط مع أول رسالة.
    الملفات المكررة (بنفس الاسم + الحجم) تُحذف تلقائياً.
    """
    if not files_data:
        return

    # إزالة المكررات بالحفاظ على الترتيب
    seen_keys = set()
    unique = []
    for item_bytes, item_name in files_data:
        key = (item_name, len(item_bytes))
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append((item_bytes, item_name))
    files_data = unique

    chunk = []
    chunk_size = 0
    first_msg = True

    for item_bytes, item_name in files_data:
        file_size = len(item_bytes)
        should_flush = chunk and (
            len(chunk) >= MAX_MSG_FILES or chunk_size + file_size > MAX_MSG_SIZE
        )

        if should_flush:
            discord_files = [
                discord.File(io.BytesIO(b), filename=fn) for b, fn in chunk
            ]
            if first_msg:
                await channel.send(
                    content=extra_content, files=discord_files, view=view
                )
                first_msg = False
            else:
                await channel.send(files=discord_files)
            chunk = []
            chunk_size = 0

        chunk.append((item_bytes, item_name))
        chunk_size += file_size

    if chunk:
        discord_files = [discord.File(io.BytesIO(b), filename=fn) for b, fn in chunk]
        if first_msg:
            await channel.send(content=extra_content, files=discord_files, view=view)
        else:
            await channel.send(files=discord_files)


# ── إرسال الطلب لقناة المراجعة ───────────────────────────────────────────────
async def send_review(
    user_id: int,
    guild_id: int,
    room_name: str,
    extra_text: str,
    raw_files: list,
    request_id: str,
    target_channel_id=None,
):
    try:
        review_channel = await bot.fetch_channel(REVIEW_CHANNEL_ID)

        if target_channel_id:
            header = f"**طلب اضافة لروم موجود**\n**الروم:** <#{target_channel_id}>"
        else:
            header = f"**طلب روم جديد**\n**اسم الروم:** {room_name}"

        if extra_text:
            header += f"\n**الكلام:** {extra_text}"
        header += f"\n**من:** <@{user_id}>"
        header += f"\n**عدد الملفات:** {len(raw_files)}"

        view = ReviewView(
            user_id=user_id,
            room_name=room_name,
            guild_id=guild_id,
            extra_text=extra_text,
            target_channel_id=target_channel_id,
            request_id=request_id,
        )

        await send_files_in_chunks(
            channel=review_channel,
            files_data=raw_files,
            extra_content=header,
            view=view,
        )
        print(f"تم إرسال الطلب للمراجعة [{request_id}] - {len(raw_files)} ملف")

    except Exception as e:
        print(f"خطأ في إرسال الطلب للقناة: {e}")


# ── زر المراجعة ──────────────────────────────────────────────────────────────
class ReviewView(discord.ui.View):
    def __init__(
        self,
        user_id,
        room_name,
        guild_id,
        request_id,
        extra_text="",
        target_channel_id=None,
    ):
        super().__init__(timeout=86400)
        self.user_id = user_id
        self.room_name = room_name
        self.guild_id = guild_id
        self.extra_text = extra_text
        self.target_channel_id = target_channel_id
        self.request_id = request_id

        accept_btn = discord.ui.Button(
            label="قبول",
            style=discord.ButtonStyle.success,
            custom_id=f"accept_{request_id}",
        )
        reject_btn = discord.ui.Button(
            label="رفض",
            style=discord.ButtonStyle.danger,
            custom_id=f"reject_{request_id}",
        )
        accept_btn.callback = self.accept_callback
        reject_btn.callback = self.reject_callback
        self.add_item(accept_btn)
        self.add_item(reject_btn)

    async def accept_callback(self, interaction: discord.Interaction):
        rid = self.request_id

        # ── حماية الضغط المزدوج ───────────────────────────────────────────
        if rid in accepted_requests:
            await interaction.response.send_message(
                "⚠️ هذا الطلب تمت معالجته مسبقاً.", ephemeral=True
            )
            return
        if rid in accepting_requests:
            await interaction.response.send_message(
                "⏳ الطلب يُعالج الآن، لا تضغط مرة ثانية.", ephemeral=True
            )
            return

        accepting_requests.add(rid)
        print(f"قبول [{rid}] user={self.user_id}")

        await interaction.response.defer()

        try:
            await interaction.message.delete()
        except Exception as e:
            print(f"فشل حذف رسالة الطلب: {e}")

        guild = bot.get_guild(self.guild_id)
        dest_channel = None

        # ── تحديد القناة الهدف ────────────────────────────────────────────
        if self.target_channel_id:
            try:
                dest_channel = await bot.fetch_channel(self.target_channel_id)
            except Exception as e:
                print(f"فشل جلب القناة: {e}")
        else:
            if guild:
                try:
                    channel_name = (
                        self.room_name.replace(" ", "-")
                        if self.room_name
                        else "روم-جديد"
                    )
                    category = guild.get_channel(CATEGORY_ID)
                    private_role = guild.get_role(1515958292969689098)
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(
                            view_channel=False
                        ),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            embed_links=True,
                            attach_files=True,
                            read_message_history=True,
                        ),
                    }
                    if private_role:
                        overwrites[private_role] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=False,
                            read_message_history=True,
                        )
                    dest_channel = await guild.create_text_channel(
                        name=channel_name,
                        category=category,
                        overwrites=overwrites,
                        reason="طلب مقبول",
                    )
                    print(f"تم إنشاء القناة: {dest_channel.name}")
                except Exception as e:
                    print(f"فشل إنشاء القناة: {e}")

        # ── إرسال الملفات للروم ───────────────────────────────────────────
        send_success = False
        if dest_channel:
            try:
                stored = file_store.get(rid)
                if stored:
                    all_files = stored["files"]
                    files_data = [(f["bytes"], f["filename"]) for f in all_files]
                    print(f"إرسال {len(files_data)} ملف -> {dest_channel.name}")
                    await send_files_in_chunks(
                        channel=dest_channel, files_data=files_data
                    )
                    send_success = True
                    del file_store[rid]  # حذف بعد نجاح الإرسال
                    accepted_requests.add(rid)  # تأشير كمقبول نهائياً
                else:
                    print(f"الملفات غير موجودة في المخزن [{rid}]")
            except Exception as e:
                print(f"فشل إرسال الملفات: {e}")

        accepting_requests.discard(rid)

        # ── إشعار المستخدم ────────────────────────────────────────────────
        user = bot.get_user(self.user_id)
        if user:
            try:
                msg = "**تم قبول طلبك!**"
                if dest_channel:
                    msg += f"\n\nالروم: {dest_channel.mention}"
                await user.send(msg)
            except Exception:
                pass

        # ── سجل اللوق ─────────────────────────────────────────────────────
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
            embed = discord.Embed(
                title="طلب مقبول",
                color=discord.Color.green(),
                timestamp=datetime.utcnow(),
            )
            embed.add_field(name="العضو", value=f"<@{self.user_id}>", inline=True)
            embed.add_field(name="الروم", value=self.room_name, inline=True)
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"فشل اللوق: {e}")

    async def reject_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            RejectModal(
                self.user_id, self.room_name, self.request_id, interaction.message
            )
        )


# ── نافذة سبب الرفض ──────────────────────────────────────────────────────────
class RejectModal(discord.ui.Modal, title="سبب الرفض"):
    reason = discord.ui.TextInput(
        label="ادخل سبب الرفض",
        placeholder="مثال: الصورة غير واضحة...",
        required=True,
        max_length=500,
    )

    def __init__(self, user_id, room_name, request_id, review_message):
        super().__init__()
        self.user_id = user_id
        self.room_name = room_name
        self.request_id = request_id
        self.review_message = review_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            await self.review_message.delete()
        except Exception as e:
            print(f"فشل حذف رسالة الطلب: {e}")

        user = bot.get_user(self.user_id)
        if user:
            try:
                await user.send(f"**تم رفض طلبك**\n\n**السبب:** {self.reason.value}")
            except Exception:
                pass

        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
            embed = discord.Embed(
                title="طلب مرفوض",
                color=discord.Color.red(),
                timestamp=datetime.utcnow(),
            )
            embed.add_field(name="العضو", value=f"<@{self.user_id}>", inline=True)
            embed.add_field(name="الروم", value=self.room_name, inline=True)
            embed.add_field(name="السبب", value=self.reason.value, inline=False)
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"فشل اللوق: {e}")

        file_store.pop(self.request_id, None)
        accepted_requests.discard(self.request_id)  # تنظيف إذا رُفض


# ── معالجة مشتركة للطلبات ────────────────────────────────────────────────────
async def handle_request(
    message, room_name: str, extra_text: str, target_channel_id=None
):
    all_attachments = await fetch_all_attachments(message)
    if not all_attachments:
        return False, "no_files"

    raw_files, processed_files = await process_attachments(all_attachments)
    if not processed_files:
        return False, "unsupported"

    request_id = uuid.uuid4().hex[:10]
    file_store[request_id] = {"files": processed_files}

    await send_review(
        user_id=message.author.id,
        guild_id=message.guild.id,
        room_name=room_name,
        extra_text=extra_text,
        raw_files=raw_files,
        request_id=request_id,
        target_channel_id=target_channel_id,
    )
    return True, request_id


# ── البوت ────────────────────────────────────────────────────────────────────
def make_bot():
    b = commands.Bot(command_prefix="!", intents=intents)

    @b.event
    async def on_ready():
        print(f"البوت شغال: {b.user.name}")
        print(f"ID: {b.user.id}")
        patch_parsers(b._connection)

    @b.event
    async def on_message(message):
        if message.author.bot:
            return

        content = message.content.strip()
        user_id = message.author.id
        print(
            f"[MSG] id={message.id} ch={message.channel.id} "
            f"from={message.author} atts={len(message.attachments)} "
            f"ref={bool(message.reference)}"
        )

        # ── أمر -روم (إضافة لروم موجود) ─────────────────────────────────
        if content.startswith("-روم"):
            lines = content.splitlines()
            first_line = lines[0].replace("-روم", "", 1).strip()
            extra_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

            if not first_line:
                try:
                    await message.reply(
                        "اكتب اسم الروم بعد `-روم`\nمثال:\n`-روم اسم-الروم`\nالكلام اللي تبيه",
                        delete_after=10,
                    )
                except Exception:
                    pass
                return

            target_channel = find_channel_by_name(message.guild, first_line)
            if not target_channel:
                try:
                    await message.reply(
                        f"ما لقيت روم باسم **{first_line}**، تأكد من الاسم.",
                        delete_after=10,
                    )
                except Exception:
                    pass
                return

            all_attachments = await fetch_all_attachments(message)
            if not all_attachments:
                try:
                    await message.reply(
                        "لازم ترفق صورة أو مقطع أو فويس مع الأمر.", delete_after=10
                    )
                except Exception:
                    pass
                return

            try:
                raw_files, processed_files = await process_attachments(all_attachments)
                if not processed_files:
                    try:
                        await message.reply(
                            "الملفات غير مدعومة. أرسل صور أو مقاطع فيديو أو فويس.",
                            delete_after=10,
                        )
                    except Exception:
                        pass
                    return

                request_id = uuid.uuid4().hex[:10]
                file_store[request_id] = {"files": processed_files}

                try:
                    await message.delete()
                except Exception:
                    pass

                await send_review(
                    user_id=user_id,
                    guild_id=message.guild.id,
                    room_name=target_channel.name,
                    extra_text=extra_text,
                    raw_files=raw_files,
                    request_id=request_id,
                    target_channel_id=target_channel.id,
                )

                try:
                    await message.author.send("استلمنا")
                except Exception:
                    pass

            except Exception as e:
                print(f"خطأ في أمر -روم: {e}")

            return

        # ── قناة الطلبات (روم جديد) ─────────────────────────────────────
        if message.channel.id == REQUEST_CHANNEL_ID:
            # حماية المعالجة المزدوجة
            async with processing_lock:
                if message.id in processed_messages:
                    return
                processed_messages.add(message.id)

            lines = content.splitlines()
            room_name = lines[0].strip() if lines else "غير محدد"
            extra_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

            all_attachments = await fetch_all_attachments(message)
            if not all_attachments:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            raw_files, processed_files = await process_attachments(all_attachments)
            if not processed_files:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            print(
                f"طلب جديد من {message.author} - الروم: {room_name} - {len(processed_files)} ملف"
            )

            request_id = uuid.uuid4().hex[:10]
            file_store[request_id] = {"files": processed_files}

            await send_review(
                user_id=user_id,
                guild_id=message.guild.id,
                room_name=room_name,
                extra_text=extra_text,
                raw_files=raw_files,
                request_id=request_id,
                target_channel_id=None,
            )

            try:
                log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
                embed = discord.Embed(
                    title="طلب جديد",
                    color=discord.Color.blue(),
                    timestamp=datetime.utcnow(),
                )
                embed.add_field(name="الروم", value=room_name, inline=True)
                embed.add_field(
                    name="عدد الملفات", value=str(len(processed_files)), inline=True
                )
                embed.add_field(name="الحالة", value="قيد المراجعة", inline=True)
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"فشل اللوق: {e}")

            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.author.send("استلمنا")
            except Exception:
                pass

            return

        await b.process_commands(message)

    @b.event
    async def on_message_delete(message):
        processed_messages.discard(message.id)

    # ── أمر !pending ─────────────────────────────────────────────────────
    @b.command(name="pending")
    @commands.has_permissions(administrator=True)
    async def pending_cmd(ctx):
        if not file_store:
            await ctx.send("لا توجد طلبات معلقة حالياً.")
            return
        embed = discord.Embed(
            title="الطلبات المعلقة",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow(),
        )
        for rid, data in file_store.items():
            count = len(data.get("files", []))
            status = (
                "✅ مقبول (جارٍ الإرسال)"
                if rid in accepting_requests
                else "⏳ قيد المراجعة"
            )
            embed.add_field(
                name=f"ID: {rid}", value=f"{count} ملف — {status}", inline=False
            )
        await ctx.send(embed=embed)

    # ── أمر !مسح ─────────────────────────────────────────────────────────
    @b.command(name="مسح")
    @commands.has_permissions(manage_messages=True)
    async def purge_cmd(ctx, amount: int = None):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        if ctx.message.reference:
            try:
                target = await ctx.channel.fetch_message(
                    ctx.message.reference.message_id
                )
                await target.delete()
                await ctx.send("تم الحذف.", delete_after=3)
            except Exception as e:
                print(f"فشل مسح الرسالة: {e}")
            return

        n = amount if amount else 10
        deleted = await ctx.channel.purge(limit=n)
        await ctx.send(f"تم حذف {len(deleted)} رسالة.", delete_after=4)

    return b


bot = make_bot()

if not TOKEN:
    print("خطأ: DISCORD_TOKEN غير موجود في المتغيرات البيئية!")
    exit(1)

while True:
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("تم إيقاف البوت.")
        break
    except Exception as e:
        print(f"البوت توقف بسبب: {e}")
        print("إعادة التشغيل بعد 5 ثواني...")
        time.sleep(5)
        bot = make_bot()
