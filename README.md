# outbox

A Relay-compatible outbox for testing email flows.

Point an application that sends transactional email through a Relay-style
HTTP API at this server instead. The outbox accepts the same requests,
renders the templates the same way, stores every message in SQLite and shows
it in a browser, the way MailHog or Mailpit do for SMTP. Nothing is ever
delivered.

- Relay transactional API: template catalog, publish, versions, preview,
  test-send, send, message status and reconciliation
- Relay contact upsert and status, accepted and recorded
- Optional signed delivery callbacks to the sender
- A web inbox with a Gmail-style reading view and Gmail rendering emulation
- A JSON API for automated tests

## Quick start

```bash
uv run outbox                 # http://127.0.0.1:8025
uv run outbox --port 9025     # or OUTBOX_PORT=9025
```

With Docker:

```bash
docker build -t outbox .
docker run --rm -p 8025:8025 -v outbox-data:/data outbox
```

Open http://127.0.0.1:8025 to see the inbox.

## Pointing an application at it

A Relay client needs a base URL and an API key:

| Client setting | Value |
|---|---|
| `RELAY_BASE_URL` | `http://127.0.0.1:8025` |
| `RELAY_API_KEY` | any non-empty string, or the value of `OUTBOX_API_KEY` if you set one |
| `RELAY_WEBHOOK_SECRET` | the same value as `OUTBOX_CALLBACK_SECRET`, only if you enable callbacks |

The client must also be switched to its Relay mail backend (for example a
`MAIL_BACKEND = "relay"` setting). Run the application's template import
against the outbox (anything that calls `PUT /api/transactional/templates/<key>`
and `POST .../publish`) to load your real templates. Sends for templates the
outbox does not have still work in the default lenient mode (see below).

## Configuration

