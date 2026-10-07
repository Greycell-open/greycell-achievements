"""Standalone executables: register, fingerprint, detect, unlock by rule."""
import pytest

from openachievements import reducer
from openachievements.adapters import executable as exe
from openachievements.profile import ProfileError
from conftest import write_pack

RULES = [
    {"id": "first-launch", "name": "Boot it up", "points": 5, "rules": [{"signal": "launch"}]},
    {"id": "hour", "name": "An hour in", "points": 10, "rules": [{"signal": "playtime", "minutes": 60}]},
    {"id": "regular", "name": "Regular", "points": 10, "rules": [{"signal": "sessions", "count": 2}]},
    {"id": "boss", "name": "Beat the boss", "points": 50},
]


@pytest.fixture
def game(profile, tmp_path):
    binary = tmp_path / "Games" / "Cave Story" / "Doukutsu.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ fake executable")
    profile.install_pack(write_pack(tmp_path / "pack", pack_id="cave-story-local", game_id="cave-story",
                                    achievements=RULES))
    inst = exe.register(profile, "cave-story", binary)
    return binary, inst


def test_register_fingerprints_without_putting_the_path_in_synced_events(profile, game):
    binary, inst = game
    assert inst["sha256"] and inst["size"] == len(b"MZ fake executable")
    events = profile.log.read().events
    registered = [e for e in events if e["event_type"] == "game.installation_registered"][0]
    assert registered["payload"]["executable_name"] == "Doukutsu.exe"
    assert str(binary) not in str(registered)
    assert exe.local_installations(profile)[0]["path"] == str(binary.resolve())


def test_registering_the_same_build_twice_is_one_installation(profile, game):
    binary, inst = game
    again = exe.register(profile, "cave-story", binary)
    assert again["already_registered"] and again["installation_id"] == inst["installation_id"]


def test_launch_and_exit_become_sessions_and_unlock_rule_achievements(profile, game):
    binary, _ = game
    w = exe.Watcher(profile)
    running = {str(binary.resolve()).lower()}
    started = w.poll(running, now=0)
    assert [e["event_type"] for e in started] == ["session.started", "achievement.unlocked"]
    assert started[1]["achievement_id"] == "cave-story-local:first-launch"
    assert started[1]["payload"]["provenance"] == "local-executable"
    assert w.poll(running, now=1800) == []
    ended = w.poll(set(), now=3700)
    assert [e["event_type"] for e in ended] == ["session.ended", "achievement.unlocked"]
    assert ended[0]["payload"]["seconds"] == 3700
    state = profile.state()
    assert reducer.is_unlocked(state, "cave-story-local:hour")
    assert not reducer.is_unlocked(state, "cave-story-local:regular")
    assert not reducer.is_unlocked(state, "cave-story-local:boss")      # needs a real game signal
    w.poll(running, now=4000)
    w.poll(set(), now=4100)
    assert reducer.is_unlocked(profile.state(), "cave-story-local:regular")
    game_view = profile.library()["games"][0]
    assert game_view["playtime_seconds"] == 3800


def test_unlocks_record_local_executable_provenance(profile, game):
    binary, _ = game
    exe.Watcher(profile).poll({str(binary.resolve()).lower()}, now=0)
    ach = next(a for a in profile.library()["games"][0]["achievements"] if a["id"] == "first-launch")
    assert ach["provenance"] == ["local-executable"] and ach["sources"] == ["executable"]


def test_nothing_runs_nothing_happens(profile, game):
    assert exe.Watcher(profile).poll(set(), now=0) == []


def test_missing_file_is_refused(profile, tmp_path):
    with pytest.raises(ProfileError):
        exe.register(profile, "ghost", tmp_path / "nope.exe")


def test_the_process_list_can_be_read_on_this_os():
    assert isinstance(exe.running_executables(), set)


def test_a_program_on_two_linked_games_is_one_session_on_the_shown_game(profile, tmp_path):
    """A local stand-in linked into the Steam game it turned out to be (the
    achievement finder, or a later recognition) often has the same program
    registered on both. The library adds linked games' play time together, so
    two sessions would show every minute twice."""
    binary = tmp_path / "Games" / "Mini Airways" / "MiniAirways.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ fake")
    profile.register_game("local-mini-airways", "Mini Airways", platform="PC", external_ids={})
    profile.register_game("steam-2289650", "Mini Airways - ATC simulator", platform="PC", external_ids={"steam": 2289650})
    exe.register(profile, "local-mini-airways", binary)
    profile.link_games("local-mini-airways", "steam-2289650")
    exe.register(profile, "steam-2289650", binary)
    w = exe.Watcher(profile)
    running = {str(binary.resolve()).lower()}
    started = [e for e in w.poll(running, now=0) if e["event_type"] == "session.started"]
    assert [e["game_id"] for e in started] == ["steam-2289650"]
    ended = [e for e in w.poll(set(), now=600) if e["event_type"] == "session.ended"]
    assert [e["game_id"] for e in ended] == ["steam-2289650"]
    shown = next(g for g in profile.library()["games"] if g["game_id"] == "steam-2289650")
    assert shown["playtime_seconds"] == 600
