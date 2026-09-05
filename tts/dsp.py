"""The monster treatment, done here rather than by Polly.

A speaking monster used to be an ordinary voice put through `<amazon:effect
vocal-tract-length>`, and that tag exists only on the **standard** engine — so
the monsters were dragged back to the engine the rest of the table had already
left. One SSML tag was buying one effect at the price of every monster line
sounding a decade older than the narrator reading over it.

This module is the trade in the other direction: monsters render on whatever
engine the table uses, and the size of the creature is made here, out of the
audio, after Polly has finished with it.

Three things make that cheap enough to do in Python, in the request, with no
new dependency:

  * **A clip is synthesized once and cached forever** (`tts/cache.py`), so this
    runs once per distinct line and never again — not once per playback, and
    not once per spectator.
  * **The size shift costs nothing at all.** Playing a recording at a different
    sample rate scales the whole spectrum — pitch *and* formants together,
    which is what "a bigger creature" means and is what VTL was bought for — so
    it is a number in the WAV header rather than a pass over the samples. The
    browser's own resampler does the arithmetic, better than a line of Python
    would. What it also scales is duration, and that is undone before the audio
    exists: `<prosody rate>` is supported on every engine we use, so the line
    is spoken faster by exactly the factor the playback rate will slow it by
    (`MonsterFX.rate_pct`).
  * **Only the character effects touch samples**, and a monster gets at most
    two of them.

`pcm` is the one Polly output format that is decodable without a codec:
"signed 16-bit, 1 channel (mono), little-endian format", at 8000 or 16000 Hz
and no higher
(https://docs.aws.amazon.com/polly/latest/APIReference/API_SynthesizeSpeech.html,
read 2026-09-04). 16 kHz is the ceiling and what this asks for, against the
24 kHz the table's MP3 gets — a real loss, and the smaller of the two here: a
monster is shifted down the spectrum anyway, and the band it loses is the band
a neural voice has and a standard voice mostly does not.

Pure: no boto3, no network, no filesystem, and it imports nothing else in this
app. `tts/voices.py` deals the treatment, this applies it.
"""

from __future__ import annotations

import hashlib
import math
import struct
import sys
from array import array
from dataclasses import dataclass

__all__ = [
    "SAMPLE_RATE",
    "MonsterFX",
    "apply",
    "wav",
    "source_fingerprint",
    "CAVE_DELAY_S",
    "MAX_SIZE_PCT",
    "DRIVE_REF",
    "RING_HZ",
    "TREMOLO_HZ",
    "MUFFLE_HZ_OPEN",
    "MUFFLE_HZ_SHUT",
    "CRUSH_BITS_OPEN",
    "CRUSH_BITS_MIN",
    "CRUSH_CREST",
]

#: What we ask Polly for, and the most `pcm` will give: "Valid values for pcm
#: are 8000 and 16000" (API_SynthesizeSpeech.html, read 2026-09-04).
SAMPLE_RATE = 16000

#: 16-bit signed, so this is what a sample can be.
PEAK = 32767

#: How far the size shift may be dealt, either way. The ceiling is the rate
#: compensation's: `<prosody rate>` has "a range of 20-200%"
#: (prosody-tag.html), and `rate_pct` writes `100 + size_pct`, so anything
#: under +100 is expressible. The real limit is taste — beyond about a third
#: either way a voice stops being a big or small person and becomes a tape
#: artefact — and this is a guard rail rather than a spread. `voices.py` deals
#: from a much narrower one.
MAX_SIZE_PCT = 60

#: The comb delay that makes a room. Short enough to be heard as the space a
#: creature is standing in rather than as a repeat of what it said, which is
#: the line somewhere around 100 ms.
CAVE_DELAY_S = 0.052

#: How hard the saturation can be driven at `growl_pct = 100`. Past this the
#: consonants stop arriving.
MAX_DRIVE = 5.0

#: Where a clip is placed in the shaper's knee at `growl_pct` just above zero,
#: as a fraction of the shaper's unit scale, measured on the clip's own RMS.
#:
#: The drive used to be measured against `PEAK` — full scale — which assumed a
#: clip arrives mastered to the top of the format. Polly's does not: a line
#: comes back peaking somewhere around a quarter to a half of full scale, and
#: an average sample an order below that, which put the whole clip in the part
#: of the curve that is indistinguishable from a straight line. The slider
#: moved and nothing happened, and the quieter the line the less happened.
#: Against the clip's own level instead, `growl` means the same thing whatever
#: arrives — which is the only way a number dealt by the casting can describe
#: a creature rather than a recording.
DRIVE_REF = 0.25

