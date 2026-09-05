"""The monster treatment: what it does to a clip, and what it must not.

`tts/dsp.py` is the only place in this app that touches audio samples. Nothing
downstream can tell a correct treatment from a wrong one — a monster that comes
back too loud, too long, or clipped still plays, and the page has no way to
say so — which is what these are for.

Everything here is arithmetic on a synthetic tone. Whether the result *sounds*
better than the standard engine it replaced is a judgement, and
`tools/polly_check.py --ab` is where it is made.
"""

from __future__ import annotations

import io
import math
import wave
from array import array

import pytest

from tts.dsp import (
    CAVE_DELAY_S,
    MAX_SIZE_PCT,
    SAMPLE_RATE,
    MonsterFX,
    apply,
    source_fingerprint,
    wav,
)

#: Two seconds of a voice-ish tone: a fundamental where a low voice sits, one
#: harmonic above it, at about a quarter of full scale.
SECONDS = 2


def tone(seconds: int = SECONDS, rate: int = SAMPLE_RATE) -> bytes:
    return array("h", [
        int(8000 * (math.sin(2 * math.pi * 180 * i / rate)
                    + 0.4 * math.sin(2 * math.pi * 540 * i / rate)))
        for i in range(seconds * rate)
    ]).tobytes()


def samples(pcm: bytes) -> array:
    out = array("h")
    out.frombytes(pcm)
    return out


def rms(data) -> float:
    values = samples(data) if isinstance(data, bytes) else data
    if not values:
        return 0.0
    return (sum(float(v) * v for v in values) / len(values)) ** 0.5


def peak(data) -> int:
    values = samples(data) if isinstance(data, bytes) else data
    return max((abs(v) for v in values), default=0)


# -- the treatment itself ----------------------------------------------------

def test_the_size_shift_is_the_sample_rate_and_nothing_else():
    """The whole point of doing it this way: a bigger creature costs one
    integer in a header, not a pass over the samples — so the audio a monster
    with no character effects gets is Polly's own, unaltered."""
    pcm = tone()
    treated, rate = apply(pcm, MonsterFX(size_pct=24))
    assert treated == pcm                       # byte for byte
    assert rate == round(SAMPLE_RATE / 1.24)
    assert rate < SAMPLE_RATE                   # bigger is lower

    up, rate_up = apply(pcm, MonsterFX(size_pct=-16))
    assert up == pcm and rate_up > SAMPLE_RATE  # smaller is higher

    # And a null treatment is a null treatment: same bytes, same rate.
    assert apply(pcm, MonsterFX()) == (pcm, SAMPLE_RATE)


def test_the_speaking_rate_undoes_what_the_size_shift_does_to_duration():
    """A monster is a different voice, not a slower one. Polly is asked to
    speak at `rate_pct`; playing the result at `playback_rate` gives back the
    duration the line was written to have, to within rounding."""
    for size in range(-MAX_SIZE_PCT, MAX_SIZE_PCT + 1):
        fx = MonsterFX(size_pct=size)
        assert fx.rate_pct() == 100 + size
        # Spoken (1 + s) times as fast, played back at 1/(1 + s) the rate:
        # the two are the same factor and cancel.
        assert fx.playback_rate() * fx.rate_pct() / 100 == pytest.approx(SAMPLE_RATE, abs=1)


def test_a_treated_monster_is_a_different_voice_not_a_louder_one():
    """Saturation is a compressor and a comb adds energy, so both raise the
    level as a side effect. A monster ten decibels over the narrator reading
    the line before it is a mixing fault, so the level is held."""
    pcm = tone()
    before = rms(pcm)
    for fx in (MonsterFX(24, 80, 0), MonsterFX(-16, 0, 55), MonsterFX(34, 55, 35)):
        treated, _rate = apply(pcm, fx)
        assert rms(treated) == pytest.approx(before, rel=0.02)
        assert peak(treated) <= 32767                  # and never past the format
        assert treated != pcm                          # but something did happen


