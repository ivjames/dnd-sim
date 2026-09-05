"""Compact text views of game state for LLM prompts.

Token frugality is the whole point of this module: a player view for a
9-combatant fight must stay well under ~700 tokens. Everything here is
duck-typed against CONTRACTS.md §1 so the engine can evolve independently.
"""

from __future__ import annotations

from typing import Any, Iterable

from .common import event_text

__all__ = [
    "player_view",
    "dm_view",
    "render_actions",
    "hp_band",
    "party_summary",
    "pronouns_for",
    "pronouns_of_sheet",
    "surprised",
    "PRONOUNS",
    "DEFAULT_PRONOUNS",
]

MAX_RECENT_EVENTS = 12
# Mechanical noise the LLM does not need one line each for.
_SKIP_KINDS = {"roll", "turn_start", "turn_end", "round_start", "system", "cost"}


#: How a character's LEGACY stated gender is read as pronouns for narration.
#:
#: A party spec states `pronouns` now, and where it does this table is not
#: consulted at all: the authored answer is already the thing this column
#: wants, and reading it needs no inference. The table remains for the older
#: `gender` key, which a stranger's config and a game persisted before the
#: change still carry.
#:
#: The spellings are exactly those `tts.voices.GENDERS` accepts, and
#: `tests/orchestrator/test_narration_attribution.py` runs the two against each other
#: so they cannot drift apart: one authored answer on a party spec should not
#: cast a voice one way and narrate a character the other. It is not imported
#: from there — `agents/` sits under `orchestrator/` in the layering and `tts/`
#: is the web layer's, so the table is copied and pinned rather than reached
#: across for.
PRONOUNS = {
    "f": "she/her", "female": "she/her", "woman": "she/her",
    "m": "he/him", "male": "he/him", "man": "he/him",
}

#: Everything else, and that is the answer far more often than not.
#:
#: A monster gets this always. `monster_to_combatant` builds from an SRD stat
#: block, which states a size and a type and no gender, so there is nothing
#: authored to read — and the fix for a Bandit Captain who is "she" in round 2
#: and "he" in round 5 is not to deal her a gender out of the dice, it is to
#: stop the question being open. Singular "they" is what English already does
#: for a referent whose gender is not established, it is stable because it is
#: the same for every monster in every game rather than drawn per instance, and
#: it invents nothing about a creature nobody wrote. The same goes for a party
#: member whose own spec is silent: unstated stays unstated.
DEFAULT_PRONOUNS = "they/them"


def pronouns_for(c: Any) -> str:
    """The pronouns narration should use for `c` — from its sheet, or the default.

    A stated `pronouns` is used **as written**: it is already the answer this
    column asks for, and the same authored string is what `tts.voices` reads to
    pick a voice, so the DM narrates a character in the pronouns it is spoken
    in. A spec that states only the older `gender` is read through `PRONOUNS`.

    Only a character sheet can carry an answer, and only where the party spec
    that built it stated one. Nothing is inferred from a name, a class or a
    stat block.
    """
    return pronouns_of_sheet(getattr(c, "sheet", None))


def pronouns_of_sheet(sheet: Any) -> str:
    """`pronouns_for` for a bare sheet, for a caller that has no combatant.

    The player's own cached system block is built from its `CharacterSheet`
    alone, and it has to answer this the same way the COMBATANTS column will —
    a prompt that introduces a character as they/them and then lists her as
    she/her has told the model two things in one breath.
    """
    said = str(getattr(sheet, "pronouns", "") or "").strip()
    if said:
        return said
    stated = str(getattr(sheet, "gender", "") or "").strip().lower()
    return PRONOUNS.get(stated, DEFAULT_PRONOUNS)


# --- small helpers ---------------------------------------------------------


def hp_band(hp: int, max_hp: int) -> str:
    if hp <= 0:
        return "down"
    if max_hp <= 0:
        return "healthy"
    pct = hp / max_hp
    if pct > 0.85:
        return "healthy"
    if pct > 0.5:
        return "wounded"
    if pct > 0.25:
        return "bloodied"
    return "critical"


def _mod(score: int) -> int:
    return (score - 10) // 2


def _condition_names(c: Any) -> list[str]:
    names = []
    for cond in getattr(c, "conditions", None) or []:
        name = getattr(cond, "name", None) or (
            cond.get("name") if isinstance(cond, dict) else str(cond)
        )
        if name:
            names.append(str(name))
    return names


