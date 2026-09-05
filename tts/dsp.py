"""The monster treatment, done here rather than by Polly.

A speaking monster used to be an ordinary voice put through `<amazon:effect
vocal-tract-length>`, and that tag exists only on the **standard** engine — so
the monsters were dragged back to the engine the rest of the table had already
left. One SSML tag was buying one effect at the price of every monster line
sounding a decade older than the narrator reading over it.

This module is the trade in the other direction: monsters render on whatever
engine the table uses, and the size of the creature is made here, out of the
audio, after Polly has finished with it.

What that costs is worth being exact about, because the module has grown:
there are seventeen knobs here (`FIELDS`), sixteen of which are a pass over
the samples (`EFFECT_CHAIN`). The **casting deals three of them** — size,
growl, room — and has since there were only three; the other fourteen are
character rather than size and are reachable only through a `Tune`, which is
the voice lab, so that what a creature type should sound like is settled by
ear before it is written into `voices.py`. A monster taken off the casting
today therefore pays for at most two passes over its audio, and the thirteen
sliders it has never been dealt cost it nothing.

Three things keep even the top of that affordable in Python, in the request,
with no new dependency:

  * **A clip is synthesized once and cached forever** (`tts/cache.py`), so this
    runs once per distinct line and never again — not once per playback, and
    not once per spectator. Everything below is amortized over every spectator
    who ever hears that line, which is the fact that makes a per-sample Python
    loop a reasonable thing to write at all.
  * **The size shift costs nothing at all.** Playing a recording at a different
    sample rate scales the whole spectrum — pitch *and* formants together,
    which is what "a bigger creature" means and is what VTL was bought for — so
    it is a number in the WAV header rather than a pass over the samples. The
    browser's own resampler does the arithmetic, better than a line of Python
    would. What it also scales is duration, and that is undone before the audio
    exists: `<prosody rate>` is supported on every engine we use, so the line
    is spoken faster by exactly the factor the playback rate will slow it by
    (`MonsterFX.rate_pct`). A treatment that is only a size shift is answered
    by `apply` without reading a sample, and most of them are.
  * **A monster pays only for what it is dealt.** Every effect is skipped
    unless its knob is non-zero, and each is one pass: measured on a 14-second
    clip (224,000 samples), the cheap ones — the combs, the gate — are under
    10 ms and the dear ones — the granular sub-octave, the four-stage phaser —
    are 60-70 ms. All sixteen at once, which nothing on the casting does and
    only the voice lab can even ask for, is about half a second. The two knobs
    a monster actually gets are a few tens of milliseconds, once, ever.

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

# -- the character effects' constants ---------------------------------------
# Every one of these is a number picked for a 16 kHz clip of a human voice,
# and the doc-comment is where the picking is recorded: the value on its own
# is unarguable-with, and someone who wants a different monster needs to know
# what the current one was aiming at before they move it.

#: The grain the sub-octave is built from. It has to hold several cycles of
#: the thing it is halving — a 180 Hz fundamental is 5.6 ms, so 50 ms is nine
#: of them — or the half-speed read has a fragment rather than a waveform. The
#: ceiling is the other way: a grain much longer than a phoneme cross-fades
#: one sound over the next and the line stops being words. 50 ms is the usual
#: granular compromise and this is no cleverer than usual.
SUBOCTAVE_GRAIN_S = 0.050

#: How much sub-octave is mixed under a dry voice held at 1.0, at
#: `suboctave_pct = 100`. Under, never level with: the intelligibility of the
#: line is the dry signal's, and an octave-down grain train loud enough to
#: compete with it stops being weight underneath a voice and becomes a second,
#: worse voice.
SUBOCTAVE_MAX_MIX = 0.75

#: `_noise`'s generator, in full, at module level so that it is obvious there
#: is no clock and no `random` in it. Numerical Recipes' LCG constants
#: (`a = 1664525`, `c = 1013904223`, modulus 2**32): unremarkable arithmetic
#: whose only virtues here are that it is four lines and that it will produce
#: the same stream on every Python that ever runs this. The seed is a constant
#: — any constant; this one is the golden-ratio word everyone's hash uses —
#: because a clip is written to disk once and served for a year, and a breath
#: that was different the second time it rendered would be a bug with no
#: observer.
NOISE_SEED = 0x9E3779B9
NOISE_LCG_A = 1664525
NOISE_LCG_C = 1013904223
NOISE_LCG_M = 2 ** 32

#: The breath envelope's time constant. Fast enough to open on a syllable
#: (12 ms is well inside the ~40 ms a consonant occupies) and slow enough not
#: to follow the waveform itself, which at 180 Hz would turn the follower into
#: a rectifier and the breath into a buzz.
NOISE_ENV_S = 0.012

#: Breath depth at `noise_pct = 100`, against a voice at 1.0. Short of parity
#: on purpose: at 1.0 the hiss is as loud as the vowel that shaped it, which
#: reads as a broken recording rather than a damaged throat.
NOISE_MAX_MIX = 0.6

#: Where `_bandgrowl` splits. Below this is the vowel body — pitch, chest,
#: everything a growl is made of; above it are the fricatives and stop bursts
#: that carry which word was said. 800 Hz is under the second formant of every
#: vowel and over almost no consonant energy, which is exactly the trade the
#: split is for.
BANDGROWL_SPLIT_HZ = 800.0

#: How hard the low band is driven at `bandgrowl_pct = 100` — far past
#: `MAX_DRIVE`, which is a limit on saturating the WHOLE signal and is set by
#: what the consonants can survive. The consonants are not in this band, so
#: they survive anything done to it.
BANDGROWL_DRIVE = 12.0

#: How far into the fold the signal is driven at `fold_pct = 100`. Above 1 the
#: peaks reflect off the ceiling instead of leaning on it; at 3.2 a loud vowel
#: folds twice and a quiet one not at all, so the effect arrives with the
#: syllable rather than sitting under the whole line.
FOLD_DRIVE = 3.2

#: The corner of the DC blocker on `_rectify`'s wet path. `|x|` is
#: one-sided, so rectification hands back a large DC offset along with the
#: octave — inaudible, and it would eat the headroom `_to_pcm` then normalizes
#: away, quietly costing the clip level. 20 Hz is under everything a voice
#: does and above zero, which is the whole specification for a DC blocker.
RECTIFY_DC_HZ = 20.0

#: The one-pole highpass corner at either end of the slider, swept
#: geometrically (frequency is heard in ratios, and a linear sweep would spend
#: half the knob between 700 Hz and 1400 Hz). 25 Hz is below anything in a
#: voice, so the bottom of the range is a DC blocker and audibly nothing;
#: 1400 Hz has taken every vowel's first formant out and leaves a voice that
#: is all edge and no chest.
HIGHPASS_MIN_HZ = 25.0
HIGHPASS_MAX_HZ = 1400.0

#: `_formant`'s bell at full deflection. 9 dB is about the most a single
#: peaking filter can add before it stops reading as a resonance of the
#: speaker and starts reading as an EQ someone left on.
FORMANT_GAIN_DB = 9.0

#: Where the bell sits for a bigger creature and for a smaller one. 320 Hz is
#: the first-formant region — the part of the spectrum a long vocal tract
#: pushes energy into — and 2200 Hz is where a short one puts it, near the
#: third formant, which is why a small speaker sounds bright rather than
#: merely high. Neither centre moves with the knob; only the gain does.
FORMANT_BIG_HZ = 320.0
FORMANT_SMALL_HZ = 2200.0

#: The bell's Q. 1.0 is a little under an octave wide: broad enough to be
#: heard as a vocal tract rather than as a whistle at one frequency, narrow
#: enough that it is still a formant and not a tilt.
FORMANT_Q = 1.0

#: The telephone band, and it is these two numbers rather than any other two
#: because every listener alive has heard exactly this band and knows what it
#: means. G.712's passband, borrowed whole.
PHONE_LOW_HZ = 300.0
PHONE_HIGH_HZ = 3400.0

#: The vibrato rate. 5.5 Hz is where a singer's vibrato sits, which is the
#: point: slower reads as a tape fault and faster as a tremolo on the wrong
#: parameter. It is counted in heard time, so a creature shifted down wavers
#: at the same rate as one shifted up.
VIBRATO_HZ = 5.5

#: The most the read pointer is moved at `vibrato_pct = 100`. A delay that
#: sweeps 3 ms at 5.5 Hz is a peak pitch deviation of about 5% — a semitone,
#: near enough — which is a voice not holding its shape rather than a voice
#: singing.
VIBRATO_DEPTH_S = 0.003

#: How many all-pass sections the phaser cascades. Each is a whole pass over
#: the clip, so this is a cost as much as a sound: four sections give two
#: notches, which is the classic phaser everyone recognises, and the cost of
#: the six that would give three is not worth the difference.
PHASER_STAGES = 4

#: The phaser's sweep rate and the band it sweeps. Slow — 0.45 Hz is one
#: sweep every couple of seconds — because the effect is the movement, and a
#: sentence is only a few seconds long: sweep it faster and a listener hears a
#: warble instead of a slow wrongness. The band is the middle of the voice,
#: where the notches land on something worth notching.
PHASER_HZ = 0.45
PHASER_MIN_HZ = 300.0
PHASER_MAX_HZ = 2400.0

#: How finely the swept coefficient is tabulated over one LFO cycle. The
#: alternative is a `tan` per sample per stage — a million of them on a long
#: clip — and 512 steps over a 2.2-second sweep moves the corner by well under
#: a cent between steps, which is nothing anyone can hear.
PHASER_TABLE = 512

#: The feedforward comb's delay. 1.2 ms puts its first notch at about 420 Hz
#: and the rest every 840 Hz above it — close enough together that the ear
#: reads the comb as the timbre of the thing speaking rather than as a repeat
#: of what it said. Anything past about 10 ms and this becomes `_slap`.
METAL_DELAY_S = 0.0012

#: The comb's gain at `metal_pct = 100`. Short of 1.0 so the notches are deep
#: and not infinite: at exactly 1.0 the nulls are total and the vowel
#: frequencies that land in one disappear outright.
METAL_MAX_GAIN = 0.85

#: The single early reflection. Under about 30 ms a reflection fuses with the
#: sound that made it and is heard as a surface rather than as an echo, and
#: 24 ms is inside that with room to spare. Deliberately not near
#: `CAVE_DELAY_S`: the two are meant to stack into a room with a wall in it
#: rather than to double one comb.
SLAP_DELAY_S = 0.024

#: The reflection's level at `slap_pct = 100`. A wall that returned as much as
#: it was given would be a wall made of nothing, and it is one tap with no
#: feedback, so this can be generous without ringing.
SLAP_MAX_GAIN = 0.65

#: The tremolo rate. 4.5 Hz is slow enough to be heard as pulsing — breath, or
#: something not quite sustaining itself — and an order of magnitude below the
#: ~20 Hz where amplitude modulation stops being a tremolo and becomes
#: sidebands, which would be a ring modulator and a different effect.
TREMOLO_HZ = 4.5

#: How deep the pulse goes at `tremolo_pct = 100`. Not 1.0: full depth takes
#: the voice to silence at the bottom of every cycle, which chops words in
#: half rather than making them waver.
TREMOLO_MAX_DEPTH = 0.9

#: The stutter grid. 11 Hz is above the rate a syllable arrives at and below
#: the ~20 Hz where a gate stops being rhythm and starts being a buzz, so what
#: is heard is a voice made of more than one throat rather than a voice with a
#: tone on top of it.
STUTTER_HZ = 11.0

#: How long each gate edge is ramped over. A hard gate on a 16 kHz clip
#: clicks, and a click is the one artefact a listener hears as a broken file
#: rather than as a monster; 4 ms is short enough to still read as a gate and
#: long enough that there is no step in the waveform to click on.
STUTTER_RAMP_S = 0.004

#: How much of each window is gated, and how far down, at `stutter_pct = 100`.
#: Both are short of everything: gating more than 60% of the grid, or all the
#: way to silence, removes more of the line than a listener can reconstruct,
#: and an unintelligible monster is a failed one however good the effect is.
STUTTER_MAX_DUTY = 0.6
STUTTER_MAX_CUT = 0.92


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

    The hop is half a grain, which is why the window is Hann: two Hann windows
    overlapped 50% sum to exactly 1, so the cross-fade neither dips nor bulges
    between grains and no per-grain gain correction is needed. The first half
    grain has only one window over it, so the sub-octave fades in over 25 ms
    rather than starting at full weight — inaudible under a dry voice that
    does not, and cheaper than the special case that would remove it.

    Reading at half speed means the source index advances by a half sample,
    which is either a sample or the midpoint of two — so the interpolation is
    one branch and one average rather than the general fractional read
    `_vibrato` needs.

    Written into a separate buffer and added at the end: a grain reads samples
    the write pointer has already passed, and reading them back out of `work`
    would make this a feedback loop rather than a pitch shift.
    """
    n = len(work)
    grain = max(4, int(rate * SUBOCTAVE_GRAIN_S))
    grain -= grain % 2          # even, so the half-grain hop is exact
    half = grain // 2
    window = [0.5 - 0.5 * math.cos(2.0 * math.pi * j / grain) for j in range(grain)]
    sub = [0.0] * n
    for start in range(0, n, half):
        stop = min(grain, n - start)
        for j in range(stop):
            src = start + (j >> 1)
            if j & 1:
                nxt = src + 1 if src + 1 < n else src
                value = 0.5 * (work[src] + work[nxt])
            else:
                value = work[src]
            sub[start + j] += window[j] * value
    mix = (pct / 100.0) * SUBOCTAVE_MAX_MIX
    for i in range(n):
        work[i] += mix * sub[i]


