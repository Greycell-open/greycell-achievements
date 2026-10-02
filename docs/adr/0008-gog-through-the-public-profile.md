# ADR 0008: GOG achievements from the player's public GOG profile, read only

Date: 2026-10-02. Status: accepted (owner decision: "do gog next").

**Context.** GOG achievements live in GOG Galaxy. GOG's achievement API
(`gameplay.gog.com`) only accepts tokens issued to GOG Galaxy's own client;
getting one means using Galaxy's client id and secret, which is impersonating
GOG's client, and this project does not do that. Galaxy's local database only
exists where Galaxy is installed and its layout is private. GOG profiles, on
the other hand, are public pages anyone can open: `gog.com/u/<username>`
answers its games list as JSON (playtime, last session, achievement
percentage), and each game's profile page embeds the full achievement list
with unlock times (`window.profilesData.achievements`).

**Decision.** Linking GOG is the username: no sign-in, no password, no token,
nothing secret stored. The profile must be visible to everyone (GOG: Settings,
Privacy); a private profile gets that explanation. `adapters/gog.py` reads the
games list, then the profile page of each game with achievements whose
percentage or last session changed, games with progress first, one request
per watcher round and never two within ten seconds. Games are `gog-<productId>`,
GOG has no points, hidden achievements stay hidden until unlocked (GOG shows
"Secret achievement" until then), unlocks keep GOG's time and the external id
`<userId>:<productId>:<achievementId>`, and they sync quietly. Nothing is ever
written to GOG.

**Consequences.** Anyone could link anyone's public profile; that only reads
what GOG already shows the world. The game pages are HTML, not an API, so GOG
can change them: the watcher then reports "GOG changed its profile pages"
rather than guessing. GOG updates a profile after Galaxy syncs, so unlocks
arrive minutes after that, not instantly. `tests/test_gog.py` runs against a
fake gog.com shaped like the real pages; a real run against a public profile
(2026-10-02) read 7 list pages, 31 games with achievements and Oxenfree at 1
of 13 with GOG's unlock time.
