# Achievement pack format 1.0

Status: implemented in 0.1. CC0-1.0.

A pack is a folder, or a ZIP containing exactly one such folder:

```
cave-story-community/
  pack.json
  achievements.json
  icons/            optional
  README.md         optional
```

## pack.json

```json
{
  "schema_version": "1.0",
  "id": "cave-story-community",
  "name": "Cave Story (community set)",
  "version": "1.0.0",
  "game_ids": ["cave-story"],
  "games": [{ "id": "cave-story", "title": "Cave Story", "platform": "PC (freeware)" }],
  "authors": ["Your name"],
  "license": "CC0-1.0",
  "source_repository": "https://example.com/packs",
  "supported_adapters": ["executable", "manual"]
}
```

`games` is optional; games listed there are registered automatically on
install. Every id in `game_ids` must be registered or listed.

## achievements.json

A list of up to 2000 achievements:

```json
[
  { "id": "first-launch", "name": "Hello, Mimiga Village", "description": "Start the game.",
    "points": 5, "rules": [{ "signal": "launch" }] },
  { "id": "missiles", "name": "Missile Collector", "points": 20,
    "progress": { "type": "counter", "target": 10, "unit": "expansion" } },
  { "id": "curly-saved", "name": "Nobody Left Behind", "points": 50, "hidden": true }
]
```

| Field | Rules |
|---|---|
| `id` | lowercase slug, unique in the pack, no `:` |
| `name` | 1 to 120 characters |
| `description` | up to 500 characters |
| `points` | whole number 0 to 10000 |
| `hidden` | name and description are masked until unlocked; the owner's own library can reveal them on click, public profiles never carry them |
| `repeatable` | may be unlocked more than once |
| `category`, `rarity` | free text |
| `icon` | an https URL, or a path inside the pack (checked; `..` and absolute paths are refused) |
| `external_id` | the platform's own id for it, such as Steam's internal name (`FOY_COMPLETE`); up to 128 characters |
| `progress` | `{type, target, unit}`; reaching `target` unlocks automatically |
| `rules` | see below |

## Rules

Rules let an adapter unlock without anyone ticking a box. All rules on an
achievement must hold. Rules an adapter does not understand are left alone.

| Signal | Adapter | Holds when |
|---|---|---|
| `{"signal": "launch"}` | executable | the game has been seen running once |
| `{"signal": "playtime", "minutes": 60}` | executable | total play time reaches the minutes |
| `{"signal": "sessions", "count": 5}` | executable | this many separate play sessions have ended |

| `{"signal": "save", ...}` | save-file | a value in the game's own save file (below) |

Log and plugin signals are future work.

## Save files

For games no store client is watching. The pack says where the game keeps its
saves and how to read them; rules say which values mean an achievement. It works
the same for every copy of a game.

In `pack.json`, a `saves` list (1 to 10 entries):

| Field | Meaning |
|---|---|
| `id` | lowercase slug, unique in the pack; rules refer to it |
| `root` | one of `LOCALAPPDATA`, `LOCALLOW`, `APPDATA`, `DOCUMENTS`, `SAVED_GAMES` (the platform's own folders; there is no home-folder root) |
| `path` | relative to the root; no `..`, no absolute path, no drive, no segment starting with `.` |
| `pattern` | file name pattern in that folder only, such as `slot*.json` (default `*`) |
| `format` | `json`, `ini` (key=value, optional `[sections]`), or `text` |

A `save` rule is either a field comparison or a text search:

```json
{"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 3}
{"signal": "save", "save": "main", "contains": "BOSS_ALMA_DEFEATED"}
```

- `field` is a dotted path: object keys, or whole numbers for list positions
  (`slots.0.chapter`). In `ini` saves it is `Section.key`, or `key` before any section.
  A key that itself contains dots matches whole (`SavedFloatMap.NC.Character.GyroMult`).
- `format` is `json`, `ini`, `text` or `gvas` (Unreal Engine save games: top-level
  properties, with maps, arrays and structs of numbers, text and booleans; anything
  else is skipped, never guessed) or `fireproof` (The Room series' LZF-packed XML
  slots: `slot.<field>` for the slot's simple values such as `slot.GameComplete`,
  and `levels.<scene>` for each room's Locked, Entered or Complete state), `nrbf`
  (.NET BinaryFormatter, common in Unity games: classes as objects, property
  backing fields by their property name, lists and simple-keyed dictionaries as
  such) or `es3` (Easy Save 3 JSON with its `__type`/`value` wrappers removed).
- `op` is one of `== != >= <= > < exists contains`. Numbers compare as numbers
  when both sides are numeric; otherwise `==` and `!=` compare text ignoring case.
- An achievement unlocks when **one** save file satisfies **all** its rules, so
  progress split across two save slots does not count. Save rules cannot be mixed
  with executable rules on one achievement.
- There are no regular expressions, on purpose: a crafted one can hang the watcher.

Limits: 64 MB per file, 50 newest matching files per save, 256 MB read per poll.

**Consent.** A pack is data from someone else, and it can arrive through sync, so
nothing is read until the person allows each location on their machine:
`openachievements save allow <pack> <save>` shows and records the exact folder;
`save locate <pack> <save> <folder>` picks another folder and allows it. If a pack
update moves the location, it must be allowed again. Files that resolve outside the
folder (links) are skipped. Saves are only read: never written, copied or uploaded;
an unlock records `save-derived` provenance with the file name and a hash prefix.

A complete example:

```json
{"id": "example-save-game", "name": "Example (save rules)", "version": "1.0.0",
 "game_ids": ["example-game"], "games": [{"id": "example-game", "title": "Example Game"}],
 "saves": [{"id": "main", "root": "LOCALLOW", "path": "Example Studio/Example Game",
            "pattern": "slot*.json", "format": "json"}]}
```
```json
[{"id": "halfway", "name": "Halfway There", "rules": [
   {"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 3}]},
 {"id": "true-ending", "name": "The Truth", "hidden": true, "rules": [
   {"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 7},
   {"signal": "save", "save": "main", "field": "flags.ending", "op": "==", "value": "true"}]}]
```

`openachievements save check` lists each save's folder, whether it is allowed,
its files, and which achievements each file satisfies: the tool for writing rules.

## Installing and updating

`openachievements pack install <folder or .zip>`. The whole normalised pack
is recorded in a `pack.installed` event, so the event log alone can interpret
every unlock. Installing a pack with an id that exists records `pack.updated`.
