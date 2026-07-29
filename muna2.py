"""
MUNA BEAUTY — два связанных Telegram-бота.

- CLIENT BOT  — с ним общается клиент:
    * обычные сообщения пересылаются администратору
    * запись на услугу через кнопки (услуга -> дата -> свободное время -> телефон)
    * «Мои записи» — просмотр и отмена своих записей
    * «Портфолио» / «Прайс-лист» / «Адрес» — информационные кнопки
    * напоминание за 2 часа до визита и запрос отзыва после
    * защита от флуда (не больше N сообщений в минуту)
- ADMIN BOT   — с ним общается администратор:
    * доступ строго только у chat_id, перечисленных в ADMIN_CHAT_ID (через запятую) —
      все остальные получают вежливый отказ и ссылку на клиентский бот.
      Уведомления (новые записи, отзывы, отмены/переносы, утренняя сводка,
      пересланные сообщения клиента) уходят каждому из перечисленных админов.
    * Reply на пересланное сообщение клиента -> уходит клиенту
    * под уведомлением о новой записи — кнопка "Отменить запись"
    * нижняя клавиатура: Сегодня / Записи на дату / Экспорт CSV / Рассылка / Помощь
    * /today   — все записи на сегодня таблицей
    * /slots ГГГГ-ММ-ДД — записи на конкретную дату
    * /export  — выгрузка всех записей в CSV
    * /broadcast текст — рассылка всем клиентам
    * утренняя авто-рассылка со списком записей на день

Запуск:
    pip install -r requirements.txt
    (нужен доп. пакет для напоминаний: pip install "python-telegram-bot[job-queue]")
    export CLIENT_BOT_TOKEN=...   (или пропишите в .env)
    export ADMIN_BOT_TOKEN=...
    export ADMIN_CHAT_ID=...      (chat_id админа(ов), через запятую если их несколько; см. ниже как узнать)
    export CLIENT_BOT_USERNAME=... (юзернейм клиентского бота без @ — для ссылки в сообщении отказа)
    python main.py

Как первый раз узнать свой ADMIN_CHAT_ID:
    1. Оставьте ADMIN_CHAT_ID пустым и запустите бота.
    2. Каждый будущий админ пусть напишет /start админ-боту от своего аккаунта —
       бот пришлёт его chat_id.
    3. Впишите все эти числа в ADMIN_CHAT_ID через запятую, например:
       ADMIN_CHAT_ID=111111111,222222222
       и перезапустите ботов.
    После этого доступ к админ-боту будет строго только у перечисленных chat_id.
"""

import asyncio
import csv
import io
import logging
import json
import os
import signal
from collections import Counter, defaultdict, deque
from datetime import date as date_cls, datetime, time as time_cls, timedelta
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("muna-bots")

CLIENT_BOT_TOKEN = os.environ.get("CLIENT_BOT_TOKEN", "").strip()
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "").strip()

# ADMIN_CHAT_ID теперь может содержать несколько id через запятую, например:
#   ADMIN_CHAT_ID=111111111,222222222,333333333
# Уведомления (новая запись, отзыв, отмена/перенос клиентом, утренняя сводка,
# пересланные сообщения клиента) уходят КАЖДОМУ из этих chat_id.
_ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_IDS: list[str] = [x.strip() for x in _ADMIN_CHAT_ID_RAW.split(",") if x.strip()]
# Оставлено для обратной совместимости (используется как «признак, что хоть
# один админ настроен», и в местах, которым нужен ровно один id).
ADMIN_CHAT_ID = ADMIN_CHAT_IDS[0] if ADMIN_CHAT_IDS else ""

# Юзернейм клиентского бота (без @) — используется только для ссылки в сообщении
# отказа, если посторонний человек пишет админ-боту. Можно оставить пустым.
CLIENT_BOT_USERNAME = os.environ.get("CLIENT_BOT_USERNAME", "").strip().lstrip("@")

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "links.json"
BOOKINGS_FILE = BASE_DIR / "bookings.json"
REVIEWS_FILE = BASE_DIR / "reviews.json"
BLOCKED_FILE = BASE_DIR / "blocked_slots.json"
BONUSES_FILE = BASE_DIR / "bonuses.json"
BIRTHDAYS_FILE = BASE_DIR / "birthdays.json"
NOSHOWS_FILE = BASE_DIR / "noshows.json"

MEDIA_DIR = BASE_DIR / "media"
PORTFOLIO_DIR = MEDIA_DIR / "portfolio"     # положите сюда фото/видео примеров работ
PRICE_LIST_PDF = MEDIA_DIR / "price_list.pdf"  # если есть готовый PDF с ценами — положите сюда

# ---------------------------------------------------------------------------
# Настройки — поправьте под реальные данные Muna Beauty
# ---------------------------------------------------------------------------

# (код_услуги, подпись с эмодзи, цена одной строкой)
SERVICES = [
    ("makeup", "💄 Макияж", "150 смн"),
    ("hair", "💇‍♀️ Причёска", "80 смн"),
    ("photo", "📸 Фотосъёмка", "300 смн"),
    ("video", "🎥 Видеосъёмка", "500 смн"),
]
SERVICE_LABELS = {code: label for code, label, _ in SERVICES}
SERVICE_PRICES = {code: price for code, _, price in SERVICES}

# Числовое значение цены (в сомони) для подсчёта дохода в /stats.
# Если у вас другая валюта или структура цен — поправьте эти значения.
SERVICE_PRICE_VALUES = {
    "makeup": 150,
    "hair": 120,
    "photo": 300,
    "video": 500,
}

# Пороги для статуса клиента (по количеству когда-либо сделанных записей).
CLIENT_STATUS_THRESHOLDS = [
    (10, "🥇 VIP"),
    (3, "⭐ Постоянный"),
]
CLIENT_STATUS_NEW = "🆕 Новый"

# Бонусная система: фиксированное начисление за каждый состоявшийся визит.
BONUS_PER_VISIT = 15
BONUS_DISCOUNT_THRESHOLD = 200   # накопил столько — можно получить скидку
BONUS_DISCOUNT_PERCENT = 20      # размер скидки в %

# Поздравление с днём рождения.
BIRTHDAY_DISCOUNT_PERCENT = 20
BIRTHDAY_CHECK_HOUR = 9          # во сколько раз в день проверять дни рождения
BIRTHDAY_CHECK_MINUTE = 0

WORK_START_HOUR = 10      # начало рабочего дня
WORK_END_HOUR = 19        # последний слот начинается до этого часа
SLOT_MINUTES = 60         # длительность одного слота (одна для всех услуг)
DAYS_AHEAD = 7            # на сколько дней вперёд показывать даты

WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

REMINDER_HOURS_BEFORE = 2         # за сколько часов напомнить клиенту
REVIEW_DELAY_AFTER_MIN = SLOT_MINUTES  # через сколько минут после начала слота спросить отзыв

MORNING_REPORT_HOUR = 8           # во сколько присылать админу список записей на день
MORNING_REPORT_MINUTE = 0

FLOOD_LIMIT_MESSAGES = 5          # не больше стольких сообщений...
FLOOD_LIMIT_WINDOW_SECONDS = 60   # ...за этот период (секунды)

ADDRESSES = [

    {"title": "📍 Филиал 1", "text": "г. Душанбе, ул. Вторая, 25", "lat": 38.5731, "lon": 68.7794},
    {"title": "📍 Филиал 2", "text": "г. Пенджикент, ул. Даврон Бобораджабов 101", "lat": 39.493365, "lon":  67.605858},
]

# Ссылки на соцсети — поправьте на реальные. Если ссылки нет — просто удалите строку из списка.
SOCIAL_LINKS = [
    ("📷 Instagram", "https://www.instagram.com/munzifa_shodieva "),
    ("✈️ Telegram-канал", "https://t.me/muna_beauty"),
]

# Быстрые готовые ответы админа — жмёт кнопку под сообщением клиента, текст уходит сразу.
# Список легко менять/дополнять: (код, текст_который_уйдёт_клиенту)
QUICK_REPLIES = [
    ("hello", "Здравствуйте! 😊"),
    ("booked", "Записали, ждём вас ✅"),
    ("soon", "Спасибо за сообщение! Ответим в ближайшее время"),
    ("clarify", "Уточните, пожалуйста, желаемую дату и время"),
    ("busy", "К сожалению, это время уже занято, выберите другое, пожалуйста"),
    ("thanks", "Спасибо! Хорошего дня 🌸"),
]
QUICK_REPLY_TEXTS = {code: text for code, text in QUICK_REPLIES}

# Кнопки нижней клавиатуры админ-бота
ADMIN_MENU_TODAY = "📅 Сегодня"
ADMIN_MENU_SLOTS = "📆 Записи на дату"
ADMIN_MENU_CALENDAR = "🗓 Календарь"
ADMIN_MENU_ADD = "➕ Добавить запись"
ADMIN_MENU_STATS = "📈 Статистика"
ADMIN_MENU_EXPORT = "📤 Экспорт CSV"
ADMIN_MENU_BROADCAST = "📢 Рассылка"
ADMIN_MENU_CANCEL_ALL = "❌ Отменить предстоящие записи"
ADMIN_MENU_HELP = "ℹ️ Помощь"

# ---------------------------------------------------------------------------
# Хранилища (простые json-файлы)
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("%s повреждён, начинаю с пустого хранилища", path.name)
            return {}
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


LINKS = _load_json(DATA_FILE)          # { "<admin_message_id>": {"client_chat_id": int, "name": str} }
BOOKINGS = _load_json(BOOKINGS_FILE)    # { "2026-07-26_14:00": {...} }
REVIEWS = _load_json(REVIEWS_FILE)      # { "2026-07-26_14:00": {"score": int, "client_chat_id": int} }
BLOCKED = _load_json(BLOCKED_FILE)      # { "2026-07-26_14:00": true }  — слоты, закрытые админом вручную
BONUSES = _load_json(BONUSES_FILE)      # { "<client_chat_id>": 45 }  — накопленные бонусные баллы
BIRTHDAYS = _load_json(BIRTHDAYS_FILE)  # { "<client_chat_id>": "15.03" или "15.03.1995" }
NOSHOWS = _load_json(NOSHOWS_FILE)      # { "2026-07-26_14:00#169...": {...} } — история неявок (не влияет на статистику/бонусы)

