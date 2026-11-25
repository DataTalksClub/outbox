"""Emulate how Gmail displays an HTML email.

The rules come from caniemail.com's Gmail data (snapshot 2026-09-16, CC BY
4.0, https://www.caniemail.com/clients/gmail/) plus long-documented Gmail
behaviour (102 KB clipping, `<style>` limited to `<head>` and 16 KB, class and
id prefixing, forced `target="_blank"`). See the README for which behaviours
are exact and which are approximations.

`sanitize(html, mode=...)` returns an HTML fragment (no `<html>`/`<body>`)
plus notes describing what was changed. `document(...)` wraps it in the page
the reading view loads into its sandboxed iframe.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

CLIP_BYTES = 102 * 1024
STYLE_LIMIT_BYTES = 16 * 1024

MODES = ("desktop", "mobile", "dark")

# Elements removed together with everything inside them.
DROP_WITH_CONTENT = {
    "script", "noscript", "title", "object", "embed", "applet", "iframe", "frame",
    "frameset", "svg", "math", "audio", "template", "canvas", "portal",
}
# Void-ish elements dropped on sight.
DROP_VOID = {"link", "meta", "base", "param"}
# caniemail: <video>, <picture>, <source>, <track> become <u></u>; the
# fallback content inside (for example the <img> in a <picture>) survives.
REPLACE_WITH_U = {"video", "picture", "source", "track"}
# Unsupported elements whose content Gmail keeps (tags dropped).
UNWRAP = {
    "html", "head", "dialog", "meter", "progress", "bdi", "marquee", "details",
    "summary", "main", "nav", "ruby", "rt", "rp",
}
MOBILE_UNWRAP_EXTRA = {"wbr"}
VOID_ELEMENTS = {
    "area", "br", "col", "hr", "img", "input", "wbr", "source", "track", "link",
    "meta", "base", "param", "embed",
}
DROP_ATTRIBUTES = {
    "srcset", "sizes", "loading", "hidden", "inert", "popover", "popovertarget",
    "command", "commandfor", "nonce", "integrity", "crossorigin", "autofocus",
    "contenteditable", "draggable", "tabindex", "accesskey",
}

# CSS properties Gmail (web, Google account) removes. caniemail "n".
UNSUPPORTED_PROPERTIES = {
    "accent-color", "align-items", "aspect-ratio", "backdrop-filter", "block-size",
    "inline-size", "box-shadow", "clip-path", "color-scheme", "cursor", "filter",
    "flex-direction", "flex-wrap", "hyphens", "hyphenate-character",
    "hyphenate-limit-chars", "inset", "justify-content", "left", "right", "top",
    "bottom", "list-style-image", "max-block-size", "max-inline-size",
    "min-block-size", "min-inline-size", "orphans", "widows", "outline-offset",
    "overflow-wrap", "position", "resize", "shape-margin", "shape-outside",
    "tab-size", "text-decoration-skip-ink", "text-decoration-thickness",
    "text-emphasis-position", "text-shadow", "text-underline-offset", "text-wrap",
    "user-select", "visibility", "white-space-collapse", "z-index", "gap", "row-gap",
}
UNSUPPORTED_PREFIXES = (
    "animation", "transition", "transform", "border-image", "border-inline",
    "border-block", "border-start-", "border-end-", "margin-inline", "margin-block",
    "padding-inline", "padding-block", "mask", "scroll-snap", "grid-template",
    "inset-", "-webkit-transform", "-webkit-animation", "-webkit-transition",
    "-webkit-mask", "-webkit-filter",
)
# Gmail Android/iOS app (Google account) additionally drops these.
MOBILE_UNSUPPORTED_PROPERTIES = {"text-emphasis", "font-stretch"}

UNSUPPORTED_AT_RULES = {
    "import", "font-face", "keyframes", "-webkit-keyframes", "supports", "charset",
    "namespace", "page", "layer", "container", "property", "counter-style",
}
UNSUPPORTED_MEDIA_FEATURES = re.compile(
    r"prefers-color-scheme|prefers-reduced-motion|hover|pointer|device-pixel-ratio|"
    r"resolution|(?:min-|max-)?(?:device-)?height|aspect-ratio",
    re.IGNORECASE,
)
MODERN_COLOR = re.compile(
    r"\b(?:oklch|oklab|lab|lch|hwb|color|color-mix|light-dark)\s*\(|"
    r"\b(?:rgba?|hsla?)\(\s*[\d.]+%?\s+[\d.]+%?\s+[\d.]+%?",
    re.IGNORECASE,
)
PSEUDO = re.compile(r"::?([a-zA-Z-]+)")
ATTRIBUTE_SELECTOR = re.compile(r"\[([^\]]*)\]")
CLASS_SELECTOR = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
ID_SELECTOR = re.compile(r"#(-?[_a-zA-Z][\w-]*)")
NEGATIVE_LENGTH = re.compile(r"(?<![\w.])-\d")


@dataclass
class Result:
    html: str
    styles: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clipped: bool = False


def _note(notes: list[str], text: str) -> None:
    if text not in notes:
        notes.append(text)


def clean_declarations(
    css: str, *, mobile: bool, notes: list[str], inline: bool
) -> str | None:
    """Filter a declaration list. `None` means "drop the whole style attribute"."""
    if inline and MODERN_COLOR.search(css):
        # caniemail css-modern-color / css-rgb: in a style attribute this
        # syntax makes Gmail remove every inline style on the element.
        _note(notes, "modern colour syntax in a style attribute removed all its inline styles")
        return None
    kept = []
    for declaration in _split_declarations(css):
        name, _, value = declaration.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if not name or not value:
            continue
        reason = _unsupported(name, value, mobile=mobile)
        if reason:
            _note(notes, reason)
            continue
        if not inline and MODERN_COLOR.search(value):
            _note(notes, "modern colour syntax removed")
            continue
        if name == "background-position" and "," in value:
            value = value.replace(",", " ")
            _note(notes, "multiple background-position values merged (comma removed)")
        kept.append(f"{name}:{value}")
    return ";".join(kept)


def _unsupported(name: str, value: str, *, mobile: bool) -> str:
    lowered = value.lower()
    if name.startswith("--"):
        return "CSS custom property declarations removed"
    if name in UNSUPPORTED_PROPERTIES or name.startswith(UNSUPPORTED_PREFIXES):
        return f"unsupported CSS property removed: {name}"
    if mobile and name in MOBILE_UNSUPPORTED_PROPERTIES:
        return f"unsupported CSS property removed (Gmail app): {name}"
    if "!important" in lowered and "!important" not in value:
        return "!important must be lowercase; declaration removed"
    if name.startswith("margin") and NEGATIVE_LENGTH.search(value):
        return "negative margin removed"
    if name == "text-indent" and NEGATIVE_LENGTH.search(value):
        return "negative text-indent removed"
    if name == "list-style" and "url(" in lowered:
        return "list-style with an image removed"
    if mobile and name == "white-space" and lowered.startswith("pre"):
        return "white-space: pre removed (Gmail app)"
    if "expression(" in lowered or "javascript:" in lowered:
        return "script in CSS removed"
    return ""


def _split_declarations(css: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in css:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == ";" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part for part in parts if part.strip()]


class _CssError(ValueError):
    pass


def _parse_blocks(css: str) -> list[tuple[str, str]]:
    """Split a stylesheet into (prelude, body) pairs; raises on bad braces."""
    blocks = []
    index, length = 0, len(css)
    while index < length:
        start = css.find("{", index)
        semicolon = css.find(";", index)
        if semicolon != -1 and (start == -1 or semicolon < start):
            # A statement at-rule such as @import url(...);
            blocks.append((css[index:semicolon].strip(), None))
            index = semicolon + 1
            continue
        if start == -1:
            if css[index:].strip():
                raise _CssError("trailing text")
            break
        depth, cursor = 1, start + 1
        while cursor < length and depth:
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth:
            raise _CssError("unbalanced braces")
        prelude = css[index:start].strip()
        if "}" in prelude:
            raise _CssError("unbalanced braces")
        blocks.append((prelude, css[start + 1 : cursor - 1]))
        index = cursor
    return blocks


def clean_stylesheet(css: str, *, prefix: str, mobile: bool, notes: list[str]) -> str | None:
    """Return the stylesheet as Gmail would keep it, or None to drop it."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    try:
        blocks = _parse_blocks(css)
    except _CssError:
        # Gmail drops the whole <style> element when it cannot parse it.
        _note(notes, "a <style> block with a CSS syntax error was removed entirely")
        return None
    return "\n".join(_clean_blocks(blocks, prefix=prefix, mobile=mobile, notes=notes))


