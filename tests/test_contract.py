"""The Relay HTTP contract as a Relay client (such as community-base) uses it."""

TEMPLATE = {
    "name": "Event registration",
    "subject": "You're registered: {{ event_title }}",
    "markdown_body": (
        "Hi {{ user_name }},\n\n"
        "{% if join_url %}Join: [link]({{ join_url }}){% endif %}\n"
    ),
    "required_context": ["event_title"],
}


def publish(client, auth, key="event-registration", draft=TEMPLATE):
    assert client.put(f"/api/transactional/templates/{key}", json=draft, headers=auth).status_code in {200, 201}
    response = client.post(f"/api/transactional/templates/{key}/publish", json={}, headers=auth)
    assert response.status_code == 201
    return response.json()


def send_payload(**overrides):
    payload = {
        "email": "dev-test+1@example.com",
        "template_key": "event-registration",
        "template_version": 1,
        "idempotency_key": "events.verify:1:1",
        "context": {"event_title": "Community Lunch", "user_name": "Ada", "join_url": "https://example.com/join"},
    }
    payload.update(overrides)
    return payload


def test_requires_bearer_token(client):
    response = client.post("/api/transactional/send", json=send_payload())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_authorization"
    response = client.get("/api/transactional/templates", headers={"Authorization": "Basic x"})
    assert response.json()["error"]["code"] == "invalid_authorization"


def test_configured_api_key_must_match(make_client):
    client = make_client(api_key="right")
    assert client.get("/api/transactional/templates", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/transactional/templates", headers={"Authorization": "Bearer right"}).status_code == 200


def test_template_catalog_round_trip(client, auth):
    created = client.put("/api/transactional/templates/event-registration", json=TEMPLATE, headers=auth)
    assert created.status_code == 201
    assert created.json()["template"]["latest_version"] is None
    published = client.post("/api/transactional/templates/event-registration/publish", json={}, headers=auth).json()
    assert published["version"]["template_key"] == "event-registration"
    assert published["version"]["version"] == 1
    assert published["latest_version"] == 1
    listed = client.get("/api/transactional/templates", headers=auth).json()["templates"]
    assert [(row["key"], row["latest_version"]) for row in listed] == [("event-registration", 1)]
    assert listed[0]["markdown_body"] == TEMPLATE["markdown_body"]
    publish(client, auth)
    versions = client.get("/api/transactional/templates/event-registration/versions", headers=auth).json()
    assert [row["version"] for row in versions["versions"]] == [2, 1]
    assert versions["versions"][0]["subject"] == TEMPLATE["subject"]


def test_send_renders_like_relay_and_matches_client_checks(client, auth):
    publish(client, auth)
    response = client.post("/api/transactional/send", json=send_payload(), headers=auth)
    assert response.status_code == 202
    document = response.json()
    message = document["message"]
    # community_base.mail.relay.RelayMailClient.send validates exactly these.
    assert isinstance(message["id"], int)
    assert message["status"] == "queued"
    assert message["template_key"] == "event-registration"
    assert message["template_version"] == 1
    assert message["idempotency_key"] == "events.verify:1:1"
    assert document["idempotent_replay"] is False

    stored = client.get(f"/api/messages/{message['id']}").json()
    assert stored["subject"] == "You're registered: Community Lunch"
    assert '<a href="https://example.com/join" rel="noopener" target="_blank">link</a>' in stored["html_body"]
    assert stored["text_body"].startswith("Hi Ada,")
    assert "https://example.com/join" in stored["links"]


def test_idempotent_replay_returns_the_original(client, auth):
    publish(client, auth)
    first = client.post("/api/transactional/send", json=send_payload(), headers=auth).json()
    again = client.post("/api/transactional/send", json=send_payload(), headers=auth)
    assert again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert again.json()["message"]["id"] == first["message"]["id"]
    assert len(client.get("/api/messages").json()["messages"]) == 1


def test_missing_required_context_is_a_validation_error(client, auth):
    publish(client, auth)
    response = client.post(
        "/api/transactional/send", json=send_payload(context={"user_name": "Ada"}), headers=auth
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "validation_error", "fields": {"context.event_title": "required"}}
    }


def test_send_validates_the_request(client, auth):
    response = client.post(
        "/api/transactional/send",
        json={"email": "nope", "template_key": "", "template_version": 0},
        headers=auth,
    )
    assert response.status_code == 400
    assert set(response.json()["error"]["fields"]) == {"email", "template_key", "template_version"}


