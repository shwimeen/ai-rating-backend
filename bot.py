import os
import json
import sqlite3
import threading
from datetime import datetime

import uvicorn
from fastapi import FastAPI


web_app = FastAPI()


@web_app.get("/")
def home():
    return {
        "status": "bot running"
    }


def run_web():
    uvicorn.run(
        web_app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )


threading.Thread(
    target=run_web,
    daemon=True
).start()

from dotenv import load_dotenv

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    MessageHandler,
    filters,
)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBAPP_URL = "https://shwimeen.github.io/ai-rating-webapp/?v=6"


# ==========================
# БАЗА ДАННЫХ (та же, что использует backend.py)
# ==========================
#
# ВАЖНО: здесь нужны ТЕ ЖЕ значения TURSO_DATABASE_URL / TURSO_AUTH_TOKEN,
# что и в переменных окружения сервиса backend.py — это одна общая база,
# просто к ней обращаются два разных процесса (бот и бэкенд).

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
DB_PATH = os.getenv("DB_PATH", "app.db")

USING_TURSO = bool(TURSO_DATABASE_URL)


def get_conn():
    if USING_TURSO:
        import libsql

        return libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)

    return sqlite3.connect(DB_PATH, timeout=10)


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def ensure_user_exists(telegram_id):
    """На случай если оплата пришла раньше, чем пользователь открыл Mini App хоть раз."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_id, created_at) VALUES (?, ?)",
        (telegram_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    _close(conn)


def credit_payment(telegram_id, credits, charge_id, package, stars):
    """
    Начисляет кредиты за платёж. Идемпотентно — charge_id уникален, повторная
    доставка того же successful_payment (Telegram иногда ретраит апдейты)
    не приведёt к повторному начислению.
    Возвращает True, если кредиты реально начислены (это первая обработка).
    """
    conn = get_conn()

    cur = conn.execute("SELECT 1 FROM payments WHERE charge_id = ?", (charge_id,))
    if cur.fetchone():
        _close(conn)
        return False

    conn.execute(
        """
        INSERT INTO payments (telegram_id, charge_id, package, stars, credits, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, charge_id, package, stars, credits, datetime.utcnow().isoformat()),
    )
    conn.execute(
        "UPDATE users SET credits = credits + ? WHERE telegram_id = ?",
        (credits, telegram_id),
    )
    conn.commit()
    _close(conn)
    return True


# ==========================
# КОМАНДЫ
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text="🚀 Открыть AI Rating",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ]
    ])

    await update.message.reply_text(
        "Добро пожаловать в AI Rating 👋",
        reply_markup=keyboard
    )


# ==========================
# ОПЛАТА (Telegram Stars)
# ==========================

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram спрашивает подтверждение прямо перед списанием звёзд."""
    query = update.pre_checkout_query

    try:
        json.loads(query.invoice_payload)
    except Exception:
        await query.answer(ok=False, error_message="Некорректный заказ, попробуй ещё раз.")
        return

    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приходит, когда звёзды реально списаны."""
    payment = update.message.successful_payment

    try:
        payload = json.loads(payment.invoice_payload)
        telegram_id = int(payload["telegram_id"])
        credits = int(payload["credits"])
        package = payload.get("package", "")
    except Exception as e:
        print("Ошибка разбора payload платежа:", e)
        await update.message.reply_text(
            "⚠️ Оплата прошла, но не удалось её обработать. Напиши в поддержку."
        )
        return

    ensure_user_exists(telegram_id)

    credited = credit_payment(
        telegram_id=telegram_id,
        credits=credits,
        charge_id=payment.telegram_payment_charge_id,
        package=package,
        stars=payment.total_amount,
    )

    if credited:
        await update.message.reply_text(
            f"✅ Оплата получена! Начислено {credits} анализов. Открой приложение и продолжай ✨"
        )
    else:
        # Этот платёж уже был обработан ранее (повторная доставка апдейта) — просто подтверждаем.
        await update.message.reply_text("✅ Этот платёж уже был засчитан ранее.")


async def post_init(application):

    await application.bot.set_my_commands([
        BotCommand(
            "start",
            "Открыть приложение"
        )
    ])


def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    print("Bot started 🚀")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()