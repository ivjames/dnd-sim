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
import struct
import sys
from array import array
from dataclasses import dataclass

__all__ = [
    "SAMPLE_RATE",
    "FIELDS",
    "BOUNDS",
    "EFFECT_CHAIN",
    "MonsterFX",
    "apply",
    "wav",
    "source_fingerprint",
    "CAVE_DELAY_S",
    "MAX_SIZE_PCT",
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

#: The comb's feedback at `cave_pct = 100`. Below 1 by construction (the comb
#: is `y[n] = x[n] + fb·y[n-d]`, which decays geometrically and is stable for
#: any fb < 1), and well below it in practice: at 0.6 the peak gain is 2.5x and
#: everything else in the clip is normalized down to make room for it.
MAX_FEEDBACK = 0.6


#: Every knob, with the bounds it is clamped to. One table, because seventeen
#: of anything hand-written seventeen times is seventeen chances to write the
#: sixteenth one wrong — `__post_init__` clamps from here, `token()` spells
#: from here, and `tts/voices.py` builds its `Tune` and the voice lab its
#: sliders from here rather than from a second list that would drift.
#:
#: This is DECLARATION order, which is the dataclass's positional order, and
#: the original three keep the front of it: `MonsterFX(20, 55, 40)` has meant
#: size, growl, room since there were only three, and a knob inserted ahead of
#: those would silently repoint every positional construction in the tree at a
#: different effect. The order the chain applies them in is a different
#: question and `EFFECT_CHAIN` answers it.
FIELDS: tuple[tuple[str, int, int], ...] = (
    ("size_pct", -MAX_SIZE_PCT, MAX_SIZE_PCT),
    ("growl_pct", 0, 100),
    ("cave_pct", 0, 100),
    ("suboctave_pct", 0, 100),
    ("noise_pct", 0, 100),
    ("bandgrowl_pct", 0, 100),
    ("fold_pct", 0, 100),
    ("rectify_pct", 0, 100),
    ("highpass_pct", 0, 100),
    ("formant_pct", -100, 100),
    ("phone_pct", 0, 100),
    ("vibrato_pct", 0, 100),
    ("phaser_pct", 0, 100),
    ("metal_pct", 0, 100),
    ("slap_pct", 0, 100),
    ("tremolo_pct", 0, 100),
    ("stutter_pct", 0, 100),
)

#: The bounds alone, by name. What `voices.py` and the voice lab want.
BOUNDS: dict[str, tuple[int, int]] = {n: (lo, hi) for n, lo, hi in FIELDS}

#: The sample-touching part of `FIELDS`, in the order a signal chain wants
#: them, paired with the function that applies each. `size_pct` is absent by
#: construction: it is the sample rate in the WAV header and never a pass over
#: the audio.
#:
#: What generates content comes first (an octave under the voice, breath
#: through it), then the nonlinearities while the signal is still clean enough
#: for them to bite, then the filters that shape what those made, then
#: modulation, then the room, and last the two that chop the level — a gate
#: ahead of a reverb tail cuts the tail, which is a fault rather than an effect.
#:
#: Every one of these takes `(work, pct, rate)` and mutates `work` in place,
#: where `work` is a list of floats at 16-bit scale and `rate` is the PLAYBACK
#: rate — heard time, not recorded time, so that a creature shifted down stands
#: in the same room and rasps at the same speed as one shifted up. `_cave` has
#: always counted its delay that way and the rest follow it.
EFFECT_CHAIN: tuple[tuple[str, str], ...] = (
    ("suboctave_pct", "_suboctave"),
    ("noise_pct", "_noise"),
    ("bandgrowl_pct", "_bandgrowl"),
    ("growl_pct", "_saturate"),
    ("fold_pct", "_fold"),
    ("rectify_pct", "_rectify"),
    ("highpass_pct", "_highpass"),
    ("formant_pct", "_formant"),
    ("phone_pct", "_phone"),
    ("vibrato_pct", "_vibrato"),
    ("phaser_pct", "_phaser"),
    ("metal_pct", "_metal"),
    ("slap_pct", "_slap"),
    ("cave_pct", "_cave"),
    ("tremolo_pct", "_tremolo"),
    ("stutter_pct", "_stutter"),
)


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

    `formant_pct` is the one knob here that does move an emphasis without
    moving pitch, and it is signed to agree with `size_pct`: positive is a
    bigger creature. It is a single peaking filter rather than a real vocal
    tract, so it is a cue and not a transform — but it is the only cue in this
    module that a listener hears as head size rather than as tape speed.

    The other fourteen are character rather than size, all of them 0 by
    default, none of them dealt by `voices.py` as this stands: they are
    reachable through a `Tune` — the voice lab — so that what a creature type
    should sound like is decided by ear before it is written into the casting.
    """

    # The original three, first and in their original order: this dataclass is
    # constructed positionally in places, and these three have meant these
    # three since it had no others. See `FIELDS`.
    size_pct: int = 0        # + is bigger/deeper; the whole spectrum scales
    growl_pct: int = 0       # soft saturation: harmonics, grit
    cave_pct: int = 0        # a feedback comb: the room it is standing in
    # Character, all of them 0 unless a Tune says otherwise.
    suboctave_pct: int = 0   # an octave under the voice, mixed beneath it
    noise_pct: int = 0       # breath, shaped by the voice's own envelope
    bandgrowl_pct: int = 0   # saturation on the low band alone
    fold_pct: int = 0        # wavefolding: harsher, and not a voice any more
    rectify_pct: int = 0     # blended rectification: an octave up, nastily
    highpass_pct: int = 0    # thins it: incorporeal, distant
    formant_pct: int = 0     # + is a bigger head; pitch untouched
    phone_pct: int = 0       # 300-3400 Hz: a helm, a jar, a speaking stone
    vibrato_pct: int = 0     # pitch wobble off a modulated delay
    phaser_pct: int = 0      # cascaded all-passes: swirl
    metal_pct: int = 0       # feedforward comb: notches, no tail
    slap_pct: int = 0        # one early reflection
    tremolo_pct: int = 0     # sub-audio amplitude modulation
    stutter_pct: int = 0     # a gate on a grid: chittering

    def __post_init__(self) -> None:
        # Clamped rather than rejected: these are dealt from a hash, and a
        # spread widened past what Polly's rate tag can undo should sound wrong
        # rather than fail a line that a listener is waiting for.
        for name, lo, hi in FIELDS:
            object.__setattr__(self, name, _clamp(getattr(self, name), lo, hi))

    def __bool__(self) -> bool:
        return any(getattr(self, name) for name, _, _ in FIELDS)

    def touches_samples(self) -> bool:
        """Whether anything here is a pass over the audio.

        False for a treatment that is only a size shift, which is most of them
        and which `apply` answers without reading a sample.
        """
        return any(getattr(self, name) for name, _ in EFFECT_CHAIN)

    def token(self) -> str:
        """The part of a cache key this treatment is responsible for.

        Spells only what is dealt — `fx:size=20,growl=55:<fp>` — so that a
        treatment keys the same length it always did however many knobs exist,
        and so that adding a knob nobody has turned on does not rewrite the key
        of every clip on disk. (The fingerprint does that anyway when the code
        moves; this is about the ones where it has not.)

        Includes `source_fingerprint`, so that editing the arithmetic below
        retires the clips it made. Without it a changed saturation curve would
        go on being served from disk, for a year, from a key that still
        described it correctly.
        """
        dealt = ",".join(
            f"{name[:-4]}={getattr(self, name)}"
            for name, _, _ in FIELDS
            if getattr(self, name)
        )
        return f"fx:{dealt}:{source_fingerprint()}"

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
    if not samples or not fx.touches_samples():
        # The size shift is the header, so a monster with no character effects
        # is answered without touching a sample. Most of them are.
        return _bytes(samples), rate
    work = [float(v) for v in samples]
    # Held across the treatment: saturation is a compressor, a comb adds
    # energy, a gate takes it away, and a bandpass throws most of the spectrum
    # out. Every one of those moves the LEVEL as a side effect of changing the
    # shape. A monster ten decibels over — or under — the narrator reading the
    # line before it is a mixing fault, not a costume, so the level that comes
    # out is the level that went in and only the timbre has moved.
    #
    # It is measured once, here, rather than per effect: sixteen successive
    # renormalizations would each undo part of what the next one was reacting
    # to, and a gate would end up pumping the surviving syllables up by
    # whatever fraction of the clip it had just silenced.
    level = _rms(work)
    for name, func in EFFECT_CHAIN:
        pct = getattr(fx, name)
        if pct:
            globals()[func](work, pct, rate)
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


def _saturate(work: list[float], pct: int, rate: int) -> None:
    """Soft-clip, in place: harmonics on top of the voice, not instead of it.

    `x(27+x²)/(27+9x²)` is the standard rational stand-in for `tanh` — three
    multiplies a sample, where `math.tanh` is a call — and it is used the same
    way: drive into it, then divide by what it does to full scale, so nothing
    here overshoots the format. What it does to the LEVEL is undone afterwards,
    in `_to_pcm`, because compressing a signal upward is most of what a
    saturator does and none of what this one is for.
    """
    drive = 1.0 + (pct / 100.0) * (MAX_DRIVE - 1.0)
    norm = _shape(drive) or 1.0
    scale = PEAK / norm
    for i, v in enumerate(work):
        work[i] = _shape(drive * v / PEAK) * scale


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


# --------------------------------------------------------------------------
# The character effects. Every one of them takes `(work, pct, rate)`, mutates
# `work` in place, and is called only when `pct` is non-zero — so none of them
# needs a "not dealt" branch, and every one of them must be a no-op in the
# limit as `pct` approaches 0 anyway, or the voice lab's sliders jump.
#
# `rate` is the PLAYBACK rate. Anything counted in seconds is counted in
# seconds the listener will hear.
# --------------------------------------------------------------------------


def _suboctave(work: list[float], pct: int, rate: int) -> None:
    """An octave under the voice, mixed beneath it: the classic fiend.

    Overlap-add: grains of `SUBOCTAVE_GRAIN_S` are read at half speed against
    a full-speed write pointer and cross-faded with a Hann window, which
    halves the frequency of everything without halving the duration. Crude
    next to a phase vocoder and much cheaper — and mixed *under* the dry voice
    at `pct`, never replacing it, so the intelligibility of the line is the dry
    signal's and only the weight underneath it is this.
    """
    raise NotImplementedError


def _noise(work: list[float], pct: int, rate: int) -> None:
    """Breath: white noise shaped by the voice's own envelope.

    A one-pole envelope follower over |x|, times a deterministic LCG's output,
    mixed in at `pct`. Shaped rather than flat because a constant hiss is a bad
    line and a rasp that arrives only where the creature is speaking is a
    damaged throat. The generator is seeded from a constant, not from the
    clock or the id: a clip is cached, and a cached clip that was different the
    second time it was rendered would be a bug nobody could see.
    """
    raise NotImplementedError


def _bandgrowl(work: list[float], pct: int, rate: int) -> None:
    """Saturation on the low band alone, so the consonants survive it.

    `MAX_DRIVE` is where it is because past it "the consonants stop arriving" —
    which is a statement about the high band, since that is where they live. So
    split at `BANDGROWL_SPLIT_HZ` with a one-pole, saturate the low half, add
    the untouched high half back. The same curve as `_saturate`, driven much
    harder, at an intelligibility cost of nearly nothing.
    """
    raise NotImplementedError


def _fold(work: list[float], pct: int, rate: int) -> None:
    """Wavefolding: what saturation does, but no longer a voice.

    Where `_saturate` compresses a signal toward a ceiling, this reflects it
    back off one — so a loud syllable comes out with more harmonics than it
    went in with rather than fewer, and the result reads as a thing wearing a
    voice rather than a person with a rough one. Triangular folding, `pct`
    setting how hard the signal is driven into the fold.
    """
    raise NotImplementedError


def _rectify(work: list[float], pct: int, rate: int) -> None:
    """Blended rectification: an octave up, and nasty with it.

    `|x|` at full blend is the octave-doubling trick every fuzz pedal knows;
    blended against the dry signal at `pct` it is an edge rather than a
    transformation. Cheap — one comparison a sample — and the only effect here
    that adds energy specifically at the top of the band.
    """
    raise NotImplementedError


def _highpass(work: list[float], pct: int, rate: int) -> None:
    """One-pole highpass: thins the voice out.

    Corner slides from `HIGHPASS_MIN_HZ` to `HIGHPASS_MAX_HZ` across the range,
    so 0 is genuinely nothing and 100 is a voice with no chest left in it —
    incorporeal, or a long way off. The complement of the muffle that would
    take the metallic edge off `_cave`, and deliberately gentle: one pole, 6 dB
    an octave, because a steep highpass on a 16 kHz clip sounds like a fault.
    """
    raise NotImplementedError


def _formant(work: list[float], pct: int, rate: int) -> None:
    """A peaking bell where the vocal tract would put one: head size, no pitch.

    The one knob here that pushes on the axis `<amazon:effect
    vocal-tract-length>` owned — an emphasis moved without the pitch moving
    with it — and it is a cue rather than a transform: one RBJ peaking biquad,
    `FORMANT_GAIN_DB` at full deflection, centred at `FORMANT_BIG_HZ` for
    positive and `FORMANT_SMALL_HZ` for negative. Signed to agree with
    `size_pct`: positive is a bigger creature, which is energy moved down.

    Gain scales with `|pct|` rather than the centre sliding, so 0 is no filter
    at all rather than a bell parked in the middle doing nothing audible.
    """
    raise NotImplementedError


def _phone(work: list[float], pct: int, rate: int) -> None:
    """300-3400 Hz, blended: a helm, a jar, a voice out of a speaking stone.

    The telephone band, which is a band because that is what a narrow channel
    does to a voice and every listener has heard it. A one-pole pair rather
    than a steep filter, and blended against the dry signal at `pct` so the
    slider is a distance rather than a switch.
    """
    raise NotImplementedError


def _vibrato(work: list[float], pct: int, rate: int) -> None:
    """Pitch wobble, off a delay line whose read pointer is modulated.

    A sine LFO at `VIBRATO_HZ` moving a fractional read pointer over a short
    delay, linearly interpolated — a delay whose length is changing IS a pitch
    shift, which is the whole trick. `pct` is the depth. Unstable things:
    oozes, something not entirely holding its shape.
    """
    raise NotImplementedError


def _phaser(work: list[float], pct: int, rate: int) -> None:
    """Cascaded all-passes with a swept coefficient: swirl.

    `PHASER_STAGES` first-order all-pass sections whose coefficient is swept by
    an LFO at `PHASER_HZ`, summed with the dry signal so the moving phase
    becomes moving notches. Costs a pass per stage and sounds like nothing else
    in this module — incorporeal, planar, wrong in a way that is not grit.
    """
    raise NotImplementedError


def _metal(work: list[float], pct: int, rate: int) -> None:
    """A feedforward comb: fixed notches, and no tail at all.

    `y[n] = x[n] + g*x[n-d]` — the same delay `_cave` uses with the feedback
    taken out, at `METAL_DELAY_S`, which is short enough that what is heard is
    a timbre rather than a repeat. Where `_cave` is the room a creature stands
    in, this is the creature being made of something that rings. They stack.
    """
    raise NotImplementedError


def _slap(work: list[float], pct: int, rate: int) -> None:
    """One early reflection at `SLAP_DELAY_S`, and only one.

    A single non-feedback tap far enough out to be heard as a surface rather
    than as timbre, and near enough not to be heard as an echo. It is what
    tells a listener the creature is standing in front of something, which
    `_cave` says much more expensively.
    """
    raise NotImplementedError


def _tremolo(work: list[float], pct: int, rate: int) -> None:
    """Sub-audio amplitude modulation: a voice that pulses.

    A sine at `TREMOLO_HZ` scaling the amplitude by `pct` — slow enough to be
    heard as breathing or as something not quite sustaining itself, where the
    same modulation at audio rate would be ring modulation and a different
    effect entirely. Undead, oozes, a thing speaking on borrowed air.
    """
    raise NotImplementedError


def _stutter(work: list[float], pct: int, rate: int) -> None:
    """A gate on a fixed grid: chittering.

    The clip is divided into `STUTTER_HZ` windows and the amplitude is pulled
    down over part of each, with `pct` setting both how much of the window is
    gated and how far down it goes. Edges are ramped over `STUTTER_RAMP_S`
    rather than switched, because a hard gate on a 16 kHz clip clicks, and a
    click is the one artefact a listener reads as a broken file rather than a
    monster. Swarms, insect things, a voice made of more than one throat.

    Last in the chain: a gate ahead of `_cave` would cut the tail it just made.
    """
    raise NotImplementedError


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
