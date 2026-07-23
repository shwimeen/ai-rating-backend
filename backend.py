import os
import json
import time
import hmac
import hashlib
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware

from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError


load_dotenv()

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")  # тот же токен, что и в bot.py; нужен для проверки подписи initData

# Аккаунты без ограничений (пробная попытка/оплата их не касаются).
# В .env: ADMIN_USERNAMES=shwimeen,another_username (без @, через запятую)
# и/или ADMIN_TELEGRAM_IDS=123456789,987654321 (числовые id, надёжнее — username можно сменить)
ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "").split(",")
    if u.strip()
}
ADMIN_TELEGRAM_IDS = {
    int(i.strip())
    for i in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
    if i.strip().isdigit()
}


def is_admin(user_info):
    if not user_info:
        return False
    if user_info.get("id") in ADMIN_TELEGRAM_IDS:
        return True
    username = (user_info.get("username") or "").lower()
    return bool(username) and username in ADMIN_USERNAMES

client = genai.Client(api_key=GEMINI_KEY)

MODEL_NAME = "gemini-2.5-flash-lite"

# ==========================
# ОПЛАТА (Telegram Stars)
# ==========================
#
# 1 анализ — бесплатно (пробный). Дальше нужны кредиты, покупаются за Stars
# (встроенная валюта Telegram — платёжный провайдер не нужен, currency="XTR").
# Поменяй здесь цены/пакеты под себя.

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

STAR_PACKAGES = {
    "small": {
        "credits": 5,
        "stars": 99,
        "title": "5 анализов",
        "description": "5 дополнительных AI-анализов внешности",
    },
    "medium": {
        "credits": 15,
        "stars": 249,
        "title": "15 анализов",
        "description": "15 анализов — выгоднее, чем по одному",
    },
    "large": {
        "credits": 50,
        "stars": 699,
        "title": "50 анализов",
        "description": "50 анализов — максимальная выгода",
    },
}


# ==========================
# FASTAPI
# ==========================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================
# БАЗА ДАННЫХ
# ==========================
#
# Если заданы TURSO_DATABASE_URL и TURSO_AUTH_TOKEN — используем облачную
# Turso-базу (SQLite-совместимая, бесплатный тариф, ПЕРЕЖИВАЕТ редеплой).
# Это нужно для прода на бесплатном Render: там локальная файловая система
# эфемерна и обнуляется при каждом деплое/рестарте.
#
# Если переменные не заданы — используем обычный локальный файл SQLite
# (app.db) — удобно для локальной разработки, но НЕ переживает редеплой.
#
# Работаем только с позиционными индексами колонок (row[0], row[1], ...),
# без row_factory / доступа по имени — так код гарантированно работает
# одинаково и с sqlite3, и с клиентом Turso.

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
DB_PATH = os.getenv("DB_PATH", "app.db")

USING_TURSO = bool(TURSO_DATABASE_URL)