# антифлуд: client_id -> deque(таймстампы последних сообщений)
_flood_tracker: dict[int, deque] = defaultdict(deque)


def _link_key(admin_chat_id, admin_message_id: int) -> str:
    # Составной ключ: одно и то же message_id может существовать параллельно
    # в разных чатах разных админов, поэтому чат обязательно учитываем.
    return f"{admin_chat_id}:{admin_message_id}"


def remember(admin_chat_id, admin_message_id: int, client_chat_id: int, name: str) -> None:
    LINKS[_link_key(admin_chat_id, admin_message_id)] = {"client_chat_id": client_chat_id, "name": name}
    _save_json(DATA_FILE, LINKS)


def recall(admin_chat_id, admin_message_id: int):
    return LINKS.get(_link_key(admin_chat_id, admin_message_id))


def save_booking(slot_key: str, service_code: str, client_chat_id: int, name: str, phone: str) -> None:
    BOOKINGS[slot_key] = {
        "service": service_code,
        "client_chat_id": client_chat_id,
        "name": name,
        "phone": phone,
    }
    _save_json(BOOKINGS_FILE, BOOKINGS)


def delete_booking(slot_key: str):
    entry = BOOKINGS.pop(slot_key, None)
    if entry is not None:
        _save_json(BOOKINGS_FILE, BOOKINGS)
    return entry


def add_bonus(client_chat_id: int, amount: int) -> int:
    """Начисляет бонусные баллы клиенту за состоявшийся визит. Возвращает новый общий баланс.
    Для ручных записей без Telegram (client_chat_id <= 0) бонусы не начисляются — уведомлять некого."""
    if client_chat_id <= 0:
        return 0
    key = str(client_chat_id)
    new_total = BONUSES.get(key, 0) + amount
    BONUSES[key] = new_total
    _save_json(BONUSES_FILE, BONUSES)
    return new_total


def mark_noshow(slot_key: str, job_queue) -> dict | None:
    """
    Отмечает, что клиент не пришёл: запись убирается из BOOKINGS (значит не попадёт
    в статистику /stats, не попадёт в бонусы — они начисляются только когда запись
    ещё существует на момент напоминания об отзыве), но данные клиента не теряются —
    сохраняются в отдельном noshows.json для истории/справки.
    """
    entry = delete_booking(slot_key)
    if entry is None:
        return None
    _cancel_jobs(job_queue, slot_key)
    log_key = f"{slot_key}#{int(datetime.now().timestamp())}"
    NOSHOWS[log_key] = {**entry, "slot_key": slot_key}
    _save_json(NOSHOWS_FILE, NOSHOWS)
    return entry


def _next_manual_client_id() -> int:
    """
    Отрицательный уникальный псевдо-ID для записей, которые админ добавляет вручную
    (клиент пришёл/позвонил напрямую, у него нет диалога с ботом). Реальные Telegram
    chat_id всегда положительные, поэтому отрицательные значения никогда с ними не
    совпадут — это не даёт ручным записям путаться со статусом реальных клиентов
    и не мешает статистике (каждая ручная запись считается как отдельный клиент).
    """
    return -int(datetime.now().timestamp() * 1000)


def is_slot_taken(slot_key: str) -> bool:
    return slot_key in BOOKINGS


def is_slot_blocked(slot_key: str) -> bool:
    return slot_key in BLOCKED


def block_slot(slot_key: str) -> None:
    BLOCKED[slot_key] = True
    _save_json(BLOCKED_FILE, BLOCKED)


def unblock_slot(slot_key: str) -> None:
    if BLOCKED.pop(slot_key, None) is not None:
        _save_json(BLOCKED_FILE, BLOCKED)


def is_slot_unavailable(slot_key: str) -> bool:
    """Слот недоступен для новой записи, если занят ИЛИ закрыт админом вручную."""
    return is_slot_taken(slot_key) or is_slot_blocked(slot_key)


def get_client_status(client_chat_id: int) -> str:
    """Статус клиента по общему числу когда-либо сделанных записей (включая прошедшие)."""
    total = sum(1 for v in BOOKINGS.values() if v.get("client_chat_id") == client_chat_id)
    for threshold, label in CLIENT_STATUS_THRESHOLDS:
        if total >= threshold:
            return label
    return CLIENT_STATUS_NEW


def is_flooding(client_id: int) -> bool:
    now = datetime.now().timestamp()
    dq = _flood_tracker[client_id]
    dq.append(now)
    while dq and now - dq[0] > FLOOD_LIMIT_WINDOW_SECONDS:
        dq.popleft()
    return len(dq) > FLOOD_LIMIT_MESSAGES


def _slot_datetime(slot_key: str) -> datetime:
    date_part, time_part = slot_key.split("_", 1)
    d = date_cls.fromisoformat(date_part)
    h, m = map(int, time_part.split(":"))
    return datetime.combine(d, time_cls(hour=h, minute=m))


# ---------------------------------------------------------------------------
# Проверка доступа к админ-боту — управлять ботом может только ADMIN_CHAT_ID
# ---------------------------------------------------------------------------


def _is_admin_chat(chat_id: int) -> bool:
    """
    Строгая проверка: доступ к админ-боту разрешён только chat_id,
    перечисленным в переменной окружения ADMIN_CHAT_ID (через запятую).

    Пока ADMIN_CHAT_ID не задан (самый первый запуск, ещё не знаем chat_id
    администратора) — разрешаем /start всем, чтобы администратор смог узнать
    свой chat_id и прописать его. Как только ADMIN_CHAT_ID задан — доступ
    строго только перечисленным там chat_id, все остальные получают отказ.
    """
    if not ADMIN_CHAT_IDS:
        return True
    return str(chat_id) in ADMIN_CHAT_IDS


async def _deny_admin_access(update: Update) -> None:
    """Отправляет вежливый отказ постороннему, который пишет админ-боту."""
    text = (
        "⛔ Этот бот предназначен только для администратора MUNA BEAUTY.\n"
        "Здесь нельзя записаться на услугу или задать вопрос."
    )
    if CLIENT_BOT_USERNAME:
        text += f"\n\nЕсли вы клиент — перейдите, пожалуйста, в бот для записи: https://t.me/{CLIENT_BOT_USERNAME}"
    else:
        text += "\n\nЕсли вы клиент — пожалуйста, воспользуйтесь ботом для записи (уточните ссылку у администратора)."
    await update.effective_message.reply_text(text, reply_markup=ReplyKeyboardRemove())


# ---------------------------------------------------------------------------
# CLIENT BOT — общается с клиентами
# ---------------------------------------------------------------------------

CLIENT_WELCOME = (
    "Здравствуйте! 👋 Это чат-бот MUNA BEAUTY.\n\n"
    "Можете написать вопрос — сообщение сразу увидит администратор и ответит вам "
    "прямо здесь. Либо воспользуйтесь кнопками ниже."
)


