# dnd-sim — working notes

An LLM-run D&D 5e game with no humans at the table: a Sonnet DM, Haiku
players, a deterministic Python rules engine, and a spectator web UI over SSE.
(`dnd-sim` is the working name; the product name is undecided — see PLAN.md.)

Served at **https://dndsim.lab980.com** from the lab980 droplet.

How work lands here — branch, PR, and the fact that merging is not deploying —
is in `.claude/rules/lab980-conventions.md`, which Claude Code loads
automatically every session. That file is owned by the lab980 scaffold and is
overwritten by it; **this** file is the site's own, and everything below is
about this site rather than about the platform. For the box itself, read the
`ivjames/lab980.com` repo's `CLAUDE.md`.

## Decisions

This repo's own runbook and the lab980 conventions used to disagree on three
points. All three are decided (2026-09-03) and the files agree: the checkout
dir is **`/var/www/dndsim`** (always `/var/www/<stub>` on this box), the port
is **8071** (first free in the 8060+ range on the droplet), and the keys live
in **`/var/www/dndsim/.env`** and nowhere else on the box — revised
2026-09-05: there is **no box-level key store**; `/etc/environment` holds no
API key and `dndsim deploy` copies nothing from anywhere. A key goes into
`.env` with an editor (never `echo` on a command line), `run.sh` sources the
file on every start, and pm2 is launched under `env -i` with an allowlist
(PATH, HOME, PM2_HOME, TERM, LANG) so its dump never carries a key — known or
otherwise. There is no hand runbook: `deploy/INSTALL.md` is a
pointer, `DEPLOY.md` is the doc, and `bin/dndsim` does the work. Don't
reopen these in a code change; if one has to change, make it a decision.

## Shape

A **proxied app**: nginx fronts a pm2-managed **Python 3.11 / Flask** process
on `127.0.0.1:8071`. Not Node — there is no `package.json`, no `npm ci`, no
build. The install is `python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt`, and `requirements.txt` is deliberately tiny (Flask, httpx,
anthropic, boto3, pytest; ranges, not pins — no Pydantic, per CONTRACTS.md).
boto3 is there for one thing, Amazon Polly narration, and narration falls back
to the browser's own voices without it — so it is the one dependency an install
can lack and still run.

- Repo: `ivjames/dnd-sim` · droplet dir: `/var/www/dndsim`
- pm2 process: **`dnd-sim`** (from `ecosystem.config.js`; note it is not the
  stub `dndsim`) — runs `./run.sh`, which execs `.venv/bin/python -m web.app`.
  **fork mode**, `exec_mode: 'fork'` explicit, and `instances: 1` is a hard
  ceiling, not a default: games run as threads inside the web process and SSE
  subscribers attach to its in-memory event bus, so a second instance would
  answer half the requests from a process that knows nothing about the game.
  Never raise it.
- **SSE.** The spectator UI reads `/api/games/<id>/stream`. The vhost must
  carry `proxy_buffering off`, `proxy_cache off`, hour-long
  `proxy_read_timeout`/`proxy_send_timeout`, and `gzip off` (the reference block
  is `deploy/nginx-dndsim.conf`), or the browser sees nothing until the game
  ends. `provision-site`'s generic vhost says the opposite, so `dndsim
  setup` rewrites the proxied location with a marker-tagged block and
  repairs it on every run; `dndsim status` says whether it is present.
- Config is process environment, loaded from `./.env` by `run.sh` (`set -a;
  . ./.env; set +a`, only if the file exists) before it execs python — so
  pm2-managed and hand runs read the same file. Keys: `PORT`, `HOST`, the
  platform API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `XAI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, and for the two
  inference hosts `SILICONFLOW_API_KEY` and `DEEPINFRA_API_KEY`, whose seats
  are written `siliconflow:<id>` / `deepinfra:<id>` — any subset; only the
  Anthropic one is warned about, since the default DM needs it, and a seat
  whose platform has no key fails at game creation naming the variable;
  `CARTESIA_API_KEY` on the droplet is a text-to-speech key this app does not
  use and is not one of these), the AWS trio `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` that Polly narration reads through
  boto3, `DND_SIM_MOCK`, `DND_SIM_DB`, the `DND_*_MODEL` overrides and the
  `DND_TTS*` narration knobs (full table in README and `DEPLOY.md`). The app itself reads `os.environ` only; there is
  no `python-dotenv`. `dndsim deploy` writes `.env` (mode 600), seeds `PORT`,
  `HOST` and `DND_SIM_DB`, and reports which known keys it holds (names
  only); it copies no key in from anywhere — the keys are put there by hand,
  one `KEY=value` line each, with an editor. The list is `KNOWN_KEYS` in
  `bin/dndsim`, overridable with `DNDSIM_KEYS`; `DND_WRITE_TOKEN` is the one
  `dndsim token` generates rather than one you type; pm2's launch does not
  consult the list at all — it drops the whole environment. `dndsim keys`
  prints which known keys `.env` holds (names only) and exits 1
  without `ANTHROPIC_API_KEY`. State
  is SQLite at `data/dndsim.sqlite3`; `data/`, `.env` and `.venv/` are
  gitignored and survive a deploy's hard reset.
