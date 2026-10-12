import os
import re
import json
import random
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

============================================================

TikTok Username Finder - Discord Bot

main.py | Python 3.12+ | discord.py 2.7+

============================================================

TOKEN = os.getenv(”DISCORD_TOKEN”)
RESULT_CHANNEL_ID = int(os.getenv(“RESULT_CHANNEL_ID”, “0”))

DATA_DIR = Path(“data”)
DATA_DIR.mkdir(parents=True, exist_ok=True)

RESULTS_FILE = DATA_DIR / “results.json”

CHECK_DELAY = 1.0
MAX_PER_RUN = 100
REQUEST_TIMEOUT = 15

ALPHABET = “abcdefghijklmnopqrstuvwxyz0123456789_”
USER_AGENT = (
“Mozilla/5.0 (Windows NT 10.0; Win64; x64) “
“AppleWebKit/537.36 (KHTML, like Gecko) “
“Chrome/130.0.0.0 Safari/537.36”
)

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s | %(levelname)s | %(message)s”,
)

log = logging.getLogger(“tiktok-finder”)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=”!”, intents=intents)

running = False
stop_requested = False

def load_json(path, default):
try:
if path.exists():
return json.loads(path.read_text(encoding=“utf-8”))
except (OSError, json.JSONDecodeError):
log.exception(“تعذرت قراءة %s”, path)

return default

def save_json(path, data):
temporary = path.with_suffix(”.tmp”)
temporary.write_text(
json.dumps(data, ensure_ascii=False, indent=2),
encoding=“utf-8”,
)
temporary.replace(path)

def load_results():
data = load_json(RESULTS_FILE, [])
return data if isinstance(data, list) else []

def valid_username(username):
return bool(re.fullmatch(r”[A-Za-z0-9._]{3,4}”, username))

def generate_usernames(limit=MAX_PER_RUN):
candidates = set()

while len(candidates) < limit * 3:
    length = random.choice((3, 4))
    if random.random() < 0.7:
        username = "d" + "".join(
            random.choice(ALPHABET)
            for _ in range(length - 1)
        )
    else:
        username = "".join(
            random.choice(ALPHABET)
            for _ in range(length)
        )
    if valid_username(username):
        candidates.add(username)
preferred = [
    name for name in candidates if name.startswith("d")
]
others = [
    name for name in candidates if not name.startswith("d")
]
random.shuffle(preferred)
random.shuffle(others)
return (preferred + others)[:limit]

async def check_public_profile(session, username):
“””
يفحص الصفحة العامة فقط.
لا يعتبر غياب بيانات الحساب دليلاً على توفر الاسم.
“””
url = f”https://www.tiktok.com/@{username}”

try:
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        allow_redirects=True,
    ) as response:
        if response.status in (403, 429, 503):
            return "❔", "تعذر التأكد بسبب قيود الوصول"
        if response.status == 404:
            return "❔", "استجابة الصفحة لا تثبت توفر الاسم"
        if response.status != 200:
            return "❔", f"HTTP {response.status}"
        html = await response.text(errors="ignore")
        patterns = (
            r'"uniqueId"\s*:\s*"' + re.escape(username) + r'"',
            r'"unique_id"\s*:\s*"' + re.escape(username) + r'"',
        )
        if any(
            re.search(pattern, html, re.IGNORECASE)
            for pattern in patterns
        ):
            return "❌", "ظهرت بيانات الحساب في الصفحة"
        return "❔", "لم يتوفر دليل موثوق على توفر الاسم"
except asyncio.TimeoutError:
    return "❔", "انتهت مهلة الاتصال"
except aiohttp.ClientError as exc:
    log.warning("خطأ اتصال أثناء فحص %s: %s", username, exc)
    return "❔", "تعذر الاتصال"
except Exception:
    log.exception("خطأ أثناء فحص %s", username)
    return "❔", "تعذر التأكد"

async def send_result(channel, username, status):
await channel.send(
f”{username} {status}”,
allowed_mentions=discord.AllowedMentions.none(),
)

@bot.event
async def on_ready():
log.info(“تم تسجيل الدخول: %s (%s)”, bot.user, bot.user.id)

try:
    synced = await bot.tree.sync()
    log.info("تمت مزامنة %s أمر", len(synced))
except Exception:
    log.exception("فشلت مزامنة أوامر Discord")

