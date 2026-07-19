import os
import threading
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
    WebAppInfo
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBAPP_URL = "https://shwimeen.github.io/ai-rating-webapp/?v=5"


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


    app.add_handler(
        CommandHandler("start", start)
    )


    print("Bot started 🚀")


    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()