import asyncio
import logging
import os
import sys
import nest_asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from flask import Flask, request  # Добавлен Flask для обработки запросов

# Настройка продвинутого логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]  # Убран FileHandler для Koyeb
)
logger = logging.getLogger(__name__)

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
nest_asyncio.apply()

# Инициализация Flask приложения
app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8000"))

FRIEND_ID = 424546089
MY_ID = 1181433072

# Хранилище счетчиков в памяти
bot_data = {
    "friend_count": 0,
    "my_count": 0,
    "thread_id": None,
    "actions_chat_id": None,
    "actions_msg_id": None
}

# Эндпоинт для Health Check
@app.route('/health')
def health():
    return 'OK', 200

# Обработчик вебхука от Telegram
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        update = Update.de_json(request.get_json(), application.bot)
        asyncio.run(application.process_update(update))
        return 'OK', 200
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {str(e)}", exc_info=True)
        return 'ERROR', 500

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(
            "Привет! Я бот для счётчика сообщений.\n\n"
            "Используй /start_actions в нужной теме группы, чтобы бот отследил сообщения.\n"
            "Используй /edit_count <friend|me> <число> чтобы изменить счётчик вручную."
        )
    except Exception as e:
        logger.error(f"Ошибка в /start: {str(e)}", exc_info=True)

async def start_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        thread_id = update.message.message_thread_id
        if thread_id is None:
            await update.message.reply_text("Это не тема супергруппы. Используйте /start_actions в теме!")
            return

        if bot_data.get("friend_count") is None:
            bot_data["friend_count"] = 0
        if bot_data.get("my_count") is None:
            bot_data["my_count"] = 0

        bot_data["thread_id"] = thread_id
        bot_data["actions_chat_id"] = update.effective_chat.id

        base_text = "Счётчик действий:\n"
        button_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        sent_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=base_text,
            reply_markup=reply_markup,
            message_thread_id=thread_id
        )
        bot_data["actions_msg_id"] = sent_msg.message_id

        await update.message.reply_text("Счётчик запущен!")
    except Exception as e:
        logger.error(f"Ошибка в /start_actions: {str(e)}", exc_info=True)

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not bot_data["thread_id"]:
            return

        if update.message.message_thread_id != bot_data["thread_id"]:
            return

        user_id = update.effective_user.id
        if user_id == FRIEND_ID:
            bot_data["friend_count"] += 1
        elif user_id == MY_ID:
            bot_data["my_count"] += 1
        else:
            return

        await update_counter_message(context)
    except Exception as e:
        logger.error(f"Ошибка в обработке сообщения: {str(e)}", exc_info=True)

async def update_counter_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat_id = bot_data["actions_chat_id"]
        msg_id = bot_data["actions_msg_id"]
        
        if not chat_id or not msg_id:
            return

        button_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text="Счётчик действий:\n",
            reply_markup=reply_markup
        )
    except BadRequest as e:
        logger.error(f"Не удалось обновить сообщение: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка в update_counter_message: {str(e)}", exc_info=True)

async def edit_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Формат: /edit_count <friend|me> <число>")
            return

        who = args[0].lower()
        try:
            delta = int(args[1])
        except ValueError:
            await update.message.reply_text("Второй аргумент должен быть числом.")
            return

        if who == "friend":
            bot_data["friend_count"] += delta
            new_val = bot_data["friend_count"]
        elif who == "me":
            bot_data["my_count"] += delta
            new_val = bot_data["my_count"]
        else:
            await update.message.reply_text("Первый аргумент должен быть 'friend' или 'me'.")
            return

        await update.message.reply_text(f"Счётчик {who} теперь: {new_val}")
        await update_counter_message(context)
    except Exception as e:
        logger.error(f"Ошибка в /edit_count: {str(e)}", exc_info=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Ошибка обработки обновления: {context.error}", exc_info=True)
    if update and isinstance(update, Update):
        logger.error(f"Обновление вызвавшее ошибку: {update.to_dict()}")

async def main_bot_webhook():
    global application  # Делаем application глобальной для доступа из Flask

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN не установлен!")
        raise ValueError("BOT_TOKEN not set!")
    if not APP_URL:
        logger.critical("APP_URL не установлен!")
        raise ValueError("APP_URL not set!")

    try:
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Хендлеры
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("start_actions", start_actions))
        application.add_handler(CommandHandler("edit_count", edit_count))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), count_messages))
        application.add_error_handler(error_handler)

        # Регистрация вебхука
        await application.bot.set_webhook(
            url=f"{APP_URL}/telegram",
            secret_token='YOUR_SECRET_TOKEN'
        )

        logger.info(f"Вебхук зарегистрирован: {APP_URL}/telegram")
        
    except TelegramError as e:
        logger.critical(f"Ошибка Telegram API: {str(e)}")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {str(e)}", exc_info=True)

if __name__ == "__main__":
    try:
        # Запуск Flask в отдельном потоке
        from threading import Thread
        Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': PORT}).start()
        
        # Запуск бота
        asyncio.run(main_bot_webhook())
        
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"Непредвиденная ошибка: {str(e)}", exc_info=True)
