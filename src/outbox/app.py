"""HTTP application: the Relay-compatible API, the web UI and a JSON API."""

from __future__ import annotations

import email.utils
import html
import re
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from outbox import callbacks, gmail
from outbox.config import Settings, from_env
from outbox.render import missing_context, render_fallback, render_source
from outbox.store import Store

TEMPLATE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")
URL = re.compile(r"https?://[^\s<>\"']+")
VIEW_MODES = ("desktop", "mobile", "dark", "raw")


class ApiError(Exception):
    def __init__(self, fields: dict, status: int = 400, code: str = "validation_error"):
        self.fields = fields
        self.status = status
        self.code = code


def _error(code: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, **extra}}, status_code=status)


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or from_env()
    store = Store(settings.db_path)
    jinja = Environment(
        loader=PackageLoader("outbox", "templates"), autoescape=select_autoescape(["html"])
    )
    jinja.filters["linkify"] = _linkify
    jinja.filters["when"] = _when

    def authorized(request: Request) -> JSONResponse | None:
        header = request.headers.get("authorization", "")
        if not header:
            return _error("missing_authorization", 401,
                          message="Authentication credentials were not accepted.")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return _error("invalid_authorization", 401,
                          message="Authentication credentials were not accepted.")
        if settings.api_key and token.strip() != settings.api_key:
            return _error("invalid_api_key", 401,
                          message="Authentication credentials were not accepted.")
        return None

    def api(handler):
        async def endpoint(request: Request):
            denied = authorized(request)
            if denied is not None:
                return denied
            payload = None
            if request.method in {"POST", "PUT"}:
                try:
                    payload = await request.json() if await request.body() else {}
                except ValueError:
                    return _error("invalid_json", 400)
                if not isinstance(payload, dict):
                    return _error("invalid_json", 400)
            try:
                return handler(request, payload)
            except ApiError as exc:
                return JSONResponse(
                    {"error": {"code": exc.code, "fields": exc.fields}}, status_code=exc.status
                )

        return endpoint

    # Relay: templates ------------------------------------------------------

    def template_key(request: Request) -> str:
        key = request.path_params["key"]
        if not TEMPLATE_KEY.fullmatch(key):
            raise ApiError({"template_key": "invalid"}, 404)
        return key

    def list_templates(request, payload):
        return JSONResponse({"templates": store.list_templates()})

    def template_item(request, payload):
        key = template_key(request)
        if request.method == "GET":
            template = store.get_template(key)
            if template is None:
                raise ApiError({"template_key": "not_found"}, 404)
            return JSONResponse({"template": template})
        errors = {}
        if not isinstance(payload.get("subject", ""), str):
            errors["subject"] = "must_be_string"
        if not isinstance(payload.get("required_context", []), list):
            errors["required_context"] = "must_be_list"
        if errors:
            raise ApiError(errors)
        template, created = store.put_template(key, payload)
        return JSONResponse({"template": template}, status_code=201 if created else 200)

    def publish(request, payload):
        key = template_key(request)
        version = store.publish(key)
        if version is None:
            raise ApiError({"template_key": "not_found"}, 404)
        return JSONResponse(
            {"template_key": key, "latest_version": version["version"], "version": version},
            status_code=201,
        )

    def versions(request, payload):
        key = template_key(request)
        if store.get_template(key) is None:
            raise ApiError({"template_key": "not_found"}, 404)
        rows = store.list_versions(key)
        return JSONResponse(
            {
                "template_key": key,
                "latest_version": rows[0]["version"] if rows else None,
                "versions": rows,
            }
        )

    def resolve(key: str, version, *, draft: bool = False):
        """Return (source, version_number, published, warnings) like Relay."""
        template = store.get_template(key)
        if template is None:
            if settings.strict:
                raise ApiError({"template_key": "not_found"}, 404)
            return None, version or 1, False, ["template_not_found"]
        if draft:
            return template, template["latest_version"] or 1, False, []
        if version is not None:
            row = store.get_version(key, version)
            if row is not None:
                return row, version, True, []
            if settings.strict:
                raise ApiError({"template_version": "not_found"}, 404)
            warnings = ["template_version_not_found"]
        else:
            warnings = []
        latest = template["latest_version"]
        if latest:
            return store.get_version(key, latest), version or latest, True, warnings
        return template, version or 1, False, warnings

    def render_for(key: str, source, context: dict, warnings: list[str]) -> dict:
        if source is None:
            return render_fallback(key, context, "no such template")
        rendered = render_source(source, context)
        if warnings:
            rendered["warnings"] = warnings
        return rendered

    def preview(request, payload):
        key = template_key(request)
        context = payload.get("context") or {}
        if not isinstance(context, dict):
            raise ApiError({"context": "must_be_object"})
        draft = payload.get("draft") is True
        version = _version_value(payload.get("template_version"))
        if draft and version is not None:
            raise ApiError({"template_version": "mutually_exclusive_with_draft"})
        source, number, published, warnings = resolve(key, version, draft=draft)
        rendered = render_for(key, source, context, warnings)
        return JSONResponse(
            {
                "template_key": key,
                "template_version": number,
                "published": published,
                "subject": rendered["subject"],
                "html_body": rendered["html_body"],
                "text_body": rendered["text_body"],
                "missing_context": missing_context(
                    (source or {}).get("required_context"), context
                ),
            }
        )

    def test_send(request, payload):
        key = template_key(request)
        data = dict(payload, template_key=key, idempotency_key="")
        data.setdefault("metadata", {})
        data["metadata"] = dict(data["metadata"] or {}, test_send=True)
        return accept(data)

    # Relay: send -------------------------------------------------------------

    def send(request, payload):
        return accept(payload)

    def accept(payload: dict) -> JSONResponse:
        data = validate_send(payload)
        existing = store.find_by_idempotency_key(data["idempotency_key"])
        if existing is not None:
            return JSONResponse(
                send_response(existing, replay=True, enqueued=False), status_code=200
            )
        source, number, _, warnings = resolve(data["template_key"], data["template_version"])
        if source is not None:
            missing = missing_context(source.get("required_context"), data["context"])
            if missing:
                raise ApiError({f"context.{name}": "required" for name in missing})
        rendered = render_for(data["template_key"], source, data["context"], warnings)
        sender_id, sender = resolve_sender(data["from_email"])
        message = store.add_message(
            {
                "status": "queued",
                "email": data["email"],
                "from_email_id": sender_id,
                "from_email": sender,
                "reply_to": data["reply_to"],
                "cc": data["cc"],
                "bcc": data["bcc"],
                "subject": rendered["subject"],
                "html_body": rendered["html_body"],
                "text_body": rendered["text_body"],
                "template_key": data["template_key"],
                "template_version": number,
                "idempotency_key": data["idempotency_key"],
                "context": data["context"],
                "headers": data["headers"],
                "metadata": data["metadata"],
                "warnings": rendered.get("warnings", warnings if source is None else []),
            }
        )
        callbacks.announce(settings, store, message)
        return JSONResponse(send_response(message, replay=False, enqueued=True), status_code=202)

    def resolve_sender(sender_id: str) -> tuple[str, str]:
        if not sender_id:
            return "", settings.default_from
        if sender_id in settings.senders:
            return sender_id, settings.senders[sender_id]
        if "@" in sender_id:
            return sender_id, sender_id
        name, address = email.utils.parseaddr(settings.default_from)
        domain = address.partition("@")[2] or "example.com"
        return sender_id, email.utils.formataddr((name, f"{sender_id}@{domain}"))

    def message_status(request, payload):
        message = store.get_message(_int(request.path_params["message_id"]))
        if message is None:
            raise ApiError({"message_id": "not_found"}, 404)
        return JSONResponse(
            {
                "message": send_response(message, replay=False, enqueued=False)["message"],
                "events": [{"type": "queued"}, {"type": message["status"]}],
            }
        )

    def messages_since(request, payload):
        since = request.query_params.get("since", "")
        try:
            parsed = datetime.fromisoformat(since)
        except ValueError:
            raise ApiError({"since": "invalid"}) from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        rows = store.messages_since(parsed.astimezone(UTC).isoformat())
        return JSONResponse(
            {
                "messages": [
                    {
                        "id": str(row["id"]),
                        "client_reference": row["idempotency_key"],
                        "status": row["status"],
                        "template_key": row["template_key"],
                        "template_version": row["template_version"],
                        "reason_code": "",
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
            }
        )

    # Relay: contacts (accepted and recorded, no policy) ---------------------

    def upsert_contact(request, payload):
        scope = _scope(payload)
        current = store.get_contact(scope["email"]) or {}
        document = {**current, **{k: v for k, v in payload.items() if k != "email"}}
        document.pop("id", None)
        contact_id = store.save_contact(scope["email"], document)
        return JSONResponse(contact_document(contact_id, scope, document, tags=True))

    def contact_status(request, payload):
        scope = _scope(dict(request.query_params))
        current = store.get_contact(scope["email"])
        if current is None:
            return JSONResponse(contact_document(None, scope, {}, tags=False))
        contact_id = current.pop("id")
        return JSONResponse(contact_document(contact_id, scope, current, tags=False))

    # Health ----------------------------------------------------------------------

    async def ready(request):
        return JSONResponse({"status": "ready", "service": "relay"})

    # Web UI ----------------------------------------------------------------------

    def page(name: str, **context) -> HTMLResponse:
        return HTMLResponse(jinja.get_template(name).render(**context))

    async def index(request: Request):
        to = request.query_params.get("to", "")
        return page("index.html", messages=store.list_messages(to=to), to=to)

    async def detail(request: Request):
        message = store.get_message(_int(request.path_params["message_id"]))
        if message is None:
            return HTMLResponse("Message not found", status_code=404)
        mode = request.query_params.get("view", "desktop")
        if mode not in VIEW_MODES:
            mode = "desktop"
        notes = []
        if mode != "raw":
            notes = gmail.sanitize(message["html_body"], mode=mode, seed=str(message["id"])).notes
        name, address = email.utils.parseaddr(message["from_email"])
        domain = address.partition("@")[2].lower()
        via = settings.mailed_by if settings.mailed_by and settings.mailed_by.lower() != domain else ""
        return page(
            "message.html",
            message=message,
            mode=mode,
            modes=VIEW_MODES,
            notes=notes,
            sender_name=name or address,
            sender_address=address,
            mailed_by=settings.mailed_by or domain,
            via=via,
            headers=message_headers(message),
            html_size=len(message["html_body"].encode()),
        )

    async def body(request: Request):
        message = store.get_message(_int(request.path_params["message_id"]))
        if message is None:
            return HTMLResponse("Message not found", status_code=404)
        mode = request.query_params.get("view", "desktop")
        headers = {
            # The body is untrusted: no scripts, no external fonts, no frames.
            "Content-Security-Policy": (
                "default-src 'none'; img-src * data:; style-src 'unsafe-inline'; "
                "font-src 'none'; form-action 'none'; frame-ancestors 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        }
        if mode == "raw":
            return HTMLResponse(message["html_body"], headers=headers)
        if mode not in gmail.MODES:
            mode = "desktop"
        clip_url = f"/messages/{message['id']}/body?view=raw"
        result = gmail.sanitize(
            message["html_body"], mode=mode, seed=str(message["id"]), clip_url=clip_url
        )
        return HTMLResponse(gmail.document(result, mode=mode), headers=headers)

    async def clear(request: Request):
        store.clear_messages()
        return RedirectResponse("/", status_code=303)

    async def delete_one(request: Request):
        store.delete_message(_int(request.path_params["message_id"]))
        return RedirectResponse("/", status_code=303)

    # JSON API for tests -----------------------------------------------------------

    async def api_messages(request: Request):
        if request.method == "DELETE":
            return JSONResponse({"deleted": store.clear_messages()})
        to = request.query_params.get("to", "")
        limit = min(_int(request.query_params.get("limit", "200")) or 200, 1000)
        return JSONResponse({"messages": [public(m) for m in store.list_messages(to=to, limit=limit)]})

    async def api_message(request: Request):
        message_id = _int(request.path_params["message_id"])
        if request.method == "DELETE":
            return JSONResponse({"deleted": store.delete_message(message_id)})
        message = store.get_message(message_id)
        if message is None:
            return _error("not_found", 404)
        return JSONResponse(public(message, full=True))

    async def api_latest(request: Request):
        rows = store.list_messages(to=request.query_params.get("to", ""), limit=1)
        if not rows:
            return _error("not_found", 404)
        return JSONResponse(public(rows[0], full=True))

    static_dir = Path(__file__).parent / "static"
    routes = [
        Route("/", index),
        Route("/messages/{message_id}", detail),
        Route("/messages/{message_id}/body", body),
        Route("/messages/{message_id}/delete", delete_one, methods=["POST"]),
        Route("/clear", clear, methods=["POST"]),
        Route("/api/messages", api_messages, methods=["GET", "DELETE"]),
        Route("/api/messages/latest", api_latest),
        Route("/api/messages/{message_id}", api_message, methods=["GET", "DELETE"]),
        Route("/health/ready", ready),
        Route("/api/transactional/send", api(send), methods=["POST"]),
        Route("/api/transactional/messages", api(messages_since), methods=["GET"]),
        Route("/api/transactional/messages/{message_id}", api(message_status), methods=["GET"]),
        Route("/api/transactional/templates", api(list_templates), methods=["GET"]),
        Route("/api/transactional/templates/{key}", api(template_item), methods=["GET", "PUT"]),
        Route("/api/transactional/templates/{key}/publish", api(publish), methods=["POST"]),
        Route("/api/transactional/templates/{key}/versions", api(versions), methods=["GET"]),
        Route("/api/transactional/templates/{key}/preview", api(preview), methods=["POST"]),
        Route("/api/transactional/templates/{key}/test-send", api(test_send), methods=["POST"]),
        Route("/api/contacts", api(upsert_contact), methods=["POST"]),
        Route("/api/contacts/status", api(contact_status), methods=["GET"]),
        Mount("/static", StaticFiles(directory=static_dir), name="static"),
    ]
    app = Starlette(routes=routes)
    app.state.settings = settings
    app.state.store = store
    return app


def validate_send(data: dict) -> dict:
    errors = {}
    address = data.get("email")
    if not isinstance(address, str) or not address.strip():
        errors["email"] = "required"
    elif not EMAIL.fullmatch(address.strip()):
        errors["email"] = "invalid"
    key = data.get("template_key")
    if not isinstance(key, str) or not key.strip():
        errors["template_key"] = "required"
    try:
        version = _version_value(data.get("template_version"))
    except ApiError as exc:
        errors.update(exc.fields)
        version = None
    idempotency_key = data.get("idempotency_key") or ""
    if not isinstance(idempotency_key, str):
        errors["idempotency_key"] = "must_be_non_empty_string"
    context = data.get("context") or {}
    if not isinstance(context, dict):
        errors["context"] = "must_be_object"
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        errors["metadata"] = "must_be_object"
    reply_to = data.get("reply_to") or ""
    if reply_to and (not isinstance(reply_to, str) or not EMAIL.search(reply_to)):
        errors["reply_to"] = "invalid"
    lists = {}
    for name in ("cc", "bcc"):
        value = data.get(name) or []
        if not isinstance(value, list) or not all(
            isinstance(item, str) and EMAIL.search(item) for item in value
        ):
            errors[name] = "invalid"
            value = []
        lists[name] = value
    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        errors["headers"] = "must_be_object"
    from_email = data.get("from_email") or ""
    if not isinstance(from_email, str):
        errors["from_email"] = "invalid"
    if errors:
        raise ApiError(errors)
    if data.get("category"):
        metadata = dict(metadata, category=data["category"])
    if data.get("category_tag"):
        metadata = dict(metadata, category_tag=data["category_tag"])
    return {
        "email": address.strip(),
        "template_key": key.strip(),
        "template_version": version,
        "idempotency_key": idempotency_key.strip(),
        "context": context,
        "metadata": metadata,
        "reply_to": reply_to,
        "cc": lists["cc"],
        "bcc": lists["bcc"],
        "headers": {str(k): str(v) for k, v in headers.items()},
        "from_email": from_email.strip(),
    }


def send_response(message: dict, *, replay: bool, enqueued: bool) -> dict:
    return {
        "message": {
            "id": message["id"],
            "email": message["email"],
            "from_email": message["from_email_id"],
            "from_email_address": message["from_email"],
            "reply_to": message["reply_to"],
            "cc": message["cc"],
            "bcc": message["bcc"],
            "status": message["status"],
            "template_key": message["template_key"],
            "template_version": message["template_version"],
            "idempotency_key": message["idempotency_key"],
            "created_at": message["created_at"],
        },
        "idempotent_replay": replay,
        "enqueued": enqueued,
    }


def contact_document(contact_id, scope: dict, document: dict, *, tags: bool) -> dict:
    exists = contact_id is not None
    status = document.get("status") if exists else None
    verified = bool(document.get("verified")) if exists else False
    subscription = {
        "subscribed": status == "subscribed",
        "status": status,
        "verified": verified,
        "verified_at": None,
        "unsubscribed_at": None,
        "unsubscribe_reason": "",
    }
    result = {
        "contact_id": contact_id,
        "email": scope["email"],
        "exists": exists,
        "verified": verified,
        "verified_at": None,
        "email_validation": document.get("email_validation")
        or {"status": "unknown", "reason": "", "validated_at": None},
        "global_unsubscribed": False,
        "hard_bounced": False,
        "complained": False,
        "audience": {"slug": scope["audience"], **subscription},
        "client": {"slug": scope["client"], **subscription},
        "can_send_marketing": exists and verified and status == "subscribed",
        "can_send_transactional": True,
    }
    if tags:
        result["tags"] = sorted({str(tag).strip().lower() for tag in document.get("tags") or []})
    return result


def _scope(source: dict) -> dict:
    errors = {}
    for name in ("email", "audience", "client"):
        if not isinstance(source.get(name), str) or not source[name].strip():
            errors[name] = "required"
    if errors:
        raise ApiError(errors)
    return {
        "email": source["email"].strip().lower(),
        "audience": source["audience"],
        "client": source["client"],
    }


def message_headers(message: dict) -> list[tuple[str, str]]:
    created = datetime.fromisoformat(message["created_at"])
    headers = [
        ("From", message["from_email"]),
        ("To", message["email"]),
    ]
    if message["cc"]:
        headers.append(("Cc", ", ".join(message["cc"])))
    if message["bcc"]:
        headers.append(("Bcc", ", ".join(message["bcc"])))
    if message["reply_to"]:
        headers.append(("Reply-To", message["reply_to"]))
    headers += [
        ("Subject", message["subject"]),
        ("Date", email.utils.format_datetime(created)),
        ("Message-ID", f"<outbox-{message['id']}@outbox.invalid>"),
        ("MIME-Version", "1.0"),
        ("Content-Type", "multipart/alternative"),
        ("X-Relay-Template", f"{message['template_key']} v{message['template_version']}"),
    ]
    if message["idempotency_key"]:
        headers.append(("X-Relay-Idempotency-Key", message["idempotency_key"]))
    headers += list(message["headers"].items())
    return headers


def public(message: dict, *, full: bool = False) -> dict:
    data = {
        "id": message["id"],
        "created_at": message["created_at"],
        "status": message["status"],
        "to": message["email"],
        "from": message["from_email"],
        "subject": message["subject"],
        "template_key": message["template_key"],
        "template_version": message["template_version"],
        "idempotency_key": message["idempotency_key"],
        "warnings": message["warnings"],
    }
    if full:
        data.update(
            {
                "from_email_id": message["from_email_id"],
                "reply_to": message["reply_to"],
                "cc": message["cc"],
                "bcc": message["bcc"],
                "html_body": message["html_body"],
                "text_body": message["text_body"],
                "context": message["context"],
                "headers": message["headers"],
                "metadata": message["metadata"],
                "links": sorted(
                    set(URL.findall(message["text_body"] + " " + html.unescape(message["html_body"])))
                ),
            }
        )
    return data


def _version_value(value):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ApiError({"template_version": "must_be_positive_integer"})
    return value


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _linkify(text: str) -> Markup:
    escaped = str(escape(text))
    return Markup(
        URL.sub(
            lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener noreferrer">{m.group(0)}</a>',
            escaped,
        )
    )


def _when(value: str) -> str:
    stamp = datetime.fromisoformat(value).astimezone()
    return stamp.strftime("%b %-d, %Y, %-I:%M %p")
