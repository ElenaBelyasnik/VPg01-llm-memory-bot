"""SQLite-backed long-term conversation memory for the Telegram bot.

The module exposes a small :class:`Database` wrapper around the standard
library :mod:`sqlite3` driver. It is intentionally synchronous: callers run
its methods inside a worker thread (``asyncio.to_thread``) so the asyncio
event loop of python-telegram-bot is never blocked.
"""

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Schema is applied on every connection start; IF NOT EXISTS keeps it idempotent.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages (user_id);
"""


class Database:
    """Persistent per-user conversation storage backed by SQLite.

    Args:
        db_path: Path to the SQLite database file. It is created
            automatically if it does not exist.
    """

    def __init__(self, db_path: str = "bot_memory.db") -> None:
        self.db_path = db_path
        try:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
            logger.info("Database initialized at %s", self.db_path)
        except sqlite3.Error:
            logger.exception("Failed to initialize database at %s", self.db_path)
            raise

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived SQLite connection.

        A new connection per operation keeps the helper thread-safe: SQLite
        connections created in one thread cannot be used in another, and each
        database call may run in a different ``asyncio.to_thread`` worker.

        Returns:
            sqlite3.Connection: Connection with autocommit-style isolation.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def save_message(self, user_id: int, role: str, content: str) -> None:
        """Persist a single message of the conversation.

        Args:
            user_id: Telegram user id the message belongs to.
            role: Message role, e.g. ``user`` or ``assistant``.
            content: Message text.

        Raises:
            sqlite3.Error: On any database failure (logged before re-raise).
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO messages (user_id, role, content, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, role, content, timestamp),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.exception(
                "Failed to save %s message for user %s", role, user_id
            )
            raise

    def get_history(self, user_id: int, limit: int = 20) -> list[dict[str, str]]:
        """Return the latest messages of a user in chronological order.

        Args:
            user_id: Telegram user id.
            limit: Maximum number of most recent messages to return.

        Returns:
            list[dict[str, str]]: Messages as ``{"role": ..., "content": ...}``
            dictionaries ordered oldest -> newest. Empty list if no history
            or on database error (failure is logged, bot keeps working).
        """
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT role, content FROM ("
                    "  SELECT id, role, content FROM messages"
                    "  WHERE user_id = ? ORDER BY id DESC LIMIT ?"
                    ") ORDER BY id ASC",
                    (user_id, limit),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.exception("Failed to load history for user %s", user_id)
            return []
        return [{"role": role, "content": content} for role, content in rows]

    def clear_history(self, user_id: int) -> int:
        """Delete all stored messages of a user.

        Args:
            user_id: Telegram user id.

        Returns:
            int: Number of deleted rows (0 on error; failure is logged).
        """
        try:
            conn = self._connect()
            try:
                cursor = conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.exception("Failed to clear history for user %s", user_id)
            return 0
        deleted = cursor.rowcount
        logger.info("Cleared %s message(s) for user %s", deleted, user_id)
        return deleted
