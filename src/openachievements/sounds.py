"""The unlock and Platinum sounds, synthesised here: no sound file to license.

Designed from how reward sounds work (owner, 2026-10-02: "something that just
sticks"), and built only from one instrument, a bubble that slides up into
pitch (owner rule, 2026-10-02: no chimes, no bells, nothing that rings; the
"Chirp" family). Every sound has three layers:

  transient  a few ms of soft filtered noise: the physical "tick" of a hit
  body       FM tones whose brightness decays fast (a rich hit that mellows,
             sparkle without ear fatigue), often with a small upward pitch
             bend at the onset, on a rising motif in major intervals
  tail       a short, low room reverb that places the sound in a space

Bodies sit in the mids (fundamentals 260 Hz to 1.2 kHz: small speakers drop
the lows, and the highs tire the ear), a gentle low-pass softens the top, and
every sound is normalised to the same modest loudness and ends in exact
silence. Unlock sounds are short; Platinum sounds are longer and resolve.
"""
from __future__ import annotations

import math
import random
import struct

RATE = 32000
TAIL = 0.7                          # room tail added after the last note
UNLOCK_PEAK, PLATINUM_PEAK = 0.22, 0.26
MAX_FUNDAMENTAL = 1200.0            # tests hold every voice to this


# ---- one voice ----------------------------------------------------------------------

def _voice(buf: list, start: float, freq: float, dur: float, level: float, *, attack: float = 0.006,
           decay: float = 6.0, ratio: float = 1.0, index: float = 0.0, index_decay: float = 14.0,
           bend: float = 0.0, bend_time: float = 0.025, click: float = 0.0) -> None:
    """Add one FM note to buf. bend < 0 starts it a little flat and rises into
    pitch (a "bloop"); index is the brightness, decaying at index_decay."""
    if freq > MAX_FUNDAMENTAL or attack < 0.003:
        raise ValueError("too high or too sharp for these sounds")
    first, n = int(start * RATE), int(dur * RATE)
    if first + n > len(buf):
        buf.extend([0.0] * (first + n - len(buf)))
    release = min(0.12, dur / 3)
    pc = pm = 0.0
    two_pi = 2 * math.pi
    for i in range(n):
        t = i / RATE
        f = freq * (1 + bend * math.exp(-t / bend_time))
        pm += two_pi * f * ratio / RATE
        pc += two_pi * f / RATE
        value = math.sin(pc + index * math.exp(-t * index_decay) * math.sin(pm))
        env = min(1.0, t / attack) * math.exp(-t * decay)
        tail = dur - t
        if tail < release:
            env *= 0.5 - 0.5 * math.cos(math.pi * tail / release)
        buf[first + i] += level * env * value
    if click:
        rnd, lp, m = random.Random(int(freq * 1000 + start * 7)), 0.0, int(0.006 * RATE)
        for i in range(m):
            lp += 0.35 * (rnd.uniform(-1, 1) - lp)               # soft, low-passed noise
            buf[first + i] += click * lp * (1 - i / m) ** 2


# ---- the room, the filter, the level --------------------------------------------------

def _room(dry: list, wet: float, tail: float | None = None) -> list:
    """A small Schroeder room: four combs, two all-passes, mixed in low."""
    out = dry + [0.0] * int((TAIL if tail is None else tail) * RATE)
    acc = [0.0] * len(out)
    for ms, fb in ((29.7, 0.72), (37.1, 0.70), (41.1, 0.68), (43.7, 0.66)):
        d = int(ms / 1000 * RATE)
        line = [0.0] * len(out)
        for i in range(len(out)):
            x = out[i] + (line[i - d] * fb if i >= d else 0.0)
            line[i] = x
            acc[i] += x
    for ms, g in ((5.0, 0.7), (1.7, 0.7)):
        d = int(ms / 1000 * RATE)
        y = [0.0] * len(acc)
        for i in range(len(acc)):
            prev_x = acc[i - d] if i >= d else 0.0
            prev_y = y[i - d] if i >= d else 0.0
            y[i] = -g * acc[i] + prev_x + g * prev_y
        acc = y
    return [out[i] + wet * 0.25 * acc[i] for i in range(len(out))]


