# dnd-sim — autonomous D&D 5e simulation

Working name: `dnd-sim` (product name undecided; Jimmy names it later).
Target: `dndsim.lab980.com`, Python app, port **8071** (first free port in lab980's 8060+ range, confirmed on the droplet 2026-09-03), PM2 process `dnd-sim`.

## What it is
A server that runs a D&D 5e game with no humans at the table:
- **DM = Sonnet** (`claude-sonnet-5`): narrates, runs monsters/NPCs, adjudicates non-combat, sets DCs.
- **Players = Haiku** (`claude-haiku-4-5-20251001`), one instance per party member, each with a persona and a real character sheet.
- **Rules = code.** Dice, initiative, attacks, saves, damage, conditions, spells, concentration, death saves, movement on a 5-ft grid. LLMs propose; the engine validates and resolves. The DM never "decides" a hit.
- **Human = spectator with knobs.** Watches a live SSE stream; can pause/resume/stop, set party/setting/seed before start, and inject a DM note mid-game. No seat at the table.

Everything is seeded: same config + seed + mock LLM → identical game. Real LLM runs are reproducible on the dice side only.

## Fidelity target: "full 5e combat"
Full 5e combat is weeks of work if done all at once. Phase it so each phase is a shippable, playable engine:

- **Phase 1 (this build):** SRD 5.1 (CC-BY-4.0) content subset. Classes: Fighter, Rogue, Cleric, Wizard, levels 1–5, including class features that matter in combat (Second Wind, Action Surge, Sneak Attack, Cunning Action, Channel Divinity: Turn Undead, Arcane Recovery, Spellcasting). Races: Human, Elf (High), Dwarf (Hill), Halfling (Lightfoot). Weapons/armor: full SRD tables. Spells: ~40 covering levels 0–3 across Cleric/Wizard lists (attack, save, AoE, buff, healing, control). Monsters: ~25 CR 0–5. Conditions: all 15 + exhaustion. Grid movement with difficult terrain, opportunity attacks, cover (half/three-quarters), reach.
- **Phase 2 (later):** remaining SRD classes/races, levels 6–10, more spells, legendary/lair actions, mounted/underwater, multiclassing.
- **Out of scope for now:** non-SRD content (copyright), homebrew, VTT-style maps beyond a simple grid.

## Layers (strict, one-way dependencies)

```
web  →  orchestrator  →  agents  →  llm
                     ↘        ↘
                       engine  (pure, no I/O, no LLM)
```

- `engine/` — pure Python, deterministic, no network, no threads. Owns SRD data (`engine/data/*.json`).
- `llm/` — thin Anthropic client + MockLLMClient + token/cost accounting.
- `agents/` — prompt construction and output parsing for DM and Players. Knows engine types; never mutates state.
- `orchestrator/` — game loop (scenes, turns), memory/summaries, event bus, controls.
- `web/` — Flask, SSE, SQLite, static UI.
- `deploy/` — PM2 ecosystem, nginx vhost (HTTP-only; certbot after DNS), install notes.

`CONTRACTS.md` is the binding interface spec. Builders work in parallel against it. If a builder must change a contract, they record the change at the bottom of `CONTRACTS.md` under "Amendments" with rationale.

## Token frugality (hard requirement, same as qa-engine)
- Players see a **compact state view**, not the full state: their own sheet, visible combatants (name, side, approx HP band, position, conditions), the last N events as one-line strings, and the **enumerated legal actions** with short ids. They answer with a single JSON object choosing an action id plus optional parameters and ≤40 words of in-character speech.
- DM gets the same compact view plus a rolling summary. DM narration capped (`max_tokens` ~300 in combat, ~600 in scenes).
- Rolling summary every K events (Haiku summarizes, not Sonnet).
- Prompt caching: static system prompt + SRD rules digest at the front of every call, stable across turns.
- Per-game USD budget from config; orchestrator stops the game when exceeded and emits a `cost` event.

## Run modes
- `python -m orchestrator.cli --config examples/goblin_ambush.json --mock --seed 42` — headless, mock LLM, prints events. This is the integration test.
- `python -m orchestrator.cli --config ... --live` — real API.
- `python -m web.app` — Flask, reads `ANTHROPIC_API_KEY` from env (`/etc/environment` on lab980). Mock mode via `DND_SIM_MOCK=1`.

## Build plan (Opus builders, parallel)
| Task | Owner | Files |
|---|---|---|
| A | engine builder | `engine/**`, `tests/engine/**` |
| B | agents+orchestrator builder | `llm/**`, `agents/**`, `orchestrator/**`, `tests/orchestrator/**`, `examples/**` |
| C | web builder | `web/**`, `deploy/**`, `README.md`, `requirements.txt`, `ecosystem.config.js` |
| D | integrator (after A–C) | wire, run tests, run mock game, fix, tarball |

Builders must not edit files outside their ownership. Cross-layer needs go through CONTRACTS.md types; if the other side isn't built yet, code against the contract and stub in tests.

## Deploy (lab980 protocol)
1. `git clone` to `/opt/dnd-sim` (or wherever the other Python apps live — confirm), `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
2. `pm2 start ecosystem.config.js` (process `dnd-sim`, port 8071, `ANTHROPIC_API_KEY` from `/etc/environment`).
3. nginx: HTTP-only vhost for `dndsim.lab980.com` → `127.0.0.1:8071`, SSE-safe (`proxy_buffering off`, long `proxy_read_timeout`). Point DNS. Then `certbot --nginx -d dndsim.lab980.com`. Never ship SSL blocks before certbot has run.
