# CONTRACTS — binding interfaces between layers

Python 3.11. Type hints everywhere. Dataclasses (or plain dicts where noted) — **no Pydantic**, keep deps minimal (`flask`, `anthropic`, `pytest` only).
All state is JSON-serializable via `to_dict()` / `from_dict()` on every dataclass. IDs are short strings (`"pc_1"`, `"mon_3"`).

---

## 1. engine/  (pure, deterministic)

### 1.1 Dice — `engine/dice.py`
```python
class RNG:
    def __init__(self, seed: int): ...
    def roll(self, expr: str) -> RollResult      # "1d20+5", "2d6", "8d6", "1d20" ; supports "adv"/"dis" via roll_d20
    def roll_d20(self, mod: int = 0, mode: str = "normal") -> RollResult   # mode: "normal" | "advantage" | "disadvantage"
    def randint(self, a: int, b: int) -> int
    def choice(self, seq): ...
    def state(self) -> dict ; @classmethod from_state(d) -> RNG   # so games can be snapshotted/resumed

@dataclass
class RollResult:
    expr: str
    rolls: list[int]        # raw dice (for d20 adv/dis: both dice)
    kept: list[int]
    modifier: int
    total: int
    mode: str               # normal/advantage/disadvantage
    natural: int | None     # the kept d20 face if a d20 roll, else None
```
Same seed ⇒ identical sequence. Never use `random` global.

### 1.2 Data — `engine/data/*.json` + `engine/srd.py`
JSON files: `races.json`, `classes.json`, `spells.json`, `monsters.json`, `weapons.json`, `armor.json`, `conditions.json`, `equipment.json`. Content from **SRD 5.1 only** (CC-BY-4.0); include `engine/data/LICENSE-SRD.txt` with the required attribution text.
```python
srd.race(name) -> dict ; srd.klass(name) -> dict ; srd.spell(name) -> dict ; srd.monster(name) -> dict
srd.weapon(name) -> dict ; srd.armor(name) -> dict ; srd.condition(name) -> dict
srd.list_spells(klass: str, level: int | None = None) -> list[str]
srd.list_monsters(cr_max: float | None = None) -> list[str]
srd.rules_digest() -> str   # ≤1200 tokens plain-English digest of combat rules used in prompts (stable string, cached)
```
Spell records must be **machine-resolvable**: each has `effect` with a small closed vocabulary the resolver implements:
`{"kind": "attack"|"save"|"heal"|"buff"|"debuff"|"summon_none"|"utility", "attack_type": "melee"|"ranged"|null, "save": "DEX"|..., "half_on_save": bool, "damage": "8d6", "damage_type": "fire", "upcast": "+1d6/slot", "area": {"shape":"sphere"|"cone"|"line"|"cube"|null, "size": 20}, "range": 150, "duration_rounds": int|null, "concentration": bool, "conditions_applied": [...], "targets": int}`.
Monsters: full stat block — abilities, AC, HP (avg), speed, saves, skills, senses, CR, actions with `attack_bonus`, `reach/range`, `damage` expr, `damage_type`, on-hit effects/saves; multiattack as an ordered list; spellcasting where SRD monster has it (subset ok).

### 1.3 Characters — `engine/characters.py`
```python
@dataclass
class CharacterSheet:
    id: str; name: str; race: str; klass: str; level: int
    abilities: dict[str,int]           # STR DEX CON INT WIS CHA (post-racial)
    max_hp: int; ac: int; speed: int; proficiency: int
    saves: list[str]; skills: list[str]
    weapons: list[str]; armor: str | None; shield: bool
    spells_known: list[str]; spell_slots: dict[int,int]     # {1: 4, 2: 2}
    spellcasting_ability: str | None
    features: list[str]                # engine-recognized feature ids: "second_wind","action_surge","sneak_attack","cunning_action","channel_divinity_turn_undead","arcane_recovery","extra_attack",...
    persona: str                       # free text; not used by engine

def build_character(spec: dict, rng: RNG) -> CharacterSheet
# spec: {"id","name","race","klass","level","abilities": {"STR":15,...} | "standard_array" | "point_buy_default", "equipment": "default"|[...], "spells": "default"|[...], "persona": str}
def monster_to_combatant(name: str, cid: str, rng: RNG, roll_hp: bool=False) -> "Combatant"
```

### 1.4 State — `engine/state.py`
```python
@dataclass
class Condition: name: str; duration: int | None; source: str | None; save_dc: int | None; save_ability: str | None; extra: dict

@dataclass
class Combatant:
    id: str; name: str; side: str                 # "party" | "enemy" | "neutral"
    kind: str                                     # "pc" | "monster"
    sheet: CharacterSheet | None                  # pcs
    stat_block: dict | None                       # monsters (from srd)
    hp: int; max_hp: int; temp_hp: int; ac: int; speed: int
    abilities: dict[str,int]; save_profs: list[str]; skill_profs: list[str]; proficiency: int
    position: tuple[int,int]                      # grid squares (5 ft)
    size: str                                     # T S M L H G
    conditions: list[Condition]
    concentration: dict | None                    # {"spell": name, "targets": [...], "started_round": int}
    death_saves: dict                             # {"success": 0, "failure": 0}
    stable: bool; dead: bool
    resources: dict                               # {"spell_slots": {1:4}, "second_wind": 1, "action_surge": 1, "channel_divinity": 1, "hit_dice": 3, ...}
    turn: dict                                    # {"action": False, "bonus": False, "reaction": False, "movement_left": int, "attacks_left": int, "free_object": bool}
    inventory: list[str]

@dataclass
class GameState:
    seed: int; rng: dict                          # RNG.state()
    mode: str                                     # "combat" | "exploration" | "social"
    round: int; turn_index: int
    combatants: dict[str, Combatant]
    initiative: list[tuple[str,int]]              # [(id, score)] sorted desc
    grid: Grid
    scene: dict                                   # {"title","description","objectives":[...], "location"}
    event_seq: int
    def active_id(self) -> str | None
    def to_dict(self) / from_dict(d)

@dataclass
class Grid:
    width: int; height: int
    difficult: set[tuple[int,int]]; walls: set[tuple[int,int]]; cover: dict[tuple[int,int], str]   # "half"|"three_quarters"
    def distance_ft(a, b) -> int      # 5e simplified diagonal (every diagonal 5 ft)
    def path(state, start, goal, max_ft) -> list[tuple[int,int]] | None   # BFS respecting walls/occupancy, difficult terrain costs double
```

### 1.5 Events — `engine/events.py`
```python
@dataclass
class Event:
    seq: int; round: int; kind: str; actor: str | None; text: str; data: dict
# kind ∈ {"combat_start","round_start","turn_start","turn_end","roll","attack","damage","heal","save","condition_add","condition_remove",
#         "move","spell_cast","concentration_broken","death_save","down","dead","stable","combat_end",
#         "narration","dialogue","dm_note","scene","skill_check","system","cost","error"}
# text: one-line human string ("Thorin attacks Goblin 2: 1d20+5 → 17 vs AC 15, hit, 1d8+3 → 9 slashing")
# data: structured (roll results as RollResult.to_dict(), target ids, hp before/after, etc.)
```
Events are the **only** thing the UI and the LLM layer read from a resolved turn.

