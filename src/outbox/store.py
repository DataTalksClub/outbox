"""SQLite storage for templates, published versions, messages and contacts."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    key TEXT PRIMARY KEY,
    draft TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS template_versions (
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, version)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    email TEXT NOT NULL,
    from_email_id TEXT NOT NULL,
    from_email TEXT NOT NULL,
    reply_to TEXT NOT NULL,
    cc TEXT NOT NULL,
    bcc TEXT NOT NULL,
    subject TEXT NOT NULL,
    html_body TEXT NOT NULL,
    text_body TEXT NOT NULL,
    template_key TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    context TEXT NOT NULL,
    headers TEXT NOT NULL,
    metadata TEXT NOT NULL,
    warnings TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_idempotency ON messages (idempotency_key);
CREATE INDEX IF NOT EXISTS messages_updated ON messages (updated_at);
CREATE TABLE IF NOT EXISTS contacts (
    email TEXT PRIMARY KEY,
    id INTEGER NOT NULL,
    document TEXT NOT NULL
);
"""

JSON_FIELDS = ("cc", "bcc", "context", "headers", "metadata", "warnings")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        with self._lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    # Templates -----------------------------------------------------------

    def get_template(self, key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM templates WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            latest = db.execute(
                "SELECT MAX(version) FROM template_versions WHERE key = ?", (key,)
            ).fetchone()[0]
        return _template(row, latest)

    def list_templates(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM templates ORDER BY key").fetchall()
            latest = dict(
                db.execute(
                    "SELECT key, MAX(version) FROM template_versions GROUP BY key"
                ).fetchall()
            )
        return [_template(row, latest.get(row["key"])) for row in rows]

    def put_template(self, key: str, draft: dict) -> tuple[dict, bool]:
        stamp = now_iso()
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM templates WHERE key = ?", (key,)).fetchone()
            if exists:
                db.execute(
                    "UPDATE templates SET draft = ?, updated_at = ? WHERE key = ?",
                    (json.dumps(draft), stamp, key),
                )
            else:
                db.execute(
                    "INSERT INTO templates (key, draft, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (key, json.dumps(draft), stamp, stamp),
                )
        return self.get_template(key), not exists

    def publish(self, key: str) -> dict | None:
        template = self.get_template(key)
        if template is None:
            return None
        number = (template["latest_version"] or 0) + 1
        snapshot = {
            field: template.get(field)
            for field in (
                "subject",
                "html_body",
                "text_body",
                "markdown_body",
                "markdown_layout_context",
                "required_context",
                "category",
            )
        }
        stamp = now_iso()
        with self._connect() as db:
            db.execute(
                "INSERT INTO template_versions (key, version, snapshot, created_at)"
                " VALUES (?, ?, ?, ?)",
                (key, number, json.dumps(snapshot), stamp),
            )
        return self.get_version(key, number)

    def get_version(self, key: str, number: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM template_versions WHERE key = ? AND version = ?", (key, number)
            ).fetchone()
        return _version(row) if row else None

    def list_versions(self, key: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM template_versions WHERE key = ? ORDER BY version DESC", (key,)
            ).fetchall()
        return [_version(row) for row in rows]

    # Messages ------------------------------------------------------------

    def add_message(self, message: dict) -> dict:
        stamp = now_iso()
        row = dict(message)
        row.setdefault("created_at", stamp)
        row.setdefault("updated_at", stamp)
        for name in JSON_FIELDS:
            row[name] = json.dumps(row.get(name) or ([] if name in {"cc", "bcc", "warnings"} else {}))
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._connect() as db:
            cursor = db.execute(f"INSERT INTO messages ({columns}) VALUES ({marks})", tuple(row.values()))
            message_id = cursor.lastrowid
        return self.get_message(message_id)

    def get_message(self, message_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def find_by_idempotency_key(self, key: str) -> dict | None:
        if not key:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE idempotency_key = ? ORDER BY id LIMIT 1", (key,)
            ).fetchone()
        return _message(row) if row else None

    def list_messages(self, *, to: str = "", limit: int = 200) -> list[dict]:
        query = "SELECT * FROM messages"
        params: list = []
        if to:
            query += " WHERE lower(email) = lower(?)"
            params.append(to)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [_message(row) for row in rows]

    def messages_since(self, since: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE updated_at >= ? AND idempotency_key != ''"
                " ORDER BY updated_at, id LIMIT 1000",
                (since,),
            ).fetchall()
        return [_message(row) for row in rows]

    def set_status(self, message_id: int, status: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE messages SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), message_id),
            )

    def delete_message(self, message_id: int) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM messages WHERE id = ?", (message_id,)).rowcount > 0

    def clear_messages(self) -> int:
        with self._connect() as db:
            return db.execute("DELETE FROM messages").rowcount

    # Contacts ------------------------------------------------------------

    def get_contact(self, email: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM contacts WHERE email = ?", (email,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], **json.loads(row["document"])}

    def save_contact(self, email: str, document: dict) -> int:
        with self._connect() as db:
            row = db.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
            if row is None:
                contact_id = (db.execute("SELECT COALESCE(MAX(id), 0) FROM contacts").fetchone()[0]) + 1
                db.execute(
                    "INSERT INTO contacts (email, id, document) VALUES (?, ?, ?)",
                    (email, contact_id, json.dumps(document)),
                )
            else:
                contact_id = row["id"]
                db.execute(
                    "UPDATE contacts SET document = ? WHERE email = ?",
                    (json.dumps(document), email),
                )
        return contact_id


def _template(row: sqlite3.Row, latest: int | None) -> dict:
    draft = json.loads(row["draft"])
    return {
        "key": row["key"],
        "client": "outbox",
        "name": draft.get("name", row["key"]),
        "description": draft.get("description", ""),
        "subject": draft.get("subject", ""),
        "html_body": draft.get("html_body", ""),
        "text_body": draft.get("text_body", ""),
        "markdown_body": draft.get("markdown_body", ""),
        "markdown_layout_context": bool(draft.get("markdown_layout_context", False)),
        "category": draft.get("category", ""),
        "required_context": draft.get("required_context", []),
        "example_context": draft.get("example_context", {}),
        "default_sender_id": draft.get("default_sender_id", ""),
        "latest_version": latest,
        "is_transactional": True,
        "is_active": bool(draft.get("is_active", True)),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _version(row: sqlite3.Row) -> dict:
    return {
        "template_key": row["key"],
        "version": row["version"],
        **json.loads(row["snapshot"]),
        "created_at": row["created_at"],
    }


def _message(row: sqlite3.Row) -> dict:
    message = dict(row)
    for name in JSON_FIELDS:
        message[name] = json.loads(message[name])
    return message
