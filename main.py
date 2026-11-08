"""
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
from aiogram.enums import ParseMode
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6I4Sl3rZXMyL5C1J6ANVNLFUbANDfJTRhno8pFKT0huEA")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
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

# Gemini AI (v1beta)
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

    # Baza boshlang'ich sozlamalari
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
    return os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6I4Sl3rZXMyL5C1J6ANVNLFUbANDfJTRhno8pFKT0huEA").strip()


def get_gemini_client():
    key = get_gemini_api_key()
    return genai.Client(api_key=key, http_options={"api_version": "v1beta"})


async def test_gemini_key(key: str = None):
    test_k = key.strip() if key else get_gemini_api_key()
    if not test_k:
        return False, "Kalit bo'sh!"
    try:
        test_client = genai.Client(api_key=test_k, http_options={"api_version": "v1beta"})
        models_to_test = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest", "gemini-3-flash-preview"]
        last_e = ""
        for m in models_to_test:
            try:
                res = await test_client.aio.models.generate_content(model=m, contents="Test")
                if res and (res.text or "").strip():
                    return True, f"✅ Aloqa o'rnatildi! ({m} modeli javob berdi)"
            except Exception as e:
                last_e = str(e)
        return False, f"Google xatosi: {last_e or 'Javob olinmadi'}"
    except Exception as e:
        return False, f"Ulanish xatosi: {e}"


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


# ============================ ZAXIRA NUSXA VA TIKLASH (RENDER DISK XIMOYASI) ============================
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


class EditPrompt(StatesGroup):
    prompt = State()


class EditHours(StatesGroup):
    hours = State()


class EditGroup(StatesGroup):
    group = State()


class AdminCard(StatesGroup):
    number = State()
    owner = State()


class AdminRestore(StatesGroup):
    file = State()


class AdminGemini(StatesGroup):
    key = State()


class Pay(StatesGroup):
    receipt = State()


class AdminUser(StatesGroup):
    uid = State()


class Bcast(StatesGroup):
    content = State()


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


@dp.callback_query(F.data == "check_sub")
async def check_sub(cb: types.CallbackQuery):
    register_user(cb.from_user)
    missing = await missing_channels(cb.from_user.id)
    if missing:
        await cb.answer("❌ Hali hamma kanallarga a'zo bo'lmadingiz!", show_alert=True)
        return
    await cb.answer("✅ Obuna muvaffaqiyatli tasdiqlandi!")
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer("🎉 <b>Xush kelibsiz!</b> Asosiy menyudan foydalanishingiz mumkin 👇", reply_markup=MENU)


# ============================ ASOSIY MENYU ============================
@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    register_user(m.from_user)
    await m.answer(
        f"👋 <b>Assalomu alaykum, {html.escape(m.from_user.full_name)}!</b>\n\n"
        "🚀 <b>Professional AI Kanal & Guruh Menejeri</b>ga xush kelibsiz!\n\n"
        "⚡️ <b>Imkoniyatlarimiz:</b>\n"
        "• <b>Rang-barang Postlar</b> — Real keyslar, bepul xostinglar (Render, PythonAnywhere), "
        "foydali vositalar, YouTube darsliklari, kulgili memlar va mantiqiy o'yinlar!\n"
        "• 🖼 <b>Xilma-xil Rasmlar</b> — Flux AI san'at asarlari + Internetdan haqiqiy fotolar!\n"
        "• 📊 <b>Kafolatlangan So'rovnomalar</b> — Auditoriyangizni uyg'otuvchi interaktiv ovoz berishlar.\n"
        "• 🛡 <b>Xushyor Guruh Moderatori</b> — So'kinish, haqorat, kamsitish va firibgarlikni zudlik bilan yo'q qiladi (20 soniyada o'chiradi).\n"
        "• 💬 <b>Samimiy AI Muloqot</b> — Guruhdagi har bir a'zoga xushmuomala javob qaytaradi va ularning fikriga qarab kanalga post chiqaradi!\n\n"
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
    ch_rows = q("SELECT COUNT(*) c, COALESCE(SUM(posts_sent),0) p, COALESCE(SUM(polls_sent),0) pl FROM channels WHERE owner_id=?",
                (m.from_user.id,), one=True)
    plan, exp = get_user_plan(m.from_user.id)
    joined = datetime.fromisoformat(u["joined_at"]) if u and u["joined_at"] else now_utc()
    await m.answer(
        "👤 <b>FOYDALANUVCHI PROFILI</b>\n\n"
        f"🆔 <b>Sizning ID:</b> <code>{m.from_user.id}</code>\n"
        f"👤 <b>Ismingiz:</b> <b>{html.escape(m.from_user.full_name)}</b>\n"
        f"📅 <b>Ro'yxatdan o'tgan:</b> <code>{fmt(joined)}</code>\n"
        f"💎 <b>Joriy tarif:</b> <b>{PLANS[plan]['name'] if plan else 'Yo`q (faol emas)'}</b>"
        f"{(' (tugaydi: ' + fmt(exp) + ')') if plan else ''}\n"
        f"📺 <b>Ulangan kanallar:</b> <b>{ch_rows['c']} ta</b>\n"
        f"📝 <b>Yuborilgan AI postlar:</b> <b>{ch_rows['p']} ta</b>\n"
        f"📊 <b>Yuborilgan so'rovnomalar:</b> <b>{ch_rows['pl']} ta</b>"
    )


# ============================ TO'LOV JARAYONI ============================
@dp.callback_query(F.data.startswith("buy:"))
async def buy(cb: types.CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    p = PLANS.get(key)
    if not p:
        return await cb.answer("Tarif topilmadi")
    card_num, card_own = get_card_info()
    await state.set_state(Pay.receipt)
    await state.update_data(plan=key)
    await cb.message.answer(
        f"💳 <b>{p['name']}</b> tarifini xarid qilish\n\n"
        f"💵 <b>To'lov summasi:</b> <code>{p['price']:,} so'm</code>\n"
        f"💳 <b>Karta raqami:</b> <code>{card_num}</code>\n"
        f"👤 <b>Karta egasi:</b> <b>{html.escape(card_own)}</b>\n\n"
        f"📸 To'lovni amalga oshirgach, <b>chek skrinshotini rasm sifatida</b> shu yerga yuboring.\n"
        f"❌ Bekor qilish: /cancel".replace(",", " ")
    )
    await cb.answer()


@dp.message(F.text.casefold().in_({"cancel", "/cancel", "bekor", "bekor qilish", "❌ bekor qilish", "отмена", "/bekor"}))
@dp.message(Command("cancel"))
async def cancel(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ <b>Amaliyot bekor qilindi.</b>", reply_markup=MENU if m.chat.type == "private" else None)


@dp.message(Pay.receipt, F.photo)
async def got_receipt(m: types.Message, state: FSMContext):
    key = (await state.get_data())["plan"]
    p = PLANS[key]
    file_id = m.photo[-1].file_id
    cur = db.execute(
        "INSERT INTO payments (user_id, plan, amount, file_id, created_at) VALUES (?,?,?,?,?)",
        (m.from_user.id, key, p["price"], file_id, now_utc().isoformat()),
    )
    db.commit()
    pid = cur.lastrowid
    await state.clear()
    await m.answer("✅ <b>Chek qabul qilindi!</b> Admin tekshirib tasdiqlagach, tarifingiz avtomatik faollashadi.", reply_markup=MENU)
    
    caption = (
        f"💳 <b>Yangi To'lov #{pid}</b>\n\n"
        f"👤 <b>Foydalanuvchi:</b> {html.escape(m.from_user.full_name)} (<code>{m.from_user.id}</code>)\n"
        f"💎 <b>Tarif:</b> <b>{p['name']}</b> — <code>{p['price']:,} so'm</code>\n"
        f"📅 <b>Vaqti:</b> <code>{fmt(now_utc())}</code>".replace(",", " ")
    )
    for aid in ADMIN_IDS:
        try:
            await bot.send_photo(aid, file_id, caption=caption,
                                 reply_markup=ik([("✅ Tasdiqlash", f"pay_ok:{pid}"), ("❌ Rad etish", f"pay_no:{pid}")]))
        except Exception as e:
            logging.error(f"Adminga chek yuborilmadi ({aid}): {e}")


@dp.message(Pay.receipt)
async def receipt_wrong(m: types.Message):
    await m.answer("⚠️ Iltimos, to'lov chekini <b>rasm (skrinshot)</b> sifatida yuboring yoki /cancel bosing.")


def apply_plan(uid, plan_key, days=PLAN_DAYS):
    cur_plan, exp = get_user_plan(uid)
    base = exp if exp else now_utc()
    new_exp = base + timedelta(days=days)
    q("UPDATE users SET plan=?, expires_at=? WHERE user_id=?", (plan_key, new_exp.isoformat(), uid), commit=True)
    return new_exp


@dp.callback_query(F.from_user.id.in_(ADMIN_IDS), F.data.startswith(("pay_ok:", "pay_no:")))
async def pay_decision(cb: types.CallbackQuery):
    action, pid = cb.data.split(":")
    pay = q("SELECT * FROM payments WHERE id=?", (int(pid),), one=True)
    if not pay or pay["status"] != "pending":
        return await cb.answer("Bu to'lov allaqachon ko'rib chiqilgan!", show_alert=True)
    if action == "pay_ok":
        q("INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?,?)", (pay["user_id"], now_utc().isoformat()), commit=True)
        exp = apply_plan(pay["user_id"], pay["plan"])
        q("UPDATE payments SET status='approved' WHERE id=?", (pay["id"],), commit=True)
        note = f"🎉 <b>To'lovingiz tasdiqlandi!</b>\n\n💎 <b>{PLANS[pay['plan']]['name']}</b> tarifi <b>{fmt(exp)}</b> gacha faollashtirildi. Marhamat, kanallaringizni ulang!"
        status = "✅ Tasdiqlandi"
    else:
        q("UPDATE payments SET status='rejected' WHERE id=?", (pay["id"],), commit=True)
        note = "❌ <b>To'lovingiz tasdiqlanmadi.</b> Chek yaroqsiz bo'lsa yoki summa to'liq bo'lmasa, qayta to'lov qiling yoki adminga murojaat qiling."
        status = "❌ Rad etildi"
    try:
        await bot.send_message(pay["user_id"], note)
    except Exception:
        pass
    await cb.message.edit_caption(caption=(cb.message.caption or "") + f"\n\n<b>{status}</b> ({cb.from_user.full_name})", reply_markup=None)
    await cb.answer("Bajarildi!")


# ============================ KANAL ULASH ============================
async def is_admin_of(chat_id, user_id):
    if user_id in ADMIN_IDS:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


@dp.message(F.chat.type == "private", F.text == "➕ Kanal ulash")
async def add_channel(m: types.Message, state: FSMContext):
    await state.clear()
    plan, _ = get_user_plan(m.from_user.id)
    if not plan:
        return await m.answer(
            "⚠️ <b>Kanal ulash uchun avval tarif xarid qilishingiz kerak!</b>\n\n"
            f"{plans_text()}", reply_markup=plans_keyboard()
        )
    n = q("SELECT COUNT(*) c FROM channels WHERE owner_id=?", (m.from_user.id,), one=True)["c"]
    if n >= PLANS[plan]["channels"]:
        return await m.answer(
            f"⚠️ Sizning <b>{PLANS[plan]['name']}</b> tarifingizda ko'pi bilan <b>{PLANS[plan]['channels']} ta</b> kanal ulash mumkin.\n"
            f"Ko'proq kanal ulash uchun yuqoriroq tarifga o'ting."
        )
    await state.set_state(AddCh.channel)
    await m.answer(
        "1️⃣ <b>Kanalni tayyorlash:</b>\n\n"
        "1. Botni kanalingizga <b>Admin</b> qiling (Post yuborish va tahrirlash huquqi bilan).\n"
        "2. So'ng kanalingizning <b>to'liq havolasi</b> (masalan: <code>https://t.me/kanalingiz</code>), "
        "<b>@username</b> manzilini yoki kanal ID raqamini shu yerga yuboring.\n\n"
        "❌ Bekor qilish: /cancel"
    )


@dp.message(AddCh.channel, F.text)
async def add_ch_channel(m: types.Message, state: FSMContext):
    target = clean_channel_input(m.text)
    try:
        chat = await bot.get_chat(target)
    except Exception:
        return await m.answer(
            "❌ <b>Kanal topilmadi!</b>\n\n"
            "Iltimos, havolani to'g'ri yuborganingizni tekshiring (masalan: <code>https://t.me/kanal_nomi</code> yoki <code>@kanal_nomi</code>) "
            "va bot u yerda admin qilinganligiga ishonch hosil qiling."
        )
    if chat.type != "channel":
        return await m.answer("❌ Bu kanal emas. Iltimos, Telegram kanal havolasini yuboring.")
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception:
        return await m.answer("❌ Bot bu kanalda topilmadi. Avval botni kanalingizga admin qiling.")
    if me.status != "administrator" or not getattr(me, "can_post_messages", True):
        return await m.answer("❌ Bot kanalda post yuborish huquqiga ega admin emas. Huquqlarini to'liq bering.")
    if not await is_admin_of(chat.id, m.from_user.id):
        return await m.answer("❌ Siz bu kanalning admini emassiz.")
    if q("SELECT 1 FROM channels WHERE channel_id=?", (chat.id,), one=True):
        return await m.answer("❌ Bu kanal allaqachon tizimga ulangan.")
    
    await state.update_data(channel_id=chat.id, title=chat.title)
    await state.set_state(AddCh.niche)
    await m.answer(
        f"✅ <b>{html.escape(chat.title)}</b> muvaffaqiyatli tekshirildi!\n\n"
        "2️⃣ <b>Kanal yo'nalishi nima?</b> (Masalan: <i>Dasturlash va IT, Trading va Kriptovalyuta, PUBG Mobile, Ingliz tili, Biznes va Startaplar</i>):"
    )


@dp.message(AddCh.niche, F.text)
async def add_ch_niche(m: types.Message, state: FSMContext):
    await state.update_data(niche=m.text.strip())
    await state.set_state(AddCh.detailed_prompt)
    await m.answer(
        "3️⃣ <b>AI UCHUN BATAFSIL PROMPT VA TALABLARINGIZ:</b>\n\n"
        "AI aynan nimalar haqida yozishi kerakligini batafsil tushuntiring. "
        "Faqat bitta mavzuni aylantirmaslik uchun, nimalarni ko'proq yoritishi kerakligini yozing!\n\n"
        "<i>Masalan:</i>\n"
        "«Kanalda faqat pul ishlash emas, balki real keyslar (kim qanday bot/sayt qilib sotdi), "
        "botlarni tekin xostinglarga qo'yish (Render, PythonAnywhere), sayt yaratish asboblari, "
        "YouTube'dagi eng zo'r darsliklar tavsiyasi, qiziqarli IT o'yinlar, topishmoqlar va kulgili memlar chiqsin. "
        "Xuddi onadek mehribonlik bilan o'rgatuvchi tilda yozilsin.»\n\n"
        "👇 <b>O'z batafsil yo'riqnomangizni yuboring (cheksiz uzunlikda yozishingiz mumkin):</b>"
    )


@dp.message(AddCh.detailed_prompt, F.text)
async def add_ch_prompt(m: types.Message, state: FSMContext):
    await state.update_data(detailed_prompt=m.text.strip())
    await state.set_state(AddCh.audience)
    await m.answer(
        "4️⃣ <b>Auditoriyangiz kimlar?</b>\n\n"
        "Yoshi, darajasi va qiziqishlari (Masalan: <i>16–30 yoshli talabalar, boshlovchi dasturchilar va o'rganuvchilar</i>):"
    )


@dp.message(AddCh.audience, F.text)
async def add_ch_audience(m: types.Message, state: FSMContext):
    await state.update_data(audience=m.text.strip())
    await state.set_state(AddCh.group)
    await m.answer(
        "5️⃣ <b>Kanalning muhokama guruhi (chat) bormi?</b>\n\n"
        "Guruh havolasi (<code>https://t.me/guruh</code>), <b>@username</b> yoki ID raqamini yuboring "
        "(bot u yerda ham admin bo'lishi kerak, shunda so'kinganlarni jazolaydi va a'zolarga do'stona javob beradi).\n\n"
        "<i>Agar guruhingiz bo'lmasa, shunchaki «yo'q» deb yozing.</i>"
    )


@dp.message(AddCh.group, F.text)
async def add_ch_group(m: types.Message, state: FSMContext):
    group_id = None
    txt = m.text.strip()
    if txt.lower() not in ("yo'q", "yoq", "yo‘q", "yo`q", "none", "no"):
        target_g = clean_channel_input(txt)
        try:
            g = await bot.get_chat(target_g)
            me = await bot.get_chat_member(g.id, bot.id)
        except Exception:
            return await m.answer("❌ Guruh topilmadi yoki bot unda yo'q. Qayta yuboring yoki «yo'q» deb yozing.")
        if g.type not in ("group", "supergroup") or me.status != "administrator":
            return await m.answer("❌ Bu guruh emas yoki bot unda admin emas. Botni admin qiling.")
        if not await is_admin_of(g.id, m.from_user.id):
            return await m.answer("❌ Siz bu guruhning admini emassiz.")
        group_id = g.id
    
    await state.update_data(group_id=group_id)
    await state.set_state(AddCh.hours)
    await m.answer(
        "6️⃣ <b>Postlar qaysi soatlarda chiqsin?</b>\n\n"
        "Soatlarni vergul bilan kiriting (0 dan 23 gacha, masalan: <code>9, 14, 20</code>).\n"
        "<i>Agar tarifingizdagi standart soatlar qolishini xohlasangiz, «standart» deb yozing.</i>"
    )


@dp.message(AddCh.hours, F.text)
async def add_ch_hours(m: types.Message, state: FSMContext):
    txt = m.text.strip().lower()
    custom_hours = ""
    if txt not in ("standart", "standart rejim", "default"):
        raw_nums = re.findall(r"\b([0-1]?[0-9]|2[0-3])\b", txt)
        hrs = [int(x) for x in raw_nums if 0 <= int(x) <= 23]
        if hrs:
            custom_hours = ",".join(map(str, sorted(set(hrs))))
        else:
            return await m.answer("⚠️ Iltimos, soatlarni to'g'ri kiriting (masalan: <code>9, 14, 20</code>) yoki «standart» deb yozing:")
    
    d = await state.get_data()
    q("""INSERT INTO channels (channel_id, owner_id, title, niche, detailed_prompt, audience, group_id, custom_hours, created_at)
         VALUES (?,?,?,?,?,?,?,?,?)""",
      (d["channel_id"], m.from_user.id, d["title"], d["niche"], d["detailed_prompt"], d["audience"],
       d["group_id"], custom_hours, now_utc().isoformat()), commit=True)
    await state.clear()
    
    # Zaxira nusxani darhol yangilash
    auto_backup_channels()
    
    await m.answer(
        f"🎉 <b>Kanal muvaffaqiyatli ulandi!</b>\n\n"
        f"📺 <b>Nomi:</b> <b>{html.escape(d['title'])}</b>\n"
        f"🎯 <b>Yo'nalish:</b> {html.escape(d['niche'])}\n"
        f"⏰ <b>Soatlar:</b> <code>{custom_hours or 'Tarif bo`yicha standart'}</code>\n"
        f"🛡 <b>Guruh nazorati:</b> {'Ulangan va faol' if d['group_id'] else 'Ulanmagan'}\n\n"
        f"🚀 <i>Endi «📺 Kanallarim» bo'limi orqali istalgan payt rasm bilan post yaratishingiz yoki so'rovnoma yuborishingiz mumkin!</i>",
        reply_markup=MENU,
    )


# ============================ KANALLARIM BO'LIMI ============================
def own_channel(cid, uid):
    return q("SELECT * FROM channels WHERE channel_id=? AND owner_id=?", (cid, uid), one=True)


@dp.message(F.chat.type == "private", F.text == "📺 Kanallarim")
async def my_channels(m: types.Message, state: FSMContext):
    await state.clear()
    rows = q("SELECT channel_id, title, active FROM channels WHERE owner_id=?", (m.from_user.id,))
    if not rows:
        return await m.answer("📺 Hali hech qanday kanal ulanmagan. «➕ Kanal ulash» tugmasini bosing.")
    await m.answer(
        "📺 <b>SIZNING ULANGAN KANALLARINGIZ:</b>\n\n"
        "<i>Boshqarish uchun kerakli kanalni tanlang 👇</i>",
        reply_markup=ik(*[[(("🟢 " if r["active"] else "⏸ ") + r["title"], f"ch:{r['channel_id']}")] for r in rows])
    )


async def channel_card(cid, uid):
    c = own_channel(cid, uid)
    if not c:
        return None, None
    try:
        subs = await bot.get_chat_member_count(cid)
    except Exception:
        subs = "Aniqlanmadi"
    plan, exp = get_user_plan(uid)
    hours_display = c["custom_hours"] if c["custom_hours"] else (", ".join(map(str, PLANS[plan]["hours"])) if plan else "Yo'q")
    prompt_snippet = (c["detailed_prompt"][:120] + "...") if c["detailed_prompt"] and len(c["detailed_prompt"]) > 120 else (c["detailed_prompt"] or "Kiritilmagan")
    
    text = (
        f"📺 <b>KANAL: {html.escape(c['title'])}</b>\n\n"
        f"⚙️ <b>Holat:</b> {'🟢 Faol' if c['active'] else '⏸ To`xtatilgan'}"
        f"{'' if plan else ' ⚠️ (Tarif tugagan!)'}\n"
        f"🎯 <b>Yo'nalish:</b> <b>{html.escape(c['niche'] or '')}</b>\n"
        f"👥 <b>Auditoriya:</b> <i>{html.escape(c['audience'] or '')}</i>\n"
        f"💬 <b>Guruh ID:</b> <code>{c['group_id'] or 'Ulanmagan'}</code>\n"
        f"⏰ <b>Post soatlari:</b> <code>{hours_display}</code>\n"
        f"📝 <b>AI Prompt:</b> <i>{html.escape(prompt_snippet)}</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>STATISTIKA:</b>\n"
        f"• 👥 Obunachilar: <b>{subs}</b>\n"
        f"• 📝 Yuborilgan postlar: <b>{c['posts_sent']} ta</b>\n"
        f"• 📊 Yuborilgan so'rovlar: <b>{c['polls_sent']} ta</b>\n"
        f"• 💬 Guruhdagi xabarlar: <b>{c['group_messages']} ta</b>\n"
        f"• ⚠️ Guruh ogohlantirishlari: <b>{c['warnings']} ta</b>\n"
        f"• 🚫 Guruh bloklari: <b>{c['bans']} ta</b>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    kb = ik(
        [("🖼 Rasm + Post yuborish", f"imgpost:{cid}"), ("📊 So'rovnoma (Poll) yuborish", f"poll:{cid}")],
        [("📝 Faqat matnli post", f"post:{cid}"), ("⏰ Soatlarni sozlash", f"sethours:{cid}")],
        [("✏️ Promptni tahrirlash", f"setprompt:{cid}"), ("💬 Guruhni ulash/o'zgartirish", f"setgrp:{cid}")],
        [("⏸ / ▶️ Holatni o'zgartirish", f"toggle:{cid}"), ("🗑 Kanalni o'chirish", f"del:{cid}")],
    )
    return text, kb


@dp.callback_query(F.data.startswith("ch:"))
async def show_channel(cb: types.CallbackQuery):
    text, kb = await channel_card(int(cb.data.split(":")[1]), cb.from_user.id)
    if not text:
        return await cb.answer("Kanal topilmadi!", show_alert=True)
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_channel(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer()
    q("UPDATE channels SET active = 1 - active WHERE channel_id=?", (cid,), commit=True)
    auto_backup_channels()
    text, kb = await channel_card(cid, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Holat yangilandi!")


@dp.callback_query(F.data.startswith("del:"))
async def delete_channel(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    if own_channel(cid, cb.from_user.id):
        q("DELETE FROM channels WHERE channel_id=?", (cid,), commit=True)
        auto_backup_channels()
        await cb.message.edit_text("🗑 <b>Kanal muvaffaqiyatli olib tashlandi.</b>")
    await cb.answer()


# ============================ PROMPT, GURUH VA SOATLARNI TAHRIRLASH ============================
@dp.callback_query(F.data.startswith("setprompt:"))
async def edit_prompt_start(cb: types.CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer("Kanal topilmadi")
    await state.set_state(EditPrompt.prompt)
    await state.update_data(cid=cid)
    await cb.message.answer(
        "✏️ <b>AI UCHUN YANGI PROMPT (YO'RIQNOMA) YOZING:</b>\n\n"
        "AI qanday mavzularda, qanday formatda, nimalarni ko'proq yoritishi kerakligini batafsil yozing. "
        "Istalgancha uzun matn yozishingiz mumkin!\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(EditPrompt.prompt, F.text)
async def edit_prompt_finish(m: types.Message, state: FSMContext):
    d = await state.get_data()
    cid = d["cid"]
    q("UPDATE channels SET detailed_prompt=? WHERE channel_id=?", (m.text.strip(), cid), commit=True)
    auto_backup_channels()
    await state.clear()
    await m.answer("✅ <b>AI Prompti muvaffaqiyatli yangilandi!</b>", reply_markup=MENU)


@dp.callback_query(F.data.startswith("sethours:"))
async def edit_hours_start(cb: types.CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer("Kanal topilmadi")
    await state.set_state(EditHours.hours)
    await state.update_data(cid=cid)
    await cb.message.answer(
        "⏰ <b>POST VA SO'ROVNOMA SOATLARINI SOZLASH:</b>\n\n"
        "Qaysi soatlarda avtomatik chiqsin? Vergul bilan kiriting (masalan: <code>8, 12, 17, 21</code>).\n"
        "Tarifdagi standart soatlarga qaytarish uchun «standart» deb yozing.\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(EditHours.hours, F.text)
async def edit_hours_finish(m: types.Message, state: FSMContext):
    d = await state.get_data()
    cid = d["cid"]
    txt = m.text.strip().lower()
    custom_hours = ""
    if txt not in ("standart", "standart rejim", "default"):
        raw_nums = re.findall(r"\b([0-1]?[0-9]|2[0-3])\b", txt)
        hrs = [int(x) for x in raw_nums if 0 <= int(x) <= 23]
        if hrs:
            custom_hours = ",".join(map(str, sorted(set(hrs))))
        else:
            return await m.answer("⚠️ Iltimos, soatlarni to'g'ri kiriting (masalan: <code>9, 14, 21</code>) yoki «standart» deb yozing:")
    
    q("UPDATE channels SET custom_hours=? WHERE channel_id=?", (custom_hours, cid), commit=True)
    auto_backup_channels()
    await state.clear()
    await m.answer(f"✅ <b>Soatlar sozlandi:</b> <code>{custom_hours or 'Standart'}</code>", reply_markup=MENU)


@dp.callback_query(F.data.startswith("setgrp:"))
async def edit_grp_start(cb: types.CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer("Kanal topilmadi")
    await state.set_state(EditGroup.group)
    await state.update_data(cid=cid)
    await cb.message.answer(
        "💬 <b>KANALNING MUHOKAMA GURUHI HAVOLASINI YUBORING:</b>\n\n"
        "Guruh havolasi (<code>https://t.me/guruh</code>), @username yoki ID raqamini yuboring.\n"
        "Bot u yerda admin bo'lishi kerak. Olib tashlash uchun «yo'q» deb yozing.\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(EditGroup.group, F.text)
async def edit_grp_finish(m: types.Message, state: FSMContext):
    d = await state.get_data()
    cid = d["cid"]
    txt = m.text.strip()
    group_id = None
    if txt.lower() not in ("yo'q", "yoq", "yo‘q", "none"):
        target_g = clean_channel_input(txt)
        try:
            g = await bot.get_chat(target_g)
            me = await bot.get_chat_member(g.id, bot.id)
            if me.status != "administrator":
                return await m.answer("❌ Bot bu guruhda admin emas. Avval admin qiling.")
            group_id = g.id
        except Exception:
            return await m.answer("❌ Guruh topilmadi yoki bot unda yo'q. Qayta urinib ko'ring yoki /cancel bosing.")
    
    q("UPDATE channels SET group_id=? WHERE channel_id=?", (group_id, cid), commit=True)
    auto_backup_channels()
    await state.clear()
    await m.answer(f"✅ <b>Guruh muvaffaqiyatli yangilandi:</b> <code>{group_id or 'Olib tashlandi'}</code>", reply_markup=MENU)


# ============================ PROFESSIONAL AI GENERATOR (100% KAFOLATLI VA KANALGA MOS) ============================
POST_THEMES = [
    "real_cases",       # Real loyihalar, keyslar, natija va muvaffaqiyat tajribalari
    "free_tools",       # Bepul asboblar, foydali platformalar va tekin vositalar
    "youtube_learning", # YouTube darsliklari, amaliy bilimlar (onadek/ustozdek g'amxo'r tilda)
    "interactive_game", # Qiziqarli mantiqiy savol, sohaviy topishmoq yoki test
    "funny_memes",      # Hayotiy kulgili voqea, mutaxassislar uchraydigan qiziqarli hazil
    "expert_guide"      # Kanal mavzusiga oid amaliy professional qo'llanma
]

IMAGE_STYLES = [
    "hyperrealistic 8k documentary photograph, natural studio lighting, ultra-detailed, depth of field",
    "futuristic 3d render, octane engine, vibrant glowing colors, modern tech aesthetic",
    "clean minimalist workspace photography, soft morning sunlight, aesthetic desk setup",
    "cinematic cyberpunk neon street scene, moody atmosphere, rich textures, 8k",
    "isometric 3d digital illustration, vivid pastel colors, clean vector design",
    "cozy vintage retro tech atmosphere, warm tones, high quality photography"
]


def generate_niche_adaptive_post_and_poll(c, theme=None):
    """
    Kanal yo'nalishi (niche), sarlavhasi (title) va promptiga 100% moslashuvchan,
    YouTube darslik havolasi va maxsus so'rovnomaga ega professional generator.
    AI kalit ishlamagan taqdirda ham kanallar bir-birini hech qachon takrorlamaydi!
    """
    title = (c.get("title") or "Kanalimiz").strip()
    niche = (c.get("niche") or "Foydali bilimlar va amaliyot").strip()
    detailed = (c.get("detailed_prompt") or "").strip()
    
    if not theme or theme not in POST_THEMES:
        theme = random.choice(POST_THEMES)
        
    n_lower = f"{niche} {detailed}".lower()
    
    # 1. Trading / Kripto
    if any(k in n_lower for k in ["treding", "trading", "kripto", "crypto", "forex", "valyuta", "scalping", "invest", "aksiya", "bozor"]):
        yt_query = "kripto va trading darslari uzbek"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = "professional modern financial trading desk multiple monitors glowing cryptocurrency candlestick charts dark mode 8k resolution"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: Kichik depozitdan intizom bilan natijaga chiqish!</b>\n\n"
                f"Ko'pchilik treydingda bitta bitim bilan boyib ketishni orzu qiladi va bor pulini yo'qotadi. "
                f"Lekin yaqinda bir treyder har bir bitimda atigi <b>1-2% risk</b> qilib, qat'iy reja bilan 3 oyda hisobini barqaror 40% ga o'stirdi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Treyding — bu omad o'yini emas, bu <b>ehtimollar va intizom</b> matematikasi.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da bepul trading darslarini tomosha qiling</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror strategiyani sinab ko'ring! Bugun qaysi juftlikni kuzatyapsiz?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>Treyding uchun eng zo'r BEPUL asboblar:</b>\n\n"
                f"Grafik va bozor tahlili uchun qimmat dasturlar shart emas. Bepul vositalardan unumli foydalaning:\n\n"
                f"1. <b>TradingView</b> — Eng kuchli grafiklar, indikatorlar va tahlil maydoni.\n"
                f"2. <b>CoinMarketCap / CoinGecko</b> — Kriptovalyutalar kapitallashuvi va likvidlik ko'rsatkichlari.\n"
                f"3. <b>CryptoPanic</b> — Eng qaynoq bozor yangiliklari bitta tasmasida.\n"
                f"4. <b>Notion / Google Sheets</b> — Xatolarni tahlil qilish uchun shaxsiy savdo jurnali.\n\n"
                f"🎥 <b>Qo'llanma:</b> <a href=\"{yt_url}\">YouTube'da instrumentlardan foydalanish darslari</a>\n\n"
                f"💡 <i>Siz ko'proq qaysi platformada tahlil qilasiz?</i> 🚀"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: Treyding bo'yicha YouTube'dagi oltin tavsiyalar!</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"Treydingda muvaffaqiyat qozonish uchun shoshilmang, avval bilim oling! ✨\n\n"
                f"📌 <b>O'rganish kerak bo'lgan eng zarur mavzular:</b>\n"
                f"• <code>Texnik tahlil va shamchalar (Candlestick) signallari</code>\n"
                f"• <code>Risk va Money Management (Kapitalni asrash)</code>\n"
                f"• <code>Treyding psixologiyasi va FOMO bilan kurashish</code>\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da ushbu mavzular bo'yicha darsliklarni ko'rish</a>\n\n"
                f"<i>Kuniga 30 daqiqa amaliyot qilsangiz, bir oyda natijangiz mutlaqo boshqacha bo'ladi!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Mantiqiy savol: Qaysi treyder oxir-oqibat g'olib bo'ladi?</b>\n\n"
                f"Tasavvur qiling, bozorda 2 xil treyder bor:\n"
                f"A) Bitimlarining 80%i to'g'ri chiqadi, lekin Stop-loss qo'ymaydi.\n"
                f"B) Bitimlarining atigi 45%i to'g'ri chiqadi, lekin har doim 1:3 risk/reward va qat'iy Stop-loss bilan ishlaydi.\n\n"
                f"Sizningcha, 6 oydan keyin qaysi biri daromadda bo'ladi? O'z javobingizni izohda yozib qoldiring! 👇"
            ),
            "funny_memes": (
                f"😂 <b>Treyder hayotidan qiziqarli lahza:</b>\n\n"
                f"— Grafikka 3 soat tikilib «Hozir uchadi» deb sotib oldim: darhol pastga quladi! 📉\n"
                f"— Chidolmay sotsam: raketadek tepaga uchib ketdi! 🚀\n\n"
                f"Tanish holatmi? Bu bozorning odatiy sinovi! Muhimi — emotsiyaga berilmaslik va sabr qilish 🧘‍♂️"
            ),
            "expert_guide": (
                f"💎 <b>Ekspert Maslahati: Treydingda depozitni saqlab qolishning 3 oltin qoidasi</b>\n\n"
                f"1. <b>Bitta bitimga butun depozitni tikmang</b> — Har qanday sharoitda 1-2% risk qoidasiga amal qiling.\n"
                f"2. <b>Stop-loss'siz bozorga kirmang</b> — Qabul qilinadigan zararni oldindan belgilang.\n"
                f"3. <b>FOMO (ortda qolish qo'rquviga) uchmang</b> — O'tib ketgan imkoniyat ortidan quvmang, yangisi albatta keladi.\n\n"
                f"🎥 <b>Tavsiya:</b> <a href=\"{yt_url}\">YouTube'da amaliy master-klass darslarini ko'rish</a>"
            )
        }
        
        poll_q_list = [
            "Siz treydingda eng ko'p qaysi uslubni qo'llaysiz? 📊",
            "Treydingda siz uchun eng katta qiyinchilik nima? 🤔",
            "Bozorda risk-menejmentga (1-2% risk) qat'iy amal qilasizmi? ⚖️"
        ]
        poll_opts_list = [
            ["Spot savdo (Uzoq muddat) 📈", "Scalping va tezkor savdo ⚡️", "Texnik tahlil va Smart Money 🧠", "Hali o'rganish bosqichidaman 📚"],
            ["Emotsiyani jilovlash (Sabr) 🧘‍♂️", "To'g'ri kirish nuqtasi topish 🎯", "Stop-loss intizomi ⏳", "Bozor tahlili 📉"],
            ["Ha, har bir bitimda riskni hisoblayman ✅", "Ba'zida emotsiyaga berilaman 😅", "Yo'q, stop-loss ishlatmayman ❌", "O'rganishim kerak 📝"]
        ]

    # 2. IT / Botlar / Web / Dasturlash
    elif any(k in n_lower for k in ["bot", "sayt", "dastur", "python", "web", "kod", "developer", "it", "xosting"]):
        yt_query = "telegram bot yaratish va python darslari noldan"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = "modern high-tech software engineer workstation dual monitors glowing code python terminal ambient neon lighting 8k resolution"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: Qanday qilib oddiy Telegram bot yaratib sotildi?</b>\n\n"
                f"Ko'pchilik katta dasturchi bo'lmasdan turib daromadli loyiha qilib bo'lmaydi deb o'ylaydi. "
                f"Lekin yaqinda bir yosh dasturchi oddiy <i>restoran buyurtma boti</i>ni yasab, bir nechta kafelarga "
                f"oylik obuna sifatida taqdim etdi va barqaror daromadga chiqdi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Katta narsani kutib o'tirmang! Hatto oddiy, lekin odamlar dardini yengillatadigan "
                f"kichik loyiha ham katta imkoniyatlar eshigini ochadi.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da bot yaratish bo'yicha bepul darsliklar</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring! Sizda qanday loyiha g'oyasi bor?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>Telegram botlarni qayerga TEKIN xostingga qo'yish mumkin?</b>\n\n"
                f"Kompyuteringizni 24/7 yoqib o'tirish shart emas. Botingizni bepul serverlarga joylashtiring:\n\n"
                f"1. <b>Render.com</b> — Background Worker yoki Web Service orqali 24/7 bepul ishlatish mumkin.\n"
                f"2. <b>PythonAnywhere.com</b> — Python botlar va skriptlar uchun eng qulay bepul xosting.\n"
                f"3. <b>Railway.app & Koyeb</b> — Tezkor va zamonaviy konteyner xostinglari.\n"
                f"4. <b>GitHub</b> — Loyihangiz kodini xavfsiz saqlash va Renderga ulash uchun.\n\n"
                f"🎥 <b>Ko'rsatma:</b> <a href=\"{yt_url}\">YouTube'da bepul serverga bot yuklash darslari</a>\n\n"
                f"💡 <i>Boshlanishiga ushbu platformalar sizga 100% yetadi. Sinab ko'rganmisiz?</i> 🚀"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: YouTube'dan nimalarni o'rganish kerak?</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"Internet — bepul bilimlar xazinasi! Vaqtingizni bekorchi videolarga emas, o'z ustingizda ishlashga sarflang ✨\n\n"
                f"📌 <b>Tavsiya qilinadigan qidiruv mavzulari:</b>\n"
                f"• <code>Telegram bot yaratish noldan aiogram 3</code>\n"
                f"• <code>Python dasturlash asoslari to'liq kurs</code>\n"
                f"• <code>Web-sayt yaratish va internet loyihalar</code>\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da ushbu bepul darsliklarni tomosha qiling</a>\n\n"
                f"<i>Kuniga atigi 30 daqiqa video ko'rib amaliyot qilsangiz, bir oyda natijangizni tanimay qolasiz!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Dasturchilar va foydalanuvchilar uchun qiziqarli mantiqiy topishmoq:</b>\n\n"
                f"Kodni qanchalik tez o'qiysiz?\n"
                f"<code>x = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x))</code>\n\n"
                f"Ekranga qanday natija chiqadi: <b>3</b>mi yoki <b>4</b>? Izohda o'z javobingizni qoldiring! 💡"
            ),
            "funny_memes": (
                f"😂 <b>Dasturchilar hayotidan kulgili holat:</b>\n\n"
                f"— «Dasturim kompyuterimda ideal ishlayotgan edi!»\n"
                f"— Serverga yuklaganimdan so'ng: <i>Error 500 / Crash</i> 😅\n\n"
                f"Haqiqiy dasturchi xatolardan qo'rqmaydi, loglarni o'qib darhol tuzatadi! Har qanday xato yangi tajriba beradi 🚀"
            ),
            "expert_guide": (
                f"💎 <b>Professional Maslahat: Qanday qilib birinchi IT loyihani sotish mumkin?</b>\n\n"
                f"1. <b>Atrofdagi muammolarni qidiring</b> — Do'kon, kafe yoki kurslar uchun buyurtma/ro'yxatdan o'tish boti qilib bering.\n"
                f"2. <b>Bepul namuna qiling</b> — «Sizga bepul 3 kun sinov boti ulab beraman, yoqsa davom ettirasiz» deng.\n"
                f"3. <b>Soddalikni saqlang</b> — Murakkab funksiyalardan ko'ra, tez va aniq ishlaydigan yechim qadrliroq.\n\n"
                f"🎥 <b>Tavsiya:</b> <a href=\"{yt_url}\">YouTube'da loyiha yaratish darslari</a>"
            )
        }
        
        poll_q_list = [
            "Kanalimizda navbatdagi amaliy dars qaysi mavzuda bo'lsin? 🚀",
            "Botingiz yoki veb-saytingizni bepul xostingga qo'yganmisiz? 🌐",
            "Siz ko'proq qaysi yo'nalishga qiziqasiz? 💻"
        ]
        poll_opts_list = [
            ["Telegram botlar va Python 🤖", "Web-saytlar va Frontend 💻", "Sun'iy intellekt (AI) integratsiyasi ⚡️", "Real keyslar va sotish sirlari 💡"],
            ["Ha, Render/PythonAnywhere ishlataman ✅", "Yo'q, hali bilmayman 🤔", "Kompyuterimda ishlaydi 💻", "Tekin xosting qo'llanmasi kerak 📝"],
            ["Backend va botlar ⚙️", "Frontend va saytlar 🎨", "AI va Data Science 🧠", "IT loyihalarni sotish va biznes 💰"]
        ]

    # 3. Biznes / Startap / Savdo
    elif any(k in n_lower for k in ["biznes", "startap", "savdo", "marketing", "mijoz", "sotuv", "daromad"]):
        yt_query = "biznes boshlash va daromad sirlari noldan"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = "modern minimalist business executive office glass windows city skyline confident entrepreneur working on laptop 8k"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: 0 so'm xarajat bilan xizmat ko'rsatish biznesini boshlash!</b>\n\n"
                f"Ko'pchilik biznes uchun katta mablag' kerak deb boshlashni kechiktiradi. "
                f"Lekin bir tadbirkor shunchaki o'z sohasidagi mijozlarni topib, pudratchi mutaxassislar bilan "
                f"bog'lash (vositachilik) orqali hech qanday boshlang'ich pulsiz daromadli biznes modelini yo'lga qo'ydi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Eng muhimi kapital emas, balki <b>mijoz topish va muammosini hal qilish</b> qobiliyati.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da biznes va startap darslari</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring! Sizda qanday biznes g'oya bor?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>Biznes va startap uchun eng foydali BEPUL vositalar:</b>\n\n"
                f"1. <b>Notion & Trello</b> — Loyihalarni, rejalarni va vazifalarni tizimli boshqarish.\n"
                f"2. <b>Canva</b> — Sifatli reklama bannerlari va taqdimotlar tayyorlash.\n"
                f"3. <b>Google Sheets & Forms</b> — Mijozlar bazasi va so'rovnomalarni qulay yuritish.\n"
                f"4. <b>Telegram kanallar va guruhlar</b> — Bepul organik auditoriya to'plash maydoni.\n\n"
                f"🎥 <b>Qo'llanma:</b> <a href=\"{yt_url}\">YouTube'da biznes asboblari bo'yicha darslar</a>"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: Biznes va marketing sirlarini o'rganing!</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"Zamonaviy dunyoda eng qimmat ko'nikma — bu sotish, marketing va muzokara olib borishdir! ✨\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da biznes va savdoni rivojlantirish darslari</a>\n\n"
                f"<i>Kuniga bittadan amaliy keysni tahlil qiling va o'z faoliyatingizga tadbiq eting!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Biznes topishmoq: Qaysi mahsulot ko'proq sotiladi?</b>\n\n"
                f"A) Narxi juda arzon, lekin hech kim bilmaydigan zo'r mahsulot.\n"
                f"B) Narxi o'rtacha, lekin aniq auditoriya muammosini hal qiluvchi kuchli reklama qilingan mahsulot.\n\n"
                f"Sizningcha, uzoq muddatda qaysi biri bozor yetakchisiga aylanadi? Izohda fikringizni yozing! 💡"
            ),
            "funny_memes": (
                f"😂 <b>Biznesdagi kulgili hayotiy holat:</b>\n\n"
                f"— «Ertadan yangi loyihani boshlayman, biznes reja tayyor!»\n"
                f"— 3 oydan so'ng: Hali ham biznes rejani mukammallashtirib o'tiribdi... 😅\n\n"
                f"Mukammallikni kutmang, hoziroq harakatni boshlang! Haqiqiy natija faqat amaliyotda keladi 🚀"
            ),
            "expert_guide": (
                f"💎 <b>Ekspert Maslahati: Yangi loyihada mijozlarni jalb qilishning 3 qoidasi</b>\n\n"
                f"1. <b>Mijoz dardiga e'tibor bering</b> — Mahsulotingiz emas, u beradigan foyda sotadi.\n"
                f"2. <b>Ochiq muloqot va ishonch</b> — Mijozlar bilan samimiy munosabat o'rnating.\n"
                f"3. <b>Doimiy sinov (A/B testing)</b> — Har xil takliflarni sinab ko'ring va eng yaxshisini tanlang."
            )
        }
        
        poll_q_list = [
            "Biznes yoki startap boshlashda siz uchun eng katta to'siq nima? 💡",
            "Mijozlarni jalb qilishda eng samarali usul qaysi deb o'ylaysiz? 📢",
            "Shaxsiy loyihangizni qachon ishga tushirmoqchisiz? 🚀"
        ]
        poll_opts_list = [
            ["Boshlang'ich kapital topish 💰", "Mijozlarni jalb qilish (Marketing) 📢", "To'g'ri g'oya tanlash 🎯", "Jamoa va intizom ⏳"],
            ["Telegram va ijtimoiy tarmoqlar 📱", "Tavsiyalar (Sarafan marketing) 🤝", "Target va to'g'ridan-to'g'ri reklama 🎯", "Shaxsiy brend va blog yuritish ✨"],
            ["Hozirda faol ishlayapman 🔥", "Shu oy ichida boshlayman 🚀", "Hali rejalashtiryapman 🤔", "G'oya qidiryapman 💡"]
        ]

    # 4. Universal / Boshqa sohalar
    else:
        yt_query = f"{niche} o'rganish va amaliy darslar"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = f"high quality editorial photograph inspiring atmosphere related to {niche} clean natural lighting 8k resolution"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: «{html.escape(niche)}» sohasida qanday qilib yuqori natijaga erishildi?</b>\n\n"
                f"Ko'pchilik katta natijalarga birdaniga erishiladi deb o'ylaydi. Lekin tajriba shuni ko'rsatadiki, "
                f"har kuni qo'yilgan kichik va qat'iy qadamlar bir necha oy ichida aqlbovar qilmas natijalarni beradi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Doimiylik va to'g'ri yo'nalish har qanday murakkab maqsadni osonlashtiradi.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da bepul darslik va amaliyotlarni ko'rish</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring! Sizning maqsadingiz nima?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>«{html.escape(niche)}» yo'nalishida eng foydali BEPUL asboblar:</b>\n\n"
                f"1. <b>Internet platformalari</b> — Bepul onlayn kurslar va tahliliy maqolalar.\n"
                f"2. <b>Zamonaviy ilovalar</b> — Kundalik amaliyot va jarayonlarni soddalashtiruvchi vositalar.\n"
                f"3. <b>YouTube video darsliklari</b> — Dunyoning eng tajribali mutaxassislari tajribalari.\n\n"
                f"🎥 <b>Qo'llanma:</b> <a href=\"{yt_url}\">YouTube'da ushbu mavzu bo'yicha darslar</a>"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: YouTube'dagi eng foydali yo'nalishlar!</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"O'z sohangizda mutaxassis bo'lish uchun har kuni yangi bilim o'rganishdan to'xtamang! ✨\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da ushbu yo'nalish darsliklarini tomosha qiling</a>\n\n"
                f"<i>Kuniga 30 daqiqa video ko'rib amaliyot qilsangiz, tez orada natijalar o'z so'zini aytadi!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Auditoriya uchun qiziqarli savol va fikr almashish:</b>\n\n"
                f"«{html.escape(niche)}» yo'nalishida sizningcha eng muhimi nima:\n"
                f"A) Chuqur nazariy bilim va qoidalar.\n"
                f"B) Har kuni tinimsiz amaliyot qilish va tajriba orttirish.\n\n"
                f"O'z fikringizni izohlarda yozib qoldiring! 👇"
            ),
            "funny_memes": (
                f"😂 <b>Hayotiy kulgili haqiqat:</b>\n\n"
                f"— «Hamma narsani rejalashtirib qo'ydim, dushanbadan 100% boshlayman!»\n"
                f"— Dushanba kuni: <i>Yana bir haftadan boshlasamchi?</i> 😅\n\n"
                f"Dushanbani kutmang, hoziroq birinchi qadamni tashlang! Harakatda baraka bor 🚀"
            ),
            "expert_guide": (
                f"💎 <b>Professional Maslahat: Muvaffaqiyatga eltuvchi 3 oltin qoida</b>\n\n"
                f"1. <b>Doimiylik (Intizom)</b> — Har kuni kichik bo'lsa ham amaliyot qiling.\n"
                f"2. <b>Sifatli manbalar</b> — Eng ishonchli va toza ma'lumotlarni o'rganing.\n"
                f"3. <b>Xatolar bilan ishlash</b> — Xatodan xulosa chiqarib, yanada kuchliroq davom eting.\n\n"
                f"🎥 <b>Tavsiya:</b> <a href=\"{yt_url}\">YouTube'da foydali darsliklarni ko'rish</a>"
            )
        }
        
        poll_q_list = [
            f"«{html.escape(title)}» kanalimizda qaysi mavzuni ko'proq yoritishimizni xohlaysiz? 💡",
            f"«{html.escape(niche[:40])}» yo'nalishida hozirgi darajangiz qanday? 📊",
            "Kanalimizdagi navbatdagi post qanday formatda bo'lsin? ✨"
        ]
        poll_opts_list = [
            ["Amaliy qo'llanmalar 📚", "Real muvaffaqiyat keyslari 💡", "Bepul vositalar va tavsiyalar 🛠", "Video darsliklar tavsiyasi 🎥"],
            ["Endi boshlayapman (Boshlovchi) 🌱", "O'rtacha darajada bilaman 📈", "Tajribam yetarli (Professional) 🎯", "Shunchaki qiziqaman 👀"],
            ["Batafsil mini-qo'llanma 📝", "Qiziqarli keys / Tarix 💡", "Mantiqiy savol / O'yin 🎮", "Foydali video tavsiya 🎥"]
        ]

    post_text = posts.get(theme) or posts.get("expert_guide") or list(posts.values())[0]
    idx = random.randint(0, len(poll_q_list) - 1)
    poll_q = poll_q_list[idx]
    poll_opts = poll_opts_list[idx]
    
    return {
        "post_text": post_text,
        "image_prompt": img_prompt,
        "poll_question": poll_q,
        "poll_options": poll_opts
    }


async def get_recent_chat_insights(channel_id):
    rows = q("SELECT user_name, text FROM chat_insights WHERE channel_id=? ORDER BY id DESC LIMIT 15", (channel_id,))
    if not rows:
        return "Guruhda hali alohida savollar yozilmagan."
    return "\n".join(f"- {r['user_name'] or 'A`zo'}: {r['text']}" for r in rows)


def extract_post_from_text(raw_text: str, c: dict, fallback_theme: str):
    """
    Gemini javobidan xatosiz post matni va rasm promptini ajratib oladi.
    JSON buzilgan bo'lsa ham matnni 100% saqlab qoladi!
    """
    txt = raw_text.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"```$", "", txt).strip()

    # JSON qidirish
    json_match = re.search(r"(\{.*\})", txt, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if isinstance(data, dict) and data.get("post_text"):
                return data
        except Exception:
            pass

    # Agar JSON bo'lmasa, toza matnni post sifatida olish
    clean = re.sub(r"[\{\}\"']", "", txt).strip()
    if len(clean) > 80:
        adaptive = generate_niche_adaptive_post_and_poll(c, fallback_theme)
        return {
            "post_text": clean,
            "image_prompt": adaptive.get("image_prompt", ""),
            "poll_question": adaptive.get("poll_question", ""),
            "poll_options": adaptive.get("poll_options", [])
        }
    
    # AI javob bermasa -> Kanal mavzusiga 100% mos generator
    return generate_niche_adaptive_post_and_poll(c, fallback_theme)


async def ai_generate_full(c):
    insights = await get_recent_chat_insights(c["channel_id"])
    recent = [r["text"][:150] for r in q(
        "SELECT text FROM recent_posts WHERE channel_id=? ORDER BY created_at DESC LIMIT 5", (c["channel_id"],))]
    
    theme = random.choice(POST_THEMES)
    style = random.choice(IMAGE_STYLES)
    
    theme_instructions = {
        "real_cases": "Mavzu: REAL KEYS VA MUVAFFAQIYAT! Shu kanal mavzusida birov qanday qilib natijaga/daromadga chiqqani, qanday loyiha qilgani haqida hikoya. Obunachilarni ham yangilik yaratishga chorla: «Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring!».",
        "free_tools": "Mavzu: BEPUL INSTRUMENTLAR VA VOSITALAR! Shu soha bo'yicha tekin dasturlar, xostinglar, platformalar va vositalar haqida aniq, foydali tavsiyalar.",
        "youtube_learning": "Mavzu: YOUTUBE VA INTERNET DARSLIKLARI! Xuddi onadek/ustozdek mehribonlik bilan, obunachilar ko'proq narsa o'rganishi uchun YouTube'dagi qidirish kerak bo'lgan eng foydali darslar va bilim manbalarini tavsiya qil.",
        "interactive_game": "Mavzu: QIZIQARLI O'YIN VA TOPISHMOQ! Auditoriya bilan shu soha bo'yicha mantiqiy o'yin, qiziqarli topishmoq yoki test o'tkaz.",
        "funny_memes": "Mavzu: KULGILI HAYOTIY MEM VA VOQEA! Shu sohadagi qiziqarli hayotiy holat, mutaxassislar uchraydigan kulgili voqea va kayfiyat ko'taruvchi gaplar.",
        "expert_guide": "Mavzu: AMALIY EKSPERT MASLAHATI! Foydalanuvchining kanal mavzusi bo'yicha eng zarur, qimmatli va amaliy qo'llanma."
    }
    
    encoded_search = urllib.parse.quote(f"{c['niche']} darslari")
    yt_example = f"https://www.youtube.com/results?search_query={encoded_search}"
    
    prompt = f"""Sen «{c['title']}» kanalining eng iqtidorli, jonkuyar va professional bosh muharririsan.
