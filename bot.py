import asyncio
import logging
import sys
import os
import nest_asyncio
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

from flask import Flask

###############################################################################
# 1. Настройка логирования (необязательно)
###############################################################################
logging.basicConfig(level=logging.INFO)

###############################################################################
# 2. Если на Windows, возможно, нужно установить политику цикла
###############################################################################
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
nest_asyncio.apply()

###############################################################################
# 3. Чтение токена и ID пользователей из переменных окружения
#    (или захардкоженных констант, но лучше из env)
###############################################################################
BOT_TOKEN = os.getenv("BOT_TOKEN")
FRIEND_ID = int(os.getenv("FRIEND_ID", "424546089"))  # если хотите читать из env
MY_ID = int(os.getenv("MY_ID", "1181433072"))         # или захардкодите, если удобно

###############################################################################
# 4. Создаём Flask-приложение для health check
###############################################################################
app = Flask(__name__)

@app.route('/')
def index():
    return "OK"

###############################################################################
# 5. Логика Telegram-бота (команды и хендлеры)
###############################################################################
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start – приветствие
    """
    await update.message.reply_text(
        "Привет! Я бот для счётчика сообщений.\n\n"
        "Используй /start_actions в нужной теме группы, чтобы бот отследил сообщения."
    )

async def start_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start_actions – инициализируем счётчики, отправляем сообщение с кнопкой "0/0"
    """
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
    """
    Считаем сообщения в выбранной теме
    """
    # Проверяем, инициализирован ли счётчик
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

###############################################################################
# 6. Функция для запуска Telegram-бота (polling) в асинхронном режиме
###############################################################################
async def main_bot():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не найдено в переменных окружения!")
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), count_messages)
    application.add_handler(msg_handler)

    await application.run_polling()

def run_bot():
    """
    Запускает main_bot() в event loop
    """
    asyncio.run(main_bot())

###############################################################################
# 7. Функция для запуска Flask-сервера на нужном порту (health check)
###############################################################################
def run_flask():
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

###############################################################################
# 8. Точка входа
###############################################################################
if __name__ == "__main__":
    # Запускаем бота (polling) в другом потоке
    from threading import Thread
    t = Thread(target=run_bot)
    t.start()

    # Запускаем Flask (блокирующе)
    run_flask()
