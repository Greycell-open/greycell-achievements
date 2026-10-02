# ADR 0006: PlayStation trophies through Sony's web API, read only

Date: 2026-10-02. Status: accepted (owner decision: "must be live sync").

**Context.** Sony publishes no trophy API for other apps and no way for an
app to sign a player in. Every PlayStation trophy tracker reads the API that
Sony's own trophy pages use, documented by the community and used by
maintained libraries. The alternatives were weighed with the owner: Sony's
privacy data export (official, but a manual snapshot), paid third-party
services (a third party holds the access, against local-first), and scraping
trophy sites (their terms forbid it). The owner wanted a live sync.

**Decision.** The player signs in on playstation.com, opens
`https://ca.account.sony.com/api/v1/ssocookie` and pastes the "npsso" code.
It is exchanged once for an access token (an hour, memory only) and a refresh
token (about two months), kept in the operating system's credential store and
nowhere else: not the profile, not an event, not a log, not a sync. The code
itself is not kept and no password is asked for. The dialog suggests a spare
PSN account, which can read a main account's trophies when those are visible
to others, so the main account's session is never used.
`adapters/psn.py` then reads the trophy title list, and for each new or
changed title its trophy list and what was earned: one request per watcher
round, about one every five seconds, under Sony's rate limit. Games are
`psn-<npCommunicationId>` with the platform (PS5, PS4, PS3, Vita); trophies
carry PSN's points (bronze 15, silver 30, gold 90, platinum 300) and grade;
unlocks keep Sony's earned time and the external id
`<accountId>:<npCommunicationId>:<trophyId>`, so nothing counts twice.
Unlocks are imported and sync quietly, like Steam's. Nothing is ever written
to Sony.

**Consequences.** The route is unofficial: Sony can change or block it, and
the app says so where the player connects. The connection lasts about two
months, then the player pastes a fresh code; the dialog and the status say
when. A first import of a large library takes a while by design (two requests
per game, five seconds apart). `tests/test_psn.py` runs against a fake PSN and
a fake credential store; nothing in the tests reaches Sony.
