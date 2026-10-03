#!/usr/bin/env bash
# The Linux update, end to end, with real AppImages and nothing published:
# OLD runs from a scratch home and finds NEW in a manifest served on this
# computer, Install update replaces the file and the new version starts on its
# own. Only this script's own processes are stopped.
#
#   scripts/test-update-cycle-linux.sh OLD.AppImage NEW.AppImage NEW_VERSION
set -u
OLD="$1"; NEW="$2"; WANT="$3"
T="$(mktemp -d)"; SRV=8797; PORT=8799; FAILED=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAILED=1; }

mkdir -p "$T/apps" "$T/srv" "$T/home/.config/openachievements"
cp "$OLD" "$T/apps/GreycellAchievements.AppImage"; chmod 755 "$T/apps/GreycellAchievements.AppImage"
cp "$NEW" "$T/srv/new.AppImage"
SHA="$(sha256sum "$NEW" | cut -d' ' -f1)"; SIZE="$(stat -c %s "$NEW")"
cat > "$T/srv/latest.json" <<EOF
{"version": "$WANT", "notes": "test", "platforms": {"linux-x86_64":
 {"version": "$WANT", "url": "http://127.0.0.1:$SRV/new.AppImage", "sha256": "$SHA", "size": $SIZE}}}
EOF
echo "{\"update\": {\"manifest\": \"http://127.0.0.1:$SRV/latest.json\"}}" > "$T/home/.config/openachievements/machine.json"

python3 -m http.server --bind 127.0.0.1 "$SRV" --directory "$T/srv" >/dev/null 2>&1 &
SERVER=$!
export HOME="$T/home" XDG_CONFIG_HOME="$T/home/.config" XDG_DATA_HOME="$T/home/.local/share" XDG_CACHE_HOME="$T/home/.cache"
export GREYCELL_ACHIEVEMENTS_PORT=$PORT GREYCELL_ACHIEVEMENTS_NO_BROWSER=1 APPIMAGE_EXTRACT_AND_RUN=1
unset DISPLAY WAYLAND_DISPLAY
URL="http://127.0.0.1:$PORT"
token() { curl -s "$URL/" | grep -o 'oa-local-token" content="[^"]*"' | cut -d'"' -f3; }
up() { for _ in $(seq 1 120); do curl -s -o /dev/null "$URL/" && return 0; sleep 0.5; done; return 1; }

"$T/apps/GreycellAchievements.AppImage" --background >"$T/old.txt" 2>&1 &
APP=$!
up && pass "the old version started" || fail "the old version did not start"
STATUS="$(curl -s "$URL/v1/local/update?force=true")"
echo "$STATUS" | grep -q "\"version\":\"$WANT\"" && echo "$STATUS" | grep -q '"installable":true' \
  && pass "it offers $WANT as installable" || fail "no installable $WANT: $STATUS"
curl -s -X POST -H "X-OA-Token: $(token)" "$URL/v1/local/update/install" >/dev/null
for _ in $(seq 1 120); do kill -0 $APP 2>/dev/null || break; sleep 0.5; done
kill -0 $APP 2>/dev/null && { fail "the old version did not quit"; kill $APP; } || pass "the old version quit"
NOW=""
for _ in $(seq 1 120); do
  NOW="$(curl -s "$URL/v1/local/update" | grep -o '"current":"[^"]*"' | cut -d'"' -f4)"
  [ "$NOW" = "$WANT" ] && break; sleep 0.5
done
[ "$NOW" = "$WANT" ] && pass "the new version is running ($NOW)" || fail "running version: '$NOW'"
[ "$(sha256sum "$T/apps/GreycellAchievements.AppImage" | cut -d' ' -f1)" = "$SHA" ] \
  && pass "the AppImage file is the published one" || fail "the AppImage file was not replaced"
[ -x "$T/apps/GreycellAchievements.AppImage" ] && pass "and it is executable" || fail "not executable"
ls -A "$T/apps" | grep -q '^\.' && fail "something half-placed was left beside it" || pass "nothing left beside it"
curl -s -X POST -H "X-OA-Token: $(token)" "$URL/v1/local/app/quit" >/dev/null
for _ in $(seq 1 40); do curl -s -o /dev/null "$URL/" || break; sleep 0.5; done
curl -s -o /dev/null "$URL/" && fail "the new version did not quit" || pass "the new version quit from the page"

kill $SERVER 2>/dev/null
rm -rf "$T"
[ $FAILED = 0 ] && echo "RESULT: all checks passed" || echo "RESULT: FAILED"
exit $FAILED