### 1.6 Actions — `engine/actions.py`
```python
@dataclass
class ActionTemplate:            # what a chooser sees
    id: str                      # short, unique within the turn: "a1","a2",...
    type: str                    # "attack","cast","move","dash","dodge","disengage","help","hide","use_item","ready","second_wind","action_surge","cunning_action","channel_divinity","death_save","end_turn","skill_check"
    label: str                   # ≤80 chars: "Attack Goblin 2 with Longsword (+5, 1d8+3)"
    params: dict                 # fixed params (target id, weapon, spell, slot) — chooser only supplies what's in `needs`
    needs: list[str]             # e.g. ["path"] for move, ["targets"] for multi-target spells, ["point"] for AoE, [] for none
    cost: str                    # "action"|"bonus"|"reaction"|"movement"|"free"

@dataclass
class Action:                    # what a chooser returns
    actor: str; template_id: str; params: dict; speech: str | None = None

def legal_actions(state: GameState, actor_id: str) -> list[ActionTemplate]
# exhaustive for this actor this turn, given remaining action/bonus/movement; includes "end_turn" always; attacks list one template per (weapon,target in reach/range); spells one per (spell, slot level, target) for single-target, one per spell for AoE with needs=["point"]; move as ONE template with needs=["path"] plus up to 6 suggested destinations in params["suggested"]
def apply(state: GameState, action: Action) -> tuple[GameState, list[Event]]
# validates (raise IllegalAction(msg)), resolves fully (rolls, damage, conditions, opportunity attacks on move, concentration checks, death), returns NEW state (do not mutate) + events
def start_combat(state, rng_state) -> tuple[GameState, list[Event]]      # rolls initiative, sets mode, round=1, resets turn budgets
def advance_turn(state) -> tuple[GameState, list[Event]]                  # end current turn effects, tick durations, start next (round_start when wrapping); handles unconscious actors (auto death save event) and dead ones (skip)
def combat_over(state) -> str | None                                      # "party" | "enemy" | None (side with no conscious, non-fled members loses)
def reactions_for(state, trigger: dict) -> list[ActionTemplate]           # opportunity attacks; Phase 1: engine auto-resolves opportunity attacks (no LLM ask)
def skill_check(state, actor_id, skill: str, dc: int) -> tuple[GameState, list[Event]]   # d20 + mod (+prof if proficient), adv/dis from conditions
```
Rules coverage required in Phase 1 (each needs at least one test): initiative (DEX tiebreak), attack rolls (adv/dis from conditions: prone, restrained, blinded, invisible, paralyzed/unconscious auto-crit within 5 ft, poisoned, frightened, dodge), crits (double dice), cover (+2/+5 AC, +DEX saves), reach vs range (disadvantage in melee for ranged), two-weapon fighting, extra attack, sneak attack (adv or ally adjacent, once/turn), second wind, action surge, cunning action, spell attack & save spells, AoE with area targeting on grid (sphere/cone/cube/line), half-on-save, upcasting, concentration (CON save DC 10 or half dmg; casting a second concentration spell ends the first), conditions with durations & repeat saves at end of turn, difficult terrain, opportunity attacks (disengage negates), dodge, help, hide (Stealth vs passive Perception), healing, temp HP, 0 HP → unconscious + death saves (nat 20/1 rules), instant death on massive damage, stabilizing, turn undead, monster multiattack, exhaustion levels.

---

## 2. llm/  — client + accounting

```python
# llm/client.py
@dataclass
class LLMResponse: text: str; input_tokens: int; output_tokens: int; cache_read_tokens: int; cache_write_tokens: int; model: str; stop_reason: str

class LLMClient(Protocol):
    def complete(self, *, model: str, system: str | list[dict], messages: list[dict], max_tokens: int, temperature: float = 0.7, json_only: bool = False) -> LLMResponse

class AnthropicClient(LLMClient):   # uses anthropic SDK; system passed as content blocks with cache_control on the stable block
class MockLLMClient(LLMClient):     # seeded; given a prompt containing legal action ids (it will find lines matching r"^\s*\[(a\d+)\]"), picks one weighted toward attacks/spells over end_turn; returns valid JSON per the requested shape (the shape is announced in the prompt as `RESPONSE_SHAPE: player_action|dm_narration|dm_monster_action|dm_adjudication|summary`); fills speech/narration with short canned strings. Never returns malformed JSON.

# llm/cost.py
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5-20251001": (1.0, 5.0)}   # $/MTok in, out; cache read = 0.1×in, cache write = 1.25×in
class Ledger:
    def add(self, role: str, resp: LLMResponse) -> float   # returns USD for this call
    total_usd: float ; by_role: dict[str, dict]   # {"dm": {"calls","in","out","usd"}, "player:pc_1": {...}}
    def to_dict(self)
```
Model names come from env: `DND_DM_MODEL` (default `claude-sonnet-5`), `DND_PLAYER_MODEL` (default `claude-haiku-4-5-20251001`), `DND_SUMMARY_MODEL` (default = player model). A model id may name any platform in `llm/providers.py`; see the amendment "2026-09-03 — llm/ — multi-provider routing" for `RouterClient`, `OpenAICompatClient` and per-seat `model`.

---

## 3. agents/  — prompts and parsing

```python
# agents/views.py
def player_view(state: GameState, actor_id: str, recent: list[Event], summary: str) -> str
# compact text, ≤~700 tokens: own sheet line, resources, conditions; visible combatants table (name, side, HP band: "healthy/wounded/bloodied/critical/down", distance ft, conditions); last ≤12 events one-line; scene one-liner; summary paragraph
def dm_view(state, recent, summary) -> str        # same but full HP numbers and all sides
def render_actions(templates: list[ActionTemplate]) -> str   # "[a1] Attack Goblin 2 with Longsword (+5, 1d8+3)\n[a2] ..."

# agents/player.py
class PlayerAgent:
    def __init__(self, client: LLMClient, model: str, sheet: CharacterSheet, ledger: Ledger)
    def choose_action(self, view: str, templates: list[ActionTemplate]) -> Action
    # prompt: cached system (persona + role rules + srd.rules_digest()) ; user = view + actions + RESPONSE_SHAPE: player_action
    # response JSON: {"action": "a3", "params": {...}, "speech": "≤40 words or null", "reasoning": "≤25 words"}
    # parsing: tolerant (strip fences, find first {...}); on invalid id or bad params → one retry with error appended; on second failure → return end_turn (and emit an "error" event via return value? No: raise AgentOutputError; orchestrator handles fallback)
    def speak(self, view: str, prompt: str) -> str     # for social/exploration scenes, ≤60 words
    def choose_scene_action(self, view: str, options: list[str]) -> dict  # {"choice": idx, "speech": str}

# agents/dm.py
class DMAgent:
    def __init__(self, client, model, ledger, setting: str, tone: str)
    def open_scene(self, scene: dict, party_summary: str) -> str                     # ≤600 tokens narration
    def narrate(self, view: str, events: list[Event]) -> str                          # ≤60 words (a listening budget — see Amendments), describes the last turn's resolved events in-world; MUST NOT contradict numbers
    def monster_action(self, view: str, templates: list[ActionTemplate], monster_id: str) -> Action   # RESPONSE_SHAPE: dm_monster_action, same JSON as player
    def adjudicate(self, view: str, request: str) -> dict                             # RESPONSE_SHAPE: dm_adjudication → {"resolution": "skill_check"|"narrative"|"start_combat", "skill": "Stealth", "dc": 13, "actor": "pc_2", "narration": str, "encounter": {"monsters": [{"name": "Goblin","count": 4}], "grid": {...}} | null}
    def scene_options(self, view: str) -> list[str]                                   # 3–4 short options for the party in exploration/social mode
    def epilogue(self, view: str, outcome: str) -> str

# agents/summarizer.py
def summarize(client, model, ledger, previous_summary: str, events: list[Event]) -> str   # ≤150 words
```
All prompts live in `agents/prompts/*.txt` (plain text with `{placeholders}`), not inline strings. System prompts are stable strings (cacheable).