def _finish(buf: list, wet: float, peak: float, tail: float | None = None) -> list:
    buf = _room(buf, wet, tail)
    lp, smooth = 0.0, 0.55                                      # gentle low-pass, about 7 kHz
    for i, x in enumerate(buf):
        lp += smooth * (x - lp)
        buf[i] = lp
    fade = int(0.25 * RATE)                                    # the tail ends in exact silence
    for i in range(1, fade + 1):
        buf[-i] *= 0.5 - 0.5 * math.cos(math.pi * (i - 1) / fade)
    top = max(map(abs, buf)) or 1.0
    return [x * peak / top for x in buf]


# ---- the one instrument: a bubble ------------------------------------------------------
# Owner rule, 2026-10-02: no chimes, no bells, nothing that rings. Every sound
# is built only from this bubble (the "Chirp" family the owner liked): it
# starts a little flat and slides up into pitch within ~20 ms, a soft round
# tone that decays fast. tests/test_notify.py holds every sound to it.
BUBBLE_MAX_DUR = 0.5


def _bubble(buf, start, f, level=1.0, dur=0.22, bend=-0.22, bend_time=0.018, decay=9.0, tone=0.6):
    if dur > BUBBLE_MAX_DUR or bend > -0.1:
        raise ValueError("a bubble is short and slides up into pitch")
    _voice(buf, start, f, dur, level, ratio=2.0, index=tone, decay=decay, bend=bend, bend_time=bend_time, click=0.1)


def _run(buf, start, freqs, gap, level=1.0, dur=0.2, **kw):
    for i, f in enumerate(freqs):
        _bubble(buf, start + i * gap, f, level, dur, **kw)


# ---- the sounds -----------------------------------------------------------------------
# C5 523.3  D5 587.3  E5 659.3  G5 784.0  A5 880.0  B5 987.8  C6 1046.5  D6 1174.7
# G4 392.0  C4 261.6  E4 329.6

def _chirp(b):                                                  # the one the owner liked: three quick rising bubbles
    _bubble(b, 0.0, 659.3, 0.8, 0.18); _bubble(b, 0.05, 880.0, 0.9, 0.2); _bubble(b, 0.1, 1046.5, 1.0, 0.3)
    return b, 0.1


# Unlock sounds (owner, 2026-10-02): the family of a console achievement pop,
# not a copy of one. One round low-mid pop with a deep, slightly slow upward
# slide ("bwoop"), a faint octave layer so small speakers still carry it, and
# at most two pops: simple is what sticks.
def _pop_at(b, start, f, level=1.0, dur=0.38, bend=-0.45, bend_time=0.035, tone=0.35):
    _bubble(b, start, f, level, dur, bend=bend, bend_time=bend_time, decay=7.5, tone=tone)
    layer = f * 2 if f * 2 <= MAX_FUNDAMENTAL else f * 1.5    # the octave, or the fifth when that is too high
    _bubble(b, start + 0.004, layer, 0.22 * level, dur * 0.7, bend=bend, bend_time=bend_time, decay=10.0, tone=0.2)


def _pop(b):                                                    # the signature: one round pop
    _pop_at(b, 0.0, 466.2)                                      # B flat 4
    return b, 0.16


def _pop_duo(b):                                                # two pops, a fifth up
    _pop_at(b, 0.0, 392.0, 0.85, 0.26); _pop_at(b, 0.1, 587.3, 1.0)
    return b, 0.16


def _bwoop(b):                                                  # a slower, rounder slide
    _pop_at(b, 0.0, 440.0, dur=0.45, bend=-0.5, bend_time=0.06, tone=0.3)
    return b, 0.18


