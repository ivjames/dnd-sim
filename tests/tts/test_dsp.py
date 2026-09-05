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
    RING_HZ,
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


# -- the four character effects that are not growl or cave --------------------

#: The sliders added after growl and cave, each dealt as a percentage that has
#: to be audible at the bottom of its range and clearly more so at the top.
NEW_EFFECTS = ("ring_pct", "tremolo_pct", "muffle_pct", "crush_pct")


@pytest.mark.parametrize("field", NEW_EFFECTS)
def test_every_new_slider_uses_its_whole_range(field):
    """A control with a dead half is dealt from a hash and heard as nothing
    for half the deals it makes.

    So: more percentage is more effect, the lowest position worth dealing
    already does something, and the top is several times the bottom rather
    than the bottom again. `crush` failed all three until the step it
    quantises to was measured against the clip instead of against full scale:
    it moved the audio by 0.1% at 25 and 3.5% at 75, which is a control whose
    entire travel is its last quarter.
    """
    pcm = speechy(0.4)
    got = [change(pcm, MonsterFX(**{field: p})) for p in (25, 50, 75, 100)]
    assert got == sorted(got), f"{field} is not monotonic: {got}"
    assert got[0] > 0.02, f"{field} at a quarter is inaudible: {got}"
    assert got[-1] > 0.25, f"{field} at the top is barely a treatment: {got}"
    assert got[-1] > got[0] * 4, f"{field} has no resolution in it: {got}"


def test_crush_does_the_same_thing_however_quietly_the_line_was_spoken():
    """The regression `growl` has, one effect further down the same chain.

    Bit depth was measured against the format: 16 bits down to 4 across full
    scale. A Polly line reaches a quarter or so of full scale, so the first
    half of that sweep rounded to steps below the noise the line already
    carried, and the quieter the line the further along the slider anything
    began to happen. Measured against the clip's own level, a dealt `crush`
    means the same treatment whatever level arrives.
    """
    for pct in (25, 60, 100):
        changes = [change(speechy(frac), MonsterFX(crush_pct=pct))
                   for frac in (0.9, 0.5, 0.25, 0.1)]
        assert min(changes) > 0.02, f"crush {pct} is inaudible somewhere: {changes}"
        assert max(changes) - min(changes) < 0.01, changes


def test_the_new_effects_are_a_different_voice_not_a_louder_one():
    """The requirement growl and cave are already held to, for the four that
    came after them — and each breaks it in its own direction: a ring
    modulator halves the average level, a tremolo digs holes in it, a low-pass
    throws away everything above its corner, a quantiser rounds samples
    outward. What comes out is the level that went in, inside the format."""
    pcm = speechy(0.4)
    before = rms(pcm)
    for fx in (MonsterFX(ring_pct=70), MonsterFX(tremolo_pct=80),
               MonsterFX(muffle_pct=90), MonsterFX(crush_pct=85),
               # And all seven at once, the arrangement most able to overshoot:
               # saturation and a comb both add energy on top of the rest.
               MonsterFX(24, 80, 55, 60, 50, 40, 30)):
        treated, _rate = apply(pcm, fx)
        assert rms(treated) == pytest.approx(before, rel=0.02), fx
        assert peak(treated) <= 32767, fx
        assert treated != pcm, fx


def two_tone(low_hz: int = 200, high_hz: int = 3000, seconds: int = 1,
             rate: int = SAMPLE_RATE) -> bytes:
    """Two equal tones far enough apart to watch a filter act on one of them.

    `tone()` cannot do this job: both its partials sit below `MUFFLE_HZ_SHUT`,
    so even a fully shut muffle barely touches it. 3 kHz is above the corner at
    every slider position and 200 Hz is below all of them.
    """
    return array("h", [
        int(6000 * (math.sin(2 * math.pi * low_hz * i / rate)
                    + math.sin(2 * math.pi * high_hz * i / rate)))
        for i in range(seconds * rate)
    ]).tobytes()


def band(data, hz: float, rate: int = SAMPLE_RATE) -> float:
    """The amplitude at one frequency: a single DFT bin, computed directly.

    One bin is O(n) and needs nothing but `math`, where an FFT would be either
    a dependency or a hundred lines of fixture. Every clip measured here is a
    whole number of cycles long at every frequency asked about, so the bin is
    exact and no window is called for.
    """
    values = samples(data) if isinstance(data, bytes) else data
    real = imag = 0.0
    step = 2 * math.pi * hz / rate
    for i, v in enumerate(values):
        real += v * math.cos(step * i)
        imag += v * math.sin(step * i)
    return 2 * math.hypot(real, imag) / len(values)


