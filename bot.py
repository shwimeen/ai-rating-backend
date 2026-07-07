import os
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
        "Добро пожаловать в AI Rating 👋\n\nНажми кнопку ниже, чтобы открыть приложение.",
        reply_markup=keyboard
    )


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Открыть приложение")
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

    app.run_polling()


if __name__ == "__main__":
    main()