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
                      {"on": True, "unlock": "echo", "platinum": "burst", "rare": "rare-echo"})]


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
    assert len(pcm) / 2 / sounds.RATE < 3.0 and 0 < peak < 0.25 * 32767   # Echo's room tail included; well below full scale


def test_every_sound_choice_is_short_enough_quiet_and_ends_in_silence():
    from openachievements import sounds
    # Unlock sounds may run to 3 s: Echo (owner's pick, 2026-10-03) fades out in a longer room.
    # Rare and Platinum may run longer: the owner chose Radiant Awaken (5.7 s) and Roar (6.1 s) by ear, 2026-10-03.
    for kind, table, limit in (("unlock", sounds.UNLOCK, 3.0), ("rare", sounds.RARE, 6.0), ("platinum", sounds.PLATINUM, 6.5)):
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
    assert (prefs["unlock_sound"], prefs["platinum_sound"]) == ("echo", "burst")


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
    assert sounds.INSTRUMENT_SOUNDS == {"echo-marimba", "echo-retro", "rare-marimba", "rare-retro"}   # owner-approved, 2026-10-03
    for kind, table in (("unlock", sounds.UNLOCK), ("rare", sounds.RARE), ("platinum", sounds.PLATINUM)):
        for name in table:
            if name in sounds.INSTRUMENT_SOUNDS or name in sounds.RECORDED_SOUNDS:
                continue                                       # recordings: test_recorded_sounds_follow_the_rules
            seen.clear()
            sounds.samples(kind, name)
            assert seen, name
            for dur, bend, ratio in seen:
                assert dur <= sounds.BUBBLE_MAX_DUR and bend <= -0.1 and ratio == 2.0, (kind, name, dur, bend, ratio)


def test_every_local_popup_decision_is_logged(profile, capsys):
    """A missing popup must be traceable afterwards: the log says it started,
    or why not. Platform imports stay out of the log (thousands of them)."""
    from datetime import datetime, timedelta, timezone
    from openachievements import notify
    started = []
    n = notify.Notifier(profile, launch=lambda cards, sound: started.append(cards))
    now = datetime.now(timezone.utc)
    stamp = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fresh = {"event_type": "achievement.unlocked", "achievement_id": "g:fresh", "game_id": "g",
             "occurred_at": stamp(now), "source": {"adapter": "save-file"}}
    late = dict(fresh, achievement_id="g:late", occurred_at=stamp(now - timedelta(minutes=10)))
    imported = dict(fresh, achievement_id="g:steam", source={"adapter": "steam"})
    for e in (fresh, late, imported):
        n.add(e)
    n.flush()
    out = capsys.readouterr().out
    assert "popup: started for" in out and len(started) == 1
    assert "no popup for g:late: unlocked 600 s ago" in out
    assert "g:steam" not in out


def test_a_popup_that_cannot_start_is_logged_and_raised(profile, capsys):
    from datetime import datetime, timezone
    from openachievements import notify

    def broken(cards, sound):
        raise OSError("blocked")
    n = notify.Notifier(profile, launch=broken)
    n.add({"event_type": "achievement.unlocked", "achievement_id": "g:a", "game_id": "g",
           "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
           "source": {"adapter": "save-file"}})
    import pytest
    with pytest.raises(OSError):
        n.flush()
    assert "popup: could not start the popup" in capsys.readouterr().out


def test_log_lines_carry_the_time_and_process(tmp_path):
    import io, os, re
    from openachievements.desktop import _Stamped
    buf = io.StringIO()
    log = _Stamped(buf)
    log.write("one\ntwo ")
    log.write("still two\n")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2 and lines[1].endswith("two still two")
    assert all(re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[" + str(os.getpid()) + r"\] ", l) for l in lines)


def test_the_two_instruments_keep_the_bubbles_limits():
    """Marimba and retro 8-bit (owner, 2026-10-03) obey the same ceiling and
    soft attack as every other sound."""
    from openachievements import sounds
    for instrument in (sounds._marimba, sounds._retro):
        with pytest.raises(ValueError):
            instrument(1500.0, 0.2, 1.0)
        assert instrument(784.0, 0.2, 1.0)
    with pytest.raises(ValueError):
        sounds._envelope(100, 0.001, 5.0)
    for name in sounds.INSTRUMENT_SOUNDS:
        kind = "rare" if name.startswith("rare-") else "unlock"
        assert name in sounds.TABLES[kind][0]() and sounds.samples(kind, name)


def test_recorded_sounds_follow_the_rules():
    """The owner's recorded picks (2026-10-03, chosen by ear; no chimes): only
    these ten, each a plain 16-bit mono 32 kHz WAV at the app's loudness that
    ends in exact silence, with nothing in the file but its format and its
    sound (no metadata naming whatever made it)."""
    from openachievements import sounds
    assert sounds.RECORDED_SOUNDS == {"ki-flicker", "flash-rush", "ki-burst", "apex", "ki-ascension",
                                      "radiant-awaken", "ki-completion", "roar", "warp-step", "beam-roar"}
    found = set()
    for kind, table, peak in (("unlock", sounds.UNLOCK, 0.22), ("rare", sounds.RARE, 0.22),
                              ("platinum", sounds.PLATINUM, 0.26)):
        for name, (_label, maker) in table.items():
            if not isinstance(maker, sounds.Recorded):
                continue
            found.add(name)
            data = maker.path.read_bytes()
            chunks, at = [], 12
            while at + 8 <= len(data):
                size = struct.unpack("<I", data[at + 4:at + 8])[0]
                chunks.append(data[at:at + 4])
                at += 8 + size + (size & 1)
            assert chunks == [b"fmt ", b"data"], (name, chunks)
            values = sounds.samples(kind, name)
            assert 0 < max(map(abs, values)) <= peak + 0.005, (name, max(map(abs, values)))
            assert max(map(abs, values[-40:])) == 0, name
    assert found == sounds.RECORDED_SOUNDS
