# Sourcing music, ambience, stings, swells and effects

A tool for picking the game's audio, not for playing it. It searches the
openly-licensed libraries, builds a preview screen you audition in a browser,
and turns what you picked into files on disk with a manifest and credits. What
plays the manifest — the spectator UI, a server mixer, something else — is a
separate decision and is not built yet.

```bash
export FREESOUND_API_KEY=...            # optional, and the one worth having
export JAMENDO_CLIENT_ID=...            # optional, for full-length music
.venv/bin/python -m tools.audio harvest # search every cue → audio/candidates.json + picker.html
open audio/picker.html                  # audition, assign, tune, Copy configuration
# save what you copied as audio/config.json
.venv/bin/python -m tools.audio fetch   # download → audio/assets/, manifest.json, CREDITS.md,
                                        # levelled with ffmpeg where you have it
.venv/bin/python -m tools.audio verify  # re-hash what was fetched
```

**The picked audio is committed.** `audio/assets/`, `manifest.json`,
`CREDITS.md` and the `config.json` that produced them are tracked, because a
deploy hard-resets the checkout from git and anything untracked would not
survive one. Only two build artefacts are ignored — `candidates.json` (a search
dump) and `picker.html` (generated from it), both re-made by one `harvest`.
That makes size a real cost, which is half of why `fetch` re-encodes: a
five-minute bed off incompetech is ~10 MB as published and about a fifth of
that afterwards.

## Where the audio comes from

Four libraries are worth querying programmatically — three with a real search
API and a licence field worth trusting, plus one catalogue that publishes
itself as a JSON file.

