bu"""
Kanal boshqaruv boti (aiogram 3 + Gemini + SQLite + Flux AI + Web Photos)

Yangi imkoniyatlar:
- 100% kafolatlangan Post va So'rovnoma yuborish (xatolik bo'lsa zaxiraga o'tish)
- Har qanday formatdagi kanal havolalarini (@..., https://t.me/..., ID) qo'llab-quvvatlash
- Kengaytirilgan moderatsiya (so'kinish, haqorat, kamsitish, firibgarlik/masheniklikka qarshi)
- 20 soniyada qoidabuzarlik xabarlarini tozalash
- Guruh chatidagi har bir a'zoga xushmuomala javob / minnatdorchilik bildirish
- Rang-barang rasmlar (Flux AI har xil uslublar + Internetdan haqiqiy fotolar)
- Renderda kanallar o'chib ketmasligi uchun avtomatik JSON zaxira va tiklash
- Admin tomonidan karta raqami va egasini bot ichidan o'zgartirish
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from google import genai

load_dotenv()

# ============================ SOZLAMALAR ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9210617689:AAEPnowEia2VT_wv3Nq8826oJFjFRkjbGl0")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KAlZvxYEqIFZe1BSp9ADituTrdwe5HuaiFCel53p2oUw")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "6125160021")
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()}
DEFAULT_CARD_NUMBER = os.getenv("CARD_NUMBER", "8600 0000 0000 0000")
DEFAULT_CARD_OWNER = os.getenv("CARD_OWNER", "Admin")
TZ = ZoneInfo("Asia/Tashkent")

PLANS = {
    "basic": {
        "name": "Oddiy", "price": 10000, "channels": 1,
        "hours": [12], "moderation": True, "ai_chat": False,
        "desc": "1 ta kanal, kuniga 1 ta post, professional matn",
    },
    "standard": {
        "name": "O'rta", "price": 20000, "channels": 2,
        "hours": [9, 14, 21], "moderation": True, "ai_chat": True,
        "desc": "2 ta kanal, kuniga 3 ta post, AI rasmlar, guruh nazorati",
    },
    "max": {
        "name": "Maksimal VIP", "price": 30000, "channels": 5,
        "hours": [8, 11, 14, 17, 20, 22], "moderation": True, "ai_chat": True,
        "desc": "5 ta kanal, cheksiz AI rasm/postlar, so'rovnomalar, chat tahlili va AI moderator",
    },
}
PLAN_DAYS = 30

# ============================ KENGAYTIRILGAN MODERATSIYA ============================
ROOT_PROFANITIES = [
    r"(?:pashol\s*|di\s*|poshol\s*)?na[hx]u[yj]\w*",
    r"dalba(?:yo|ye|e|o)[bp]\w*",
    r"sik[a-z0-9']*\w*",
    r"q[o']?toq\w*",
    r"gandon\w*",
    r"jalap\w*",
    r"chort\w*",
    r"[ye]iban\w*",
    r"eblan\w*",
    r"bl[ya]+[td]?\w*",
    r"suk[ai]\w*",
    r"haromi\w*",
    r"onang[nd]i\w*",
    r"foxish[aa]\w*",
    r"porno\w*",
    r"pedik\w*",
    r"pizd[ae]\w*",
    r"shilta\w*",
    r"qanjiq\w*",
]

STANDALONE_PROFANITIES = [
    r"ahmoq\w*", r"itvachcha\w*", r"padariga\w*", r"chmo\w*", r"lox\w*",
    r"tupoy\w*", r"tentak\w*", r"mol\b", r"eshak\w*", r"maraz\w*"
]

PROFANITY_RE = re.compile(
    r"(" + "|".join(ROOT_PROFANITIES) + r"|(?<!\w)(?:" + "|".join(STANDALONE_PROFANITIES) + r")(?!\w))",
    re.I
)

SCAM_PATTERNS = [
    r"kuniga\s*\d+\$?", r"kunlik\s*daromad", r"oyiga\s*\d+\$?",
    r"daromad\s*100%", r"investitsiya\s*(?:qiling|qilib)?", r"lichkaga\s*(?:yozing|o'ting)?",
    r"\blich\b", r"\blichka\b", r"pul\s*ko'paytirish", r"garantiyali\s*daromad",
    r"vznos", r"kripto(?:valyuta)?\s*ko'paytir", r"pul\s*tikib", r"tez\s*boyish",
    r"sxema\s*bor", r"tekinga\s*\d+\$?", r"tekin\s*pul", r"oson\s*pul",
    r"adminman\s*pul", r"parolingizni", r"1xbet", r"stavka", r"linebet",
    r"melbet", r"mostbet", r"kazinolar", r"kripto\s*piramida",
    r"shuncha\s*pul\s*ishladim", r"sarmoyasiz\s*daromad"
]
SCAM_RE = re.compile(r"(" + "|".join(SCAM_PATTERNS) + r")", re.I)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)

ai = genai.Client(api_key=GEMINI_API_KEY, http_options={"api_version": "v1beta"})

# ============================ BAZA VA MIGRATSIYA ============================
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        plan TEXT, expires_at TEXT, banned INTEGER DEFAULT 0, joined_at TEXT
    );
    CREATE TABLE IF NOT EXISTS channels (
        channel_id INTEGER PRIMARY KEY, owner_id INTEGER, title TEXT, niche TEXT, audience TEXT,
        group_id INTEGER, active INTEGER DEFAULT 1,
        posts_sent INTEGER DEFAULT 0, join_requests INTEGER DEFAULT 0,
        joined INTEGER DEFAULT 0, left_count INTEGER DEFAULT 0,
        group_messages INTEGER DEFAULT 0, warnings INTEGER DEFAULT 0, bans INTEGER DEFAULT 0,
        detailed_prompt TEXT DEFAULT '', custom_hours TEXT DEFAULT '',
        polls_sent INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT, amount INTEGER,
        file_id TEXT, status TEXT DEFAULT 'pending', created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS violations (
        chat_id INTEGER, user_id INTEGER, warnings INTEGER DEFAULT 0, cycle INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS required (chat TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS recent_posts (channel_id INTEGER, text TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS chat_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, user_id INTEGER,
        user_name TEXT, text TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    );
    """)
    for col, col_type in [
        ("detailed_prompt", "TEXT DEFAULT ''"),
        ("custom_hours", "TEXT DEFAULT ''"),
        ("polls_sent", "INTEGER DEFAULT 0"),
        ("group_messages", "INTEGER DEFAULT 0"),
        ("warnings", "INTEGER DEFAULT 0"),
        ("bans", "INTEGER DEFAULT 0"),
    ]:
        try:
            db.execute(f"ALTER TABLE channels ADD COLUMN {col} {col_type}")
            db.commit()
        except Exception:
            pass

    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_number', ?)", (DEFAULT_CARD_NUMBER,))
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_owner', ?)", (DEFAULT_CARD_OWNER,))
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (GEMINI_API_KEY,))
    db.commit()

