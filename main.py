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
CLIENTS_FILE = BASE_DIR / "clients.json"     # реестр всех, кто хоть раз зашёл в клиентский бот
BOOKINGS_FILE = BASE_DIR / "bookings.json"
REVIEWS_FILE = BASE_DIR / "reviews.json"
BLOCKED_FILE = BASE_DIR / "blocked_slots.json"
BONUSES_FILE = BASE_DIR / "bonuses.json"
BIRTHDAYS_FILE = BASE_DIR / "birthdays.json"
NOSHOWS_FILE = BASE_DIR / "noshows.json"
SERVICES_FILE = BASE_DIR / "services.json"      # услуги теперь редактируются из админ-панели
PROMOTIONS_FILE = BASE_DIR / "promotions.json"  # акции, редактируются из админ-панели

MEDIA_DIR = BASE_DIR / "media"
PORTFOLIO_DIR = MEDIA_DIR / "portfolio"     # положите сюда фото/видео примеров работ
PRICE_LIST_PDF = MEDIA_DIR / "price_list.pdf"  # если есть готовый PDF с ценами — положите сюда

# ---------------------------------------------------------------------------
# Настройки — поправьте под реальные данные Muna Beauty
# ---------------------------------------------------------------------------

# Услуги (код, подпись с эмодзи, цена одной строкой, числовое значение цены в сомони)
# хранятся в services.json и управляются из админ-панели (кнопка «🛠 Услуги»):
# можно добавлять новые услуги и менять цены прямо в боте, без правки кода.
# Значения ниже — только «стартовый набор» на самый первый запуск, когда
# services.json ещё не существует.
_DEFAULT_SERVICES = [
    {"code": "makeup", "label": "💄 Макияж", "price": "150 смн", "price_value": 150},
    {"code": "hair", "label": "💇‍♀️ Причёска", "price": "80 смн", "price_value": 120},
    {"code": "lashes", "label": "👁️ Наращивание ресниц", "price": "200 смн", "price_value": 200},
    {"code": "photo", "label": "📸 Фотосъёмка", "price": "300 смн", "price_value": 300},
    {"code": "video", "label": "🎥 Видеосъёмка", "price": "500 смн", "price_value": 500},
]


