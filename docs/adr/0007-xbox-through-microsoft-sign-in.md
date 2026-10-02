# ADR 0007: Xbox achievements through Microsoft sign-in, read only

Date: 2026-10-02. Status: accepted (owner decision: "go for xbox").

**Context.** Xbox Live has no public achievement API for other apps, but it
does accept a Microsoft account sign-in from any app registered with
Microsoft: the Microsoft token is exchanged for an Xbox Live user token and an
XSTS token, which authorise reading the player's own achievements. Community
libraries and game launchers sign in this way. The alternatives were a
third-party relay service (it holds the access and sees the data, against
local-first) or borrowing the client id of Microsoft's own Xbox app (that is
impersonating Microsoft's client, which this project does not do).

**Decision.** Each installation uses its owner's own app registration: a free
"Personal Microsoft accounts" registration in the Azure portal with the
public-client redirect `http://localhost:8788/v1/local/xbox/return` and public
client flows allowed. The player pastes its Application (client) ID once (it
is not a secret). Signing in is Microsoft's page, an authorization code with
PKCE and a single-use state, asking only `XboxLive.signin offline_access`; the
app never sees the password. Only Microsoft's refresh token is kept, in the
operating system's credential store; the Xbox tokens live in memory.
`adapters/xbox.py` reads the title history, then each new or changed title's
achievements (one request for Xbox One, Series and PC titles; two for Xbox
360 titles, on the older contract), at most one request every ten seconds and
waiting out any Retry-After. Games are `xbox-<titleId>`, achievement points
are gamerscore, unlocks keep Xbox's time and the external id
`<xuid>:<titleId>:<achievementId>`, and they sync quietly. Nothing is ever
written to Xbox.

**Consequences.** Setting up Xbox costs the player about five minutes in the
Azure portal; the dialog walks through it. The Xbox Live endpoints used are
not published for general use and can change. `tests/test_xbox.py` runs
against a fake Microsoft and a fake Xbox Live; nothing in the tests reaches
them.
