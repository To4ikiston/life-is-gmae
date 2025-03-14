import asyncio
import logging
import sys
import os
import nest_asyncio
from threading import Thread

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

logging.basicConfig(level=logging.INFO)

# Если вы на Windows, это может помочь избежать конфликтов в event loop
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
nest_asyncio.apply()

# Flask-приложение, чтобы Koyeb/Heroku проходили health check
app = Flask(__name__)

@app.route('/')
def index():
    return "OK"  # Просто возвращаем OK

# Читаем токен из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Укажите ID друга и ваш ID (можете брать тоже из окружения, если хотите)
FRIEND_ID = 424546089
MY_ID = 1181433072

# ========= ЛОГИКА ВАШЕГО БОТА ===========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для счётчика сообщений.\n\n"
        "Используй /start_actions в нужной теме группы, чтобы бот отследил сообщения."
    )

async def start_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    thread_id = update.message.message_thread_id
    if thread_id is None:
        await update.message.reply_text(
            "Похоже, что это не тема супергруппы. Используйте /start_actions именно в теме!"
        )
        return

    context.bot_data["friend_count"] = 0
    context.bot_data["my_count"] = 0
    context.bot_data["thread_id"] = thread_id

    text = "Счётчик действий:\n"
    button_text = "0/0"
    keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup,
        message_thread_id=thread_id
    )
    context.bot_data["actions_msg_id"] = sent_msg.message_id

    await update.message.reply_text("Счётчик запущен! Теперь я буду считать сообщения в этой теме.")

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "thread_id" not in context.bot_data:
        return

    thread_id = context.bot_data["thread_id"]
    msg_thread_id = update.message.message_thread_id
    if msg_thread_id != thread_id:
        return

    user_id = update.effective_user.id

    if user_id == FRIEND_ID:
        context.bot_data["friend_count"] += 1
    elif user_id == MY_ID:
        context.bot_data["my_count"] += 1
    else:
        return

    friend_count = context.bot_data["friend_count"]
    my_count = context.bot_data["my_count"]

    button_text = f"{friend_count}/{my_count}"
    keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if "actions_msg_id" not in context.bot_data:
        return

    actions_msg_id = context.bot_data["actions_msg_id"]
    base_text = "Счётчик действий:\n"

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=actions_msg_id,
            text=base_text,
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Не удалось обновить сообщение-счётчик: {e}")

# ========= ФУНКЦИЯ ДЛЯ ЗАПУСКА БОТА (POLLING) ==========

async def main_bot():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не найден в переменных окружения!")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), count_messages)
    application.add_handler(msg_handler)

    # Ключевой момент: отключаем установку сигналов (stop_signals=None)
    await application.run_polling(stop_signals=None)

def run_bot():
    asyncio.run(main_bot())

# ========= ФУНКЦИЯ ДЛЯ ЗАПУСКА FLASK (HEALTH CHECK) ==========

def run_flask():
    # Порт либо 8000, либо берем из переменной окружения PORT
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

# ========== ТОЧКА ВХОДА ============

if __name__ == "__main__":
    # 1) Запускаем бота в отдельном потоке
    from threading import Thread
    t = Thread(target=run_bot)
    t.start()

    # 2) Запускаем Flask (блокирующе) в основном потоке
    run_flask()
