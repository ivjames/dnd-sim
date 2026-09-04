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

## Scenarios

`examples/*.json` is the whole scenario set; the new-game panel offers every
file in that directory (`DND_SIM_EXAMPLES` moves it), and the CLI takes one by
path. All of them are SRD 5.1 content only.

| File | Party | Shape | What it exercises |
|---|---|---|---|
| `cellar_rats.json` | level 1 | one scene, one fight | The cheapest live smoke test there is: giant rats and two kobolds in a fish cellar. |
| `spider_mine.json` | level 2 | two scenes, two fights | Kobold pack tactics underground, then giant spiders, webs, and a lot of difficult terrain. |
| `goblin_ambush.json` | level 3 | two scenes, one fight | The reference game: goblins and a boss on an open road. |
| `tollhouse.json` | level 3 | three scenes, one fight | Talk first: two full social scenes before anyone draws, and a fight that arrives when the talking runs out. |
| `gnoll_pyre.json` | level 4 | two scenes, two fights | Gnoll Rampage and worg speed on open ground, then an ogre at a fire. |
| `crypt.json` | level 5 | two scenes, two fights | Undead: turn undead, ghoul paralysis, a party that can be locked down. |
| `troll_fen.json` | level 5 | two scenes, two fights | A troll. Regeneration stops for a round on fire or acid damage, so the wizard's slots decide the fight. |

Each carries a `budget_usd` sized from a full mock run of that scenario with
headroom, because the budget is a stop, not an estimate: a game that exceeds it
halts mid-scene. A mock run is the cheap way to re-check one after editing:

```sh
.venv/bin/python -m orchestrator.cli --config examples/troll_fen.json --mock --tempo 0
```

Writing a new one: copy the nearest file. `scenario.scenes[i]` and
`scenario.encounters[].trigger: "scene_<i>"` line up by index — and that
encounter **always** runs, after that scene's beats, however the talking went;
there is no conditional trigger, so a scenario cannot offer a fight the party
can talk its way out of (the DM can start combat *earlier* by adjudicating
`start_combat`, never later). `grid` coordinates must sit inside
`width`/`height` and off the walls, and every
monster name must resolve in `engine/data/monsters.json` (29 of them, CR ⅛–5).
The test suite checks all of that for every file in `examples/`, so a broken
scenario fails `pytest`, not a live game.

## How improvised the players are

Player seats sample at **`player_temperature`** (default `1.0`, the top of the
Anthropic range). At the old 0.8 a character would converge — the same opening
line, the same attack, every round — and the transcript's repetition guard then
dropped the repeat, so the character went quiet instead of saying something
new. Set it per game in the config, per seat in a party spec, or from the
**Improv (0–1)** field in the new-game panel:

```json
{
  "player_temperature": 1.0,
  "party": [
    {"id": "pc_1", "name": "Thorin", "temperature": 0.6, "...": "..."}
  ]
}
```

Out-of-range and unreadable values are clamped to `[0, 1]` rather than
rejected. The DM and the summarizer are deliberately not affected: the DM owns
world facts and runs at 0.8, the summarizer at 0.3.