#: The carrier a ring modulator multiplies the voice by, in Hz of *heard*
#: time (as `CAVE_DELAY_S` is: a creature shifted down is not also detuned).
#:
#: Low, and deliberately near where a voice's own fundamental sits: the sum and
#: difference tones it produces then land inside the voice rather than above it,
#: which is the difference between a creature that speaks wrongly and a radio
#: with a fault. This is the one effect here that nothing about a human throat
#: could produce, which is exactly what it is for.
RING_HZ = 55.0

#: How fast a tremolo wobbles, Hz of heard time. Slow enough to hear as an
#: unsteady voice rather than as roughness — past about 20 Hz amplitude
#: modulation stops being a wobble and becomes another ring modulator.
TREMOLO_HZ = 5.5

#: What `muffle` sweeps between: a one-pole low-pass corner, in Hz, at 0% and
#: at 100%. The open end is above the top of what a 16 kHz clip carries, so
#: `muffle=0` is transparent by construction rather than by a special case.
MUFFLE_HZ_OPEN = 8000.0
MUFFLE_HZ_SHUT = 700.0

#: What `crush` sweeps between, in bits — but bits across the clip's own level
#: rather than across the format. `CRUSH_CREST` below sets the span they divide.
#:
#: The depth used to run the format's 16 bits down to 4, measured against full
#: scale. That reads sensibly and sweeps nothing. A Polly line sits an order
#: below full scale, so the first half of the slider was rounding to steps
#: smaller than the noise the line already carries, and the whole control
#: happened in its last quarter — measured on a speech-like clip at a quarter
#: of full scale, as a fraction of its own RMS: 25% -> 0%, 50% -> 0%,
#: 75% -> 4%, 100% -> 26%.
#:
#: This is the lesson `DRIVE_REF` records one effect up, in the other
#: direction: quantisation error is heard against the signal sitting on top of
#: it, not against a full scale nothing here reaches. Referred to the clip the
#: useful range is narrow and low — 7 bits down to 2, which is a step running
#: from a sixteenth of the clip's own level to twice it. Below that the error
#: goes under the line rather than on it, and above it there is no voice left
#: to break. Each quarter of the slider then roughly doubles the error it
#: adds: 4%, 10%, 22%, 42% by the same measure as the figures above.
CRUSH_BITS_OPEN = 7.0
CRUSH_BITS_MIN = 2.0

#: The peak-to-RMS ratio the quantiser sizes its range against: speech stands
#: some four times its own RMS at the syllables (the same figure `_saturate`
#: drives against), so `2**bits` steps across `CRUSH_CREST * rms` is `bits`
#: bits across the part of the waveform anyone hears. Approximate on purpose —
#: a line whose real crest is five rather than four moves the step by a third
#: of a bit, where the range itself is five bits wide.
CRUSH_CREST = 4.0

#: The comb's feedback at `cave_pct = 100`. Below 1 by construction (the comb
#: is `y[n] = x[n] + fb·y[n-d]`, which decays geometrically and is stable for
#: any fb < 1), and well below it in practice: at 0.6 the peak gain is 2.5x and
#: everything else in the clip is normalized down to make room for it.
MAX_FEEDBACK = 0.6


