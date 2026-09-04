"""Bring the fetched files to one level, and to one size.

Fifty-five cues pulled from four libraries do not match each other: one sting
is 12 dB hotter than the next, a Freesound clip carries 300 ms of silence
before the hit so it fires late, and an incompetech bed arrives as a 10 MB MP3
that has to live in the repository. Setting a gain per cue by ear is a worse
version of measuring, and it cannot fix either of the other two.

So this measures and re-encodes, with the numbers `sheep` arrived at by
auditioning the results:

    beds (music, ambience)      EBU R128 loudnorm to -16 LUFS, true peak
                                -1.5 dB; 44.1 kHz stereo VBR MP3
    one-shots (stings, swells,  silence trimmed off both ends, peak normalised
    effects)                    to -0.7 dBFS, 8 ms edge fades, mono 64 kbps

It shells out to **ffmpeg**, which is not a dependency of this repo — it is a
tool you either have or do not, and `have_ffmpeg()` says which. Every command
is built here and handed to an injected runner, so the tests can read the
command lines without ffmpeg installed and run the real thing where it is.

Processing is destructive and the manifest records it: each entry gains a
`normalized` block naming the profile, so a second run is a no-op rather than
a second generation of lossy encoding. To go back, re-fetch the cue with
`fetch --force --no-normalize`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import cues as C

__all__ = ["Profile", "BED", "ONESHOT", "PROFILES", "have_ffmpeg", "normalize_file",
           "normalize_manifest", "measure_peak_db", "probe_duration", "MissingFFmpeg",
           "duration_problem"]


class MissingFFmpeg(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    """What one group of cues should sound like, and weigh.

    `id` is written into the manifest; bump the version in it whenever the
    numbers change, or a re-run will decline to redo work it should redo.
    """

    id: str
    mode: str                  # "ebu" (integrated loudness) or "peak"
    loudnorm: str = ""         # ffmpeg loudnorm parameters, for mode "ebu"
    peak_dbfs: float = -0.7    # target true-ish peak, for mode "peak"
    channels: int = 2
    rate: int = 44100
    encode: tuple[str, ...] = ("-codec:a", "libmp3lame", "-q:a", "6")
    trim_silence: bool = False
    silence_threshold_db: int = -50
    fade_ms: int = 0


# Beds run for minutes under narration, so they are matched on integrated
# loudness rather than peak — a single loud transient must not duck a whole
# five-minute track. `-q:a 6` is ~115 kbps VBR; sheep used ~100 (`-q:a 7`),
# one notch lower than this, and these loop for longer.
BED = Profile(
    id="bed-v1",
    mode="ebu",
    loudnorm="I=-16:TP=-1.5:LRA=11",
    channels=2,
    encode=("-codec:a", "libmp3lame", "-q:a", "6"),
)

# A half-second sting has no meaningful integrated loudness, so these are
# peak-normalised instead, and trimmed: silence at the head of a one-shot is
# latency you cannot get back at playback time. The 8 ms edge fades are what
# stop a hard cut clicking.
ONESHOT = Profile(
    id="oneshot-v1",
    mode="peak",
    peak_dbfs=-0.7,
    channels=1,
    encode=("-codec:a", "libmp3lame", "-b:a", "64k"),
    trim_silence=True,
    fade_ms=8,
)

PROFILES = {
    "music": BED,
    "ambience": BED,
    "sting": ONESHOT,
    "swell": ONESHOT,
    "sfx": ONESHOT,
}

_MAX_VOLUME = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def _check(proc, cmd: list[str]) -> None:
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(f"{cmd[0]} failed: {' / '.join(tail) or proc.returncode}")


def measure_peak_db(path: Path, run=_run) -> float:
    """Peak level in dBFS, from ffmpeg's `volumedetect`."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
           "-af", "volumedetect", "-f", "null", "-"]
    proc = run(cmd)
    _check(proc, cmd)
    m = _MAX_VOLUME.search(proc.stderr or "")
    if not m:
        raise RuntimeError(f"no max_volume in ffmpeg output for {path.name}")
    return float(m.group(1))