---

## 4. orchestrator/

```python
# orchestrator/config.py
@dataclass
class GameConfig:
    seed: int
    setting: str                     # free text world/tone
    tone: str = "classic heroic"
    party: list[dict]                # character specs (see build_character)
    scenario: dict                   # {"opening": str, "encounters": [{"trigger":"scene_1", "monsters":[{"name":"Goblin","count":4}], "grid": {"width":12,"height":10,"party_start":[[1,4],[1,5],[2,4],[2,5]], "enemy_start":[[10,3],...], "difficult":[[5,5]], "walls":[]}}], "max_scenes": 3}
    dm_model: str; player_model: str; summary_model: str
    max_rounds_per_combat: int = 20
    budget_usd: float = 1.00
    tempo_ms: int = 800              # min delay between emitted events (spectator pacing); 0 in tests
    mock: bool = False
    @classmethod from_dict / to_dict

# orchestrator/bus.py
class EventBus:
    def subscribe(self) -> "queue.Queue[Event|None]"      # None = stream closed
    def unsubscribe(self, q)
    def publish(self, ev: Event)
    def history(self) -> list[Event]

# orchestrator/game.py
class Game:
    def __init__(self, cfg: GameConfig, client: LLMClient, bus: EventBus, on_event: Callable[[Event], None] | None = None)
    id: str; status: str            # "created"|"running"|"paused"|"finished"|"stopped"|"error"|"budget_exceeded"
    def start(self) -> None         # spawns a daemon thread running run()
    def run(self) -> None           # blocking loop (used by CLI/tests)
    def pause(self); def resume(self); def stop(self)
    def inject_dm_note(self, text: str)   # queued; delivered to DM before its next call as "DM NOTE FROM TABLE: ..." and emitted as dm_note event
    def snapshot(self) -> dict      # {"state": GameState.to_dict(), "summary": str, "ledger": Ledger.to_dict(), "status": str, "round": int}
    ledger: Ledger
# Loop: open_scene → exploration/social loop (DM scene_options → each player choose_scene_action (majority/first) → DM adjudicate → skill checks via engine) → when adjudicate says start_combat or scenario encounter triggers: build combatants, place on grid, start_combat → per turn: if pc: player.choose_action(view, legal_actions) ; if monster: dm.monster_action ; engine.apply ; publish engine events ; dm.narrate(events) → publish narration ; advance_turn ; every K=15 events summarize ; check combat_over / max_rounds ; then back to scene loop until max_scenes or TPK → epilogue → status finished.
# Fallbacks: AgentOutputError → emit "error" event, apply end_turn. Budget exceeded → status budget_exceeded, stop. Exceptions → status error, emit error event with traceback text (truncated). Pause is checked between events; stop is checked between LLM calls.
# Determinism: with MockLLMClient and fixed seed, the event list (excluding timestamps) is identical across runs — test asserts this.

# orchestrator/cli.py
# python -m orchestrator.cli --config path.json [--mock] [--live] [--seed N] [--tempo 0] [--json]  → prints events (text or JSON lines), exits 0 on finished
```

`examples/goblin_ambush.json`: 4 PCs (Fighter/Rogue/Cleric/Wizard, level 3, distinct personas), one scene + one 4-goblin + 1 goblin boss encounter on a 12×10 grid with a few difficult squares and a wall segment. `examples/crypt.json`: level 5 party vs skeletons/zombies + a ghoul (tests turn undead, paralysis).

---

## 5. web/

Flask app factory `web/app.py: create_app() -> Flask`; `python -m web.app` runs on `PORT` env (default 8071), host 127.0.0.1.

Routes:
```
GET  /                                  spectator UI (single static page: web/static/index.html + app.js + style.css)
GET  /api/health                        {"ok":true,"mock":bool,"games_running":n}
GET  /api/presets                       list of example configs (name, description, config)
POST /api/games          {config}       → {"id", "status"}   (creates + starts)
GET  /api/games                         [{id, status, created_at, title, round, cost_usd}]
GET  /api/games/<id>                    snapshot + config + ledger
GET  /api/games/<id>/events?after=seq   history (from SQLite)
GET  /api/games/<id>/stream             SSE: `event: <kind>` `data: <Event JSON>`; on connect replays history after ?after=, then live; heartbeat comment every 15s; on finish sends `event: end`
POST /api/games/<id>/pause | /resume | /stop
POST /api/games/<id>/hold   {"seconds"}  → 202 {"holding": <granted>}  narration lease; expires by itself, leaves status alone
POST /api/games/<id>/note   {"text"}    → 202
```
SQLite `web/db.py` (file at `DND_SIM_DB`, default `./data/dndsim.sqlite3`): tables `games(id TEXT PK, created_at, config_json, status, title, cost_usd, snapshot_json)` and `events(game_id, seq, kind, json, PRIMARY KEY(game_id, seq))`. Persist via `Game(on_event=...)`. Snapshot saved on status change and every 25 events.

UI (vanilla JS, no build step; dark "candlelit table" theme is fine but keep it readable): transcript column (narration/dialogue prominent, mechanical events as compact monospace lines, collapsible), initiative/turn tracker, party cards (HP bars, conditions, slots), a simple grid canvas showing positions, a cost/token meter, controls (pause/resume/stop, DM-note textbox), a "new game" panel with preset picker + seed + setting/tone text + budget. Theme can be refined later; correctness first.

`deploy/nginx-dndsim.conf` (HTTP-only, SSE-safe), `ecosystem.config.js` (PM2, `interpreter: ".venv/bin/python"`, `script: "-m web.app"` or a `run.sh`), `deploy/INSTALL.md` following lab980 protocol (certbot after DNS).

---

## Amendments
(builders append here: date, layer, what changed, why)

### 2026-09-03 — web — injectable game factory in `create_app`

`web/app.py` signature is now:

```python
def create_app(game_factory: GameFactory | None = None,
               db_path: str | None = None,
               config: dict | None = None) -> Flask
GameFactory = Callable[[dict, Callable[[Event], None]], tuple[Game, EventBus]]
```

