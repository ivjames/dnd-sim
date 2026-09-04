# CONTRACTS — binding interfaces between layers

Python 3.11. Type hints everywhere. Dataclasses (or plain dicts where noted) — **no Pydantic**, keep deps minimal (`flask`, `anthropic`, `httpx`, `pytest`, and `boto3` for Polly narration — see the 2026-09-04 `tts/` amendment).
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
    pronouns: str                      # as stated, or ""; read by agents.views.pronouns_for and tts/. No rules meaning
    gender: str                        # the older spelling of the same answer, still read underneath it

def build_character(spec: dict, rng: RNG) -> CharacterSheet
# spec: {"id","name","race","klass","level","abilities": {"STR":15,...} | "standard_array" | "point_buy_default", "equipment": "default"|[...], "spells": "default"|[...], "persona": str, "pronouns": str}
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
    def path(state, start, goal, max_ft, mover_id=None, threat=None) -> list[tuple[int,int]] | None   # BFS respecting walls/occupancy, difficult terrain costs double; with threat={square: frozenset(enemy_ids)} an equal-length route that provokes fewer opportunity attacks wins the tie
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
    def add_usd(self, role: str, usd: float, **counters: int) -> float   # a priced-elsewhere service; see the 2026-09-04 tts amendment
    total_usd: float ; by_role: dict[str, dict]   # {"dm": {"calls","in","out","usd"}, "player:pc_1": {...}, "narrator": {"clips","chars","usd"} — `calls` stays 0, it means MODEL calls}
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
    party: list[dict]                # character specs (see build_character); may also carry "model" (per-seat), "age" (voice casting; never reaches the engine) and "pronouns" (voice casting + the DM's pronouns column; carried on CharacterSheet, read by no rule)
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
GET  /api/auth                          {"writes": "token"|"unconfigured", "header", "authenticated"} — may this caller write?
POST /api/games          {config}       → {"id", "status"}   (creates + starts)   **write token**
GET  /api/games                         [{id, status, created_at, title, round, cost_usd}]
GET  /api/games/<id>[?at_seq=n]         snapshot + config + ledger; `at_seq` pins `snapshot.state`/`round` to the board archived at or before event n and reports it as `snapshot_seq` (status/ledger/cost stay live)
GET  /api/games/<id>/events?after=seq   history (from SQLite)
GET  /api/games/<id>/stream             SSE: `event: <kind>` `data: <Event JSON>`; on connect replays history after ?after=, then live; heartbeat comment every 15s; on finish sends `event: end`
POST /api/games/<id>/pause | /resume | /stop                                   **write token**
POST /api/games/<id>/hold   {"seconds","client"} → 202 {"holding": <granted>}  per-client narration lease; expires by itself, leaves status alone
POST /api/games/<id>/note   {"text"}    → 202                                  **write token**
GET  /api/tts                           {"available":bool,"engine","monster_engine","language","max_chars","price_per_million_chars","monster_price_per_million_chars","config"} — can this server render voices?
POST /api/tts/cast       {party}       {"available":bool,"seats":[{"id","voice","language","accent","gender"}]} — who will read each seat, before the game exists; casts nothing, spends nothing
GET  /api/games/<id>/tts?key=&text=&v=  audio/mpeg for one narrated line; 402 over budget, 503 no service, 502 synthesis failed. `v` is the probe's `config`, a cache-buster the server ignores
```
Routes marked **write token** require the `X-Dnd-Token` header to match
`DND_WRITE_TOKEN`; every other route is anonymous. See the 2026-09-04 `web/`
amendment "a write token on the routes that mutate or spend".
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

   - Leases are **per client** (`hold(seconds, client="")`), and the loop waits
     for the longest outstanding. One global deadline would make every
     spectator the last writer of everyone else's: a second tab catching up
     and releasing would cut short a first tab that is still behind, and a
     renewal in flight when another released would reinstate the hold.
     `release(client)` drops one; `release_all()` drops every one, and is what
     `stop()` calls. The table is bounded at `MAX_HOLD_CLIENTS = 64` — ids come
     from callers, and leases expire, so evicting the soonest to go costs at
     most that one lease.

   Exposed as `POST /api/games/<id>/hold {"seconds": n, "client": id}` → `202
   {"id", "status", "holding": <seconds granted>}`. Non-numeric `seconds` is a
   400; a game not running in this process is a 409, as for the other controls.
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

### 2026-09-04 — agents/ + orchestrator/ + web/ — how improvised the players are

Players were sampling at a hard-coded 0.8 and converging: the same character
reached for the same opening line and the same attack every round, and the
repetition guard from the 2026-09-04 amendment above then swallowed the repeat,
so a converged character went *quiet* rather than saying something new. Two
levers, one mechanical and one prompt-side.

1. **`PlayerAgent(..., temperature=DEFAULT_TEMPERATURE)`** (§3). The value is
   the default for every call the agent makes; `_call` still takes a
   per-call override. `agents.player.DEFAULT_TEMPERATURE` is **1.0** — the top
   of the Anthropic range, not a tuned optimum: it is simply as much variance
   as the API will give a Haiku seat. The DM (0.8) and summarizer (0.3) are
   unchanged; a hotter DM invents world facts, which is the one thing it must
   not do.

2. **`GameConfig.player_temperature: float = DEFAULT_TEMPERATURE`** (§4), with a
   per-seat override in a party spec's `temperature` field and the accessor
   **`player_temperature_for(spec)`**, alongside the existing
   `player_model_for`. `Game` records `seat_temperatures: dict[str, float]`
   beside `seat_models` and passes each seat's value to its `PlayerAgent`.

   **`agents.player.clamp_temperature(value, default=DEFAULT_TEMPERATURE)`**
   is the single definition of what is acceptable: `[0.0, 1.0]`, with anything
   unreadable (a string, `None`, NaN) falling back to `default`. It clamps
   rather than raises — a silly number in a scenario file should cost variety,
   not kill a live game mid-scene — and the ceiling is 1.0 because that is
   Anthropic's maximum and every default seat is an Anthropic model. An
   OpenAI-compatible host would accept 2.0; a config that only works on some
   seats is worse than one that works on all of them. `orchestrator/config.py`
   imports the helper rather than restating the range (layering allows
   `orchestrator → agents`).

3. **Prompt changes, `agents/prompts/player_*.txt`.** `player_system.txt` gains
   a "PLAY LIKE A PERSON, NOT A PROCEDURE" block: react to the turn actually
   handed to you, check the legal list for something better than last round's
   action before repeating it, hold your own read of the fight, never reuse a
   line. `player_scene_choice.txt` asks for the character's own vote rather
   than the safest or the winning one; `player_speech.txt` asks the line to
   answer *this* moment. The ABSOLUTE RULES are untouched — improvisation is
   scoped to motive, voice and choice of legal action, and never to dice,
   outcomes, or facts about the world.

4. **`web/`**: the new-game panel gains an **Improv (0–1)** field
   (`ng-temp` → `config.player_temperature`, clamped client-side too). No API
   change: `player_temperature` rides in the config body like any other field
   and `GameConfig.from_dict` clamps it server-side regardless.

Determinism is unaffected: `MockLLMClient` ignores `temperature`, so a mock run
at the same seed is still byte-identical.

### 2026-09-04 — new `tts/` + web/ + llm/ — narration moves to Amazon Polly

Spoken narration was the spectator's own `speechSynthesis`: free, private, and
whatever voice the device happened to ship — an iPad reading a dwarf's dying
words in the one voice it has. It is now **Amazon Polly, rendered server-side**,
with the browser's voices kept as a real fallback rather than deleted.

1. **New top-level package `tts/`.** A sibling of `llm/`, not a layer under it:
   an outside service with a price list. It imports nothing from `engine`,
   `agents`, `orchestrator`, `llm` or `web`; only `web` imports it, so the
   layering rule reads `web → orchestrator → agents → llm` **and** `web → tts`.

   ```python
   # tts/voices.py   (pure: no boto3, no I/O)
   @dataclass(frozen=True)
   class Voice:  id: str; language: str; gender: str      # DescribeVoices' Id/LanguageCode/Gender
   @dataclass(frozen=True)
   class Cast:   key: str; voice_id: str; language: str; engine: str; pitch_pct: int; rate_pct: int; vtl_pct: int
   def hash_key(s: str) -> int                            # FNV-1a/32 over UTF-16 code units — the same
                                                          # number as speech.js `hashString`
   def normalize_gender(gender: str) -> str               # "female" | "male" | "" (no constraint)
   def normalize_age(age) -> str                          # "child" | "adult" | "" ; a number is years
   def is_child_voice(voice) -> bool                      # a `Voice` or a bare id, against CHILD_VOICE_IDS
   CHILD_VOICE_IDS: frozenset[str]                        # {"ivy","justin","kevin"} — DescribeVoices has no age
   CHILD_MAX_AGE: int                                     # 12; above it a stated age is an adult
   def cast_for(key, pool, dm_voice="", gender="", engine="standard", age="") -> Cast   # ValueError on an empty pool
   def ssml_for(text: str, cast: Cast, engine: str = "") -> str   # only what the cast's engine accepts
   def source_fingerprint() -> str                        # digest of this module: the casting decides the audio too
   ENGINE_SSML: dict[str, frozenset[str]]                 # engine → {"pitch","rate","vtl"} subset
   def billable_chars(text: str) -> int                   # the words, not the markup
   STANDARD_ENGLISH: tuple[Voice, ...]                    # fallback roster, read 2026-09-04

   # tts/cache.py
   def cache_key(*parts: str) -> str                      # sha256 hex
   class AudioCache:
       def __init__(self, root: str, max_bytes: int = 512*1024*1024)
       def get(key) -> bytes | None ; def put(key, data) -> None ; def prune() -> int

   # tts/client.py
   PRICE_USD_PER_MILLION_CHARS = {"standard": 4.0, "neural": 16.0, "long-form": 100.0, "generative": 30.0}
   class TTSError(RuntimeError)
   @dataclass
   class TTSResult: audio: bytes; cast: Cast; chars: int; usd: float; cached: bool; key: str
   class PollyTTS:
       def __init__(self, cache: AudioCache, *, client=None, region="", engine="neural",
                    monster_engine="standard", language="en-US", dm_voice="Brian", max_chars=400)
       def engine_for(self, key: str) -> str              # monsters keep `monster_engine`
       def available(self) -> bool                        # False = no boto3, or no credentials
       def voices(self, engine="") -> tuple[Voice, ...]   # DescribeVoices once per engine, else STANDARD_ENGLISH
       def cached(self, ckey: str) -> bytes | None        # a clip already paid for
       def exclusive(self, ckey: str)                     # contextmanager: the one-synthesis-per-line gate
       def render(self, key, text, gender="", age="") -> TTSResult   # synthesize; caller holds `exclusive`
       def ssml(self, text: str, cast: Cast) -> str       # `ssml_for` at this instance's engine
       def config_id(self) -> str                         # 12 hex over engine|language|dm_voice|roster|voices.py source
       def cast(self, key: str, gender: str = "", age="") -> Cast
       def cache_key_for(self, key: str, text: str, gender: str = "", age="") -> tuple[Cast, str]
       def synthesize(self, key: str, text: str, gender: str = "", age="") -> TTSResult   # raises TTSError
   def from_env(cache_dir: str) -> PollyTTS | None
   ```

   `client=` is injectable so every path is testable without an AWS account
   (`tests/tts/`). boto3 is imported lazily and its absence is `available() is
   False`, never an ImportError: an install without it still runs.

2. **The wording stays in the browser.** `web/static/speech.js` is unchanged and
   still decides what is said (`phraseFor`) and who says it (`voiceKeyFor` →
   `dm` | `<pc id>` | `npc` | `monster:<id>`). The page sends the key and the
   words; the server deals the key a voice. That is why `hash_key` has to be the
   browser's hash exactly — otherwise an actor changes voice depending on which
   engine spoke. `tests/tts/test_voices.py` drives `node` to assert it.

3. **Two new routes** (§5 above). `GET /api/tts` is the capability probe the
   page asks once at start-up; it also returns `config`, a fingerprint of
   everything process-level that decides a clip (engine, language, the DM's
   voice, the roster `DescribeVoices` returned). `GET
   /api/games/<id>/tts?key=&text=&v=<config>` returns `audio/mpeg` with a
   strong `ETag` (the cache key) and a year-long immutable `Cache-Control`,
   plus `X-Dnd-Voice`. `v` is a cache-buster the server ignores: the rest of
   the URL names only the game, the seat and the words, so without it a
   reconfigured server would leave every browser replaying the old voice out of
   its own cache for a year. The fingerprint covers `tts/voices.py`'s own source
   as well as the settings — the casting and the SSML decide the audio too, and
   a deployment that changes them moves nothing else. Digested rather than a
   hand-bumped constant, because a constant is correct only for as long as
   someone remembers it. The page carries whatever the probe gave it. Refusals are JSON the page can fall back from,
   never a hang: **402** budget spent, **404** no such game, **400** nothing
   sayable or over `max_chars`, **502** Polly failed, **503** no service (or a
   mock game without `DND_TTS=1`).

4. **Narration is charged to `budget_usd`.** `Ledger.add_usd(role, usd,
   **counters)` is new (§2 above) and takes a cost that is not a model call;
   the web layer calls it as `add_usd("narrator", usd, clips=1, chars=n)`, so
   `by_role["narrator"]` (the role name TTS-COSTS.md §3 asks for, and the right
   one: `by_role` holds seats at the table, not technologies)
   sits beside the model rows and the orchestrator's existing budget check stops
   a game whose narration has spent it. Only an actual synthesis is charged — a
   cache hit costs nothing and is charged nothing.

   The end-of-game cost line is built from `to_dict()` rather than by
   iterating `by_role`: a charge from a request thread inserts a key, and an
   unlocked iteration raises "dictionary changed size during iteration" — which
   in `Game`'s `finally` would take both the final cost event and `bus.close()`
   with it.

   `add_usd` does **not** touch `calls`. `to_dict()["calls"]` and the line
   `Game` emits at the end of a game (`"... over N model calls"`) both report
   that figure as model calls, and a clip is by definition not one; the caller
   counts its own units through `**counters` instead.

   `Ledger` is now **locked**: it was written only by the game thread, and
   narration is charged from Flask request threads. `Ledger.ROW` names every
   counter a row can carry so `to_dict` can sum across rows that count
   different things (tokens for a model, characters for a voice).

   **Admission is atomic, and it happens inside the line's own gate.** The
   check and the charge sit either side of a synthesis, and different lines are
   different cache keys, so `PollyTTS`'s per-key gate does not serialize them: without this, N spectators asking for
   N different lines all read the same below-budget total and all N call Polly.
   A synthesis about to happen therefore holds its own cost against the game
   (`_RESERVED` in `web/routes/tts.py`, under one process-wide lock) until it
   is charged or abandoned, and a clip that WOULD take the game over is refused
   before the call. In-process is sufficient because `instances: 1` is a hard
   ceiling. A **cache hit skips the budget entirely**: it is not spend, and a
   game that has run out of money stays listenable. Two details the shape
   depends on:

   - **The charge lands inside the reservation.** Releasing one before making
     the other leaves a gap in which a waiting request reads a ledger that does
     not yet know about the clip just synthesized, and reserves money already
     spent.
   - **The reservation is taken inside `PollyTTS.exclusive(ckey)`**, which is
     why that gate is public rather than private to `synthesize`. Two tabs
     after the SAME line would otherwise each reserve the cost of it, and the
     second be refused for a clip the first is a moment from making free —
     which the page reads as a settled refusal and gives up on server voices
     for the whole game. `render()` is `synthesize()` without the gate or the
     cache read, for a caller that holds both. The gate entry is
     reference-counted rather than popped on exit: retiring it while a queued
     holder still owns that lock lets the next arrival mint a second one and
     render alongside them, which is now a duplicate charge rather than merely
     a duplicate clip.

   **A charge is persisted where it is made.** A `GameEntry` lives in the
   registry for the life of the process but its monitor thread returns at the
   first terminal status — so for a finished game, which is exactly the game
   people replay, nothing else would write after that and the charge would sit
   in memory until a restart handed the budget back.

   **`persist_snapshot` writes an absolute total, so every writer of it
   serializes** — on a lock that belongs to `GameEntry`, not to the narration
   route, because there are four writers: the game thread every 25 events, the
   monitor on a status change, the control routes, and a charge. New
   `GameEntry.record_cost(charge)` applies a charge to the live ledger and
   persists it as one step under that lock; anything else overlapping would
   otherwise persist a total read from before the charge landed.

   A game this process is no longer running has no `Ledger` to charge, so its
   spend goes to the row instead through the new `web/db.py: Database.add_cost(
   game_id, usd)` — an atomic `cost_usd = cost_usd + ?`, correct against
   concurrent spectators, and safe only because nothing is writing snapshots
   over that row any more. The budget stop applies to it either way.

   **The cap is server-owned as well as game-owned.** `budget_usd` arrives in
   the request body on a route that takes no credential (TTS-COSTS.md §1), so
   narration stops at the lower of it and `DND_TTS_MAX_USD` (default $10.00).
   That bounds one game and nothing more: how many games a stranger may create
   is unbounded, and `POST /api/games/<id>/note` still feeds 2,000
   unauthenticated characters into a `dm_note` that `speech.js` always speaks.
   Spectator authentication is the missing piece and this amendment does not
   add it. (Added the same day — see the write-token amendment below.)

   **A budget that cannot be compared is not a budget.** `float()` accepts
   `"NaN"`, and NaN compares False against everything — so a NaN `budget_usd`
   is not a large budget, it is the absence of every budget check in the app:
   `Game._check_budget`'s `total_usd >= budget_usd` never fires either. It is
   refused at `create_game` (the root cause, and the reason this amendment
   touches `web/routes/api.py` at all) and ignored by `_budget_of` in favour of
   the default, for rows written before that check existed.

   **An unknown budget is `GameConfig`'s default, never zero.** Zero is a real
   value — `Game._check_budget` halts at `total_usd >= budget_usd`, so a zero
   budget is a game already over — and a game created through the API without
   `budget_usd` persists a config that never had the key, so after a restart
   there is nothing in the row to read. Answering that with zero, and treating
   zero as "no ceiling", would have removed the cap from exactly the games that
   never set one. `_budget_of` falls back to `GameConfig().budget_usd` and
   `_admission` has no `budget > 0` escape.

5. **A party member may state a `gender`, and it decides which voices that
   character can be cast from.** New optional key in the party spec —
   `{"id","name","race","klass","level",…,"gender": "female"|"male"}` — read by
   the web layer out of the game's own config (live entry or DB row), never
   from the request: this endpoint spends money, and a gender in the query
   string would let a caller walk the roster a paid clip at a time. It narrows
   the pool and nothing else; the choice within it is the same hash, so a
   character keeps its voice for as long as its gender and the roster hold.

   **This is not an engine field.** `CharacterSheet` (§1.3) is unchanged and
   `build_character` ignores the key: gender has no rules meaning in 5e, the
   engine stays pure, and the DM and player prompts do not see it. `dm`, `npc`
   and `monster:<id>` have no character record and are cast from the whole pool
   as before.

   Polly reports `Gender` as exactly `Female` or `Male`, so a character who
   states neither — or states nothing — is dealt from the **whole** pool rather
   than pushed into one of the two; the roster's limitation is not laundered
   into a character sheet. Where a language's voices are all one gender
   (Korean, Swedish), a gender that cannot be answered still gets a voice: a
   worse match, not a silence. The browser fallback ignores gender entirely —
   `SpeechSynthesisVoice` has no gender attribute in any browser, and inferring
   one from voice names would be a guess dressed as data.

6. **Casting differs from the browser's in two ways, both because Polly is not
   a device voice list.** There are no novelty voices, so a speaking monster is
   an ordinary voice put through `<amazon:effect vocal-tract-length>` (never
   0%) with pitch and rate behind it; and the pool is the same for every seat,
   because there is nothing in it that cannot carry narration. The DM's voice is
   chosen (`DND_TTS_DM_VOICE`, default `Brian`) rather than dealt, and no other
   seat is given it.

7. **Two engines, chosen per seat.** `DND_TTS_ENGINE` (default **`neural`**)
   speaks the DM, the players and the NPCs; `DND_TTS_MONSTER_ENGINE` (default
   **`standard`**) speaks anything cast as `monster:<id>`. The engine rides on
   the `Cast` rather than being read from a setting at render time, so a line
   cannot be cast for one engine and rendered on another; each engine has its
   own `DescribeVoices` roster, its own cache namespace and its own rate in the
   ledger — and both are load-bearing at the edges: a clip is **reserved** at
   the rate of the engine its own seat renders on (reserving at the table's
   refuses a monster clip the game can afford, or admits one it cannot), and
   `/api/tts` reports unavailable unless **every** configured engine has a
   roster (an engine without one is a 503 on its first line, which the page
   reads as settled and uses to switch server voices off for the whole game,
   taking the seats that were working with it).

   The split exists for one tag. `<amazon:effect vocal-tract-length>` changes
   timbre rather than pitch — a longer vocal tract is a bigger creature — and
   it is standard-only. TTS-COSTS.md §4 concluded the novelty-voiced monsters
   had no vendor equivalent; this is it, and it is available on exactly one
   engine. Neural carries the rest of the table because §5 recommends it: at
   this quality nothing can pitch-shift, so distinctness is bought as separate
   voices, and neural's roster is larger and more accented.

   Polly errors on an unsupported tag rather than ignoring it, so `ssml_for`
   writes only what the cast's engine accepts (`ENGINE_SSML`): standard takes
   pitch, rate and `vocal-tract-length`; `rate` survives on neural and
   long-form; generative gets plain text, because its prosody tag is documented
   as full-sentences-only and a chunk can be a fragment.

   `STANDARD_ENGLISH` is the standard engine's **English** roster and nothing
   else, so on another engine — or in another language — a failed
   `DescribeVoices` leaves no roster to vouch for:
   `voices()` comes back empty, `/api/tts` reports unavailable, and the page
   uses the browser's voices — rather than casting a standard-only voice with
   `Engine=neural` and reaching the same 502 loop by another route. An empty
   pool is cached only for `EMPTY_ROSTER_TTL` (60 s): a `DescribeVoices` that
   fails transiently is the absence of an answer rather than an answer, and
   caching it for the life of the process would leave narration off long after
   the outage cleared. A roster that does come back is kept for good.

   Putting the whole table on one engine is a supported choice, not a special
   case, and the cost of doing so on anything but standard is a blunter table:
   with no pitch, two characters dealt one voice cannot be told apart. The
   cache key is the rendered document rather than the cast, so casts an engine
   renders identically share one clip. Billed
   characters exclude SSML tags
   (<https://docs.aws.amazon.com/polly/latest/dg/limits.html>), which is what
   `billable_chars` counts.

8. **Mock mode stays free.** `from_env` returns `None` under `DND_SIM_MOCK`
   unless `DND_TTS=1`, and the route refuses a game whose own config is `mock`
   on the same terms. "Same config + seed ⇒ byte-identical game, costing
   nothing" is a property of this repo; paying Polly to read a mock game aloud
   would quietly end it.

The browser half — one `<audio>` element, clips prefetched a line ahead, a
per-line fallback to `speechSynthesis` that becomes a session-long one after
three failures or a settled refusal, and both engines armed inside the same
unlock gesture because the fallback has to work on iOS — is UI, not contract.
It is described in the README.

### 2026-09-04 — web/static — a spoken line may be more than one voice

The speaker's name was prepended to the words of every non-PC `dialogue` event
(`phraseFor`: `who + ': ' + said`), so a listener heard "Goblin Sneak: I'll gut
you". The name was therefore *inside the spoken text*: billed as characters,
rendered through the monster's own `vocal-tract-length` distortion, and
punctuated with a colon in the middle of a sentence, which both engines read as
a label rather than as an attribution. It is now announced by the narrator
instead, as its own clip.

1. **`phraseFor(ev, names, party)` returns the words alone for `dialogue`**, for
   every speaker. `dialogueLine(ev, names)` is the extracted helper that finds
   the speaker and the words — `data.speaker` first, `splitSpeaker(text)` as
   the fallback for history stored before the speaker moved into `data` (the
   2026-09-04 "one line per turn" amendment, §5).

2. **`attributionFor(ev, names, party) -> string | null`** is new: the name to
   say before the line, or `null` for "announce nobody". Who is announced is
   unchanged from what got the prefix — never a PC, always anyone else with a
   known name — and only the delivery is new. The name is returned as its own
   sentence (a full stop appended unless it already ends in one) so the
   narrator lands on it instead of running into the line.

3. **`segmentsFor(ev, names, party, monsters) -> [{key, text}]`** is new and is
   now the whole of "what is spoken and by whom": one part per voice, in order.
   Every event except an attributed line of dialogue is a single part, exactly
   as before; an attributed one is `[{key: 'dm', text: name}, {key: <speaker>,
   text: words}]`. `phraseFor` and `voiceKeyFor` are unchanged as the pieces it
   is built from, and both stay exported — `voiceKeyFor` is still the one
   definition of who owns a line.

   This supersedes the sentence in the 2026-09-04 Polly amendment (§2, "the
   wording stays in the browser") that says a line has *a* voice key. It still
   stays in the browser: the server is told a key and a string and does not
   re-derive either, and no route, request or response shape changes. What
   changes is that one event can be more than one request.

4. **`web/static/app.js` casts per chunk, not per line.** `cur.chunks` is now
   `[{key, text}]` rather than `[text]` and `cur.vkey` is gone; `chunkKey(cur)`
   /`chunkText(cur)` are what the server path, the browser-fallback path and
   the prefetch all read.

   **One line, one engine, where the line is more than one voice.**
   `voiceStartLine` asks for every remaining clip of such a line before any of
   it plays, and hands the whole line to the browser's voices if any is
   refused. `cur.local` cannot do this on its own: it is set by a failure that
   has already happened, and the attribution is the half most likely to be a
   free cache hit — the same name every time that character speaks. So the
   name would play through Polly and the words behind it still be refused,
   which is precisely what a game running out of budget mid-scene does. A
   single-voice line is unchanged and still starts on its first chunk while
   the rest fetch: that is what makes a long narration start promptly, and one
   speaker crossing engines between chunks is a seam nobody can hear.

**Why the recorded rationale was overridden.** The comment this replaces
argued that a speaking monster's novelty voice was "a costume rather than a
name anyone can place", so the name had to be spoken. That premise was written
against the browser's `speechSynthesis`, where a monster is cast from whatever
novelty voices the device happens to ship and two monsters routinely draw the
same one. Since narration moved to Polly it is false often enough not to rely
on: a monster is an ordinary voice with a `vocal-tract-length` timbre, and the
distinctness is bought as separate voices. The half of the argument that still
holds is about the *shared* voices — every NPC speaks in the one `npc` voice,
and nothing tells two of them apart by ear — which is why the name is still
said, rather than dropped as it is for a PC. It is now said by the voice whose
job announcing things is.

**Cost.** Billed characters are unchanged to within a character per attributed
line (`": "` becomes `"."`). What changes is requests: one extra per attributed
line of dialogue. Nearly all of them are free — the attribution text is
identical every time that speaker talks and the cache key (engine, voice id,
rendered SSML) contains no game id, so one synthesis per distinct name per
voice covers every line that character will ever speak, in this game and in
every later one. **The re-spend is real but narrow**: changing the wording
changes the key, so every *attributed dialogue* clip already on disk is
orphaned and each such line is paid for once more. Non-dialogue clips — which
are the overwhelming majority of a game's characters — keep their keys and are
not re-synthesized. Orphans are not deleted: `AudioCache.prune` is
size-triggered LRU (every 50 writes, only above `max_bytes`), so they sit there
until the ceiling is crossed and then go first, having stopped being read.

The `&v=` fingerprint (`PollyTTS.config_id`) does not cover `speech.js` and
does not need to here: the words are in the query string, so new wording is a
new URL and neither the browser's year-long immutable copy nor the server's
cache can answer it with a clip of the old wording.

### 2026-09-04 — web/ — a write token on the routes that mutate or spend

Narration went live on Amazon Polly the same day (the "narration moves to
Amazon Polly" amendment), and two routes that had been theoretically open
became a spend surface. `POST /api/games` took no credential and was unbounded
in call count — and the caller supplies `budget_usd`, which is why the narration
route clamps to `min(game budget, DND_TTS_MAX_USD)`; that ceiling is *per game*,
so N games are N × $10.
`POST /api/games/<id>/note` took 2,000 characters that `web/static/speech.js`
always speaks, roughly 3¢ a call at neural rates, repeatable. TTS-COSTS.md §1,
§4 and §6 each land on the same missing piece and §6 states it as unbuilt.

**New env var `DND_WRITE_TOKEN` and new request header `X-Dnd-Token`.**
`web/auth.py` is the whole mechanism: `configured_token()`, `authenticated()`,
`write_refusal()` and the `@require_write` decorator, compared with
`hmac.compare_digest`. `create_app` snapshots the variable into
`app.config["DND_WRITE_TOKEN"]` (injectable, like `DND_TTS`), so a route never
reads the environment mid-request.

1. **Which routes.** Exactly those that mutate a game or spend on it:
   `POST /api/games`, `/pause`, `/resume`, `/stop`, `/note`. §5's table marks
   them.

2. **Which routes deliberately do not**, and why each would be a regression:

   - **Every read**, including `GET /api/games/<id>/stream`. The spectator UI
     is public at <https://dndsim.lab980.com> and reading a game anonymously is
     the product. Authenticating the stream would not protect anything — it
     spends nothing — and would end the site.
   - **`POST /api/games/<id>/hold`.** It is the narration keeping-step lease
     from the amendment above: every anonymous listener renews one every few
     seconds, it spends nothing, it leaves `status` alone, it expires by itself
     (≤ `MAX_HOLD_SECONDS`) and the table is bounded at `MAX_HOLD_CLIENTS`. The
     worst an anonymous caller can do with it is slow a game down, which is
     what having the page open does anyway. Gating it would take server
     narration away from exactly the audience this site is for.
   - **`GET /api/games/<id>/tts`.** It does spend, which is why it is the
     interesting one. But an anonymous spectator cannot hear the game without
     it, and what it spends is already bounded on the axis that matters: per
     line by `DND_TTS_MAX_CHARS`, per game by `min(budget_usd,
     DND_TTS_MAX_USD)`, and once for good by the on-disk cache — a clip is
     paid for once and replayed free, forever, by every spectator. The
     unbounded axis was never this route; it was *how many games a stranger may
     create*, and that is what the token closes. Charging a stranger's replay
     to a game that is already capped is the design working.

3. **`GET /api/auth`** (new, anonymous, a read): `{"writes":
   "token"|"unconfigured", "header": "X-Dnd-Token", "authenticated": bool}`. It
   reports only what a write attempt would report a moment later, so it is no
   more of an oracle than `POST /api/games` already was — and without it the
   page's only way to learn whether it may create a game is to create one. It
   never echoes the token.

4. **Refusals.** `401` with `{"error", "code": "unauthorized"}` when the header
   is missing or wrong; `503` with `{"error", "code": "writes_unconfigured"}`
   when the server has no token set. The gate runs before body validation, so
   an anonymous caller gets a 401 rather than a 400 that would tell it what a
   valid request looks like.

5. **An unset token fails closed.** The token is not a vendor key adopted from
   the box's store; `dndsim token` generates it into `.env` (see DEPLOY.md),
   so a deploy alone never produces one and the first deploy carrying this code
   lands on a server where the token is not set. Failing open there would ship a no-op — the hole
   still open and now believed closed. Failing closed costs one edit to
   `/var/www/dndsim/.env` and a restart, and nothing else: the app starts, the
   page loads, every read and the stream work, and a game already running keeps
   running and stays audible. That is the "degrade safely" a missing key owes,
   and it is not the same as staying open. A whitespace-only value reads as
   unset, so a blank line in `.env` cannot be matched by an empty header.

6. **The token is read from the header alone**, never a query string: a query
   string is in nginx's access log, in `Referer` on any outbound link, and in
   the browser's history.

7. **UI (not contract, recorded because it decides whether a browser holds a
   credential at all).** The spectator page keeps the token in `localStorage`
   under `dndsim.token` and sends it on every request; `checkAuth()` asks
   `/api/auth` at start-up. The write controls — the New game button, the
   pause/resume/stop row and the DM-note form — are **not rendered** until the
   page holds a token the server accepts, so an anonymous visitor never sees a
   control that can only 401; a panel opened from the header takes the token
   and validates it against `/api/auth` before storing it. That button stays
   visible once unlocked (as "Token"), because the panel behind it is the only
   way to reach Forget — hiding it would leave a shared browser holding the
   credential with no way out short of clearing site data; it is hidden only
   where the server has no token set. `renderWriteAccess()` never opens or
   closes that panel, so an auth answer arriving while someone has it open does
   not shut it. Every `/api/auth` probe carries a generation number
   (`S.authGen`, the same rule `loadGen` enforces for a game load) and Forget
   bumps it: Forget is deliberately not disabled while a probe is in flight —
   it is the one control never worth taking away — so a stale answer would
   otherwise re-persist the token the user had just cleared. `#ctl-note` stays outside the hidden wrapper: load failures are
   reported there and every spectator needs to see those. A 401 or 503 from any
   write re-renders the gate.