def _deep_pop(b):                                               # lower and fuller
    _pop_at(b, 0.0, 349.2, dur=0.42, bend=-0.4, bend_time=0.04, tone=0.45)
    return b, 0.18


def _bright_pop(b):                                             # lighter and higher
    _pop_at(b, 0.0, 622.3, dur=0.32, bend=-0.4, bend_time=0.028, tone=0.3)
    return b, 0.14


def _burst(b):                                                  # three chirps, each higher, then a big landing
    for i, base in enumerate((523.3, 659.3, 784.0)):
        _run(b, i * 0.2, (base, base * 1.26, base * 1.5), 0.045, 0.7 + 0.1 * i, 0.16)
    _run(b, 0.62, (523.3, 1046.5), 0.06, 1.0, 0.4, bend=-0.3, decay=6.0)
    return b, 0.22


def _flurry(b):                                                 # a run that speeds up as it climbs
    freqs = (392.0, 440.0, 493.9, 523.3, 587.3, 659.3, 784.0, 880.0, 987.8, 1046.5)
    t = 0.0
    for i, f in enumerate(freqs):
        _bubble(b, t, f, 0.6 + 0.04 * i, 0.16, decay=12.0)
        t += 0.09 - 0.006 * i
    _run(b, t + 0.04, (784.0, 1046.5, 1174.7), 0.05, 1.0, 0.35, decay=6.0)
    return b, 0.2


def _parade(b):                                                 # bouncing triplets marching upward
    for i, group in enumerate(((523.3, 659.3, 784.0), (659.3, 784.0, 1046.5), (784.0, 1046.5, 1174.7))):
        _run(b, i * 0.24, group, 0.07, 0.75 + 0.08 * i, 0.18)
    _bubble(b, 0.78, 1046.5, 1.0, 0.45, bend=-0.35, decay=5.0)
    return b, 0.22


def _rocket(b):                                                 # a low rumble of bubbles taking off
    for i in range(14):
        f = 261.6 * (2 ** (i / 6.5))
        if f > 1174.7:
            break
        _bubble(b, i * 0.055, f, 0.5 + 0.035 * i, 0.15, bend=-0.25, decay=13.0, tone=0.5)
    _run(b, 0.82, (784.0, 1174.7), 0.07, 1.0, 0.4, bend=-0.35, decay=6.0)
    return b, 0.22


# Echo (owner's pick, 2026-10-03, after five rounds of previews built from
# reward-sound research): a soft tick, a short climb (G4, C5) into a round,
# Xbox-style tone gliding up into G5, then the same tone answering an octave
# lower, twice, fading, in a wider room. Rising consonant steps, anticipation
# then payoff then resolution; round tones only (no chimes).
def _wide(b, t, f, level, dur, glide=0.05, decay=4.8, body=0.3):
    _bubble(b, t, f, level, dur, bend=-0.35, bend_time=glide, decay=decay, tone=0.1)
    if body:
        _bubble(b, t, f / 2, level * body, dur * 0.8, bend=-0.35, bend_time=glide, decay=decay + 1.2, tone=0.04)


def _echo(b):
    _bubble(b, 0.0, 261.6, 0.16, 0.06, bend=-0.3, bend_time=0.008, decay=30.0, tone=0.1)   # the tick
    _wide(b, 0.045, 392.0, 0.7, 0.16, glide=0.035)
    _wide(b, 0.125, 523.3, 0.9, 0.3)
    _wide(b, 0.235, 784.0, 1.0, 0.45, decay=3.5, body=0.4)                               # the landing
    _wide(b, 0.475, 392.0, 0.38, 0.45, decay=4.0, body=0.0)                              # the low answer
    _wide(b, 0.715, 392.0, 0.13, 0.4, decay=4.5, body=0.0)
    return b, 0.75, 1.7


