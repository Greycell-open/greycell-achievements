#!/bin/sh
# Regenerate requirements/*.lock inside the pinned base image, so the hashes
# are the ones the Docker build will verify. Needs Docker. Run from the
# repository root, then commit the changed lock files.
set -eu
BASE=$(sed -n 's/^FROM \(python[^ ]*\) AS base$/\1/p' Dockerfile)
[ -n "$BASE" ] || { echo "no 'FROM python... AS base' line in Dockerfile" >&2; exit 1; }
docker run --rm -v "$PWD/requirements:/req" -w /req "$BASE" sh -c '
  pip install -q --root-user-action=ignore pip-tools &&
  for set in server dev; do
    pip-compile -q --generate-hashes --strip-extras --allow-unsafe --no-header -o "$set.lock" "$set.in"
  done'