Kanal yo'nalishi: {c['niche']}
Auditoriya: {c['audience']}

AI UCHUN ASOSIY YO'RIQNOMA (PROMPT):
{c['detailed_prompt'] or "Kanal mavzusiga mos eng foydali va qiziqarli narsalarni yoritish."}

BU POST UCHUN TANLANGAN MAVZU:
👉 {theme_instructions.get(theme)}

Oxirgi postlar (takrorlanmasin):
{" | ".join(recent) if recent else "Yo'q"}

Guruhdagi muhokamalar:
{insights}

MUHIM TALABLAR:
1. O'zbek tilida, nihoyatda samimiy, onadek g'amxo'r, rag'batlantiruvchi va jonli tilda yoz (400-800 belgi).
2. Diqqatni birinchi soniyada tortadigan kuchli sarlavha (Hook).
3. Matn shriftlari qalin (<b>...</b>), muhim jumlalar (<i>...</i>), parametr va kodlar (<code>...</code>).
4. Emojilarga boy va jozibador (🚀, 💡, 🛠, 🎥, 🎮, 😂, 💎, ⚡️).
5. Post ichiga mavzuga doir YouTube qidiruv/darslik havolasini qo'sh: masalan: <a href="{yt_example}">🎥 YouTube'da ushbu darslikni tomosha qiling</a>
6. poll_question: Aynan «{c['title']}» kanali mavzusi ({c['niche']}) bo'yicha 1 ta o'ta qiziq so'rovnoma savoli (maksimal 200 belgi).
7. poll_options: Ushbu savolga mos 3-4 ta takrorlanmas variant.
8. image_prompt: Ingliz tilida, ushbu uslubda yoz: "{style}".

