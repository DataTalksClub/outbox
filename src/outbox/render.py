"""Render templates the way Relay does.

Relay's transactional rendering, reproduced here:

1. `subject`, `html_body`, `text_body` and `markdown_body` are Django
   templates rendered with the send context (autoescape off).
2. A Markdown body is converted to HTML (extensions `fenced_code`, `tables`,
   `attr_list`, `md_in_html`; external links get `target="_blank"` and
   `rel="noopener"`) and wrapped in a neutral HTML shell. The text part is
   the substituted Markdown source.
3. A version with `markdown_layout_context` exposes `footer_note`,
   `verify_email_url` and `unsubscribe_url` to the shell and the text part.

Relay can also wrap Markdown in a client-specific branded layout configured
inside Relay; the outbox always uses the neutral shell.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import markdown as markdown_lib
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

_engine = None

INLINE_BULLET_RE = re.compile(r"^(?P<lead>.*?:) - (?P<rest>.+)$")
LAYOUT_KEYS = ("footer_note", "verify_email_url", "unsubscribe_url")

NEUTRAL_SHELL = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{{ subject }}</title></head>
<body style="font-family: sans-serif; line-height: 1.6; color: #222;">
<div style="max-width: 600px; margin: 0 auto; padding: 24px;">
{{ body_html|safe }}
{% if footer_note %}<p style="font-size: 12px; color: #888;">{{ footer_note }}</p>{% endif %}
{% if verify_email_url %}<p style="font-size: 12px; color: #888;">Your email is not verified on our platform. <a href="{{ verify_email_url }}">Verify your email</a></p>{% endif %}
{% if unsubscribe_url %}<p style="font-size: 12px; color: #888;"><a href="{{ unsubscribe_url }}">Unsubscribe from all emails</a></p>{% endif %}
</div>
</body>
</html>"""


def engine():
    """A standalone Django template engine (no Django project needed)."""
    global _engine
    if _engine is None:
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(USE_TZ=True, USE_I18N=False, TEMPLATES=[])
            django.setup()
        from django.template import Engine

        _engine = Engine(debug=False)
    return _engine


def render_string(value: str, context: dict, *, autoescape: bool = False) -> str:
    if not value:
        return ""
    from django.template import Context

    return engine().from_string(value).render(Context(context, autoescape=autoescape))


def normalize_inline_bullets(text: str) -> str:
    if not text or " - " not in text:
        return text
    out: list[str] = []
    for line in text.split("\n"):
        match = INLINE_BULLET_RE.match(line)
        if match:
            items = [item.strip() for item in match.group("rest").split(" - ")]
            if len(items) >= 2 and all(items):
                if out and out[-1].strip():
                    out.append("")
                out.append(match.group("lead"))
                out.append("")
                out.extend(f"- {item}" for item in items)
                out.append("")
                continue
        out.append(line)
    return "\n".join(out)


class _ExternalLinks(Treeprocessor):
    def run(self, root):
        for element in root.iter("a"):
            href = (element.get("href") or "").strip()
            parsed = urlparse(href)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                continue
            if not element.get("target"):
                element.set("target", "_blank")
            rel = (element.get("rel") or "").split()
            if "noopener" not in {token.lower() for token in rel}:
                rel.append("noopener")
            element.set("rel", " ".join(rel))


class _ExternalLinksExtension(Extension):
    def extendMarkdown(self, md):
        md.treeprocessors.register(_ExternalLinks(md), "external_links", 0)


def render_markdown(text: str) -> str:
    return markdown_lib.markdown(
        normalize_inline_bullets(text or ""),
        extensions=[_ExternalLinksExtension(), "fenced_code", "tables", "attr_list", "md_in_html"],
        output_format="html",
    )


def required_names(required_context) -> list[str]:
    names = []
    for item in required_context or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def missing_context(required_context, context: dict) -> list[str]:
    return [
        name
        for name in required_names(required_context)
        if name not in context or context[name] in (None, "")
    ]


def render_source(source: dict, context: dict) -> dict:
    """Subject/html/text for a draft or published version."""
    subject = render_string(source.get("subject", ""), context)
    markdown_body = source.get("markdown_body") or ""
    if markdown_body:
        rendered_body = render_string(markdown_body, context)
        layout = {name: "" for name in LAYOUT_KEYS}
        if source.get("markdown_layout_context"):
            for name in LAYOUT_KEYS:
                value = context.get(name)
                if isinstance(value, str):
                    layout[name] = value
        html_body = render_string(
            NEUTRAL_SHELL,
            {"subject": subject, "body_html": render_markdown(rendered_body), **layout},
            autoescape=True,
        )
        sections = [rendered_body]
        if layout["footer_note"]:
            sections.append(layout["footer_note"].strip())
        if layout["verify_email_url"]:
            sections.append(
                "Your email is not verified on our platform.\n"
                f"Verify your email: {layout['verify_email_url']}"
            )
        if layout["unsubscribe_url"]:
            sections.append(f"Unsubscribe from all emails: {layout['unsubscribe_url']}")
        text_body = "\n\n".join(section for section in sections if section).strip()
        return {"subject": subject, "html_body": html_body, "text_body": text_body}
    return {
        "subject": subject,
        "html_body": render_string(source.get("html_body", ""), context),
        "text_body": render_string(source.get("text_body", ""), context),
    }


URL_RE = re.compile(r"https?://[^\s<>\"']+")


def render_fallback(template_key: str, context: dict, reason: str) -> dict:
    """A readable message for a template the outbox does not have.

    Lenient mode only: the real Relay answers 404. Every context value is
    listed, and URLs are links, so flows such as "click the confirmation
    link" stay testable without the template.
    """
    subject = str(context.get("subject") or f"[{template_key}]")
    rows_html = []
    rows_text = []
    for name in sorted(context):
        value = context[name]
        text = value if isinstance(value, str) else repr(value)
        escaped = html.escape(text)
        linked = URL_RE.sub(
            lambda match: f'<a href="{match.group(0)}" target="_blank" rel="noopener">{match.group(0)}</a>',
            escaped,
        )
        rows_html.append(
            f'<tr><th style="text-align:left;padding:4px 12px 4px 0;vertical-align:top;width:30%;">'
            f"{html.escape(name)}</th><td style=\"padding:4px 0;word-break:break-all;\">{linked}</td></tr>"
        )
        rows_text.append(f"{name}: {text}")
    body = (
        f"<p><strong>Template <code>{html.escape(template_key)}</code></strong> "
        f"is not in the outbox catalog ({html.escape(reason)}). "
        "This is the context the sender passed:</p>"
        f'<table style="border-collapse:collapse;width:100%;table-layout:fixed;">{"".join(rows_html)}</table>'
    )
    html_body = render_string(
        NEUTRAL_SHELL,
        {"subject": subject, "body_html": body, **{name: "" for name in LAYOUT_KEYS}},
        autoescape=True,
    )
    text_body = (
        f"Template {template_key} is not in the outbox catalog ({reason}).\n\n"
        + "\n".join(rows_text)
    )
    return {"subject": subject, "html_body": html_body, "text_body": text_body}
