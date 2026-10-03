"""What the app sends over the network without being asked, and its switches.

Everything else goes online only because the player asked: linking an account
(Steam, PlayStation, Xbox, GOG, RetroAchievements) reads that account, adding a
game fetches its achievement list, a sync server is one the player set up.
Without being asked, the app:

  - looks at greycell.app's update manifest at start and once a day ("updates");
  - fetches achievement pictures and game banners from the platforms' image
    hosts, once each, then keeps them on this computer ("pictures");
  - asks Steam for each Steam game's unlock percentages, for rare
    achievements ("rarity").

None of these requests carries anything about the player but the address
every request has, and which game it is about. Each can be switched off; what
was already fetched stays usable. The installer and greycell.app say the same.
"""
from __future__ import annotations

SWITCHES = ("updates", "pictures", "rarity")


def settings(config: dict) -> dict:
    """{"updates", "pictures", "rarity"}: True unless the player switched it off."""
    network = config.get("network") or {}
    return {"updates": (config.get("update") or {}).get("check", True) is not False,
            "pictures": network.get("pictures", True) is not False,
            "rarity": network.get("rarity", True) is not False}


def change(config_store, **switches: bool) -> dict:
    unknown = set(switches) - set(SWITCHES)
    if unknown:
        raise ValueError(f"no such switch: {', '.join(sorted(unknown))}")

    def apply(config: dict) -> None:
        if "updates" in switches:
            config.setdefault("update", {})["check"] = bool(switches["updates"])
        for key in ("pictures", "rarity"):
            if key in switches:
                config.setdefault("network", {})[key] = bool(switches[key])
    config_store.edit(apply)
    return settings(config_store.load())


def allowed(config_store, switch: str) -> bool:
    try:
        return settings(config_store.load())[switch]
    except Exception:  # noqa: BLE001 - an unreadable settings file keeps the defaults
        return True
