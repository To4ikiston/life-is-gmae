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
from functools import lru_cache
import json
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
application = None

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
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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
data_lock = asyncio.Lock()
try:
    # 2.1. Пытаемся подключиться к Supabase
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # 2.2. Проверяем подключение (делаем тестовый запрос)
    test = supabase.table("actions").select("user_id").limit(1).execute()
    logger.info("✅ Успешное подключение к Supabase")

except Exception as e:
    # 2.3. Если произошла ошибка:
    logger.critical(f"❌ Ошибка подключения к Supabase: {str(e)}")
    sys.exit(1)  # Завершаем работу бота



async def load_initial_data():
    """Загружает данные из Supabase при старте бота"""
    try:
        data = supabase.table("actions").select("*").execute().data
        bot_data["friend_count"] = sum(row["count"] for row in data if row["user_id"] == FRIEND_ID)
        bot_data["my_count"] = sum(row["count"] for row in data if row["user_id"] == MY_ID)
        logger.info(f"Данные восстановлены: Ян={bot_data['my_count']}, Егор={bot_data['friend_count']}")
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {str(e)}")


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
    if application is None:
        logger.error("Бот ещё не инициализирован.")
        return 'Server Error', 500
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
        await update.effective_message.reply_text(
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
        if update.message is None:
            return

        if not bot_data["thread_id"]:
            return

        if update.message.message_thread_id != bot_data["thread_id"]:
            return

        user_id = update.effective_user.id
        today = datetime.now().strftime("%Y-%m-%d")

        # Определяем пользователя и увеличиваем счётчик
        if user_id not in [FRIEND_ID, MY_ID]:
            return

        # Увеличиваем счётчик в памяти ОДИН РАЗ
        async with data_lock:
            if user_id == FRIEND_ID:
                bot_data["friend_count"] += 1
            else:
                bot_data["my_count"] += 1

        # Пытаемся обновить Supabase
        try:
            # Сначала пытаемся получить существующую запись для текущего пользователя и даты
            existing = supabase.table('actions') \
                .select("count") \
                .eq("user_id", user_id) \
                .eq("date", today) \
                .execute().data

            if existing and len(existing) > 0:
                # Если запись есть, увеличиваем значение count
                new_count = existing[0]['count'] + 1
                response = supabase.table('actions') \
                    .update({"count": new_count}) \
                    .eq("user_id", user_id) \
                    .eq("date", today) \
                    .execute()
            else:
                # Если записи нет, вставляем новую
                response = supabase.table('actions') \
                    .insert({"user_id": user_id, "date": today, "count": 1}) \
                    .execute()

            if response.error:
                raise Exception(f"Supabase error: {response.error}")

        except Exception as e:
            # Откатываем изменения в памяти, если произошла ошибка
            async with data_lock:
                if user_id == FRIEND_ID:
                    bot_data["friend_count"] -= 1
                else:
                    bot_data["my_count"] -= 1
            logger.error(f"Ошибка Supabase: {str(e)}")
            raise

        await update_counter_message(context)

    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {str(e)}", exc_info=True)



async def help_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /help_counter вызвана")
    help_text = (
        "🛠️ *Помощь по боту-счетчику* 🛠️\n\n"
        "Я помогу отслеживать ваши действия в теме чата. Вот что я умею:\n\n"
        "🔹 `/start_actions` — Запустить счётчик в теме группы.\n"
        "🔹 `/edit_count <friend|me> <число>` — Изменить счётчик вручную.\n"
        "🔹 `/stats_counter <период>` — Показать статистику (неделя/месяц/все время).\n"
        "🔹 `/help_counter` — Это сообщение.\n\n"
        "*Примеры:*\n"
        "▫️ `/edit_count me +5` — Увеличить ваш счётчик на 5.\n"
        "▫️ `/stats_counter week` — График за неделю.\n\n"
        "📌 _Чтобы команды работали, бот должен быть админом в группе._"
    )
    try:
        await update.effective_message.reply_text(
            help_text,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Ошибка в /help_counter: {str(e)}", exc_info=True)

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
    # Устанавливаем стиль оформления графика, можно экспериментировать с вариантами:
    plt.style.use('seaborn-darkgrid')
    fig, ax = plt.subplots(figsize=(12, 6))

    try:
        if df.empty:
            ax.text(0.5, 0.5, 'Нет данных за выбранный период', 
                    ha='center', va='center', fontsize=14)
            ax.set_title("Аналитика действий", fontsize=16)
            ax.set_xlabel("Дата", fontsize=14)
            ax.set_ylabel("Количество действий", fontsize=14)
            buf = BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
            buf.seek(0)
            plt.close()
            return buf

        # Группируем данные
        df['date'] = pd.to_datetime(df['date'])
        df_grouped = df.groupby(['user_id', 'date'])['count'].sum().unstack(level=0).fillna(0)
        all_dates = pd.date_range(df['date'].min(), df['date'].max())
        df_grouped = df_grouped.reindex(all_dates, fill_value=0)

        dates = df_grouped.index
        yan = df_grouped.get(MY_ID, pd.Series(0, index=dates))
        egor = df_grouped.get(FRIEND_ID, pd.Series(0, index=dates))

        # Построение столбчатой диаграммы
        bar_width = 0.35
        x = np.arange(len(dates))
        ax.bar(x - bar_width/2, yan, bar_width, label='Ян', color='#3498db', alpha=0.7)
        ax.bar(x + bar_width/2, egor, bar_width, label='Егор', color='#2ecc71', alpha=0.7)

        # Построение линий тренда, если данных достаточно
        if len(dates) >= 3:
            window = min(3, len(dates))
            ax.plot(x, yan.rolling(window).mean(), color='#2980b9', linestyle='--', label='Тренд Ян')
            ax.plot(x, egor.rolling(window).mean(), color='#27ae60', linestyle='--', label='Тренд Егор')

        # Настройки осей и заголовков
        ax.set_xticks(x)
        ax.set_xticklabels([d.strftime("%d.%m") for d in dates], rotation=45)
        ax.set_title("Аналитика действий", fontsize=16)
        ax.set_xlabel("Дата", fontsize=14)
        ax.set_ylabel("Количество действий", fontsize=14)
        ax.legend()

        # Добавляем сетку для лучшей читаемости
        ax.grid(True, linestyle='--', alpha=0.7)

        # Автоматическое форматирование подписей дат
        fig.autofmt_xdate()

    except Exception as e:
        ax.clear()
        ax.text(0.5, 0.5, 'Ошибка генерации графика', 
                ha='center', va='center', fontsize=14, color='red')
        logger.error(f"Ошибка генерации графика: {str(e)}")
    finally:
        buf = BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
        buf.seek(0)
        plt.close()
        return buf

@lru_cache(maxsize=10)
async def generate_plot_cached(df_hash: str, period: str) -> BytesIO:
    """Кэшированная версия генерации графиков"""
    try:
        # Конвертируем JSON обратно в DataFrame
        df = pd.read_json(df_hash, orient='split')
        return await generate_plot(df, period)
    except Exception as e:
        logger.error(f"Ошибка в кэшированной функции: {str(e)}")
        raise

async def edit_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        if len(args) < 2:
            await update.effective_message.reply_text("Формат: /edit_count <friend|me> <число>")
            return

        who = args[0].lower()
        try:
            delta = int(args[1])
        except ValueError:
            await update.effective_message.reply_text("Второй аргумент должен быть числом.")
            return

        async with data_lock:
            if who == "friend":
                bot_data["friend_count"] += delta
            elif who == "me":
                bot_data["my_count"] += delta
            else:
                await update.effective_message.reply_text("Первый аргумент должен быть 'friend' или 'me'.")
                return

        # Вместо отправки отдельного сообщения обновляем inline-кнопку
        await update_counter_message(context)
    except Exception as e:
        logger.error(f"Ошибка в /edit_count: {str(e)}", exc_info=True)

async def stats_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        period = "week"
        
        if args:
            if args[0] in ["week", "month", "all"]:
                period = args[0]
            else:
                try:
                    start_date = datetime.strptime(args[0], "%Y-%m-%d")
                    end_date = datetime.strptime(args[1], "%Y-%m-%d") if len(args) > 1 else datetime.now()
                    
                    if end_date < start_date:
                        await update.effective_message.reply_text("❌ Конечная дата не может быть раньше начальной.")
                        return
                        
                    period = "custom"
                except ValueError:
                    await update.effective_message.reply_text("❌ Некорректный формат даты. Используйте YYYY-MM-DD.")
                    return

        # Получаем данные из Supabase
        query = supabase.table("actions")
        today = datetime.now()
        if period == "week":
            start_date = today - timedelta(days=7)
            query = query.gte("date", start_date.strftime("%Y-%m-%d"))
        elif period == "month":
            start_date = today.replace(day=1)
            query = query.gte("date", start_date.strftime("%Y-%m-%d"))
        elif period == "custom":
            query = query.gte("date", start_date.strftime("%Y-%m-%d")).lte("date", end_date.strftime("%Y-%m-%d"))

        query = query.in_("user_id", [FRIEND_ID, MY_ID])
        data = query.select("user_id, date, count").execute().data
        df = pd.DataFrame(data)
        df_hash = df.to_json(orient='split')
        plot_buf = await generate_plot_cached(df_hash, period)
        
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

    await load_initial_data()  # Загрузка данных из Supabase
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    application.add_handler(CommandHandler("edit_count", edit_count))
    application.add_handler(CommandHandler("stats_counter", stats_counter))
    application.add_handler(CommandHandler("help_counter", help_counter))
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
