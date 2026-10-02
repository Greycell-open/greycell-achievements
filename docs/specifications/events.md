# Event format 1.0

Status: implemented in 0.1. CC0-1.0.

One JSON object per line in `events/YYYY/MM/events-YYYY-MM-DD.jsonl` inside
the profile folder, named by the day the event was recorded on the device
that wrote it. Lines are never edited.

```json
{
  "schema_version": "1.0",
  "event_id": "lowercase uuid",
  "event_type": "achievement.unlocked",
  "profile_id": "lowercase uuid",
  "device_id": "lowercase uuid",
  "game_id": "cave-story",
  "achievement_id": "cave-story-community:balrog",
  "occurred_at": "2026-09-30T12:00:00.000Z",
  "recorded_at": "2026-09-30T12:00:00.120Z",
  "source": {
    "adapter": "manual",
    "adapter_version": "0.1.0",
    "external_account_id": null,
    "external_event_id": null
  },
  "payload": {"provenance": "manual"},
  "integrity": {"previous_event_hash": null, "event_hash": "sha256:..."}
}
```

## Hash

`event_hash` is `"sha256:"` plus the hex SHA-256 of the event **without** its
`integrity` key, serialised as JSON with sorted keys, separators `,` and `:`
(no spaces), UTF-8, non-ASCII characters kept as-is. In Python:
`json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
An event whose hash does not match is rejected on read and on upload.

## Identifiers

- `game_id`, pack ids and achievement ids: lowercase, `[a-z0-9][a-z0-9._:-]{0,127}`.
- `achievement_id` in an event is `<pack-id>:<achievement-id>`.
- Imported games use the platform: `ra-<RA game id>`, `steam-<app id>`.

## Types in use

| Type | Payload |
|---|---|
| `profile.created`, `profile.updated` | `name`, `privacy`, `settings` |
| `device.registered` | `name`, `platform`, `client_version` |
| `game.registered` | `title`, `platform`, `release_year`, `icon_url`, `external_ids` |
| `game.metadata_updated` | any of the above, and `linked_to` (another game id, or null to unlink) |
| `game.installation_registered` | `installation_id`, `kind`, `source`, `executable_name`, `size`, `sha256`, `partial`. **Never the path.** |
| `pack.installed`, `pack.updated` | the whole normalised pack, including every achievement definition |
| `pack.removed` | `pack_id` |
| `progress.incremented` | `amount` |
| `progress.set` | `value` |
| `achievement.unlocked` | `provenance`, optional `mode` (RA: softcore/hardcore), `evidence`, `note`, `installation_id` |
| `achievement.revoked` | `reason`, optional `revokes` (event ids; absent means all) |
| `session.started`, `session.ended` | `installation_id`; ended adds `seconds`, `started_event_id` |
| `platform.account_linked` | `adapter`, `account` |
| `platform.import_completed` | `adapter`, `summary` |

Other types listed in `events.py` (save transfer, conflicts, evidence) are
reserved and accepted but not yet produced.

## Provenance

`imported`, `local-executable`, `save-derived`, `game-log`, `adapter-verified`
(a progress target an adapter's counts reached), `unverified`. Shown next to
every unlock.

There are no hand-made achievements (owner decision 2026-10-01). An
`achievement.unlocked` with provenance `manual` (or no provenance from the
`manual` adapter), and any `progress.*` event from the `manual` adapter, stays
in the log, because the log is append-only and may hold them from older
clients, but the reducer never counts it, on any device or server.
