"""The monster treatment: what it does to a clip, and what it must not.

`tts/dsp.py` is the only place in this app that touches audio samples. Nothing
downstream can tell a correct treatment from a wrong one — a monster that comes
back too loud, too long, or clipped still plays, and the page has no way to
say so — which is what these are for.

Everything here is arithmetic on a synthetic tone. Whether the result *sounds*
better than the standard engine it replaced is a judgement, and
`tools/polly_check.py --ab` is where it is made.

There are seventeen knobs now and only three of them are dealt by the casting,
so most of what is below iterates `FIELDS` and `EFFECT_CHAIN` rather than
naming effects one at a time: a knob added to those tables is covered by the
whole per-effect sweep the day it is added, and a knob added anywhere else
fails `test_the_chain_covers_every_knob_but_the_size_shift` on its way in.
Where a test does name one effect, it is because that effect's docstring makes
a claim of its own — a comb with no tail, a band that is the telephone's, a
gate that does not click — and the claim is the test.

Spectra are measured with a Goertzel filter at one frequency at a time.
`numpy` would be an FFT and one line, and it is not a dependency of this app;
`requirements.txt` is deliberately tiny and audio is not the reason to grow it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import math
import os
import pathlib
import subprocess
import sys
import wave
from array import array

import pytest

from tts import dsp
from tts.dsp import (
    BOUNDS,
    CAVE_DELAY_S,
    EFFECT_CHAIN,
    FIELDS,
    MAX_SIZE_PCT,
    SAMPLE_RATE,
    MonsterFX,
    apply,
    source_fingerprint,
    wav,
)

#: The effect functions as this module found them, so that a test which swaps
#: recorders in can put the real ones back whatever it does.
_ORIGINAL_EFFECTS = {func: getattr(dsp, func) for _name, func in EFFECT_CHAIN}

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


# -- what the fourteen newer effects need to be measured with ----------------


def voice(seconds: int = 1, rate: int = SAMPLE_RATE, top: int = 32000) -> bytes:
    """Speech-shaped, and as hot as Polly hands a line over.

    `tone` sits at about a quarter of full scale, which leaves twelve decibels
    of headroom that a real clip does not have: a synthesized line arrives
    normalized close to the format, and several effects here only cost level
    when there is nowhere for a raised peak to go. This is a harmonic stack
    under a syllabic envelope — a crest factor near a voice's rather than a
    sine's — scaled to whatever peak the test wants.
    """
    raw = []
    for i in range(seconds * rate):
        seconds_in = i / rate
        envelope = 0.15 + 0.85 * max(0.0, math.sin(2 * math.pi * 3.5 * seconds_in)) ** 2
        body = sum(
            math.sin(2 * math.pi * 180 * k * seconds_in + k) / k for k in range(1, 9)
        )
        raw.append(envelope * body)
    loudest = max(abs(v) for v in raw) or 1.0
    return array("h", [int(top * v / loudest) for v in raw]).tobytes()


def floats(pcm: bytes) -> list[float]:
    """What the effect functions themselves take: `work`, at 16-bit scale."""
    return [float(v) for v in samples(pcm)]


def sine(freq: float, seconds: float = 0.5, amp: float = 8000.0,
         rate: int = SAMPLE_RATE) -> list[float]:
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(int(seconds * rate))]


def cosine(freq: float, seconds: float = 0.5, amp: float = 8000.0,
           rate: int = SAMPLE_RATE) -> list[float]:
    """A tone that does not start at zero, for the two effects whose claim is
    about what they do to the first sample."""
    return [amp * math.cos(2 * math.pi * freq * i / rate) for i in range(int(seconds * rate))]


def impulse(where: int = 100, length: int = 4000, rate: int = SAMPLE_RATE) -> list[float]:
    """One sample, so that what an effect hands back is countable."""
    work = [0.0] * max(length, int(rate * 0.25))
    work[where] = 10000.0
    return work


def level(work: list[float]) -> float:
    """RMS of a float pass, which is what `_to_pcm` puts back afterwards."""
    return (sum(v * v for v in work) / len(work)) ** 0.5 if work else 0.0


def crest(work: list[float]) -> float:
    """Peak over RMS: the cheapest signature of a nonlinearity that does not
    need a spectrum, and the number `_to_pcm`'s second correction pays for."""
    return max(abs(v) for v in work) / level(work)


def correlation(left, right) -> float:
    """Normalized cross-correlation at zero lag: 1.0 for the same waveform at
    any level, and the right measure for "did this effect change the sound".

    A sample-wise difference is the wrong one: `_highpass` and `_vibrato` are
    phase shifts, so at the bottom of their sliders every sample has moved and
    the waveform has not.
    """
    count = min(len(left), len(right))
    both = sum(float(left[i]) * float(right[i]) for i in range(count))
    here = sum(float(left[i]) ** 2 for i in range(count))
    there = sum(float(right[i]) ** 2 for i in range(count))
    if here <= 0.0 or there <= 0.0:
        return 0.0
    return both / math.sqrt(here * there)


def goertzel(values, freq: float, rate: int = SAMPLE_RATE) -> float:
    """Magnitude at one frequency, per sample: half the amplitude of a sine at
    `freq`, and near zero where there is nothing there.

    Goertzel rather than an FFT because the questions here are all of the form
    "is there energy at 3400 Hz" and because `numpy` is not a dependency.
    """
    count = len(values)
    if not count:
        return 0.0
    coefficient = 2.0 * math.cos(2.0 * math.pi * freq / rate)
    previous = older = 0.0
    for value in values:
        current = float(value) + coefficient * previous - older
        older, previous = previous, current
    magnitude = previous * previous + older * older - coefficient * previous * older
    return math.sqrt(max(0.0, magnitude)) / count


