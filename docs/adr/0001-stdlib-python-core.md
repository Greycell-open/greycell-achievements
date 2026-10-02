# ADR 0001: The core is Python with no dependencies outside the standard library

Date: 2026-09-30. Status: accepted.

**Context.** The original plan preferred Python 3.12, Pydantic and FastAPI. The core
(profile folder, events, reducer, index, CLI, imports, sync client) must run
on any machine a player owns, offline, and be easy to package later.

**Decision.** The core uses only the standard library: `json`, `sqlite3`,
`hashlib`, `urllib`, `zipfile`. FastAPI and uvicorn are an optional extra
(`[server]`) used only by the sync server and `openachievements serve`.
Validation is hand-written in `events.py` and `packs.py` rather than Pydantic.

**Consequences.** `pip install open-achievements` works anywhere Python does,
and a future desktop shell can embed the core without a dependency tree. The
server keeps Pydantic models for its OpenAPI contract. JSON Schemas are
documented in `docs/specifications/` rather than generated.
