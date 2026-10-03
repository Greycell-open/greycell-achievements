# Greycell Achievements

**Every game deserves achievements.** Greycell Achievements is one
achievement library for every game you play: Steam, PlayStation, Xbox, GOG,
RetroAchievements, and the games no platform tracks (DRM-free copies, physical installs, source
ports, emulators, your own challenges).

- **Local first.** Your profile is a folder of plain JSON files on your own
  computer. It works offline, forever, with no account.
- **Yours to move.** Export it, back it up, copy it to another machine, point
  it at a different server. A server is a sync copy, never the only one.
- **Yours, not ours.** Install it and your achievements stay on your
  computer. Greycell hosts no accounts and keeps no copy. To sync your own
  PCs you can run the small sync server yourself, in one container.
- **Get it** at [greycell.app/achievements](https://greycell.app/achievements/):
  download `GreycellAchievementsSetup.exe` (Windows 10 and 11, installs for
  your user only, no administrator), run it, and link your accounts. It updates
  itself. From a source checkout, double-click `run.bat` instead.
- **Free, forever.** AGPL-3.0. No ads, no premium tier, no selling your data.
- **Honest.** Every unlock says where it came from: imported from a platform,
  or detected from a game you played or its save. There is no unlock button:
  an achievement counts only when something the game recorded says so.

Status: **1.0.** Local profiles, achievement packs, progress
achievements, standalone executable tracking, save-file achievements,
RetroAchievements, Steam, PlayStation, Xbox and GOG imports, games linked
across sources, sync with multiple devices, the Windows tray app and its live
dashboard, export and restore all work and are tested. Not yet built: save
transfer, pack signing, a pack registry.

## Quick start (local only)

Requires Python 3.12 or later. The core needs nothing outside the standard
library.

```bash
pip install ".[server]"          # from a checkout; [server] adds the web UI
openachievements profile create "Your name"
openachievements pack install examples/achievement-packs/cave-story-community
openachievements achievement list
openachievements serve           # the library in your browser, on this computer only
```

Your profile lives in `~/OpenAchievements/profiles/<id>/`
(or `$OPENACHIEVEMENTS_HOME`). Nothing leaves your computer until you connect
a server.

### The Windows app

`GreycellAchievements.exe` is this package and its own Python in one file,
built with `powershell -ExecutionPolicy Bypass -File scripts\build-exe.ps1`
from the same locked packages as `run.bat`. Double-click it: it opens your
library in the browser and sits quietly in the system tray, watching running
games, saves and Steam. The first run names your profile after your Windows
user. Right-click the tray icon for Open library, Start with Windows (starts
it in the tray at login, no browser) and Quit. Run it with a command
(`GreycellAchievements.exe save check`) to use the command line; its own
output goes to `app.log` in `%APPDATA%\OpenAchievements`.

### Installing and updating on Windows

`GreycellAchievementsSetup.exe` (Inno Setup, `installer/GreycellAchievements.iss`,
built with `scripts\build-setup.ps1`) installs the app for the current user in
`%LOCALAPPDATA%\Programs\Greycell Achievements`, with no administrator prompt,
a Start menu entry and an uninstaller in Windows Settings. Your achievements
are in `%USERPROFILE%\OpenAchievements` (settings in `%APPDATA%\OpenAchievements`)
and installing, updating or uninstalling
never touches them.

Updates work like VLC's. Once a day the app reads a small manifest from
greycell.app (the version, the installer's address, its SHA-256 and size, and
what's new). When a newer version exists and no game is running, a dialog
asks once that day whether to download and install it; the dashboard shows
an **Install update** button too. Yes downloads the installer, deletes it
unless its size and SHA-256 match the manifest, runs it with only its
progress window, and the new version starts in the tray. `openachievements
update check off` stops the daily look.

### PlayStation

**PlayStation** in the dashboard connects a PSN account, read only. Sony has
no public sign-in for apps, so you paste the sign-in code its website uses
(the dialog shows where); the session is kept only in Windows Credential
Manager and your password is never asked for. A spare PSN account can read
your main account's trophies if they are visible to others. Every game with
trophies comes in, with Sony's dates and PSN's trophy points, and new trophies
follow within minutes. This route is unofficial (ADR 0006): Sony could change
it. The connection lasts about two months, then paste a fresh code.

### Xbox

**Xbox** in the dashboard signs in with Microsoft, read only. Microsoft needs
the app registered once (free, about five minutes in the Azure portal; the
dialog shows the steps), then you sign in on Microsoft's own page. Every game
with achievements comes in (Xbox Series, One, PC and 360), with gamerscore and
Xbox's dates, and new ones follow within minutes. The session is kept only in
Windows Credential Manager (ADR 0007).

### GOG

**GOG** in the dashboard needs only your GOG username (or your profile link).
GOG lets no other app sign you in, so it reads your public GOG profile: every
game with achievements, with GOG's dates, and new ones follow within minutes
of GOG Galaxy syncing. Your profile must be visible to everyone (gog.com,
Settings, Privacy). Nothing secret is kept. This route is unofficial (ADR
0008): GOG could change its pages.

### RetroAchievements

**RetroAchievements** in the dashboard takes your username and your Web API
key (from your RetroAchievements settings page). It is RetroAchievements'
official API; the key is kept only in Windows Credential Manager. Every game
you have started comes in with its full set and RetroAchievements points,
softcore and hardcore kept apart, and new unlocks follow within about two
minutes (ADR 0009).

Sign-ins and keys are kept in Windows Credential Manager. Run from source on
Linux, they go to the desktop keyring (`secret-tool`); with no keyring the app
refuses to keep them, unless you set `OPENACHIEVEMENTS_FILE_SECRETS=1` to accept
a file only you can read.

All five accounts sit under **Link accounts** at the top of the dashboard.

### Try it: the test game and Platinum

`GreycellAchievements.exe testgame` plays one level of a five-level test game
by writing its save file; the app picks the save up and the achievement pops,
like any game. The fifth level completes it and brings the Platinum popup.
`testgame reset` takes the unlocks back to test again; `testgame remove` takes
the test game out of your library. Choose the unlock, rare and Platinum sounds
under **Settings, Sounds** in the dashboard, or use your own WAV file.

A rare achievement, one that fewer than 1% of players have (5% or 10% if you
prefer), gets a silver popup that says how rare it is, and its own sound.
Rarity comes from Steam's public unlock percentages and from RetroAchievements,
kept on your PC and refreshed weekly. Popups show the achievement's own picture.

### Running it on Windows

Double-click `run.bat`. The first run sets up a private `.venv` from the
locked packages and asks your name for your profile; after that it opens your
library at http://127.0.0.1:8788. While that window is open the library also
watches in the background (running games, save progress, Steam); there is no
separate watcher window any more. Any command works through it:
`run.bat catalog install 400`, `run.bat save check`. `catalog install` takes
any Steam app id: a game the catalogue crawl has not reached yet is read from
Steam's public pages there and then.

What you get: your profile page with your stats and your games on shelves
(Playing, Backlog, Wishlist, Completed, Dropped); **+ Add games** searches the
Steam catalogue; **Link Steam** follows the Steam account signed in on this
computer; and the watcher, while it runs, does the rest:

- **Steam games:** the Steam button, "Sign in through Steam" (on Steam's own
  page; the app never sees your password and never uses a key, ADR 0005). Every
  game that account has played on this computer comes in with when you last
  played it, and unlocks appear as Steam records them, all read from Steam's
  own files here. Steam shows a whole library, including games never played,
  only to an API key, so games you own but never played here are not listed;
  add them from **+ Add games**.
- **Games found by their saves:** save folders named like a game, with save
  files in them, add that game, dated by its newest save. Games sort by
  recently played.
- **Any other copy of a game:** a running program whose folder holds game
  engine files and is named like a catalogued game is recognised, put on the
  Playing shelf, and its play time counted; folders that look like its saves are
  noted. `openachievements autodetect off` turns this off.
- **Save files:** with rules for a game, its saves unlock achievements (below).

### Updates (from source)

A source install looks at the project's GitHub releases at most once a day (one
request, nothing about you in it). When a newer version is out, the page says
so, and the next start of `run.bat` installs it before opening.

```bash
openachievements update              # is there a newer version?
openachievements update install      # install it now
openachievements update check off    # stop the daily look
openachievements update repo owner/name   # where releases come from
```

A copy that runs from a git checkout is never installed over: pull it instead.

### Trying it on Windows without installing anything

Double-click `dev.bat`. The first run builds a private `.venv` from the locked
packages and a dev profile with the example pack, then opens the library in
your browser. It never touches a real profile.

```bat
dev.bat                   :: the web library on http://127.0.0.1:8788
dev.bat sync-server       :: also a local sync server on http://127.0.0.1:8787
dev.bat server connect http://127.0.0.1:8787 dev --register
dev.bat sync
dev.bat reset             :: throw the dev profile away and start over
```

### The unlock popup

When something unlocks while you play, a small rounded card with the
achievement's picture slides up at the bottom centre of the screen with a soft
sound (Echo by default: a short climb into a round tone that echoes back), then
fades after five seconds. A rare achievement gets a silver card and its own
sound. It never takes focus and clicks pass through it, so the game keeps your
input. Steam, Xbox, GOG and RetroAchievements unlocks are left to their own
popups unless you ask; PlayStation trophies never pop up on the PC.

```bash
openachievements notify test          # show one now
openachievements notify off           # or on
openachievements notify sound off     # the card without a sound
openachievements notify steam on      # also for Steam unlocks
```

It shows over windowed and borderless games. A game in exclusive fullscreen
owns the screen and hides it: Steam's and Xbox's popups draw inside the game,
which Greycell Achievements does not do.

## Roadmap

What is coming next (Linux, Steam Deck, Windows on ARM, Linux on ARM, macOS on
Intel and Apple Silicon, more stores, and more) is on
[greycell.app](https://greycell.app/apps/greycell-achievements-roadmap.html),
and behind the **Roadmap** button in the app.

## Your games, from everywhere

```bash
# RetroAchievements: your username and your Web API key (from your RA settings)
openachievements import retroachievements YourName          # asks for the key

# Steam: no key. Sign in through Steam in the page, or follow this PC's Steam
openachievements steam link                                 # 'watch' keeps it live

# A game no platform tracks: point at its executable
openachievements game register-executable cave-story "D:/Games/Cave Story/Doukutsu.exe"
openachievements watch           # notices when it runs; play sessions unlock rule achievements

# The same game from two places, shown as one
openachievements game link steam-504230 celeste
```

Imports are read-only and idempotent: run them as often as you like, and
nothing is ever written back to Steam or RetroAchievements. The RetroAchievements
key is read from `OA_RA_KEY` or prompted for, never stored in the profile.

## Games no store is watching: save-file achievements

A game's progress is in its save file, so a pack can say which values mean an
achievement. It works for any copy of a game, and nothing is checked or bypassed.

```json
"saves": [{"id": "main", "root": "LOCALLOW", "path": "Studio/Game", "pattern": "slot*.json", "format": "json"}]
```
```json
"rules": [{"signal": "save", "save": "main", "field": "progress.chapter", "op": ">=", "value": 3}]
```

Roots are fixed known folders (`LOCALAPPDATA`, `LOCALLOW`, `APPDATA`,
`DOCUMENTS`, `SAVED_GAMES`); a pack cannot point anywhere else, and nothing is
read until you allow each location on your machine with `save allow`, which
shows the exact folder.
Formats: `json`, `ini`, `text` (with `contains` rules), and `gvas` (Unreal
Engine `.sav` files, read into the same dotted fields: `SavedIntMap.Total_NumItemsCrafted`;
a key that itself contains dots works too). Operators: `== != >= <= > <
exists contains`. All of an achievement's rules must hold in the same save slot.

Some games' rules ship with the app (`src/openachievements/saverules/`, one file
per game, matched by achievement name). They are added to the game's list as
soon as it is in your library, and their save folder is allowed once,
automatically, because they come with the app rather than from another person;
`save revoke` withdraws that for good. Only what a real save was seen to prove
is mapped, and each file lists what it could not map and why:

- **Luck be a Landlord**: all 186, from the game's own `achievements_unlocked` list.
- **Hell Clock**: all 71, from the game's own list of Steam achievement names.
- **SILENT HILL: Townfall**: "Welcome To St. Amelia" (end of chapter 1, from the
  slot's mission number), "From Scratch" and "Fleeting Visit"; the rest of the
  story is not mapped because mission numbers do not follow the story.

```bash
openachievements save check              # each save's folder, whether allowed, and what each file satisfies
openachievements save allow my-pack main # let this machine read that folder
openachievements save locate my-pack main "D:/Games/Game/saves"   # a copy that saves elsewhere
openachievements watch                   # running games and saves, unlocking as you play
```

Unlocks carry `save-derived` provenance and the save's file name and hash as
evidence; the save itself is never copied or uploaded. Progress shows up when
the game saves, not the instant it happens.

## The public Steam catalogue

Every Steam game's achievements, gathered without an account or API key from
what Steam shows anyone: the documented keyless unlock-rate endpoint (which
also says whether a game has achievements) and each game's public community
achievement page. Each game becomes an ordinary pack, `steam-<appid>`.

```bash
openachievements catalog crawl steam --apps steam-apps.txt   # appid[TAB name] per line; resumes
openachievements catalog list "hollow knight"
openachievements catalog install 367520                     # into your profile, like any pack
```

It is paced at one request per 1.5 s per Steam host, so a full pass over about
170,000 apps takes about three days, and it resumes where it stopped. The
public page blanks hidden achievements' descriptions; where Steam's keyless
achievement schema (`IPlayerService/GetGameAchievements`) answers, it is used
instead and gives hidden achievements their real text and Steam's own hidden
flag, and library games listed from the public page are filled in from it in
the background. In the library a hidden achievement shows as "Hidden
achievement"; click it to read it, click again to hide it, or tick "Show hidden
achievements". A public profile never carries the hidden text. Re-crawling keeps every achievement's id, so unlocks
survive renames. Steam's own keyless app list was retired, so the crawl takes
the list of apps as a file. `scripts/check_catalog.py` compares a catalogue to
the schemas your Steam client has cached.

## Sync between devices

```bash
openachievements server connect https://achievements.example.com yourname --register
openachievements sync

# on another computer
openachievements profile restore https://achievements.example.com yourname
```

Unlocks made offline sync when you are back. The same unlock never appears
twice. See [docs/specifications/sync-protocol.md](docs/specifications/sync-protocol.md).

## Self-hosting

```bash
docker compose up -d             # one container, one SQLite file in the oa-data volume
```

Put it behind HTTPS (a Caddy example is in `deploy/`). Registration defaults
to `first-only`: the first account is yours and nobody else can sign up.
Full guide: [docs/self-hosting/README.md](docs/self-hosting/README.md).

## Writing achievement packs

A pack is a folder with `pack.json` and `achievements.json`. Achievements can
count progress to a target, or unlock from signals like "launched the game",
"played for an hour" or a value in a save. See
[docs/specifications/achievement-pack.md](docs/specifications/achievement-pack.md)
and the example in `examples/achievement-packs/`.

## How it is built

- `src/openachievements/` core: events, reducer, profile folder, SQLite index, packs, CLI, sync client
- `src/openachievements/adapters/` RetroAchievements, Steam, standalone executables
- `src/openachievements/server/` FastAPI sync server (OpenAPI at `/docs`)
- `src/openachievements/web/` the web library, used by both the server and `openachievements serve`
- `docs/adr/` the decisions and why

```bash
pip install ".[dev]"
pytest
```

The same suite in the exact environment the server ships in (pinned base image,
hash-locked packages), then a start-up check of the server image:

```bash
scripts/verify.sh      # needs Docker; exits non-zero on any failure
scripts/lock.sh        # after editing requirements/*.in or the base digest
```

## Licence

Code: AGPL-3.0-or-later (see `LICENSE`). Specifications and example packs:
CC0-1.0. Platform names and artwork belong to their owners. The app fetches a game's
banner and its achievement icons from the platform's own image host once and
keeps a private copy on your PC for display; it never shares or republishes them.