The other half of this is the player prompt, which asks for reaction over
routine — vary the action when the fight has changed, hold your own read of it,
never reuse a line. What improvisation is scoped to is unchanged: motive,
voice, and which legal action to take. Dice, outcomes and the contents of the
world stay the engine's and the DM's.

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
| `DND_TTS_ENGINE` | `neural` | Polly engine for the table: `standard`, `neural`, `long-form` or `generative`. Each is sent only the SSML it accepts. |
| `DND_TTS_MONSTER_ENGINE` | `standard` | Engine for speaking monsters, which is separate because `vocal-tract-length` is standard-only. Set it equal to `DND_TTS_ENGINE` to put the whole table on one engine. |
| `DND_TTS_LANG` | `en-US` | The language the voice pool is drawn from. |
| `DND_TTS_DM_VOICE` | `Brian` | The DM's voice; the rest of the table is dealt from the other voices. |
| `DND_TTS_CACHE` | `<dir of DND_SIM_DB>/tts` | Where synthesized clips are kept. |
| `DND_TTS_CACHE_MB` | `512` | Cache ceiling; least-recently-played clips go first. |
| `DND_TTS_MAX_CHARS` | `400` | Longest line the endpoint will synthesize. |
| `DND_TTS_MAX_USD` | `10.00` | Server-owned ceiling on a game's spend before narration stops, whatever `budget_usd` says. It caps one game; `DND_WRITE_TOKEN` caps how many can be started. |
| `DND_WRITE_TOKEN` | unset | The shared secret the write routes require in an `X-Dnd-Token` header: `POST /api/games`, `/note`, `/pause`, `/resume`, `/stop`. **Unset → those routes answer 503**; reading a game, listing games, the SSE stream, `/api/tts` and the narration hold are anonymous either way. On the droplet, `dndsim token` generates one into `.env` and restarts; locally, any long random string. |

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
     "pronouns": "he/him"},
    {"id": "pc_2", "name": "Vessa Quill", "race": "Halfling (Lightfoot)", "klass": "Rogue", "level": 3,
     "pronouns": "she/her", "model": "grok-4.3"},
    {"id": "pc_3", "name": "Sister Marigold Penn", "race": "Human", "klass": "Cleric", "level": 3,
     "pronouns": "she/her", "model": "gemini-2.5-flash"},
    {"id": "pc_4", "name": "Ilbrandt Ash", "race": "Elf (High)", "klass": "Wizard", "level": 3,
     "pronouns": "he/him", "model": "deepseek-v4-flash"}
  ]
}
```

`"pronouns"` and `"age"` are optional and decide which Polly voices that
character can be cast from (see [Spoken narration](#spoken-narration)) —
`"pronouns"` takes any set a character goes by (`he/him`, `she/her`,
`they/them`, anything else), and `"age"` takes `child`, `adult` or a number of
years, of which only an age that reads as a child changes anything.
`"pronouns"` is also what the DM is told to call the character, in the
COMBATANTS block of its own prompt, so the pronouns a character is narrated in
and the voice it is spoken in come off the one authored string. Neither is a
rules field: nothing in the engine reads either, and the players see neither.

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
GET  /api/auth                          {"writes":"token"|"unconfigured", header, authenticated}
POST /api/games          {config}       → 201 {"id","status"}  (creates + starts)   ← token
GET  /api/games                         [{id, status, created_at, title, round, cost_usd}]
GET  /api/games/<id>                    snapshot + config + ledger
GET  /api/games/<id>/events?after=seq   transcript from SQLite
GET  /api/games/<id>/stream?after=seq   SSE: replay then live, `event: end` on finish
POST /api/games/<id>/pause|resume|stop  → 202                                       ← token
POST /api/games/<id>/note  {"text"}     → 202  (DM note from the table)             ← token
POST /api/games/<id>/hold  {"seconds","client"} → 202 {"holding": granted}
GET  /api/tts                           {"available":bool, engine, monster_engine, language, max_chars, price_per_million_chars, monster_price_per_million_chars, config}
POST /api/tts/cast       {party}        {"available":bool, seats:[{id, voice, language, accent, gender}]} — who reads each seat; renders nothing, spends nothing
GET  /api/games/<id>/tts?key=&text=&v=  audio/mpeg — one narrated line, cached and charged
```

The stream sends `id: <seq>` on every message, so an `EventSource` reconnect
resumes from `Last-Event-ID` instead of replaying the whole game. Heartbeat
comments go out every 15s.

### Write access

Reading is anonymous and stays that way — that is what the site is. The routes
marked `← token` above mutate a game or spend on it, and they require a shared
secret in a header:

```bash
curl -H "X-Dnd-Token: $DND_WRITE_TOKEN" -H 'Content-Type: application/json' \
     -d "{\"config\": $(cat examples/goblin_ambush.json)}" \
     https://dndsim.lab980.com/api/games
```

