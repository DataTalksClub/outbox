import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from outbox import callbacks

TEMPLATE = {"subject": "Hello {{ name }}", "markdown_body": "Visit https://example.com/a?x=1&y=2", "required_context": []}


def send(client, auth, **context):
    client.put("/api/transactional/templates/hello", json=TEMPLATE, headers=auth)
    response = client.post(
        "/api/transactional/send",
        json={
            "email": "dev-test+1@example.com",
            "template_key": "hello",
            "idempotency_key": f"k-{time.time_ns()}",
            "context": {"name": "Ada", **context},
            "reply_to": "help@example.org",
        },
        headers=auth,
    )
    return response.json()["message"]["id"]


def test_inbox_lists_newest_first_and_clear_all(client, auth):
    first = send(client, auth)
    second = send(client, auth, name="Bob")
    page = client.get("/").text
    assert page.index(f"/messages/{second}") < page.index(f"/messages/{first}")
    assert "Hello Bob" in page and "dev-test+1@example.com" in page
    response = client.post("/clear", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/messages").json()["messages"] == []


def test_reading_view_shows_gmail_header_text_part_and_headers(client, auth):
    message_id = send(client, auth)
    page = client.get(f"/messages/{message_id}").text
    assert "Hello Ada" in page
    assert "outbox@example.com" in page
    assert "reply-to:" in page and "help@example.org" in page
    assert f'src="/messages/{message_id}/body?view=desktop"' in page
    assert 'sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"' in page
    assert '<a href="https://example.com/a?x=1&amp;y=2" target="_blank"' in page
    assert "X-Relay-Template" in page


def test_body_is_served_with_a_restrictive_csp(client, auth):
    message_id = send(client, auth)
    for view in ("desktop", "mobile", "dark", "raw"):
        response = client.get(f"/messages/{message_id}/body", params={"view": view})
        assert response.status_code == 200
        assert "default-src 'none'" in response.headers["content-security-policy"]
    assert 'class="gmail-message"' in client.get(f"/messages/{message_id}/body").text


def test_json_api_latest_and_filter(client, auth):
    message_id = send(client, auth)
    latest = client.get("/api/messages/latest", params={"to": "DEV-TEST+1@example.com"}).json()
    assert latest["id"] == message_id
    assert latest["text_body"].startswith("Visit")
    assert client.get("/api/messages", params={"to": "other@example.com"}).json()["messages"] == []
    assert client.delete(f"/api/messages/{message_id}").json() == {"deleted": True}


def test_signed_callbacks_are_sent(make_client, auth):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((dict(self.headers), body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = make_client(
            callback_url=f"http://127.0.0.1:{server.server_port}/internal/mail/callback",
            callback_secret="local-secret",
            callback_delay_seconds=0,
        )
        message_id = send(client, auth)
        for _ in range(50):
            if len(received) == 2:
                break
            time.sleep(0.05)
    finally:
        server.shutdown()
    assert [json.loads(body)["event_type"] for _, body in received] == [
        "delivery.accepted", "delivery.delivered"
    ]
    headers, body = received[1]
    expected = "sha256=" + hmac.new(
        b"local-secret", headers["X-Relay-Timestamp"].encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert headers["X-Relay-Signature"] == expected
    payload = json.loads(body)
    assert payload["message_id"] == str(message_id)
    assert payload["contract_version"] == 1 and payload["sequence"] == 2
    assert body == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert callbacks.sign(body, headers["X-Relay-Timestamp"], "local-secret") == expected
