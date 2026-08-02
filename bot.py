import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

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
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))    # <-- o'z Telegram ID raqamingiz

# Majburiy obuna kanallari. Bot shu kanal(lar)da ADMIN bo'lishi shart.
# id: kanal username'i ("@kanalim") yoki -100... ko'rinishidagi ID
REQUIRED_CHANNELS = [
    {"id": "@keyhon", "title": "📢 Asosiy kanal", "url": "https://t.me/keyhon"},
]
    # Kerak bo'lsa yana qo'shishingiz mumkin:
    # {"id": "@ikkinchi_kanal", "title": "📢 Ikkinchi kanal", "url": "https://t.me/ikkinchi_kanal"}

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

# Conversation state'lari
(
    (
    WAIT_POST_CONTENT,
    WAIT_POST_CONFIRM,
    WAIT_CAT_NAME,
    WAIT_CAT_CONTENT,
    WAIT_ADMIN_ID,
    WAIT_DEL_ADMIN_ID,
    WAIT_EDIT_CONTENT,
) = range(7)


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
            subscribed INTEGER DEFAULT 1
        )
    """)
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
            file_id TEXT
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


def get_categories():
    return db_query("SELECT id, name FROM categories ORDER BY id", fetch="all")


# ----------------------------------------------------------------------
# MAJBURIY OBUNA TEKSHIRUVI
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
        "🔒 Botdan foydalanish uchun avval quyidagi kanal(lar)ga obuna bo'ling, "
        "so'ng \"✅ Tekshirish\" tugmasini bosing:",
        reply_markup=build_subscribe_keyboard(missing),
    )


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    missing = await get_not_subscribed(context.bot, query.from_user.id)
    if missing:
        await query.answer("❌ Siz hali barcha kanallarga obuna bo'lmagansiz.", show_alert=True)
        return
    await query.answer("✅ Obuna tasdiqlandi!")
    await query.message.delete()
    upsert_user(query.from_user)
    await context.bot.send_message(
        query.message.chat_id,
        "✅ Rahmat! Endi botdan to'liq foydalanishingiz mumkin.\n\nKerakli bo'limni tanlang:",
        reply_markup=build_user_menu(),
    )


# ----------------------------------------------------------------------
# REPLY KEYBOARD (PASTKI TUGMALAR) QURISH
# ----------------------------------------------------------------------
def build_user_menu():
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
        rows = [[KeyboardButton("ℹ️ Hozircha bo'limlar yo'q")]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_admin_menu():
    rows = [
        [KeyboardButton(BTN_POST), KeyboardButton(BTN_CATEGORIES)],
        [KeyboardButton(BTN_ADMINS), KeyboardButton(BTN_STATS)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ----------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    if is_admin(user.id):
        await update.message.reply_text(
            "🛠 Xush kelibsiz, Admin!\nQuyidagi tugmalar orqali boshqaring:",
            reply_markup=build_admin_menu(),
        )
        return

    missing = await get_not_subscribed(context.bot, user.id)
    if missing:
        await send_subscribe_prompt(context.bot, update.effective_chat.id, missing)
        return

    await update.message.reply_text(
        "👋 Xush kelibsiz!\n\nKerakli bo'limni tanlang:",
        reply_markup=build_user_menu(),
    )


# ----------------------------------------------------------------------
# FOYDALANUVCHI: BO'LIM TANLASH
# ----------------------------------------------------------------------
async def send_category_content(update: Update, context: ContextTypes.DEFAULT_TYPE, cat_id: int, name: str):
    row = db_query(
        "SELECT content_type, content_text, file_id FROM categories WHERE id=?",
        (cat_id,), fetch="one",
    )
    if not row:
        return
    content_type, content_text, file_id = row

    db_query(
        "INSERT INTO clicks (user_id, category_id, clicked_at) VALUES (?, ?, ?)",
        (update.effective_user.id, cat_id, datetime.now().isoformat()),
    )

    if content_type == "text":
        await update.message.reply_text(content_text or "(matn kiritilmagan)")
    elif content_type == "photo":
        await update.message.reply_photo(photo=file_id, caption=content_text or "")
    elif content_type == "document":
        await update.message.reply_document(document=file_id, caption=content_text or "")
    elif content_type == "video":
        await update.message.reply_video(video=file_id, caption=content_text or "")


# ----------------------------------------------------------------------
# 📢 POST YUBORISH (BROADCAST) — faqat admin
# ----------------------------------------------------------------------
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
        [InlineKeyboardButton("✅ Yuborish", callback_data="post_confirm")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="post_cancel")],
    ])
    await update.message.reply_text("Xabarni tasdiqlaysizmi?", reply_markup=keyboard)
    return WAIT_POST_CONFIRM


async def post_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_cancel":
        await query.message.edit_text("❌ Bekor qilindi.")
        return ConversationHandler.END

    msg = context.user_data.get("post_message")
    if not msg:
        await query.message.edit_text("Xatolik: xabar topilmadi.")
        return ConversationHandler.END

    users = db_query("SELECT user_id FROM users WHERE subscribed=1", fetch="all")
    total_sent, total_failed = 0, 0
    await query.message.edit_text(f"⏳ Yuborilmoqda... (0/{len(users)})")

    for (uid,) in users:
        try:
            await msg.copy(chat_id=uid)
            total_sent += 1
        except Exception as e:
            total_failed += 1
            logger.warning(f"Yuborishda xatolik ({uid}): {e}")

    db_query(
        "INSERT INTO broadcasts (admin_id, sent_at, total_sent, total_failed) VALUES (?, ?, ?, ?)",
        (query.from_user.id, datetime.now().isoformat(), total_sent, total_failed),
    )
    await query.message.edit_text(
        f"✅ Post yuborildi!\n📤 Yetib bordi: {total_sent}\n⚠️ Xato: {total_failed}"
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
    for cat_id, name in categories:
        keyboard.append([InlineKeyboardButton(f"🗑 {name}", callback_data=f"admin_delcat:{cat_id}")])
    keyboard.append([InlineKeyboardButton("➕ Yangi bo'lim qo'shish", callback_data="admin_addcat")])
    await update.message.reply_text(
        "📂 Bo'limlar ro'yxati (o'chirish uchun bosing):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delcat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cat_id = int(query.data.split(":")[1])
    db_query("DELETE FROM categories WHERE id=?", (cat_id,))
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

    db_query(
        "INSERT INTO categories (name, content_type, content_text, file_id) VALUES (?, ?, ?, ?)",
        (name, content_type, text, file_id),
    )
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
# 📊 TO'LIQ STATISTIKA — faqat admin
# ----------------------------------------------------------------------
async def stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
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

    await update.message.reply_text(text, parse_mode="HTML")


# ----------------------------------------------------------------------
# ASOSIY MATN ROUTERI — pastki tugmalarni ushlaydi
# ----------------------------------------------------------------------
async def main_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    upsert_user(user)

    # ---- ADMIN tugmalari ----
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
        # admin biror bo'lim nomini bossa ham ko'rsatib qo'yamiz
        row = db_query("SELECT id FROM categories WHERE name=?", (text,), fetch="one")
        if row:
            await send_category_content(update, context, row[0], text)
        return

    # ---- ODDIY FOYDALANUVCHI ----
    missing = await get_not_subscribed(context.bot, user.id)
    if missing:
        await send_subscribe_prompt(context.bot, update.effective_chat.id, missing)
        return

    row = db_query("SELECT id FROM categories WHERE name=?", (text,), fetch="one")
    if row:
        await send_category_content(update, context, row[0], text)
    else:
        await update.message.reply_text(
            "Iltimos, quyidagi tugmalardan birini tanlang 👇",
            reply_markup=build_user_menu(),
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
    if BOT_TOKEN == "BU_YERGA_TOKENINGIZNI_QOYING" or not OWNER_ID:
        print("⚠️  Iltimos, avval BOT_TOKEN va OWNER_ID qiymatlarini to'ldiring!")
        return

    # Portni ENG BIRINCHI bo'lib ochamiz — Render buni tezroq aniqlashi uchun
    start_keep_alive_thread()

    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    post_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_POST}$"), post_start)],
        states={
            WAIT_POST_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, post_receive)],
            WAIT_POST_CONFIRM: [CallbackQueryHandler(post_confirm, pattern="^post_(confirm|cancel)$")],
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

    app.add_handler(post_conv)
    app.add_handler(addcat_conv)
    app.add_handler(editcat_conv)
    app.add_handler(admin_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(delcat, pattern="^admin_delcat:"))

    # Pastki tugmalarni ushlaydigan umumiy router — eng oxirida bo'lishi kerak
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_router))

    app.add_error_handler(error_handler)

    print("✅ Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()