`POST /api/games` starts a real game against real API keys, and
`POST /api/games/<id>/note` feeds up to 2,000 characters into a `dm_note` that
the page speaks at Polly's neural rate — roughly 3¢ a call. Left open, the
per-game cap below bounds one game and nothing bounds how many are started.

- Wrong or missing header → `401 {"code": "unauthorized"}`.
- **`DND_WRITE_TOKEN` unset → `503 {"code": "writes_unconfigured"}`.** It fails
  closed: the app starts, the page loads, every read and the stream work, and a
  running game keeps running and stays audible — but nobody can start a new one
  until the token is set. Failing open would be a hole that looks closed.
- The token is read from the header only, never a query string: a query string
  is in nginx's access log, in `Referer` and in the browser's history.
- `GET /api/auth` reports whether this server takes a token and whether the
  caller's is accepted. It never echoes the token.

On the droplet, one command sets it and then uses it:

```bash
dndsim token          # generate → .env → restart → check against the running
                      # app → print it to paste into the page
dndsim token --show   # print the current one, change nothing
dndsim token --stdin  # use your own, read from stdin (not an argument: argv is
                      # world-readable through /proc)
```

It writes `.env` and nothing else. Unlike a platform key it is not kept in
`/etc/environment`: that file exists so `dndsim deploy` can adopt the box's
shared vendor keys, and this secret is this app's own — a second copy would be
one more place to leak it from and one more to forget on a rotation.

Three POSTs deliberately take no token. `POST /api/games/<id>/hold` is the
narration lease — every anonymous listener renews one every few seconds, it
spends nothing, it leaves `status` alone and it expires by itself.
`GET /api/games/<id>/tts` does spend, and stays open because an anonymous
spectator cannot hear the game without it and what it spends is capped per line
(`DND_TTS_MAX_CHARS`), per game (`min(budget_usd, DND_TTS_MAX_USD)`) and once
for good by the on-disk cache.

In the browser: the New game button, the pause/resume/stop row and the DM-note
form are not rendered until the page holds a token the server accepts — an
anonymous visitor never sees a control that can only 401. The header button
takes the token, validates it against `/api/auth` before keeping it, and stores
it in `localStorage` for that browser only. It stays in the header once
unlocked (labelled **Token** rather than **Unlock**), because the panel behind
it is the only way to press **Forget**: on a shared browser that is the
difference between signing out and clearing site data by hand. It is hidden
only where the server has no token set, so there is nothing to enter.

There is no per-writer identity and no revocation short of rotating the value
in `.env` and restarting. With one writer, that is the whole story rather than
an oversight.

## Cost control

Each game config carries `budget_usd`. The orchestrator tracks real token spend
per role and halts the game at `budget_exceeded`. The UI shows spend against
budget live. Prompts are built for frugality: players see a compact state view
plus enumerated legal actions (not full state), the static rules digest is
prompt-cached, and summaries are written by the cheap model.

## The spectator page

One rail and one column. Initiative, the party and the enemies sit down the
left in tight rows — one line per enemy, two per party member — so a full
combat is a glance rather than a scroll. The battle map and the controls sit in
a strip **above** the transcript; the transcript takes whatever height the
viewport has left; and under it, in reading order, come its own `mechanics` and
`follow` toggles and then the narrator's transport — the things you reach for
because of a line you have just read, rather than something between you and the
line. Below a phone's width the whole thing becomes one column and the
transcript moves back above the strip, because six panels of preamble is not a
way to read a story. The narration bar stays under the transcript at every
width.

**Dark or light, and the page will follow the system unless told otherwise.**
The button in the top bar cycles `auto → light → dark`; the choice is kept in
that browser's `localStorage` and nowhere else, so it is a reader's preference
and never a server setting. Dark is a rich brown rather than near-black, light
is pale wood, and both were solved rather than eyeballed: **every** run of text
clears WCAG AAA (7:1) against the worst of the eight surfaces it can land on,
with the tiers spaced by ratio so the hierarchy is carried by measured steps
rather than by letting the quietest one sag — story text at 12:1, the secondary
tier at 9.5:1, names and status colours at 8:1, the metadata (initiative
scores, the AC/class line, sequence numbers) at 7:1. Borders, focus rings and
the HP and budget bars clear 4.5:1, half again what 1.4.11 asks of a non-text
carrier of meaning; the map's walls and difficult ground clear 3:1 against the
board. The battle map paints from the same custom properties as everything
else, so it changes with the theme instead of staying dark on a light page, and
`web/tests/test_theme_tokens.py` checks the whole table as arithmetic.

