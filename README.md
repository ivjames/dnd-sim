# dnd-sim

A server that runs a D&D 5e game with **no humans at the table**. An LLM Dungeon
Master narrates and runs the monsters, one LLM player per party member decides
what its character does, and a deterministic Python engine adjudicates every
roll. You watch.

- **DM** — Sonnet: narration, monsters/NPCs, DCs, adjudication.
- **Players** — Haiku, one instance per PC, each with a persona and a real sheet.
- **Rules** — code. Dice, initiative, attacks, saves, conditions, spells,
  concentration, death saves, 5-ft grid movement. LLMs *propose*; the engine
  *validates and resolves*. The DM never decides a hit.
- **You** — spectator with knobs: pause/resume/stop, set party/setting/seed
  before the start, drop a DM note mid-game. No seat at the table.

Same config + seed + mock LLM ⇒ byte-identical game.

## Architecture

Strict one-way layering (full spec in [PLAN.md](PLAN.md), binding interfaces in
[CONTRACTS.md](CONTRACTS.md)):

```
web  →  orchestrator  →  agents  →  llm
                     ↘        ↘
                       engine  (pure, no I/O, no LLM)
```

| Path | What |
|---|---|
| `engine/` | Pure deterministic 5e rules + SRD data. No network, no threads. |
| `llm/` | Anthropic client, OpenAI-compatible client, per-seat router, `MockLLMClient`, token/cost ledger. |
| `agents/` | Prompt construction and output parsing for DM and players. |
| `orchestrator/` | Game loop, scenes, turns, memory, event bus, controls. |
| `tts/` | Amazon Polly: voice casting, SSML, the clip cache, and what a clip costs. |
| `web/` | Flask app, SQLite transcripts, SSE stream, static spectator UI. |
| `deploy/` | Fallback nginx vhost (used by `dndsim setup`); install notes point at DEPLOY.md. |

Everything the UI and the LLM layer see from a resolved turn is an `Event`
(`seq, round, kind, actor, text, data`). Events are appended to SQLite and
published on an in-process bus; the browser reads them over SSE.

