import discord
from discord.ext import commands
import os
import io
import aiohttp
import asyncio
import tempfile
import uuid
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.environ.get("DISCORD_TOKEN")

REQUEST_CHANNEL_ID = 1520402264441884672
LOG_CHANNEL_ID     = 1520405217210929162
CATEGORY_ID        = 1508218078058774555
ADMIN_USER_ID      = 1483889802876158014
REVIEW_CHANNEL_ID  = 1520402893126111473

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

processed_messages = set()
# { request_id: { "bytes": ..., "filename": ..., "is_video": bool } }
file_store = {}

IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp']
VIDEO_EXTS = ['.mp4', '.mov', '.mkv', '.avi', '.webm']


async def download_file(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()


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

        # 3 مواقع قطرية: يسار فوق — وسط — يمين تحت
        positions = [
            (m,                                        img.height // 5  - text_h // 2),
            (img.width // 2 - text_w // 2,             img.height // 2  - text_h // 2),
            (img.width - text_w - m,                   img.height * 4 // 5 - text_h // 2),
        ]

        for (x, y) in positions:
            draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 150))
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 200))

        output = io.BytesIO()
        img = img.convert("RGB")
        img.save(output, format="JPEG", quality=95)
        return output.getvalue()
    except Exception as e:
        print(f"⚠️ فشل الواترمارك: {e}")
        return img_bytes


async def add_video_watermark(video_bytes: bytes, ext: str) -> bytes:
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if not os.path.exists(font_path):
            font_path = ""

        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f_in:
            f_in.write(video_bytes)
            in_path = f_in.name

        out_path = in_path.replace(f".{ext}", f"_wm.mp4")

        text = "© Depth Of School"
        fontfile_arg = f":fontfile={font_path}" if font_path else ""

        # 3 مواقع قطرية: يسار فوق — وسط — يمين تحت
        base = "fontsize=w/22:fontcolor=white@0.85:shadowcolor=black@0.6:shadowx=2:shadowy=2"
        drawtext = (
            f"drawtext=text='{text}'{fontfile_arg}:{base}:x=20:y=h/5-text_h/2,"
            f"drawtext=text='{text}'{fontfile_arg}:{base}:x=(w-text_w)/2:y=(h-text_h)/2,"
            f"drawtext=text='{text}'{fontfile_arg}:{base}:x=w-text_w-20:y=h*4/5-text_h/2"
        )

        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-vf", drawtext,
            "-c:a", "copy",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            out_path
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            print(f"⚠️ ffmpeg error: {stderr.decode()[-500:]}")
            os.unlink(in_path)
            return video_bytes

        with open(out_path, "rb") as f:
            result = f.read()

        os.unlink(in_path)
        os.unlink(out_path)
        print(f"✅ واترمارك الفيديو جاهز ({len(result)} bytes)")
        return result

    except Exception as e:
        print(f"⚠️ فشل واترمارك الفيديو: {e}")
        return video_bytes


def find_channel_by_name(guild: discord.Guild, name: str):
    name_clean = name.strip().replace(" ", "-").lower()
    for ch in guild.text_channels:
        if ch.name.lower() == name_clean or ch.name.lower() == name.strip().lower():
            return ch
    return None


async def send_review(user_id: int, guild_id: int, room_name: str, extra_text: str,
                      raw_bytes: bytes, orig_filename: str, request_id: str,
                      is_video: bool = False, target_channel_id=None):
    try:
        review_channel = await bot.fetch_channel(REVIEW_CHANNEL_ID)

        if target_channel_id:
            header = f"📋 **طلب إضافة لروم موجود**\n**الروم:** <#{target_channel_id}>"
        else:
            header = f"📋 **طلب روم جديد**\n**اسم الروم:** {room_name}"

        if extra_text:
            header += f"\n**الكلام:** {extra_text}"
        header += f"\n**من:** <@{user_id}>"

        view = ReviewView(
            user_id=user_id,
            room_name=room_name,
            guild_id=guild_id,
            extra_text=extra_text,
            target_channel_id=target_channel_id,
            request_id=request_id
        )

        preview_file = discord.File(io.BytesIO(raw_bytes), filename=orig_filename)
        await review_channel.send(content=header, file=preview_file, view=view)

        print(f"✅ تم إرسال الطلب لقناة المراجعة [{request_id}]")
    except Exception as e:
        print(f"❌ خطأ في إرسال للقناة: {e}")


