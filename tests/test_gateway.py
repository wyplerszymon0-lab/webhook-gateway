import asyncio
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app, dead_letter, endpoints, store, queue


@pytest.fixture(autouse=True)
def clear_state():
    store.clear()
    endpoints.clear()
    dead_letter.clear()
    while not queue.empty():
        try:
            queue.get_nowait()
        except Exception:
            break
    yield


client = TestClient(app)


def test_register_endpoint():
    resp = client.post("/endpoints", json={
        "source": "github",
        "url": "https://example.com/hook",
        "secret": "mysecret",
    })
    assert resp.status_code == 201
    assert resp.json()["registered"] is True


def test_register_and_list_endpoints():
    client.post("/endpoints", json={"source": "stripe", "url": "https://example.com/hook", "secret": "s"})
    resp = client.get("/endpoints")
    assert "stripe" in resp.json()


def test_receive_webhook_unknown_source():
    resp = client.post("/webhooks/unknown", json={"event": "test"})
    assert resp.status_code == 404


def test_receive_webhook_returns_event_id():
    client.post("/endpoints", json={"source": "github", "url": "https://example.com/hook", "secret": "s"})
    resp = client.post("/webhooks/github", json={"action": "push"})
    assert resp.status_code == 202
    assert "event_id" in resp.json()


def test_get_event_after_receive():
    client.post("/endpoints", json={"source": "github", "url": "https://example.com/hook", "secret": "s"})
    resp = client.post("/webhooks/github", json={"action": "push"})
    event_id = resp.json()["event_id"]
    event = client.get(f"/events/{event_id}").json()
    assert event["source"] == "github"
    assert event["payload"]["action"] == "push"


def test_get_event_not_found():
    resp = client.get("/events/nonexistent-id")
    assert resp.status_code == 404


def test_invalid_json_payload():
    client.post("/endpoints", json={"source": "github", "url": "https://example.com/hook", "secret": "s"})
    resp = client.post(
        "/webhooks/github",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_invalid_signature_rejected():
    client.post("/endpoints", json={"source": "github", "url": "https://example.com/hook", "secret": "correct"})
    resp = client.post(
        "/webhooks/github",
        json={"action": "push"},
        headers={"X-Webhook-Signature": "sha256=wrongsignature"},
    )
    assert resp.status_code == 401


def test_list_events_filter_by_source():
    client.post("/endpoints", json={"source": "github", "url": "https://example.com/hook", "secret": "s"})
    client.post("/endpoints", json={"source": "stripe", "url": "https://example.com/hook", "secret": "s"})
    client.post("/webhooks/github", json={"a": 1})
    client.post("/webhooks/stripe", json={"b": 2})
    resp = client.get("/events?source=github")
    events = resp.json()["events"]
    assert all(e["source"] == "github" for e in events)


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_dead_letter_empty_initially():
    resp = client.get("/dead-letter")
    assert resp.json()["total"] == 0
````

**`requirements.txt`**
````
fastapi==0.115.0
uvicorn==0.30.0
httpx==0.27.0
pydantic==2.7.0
pytest==8.2.0
pytest-asyncio==0.23.0
