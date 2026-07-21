import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 1. Log yozishni sozlash (xatolarni ko‘rish uchun)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 2. /start buyrug‘iga javob
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Assalomu alaykum, {user.first_name}! Men sizga yordam beradigan oddiy botman.\n"
        "Menga istalgan xabarni yuboring, men uni qaytaraman."
    )

# 3. /help buyrug‘i
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Mavjud buyruqlar:\n"
        "/start - Botni ishga tushirish\n"
        "/help - Yordam\n"
        "Boshqa xabarlar - echo (qaytarish)"
    )

# 4. Echo funksiyasi (foydalanuvchi yozgan matnni qaytaradi)
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    # Agar foydalanuvchi rasm, video va h.k. yuborsa, bu yerda faqat matnli xabarlar ushlanadi
    await update.message.reply_text(f"Siz yozdingiz: {user_text}")

# 5. Asosiy funksiya - botni ishga tushirish
def main():
    # TOKEN ni o‘z tokenigiz bilan almashtiring!
    TOKEN = "8934030899:AAH2862EY7bm9g8_0O5gCz9-4Hmx0jOvgYI"

    # Application yaratish
    application = Application.builder().token(TOKEN).build()

    # Handlerlarni qo‘shish
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    # Matnli xabarlarni echo handleriga yo‘naltirish
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Botni ishga tushirish (polling usuli)
    print("Bot ishga tushdi...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()