# Adapters and their support levels

| Adapter | Level | How | Notes |
|---|---|---|---|
| manual | removed | | no unlock by hand from any device; the reducer ignores old manual unlocks |
| executable | official | executable fingerprint + OS process list | launch, playtime and session rules; no injection, path never synced |
| retroachievements | official | RA Web API, your username + Web API key (key in the OS credential store) | live in the dashboard (ADR 0009) or `import` on the command line; softcore and hardcore kept apart; badges linked, not copied |
| steam | official | Sign in through Steam, then Steam's own files on this PC for that account | no key (ADR 0005); games never played on this PC are not listed; Steam has no points |
| retroarch | not yet | | planned: local core/ROM detection for games without RA sets |
| save-file | official | the game's own save files, read only | per-game pack rules; each save folder read only after `save allow`; provenance `save-derived` |
| game-log | not yet | | planned: game-specific, read-only |
| xbox | experimental | Microsoft sign-in (the owner's own app registration, PKCE), Xbox Live read only; session in the OS credential store | live, quiet, gamerscore as points; Xbox 360 included; ADR 0007 |
| psn | experimental | Sony's web API (unofficial), with a pasted sign-in code; session in the OS credential store | read only, live sync, quiet; ADR 0006; no password, no scraping |
| gog | experimental | the player's public GOG profile (username only, no sign-in) | read only, live, quiet, no points; profile must be public; ADR 0008 |
| nintendo switch | n/a | | no platform achievements exist; community packs and manual play instead |

Every adapter is read-only toward its platform. Nothing Open Achievements does
can grant a Steam, PSN, Xbox or GOG achievement.