# Текст кнопок нижнего меню — используется и для отрисовки клавиатуры, и для
# распознавания нажатия (Telegram присылает нажатие такой кнопки как обычный текст).
MENU_BOOK = "📅 Записаться на услугу"
MENU_MY_BOOKINGS = "📋 Мои записи"
MENU_PORTFOLIO = "🖼 Портфолио"
MENU_PRICE = "💰 Прайс-лист"
MENU_ADDRESS = "📍 Адрес"
MENU_SOCIAL = "🌐 Наши соцсети"


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура снизу экрана (а не кнопки под сообщением)."""
    return ReplyKeyboardMarkup(
        [
            [MENU_BOOK],
            [MENU_MY_BOOKINGS],
            [MENU_PORTFOLIO, MENU_PRICE],
            [MENU_ADDRESS, MENU_SOCIAL],
        ],
        resize_keyboard=True,   # кнопки компактнее, не занимают весь экран
        is_persistent=True,     # клавиатура не пропадает после нажатия
    )


async def client_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["_menu_sent"] = True
    await update.message.reply_text(CLIENT_WELCOME, reply_markup=_main_menu_keyboard())


async def client_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Выберите действие:", reply_markup=_main_menu_keyboard())


async def client_book_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Выберите услугу:", reply_markup=_services_keyboard())


def _services_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{label} — {SERVICE_PRICES[code]}", callback_data=f"svc:{code}")]
        for code, label, _ in SERVICES
    ]
    return InlineKeyboardMarkup(rows)


def _dates_keyboard(service_code: str) -> InlineKeyboardMarkup:
    rows = []
    today = datetime.now().date()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_RU[d.weekday()]})"
        rows.append([InlineKeyboardButton(label, callback_data=f"date:{service_code}:{d.isoformat()}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к услугам", callback_data="book:start")])
    return InlineKeyboardMarkup(rows)


def _times_keyboard(service_code: str, date_iso: str):
    """Возвращает (клавиатура, количество недоступных слотов на эту дату — занятых или закрытых)."""
    rows = []
    taken_count = 0
    now = datetime.now()
    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        for minute in range(0, 60, SLOT_MINUTES):
            time_str = f"{hour:02d}:{minute:02d}"
            slot_key = f"{date_iso}_{time_str}"
            if is_slot_unavailable(slot_key):
                taken_count += 1
                continue
            if _slot_datetime(slot_key) <= now:
                continue  # прошедшее время сегодня не предлагаем
            rows.append([InlineKeyboardButton(time_str, callback_data=f"time:{service_code}:{date_iso}:{time_str}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к датам", callback_data=f"svc:{service_code}")])
    return InlineKeyboardMarkup(rows), taken_count


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поделиться контактом", request_contact=True)], ["Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def client_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обычные текстовые/медиа-сообщения от клиента: либо часть записи (ждём контакт), либо пересылка админу."""
    msg = update.message
    user = update.effective_user

    # Первое сообщение от пользователя (даже если он не жал /start) —
    # сразу показываем нижнюю клавиатуру, чтобы она не появлялась только после /start.
    if not context.user_data.get("_menu_sent"):
        context.user_data["_menu_sent"] = True
        await msg.reply_text(CLIENT_WELCOME, reply_markup=_main_menu_keyboard())

    pending = context.user_data.get("pending_booking")
    if pending:
        if msg.text and msg.text.strip().lower() == "отмена":
            context.user_data.pop("pending_booking", None)
            await msg.reply_text("Запись отменена.", reply_markup=_main_menu_keyboard())
            return
        await msg.reply_text(
            "Чтобы завершить запись, поделитесь контактом кнопкой ниже, либо напишите «Отмена».",
            reply_markup=_contact_keyboard(),
        )
        return

    # Нажатия постоянных кнопок снизу приходят как обычный текст — перехватываем их здесь,
    # до пересылки администратору.
    if msg.text == MENU_BOOK:
        await msg.reply_text("Выберите услугу:", reply_markup=_services_keyboard())
        return
    if msg.text == MENU_MY_BOOKINGS:
        text, keyboard = _build_my_bookings_view(msg.chat_id)
        await msg.reply_text(text, reply_markup=keyboard)
        return
    if msg.text == MENU_PORTFOLIO:
        await _send_portfolio(context, msg.chat_id)
        return
    if msg.text == MENU_PRICE:
        await _send_price(context, msg.chat_id)
        return
    if msg.text == MENU_ADDRESS:
        await _send_address(context, msg.chat_id)
        return
    if msg.text == MENU_SOCIAL:
        await _send_social(context, msg.chat_id)
        return

    if is_flooding(user.id):
        await msg.reply_text("Слишком много сообщений подряд. Пожалуйста, подождите минуту ⏳")
        return

    if not ADMIN_CHAT_ID:
        await msg.reply_text(
            "Бот пока не полностью настроен администратором. Попробуйте написать позже."
        )
        log.warning("ADMIN_CHAT_ID не задан — сообщение клиента не доставлено")
        return

    header = (
        f"📩 Новое сообщение\n"
        f"От: {user.full_name} (@{user.username or 'нет username'})\n"
        f"ID клиента: {user.id}\n"
        f"—"
    )

    admin_bot = context.bot_data["admin_bot"]
    qr_keyboard = _quick_replies_keyboard()

    # Файлы (фото/голос/документ) скачиваем один раз и рассылаем всем админам,
    # чтобы не дёргать Telegram API за одним и тем же файлом несколько раз.
    file_bytes = None
    if msg.photo:
        file_bytes = bytes(await (await msg.photo[-1].get_file()).download_as_bytearray())
    elif msg.voice:
        file_bytes = bytes(await (await msg.voice.get_file()).download_as_bytearray())
    elif msg.document:
        file_bytes = bytes(await (await msg.document.get_file()).download_as_bytearray())

    delivered_to = []  # (admin_chat_id, sent_message) — только успешные отправки

    for admin_chat_id in ADMIN_CHAT_IDS:
        try:
            if msg.text:
                sent = await admin_bot.send_message(
                    chat_id=admin_chat_id, text=f"{header}\n{msg.text}", reply_markup=qr_keyboard
                )
            elif msg.photo:
                sent = await admin_bot.send_photo(
                    chat_id=admin_chat_id, photo=file_bytes,
                    caption=f"{header}\n{msg.caption or ''}".strip(),
                    reply_markup=qr_keyboard,
                )
            elif msg.voice:
                sent = await admin_bot.send_voice(
                    chat_id=admin_chat_id, voice=file_bytes, caption=header, reply_markup=qr_keyboard
                )
            elif msg.document:
                sent = await admin_bot.send_document(
                    chat_id=admin_chat_id, document=file_bytes,
                    filename=msg.document.file_name,
                    caption=f"{header}\n{msg.caption or ''}".strip(),
                    reply_markup=qr_keyboard,
                )
            else:
                sent = await admin_bot.send_message(
                    chat_id=admin_chat_id, text=f"{header}\n[Сообщение неподдерживаемого типа]",
                    reply_markup=qr_keyboard,
                )
        except Exception:
            log.exception("Не удалось переслать сообщение клиента администратору %s", admin_chat_id)
            continue

        delivered_to.append((admin_chat_id, sent))

    if not delivered_to:
        await msg.reply_text("Не удалось отправить сообщение. Попробуйте ещё раз чуть позже.")
        return

    for admin_chat_id, sent in delivered_to:
        remember(admin_chat_id, sent.message_id, msg.chat_id, user.full_name)

    await msg.reply_text("Спасибо! Ваше сообщение передано администратору ✅")


async def client_contact_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Клиент поделился контактом — завершаем незакрытую запись, если она есть."""
    pending = context.user_data.get("pending_booking")
    if not pending:
        return  # контакт без активной записи — игнорируем

    contact = update.message.contact
    phone = contact.phone_number if contact else ""
    context.user_data.pop("pending_booking", None)

    await _finalize_booking(
        update, context,
        service_code=pending["service"],
        date_iso=pending["date_iso"],
        time_str=pending["time_str"],
        phone=phone,
    )


async def _finalize_booking(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             service_code: str, date_iso: str, time_str: str, phone: str) -> None:
    slot_key = f"{date_iso}_{time_str}"
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_slot_unavailable(slot_key):
        await update.message.reply_text(
            "Это время только что заняли или закрыли 😔 Выберите другое через /book.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    label = SERVICE_LABELS.get(service_code, service_code)
    price = SERVICE_PRICES.get(service_code, "")
    save_booking(slot_key, service_code, chat_id, user.full_name, phone)

    d = date_cls.fromisoformat(date_iso)
    confirm_text = (
        f"Вы записаны ✅\n\n"
        f"Услуга: {label} ({price})\n"
        f"Дата: {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n"
        f"Время: {time_str}\n\n"
        f"Мы напомним вам за {REMINDER_HOURS_BEFORE} ч. до визита."
    )
    await update.message.reply_text(confirm_text, reply_markup=_main_menu_keyboard())

    _schedule_reminder_and_review(context, slot_key, chat_id)

    if ADMIN_CHAT_IDS:
        admin_bot = context.bot_data["admin_bot"]
        status = get_client_status(chat_id)
        admin_text = (
            f"🆕 Новая запись\n"
            f"Клиент: {user.full_name} (@{user.username or 'нет username'})\n"
            f"Статус клиента: {status}\n"
            f"Телефон: {phone or 'не указан'}\n"
            f"ID клиента: {user.id}\n"
            f"Услуга: {label} ({price})\n"
            f"Дата: {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n"
            f"Время: {time_str}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚫 Не пришёл", callback_data=f"admin_noshow_ask:{slot_key}"),
            InlineKeyboardButton("❌ Отменить запись", callback_data=f"admin_cancel_ask:{slot_key}"),
        ]])
        for admin_chat_id in ADMIN_CHAT_IDS:
            try:
                sent = await admin_bot.send_message(chat_id=admin_chat_id, text=admin_text, reply_markup=keyboard)
                # Важно: запоминаем связь (админ, message_id) -> клиент, иначе Reply
                # на это уведомление даст ошибку "Не нашёл клиента для этого сообщения".
                remember(admin_chat_id, sent.message_id, chat_id, user.full_name)
            except Exception:
                log.exception("Не удалось уведомить админа %s о новой записи", admin_chat_id)


def _schedule_reminder_and_review(context: ContextTypes.DEFAULT_TYPE, slot_key: str, client_chat_id: int) -> None:
    """Ставит в очередь напоминание и запрос отзыва (в рамках клиентского бота)."""
    job_queue = context.application.job_queue
    if job_queue is None:
        log.warning("JobQueue недоступен — установите python-telegram-bot[job-queue]")
        return

    slot_dt = _slot_datetime(slot_key)
    now = datetime.now()

    reminder_at = slot_dt - timedelta(hours=REMINDER_HOURS_BEFORE)
    if reminder_at > now:
        job_queue.run_once(
            _send_reminder, when=reminder_at,
            data={"chat_id": client_chat_id, "slot_key": slot_key},
            name=f"reminder_{slot_key}",
        )

    review_at = slot_dt + timedelta(minutes=REVIEW_DELAY_AFTER_MIN)
    job_queue.run_once(
        _send_review_request, when=review_at,
        data={"chat_id": client_chat_id, "slot_key": slot_key},
        name=f"review_{slot_key}",
    )


async def _send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    slot_key = data["slot_key"]
    if slot_key not in BOOKINGS:
        return  # запись отменили — не напоминаем
    entry = BOOKINGS[slot_key]
    label = SERVICE_LABELS.get(entry["service"], entry["service"])
    date_part, time_part = slot_key.split("_", 1)
    d = date_cls.fromisoformat(date_part)
    text = (
        f"⏰ Напоминаем: у вас запись сегодня в {time_part}\n"
        f"Услуга: {label}\n"
        f"Дата: {d.strftime('%d.%m.%Y')}\n\n"
        f"Ждём вас в MUNA BEAUTY!"
    )
    try:
        await context.bot.send_message(chat_id=data["chat_id"], text=text)
    except Exception:
        log.exception("Не удалось отправить напоминание клиенту")


def _review_keyboard(slot_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(str(n), callback_data=f"review:{slot_key}:{n}") for n in range(1, 6)
    ]])


async def _send_review_request(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    slot_key = data["slot_key"]
    if slot_key not in BOOKINGS:
        return  # отменена или отмечена "не пришёл" — не спрашиваем отзыв и не начисляем бонус
    new_total = add_bonus(data["chat_id"], BONUS_PER_VISIT)
    bonus_line = f"\n\n🎁 Начислено бонусов: {BONUS_PER_VISIT} (всего на счету: {new_total})" if new_total else ""
    try:
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text="Спасибо, что посетили MUNA BEAUTY! Оцените, пожалуйста, наш сервис от 1 до 5:" + bonus_line,
            reply_markup=_review_keyboard(slot_key),
        )
    except Exception:
        log.exception("Не удалось запросить отзыв у клиента")


async def client_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, slot_key, score_str = query.data.split(":", 2)
    score = int(score_str)
    REVIEWS[slot_key] = {"score": score, "client_chat_id": query.message.chat_id}
    _save_json(REVIEWS_FILE, REVIEWS)
    await query.edit_message_text(f"Спасибо за оценку: {'⭐' * score}")

    if ADMIN_CHAT_IDS:
        admin_bot = context.bot_data["admin_bot"]
        for admin_chat_id in ADMIN_CHAT_IDS:
            try:
                await admin_bot.send_message(
                    chat_id=admin_chat_id,
                    text=f"⭐ Новый отзыв: {score}/5 (запись {slot_key})",
                )
            except Exception:
                log.exception("Не удалось переслать отзыв админу %s", admin_chat_id)


async def _send_portfolio(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    files = []
    if PORTFOLIO_DIR.exists():
        files = sorted(
            p for p in PORTFOLIO_DIR.iterdir()
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4", ".mov")
        )
    if not files:
        await context.bot.send_message(chat_id=chat_id, text="Портфолио скоро будет пополнено 🙌 Загляните позже!")
        return
    for f in files[:10]:
        try:
            if f.suffix.lower() in (".mp4", ".mov"):
                await context.bot.send_video(chat_id=chat_id, video=f.open("rb"))
            else:
                await context.bot.send_photo(chat_id=chat_id, photo=f.open("rb"))
        except Exception:
            log.exception("Не удалось отправить файл портфолио %s", f)


async def _send_price(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if PRICE_LIST_PDF.exists():
        await context.bot.send_document(chat_id=chat_id, document=PRICE_LIST_PDF.open("rb"))
        return
    lines = ["💰 Прайс-лист:"]
    for code, label, price in SERVICES:
        lines.append(f"{label} — {price}")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def _send_address(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if not ADDRESSES:
        await context.bot.send_message(chat_id=chat_id, text="Адрес скоро появится 🙌")
        return
    for addr in ADDRESSES:
        await context.bot.send_location(chat_id=chat_id, latitude=addr["lat"], longitude=addr["lon"])
        await context.bot.send_message(chat_id=chat_id, text=f"{addr['title']}: {addr['text']}")


async def _send_social(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if not SOCIAL_LINKS:
        await context.bot.send_message(chat_id=chat_id, text="Ссылки на соцсети скоро появятся 🙌")
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, url=url)] for label, url in SOCIAL_LINKS]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="🌐 <b>Мы в соцсетях</b>\nПодписывайтесь, чтобы не пропустить новости и акции ✨",
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def _build_my_bookings_view(chat_id: int):
    """Возвращает (текст, inline-клавиатура с кнопками отмены) для списка записей клиента."""
    now = datetime.now()
    my_slots = [
        (key, val) for key, val in BOOKINGS.items()
        if val["client_chat_id"] == chat_id and _slot_datetime(key) > now
    ]
    my_slots.sort(key=lambda kv: kv[0])

    if not my_slots:
        return "У вас нет предстоящих записей.", None

    rows = []
    lines = ["📋 Ваши записи:"]
    for key, val in my_slots:
        label = SERVICE_LABELS.get(val["service"], val["service"])
        date_part, time_part = key.split("_", 1)
        d = date_cls.fromisoformat(date_part)
        lines.append(f"\n{d.strftime('%d.%m.%Y')} {time_part} — {label}")
        rows.append([
            InlineKeyboardButton(
                f"🔄 Перенести {d.strftime('%d.%m')} {time_part}", callback_data=f"resched_ask:{key}"
            ),
            InlineKeyboardButton(
                f"❌ Отменить", callback_data=f"cancel_ask:{key}"
            ),
        ])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def client_my_bookings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Используется только для кнопки «Нет» внутри диалога отмены (редактирует то же сообщение)."""
    query = update.callback_query
    await query.answer()
    text, keyboard = _build_my_bookings_view(query.message.chat_id)
    await query.edit_message_text(text, reply_markup=keyboard)