def in_band(values, freq: float, chunks: int = 8, rate: int = SAMPLE_RATE) -> float:
    """`goertzel`, but averaged over `chunks` shorter windows — a wider bin.

    A one-second window makes a 1 Hz-wide bin, and a bin that narrow answers
    "is there a tone at exactly this frequency", which is not the question
    anywhere the thing under test tracks a pitch it had to estimate. The
    sub-octave lands at half the *estimated* fundamental, and the estimate is
    good to about half a percent — nine cents, well under what anyone can hear
    and enough to walk a 1 Hz bin half out of it and read a real octave as a
    50% loss.

    Eight windows makes the bin 8 Hz wide, which tolerates that offset and
    still hears nothing where there is nothing. It is deliberately not a free
    pass: `test_the_sub_octave_lands_on_the_octave_and_not_near_it` pins how
    far the pitch may actually be off, so what this widening tolerates is
    measured somewhere rather than assumed.
    """
    width = len(values) // chunks
    if width < 2:
        return goertzel(values, freq, rate)
    return sum(goertzel(values[k * width:(k + 1) * width], freq, rate)
               for k in range(chunks)) / chunks


def biggest_step(work: list[float]) -> float:
    """The largest jump between neighbouring samples, which is what a click
    is."""
    return max(abs(work[i] - work[i - 1]) for i in range(1, len(work)))


def deflections(name: str, size: int = 100) -> tuple[int, ...]:
    """Full deflection of a knob, both ways where the knob is signed."""
    low, _high = BOUNDS[name]
    return (size, -size) if low < 0 else (size,)


def record_chain(fx: MonsterFX) -> list[str]:
    """Which effects `apply` reaches, in the order it reaches them.

    Every effect is swapped for a recorder that does nothing, so this says what
    the chain did rather than what the audio sounds like.
    """
    order: list[str] = []

    def recorder(func):
        def record(work, pct, rate):
            order.append(func)
        return record

    for _name, func in EFFECT_CHAIN:
        setattr(dsp, func, recorder(func))
    try:
        apply(tone(1), fx)
    finally:
        for _name, func in EFFECT_CHAIN:
            setattr(dsp, func, _ORIGINAL_EFFECTS[func])
    return order


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


# -- the tables the module is driven by --------------------------------------
#
# `FIELDS`, `BOUNDS` and `EFFECT_CHAIN` are the module's only description of
# itself: `__post_init__` clamps from one, `token()` spells from one, `apply`
# dispatches from another, and `voices.py` and the voice lab build from them
# rather than from a hand-written list. So these are checked against each other
# and against the dataclass, and everything below iterates them rather than
# naming seventeen knobs — knob eighteen is covered the day it is added.

def test_every_effect_in_the_chain_names_a_knob_and_a_function():
    """`apply` dispatches through `globals()[func]`, so a typo in either half
    of a chain entry is an exception on a line a listener is waiting for rather
    than an import error anyone would have seen."""
    for name, func in EFFECT_CHAIN:
        assert name in BOUNDS, f"{name} is not a knob"
        applied = getattr(dsp, func, None)
        assert callable(applied), f"{func} is not a function in tts.dsp"
    # And no knob is applied twice, which would be a silently doubled effect.
    names = [name for name, _ in EFFECT_CHAIN]
    funcs = [func for _, func in EFFECT_CHAIN]
    assert len(set(names)) == len(names)
    assert len(set(funcs)) == len(funcs)


def test_the_field_table_is_the_dataclass_and_the_dataclass_is_the_field_table():
    """`FIELDS` is declaration order on purpose — `MonsterFX(20, 55, 40)` has
    meant size, growl, room since there were only three — and every knob is 0
    for "not dealt", which is what makes a default `MonsterFX` the null
    treatment."""
    declared = [f.name for f in dataclasses.fields(MonsterFX)]
    assert [name for name, _, _ in FIELDS] == declared
    for name, _lo, _hi in FIELDS:
        assert getattr(MonsterFX(), name) == 0, f"{name} does not default to 0"
    assert declared[:3] == ["size_pct", "growl_pct", "cave_pct"]


def test_the_bounds_are_the_field_table_by_another_name():
    assert BOUNDS == {name: (lo, hi) for name, lo, hi in FIELDS}
    for name, lo, hi in FIELDS:
        assert lo <= 0 <= hi, f"{name} cannot be turned off"


def test_the_chain_covers_every_knob_but_the_size_shift():
    """`size_pct` is the sample rate in the WAV header and never a pass over
    the audio; everything else is a pass and has to be in the chain, or a knob
    would be dealt, keyed into the cache, and do nothing."""
    assert {name for name, _ in EFFECT_CHAIN} == {n for n, _, _ in FIELDS} - {"size_pct"}


def test_every_knob_is_clamped_to_its_own_bounds():
    """The table-driven half of `test_a_spread_wider_than_the_rate_tag_can_undo
    _is_clamped_not_refused`: whatever is added to `FIELDS` is clamped from
    `FIELDS`, so a new knob cannot arrive un-clamped."""
    for name, lo, hi in FIELDS:
        assert getattr(MonsterFX(**{name: 10_000}), name) == hi
        assert getattr(MonsterFX(**{name: -10_000}), name) == lo