def _clean_blocks(blocks, *, prefix: str, mobile: bool, notes: list[str]) -> list[str]:
    out = []
    for prelude, body in blocks:
        if prelude.startswith("@"):
            name = prelude[1:].split(None, 1)[0].split("(", 1)[0].lower()
            if name in UNSUPPORTED_AT_RULES:
                _note(notes, f"@{name} removed")
                continue
            if name == "media" and body is not None:
                query = prelude[len("@media") :]
                if UNSUPPORTED_MEDIA_FEATURES.search(query):
                    _note(notes, "@media query with an unsupported feature removed")
                    continue
                try:
                    inner = _parse_blocks(body)
                except _CssError:
                    continue
                cleaned = _clean_blocks(inner, prefix=prefix, mobile=mobile, notes=notes)
                if cleaned:
                    out.append(f"@media{query}{{{''.join(cleaned)}}}")
                continue
            _note(notes, f"@{name} removed")
            continue
        if body is None:
            continue
        if "{" in body:
            _note(notes, "CSS nesting removed")
            continue
        selectors = []
        for selector in prelude.split(","):
            rewritten = _rewrite_selector(selector.strip(), prefix=prefix, mobile=mobile, notes=notes)
            if rewritten:
                selectors.append(rewritten)
        declarations = clean_declarations(body, mobile=mobile, notes=notes, inline=False)
        if selectors and declarations:
            out.append(f"{','.join(selectors)}{{{declarations}}}")
    return out