- vhost: `/etc/nginx/sites-available/dndsim.lab980.com`, written by
  `provision-site` (via `dndsim setup`) or, without it, from
  `deploy/nginx-dndsim.conf`. `dndsim setup` keeps the marker-tagged SSE
  block in it either way.

## Deploying

On the droplet, as root. First time:

```bash
git clone https://github.com/ivjames/dnd-sim /var/www/dndsim \
  && ln -sf /var/www/dndsim/bin/dndsim /usr/local/bin/dndsim \
  && dndsim deploy                     # seeds .env; warns that no key is in it yet
${EDITOR:-nano} /var/www/dndsim/.env   # ANTHROPIC_API_KEY=sk-ant-... and any others, one per line
dndsim restart && dndsim token
```

Every time after:

```bash
dndsim deploy      # reset to origin/main, venv + pip, .env (seed, report keys), vhost via
                   # setup if missing, SSE block repaired, pm2 start/restart (env -i
                   # allowlist), save if online, probe; exit 1 unless local health is 200
dndsim setup       # once, idempotent: provision-site (or HTTP-only fallback vhost),
                   # SSE block in the vhost, pm2-root check; --dry-run shows the diff
dndsim status      # HEAD, pm2, .env + per-key presence, upstream family, vhost + SSE
                   # block, local + public /api/health, cert days
dndsim keys        # per-key present/absent in .env (names only); exit 1 if
                   # ANTHROPIC_API_KEY is missing from it
dndsim token       # set the write token and use it: generate → .env → restart →
                   # verify against the running app → print. --show reads it back,
                   # --stdin takes your own. Like every key, it lives in .env only.
dndsim logs        # tail this app's pm2 logs
```

A restart kills any in-flight game (the app marks it `stopped` on boot; the
transcript stays readable) — deploy between games if one matters. What each
step does, the env keys, and how to confirm what is live: `DEPLOY.md`.

## Things worth knowing

- **Mock mode costs nothing.** `DND_SIM_MOCK=1` swaps in `MockLLMClient`: no
  key, no API calls, and same config + seed ⇒ byte-identical game. Run locally
  and test that way; `.venv/bin/python -m orchestrator.cli --config
  examples/goblin_ambush.json --mock --seed 42` is the headless integration
  test.