def test_every_knob_on_its_own_is_a_treatment():
    """`bool(fx)` gates the whole thing at `Cast.fx` and at the cache key."""
    for name, _lo, hi in FIELDS:
        assert MonsterFX(**{name: hi}), name
    assert not MonsterFX()


# -- the null treatment ------------------------------------------------------

def test_a_size_only_treatment_is_answered_without_reading_a_sample():
    """`touches_samples()` is what says so, and `apply` is what has to honour
    it — this is the case nearly every monster on the casting is in, and the
    reason the size shift was moved into the header at all. Proved by making
    every effect in the chain fatal: a treatment that reaches one has read the
    samples."""
    def refuse(work, pct, rate):  # pragma: no cover - the point is it is not called
        raise AssertionError("apply read the samples for a size-only treatment")

    for _name, func in EFFECT_CHAIN:
        setattr(dsp, func, refuse)
    try:
        assert not MonsterFX(size_pct=40).touches_samples()
        assert not MonsterFX().touches_samples()
        pcm = tone(1)
        assert apply(pcm, MonsterFX(size_pct=40))[0] == pcm
        assert apply(pcm, MonsterFX())[0] == pcm
    finally:
        for _name, func in EFFECT_CHAIN:
            setattr(dsp, func, _ORIGINAL_EFFECTS[func])
    # ...and any single knob in the chain does say the samples are touched.
    for name, _func in EFFECT_CHAIN:
        assert MonsterFX(**{name: 1}).touches_samples(), name


# -- every effect, one at a time ---------------------------------------------

def test_every_effect_tends_to_the_untreated_voice_as_its_knob_goes_to_zero():
    """The bottom of every slider in the voice lab has to be silence-of-effect,
    or dragging one from 0 jumps instead of opening.

    Measured as normalized cross-correlation rather than sample-wise
    difference, because `_highpass` and `_vibrato` are phase shifts: they move
    every sample and are still, correctly, the same waveform arriving a
    fraction of a sample later.
    """
    pcm = tone(1)
    plain = samples(pcm)
    for name, _func in EFFECT_CHAIN:
        for pct in deflections(name, 1):
            treated, _rate = apply(pcm, MonsterFX(**{name: pct}))
            assert len(treated) == len(pcm), name
            assert correlation(plain, samples(treated)) > 0.985, (name, pct)


def test_no_effect_lengthens_or_shortens_the_line():
    """A clip that came back longer than it went out would desynchronize the
    narration hold from the board, which is the one timing anything downstream
    depends on."""
    pcm = tone(1)
    for name, _func in EFFECT_CHAIN:
        for pct in deflections(name):
            treated, _rate = apply(pcm, MonsterFX(**{name: pct}))
            assert len(treated) == len(pcm), (name, pct)


def test_no_effect_hands_back_a_number_that_is_not_a_number():
    """The recursions here are all argued to be stable in their docstrings.
    `_to_pcm` would raise on a NaN and hand a 502 to a page that hides it by
    speaking the line itself, so nothing would say which filter ran away."""
    voiced = floats(tone(1))
    for name, func in EFFECT_CHAIN:
        for pct in deflections(name):
            work = list(voiced)
            getattr(dsp, func)(work, pct, SAMPLE_RATE)
            assert all(math.isfinite(v) for v in work), (name, pct)


def test_silence_stays_silence_under_every_effect():
    """A gap between two lines that came back with a hiss, a hum or a click in
    it would be audible on every monster in the game."""
    quiet = b"\x00" * 8000
    for name, _func in EFFECT_CHAIN:
        for pct in deflections(name):
            assert apply(quiet, MonsterFX(**{name: pct}))[0] == quiet, (name, pct)


def test_no_effect_turns_a_voice_into_silence():
    """Every one of these is a costume. A monster whose line came back empty
    plays as a monster that did not speak, and nothing downstream can tell that
    from a monster with nothing to say."""
    pcm = tone(1)
    before = rms(pcm)
    for name, _func in EFFECT_CHAIN:
        for pct in deflections(name):
            treated, _rate = apply(pcm, MonsterFX(**{name: pct}))
            assert rms(treated) > 0.1 * before, (name, pct)
            assert treated != pcm, (name, pct)


def test_the_level_is_held_through_every_effect_where_the_format_has_room():
    """The claim `apply` makes in its own comment: a saturator compresses
    upward, a comb adds energy, a gate takes it away, and the clip comes back
    at the level it went in at so a monster is not mixed over the narrator.

    "Where the format has room" is the qualification, and it is not a small
    one — see `test_the_level_hold_gives_way_to_the_format_when_there_is_no
    _headroom` for what happens on a clip Polly has already normalized.
    """
    pcm = tone(1)
    before = rms(pcm)
    for name, _func in EFFECT_CHAIN:
        for pct in deflections(name):
            treated, _rate = apply(pcm, MonsterFX(**{name: pct}))
            assert rms(treated) == pytest.approx(before, rel=0.01), (name, pct)
            assert peak(treated) <= dsp.PEAK, (name, pct)


def test_every_effect_is_deterministic():
    """Every clip is written to disk once and served for a year: an effect that
    rendered differently the second time would be a bug with no observer."""
    pcm = tone(1)
    for name, _func in EFFECT_CHAIN:
        for pct in deflections(name):
            fx = MonsterFX(**{name: pct})
            assert apply(pcm, fx) == apply(pcm, fx), (name, pct)