def _rewrite_selector(selector: str, *, prefix: str, mobile: bool, notes: list[str]) -> str:
    if not selector:
        return ""
    for match in PSEUDO.finditer(selector):
        name = match.group(1).lower()
        if match.group(0).startswith("::") or name not in {"hover"} or mobile:
            _note(notes, f"rule with an unsupported pseudo-class/element removed: {match.group(0)}")
            return ""
    for match in ATTRIBUTE_SELECTOR.finditer(selector):
        if not re.fullmatch(r"\s*class\s*~=\s*['\"]?[\w-]+['\"]?\s*", match.group(1)):
            _note(notes, "rule with an unsupported attribute selector removed")
            return ""
    selector = CLASS_SELECTOR.sub(lambda m: f".{prefix}{m.group(1)}", selector)
    selector = ID_SELECTOR.sub(lambda m: f"#{prefix}{m.group(1)}", selector)
    selector = re.sub(
        r"\[\s*class\s*~=\s*['\"]?([\w-]+)['\"]?\s*\]",
        lambda m: f'[class~="{prefix}{m.group(1)}"]',
        selector,
    )
    # Gmail scopes every rule to the message container; `html`/`body`
    # selectors land on the container that replaced <body>.
    parts = selector.split()
    if parts and parts[0].lower() in {"html", "body", ":root"}:
        parts[0] = ".gmail-body"
        if len(parts) > 1 and parts[1].lower() == "body":
            parts.pop(1)
    return ".gmail-message " + " ".join(parts)


