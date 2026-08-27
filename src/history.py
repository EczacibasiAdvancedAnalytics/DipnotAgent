"""Sohbet geçmişinin yerel SQLite dosyasında saklanması.

Streamlit oturum durumu sayfa yenilendiğinde kaybolduğu için konuşmalar
`data/chats.db` içine yazılır. Yalnızca standart kütüphane kullanılır.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .models import SourceDoc

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "chats.db"

TITLE_LIMIT = 60
NEW_CHAT_TITLE = "Yeni sohbet"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    thread_id   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    position        INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    sources         TEXT,
    cited           TEXT,
    meta            TEXT,
    failed          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, position);
"""


def title_from_question(question: str, limit: int = TITLE_LIMIT) -> str:
    """Sohbet başlığını ilk sorudan üretir."""
    text = " ".join((question or "").split())
    if not text:
        return NEW_CHAT_TITLE
    if len(text) <= limit:
        return text
    return text[:limit].rstrip(" ,.;:!?-") + "…"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Conversation:
    """Yan menüde listelenen tek bir sohbet."""

    id: int
    title: str
    created_at: str
    updated_at: str
    thread_id: Optional[str] = None
    message_count: int = 0

    @property
    def updated_label(self) -> str:
        """`27.08.2026 19:40` biçiminde okunur zaman damgası."""
        try:
            return datetime.fromisoformat(self.updated_at).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return self.updated_at


def _dump_sources(sources: Optional[Sequence[SourceDoc]]) -> str:
    payload = []
    for doc in sources or []:
        payload.append(doc.to_dict() if isinstance(doc, SourceDoc) else dict(doc))
    return json.dumps(payload, ensure_ascii=False)


def _load_sources(raw: Optional[str]) -> List[SourceDoc]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Kaynaklar çözümlenemedi, boş liste kullanılıyor.")
        return []
    return [SourceDoc.from_dict(item) for item in data if isinstance(item, dict)]


def _dump_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "null"


def _load_json(raw: Optional[str], fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return fallback if value is None else value


class ChatStore:
    """Konuşmaları ve mesajları saklayan basit SQLite deposu."""

    def __init__(self, db_path: Union[str, Path, None] = None):
        self.path = Path(db_path or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Streamlit betiği farklı iş parçacıklarında çalıştırabilir.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - kapanışta hata önemli değil
            pass

    # ------------------------------------------------------------------
    # Konuşmalar
    # ------------------------------------------------------------------
    def create_conversation(self, title: str, thread_id: Optional[str] = None) -> int:
        stamp = _now()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO conversations (title, created_at, updated_at, thread_id)"
                " VALUES (?, ?, ?, ?)",
                (title or NEW_CHAT_TITLE, stamp, stamp, thread_id),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_conversations(self, limit: int = 50) -> List[Conversation]:
        """En son güncellenen sohbet başta olacak şekilde listeler."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at, c.thread_id,"
                "       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS n"
                "  FROM conversations c"
                " ORDER BY c.updated_at DESC, c.id DESC"
                " LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Conversation(
                id=int(row["id"]),
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                thread_id=row["thread_id"],
                message_count=int(row["n"] or 0),
            )
            for row in rows
        ]

    def get_conversation(self, conversation_id: int) -> Optional[Conversation]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, created_at, updated_at, thread_id FROM conversations"
                " WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return Conversation(
            id=int(row["id"]),
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            thread_id=row["thread_id"],
        )

    def rename_conversation(self, conversation_id: int, title: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title or NEW_CHAT_TITLE, _now(), conversation_id),
            )
            self._conn.commit()

    def set_thread_id(self, conversation_id: int, thread_id: Optional[str]) -> None:
        """Foundry motorunda sohbete ait thread'i saklar."""
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET thread_id = ? WHERE id = ?",
                (thread_id, conversation_id),
            )
            self._conn.commit()

    def get_thread_id(self, conversation_id: int) -> Optional[str]:
        conversation = self.get_conversation(conversation_id)
        return conversation.thread_id if conversation else None

    def delete_conversation(self, conversation_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            )
            self._conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Mesajlar
    # ------------------------------------------------------------------
    def append_message(self, conversation_id: int, message: Dict[str, Any]) -> int:
        """Arayüzdeki mesaj sözlüğünü olduğu gibi saklar; sıra numarasını döndürür."""
        cited = message.get("cited") or set()
        row = (
            conversation_id,
            str(message.get("role") or "user"),
            str(message.get("content") or ""),
            _dump_sources(message.get("sources")),
            _dump_json(sorted(int(value) for value in cited)),
            _dump_json(message.get("meta") or {}),
            1 if message.get("failed") else 0,
            _now(),
        )
        with self._lock:
            position = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            self._conn.execute(
                "INSERT INTO messages (conversation_id, role, content, sources, cited, meta,"
                " failed, created_at, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row + (position,),
            )
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_now(), conversation_id),
            )
            self._conn.commit()
        return position

    def load_messages(self, conversation_id: int) -> List[Dict[str, Any]]:
        """Sohbeti `st.session_state.messages` ile aynı biçimde geri yükler."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, sources, cited, meta, failed FROM messages"
                " WHERE conversation_id = ? ORDER BY position ASC, id ASC",
                (conversation_id,),
            ).fetchall()

        messages: List[Dict[str, Any]] = []
        for row in rows:
            message: Dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if row["role"] == "assistant":
                message["sources"] = _load_sources(row["sources"])
                message["cited"] = {
                    int(value) for value in _load_json(row["cited"], []) if str(value).isdigit()
                }
                message["meta"] = _load_json(row["meta"], {})
                message["failed"] = bool(row["failed"])
            messages.append(message)
        return messages


def get_store(db_path: Union[str, Path, None] = None) -> ChatStore:
    return ChatStore(db_path)
