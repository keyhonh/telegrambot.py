# -*- coding: utf-8 -*-
"""
Keyhon boti — yangilangan versiya.

Muhim:
- Bot faqat o'zbek tilida ishlaydi (til bo'limi olib tashlangan).
- Barcha vaqtlar Toshkent vaqti (UTC+5) bo'yicha.
- Muhit o'zgaruvchilari: BOT_TOKEN, OWNER_ID (avvalgidek).
"""
import asyncio
import html
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from io import BytesIO

import matplotlib
matplotlib.use("Agg")  # server muhitida ekransiz ishlash uchun
import matplotlib.pyplot as plt

from flask import Flask

from telegram import (
    Update,
    Poll,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ConversationHandler,
    ContextTypes,
    PollAnswerHandler,
    filters,
)

# ----------------------------------------------------------------------
# SOZLAMALAR
# ----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")    # <-- o'z Telegram ID raqamingiz

# Majburiy obuna kanallari. Bot shu kanal(lar)da ADMIN bo'lishi shart.
# id: kanal username'i ("@kanalim") yoki -100... ko'rinishidagi ID
REQUIRED_CHANNELS = [
    {"id": "@keyhon", "title": "📢 Asosiy kanal", "url": "https://t.me/keyhon"},
    # Kerak bo'lsa yana qo'shishingiz mumkin:
    {"id": "-1003860693315", "title": "📢 Ikkinchi kanal", "url": "https://t.me/+3Fz0sY6URQNiNWVi"},
]

DB_PATH = "admin_panel.db"

# Toshkent vaqti (UTC+5, yozgi vaqtga o'tish yo'q)
TZ = timezone(timedelta(hours=5))


def now_tk() -> datetime:
    """Toshkent vaqti (tzinfo'siz) — bazaga yozish va solishtirish uchun."""
    return datetime.now(TZ).replace(tzinfo=None)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Tugmalar matni (o'zgartirmang — kod shu matnlarni tekshiradi)
BTN_POST = "📢 Post yuborish"
BTN_CATEGORIES = "📂 Bo'limlar"
BTN_ADMINS = "👤 Adminlar"
BTN_STATS = "📊 Statistika"
BTN_USERS = "🚫 Foydalanuvchilar"
BTN_HISTORY = "📜 Tarix"
BTN_BACKUP = "💾 Zaxira nusxa"
BTN_CONTACT = "📩 Keyhonga murojaat"
BTN_SEARCH = "🔍 Qidiruv"
BTN_FAVORITES = "⭐ Sevimlilar"
BTN_GAME = "🎮 Minecraft Viktorina"
BTN_LEADERBOARD = "🏆 Reyting"
BTN_STOP = "🔚 O'yinni to'xtatish"

# Viktorina darajalari
LEVELS = {
    "easy":   {"title": "Oson",  "emoji": "🟢", "count": 30, "seconds": 20},
    "medium": {"title": "O'rta", "emoji": "🟡", "count": 20, "seconds": 25},
    "hard":   {"title": "Qiyin", "emoji": "🔴", "count": 10, "seconds": 30},
}
LEVEL_ORDER = ["easy", "medium", "hard"]
MAX_IDLE = 3                 # ketma-ket shuncha savolga javob bermasa, o'yin to'xtaydi
NEXT_QUESTION_DELAY = 1.5    # to'g'ri javobni ko'rish uchun qisqa pauza (soniya)

# Conversation state'lari
(
    WAIT_POST_CONTENT,
    WAIT_POST_CONFIRM,
    WAIT_CAT_NAME,
    WAIT_CAT_CONTENT,
    WAIT_ADMIN_ID,
    WAIT_DEL_ADMIN_ID,
    WAIT_EDIT_CONTENT,
    WAIT_POST_TIME,
    WAIT_CONTACT_MESSAGE,
    WAIT_ADMIN_REPLY,
    WAIT_SEARCH_TERM,
    WAIT_BLOCK_USER_ID,
    WAIT_RESTORE_FILE,
) = range(13)


# ----------------------------------------------------------------------
# MA'LUMOTLAR BAZASI
# ----------------------------------------------------------------------
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT,
            last_active TEXT,
            subscribed INTEGER DEFAULT 1,
            language TEXT DEFAULT 'uz'
        )
    """)
    # Eski bazalarda ba'zi ustunlar bo'lmasligi mumkin — xavfsiz qo'shamiz
    for ddl in (
        "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'uz'",
        "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN subscribe_prompted INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN bot_blocked INTEGER DEFAULT 0",
    ):
        try:
            cur.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # ustun allaqachon mavjud
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            content_type TEXT,
            content_text TEXT,
            file_id TEXT,
            position INTEGER DEFAULT 0
        )
    """)
    try:
        cur.execute("ALTER TABLE categories ADD COLUMN position INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER,
            category_id INTEGER,
            PRIMARY KEY (user_id, category_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            detail TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category_id INTEGER,
            clicked_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            sent_at TEXT,
            total_sent INTEGER,
            total_failed INTEGER
        )
    """)
    # Viktorina: har bir o'yinchining har bir daraja bo'yicha eng past va eng yuqori foizi
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quiz_stats (
            user_id INTEGER,
            first_name TEXT,
            level TEXT,
            games INTEGER DEFAULT 0,
            min_pct INTEGER,
            max_pct INTEGER,
            PRIMARY KEY (user_id, level)
        )
    """)
    # Post yuboriladigan guruhlar (bot a'zo/admin bo'lgan guruhlar)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            chat_type TEXT,
            added_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            admin_chat_id INTEGER,
            from_chat_id INTEGER,
            message_id INTEGER,
            run_at TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    if OWNER_ID:
        cur.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (OWNER_ID, OWNER_ID, now_tk().isoformat()),
        )
        conn.commit()
    conn.close()


def db_query(query, params=(), fetch=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    result = None
    if fetch == "one":
        result = cur.fetchone()
    elif fetch == "all":
        result = cur.fetchall()
    conn.commit()
    conn.close()
    return result


def db_insert(query, params=()):
    """INSERT qiladi va yangi qator ID'sini qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def get_setting(key: str):
    row = db_query("SELECT value FROM settings WHERE key=?", (key,), fetch="one")
    return row[0] if row else None


def set_setting(key: str, value: str):
    db_query("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def upsert_user(user):
    now = now_tk().isoformat()
    existing = db_query("SELECT user_id FROM users WHERE user_id=?", (user.id,), fetch="one")
    if existing:
        # Obuna holatini har bir xabarda avtomatik 1 qilib yubormaymiz.
        # Uni faqat haqiqiy obuna tekshiruvi o'zgartiradi.
        db_query(
            "UPDATE users SET username=?, first_name=?, last_active=? WHERE user_id=?",
            (user.username, user.first_name, now, user.id),
        )
    else:
        db_query(
            "INSERT INTO users (user_id, username, first_name, joined_at, last_active, subscribed) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (user.id, user.username, user.first_name, now, now),
        )


def is_admin(user_id: int) -> bool:
    row = db_query("SELECT user_id FROM admins WHERE user_id=?", (user_id,), fetch="one")
    return row is not None


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_blocked(user_id: int) -> bool:
    row = db_query("SELECT blocked FROM users WHERE user_id=?", (user_id,), fetch="one")
    return bool(row and row[0])


def get_categories():
    return db_query("SELECT id, name FROM categories ORDER BY position, id", fetch="all")


def log_action(admin_id: int, action: str, detail: str = ""):
    db_query(
        "INSERT INTO audit_log (admin_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
        (admin_id, action, detail, now_tk().isoformat()),
    )


def display_name(user) -> str:
    """Foydalanuvchiga murojaat uchun ism: avval ism, bo'lmasa @username."""
    if user.first_name:
        return user.first_name
    if user.username:
        return "@" + user.username
    return "aziz foydalanuvchi"


# ----------------------------------------------------------------------
# MATNLAR (hammasi "siz" bilan, doimiy emojilar bilan)
# ----------------------------------------------------------------------
def welcome_text(name: str) -> str:
    return (
        f"👋 Assalomu alaykum, {name}!\n"
        f"Keyhon botiga xush kelibsiz 🌟\n\n"
        f"Kerakli bo'limni tanlang 👇"
    )


def admin_welcome_text(name: str) -> str:
    return (
        f"🛠 Assalomu alaykum, {name}!\n"
        f"Admin panelga xush kelibsiz 🌟\n\n"
        f"Boshqaruv tugmalari quyida 👇"
    )


SUBSCRIBE_PROMPT = (
    "🔒 Botdan foydalanish uchun avval quyidagi kanal(lar)ga obuna bo'ling, "
    "so'ng \"✅ Tekshirish\" tugmasini bosing 👇"
)
NOT_SUBSCRIBED_ALERT = "❌ Siz hali barcha kanallarga obuna bo'lmagansiz."
NO_SECTIONS_BUTTON = "ℹ️ Hozircha bo'limlar yo'q"
CHOOSE_BUTTON_TEXT = "Iltimos, quyidagi tugmalardan birini tanlang 👇"


# ----------------------------------------------------------------------
# MAJBURIY OBUNA
# ----------------------------------------------------------------------
async def get_not_subscribed(bot, user_id: int):
    if not REQUIRED_CHANNELS:
        return []
    missing = []
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            if member.status not in ("member", "administrator", "creator"):
                missing.append(ch)
        except Exception as e:
            logger.warning(f"Obuna tekshiruvida xato ({ch['id']}): {e}")
            missing.append(ch)
    return missing


def build_subscribe_keyboard(missing):
    keyboard = [[InlineKeyboardButton(ch["title"], url=ch["url"])] for ch in missing]
    keyboard.append([InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(keyboard)


async def send_subscribe_prompt(bot, chat_id, missing):
    await bot.send_message(
        chat_id,
        SUBSCRIBE_PROMPT,
        reply_markup=build_subscribe_keyboard(missing),
    )


async def ensure_subscription(bot, user_id: int, chat_id: int) -> bool:
    """
    Obunani tekshiradi, lekin obuna bo'lmagan foydalanuvchiga
    majburiy obuna xabarini faqat BIR MARTA yuboradi.

    Foydalanuvchi obuna bo'lgach, keyingi xabarlarida qayta so'ralmaydi.
    Agar keyinchalik kanaldan chiqsa, yana bir marta so'raladi.
    """
    missing = await get_not_subscribed(bot, user_id)

    row = db_query(
        "SELECT subscribed, subscribe_prompted FROM users WHERE user_id=?",
        (user_id,),
        fetch="one",
    )
    prompted = bool(row and row[1])

    if missing:
        db_query("UPDATE users SET subscribed=0 WHERE user_id=?", (user_id,))
        if not prompted:
            await send_subscribe_prompt(bot, chat_id, missing)
            db_query("UPDATE users SET subscribe_prompted=1 WHERE user_id=?", (user_id,))
        return False

    # Obuna tasdiqlandi.
    db_query("UPDATE users SET subscribed=1, subscribe_prompted=0 WHERE user_id=?", (user_id,))
    return True


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    missing = await get_not_subscribed(context.bot, query.from_user.id)
    if missing:
        await query.answer(NOT_SUBSCRIBED_ALERT, show_alert=True)
        return
    await query.answer("✅ Tekshirildi")
    try:
        await query.message.delete()
    except Exception:
        pass
    upsert_user(query.from_user)
    db_query(
        "UPDATE users SET subscribed=1, subscribe_prompted=0 WHERE user_id=?",
        (query.from_user.id,),
    )
    name = display_name(query.from_user)
    await context.bot.send_message(
        query.message.chat_id,
        f"✅ Rahmat, {name}! Endi botdan to'liq foydalanishingiz mumkin 🎉\n\nKerakli bo'limni tanlang 👇",
        reply_markup=build_user_menu(),
    )


# ----------------------------------------------------------------------
# REPLY KEYBOARD (PASTKI TUGMALAR)
# ----------------------------------------------------------------------
def _category_rows():
    rows = []
    row = []
    for cat_id, name in get_categories():
        row.append(KeyboardButton(name))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def build_user_menu():
    rows = _category_rows()
    if not rows:
        rows = [[KeyboardButton(NO_SECTIONS_BUTTON)]]
    rows.append([KeyboardButton(BTN_SEARCH), KeyboardButton(BTN_FAVORITES)])
    rows.append([KeyboardButton(BTN_GAME), KeyboardButton(BTN_LEADERBOARD)])
    rows.append([KeyboardButton(BTN_CONTACT)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_admin_menu():
    rows = _category_rows()
    # Oddiy foydalanuvchi funksiyalari — adminlar ham foydalana oladi
    rows.append([KeyboardButton(BTN_SEARCH), KeyboardButton(BTN_FAVORITES)])
    rows.append([KeyboardButton(BTN_GAME), KeyboardButton(BTN_LEADERBOARD)])
    rows.append([KeyboardButton(BTN_CONTACT)])
    # Faqat adminlar uchun boshqaruv tugmalari
    rows.append([KeyboardButton(BTN_POST), KeyboardButton(BTN_CATEGORIES)])
    rows.append([KeyboardButton(BTN_ADMINS), KeyboardButton(BTN_STATS)])
    rows.append([KeyboardButton(BTN_USERS), KeyboardButton(BTN_HISTORY)])
    rows.append([KeyboardButton(BTN_BACKUP)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def menu_for(user_id: int):
    return build_admin_menu() if is_admin(user_id) else build_user_menu()


# ----------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    db_query("UPDATE users SET bot_blocked=0 WHERE user_id=?", (user.id,))
    cleanup_session(context, user.id)  # /start bosilsa, tugallanmagan o'yin bekor qilinadi

    name = display_name(user)
    if is_admin(user.id):
        await update.message.reply_text(admin_welcome_text(name), reply_markup=build_admin_menu())
        return

    if is_blocked(user.id):
        return

    if not await ensure_subscription(context.bot, user.id, update.effective_chat.id):
        return

    await update.message.reply_text(welcome_text(name), reply_markup=build_user_menu())


# ----------------------------------------------------------------------
# FOYDALANUVCHI: BO'LIM TANLASH, SEVIMLILAR, QIDIRUV
# ----------------------------------------------------------------------
async def send_category_content(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, cat_id: int, name: str):
    row = db_query(
        "SELECT content_type, content_text, file_id FROM categories WHERE id=?",
        (cat_id,), fetch="one",
    )
    if not row:
        return
    content_type, content_text, file_id = row

    db_query(
        "INSERT INTO clicks (user_id, category_id, clicked_at) VALUES (?, ?, ?)",
        (user_id, cat_id, now_tk().isoformat()),
    )

    is_fav = bool(db_query(
        "SELECT 1 FROM favorites WHERE user_id=? AND category_id=?", (user_id, cat_id), fetch="one"
    ))
    fav_label = "💔 Sevimlilardan olib tashlash" if is_fav else "⭐ Sevimlilarga qo'shish"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(fav_label, callback_data=f"togglefav:{cat_id}")]])

    if content_type == "text":
        await context.bot.send_message(chat_id, content_text or "(matn kiritilmagan)", reply_markup=keyboard)
    elif content_type == "photo":
        await context.bot.send_photo(chat_id, photo=file_id, caption=content_text or "", reply_markup=keyboard)
    elif content_type == "document":
        await context.bot.send_document(chat_id, document=file_id, caption=content_text or "", reply_markup=keyboard)
    elif content_type == "video":
        await context.bot.send_video(chat_id, video=file_id, caption=content_text or "", reply_markup=keyboard)


async def toggle_favorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cat_id = int(query.data.split(":")[1])
    user_id = query.from_user.id

    existing = db_query(
        "SELECT 1 FROM favorites WHERE user_id=? AND category_id=?", (user_id, cat_id), fetch="one"
    )
    if existing:
        db_query("DELETE FROM favorites WHERE user_id=? AND category_id=?", (user_id, cat_id))
        await query.answer("💔 Sevimlilardan olib tashlandi")
        new_label = "⭐ Sevimlilarga qo'shish"
    else:
        db_query("INSERT OR IGNORE INTO favorites (user_id, category_id) VALUES (?, ?)", (user_id, cat_id))
        await query.answer("⭐ Sevimlilarga qo'shildi")
        new_label = "💔 Sevimlilardan olib tashlash"

    try:
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton(new_label, callback_data=f"togglefav:{cat_id}")]])
        )
    except Exception:
        pass


