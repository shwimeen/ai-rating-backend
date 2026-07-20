import os
import json
import time
import hmac
import hashlib
import sqlite3
import urllib.parse
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

client = genai.Client(api_key=GEMINI_KEY)

MODEL_NAME = "gemini-2.5-flash-lite"


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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            mode TEXT,
            rating REAL,
            style_score REAL,
            vibe TEXT,
            potential TEXT,
            summary TEXT,
            strengths TEXT,
            advice TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

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

    conn.execute("CREATE INDEX IF NOT EXISTS idx_analyses_user ON analyses(telegram_id)")

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
        "leaderboard_opt_in, referred_by, referral_count "
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

    _close(conn)

    return {
        "telegram_id": user_info["id"],
        "username": user_info.get("username"),
        "first_name": user_info.get("first_name"),
        "photo_url": user_info.get("photo_url"),
        "leaderboard_opt_in": leaderboard_opt_in,
        "referred_by": referred_by,
        "referral_count": referral_count,
    }


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
    {"id": "inviter", "emoji": "🤝", "name": "Первый друг", "check": lambda s: s["referral_count"] >= 1},
    {"id": "inviter5", "emoji": "📣", "name": "Амбассадор", "check": lambda s: s["referral_count"] >= 5},
]


def get_stats(telegram_id):
    conn = get_conn()

    cur = conn.execute(
        """
        SELECT COUNT(*), COALESCE(MAX(rating), 0), COALESCE(MAX(style_score), 0)
        FROM analyses WHERE telegram_id = ?
        """,
        (telegram_id,),
    )
    total, best_rating, best_style = cur.fetchone()

    cur2 = conn.execute(
        "SELECT referral_count FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    user_row = cur2.fetchone()

    _close(conn)

    return {
        "total": total or 0,
        "best_rating": best_rating or 0,
        "best_style": best_style or 0,
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
        "rating": {"type": "number"},
        "style_score": {"type": "number"},
        "vibe": {"type": "string"},
        "potential": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "advice": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "face_visible",
        "rating",
        "style_score",
        "vibe",
        "potential",
        "strengths",
        "advice",
        "summary",
    ],
}


# ==========================
# GEMINI ANALYSIS
# ==========================

def analyze_image(image_path, mode_key, profile):

    mode_desc = MODES.get(mode_key, MODES["general"])

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

    prompt = (
        "Ты — строгий эксперт по анализу фотографий. "
        "ШАГ 1 (обязательный, выполняется первым): проверь изображение и определи, "
        "есть ли на нём настоящее человеческое лицо, которое хорошо и чётко видно. "
        "Установи face_visible=false, если выполняется хотя бы одно условие: "
        "это скриншот игры, скриншот интерфейса или соцсети, мем, коллаж с текстом, "
        "рисунок или аватар, фотография предмета, животного или пейзажа без человека, "
        "лицо отсутствует в кадре, лицо слишком маленькое или размытое, "
        "лицо закрыто маской, руками или иным объектом, "
        "либо человек повернут спиной/затылком к камере. "
        "В этом случае rating=0, style_score=0, vibe и potential — пустые строки, "
        "strengths и advice — пустые массивы, summary — пустая строка. "
        "ШАГ 2: только если лицо реально видно и его можно оценить, установи face_visible=true "
        "и продолжи анализ. "
        f"Фокус анализа: {mode_desc}. "
        f"{profile_line} "
        "Отвечай строго на русском языке, все поля JSON должны быть на русском. "
        "Если face_visible=true, заполни: "
        "rating — общая оценка внешности от 1.0 до 10.0 с одной цифрой после запятой; "
        "style_score — отдельная оценка стиля, подачи и ухоженности от 1.0 до 10.0; "
        "vibe — короткая фраза из 2-4 слов, описывающая ауру/энергетику человека "
        "(например: 'уверенный минимализм', 'дерзкая харизма'); "
        "potential — одно короткое предложение о том, что сильнее всего повысит оценку; "
        "strengths — список конкретных сильных сторон; "
        "advice — список конкретных советов (волосы, кожа, стиль, одежда, поза, освещение); "
        "summary — краткое резюме на 1-2 предложения. "
        "Оценивай только то, что реально видно на фотографии. Будь реалистичен и справедлив."
    )

    with open(image_path, "rb") as image:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                prompt,
                types.Part.from_bytes(data=image.read(), mime_type="image/jpeg"),
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
            "message": "❌ Лицо не обнаружено. Загрузите фото, где лицо хорошо видно.",
        }

    return data


# ==========================
# API — АНАЛИЗ
# ==========================

@app.post("/analyze")
async def analyze(
    photo: UploadFile = File(...),
    mode: str = Form(...),
    age: str = Form(None),
    height: str = Form(None),
    weight: str = Form(None),
    init_data: str = Form(None),
):

    temp_file = f"temp_{int(time.time() * 1000)}.jpg"

    with open(temp_file, "wb") as f:
        f.write(await photo.read())

    profile = {"age": age, "height": height, "weight": weight}

    try:
        result = None

        for attempt in range(3):
            try:
                print(f"Gemini попытка {attempt + 1}/3")
                result = analyze_image(temp_file, mode, profile)
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
            return result

        # Если пользователь авторизован через Telegram — сохраняем в историю
        user_info = verify_init_data(init_data) if init_data else None

        if user_info:
            user = get_or_create_user(user_info)

            conn = get_conn()
            conn.execute(
                """
                INSERT INTO analyses
                    (telegram_id, mode, rating, style_score, vibe, potential,
                     summary, strengths, advice, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["telegram_id"],
                    mode,
                    result.get("rating", 0),
                    result.get("style_score", 0),
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

        return result

    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)


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
        SELECT id, mode, rating, style_score, vibe, potential, summary,
               strengths, advice, created_at
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
            "vibe": r[4],
            "potential": r[5],
            "summary": r[6],
            "strengths": json.loads(r[7] or "[]"),
            "advice": json.loads(r[8] or "[]"),
            "created_at": r[9],
        }
        for r in rows
    ]

    return {"items": items}


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
# CHECK SERVER
# ==========================

@app.get("/")
def root():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "database": "turso" if USING_TURSO else "local-sqlite",
        "features": ["analyze", "history", "profile", "leaderboard", "referral"],
    }