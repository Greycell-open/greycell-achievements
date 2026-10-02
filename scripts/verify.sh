#!/bin/sh
# One command that proves this commit works, anywhere Docker runs:
#   1. the whole test suite, inside the pinned image with the locked packages;
#   2. the server image builds, starts as its non-root user, and answers.
# Exits non-zero on the first failure. Run from the repository root.
set -eu
TAG=open-achievements:verify-$$
NAME=oa-verify-$$
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker rmi "$TAG" >/dev/null 2>&1 || true; }
trap cleanup EXIT

LOG=$(mktemp)
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker rmi "$TAG" >/dev/null 2>&1 || true; rm -f "$LOG"; }

echo "== tests (docker build --target test, never from cache)"
if ! docker build --target test --no-cache-filter test --progress=plain . >"$LOG" 2>&1; then
  tail -40 "$LOG" >&2; echo "test suite failed" >&2; exit 1
fi
grep -Eo "[0-9]+ passed[^=]*" "$LOG" | tail -1

echo "== server image"
docker build -q -t "$TAG" . >/dev/null
docker run -d --name "$NAME" -p 127.0.0.1::8787 "$TAG" >/dev/null
PORT=$(docker port "$NAME" 8787/tcp | head -1 | sed 's/.*://')
i=0
until [ "$(curl -s "http://127.0.0.1:$PORT/healthz" || true)" = ok ]; do
  i=$((i + 1)); [ $i -lt 30 ] || { echo "server never answered /healthz" >&2; docker logs "$NAME" >&2; exit 1; }
  sleep 1
done
echo "healthz: ok on port $PORT"
USER=$(docker inspect --format '{{.Config.User}}' "$NAME")
[ "$USER" = oa ] || { echo "server runs as '$USER', expected oa" >&2; exit 1; }
echo "runs as: $USER"
echo "== verified"
