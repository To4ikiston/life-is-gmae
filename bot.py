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
from functools import lru_cache
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
        logger.info(f"Данные восстановлены: Ян={bot_data['my_count']}, Егор={bot_data['friend_count']}")
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {str(e)}", exc_info=True)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def safe_edit_message(context, chat_id, msg_id, text, reply_markup):
    logger.info(f"safe_edit_message: редактирование сообщения {msg_id} в чате {chat_id}")
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
    logger.info(f"Получен GET запрос на произвольный путь: /{path}")
    return f"Запрошенный путь: /{path}", 200

# Обработчик вебхука Telegram (POST)
@app.route('/telegram', methods=['POST'])
@app.route('/telegram/', methods=['POST'])
async def telegram_webhook():
    logger.info("Получен запрос на /telegram")
    if application is None:
        logger.error("Бот ещё не инициализирован.")
        return 'Server Error', 500
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != SECRET_TOKEN:
        logger.warning("Запрос с неверным секретным токеном")
        return 'Forbidden', 403
    try:
        json_data = await request.get_json()
        logger.info(f"Получены данные от Telegram: {json_data}")
        update = Update.de_json(json_data, application.bot)
        logger.info(f"Преобразовано обновление: {update}")
        await application.process_update(update)
        logger.info("Обновление успешно обработано")
        return 'OK', 200
    except BadRequest as e:
        logger.error(f"Неверный запрос: {str(e)}")
        return 'Bad Request', 400
    except TelegramError as e:
        logger.error(f"Ошибка Telegram API: {str(e)}")
        return 'Error', 500
    except Exception as e:
        logger.error(f"Неизвестная ошибка в вебхуке: {str(e)}", exc_info=True)
        return 'Server Error', 500

# Тестовые GET-обработчики
@app.route('/telegram', methods=['GET'])
@app.route('/telegram/', methods=['GET'])
async def telegram_webhook_get():
    logger.info("Получен GET запрос на /telegram")
    return "Telegram GET endpoint работает", 200

@app.route('/test_webhook', methods=['GET'])
async def test_webhook():
    logger.info("Получен GET запрос на /test_webhook")
    return "Test webhook работает", 200

# Главное меню (отправляется при /start)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /start вызвана")
    try:
        main_menu_buttons = [
            [InlineKeyboardButton("Запустить счётчик", callback_data="start_actions")],
            [InlineKeyboardButton("Изменить счётчик", callback_data="edit_count_menu")],
            [InlineKeyboardButton("Статистика", callback_data="stats_menu")],
            [InlineKeyboardButton("Помощь", callback_data="help")]
        ]
        keyboard = InlineKeyboardMarkup(main_menu_buttons)
        await update.effective_message.reply_text(
            "Привет! Я бот-счётчик. Выберите действие:",
            reply_markup=keyboard
        )
        logger.info("Главное меню отправлено")
    except Exception as e:
        logger.error(f"Ошибка в /start: {str(e)}", exc_info=True)