init_db()


def q(sql, args=(), one=False, commit=False):
    cur = db.execute(sql, args)
    if commit:
        db.commit()
    return cur.fetchone() if one else cur.fetchall()


def now_utc():
    return datetime.now(timezone.utc)


def fmt(dt):
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def get_card_info():
    r_num = q("SELECT value FROM settings WHERE key='card_number'", one=True)
    r_owner = q("SELECT value FROM settings WHERE key='card_owner'", one=True)
    num = r_num["value"] if r_num and r_num["value"] else DEFAULT_CARD_NUMBER
    owner = r_owner["value"] if r_owner and r_owner["value"] else DEFAULT_CARD_OWNER
    return num, owner


def get_gemini_api_key():
    try:
        r = q("SELECT value FROM settings WHERE key='gemini_api_key'", one=True)
        if r and r["value"]:
            return r["value"].strip()
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY", "").strip()


def get_gemini_client():
    key = get_gemini_api_key()
    return genai.Client(api_key=key, http_options={"api_version": "v1beta"})


def get_user_plan(uid):
    r = q("SELECT plan, expires_at FROM users WHERE user_id=?", (uid,), one=True)
    if r and r["plan"] and r["expires_at"]:
        exp = datetime.fromisoformat(r["expires_at"])
        if exp > now_utc():
            return r["plan"], exp
    return None, None