async def client_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("cancel_ask:"):
        slot_key = data.split(":", 1)[1]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, отменить", callback_data=f"cancel_yes:{slot_key}"),
            InlineKeyboardButton("Нет", callback_data="mybookings:list"),
        ]])
        await query.edit_message_text("Точно отменить эту запись?", reply_markup=keyboard)
        return

    if data.startswith("cancel_yes:"):
        slot_key = data.split(":", 1)[1]
        entry = delete_booking(slot_key)
        _cancel_jobs(context.application.job_queue, slot_key)
        await query.edit_message_text("Запись отменена ✅")

        if entry and ADMIN_CHAT_IDS:
            admin_bot = context.bot_data["admin_bot"]
            date_part, time_part = slot_key.split("_", 1)
            for admin_chat_id in ADMIN_CHAT_IDS:
                try:
                    await admin_bot.send_message(
                        chat_id=admin_chat_id,
                        text=f"⚠️ Клиент {entry['name']} отменил запись на {date_part} {time_part}",
                    )
                except Exception:
                    log.exception("Не удалось уведомить админа %s об отмене клиентом", admin_chat_id)
        return


def _cancel_jobs(job_queue, slot_key: str) -> None:
    if job_queue is None:
        return
    for name in (f"reminder_{slot_key}", f"review_{slot_key}"):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()