# Two instruments besides the bubble (owner, 2026-10-03: "add marimba and
# retro 8 bit"): the Echo motif on wood and on a softened 8-bit square. Still
# no chimes and no bells; the same limits as the bubble (fundamental up to
# 1.2 kHz, no attack under 3 ms). INSTRUMENT_SOUNDS names the only sounds not
# made of bubbles; tests hold it to exactly these.
INSTRUMENT_SOUNDS = frozenset({"echo-marimba", "echo-retro", "rare-marimba", "rare-retro"})


def _place(buf: list, start: float, samples: list) -> None:
    first = int(start * RATE)
    if first + len(samples) > len(buf):
        buf.extend([0.0] * (first + len(samples) - len(buf)))
    for i, v in enumerate(samples):
        buf[first + i] += v


def _envelope(n: int, attack: float, decay: float, release: float = 0.04) -> list:
    if attack < 0.003:
        raise ValueError("too sharp for these sounds")
    out = []
    for i in range(n):
        t = i / RATE
        e = min(1.0, t / attack) * math.exp(-t * decay)
        left = (n - i) / RATE
        out.append(e * (left / release if left < release else 1.0))
    return out


def _marimba(f: float, dur: float, level: float) -> list:
    """Wood: the note and its fourth partial (a marimba bar's tuning), the partial gone in a moment."""
    if f > MAX_FUNDAMENTAL:
        raise ValueError("too high for these sounds")
    n = int(dur * RATE)
    body, knock = _envelope(n, 0.003, 5.0), _envelope(n, 0.003, 28.0)
    w = 2 * math.pi * f / RATE
    return [level * (math.sin(w * i) * body[i] + 0.25 * math.sin(4 * w * i) * knock[i]) for i in range(n)]


def _retro(f: float, dur: float, level: float) -> list:
    """8-bit, softened: a square wave with its edges rounded off by a low-pass."""
    if f > MAX_FUNDAMENTAL:
        raise ValueError("too high for these sounds")
    n, out, lp = int(dur * RATE), [], 0.0
    e = _envelope(n, 0.004, 4.5)
    w = 2 * math.pi * f / RATE
    for i in range(n):
        lp += 0.12 * ((1.0 if math.sin(w * i) >= 0 else -1.0) - lp)
        out.append(level * 0.6 * lp * e[i])
    return out


ECHO_NOTES = ((0.0, 261.6, 0.06, 0.15), (0.045, 392.0, 0.18, 0.7), (0.125, 523.3, 0.32, 0.9),
              (0.235, 784.0, 0.6, 1.0), (0.475, 392.0, 0.5, 0.38), (0.715, 392.0, 0.45, 0.13))
# Rare (owner, 2026-10-03: "a special sound for rare achievements"): the Echo
# climb taken one step further (round 4's "rare" idea the owner liked), landing
# on C6, answered low twice, in a wider room.
RARE_NOTES = ((0.0, 261.6, 0.06, 0.15), (0.045, 392.0, 0.16, 0.65), (0.12, 523.3, 0.2, 0.8),
              (0.195, 784.0, 0.26, 0.9), (0.29, 1046.5, 0.6, 1.0), (0.56, 523.3, 0.5, 0.38), (0.83, 523.3, 0.45, 0.13))


def _echo_on(instrument, notes=ECHO_NOTES, wet=0.75, tail=1.7):
    def make(b):                                # the motif, note for note
        for start, f, dur, level in notes:
            _place(b, start, instrument(f, dur, level))
        return b, wet, tail
    return make