8. **Not built, deliberately.** No per-user identity, no revocation short of
   rotating the value and restarting, no rate limit. There is one writer here —
   the operator — so identity would be a table with one row in it; and a rate
   limit protects a *leaked* token, which is a different threat from the open
   door this closes. Both remain open, and neither is a prerequisite for
   the other.

### 2026-09-04 — tts/ + web/ — a character's age decides whether it is cast as a child

Reported from a live game: the cleric **Father Bexley Crane** was read out in a
child's voice. Nothing was broken — Polly's roster has children's voices in it
(`Ivy`, `Justin`, `Kevin`), the pool every seat was dealt from held them, and
the hash put one adventurer in eight on one. The casting had no idea they were
children, because there is nothing to ask.

1. **A party member may state an `age`, and only an age that reads as a child
   changes the casting.** New optional key in the party spec —
   `{"id","name",…,"age": "child" | "adult" | 9 | "40"}` — read by the web
   layer out of the game's own config, never from the request, for the reason
   `gender` is (the narration amendment's §5: this endpoint spends money, and a
   trait in the query string walks the roster a paid clip at a time). `normalize_age` is the only
   place that decides what an answer means: the words in `AGES`, or a number of
   years against `CHILD_MAX_AGE` (12), written in the one grammar
   `_NUMERIC_AGE` pins — sign, decimal digits, at most one point, optional
   exponent. Anything unreadable — `"old enough"`, `0`, `-3`, `True`, `0xA`,
   `1_0` — is nothing said. The grammar is pinned rather than left to
   `float()` because `web/static/app.js` decides which way its select starts
   and `Number()` disagrees with `float()` in both directions; a panel that
   showed one answer while the server cast another would restate a
   character's age on submit, since submitting writes the select back.
   `web/tests/test_newgame_panel.py` runs both implementations over one
   corpus.

   **Not an engine field**, exactly as `gender` is not: `CharacterSheet` (§1.3)
   is unchanged, `build_character` ignores the key, and neither the DM nor the
   players see it. Age has no rules meaning in 5e here.

