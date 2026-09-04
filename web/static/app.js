/* dnd-sim spectator UI. Vanilla ES2018+, no build step, no dependencies.
   Targets desktop Safari/Chrome/Firefox and iPad Safari 14+. */
(function () {
  'use strict';

  var EVENT_KINDS = [
    'combat_start', 'round_start', 'turn_start', 'turn_end', 'roll', 'attack', 'damage',
    'heal', 'save', 'condition_add', 'condition_remove', 'move', 'spell_cast',
    'concentration_broken', 'death_save', 'down', 'dead', 'stable', 'combat_end',
    'narration', 'dialogue', 'dm_note', 'scene', 'skill_check', 'system', 'cost', 'error'
  ];
  var PROMINENT = { narration: 1, dialogue: 1, dm_note: 1, scene: 1 };
  var SNAPSHOT_TRIGGERS = {
    turn_start: 1, move: 1, combat_start: 1, combat_end: 1, damage: 1, heal: 1,
    condition_add: 1, condition_remove: 1, down: 1, dead: 1, stable: 1, spell_cast: 1,
    round_start: 1
  };
  var TERMINAL = { finished: 1, stopped: 1, error: 1, budget_exceeded: 1 };

  var S = {
    gameId: null,
    es: null,
    lastSeq: -1,
    seen: {},
    snapshot: null,
    status: 'idle',
    budget: 1.0,
    cost: 0,
    tokens: 0,
    activeId: null,
    group: null,        // current turn group DOM node
    groupBody: null,
    snapTimer: null,
    pollTimer: null,
    presets: []
  };

  // ---------- tiny helpers ----------
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function num(v, d) { var x = Number(v); return isFinite(x) ? x : (d || 0); }

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: {}, credentials: 'same-origin' };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = { error: t }; }
        if (!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
        return data;
      });
    });
  }

  // ---------- transcript ----------
  function transcriptEl() { return $('transcript'); }

  function resetTranscript() {
    var t = transcriptEl();
    clear(t);
    S.group = null; S.groupBody = null;
    S.seen = {}; S.lastSeq = -1;
  }

  function atBottom(node) {
    return node.scrollTop + node.clientHeight >= node.scrollHeight - 60;
  }

  function appendNode(node) {
    var t = transcriptEl();
    var empty = t.querySelector('.empty');
    if (empty) t.removeChild(empty);
    var follow = $('toggle-follow').checked;
    var wasBottom = atBottom(t);
    t.appendChild(node);
    if (follow && wasBottom) t.scrollTop = t.scrollHeight;
  }

  function startTurnGroup(ev) {
    var name = (ev.data && (ev.data.name || ev.data.actor_name)) || nameOf(ev.actor) || ev.actor || '?';
    var g = el('div', 'turn-group');
    var head = el('div', 'turn-head');
    head.appendChild(document.createTextNode('round ' + num(ev.round) + ' — '));
    head.appendChild(el('span', 'who', name));
    g.appendChild(head);
    var det = document.createElement('details');
    det.className = 'turn-details';
    det.open = true;
    var sum = document.createElement('summary');
    sum.textContent = 'mechanics';
    det.appendChild(sum);
    var body = el('div', 'turn-body');
    det.appendChild(body);
    g.appendChild(det);
    S.group = g; S.groupBody = body;
    appendNode(g);
    return head;
  }

  function mechLine(ev) {
    var line = el('div', 'mech k-' + ev.kind);
    line.appendChild(el('span', 'seq', String(ev.seq)));
    line.appendChild(document.createTextNode(ev.text || ev.kind));
    return line;
  }

  function renderEvent(ev) {
    if (!ev || S.seen[ev.seq]) return;
    S.seen[ev.seq] = 1;
    if (ev.seq > S.lastSeq) S.lastSeq = ev.seq;

    if (ev.kind === 'cost') {
      var d = ev.data || {};
      if (d.total_usd !== undefined) setCost(num(d.total_usd), null);
    }
    var node = null;   // the transcript node for this event (voice highlights it)
    if (ev.kind === 'turn_start') {
      S.activeId = ev.actor;
      node = startTurnGroup(ev);
    } else if (ev.kind === 'turn_end') {
      S.group = null; S.groupBody = null;
    } else if (ev.kind === 'narration') {
      node = el('p', 'narration', ev.text);
      appendNode(node);
    } else if (ev.kind === 'dialogue') {
      node = el('p', 'dialogue');
      var who = (ev.data && ev.data.speaker) || nameOf(ev.actor) || ev.actor;
      if (who) node.appendChild(el('span', 'speaker', who + ':'));
      node.appendChild(document.createTextNode(ev.text || ''));
      appendNode(node);
    } else if (ev.kind === 'dm_note') {
      node = el('p', 'dmnote', 'DM note from the table: ' + (ev.text || ''));
      appendNode(node);
    } else if (ev.kind === 'scene') {
      node = el('div', 'scene');
      var title = (ev.data && (ev.data.title || ev.data.location)) || 'Scene';
      node.appendChild(el('span', 'scene-title', title));
      node.appendChild(document.createTextNode(ev.text || ''));
      appendNode(node);
    } else if (ev.kind === 'combat_start' || ev.kind === 'combat_end' || ev.kind === 'round_start') {
      S.group = null; S.groupBody = null;
      node = el('div', 'end-banner', ev.text || ev.kind.replace('_', ' '));
      appendNode(node);
    } else {
      node = mechLine(ev);
      if (S.groupBody) {
        var t = transcriptEl();
        var follow = $('toggle-follow').checked, wasBottom = atBottom(t);
        S.groupBody.appendChild(node);
        if (follow && wasBottom) t.scrollTop = t.scrollHeight;
      } else {
        appendNode(node);
      }
    }
    if (node) node.setAttribute('data-seq', String(ev.seq));
    voiceOnEvent(ev);
    if (SNAPSHOT_TRIGGERS[ev.kind]) scheduleSnapshot();
  }

  function nameOf(id) {
    if (!id || !S.snapshot) return null;
    var c = combatants()[id];
    return c ? c.name : null;
  }

  // ---------- snapshot rendering ----------
  function gameState() {
    var snap = S.snapshot || {};
    var st = snap.state || snap;
    return st && typeof st === 'object' ? st : {};
  }
  function combatants() {
    var c = gameState().combatants;
    return c && typeof c === 'object' ? c : {};
  }

  function scheduleSnapshot() {
    if (S.snapTimer) return;
    S.snapTimer = setTimeout(function () {
      S.snapTimer = null;
      refreshSnapshot();
    }, 700);
  }

  function refreshSnapshot() {
    if (!S.gameId) return Promise.resolve();
    return api('/api/games/' + encodeURIComponent(S.gameId)).then(function (g) {
      S.snapshot = g.snapshot || null;
      S.status = g.status || S.status;
      var cfg = g.config || {};
      if (cfg.budget_usd) S.budget = num(cfg.budget_usd, S.budget);
      setCost(num(g.cost_usd, S.cost), g.ledger);
      renderAll(g);
    }).catch(function (e) { console.warn('snapshot', e); });
  }

  function renderAll(g) {
    setStatus(S.status, g && g.round);
    renderInitiative();
    renderParty();
    drawGrid();
    updateControls();
  }

  function setStatus(status, round) {
    var pill = $('status-pill');
    pill.textContent = status || 'idle';
    pill.className = 'pill pill-' + (status || 'idle');
    var st = gameState();
    var r = round || st.round || 0;
    $('round-pill').textContent = r ? ('round ' + r) : 'round —';
  }

  function setCost(usd, ledger) {
    S.cost = num(usd, S.cost);
    if (ledger && ledger.by_role) {
      var tok = 0;
      for (var k in ledger.by_role) {
        if (!Object.prototype.hasOwnProperty.call(ledger.by_role, k)) continue;
        var r = ledger.by_role[k] || {};
        tok += num(r['in']) + num(r.out);
      }
      S.tokens = tok;
    }
    var meter = $('cost-meter');
    meter.querySelector('.cost-usd').textContent = '$' + S.cost.toFixed(4);
    meter.querySelector('.cost-tokens').textContent = fmtTokens(S.tokens);
    var pct = S.budget > 0 ? Math.min(100, (S.cost / S.budget) * 100) : 0;
    var bar = meter.querySelector('.cost-bar');
    bar.firstElementChild.style.width = pct.toFixed(1) + '%';
    if (pct >= 100) bar.classList.add('over'); else bar.classList.remove('over');
    meter.title = '$' + S.cost.toFixed(4) + ' of $' + S.budget.toFixed(2) + ' budget';
  }

  function fmtTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M tok';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k tok';
    return n + ' tok';
  }

  function renderInitiative() {
    var list = $('initiative');
    clear(list);
    var st = gameState();
    var init = st.initiative || [];
    var cs = combatants();
    var activeId = st.active_id || S.activeId;
    if (typeof st.turn_index === 'number' && init.length) {
      var cur = init[st.turn_index % init.length];
      if (cur) activeId = Array.isArray(cur) ? cur[0] : (cur.id || activeId);
    }
    if (!init.length) {
      list.appendChild(el('li', 'empty', 'No combat.'));
      return;
    }
    init.forEach(function (row) {
      var id = Array.isArray(row) ? row[0] : (row && row.id);
      var score = Array.isArray(row) ? row[1] : (row && row.score);
      var c = cs[id] || {};
      var li = el('li');
      if (id === activeId) li.className = 'active';
      if (c.dead || (c.hp !== undefined && num(c.hp) <= 0)) li.className += ' down';
      li.appendChild(el('span', 'side-' + (c.side || 'neutral'), c.name || id || '?'));
      li.appendChild(el('span', 'score', score === undefined ? '' : String(score)));
      list.appendChild(li);
    });
  }

  function hpClass(frac) { return frac > 0.5 ? '' : (frac > 0.25 ? 'hurt' : 'bad'); }

  function card(c, id, compact) {
    var st = gameState();
    var node = el('div', 'card');
    if (id === (st.active_id || S.activeId)) node.className += ' is-active';
    var hp = num(c.hp), max = Math.max(1, num(c.max_hp, 1));
    if (c.dead || hp <= 0) node.className += ' is-down';

    var top = el('div', 'card-top');
    top.appendChild(el('span', 'card-name', c.name || id));
    var sub = [];
    if (c.ac !== undefined) sub.push('AC ' + num(c.ac));
    if (c.sheet && c.sheet.klass) sub.push(c.sheet.klass + ' ' + num(c.sheet.level));
    top.appendChild(el('span', 'card-sub', sub.join(' · ')));
    node.appendChild(top);

    var frac = Math.max(0, Math.min(1, hp / max));
    var bar = el('div', 'hpbar ' + hpClass(frac));
    var fill = el('i'); fill.style.width = (frac * 100).toFixed(0) + '%';
    bar.appendChild(fill);
    node.appendChild(bar);

    var hpText = hp + '/' + num(c.max_hp);
    if (num(c.temp_hp) > 0) hpText += ' (+' + num(c.temp_hp) + ' temp)';
    if (hp <= 0 && !c.dead) {
      var ds = c.death_saves || {};
      hpText += ' — down ' + num(ds.success) + '✓/' + num(ds.failure) + '✗';
    }
    if (c.dead) hpText += ' — dead';
    node.appendChild(el('div', 'hpnum', hpText));

    var tags = el('div', 'tags');
    (c.conditions || []).forEach(function (cond) {
      var nm = typeof cond === 'string' ? cond : (cond && cond.name);
      if (!nm) return;
      var t = el('span', 'tag', nm);
      if (cond && cond.duration) t.textContent = nm + ' ' + cond.duration;
      tags.appendChild(t);
    });
    if (c.concentration && c.concentration.spell) {
      tags.appendChild(el('span', 'tag conc', 'conc: ' + c.concentration.spell));
    }
    if (!compact) {
      var slots = (c.resources && c.resources.spell_slots) || (c.sheet && c.sheet.spell_slots);
      if (slots) {
        Object.keys(slots).sort().forEach(function (lvl) {
          var n = num(slots[lvl]);
          tags.appendChild(el('span', 'tag slot', 'lv' + lvl + ': ' + n));
        });
      }
      var res = c.resources || {};
      ['second_wind', 'action_surge', 'channel_divinity'].forEach(function (k) {
        if (res[k] !== undefined && num(res[k]) > 0) {
          tags.appendChild(el('span', 'tag slot', k.replace(/_/g, ' ')));
        }
      });
    }
    if (tags.childNodes.length) node.appendChild(tags);
    return node;
  }

  function renderParty() {
    var partyBox = $('party'), enemyBox = $('enemies');
    clear(partyBox); clear(enemyBox);
    var cs = combatants();
    var ids = Object.keys(cs);
    var nParty = 0, nEnemy = 0;
    ids.forEach(function (id) {
      var c = cs[id] || {};
      if (c.side === 'party') { partyBox.appendChild(card(c, id, false)); nParty++; }
      else { enemyBox.appendChild(card(c, id, true)); nEnemy++; }
    });
    if (!nParty) partyBox.appendChild(el('p', 'empty', S.gameId ? 'Party not built yet.' : 'No game loaded.'));
    if (!nEnemy) enemyBox.appendChild(el('p', 'empty', '—'));
  }

  // ---------- grid canvas ----------
  function coordSet(list) {
    var set = {};
    (list || []).forEach(function (p) {
      if (Array.isArray(p) && p.length >= 2) set[p[0] + ',' + p[1]] = 1;
      else if (typeof p === 'string') set[p.replace(/[()\s]/g, '')] = 1;
    });
    return set;
  }

  function drawGrid() {
    var canvas = $('grid');
    var st = gameState();
    var grid = st.grid || {};
    var w = Math.max(1, num(grid.width, 12)), h = Math.max(1, num(grid.height, 10));

    var cssW = canvas.clientWidth || 480;
    var cell = Math.max(14, Math.floor(cssW / w));
    var cssH = cell * h;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(cell * w * dpr);
    canvas.height = Math.floor(cssH * dpr);
    canvas.style.height = cssH + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cell * w, cssH);

    ctx.fillStyle = '#100e0c';
    ctx.fillRect(0, 0, cell * w, cssH);

    var difficult = coordSet(grid.difficult);
    var walls = coordSet(grid.walls);
    var cover = grid.cover || {};

    var x, y, key;
    for (y = 0; y < h; y++) {
      for (x = 0; x < w; x++) {
        key = x + ',' + y;
        if (walls[key]) ctx.fillStyle = '#4a4038';
        else if (difficult[key]) ctx.fillStyle = '#3d3323';
        else continue;
        ctx.fillRect(x * cell, y * cell, cell, cell);
      }
    }
    Object.keys(cover).forEach(function (k) {
      var parts = k.replace(/[()\s]/g, '').split(',');
      var cx = num(parts[0]), cy = num(parts[1]);
      ctx.strokeStyle = 'rgba(226,163,63,.5)';
      ctx.lineWidth = 2;
      ctx.strokeRect(cx * cell + 2, cy * cell + 2, cell - 4, cell - 4);
    });

    ctx.strokeStyle = '#262019';
    ctx.lineWidth = 1;
    for (x = 0; x <= w; x++) {
      ctx.beginPath(); ctx.moveTo(x * cell + .5, 0); ctx.lineTo(x * cell + .5, cssH); ctx.stroke();
    }
    for (y = 0; y <= h; y++) {
      ctx.beginPath(); ctx.moveTo(0, y * cell + .5); ctx.lineTo(cell * w, y * cell + .5); ctx.stroke();
    }

    var cs = combatants();
    var activeId = st.active_id || S.activeId;
    Object.keys(cs).forEach(function (id) {
      var c = cs[id] || {};
      var pos = c.position;
      if (!pos || pos.length < 2) return;
      var px = num(pos[0]) * cell + cell / 2, py = num(pos[1]) * cell + cell / 2;
      var r = Math.max(5, cell * 0.36);
      var dead = c.dead || num(c.hp) <= 0;
      var color = c.side === 'party' ? '#6fa8c9' : (c.side === 'enemy' ? '#c9705f' : '#b39a6a');
      ctx.globalAlpha = dead ? 0.35 : 1;
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      if (id === activeId) {
        ctx.strokeStyle = '#e2a33f'; ctx.lineWidth = 2.5; ctx.stroke();
      }
      ctx.globalAlpha = 1;
      var label = (c.name || id).replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase();
      ctx.fillStyle = '#14110e';
      ctx.font = 'bold ' + Math.max(8, Math.floor(r)) + 'px ui-monospace, Menlo, monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(label, px, py + 0.5);
      if (dead) {
        ctx.strokeStyle = '#b4412f'; ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(px - r, py - r); ctx.lineTo(px + r, py + r);
        ctx.moveTo(px + r, py - r); ctx.lineTo(px - r, py + r);
        ctx.stroke();
      }
    });
  }


  // ---------- voice (Web Speech API) ----------
  // The selection + wording live in speech.js (pure, node-testable); this is
  // the queue and the speechSynthesis glue. Designed around iPad Safari:
  // speech only starts inside a user gesture, the voice list arrives late (or
  // never fires voiceschanged), onend can go missing, and speak() right after
  // cancel() is dropped — each of which has a line below.
  var Speech = window.DndSpeech || null;
  var VOICE_RATES = { slow: 0.85, normal: 1.0, fast: 1.2 };
  var VOICE_MAX_QUEUE = 20;
  var V = {
    supported: false,
    synth: null,
    settings: { enabled: false, rate: 'normal', muteMechanics: false },
    unlocked: false,       // a user gesture has started speech on this page
    sinceSeq: Infinity,    // speak only events with seq > this
    caughtUp: false,       // history replay rendered; until then sinceSeq stays Infinity
    queue: [],             // [{ ev, phrase, key }] in seq order
    current: null,         // { item, chunks, idx, node, timer, utt }
    voices: [],
    profiles: {},
    heldUtt: null          // keeps the unlock utterance referenced until it ends
  };

  // Hidden pages stay disarmed: the SSE stream keeps flowing in the background,
  // so a cancel on hide is not enough — the next event would speak again.
  function pageHidden() { return typeof document !== 'undefined' && !!document.hidden; }
  function voiceArmed() { return V.supported && V.settings.enabled && V.unlocked && !pageHidden(); }

  function voiceLoadSettings() {
    try {
      var raw = localStorage.getItem('dndsim.voice');
      var o = raw ? JSON.parse(raw) : null;
      if (o && typeof o === 'object') {
        V.settings.enabled = !!o.enabled;
        V.settings.rate = VOICE_RATES[o.rate] ? o.rate : 'normal';
        V.settings.muteMechanics = !!o.muteMechanics;
      }
    } catch (e) { /* private mode or junk */ }
  }
  function voiceSaveSettings() {
    try { localStorage.setItem('dndsim.voice', JSON.stringify(V.settings)); } catch (e) { /* ignore */ }
  }

  function voiceRefreshVoices() {
    var list = [];
    try { list = V.synth.getVoices() || []; } catch (e) { list = []; }
    V.voices = list;
    V.profiles = {};
  }
  function voiceProfile(key) {
    if (!V.voices.length) voiceRefreshVoices();    // Safari: no voiceschanged, voices just appear
    if (!V.profiles[key]) {
      var lang = document.documentElement.lang || navigator.language || 'en';
      V.profiles[key] = Speech.voiceProfileFor(key, V.voices, lang);
    }
    return V.profiles[key];
  }

  // {id: true} for every party member: the snapshot's combatants with side
  // 'party', plus the config's party ids when the snapshot carries them (before
  // combat the combatant list can be empty).
  function voiceParty() {
    var cs = combatants(), out = {};
    Object.keys(cs).forEach(function (id) { if (cs[id] && cs[id].side === 'party') out[id] = true; });
    var g = S.game || {}, cfg = g.config || {};
    if (cfg && Array.isArray(cfg.party)) cfg.party.forEach(function (m) { if (m && m.id) out[m.id] = true; });
    return out;
  }

  // {id: true} for every monster in the snapshot whose stat block names a
  // language it can speak — the seats a novelty voice may be cast to.
  function voiceMonsters() {
    return Speech.speakingMonsters(combatants());
  }

  function voiceNames() {
    var cs = combatants(), out = {};
    Object.keys(cs).forEach(function (id) { if (cs[id] && cs[id].name) out[id] = cs[id].name; });
    return out;
  }

  function voiceOnEvent(ev) {
    if (!voiceArmed() || !ev || !(ev.seq > V.sinceSeq)) return;
    if (!Speech.shouldSpeak(ev, V.settings)) return;
    var party = voiceParty();
    var phrase = Speech.phraseFor(ev, voiceNames(), party);
    if (!phrase) return;
    V.queue.push({ ev: ev, phrase: phrase, key: Speech.voiceKeyFor(ev, party, voiceMonsters()) });
    voiceTrimQueue();
    voicePump();
  }

  // Keep speech from falling minutes behind: over the cap, shed the oldest
  // mechanics first; if a burst of story lines alone overflows twice the cap
  // (a reconnect replay), keep only the newest of those.
  function voiceTrimQueue() {
    var q = V.queue;
    for (var i = 0; i < q.length && q.length > VOICE_MAX_QUEUE;) {
      if (Speech.isMechanic(q[i].ev)) q.splice(i, 1); else i++;
    }
    if (q.length > VOICE_MAX_QUEUE * 2) q.splice(0, q.length - VOICE_MAX_QUEUE * 2);
  }

  function voicePump() {
    if (V.current || !V.queue.length || !voiceArmed()) return;
    var item = V.queue.shift();
    var chunks = Speech.chunksFor(item.phrase);
    if (!chunks.length) { voicePump(); return; }
    var prof = voiceProfile(item.key);
    var rate = (VOICE_RATES[V.settings.rate] || 1) * (prof.rate || 1);
    var node = transcriptEl().querySelector('[data-seq="' + item.ev.seq + '"]');
    var cur = { item: item, chunks: chunks, idx: 0, node: node, timer: null, utt: null };
    V.current = cur;
    if (node) node.classList.add('speaking');
    voiceSpeakChunk(cur, prof, rate);
  }

  function voiceSpeakChunk(cur, prof, rate) {
    var text = cur.chunks[cur.idx];
    var u = new SpeechSynthesisUtterance(text);
    if (prof.voice) { u.voice = prof.voice; u.lang = prof.voice.lang || u.lang; }
    u.pitch = prof.pitch || 1;
    u.rate = rate;
    cur.utt = u;   // hold the reference: an unreferenced utterance can be GC'd mid-speech and never fire onend
    var done = function () {
      if (V.current !== cur || cur.utt !== u) return;   // stale: skipped or cancelled
      clearTimeout(cur.timer);
      cur.idx++;
      if (cur.idx < cur.chunks.length) { voiceSpeakChunk(cur, prof, rate); return; }
      voiceFinishCurrent();
      voicePump();
    };
    u.onend = done;
    u.onerror = done;     // not-allowed / synthesis-failed / interrupted: move on rather than stall
    // Watchdog: Safari sometimes never fires onend; Chrome loses it across tab switches.
    cur.timer = setTimeout(done, Math.max(2000, (text.length * 90) / rate) + 2500);
    try { if (V.synth.paused) V.synth.resume(); } catch (e) { /* ignore */ }
    V.synth.speak(u);
  }

  function voiceFinishCurrent() {
    var cur = V.current;
    if (!cur) return;
    clearTimeout(cur.timer);
    if (cur.node) cur.node.classList.remove('speaking');
    V.current = null;
  }

  function voiceCancelAll() {
    V.queue = [];
    voiceFinishCurrent();
    if (V.supported) { try { V.synth.cancel(); } catch (e) { /* ignore */ } }
  }

  // Skip the line being read. iOS drops a speak() issued synchronously after
  // cancel(), so the next line is pumped a beat later.
  function voiceSkip() {
    if (!V.supported) return;
    voiceFinishCurrent();
    try { V.synth.cancel(); } catch (e) { /* ignore */ }
    setTimeout(voicePump, 80);
  }

  // Switching games: silence, and speak nothing until the history replay is done.
  function voiceReset() { voiceCancelAll(); V.sinceSeq = Infinity; V.caughtUp = false; }
  function voiceCaughtUp(seq) { V.caughtUp = true; V.sinceSeq = seq; }
  // Move the "speak from here" mark to the latest seen event — but only once
  // the history replay has been rendered; before that the Infinity guard
  // stays, or enabling mid-load would read the whole transcript.
  function voiceMarkNow() { if (V.caughtUp) V.sinceSeq = S.lastSeq; }

  function voiceOnGameEnd(status) {
    // 'finished' lets the epilogue finish; anything else is a halt.
    if (status !== 'finished') voiceCancelAll();
  }

  // Must run inside a user gesture the first time (autoplay policy): the short
  // confirmation is what unlocks speech for the rest of the page.
  function voiceEnable() {
    V.settings.enabled = true;
    voiceSaveSettings();
    if (!V.unlocked) {
      V.unlocked = true;
      voiceRefreshVoices();
      var u = new SpeechSynthesisUtterance('Voice on.');
      var p = voiceProfile('dm');
      if (p.voice) { u.voice = p.voice; u.lang = p.voice.lang || u.lang; }
      V.heldUtt = u;
      u.onend = u.onerror = function () { V.heldUtt = null; };
      try { V.synth.speak(u); } catch (e) { /* ignore */ }
    }
    voiceMarkNow();           // only what arrives from now on; never the replayed transcript
    voiceRenderControls();
  }

  function voiceDisable() {
    V.settings.enabled = false;
    voiceSaveSettings();
    voiceCancelAll();
    voiceRenderControls();
  }

  function voiceRenderControls() {
    $('voice-on').checked = V.settings.enabled;
    $('voice-rate').value = V.settings.rate;
    $('voice-mute-mech').checked = V.settings.muteMechanics;
    var locked = V.settings.enabled && !V.unlocked;
    $('voice').classList.toggle('locked', locked);
    $('voice-hint').hidden = !locked;
    $('voice-skip').disabled = !voiceArmed();
  }

  function voiceInit() {
    var synth = window.speechSynthesis;
    if (!synth || typeof window.SpeechSynthesisUtterance !== 'function' || !Speech) {
      console.info('dnd-sim: speechSynthesis unavailable; spoken narration hidden');
      return;
    }
    V.supported = true;
    V.synth = synth;
    voiceLoadSettings();
    $('voice').hidden = false;
    voiceRefreshVoices();
    if (typeof synth.addEventListener === 'function') synth.addEventListener('voiceschanged', voiceRefreshVoices);
    else synth.onvoiceschanged = voiceRefreshVoices;

    $('voice-on').addEventListener('change', function () {
      if (this.checked) voiceEnable(); else voiceDisable();
    });
    $('voice-rate').addEventListener('change', function () {
      V.settings.rate = VOICE_RATES[this.value] ? this.value : 'normal';
      voiceSaveSettings();
    });
    $('voice-mute-mech').addEventListener('change', function () {
      V.settings.muteMechanics = this.checked;
      voiceSaveSettings();
      if (this.checked) V.queue = V.queue.filter(function (q) { return !Speech.isMechanic(q.ev); });
    });
    $('voice-skip').addEventListener('click', voiceSkip);

    // Voice was on last time. It cannot start without a gesture, so the toggle
    // shows "tap to start" and the first tap anywhere on the page unlocks it.
    if (V.settings.enabled) {
      var unlockOnce = function (e) {
        if (e && e.target && e.target.id === 'voice-on') return;   // the toggle decides for itself
        document.removeEventListener('click', unlockOnce, true);
        document.removeEventListener('touchend', unlockOnce, true);
        if (V.settings.enabled && !V.unlocked) voiceEnable();
      };
      document.addEventListener('click', unlockOnce, true);
      document.addEventListener('touchend', unlockOnce, true);
    }
    window.addEventListener('pagehide', voiceCancelAll);
    voiceRenderControls();
  }

  // ---------- stream ----------
  function setConn(text, cls) {
    var n = $('conn');
    n.textContent = 'stream: ' + text;
    n.className = 'conn ' + cls;
  }

  function closeStream() {
    if (S.es) { try { S.es.close(); } catch (e) { /* ignore */ } S.es = null; }
    setConn('offline', 'conn-off');
  }

  function connect(gameId) {
    closeStream();
    var url = '/api/games/' + encodeURIComponent(gameId) + '/stream?after=' + S.lastSeq;
    var es = new EventSource(url);
    S.es = es;
    setConn('connecting…', 'conn-warn');

    es.onopen = function () { setConn('live', 'conn-on'); };
    es.onerror = function () {
      // EventSource retries on its own (server sends retry: 3000)
      if (es.readyState === 2) setConn('closed', 'conn-off');
      else setConn('reconnecting…', 'conn-warn');
    };
    EVENT_KINDS.forEach(function (kind) {
      es.addEventListener(kind, function (e) { onMessage(e); });
    });
    es.onmessage = function (e) { onMessage(e); };
    es.addEventListener('end', function (e) {
      var d = {};
      try { d = JSON.parse(e.data); } catch (err) { /* ignore */ }
      S.status = d.status || S.status;
      closeStream();
      voiceOnGameEnd(S.status);
      setConn('ended (' + S.status + ')', 'conn-off');
      appendNode(el('div', 'end-banner', 'session ' + S.status));
      refreshSnapshot();
    });
  }

  function onMessage(e) {
    var ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    renderEvent(ev);
  }

  // ---------- controls ----------
  function updateControls() {
    var live = S.gameId && !TERMINAL[S.status];
    $('btn-pause').disabled = !(live && S.status === 'running');
    $('btn-resume').disabled = !(live && S.status === 'paused');
    $('btn-stop').disabled = !live;
    $('btn-note').disabled = !live;
  }

  function control(action) {
    if (!S.gameId) return;
    if (action === 'stop') voiceCancelAll();
    api('/api/games/' + encodeURIComponent(S.gameId) + '/' + action, { method: 'POST', body: {} })
      .then(function (r) { S.status = r.status || S.status; renderAll(); })
      .catch(function (e) { alert(action + ' failed: ' + e.message); });
  }

  // ---------- game selection ----------
  function selectGame(id) {
    if (!id) return;
    S.gameId = id;
    S.activeId = null;
    resetTranscript();
    voiceReset();
    S.snapshot = null;
    try { localStorage.setItem('dndsim.game', id); } catch (e) { /* private mode */ }
    if ($('game-select').value !== id) $('game-select').value = id;

    api('/api/games/' + encodeURIComponent(id) + '/events?after=-1')
      .then(function (events) {
        (events || []).forEach(renderEvent);
        voiceCaughtUp(S.lastSeq);
        return refreshSnapshot();
      })
      .then(function () {
        if (!TERMINAL[S.status]) connect(id);
        else setConn('ended (' + S.status + ')', 'conn-off');
        startPolling();
      })
      .catch(function (e) { alert('load failed: ' + e.message); });
  }

  function startPolling() {
    if (S.pollTimer) clearInterval(S.pollTimer);
    S.pollTimer = setInterval(function () {
      if (S.gameId && !TERMINAL[S.status]) refreshSnapshot();
    }, 5000);
  }

  function loadGames(preferred) {
    return api('/api/games').then(function (games) {
      var sel = $('game-select');
      clear(sel);
      if (!games.length) {
        var o = el('option', null, 'no games yet');
        o.value = '';
        sel.appendChild(o);
        return null;
      }
      games.forEach(function (g) {
        var when = new Date(num(g.created_at) * 1000);
        var label = (g.title || g.id) + ' · ' + g.status +
          ' · $' + num(g.cost_usd).toFixed(3) +
          ' · ' + when.toLocaleString();
        var opt = el('option', null, label);
        opt.value = g.id;
        sel.appendChild(opt);
      });
      var pick = preferred;
      if (!pick) { try { pick = localStorage.getItem('dndsim.game'); } catch (e) { pick = null; } }
      var ok = games.some(function (g) { return g.id === pick; });
      return ok ? pick : games[0].id;
    });
  }

  // ---------- new game ----------
  function openNewGame() {
    $('ng-error').hidden = true;
    $('newgame').hidden = false;
    if (S.presets.length) return;
    api('/api/presets').then(function (presets) {
      S.presets = presets || [];
      var sel = $('ng-preset');
      clear(sel);
      S.presets.forEach(function (p, i) {
        var o = el('option', null, p.name);
        o.value = String(i);
        sel.appendChild(o);
      });
      applyPreset(0);
    }).catch(function (e) {
      $('ng-error').textContent = 'could not load presets: ' + e.message;
      $('ng-error').hidden = false;
    });
  }

  function applyPreset(i) {
    var p = S.presets[i];
    if (!p) return;
    var cfg = p.config || {};
    $('ng-desc').textContent = p.description || '';
    if (cfg.seed !== undefined) $('ng-seed').value = cfg.seed;
    if (cfg.budget_usd !== undefined) $('ng-budget').value = cfg.budget_usd;
    if (cfg.tempo_ms !== undefined) $('ng-tempo').value = cfg.tempo_ms;
    $('ng-setting').value = cfg.setting || '';
    $('ng-tone').value = cfg.tone || 'classic heroic';
  }

  function submitNewGame(e) {
    e.preventDefault();
    var i = num($('ng-preset').value, 0);
    var preset = S.presets[i];
    if (!preset) return;
    var cfg = JSON.parse(JSON.stringify(preset.config || {}));
    cfg.seed = num($('ng-seed').value, 0);
    cfg.budget_usd = num($('ng-budget').value, 1);
    cfg.tempo_ms = num($('ng-tempo').value, 800);
    cfg.setting = $('ng-setting').value;
    cfg.tone = $('ng-tone').value;

    var btn = $('ng-start');
    btn.disabled = true;
    api('/api/games', { method: 'POST', body: { config: cfg, title: preset.name } })
      .then(function (r) {
        $('newgame').hidden = true;
        S.budget = cfg.budget_usd;
        return loadGames(r.id).then(function () { selectGame(r.id); });
      })
      .catch(function (err) {
        $('ng-error').textContent = err.message;
        $('ng-error').hidden = false;
      })
      .then(function () { btn.disabled = false; });
  }

  // ---------- wiring ----------
  function init() {
    $('btn-new').addEventListener('click', openNewGame);
    $('ng-cancel').addEventListener('click', function () { $('newgame').hidden = true; });
    $('newgame-form').addEventListener('submit', submitNewGame);
    $('ng-preset').addEventListener('change', function () { applyPreset(num(this.value, 0)); });
    $('game-select').addEventListener('change', function () { if (this.value) selectGame(this.value); });
    $('btn-pause').addEventListener('click', function () { control('pause'); });
    $('btn-resume').addEventListener('click', function () { control('resume'); });
    $('btn-stop').addEventListener('click', function () {
      if (confirm('Stop this game?')) control('stop');
    });
    $('note-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var text = $('note-text').value.trim();
      if (!text || !S.gameId) return;
      api('/api/games/' + encodeURIComponent(S.gameId) + '/note',
        { method: 'POST', body: { text: text } })
        .then(function () { $('note-text').value = ''; })
        .catch(function (e2) { alert('note failed: ' + e2.message); });
    });
    $('toggle-mech').addEventListener('change', function () {
      transcriptEl().classList.toggle('hide-mech', !this.checked);
    });
    window.addEventListener('resize', function () { drawGrid(); });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { voiceCancelAll(); }
      else { voiceMarkNow(); }  // back: speak only what arrives from now on
      if (!document.hidden && S.gameId && !TERMINAL[S.status]) {
        if (!S.es || S.es.readyState === 2) connect(S.gameId);
        refreshSnapshot();
      }
    });

    voiceInit();

    api('/api/health').then(function (h) {
      if (h && h.mock) {
        var pill = el('span', 'pill pill-quiet', 'mock');
        $('status-pill').parentNode.insertBefore(pill, $('status-pill'));
      }
    }).catch(function () { /* ignore */ });

    loadGames().then(function (id) {
      if (id) selectGame(id); else openNewGame();
    }).catch(function (e) { console.warn(e); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