def _resched_dates_keyboard(old_slot_key: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора новой даты при переносе существующей записи."""
    rows = []
    today = datetime.now().date()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_RU[d.weekday()]})"
        rows.append([InlineKeyboardButton(label, callback_data=f"resched_date:{old_slot_key}:{d.isoformat()}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к моим записям", callback_data="mybookings:list")])
    return InlineKeyboardMarkup(rows)


def _resched_times_keyboard(old_slot_key: str, date_iso: str):
    """Клавиатура выбора нового времени при переносе. Сам переносимый слот из списка исключается."""
    rows = []
    taken_count = 0
    now = datetime.now()
    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        for minute in range(0, 60, SLOT_MINUTES):
            time_str = f"{hour:02d}:{minute:02d}"
            slot_key = f"{date_iso}_{time_str}"
            if slot_key == old_slot_key:
                continue  # это то же самое время, что и сейчас
            if is_slot_unavailable(slot_key):
                taken_count += 1
                continue
            if _slot_datetime(slot_key) <= now:
                continue
            rows.append([InlineKeyboardButton(
                time_str, callback_data=f"resched_time:{old_slot_key}:{date_iso}:{time_str}"
            )])
    rows.append([InlineKeyboardButton("⬅️ Назад к датам", callback_data=f"resched_ask:{old_slot_key}")])
    return InlineKeyboardMarkup(rows), taken_count


async def client_reschedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает перенос существующей записи: resched_ask / resched_date / resched_time."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("resched_ask:"):
        old_slot_key = data.split(":", 1)[1]
        if old_slot_key not in BOOKINGS:
            await query.edit_message_text("Эта запись уже не активна.")
            return
        await query.edit_message_text(
            "Выберите новую дату:", reply_markup=_resched_dates_keyboard(old_slot_key)
        )
        return

    if data.startswith("resched_date:"):
        _, old_slot_key, date_iso = data.split(":", 2)
        if old_slot_key not in BOOKINGS:
            await query.edit_message_text("Эта запись уже не активна.")
            return
        keyboard, taken_count = _resched_times_keyboard(old_slot_key, date_iso)
        d = date_cls.fromisoformat(date_iso)
        text = f"Новая дата: {d.strftime('%d.%m.%Y')}\nВыберите свободное время:"
        if taken_count:
            text += f"\n(занято слотов на этот день: {taken_count}, они скрыты)"
        if len(keyboard.inline_keyboard) <= 1:
            text += "\n\nНа эту дату свободных слотов нет 😔 Выберите другой день."
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data.startswith("resched_time:"):
        _, old_slot_key, date_iso, time_str = data.split(":", 3)
        new_slot_key = f"{date_iso}_{time_str}"

        entry = BOOKINGS.get(old_slot_key)
        if entry is None:
            await query.edit_message_text("Эта запись уже не активна.")
            return

        if is_slot_unavailable(new_slot_key):
            keyboard, _ = _resched_times_keyboard(old_slot_key, date_iso)
            await query.edit_message_text(
                "Это время только что заняли или закрыли 😔 Выберите другое:", reply_markup=keyboard
            )
            return

        # Переносим: удаляем старый слот, создаём новый с теми же данными клиента.
        delete_booking(old_slot_key)
        save_booking(new_slot_key, entry["service"], entry["client_chat_id"], entry["name"], entry["phone"])

        # Старые напоминание/запрос отзыва больше не актуальны — отменяем и ставим новые.
        _cancel_jobs(context.application.job_queue, old_slot_key)
        _schedule_reminder_and_review(context, new_slot_key, entry["client_chat_id"])

        label = SERVICE_LABELS.get(entry["service"], entry["service"])
        price = SERVICE_PRICES.get(entry["service"], "")
        d = date_cls.fromisoformat(date_iso)
        await query.edit_message_text(
            f"Запись перенесена ✅\n\n"
            f"Услуга: {label} ({price})\n"
            f"Новая дата: {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n"
            f"Новое время: {time_str}"
        )

        if ADMIN_CHAT_IDS:
            admin_bot = context.bot_data["admin_bot"]
            old_date_part, old_time_part = old_slot_key.split("_", 1)
            for admin_chat_id in ADMIN_CHAT_IDS:
                try:
                    await admin_bot.send_message(
                        chat_id=admin_chat_id,
                        text=(
                            f"🔄 Клиент перенёс запись\n"
                            f"Клиент: {entry['name']}\n"
                            f"Телефон: {entry.get('phone') or 'не указан'}\n"
                            f"Услуга: {label} ({price})\n"
                            f"Было: {old_date_part} {old_time_part}\n"
                            f"Стало: {date_iso} {time_str}"
                        ),
                    )
                except Exception:
                    log.exception("Не удалось уведомить админа %s о переносе записи", admin_chat_id)
        return


async def client_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия кнопок записи: book:start / svc:X / date:X:Y / time:X:Y:Z."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "book:start":
        await query.edit_message_text("Выберите услугу:", reply_markup=_services_keyboard())
        return

    if data.startswith("svc:"):
        service_code = data.split(":", 1)[1]
        label = SERVICE_LABELS.get(service_code, service_code)
        price = SERVICE_PRICES.get(service_code, "")
        await query.edit_message_text(
            f"Услуга: {label} ({price})\nВыберите дату:", reply_markup=_dates_keyboard(service_code)
        )
        return

    if data.startswith("date:"):
        _, service_code, date_iso = data.split(":", 2)
        label = SERVICE_LABELS.get(service_code, service_code)
        keyboard, taken_count = _times_keyboard(service_code, date_iso)
        d = date_cls.fromisoformat(date_iso)
        text = f"Услуга: {label}\nДата: {d.strftime('%d.%m.%Y')}\nВыберите свободное время:"
        if taken_count:
            text += f"\n(занято слотов на этот день: {taken_count}, они скрыты)"
        if len(keyboard.inline_keyboard) <= 1:
            text += "\n\nНа эту дату свободных слотов нет 😔 Выберите другой день."
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data.startswith("time:"):
        _, service_code, date_iso, time_str = data.split(":", 3)
        slot_key = f"{date_iso}_{time_str}"

        if is_slot_unavailable(slot_key):
            keyboard, _ = _times_keyboard(service_code, date_iso)
            await query.edit_message_text(
                "Это время только что заняли или закрыли 😔 Выберите другое:", reply_markup=keyboard
            )
            return

        # запоминаем выбор и просим контакт для подтверждения
        context.user_data["pending_booking"] = {
            "service": service_code, "date_iso": date_iso, "time_str": time_str,
        }
        label = SERVICE_LABELS.get(service_code, service_code)
        d = date_cls.fromisoformat(date_iso)
        await query.edit_message_text(
            f"Услуга: {label}\nДата: {d.strftime('%d.%m.%Y')}\nВремя: {time_str}\n\n"
            f"Осталось поделиться контактом, чтобы подтвердить запись 👇"
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Нажмите кнопку ниже, чтобы поделиться номером телефона:",
            reply_markup=_contact_keyboard(),
        )
        return


# ---------------------------------------------------------------------------
# ADMIN BOT — общается с администратором
# ---------------------------------------------------------------------------

def _admin_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура снизу экрана у админ-бота."""
    return ReplyKeyboardMarkup(
        [
            [ADMIN_MENU_TODAY, ADMIN_MENU_CALENDAR],
            [ADMIN_MENU_SLOTS, ADMIN_MENU_STATS],
            [ADMIN_MENU_ADD, ADMIN_MENU_EXPORT],
            [ADMIN_MENU_BROADCAST, ADMIN_MENU_CANCEL_ALL],
            [ADMIN_MENU_HELP],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    context.user_data.pop("awaiting", None)
    context.user_data["_menu_sent"] = True
    await update.message.reply_text(
        "Бот администратора запущен.\n\n"
        f"Ваш chat_id: {update.effective_chat.id}\n\n"
        "Если переменная окружения ADMIN_CHAT_ID ещё не задана (или нужно добавить ещё "
        "админов) — впишите туда это число (через запятую, если админов несколько) "
        "и перезапустите ботов.\n\n"
        "Чтобы ответить клиенту: сделайте Reply на пересланное сообщение с его вопросом,\n"
        "или нажмите одну из кнопок готового ответа под самим сообщением.\n\n"
        "Остальные действия — кнопками снизу 👇\n\n"
        "Команды (то же самое, но текстом):\n"
        "/today — записи на сегодня\n"
        "/slots ГГГГ-ММ-ДД — записи на дату\n"
        "/calendar [ГГГГ-ММ-ДД] — интерактивный календарь дня (открыть/закрыть слоты, "
        "а на свободном 🟢 слоте можно сразу «Записать клиента»)\n"
        "/addbooking — добавить запись вручную (клиент по телефону, без Telegram)\n"
        "/stats — статистика за текущий месяц (учитывает и ручные записи)\n"
        "/export — выгрузить все записи в CSV\n"
        "/broadcast текст — разослать сообщение всем клиентам",
        reply_markup=_admin_menu_keyboard(),
    )


def _format_bookings_table(entries) -> str:
    if not entries:
        return "Записей нет."
    entries = sorted(entries, key=lambda kv: kv[0])
    lines = ["Время     | Услуга        | Клиент          | Телефон"]
    lines.append("-" * 55)
    for key, val in entries:
        time_part = key.split("_", 1)[1]
        label = SERVICE_LABELS.get(val["service"], val["service"])
        name = val.get("name", "")
        phone = val.get("phone", "") or "—"
        lines.append(f"{time_part:<9} | {label:<13} | {name:<15} | {phone}")
    return "```\n" + "\n".join(lines) + "\n```"


async def _show_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today_iso = datetime.now().date().isoformat()
    entries = [(k, v) for k, v in BOOKINGS.items() if k.startswith(f"{today_iso}_")]
    text = f"📅 Записи на сегодня ({today_iso}):\n\n" + _format_bookings_table(entries)
    await update.message.reply_text(text, parse_mode="Markdown")


async def admin_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    await _show_today(update, context)


async def _show_slots(update: Update, context: ContextTypes.DEFAULT_TYPE, date_iso: str) -> None:
    """Общая логика для /slots и для кнопки «Записи на дату»."""
    date_iso = date_iso.strip()
    try:
        date_cls.fromisoformat(date_iso)
    except ValueError:
        await update.message.reply_text(
            "Не понял дату. Введите в формате ГГГГ-ММ-ДД, например 2026-07-27",
            reply_markup=_admin_menu_keyboard(),
        )
        return
    entries = [(k, v) for k, v in BOOKINGS.items() if k.startswith(f"{date_iso}_")]
    text = f"Записи на {date_iso}:\n\n" + _format_bookings_table(entries)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_admin_menu_keyboard())


async def admin_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/slots 2026-07-26 — таблица записей на дату."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    if not context.args:
        await update.message.reply_text("Использование: /slots 2026-07-26")
        return
    await _show_slots(update, context, context.args[0])


# --- Интерактивный календарь дня -------------------------------------------------

def _calendar_text(date_iso: str) -> str:
    d = date_cls.fromisoformat(date_iso)
    return (
        f"🗓 Календарь на {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n\n"
        f"🟢 свободно  ·  🔴 занято (📝 — добавлено вручную)  ·  ⛔ закрыто\n"
        f"Нажмите 🟢 чтобы записать клиента или закрыть слот, ⛔ чтобы открыть, "
        f"🔴 чтобы увидеть данные клиента."
    )


def _calendar_keyboard(date_iso: str) -> InlineKeyboardMarkup:
    rows = []
    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        for minute in range(0, 60, SLOT_MINUTES):
            time_str = f"{hour:02d}:{minute:02d}"
            slot_key = f"{date_iso}_{time_str}"
            if slot_key in BOOKINGS:
                name = BOOKINGS[slot_key].get("name", "")
                manual_mark = "📝" if BOOKINGS[slot_key].get("manual") else ""
                label = f"🔴 {time_str} — {manual_mark}{name}"
                callback = f"cal_info:{slot_key}"
            elif is_slot_blocked(slot_key):
                label = f"⛔ {time_str} — закрыто"
                callback = f"cal_toggle:{slot_key}"
            else:
                label = f"🟢 {time_str} — свободно"
                callback = f"cal_free:{slot_key}"
            rows.append([InlineKeyboardButton(label, callback_data=callback)])

    d = date_cls.fromisoformat(date_iso)
    prev_day = (d - timedelta(days=1)).isoformat()
    next_day = (d + timedelta(days=1)).isoformat()
    rows.append([
        InlineKeyboardButton("⬅️", callback_data=f"cal_date:{prev_day}"),
        InlineKeyboardButton("Сегодня", callback_data=f"cal_date:{datetime.now().date().isoformat()}"),
        InlineKeyboardButton("➡️", callback_data=f"cal_date:{next_day}"),
    ])
    return InlineKeyboardMarkup(rows)


async def _show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE, date_iso: str) -> None:
    await update.message.reply_text(_calendar_text(date_iso), reply_markup=_calendar_keyboard(date_iso))


async def admin_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/calendar [ГГГГ-ММ-ДД] — интерактивный календарь дня (по умолчанию сегодня)."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    date_iso = context.args[0] if context.args else datetime.now().date().isoformat()
    try:
        date_cls.fromisoformat(date_iso)
    except ValueError:
        await update.message.reply_text("Не понял дату. Формат: /calendar 2026-07-27")
        return
    await _show_calendar(update, context, date_iso)


async def admin_calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия в интерактивном календаре: cal_date / cal_toggle / cal_info."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    data = query.data

    if data.startswith("cal_date:"):
        date_iso = data.split(":", 1)[1]
        await query.answer()
        await query.edit_message_text(_calendar_text(date_iso), reply_markup=_calendar_keyboard(date_iso))
        return

    if data.startswith("cal_free:"):
        slot_key = data.split(":", 1)[1]
        await query.answer()
        if slot_key in BOOKINGS:
            date_iso = slot_key.split("_", 1)[0]
            await query.edit_message_text(_calendar_text(date_iso), reply_markup=_calendar_keyboard(date_iso))
            return
        date_iso, time_part = slot_key.split("_", 1)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Записать клиента", callback_data=f"cal_add_start:{slot_key}")],
            [InlineKeyboardButton("⛔ Закрыть слот", callback_data=f"cal_toggle:{slot_key}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"cal_date:{date_iso}")],
        ])
        await query.edit_message_text(
            f"🟢 {date_iso} {time_part} — свободно.\nЧто сделать?", reply_markup=keyboard
        )
        return

    if data.startswith("cal_add_start:"):
        slot_key = data.split(":", 1)[1]
        if slot_key in BOOKINGS:
            await query.answer("Уже занято другой записью", show_alert=True)
            date_iso = slot_key.split("_", 1)[0]
            await query.edit_message_text(_calendar_text(date_iso), reply_markup=_calendar_keyboard(date_iso))
            return
        await query.answer()
        date_iso, time_str = slot_key.split("_", 1)
        context.user_data["add_booking"] = {"date_iso": date_iso, "time_str": time_str}
        await query.edit_message_text(
            f"➕ Запись на {date_iso} {time_str}\n\nВыберите услугу:",
            reply_markup=_calendar_add_services_keyboard(date_iso, time_str),
        )
        return

    if data.startswith("cal_add_svc:"):
        _, date_iso, time_str, service_code = data.split(":", 3)
        slot_key = f"{date_iso}_{time_str}"
        if slot_key in BOOKINGS:
            await query.answer("Уже занято другой записью", show_alert=True)
            await query.edit_message_text(_calendar_text(date_iso), reply_markup=_calendar_keyboard(date_iso))
            return
        await query.answer()
        context.user_data["add_booking"] = {
            "service": service_code, "date_iso": date_iso, "time_str": time_str,
        }
        context.user_data["awaiting"] = "add_name"
        label = SERVICE_LABELS.get(service_code, service_code)
        await query.edit_message_text(
            f"Услуга: {label}\nДата: {date_iso}, время: {time_str}\n\n"
            f"Введите имя клиента (или «Отмена»):"
        )
        return

    if data.startswith("cal_toggle:"):
        slot_key = data.split(":", 1)[1]
        if slot_key in BOOKINGS:
            await query.answer("Это время уже занято записью, сначала отмените её", show_alert=True)
            return
        date_iso = slot_key.split("_", 1)[0]
        if is_slot_blocked(slot_key):
            unblock_slot(slot_key)
            await query.answer("Слот открыт ✅")
        else:
            block_slot(slot_key)
            await query.answer("Слот закрыт ⛔")
        await query.edit_message_text(_calendar_text(date_iso), reply_markup=_calendar_keyboard(date_iso))
        return

    if data.startswith("cal_info:"):
        slot_key = data.split(":", 1)[1]
        entry = BOOKINGS.get(slot_key)
        date_iso, time_part = slot_key.split("_", 1)
        if not entry:
            await query.answer("Запись не найдена (возможно, уже отменена)", show_alert=True)
            await query.edit_message_text(_calendar_text(date_iso), reply_markup=_calendar_keyboard(date_iso))
            return
        label = SERVICE_LABELS.get(entry["service"], entry["service"])
        status = get_client_status(entry["client_chat_id"])
        manual_note = " · запись без Telegram" if entry.get("manual") else ""
        await query.answer()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Клиент не пришёл", callback_data=f"admin_noshow_ask:{slot_key}")],
            [InlineKeyboardButton("❌ Отменить запись", callback_data=f"admin_cancel_ask:{slot_key}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"cal_date:{date_iso}")],
        ])
        await query.edit_message_text(
            f"🔴 {date_iso} {time_part}{manual_note}\n\n"
            f"Клиент: {entry.get('name', '')}\n"
            f"Услуга: {label}\n"
            f"Тел.: {entry.get('phone') or 'не указан'}\n"
            f"Статус: {status}",
            reply_markup=keyboard,
        )
        return


# --- Ручное добавление записи админом (клиент без Telegram, по звонку/визиту) ------

def _calendar_add_services_keyboard(date_iso: str, time_str: str) -> InlineKeyboardMarkup:
    """Выбор услуги, когда дата и время уже зафиксированы (запись стартовала из календаря)."""
    rows = [
        [InlineKeyboardButton(f"{label} — {SERVICE_PRICES[code]}", callback_data=f"cal_add_svc:{date_iso}:{time_str}:{code}")]
        for code, label, _ in SERVICES
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"cal_date:{date_iso}")])
    return InlineKeyboardMarkup(rows)