def test_a_clip_of_almost_no_samples_survives_every_effect_at_every_size():
    """Every effect here reads backwards, or forwards past a delay, or in
    grains — all of which have an end to fall off. A clip shorter than one
    grain, one delay or one gate window is the case a truncated Polly response
    lands in, and it must cost a sample rather than the line."""
    for count in (0, 1, 2, 3, 7):
        pcm = array("h", [(-1) ** i * (3000 + 900 * i) for i in range(count)]).tobytes()
        for size in (-MAX_SIZE_PCT, -16, 0, 34, MAX_SIZE_PCT):
            for name, _func in EFFECT_CHAIN:
                for pct in deflections(name):
                    treated, rate = apply(pcm, MonsterFX(size_pct=size, **{name: pct}))
                    assert len(treated) == len(pcm), (count, size, name, pct)
                    assert rate >= 1


def test_a_trailing_odd_byte_costs_a_sample_under_every_effect():
    """The existing guarantee, held for the fourteen that arrived after it."""
    for name, _func in EFFECT_CHAIN:
        for pct in deflections(name):
            treated, _rate = apply(b"\x01\x02\x03", MonsterFX(**{name: pct}))
            assert len(treated) == 2, (name, pct)


# -- the order the chain runs in ---------------------------------------------

def test_the_chain_applies_its_effects_in_the_order_the_table_gives():
    """`EFFECT_CHAIN` is not declaration order and the difference is the whole
    point of the second table: what generates content first, the
    nonlinearities while the signal is still clean, then filters, then
    modulation, then the room."""
    order = record_chain(MonsterFX(**{name: 50 for name, _ in EFFECT_CHAIN}))
    assert order == [func for _, func in EFFECT_CHAIN]
    # Only what is dealt is applied, and in chain order rather than the
    # declaration order these two happen to disagree about.
    assert record_chain(MonsterFX(growl_pct=50, suboctave_pct=50)) == [
        "_suboctave", "_saturate",
    ]
    assert record_chain(MonsterFX(size_pct=30)) == []


def test_the_two_effects_that_chop_the_level_come_last():
    """A gate ahead of a reverb tail cuts the tail, which is a fault rather
    than an effect — so the room is made before anything takes level away."""
    order = [name for name, _ in EFFECT_CHAIN]
    assert order[-2:] == ["tremolo_pct", "stutter_pct"]
    assert order.index("cave_pct") < order.index("tremolo_pct")
    assert order.index("slap_pct") < order.index("cave_pct")
    # And the generators come before the nonlinearities that shape what they made.
    assert order.index("suboctave_pct") < order.index("growl_pct")
    assert order.index("noise_pct") < order.index("growl_pct")


# -- what each effect claims to be -------------------------------------------

def test_the_feedforward_combs_leave_no_tail_and_the_cave_does():
    """`_metal` and `_slap` run backwards down the clip precisely so their tap
    reads the input rather than an output. Going forward in place would make
    either of them `_cave` with a short delay — the one mistake in this module
    that would still produce plausible audio, and so the one worth a test.

    An impulse in, and the answer is countable: one tap out per tap in."""
    for func, taps in (("_metal", 2), ("_slap", 2)):
        work = impulse()
        getattr(dsp, func)(work, 100, SAMPLE_RATE)
        assert sum(1 for v in work if v) == taps, func
    # The comb with feedback is the contrast: it rings until it runs out of clip.
    work = impulse()
    dsp._cave(work, 100, SAMPLE_RATE)
    assert sum(1 for v in work if v) > 3


def test_the_two_combs_are_a_timbre_and_a_surface_rather_than_two_echoes():
    """`METAL_DELAY_S` is short enough to be heard as what the creature is made
    of and `SLAP_DELAY_S` as what it is standing in front of; both are counted
    in samples of the playback rate, like `_cave`, so a creature shifted down
    is not made of something larger."""
    for func, seconds in (("_metal", dsp.METAL_DELAY_S), ("_slap", dsp.SLAP_DELAY_S)):
        for rate in (8000, SAMPLE_RATE):
            work = impulse(rate=rate)
            getattr(dsp, func)(work, 100, rate)
            landed = [i for i, v in enumerate(work) if v]
            assert (landed[1] - landed[0]) / rate == pytest.approx(seconds, abs=1.0 / rate)
    assert dsp.METAL_DELAY_S < 0.010 < dsp.SLAP_DELAY_S < 0.030 < dsp.CAVE_DELAY_S


def test_the_formant_bell_is_signed_the_way_the_size_shift_is():
    """The one knob that moves an emphasis without moving pitch, which is the
    axis `<amazon:effect vocal-tract-length>` owned. Positive is a bigger
    creature: energy into the first-formant region. Negative is the small one,
    up near the third."""
    def bell(freq, pct):
        work = sine(freq)
        before = goertzel(work, freq)
        dsp._formant(work, pct, SAMPLE_RATE)
        return 20.0 * math.log10(goertzel(work, freq) / before)

    assert bell(dsp.FORMANT_BIG_HZ, 100) == pytest.approx(dsp.FORMANT_GAIN_DB, abs=0.2)
    assert bell(dsp.FORMANT_SMALL_HZ, -100) == pytest.approx(dsp.FORMANT_GAIN_DB, abs=0.2)
    # Symmetric: each deflection lifts its own centre and leaves the other's.
    assert bell(dsp.FORMANT_SMALL_HZ, 100) < 0.5
    assert bell(dsp.FORMANT_BIG_HZ, -100) < 0.5
    # The gain is the knob and the centre never moves, so half the deflection
    # is half the decibels rather than a bell parked somewhere else.
    assert bell(dsp.FORMANT_BIG_HZ, 50) == pytest.approx(dsp.FORMANT_GAIN_DB / 2, abs=0.2)
    assert bell(dsp.FORMANT_SMALL_HZ, -50) == pytest.approx(dsp.FORMANT_GAIN_DB / 2, abs=0.2)