2. **An unstated age is an adult, and that is a behaviour change.** Every seat
   — including `dm`, `npc` and `monster:<id>`, which have no character record —
   is now dealt from the voices that are not children's. This is the fix: a
   party of four at a table whose roster holds two or three children's voices
   was casting a child by accident, reliably, and "deterministically wrong" is
   still wrong. A character that asks for a child's voice gets one; nobody else
   can be dealt one.

   The DM's *named* voice is honoured whatever age it is (asking for `Ivy` is
   asking for `Ivy`); the unnamed fallback — whatever sorts first — now skips
   the children, which on the en-US roster alone was `Ivy`.

3. **`CHILD_VOICE_IDS` is transcribed, not discovered.** `DescribeVoices`
   reports `Gender` and nothing else about the speaker, so unlike the gender
   constraint there is no live field to narrow on. The three ids are the whole
   set Amazon's voice list annotates in any language, and
   `tests/tts/test_polly_contract.py` holds the table against that page's
   wording; a language with no children's voices (every one but en-US) casts a
   stated child from the adult voices — a worse match, not a silence, as an
   unanswerable gender already was.

4. **Every seat re-casts once, and the clips are already retired.**
   Narrowing the pool changes which voice a given hash lands on, so a table
   that has been running sounds different after a deploy. `config_id()` covers
   it: it hashes `voices.py`'s own source (§ narration, `source_fingerprint`),
   so the URL a browser holds a year-long-immutable copy under changes with
   this commit and the old audio is never replayed against the new casting.