class ReviewView(discord.ui.View):
    def __init__(self, user_id, room_name, guild_id, request_id,
                 extra_text="", target_channel_id=None):
        super().__init__(timeout=86400)
        self.user_id = user_id
        self.room_name = room_name
        self.guild_id = guild_id
        self.extra_text = extra_text
        self.target_channel_id = target_channel_id
        self.request_id = request_id

        accept_btn = discord.ui.Button(
            label="✅ قبول",
            style=discord.ButtonStyle.success,
            custom_id=f"accept_{request_id}"
        )
        reject_btn = discord.ui.Button(
            label="❌ رفض",
            style=discord.ButtonStyle.danger,
            custom_id=f"reject_{request_id}"
        )
        accept_btn.callback = self.accept_callback
        reject_btn.callback = self.reject_callback
        self.add_item(accept_btn)
        self.add_item(reject_btn)

    async def accept_callback(self, interaction: discord.Interaction):
        print(f"🔘 قبول [{self.request_id}] user={self.user_id}")
        await interaction.response.defer()

        guild = bot.get_guild(self.guild_id)
        dest_channel = None

        if self.target_channel_id:
            try:
                dest_channel = await bot.fetch_channel(self.target_channel_id)
            except Exception as e:
                print(f"❌ فشل جلب القناة: {e}")
        else:
            if guild:
                try:
                    channel_name = self.room_name.replace(" ", "-") if self.room_name else "روم-جديد"
                    category = guild.get_channel(CATEGORY_ID)
                    private_role = guild.get_role(1515958292969689098)
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True,
                            embed_links=True, attach_files=True, read_message_history=True
                        )
                    }
                    if private_role:
                        overwrites[private_role] = discord.PermissionOverwrite(
                            view_channel=True, send_messages=False, read_message_history=True
                        )
                    dest_channel = await guild.create_text_channel(
                        name=channel_name, category=category,
                        overwrites=overwrites, reason="طلب مقبول"
                    )
                    print(f"✅ تم إنشاء القناة: {dest_channel.name}")
                except Exception as e:
                    print(f"❌ فشل إنشاء القناة: {e}")

        if dest_channel:
            try:
                stored = file_store.get(self.request_id)
                if stored:
                    fb = stored["bytes"]
                    fname = stored["filename"]
                    print(f"📤 إرسال الملف ({len(fb)} bytes) → {dest_channel.name}")
                    file = discord.File(io.BytesIO(fb), filename=fname)
                    content = self.extra_text if self.extra_text else None
                    await dest_channel.send(content=content, file=file)
                    del file_store[self.request_id]
                else:
                    print(f"⚠️ الملف غير موجود في المخزن [{self.request_id}]")
            except Exception as e:
                print(f"❌ فشل إرسال الملف: {e}")

        user = bot.get_user(self.user_id)
        if user:
            try:
                msg = "✅ **تم قبول طلبك!**"
                if dest_channel:
                    msg += f"\n\n📌 الروم: {dest_channel.mention}"
                await user.send(msg)
            except Exception:
                pass

        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
            embed = discord.Embed(title="✅ طلب مقبول", color=discord.Color.green(), timestamp=datetime.utcnow())
            embed.add_field(name="العضو", value=f"<@{self.user_id}>", inline=True)
            embed.add_field(name="الروم", value=self.room_name, inline=True)
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"❌ فشل اللوق: {e}")

        try:
            await interaction.message.delete()
            print(f"🗑️ تم حذف رسالة الطلب [{self.request_id}] من قناة الإدارة")
        except Exception as e:
            print(f"⚠️ فشل حذف رسالة الطلب: {e}")

    async def reject_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            RejectModal(self.user_id, self.room_name, self.request_id, interaction.message)
        )


