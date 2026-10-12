
import os
import re
import json
import time
import random
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord.ext import commands

# ============================================================
# TikTok Username Finder - Discord Bot
# Single file: main.py
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
RESULT_CHANNEL_ID = int(os.getenv("RESULT_CHANNEL_ID", "0"))

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

RESULTS_FILE = DATA_DIR / "results.json"
STATE_FILE = DATA_DIR / "state.json"

# فحص محدود لتفادي الضغط على الموقع
CHECK_DELAY = 3.0
MAX_PER_RUN = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tiktok-finder")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

running = False
stop_requested = False


def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("تعذر قراءة %s", path)
    return default


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_results():
    data = load_json(RESULTS_FILE, [])
    return data if isinstance(data, list) else []


def valid_username(name):
    # قواعد شكل الاسم فقط، ولا تثبت توفره
    return bool(re.fullmatch(r"[A-Za-z0-9._]{3,4}", name))


def generate_usernames(limit=MAX_PER_RUN):
    """توليد أسماء ثلاثية ورباعية، مع تفضيل الحرف d."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_"
    candidates = set()

    # الأسماء التي تبدأ بـ d لها أولوية
    for length in (3, 4):
        for _ in range(limit * 8):
            first = "d" if random.random() < 0.7 else random.choice(alphabet)
            name = first + "".join(
                random.choice(alphabet) for _ in range(length - 1)
            )
            if valid_username(name):
                candidates.add(name)

    preferred = sorted(
        (n for n in candidates if n.startswith("d")),
        key=lambda _: random.random(),
    )
    others = sorted(
        (n for n in candidates if not n.startswith("d")),
        key=lambda _: random.random(),
    )
    return (preferred + others)[:limit]


async def check_public_profile(session, username):
    """
    فحص صفحة عامة فقط.
    لا نتجاوز CAPTCHA أو قيود الوصول.
    نتيجة الصفحة ليست ضماناً لإمكانية حجز الاسم.
    """
    url = f"https://www.tiktok.com/@{username}"
    try:
        async with session.get(
            url,
            timeout=15,
            allow_redirects=True,
        ) as response:
            if response.status in (403, 429, 503):
                return "غير مؤكد", "تيك توك قيّد الطلب أو رفضه"

            if response.status != 200:
                return "غير مؤكد", f"HTTP {response.status}"

            html = await response.text()

            # لا نعتبر غياب الاسم من HTML وحده دليلاً على توفره
            if (
                '"userInfo":{"user":' in html
                or '"uniqueId":"' + username + '"' in html
            ):
                return "موجود غالباً", "ظهرت بيانات حساب في الصفحة"

            return "غير مؤكد", "لم يمكن إثبات توفر الاسم من الصفحة"

    except asyncio.TimeoutError:
        return "غير مؤكد", "انتهت مهلة الاتصال"
    except Exception as exc:
        log.warning("فشل فحص %s: %s", username, exc)
        return "غير مؤكد", "تعذر الاتصال"


async def send_result(channel, username, status, details):
    color = (
        discord.Color.green()
        if status == "موجود غالباً"
        else discord.Color.orange()
    )

    embed = discord.Embed(
        title=f"فحص اليوزر: @{username}",
        url=f"https://www.tiktok.com/@{username}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="النتيجة", value=status, inline=True)
    embed.add_field(name="التفاصيل", value=details, inline=False)
    embed.set_footer(
        text="الفحص لا يؤكد إمكانية حجز الاسم؛ تحقق من تطبيق TikTok."
    )

    await channel.send(embed=embed)


@bot.event
async def on_ready():
    log.info("تم تسجيل الدخول: %s (%s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        log.info("تمت مزامنة %s أمر", len(synced))
    except Exception:
        log.exception("فشلت مزامنة أوامر Discord")


@bot.tree.command(
    name="فحص_يوزرات",
    description="ولّد أسماء تيك توك ثلاثية ورباعية وافحص صفحاتها العامة",
)
async def start_scan(interaction: discord.Interaction):
    global running, stop_requested

    if running:
        await interaction.response.send_message(
            "يوجد فحص يعمل حالياً.", ephemeral=True
        )
        return

    if not RESULT_CHANNEL_ID:
        await interaction.response.send_message(
            "أضف RESULT_CHANNEL_ID في متغيرات Render أولاً.",
            ephemeral=True,
        )
        return

    channel = bot.get_channel(RESULT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(RESULT_CHANNEL_ID)
        except discord.HTTPException:
            await interaction.response.send_message(
                "لم أستطع الوصول إلى روم النتائج. تأكد من المعرّف والصلاحيات.",
                ephemeral=True,
            )
            return

    await interaction.response.send_message(
        f"بدأ الفحص المحدود. ستُنشر النتائج في {channel.mention}.",
        ephemeral=True,
    )

    running = True
    stop_requested = False

    try:
        import aiohttp

        results = load_results()
        known = {item.get("username") for item in results}

        timeout = aiohttp.ClientTimeout(total=20)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; PublicProfileCheck/1.0)"
        }

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as session:
            names = generate_usernames(MAX_PER_RUN)
            checked = 0

            for username in names:
                if stop_requested:
                    break

                if username in known:
                    continue

                status, details = await check_public_profile(
                    session, username
                )

                record = {
                    "username": username,
                    "status": status,
                    "details": details,
                    "checked_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }

                results.append(record)
                save_json(RESULTS_FILE, results)
                checked += 1

                await send_result(
                    channel, username, status, details
                )

                await asyncio.sleep(CHECK_DELAY)

        await interaction.followup.send(
            f"انتهى الفحص. عدد الأسماء التي فُحصت: {checked}.",
            ephemeral=True,
        )

    except Exception:
        log.exception("حدث خطأ أثناء الفحص")
        await interaction.followup.send(
            "توقف الفحص بسبب خطأ. راجع سجلات Render.",
            ephemeral=True,
        )
    finally:
        running = False


@bot.tree.command(
    name="ايقاف_الفحص",
    description="إيقاف الفحص الجاري",
)
async def stop_scan(interaction: discord.Interaction):
    global stop_requested

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "تحتاج صلاحية Manage Server لاستخدام هذا الأمر.",
            ephemeral=True,
        )
        return

    if not running:
        await interaction.response.send_message(
            "لا يوجد فحص يعمل حالياً.", ephemeral=True
        )
        return

    stop_requested = True
    await interaction.response.send_message(
        "سيتم إيقاف الفحص بعد انتهاء الطلب الحالي.",
        ephemeral=True,
    )


@bot.tree.command(
    name="نتائج_الفحص",
    description="عرض ملخص النتائج المحفوظة",
)
async def show_results(interaction: discord.Interaction):
    results = load_results()
    total = len(results)
    existing = sum(
        1 for item in results
        if item.get("status") == "موجود غالباً"
    )
    uncertain = sum(
        1 for item in results
        if item.get("status") == "غير مؤكد"
    )

    await interaction.response.send_message(
        f"**إجمالي النتائج:** {total}\n"
        f"**حسابات ظهرت بياناتها:** {existing}\n"
        f"**نتائج غير مؤكدة:** {uncertain}\n"
        f"**ملف الحفظ:** `{RESULTS_FILE}`",
        ephemeral=True,
    )


if not TOKEN:
    raise RuntimeError(
        "أضف DISCORD_TOKEN في Environment Variables على Render."
    )

bot.run(TOKEN)