#: The word both views print for a creature that has not had its first turn of
#: a combat it was ambushed in.
SURPRISED = "surprised"


def surprised(c: Any) -> bool:
    """Whether `c` is surprised, however the engine chose to record it.

    Surprise is not one of the SRD's fifteen conditions and `conditions.json`
    does not carry it, so the engine may hold it as a flag, an attribute, or a
    condition of its own; all three are read here rather than one of them being
    picked and the view going quiet if the engine picked another. Being quiet
    is the failure that matters: a surprised character offered nothing but
    `end_turn` and told nothing about why spends its one call arguing with the
    list.
    """
    if getattr(c, SURPRISED, False):
        return True
    flags = getattr(c, "flags", None)
    if isinstance(flags, dict) and flags.get(SURPRISED):
        return True
    return any(n.lower() == SURPRISED for n in _condition_names(c))


def _conds(c: Any) -> str:
    names = _condition_names(c)
    if surprised(c) and not any(n.lower() == SURPRISED for n in names):
        names.append(SURPRISED)
    return ",".join(names) if names else "-"


def _pos(c: Any) -> tuple[int, int]:
    p = getattr(c, "position", (0, 0)) or (0, 0)
    return (int(p[0]), int(p[1]))


def _distance_ft(state: Any, a: Any, b: Any) -> int:
    pa, pb = _pos(a), _pos(b)
    grid = getattr(state, "grid", None)
    if grid is not None and hasattr(grid, "distance_ft"):
        try:
            return int(grid.distance_ft(pa, pb))
        except Exception:  # noqa: BLE001 - fall through to the 5e default
            pass
    return 5 * max(abs(pa[0] - pb[0]), abs(pa[1] - pb[1]))


def _active(c: Any) -> bool:
    return not getattr(c, "dead", False)


def _active_id(state: Any) -> str | None:
    """The combatant whose turn it is, or None outside combat.

    Duck-typed like everything else here: a state object that cannot answer
    (a test fake, a snapshot dict) simply has no turn to report.
    """
    fn = getattr(state, "active_id", None)
    if not callable(fn):
        return None
    try:
        cid = fn()
    except Exception:  # noqa: BLE001 - a view must never be the thing that fails
        return None
    return str(cid) if cid else None


def _role(c: Any) -> str:
    """What a character is, for a view that otherwise carries only a name.

    A narrator with no class column calls the wizard who just went down "the
    downed cleric", for the same reason it guesses pronouns from a name: the
    fact was never in front of it.
    """
    sheet = getattr(c, "sheet", None)
    if sheet is not None:
        return f"{getattr(sheet, 'klass', '?')} {getattr(sheet, 'level', 1)}"
    return "—"


def _sheet_line(c: Any) -> str:
    """One dense line describing a PC's own capabilities."""
    sheet = getattr(c, "sheet", None)
    name = getattr(c, "name", "?")
    if sheet is None:
        return (
            f"{name} — HP {getattr(c, 'hp', 0)}/{getattr(c, 'max_hp', 0)} "
            f"AC {getattr(c, 'ac', 10)} speed {getattr(c, 'speed', 30)}ft"
        )
    abil = getattr(c, "abilities", None) or getattr(sheet, "abilities", {}) or {}
    abil_str = " ".join(
        f"{k}{_mod(v):+d}" for k, v in abil.items()
    )
    parts = [
        f"{name} — {getattr(sheet, 'race', '?')} {getattr(sheet, 'klass', '?')} "
        f"{getattr(sheet, 'level', 1)}",
        f"HP {getattr(c, 'hp', 0)}/{getattr(c, 'max_hp', 0)}"
        + (f"(+{c.temp_hp} temp)" if getattr(c, "temp_hp", 0) else ""),
        f"AC {getattr(c, 'ac', 10)}",
        f"speed {getattr(c, 'speed', 30)}ft",
        f"prof +{getattr(c, 'proficiency', 2)}",
        abil_str,
    ]
    return " | ".join(p for p in parts if p)