def _admin_add_services_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{label} — {SERVICE_PRICES[code]}", callback_data=f"aadd_svc:{code}")]
        for code, label, _ in SERVICES
    ]
    return InlineKeyboardMarkup(rows)


def _admin_add_dates_keyboard(service_code: str) -> InlineKeyboardMarkup:
    rows = []
    today = datetime.now().date()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_RU[d.weekday()]})"
        rows.append([InlineKeyboardButton(label, callback_data=f"aadd_date:{service_code}:{d.isoformat()}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к услугам", callback_data="aadd_back_svc")])
    return InlineKeyboardMarkup(rows)


def _admin_add_times_keyboard(service_code: str, date_iso: str):
    """Для ручной записи показываем и закрытые (⛔) слоты — выбор автоматически их откроет.
    Скрыты только слоты, уже занятые реальной записью."""
    rows = []
    now = datetime.now()
    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        for minute in range(0, 60, SLOT_MINUTES):
            time_str = f"{hour:02d}:{minute:02d}"
            slot_key = f"{date_iso}_{time_str}"
            if is_slot_taken(slot_key):
                continue
            if _slot_datetime(slot_key) <= now:
                continue
            mark = "⛔ " if is_slot_blocked(slot_key) else ""
            rows.append([InlineKeyboardButton(f"{mark}{time_str}", callback_data=f"aadd_time:{service_code}:{date_iso}:{time_str}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к датам", callback_data=f"aadd_svc:{service_code}")])
    return InlineKeyboardMarkup(rows)


async def admin_addbooking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addbooking и кнопка «➕ Добавить запись» — старт ручного добавления записи."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    context.user_data.pop("awaiting", None)
    context.user_data["add_booking"] = {}
    await update.message.reply_text(
        "➕ Добавление записи вручную (для клиента без Telegram)\n\nВыберите услугу:",
        reply_markup=_admin_add_services_keyboard(),
    )


async def admin_addbooking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает шаги выбора услуги/даты/времени при ручном добавлении записи (aadd_*)."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "aadd_back_svc":
        await query.edit_message_text(
            "➕ Добавление записи вручную (для клиента без Telegram)\n\nВыберите услугу:",
            reply_markup=_admin_add_services_keyboard(),
        )
        return

    if data.startswith("aadd_svc:"):
        service_code = data.split(":", 1)[1]
        context.user_data.setdefault("add_booking", {})["service"] = service_code
        label = SERVICE_LABELS.get(service_code, service_code)
        await query.edit_message_text(
            f"Услуга: {label}\nВыберите дату:", reply_markup=_admin_add_dates_keyboard(service_code)
        )
        return

    if data.startswith("aadd_date:"):
        _, service_code, date_iso = data.split(":", 2)
        context.user_data.setdefault("add_booking", {})["date_iso"] = date_iso
        keyboard = _admin_add_times_keyboard(service_code, date_iso)
        d = date_cls.fromisoformat(date_iso)
        text = (
            f"Дата: {d.strftime('%d.%m.%Y')}\n"
            f"Выберите время (⛔ — сейчас закрыто вручную, при выборе автоматически откроется):"
        )
        if len(keyboard.inline_keyboard) <= 1:
            text += "\n\nНа эту дату свободных слотов нет."
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data.startswith("aadd_time:"):
        _, service_code, date_iso, time_str = data.split(":", 3)
        slot_key = f"{date_iso}_{time_str}"
        if is_slot_taken(slot_key):
            await query.edit_message_text(
                "Это время уже заняла другая запись. Выберите другое:",
                reply_markup=_admin_add_times_keyboard(service_code, date_iso),
            )
            return
        booking_data = context.user_data.setdefault("add_booking", {})
        booking_data["service"] = service_code
        booking_data["date_iso"] = date_iso
        booking_data["time_str"] = time_str
        context.user_data["awaiting"] = "add_name"
        label = SERVICE_LABELS.get(service_code, service_code)
        await query.edit_message_text(
            f"Услуга: {label}\nДата: {date_iso}, время: {time_str}\n\n"
            f"Введите имя клиента (или «Отмена»):"
        )
        return


