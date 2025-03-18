from supabase import create_client, Client
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
sns.set_style('darkgrid')
from io import BytesIO
from datetime import datetime, timedelta
import numpy as np
import asyncio
import logging
import os
import sys
import nest_asyncio
from functools import lru_cache  # (будет не использоваться для графика)
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
from quart import Quart, request, Response
from hypercorn.asyncio import serve
from hypercorn.config import Config
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

# Хранилище данных
bot_data = {
    "friend_count": 0,
    "my_count": 0,
    "thread_id": None,
    "actions_chat_id": None,
    "actions_msg_id": None
}
data_lock = asyncio.Lock()

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    test = supabase.table("actions").select("user_id").limit(1).execute()
    logger.info("✅ Успешное подключение к Supabase")
except Exception as e:
    logger.critical(f"❌ Ошибка подключения к Supabase: {str(e)}")
    sys.exit(1)

async def load_initial_data():
    try:
        logger.info("Начало загрузки начальных данных из Supabase")
        data = supabase.table("actions").select("*").execute().data
        bot_data["friend_count"] = sum(row["count"] for row in data if row["user_id"] == FRIEND_ID)
        bot_data["my_count"] = sum(row["count"] for row in data if row["user_id"] == MY_ID)
        logger.info(f"Данные восстановлены: Мой счёт = {bot_data['my_count']}, Счёт друга = {bot_data['friend_count']}")
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {str(e)}", exc_info=True)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def safe_edit_message(context, chat_id, msg_id, text, reply_markup=None):
    logger.info(f"Редактирование сообщения {msg_id} в чате {chat_id}")
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=text,
        reply_markup=reply_markup
    )

@app.route('/health')
async def health():
    logger.info("Health check вызван")
    return 'OK', 200

@app.route('/<path:path>', methods=['GET'])
async def catch_all(path):
    logger.info(f"Получен GET запрос на /{path}")
    return f"Запрошенный путь: /{path}", 200

@app.route('/telegram', methods=['POST'])
@app.route('/telegram/', methods=['POST'])
async def telegram_webhook():
    logger.info("Получен запрос на /telegram")
    if application is None:
        logger.error("Бот ещё не инициализирован.")
        return 'Server Error', 500
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != SECRET_TOKEN:
        logger.warning("Неверный секретный токен")
        return 'Forbidden', 403
    try:
        json_data = await request.get_json()
        logger.info(f"Получены данные от Telegram: {json_data}")
        update = Update.de_json(json_data, application.bot)
        logger.info(f"Преобразовано обновление: {update}")
        await application.process_update(update)
        logger.info("Обновление успешно обработано")
        return 'OK', 200
    except Exception as e:
        logger.error(f"Ошибка в вебхуке: {str(e)}", exc_info=True)
        return 'Server Error', 500

@app.route('/telegram', methods=['GET'])
@app.route('/telegram/', methods=['GET'])
async def telegram_webhook_get():
    logger.info("Получен GET запрос на /telegram")
    return "Telegram GET endpoint работает", 200

@app.route('/test_webhook', methods=['GET'])
async def test_webhook():
    logger.info("Получен GET запрос на /test_webhook")
    return "Test webhook работает", 200

# ------------- Слэш-команды без callback-обработчиков -------------

# /start – приветствие и список доступных команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /start вызвана")
    try:
        text = (
            "Привет! Я бот-счётчик.\n\n"
            "Доступные команды:\n"
            "• /start_actions – запустить счётчик (команда должна вызываться в теме супергруппы)\n"
            "• /edit_count <friend|me> <число> – изменить счётчик вручную\n"
            "• /stats_counter [week|month|all] – показать статистику\n"
            "• /help_counter – помощь"
        )
        await update.effective_message.reply_text(text)
        logger.info("Приветственное сообщение отправлено")
    except Exception as e:
        logger.error(f"Ошибка в /start: {str(e)}", exc_info=True)

