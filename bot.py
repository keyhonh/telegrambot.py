import telebot
from telebot import types

TOKEN="8934030899:AAH9EL02zCsowEjZIfk2Jt7bJzpspdyCt3w"
CHANNEL="@keyhon"

bot=telebot.TeleBot(TOKEN)

def subscribed(uid):
    try:
        s=bot.get_chat_member(CHANNEL,uid).status
        return s in ["member","administrator","creator"]
    except:
        return False

@bot.message_handler(commands=["start"])
def start(m):
    if not subscribed(m.from_user.id):
        kb=types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📢 Kanalga o'tish",url=f"https://t.me/{CHANNEL[1:]}"))
        kb.add(types.InlineKeyboardButton("✅ Tekshirish",callback_data="check"))
        bot.send_message(m.chat.id,"Botdan foydalanish uchun kanalga obuna bo'ling.",reply_markup=kb)
    else:
        bot.send_message(m.chat.id,"Xush kelibsiz!")

@bot.callback_query_handler(func=lambda c:c.data=="check")
def check(c):
    if subscribed(c.from_user.id):
        bot.edit_message_text("✅ Obuna tasdiqlandi!",c.message.chat.id,c.message.message_id)
        bot.send_message(c.message.chat.id,"Asosiy menyu")
    else:
        bot.answer_callback_query(c.id,"Avval obuna bo'ling!",show_alert=True)

bot.infinity_polling()
