import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


class DeliveryAttempt(BaseModel):
    attempted_at: float
    status_code: int | None
    error: str | None
    success: bool


class WebhookEvent(BaseModel):
    id: str
    source: str
    payload: dict[str, Any]
    received_at: float
    attempts: list[DeliveryAttempt] = []
    delivered: bool = False
    dead: bool = False


class EndpointConfig(BaseModel):
    url: HttpUrl
    secret: str
    max_retries: int = 5
    timeout_seconds: float = 10.0


class RegisterEndpointRequest(BaseModel):
    source: str
    url: HttpUrl
    secret: str
    max_retries: int = 5
    timeout_seconds: float = 10.0


store: dict[str, WebhookEvent] = {}
endpoints: dict[str, EndpointConfig] = {}
queue: asyncio.Queue[str] = asyncio.Queue()
dead_letter: list[str] = []


async def deliver_event(event: WebhookEvent, config: EndpointConfig) -> bool:
    payload_bytes = json.dumps(event.payload).encode()
    signature = hmac.new(
        config.secret.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Webhook-ID": event.id,
        "X-Webhook-Source": event.source,
        "X-Webhook-Timestamp": str(int(event.received_at)),
        "X-Webhook-Signature": f"sha256={signature}",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            str(config.url),
            content=payload_bytes,
            headers=headers,
            timeout=config.timeout_seconds,
        )
        return response.status_code < 300


async def process_queue():
    while True:
        event_id = await queue.get()
        event = store.get(event_id)

        if not event or event.delivered or event.dead:
            queue.task_done()
            continue

        config = endpoints.get(event.source)
        if not config:
            log.warning("No endpoint for source: %s", event.source)
            queue.task_done()
            continue

        delay = 1
        for attempt in range(config.max_retries):
            if attempt > 0:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

            try:
                success = await deliver_event(event, config)
                attempt_record = DeliveryAttempt(
                    attempted_at=time.time(),
                    status_code=200 if success else 400,
                    error=None,
                    success=success,
                )
                event.attempts.append(attempt_record)

                if success:
                    event.delivered = True
                    log.info("Delivered event %s on attempt %d", event_id, attempt + 1)
                    break

            except Exception as exc:
                attempt_record = DeliveryAttempt(
                    attempted_at=time.time(),
                    status_code=None,
                    error=str(exc),
                    success=False,
                )
                event.attempts.append(attempt_record)
                log.warning("Attempt %d failed for event %s: %s", attempt + 1, event_id, exc)

        if not event.delivered:
            event.dead = True
            dead_letter.append(event_id)
            log.error("Event %s moved to dead-letter after %d attempts", event_id, config.max_retries)

        queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = asyncio.create_task(process_queue())
    yield
    worker.cancel()


app = FastAPI(title="Webhook Gateway", lifespan=lifespan)


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/endpoints", status_code=201)
async def register_endpoint(body: RegisterEndpointRequest):
    endpoints[body.source] = EndpointConfig(
        url=body.url,
        secret=body.secret,
        max_retries=body.max_retries,
        timeout_seconds=body.timeout_seconds,
    )
    return {"source": body.source, "registered": True}


@app.get("/endpoints")
async def list_endpoints():
    return {
        source: {"url": str(cfg.url), "max_retries": cfg.max_retries}
        for source, cfg in endpoints.items()
    }


@app.post("/webhooks/{source}", status_code=202)
async def receive_webhook(
    source: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_signature: str | None = Header(None),
):
    config = endpoints.get(source)
    if not config:
        raise HTTPException(status_code=404, detail=f"No endpoint registered for source: {source}")

    raw = await request.body()

    if x_webhook_signature:
        if not verify_signature(raw, x_webhook_signature, config.secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = WebhookEvent(
        id=str(uuid.uuid4()),
        source=source,
        payload=payload,
        received_at=time.time(),
    )
    store[event.id] = event
    await queue.put(event.id)

    log.info("Queued event %s from source %s", event.id, source)
    return {"event_id": event.id, "status": "queued"}


@app.get("/events/{event_id}")
async def get_event(event_id: str):
    event = store.get(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@app.get("/events")
async def list_events(source: str | None = None, delivered: bool | None = None):
    events = list(store.values())
    if source:
        events = [e for e in events if e.source == source]
    if delivered is not None:
        events = [e for e in events if e.delivered == delivered]
    return {"total": len(events), "events": events}


@app.get("/dead-letter")
async def get_dead_letter():
    events = [store[eid] for eid in dead_letter if eid in store]
    return {"total": len(events), "events": events}


@app.delete("/dead-letter/{event_id}/retry")
async def retry_dead_letter(event_id: str):
    event = store.get(event_id)
    if not event or not event.dead:
        raise HTTPException(status_code=404, detail="Dead-letter event not found")
    event.dead = False
    event.delivered = False
    dead_letter.remove(event_id)
    await queue.put(event_id)
    return {"event_id": event_id, "status": "requeued"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "queue_size": queue.qsize(),
        "total_events": len(store),
        "dead_letter_count": len(dead_letter),
    }