- **Narration is Polly, and it is also real money.** The spectator page asks
  `/api/games/<id>/tts` for each line and plays the audio; where the server has
  no Polly (no AWS credentials, a mock game, the budget spent) the page speaks
  it with the browser's own `speechSynthesis`, which is what it did before and
  is still the fallback for a single failed line. $16/1M characters on neural —
  the table's engine, and the monsters' too since the treatment below — against
  $4/1M on standard, which is where a monster used to be held.
  Charged to the game's `budget_usd` as `by_role.narrator`, every clip
  cached in `data/tts` so a line is paid for once, and stopped at the lower of
  the game's `budget_usd` and the server-owned `DND_TTS_MAX_USD` (default $10),
  because the config's budget is whatever the caller asked for. That caps one
  game; how many games can be started is capped by `DND_WRITE_TOKEN` — see
  **Write access** below. `DND_TTS=0` turns it off; mock games never
  touch it unless `DND_TTS=1`. `TTS-COSTS.md` is the costing this came from —
  its §6 records what Polly changed and what is still open.
  **A speaking monster is made after Polly, not by it** (`tts/dsp.py`): its
  clip is asked for as `pcm`, played back at a different sample rate — pitch
  and formants together, which is a bigger creature — with grit or a stone
  room dealt to some of them, and served as `audio/wav`. **How big it sounds is
  its stat block's `size`**, one band of `MONSTER_SIZE_BANDS` each, because the
  voice key is `monster:mon_6` and `mon_6` is spawn order: dealt from the key
  alone, an Ogre came out smaller than the Gnoll it walked in behind. The band
  is the creature's and the value within it is the hash's, so two gnolls still
  differ; `web/routes/tts.py: _creature_size_for` reads the size off the live
  `Combatant` (or the persisted snapshot, which has always carried it) rather
  than off the query string, for the reason `_member_for` gives. The duration that
  shift would cost is bought back with `<prosody rate>`, and the level the grit
  would add is taken back off, so a monster is a different voice rather than a
  slower or louder one. That is why monsters are no longer held on the standard
  engine: `<amazon:effect vocal-tract-length>` was the only vendor way to do it
  and exists nowhere else, and the price was every monster line being read by
  the flatter engine. `DND_TTS_MONSTER_FX=0` puts that arrangement back whole,
  and is the way out if the treatment sounds worse. Either way the monster
  seats are the only ones asking Polly for something no other seat asks for, so
  a working table proves nothing about them and a refused monster line is a 502
  the page hides by speaking that line itself:
  `tests/tts/test_polly_contract.py` holds every document **both** arrangements
  can emit against Amazon's published matrix, `tests/tts/test_dsp.py` the
  arithmetic of the treatment, and `python -m tools.polly_check` sends two real
  lines from the droplet, with `.env` sourced first the way `run.sh` does
  (`--dry-run` anywhere else; `--ab --out DIR` renders the monster line both
  ways to listen to, which is the only way to judge it).
  **The treatment is seventeen knobs now, and fourteen of them nothing deals.**
  `EFFECT_CHAIN` in `tts/dsp.py` is the list and the order a signal chain wants
  them in — sub-octave and breath, then the nonlinearities, the filters,
  modulation, the room, and the two that chop the level last — and `FIELDS` is
  the bounds, read by the clamp, by the cache token and by the lab's sliders
  rather than by a second list that would drift. What the casting deals has not
  moved: `voices.py` still gives a monster `size_pct` from its SRD size band and
  grit and a room from the hash, so no monster on the site sounds different than
  it did. The other fourteen are reachable only through a `Tune` — the voice lab
  — deliberately, because what a gnoll or an ooze or a swarm should sound like
  is settled by ear before it is written into the casting; **which creature
  types get dealt which of them is the next decision and is not made.**
  `formant_pct` is the one worth naming: a single peaking bell, a cue rather
  than a transform, and the only knob here that moves an emphasis without moving
  pitch with it — which is the axis `vocal-tract-length` owned, bought back in
  part. **Editing `dsp.py` retires every monster clip on disk**, because
  `MonsterFX.token()` folds in a hash of the module's own source: a changed
  curve must not go on being served from a key that still describes it
  correctly. The dealt monsters re-render identically — same three knobs, same
  arithmetic — but they do re-render, and Polly charges the neural rate for each
  again; a one-off, bounded by dialogue, costed in `TTS-COSTS.md` §6. The token
  spells only what is dealt (`fx:size=20,growl=55:<fp>`) instead of joining
  every field positionally, so adding a knob nobody turned on moves no key by
  itself.
  **A monster is dealt a half of the roster, and a character never is.** A
  gnoll has no party spec to state pronouns in, so it used to be dealt from the
  whole roster — which is not neutral, because Polly's English roster is about
  two women to every man, and the pool a monster is dealt from is worse than
  the roster: the children and the DM's own voice come out of it before the
  deal, leaving nine to three on the built-in roster. Over the two
  or three creatures a fight gives lines to, that was heard as every monster in
  the game being the same woman with grit on her. `MONSTER_GENDERS` in
  `tts/voices.py` deals the half off the key's own hash and the voice is picked
  within it, so a seat still keeps its voice for as long as its id does. The
  hash is a second one over a salted key **and** is finalized through `_mix32`,
  neither of which is decoration: FNV-1a over ids differing in their last
  character leaves bits 14-23 and 28-31 identical across `monster:mon_1` …
  `monster:mon_9`, so a slice up there deals every monster in a game the same
  half — and its low bit is the parity of the key's characters, so a slice down
  there alternates the halves in lockstep with spawn order. A test fails on
  either one coming back. The same arithmetic is why `MONSTER_TEMPO` reads
  `(h >> 16) % 4` and every creature in a game talks at 90% — a real defect,
  knowingly left alone, written down in `TTS-COSTS.md` §6 with what fixing it
  costs. Changing which voice a creature is dealt retires its cached clips
  exactly as editing `dsp.py` does, and only its: a DM, PC or NPC cast is
  untouched.
  **A party spec states `pronouns`, not a gender.** `he/him` narrows the pool
  to Polly's male voices, `she/her` to its female ones, and `they/them` — or
  any other set, or nothing said — is dealt from the whole roster, because the
  roster is two voices deep and its limitation is not something to write into
  someone's character sheet. `gender_for_pronouns` in `tts/voices.py` is the
  only place that mapping happens and it runs one way; the older `gender` key
  is still read where a config states it alone. The New game panel has a
  pronoun row per seat, which a gender picker could never have been. The same
  key is what the DM is told to call the character (`pronouns_for` in
  `agents/views.py`, the COMBATANTS column), so the pronouns a character is
  narrated in and the voice it is spoken in cannot disagree — it is carried on
  `CharacterSheet` for that, and read by no rule.
  **Every seat is cast as an adult unless its party spec says otherwise.**
  Polly's roster carries three children's voices (`Ivy`, `Justin`, `Kevin`) and
  they used to be dealt to anyone, which is how a cleric called Father Bexley
  ended up sounding nine; `CHILD_VOICE_IDS` in `tts/voices.py` keeps them for
  characters whose spec states a child `age`, and the New game panel has a row
  per seat to say so. `DescribeVoices` reports no age, so that list is
  transcribed from Amazon's voice list and held to it by
  `tests/tts/test_polly_contract.py` — the only thing that can catch it going
  stale.