All configuration is through environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `OUTBOX_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in the Docker image) |
| `OUTBOX_PORT` | `8025` | Port |
| `OUTBOX_DATA_DIR` | `./data` | Directory for `outbox.sqlite3` |
| `OUTBOX_API_KEY` | empty | When set, the Bearer token must equal it. When empty, any non-empty Bearer token is accepted. |
| `OUTBOX_STRICT` | `0` | `1` answers unknown templates and versions with Relay's `404` instead of rendering a fallback |
| `OUTBOX_DEFAULT_FROM` | `Outbox <outbox@example.com>` | From address when a send names no sender |
| `OUTBOX_SENDERS` | empty | Sender id map, `id=Name <addr>;id2=addr2`. Relay sends name a sender id in `from_email`. An unmapped id becomes `<id>@<default domain>`. |
| `OUTBOX_MAILED_BY` | empty | Domain shown as "mailed-by". When it differs from the From domain, the reading view shows Gmail's "via" line. |
| `OUTBOX_CALLBACK_URL` | empty | Where to post signed delivery callbacks |
| `OUTBOX_CALLBACK_SECRET` | empty | HMAC secret for callbacks. Both URL and secret are needed to enable them. |
| `OUTBOX_CALLBACK_DELAY_SECONDS` | `2` | Wait before announcing, so the sender has recorded the send first |

## The Relay contract

Every `/api/...` endpoint below takes `Authorization: Bearer <key>`.
Validation errors use Relay's shape:
`{"error": {"code": "validation_error", "fields": {"context.name": "required"}}}`.

| Method and path | Behaviour |
|---|---|
| `PUT /api/transactional/templates/{key}` | Replace the draft (`name`, `subject`, `markdown_body` or `html_body`/`text_body`, `required_context`, `markdown_layout_context`, `category`, ...). `201` on create, `200` on update, body `{"template": {...}}` |
| `GET /api/transactional/templates/{key}` | `{"template": {...}}` including `latest_version` |
| `GET /api/transactional/templates` | `{"templates": [...]}` |
| `POST /api/transactional/templates/{key}/publish` | Snapshot the draft as the next immutable version. `201`, `{"template_key", "latest_version", "version": {...}}` |
| `GET /api/transactional/templates/{key}/versions` | Newest first, `{"template_key", "latest_version", "versions": [...]}` |
| `POST /api/transactional/templates/{key}/preview` | `{"context", "template_version" or "draft": true}` returns `subject`, `html_body`, `text_body`, `published`, `missing_context`, identical to what a send stores |
| `POST /api/transactional/templates/{key}/test-send` | Like a send, with `metadata.test_send = true`. There is no allowlist. |
| `POST /api/transactional/send` | `email`, `template_key`, optional `template_version`, `idempotency_key`, `context`, `from_email` (sender id), `reply_to`, `cc`, `bcc`, `headers`, `metadata`, `category`. Answers `202` with `{"message": {"id", "status": "queued", "template_key", "template_version", "idempotency_key", ...}, "idempotent_replay": false, "enqueued": true}`. A repeated `idempotency_key` answers `200` with the original message and `"idempotent_replay": true`. Missing required context answers `400`. |
| `GET /api/transactional/messages/{id}` | Message status |
| `GET /api/transactional/messages?since=<iso8601>` | Reconciliation: `{"messages": [{"id", "client_reference", "status", "template_key", "template_version", "reason_code", "updated_at"}]}` |
| `POST /api/contacts` | Contact upsert. Recorded and echoed in Relay's contact document shape. No subscription policy is applied. |
| `GET /api/contacts/status` | The stored contact, or `"exists": false` |
| `GET /health/ready` | `{"status": "ready"}` (no auth) |

### Rendering

Templates are rendered like Relay renders them:

1. `subject`, `markdown_body`, `html_body` and `text_body` are Django
   templates rendered with the send context, autoescape off.
2. A Markdown body becomes HTML with the `fenced_code`, `tables`,
   `attr_list` and `md_in_html` extensions. External links get
   `target="_blank" rel="noopener"`. The result is wrapped in a neutral HTML
   shell. The text part is the rendered Markdown source.
3. With `markdown_layout_context: true`, the context values `footer_note`,
   `verify_email_url` and `unsubscribe_url` are added to the shell and the
   text part.

Relay can also wrap Markdown in a client-specific branded layout that is
configured inside Relay. The outbox always uses the neutral shell.

### Lenient mode and stubs

- **Unknown template or version.** Relay answers `404`. By default the outbox
  accepts the send anyway: an unknown template renders a fallback message that
  lists every context value, with URLs as links. An unknown version renders the
  latest published version. Either way the message carries a warning that the
  inbox shows. Set `OUTBOX_STRICT=1` for Relay's `404`s.
- **Delivery.** Nothing is sent. A message is `queued` in the send response,
  then `delivered`.
- **Suppression, bounces, complaints, subscription policy, categories and
  rate limits** are not emulated. Every send is accepted.
- **Callbacks.** When enabled, every accepted message is announced as
  `delivery.accepted` and then `delivery.delivered` (contract version 1,
  canonical JSON, `X-Relay-Timestamp`/`X-Relay-Signature` HMAC-SHA256 over
  `<timestamp>.<body>`), once and without retries.
- **Not implemented:** the task/job and schedule API, subscription endpoints
  (subscribe, unsubscribe, double opt-in), tag endpoints, contact
  preferences, recipient lists, attachments and calendar parts. Run your
  application's jobs inline when testing against the outbox.

## Web UI

- `/` lists messages newest first (time, recipient, subject, template,
  status), with a recipient filter, a "Clear all" button and automatic
  refresh when a new message arrives.
- `/messages/{id}` is a Gmail-style reading view: subject, sender name and
  address, the "to" line with a details panel (from, reply-to, to, cc, date,
  subject, mailed-by), the rendered body, the list of changes Gmail would make,
  the plain-text part with clickable links, the headers and the template
  context.

The body is rendered in an `<iframe>` sandboxed without scripts, served with a
`default-src 'none'` Content Security Policy. Links open in a new tab.

### Dark mode

The outbox chrome (header, tabs, cards, panels) follows the OS colour scheme.
The Light / Dark / System buttons in the header override it; the choice is kept
in `localStorage` and set as `data-theme` on `<html>`. Only the chrome changes:
the message body keeps its own rendering (light in Gmail web/mobile, the
approximated inversion in Gmail app dark).

### Gmail rendering emulation

The reading view has four modes: **Gmail web**, **Gmail mobile**, **Gmail
app dark (approx.)** and **Raw HTML**. The Gmail modes run the HTML through a
sanitizer built from [caniemail.com's Gmail data](https://www.caniemail.com/clients/gmail/)
(snapshot of 2026-09-16, CC BY 4.0) and from documented Gmail behaviour.

Emulated:

| Gmail behaviour | What the outbox does |
|---|---|
| `<script>`, `<iframe>`, `<object>`, `<embed>`, `<svg>`, `<audio>`, `<link>`, `<meta>`, `<base>` removed | Removed |
| `<video>`, `<picture>`, `<source>`, `<track>` replaced by `<u></u>` | Replaced; fallback content inside is kept |
| `<body>` replaced by a `<div>` with its supported attributes | Same; `bgcolor` becomes a background colour |
| `<style>` supported in `<head>` only, up to 16 KB | `<style>` in the body or over 16 KB is dropped |
| A `<style>` block with a CSS syntax error is dropped entirely | Same |
| Class names and ids are prefixed, and rules are scoped to the message | Classes and ids get an `m_<n>_` prefix in markup and selectors. Rules are scoped to the message container; `html`/`body` selectors target the container. |
| Unsupported at-rules: `@font-face`, `@import`, `@keyframes`, `@supports`; media queries on height, colour scheme, hover, pixel ratio | Removed |
| Pseudo-classes and pseudo-elements other than `:hover` (web only) | The rule is removed |
| Attribute selectors other than `[class~=...]` | The rule is removed |
| Unsupported properties such as `position`, `z-index`, `top/left/...`, `box-shadow` (web), `transform`, `transition`, `animation`, `filter`, `text-shadow`, `visibility`, logical properties (`margin-inline`, `padding-block`, ...), `justify-content`, `align-items`, `flex-direction`, `flex-wrap`, `gap`, `cursor`, `user-select`, `aspect-ratio`, `clip-path`, `mask` | Declaration removed, both inline and in `<style>` |
| CSS custom property declarations (`--x: ...`) | Removed |
| Negative `margin` and `text-indent` | Removed |
| Uppercase `!IMPORTANT` | Declaration removed |
| `list-style` with an image | Removed |
| Modern colour syntax (`rgb(0 128 0)`, `oklch()`, ...) in a `style` attribute removes all inline styles of that element | Same |
| Event handler attributes, `javascript:` URLs, `srcset`, `loading`, `hidden` | Removed |
| `data:` (base64) images not supported | `src` removed |
| `target="_blank"` forced on every link | Same |
| Messages over 102 KB are clipped with "[Message clipped] View entire message" | Clipped once the source passes 102 KB; the link opens the raw HTML |
| Message container font Arial at `font-size: small`, links `#1155cc`, `word-wrap: break-word` everywhere | Applied as the default styles around the body |
| Gmail app (Google account): `:hover` unsupported, `text-emphasis`, `font-stretch` and `white-space: pre` removed | Applied in the mobile and dark modes |