# /start_actions – запускает счётчик, отправляя сообщение с текстом "Счётчик" и единственной кнопкой с текущим счётом
async def start_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /start_actions вызвана")
    try:
        thread_id = update.message.message_thread_id if update.message else None
        if thread_id is None:
            logger.warning("Команда /start_actions должна вызываться в теме супергруппы")
            await update.effective_message.reply_text("Это не тема супергруппы. Используйте эту команду в теме!")
            return

        bot_data.setdefault("friend_count", 0)
        bot_data.setdefault("my_count", 0)
        bot_data["thread_id"] = thread_id
        bot_data["actions_chat_id"] = update.effective_chat.id

        # Отправляем сообщение с текстом "Счётчик" и встроенной кнопкой с текущим счётом (например, "0/0")
        counter_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        keyboard = [[InlineKeyboardButton(counter_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await update.effective_message.reply_text(
            "Счётчик",
            reply_markup=reply_markup,
            message_thread_id=thread_id
        )
        bot_data["actions_msg_id"] = msg.message_id
        logger.info(f"Сообщение-счётчик отправлено, ID: {msg.message_id}")
    except Exception as e:
        logger.error(f"Ошибка в /start_actions: {str(e)}", exc_info=True)

# /edit_count – изменение счётчика вручную (через слэш-команду)
async def edit_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /edit_count вызвана")
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
        logger.info(f"Счётчик изменён: Мой счёт = {bot_data['my_count']}, Счёт друга = {bot_data['friend_count']}")
        await update_counter_message(context)
    except Exception as e:
        logger.error(f"Ошибка в /edit_count: {str(e)}", exc_info=True)

# /stats_counter – получение статистики и отправка графика (в том же треде, если есть)
async def stats_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /stats_counter вызвана")
    try:
        args = context.args
        period = "week"
        if args:
            period = args[0]

        all_data = supabase.table("actions").select("user_id, date, count").execute().data
        logger.info(f"Общее количество записей: {len(all_data)}")
        if period == "week":
            start_date = datetime.now() - timedelta(days=7)
            filtered = [rec for rec in all_data if datetime.strptime(rec["date"], "%Y-%m-%d") >= start_date]
        elif period == "month":
            start_date = datetime.now().replace(day=1)
            filtered = [rec for rec in all_data if datetime.strptime(rec["date"], "%Y-%m-%d") >= start_date]
        else:
            filtered = all_data

        filtered = [rec for rec in filtered if rec["user_id"] in [FRIEND_ID, MY_ID]]
        logger.info(f"Записей после фильтрации: {len(filtered)}")
        df = pd.DataFrame(filtered)
        df_hash = df.to_json(orient='split')
        plot_buf = await generate_plot_cached(df_hash, period)
        thread_id = update.effective_message.message_thread_id if update.effective_message else None
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=plot_buf,
            caption=f"📊 Статистика за {period}",
            message_thread_id=thread_id
        )
        logger.info("Фото со статистикой отправлено")
    except Exception as e:
        logger.error(f"Ошибка в /stats_counter: {str(e)}", exc_info=True)

# /help_counter – вывод списка команд (помощь)
async def help_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /help_counter вызвана")
    try:
        help_text = (
            "🛠️ *Помощь по боту-счетчику* 🛠️\n\n"
            "• /start_actions – запустить счётчик (команда должна вызываться в теме супергруппы)\n"
            "• /edit_count <friend|me> <число> – изменить счётчик вручную\n"
            "• /stats_counter [week|month|all] – показать статистику\n"
            "• /help_counter – помощь\n\n"
            "📌 _Примечание:_ Если бот используется в группе, убедитесь, что режим приватности отключён, или отправляйте команды с упоминанием имени бота."
        )
        thread_id = update.effective_message.message_thread_id if update.effective_message else None
        await update.effective_message.reply_text(help_text, parse_mode="Markdown", message_thread_id=thread_id)
        logger.info("Ответ на /help_counter отправлен")
    except Exception as e:
        logger.error(f"Ошибка в /help_counter: {str(e)}", exc_info=True)

# Функция обновления сообщения-счётчика (текст всегда "Счётчик", а кнопка отображает текущие значения)
async def update_counter_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Обновление сообщения-счётчика")
    try:
        chat_id = bot_data["actions_chat_id"]
        msg_id = bot_data["actions_msg_id"]
        if not chat_id or not msg_id:
            logger.warning("Не установлены chat_id или msg_id для обновления")
            return
        button_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        keyboard = [[InlineKeyboardButton(button_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # Текст сообщения остаётся неизменным – "Счётчик"
        await safe_edit_message(context, chat_id, msg_id, "Счётчик", reply_markup)
        logger.info("Сообщение-счётчик успешно обновлено")
    except BadRequest as e:
        logger.error(f"Не удалось обновить сообщение: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка в update_counter_message: {str(e)}", exc_info=True)

# Обработчик входящих сообщений для автоматического подсчёта (если пишут в нужном треде)
async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Входящее сообщение для подсчёта получено")
    try:
        if update.message is None:
            logger.debug("update.message отсутствует")
            return
        if not bot_data["thread_id"]:
            logger.debug("thread_id не установлен")
            return
        if update.message.message_thread_id != bot_data["thread_id"]:
            logger.debug("Сообщение не из нужной темы")
            return

        user_id = update.effective_user.id
        today = datetime.now().strftime("%Y-%m-%d")
        if user_id not in [FRIEND_ID, MY_ID]:
            logger.debug(f"Сообщение от неизвестного пользователя: {user_id}")
            return

        async with data_lock:
            if user_id == FRIEND_ID:
                bot_data["friend_count"] += 1
            else:
                bot_data["my_count"] += 1
            logger.info(f"Обновлены счётчики: Мой счёт = {bot_data['my_count']}, Счёт друга = {bot_data['friend_count']}")

        try:
            existing = supabase.table('actions') \
                .select("count") \
                .eq("user_id", user_id) \
                .eq("date", today) \
                .execute().data
            if existing and len(existing) > 0:
                new_count = existing[0]['count'] + 1
                _ = supabase.table('actions') \
                    .update({"count": new_count}) \
                    .eq("user_id", user_id) \
                    .eq("date", today) \
                    .execute()
            else:
                _ = supabase.table('actions') \
                    .insert({"user_id": user_id, "date": today, "count": 1}) \
                    .execute()
            logger.info("Данные в Supabase обновлены")
        except Exception as e:
            async with data_lock:
                if user_id == FRIEND_ID:
                    bot_data["friend_count"] -= 1
                else:
                    bot_data["my_count"] -= 1
            logger.error(f"Ошибка Supabase: {str(e)}")
            raise

        await update_counter_message(context)
        logger.info("Сообщение с обновленным счётчиком отправлено")
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {str(e)}", exc_info=True)

# Функция генерации графика (оставляем без изменений)
async def generate_plot(df: pd.DataFrame, period: str) -> BytesIO:
    logger.info("Начало генерации графика")
    fig, ax = plt.subplots(figsize=(12, 6))
    try:
        if df.empty:
            ax.text(0.5, 0.5, 'Нет данных за выбранный период', ha='center', va='center', fontsize=14)
            ax.set_title("Аналитика действий", fontsize=16)
            ax.set_xlabel("Дата", fontsize=14)
            ax.set_ylabel("Количество действий", fontsize=14)
            buf = BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
            buf.seek(0)
            plt.close()
            logger.info("График с отсутствием данных сгенерирован")
            return buf

        df['date'] = pd.to_datetime(df['date'])
        df_grouped = df.groupby(['user_id', 'date'])['count'].sum().unstack(level=0).fillna(0)
        all_dates = pd.date_range(df['date'].min(), df['date'].max())
        df_grouped = df_grouped.reindex(all_dates, fill_value=0)

        dates = df_grouped.index
        yan = df_grouped.get(MY_ID, pd.Series(0, index=dates))
        egor = df_grouped.get(FRIEND_ID, pd.Series(0, index=dates))

        bar_width = 0.35
        x = np.arange(len(dates))
        ax.bar(x - bar_width/2, yan, bar_width, label='Ян', color='#3498db', alpha=0.7)
        ax.bar(x + bar_width/2, egor, bar_width, label='Егор', color='#2ecc71', alpha=0.7)

        if len(dates) >= 3:
            window = min(3, len(dates))
            ax.plot(x, yan.rolling(window).mean(), color='#2980b9', linestyle='--', label='Тренд Ян')
            ax.plot(x, egor.rolling(window).mean(), color='#27ae60', linestyle='--', label='Тренд Егор')

        ax.set_xticks(x)
        ax.set_xticklabels([d.strftime("%d.%m") for d in dates], rotation=45)
        ax.set_title("Аналитика действий", fontsize=16)
        ax.set_xlabel("Дата", fontsize=14)
        ax.set_ylabel("Количество действий", fontsize=14)
        ax.legend()
        ax.grid(True, linestyle='--', alpha=0.7)
        fig.autofmt_xdate()
    except Exception as e:
        ax.clear()
        ax.text(0.5, 0.5, 'Ошибка генерации графика', ha='center', va='center', fontsize=14, color='red')
        logger.error(f"Ошибка генерации графика: {str(e)}")
    finally:
        buf = BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
        buf.seek(0)
        plt.close()
        logger.info("График сгенерирован")
        return buf

# Здесь убираем декоратор кэширования, чтобы график всегда генерировался заново
async def generate_plot_cached(df_hash: str, period: str) -> BytesIO:
    logger.info("Вызов функции генерации графика (без кеширования)")
    try:
        df = pd.read_json(df_hash, orient='split')
        return await generate_plot(df, period)
    except Exception as e:
        logger.error(f"Ошибка в функции генерации графика: {str(e)}")
        raise

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Ошибка: {context.error}", exc_info=True)
    if isinstance(context.error, TelegramError):
        logger.error(f"Детали ошибки Telegram: {context.error.message}")

async def main():
    global application
    logger.info("Инициализация бота")
    application = (
        ApplicationBuilder()
            .token(BOT_TOKEN)
            .read_timeout(30)
            .write_timeout(30)
            .build()
    )
    await load_initial_data()
    logger.info("Начальные данные загружены")

    # Регистрируем слэш-команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    application.add_handler(CommandHandler("edit_count", edit_count))
    application.add_handler(CommandHandler("stats_counter", stats_counter))
    application.add_handler(CommandHandler("help_counter", help_counter))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, count_messages))
    application.add_error_handler(error_handler)
    logger.info("Обработчики зарегистрированы")

    await application.initialize()
    await application.start()
    logger.info("Бот запущен")

    await application.bot.set_webhook(
        url=f"{APP_URL}/telegram",
        secret_token=SECRET_TOKEN
    )
    logger.info("Вебхук установлен")

    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    logger.info(f"Запуск сервера на порту {PORT}")
    await serve(app, config)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {str(e)}", exc_info=True)