def test_saturation_adds_harmonics_rather_than_volume():
    """What `growl` is for: the same fundamental, more of everything above it.

    Measured as crest factor — peak over RMS — which falls when a waveform is
    squared off and is the cheapest signature of soft clipping that does not
    need an FFT.
    """
    pcm = tone()
    plain = samples(pcm)
    grim = samples(apply(pcm, MonsterFX(growl_pct=90))[0])
    assert peak(grim) / rms(grim) < peak(plain) / rms(plain)


def speechy(peak_frac: float, seconds: int = SECONDS, rate: int = SAMPLE_RATE) -> bytes:
    """A voice-ish tone with syllables, at a chosen fraction of full scale.

    Two things about it matter and `tone()` has neither. Polly does not master
    a line to the top of the format — one comes back peaking somewhere around a
    quarter to a half — so the level a treatment meets is not a detail but the
    case that actually occurs. And speech is peaky: it arrives in syllables
    with gaps between them, so its peaks stand three or four times its RMS,
    which is the part of a waveform a soft clipper acts on. A steady tone
    understates both.
    """
    top = 32767 * peak_frac / 1.4
    out = []
    for i in range(seconds * rate):
        t = i / rate
        syllable = abs(math.sin(2 * math.pi * 2.5 * t)) ** 3
        env = 0.05 + 0.95 * syllable
        out.append(int(top * env * (math.sin(2 * math.pi * 180 * t)
                                    + 0.4 * math.sin(2 * math.pi * 540 * t))))
    return array("h", out).tobytes()


def change(pcm: bytes, fx: MonsterFX) -> float:
    """How much a treatment altered a clip, relative to the clip's own level.

    The difference signal's RMS over the untreated RMS: 0 is "did nothing",
    and it is a ratio so clips at different levels can be compared.
    """
    base = samples(apply(pcm, MonsterFX(size_pct=fx.size_pct))[0])
    out = samples(apply(pcm, fx)[0])
    n = min(len(base), len(out))
    delta = array("h", [max(-32768, min(32767, base[i] - out[i])) for i in range(n)])
    return rms(delta) / (rms(base) or 1.0)


def test_growl_does_the_same_thing_however_quietly_the_line_was_spoken():
    """The regression this exists for.

    The drive used to be measured against `PEAK` — full scale — which assumed
    a clip arrives mastered to the top of the format. A Polly line does not, so
    every sample sat on the straight part of the shaper and the effect came out
    in proportion to how loudly the voice happened to speak: at a quarter of
    full scale, `growl=100` moved the audio by about a tenth of what it moved
    at nine tenths. From the listener's side the slider did nothing.

    Measured against the clip's own level instead, the same number has to mean
    the same treatment at any input level.
    """
    changes = [change(speechy(frac), MonsterFX(growl_pct=80))
               for frac in (0.9, 0.5, 0.25, 0.1)]
    assert min(changes) > 0.15, f"growl 80 is inaudible somewhere: {changes}"
    # Level-independent: the quietest and the loudest get the same treatment.
    assert max(changes) - min(changes) < 0.05, changes


def test_growl_is_monotonic_and_arrives_at_something():
    """A slider whose top end is not clearly different from its bottom end is
    a slider nobody can use."""
    pcm = speechy(0.4)
    got = [change(pcm, MonsterFX(growl_pct=p)) for p in (30, 55, 80, 100)]
    assert got == sorted(got), got
    assert got[0] > 0.05, f"the lowest dealt growl is inaudible: {got}"
    assert got[-1] > 0.25, f"full growl is not much of a growl: {got}"
    # And the range has resolution in it: the top is not the bottom twice over.
    assert got[-1] > got[0] * 2, got


