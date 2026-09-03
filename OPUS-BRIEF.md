# Build brief for the Opus session

Read `PLAN.md` then `CONTRACTS.md` (all of it, including Amendments at the bottom). Those are the spec. This file is the work order.

## Status of the repo you are inheriting

| Layer | State | Tests |
|---|---|---|
| `engine/` data, dice, events, srd, state, characters | done | none yet |
| `engine/actions.py` | **missing** | **missing** (`tests/engine/`) |
| `llm/`, `agents/`, `orchestrator/`, `examples/` | done | `tests/orchestrator` 61 pass, 2 skipped (skips wait on `engine/actions.py`) |
| `web/`, `deploy/`, README, PM2, nginx | done | `web/tests` 19 pass |

`python -m pytest -q` from repo root → 80 passed, 2 skipped.

## Work order

### 1. `engine/actions.py` (the core of the project)
Implement CONTRACTS.md §1.6 exactly: `ActionTemplate`, `Action`, `IllegalAction`, `legal_actions`, `apply`, `start_combat`, `advance_turn`, `combat_over`, `reactions_for`, `skill_check`. Read `orchestrator/game.py` first and match the names it actually calls (it also expects `engine.characters.pc_to_combatant(sheet, position=...)` and uses `engine.characters.starting_resources(sheet)` — see Amendments). `apply` returns a new state. Opportunity attacks are auto-resolved inside `apply` on movement. Shield is auto-cast as a reaction when it converts a hit to a miss.

Cover the full "Rules coverage required in Phase 1" list in §1.6. Resolve every `effect.kind` present in `engine/data/spells.json`; fix the data if a record is unresolvable.

### 2. `tests/engine/`
One test per rule in the §1.6 list. Plus a scripted full combat: build the 4 PCs from `examples/goblin_ambush.json` via `build_character` + `pc_to_combatant`, 5 goblins via `monster_to_combatant`, seeded RNG picks random legal actions through `legal_actions → apply → advance_turn` until `combat_over`; assert per step: event seq monotonic, turn budgets ≥ 0, HP in [0, max], initiative list unchanged, `to_dict/from_dict` round-trips equal. Determinism: same seed → identical event text list.

### 3. Integration
- `python -m pytest -q` — the two skipped tests in `tests/orchestrator/test_real_engine_smoke.py` must now run and pass. Fix the engine, not the orchestrator, unless the orchestrator is calling outside the contract — then fix the orchestrator and record an Amendment.
- `python -m orchestrator.cli --config examples/goblin_ambush.json --mock --seed 42 --tempo 0` → runs to `finished`, no `error` events. Same for `examples/crypt.json` (exercises turn undead, paralysis).
- `DND_SIM_MOCK=1 python -m web.app`, then in another shell: create a game via `POST /api/games` with a preset, `curl -N /api/games/<id>/stream` shows live events, pause/resume/note work, `/api/games/<id>` snapshot has ledger and state.
- Open `/` in a browser (iPad Safari matters): transcript, initiative, party cards, grid, cost meter all update.

### 4. First live run
Set `ANTHROPIC_API_KEY`, run `goblin_ambush` with `--live`, budget $0.50. Read the transcript. Expect to tune `agents/prompts/*.txt` (narration length, player tactics, JSON discipline). Check the ledger: target well under $0.25 for a 4-goblin fight; if higher, shrink `views.py` output or the rules digest.

### 5. Deploy (lab980 protocol)
`deploy/INSTALL.md`. Port 8071, PM2 `dnd-sim`, HTTP-only nginx vhost first, certbot after DNS. `instances: 1` is mandatory (games are in-process threads).

## Non-negotiables
- LLMs never decide outcomes; the engine does. Any prompt that lets the DM "declare" a hit is a bug.
- SRD 5.1 content only (CC-BY-4.0, attribution in `engine/data/LICENSE-SRD.txt`).
- Token frugality: compact views, cached system blocks, Haiku for summaries, per-game USD budget enforced.
- Deterministic under mock + seed.

## Later (Phase 2)
Remaining SRD classes/races, levels 6–10, more spells, legendary/lair actions, multiclassing. Human "take a seat" mode was explicitly deferred.
