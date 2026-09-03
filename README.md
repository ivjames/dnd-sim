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
| `DND_ALLOW_UNPRICED` | unset | `1` → let a live game start with a model that has no row in `llm/cost.py` `PRICES` (it is then charged at the default $2/$10 rate). Off by default: the budget stop is blind for an unpriced model, so game creation refuses. |
| `DND_SIM_MOCK` | unset | `1` → `MockLLMClient`, zero API calls. |
| `DND_SIM_DB` | `./data/dndsim.sqlite3` | SQLite transcript store. |
| `DND_SIM_EXAMPLES` | `./examples` | Where `/api/presets` reads scenarios from. |
| `DND_DM_MODEL` | `claude-sonnet-5` | DM model. |
| `DND_PLAYER_MODEL` | `claude-haiku-4-5-20251001` | Player model. |
| `DND_SUMMARY_MODEL` | = player model | Rolling-summary model. |

Any of the three `DND_*_MODEL` values, and a party member's per-seat `model`,
may name a model on any platform above — the platform is chosen from the
model id's prefix. Keys are read only for platforms actually seated.

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
    {"id": "pc_1", "name": "Thorin Cragmantle", "race": "Dwarf (Hill)", "klass": "Fighter", "level": 3},
    {"id": "pc_2", "name": "Vessa Quill", "race": "Halfling (Lightfoot)", "klass": "Rogue", "level": 3,
     "model": "grok-4.3"},
    {"id": "pc_3", "name": "Sister Marigold Penn", "race": "Human", "klass": "Cleric", "level": 3,
     "model": "gemini-2.5-flash"},
    {"id": "pc_4", "name": "Ilbrandt Ash", "race": "Elf (High)", "klass": "Wizard", "level": 3,
     "model": "deepseek-v4-flash"}
  ]
}
```

In live mode, game creation checks every seat up front and refuses with one
message naming what is wrong: a model no row routes, a platform whose key is
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
| `DND_SIM_LOGLEVEL` | `INFO` | Server log level. |

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

The spectator page can read the game aloud. Tick **voice** in the top bar
(the tick is the tap that browsers require before a page may speak) and the
DM's narration, scene openings, the epilogue, every line of dialogue and any DM
note from the table are spoken as they arrive — never the replayed transcript.
Mechanics are spoken too, but shaped into a short line ("Goblin 2 hits Thorin
for 6", "Round 3", "Vessa moves 30 feet") rather than the dice string, and
**mute mechanics** silences them entirely. The DM has one voice; each PC gets
its own, picked deterministically from the voices the browser has, with a
pitch/rate nudge when there are too few to go round (an iPad often has one or
two). The rate has three steps, **skip** drops the line being read, and the
transcript line being spoken is highlighted.

This is the browser's own Web Speech API (`speechSynthesis`) — nothing leaves
the device and it costs nothing, so voice quality is whatever the OS ships.
Server-rendered voices (Amazon Polly, Cartesia and the like) would be a later,
paid option. Selection and wording live in `web/static/speech.js`, a
dependency-free module that `node` can exercise directly; the queue and the
speech calls are in `app.js`.

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