class _Sanitizer(HTMLParser):
    def __init__(self, *, prefix: str, mobile: bool, notes: list[str], clip_url: str):
        super().__init__(convert_charrefs=False)
        self.prefix = prefix
        self.mobile = mobile
        self.notes = notes
        self.clip_url = clip_url
        self.out: list[str] = []
        self.styles: list[str] = []
        self.skip_depth = 0
        self.skip_tag = ""
        self.in_head = False
        self.in_style = False
        self.style_in_head = False
        self.style_text: list[str] = []
        self.source_bytes = 0
        self.clipped = False
        self.open_tags: list[str] = []

    # Clipping: Gmail clips once the message source passes 102 KB.
    def _consume(self, text: str) -> bool:
        if self.clipped:
            return False
        self.source_bytes += len(text.encode("utf-8"))
        if self.source_bytes > CLIP_BYTES:
            self.clipped = True
            return False
        return True

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text() or ""
        if not self._consume(raw):
            return
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        raw = self.get_starttag_text() or ""
        if not self._consume(raw):
            return
        self._start(tag, attrs, self_closing=True)

    def _start(self, tag, attrs, *, self_closing):
        if self.skip_depth:
            if tag == self.skip_tag and not self_closing:
                self.skip_depth += 1
            return
        if tag == "head":
            self.in_head = True
            return
        if tag == "style":
            self.in_style = True
            self.style_in_head = self.in_head
            self.style_text = []
            return
        if tag in DROP_WITH_CONTENT:
            _note(self.notes, f"<{tag}> removed")
            if not self_closing and tag not in VOID_ELEMENTS:
                self.skip_depth, self.skip_tag = 1, tag
            return
        if self.in_head:
            return
        if tag in DROP_VOID:
            _note(self.notes, f"<{tag}> removed")
            return
        if tag in REPLACE_WITH_U:
            _note(self.notes, f"<{tag}> replaced by <u></u>")
            self.out.append("<u></u>" if tag in {"source", "track"} else "<u>")
            return
        if tag in UNWRAP or (self.mobile and tag in MOBILE_UNWRAP_EXTRA):
            if tag not in {"html", "head"}:
                _note(self.notes, f"<{tag}> is not supported; its content is kept")
            return
        if tag == "body":
            tag = "div"
            attrs = self._body_attrs(attrs)
        attrs = self._clean_attrs(tag, attrs)
        rendered = "".join(
            f' {name}="{html_lib.escape(value, quote=True)}"' if value is not None else f" {name}"
            for name, value in attrs
        )
        self.out.append(f"<{tag}{rendered}>")
        if tag not in VOID_ELEMENTS and not self_closing:
            self.open_tags.append(tag)

    def _body_attrs(self, attrs):
        # caniemail html-body: <body> becomes a <div> with supported attributes.
        result = [("class", "gmail-body")]
        style = ""
        for name, value in attrs:
            name = name.lower()
            if name == "bgcolor" and value:
                style += f"background-color:{value};"
            elif name == "style" and value:
                style += value
            elif name in {"dir", "lang"}:
                result.append((name, value))
        if style:
            result.append(("style", style))
        return result

    def _clean_attrs(self, tag, attrs):
        cleaned = []
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on"):
                _note(self.notes, "event handler attributes removed")
                continue
            if name in DROP_ATTRIBUTES:
                _note(self.notes, f"{name} attribute removed")
                continue
            if name == "class" and value is not None:
                if "gmail-body" in value.split():
                    cleaned.append((name, value))
                    continue
                value = " ".join(f"{self.prefix}{item}" for item in value.split())
            elif name == "id" and value:
                value = f"{self.prefix}{value}"
            elif name == "style" and value is not None:
                value = clean_declarations(value, mobile=self.mobile, notes=self.notes, inline=True)
                if not value:
                    continue
            elif name in {"href", "src", "action", "background", "poster", "formaction"} and value:
                lowered = value.strip().lower()
                if lowered.startswith(("javascript:", "vbscript:")):
                    _note(self.notes, "javascript: URL removed")
                    continue
                if tag == "img" and name == "src" and lowered.startswith("data:"):
                    _note(self.notes, "base64 (data:) image removed")
                    continue
            if tag == "a" and name in {"target", "rel"}:
                continue
            cleaned.append((name, value))
        if tag == "a" and any(name == "href" for name, _ in cleaned):
            # caniemail html-target: target="_blank" is forced on all links.
            cleaned += [("target", "_blank"), ("rel", "noopener noreferrer")]
        return cleaned

    def handle_endtag(self, tag):
        if not self._consume(f"</{tag}>"):
            return
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        if tag == "head":
            self.in_head = False
            return
        if tag == "style" and self.in_style:
            self.in_style = False
            self._finish_style()
            return
        if self.in_head:
            return
        if tag in REPLACE_WITH_U and tag not in {"source", "track"}:
            self.out.append("</u>")
            return
        if tag in UNWRAP or tag in DROP_VOID or tag in DROP_WITH_CONTENT:
            return
        if self.mobile and tag in MOBILE_UNWRAP_EXTRA:
            return
        if tag == "body":
            tag = "div"
        if tag in self.open_tags:
            while self.open_tags:
                last = self.open_tags.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break

    def _finish_style(self):
        css = "".join(self.style_text)
        if not self.style_in_head:
            # caniemail html-style: not supported inside <body>.
            _note(self.notes, "<style> inside <body> removed")
            return
        if len(css.encode("utf-8")) > STYLE_LIMIT_BYTES:
            _note(self.notes, "<style> block over 16 KB removed")
            return
        cleaned = clean_stylesheet(css, prefix=self.prefix, mobile=self.mobile, notes=self.notes)
        if cleaned:
            self.styles.append(cleaned)

    def handle_data(self, data):
        if not self._consume(data):
            return
        if self.skip_depth:
            return
        if self.in_style:
            self.style_text.append(data)
            return
        if self.in_head:
            return
        self.out.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")

    def handle_comment(self, data):
        self._consume(f"<!--{data}-->")

    def finish(self) -> str:
        if self.clipped:
            _note(self.notes, "message clipped at 102 KB")
        while self.open_tags:
            self.out.append(f"</{self.open_tags.pop()}>")
        body = "".join(self.out)
        if self.clipped:
            body += (
                '<div class="gmail-clip">[Message clipped]&nbsp;&nbsp;'
                f'<a href="{html_lib.escape(self.clip_url)}" target="_blank" rel="noopener">'
                "View entire message</a></div>"
            )
        return body


