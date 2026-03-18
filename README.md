# webhook-gateway

Production-grade webhook gateway built with FastAPI. Receives webhooks from external services, validates HMAC signatures, queues events and delivers them with exponential backoff retry logic.

## Features

- HMAC-SHA256 signature verification per source
- Async delivery queue with configurable retries
- Exponential backoff (1s → 2s → 4s → ... → 60s)
- Dead-letter queue for failed events with manual retry
- Per-source endpoint registration
- Full event history and delivery status

## Architecture
````
External Service → POST /webhooks/{source}
                        ↓
               Signature Verification
                        ↓
                  In-Memory Queue
                        ↓
               Async Worker (retry loop)
                        ↓
              Registered Endpoint URL
````

## Run
````bash
pip install -r requirements.txt
uvicorn main:app --reload
````

## Test
````bash
pytest tests/ -v
````

## Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/endpoints` | Register delivery endpoint |
| GET | `/endpoints` | List registered endpoints |
| POST | `/webhooks/{source}` | Receive webhook |
| GET | `/events/{id}` | Get event status |
| GET | `/events` | List events (filterable) |
| GET | `/dead-letter` | List failed events |
| DELETE | `/dead-letter/{id}/retry` | Retry dead-letter event |
| GET | `/health` | Gateway health |