def _resources(c: Any) -> str:
    res = getattr(c, "resources", None) or {}
    bits = []
    slots = res.get("spell_slots") or {}
    if slots:
        bits.append(
            "slots " + " ".join(f"L{lvl}:{n}" for lvl, n in sorted(slots.items()) if n)
        )
    for key, val in res.items():
        if key == "spell_slots":
            continue
        bits.append(f"{key}:{val}")
    turn = getattr(c, "turn", None) or {}
    if turn:
        econ = []
        if not turn.get("action", False):
            econ.append("action")
        if not turn.get("bonus", False):
            econ.append("bonus")
        left = turn.get("movement_left")
        if left is not None:
            econ.append(f"{left}ft move")
        if econ:
            bits.append("available: " + ", ".join(econ))
    return "; ".join(bits) if bits else "none"


def _event_lines(recent: Iterable[Any], limit: int = MAX_RECENT_EVENTS) -> list[str]:
    lines: list[str] = []
    for ev in recent or []:
        kind = getattr(ev, "kind", "") or (ev.get("kind") if isinstance(ev, dict) else "")
        text = event_text(ev)
        if not text:
            continue
        if kind in _SKIP_KINDS:
            continue
        lines.append(text)
    return lines[-limit:]


def _scene_line(state: Any) -> str:
    scene = getattr(state, "scene", None) or {}
    title = scene.get("title") or ""
    desc = scene.get("description") or ""
    loc = scene.get("location") or ""
    head = " — ".join(p for p in (title, loc) if p)
    if desc:
        desc = " ".join(str(desc).split())
        if len(desc) > 220:
            desc = desc[:217] + "..."
    line = f"{head}: {desc}" if head else desc
    objectives = scene.get("objectives") or []
    if objectives:
        line += " Objectives: " + "; ".join(str(o) for o in objectives[:3]) + "."
    return line or "(no scene set)"


def party_summary(state: Any) -> str:
    """One-line-per-PC roster, used to prime the DM at scene open."""
    rows = []
    for c in (getattr(state, "combatants", None) or {}).values():
        if getattr(c, "side", "") != "party":
            continue
        sheet = getattr(c, "sheet", None)
        if sheet is not None:
            rows.append(
                f"{c.name} ({pronouns_for(c)}, {getattr(sheet, 'race', '?')} "
                f"{getattr(sheet, 'klass', '?')} {getattr(sheet, 'level', 1)}): "
                f"{getattr(sheet, 'persona', '') or 'no notes'}"
            )
        else:
            rows.append(f"{c.name}")
    return "\n".join(rows)


# --- the views -------------------------------------------------------------


def player_view(state: Any, actor_id: str, recent: list, summary: str) -> str:
    """Compact view for one PC. Enemy HP is banded, never numeric."""
    combatants = getattr(state, "combatants", None) or {}
    me = combatants.get(actor_id)
    lines: list[str] = []
    lines.append(f"SCENE: {_scene_line(state)}")
    mode = getattr(state, "mode", "exploration")
    if mode == "combat":
        lines.append(f"COMBAT round {getattr(state, 'round', 1)}")
    if summary:
        lines.append(f"SO FAR: {' '.join(str(summary).split())}")

    if me is not None:
        lines.append("")
        lines.append("YOU: " + _sheet_line(me))
        lines.append("Resources: " + _resources(me))
        lines.append("Your conditions: " + _conds(me))
        if surprised(me):
            lines.append(
                "YOU ARE SURPRISED: you were caught unawares and cannot act or "
                "move on this first turn, and you take no reactions until it "
                "ends. End your turn."
            )

    lines.append("")
    # Coordinates as well as distances. A distance says how far a square is,
    # never which square it is, so a character asked for a `path` had nothing
    # to build one out of and guessed — which is most of what the engine was
    # rejecting. The DM view has carried positions from the start; this is the
    # same column, and it costs about eight characters a row.
    lines.append("COMBATANTS (name | pronouns | side | health | pos | dist | conditions)")
    for cid, c in combatants.items():
        if not _active(c):
            continue
        side = getattr(c, "side", "?")
        if cid == actor_id:
            health = f"{getattr(c, 'hp', 0)}/{getattr(c, 'max_hp', 0)}"
            dist = "0ft"
            tag = " (you)"
        else:
            health = hp_band(getattr(c, "hp", 0), getattr(c, "max_hp", 1))
            dist = f"{_distance_ft(state, me, c)}ft" if me is not None else "?"
            tag = ""
        pos = _pos(c)
        lines.append(
            f"{cid} {getattr(c, 'name', '?')}{tag} | {pronouns_for(c)} | "
            f"{side} | {health} | ({pos[0]},{pos[1]}) | {dist} | {_conds(c)}"
        )

    events = _event_lines(recent)
    if events:
        lines.append("")
        lines.append("RECENT:")
        lines.extend(f"- {e}" for e in events)
    return "\n".join(lines)