# Команда /start_actions – отправка отдельного сообщения-счётчика
async def start_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /start_actions вызвана")
    try:
        thread_id = update.message.message_thread_id
        if thread_id is None:
            logger.warning("Команда /start_actions вызвана не в теме супергруппы")
            await update.message.reply_text("Это не тема супергруппы. Используйте /start_actions в теме!")
            return

        bot_data.setdefault("friend_count", 0)
        bot_data.setdefault("my_count", 0)
        bot_data["thread_id"] = thread_id
        bot_data["actions_chat_id"] = update.effective_chat.id

        # Формируем текст счётчика
        counter_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        # Создаём клавиатуру с одной кнопкой, которая показывает счёт
        keyboard = [[InlineKeyboardButton(counter_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Отправляем сообщение-счётчик (это сообщение можно закрепить)
        counter_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Счётчик: {counter_text}",
            reply_markup=reply_markup,
            message_thread_id=thread_id
        )
        bot_data["actions_msg_id"] = counter_msg.message_id
        logger.info(f"Сообщение-счётчик отправлено, ID: {counter_msg.message_id}")
    except Exception as e:
        logger.error(f"Ошибка в /start_actions: {str(e)}", exc_info=True)

# Обработчик входящих текстовых сообщений для подсчёта
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
            logger.info(f"Обновлены счётчики: Ян={bot_data['my_count']}, Егор={bot_data['friend_count']}")

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

# Команда /help_counter – отправка справки в том же треде
async def help_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /help_counter вызвана")
    try:
        help_text = (
            "🛠️ *Помощь по боту-счетчику* 🛠️\n\n"
            "Доступные команды:\n\n"
            "• *Запустить счётчик:* Нажмите кнопку «Запустить счётчик» в главном меню.\n"
            "• *Изменить счётчик:* Нажмите кнопку «Изменить счётчик» и выберите нужное изменение.\n"
            "• *Статистика:* Нажмите кнопку «Статистика» и выберите период для отображения статистики.\n"
            "• *Помощь:* Отображает это сообщение.\n\n"
            "📌 _Примечание:_ Если бот используется в группе, убедитесь, что режим приватности отключён, или отправляйте команды с упоминанием имени бота."
        )
        thread_id = update.effective_message.message_thread_id
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=help_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            message_thread_id=thread_id
        )
        logger.info("Ответ на /help_counter отправлен")
    except Exception as e:
        logger.error(f"Ошибка в /help_counter: {str(e)}", exc_info=True)

# Функция обновления сообщения-счётчика (обновляет текст и кнопку)
async def update_counter_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Обновление сообщения-счётчика")
    try:
        chat_id = bot_data["actions_chat_id"]
        msg_id = bot_data["actions_msg_id"]
        if not chat_id or not msg_id:
            logger.warning("Не установлены chat_id или msg_id для обновления")
            return

        counter_text = f"{bot_data['friend_count']}/{bot_data['my_count']}"
        keyboard = [[InlineKeyboardButton(counter_text, callback_data="none")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await safe_edit_message(context, chat_id, msg_id, f"Счётчик: {counter_text}", reply_markup)
        logger.info("Сообщение-счётчик успешно обновлено")
    except BadRequest as e:
        logger.error(f"Не удалось обновить сообщение: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка в update_counter_message: {str(e)}", exc_info=True)

# Функция генерации графика
async def generate_plot(df: pd.DataFrame, period: str) -> BytesIO:
    logger.info("Начало генерации графика")
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
        ax.text(0.5, 0.5, 'Ошибка генерации графика', 
                ha='center', va='center', fontsize=14, color='red')
        logger.error(f"Ошибка генерации графика: {str(e)}")
    finally:
        buf = BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
        buf.seek(0)
        plt.close()
        logger.info("График сгенерирован")
        return buf

@lru_cache(maxsize=10)
async def generate_plot_cached(df_hash: str, period: str) -> BytesIO:
    logger.info("Вызов кэшированной функции генерации графика")
    try:
        df = pd.read_json(df_hash, orient='split')
        return await generate_plot(df, period)
    except Exception as e:
        logger.error(f"Ошибка в кэшированной функции: {str(e)}")
        raise

# Команда /edit_count
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

        logger.info(f"Счетчик изменен: Ян={bot_data['my_count']}, Егор={bot_data['friend_count']}")
        await update_counter_message(context)
    except Exception as e:
        logger.error(f"Ошибка в /edit_count: {str(e)}", exc_info=True)

# Команда /stats_counter – меню выбора периода и построение графика
async def stats_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /stats_counter вызвана")
    try:
        args = context.args
        period = "week"  # По умолчанию
        # Если команда вызвана через кнопки, args может содержать период (например, "week", "month", "all")
        if args:
            period = args[0]
        # Получаем все данные из Supabase (фильтрация выполняется на стороне Python)
        all_data = supabase.table("actions").select("user_id, date, count").execute().data
        logger.info(f"Общее количество записей: {len(all_data)}")
        # Фильтруем по периоду
        if period == "week":
            start_date = datetime.now() - timedelta(days=7)
            filtered = [rec for rec in all_data if datetime.strptime(rec["date"], "%Y-%m-%d") >= start_date]
        elif period == "month":
            start_date = datetime.now().replace(day=1)
            filtered = [rec for rec in all_data if datetime.strptime(rec["date"], "%Y-%m-%d") >= start_date]
        elif period == "custom":
            # Если period == "custom", предполагается, что пользователь ввёл даты вручную
            # Здесь можно добавить обработку ввода дат
            filtered = all_data
        else:
            filtered = all_data

        # Фильтруем по user_id
        filtered = [rec for rec in filtered if rec["user_id"] in [FRIEND_ID, MY_ID]]
        logger.info(f"Записей после фильтрации: {len(filtered)}")

        df = pd.DataFrame(filtered)
        df_hash = df.to_json(orient='split')
        plot_buf = await generate_plot_cached(df_hash, period)

        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=plot_buf,
            caption=f"📊 Статистика за {period}"
        )
        logger.info("Фото со статистикой отправлено")
    except Exception as e:
        logger.error(f"Ошибка в /stats_counter: {str(e)}", exc_info=True)

# Обработчик callback-запросов для интерактивного меню
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # обязательно отвечаем на callback
    data = query.data
    logger.info(f"Нажата кнопка: {data}")

    if data == "start_actions":
        await start_actions(update, context)
    elif data == "help":
        await help_counter(update, context)
    elif data == "stats_menu":
        stats_menu_buttons = [
            [InlineKeyboardButton("За неделю", callback_data="stats_week")],
            [InlineKeyboardButton("За месяц", callback_data="stats_month")],
            [InlineKeyboardButton("За всё время", callback_data="stats_all")],
            [InlineKeyboardButton("Указать период вручную", callback_data="stats_custom")]
        ]
        keyboard = InlineKeyboardMarkup(stats_menu_buttons)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Выберите период для статистики:",
            reply_markup=keyboard,
            message_thread_id=update.effective_message.message_thread_id
        )
    elif data.startswith("stats_"):
        period = data.split("_")[1]
        context.args = [period]
        await stats_counter(update, context)
    elif data == "edit_count_menu":
        edit_menu_buttons = [
            [InlineKeyboardButton("Мой счёт +1", callback_data="edit_me_+1"),
             InlineKeyboardButton("Мой счёт -1", callback_data="edit_me_-1")],
            [InlineKeyboardButton("Счёт друга +1", callback_data="edit_friend_+1"),
             InlineKeyboardButton("Счёт друга -1", callback_data="edit_friend_-1")]
        ]
        keyboard = InlineKeyboardMarkup(edit_menu_buttons)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Выберите изменение счётчика:",
            reply_markup=keyboard,
            message_thread_id=update.effective_message.message_thread_id
        )
    elif data.startswith("edit_"):
        # data вида "edit_me_+1" или "edit_friend_-1"
        parts = data.split("_")
        target = parts[1]
        delta = int(parts[2])
        async with data_lock:
            if target == "me":
                bot_data["my_count"] += delta
            elif target == "friend":
                bot_data["friend_count"] += delta
        await update_counter_message(context)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Счёт обновлён: Ян={bot_data['my_count']}, Егор={bot_data['friend_count']}",
            message_thread_id=update.effective_message.message_thread_id
        )

# Обработчик ошибок
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Ошибка: {context.error}", exc_info=True)
    if isinstance(context.error, TelegramError):
        logger.error(f"Детали ошибки Telegram: {context.error.message}")

# Основная функция запуска
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

    # Регистрация команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_actions", start_actions))
    application.add_handler(CommandHandler("edit_count", edit_count))
    application.add_handler(CommandHandler("stats_counter", stats_counter))
    application.add_handler(CommandHandler("help_counter", help_counter))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, count_messages))
    application.add_handler(CallbackQueryHandler(button_handler))
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