5. **The panel asks.** `web/static/index.html` gains `#ng-party`, one row per
   seat of the chosen preset with an adult/child select
   (`renderPartyAges` / `applyPartyAges` in `app.js`). Choosing *adult*
   **deletes** the key rather than writing it: an unstated age already casts as
   an adult, and stating one back into a scenario's config would be writing a
   fact about a character nobody chose to state. Age is the only voice trait
   editable there — the shipped-party rule for `gender` (README, *Spoken
   narration*) is that a character states one only where its own persona
   already does, and a dropdown inviting an operator to pick one for someone
   else's character is the opposite of that.

6. **The browser fallback cannot do any of this.** `SpeechSynthesisVoice`
   reports no age, exactly as it reports no gender, and inferring one from
   voice names across every OS and locale would be a guess dressed as data. A
   session on the fallback engine casts as it always did.

### 2026-09-04 — orchestrator/ + web/ — a knockout is announced after its narration

The loop in §4 publishes a turn's engine events and *then* asks the DM to
narrate it. That is right for everything the engine says except the two lines
that are the reveal: `down` and `dead` are reported the instant the HP reaches
0, which is a paragraph before the prose describing the same swing. On the page
it merely reads oddly. Spoken — and `speech.js` classes both kinds as STORY, so
they are read aloud even with *mute mechanics* on — it is the spoiler said over
the top of its own reveal: "Bandit 3 dies", and then, seconds later, the axe is
still in the air.

