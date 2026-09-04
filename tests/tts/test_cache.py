"""The clip cache: a line is paid for once."""

from __future__ import annotations

import os
import time

from tts.cache import AudioCache, cache_key


def test_a_clip_survives_the_round_trip(tmp_path):
    c = AudioCache(str(tmp_path), 0)
    key = cache_key("standard", "Brian|0|100|0", "<speak>Hello.</speak>")
    assert c.get(key) is None
    c.put(key, b"ID3-pretend-mp3")
    assert c.get(key) == b"ID3-pretend-mp3"
    assert os.path.exists(c.path_for(key))
    # Content-addressed: the same description is the same file, a different
    # one is a different file.
    assert cache_key("standard", "Brian|0|100|0", "<speak>Hello.</speak>") == key
    assert cache_key("standard", "Joanna|0|100|0", "<speak>Hello.</speak>") != key


def test_nothing_half_written_is_ever_served(tmp_path):
    c = AudioCache(str(tmp_path), 0)
    c.put("a" * 64, b"x" * 4096)
    leftovers = [f for _d, _s, fs in os.walk(str(tmp_path)) for f in fs if f.endswith(".part")]
    assert leftovers == []


def test_an_unwritable_cache_is_not_a_broken_narrator(tmp_path):
    """A read-only disk costs money, not silence."""
    blocked = tmp_path / "nope"
    blocked.write_text("I am a file, not a directory")
    c = AudioCache(str(blocked / "cache"), 0)
    c.put("b" * 64, b"data")          # must not raise
    assert c.get("b" * 64) is None


def test_pruning_drops_what_nobody_is_listening_to(tmp_path):
    c = AudioCache(str(tmp_path), 3000)
    keys = [str(i) * 64 for i in range(4)]
    for k in keys:
        c.put(k, b"z" * 1000)
        time.sleep(0.01)
    # Reading the oldest makes it the most recent thing anyone wanted.
    assert c.get(keys[0]) is not None
    freed = c.prune()
    assert freed > 0 and c.total_bytes() <= 3000
    assert c.get(keys[0]) is not None      # kept: it was just played
    assert c.get(keys[1]) is None          # dropped: least recently used

    # No ceiling means no pruning, which is what the tests and a small game want.
    unbounded = AudioCache(str(tmp_path / "u"), 0)
    unbounded.put("c" * 64, b"y" * 10_000)
    assert unbounded.prune() == 0 and unbounded.get("c" * 64) is not None