@dataclass(frozen=True)
class MonsterFX:
    """What is done to a monster's audio after Polly hands it over.

    Every field is a percentage and every one of them is 0 for "not dealt", so
    a default `MonsterFX()` is the null treatment and `bool(fx)` is False.
    That matters at the cache: a seat with no treatment must key — and sound —
    exactly like the plain voice it is.

    `size_pct` is the one that replaces `<amazon:effect vocal-tract-length>`,
    and it is signed the same way: positive is a **longer vocal tract**, i.e. a
    bigger creature, i.e. lower. Unlike VTL it moves pitch along with the
    formants, because it is a resample rather than a synthesizer parameter —
    a cruder effect than Amazon's, applied to much better audio.
    """

    size_pct: int = 0       # + is bigger/deeper; the whole spectrum scales
    growl_pct: int = 0      # soft saturation: harmonics, grit
    cave_pct: int = 0       # a feedback comb: the room it is standing in
    ring_pct: int = 0       # ring modulation: a throat no animal has
    tremolo_pct: int = 0    # amplitude wobble: an unsteady, failing voice
    muffle_pct: int = 0     # one-pole low-pass: behind a door, under water
    crush_pct: int = 0      # bit-depth reduction: a construct, a bad channel

    def __post_init__(self) -> None:
        # Clamped rather than rejected: these are dealt from a hash, and a
        # spread widened past what Polly's rate tag can undo should sound wrong
        # rather than fail a line that a listener is waiting for.
        object.__setattr__(self, "size_pct", _clamp(self.size_pct, -MAX_SIZE_PCT, MAX_SIZE_PCT))
        object.__setattr__(self, "growl_pct", _clamp(self.growl_pct, 0, 100))
        object.__setattr__(self, "cave_pct", _clamp(self.cave_pct, 0, 100))
        for extra in ("ring_pct", "tremolo_pct", "muffle_pct", "crush_pct"):
            object.__setattr__(self, extra, _clamp(getattr(self, extra), 0, 100))

    def character(self) -> tuple[int, ...]:
        """Everything that touches samples, in the order `apply` runs it.

        The size shift is not here: it is the sample rate in the header and
        costs nothing, which is the distinction the whole module is built on.
        """
        return (self.growl_pct, self.ring_pct, self.muffle_pct,
                self.tremolo_pct, self.crush_pct, self.cave_pct)

    def __bool__(self) -> bool:
        return bool(self.size_pct or any(self.character()))

    def token(self) -> str:
        """The part of a cache key this treatment is responsible for.

        Includes `source_fingerprint`, so that editing the arithmetic below
        retires the clips it made. Without it a changed saturation curve would
        go on being served from disk, for a year, from a key that still
        described it correctly.
        """
        # The three originals keep their places so that a clip treated with
        # nothing else keys as it always did; the rest are appended, and only
        # when set, for the same reason.
        base = f"fx:{self.size_pct}:{self.growl_pct}:{self.cave_pct}"
        extra = (self.ring_pct, self.tremolo_pct, self.muffle_pct, self.crush_pct)
        if any(extra):
            base += ":" + ":".join(str(v) for v in extra)
        return f"{base}:{source_fingerprint()}"

    def rate_pct(self) -> int:
        """The speaking rate that undoes what `playback_rate` will do to duration.

        Playing at `1/(1+s)` of the recorded rate multiplies duration by
        `(1+s)`, so the line is spoken at `(1+s)` of normal and arrives the
        length it started. In percent that is exactly `100 + size_pct`.

        A caller that wants the monster to speak slowly asks for that on top
        (`voices.py` multiplies a tempo in): this is only the compensation.
        """
        return 100 + self.size_pct

    def playback_rate(self, in_rate: int = SAMPLE_RATE) -> int:
        """The sample rate to declare, which is the whole size effect."""
        return max(1, round(int(in_rate) / (1.0 + self.size_pct / 100.0)))


def _clamp(value, low, high):
    return max(low, min(high, int(value)))


def source_fingerprint() -> str:
    """A digest of this module's own source.

    Same argument as `voices.source_fingerprint`, which folds this in: the
    audio a given `MonsterFX` produces is decided by the code below, so an edit
    to the code has to move the keys as surely as an edit to the numbers.
    Unreadable source answers "unknown", stably — the behaviour of having no
    version rather than a wrong one.
    """
    global _SOURCE_FP
    if _SOURCE_FP is None:
        try:
            with open(__file__, "rb") as fh:
                _SOURCE_FP = hashlib.sha256(fh.read()).hexdigest()[:8]
        except OSError:  # pragma: no cover - unreadable source
            _SOURCE_FP = "unknown"
    return _SOURCE_FP


_SOURCE_FP: str | None = None


def apply(pcm: bytes, fx: MonsterFX, in_rate: int = SAMPLE_RATE) -> tuple[bytes, int]:
    """Treat one clip. Returns the samples and the rate to play them at.

    `pcm` is Polly's `pcm` output as it arrives — signed 16-bit mono
    little-endian. The returned bytes are the same format; the returned rate is
    not `in_rate` unless the size shift was zero.
    """
    rate = fx.playback_rate(in_rate)
    samples = _samples(pcm)
    if not samples or not any(fx.character()):
        # The size shift is the header, so a monster with no character effects
        # is answered without touching a sample. Most of them are.
        return _bytes(samples), rate
    work = [float(v) for v in samples]
    # Held across the treatment: saturation is a compressor, a comb adds energy
    # and a low-pass takes it away, so all of them change the level as a side
    # effect of changing the shape. A monster ten decibels over the narrator
    # reading the line before it is a mixing fault, not a costume — so the
    # level that comes out is the level that went in, and only the timbre has
    # moved. It is measured on what Polly sent, so a seat that asked to be
    # quieter (`<prosody volume>`) still is.
    level = _rms(work)
    # The order is a signal chain, not a list: what the throat does, then what
    # the body does to it, then the room it all happens in. Each is one pass
    # over the samples, in place — nothing here copies the clip, which is what
    # keeps the whole treatment affordable inside a request.
    if fx.growl_pct:
        _saturate(work, fx.growl_pct)
    if fx.ring_pct:
        _ring(work, fx.ring_pct, rate)
    if fx.muffle_pct:
        _muffle(work, fx.muffle_pct, rate)
    if fx.tremolo_pct:
        _tremolo(work, fx.tremolo_pct, rate)
    if fx.crush_pct:
        _crush(work, fx.crush_pct)
    if fx.cave_pct:
        _cave(work, fx.cave_pct, rate)
    return _bytes(_to_pcm(work, level)), rate