def _noise(work: list[float], pct: int, rate: int) -> None:
    """Breath: white noise shaped by the voice's own envelope.

    A one-pole envelope follower over |x|, times a deterministic LCG's output,
    mixed in at `pct`. Shaped rather than flat because a constant hiss is a bad
    line and a rasp that arrives only where the creature is speaking is a
    damaged throat. The generator is seeded from a constant, not from the
    clock or the id: a clip is cached, and a cached clip that was different the
    second time it was rendered would be a bug nobody could see.

    The generator is written out here rather than taken from `random` for the
    same reason it is not taken from the clock. `random`'s stream is a CPython
    implementation detail and has moved between versions before; this one is
    `x = (a·x + c) mod 2³²` with the constants in view, so the breath a clip
    was rendered with is the breath it will be rendered with on any Python
    that can run this file. (The modulus is a power of two, so the `and` mask
    below is that `mod`, spelled the way that costs less.)

    The follower is one pole with a single time constant rather than the usual
    fast-attack/slow-release pair: the difference is audible on a compressor,
    where the envelope is the gain, and not here, where it is the loudness of
    a hiss underneath a voice that is louder.
    """
    mix = (pct / 100.0) * NOISE_MAX_MIX
    coefficient = 1.0 - math.exp(-1.0 / (rate * NOISE_ENV_S))
    mask = NOISE_LCG_M - 1
    scale = 2.0 / NOISE_LCG_M
    state = NOISE_SEED
    envelope = 0.0
    for i, v in enumerate(work):
        envelope += coefficient * ((v if v >= 0.0 else -v) - envelope)
        state = (NOISE_LCG_A * state + NOISE_LCG_C) & mask
        work[i] = v + mix * envelope * (state * scale - 1.0)