async def _finalize_admin_booking(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   booking_data: dict, phone: str) -> None:
    """Сохраняет запись, добавленную админом вручную (без Telegram-клиента)."""
    service_code = booking_data.get("service")
    date_iso = booking_data.get("date_iso")
    time_str = booking_data.get("time_str")
    name = booking_data.get("name") or "Клиент"

    if not (service_code and date_iso and time_str):
        await update.message.reply_text(
            "Что-то пошло не так, начните заново: /addbooking", reply_markup=_admin_menu_keyboard()
        )
        return

    slot_key = f"{date_iso}_{time_str}"
    if is_slot_taken(slot_key):
        await update.message.reply_text(
            "Это время уже заняла другая запись. Начните заново: /addbooking",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    unblock_slot(slot_key)  # если слот был закрыт вручную — запись автоматически его открывает
    fake_chat_id = _next_manual_client_id()
    save_booking(slot_key, service_code, fake_chat_id, name, phone)
    BOOKINGS[slot_key]["manual"] = True
    _save_json(BOOKINGS_FILE, BOOKINGS)

    label = SERVICE_LABELS.get(service_code, service_code)
    price = SERVICE_PRICES.get(service_code, "")
    d = date_cls.fromisoformat(date_iso)
    await update.message.reply_text(
        f"✅ Запись добавлена вручную\n\n"
        f"Клиент: {name}\n"
        f"Телефон: {phone or 'не указан'}\n"
        f"Услуга: {label} ({price})\n"
        f"Дата: {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n"
        f"Время: {time_str}\n\n"
        f"(запись без Telegram — напоминание и запрос отзыва клиенту не отправляются, "
        f"но она учтена в статистике, календаре и экспорте CSV)",
        reply_markup=_admin_menu_keyboard(),
    )


# --- Статистика --------------------------------------------------------------------

def _compute_stats() -> dict:
    """Считает статистику за текущий календарный месяц по данным BOOKINGS."""
    now = datetime.now()
    month_prefix = now.strftime("%Y-%m")
    month_entries = [(k, v) for k, v in BOOKINGS.items() if k.startswith(month_prefix)]

    total_count = len(month_entries)
    revenue = sum(SERVICE_PRICE_VALUES.get(v["service"], 0) for _, v in month_entries)

    service_counter = Counter(v["service"] for _, v in month_entries)
    top = service_counter.most_common(1)
    top_service_label = SERVICE_LABELS.get(top[0][0], top[0][0]) if top else "—"

    weekday_counter = Counter(_slot_datetime(k).weekday() for k, _ in month_entries)
    best = weekday_counter.most_common(1)
    best_day_label = WEEKDAY_RU[best[0][0]] if best else "—"

    # Новый клиент — все его записи в этом месяце. Повторный — есть запись до этого месяца.
    month_clients = {v["client_chat_id"] for _, v in month_entries}
    new_clients = 0
    repeat_clients = 0
    for cid in month_clients:
        had_before = any(
            v.get("client_chat_id") == cid and not k.startswith(month_prefix)
            for k, v in BOOKINGS.items()
        )
        if had_before:
            repeat_clients += 1
        else:
            new_clients += 1

    return {
        "month_label": now.strftime("%m.%Y"),
        "total_count": total_count,
        "revenue": revenue,
        "top_service_label": top_service_label,
        "best_day_label": best_day_label,
        "new_clients": new_clients,
        "repeat_clients": repeat_clients,
    }


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — статистика записей за текущий календарный месяц."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    stats = _compute_stats()
    text = (
        f"📈 Статистика за {stats['month_label']}\n\n"
        f"Записей за месяц: {stats['total_count']}\n"
        f"Ожидаемый доход (по прайсу): {stats['revenue']} смн\n"
        f"Самая популярная услуга: {stats['top_service_label']}\n"
        f"Лучший день недели: {stats['best_day_label']}\n"
        f"Новых клиентов: {stats['new_clients']}\n"
        f"Повторных клиентов: {stats['repeat_clients']}\n\n"
        f"(доход считается по всем записям месяца, включая ещё не состоявшиеся;\n"
        f"отменённые записи в подсчёт не входят)"
    )
    await update.message.reply_text(text)


async def admin_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export — выгрузить все записи в CSV (открывается как таблица)."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    if not BOOKINGS:
        await update.message.reply_text("Записей пока нет.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Дата", "Время", "Услуга", "Клиент", "Телефон", "ID клиента"])
    for key, val in sorted(BOOKINGS.items()):
        date_part, time_part = key.split("_", 1)
        label = SERVICE_LABELS.get(val["service"], val["service"])
        writer.writerow([date_part, time_part, label, val.get("name", ""), val.get("phone", ""), val.get("client_chat_id", "")])

    buffer.seek(0)
    data_bytes = buffer.getvalue().encode("utf-8-sig")  # BOM для корректного открытия в Excel
    filename = f"bookings_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    await update.message.reply_document(document=io.BytesIO(data_bytes), filename=filename)


async def _do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Общая логика для /broadcast и для кнопки «Рассылка»."""
    client_bot = context.bot_data["client_bot"]
    already_sent = set()
    count = 0
    for entry in LINKS.values():
        cid = entry["client_chat_id"]
        if cid in already_sent:
            continue
        already_sent.add(cid)
        try:
            await client_bot.send_message(chat_id=cid, text=text)
            count += 1
        except Exception:
            log.exception("Не удалось разослать сообщение чату %s", cid)

    await update.message.reply_text(f"Рассылка отправлена {count} клиент(ам).", reply_markup=_admin_menu_keyboard())


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast текст — разослать сообщение всем клиентам, которые уже писали боту."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Использование: /broadcast Текст сообщения")
        return
    await _do_broadcast(update, context, text)


def _quick_replies_keyboard() -> InlineKeyboardMarkup:
    """Кнопки готовых ответов под пересланным сообщением клиента (2 в ряд)."""
    buttons = [InlineKeyboardButton(text, callback_data=f"qr:{code}") for code, text in QUICK_REPLIES]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def admin_quick_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ нажал кнопку готового ответа под пересланным сообщением клиента."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    code = query.data.split(":", 1)[1]
    text = QUICK_REPLY_TEXTS.get(code)

    if not text:
        await query.answer("Неизвестный шаблон ответа", show_alert=True)
        return

    link = recall(query.message.chat_id, query.message.message_id)
    if not link:
        await query.answer(
            "Не нашёл клиента для этого сообщения (бот перезапускался?)", show_alert=True
        )
        return

    client_bot = context.bot_data["client_bot"]
    try:
        await client_bot.send_message(chat_id=link["client_chat_id"], text=text)
    except Exception:
        log.exception("Не удалось отправить быстрый ответ клиенту")
        await query.answer("Не удалось отправить клиенту", show_alert=True)
        return

    await query.answer("Отправлено клиенту ✅")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"✅ Клиенту отправлен быстрый ответ: «{text}»",
        reply_to_message_id=query.message.message_id,
    )


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message

    if not msg.reply_to_message:
        await msg.reply_text(
            "Чтобы ответить клиенту, сделайте Reply на его сообщение "
            "(долгое нажатие по сообщению → «Ответить»)."
        )
        return

    link = recall(msg.chat_id, msg.reply_to_message.message_id)
    if not link:
        await msg.reply_text(
            "Не нашёл клиента для этого сообщения (например, бот перезапускался, "
            "и links.json был очищен)."
        )
        return

    client_bot = context.bot_data["client_bot"]
    client_chat_id = link["client_chat_id"]

    try:
        if msg.text:
            await client_bot.send_message(chat_id=client_chat_id, text=msg.text)
        elif msg.photo:
            file = await msg.photo[-1].get_file()
            data = await file.download_as_bytearray()
            await client_bot.send_photo(chat_id=client_chat_id, photo=bytes(data), caption=msg.caption)
        elif msg.voice:
            file = await msg.voice.get_file()
            data = await file.download_as_bytearray()
            await client_bot.send_voice(chat_id=client_chat_id, voice=bytes(data))
        elif msg.document:
            file = await msg.document.get_file()
            data = await file.download_as_bytearray()
            await client_bot.send_document(
                chat_id=client_chat_id, document=bytes(data),
                filename=msg.document.file_name, caption=msg.caption,
            )
        else:
            await msg.reply_text("Этот тип сообщения пока нельзя переслать клиенту.")
            return
        await msg.reply_text("Ответ отправлен клиенту ✅")
    except Exception as exc:
        log.exception("Не удалось отправить ответ клиенту")
        await msg.reply_text(f"Не удалось отправить ответ: {exc}")


async def admin_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Единая точка входа для всех обычных сообщений от админа (без reply).
    Разбирает: нажатия кнопок нижнего меню, ввод даты/текста после них,
    а всё остальное отдаёт в admin_reply (Reply клиенту).

    Строго проверяет, что пишет именно администратор (ADMIN_CHAT_ID) — иначе
    отправляет отказ в доступе и ничего не выполняет.
    """
    msg = update.message
    text = (msg.text or "").strip()

    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return

    # Первое сообщение от админа (даже если он не жал /start) — сразу показываем меню.
    if not context.user_data.get("_menu_sent"):
        context.user_data["_menu_sent"] = True
        await msg.reply_text("Меню администратора 👇", reply_markup=_admin_menu_keyboard())
        return

    # --- Шаг 2 диалога: админ вводит дату после «Записи на дату» ---
    if context.user_data.get("awaiting") == "slots":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            await msg.reply_text("Отменено.", reply_markup=_admin_menu_keyboard())
            return
        await _show_slots(update, context, text)
        return

    # --- Шаг 2 диалога: админ вводит текст после «Рассылка» ---
    if context.user_data.get("awaiting") == "broadcast":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            await msg.reply_text("Рассылка отменена.", reply_markup=_admin_menu_keyboard())
            return
        await _do_broadcast(update, context, text)
        return

    # --- Шаги диалога ручного добавления записи: имя клиента, затем телефон ---
    if context.user_data.get("awaiting") == "add_name":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("add_booking", None)
            await msg.reply_text("Добавление записи отменено.", reply_markup=_admin_menu_keyboard())
            return
        context.user_data.setdefault("add_booking", {})["name"] = text
        context.user_data["awaiting"] = "add_phone"
        await msg.reply_text("Введите телефон клиента (или «-» если не указан, или «Отмена»):")
        return

    if context.user_data.get("awaiting") == "add_phone":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("add_booking", None)
            await msg.reply_text("Добавление записи отменено.", reply_markup=_admin_menu_keyboard())
            return
        phone = "" if text.strip() == "-" else text.strip()
        booking_data = context.user_data.pop("add_booking", {})
        await _finalize_admin_booking(update, context, booking_data, phone)
        return

    # --- Нажатия кнопок нижнего меню ---
    if text == ADMIN_MENU_TODAY:
        await _show_today(update, context)
        return

    if text == ADMIN_MENU_CALENDAR:
        await _show_calendar(update, context, datetime.now().date().isoformat())
        return

    if text == ADMIN_MENU_SLOTS:
        context.user_data["awaiting"] = "slots"
        await msg.reply_text(
            "Введите дату в формате ГГГГ-ММ-ДД (например 2026-07-27) или «Отмена»:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text == ADMIN_MENU_ADD:
        await admin_addbooking_start(update, context)
        return

    if text == ADMIN_MENU_STATS:
        await admin_stats(update, context)
        return

    if text == ADMIN_MENU_EXPORT:
        await admin_export(update, context)
        return

    if text == ADMIN_MENU_BROADCAST:
        context.user_data["awaiting"] = "broadcast"
        await msg.reply_text(
            "Введите текст рассылки или «Отмена»:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text == ADMIN_MENU_CANCEL_ALL:
        await admin_cancel_all_start(update, context)
        return

    if text == ADMIN_MENU_HELP:
        await admin_start(update, context)
        return

    # --- Иначе считаем, что это ответ клиенту (обычный Reply-флоу) ---
    await admin_reply(update, context)


async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("admin_cancel_ask:"):
        slot_key = data.split(":", 1)[1]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, отменить", callback_data=f"admin_cancel_yes:{slot_key}"),
            InlineKeyboardButton("Нет", callback_data="admin_cancel_no"),
        ]])
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return

    if data == "admin_cancel_no":
        await query.answer("Отменено (запись сохранена)")
        return

    if data.startswith("admin_cancel_yes:"):
        slot_key = data.split(":", 1)[1]
        entry = delete_booking(slot_key)

        client_job_queue = context.bot_data.get("client_job_queue")
        _cancel_jobs(client_job_queue, slot_key)

        await query.edit_message_text(query.message.text + "\n\n❌ ЗАПИСЬ ОТМЕНЕНА АДМИНОМ")

        if entry and entry.get("client_chat_id", 0) > 0:
            client_bot = context.bot_data["client_bot"]
            date_part, time_part = slot_key.split("_", 1)
            try:
                await client_bot.send_message(
                    chat_id=entry["client_chat_id"],
                    text=f"К сожалению, ваша запись на {date_part} {time_part} отменена администратором. "
                         f"Пожалуйста, свяжитесь с нами или запишитесь на другое время.",
                )
            except Exception:
                log.exception("Не удалось уведомить клиента об отмене админом")
        return