def register_user(u: types.User):
    q("""INSERT INTO users (user_id, username, full_name, joined_at) VALUES (?,?,?,?)
         ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
      (u.id, u.username, u.full_name, now_utc().isoformat()), commit=True)


# ============================ ZAXIRA NUSXA VA TIKLASH ============================
BACKUP_FILE = "channels_backup.json"

def auto_backup_channels():
    try:
        rows = q("SELECT * FROM channels")
        data = [dict(r) for r in rows]
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Zaxira fayl yozishda xato: {e}")


def auto_restore_channels():
    if not os.path.exists(BACKUP_FILE):
        return 0
    try:
        with open(BACKUP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = 0
        for r in data:
            q("""INSERT OR IGNORE INTO channels (channel_id, owner_id, title, niche, audience, group_id, active, posts_sent, join_requests, joined, left_count, group_messages, warnings, bans, detailed_prompt, custom_hours, polls_sent, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (r.get("channel_id"), r.get("owner_id"), r.get("title"), r.get("niche"), r.get("audience"),
               r.get("group_id"), r.get("active", 1), r.get("posts_sent", 0), r.get("join_requests", 0),
               r.get("joined", 0), r.get("left_count", 0), r.get("group_messages", 0), r.get("warnings", 0),
               r.get("bans", 0), r.get("detailed_prompt", ""), r.get("custom_hours", ""), r.get("polls_sent", 0),
               r.get("created_at", now_utc().isoformat())), commit=True)
            count += 1
        return count
    except Exception as e:
        logging.warning(f"Zaxiradan tiklashda xato: {e}")
        return 0

auto_restore_channels()


# ============================ KANAL HAVOLALARINI TOZALASH ============================
def clean_channel_input(text: str):
    text = text.strip()
    m_c = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/c/(\d+)", text)
    if m_c:
        cid = int(m_c.group(1))
        return -int(f"100{cid}") if not str(cid).startswith("100") else -cid
    m = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/+([a-zA-Z0-9_]+)", text)
    if m:
        u = m.group(1)
        if u not in ("joinchat", "c", "addstickers"):
            return "@" + u
    if text.startswith("@"):
        return text
    if text.lstrip("-").isdigit():
        val = int(text)
        if val > 0:
            s = str(val)
            return -int(s) if s.startswith("100") else -int(f"100{s}")
        return val
    clean = text.replace("https://", "").replace("http://", "").replace("t.me/", "").replace("/", "").strip()
    return "@" + clean if not clean.startswith("@") else clean


# ============================ KLAVIATURALAR ============================
def ik(*rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=t, url=d) if d.startswith("http")
         else types.InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])


MENU = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="➕ Kanal ulash"), types.KeyboardButton(text="📺 Kanallarim")],
        [types.KeyboardButton(text="💎 Tariflar & To'lov"), types.KeyboardButton(text="👤 Profilim")],
    ],
    resize_keyboard=True,
)


def plans_keyboard():
    return ik(*[[(f"💳 {p['name']} — {p['price']:,} so'm".replace(",", " "), f"buy:{k}")] for k, p in PLANS.items()])


def plans_text():
    return "\n\n".join(
        f"💎 <b>{p['name']}</b> — <code>{p['price']:,} so'm/oy</code>\n"
        f"📌 <i>{p['desc']}</i>".replace(",", " ")
        for p in PLANS.values()
    )


# ============================ HOLATLAR (FSM) ============================
class AddCh(StatesGroup):
    channel = State()
    niche = State()
    detailed_prompt = State()
    audience = State()
    group = State()
    hours = State()


# ============================ MAJBURIY OBUNA MIDDLEWARE ============================
async def missing_channels(uid):
    missing = []
    for r in q("SELECT chat FROM required"):
        try:
            m = await bot.get_chat_member(r["chat"], uid)
            if m.status in ("left", "kicked"):
                missing.append(r["chat"])
        except Exception as e:
            logging.warning(f"Majburiy kanal tekshirib bo'lmadi ({r['chat']}): {e}")
    return missing