async def view_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split(":")[1])
    row = db_query("SELECT name FROM categories WHERE id=?", (cat_id,), fetch="one")
    name = row[0] if row else ""
    await send_category_content(context, query.message.chat_id, query.from_user.id, cat_id, name)


async def favorites_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = db_query(
        "SELECT c.id, c.name FROM favorites f JOIN categories c ON c.id = f.category_id "
        "WHERE f.user_id=? ORDER BY c.position",
        (user_id,), fetch="all",
    )
    if not rows:
        await update.message.reply_text(
            "⭐ Hozircha sevimli bo'limlaringiz yo'q.\n"
            "Biror bo'lim ostidagi \"⭐ Sevimlilarga qo'shish\" tugmasi orqali qo'shishingiz mumkin."
        )
        return
    keyboard = [[InlineKeyboardButton(name, callback_data=f"viewcat:{cid}")] for cid, name in rows]
    await update.message.reply_text("⭐ Sevimli bo'limlaringiz:", reply_markup=InlineKeyboardMarkup(keyboard))


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 Qidirmoqchi bo'lgan bo'lim nomini (yoki bir qismini) yozing:\n\nBekor qilish uchun /cancel"
    )
    return WAIT_SEARCH_TERM


async def search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = update.message.text.strip()
    user_id = update.effective_user.id
    rows = db_query(
        "SELECT id, name FROM categories WHERE name LIKE ? ORDER BY position",
        (f"%{term}%",), fetch="all",
    )
    if not rows:
        await update.message.reply_text(
            "😕 Afsuski, hech narsa topilmadi. Boshqa so'z bilan urinib ko'ring.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(name, callback_data=f"viewcat:{cid}")] for cid, name in rows]
    await update.message.reply_text(
        f"🔍 Topilgan natijalar ({len(rows)}):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 🎮 VIKTORINA SAVOLLARI — har bir darajada 50 tadan
# Format: (savol, TO'G'RI javob, noto'g'ri 1, noto'g'ri 2, noto'g'ri 3)
# Variantlar tartibi har safar tasodifiy aralashtiriladi.
# ----------------------------------------------------------------------
QUESTIONS = {
    "easy": [
        ("Creeper o'yinchiga yaqinlashganda nima qiladi?", "Portlaydi", "Kamon otadi", "Olov purkaydi", "Uchib ketadi"),
        ("Ishlab chiqarish stoli (Crafting Table) necha katakli?", "3x3", "2x2", "4x4", "5x5"),
        ("Kunduzi quyosh nurida qaysi mob yonib ketadi?", "Zombi", "Creeper", "Enderman", "O'rgimchak"),
        ("Karavot nima uchun kerak?", "Uxlab tunni o'tkazish uchun", "Tezroq yugurib ketish uchun", "Suvda uzoqroq suzish uchun", "Qazishni tezlashtirish uchun"),
        ("Sandiqda nima qilinadi?", "Buyumlar saqlanadi", "Ovqat pishiriladi", "Qurol yasaladi", "Mob chaqiriladi"),
        ("Sigirdan sut olish uchun nima kerak?", "Chelak", "Qilich", "Belkurak", "Kamon"),
        ("Qo'ydan jun olish uchun nima ishlatiladi?", "Qaychi", "Bolta", "Kilka", "Ketmon"),
        ("Tovuq qaysi buyumni tashlaydi?", "Tuxum", "Sut", "Jun", "Asal"),
        ("Skelet qanday qurol bilan hujum qiladi?", "Kamon", "Qilich", "Bolta", "Nayza"),
        ("Enderman qachon sizga hujum qiladi?", "Unga to'g'ri qaraganingizda", "Yomg'ir yog'ib turgan paytda", "Karavotda uxlab yotganingizda", "Sandiqni ochayotgan paytingizda"),
        ("Nether'ga o'tish uchun qaysi blokdan portal quriladi?", "Obsidian", "Tosh", "Temir", "Yog'och"),
        ("Yog'ochni qaysi asbob tezroq kesadi?", "Bolta", "Belkurak", "Qaychi", "Kamon"),
        ("Tuproq va qumni qaysi asbob tezroq qazadi?", "Belkurak", "Bolta", "Qilich", "Kamon"),
        ("Toshni qazish uchun qaysi asbob kerak?", "Kilka", "Belkurak", "Qaychi", "Ketmon"),
        ("O'yinchi sog'lig'i qanday ko'rsatiladi?", "Yuraklar bilan", "Olmoslar bilan", "Yulduzlar bilan", "Raqamlar bilan"),
        ("Ochlik darajasi qanday belgilar bilan ko'rsatiladi?", "Go'sht bo'laklari", "Yurak belgilari", "Olmos belgilari", "Yulduz belgilari"),
        ("Qaysi hayvonga egar solib minish mumkin?", "Otga", "Tovuqqa", "Qo'yga", "Quyonga"),
        ("TNT ishga tushganda nima bo'ladi?", "Portlaydi", "Muzlaydi", "Davolaydi", "O'sadi"),
        ("Mash'al nima beradi?", "Yorug'lik", "Cheksiz suv", "To'yimli ovqat", "Kuchli zirh"),
        ("Enderman suvga tegsa nima bo'ladi?", "Zarar oladi", "Kuchayadi", "Uxlab qoladi", "Hech narsa bo'lmaydi"),
        ("Minecraft'ni 2014-yilda qaysi kompaniya sotib olgan?", "Microsoft", "Google", "Sony", "Apple"),
        ("Minecraft o'yinini kim yaratgan?", "Markus Persson", "Bill Geyts", "Elon Mask", "Stiv Jobs"),
        ("Qaysi blokni Survival rejimida qazib bo'lmaydi?", "Bedrock", "Tosh", "Qum", "Tuproq"),
        ("Cho'lda eng ko'p qaysi o'simlik o'sadi?", "Kaktus", "Bug'doy", "Qovoq", "Qovun"),
        ("Kaktusga tegib ketsangiz nima bo'ladi?", "Zarar olasiz", "Davolanasiz", "Tezlashasiz", "Hech narsa bo'lmaydi"),
        ("Zirh nima uchun kerak?", "Zarardan himoyalanish uchun", "Tezroq yugurib ketish uchun", "Rudalarni tez qazish uchun", "Baland sakrab chiqish uchun"),
        ("Qaysi asbob materiali eng kuchli hisoblanadi?", "Netherit", "Olmos", "Temir", "Tosh"),
        ("Suv lava manbasiga tegsa nima hosil bo'ladi?", "Obsidian", "Qum", "Shisha", "Temir"),
        ("Non qanday yasaladi?", "3 ta bug'doydan", "3 ta sabzidan", "3 ta kartoshkadan", "3 ta tuxumdan"),
        ("Karavot yasash uchun nimalar kerak?", "Jun va taxta", "Temir va tosh", "Qum va shisha", "Olmos va ko'mir"),
        ("Temir golem nimadan yasaladi?", "Temir bloklari va qovoq", "Tosh bloklari va qovoq", "Oltin bloklari va qovoq", "Qum bloklari va qovoq"),
        ("Qor golemi nimadan yasaladi?", "Qor bloklari va qovoq", "Muz bloklari va qovoq", "Tosh bloklari va qovoq", "Qum bloklari va qovoq"),
        ("Overworld nima?", "Asosiy oddiy dunyo", "Do'zax dunyosi", "Osmon dunyosi", "Suv ostidagi dunyo"),
        ("Ender Dragon qaysi dunyoda yashaydi?", "The End", "Nether", "Overworld", "Okean tubida"),
        ("O'yinchi o'lsa, odatda qayerda qayta tug'iladi?", "Karavotda yoki boshlang'ich nuqtada", "Nether dunyosining o'rtasidagi qal'ada", "The End dunyosining markaziy orolida", "Okean tubidagi yodgorlik ichida"),
        ("Pichan to'plami nimadan yasaladi?", "Bug'doydan", "Qumdan", "Toshdan", "Taxtadan"),
        ("Pech (Furnace) nima uchun ishlatiladi?", "Eritish va pishirish uchun", "Uxlash va dam olish uchun", "Ekin ekish va sug'orish uchun", "Zirh kiyish va yechish uchun"),
        ("Ko'mir nima uchun ishlatiladi?", "Yoqilg'i va mash'al uchun", "Karavot yasash va bezash uchun", "Suv olish va saqlash uchun", "Zirh yasash va kiyish uchun"),
        ("Ko'pchilik bloklar bitta to'plamda nechta bo'ladi?", "64", "16", "32", "100"),
        ("Inventar qaysi tugma bilan ochiladi (Java)?", "E", "Q", "R", "F"),
        ("Shift tugmasi bosilsa nima bo'ladi?", "O'yinchi cho'kkalab yuradi", "O'yinchi uchib ketadi", "O'yinchi baland sakraydi", "O'yinchi tezroq yuguradi"),
        ("Qaysi hayvonni bug'doy bilan ko'paytirish mumkin?", "Sigirni", "Tovuqni", "Quyonni", "Itni"),
        ("Ari nima ishlab chiqaradi?", "Asal", "Sut", "Jun", "Un"),
        ("Bo'rini qo'lga o'rgatish uchun nima kerak?", "Suyak", "Sabzi", "Bug'doy", "Qum"),
        ("Mushukni qo'lga o'rgatish uchun nima kerak?", "Xom baliq", "Suyak", "Sabzi", "Bug'doy"),
        ("Oltin olma nimadan yasaladi?", "Olma va oltin", "Olma va temir", "Olma va olmos", "Olma va ko'mir"),
        ("Nether portalini yoqish uchun nima kerak?", "Chaqmoqtosh va po'lat", "Kamon va oddiy o'q", "Qaychi va uzun ip", "Qor to'pi va tuxum"),
        ("Qishloq aholisi bilan nima qilish mumkin?", "Savdo qilish", "Uchish", "Portlatish", "Uxlatish"),
        ("Steve va Alex kimlar?", "Asosiy qahramonlar", "Dushman mob turlari", "Maxsus blok turlari", "Kuchli qurol turlari"),
        ("Qizil tosh (Redstone) nima uchun kerak?", "Elektr sxemalari uchun", "Qurol yasash uchun", "Ovqat pishirish uchun", "Sehr o'qish uchun"),
    ],
    "medium": [
        ("Obsidianni qazish uchun kamida qaysi kilka kerak?", "Olmos kilka", "Temir kilka", "Tosh kilka", "Oltin kilka"),
        ("Blaze qaysi dunyoda uchraydi?", "Nether", "The End", "Overworld", "Okean tubida"),
        ("Ghast qanday hujum qiladi?", "Olov sharlari otadi", "Kamon bilan o'q otadi", "Zahar bulutini purkaydi", "Yaqin kelib portlab ketadi"),
        ("Piglin qaysi buyumga qiziqadi?", "Oltin", "Olmos", "Temir", "Zumrad"),
        ("Netherit qaysi rudadan olinadi?", "Ancient Debris", "Olmos rudasi", "Zumrad rudasi", "Lapis rudasi"),
        ("Elytra odatda qayerdan topiladi?", "End kemasidan", "Nether qal'asidan", "Qishloqdan", "Cho'l ibodatxonasidan"),
        ("Nether Star qaysi bossdan tushadi?", "Wither", "Ender Dragon", "Warden", "Elder Guardian"),
        ("Totem of Undying qaysi mobdan tushadi?", "Evoker", "Pillager", "Vindicator", "Witch"),
        ("Shulker'lar asosan qayerda uchraydi?", "End shaharlarida", "Nether qal'asida", "Okean yodgorligida", "Cho'l ibodatxonasida"),
        ("Sehrlash stolining kuchini oshirish uchun nima qo'yiladi?", "Kitob javonlari", "Temir bloklari", "Oltin bloklari", "Mash'allar"),
        ("Anvil (sandon) nima uchun kerak?", "Buyumlarni birlashtirish va nomlash", "Ovqatni pishirish va saqlash uchun", "Ekin ekish va ularni sug'orish", "Portal yasash va uni faollashtirish"),
        ("Mending sehri nima qiladi?", "Tajriba orqali buyumni ta'mirlaydi", "Zarar kuchini ancha oshiradi", "Yugurish tezligini oshirib beradi", "Suvda nafas olishni ta'minlaydi"),
        ("Silk Touch sehri nima uchun kerak?", "Blokni o'z holicha olish uchun", "Rudani tezroq qazib olish uchun", "Balandroq sakrab chiqish uchun", "Suv ustida yurib ketish uchun"),
        ("Fortune sehri nimani oshiradi?", "Rudadan tushadigan o'ljani", "Zirhning mustahkamligini", "O'yinchining tezligini", "Kamon otish masofasini"),
        ("Respiration sehri nima beradi?", "Suv ostida uzoq nafas olishni", "Suvda tezroq suzib yurishni", "Tunda atrofni yaxshi ko'rishni", "Olovdan to'liq himoyalanishni"),
        ("Repeater (takrorlagich) nima qiladi?", "Signalni kuchaytiradi va kechiktiradi", "Signalni butunlay to'xtatib qo'yadi", "Atrofni yoritib va isitib turadi", "Buyumlarni bir joydan boshqasiga tashiydi"),
        ("Piston nima qiladi?", "Bloklarni suradi", "Yorug'lik beradi", "Suv chiqaradi", "Buyum saqlaydi"),
        ("Observer bloki nimani sezadi?", "Oldidagi blok o'zgarishini", "Atrofdagi moblar harakatini", "O'yinchi yugurish tezligini", "Ob-havo o'zgarishini"),
        ("Hopper nima qiladi?", "Buyumlarni boshqa idishga tashiydi", "Ekinlarni avtomatik sug'oradi", "Moblarni bir joyda ushlab turadi", "Atrofni tunda yoritib turadi"),
        ("Iksir tayyorlash stolining yoqilg'isi nima?", "Blaze kukuni", "Ko'mir", "Redstone", "Ender ko'zi"),
        ("Nether Wart nima uchun ishlatiladi?", "Iksir tayyorlashda asos sifatida", "Qurol yasashda asosiy material", "Uy qurishda qurilish materiali", "Suvni tozalashda filtr sifatida"),
        ("Oltin olma qanday ta'sir beradi?", "Sog'liq tiklanadi va qo'shimcha yurak beriladi", "Tezlik va baland sakrash ta'siri beriladi", "O'yinchi bir necha soniya ko'rinmas bo'ladi", "Olov va lavadan to'liq himoya beriladi"),
        ("Fire Resistance iksiri nimadan himoya qiladi?", "Olov va lavadan", "Suv va namlikdan", "Zahar va kasallikdan", "Yiqilish zarbasidan"),
        ("Qishloq aholisidan qaysi biri o'q va kamon sotadi?", "Fletcher", "Farmer", "Librarian", "Cleric"),
        ("Warden qayerda paydo bo'ladi?", "Qadimiy shaharda", "Nether qal'asida", "Okean yodgorligida", "Cho'l ibodatxonasida"),
        ("Allay nimaga yordam beradi?", "Buyumlarni yig'ishga", "Dushman bilan jangga", "Suvda suzishga", "Rudalarni qazishga"),
        ("Strider nimaning ustida yura oladi?", "Lava ustida", "Suv ustida", "Qum ustida", "Muz ustida"),
        ("Piglin Brute qayerda uchraydi?", "Bastion qoldig'ida", "Nether qal'asida", "End shahrida", "Qishloqda"),
        ("Respawn Anchor nima bilan zaryadlanadi?", "Glowstone", "Redstone", "Lapis", "Ko'mir"),
        ("Lodestone nimani o'ziga bog'laydi?", "Kompasni", "Soatni", "Xaritani", "Kamonni"),
        ("Elytra bilan uchishda tezlashish uchun nima ishlatiladi?", "Mushak", "Kamon", "Qor to'pi", "Tuxum"),
        ("Trident qaysi mobdan tushishi mumkin?", "Drowned", "Guardian", "Zombi", "Skelet"),
        ("Panda qaysi biomda yashaydi?", "Bambuk o'rmonida", "Cho'l qumliklarida", "Tayga qorliklarida", "Okean sohillarida"),
        ("Mooshroom qaysi biomda uchraydi?", "Qo'ziqorin maydonida", "Cho'l qumliklarida", "Tayga qorliklarida", "Savanna dalalarida"),
        ("Sniffer nimani topishga yordam beradi?", "Qadimiy urug'larni", "Yashirin olmoslarni", "Ko'milgan xazinalarni", "Yashirin portallarni"),
        ("Tuya (Camel) qayerda uchraydi?", "Cho'l qishloqlarida", "Okean tubi g'orlarida", "Nether qal'alarida", "End shahar kemalarida"),
        ("Nether'da karavotda uxlashga urinsangiz nima bo'ladi?", "Karavot portlaydi", "Tun o'tib ketadi", "Sog'liq tiklanadi", "Hech narsa bo'lmaydi"),
        ("Nether'ga suv quysangiz nima bo'ladi?", "Bug'lanib ketadi", "Muzlab qoladi", "Lavaga aylanadi", "Odatdagidek oqadi"),
        ("Ender Dragon'ni nima davolab turadi?", "End kristallari", "Oltin olmalar", "Iksirlar", "Ender ko'zlari"),
        ("Ender ko'zi nima uchun kerak?", "End portalini topish va faollashtirish", "Qorong'uda atrofni yaxshi ko'rish", "Yangi moblarni o'ziga chaqirish", "Suv ostida uzoq vaqt nafas olish"),
        ("End portali qaysi inshootda topiladi?", "Stronghold", "Qishloq", "Cho'l ibodatxonasi", "Bastion"),
        ("Ender Dragon o'ldirilganda qaysi noyob buyum paydo bo'ladi?", "Ajdaho tuxumi", "Olmos bloki", "Beacon", "Netherit bloki"),
        ("Bambukdan nima yasash mumkin?", "Iskala", "Kamon", "Zirh", "Qilich"),
        ("Mis bloki vaqt o'tishi bilan nima bo'ladi?", "Oksidlanib rangi o'zgaradi", "Toshga aylanib qoladi", "O'z-o'zidan portlaydi", "Butunlay yo'qolib ketadi"),
        ("Chaqmoq tayoqchasi (Lightning Rod) nima qiladi?", "Chaqmoqni o'ziga tortadi", "Atrofni yoritib turadi", "Yomg'ir suvini yig'adi", "Yaqin moblarni portlatadi"),
        ("Tuxum va qor to'pi bitta to'plamda nechta bo'ladi?", "16", "64", "32", "8"),
        ("Oddiy sandiq necha katakli?", "27", "54", "36", "18"),
        ("Smithing Table nima uchun kerak?", "Netherit buyumlarini yasash uchun", "Ovqatni pishirish va saqlash uchun", "Xarita chizish va nusxalash uchun", "Iksir tayyorlash va saqlash uchun"),
        ("Grindstone nima qiladi?", "Buyumdagi sehrni olib tashlaydi", "Ovqatni pishirib berib turadi", "Cheksiz suv hosil qilib beradi", "Atrofga yorug'lik tarqatib turadi"),
        ("F3 tugmasi (Java) nimani ko'rsatadi?", "Texnik ma'lumotlarni", "Inventar va buyumlarni", "Dunyoning xaritasini", "Chat va yozishmalarni"),
    ],
    "hard": [
        ("Minecraft 1.0 versiyasi qaysi yili rasman chiqdi?", "2011", "2009", "2013", "2014"),
        ("Ender Dragon birinchi marta o'ldirilganda necha tajriba (XP) beradi?", "12000", "5000", "20000", "500"),
        ("Wither'ni yasash uchun nechta Wither skeleti kallasi kerak?", "3", "1", "2", "4"),
        ("Beacon'ning eng katta piramidasi necha qavatli?", "4", "3", "5", "6"),
        ("Nether'da 1 blok masofa Overworld'da necha blokka teng?", "8", "4", "16", "2"),
        ("Netherit buyumlari lavaga tushsa nima bo'ladi?", "Yonmaydi", "Yonib ketadi", "Sinib qoladi", "Kuchayadi"),
        ("Minecraft'da bir kun-tun sikli necha daqiqa davom etadi?", "20 daqiqa", "10 daqiqa", "30 daqiqa", "60 daqiqa"),
        ("Bir Minecraft kuni necha o'yin tikidan iborat?", "24000", "12000", "20000", "48000"),
        ("Creeper g'oyasi qanday paydo bo'lgan?", "Cho'chqa modelidagi xato tufayli", "Maxsus tanlov g'olibi sifatida", "Bolalar chizmasidan olingan", "Eski o'yindan ko'chirilgan"),
        ("Mojang qaysi mamlakatda tashkil topgan?", "Shvetsiya", "Daniya", "AQSh", "Norvegiya"),
        ("Minecraft'ning dastlabki musiqalarini kim yaratgan?", "Daniel Rosenfeld", "Hans Zimmer", "Jeremy Soule", "Koji Kondo"),
        ("Warden necha sog'liq (HP) ga ega?", "500", "300", "200", "1000"),
        ("Ender Dragon necha sog'liq (HP) ga ega?", "200", "100", "300", "500"),
        ("Elder Guardian qaysi ta'sirni beradi?", "Qazishda charchoq", "Kuchli zaharlanish", "To'liq ko'rlik", "Sekinlashish va zaiflik"),
        ("Nether portalini yasash uchun eng kamida nechta obsidian kerak?", "10", "12", "14", "8"),
        ("Redstone signali maksimal necha blokkacha boradi?", "15", "10", "16", "20"),
        ("Hopper necha katakli?", "5", "9", "3", "27"),
        ("Bitta ko'mir pechda necha buyumni eritadi?", "8", "10", "6", "4"),
        ("Bitta lava chelagi necha buyumni eritadi?", "100", "64", "50", "200"),
        ("Qaysi blokning yorug'lik darajasi 15 ga teng?", "Glowstone", "Mash'al", "Redstone mash'ali", "Magma bloki"),
        ("Mace qurolining asosiy xususiyati nima?", "Yiqilayotganda kuchli zarar beradi", "Uzoq masofadan o'q otib zarar beradi", "Nishonni olovga o'rab tashlaydi", "Atrofdagi do'stlarni davolab turadi"),
        ("Bitta blokka ko'pi bilan nechta sham (Candle) qo'yiladi?", "4", "2", "8", "16"),
        ("Ender marvaridi bitta to'plamda nechta bo'ladi?", "16", "64", "8", "32"),
        ("End portali ramkasi nechta blokdan iborat?", "12", "10", "8", "16"),
        ("To'liq kuchli Beacon uchun nechta blokli piramida kerak?", "164", "100", "120", "200"),
        ("Wither skeletining kallasini topish uchun qayerga borish kerak?", "Nether qal'asiga", "End shahriga", "Cho'l ibodatxonasiga", "Okean yodgorligiga"),
        ("Qaysi mob Illager guruhiga kirmaydi?", "Zombi", "Pillager", "Vindicator", "Evoker"),
        ("Oddiy (Normal) qiyinchilikda reyd necha to'lqindan iborat?", "5", "3", "7", "10"),
        ("Bad Omen effektini qaysi mobni o'ldirib olish mumkin?", "Pillager sardori", "Creeper sardori", "Enderman sardori", "Piglin sardori"),
        ("Temir golem necha sog'liq (HP) ga ega?", "100", "50", "200", "20"),
        ("Zombi qishloqchisini davolash uchun nima kerak?", "Zaiflik iksiri va oltin olma", "Sut chelagi va pishgan non", "Suv chelagi va sariq sabzi", "Olov va ko'p miqdorda tosh"),
        ("Silverfish qaysi bloklar ichida yashirinib yotadi?", "Tosh bloklari ichida", "Qum bloklari ichida", "Muz bloklari ichida", "Yog'och bloklari ichida"),
        ("Durbin (Spyglass) qanday materiallardan yasaladi?", "Mis va ametist", "Temir va shisha", "Oltin va zumrad", "Olmos va shisha"),
        ("Ocelot qaysi biomda uchraydi?", "Tropik o'rmonda", "Cho'lda", "Taygada", "Qorli tundrada"),
        ("Quyon oyog'i qaysi iksir uchun kerak?", "Sakrash iksiri", "Tezlik iksiri", "Kuch iksiri", "Davolash iksiri"),
        ("Ghast ko'z yoshi qaysi iksir uchun kerak?", "Regeneratsiya iksiri", "Tezlik iksiri", "Ko'rinmaslik iksiri", "Kuch iksiri"),
        ("Phantom'lar nima sababdan paydo bo'ladi?", "Uzoq vaqt uxlamaslikdan", "Haddan ortiq ochlikdan", "Suvda juda uzoq qolishdan", "Qorong'ulikda yurishdan"),
        ("Delfin sizga qanday yordam berishi mumkin?", "Xazina yo'lini ko'rsatadi", "Qazishni tezlashtirib beradi", "Zirh yasab berib turadi", "Ob-havoni o'zgartirib turadi"),
        ("Qaysi sehr etikka suv ustida muz hosil qilib yurishni beradi?", "Frost Walker", "Depth Strider", "Soul Speed", "Feather Falling"),
        ("Soul Speed sehri qaysi blokda tezlik beradi?", "Ruh qumi va ruh tuprog'ida", "Oddiy qum va oddiy tuproqda", "Muz va qor ustida yurganda", "Asal va shira bloklari ustida"),
        ("Conduit (kanal) qanday ta'sir beradi?", "Suv ostida nafas olish va ko'rish", "Havoda erkin uchish imkonini", "Quruqlikda tez yugurish imkonini", "Olov va lavadan himoyalanishni"),
        ("Sculk Sensor nimani sezadi?", "Tebranishlarni", "Hidlarni", "Rang o'zgarishini", "Haroratni"),
        ("Prizmarin kristallari qaysi mobdan tushadi?", "Guardian", "Drowned", "Zombi", "Creeper"),
        ("Ender sandig'ining asosiy xususiyati nima?", "Ichidagi buyumlarga hamma joydan kirish mumkin", "Faqat bir marta ochish mumkin bo'ladi", "Hech qachon sindirib bo'lmaydigan blok", "Ichida doim yangi buyumlar paydo bo'ladi"),
        ("Shulker qutisi nimadan yasaladi?", "Shulker qobig'i va sandiq", "Temir quymasi va sandiq", "Olmos bloki va sandiq", "Obsidian bloki va sandiq"),
        ("Looting sehri nimani oshiradi?", "Moblardan tushadigan o'ljani", "Rudalardan tushadigan o'ljani", "Baliq ovlash natijasini", "Ekin hosildorligini"),
        ("Luck of the Sea sehri nimaga ta'sir qiladi?", "Baliq ovlashda sovg'a tushishiga", "Qazish tezligini oshirishga", "Zirh mustahkamligini oshirishga", "Kamon otish masofasiga"),
        ("Unbreaking sehri nima qiladi?", "Buyum uzoqroq xizmat qiladi", "Zarar kuchini oshirib beradi", "Yugurishni tezlashtirib beradi", "Atrofga yorug'lik tarqatadi"),
        ("Netherit quymasi nimalardan yasaladi?", "4 oltin quymasi va 4 netherit parchasi", "4 olmos bloki va 4 oltin quymasi", "4 temir quymasi va 4 oltin quymasi", "4 netherit parchasi va 4 olmos bloki"),
        ("Bedrock Edition qaysi dasturlash tilida yozilgan?", "C++", "Java", "Python", "Go"),
    ],
}


# ----------------------------------------------------------------------
# 🎮 MINECRAFT VIKTORINA — Telegramning vaqtli viktorina (quiz poll) oynasi bilan
# ----------------------------------------------------------------------
def _sessions(app) -> dict:
    return app.bot_data.setdefault("quiz_sessions", {})


def _polls(app) -> dict:
    return app.bot_data.setdefault("quiz_polls", {})


def _cancel_jobs(app, name: str):
    if not app.job_queue:
        return
    for job in app.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def cleanup_session(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Tugallanmagan o'yinni jim bekor qiladi (masalan /start bosilganda)."""
    app = context.application
    session = _sessions(app).pop(user_id, None)
    if session:
        _polls(app).pop(session.get("poll_id"), None)
    _cancel_jobs(app, f"quiz_timeout_{user_id}")
    _cancel_jobs(app, f"quiz_next_{user_id}")


def build_level_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key in LEVEL_ORDER:
        lv = LEVELS[key]
        rows.append([InlineKeyboardButton(
            f"{lv['emoji']} {lv['title']} — {lv['count']} ta savol",
            callback_data=f"quizlvl:{key}",
        )])
    return InlineKeyboardMarkup(rows)


async def game_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎮 <b>Minecraft Viktorina</b>\n\n"
        "Darajani tanlang 👇\n"
        "⏱ Har bir savolga vaqt beriladi: Oson — 20, O'rta — 25, Qiyin — 30 soniya.",
        parse_mode="HTML",
        reply_markup=build_level_keyboard(),
    )


def save_quiz_result(user_id: int, first_name: str, level: str, pct: int):
    row = db_query(
        "SELECT games, min_pct, max_pct FROM quiz_stats WHERE user_id=? AND level=?",
        (user_id, level), fetch="one",
    )
    if row:
        db_query(
            "UPDATE quiz_stats SET games=?, min_pct=?, max_pct=?, first_name=? WHERE user_id=? AND level=?",
            (row[0] + 1, min(row[1], pct), max(row[2], pct), first_name, user_id, level),
        )
    else:
        db_query(
            "INSERT INTO quiz_stats (user_id, first_name, level, games, min_pct, max_pct) VALUES (?, ?, ?, 1, ?, ?)",
            (user_id, first_name, level, pct, pct),
        )


async def quiz_level_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    level = query.data.split(":")[1]
    if level not in LEVELS:
        await query.answer()
        return
    if not is_admin(user.id) and is_blocked(user.id):
        await query.answer()
        return

    app = context.application
    if user.id in _sessions(app):
        await query.answer("🎮 O'yin allaqachon boshlangan.", show_alert=True)
        return
    await query.answer()

    lv = LEVELS[level]
    pool = QUESTIONS[level]
    questions = random.sample(pool, min(lv["count"], len(pool)))
    chat_id = query.message.chat_id
    _sessions(app)[user.id] = {
        "level": level,
        "questions": questions,
        "index": 0,
        "correct": 0,
        "answered": 0,       # foydalanuvchi haqiqatan javob bergan savollar
        "idle": 0,           # ketma-ket javobsiz qolgan savollar
        "chat_id": chat_id,
        "name": user.first_name or user.username or "Foydalanuvchi",
        "qid": 0,
        "poll_id": None,
        "msg_id": None,
        "correct_pos": None,
        "answered_now": False,
    }

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await context.bot.send_message(
        chat_id,
        f"{lv['emoji']} <b>{lv['title']}</b> daraja boshlandi!\n\n"
        f"📝 Savollar soni: {len(questions)}\n"
        f"⏱ Har biriga: {lv['seconds']} soniya\n\n"
        f"Omad! 🍀 To'xtatish uchun pastdagi tugmani bosing 👇",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BTN_STOP)]], resize_keyboard=True),
    )
    await quiz_send_next(app, user.id)


