# Добавьте эти строки:
from supabase import create_client, Client
import pandas as pd
import matplotlib.pyplot as plt
from io import BytesIO
from datetime import datetime, timedelta
import numpy as np
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
from quart import Quart, request
from hypercorn.asyncio import serve
from hypercorn.config import Config
# Добавьте новый импорт
from tenacity import retry, stop_after_attempt, wait_exponential
# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
nest_asyncio.apply()

# Инициализация Quart приложения
app = Quart(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8000"))
SECRET_TOKEN = os.getenv("SECRET_TOKEN")
# Конфигурация Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")  # URL вашего проекта Supabase
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # Ключ (service_role или anon)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

FRIEND_ID = 424546089
MY_ID = 1181433072

# Хранилище данных в памяти
bot_data = {
    "friend_count": 0,
    "my_count": 0,
    "thread_id": None,
    "actions_chat_id": None,
    "actions_msg_id": None
}

# Вставьте это после импортов, но перед другими функциями
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def safe_edit_message(context, chat_id, msg_id, text, reply_markup):
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=text,
        reply_markup=reply_markup
    )

# Эндпоинт для Health Check
@app.route('/health')
async def health():
    return 'OK', 200

# Обработчик вебхука Telegram
@app.route('/telegram', methods=['POST'])
async def telegram_webhook():
    # Добавьте проверку токена
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != SECRET_TOKEN:
        return 'Forbidden', 403
        
    try:
        json_data = await request.get_json()
        update = Update.de_json(json_data, application.bot)
        await application.process_update(update)
        return 'OK', 200
    except BadRequest as e:
        logger.error(f"Неверный запрос: {str(e)}")
        return 'Bad Request', 400
    except TelegramError as e:
        logger.error(f"Ошибка Telegram API: {str(e)}")
        return 'Error', 500
    except Exception as e:
        logger.error(f"Неизвестная ошибка: {str(e)}", exc_info=True)
        return 'Server Error', 500

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

        # Инициализация счетчиков
        bot_data.setdefault("friend_count", 0)
        bot_data.setdefault("my_count", 0)

        # Обновляем идентификаторы
        bot_data["thread_id"] = thread_id
        bot_data["actions_chat_id"] = update.effective_chat.id

        # Создаем сообщение со счетчиком
        button_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        sent_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Счётчик действий:\n",
            reply_markup=reply_markup,
            message_thread_id=thread_id
        )
        bot_data["actions_msg_id"] = sent_msg.message_id

        await update.message.reply_text("Счётчик запущен!")
    except Exception as e:
        logger.error(f"Ошибка в /start_actions: {str(e)}", exc_info=True)

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        # Добавьте эту проверку (ШАГ 1)
        if update.message is None:
            return

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

        # Запись в Supabase (добавьте этот блок)
        user_id = update.effective_user.id
        today = datetime.now().strftime("%Y-%m-%d")

        # Обновляем счетчик в базе
        response = supabase.table('actions').upsert({
            "user_id": user_id,
            "date": today,
            "count": 1
        }, on_conflict="user_id, date").execute()

        logger.info(f"Данные обновлены: {response.data}")

        await update_counter_message(context)
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {str(e)}", exc_info=True)


async def update_counter_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat_id = bot_data["actions_chat_id"]
        msg_id = bot_data["actions_msg_id"]
        
        if not chat_id or not msg_id:
            return

        button_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await safe_edit_message(context, chat_id, msg_id, "Счётчик действий:\n", reply_markup)
    except BadRequest as e:
        logger.error(f"Не удалось обновить сообщение: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка в update_counter_message: {str(e)}", exc_info=True)

async def generate_plot(df: pd.DataFrame, period: str) -> BytesIO:
    plt.style.use('seaborn')
    fig, ax = plt.subplots(figsize=(12, 6))

    # Группируем данные
    df['date'] = pd.to_datetime(df['date'])
    df_grouped = df.groupby(['user_id', 'date'])['count'].sum().unstack(level=0).fillna(0)
    
    # Данные для Яна и Егора
    dates = df_grouped.index
    yan = df_grouped.get(1181433072, pd.Series(0, index=dates))
    egor = df_grouped.get(424546089, pd.Series(0, index=dates))

    # Столбчатая диаграмма
    bar_width = 0.35
    x = np.arange(len(dates))
    ax.bar(x - bar_width/2, yan, bar_width, label='Ян', color='#3498db', alpha=0.7)
    ax.bar(x + bar_width/2, egor, bar_width, label='Егор', color='#2ecc71', alpha=0.7)

    # Линии тренда
    window = 3
    ax.plot(x, yan.rolling(window).mean(), color='#2980b9', linestyle='--', label='Тренд Ян')
    ax.plot(x, egor.rolling(window).mean(), color='#27ae60', linestyle='--', label='Тренд Егор')

    # Настройки графика
    ax.set_xticks(x)
    ax.set_xticklabels([d.strftime("%d.%m") for d in dates], rotation=45)
    ax.set_title("Аналитика действий")
    ax.set_xlabel("Дата")
    ax.set_ylabel("Количество действий")
    ax.legend()

    # Сохраняем в буфер
    buf = BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
    buf.seek(0)
    plt.close()
    return buf

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

async def stats_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        period = "week"  # Значение по умолчанию
        
        # Парсим аргументы
        if args:
            if args[0] in ["week", "month", "all"]:
                period = args[0]
            else:
                try:
                    start_date = datetime.strptime(args[0], "%Y-%m-%d")
                    end_date = datetime.strptime(args[1], "%Y-%m-%d") if len(args) > 1 else datetime.now()
                    period = "custom"
                except ValueError:
                    await update.message.reply_text("❌ Некорректный формат даты. Используйте YYYY-MM-DD.")
                    return

        # Получаем данные из Supabase
        query = supabase.table('actions').select("*")

        # Фильтруем по периоду
        today = datetime.now()
        if period == "week":
            start_date = today - timedelta(days=7)
            query = query.gte("date", start_date.strftime("%Y-%m-%d"))
        elif period == "month":
            start_date = today.replace(day=1)
            query = query.gte("date", start_date.strftime("%Y-%m-%d"))
        elif period == "custom":
            query = query.gte("date", start_date.strftime("%Y-%m-%d")).lte("date", end_date.strftime("%Y-%m-%d"))

        data = query.execute().data
        df = pd.DataFrame(data)

        # Генерируем график
        plot_buf = await generate_plot(df, period)
        
        # Отправляем график
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=plot_buf,
            caption=f"📊 Статистика за {period}"
        )

    except Exception as e:
        logger.error(f"Ошибка в /stats_counter: {str(e)}", exc_info=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Ошибка: {context.error}", exc_info=True)
    if isinstance(context.error, TelegramError):
        logger.error(f"Детали ошибки Telegram: {context.error.message}")

async def main():
    global application
    
    # Инициализация бота
    application = (
    ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(30)  # Добавьте эту строку
        .write_timeout(30)  # И эту
        .build()
    )

    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    application.add_handler(CommandHandler("edit_count", edit_count))
    application.add_handler(CommandHandler("stats_counter", stats_counter))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, count_messages))
    application.add_error_handler(error_handler)

    # Инициализация приложения
    await application.initialize()
    
    # Установка вебхука
    await application.bot.set_webhook(
        url=f"{APP_URL}/telegram",
        secret_token=SECRET_TOKEN
    )
    
    # Запуск Quart через Hypercorn
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    await serve(app, config)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {str(e)}", exc_info=True)
