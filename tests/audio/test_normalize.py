"""Levelling the fetched files.

Most of this reads the ffmpeg command lines through an injected runner, so it
runs anywhere; the round trip at the bottom does the real thing and skips
where ffmpeg is not installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from tools.audio import normalize as N

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")

LOUDNORM_JSON = """[Parsed_loudnorm_0 @ 0x55] \n{
\t"input_i" : "-24.51",
\t"input_tp" : "-3.20",
\t"input_lra" : "7.30",
\t"input_thresh" : "-34.62",
\t"output_i" : "-16.01",
\t"target_offset" : "-0.11"
}
"""


class FakeRun:
    """Records commands and writes whatever output file each one names."""

    def __init__(self, peak_db=-12.5, duration=2.0):
        self.cmds: list[list[str]] = []
        self.peak_db = peak_db
        self.duration = duration

    def __call__(self, cmd):
        self.cmds.append(list(cmd))
        out, err = "", ""
        if "volumedetect" in " ".join(cmd):
            err = f"[Parsed_volumedetect_0 @ 0x1] max_volume: {self.peak_db} dB\n"
        elif "print_format=json" in " ".join(cmd):
            err = LOUDNORM_JSON
        elif cmd[0] == "ffprobe":
            out = f"{self.duration}\n"
        elif cmd[-1] != "-":
            from pathlib import Path
            Path(cmd[-1]).write_bytes(b"ID3" + b"x" * 100)
        return subprocess.CompletedProcess(cmd, 0, out, err)

    def filters(self) -> list[str]:
        out = []
        for cmd in self.cmds:
            if "-af" in cmd:
                out.append(cmd[cmd.index("-af") + 1])
        return out


def make_file(tmp_path, name="sfx_dice.mp3", data=b"ID3original"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ------------------------------------------------------------------ parsing

def test_peak_is_read_from_volumedetect(tmp_path):
    run = FakeRun(peak_db=-17.25)
    assert N.measure_peak_db(make_file(tmp_path), run=run) == -17.25


def test_a_missing_peak_is_an_error(tmp_path):
    def run(cmd):
        return subprocess.CompletedProcess(cmd, 0, "", "nothing useful here")
    with pytest.raises(RuntimeError, match="no max_volume"):
        N.measure_peak_db(make_file(tmp_path), run=run)


def test_a_failing_ffmpeg_says_what_it_said(tmp_path):
    def run(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "Invalid data found\n")
    with pytest.raises(RuntimeError, match="Invalid data found"):
        N.measure_peak_db(make_file(tmp_path), run=run)


def test_the_loudness_measurement_is_pulled_out_of_the_noise(tmp_path):
    got = N._measure_loudness(make_file(tmp_path), N.BED, run=FakeRun())
    assert got["input_i"] == "-24.51"
    assert got["target_offset"] == "-0.11"


# ------------------------------------------------------------ command lines

def test_a_bed_is_measured_first_and_then_levelled_linearly(tmp_path):
    run = FakeRun()
    report = N.normalize_file(make_file(tmp_path, "music_combat.mp3"), N.BED, run=run)

    measure, apply = run.filters()
    assert measure == "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json"
    assert apply.startswith("loudnorm=I=-16:TP=-1.5:LRA=11:")
    assert "measured_I=-24.51" in apply and "measured_TP=-3.20" in apply
    assert "measured_LRA=7.30" in apply and "measured_thresh=-34.62" in apply
    assert "offset=-0.11" in apply
    assert "linear=true" in apply, "a bed gets one constant gain, not a compressor"

    encode = run.cmds[-1]
    assert encode[encode.index("-ac") + 1] == "2"
    assert encode[encode.index("-ar") + 1] == "44100"
    assert "-q:a" in encode and encode[encode.index("-q:a") + 1] == "6"
    assert report["profile"] == "bed-v1"
    assert report["measured_i_lufs"] == "-24.51"


def test_a_one_shot_is_trimmed_then_peaked_then_faded(tmp_path):
    run = FakeRun(peak_db=-12.5, duration=2.0)
    report = N.normalize_file(make_file(tmp_path), N.ONESHOT, run=run)

    trim, encode = run.filters()[0], run.filters()[-1]
    assert trim.count("silenceremove") == 2 and trim.count("areverse") == 2
    assert "start_threshold=-50dB" in trim
    # -0.7 target from a -12.5 peak is +11.8 dB
    assert "volume=11.8dB" in encode
    assert "afade=t=in:st=0:d=0.008" in encode
    assert "afade=t=out:st=1.992:d=0.008" in encode

    cmd = run.cmds[-1]
    assert cmd[cmd.index("-ac") + 1] == "1", "one-shots are mono"
    assert cmd[cmd.index("-b:a") + 1] == "64k"
    assert report["gain_db_applied"] == 11.8
    assert report["peak_before_db"] == -12.5
    assert report["trimmed_to_s"] == 2.0


def test_a_clip_shorter_than_its_fades_is_not_faded(tmp_path):
    run = FakeRun(duration=0.01)
    N.normalize_file(make_file(tmp_path), N.ONESHOT, run=run)
    assert "afade" not in run.filters()[-1]


def test_the_working_files_are_cleaned_up(tmp_path):
    run = FakeRun()
    N.normalize_file(make_file(tmp_path), N.ONESHOT, run=run)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sfx_dice.mp3"]


def test_a_non_mp3_becomes_one(tmp_path):
    run = FakeRun()
    report = N.normalize_file(make_file(tmp_path, "sfx_dice.ogg"), N.ONESHOT, run=run)
    assert report["file"] == "sfx_dice.mp3"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sfx_dice.mp3"]


def test_every_cue_group_has_a_profile():
    from tools.audio import cues as C
    assert set(N.PROFILES) == set(C.GROUPS)


# --------------------------------------------------------------- the manifest

def manifest_dir(tmp_path, **over):
    (tmp_path / "assets" / "sfx").mkdir(parents=True)
    f = tmp_path / "assets" / "sfx" / "sfx_dice.mp3"
    f.write_bytes(b"ID3original")
    entry = {"file": "assets/sfx/sfx_dice.mp3", "group": "sfx", "bytes": f.stat().st_size,
             "sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "credit": {}}
    entry.update(over)
    (tmp_path / "manifest.json").write_text(json.dumps({"version": 1, "cues": {"sfx_dice": entry}}))
    return tmp_path


def read_manifest(tmp_path):
    return json.loads((tmp_path / "manifest.json").read_text())["cues"]["sfx_dice"]


def test_the_manifest_records_what_was_done_and_re_hashes(tmp_path):
    out = manifest_dir(tmp_path)
    N.normalize_manifest(out, run=FakeRun(), log=lambda *_: None)
    entry = read_manifest(out)
    assert entry["normalized"]["profile"] == "oneshot-v1"
    body = (out / entry["file"]).read_bytes()
    assert entry["bytes"] == len(body)
    assert entry["sha256"] == hashlib.sha256(body).hexdigest()


def test_a_second_run_does_not_re_encode(tmp_path):
    out = manifest_dir(tmp_path, normalized={"profile": "oneshot-v1"})
    run = FakeRun()
    N.normalize_manifest(out, run=run, log=lambda *_: None)
    assert run.cmds == [], "re-encoding an already-levelled file loses a generation"

    N.normalize_manifest(out, run=run, force=True, log=lambda *_: None)
    assert run.cmds, "--force should redo it"


def test_an_older_profile_is_redone(tmp_path):
    out = manifest_dir(tmp_path, normalized={"profile": "oneshot-v0"})
    run = FakeRun()
    N.normalize_manifest(out, run=run, log=lambda *_: None)
    assert run.cmds


def test_a_missing_file_is_reported_not_fatal(tmp_path):
    out = manifest_dir(tmp_path)
    (out / "assets" / "sfx" / "sfx_dice.mp3").unlink()
    said = []
    N.normalize_manifest(out, run=FakeRun(), log=said.append)
    assert any("MISSING" in s for s in said)
    assert "normalized" not in read_manifest(out)


def test_a_failure_leaves_the_entry_alone(tmp_path):
    out = manifest_dir(tmp_path)

    def boom(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "Invalid data found\n")

    said = []
    N.normalize_manifest(out, run=boom, log=said.append)
    assert any("FAILED" in s for s in said)
    assert "normalized" not in read_manifest(out)


def test_no_manifest_is_a_clean_exit(tmp_path):
    with pytest.raises(SystemExit, match="no manifest"):
        N.normalize_manifest(tmp_path, run=FakeRun(), log=lambda *_: None)


# ------------------------------------------------------------- the real thing

@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_one_shot_really_is_trimmed_and_levelled(tmp_path):
    """A quiet 0.5s tone padded with 0.4s of silence at each end."""
    src = tmp_path / "assets" / "sfx" / "sfx_dice.mp3"
    src.parent.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=800:duration=0.5",
         "-af", "adelay=400|400,apad=pad_dur=0.4,volume=-20dB",
         "-codec:a", "libmp3lame", "-q:a", "2", str(src)],
        check=True, timeout=120)

    before = N.probe_duration(src)
    assert before > 1.2, "the fixture should carry its silence"

    N.normalize_file(src, N.ONESHOT)

    assert N.probe_duration(src) < 0.7, "the silence should be gone"
    assert N.measure_peak_db(src) > -3.0, "and it should be loud enough to hear"
    channels = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, timeout=60).stdout.strip()
    assert channels == "1"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_bed_really_lands_on_the_target_loudness(tmp_path):
    """Two tones 22 dB apart, so a one-pass compressor would look different."""
    src = tmp_path / "music_combat.mp3"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=200:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
         "-filter_complex",
         "[0:a]volume=-34dB[a];[1:a]volume=-12dB[b];[a][b]concat=n=2:v=0:a=1",
         "-ar", "48000", "-ac", "2", "-codec:a", "libmp3lame", "-b:a", "320k", str(src)],
        check=True, timeout=120)
    before = src.stat().st_size

    N.normalize_file(src, N.BED)

    measured = float(N._measure_loudness(src, N.BED)["input_i"])
    assert -16.6 < measured < -15.4, f"landed at {measured} LUFS, wanted -16"
    assert src.stat().st_size < before / 2, "and the file should be smaller"