async def quiz_send_next(app, user_id: int):
    sessions = _sessions(app)
    s = sessions.get(user_id)
    if not s:
        return
    if s["index"] >= len(s["questions"]):
        await quiz_finish(app, user_id, "done")
        return

    q_text, correct, *wrong = s["questions"][s["index"]]
    options = [correct] + list(wrong)
    random.shuffle(options)
    correct_pos = options.index(correct)
    seconds = LEVELS[s["level"]]["seconds"]
    number = s["index"] + 1
    total = len(s["questions"])

    try:
        msg = await app.bot.send_poll(
            chat_id=s["chat_id"],
            question=f"{number}/{total}. {q_text}"[:300],
            options=options,
            type=Poll.QUIZ,
            correct_option_id=correct_pos,
            is_anonymous=False,
            open_period=seconds,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔚 To'xtatish", callback_data="quiz_stop")]]
            ),
        )
    except Exception as e:
        logger.warning(f"Viktorina savolini yuborib bo'lmadi ({user_id}): {e}")
        sessions.pop(user_id, None)
        return

    s["index"] += 1
    s["qid"] += 1
    s["poll_id"] = msg.poll.id
    s["msg_id"] = msg.message_id
    s["correct_pos"] = correct_pos
    s["answered_now"] = False
    _polls(app)[msg.poll.id] = user_id

    if app.job_queue:
        app.job_queue.run_once(
            quiz_timeout_job,
            when=seconds + 1.5,
            data={"uid": user_id, "qid": s["qid"]},
            name=f"quiz_timeout_{user_id}",
        )


