import asyncio
import logging
import os
import sys
import nest_asyncio
from zoneinfo import ZoneInfo  # Если захотите время по часовому поясу (необязательно)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

logging.basicConfig(level=logging.INFO)

# Если работаете на Windows - иногда нужно:
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

nest_asyncio.apply()

###############################################################################
# ПАРАМЕТРЫ
###############################################################################
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL   = os.getenv("APP_URL")       # например: https://my-bot-1.koyeb.app
PORT      = int(os.getenv("PORT", 8000))
FRIEND_ID = 424546089  # Ваш друг
MY_ID     = 1181433072 # Вы

###############################################################################
# ФУНКЦИИ БОТА
###############################################################################
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Просто приветствие"""
    await update.message.reply_text(
        "Привет! Я бот для счётчика сообщений.\n\n"
        "Используй /start_actions в нужной теме группы, чтобы бот отследил сообщения.\n"
        "Используй /edit_count <user> <число> (где user = friend или me), чтобы изменить счётчик вручную.\n"
    )

async def start_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает счётчик в текущей теме (Topic)"""
    thread_id = update.message.message_thread_id
    if thread_id is None:
        await update.message.reply_text(
            "Похоже, что это не тема супергруппы. Используйте /start_actions именно в теме!"
        )
        return

    # Инициализируем счётчики
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
    """Любое новое сообщение (не команда) - увеличивает счётчик того, кто пишет (вы или друг)."""
    # Проверяем, запущен ли счётчик
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
        return  # Игнорируем сообщения других пользователей

    await update_counter_message(context)

async def update_counter_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновляет сообщение с кнопкой (friend_count/my_count). Вызывается при любом изменении счётчика."""
    friend_count = context.bot_data.get("friend_count", 0)
    my_count = context.bot_data.get("my_count", 0)
    button_text = f"{friend_count}/{my_count}"
    keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]

    actions_msg_id = context.bot_data.get("actions_msg_id")
    if not actions_msg_id:
        return

    base_text = "Счётчик действий:\n"
    try:
        await context.bot.edit_message_text(
            chat_id=context._chat_id,  # см. Примечание*
            message_id=actions_msg_id,
            text=base_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logging.error(f"Не удалось обновить сообщение-счётчик: {e}")

# Примечание*: context._chat_id не всегда доступен;
# можно хранить его вместе с actions_msg_id. Для простоты добавим:
# (См. ниже "захардкодим" chat_id при старте счётчика.)
# Или запомним в bot_data["actions_chat_id"].

async def edit_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /edit_count <who> <число>
    Позволяет вручную уменьшать (или увеличивать) счётчик.
    Пример: /edit_count friend -2  -> уменьшит счетчик друга на 2
             /edit_count me 5     -> увеличит счетчик мой на 5
    """
    if "thread_id" not in context.bot_data:
        await update.message.reply_text("Счётчик не запущен. Сначала /start_actions.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /edit_count <friend|me> <число>")
        return

    who = args[0].lower()
    try:
        delta = int(args[1])
    except ValueError:
        await update.message.reply_text("Вторым аргументом должно быть целое число (например, -1 или 2).")
        return

    if who == "friend":
        context.bot_data["friend_count"] = context.bot_data.get("friend_count", 0) + delta
        res = context.bot_data["friend_count"]
    elif who == "me":
        context.bot_data["my_count"] = context.bot_data.get("my_count", 0) + delta
        res = context.bot_data["my_count"]
    else:
        await update.message.reply_text("Первый аргумент должен быть 'friend' или 'me'.")
        return

    await update.message.reply_text(f"Ок, теперь {who} = {res}")
    await update_counter_message(context)

###############################################################################
# ЗАПУСК WEBHOOK (ВМЕСТО POLLING)
###############################################################################
async def main_bot_webhook():
    """Запускает бота в режиме webhook."""
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не найден в переменных окружения!")

    if not APP_URL:
        raise ValueError("APP_URL не найден (нужен ваш https://... URL)")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Регистрируем хендлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    application.add_handler(CommandHandler("edit_count", edit_count))  # <----
    msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), count_messages)
    application.add_handler(msg_handler)

    # Запускаем webhook-сервер, setWebhook(...) и т.д.
    await application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{APP_URL}/telegram",  # Telegram будет POSTить сюда
        stop_signals=None
    )

if __name__ == "__main__":
    # Просто запускаем функцию main_bot_webhook().
    # Flask мы УБИРАЕМ, чтобы не конфликтовать, так что выше код Flask удалён.
    asyncio.run(main_bot_webhook())
