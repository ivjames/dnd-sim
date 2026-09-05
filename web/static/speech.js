/* dnd-sim spoken narration — the pure half. No DOM, no speechSynthesis, no
   state: given an event (engine/events.py shape) and the voice settings,
   decide whether it is spoken, what words are said, and which voice says
   them. app.js owns the queue and the Web Speech calls. Exposed as the global
   `DndSpeech` for the page and as module.exports for `node` tests. */
(function (root) {
  'use strict';

  // Story beats: always spoken while voice is on.
  var STORY = { narration: 1, dialogue: 1, scene: 1, dm_note: 1, down: 1, dead: 1 };
  // Mechanics: spoken in a short shaped form unless "mute mechanics" is set.
  var MECH = {
    attack: 1, damage: 1, heal: 1, save: 1, skill_check: 1, move: 1, spell_cast: 1,
    condition_add: 1, condition_remove: 1, concentration_broken: 1, death_save: 1,
    stable: 1, combat_start: 1, combat_end: 1, round_start: 1, turn_start: 1,
    system: 1, cost: 1
  };
  // Never spoken: turn_end (redundant with the next turn_start), roll (the
  // damage/heal event that follows says it better), error (belongs on screen).

  var ABILITY = { STR: 'Strength', DEX: 'Dexterity', CON: 'Constitution',
                  INT: 'Intelligence', WIS: 'Wisdom', CHA: 'Charisma' };

  var MAX_CHUNK = 220;   // Chrome stops long utterances (~15 s) — split at sentences

  // Monsters and NPCs share one voice, and on most machines the hash used to
  // land it anywhere in the same pool the PCs came from — same voice, a pitch
  // nudge of ±0.05, indistinguishable from a person. So the npc key now gets a
  // voice reserved out of the PC pool *and* a timbre of its own: lower and a
  // shade slower than any PC can be given by default, so the difference
  // survives a device with one voice. Both are overridable per role (the voice
  // lab in the UI writes those overrides).
  var NPC_PITCH = 0.72;
  var NPC_RATE = 0.92;
  var PITCH_MIN = 0.1, PITCH_MAX = 2, RATE_MIN = 0.5, RATE_MAX = 2;

  function isStory(ev) { return !!(ev && STORY[ev.kind]); }
  function isMechanic(ev) { return !!(ev && MECH[ev.kind]); }

  function shouldSpeak(ev, settings) {
    settings = settings || {};
    if (!ev || !settings.enabled) return false;
    if (STORY[ev.kind]) return true;
    if (MECH[ev.kind]) return !settings.muteMechanics;
    return false;
  }

  // Drop dice expressions, parentheticals and [reason] tags from an engine
  // line: "X takes 6 piercing damage (17 → 11 HP)" → "X takes 6 piercing damage".
  function stripDice(s) {
    return String(s || '')
      .replace(/\s*\[[^\]]*\]/g, '')
      .replace(/\s*\([^()]*\)/g, '')
      .replace(/\s*\d*d\d+(?:\s*[+-]\s*\d+)?\s*→\s*\d+/g, '')
      .replace(/\bHP\b/g, 'hit points')
      .replace(/:\s*,/g, ':')
      .replace(/\s+,/g, ',')
      .replace(/\s{2,}/g, ' ')
      .replace(/[\s:,]+$/, '')
      .trim();
  }

  function nameFrom(names, id) {
    return (names && id && names[id]) ? String(names[id]) : null;
  }

  function splitSpeaker(text) {
    var m = /^([^:]{1,60}):\s*(.*)$/.exec(String(text || ''));
    return m ? { who: m[1].trim(), said: m[2].trim() } : { who: null, said: String(text || '').trim() };
  }

  // Party membership comes from the caller (`party`: {id: true} built from the
  // snapshot's combatants with side 'party', or the config's party ids). Ids
  // are arbitrary strings, so the 'pc_' prefix is only a fallback when no
  // party information has been supplied at all.
  function isPC(actor, party) {
    if (typeof actor !== 'string' || !actor) return false;
    if (party && typeof party === 'object') return !!party[actor];
    return actor.indexOf('pc_') === 0;
  }

  // What is said for an event, or null for "nothing". `names` is an optional
  // {id: display name} map (the snapshot's combatants) used where the engine
  // line carries only an id.
  function phraseFor(ev, names, party) {
    if (!ev) return null;
    var d = ev.data || {};
    var text = String(ev.text || '');
    var m;
    switch (ev.kind) {
      case 'narration':
        return text.trim() || null;

      case 'dialogue': {
        var sp = splitSpeaker(text);
        if (!sp.said) return null;
        var who = sp.who || nameFrom(names, ev.actor);
        // PCs have their own voice; the voice is the attribution. Everyone
        // else shares the NPC voice, so keep the name.
        return isPC(ev.actor, party) || !who ? sp.said : who + ': ' + sp.said;
      }

      case 'scene': {
        var sc = d.scene || {};
        var head = text.trim() || (sc.title ? 'Scene: ' + sc.title : 'A new scene');
        var desc = String(sc.description || '').trim();
        return desc ? head.replace(/[.!]$/, '') + '. ' + desc : head;
      }

      case 'dm_note': {
        var note = String(d.text || text.replace(/^DM NOTE FROM TABLE:\s*/i, '')).trim();
        return note ? 'Note from the table: ' + note : null;
      }

      case 'down': case 'dead': case 'stable': case 'condition_add':
      case 'condition_remove': case 'concentration_broken': case 'death_save':
        return stripDice(text) || null;

      case 'system':
        if (d.options || d.seed !== undefined || d.game_id) return null;   // menus and bookkeeping
        return stripDice(text) || null;

      case 'combat_start':
        return 'Roll for initiative!';

      case 'combat_end':
        return stripDice(text) || 'Combat ends.';

      case 'round_start':
        return 'Round ' + (d.round || ev.round || '') + '.';

      case 'turn_start': {
        var tn = nameFrom(names, ev.actor) || d.name || d.actor_name;
        if (!tn) { m = /^(.+?)'s turn/.exec(text); tn = m ? m[1] : null; }
        return tn ? tn + "'s turn." : null;
      }

      case 'attack': {
        // A hit is voiced by the damage event that follows; here only the
        // miss (and the excitement of a crit) is worth a breath.
        m = /^(.+?) (?:attacks|makes an opportunity attack on) (.+?) with /.exec(text);
        var atk = (m && m[1]) || nameFrom(names, ev.actor);
        var tgt = (m && m[2]) || nameFrom(names, d.target);
        if (d.hit) return d.crit ? 'Critical hit!' : null;
        if (!atk || !tgt) return null;
        return atk + ' misses ' + tgt + '.';
      }

      case 'damage': {
        m = /^(.+?) takes (\d+)/.exec(text);
        var victim = (m && m[1]) || nameFrom(names, d.target) || 'Someone';
        var amt = d.amount !== undefined ? d.amount : (m ? m[2] : null);
        if (amt === null || amt === undefined) return null;
        var src = nameFrom(names, ev.actor);
        var s = (src && src !== victim) ? src + ' hits ' + victim + ' for ' + amt
                                        : victim + ' takes ' + amt;
        return s + '.';
      }

      case 'heal':
        return stripDice(text) || null;                 // "X regains 8 HP from Healing Word"

      case 'save': {
        m = /^(.+?) (?:automatically fails the )?([A-Z]{3}) save/.exec(text);
        var sv = (m && m[1]) || nameFrom(names, d.target) || nameFrom(names, ev.actor);
        var ab = ABILITY[d.ability || (m && m[2])] || 'saving';
        if (!sv) return null;
        return sv + (d.success ? ' makes the ' : ' fails the ') + ab + ' save.';
      }

      case 'skill_check': {
        var headSk = stripDice(text.split(':')[0]).replace(/\s+vs DC \d+/, '');
        var parts = text.split(',');
        var tail = parts.length > 1 ? stripDice(parts[parts.length - 1]) : '';
        if (!tail || /\d/.test(tail)) tail = d.success === undefined ? '' : (d.success ? 'success' : 'failure');
        if (!headSk) return null;
        return headSk + (tail ? ': ' + tail : '') + '.';
      }

      case 'move': {
        m = /^(.+?) moves\b/.exec(text);
        if (!m) {
          // Not "<name> moves": something else moved (a flaming sphere being
          // rolled, a shove). The actor did not, so keep the engine's own
          // sentence rather than claiming the actor moved.
          var moved = stripDice(text.replace(/\s+to\s*\(\s*-?\d+\s*,\s*-?\d+\s*\)/, ''));
          return moved ? moved.replace(/[.!]?$/, '.') : null;
        }
        var ft = d.ft !== undefined ? d.ft : null;
        return m[1] + (ft !== null ? ' moves ' + ft + ' feet.' : ' moves.');
      }

      case 'spell_cast': {
        var castLine = stripDice(text.split(':')[0]);
        return castLine ? castLine.replace(/[.!]?$/, '.') : null;
      }

      case 'cost': {
        var usd = Number(d.total_usd);
        if (!isFinite(usd)) return null;
        return 'Total spend: ' + (usd < 1 ? Math.round(usd * 100) + ' cents.' : usd.toFixed(2) + ' dollars.');
      }

      default:
        return null;     // turn_end, roll, error, unknown
    }
  }

  // Which voice speaks the event: the DM narrates everything except a line of
  // dialogue, which belongs to its speaker — each PC its own voice, all
  // monsters/NPCs one shared voice.
  function voiceKeyFor(ev, party) {
    if (!ev) return 'dm';
    if (ev.kind !== 'dialogue') return 'dm';
    if (!ev.actor || ev.actor === 'dm') return 'dm';
    return isPC(ev.actor, party) ? ev.actor : 'npc';
  }

  // FNV-1a, 32-bit, so the same actor id lands on the same voice every load.
  function hashString(s) {
    var h = 0x811c9dc5;
    s = String(s || '');
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
  }

  function langPrefix(lang) {
    return String(lang || 'en').toLowerCase().replace('_', '-').split('-')[0];
  }

  function clampNum(v, lo, hi, dflt) {
    var x = Number(v);
    if (!isFinite(x)) return dflt;
    return x < lo ? lo : x > hi ? hi : x;
  }

  // Find a voice by its `name` in a voice list. Names are what an override can
  // store — a SpeechSynthesisVoice object cannot be serialised, and the list is
  // rebuilt on every page load.
  function findVoice(voices, name) {
    if (!name) return null;
    var list = voices || [];
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].name) === String(name)) return list[i];
    }
    return null;
  }

  // The voices of one language, sorted by name so a given browser always
  // produces the same order (and so the same actor keeps the same voice).
  function voicePool(voices, lang) {
    var pref = langPrefix(lang);
    var all = (voices || []).slice().sort(function (a, b) {
      var an = String(a.name || ''), bn = String(b.name || '');
      return an < bn ? -1 : an > bn ? 1 : 0;
    });
    var pool = all.filter(function (v) { return langPrefix(v.lang) === pref; });
    return pool.length ? pool : all;
  }

  // Pick {voice, pitch, rate} for a voice key from the browser's voice list
  // (any array of objects with name/lang/default — the real
  // SpeechSynthesisVoice objects in the page, plain objects in tests).
  //   dm  → the language's default voice (or the first of them)
  //   npc → a voice reserved out of the PC pool, at the monster timbre
  //   pc_* → hash into what is left; when there are too few voices to tell
  //   actors apart (iPad Safari often ships one or two), vary pitch and rate
  //   deterministically as well.
  // `overrides` is an optional {key: {voice: name, pitch, rate}} map — what the
  // voice lab saves. A field it does not set keeps the computed default, so a
  // row that only moves the pitch slider still follows the voice list.
  function voiceProfileFor(key, voices, lang, overrides) {
    var prof = defaultProfileFor(key, voices, lang);
    var ov = overrides && typeof overrides === 'object' ? overrides[key] : null;
    if (ov && typeof ov === 'object') {
      var v = findVoice(voices, ov.voice);
      if (v) prof.voice = v;
      if (ov.pitch !== undefined && ov.pitch !== null && ov.pitch !== '') {
        prof.pitch = clampNum(ov.pitch, PITCH_MIN, PITCH_MAX, prof.pitch);
      }
      if (ov.rate !== undefined && ov.rate !== null && ov.rate !== '') {
        prof.rate = clampNum(ov.rate, RATE_MIN, RATE_MAX, prof.rate);
      }
    }
    return prof;
  }

  function defaultProfileFor(key, voices, lang) {
    var pool = voicePool(voices, lang);
    if (!pool.length) {
      return { voice: null, pitch: key === 'npc' ? NPC_PITCH : 1,
               rate: key === 'npc' ? NPC_RATE : 1, key: key };
    }

    var dmIdx = 0;
    for (var i = 0; i < pool.length; i++) { if (pool[i]['default']) { dmIdx = i; break; } }
    if (key === 'dm') return { voice: pool[dmIdx], pitch: 1, rate: 1, key: key };

    var others = pool.filter(function (v, idx) { return idx !== dmIdx; });
    if (!others.length) others = pool;                        // one voice on the device
    var npcVoice = others[hashString('npc') % others.length];
    if (key === 'npc') return { voice: npcVoice, pitch: NPC_PITCH, rate: NPC_RATE, key: key };

    // PCs draw from what is left, so a party member never lands on the voice
    // the monsters are using unless the device has nothing else to give.
    var pcPool = others.filter(function (v) { return v !== npcVoice; });
    var few = pcPool.length < 4;
    if (!pcPool.length) { pcPool = others; few = true; }
    var h = hashString(key);
    var voice = pcPool[h % pcPool.length];
    // Always a small per-actor pitch offset (two PCs can hash to one voice);
    // with too few voices to go round, lean on pitch and rate much harder.
    var pitch = Math.round((1 + (((h >>> 8) % 5) - 2) * 0.05) * 100) / 100;   // 0.90 … 1.10
    var rate = 1;
    if (few) {
      pitch = Math.round((0.85 + ((h >>> 8) % 6) * 0.1) * 100) / 100;   // 0.85 … 1.35
      rate = Math.round((0.96 + ((h >>> 16) % 5) * 0.04) * 100) / 100;  // 0.96 … 1.12
    }
    return { voice: voice, pitch: pitch, rate: rate, key: key };
  }

  // Split a phrase into utterance-sized pieces at sentence boundaries.
  function chunksFor(phrase) {
    var s = String(phrase || '').trim();
    if (!s) return [];
    if (s.length <= MAX_CHUNK) return [s];
    var out = [], cur = '';
    var sentences = s.match(/[^.!?…]+[.!?…]*["”’)]?\s*/g) || [s];
    sentences.forEach(function (sent) {
      if (cur && (cur + sent).length > MAX_CHUNK) { out.push(cur.trim()); cur = ''; }
      if (sent.length > MAX_CHUNK) {          // one giant sentence: break at commas/spaces
        var words = sent.split(/\s+/);
        words.forEach(function (w) {
          if (cur && (cur + ' ' + w).length > MAX_CHUNK) { out.push(cur.trim()); cur = ''; }
          cur += (cur ? ' ' : '') + w;
        });
      } else {
        cur += sent;
      }
    });
    if (cur.trim()) out.push(cur.trim());
    return out;
  }

  var api = {
    shouldSpeak: shouldSpeak,
    phraseFor: phraseFor,
    voiceKeyFor: voiceKeyFor,
    voiceProfileFor: voiceProfileFor,
    defaultProfileFor: defaultProfileFor,
    voicePool: voicePool,
    findVoice: findVoice,
    chunksFor: chunksFor,
    isStory: isStory,
    isMechanic: isMechanic,
    hashString: hashString,
    stripDice: stripDice,
    STORY: STORY,
    MECH: MECH,
    NPC_PITCH: NPC_PITCH,
    NPC_RATE: NPC_RATE,
    PITCH_MIN: PITCH_MIN,
    PITCH_MAX: PITCH_MAX,
    RATE_MIN: RATE_MIN,
    RATE_MAX: RATE_MAX
  };

  root.DndSpeech = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
