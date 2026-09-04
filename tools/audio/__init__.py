"""Sourcing thematic music, ambience, stings, swells and effects.

`cues.py` is the cue table; `sources.py` searches the openly-licensed
libraries; `harvest.py` fills a candidate set; `build.py` bakes the picker
page; `fetch.py` turns a picked config into files, a manifest and credits.
Run it with `python -m tools.audio --help`. Full notes: AUDIO.md.
"""

from . import cues  # noqa: F401
