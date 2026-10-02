# ADR 0003: What belongs to a computer lives outside the profile folder

Date: 2026-09-30. Status: accepted.

**Context.** The profile folder must be copyable, exportable and shareable
between a user's machines. Some data is inherently per machine.

**Decision.** The machine config (`%APPDATA%/OpenAchievements`,
`~/.config/openachievements`, or `$OPENACHIEVEMENTS_CONFIG`) holds: this
computer's device id per profile, the sync server and token, the sync cursor
and acknowledged ids, import markers, and executable paths. The profile
folder holds only history and packs.

**Consequences.** A profile copied to a new machine registers a new device
instead of impersonating the old one. Tokens never travel in an export.
Executable paths, which can name a user's folders, never reach the server.
Losing the machine config costs a re-upload and re-login, never data. Tokens
are in a file readable only by the owner; moving them to the OS credential
store is follow-up work.
