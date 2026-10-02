# ADR 0002: The sync server stores everything in one SQLite file

Date: 2026-09-30. Status: accepted, revisit for a large hosted instance.

**Context.** The original plan suggested PostgreSQL for server mode. The
first priority is that anyone can self-host in one step.

**Decision.** SQLite in WAL mode, one file, created on start
(`server/store.py`). One container, one volume, no database service. The
schema is plain (accounts, tokens, profiles, events) with uniqueness
constraints doing the idempotency work.

**Consequences.** A home server or a small VPS runs it with nothing else. The
Greycell-hosted instance can run the same way until concurrent writes become
a real bottleneck; at that point a PostgreSQL store with the same interface
replaces `Store` without changing the sync protocol. Schema versions are
recorded in `schema_migrations` from the start.