def test_the_formant_bell_at_zero_is_not_a_filter_at_all():
    """At `A = 1` the cookbook's numerators and denominators are equal term by
    term, so the section is provably transparent — but the chain never runs it
    at 0 anyway, which is the stronger guarantee and the one that matters for
    the slider."""
    assert record_chain(MonsterFX(formant_pct=0)) == []
    work = sine(1000.0)
    before = list(work)
    dsp._formant(work, 0, SAMPLE_RATE)
    for got, want in zip(work, before):
        assert got == pytest.approx(want, abs=1e-6)


def test_the_telephone_band_is_the_telephone_band():
    """G.712's passband, borrowed whole, because every listener alive has heard
    exactly this band. One pole either way — gentle on purpose — so this is a
    test that the chest and the air are gone, not that the skirts are steep."""
    def gain(freq):
        work = sine(freq)
        before = goertzel(work, freq)
        dsp._phone(work, 100, SAMPLE_RATE)
        return goertzel(work, freq) / before

    inside = gain(1000.0)
    assert inside > 0.8 and gain(1500.0) > 0.8
    # Under 300 Hz: the chest.
    assert gain(100.0) < 0.45 < gain(200.0) < inside
    # Over 3400 Hz: the air.
    assert gain(7000.0) < 0.65
    assert gain(7000.0) < gain(5000.0) < inside


def test_the_thinning_slider_moves_the_corner_in_musical_intervals():
    """"Frequency is heard in ratios": the corner slides geometrically from
    `HIGHPASS_MIN_HZ` to `HIGHPASS_MAX_HZ`, so every percent of the slider is
    the same interval and the bottom of it is genuinely inaudible. A linear
    sweep would spend its whole bottom half above 700 Hz, where every step is
    already a big change, and its top half doing nothing.

    Checked where a corner is: the half-power point, taken against the filter's
    own passband — a one-pole run as `r·(y + x - x[-1])` passes the top of the
    band at `2r/(1 + r)` rather than at 1, which costs level and no shape and
    is put back by `_to_pcm`.
    """
    def corner_of(pct):
        return dsp.HIGHPASS_MIN_HZ * (dsp.HIGHPASS_MAX_HZ / dsp.HIGHPASS_MIN_HZ) ** (pct / 100.0)

    def gain(freq, pct):
        work = sine(freq)
        before = goertzel(work, freq)
        dsp._highpass(work, pct, SAMPLE_RATE)
        return goertzel(work, freq) / before

    for pct in (10, 25, 50, 75):
        half_power = gain(corner_of(pct), pct) / gain(6000.0, pct)
        assert half_power == pytest.approx(1 / math.sqrt(2), abs=0.02), pct
    # Half the slider is the geometric mean of its ends, not the arithmetic one.
    assert corner_of(50) == pytest.approx(
        math.sqrt(dsp.HIGHPASS_MIN_HZ * dsp.HIGHPASS_MAX_HZ), rel=1e-6
    )
    assert corner_of(50) < (dsp.HIGHPASS_MIN_HZ + dsp.HIGHPASS_MAX_HZ) / 2
    # And at the top there is no chest left: 100 Hz is 25 dB down.
    assert gain(100.0, 100) < 0.06


def test_the_growl_that_spares_the_consonants_leaves_the_high_band_alone():
    """`MAX_DRIVE` is where it is because past it "the consonants stop
    arriving", and the consonants are the high band — so `_bandgrowl` drives
    the low band far past that limit and hands the high band back untouched."""
    low, high = 150.0, 4000.0
    work = [a + b for a, b in zip(sine(low), sine(high, amp=3000.0))]
    before_low = goertzel(work, low)
    before_high = goertzel(work, high)
    before_third = goertzel(work, 3 * low)
    dsp._bandgrowl(work, 100, SAMPLE_RATE)
    # The high half is `x - low` by construction, so it survives the drive...
    high_gain = goertzel(work, high) / before_high
    assert high_gain == pytest.approx(1.0, rel=0.15)
    # ...where the low half, driven at `BANDGROWL_DRIVE`, plainly does not.
    low_gain = goertzel(work, low) / before_low
    assert abs(low_gain - 1.0) > 10 * abs(high_gain - 1.0)
    # ...and the low half has been driven hard enough to grow harmonics that
    # were not in the input at all.
    assert before_third < 1.0 < 100.0 < goertzel(work, 3 * low)
    assert dsp.BANDGROWL_DRIVE > dsp.MAX_DRIVE