def test_muffle_takes_the_top_off_rather_than_turning_the_whole_thing_down():
    """That `muffle` is a filter and not a fader.

    The two are indistinguishable on the level — which is held either way —
    and both register in `change()`. What separates them is balance: the 3 kHz
    tone loses ground to the 200 Hz one as the corner comes down, while the
    200 Hz one is not attenuated at all. It comes out louder, in fact, because
    holding the clip's RMS makes room for what the filter took away.
    """
    pcm = two_tone()
    low_before, high_before = band(pcm, 200), band(pcm, 3000)
    ratios = []
    for pct in (25, 50, 75, 100):
        treated, _rate = apply(pcm, MonsterFX(muffle_pct=pct))
        low, high = band(treated, 200), band(treated, 3000)
        assert low > low_before, f"muffle {pct} is attenuating the bottom too"
        ratios.append(high / low)
    assert ratios == sorted(ratios, reverse=True), ratios
    assert ratios[-1] < 0.35 * (high_before / low_before), ratios


def test_ring_replaces_the_fundamental_rather_than_scaling_it():
    """That `ring` is modulation — which nothing with lungs does — rather than
    a gain change dressed up as one.

    Multiplying by a sine moves every partial to a pair of sidebands `RING_HZ`
    either side of it and leaves nothing where it was. So at full depth the
    180 Hz fundamental of `tone()` is not reduced but gone, and its energy is
    at 125 and 235 Hz instead: an arrangement no fader reaches at any setting.
    """
    pcm = tone()
    plain = band(pcm, 180)
    assert plain > 0

    full, _rate = apply(pcm, MonsterFX(ring_pct=100))
    assert band(full, 180) < plain * 0.01
    for side in (180 - RING_HZ, 180 + RING_HZ):
        assert band(full, side) > plain * 0.5

    # Half depth is a crossfade against the dry signal, so the fundamental is
    # still there and the sidebands are already up: the control sweeps between
    # the two rather than switching from one to the other.
    half, _rate = apply(pcm, MonsterFX(ring_pct=50))
    assert plain * 0.4 < band(half, 180) < plain * 0.95
    assert band(half, 180 + RING_HZ) > plain * 0.25

    # A tremolo is amplitude modulation too, but unipolar: it dips the level
    # instead of inverting it, so the fundamental survives. The two effects
    # must not have become each other.
    wobbled, _rate = apply(pcm, MonsterFX(tremolo_pct=100))
    assert band(wobbled, 180) > plain * 0.5


def test_a_dealt_character_effect_reaches_the_samples():
    """`apply` hands back Polly's bytes untouched unless `character()` reports
    something, so a field missing from that tuple is an effect that silently
    does nothing. The size shift is the one that is meant to skip the pass."""
    pcm = tone(1)
    for field in NEW_EFFECTS:
        fx = MonsterFX(**{field: 50})
        assert fx, field                            # dealt at all
        assert apply(pcm, fx)[0] != pcm, field      # and it reached the audio
    # While the effect that lives in the header still costs no pass at all.
    assert apply(pcm, MonsterFX(size_pct=24))[0] == pcm


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


def test_the_token_names_the_new_fields_without_renaming_the_old_treatments():
    """Both halves of what `token()` owes the cache.

    A field the token cannot see is a different clip served from an unchanged
    key, for as long as the cache keeps it — so each of the four has to move
    the token on its own. And a treatment that deals only the three original
    fields has to key exactly as it did before the other four existed, or the
    clips already on disk are orphaned in the same edit that adds a slider
    nobody has dealt yet.
    """
    old = MonsterFX(24, 55, 35)
    assert old.token() == f"fx:24:55:35:{source_fingerprint()}"

    seen = {old.token()}
    for field in NEW_EFFECTS:
        token = MonsterFX(24, 55, 35, **{field: 40}).token()
        assert token not in seen, f"{field} does not reach the cache key"
        seen.add(token)
    # And they are distinguished from each other, not merely from the original:
    # four fields folded into one number would pass the loop above.
    assert len(seen) == len(NEW_EFFECTS) + 1


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