async def quiz_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa or not pa.user:
        return
    app = context.application
    uid = _polls(app).get(pa.poll_id)
    if uid is None or uid != pa.user.id:
        return
    s = _sessions(app).get(uid)
    if not s or s["poll_id"] != pa.poll_id or s["answered_now"]:
        return

    s["answered_now"] = True
    s["idle"] = 0
    s["answered"] += 1
    if pa.option_ids and pa.option_ids[0] == s["correct_pos"]:
        s["correct"] += 1
    _polls(app).pop(pa.poll_id, None)
    _cancel_jobs(app, f"quiz_timeout_{uid}")

    if app.job_queue:
        app.job_queue.run_once(
            quiz_next_job, when=NEXT_QUESTION_DELAY, data={"uid": uid}, name=f"quiz_next_{uid}"
        )


async def quiz_next_job(context: ContextTypes.DEFAULT_TYPE):
    await quiz_send_next(context.application, context.job.data["uid"])


async def quiz_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    uid = context.job.data["uid"]
    qid = context.job.data["qid"]
    s = _sessions(app).get(uid)
    if not s or s["qid"] != qid or s["answered_now"]:
        return  # allaqachon javob berilgan yoki o'yin tugagan

    s["idle"] += 1
    _polls(app).pop(s["poll_id"], None)
    if s["idle"] >= MAX_IDLE:
        await quiz_finish(app, uid, "idle")
    else:
        await quiz_send_next(app, uid)