def test_the_gate_ramps_its_edges_rather_than_clicking():
    """A click is the one artefact a listener reads as a broken file rather
    than as a monster, and `STUTTER_RAMP_S` is the whole defence. Measured as
    the largest step between neighbouring samples, which is what a click is."""
    work = sine(180.0, seconds=1.0, amp=20000.0)
    plain = biggest_step(work)
    ramped = list(work)
    dsp._stutter(ramped, 100, SAMPLE_RATE)
    assert biggest_step(ramped) < 1.1 * plain
    # The contrast: the same gate without the ramps, which is what this would
    # be if `STUTTER_RAMP_S` were ever taken out.
    width = max(2, int(SAMPLE_RATE / dsp.STUTTER_HZ))
    closed = int(width * dsp.STUTTER_MAX_DUTY)
    floor = 1.0 - dsp.STUTTER_MAX_CUT
    hard = [v * (floor if i % width >= width - closed else 1.0) for i, v in enumerate(work)]
    assert biggest_step(hard) > 5 * plain


def test_the_gate_leaves_something_of_the_line_to_hear():
    """"An unintelligible monster is a failed one however good the effect is":
    neither the duty nor the cut reaches everything, so no window is silent and
    no syllable is removed whole."""
    assert 0 < dsp.STUTTER_MAX_DUTY < 1 and 0 < dsp.STUTTER_MAX_CUT < 1
    work = sine(180.0, seconds=1.0, amp=20000.0)
    dsp._stutter(work, 100, SAMPLE_RATE)
    width = max(2, int(SAMPLE_RATE / dsp.STUTTER_HZ))
    for start in range(0, len(work) - width, width):
        window = work[start:start + width]
        assert max(abs(v) for v in window) > 0.0


def test_the_wobble_and_the_pulse_both_start_where_the_line_starts():
    """Both are raised cosines rather than sines so the first syllable is not
    sometimes half there, and `_vibrato`'s delay sweeps from zero rather than
    either side of a centre — which is what makes it testable for transparency
    at all."""
    for func in ("_vibrato", "_tremolo"):
        work = cosine(180.0, amp=10000.0)
        started = work[0]
        getattr(dsp, func)(work, 100, SAMPLE_RATE)
        assert work[0] == pytest.approx(started)
        assert any(v != w for v, w in zip(work, cosine(180.0, amp=10000.0)))


def test_the_fold_cannot_hand_back_anything_past_full_scale():
    """The triangle is bounded by ±1 by construction, however hard the drive
    is — so `_to_pcm` never has to scale a folded clip down, which is the
    reason for folding rather than clipping."""
    work = [4.0 * v for v in floats(tone(1))]     # deliberately way over
    dsp._fold(work, 100, SAMPLE_RATE)
    assert max(abs(v) for v in work) <= dsp.PEAK


def test_the_sub_octave_puts_an_octave_under_the_voice():
    """What the effect is for: energy at half the fundamental, mixed under a
    dry signal whose own fundamental is left where it was."""
    f0 = 160.0
    work = sine(f0, seconds=1.0)
    assert goertzel(work, f0 / 2) < 1.0            # nothing down there to start
    before = goertzel(work, f0)
    dsp._suboctave(work, 100, SAMPLE_RATE)
    # Under, never level with: the intelligibility of the line is the dry
    # signal's, so the octave arrives at `SUBOCTAVE_MAX_MIX` and no louder.
    assert goertzel(work, f0 / 2) == pytest.approx(
        before * dsp.SUBOCTAVE_MAX_MIX, rel=0.05
    )
    assert goertzel(work, f0) == pytest.approx(before, rel=0.02)


def test_the_sub_octave_arrives_at_every_fundamental_a_human_voice_uses():
    """The defect this test used to pin, now the other way round.

    Two grains overlap at every output sample and each reads at half speed, so
    they read the source `hop/2` apart — a phase difference at the fundamental,
    which means they ADD where that offset is a whole number of periods and
    CANCEL where it is half of one. With the hop fixed at half a fixed grain
    that made the effect a function of the speaker: at a 400-sample hop the
    octave arrived at full weight at 160 Hz and 240 Hz and vanished at 120,
    140, 180, 200 and 220, leaving a ±20 Hz flutter doublet where it should
    have been. Five of those seven are inside the 85-255 Hz a human voice
    occupies, so the knob did nothing for most speakers.

    The hop is now a whole number of estimated pitch periods, so it is right
    for whatever it is given. Swept across the range rather than tested at one
    pitch, because one pitch is exactly how the old arrangement passed.
    """
    for f0 in (90.0, 110.0, 130.0, 160.0, 190.0, 220.0, 250.0):
        work = sine(f0, seconds=1.0)
        # Narrow bin for this one: `in_band`'s wider one is wide enough to
        # catch the skirt of the fundamental itself, which is not the thing
        # being asked about here.
        assert goertzel(work, f0 / 2) < 1.0         # nothing down there to start
        before = goertzel(work, f0)
        dsp._suboctave(work, 100, SAMPLE_RATE)
        arrived = in_band(work, f0 / 2)
        assert arrived == pytest.approx(before * dsp.SUBOCTAVE_MAX_MIX, rel=0.08), (
            f"the octave under {f0:.0f} Hz arrived at "
            f"{arrived / before:.3f} of the fundamental"
        )
        # ...and the voice itself is still where it was, underneath it.
        assert goertzel(work, f0) == pytest.approx(before, rel=0.03)