def _samples(pcm: bytes) -> array:
    """Polly's little-endian frames as host-order signed shorts.

    A trailing odd byte is dropped rather than raising: `array.frombytes`
    refuses a partial frame, and a truncated stream should cost the listener
    1/32000 of a second rather than the line.
    """
    data = bytes(pcm or b"")
    out = array("h")
    out.frombytes(data[: len(data) - (len(data) % 2)])
    if sys.byteorder == "big":  # pragma: no cover - little-endian everywhere we run
        out.byteswap()
    return out


def _bytes(samples: array) -> bytes:
    if sys.byteorder == "big":  # pragma: no cover - see above
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def _rms(work: list[float]) -> float:
    """Root mean square: loudness, near enough, and cheap."""
    if not work:
        return 0.0
    total = 0.0
    for v in work:
        total += v * v
    return (total / len(work)) ** 0.5


def _saturate(work: list[float], pct: int) -> None:
    """Soft-clip, in place: harmonics on top of the voice, not instead of it.

    `x(27+x²)/(27+9x²)` is the standard rational stand-in for `tanh` — three
    multiplies a sample, where `math.tanh` is a call — and it is used the same
    way: drive into it, then bring the level back, which `_to_pcm` does.
    Compressing a signal upward is most of what a saturator does and none of
    what this one is for.

    **The drive is measured against the clip's own level, not against full
    scale** (`DRIVE_REF`). A shaper only shapes near and past its unit; a Polly
    line arrives quiet enough that measuring against `PEAK` left every sample
    on the straight part of the curve, and the effect was inaudible in
    proportion to how quietly the voice happened to speak.
    """
    ref = _rms(work)
    if ref <= 0.0:
        return
    drive = 1.0 + (pct / 100.0) * (MAX_DRIVE - 1.0)
    # An average sample lands at `DRIVE_REF` of the shaper's unit at the bottom
    # of the range and at `MAX_DRIVE` times that at the top; the peaks, some
    # four times the RMS in speech, are what actually round over.
    gain = drive * DRIVE_REF / ref
    for i, v in enumerate(work):
        work[i] = _shape(v * gain) * PEAK


def _shape(x: float) -> float:
    xx = x * x
    return x * (27.0 + xx) / (27.0 + 9.0 * xx)


def _cave(work: list[float], pct: int, rate: int) -> None:
    """A feedback comb, in place: the same line arriving off stone.

    The delay is in seconds of *heard* time, so it is counted in samples of the
    playback rate rather than the recorded one — a monster shifted down is
    standing in the same cave as one shifted up, not a cave a third larger.
    """
    delay = max(1, int(rate * CAVE_DELAY_S))
    feedback = (pct / 100.0) * MAX_FEEDBACK
    for i in range(delay, len(work)):
        work[i] += feedback * work[i - delay]


def _ring(work: list[float], pct: int, rate: int) -> None:
    """Ring modulation, in place: the voice times a low sine.

    A multiply, not a mix — but crossfaded against the dry signal by `pct`, so
    the control sweeps from the voice to the fully modulated version of it
    rather than switching between two things. At full depth the fundamental
    disappears and only the sum and difference tones are left, which is the
    sound nothing with lungs makes.
    """
    depth = pct / 100.0
    step = 2.0 * math.pi * RING_HZ / rate
    for i, v in enumerate(work):
        work[i] = v * (1.0 - depth + depth * math.sin(step * i))