@bot.tree.command(
name=“فحص_يوزرات”,
description=“فحص أسماء تيك توك ثلاثية ورباعية”,
)
async def start_scan(interaction: discord.Interaction):
global running, stop_requested

if running:
    await interaction.response.send_message(
        "يوجد فحص يعمل حالياً.",
        ephemeral=True,
    )
    return
if not RESULT_CHANNEL_ID:
    await interaction.response.send_message(
        "أضف RESULT_CHANNEL_ID في متغيرات البيئة.",
        ephemeral=True,
    )
    return
channel = bot.get_channel(RESULT_CHANNEL_ID)
if channel is None:
    try:
        channel = await bot.fetch_channel(RESULT_CHANNEL_ID)
    except discord.HTTPException:
        await interaction.response.send_message(
            "تعذر الوصول إلى روم النتائج. تحقق من المعرّف والصلاحيات.",
            ephemeral=True,
        )
        return
if not hasattr(channel, "send"):
    await interaction.response.send_message(
        "المعرّف لا يشير إلى روم يمكن إرسال الرسائل فيه.",
        ephemeral=True,
    )
    return
await interaction.response.send_message(
    f"بدأ الفحص. النتائج في {channel.mention}.",
    ephemeral=True,
)
running = True
stop_requested = False
checked = 0
try:
    results = load_results()
    known = {
        str(item.get("username", "")).lower()
        for item in results
        if isinstance(item, dict)
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:
        names = generate_usernames(MAX_PER_RUN)
        for username in names:
            if stop_requested:
                break
            if username.lower() in known:
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
            known.add(username.lower())
            checked += 1
            await send_result(channel, username, status)
            # لا نستخدم علامة التوفر دون تأكيد موثوق.
            # لذلك يتوقف الفحص عند أي نتيجة مؤكدة فقط،
            # ولا يصنف الأسماء غير المؤكدة على أنها متاحة.
            await asyncio.sleep(CHECK_DELAY)
    if stop_requested:
        summary = f"تم إيقاف الفحص بعد فحص {checked} اسم."
    else:
        summary = (
            f"انتهى الفحص. عدد الأسماء التي فُحصت: {checked}.\n"
            "لم يتم تأكيد توفر أي اسم؛ علامة ❔ تعني أن النتيجة غير محسومة."
        )
    await interaction.followup.send(summary, ephemeral=True)
except Exception:
    log.exception("حدث خطأ أثناء الفحص")
    try:
        await interaction.followup.send(
            "توقف الفحص بسبب خطأ. راجع سجلات الاستضافة.",
            ephemeral=True,
        )
    except discord.HTTPException:
        pass
finally:
    running = False
    stop_requested = False

@bot.tree.command(
name=“ايقاف_الفحص”,
description=“إيقاف الفحص الجاري”,
)
async def stop_scan(interaction: discord.Interaction):
global stop_requested

if (
    not interaction.guild
    or not interaction.user.guild_permissions.manage_guild
):
    await interaction.response.send_message(
        "تحتاج صلاحية Manage Server لاستخدام هذا الأمر.",
        ephemeral=True,
    )
    return
if not running:
    await interaction.response.send_message(
        "لا يوجد فحص يعمل حالياً.",
        ephemeral=True,
    )
    return
stop_requested = True
await interaction.response.send_message(
    "سيتم إيقاف الفحص بعد انتهاء الطلب الحالي.",
    ephemeral=True,
)

@bot.tree.command(
name=“نتائج_الفحص”,
description=“عرض ملخص النتائج المحفوظة”,
)
async def show_results(interaction: discord.Interaction):
results = load_results()

total = len(results)
taken = sum(
    1 for item in results
    if isinstance(item, dict) and item.get("status") == "❌"
)
available = sum(
    1 for item in results
    if isinstance(item, dict) and item.get("status") == "✅"
)
uncertain = sum(
    1 for item in results
    if isinstance(item, dict) and item.get("status") == "❔"
)
await interaction.response.send_message(
    f"**إجمالي الأسماء:** {total}\n"
    f"**حسابات ظهرت بياناتها:** {taken}\n"
    f"**أسماء متاحة مؤكدة:** {available}\n"
    f"**نتائج غير مؤكدة:** {uncertain}",
    ephemeral=True,
)

if not TOKEN:
raise RuntimeError(
“أضف DISCORD_TOKEN في متغيرات البيئة.”
)

bot.run(TOKEN)
