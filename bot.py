import logging
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO

import matplotlib
matplotlib.use("Agg")  # server muhitida ekransiz ishlash uchun
import matplotlib.pyplot as plt
import yt_dlp

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
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
    # {"id": "@ikkinchi_kanal", "title": "📢 Ikkinchi kanal", "url": "https://t.me/ikkinchi_kanal"},
]

DB_PATH = "admin_panel.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Admin panel tugmalari matni (o'zgartirmang — kod shu matnlarni tekshiradi)
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
) = range(12)


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
    # Eski bazalarda 'language' ustuni bo'lmasligi mumkin — xavfsiz qo'shamiz
    try:
        cur.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'uz'")
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
            content_type TEXT,   -- 'text' | 'photo' | 'document' | 'video'
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
    try:
        cur.execute("ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0")
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS game_scores (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            score INTEGER DEFAULT 0,
            games_played INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    if OWNER_ID:
        cur.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (OWNER_ID, OWNER_ID, datetime.now().isoformat()),
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


def upsert_user(user):
    now = datetime.now().isoformat()
    existing = db_query("SELECT user_id FROM users WHERE user_id=?", (user.id,), fetch="one")
    if existing:
        db_query(
            "UPDATE users SET username=?, first_name=?, last_active=?, subscribed=1 WHERE user_id=?",
            (user.username, user.first_name, now, user.id),
        )
    else:
        db_query(
            "INSERT INTO users (user_id, username, first_name, joined_at, last_active, subscribed) "
            "VALUES (?, ?, ?, ?, ?, 1)",
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
        (admin_id, action, detail, datetime.now().isoformat()),
    )


# ----------------------------------------------------------------------
# KO'P TILLILIK (uz / ru / en)
# ----------------------------------------------------------------------
TRANSLATIONS = {
    "choose_language": {
        "uz": "🌐 Tilni tanlang:",
        "ru": "🌐 Выберите язык:",
        "en": "🌐 Choose your language:",
    },
    "welcome": {
        "uz": "👋 Xush kelibsiz!\n\nKerakli bo'limni tanlang:",
        "ru": "👋 Добро пожаловать!\n\nВыберите нужный раздел:",
        "en": "👋 Welcome!\n\nPlease choose a section:",
    },
    "no_sections": {
        "uz": "ℹ️ Hozircha bo'limlar yo'q",
        "ru": "ℹ️ Разделов пока нет",
        "en": "ℹ️ No sections yet",
    },
    "choose_button": {
        "uz": "Iltimos, quyidagi tugmalardan birini tanlang 👇",
        "ru": "Пожалуйста, выберите одну из кнопок ниже 👇",
        "en": "Please choose one of the buttons below 👇",
    },
    "subscribe_prompt": {
        "uz": "🔒 Botdan foydalanish uchun avval quyidagi kanal(lar)ga obuna bo'ling, so'ng \"✅ Tekshirish\" tugmasini bosing:",
        "ru": "🔒 Чтобы пользоваться ботом, сначала подпишитесь на канал(ы) ниже, затем нажмите \"✅ Проверить\":",
        "en": "🔒 To use the bot, first subscribe to the channel(s) below, then press \"✅ Check\":",
    },
    "check_button": {
        "uz": "✅ Tekshirish",
        "ru": "✅ Проверить",
        "en": "✅ Check",
    },
    "not_subscribed_alert": {
        "uz": "❌ Siz hali barcha kanallarga obuna bo'lmagansiz.",
        "ru": "❌ Вы ещё не подписались на все каналы.",
        "en": "❌ You haven't subscribed to all channels yet.",
    },
    "subscribed_thanks": {
        "uz": "✅ Rahmat! Endi botdan to'liq foydalanishingiz mumkin.\n\nKerakli bo'limni tanlang:",
        "ru": "✅ Спасибо! Теперь вы можете полностью пользоваться ботом.\n\nВыберите раздел:",
        "en": "✅ Thank you! You can now fully use the bot.\n\nChoose a section:",
    },
    "contact_button": {
        "uz": "📩 Keyhonga murojaat",
        "ru": "📩 Написать в Keyhon",
        "en": "📩 Contact Keyhon",
    },
    "contact_prompt": {
        "uz": "✍️ Xabaringizni yozing (matn, rasm yoki fayl bo'lishi mumkin), adminlarga yetkazamiz:\n\nBekor qilish uchun /cancel",
        "ru": "✍️ Напишите ваше сообщение (текст, фото или файл), мы передадим его администраторам:\n\nОтмена: /cancel",
        "en": "✍️ Write your message (text, photo, or file), we'll pass it to the admins:\n\nCancel: /cancel",
    },
    "contact_sent": {
        "uz": "✅ Xabaringiz yuborildi! Tez orada javob olasiz.",
        "ru": "✅ Ваше сообщение отправлено! Скоро получите ответ.",
        "en": "✅ Your message has been sent! You'll get a reply soon.",
    },
}


def t(key: str, lang: str) -> str:
    entry = TRANSLATIONS.get(key, {})
    return entry.get(lang, entry.get("uz", key))


LANGUAGE_NAMES = {"uz": "🇺🇿 O'zbekcha", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}


def get_user_lang(user_id: int) -> str:
    row = db_query("SELECT language FROM users WHERE user_id=?", (user_id,), fetch="one")
    if row and row[0]:
        return row[0]
    return "uz"


def set_user_lang(user_id: int, lang: str):
    db_query("UPDATE users SET language=? WHERE user_id=?", (lang, user_id))


# Tildan qat'i nazar, murojaat tugmasini aniqlash uchun barcha variantlar
CONTACT_BTN_TEXTS = list(TRANSLATIONS["contact_button"].values())
CONTACT_BTN_PATTERN = f"^({'|'.join(re.escape(x) for x in CONTACT_BTN_TEXTS)})$"



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


def build_subscribe_keyboard(missing, lang: str = "uz"):
    keyboard = [[InlineKeyboardButton(ch["title"], url=ch["url"])] for ch in missing]
    keyboard.append([InlineKeyboardButton(t("check_button", lang), callback_data="check_sub")])
    return InlineKeyboardMarkup(keyboard)


async def send_subscribe_prompt(bot, chat_id, missing, lang: str = "uz"):
    await bot.send_message(
        chat_id,
        t("subscribe_prompt", lang),
        reply_markup=build_subscribe_keyboard(missing, lang),
    )


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = get_user_lang(query.from_user.id)
    missing = await get_not_subscribed(context.bot, query.from_user.id)
    if missing:
        await query.answer(t("not_subscribed_alert", lang), show_alert=True)
        return
    await query.answer(t("check_button", lang))
    await query.message.delete()
    upsert_user(query.from_user)
    await context.bot.send_message(
        query.message.chat_id,
        t("subscribed_thanks", lang),
        reply_markup=build_user_menu(lang),
    )


# ----------------------------------------------------------------------
# REPLY KEYBOARD (PASTKI TUGMALAR) QURISH
# ----------------------------------------------------------------------
def build_user_menu(lang: str = "uz"):
    categories = get_categories()
    rows = []
    row = []
    for cat_id, name in categories:
        row.append(KeyboardButton(name))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows = [[KeyboardButton(t("no_sections", lang))]]
    rows.append([KeyboardButton(BTN_SEARCH), KeyboardButton(BTN_FAVORITES)])
    rows.append([KeyboardButton(BTN_GAME), KeyboardButton(BTN_LEADERBOARD)])
    rows.append([KeyboardButton(t("contact_button", lang))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_admin_menu():
    rows = [
        [KeyboardButton(BTN_POST), KeyboardButton(BTN_CATEGORIES)],
        [KeyboardButton(BTN_ADMINS), KeyboardButton(BTN_STATS)],
        [KeyboardButton(BTN_USERS), KeyboardButton(BTN_HISTORY)],
        [KeyboardButton(BTN_BACKUP)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ----------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------
def build_language_keyboard():
    keyboard = [
        [InlineKeyboardButton(LANGUAGE_NAMES["uz"], callback_data="setlang:uz")],
        [InlineKeyboardButton(LANGUAGE_NAMES["ru"], callback_data="setlang:ru")],
        [InlineKeyboardButton(LANGUAGE_NAMES["en"], callback_data="setlang:en")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    if is_admin(user.id):
        await update.message.reply_text(
            "🛠 Xush kelibsiz, Admin!\nQuyidagi tugmalar orqali boshqaring:",
            reply_markup=build_admin_menu(),
        )
        return

    row = db_query("SELECT language FROM users WHERE user_id=?", (user.id,), fetch="one")
    if not row or not row[0]:
        await update.message.reply_text(
            t("choose_language", "uz"), reply_markup=build_language_keyboard()
        )
        return

    missing = await get_not_subscribed(context.bot, user.id)
    if missing:
        await send_subscribe_prompt(context.bot, update.effective_chat.id, missing, row[0])
        return

    await update.message.reply_text(
        t("welcome", row[0]),
        reply_markup=build_user_menu(row[0]),
    )


async def setlang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":")[1]
    set_user_lang(query.from_user.id, lang)
    await query.message.delete()

    missing = await get_not_subscribed(context.bot, query.from_user.id)
    if missing:
        await send_subscribe_prompt(context.bot, query.message.chat_id, missing, lang)
        return

    await context.bot.send_message(
        query.message.chat_id,
        t("welcome", lang),
        reply_markup=build_user_menu(lang),
    )


# ----------------------------------------------------------------------
# 📥 INSTAGRAM / YOUTUBE VIDEO YUKLAB OLISH — hammaga ochiq
# ----------------------------------------------------------------------
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
INSTAGRAM_RE = re.compile(r"instagram\.com", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot uchun 50 MB limit


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_RE.search(text))


def is_instagram_url(text: str) -> bool:
    return bool(INSTAGRAM_RE.search(text))


def is_generic_url(text: str) -> bool:
    return bool(URL_RE.search(text))


def _download_media_sync(url: str, work_dir: str):
    """yt-dlp orqali video va audio faylni diskka yuklab oladi (sinxron, alohida oqimda ishlaydi)."""
    result = {"video_path": None, "audio_path": None, "title": None, "error": None}

    # --- Video (ffmpeg shart bo'lmasligi uchun progressiv mp4 formatini tanlaymiz) ---
    video_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(work_dir, "video.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(video_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            result["title"] = info.get("title", "Video")
            result["video_path"] = ydl.prepare_filename(info)
    except Exception as e:
        result["error"] = str(e)
        return result

    # --- Faqat audio (musiqa) — ffmpeg shart bo'lmasligi uchun konvertatsiyasiz ---
    audio_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": os.path.join(work_dir, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            result["audio_path"] = ydl.prepare_filename(info)
    except Exception:
        pass  # audio topilmasa ham video baribir yuborilaveradi

    return result


async def handle_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    status_msg = await update.message.reply_text("⏳ Video yuklab olinmoqda, biroz kuting...")

    work_dir = tempfile.mkdtemp(prefix=f"dl_{uuid.uuid4().hex}_")
    try:
        result = await _run_download_in_thread(url, work_dir)

        if result.get("error"):
            await status_msg.edit_text(
                "❌ Video topilmadi yoki yuklab bo'lmadi. Havola to'g'ri ekanini va "
                "video ochiq (public) ekanini tekshiring."
            )
            return

        video_path = result.get("video_path")
        audio_path = result.get("audio_path")
        title = result.get("title", "Video")

        if video_path and os.path.exists(video_path):
            if os.path.getsize(video_path) <= MAX_TELEGRAM_FILE_SIZE:
                with open(video_path, "rb") as f:
                    await update.message.reply_video(video=f, caption=f"🎬 {title}")
            else:
                await update.message.reply_text(
                    "⚠️ Video hajmi 50 MB dan katta — Telegram orqali yuborib bo'lmaydi."
                )
        else:
            await status_msg.edit_text("❌ Video yuklab bo'lmadi.")
            return

        if audio_path and os.path.exists(audio_path):
            if os.path.getsize(audio_path) <= MAX_TELEGRAM_FILE_SIZE:
                with open(audio_path, "rb") as f:
                    await update.message.reply_audio(audio=f, title=title)

        await status_msg.delete()

    except Exception as e:
        logger.warning(f"Video yuklashda xatolik: {e}")
        await status_msg.edit_text("❌ Xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")
    finally:
        for fname in os.listdir(work_dir):
            try:
                os.remove(os.path.join(work_dir, fname))
            except OSError:
                pass
        try:
            os.rmdir(work_dir)
        except OSError:
            pass


async def _run_download_in_thread(url: str, work_dir: str):
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_media_sync, url, work_dir)


# ----------------------------------------------------------------------
# FOYDALANUVCHI: BO'LIM TANLASH
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
        (user_id, cat_id, datetime.now().isoformat()),
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
        await update.message.reply_text("⭐ Sizda hali sevimli bo'limlar yo'q.")
        return
    keyboard = [[InlineKeyboardButton(name, callback_data=f"viewcat:{cid}")] for cid, name in rows]
    await update.message.reply_text("⭐ Sevimli bo'limlaringiz:", reply_markup=InlineKeyboardMarkup(keyboard))


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Qidirmoqchi bo'lgan bo'lim nomini (yoki bir qismini) yozing:\n\nBekor qilish uchun /cancel")
    return WAIT_SEARCH_TERM


async def search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = update.message.text.strip()
    lang = get_user_lang(update.effective_user.id)
    rows = db_query(
        "SELECT id, name FROM categories WHERE name LIKE ? ORDER BY position",
        (f"%{term}%",), fetch="all",
    )
    if not rows:
        await update.message.reply_text("❌ Hech narsa topilmadi.", reply_markup=build_user_menu(lang))
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(name, callback_data=f"viewcat:{cid}")] for cid, name in rows]
    await update.message.reply_text(
        f"🔍 Topilgan natijalar ({len(rows)}):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 🎮 MINECRAFT VIKTORINA — hammaga ochiq o'yin
# ----------------------------------------------------------------------
_QUESTIONS_RAW = [
    ("Minecraft'da eng qattiq blok qaysi?", ["Toshdan", "Bedrock", "Obsidian", "Temir rudasi"], 1),
    ("Enderman qaysi blokdan qo'rqadi?", ["Suv", "Tosh", "Yog'och", "Qum"], 0),
    ("Nether portalini yasash uchun nima kerak?", ["Temir + olov", "Obsidian + olov", "Tosh + suv", "Yog'och + olov"], 1),
    ("Creeper portlaganda nima yo'qotadi (odatda)?", ["Hech narsa", "Sog'liq", "Bloklarni atrofda", "Inventarni"], 2),
    ("Eng zaif qurol materiali qaysi?", ["Yog'och", "Olmos", "Temir", "Oltin"], 0),
    ("Ender Dragon qayerda yashaydi?", ["Nether", "Overworld", "The End", "Okean"], 2),
    ("Qaysi mob tunda yonib ketadi (quyosh nurida)?", ["Zombi", "Enderman", "Creeper", "Spider"], 0),
    ("Eng qimmatli ruda qaysi (odatiy o'yinda)?", ["Temir", "Oltin", "Olmos", "Ko'mir"], 2),
    ("Golem qanday blokdan yasaladi?", ["Tosh", "Temir bloklari", "Yog'och", "Loy"], 1),
    ("TNT nima bilan portlatiladi?", ["Suv", "Olov/qo'zg'atuvchi", "Muz", "Shamol"], 1),
    ("Qaysi mob suvda yashaydi va o'q otadi?", ["Guardian", "Slime", "Witch", "Blaze"], 0),
    ("Netherite qanday olinadi?", ["Qazib", "Ancient Debris'ni eritib", "Sotib olib", "Baliq ovlab"], 1),
    ("Qaysi blok yorug'lik chiqaradi?", ["Tosh", "Glowstone", "Qum", "Loy"], 1),
    ("Villager bilan nima qilish mumkin?", ["Savdo qilish", "Uchish", "Suzish", "Portlash"], 0),
    ("Elytra nima uchun ishlatiladi?", ["Suzish", "Uchish/parvoz", "Qazish", "Ovqatlanish"], 1),
    ("Blaze qayerda uchraydi?", ["Overworld", "The End", "Nether", "Okean"], 2),
    ("Qaysi mob tuxum qo'yadi?", ["Sigir", "Tovuq", "Cho'chqa", "Qo'y"], 1),
    ("Bookshelf (kitob javoni) nima uchun kerak?", ["Ovqat", "Enchanting kuchini oshirish", "Yorug'lik", "Himoya"], 1),
    ("Piston qanday ishlaydi?", ["Bloklarni suradi", "Yorug'lik beradi", "Suv chiqaradi", "Ovoz chiqaradi"], 0),
    ("Qaysi biome'da kaktus ko'p o'sadi?", ["O'rmon", "Cho'l", "Tayga", "Okean"], 1),
    ("Redstone nima uchun ishlatiladi?", ["Ovqat pishirish", "Mexanizm/elektr sxemalari", "Suzish", "Qurol yasash"], 1),
    ("Qaysi mob 'silent' (ovozsiz) hujum qiladi?", ["Creeper", "Zombi", "Skeleton", "Spider"], 0),
    ("Diamond ruda odatda qaysi chuqurlikda ko'p topiladi?", ["Yer yuzida", "Juda chuqur, pastda", "Faqat tog'da", "Faqat suv ostida"], 1),
    ("Crafting Table necha katakli?", ["2x2", "3x3", "4x4", "1x1"], 1),
    ("Furnace (pech) nima uchun ishlatiladi?", ["Eritish/pishirish", "Uxlash", "Suzish", "Portlatish"], 0),
    ("Bed (karavot) nima uchun kerak?", ["Uxlash va spawn nuqtasini belgilash", "Ovqatlanish", "Uchish", "Qazish"], 0),
    ("Qaysi asbob eng tez qazadi (bir xil materialda)?", ["Ketmon", "Bolta", "Kilka", "Belkurak"], 2),
    ("Compass nimani ko'rsatadi?", ["Spawn nuqtasini", "Vaqtni", "Sog'liqni", "Ob-havoni"], 0),
    ("Clock (soat) nimani ko'rsatadi?", ["Kun/tun vaqtini", "Sog'liqni", "Yo'nalishni", "Masofani"], 0),
    ("Map (xarita) qanday yasaladi?", ["Qog'oz + kompas", "Qog'oz orqali", "Faqat sotib olinadi", "Temir orqali"], 1),
    ("Obsidian qanday hosil bo'ladi?", ["Lava + suv", "Tosh + olov", "Muz + issiqlik", "Qum + lava"], 0),
    ("Ghast qayerda uchraydi?", ["Overworld", "The End", "Nether", "Tog'da"], 2),
    ("Wither bossni chaqirish uchun nima kerak?", ["4 ta blok + 3 ta kalla", "Faqat kalla", "TNT", "Olmos qilich"], 0),
    ("Shulker qayerda topiladi?", ["Overworld", "End City", "Nether Fortress", "Okean"], 1),
    ("Qaysi mob teleportatsiya qila oladi?", ["Zombi", "Enderman", "Skeleton", "Spider"], 1),
    ("Beacon (mayoq) nima beradi?", ["Kuchaytiruvchi effektlar", "Ovqat", "Suv", "Uy qurish"], 0),
    ("Anvil (sandon) nima uchun kerak?", ["Buyumlarni ta'mirlash/nomlash", "Ovqat pishirish", "Uxlash", "Qazish"], 0),
    ("Loom nima uchun ishlatiladi?", ["Bayroq naqshlash", "Qurol yasash", "Ovqat pishirish", "Portlatish"], 0),
    ("Grindstone nima qiladi?", ["Enchant olib tashlaydi/ta'mirlaydi", "Ovqat beradi", "Uxlatadi", "Uchiradi"], 0),
    ("Cartography Table nima uchun kerak?", ["Xarita bilan ishlash", "Qurol yasash", "Ovqat pishirish", "Suzish"], 0),
    ("Fletching Table qaysi kasb bilan bog'liq?", ["Temirchi", "O'qchi (fletcher)", "Bog'bon", "Baliqchi"], 1),
    ("Smithing Table nima uchun ishlatiladi?", ["Netherite yangilash", "Ovqat pishirish", "Suzish", "Xarita yasash"], 0),
    ("Composter nima uchun ishlatiladi?", ["Chiqindidan suyuqlik/ o'g'it yasash", "Qurol yasash", "Uxlash", "Portlatish"], 0),
    ("Lodestone nima qiladi?", ["Kompasni o'ziga bog'laydi", "Yorug'lik beradi", "Ovqat beradi", "Portlaydi"], 0),
    ("Respawn Anchor qayerda ishlaydi?", ["Faqat Overworld'da", "Faqat Nether'da", "Faqat End'da", "Hamma joyda"], 1),
    ("Qaysi mob 'Iron Golem' ga hujum qilmaydi (odatda)?", ["Zombi", "Villager", "Skeleton", "Creeper"], 1),
    ("Silverfish qaysi blok ichida yashiringan bo'ladi?", ["Yog'och", "Tosh (infested)", "Qum", "Muz"], 1),
    ("Phantom qachon paydo bo'ladi?", ["Uzoq vaqt uxlamasa", "Ovqat yemasa", "Suvga tushsa", "Tog'ga chiqsa"], 0),
    ("Ravager qaysi mob bilan birga yuradi (raid'da)?", ["Villager", "Pillager", "Golem", "Wolf"], 1),
    ("Llama nima olib yura oladi?", ["Yuk (inventar)", "Suv", "Olov", "Hech narsa"], 0),
    ("Fox (tulki) qanday xatti-harakat qiladi?", ["Tunda faol", "Suvda yashaydi", "Uchadi", "Portlaydi"], 0),
    ("Bee (ari) nima yasaydi?", ["Asal", "Sut", "Yog'", "Un"], 0),
    ("Panda qaysi biome'da yashaydi?", ["Cho'l", "Bambuk o'rmoni", "Tayga", "Okean"], 1),
    ("Turtle (toshbaqa) nima qoldiradi?", ["Tuxum", "Asal", "Jun", "Sut"], 0),
    ("Dolphin (delfin) qanday yordam beradi?", ["Xazina topishga yo'l ko'rsatadi", "Ovqat beradi", "Uy quradi", "Qurol beradi"], 0),
    ("Axolotl qayerda topiladi?", ["Cho'lda", "Loy g'orlarida (suv ostida)", "Tog'da", "Nether'da"], 1),
    ("Warden qanday sezadi?", ["Ovoz va tebranish orqali", "Ko'rish orqali", "Hid orqali", "Umuman sezmaydi"], 0),
    ("Sculk Sensor nima qiladi?", ["Tovushni sezadi", "Yorug'lik beradi", "Suv chiqaradi", "Ovqat beradi"], 0),
    ("Allay nima qila oladi?", ["Belgilangan buyumlarni yig'ib beradi", "Jang qiladi", "Uchadi va portlaydi", "Ovqat pishiradi"], 0),
    ("Goat (echki) nima qila oladi?", ["Suzadi", "Sakrab, mob'larni itaradi", "Uchadi", "Portlaydi"], 1),
    ("Frog (qurbaqa) nimani ovlaydi?", ["Kichik slime'larni", "Zombini", "Skeletoni", "Enderman'ni"], 0),
    ("Piglin qanday buyumga qiziqadi?", ["Oltin", "Olmos", "Temir", "Yog'och"], 0),
    ("Hoglin qayerda yashaydi?", ["Overworld", "The End", "Nether", "Okean"], 2),
    ("Strider nimaning ustida yura oladi?", ["Lava", "Suv", "Muz", "Qum"], 0),
    ("Qaysi enchant qurolni 'uchqun' bilan yoqadi?", ["Sharpness", "Fire Aspect", "Knockback", "Unbreaking"], 1),
    ("Efficiency enchant nimaga ta'sir qiladi?", ["Qazish tezligiga", "Zarar miqdoriga", "Himoyaga", "Uchishga"], 0),
    ("Mending enchant nima qiladi?", ["Tajriba orqali ta'mirlaydi", "Zarar oshiradi", "Tezlik beradi", "Uchiradi"], 0),
    ("Sharpness enchant nimaga ta'sir qiladi?", ["Melee zarariga", "Himoyaga", "Tezlikka", "Qazishga"], 0),
    ("Protection enchant nima qiladi?", ["Umumiy himoyani oshiradi", "Zarar beradi", "Tezlashtiradi", "Uchiradi"], 0),
    ("Potion of Healing nima qiladi?", ["Sog'liqni tiklaydi", "Zaharlaydi", "Tezlashtiradi", "Ko'rinmas qiladi"], 0),
    ("Potion of Invisibility nima qiladi?", ["Ko'rinmas qiladi", "Tezlashtiradi", "Davolaydi", "Kuchaytiradi"], 0),
    ("Golden Apple qanday effekt beradi?", ["Regeneratsiya/Absorption", "Zaharlaydi", "Sekinlashtiradi", "Ko'rlik beradi"], 0),
    ("Nether Wart nima uchun ishlatiladi?", ["Potion (iksir) yasashda", "Ovqatda", "Qurol yasashda", "Uy qurishda"], 0),
    ("Brewing Stand nima uchun ishlatiladi?", ["Iksir tayyorlash", "Qurol yasash", "Ovqat pishirish", "Xarita yasash"], 0),
    ("Cauldron (qozon) nima uchun ishlatiladi?", ["Suv/iksir saqlash", "Ovqat pishirish", "Qazish", "Uchish"], 0),
    ("Qaysi ovqat eng ko'p to'yimlilik beradi?", ["Non", "Olma", "Steak (pishgan go'sht)", "Sabzi"], 2),
    ("Golden Carrot qanday ishlatiladi?", ["Ovqat/iksir uchun", "Qurol yasashda", "Uy qurishda", "Portlatishda"], 0),
    ("Suspicious Stew qanday ta'sir qilishi mumkin?", ["Turli tasodifiy effektlar", "Doim zaharlaydi", "Hech narsa qilmaydi", "Doim davolaydi"], 0),
    ("Honey Block (asal bloki) nima qiladi?", ["Sirg'anishni kamaytiradi", "Portlaydi", "Yorug'lik beradi", "Uchiradi"], 0),
    ("Slime Block nima qiladi?", ["Sakratadi/qaytaradi", "Yopishtiradi butunlay", "Portlaydi", "Yondiradi"], 0),
    ("Scaffolding nima uchun ishlatiladi?", ["Tez qurilish uchun vaqtinchalik", "Doimiy uy qurish", "Qazish", "Suzish"], 0),
    ("Lightning Rod nima qiladi?", ["Chaqmoqni tortadi", "Olov chiqaradi", "Suv chiqaradi", "Portlatadi"], 0),
    ("Target Block nima qiladi?", ["O'qqa tekkanda signal beradi", "Yorug'lik beradi", "Ovqat beradi", "Uy quradi"], 0),
    ("Observer bloki nimani kuzatadi?", ["Oldingi blokdagi o'zgarishni", "Mob harakatini", "O'yinchi joylashuvini", "Vaqtni"], 0),
    ("Hopper nima qiladi?", ["Buyumlarni tashiydi", "Suv tashiydi", "Yorug'lik beradi", "Portlatadi"], 0),
    ("Dispenser va Dropper farqi nimada?", ["Dispenser buyumni ishlatadi, Dropper tashlaydi", "Farqi yo'q", "Dropper portlaydi", "Dispenser yonadi"], 0),
    ("Command Block kim uchun mo'ljallangan?", ["Xarita/server yaratuvchilar uchun", "Oddiy o'yin uchun", "Faqat dekor", "Faqat Creative himoyasi"], 0),
    ("Barrier bloki nima qiladi?", ["Ko'rinmas devor yaratadi", "Yorug'lik beradi", "Portlatadi", "Suv chiqaradi"], 0),
    ("End Crystal nima uchun ishlatiladi?", ["Ender Dragon'ni davolash/portlatish", "Ovqat pishirish", "Uy qurish", "Xarita yasash"], 0),
    ("Chorus Fruit yeganda nima bo'ladi?", ["Tasodifiy teleport qiladi", "Davolaydi", "Zaharlaydi", "Uchiradi"], 0),
    ("Elytra qayerda topiladi?", ["End City'da", "Nether Fortress'da", "Villagerlarda", "Overworld tog'ida"], 0),
    ("Trident qanday quroldir?", ["Poseydon nayzasi (Drowned'dan)", "Kamon turi", "Qilich turi", "Belkurak turi"], 0),
    ("Drowned qayerda uchraydi?", ["Suv ostida", "Tog'da", "Nether'da", "The End'da"], 0),
    ("Pillager qanday qurol ishlatadi?", ["Kamon", "Qilich", "Bolta", "Belkurak"], 0),
    ("Vindicator qanday qurol ishlatadi?", ["Bolta", "Kamon", "Qilich", "Nayza"], 0),
    ("Evoker nima chaqira oladi?", ["Vex'larni va 'fang' hujumini", "Zombi armiyasini", "Yomg'irni", "Chaqmoqni"], 0),
    ("Raid (reyd) qanday boshlanadi?", ["Village'da 'Bad Omen' effekti bilan", "Tasodifiy", "Faqat tunda", "Faqat Nether'da"], 0),
    ("Totem of Undying nima qiladi?", ["O'limdan bir marta qutqaradi", "Tezlashtiradi", "Ko'rinmas qiladi", "Uchiradi"], 0),
    ("Qaysi blok orqali xarita chegarasi (World Border) belgilanadi?", ["Bedrock", "Bu maxsus tizim, blok emas", "Obsidian", "Barrier"], 1),
    ("Mycelium bloki qayerda uchraydi?", ["Mushroom Fields biome'da", "Cho'lda", "Tog'da", "Nether'da"], 0),
    ("Qaysi mob 'Snow Golem' yasaladi?", ["2 ta qor bloki + qovoq", "3 ta tosh", "Temir bloklari", "Yog'och"], 0),
    ("Sniffer mobining vazifasi nima?", ["Qadimiy urug'larni topish", "Jang qilish", "Uchish", "Suzish"], 0),
    ("Copper (mis) blok vaqt o'tishi bilan nima bo'ladi?", ["Oksidlanib rang o'zgartiradi", "Yo'qoladi", "Portlaydi", "Kattalashadi"], 0),
]

MINECRAFT_QUESTIONS = [
    {"q": q, "options": opts, "correct": c} for q, opts, c in _QUESTIONS_RAW
]


QUIZ_MAX_QUESTIONS = 100


def build_quiz_queue():
    """O'yin boshida 100 tagacha takrorlanmaydigan savol tartibini tuzadi."""
    total = min(QUIZ_MAX_QUESTIONS, len(MINECRAFT_QUESTIONS))
    indices = list(range(len(MINECRAFT_QUESTIONS)))
    random.shuffle(indices)
    return indices[:total]


async def send_quiz_question(update_or_query, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    queue = context.user_data.get("quiz_queue", [])
    if not queue:
        await finish_quiz(context, chat_id, manual_stop=False)
        return

    idx = queue.pop(0)
    context.user_data["quiz_current_idx"] = idx
    question = MINECRAFT_QUESTIONS[idx]

    correct_count = context.user_data.get("quiz_correct", 0)
    answered = context.user_data.get("quiz_answered", 0)
    total = context.user_data.get("quiz_total", QUIZ_MAX_QUESTIONS)

    keyboard = [
        [InlineKeyboardButton(opt_text, callback_data=f"quiz:{idx}:{opt_i}")]
        for opt_i, opt_text in enumerate(question["options"])
    ]
    keyboard.append([InlineKeyboardButton("🔚 O'yinni to'xtatish", callback_data="quiz_stop")])

    await context.bot.send_message(
        chat_id,
        f"🎮 <b>Minecraft Viktorina</b> | {answered}/{total} | ✅ {correct_count} to'g'ri\n\n❓ {question['q']}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def game_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = build_quiz_queue()
    context.user_data["quiz_queue"] = queue
    context.user_data["quiz_total"] = len(queue)
    context.user_data["quiz_correct"] = 0
    context.user_data["quiz_answered"] = 0
    await update.message.reply_text(
        f"🎮 Viktorina boshlandi! Jami {len(queue)} ta savol. "
        f"Xato javob bersangiz ham o'yin davom etadi — faqat \"🔚 To'xtatish\" tugmasi orqali chiqasiz."
    )
    await send_quiz_question(update, context, update.effective_chat.id)


def _save_game_result(user, score: int):
    row = db_query("SELECT score, games_played FROM game_scores WHERE user_id=?", (user.id,), fetch="one")
    if row:
        best_score = max(row[0], score)
        db_query(
            "UPDATE game_scores SET score=?, games_played=games_played+1, first_name=? WHERE user_id=?",
            (best_score, user.first_name, user.id),
        )
    else:
        db_query(
            "INSERT INTO game_scores (user_id, first_name, score, games_played) VALUES (?, ?, ?, 1)",
            (user.id, user.first_name, score),
        )


async def finish_quiz(context: ContextTypes.DEFAULT_TYPE, chat_id: int, manual_stop: bool, user=None):
    correct = context.user_data.get("quiz_correct", 0)
    answered = context.user_data.get("quiz_answered", 0)

    if user:
        _save_game_result(user, correct)

    reason = "O'yinni to'xtatdingiz." if manual_stop else "Barcha savollarga javob berdingiz!"
    await context.bot.send_message(
        chat_id,
        f"🏁 {reason}\n\n📊 Javob berilgan savollar: {answered}\n✅ To'g'ri javoblar: <b>{correct}</b>\n\n"
        f"🏆 Bu — sizning ushbu o'yindagi natijangiz (max {QUIZ_MAX_QUESTIONS} ball).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Qaytadan boshlash", callback_data="quiz_restart")]]),
    )
    for key in ("quiz_queue", "quiz_current_idx", "quiz_total", "quiz_correct", "quiz_answered"):
        context.user_data.pop(key, None)


async def quiz_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, q_idx_str, answer_idx_str = query.data.split(":")
    q_idx, answer_idx = int(q_idx_str), int(answer_idx_str)

    current_idx = context.user_data.get("quiz_current_idx")
    if current_idx != q_idx:
        await query.answer("Bu savol eskirgan, yangi savolga javob bering.", show_alert=True)
        return

    question = MINECRAFT_QUESTIONS[q_idx]
    is_correct = answer_idx == question["correct"]

    context.user_data["quiz_answered"] = context.user_data.get("quiz_answered", 0) + 1
    if is_correct:
        context.user_data["quiz_correct"] = context.user_data.get("quiz_correct", 0) + 1
        await query.answer("✅ To'g'ri!")
    else:
        correct_text = question["options"][question["correct"]]
        await query.answer(f"❌ Noto'g'ri! To'g'ri javob: {correct_text}", show_alert=True)

    await query.message.delete()

    queue = context.user_data.get("quiz_queue", [])
    if not queue:
        await finish_quiz(context, query.message.chat_id, manual_stop=False, user=query.from_user)
    else:
        await send_quiz_question(update, context, query.message.chat_id)


async def quiz_restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    queue = build_quiz_queue()
    context.user_data["quiz_queue"] = queue
    context.user_data["quiz_total"] = len(queue)
    context.user_data["quiz_correct"] = 0
    context.user_data["quiz_answered"] = 0
    await query.message.delete()
    await send_quiz_question(update, context, query.message.chat_id)


async def quiz_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await finish_quiz(context, query.message.chat_id, manual_stop=True, user=query.from_user)


async def leaderboard_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = db_query(
        "SELECT first_name, score FROM game_scores ORDER BY score DESC LIMIT 10", fetch="all"
    )
    if not top:
        text = "🏆 Hali hech kim viktorinada qatnashmagan."
    else:
        medals = ["🥇", "🥈", "🥉"]
        text = "🏆 <b>Eng yaxshi natijalar:</b>\n\n"
        for i, (name, score) in enumerate(top):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            text += f"{prefix} {name or 'Foydalanuvchi'} — {score} ball\n"

    keyboard = None
    if is_admin(update.effective_user.id):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Reytingni tozalash (0 ga tushirish)", callback_data="reset_leaderboard_confirm")]
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
        "⚠️ Rostdan ham BARCHA foydalanuvchilarning reytingini 0 ga tushirmoqchimisiz? Bu amalni ortga qaytarib bo'lmaydi.",
        reply_markup=keyboard,
    )


async def reset_leaderboard_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    if query.data == "reset_leaderboard_yes":
        db_query("UPDATE game_scores SET score=0, games_played=0")
        await query.message.edit_text("✅ Reyting tozalandi — barcha natijalar 0 ga tushirildi.")
    else:
        await query.message.edit_text("❌ Bekor qilindi, reyting o'zgarmadi.")

# ----------------------------------------------------------------------
# 📩 KEYHONGA MUROJAAT (foydalanuvchi ↔ admin to'g'ridan-to'g'ri xabar)
# ----------------------------------------------------------------------
async def contact_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(t("contact_prompt", lang))
    return WAIT_CONTACT_MESSAGE


async def contact_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = get_user_lang(user.id)
    msg = update.message

    admins = db_query("SELECT user_id FROM admins", fetch="all")
    username_part = f"@{user.username}" if user.username else "(username yo'q)"
    header = (
        f"📩 <b>Yangi murojaat!</b>\n"
        f"👤 {user.first_name or ''} {username_part}\n"
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

    await update.message.reply_text(t("contact_sent", lang), reply_markup=build_user_menu(lang))
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
        await update.message.reply_text("Xatolik: kimga javob berish noma'lum.")
        return ConversationHandler.END

    try:
        await update.message.copy(chat_id=target_user_id)
        await update.message.reply_text("✅ Javob foydalanuvchiga yuborildi.", reply_markup=build_admin_menu())
    except Exception as e:
        await update.message.reply_text(f"❌ Yuborishda xatolik: foydalanuvchi botni bloklagan bo'lishi mumkin.")
        logger.warning(f"Admin javobini yuborishda xatolik ({target_user_id}): {e}")

    context.user_data.pop("reply_target", None)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# 📢 POST YUBORISH (BROADCAST) — faqat admin
# ----------------------------------------------------------------------
async def do_broadcast(bot, msg, admin_id) -> tuple:
    """Barcha obunachilarga xabar yuboradi va natijani bazaga yozadi."""
    users = db_query("SELECT user_id FROM users WHERE subscribed=1", fetch="all")
    total_sent, total_failed = 0, 0
    for (uid,) in users:
        try:
            await msg.copy(chat_id=uid)
            total_sent += 1
        except Exception as e:
            total_failed += 1
            logger.warning(f"Yuborishda xatolik ({uid}): {e}")

    db_query(
        "INSERT INTO broadcasts (admin_id, sent_at, total_sent, total_failed) VALUES (?, ?, ?, ?)",
        (admin_id, datetime.now().isoformat(), total_sent, total_failed),
    )
    return total_sent, total_failed


async def scheduled_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue orqali belgilangan vaqtda ishga tushadigan post yuborish."""
    data = context.job.data
    msg = data["message"]
    admin_id = data["admin_id"]
    chat_id = data["admin_chat_id"]

    sent, failed = await do_broadcast(context.bot, msg, admin_id)
    try:
        await context.bot.send_message(
            chat_id,
            f"✅ Rejalashtirilgan post yuborildi!\n📤 Yetib bordi: {sent}\n⚠️ Xato: {failed}",
        )
    except Exception as e:
        logger.warning(f"Admin'ga xabar berishda xatolik: {e}")


async def post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "📢 Barcha obunachilarga yuboriladigan xabarni yuboring "
        "(matn, rasm, video yoki fayl bo'lishi mumkin).\n\nBekor qilish uchun /cancel"
    )
    return WAIT_POST_CONTENT


async def post_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["post_message"] = update.message
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Hozir yuborish", callback_data="post_confirm")],
        [InlineKeyboardButton("🕒 Vaqt belgilash", callback_data="post_schedule")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="post_cancel")],
    ])
    await update.message.reply_text("Xabarni qachon yuborishni tanlang:", reply_markup=keyboard)
    return WAIT_POST_CONFIRM


async def post_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_cancel":
        await query.message.edit_text("❌ Bekor qilindi.")
        return ConversationHandler.END

    if query.data == "post_schedule":
        await query.message.edit_text(
            "🕒 Xabarni qachon yuborishni kiriting.\n\n"
            "Format: <code>KK.OO.YYYY SS:DD</code>\n"
            "Masalan: <code>15.08.2026 09:30</code>\n\n"
            "Bekor qilish uchun /cancel",
            parse_mode="HTML",
        )
        return WAIT_POST_TIME

    msg = context.user_data.get("post_message")
    if not msg:
        await query.message.edit_text("Xatolik: xabar topilmadi.")
        return ConversationHandler.END

    await query.message.edit_text("⏳ Yuborilmoqda...")
    total_sent, total_failed = await do_broadcast(context.bot, msg, query.from_user.id)
    await query.message.edit_text(
        f"✅ Post yuborildi!\n📤 Yetib bordi: {total_sent}\n⚠️ Xato: {total_failed}"
    )
    context.user_data.pop("post_message", None)
    return ConversationHandler.END


async def post_time_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        when = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Format noto'g'ri. Masalan: <code>15.08.2026 09:30</code>\n\nQaytadan kiriting yoki /cancel",
            parse_mode="HTML",
        )
        return WAIT_POST_TIME

    if when <= datetime.now():
        await update.message.reply_text("❌ Bu vaqt allaqachon o'tib ketgan. Kelajakdagi vaqt kiriting.")
        return WAIT_POST_TIME

    msg = context.user_data.get("post_message")
    if not msg:
        await update.message.reply_text("Xatolik: xabar topilmadi.")
        return ConversationHandler.END

    context.application.job_queue.run_once(
        scheduled_broadcast_job,
        when=when,
        data={
            "message": msg,
            "admin_id": update.effective_user.id,
            "admin_chat_id": update.effective_chat.id,
        },
        name=f"broadcast_{update.effective_user.id}_{when.isoformat()}",
    )
    await update.message.reply_text(
        f"✅ Post {when.strftime('%d.%m.%Y %H:%M')} da avtomatik yuborilishi rejalashtirildi!",
        reply_markup=build_admin_menu(),
    )
    context.user_data.pop("post_message", None)
    return ConversationHandler.END

# ----------------------------------------------------------------------
# 📂 BO'LIMLAR (KATEGORIYALAR) — faqat admin
# ----------------------------------------------------------------------
async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    categories = get_categories()
    keyboard = []
    for i, (cat_id, name) in enumerate(categories):
        row = [
            InlineKeyboardButton("⬆️", callback_data=f"admin_catup:{cat_id}"),
            InlineKeyboardButton("⬇️", callback_data=f"admin_catdown:{cat_id}"),
            InlineKeyboardButton(f"✏️ {name}", callback_data=f"admin_editcat:{cat_id}"),
            InlineKeyboardButton(f"🗑 {name}", callback_data=f"admin_delcat:{cat_id}"),
        ]
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Yangi bo'lim qo'shish", callback_data="admin_addcat")])
    await update.message.reply_text(
        "📂 Bo'limlar ro'yxati (⬆️⬇️ — tartib, ✏️ — tahrirlash, 🗑 — o'chirish):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def move_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    direction, cat_id = query.data.split(":")
    cat_id = int(cat_id)

    categories = get_categories()
    ids = [c[0] for c in categories]
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


async def editcat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    cat_id = int(query.data.split(":")[1])
    context.user_data["edit_cat_id"] = cat_id
    await query.message.reply_text(
        "✏️ Bu bo'lim uchun yangi kontent yuboring (matn, rasm, video yoki fayl):\n\nBekor qilish uchun /cancel"
    )
    return WAIT_EDIT_CONTENT


async def editcat_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = context.user_data.get("edit_cat_id")
    msg = update.message

    if msg.photo:
        content_type, file_id, text = "photo", msg.photo[-1].file_id, msg.caption
    elif msg.document:
        content_type, file_id, text = "document", msg.document.file_id, msg.caption
    elif msg.video:
        content_type, file_id, text = "video", msg.video.file_id, msg.caption
    else:
        content_type, file_id, text = "text", None, msg.text

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
    await query.message.reply_text("Yangi bo'lim nomini kiriting (masalan: 'Narxlar'):\n\nBekor qilish uchun /cancel")
    return WAIT_CAT_NAME


async def addcat_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_cat_name"] = update.message.text
    await update.message.reply_text("Endi shu bo'lim uchun kontent yuboring (matn, rasm, video yoki fayl):")
    return WAIT_CAT_CONTENT


async def addcat_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get("new_cat_name", "Nomsiz")
    msg = update.message

    if msg.photo:
        content_type, file_id, text = "photo", msg.photo[-1].file_id, msg.caption
    elif msg.document:
        content_type, file_id, text = "document", msg.document.file_id, msg.caption
    elif msg.video:
        content_type, file_id, text = "video", msg.video.file_id, msg.caption
    else:
        content_type, file_id, text = "text", None, msg.text

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
        (new_id, update.effective_user.id, datetime.now().isoformat()),
    )
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
    text += "\n".join(f"• {name or ''} (ID: {uid})" for uid, name in blocked) if blocked else "(hozircha yo'q)"

    keyboard = [[InlineKeyboardButton("➕ Foydalanuvchini bloklash", callback_data="block_user_start")]]
    for uid, name in blocked:
        keyboard.append([InlineKeyboardButton(f"✅ {name or uid} — blokdan chiqarish", callback_data=f"unblock_user:{uid}")])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def block_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text("Bloklamoqchi bo'lgan foydalanuvchining Telegram ID raqamini yuboring:\n\nBekor qilish uchun /cancel")
    return WAIT_BLOCK_USER_ID


async def block_user_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Iltimos, faqat raqam yuboring.")
        return WAIT_BLOCK_USER_ID

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
        date_str = created_at.split("T")[0] + " " + created_at.split("T")[1][:5]
        text += f"• {date_str} — {action}"
        if detail:
            text += f" ({detail})"
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
            filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
            caption="💾 Ma'lumotlar bazasining joriy zaxira nusxasi",
        )


# ----------------------------------------------------------------------
# 📊 TO'LIQ STATISTIKA — faqat admin
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# STATISTIKA GRAFIGI
# ----------------------------------------------------------------------
def generate_stats_chart():
    """Oxirgi 7 kunlik yangi foydalanuvchilar va bo'limlar bo'yicha bosilishlar grafigi."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))

    # --- 1) Oxirgi 7 kunlik yangi foydalanuvchilar ---
    days, counts = [], []
    for i in range(6, -1, -1):
        day = datetime.now() - timedelta(days=i)
        day_start = day.strftime("%Y-%m-%d")
        row = db_query(
            "SELECT COUNT(*) FROM users WHERE joined_at LIKE ?",
            (f"{day_start}%",), fetch="one",
        )
        days.append(day.strftime("%d.%m"))
        counts.append(row[0] if row else 0)

    ax1.plot(days, counts, marker="o", color="#4C6EF5")
    ax1.set_title("Yangi foydalanuvchilar (7 kun)")
    ax1.set_ylabel("Kishi")
    ax1.grid(alpha=0.3)

    # --- 2) Bo'limlar bo'yicha bosilishlar ---
    top = db_query("""
        SELECT c.name, COUNT(cl.id) as cnt
        FROM clicks cl JOIN categories c ON c.id = cl.category_id
        GROUP BY c.id ORDER BY cnt DESC LIMIT 5
    """, fetch="all")

    if top:
        names = [row[0] for row in top]
        vals = [row[1] for row in top]
        ax2.barh(names, vals, color="#12B886")
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

    day_ago = (datetime.now() - timedelta(days=1)).isoformat()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    active_24h = db_query("SELECT COUNT(*) FROM users WHERE last_active >= ?", (day_ago,), fetch="one")[0]
    active_7d = db_query("SELECT COUNT(*) FROM users WHERE last_active >= ?", (week_ago,), fetch="one")[0]

    top_categories = db_query("""
        SELECT c.name, COUNT(cl.id) as cnt
        FROM clicks cl JOIN categories c ON c.id = cl.category_id
        GROUP BY c.id ORDER BY cnt DESC LIMIT 5
    """, fetch="all")

    last_broadcasts = db_query("""
        SELECT sent_at, total_sent, total_failed FROM broadcasts ORDER BY id DESC LIMIT 3
    """, fetch="all")

    text = (
        "📊 <b>To'liq statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
        f"✅ Obuna bo'lganlar: <b>{subscribed}</b>\n"
        f"🟢 Faol (24 soat): <b>{active_24h}</b>\n"
        f"🟢 Faol (7 kun): <b>{active_7d}</b>\n\n"
        "🔥 <b>Eng ko'p bosilgan bo'limlar:</b>\n"
    )
    if top_categories:
        for name, cnt in top_categories:
            text += f"  • {name}: {cnt} marta\n"
    else:
        text += "  (hali ma'lumot yo'q)\n"

    text += "\n📢 <b>So'nggi postlar:</b>\n"
    if last_broadcasts:
        for sent_at, sent, failed in last_broadcasts:
            date_str = sent_at.split("T")[0]
            text += f"  • {date_str}: {sent} ta yetdi, {failed} ta xato\n"
    else:
        text += "  (hali post yuborilmagan)\n"

    return text


async def stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = get_stats_text()
    await update.message.reply_text(text, parse_mode="HTML")

    try:
        chart = generate_stats_chart()
        await update.message.reply_photo(photo=chart, caption="📈 Vizual statistika")
    except Exception as e:
        logger.warning(f"Grafik yasashda xatolik: {e}")


async def daily_stats_job(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni belgilangan vaqtda OWNER'ga avtomatik statistika yuboradi."""
    if not OWNER_ID:
        return
    try:
        text = get_stats_text()
        await context.bot.send_message(OWNER_ID, f"🕐 <b>Kunlik hisobot</b>\n\n{text}", parse_mode="HTML")
        chart = generate_stats_chart()
        await context.bot.send_photo(OWNER_ID, photo=chart, caption="📈 Vizual statistika")
    except Exception as e:
        logger.warning(f"Kunlik statistika yuborishda xatolik: {e}")


# ----------------------------------------------------------------------
# ASOSIY MATN ROUTERI — pastki tugmalarni ushlaydi
# ----------------------------------------------------------------------
async def main_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    upsert_user(user)

    # ---- VIDEO HAVOLASI (hammaga ochiq — YouTube/Instagram) ----
    if is_generic_url(text):
        if is_youtube_url(text) or is_instagram_url(text):
            await handle_video_link(update, context, text.strip())
        else:
            await update.message.reply_text(
                "❌ Faqat Instagram va YouTube havolalari orqali video yuklab olish mumkin."
            )
        return

    # ---- O'YIN (hammaga ochiq) ----
    if text == BTN_GAME:
        return await game_start(update, context)
    if text == BTN_LEADERBOARD:
        return await leaderboard_view(update, context)

    # ---- ADMIN tugmalari ----
    if is_admin(user.id):
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
        if text == BTN_LEADERBOARD:
            return await leaderboard_view(update, context)
        # admin biror bo'lim nomini bossa ham ko'rsatib qo'yamiz
        row = db_query("SELECT id FROM categories WHERE name=?", (text,), fetch="one")
        if row:
            await send_category_content(context, update.effective_chat.id, user.id, row[0], text)
        return

    # ---- ODDIY FOYDALANUVCHI ----
    lang = get_user_lang(user.id)

    # Til hali tanlanmagan bo'lsa
    row_lang = db_query("SELECT language FROM users WHERE user_id=?", (user.id,), fetch="one")
    if not row_lang or not row_lang[0]:
        await update.message.reply_text(t("choose_language", "uz"), reply_markup=build_language_keyboard())
        return

    if is_blocked(user.id):
        return  # bloklangan foydalanuvchiga javob berilmaydi

    missing = await get_not_subscribed(context.bot, user.id)
    if missing:
        await send_subscribe_prompt(context.bot, update.effective_chat.id, missing, lang)
        return

    if text == BTN_FAVORITES:
        return await favorites_view(update, context)

    row = db_query("SELECT id FROM categories WHERE name=?", (text,), fetch="one")
    if row:
        await send_category_content(context, update.effective_chat.id, user.id, row[0], text)
    else:
        await update.message.reply_text(
            t("choose_button", lang),
            reply_markup=build_user_menu(lang),
        )


# ----------------------------------------------------------------------
# UMUMIY
# ----------------------------------------------------------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    is_adm = is_admin(update.effective_user.id)
    await update.message.reply_text(
        "❌ Amal bekor qilindi.",
        reply_markup=build_admin_menu() if is_adm else build_user_menu(),
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
# ASOSIY FUNKSIYA
# ----------------------------------------------------------------------
def main():
    if not BOT_TOKEN or not OWNER_ID:
        print("⚠️  Iltimos, avval BOT_TOKEN va OWNER_ID muhit o'zgaruvchilarini to'ldiring!")
        return

    # Portni ENG BIRINCHI bo'lib ochamiz — Render buni tezroq aniqlashi uchun
    start_keep_alive_thread()

    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    post_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_POST}$"), post_start)],
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
        entry_points=[MessageHandler(filters.Regex(CONTACT_BTN_PATTERN), contact_start)],
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
        entry_points=[MessageHandler(filters.Regex(f"^{re.escape(BTN_SEARCH)}$"), search_start)],
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

    app.add_handler(post_conv)
    app.add_handler(addcat_conv)
    app.add_handler(editcat_conv)
    app.add_handler(contact_conv)
    app.add_handler(admin_reply_conv)
    app.add_handler(admin_conv)
    app.add_handler(search_conv)
    app.add_handler(block_user_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(setlang_callback, pattern="^setlang:"))
    app.add_handler(CallbackQueryHandler(quiz_answer_callback, pattern="^quiz:"))
    app.add_handler(CallbackQueryHandler(quiz_restart_callback, pattern="^quiz_restart$"))
    app.add_handler(CallbackQueryHandler(quiz_stop_callback, pattern="^quiz_stop$"))
    app.add_handler(CallbackQueryHandler(reset_leaderboard_confirm, pattern="^reset_leaderboard_confirm$"))
    app.add_handler(CallbackQueryHandler(reset_leaderboard_execute, pattern="^reset_leaderboard_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(toggle_favorite_callback, pattern="^togglefav:"))
    app.add_handler(CallbackQueryHandler(view_category_callback, pattern="^viewcat:"))
    app.add_handler(CallbackQueryHandler(unblock_user_callback, pattern="^unblock_user:"))
    app.add_handler(CallbackQueryHandler(delcat, pattern="^admin_delcat:"))
    app.add_handler(CallbackQueryHandler(move_category, pattern="^admin_cat(up|down):"))

    # Pastki tugmalarni ushlaydigan umumiy router — eng oxirida bo'lishi kerak
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_router))

    app.add_error_handler(error_handler)

    # Har kuni soat 09:00 da (server vaqti bo'yicha, odatda UTC) OWNER'ga avtomatik statistika
    if app.job_queue:
        app.job_queue.run_daily(daily_stats_job, time=datetime.strptime("09:00", "%H:%M").time())
    else:
        logger.warning("JobQueue mavjud emas — 'python-telegram-bot[job-queue]' o'rnatilganini tekshiring.")

    print("✅ Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()