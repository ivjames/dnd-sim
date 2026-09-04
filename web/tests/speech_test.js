/* Node tests for web/static/speech.js — the pure half of the spoken narration.
   No dependencies and no DOM: `node web/tests/speech_test.js` runs them, and
   test_speech_js.py runs that from pytest (skipping when node is absent). */
'use strict';

var path = require('path');
var S = require(path.join(__dirname, '..', 'static', 'speech.js'));

var failures = 0, count = 0;
function test(name, fn) {
  count++;
  try { fn(); }
  catch (e) { failures++; console.error('FAIL ' + name + '\n      ' + e.message); }
}
function eq(actual, expected, what) {
  if (actual !== expected) {
    throw new Error((what || 'value') + ': expected ' + JSON.stringify(expected) +
                    ', got ' + JSON.stringify(actual));
  }
}
function ok(cond, what) { if (!cond) throw new Error(what || 'expected truthy'); }

function voices(names, defaultName, lang) {
  return names.map(function (n) {
    return { name: n, lang: lang || 'en-US', 'default': n === defaultName };
  });
}
var MANY = voices(['Alex', 'Daniel', 'Fiona', 'Karen', 'Moira', 'Samantha', 'Tessa'], 'Samantha');
var ONE = voices(['Samantha'], 'Samantha');

// ---- who gets which voice ----------------------------------------------

test('dm takes the language default voice', function () {
  eq(S.voiceProfileFor('dm', MANY, 'en-US').voice.name, 'Samantha', 'dm voice');
});

test('monsters do not share the DM voice, nor a PC voice', function () {
  var npc = S.voiceProfileFor('npc', MANY, 'en-US');
  ok(npc.voice.name !== 'Samantha', 'npc must not be the DM voice');
  ['pc_thorin', 'pc_vessa', 'pc_ilda', 'pc_bram', 'pc_rook'].forEach(function (id) {
    var pc = S.voiceProfileFor(id, MANY, 'en-US');
    ok(pc.voice.name !== npc.voice.name, id + ' landed on the monster voice');
  });
});

test('monsters get their own timbre, below every default PC pitch', function () {
  var npc = S.voiceProfileFor('npc', MANY, 'en-US');
  eq(npc.pitch, S.NPC_PITCH, 'npc pitch');
  eq(npc.rate, S.NPC_RATE, 'npc rate');
  ['pc_a', 'pc_b', 'pc_c', 'pc_d', 'pc_e', 'pc_f'].forEach(function (id) {
    ok(S.voiceProfileFor(id, MANY, 'en-US').pitch > npc.pitch, id + ' is not above the monster pitch');
  });
});

test('one-voice device still separates monsters from people by pitch', function () {
  var npc = S.voiceProfileFor('npc', ONE, 'en-US');
  eq(npc.voice.name, 'Samantha', 'the only voice');
  ['pc_a', 'pc_b', 'pc_c', 'pc_d', 'pc_e', 'pc_f'].forEach(function (id) {
    var pc = S.voiceProfileFor(id, ONE, 'en-US');
    ok(pc.pitch - npc.pitch >= 0.1, id + ' pitch is too close to the monster pitch');
  });
});

test('the pick is stable across calls and list order', function () {
  var a = S.voiceProfileFor('pc_thorin', MANY, 'en-US');
  var shuffled = MANY.slice().reverse();
  var b = S.voiceProfileFor('pc_thorin', shuffled, 'en-US');
  eq(b.voice.name, a.voice.name, 'same actor, same voice');
  eq(b.pitch, a.pitch, 'same actor, same pitch');
});

test('no voices at all yields a usable profile', function () {
  var p = S.voiceProfileFor('npc', [], 'en-US');
  eq(p.voice, null, 'voice');
  eq(p.pitch, S.NPC_PITCH, 'pitch');
});

// ---- overrides (what the voice lab saves) -------------------------------