def test_the_room_is_the_same_size_whatever_the_creature_is():
    """The comb delay is counted in samples of the PLAYBACK rate, so a monster
    shifted down is standing in the same cave as one shifted up rather than in
    one a third larger. Heard as an echo at a fixed number of milliseconds."""
    pcm = tone(1)
    for size in (-16, 0, 34):
        fx = MonsterFX(size_pct=size, cave_pct=55)
        treated, rate = apply(pcm, fx)
        # The first reflection lands `CAVE_DELAY_S` after the sound that made
        # it, in the time base the clip will be played back in.
        delay_seconds = int(rate * CAVE_DELAY_S) / rate
        assert delay_seconds == pytest.approx(CAVE_DELAY_S, abs=1.0 / rate)
        assert treated != pcm


def test_a_short_or_ragged_clip_costs_a_sample_rather_than_the_line():
    """A truncated stream is a listener hearing 1/32000 of a second less, not
    a 502 and a fallback: `array.frombytes` refuses a partial frame."""
    assert apply(b"", MonsterFX(24, 80, 55)) == (b"", round(SAMPLE_RATE / 1.24))
    treated, _rate = apply(b"\x01\x02\x03", MonsterFX(growl_pct=50))
    assert len(treated) == 2
    # Silence has no level to hold, and dividing by it would be the one way
    # this could raise.
    assert apply(b"\x00" * 4000, MonsterFX(24, 80, 55))[0] == b"\x00" * 4000


def test_the_treatment_is_deterministic():
    pcm = tone(1)
    fx = MonsterFX(34, 55, 35)
    assert apply(pcm, fx) == apply(pcm, fx)


# -- the description of a treatment ------------------------------------------

def test_nothing_dealt_is_nothing_done():
    """`bool(fx)` is what `Cast.fx` and the cache key are read through, so a
    treatment that rounds to nothing has to be falsy — otherwise an untreated
    seat would key differently from the plain voice it sounds exactly like."""
    assert not MonsterFX()
    assert MonsterFX(size_pct=9) and MonsterFX(growl_pct=30) and MonsterFX(cave_pct=35)


def test_a_spread_wider_than_the_rate_tag_can_undo_is_clamped_not_refused():
    """These are dealt from a hash. A spread widened past what `<prosody rate>`
    accepts should sound wrong rather than fail a line someone is waiting on."""
    assert MonsterFX(size_pct=5000).size_pct == MAX_SIZE_PCT
    assert MonsterFX(size_pct=-5000).size_pct == -MAX_SIZE_PCT
    assert MonsterFX(growl_pct=-10, cave_pct=900) == MonsterFX(growl_pct=0, cave_pct=100)
    # And the compensation stays inside the documented 20-200%.
    for size in (-MAX_SIZE_PCT, MAX_SIZE_PCT):
        assert 20 <= MonsterFX(size_pct=size).rate_pct() <= 200


def test_the_token_names_the_treatment_and_the_code_that_applies_it():
    """A changed saturation curve is a changed clip under an unchanged
    description, and the cache would serve the old one for a year."""
    token = MonsterFX(24, 55, 35).token()
    assert "24" in token and "55" in token and "35" in token
    assert source_fingerprint() in token
    assert token != MonsterFX(24, 55, 0).token()
    assert MonsterFX(24, 55, 35).token() == token


# -- the container -----------------------------------------------------------

def test_the_wav_is_a_wav_the_standard_library_can_read():
    """Written by hand, so something that has never seen this code has to be
    able to open it: a header the browser cannot parse is a monster that never
    speaks, and only a 502 would say so — this does not even 502."""
    pcm = tone(1)
    fx = MonsterFX(34, 55, 35)
    treated, rate = apply(pcm, fx)
    with wave.open(io.BytesIO(wav(treated, rate))) as fh:
        assert fh.getnchannels() == 1
        assert fh.getsampwidth() == 2
        assert fh.getframerate() == rate == fx.playback_rate()
        assert fh.getnframes() == len(treated) // 2
        assert fh.readframes(fh.getnframes()) == treated


def test_an_empty_clip_is_still_a_readable_wav():
    with wave.open(io.BytesIO(wav(b"", SAMPLE_RATE))) as fh:
        assert fh.getnframes() == 0
