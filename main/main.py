# bot.py

import logging
import sqlite3
import csv
import io
from datetime import date
from datetime import time
import asyncio
import warnings
import os
import pytz
from dotenv import load_dotenv

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# Подавляем предупреждения о старом адаптере даты в sqlite3
warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- 1. КОНФИГУРАЦИЯ ---
load_dotenv()

# Токен вашего бота из .env файла
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Список ID администраторов из .env файла
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]
# Часовой пояс из .env файла
TIMEZONE_STR = os.getenv("TIMEZONE", "UTC")

# Название файла базы данных
DB_NAME = 'reports_bot.db'

# Включаем логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Отключаем подробные логи от библиотеки httpx, чтобы не засорять консоль
logging.getLogger("httpx").setLevel(logging.WARNING)
# Отключаем информационные сообщения от других библиотек
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)

# Состояния для диалогов (ConversationHandler)
(
    AWAIT_REGISTRATION_START, REGISTER_NAME, REGISTER_LAST_NAME, REGISTER_EMPLOYEE_ID, REGISTER_POSITION,
    CONFIRM_EDIT,
    DELETE_USER_PROMPT, DELETE_USER_CONFIRM,
    SHOW_REPORT_MENU, AWAITING_FIELD_VALUE
) = range(10)

# --- Определяем поля (ключи — для БД/кода; значения — отображаемые подписи) ---
NUMERIC_FIELDS = [
    ("prinyato_zayavok", "При/заяв/работ"),
    ("protokola_na_oformlenii", "Прот на оформ"),
    ("oformleno_protokolov", "Офор/протокол"),
    ("dogovora_na_oformlenii", "Дог на оформл"),
    ("oformleno_dogovorov", "Офор-но Дог"),
    ("napravleno_zaprosov_tkp", "Запрос/ТКП"),
    ("polucheno_tkp", "Получено/ТКП"),
    ("napravleno_na_techzaklyuchenie", "Напра/техзак"),
    ("napravleno_na_prkf", "Направ/ПРКФ"),
    ("oformleno_doverennostey", "Офор/довер-ть"),
    ("oformlena_zayavka_el_magazin", "Заявк/Магазин"),
    ("oformlena_zayavka_el_aukcion", "Заявк/Аукцион"),
    ("oformlena_zayavka_kooper_portal", "Заявк/Коопер."),
    ("oformlena_zayavka_spot", "Заявка/СПОТ"),
]

TEXT_FIELDS = [
    ("provedeny_peregovory", "Проведены переговоры"),
    ("problemy", "Прочие вопросы"), # Это название для кнопки, его можно сделать короче
]

# Объединяем поля для удобства
ALL_FIELDS = NUMERIC_FIELDS + TEXT_FIELDS

# Полные названия полей для команды /help и выгрузки в CSV
FULL_FIELD_LABELS = {
    "prinyato_zayavok": "Принято заявок в работу",
    "protokola_na_oformlenii": "Протоколы на оформлении",
    "oformleno_protokolov": "Оформлено протоколов",
    "dogovora_na_oformlenii": "Договоры на оформлении",
    "oformleno_dogovorov": "Оформлено договоров",
    "napravleno_zaprosov_tkp": "Направлено запросов для получения ТКП",
    "polucheno_tkp": "Получено ТКП",
    "napravleno_na_techzaklyuchenie": "Направлено на техзаключение",
    "napravleno_na_prkf": "Направлено на ПРКФ",
    "oformleno_doverennostey": "Оформлено доверенностей",
    "oformlena_zayavka_el_magazin": "Оформлена заявка в электронный магазин",
    "oformlena_zayavka_el_aukcion": "Оформлена заявка на электронный аукцион",
    "oformlena_zayavka_kooper_portal": "Оформлена заявка на кооперационный портал",
    "oformlena_zayavka_spot": "Оформлена заявка на СПОТ",
    "provedeny_peregovory": "Проведены переговоры по поставке (указать наименования ТМЦ)",
    "problemy": "Прочие вопросы",
}

def get_db_conn():
    return sqlite3.connect(DB_NAME)

def is_pending_approval(user_id):
    """Проверяет, находится ли пользователь в списке ожидания."""
    with get_db_conn() as conn:
        return conn.cursor().execute("SELECT 1 FROM pending_users WHERE user_id = ?", (user_id,)).fetchone() is not None

# --- 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ---