def test_the_sub_octave_lands_on_the_octave_and_not_near_it():
    """What `in_band`'s wider bin is allowed to tolerate, measured.

    The grain hop is two estimated periods, so the octave is at half the
    *estimated* pitch rather than half the true one, and a systematically wrong
    estimate would be a systematically detuned monster — audible as an
    interval, not as a loss. Scanning either side says how far off it actually
    lands: within a percent, which is under twenty cents and under what a
    voice's own pitch does inside a syllable.
    """
    for f0 in (110.0, 190.0, 250.0):
        work = sine(f0, seconds=1.0)
        dsp._suboctave(work, 100, SAMPLE_RATE)
        offsets = [d / 1000.0 for d in range(-40, 41, 2)]
        best = max(offsets, key=lambda d: goertzel(work, f0 / 2 * (1.0 + d)))
        assert abs(best) <= 0.01, f"the octave under {f0:.0f} Hz landed {best:+.1%} out"


def test_the_sub_octave_falls_back_to_a_fixed_grain_where_there_is_no_pitch():
    """Noise has no period to be in phase with — and nothing periodic to
    cancel either, which is why the fixed grain is still the right answer
    there and `SUBOCTAVE_GRAIN_S` is still a constant.

    What must not happen is the estimator finding a "period" in a fricative and
    handing back a grain length that wanders: the effect has to stay bounded,
    finite and quiet on material it cannot track.
    """
    state = 12345
    noise = []
    for _ in range(SAMPLE_RATE):
        state = (1103515245 * state + 12345) % (2 ** 31)
        noise.append(8000.0 * (state / (2 ** 30) - 1.0))
    periods, _block = dsp._periods(noise, SAMPLE_RATE)
    assert periods and not any(periods), "noise was called voiced"
    work = list(noise)
    dsp._suboctave(work, 100, SAMPLE_RATE)
    assert all(v == v and abs(v) < 1e9 for v in work)
    assert rms(work) == pytest.approx(rms(noise), rel=0.6)


def test_the_period_search_is_the_search_the_constants_describe():
    """`_periods` answers in samples of the rate it was handed, over the range
    the constants name — the two facts `_suboctave` relies on to turn an
    answer into a hop, and the two a change to the decimation could break
    silently."""
    for rate in (SAMPLE_RATE, 12903, 20000):
        for f0 in (dsp.SUBOCTAVE_MIN_HZ + 10.0, 150.0, dsp.SUBOCTAVE_MAX_HZ - 10.0):
            found = [p for p in dsp._periods(sine(f0, 0.5, rate=rate), rate)[0] if p]
            assert found, f"{f0:.0f} Hz at {rate} Hz went undetected"
            # An octave error is harmless here — a whole number of periods is
            # still a whole number of periods — so what is checked is that the
            # answer is a MULTIPLE of the period, not that it is the period.
            for period in found:
                ratio = period / (rate / f0)
                assert abs(ratio - round(ratio)) < 0.02 and 1 <= round(ratio) <= 3


# -- the breath, which is the only effect with a generator in it -------------

def test_the_breath_is_the_same_breath_in_the_next_process():
    """`_noise` is seeded from a constant and runs its own LCG rather than
    `random`, whose stream is a CPython implementation detail. What that buys
    is this: a clip rendered today and a clip rendered by a later process —
    different interpreter, different hash seed — are the same bytes, so the
    on-disk cache serves a line that still sounds like the one it replaced.

    Checked across processes because that is the claim; a same-process repeat
    would pass on a generator seeded from `id()`.
    """
    pcm = array("h", [((i * 37) % 4001) - 2000 for i in range(4000)]).tobytes()
    here = hashlib.sha256(apply(pcm, MonsterFX(noise_pct=70))[0]).hexdigest()
    root = str(pathlib.Path(dsp.__file__).resolve().parent.parent)
    script = (
        "import hashlib\n"
        "from array import array\n"
        "from tts.dsp import MonsterFX, apply\n"
        "pcm = array('h', [((i * 37) % 4001) - 2000 for i in range(4000)]).tobytes()\n"
        "print(hashlib.sha256(apply(pcm, MonsterFX(noise_pct=70))[0]).hexdigest())\n"
    )
    seen = set()
    for seed in ("0", "1", "982451653"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=root)
        done = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root, env=env, capture_output=True, text=True, check=True,
        )
        seen.add(done.stdout.strip())
    assert seen == {here}


