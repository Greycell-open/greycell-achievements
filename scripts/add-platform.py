"""Add a build's entry (dist/linux-x86_64.json from build-linux.sh) to the
manifest's "platforms", after build-setup.ps1 has written dist/latest.json.

    python scripts/add-platform.py dist/latest.json dist/linux-x86_64.json

Refuses an entry whose version is not the manifest's: one release, one version.
"""
import json
import os
import sys


def main(manifest_path: str, *entries: str) -> int:
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    platforms = manifest.setdefault("platforms", {})
    for path in entries:
        with open(path, encoding="utf-8") as f:
            for key, entry in json.load(f).items():
                if entry.get("version") != manifest.get("version"):
                    print(f"{path}: {key} is version {entry.get('version')}, the manifest {manifest.get('version')}",
                          file=sys.stderr)
                    return 1
                platforms[key] = entry
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, ensure_ascii=False)
    os.replace(tmp, manifest_path)
    print(f"{manifest_path}: platforms {sorted(platforms)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], *sys.argv[2:]))