def _bandgrowl(work: list[float], pct: int, rate: int) -> None:
    """Saturation on the low band alone, so the consonants survive it.

    `MAX_DRIVE` is where it is because past it "the consonants stop arriving" —
    which is a statement about the high band, since that is where they live. So
    split at `BANDGROWL_SPLIT_HZ` with a one-pole, saturate the low half, add
    the untouched high half back. The same curve as `_saturate`, driven much
    harder, at an intelligibility cost of nearly nothing.

    Same `_shape` and the same unity-scaling as `_saturate`, because it is the
    same nonlinearity and a second curve here would be a second thing to keep
    true. What is different is that the wet low band is cross-faded against the
    dry one at `pct` rather than only driven harder: `_saturate` at drive 1 is
    already a slight compression, which is fine as the bottom of a slider that
    starts at "some growl" and wrong as the bottom of one that has to start at
    nothing, since a knob at 1% would otherwise tilt the whole voice by the
    2 dB the low band came up.

    The high band is `x - low` rather than a second filter: complementary by
    construction, so the two halves sum back to exactly the input when the
    slider is at zero, which no pair of independently designed filters would.
    """
    mix = pct / 100.0
    drive = 1.0 + mix * (BANDGROWL_DRIVE - 1.0)
    scale = PEAK / (_shape(drive) or 1.0)
    coefficient = 1.0 - math.exp(-2.0 * math.pi * BANDGROWL_SPLIT_HZ / rate)
    dry = 1.0 - mix
    low = 0.0
    for i, v in enumerate(work):
        low += coefficient * (v - low)
        work[i] = (v - low) + dry * low + mix * _shape(drive * low / PEAK) * scale


