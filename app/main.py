from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from .config import settings


telegram_app = (
    Application.builder()
    .token(settings.telegram_bot_token)
    .build()
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ SentinelChat ativo.\n\n"
        "Proteção e moderação da sua comunidade."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ SentinelChat online."
    )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("status", status))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    if settings.telegram_mode == "polling":
        await telegram_app.updater.start_polling()

    yield

    if settings.telegram_mode == "polling":
        await telegram_app.updater.stop()

    await telegram_app.stop()
    await telegram_app.shutdown()


api = FastAPI(
    title="SentinelChat",
    version="0.1.0",
    description="Telegram community protection and moderation platform.",
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    status: str
    environment: str


@api.get("/")
async def root():
    return {
        "name": "SentinelChat",
        "status": "online",
        "version": "0.1.0",
    }


@api.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        environment=settings.app_env,
    )