Approximations, not exact:

- **Reading-pane width.** Gmail web uses the reading pane width of the
  browser window; the outbox uses its own column width. The mobile modes use a
  412 px wide frame and the Roboto stack at 14 px. The app's automatic
  scaling of wide emails is not emulated.
- **Dark mode.** Gmail web does not change email colours in its dark theme.
  The Gmail apps transform light emails, and the transform is not documented.
  The outbox inverts the body and re-inverts images, which is close to the iOS
  app's full inversion and darker than Android's partial one.
- **Clipping threshold.** Gmail measures the encoded message source. The
  outbox counts the bytes of the HTML part.
- **Images.** Gmail fetches images through its image proxy and may hide them
  for unknown senders; the outbox loads them directly. WebP/AVIF/SVG
  conversion is not emulated.
- **Link rewriting.** Gmail rewrites links through its redirector; the outbox
  keeps the original URLs so they work in tests.
- **Non-Google accounts in the Gmail apps**, which ignore `<style>`
  entirely, are not emulated.
- **Class-prefix format.** Gmail's prefix is random per view; the outbox
  derives a stable one from the message id.

## JSON API for tests

No authentication. Intended for test code and scripts.

| Method and path | Result |
|---|---|
| `GET /api/messages?to=<email>&limit=<n>` | `{"messages": [...]}`, newest first |
| `GET /api/messages/latest?to=<email>` | Newest message with bodies, context, headers and extracted `links` |
| `GET /api/messages/{id}` | One message with bodies, context, headers and `links` |
| `DELETE /api/messages/{id}` | Delete one |
| `DELETE /api/messages` | Delete all |

```bash
curl -s 'http://127.0.0.1:8025/api/messages/latest?to=dev-test@example.com' | jq -r '.links[0]'
```

## Development

```bash
uv sync
uv run pytest
```

## License

MIT. The Gmail support data is derived from caniemail.com (CC BY 4.0).