- **The screen waits for the voice.** With narration on, arriving events queue
  in the page and reach the screen — transcript, map, hit points — in the beat
  the narrator *begins* the line that describes them, a whole turn at a time
  where its mechanics are not themselves spoken. The game runs on into that
  queue; the hold is what stops the queue growing without limit, and the two
  are separate mechanisms. The board is the part that could not be delayed
  client-side, so `GET /api/games/<id>?at_seq=<seq>` serves it from a bounded
  per-event archive in `web/registry.py` while status and cost stay live. The
  rule for how much may be revealed at once is `revealRun` in `speech.js`
  (pure, node-tested); the queue and the DOM are `app.js`. Voice off, or before
  the first tap, the gate is open and the page behaves as it always did.
- **Writes take a token; reads never do.** `POST /api/games`, `/note`,
  `/pause`, `/resume` and `/stop` require `X-Dnd-Token` to match
  `DND_WRITE_TOKEN` (`web/auth.py`). Everything a spectator does stays
  anonymous — reading a game, listing games, the SSE stream, `/api/tts`, the
  paid narration endpoint and the narration hold — because the public
  spectator UI is the product. **Unset, the token fails closed**: writes answer
  503 and everything else works, so a deploy that forgets it does not take the
  site down but nobody can start a game until it is set. **`dndsim token`** is
  how it is set — generate into `.env`, restart, check it against the running
  app, print it to paste into the page — and rotation is the same command
  again. Like every key it lives in `.env` and nowhere else; unlike a vendor
  key it is generated rather than typed, so nobody has to see it. It is on
  `KNOWN_KEYS` so `keys`/`status` report it.
  The page keeps it in `localStorage` and hides the New game button, the
  pause/resume/stop row and the note form until the server accepts it.
- **Live mode is real money.** Each game config carries `budget_usd`; the
  orchestrator tracks spend per role and halts the game at `budget_exceeded`.
  Prompts are built for frugality (compact state views, enumerated legal
  actions, prompt-cached rules digest, summaries by the cheap model) — keep
  them that way.
