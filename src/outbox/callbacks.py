"""Optional signed client callbacks, in Relay's contract-version-1 format.

When `OUTBOX_CALLBACK_URL` and `OUTBOX_CALLBACK_SECRET` are set, each accepted
message announces `delivery.accepted` and then `delivery.delivered` to that
URL, signed with HMAC-SHA256 over `<timestamp>.<body>` exactly like Relay.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime

logger = logging.getLogger("outbox.callbacks")

EVENT_NAMESPACE = uuid.UUID("6f1c3a52-7d1e-4c1f-9a43-0e2d6f0b7a11")


def sign(body: bytes, timestamp: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def event_payload(message: dict, event_type: str, sequence: int) -> dict:
    return {
        "client_reference": message["idempotency_key"] or None,
        "contract_version": 1,
        "event_id": str(uuid.uuid5(EVENT_NAMESPACE, f"{message['id']}:{event_type}")),
        "event_type": event_type,
        "message_id": str(message["id"]),
        "sequence": sequence,
        "template_key": message["template_key"],
        "timestamp": datetime.now(UTC).isoformat(),
    }


def post(url: str, secret: str, payload: dict, *, attempt: int = 1) -> int | None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Signature": sign(body, timestamp, secret),
            "X-Relay-Event-Id": payload["event_id"],
            "X-Relay-Event-Type": payload["event_type"],
            "X-Relay-Contract-Version": "1",
            "X-Relay-Attempt": str(attempt),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError) as error:
        logger.warning("callback failed: %s %s", payload["event_type"], error)
        return None


def announce(settings, store, message: dict) -> None:
    """Send accepted + delivered callbacks in a background thread."""
    if not settings.callback_url or not settings.callback_secret:
        store.set_status(message["id"], "delivered")
        return

    def run():
        # The sender records the provider acceptance after its HTTP call
        # returns; give it time so `delivered` is not overwritten.
        time.sleep(settings.callback_delay_seconds)
        for sequence, event_type in enumerate(("delivery.accepted", "delivery.delivered"), 1):
            status = post(settings.callback_url, settings.callback_secret,
                          event_payload(message, event_type, sequence))
            logger.info("callback %s -> %s", event_type, status)
        store.set_status(message["id"], "delivered")

    threading.Thread(target=run, daemon=True).start()
