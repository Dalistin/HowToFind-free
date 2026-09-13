import asyncio
import logging
import os
import json
import re
import html
import time
from datetime import datetime
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice,
)
from telegram.error import RetryAfter, Forbidden, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
    ContextTypes,
)

# ==== ЛОГИРОВАНИЕ ====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
for noisy in ("httpx", "telegram", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ==== ТОКЕН ====
TOKEN = os.getenv("BOT_TOKEN", "8847391443:AAENZR4_-pprfvZ76IMTJt0DhOaGL533v3k")

# ==== ПОДПИСКА ====
CHANNELS = [
    {"id": "@wpftg", "link": "https://t.me/wpftg", "name": "Канал #1"},
    {"id": "@HowToFindWPF", "link": "https://t.me/HowToFindWPF", "name": "Канал #2"},
]

# ==== АДМИНЫ ====
ADMIN_IDS = {8435624867, 7676128040, 676376840}

# ==== ПУТИ ====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATS_FILE = os.path.join(BASE_DIR, "stats.json")

MAX_MSG_LEN = 4000
SEARCH_LIMIT = 20
BROADCAST_DELAY = 0.05
STATS_SAVE_INTERVAL = 5.0
SUB_CACHE_TTL = 60.0

# ==== БАЗЫ ДАННЫХ (оплата звёздами) ====
STARS_PRICE = 50
DB_LINK = "https://t.me/+K77GRMo-nAFmOGIy"   # ← ЗАМЕНИ НА СВОЮ ССЫЛКУ
DB_PAYLOAD = "db_access_v1"


# =========================================================
# ПАРСЕР ССЫЛОК
# =========================================================

_LINK_RE = re.compile(
    r'^\s*(.*)\s*\(\s*(https?://[^\s)]+)\s*\)\s*$',
    re.IGNORECASE,
)


def format_links_html(text: str) -> str:
    out_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue

        m = _LINK_RE.match(stripped)
        if m:
            name = m.group(1).strip()
            url = m.group(2).strip()
            if not name:
                name = url
            out_lines.append(
                f'<a href="{html.escape(url, quote=True)}">'
                f'{html.escape(name)}</a>'
            )
        else:
            out_lines.append(html.escape(line))

    return "\n".join(out_lines)


def truncate(text: str, limit: int = MAX_MSG_LEN) -> str:
    if len(text) > limit:
        return text[:limit] + "\n\n... (обрезано)"
    return text


def truncate_html_body(header: str, body: str,
                       limit: int = MAX_MSG_LEN) -> str:
    full = f"{header}\n\n{body}"
    if len(full) <= limit:
        return full

    lines = body.split("\n")
    acc = []
    total = len(header) + 2
    suffix = "\n\n... (обрезано)"
    for ln in lines:
        if total + len(ln) + 1 > limit - len(suffix):
            acc.append(suffix)
            break
        acc.append(ln)
        total += len(ln) + 1

    return f"{header}\n\n" + "\n".join(acc)


# =========================================================
# ПРЕДЗАГРУЗКА ДАННЫХ
# =========================================================

_DATA_CACHE: dict[tuple[str, str], Optional[str]] = {}
_FORMATTED_CACHE: dict[tuple[str, str], str] = {}


def preload_data() -> None:
    if not os.path.isdir(DATA_DIR):
        logger.warning(f"Папка data/ не найдена: {DATA_DIR}")
        return

    count = 0
    for cat in os.listdir(DATA_DIR):
        cat_dir = os.path.join(DATA_DIR, cat)
        if not os.path.isdir(cat_dir):
            continue
        for filename in os.listdir(cat_dir):
            if not filename.endswith(".txt"):
                continue
            key = filename[:-4]
            path = os.path.join(cat_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                _DATA_CACHE[(cat, key)] = content or None
                count += 1
            except Exception as e:
                logger.error(f"Ошибка чтения {path}: {e}")
                _DATA_CACHE[(cat, key)] = None

    logger.info(f"✅ Предзагружено {count} .txt файлов в память")


def read_data(category_key: str, country_key: str) -> Optional[str]:
    return _DATA_CACHE.get((category_key, country_key))


def get_formatted_html(category_key: str, country_key: str) -> str:
    cache_key = (category_key, country_key)
    cached = _FORMATTED_CACHE.get(cache_key)
    if cached is not None:
        return cached

    raw = read_data(category_key, country_key)
    if raw is None:
        raw = "Данные для этого пункта отсутствуют."
    formatted = format_links_html(raw)
    _FORMATTED_CACHE[cache_key] = formatted
    return formatted


def reload_data() -> None:
    _DATA_CACHE.clear()
    _FORMATTED_CACHE.clear()
    preload_data()


# =========================================================
# СТАТИСТИКА
# =========================================================

_stats_cache: Optional[dict] = None
_stats_last_save = 0.0


def _empty_stats() -> dict:
    return {
        "users": [],
        "total_commands": 0,
        "first_start": datetime.now().isoformat(),
    }


def load_stats() -> dict:
    global _stats_cache
    if _stats_cache is not None:
        return _stats_cache

    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                _stats_cache = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки статистики: {e}")
            _stats_cache = _empty_stats()
    else:
        _stats_cache = _empty_stats()
    return _stats_cache


def _flush_stats(force: bool = False) -> None:
    global _stats_last_save
    now = time.monotonic()
    if not force and now - _stats_last_save < STATS_SAVE_INTERVAL:
        return
    _stats_last_save = now
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(_stats_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения статистики: {e}")


def add_user(user_id: int, username: Optional[str] = None,
             first_name: Optional[str] = None) -> None:
    stats = load_stats()
    now = datetime.now().isoformat()

    for user in stats["users"]:
        if user["id"] == user_id:
            user["last_seen"] = now
            if username:
                user["username"] = username
            if first_name:
                user["first_name"] = first_name
            _flush_stats()
            return

    stats["users"].append({
        "id": user_id,
        "username": username or "None",
        "first_name": first_name or "None",
        "first_seen": now,
        "last_seen": now,
    })
    _flush_stats(force=True)


def increment_commands() -> None:
    stats = load_stats()
    stats["total_commands"] = stats.get("total_commands", 0) + 1
    _flush_stats()


def _safe_iso_to_ts(value: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def get_stats_text() -> str:
    stats = load_stats()

    total_users = len(stats["users"])
    total_commands = stats.get("total_commands", 0)
    first_start = stats.get("first_start", "Неизвестно")

    now_ts = datetime.now().timestamp()
    week_ago = now_ts - 7 * 24 * 60 * 60
    day_ago = now_ts - 24 * 60 * 60

    new_users = 0
    active_users = 0
    for u in stats["users"]:
        fs = _safe_iso_to_ts(u.get("first_seen", ""))
        ls = _safe_iso_to_ts(u.get("last_seen", ""))
        if fs is not None and fs > week_ago:
            new_users += 1
        if ls is not None and ls > day_ago:
            active_users += 1

    text = f"""📊 <b>Статистика бота</b>

👥 <b>Всего пользователей:</b> {total_users}
🆕 <b>Новых за неделю:</b> {new_users}
🟢 <b>Активных за 24ч:</b> {active_users}
📝 <b>Всего команд:</b> {total_commands}
📅 <b>Запущен:</b> {first_start[:19]}

<b>Последние 10 пользователей:</b>
"""

    for user in stats["users"][-10:][::-1]:
        name = user.get("first_name", "Без имени")
        uname = user.get("username")
        username = f"@{uname}" if uname and uname != "None" else "нет username"
        last_seen = user.get("last_seen", "")[:19]
        text += f"• {name} ({username}) - {last_seen}\n"

    return text


def get_all_user_ids() -> list[int]:
    return [u["id"] for u in load_stats()["users"]]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================================================
# ПОИСК
# =========================================================

def search_all(query: str) -> list[str]:
    results = []
    q = query.lower()

    for (cat_key, country_key), content in _DATA_CACHE.items():
        if not content:
            continue
        if q in content.lower():
            cat_name = CATEGORIES.get(cat_key, cat_key)
            country_name = COUNTRY_DISPLAY.get(country_key, country_key)
            snippet = content[:200].replace("\n", " ")
            results.append(f"**{cat_name}** / {country_name}:\n{snippet}...")
    return results


# =========================================================
# КАТЕГОРИИ
# =========================================================

CATEGORIES = {
    "phone": "Номер телефона",
    "account": "Аккаунт",
    "email": "E-mail",
    "fullname": "ФИО",
    "biometry": "Биометрия",
    "address": "Адрес",
    "nickname": "Ник",
    "transport": "Транспорт",
    "documents": "Документы",
    "domain": "Домен",
    "file": "Файл",
    "wallet": "Номер кошелька",
    "password": "Пароль",
    "text": "Текст",
    "tracker": "Трекер",
    "wifi": "WI-FI",
    "serial": "Серийный номер",
    "imei": "IMEI",
}

CATEGORY_PAGES = {
    "phone": [
        {
            "title": "Выберите страну:",
            "rows": [
                [{"text": "Любой", "data": "country:phone:any"}],
                [
                    {"text": "Россия", "data": "country:phone:russia"},
                    {"text": "Украина", "data": "country:phone:ukraine"},
                    {"text": "Казахстан", "data": "country:phone:kazakhstan"},
                ],
                [
                    {"text": "Анонимный", "data": "country:phone:anonymous"},
                    {"text": "Австралия", "data": "country:phone:australia"},
                    {"text": "Беларусь", "data": "country:phone:belarus"},
                ],
                [
                    {"text": "Бразилия", "data": "country:phone:brazil"},
                    {"text": "Венгрия", "data": "country:phone:hungary"},
                    {"text": "Великобритания", "data": "country:phone:uk"},
                ],
                [
                    {"text": "Вьетнам", "data": "country:phone:vietnam"},
                    {"text": "Германия", "data": "country:phone:germany"},
                    {"text": "Гонконг", "data": "country:phone:hongkong"},
                ],
                [
                    {"text": "Дания", "data": "country:phone:denmark"},
                    {"text": "Италия", "data": "country:phone:italy"},
                    {"text": "Исландия", "data": "country:phone:iceland"},
                ],
                [
                    {"text": "Испания", "data": "country:phone:spain"},
                    {"text": "Индия", "data": "country:phone:india"},
                    {"text": "Канада", "data": "country:phone:canada"},
                ],
                [
                    {"text": "Китай", "data": "country:phone:china"},
                    {"text": "Куба", "data": "country:phone:cuba"},
                    {"text": "Латвия", "data": "country:phone:latvia"},
                ],
                [{"text": "Ещё 15 стран ▶", "data": "catpage:phone:1"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
        {
            "title": "Выберите страну (стр. 2):",
            "rows": [
                [
                    {"text": "Молдавия", "data": "country:phone:moldova"},
                    {"text": "Новая Зеландия", "data": "country:phone:newzealand"},
                    {"text": "Нидерланды", "data": "country:phone:netherlands"},
                ],
                [
                    {"text": "Норвегия", "data": "country:phone:norway"},
                    {"text": "Польша", "data": "country:phone:poland"},
                    {"text": "Приднестровье", "data": "country:phone:pridnestrovye"},
                ],
                [
                    {"text": "Румыния", "data": "country:phone:romania"},
                    {"text": "Сингапур", "data": "country:phone:singapore"},
                    {"text": "США", "data": "country:phone:usa"},
                ],
                [
                    {"text": "Франция", "data": "country:phone:france"},
                    {"text": "Швеция", "data": "country:phone:sweden"},
                    {"text": "Швейцария", "data": "country:phone:switzerland"},
                ],
                [
                    {"text": "Эстония", "data": "country:phone:estonia"},
                    {"text": "Южная Корея", "data": "country:phone:southkorea"},
                    {"text": "Япония", "data": "country:phone:japan"},
                ],
                [
                    {"text": "◀️ Назад", "data": "catpage:phone:0"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "account": [
        {
            "title": "Выберите сервис:",
            "rows": [
                [
                    {"text": "VK", "data": "country:account:vk"},
                    {"text": "Telegram", "data": "country:account:telegram"},
                    {"text": "Facebook", "data": "country:account:facebook"},
                ],
                [
                    {"text": "Twitter\\X", "data": "country:account:twitter"},
                    {"text": "Instagram", "data": "country:account:instagram"},
                    {"text": "TikTok", "data": "country:account:tiktok"},
                ],
                [
                    {"text": "OK", "data": "country:account:ok"},
                    {"text": "Youtube", "data": "country:account:youtube"},
                    {"text": "Яндекс", "data": "country:account:yandex"},
                ],
                [
                    {"text": "Amzn.to", "data": "country:account:amzn"},
                    {"text": "Behance", "data": "country:account:behance"},
                    {"text": "Bitbucket", "data": "country:account:bitbucket"},
                ],
                [
                    {"text": "bit.do", "data": "country:account:bitdo"},
                    {"text": "bit.ly", "data": "country:account:bitly"},
                    {"text": "Blogspot", "data": "country:account:blogspot"},
                ],
                [
                    {"text": "Chess.com", "data": "country:account:chess"},
                    {"text": "Clubhouse", "data": "country:account:clubhouse"},
                    {"text": "Cutt.ly", "data": "country:account:cuttly"},
                ],
                [
                    {"text": "Discord", "data": "country:account:discord"},
                    {"text": "Eyeem", "data": "country:account:eyeem"},
                    {"text": "eBay", "data": "country:account:ebay"},
                ],
                [
                    {"text": "Flickr", "data": "country:account:flickr"},
                    {"text": "Gravatar", "data": "country:account:gravatar"},
                    {"text": "Google", "data": "country:account:google"},
                ],
                [
                    {"text": "Github", "data": "country:account:github"},
                    {"text": "Gitlab", "data": "country:account:gitlab"},
                    {"text": "Habr", "data": "country:account:habr"},
                ],
                [{"text": "Ещё 34 сервиса ▶", "data": "catpage:account:1"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
        {
            "title": "Выберите сервис (стр. 2):",
            "rows": [
                [
                    {"text": "Huawei", "data": "country:account:huawei"},
                    {"text": "ICQ", "data": "country:account:icq"},
                    {"text": "Keybase", "data": "country:account:keybase"},
                ],
                [
                    {"text": "Kik", "data": "country:account:kik"},
                    {"text": "LinkedIn", "data": "country:account:linkedin"},
                    {"text": "Minecraft", "data": "country:account:minecraft"},
                ],
                [
                    {"text": "mail.ru", "data": "country:account:mailru"},
                    {"text": "Medium", "data": "country:account:medium"},
                    {"text": "Nintendo", "data": "country:account:nintendo"},
                ],
                [
                    {"text": "OnlyFans", "data": "country:account:onlyfans"},
                    {"text": "Pastebin", "data": "country:account:pastebin"},
                    {"text": "Patreon", "data": "country:account:patreon"},
                ],
                [
                    {"text": "Pikabu", "data": "country:account:pikabu"},
                    {"text": "Pinterest", "data": "country:account:pinterest"},
                    {"text": "Playstation", "data": "country:account:playstation"},
                ],
                [
                    {"text": "QQ", "data": "country:account:qq"},
                    {"text": "Reddit", "data": "country:account:reddit"},
                    {"text": "SoundCloud", "data": "country:account:soundcloud"},
                ],
                [
                    {"text": "Skype", "data": "country:account:skype"},
                    {"text": "Snapchat", "data": "country:account:snapchat"},
                    {"text": "Stackoverflow", "data": "country:account:stackoverflow"},
                ],
                [{"text": "Ещё 13 сервисов ▶", "data": "catpage:account:2"}],
                [
                    {"text": "◀️ Назад", "data": "catpage:account:0"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
        {
            "title": "Выберите сервис (стр. 3):",
            "rows": [
                [
                    {"text": "Steam", "data": "country:account:steam"},
                    {"text": "Slack", "data": "country:account:slack"},
                    {"text": "Tumblr", "data": "country:account:tumblr"},
                ],
                [
                    {"text": "Twitch", "data": "country:account:twitch"},
                    {"text": "Tiny.cc", "data": "country:account:tinycc"},
                    {"text": "Tiny.pl", "data": "country:account:tinypl"},
                ],
                [
                    {"text": "Tinyurl.com", "data": "country:account:tinyurl"},
                    {"text": "vc.ru", "data": "country:account:vcru"},
                    {"text": "VimeWorld", "data": "country:account:vimeworld"},
                ],
                [
                    {"text": "Weibo", "data": "country:account:weibo"},
                    {"text": "WhatsApp", "data": "country:account:whatsapp"},
                    {"text": "Xiaomi", "data": "country:account:xiaomi"},
                ],
                [{"text": "Xbox Live", "data": "country:account:xboxlive"}],
                [
                    {"text": "◀️ Назад", "data": "catpage:account:1"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "email": [
        {
            "title": "Выберите почтовый сервис:",
            "rows": [
                [{"text": "Определить почтовый сервис", "data": "email:detect"}],
                [{"text": "Любой", "data": "country:email:any"}],
                [
                    {"text": "Aol", "data": "country:email:aol"},
                    {"text": "Gmail", "data": "country:email:gmail"},
                    {"text": "Mail.ru", "data": "country:email:mailru"},
                ],
                [
                    {"text": "ProtonMail", "data": "country:email:protonmail"},
                    {"text": "Yahoo", "data": "country:email:yahoo"},
                    {"text": "QQ", "data": "country:email:qq"},
                ],
                [
                    {"text": "GMX.net", "data": "country:email:gmx"},
                    {"text": "Web.de", "data": "country:email:webde"},
                    {"text": "Rambler", "data": "country:email:rambler"},
                ],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "fullname": [
        {
            "title": "Выберите страну:",
            "rows": [
                [{"text": "Любой", "data": "country:fullname:any"}],
                [
                    {"text": "Россия", "data": "country:fullname:russia"},
                    {"text": "Казахстан", "data": "country:fullname:kazakhstan"},
                    {"text": "Украина", "data": "country:fullname:ukraine"},
                ],
                [
                    {"text": "Австралия", "data": "country:fullname:australia"},
                    {"text": "Австрия", "data": "country:fullname:austria"},
                    {"text": "Аргентина", "data": "country:fullname:argentina"},
                ],
                [
                    {"text": "Беларусь", "data": "country:fullname:belarus"},
                    {"text": "Бразилия", "data": "country:fullname:brazil"},
                    {"text": "Бельгия", "data": "country:fullname:belgium"},
                ],
                [
                    {"text": "Болгария", "data": "country:fullname:bulgaria"},
                    {"text": "Великобритания", "data": "country:fullname:uk"},
                    {"text": "Венгрия", "data": "country:fullname:hungary"},
                ],
                [
                    {"text": "Германия", "data": "country:fullname:germany"},
                    {"text": "Гонконг", "data": "country:fullname:hongkong"},
                    {"text": "Греция", "data": "country:fullname:greece"},
                ],
                [
                    {"text": "Дания", "data": "country:fullname:denmark"},
                    {"text": "Индия", "data": "country:fullname:india"},
                    {"text": "Индонезия", "data": "country:fullname:indonesia"},
                ],
                [
                    {"text": "Ирландия", "data": "country:fullname:ireland"},
                    {"text": "Исландия", "data": "country:fullname:iceland"},
                    {"text": "Испания", "data": "country:fullname:spain"},
                ],
                [
                    {"text": "Италия", "data": "country:fullname:italy"},
                    {"text": "Канада", "data": "country:fullname:canada"},
                    {"text": "Кипр", "data": "country:fullname:cyprus"},
                ],
                [
                    {"text": "Киргизия", "data": "country:fullname:kyrgyzstan"},
                    {"text": "Китай", "data": "country:fullname:china"},
                    {"text": "Куба", "data": "country:fullname:cuba"},
                ],
                [{"text": "Ещё 26 стран ▶", "data": "catpage:fullname:1"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
        {
            "title": "Выберите страну (стр. 2):",
            "rows": [
                [
                    {"text": "Латвия", "data": "country:fullname:latvia"},
                    {"text": "Литва", "data": "country:fullname:lithuania"},
                    {"text": "Люксембург", "data": "country:fullname:luxembourg"},
                ],
                [
                    {"text": "Мальта", "data": "country:fullname:malta"},
                    {"text": "Молдова", "data": "country:fullname:moldova"},
                    {"text": "Нидерланды", "data": "country:fullname:netherlands"},
                ],
                [
                    {"text": "Норвегия", "data": "country:fullname:norway"},
                    {"text": "Новая Зеландия", "data": "country:fullname:newzealand"},
                    {"text": "Польша", "data": "country:fullname:poland"},
                ],
                [
                    {"text": "Португалия", "data": "country:fullname:portugal"},
                    {"text": "Приднестровье", "data": "country:fullname:pridnestrovye"},
                    {"text": "Румыния", "data": "country:fullname:romania"},
                ],
                [
                    {"text": "Словакия", "data": "country:fullname:slovakia"},
                    {"text": "Словения", "data": "country:fullname:slovenia"},
                    {"text": "США", "data": "country:fullname:usa"},
                ],
                [
                    {"text": "Турция", "data": "country:fullname:turkey"},
                    {"text": "Таджикистан", "data": "country:fullname:tajikistan"},
                    {"text": "Узбекистан", "data": "country:fullname:uzbekistan"},
                ],
                [
                    {"text": "Франция", "data": "country:fullname:france"},
                    {"text": "Финляндия", "data": "country:fullname:finland"},
                    {"text": "Хорватия", "data": "country:fullname:croatia"},
                ],
                [
                    {"text": "Чехия", "data": "country:fullname:czech"},
                    {"text": "Швейцария", "data": "country:fullname:switzerland"},
                    {"text": "Швеция", "data": "country:fullname:sweden"},
                ],
                [
                    {"text": "Эстония", "data": "country:fullname:estonia"},
                    {"text": "Южная Корея", "data": "country:fullname:southkorea"},
                    {"text": "Япония", "data": "country:fullname:japan"},
                ],
                [
                    {"text": "◀️ Назад", "data": "catpage:fullname:0"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "address": [
        {
            "title": "Выберите тип адреса:",
            "rows": [
                [{"text": "Физический адрес", "data": "address:physical"}],
                [{"text": "IP адрес", "data": "address:ip"}],
                [{"text": "MAC адрес", "data": "address:mac"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "address_physical": [
        {
            "title": "Выберите страну:",
            "rows": [
                [{"text": "Любая", "data": "address_physical:any"}],
                [
                    {"text": "Россия", "data": "address_physical:russia"},
                    {"text": "Украина", "data": "address_physical:ukraine"},
                    {"text": "Беларусь", "data": "address_physical:belarus"},
                ],
                [
                    {"text": "Австралия", "data": "address_physical:australia"},
                    {"text": "Албания", "data": "address_physical:albania"},
                    {"text": "Армения", "data": "address_physical:armenia"},
                ],
                [
                    {"text": "Бельгия", "data": "address_physical:belgium"},
                    {"text": "Болгария", "data": "address_physical:bulgaria"},
                    {"text": "Великобритания", "data": "address_physical:uk"},
                ],
                [
                    {"text": "Венгрия", "data": "address_physical:hungary"},
                    {"text": "Вьетнам", "data": "address_physical:vietnam"},
                    {"text": "Дания", "data": "address_physical:denmark"},
                ],
                [
                    {"text": "Индия", "data": "address_physical:india"},
                    {"text": "Испания", "data": "address_physical:spain"},
                    {"text": "Исландия", "data": "address_physical:iceland"},
                ],
                [
                    {"text": "Казахстан", "data": "address_physical:kazakhstan"},
                    {"text": "Кипр", "data": "address_physical:cyprus"},
                    {"text": "Китай", "data": "address_physical:china"},
                ],
                [
                    {"text": "Косово", "data": "address_physical:kosovo"},
                    {"text": "Латвия", "data": "address_physical:latvia"},
                    {"text": "Нидерланды", "data": "address_physical:netherlands"},
                ],
                [
                    {"text": "Новая Зеландия", "data": "address_physical:newzealand"},
                    {"text": "Норвегия", "data": "address_physical:norway"},
                    {"text": "Польша", "data": "address_physical:poland"},
                ],
                [
                    {"text": "Приднестровье", "data": "address_physical:pridnestrovye"},
                    {"text": "США", "data": "address_physical:usa"},
                    {"text": "Турция", "data": "address_physical:turkey"},
                ],
                [{"text": "Ещё 7 стран ▶", "data": "catpage:address_physical:1"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
        {
            "title": "Выберите страну (стр. 2):",
            "rows": [
                [
                    {"text": "Финляндия", "data": "address_physical:finland"},
                    {"text": "Франция", "data": "address_physical:france"},
                    {"text": "Чехия", "data": "address_physical:czech"},
                ],
                [
                    {"text": "Швеция", "data": "address_physical:sweden"},
                    {"text": "Швейцария", "data": "address_physical:switzerland"},
                    {"text": "Эстония", "data": "address_physical:estonia"},
                ],
                [{"text": "Южная Корея", "data": "address_physical:southkorea"}],
                [
                    {"text": "◀️ Назад", "data": "catpage:address_physical:0"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "transport": [
        {
            "title": "Выберите вид транспорта:",
            "rows": [
                [{"text": "Автомобиль", "data": "transport:car"}],
                [{"text": "Самолет", "data": "transport:plane"}],
                [{"text": "Мотоцикл", "data": "transport:motorcycle"}],
                [{"text": "Поезда", "data": "transport:train"}],
                [{"text": "Судна", "data": "transport:ship"}],
                [{"text": "Грузовые контейнера", "data": "transport:container"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "documents": [
        {
            "title": "Выберите тип документов:",
            "rows": [
                [{"text": "ИП и компании", "data": "documents:business"}],
                [{"text": "Физ. лица", "data": "documents:personal"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "domain": [
        {
            "title": "Выберите домен:",
            "rows": [
                [{"text": "Любой", "data": "domain:any"}],
                [{"text": ".ru", "data": "domain:ru"}],
                [{"text": ".onion", "data": "domain:onion"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "file": [
        {
            "title": "Выберите тип файла:",
            "rows": [
                [{"text": "Определить расширение файла", "data": "file:detect"}],
                [{"text": "Любой", "data": "file:any"}],
                [
                    {"text": "Картинка", "data": "file:image"},
                    {"text": "Видео", "data": "file:video"},
                    {"text": "Документ", "data": "file:document"},
                ],
                [
                    {"text": "HAR", "data": "file:har"},
                    {"text": "DS_STORE", "data": "file:ds_store"},
                    {"text": "Приложение", "data": "file:app"},
                ],
                [{"text": "CVS", "data": "file:cvs"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "wallet": [
        {
            "title": "Выберите тип кошелька:",
            "rows": [
                [{"text": "Криптокошельки", "data": "wallet:crypto"}],
                [{"text": "Платежные системы", "data": "wallet:payment"}],
                [{"text": "Номер банковской карты", "data": "wallet:card"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "tracker": [
        {
            "title": "Выберите трекер:",
            "rows": [
                [{"text": "Любой", "data": "tracker:any"}],
                [
                    {"text": "Mailru", "data": "tracker:mailru"},
                    {"text": "Яндекс Метрика", "data": "tracker:yandex"},
                    {"text": "Google", "data": "tracker:google"},
                ],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "wifi": [
        {
            "title": "Выберите тип Wi-Fi:",
            "rows": [
                [{"text": "SSID - Имя точки доступа", "data": "wifi:ssid"}],
                [{"text": "BSSID - MAC-адрес", "data": "wifi:bssid"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "serial": [
        {
            "title": "Выберите тип:",
            "rows": [
                [{"text": "Техника", "data": "serial:device"}],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
    "other": [
        {
            "title": "Выберите раздел:",
            "rows": [
                [
                    {"text": "Принять почту", "data": "other:mail"},
                    {"text": "Принять СМС", "data": "other:sms"},
                ],
                [
                    {"text": "Визуализация данных", "data": "other:visual"},
                ],
                [
                    {"text": "Расширения для браузера", "data": "other:browser_ext"},
                ],
                [
                    {"text": "◀️ Назад", "data": "action:back"},
                    {"text": "🏠 Главное меню", "data": "action:back"},
                ],
            ],
        },
    ],
}

DIRECT_TEXT_CATEGORIES = {
    "biometry": "Биометрия",
    "nickname": "Ник",
    "password": "Пароль",
    "text": "Текст",
    "imei": "IMEI",
}

COUNTRY_DISPLAY = {
    "any": "Любой",
    "anonymous": "Анонимный", "australia": "Австралия", "belarus": "Беларусь",
    "brazil": "Бразилия", "hungary": "Венгрия", "uk": "Великобритания",
    "vietnam": "Вьетнам", "germany": "Германия", "hongkong": "Гонконг",
    "denmark": "Дания", "italy": "Италия", "iceland": "Исландия",
    "spain": "Испания", "india": "Индия", "canada": "Канада",
    "china": "Китай", "cuba": "Куба", "latvia": "Латвия",
    "moldova": "Молдавия", "newzealand": "Новая Зеландия",
    "netherlands": "Нидерланды", "norway": "Норвегия", "poland": "Польша",
    "pridnestrovye": "Приднестровье", "romania": "Румыния",
    "singapore": "Сингапур", "usa": "США", "france": "Франция",
    "sweden": "Швеция", "switzerland": "Швейцария", "estonia": "Эстония",
    "southkorea": "Южная Корея", "japan": "Япония",
    "russia": "Россия", "ukraine": "Украина", "kazakhstan": "Казахстан",
    "vk": "VK", "telegram": "Telegram", "facebook": "Facebook",
    "twitter": "Twitter\\X", "instagram": "Instagram", "tiktok": "TikTok",
    "ok": "OK", "youtube": "Youtube", "yandex": "Яндекс",
    "amzn": "Amzn.to", "behance": "Behance", "bitbucket": "Bitbucket",
    "bitdo": "bit.do", "bitly": "bit.ly", "blogspot": "Blogspot",
    "chess": "Chess.com", "clubhouse": "Clubhouse", "cuttly": "Cutt.ly",
    "discord": "Discord", "eyeem": "Eyeem", "ebay": "eBay",
    "flickr": "Flickr", "gravatar": "Gravatar", "google": "Google",
    "github": "Github", "gitlab": "Gitlab", "habr": "Habr",
    "huawei": "Huawei", "icq": "ICQ", "keybase": "Keybase",
    "kik": "Kik", "linkedin": "LinkedIn", "minecraft": "Minecraft",
    "mailru": "mail.ru", "medium": "Medium", "nintendo": "Nintendo",
    "onlyfans": "OnlyFans", "pastebin": "Pastebin", "patreon": "Patreon",
    "pikabu": "Pikabu", "pinterest": "Pinterest", "playstation": "Playstation",
    "qq": "QQ", "reddit": "Reddit", "soundcloud": "SoundCloud",
    "skype": "Skype", "snapchat": "Snapchat", "stackoverflow": "Stackoverflow",
    "steam": "Steam", "slack": "Slack", "tumblr": "Tumblr",
    "twitch": "Twitch", "tinycc": "Tiny.cc", "tinypl": "Tiny.pl",
    "tinyurl": "Tinyurl.com", "vcru": "vc.ru", "vimeworld": "VimeWorld",
    "weibo": "Weibo", "whatsapp": "WhatsApp", "xiaomi": "Xiaomi",
    "xboxlive": "Xbox Live",
    "aol": "Aol", "gmail": "Gmail",
    "protonmail": "ProtonMail", "yahoo": "Yahoo",
    "gmx": "GMX.net", "webde": "Web.de", "rambler": "Rambler",
    "austria": "Австрия", "argentina": "Аргентина", "belgium": "Бельгия",
    "bulgaria": "Болгария", "greece": "Греция", "indonesia": "Индонезия",
    "ireland": "Ирландия", "cyprus": "Кипр", "kyrgyzstan": "Киргизия",
    "lithuania": "Литва", "luxembourg": "Люксембург", "malta": "Мальта",
    "portugal": "Португалия", "slovakia": "Словакия", "slovenia": "Словения",
    "turkey": "Турция", "tajikistan": "Таджикистан", "finland": "Финляндия",
    "croatia": "Хорватия", "czech": "Чехия",
    "physical": "Физический адрес", "ip": "IP адрес", "mac": "MAC адрес",
    "car": "Автомобиль", "plane": "Самолет", "motorcycle": "Мотоцикл",
    "train": "Поезда", "ship": "Судна", "container": "Грузовые контейнера",
    "business": "ИП и компании", "personal": "Физ. лица",
    "ru": ".ru", "onion": ".onion",
    "image": "Картинка", "video": "Видео", "document": "Документ",
    "har": "HAR", "ds_store": "DS_STORE", "app": "Приложение", "cvs": "CVS",
    "crypto": "Криптокошельки", "payment": "Платежные системы",
    "card": "Номер банковской карты",
    "ssid": "SSID - Имя точки доступа", "bssid": "BSSID - MAC-адрес",
    "device": "Техника",
    "albania": "Албания", "armenia": "Армения", "kosovo": "Косово",
    "mail": "Принять почту",
    "sms": "Принять СМС",
    "visual": "Визуализация данных",
    "browser_ext": "Расширения для браузера",
}

_NAME_TO_CAT = {name: key for key, name in CATEGORIES.items()}


# =========================================================
# ФЛАГИ
# =========================================================

FLAGS = {
    "russia": "🇷🇺", "ukraine": "🇺🇦", "kazakhstan": "🇰🇿",
    "australia": "🇦🇺", "austria": "🇦🇹", "argentina": "🇦🇷",
    "belarus": "🇧🇾", "brazil": "🇧🇷", "belgium": "🇧🇪",
    "bulgaria": "🇧🇬", "uk": "🇬🇧", "hungary": "🇭🇺",
    "vietnam": "🇻🇳", "germany": "🇩🇪", "hongkong": "🇭🇰",
    "greece": "🇬🇷", "denmark": "🇩🇰", "india": "🇮🇳",
    "indonesia": "🇮🇩", "ireland": "🇮🇪", "iceland": "🇮🇸",
    "spain": "🇪🇸", "italy": "🇮🇹", "canada": "🇨🇦",
    "cyprus": "🇨🇾", "kyrgyzstan": "🇰🇬", "china": "🇨🇳",
    "cuba": "🇨🇺", "latvia": "🇱🇻", "lithuania": "🇱🇹",
    "luxembourg": "🇱🇺", "malta": "🇲🇹", "moldova": "🇲🇩",
    "netherlands": "🇳🇱", "norway": "🇳🇴", "newzealand": "🇳🇿",
    "poland": "🇵🇱", "portugal": "🇵🇹", "pridnestrovye": "🇲🇩",
    "romania": "🇷🇴", "slovakia": "🇸🇰", "slovenia": "🇸🇮",
    "usa": "🇺🇸", "turkey": "🇹🇷", "tajikistan": "🇹🇯",
    "uzbekistan": "🇺🇿", "france": "🇫🇷", "finland": "🇫🇮",
    "croatia": "🇭🇷", "czech": "🇨🇿", "switzerland": "🇨🇭",
    "sweden": "🇸🇪", "estonia": "🇪🇪", "southkorea": "🇰🇷",
    "japan": "🇯🇵", "singapore": "🇸🇬",
    "albania": "🇦🇱", "armenia": "🇦🇲", "kosovo": "🇽🇰",
    "anonymous": "🕵️",
}


def flag_name(country_key: str) -> str:
    name = COUNTRY_DISPLAY.get(country_key, country_key)
    flag = FLAGS.get(country_key)
    return f"{flag} {name}" if flag else name


_COUNTRY_CATEGORIES = frozenset({"phone", "fullname", "address_physical"})


def _is_country_category(category_key: str) -> bool:
    return category_key in _COUNTRY_CATEGORIES


def _btn_text_with_flag(btn: dict, category_key: str) -> str:
    data = btn.get("data", "")
    text = btn["text"]

    if data.startswith("country:"):
        parts = data.split(":", 2)
        if len(parts) == 3 and _is_country_category(category_key):
            return flag_name(parts[2])
        return text

    if category_key == "address_physical" and data.startswith("address_physical:"):
        key = data.split(":", 1)[1]
        return flag_name(key)

    return text


# =========================================================
# КЛАВИАТУРЫ
# =========================================================

_kb_main_cache: Optional[ReplyKeyboardMarkup] = None
_kb_submenu_cache: dict[tuple[str, int], Optional[ReplyKeyboardMarkup]] = {}


def build_main_menu() -> ReplyKeyboardMarkup:
    global _kb_main_cache
    if _kb_main_cache is not None:
        return _kb_main_cache

    keyboard = []
    row = []
    for name in CATEGORIES.values():
        row.append(KeyboardButton(name))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([KeyboardButton("🔎 Поиск по боту")])
    keyboard.append([
        KeyboardButton("❓ Помощь"),
        KeyboardButton("📞 Контакты"),
        KeyboardButton("📎 Прочее"),
    ])
    keyboard.append([KeyboardButton("🗄 Базы Данных")])

    _kb_main_cache = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    return _kb_main_cache


_HIDDEN_NAV = {"◀️ Назад", "🏠 Главное меню"}
_PRESERVE_ROWS = {
    "address", "transport", "documents", "domain",
    "wallet", "wifi", "serial", "address_physical",
    "file", "tracker", "other",
}


def build_category_submenu(category_key: str,
                           page_index: int = 0) -> Optional[ReplyKeyboardMarkup]:
    cache_key = (category_key, page_index)
    if cache_key in _kb_submenu_cache:
        return _kb_submenu_cache[cache_key]

    keyboard = []

    if category_key in CATEGORY_PAGES:
        pages = CATEGORY_PAGES[category_key]
        if page_index >= len(pages):
            _kb_submenu_cache[cache_key] = None
            return None
        page = pages[page_index]

        if category_key in _PRESERVE_ROWS:
            for row_def in page["rows"]:
                visible = [b for b in row_def if b["text"] not in _HIDDEN_NAV]
                if not visible:
                    continue
                if len(visible) == 1:
                    keyboard.append([KeyboardButton(
                        _btn_text_with_flag(visible[0], category_key)
                    )])
                else:
                    row = [KeyboardButton(_btn_text_with_flag(b, category_key))
                           for b in visible]
                    keyboard.append(row)
        else:
            row = []
            for row_def in page["rows"]:
                visible = [b for b in row_def if b["text"] not in _HIDDEN_NAV]
                if len(visible) == 1:
                    if row:
                        keyboard.append(row)
                        row = []
                    keyboard.append([KeyboardButton(
                        _btn_text_with_flag(visible[0], category_key)
                    )])
                    continue
                for btn in visible:
                    row.append(KeyboardButton(
                        _btn_text_with_flag(btn, category_key)
                    ))
                    if len(row) == 3:
                        keyboard.append(row)
                        row = []
            if row:
                keyboard.append(row)
    else:
        cat_dir = os.path.join(DATA_DIR, category_key)
        if os.path.isdir(cat_dir):
            try:
                files = sorted(f[:-4] for f in os.listdir(cat_dir) if f.endswith(".txt"))
                row = []
                for key in files:
                    if _is_country_category(category_key):
                        name = flag_name(key)
                    else:
                        name = COUNTRY_DISPLAY.get(key, key)
                    row.append(KeyboardButton(name))
                    if len(row) == 3:
                        keyboard.append(row)
                        row = []
                if row:
                    keyboard.append(row)
            except Exception as e:
                logger.error(f"Ошибка чтения папки {cat_dir}: {e}")

    nav_row = []
    if page_index > 0:
        nav_row.append(KeyboardButton("◀️ Назад"))
    nav_row.append(KeyboardButton("🏠 Главное меню"))
    keyboard.append(nav_row)

    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    _kb_submenu_cache[cache_key] = markup
    return markup


def invalidate_keyboard_cache() -> None:
    global _kb_main_cache
    _kb_main_cache = None
    _kb_submenu_cache.clear()


# =========================================================
# ПОДПИСКА (с кэшем)
# =========================================================

_sub_cache: dict[int, tuple[float, bool]] = {}


async def check_subscription(user_id: int, bot) -> bool:
    now = time.monotonic()
    cached = _sub_cache.get(user_id)
    if cached and now - cached[0] < SUB_CACHE_TTL:
        return cached[1]

    result = True
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if member.status not in ("member", "administrator", "creator"):
                result = False
                break
        except Exception as e:
            logger.error(f"Ошибка проверки подписки на {ch['id']}: {e}")
            result = False
            break

    _sub_cache[user_id] = (now, result)
    return result


def _subscription_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(f"📢 {ch['name']}", url=ch["link"])]
                for ch in CHANNELS]
    keyboard.append([InlineKeyboardButton("🔄 Проверить подписку",
                                          callback_data="check_sub")])
    return InlineKeyboardMarkup(keyboard)


async def subscription_required(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    text = ("❌ <b>Для использования бота необходима подписка на 2 канала!</b>\n\n"
            "Подпишитесь на следующие каналы:\n\n")
    for ch in CHANNELS:
        text += f"📢 <a href='{ch['link']}'>{ch['name']}</a>\n"
    text += "\nПосле подписки нажмите кнопку <b>\"Проверить подписку\"</b> 👇"

    markup = _subscription_keyboard()

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            try:
                await update.callback_query.message.delete()
            except Exception:
                pass
            await update.callback_query.message.reply_text(
                text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode="HTML")


async def handle_subscription_check(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    _sub_cache.pop(user_id, None)
    if await check_subscription(user_id, context.bot):
        await query.edit_message_text(
            "✅ Подписка на все каналы подтверждена! Добро пожаловать в бота.")
        await start(update, context)
    else:
        try:
            await query.edit_message_text(
                "❌ Вы еще не подписались на все каналы!\n\n"
                "Пожалуйста, подпишитесь на оба канала и нажмите 'Проверить подписку'.",
                reply_markup=_subscription_keyboard(),
                parse_mode="HTML",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Ошибка: {e}")


# =========================================================
# БАЗЫ ДАННЫХ — ОПЛАТА ЗВЁЗДАМИ
# =========================================================

def db_purchase_text() -> str:
    return (
        "🗄 <b>Базы Данных</b>\n\n"
        "Доступ к закрытому каталогу баз данных:\n"
        "• Базы по утечкам\n"
        "• Дампы Телеграма\n"
        "• Базы Почт и паролей\n"
        "• Базы Паспортов\n"
        "• Базы соцсетей\n\n"
        f"💫 Стоимость доступа: <b>{STARS_PRICE} ⭐</b>\n\n"
        "После оплаты бот автоматически пришлёт ссылку на закрытый канал."
    )


def db_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"💫 Оплатить {STARS_PRICE} ⭐",
            callback_data="db_buy",
        )],
    ])


async def db_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data != "db_buy":
        return

    try:
        await context.bot.send_invoice(
            chat_id=query.from_user.id,
            title="Доступ к Базам Данных",
            description="Доступ к закрытому каталогу баз данных",
            payload=DB_PAYLOAD,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Доступ", amount=STARS_PRICE)],
        )
    except Exception as e:
        logger.error(f"Ошибка send_invoice: {e}")
        await query.message.reply_text(
            "❌ Не удалось создать счёт. Попробуйте позже или напишите @dalistin."
        )


async def pre_checkout_handler(update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    try:
        await q.answer(ok=True)
    except Exception as e:
        logger.error(f"Ошибка answer_pre_checkout_query: {e}")


async def successful_payment_handler(update: Update,
                                     context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment

    logger.info(
        f"Оплата от {update.effective_user.id}: "
        f"{payment.total_amount} {payment.currency}, "
        f"payload={payment.invoice_payload}, "
        f"charge_id={payment.telegram_payment_charge_id}"
    )

    if payment.invoice_payload == DB_PAYLOAD:
        await update.message.reply_text(
            "✅ <b>Оплата получена!</b>\n\n"
            "Спасибо за покупку. Вот ваша ссылка:\n\n"
            f"🔗 {DB_LINK}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "✅ Оплата получена, но товар не распознан. "
            "Напишите @dalistin."
        )


# =========================================================
# ХЭНДЛЕРЫ
# =========================================================

_CLEARABLE_STATES = (
    "search_state", "email_detect_state", "current_category", "current_page",
    "address_physical_mode", "adm_broadcast_state", "adm_rename_state",
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    add_user(user.id, user.username, user.first_name)
    increment_commands()

    if not await check_subscription(user.id, context.bot):
        await subscription_required(update, context)
        return

    text = "Добро пожаловать!\n\nВыберите категорию из меню ниже:"
    for key in _CLEARABLE_STATES:
        context.user_data.pop(key, None)

    menu = build_main_menu()

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=menu)
        except BadRequest as e:
            msg = str(e)
            if "Inline keyboard expected" in msg or "Message is not modified" in msg:
                try:
                    await update.callback_query.message.delete()
                except Exception:
                    pass
                await update.callback_query.message.reply_text(text, reply_markup=menu)
            else:
                logger.error(f"Ошибка в start: {e}")
    else:
        await update.message.reply_text(text, reply_markup=menu)


async def stat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info(f"Команда /stat от пользователя {user_id}")
    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ У вас нет доступа к этой команде.\nВаш ID: " + str(user_id))
        return
    await update.message.reply_text(get_stats_text(), parse_mode="HTML")


def build_adm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Написать всем пользователям",
                              callback_data="adm_broadcast")],
        [InlineKeyboardButton("✏️ Изменить никнейм бота",
                              callback_data="adm_rename")],
        [InlineKeyboardButton("📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="adm_close")],
    ])


async def adm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к админ-панели.")
        return
    await update.message.reply_text(
        "👑 <b>Админ-панель</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=build_adm_menu(),
    )


async def adm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await query.edit_message_text("⛔ У вас нет доступа.")
        return

    data = query.data

    if data == "adm_broadcast":
        context.user_data["adm_broadcast_state"] = True
        await query.edit_message_text(
            "📨 <b>Массовая рассылка</b>\n\n"
            "Отправьте текст сообщения, которое нужно разослать всем пользователям бота.\n"
            "Для отмены отправьте /cancel",
            parse_mode="HTML",
        )
    elif data == "adm_rename":
        context.user_data["adm_rename_state"] = True
        await query.edit_message_text(
            "✏️ <b>Изменение никнейма бота</b>\n\n"
            "Отправьте новое имя бота (nickname).\n"
            "Оно будет установлено через setMyName.\n"
            "Для отмены отправьте /cancel",
            parse_mode="HTML",
        )
    elif data == "adm_stats":
        await query.edit_message_text(get_stats_text(), parse_mode="HTML")
    elif data == "adm_close":
        await query.edit_message_text("👑 Админ-панель закрыта.")


async def adm_broadcast_handler(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("adm_broadcast_state"):
        return False
    if not is_admin(update.effective_user.id):
        return False

    text = update.message.text
    if text.strip() == "/cancel":
        context.user_data.pop("adm_broadcast_state", None)
        await update.message.reply_text("❌ Рассылка отменена.")
        return True

    context.user_data.pop("adm_broadcast_state", None)

    user_ids = get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("Нет пользователей для рассылки.")
        return True

    status_msg = await update.message.reply_text(
        f"📨 Начинаю рассылку для {len(user_ids)} пользователей...")

    success = 0
    fail = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            success += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                success += 1
            except Exception as e2:
                fail += 1
                logger.error(f"Повторная ошибка отправки {uid}: {e2}")
        except (Forbidden, BadRequest) as e:
            fail += 1
            logger.warning(f"Не доставлено {uid}: {e}")
        except Exception as e:
            fail += 1
            logger.error(f"Ошибка отправки {uid}: {e}")
        await asyncio.sleep(BROADCAST_DELAY)

    await status_msg.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"📤 Успешно: {success}\n"
        f"❌ Неудачно: {fail}"
    )
    return True


async def adm_rename_handler(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("adm_rename_state"):
        return False
    if not is_admin(update.effective_user.id):
        return False

    new_name = update.message.text.strip()
    if new_name == "/cancel":
        context.user_data.pop("adm_rename_state", None)
        await update.message.reply_text("❌ Изменение никнейма отменено.")
        return True

    context.user_data.pop("adm_rename_state", None)

    try:
        await context.bot.set_my_name(name=new_name)
        await update.message.reply_text(
            f"✅ Никнейм бота успешно изменён на: <b>{new_name}</b>",
            parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка изменения имени: {e}")
        await update.message.reply_text(f"❌ Не удалось изменить никнейм: {e}")
    return True


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("adm_broadcast_state", "adm_rename_state",
                "search_state", "email_detect_state"):
        context.user_data.pop(key, None)
    await update.message.reply_text("❌ Действие отменено.")


# =========================================================
# EMAIL DETECT
# =========================================================

EMAIL_DOMAINS = {
    "gmail.com": "Gmail",
    "googlemail.com": "Gmail",
    "aol.com": "Aol",
    "mail.ru": "Mail.ru",
    "inbox.ru": "Mail.ru",
    "list.ru": "Mail.ru",
    "bk.ru": "Mail.ru",
    "protonmail.com": "ProtonMail",
    "proton.me": "ProtonMail",
    "yahoo.com": "Yahoo",
    "yahoo.ru": "Yahoo",
    "rambler.ru": "Rambler",
    "lenta.ru": "Rambler",
    "qq.com": "QQ",
    "gmx.net": "GMX.net",
    "web.de": "Web.de",
}


def detect_email_service(email: str) -> str:
    at = email.find("@")
    domain = email[at + 1:].strip().lower() if at != -1 else ""
    if domain in EMAIL_DOMAINS:
        return f"Этот адрес принадлежит сервису **{EMAIL_DOMAINS[domain]}**."
    return "Не удалось точно определить сервис."


# =========================================================
# РОУТИНГ
# =========================================================

async def _handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         text: str) -> None:
    context.user_data.pop("search_state", None)
    if not text.strip():
        return
    await update.message.reply_text(f'Поиск по запросу: "{text}"...')
    results = search_all(text)
    if not results:
        response = f'По запросу "{text}" ничего не найдено.'
    else:
        response = f"Найдено результатов: **{len(results)}**\n\n"
        for i, r in enumerate(results[:SEARCH_LIMIT], 1):
            response += f"{i}. {r}\n\n"
        if len(results) > SEARCH_LIMIT:
            response += f"\n... и ещё {len(results) - SEARCH_LIMIT} результатов"
    await update.message.reply_text(truncate(response))
    await update.message.reply_text("Выберите категорию:",
                                    reply_markup=build_main_menu())


async def _handle_email_detect(update: Update,
                               context: ContextTypes.DEFAULT_TYPE,
                               text: str) -> None:
    context.user_data.pop("email_detect_state", None)
    email = text.lower()
    service = detect_email_service(email)
    await update.message.reply_text(
        f"Почтовый сервис для {email}:\n\n{service}")


async def _handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("address_physical_mode"):
        context.user_data.pop("address_physical_mode", None)
        context.user_data["current_category"] = "address"
        context.user_data["current_page"] = 0
        pages = CATEGORY_PAGES["address"]
        markup = build_category_submenu("address", 0)
        await update.message.reply_text(
            f"📂 **Адрес**\n{pages[0]['title']}",
            parse_mode="Markdown",
            reply_markup=markup)
        return

    cat_key = context.user_data.get("current_category")
    current_page = context.user_data.get("current_page", 0)
    if cat_key and current_page > 0:
        prev_page = current_page - 1
        context.user_data["current_page"] = prev_page
        if cat_key in CATEGORY_PAGES:
            pages = CATEGORY_PAGES[cat_key]
            cat_name = CATEGORIES.get(cat_key, cat_key)
            markup = build_category_submenu(cat_key, prev_page)
            if markup:
                await update.message.reply_text(
                    f"📂 **{cat_name}**\n{pages[prev_page]['title']}",
                    parse_mode="Markdown",
                    reply_markup=markup)
                return
    await update.message.reply_text("Вы на первой странице.")


async def _handle_next_page(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            cat_key: str) -> None:
    current_page = context.user_data.get("current_page", 0)
    next_page = current_page + 1
    if cat_key in CATEGORY_PAGES:
        pages = CATEGORY_PAGES[cat_key]
        if next_page < len(pages):
            context.user_data["current_page"] = next_page
            cat_name = CATEGORIES.get(cat_key, cat_key)
            if cat_key == "address_physical":
                cat_name = "Физический адрес"
            markup = build_category_submenu(cat_key, next_page)
            if markup:
                await update.message.reply_text(
                    f"📂 **{cat_name}**\n{pages[next_page]['title']}",
                    parse_mode="Markdown",
                    reply_markup=markup)
                return
    await update.message.reply_text("Больше страниц нет.")


async def _handle_physical_address(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["address_physical_mode"] = True
    context.user_data["current_category"] = "address_physical"
    context.user_data["current_page"] = 0
    pages = CATEGORY_PAGES["address_physical"]
    markup = build_category_submenu("address_physical", 0)
    await update.message.reply_text(
        f"📂 **Физический адрес**\n{pages[0]['title']}",
        parse_mode="Markdown",
        reply_markup=markup)


async def _handle_other_menu(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["current_category"] = "other"
    context.user_data["current_page"] = 0
    pages = CATEGORY_PAGES["other"]
    markup = build_category_submenu("other", 0)
    await update.message.reply_text(
        f"📂 **Прочее**\n{pages[0]['title']}",
        parse_mode="Markdown",
        reply_markup=markup)


async def _handle_db_menu(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        db_purchase_text(),
        parse_mode="HTML",
        reply_markup=db_inline_keyboard(),
    )


def _find_data_button(cat_key: str, page_index: int, text: str) -> Optional[str]:
    pages = CATEGORY_PAGES.get(cat_key)
    if not pages or page_index >= len(pages):
        return None
    for row in pages[page_index]["rows"]:
        for btn in row:
            if btn["text"] == text:
                return btn["data"]
            if _btn_text_with_flag(btn, cat_key) == text:
                return btn["data"]
    return None


async def _send_content(update: Update, category_key: str, sub_key: str,
                        header_prefix: str) -> bool:
    if _is_country_category(category_key):
        display = flag_name(sub_key)
    else:
        display = COUNTRY_DISPLAY.get(sub_key, sub_key)

    body = get_formatted_html(category_key, sub_key)
    header = f"📂 {header_prefix} / {display}:"
    full = truncate_html_body(header, body)

    await update.message.reply_text(
        full,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return True


async def _handle_category_button(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE,
                                  cat_key: str, text: str) -> bool:
    pages = CATEGORY_PAGES.get(cat_key)
    if not pages:
        return False
    current_page = context.user_data.get("current_page", 0)
    if current_page >= len(pages):
        return False

    data = _find_data_button(cat_key, current_page, text)
    if not data:
        return False

    if data.startswith("country:"):
        _, _, sub_key = data.split(":", 2)
        return await _send_content(update, cat_key, sub_key, CATEGORIES[cat_key])

    if data.startswith("address_physical:"):
        sub_key = data.split(":", 1)[1]
        return await _send_content(update, "address_physical", sub_key,
                                   "Физический адрес")

    if ":" in data:
        parts = data.split(":", 1)
        if parts[0] == cat_key:
            header = CATEGORIES.get(cat_key,
                                    "Прочее" if cat_key == "other" else cat_key)
            return await _send_content(update, cat_key, parts[1], header)
    return False


async def _handle_country_from_disk(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE,
                                    cat_key: str, text: str) -> bool:
    is_country = _is_country_category(cat_key)
    for country_key, name in COUNTRY_DISPLAY.items():
        display = flag_name(country_key) if is_country else name
        if name == text or display == text:
            if (cat_key, country_key) in _DATA_CACHE:
                header = f"📂 {CATEGORIES.get(cat_key, cat_key)} / {display}:"
                body = get_formatted_html(cat_key, country_key)
                full = truncate_html_body(header, body)
                await update.message.reply_text(
                    full,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return True
    return False


async def handle_reply_keyboard(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if context.user_data.get("adm_broadcast_state"):
        if await adm_broadcast_handler(update, context):
            return
    if context.user_data.get("adm_rename_state"):
        if await adm_rename_handler(update, context):
            return

    user = update.effective_user
    add_user(user_id, user.username, user.first_name)
    increment_commands()

    if not await check_subscription(user_id, context.bot):
        await subscription_required(update, context)
        return

    text = update.message.text

    if context.user_data.get("search_state"):
        await _handle_search(update, context, text)
        return

    if context.user_data.get("email_detect_state"):
        await _handle_email_detect(update, context, text)
        return

    if text == "🏠 Главное меню":
        for k in ("current_category", "current_page", "address_physical_mode"):
            context.user_data.pop(k, None)
        await update.message.reply_text("Главное меню:",
                                        reply_markup=build_main_menu())
        return

    if text == "◀️ Назад":
        await _handle_back(update, context)
        return

    if text == "🔎 Поиск по боту":
        context.user_data["search_state"] = True
        await update.message.reply_text(
            "Введите поисковый запрос:\n(напишите любое слово или число для поиска по всем категориям)")
        return

    if text == "❓ Помощь":
        await update.message.reply_text(
            "<b>ОПИСАНИЕ</b>\n\n"
            "HowToFind bot — каталог бесплатных ресурсов для поиска из открытых источников.\n\n"
            "Он подскажет где искать, даст ссылки на поисковики\n\n"
            "В каталоге 5000+ ссылок, это сайты, приложения, программы, Telegram-боты и, пошаговые инструкции.\n\n\n"
            "<b>НАВИГАЦИЯ</b>\n\n"
            "Используйте кнопки под полем ввода текста для того, чтобы выбрать то, что вам известно, и получить ссылки на ресурсы.",
            parse_mode="HTML")
        return

    if text == "📞 Контакты":
        await update.message.reply_text("Контакты:\n\nTelegram: @dalistin")
        return

    if text == "📎 Прочее":
        await _handle_other_menu(update, context)
        return

    if text == "🗄 Базы Данных":
        await _handle_db_menu(update, context)
        return

    cat_key = context.user_data.get("current_category")
    if cat_key and text.startswith("Ещё"):
        await _handle_next_page(update, context, cat_key)
        return

    if text == "Физический адрес" and cat_key == "address":
        await _handle_physical_address(update, context)
        return

    if cat_key:
        if await _handle_category_button(update, context, cat_key, text):
            return
        if await _handle_country_from_disk(update, context, cat_key, text):
            return

    new_cat_key = _NAME_TO_CAT.get(text)
    if new_cat_key:
        context.user_data["current_category"] = new_cat_key
        context.user_data["current_page"] = 0
        cat_name = CATEGORIES[new_cat_key]

        if new_cat_key in DIRECT_TEXT_CATEGORIES:
            body = get_formatted_html(new_cat_key, "info")
            full = truncate_html_body(f"📂 {cat_name}", body)
            await update.message.reply_text(
                full,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await update.message.reply_text("Выберите категорию:",
                                            reply_markup=build_main_menu())
            return

        if new_cat_key in CATEGORY_PAGES:
            pages = CATEGORY_PAGES[new_cat_key]
            markup = build_category_submenu(new_cat_key, 0)
            await update.message.reply_text(
                f"📂 **{cat_name}**\n{pages[0]['title']}",
                parse_mode="Markdown",
                reply_markup=markup)
        else:
            markup = build_category_submenu(new_cat_key)
            if markup is None:
                await update.message.reply_text(
                    f"Категория «{cat_name}» — данных пока нет.")
                return
            await update.message.reply_text(
                f"📂 **{cat_name}**\nВыберите:",
                parse_mode="Markdown",
                reply_markup=markup)
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await handle_reply_keyboard(update, context)


# =========================================================
# ЗАПУСК
# =========================================================

async def _post_init(app: Application) -> None:
    preload_data()


def main() -> None:
    print("🚀 Запуск бота...")
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stat", stat_command))
    app.add_handler(CommandHandler("adm", adm_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(handle_subscription_check, pattern="check_sub"))
    app.add_handler(CallbackQueryHandler(db_callback, pattern="^db_buy$"))
    app.add_handler(CallbackQueryHandler(adm_callback, pattern="^adm_"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT,
                                   successful_payment_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("✅ Бот запущен! Напишите /start в Telegram")
    print("📊 Для статистики используйте /stat")
    print("👑 Для админ-панели используйте /adm")
    print(f"👑 Администраторы: {ADMIN_IDS}")
    print(f"⭐ Цена баз данных: {STARS_PRICE} ⭐")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