def _load_services() -> list[dict]:
    if SERVICES_FILE.exists():
        try:
            data = json.loads(SERVICES_FILE.read_text(encoding="utf-8"))
            if data:
                return data
        except json.JSONDecodeError:
            log.warning("services.json повреждён, использую значения по умолчанию")
    SERVICES_FILE.write_text(
        json.dumps(_DEFAULT_SERVICES, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return [dict(s) for s in _DEFAULT_SERVICES]


def _save_services() -> None:
    SERVICES_FILE.write_text(json.dumps(_SERVICES_DATA, ensure_ascii=False, indent=2), encoding="utf-8")


def _rebuild_service_dicts() -> None:
    """Пересобирает удобные для остального кода структуры (SERVICES/SERVICE_LABELS/...)
    из «сырых» данных _SERVICES_DATA. Вызывается после любого изменения услуг."""
    global SERVICES, SERVICE_LABELS, SERVICE_PRICES, SERVICE_PRICE_VALUES
    SERVICES = [(s["code"], s["label"], s["price"]) for s in _SERVICES_DATA]
    SERVICE_LABELS = {s["code"]: s["label"] for s in _SERVICES_DATA}
    SERVICE_PRICES = {s["code"]: s["price"] for s in _SERVICES_DATA}
    SERVICE_PRICE_VALUES = {s["code"]: s["price_value"] for s in _SERVICES_DATA}


_SERVICES_DATA: list[dict] = _load_services()
_rebuild_service_dicts()


def service_code_exists(code: str) -> bool:
    return any(s["code"] == code for s in _SERVICES_DATA)


def add_service(code: str, label: str, price: str, price_value: int) -> None:
    _SERVICES_DATA.append({"code": code, "label": label, "price": price, "price_value": price_value})
    _save_services()
    _rebuild_service_dicts()


def update_service_price(code: str, price: str, price_value: int) -> bool:
    for s in _SERVICES_DATA:
        if s["code"] == code:
            s["price"] = price
            s["price_value"] = price_value
            _save_services()
            _rebuild_service_dicts()
            return True
    return False


def delete_service(code: str) -> bool:
    global _SERVICES_DATA
    before = len(_SERVICES_DATA)
    _SERVICES_DATA = [s for s in _SERVICES_DATA if s["code"] != code]
    if len(_SERVICES_DATA) != before:
        _save_services()
        _rebuild_service_dicts()
        return True
    return False


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
DAYS_AHEAD = 30           # на сколько дней вперёд показывать даты (календарь на месяц)

WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

REMINDER_HOURS_BEFORE = 2         # за сколько часов напомнить клиенту
REVIEW_DELAY_AFTER_MIN = SLOT_MINUTES  # через сколько минут после начала слота спросить отзыв

MORNING_REPORT_HOUR = 8           # во сколько присылать админу список записей на день
MORNING_REPORT_MINUTE = 0

FLOOD_LIMIT_MESSAGES = 5          # не больше стольких сообщений...
FLOOD_LIMIT_WINDOW_SECONDS = 60   # ...за этот период (секунды)

ADDRESSES = [

    {"title": "📍 Филиал 1", "text": "Согдийская область, г. Пенджикент, проспект Рудаки", "lat": 39.494065, "lon": 67.601944},
    {"title": "📍 Филиал 2", "text": "г. Пенджикент, ул. Даврон Бобораджабов 101", "lat": 39.493365, "lon":  67.605858},
]

# Ссылки на соцсети — поправьте на реальные. Если ссылки нет — просто удалите строку из списка.
SOCIAL_LINKS = [
    ("📷 Instagram", "https://www.instagram.com/munzifa_shodieva "),
    ("✈️ Telegram-канал", "https://t.me/munabeauty98"),
]

# Кнопка «🖼 Портфолио» ведёт клиента в этот Telegram-канал (там примеры работ, актуальные фото и т.п.)
PORTFOLIO_CHANNEL_URL = "https://t.me/muna_beauty"

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
ADMIN_MENU_CLIENTS = "👥 Клиенты бота"
ADMIN_MENU_EXPORT = "📤 Экспорт CSV"
ADMIN_MENU_BROADCAST = "📢 Рассылка"
ADMIN_MENU_CANCEL_ALL = "❌ Отменить предстоящие записи"
ADMIN_MENU_REVIEWS = "📝 Отзывы"
ADMIN_MENU_SERVICES = "🛠 Услуги"
ADMIN_MENU_PROMOTIONS = "🎁 Акции"
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
CLIENTS = _load_json(CLIENTS_FILE)     # { "<chat_id>": {"name","username","first_seen","last_seen"} } — все, кто заходил в клиентский бот
BOOKINGS = _load_json(BOOKINGS_FILE)    # { "2026-07-26_14:00": {...} }
REVIEWS = _load_json(REVIEWS_FILE)      # { "2026-07-26_14:00": {"score": int, "client_chat_id": int} }
BLOCKED = _load_json(BLOCKED_FILE)      # { "2026-07-26_14:00": true }  — слоты, закрытые админом вручную
BONUSES = _load_json(BONUSES_FILE)      # { "<client_chat_id>": 45 }  — накопленные бонусные баллы
BIRTHDAYS = _load_json(BIRTHDAYS_FILE)  # { "<client_chat_id>": "15.03" или "15.03.1995" }
NOSHOWS = _load_json(NOSHOWS_FILE)      # { "2026-07-26_14:00#169..." : {...} } — история неявок (не влияет на статистику/бонусы)
PROMOTIONS = _load_json(PROMOTIONS_FILE)  # { "<id>": {"text": str, "photo_file_id": str|None, "date": iso} }

# антифлуд: client_id -> deque(таймстампы последних сообщений)
_flood_tracker: dict[int, deque] = defaultdict(deque)


def add_promotion(text: str, photo_file_id: str | None = None,
                   discount_percent: int = 0, service_codes=None,
                   valid_until: str | None = None) -> str:
    promo_id = str(int(datetime.now().timestamp() * 1000))
    PROMOTIONS[promo_id] = {
        "text": text,
        "photo_file_id": photo_file_id,
        "date": datetime.now().isoformat(timespec="seconds"),
        "discount_percent": discount_percent or 0,
        "service_codes": service_codes or [],   # пусто = скидка действует на все услуги
        "valid_until": valid_until,              # "ГГГГ-ММ-ДД" включительно, либо None = бессрочно
    }
    _save_json(PROMOTIONS_FILE, PROMOTIONS)
    return promo_id


def delete_promotion(promo_id: str) -> bool:
    if PROMOTIONS.pop(promo_id, None) is not None:
        _save_json(PROMOTIONS_FILE, PROMOTIONS)
        return True
    return False


def _promotions_sorted():
    return sorted(PROMOTIONS.items(), key=lambda kv: kv[1].get("date", ""), reverse=True)


def _promotion_is_active(promo: dict, on_date: date_cls | None = None) -> bool:
    """Акция активна, если сегодня (или указанная дата) не позже даты valid_until.
    Если valid_until не задан — акция считается бессрочной."""
    valid_until = promo.get("valid_until")
    if not valid_until:
        return True
    check_date = on_date or datetime.now().date()
    try:
        return check_date <= date_cls.fromisoformat(valid_until)
    except ValueError:
        return True


def get_active_discount(service_code: str, on_date: date_cls | None = None) -> tuple[int, str | None]:
    """
    Возвращает (процент_скидки, valid_until) — самую большую активную скидку на
    указанную услугу среди всех акций. Скидка распространяется на услугу, если
    у акции список service_codes пуст (значит на все услуги) либо код услуги
    в нём указан явно. Если активных скидок нет — возвращает (0, None).
    """
    best_percent = 0
    best_until = None
    for _, promo in PROMOTIONS.items():
        percent = promo.get("discount_percent", 0)
        if not percent:
            continue
        codes = promo.get("service_codes") or []
        if codes and service_code not in codes:
            continue
        if not _promotion_is_active(promo, on_date):
            continue
        if percent > best_percent:
            best_percent = percent
            best_until = promo.get("valid_until")
    return best_percent, best_until


def get_effective_price(service_code: str, on_date: date_cls | None = None) -> tuple[str, int, int]:
    """
    Возвращает (текст_цены_для_клиента, итоговая_цена_в_сомони, скидка_в_процентах)
    для услуги с учётом активных на сегодня акций. Если скидки нет — возвращает
    исходную цену и SERVICE_PRICE_VALUES без изменений.
    """
    base_value = SERVICE_PRICE_VALUES.get(service_code, 0)
    base_text = SERVICE_PRICES.get(service_code, "")
    percent, _ = get_active_discount(service_code, on_date)
    if not percent:
        return base_text, base_value, 0
    discounted_value = round(base_value * (100 - percent) / 100)
    price_text = f"~{base_text}~ {discounted_value} смн (-{percent}%)"
    return price_text, discounted_value, percent


def _link_key(admin_chat_id, admin_message_id: int) -> str:
    # Составной ключ: одно и то же message_id может существовать параллельно
    # в разных чатах разных админов, поэтому чат обязательно учитываем.
    return f"{admin_chat_id}:{admin_message_id}"


def remember(admin_chat_id, admin_message_id: int, client_chat_id: int, name: str) -> None:
    LINKS[_link_key(admin_chat_id, admin_message_id)] = {"client_chat_id": client_chat_id, "name": name}
    _save_json(DATA_FILE, LINKS)


def recall(admin_chat_id, admin_message_id: int):
    return LINKS.get(_link_key(admin_chat_id, admin_message_id))


def register_client(chat_id: int, name: str, username: str | None) -> bool:
    """Запоминает клиента, зашедшего в клиентский бот (для счётчика «сколько клиентов зашли в бот»).
    Возвращает True, если это НОВЫЙ клиент (первый визит)."""
    key = str(chat_id)
    now_iso = datetime.now().isoformat(timespec="seconds")
    is_new = key not in CLIENTS
    if is_new:
        CLIENTS[key] = {
            "name": name,
            "username": username or "",
            "first_seen": now_iso,
            "last_seen": now_iso,
        }
    else:
        CLIENTS[key]["last_seen"] = now_iso
        if name:
            CLIENTS[key]["name"] = name
        if username:
            CLIENTS[key]["username"] = username
    _save_json(CLIENTS_FILE, CLIENTS)
    return is_new


def save_booking(slot_key: str, service_code: str, client_chat_id: int, name: str, phone: str,
                  price_value: int | None = None) -> None:
    BOOKINGS[slot_key] = {
        "service": service_code,
        "client_chat_id": client_chat_id,
        "name": name,
        "phone": phone,
        # цена, зафиксированная в момент записи (с учётом скидки, если она действовала) —
        # используется для статистики/дохода, чтобы будущие изменения цены или акции
        # не искажали задним числом уже прошедшие записи.
        "price_value": price_value if price_value is not None else SERVICE_PRICE_VALUES.get(service_code, 0),
        # Отмечается кнопкой "✅ Пришёл" в админ-боте. Пока не отмечено — запись
        # не попадает в отчёты статистики (день/неделя/месяц/год).
        "attended": False,
    }
    _save_json(BOOKINGS_FILE, BOOKINGS)


def mark_attended(slot_key: str) -> dict | None:
    """Отмечает, что клиент пришёл на визит. После этого запись учитывается
    в отчётах статистики (день/неделя/месяц/год)."""
    entry = BOOKINGS.get(slot_key)
    if entry is None:
        return None
    entry["attended"] = True
    _save_json(BOOKINGS_FILE, BOOKINGS)
    return entry


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
MENU_REVIEWS = "⭐ Отзывы"
MENU_PROMOTIONS = "🎁 Акции"


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура снизу экрана (а не кнопки под сообщением)."""
    return ReplyKeyboardMarkup(
        [
            [MENU_BOOK],
            [MENU_MY_BOOKINGS],
            [MENU_PORTFOLIO, MENU_PRICE],
            [MENU_PROMOTIONS, MENU_REVIEWS],
            [MENU_ADDRESS, MENU_SOCIAL],
        ],
        resize_keyboard=True,   # кнопки компактнее, не занимают весь экран
        is_persistent=True,     # клавиатура не пропадает после нажатия
    )


async def client_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_client(update.effective_chat.id, user.full_name if user else "", user.username if user else None)
    context.user_data["_menu_sent"] = True
    await update.message.reply_text(CLIENT_WELCOME, reply_markup=_main_menu_keyboard())


async def client_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Выберите действие:", reply_markup=_main_menu_keyboard())


async def client_book_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Выберите услугу:", reply_markup=_services_keyboard())


def _services_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for code, label, _ in SERVICES:
        price_text, _, _ = get_effective_price(code)
        rows.append([InlineKeyboardButton(f"{label} — {price_text}", callback_data=f"svc:{code}")])
    return InlineKeyboardMarkup(rows)


def _dates_keyboard(service_code: str) -> InlineKeyboardMarkup:
    rows = []
    today = datetime.now().date()
    row = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_RU[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"date:{service_code}:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
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
        register_client(msg.chat_id, user.full_name if user else "", user.username if user else None)
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

    pending_review = context.user_data.get("pending_free_review")
    if pending_review:
        raw_text = (msg.text or "").strip()
        if raw_text.lower() == "отмена":
            context.user_data.pop("pending_free_review", None)
            await msg.reply_text("Отзыв отменён.", reply_markup=_main_menu_keyboard())
            return
        comment = None if raw_text == "-" else raw_text
        review_key = f"free_{int(datetime.now().timestamp() * 1000)}_{user.id}"
        REVIEWS[review_key] = {
            "score": pending_review["score"],
            "client_chat_id": msg.chat_id,
            "name": user.full_name,
            "text": comment,
            "date": datetime.now().isoformat(timespec="seconds"),
            "slot_key": None,
        }
        _save_json(REVIEWS_FILE, REVIEWS)
        context.user_data.pop("pending_free_review", None)
        await msg.reply_text("Спасибо за отзыв! 🌸", reply_markup=_main_menu_keyboard())

        if ADMIN_CHAT_IDS:
            admin_bot = context.bot_data["admin_bot"]
            stars = "⭐" * pending_review["score"]
            text_line = f"\n«{comment}»" if comment else ""
            for admin_chat_id in ADMIN_CHAT_IDS:
                try:
                    await admin_bot.send_message(
                        chat_id=admin_chat_id,
                        text=f"⭐ Новый отзыв: {stars} от {user.full_name}{text_line}",
                    )
                except Exception:
                    log.exception("Не удалось переслать отзыв админу %s", admin_chat_id)
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
    if msg.text == MENU_REVIEWS:
        await _send_reviews_menu(context, msg.chat_id)
        return
    if msg.text == MENU_PROMOTIONS:
        await _send_promotions(context, msg.chat_id)
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
    price_text, price_value, discount_percent = get_effective_price(service_code, date_cls.fromisoformat(date_iso))
    save_booking(slot_key, service_code, chat_id, user.full_name, phone, price_value=price_value)

    d = date_cls.fromisoformat(date_iso)
    confirm_text = (
        f"Вы записаны ✅\n\n"
        f"Услуга: {label} ({price_text})\n"
        f"Дата: {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n"
        f"Время: {time_str}\n\n"
        f"Мы напомним вам за {REMINDER_HOURS_BEFORE} ч. до визита."
    )
    await update.message.reply_text(confirm_text, reply_markup=_main_menu_keyboard())

    _schedule_reminder_and_review(context, slot_key, chat_id)

    if ADMIN_CHAT_IDS:
        admin_bot = context.bot_data["admin_bot"]
        status = get_client_status(chat_id)
        discount_line = f"\nСкидка: -{discount_percent}% (по акции)" if discount_percent else ""
        admin_text = (
            f"🆕 Новая запись\n"
            f"Клиент: {user.full_name} (@{user.username or 'нет username'})\n"
            f"Статус клиента: {status}\n"
            f"Телефон: {phone or 'не указан'}\n"
            f"ID клиента: {user.id}\n"
            f"Услуга: {label} ({price_text}){discount_line}\n"
            f"Дата: {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n"
            f"Время: {time_str}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Пришёл", callback_data=f"admin_attend:{slot_key}"),
            InlineKeyboardButton("🚫 Не пришёл", callback_data=f"admin_noshow_ask:{slot_key}"),
        ], [
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
    booking_entry = BOOKINGS.get(slot_key, {})
    REVIEWS[slot_key] = {
        "score": score,
        "client_chat_id": query.message.chat_id,
        "name": booking_entry.get("name", "Клиент"),
        "text": None,
        "date": datetime.now().isoformat(timespec="seconds"),
        "slot_key": slot_key,
    }
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
    """Кнопка «🖼 Портфолио» — отправляет ссылку-кнопку на Telegram-канал с примерами работ."""
    if not PORTFOLIO_CHANNEL_URL:
        await context.bot.send_message(chat_id=chat_id, text="Портфолио скоро будет пополнено 🙌 Загляните позже!")
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✈️ Открыть канал с портфолио", url=PORTFOLIO_CHANNEL_URL)]])
    await context.bot.send_message(
        chat_id=chat_id,
        text="🖼 Все примеры наших работ — в Telegram-канале. Загляните и подпишитесь, чтобы не пропустить новое ✨",
        reply_markup=keyboard,
    )


async def _send_price(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if PRICE_LIST_PDF.exists():
        await context.bot.send_document(chat_id=chat_id, document=PRICE_LIST_PDF.open("rb"))
        return
    lines = ["💰 Прайс-лист:"]
    for code, label, price in SERVICES:
        price_text, _, _ = get_effective_price(code)
        lines.append(f"{label} — {price_text}")
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


async def _send_promotions(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Кнопка «🎁 Акции» — показывает всю информацию по текущим (ещё не истёкшим) акциям."""
    active = [(pid, p) for pid, p in _promotions_sorted() if _promotion_is_active(p)]
    if not active:
        await context.bot.send_message(chat_id=chat_id, text="🎁 Акций пока нет — загляните позже 🌸")
        return
    for promo_id, promo in active:
        text = f"🎁 {promo['text']}"
        percent = promo.get("discount_percent", 0)
        until = promo.get("valid_until")
        if percent:
            codes = promo.get("service_codes") or []
            if codes:
                names = ", ".join(SERVICE_LABELS.get(c, c) for c in codes)
                text += f"\n\nСкидка {percent}% на: {names}"
            else:
                text += f"\n\nСкидка {percent}% на все услуги"
            if until:
                d = date_cls.fromisoformat(until)
                text += f"\nАкция действует до {d.strftime('%d.%m.%Y')} включительно"
        photo_id = promo.get("photo_file_id")
        try:
            if photo_id:
                await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception("Не удалось отправить акцию %s", promo_id)


def _reviews_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Оставить отзыв", callback_data="reviews:leave")],
        [InlineKeyboardButton("📖 Посмотреть отзывы", callback_data="reviews:view")],
    ])


