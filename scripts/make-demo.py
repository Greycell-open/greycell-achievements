"""A demo profile for screenshots: real Steam games (names, achievements and
banners from Steam's public data), made-up progress. Run it with scratch
folders, never your own:

    OPENACHIEVEMENTS_HOME=<scratch>/home OPENACHIEVEMENTS_CONFIG=<scratch>/config \\
    OA_CATALOG_DIR=<scratch>/catalog python scripts/make-demo.py

then `openachievements serve --no-watch` with the same variables.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openachievements.catalog import steam as cat  # noqa: E402
from openachievements.profile import Profile  # noqa: E402

# appid, status, share of achievements unlocked (1.0: every one, a Platinum)
GAMES = [
    (2379780, "playing", 0.62),    # Balatro
    (1145360, "playing", 0.45),    # Hades
    (367520, "playing", 0.30),     # Hollow Knight
    (1868140, "playing", 0.18),    # Dave the Diver
    (620, "completed", 1.0),       # Portal 2
    (504230, "completed", 0.84),   # Celeste
    (268910, "backlog", 0.0),      # Cuphead
    (646570, "backlog", 0.12),     # Slay the Spire
    (413150, None, 0.40),          # Stardew Valley
    (1794680, None, 0.55),         # Vampire Survivors
]


def main() -> None:
    for var in ("OPENACHIEVEMENTS_HOME", "OPENACHIEVEMENTS_CONFIG", "OA_CATALOG_DIR"):
        if not os.environ.get(var):
            raise SystemExit(f"set {var} to a scratch folder first")
    profile = Profile.create("Demo")
    index = cat.CatalogIndex(Path(os.environ["OA_CATALOG_DIR"]))
    fetch = cat.PacedFetcher(pace=1.5)
    queues = []
    for appid, status, share in GAMES:
        added = cat.add_to_profile(profile, index, appid, status=status, fetch=fetch)
        game_id = f"steam-{appid}"
        pack = profile.state()["packs"].get(game_id) or {}
        keys = [f"{game_id}:{a}" for a in pack.get("achievements", {})]
        queues.append(keys[:round(len(keys) * share)])
        print(f"{added['title']}: {len(queues[-1])} of {len(keys)}")
    # Each game's unlocks spread evenly over the same stretch of time, so every
    # game finishes together and Recent unlocks shows a mix of games.
    order = sorted((n / len(q), key) for q in queues for n, key in enumerate(q, 1))
    for _, key in order:
        profile.record_unlock(key, adapter="steam-local")


if __name__ == "__main__":
    main()