`game_factory(config_dict, on_event) -> (Game, EventBus)` builds (but does not
start) a game. Default is `web.factory.default_game_factory`, which does exactly
what §5 implies: `GameConfig.from_dict(config)` → `MockLLMClient` if
`DND_SIM_MOCK=1`/`config.mock` else `AnthropicClient(api_key=ANTHROPIC_API_KEY)`
→ `EventBus()` → `Game(cfg, client, bus, on_event=on_event)`. `orchestrator`/`llm`
imports inside it are lazy.

*Why:* the web layer had to be buildable and testable before/independently of
engine+orchestrator, and `web/tests/` injects fakes for `Game`/`EventBus`.
No other layer's interface changes; nothing outside `web/` needs to do anything.

Two smaller web-side conventions, recorded so nobody is surprised:

- **SSE resume point** is `max(?after=, Last-Event-ID)`. `EventSource` reconnects
  to the original URL (stale `?after=`) while sending the freshest seq in the
  header, so taking the max makes reconnects resume instead of replaying.
- **Stream replay** merges SQLite history with `bus.history()` (deduped by
  `seq`). The game thread's DB write and its `bus.publish` are not atomic, so a
  subscriber reading only one source can miss an event straddling the subscribe.
  This assumes `EventBus.history()` stays cheap and monotonic in `seq`.

### 2026-09-03 — llm/ + agents/ + orchestrator/ (builder B)

1. **`Game(cfg, client, bus, on_event=None, engine=None)`** — added the `engine`
   parameter. It takes one module-like object exposing the §1 public names
   (`RNG`, `GameState`, `Grid`, `Combatant`, `Event`, `Action`, `ActionTemplate`,
   `build_character`, `monster_to_combatant`, `legal_actions`, `apply`,
   `start_combat`, `advance_turn`, `combat_over`, `skill_check`). When omitted,
   `orchestrator.game.default_engine()` flattens `engine.{dice,state,events,characters,actions,srd}`
   into that namespace. Rationale: lets the orchestrator be built and tested in
   parallel with the engine (`tests/orchestrator/fake_engine.py`), and gives the
   web layer a seam for future variants. `PlayerAgent`/`DMAgent` take the same
   optional `engine=` kwarg so they construct the engine's real `Action` class.

2. **Gap in §1.3: no PC equivalent of `monster_to_combatant`.** `build_character`
   returns a `CharacterSheet`, but nothing turns one into a `Combatant`. The
   orchestrator constructs `Combatant` directly from the sheet per §1.4, using
   `engine.starting_resources(sheet)` when the engine exposes it, and preferring
   `engine.pc_to_combatant` / `sheet_to_combatant` / `combatant_from_sheet` if any
   of those ever appear. **Engine builder: adding `pc_to_combatant(sheet, position)`
   would be the cleaner home for this.**

3. **`agents/common.py`** (not named in §3) holds the shared plumbing: prompt
   loading, tolerant JSON extraction, `AgentOutputError`, word clamping, and the
   `rules_digest()` wrapper that calls `engine.srd.rules_digest()` and falls back
   to a built-in digest when the engine is not importable (so agent tests run
   standalone).

4. **MockLLMClient shapes.** §2 lists five; the loop needs three more, so the mock
   also answers `scene_options`, `scene_choice`, and `player_speech`. Every shape —
   `dm_narration` included — returns a JSON object (`{"narration": ...}`,
   `{"summary": ...}`, `{"options": [...]}`, `{"speech": ...}`); the agents parse
   these tolerantly and fall back to treating the raw text as prose. The mock also
   fills `needs=["path"]`/`["point"]` params from the `suggested=` list that
   `render_actions` prints, so moves carry a real destination.

5. **Signature tweaks.** `DMAgent.monster_action(view, templates, monster_id,
   monster_name=None)` — the name makes for better tactics prompts than the id.
   `summarize(..., *, role="summarizer")` — keeps summary spend a separate ledger
   row. `agents.views.party_summary(state)` added for `DMAgent.open_scene`.

6. **Event sequence numbers are re-assigned by the orchestrator** as it publishes,
   so each game exposes one gapless monotonic stream regardless of how the engine
   numbers its own events (`web/` uses `(game_id, seq)` as a primary key).

7. **`EventBus.close()`** added: pushes the `None` end-of-stream sentinel to every
   subscriber. `Game.run()` always calls it in a `finally`. A subscriber queue that
   fills up is dropped rather than allowed to block the game thread.

8. **`GameConfig` extras** (all optional, defaults preserve §4 behaviour):
   `title`; and in `scenario`: `scenes: [{title, description, location, objectives}]`,
   `beats_per_scene` (default 2), `objectives`, `location`. `GameConfig.load(path)`
   and the helpers `max_scenes`, `opening`, `encounters()`, `encounter_for(trigger)`.

9. **`Game` extras**: `join(timeout)`, `outcome: str`, `error: str | None`, and
   `id`/`outcome`/`error` in `snapshot()`.

10. **Budget** is checked before every LLM call (not after), so a game can never
    overshoot by more than one call. Exceeding it emits a `cost` event and sets
    status `budget_exceeded`; a final `cost` event with the full ledger is emitted
    on every exit path.

### 2026-09-03 — llm/ — per-model request parameters (live-mode fix)

1. **`request_params_for(model, *, temperature) -> dict`** and the table
   **`MODEL_RULES`** in `llm/client.py` decide, per model-id prefix, which
   sampling and `thinking` fields go on the wire. `AnthropicClient.complete`
   splats the result into `messages.create`; the `LLMClient.complete` signature
   in §2 is unchanged, so the agents keep passing `temperature` (DM 0.8, players
   0.8, summarizer 0.3) and the client decides. `MockLLMClient` ignores the rule.
   A model swap via `DND_*_MODEL` is one row in the table.

   | model id prefix | sampling (`temperature`/`top_p`/`top_k`) | `thinking` field |
   |---|---|---|
   | `claude-sonnet-5`, `claude-opus-5`, `claude-opus-4-7`, `claude-opus-4-8` | dropped | `{"type": "disabled"}` |
   | `claude-fable` | dropped | omitted (`disabled` is rejected there) |
   | anything else (`claude-haiku-4-5-*`, `claude-sonnet-4-6`, …) | forwarded as before | omitted |

   So with the defaults the DM (`claude-sonnet-5`) sends
   `model, system, messages, max_tokens, thinking={"type": "disabled"}` and the
   players/summarizer (`claude-haiku-4-5-20251001`) send
   `model, system, messages, max_tokens, temperature`.

   Rationale (Claude API reference, cached 2026-06-24): on Sonnet 5 / Opus 5 /
   Opus 4.7–4.8 / Fable the sampling parameters are removed and return HTTP 400
   — every DM call was failing outright. Separately, Sonnet 5 runs *adaptive*
   thinking when the field is absent, thinking tokens are billed as output and
   `max_tokens` caps thinking + reply together; with this project's 200–600
   token per-call caps an unasked-for think would consume the budget and hand
   back truncated JSON. `{"type": "disabled"}` keeps the caps and the frugality
   requirement meaningful. Haiku 4.5 is the opposite: thinking is off unless
   enabled, `disabled` is not accepted, and sampling still is — so nothing
   changes for it. `budget_tokens` is never sent (400 on Sonnet 5).