1. **`Game._emit_turn` holds them; `Game._flush_reveals` releases them.** The
   per-turn emission in `_run_turn` now routes through `_emit_turn`, which
   diverts events whose kind is in `REVEAL_KINDS = ("down", "dead")` into
   `_pending_reveals` and emits everything else unchanged. `_run_combat`
   flushes after `_narrate` and before `advance_turn`, so the reveal lands
   inside its own turn, after the paragraph, ahead of `turn_end`/`round_start`.

   The flush is **unconditional**: `_narrate` returns early for a turn with
   nothing worth describing, and the DM may answer with nothing at all. A
   knockout held for a narration that never comes still has to be said.

2. **The order is the only thing that changes.** The attack, the damage and
   the HP line stay where they happened — §6 requires the narration not to
   contradict the numbers, and moving the numbers with the beat would take
   that check away with them. `dm.narrate` is still handed the turn's *whole*
   event list, reveals included, so the DM writes from the same facts as
   before. A mock run at a fixed seed emits an identical multiset of events;
   only the position of the `down`/`dead` lines differs.

3. **Reveals held at the end of a game are released on the way out.**
   `_narrate` gates and checks the budget, so a stop or an exhausted budget can
   land between the blow and the line that says it landed. `run()`'s `finally`
   calls `_flush_reveals(gated=False)` — `_emit` gained that keyword, which
   skips the tempo sleep and the pause/hold/stop gate — before the closing cost
   event. A transcript in which someone is hit to 0 HP and nothing ever says
   they fell is worse than one where the beat arrives late. `_flush_reveals`
   pops one at a time for the same reason: a stop raised mid-flush must leave
   the rest still pending for that last pass to find.