def _fold(work: list[float], pct: int, rate: int) -> None:
    """Wavefolding: what saturation does, but no longer a voice.

    Where `_saturate` compresses a signal toward a ceiling, this reflects it
    back off one — so a loud syllable comes out with more harmonics than it
    went in with rather than fewer, and the result reads as a thing wearing a
    voice rather than a person with a rough one. Triangular folding, `pct`
    setting how hard the signal is driven into the fold.

    The fold is the triangle wave of the driven signal, which is what makes it
    cheap: `(t + 1) mod 4` maps the whole real line onto one period, and one
    subtract and one comparison bend that period into the triangle. No `asin`,
    no table, and — because the triangle is bounded by ±1 by construction —
    nothing here can hand `_to_pcm` a value it has to clip rather than scale,
    however hard the drive is.

    Cross-faded against the dry signal at `pct` as well as driven by it, so
    that the bottom of the slider is the untreated voice exactly. Both, rather
    than either alone: drive alone would leave a fold at 1% (the signal already
    reaches ±1 at full scale, which is already the first fold), and mix alone
    would put a fully folded signal at 1% weight, which is a quiet buzz added
    to a voice rather than a voice beginning to fold.
    """
    mix = pct / 100.0
    dry = 1.0 - mix
    drive = 1.0 + mix * (FOLD_DRIVE - 1.0)
    for i, v in enumerate(work):
        phase = (drive * v / PEAK + 1.0) % 4.0     # one period of the triangle
        folded = phase - 1.0                       # rising limb: -1 .. +1
        if folded > 1.0:
            folded = 2.0 - folded                  # falling limb, reflected
        work[i] = dry * v + mix * PEAK * folded