async def quiz_finish(app, user_id: int, reason: str):
    s = _sessions(app).pop(user_id, None)
    if not s:
        return
    _cancel_jobs(app, f"quiz_timeout_{user_id}")
    _cancel_jobs(app, f"quiz_next_{user_id}")
    _polls(app).pop(s.get("poll_id"), None)

    # Javob berilmagan ochiq savol bo'lsa, uni yopamiz (o'chirmaymiz)
    if s.get("msg_id") and not s.get("answered_now"):
        try:
            await app.bot.stop_poll(s["chat_id"], s["msg_id"])
        except Exception:
            pass

    lv = LEVELS[s["level"]]
    total = len(s["questions"])
    pct = round(100 * s["correct"] / total) if total else 0
    counted = s["answered"] >= 1     # kamida bitta savolga javob berilgan o'yin hisoblanadi

    if reason == "idle":
        head = f"⏰ Ketma-ket {MAX_IDLE} ta savolga javob bermadingiz, o'yin to'xtatildi."
    elif reason == "stop":
        head = "🔚 O'yin to'xtatildi."
    else:
        head = "🏁 O'yin tugadi!"

    text = (
        f"{head}\n\n"
        f"{lv['emoji']} Daraja: <b>{lv['title']}</b>\n"
        f"✅ To'g'ri javoblar: <b>{s['correct']}/{total}</b>\n"
        f"📊 Natija: <b>{pct}%</b>"
    )
    if counted:
        save_quiz_result(user_id, s["name"], s["level"], pct)
        text += "\n\n🏆 Natijangiz reytingga qo'shildi."
    else:
        text += "\n\nℹ️ Birorta ham savolga javob berilmagani uchun natija reytingga qo'shilmadi."
    text += "\n\nYana o'ynash uchun \"🎮 Minecraft Viktorina\" tugmasini bosing."

    try:
        await app.bot.send_message(
            s["chat_id"], text, parse_mode="HTML", reply_markup=menu_for(user_id)
        )
    except Exception as e:
        logger.warning(f"Viktorina natijasini yuborib bo'lmadi ({user_id}): {e}")


async def quiz_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in _sessions(context.application):
        await query.answer("O'yin allaqachon tugagan.", show_alert=True)
        return
    await query.answer()
    await quiz_finish(context.application, query.from_user.id, "stop")


async def quiz_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in _sessions(context.application):
        await quiz_finish(context.application, uid, "stop")
    else:
        await update.message.reply_text(
            "ℹ️ Hozir o'yin boshlanmagan.", reply_markup=menu_for(uid)
        )


# ----------------------------------------------------------------------
# 🏆 REYTING — har bir o'yinchi uchun daraja bo'yicha eng past va eng yuqori foiz
# ----------------------------------------------------------------------
def _pct_range(min_pct, max_pct) -> str:
    if min_pct is None:
        return "—"
    if min_pct == max_pct:
        return f"{max_pct}%"
    return f"{min_pct}% - {max_pct}%"


async def leaderboard_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_query(
        "SELECT user_id, first_name, level, min_pct, max_pct FROM quiz_stats", fetch="all"
    )
    players = {}
    for uid, name, level, mn, mx in rows:
        p = players.setdefault(uid, {"name": name, "levels": {}})
        p["name"] = name or p["name"]
        p["levels"][level] = (mn, mx)

    if not players:
        text = "🏆 Hozircha hech kim viktorinada qatnashmagan.\nBirinchi bo'ling! 🎮"
    else:
        def score(p):
            return sum(p["levels"].get(k, (0, 0))[1] for k in LEVEL_ORDER)

        ranked = sorted(players.values(), key=score, reverse=True)[:10]
        medals = ["🥇", "🥈", "🥉"]
        blocks = []
        for i, p in enumerate(ranked):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lines = [f"{prefix} <b>{html.escape(p['name'] or 'Foydalanuvchi')}</b>"]
            for key in LEVEL_ORDER:
                mn, mx = p["levels"].get(key, (None, None))
                lines.append(f"{LEVELS[key]['title']}: {_pct_range(mn, mx)}")
            blocks.append("\n".join(lines))
        text = "🏆 <b>Reyting</b>\n\n" + "\n\n".join(blocks)

    keyboard = None
    if is_admin(update.effective_user.id):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Reytingni tozalash", callback_data="reset_leaderboard_confirm")]
        ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def reset_leaderboard_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ha, tozalash", callback_data="reset_leaderboard_yes")],
        [InlineKeyboardButton("❌ Yo'q", callback_data="reset_leaderboard_no")],
    ])
    await query.message.edit_text(
        "⚠️ Rostdan ham BARCHA foydalanuvchilarning reytingini tozalamoqchimisiz? "
        "Bu amalni ortga qaytarib bo'lmaydi.",
        reply_markup=keyboard,
    )


async def reset_leaderboard_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    if query.data == "reset_leaderboard_yes":
        db_query("DELETE FROM quiz_stats")
        log_action(query.from_user.id, "Reyting tozalandi")
        await query.message.edit_text("✅ Reyting tozalandi.")
    else:
        await query.message.edit_text("❌ Bekor qilindi, reyting o'zgarmadi.")


# ----------------------------------------------------------------------
# 🎯 HAFTALIK SAVOL — har shanba 18:00 (Toshkent), reytingga qo'shilmaydi
# ----------------------------------------------------------------------
async def weekly_quiz_job(context: ContextTypes.DEFAULT_TYPE):
    now = now_tk()
    if now.weekday() != 5:          # 5 = shanba
        return
    today = now.strftime("%Y-%m-%d")
    if get_setting("last_weekly_quiz") == today:
        return                      # bugun allaqachon yuborilgan
    set_setting("last_weekly_quiz", today)

    q_text, correct, *wrong = random.choice(QUESTIONS["easy"])
    options = [correct] + list(wrong)
    random.shuffle(options)
    correct_pos = options.index(correct)
    question = f"🎯 Haftalik savol\n\n{q_text}"[:300]

    async def send_one(chat_id):
        await context.bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            type=Poll.QUIZ,
            correct_option_id=correct_pos,
            is_anonymous=True,
        )

    su, fu, sg, fg = await broadcast_to_all(context.bot, send_one)
    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"🎯 Haftalik savol yuborildi.\n👤 Foydalanuvchilar: {su} ta yetdi, {fu} ta xato\n"
                f"👥 Guruhlar: {sg} ta yetdi, {fg} ta xato",
            )
        except Exception:
            pass


# ----------------------------------------------------------------------
# 📩 KEYHONGA MUROJAAT (foydalanuvchi ↔ admin to'g'ridan-to'g'ri xabar)
# ----------------------------------------------------------------------
async def contact_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) and is_blocked(uid):
        return ConversationHandler.END
    await update.message.reply_text(
        "✍️ Xabaringizni yozing (matn, rasm yoki fayl bo'lishi mumkin), adminlarga yetkazamiz 📨\n\n"
        "Bekor qilish uchun /cancel"
    )
    return WAIT_CONTACT_MESSAGE


async def contact_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    admins = db_query("SELECT user_id FROM admins", fetch="all")
    username_part = f"@{html.escape(user.username)}" if user.username else "(username yo'q)"
    header = (
        f"📩 <b>Yangi murojaat!</b>\n"
        f"👤 {html.escape(user.first_name or '')} {username_part}\n"
        f"🆔 ID: <code>{user.id}</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Javob berish", callback_data=f"admin_reply:{user.id}")]
    ])

    for (admin_id,) in admins:
        try:
            await context.bot.send_message(admin_id, header, parse_mode="HTML", reply_markup=keyboard)
            await msg.copy(chat_id=admin_id)
        except Exception as e:
            logger.warning(f"Adminga murojaat yuborishda xatolik ({admin_id}): {e}")

    await update.message.reply_text(
        "✅ Rahmat! Xabaringiz adminlarga yetkazildi, tez orada javob beramiz 🙏",
        reply_markup=menu_for(user.id),
    )
    return ConversationHandler.END


async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    target_user_id = int(query.data.split(":")[1])
    context.user_data["reply_target"] = target_user_id
    await query.message.reply_text(
        "✍️ Javobingizni yozing (matn, rasm yoki fayl bo'lishi mumkin):\n\nBekor qilish uchun /cancel"
    )
    return WAIT_ADMIN_REPLY