4. **The transcript closes the mechanics group at a narration.** In `app.js`
   the `narration` branch now clears `S.group`/`S.groupBody`. A turn's
   mechanics group is a node already appended *above* the paragraph, and the
   mechanics branch files any line into `S.groupBody` while one is open — so
   leaving it open would put the released reveal back inside the group, above
   the prose it is meant to follow, and send the playhead scrolling up to it.
   Nothing else lands there today: `turn_end` already closed the group for the
   end-of-turn ticks that follow it.

Deaths from failed death saves are untouched. `advance_turn` rolls them
between turns, where there is no narration for them to wait behind.

### 2026-09-04 — web — `api()` errors carry their status; the hold latches only on one

The spectator page's narration hold is a lease renewed every 4 s (the
2026-09-04 amendment above). `voiceHoldSend`'s rejection handler latched
`V.holdBroken = true` — stop asking for the rest of this game — on **any**
rejection, its comment naming 404/409/501 as though those were the only ones
possible. They are not: `api()` rejects on a dropped connection, a 502 while
nginx restarts under a deploy, any 5xx. One of those ended holding for the rest
of the page's life, and nothing re-armed it short of switching games or
reloading; the game then ran on at `tempo_ms` and the narrator fell arbitrarily
far behind, silently. A spectator reported 43 lines.

1. **`api()` attaches the status to the error it throws**: `err.status =
   r.status`, alongside the unchanged message. Additive — every existing catch
   reads `err.message` and is untouched — and deliberately absent when `fetch`
   itself rejects, so no `status` means "no server answered" rather than some
   code standing in for one. It is the only way a caller can tell a refusal
   from a blip: the message is the server's own words whenever there are any,
   and carries no code.

2. **The hold gives up only on 404 / 409 / 501** — the three `hold()` in
   `web/routes/api.py` returns that mean this game cannot be held (no such
   game, running in another process, a game object without the method). Not the
   400 for a non-numeric `seconds`, which is the page's own bug, and not a 5xx.

3. **Anything else is retried, with a bounded back-off.** The hold stays wanted
   and the next tick asks again, delayed by one `HOLD_TICK` per consecutive
   failure up to `HOLD_RETRY_MAX` (60 s), cleared by a success and by
   `voiceReset`. So a blip costs one tick and a genuinely dead endpoint is
   asked once a minute rather than every four seconds forever. This is the one
   qualification on "the client renews every 4 s" in the amendment above.

Client-side only: no route, no payload and no `Game` method changes.
`voiceOnGameEnd` still sets `holdBroken` outright — the game is over, there is
nothing left to hold — and a backgrounded tab still drops its lease, which is a
separate deliberate rule and not this bug.

### 2026-09-04 — agents + engine — the DM is told whose turn it is, and what to call everyone

The DM's per-turn narration was mechanically faithful and repeatedly wrong
about attribution: it acted for player characters on a monster's turn, gave the
actor's own blow to its target, asserted knockouts the engine never reported,
and swapped a monster's gender between rounds. `dm_narrate.txt` already said
"Do not act for player characters" and was ignored, because the prompt never
established the two facts those errors need — who is acting, and what each
creature is called.

Measured over `examples/tollhouse.json` at seed 23 (62 narrations): **34 of
them (55%) handed the DM an event list naming more than one creature**, and one
of them opened with `Bandit 2 makes an opportunity attack on Captain Isolde
Rooke …` on *Rooke's* turn. Inferring the actor from whichever name leads the
list is the only thing the prompt allowed, and that is the inference that fails.

**`dm_view` output changes in two ways** (§3; the `dm_view` and `DMAgent`
signatures do not change):

1. A `TURN: <id> <name>` line, above the combatants table, whenever
   `state.active_id()` answers. It is absent outside combat and absent for a
   state that cannot answer — the views stay duck-typed.
2. The combatants table gains a pronouns column:
   `id | name | pronouns | side | HP | AC | pos | conditions`.

`dm_narrate.txt` gains one paragraph tying the two together — everything in the
list other than the TURN creature is a reaction, pronouns come from the table
and do not change, and a creature is bloodied/down/dead only where a line says
so — and loses the now-redundant "Do not act for player characters".

**`CharacterSheet` gains `gender: str = ""`** (§1.3), read by `build_character`
from `spec["gender"]`, serialized both ways. Inert: no rule reads it. It exists
so the one authored answer already on a party spec — `tts/voices.py` has read it
for casting since 2026-09-04 — also reaches narration, instead of each reader
inventing its own.

**Pronouns are not dealt.** `agents/views.py` maps a *stated* gender to
`she/her` / `he/him` on exactly the spellings `tts.voices.GENDERS` accepts
(pinned against each other by a test, since `agents/` cannot import `tts/`), and
everything else — every monster, and any party member whose spec is silent —
gets `they/them`.

That is a deliberate reading of the rule the `tts/` amendments set down. A
shipped party character states a gender only where its own persona already
does; the same principle says a `Bandit 3` built from an SRD stat block, which
records a size and a type and no gender, has nothing to state. So nothing is
inferred from a name and nothing is drawn from the dice — a pronoun out of the
RNG is a pronoun that can drift, which is the bug. Singular *they* is what
English already does for an unestablished referent, it is stable because it is
the same for every monster in every game rather than per-instance, and it
invents nothing about a creature nobody wrote.

**No TTS consequence.** `web/routes/tts.py: _voice_traits_for` reads the game
*config's* party list keyed by the voice key, and a monster's key is
`monster:<id>`, which never matches a `pc_N` id. Monsters were cast as an adult
of unstated gender before this and still are; `pronouns_for` lives in `agents/`
and nothing in `tts/` reads it.

**Frugality.** `orchestrator/game.py: _narrate` now builds its view with no
`recent`. It was passing the turn's own events, so the view's RECENT block
reprinted, line for line, the events block directly beneath it — across all 62
narrations of that game every RECENT line was already in the events block,
costing ~3.7k tokens a game for a second copy. Netting that against the TURN
line, the pronouns column and the new paragraph, one full tollhouse game's
accounted spend moves **$1.1082 → $1.1188, +1.0%**, and the game itself is
unchanged: same 328 model calls, same events, same round 11.