def class_prefix(seed: str) -> str:
    digest = int(hashlib.sha1(seed.encode()).hexdigest()[:12], 16)
    return f"m_{digest}_"


def sanitize(source: str, *, mode: str = "desktop", seed: str = "0", clip_url: str = "#") -> Result:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    notes: list[str] = []
    parser = _Sanitizer(
        prefix=class_prefix(seed), mobile=mode != "desktop", notes=notes, clip_url=clip_url
    )
    parser.feed(source or "")
    parser.close()
    body = parser.finish()
    return Result(html=body, styles=parser.styles, notes=notes, clipped=parser.clipped)


# Gmail's own reading-pane styles around the message body: the `.a3s`
# container uses Arial at `font-size: small`, links are #1155cc, and
# `word-wrap: break-word` applies to everything.
BASE_CSS = """
html,body{margin:0;padding:0;background:#fff;}
.gmail-message{font-family:Arial,Helvetica,sans-serif;font-size:small;color:#222;
  word-wrap:break-word;overflow-wrap:break-word;padding:0 2px;}
.gmail-message a[href]{color:#1155cc;}
.gmail-message img{border:0;}
.gmail-clip{margin:16px 0;font-family:Arial,Helvetica,sans-serif;font-size:small;color:#222;}
.gmail-clip a{color:#1155cc;}
"""
MOBILE_CSS = """
.gmail-message{font-family:Roboto,Arial,sans-serif;font-size:14px;}
"""
# Approximation of the Gmail apps' dark theme, which inverts light emails.
DARK_CSS = """
html,body{background:#121212;}
.gmail-message{filter:invert(1) hue-rotate(180deg);background:#fff;}
.gmail-message img,.gmail-message [style*="background-image"]{filter:invert(1) hue-rotate(180deg);}
"""


def document(result: Result, *, mode: str) -> str:
    css = BASE_CSS
    if mode in {"mobile", "dark"}:
        css += MOBILE_CSS
    if mode == "dark":
        css += DARK_CSS
    styles = "".join(f"<style>{style}</style>" for style in result.styles)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{css}</style>{styles}</head>"
        f'<body><div class="gmail-message">{result.html}</div></body></html>'
    )
