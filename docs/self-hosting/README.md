# Self-hosting Open Achievements

The server is one Python process and one SQLite file. It stores copies of
your devices' events so they can sync; every device keeps the full history
itself, so losing the server loses nothing that a device still has.

## With Docker Compose

```bash
git clone <this repository> open-achievements && cd open-achievements
docker compose up -d
curl http://127.0.0.1:8787/healthz        # ok
```

Data lives in the `oa-data` Docker volume as `openachievements.sqlite`. Back
it up with the server stopped, for example
`docker compose stop && docker run --rm -v open-achievements_oa-data:/data -v "$PWD":/out alpine cp /data/openachievements.sqlite /out/`
(the volume name is prefixed with your compose project folder's name).

## HTTPS

Clients refuse plain `http://` for anything except `localhost`, so a server
other people reach needs HTTPS. With Caddy:

```
achievements.example.com {
	reverse_proxy 127.0.0.1:8787
}
```

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `OA_REGISTRATION` | `open` (compose file: `first-only`) | `open` anyone can sign up, `first-only` only the first account, `closed` nobody new |
| `OA_MAX_EVENTS` | `250000` | events stored per profile; over the limit, uploads are refused with `quota_exceeded`, never dropped silently |
| `OA_MAX_BATCH` | `500` | events per sync request |
| `OA_LOGIN_ATTEMPTS` | `10` | failed sign-ins per username per 15 minutes |
| `OA_DATA_DIR` | `./data` (`/data` in Docker) | where the database lives |
| `OA_PORT` | `8787` | listening port |

## Moving between servers

A profile is not tied to a server. Greycell hosts none: your achievements live
on your computer, and a server is only a way to sync your own machines. To move
to another server: run it, then on each device

```bash
openachievements server connect https://your.server yourname --register
openachievements sync
```

The first device uploads its whole history; the others follow. Nothing on the
old server is needed.

## What the server knows

Events: which games and achievements, when, from which source. Not your saves,
not your executables or their paths, not your platform API keys. Profiles are
private unless the owner turns on a public profile. Deleting an account
removes everything the server holds for it and leaves every device's local
copy alone.

## Conformance

`tests/test_sync.py` drives the sync protocol end to end against the real app.
Another server implementation is compatible if the same scenarios pass
against it.
