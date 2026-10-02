# Sync protocol 1.0

Status: implemented in 0.1. CC0-1.0.

## Model

Every device keeps the whole history as immutable events
([event format](events.md)). Sync exchanges events; it never exchanges
summaries, so there is nothing for devices to overwrite. State is recomputed
from events by a deterministic reducer, so any two devices holding the same
events show the same library.

## Authentication

`POST /v1/sessions {username, password, device_id, device_name}` returns a
bearer token for that device. Tokens are stored hashed on the server and can
be revoked per device (`DELETE /v1/devices/{device_id}`). Passwords are never
stored by the client.

## Exchange

`POST /v1/sync`

```json
{
  "protocol_version": "1.0",
  "profile_id": "uuid",
  "device_id": "uuid",
  "cursor": "opaque or null",
  "events": [ ...up to max_batch events... ]
}
```

Response:

```json
{
  "protocol_version": "1.0",
  "accepted_event_ids": ["..."],
  "rejected_events": [{"event_id": "...", "code": "bad_hash", "message": "..."}],
  "remote_events": [ ...events after the cursor, excluding those just sent... ],
  "next_cursor": "opaque",
  "more": false,
  "server_time": "2026-09-30T12:00:00.000Z",
  "server_capabilities": {"protocol_versions": ["1.0"]}
}
```

Rules:

- The first sync binds the profile to the account; another profile is `409 profile_mismatch`.
- Every uploaded event is validated: schema, UUIDs, timestamps, the profile id,
  and `integrity.event_hash` recomputed from the event. A failure is a
  rejection with a machine-readable `code`; the rest of the batch still counts.
- An event already stored is **accepted**, not rejected. Re-sending is always safe.
- Over the server's quota, the excess is rejected with `quota_exceeded` and
  the client keeps it pending. Nothing is dropped silently.
- The cursor is opaque. Clients store it and send it back.
- `more: true` means call again with `next_cursor`.
- Clients retry `429` and `5xx` with exponential backoff and jitter.
- Remote servers must be HTTPS; clients allow plain HTTP only to localhost.

`GET /v1/capabilities` reports protocol versions, limits and features and
must be checked before the first exchange.

## Conflicts

There is no last-write-wins. The reducer:

- treats the same `event_id` as one event;
- treats the same imported unlock (`adapter` + `external_event_id`) from two
  devices as one provenance record;
- adds progress increments; a `progress.set` resets the base, and two sets
  from different devices with different values in the same minute are listed
  under `conflicts` in the library for a person to resolve;
- applies revocations as events, never by deleting the unlock.

Ordering for reduction is `(occurred_at, recorded_at, event_id)`.
