"""The cue table: every slot the game can play a sound into, and the event
that fires it.

This is the one authoritative list. `harvest.py` searches per cue, the picker
screen offers one slot per cue, and the manifest written by `fetch.py` carries
each cue's `match` rule along with the file — so a player (browser or server)
needs the manifest and nothing else to route an event to a sound.

A `match` is a kind plus zero or more equality constraints on the event's
`data`, with dotted paths for nested values (`roll.natural`). Constraints are
deliberately dumb: no ranges, no negation, no expressions. Anything that cannot
be said this way is left to the human — those cues carry `match=None` and say
in `when` what fires them.

Nothing here imports the engine: the cue table is data, and `tests/audio`
checks it against `engine.events.EVENT_KINDS` so a new event kind cannot land
without someone deciding whether it makes a noise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

__all__ = [
    "Cue",
    "CUES",
    "CUES_BY_ID",
    "GROUPS",
    "GROUP_DEFAULTS",
    "cue",
    "cues_in",
    "required_cues",
    "event_matches",
    "cues_for_event",
    "cue_for_event",
    "UNSCORED_EVENT_KINDS",
]

GROUPS = ("music", "ambience", "sting", "swell", "sfx")

# Per-group defaults for the fields most cues do not bother to override:
# (min duration, max duration) in seconds, whether it loops, and a starting
# gain in dB. Gains are negative because everything sits under the narration.
GROUP_DEFAULTS = {
    "music": {"dur": (45.0, 420.0), "loop": True, "gain": -15.0},
    "ambience": {"dur": (20.0, 900.0), "loop": True, "gain": -21.0},
    "sting": {"dur": (0.4, 8.0), "loop": False, "gain": -8.0},
    "swell": {"dur": (1.5, 15.0), "loop": False, "gain": -10.0},
    "sfx": {"dur": (0.1, 6.0), "loop": False, "gain": -11.0},
}


@dataclass(frozen=True)
class Cue:
    id: str
    group: str
    label: str
    when: str
    queries: tuple[str, ...]
    dur: tuple[float, float]
    loop: bool
    gain_db: float
    required: bool = False
    match: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["queries"] = list(self.queries)
        d["dur"] = list(self.dur)
        return d


def _cue(group: str, id: str, label: str, when: str, queries, *, match=None,
         required: bool = False, dur=None, loop=None, gain=None) -> Cue:
    d = GROUP_DEFAULTS[group]
    return Cue(
        id=id,
        group=group,
        label=label,
        when=when,
        queries=tuple(queries),
        dur=tuple(dur or d["dur"]),
        loop=d["loop"] if loop is None else loop,
        gain_db=d["gain"] if gain is None else gain,
        required=required,
        match=match,
    )


# --------------------------------------------------------------------------
# Music beds. Long, loopable, and switched by game phase rather than by a
# single event — the phase cues carry a match where one event unambiguously
# starts the phase, and none where the phase is a judgement call.
# --------------------------------------------------------------------------

_MUSIC = [
    _cue("music", "music_explore", "Exploration bed",
         "Out of combat and nothing is wrong yet: travel, searching, doors.",
         ["dark fantasy ambient loop", "dungeon exploration music", "medieval mystery loop"],
         required=True),
    _cue("music", "music_tension", "Tension bed",
         "Something is about to go wrong. Set by the DM, or by a failed stealth check.",
         ["dark tension underscore loop", "suspense drone strings", "ominous low pulse"],
         required=True),
    _cue("music", "music_combat", "Combat bed",
         "Initiative is rolled and the party is not yet losing.",
         ["epic battle music loop", "orchestral combat percussion", "fantasy battle drums"],
         match={"kind": "combat_start"}, required=True),
    _cue("music", "music_combat_desperate", "Desperate combat bed",
         "A party member is at 0 HP — swap under the combat bed.",
         ["desperate battle music", "dark orchestral combat", "frantic strings ostinato"],
         match={"kind": "down"}),
    _cue("music", "music_victory", "Victory",
         "Combat ends with the party standing. One-shot, then back to exploration.",
         ["fantasy victory fanfare", "triumphant orchestral short", "heroic resolution"],
         match={"kind": "combat_end"}, loop=False, dur=(3.0, 45.0), required=True),
    _cue("music", "music_defeat", "Defeat",
         "The party is wiped or the game ends badly. One-shot.",
         ["sad orchestral ending", "defeat music fantasy", "requiem short strings"],
         loop=False, dur=(3.0, 60.0)),
    _cue("music", "music_downtime", "Downtime bed",
         "Talk, shopping, rest, a scene with no threat in it.",
         ["medieval tavern music loop", "gentle lute fantasy", "calm folk instrumental"],
         ),
]

# --------------------------------------------------------------------------
# Ambience beds. Chosen per scenario, not per event: `examples/*.json` says
# what the place is, and the assignment maps a place to one of these.
# --------------------------------------------------------------------------

_AMBIENCE = [
    _cue("ambience", "amb_dungeon_stone", "Stone corridors",
         "Worked stone, still air, far-off drips. The default indoors bed.",
         ["dungeon ambience loop", "stone corridor room tone", "underground hall ambience"],
         required=True),
    _cue("ambience", "amb_cave_dripping", "Wet cave",
         "Natural rock, running water, echo.",
         ["cave ambience water drips", "underground stream echo", "wet cavern ambience"]),
    _cue("ambience", "amb_crypt_undead", "Crypt",
         "Sealed air, distant moans, the dead not resting.",
         ["crypt ambience ghostly", "tomb ambience whispers", "haunted dungeon drone"],
         required=True),
    _cue("ambience", "amb_mine_deep", "Deep mine",
         "Timber, grit, the pressure of rock overhead.",
         ["mine ambience creaking timber", "deep underground rumble", "cavern low drone"]),
    _cue("ambience", "amb_forest_night", "Night forest",
         "Leaves, owls, insects, the odd branch.",
         ["forest night ambience", "woodland night crickets owl", "dark forest wind trees"],
         required=True),
    _cue("ambience", "amb_marsh_fen", "Fen and marsh",
         "Standing water, frogs, gas, reeds.",
         ["swamp ambience frogs", "marsh night ambience", "bog water insects"]),
    _cue("ambience", "amb_camp_fire", "Campfire",
         "A fire close enough to hear, and not much else.",
         ["campfire crackling loop", "fireplace ambience", "bonfire close"],
         required=True),
    _cue("ambience", "amb_village_road", "Road and village",
         "Outdoors, human, daylight — a tollhouse, a track, a hamlet.",
         ["village ambience distant", "country road ambience birds", "medieval village crowd distant"]),
    _cue("ambience", "amb_rain_wind", "Weather",
         "Rain or wind laid over any other bed.",
         ["rain on stone ambience", "wind howling loop", "storm distant thunder ambience"]),
]

# --------------------------------------------------------------------------
# Stings. Short, one-shot, and mostly a direct read of one event.
# --------------------------------------------------------------------------

_STINGS = [
    _cue("sting", "sting_combat_start", "Initiative",
         "Combat begins.",
         ["battle start sting", "combat begin hit orchestral", "danger stinger short"],
         match={"kind": "combat_start"}, required=True),
    _cue("sting", "sting_combat_end", "Combat over",
         "The last enemy drops, or the fight is called.",
         ["resolution sting", "combat end chord", "release stinger orchestral"],
         match={"kind": "combat_end"}),
    _cue("sting", "sting_crit", "Critical hit",
         "An attack roll crits.",
         ["critical hit sting", "impact hit orchestral short", "heavy metal impact stinger"],
         match={"kind": "attack", "data": {"hit": True, "crit": True}}, required=True),
    _cue("sting", "sting_fumble", "Natural 1",
         "An attack misses on a natural 1.",
         ["comedy fail sting", "wah wah trombone short", "fumble stinger"],
         match={"kind": "attack", "data": {"hit": False, "roll.natural": 1}}),
    _cue("sting", "sting_down", "A combatant drops",
         "Anyone falls unconscious at 0 HP.",
         ["dramatic impact sting low", "body fall stinger", "dark hit orchestral"],
         match={"kind": "down"}, required=True),
    _cue("sting", "sting_dead", "Death",
         "A combatant dies outright.",
         ["death sting low strings", "dark ominous impact", "funeral bell single"],
         match={"kind": "dead"}, required=True),
    _cue("sting", "sting_stable", "Stabilised",
         "A dying character is stabilised.",
         ["relief sting soft", "gentle resolve chord", "warm short swell"],
         match={"kind": "stable"}),
    _cue("sting", "sting_death_save_fail", "Death save failed",
         "A death saving throw fails.",
         ["heartbeat single low", "dark low thud", "tension hit short"],
         match={"kind": "death_save", "data": {"success": False}}, required=True),
    _cue("sting", "sting_death_save_pass", "Death save passed",
         "A death saving throw succeeds.",
         ["hopeful short chime", "soft rising note", "gentle bell single"],
         match={"kind": "death_save", "data": {"success": True}}),
    _cue("sting", "sting_scene", "Scene change",
         "A new scene opens.",
         ["scene transition whoosh", "chapter transition sting", "soft cinematic transition"],
         match={"kind": "scene"}, required=True),
    _cue("sting", "sting_dm_note", "DM note",
         "The table injects a note mid-game.",
         ["notification soft chime", "ui alert gentle", "short marimba notify"],
         match={"kind": "dm_note"}),
]

# --------------------------------------------------------------------------
# Swells. Longer than a sting, no impact — they lead somewhere. All manual:
# no single event says "this is the big moment".
# --------------------------------------------------------------------------

_SWELLS = [
    _cue("swell", "swell_dread", "Dread swell",
         "Undead rise, the door opens on something bad. DM-triggered.",
         ["dark riser dread", "horror swell low strings", "ominous rise cinematic"],
         required=True),
    _cue("swell", "swell_heroic", "Heroic swell",
         "The turnaround: the cleric's channel lands, the boss staggers.",
         ["heroic riser orchestral", "uplifting swell brass", "triumphant rise short"]),
    _cue("swell", "swell_reveal", "Reveal swell",
         "A room, a map, a truth. Under scene narration.",
         ["cinematic reveal swell", "mystery reveal shimmer", "magical discovery swell"]),
]

# --------------------------------------------------------------------------
# Effects. The mechanical layer: dice, weapons, damage by type, spells.
# Damage cues match `damage_type`, which is the one field the engine gives us
# that reliably says what the blow felt like.
# --------------------------------------------------------------------------

_SFX = [
    _cue("sfx", "sfx_dice", "Dice",
         "Any explicit roll event.",
         ["dice roll table wood", "rolling dice single", "d20 roll"],
         match={"kind": "roll"}, required=True),
    _cue("sfx", "sfx_attack_hit", "Attack hits",
         "An attack connects. Generic — the damage cues carry the flavour.",
         ["sword hit flesh", "weapon impact hit", "melee hit body"],
         match={"kind": "attack", "data": {"hit": True}}, required=True),
    _cue("sfx", "sfx_attack_miss", "Attack misses",
         "An attack roll misses.",
         ["sword swing whoosh miss", "weapon swoosh air", "blade miss whoosh"],
         match={"kind": "attack", "data": {"hit": False}}, required=True),
    _cue("sfx", "sfx_dmg_slashing", "Slashing damage",
         "damage_type = slashing.",
         ["sword slash flesh", "blade cut wet", "slash impact"],
         match={"kind": "damage", "data": {"damage_type": "slashing"}}, required=True),
    _cue("sfx", "sfx_dmg_piercing", "Piercing damage",
         "damage_type = piercing.",
         ["arrow impact flesh", "stab knife", "spear pierce"],
         match={"kind": "damage", "data": {"damage_type": "piercing"}}, required=True),
    _cue("sfx", "sfx_dmg_bludgeoning", "Bludgeoning damage",
         "damage_type = bludgeoning.",
         ["club hit body thud", "blunt impact heavy", "mace hit"],
         match={"kind": "damage", "data": {"damage_type": "bludgeoning"}}, required=True),
    _cue("sfx", "sfx_dmg_fire", "Fire damage",
         "damage_type = fire.",
         ["fire whoosh burst", "flame burst magic", "fireball explosion"],
         match={"kind": "damage", "data": {"damage_type": "fire"}}, required=True),
    _cue("sfx", "sfx_dmg_cold", "Cold damage",
         "damage_type = cold.",
         ["ice freeze magic", "frost shatter", "cold wind magic hit"],
         match={"kind": "damage", "data": {"damage_type": "cold"}}),
    _cue("sfx", "sfx_dmg_lightning", "Lightning damage",
         "damage_type = lightning.",
         ["lightning strike zap", "electric discharge magic", "thunder crack close"],
         match={"kind": "damage", "data": {"damage_type": "lightning"}}),
    _cue("sfx", "sfx_dmg_necrotic", "Necrotic damage",
         "damage_type = necrotic.",
         ["dark magic drain", "necrotic whoosh evil", "soul drain effect"],
         match={"kind": "damage", "data": {"damage_type": "necrotic"}}),
    _cue("sfx", "sfx_dmg_radiant", "Radiant damage",
         "damage_type = radiant.",
         ["holy magic burst", "divine light shimmer hit", "angelic chime impact"],
         match={"kind": "damage", "data": {"damage_type": "radiant"}}),
    _cue("sfx", "sfx_dmg_poison", "Poison damage",
         "damage_type = poison.",
         ["poison bubbling hiss", "acid sizzle", "toxic splash"],
         match={"kind": "damage", "data": {"damage_type": "poison"}}),
    _cue("sfx", "sfx_dmg_acid", "Acid damage",
         "damage_type = acid.",
         ["acid burn sizzle", "corrosive hiss", "acid splash"],
         match={"kind": "damage", "data": {"damage_type": "acid"}}),
    _cue("sfx", "sfx_dmg_thunder", "Thunder damage",
         "damage_type = thunder.",
         ["thunder boom close", "shockwave blast", "concussive boom"],
         match={"kind": "damage", "data": {"damage_type": "thunder"}}),
    _cue("sfx", "sfx_dmg_force", "Force damage",
         "damage_type = force.",
         ["magic missile impact", "arcane force hit", "energy blast short"],
         match={"kind": "damage", "data": {"damage_type": "force"}}),
    _cue("sfx", "sfx_spell_cast", "Spell cast",
         "Any spell goes off. The damage cue carries the element.",
         ["magic spell cast whoosh", "arcane casting shimmer", "spell charge release"],
         match={"kind": "spell_cast"}, required=True),
    _cue("sfx", "sfx_heal", "Healing",
         "Hit points are restored.",
         ["healing magic chime", "restore health shimmer", "warm magic heal"],
         match={"kind": "heal"}, required=True),
    _cue("sfx", "sfx_save", "Saving throw",
         "A saving throw resolves — quiet, it fires often.",
         ["soft ui tick", "short woosh subtle", "light magic tick"],
         match={"kind": "save"}, gain=-18.0),
    _cue("sfx", "sfx_condition_add", "Condition applied",
         "Someone becomes poisoned, prone, frightened, grappled…",
         ["debuff magic short", "curse effect short", "dark magic tick"],
         match={"kind": "condition_add"}),
    _cue("sfx", "sfx_condition_remove", "Condition ends",
         "A condition wears off.",
         ["buff release short", "magic dissipate", "soft magic off"],
         match={"kind": "condition_remove"}, gain=-18.0),
    _cue("sfx", "sfx_concentration_broken", "Concentration broken",
         "A caster loses concentration.",
         ["magic fizzle out", "spell fail glitch", "magic power down"],
         match={"kind": "concentration_broken"}),
    _cue("sfx", "sfx_move", "Movement",
         "A combatant moves on the grid.",
         ["footsteps stone single", "armour step", "boot step gravel"],
         match={"kind": "move"}, gain=-20.0),
    _cue("sfx", "sfx_turn_start", "Turn begins",
         "The initiative pointer advances.",
         ["ui soft tick", "subtle marker beep", "wood block short"],
         match={"kind": "turn_start"}, gain=-22.0),
    _cue("sfx", "sfx_skill_check", "Skill check",
         "A skill check resolves.",
         ["ui click soft", "page turn short", "light tick wood"],
         match={"kind": "skill_check"}, gain=-20.0),
    _cue("sfx", "sfx_error", "Error",
         "The orchestrator emits an error event.",
         ["error tone soft", "ui negative short", "low error beep"],
         match={"kind": "error"}, gain=-16.0),
]

CUES: tuple[Cue, ...] = tuple(_MUSIC + _AMBIENCE + _STINGS + _SWELLS + _SFX)
CUES_BY_ID = {c.id: c for c in CUES}

# Event kinds that deliberately make no noise. `turn_end` and `system` fire
# constantly and carry nothing a listener needs; `dialogue`, `narration` and
# `cost` belong to the narration and the meter, not to the score. Listed here
# so the test can insist every kind is either scored or explicitly waived.
UNSCORED_EVENT_KINDS = frozenset({
    "round_start", "turn_end", "system", "narration", "dialogue", "cost",
})

assert len(CUES_BY_ID) == len(CUES), "duplicate cue id"


def cue(cue_id: str) -> Cue:
    return CUES_BY_ID[cue_id]


def cues_in(group: str) -> list[Cue]:
    return [c for c in CUES if c.group == group]


def required_cues() -> list[Cue]:
    return [c for c in CUES if c.required]


def _dig(data: dict, path: str):
    """`a.b` → data['a']['b'], or None where the path runs out."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def event_matches(match: dict | None, event: dict) -> bool:
    """True where `event` (an `Event.to_dict()`) satisfies a cue's match rule."""
    if not match:
        return False
    if event.get("kind") != match.get("kind"):
        return False
    data = event.get("data") or {}
    for path, want in (match.get("data") or {}).items():
        got = _dig(data, path)
        # `1 == True` in Python and that is not what a cue means by it.
        if isinstance(want, bool) != isinstance(got, bool):
            return False
        if got != want:
            return False
    return True


def cues_for_event(event: dict, cues: tuple[Cue, ...] = CUES) -> list[Cue]:
    """Every layer this event fires: at most one cue per group.

    Audio layers rather than replaces — `combat_start` swaps the music bed
    *and* hits a sting, and a crit wants both its sting and the hit effect —
    so one event can light one cue in each group and no more. Within a group
    the most specific match wins: `sting_crit` (`hit` + `crit`) beats nothing
    else in stings, and `sfx_attack_hit` (`hit`) loses to nothing in effects.
    Ties break on declaration order.
    """
    best: dict[str, tuple[int, Cue]] = {}
    for c in cues:
        if not event_matches(c.match, event):
            continue
        rank = len((c.match or {}).get("data") or {})
        if rank > best.get(c.group, (-1, None))[0]:
            best[c.group] = (rank, c)
    return [best[g][1] for g in GROUPS if g in best]


def cue_for_event(event: dict, group: str, cues: tuple[Cue, ...] = CUES) -> Cue | None:
    """The cue this event fires in one group, or None."""
    if group not in GROUPS:
        raise ValueError(f"unknown cue group {group!r}")
    for c in cues_for_event(event, cues):
        if c.group == group:
            return c
    return None