2. **Caching note, not changed now.** The minimum cacheable prefix is 4,096
   tokens on Haiku 4.5 and 1,024 on Sonnet 5; shorter prefixes silently do not
   cache. The player system block (role rules + SRD digest + sheet) is roughly
   1.7k tokens and the summarizer's about 150, so the `cache_control` marker on
   Haiku calls is inert today — those calls pay full input price every turn.
   The DM block clears Sonnet 5's 1,024 floor and does cache. Left as-is:
   padding prompts to reach the floor would cost more than it saves at current
   game lengths; it is a future cost lever if player calls dominate spend.

### 2026-09-03 — llm/ — multi-provider routing

Any seat at the table (DM, each player, summarizer) can be served by a
different platform. `LLMClient.complete` in §2 is unchanged; what changed is
which object sits behind it in live mode and how a model id reaches a seat.

1. **`llm/providers.py` — `PROVIDERS`**, one row per platform, routed by
   model-id prefix (`provider_for(model)`). Every URL and price was read from
   the cited page on 2026-09-03.

   | name | key env var | base URL | routes prefixes | dialect | `json_object` | max-tokens field |
   |---|---|---|---|---|---|---|
   | anthropic | `ANTHROPIC_API_KEY` | (native SDK) | `claude-` | anthropic | no (asked in system text) | `max_tokens` |
   | openai | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `gpt-`, `chatgpt-`, `o1`, `o3`, `o4` | openai_compat | yes | `max_completion_tokens` (`max_tokens` is deprecated there) |
   | xai | `XAI_API_KEY` | `https://api.x.ai/v1` | `grok-` | openai_compat | yes | `max_tokens` |
   | mistral | `MISTRAL_API_KEY` | `https://api.mistral.ai/v1` | `mistral-`, `ministral-`, `magistral-`, `codestral-`, `open-mistral`, `open-mixtral`, `pixtral-` | openai_compat | yes | `max_tokens` |
   | gemini | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-` | openai_compat | **no** (unconfirmed on the compat layer; left off) | `max_tokens` |
   | deepseek | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` (the docs post to `/chat/completions` with no `/v1`) | `deepseek-` | openai_compat | yes (needs "json" in the prompt — the suffix has it) | `max_tokens` |
   | siliconflow | `SILICONFLOW_API_KEY` | `https://api.siliconflow.com/v1` (the international platform; `.cn` is a separate platform with its own keys) | *(none — host, `siliconflow:<id>` only)* | openai_compat | yes, except the DeepSeek R1/V3 ids (`json_mode_except`) | `max_tokens` |
   | deepinfra | `DEEPINFRA_API_KEY` | `https://api.deepinfra.com/v1/openai` | *(none — host, `deepinfra:<id>` only)* | openai_compat | yes | `max_tokens` |

2. **`OpenAICompatClient(provider, key)`** (`llm/compat.py`, httpx) serves
   every `openai_compat` row: `POST <base_url>/chat/completions`, `Bearer`
   key. Mapping from the §2 call:
   - `system` (str or Anthropic content blocks) → one `{"role": "system"}`
     message, block texts joined with blank lines, `cache_control` dropped;
     with `json_only=True` the same `JSON_ONLY_SUFFIX` the Anthropic client
     appends is appended here, and `response_format: {"type": "json_object"}`
     is added **only** when the row says the provider accepts it. Non-JSON
     calls send neither.
   - `messages` → `{"role", "content"}` with content flattened to text.
   - `max_tokens` → the row's field; `temperature` and extra fields per
     **`COMPAT_RULES`** (first prefix match; default = forward temperature,
     nothing extra): `gpt-5-*`/`gpt-5`/`gpt-6*` drop temperature and send
     `reasoning_effort: "minimal"`, `gpt-5.x` drop temperature and send
     `"none"`; `grok-4.6`/`grok-4.5`/`grok-4.20-multi-agent` keep temperature
     and send `reasoning_effort: "low"`; `gemini-2.5-flash*` send `"none"`,
     other `gemini-*` send `"minimal"` (2.5 Pro and 3.x cannot switch thinking
     off); `deepseek-*` send top-level `thinking: {"type": "disabled"}`
     (thinking is on by default there). Same reason as `MODEL_RULES`: hidden
     reasoning is billed as output and counts against the 200–600-token caps.
   - Response → `LLMResponse`: `choices[0].message.content` (empty string when
     null), `finish_reason` → `stop_reason`, `usage.prompt_tokens −
     cached` → `input_tokens`, `cached` → `cache_read_tokens` where `cached`
     is `prompt_tokens_details.cached_tokens` (OpenAI/xAI/Gemini/Mistral) or
     `prompt_cache_hit_tokens` (DeepSeek), `completion_tokens` (reasoning
     tokens included) → `output_tokens`, `cache_write_tokens` = 0 always.
   - Retry: 429 / 5xx / timeout / transport error → backoff 0.5·2ⁿ s capped
     at 8, three attempts; any other 4xx → `LLMError` at once with the
     provider name, status and the server's error message. The key is never
     in a message.

3. **`RouterClient`** (`llm/router.py`) implements `LLMClient`, builds one
   client per provider lazily (`AnthropicClient` for the anthropic row, the
   compat client otherwise), and is what `web/factory.py` and
   `orchestrator/cli.py` hand to `Game` in live mode. Mock mode still gets
   `MockLLMClient` and is byte-for-byte unchanged (verified: same seed → same
   `--json` stream before and after, and with or without seat overrides).
   Unknown prefix → `LLMError` listing the known prefixes; missing key →
   `LLMError` naming the env var.

4. **Per-seat models.** A party member may carry `"model": "<id>"`, which
   overrides `player_model` for that PC (`GameConfig.player_model_for(spec)`);
   `dm_model` / `summary_model` stay per game. `GameConfig.seat_models()`
   returns `{"dm", "summary", "player:<id>"...} → model`. `Game.seat_models`
   records combatant id → model, and `snapshot()["models"]` exposes
   `{"dm", "summary", "players": {id: model}}`. The ledger row per seat
   (`player:<id>`) is priced at that seat's model.

5. **Fail fast on price (live mode only).** `RouterClient.preflight(seats)`
   runs at game creation (factory and CLI) and raises one `LLMError` listing
   every seat whose model has no provider, whose provider's key is unset, or
   which has **no row in `PRICES`** — the budget stop is blind otherwise.
   `DND_ALLOW_UNPRICED=1` waives only the price check (the model then bills at
   the $2/$10 default). The web API surfaces it as the 400 it already returns
   for a bad config; the CLI prints it and exits 2. `_check_budget` is
   unchanged and still runs before every call regardless of provider — the
   router is below it.