def _rare_echo(b):
    _bubble(b, 0.0, 261.6, 0.15, 0.06, bend=-0.3, bend_time=0.008, decay=30.0, tone=0.1)    # the tick
    _wide(b, 0.045, 392.0, 0.65, 0.16, glide=0.035)
    _wide(b, 0.12, 523.3, 0.8, 0.2, glide=0.04)
    _wide(b, 0.195, 784.0, 0.9, 0.26)
    _wide(b, 0.29, 1046.5, 1.0, 0.48, decay=3.2, body=0.45)                                  # the landing, a step past Echo
    _wide(b, 0.56, 523.3, 0.38, 0.45, decay=4.0, body=0.0)                                   # the low answer
    _wide(b, 0.83, 523.3, 0.13, 0.4, decay=4.5, body=0.0)
    return b, 0.85, 2.0


UNLOCK = {
    "echo": ("Echo", _echo),
    "echo-marimba": ("Echo, marimba", _echo_on(_marimba)),
    "echo-retro": ("Echo, retro 8-bit", _echo_on(_retro)),
    "pop": ("Pop", _pop),
    "pop-duo": ("Pop Duo", _pop_duo),
    "bwoop": ("Bwoop", _bwoop),
    "deep-pop": ("Deep Pop", _deep_pop),
    "bright-pop": ("Bright Pop", _bright_pop),
    "chirp": ("Chirp", _chirp),
}
RARE = {
    "rare-echo": ("Rare Echo", _rare_echo),
    "rare-marimba": ("Rare Echo, marimba", _echo_on(_marimba, RARE_NOTES, 0.85, 2.0)),
    "rare-retro": ("Rare Echo, retro 8-bit", _echo_on(_retro, RARE_NOTES, 0.85, 2.0)),
}
PLATINUM = {
    "burst": ("Burst", _burst),
    "flurry": ("Flurry", _flurry),
    "parade": ("Parade", _parade),
    "rocket": ("Rocket", _rocket),
}
DEFAULT_UNLOCK, DEFAULT_PLATINUM, DEFAULT_RARE = "echo", "burst", "rare-echo"
TABLES = {"unlock": (lambda: UNLOCK, "echo"), "rare": (lambda: RARE, "rare-echo"), "platinum": (lambda: PLATINUM, "burst")}
CUSTOM = "custom"                   # the player's own WAV file, kept on this computer only
MAX_CUSTOM_BYTES = 4 * 1024 * 1024


def choices() -> dict:
    custom = [{"id": CUSTOM, "name": "Custom (your own file)"}]
    return {kind: [{"id": k, "name": v[0]} for k, v in get().items()] + custom for kind, (get, _d) in TABLES.items()}


def known(kind: str, name: str | None) -> str:
    """The sound to use: the chosen one if it exists, else the default (so a
    choice saved before the sounds were replaced still plays something)."""
    get, default = TABLES.get(kind, TABLES["unlock"])
    return name if name in get() else default


def samples(kind: str, name: str | None) -> list:
    table = TABLES.get(kind, TABLES["unlock"])[0]()
    made = table[known(kind, name)][1]([])
    buf, wet, tail = made if len(made) == 3 else (*made, None)     # a sound may ask for a longer room
    return _finish(buf, wet, PLATINUM_PEAK if kind == "platinum" else UNLOCK_PEAK, tail)


def wav(kind: str, name: str | None) -> bytes:
    """16-bit mono WAV of a sound, ending in exact silence."""
    pcm = b"".join(struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples(kind, name))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm)


def check_custom_wav(data: bytes) -> None:
    """A file Windows can play as-is: a RIFF/WAVE file of plain PCM, not too big."""
    if len(data) > MAX_CUSTOM_BYTES:
        raise ValueError("the sound file is larger than 4 MB")
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("choose a .wav sound file")
    at = 12
    while at + 8 <= len(data):
        tag, size = data[at:at + 4], struct.unpack("<I", data[at + 4:at + 8])[0]
        if tag == b"fmt ":
            if struct.unpack("<H", data[at + 8:at + 10])[0] not in (1, 3, 0xFFFE):
                raise ValueError("that WAV is compressed; save it as plain (PCM) WAV")
            return
        at += 8 + size + (size & 1)
    raise ValueError("that WAV file has no format section")