## Spoken narration

The spectator page can read the game aloud. Tick **voice** in the top bar (the
tick is the tap that browsers require before a page may speak) and the DM's
narration, scene openings, the epilogue, every line of dialogue and any DM note
from the table are spoken. Mechanics are spoken too, but shaped into a short
line ("Goblin 2 hits Thorin for 6", "Round 3", "Vessa moves 30 feet") rather
than the dice string, and **mute mechanics** silences them entirely. The DM has
one voice; each PC gets its own, dealt deterministically so the same character
sounds the same every session.

**Who is speaking is announced, not read out as a label.** A PC's line is just
the line — their own voice is the attribution, and always was. Everyone else is
introduced by the narrator first, in the DM's voice and as a clip of its own:
"Goblin Sneak." and then, in the goblin's voice, "I'll gut you." The name used
to be glued to the front of the words instead, which meant it was spoken by the
monster through its own distortion, with a colon in the middle of the sentence
that the engine reads as a label. The name is unchanged **on screen**: the
transcript prints it in front of every line of dialogue as it always has.

### Who a character is

**The pronouns a character states reach the table, not just the casting.** The
DM's combatant table has carried a pronouns column since the attribution fix;
`player_view` now carries the same one, because a player talks about its allies
and the monsters both and infers a gender from a name exactly as readily. The
roster that opens a scene introduces each character with them, the player's own
cached prefix carries its own, and both system prompts say it outright: use the
pronouns you are given, never infer them from a name, a class, a title or a
voice. A monster has no character sheet, so it gets they/them on its own row,
every row, every turn — the fix for a Bandit Captain who is "she" in round 2
and "he" in round 5 is not to deal her a gender, it is to close the question.

A party member may state `"pronouns"` — `he/him`, `she/her`, `they/them`, or
any other set — and that decides which voices the character can be dealt.
`he` narrows the pool to the voices Polly reports as `Male`, `she` to the ones
it reports as `Female`, and everything else leaves the pool whole. Only the
first pronoun is read, so `he/him` and `he/him/his` are one answer. It narrows
who can be cast and nothing else: the choice within that set is the same hash
as before, so a character keeps its voice for as long as its pronouns and the
roster hold. They are read from the game's own party list, never from the
request that asks for the clip — this endpoint spends money, and pronouns in
the query string would be a way to walk the whole roster a paid clip at a time.

The mapping runs one way, from pronouns to a set of voices, and it is not a
claim that a pronoun is a gender. Polly's roster is `Female` and `Male`; there
is no third kind of voice on it. So a character who goes by `they/them`, or by
a set this mapping has never seen, or who states nothing at all, is dealt from
the **whole** pool rather than pushed into one of the two — the roster's
limitation is not something to launder into a character sheet.

The older key `"gender"` (`female` / `male`) is still read where a config
states it and no `pronouns`, so a scenario written before this, or a
stranger's, casts as it always did. Where both are stated the pronouns decide,
including `they/them`: a config that was updated should not go on being
narrowed by the key the update replaced.

The shipped parties follow one rule, and it is worth stating because it decides
what a stranger's config should look like too: **a character states pronouns
only where their own persona already does** — an explicit pronoun, or a
gendered form of address (`Dame`, `Sister`, `Brother`, `Father`, `Mother`).
Where the persona says nothing, the config says nothing, and that character is
dealt from the whole pool. Five of the twenty-eight in `examples/` are like
that — Crick, Vessa Quill, Ilbrandt Ash, Pib Underbough, Ozric Talleyrand — and
so are two of the four in the built-in preset. Choosing for them would be
writing a fact into someone else's character. Where a language ships
voices of only one gender (Korean and Swedish each ship one), a narrowing that
cannot be answered gets a voice anyway: a worse match, not a silence.

