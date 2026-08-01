import logging
import os
import sqlite3
import time
from collections import defaultdict, deque

from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------------------------------------------------------------
# SOZLAMALAR
# ----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

MAX_WARNS = 3                # nechta warndan keyin foydalanuvchi bloklanadi
FLOOD_LIMIT = 5              # necha xabar
FLOOD_SECONDS = 6            # necha soniya ichida yuborilsa spam hisoblanadi
FLOOD_MUTE_MINUTES = 10      # spamchini necha daqiqaga mute qilish
BAD_WORDS = ["so'kinish1", "so'kinish2"]  # taqiqlangan so'zlarni shu yerga qo'shing

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Flood nazorati uchun xotirada saqlanadigan struktura: {(chat_id, user_id): deque[timestamps]}
user_message_times = defaultdict(lambda: deque(maxlen=FLOOD_LIMIT))

DB_PATH = "bot_data.db"


# ----------------------------------------------------------------------
# MA'LUMOTLAR BAZASI
# ----------------------------------------------------------------------
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS warns (
            chat_id INTEGER,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            link_filter INTEGER DEFAULT 1,
            welcome_enabled INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


def get_settings(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT link_filter, welcome_enabled FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO settings (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        row = (1, 1)
    conn.close()
    return {"link_filter": bool(row[0]), "welcome_enabled": bool(row[1])}


def set_setting(chat_id: int, key: str, value: bool):
    get_settings(chat_id)  # qator mavjudligiga ishonch hosil qilish
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (int(value), chat_id))
    conn.commit()
    conn.close()


def add_warn(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO warns (chat_id, user_id, count) VALUES (?, ?, 0)", (chat_id, user_id))
    cur.execute("UPDATE warns SET count = count + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    cur.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    count = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return count


def reset_warns(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE warns SET count = 0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def get_warns(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


# ----------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# ----------------------------------------------------------------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None) -> bool:
    user_id = user_id or update.effective_user.id
    member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def get_target_user(update: Update):
    """Reply qilingan xabardan foydalanuvchini aniqlaydi."""
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None


# ----------------------------------------------------------------------
# XUSH KELIBSIZ
# ----------------------------------------------------------------------
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(update.effective_chat.id)
    if not settings["welcome_enabled"]:
        return
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await update.message.reply_text(
            f"👋 Xush kelibsiz, {member.mention_html()}!\n"
            f"Guruh qoidalari bilan tanishib chiqishingizni so'raymiz.",
            parse_mode="HTML",
        )


# ----------------------------------------------------------------------
# ASOSIY BUYRUQLAR
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Salom! Men guruh boshqaruvi uchun botman.\n"
        "Meni guruhga admin qilib qo'shing va /help buyrug'i bilan imkoniyatlarimni ko'ring."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛠 <b>Admin buyruqlari</b> (xabarga reply qilib ishlatiladi):\n"
        "/ban - Foydalanuvchini bloklash\n"
        "/unban - Blokdan chiqarish (user_id bilan: /unban 123456)\n"
        "/kick - Guruhdan chiqarib yuborish\n"
        "/mute - Yozishni taqiqlash\n"
        "/unmute - Yozishga ruxsat berish\n"
        "/warn - Ogohlantirish berish (3 tadan keyin avtomatik ban)\n"
        "/unwarn - Ogohlantirishni bekor qilish\n"
        "/warns - Ogohlantirishlar sonini ko'rish\n"
        "/purge - Reply qilingan xabargacha bo'lgan barcha xabarlarni o'chirish\n"
        "/pin - Xabarni mahkamlash\n"
        "/unpin - Mahkamlashni bekor qilish\n\n"
        "⚙️ <b>Sozlamalar</b>:\n"
        "/setlinks on|off - Link filtrini yoqish/o'chirish\n"
        "/setwelcome on|off - Xush kelibsiz xabarini yoqish/o'chirish\n\n"
        "🛡 Avtomatik: spam/flood filtri va taqiqlangan so'zlar filtri doim ishlaydi."
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ----------------------------------------------------------------------
# ADMIN BUYRUQLARI
# ----------------------------------------------------------------------
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("Bloklamoqchi bo'lgan foydalanuvchi xabariga reply qiling.")
        return
    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(f"🚫 {target.mention_html()} bloklandi.", parse_mode="HTML")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /unban <user_id>")
        return
    user_id = int(context.args[0])
    await context.bot.unban_chat_member(update.effective_chat.id, user_id)
    await update.message.reply_text(f"✅ Foydalanuvchi (ID: {user_id}) blokdan chiqarildi.")


async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("Chiqarmoqchi bo'lgan foydalanuvchi xabariga reply qiling.")
        return
    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await context.bot.unban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(f"👢 {target.mention_html()} guruhdan chiqarildi.", parse_mode="HTML")


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("Mute qilmoqchi bo'lgan foydalanuvchi xabariga reply qiling.")
        return
    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id, ChatPermissions(can_send_messages=False)
    )
    await update.message.reply_text(f"🔇 {target.mention_html()} ovozsiz qilindi.", parse_mode="HTML")


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("Unmute qilmoqchi bo'lgan foydalanuvchi xabariga reply qiling.")
        return
    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id, ChatPermissions(can_send_messages=True)
    )
    await update.message.reply_text(f"🔊 {target.mention_html()}ga yozish ruxsati qaytarildi.", parse_mode="HTML")


async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("Ogohlantirmoqchi bo'lgan foydalanuvchi xabariga reply qiling.")
        return
    count = add_warn(update.effective_chat.id, target.id)
    if count >= MAX_WARNS:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        reset_warns(update.effective_chat.id, target.id)
        await update.message.reply_text(
            f"🚫 {target.mention_html()} {MAX_WARNS} marta ogohlantirilgani uchun bloklandi.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"⚠️ {target.mention_html()} ogohlantirildi. ({count}/{MAX_WARNS})",
            parse_mode="HTML",
        )


async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("Foydalanuvchi xabariga reply qiling.")
        return
    reset_warns(update.effective_chat.id, target.id)
    await update.message.reply_text(f"✅ {target.mention_html()}ning ogohlantirishlari tozalandi.", parse_mode="HTML")


async def warns_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = get_target_user(update) or update.effective_user
    count = get_warns(update.effective_chat.id, target.id)
    await update.message.reply_text(f"{target.mention_html()}: {count}/{MAX_WARNS} ogohlantirish", parse_mode="HTML")


async def purge_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("O'chirish boshlanishi kerak bo'lgan xabarga reply qiling.")
        return
    start_id = update.message.reply_to_message.message_id
    end_id = update.message.message_id
    deleted = 0
    for msg_id in range(end_id, start_id - 1, -1):
        try:
            await context.bot.delete_message(update.effective_chat.id, msg_id)
            deleted += 1
        except Exception:
            pass
    logger.info(f"{deleted} ta xabar o'chirildi (chat {update.effective_chat.id})")


async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Mahkamlamoqchi bo'lgan xabarga reply qiling.")
        return
    await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)


async def unpin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    await context.bot.unpin_chat_message(update.effective_chat.id)


# ----------------------------------------------------------------------
# SOZLAMA BUYRUQLARI
# ----------------------------------------------------------------------
async def set_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    if not context.args or context.args[0] not in ("on", "off"):
        await update.message.reply_text("Foydalanish: /setlinks on yoki /setlinks off")
        return
    set_setting(update.effective_chat.id, "link_filter", context.args[0] == "on")
    await update.message.reply_text(f"🔗 Link filtri: {context.args[0].upper()}")


async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Bu buyruq faqat adminlar uchun.")
        return
    if not context.args or context.args[0] not in ("on", "off"):
        await update.message.reply_text("Foydalanish: /setwelcome on yoki /setwelcome off")
        return
    set_setting(update.effective_chat.id, "welcome_enabled", context.args[0] == "on")
    await update.message.reply_text(f"👋 Xush kelibsiz xabari: {context.args[0].upper()}")


# ----------------------------------------------------------------------
# XABARLARNI NAZORAT QILISH: SPAM, LINK, TAQIQLANGAN SO'ZLAR
# ----------------------------------------------------------------------
async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Adminlarni tekshirmaymiz - ular filtrlardan ozod
    if await is_admin(update, context, user.id):
        return

    text = update.message.text
    settings = get_settings(chat_id)

    # 1) Taqiqlangan so'zlar
    lowered = text.lower()
    if any(bad in lowered for bad in BAD_WORDS):
        try:
            await update.message.delete()
        except Exception:
            pass
        count = add_warn(chat_id, user.id)
        await context.bot.send_message(
            chat_id,
            f"🚫 {user.mention_html()}, xabaringizda taqiqlangan so'z bor edi va o'chirildi. "
            f"({count}/{MAX_WARNS} ogohlantirish)",
            parse_mode="HTML",
        )
        if count >= MAX_WARNS:
            await context.bot.ban_chat_member(chat_id, user.id)
            reset_warns(chat_id, user.id)
        return

    # 2) Link filtri
    if settings["link_filter"] and ("http://" in lowered or "https://" in lowered or "t.me/" in lowered):
        try:
            await update.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id,
            f"🔗 {user.mention_html()}, guruhda link tashlash taqiqlangan.",
            parse_mode="HTML",
        )
        return

    # 3) Flood/spam nazorati
    key = (chat_id, user.id)
    now = time.time()
    user_message_times[key].append(now)
    times = user_message_times[key]
    if len(times) == FLOOD_LIMIT and (now - times[0]) < FLOOD_SECONDS:
        until = int(now + FLOOD_MUTE_MINUTES * 60)
        try:
            await context.bot.restrict_chat_member(
                chat_id, user.id, ChatPermissions(can_send_messages=False), until_date=until
            )
            await context.bot.send_message(
                chat_id,
                f"🔇 {user.mention_html()} spam qilgani uchun {FLOOD_MUTE_MINUTES} daqiqaga ovozsiz qilindi.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Flood mute xatosi: {e}")
        times.clear()


# ----------------------------------------------------------------------
# XATOLIKLARNI USHLASH
# ----------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Xatolik yuz berdi:", exc_info=context.error)


# ----------------------------------------------------------------------
# ASOSIY FUNKSIYA
# ----------------------------------------------------------------------
def main():
    if BOT_TOKEN == "BU_YERGA_TOKENINGIZNI_QOYING":
        print("⚠️  Iltimos, avval BOT_TOKEN qiymatini o'z tokeningiz bilan almashtiring!")
        return

    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    # Asosiy buyruqlar
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Admin buyruqlari
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("kick", kick_user))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("unwarn", unwarn_user))
    app.add_handler(CommandHandler("warns", warns_count))
    app.add_handler(CommandHandler("purge", purge_messages))
    app.add_handler(CommandHandler("pin", pin_message))
    app.add_handler(CommandHandler("unpin", unpin_message))

    # Sozlamalar
    app.add_handler(CommandHandler("setlinks", set_links))
    app.add_handler(CommandHandler("setwelcome", set_welcome))

    # Yangi a'zolar
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # Har bir matnli xabarni moderatsiya qilish (spam, link, taqiqlangan so'z)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, moderate_message))

    app.add_error_handler(error_handler)

    print("✅ Guruh boshqaruvi bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()