*Determinism:* nothing added draws from anything. A pronoun is a pure function
of an authored string, and the turn's actor is state the engine already holds.
`--mock --seed 23` is byte-identical across runs, and
`tests/orchestrator/test_narration_attribution.py` asserts the narration
prompts themselves are identical across two runs of one seed.

### 2026-09-04 — tts/ + web/ + engine/ + agents/ — a character states pronouns, not a gender

The narration amendment gave a party member a `gender`, `female` or `male`,
because Polly's roster is `Female` and `Male` and that is the field
`DescribeVoices` answers. It is the wrong end of the mapping. What a character
actually carries — in its own persona, where the shipped-party rule already
reads it from — is its pronouns; the gender is a second fact somebody has to
infer from them in order to write the config, and once written it is a fact
about a person recorded to serve a two-item voice list.

1. **New optional key, `pronouns`, replacing `gender` in every shipped party.**
   `{"id","name",…,"pronouns": "he/him" | "she/her" | "they/them" | …}`, read
   by the web layer out of the game's own config and never from the request,
   for the reason every other voice trait is (the narration amendment's §5:
   this endpoint spends money). `tts.voices.gender_for_pronouns` is the one
   place that turns a stated set into a pool: the FIRST pronoun decides, `he` →
   `PRONOUN_GENDERS["he"]` → the male voices, `she` → the female ones, and
   everything else → `""`, the whole roster. So `he/him`, `he/him/his` and a
   bare `He` are one answer, and `she/they` is read the way its author wrote
   it. Any set at all may be stated; the function answers what the roster can
   do about it, not what the character is.

   **It is the key the amendment above wanted.** That one put `gender` on
   `CharacterSheet` (§1.3) — inert, read by nothing in the rules — so that one
   authored answer could reach both the voice casting and the pronouns column
   in `dm_view`, and it read it there through `agents.views.PRONOUNS`, which
   infers `female` → `she/her`. With `pronouns` stated there is nothing left to
   infer: `CharacterSheet` gains `pronouns` beside `gender`, `build_character`
   copies each verbatim from the spec, and `pronouns_for` returns a stated
   `pronouns` **as written** — a set neither table has heard of reaches the DM
   intact — falling back to `PRONOUNS.get(gender)` and then to `they/them`. So
   the string the DM narrates a character in and the string the voice is dealt
   from are the same string, and `tests/orchestrator/test_narration_attribution.py`
   pins that both ways. The engine still reads neither: both fields are carried,
   not consulted, and no rule, prompt or action sees them.

2. **`they/them` becomes sayable, and it is not the same statement as silence.**
   Both cast from the whole pool — Polly has no third voice and inventing one
   by pushing the character into one of the two would launder the roster's
   limitation into a character sheet — but a config could previously only reach
   that casting by omitting the key, which reads as *nobody decided*. The two
   are now distinguishable in the config even though the audio is identical,
   which is the point: the file is also read by people.

3. **`gender` is still read, and stated pronouns beat it — on both sides.** A
   game persists its config and its clips are cached per cast, so a row written
   before this — or a stranger's config — would otherwise re-cast mid-transcript
   and pay Polly again to do it. `_pool_gender_of` in `web/routes/tts.py` reads
   `pronouns`
   where the key is present and non-blank, `gender` otherwise; a stated
   `pronouns` decides **including when it narrows nothing**, or a config
   updated to `they/them` would go on being narrowed by the key that update
   replaced. `pronouns_for` resolves the same collision the same way, or a
   config that took the trouble to say so would be narrated in one set of
   pronouns and spoken in the voice of another. No example or preset states
   `gender` any more, and `web/tests/test_tts_api.py` holds them to that.

4. **The panel asks, where it could not ask for a gender.** `#ng-party`'s rows
   gain a pronoun select beside the age one (`renderPartySeats` /
   `applyPartySeats`, renamed from `renderPartyAges` / `applyPartyAges`). The
   age amendment's §5 refused a gender dropdown, and the refusal stands: a
   picker inviting an operator to choose a *gender* for someone else's
   character is the opposite of the shipped-party rule. Pronouns are a fact the
   character already carries, so the row reads one back rather than assigning
   one — and the default is *not stated*, which, like *adult*, **deletes** its
   key rather than writing it.

   Three sets are offered and they are not a taxonomy: a config stating
   something else keeps it verbatim as its own option, and a spelling that
   differs only in case (`He/Him`) wins over the offered one, so opening the
   panel and touching nothing cannot restate a character on submit. A config
   stating only the legacy `gender` shows an unstated pronoun row: filling it
   in from that key would run the mapping backwards, which is the inference
   this amendment exists to stop making.

5. **Casting is unchanged, and so is the bill.** `she/her` narrows exactly as
   `female` did and the hash within the narrowed set is untouched, so every
   converted character keeps the voice it had — and the disk cache key is
   `(engine, voice id, rendered SSML)`, with no fingerprint in it, so every
   clip already paid for is still a hit. What does move is `config_id()`: it
   hashes `voices.py`'s own source (§ narration, `source_fingerprint`) and this
   commit changes that file, so the `&v=` in every clip URL changes and each
   browser re-fetches its copies once. Those re-fetches are served from
   `data/tts` and cost nothing at Polly. This is *not* the age amendment's §4,
   where the casting itself changed and the orphaned clips genuinely were
   re-synthesized; the only re-spend here would be for a config that states
   `they/them` beside a legacy `gender`, and no config could state `pronouns`
   before this commit.

6. **The browser fallback is unaffected.** `SpeechSynthesisVoice` reports no
   gender and no age in any browser, so `speech.js` casts as it always did.

---

---

---

### 2026-09-04 — tts/ + web/ — the panel says who will read each seat

The new-game panel gained a per-seat `adult`/`child` control (the age
amendment above) and stopped there: one visible knob over an outcome — which
of Polly's voices reads your cleric, and what it sounds like — that is
otherwise decided silently by a hash. Gender is in the party spec and was
never shown at all; accent is not in the spec, falls out of the roster, and a
listener notices it in the first sentence.

1. **`tts/voices.py: accent_for(language) -> str`**, with the `ACCENTS` table
   behind it: a Polly `LanguageCode` in the words a listener would use —
   `en-GB-WLS` is *Welsh* and is keyed on the full code, because
   Welsh-accented English is not `en-GB`. An unlisted locale is returned **as
   its code**, not guessed at: `voices()` reads the live roster, so a locale
   Amazon adds tomorrow has to remain describable, and a guess from a table
   written today would eventually be a confident lie about which voice a
   listener is hearing. Pure, and every voice on `STANDARD_ENGLISH` is held to
   having a name by `tests/tts/test_voices.py`.

2. **`POST /api/tts/cast`** answers, for a proposed party, the voice each seat
   is dealt, its accent and the gender Polly records for the recording. Body
   `{"party":[{"id","pronouns","gender","age"}…]}`, read through
   `_pool_gender_of` so the preview narrows the pool exactly as the paid
   endpoint will — at most `MAX_CAST_SEATS` (16) seats;
   a member with no `id` comes back with `"voice": null`, because `cast_for`
   reads an empty key as the DM and the narrator's voice printed against a
   player's name would be a confident wrong answer. A server with no Polly
   answers `{"available": false, "seats": []}` — the same shape `/api/tts`
   refuses in — since a cast nobody will hear is worse than no cast.

   **Anonymous, and safe to be.** It renders nothing, spends nothing and
   writes no cache entry; it is `cast_for` over a roster the service has
   already listed. That is the opposite of the rule on `GET
   /api/games/<id>/tts`, where traits are read from the game's own config and
   never the request (narration amendment §5) — there the request could walk
   the roster a *paid clip* at a time. Here there is no clip. What this must
   never become is a way to *hear* an arbitrary voice.

