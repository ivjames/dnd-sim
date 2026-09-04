"""Content-addressed disk cache for synthesized audio.

Every line the narrator has already said is a line Polly never has to say
again: the same words in the same seat produce the same bytes, so the cache
key is the audio's own description. That makes re-listening to a game free,
and it makes a reload free, which matters because the playhead in the browser
is designed to be moved backwards.

Lives under `data/` beside the SQLite file, which is gitignored and survives a
deploy's hard reset (CLAUDE.md). Bounded: a game is a few hundred kilobytes,
but nothing here ever expires on its own, so `prune` drops the
least-recently-used files once the directory passes its ceiling.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading

__all__ = ["AudioCache", "cache_key"]


def cache_key(*parts: str) -> str:
    """A hex digest over everything that changes the audio."""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class AudioCache:
    """A flat two-level directory of `<sha256>.mp3`.

    The suffix is a name and not a claim: a monster's clip is a WAV
    (`tts/dsp.py` treats `pcm` and wraps it, because an MP3 would need an
    encoder), and it is filed under the same extension as everything else. The
    key decides the format — it is a digest over the cast, and only a treated
    cast produces a WAV — so nothing has to read the suffix to know what it
    holds, and giving a monster its own would orphan every clip on the disk to
    say something the key already says.

    Reads are lock-free (a file is written whole or not at all — see `put`);
    writes take a lock only to keep the byte total honest. `max_bytes <= 0`
    disables pruning, which is what the tests want.
    """

    def __init__(self, root: str, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.root = os.path.abspath(root)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._writes_since_prune = 0

    def path_for(self, key: str) -> str:
        return os.path.join(self.root, key[:2], key + ".mp3")

    def get(self, key: str) -> bytes | None:
        try:
            with open(self.path_for(key), "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        if not data:
            return None
        # Touch, so pruning drops what nobody is listening to rather than what
        # was synthesized longest ago. Best-effort: a read-only cache still reads.
        try:
            os.utime(self.path_for(key), None)
        except OSError:
            pass
        return data

    def put(self, key: str, data: bytes) -> None:
        path = self.path_for(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Write-then-rename: a reader either sees the whole clip or no file
            # at all, never a half-written one being played as it lands.
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return          # a cache that cannot be written is still a working narrator
        with self._lock:
            self._writes_since_prune += 1
            due = self._writes_since_prune >= 50
            if due:
                self._writes_since_prune = 0
        if due:
            self.prune()

    def total_bytes(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def prune(self) -> int:
        """Drop least-recently-used files until under the ceiling. Returns bytes freed."""
        if self.max_bytes <= 0:
            return 0
        entries: list[tuple[float, int, str]] = []
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                entries.append((st.st_atime, st.st_size, path))
                total += st.st_size
        if total <= self.max_bytes:
            return 0
        entries.sort()
        freed = 0
        for _atime, size, path in entries:
            if total - freed <= self.max_bytes:
                break
            try:
                os.unlink(path)
            except OSError:
                continue
            freed += size
        return freed