test('an override replaces voice, pitch and rate', function () {
  var p = S.voiceProfileFor('npc', MANY, 'en-US', { npc: { voice: 'Karen', pitch: 1.4, rate: 1.1 } });
  eq(p.voice.name, 'Karen', 'voice');
  eq(p.pitch, 1.4, 'pitch');
  eq(p.rate, 1.1, 'rate');
});

test('a partial override keeps the computed rest', function () {
  var auto = S.voiceProfileFor('npc', MANY, 'en-US');
  var p = S.voiceProfileFor('npc', MANY, 'en-US', { npc: { pitch: 1.05 } });
  eq(p.voice.name, auto.voice.name, 'voice still automatic');
  eq(p.pitch, 1.05, 'pitch');
  eq(p.rate, auto.rate, 'rate still automatic');
});

test('overrides for other roles are ignored', function () {
  var auto = S.voiceProfileFor('dm', MANY, 'en-US');
  var p = S.voiceProfileFor('dm', MANY, 'en-US', { npc: { voice: 'Karen' } });
  eq(p.voice.name, auto.voice.name, 'dm untouched');
});

test('a voice name the device no longer has falls back to the automatic pick', function () {
  var auto = S.voiceProfileFor('pc_thorin', MANY, 'en-US');
  var p = S.voiceProfileFor('pc_thorin', MANY, 'en-US', { pc_thorin: { voice: 'Ghost of iOS 12' } });
  eq(p.voice.name, auto.voice.name, 'voice');
});

test('out-of-range slider values are clamped, junk is ignored', function () {
  var hi = S.voiceProfileFor('dm', MANY, 'en-US', { dm: { pitch: 99, rate: -4 } });
  eq(hi.pitch, S.PITCH_MAX, 'pitch clamped high');
  eq(hi.rate, S.RATE_MIN, 'rate clamped low');
  var junk = S.voiceProfileFor('dm', MANY, 'en-US', { dm: { pitch: 'loud', rate: null } });
  eq(junk.pitch, 1, 'junk pitch ignored');
  eq(junk.rate, 1, 'null rate ignored');
});

test('voiceProfileFor without an overrides argument still works', function () {
  eq(S.voiceProfileFor('dm', MANY, 'en-US').pitch, 1, 'pitch');
});

// ---- routing and wording (unchanged behaviour, kept honest) -------------

test('dialogue is routed to its speaker, everything else to the DM', function () {
  var party = { pc_thorin: true };
  eq(S.voiceKeyFor({ kind: 'dialogue', actor: 'pc_thorin' }, party), 'pc_thorin', 'pc');
  eq(S.voiceKeyFor({ kind: 'dialogue', actor: 'goblin_2' }, party), 'npc', 'monster');
  eq(S.voiceKeyFor({ kind: 'narration', actor: 'dm' }, party), 'dm', 'narration');
});

test('mechanics are spoken unless muted; story always', function () {
  var ev = { kind: 'damage' };
  eq(S.shouldSpeak(ev, { enabled: true }), true, 'mechanics on');
  eq(S.shouldSpeak(ev, { enabled: true, muteMechanics: true }), false, 'mechanics muted');
  eq(S.shouldSpeak({ kind: 'narration' }, { enabled: true, muteMechanics: true }), true, 'story');
  eq(S.shouldSpeak(ev, { enabled: false }), false, 'voice off');
});

test('a monster line keeps its name, a PC line does not', function () {
  var party = { pc_thorin: true };
  eq(S.phraseFor({ kind: 'dialogue', actor: 'goblin_2', text: 'Goblin 2: You die here.' }, {}, party),
     'Goblin 2: You die here.', 'monster');
  eq(S.phraseFor({ kind: 'dialogue', actor: 'pc_thorin', text: 'Thorin: Not today.' }, {}, party),
     'Not today.', 'pc');
});

if (failures) {
  console.error(failures + ' of ' + count + ' speech.js tests failed');
  process.exit(1);
}
console.log(count + ' speech.js tests passed');
