import asyncio
import logging
import os
import sys
import nest_asyncio
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    PicklePersistence
)

logging.basicConfig(level=logging.INFO)

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
nest_asyncio.apply()

# Читаем токен и другие переменные из окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
FRIEND_ID = 424546089
MY_ID = 1181433072

# ========= ЛОГИКА БОТА ===========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для счётчика сообщений.\n\n"
        "Используй /start_actions в нужной теме группы, чтобы бот отследил сообщения.\n"
        "Для корректировки счётчика используй: /edit_count <friend|me> <число>."
    )

async def start_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    thread_id = update.message.message_thread_id
    if thread_id is None:
        await update.message.reply_text(
            "Похоже, что это не тема супергруппы. Используйте /start_actions именно в теме!"
        )
        return

    # Если persistent данные уже есть, не обнуляем их
    if "friend_count" not in context.bot_data:
        context.bot_data["friend_count"] = 0
    if "my_count" not in context.bot_data:
        context.bot_data["my_count"] = 0
    context.bot_data["thread_id"] = thread_id
    # Сохраним также chat_id для обновления сообщения
    context.bot_data["actions_chat_id"] = update.effective_chat.id

    text = "Счётчик действий:\n"
    button_text = f"{context.bot_data['friend_count']}/{context.bot_data['my_count']}"
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
    if update.message.message_thread_id != thread_id:
        return

    user_id = update.effective_user.id
    if user_id == FRIEND_ID:
        context.bot_data["friend_count"] += 1
    elif user_id == MY_ID:
        context.bot_data["my_count"] += 1
    else:
        return

    await update_counter_message(context)

async def update_counter_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    friend_count = context.bot_data.get("friend_count", 0)
    my_count = context.bot_data.get("my_count", 0)
    button_text = f"{friend_count}/{my_count}"
    keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    chat_id = context.bot_data.get("actions_chat_id")
    actions_msg_id = context.bot_data.get("actions_msg_id")
    if not chat_id or not actions_msg_id:
        return

    base_text = "Счётчик действий:\n"
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=actions_msg_id,
            text=base_text,
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Не удалось обновить сообщение-счётчик: {e}")

async def edit_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /edit_count <friend|me> <число> позволяет вручную изменить счётчик.
    Например: /edit_count friend -1
    """
    if "friend_count" not in context.bot_data or "my_count" not in context.bot_data:
        await update.message.reply_text("Счётчик ещё не запущен. Используйте /start_actions.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /edit_count <friend|me> <число>")
        return

    target = args[0].lower()
    try:
        delta = int(args[1])
    except ValueError:
        await update.message.reply_text("Второй аргумент должен быть числом.")
        return

    if target == "friend":
        context.bot_data["friend_count"] += delta
        new_val = context.bot_data["friend_count"]
    elif target == "me":
        context.bot_data["my_count"] += delta
        new_val = context.bot_data["my_count"]
    else:
        await update.message.reply_text("Первый аргумент должен быть 'friend' или 'me'.")
        return

    await update.message.reply_text(f"Счётчик {target} теперь: {new_val}")
    await update_counter_message(context)

# ========= ЗАПУСК БОТА В РЕЖИМЕ WEBHOOK ===========
async def main_bot_webhook():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не найден в переменных окружения!")
    if not os.getenv("APP_URL"):
        raise ValueError("APP_URL не найден. Укажите публичный URL вашего приложения.")
    
    # Используем persistence, чтобы сохранять данные между перезапусками
    from telegram.ext import PicklePersistence
    persistence = PicklePersistence(filepath="bot_data.pickle")
    
    application = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    application.add_handler(CommandHandler("edit_count", edit_count))
    msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), count_messages)
    application.add_handler(msg_handler)

    PORT = int(os.getenv("PORT", "8000"))
    APP_URL = os.getenv("APP_URL")
    
    await application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{APP_URL}/telegram",
        stop_signals=None
    )

if __name__ == "__main__":
    asyncio.run(main_bot_webhook())