Javobni FAQAT toza JSON formatida qaytar:
{{
  "post_text": "HTML formatidagi post matni",
  "image_prompt": "english detailed prompt for image generation",
  "poll_question": "So'rovnoma savoli",
  "poll_options": ["Variant 1", "Variant 2", "Variant 3"]
}}
"""
    client = get_gemini_client()
    models_to_try = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest", "gemini-3.8-flash", "gemini-3-flash-preview"]
    for m in models_to_try:
        try:
            r = await client.aio.models.generate_content(model=m, contents=prompt)
            raw = (r.text or "").strip()
            if raw:
                extracted = extract_post_from_text(raw, c, theme)
                if extracted and extracted.get("post_text"):
                    return extracted
        except Exception as e:
            logging.warning(f"AI generatsiya urinishi ({m}) muvaffaqiyatsiz: {e}")
            await asyncio.sleep(0.3)

    # Agar barcha modellar xato bersa -> Niche-Adaptive generator
    return generate_niche_adaptive_post_and_poll(c, theme)


# ============================ XILMA-XIL RASMLAR YARATISH VA YUKLASH ============================
async def download_post_image(prompt: str, niche: str):
    mode = random.choice(["flux_ai", "web_photo"])
    
    # 1. Flux AI orqali yaratish
    if mode == "flux_ai":
        try:
            seed = random.randint(1000, 99999999)
            encoded = urllib.parse.quote(prompt[:300])
            url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&model=flux&seed={seed}&nologo=true"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 8000:
                            return data
        except Exception as e:
            logging.warning(f"Flux AI rasmda kechikish: {e}")

    # 2. Internetdan yuqori aniqlikdagi haqiqiy fotosurat
    img_urls = [
        "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1280&q=80",
        "https://images.unsplash.com/photo-1498050108023-c5249f4df085?auto=format&fit=crop&w=1280&q=80",
        "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?auto=format&fit=crop&w=1280&q=80",
        "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?auto=format&fit=crop&w=1280&q=80",
        "https://images.unsplash.com/photo-1504384308090-c894fdcc538d?auto=format&fit=crop&w=1280&q=80",
        "https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1280&q=80",
        f"https://picsum.photos/1280/720?random={random.randint(1, 9999)}"
    ]
    random.shuffle(img_urls)
    for u in img_urls[:3]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(u, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 8000:
                            return data
        except Exception:
            continue
    return None


# ============================ KANALGA POST YUBORISH (100% KAFOLATLI) ============================
async def send_ai_post(c, with_image=True):
    """
    Postni 100% kanalga yetkazadi.
    Qaytaradi: (bool ok, str message)
    """
    data = await ai_generate_full(c)
    post_text = data.get("post_text", "").strip()
    if not post_text:
        adaptive = generate_niche_adaptive_post_and_poll(c)
        post_text = adaptive["post_text"]
        
    img_prompt = data.get("image_prompt", "")
    sent = False
    error_detail = ""

    # Rasm bilan yuborish
    if with_image:
        try:
            img_bytes = await download_post_image(img_prompt or c["niche"], c["niche"])
            if img_bytes:
                photo_file = types.BufferedInputFile(img_bytes, filename="post_image.jpg")
                try:
                    await bot.send_photo(c["channel_id"], photo=photo_file, caption=post_text)
                    sent = True
                except Exception as e_cap:
                    # Caption 1024 belgidan oshgan bo'lsa yoki HTML xatosi bo'lsa
                    clean_caption = re.sub(r"<[^>]+>", "", post_text)[:1020]
                    await bot.send_photo(c["channel_id"], photo=photo_file, caption=clean_caption, parse_mode=None)
                    sent = True
        except Exception as e_img:
            logging.warning(f"Rasm bilan yuborishda xato: {e_img}, matn ko'rinishida yuboriladi...")

    # Agar rasm bilan ketmagan bo'lsa, oddiy matn sifatida yuborish
    if not sent:
        try:
            await bot.send_message(c["channel_id"], post_text)
            sent = True
        except Exception as e1:
            try:
                clean_text = re.sub(r"<[^>]+>", "", post_text)
                await bot.send_message(c["channel_id"], clean_text, parse_mode=None)
                sent = True
            except Exception as e2:
                error_detail = str(e2)
                logging.error(f"Post mutlaqo yuborilmadi ({c['channel_id']}): {e2}")

    if sent:
        q("UPDATE channels SET posts_sent = posts_sent + 1 WHERE channel_id=?", (c["channel_id"],), commit=True)
        q("INSERT INTO recent_posts VALUES (?,?,?)", (c["channel_id"], post_text, now_utc().isoformat()), commit=True)
        auto_backup_channels()
        preview = post_text[:350] + ("..." if len(post_text) > 350 else "")
        return True, (
            f"🎉 <b>Post «{html.escape(c['title'])}» kanaliga muvaffaqiyatli yuborildi!</b>\n\n"
            f"📌 <b>Kanal yo'nalishi:</b> {html.escape(c['niche'])}\n\n"
            f"📝 <b>Post matni:</b>\n{preview}"
        )
    else:
        return False, f"❌ <b>Xatolik yuz berdi:</b> <code>{error_detail or 'Telegram kanalda bot huquqini tekshiring'}</code>"


# ============================ KAFOLATLANGAN INTERAKTIV SO'ROVNOMA (POLL) ============================
async def send_ai_poll(c):
    question = None
    options = []
    
    data = await ai_generate_full(c)
    if data and data.get("poll_question") and data.get("poll_options"):
        raw_q = str(data["poll_question"]).strip()
        raw_opts = [str(opt).strip() for opt in data["poll_options"] if str(opt).strip()]
        unique_opts = []
        for o in raw_opts:
            if o not in unique_opts:
                unique_opts.append(o[:95])
        if len(unique_opts) >= 2:
            question = raw_q[:250]
            options = unique_opts[:10]
            
    if not question or len(options) < 2:
        adaptive = generate_niche_adaptive_post_and_poll(c)
        question = adaptive["poll_question"]
        options = adaptive["poll_options"]
        
    try:
        try:
            await bot.send_poll(
                chat_id=c["channel_id"],
                question=question,
                options=options,
                is_anonymous=False,
            )
        except Exception:
            await bot.send_poll(
                chat_id=c["channel_id"],
                question=question,
                options=options,
                is_anonymous=True,
            )
        q("UPDATE channels SET polls_sent = polls_sent + 1 WHERE channel_id=?", (c["channel_id"],), commit=True)
        auto_backup_channels()
        
        opts_formatted = "\n".join(f"{i+1}. {opt}" for i, opt in enumerate(options))
        success_msg = (
            f"📊 <b>Interaktiv so'rovnoma «{html.escape(c['title'])}» kanaliga muvaffaqiyatli yuborildi!</b>\n\n"
            f"📌 <b>Kanal yo'nalishi:</b> {html.escape(c['niche'])}\n\n"
            f"❓ <b>Savol:</b> {html.escape(question)}\n\n"
            f"🔘 <b>Variantlar:</b>\n{html.escape(opts_formatted)}"
        )
        return True, success_msg
    except Exception as e:
        logging.error(f"Kanalga poll yuborishda xatolik: {e}")
        return False, f"❌ <b>So'rovnoma yuborilmadi:</b> <code>{e}</code>"


# ============================ TEZKOR TUGMALAR HANDLERLARI ============================
@dp.callback_query(F.data.startswith("imgpost:"))
async def on_click_imgpost(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = own_channel(cid, cb.from_user.id)
    if not c:
        return await cb.answer("Kanal topilmadi")
    plan, _ = get_user_plan(cb.from_user.id)
    if not plan:
        return await cb.answer("Tarifingiz faol emas!", show_alert=True)
    
    await cb.answer("⏳ Professional rasm va post tayyorlanmoqda (bir necha soniya)...")
    ok, msg = await send_ai_post(c, with_image=True)
    await cb.message.answer(msg)


@dp.callback_query(F.data.startswith("post:"))
async def on_click_post(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = own_channel(cid, cb.from_user.id)
    if not c:
        return await cb.answer("Kanal topilmadi")
    plan, _ = get_user_plan(cb.from_user.id)
    if not plan:
        return await cb.answer("Tarif faol emas!", show_alert=True)
    
    await cb.answer("⏳ Post tayyorlanmoqda...")
    ok, msg = await send_ai_post(c, with_image=False)
    await cb.message.answer(msg)


@dp.callback_query(F.data.startswith("poll:"))
async def on_click_poll(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = own_channel(cid, cb.from_user.id)
    if not c:
        return await cb.answer("Kanal topilmadi")
    plan, _ = get_user_plan(cb.from_user.id)
    if not plan:
        return await cb.answer("Tarif faol emas!", show_alert=True)
    
    await cb.answer("⏳ So'rovnoma tayyorlanmoqda...")
    ok, msg = await send_ai_poll(c)
    await cb.message.answer(msg)


# ============================ AVTO JADVAL VA SCHEDULER ============================
async def delayed_action(c, action_type):
    await asyncio.sleep(random.randint(10, 300))
    if action_type == "poll":
        await send_ai_poll(c)
    else:
        await send_ai_post(c, with_image=True)


async def scheduled_tasks():
    current_hour = datetime.now(TZ).hour
    for c in q("SELECT * FROM channels WHERE active=1"):
        plan, _ = get_user_plan(c["owner_id"])
        if not plan:
            continue
            
        allowed_hours = []
        if c["custom_hours"]:
            try:
                allowed_hours = [int(x.strip()) for x in c["custom_hours"].split(",") if x.strip()]
            except Exception:
                pass
        if not allowed_hours:
            allowed_hours = PLANS[plan]["hours"]
            
        if current_hour in allowed_hours:
            action = "poll" if random.random() < 0.3 else "post"
            asyncio.create_task(delayed_action(c, action))


async def expiry_reminders():
    now = now_utc()
    for u in q("SELECT user_id, plan, expires_at FROM users WHERE plan IS NOT NULL AND expires_at IS NOT NULL"):
        left = datetime.fromisoformat(u["expires_at"]) - now
        if timedelta(0) < left <= timedelta(days=1) or timedelta(days=2) < left <= timedelta(days=3):
            try:
                await bot.send_message(
                    u["user_id"],
                    f"⏳ <b>Eslatma:</b> Sizning <b>{PLANS[u['plan']]['name']}</b> tarifingiz <b>{fmt(datetime.fromisoformat(u['expires_at']))}</b> da tugaydi.\n"
                    "Uzluksiz postlar va xizmatlar uchun «💎 Tariflar & To'lov» bo'limi orqali uzaytirishingiz mumkin."
                )
            except Exception:
                pass


# ============================ XUSHYOR GURUH MODERATSIYASI VA SAMIMIY JAVOBLAR ============================
async def delete_later(msg: types.Message, sec=20):
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except Exception:
        pass


async def handle_violation(m: types.Message, chat_id: int, reason_text: str):
    uid = m.from_user.id
    mention = m.from_user.mention_html()
    
    try:
        await m.delete()
    except Exception:
        pass
        
    r = q("SELECT warnings, cycle FROM violations WHERE chat_id=? AND user_id=?", (chat_id, uid), one=True)
    warns, cycle = (r["warnings"], r["cycle"]) if r else (0, 0)
    warns += 1

    # Guruh ogohlantirishlar sonini yangilash
    q("UPDATE channels SET warnings = warnings + 1 WHERE group_id=?", (chat_id,), commit=True)

    if warns < 3:
        q("INSERT OR REPLACE INTO violations VALUES (?,?,?,?)", (chat_id, uid, warns, cycle), commit=True)
        warn_msg = await m.answer(
            f"⚠️ {mention}, <b>{reason_text}!</b>\n"
            f"📌 Sizga ogohlantirish berildi: <code>{warns}/3</code>\n"
            f"<i>(Ushbu ogohlantirish 20 soniyada o'chiriladi)</i>"
        )
        asyncio.create_task(delete_later(warn_msg, 20))
        return

    # Guruh cheklovlari va banlar sonini yangilash
    q("UPDATE channels SET bans = bans + 1 WHERE group_id=?", (chat_id,), commit=True)
    try:
        if cycle == 0:
            q("INSERT OR REPLACE INTO violations VALUES (?,?,?,?)", (chat_id, uid, 0, 1), commit=True)
            await bot.restrict_chat_member(
                chat_id, uid,
                permissions=types.ChatPermissions(can_send_messages=False),
                until_date=now_utc() + timedelta(days=30)
            )
            punish_msg = await m.answer(f"⛔ {mention} <b>3 marta ogohlantirildi va 30 kunga yozishdan cheklandi!</b>")
        else:
            await bot.ban_chat_member(chat_id, uid)
            q("DELETE FROM violations WHERE chat_id=? AND user_id=?", (chat_id, uid), commit=True)
            punish_msg = await m.answer(f"🚫 {mention} <b>qoidani surunkali buzgani sababli guruhdan chiqarib yuborildi!</b>")
        asyncio.create_task(delete_later(punish_msg, 20))
    except Exception as e:
        logging.error(f"Foydalanuvchini cheklashda xato: {e}")


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_handler(m: types.Message):
    if not m.from_user or m.from_user.is_bot or not m.text:
        return
        
    text_content = m.text.strip()
    chat_id = m.chat.id
    
    # Guruhdagi xabarlar statistikasini yangilash
    q("UPDATE channels SET group_messages = group_messages + 1 WHERE group_id=?", (chat_id,), commit=True)
    
    if await is_admin_of(chat_id, m.from_user.id):
        return

    # So'kinish yoki haqorat
    if PROFANITY_RE.search(text_content):
        return await handle_violation(m, chat_id, "guruhda so'kinish, haqorat va kamsitish taqiqlanadi")
        
    # Firibgarlik (masheniklik) va spam
    if SCAM_RE.search(text_content):
        return await handle_violation(m, chat_id, "guruhda firibgarlik, spam va noqonuniy reklamalar qat'iyan man etiladi")
    
    c = q("SELECT * FROM channels WHERE group_id=? AND active=1", (chat_id,), one=True)
    channel_id = c["channel_id"] if c else 0
    
    if len(text_content) > 5 and not text_content.startswith("/"):
        q("INSERT INTO chat_insights (channel_id, user_id, user_name, text, created_at) VALUES (?,?,?,?,?)",
          (channel_id, m.from_user.id, m.from_user.full_name, text_content[:300], now_utc().isoformat()), commit=True)
    
    me = await bot.me()
    replied = m.reply_to_message and m.reply_to_message.from_user and m.reply_to_message.from_user.id == me.id
    mentioned = f"@{me.username.lower()}" in text_content.lower()
    
    if replied or mentioned:
        clean_query = re.sub(f"@{me.username}", "", text_content, flags=re.I).strip()
        ai_prompt = (
            f"Sen ushbu guruhning oliy toifali, juda xushmuomala ekspert yordamchisisan.\n"
            f"Kanal yo'nalishi: {c['niche'] if c else 'Dasturlash, texnologiya va internet loyihalari'}.\n\n"
            "Qoidalar:\n"
            "- O'zbek tilida, do'stona, qisqa (2-4 gap) va aniq foydali javob ber.\n"
            "- Shriftlarni chiroyli formatla: qalin <b>...</b>, kodlar <code>...</code>.\n"
            "- O'zingni AI deb atama, mutaxassis sifatida gapir.\n\n"
            f"Foydalanuvchi savoli: {clean_query}"
        )
        try:
            client = get_gemini_client()
            r = await client.aio.models.generate_content(model=GEMINI_MODEL, contents=ai_prompt)
            ans = (r.text or "").strip()
            if ans:
                await m.reply(ans)
        except Exception as e:
            logging.error(f"Guruhda AI javob xatosi: {e}")
            await m.reply("Savolingiz uchun katta rahmat! 🤝 Tez orada bu bo'yicha batafsil ma'lumot beramiz ✨")
    else:
        gratitude_responses = [
            f"Fikringiz uchun katta rahmat, <b>{html.escape(m.from_user.first_name)}</b>! 🤝 Ushbu mavzuni kanalimizda ham tez orada yoritamiz ✨",
            f"Ajoyib fikr! 💡 Buni albatta inobatga olamiz, rahmat!",
            f"Savol va qiziqishingiz uchun rahmat! 🚀 Kanalimizni kuzatib boring, yangi foydali ma'lumotlar tayyorlanmoqda.",
            f"Fikr almashganingiz uchun minnatdormiz! 🤝 Bilim ulashishda davom etamiz."
        ]
        if any(w in text_content.lower() for w in ["qanday", "qaysi", "nima", "kerak", "maslahat", "yordam", "fikr", "loyiha", "bot", "sayt"]):
            await m.reply(random.choice(gratitude_responses))


# ============================ KANAL QO'SHILISH HODISALARI ============================
@dp.chat_join_request()
async def on_join_request(r: types.ChatJoinRequest):
    c = q("SELECT 1 FROM channels WHERE channel_id=?", (r.chat.id,), one=True)
    if not c:
        return
    q("UPDATE channels SET join_requests = join_requests + 1 WHERE channel_id=?", (r.chat.id,), commit=True)
    try:
        await r.approve()
    except Exception as e:
        logging.warning(f"So'rovni tasdiqlab bo'lmadi: {e}")


@dp.chat_member()
async def on_chat_member(e: types.ChatMemberUpdated):
    if not q("SELECT 1 FROM channels WHERE channel_id=?", (e.chat.id,), one=True):
        return
    old, new = e.old_chat_member.status, e.new_chat_member.status
    if old in ("left", "kicked") and new in ("member", "restricted"):
        q("UPDATE channels SET joined = joined + 1 WHERE channel_id=?", (e.chat.id,), commit=True)
    elif old in ("member", "restricted") and new in ("left", "kicked"):
        q("UPDATE channels SET left_count = left_count + 1 WHERE channel_id=?", (e.chat.id,), commit=True)


# ============================ ADMIN PANEL ============================
AdminMsg = F.from_user.id.in_(ADMIN_IDS)


@dp.message(Command("admin"), F.chat.type == "private", AdminMsg)
async def admin_panel(m: types.Message, state: FSMContext):
    await state.clear()
    card_num, card_own = get_card_info()
    g_key = get_gemini_api_key()
    masked_g = (g_key[:6] + "..." + g_key[-4:]) if len(g_key) > 10 else (g_key or "Kiritilmagan")
    await m.answer(
        "🛠 <b>BOSH ADMIN BOSHQARUV PANELI</b>\n\n"
        f"💳 <b>Hozirgi karta:</b> <code>{card_num}</code> ({html.escape(card_own)})\n"
        f"🔑 <b>Gemini AI Kalit:</b> <code>{masked_g}</code>\n\n"
        "<i>Quyidagi bo'limlardan birini tanlang:</i>",
        reply_markup=ik(
            [("📊 Umumiy Statistika", "adm:stats"), ("💳 Kutilayotgan To'lovlar", "adm:pay")],
            [("💳 Karta Sozlamalari", "adm:card"), ("🔑 Gemini AI Kaliti", "adm:gemini")],
            [("👥 Foydalanuvchini boshqarish", "adm:user"), ("📺 Barcha Kanallar", "adm:channels")],
            [("💾 Baza Zaxirasi (Backup)", "adm:backup"), ("📣 Reklama Yuborish (Broadcast)", "adm:bcast")],
            [("🔒 Majburiy Kanallar", "adm:req")],
        )
    )


# --- KARTA SOZLAMALARI ---
@dp.callback_query(AdminMsg, F.data == "adm:card")
async def adm_card(cb: types.CallbackQuery):
    card_num, card_own = get_card_info()
    await cb.message.answer(
        "💳 <b>KARTA SOZLAMALARI:</b>\n\n"
        f"🔢 <b>Hozirgi karta raqami:</b> <code>{card_num}</code>\n"
        f"👤 <b>Karta egasi:</b> <b>{html.escape(card_own)}</b>\n\n"
        "<i>O'zgartirish uchun kerakli tugmani bosing 👇</i>",
        reply_markup=ik(
            [("✏️ Karta raqamini o'zgartirish", "adm:setcardnum")],
            [("✏️ Karta egasini o'zgartirish", "adm:setcardowner")],
            [("🔙 Orqaga", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:setcardnum")
async def adm_setcardnum(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminCard.number)
    await cb.message.answer("💳 <b>Yangi karta raqamini yuboring</b> (masalan: <code>8600 1234 5678 9999</code>):")
    await cb.answer()


@dp.message(AdminCard.number, AdminMsg, F.text)
async def adm_savecardnum(m: types.Message, state: FSMContext):
    new_num = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('card_number', ?)", (new_num,), commit=True)
    await state.clear()
    await m.answer(f"✅ <b>Karta raqami muvaffaqiyatli saqlandi:</b> <code>{new_num}</code>", reply_markup=MENU)


@dp.callback_query(AdminMsg, F.data == "adm:setcardowner")
async def adm_setcardowner(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminCard.owner)
    await cb.message.answer("👤 <b>Yangi karta egasining ism-familiyasini yuboring</b> (masalan: <code>Ali Valiyev</code>):")
    await cb.answer()


@dp.message(AdminCard.owner, AdminMsg, F.text)
async def adm_savecardowner(m: types.Message, state: FSMContext):
    new_owner = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('card_owner', ?)", (new_owner,), commit=True)
    await state.clear()
    await m.answer(f"✅ <b>Karta egasi saqlandi:</b> <b>{html.escape(new_owner)}</b>", reply_markup=MENU)


# --- GEMINI AI KALITI SOZLAMALARI ---
@dp.callback_query(AdminMsg, F.data == "adm:gemini")
async def adm_gemini(cb: types.CallbackQuery):
    key = get_gemini_api_key()
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else (key or "Kiritilmagan")
    await cb.message.answer(
        "🔑 <b>GEMINI AI KALITI VA SOZLAMALARI:</b>\n\n"
        f"🗝 <b>Joriy kalit:</b> <code>{masked}</code>\n"
        f"🤖 <b>Asosiy model:</b> <code>{GEMINI_MODEL}</code>\n\n"
        "<i>Quyidagi amallardan birini tanlang 👇</i>",
        reply_markup=ik(
            [("🧪 Kalitni sinab ko'rish (Test)", "adm:testgemini")],
            [("✏️ Yangi Gemini kalit kiritish", "adm:setgemini")],
            [("🔙 Orqaga", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:testgemini")
async def adm_testgemini(cb: types.CallbackQuery):
    await cb.answer("⏳ Gemini AI tekshirilmoqda...")
    ok, msg = await test_gemini_key()
    if ok:
        await cb.message.answer(f"✅ <b>Gemini AI to'liq ishlamoqda!</b>\n\n{msg}")
    else:
        await cb.message.answer(
            f"❌ <b>Gemini AI ulanmadi!</b>\n\n"
            f"Sababi: <code>{msg}</code>\n\n"
            "💡 <i>Eslatma: Haqiqiy bepul Gemini API kalitini https://aistudio.google.com/app/apikey "
            "saytidan bepul olishingiz mumkin (odatda «AIzaSy...» bilan boshlanadi).</i>"
        )


@dp.callback_query(AdminMsg, F.data == "adm:setgemini")
async def adm_setgemini(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminGemini.key)
    await cb.message.answer(
        "🔑 <b>Yangi Gemini API kalitini yuboring:</b>\n\n"
        "Uni https://aistudio.google.com/app/apikey manzilidan bepul olishingiz mumkin "
        "(odatda <code>AIzaSy...</code> bilan boshlanadi).\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(AdminGemini.key, AdminMsg, F.text)
async def adm_savegemini(m: types.Message, state: FSMContext):
    new_key = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (new_key,), commit=True)
    await state.clear()
    
    ok, msg = await test_gemini_key(new_key)
    if ok:
        await m.answer(f"🎉 <b>Yangi Gemini API kaliti saqlandi va tekshirildi!</b>\n\n{msg}", reply_markup=MENU)
    else:
        await m.answer(
            f"⚠️ <b>Kalit saqlandi, lekin Google xato qaytardi:</b>\n<code>{msg}</code>\n\n"
            "Iltimos, kalit to'g'riligini tekshiring (bepul olish: https://aistudio.google.com/app/apikey).",
            reply_markup=MENU
        )


# --- BAZA ZAXIRASI (BACKUP / RESTORE) ---
@dp.callback_query(AdminMsg, F.data == "adm:backup")
async def adm_backup_menu(cb: types.CallbackQuery):
    count = q("SELECT COUNT(*) c FROM channels", one=True)["c"]
    await cb.message.answer(
        "💾 <b>BAZA ZAXIRASI (BACKUP & RESTORE):</b>\n\n"
        f"Render serveri qayta ishga tushganda kanallar o'chib ketmasligi uchun zaxira nusxasini saqlab qo'yishingiz mumkin.\n"
        f"📊 <b>Bazadagi joriy kanallar:</b> <b>{count} ta</b>\n\n"
        "<i>Quyidagi amallardan birini tanlang:</i>",
        reply_markup=ik(
            [("📥 Zaxira faylini yuklab olish (Eksport)", "adm:download_backup")],
            [("📤 Zaxira faylidan tiklash (Import)", "adm:restore_backup")],
            [("🔙 Orqaga", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:download_backup")
async def adm_download_backup(cb: types.CallbackQuery):
    auto_backup_channels()
    if os.path.exists(BACKUP_FILE):
        doc = types.FSInputFile(BACKUP_FILE, filename=f"channels_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
        await bot.send_document(cb.from_user.id, document=doc, caption="💾 <b>Ulangan barcha kanallarning zaxira nusxasi!</b>")
    else:
        await cb.message.answer("⚠️ Hozircha bazada kanallar mavjud emas.")
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:restore_backup")
async def adm_restore_backup(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminRestore.file)
    await cb.message.answer("📤 <b>Iltimos, avval yuklab olingan zaxira (.json) faylini shu yerga yuboring:</b>\n\nBekor qilish: /cancel")
    await cb.answer()


@dp.message(AdminRestore.file, AdminMsg, F.document)
async def adm_process_restore_file(m: types.Message, state: FSMContext):
    file_info = await bot.get_file(m.document.file_id)
    await bot.download_file(file_info.file_path, BACKUP_FILE)
    restored_count = auto_restore_channels()
    await state.clear()
    await m.answer(f"🎉 <b>Baza muvaffaqiyatli tiklandi!</b> Jami <b>{restored_count} ta</b> kanal qayta tiklandi.", reply_markup=MENU)


@dp.callback_query(AdminMsg, F.data == "adm:back")
async def adm_back_btn(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    card_num, card_own = get_card_info()
    g_key = get_gemini_api_key()
    masked_g = (g_key[:6] + "..." + g_key[-4:]) if len(g_key) > 10 else (g_key or "Kiritilmagan")
    await cb.message.edit_text(
        "🛠 <b>BOSH ADMIN BOSHQARUV PANELI</b>\n\n"
        f"💳 <b>Hozirgi karta:</b> <code>{card_num}</code> ({html.escape(card_own)})\n"
        f"🔑 <b>Gemini AI Kalit:</b> <code>{masked_g}</code>\n\n"
        "<i>Quyidagi bo'limlardan birini tanlang:</i>",
        reply_markup=ik(
            [("📊 Umumiy Statistika", "adm:stats"), ("💳 Kutilayotgan To'lovlar", "adm:pay")],
            [("💳 Karta Sozlamalari", "adm:card"), ("🔑 Gemini AI Kaliti", "adm:gemini")],
            [("👥 Foydalanuvchini boshqarish", "adm:user"), ("📺 Barcha Kanallar", "adm:channels")],
            [("💾 Baza Zaxirasi (Backup)", "adm:backup"), ("📣 Reklama Yuborish (Broadcast)", "adm:bcast")],
            [("🔒 Majburiy Kanallar", "adm:req")],
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:stats")
async def adm_stats(cb: types.CallbackQuery):
    users = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    now = now_utc().isoformat()
    by_plan = {k: q("SELECT COUNT(*) c FROM users WHERE plan=? AND expires_at>?", (k, now), one=True)["c"] for k in PLANS}
    income = q("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved'", one=True)["s"]
    pending = q("SELECT COUNT(*) c FROM payments WHERE status='pending'", one=True)["c"]
    ch = q("SELECT COUNT(*) c, COALESCE(SUM(posts_sent),0) p, COALESCE(SUM(polls_sent),0) pl FROM channels", one=True)
    await cb.message.answer(
        "📊 <b>BOTNING TO'LIQ STATISTIKASI:</b>\n\n"
        f"👤 <b>Jami foydalanuvchilar:</b> <b>{users} ta</b>\n"
        + "\n".join(f"💎 {PLANS[k]['name']} tarifi: <b>{v} ta</b>" for k, v in by_plan.items())
        + f"\n\n💳 <b>Kutilayotgan to'lovlar:</b> <b>{pending} ta</b>\n"
        f"💰 <b>Jami tasdiqlangan tushum:</b> <code>{income:,} so'm</code>\n".replace(",", " ")
        + f"📺 <b>Ulangan kanallar:</b> <b>{ch['c']} ta</b>\n"
        f"📝 <b>Yuborilgan AI postlar:</b> <b>{ch['p']} ta</b>\n"
        f"📊 <b>Yuborilgan so'rovnomalar:</b> <b>{ch['pl']} ta</b>"
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:pay")
async def adm_pay(cb: types.CallbackQuery):
    rows = q("SELECT * FROM payments WHERE status='pending' ORDER BY id LIMIT 10")
    if not rows:
        await cb.message.answer("✅ <b>Hozirda kutilayotgan to'lovlar mavjud emas.</b>")
    for p in rows:
        await bot.send_photo(
            cb.from_user.id, p["file_id"],
            caption=f"💳 <b>To'lov #{p['id']}</b>\nFoydalanuvchi: <code>{p['user_id']}</code>\nTarif: <b>{PLANS[p['plan']]['name']}</b> — <code>{p['amount']:,} so'm</code>".replace(",", " "),
            reply_markup=ik([("✅ Tasdiqlash", f"pay_ok:{p['id']}"), ("❌ Rad etish", f"pay_no:{p['id']}")]))
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:channels")
async def adm_channels(cb: types.CallbackQuery):
    rows = q("SELECT * FROM channels ORDER BY posts_sent DESC LIMIT 25")
    text = "📺 <b>Barcha Ulangan Kanallar:</b>\n\n" + ("\n".join(
        f"{'🟢' if r['active'] else '⏸'} <b>{html.escape(r['title'])}</b> | Egasi: <code>{r['owner_id']}</code> | Guruh: <code>{r['group_id'] or 'Yoq'}</code> | Post: {r['posts_sent']} | So'rov: {r['polls_sent']}"
        for r in rows) or "Kanal mavjud emas.")
    await cb.message.answer(text)
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:user")
async def adm_user(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminUser.uid)
    await cb.message.answer("👤 Boshqarmoqchi bo'lgan foydalanuvchining <b>Telegram ID raqami</b>ni yuboring:")
    await cb.answer()


def user_admin_card(uid):
    u = q("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    if not u:
        return None, None
    plan, exp = get_user_plan(uid)
    n = q("SELECT COUNT(*) c FROM channels WHERE owner_id=?", (uid,), one=True)["c"]
    text = (f"👤 <b>Foydalanuvchi:</b> {html.escape(u['full_name'] or '')} (<code>{uid}</code>)\n"
            f"🔗 @{u['username'] or 'Yo`q'}\n"
            f"💎 <b>Tarif:</b> {PLANS[plan]['name'] + ' (' + fmt(exp) + ' gacha)' if plan else 'Yo`q'}\n"
            f"📺 <b>Kanallar soni:</b> {n} ta\n"
            f"⚙️ <b>Holat:</b> {'🚫 Bloklangan' if u['banned'] else '✅ Faol'}")
    kb = ik(
        [(f"🎁 {p['name']} (30 kun)", f"ug:{uid}:{k}") for k, p in PLANS.items()],
        [("❌ Tarifni bekor qilish", f"urev:{uid}"), ("🚫 Bloklash / ✅ Ochish", f"uban:{uid}")],
    )
    return text, kb


@dp.message(AdminUser.uid, AdminMsg, F.text)
async def adm_user_find(m: types.Message, state: FSMContext):
    if not m.text.strip().lstrip("-").isdigit():
        return await m.answer("Faqat raqam yuboring.")
    text, kb = user_admin_card(int(m.text.strip()))
    if not text:
        return await m.answer("❌ Foydalanuvchi topilmadi.")
    await state.clear()
    await m.answer(text, reply_markup=kb)


@dp.callback_query(AdminMsg, F.data.startswith(("ug:", "urev:", "uban:")))
async def adm_user_action(cb: types.CallbackQuery):
    parts = cb.data.split(":")
    uid = int(parts[1])
    if parts[0] == "ug":
        apply_plan(uid, parts[2])
        try:
            await bot.send_message(uid, f"🎁 <b>Tabriklaymiz!</b> Sizga admin tomonidan <b>{PLANS[parts[2]]['name']}</b> tarifi 30 kunga faollashtirildi.")
        except Exception:
            pass
    elif parts[0] == "urev":
        q("UPDATE users SET plan=NULL, expires_at=NULL WHERE user_id=?", (uid,), commit=True)
    else:
        q("UPDATE users SET banned = 1 - banned WHERE user_id=?", (uid,), commit=True)
    text, kb = user_admin_card(uid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Bajarildi!")


@dp.callback_query(AdminMsg, F.data == "adm:req")
async def adm_req(cb: types.CallbackQuery):
    rows = [r["chat"] for r in q("SELECT chat FROM required")]
    await cb.message.answer(
        "🔒 <b>Majburiy Obuna Kanallari:</b>\n\n" + ("\n".join(rows) or "Hozircha yo'q") +
        "\n\n➕ Qo'shish: <code>/addreq @kanal</code>\n➖ Olib tashlash: <code>/delreq @kanal</code>\n"
        "<i>Eslatma: Bot ushbu kanallarda admin bo'lishi shart!</i>")
    await cb.answer()


@dp.message(Command("addreq"), AdminMsg)
async def add_req(m: types.Message):
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].startswith("@"):
        return await m.answer("Format: <code>/addreq @kanal</code>")
    q("INSERT OR IGNORE INTO required VALUES (?)", (parts[1],), commit=True)
    await m.answer(f"✅ <b>{parts[1]} majburiy obuna ro'yxatiga qo'shildi.</b>")


@dp.message(Command("delreq"), AdminMsg)
async def del_req(m: types.Message):
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Format: <code>/delreq @kanal</code>")
    q("DELETE FROM required WHERE chat=?", (parts[1],), commit=True)
    await m.answer("🗑 <b>Majburiy kanallardan olib tashlandi.</b>")


# ---- Reklama yuborish (Broadcast) ----
@dp.callback_query(AdminMsg, F.data == "adm:bcast")
async def adm_bcast(cb: types.CallbackQuery):
    await cb.message.answer(
        "📣 <b>Reklama xabari kimlarga yuborilsin?</b>",
        reply_markup=ik(
            [("👥 Foydalanuvchilarga", "bc:users"), ("📺 Kanallarga", "bc:channels")],
            [("🌐 Barchasiga (User + Kanal)", "bc:both")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data.startswith("bc:") & (F.data != "bc:go"))
async def bc_target(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Bcast.content)
    await state.update_data(target=cb.data.split(":")[1])
    await cb.message.answer("📝 <b>Reklama xabaringizni yuboring</b> (Matn, rasm, video, havola — istalgan turdagi):")
    await cb.answer()


@dp.message(Bcast.content, AdminMsg)
async def bc_content(m: types.Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    d = await state.get_data()
    nu = q("SELECT COUNT(*) c FROM users WHERE banned=0", one=True)["c"]
    nc = q("SELECT COUNT(*) c FROM channels", one=True)["c"]
    await m.answer(
        f"📣 <b>Yuborish tasdiqlansinmi?</b>\n\n"
        f"🎯 <b>Nishon:</b> <code>{d['target']}</code>\n"
        f"👥 Foydalanuvchilar: <b>{nu} ta</b>\n"
        f"📺 Kanallar: <b>{nc} ta</b>",
        reply_markup=ik([("✅ Ha, yuborilsin", "bc:go"), ("❌ Bekor qilish", "bc:cancel")])
    )


@dp.callback_query(AdminMsg, F.data == "bc:go")
async def bc_go(cb: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("msg_id"):
        return await cb.answer("Xabar topilmadi", show_alert=True)
    await state.clear()
    targets = []
    if d["target"] in ("users", "both"):
        targets += [r["user_id"] for r in q("SELECT user_id FROM users WHERE banned=0")]
    if d["target"] in ("channels", "both"):
        targets += [r["channel_id"] for r in q("SELECT channel_id FROM channels")]
    
    await cb.message.answer(f"🚀 <b>Reklama yuborish boshlandi:</b> <code>{len(targets)} ta</code> manzil...")
    ok = fail = 0
    for t in targets:
        try:
            await bot.copy_message(t, d["chat_id"], d["msg_id"])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await cb.message.answer(f"✅ <b>Yuborish yakunlandi!</b>\nYetkazildi: <b>{ok} ta</b>\nXatolik: <b>{fail} ta</b>")
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "bc:cancel")
async def bc_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("❌ Reklama bekor qilindi.")
    await cb.answer()


# ============================ ISHGA TUSHIRISH ============================
async def run_render_web_server():
    port_str = os.getenv("PORT")
    if port_str:
        try:
            port = int(port_str)
            async def handle_ping(request):
                return web.Response(text="Bot is running smoothly!")

            app = web.Application()
            app.router.add_get("/", handle_ping)
            app.router.add_get("/health", handle_ping)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            logging.info(f"Render Health Check server {port}-portda ishga tushdi.")
        except Exception as e:
            logging.warning(f"Health server xatosi: {e}")


async def main():
    await run_render_web_server()
    scheduler.add_job(scheduled_tasks, "cron", minute=0)
    scheduler.add_job(expiry_reminders, "cron", hour=10, minute=0)
    scheduler.start()
    logging.info(f"Bot to'liq ishga tushdi! Model: {GEMINI_MODEL}, Admin: {ADMIN_IDS}")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