async def admin_reply_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_user_id = context.user_data.get("reply_target")
    if not target_user_id:
        await update.message.reply_text("⚠️ Xatolik: kimga javob berish noma'lum.")
        return ConversationHandler.END

    try:
        await update.message.copy(chat_id=target_user_id)
        await update.message.reply_text("✅ Javob foydalanuvchiga yuborildi.", reply_markup=build_admin_menu())
    except Exception as e:
        await update.message.reply_text("❌ Yuborishda xatolik: foydalanuvchi botni bloklagan bo'lishi mumkin.")
        logger.warning(f"Admin javobini yuborishda xatolik ({target_user_id}): {e}")

    context.user_data.pop("reply_target", None)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 📢 POST YUBORISH (BROADCAST) — foydalanuvchilarga va bot a'zo bo'lgan guruhlarga
# ----------------------------------------------------------------------
def get_user_targets():
    """Post oladigan foydalanuvchilar: bloklanmagan, botni to'xtatmagan, obuna bo'lgan (yoki admin)."""
    rows = db_query(
        "SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(bot_blocked,0)=0 "
        "AND (subscribed=1 OR user_id IN (SELECT user_id FROM admins))",
        fetch="all",
    )
    return [r[0] for r in rows]


def get_group_targets():
    rows = db_query("SELECT chat_id FROM chats", fetch="all")
    return [r[0] for r in rows]


def _retry_seconds(e) -> float:
    ra = e.retry_after
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def deliver(chat_id: int, is_group: bool, send_fn) -> bool:
    """Bitta chatga yuboradi; Telegram cheklovlari va xatolarini to'g'ri boshqaradi."""
    for _attempt in range(2):
        try:
            await send_fn(chat_id)
            return True
        except RetryAfter as e:
            await asyncio.sleep(_retry_seconds(e) + 1)
        except ChatMigrated as e:
            new_id = e.new_chat_id
            db_query("DELETE FROM chats WHERE chat_id=?", (chat_id,))
            db_query(
                "INSERT OR IGNORE INTO chats (chat_id, title, chat_type, added_at) VALUES (?, ?, ?, ?)",
                (new_id, "", "supergroup", now_tk().isoformat()),
            )
            chat_id = new_id
        except Forbidden:
            if is_group:
                db_query("DELETE FROM chats WHERE chat_id=?", (chat_id,))
            else:
                db_query("UPDATE users SET bot_blocked=1 WHERE user_id=?", (chat_id,))
            return False
        except BadRequest as e:
            if is_group and "chat not found" in str(e).lower():
                db_query("DELETE FROM chats WHERE chat_id=?", (chat_id,))
            logger.warning(f"Yuborishda xatolik ({chat_id}): {e}")
            return False
        except Exception as e:
            logger.warning(f"Yuborishda xatolik ({chat_id}): {e}")
            return False
    return False


async def broadcast_to_all(bot, send_fn):
    """send_fn(chat_id) — har bir chatga yuboradigan coroutine. (user_ok, user_fail, group_ok, group_fail)"""
    su = fu = sg = fg = 0
    for uid in get_user_targets():
        if await deliver(uid, False, send_fn):
            su += 1
        else:
            fu += 1
        await asyncio.sleep(0.05)       # sekundiga ~20 ta — Telegram limitidan past
    for gid in get_group_targets():
        if await deliver(gid, True, send_fn):
            sg += 1
        else:
            fg += 1
        await asyncio.sleep(0.05)
    return su, fu, sg, fg


async def do_broadcast(bot, from_chat_id: int, message_id: int, admin_id: int):
    async def send_one(chat_id):
        await bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)

    su, fu, sg, fg = await broadcast_to_all(bot, send_one)
    db_query(
        "INSERT INTO broadcasts (admin_id, sent_at, total_sent, total_failed) VALUES (?, ?, ?, ?)",
        (admin_id, now_tk().isoformat(), su + sg, fu + fg),
    )
    return su, fu, sg, fg


def _report_text(title: str, su, fu, sg, fg) -> str:
    return (
        f"{title}\n\n"
        f"👤 Foydalanuvchilar: {su} ta yetdi, {fu} ta xato\n"
        f"👥 Guruhlar: {sg} ta yetdi, {fg} ta xato"
    )


async def _broadcast_and_report(bot, from_chat_id, message_id, admin_id, report_chat_id, title):
    try:
        su, fu, sg, fg = await do_broadcast(bot, from_chat_id, message_id, admin_id)
        await bot.send_message(report_chat_id, _report_text(title, su, fu, sg, fg))
    except Exception as e:
        logger.error(f"Post yuborishda xatolik: {e}")
        try:
            await bot.send_message(report_chat_id, f"❌ Post yuborishda xatolik yuz berdi: {e}")
        except Exception:
            pass


def schedule_post(app, post_id: int, when: datetime):
    """when — Toshkent vaqti (tzinfo'siz)."""
    if not app.job_queue:
        return
    app.job_queue.run_once(
        scheduled_broadcast_job,
        when=when.replace(tzinfo=TZ),
        data={"post_id": post_id},
        name=f"post_{post_id}",
    )


async def scheduled_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    post_id = context.job.data["post_id"]
    row = db_query(
        "SELECT admin_id, admin_chat_id, from_chat_id, message_id, status FROM scheduled_posts WHERE id=?",
        (post_id,), fetch="one",
    )
    if not row or row[4] != "pending":
        return
    admin_id, admin_chat_id, from_chat_id, message_id, _status = row
    db_query("UPDATE scheduled_posts SET status='sent' WHERE id=?", (post_id,))
    await _broadcast_and_report(
        context.bot, from_chat_id, message_id, admin_id, admin_chat_id,
        "✅ Rejalashtirilgan post yuborildi!",
    )


async def post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "📢 Yuboriladigan xabarni yuboring (matn, rasm, video yoki fayl bo'lishi mumkin).\n"
        "Xabar barcha obunachilarga va bot a'zo bo'lgan guruhlarga boradi.\n\n"
        "Bekor qilish uchun /cancel"
    )
    return WAIT_POST_CONTENT


async def post_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["post_from_chat"] = update.message.chat_id
    context.user_data["post_message_id"] = update.message.message_id
    n_users = len(get_user_targets())
    n_groups = len(get_group_targets())
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Hozir yuborish", callback_data="post_confirm")],
        [InlineKeyboardButton("🕒 Vaqt belgilash", callback_data="post_schedule")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="post_cancel")],
    ])
    await update.message.reply_text(
        f"👀 Xabar {n_users} ta foydalanuvchiga va {n_groups} ta guruhga yuboriladi.\n\n"
        f"Qachon yuborishni tanlang:",
        reply_markup=keyboard,
    )
    return WAIT_POST_CONFIRM


async def post_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_cancel":
        await query.message.edit_text("❌ Bekor qilindi.")
        return ConversationHandler.END

    if query.data == "post_schedule":
        await query.message.edit_text(
            "🕒 Xabarni qachon yuborishni kiriting (Toshkent vaqti bilan).\n\n"
            "Format: <code>KK.OO.YYYY SS:DD</code>\n"
            "Masalan: <code>15.11.2026 09:30</code>\n\n"
            "Bekor qilish uchun /cancel",
            parse_mode="HTML",
        )
        return WAIT_POST_TIME

    from_chat = context.user_data.get("post_from_chat")
    message_id = context.user_data.get("post_message_id")
    if not from_chat or not message_id:
        await query.message.edit_text("⚠️ Xatolik: xabar topilmadi.")
        return ConversationHandler.END

    await query.message.edit_text("⏳ Yuborish boshlandi. Tugagach, natijani yuboraman 📨")
    # Fon rejimida yuboramiz — shunda bot boshqa foydalanuvchilarga javob berishda davom etadi
    context.application.create_task(
        _broadcast_and_report(
            context.bot, from_chat, message_id, query.from_user.id,
            query.message.chat_id, "✅ Post yuborildi!",
        )
    )
    context.user_data.pop("post_from_chat", None)
    context.user_data.pop("post_message_id", None)
    return ConversationHandler.END


async def post_time_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        when = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Format noto'g'ri. Masalan: <code>15.11.2026 09:30</code>\n\nQaytadan kiriting yoki /cancel",
            parse_mode="HTML",
        )
        return WAIT_POST_TIME

    if when <= now_tk():
        await update.message.reply_text("❌ Bu vaqt allaqachon o'tib ketgan. Kelajakdagi vaqtni kiriting.")
        return WAIT_POST_TIME

    from_chat = context.user_data.get("post_from_chat")
    message_id = context.user_data.get("post_message_id")
    if not from_chat or not message_id:
        await update.message.reply_text("⚠️ Xatolik: xabar topilmadi.")
        return ConversationHandler.END

    post_id = db_insert(
        "INSERT INTO scheduled_posts (admin_id, admin_chat_id, from_chat_id, message_id, run_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        (update.effective_user.id, update.effective_chat.id, from_chat, message_id, when.isoformat()),
    )
    schedule_post(context.application, post_id, when)
    log_action(update.effective_user.id, "Post rejalashtirildi", when.strftime("%d.%m.%Y %H:%M"))
    await update.message.reply_text(
        f"✅ Post {when.strftime('%d.%m.%Y %H:%M')} (Toshkent vaqti) da yuborilishi rejalashtirildi!",
        reply_markup=build_admin_menu(),
    )
    context.user_data.pop("post_from_chat", None)
    context.user_data.pop("post_message_id", None)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 👥 GURUHLARNI KUZATISH — bot qo'shilgan guruhlar post ro'yxatiga tushadi
# ----------------------------------------------------------------------
def _remember_chat(chat):
    db_query(
        "INSERT OR IGNORE INTO chats (chat_id, title, chat_type, added_at) VALUES (?, ?, ?, ?)",
        (chat.id, chat.title or "", chat.type, now_tk().isoformat()),
    )
    db_query("UPDATE chats SET title=?, chat_type=? WHERE chat_id=?", (chat.title or "", chat.type, chat.id))


async def my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    if not cmu:
        return
    chat = cmu.chat
    new_status = cmu.new_chat_member.status

    if chat.type in ("group", "supergroup"):
        if new_status in ("member", "administrator", "restricted"):
            _remember_chat(chat)
        elif new_status in ("left", "kicked"):
            db_query("DELETE FROM chats WHERE chat_id=?", (chat.id,))
    elif chat.type == "private":
        if new_status == "kicked":
            db_query("UPDATE users SET bot_blocked=1 WHERE user_id=?", (chat.id,))
        elif new_status == "member":
            db_query("UPDATE users SET bot_blocked=0 WHERE user_id=?", (chat.id,))


