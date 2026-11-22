"""Base Telegram bot backed by an OpenAI-compatible chat completions API.

The bot has no conversation memory: every incoming message is answered using
only the system prompt plus the current user message.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# Load environment variables from .env before reading them.
load_dotenv()

# System prompt used for every request (no conversation history).
SYSTEM_PROMPT = "Ты полезный AI-ассистент. Отвечай по делу."


def setup_logging() -> logging.Logger:
    """Configure root logging to stdout and return the application logger.

    Log level is taken from the LOG_LEVEL environment variable (default INFO).
    Format: time, level, logger name, message.

    Returns:
        logging.Logger: Configured logger for this module.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # Avoid duplicate handlers if setup_logging is called more than once.
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Keep telegram/httpx libraries quiet unless LOG_LEVEL is DEBUG.
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))

    return logging.getLogger(__name__)


logger = setup_logging()


def get_env(name: str, default: str | None = None) -> str:
    """Read a required environment variable.

    Args:
        name: Variable name.
        default: Optional fallback value.

    Returns:
        str: The variable value.

    Raises:
        RuntimeError: If the variable is missing and no default is provided.
    """
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_temperature(raw: str) -> float:
    """Parse the TEMPERATURE environment variable into a float.

    Falls back to 1.0 if the value cannot be parsed.

    Args:
        raw: Raw string value from the environment.

    Returns:
        float: Temperature in the [0, 2] range.
    """
    try:
        temperature = float(raw)
    except (TypeError, ValueError):
        logger.error("Invalid TEMPERATURE value %r, falling back to 1.0", raw)
        return 1.0
    # Clamp to the commonly supported range for chat models.
    return max(0.0, min(2.0, temperature))


# OpenAI client is created once at import time; failures here are fatal.
try:
    client = OpenAI(
        api_key=get_env("OPENAI_API_KEY"),
        base_url=get_env("OPENAI_BASE_URL", "https://api.proxyapi.ru/openai/v1"),
    )
except Exception as exc:  # noqa: BLE001 - fail fast with a clear message.
    logger.error("Failed to initialize OpenAI client: %s", exc)
    raise

OPENAI_MODEL = get_env("OPENAI_MODEL", "gpt-5.6-luna")
TEMPERATURE = parse_temperature(get_env("TEMPERATURE", "1"))


def ask_model(user_text: str) -> str:
    """Send a single-turn request to the chat model (no history).

    Args:
        user_text: The current user message text.

    Returns:
        str: Model reply text.

    Raises:
        openai.OpenAIError: On any API-side failure.
        RuntimeError: If the API returns no usable choices.
    """
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=TEMPERATURE,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    )
    if not response.choices:
        raise RuntimeError("OpenAI API returned no choices")
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("OpenAI API returned empty message content")
    return content


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command with a short greeting."""
    if update.message is None:
        return
    try:
        await update.message.reply_text(
            "Привет! Я базовый AI-ассистент без памяти. Напиши мне сообщение."
        )
    except Exception:
        logger.exception("Failed to answer /start")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a plain text message: ask the model and reply.

    Errors from the API or Telegram layer are caught, logged, and reported
    back to the user with a generic failure message.

    Args:
        update: Incoming Telegram update.
        context: PTB context (unused, kept for handler signature compatibility).
    """
    message = update.message
    if message is None or message.text is None:
        return

    user_text = message.text
    try:
        # Blocking OpenAI SDK call is executed in a worker thread
        # so the event loop stays responsive.
        answer = await context.application.create_task(_run_in_thread(user_text))
        await message.reply_text(answer)
    except Exception:
        logger.exception("Failed to process message from chat %s", message.chat_id)
        try:
            await message.reply_text(
                "Произошла ошибка при обработке запроса. Попробуйте позже."
            )
        except Exception:
            logger.exception("Failed to send error reply to chat %s", message.chat_id)


async def _run_in_thread(user_text: str) -> str:
    """Run the blocking OpenAI call in a thread and return its result."""
    return await asyncio.to_thread(ask_model, user_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled exception raised inside the PTB application."""
    logger.error("Unhandled exception while processing an update", exc_info=context.error)


def main() -> None:
    """Build the application and start long polling."""
    try:
        bot_token = get_env("BOT_TOKEN")
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    application: Application = ApplicationBuilder().token(bot_token).build()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_error_handler(error_handler)

    logger.info("Bot is starting polling (model=%s)", OPENAI_MODEL)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        logger.exception("Bot stopped due to an error")
        sys.exit(1)


if __name__ == "__main__":
    main()