async def admin_noshow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🚫 Клиент не пришёл» (admin_noshow_ask/yes/no). Запись убирается из активных,
    не попадает в статистику и не получает бонус, но её данные сохраняются в noshows.json."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("admin_noshow_ask:"):
        slot_key = data.split(":", 1)[1]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, не пришёл", callback_data=f"admin_noshow_yes:{slot_key}"),
            InlineKeyboardButton("Нет", callback_data="admin_noshow_no"),
        ]])
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return

    if data == "admin_noshow_no":
        await query.answer("Отменено (запись сохранена)")
        return

    if data.startswith("admin_noshow_yes:"):
        slot_key = data.split(":", 1)[1]
        client_job_queue = context.bot_data.get("client_job_queue")
        entry = mark_noshow(slot_key, client_job_queue)

        if not entry:
            await query.edit_message_text(query.message.text + "\n\n⚠️ Запись уже не активна.")
            return

        await query.edit_message_text(
            query.message.text
            + "\n\n🚫 ОТМЕЧЕНО: КЛИЕНТ НЕ ПРИШЁЛ\n"
              "(в статистику и бонусы не засчитано, данные клиента сохранены в истории)"
        )

        if entry.get("client_chat_id", 0) > 0:
            client_bot = context.bot_data["client_bot"]
            date_part, time_part = slot_key.split("_", 1)
            try:
                await client_bot.send_message(
                    chat_id=entry["client_chat_id"],
                    text=f"Жаль, что не получилось встретиться {date_part} в {time_part} 🌸 "
                         f"Будем рады видеть вас в другой раз — запишитесь через /book.",
                )
            except Exception:
                log.exception("Не удалось уведомить клиента о неявке")
        return


async def admin_cancel_all_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «❌ Отменить все записи» — отменяет только ПРЕДСТОЯЩИЕ записи.
    Прошедшие записи и вся история/статистика клиентов не трогаются."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return

    now = datetime.now()
    upcoming_count = sum(1 for k in BOOKINGS if _slot_datetime(k) > now)

    if not upcoming_count:
        await update.message.reply_text(
            "Предстоящих записей нет — отменять нечего.", reply_markup=_admin_menu_keyboard()
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, отменить все предстоящие", callback_data="cancelall_yes"),
        InlineKeyboardButton("Нет", callback_data="cancelall_no"),
    ]])
    await update.message.reply_text(
        f"⚠️ Вы точно хотите отменить ВСЕ предстоящие записи?\n\n"
        f"Предстоящих записей: {upcoming_count}\n"
        f"Прошедшие записи и данные/статистика клиентов останутся нетронутыми.\n"
        f"Клиентам с Telegram придёт уведомление об отмене. Это действие нельзя отменить.",
        reply_markup=keyboard,
    )


async def admin_cancel_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает подтверждение/отказ массовой отмены предстоящих записей (cancelall_*)."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    if query.data == "cancelall_no":
        await query.answer("Отменено (записи сохранены)")
        await query.edit_message_text("Действие отменено, записи не тронуты ✅")
        return

    if query.data == "cancelall_yes":
        await query.answer()

        now = datetime.now()
        # Важно: трогаем только предстоящие записи. Прошедшие остаются в BOOKINGS —
        # они нужны для статистики, статуса клиента (VIP/постоянный) и истории визитов.
        entries = [(k, v) for k, v in BOOKINGS.items() if _slot_datetime(k) > now]
        client_job_queue = context.bot_data.get("client_job_queue")
        client_bot = context.bot_data["client_bot"]

        notified = 0
        for slot_key, entry in entries:
            _cancel_jobs(client_job_queue, slot_key)
            if entry.get("client_chat_id", 0) > 0:
                date_part, time_part = slot_key.split("_", 1)
                try:
                    await client_bot.send_message(
                        chat_id=entry["client_chat_id"],
                        text=f"К сожалению, ваша запись на {date_part} {time_part} отменена администратором. "
                             f"Пожалуйста, свяжитесь с нами или запишитесь на другое время.",
                    )
                    notified += 1
                except Exception:
                    log.exception("Не удалось уведомить клиента о массовой отмене")

        for slot_key, _ in entries:
            BOOKINGS.pop(slot_key, None)
        _save_json(BOOKINGS_FILE, BOOKINGS)

        await query.edit_message_text(
            f"❌ Все предстоящие записи отменены ({len(entries)} шт.)\n"
            f"Уведомлено клиентов: {notified}\n"
            f"Прошедшие записи и история клиентов сохранены."
        )
        return


async def morning_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_CHAT_IDS:
        return
    today_iso = datetime.now().date().isoformat()
    entries = [(k, v) for k, v in BOOKINGS.items() if k.startswith(f"{today_iso}_")]
    text = f"🌅 Доброе утро! Записи на сегодня ({today_iso}):\n\n" + _format_bookings_table(entries)
    for admin_chat_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=admin_chat_id, text=text, parse_mode="Markdown")
        except Exception:
            log.exception("Не удалось отправить утреннюю сводку админу %s", admin_chat_id)


# ---------------------------------------------------------------------------
# Запуск обоих ботов в одном процессе
# ---------------------------------------------------------------------------

async def _run_app(app: Application) -> None:
    await app.initialize()
    await app.start()
    await app.updater.start_polling()


async def _stop_app(app: Application) -> None:
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


async def main() -> None:
    if not CLIENT_BOT_TOKEN or not ADMIN_BOT_TOKEN:
        raise SystemExit(
            "Задайте CLIENT_BOT_TOKEN и ADMIN_BOT_TOKEN (переменные окружения или .env). "
            "См. .env.example"
        )
    if not ADMIN_CHAT_ID:
        log.warning(
            "ADMIN_CHAT_ID не задан. Напишите /start админскому боту, чтобы узнать chat_id, "
            "затем впишите его в .env и перезапустите."
        )

    client_app = ApplicationBuilder().token(CLIENT_BOT_TOKEN).build()
    admin_app = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()

    # каждому боту даём доступ к экземпляру другого бота и к job_queue клиента (для отмены брони админом)
    client_app.bot_data["admin_bot"] = admin_app.bot
    admin_app.bot_data["client_bot"] = client_app.bot
    admin_app.bot_data["client_job_queue"] = client_app.job_queue

    # --- клиентский бот ---
    client_app.add_handler(CommandHandler("start", client_start))
    client_app.add_handler(CommandHandler("menu", client_menu_command))
    client_app.add_handler(CommandHandler("book", client_book_command))

    client_app.add_handler(CallbackQueryHandler(client_booking_callback, pattern=r"^(book:|svc:|date:|time:)"))
    client_app.add_handler(CallbackQueryHandler(client_my_bookings_callback, pattern=r"^mybookings:"))
    client_app.add_handler(CallbackQueryHandler(client_cancel_callback, pattern=r"^cancel_(ask|yes):"))
    client_app.add_handler(CallbackQueryHandler(client_reschedule_callback, pattern=r"^resched_(ask|date|time):"))
    client_app.add_handler(CallbackQueryHandler(client_review_callback, pattern=r"^review:"))

    client_app.add_handler(MessageHandler(filters.CONTACT, client_contact_received))
    client_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.CONTACT, client_incoming))

    # --- админский бот ---
    admin_app.add_handler(CommandHandler("start", admin_start))
    admin_app.add_handler(CommandHandler("broadcast", admin_broadcast))
    admin_app.add_handler(CommandHandler("slots", admin_slots))
    admin_app.add_handler(CommandHandler("today", admin_today))
    admin_app.add_handler(CommandHandler("calendar", admin_calendar))
    admin_app.add_handler(CommandHandler("stats", admin_stats))
    admin_app.add_handler(CommandHandler("export", admin_export))
    admin_app.add_handler(CommandHandler("addbooking", admin_addbooking_start))
    admin_app.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern=r"^admin_cancel_"))
    admin_app.add_handler(CallbackQueryHandler(admin_noshow_callback, pattern=r"^admin_noshow_"))
    admin_app.add_handler(CallbackQueryHandler(admin_cancel_all_callback, pattern=r"^cancelall_"))
    admin_app.add_handler(CallbackQueryHandler(admin_calendar_callback, pattern=r"^cal_"))
    admin_app.add_handler(CallbackQueryHandler(admin_addbooking_callback, pattern=r"^aadd_"))
    admin_app.add_handler(CallbackQueryHandler(admin_quick_reply_callback, pattern=r"^qr:"))
    admin_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, admin_dispatch))

    if admin_app.job_queue is not None:
        admin_app.job_queue.run_daily(
            morning_report, time=time_cls(hour=MORNING_REPORT_HOUR, minute=MORNING_REPORT_MINUTE)
        )
    else:
        log.warning("JobQueue недоступен у admin_app — утренняя сводка не будет отправляться. "
                    "Установите: pip install \"python-telegram-bot[job-queue]\"")

    await _run_app(client_app)
    await _run_app(admin_app)
    log.info("Оба бота запущены. Для остановки нажмите Ctrl+C.")

    stop_event = asyncio.Event()

    def _handle_stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            pass  # Windows

    await stop_event.wait()

    log.info("Останавливаю ботов...")
    await _stop_app(client_app)
    await _stop_app(admin_app)


if __name__ == "__main__":
    asyncio.run(main())