def test_unknown_template_strict_mode_is_404(make_client, auth):
    client = make_client(strict=True)
    response = client.post("/api/transactional/send", json=send_payload(), headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}
    publish(client, auth)
    response = client.post("/api/transactional/send", json=send_payload(template_version=9), headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_version": "not_found"}


def test_unknown_template_lenient_mode_renders_the_context(client, auth):
    payload = send_payload(
        template_key="events.verify_registration",
        context={"event_title": "Lunch", "verify_url": "http://localhost:8000/verify/?token=abc"},
    )
    response = client.post("/api/transactional/send", json=payload, headers=auth)
    assert response.status_code == 202
    assert response.json()["message"]["template_version"] == 1
    stored = client.get(f"/api/messages/{response.json()['message']['id']}").json()
    assert stored["warnings"] == ["template_not_found"]
    assert 'href="http://localhost:8000/verify/?token=abc"' in stored["html_body"]
    assert "verify_url: http://localhost:8000/verify/?token=abc" in stored["text_body"]


def test_recipient_options_and_sender_mapping(make_client, auth):
    client = make_client(senders={"events": "Events Team <events@example.org>"})
    publish(client, auth)
    payload = send_payload(
        from_email="events", reply_to="help@example.org", cc=["a@example.org"], bcc=["b@example.org"]
    )
    message = client.post("/api/transactional/send", json=payload, headers=auth).json()["message"]
    assert message["from_email"] == "events"
    assert message["from_email_address"] == "Events Team <events@example.org>"
    assert message["reply_to"] == "help@example.org"
    assert message["cc"] == ["a@example.org"]
    assert message["bcc"] == ["b@example.org"]


def test_preview_matches_send(client, auth):
    publish(client, auth)
    context = send_payload()["context"]
    preview = client.post(
        "/api/transactional/templates/event-registration/preview",
        json={"context": context, "template_version": 1},
        headers=auth,
    ).json()
    message_id = client.post("/api/transactional/send", json=send_payload(), headers=auth).json()["message"]["id"]
    stored = client.get(f"/api/messages/{message_id}").json()
    assert (preview["subject"], preview["html_body"], preview["text_body"]) == (
        stored["subject"], stored["html_body"], stored["text_body"]
    )
    assert preview["published"] is True
    assert preview["missing_context"] == []
    missing = client.post(
        "/api/transactional/templates/event-registration/preview", json={"context": {}}, headers=auth
    ).json()
    assert missing["missing_context"] == ["event_title"]


def test_test_send(client, auth):
    publish(client, auth)
    response = client.post(
        "/api/transactional/templates/event-registration/test-send",
        json={"email": "staff@example.com", "context": send_payload()["context"]},
        headers=auth,
    )
    assert response.status_code == 202
    assert response.json()["message"]["template_key"] == "event-registration"
    stored = client.get(f"/api/messages/{response.json()['message']['id']}").json()
    assert stored["metadata"]["test_send"] is True


def test_reconciliation_lists_messages_since(client, auth):
    publish(client, auth)
    client.post("/api/transactional/send", json=send_payload(), headers=auth)
    rows = client.get(
        "/api/transactional/messages", params={"since": "2000-01-01T00:00:00+00:00"}, headers=auth
    ).json()["messages"]
    assert len(rows) == 1
    assert rows[0]["client_reference"] == "events.verify:1:1"
    assert rows[0]["status"] in {"queued", "delivered"}
    assert set(rows[0]) == {
        "id", "client_reference", "status", "template_key", "template_version", "reason_code", "updated_at"
    }
    bad = client.get("/api/transactional/messages", params={"since": "nope"}, headers=auth)
    assert bad.status_code == 400


def test_contacts_upsert_and_status(client, auth):
    body = {"email": "Dev-Test+1@example.com", "audience": "dtc", "client": "site",
            "status": "subscribed", "verified": True, "tags": ["Course-X"]}
    contact = client.post("/api/contacts", json=body, headers=auth).json()
    assert contact["contact_id"] == 1
    assert contact["tags"] == ["course-x"]
    assert contact["client"]["subscribed"] is True
    status = client.get(
        "/api/contacts/status",
        params={"email": "dev-test+1@example.com", "audience": "dtc", "client": "site"},
        headers=auth,
    ).json()
    assert status["exists"] is True and status["contact_id"] == 1


def test_health(client):
    assert client.get("/health/ready").json()["status"] == "ready"
