"""Check the Steam catalogue against Steam's own achievement schemas.

The Steam client caches each played game's schema on disk as binary
KeyValues in `<Steam>/appcache/stats/UserGameStatsSchema_<appid>.bin`. For
every catalogued game with a cached schema, this compares achievements one to
one on (icon file, English name) as multisets, so a duplicate or an extra
counts, and checks descriptions and the hidden flag.

    python scripts/check_catalog.py "C:/Program Files (x86)/Steam" "%LOCALAPPDATA%/OpenAchievements/catalog"

Read-only. Exits 0 always; the numbers are the result.
"""
from __future__ import annotations

import json
import struct
import sys
from collections import Counter
from pathlib import Path


def parse_kv(b: bytes, i: int = 0) -> tuple[dict, int]:
    out = {}
    while True:
        t = b[i]
        i += 1
        if t in (8, 11):
            return out, i
        j = b.index(0, i)
        k = b[i:j].decode("utf-8", "replace")
        i = j + 1
        if t == 0:
            v, i = parse_kv(b, i)
        elif t == 1:
            j = b.index(0, i)
            v = b[i:j].decode("utf-8", "replace")
            i = j + 1
        elif t in (2, 4, 6):
            v = struct.unpack_from("<i" if t == 2 else "<I", b, i)[0]
            i += 4
        elif t == 3:
            v = struct.unpack_from("<f", b, i)[0]
            i += 4
        elif t in (7, 10):
            v = struct.unpack_from("<Q" if t == 7 else "<q", b, i)[0]
            i += 8
        else:
            raise ValueError(f"KeyValues type {t} at {i}")
        out[k] = v


def schema(path: Path, appid: int) -> list[dict]:
    data, _ = parse_kv(path.read_bytes())
    found = []
    for stat in (data.get(str(appid), {}).get("stats") or {}).values():
        if isinstance(stat, dict):
            for bit in (stat.get("bits") or {}).values():
                d = bit.get("display") or {}
                found.append({"icon": (d.get("icon") or "").lower(),
                              "name": " ".join(((d.get("name") or {}).get("english") or "").split()),
                              "description": " ".join(((d.get("desc") or {}).get("english") or "").split()),
                              "hidden": str(d.get("hidden")) == "1"})
    return found


def main(steam_dir: str, catalog_dir: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from openachievements.catalog import steam as cat
    stats = Path(steam_dir) / "appcache" / "stats"
    totals = Counter()
    for appid, entry in sorted(cat.load(Path(catalog_dir)).items()):
        cached = stats / f"UserGameStatsSchema_{appid}.bin"
        if not cached.exists():
            continue
        truth = schema(cached, appid)
        if not truth:
            continue
        got = [{"icon": (a.get("icon") or "").rsplit("/", 1)[-1].lower(), "name": a["name"],
                "description": a["description"], "hidden": a.get("hidden", False)} for a in entry["achievements"]]
        key = lambda a: (a["icon"], a["name"])
        t_keys, g_keys = Counter(map(key, truth)), Counter(map(key, got))
        missing, extra = t_keys - g_keys, g_keys - t_keys
        by_key = {}
        for a in got:
            by_key.setdefault(key(a), []).append(a)
        desc_ok = desc_hidden_blank = desc_diff = hidden_ok = hidden_over = hidden_under = 0
        for a in truth:
            match = (by_key.get(key(a)) or [None]).pop(0) if by_key.get(key(a)) else None
            if match is None:
                continue
            if match["description"] == a["description"]:
                desc_ok += 1
            elif not match["description"] and a["hidden"]:
                desc_hidden_blank += 1
            else:
                desc_diff += 1
            if match["hidden"] == a["hidden"]:
                hidden_ok += 1
            elif match["hidden"]:
                hidden_over += 1
            else:
                hidden_under += 1
        exact = not missing and not extra and not desc_diff and not hidden_under
        totals.update(games=1, exact_games=int(exact), achievements=len(truth),
                      matched=len(truth) - sum(missing.values()), missing=sum(missing.values()),
                      extra=sum(extra.values()), description_exact=desc_ok,
                      description_blank_hidden=desc_hidden_blank, description_differs=desc_diff,
                      hidden_right=hidden_ok, hidden_over=hidden_over, hidden_under=hidden_under)
        if not exact:
            print(f"  {appid} {entry['pack']['games'][0]['title']}: {sum(missing.values())} missing, "
                  f"{sum(extra.values())} extra, {desc_diff} descriptions differ, {hidden_under} hidden not marked")
    print(json.dumps(dict(totals), indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:3])
