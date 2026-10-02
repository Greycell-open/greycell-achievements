# ADR 0005: Steam is sign-in only; no Web API key anywhere

Date: 2026-10-01. Status: accepted (owner decision).

**Context.** A player's whole Steam library, including games never played, is
only shown by Steam to a Web API key or to a signed-in Steam session.
Anonymous community pages stopped carrying a game list (verified 2026-10-01:
the profile and games pages answer `private` or redirect to Steam's login).
Version 0.1 offered two key routes: the player's own key in the OS credential
store, and an operator key on the sync server (`OA_STEAM_KEY`,
`server/steam_service.py`) that read libraries for every player. The owner
ruled both out: "must not use key just sign on".

**Decision.** Steam is linked by "Sign in through Steam" (OpenID 2.0) and
nothing else. Signing in records which account is the player's, then follows
Steam's own files on this PC for that exact account
(`adapters/steam_local.py`): every game it has played here, with play history,
achievement definitions and unlocks with Steam's own times. A different
account that happens to play on the same PC is never followed. Removed:
`adapters/steam.py` (Web API importer), `credentials.py`,
`server/steam_service.py`, `openachievements steam key|service`,
`openachievements import steam`, the local `/v1/local/steam/key` and
`/v1/local/steam/import` endpoints, and the server's `steam-library` feature.
`tests/test_steam_account.py::test_there_is_no_key_route_anywhere` pins it.

**Consequences.** Games owned but never played on this PC do not come in, and
the Steam dialog says so. Hidden-achievement text still comes from Steam's
keyless `IPlayerService/GetGameAchievements`, and the public catalogue still
needs no key. Profiles that already hold unlocks from the old keyed import
keep them: events are immutable and their `external_event_id`s match the
local follower's, so nothing double counts. A key a player saved under 0.1
stays where 0.1 put it (Windows Credential Manager, target
`OpenAchievements/steam-web-api-key/<profile id>`; elsewhere the 0600
`credentials.json` beside the machine config). This version never reads it, and
the player may delete it there.
