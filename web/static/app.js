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
    events: [],         // every event in arrival order; the narrator's playhead indexes it
    game: null,         // last /api/games/<id> body (config, ledger, …)
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
    loadGen: 0,         // bumped per selectGame; a stale load must not finish
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

  // A game that has ended cannot un-end. Three sources report status — the SSE
  // `end` frame, the 5 s poll, and a control's own reply — and the control
  // reply is the one that can be stale by the time it arrives: POST /stop
  // answers "running", because the loop only notices the stop at its next
  // gate. Landing after the stream has already said "stopped", it would put
  // the UI back to claiming a live game, with a Stop button that then does
  // nothing. Latching terminal makes the ordering not matter.
  function setGameStatus(next) {
    if (!next || next === S.status) return;
    if (TERMINAL[S.status] && !TERMINAL[next]) return;
    S.status = next;
  }
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
    S.events = [];
  }

  function atBottom(node) {
    return node.scrollTop + node.clientHeight >= node.scrollHeight - 60;
  }

  function appendNode(node) {
    var t = transcriptEl();
    var empty = t.querySelector('.empty');
    if (empty) t.removeChild(empty);
    var follow = $('toggle-follow').checked && !voiceOwnsScroll();
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
    S.events.push(ev);

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
        var follow = $('toggle-follow').checked && !voiceOwnsScroll(), wasBottom = atBottom(t);
        S.groupBody.appendChild(node);
        if (follow && wasBottom) t.scrollTop = t.scrollHeight;
      } else {
        appendNode(node);
      }
    }
    if (node) node.setAttribute('data-seq', String(ev.seq));
    voiceOnEvent();
    // Monsters are spawned mid-game and announced by this one event; every
    // later trigger is debounced 700 ms, which at tempo_ms 0 is several turns
    // of a creature the page has never heard of — no name for it, no voice
    // for it. Fetch on the spawn itself instead of waiting.
    if (ev.kind === 'system' && ev.data && ev.data.encounter) refreshSnapshot();
    else if (SNAPSHOT_TRIGGERS[ev.kind]) scheduleSnapshot();
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
      S.game = g;
      S.snapshot = g.snapshot || null;
      setGameStatus(g.status);
      voiceCtxDirty();
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


  // ---------- narration playback (Web Speech API) ----------
  // The selection + wording live in speech.js (pure, node-testable); this is
  // the transport and the speechSynthesis glue. Designed around iPad Safari:
  // speech only starts inside a user gesture, the voice list arrives late (or
  // never fires voiceschanged), onend can go missing, and speak() right after
  // cancel() is dropped — each of which has a line below.
  //
  // The model is a PLAYHEAD, not a queue. `V.cursor` is an index into
  // `S.events`, the whole transcript in arrival order, and the narrator reads
  // forward from it. Nothing is ever silently dropped and nothing is ever
  // silently skipped: pausing (or backgrounding the tab) leaves the playhead
  // exactly where it was, so coming back resumes the line you were on rather
  // than jumping to whatever the game has reached since. Going faster than the
  // game is impossible; the game going faster than the narrator is handled by
  // holding it — see voiceHoldEval.
  var Speech = window.DndSpeech || null;
  var VOICE_RATES = { slow: 0.85, normal: 1.0, fast: 1.2 };
  var HOLD_HIGH = 3;        // unread speakable lines at which we ask the game to wait
  var HOLD_LOW = 1;         // ... and at which we let it go again (hysteresis)
  var HOLD_LEASE = 12;      // seconds asked for; the server caps it and it self-expires
  var HOLD_TICK = 4000;     // renewal interval, comfortably inside the lease
  var BACKLOG_SCAN = 400;   // cap the count: the badge only needs "lots"
  // This tab's hold lease. Per tab, not per browser: two tabs on one game are
  // two listeners at different places in it, and the server waits for whichever
  // is furthest behind. sessionStorage keeps the id across a reload of *this*
  // tab, so a reload renews its own lease instead of stranding one to expire.
  var CLIENT_ID = (function () {
    var k = 'dndsim.client', id = null;
    try { id = sessionStorage.getItem(k); } catch (e) { /* private mode */ }
    if (!id) {
      id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
      try { sessionStorage.setItem(k, id); } catch (e) { /* ignore */ }
    }
    return id;
  })();

  var V = {
    supported: false,
    synth: null,
    settings: { enabled: false, rate: 'normal', muteMechanics: false, hold: true },
    unlocked: false,       // a user gesture has started speech on this page
    playing: false,        // transport state: the spectator wants to hear it
    loading: false,        // a game's history is being replayed; the playhead is not placed yet
    cursor: 0,             // index into S.events of the line to read next
    current: null,         // { ev, chunks, idx, node, timer, utt }
    resumeChunk: null,     // { seq, idx }: pick a part-read line up where it stopped
    voices: [],
    profiles: {},
    ctx: null,             // { names, party }, rebuilt when the snapshot changes
    heldUtt: null,         // keeps the unlock utterance referenced until it ends
    holdTimer: null,
    holding: false,
    holdBroken: false      // this game will not take a hold (ended, or another process)
  };

  function pageHidden() { return typeof document !== 'undefined' && !!document.hidden; }
  // Backgrounded tabs suspend synthesis and hand back garbled audio, so the
  // playhead stops there too — but it stops, it does not move.
  function voiceArmed() {
    return V.supported && V.settings.enabled && V.unlocked && V.playing &&
           !V.loading && !pageHidden();
  }
  function voiceUsable() { return V.supported && V.settings.enabled; }

  function voiceLoadSettings() {
    try {
      var raw = localStorage.getItem('dndsim.voice');
      var o = raw ? JSON.parse(raw) : null;
      if (o && typeof o === 'object') {
        V.settings.enabled = !!o.enabled;
        V.settings.rate = VOICE_RATES[o.rate] ? o.rate : 'normal';
        V.settings.muteMechanics = !!o.muteMechanics;
        V.settings.hold = o.hold === undefined ? true : !!o.hold;
      }
    } catch (e) { /* private mode or junk */ }
  }
  function voiceSaveSettings() {
    try { localStorage.setItem('dndsim.voice', JSON.stringify(V.settings)); } catch (e) { /* ignore */ }
  }

  // The playhead survives a reload: it is the answer to "where was I?".
  function voicePosKey() { return S.gameId ? 'dndsim.pos.' + S.gameId : null; }
  function voiceSavePos() {
    var k = voicePosKey();
    // Never while loading: the replay would overwrite the very position
    // voiceStartAt is about to read back, and file the top of the transcript
    // as where this spectator was. Never with an empty transcript either:
    // "position 0 of a game we hold no events for" is not a place anyone was,
    // and writing it would send that game back to its first line.
    if (!k || V.loading || !S.events.length) return;
    var ev = S.events[V.cursor];
    var seq = ev ? ev.seq : (S.lastSeq + 1);
    try { localStorage.setItem(k, String(seq)); } catch (e) { /* ignore */ }
  }
  function voiceLoadPos() {
    var k = voicePosKey();
    if (!k) return null;
    try {
      var raw = localStorage.getItem(k);
      if (raw === null) return null;
      var n = Number(raw);
      return isFinite(n) ? n : null;
    } catch (e) { return null; }
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
  // combat the combatant list can be empty), the display names, and the
  // monsters whose stat block names a language they can speak — the seats a
  // novelty voice may be cast to. Cached: voicePump asks for it per line.
  function voiceCtx() {
    if (!V.ctx) {
      var cs = combatants(), names = {}, party = {};
      Object.keys(cs).forEach(function (id) {
        var c = cs[id] || {};
        if (c.name) names[id] = c.name;
        if (c.side === 'party') party[id] = true;
      });
      var cfg = (S.game || {}).config || {};
      if (Array.isArray(cfg.party)) cfg.party.forEach(function (m) { if (m && m.id) party[m.id] = true; });
      V.ctx = { names: names, party: party, monsters: Speech.speakingMonsters(cs) };
    }
    return V.ctx;
  }
  function voiceCtxDirty() { V.ctx = null; }

  // The transcript dims what the narrator has already read, so a spectator
  // coming back to the tab can see where they were. Marked one node at a time
  // as the playhead advances; a jump (back, live, a click, a game load) moves
  // it arbitrarily, so those resync the lot.
  function voiceMarkRead(node) { if (node) node.classList.add('read'); }

  function voiceResyncRead() {
    var at = S.events[V.cursor];
    var upto = at ? at.seq : Infinity;   // cursor past the end: everything is read
    var nodes = transcriptEl().querySelectorAll('[data-seq]');
    for (var i = 0; i < nodes.length; i++) {
      var seq = Number(nodes[i].getAttribute('data-seq'));
      nodes[i].classList.toggle('read', isFinite(seq) && seq < upto);
    }
  }

  function voicePhrase(ev) {
    if (!ev || !Speech.shouldSpeak(ev, V.settings)) return null;
    var ctx = voiceCtx();
    return Speech.phraseFor(ev, ctx.names, ctx.party) || null;
  }

  // Unread speakable lines ahead of the playhead, counted up to BACKLOG_SCAN.
  // shouldSpeak alone (not phraseFor) — this runs on every event, and the
  // handful of lines that turn out to have nothing to say do not change any
  // decision made from the count.
  function voiceBacklog() {
    var n = 0;
    for (var i = V.cursor; i < S.events.length && n < BACKLOG_SCAN; i++) {
      if (Speech.shouldSpeak(S.events[i], V.settings)) n++;
    }
    return n;
  }
  function voiceBehind() { return V.supported && V.settings.enabled ? voiceBacklog() : 0; }

  // ---- transport ----
  function voicePlay() {
    if (!voiceUsable()) return;
    if (!V.unlocked) voiceUnlock();   // must happen inside the click that got us here
    V.playing = true;
    voicePump();
    voiceHoldStart();
    voiceRenderControls();
  }

  function voicePausePlayback() {
    V.playing = false;
    voiceStopCurrent(false);
    voiceHoldStop();
    voiceRenderControls();
  }

  function voiceToggle() { if (V.playing) voicePausePlayback(); else voicePlay(); }

  // Stop the utterance in flight. `advance` moves the playhead past the line;
  // without it the playhead stays put and the line is read again from the part
  // it had reached, which is what pause/resume has to do.
  function voiceStopCurrent(advance) {
    var cur = V.current;
    if (cur) {
      clearTimeout(cur.timer);
      if (cur.node) cur.node.classList.remove('speaking');
      V.current = null;
      if (advance) {
        V.cursor++;
        V.resumeChunk = null;
      } else {
        V.resumeChunk = { seq: cur.ev.seq, idx: cur.idx };
      }
      voiceSavePos();
    }
    if (V.supported) { try { V.synth.cancel(); } catch (e) { /* ignore */ } }
  }

  // iOS drops a speak() issued synchronously after cancel(), so every transport
  // move that cancels pumps the next line a beat later.
  function voiceRepump() { setTimeout(voicePump, 80); }

  function voiceSkip() {
    if (!voiceUsable()) return;
    if (V.current) voiceStopCurrent(true);
    else { V.cursor = voiceNextSpeakable(V.cursor + 1); V.resumeChunk = null; voiceSavePos(); }
    voiceResyncRead();
    voiceRenderControls();
    voiceRepump();
  }

  function voiceBack() {
    if (!voiceUsable()) return;
    var from = V.current ? indexOfSeq(V.current.ev.seq) : V.cursor;
    voiceStopCurrent(false);
    V.resumeChunk = null;
    var prev = voicePrevSpeakable(from - 1);
    if (prev !== null) V.cursor = prev;
    voiceSavePos();
    voiceResyncRead();
    voiceRenderControls();
    voiceRepump();
  }

  function voiceJumpLive() {
    if (!voiceUsable()) return;
    voiceStopCurrent(false);
    V.resumeChunk = null;
    V.cursor = S.events.length;
    voiceSavePos();
    voiceResyncRead();
    voiceHoldRelease();
    voiceRenderControls();
    voiceRepump();
  }

  // Read from a particular transcript line: the spectator's own "back to here".
  function voicePlayFromSeq(seq) {
    if (!voiceUsable()) return;
    var i = indexOfSeq(seq);
    if (i < 0) return;
    voiceStopCurrent(false);
    V.resumeChunk = null;
    V.cursor = i;
    voiceSavePos();
    voiceResyncRead();
    voicePlay();
    voiceRepump();
  }

  function indexOfSeq(seq) {
    for (var i = S.events.length - 1; i >= 0; i--) if (S.events[i].seq === seq) return i;
    return -1;
  }
  function voiceNextSpeakable(from) {
    for (var i = Math.max(0, from); i < S.events.length; i++) {
      if (Speech.shouldSpeak(S.events[i], V.settings)) return i;
    }
    return S.events.length;
  }
  function voicePrevSpeakable(from) {
    for (var i = Math.min(from, S.events.length - 1); i >= 0; i--) {
      if (Speech.shouldSpeak(S.events[i], V.settings)) return i;
    }
    return null;
  }

  // ---- the reader ----
  function voicePump() {
    if (V.current || !voiceArmed()) { voiceRenderControls(); return; }
    while (V.cursor < S.events.length) {
      var ev = S.events[V.cursor];
      var phrase = voicePhrase(ev);
      var chunks = phrase ? Speech.chunksFor(phrase) : [];
      if (!chunks.length) { V.cursor++; continue; }
      var idx = 0;
      if (V.resumeChunk && V.resumeChunk.seq === ev.seq && V.resumeChunk.idx < chunks.length) {
        idx = V.resumeChunk.idx;
      }
      V.resumeChunk = null;
      var node = transcriptEl().querySelector('[data-seq="' + ev.seq + '"]');
      var cur = { ev: ev, phrase: phrase, chunks: chunks, idx: idx, node: node, timer: null, utt: null };
      V.current = cur;
      if (node) { node.classList.add('speaking'); voiceFollow(node); }
      voiceSavePos();
      // Cast at the playhead, not on arrival: a monster's first line can beat
      // the snapshot that first names it, and every moment the line waits its
      // turn is a moment that fetch has to land.
      var ctx = voiceCtx();
      voiceSpeakChunk(cur, voiceProfile(Speech.voiceKeyFor(ev, ctx.party, ctx.monsters)));
      voiceRenderControls();
      return;
    }
    voiceSavePos();
    voiceRenderControls();
  }

  function voiceSpeakChunk(cur, prof) {
    var rate = (VOICE_RATES[V.settings.rate] || 1) * (prof.rate || 1);
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
      if (cur.idx < cur.chunks.length) { voiceSpeakChunk(cur, prof); return; }
      voiceFinishLine();
    };
    u.onend = done;
    u.onerror = done;     // not-allowed / synthesis-failed / interrupted: move on rather than stall
    // Watchdog: Safari sometimes never fires onend; Chrome loses it across tab switches.
    cur.timer = setTimeout(done, Math.max(2000, (text.length * 90) / rate) + 2500);
    try { if (V.synth.paused) V.synth.resume(); } catch (e) { /* ignore */ }
    V.synth.speak(u);
  }

  function voiceFinishLine() {
    var cur = V.current;
    if (!cur) return;
    clearTimeout(cur.timer);
    if (cur.node) { cur.node.classList.remove('speaking'); voiceMarkRead(cur.node); }
    V.current = null;
    V.cursor++;
    V.resumeChunk = null;
    voiceSavePos();
    voicePump();
    voiceHoldEval();
  }

  // When the narrator is behind, the transcript follows the narrator rather
  // than the tail — otherwise "follow" scrolls away from the line you can hear.
  function voiceFollow(node) {
    if (!node || !$('toggle-follow').checked) return;
    var t = transcriptEl();
    var top = node.offsetTop - t.offsetTop;
    if (top < t.scrollTop || top > t.scrollTop + t.clientHeight - 40) {
      t.scrollTop = Math.max(0, top - t.clientHeight * 0.5);
    }
  }
  function voiceOwnsScroll() { return voiceArmed() && voiceBacklog() > 1; }

  // ---- holding the game for the narrator ----
  // A lease, renewed every HOLD_TICK while the narrator is behind: the game
  // waits, so what you hear is what is happening. It expires by itself, so a
  // tab that dies mid-hold costs the game seconds, not the rest of the session.
  function voiceHoldStart() {
    if (V.holdTimer) return;
    V.holdTimer = setInterval(voiceHoldEval, HOLD_TICK);
    voiceHoldEval();
  }
  function voiceHoldStop() {
    if (V.holdTimer) { clearInterval(V.holdTimer); V.holdTimer = null; }
    voiceHoldRelease();
  }
  function voiceHoldEval() {
    if (!V.settings.hold || V.holdBroken || !S.gameId || TERMINAL[S.status] || !voiceArmed()) {
      voiceHoldRelease();
      return;
    }
    var behind = voiceBacklog();
    if (V.holding ? behind > HOLD_LOW : behind >= HOLD_HIGH) voiceHoldSend(HOLD_LEASE);
    else voiceHoldRelease();
  }
  function voiceHoldRelease() {
    if (!V.holding) return;
    V.holding = false;
    voiceHoldSend(0);
  }
  function voiceHoldSend(seconds) {
    if (!S.gameId) return;
    if (seconds > 0) V.holding = true;
    var id = S.gameId;
    api('/api/games/' + encodeURIComponent(id) + '/hold',
        { method: 'POST', body: { seconds: seconds, client: CLIENT_ID } })
      .catch(function () {
        // 404/409/501: this game cannot be held (ended, restarted, or served by
        // another process). Stop asking; the narrator just runs behind.
        if (id === S.gameId) { V.holdBroken = true; V.holding = false; voiceRenderControls(); }
      });
  }

  // ---- lifecycle ----
  // Switching games: silence, and start at the saved playhead if there is one.
  // Called while the outgoing game is still S.gameId, so the playhead is saved
  // and the lease released against the game they belong to.
  function voiceReset() {
    voiceStopCurrent(false);
    voiceHoldStop();
    V.loading = true;   // until voiceStartAt places the playhead in the new game
    V.resumeChunk = null;
    V.cursor = 0;
    V.holding = false;
    V.holdBroken = false;
    voiceCtxDirty();
    voiceRenderControls();
  }

  // Called once the history replay has been rendered: with no saved playhead
  // the narrator starts at the live edge rather than reading the whole
  // transcript back; with one, it picks up where this browser left off.
  function voiceStartAt() {
    var seq = voiceLoadPos();
    var i = seq === null ? -1 : indexOfSeq(seq);
    if (i < 0 && seq !== null) {
      // The saved line is not in this transcript (or is the one past its end).
      i = seq > S.lastSeq ? S.events.length : -1;
    }
    V.cursor = i < 0 ? S.events.length : i;
    V.resumeChunk = null;
    V.loading = false;   // the history is on screen and the playhead is placed
    voiceSavePos();
    voiceResyncRead();
    voiceRenderControls();
    voicePump();
  }

  function voiceOnEvent() {
    if (!V.settings.enabled) { V.cursor = S.events.length; V.resumeChunk = null; }
    voicePump();
    voiceHoldEval();
    voiceRenderControls();
  }

  function voiceOnGameEnd() {
    // Nothing is cancelled: the transcript is complete and the playhead is
    // wherever the listener is. Let it read to the end; there is just no
    // longer anything to hold.
    V.holdBroken = true;
    voiceHoldStop();
    voiceRenderControls();
  }

  // Must run inside a user gesture the first time (autoplay policy): the short
  // confirmation is what unlocks speech for the rest of the page.
  function voiceUnlock() {
    if (V.unlocked || !V.supported) return;
    V.unlocked = true;
    voiceRefreshVoices();
    var u = new SpeechSynthesisUtterance('Voice on.');
    var p = voiceProfile('dm');
    if (p.voice) { u.voice = p.voice; u.lang = p.voice.lang || u.lang; }
    V.heldUtt = u;
    u.onend = u.onerror = function () { V.heldUtt = null; };
    try { V.synth.speak(u); } catch (e) { /* ignore */ }
  }

  function voiceEnable() {
    V.settings.enabled = true;
    voiceSaveSettings();
    voicePlay();
  }

  function voiceDisable() {
    V.settings.enabled = false;
    voiceSaveSettings();
    voicePausePlayback();
  }

  // ---- controls ----
  function voiceNowText() {
    if (!V.settings.enabled) return { tag: '', text: 'voice off' };
    if (!V.unlocked) return { tag: '', text: 'press play to start — browsers only allow speech from a tap' };
    if (V.current) {
      return { tag: V.current.ev.kind.replace('_', ' '), text: V.current.phrase };
    }
    if (!V.playing) {
      var next = S.events[V.cursor];
      if (next) return { tag: 'paused at', text: voicePhrase(next) || ('#' + next.seq) };
      return { tag: '', text: 'paused — nothing to read yet' };
    }
    if (pageHidden()) return { tag: '', text: 'held while the tab is in the background' };
    return { tag: '', text: 'caught up — waiting for the table' };
  }

  function voiceRenderControls() {
    if (!V.supported) return;
    $('voice-on').checked = V.settings.enabled;
    $('voice-rate').value = V.settings.rate;
    $('voice-mute-mech').checked = V.settings.muteMechanics;
    $('voice-hold').checked = V.settings.hold;

    var locked = V.settings.enabled && !V.unlocked;
    $('voice').classList.toggle('locked', locked);
    $('voice-hint').hidden = !locked;

    var on = voiceUsable();
    var glyph = V.playing ? '⏸' : '▶';
    var label = V.playing ? 'Pause the narration' : 'Play the narration';
    ['vt-play', 'vt-play2'].forEach(function (id) {
      var b = $(id);
      b.disabled = !on;
      b.textContent = glyph;
      b.setAttribute('aria-label', label);
      b.classList.toggle('on', V.playing);
    });
    $('vt-back').disabled = !on;
    $('vt-skip').disabled = !on;

    var behind = voiceBehind();
    $('vt-live').disabled = !on || behind === 0;
    var badge = $('vt-behind');
    badge.hidden = !(on && behind > 0);
    badge.textContent = (behind >= BACKLOG_SCAN ? BACKLOG_SCAN + '+' : behind) +
      ' behind' + (V.holding ? ' · holding' : '');

    var now = voiceNowText();
    var el0 = $('vt-now');
    clear(el0);
    if (now.tag) el0.appendChild(el('span', 'vt-tag', now.tag));
    el0.appendChild(document.createTextNode(now.text || ''));

    var t = transcriptEl();
    t.classList.toggle('voice-clickable', on);
    t.classList.toggle('has-playhead', on && behind > 0);
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
    $('voice-panel').hidden = false;
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
      voiceRenderControls();
    });
    $('voice-hold').addEventListener('change', function () {
      V.settings.hold = this.checked;
      voiceSaveSettings();
      if (this.checked) voiceHoldEval(); else voiceHoldRelease();
      voiceRenderControls();
    });
    $('vt-play').addEventListener('click', voiceToggle);
    $('vt-play2').addEventListener('click', voiceToggle);
    $('vt-back').addEventListener('click', voiceBack);
    $('vt-skip').addEventListener('click', voiceSkip);
    $('vt-live').addEventListener('click', voiceJumpLive);
    $('vt-behind').addEventListener('click', voiceJumpLive);

    // Click any spoken line to read from there — the fastest way back to a
    // place you recognise after being away.
    transcriptEl().addEventListener('click', function (e) {
      if (!voiceUsable()) return;
      var n = e.target;
      while (n && n !== this && !n.hasAttribute('data-seq')) n = n.parentNode;
      if (!n || n === this) return;
      var seq = Number(n.getAttribute('data-seq'));
      if (isFinite(seq)) voicePlayFromSeq(seq);
    });

    if (V.settings.enabled) {
      var unlockOnce = function (e) {
        var id = e && e.target && e.target.id;
        if (id === 'voice-on' || id === 'vt-play' || id === 'vt-play2') return;  // those decide for themselves
        document.removeEventListener('click', unlockOnce, true);
        document.removeEventListener('touchend', unlockOnce, true);
        if (V.settings.enabled && !V.unlocked) voicePlay();
      };
      document.addEventListener('click', unlockOnce, true);
      document.addEventListener('touchend', unlockOnce, true);
    }

    window.addEventListener('pagehide', function () {
      voiceStopCurrent(false);
      voiceHoldStop();
    });
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
      setGameStatus(d.status);
      closeStream();
      voiceOnGameEnd();
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
  // A disabled control has to say why: "the buttons do nothing" is otherwise
  // indistinguishable from a game that ended while you were looking elsewhere.
  function ctlNote(msg, bad) {
    var n = $('ctl-note');
    n.textContent = msg || '';
    n.classList.toggle('bad', !!bad);
    n.hidden = !msg;
  }

  function updateControls() {
    var live = !!S.gameId && !TERMINAL[S.status];
    // 'created' counts: the game thread may not have reached run() yet, and a
    // pause issued in that window is honoured by the orchestrator.
    var pausable = live && (S.status === 'running' || S.status === 'created');
    $('btn-pause').disabled = !pausable;
    $('btn-resume').disabled = !(live && S.status === 'paused');
    $('btn-stop').disabled = !live;
    $('btn-note').disabled = !live;
    var why = !S.gameId ? 'No game selected.'
      : TERMINAL[S.status] ? 'This game has ended (' + S.status + '). Narration still plays back.'
      : '';
    ['btn-pause', 'btn-resume', 'btn-stop', 'btn-note'].forEach(function (id) {
      $(id).title = $(id).disabled ? (why || 'Not available while ' + S.status + '.') : '';
    });
    if (!$('ctl-note').classList.contains('bad')) ctlNote(why);
  }

  function control(action) {
    if (!S.gameId) return;
    ctlNote('');
    api('/api/games/' + encodeURIComponent(S.gameId) + '/' + action, { method: 'POST', body: {} })
      .then(function (r) {
        setGameStatus(r.status);
        renderAll(S.game);
        if (action === 'stop') { V.holdBroken = true; voiceHoldStop(); }
        // stop and pause both settle a beat after the call returns: re-read so
        // the buttons show what the game is actually doing.
        refreshSnapshot();
        setTimeout(refreshSnapshot, 1500);
      })
      .catch(function (e) {
        // The usual cause is a status this page believes and the server does
        // not (the game ended, or the process restarted under it), so re-read
        // the truth rather than leaving the buttons lying about what they do.
        ctlNote(action + ' failed: ' + e.message, true);
        refreshSnapshot();
      });
  }

  // ---------- game selection ----------
  function selectGame(id) {
    if (!id) return;
    // Leave the game we are on before taking on the next one's identity.
    // voiceSavePos() and the hold both key on S.gameId, so reassigning it first
    // would file the outgoing playhead under the incoming game — and with the
    // transcript already cleared it files seq 0, which would make the new game
    // replay from its first line — and would leave the old game held.
    voiceReset();
    S.gameId = id;
    S.activeId = null;
    S.status = 'idle';   // clears the terminal latch: a different game may be live
    resetTranscript();
    S.snapshot = null;
    S.game = null;
    ctlNote('');
    try { localStorage.setItem('dndsim.game', id); } catch (e) { /* private mode */ }
    if ($('game-select').value !== id) $('game-select').value = id;

    // Switch again before this load finishes and its history would be rendered
    // into the new game's transcript, and its voiceStartAt would place the
    // playhead against the wrong events. Only the newest load may finish.
    var gen = ++S.loadGen;
    var current = function () { return gen === S.loadGen; };

    api('/api/games/' + encodeURIComponent(id) + '/events?after=-1')
      .then(function (events) {
        if (!current()) return null;
        (events || []).forEach(renderEvent);
        return refreshSnapshot();
      })
      .then(function () { if (current()) voiceStartAt(); })
      .then(function () {
        if (!current()) return;
        if (!TERMINAL[S.status]) connect(id);
        else { setConn('ended (' + S.status + ')', 'conn-off'); voiceOnGameEnd(); }
        startPolling();
      })
      .catch(function (e) {
        if (!current()) return;
        V.loading = false;
        voiceRenderControls();
        ctlNote('load failed: ' + e.message, true);
      });
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
        .then(function () { $('note-text').value = ''; ctlNote('note sent'); })
        .catch(function (e2) { ctlNote('note failed: ' + e2.message, true); });
    });
    $('toggle-mech').addEventListener('change', function () {
      transcriptEl().classList.toggle('hide-mech', !this.checked);
    });
    window.addEventListener('resize', function () { drawGrid(); });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        // Backgrounded synthesis is suspended and comes back garbled, so stop —
        // but leave the playhead on the line it was reading. Coming back
        // resumes there; it does not jump to whatever the game reached
        // meanwhile, which is the whole point of a playhead.
        voiceStopCurrent(false);
        voiceHoldRelease();
      } else {
        voiceRepump();      // pick the same line back up
        voiceHoldEval();
      }
      voiceRenderControls();
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