6. **`llm/cost.py`.** `PRICES` gained rows for OpenAI (`gpt-6-astra`,
   `gpt-5.6-*`, `gpt-5.5[-pro]`, `gpt-5.4[-mini|-nano|-pro]`, `gpt-5.2[-pro]`,
   `gpt-5.1`, `gpt-5[-mini|-nano|-pro]`), xAI (`grok-4.6`, `grok-4.5`,
   `grok-4.3`, `grok-4.20-0309-*`, `grok-build-0.1`), Mistral
   (`mistral-medium-latest`, `mistral-medium-3-5`, `mistral-small-latest`,
   `mistral-large-latest`, `codestral-latest`, `ministral-{3b,8b,14b}-latest`),
   Gemini (`gemini-3.8/3.7/3.6-flash` at promotional rates through
   2026-12-31, `gemini-3.5-flash[-lite]`, `gemini-3.1-flash-lite`,
   `gemini-3.1-pro-preview`, `gemini-2.5-pro`, `gemini-2.5-flash[-lite]`) and
   DeepSeek (`deepseek-v4-flash`, `deepseek-v4-pro`, at **peak** rates).
   Lookup is exact, then the longest prefix ending on an id boundary
   (`-`, `.`, end) — so `gpt-5.4-nano-<date>` prices as `gpt-5.4-nano`, never
   as `gpt-5`. `has_price(model)` is that test. Cache reads still cost
   0.1× input by default; `CACHE_READ_PRICES` overrides it where a provider's
   discount differs (xAI 0.15–0.25×, DeepSeek ~0.03×) via
   `cache_read_price_for`. The Anthropic rows and the §2 formula for them are
   unchanged.

7. **Not supported across providers, deliberately:** Anthropic prompt caching
   (`cache_control` is stripped; whatever a provider caches on its own is
   only *priced*, never requested); Anthropic thinking controls (the compat
   rules above are the nearest knob each platform offers, and some — Gemini
   2.5 Pro / 3.x, gpt-6-astra — cannot switch reasoning off, only down);
   structured-output schemas (only `json_object` mode, and only where
   confirmed); streaming; tool use. Gemini's `json_object` support on the
   compat layer could not be confirmed from its docs and is left off, so
   Gemini seats rely on the JSON instruction in the system text plus the
   agents' tolerant parser, as Anthropic seats already do.

