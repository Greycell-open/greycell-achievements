#!/usr/bin/env bash
# The Linux app: dist/GreycellAchievements-<version>-<arch>.AppImage, a copy
# under the stable name, and dist/linux-<arch>.json, this build's entry for
# latest.json's "platforms" (scripts/add-platform.py puts it there).
#
#   PYTHON=/path/to/python3.12 scripts/build-linux.sh [--base-url URL]
#
# PYTHON must have tkinter (the popup) and should be a python-build-standalone
# interpreter (what `uv python install 3.12` gives): it is built against an
# old glibc, so the AppImage runs on any current distribution and the Steam
# Deck, unlike one built with a new distribution's own Python. Runs on the
# architecture it builds for: x86_64 here, aarch64 on an ARM machine.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="https://greycell.app/downloads/greycell-achievements"
while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) BASE_URL="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python3}"
ARCH="$(uname -m)"
KEY="linux-$([ "$ARCH" = aarch64 ] && echo arm64 || echo "$ARCH")"
BUILD=.build/linux
VERSION="$("$PYTHON" -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
TOOL_VERSION=1.9.0                         # appimagetool, pinned; checked against TOOL_SHA256 when set

"$PYTHON" -c 'import tkinter' || { echo "this Python has no tkinter: the popup needs it" >&2; exit 1; }
if [ ! -x "$BUILD/venv/bin/python" ]; then
  "$PYTHON" -m venv "$BUILD/venv"
  "$BUILD/venv/bin/python" -m pip install -q --disable-pip-version-check --require-hashes --no-deps -r requirements/server.lock
  "$BUILD/venv/bin/python" -m pip install -q --disable-pip-version-check "pyinstaller==6.16.0"
fi
# The Linux popup renders its text and pictures with Pillow (popup_linux.py).
"$BUILD/venv/bin/python" -m pip install -q --disable-pip-version-check "pillow==12.3.0"
"$BUILD/venv/bin/python" -m pip install -q --disable-pip-version-check --no-deps --force-reinstall --no-build-isolation .

rm -rf "$BUILD/dist" "$BUILD/AppDir"
"$BUILD/venv/bin/python" -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name greycell-achievements \
  --collect-data openachievements \
  --collect-submodules openachievements \
  --collect-submodules uvicorn \
  --hidden-import PIL.ImageDraw --hidden-import PIL.ImageFont \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  "$PWD/scripts/exe_entry.py"

APPDIR="$BUILD/AppDir"
mkdir -p "$APPDIR/usr"
cp -a "$BUILD/dist/greycell-achievements" "$APPDIR/usr/bin"
# PyInstaller also copies the build machine's own X11 and C and C++ runtime libraries.
# Every desktop has them, and the build machine's copies would demand its newer
# glibc, so the player's own are used. Without X11 the popup falls back to the
# desktop's notification.
for lib in libX11.so.6 libXau.so.6 libXdmcp.so.6 libXext.so.6 libxcb.so.1 libbsd.so.0 libmd.so.0 libgcc_s.so.1 \
           libstdc++.so.6; do
  rm -f "$APPDIR/usr/bin/_internal/$lib"
done
# Refuse a bundle that needs a newer glibc than 2.28 (Debian 10, Ubuntu 18.10):
# it would not start on older systems, or on the Steam Deck after a downgrade.
if command -v objdump >/dev/null; then
  FLOOR="$(find "$APPDIR" -type f \( -name '*.so*' -o -name greycell-achievements \) -exec objdump -T {} \; 2>/dev/null \
    | grep -o 'GLIBC_[0-9.]*' | sort -t. -k2,2n -k3,3n -u | tail -1)"
  case "$FLOOR" in
    GLIBC_2.[0-9]|GLIBC_2.[0-9].*|GLIBC_2.1[0-9]*|GLIBC_2.2[0-8]*) echo "glibc floor: $FLOOR" ;;
    *) echo "the bundle needs $FLOOR; find the library that asks for it and leave it out" >&2; exit 1 ;;
  esac
else
  echo "objdump not found: the glibc floor was not checked" >&2
fi
cp src/openachievements/web/logo-512.png "$APPDIR/greycell-achievements.png"
cp src/openachievements/web/logo-512.png "$APPDIR/.DirIcon"
cat > "$APPDIR/greycell-achievements.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Greycell Achievements
Comment=Every game deserves achievements
Exec=greycell-achievements
Icon=greycell-achievements
Terminal=false
Categories=Game;Utility;
StartupWMClass=greycell-achievements
X-AppImage-Version=$VERSION
EOF
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/greycell-achievements" "$@"
EOF
chmod 755 "$APPDIR/AppRun"

TOOL="$BUILD/appimagetool-$TOOL_VERSION-$ARCH.AppImage"
if [ ! -x "$TOOL" ]; then
  curl -fsSL -o "$TOOL.part" "https://github.com/AppImage/appimagetool/releases/download/$TOOL_VERSION/appimagetool-$ARCH.AppImage"
  if [ -n "${TOOL_SHA256:-}" ]; then echo "$TOOL_SHA256  $TOOL.part" | sha256sum -c - >/dev/null; fi
  chmod 755 "$TOOL.part" && mv "$TOOL.part" "$TOOL"
fi
if ! command -v desktop-file-validate >/dev/null; then
  # appimagetool insists on the desktop-file-utils validator. Where it is not
  # installed, this stand-in checks the keys our entry must have.
  mkdir -p "$BUILD/bin"
  cat > "$BUILD/bin/desktop-file-validate" <<'CHECK'
#!/bin/sh
for key in '^\[Desktop Entry\]$' '^Type=Application$' '^Name=.' '^Exec=.' '^Icon=.'; do
  grep -q "$key" "$1" || { echo "$1: missing $key" >&2; exit 1; }
done
CHECK
  chmod 755 "$BUILD/bin/desktop-file-validate"
  export PATH="$PWD/$BUILD/bin:$PATH"
fi
mkdir -p dist
OUT="dist/GreycellAchievements-$VERSION-$ARCH.AppImage"
# No FUSE needed: the tool unpacks itself and runs.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$TOOL" --no-appstream "$APPDIR" "$OUT" >"$BUILD/appimagetool.log" 2>&1 \
  || { tail -20 "$BUILD/appimagetool.log" >&2; exit 1; }
chmod 755 "$OUT"
cp "$OUT" "dist/GreycellAchievements-$ARCH.AppImage"

SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
SIZE="$(stat -c %s "$OUT")"
"$PYTHON" - "$KEY" "$VERSION" "$BASE_URL/$(basename "$OUT")" "$SHA" "$SIZE" <<'EOF'
import json, sys
key, version, url, sha, size = sys.argv[1:]
with open(f"dist/{key}.json", "w", encoding="utf-8") as f:
    json.dump({key: {"version": version, "url": url, "sha256": sha, "size": int(size)}}, f, indent=1)
EOF
for f in "$OUT" "dist/GreycellAchievements-$ARCH.AppImage" "dist/$KEY.json"; do
  printf '%s  %s bytes  sha256 %s\n' "$(basename "$f")" "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -d' ' -f1)"
done