| Source | Good for | Licences | Key | Notes |
|---|---|---|---|---|
| [Freesound](https://freesound.org/docs/api/) | effects, stings, swells, ambience, some loops | CC0, CC BY, CC BY-NC (the harvester keeps the first two) | [free, instant](https://freesound.org/apiv2/apply/) → `FREESOUND_API_KEY` | 60 requests/min, 2000/day. Originals need OAuth2; previews do not (see below) |
| [Jamendo](https://developer.jamendo.com/v3.0) | full-length music beds | CC, per track via `license_ccurl` | [free](https://devportal.jamendo.com/) → `JAMENDO_CLIENT_ID` | Their API terms govern the free tier — read them before anything commercial |
| [incompetech](https://incompetech.com/music/royalty-free/music.html) | music beds, and a Stings genre | CC BY 4.0, all of it | none | Kevin MacLeod's 1400-piece catalogue, published whole as [`pieces.json`](https://incompetech.com/music/royalty-free/pieces.json) — fetched once per run and searched in memory, so it is one request and no rate limit. Its `feel` vocabulary is *Dark, Eerie, Mysterious, Unnerving, Somber, Epic, Action, Suspenseful*, which is this game's mood list almost exactly |
| [Internet Archive](https://archive.org/advancedsearch.php) | music and long ambience | whatever the uploader declared; the query keeps only public-domain / BY / BY-SA | none | Works with no credentials at all, which is why it is here. The metadata is user-supplied and the hit rate is poor — a fallback, not a first choice |

**Why a CC BY catalogue is in the default set:** the `sheep` repo sourced two
rounds of CC0 music for its game and *neither survived audition* — the CC0 pool
is thin, and it switched to CC BY. That lesson is baked in here: incompetech is
keyless, so it is queried by default, and the credit it requires is generated
for you rather than left as homework.

Everything else worth raiding has no search API. The picker's **Add by URL**
form takes a direct audio link plus title, author and licence, so these are one
paste each rather than unreachable:

- [OpenGameArt](https://opengameart.org/) — filter by CC0; deep in fantasy
  loops and RPG effect packs.
- [Kenney](https://kenney.nl/assets?q=audio) — CC0 packs, consistently made,
  good for UI ticks and impacts.
- [Pixabay](https://pixabay.com/music/) — sizeable music and SFX libraries
  under the Pixabay Content License. Their public API covers images and video
  only, so music is a manual paste.
- [ccMixter](http://dig.ccmixter.org/) — CC music, mostly BY / BY-NC.
- [Audionautix](https://audionautix.com/) (Jason Shaw, CC BY) — scrapable but
  not wired in: its categories are acoustic, bluegrass, country, folk, jazz,
  lo-fi and so on, with nothing orchestral, cinematic or horror. It is the
  right library for `sheep` and the wrong one for a dungeon.
- [Sonniss GDC bundles](https://sonniss.com/gameaudiogdc) — large royalty-free
  SFX bundles; read the licence that ships inside each bundle.
- [Tabletop Audio](https://tabletopaudio.com/) — built for exactly this job,
  but under its own terms rather than a Creative Commons licence. Check them
  before using anything from it.

## Licences

`sources.PERMISSIVE` is `cc0`, `pd`, `by`, `by-sa` — public domain or
credit-required. The harvester drops anything else from the candidate list and
`fetch` refuses it, because a NonCommercial or NoDerivatives track is a thing
you cannot ship and therefore noise in a list you are auditioning by ear. If
you have a reason to take one anyway, `fetch --allow by-nc` says so out loud.

`fetch` writes `audio/CREDITS.md` from the manifest, grouped by licence, listing
every asset with author, source and page. Anything under a BY or BY-SA heading
has to be credited wherever the audio plays.

The file opens with a **paste block**: the finished credit sentences, one per
track, deduplicated across cues, in each source's own required wording —

```
"Curse of the Scarab" Kevin MacLeod (incompetech.com) — Licensed under Creative Commons: By Attribution 4.0 — https://creativecommons.org/licenses/by/4.0/
```

That block is the deliverable, not a list to write credits *from*: attribution
is only done when a listener can read it, so it goes on the credits panel as
it stands.

Nothing here touches game *content* licensing: SRD 5.1 (CC-BY-4.0) still governs
what the rules engine knows, and audio licences are a separate obligation.

## Levelling

Fifty-five cues pulled from four libraries do not match each other. One sting
is 12 dB hotter than the next, a Freesound clip carries 300 ms of silence
before the hit so it fires late, and the beds are large. Setting a gain per cue
by ear is a worse version of measuring, and it fixes neither of the other two —
so `fetch` measures and re-encodes, with the numbers `sheep` arrived at by
auditioning the results:

| Group | What happens |
|---|---|
| music, ambience (`bed-v1`) | EBU R128 loudnorm to **-16 LUFS**, true peak **-1.5 dB**, LRA 11; 44.1 kHz stereo, VBR MP3 (`-q:a 6`, ~115 kbps) |
| stings, swells, effects (`oneshot-v1`) | silence trimmed off both ends at -50 dB, peak normalised to **-0.7 dBFS**, **8 ms** edge fades, mono 64 kbps |

Two details worth knowing. Beds are levelled in **two passes** — measure, then
apply the measurement — because single-pass `loudnorm` runs in dynamic mode and
compresses to hit the target, which is an effect applied to the score rather
than a level change; the second pass is linear, one constant gain. One-shots
are peak-normalised instead, because a half-second sting has no meaningful
integrated loudness, and the 8 ms fades are what stop a trimmed edge clicking.
The peak lands a few tenths of a dB off target after encoding, since it is
measured before the MP3 stage.

This needs **ffmpeg** on PATH. It is not a dependency of this repo and nothing
else here wants it: without it, `fetch` says so and keeps the files exactly as
downloaded. `fetch --no-normalize` opts out where you have it; `python -m
tools.audio normalize` runs it over a directory fetched earlier.

Processing is destructive, and the manifest records it — each entry gains a
`normalized` block naming the profile, so a second run is a no-op rather than a
second generation of lossy encoding. Bump the version in a profile's `id` when
you change its numbers and the next run will redo the files. To get an original
back: `fetch --force --no-normalize`.

## Quality and the preview caveat

Freesound generates .ogg and .mp3 previews for every upload; downloading the
**original** file needs OAuth2 (a user login flow), while previews need only the
API token. The harvester takes the HQ MP3 preview and the fetcher downloads
that, so Freesound assets arrive as 128 kbps MP3 rather than the uploader's WAV.
For a sting under narration that is inaudible; for a long music bed it is worth
knowing. The licence on the audio is the same either way — it governs the
recording, not the encoding — so nothing about attribution changes. If a
particular asset deserves the original, fetch it by hand from its `page_url` and
drop it in over the downloaded file (`verify` will then report a hash mismatch,
which is the correct complaint).

Jamendo returns a real download URL where the artist allowed it and a stream URL
otherwise; the fetcher prefers the former. Archive files are whatever was
uploaded.

## The cue table

`tools/audio/cues.py` is the authoritative list: 55 cues in five groups, 24 of
them marked required. `python -m tools.audio cues` prints it.

- **music** — long loopable beds, switched by phase: explore, tension, combat,
  desperate combat, victory, defeat, downtime.
- **ambience** — the place: stone corridors, wet cave, crypt, deep mine, night
  forest, fen, campfire, road, weather. Assign one (or two, layered) per
  scenario in `examples/`.
- **sting** — short one-shots on a specific moment: initiative, crit, natural 1,
  a body dropping, death, a failed death save, a scene change.
- **swell** — risers with no impact, for the moments no single event can
  identify. All manual.
- **sfx** — the mechanical layer: dice, hit, miss, one per damage type, spell
  cast, heal, conditions, movement, turn ticks.

Each cue carries a `match` rule — an event kind plus equality constraints on the
event's `data`, with dotted paths for nested values:

```python
{"kind": "attack", "data": {"hit": False, "roll.natural": 1}}   # sting_fumble
{"kind": "damage", "data": {"damage_type": "fire"}}             # sfx_dmg_fire
```

Audio layers rather than replaces, so **one event lights at most one cue per
group**: `combat_start` swaps the music bed *and* hits a sting; a crit fires
`sting_crit` *and* `sfx_attack_hit`. Within a group the most specific match
wins. `cues.cues_for_event(event)` is that logic, and `fetch` copies each cue's
match rule into the manifest so a player needs the manifest and nothing else.

Constraints are deliberately dumb — no ranges, no negation, no expressions.
A cue that cannot be expressed that way carries `match: null` and says in its
`when` what fires it.

The test suite holds the table to the engine: every kind in
`engine.events.EVENT_KINDS` must either have a cue or be listed in
`UNSCORED_EVENT_KINDS`, no cue may match a kind the engine cannot emit, and a
full seeded mock game is routed through the table to prove the rules fire on
events the engine really produces. Add an event kind and the audio tests fail
until someone decides whether it makes a noise.

## The two documents

**`config.json`** — what the picker copies out. One entry per assigned cue: the
candidate's identity and URLs, its licence, and the playback knobs you set
(`gain_db`, `loop`, `fade_in_ms`, `fade_out_ms`, `trim_start_s`, `trim_end_s`).

```json
{"version": 1, "assignments": {
  "sting_crit": {"source": "freesound", "source_id": "316847",
                 "title": "sword-hit.wav", "author": "someone",
                 "license": "cc0", "page_url": "https://freesound.org/s/316847/",
                 "download_url": "https://cdn.freesound.org/previews/316/316847_1-hq.mp3",
                 "duration": 1.4, "gain_db": -8, "loop": false,
                 "fade_in_ms": 0, "fade_out_ms": 0,
                 "trim_start_s": 0, "trim_end_s": null}}}
```

**`manifest.json`** — what `fetch` writes beside the files. The same knobs, plus
the local path, size and sha256, plus the cue's `match` rule and `when`, plus
the credit block. Ordered like the cue table, so a diff between two runs reads.

## Commands

| | |
|---|---|
| `harvest` | search every cue and write `candidates.json`, then rebuild the picker. `--group sting`, `--cues a,b`, `--required`, `--per-query N`, `--source freesound`. Re-running one cue keeps the others |
| `picker` | rebuild `picker.html` from an existing `candidates.json` |
| `fetch` | download a config. `--config -` reads stdin, `--dry-run` lists without downloading, `--force` re-downloads, `--allow by-nc` widens the licence gate |
| `verify` | re-hash every file against the manifest |
| `cues` | print the cue table; `--json` for the machine-readable form |

`--out` moves the working directory (default `audio/`).

## Using the picker

It is one self-contained HTML file with the candidates baked in — open it off
the filesystem, no server. Audio streams from the source, so the machine doing
the picking needs the network.

Cues on the left, candidates in the middle, the assignment and its knobs on the
right. <kbd>j</kbd>/<kbd>k</kbd> move, <kbd>space</kbd> auditions,
<kbd>enter</kbd> assigns, <kbd>x</kbd> clears, <kbd>n</kbd>/<kbd>p</kbd> change
cue, <kbd>u</kbd> jumps to the next unassigned required cue. The gain knob is
applied to the preview, so what you hear is what the config asks for. Picks are
kept in the browser's local storage; **Copy configuration** is the output, and
**Import config** takes one back so a config can be revised later rather than
redone.

## What this deliberately does not do

- **No playback in the game.** Nothing under `web/` or `orchestrator/` imports
  any of this, and no event currently produces a sound. The manifest is the
  handoff point; wiring it into the spectator UI is a separate change with its
  own decisions (per-viewer volume, ducking under the narration voice, whether
  beds cross-fade on the client or a mixer runs server-side).
- **The playback knobs are still intent, not baked in.** Levelling is applied
  to the file; `gain_db`, `loop`, the fades and the trim points recorded per cue
  are for a player to honour at runtime, and nothing applies them yet.
- **No original-quality Freesound downloads.** That needs the OAuth2 flow;
  previews are what is fetched.