8. **Not read, so not set:** an OpenAI statement on whether `temperature` is
   rejected on the gpt-5 family (the reference only says support "can
   differ ... particularly for newer reasoning models"); dropping it can
   never 400, so it is dropped. `requirements.txt` now names
   `httpx>=0.27,<1.0` explicitly (it was already installed as anthropic's
   dependency).

9. **Explicit `provider:model` form and the two hosts** (same day, later).
   SiliconFlow and DeepInfra are *hosts*: they serve other people's models
   under namespaced ids (`deepseek-ai/DeepSeek-V3.2`, `Qwen/Qwen3-32B`) that
   do not identify the host, and `deepseek-` already routes to DeepSeek's own
   API. So their rows carry **no prefixes** (`HOSTS` = the prefix-less rows)
   and a seat reaches them only through the explicit form
   **`<provider name>:<model id>`**, which `split_model` parses and which
   overrides prefix routing for every row (`openai:gpt-5.4-nano` is legal and
   identical to `gpt-5.4-nano`). The rule, everywhere a model id is read:
   - `provider_for`: explicit name → that row (an unknown name → None, never
     a prefix fallback on the remainder); bare id → prefix as before.
   - `RouterClient`: an unknown provider name is an `LLMError` naming it and
     the row names; a bare **namespaced** id (contains `/`) is an `LLMError`
     saying to use the `provider:model` form and listing `<host>:<id>` for
     each host; the old unknown-prefix message now also mentions the form.
     Preflight surfaces both per seat. The full seat id is what
     `GameConfig.seat_models()`, `snapshot()["models"]`, the ledger row and
     the preflight messages carry.
   - **Wire model** = the part after the colon. The compat adapter strips it
     itself (`build_body`'s `model`) and stamps the *full* id back onto the
     `LLMResponse` (a bare id still keeps the server's model string); the
     router hands the native Anthropic SDK the bare id and re-stamps the full
     one, so `Ledger` always keys on the seat as configured.
   - **Prices** (`PRICES`, `CACHE_READ_PRICES`) are keyed by the full
     `provider:model` string, case-sensitive after the colon, so one model is
     priced at each host's own rate and the bare host id has no row (an
     unprefixed `Qwen/Qwen3-32B` cannot reach preflight's price check anyway).
     Rows read 2026-09-03 — SiliconFlow (`siliconflow.com/models`):
     `deepseek-ai/DeepSeek-V3.2` $0.27/$0.42, `deepseek-ai/DeepSeek-V3`
     $0.25/$1.00, `Qwen/Qwen3-32B` $0.14/$0.57, `Qwen/Qwen3-14B` $0.07/$0.28;
     DeepInfra (`deepinfra.com/pricing`): `deepseek-ai/DeepSeek-V3.2`
     $0.26/$0.38 (cached input $0.13 → `CACHE_READ_PRICES`), `Qwen/Qwen3-32B`
     $0.08/$0.28, `meta-llama/Llama-3.3-70B-Instruct-Turbo` $0.10/$0.32.
     **Not priced, so not seatable without `DND_ALLOW_UNPRICED`:** Llama 3.3
     70B on SiliconFlow (not listed on the international platform) and the
     non-Turbo `meta-llama/Llama-3.3-70B-Instruct` on DeepInfra (its page is
     a 404).
   - **Request shape per host.** SiliconFlow: `max_tokens`, `temperature`,
     `response_format json_object` except on the DeepSeek R1/V3 ids (its
     json-mode page excludes them — `Provider.json_mode_except`, checked per
     wire model by `accepts_json_mode`), and `enable_thinking: false` on the
     Qwen3 series and DeepSeek-V3.1/V3.2 (the reference says it defaults to
     true there). Its usage object has no cache-hit field, so cache reads are
     never counted on that host. DeepInfra: `max_tokens`, `temperature`,
     `response_format json_object`; its `reasoning_effort` (none|low|medium|
     high) is documented for the DeepSeek-V4 family and a few others, not for
     DeepSeek-V3.2 or Qwen3, so nothing thinking-related is sent to those.
     `COMPAT_RULES` may be keyed on the full seat id (`siliconflow:qwen/qwen3-`)
     and is matched full-id first; the bare wire id is consulted only when
     the explicit provider is the one its prefix routes to anyway — so
     DeepSeek's `thinking` field never reaches `deepinfra:deepseek-ai/…`.
   - `bin/dndsim` knows `SILICONFLOW_API_KEY` and `DEEPINFRA_API_KEY` (adopted
     from `/etc/environment`, unset for pm2's launch). `CARTESIA_API_KEY`,
     also in that store, is text-to-speech and is not a provider.

### 2026-09-03 — engine (builder A: `engine/actions.py`, `engine/srd.py`, `engine/characters.py`)

The SRD tables in `engine/data/` are consumed exactly as shipped (49 spells, 29
monsters, 38 weapons, 13 armors, 15 conditions, 17 equipment items, 4 races,
4 classes). **No data record was changed.** What the engine reads, and the two
places it derives something the record does not literally carry:

1. **Race names.** `examples/*.json` (and PLAN.md) write races as
   `"Dwarf (Hill)"` / `"Elf (High)"` / `"Halfling (Lightfoot)"`; `races.json`
   names them `"Hill Dwarf"` etc. `srd.race()` now accepts both forms
   (`"X (Y)"` → `"Y X"`). No change to either file.

2. **`characters.pc_to_combatant(sheet, position=(0, 0))`** added (closes B.2),
   plus `characters.fresh_turn(speed)`; `monster_to_combatant` fills `turn` the
   same way (the turn budget lives on state, never in the data).
   `starting_resources` reads the class `resources` table
   (`{"second_wind": {"1": 1}, ...}`, count at the highest level reached) and
   `sneak_attack_dice`; `_default_spells` interleaves spell levels so a level-5
   caster prepares 3rd-level spells within its prepared budget.

3. **Spell `effect` vocabulary consumed** (spells.json). Kinds: `attack`
   (spell attack, or `auto_hit`; `rays` = separate bolts; `add_mod`;
   `choose_damage_type` via `params.damage_type`; riders `speed_reduction` +
   `effect_duration_rounds`, `no_reactions_rounds`, `no_healing_rounds`,
   `grants_advantage_rounds`, `drain_half`; `summoned_weapon` = Spiritual
   Weapon strikes as a bonus action for `duration_rounds`), `save` (damage,
   `half_on_save`, `area` shapes sphere/cone/cube/line on the grid, `push_ft`,
   `ignores_cover`, `persistent_aura`: Spirit Guardians = aura around the caster
   checked at the start of each enemy turn, Flaming Sphere = a placed sphere
   that burns creatures ending their turn within 5 ft and can be moved 30 ft
   and rammed as a bonus action), `debuff` (save-or-condition with
   `condition_duration`, `repeat_save` at the end of the target's turn,
   `only_types`/`immune_types`, `ends_on_attack`), `heal` (`damage` is the
   healing dice, `add_mod`, Disciple of Life +2+slot), `buff` (`ac_bonus`,
   `set_base_ac`, `attack_bonus_die`/`save_bonus_die`/`check_bonus_die` with
   `uses`, `temp_hp`, `max_hp_bonus`, `attackers_disadvantage`,
   `speed_multiplier`, `extra_action` (Haste: one extra attack/Dash/Disengage/
   Hide per turn; the target loses its next turn when Haste ends),
   `fly_speed` (recorded only — the grid is flat), `max_healing`,
   `save_advantage`, `conditions_applied`, `trigger: attacked` = Shield,
   auto-cast as a reaction only when it converts a hit into a miss and absorbs
   Magic Missile), `utility` (`stabilize`, `removes_conditions`, `teleport_ft`,
   `dispel_level`; utility records with none of these — Light, Mage Hand,
   Prestidigitation, Detect Magic — are never enumerated in combat).
   `cantrip_scaling` multiplies the dice at caster levels 5/11/17. `upcast`
   strings are parsed as `+NdF/slot`, `+N target|dart|ray/slot`, `+N/slot`
   (flat), with an optional `/2slots` step. The top-level `range` string
   ("Self (15-foot cone)") decides whether a shape originates at the caster.
   Only `summon_none` never appears in the data; the resolver would emit the
   cast event and nothing else.

4. **Monster fields consumed** (monsters.json): `actions[*].kind`
   (`melee_weapon|ranged_weapon|melee_or_ranged`), `attack_bonus`, `reach`,
   `range`, `damage`, `damage_type`, and `extra[*]` riders (`save`+`dc` with
   `condition`/`duration`/`repeat_save`/`immune_races`, `damage` +
   `half_on_save`, `escape_dc` → repeat STR save, `max_hp_reduction`,
   `ability_drain` + `amount`); `multiattack` (ordered names → attacks per
   Attack action, in that order); `bonus_actions` (`disengage`/`hide` →
   Nimble Escape, `dash_toward_enemy` → Aggressive); `spellcasting`
   (`cantrips` + `spells: {level: [...]}`, `slots`, `dc`, `attack_bonus`,
   `level`); "Recharge N-6" parsed from an action's `desc`; traits by `id`, or
   by slugified `name` when the record has no id (Nimble Escape): `pack_tactics`,
   `martial_advantage` (`damage`), `undead_fortitude`, `regeneration` (`amount`,
   `stopped_by`), `stench_aura` (`save`, `dc`, `condition`, `radius`),
   `turning_defiance`, `dark_devotion`, `two_heads`
   (`save_advantage_conditions`). Not modelled: `surprise_attack`, `rampage`,
   sunlight traits, Parry / Redirect Attack reactions.
   **Gap:** the Goblin Boss record lists `["Scimitar", "Scimitar"]` with no
   marker for the SRD's "the second attack has disadvantage"; the engine
   honours a `{"name": ..., "disadvantage": true}` entry if one is ever added
   but the shipped record plays both attacks straight.

5. **Conditions** come from conditions.json flags: `attack_by`/`attack_own`
   (prone's `"special"` = advantage within 5 ft, disadvantage beyond),
   `auto_fail_saves`, `save_disadvantage`, `auto_crit_within_5`,
   `incapacitated`, `speed_zero` (via state.py). Two engine-only markers ride on
   `Combatant.conditions` without a data record: `hidden` (after Hide) and
   `turned` (Turn Undead). Races: `saving_throw_advantages` tags (`poisoned` is
   matched against poison damage too), `damage_resistances`, `hp_per_level`,
   `weapon_proficiencies`, feature `lucky` (reroll natural 1 on attacks).
   Equipment: `use: {kind: heal, amount, cost}` drives `use_item`.

6. **Enumeration policy** (`legal_actions`), for token frugality and sane mock
   play: attack templates target living, non-downed enemies only (nearest 5 for
   ranged weapons, nearest 4 for single-target spells) — a creature at 0 HP is
   never offered as a target, though damage-while-down is still resolved
   (opportunity attacks, areas); upcast variants only for area / multi-target
   spells (base + highest slot); one `move` per turn plus one per Dash, with up
   to 6 `params.suggested` squares (approach the nearest enemies, then two
   kiting squares; a turned creature only gets squares away from its turner);
   `path` accepts `[[x,y], ...]`, `[x,y]` or a string; multi-target spells fall
   back to `params.suggested` when the chooser sends an empty `targets` list;
   the SRD bonus-action-spell rule is enforced; `ready`, `death_save` and
   `skill_check` are not enumerated (death saves are automatic in
   `advance_turn`; skill checks go through `skill_check()`).

7. **Turn mechanics.** `advance_turn` ends the current turn (repeat saves, then
   duration ticks, timed buffs, concentration duration, Flaming Sphere burn),
   then advances past dead creatures and rolls the automatic death save for
   dying ones (skipping them unless a natural 20 revives them), then runs
   start-of-turn effects for the next actor (regeneration, recharge, auras).
   `start_combat(state, rng_state)` uses `rng_state` when given, else
   `state.rng`. `combat_over` treats "conscious" as `hp > 0 and not dead`.
   Transient per-creature effects live in `Combatant.flags` (`buffs`,
   `ac_bonus`, `speed_penalty`, `speed_multiplier`, `dodging`,
   `spiritual_weapon`, `spirit_guardians`, `flaming_sphere`, ...), which
   state.py already serializes; concentration-sourced conditions and buffs
   carry `source="<caster_id>:<spell>"` so breaking concentration can find them.

8. **Events.** Attack events carry the damage roll inline in `text` (per the
   §1.5 example) and `data.{hit,crit,mode,reasons,ac,damage}`; `save` events
   carry `data.target`. Reactions (opportunity attacks, auto-cast Shield,
   Uncanny Dodge) are emitted *after* the attack event they answer.

### 2026-09-03 — agents/ + orchestrator/ — one line per turn, not one per action

The first live transcript read as a monologue: a single Fighter turn (four
thrown handaxes under Action Surge, then a move) printed seven dialogue lines,
each a rewording of the one before, and skeletons repeated "click…" every time
they were asked to act. Nothing was wrong mechanically — `_run_turn` asks the
agent for up to `MAX_ACTIONS_PER_TURN` actions and every `Action.speech` was
emitted — but a turn is a beat at the table, not a speech per die roll.

1. **`PlayerAgent.choose_action(view, templates, *, speak=True)`** and
   **`DMAgent.monster_action(view, templates, monster_id, monster_name=None, *,
   speak=True)`**. `speak=False` renders the action prompt with `"speech": null`
   in its RESPONSE_SHAPE and drops any speech the model returns anyway. The
   orchestrator passes `speak=False` for every action after the actor's line has
   landed, so the extra calls in a turn are also a little cheaper.
   `agents.common.speech_fields(speak, words)` builds the two placeholders
   (`{speech_shape}`, `{speech_rule}`) that `player_action.txt` and
   `dm_monster_action.txt` now take.

2. **`PlayerAgent.choose_scene_action(view, options, said=None)`** — `said` is
   the lines the party has already spoken this exploration beat, rendered into
   `player_scene_choice.txt`. Each character was being prompted in isolation
   with the same option list, so all four argued for the same option in their
   own words; now a character who has nothing to add votes and stays quiet.

3. **`Game._say(actor_id, name, speech) -> bool`** is the only path to a
   `dialogue` event. Against each of the last `DIALOGUE_MEMORY` (8) lines it
   drops the new one when the content words are identical, or — for lines of
   `FUZZY_MIN_WORDS` (4) content words or more — when the Jaccard overlap is
   ≥ `SELF_REPEAT` (0.5) for the same speaker or ≥ `ECHO_REPEAT` (0.7) for a
   different one. `_line_key` decides what "content" means: function words out
   (`_STOPWORDS`), everything else in, length no test of meaning, words
   tokenized as Unicode so a game played in Cyrillic or CJK is judged on its
   words rather than on the empty set an ASCII-only pattern leaves. A line
   with no words at all (pure punctuation) is never suppressed.

   Two rules exist because a word-overlap heuristic reads wording, not sense,
   and the failures are asymmetric — a false repeat silences a real
   contribution, while a missed one costs a line of noise. So:

   - **Negation is compared before overlap.** "Open the door" and "do not open
     the door" share every content word and mean opposite things, so `_line_key`
     returns a negation flag beside the word set (`_NEGATIONS`, apostrophes
     stripped first so "don't" arrives as "dont"), and a line that negates is
     never a repeat of one that does not.
   - **Overlap is not trusted on short lines.** In "heal me" / "heal him" or
     "I go left" / "I go right" one word is most of the line, and the score is
     as high as a real repeat's; below `FUZZY_MIN_WORDS` only identical content
     counts, which is what a repeated bark ("Click.") actually is.

   The thresholds are judgment, not measurement; all four constants are module
   constants in `orchestrator/game.py` so they can be tuned.

4. **Speech word caps** dropped from 40 to 20 in combat (`SPEECH_WORDS` in both
   agents) and to 25 for scene choices (`player.SCENE_SPEECH_WORDS`), and the
   prompts now say that silence is the normal case.

5. **`dialogue` event shape.** `text` is now the bare spoken line and the
   speaker's name lives in `data["speaker"]`; the orchestrator used to emit
   `"Name: line"` while `web/static/app.js` separately prefixed the name from
   `ev.actor`, so the UI printed it twice. `agents.common.event_text(ev)` puts
   the name back for prompt context, and both `_event_lines` helpers use it.

6. **`MockLLMClient`** honours `"speech": null` in the prompt's shape, as a live
   model would. Its canned speech pool is 8 lines, so in a mock game the
   repetition guard suppresses nearly every line after the first eight — an
   artifact of the fixture, not of a live game.

### 2026-09-04 — orchestrator/ + web/ + agents/ — narration hold, and word caps as a listening budget

The spectator page reads the game aloud, and text arrives far faster than a
voice speaks it, so the simulation ran minutes ahead of what a listener heard.
Two changes, at the two ends of that gap.

1. **`Game.hold(seconds) -> float` / `Game.release()` / `Game.hold_remaining()`**
   — a renewable lease that makes `_gate()` wait. Not `pause()`, on purpose:

   - It **expires by itself** (capped at `MAX_HOLD_SECONDS = 30`, a module
     constant in `orchestrator/game.py`). A spectator whose browser dies
     mid-hold costs the game seconds, where a pause would freeze it for good.
     The client renews every 4 s; `hold(0)`/`release()` lifts it at once.
   - It **does not touch `status`.** Holding is the narration keeping step;
     pausing is the table stopping. They are separately controllable, and a
     held game still reads as `running`, so the UI's pause/resume stay
     meaningful and the two never fight.
   - `stop()` releases it, so a held loop notices the stop immediately.

   Exposed as `POST /api/games/<id>/hold {"seconds": n}` → `202 {"id",
   "status", "holding": <seconds granted>}`. Non-numeric `seconds` is a 400;
   a game not running in this process is a 409, as for the other controls.
   Unlike them it writes no snapshot — it is called every few seconds.
   `Game.snapshot()` gains `"holding": bool` so other spectators can see why a
   game is quiet.

2. **`Game.pause()` before `run()` now reads back as `paused`.** `run()` set
   `status = "running"` unconditionally, so a pause landing in the gap between
   `start()` and `run()` left the loop blocked at its first gate while
   reporting itself running — and a UI that offers Resume only for `"paused"`
   had no way to let it go. `pause()` now also marks `"created"` games paused,
   `run()` takes the status from the resume flag, and `resume()` restores
   `"running"` or `"created"` depending on whether `run()` has taken over
   (`Game._live`).

3. **The DM's word caps are a listening budget.** §3's `narrate` is now **≤60
   words**, not 120: at ~150 words a minute of speech, 120 words per resolved
   turn is a minute of narration per six seconds of combat and the voice can
   never catch up. Also `SCENE_WORDS` 150→90, a new `EPILOGUE_WORDS` 100 (the
   epilogue no longer borrows `SCENE_WORDS`), a new `ADJUDICATION_WORDS` 45 for
   the setup line in `adjudicate` (it used to borrow `NARRATION_WORDS`), and
   `player.speak` 60→35 (`FREE_SPEECH_WORDS`). Max-token ceilings came down to
   match. The prompts ask for the same numbers and say why: every line is read
   aloud while the game waits.

The web client's half of this — narration as a playhead over the transcript
rather than a queue, so pausing, backgrounding the tab and reloading all leave
a resumable mark — is UI, not contract; it is described in the README.