class RejectModal(discord.ui.Modal, title="سبب الرفض"):
    reason = discord.ui.TextInput(
        label="أدخل سبب الرفض",
        placeholder="مثال: الصورة غير واضحة...",
        required=True, max_length=500
    )

    def __init__(self, user_id, room_name, request_id, review_message):
        super().__init__()
        self.user_id = user_id
        self.room_name = room_name
        self.request_id = request_id
        self.review_message = review_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        user = bot.get_user(self.user_id)
        if user:
            try:
                await user.send(f"❌ **تم رفض طلبك**\n\n**السبب:** {self.reason.value}")
            except Exception:
                pass

        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
            embed = discord.Embed(title="❌ طلب مرفوض", color=discord.Color.red(), timestamp=datetime.utcnow())
            embed.add_field(name="العضو", value=f"<@{self.user_id}>", inline=True)
            embed.add_field(name="الروم", value=self.room_name, inline=True)
            embed.add_field(name="السبب", value=self.reason.value, inline=False)
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"❌ فشل اللوق: {e}")

        try:
            await self.review_message.delete()
            print(f"🗑️ تم حذف رسالة الطلب [{self.request_id}] من قناة الإدارة")
        except Exception as e:
            print(f"⚠️ فشل حذف رسالة الطلب: {e}")

        file_store.pop(self.request_id, None)


