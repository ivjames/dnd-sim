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
    presets: [],
    // Write access. Reading a game, listing games and the stream are anonymous
    // and stay that way; creating a game, talking to the table and pause/
    // resume/stop carry a shared secret (web/auth.py). `writes` is what the
    // server says it takes, `token` is what this browser holds, `authed` is
    // whether the two agree.
    writes: 'unknown',  // 'unknown' | 'token' | 'unconfigured'
    token: '',
    authed: false
  };

  var WRITE_HEADER = 'X-Dnd-Token';
  var TOKEN_STORE = 'dndsim.token';

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
    // Sent on every request rather than only on writes: the reads ignore it,
    // and /api/auth needs it to answer whether this browser can write at all.
    if (S.token) init.headers[WRITE_HEADER] = S.token;
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = { error: t }; }
        if (!r.ok) {
          // A write refused for a credential reason is the one failure that
          // changes what this page may show: re-render the gate rather than
          // leaving buttons up that will only fail again.
          noteWriteRefusal(r.status, data);
          throw new Error((data && data.error) || ('HTTP ' + r.status));
        }
        return data;
      });
    });
  }

  // ---------- write access ----------
  function tokenLoad() {
    try { return localStorage.getItem(TOKEN_STORE) || ''; } catch (e) { return ''; }
  }
  function tokenStore(value) {
    try {
      if (value) localStorage.setItem(TOKEN_STORE, value);
      else localStorage.removeItem(TOKEN_STORE);
    } catch (e) { /* private mode: the token lives for this page load only */ }
  }

  function canWrite() { return S.authed; }

  function noteWriteRefusal(status, data) {
    var code = data && data.code;
    if (status === 401 || code === 'unauthorized') {
      S.writes = 'token';
      S.authed = false;
      renderWriteAccess();
    } else if (status === 503 && code === 'writes_unconfigured') {
      S.writes = 'unconfigured';
      S.authed = false;
      renderWriteAccess();
    }
  }

  // What the server takes, and whether this browser has it. A read, and the
  // only way to know before trying a write — which for "create a game" would
  // mean starting one to find out.
  function checkAuth() {
    return api('/api/auth').then(function (a) {
      S.writes = (a && a.writes) || 'unknown';
      S.authed = !!(a && a.authenticated);
      renderWriteAccess();
      return a;
    }).catch(function () {
      // The probe itself failing says nothing about the token; leave the last
      // answer standing and let a real write report its own refusal.
      renderWriteAccess();
      return null;
    });
  }

  function renderWriteAccess() {
    var ok = canWrite();
    var unconfigured = S.writes === 'unconfigured';
    $('btn-new').hidden = !ok;
    $('btn-unlock').hidden = ok || unconfigured;
    $('write-controls').hidden = !ok;
    var locked = $('write-locked');
    if (ok) {
      locked.hidden = true;
      locked.textContent = '';
    } else {
      locked.textContent = unconfigured
        ? 'This server has no write token set, so no one can start a game, ' +
          'pause one or send a note from here. Watching is unaffected.'
        : 'Watching needs nothing. Starting a game, pausing one or sending a ' +
          'note needs the write token — press Unlock.';
      locked.hidden = false;
    }
    if (ok) $('unlock').hidden = true;
  }

  function openUnlock() {
    $('ul-error').hidden = true;
    $('ul-token').value = S.token || '';
    $('unlock').hidden = false;
    $('ul-token').focus();
  }

  function submitUnlock(e) {
    e.preventDefault();
    var value = $('ul-token').value.trim();
    if (!value) {
      $('ul-error').textContent = 'enter the token, or press Forget to clear it';
      $('ul-error').hidden = false;
      return;
    }
    var previous = S.token;
    S.token = value;
    var btn = $('ul-save');
    btn.disabled = true;
    // Checked against the server before it is kept, so a mistyped token is a
    // message here rather than a 401 on the next thing you try to do.
    api('/api/auth').then(function (a) {
      S.writes = (a && a.writes) || 'unknown';
      S.authed = !!(a && a.authenticated);
      if (S.authed) {
        tokenStore(value);
        $('unlock').hidden = true;
      } else {
        S.token = previous;
        $('ul-error').textContent = S.writes === 'unconfigured'
          ? 'this server has no write token set'
          : 'that token was not accepted';
        $('ul-error').hidden = false;
      }
      renderWriteAccess();
    }).catch(function (err) {
      S.token = previous;
      $('ul-error').textContent = err.message;
      $('ul-error').hidden = false;
    }).then(function () { btn.disabled = false; });
  }

  function forgetToken() {
    S.token = '';
    S.authed = false;
    tokenStore('');
    $('ul-token').value = '';
    $('unlock').hidden = true;
    renderWriteAccess();
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


  // ---------- narration playback ----------
  // The selection + wording live in speech.js (pure, node-testable); this is
  // the transport and the glue to whichever engine is speaking.
  //
  // There are two. Server voices (Amazon Polly, rendered by /api/games/<id>/tts
  // and played through one <audio> element) are the good ones and the default
  // where the server has them. The browser's own speechSynthesis is the
  // fallback, and it is a real fallback rather than a legacy path: it answers
  // for a line the server refuses (no budget left, a mock game, a failed
  // synthesis) and for a whole session where the server has no Polly at all.
  // Which one spoke changes nothing above this layer — same playhead, same
  // transport, same holds.
  //
  // Designed around iPad Safari: audio and speech both only start inside a
  // user gesture (voiceUnlock arms BOTH, because the fallback is per line),
  // the voice list arrives late (or never fires voiceschanged), onend can go
  // missing, and speak() right after cancel() is dropped — each has a line below.
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
  // 25 ms of nothing, played inside the tap that turns voice on. An <audio>
  // element that has played once may be played again from script, which is the
  // only way server clips ever sound on iOS.
  var SILENT_WAV = 'data:audio/wav;base64,UklGRuwAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YcgAAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==';
  var CLIP_KEEP = 8;        // fetched clips kept as object URLs (one line ahead, a few behind)
  var CLIP_FAILS = 3;       // consecutive server failures before we stop asking
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
    supported: false,      // something here can speak: server voices, the browser's, or both
    synth: null,           // speechSynthesis, where this browser has it
    audio: null,           // the one <audio> element every server clip plays through
    // Server voices. `available` is what /api/tts said; `degraded` is this
    // page giving up on them for now — a settled refusal (no budget, a mock
    // game) or three failures running — after which every line is spoken by
    // the browser and the panel says so.
    tts: { available: false, checked: false, engine: '', maxChars: 400,
           config: '', degraded: false, fails: 0, reason: '' },
    clips: {},             // clip URL → object URL, for what has been fetched
    pending: {},           // clip URL → in-flight promise, so one URL is asked for once
    clipOrder: [],
    gestureArmed: false,   // the document-level "first tap starts it" listener is on
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
    holding: false,        // the game has CONFIRMED a lease — what the badge may claim
    holdWanted: false,     // what we have asked for — drives the hysteresis, not the badge
    holdSentAt: 0,         // last renewal, so the heartbeat stays a heartbeat
    holdBroken: false      // this game will not take a hold (ended, or another process)
  };

  function pageHidden() { return typeof document !== 'undefined' && !!document.hidden; }
  // Backgrounded tabs suspend synthesis and hand back garbled audio, so the
  // playhead stops there too — but it stops, it does not move.
  //
  // Server-rendered clips have no such problem: an <audio> element keeps
  // playing in a background tab, so this rule could be relaxed for them. It
  // deliberately is not, because it is not only about garbling — a tab nobody
  // is looking at goes on HOLDING the game for a narrator nobody is listening
  // to. Relaxing it is a decision about the hold, not about the audio.
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
    if (V.synth) { try { list = V.synth.getVoices() || []; } catch (e) { list = []; } }
    V.voices = list;
    V.profiles = {};
  }
  function voiceProfile(key) {
    if (!V.synth) return { voice: null, pitch: 1, rate: 1, key: key };
    if (!V.voices.length) voiceRefreshVoices();    // Safari: no voiceschanged, voices just appear
    if (!V.profiles[key]) {
      var lang = document.documentElement.lang || navigator.language || 'en';
      V.profiles[key] = Speech.voiceProfileFor(key, V.voices, lang);
    }
    return V.profiles[key];
  }

  // ---- server voices ----
  // Whether the next line should be asked of the server. A game id is part of
  // it because the endpoint is per game: the budget a clip is charged against
  // is that game's.
  function ttsOn() {
    return !!(V.tts.available && !V.tts.degraded && V.audio && S.gameId);
  }

  // `v` is the server's synthesis fingerprint (engine, language, the DM's
  // voice, the roster). The clip is served `immutable` for a year, and the rest
  // of the URL does not name any of those — so without this, reconfiguring the
  // server would leave every browser replaying the old voice from its own cache
  // forever. The server ignores the value; moving it is the whole job.
  function ttsUrl(key, text) {
    return '/api/games/' + encodeURIComponent(S.gameId) + '/tts' +
           '?key=' + encodeURIComponent(key) + '&text=' + encodeURIComponent(text) +
           (V.tts.config ? '&v=' + encodeURIComponent(V.tts.config) : '');
  }

  // Fetched clips are kept as object URLs so a line can be prefetched while
  // the one before it plays — the gap between chunks is what makes synthesized
  // narration sound like a machine reading rather than someone talking.
  function clipKeep(url, obj) {
    V.clips[url] = obj;
    V.clipOrder.push(url);
    while (V.clipOrder.length > CLIP_KEEP) {
      var old = V.clipOrder.shift();
      if (old === url || !V.clips[old]) continue;
      try { URL.revokeObjectURL(V.clips[old]); } catch (e) { /* ignore */ }
      delete V.clips[old];
    }
  }

  function clipForget() {
    Object.keys(V.clips).forEach(function (u) {
      try { URL.revokeObjectURL(V.clips[u]); } catch (e) { /* ignore */ }
    });
    V.clips = {};
    V.clipOrder = [];
    V.pending = {};
  }

  // Resolves to a playable object URL. Rejects with `.status` set from the
  // response, because which refusal it was decides whether to ask again.
  //
  // One request per URL, ever, including while one is still in flight: a
  // prefetch that has not landed by the time the playhead reaches its line
  // would otherwise be asked for a second time, and on the server those two
  // are a race for the same clip — near the budget the second is refused, and
  // the page reads a 402 as settled and gives up on server voices entirely.
  function clipFetch(url) {
    if (V.clips[url]) return Promise.resolve(V.clips[url]);
    if (V.pending[url]) return V.pending[url];
    var p = fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (body) {
          var msg = body;
          try { msg = (JSON.parse(body) || {}).error || body; } catch (e) { /* not JSON */ }
          var err = new Error(msg || ('HTTP ' + r.status));
          err.status = r.status;
          throw err;
        });
      }
      return r.blob();
    }).then(function (blob) {
      var obj = URL.createObjectURL(blob);
      clipKeep(url, obj);
      return obj;
    });
    V.pending[url] = p;
    // Clear on both paths, so a line refused once can be asked for again after
    // the game's budget or the server's configuration has moved on.
    var forget = function () { delete V.pending[url]; };
    p.then(forget, forget);
    return p;
  }

  // Ask once, at start-up: can this server speak at all? A no is not an error
  // — it is the browser's own voices being the answer, which is what they were
  // before any of this existed.
  function ttsProbe() {
    return fetch('/api/tts', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        V.tts.checked = true;
        if (!d || !d.available) {
          V.tts.reason = (d && d.reason) || 'this server renders no voices';
          return false;
        }
        V.tts.available = true;
        V.tts.engine = d.engine || '';
        V.tts.config = d.config || '';
        V.tts.maxChars = Number(d.max_chars) || V.tts.maxChars;
        return true;
      });
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
    audioDetach();
    if (V.synth) { try { V.synth.cancel(); } catch (e) { /* ignore */ } }
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
      var cur = { ev: ev, phrase: phrase, chunks: chunks, idx: idx, node: node,
                  timer: null, utt: null, clip: null };
      V.current = cur;
      if (node) { node.classList.add('speaking'); voiceFollow(node); }
      voiceSavePos();
      // Cast at the playhead, not on arrival: a monster's first line can beat
      // the snapshot that first names it, and every moment the line waits its
      // turn is a moment that fetch has to land.
      var ctx = voiceCtx();
      cur.vkey = Speech.voiceKeyFor(ev, ctx.party, ctx.monsters);
      voiceSpeakChunk(cur);
      voiceRenderControls();
      return;
    }
    voiceSavePos();
    voiceRenderControls();
  }

  // Speak the chunk the playhead is on, in whichever engine is answering.
  // Both paths end at the same `voiceChunkDone`, so everything above here —
  // the playhead, the transport, the holds, the read marks — is untouched by
  // which one it was.
  function voiceSpeakChunk(cur) {
    // `cur.local` is set when a line has already fallen back: a line that
    // changes voice halfway through sounds like two people reading it.
    // A chunk over the server's cap would be refused, and a refusal counts
    // toward giving up on the server entirely — so it is not even asked. The
    // chunker caps at 220 and the server at 400, so this is only reachable on
    // a server configured tighter than its client.
    var over = cur.chunks[cur.idx].length > V.tts.maxChars;
    if (ttsOn() && !cur.local && !over) voiceSpeakServer(cur);
    else voiceSpeakLocal(cur, voiceProfile(cur.vkey));
  }

  // Let go of the audio element: its handlers are per chunk, and one left
  // bound fires against a line that has moved on.
  function audioDetach() {
    var a = V.audio;
    if (!a) return;
    a.onended = null;
    a.onerror = null;
    try { a.pause(); } catch (e) { /* ignore */ }
  }

  // Advance within the line, or finish it. Guards against a stale callback:
  // the utterance or clip that was in flight when the spectator skipped.
  function voiceChunkDone(cur, token) {
    if (V.current !== cur || cur.token !== token) return;
    clearTimeout(cur.timer);
    cur.idx++;
    if (cur.idx < cur.chunks.length) { voiceSpeakChunk(cur); return; }
    voiceFinishLine();
  }

  // ---- server voices: one clip, one <audio> element ----
  function voiceSpeakServer(cur) {
    var text = cur.chunks[cur.idx];
    var url = ttsUrl(cur.vkey, text);
    var token = cur.token = url + '#' + cur.idx;
    clipFetch(url).then(function (src) {
      if (V.current !== cur || cur.token !== token) return;   // skipped while it was in flight
      // Three failures RUNNING is what gives up on the server; a clip that
      // arrives clears the count, so three unlucky minutes an hour apart do not
      // add up to a verdict.
      V.tts.fails = 0;
      V.tts.reason = '';
      var audio = V.audio;
      var done = function () { voiceChunkDone(cur, token); };
      // A media failure can fire `error` AND reject the play() promise for the
      // same clip, so both paths carry the token and the handler ignores a
      // stale one. Without that the second arrival counts a second failure,
      // clears the fallback's watchdog and speaks the line twice.
      var failed = function () { voiceServerFailed(cur, null, token); };
      audio.onended = done;
      audio.onerror = failed;
      // The per-actor rate is baked into the clip's SSML; this is the
      // spectator's own slow/normal/fast, which must not cost a re-synthesis.
      try { audio.playbackRate = VOICE_RATES[V.settings.rate] || 1; } catch (e) { /* ignore */ }
      audio.src = src;
      // Same watchdog reasoning as the browser path: a stalled element that
      // never fires `ended` would park the playhead forever.
      cur.timer = setTimeout(done, Math.max(4000, text.length * 110) + 4000);
      var p = audio.play();
      if (p && p['catch']) p['catch'](failed);
      ttsPrefetch(cur);
    })['catch'](function (err) {
      voiceServerFailed(cur, err, token);
    });
  }

  // A line the server would not or could not say. Say it in this browser's own
  // voice rather than dropping it — a missed line is the one outcome the
  // playhead exists to prevent — and decide whether to keep asking.
  function voiceServerFailed(cur, err, token) {
    // Idempotent per attempt: the second report of one failure must not count
    // twice, nor restart a line the first report already handed to the browser.
    if (token !== undefined && (V.current !== cur || cur.token !== token)) return;
    audioDetach();
    if (cur) clearTimeout(cur.timer);
    var status = err && err.status;
    var settled = status === 402 || status === 404 || status === 503;
    if (settled) V.tts.fails = CLIP_FAILS;
    else V.tts.fails += 1;
    if (V.tts.fails >= CLIP_FAILS) {
      V.tts.degraded = true;
      V.tts.reason = (err && err.message) || 'server voices stopped answering';
    }
    if (V.current !== cur) { voiceRenderControls(); return; }
    cur.local = true;
    if (V.synth) {
      voiceSpeakLocal(cur, voiceProfile(cur.vkey));
    } else {
      // Nothing else here can speak. Stop rather than run the playhead
      // silently through the transcript.
      voicePausePlayback();
    }
    voiceRenderControls();
  }

  // Fetch what comes next while this plays: the rest of this line, or the
  // first chunk of the next line worth speaking.
  function ttsPrefetch(cur) {
    if (!ttsOn()) return;
    var url = null;
    if (cur.idx + 1 < cur.chunks.length) {
      url = ttsUrl(cur.vkey, cur.chunks[cur.idx + 1]);
    } else {
      var at = indexOfSeq(cur.ev.seq);
      var next = at < 0 ? null : S.events[voiceNextSpeakable(at + 1)];
      var phrase = next ? voicePhrase(next) : null;
      var chunks = phrase ? Speech.chunksFor(phrase) : [];
      if (chunks.length) {
        var ctx = voiceCtx();
        url = ttsUrl(Speech.voiceKeyFor(next, ctx.party, ctx.monsters), chunks[0]);
      }
    }
    // A prefetch that fails is not news: the real attempt will report it.
    if (url) clipFetch(url)['catch'](function () { /* ignore */ });
  }

  // ---- the browser's own voices ----
  function voiceSpeakLocal(cur, prof) {
    if (!V.synth) { voicePausePlayback(); return; }
    var rate = (VOICE_RATES[V.settings.rate] || 1) * (prof.rate || 1);
    var text = cur.chunks[cur.idx];
    var u = new SpeechSynthesisUtterance(text);
    if (prof.voice) { u.voice = prof.voice; u.lang = prof.voice.lang || u.lang; }
    u.pitch = prof.pitch || 1;
    u.rate = rate;
    cur.utt = u;   // hold the reference: an unreferenced utterance can be GC'd mid-speech and never fire onend
    var token = cur.token = 'utt#' + cur.idx;
    var done = function () {
      if (cur.utt !== u) return;                        // stale: skipped or cancelled
      voiceChunkDone(cur, token);
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
    // Hysteresis keys off what we have ASKED for, not off what the game has
    // confirmed: the badge may only claim a hold that exists, but the decision
    // to keep holding cannot wait a round trip.
    if (V.holdWanted ? behind > HOLD_LOW : behind >= HOLD_HIGH) voiceHoldSend(HOLD_LEASE);
    else voiceHoldRelease();
  }
  function voiceHoldRelease() {
    if (!V.holdWanted && !V.holding) return;
    voiceHoldSend(0);
  }
  function voiceHoldSend(seconds) {
    if (!S.gameId) return;
    var want = seconds > 0;
    var now = Date.now();
    // A renewal is a heartbeat, not a per-line event. voiceHoldEval runs on
    // every finished line, so without this the page would POST a lease for
    // each one instead of once a tick.
    if (want && V.holdWanted && (now - V.holdSentAt) < HOLD_TICK) return;
    V.holdWanted = want;
    V.holdSentAt = now;
    var id = S.gameId;
    api('/api/games/' + encodeURIComponent(id) + '/hold',
        { method: 'POST', body: { seconds: seconds, client: CLIENT_ID } })
      .then(function (r) {
        if (id !== S.gameId) return;
        // Only now may the badge say "holding": the game has taken the lease.
        // Saying it on the strength of a request merely sent is the UI
        // claiming a thing works before it knows that it does.
        V.holding = want && !!(r && r.holding > 0);
        voiceRenderControls();
      })
      .catch(function () {
        // 404/409/501: this game cannot be held (ended, restarted, or served by
        // another process). Stop asking; the narrator just runs behind.
        if (id !== S.gameId) return;
        V.holdBroken = true;
        V.holding = false;
        V.holdWanted = false;
        voiceRenderControls();
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
    V.holdWanted = false;
    V.holdSentAt = 0;
    V.holdBroken = false;
    // Clips are keyed by game id, so the old game's are dead weight; and a
    // refusal belonged to that game's budget, not to this one's.
    clipForget();
    V.tts.degraded = false;
    V.tts.fails = 0;
    V.tts.reason = '';
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
    // Both engines, whichever is going to speak. The <audio> element needs to
    // have played once inside a gesture before script may play it, and
    // speechSynthesis needs to have spoken once — and since the server path
    // falls back to the browser LINE BY LINE, arming only the one in use would
    // make that fallback silent on exactly the devices that need it.
    if (V.audio) {
      try {
        V.audio.src = SILENT_WAV;
        var q = V.audio.play();
        if (q && q['catch']) q['catch'](function () { /* nothing to hear anyway */ });
      } catch (e) { /* ignore */ }
    }
    if (V.synth) {
      voiceRefreshVoices();
      var server = ttsOn();
      var u = new SpeechSynthesisUtterance(server ? ' ' : 'Voice on.');
      // With server voices answering, the confirmation is the first real line
      // in a real voice; this utterance is only here to arm the fallback.
      if (server) u.volume = 0;
      var p = voiceProfile('dm');
      if (p.voice) { u.voice = p.voice; u.lang = p.voice.lang || u.lang; }
      V.heldUtt = u;
      u.onend = u.onerror = function () { V.heldUtt = null; };
      try { V.synth.speak(u); } catch (e) { /* ignore */ }
    }
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

    // Every other control here takes a disabled state; this one did not, so it
    // looked armed in every case where nothing is being held — voice off,
    // playback paused or backgrounded, and a game that cannot be held at all.
    // An option that always looks on cannot tell you whether it is doing
    // anything, which is the only thing this option is for.
    //
    // Two different things, kept apart: whether the option can apply to this
    // game AT ALL (no speech, an ended game, one another process is running)
    // and whether it is doing anything RIGHT NOW (voice off, paused, tab in
    // the background). The first disables it — there is no preference to hold
    // about a thing that cannot happen. The second only dims it: the
    // preference still means something the moment you press play, so it must
    // stay changeable while it is idle.
    var hold = $('voice-hold');
    var applies = V.supported && !V.holdBroken && !!S.gameId && !TERMINAL[S.status];
    var active = applies && V.settings.hold && voiceArmed();
    hold.checked = V.settings.hold;
    hold.disabled = !applies;
    hold.parentNode.classList.toggle('disabled', !applies);
    hold.parentNode.classList.toggle('idle', applies && !active);
    hold.parentNode.title =
      !V.supported ? 'This browser cannot speak, so there is nothing to keep step with.'
      : V.holdBroken ? 'This game cannot be held — it has ended, or another process is running it.'
      : !S.gameId || TERMINAL[S.status] ? 'This game has ended; nothing left to hold.'
      : !V.settings.hold ? 'Off: the game runs at its own pace and the narrator may fall behind.'
      : !V.settings.enabled ? 'Idle — turn voice on: with no narration there is nothing to hold the game for.'
      : !V.playing ? 'Idle while playback is paused; the game runs on.'
      : pageHidden() ? 'Idle while the tab is in the background; the game runs on.'
      : V.holding ? 'Holding the game now: the narrator is behind and the game is waiting.'
      : 'On: the game will wait when the narrator falls behind, so what you hear is what is happening.';

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

    var src = $('vt-src');
    if (!V.settings.enabled || !V.tts.checked) {
      src.hidden = true;
    } else {
      src.hidden = false;
      src.textContent = ttsOn()
        ? ('server voices' + (V.tts.engine ? ' · polly ' + V.tts.engine : ''))
        : ("this browser's own voices" + (V.tts.reason ? ' — ' + V.tts.reason : ''));
    }

    var now = voiceNowText();
    var el0 = $('vt-now');
    clear(el0);
    if (now.tag) el0.appendChild(el('span', 'vt-tag', now.tag));
    el0.appendChild(document.createTextNode(now.text || ''));

    var t = transcriptEl();
    t.classList.toggle('voice-clickable', on);
    t.classList.toggle('has-playhead', on && behind > 0);
  }

  function voiceShow() {
    $('voice').hidden = false;
    $('voice-panel').hidden = false;
  }

  // The first tap anywhere starts playback, because that is the only moment a
  // browser will let anything make a sound. Armed once, whichever engine ends
  // up speaking.
  function voiceArmGesture() {
    if (V.gestureArmed || !V.settings.enabled) return;
    V.gestureArmed = true;
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

  function voiceInit() {
    if (!Speech) {
      console.info('dnd-sim: speech.js missing; spoken narration hidden');
      return;
    }
    // Either engine on its own is enough to show the narration UI: a browser
    // with no speechSynthesis (some Linux builds, locked-down kiosks) still
    // plays audio, and a server with no Polly still has speechSynthesis.
    var synth = window.speechSynthesis;
    if (synth && typeof window.SpeechSynthesisUtterance === 'function') {
      V.synth = synth;
      V.supported = true;
      voiceRefreshVoices();
      if (typeof synth.addEventListener === 'function') synth.addEventListener('voiceschanged', voiceRefreshVoices);
      else synth.onvoiceschanged = voiceRefreshVoices;
    }
    try { V.audio = new Audio(); V.audio.preload = 'auto'; } catch (e) { V.audio = null; }
    voiceLoadSettings();
    if (V.supported) voiceShow();

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

    voiceArmGesture();

    window.addEventListener('pagehide', function () {
      voiceStopCurrent(false);
      voiceHoldStop();
    });
    voiceRenderControls();

    // Asked once, and late on purpose: everything above works without an
    // answer, and a browser that cannot speak for itself gets its narration
    // panel only when the answer is yes.
    ttsProbe().then(function (ok) {
      if (ok && V.audio && !V.supported) {
        V.supported = true;
        voiceShow();
        voiceArmGesture();
      }
      voiceRenderControls();
    });
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
    if (!canWrite()) { openUnlock(); return; }
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
    // always reset: a preset without a temperature must not inherit the last one's
    $('ng-temp').value = cfg.player_temperature !== undefined ? cfg.player_temperature : 1;
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
    cfg.player_temperature = Math.min(1, Math.max(0, num($('ng-temp').value, 1)));
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
    S.token = tokenLoad();
    // With a token in hand the probe is about to answer; rendering the locked
    // state first would flash "Unlock" at a browser that is already unlocked.
    if (!S.token) renderWriteAccess();
    $('btn-new').addEventListener('click', openNewGame);
    $('btn-unlock').addEventListener('click', openUnlock);
    $('unlock-form').addEventListener('submit', submitUnlock);
    $('ul-cancel').addEventListener('click', function () { $('unlock').hidden = true; });
    $('ul-forget').addEventListener('click', forgetToken);
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

    // The auth probe and the game list are independent; the only ordering that
    // matters is that the "no games yet" fallback must not open a panel that
    // would only 401.
    checkAuth().then(function () {
      return loadGames();
    }).then(function (id) {
      if (id) selectGame(id);
      else if (canWrite()) openNewGame();
      else ctlNote('No games yet.');
    }).catch(function (e) { console.warn(e); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