def get_conn():
    if USING_TURSO:
        import libsql  # локальный импорт: пакет нужен только если используется Turso

        return libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)

    return sqlite3.connect(DB_PATH, timeout=10)


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def init_db():
    conn = get_conn()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            photo_url TEXT,
            leaderboard_opt_in INTEGER NOT NULL DEFAULT 1,
            referred_by INTEGER,
            referral_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    # Миграция для баз, созданных до введения оплаты: добавляем колонки,
    # если их ещё нет (ALTER TABLE ADD COLUMN упадёт с ошибкой, если колонка
    # уже существует — это ожидаемо и безопасно игнорируется).
    for alter_sql in (
        "ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN free_used INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(alter_sql)
            conn.commit()
        except Exception:
            pass

    # Пользователям, которые уже что-то анализировали ДО введения оплаты,
    # считаем пробную попытку использованной — иначе они получат ещё одну
    # бесплатную сверх положенной. Выполняется ниже, после создания analyses.

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            mode TEXT,
            rating REAL,
            style_score REAL,
            symmetry_score REAL,
            harmony_score REAL,
            dimorphism_score REAL,
            vibe TEXT,
            potential TEXT,
            summary TEXT,
            strengths TEXT,
            advice TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    # Миграция для баз, созданных до введения новых метрик.
    for alter_sql in (
        "ALTER TABLE analyses ADD COLUMN symmetry_score REAL",
        "ALTER TABLE analyses ADD COLUMN harmony_score REAL",  # старое имя, оставлено для истории
        "ALTER TABLE analyses ADD COLUMN dimorphism_score REAL",
        "ALTER TABLE analyses ADD COLUMN proportions_score REAL",
        "ALTER TABLE analyses ADD COLUMN jawline_score REAL",
        "ALTER TABLE analyses ADD COLUMN chin_score REAL",
        "ALTER TABLE analyses ADD COLUMN eyes_score REAL",
        "ALTER TABLE analyses ADD COLUMN nose_score REAL",
        "ALTER TABLE analyses ADD COLUMN lips_score REAL",
        "ALTER TABLE analyses ADD COLUMN skin_score REAL",
        "ALTER TABLE analyses ADD COLUMN hair_score REAL",
        "ALTER TABLE analyses ADD COLUMN expression_score REAL",
        "ALTER TABLE analyses ADD COLUMN photo_quality_score REAL",
        "ALTER TABLE analyses ADD COLUMN body_fat_percent REAL",
    ):
        try:
            conn.execute(alter_sql)
            conn.commit()
        except Exception:
            pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_badges (
            telegram_id INTEGER NOT NULL,
            badge_id TEXT NOT NULL,
            earned_at TEXT NOT NULL,
            UNIQUE(telegram_id, badge_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            charge_id TEXT NOT NULL UNIQUE,
            package TEXT,
            stars INTEGER,
            credits INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_analyses_user ON analyses(telegram_id)")

    # Теперь, когда analyses точно существует: пользователям, которые уже
    # что-то анализировали ДО введения оплаты, считаем пробную попытку
    # использованной — иначе они получат ещё одну бесплатную сверх положенной.
    try:
        conn.execute(
            """
            UPDATE users SET free_used = 1
            WHERE free_used = 0
              AND telegram_id IN (SELECT DISTINCT telegram_id FROM analyses)
            """
        )
        conn.commit()
    except Exception:
        pass

    conn.commit()
    _close(conn)


try:
    init_db()
    print(f"✅ БД инициализирована ({'Turso (облако)' if USING_TURSO else 'локальный SQLite-файл'})")
except Exception as e:
    print("❌ Ошибка инициализации базы данных:", e)
    raise


# ==========================
# TELEGRAM INIT DATA
# ==========================

def verify_init_data(init_data: str):
    """
    Проверяет подпись Telegram WebApp initData и возвращает данные пользователя.
    Если BOT_TOKEN не задан (локальная разработка) — доверяем данным без проверки подписи.
    Возвращает dict {id, username, first_name, photo_url} или None.
    """

    if not init_data:
        return None

    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None

    if BOT_TOKEN:
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()

        computed_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            return None

        auth_date = pairs.get("auth_date")
        if auth_date:
            try:
                ts = int(auth_date)
                if time.time() - ts > 86400:
                    return None
            except ValueError:
                pass

    try:
        user = json.loads(user_raw)
    except Exception:
        return None

    if not user.get("id"):
        return None

    return {
        "id": user["id"],
        "username": user.get("username"),
        "first_name": user.get("first_name") or "Аноним",
        "photo_url": user.get("photo_url"),
    }


def get_or_create_user(user_info):
    conn = get_conn()

    cur = conn.execute(
        "SELECT telegram_id, username, first_name, photo_url, "
        "leaderboard_opt_in, referred_by, referral_count, credits, free_used "
        "FROM users WHERE telegram_id = ?",
        (user_info["id"],),
    )
    row = cur.fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, photo_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_info["id"],
                user_info.get("username"),
                user_info.get("first_name"),
                user_info.get("photo_url"),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()

        leaderboard_opt_in, referred_by, referral_count = 1, None, 0
        credits, free_used = 0, 0
    else:
        conn.execute(
            "UPDATE users SET username = ?, first_name = ?, photo_url = ? "
            "WHERE telegram_id = ?",
            (
                user_info.get("username"),
                user_info.get("first_name"),
                user_info.get("photo_url"),
                user_info["id"],
            ),
        )
        conn.commit()

        leaderboard_opt_in, referred_by, referral_count = row[4], row[5], row[6]
        credits, free_used = row[7], row[8]

    _close(conn)

    return {
        "telegram_id": user_info["id"],
        "username": user_info.get("username"),
        "first_name": user_info.get("first_name"),
        "photo_url": user_info.get("photo_url"),
        "leaderboard_opt_in": leaderboard_opt_in,
        "referred_by": referred_by,
        "referral_count": referral_count,
        "credits": credits,
        "free_used": free_used,
    }


# ==========================
# ДОСТУП К АНАЛИЗУ (пробная попытка + кредиты)
# ==========================

def get_access_status(telegram_id):
    """
    Только проверяет, есть ли доступ, НИЧЕГО не списывает.
    Возвращает (allowed: bool, reason: "free" | "credit" | "none").
    """
    conn = get_conn()
    cur = conn.execute(
        "SELECT free_used, credits FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    row = cur.fetchone()
    _close(conn)

    free_used, credits = (row[0], row[1]) if row else (0, 0)

    if not free_used:
        return True, "free"
    if credits and credits > 0:
        return True, "credit"
    return False, "none"


def consume_access(telegram_id, reason):
    """Списывает пробную попытку или один кредит. Вызывать ТОЛЬКО после успешного анализа."""
    if reason == "admin":
        return  # у админов ничего не списываем

    conn = get_conn()

    if reason == "free":
        conn.execute("UPDATE users SET free_used = 1 WHERE telegram_id = ?", (telegram_id,))
    elif reason == "credit":
        conn.execute(
            "UPDATE users SET credits = credits - 1 WHERE telegram_id = ? AND credits > 0",
            (telegram_id,),
        )

    conn.commit()
    _close(conn)


# ==========================
# СТРИКИ И БЕЙДЖИ
# ==========================

def compute_streak(telegram_id):
    conn = get_conn()
    cur = conn.execute(
        "SELECT DISTINCT substr(created_at, 1, 10) AS d FROM analyses "
        "WHERE telegram_id = ? ORDER BY d DESC",
        (telegram_id,),
    )
    rows = cur.fetchall()
    _close(conn)

    if not rows:
        return 0

    dates = [date.fromisoformat(r[0]) for r in rows]
    today = date.today()

    if dates[0] not in (today, today - timedelta(days=1)):
        return 0

    streak = 1
    for i in range(1, len(dates)):
        if dates[i - 1] - dates[i] == timedelta(days=1):
            streak += 1
        else:
            break

    return streak


BADGES = [
    {"id": "first_scan", "emoji": "🎉", "name": "Первый шаг", "check": lambda s: s["total"] >= 1},
    {"id": "five_scans", "emoji": "🔥", "name": "Разогрелся", "check": lambda s: s["total"] >= 5},
    {"id": "twenty_scans", "emoji": "💎", "name": "Ветеран", "check": lambda s: s["total"] >= 20},
    {"id": "streak_3", "emoji": "⚡", "name": "3 дня подряд", "check": lambda s: s["streak"] >= 3},
    {"id": "streak_7", "emoji": "🏆", "name": "Неделя подряд", "check": lambda s: s["streak"] >= 7},
    {"id": "high_score", "emoji": "🌟", "name": "Топ 9+", "check": lambda s: s["best_rating"] >= 9},
    {"id": "style_icon", "emoji": "🕶️", "name": "Икона стиля", "check": lambda s: s["best_style"] >= 9},
    {"id": "symmetry_master", "emoji": "🔮", "name": "Идеальная симметрия", "check": lambda s: s["best_symmetry"] >= 9},
    {"id": "golden_ratio", "emoji": "📐", "name": "Золотое сечение", "check": lambda s: s["best_harmony"] >= 9},
    {"id": "sharp_jawline", "emoji": "🦴", "name": "Чёткая челюсть", "check": lambda s: s["best_jawline"] >= 9},
    {"id": "clear_skin", "emoji": "🧴", "name": "Чистая кожа", "check": lambda s: s["best_skin"] >= 9},
    {"id": "inviter", "emoji": "🤝", "name": "Первый друг", "check": lambda s: s["referral_count"] >= 1},
    {"id": "inviter5", "emoji": "📣", "name": "Амбассадор", "check": lambda s: s["referral_count"] >= 5},
]


def get_stats(telegram_id):
    conn = get_conn()

    cur = conn.execute(
        """
        SELECT COUNT(*), COALESCE(MAX(rating), 0), COALESCE(MAX(style_score), 0),
               COALESCE(MAX(symmetry_score), 0), COALESCE(MAX(proportions_score), 0),
               COALESCE(MAX(jawline_score), 0), COALESCE(MAX(skin_score), 0)
        FROM analyses WHERE telegram_id = ?
        """,
        (telegram_id,),
    )
    total, best_rating, best_style, best_symmetry, best_proportions, best_jawline, best_skin = cur.fetchone()

    cur2 = conn.execute(
        "SELECT referral_count FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    user_row = cur2.fetchone()

    _close(conn)

    return {
        "total": total or 0,
        "best_rating": best_rating or 0,
        "best_style": best_style or 0,
        "best_symmetry": best_symmetry or 0,
        "best_harmony": best_proportions or 0,  # имя ключа сохранено для совместимости бейджей
        "best_jawline": best_jawline or 0,
        "best_skin": best_skin or 0,
        "referral_count": user_row[0] if user_row else 0,
        "streak": compute_streak(telegram_id),
    }


def sync_badges(telegram_id, stats):
    """Начисляет новые бейджи и возвращает список только что полученных."""

    conn = get_conn()

    cur = conn.execute(
        "SELECT badge_id FROM user_badges WHERE telegram_id = ?", (telegram_id,)
    )
    earned_ids = {r[0] for r in cur.fetchall()}

    new_badges = []

    for badge in BADGES:
        if badge["id"] in earned_ids:
            continue

        if badge["check"](stats):
            conn.execute(
                "INSERT OR IGNORE INTO user_badges (telegram_id, badge_id, earned_at) "
                "VALUES (?, ?, ?)",
                (telegram_id, badge["id"], datetime.utcnow().isoformat()),
            )
            new_badges.append({"id": badge["id"], "emoji": badge["emoji"], "name": badge["name"]})

    conn.commit()
    _close(conn)

    return new_badges


def get_all_badges(telegram_id):
    conn = get_conn()
    cur = conn.execute(
        "SELECT badge_id FROM user_badges WHERE telegram_id = ?", (telegram_id,)
    )
    earned_ids = {r[0] for r in cur.fetchall()}
    _close(conn)

    return [
        {"id": b["id"], "emoji": b["emoji"], "name": b["name"], "earned": b["id"] in earned_ids}
        for b in BADGES
    ]


def get_credits(telegram_id):
    conn = get_conn()
    cur = conn.execute("SELECT credits FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    _close(conn)
    return row[0] if row else 0


# ==========================
# РЕЖИМЫ
# ==========================

MODES = {
    "male": "мужской образ (лицо, стиль, ухоженность)",
    "female": "женский образ (лицо, стиль, макияж)",
    "general": "общая привлекательность без привязки к полу",
}


# ==========================
# JSON СХЕМА
# ==========================

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "face_visible": {"type": "boolean"},
        # --- основные критерии (по запросу пользователя) ---
        "rating": {"type": "number"},
        "symmetry_score": {"type": "number"},
        "proportions_score": {"type": "number"},
        "jawline_score": {"type": "number"},
        "chin_score": {"type": "number"},
        "eyes_score": {"type": "number"},
        "nose_score": {"type": "number"},
        "lips_score": {"type": "number"},
        "skin_score": {"type": "number"},
        "hair_score": {"type": "number"},
        "expression_score": {"type": "number"},
        "photo_quality_score": {"type": "number"},
        # --- lookmaxing ---
        "body_fat_percent": {"type": "number"},
        # --- бонусные метрики (были и раньше) ---
        "style_score": {"type": "number"},
        "dimorphism_score": {"type": "number"},
        # --- текстовая часть ---
        "vibe": {"type": "string"},
        "potential": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "advice": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "face_visible",
        "rating",
        "symmetry_score",
        "proportions_score",
        "jawline_score",
        "chin_score",
        "eyes_score",
        "nose_score",
        "lips_score",
        "skin_score",
        "hair_score",
        "expression_score",
        "photo_quality_score",
        "body_fat_percent",
        "style_score",
        "dimorphism_score",
        "vibe",
        "potential",
        "strengths",
        "advice",
        "summary",
    ],
}

# Порядок и подписи для UI (таблица критериев) — единый источник правды,
# фронтенд получает их через /criteria, чтобы подписи не разъезжались.
CRITERIA_GROUPS = [
    {
        "id": "main",
        "title": "📊 Основные критерии",
        "items": [
            {"key": "rating", "emoji": "⭐", "label": "Общая привлекательность"},
            {"key": "symmetry_score", "emoji": "😊", "label": "Симметрия лица"},
            {"key": "proportions_score", "emoji": "📐", "label": "Пропорции лица"},
            {"key": "jawline_score", "emoji": "🦴", "label": "Линия челюсти"},
            {"key": "chin_score", "emoji": "👤", "label": "Подбородок"},
            {"key": "eyes_score", "emoji": "👀", "label": "Глаза"},
            {"key": "nose_score", "emoji": "👃", "label": "Нос"},
            {"key": "lips_score", "emoji": "👄", "label": "Губы"},
            {"key": "skin_score", "emoji": "🧴", "label": "Кожа"},
            {"key": "hair_score", "emoji": "💇", "label": "Волосы и причёска"},
            {"key": "expression_score", "emoji": "😐", "label": "Выражение лица"},
            {"key": "photo_quality_score", "emoji": "📸", "label": "Качество фотографии"},
        ],
    },
    {
        "id": "lookmax",
        "title": "💪 Lookmaxing",
        "items": [
            {"key": "body_fat_percent", "emoji": "🏋️", "label": "Процент жира (оценочно)", "unit": "%"},
        ],
    },
    {
        "id": "bonus",
        "title": "✨ Дополнительно",
        "items": [
            {"key": "style_score", "emoji": "💅", "label": "Стиль"},
            {"key": "dimorphism_score", "emoji": "⚖️", "label": "Диморфизм"},
        ],
    },
]


# ==========================
# GEMINI ANALYSIS
# ==========================

def analyze_image(front_path, profile_path, mode_key, profile):

    mode_desc = MODES.get(mode_key, MODES["general"])

    dimorphism_desc = {
        "male": (
            "dimorphism_score — выраженность маскулинных черт лица (широкая нижняя "
            "челюсть, выраженные надбровные дуги, скулы, тяжёлый подбородок): "
            "10.0 — максимально маскулинное лицо, 1.0 — минимально маскулинное"
        ),
        "female": (
            "dimorphism_score — выраженность женственных черт лица (мягкие линии, "
            "полные губы, тонкие брови, узкий подбородок, гладкий контур): "
            "10.0 — максимально женственное лицо, 1.0 — минимально женственное"
        ),
        "general": (
            "dimorphism_score — насколько ярко лицо тяготеет к типично мужским или "
            "женским чертам (не важно, к каким именно): 10.0 — черты ярко выражены "
            "и контрастны, 1.0 — черты нейтральные, андрогинные"
        ),
    }.get(mode_key, "dimorphism_score — выраженность гендерных черт лица от 1.0 до 10.0")

    profile_line = ""

    if profile:
        parts = []

        if profile.get("age"):
            parts.append(f"возраст {profile['age']} лет")

        if profile.get("height"):
            parts.append(f"рост {profile['height']} см")

        if profile.get("weight"):
            parts.append(f"вес {profile['weight']} кг")

        if parts:
            profile_line = "Дополнительный контекст: " + ", ".join(parts)

    zero_fields = (
        "rating=0, symmetry_score=0, proportions_score=0, jawline_score=0, chin_score=0, "
        "eyes_score=0, nose_score=0, lips_score=0, skin_score=0, hair_score=0, "
        "expression_score=0, photo_quality_score=0, body_fat_percent=0, style_score=0, "
        "dimorphism_score=0"
    )

    prompt = (
        "Ты — строгий эксперт по анализу внешности и антропометрии лица. "
        "Тебе присланы ДВЕ фотографии одного и того же человека: ПЕРВОЕ изображение — "
        "фото анфас (лицом прямо к камере), ВТОРОЕ изображение — фото профиля (вид сбоку). "
        "Используй анфас для оценки симметрии, глаз, губ, кожи, выражения лица; "
        "используй профиль для оценки линии челюсти, подбородка, носа сбоку — эти черты "
        "гораздо точнее видны в профиль. Остальные критерии оценивай, комбинируя оба фото. "
        ""
        "ШАГ 1 (обязательный, выполняется первым): проверь ОБА изображения и определи, "
        "есть ли на них настоящее человеческое лицо, которое хорошо и чётко видно. "
        "Установи face_visible=false, если хотя бы одно из двух фото не подходит: "
        "это скриншот игры, скриншот интерфейса или соцсети, мем, коллаж с текстом, "
        "рисунок или аватар, фотография предмета, животного или пейзажа без человека, "
        "лицо отсутствует в кадре, лицо слишком маленькое или размытое, "
        "лицо закрыто маской, руками или иным объектом. "
        f"В этом случае {zero_fields}, vibe и potential — пустые строки, "
        "strengths и advice — пустые массивы, summary — пустая строка. "
        ""
        "ШАГ 2: только если на обоих фото лицо реально видно, установи face_visible=true "
        "и продолжи анализ. "
        f"Фокус анализа: {mode_desc}. "
        f"{profile_line} "
        "Отвечай строго на русском языке, все поля JSON — на русском. "
        "Если face_visible=true, заполни (все оценки от 1.0 до 10.0 с одной цифрой после "
        "запятой, независимо друг от друга — НЕ копируй одинаковые числа между полями): "
        "rating — итоговая общая привлекательность; "
        "symmetry_score — симметрия лица (левая половина против правой: положение глаз, "
        "бровей, уголков рта, центровка носа), по фото анфас; "
        "proportions_score — гармоничность пропорций (баланс лба/носа/подбородка, "
        "расстояние между глазами, близость к классическим пропорциям); "
        "jawline_score — чёткость и выраженность линии челюсти, оценивай ПРЕИМУЩЕСТВЕННО "
        "по фото профиля; "
        "chin_score — форма и размер подбородка, тоже ориентируйся на профиль; "
        "eyes_score — форма, размер и выразительность глаз (по анфасу); "
        "nose_score — гармоничность носа относительно остального лица (используй оба фото, "
        "особенно профиль для оценки формы спинки носа); "
        "lips_score — объём и симметрия губ; "
        "skin_score — чистота кожи, наличие акне/покраснений/неровностей, текстура; "
        "hair_score — состояние волос и насколько причёска подходит форме лица; "
        "expression_score — естественность и расслабленность выражения лица на фото "
        "(зажатость/улыбка/напряжение); "
        "photo_quality_score — качество самих фотографий (освещение, ракурс, резкость, "
        "разрешение) — это оценка фото, а не внешности человека; "
        "body_fat_percent — ПРИМЕРНАЯ оценка процента жира в организме на основе черт "
        "лица (чёткость скул/челюсти, полнота щёк, второй подбородок), реалистичное "
        "число в диапазоне 5-40, это грубая прикидка, а не медицинское измерение; "
        "style_score — оценка стиля, подачи и ухоженности; "
        f"{dimorphism_desc}; "
        "vibe — короткая фраза из 2-4 слов, описывающая ауру/энергетику человека; "
        "potential — одно короткое предложение о том, что сильнее всего повысит оценку; "
        "strengths — список конкретных сильных сторон; "
        "advice — список конкретных советов (волосы, кожа, стиль, одежда, поза, освещение); "
        "summary — краткое резюме на 1-2 предложения. "
        "Оценивай только то, что реально видно на фотографиях. Будь реалистичен и справедлив."
    )

    with open(front_path, "rb") as f_front, open(profile_path, "rb") as f_profile:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                prompt,
                "Фото 1 — АНФАС:",
                types.Part.from_bytes(data=f_front.read(), mime_type="image/jpeg"),
                "Фото 2 — ПРОФИЛЬ:",
                types.Part.from_bytes(data=f_profile.read(), mime_type="image/jpeg"),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )

    try:
        data = json.loads(response.text)
    except Exception as e:
        print("JSON ERROR:", e)
        return {"error": True, "message": "⚠️ AI вернул некорректный ответ."}

    if not data.get("face_visible"):
        return {
            "error": True,
            "message": "❌ Лицо не обнаружено хотя бы на одном фото. Загрузите два фото, где лицо хорошо видно.",
        }

    return data


# ==========================
# API — АНАЛИЗ
# ==========================

@app.post("/analyze")
async def analyze(
    photo_front: UploadFile = File(...),
    photo_profile: UploadFile = File(...),
    mode: str = Form(...),
    age: str = Form(None),
    height: str = Form(None),
    weight: str = Form(None),
    init_data: str = Form(None),
):
    # Оплата привязана к Telegram-аккаунту, поэтому авторизация теперь
    # обязательна — без неё нельзя ни посчитать пробную попытку, ни списать
    # кредит. Открывать мини-апп нужно через Telegram.
    user_info = verify_init_data(init_data) if init_data else None

    if not user_info:
        return {
            "error": True,
            "message": "⚠️ Открой приложение через Telegram, чтобы им пользоваться.",
        }

    user = get_or_create_user(user_info)

    if is_admin(user_info):
        allowed, reason = True, "admin"
    else:
        allowed, reason = get_access_status(user["telegram_id"])

    if not allowed:
        return {
            "error": True,
            "need_payment": True,
            "message": "🔒 Бесплатная попытка уже использована. Пополни баланс, чтобы продолжить.",
        }

    ts = int(time.time() * 1000)
    front_temp = f"temp_front_{ts}.jpg"
    profile_temp = f"temp_profile_{ts}.jpg"

    with open(front_temp, "wb") as f:
        f.write(await photo_front.read())

    with open(profile_temp, "wb") as f:
        f.write(await photo_profile.read())

    profile = {"age": age, "height": height, "weight": weight}

    try:
        result = None

        for attempt in range(3):
            try:
                print(f"Gemini попытка {attempt + 1}/3")
                result = analyze_image(front_temp, profile_temp, mode, profile)
                print("Анализ успешный")
                break

            except ServerError as e:
                print("Gemini 503:", e)
                if attempt < 2:
                    print("Повтор через 3 секунды...")
                    time.sleep(3)
                else:
                    return {
                        "error": True,
                        "message": "⚠️ AI перегружен. Попробуйте позже.",
                    }

            except ClientError as e:
                print("Gemini API ошибка:", e)
                return {
                    "error": True,
                    "message": "⚠️ Лимит AI запросов закончился.",
                }

            except Exception as e:
                print("Ошибка анализа:", e)
                return {
                    "error": True,
                    "message": "⚠️ Ошибка обработки изображения.",
                }

        if result is None or result.get("error"):
            # Неудачная попытка (например, лицо не найдено) — доступ НЕ списываем.
            return result

        # Успех — теперь можно списать пробную попытку/кредит и сохранить историю.
        consume_access(user["telegram_id"], reason)

        conn = get_conn()
        conn.execute(
            """
            INSERT INTO analyses
                (telegram_id, mode, rating, style_score, symmetry_score, proportions_score,
                 jawline_score, chin_score, eyes_score, nose_score, lips_score, skin_score,
                 hair_score, expression_score, photo_quality_score, body_fat_percent,
                 dimorphism_score, vibe, potential, summary, strengths, advice, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["telegram_id"],
                mode,
                result.get("rating", 0),
                result.get("style_score", 0),
                result.get("symmetry_score", 0),
                result.get("proportions_score", 0),
                result.get("jawline_score", 0),
                result.get("chin_score", 0),
                result.get("eyes_score", 0),
                result.get("nose_score", 0),
                result.get("lips_score", 0),
                result.get("skin_score", 0),
                result.get("hair_score", 0),
                result.get("expression_score", 0),
                result.get("photo_quality_score", 0),
                result.get("body_fat_percent", 0),
                result.get("dimorphism_score", 0),
                result.get("vibe", ""),
                result.get("potential", ""),
                result.get("summary", ""),
                json.dumps(result.get("strengths", []), ensure_ascii=False),
                json.dumps(result.get("advice", []), ensure_ascii=False),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        _close(conn)

        stats = get_stats(user["telegram_id"])
        new_badges = sync_badges(user["telegram_id"], stats)

        result["streak"] = stats["streak"]
        result["total_analyses"] = stats["total"]
        result["new_badges"] = new_badges
        result["credits_left"] = get_credits(user["telegram_id"])
        result["used_free_trial"] = reason == "free"
        result["mode"] = mode

        return result

    finally:
        for f in (front_temp, profile_temp):
            if os.path.exists(f):
                os.remove(f)


# ==========================
# API — ИСТОРИЯ
# ==========================

@app.get("/history")
def history(init_data: str = Query(...), limit: int = 20):
    user_info = verify_init_data(init_data)
    if not user_info:
        return {"error": True, "message": "⚠️ Не удалось подтвердить пользователя Telegram."}

    conn = get_conn()
    cur = conn.execute(
        """
        SELECT id, mode, rating, style_score, symmetry_score, proportions_score,
               jawline_score, chin_score, eyes_score, nose_score, lips_score, skin_score,
               hair_score, expression_score, photo_quality_score, body_fat_percent,
               dimorphism_score, vibe, potential, summary, strengths, advice, created_at
        FROM analyses WHERE telegram_id = ?
        ORDER BY created_at DESC LIMIT ?
        """,
        (user_info["id"], limit),
    )
    rows = cur.fetchall()
    _close(conn)

    items = [
        {
            "id": r[0],
            "mode": r[1],
            "rating": r[2],
            "style_score": r[3],
            "symmetry_score": r[4],
            "proportions_score": r[5],
            "jawline_score": r[6],
            "chin_score": r[7],
            "eyes_score": r[8],
            "nose_score": r[9],
            "lips_score": r[10],
            "skin_score": r[11],
            "hair_score": r[12],
            "expression_score": r[13],
            "photo_quality_score": r[14],
            "body_fat_percent": r[15],
            "dimorphism_score": r[16],
            "vibe": r[17],
            "potential": r[18],
            "summary": r[19],
            "strengths": json.loads(r[20] or "[]"),
            "advice": json.loads(r[21] or "[]"),
            "created_at": r[22],
        }
        for r in rows
    ]

    return {"items": items}


@app.get("/criteria")
def criteria():
    """Единый источник правды для подписей/эмодзи критериев — фронтенд строит по этому таблицу."""
    return {"groups": CRITERIA_GROUPS}


# ==========================
# API — ПРОФИЛЬ
# ==========================

@app.get("/profile")
def profile(init_data: str = Query(...)):
    user_info = verify_init_data(init_data)
    if not user_info:
        return {"error": True, "message": "⚠️ Не удалось подтвердить пользователя Telegram."}

    user = get_or_create_user(user_info)
    stats = get_stats(user["telegram_id"])
    badges = get_all_badges(user["telegram_id"])

    return {
        "first_name": user["first_name"],
        "username": user["username"],
        "photo_url": user["photo_url"],
        "leaderboard_opt_in": bool(user["leaderboard_opt_in"]),
        "credits": user["credits"],
        "free_used": bool(user["free_used"]),
        "stats": stats,
        "badges": badges,
    }


@app.post("/profile/visibility")
def set_visibility(init_data: str = Form(...), visible: bool = Form(...)):
    user_info = verify_init_data(init_data)
    if not user_info:
        return {"error": True, "message": "⚠️ Не удалось подтвердить пользователя Telegram."}

    get_or_create_user(user_info)

    conn = get_conn()
    conn.execute(
        "UPDATE users SET leaderboard_opt_in = ? WHERE telegram_id = ?",
        (1 if visible else 0, user_info["id"]),
    )
    conn.commit()
    _close(conn)

    return {"ok": True}


# ==========================
# API — ЛИДЕРБОРД
# ==========================

@app.get("/leaderboard")
def leaderboard(init_data: str = Query(None), limit: int = 20):
    conn = get_conn()
    cur = conn.execute(
        """
        SELECT u.telegram_id, u.first_name, u.username, u.photo_url,
               MAX(a.rating) AS best_rating
        FROM users u
        JOIN analyses a ON a.telegram_id = u.telegram_id
        WHERE u.leaderboard_opt_in = 1
        GROUP BY u.telegram_id
        ORDER BY best_rating DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    _close(conn)

    my_id = None
    if init_data:
        user_info = verify_init_data(init_data)
        if user_info:
            my_id = user_info["id"]

    items = [
        {
            "rank": i + 1,
            "telegram_id": r[0],
            "first_name": r[1],
            "username": r[2],
            "photo_url": r[3],
            "best_rating": r[4],
            "is_you": r[0] == my_id,
        }
        for i, r in enumerate(rows)
    ]

    return {"items": items}


# ==========================
# API — РЕФЕРАЛЫ
# ==========================

@app.post("/referral")
def referral(init_data: str = Form(...), referred_by: int = Form(...)):
    user_info = verify_init_data(init_data)
    if not user_info:
        return {"error": True, "message": "⚠️ Не удалось подтвердить пользователя Telegram."}

    if referred_by == user_info["id"]:
        return {"ok": False, "message": "Нельзя пригласить самого себя."}

    user = get_or_create_user(user_info)

    if user["referred_by"]:
        return {"ok": False, "message": "Реферал уже засчитан ранее."}

    conn = get_conn()

    cur = conn.execute(
        "SELECT telegram_id FROM users WHERE telegram_id = ?", (referred_by,)
    )
    referrer_row = cur.fetchone()

    if not referrer_row:
        _close(conn)
        return {"ok": False, "message": "Пригласивший пользователь не найден."}

    conn.execute(
        "UPDATE users SET referred_by = ? WHERE telegram_id = ?",
        (referred_by, user_info["id"]),
    )
    conn.execute(
        "UPDATE users SET referral_count = referral_count + 1 WHERE telegram_id = ?",
        (referred_by,),
    )
    conn.commit()
    _close(conn)

    stats = get_stats(referred_by)
    sync_badges(referred_by, stats)

    return {"ok": True}


# ==========================
# API — ОПЛАТА (Telegram Stars)
# ==========================

@app.get("/packages")
def packages():
    return {
        "items": [
            {"id": key, "credits": p["credits"], "stars": p["stars"], "title": p["title"]}
            for key, p in STAR_PACKAGES.items()
        ]
    }


@app.post("/create_invoice")
def create_invoice(init_data: str = Form(...), package: str = Form(...)):
    user_info = verify_init_data(init_data)
    if not user_info:
        return {"error": True, "message": "⚠️ Не удалось подтвердить пользователя Telegram."}

    pkg = STAR_PACKAGES.get(package)
    if not pkg:
        return {"error": True, "message": "Неизвестный пакет."}

    if not TELEGRAM_API_BASE:
        return {
            "error": True,
            "message": "⚠️ Оплата временно недоступна (на сервере не настроен BOT_TOKEN).",
        }

    payload = json.dumps(
        {"telegram_id": user_info["id"], "credits": pkg["credits"], "package": package}
    )

    body = json.dumps(
        {
            "title": pkg["title"],
            "description": pkg["description"],
            "payload": payload,
            "currency": "XTR",
            "prices": [{"label": pkg["title"], "amount": pkg["stars"]}],
        }
    ).encode()

    req = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/createInvoiceLink",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print("createInvoiceLink error:", e)
        return {"error": True, "message": "⚠️ Не удалось создать счёт для оплаты."}

    if not data.get("ok"):
        return {"error": True, "message": data.get("description", "Ошибка Telegram API")}

    return {"invoice_link": data["result"]}


# ==========================
# CHECK SERVER
# ==========================

@app.get("/")
def root():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "database": "turso" if USING_TURSO else "local-sqlite",
        "features": ["analyze", "history", "profile", "leaderboard", "referral", "payments"],
    }