@bot.event
async def on_ready():
    print(f"✅ البوت شغال: {bot.user.name}")
    print(f"ID: {bot.user.id}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.strip()
    user_id = message.author.id

    # ── أمر -روم ──────────────────────────────────────────────────────────────
    if content.startswith("-روم"):
        lines = content.splitlines()
        first_line = lines[0].replace("-روم", "", 1).strip()
        extra_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

        if not first_line:
            try:
                await message.reply("⚠️ اكتب اسم الروم بعد `-روم`\nمثال:\n`-روم اسم-الروم`\nالكلام اللي تبيه", delete_after=10)
            except Exception:
                pass
            return

        target_channel = find_channel_by_name(message.guild, first_line)
        if not target_channel:
            try:
                await message.reply(f"⚠️ ما لقيت روم باسم **{first_line}**، تأكد من الاسم.", delete_after=10)
            except Exception:
                pass
            return

        if not message.attachments:
            try:
                await message.reply("⚠️ لازم ترفق صورة أو مقطع مع الأمر.", delete_after=10)
            except Exception:
                pass
            return

        attachment = message.attachments[0]
        fname = attachment.filename.lower()
        is_image = any(fname.endswith(ext) for ext in IMAGE_EXTS)
        is_video = any(fname.endswith(ext) for ext in VIDEO_EXTS)

        if not is_image and not is_video:
            try:
                await message.reply("⚠️ الملف غير مدعوم. أرسل صورة أو مقطع فيديو.", delete_after=10)
            except Exception:
                pass
            return

        try:
            print(f"⬇️ تحميل الملف (-روم)...")
            raw_bytes = await download_file(attachment.url)
            orig_ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else "jpg"
            orig_filename = f"preview.{orig_ext}"

            if is_image:
                processed = add_watermark(raw_bytes)
                stored_filename = "image.jpg"
            else:
                print("🎬 إضافة واترمارك على الفيديو...")
                processed = await add_video_watermark(raw_bytes, orig_ext)
                stored_filename = "video.mp4"

            request_id = uuid.uuid4().hex[:10]
            file_store[request_id] = {"bytes": processed, "filename": stored_filename, "is_video": is_video}

            try:
                await message.delete()
            except Exception:
                pass

            await send_review(
                user_id=user_id,
                guild_id=message.guild.id,
                room_name=target_channel.name,
                extra_text=extra_text,
                raw_bytes=raw_bytes,
                orig_filename=orig_filename,
                request_id=request_id,
                is_video=is_video,
                target_channel_id=target_channel.id
            )

            try:
                await message.author.send("✅ استلمنا")
            except Exception:
                pass

        except Exception as e:
            print(f"❌ خطأ في أمر -روم: {e}")

        return

    # ── قناة الطلبات (روم جديد) ──────────────────────────────────────────────
    if message.channel.id == REQUEST_CHANNEL_ID:
        if message.id in processed_messages:
            return
        processed_messages.add(message.id)

        lines = content.splitlines()
        room_name = lines[0].strip() if lines else "غير محدد"
        extra_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

        if not message.attachments:
            try:
                await message.delete()
            except Exception:
                pass
            return

        has_valid = False
        for attachment in message.attachments:
            fn = attachment.filename.lower()
            is_image = any(fn.endswith(ext) for ext in IMAGE_EXTS)
            is_video = any(fn.endswith(ext) for ext in VIDEO_EXTS)

            if not is_image and not is_video:
                continue

            has_valid = True
            print(f"{'📸' if is_image else '🎬'} طلب جديد من {message.author} - الروم: {room_name}")

            try:
                print(f"⬇️ تحميل الملف...")
                raw_bytes = await download_file(attachment.url)
                orig_ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else "jpg"
                orig_filename = f"preview.{orig_ext}"

                if is_image:
                    processed = add_watermark(raw_bytes)
                    stored_filename = "image.jpg"
                else:
                    print("🎬 إضافة واترمارك على الفيديو...")
                    processed = await add_video_watermark(raw_bytes, orig_ext)
                    stored_filename = "video.mp4"

                request_id = uuid.uuid4().hex[:10]
                file_store[request_id] = {"bytes": processed, "filename": stored_filename, "is_video": is_video}
                print(f"✅ الملف جاهز ({len(processed)} bytes)")
            except Exception as e:
                print(f"⚠️ فشل تحميل/واترمارك: {e}")
                continue

            await send_review(
                user_id=user_id,
                guild_id=message.guild.id,
                room_name=room_name,
                extra_text=extra_text,
                raw_bytes=raw_bytes,
                orig_filename=orig_filename,
                request_id=request_id,
                is_video=is_video,
                target_channel_id=None
            )

            try:
                log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
                embed = discord.Embed(title="📥 طلب جديد", color=discord.Color.blue(), timestamp=datetime.utcnow())
                embed.add_field(name="الروم", value=room_name, inline=True)
                embed.add_field(name="النوع", value="🎬 فيديو" if is_video else "📸 صورة", inline=True)
                embed.add_field(name="الحالة", value="⏳ قيد المراجعة", inline=True)
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"⚠️ فشل اللوق: {e}")

        if has_valid:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.author.send("✅ استلمنا")
            except Exception:
                pass
        else:
            try:
                await message.delete()
            except Exception:
                pass

    await bot.process_commands(message)


@bot.command(name="pending")
@commands.has_permissions(administrator=True)
async def pending_cmd(ctx):
    if not file_store:
        await ctx.send("✅ لا توجد طلبات معلقة حالياً.")
        return
    embed = discord.Embed(title="⏳ الطلبات المعلقة", color=discord.Color.orange(), timestamp=datetime.utcnow())
    for rid in file_store:
        embed.add_field(name=f"ID: {rid}", value="قيد المراجعة", inline=False)
    await ctx.send(embed=embed)


@bot.event
async def on_message_delete(message):
    processed_messages.discard(message.id)


@bot.command(name="مسح")
@commands.has_permissions(manage_messages=True)
async def purge_cmd(ctx, amount: int = None):
    try:
        await ctx.message.delete()
    except Exception:
        pass

    if ctx.message.reference:
        try:
            target = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            await target.delete()
            await ctx.send("🗑️ تم الحذف.", delete_after=3)
        except Exception as e:
            print(f"⚠️ فشل مسح الرسالة: {e}")
        return

    n = amount if amount else 10
    deleted = await ctx.channel.purge(limit=n)
    await ctx.send(f"🗑️ تم حذف {len(deleted)} رسالة.", delete_after=4)


bot.run(TOKEN)