async def track_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guruhdagi har qanday xabar guruhni ro'yxatga oladi (bot qo'shilganda hodisa o'tkazib yuborilgan bo'lsa ham)."""
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        _remember_chat(chat)


# ----------------------------------------------------------------------
# 📂 BO'LIMLAR (KATEGORIYALAR) — faqat admin
# ----------------------------------------------------------------------
async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    categories = get_categories()
    keyboard = []
    for cat_id, name in categories:
        keyboard.append([
            InlineKeyboardButton("⬆️", callback_data=f"admin_catup:{cat_id}"),
            InlineKeyboardButton("⬇️", callback_data=f"admin_catdown:{cat_id}"),
            InlineKeyboardButton(f"✏️ {name}", callback_data=f"admin_editcat:{cat_id}"),
            InlineKeyboardButton(f"🗑 {name}", callback_data=f"admin_delcat:{cat_id}"),
        ])
    keyboard.append([InlineKeyboardButton("➕ Yangi bo'lim qo'shish", callback_data="admin_addcat")])
    text = "📂 Bo'limlar ro'yxati (⬆️⬇️ — tartib, ✏️ — tahrirlash, 🗑 — o'chirish):"
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        # Tartib o'zgarganda yangi xabar yubormasdan, mavjudini yangilaymiz
        try:
            await update.callback_query.message.edit_text(text, reply_markup=markup)
        except BadRequest:
            pass
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def move_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    direction, cat_id = query.data.split(":")
    cat_id = int(cat_id)

    ids = [c[0] for c in get_categories()]
    if cat_id not in ids:
        return
    pos = ids.index(cat_id)
    swap_with = pos - 1 if direction == "admin_catup" else pos + 1
    if swap_with < 0 or swap_with >= len(ids):
        return  # allaqachon chekkada

    ids[pos], ids[swap_with] = ids[swap_with], ids[pos]
    for new_pos, cid in enumerate(ids):
        db_query("UPDATE categories SET position=? WHERE id=?", (new_pos, cid))

    await categories_menu(update, context)


async def delcat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cat_id = int(query.data.split(":")[1])
    db_query("DELETE FROM categories WHERE id=?", (cat_id,))
    db_query("DELETE FROM favorites WHERE category_id=?", (cat_id,))
    log_action(query.from_user.id, "Bo'lim o'chirildi", str(cat_id))
    await query.message.edit_text("✅ Bo'lim o'chirildi.")


def _extract_content(msg):
    if msg.photo:
        return "photo", msg.photo[-1].file_id, msg.caption
    if msg.document:
        return "document", msg.document.file_id, msg.caption
    if msg.video:
        return "video", msg.video.file_id, msg.caption
    return "text", None, msg.text


async def editcat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data["edit_cat_id"] = int(query.data.split(":")[1])
    await query.message.reply_text(
        "✏️ Bu bo'lim uchun yangi kontent yuboring (matn, rasm, video yoki fayl):\n\nBekor qilish uchun /cancel"
    )
    return WAIT_EDIT_CONTENT


async def editcat_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = context.user_data.get("edit_cat_id")
    content_type, file_id, text = _extract_content(update.message)
    db_query(
        "UPDATE categories SET content_type=?, content_text=?, file_id=? WHERE id=?",
        (content_type, text, file_id, cat_id),
    )
    await update.message.reply_text("✅ Bo'lim kontenti yangilandi!", reply_markup=build_admin_menu())
    context.user_data.pop("edit_cat_id", None)
    return ConversationHandler.END


async def addcat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "Yangi bo'lim nomini kiriting (masalan: 'Narxlar'):\n\nBekor qilish uchun /cancel"
    )
    return WAIT_CAT_NAME


async def addcat_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_cat_name"] = update.message.text
    await update.message.reply_text("Endi shu bo'lim uchun kontent yuboring (matn, rasm, video yoki fayl):")
    return WAIT_CAT_CONTENT


async def addcat_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get("new_cat_name", "Nomsiz")
    content_type, file_id, text = _extract_content(update.message)

    max_pos_row = db_query("SELECT MAX(position) FROM categories", fetch="one")
    next_pos = (max_pos_row[0] or 0) + 1
    db_query(
        "INSERT INTO categories (name, content_type, content_text, file_id, position) VALUES (?, ?, ?, ?, ?)",
        (name, content_type, text, file_id, next_pos),
    )
    log_action(update.effective_user.id, "Bo'lim qo'shildi", name)
    await update.message.reply_text(f"✅ '{name}' bo'limi qo'shildi!", reply_markup=build_admin_menu())
    context.user_data.pop("new_cat_name", None)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 👤 ADMINLAR — faqat admin (qo'shish/o'chirish faqat OWNER)
# ----------------------------------------------------------------------
async def admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    admins = db_query("SELECT user_id FROM admins", fetch="all")
    text = "👤 Adminlar ro'yxati:\n\n" + "\n".join(f"• {a[0]}" for a in admins)

    keyboard = []
    if is_owner(update.effective_user.id):
        keyboard.append([InlineKeyboardButton("➕ Admin qo'shish", callback_data="admin_addadmin")])
        keyboard.append([InlineKeyboardButton("➖ Adminni o'chirish", callback_data="admin_deladmin")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)


async def addadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.message.reply_text("❌ Faqat bosh admin (owner) yangi admin qo'sha oladi.")
        return ConversationHandler.END
    await query.message.reply_text("Yangi adminning Telegram ID raqamini yuboring:\n\nBekor qilish uchun /cancel")
    return WAIT_ADMIN_ID


async def addadmin_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Iltimos, faqat raqam yuboring.")
        return WAIT_ADMIN_ID
    db_query(
        "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
        (new_id, update.effective_user.id, now_tk().isoformat()),
    )
    log_action(update.effective_user.id, "Admin qo'shildi", str(new_id))
    await update.message.reply_text(f"✅ {new_id} admin qilib qo'shildi.", reply_markup=build_admin_menu())
    return ConversationHandler.END


async def deladmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.message.reply_text("❌ Faqat bosh admin (owner) adminni o'chira oladi.")
        return ConversationHandler.END
    await query.message.reply_text("O'chirmoqchi bo'lgan adminning ID raqamini yuboring:\n\nBekor qilish uchun /cancel")
    return WAIT_DEL_ADMIN_ID


async def deladmin_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        del_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Iltimos, faqat raqam yuboring.")
        return WAIT_DEL_ADMIN_ID
    if del_id == OWNER_ID:
        await update.message.reply_text("❌ Bosh adminni o'chirib bo'lmaydi.")
        return ConversationHandler.END
    db_query("DELETE FROM admins WHERE user_id=?", (del_id,))
    log_action(update.effective_user.id, "Admin o'chirildi", str(del_id))
    await update.message.reply_text(f"✅ {del_id} adminlikdan chiqarildi.", reply_markup=build_admin_menu())
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 🚫 FOYDALANUVCHILARNI BLOKLASH — faqat admin
# ----------------------------------------------------------------------
async def users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    blocked = db_query("SELECT user_id, first_name FROM users WHERE blocked=1", fetch="all")
    text = "🚫 <b>Bloklangan foydalanuvchilar:</b>\n\n"
    text += (
        "\n".join(f"• {html.escape(name or '')} (ID: {uid})" for uid, name in blocked)
        if blocked else "(hozircha yo'q)"
    )

    keyboard = [[InlineKeyboardButton("➕ Foydalanuvchini bloklash", callback_data="block_user_start")]]
    for uid, name in blocked:
        keyboard.append([InlineKeyboardButton(f"✅ {name or uid} — blokdan chiqarish", callback_data=f"unblock_user:{uid}")])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def block_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "Bloklamoqchi bo'lgan foydalanuvchining Telegram ID raqamini yuboring:\n\nBekor qilish uchun /cancel"
    )
    return WAIT_BLOCK_USER_ID


async def block_user_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Iltimos, faqat raqam yuboring.")
        return WAIT_BLOCK_USER_ID

    if is_admin(target_id):
        await update.message.reply_text("❌ Adminni bloklab bo'lmaydi.", reply_markup=build_admin_menu())
        return ConversationHandler.END

    db_query("UPDATE users SET blocked=1 WHERE user_id=?", (target_id,))
    log_action(update.effective_user.id, "Foydalanuvchi bloklandi", str(target_id))
    await update.message.reply_text(f"✅ {target_id} bloklandi.", reply_markup=build_admin_menu())
    return ConversationHandler.END


async def unblock_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    target_id = int(query.data.split(":")[1])
    db_query("UPDATE users SET blocked=0 WHERE user_id=?", (target_id,))
    log_action(query.from_user.id, "Foydalanuvchi blokdan chiqarildi", str(target_id))
    await query.message.edit_text("✅ Foydalanuvchi blokdan chiqarildi.")


# ----------------------------------------------------------------------
# 📜 AMALLAR TARIXI — faqat admin
# ----------------------------------------------------------------------
async def history_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = db_query(
        "SELECT admin_id, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT 15",
        fetch="all",
    )
    if not rows:
        await update.message.reply_text("📜 Hali hech qanday amal qayd etilmagan.")
        return

    text = "📜 <b>So'nggi amallar:</b>\n\n"
    for admin_id, action, detail, created_at in rows:
        try:
            date_str = created_at.split("T")[0] + " " + created_at.split("T")[1][:5]
        except Exception:
            date_str = created_at or ""
        text += f"• {date_str} — {html.escape(action or '')}"
        if detail:
            text += f" ({html.escape(detail)})"
        text += f" [admin: {admin_id}]\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ----------------------------------------------------------------------
# 💾 ZAXIRA NUSXA — faqat admin
# ----------------------------------------------------------------------
async def send_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("❌ Baza fayli topilmadi.")
        return
    with open(DB_PATH, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"backup_{now_tk().strftime('%Y%m%d_%H%M')}.db",
            caption="💾 Ma'lumotlar bazasining joriy zaxira nusxasi\n\n"
                    "Buni qayta tiklash uchun /restore buyrug'ini yuboring.",
        )


async def restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "🔄 Avval yuborilgan zaxira (.db) faylini shu yerga yuboring.\n\n"
        "⚠️ Diqqat: joriy ma'lumotlar shu fayl bilan almashtiriladi!\n\nBekor qilish uchun /cancel"
    )
    return WAIT_RESTORE_FILE


async def restore_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".db"):
        await update.message.reply_text("❌ Iltimos, .db kengaytmali zaxira faylini yuboring.")
        return WAIT_RESTORE_FILE

    # Avval vaqtinchalik faylga yuklab, haqiqiy SQLite ekanini tekshiramiz —
    # shunda buzuq fayl joriy bazani yo'q qilib yubormaydi.
    tmp_path = DB_PATH + ".restore_tmp"
    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(tmp_path)
    with open(tmp_path, "rb") as f:
        header = f.read(16)
    if not header.startswith(b"SQLite format 3"):
        os.remove(tmp_path)
        await update.message.reply_text("❌ Bu fayl to'g'ri baza fayli emas. Joriy ma'lumotlar o'zgarmadi.")
        return WAIT_RESTORE_FILE

    os.replace(tmp_path, DB_PATH)
    db_init()   # eski zaxirada yangi jadvallar bo'lmasligi mumkin — qo'shib qo'yamiz

    log_action(update.effective_user.id, "Baza zaxiradan tiklandi", doc.file_name)
    await update.message.reply_text(
        "✅ Ma'lumotlar bazasi muvaffaqiyatli tiklandi!",
        reply_markup=build_admin_menu(),
    )
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 📊 STATISTIKA — faqat admin
# ----------------------------------------------------------------------
def generate_stats_chart():
    """Oxirgi 7 kunlik yangi foydalanuvchilar va bo'limlar bo'yicha bosilishlar grafigi."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))

    days, counts = [], []
    for i in range(6, -1, -1):
        day = now_tk() - timedelta(days=i)
        row = db_query(
            "SELECT COUNT(*) FROM users WHERE joined_at LIKE ?",
            (f"{day.strftime('%Y-%m-%d')}%",), fetch="one",
        )
        days.append(day.strftime("%d.%m"))
        counts.append(row[0] if row else 0)

    ax1.plot(days, counts, marker="o", color="#4C6EF5")
    ax1.set_title("Yangi foydalanuvchilar (7 kun)")
    ax1.set_ylabel("Kishi")
    ax1.grid(alpha=0.3)

    top = db_query("""
        SELECT c.name, COUNT(cl.id) as cnt
        FROM clicks cl JOIN categories c ON c.id = cl.category_id
        GROUP BY c.id ORDER BY cnt DESC LIMIT 5
    """, fetch="all")

    if top:
        ax2.barh([r[0] for r in top], [r[1] for r in top], color="#12B886")
        ax2.set_title("Eng ko'p bosilgan bo'limlar")
        ax2.invert_yaxis()
    else:
        ax2.text(0.5, 0.5, "Hali ma'lumot yo'q", ha="center", va="center")
        ax2.set_title("Eng ko'p bosilgan bo'limlar")
        ax2.axis("off")

    plt.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def get_stats_text() -> str:
    total_users = db_query("SELECT COUNT(*) FROM users", fetch="one")[0]
    subscribed = db_query("SELECT COUNT(*) FROM users WHERE subscribed=1", fetch="one")[0]
    groups = db_query("SELECT COUNT(*) FROM chats", fetch="one")[0]

    day_ago = (now_tk() - timedelta(days=1)).isoformat()
    week_ago = (now_tk() - timedelta(days=7)).isoformat()
    active_24h = db_query("SELECT COUNT(*) FROM users WHERE last_active >= ?", (day_ago,), fetch="one")[0]
    active_7d = db_query("SELECT COUNT(*) FROM users WHERE last_active >= ?", (week_ago,), fetch="one")[0]

    top_categories = db_query("""
        SELECT c.name, COUNT(cl.id) as cnt
        FROM clicks cl JOIN categories c ON c.id = cl.category_id
        GROUP BY c.id ORDER BY cnt DESC LIMIT 5
    """, fetch="all")

    last_broadcasts = db_query(
        "SELECT sent_at, total_sent, total_failed FROM broadcasts ORDER BY id DESC LIMIT 3", fetch="all"
    )

    text = (
        "📊 <b>To'liq statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
        f"✅ Obuna bo'lganlar: <b>{subscribed}</b>\n"
        f"💬 Guruhlar: <b>{groups}</b>\n"
        f"🟢 Faol (24 soat): <b>{active_24h}</b>\n"
        f"🟢 Faol (7 kun): <b>{active_7d}</b>\n\n"
        "🔥 <b>Eng ko'p bosilgan bo'limlar:</b>\n"
    )
    if top_categories:
        for name, cnt in top_categories:
            text += f"  • {html.escape(name or '')}: {cnt} marta\n"
    else:
        text += "  (hali ma'lumot yo'q)\n"

    text += "\n📢 <b>So'nggi postlar:</b>\n"
    if last_broadcasts:
        for sent_at, sent, failed in last_broadcasts:
            text += f"  • {sent_at.split('T')[0]}: {sent} ta yetdi, {failed} ta xato\n"
    else:
        text += "  (hali post yuborilmagan)\n"
    return text