def init_db():
    """Инициализирует базу данных и создает таблицы, если их нет."""
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                employee_id TEXT UNIQUE,
                position TEXT,
                is_registered BOOLEAN DEFAULT 1
            )
        ''')
        # Создаём таблицу reports с базовыми колонками
        cur.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                report_date DATE
                -- далее динамически добавим остальные столбцы
            )
        ''')
        # Создаём таблицу для ожидающих подтверждения пользователей
        cur.execute('''
            CREATE TABLE IF NOT EXISTS pending_users (
                user_id INTEGER PRIMARY KEY,
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

        # --- Проверка и добавление столбца 'position' в таблицу 'users' ---
        cur.execute("PRAGMA table_info(users)")
        user_cols = {row[1] for row in cur.fetchall()}
        if 'position' not in user_cols:
            try:
                cur.execute('ALTER TABLE users ADD COLUMN position TEXT')
                logger.info("Добавлен столбец 'position' в таблицу 'users'")
            except Exception as e:
                logger.exception(f"Не удалось добавить столбец 'position' в таблицу 'users': {e}")

        # Получим текущие колонки таблицы reports
        cur.execute("PRAGMA table_info(reports)")
        existing_cols = {row[1] for row in cur.fetchall()}

        # Нужно добавить user meta и все поля
        required_cols = {
            "user_id": "INTEGER",
            "report_date": "DATE",
        }

        for key, _ in ALL_FIELDS:
            # все числовые — INTEGER, текстовые — TEXT
            required_cols[key] = "INTEGER" if key in dict(NUMERIC_FIELDS) else "TEXT"

        # Добавляем отсутствующие колонки
        for col, col_type in required_cols.items():
            if col not in existing_cols:
                try:
                    cur.execute(f'ALTER TABLE reports ADD COLUMN {col} {col_type}')
                    logger.info(f"Добавлен столбец {col} {col_type} в таблицу reports")
                except Exception as e:
                    logger.exception(f"Не удалось добавить столбец {col}: {e}")
        conn.commit()

def user_exists(user_id):
    """Проверяет, существует ли пользователь в базе."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def add_user(user_id, first_name, last_name, employee_id, position):
    """Добавляет нового пользователя."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (user_id, first_name, last_name, employee_id, position) VALUES (?, ?, ?, ?, ?)",
            (user_id, first_name, last_name, employee_id, position)
        )
        conn.commit()

def has_submitted_report_today(user_id):
    """Проверяет, отправлял ли пользователь отчет сегодня."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        today = date.today()
        cursor.execute(
            "SELECT 1 FROM reports WHERE user_id = ? AND report_date = ?",
            (user_id, today)
        )
        return cursor.fetchone() is not None

def add_report_row(user_id, data: dict):
    """Добавляет новый отчет."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cols = ["user_id", "report_date"] + list(data.keys())
        placeholders = ",".join("?" for _ in cols)
        values = [user_id, date.today()] + [data[k] for k in data.keys()]

        sql = f"INSERT INTO reports ({','.join(cols)}) VALUES ({placeholders})"
        cursor.execute(sql, values)
        conn.commit()

def update_report_today(user_id, data: dict):
    """Обновляет сегодняшний отчет пользователя."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        set_clause = ", ".join(f"{k} = ?" for k in data.keys())
        values = list(data.values()) + [user_id, date.today()]
        sql = f"UPDATE reports SET {set_clause} WHERE user_id = ? AND report_date = ?"
        cursor.execute(
            sql, values
        )
        conn.commit()

def get_user_reports(user_id):
    """Получает последний отчет пользователя."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT report_date, " + ", ".join(k for k, _ in ALL_FIELDS) + " FROM reports WHERE user_id = ? ORDER BY report_date DESC LIMIT 1", (user_id,))
        return cursor.fetchall()

def get_user_by_employee_id(employee_id):
    """Находит пользователя по табельному номеру."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, first_name, last_name FROM users WHERE employee_id = ?", (employee_id,))
        return cursor.fetchone()

def delete_user(user_id):
    """Удаляет пользователя и все его отчеты (каскадно)."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Благодаря ON DELETE CASCADE, отчеты удалятся автоматически
        cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()

def get_all_registered_users():
    """Получает всех зарегистрированных пользователей."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, first_name, last_name, employee_id, position FROM users")
        return cursor.fetchall()

def get_users_submitted_today():
    """Получает ID пользователей, отправивших отчет сегодня."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        today = date.today()
        cursor.execute("SELECT DISTINCT user_id FROM reports WHERE report_date = ?", (today,))
        return [row[0] for row in cursor.fetchall()]

def get_all_reports_for_csv():
    """Получает все отчеты для выгрузки в CSV."""
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        # Составляем список колонок в нужном порядке
        header_cols = ["first_name", "last_name", "employee_id", "position", "report_date"] 
        all_field_keys = [k for k, _ in ALL_FIELDS]
        select_cols = ", ".join([f"u.{c}" for c in header_cols[:4]] + ["r.report_date"] + [f"r.{c}" for c in all_field_keys])
        sql = f'''
            SELECT {select_cols}
            FROM reports r
            JOIN users u ON r.user_id = u.user_id
            ORDER BY r.report_date DESC
        '''

        cur.execute(sql)
        rows = cur.fetchall()
        # Заголовки для CSV (человекочитаемые)
        headers = ["Имя", "Фамилия", "Табельный номер", "Должность", "Дата"]
        headers += [FULL_FIELD_LABELS[key] for key in all_field_keys]
        return headers, rows


# --- 3. КЛАВИАТУРЫ (МЕНЮ) ---

def user_main_menu_keyboard():
    """Главное меню для сотрудника."""
    keyboard = [
        [KeyboardButton("📝 Отправить отчет")],
        [KeyboardButton("📂 Мои отчеты")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_main_menu_keyboard():
    """Главное меню для администратора."""
    keyboard = [
        [KeyboardButton("📊 Статистика за сегодня")],
        [KeyboardButton("🔔 Напомнить всем")],
        [KeyboardButton("📥 Скачать все отчеты (CSV)")],
        [KeyboardButton("👥 Список сотрудников")],
        [KeyboardButton("🗑️ Удалить сотрудника")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def back_to_main_menu_keyboard():
    """Клавиатура с кнопкой 'Назад в меню'."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("⬅️ Назад в главное меню")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def confirm_edit_keyboard():
    """Клавиатура для подтверждения редактирования отчета."""
    keyboard = [
        [KeyboardButton("Да, редактировать")],
        [KeyboardButton("Нет, вернуться в меню")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def confirm_delete_keyboard():
    """Клавиатура для подтверждения удаления пользователя."""
    keyboard = [
        [KeyboardButton("Да, удалить")],
        [KeyboardButton("Отмена")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def start_registration_keyboard():
    """Клавиатура с кнопкой 'Начать регистрацию'."""
    keyboard = [
        [KeyboardButton("🚀 Начать регистрацию")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def build_report_inline_keyboard(current_values: dict):
    """
    current_values: dict key->value (может быть None если не заполнено)
    Формируем таблицу кнопок по 2 в ряд.
    Кнопки для числовых полей — показывают текущее значение (или 0/пусто).
    Также добавляем кнопки для текстовых полей и кнопку SEND.
    """
    keyboard = []
    # числовые — по 2 в ряд
    for i in range(0, len(NUMERIC_FIELDS), 2):
        row = []
        for key, label in NUMERIC_FIELDS[i:i+2]:
            display = current_values.get(key)
            if display is None:
                btn_text = f"{label} — (0)"
            else:
                btn_text = f"{label} — ({display})"
            row.append(InlineKeyboardButton(btn_text, callback_data=f"field|{key}"))
        keyboard.append(row)

    # текстовые — по 2 в ряд
    for i in range(0, len(TEXT_FIELDS), 2):
        row = []
        for key, label in TEXT_FIELDS[i:i+2]:
            display = current_values.get(key)
            if display is None or display == "":
                btn_text = f"{label} — (пусто)"
            else:
                short = display if len(display) <= 20 else display[:17] + "..."
                btn_text = f"{label} — ({short})"
            row.append(InlineKeyboardButton(btn_text, callback_data=f"field|{key}"))
        keyboard.append(row)

    # команды управления
    keyboard.append([
        InlineKeyboardButton("✅ Отправить отчёт", callback_data="action|send"),
        InlineKeyboardButton("❌ Отменить", callback_data="action|cancel"),
    ])
    keyboard.append([
        InlineKeyboardButton("🔄 Сбросить все введённые значения", callback_data="action|reset")
    ])
    return InlineKeyboardMarkup(keyboard)


# --- 4. ЛОГИКА БОТА (ОБРАБОТЧИКИ) ---

# --- Общие функции ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /start. Также используется как точка входа в регистрацию."""
    user = update.effective_user

    # Если пользователь уже зарегистрирован, показать ему главное меню
    if user_exists(user.id):
        await show_main_menu(update, context)
        return ConversationHandler.END

    # Если пользователь уже в списке ожидания, просто сообщаем ему об этом
    if is_pending_approval(user.id):
        await update.message.reply_text(
            "Ваша заявка на доступ уже одобрена администратором. "
            "Пожалуйста, нажмите кнопку ниже, чтобы начать регистрацию.",
            reply_markup=start_registration_keyboard()
        )
        return ConversationHandler.END

    # Если пользователь новый, отправляем запрос администраторам
    with get_db_conn() as conn:
        conn.cursor().execute("INSERT INTO pending_users (user_id) VALUES (?)", (user.id,))
        conn.commit()

    await update.message.reply_text("Ваш запрос на доступ отправлен администратору. Пожалуйста, ожидайте.")

    # Формируем сообщение для администраторов
    user_info = (
        f"👤 <b>Новый запрос на доступ</b>\n\n"
        f"<b>Имя:</b> {user.first_name}\n"
        f"<b>Фамилия:</b> {user.last_name or '<i>(не указана)</i>'}\n"
        f"<b>Username:</b> @{user.username or '<i>(не указан)</i>'}\n"
        f"<b>User ID:</b> <code>{user.id}</code>"
    )
    approval_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve|{user.id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject|{user.id}"),
        ]
    ])

    # Отправляем уведомление всем администраторам
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=user_info, parse_mode='HTML', reply_markup=approval_keyboard)
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление администратору {admin_id}: {e}")

    return ConversationHandler.END

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню в зависимости от прав пользователя."""
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    user_id = user.id
    text, reply_markup = get_menu_for_user(user_id)
    # Отправляем новое сообщение, чтобы гарантированно показать ReplyKeyboard
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
    return ConversationHandler.END

def get_menu_for_user(user_id, force_user_menu: bool = False):
    """Возвращает текст и клавиатуру в зависимости от прав пользователя."""
    is_admin = user_id in ADMIN_IDS

    if is_admin and not force_user_menu:
        text = "Добро пожаловать в панель администратора!"
        reply_markup = admin_main_menu_keyboard()
    else:
        text = "Добро пожаловать в главное меню сотрудника!"
        # Для обычного сотрудника показываем стандартное меню
        reply_markup = user_main_menu_keyboard()
    return text, reply_markup

# --- Логика регистрации ---
async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает процесс регистрации после нажатия кнопки."""
    user_id = update.effective_user.id

    # Дополнительная проверка: разрешена ли регистрация
    if not is_pending_approval(user_id):
        await update.message.reply_text("Ваша заявка еще не одобрена администратором.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    await update.message.reply_html(
        "Отлично! Давайте начнем.\n"
        "Пожалуйста, введите ваше <b>имя</b>:",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data['is_registration_approved'] = True
    return REGISTER_NAME

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['first_name'] = update.message.text
    await update.message.reply_text(
        "Отлично! Теперь введите вашу <b>фамилию</b>:",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )
    return REGISTER_LAST_NAME

async def register_last_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['last_name'] = update.message.text
    await update.message.reply_text(
        "Хорошо. Теперь введите ваш <b>табельный номер</b>:",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )
    return REGISTER_EMPLOYEE_ID

async def register_employee_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['employee_id'] = update.message.text
    await update.message.reply_text(
        "И последний шаг. Введите вашу <b>должность</b>:",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )
    return REGISTER_POSITION

async def register_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    context.user_data['position'] = update.message.text
    try:
        add_user(
            user_id=user.id,
            first_name=context.user_data.get('first_name'),
            last_name=context.user_data.get('last_name'),
            employee_id=context.user_data.get('employee_id'),
            position=context.user_data.get('position')
        )
        # Удаляем пользователя из списка ожидания после успешной регистрации
        with get_db_conn() as conn:
            conn.cursor().execute("DELETE FROM pending_users WHERE user_id = ?", (user.id,))
            conn.commit()

        await update.message.reply_text("🎉 Регистрация успешно завершена!")
        # Показываем главное меню только после УСПЕШНОЙ регистрации
        await show_main_menu(update, context)

    except sqlite3.IntegrityError:
        logger.warning(f"Попытка регистрации с дублирующимся табельным номером: {context.user_data.get('employee_id')}")
        await update.message.reply_text(
            "Произошла ошибка: сотрудник с таким табельным номером уже зарегистрирован. "
            "Пожалуйста, начните регистрацию заново с корректным номером.",
            reply_markup=start_registration_keyboard()
        )
        # Не показываем меню, а даем возможность исправить ошибку
    except Exception as e:
        logger.error(f"Ошибка при регистрации пользователя {user.id}: {e}")
        await update.message.reply_text(
            "Произошла непредвиденная ошибка при регистрации. Пожалуйста, попробуйте позже."
        )
    return ConversationHandler.END

# --- Логика отправки отчета ---
async def start_submit_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог отправки отчета."""
    user_id = update.effective_user.id
    user = update.effective_user

    if has_submitted_report_today(user_id):
        await update.message.reply_text(
            "Вы уже отправляли отчет сегодня. Хотите его отредактировать?",
            reply_markup=confirm_edit_keyboard()
        )
        return CONFIRM_EDIT

    # Инициализируем временную структуру в context.user_data
    context.user_data['pending_report'] = {}
    # значения по умолчанию None — значит не заполнил (при отправке станут 0 или '')
    for key, _ in ALL_FIELDS:
        context.user_data['pending_report'][key] = None

    markup = build_report_inline_keyboard(context.user_data['pending_report'])
    # Сохраняем сообщение-id, чтобы редактировать клавиатуру в будущем
    msg = await update.message.reply_text("Пожалуйста, заполните отчёт. Нажмите на нужное поле:", reply_markup=markup)
    context.user_data['pending_report_msg_id'] = msg.message_id
    return SHOW_REPORT_MENU

async def start_edit_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог редактирования отчета (загружает существующие данные)."""
    user_id = update.effective_user.id
    with get_db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM reports WHERE user_id = ? AND report_date = ?", (user_id, date.today()))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("Ваш сегодняшний отчет не найден. Создайте новый.", reply_markup=user_main_menu_keyboard())
            return ConversationHandler.END
        cols = [d[0] for d in cur.description]
        rowdict = dict(zip(cols, row))
        pending = {k: rowdict.get(k) for k, _ in ALL_FIELDS}
        context.user_data['pending_report'] = pending
        markup = build_report_inline_keyboard(context.user_data['pending_report'])
        msg = await update.message.reply_text("Загружен ваш сегодняшний отчет. Внесите необходимые правки.", reply_markup=markup)
        context.user_data['pending_report_msg_id'] = msg.message_id
        return SHOW_REPORT_MENU

async def callback_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CallbackQueryHandler для инлайн-кнопок отчёта."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if 'pending_report' not in context.user_data:
        context.user_data['pending_report'] = {k: None for k, _ in ALL_FIELDS}

    if data.startswith("field|"):
        key = data.split("|", 1)[1]
        context.user_data['awaiting_field'] = key
        numeric_keys = [k for k, _ in NUMERIC_FIELDS]
        if key in numeric_keys:
            prompt_text = (
                f"Пожалуйста, введите <b>число</b> для поля:\n"
                f"<b>{FULL_FIELD_LABELS[key]}</b>\n\n"
                f"<i>Если значение отсутствует, отправьте 0 или /skip.</i>"
            )
        else:
            prompt_text = (
                f"Пожалуйста, введите <b>текст</b> для поля:\n"
                f"<b>{FULL_FIELD_LABELS[key]}</b>\n\n"
                f"<i>Если информации нет, отправьте /skip.</i>"
            )
        prompt_msg = await query.message.reply_text(prompt_text, parse_mode='HTML')
        context.user_data['prompt_msg_id'] = prompt_msg.message_id
        return AWAITING_FIELD_VALUE

    if data == "action|send":
        pending = context.user_data.get('pending_report', {})
        for k, _ in ALL_FIELDS:
            if pending.get(k) is None: pending[k] = 0
        for k, _ in [f for f in ALL_FIELDS if f[0] in dict(TEXT_FIELDS)]:
            if pending.get(k) is None: pending[k] = ""

        try:
            confirmation_msg = None
            if has_submitted_report_today(user.id):
                update_report_today(user.id, pending)
                confirmation_msg = await query.message.reply_text("✅ Ваш сегодняшний отчёт успешно обновлён.")
            else:
                add_report_row(user.id, pending)
                confirmation_msg = await query.message.reply_text("✅ Отчёт успешно отправлен. Спасибо!")
            
            # Удаляем основное сообщение с меню отчета
            main_report_msg_id = context.user_data.get('pending_report_msg_id')
            if main_report_msg_id:
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=main_report_msg_id)

            # Удаляем финальное подтверждение через 5 секунд
            if confirmation_msg:
                await asyncio.sleep(5)
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=confirmation_msg.message_id)
        except Exception as e:
            logger.exception(f"Ошибка при сохранении отчёта: {e}")
            await query.message.reply_text("❌ Произошла ошибка при сохранении отчёта. Попробуйте позже.")
        finally:
            context.user_data.clear()
            # await show_main_menu(query, context) # Не нужно, т.к. основное меню не пропадало
        return ConversationHandler.END

    if data == "action|cancel":
        context.user_data.clear()
        # Удаляем основное сообщение с меню отчета
        main_report_msg_id = context.user_data.get('pending_report_msg_id')
        if main_report_msg_id:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=main_report_msg_id)

        confirmation_msg = await query.message.reply_text("Действие отменено. Отчёт не был отправлен.")
        
        # Удаляем сообщение через 5 секунд
        await asyncio.sleep(5)
        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=confirmation_msg.message_id)

        # await show_main_menu(query, context) # Не нужно, т.к. основное меню не пропадало
        return ConversationHandler.END

    if data == "action|reset":
        for k, _ in ALL_FIELDS:
            context.user_data['pending_report'][k] = None
        new_markup = build_report_inline_keyboard(context.user_data['pending_report'])
        try:
            await query.edit_message_text("Значения сброшены. Заполните отчет заново:", reply_markup=new_markup)
        except Exception:
            await query.message.reply_text("Значения сброшены.", reply_markup=new_markup)
        return SHOW_REPORT_MENU

    if data == "action|edit_today":
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM reports WHERE user_id = ? AND report_date = ?", (user.id, date.today()))
            row = cur.fetchone()
            if not row:
                await query.message.reply_text("Запись не найдена.")
                return ConversationHandler.END
            cols = [d[0] for d in cur.description]
            rowdict = dict(zip(cols, row))
            pending = {k: rowdict.get(k) for k, _ in ALL_FIELDS}
            context.user_data['pending_report'] = pending
            markup = build_report_inline_keyboard(context.user_data['pending_report'])
            msg = await query.message.reply_text("Редактируйте поля. Нажмите на нужное поле для изменения.", reply_markup=markup)
            context.user_data['pending_report_msg_id'] = msg.message_id
            return SHOW_REPORT_MENU

async def message_fill_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод значения после того, как пользователь нажал кнопку поля."""
    awaiting = context.user_data.get('awaiting_field')
    if not awaiting:
        return

    text = update.message.text.strip()
    numeric_keys = [k for k, _ in NUMERIC_FIELDS]
    try:
        if awaiting in numeric_keys:
            val = int(text)
            if val < 0: raise ValueError("Число должно быть >= 0")
            context.user_data['pending_report'][awaiting] = val
            confirmation_msg = await update.message.reply_text(f"Сохранено: {FULL_FIELD_LABELS[awaiting]} = {val}")
        else:
            context.user_data['pending_report'][awaiting] = text
            confirmation_msg = await update.message.reply_text(f"Сохранено текстовое поле.")
        
        # Удаляем сообщение пользователя и подтверждение через 3 секунды
        await asyncio.sleep(3)
        prompt_msg_id = context.user_data.pop('prompt_msg_id', None)
        if prompt_msg_id:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation_msg.message_id)

    except ValueError:
        await update.message.reply_text(
            "Пожалуйста, введите корректное целое число (>=0) или используйте /skip.",
            reply_to_message_id=update.message.message_id
        ) # Отвечаем на сообщение пользователя с ошибкой
        return AWAITING_FIELD_VALUE # Остаемся в том же состоянии, чтобы пользователь мог исправить
    finally:
        # Этот блок больше не нужен здесь, так как мы не выходим из состояния при ошибке
        context.user_data.pop('awaiting_field', None)
        context.user_data.pop('prompt_msg_id', None) # На всякий случай

    msg_id = context.user_data.get('pending_report_msg_id')
    if msg_id:
        try:
            new_markup = build_report_inline_keyboard(context.user_data['pending_report'])
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                text="Отчет обновлен. Нажмите на следующее поле или отправьте отчет.",
                reply_markup=new_markup
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить клавиатуру: {e}")
    return SHOW_REPORT_MENU

async def skip_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /skip — оставить значение по умолчанию (0 или пусто)."""
    awaiting = context.user_data.get('awaiting_field')
    if not awaiting:
        await update.message.reply_text("Нет активного поля для пропуска.")
        return

    numeric_keys = [k for k, _ in NUMERIC_FIELDS]
    if awaiting in numeric_keys:
        context.user_data['pending_report'][awaiting] = 0
    else:
        context.user_data['pending_report'][awaiting] = ""
    context.user_data.pop('awaiting_field', None)
    
    confirmation_msg = await update.message.reply_text("Поле пропущено и установлено по умолчанию.")

    # Удаляем подтверждение через 3 секунды
    await asyncio.sleep(3)
    prompt_msg_id = context.user_data.get('prompt_msg_id')
    if prompt_msg_id:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation_msg.message_id)

    msg_id = context.user_data.get('pending_report_msg_id')
    if msg_id:
        try:
            new_markup = build_report_inline_keyboard(context.user_data['pending_report'])
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                text="Отчет обновлен. Нажмите на следующее поле или отправьте отчет.",
                reply_markup=new_markup
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить клавиатуру после /skip: {e}")
    return SHOW_REPORT_MENU

# --- Логика просмотра отчетов ---
async def show_my_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reports = get_user_reports(user_id)

    if not reports:
        # Если отчетов нет, показываем сообщение и возвращаем пользователя в главное меню
        _, reply_markup = get_menu_for_user(user_id)
        await update.message.reply_text(
            "У вас пока нет ни одного отчета.",
            reply_markup=reply_markup
        )
        return

    # Формируем и отправляем сообщение с последним отчетом
    message_text = "📂 <b>Ваш последний отчет:</b>\n\n"
    for r in reports:
        report_date = r[0]
        message_text += (
            f"📅 <b>Дата:</b> {report_date}\n"
        )
        for i, (key, _) in enumerate(ALL_FIELDS):
            label = FULL_FIELD_LABELS.get(key, key)
            value = r[i+1]
            message_text += f" - {label}: {value or '<i>(пусто)</i>'}\n"
        message_text += "--------------------\n"

    # После просмотра отчетов возвращаем пользователю его основную клавиатуру
    _, reply_markup = get_menu_for_user(user_id)
    await update.message.reply_text(message_text, parse_mode='HTML', reply_markup=reply_markup)

async def show_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает администратору список всех зарегистрированных пользователей."""
    all_users = get_all_registered_users()

    if not all_users:
        await update.message.reply_text(
            "В системе пока нет зарегистрированных сотрудников.",
            reply_markup=admin_main_menu_keyboard()
        )
        return

    message_text = "👥 <b>Список всех зарегистрированных сотрудников:</b>\n\n"
    for user_id, first_name, last_name, employee_id, position in all_users:
        message_text += (
            f"<b>Имя:</b> {first_name}\n"
            f"<b>Фамилия:</b> {last_name}\n"
            f"<b>Должность:</b> {position}\n"
            f"<b>Табельный номер:</b> {employee_id}\n"
            f"<b>User ID:</b> <code>{user_id}</code>\n"
            "--------------------\n"
        )

    message_text += "\nℹ️ Чтобы исправить или удалить запись, используйте программу для работы с базами данных (например, DB Browser for SQLite) и откройте файл `reports_bot.db`."

    await update.message.reply_text(
        message_text, parse_mode='HTML', reply_markup=admin_main_menu_keyboard()
    )

# --- Логика удаления пользователя (для админа) ---
async def start_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог удаления пользователя."""
    await update.message.reply_text(
        "Введите табельный номер сотрудника, которого хотите удалить.",
        reply_markup=ReplyKeyboardRemove()
    )
    return DELETE_USER_PROMPT

async def prompt_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает подтверждение на удаление."""
    employee_id = update.message.text
    user_to_delete = get_user_by_employee_id(employee_id)

    if not user_to_delete:
        await update.message.reply_text(
            f"Сотрудник с табельным номером '{employee_id}' не найден."
        )
        await show_main_menu(update, context)
        return ConversationHandler.END

    user_id, first_name, last_name = user_to_delete
    context.user_data['user_to_delete'] = {'id': user_id, 'name': f"{first_name} {last_name}"}

    await update.message.reply_text(
        f"Вы уверены, что хотите удалить сотрудника <b>{first_name} {last_name}</b>?\n"
        "<b>ВНИМАНИЕ:</b> Это действие удалит пользователя и все его отчеты без возможности восстановления.",
        parse_mode='HTML',
        reply_markup=confirm_delete_keyboard()
    )
    return DELETE_USER_CONFIRM

async def confirm_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Окончательно удаляет пользователя."""
    # Проверяем, что пользователь нажал "Да, удалить"
    if update.message.text != "Да, удалить":
        await update.message.reply_text("Удаление отменено.")
        await show_main_menu(update, context)
        return ConversationHandler.END

    user_to_delete = context.user_data.pop('user_to_delete', None)
    if user_to_delete and 'id' in user_to_delete:
        delete_user(user_to_delete['id'])
        await update.message.reply_text(f"Сотрудник {user_to_delete.get('name', 'N/A')} успешно удален.")
    else:
        await update.message.reply_text("Не удалось найти данные для удаления. Пожалуйста, начните заново.")
    
    await show_main_menu(update, context)
    return ConversationHandler.END

# --- Функции администратора ---
async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику по сотрудникам, исключая администраторов."""
    all_users = get_all_registered_users()
    # Исключаем администраторов из общего списка для статистики
    employees = [user for user in all_users if user[0] not in ADMIN_IDS]
    
    submitted_today_ids = get_users_submitted_today()
    
    # Считаем только сотрудников
    submitted_employees_count = len([uid for uid in submitted_today_ids if uid not in ADMIN_IDS])
    not_submitted_employees = [emp for emp in employees if emp[0] not in submitted_today_ids]

    text = (
        f"📊 <b>Статистика на {date.today()}:</b>\n\n"
        f"✅ Отправили отчет: <b>{submitted_employees_count}</b>\n" 
        f"❌ Не отправили отчет: <b>{len(not_submitted_employees)}</b>\n"
        f"👥 Всего сотрудников: <b>{len(employees)}</b>\n\n"
    )

    if not_submitted_employees:
        text += "<b>Список тех, кто не отправил отчет:</b>\n"
        for _, first_name, last_name, _, _ in not_submitted_employees:
            text += f" - {first_name} {last_name}\n"

    await update.message.reply_text(text, parse_mode='HTML', reply_markup=admin_main_menu_keyboard())

async def _send_reminders(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Внутренняя функция для поиска и отправки напоминаний. Возвращает количество отправленных."""
    all_users = get_all_registered_users()
    employees = [user for user in all_users if user[0] not in ADMIN_IDS]
    submitted_today_ids = get_users_submitted_today()
    not_submitted_employees = [emp for emp in employees if emp[0] not in submitted_today_ids]

    sent_count = 0
    logger.info(f"Найдено {len(not_submitted_employees)} сотрудников для отправки напоминания.")
    for user_id, _, _, _, _ in not_submitted_employees:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⏰ <b>Напоминание!</b>\nПожалуйста, не забудьте отправить ваш ежедневный отчет.",
                parse_mode='HTML'
            )
            sent_count += 1
            await asyncio.sleep(0.1) # Небольшая задержка, чтобы не перегружать API
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание пользователю {user_id}: {e}")
    return sent_count

async def remind_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ручного запуска рассылки напоминаний."""
    await update.message.reply_text("Начинаю рассылку напоминаний...")
    sent_count = await _send_reminders(context)

    await update.message.reply_text(
        f"✅ Рассылка завершена.\nНапоминания отправлены <b>{sent_count}</b> сотрудникам.",
        parse_mode='HTML',
        reply_markup=admin_main_menu_keyboard()
    )

async def download_csv_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_reports = get_all_reports_for_csv()
    if not all_reports:
        await update.message.reply_text("В базе данных пока нет отчетов.", reply_markup=admin_main_menu_keyboard())
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_ALL)
    headers, rows = get_all_reports_for_csv()
    writer.writerow(headers)
    for r in rows:
        # Приводим значения к строкам, убираем переносы
        cleaned = [str(x).replace("\n", " ").replace("\r", "") if x is not None else "" for x in r]
        writer.writerow(cleaned)

    output.seek(0)
    file_to_send = io.BytesIO(output.getvalue().encode('utf-8-sig')) # utf-8-sig для Excel
    file_to_send.name = f'all_reports_{date.today()}.csv'

    await context.bot.send_document(chat_id=update.effective_user.id, document=file_to_send)
    await update.message.reply_text("✅ Файл с отчетами отправлен.", reply_markup=admin_main_menu_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущий диалог."""
    user = update.effective_user
    if user:
        logger.info(f"Пользователь {user.first_name} (ID: {user.id}) отменил действие.")
    
    await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(update, context)
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет справочное сообщение в зависимости от роли пользователя."""
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_IDS

    if is_admin:
        text = (
            "ℹ️ <b>Справка для администратора</b>\n\n"
            "Вы можете использовать следующие команды через кнопки меню:\n"
            "📊 <b>Статистика за сегодня</b> - Показывает, кто сдал, а кто еще нет.\n"
            "🔔 <b>Напомнить всем</b> - Отправляет напоминание тем, кто не сдал отчет.\n"
            "📥 <b>Скачать все отчеты (CSV)</b> - Формирует и отправляет вам файл со всеми отчетами.\n"
            "👥 <b>Список сотрудников</b> - Показывает список всех зарегистрированных пользователей.\n"
            "🗑️ <b>Удалить сотрудника</b> - Запускает процесс удаления пользователя по табельному номеру.\n\n"
            "Также доступны команды:\n"
            "/start - Перезапуск бота и возврат в главное меню.\n"
            "/cancel - Отмена текущего действия и возврат в главное меню."
        )
    else:
        numeric_fields_info = "\n".join([f"• <i>{FULL_FIELD_LABELS.get(key, key)}</i>" for key, _ in NUMERIC_FIELDS])
        text_fields_info = "\n".join([f"• <i>{FULL_FIELD_LABELS.get(key, key)}</i>" for key, _ in TEXT_FIELDS])

        text = (
            "ℹ️ <b>Справка для сотрудника</b>\n\n"
            "Используйте кнопки меню для взаимодействия с ботом:\n"
            "📝 <b>Отправить отчет</b> - Заполнить и отправить ваш ежедневный отчет.\n"
            "📂 <b>Мои отчеты</b> - Просмотреть ваш последний отправленный отчет.\n\n"
            "<b>Как заполнять отчет:</b>\n"
            "При нажатии на кнопку 'Отправить отчет' появится меню с полями. Нажмите на поле, чтобы ввести значение.\n\n"
            "<b>Числовые поля (нужно ввести = 1 или 2 или 3):</b>\n"
            f"{numeric_fields_info}\n"
            "Если по какому-то из этих пунктов нет данных, просто отправьте <b>0</b> или нажмите /skip.\n\n"
            "<b>Текстовые поля (нужно написать текст):</b>\n"
            f"{text_fields_info}\n"
            "Если информации нет, нажмите /skip, чтобы оставить поле пустым.\n\n"
            "После заполнения всех полей нажмите '✅ Отправить отчёт'."
        )

    await update.message.reply_text(text, parse_mode='HTML')

async def scheduled_reminder_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Колбэк для автоматической отправки напоминаний по расписанию."""
    logger.info("Запуск автоматической рассылки напоминаний по расписанию.")
    sent_count = await _send_reminders(context)
    logger.info(f"Автоматическая рассылка завершена. Отправлено {sent_count} напоминаний.")

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки одобрения/отклонения регистрации."""
    query = update.callback_query
    await query.answer()

    action, user_id_str = query.data.split('|')
    user_id = int(user_id_str)
    admin = query.from_user

    original_text = query.message.text_html

    if action == 'approve':
        # Проверяем, есть ли пользователь еще в списке ожидания
        if not is_pending_approval(user_id):
            await query.edit_message_text(f"{original_text}\n\n<i>(Действие уже выполнено другим администратором)</i>", parse_mode='HTML')
            return

        try:
            # Отправляем пользователю приглашение к регистрации
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ Ваша заявка на доступ одобрена!\n\nТеперь вы можете начать регистрацию.",
                reply_markup=start_registration_keyboard()
            )
            await query.edit_message_text(f"{original_text}\n\n<b>✅ Одобрено администратором {admin.mention_html()}</b>", parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение одобренному пользователю {user_id}: {e}")
            await query.edit_message_text(f"{original_text}\n\n<i>Не удалось уведомить пользователя. Возможно, он заблокировал бота.</i>", parse_mode='HTML')

    elif action == 'reject':
        # Удаляем пользователя из списка ожидания
        with get_db_conn() as conn:
            conn.cursor().execute("DELETE FROM pending_users WHERE user_id = ?", (user_id,))
            conn.commit()

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ К сожалению, ваша заявка на доступ была отклонена."
            )
            await query.edit_message_text(f"{original_text}\n\n<b>❌ Отклонено администратором {admin.mention_html()}</b>", parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение отклоненному пользователю {user_id}: {e}")
            await query.edit_message_text(f"{original_text}\n\n<i>Не удалось уведомить пользователя. Возможно, он заблокировал бота.</i>", parse_mode='HTML')


async def unknown_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает любые сообщения, которые не были распознаны другими обработчиками."""
    await update.message.reply_text(
        "Я не понимаю эту команду. Пожалуйста, используйте кнопки в меню или введите /start для получения справки.",
        reply_to_message_id=update.message.message_id,
        parse_mode=None # Явно отключаем разбор Markdown/HTML
    )


# --- 5. ЗАПУСК БОТА ---

def main() -> None:
    """Основная функция для запуска бота."""
    if not BOT_TOKEN:
        logger.error("Токен бота не найден. Укажите его в .env файле (BOT_TOKEN=...).")
        return

    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    # Настройка ежедневных автоматических напоминаний
    try:
        timezone = pytz.timezone(TIMEZONE_STR)
        job_queue = application.job_queue
        # Запускать каждый день с понедельника (0) по пятницу (4) в 16:00
        job_queue.run_daily(
            scheduled_reminder_callback,
            time=time(hour=16, minute=0, tzinfo=timezone),
            days=(0, 1, 2, 3, 4)
        )
        logger.info(f"Запланирована ежедневная отправка напоминаний в 16:00 по часовому поясу {TIMEZONE_STR}")
    except pytz.UnknownTimeZoneError:
        logger.error(f"Неизвестный часовой пояс: '{TIMEZONE_STR}'. Автоматические напоминания не будут работать. "
                     f"Укажите корректный часовой пояс в .env файле (например, TIMEZONE=Asia/Tashkent).")

    # Основной обработчик, включающий все диалоги и кнопки
    conv_handler = ConversationHandler(
        entry_points=[
            # Точка входа в регистрацию теперь - кнопка, а не /start
            # CommandHandler("start", start), # Для новых пользователей
            MessageHandler(filters.Regex("^📝 Отправить отчет$"), start_submit_report),
            MessageHandler(filters.Regex("^🗑️ Удалить сотрудника$"), start_delete_user),
            # Новая точка входа в регистрацию
            MessageHandler(filters.Regex("^🚀 Начать регистрацию$"), start_registration),
        ],
        states={
            # Состояния регистрации
            AWAIT_REGISTRATION_START: [
                MessageHandler(filters.Regex("^🚀 Начать регистрацию$"), start_registration)
            ],
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_LAST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_last_name)],
            REGISTER_EMPLOYEE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_employee_id)],
            REGISTER_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_position)],
            
            # Состояние подтверждения редактирования
            CONFIRM_EDIT: [
                MessageHandler(filters.Regex("^Да, редактировать$"), start_edit_report),
                MessageHandler(filters.Regex("^Нет, вернуться в меню$"), cancel),
            ],
            
            # Состояния для нового процесса отчета
            SHOW_REPORT_MENU: [CallbackQueryHandler(callback_report_menu)],
            AWAITING_FIELD_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, message_fill_field),
                CommandHandler("skip", skip_field),
            ],

            # Состояния удаления пользователя
            DELETE_USER_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_delete_user)],
            DELETE_USER_CONFIRM: [
                MessageHandler(filters.Regex("^(Да, удалить|Отмена)$"), confirm_delete_user),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            # Этот обработчик ловит все варианты отмены и возврата в меню
            MessageHandler(filters.Regex("^(Отмена|⬅️ Назад в главное меню|Нет, вернуться в меню)$"), cancel),
            # Обработчик для любых других сообщений внутри диалога
            MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message_handler),
        ],
        # Этот флаг позволяет обработчикам вне ConversationHandler работать
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    # Обработчики команд и кнопок главного меню
    application.add_handler(CommandHandler("start", start)) # Для существующих пользователей
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", show_main_menu))
    application.add_handler(MessageHandler(filters.Regex("^📂 Мои отчеты$"), show_my_reports))
    application.add_handler(MessageHandler(filters.Regex("^📊 Статистика за сегодня$"), show_admin_stats))
    application.add_handler(MessageHandler(filters.Regex(r"^🔔 Напомнить всем$"), remind_all_users))
    application.add_handler(MessageHandler(filters.Regex(r"^📥 Скачать все отчеты \(CSV\)$"), download_csv_reports))
    application.add_handler(MessageHandler(filters.Regex(r"^👥 Список сотрудников$"), show_all_users))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ Назад в главное меню$"), show_main_menu))

    # Обработчик для кнопок одобрения/отклонения
    application.add_handler(CallbackQueryHandler(handle_approval, pattern=r"^(approve|reject)\|"))

    # Обработчик для всех остальных сообщений (должен быть последним)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message_handler))

    application.run_polling()


if __name__ == "__main__":
    main()









# --- 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ---

def init_db():
    """Инициализирует базу данных и создает таблицы, если их нет."""
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                employee_id TEXT UNIQUE,
                position TEXT,
                is_registered BOOLEAN DEFAULT 1
            )
        ''')
        # Создаём таблицу reports с базовыми колонками
        cur.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                report_date DATE
                -- далее динамически добавим остальные столбцы
            )
        ''')
        conn.commit()

        # --- Проверка и добавление столбца 'position' в таблицу 'users' ---
        cur.execute("PRAGMA table_info(users)")
        user_cols = {row[1] for row in cur.fetchall()}
        if 'position' not in user_cols:
            try:
                cur.execute('ALTER TABLE users ADD COLUMN position TEXT')
                logger.info("Добавлен столбец 'position' в таблицу 'users'")
            except Exception as e:
                logger.exception(f"Не удалось добавить столбец 'position' в таблицу 'users': {e}")

        # Получим текущие колонки таблицы reports
        cur.execute("PRAGMA table_info(reports)")
        existing_cols = {row[1] for row in cur.fetchall()}

        # Нужно добавить user meta и все поля
        required_cols = {
            "user_id": "INTEGER",
            "report_date": "DATE",
        }

        for key, _ in NUMERIC_FIELDS + TEXT_FIELDS:
            # все числовые — INTEGER, текстовые — TEXT
            required_cols[key] = "INTEGER" if key in dict(NUMERIC_FIELDS) else "TEXT" # type: ignore

        # Добавляем отсутствующие колонки
        for col, col_type in required_cols.items():
            if col not in existing_cols:
                try:
                    cur.execute(f'ALTER TABLE reports ADD COLUMN {col} {col_type}')
                    logger.info(f"Добавлен столбец {col} {col_type} в таблицу reports")
                except Exception as e:
                    logger.exception(f"Не удалось добавить столбец {col}: {e}")
        conn.commit()

def user_exists(user_id):
    """Проверяет, существует ли пользователь в базе."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def add_user(user_id, first_name, last_name, employee_id, position):
    """Добавляет нового пользователя."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (user_id, first_name, last_name, employee_id, position) VALUES (?, ?, ?, ?, ?)",
            (user_id, first_name, last_name, employee_id, position)
        )
        conn.commit()

def has_submitted_report_today(user_id):
    """Проверяет, отправлял ли пользователь отчет сегодня."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        today = date.today()
        cursor.execute(
            "SELECT 1 FROM reports WHERE user_id = ? AND report_date = ?",
            (user_id, today)
        )
        return cursor.fetchone() is not None

def add_report_row(user_id, data: dict):
    """Добавляет новый отчет."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cols = ["user_id", "report_date"] + list(data.keys())
        placeholders = ",".join("?" for _ in cols)
        values = [user_id, date.today()] + [data[k] for k in data.keys()]

        sql = f"INSERT INTO reports ({','.join(cols)}) VALUES ({placeholders})"
        cursor.execute(sql, values)
        conn.commit()

def update_report_today(user_id, data: dict):
    """Обновляет сегодняшний отчет пользователя."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        set_clause = ", ".join(f"{k} = ?" for k in data.keys())
        values = list(data.values()) + [user_id, date.today()]
        sql = f"UPDATE reports SET {set_clause} WHERE user_id = ? AND report_date = ?"
        cursor.execute(
            sql, values
        )
        conn.commit()

def get_user_reports(user_id):
    """Получает последний отчет пользователя."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT report_date, " + ", ".join(k for k, _ in NUMERIC_FIELDS + TEXT_FIELDS) + " FROM reports WHERE user_id = ? ORDER BY report_date DESC LIMIT 1", (user_id,))
        return cursor.fetchall()

def get_user_by_employee_id(employee_id):
    """Находит пользователя по табельному номеру."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, first_name, last_name FROM users WHERE employee_id = ?", (employee_id,))
        return cursor.fetchone()

def delete_user(user_id):
    """Удаляет пользователя и все его отчеты (каскадно)."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Благодаря ON DELETE CASCADE, отчеты удалятся автоматически
        cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()

def get_all_registered_users():
    """Получает всех зарегистрированных пользователей."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, first_name, last_name, employee_id, position FROM users")
        return cursor.fetchall()

def get_users_submitted_today():
    """Получает ID пользователей, отправивших отчет сегодня."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        today = date.today()
        cursor.execute("SELECT DISTINCT user_id FROM reports WHERE report_date = ?", (today,))
        return [row[0] for row in cursor.fetchall()]

def get_all_reports_for_csv():
    """Получает все отчеты для выгрузки в CSV."""
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        # Составляем список колонок в нужном порядке
        header_cols = ["first_name", "last_name", "employee_id", "position", "report_date"]
        numeric_keys = [k for k, _ in NUMERIC_FIELDS]
        text_keys = [k for k, _ in TEXT_FIELDS]
        select_cols = ", ".join([f"u.{c}" for c in header_cols[:4]] + ["r.report_date"] + [f"r.{c}" for c in numeric_keys + text_keys])
        sql = f'''
            SELECT {select_cols}
            FROM reports r
            JOIN users u ON r.user_id = u.user_id
            ORDER BY r.report_date DESC
        '''
        cur.execute(sql)
        rows = cur.fetchall()
        # Заголовки для CSV (человекочитаемые)
        headers = ["Имя", "Фамилия", "Табельный номер", "Должность", "Дата"]
        headers += [FULL_FIELD_LABELS[key] for key in numeric_keys + text_keys]
        return headers, rows


# --- 3. КЛАВИАТУРЫ (МЕНЮ) ---

def user_main_menu_keyboard():
    """Главное меню для сотрудника."""
    keyboard = [
        [KeyboardButton("📝 Отправить отчет")],
        [KeyboardButton("📂 Мои отчеты")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_main_menu_keyboard():
    """Главное меню для администратора."""
    keyboard = [
        [KeyboardButton("📊 Статистика за сегодня")],
        [KeyboardButton("🔔 Напомнить всем")],
        [KeyboardButton("📥 Скачать все отчеты (CSV)")],
        [KeyboardButton("👥 Список сотрудников")],
        [KeyboardButton("🗑️ Удалить сотрудника")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def back_to_main_menu_keyboard():
    """Клавиатура с кнопкой 'Назад в меню'."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("⬅️ Назад в главное меню")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def confirm_edit_keyboard():
    """Клавиатура для подтверждения редактирования отчета."""
    keyboard = [
        [KeyboardButton("Да, редактировать")],
        [KeyboardButton("Нет, вернуться в меню")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def confirm_delete_keyboard():
    """Клавиатура для подтверждения удаления пользователя."""
    keyboard = [
        [KeyboardButton("Да, удалить")],
        [KeyboardButton("Отмена")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def start_registration_keyboard():
    """Клавиатура с кнопкой 'Начать регистрацию'."""
    keyboard = [
        [KeyboardButton("🚀 Начать регистрацию")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def build_report_inline_keyboard(current_values: dict):
    """
    current_values: dict key->value (может быть None если не заполнено)
    Формируем таблицу кнопок по 2 в ряд.
    Кнопки для числовых полей — показывают текущее значение (или 0/пусто).
    Также добавляем кнопки для текстовых полей и кнопку SEND.
    """
    keyboard = []
    # числовые — по 2 в ряд
    for i in range(0, len(NUMERIC_FIELDS), 2):
        row = []
        for key, label in NUMERIC_FIELDS[i:i+2]:
            display = current_values.get(key)
            if display is None:
                btn_text = f"{label} — (0)"
            else:
                btn_text = f"{label} — ({display})"
            row.append(InlineKeyboardButton(btn_text, callback_data=f"field|{key}"))
        keyboard.append(row)

    # текстовые — по 2 в ряд
    for i in range(0, len(TEXT_FIELDS), 2):
        row = []
        for key, label in TEXT_FIELDS[i:i+2]:
            display = current_values.get(key)
            if display is None or display == "":
                btn_text = f"{label} — (пусто)"
            else:
                short = display if len(display) <= 20 else display[:17] + "..."
                btn_text = f"{label} — ({short})"
            row.append(InlineKeyboardButton(btn_text, callback_data=f"field|{key}"))
        keyboard.append(row)

    # команды управления
    keyboard.append([
        InlineKeyboardButton("✅ Отправить отчёт", callback_data="action|send"),
        InlineKeyboardButton("❌ Отменить", callback_data="action|cancel"),
    ])
    keyboard.append([
        InlineKeyboardButton("🔄 Сбросить все введённые значения", callback_data="action|reset")
    ])
    return InlineKeyboardMarkup(keyboard)


# --- 4. ЛОГИКА БОТА (ОБРАБОТЧИКИ) ---

# --- Общие функции ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /start. Также используется как точка входа в регистрацию."""
    user = update.effective_user
    if user_exists(user.id):
        await show_main_menu(update, context)
        return ConversationHandler.END

    # Если пользователь уже в списке ожидания, просто сообщаем ему об этом
    if is_pending_approval(user.id):
        await update.message.reply_text(
            "Ваша заявка на доступ уже одобрена администратором. "
            "Пожалуйста, нажмите кнопку ниже, чтобы начать регистрацию.",
            reply_markup=start_registration_keyboard()
        )
        return ConversationHandler.END

    # Если пользователь новый, отправляем запрос администраторам
    with get_db_conn() as conn:
        conn.cursor().execute("INSERT INTO pending_users (user_id) VALUES (?)", (user.id,))
        conn.commit()

    await update.message.reply_text("Ваш запрос на доступ отправлен администратору. Пожалуйста, ожидайте.")

    # Формируем сообщение для администраторов
    user_info = (
        f"👤 <b>Новый запрос на доступ</b>\n\n"
        f"<b>Имя:</b> {user.first_name}\n"
        f"<b>Фамилия:</b> {user.last_name or '<i>(не указана)</i>'}\n"
        f"<b>Username:</b> @{user.username or '<i>(не указан)</i>'}\n"
        f"<b>User ID:</b> <code>{user.id}</code>"
    )
    approval_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve|{user.id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject|{user.id}"),
        ]
    ])

    # Отправляем уведомление всем администраторам
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=user_info, parse_mode='HTML', reply_markup=approval_keyboard)
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление администратору {admin_id}: {e}")

    return ConversationHandler.END

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню в зависимости от прав пользователя."""
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    user_id = user.id
    text, reply_markup = get_menu_for_user(user_id)
    # Отправляем новое сообщение, чтобы гарантированно показать ReplyKeyboard
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
    return ConversationHandler.END

def get_menu_for_user(user_id, force_user_menu: bool = False):
    """Возвращает текст и клавиатуру в зависимости от прав пользователя."""
    is_admin = user_id in ADMIN_IDS

    if is_admin and not force_user_menu:
        text = "Добро пожаловать в панель администратора!"
        reply_markup = admin_main_menu_keyboard()
    else:
        text = "Добро пожаловать в главное меню сотрудника!"
        # Для обычного сотрудника показываем стандартное меню
        reply_markup = user_main_menu_keyboard()
    return text, reply_markup

# --- Логика регистрации ---
async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает процесс регистрации после нажатия кнопки."""
    user_id = update.effective_user.id

    # Дополнительная проверка: разрешена ли регистрация
    if not is_pending_approval(user_id):
        await update.message.reply_text("Ваша заявка еще не одобрена администратором.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    await update.message.reply_html(
        "Отлично! Давайте начнем.\n"
        "Пожалуйста, введите ваше <b>имя</b>:",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data['is_registration_approved'] = True
    return REGISTER_NAME

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['first_name'] = update.message.text
    await update.message.reply_text(
        "Отлично! Теперь введите вашу <b>фамилию</b>:",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )
    return REGISTER_LAST_NAME

async def register_last_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['last_name'] = update.message.text
    await update.message.reply_text(
        "Хорошо. Теперь введите ваш <b>табельный номер</b>:",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )
    return REGISTER_EMPLOYEE_ID

async def register_employee_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['employee_id'] = update.message.text
    await update.message.reply_text(
        "И последний шаг. Введите вашу <b>должность</b>:",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )
    return REGISTER_POSITION

async def register_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    context.user_data['position'] = update.message.text
    try:
        add_user(
            user_id=user.id,
            first_name=context.user_data.get('first_name'),
            last_name=context.user_data.get('last_name'),
            employee_id=context.user_data.get('employee_id'),
            position=context.user_data.get('position')
        )
        # Удаляем пользователя из списка ожидания после успешной регистрации
        with get_db_conn() as conn:
            conn.cursor().execute("DELETE FROM pending_users WHERE user_id = ?", (user.id,))
            conn.commit()

        await update.message.reply_text("🎉 Регистрация успешно завершена!")
    except sqlite3.IntegrityError:
        logger.warning(f"Попытка регистрации с дублирующимся табельным номером: {context.user_data.get('employee_id')}")
        await update.message.reply_text(
            "Произошла ошибка: сотрудник с таким табельным номером уже зарегистрирован. "
            "Пожалуйста, начните регистрацию заново с корректным номером.",
            reply_markup=start_registration_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка при регистрации пользователя {user.id}: {e}")
        await update.message.reply_text(
            "Произошла непредвиденная ошибка при регистрации. Пожалуйста, попробуйте позже."
        )
    return ConversationHandler.END

# --- Логика отправки отчета ---
async def start_submit_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог отправки отчета."""
    user_id = update.effective_user.id
    user = update.effective_user

    if has_submitted_report_today(user_id):
        await update.message.reply_text(
            "Вы уже отправляли отчет сегодня. Хотите его отредактировать?",
            reply_markup=confirm_edit_keyboard()
        )
        return CONFIRM_EDIT

    # Инициализируем временную структуру в context.user_data
    context.user_data['pending_report'] = {}
    # значения по умолчанию None — значит не заполнил (при отправке станут 0 или '')
    for key, _ in ALL_FIELDS:
        context.user_data['pending_report'][key] = None

    markup = build_report_inline_keyboard(context.user_data['pending_report'])
    # Сохраняем сообщение-id, чтобы редактировать клавиатуру в будущем
    msg = await update.message.reply_text("Пожалуйста, заполните отчёт. Нажмите на нужное поле:", reply_markup=markup)
    context.user_data['pending_report_msg_id'] = msg.message_id
    return SHOW_REPORT_MENU

async def start_edit_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог редактирования отчета (загружает существующие данные)."""
    user_id = update.effective_user.id
    with get_db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM reports WHERE user_id = ? AND report_date = ?", (user_id, date.today()))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("Ваш сегодняшний отчет не найден. Создайте новый.", reply_markup=user_main_menu_keyboard())
            return ConversationHandler.END
        cols = [d[0] for d in cur.description]
        rowdict = dict(zip(cols, row))
        pending = {k: rowdict.get(k) for k, _ in ALL_FIELDS}
        context.user_data['pending_report'] = pending
        markup = build_report_inline_keyboard(context.user_data['pending_report'])
        msg = await update.message.reply_text("Загружен ваш сегодняшний отчет. Внесите необходимые правки.", reply_markup=markup, reply_keyboard_remove=True)
        msg = await update.message.reply_text("Загружен ваш сегодняшний отчет. Внесите необходимые правки.", reply_markup=markup)
        context.user_data['pending_report_msg_id'] = msg.message_id
        return SHOW_REPORT_MENU

async def callback_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CallbackQueryHandler для инлайн-кнопок отчёта."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if 'pending_report' not in context.user_data:
        context.user_data['pending_report'] = {k: None for k, _ in ALL_FIELDS}

    if data.startswith("field|"):
        key = data.split("|", 1)[1]
        context.user_data['awaiting_field'] = key
        numeric_keys = [k for k, _ in NUMERIC_FIELDS]
        if key in numeric_keys:
            prompt_text = (
                f"Пожалуйста, введите <b>число</b> для поля:\n"
                f"<b>{FULL_FIELD_LABELS[key]}</b>\n\n"
                f"<i>Если значение отсутствует, отправьте 0 или /skip.</i>"
            )
        else:
            prompt_text = (
                f"Пожалуйста, введите <b>текст</b> для поля:\n"
                f"<b>{FULL_FIELD_LABELS[key]}</b>\n\n"
                f"<i>Если информации нет, отправьте /skip.</i>"
            )
        prompt_msg = await query.message.reply_text(prompt_text, parse_mode='HTML')
        context.user_data['prompt_msg_id'] = prompt_msg.message_id
        return AWAITING_FIELD_VALUE

    if data == "action|send":
        pending = context.user_data.get('pending_report', {})
        for k, _ in ALL_FIELDS:
            if pending.get(k) is None: pending[k] = 0
        for k, _ in [f for f in ALL_FIELDS if f[0] in dict(TEXT_FIELDS)]:
            if pending.get(k) is None: pending[k] = ""

        try:
            confirmation_msg = None
            if has_submitted_report_today(user.id):
                update_report_today(user.id, pending)
                confirmation_msg = await query.message.reply_text("✅ Ваш сегодняшний отчёт успешно обновлён.")
            else:
                add_report_row(user.id, pending)
                confirmation_msg = await query.message.reply_text("✅ Отчёт успешно отправлен. Спасибо!")
            
            # Удаляем основное сообщение с меню отчета
            main_report_msg_id = context.user_data.get('pending_report_msg_id')
            if main_report_msg_id:
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=main_report_msg_id)

            # Удаляем финальное подтверждение через 5 секунд
            if confirmation_msg:
                await asyncio.sleep(5)
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=confirmation_msg.message_id)
        except Exception as e:
            logger.exception(f"Ошибка при сохранении отчёта: {e}")
            await query.message.reply_text("❌ Произошла ошибка при сохранении отчёта. Попробуйте позже.")
        finally:
            context.user_data.clear() # Очищаем временные данные
            await show_main_menu(query, context) # Возвращаем пользователя в главное меню
            # await show_main_menu(query, context) # Не нужно, т.к. основное меню не пропадало
        return ConversationHandler.END

    if data == "action|cancel":
        context.user_data.clear()
        # Удаляем основное сообщение с меню отчета
        main_report_msg_id = context.user_data.get('pending_report_msg_id')
        if main_report_msg_id:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=main_report_msg_id)

        confirmation_msg = await query.message.reply_text("Действие отменено. Отчёт не был отправлен.")
        
        # Удаляем сообщение через 5 секунд
        await asyncio.sleep(5)
        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=confirmation_msg.message_id)

        await show_main_menu(query, context) # Возвращаем пользователя в главное меню
        return ConversationHandler.END

    if data == "action|reset":
        for k, _ in ALL_FIELDS:
            context.user_data['pending_report'][k] = None
        new_markup = build_report_inline_keyboard(context.user_data['pending_report'])
        try:
            await query.edit_message_text("Значения сброшены. Заполните отчет заново:", reply_markup=new_markup)
        except Exception:
            await query.message.reply_text("Значения сброшены.", reply_markup=new_markup)
        return SHOW_REPORT_MENU

    if data == "action|edit_today":
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM reports WHERE user_id = ? AND report_date = ?", (user.id, date.today()))
            row = cur.fetchone()
            if not row:
                await query.message.reply_text("Запись не найдена.")
                return ConversationHandler.END
            cols = [d[0] for d in cur.description]
            rowdict = dict(zip(cols, row))
            pending = {k: rowdict.get(k) for k, _ in ALL_FIELDS}
            context.user_data['pending_report'] = pending
            markup = build_report_inline_keyboard(context.user_data['pending_report'])
            msg = await query.message.reply_text("Редактируйте поля. Нажмите на нужное поле для изменения.", reply_markup=markup)
            context.user_data['pending_report_msg_id'] = msg.message_id
            return SHOW_REPORT_MENU

async def message_fill_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод значения после того, как пользователь нажал кнопку поля."""
    awaiting = context.user_data.get('awaiting_field')
    if not awaiting:
        return

    text = update.message.text.strip()
    numeric_keys = [k for k, _ in NUMERIC_FIELDS]
    try:
        if awaiting in numeric_keys:
            val = int(text)
            if val < 0: raise ValueError("Число должно быть >= 0")
            context.user_data['pending_report'][awaiting] = val
            confirmation_msg = await update.message.reply_text(f"Сохранено: {FULL_FIELD_LABELS[awaiting]} = {val}")
        else:
            context.user_data['pending_report'][awaiting] = text
            confirmation_msg = await update.message.reply_text(f"Сохранено текстовое поле.")
        
        # Удаляем сообщение пользователя и подтверждение через 3 секунды
        await asyncio.sleep(3)
        prompt_msg_id = context.user_data.pop('prompt_msg_id', None)
        if prompt_msg_id:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation_msg.message_id)

    except ValueError:
        await update.message.reply_text("Пожалуйста, введите корректное целое число (>=0) или используйте /skip.", quote=True)
        # Отвечаем на сообщение пользователя с ошибкой
        return AWAITING_FIELD_VALUE
    finally:
        # Этот блок больше не нужен здесь, так как мы не выходим из состояния при ошибке
        context.user_data.pop('awaiting_field', None)
        context.user_data.pop('prompt_msg_id', None) # На всякий случай

    msg_id = context.user_data.get('pending_report_msg_id')
    if msg_id:
        try:
            new_markup = build_report_inline_keyboard(context.user_data['pending_report'])
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                text="Отчет обновлен. Нажмите на следующее поле или отправьте отчет.",
                reply_markup=new_markup
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить клавиатуру: {e}")
    return SHOW_REPORT_MENU

async def skip_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /skip — оставить значение по умолчанию (0 или пусто)."""
    awaiting = context.user_data.get('awaiting_field')
    if not awaiting:
        await update.message.reply_text("Нет активного поля для пропуска.")
        return

    numeric_keys = [k for k, _ in NUMERIC_FIELDS]
    if awaiting in numeric_keys:
        context.user_data['pending_report'][awaiting] = 0
    else:
        context.user_data['pending_report'][awaiting] = ""
    context.user_data.pop('awaiting_field', None)
    
    confirmation_msg = await update.message.reply_text("Поле пропущено и установлено по умолчанию.")

    # Удаляем подтверждение через 3 секунды
    await asyncio.sleep(3)
    prompt_msg_id = context.user_data.get('prompt_msg_id')
    if prompt_msg_id:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation_msg.message_id)

    msg_id = context.user_data.get('pending_report_msg_id')
    if msg_id:
        try:
            new_markup = build_report_inline_keyboard(context.user_data['pending_report'])
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                text="Отчет обновлен. Нажмите на следующее поле или отправьте отчет.",
                reply_markup=new_markup
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить клавиатуру после /skip: {e}")
    return SHOW_REPORT_MENU

# --- Логика просмотра отчетов ---
async def show_my_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reports = get_user_reports(user_id)

    if not reports:
        # Если отчетов нет, показываем сообщение и возвращаем пользователя в главное меню
        _, reply_markup = get_menu_for_user(user_id)
        await update.message.reply_text(
            "У вас пока нет ни одного отчета.",
            reply_markup=reply_markup
        )
        return

    # Формируем и отправляем сообщение с последним отчетом
    message_text = "📂 <b>Ваш последний отчет:</b>\n\n"
    for r in reports:
        report_date = r[0]
        message_text += (
            f"📅 <b>Дата:</b> {report_date}\n"
        )
        for i, (key, _) in enumerate(ALL_FIELDS):
            label = FULL_FIELD_LABELS.get(key, key)
            value = r[i+1]
            message_text += f" - {label}: {value or '<i>(пусто)</i>'}\n"
        message_text += "--------------------\n"

    # После просмотра отчетов возвращаем пользователю его основную клавиатуру
    _, reply_markup = get_menu_for_user(user_id)
    await update.message.reply_text(message_text, parse_mode='HTML', reply_markup=reply_markup)

async def show_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает администратору список всех зарегистрированных пользователей."""
    all_users = get_all_registered_users()

    if not all_users:
        await update.message.reply_text(
            "В системе пока нет зарегистрированных сотрудников.",
            reply_markup=admin_main_menu_keyboard()
        )
        return

    message_text = "👥 <b>Список всех зарегистрированных сотрудников:</b>\n\n"
    for user_id, first_name, last_name, employee_id, position in all_users:
        message_text += (
            f"<b>Имя:</b> {first_name}\n"
            f"<b>Фамилия:</b> {last_name}\n"
            f"<b>Должность:</b> {position}\n"
            f"<b>Табельный номер:</b> {employee_id}\n"
            f"<b>User ID:</b> <code>{user_id}</code>\n"
            "--------------------\n"
        )

    message_text += "\nℹ️ Чтобы исправить или удалить запись, используйте программу для работы с базами данных (например, DB Browser for SQLite) и откройте файл `reports_bot.db`."

    await update.message.reply_text(
        message_text, parse_mode='HTML', reply_markup=admin_main_menu_keyboard()
    )

# --- Логика удаления пользователя (для админа) ---
async def start_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог удаления пользователя."""
    await update.message.reply_text(
        "Введите табельный номер сотрудника, которого хотите удалить.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Отмена")]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    return DELETE_USER_PROMPT

async def prompt_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает подтверждение на удаление."""
    employee_id = update.message.text
    user_to_delete = get_user_by_employee_id(employee_id)

    if not user_to_delete:
        await update.message.reply_text(
            f"Сотрудник с табельным номером '{employee_id}' не найден."
            f"Сотрудник с табельным номером `{employee_id}` не найден.",
            parse_mode='MarkdownV2'
        )
        await show_main_menu(update, context)
        return ConversationHandler.END

    user_id, first_name, last_name = user_to_delete
    context.user_data['user_to_delete'] = {'id': user_id, 'name': f"{first_name} {last_name}"}

    await update.message.reply_text(
        f"Вы уверены, что хотите удалить сотрудника <b>{first_name} {last_name}</b>?\n"
        "<b>ВНИМАНИЕ:</b> Это действие удалит пользователя и все его отчеты без возможности восстановления.",
        parse_mode='HTML',
        reply_markup=confirm_delete_keyboard()
    )
    return DELETE_USER_CONFIRM

async def confirm_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Окончательно удаляет пользователя."""
    # Проверяем, что пользователь нажал "Да, удалить"
    if update.message.text != "Да, удалить":
        await update.message.reply_text("Удаление отменено.")
        await show_main_menu(update, context)
        return ConversationHandler.END

    user_to_delete = context.user_data.pop('user_to_delete', None)
    if user_to_delete and 'id' in user_to_delete:
        delete_user(user_to_delete['id'])
        await update.message.reply_text(f"Сотрудник {user_to_delete.get('name', 'N/A')} успешно удален.")
    else:
        await update.message.reply_text("Не удалось найти данные для удаления. Пожалуйста, начните заново.")
    
    await show_main_menu(update, context)
    return ConversationHandler.END

# --- Функции администратора ---
async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику по сотрудникам, исключая администраторов."""
    all_users = get_all_registered_users()
    # Исключаем администраторов из общего списка для статистики
    employees = [user for user in all_users if user[0] not in ADMIN_IDS]
    
    submitted_today_ids = get_users_submitted_today()
    
    # Считаем только сотрудников
    submitted_employees_count = len([uid for uid in submitted_today_ids if uid not in ADMIN_IDS])
    not_submitted_employees = [emp for emp in employees if emp[0] not in submitted_today_ids]

    text = (
        f"📊 <b>Статистика на {date.today()}:</b>\n\n"
        f"✅ Отправили отчет: <b>{submitted_employees_count}</b>\n" 
        f"❌ Не отправили отчет: <b>{len(not_submitted_employees)}</b>\n"
        f"👥 Всего сотрудников: <b>{len(employees)}</b>\n\n"
    )

    if not_submitted_employees:
        text += "<b>Список тех, кто не отправил отчет:</b>\n"
        for _, first_name, last_name, _, _ in not_submitted_employees:
            text += f" - {first_name} {last_name}\n"

    await update.message.reply_text(text, parse_mode='HTML', reply_markup=admin_main_menu_keyboard())

async def _send_reminders(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Внутренняя функция для поиска и отправки напоминаний. Возвращает количество отправленных."""
    all_users = get_all_registered_users()
    employees = [user for user in all_users if user[0] not in ADMIN_IDS]
    submitted_today_ids = get_users_submitted_today()
    not_submitted_employees = [emp for emp in employees if emp[0] not in submitted_today_ids]

    sent_count = 0
    logger.info(f"Найдено {len(not_submitted_employees)} сотрудников для отправки напоминания.")
    for user_id, _, _, _, _ in not_submitted_employees:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⏰ <b>Напоминание!</b>\nПожалуйста, не забудьте отправить ваш ежедневный отчет.",
                parse_mode='HTML'
            )
            sent_count += 1
            await asyncio.sleep(0.1) # Небольшая задержка, чтобы не перегружать API
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание пользователю {user_id}: {e}")
    return sent_count

async def remind_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ручного запуска рассылки напоминаний."""
    await update.message.reply_text("Начинаю рассылку напоминаний...")
    sent_count = await _send_reminders(context)

    await update.message.reply_text(
        f"✅ Рассылка завершена.\nНапоминания отправлены <b>{sent_count}</b> сотрудникам.",
        parse_mode='HTML',
        reply_markup=admin_main_menu_keyboard()
    )

async def download_csv_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_reports = get_all_reports_for_csv()
    if not all_reports:
        await update.message.reply_text("В базе данных пока нет отчетов.", reply_markup=admin_main_menu_keyboard())
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_ALL)
    headers, rows = get_all_reports_for_csv()
    writer.writerow(headers)
    for r in rows:
        # Приводим значения к строкам, убираем переносы
        cleaned = [str(x).replace("\n", " ").replace("\r", "") if x is not None else "" for x in r]
        writer.writerow(cleaned)

    output.seek(0)
    file_to_send = io.BytesIO(output.getvalue().encode('utf-8-sig')) # utf-8-sig для Excel
    file_to_send.name = f'all_reports_{date.today()}.csv'

    await context.bot.send_document(chat_id=update.effective_user.id, document=file_to_send)
    await update.message.reply_text("✅ Файл с отчетами отправлен.", reply_markup=admin_main_menu_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущий диалог."""
    user = update.effective_user
    if user:
        logger.info(f"Пользователь {user.first_name} (ID: {user.id}) отменил действие.")
    
    await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(update, context)
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет справочное сообщение в зависимости от роли пользователя."""
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_IDS

    if is_admin:
        text = (
            "ℹ️ <b>Справка для администратора</b>\n\n"
            "Вы можете использовать следующие команды через кнопки меню:\n"
            "📊 <b>Статистика за сегодня</b> - Показывает, кто сдал, а кто еще нет.\n"
            "🔔 <b>Напомнить всем</b> - Отправляет напоминание тем, кто не сдал отчет.\n"
            "📥 <b>Скачать все отчеты (CSV)</b> - Формирует и отправляет вам файл со всеми отчетами.\n"
            "👥 <b>Список сотрудников</b> - Показывает список всех зарегистрированных пользователей.\n"
            "🗑️ <b>Удалить сотрудника</b> - Запускает процесс удаления пользователя по табельному номеру.\n\n"
            "Также доступны команды:\n"
            "/start - Перезапуск бота и возврат в главное меню.\n"
            "/cancel - Отмена текущего действия и возврат в главное меню."
        )
    else:
        numeric_fields_info = "\n".join([f"• <i>{FULL_FIELD_LABELS.get(key, key)}</i>" for key, _ in NUMERIC_FIELDS])
        text_fields_info = "\n".join([f"• <i>{FULL_FIELD_LABELS.get(key, key)}</i>" for key, _ in TEXT_FIELDS])

        text = (
            "ℹ️ <b>Справка для сотрудника</b>\n\n"
            "Используйте кнопки меню для взаимодействия с ботом:\n"
            "📝 <b>Отправить отчет</b> - Заполнить и отправить ваш ежедневный отчет.\n"
            "📂 <b>Мои отчеты</b> - Просмотреть ваш последний отправленный отчет.\n\n"
            "<b>Как заполнять отчет:</b>\n"
            "При нажатии на кнопку 'Отправить отчет' появится меню с полями. Нажмите на поле, чтобы ввести значение.\n\n"
            "<b>Числовые поля (нужно ввести = 1 или 2 или 3):</b>\n"
            f"{numeric_fields_info}\n"
            "Если по какому-то из этих пунктов нет данных, просто отправьте <b>0</b> или нажмите /skip.\n\n"
            "<b>Текстовые поля (нужно написать текст):</b>\n"
            f"{text_fields_info}\n"
            "Если информации нет, нажмите /skip, чтобы оставить поле пустым.\n\n"
            "После заполнения всех полей нажмите '✅ Отправить отчёт'."
        )

    await update.message.reply_text(text, parse_mode='HTML')

async def scheduled_reminder_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Колбэк для автоматической отправки напоминаний по расписанию."""
    logger.info("Запуск автоматической рассылки напоминаний по расписанию.")
    sent_count = await _send_reminders(context)
    logger.info(f"Автоматическая рассылка завершена. Отправлено {sent_count} напоминаний.")

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки одобрения/отклонения регистрации."""
    query = update.callback_query
    await query.answer()

    action, user_id_str = query.data.split('|')
    user_id = int(user_id_str)
    admin = query.from_user

    original_text = query.message.text_html

    if action == 'approve':
        # Проверяем, есть ли пользователь еще в списке ожидания
        if not is_pending_approval(user_id):
            await query.edit_message_text(f"{original_text}\n\n<i>(Действие уже выполнено другим администратором)</i>", parse_mode='HTML')
            return

        try:
            # Отправляем пользователю приглашение к регистрации
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ Ваша заявка на доступ одобрена!\n\nТеперь вы можете начать регистрацию.",
                reply_markup=start_registration_keyboard()
            )
            await query.edit_message_text(f"{original_text}\n\n<b>✅ Одобрено администратором {admin.mention_html()}</b>", parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение одобренному пользователю {user_id}: {e}")
            await query.edit_message_text(f"{original_text}\n\n<i>Не удалось уведомить пользователя. Возможно, он заблокировал бота.</i>", parse_mode='HTML')

    elif action == 'reject':
        # Удаляем пользователя из списка ожидания
        with get_db_conn() as conn:
            conn.cursor().execute("DELETE FROM pending_users WHERE user_id = ?", (user_id,))
            conn.commit()

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ К сожалению, ваша заявка на доступ была отклонена."
            )
            await query.edit_message_text(f"{original_text}\n\n<b>❌ Отклонено администратором {admin.mention_html()}</b>", parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение отклоненному пользователю {user_id}: {e}")
            await query.edit_message_text(f"{original_text}\n\n<i>Не удалось уведомить пользователя. Возможно, он заблокировал бота.</i>", parse_mode='HTML')


# --- 5. ЗАПУСК БОТА ---

def main() -> None:
    """Основная функция для запуска бота."""
    if not BOT_TOKEN:
        logger.error("Токен бота не найден. Укажите его в .env файле (BOT_TOKEN=...).")
        return

    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    # Настройка ежедневных автоматических напоминаний
    try:
        timezone = pytz.timezone(TIMEZONE_STR)
        job_queue = application.job_queue
        # Запускать каждый день с понедельника (0) по пятницу (4) в 16:00
        job_queue.run_daily(
            scheduled_reminder_callback,
            time=time(hour=16, minute=0, tzinfo=timezone),
            days=(0, 1, 2, 3, 4)
        )
        logger.info(f"Запланирована ежедневная отправка напоминаний в 16:00 по часовому поясу {TIMEZONE_STR}")
    except pytz.UnknownTimeZoneError:
        logger.error(f"Неизвестный часовой пояс: '{TIMEZONE_STR}'. Автоматические напоминания не будут работать. "
                     f"Укажите корректный часовой пояс в .env файле (например, TIMEZONE=Asia/Tashkent).")

    # Основной обработчик, включающий все диалоги и кнопки
    conv_handler = ConversationHandler(
        entry_points=[
            # Точка входа в регистрацию теперь - кнопка, а не /start
            # CommandHandler("start", start), # Для новых пользователей
            MessageHandler(filters.Regex("^📝 Отправить отчет$"), start_submit_report),
            MessageHandler(filters.Regex("^🗑️ Удалить сотрудника$"), start_delete_user),
            # Новая точка входа в регистрацию
            MessageHandler(filters.Regex("^🚀 Начать регистрацию$"), start_registration),
        ],
        states={
            # Состояния регистрации
            AWAIT_REGISTRATION_START: [
                MessageHandler(filters.Regex("^🚀 Начать регистрацию$"), start_registration)
            ],
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_LAST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_last_name)],
            REGISTER_EMPLOYEE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_employee_id)],
            REGISTER_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_position)],
            
            # Состояние подтверждения редактирования
            CONFIRM_EDIT: [
                MessageHandler(filters.Regex("^Да, редактировать$"), start_edit_report),
                MessageHandler(filters.Regex("^Нет, вернуться в меню$"), cancel),
            ],
            
            # Состояния для нового процесса отчета
            SHOW_REPORT_MENU: [CallbackQueryHandler(callback_report_menu)],
            AWAITING_FIELD_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, message_fill_field),
                CommandHandler("skip", skip_field),
            ],

            # Состояния удаления пользователя
            DELETE_USER_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_delete_user)],
            DELETE_USER_CONFIRM: [
                MessageHandler(filters.Regex("^(Да, удалить|Отмена)$"), confirm_delete_user),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            # Этот обработчик ловит все варианты отмены и возврата в меню
            MessageHandler(filters.Regex("^(Отмена|⬅️ Назад в главное меню|Нет, вернуться в меню)$"), cancel),
        ],
        # Этот флаг позволяет обработчикам вне ConversationHandler работать
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    # Обработчики команд и кнопок главного меню
    application.add_handler(CommandHandler("start", start)) # Для существующих пользователей
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", show_main_menu))
    application.add_handler(MessageHandler(filters.Regex("^📂 Мои отчеты$"), show_my_reports))
    application.add_handler(MessageHandler(filters.Regex("^📊 Статистика за сегодня$"), show_admin_stats))
    application.add_handler(MessageHandler(filters.Regex(r"^🔔 Напомнить всем$"), remind_all_users))
    application.add_handler(MessageHandler(filters.Regex(r"^📥 Скачать все отчеты \(CSV\)$"), download_csv_reports))
    application.add_handler(MessageHandler(filters.Regex(r"^👥 Список сотрудников$"), show_all_users))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ Назад в главное меню$"), show_main_menu))

    # Обработчик для всех остальных сообщений (должен быть последним)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message_handler))


    application.run_polling()


if __name__ == "__main__":
    main()