**A character is cast as an adult unless it says otherwise.** Polly's roster
has children's voices in it — `Ivy`, `Justin` and `Kevin`, the only three its
voice list annotates as children — and they used to sit in the pool every seat
was dealt from, which is how a cleric called Father Bexley Crane came to be
read out by a nine-year-old. So a party member may state an `"age"`: the words
`child` / `kid` / `boy` / `girl`, or `adult` / `elder` / `elderly` / `old`, or
a number of years (12 and under is a child). Only a character who asks for a
child's voice can be dealt one; everyone with no age stated, and the DM, the
NPCs and the monsters, are dealt from the adult voices.

`elder` is read and recorded, and casts as an adult: Polly has no elderly voice
to cast it as. A number is written plainly — an optional sign, digits, at most
one point, an optional exponent — because the panel and the server have to
agree about which strings are numbers at all, and `Number()` and Python's
`float()` do not (`0xA` is ten to one of them, `1_0` to the other). Anything
else, and any number that cannot be an age (`0`, `-3`, `"old enough"`), is
read as nothing said rather than rounded into one of the two, and a language
with no children's voices at all — every language but US English — casts a
stated child from the adult voices, a worse match rather than a silence.

**The New game panel asks for both, one row per seat**: the pronouns the
character goes by and whether its voice is an adult's or a child's. Either left
at its default writes nothing into the config — unstated pronouns already cast
from the whole pool and an unstated age already casts as an adult — so opening
the panel and touching nothing cannot state a fact about a character that
nobody chose to state. A scenario that already states a set the row does not
offer keeps it verbatim, as its own option; one that states only the older
`gender` key shows an unstated pronoun row, and leaving it there changes
nothing, because filling it in from that key would be running the mapping
backwards.

**The browser fallback ignores both.** `SpeechSynthesisVoice` has no gender or
age attribute in any browser, and inferring one from voice names across every
OS and locale would be a guess dressed as data. A session on the fallback
engine casts as it always did.

**And the panel says what those two rows dealt you** — under each seat, the
voice it will be read by, that voice's accent, and the gender Polly records for
the recording: `Geraint · Welsh · male`. A panel can show its controls and stay
silent about the outcome they turn, which is the state this was in; the outcome
is the interesting half, and it is not one a reader can work out from a pronoun
set and an age. The line comes from `POST /api/tts/cast`, which is `cast_for`
over the roster the server has already listed: the same function, roster and
hash that will read the game, so the panel cannot name a voice the game then
does not use. It renders nothing and spends nothing, and a server without Polly
answers that it has no cast — the browser's own voices will read the game, and
they are not this roster.

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

That effect is the reason **a monster is synthesized on a different engine from
everyone else**. It is standard-only, and so is pitch, while the table itself
sounds better on neural — so the DM, the players and the NPCs are cast on
`DND_TTS_ENGINE` (neural) and speaking monsters on `DND_TTS_MONSTER_ENGINE`
(standard). The engine travels on the cast rather than being read from a
setting when the clip is made, so a line cannot be cast for one engine and
rendered on another; each engine keeps its own voice roster, its own cached
clips and its own rate in the ledger. Set the two equal for one engine
throughout.

Because the monster seats are the only ones writing that tag and the only ones
routed to a second engine, a working table proves nothing about them — and a
monster line Polly refuses is a 502 the page answers by speaking that one line
in the browser's own voice, so it sounds fine and shows up only in the server
log. `python -m tools.polly_check` sends one real monster line and one real table
line and says what came back — on the droplet with `.env` sourced first, as
`run.sh` does (`--dry-run` prints the documents and sends nothing, anywhere); `tests/tts/test_polly_contract.py` holds every document the
app can emit against Amazon's published per-engine tag matrix.