async def stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(get_stats_text(), parse_mode="HTML")
    try:
        await update.message.reply_photo(photo=generate_stats_chart(), caption="📈 Vizual statistika")
    except Exception as e:
        logger.warning(f"Grafik yasashda xatolik: {e}")


async def daily_stats_job(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni 09:00 (Toshkent) da OWNER'ga avtomatik statistika yuboradi."""
    if not OWNER_ID:
        return
    try:
        await context.bot.send_message(
            OWNER_ID, f"🕐 <b>Kunlik hisobot</b>\n\n{get_stats_text()}", parse_mode="HTML"
        )
        await context.bot.send_photo(OWNER_ID, photo=generate_stats_chart(), caption="📈 Vizual statistika")
    except Exception as e:
        logger.warning(f"Kunlik statistika yuborishda xatolik: {e}")


# ----------------------------------------------------------------------
# ASOSIY MATN ROUTERI — pastki tugmalarni ushlaydi (faqat shaxsiy chatda)
# ----------------------------------------------------------------------
async def main_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    upsert_user(user)
    uid = user.id
    admin = is_admin(uid)

    if not admin:
        if is_blocked(uid):
            return  # bloklangan foydalanuvchiga javob berilmaydi
        if not await ensure_subscription(context.bot, uid, update.effective_chat.id):
            return

    # O'yin davomida faqat "to'xtatish" ishlaydi
    if uid in _sessions(context.application):
        if text == BTN_STOP:
            return await quiz_stop_command(update, context)
        await update.message.reply_text(
            "🎮 Viktorina davom etmoqda. To'xtatish uchun \"🔚 O'yinni to'xtatish\" tugmasini bosing 👇"
        )
        return

    if text == BTN_STOP:
        await update.message.reply_text("ℹ️ Hozir o'yin boshlanmagan.", reply_markup=menu_for(uid))
        return

    if text == BTN_GAME:
        return await game_start(update, context)
    if text == BTN_LEADERBOARD:
        return await leaderboard_view(update, context)
    if text == BTN_FAVORITES:
        return await favorites_view(update, context)

    if admin:
        if text == BTN_POST:
            return await post_start(update, context)
        if text == BTN_CATEGORIES:
            return await categories_menu(update, context)
        if text == BTN_ADMINS:
            return await admins_menu(update, context)
        if text == BTN_STATS:
            return await stats_menu(update, context)
        if text == BTN_USERS:
            return await users_menu(update, context)
        if text == BTN_HISTORY:
            return await history_view(update, context)
        if text == BTN_BACKUP:
            return await send_backup(update, context)

    row = db_query("SELECT id FROM categories WHERE name=?", (text,), fetch="one")
    if row:
        await send_category_content(context, update.effective_chat.id, uid, row[0], text)
    else:
        await update.message.reply_text(CHOOSE_BUTTON_TEXT, reply_markup=menu_for(uid))


# ----------------------------------------------------------------------
# UMUMIY
# ----------------------------------------------------------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Amal bekor qilindi.",
        reply_markup=menu_for(update.effective_user.id),
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Xatolik yuz berdi:", exc_info=context.error)


# ----------------------------------------------------------------------
# KEEP-ALIVE HTTP SERVER (Render bepul tarifida bot uxlab qolmasligi uchun)
# ----------------------------------------------------------------------
keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def keep_alive_home():
    return "Bot ishlayapti ✅"


def run_keep_alive_server():
    port = int(os.getenv("PORT", "10000"))
    logger.info(f"🌐 Keep-alive server {port}-portda ishga tushmoqda...")
    try:
        keep_alive_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    except Exception as e:
        logger.error(f"❌ Keep-alive server ishga tushmadi: {e}")


def start_keep_alive_thread():
    thread = threading.Thread(target=run_keep_alive_server, daemon=True)
    thread.start()
    time.sleep(2)  # Flask portga ulanib ulgurishi uchun qisqa kutish


# ----------------------------------------------------------------------
# ISHGA TUSHISH
# ----------------------------------------------------------------------
async def on_startup(app: Application):
    """Bot qayta ishga tushganda, bajarilmagan rejalashtirilgan postlarni qayta rejalashtiradi."""
    rows = db_query(
        "SELECT id, admin_chat_id, run_at FROM scheduled_posts WHERE status='pending'", fetch="all"
    )
    now = now_tk()
    for post_id, admin_chat_id, run_at in rows:
        try:
            when = datetime.fromisoformat(run_at)
        except ValueError:
            continue
        if when > now:
            schedule_post(app, post_id, when)
        elif now - when <= timedelta(hours=6):
            schedule_post(app, post_id, now + timedelta(seconds=5))   # kechikkan — hozir yuboramiz
        else:
            db_query("UPDATE scheduled_posts SET status='missed' WHERE id=?", (post_id,))
            try:
                await app.bot.send_message(
                    admin_chat_id,
                    f"⚠️ {when.strftime('%d.%m.%Y %H:%M')} da rejalashtirilgan post bot o'chiq "
                    f"bo'lgani uchun yuborilmadi. Uni qaytadan rejalashtiring.",
                )
            except Exception:
                pass


def main():
    if not BOT_TOKEN or not OWNER_ID:
        print("⚠️  Iltimos, avval BOT_TOKEN va OWNER_ID muhit o'zgaruvchilarini to'ldiring!")
        return

    # Portni ENG BIRINCHI bo'lib ochamiz — Render buni tezroq aniqlashi uchun
    start_keep_alive_thread()

    db_init()
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    private = filters.ChatType.PRIVATE

    post_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{re.escape(BTN_POST)}$") & private, post_start)],
        states={
            WAIT_POST_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, post_receive)],
            WAIT_POST_CONFIRM: [CallbackQueryHandler(post_confirm, pattern="^post_(confirm|cancel|schedule)$")],
            WAIT_POST_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, post_time_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    addcat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(addcat_start, pattern="^admin_addcat$")],
        states={
            WAIT_CAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcat_name)],
            WAIT_CAT_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, addcat_content)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    editcat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(editcat_start, pattern="^admin_editcat:")],
        states={
            WAIT_EDIT_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, editcat_content)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    contact_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{re.escape(BTN_CONTACT)}$") & private, contact_start)],
        states={
            WAIT_CONTACT_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, contact_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    admin_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_reply_start, pattern="^admin_reply:")],
        states={
            WAIT_ADMIN_REPLY: [MessageHandler(filters.ALL & ~filters.COMMAND, admin_reply_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(addadmin_start, pattern="^admin_addadmin$"),
            CallbackQueryHandler(deladmin_start, pattern="^admin_deladmin$"),
        ],
        states={
            WAIT_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, addadmin_receive)],
            WAIT_DEL_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, deladmin_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    search_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{re.escape(BTN_SEARCH)}$") & private, search_start)],
        states={
            WAIT_SEARCH_TERM: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    block_user_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(block_user_start, pattern="^block_user_start$")],
        states={
            WAIT_BLOCK_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, block_user_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    restore_conv = ConversationHandler(
        entry_points=[CommandHandler("restore", restore_start, filters=private)],
        states={
            WAIT_RESTORE_FILE: [MessageHandler(filters.Document.ALL, restore_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(post_conv)
    app.add_handler(addcat_conv)
    app.add_handler(editcat_conv)
    app.add_handler(contact_conv)
    app.add_handler(admin_reply_conv)
    app.add_handler(admin_conv)
    app.add_handler(search_conv)
    app.add_handler(block_user_conv)
    app.add_handler(restore_conv)

    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("stop", quiz_stop_command, filters=private))
    app.add_handler(CommandHandler("cancel", cancel, filters=private))

    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(quiz_level_callback, pattern="^quizlvl:"))
    app.add_handler(CallbackQueryHandler(quiz_stop_callback, pattern="^quiz_stop$"))
    app.add_handler(CallbackQueryHandler(reset_leaderboard_confirm, pattern="^reset_leaderboard_confirm$"))
    app.add_handler(CallbackQueryHandler(reset_leaderboard_execute, pattern="^reset_leaderboard_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(toggle_favorite_callback, pattern="^togglefav:"))
    app.add_handler(CallbackQueryHandler(view_category_callback, pattern="^viewcat:"))
    app.add_handler(CallbackQueryHandler(unblock_user_callback, pattern="^unblock_user:"))
    app.add_handler(CallbackQueryHandler(delcat, pattern="^admin_delcat:"))
    app.add_handler(CallbackQueryHandler(move_category, pattern="^admin_cat(up|down):"))

    # Viktorina javoblari va guruh/chat a'zoligi o'zgarishlari
    app.add_handler(PollAnswerHandler(quiz_poll_answer))
    app.add_handler(ChatMemberHandler(my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    # Guruhlarni ro'yxatga olish (alohida guruhda — asosiy handlerlarga xalaqit bermaydi)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, track_group), group=1)

    # Pastki tugmalarni ushlaydigan umumiy router — eng oxirida, faqat shaxsiy chatda
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, main_router))

    app.add_error_handler(error_handler)

    if app.job_queue:
        # Hamma vaqtlar Toshkent (UTC+5) bo'yicha
        app.job_queue.run_daily(daily_stats_job, time=dtime(9, 0, tzinfo=TZ))
        # Har kuni 18:00 da ishga tushadi, lekin faqat shanba kuni savol yuboradi
        app.job_queue.run_daily(weekly_quiz_job, time=dtime(18, 0, tzinfo=TZ))
    else:
        logger.warning("JobQueue mavjud emas — 'python-telegram-bot[job-queue]' o'rnatilganini tekshiring.")

    print("✅ Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
