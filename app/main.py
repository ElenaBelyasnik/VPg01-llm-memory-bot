"""Telegram bot backed by an OpenAI-compatible chat completions API.

Two modes are controlled by the ``USE_MEMORY`` environment variable:

- ``USE_MEMORY=False`` (default): stateless single-turn replies — only the
  system prompt plus the current user message are sent to the model.
- ``USE_MEMORY=True``: long-term per-user conversation memory stored in
  SQLite via :mod:`app.database`. The last N messages are replayed to the
  model as context, and a ``/reset`` command clears the history.

Run with ``python -m app.main`` from the project root.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.database import Database

# Load environment variables from .env before reading them.
load_dotenv()

# System prompt used for every request.
SYSTEM_PROMPT = "Ты полезный AI-ассистент. Отвечай по делу."

# How many recent messages from SQLite memory are replayed into each request.
HISTORY_LIMIT = 20


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


def parse_bool(raw: str, default: bool = False) -> bool:
    """Parse a boolean-like environment variable value.

    Accepts ``true/1/yes/on`` and ``false/0/no/off`` (case-insensitive).

    Args:
        raw: Raw string value from the environment.
        default: Value returned when the string cannot be interpreted.

    Returns:
        bool: Parsed flag value.
    """
    normalized = raw.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    logger.warning("Cannot parse boolean value %r, using %s", raw, default)
    return default


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


# Configuration is resolved once at import time; failures here are fatal.
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
USE_MEMORY = parse_bool(get_env("USE_MEMORY", "False"), default=False)

# SQLite database is initialized only in memory mode so the stateless bot
# never touches the filesystem.
db: Database | None = None
if USE_MEMORY:
    try:
        db = Database(get_env("DB_PATH", "bot_memory.db"))
    except Exception:
        logger.exception("USE_MEMORY=True but database initialization failed")
        sys.exit(1)


def ask_model(messages: list[dict[str, str]]) -> str:
    """Send a chat completion request to the model.

    Args:
        messages: Full message list (system prompt first, optional history,
            then the current user message).

    Returns:
        str: Model reply text.

    Raises:
        openai.OpenAIError: On any API-side failure.
        RuntimeError: If the API returns no usable choices.
    """
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=TEMPERATURE,
        messages=messages,
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
        if USE_MEMORY:
            await update.message.reply_text(
                "Привет! Я AI-ассистент с долгосрочной памятью. "
                "Я помню нашу переписку; команда /reset очищает историю."
            )
        else:
            await update.message.reply_text(
                "Привет! Я базовый AI-ассистент без памяти. Напиши мне сообщение."
            )
    except Exception:
        logger.exception("Failed to answer /start")


async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /reset command: clear the caller's stored history.

    Registered only when ``USE_MEMORY=True``.
    """
    message = update.message
    if message is None or message.from_user is None:
        return
    if db is None:  # pragma: no cover - defensive, handler is not registered then.
        return
    try:
        deleted = await asyncio.to_thread(db.clear_history, message.from_user.id)
        logger.info("User %s cleared history (%s messages)", message.from_user.id, deleted)
        await message.reply_text("История диалога очищена")
    except Exception:
        logger.exception("Failed to reset history for user %s", message.from_user.id)
        try:
            await message.reply_text(
                "Не удалось очистить историю. Попробуйте позже."
            )
        except Exception:
            logger.exception("Failed to send error reply to chat %s", message.chat_id)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a plain text message: ask the model and reply.

    In memory mode the user message and the assistant answer are persisted
    and the recent history is replayed into the next request. Errors from
    the API or Telegram layer are caught, logged, and reported back to the
    user with a generic failure message.

    Args:
        update: Incoming Telegram update.
        context: PTB context (unused, kept for handler signature compatibility).
    """
    message = update.message
    if message is None or message.text is None or message.from_user is None:
        return

    user_text = message.text
    user_id = message.from_user.id
    try:
        history = await _load_history(user_id)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": user_text},
        ]

        # Blocking OpenAI SDK call is executed in a worker thread
        # so the event loop stays responsive.
        answer = await context.application.create_task(_ask_model_in_thread(messages))

        if USE_MEMORY and db is not None:
            # Save both sides of the exchange after a successful reply.
            # A storage failure must not prevent sending the answer back.
            try:
                await asyncio.to_thread(db.save_message, user_id, "user", user_text)
                await asyncio.to_thread(db.save_message, user_id, "assistant", answer)
            except Exception:
                logger.exception("Failed to persist exchange for user %s", user_id)

        await message.reply_text(answer)
    except Exception:
        logger.exception("Failed to process message from chat %s", message.chat_id)
        try:
            await message.reply_text(
                "Произошла ошибка при обработке запроса. Попробуйте позже."
            )
        except Exception:
            logger.exception("Failed to send error reply to chat %s", message.chat_id)


async def _load_history(user_id: int) -> list[dict[str, str]]:
    """Load recent conversation history for a user (memory mode only).

    Args:
        user_id: Telegram user id.

    Returns:
        list[dict[str, str]]: History messages oldest -> newest, or an empty
        list in stateless mode.
    """
    if not USE_MEMORY or db is None:
        return []
    return await asyncio.to_thread(db.get_history, user_id, HISTORY_LIMIT)


async def _ask_model_in_thread(messages: list[dict[str, str]]) -> str:
    """Run the blocking OpenAI call in a thread and return its result."""
    return await asyncio.to_thread(ask_model, messages)


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
    if USE_MEMORY:
        application.add_handler(CommandHandler("reset", reset_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_error_handler(error_handler)

    logger.info(
        "Bot is starting polling (model=%s, use_memory=%s)", OPENAI_MODEL, USE_MEMORY
    )
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        logger.exception("Bot stopped due to an error")
        sys.exit(1)


if __name__ == "__main__":
    main()