def _rectify(work: list[float], pct: int, rate: int) -> None:
    """Blended rectification: an octave up, and nasty with it.

    `|x|` at full blend is the octave-doubling trick every fuzz pedal knows;
    blended against the dry signal at `pct` it is an edge rather than a
    transformation. Cheap — one comparison a sample — and the only effect here
    that adds energy specifically at the top of the band.

    Doubled and DC-blocked before it is blended. Doubled because `|x|` of a
    sine has half the peak-to-peak swing of the sine, so an undoubled wet path
    would arrive quieter than the dry one it is being crossfaded against and
    the slider would read as a fade rather than as an effect. DC-blocked
    because a one-sided signal carries a large offset that is inaudible and
    still occupies headroom — `_to_pcm` normalizes on the peak, so the offset
    would come straight off the level of the finished clip.
    """
    mix = (pct / 100.0)
    dry = 1.0 - mix
    coefficient = 1.0 - math.exp(-2.0 * math.pi * RECTIFY_DC_HZ / rate)
    offset = 0.0
    for i, v in enumerate(work):
        rectified = 2.0 * (v if v >= 0.0 else -v)
        offset += coefficient * (rectified - offset)
        work[i] = dry * v + mix * (rectified - offset)


def _highpass(work: list[float], pct: int, rate: int) -> None:
    """One-pole highpass: thins the voice out.

    Corner slides from `HIGHPASS_MIN_HZ` to `HIGHPASS_MAX_HZ` across the range,
    so 0 is genuinely nothing and 100 is a voice with no chest left in it —
    incorporeal, or a long way off. The complement of the muffle that would
    take the metallic edge off `_cave`, and deliberately gentle: one pole, 6 dB
    an octave, because a steep highpass on a 16 kHz clip sounds like a fault.

    The slide is geometric rather than linear because frequency is heard in
    ratios: a linear sweep from 25 Hz to 1400 Hz spends its whole bottom half
    above 700 Hz, where every step is already a big change, and its top half
    doing nothing. Geometrically, each percent is the same musical interval and
    the bottom of the slider is genuinely inaudible.

    `y[n] = r·(y[n-1] + x[n] - x[n-1])` with `r = exp(-2π·fc/rate)`: one pole
    strictly inside the unit circle for any positive corner, so the recursion
    decays and cannot run away.
    """
    corner = HIGHPASS_MIN_HZ * (HIGHPASS_MAX_HZ / HIGHPASS_MIN_HZ) ** (pct / 100.0)
    pole = math.exp(-2.0 * math.pi * corner / rate)
    last_in = 0.0
    last_out = 0.0
    for i, v in enumerate(work):
        last_out = pole * (last_out + v - last_in)
        last_in = v
        work[i] = last_out


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
    That is not only a nicety about the slider: at `A = 1` the cookbook's
    numerators and denominators are equal term by term, so the transfer
    function is exactly 1 and the filter is provably transparent rather than
    approximately so.

    The coefficients are the peaking-EQ entry of the RBJ audio cookbook,
    written out rather than reduced so they can be checked against it:

        A     = 10^(dBgain/40)          amplitude, and note the /40 — a
                                        peaking filter's gain is A² at the
                                        centre, so A is the half of it
        w0    = 2π·f0/rate              centre, in radians a sample
        alpha = sin(w0)/(2·Q)           bandwidth

        b0 = 1 + alpha·A    b1 = -2·cos(w0)    b2 = 1 - alpha·A
        a0 = 1 + alpha/A    a1 = -2·cos(w0)    a2 = 1 - alpha/A

    divided through by `a0` and run as direct form I. Stable for every A > 0,
    Q > 0 and 0 < w0 < π: the poles of a peaking section sit inside the unit
    circle by construction, which is why this is the filter to reach for and
    not, say, a hand-rolled resonator.
    """
    amount = abs(pct) / 100.0
    centre = FORMANT_BIG_HZ if pct > 0 else FORMANT_SMALL_HZ
    centre = min(centre, rate * 0.45)          # a bell above Nyquist is nonsense
    amplitude = 10.0 ** ((FORMANT_GAIN_DB * amount) / 40.0)
    w0 = 2.0 * math.pi * centre / rate
    cosine = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * FORMANT_Q)
    a0 = 1.0 + alpha / amplitude
    b0 = (1.0 + alpha * amplitude) / a0
    b1 = (-2.0 * cosine) / a0
    b2 = (1.0 - alpha * amplitude) / a0
    a1 = (-2.0 * cosine) / a0
    a2 = (1.0 - alpha / amplitude) / a0
    x1 = x2 = y1 = y2 = 0.0
    for i, v in enumerate(work):
        y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2 = x1
        x1 = v
        y2 = y1
        y1 = y
        work[i] = y


def _phone(work: list[float], pct: int, rate: int) -> None:
    """300-3400 Hz, blended: a helm, a jar, a voice out of a speaking stone.

    The telephone band, which is a band because that is what a narrow channel
    does to a voice and every listener has heard it. A one-pole pair rather
    than a steep filter, and blended against the dry signal at `pct` so the
    slider is a distance rather than a switch.

    One pole each way is 6 dB an octave, which is a long way short of what a
    real telephone did — and the blend is what makes that the right choice
    rather than a compromise: the recognisable thing is the missing chest and
    the missing air, not the steepness of the skirts, and a gentle band
    cross-faded up sounds like the voice moving away into something, where a
    steep one cross-faded up sounds like two signals.
    """
    mix = pct / 100.0
    dry = 1.0 - mix
    low_coefficient = 1.0 - math.exp(-2.0 * math.pi * PHONE_HIGH_HZ / rate)
    pole = math.exp(-2.0 * math.pi * PHONE_LOW_HZ / rate)
    low = 0.0
    last_in = 0.0
    last_out = 0.0
    for i, v in enumerate(work):
        low += low_coefficient * (v - low)      # everything under 3400 Hz
        last_out = pole * (last_out + low - last_in)   # ...minus everything under 300
        last_in = low
        work[i] = dry * v + mix * last_out


def _vibrato(work: list[float], pct: int, rate: int) -> None:
    """Pitch wobble, off a delay line whose read pointer is modulated.

    A sine LFO at `VIBRATO_HZ` moving a fractional read pointer over a short
    delay, linearly interpolated — a delay whose length is changing IS a pitch
    shift, which is the whole trick. `pct` is the depth. Unstable things:
    oozes, something not entirely holding its shape.

    The delay swings from zero to the depth rather than either side of a fixed
    centre, and does it with a raised cosine so it starts at zero. The obvious
    arrangement — a few milliseconds of delay with the LFO either side of it —
    would leave a constant delay at zero depth, which is inaudible but is not
    the same samples, and a slider whose first percent shifts the whole clip by
    a couple of milliseconds is a slider that cannot be tested for
    transparency. Pitch follows the *rate of change* of the delay, so an offset
    that does not move costs nothing and removing it costs nothing either.

    Reads from a copy: the read pointer trails the write pointer, so reading
    `work` itself would feed each output back into the next one.
    """
    n = len(work)
    source = work[:]
    depth = (pct / 100.0) * VIBRATO_DEPTH_S * rate
    step = 2.0 * math.pi * VIBRATO_HZ / rate
    for i in range(n):
        delay = 0.5 * depth * (1.0 - math.cos(step * i))
        read = i - delay
        if read <= 0.0:
            work[i] = source[0]
            continue
        whole = int(read)
        frac = read - whole
        nxt = whole + 1 if whole + 1 < n else whole
        work[i] = source[whole] + frac * (source[nxt] - source[whole])


def _phaser(work: list[float], pct: int, rate: int) -> None:
    """Cascaded all-passes with a swept coefficient: swirl.

    `PHASER_STAGES` first-order all-pass sections whose coefficient is swept by
    an LFO at `PHASER_HZ`, summed with the dry signal so the moving phase
    becomes moving notches. Costs a pass per stage and sounds like nothing else
    in this module — incorporeal, planar, wrong in a way that is not grit.

    Each section is `y[n] = c·x[n] + x[n-1] - c·y[n-1]`, with
    `c = (1 - tan(π·f/rate)) / (1 + tan(π·f/rate))` the usual mapping from the
    frequency where the section turns the phase through 90°. `tan` of a
    positive angle under π/2 is positive, so `|c| < 1` for every corner in
    band and the recursion is stable by construction.

    `pct` scales only the sum, not the sweep: the wet path is built at full
    depth and added at `pct`, so the slider is how deep the notches cut and
    zero is the dry signal exactly. Sweeping the depth instead would move the
    notches as well as flatten them, which is a different (and worse) effect
    at every setting but the top.

    The coefficient is tabulated over one LFO cycle and looked up, because the
    alternative is `PHASER_STAGES` calls to `tan` per sample — four million on
    a long clip, and the single most expensive thing this module would then do.
    All four sections read the same table at the same index, which is what
    makes the notches move together rather than smear.
    """
    n = len(work)
    corners = []
    ratio = PHASER_MAX_HZ / PHASER_MIN_HZ
    for k in range(PHASER_TABLE):
        # A raised cosine over the table, swept geometrically for the same
        # reason `_highpass` sweeps that way: the ear hears ratios.
        lfo = 0.5 - 0.5 * math.cos(2.0 * math.pi * k / PHASER_TABLE)
        corner = PHASER_MIN_HZ * ratio ** lfo
        tangent = math.tan(math.pi * min(corner, rate * 0.45) / rate)
        corners.append((1.0 - tangent) / (1.0 + tangent))
    step = PHASER_TABLE * PHASER_HZ / rate
    sweep = [corners[int(i * step) % PHASER_TABLE] for i in range(n)]
    dry = work[:]
    for _stage in range(PHASER_STAGES):
        last_in = 0.0
        last_out = 0.0
        for i, v in enumerate(work):
            coefficient = sweep[i]
            last_out = coefficient * v + last_in - coefficient * last_out
            last_in = v
            work[i] = last_out
    mix = pct / 100.0
    for i in range(n):
        work[i] = dry[i] + mix * work[i]


def _metal(work: list[float], pct: int, rate: int) -> None:
    """A feedforward comb: fixed notches, and no tail at all.

    `y[n] = x[n] + g*x[n-d]` — the same delay `_cave` uses with the feedback
    taken out, at `METAL_DELAY_S`, which is short enough that what is heard is
    a timbre rather than a repeat. Where `_cave` is the room a creature stands
    in, this is the creature being made of something that rings. They stack.

    Run backwards down the clip, which is what keeps it feedforward: the tap
    reads a sample the loop has not reached yet, so it reads the input rather
    than an output, and no copy of the clip is needed to say so. Going forward
    in place would silently make this `_cave` with a very short delay — the one
    mistake here that would still produce plausible audio.
    """
    delay = max(1, int(rate * METAL_DELAY_S))
    gain = (pct / 100.0) * METAL_MAX_GAIN
    for i in range(len(work) - 1, delay - 1, -1):
        work[i] += gain * work[i - delay]


def _slap(work: list[float], pct: int, rate: int) -> None:
    """One early reflection at `SLAP_DELAY_S`, and only one.

    A single non-feedback tap far enough out to be heard as a surface rather
    than as timbre, and near enough not to be heard as an echo. It is what
    tells a listener the creature is standing in front of something, which
    `_cave` says much more expensively.

    Backwards for the same reason `_metal` is backwards: one tap, reading the
    input, with no copy of the clip and no feedback path to argue about.
    """
    delay = max(1, int(rate * SLAP_DELAY_S))
    gain = (pct / 100.0) * SLAP_MAX_GAIN
    for i in range(len(work) - 1, delay - 1, -1):
        work[i] += gain * work[i - delay]


def _tremolo(work: list[float], pct: int, rate: int) -> None:
    """Sub-audio amplitude modulation: a voice that pulses.

    A sine at `TREMOLO_HZ` scaling the amplitude by `pct` — slow enough to be
    heard as breathing or as something not quite sustaining itself, where the
    same modulation at audio rate would be ring modulation and a different
    effect entirely. Undead, oozes, a thing speaking on borrowed air.

    A raised cosine rather than a plain sine, so the gain starts at 1 and dips:
    the clip begins at full level whatever the depth is, and the first syllable
    of a line is not sometimes half there depending on where the LFO's phase
    happened to start.
    """
    depth = (pct / 100.0) * TREMOLO_MAX_DEPTH
    step = 2.0 * math.pi * TREMOLO_HZ / rate
    for i in range(len(work)):
        work[i] *= 1.0 - 0.5 * depth * (1.0 - math.cos(step * i))


def _stutter(work: list[float], pct: int, rate: int) -> None:
    """A gate on a fixed grid: chittering.

    The clip is divided into `STUTTER_HZ` windows and the amplitude is pulled
    down over part of each, with `pct` setting both how much of the window is
    gated and how far down it goes. Edges are ramped over `STUTTER_RAMP_S`
    rather than switched, because a hard gate on a 16 kHz clip clicks, and a
    click is the one artefact a listener reads as a broken file rather than a
    monster. Swarms, insect things, a voice made of more than one throat.

    Last in the chain: a gate ahead of `_cave` would cut the tail it just made.

    Both at once — how much of the window and how far down — because either
    alone is the wrong shape of knob. All the way down over a widening slice
    chops syllables out from the first percent; a fixed slice fading down is a
    tremolo with corners. Together they open as a shallow flutter and close as
    a gate, which is the range the effect wants.

    The window's gains are computed once and reused across the clip. The grid
    is fixed and absolute — every window is identical — so this is a few
    hundred cosines rather than a few hundred thousand, and the per-sample cost
    is one multiply.
    """
    n = len(work)
    width = max(2, int(rate / STUTTER_HZ))
    amount = pct / 100.0
    closed = int(width * amount * STUTTER_MAX_DUTY)
    floor = 1.0 - amount * STUTTER_MAX_CUT
    ramp = min(max(1, int(rate * STUTTER_RAMP_S)), closed // 2)
    if closed < 2 or ramp < 1:
        # Too little of the window is gated to be worth a pass — which is
        # where the bottom of the slider lands, and is a no-op rather than a
        # special case for it.
        return
    open_until = width - closed
    gains = []
    for j in range(width):
        if j < open_until:
            gains.append(1.0)
        elif j < open_until + ramp:
            # Down over `ramp` samples: no step in the waveform, no click.
            gains.append(1.0 + (floor - 1.0) * ((j - open_until + 1) / ramp))
        elif j >= width - ramp:
            # ...and back up, reaching 1.0 exactly as the window wraps.
            gains.append(floor + (1.0 - floor) * ((j - (width - ramp) + 1) / ramp))
        else:
            gains.append(floor)
    start = 0
    while start < n:
        stop = min(width, n - start)
        for j in range(stop):
            work[start + j] *= gains[j]
        start += width


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
