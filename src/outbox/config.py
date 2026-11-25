"""Runtime configuration, read from environment variables.

Every value has a safe local default so `uv run outbox` works with no setup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _senders(raw: str) -> dict[str, str]:
    """Parse `id=Name <addr>;id2=addr2` into a sender-id map."""
    senders: dict[str, str] = {}
    for item in raw.split(";"):
        if "=" not in item:
            continue
        sender_id, _, address = item.partition("=")
        if sender_id.strip() and address.strip():
            senders[sender_id.strip()] = address.strip()
    return senders


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8025
    # Empty means "accept any non-empty Bearer token".
    api_key: str = ""
    # Strict mode answers like Relay for unknown templates/versions (404).
    # Lenient mode (default) renders a fallback message so nothing is lost.
    strict: bool = False
    default_from: str = "Outbox <outbox@example.com>"
    senders: dict[str, str] = field(default_factory=dict)
    # Optional "mailed-by" domain; when it differs from the From domain the
    # reading view shows Gmail's "via <domain>" line.
    mailed_by: str = ""
    # Optional client callbacks (delivery.accepted / delivery.delivered).
    callback_url: str = ""
    callback_secret: str = ""
    callback_delay_seconds: float = 2.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "outbox.sqlite3"


def from_env() -> Settings:
    return Settings(
        data_dir=Path(os.getenv("OUTBOX_DATA_DIR", "data")),
        host=os.getenv("OUTBOX_HOST", "127.0.0.1"),
        port=int(os.getenv("OUTBOX_PORT", "8025")),
        api_key=os.getenv("OUTBOX_API_KEY", ""),
        strict=_flag("OUTBOX_STRICT"),
        default_from=os.getenv("OUTBOX_DEFAULT_FROM", "Outbox <outbox@example.com>"),
        senders=_senders(os.getenv("OUTBOX_SENDERS", "")),
        mailed_by=os.getenv("OUTBOX_MAILED_BY", ""),
        callback_url=os.getenv("OUTBOX_CALLBACK_URL", ""),
        callback_secret=os.getenv("OUTBOX_CALLBACK_SECRET", ""),
        callback_delay_seconds=float(os.getenv("OUTBOX_CALLBACK_DELAY_SECONDS", "2")),
    )