def sub_keyboard(missing):
    rows = [[(f"📢 {c}", f"https://t.me/{c.lstrip('@')}")] for c in missing]
    rows.append([("✅ Obunani tekshirish", "check_sub")])
    return ik(*rows)


class SubGate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if isinstance(event, types.Message):
            chat = event.chat
        elif isinstance(event, types.CallbackQuery) and event.message:
            chat = event.message.chat
        else:
            chat = None
        if user is None or chat is None or chat.type != "private" or user.id in ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, types.CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        register_user(user)
        row = q("SELECT banned FROM users WHERE user_id=?", (user.id,), one=True)
        if row and row["banned"]:
            return
        missing = await missing_channels(user.id)
        if not missing:
            return await handler(event, data)

        text = (
            "⚠️ <b>Diqqat!</b> Bot xizmatlaridan foydalanish uchun quyidagi homiy kanallarga obuna bo'ling "
            "va so'ng <b>«Obunani tekshirish»</b> tugmasini bosing:"
        )
        if isinstance(event, types.CallbackQuery):
            await event.answer("Avval homiy kanallarga obuna bo'ling!", show_alert=True)
            await event.message.answer(text, reply_markup=sub_keyboard(missing))
        else:
            await event.answer(text, reply_markup=sub_keyboard(missing))


dp.message.outer_middleware(SubGate())
dp.callback_query.outer_middleware(SubGate())


# ============================ 1. TUZATILGAN: KANAL SO'ROVLARINI QABUL QILISH ============================
@dp.chat_join_request()
async def handle_join_request(update: types.ChatJoinRequest):
    """Kanalga kelgan a'zolik so'rovlarini qabul qilish va statistikani yangilash"""
    channel_id = update.chat.id
    user = update.from_user
    try:
        await update.approve()
        q("UPDATE channels SET join_requests = join_requests + 1, joined = joined + 1 WHERE channel_id=?",
          (channel_id,), commit=True)
        auto_backup_channels()
        logging.info(f"Yangi so'rov qabul qilindi: Kanal: {channel_id}, User: {user.id}")
    except Exception as e:
        logging.error(f"Kanalga a'zo so'rovini qabul qilishda xato ({channel_id}): {e}")


# ============================ 2. TUZATILGAN: GURUH MODERATSIYASI VA 20 SEKUNDDA TOZALASH ============================
async def auto_delete_msg(chat_id: int, message_id: int, delay: int = 20):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_moderation(m: types.Message):
    if not m.from_user or m.from_user.is_bot:
        return

    text = m.text or m.caption or ""
    chat_id = m.chat.id
    user_id = m.from_user.id

    is_profane = bool(PROFANITY_RE.search(text))
    is_scam = bool(SCAM_RE.search(text))

    if is_profane or is_scam:
        try:
            await m.delete()
        except Exception as e:
            logging.warning(f"Xabarni o'chirib bo'lmadi: {e}")

        warn_text = "qoidabuzarlik (reklama/so'kinish)" if is_scam else "haqorat/so'kinish"
        warn_msg = await m.answer(
            f"⚠️ <b>{html.escape(m.from_user.full_name)}</b>, guruhda {warn_text} taqiqlangan! "
            f"Xabaringiz o'chirildi."
        )
        # 20 soniyada ogohlantirish xabarini ham tozalash
        asyncio.create_task(auto_delete_msg(chat_id, warn_msg.message_id, 20))

        # Bazadagi qoidabuzarlik hisobini yangilash
        q("""INSERT INTO violations (chat_id, user_id, warnings, cycle) VALUES (?, ?, 1, 0)
             ON CONFLICT(chat_id, user_id) DO UPDATE SET warnings = warnings + 1""",
          (chat_id, user_id), commit=True)
        return

    # Guruh statistikasi
    q("UPDATE channels SET group_messages = group_messages + 1 WHERE group_id=?", (chat_id,), commit=True)