## Run locally (mock mode — no API key, no cost)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DND_SIM_MOCK=1 .venv/bin/python -m web.app
```

Open <http://127.0.0.1:8071/>, hit **New game**, pick a preset, start.

Headless, same thing without the browser:

```sh
.venv/bin/python -m orchestrator.cli --config examples/goblin_ambush.json --mock --seed 42
```

Live mode (real API calls, real money):

```sh
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python -m web.app
```

Tests:

```sh
.venv/bin/python -m pytest -q            # everything
.venv/bin/python -m pytest web/tests -q  # web layer alone (uses fakes)
```

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `8071` | Listen port. |
| `HOST` | `127.0.0.1` | Bind address. Keep it loopback; nginx fronts it. |
| `ANTHROPIC_API_KEY` | — | Required for live mode whenever a seat names a `claude-*` model (the defaults do). On lab980, `dndsim deploy` copies it from `/etc/environment` into `.env`, which `run.sh` sources. |
| `OPENAI_API_KEY` | — | Needed only if a seat names a `gpt-*` model (OpenAI, `https://api.openai.com/v1`). |
| `XAI_API_KEY` | — | Needed only if a seat names a `grok-*` model (xAI, `https://api.x.ai/v1`). |
| `MISTRAL_API_KEY` | — | Needed only if a seat names a `mistral-*` / `ministral-*` / `magistral-*` / `codestral-*` model (`https://api.mistral.ai/v1`). |
| `GEMINI_API_KEY` | — | Needed only if a seat names a `gemini-*` model (Google's OpenAI-compatible endpoint, `https://generativelanguage.googleapis.com/v1beta/openai`). |
| `DEEPSEEK_API_KEY` | — | Needed only if a seat names a `deepseek-*` model (`https://api.deepseek.com`). |
| `SILICONFLOW_API_KEY` | — | Needed only if a seat names a `siliconflow:<id>` model (SiliconFlow's **international** platform, `https://api.siliconflow.com/v1`; a key minted on the China console `cloud.siliconflow.cn` belongs to `api.siliconflow.cn` and will not authenticate here). |
| `DEEPINFRA_API_KEY` | — | Needed only if a seat names a `deepinfra:<id>` model (`https://api.deepinfra.com/v1/openai`). |
| `DND_ALLOW_UNPRICED` | unset | `1` → let a live game start with a model that has no row in `llm/cost.py` `PRICES` (it is then charged at the default $2/$10 rate). Off by default: the budget stop is blind for an unpriced model, so game creation refuses. |
| `DND_SIM_MOCK` | unset | `1` → `MockLLMClient`, zero API calls. |
| `DND_SIM_DB` | `./data/dndsim.sqlite3` | SQLite transcript store. |
| `DND_SIM_EXAMPLES` | `./examples` | Where `/api/presets` reads scenarios from. |
| `DND_DM_MODEL` | `claude-sonnet-5` | DM model. |
| `DND_PLAYER_MODEL` | `claude-haiku-4-5-20251001` | Player model. |
| `DND_SUMMARY_MODEL` | = player model | Rolling-summary model. |
| `DND_SIM_LOGLEVEL` | `INFO` | Server log level. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` | — | Amazon Polly, which reads the game aloud. Read by boto3 in the ordinary way, so an instance profile works too. Without them the spectator's own browser speaks the game. |
| `DND_TTS` | unset (auto) | `0` → no server voices at all. `1` → on even for mock games, which otherwise stay free. |
| `DND_TTS_ENGINE` | `standard` | Polly engine: `standard`, `neural`, `long-form` or `generative`. Each is sent only the SSML it accepts, so the others work — but pitch and `vocal-tract-length` are standard-only, so on anything else two characters dealt the same voice cannot be told apart and a monster is only a voice rather than a big one. |
| `DND_TTS_LANG` | `en-US` | The language the voice pool is drawn from. |
| `DND_TTS_DM_VOICE` | `Brian` | The DM's voice; the rest of the table is dealt from the other voices. |
| `DND_TTS_CACHE` | `<dir of DND_SIM_DB>/tts` | Where synthesized clips are kept. |
| `DND_TTS_CACHE_MB` | `512` | Cache ceiling; least-recently-played clips go first. |
| `DND_TTS_MAX_CHARS` | `400` | Longest line the endpoint will synthesize. |

Any of the three `DND_*_MODEL` values, and a party member's per-seat `model`,
may name a model on any platform above — the platform is chosen from the
model id's prefix, or named outright with the `provider:model` form
(`deepinfra:Qwen/Qwen3-32B`), which is the only way to reach the two hosts.
Keys are read only for platforms actually seated.

## Seating other platforms

Every seat at the table — the DM, each player, the summarizer — can be served
by a different platform. `llm/providers.py` holds the table (one row per
platform: key variable, base URL, model-id prefixes); the `RouterClient`
picks the row by prefix, so nothing in a config names a provider, only a
model. Anthropic uses its native SDK; every other row goes through one
OpenAI-compatible `chat/completions` adapter.

Per-game models are `dm_model` / `player_model` / `summary_model` (or the env
vars). A party member may carry its own `"model"`, which overrides
`player_model` for that character only:

```json
{
  "dm_model": "claude-sonnet-5",
  "player_model": "claude-haiku-4-5-20251001",
  "party": [
    {"id": "pc_1", "name": "Thorin Cragmantle", "race": "Dwarf (Hill)", "klass": "Fighter", "level": 3,
     "gender": "male"},
    {"id": "pc_2", "name": "Vessa Quill", "race": "Halfling (Lightfoot)", "klass": "Rogue", "level": 3,
     "gender": "female", "model": "grok-4.3"},
    {"id": "pc_3", "name": "Sister Marigold Penn", "race": "Human", "klass": "Cleric", "level": 3,
     "gender": "female", "model": "gemini-2.5-flash"},
    {"id": "pc_4", "name": "Ilbrandt Ash", "race": "Elf (High)", "klass": "Wizard", "level": 3,
     "gender": "male", "model": "deepseek-v4-flash"}
  ]
}
```

`"gender"` is optional and decides which Polly voices that character can be
cast from (see [Spoken narration](#spoken-narration)). It is not a rules field:
the engine, the DM and the players never see it.

Two rows are **hosts** rather than platforms: SiliconFlow and DeepInfra serve
other people's models under namespaced ids (`deepseek-ai/DeepSeek-V3.2`,
`Qwen/Qwen3-32B`, `meta-llama/Llama-3.3-70B-Instruct-Turbo`) that say
nothing about which host is meant — and a bare `deepseek-` prefix already
means DeepSeek's own API. Those seats use the explicit form
**`provider:model`**: the part before the colon is a row name from
`PROVIDERS`, the part after is the host's model id verbatim (case matters),
and only that part goes on the wire. It works for every row and overrides
prefix routing (`deepseek:deepseek-v4-flash` is the same seat as
`deepseek-v4-flash`), but it is required for the hosts, which have no
prefixes: a namespaced id without one fails at game creation with a message
that names the form and the hosts that could serve it. One example per host:

```json
{"id": "pc_2", "name": "Vessa Quill", "race": "Halfling (Lightfoot)", "klass": "Rogue", "level": 3,
 "model": "siliconflow:Qwen/Qwen3-32B"},
{"id": "pc_3", "name": "Sister Marigold Penn", "race": "Human", "klass": "Cleric", "level": 3,
 "model": "deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo"}
```

The full `provider:model` string is the seat id everywhere — preflight
messages, the snapshot's `models`, the ledger row and the `PRICES` key (so the
same model is priced at each host's own rate). Priced today: on SiliconFlow
`deepseek-ai/DeepSeek-V3.2`, `deepseek-ai/DeepSeek-V3`, `Qwen/Qwen3-32B`,
`Qwen/Qwen3-14B`; on DeepInfra `deepseek-ai/DeepSeek-V3.2`, `Qwen/Qwen3-32B`,
`meta-llama/Llama-3.3-70B-Instruct-Turbo`. SiliconFlow's Qwen3 and
DeepSeek-V3.x seats send `enable_thinking: false` (thinking is on by default
there) and its JSON mode is left off the DeepSeek V3/R1 ids, which its docs
exclude; DeepInfra seats send plain sampling fields, since its documented
`reasoning_effort` control does not list these models.

In live mode, game creation checks every seat up front and refuses with one
message naming what is wrong: a model no row routes (a namespaced id with no
`provider:` is told which form to use), a platform whose key is
not set (the message names the variable), or a model with no price row (see
`DND_ALLOW_UNPRICED`). The ledger keeps one row per seat, priced at that
seat's model, so the budget stop works across platforms. Mock mode ignores all
of it — no keys, no prices, and a seated config produces the same transcript
as an unseated one for the same seed. The game snapshot reports the seating
under `models` (`dm`, `summary`, `players.<id>`).

What does not cross platforms: Anthropic prompt caching (the compat adapter
sends plain system text; cached-token counts a provider reports are still
priced), and the thinking controls — reasoning models on the other platforms
get their effort turned down per `llm/providers.py` `COMPAT_RULES` so hidden
reasoning does not eat the tight per-call token caps. Prices in `llm/cost.py`
were read from each platform's pricing page on 2026-09-03; re-check them when
seating a model for real money.

## HTTP API

```
GET  /                                  spectator UI
GET  /api/health                        {"ok":true,"mock":bool,"games_running":n}
GET  /api/presets                       [{name, description, config}]
POST /api/games          {config}       → 201 {"id","status"}  (creates + starts)
GET  /api/games                         [{id, status, created_at, title, round, cost_usd}]
GET  /api/games/<id>                    snapshot + config + ledger
GET  /api/games/<id>/events?after=seq   transcript from SQLite
GET  /api/games/<id>/stream?after=seq   SSE: replay then live, `event: end` on finish
POST /api/games/<id>/pause|resume|stop  → 202
POST /api/games/<id>/note  {"text"}     → 202  (DM note from the table)
POST /api/games/<id>/hold  {"seconds","client"} → 202 {"holding": granted}
GET  /api/tts                           {"available":bool, engine, language, max_chars, price_per_million_chars, config}
GET  /api/games/<id>/tts?key=&text=&v=  audio/mpeg — one narrated line, cached and charged
```

The stream sends `id: <seq>` on every message, so an `EventSource` reconnect
resumes from `Last-Event-ID` instead of replaying the whole game. Heartbeat
comments go out every 15s.

## Cost control

Each game config carries `budget_usd`. The orchestrator tracks real token spend
per role and halts the game at `budget_exceeded`. The UI shows spend against
budget live. Prompts are built for frugality: players see a compact state view
plus enumerated legal actions (not full state), the static rules digest is
prompt-cached, and summaries are written by the cheap model.

## Spoken narration

The spectator page can read the game aloud. Tick **voice** in the top bar (the
tick is the tap that browsers require before a page may speak) and the DM's
narration, scene openings, the epilogue, every line of dialogue and any DM note
from the table are spoken. Mechanics are spoken too, but shaped into a short
line ("Goblin 2 hits Thorin for 6", "Round 3", "Vessa moves 30 feet") rather
than the dice string, and **mute mechanics** silences them entirely. The DM has
one voice; each PC gets its own, dealt deterministically so the same character
sounds the same every session.

A party member may state a `"gender"` — `female` or `male` — and is then dealt
only from the voices Polly reports as that gender. It narrows who can be cast
and nothing else: the choice within that set is the same hash as before, so a
character keeps its voice for as long as its gender and the roster hold. The
gender is read from the game's own party list, never from the request that asks
for the clip — this endpoint spends money, and a gender in the query string
would be a way to walk the whole roster a paid clip at a time.

Polly's roster is `Female` and `Male`; there is no third kind of voice on it.
So a character who states neither, or states nothing at all, is dealt from the
**whole** pool rather than pushed into one of the two — the roster's limitation
is not something to launder into a character sheet. `Crick` in
`examples/crypt.json` is left that way on purpose. Where a language ships
voices of only one gender (Korean and Swedish each ship one), a stated gender
that cannot be answered gets a voice anyway: a worse match, not a silence.

**The browser fallback ignores gender.** `SpeechSynthesisVoice` has no gender
attribute in any browser, and inferring one from voice names across every OS
and locale would be a guess dressed as data. A session on the fallback engine
casts as it always did.

### Two engines, one narrator

The voices come from **Amazon Polly**, rendered on the server and played as
audio. Where the server has no Polly — no AWS credentials, a mock game, the
game's budget spent — the page reads the line with the **browser's own**
`speechSynthesis` instead, which is what it always did. The narration panel
says which one is speaking.

The fallback is per line, not per session: a single refused clip is spoken by
the browser and the next line asks Polly again. Three failures running, or one
settled refusal (no budget, a mock game, a server with no Polly at all), and the
page stops asking until the game changes. Nothing above that decision changes —
same playhead, same transport, same holds — so a session can cross between the
two without the listener losing their place.

What each engine does with a monster differs, because the material does. A
monster that can speak — one whose SRD stat block names a language it uses
aloud, so a goblin or an ogre but not a wolf or a zombie — gets a voice of its
own either way. In the browser it is cast from the **novelty** voices the OS
ships beside the real ones (Bubbles, Trinoids, Zarvox and the rest, on Apple
devices); Polly has no such thing, so there it is an ordinary voice put through
`<amazon:effect vocal-tract-length>` — a longer vocal tract is a bigger
creature — with pitch and rate behind it. Nobody else is ever treated: not the
DM, not a PC, not an NPC that isn't a monster.

### What it costs

Polly's standard engine is **$4.00 per million characters**, and that spend is
charged to the game's own `budget_usd` alongside the model calls — it shows up
as `by_role.tts` in the ledger, and a game that has spent its budget goes back
to the browser's voices rather than quietly spending more. A whole game's
narration is a few cents.

A game that states no `budget_usd` gets `GameConfig`'s default rather than a
blank cheque, and a zero or negative budget refuses everything — the
orchestrator halts at `total_usd >= budget_usd`, so zero is a game already over
rather than a game with no ceiling.

A clip about to be synthesized **holds its own cost against the game** until it
is charged or abandoned, so eight spectators asking for eight different lines
at the same moment cannot each read the same below-budget total and each go to
Polly. A clip that would take the game over is refused before the call, which
is stricter than the model-call check (that one stops the game once it already
has) — erring toward stopping is the house style. A clip **already paid for is
served whatever the budget says**: the budget governs spend, and re-reading a
line is not spend, so a game that has run out of money stays listenable to the
end of its transcript.

Every clip is cached on disk under `data/tts`, keyed by the voice and the exact
SSML document sent, so a line is paid for once however many times it is replayed — which matters,
because the playhead is designed to be run backwards. Deleting the cache is
safe and costs only the re-synthesis. Clip URLs carry the probe's `config`
fingerprint, so changing the engine, the language or the DM's voice retires the
copies in every browser rather than leaving them to be replayed for a year.
`DND_TTS=0` switches server voices off entirely; mock games never use them unless `DND_TTS=1` says so, because mock
mode is the mode that costs nothing.

### The playhead

Narration is a **playhead over the transcript**, not a queue of things to say:
`V.cursor` indexes every event the page has seen, and the narrator reads
forward from it. Nothing is dropped and nothing is skipped behind your back.
The consequences are the point:

- **Pause leaves a mark.** ⏸ stops mid-line and the playhead stays on that
  line; ▶ picks it up where it stopped. So does backgrounding the tab —
  synthesis is suspended there and comes back garbled, so the narrator stops,
  but it stops *in place*. Coming back resumes the line you were on rather
  than jumping to whatever the game reached meanwhile.
- **You can go back.** ⏮ re-reads the line before, ⏭ drops the current one,
  **live** jumps to the newest, and clicking any line in the transcript starts
  reading from there.
- **It survives a reload.** The playhead is saved per game in `localStorage`,
  so reopening the page offers the line you were on, not the top of the game.
- **A "N behind" badge** in the top bar says how far the narrator is from the
  live edge, and doubles as the jump-to-live button. While the narrator is
  behind, **follow** follows the narrator rather than the tail — otherwise it
  would scroll away from the line you can hear.
- **The transport outlives the game.** A finished, stopped or budget-exhausted
  game still has a full transcript, so play/back/skip keep working on it; only
  the game controls (pause/resume/stop) go dead, and they say why.

### Holding the game for the narrator

Text arrives far faster than a voice can speak it, so left alone the game runs
minutes ahead of what you are hearing. **hold the game for the narrator** (on
by default) fixes that from the listening end: while the narrator is more than
a few lines behind, the page asks the game to wait.

That ask is `POST /api/games/<id>/hold {"seconds": n, "client": id}` — a
renewable *lease*, not a pause. It expires by itself, so a tab that is closed
mid-hold costs the game a few seconds rather than freezing it for good; the
page renews it on a 4 s heartbeat while it is behind and drops it the moment it
catches up, when playback is paused, when the tab goes to the background, or
when the game ends. It deliberately leaves `status` alone: holding is the
narration keeping step, the table's own pause is something else, and the two
stay separately controllable.

The checkbox is a *setting*, and the UI keeps that apart from whether anything
is being held right now. It is disabled where the option cannot apply to this
game at all (no speech synthesis, a game that has ended, one another process is
running); it is merely dimmed, and still changeable, where it is on but idle —
voice off, playback paused, the tab in the background — because the preference
still means something the moment you press play. Its tooltip says which. The
"N behind · holding" badge claims a hold only once the game has confirmed the
lease, never on the strength of a request the page has merely sent.

Leases are per spectator, and the game waits for the longest one outstanding.
One shared deadline would make every spectator the last writer of everyone
else's: a second tab catching up and releasing would cut short a first tab that
is still behind, and a renewal in flight when another released would put the
hold back on. Whoever is furthest behind sets the pace, which is what a shared
table means. The id is per *tab*, kept in `sessionStorage`, so a reload renews
its own lease rather than stranding one to expire.

The other half of the fix is upstream — the DM's word ceilings
(`agents/dm.py`) are a *listening* budget, roughly 150 words a minute of
speech, not just a token budget.

Which words are said, and by whom, is decided in one place for both engines:
`web/static/speech.js`, a dependency-free module that `node` can exercise
directly. It turns an event into a phrase and a **voice key** (`dm`, a party
member's id, `npc`, `monster:<id>`); the browser either speaks that itself or
sends the key and the words to `GET /api/games/<id>/tts`, which deals the key a
Polly voice — the same FNV-1a hash on both sides, so an actor keeps its seat.
Casting, SSML and the cache are in `tts/`; the playhead, the transport and both
sets of playback calls are in `app.js`.

A tab in the background still stops the narrator. Server audio would play there
perfectly well, but a tab nobody is looking at would go on holding the game for
a narrator nobody is listening to, so the rule stays until that is decided
separately.

## Deployment

See [DEPLOY.md](DEPLOY.md). Short version: on the lab980 droplet, `git clone`
to `/var/www/dndsim`, symlink `bin/dndsim` onto PATH, and `dndsim deploy` does
the rest — venv, `.env` (key adopted from `/etc/environment`), vhost with the
SSE block (`proxy_buffering off`; the stream dies without it), pm2 process
`dnd-sim` on 127.0.0.1:8071 — idempotently, every time.

## Attribution and licence

Game content is from the **System Reference Document 5.1 ("SRD 5.1") by Wizards
of the Coast LLC**, licensed under the **Creative Commons Attribution 4.0
International License** (<https://creativecommons.org/licenses/by/4.0/legal>).
The required attribution text ships with the data in
`engine/data/LICENSE-SRD.txt`.

No non-SRD Wizards content is included, and none should be added: keep monsters,
spells, classes, races and magic items to the SRD.

This is a hobby simulator. It is not affiliated with or endorsed by Wizards of
the Coast.