- **Layering is strict and one-way**: `web → orchestrator → agents → llm`,
  with `engine/` pure (no I/O, no LLM, no threads) underneath. `tts/` hangs off
  `web` alone and imports none of the others — it is a priced outside service
  like `llm/`, not a layer under it. `CONTRACTS.md`
  is the binding interface spec; a contract change is recorded under its
  "Amendments" section with rationale, not made silently. Game content is
  SRD 5.1 (CC-BY-4.0) only — no non-SRD Wizards content, ever.
- Tests: `.venv/bin/python -m pytest -q` (everything);
  `.venv/bin/python -m pytest web/tests -q` for the web layer alone (fakes).
- Verify a **clean** clone installs and passes, not just the working tree:

  ```bash
  d=$(mktemp -d) \
    && git archive HEAD | tar -x -C "$d" \
    && ( cd "$d" && python3 -m venv .venv \
         && .venv/bin/pip install -q -r requirements.txt \
         && .venv/bin/python -m pytest -q ) \
    && rm -rf "$d"
  ```

  `mktemp -d` is the point, not tidiness. The directory has to exist — `tar -x
  -C` into a missing one fails outright — and it has to be *empty*, or the
  extract merges over an earlier run's files and the check quietly stops
  testing a clean tree. A fixed `/tmp/x` gets both wrong, and on a shared
  `/tmp` it lets two runs race, each able to delete the other's tree
  mid-build. The subshell keeps you in the checkout; the trailing `rm -rf`
  fires only on success, leaving a failed run in `$d` to look at. A
  kitchen-sink `.gitignore` eating a source dir is the classic thing this
  catches (`data/` is ignored here on purpose; `engine/data/` is not, and must
  stay tracked — `git ls-files engine/data` confirms).
- **Scenarios are `examples/*.json` and the web panel offers all of them**, so a
  file dropped in there is a shipped scenario. `budget_usd` in each is sized
  from a full mock run of that file (`--mock --tempo 0 --budget 20` prints the
  total) at **about four times the mock total** — the budget is a stop, not an
  estimate, and a game that hits it halts mid-scene. Four times, because the
  mock now accounts for prompt caching the way the API does while its outputs
  are one-line canned strings against a live DM's ~90 tokens a call, so live
  LLM spend ran 2–2.5× the mock total across the first sixteen live games
  (`CONTRACTS.md`, 2026-09-05 llm/ amendment). Grid bounds, `scene_<i>` triggers, party size and
  monster names are checked for every file by the suite, so a broken scenario
  fails `pytest` rather than a live game.
- **Players sample at `player_temperature` (default 1.0), not 0.8.** Per game,
  per seat (`temperature` in a party spec), or from the panel's Improv field;
  clamped to `[0, 1]` because that is Anthropic's ceiling. The DM stays at 0.8
  — it owns world facts — and the summarizer at 0.3.
- **Audio is sourced by a dev tool and played by the page.** `tools/audio/`
  (docs: `AUDIO.md`) is the tool — cue table, library search, a self-contained
  picker page, a fetcher that writes `audio/manifest.json` + `CREDITS.md`. It
  sits outside the layering and nothing on the runtime path imports it: the page
  reads the manifest instead, served by `web/routes/audio.py` at `GET /api/audio`
  with the files under `/audio/...` (the manifest is the allowlist; `config.json`
  and `CREDITS.md` are not served). `web/static/cues.js` is `cues.py`'s match
  rules transliterated, and `web/tests/test_cues_js.py` drives both over the same
  events so the two cannot drift. `DND_AUDIO_DIR` moves the pack; without one
  the page has no Score panel. `fetch` levels what it downloads
  where **ffmpeg** is on PATH (beds to -16 LUFS, one-shots trimmed and peaked to
  -0.7 dBFS, both re-encoded); ffmpeg is not a dependency and its absence only
  means the files are kept as downloaded. The picked audio, manifest and
  credits are **committed** (a deploy hard-resets the checkout, so untracked
  files would not survive); only `audio/candidates.json` and `audio/picker.html`
  are ignored, both re-made by one `harvest`. The cue
  table is held to `engine.events.EVENT_KINDS` by `tests/audio`, so a new event
  kind fails the suite until someone decides whether it makes a noise. Only
  public-domain and attribution licences are accepted, and CC BY credits are an
  obligation that has to reach the page before any of it ships.
- pm2 process name is `dnd-sim`; `dndsim logs` tails it.