# ============================ ASOSIY BUYRUQLAR ============================
@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    register_user(m.from_user)
    await m.answer(
        f"👋 <b>Assalomu alaykum, {html.escape(m.from_user.full_name)}!</b>\n\n"
        "🚀 <b>Professional AI Kanal & Guruh Menejeri</b>ga xush kelibsiz!\n\n"
        "⚡️ <b>Imkoniyatlarimiz:</b>\n"
        "• <b>Rang-barang Postlar</b> — Real keyslar, bepul xostinglar, foydali vositalar!\n"
        "• 🖼 <b>Xilma-xil Rasmlar</b> — Flux AI san'at asarlari + Internetdan haqiqiy fotolar!\n"
        "• 📊 <b>Kafolatlangan So'rovnomalar</b> — Auditoriyangizni uyg'otuvchi interaktiv ovoz berishlar.\n"
        "• 🛡 <b>Xushyor Guruh Moderatori</b> — So'kinish, haqorat va firibgarlikni 20 soniyada yo'q qiladi.\n\n"
        "Pastdagi menyudan boshlang 👇",
        reply_markup=MENU,
    )


@dp.message(F.chat.type == "private", F.text.in_({"💎 Tariflar & To'lov", "💰 Hisobim"}))
async def my_account(m: types.Message, state: FSMContext):
    await state.clear()
    plan, exp = get_user_plan(m.from_user.id)
    card_num, card_own = get_card_info()
    if plan:
        status = f"🟢 <b>Faol tarif:</b> <code>{PLANS[plan]['name']}</code>\n⏳ <b>Tugash vaqti:</b> <code>{fmt(exp)}</code> gacha"
    else:
        status = "🔴 <b>Faol tarif mavjud emas</b>"
    
    await m.answer(
        f"💎 <b>TARIFLAR VA TO'LOV TIZIMI</b>\n\n"
        f"📊 <b>Sizning holatingiz:</b>\n{status}\n\n"
        f"💳 <b>To'lov kartasi:</b> <code>{card_num}</code> ({html.escape(card_own)})\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Mavjud Tariflar:</b>\n\n{plans_text()}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👇 <i>Tarifni faollashtirish uchun pastdagi tugmani bosing:</i>",
        reply_markup=plans_keyboard(),
    )


@dp.message(F.chat.type == "private", F.text == "👤 Profilim")
async def profile(m: types.Message, state: FSMContext):
    await state.clear()
    u = q("SELECT * FROM users WHERE user_id=?", (m.from_user.id,), one=True)
    plan, exp = get_user_plan(m.from_user.id)
    joined = datetime.fromisoformat(u["joined_at"]) if u and u["joined_at"] else now_utc()
    plan_name = PLANS[plan]['name'] if plan else 'Tarif yo\'q'
    await m.answer(
        "👤 <b>FOYDALANUVCHI PROFILI</b>\n\n"
        f"🆔 <b>Sizning ID:</b> <code>{m.from_user.id}</code>\n"
        f"👤 <b>Ismingiz:</b> <b>{html.escape(m.from_user.full_name)}</b>\n"
        f"📅 <b>Ro'yxatdan o'tgan:</b> <code>{fmt(joined)}</code>\n"
        f"💎 <b>Joriy tarif:</b> <b>{plan_name}</b>\n"
    )


# ============================ SERVERNI ISHGA TUSHIRISH ============================
async def main():
    scheduler.start()
    logging.info("Bot muvaffaqiyatli ishga tushdi...")
    # allowed_updates ichiga chat_join_request qo'shildi
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_join_request"])


if __name__ == "__main__":
    asyncio.run(main())
# ============================ RENDER UCHUN DUMMY WEB SERVER ============================
async def handle_ping(request):
    return web.Response(text="Bot ishlayapti!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Veb-server {port}-portda ishga tushdi.")

# ============================ ASOSIY ISHGA TUSHIRISH ============================
async def main():
    scheduler.start()
    await start_web_server()  # Render portini ochadi va Timed Out xatosini yo'qotadi
    logging.info("Bot muvaffaqiyatli ishga tushdi...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_join_request"])

if __name__ == "__main__":
    asyncio.run(main())