One thing the neural default costs: the built-in fallback roster is standard's
and English's, so if `DescribeVoices` cannot be reached the table has nothing to
cast from and the page falls back to the browser's voices. Under a standard,
English default that call failing was survivable. It is deliberate in both
directions — a French line read by an English voice is not a degraded narrator
but a wrong one, and the probe would report it as working.

### What it costs

Polly is **$16.00 per million characters** on the neural engine and **$4.00**
on standard, and the table uses both — neural for the DM, the players and the
NPCs, standard for speaking monsters (see above). That works out around $0.45 a
game. The spend is
charged to the game's own `budget_usd` alongside the model calls — it shows up
as `by_role.narrator` in the ledger, and a game that has spent its budget goes back
to the browser's voices rather than quietly spending more. A whole game's
narration is a few cents.

Narration stops at the **lower** of the game's own `budget_usd` and
`DND_TTS_MAX_USD` (default $10), a ceiling the server owns and a submitted
config cannot raise — `budget_usd` comes from the caller, so left alone a
stranger picks the ceiling (`TTS-COSTS.md` §1). That bounds one game; how many
games can be started is bounded separately, by the write token on `POST
/api/games` (see "Write access" above).

A `budget_usd` that is not a finite number is refused outright at game
creation: `float("NaN")` passes coercion and then compares False against
everything, so a NaN budget is not a large budget — it is the absence of every
budget check in the app, `Game._check_budget` included.

A game that states no `budget_usd` gets `GameConfig`'s default rather than a
blank cheque, and a zero or negative budget refuses everything — the
orchestrator halts at `total_usd >= budget_usd`, so zero is a game already over
rather than a game with no ceiling.

A clip about to be synthesized **holds its own cost against the game** until it
is charged or abandoned, so eight spectators asking for eight different lines
at the same moment cannot each read the same below-budget total and each go to
Polly. Eight asking for the *same* line is the other half of that: they queue
on one gate, one pays, and the rest are served free — being refused for a clip
someone else is a moment from making free would be read by the page as a
settled refusal and cost server voices for the whole game. A clip that would
take the game over is refused before the call, which is stricter than the
model-call check (that one stops the game once it already has) — erring toward
stopping is the house style. A clip **already paid for is
served whatever the budget says**: the budget governs spend, and re-reading a
line is not spend, so a game that has run out of money stays listenable to the
end of its transcript.

Every clip is cached on disk under `data/tts`, keyed by the voice and the exact
SSML document sent, so a line is paid for once however many times it is replayed — which matters,
because the playhead is designed to be run backwards. Deleting the cache is
safe and costs only the re-synthesis. Clip URLs carry the probe's `config` fingerprint — the engine, the language,
the DM's voice, the roster, and a digest of `tts/voices.py` itself, since the
casting and the SSML decide the audio too — so changing any of them retires the
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

A renewal that fails is not the same as one that is refused. Three answers mean
this game will never take a hold — no such game (404), one another process is
running (409), one that does not support it (501) — and those turn the option
off for that game, with the tooltip saying so. Anything else is the wire
failing rather than the game answering: a dropped connection, a 502 while nginx
restarts under a deploy. The page keeps asking through those, backing off one
heartbeat per consecutive failure up to a minute. Giving up on them instead is
what used to leave a listener tens of lines behind with nothing on screen to
say why.

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

## Sourcing audio

There is a tool for picking the game's music, ambience, stings, swells and
effects: `python -m tools.audio harvest` searches the openly-licensed libraries
(Freesound, Jamendo, incompetech, the Internet Archive) for every one of the 55
cues — two of those need no key at all —
`audio/picker.html` is a self-contained preview screen you audition and assign
in, and `python -m tools.audio fetch` turns what you picked into files, a
`manifest.json` carrying each cue's event-match rule, and a `CREDITS.md`. Only
public-domain and attribution licences pass the gate, and what is fetched is
levelled to a common loudness where **ffmpeg** is installed (optional; without
it the files are kept as downloaded).

Nothing plays yet: no event currently makes a sound, and neither `web/` nor
`orchestrator/` imports any of it. The manifest is the handoff point. Full
notes, the source table and the licence rules: [AUDIO.md](AUDIO.md).

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