def _reviews_summary_text() -> str:
    if not REVIEWS:
        return "⭐ Отзывов пока нет — станьте первым!"
    scores = [r.get("score", 0) for r in REVIEWS.values()]
    avg = sum(scores) / len(scores)
    return f"⭐ Средняя оценка: {avg:.1f}/5 (отзывов: {len(scores)})"


def _format_reviews_list(limit: int = 10) -> str:
    if not REVIEWS:
        return "Отзывов пока нет."
    items = sorted(REVIEWS.items(), key=lambda kv: kv[1].get("date", ""), reverse=True)[:limit]
    lines = ["📖 Последние отзывы:"]
    for _, r in items:
        stars = "⭐" * r.get("score", 0)
        name = r.get("name", "Клиент")
        text = r.get("text")
        line = f"\n{stars} — {name}"
        if text:
            line += f"\n«{text}»"
        lines.append(line)
    return "\n".join(lines)


def _free_review_score_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(str(n), callback_data=f"freereview:{n}") for n in range(1, 6)
    ]])


async def _send_reviews_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text=_reviews_summary_text() + "\n\nЧто хотите сделать?",
        reply_markup=_reviews_menu_keyboard(),
    )


async def client_reviews_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает меню отзывов клиента: reviews:leave / reviews:view / freereview:N."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "reviews:leave":
        await query.edit_message_text("Поставьте оценку от 1 до 5:", reply_markup=_free_review_score_keyboard())
        return

    if data == "reviews:view":
        await query.edit_message_text(_format_reviews_list(), reply_markup=_reviews_menu_keyboard())
        return

    if data.startswith("freereview:"):
        score = int(data.split(":", 1)[1])
        context.user_data["pending_free_review"] = {"score": score}
        await query.edit_message_text(
            f"Оценка: {'⭐' * score}\n\n"
            f"Напишите комментарий текстом, либо отправьте «-», чтобы оставить отзыв без комментария "
            f"(или «Отмена», чтобы отменить)."
        )
        return


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
    row = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_RU[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"resched_date:{old_slot_key}:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
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
        save_booking(new_slot_key, entry["service"], entry["client_chat_id"], entry["name"], entry["phone"],
                     price_value=entry.get("price_value"))

        # Старые напоминание/запрос отзыва больше не актуальны — отменяем и ставим новые.
        _cancel_jobs(context.application.job_queue, old_slot_key)
        _schedule_reminder_and_review(context, new_slot_key, entry["client_chat_id"])

        label = SERVICE_LABELS.get(entry["service"], entry["service"])
        price_text, _, _ = get_effective_price(entry["service"], date_cls.fromisoformat(date_iso))
        d = date_cls.fromisoformat(date_iso)
        await query.edit_message_text(
            f"Запись перенесена ✅\n\n"
            f"Услуга: {label} ({price_text})\n"
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
                            f"Услуга: {label} ({price_text})\n"
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
        price_text, _, _ = get_effective_price(service_code)
        await query.edit_message_text(
            f"Услуга: {label} ({price_text})\nВыберите дату:", reply_markup=_dates_keyboard(service_code)
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
            [ADMIN_MENU_ADD, ADMIN_MENU_CLIENTS],
            [ADMIN_MENU_EXPORT, ADMIN_MENU_BROADCAST],
            [ADMIN_MENU_CANCEL_ALL],
            [ADMIN_MENU_REVIEWS],
            [ADMIN_MENU_SERVICES, ADMIN_MENU_PROMOTIONS],
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
        "/stats — статистика (день/неделя/месяц/год) по визитам, отмеченным «Пришёл»\n"
        "/clients — сколько всего клиентов зашли в бот\n"
        "/reviews — список отзывов клиентов\n"
        "/services — добавить услугу или изменить цену существующей\n"
        "/promotions — добавить или удалить акцию (кнопка «🎁 Акции» у клиента)\n"
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
    rows.append([
        InlineKeyboardButton("📆 Выбрать день из месяца", callback_data=f"cal_pickmonth:{date_iso}"),
    ])
    return InlineKeyboardMarkup(rows)


def _month_picker_keyboard(anchor_date_iso: str) -> InlineKeyboardMarkup:
    """
    Компактная сетка дней на месяц вперёд от anchor_date_iso (по 7 дней в ряд, как
    обычный календарь), чтобы быстро перейти к любому дню в пределах месяца
    расписания, а не листать по одному дню.
    """
    anchor = date_cls.fromisoformat(anchor_date_iso)
    today = datetime.now().date()
    rows = []
    row = []
    for i in range(30):
        d = today + timedelta(days=i)
        mark = "•" if d == anchor else ""
        row.append(InlineKeyboardButton(f"{d.day}{mark}", callback_data=f"cal_date:{d.isoformat()}"))
        if len(row) == 7:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад к дню", callback_data=f"cal_date:{anchor_date_iso}")])
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
    """Обрабатывает нажатия в интерактивном календаре: cal_date / cal_toggle / cal_info / cal_pickmonth."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    data = query.data

    if data.startswith("cal_pickmonth:"):
        anchor_date_iso = data.split(":", 1)[1]
        await query.answer()
        await query.edit_message_text(
            "📆 Выберите день (доступно на месяц вперёд):",
            reply_markup=_month_picker_keyboard(anchor_date_iso),
        )
        return

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
        attended = entry.get("attended", False)
        keyboard_rows = []
        if not attended:
            keyboard_rows.append([InlineKeyboardButton("✅ Клиент пришёл", callback_data=f"admin_attend:{slot_key}")])
        keyboard_rows.append([InlineKeyboardButton("🚫 Клиент не пришёл", callback_data=f"admin_noshow_ask:{slot_key}")])
        keyboard_rows.append([InlineKeyboardButton("❌ Отменить запись", callback_data=f"admin_cancel_ask:{slot_key}")])
        keyboard_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"cal_date:{date_iso}")])
        keyboard = InlineKeyboardMarkup(keyboard_rows)
        attended_line = "✅ Пришёл (учтено в статистике)" if attended else "⏳ Ещё не отмечен как пришедший"
        await query.edit_message_text(
            f"🔴 {date_iso} {time_part}{manual_note}\n\n"
            f"Клиент: {entry.get('name', '')}\n"
            f"Услуга: {label}\n"
            f"Тел.: {entry.get('phone') or 'не указан'}\n"
            f"Статус: {status}\n"
            f"Визит: {attended_line}",
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
    row = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_RU[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"aadd_date:{service_code}:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
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
    _, price_value, _ = get_effective_price(service_code, date_cls.fromisoformat(date_iso))
    save_booking(slot_key, service_code, fake_chat_id, name, phone, price_value=price_value)
    BOOKINGS[slot_key]["manual"] = True
    _save_json(BOOKINGS_FILE, BOOKINGS)

    label = SERVICE_LABELS.get(service_code, service_code)
    price_text, _, _ = get_effective_price(service_code, date_cls.fromisoformat(date_iso))
    d = date_cls.fromisoformat(date_iso)
    await update.message.reply_text(
        f"✅ Запись добавлена вручную\n\n"
        f"Клиент: {name}\n"
        f"Телефон: {phone or 'не указан'}\n"
        f"Услуга: {label} ({price_text})\n"
        f"Дата: {d.strftime('%d.%m.%Y')} ({WEEKDAY_RU[d.weekday()]})\n"
        f"Время: {time_str}\n\n"
        f"(запись без Telegram — напоминание и запрос отзыва клиенту не отправляются, "
        f"но она учтена в статистике, календаре и экспорте CSV)",
        reply_markup=_admin_menu_keyboard(),
    )


# --- Статистика (отчёты День / Неделя / Месяц / Год) -------------------------------
#
# Отчёты считаются только по записям, отмеченным кнопкой "✅ Пришёл" (поле
# entry["attended"] == True) — то есть по реально состоявшимся визитам, а не по
# всем записям подряд. Пока визит не отмечен, он в отчёты не попадает.

def _period_bounds(period: str, today: date_cls) -> tuple[date_cls, date_cls]:
    if period == "day":
        return today, today
    if period == "week":
        start = today - timedelta(days=today.weekday())  # понедельник этой недели
        end = start + timedelta(days=6)
        return start, end
    if period == "month":
        start = today.replace(day=1)
        if start.month == 12:
            next_month_start = start.replace(year=start.year + 1, month=1)
        else:
            next_month_start = start.replace(month=start.month + 1)
        end = next_month_start - timedelta(days=1)
        return start, end
    if period == "year":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    raise ValueError(f"неизвестный период: {period}")


def _attended_entries_in_range(start: date_cls, end: date_cls) -> list[tuple[str, dict, date_cls]]:
    result = []
    for slot_key, entry in BOOKINGS.items():
        if not entry.get("attended"):
            continue
        d = _slot_datetime(slot_key).date()
        if start <= d <= end:
            result.append((slot_key, entry, d))
    return result


def _summarize(entries: list[tuple[str, dict, date_cls]]) -> tuple[int, int, str, Counter]:
    """Возвращает (кол-во клиентов, сумма, текст услуг «Услуга × N, ...», Counter услуг)."""
    count = len(entries)
    revenue = sum(v.get("price_value", 0) for _, v, _ in entries)
    service_counter = Counter(v["service"] for _, v, _ in entries)
    services_text = ", ".join(
        f"{SERVICE_LABELS.get(code, code)} × {n}" for code, n in service_counter.most_common()
    ) or "—"
    return count, revenue, services_text, service_counter


def _stats_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 День", callback_data="stats_period:day"),
            InlineKeyboardButton("🗓 Неделя", callback_data="stats_period:week"),
        ],
        [
            InlineKeyboardButton("📆 Месяц", callback_data="stats_period:month"),
            InlineKeyboardButton("📈 Год", callback_data="stats_period:year"),
        ],
    ])


def _build_stats_report(period: str) -> str:
    today = datetime.now().date()
    start, end = _period_bounds(period, today)
    entries = _attended_entries_in_range(start, end)

    if period == "day":
        count, revenue, services_text, _ = _summarize(entries)
        return (
            f"📊 Отчёт за день — {today.strftime('%d.%m.%Y')} ({WEEKDAY_RU[today.weekday()]})\n\n"
            f"Клиентов: {count}\n"
            f"Услуги: {services_text}\n"
            f"Итого сумма: {revenue} смн"
        )

    if period == "week":
        lines = [f"📊 Отчёт за неделю — {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}", ""]
        by_day: dict[date_cls, list] = defaultdict(list)
        for slot_key, v, d in entries:
            by_day[d].append((slot_key, v, d))
        best_day, best_count = None, -1
        for i in range(7):
            d = start + timedelta(days=i)
            day_entries = by_day.get(d, [])
            count, revenue, services_text, _ = _summarize(day_entries)
            if count > best_count:
                best_count, best_day = count, d
            marker = " 👑" if d == today else ""
            lines.append(
                f"{WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m')}{marker}: "
                f"{count} клиент(ов) — {services_text} — {revenue} смн"
            )
        total_count, total_revenue, _, service_counter = _summarize(entries)
        top = service_counter.most_common(1)
        top_label = SERVICE_LABELS.get(top[0][0], top[0][0]) if top else "—"
        lines.append("")
        lines.append(f"Итого за неделю: {total_count} клиент(ов), {total_revenue} смн")
        lines.append(f"Популярная услуга: {top_label}")
        if best_count > 0:
            lines.append(f"Больше всего клиентов: {WEEKDAY_RU[best_day.weekday()]} {best_day.strftime('%d.%m')} ({best_count})")
        return "\n".join(lines)

    if period == "month":
        lines = [f"📊 Отчёт за месяц — {MONTH_RU[start.month]} {start.year}", ""]
        by_day_count = Counter(d for _, _, d in entries)
        by_week: dict[int, list] = defaultdict(list)
        for slot_key, v, d in entries:
            by_week[d.isocalendar()[1]].append((slot_key, v, d))
        for week_num in sorted(by_week):
            week_entries = by_week[week_num]
            count, revenue, services_text, _ = _summarize(week_entries)
            dates = sorted(d for _, _, d in week_entries)
            lines.append(
                f"Неделя {dates[0].strftime('%d.%m')}–{dates[-1].strftime('%d.%m')}: "
                f"{count} клиент(ов) — {services_text} — {revenue} смн"
            )
        total_count, total_revenue, _, service_counter = _summarize(entries)
        top = service_counter.most_common(1)
        top_label = SERVICE_LABELS.get(top[0][0], top[0][0]) if top else "—"
        lines.append("")
        lines.append(f"Итого за месяц: {total_count} клиент(ов), {total_revenue} смн")
        lines.append(f"Популярная услуга: {top_label}")
        if by_day_count:
            best_day, best_count = by_day_count.most_common(1)[0]
            lines.append(f"Больше всего клиентов за день: {best_day.strftime('%d.%m')} ({best_count})")
        return "\n".join(lines)

    if period == "year":
        lines = [f"📊 Отчёт за год — {today.year}", ""]
        by_month: dict[int, list] = defaultdict(list)
        for slot_key, v, d in entries:
            by_month[d.month].append((slot_key, v, d))
        best_month, best_count = None, -1
        for m in range(1, 13):
            month_entries = by_month.get(m, [])
            if not month_entries:
                continue
            count, revenue, services_text, _ = _summarize(month_entries)
            if count > best_count:
                best_count, best_month = count, m
            lines.append(f"{MONTH_RU[m]}: {count} клиент(ов) — {services_text} — {revenue} смн")
        total_count, total_revenue, _, service_counter = _summarize(entries)
        top = service_counter.most_common(1)
        top_label = SERVICE_LABELS.get(top[0][0], top[0][0]) if top else "—"
        lines.append("")
        lines.append(f"Итого за год: {total_count} клиент(ов), {total_revenue} смн")
        lines.append(f"Популярная услуга: {top_label}")
        if best_month:
            lines.append(f"Больше всего клиентов за месяц: {MONTH_RU[best_month]} ({best_count})")
        return "\n".join(lines)

    return "Неизвестный период."


async def admin_clients_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clients и кнопка «👥 Клиенты бота» — сколько всего уникальных клиентов зашли в клиентский бот."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return

    total = len(CLIENTS)
    today_iso = datetime.now().date().isoformat()
    week_start = (datetime.now().date() - timedelta(days=datetime.now().date().weekday())).isoformat()
    month_prefix = datetime.now().strftime("%Y-%m")

    new_today = sum(1 for c in CLIENTS.values() if c.get("first_seen", "").startswith(today_iso))
    new_week = sum(1 for c in CLIENTS.values() if c.get("first_seen", "") >= week_start)
    new_month = sum(1 for c in CLIENTS.values() if c.get("first_seen", "").startswith(month_prefix))

    booked_ids = {v.get("client_chat_id") for v in BOOKINGS.values() if v.get("client_chat_id", 0) > 0}
    booked_count = len(booked_ids)

    text = (
        f"👥 Клиенты, зашедшие в бот\n\n"
        f"Всего уникальных клиентов: {total}\n"
        f"Из них хотя бы раз записались: {booked_count}\n"
        f"Только смотрели, не записались: {max(total - booked_count, 0)}\n\n"
        f"Новых сегодня: {new_today}\n"
        f"Новых за эту неделю: {new_week}\n"
        f"Новых за этот месяц: {new_month}"
    )
    await update.message.reply_text(text, reply_markup=_admin_menu_keyboard())



async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats и кнопка «📈 Статистика» — меню выбора отчёта (день/неделя/месяц/год)."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    await update.message.reply_text(
        "📈 Выберите период отчёта:\n\n"
        "(учитываются только визиты, отмеченные кнопкой «✅ Пришёл»)",
        reply_markup=_stats_menu_keyboard(),
    )


async def admin_stats_period_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатие кнопок День/Неделя/Месяц/Год (stats_period:*)."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    await query.answer()
    period = query.data.split(":", 1)[1]
    text = _build_stats_report(period)
    try:
        await query.edit_message_text(text, reply_markup=_stats_menu_keyboard())
    except Exception:
        # текст не изменился (Telegram не разрешает редактировать в тот же текст) — игнорируем
        pass


def _admin_reviews_delete_keyboard(limit: int = 15) -> InlineKeyboardMarkup:
    """Кнопки удаления под каждым из последних отзывов."""
    items = sorted(REVIEWS.items(), key=lambda kv: kv[1].get("date", ""), reverse=True)[:limit]
    rows = []
    for review_key, r in items:
        stars = "⭐" * r.get("score", 0)
        name = r.get("name", "Клиент")
        rows.append([InlineKeyboardButton(f"🗑 Удалить: {stars} — {name}", callback_data=f"review_delete_ask:{review_key}")])
    return InlineKeyboardMarkup(rows)


async def admin_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reviews и кнопка «📝 Отзывы» — список последних отзывов клиентов с возможностью удалить любой."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    text = _reviews_summary_text() + "\n\n" + _format_reviews_list(limit=15)
    await update.message.reply_text(text, reply_markup=_admin_menu_keyboard())
    if REVIEWS:
        await update.message.reply_text(
            "Управление отзывами — нажмите, чтобы удалить отзыв:",
            reply_markup=_admin_reviews_delete_keyboard(),
        )


async def admin_review_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает удаление отзыва через админ-панель (review_delete_ask/yes/no)."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    data = query.data

    if data.startswith("review_delete_ask:"):
        review_key = data.split(":", 1)[1]
        review = REVIEWS.get(review_key)
        if not review:
            await query.answer("Отзыв уже удалён", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=_admin_reviews_delete_keyboard())
            return
        stars = "⭐" * review.get("score", 0)
        name = review.get("name", "Клиент")
        await query.answer()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, удалить", callback_data=f"review_delete_yes:{review_key}"),
            InlineKeyboardButton("Отмена", callback_data="review_delete_no"),
        ]])
        await query.edit_message_text(f"Удалить отзыв {stars} — {name}?", reply_markup=keyboard)
        return

    if data == "review_delete_no":
        await query.answer("Отменено")
        if REVIEWS:
            await query.edit_message_text(
                "Управление отзывами — нажмите, чтобы удалить отзыв:",
                reply_markup=_admin_reviews_delete_keyboard(),
            )
        else:
            await query.edit_message_text("Отзывов пока нет.")
        return

    if data.startswith("review_delete_yes:"):
        review_key = data.split(":", 1)[1]
        REVIEWS.pop(review_key, None)
        _save_json(REVIEWS_FILE, REVIEWS)
        await query.answer("Отзыв удалён")
        if REVIEWS:
            await query.edit_message_text(
                "Отзыв удалён ✅\n\nУправление отзывами — нажмите, чтобы удалить отзыв:",
                reply_markup=_admin_reviews_delete_keyboard(),
            )
        else:
            await query.edit_message_text("Отзыв удалён ✅\n\nБольше отзывов нет.")
        return


# --- Управление услугами (добавление / изменение цены / удаление) -----------------

def _admin_services_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for code, label, price in SERVICES:
        rows.append([InlineKeyboardButton(f"{label} — {price}", callback_data="svc_noop")])
        rows.append([
            InlineKeyboardButton("✏️ Изменить цену", callback_data=f"svc_editprice:{code}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"svc_delete_ask:{code}"),
        ])
    rows.append([InlineKeyboardButton("➕ Добавить услугу", callback_data="svc_add_start")])
    return InlineKeyboardMarkup(rows)


async def admin_services_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/services и кнопка «🛠 Услуги» — список услуг с управлением."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    context.user_data.pop("awaiting", None)
    await update.message.reply_text(
        "🛠 Управление услугами:\n\nЗдесь можно добавить новую услугу, изменить цену "
        "существующей или удалить её.",
        reply_markup=_admin_services_keyboard(),
    )


async def admin_services_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия в панели управления услугами (svc_*)."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    data = query.data

    if data == "svc_noop":
        await query.answer()
        return

    if data == "svc_add_start":
        await query.answer()
        context.user_data["svc_add"] = {}
        context.user_data["awaiting"] = "svc_add_code"
        await query.edit_message_text(
            "➕ Новая услуга\n\nВведите короткий код услуги латиницей без пробелов "
            "(например: nails, brows) — он используется только внутри бота, клиент его "
            "не увидит. Или напишите «Отмена»:"
        )
        return

    if data.startswith("svc_editprice:"):
        code = data.split(":", 1)[1]
        await query.answer()
        context.user_data["svc_edit_code"] = code
        context.user_data["awaiting"] = "svc_edit_price"
        label = SERVICE_LABELS.get(code, code)
        await query.edit_message_text(
            f"Услуга: {label}\nТекущая цена: {SERVICE_PRICES.get(code)}\n\n"
            f"Введите новую цену одной строкой, как её увидит клиент (например: 250 смн), "
            f"или напишите «Отмена»:"
        )
        return

    if data.startswith("svc_delete_ask:"):
        code = data.split(":", 1)[1]
        label = SERVICE_LABELS.get(code, code)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, удалить", callback_data=f"svc_delete_yes:{code}"),
            InlineKeyboardButton("Нет", callback_data="svc_delete_no"),
        ]])
        await query.answer()
        await query.edit_message_text(
            f"Удалить услугу «{label}»?\n\nУже существующие записи на эту услугу это не затронет.",
            reply_markup=keyboard,
        )
        return

    if data == "svc_delete_no":
        await query.answer("Отменено")
        await query.edit_message_text("🛠 Управление услугами:", reply_markup=_admin_services_keyboard())
        return

    if data.startswith("svc_delete_yes:"):
        code = data.split(":", 1)[1]
        delete_service(code)
        await query.answer("Услуга удалена")
        await query.edit_message_text("🛠 Управление услугами:", reply_markup=_admin_services_keyboard())
        return


# --- Управление акциями (добавление / удаление) ------------------------------------

def _admin_promotions_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for promo_id, promo in _promotions_sorted():
        preview = promo["text"][:30] + ("…" if len(promo["text"]) > 30 else "")
        percent = promo.get("discount_percent", 0)
        status_mark = "✅" if _promotion_is_active(promo) else "⏰истекла"
        title = f"🗑 {preview}"
        if percent:
            title += f" (-{percent}% {status_mark})"
        rows.append([InlineKeyboardButton(title, callback_data=f"promo_delete_ask:{promo_id}")])
    rows.append([InlineKeyboardButton("➕ Добавить акцию", callback_data="promo_add_start")])
    return InlineKeyboardMarkup(rows)


def _promo_services_pick_keyboard(selected: list) -> InlineKeyboardMarkup:
    """Клавиатура выбора услуг, на которые действует скидка (мультивыбор с галочками)."""
    rows = []
    for code, label, _ in SERVICES:
        mark = "✅ " if code in selected else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"promosvc_toggle:{code}")])
    rows.append([InlineKeyboardButton("🔁 Все услуги (снять выбор)", callback_data="promosvc_clear")])
    rows.append([InlineKeyboardButton("➡️ Дальше", callback_data="promosvc_done")])
    return InlineKeyboardMarkup(rows)


async def admin_promotions_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/promotions и кнопка «🎁 Акции» — управление акциями, которые видит клиент."""
    if not _is_admin_chat(update.effective_chat.id):
        await _deny_admin_access(update)
        return
    context.user_data.pop("awaiting", None)
    if PROMOTIONS:
        lines = []
        for _, p in _promotions_sorted():
            line = f"🎁 {p['text']}"
            percent = p.get("discount_percent", 0)
            if percent:
                until = p.get("valid_until")
                until_txt = f" до {date_cls.fromisoformat(until).strftime('%d.%m.%Y')}" if until else ""
                active_txt = "действует" if _promotion_is_active(p) else "истекла"
                line += f"\nСкидка {percent}%{until_txt} ({active_txt})"
            lines.append(line)
        text = "🎁 Текущие акции (их видит клиент по кнопке «🎁 Акции»):\n\n" + "\n\n".join(lines)
    else:
        text = "🎁 Акций пока нет."
    await update.message.reply_text(text, reply_markup=_admin_promotions_keyboard())


async def admin_promotions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия в панели управления акциями (promo_*, promosvc_*)."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    data = query.data

    if data == "promo_add_start":
        await query.answer()
        context.user_data["awaiting"] = "promo_add_text"
        await query.edit_message_text(
            "➕ Новая акция\n\nОтправьте текст акции. Если нужна картинка — отправьте фото "
            "с текстом акции в подписи к нему. Или напишите «Отмена»."
        )
        return

    if data.startswith("promosvc_toggle:"):
        code = data.split(":", 1)[1]
        selected = context.user_data.setdefault("promo_add", {}).setdefault("service_codes", [])
        if code in selected:
            selected.remove(code)
        else:
            selected.append(code)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=_promo_services_pick_keyboard(selected))
        return

    if data == "promosvc_clear":
        context.user_data.setdefault("promo_add", {})["service_codes"] = []
        await query.answer("Скидка будет действовать на все услуги")
        await query.edit_message_reply_markup(reply_markup=_promo_services_pick_keyboard([]))
        return

    if data == "promosvc_done":
        await query.answer()
        context.user_data["awaiting"] = "promo_add_until"
        await query.edit_message_text(
            "До какого числа действует скидка? Введите дату в формате ГГГГ-ММ-ДД "
            "(например 2026-08-10 — тогда 10 августа скидка ещё работает, а 11-го уже нет), "
            "или «-», если скидка бессрочная, либо «Отмена»:"
        )
        return

    if data.startswith("promo_delete_ask:"):
        promo_id = data.split(":", 1)[1]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, удалить", callback_data=f"promo_delete_yes:{promo_id}"),
            InlineKeyboardButton("Нет", callback_data="promo_delete_no"),
        ]])
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return

    if data == "promo_delete_no":
        await query.answer("Отменено")
        await query.edit_message_text("🎁 Управление акциями:", reply_markup=_admin_promotions_keyboard())
        return

    if data.startswith("promo_delete_yes:"):
        promo_id = data.split(":", 1)[1]
        delete_promotion(promo_id)
        await query.answer("Акция удалена")
        await query.edit_message_text("🎁 Управление акциями:", reply_markup=_admin_promotions_keyboard())
        return


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
    writer.writerow(["Дата", "Время", "Услуга", "Клиент", "Телефон", "ID клиента", "Цена (смн)"])
    for key, val in sorted(BOOKINGS.items()):
        date_part, time_part = key.split("_", 1)
        label = SERVICE_LABELS.get(val["service"], val["service"])
        price_value = val.get("price_value", SERVICE_PRICE_VALUES.get(val["service"], 0))
        writer.writerow([date_part, time_part, label, val.get("name", ""), val.get("phone", ""),
                          val.get("client_chat_id", ""), price_value])

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

    # --- Шаги диалога добавления новой услуги: код, название, цена-строка, цена-число ---
    if context.user_data.get("awaiting") == "svc_add_code":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("svc_add", None)
            await msg.reply_text("Добавление услуги отменено.", reply_markup=_admin_menu_keyboard())
            return
        code = text.strip().lower().replace(" ", "_")
        if not code or service_code_exists(code):
            context.user_data["awaiting"] = "svc_add_code"
            await msg.reply_text(
                f"Код «{code}» уже занят или пуст. Введите другой код (или «Отмена»):"
            )
            return
        context.user_data.setdefault("svc_add", {})["code"] = code
        context.user_data["awaiting"] = "svc_add_label"
        await msg.reply_text("Введите название услуги с эмодзи (например: 💅 Маникюр):")
        return

    if context.user_data.get("awaiting") == "svc_add_label":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("svc_add", None)
            await msg.reply_text("Добавление услуги отменено.", reply_markup=_admin_menu_keyboard())
            return
        context.user_data.setdefault("svc_add", {})["label"] = text
        context.user_data["awaiting"] = "svc_add_price"
        await msg.reply_text(
            "Введите цену одной строкой, как её увидит клиент (например: 250 смн):"
        )
        return

    if context.user_data.get("awaiting") == "svc_add_price":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("svc_add", None)
            await msg.reply_text("Добавление услуги отменено.", reply_markup=_admin_menu_keyboard())
            return
        context.user_data.setdefault("svc_add", {})["price"] = text
        context.user_data["awaiting"] = "svc_add_value"
        await msg.reply_text(
            "Введите цену числом в сомони, для подсчёта статистики/дохода (например: 250):"
        )
        return

    if context.user_data.get("awaiting") == "svc_add_value":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("svc_add", None)
            await msg.reply_text("Добавление услуги отменено.", reply_markup=_admin_menu_keyboard())
            return
        try:
            value = int(text.strip())
        except ValueError:
            context.user_data["awaiting"] = "svc_add_value"
            await msg.reply_text("Нужно целое число, например 250. Попробуйте ещё раз (или «Отмена»):")
            return
        svc_data = context.user_data.pop("svc_add", {})
        add_service(svc_data["code"], svc_data["label"], svc_data["price"], value)
        await msg.reply_text(
            f"✅ Услуга добавлена: {svc_data['label']} — {svc_data['price']}",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    # --- Шаги диалога изменения цены услуги: цена-строка, затем цена-число ---
    if context.user_data.get("awaiting") == "svc_edit_price":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("svc_edit_code", None)
            await msg.reply_text("Изменение цены отменено.", reply_markup=_admin_menu_keyboard())
            return
        context.user_data["svc_edit_price_text"] = text
        context.user_data["awaiting"] = "svc_edit_value"
        await msg.reply_text("Введите цену числом в сомони, для статистики (например: 250):")
        return

    if context.user_data.get("awaiting") == "svc_edit_value":
        context.user_data.pop("awaiting", None)
        code = context.user_data.pop("svc_edit_code", None)
        price_text = context.user_data.pop("svc_edit_price_text", None)
        if text.lower() == "отмена" or not code:
            await msg.reply_text("Изменение цены отменено.", reply_markup=_admin_menu_keyboard())
            return
        try:
            value = int(text.strip())
        except ValueError:
            await msg.reply_text(
                "Нужно целое число. Начните заново через 🛠 Услуги.", reply_markup=_admin_menu_keyboard()
            )
            return
        update_service_price(code, price_text, value)
        label = SERVICE_LABELS.get(code, code)
        await msg.reply_text(f"✅ Цена обновлена: {label} — {price_text}", reply_markup=_admin_menu_keyboard())
        return

    # --- Добавление акции: текст, либо фото с текстом в подписи ---
    if context.user_data.get("awaiting") == "promo_add_text":
        context.user_data.pop("awaiting", None)
        if msg.photo:
            caption = (msg.caption or "").strip()
            if not caption:
                context.user_data["awaiting"] = "promo_add_text"
                await msg.reply_text(
                    "К фото нужен текст акции в подписи. Отправьте фото с подписью ещё раз "
                    "(или напишите «Отмена»):"
                )
                return
            context.user_data["promo_add"] = {"text": caption, "photo_file_id": msg.photo[-1].file_id}
            await _ask_promo_discount(msg, context)
            return
        if text.lower() == "отмена":
            context.user_data.pop("promo_add", None)
            await msg.reply_text("Добавление акции отменено.", reply_markup=_admin_menu_keyboard())
            return
        if not text:
            context.user_data["awaiting"] = "promo_add_text"
            await msg.reply_text("Пришлите текст акции или фото с подписью (или «Отмена»):")
            return
        context.user_data["promo_add"] = {"text": text, "photo_file_id": None}
        await _ask_promo_discount(msg, context)
        return

    # --- Акция: ввод процента скидки ---
    if context.user_data.get("awaiting") == "promo_add_percent":
        context.user_data.pop("awaiting", None)
        if text.lower() == "отмена":
            context.user_data.pop("promo_add", None)
            await msg.reply_text("Добавление акции отменено.", reply_markup=_admin_menu_keyboard())
            return
        if text == "-":
            promo_data = context.user_data.pop("promo_add", {})
            add_promotion(promo_data["text"], promo_data.get("photo_file_id"))
            await msg.reply_text("✅ Акция добавлена (без скидки, только информационный текст).",
                                  reply_markup=_admin_menu_keyboard())
            return
        try:
            percent = int(text.strip().replace("%", ""))
            if not (1 <= percent <= 90):
                raise ValueError
        except ValueError:
            context.user_data["awaiting"] = "promo_add_percent"
            await msg.reply_text("Нужно целое число от 1 до 90 (например 10), или «-» без скидки. Ещё раз:")
            return
        context.user_data.setdefault("promo_add", {})["discount_percent"] = percent
        context.user_data.setdefault("promo_add", {}).setdefault("service_codes", [])
        await msg.reply_text(
            "На какие услуги действует скидка? Отметьте нужные и нажмите «➡️ Дальше» "
            "(если не выбрать ни одной — скидка будет действовать на все услуги):",
            reply_markup=_promo_services_pick_keyboard([]),
        )
        return

    # --- Акция: ввод даты окончания скидки ---
    if context.user_data.get("awaiting") == "promo_add_until":
        context.user_data.pop("awaiting", None)
        promo_data = context.user_data.pop("promo_add", None)
        if text.lower() == "отмена" or not promo_data:
            await msg.reply_text("Добавление акции отменено.", reply_markup=_admin_menu_keyboard())
            return
        valid_until = None
        if text.strip() != "-":
            try:
                date_cls.fromisoformat(text.strip())
                valid_until = text.strip()
            except ValueError:
                context.user_data["promo_add"] = promo_data
                context.user_data["awaiting"] = "promo_add_until"
                await msg.reply_text(
                    "Не понял дату. Введите в формате ГГГГ-ММ-ДД (например 2026-08-10), "
                    "«-» для бессрочной скидки, или «Отмена»:"
                )
                return
        add_promotion(
            promo_data["text"], promo_data.get("photo_file_id"),
            discount_percent=promo_data.get("discount_percent", 0),
            service_codes=promo_data.get("service_codes") or [],
            valid_until=valid_until,
        )
        codes = promo_data.get("service_codes") or []
        services_note = ", ".join(SERVICE_LABELS.get(c, c) for c in codes) if codes else "все услуги"
        until_note = f"до {date_cls.fromisoformat(valid_until).strftime('%d.%m.%Y')}" if valid_until else "бессрочно"
        await msg.reply_text(
            f"✅ Акция добавлена: -{promo_data.get('discount_percent', 0)}% на {services_note}, {until_note}.",
            reply_markup=_admin_menu_keyboard(),
        )
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

    if text == ADMIN_MENU_CLIENTS:
        await admin_clients_count(update, context)
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

    if text == ADMIN_MENU_REVIEWS:
        await admin_reviews(update, context)
        return

    if text == ADMIN_MENU_SERVICES:
        await admin_services_start(update, context)
        return

    if text == ADMIN_MENU_PROMOTIONS:
        await admin_promotions_start(update, context)
        return

    if text == ADMIN_MENU_HELP:
        await admin_start(update, context)
        return

    # --- Иначе считаем, что это ответ клиенту (обычный Reply-флоу) ---
    await admin_reply(update, context)


async def _ask_promo_discount(msg, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Общий шаг диалога добавления акции: спросить процент скидки после текста/фото."""
    context.user_data["awaiting"] = "promo_add_percent"
    await msg.reply_text(
        "Нужна ли скидка по этой акции? Введите процент скидки числом (например: 10), "
        "или «-», если это просто информационное сообщение без скидки:"
    )


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


async def admin_attend_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «✅ Пришёл» (admin_attend:slot_key). Отмечает визит как состоявшийся —
    с этого момента запись учитывается в отчётах статистики (день/неделя/месяц/год)."""
    query = update.callback_query

    if not _is_admin_chat(update.effective_chat.id):
        await query.answer("⛔ Доступ только для администратора", show_alert=True)
        return

    slot_key = query.data.split(":", 1)[1]
    entry = mark_attended(slot_key)

    if not entry:
        await query.answer("Запись не найдена (возможно, уже отменена)", show_alert=True)
        return

    await query.answer("Отмечено: клиент пришёл ✅")

    old_text = query.message.text if query.message else ""
    if "Визит: ⏳" in old_text:
        # Это панель cal_info — обновляем строку статуса визита и убираем кнопку «Пришёл».
        new_text = old_text.replace(
            "Визит: ⏳ Ещё не отмечен как пришедший",
            "Визит: ✅ Пришёл (учтено в статистике)",
        )
        date_iso = slot_key.split("_", 1)[0]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Клиент не пришёл", callback_data=f"admin_noshow_ask:{slot_key}")],
            [InlineKeyboardButton("❌ Отменить запись", callback_data=f"admin_cancel_ask:{slot_key}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"cal_date:{date_iso}")],
        ])
        try:
            await query.edit_message_text(new_text, reply_markup=keyboard)
        except Exception:
            pass
    elif old_text and "ОТМЕЧЕНО: КЛИЕНТ ПРИШЁЛ" not in old_text:
        # Это уведомление о новой записи — просто дописываем отметку.
        new_text = old_text + "\n\n✅ ОТМЕЧЕНО: КЛИЕНТ ПРИШЁЛ (учтено в статистике)"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚫 Не пришёл", callback_data=f"admin_noshow_ask:{slot_key}"),
            InlineKeyboardButton("❌ Отменить запись", callback_data=f"admin_cancel_ask:{slot_key}"),
        ]])
        try:
            await query.edit_message_text(new_text, reply_markup=keyboard)
        except Exception:
            pass
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
    client_app.add_handler(CallbackQueryHandler(client_reviews_callback, pattern=r"^(reviews:|freereview:)"))

    client_app.add_handler(MessageHandler(filters.CONTACT, client_contact_received))
    client_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.CONTACT, client_incoming))

    # --- админский бот ---
    admin_app.add_handler(CommandHandler("start", admin_start))
    admin_app.add_handler(CommandHandler("broadcast", admin_broadcast))
    admin_app.add_handler(CommandHandler("slots", admin_slots))
    admin_app.add_handler(CommandHandler("today", admin_today))
    admin_app.add_handler(CommandHandler("calendar", admin_calendar))
    admin_app.add_handler(CommandHandler("stats", admin_stats))
    admin_app.add_handler(CommandHandler("clients", admin_clients_count))
    admin_app.add_handler(CommandHandler("reviews", admin_reviews))
    admin_app.add_handler(CommandHandler("export", admin_export))
    admin_app.add_handler(CommandHandler("addbooking", admin_addbooking_start))
    admin_app.add_handler(CommandHandler("services", admin_services_start))
    admin_app.add_handler(CommandHandler("promotions", admin_promotions_start))
    admin_app.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern=r"^admin_cancel_"))
    admin_app.add_handler(CallbackQueryHandler(admin_attend_callback, pattern=r"^admin_attend:"))
    admin_app.add_handler(CallbackQueryHandler(admin_noshow_callback, pattern=r"^admin_noshow_"))
    admin_app.add_handler(CallbackQueryHandler(admin_stats_period_callback, pattern=r"^stats_period:"))
    admin_app.add_handler(CallbackQueryHandler(admin_review_delete_callback, pattern=r"^review_delete_"))
    admin_app.add_handler(CallbackQueryHandler(admin_cancel_all_callback, pattern=r"^cancelall_"))
    admin_app.add_handler(CallbackQueryHandler(admin_calendar_callback, pattern=r"^cal_"))
    admin_app.add_handler(CallbackQueryHandler(admin_addbooking_callback, pattern=r"^aadd_"))
    admin_app.add_handler(CallbackQueryHandler(admin_services_callback, pattern=r"^svc_"))
    admin_app.add_handler(CallbackQueryHandler(admin_promotions_callback, pattern=r"^(promo_|promosvc_)"))
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