def dm_view(state: Any, recent: list, summary: str) -> str:
    """Same shape as player_view but omniscient: exact HP, all sides, positions."""
    combatants = getattr(state, "combatants", None) or {}
    lines: list[str] = []
    lines.append(f"SCENE: {_scene_line(state)}")
    mode = getattr(state, "mode", "exploration")
    lines.append(
        f"MODE: {mode}"
        + (f" | round {getattr(state, 'round', 1)}" if mode == "combat" else "")
    )
    if summary:
        lines.append(f"SO FAR: {' '.join(str(summary).split())}")

    # Whose turn it is, said outright. The DM used to have to infer it from
    # which name happened to lead the event list, and that is wrong whenever a
    # turn opens with someone else's reaction — an opportunity attack on the
    # mover goes first in the list about half the time — which is how a turn
    # belonging to a PC gets narrated as the monster's, and how the DM ends up
    # acting for a player character it was told not to act for.
    actor_id = _active_id(state)
    if actor_id is not None:
        actor = combatants.get(actor_id)
        turn_line = f"TURN: {actor_id} {getattr(actor, 'name', '?')}"
        if actor is not None and surprised(actor):
            turn_line += " — SURPRISED: it can only end its turn, and takes no reactions"
        lines.append(turn_line)

    lines.append("")
    lines.append("COMBATANTS (id | name | pronouns | side | class | HP | AC | pos | conditions)")
    for cid, c in combatants.items():
        status = ""
        if getattr(c, "dead", False):
            status = " DEAD"
        elif getattr(c, "hp", 1) <= 0:
            status = " DOWN"
        pos = _pos(c)
        lines.append(
            f"{cid} | {getattr(c, 'name', '?')} | {pronouns_for(c)} | "
            f"{getattr(c, 'side', '?')} | {_role(c)} | "
            f"{getattr(c, 'hp', 0)}/{getattr(c, 'max_hp', 0)} | AC {getattr(c, 'ac', 10)} | "
            f"({pos[0]},{pos[1]}) | {_conds(c)}{status}"
        )

    events = _event_lines(recent)
    if events:
        lines.append("")
        lines.append("RECENT:")
        lines.extend(f"- {e}" for e in events)
    return "\n".join(lines)


#: How many suggested destinations a move template prints. The engine offers
#: more than a prompt wants to read.
MAX_SUGGESTED = 6


def render_actions(templates: list) -> str:
    """`[a1] Attack Goblin 2 with Longsword (+5, 1d8+3)` — one per line.

    A template that offers `suggested` squares may also offer `labels` saying
    what each one is for ("adjacent to Goblin 2", "away from all enemies").
    Those go on a continuation line beneath, never appended after
    `suggested=[...]`: `MockLLMClient._params_for` reads the suggestions with a
    line-anchored regex, and anything printed after them on that line takes the
    mock's destinations away and every mock move with them.
    """
    out: list[str] = []
    for t in templates or []:
        tid = getattr(t, "id", None) or (t.get("id") if isinstance(t, dict) else "?")
        label = getattr(t, "label", None) or (
            t.get("label") if isinstance(t, dict) else ""
        )
        needs = getattr(t, "needs", None)
        if needs is None and isinstance(t, dict):
            needs = t.get("needs")
        line = f"[{tid}] {label}"
        note = ""
        if needs:
            line += f"  needs={list(needs)}"
            params = getattr(t, "params", None)
            if params is None and isinstance(t, dict):
                params = t.get("params")
            suggested = list((params or {}).get("suggested") or [])[:MAX_SUGGESTED]
            if suggested:
                line += f" suggested={suggested}"
                labels = [str(x) for x in ((params or {}).get("labels") or [])]
                pairs = [
                    f"{list(sq)} = {lab}"
                    for sq, lab in zip(suggested, labels)
                    if str(lab).strip()
                ]
                if pairs:
                    note = "    where " + "; ".join(pairs)
        out.append(line)
        if note:
            out.append(note)
    return "\n".join(out)
