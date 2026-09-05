# Sourcing music, ambience, stings, swells and effects

A tool for picking the game's audio. It searches the openly-licensed libraries,
builds a preview screen you audition in a browser, and turns what you picked
into files on disk with a manifest and credits.

**The spectator page plays that manifest.** `web/routes/audio.py` serves it at
`GET /api/audio` with the files under `/audio/...`, `web/static/cues.js` turns
an event into the cues it fires — the same rules as `cues.py`, checked against
it by `web/tests/test_cues_js.py` — and `app.js` mixes them under the
narration. The page holds no cue id and no event kind: everything it plays it
learns from the manifest, so re-picking the audio changes what the game sounds
like without touching the page. See "Playing it" below.

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
survive one. Only the build artefacts are ignored — `candidates.json` (a search
dump) and `picker.html` (generated from it), both re-made by one `harvest`, and
the catalogue database below, re-made by one `catalog build`.
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
| [incompetech](https://incompetech.com/music/royalty-free/music.html) | music beds and stings — **not** ambience | CC BY 4.0, all of it | none | Kevin MacLeod's 1442-piece catalogue, published whole as [`pieces.json`](https://incompetech.com/music/royalty-free/pieces.json) — fetched once per run and searched in memory, so it is one request and no rate limit. Its `feel` vocabulary is *Dark, Eerie, Mysterious, Unnerving, Somber, Epic, Action, Suspenseful*, which is this game's mood list almost exactly. Worth more than a per-cue search: see "Searching incompetech properly" |
| [Internet Archive](https://archive.org/advancedsearch.php) | field-recorded ambience, and music | whatever the uploader declared; the query keeps only public-domain / BY / BY-SA | none | Works with no credentials at all. For ambience the query narrows to field recordings (radio aporee and anything tagged as one), which is the difference between the sound of a crypt and a piece of music about one |

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

**Ambience is a recording of a place, not a piece about one.** A composer's
catalogue will answer "crypt ambience" with something atmospheric and wrong, so
incompetech is never asked for that group, and the Archive's ambience query is
narrowed to field recordings. Generic words are dropped from an Archive query
before it is sent — every field recording's description says "ambience", so
ORing that word in returns the collection in its own order and the word that
picks one out does no work.

## Searching incompetech properly

`harvest` asks each library the cue's own words and keeps what scores. That is
the right shape for four libraries at once and the wrong one for the question
"what in this catalogue is dark, slow, and long enough to loop under a scene",
because incompetech's own page cannot answer it either: it searches one
lowercased substring over title, instruments and description, ANDs a set of
feel chips, and offers **no tempo, duration, collection or date filter at
all**. Those are exactly the axes a bed is chosen on.

So the catalogue gets a local database:

```bash
.venv/bin/python -m tools.audio catalog build          # two requests, ~1 MB, no key
.venv/bin/python -m tools.audio catalog query \
    --feel Dark --feel Mysterious --bpm-max 90 --min-length 3:00 --sort bpm
```

`build` fetches [`pieces.json`](https://incompetech.com/music/royalty-free/pieces.json),
normalises it and writes `audio/incompetech.sqlite3` — 1442 pieces, one row
each, keyed by filename. It prints what it ingested and what resolved, and
takes a second request to check the lookup tables against the catalogue page
(`--no-check-lookups` skips it, `--from-file` builds from a saved copy).

**The database is a build artefact and is gitignored**, unlike everything else
under `audio/`. One command rebuilds it, it is not on the runtime path, and a
vendored copy of someone else's catalogue would go stale and churn in every
diff. `manifest.json` is committed for the opposite reason: the page reads it,
and a deploy hard-resets the checkout.

### What normalising it means

The published JSON is thin and dirty, and all of this is real:

| Raw | Stored |
|---|---|
| `genre` "10" | a `genre` row — Horror |
| `collection` "12" | a `collection` row — Hard Electronic, category *Electronic and Rock* |
| `length` "00:03:20" | `length_s` 200; "00:00:00" (3 rows) is unknown, so NULL |
| `bpm` "0" (238 rows), null (8) | NULL — "not measured" is not a tempo |
| `instruments` "Strings, Choir\r\n" | rows in `piece_instrument`, 313 instruments |
| `feel` "Dark, Eerie" | rows in `piece_feel`, 22 feels |
| `null` and `""`, used interchangeably | one representation: NULL |
| `\r\n` inside titles and descriptions, 505 rows with stray whitespace | stripped |
| `filename` | the working MP3 URL, and the ISRC detail page |

Three of these are traps worth naming.

**`collection` is a `code`, not an `id`.** Each collection in the page's
JavaScript carries both and they differ for all but a handful; the page's own
`getCollectionName()` matches on `code`. Joining on `id` still returns a name
for every row — the wrong one for 133 of them, and code 12 would come back as
Polka rather than Hard Electronic.

**A tempo of 0 is not slow.** Sixteen per cent of the catalogue says `bpm` 0,
meaning nobody measured it. Kept as a number it lands at the top of every
"slow" query; kept as NULL it stays out of a range and `--bpm-unknown` asks
for it deliberately.

**The `wav` field holds no WAV.** None of its 268 values end in `.wav`; most
point at a Downloads page that 404s and the rest at a filmmusic.io page that
has moved. It is stored verbatim as `wav_link` and is never offered as audio.

The lookup tables themselves live in `tools/audio/incompetech.py` — one
transcription of someone else's numbering, which `sources.py` reads too rather
than keeping a second copy of. `build` re-reads the page and reports drift
because a table that has silently gone stale is the failure this cannot
otherwise see.

### Querying it

Filters AND with each other. `--feel` repeats to mean *all of these* (which is
the point of the relation), `--feel-any` to mean *any of these*; `--instrument`
repeats and matches as a substring, so `--instrument drum` finds Drums and Log
Drums. `--genre`, `--collection` and `--category` OR within themselves.

```bash
# a bed for a crypt: dark and eerie, slow, long enough to loop
catalog query --feel Dark --feel Eerie --bpm-max 90 --min-length 3:00 --sort bpm

# the film-scoring shelf, chase tempo, nothing short
catalog query --category "Film Scoring Moods" --feel Suspenseful \
              --bpm-min 120 --min-length 2:00 --sort length --desc

# what has he added since the pack was picked?
catalog query --since 2025-01-01 --feel-any Dark --feel-any Eerie --sort uploaded
```

`--json` is the scriptable form: whole rows, URLs, and a finished `credit`
sentence on each one, so a row that leaves the database takes its attribution
with it. Text search is FTS5 over title and description where the interpreter
has FTS5 and a LIKE scan where it does not; a build says which it used.

Everything in the catalogue is CC BY 4.0 to Kevin MacLeod, and the `meta`
table carries the licence, the attribution and the credit wording alongside the
data.

## Playing it

Turn **sound** on in the top bar. It is off until someone asks for it, and the
first tap is what starts it — browsers make no sound without a gesture, which
is the same rule the narration lives under.

What happens then:

- **Two layers, as the cue table has them.** `music` and `ambience` are the
  bed: one plays at a time, and a cue taking the slot crossfades over whatever
  was in it at the fade times the manifest records. Stings, swells and effects
  are one-shots laid on top, three at once at most — over that the oldest gives
  way, because a burst of six is a noise rather than six sounds.
- **The bed ducks while a line is read**, whichever engine is reading it. The
  words are what a spectator is here for.
- **Cues fire when the page reveals an event, not when it arrives.** With the
  narrator running that is the beat the line is read in, so a sting lands with
  its sentence instead of minutes ahead of it while the queue drains.
- **A page opened mid-game is silent through the replay** and then picks up the
  bed from the newest event in the transcript that fires one — the music the
  fight is being fought to, without the stings of moments already gone.
- **The credits are on the page**, in the Score panel, for the whole pack
  rather than for whatever is playing. That is a licence condition, not a
  courtesy: see below.

A cue with no `match` rule (`music_explore`, the swells, `music_defeat`) is
picked but never fires, because nothing in the event stream says when it
should. They are the cues whose `when` is written for a human; giving them a
trigger is a decision about the game, not about this player.

Where the pack lives is `DND_AUDIO_DIR`, defaulting to `audio/` in the
checkout. A server without one answers `{"available": false}` and the page
simply has no Score panel, which is what every page had before this existed.

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

**Lengths are measured here, and sources lie about them.** incompetech's
catalogue gives "Cowboy Sting" as 8 seconds and ships 54; "Deep Noise" as 2 and
ships 149. The search window filters on the claimed length and the picker
auditions seven seconds, so a 150-second track can be chosen as a sting and
nothing notices until it plays over the table for two and a half minutes.
Normalising is the first point where the real length is known, so each entry
gains a measured `duration_s`, and one that does not fit its cue's window gets
a `duration_warning` in the manifest and a `WRONG LENGTH` line on the console.
A file that overruns its window is **cut to it**, at the front of the audio and
after the silence trim so it starts on the first sound, with the profile's
fade-out landing on the new end; the manifest records `trimmed_from_s` so the
cut is visible. `normalize --no-trim` keeps the whole file and leaves the
warning standing instead. Six MP3 frames of grace (0.15 s) decide both
questions, because a file cut to exactly its maximum re-encodes a few
hundredths over it and should not report itself as too long.

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
the credit block *and* `credit_text`, the finished sentence built from it.
Ordered like the cue table, so a diff between two runs reads.

`credit_text` is there so that whatever plays this never has to rebuild it. The
wording each source requires is decided in `fetch.credit_line`; a player that
derived its own would be the same licence rule in two places, and it is also
what keeps this tool off the runtime path — the page reads a manifest, not
`tools/`.

## Commands

| | |
|---|---|
| `harvest` | search every cue and write `candidates.json`, then rebuild the picker. `--group sting`, `--cues a,b`, `--required`, `--per-query N`, `--source freesound`. Re-running one cue keeps the others |
| `picker` | rebuild `picker.html` from an existing `candidates.json` |
| `fetch` | download a config. `--config -` reads stdin, `--dry-run` lists without downloading, `--force` re-downloads, `--allow by-nc` widens the licence gate |
| `verify` | re-hash every file against the manifest |
| `cues` | print the cue table; `--json` for the machine-readable form |
| `catalog build` | fetch incompetech's catalogue into `audio/incompetech.sqlite3`. `--db`, `--from-file`, `--no-check-lookups` |
| `catalog query` | filter it: `--feel` (AND), `--feel-any`, `--instrument`, `--genre`, `--collection`, `--category`, `--bpm-min/--bpm-max/--bpm-unknown`, `--min-length/--max-length`, `--since/--until`, `--text`, `--sort`, `--desc`, `--limit`, `--json` |

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

- **No mixing on the server.** Playback is the browser's: one `<audio>`
  element per sounding cue through a Web Audio gain node, per viewer, at that
  viewer's own volume. A server-side mixer would have to decide whose narration
  it was ducking under and would send the same stream to two spectators at
  different places in the same game, which is the one thing the playhead exists
  to allow.
- **No cue is chosen here.** `tools/audio` is a dev tool and stays outside the
  layering: nothing under `web/` or `orchestrator/` imports it, and the runtime
  path reads the manifest instead. A cue with no `match` rule still fires
  nothing, and giving it one is a decision about the game.
- **The playback knobs are honoured, not interpreted.** Levelling is applied to
  the file; `gain_db`, `loop`, the fades and the trim points are read off the
  manifest at runtime and applied as recorded. Nothing second-guesses them.
- **No original-quality Freesound downloads.** That needs the OAuth2 flow;
  previews are what is fetched.
