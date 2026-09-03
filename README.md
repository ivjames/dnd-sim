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
| `llm/` | Anthropic client, `MockLLMClient`, token/cost ledger. |
| `agents/` | Prompt construction and output parsing for DM and players. |
| `orchestrator/` | Game loop, scenes, turns, memory, event bus, controls. |
| `web/` | Flask app, SQLite transcripts, SSE stream, static spectator UI. |
| `deploy/` | nginx vhost + install notes. |

Everything the UI and the LLM layer see from a resolved turn is an `Event`
(`seq, round, kind, actor, text, data`). Events are appended to SQLite and
published on an in-process bus; the browser reads them over SSE.

## Run locally (mock mode — no API key, no cost)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DND_SIM_MOCK=1 .venv/bin/python -m web.app
```

Open <http://127.0.0.1:8045/>, hit **New game**, pick a preset, start.

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
| `PORT` | `8045` | Listen port. |
| `HOST` | `127.0.0.1` | Bind address. Keep it loopback; nginx fronts it. |
| `ANTHROPIC_API_KEY` | — | Required for live mode. On lab980 it lives in `/etc/environment`. |
| `DND_SIM_MOCK` | unset | `1` → `MockLLMClient`, zero API calls. |
| `DND_SIM_DB` | `./data/dndsim.sqlite3` | SQLite transcript store. |
| `DND_SIM_EXAMPLES` | `./examples` | Where `/api/presets` reads scenarios from. |
| `DND_DM_MODEL` | `claude-sonnet-5` | DM model. |
| `DND_PLAYER_MODEL` | `claude-haiku-4-5-20251001` | Player model. |
| `DND_SUMMARY_MODEL` | = player model | Rolling-summary model. |
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

## Deployment

See [deploy/INSTALL.md](deploy/INSTALL.md). Short version: PM2 process `dnd-sim`
on 127.0.0.1:8045, nginx vhost proxying `dndsim.lab980.com` with
`proxy_buffering off` (SSE dies without it), HTTP first, `certbot --nginx` only
after DNS resolves.

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