def test_the_breath_arrives_with_the_voice_rather_than_under_the_whole_line():
    """Shaped by the voice's own envelope, because a constant hiss is a bad
    recording and a rasp that arrives only where the creature is speaking is a
    damaged throat."""
    rate = SAMPLE_RATE
    speaking = 4000
    work = [0.0] * (speaking + rate // 2)
    for i in range(speaking):
        work[i] = 8000.0 * math.sin(2 * math.pi * 180 * i / rate)
    dsp._noise(work, 100, rate)
    # The follower is one pole, so it does not slam shut; give it a moment.
    tail = work[speaking + rate // 8:]
    assert max(abs(v) for v in tail) < 1.0
    assert level(work[:speaking]) > 0.0


# -- what a treatment costs the level ----------------------------------------
#
# `_to_pcm` makes two corrections in order: back to the RMS the clip had, then
# down again if that put anything past full scale. The second one wins, so the
# level hold is only a hold while there is headroom to hold it in. Polly's
# output has very little, and the three tests below are what that costs.
# Numbers are this fixture's, not constants: what they are a function of is the
# clip's crest factor and how close its peak already is to the format.

def test_the_level_hold_gives_way_to_the_format_when_there_is_no_headroom():
    """The same effects that hold the level exactly on a clip with room come
    back materially quieter on one Polly has already normalized, because
    raising the peak is the only way most of them have to change the shape.

    Pinned rather than fixed: `_to_pcm` documents this ordering deliberately
    and normalizing is the right answer against clipping. What is worth
    knowing is the size of it — a monster a few decibels under the narrator
    reading the line before it — and that nothing downstream can say so.
    """
    hot = voice()
    before = rms(hot)
    # A quiet copy of the same waveform, which is the control: with headroom,
    # every one of these holds the level to a fraction of a percent.
    quiet = voice(top=8000)
    quiet_before = rms(quiet)

    treated, _rate = apply(hot, MonsterFX(rectify_pct=100))
    assert rms(treated) / before == pytest.approx(0.935, abs=0.02)
    treated, _rate = apply(quiet, MonsterFX(rectify_pct=100))
    assert rms(treated) / quiet_before == pytest.approx(1.0, abs=0.01)

    # The worst of them on this fixture is the highpass, which takes the chest
    # out and leaves a waveform that is all edge: a crest factor the peak
    # correction then has to pay for twice.
    treated, _rate = apply(hot, MonsterFX(highpass_pct=100))
    assert rms(treated) / before == pytest.approx(0.494, abs=0.02)
    treated, _rate = apply(quiet, MonsterFX(highpass_pct=100))
    assert rms(treated) / quiet_before == pytest.approx(1.0, abs=0.01)


def test_the_whole_chain_at_once_stacks_that_loss_rather_than_cancelling_it():
    """Nothing on the casting asks for this and only the voice lab can — but a
    lab that hands back something 3 dB down is a lab someone will trust about
    loudness, so the number is written down.

    It was 0.70 while the sub-octave was cancelling itself at most pitches:
    an effect that was not arriving was also not adding to the crest factor
    that `_to_pcm` is scaling for. Fixing the octave put it back where it
    belongs, and this number is the receipt.
    """
    hot = voice()
    everything = MonsterFX(**{name: 60 for name, _ in EFFECT_CHAIN})
    treated, _rate = apply(hot, everything)
    assert rms(treated) / rms(hot) == pytest.approx(0.67, abs=0.03)
    assert peak(treated) <= dsp.PEAK
    # And with headroom, the same sixteen at the same setting cost nothing.
    quiet = voice(top=8000)
    treated, _rate = apply(quiet, everything)
    assert rms(treated) / rms(quiet) == pytest.approx(1.0, abs=0.01)


def test_the_old_growl_knob_is_the_one_that_does_not_start_at_nothing():
    """A wart on `growl_pct`, which predates the fourteen and is not shared by
    any of them. Every effect added since is a no-op in the limit as its knob
    goes to zero — `_bandgrowl` is explicitly cross-faded against the dry band
    to make sure of it — but `_saturate` at `pct = 1` is already drive 1, and
    drive 1 through `x(27+x²)/(27+9x²)` scaled to unity is a 1.29x lift and a
    little compression with it.

    `apply` hides it, because `_to_pcm` puts the level back afterwards and that
    is most of what the lift was. It is visible on the raw pass, it is what the
    voice lab's growl slider does in its first percent, and it is the reason
    that slider behaves differently from the other fifteen.
    """
    voiced = floats(tone(1))
    work = list(voiced)
    dsp._saturate(work, 1, SAMPLE_RATE)
    assert level(work) / level(voiced) == pytest.approx(1.29, abs=0.02)
    assert crest(work) < crest(voiced)          # and already compressing

    # Every one of the fourteen, at the same setting, is within a percent or
    # two of doing nothing at all.
    for name, func in EFFECT_CHAIN:
        if func == "_saturate":
            continue
        for pct in deflections(name, 1):
            work = list(voiced)
            getattr(dsp, func)(work, pct, SAMPLE_RATE)
            assert level(work) / level(voiced) == pytest.approx(1.0, abs=0.02), (name, pct)


# -- the cache key -----------------------------------------------------------

def test_the_token_spells_only_the_knobs_that_are_dealt():
    """So a treatment keys the same length it always did however many knobs
    exist, and adding a knob nobody has turned on does not rewrite the key of
    every clip on disk."""
    assert MonsterFX().token() == f"fx::{source_fingerprint()}"
    for name, _lo, hi in FIELDS:
        spelled = MonsterFX(**{name: hi}).token()
        assert spelled == f"fx:{name[:-4]}={hi}:{source_fingerprint()}"
    # Two knobs are spelled in declaration order, which is `FIELDS` order.
    assert MonsterFX(cave_pct=35, growl_pct=55).token().startswith("fx:growl=55,cave=35:")


def test_no_two_treatments_share_a_token():
    """A collision is one monster served another monster's audio, from disk,
    for as long as the cache lives."""
    seen = {}
    treatments = [MonsterFX()]
    for name, lo, hi in FIELDS:
        for value in (1, hi, lo, hi - 1):
            if value:
                treatments.append(MonsterFX(**{name: value}))
    treatments.append(MonsterFX(24, 55, 35))
    treatments.append(MonsterFX(55, 24, 35))
    treatments.append(MonsterFX(**{name: 40 for name, _, _ in FIELDS}))
    for fx in treatments:
        token = fx.token()
        assert seen.setdefault(token, fx) == fx, token
        assert fx.token() == token          # stable within a process
        assert source_fingerprint() in token
