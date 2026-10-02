import pytest
"""The unlock popup: what pops, what never does, and the chime."""
import struct
from datetime import datetime, timedelta, timezone

from openachievements import events as ev
from openachievements import notify, toast
from conftest import write_pack


def unlock(profile, when, adapter="save-file"):
    return ev.make_event("achievement.unlocked", profile_id=profile.profile_id, device_id=profile.device_id,
                         payload={"provenance": "save-derived"}, achievement_id="test-pack:first-step",
                         game_id="test-game", occurred_at=when, adapter=adapter)


def iso(delta_seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_a_fresh_unlock_pops_once_with_its_name_and_game(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    shown = []
    n = notify.Notifier(profile, launch=lambda cards, sound: shown.append((cards, sound)))
    n.add(unlock(profile, iso(3)))
    assert n.flush() == 1 and n.flush() == 0
    assert shown == [([{"name": "First step", "game": "Test Game", "points": 5}],
                      {"on": True, "unlock": "pop", "platinum": "burst"})]


def test_history_and_steam_do_not_pop_unless_asked(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    shown = []
    n = notify.Notifier(profile, launch=lambda cards, sound: shown.append(cards))
    n.add(unlock(profile, "2024-03-01T10:00:00.000Z"))            # an import of old unlocks
    n.add(unlock(profile, iso(5), adapter="steam"))                 # Steam shows its own
    assert n.flush() == 0 and shown == []
    notify.change(profile, steam=True)
    n.add(unlock(profile, iso(5), adapter="steam"))
    assert n.flush() == 1


def test_popups_and_sound_can_be_turned_off(profile, tmp_path):
    profile.install_pack(write_pack(tmp_path / "pack"))
    shown = []
    n = notify.Notifier(profile, launch=lambda cards, sound: shown.append(sound["on"]))
    notify.change(profile, sound=False)
    n.add(unlock(profile, iso(1)))
    n.flush()
    assert shown == [False]
    notify.change(profile, enabled=False)
    n.add(unlock(profile, iso(1)))
    assert n.flush() == 0 and shown == [False]


def test_the_chime_is_a_short_quiet_wav():
    wav = toast.chime_wav()
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    pcm = wav[44:]
    peak = max(abs(v) for (v,) in struct.iter_unpack("<h", pcm))
    from openachievements import sounds
    assert len(pcm) / 2 / sounds.RATE < 1.6 and 0 < peak < 0.25 * 32767   # short (room tail included), well below full scale


def test_every_sound_choice_is_short_enough_quiet_and_ends_in_silence():
    from openachievements import sounds
    for kind, table, limit in (("unlock", sounds.UNLOCK, 1.6), ("platinum", sounds.PLATINUM, 4.2)):
        for name in table:
            pcm = toast.chime_wav(kind, name)[44:]
            values = [v for (v,) in struct.iter_unpack("<h", pcm)]
            assert len(values) / sounds.RATE < limit, (kind, name)
            assert 0 < max(map(abs, values)) < 0.3 * 32767, (kind, name)        # never loud, never clipped
            assert max(map(abs, values[-40:])) == 0, (kind, name)               # ends in exact silence


def test_chosen_sounds_are_kept_and_unknown_ones_refused(profile):
    notify.change(profile, unlock_sound="bwoop", platinum_sound="parade")
    prefs = notify.settings(profile.config.load())
    assert (prefs["unlock_sound"], prefs["platinum_sound"]) == ("bwoop", "parade")
    with pytest.raises(ValueError):
        notify.change(profile, platinum_sound="airhorn")


def test_every_sound_stays_soft_and_low():
    """Owner, 2026-10-02: not hard on the ear. The engine refuses a voice above
    1.2 kHz or one that starts in under 3 ms (a click), so no sound can."""
    from openachievements import sounds
    with pytest.raises(ValueError):
        sounds._voice([], 0.0, 1500.0, 0.2, 1.0)
    with pytest.raises(ValueError):
        sounds._voice([], 0.0, 600.0, 0.2, 1.0, attack=0.001)
    for kind, table in (("unlock", sounds.UNLOCK), ("platinum", sounds.PLATINUM)):
        for name in table:
            assert sounds.samples(kind, name)                      # every sound renders within those limits


def test_a_choice_of_a_sound_since_replaced_falls_back_to_the_default(profile):
    config = profile.config.load()
    config["notify"] = {"unlock_sound": "chime", "platinum_sound": "arpeggio"}     # names from the old set
    profile.config.save(config)
    prefs = notify.settings(profile.config.load())
    assert (prefs["unlock_sound"], prefs["platinum_sound"]) == ("pop", "burst")


def test_every_sound_is_only_bubbles_no_chimes(monkeypatch):
    """Owner rule, 2026-10-02: no chimes, no bells, nothing that rings; every
    sound is made of short bubbles that slide up into pitch (the Chirp family)."""
    from openachievements import sounds
    seen = []
    real = sounds._voice
    def record(buf, start, freq, dur, level, **kw):
        seen.append((dur, kw.get("bend", 0.0), kw.get("ratio", 1.0)))
        return real(buf, start, freq, dur, level, **kw)
    monkeypatch.setattr(sounds, "_voice", record)
    for kind, table in (("unlock", sounds.UNLOCK), ("platinum", sounds.PLATINUM)):
        for name in table:
            seen.clear()
            sounds.samples(kind, name)
            assert seen, name
            for dur, bend, ratio in seen:
                assert dur <= sounds.BUBBLE_MAX_DUR and bend <= -0.1 and ratio == 2.0, (kind, name, dur, bend, ratio)