3. **One implementation of the casting, asked twice.** The panel does not
   compute the cast; `web/static/app.js` asks this route and prints the
   answer. A copy of the rules in JS would agree with `cast_for` exactly until
   somebody edited one of them, and the failure mode is a panel that names a
   voice the game does not use. `web/tests/test_tts_api.py` checks the preview
   against the `X-Dnd-Voice` of the clip the paid endpoint then serves for the
   same seat.

Unchanged: the party spec (§1.3 and the age amendment), what the engine sees,
and which traits the panel may edit — age remains the only one, for the reason
stated there.

---

---

---

### 2026-09-04 — agents/ — the pronouns reach the players, the roster and the prompts

Layered on "a character states pronouns, not a gender", which arrived on `main`
while this was open and settled the data model: `pronouns` verbatim on the
sheet, `gender` still read underneath, `agents.views.pronouns_for` the one
reader. This branch had built the same thing a different way — resolving in
`build_character` and storing the result — and that version is dropped whole
rather than kept alongside. Storing a resolution writes `they/them` into the
sheet of a character who said nothing, and an unstated answer not being written
down is the whole point of the key.

What is added here is where the answer goes:

1. **`player_view` carries the pronouns column `dm_view` has.** A player talks
   about its allies and the monsters both, and infers a gender from a name
   exactly as readily as the DM does.

2. **`party_summary` introduces each character with them**, so the roster that
   opens a scene establishes them before the first narration rather than after
   it.

3. **The player's cached system prefix carries its own**, and `dm_system.txt`
   and `player_system.txt` both gain the rule in the place a model reads before
   anything else: use the pronouns you are given, never infer them from a name,
   a class, a title or a voice. `dm_narrate.txt` says it per turn; these say it
   once, in the block that is cached for the session.

4. **The spectator card shows them**, on the line it already uses for identity,
   beside AC and class.

Unchanged: `pronouns_for`, the DM column, what a party spec states, and the
casting. `tests/engine/test_pronouns.py` keeps only what those cover between
them — that the sheet carries both keys verbatim, that both survive the
round-trip a restart puts them through (including a row written before either
existed), and that neither touches a rule.


### 2026-09-04 — engine, agents — a move that does not walk through a reach, and a log that can be checked

A recorded game (`examples/cellar_rats.json`, seed 11) was read line by line
for continuity. Four things in it looked like rules bugs. Three were not, and
the fourth had been invisible behind one of them.

**The real one.** `Grid.path` is a shortest-path search that knows nothing
about who threatens what, and ties are broken by whichever square sorts
lowest. Leaving (7,1) for anywhere south of it, Giant Rat 3 was routed through
(6,2) — inside the rogue's reach — and straight back out to (7,3), taking a
rapier that killed it. (7,2) is the same 20 ft and never enters the reach at
all; so is every other case on that board, all twenty-four of them. The rat
paid for nothing.

`path` now takes `threat`, a map of square to the enemies whose reach covers
it, and compares cost as `(feet, opportunity attacks provoked)`. Feet still
decide: a route that dodges a reach by costing 5 ft more does not win, and a
mover standing in a reach still gets hit on the way out, because there is no
route that does not leave it. Only the tie changes. `threat_map` in
`engine/actions.py` builds the map from the same predicate `reactions_for`
uses — the two must agree or the pathfinder optimises against a rule the
resolver does not apply.

**The three that were not bugs** are worth more than the one that was, because
of why they looked like bugs. Each was the log omitting the number that made
the line add up:

1. **An opportunity attack named no squares.** The move event that follows it
   reports where the mover started and where it was stopped, never the square
   it was leaving when the reaction fired — those differ whenever the mover got
   a step in first. Comparing the attacker against the move's `from`/`to` says
   the engine fired on a creature moving *into* reach. It did not. The attack
   line now carries `leaving reach (a,b)→(c,d)` in the reasons bracket it
   already had.

2. **A heal printed the die and not the modifier**: `regains 7 HP from Healing
   Word (1d4 → 1)`, which is four points of Wisdom and Disciple of Life short
   of arithmetic. Now `(1d4 → 1 + 6)`.

3. **A crit printed the dice it did not roll.** `roll_damage` doubles dice and
   labelled the result `1d4+2 (crit)`. Two dice were thrown; the expression
   said one, which reads as a crit that forgot to double. Now `2d4+2 (crit)`.

**And one narration fix.** `dm_view`'s combatant table has a class column now,
for the same reason it gained a pronouns column: what is not in the view is
guessed. Without it the narrator called the party's wizard "the downed cleric"
while the actual cleric was standing over him healing him. `party_summary`
carries the class, but that primes the scene once and is gone by round 1.
`player_view` is deliberately not changed — it is built four times a turn and
the error observed was the DM's.

**Not done, deliberately: a rules-lawyer agent watching the transcript.** The
question that started this was whether to add one. It would have found all
three phantom bugs above and none of the real one, because it reads the same
text a human does and the text was the problem. It would also cost a model
call per action against an engine whose rules are already deterministic and
already asserted. Anything a watcher could check about the rules is cheaper as
a test; anything it could check about the *story* — that two speaking kobolds
died in round 1 and the closing narration called the dead "six dead vermin"
and said nothing was learned — is a scene-level judgement, one call at scene
end, not one per turn. That one is still open.

`tests/engine/test_threat_pathing.py` pins the lot, replaying the recorded
board.

### 2026-09-04 — web/ — the screen waits for the voice, and the board with it

The page read the game aloud while showing it at the speed the events arrived,
so the two disagreed: pieces moved and hit points dropped minutes before the
narration said anything about them, and the narration panel printed the line it
was reading — and the line it was parked on — alongside a transcript that had
long since run past both. The hold (the 2026-09-04 amendment above) throttles
the *game*; this is the other half, and it is about the *screen*.

1. **`web/static/speech.js` gains `revealRun(queue, settings) -> n`** — how many
   of a queue of waiting events may go on screen in one beat: the run up to and
   including the next event `shouldSpeak` admits, or 0 when there is no such
   event in it yet. Pure and node-testable, like the rest of that module.

   A run rather than one event, because a turn's mechanics are emitted BEFORE
   the paragraph that describes them (`Game._emit_turn`, then `_narrate`) and
   with mechanics muted are never spoken at all. `app.js` holds the queue: the
   stream and the history load feed `ingest`, and `renderEvent` — the only
   thing that puts an event on screen — is reached from one caller, the queue's
   own release. With voice off, before the first tap, or during a history
   replay the gate is open and everything is revealed as it arrives. This is
   UI, not contract, and is described in the README.

2. **`GET /api/games/<id>?at_seq=<n>`.** The queue can delay a paragraph, but
   the board comes from the server's state, which is wherever the game has got
   to — so the page asks for it *as of* the line it has just revealed. The
   reply's `snapshot.state` and `round` come from the archive; `status`,
   `ledger` and `cost_usd` stay live, because money and whether the game is
   still running are facts about the table rather than things anyone narrates.
   A new `snapshot_seq` field says which seq the board actually is: the newest
   archived at or before `n`, the oldest kept when the caller is further behind
   than the archive is deep, and `null` when the archive was not consulted (no
   `at_seq`) or has nothing (a game this process is not running). A
   non-integer `at_seq` is a 400.

3. **`GameEntry` keeps that archive** (`web/registry.py`): `_record_board` runs
   on the game thread from `on_event`, so what it stores is the state the event
   was emitted against, and `board_at(seq)` reads it back. Bounded by
   `BOARD_HISTORY = 128` entries, and skipped for `BOARD_SKIP_KINDS` — prose,
   money, rolls and errors move nothing, so the state at the event before is
   still the state, and skipping them roughly halves what is kept. A small
   combat's state serializes to ~25 KB, so the ceiling is a few MB per live
   game. `system` is deliberately not in that set: a mid-game encounter spawn
   arrives on it.

Nothing in `engine/`, `agents/`, `llm/` or `orchestrator/` changed.