def _measure_loudness(path: Path, profile: Profile, run=_run) -> dict:
    """First loudnorm pass: what this file already measures, as JSON."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
           "-af", f"loudnorm={profile.loudnorm}:print_format=json", "-f", "null", "-"]
    proc = run(cmd)
    _check(proc, cmd)
    text = proc.stderr or ""
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"no loudnorm measurement for {path.name}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise RuntimeError(f"unreadable loudnorm measurement for {path.name}") from None


def probe_duration(path: Path, run=_run) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "csv=p=0", str(path)]
    proc = run(cmd)
    _check(proc, cmd)
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        raise RuntimeError(f"no duration for {path.name}") from None


def _silence_filter(profile: Profile) -> str:
    """Trim silence off both ends: trim the head, reverse, trim, reverse back."""
    one = (f"silenceremove=start_periods=1:start_duration=0:"
           f"start_threshold={profile.silence_threshold_db}dB:detection=peak")
    return f"{one},areverse,{one},areverse"


def _encode_args(profile: Profile) -> list[str]:
    return ["-ar", str(profile.rate), "-ac", str(profile.channels), *profile.encode]


def normalize_file(path: Path, profile: Profile, *, run=_run) -> dict:
    """Rewrite `path` in place to the profile. Returns what was done."""
    # An injected runner is the caller's business — only the real one needs
    # the binaries to be there.
    if run is _run and not have_ffmpeg():
        raise MissingFFmpeg("ffmpeg and ffprobe are not on PATH")

    before = path.stat().st_size
    out = path.with_suffix(".norm.mp3")
    report: dict = {"profile": profile.id, "bytes_before": before}

    if profile.mode == "ebu":
        # Two passes, not one. Single-pass loudnorm runs in "dynamic" mode: it
        # compresses to hit the target, which is an effect applied to the score
        # rather than a level change. Measuring first lets the second pass be
        # linear — one constant gain over the whole track.
        measured = _measure_loudness(path, profile, run=run)
        params = ":".join([
            profile.loudnorm,
            f"measured_I={measured['input_i']}",
            f"measured_TP={measured['input_tp']}",
            f"measured_LRA={measured['input_lra']}",
            f"measured_thresh={measured['input_thresh']}",
            f"offset={measured.get('target_offset', '0.0')}",
            "linear=true",
            "print_format=summary",
        ])
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(path),
               "-af", f"loudnorm={params}", *_encode_args(profile), str(out)]
        _check(run(cmd), cmd)
        report.update({"loudnorm": profile.loudnorm,
                       "measured_i_lufs": measured["input_i"],
                       "measured_tp_db": measured["input_tp"]})
    else:
        # Trim first: the fade-out has to land on the trimmed end, and the
        # peak has to be measured on what will actually be encoded.
        trimmed = path.with_suffix(".trim.wav")
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(path)]
        if profile.trim_silence:
            cmd += ["-af", _silence_filter(profile)]
        cmd += ["-f", "wav", str(trimmed)]
        _check(run(cmd), cmd)

        try:
            peak = measure_peak_db(trimmed, run=run)
            duration = probe_duration(trimmed, run=run)
            gain = round(profile.peak_dbfs - peak, 2)
            fade = profile.fade_ms / 1000.0
            chain = [f"volume={gain}dB"]
            if fade and duration > 2 * fade:
                chain.append(f"afade=t=in:st=0:d={fade:g}")
                chain.append(f"afade=t=out:st={duration - fade:.3f}:d={fade:g}")
            cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(trimmed),
                   "-af", ",".join(chain), *_encode_args(profile), str(out)]
            _check(run(cmd), cmd)
            report.update({"peak_before_db": peak, "gain_db_applied": gain,
                           "trimmed_to_s": round(duration, 3), "fade_ms": profile.fade_ms})
        finally:
            trimmed.unlink(missing_ok=True)

    final = path.with_suffix(".mp3")
    out.replace(final)
    if final != path:
        path.unlink(missing_ok=True)
    report["bytes_after"] = final.stat().st_size
    report["file"] = final.name
    return report


def duration_problem(cue_id: str, seconds: float) -> str:
    """A sentence when a file is not the length its cue can use, else "".

    Sources lie about this. incompetech's catalogue gives "Cowboy Sting" as
    8 seconds and ships 54, and "Deep Noise" as 2 seconds and ships 149 — both
    picked as stings off a seven-second audition clip, both of which would have
    played for a minute over the table. The search window filters on the
    claimed length; this is the only place the real one is known.
    """
    cue = C.CUES_BY_ID.get(cue_id)
    if cue is None:
        return ""
    lo, hi = cue.dur
    if seconds > hi:
        return (f"{seconds:.1f}s, but a {cue.group} cue takes {lo:g}-{hi:g}s "
                f"— it will play for {seconds / hi:.0f}x its slot")
    if seconds < lo:
        return f"{seconds:.1f}s, shorter than the {lo:g}s a {cue.group} cue expects"
    return ""


def normalize_manifest(out: Path, *, force: bool = False, run=_run, log=print) -> dict:
    """Normalise every file a manifest names, and record it there.

    Entries already carrying the current profile are left alone unless
    `force`, so running this twice does not re-encode a lossy file twice.
    """
    import hashlib

    path = out / "manifest.json"
    if not path.exists():
        raise SystemExit(f"no manifest at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))

    changed = 0
    problems: list[str] = []
    for cue_id, entry in (manifest.get("cues") or {}).items():
        profile = PROFILES.get(entry.get("group") or "")
        if profile is None:
            continue
        done = (entry.get("normalized") or {}).get("profile")
        if done == profile.id and not force:
            continue
        target = out / entry["file"]
        if not target.exists():
            log(f"  {cue_id:<28} MISSING {entry['file']}")
            continue
        try:
            report = normalize_file(target, profile, run=run)
        except (RuntimeError, OSError) as exc:
            log(f"  {cue_id:<28} FAILED {exc}")
            continue
        final = target.with_suffix(".mp3")
        entry["file"] = str(final.relative_to(out))
        entry["bytes"] = final.stat().st_size
        entry["sha256"] = hashlib.sha256(final.read_bytes()).hexdigest()
        entry["normalized"] = report
        changed += 1

        # The measured length, not the one the source claimed.
        try:
            entry["duration_s"] = round(probe_duration(final, run=run), 2)
        except (RuntimeError, OSError):
            entry.pop("duration_s", None)
        else:
            problem = duration_problem(cue_id, entry["duration_s"])
            if problem:
                entry["duration_warning"] = problem
                problems.append(f"{cue_id}: {problem}")
            else:
                entry.pop("duration_warning", None)
        saved = report["bytes_before"] - report["bytes_after"]
        log(f"  {cue_id:<28} {profile.id} "
            f"{report['bytes_before'] // 1024} kB → {report['bytes_after'] // 1024} kB "
            f"({'-' if saved >= 0 else '+'}{abs(saved) // 1024} kB)")

    path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    log(f"normalised {changed} file(s); manifest.json updated")
    for problem in problems:
        log(f"  WRONG LENGTH  {problem}")
    return manifest
