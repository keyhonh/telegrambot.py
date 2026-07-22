import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes
)

# ---------- Sozlamalar ----------
import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [7195607202]

# ---------- Ma'lumotlar bazasi ----------
conn = sqlite3.connect('bot_data.db', check_same_thread=False)
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS users
             (user_id INTEGER PRIMARY KEY, 
              username TEXT, 
              first_name TEXT, 
              last_name TEXT,
              join_date TEXT,
              is_banned INTEGER DEFAULT 0)''')

c.execute('''CREATE TABLE IF NOT EXISTS groups
             (chat_id INTEGER PRIMARY KEY, 
              title TEXT)''')

conn.commit()

# ---------- Yordamchi funksiyalar ----------
def add_user(user_id, username, first_name, last_name):
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, join_date) VALUES (?, ?, ?, ?, ?)",
              (user_id, username, first_name, last_name, datetime.now().isoformat()))
    conn.commit()

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_users_count():
    c.execute("SELECT COUNT(*) FROM users")
    return c.fetchone()[0]

def get_banned_users():
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")
    return c.fetchone()[0]

def get_recent_users(hours=24):
    since = datetime.now() - timedelta(hours=hours)
    c.execute("SELECT COUNT(*) FROM users WHERE join_date > ?", (since.isoformat(),))
    return c.fetchone()[0]

def all_users():
    c.execute("SELECT user_id FROM users WHERE is_banned=0")
    return [row[0] for row in c.fetchall()]

def all_groups():
    c.execute("SELECT chat_id FROM groups")
    return [row[0] for row in c.fetchall()]

def ban_user(user_id):
    c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    conn.commit()

def unban_user(user_id):
    c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    conn.commit()

# ---------- Logging ----------
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

async def business_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.business_message:
        try:
            await context.bot.send_message(
                chat_id=update.business_message.chat.id,
                text="Salom! 👋 Xabaringizni oldim. Tez orada javob beraman."
            )
        except Exception as e:
            logger.error(f"Business auto-reply xatosi: {e}")
    application.add_handler(
    MessageHandler(
        filters.ALL,
        business_auto_reply
    )
)
# ---------- Bot handlerlari ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name, user.last_name)

    if is_admin(user.id):
        keyboard = [
            ["🛠 Admin panel"]
        ]
        reply_markup = ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )

        await update.message.reply_text(
            f"Assalomu alaykum, {user.first_name}!\n"
            "Botimizga xush kelibsiz.",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            f"Assalomu alaykum, {user.first_name}!\n"
            "Botimizga xush kelibsiz."
        )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Siz admin emassiz!")
        return
    keyboard = [
        [InlineKeyboardButton("📊 Statistika", callback_data='stats')],
        [InlineKeyboardButton("📢 Post yuborish", callback_data='broadcast')],
        [InlineKeyboardButton("👤 Foydalanuvchilar", callback_data='users')],
        [InlineKeyboardButton("🚫 Bloklanganlar", callback_data='banned')],
        [InlineKeyboardButton("👥 Guruhlar", callback_data='groups')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Admin panelga xush kelibsiz!", reply_markup=reply_markup)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("Siz admin emassiz!")
        return
    users_count = get_users_count()
    banned_count = get_banned_users()
    recent_24h = get_recent_users(24)
    recent_7d = get_recent_users(168)
    text = (f"📊 **Bot statistikasi**\n\n"
            f"👥 Jami foydalanuvchilar: {users_count}\n"
            f"🚫 Bloklanganlar: {banned_count}\n"
            f"✅ Faol foydalanuvchilar: {users_count - banned_count}\n"
            f"🆕 24 soat ichida qo'shilgan: {recent_24h}\n"
            f"🆕 7 kun ichida qo'shilgan: {recent_7d}")
    await query.edit_message_text(text, parse_mode='Markdown')

# ---------- Broadcast post yuborish ----------
BROADCAST_TEXT, BROADCAST_MEDIA = range(2)

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Siz admin emassiz!")
        return
    await update.message.reply_text("Post matnini yuboring (yoki tugatish uchun /cancel):")
    return BROADCAST_TEXT

async def broadcast_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['broadcast_text'] = update.message.text
    await update.message.reply_text("Endi rasm/video yuboring (yoki /skip tugmasini bosing):")
    return BROADCAST_MEDIA

async def broadcast_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    media = None
    if update.message.photo:
        media = update.message.photo[-1].file_id
    elif update.message.video:
        media = update.message.video.file_id
    elif update.message.document:
        media = update.message.document.file_id
    context.user_data['broadcast_media'] = media
    # Postni yuborish
    text = context.user_data['broadcast_text']
    users = all_users()
    groups = all_groups()
    success = 0
    failed = 0
    for chat_id in users:
        try:
            if media:
                if update.message.photo:
                    await context.bot.send_photo(chat_id=chat_id, photo=media, caption=text)
                elif update.message.video:
                    await context.bot.send_video(chat_id=chat_id, video=media, caption=text)
                else:
                    await context.bot.send_document(chat_id=chat_id, document=media, caption=text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
            success += 1
        except Exception as e:
            failed += 1
            logger.error(f"Xato {chat_id}: {e}")
    # Guruhlarga ham yuborish
    for chat_id in groups:
        try:
            if media:
                # shunga o'xshash
                await context.bot.send_message(chat_id=chat_id, text=text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
            success += 1
        except:
            failed += 1
    await update.message.reply_text(f"✅ Post yuborildi.\nMuvaffaqiyatli: {success}\nXatolik: {failed}")
    return ConversationHandler.END

async def broadcast_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['broadcast_media'] = None
    # Postni faqat matn bilan yuborish
    text = context.user_data['broadcast_text']
    users = all_users()
    success = 0
    failed = 0
    for chat_id in users:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            success += 1
        except:
            failed += 1
    await update.message.reply_text(f"✅ Post yuborildi.\nMuvaffaqiyatli: {success}\nXatolik: {failed}")
    return ConversationHandler.END

async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.")
    return ConversationHandler.END

# ---------- Foydalanuvchilar ro'yxati ----------
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("Siz admin emassiz!")
        return
    c.execute("SELECT user_id, username, first_name, join_date FROM users ORDER BY join_date DESC LIMIT 50")
    users = c.fetchall()
    text = "👤 Foydalanuvchilar (oxirgi 50 ta):\n\n"
    for uid, uname, fname, jdate in users:
        username = f"@{uname}" if uname else "Yo'q"
        text += f"ID: {uid} | Ism: {fname} | Username: {username} | Qo'shilgan: {jdate[:10]}\n"
    await query.edit_message_text(text)

async def banned_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("Siz admin emassiz!")
        return
    c.execute("SELECT user_id, username FROM users WHERE is_banned=1")
    banned = c.fetchall()
    if not banned:
        text = "Hech kim bloklanmagan."
    else:
        text = "🚫 Bloklangan foydalanuvchilar:\n"
        for uid, uname in banned:
            text += f"ID: {uid} | @{uname if uname else 'Noma\'lum'}\n"
    await query.edit_message_text(text)

async def groups_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("Siz admin emassiz!")
        return
    c.execute("SELECT chat_id, title FROM groups")
    groups = c.fetchall()
    text = "👥 Guruhlar:\n"
    for cid, title in groups:
        text += f"ID: {cid} | {title}\n"
    if not groups:
        text = "Guruhlar yo'q."
    await query.edit_message_text(text)

# ---------- Ban/Unban ----------
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Siz admin emassiz!")
        return
    try:
        user_id = int(context.args[0])
        ban_user(user_id)
        await update.message.reply_text(f"Foydalanuvchi {user_id} bloklandi.")
    except (IndexError, ValueError):
        await update.message.reply_text("Iltimos, foydalanuvchi ID sini yuboring. Misol: /ban 123456789")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Siz admin emassiz!")
        return
    try:
        user_id = int(context.args[0])
        unban_user(user_id)
        await update.message.reply_text(f"Foydalanuvchi {user_id} blokdan chiqarildi.")
    except (IndexError, ValueError):
        await update.message.reply_text("Iltimos, foydalanuvchi ID sini yuboring. Misol: /unban 123456789")

# ---------- Yangi guruh qo'shilganda ----------
async def left_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member:
        old_status = update.my_chat_member.old_chat_member.status
        new_status = update.my_chat_member.new_chat_member.status

        if old_status in ["member", "administrator"] and new_status in ["left", "kicked"]:
            chat = update.effective_chat
            c.execute("DELETE FROM groups WHERE chat_id=?", (chat.id,))
            conn.commit()
async def new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.my_chat_member.new_chat_members:
        if member.id == context.bot.id:
            chat = update.effective_chat
            c.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?)",
                (chat.id, chat.title)
            )
            conn.commit()

            await context.bot.send_message(
                chat_id=chat.id,
                text="Assalomu alaykum! Bot guruhga qo'shildi."
            )

# ---------- Callback handler ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == 'stats':
        await stats(update, context)
    elif query.data == 'users':
        await list_users(update, context)
    elif query.data == 'banned':
        await banned_list(update, context)
    elif query.data == 'groups':
        await groups_list(update, context)
    elif query.data == 'broadcast':
        await query.answer()
        # broadcastni ConversationHandler orqali boshlash
        await query.edit_message_text("Post matnini yuboring (bekor qilish: /cancel):")
        return BROADCAST_TEXT

# ---------- Asosiy funksiya ----------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlerlar
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))

    # Admin panel tugmasi
    application.add_handler(
        MessageHandler(
            filters.Regex("^🛠 Admin panel$"),
            admin_panel
        )
    )

    application.add_handler(CommandHandler("stats", admin_panel))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))

    # Broadcast conversation
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_TEXT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    broadcast_receive_text
                )
            ],
            BROADCAST_MEDIA: [
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.Document.ALL,
                    broadcast_receive_media
                ),
                CommandHandler("skip", broadcast_skip)
            ]
        },
        fallbacks=[CommandHandler("cancel", broadcast_cancel)]
    )
    application.add_handler(broadcast_conv)

    # Guruh qo'shilish/chiqish
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            new_chat_member
        )
    )
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.LEFT_CHAT_MEMBER,
            left_chat_member
        )
    )

    # Callback (admin panel tugmalari)
    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    logger.info("Bot ishga tushdi!")
    application.run_polling(
    allowed_updates=Update.ALL_TYPES
)


if __name__ == '__main__':
    main()