def _tremolo(work: list[float], pct: int, rate: int) -> None:
    """Amplitude wobble, in place: a voice that cannot hold itself steady.

    The modulator is unipolar — it dips the level rather than inverting it —
    so at full depth the voice is cut to silence at the bottom of each cycle
    instead of coming back phase-flipped, which is a ring modulator again.
    """
    depth = pct / 100.0
    step = 2.0 * math.pi * TREMOLO_HZ / rate
    for i, v in enumerate(work):
        work[i] = v * (1.0 - depth * 0.5 * (1.0 - math.cos(step * i)))


def _muffle(work: list[float], pct: int, rate: int) -> None:
    """One-pole low-pass, in place: heard through a door, a helm, or water.

    The corner sweeps geometrically rather than linearly, because pitch is
    geometric: half the slider should be half the distance in octaves, or the
    whole control happens in its last quarter.
    """
    frac = pct / 100.0
    corner = MUFFLE_HZ_OPEN * (MUFFLE_HZ_SHUT / MUFFLE_HZ_OPEN) ** frac
    alpha = 1.0 - math.exp(-2.0 * math.pi * corner / rate)
    prev = 0.0
    for i, v in enumerate(work):
        prev += alpha * (v - prev)
        work[i] = prev


def _crush(work: list[float], pct: int) -> None:
    """Bit-depth reduction, in place: a voice arriving down a bad channel.

    Quantisation error is the effect, so this is one rounding per sample, and
    the only decision is how large a step to round to.

    **The step is measured against the clip's own level, not against full
    scale** (`CRUSH_BITS_OPEN`, `CRUSH_CREST`), for the reason `DRIVE_REF`
    records above: the error is heard against the signal that would otherwise
    mask it. Sixteen bits of a format a line only uses a fifth of is not
    sixteen bits of that line, and quantising below what the line's own noise
    floor already hides is a slider position that costs a pass and changes
    nothing.

    Linear in bits is geometric in step, which is the shape a slider wants —
    each quarter of the range doubles the error again, as `_muffle` sweeps its
    corner in octaves rather than in Hz. It was the reference, not the shape,
    that made the old mapping dead below three quarters.
    """
    ref = _rms(work)
    if ref <= 0.0:
        return
    bits = CRUSH_BITS_OPEN - (CRUSH_BITS_OPEN - CRUSH_BITS_MIN) * (pct / 100.0)
    # `2**bits` steps across the whole span, which is symmetric about zero, so
    # the step is half of it per bit.
    step = ref * CRUSH_CREST / (2.0 ** (bits - 1.0))
    for i, v in enumerate(work):
        work[i] = round(v / step) * step


def _to_pcm(work: list[float], level: float = 0.0) -> array:
    """Back to 16-bit at the level it started, and never over the format.

    Two corrections, in this order and both of them one multiply:

      * back to `level` — the RMS the clip had before the treatment — so a
        monster is a different voice rather than a louder one;
      * then down again if that leaves anything past full scale. Normalized
        rather than clipped: a comb has a peak gain of `1/(1-fb)`, so a loud
        syllable landing on a reflection of itself genuinely can overshoot, and
        hard-clipping it is audible where a decibel of headroom is not.
    """
    scale = 1.0
    if level > 0.0:
        now = _rms(work)
        if now > 0.0:
            scale = level / now
    top = 0.0
    for v in work:
        v = v if v >= 0 else -v
        if v > top:
            top = v
    top *= scale
    if top > PEAK:
        scale *= PEAK / top
    if scale == 1.0:
        return array("h", [int(v) for v in work])
    return array("h", [int(v * scale) for v in work])


def wav(pcm: bytes, rate: int) -> bytes:
    """A RIFF/WAVE wrapper: 16-bit, mono, `rate` Hz.

    The alternative to a wrapper is an MP3, and an MP3 needs an encoder — a
    system dependency (`ffmpeg`, `lame`) on the one path that must not acquire
    one, since a monster falling back is a 502 the page hides by speaking the
    line itself. A WAV of a 14-second clip is a few hundred KB against an
    MP3's tens, which the on-disk cache and a local nginx both survive; the
    browser plays either from the same `<audio>` element and the same blob.
    """
    rate = max(1, int(rate))
    body = bytes(pcm or b"")
    fmt = struct.pack(
        "<4sIHHIIHH",
        b"fmt ", 16,        # PCM header, 16 bytes of it
        1,                  # format 1: uncompressed PCM
        1,                  # mono, which is what Polly's pcm is
        rate,
        rate * 2,           # bytes per second: rate x 1 channel x 2 bytes
        2,                  # block align
        16,                 # bits per sample
    )
    data = b"data" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(fmt) + len(data)) + b"WAVE" + fmt + data
