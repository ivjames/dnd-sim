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

  // Who is speaking a `dialogue` event and what they say. The speaker rides in
  // data; splitting the text is the fallback for events stored before it did
  // (replayed history), and for those only. `names` fills in from the actor id
  // where neither carries a name.
  function dialogueLine(ev, names) {
    var d = (ev && ev.data) || {};
    var text = String((ev && ev.text) || '');
    var sp = d.speaker ? { who: String(d.speaker), said: text.trim() } : splitSpeaker(text);
    return { who: sp.who || nameFrom(names, ev && ev.actor), said: sp.said };
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

      case 'dialogue':
        // Only the words. The speaker's name is no longer glued to the front
        // of them: it was inside the spoken text, so it was billed, and it was
        // said in the speaker's own voice — a monster announcing itself
        // through its own distortion, with a colon mid-sentence that Polly
        // reads as a label rather than an attribution. The name is still
        // announced where it is needed, as its own clip in the narrator's
        // voice: see `attributionFor` for who gets one and why.
        return dialogueLine(ev, names).said || null;

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
  // dialogue, which belongs to its speaker — each PC its own voice, each
  // monster that can speak its own, every other NPC one shared voice.
  // `monsters` is the {id: true} map from speakingMonsters(); without it
  // nothing is a monster and the old shared NPC voice is what everyone gets.
  var MONSTER_PREFIX = 'monster:';

  function isMonsterKey(key) {
    return String(key || '').indexOf(MONSTER_PREFIX) === 0;
  }

  function voiceKeyFor(ev, party, monsters) {
    if (!ev) return 'dm';
    if (ev.kind !== 'dialogue') return 'dm';
    if (!ev.actor || ev.actor === 'dm') return 'dm';
    if (isPC(ev.actor, party)) return ev.actor;
    return (monsters && monsters[ev.actor]) ? MONSTER_PREFIX + ev.actor : 'npc';
  }

  // The speaker's name, said aloud before their line, or null for "announce
  // nobody". It is the narrator's job — `segmentsFor` gives it the `dm` voice
  // and its own clip — so what a monster's own distorted voice says is only
  // ever the words it speaks.
  //
  // Who gets announced is unchanged: a PC never does, because their voice is
  // the attribution and always has been. Everyone else does, because a name
  // heard once is what lets the voice afterwards be placed — an NPC shares the
  // one `npc` voice with every other NPC, and a monster's timbre is a costume
  // rather than a name.
  function attributionFor(ev, names, party) {
    if (!ev || ev.kind !== 'dialogue') return null;
    var line = dialogueLine(ev, names);
    if (!line.said || !line.who) return null;
    if (isPC(ev.actor, party)) return null;
    // Its own sentence, so the narrator lands on it rather than running into
    // the line: a bare name is read as a fragment by both engines.
    return /[.!?\u2026]$/.test(line.who) ? line.who : line.who + '.';
  }

  // The voiced parts of one event, in order: `[{key, text}]`, each part in one
  // voice. Everything except an attributed line of dialogue is a single part,
  // exactly as before. This is the whole of "what is spoken and by whom" — the
  // page walks the list and asks for one clip per part.
  function segmentsFor(ev, names, party, monsters) {
    var phrase = phraseFor(ev, names, party);
    if (!phrase) return [];
    var out = [];
    var who = attributionFor(ev, names, party);
    if (who) out.push({ key: 'dm', text: who });
    out.push({ key: voiceKeyFor(ev, party, monsters), text: phrase });
    return out;
  }

  // Whether a stat block's `languages` string names something the creature can
  // say out loud. SRD writes it as prose — "Common, Goblin", "—", "any one
  // language (usually Common)", "understands all it knew in life but can't
  // speak" — so this reads the two ways it says no: nothing at all, and
  // understanding without speech. A parenthetical is dropped first, which
  // leaves "— (telepathy 60 ft.)" as the dash it is.
  var NO_LANGUAGE = { '': 1, '-': 1, '\u2014': 1, '\u2013': 1, 'none': 1, 'n/a': 1 };
  var CANNOT_SPEAK = /\b(?:can'?t|cannot|does\s?n'?t|do\s?n'?t|unable to)\s+speak\b/;

  function canSpeakLanguages(languages) {
    var s = String(languages === null || languages === undefined ? '' : languages)
      .toLowerCase()
      .replace(/[\u2018\u2019]/g, "'")
      .replace(/\([^()]*\)/g, ' ')
      .replace(/\s{2,}/g, ' ')
      .trim();
    if (NO_LANGUAGE[s]) return false;
    return !CANNOT_SPEAK.test(s);
  }

  // {id: true} for every combatant that is a monster and can speak, built from
  // the snapshot's combatants (a map of id → combatant, or an array of them).
  // "Monster" is the engine's own word: `kind === 'monster'`, which is every
  // stat-blocked creature at the table whichever side it is on. A monster with
  // no stat block reaching the page is left out rather than guessed at.
  function speakingMonsters(combatants) {
    var out = {};
    if (!combatants || typeof combatants !== 'object') return out;
    Object.keys(combatants).forEach(function (k) {     // ids, or array indices
      var c = combatants[k];
      if (!c || typeof c !== 'object' || c.kind !== 'monster') return;
      var id = c.id || (Array.isArray(combatants) ? null : k);
      if (id && canSpeakLanguages((c.stat_block || {}).languages)) out[String(id)] = true;
    });
    return out;
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

  // Pick {voice, pitch, rate} for a voice key from the browser's voice list
  // (any array of objects with name/lang/default — the real
  // SpeechSynthesisVoice objects in the page, plain objects in tests).
  //   dm  → the language's default voice (or the first of them)
  //   pc_*/npc → hash into the remaining same-language voices; when there are
  //   too few to tell actors apart (iPad Safari often ships one or two), vary
  //   pitch and rate deterministically as well.
  //   monster:<id> → hash into the same-language novelty voices instead,
  //   falling back to the npc voice on a device that has none.
  // Apple ships novelty voices in the same list as the real ones — Bubbles
  // gurgles, Whisper has no voicing, Zarvox is a robot. None of them can carry
  // narration, so they are never cast for the DM, a PC or an ordinary NPC.
  // A monster that can speak is the one seat where that is the point: the
  // goblin *should* sound wrong. The name may arrive with a language in
  // parentheses ("Bubbles (English (US))"), so match the head of it.
  var NOVELTY = {
    'albert': 1, 'bad news': 1, 'bahh': 1, 'bells': 1, 'boing': 1, 'bubbles': 1,
    'cellos': 1, 'deranged': 1, 'good news': 1, 'hysterical': 1, 'jester': 1,
    'organ': 1, 'pipe organ': 1, 'superstar': 1, 'trinoids': 1, 'whisper': 1,
    'wobble': 1, 'zarvox': 1
  };

  function isNoveltyVoice(v) {
    var n = String((v && v.name) || '').toLowerCase().trim();
    n = n.replace(/\s*\(.*$/, '').trim();
    return !!NOVELTY[n];
  }

  // Deal one voice out of `pool` for a key, deterministically.
  // Always a small per-actor pitch offset (two actors can hash to one voice);
  // with too few voices to go round, lean on pitch and rate much harder.
  function castFrom(key, pool, few) {
    var h = hashString(key);
    var pitch = Math.round((1 + (((h >>> 8) % 5) - 2) * 0.05) * 100) / 100;   // 0.90 … 1.10
    var rate = 1;
    if (few) {
      pitch = Math.round((0.75 + ((h >>> 8) % 6) * 0.1) * 100) / 100;   // 0.75 … 1.25
      rate = Math.round((0.92 + ((h >>> 16) % 5) * 0.04) * 100) / 100;  // 0.92 … 1.08
    }
    return { voice: pool[h % pool.length], pitch: pitch, rate: rate, key: key };
  }

  function sameLang(list, pref) {
    return list.filter(function (v) { return langPrefix(v.lang) === pref; });
  }

  function voiceProfileFor(key, voices, lang) {
    var pref = langPrefix(lang);
    var all = (voices || []).slice().sort(function (a, b) {
      var an = String(a.name || ''), bn = String(b.name || '');
      return an < bn ? -1 : an > bn ? 1 : 0;
    });
    // Drop the novelty voices before anyone is cast — including the DM, which
    // takes pool[0] when no voice is flagged default, and alphabetically that
    // is "Albert" on a Mac. Discard them across the WHOLE list first, then
    // narrow by language: a real voice in the wrong language still reads the
    // line, where Bubbles in the right one does not. Only a device with no
    // real voice anywhere falls back to them, rather than going silent.
    var real = all.filter(function (v) { return !isNoveltyVoice(v); });
    var base = real.length ? real : all;
    var pool = sameLang(base, pref);
    if (!pool.length) pool = base;
    if (!pool.length) return { voice: null, pitch: 1, rate: 1, key: key };

    var dmIdx = 0;
    for (var i = 0; i < pool.length; i++) { if (pool[i]['default']) { dmIdx = i; break; } }
    if (key === 'dm') return { voice: pool[dmIdx], pitch: 1, rate: 1, key: key };

    // A monster that can speak is cast out of the novelty voices instead —
    // one each, so the ogre and the goblin are told apart by voice. Only
    // same-language ones: the reason a real voice beats Bubbles for narration
    // is that it can be understood, and an English Zarvox reading French is
    // the same failure. A device with no novelty voice to give (Windows,
    // Android, most of Linux) leaves the monster on the shared NPC voice,
    // exactly as before.
    var castKey = key;
    if (isMonsterKey(key)) {
      var gimmicks = sameLang(all.filter(isNoveltyVoice), pref);
      // Always the wide pitch/rate spread here, however many novelty voices
      // the device has: there are rarely more than a handful, two monsters
      // hashing to one of them is common, and a monster has no natural pitch
      // to distort — where a PC would sound processed, the ogre just sounds
      // bigger than the goblin.
      if (gimmicks.length) return castFrom(key, gimmicks, true);
      castKey = 'npc';
    }

    var others = pool.filter(function (v, idx) { return idx !== dmIdx; });
    var few = others.length < 4;          // not enough distinct voices for a party
    if (!others.length) { others = pool; few = true; }
    var prof = castFrom(castKey, others, few);
    prof.key = key;
    return prof;
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
    attributionFor: attributionFor,
    segmentsFor: segmentsFor,
    voiceKeyFor: voiceKeyFor,
    voiceProfileFor: voiceProfileFor,
    canSpeakLanguages: canSpeakLanguages,
    speakingMonsters: speakingMonsters,
    isMonsterKey: isMonsterKey,
    chunksFor: chunksFor,
    isStory: isStory,
    isMechanic: isMechanic,
    hashString: hashString,
    isNoveltyVoice: isNoveltyVoice,
    NOVELTY: NOVELTY,
    stripDice: stripDice,
    STORY: STORY,
    MECH: MECH
  };

  root.DndSpeech = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
