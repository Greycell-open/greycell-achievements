# ADR 0009: RetroAchievements live, with the key in the credential store

Date: 2026-10-02. Status: accepted (owner decision: "go for it").

**Context.** RetroAchievements has an official, documented Web API: every
player has a personal Web API key on their settings page. Version 0.1 had a
one-shot importer on the command line that asked for the key each run and
was not in the dashboard. Since 2025 RetroAchievements usernames can change;
each account also has a permanent ULID.

**Decision.** RetroAchievements joins the dashboard's Link accounts menu. The
player gives a username and Web API key once; the app checks both with
`API_GetUserProfile`, keeps the key only in the operating system's credential
store and remembers the username and ULID. `RaWatcher` (in
`adapters/retroachievements.py`) asks for the completion list every two
minutes and fetches only games whose last award changed, latest first, one
request per round and never two within five seconds; it never waits inside a
request. Requests use the ULID, so a rename does not break the link; external
ids keep the format the importer always used, `<username>:<achievementId>:<mode>`
with the username recorded at link time, so the importer and the live link
never count an unlock twice. Softcore and hardcore stay separate records,
RetroAchievements points are kept, unlocks sync quietly (the emulator shows
its own popup). The command line importer stays and shares the same per-game
code (`plan_game`). Nothing is ever written to RetroAchievements.

**Consequences.** Unlocks reach the library within about two minutes of
RetroAchievements recording them; the app does not watch the emulator itself.
Only games the player has started are listed; a catalogue of every
RetroAchievements game is possible through the same API and is separate work.
`tests/test_ra_live.py` runs against a fake API.
