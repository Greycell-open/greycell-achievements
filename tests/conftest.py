import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openachievements.profile import MachineConfig, Profile  # noqa: E402

EXAMPLE_PACK = Path(__file__).resolve().parents[1] / "examples" / "achievement-packs" / "cave-story-community"


@pytest.fixture(autouse=True)
def _private_credential_store():
    """Tests never touch the real Windows Credential Manager or keyring file.
    Its own MonkeyPatch, so a test's monkeypatch.undo() cannot lift it."""
    from openachievements import credentials
    store, mp = {}, pytest.MonkeyPatch()
    mp.setattr(credentials, "_set", lambda t, s: store.__setitem__(t, s))
    mp.setattr(credentials, "_get", lambda t: store.get(t))
    mp.setattr(credentials, "_delete", lambda t: store.pop(t, None))
    yield store
    mp.undo()


@pytest.fixture(autouse=True)
def _no_rarity_network(tmp_path_factory):
    """Rarity in tests: a scratch cache and no requests to Steam."""
    from openachievements import rarity
    mp = pytest.MonkeyPatch()
    folder = tmp_path_factory.mktemp("rarity")
    mp.setattr(rarity, "cache_dir", lambda: folder)

    def offline(url):
        raise OSError("tests never ask Steam")
    mp.setattr(rarity, "_default_fetch", offline)
    from openachievements import art                    # pictures too: a scratch cache, never the network
    art_folder = tmp_path_factory.mktemp("art")
    mp.setattr(art, "cache_dir", lambda: art_folder)
    real_fetch = art._fetch

    def no_network(url, open_url=None):                 # a test passing its own opener is not the network
        if open_url is None:
            raise OSError("tests never fetch pictures")
        return real_fetch(url, open_url)
    mp.setattr(art, "_fetch", no_network)
    yield folder
    mp.undo()


@pytest.fixture(autouse=True)
def _no_real_linux_desktop(tmp_path_factory):
    """On Linux, tests never show a real popup, start a browser or a sound
    player, or write the player's autostart and app menu: no display, no
    spawning, and scratch XDG folders. Its own MonkeyPatch, like the others."""
    from openachievements import linux_desktop
    mp = pytest.MonkeyPatch()
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        mp.delenv(name, raising=False)
    xdg = tmp_path_factory.mktemp("xdg")
    mp.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    mp.setenv("XDG_DATA_HOME", str(xdg / "data"))
    spawned = []
    mp.setattr(linux_desktop, "_spawn", lambda command: spawned.append(command) or True)
    yield spawned
    mp.undo()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def machine(tmp_path):
    return MachineConfig(tmp_path / "machine-1")


@pytest.fixture
def profile(home, machine):
    return Profile.create("Zein", home=home, config=machine)


def write_pack(folder: Path, pack_id="test-pack", game_id="test-game", achievements=None, **meta):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pack.json").write_text(json.dumps({
        "id": pack_id, "name": "Test pack", "version": meta.pop("version", "1.0.0"),
        "game_ids": [game_id], "games": [{"id": game_id, "title": "Test Game"}], **meta,
    }), encoding="utf-8")
    (folder / "achievements.json").write_text(json.dumps(achievements or [
        {"id": "first-step", "name": "First step", "description": "Start the game.", "points": 5},
        {"id": "collector", "name": "Collector", "points": 20, "progress": {"type": "counter", "target": 3}},
        {"id": "secret", "name": "Secret", "description": "A hidden one.", "points": 50, "hidden": True},
    ]), encoding="utf-8")
    return folder
