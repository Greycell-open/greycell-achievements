# ADR 0004: Platform imports are read-only copies; the same game links, it does not merge

Date: 2026-09-30. Status: accepted.

**Context.** The product combines achievements from Steam, RetroAchievements
and local play into one library. It must never write to a platform, and the
same game often exists in several places (a DRM-free copy and a Steam copy).

**Decision.** Each source keeps its own game and pack (`steam-504230`,
`ra-1234`, `celeste`). Imports only read, through documented APIs with the
player's own key, and are idempotent by `external_event_id`. A
`game.metadata_updated {linked_to}` event shows one game inside another in the
library; unlinking is another event. Nothing is merged or rewritten.

**Consequences.** Provenance is never lost: a Steam unlock stays a Steam
unlock beside a local one. Linking is reversible and syncs like everything
else. Cross-source "these two achievements are the same" matching is not
done automatically; a future pack may declare equivalences explicitly.
