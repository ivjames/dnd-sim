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
    lastSeq: -1,        // highest seq REVEALED — what the transcript has reached
    gotSeq: -1,         // highest seq RECEIVED — where a reconnect resumes from
    seen: {},
    taken: {},          // seqs the page has taken in, revealed or still queued
    events: [],         // every REVEALED event in arrival order; the playhead indexes it
    queue: [],          // received, not yet revealed — see "the reveal gate"
    revealing: false,   // re-entrancy guard: revealing an event pumps the narrator
    jumping: false,     // ... and while jumping to live, revealing must not START one
    endBanner: null,    // the stream has ended; the banner waits for the narrator
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
    snapGen: 0,         // ... and per snapshot request; a stale board must not land
    castToken: 0,       // bumped per cast preview; a stale answer must not land
    presets: [],
    // Write access. Reading a game, listing games and the stream are anonymous
    // and stay that way; creating a game, talking to the table and pause/
    // resume/stop carry a shared secret (web/auth.py). `writes` is what the
    // server says it takes, `token` is what this browser holds, `authed` is
    // whether the two agree.
    writes: 'unknown',  // 'unknown' | 'token' | 'unconfigured'
    token: '',
    authed: false,
    // Bumped by every probe and by Forget, so only the newest answer may land
    // — the same rule `loadGen` enforces for a game load, and for the same
    // reason: an answer to a question nobody is asking any more must not be
    // allowed to write state. Forget is the case that matters. It is
    // deliberately not disabled while a probe is in flight (it is the one
    // control you never want taken away), so it can land mid-probe, and the
    // callback would otherwise re-persist the very token that was just
    // cleared.
    authGen: 0
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
          var err = new Error((data && data.error) || ('HTTP ' + r.status));
          // The status, for the callers that have to tell a refusal apart from
          // a blip. The message cannot carry it — it is the server's own words
          // whenever there are any — and a rejection from `fetch` itself never
          // reaches this line, so an absent `status` means "no server answered"
          // rather than some code standing in for one.
          err.status = r.status;
          throw err;
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
    var gen = ++S.authGen;
    return api('/api/auth').then(function (a) {
      if (gen !== S.authGen) return null;   // superseded, or forgotten
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
    // Reachable in both states, because it is the only way into the panel and
    // the panel is the only way to Forget a token. Hiding it once unlocked
    // would leave a shared browser holding the credential with no way out
    // short of clearing site data. Hidden only where there is nothing to
    // enter, which is a server with no token set.
    var unlock = $('btn-unlock');
    unlock.hidden = unconfigured;
    unlock.textContent = ok ? 'Token' : 'Unlock';
    unlock.title = ok
      ? 'Forget the write token this browser is holding'
      : 'Enter the write token to create games and talk to the table';
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
    // Both halves of the credential, because a rejected replacement has to put
    // back the state it replaced and not merely the string. Typing a wrong
    // token while already unlocked is a normal slip now that the panel is
    // reachable from the unlocked state; restoring the token but not the flag
    // would take the write controls away from a browser that is still holding
    // a token the server accepts, and the only ways back would be resubmitting
    // or reloading.
    var previous = S.token;
    var wasAuthed = S.authed;
    var gen = ++S.authGen;
    S.token = value;
    var btn = $('ul-save');
    btn.disabled = true;
    // Checked against the server before it is kept, so a mistyped token is a
    // message here rather than a 401 on the next thing you try to do.
    api('/api/auth').then(function (a) {
      // Forget, or a newer submission, landed while this was in flight. Both
      // branches below write the credential — one stores the submitted token,
      // the other puts the previous one back — so a stale answer here would
      // undo an explicit Forget and leave the token in localStorage on a
      // browser whose user had just cleared it.
      if (gen !== S.authGen) return;
      S.writes = (a && a.writes) || 'unknown';
      S.authed = !!(a && a.authenticated);
      if (S.authed) {
        tokenStore(value);
        $('unlock').hidden = true;
      } else {
        S.token = previous;
        // As true as it was a moment ago. If the server has since rotated its
        // token, the next write is a 401 and `noteWriteRefusal` re-renders the
        // gate — which is the mechanism that exists for exactly that.
        S.authed = wasAuthed;
        $('ul-error').textContent = S.writes === 'unconfigured'
          ? 'this server has no write token set'
          : 'that token was not accepted';
        $('ul-error').hidden = false;
      }
      renderWriteAccess();
    }).catch(function (err) {
      if (gen !== S.authGen) return;
      // The probe failing says nothing about either token, so the previous
      // state stands whole and the gate is left as it was.
      S.token = previous;
      S.authed = wasAuthed;
      $('ul-error').textContent = err.message;
      $('ul-error').hidden = false;
      // The button is re-enabled below either way: a stale probe must not
      // leave Unlock dead for the next attempt.
    }).then(function () { btn.disabled = false; });
  }

  function forgetToken() {
    // First, so a probe already on the wire cannot write the token back.
    S.authGen++;
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
    S.taken = {}; S.gotSeq = -1;
    S.events = [];
    S.queue = [];
    S.endBanner = null;
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
      // Never downward. A cost line is revealed at the narrator's pace like
      // everything else, so by the time it lands the 5 s poll may already have
      // shown a later total — and a meter that ran backwards would be saying
      // the game had spent less than it has. Money is a live fact; this event
      // only ever brings it forward between polls.
      if (d.total_usd !== undefined) setCost(Math.max(num(d.total_usd), S.cost), null);
    }
    var node = null;   // the transcript node for this event (voice highlights it)
    if (ev.kind === 'turn_start') {
      S.activeId = ev.actor;
      node = startTurnGroup(ev);
    } else if (ev.kind === 'turn_end') {
      S.group = null; S.groupBody = null;
    } else if (ev.kind === 'narration') {
      // Close the turn's mechanics group. The orchestrator holds a turn's
      // `down`/`dead` back until after its narration, so the line that lands
      // the beat arrives AFTER this paragraph — and the group node sits ABOVE
      // it, so leaving the group open would file that line back up inside it:
      // out of order on screen, and out of order for the playhead, which
      // scrolls to whichever node carries the seq it is speaking.
      S.group = null; S.groupBody = null;
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
    scoreOnEvent(ev);
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

  // ---------- the reveal gate ----------
  // Events arrive far faster than a voice reads them, so with the narrator
  // running they land HERE first and reach the screen — transcript, map, hit
  // points, whose turn it is — only as the narrator begins the line that
  // describes them. Nothing is dropped, nothing is reordered, nothing is
  // summarised: the queue is a delay and only a delay, and the game going on
  // without us is what it is for.
  //
  // Why a run of events at a time rather than one as it is spoken: a turn's
  // mechanics are emitted BEFORE the paragraph that describes them (the
  // orchestrator resolves the turn, then asks the DM for the prose), and with
  // "mute mechanics" on they are never spoken at all. Revealing each as it
  // arrived would move the pieces and drop the hit points before a word of it
  // had been said. So the queue is read up to and including the next line that
  // WILL be spoken, and that whole run is revealed in one beat — the beat that
  // line starts in.
  //
  // With voice off, before the first tap, or while a game's history is being
  // replayed there is nothing to keep step with, and everything is revealed as
  // it arrives, which is what this page did before the gate existed.
  function revealGated() {
    return V.supported && V.settings.enabled && V.unlocked && !V.loading;
  }

  // One event off the queue and onto the screen. `renderEvent` pumps the
  // narrator on its way out, which is what starts the line this run was
  // revealed for.
  function revealOne() {
    var ev = S.queue.shift();
    if (ev) renderEvent(ev);
  }

  // Queued events with nothing spoken after them are held: the paragraph that
  // describes them is the next thing the DM writes. They are let go only when
  // that paragraph can never arrive — the game has ended and the narrator has
  // read everything already revealed.
  function revealFlushable() {
    return TERMINAL[S.status] && !V.current && V.cursor >= S.events.length;
  }

  function revealPump() {
    if (S.revealing) return;   // renderEvent → voiceOnEvent → voicePump → here
    S.revealing = true;
    try {
      while (S.queue.length) {
        if (!revealGated()) { revealOne(); continue; }
        // The run up to and including the next line that will be spoken.
        // Zero means the queue holds nothing anyone is going to read yet —
        // mechanics waiting on the paragraph that describes them.
        var run = Speech.revealRun(S.queue, V.settings);
        if (!run) {
          if (!revealFlushable()) break;
          revealOne();
          continue;
        }
        // Only when the narrator is free to begin that line in this beat: it
        // is mid-line, it is paused or backgrounded, or it is still working
        // through lines that were revealed before these.
        if (V.current || !voiceArmed() || V.cursor < S.events.length) break;
        for (var i = 0; i < run; i++) revealOne();
        voicePump();
        // Nothing to hear after all — `shouldSpeak` admits a kind, `phraseFor`
        // decides there are no words for this one (a hit that its damage line
        // will voice, a menu). Keep going rather than waiting for a line that
        // will never be read.
        if (V.current) break;
      }
      revealTail();
      // The badge counts the queue, and a queue that is filling while the
      // narrator is paused moves nothing else on the page — so this is the
      // only thing left to say how far behind the transcript has fallen.
      voiceRenderControls();
    } finally {
      S.revealing = false;
    }
  }

  // The lot, now: the spectator asked to be at the live edge, or the gate has
  // just come off under a full queue.
  function revealAll() {
    if (S.revealing) return;
    S.revealing = true;
    try {
      while (S.queue.length) revealOne();
      revealTail();
    } finally {
      S.revealing = false;
    }
  }

  // "session finished" is the end of the story, so it waits for the story: the
  // stream's `end` frame lands while the narrator may still have minutes of
  // transcript to read.
  function revealTail() {
    if (!S.endBanner || S.queue.length) return;
    if (revealGated() && (V.current || V.cursor < S.events.length)) return;
    var text = S.endBanner;
    S.endBanner = null;
    appendNode(el('div', 'end-banner', text));
  }

  // Everything the stream and the history load hand this page comes through
  // here. Deduplicated on arrival rather than on render, because a reconnect
  // resumes from the last seq RECEIVED — which, with a queue, is ahead of the
  // last one on screen.
  function ingest(ev) {
    if (!ev) return;
    var seq = num(ev.seq, -1);
    if (S.taken[seq]) return;
    S.taken[seq] = 1;
    if (seq > S.gotSeq) S.gotSeq = seq;
    S.queue.push(ev);
    revealPump();
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
    // The board is asked for AS OF the last line put on screen, never as of
    // now. The queue above can delay a paragraph, but the map and the hit
    // points come from the server's own state, which is wherever the game has
    // got to — so without `at_seq` the one thing that would still run ahead of
    // the voice is the picture of the fight. `snapshot_seq` in the reply says
    // which seq answered; status, ledger and cost stay live either way.
    var url = '/api/games/' + encodeURIComponent(S.gameId);
    if (revealGated() && S.lastSeq >= 0) url += '?at_seq=' + S.lastSeq;
    // Only the newest answer may land, the same rule `loadGen` enforces for a
    // game load. Four callers ask for this — the 700 ms debounce, an encounter
    // spawn, the 5 s poll, and every control — each pinning a different
    // `at_seq`, and they overtake each other: an older answer landing last
    // would walk the hit points, the positions and the initiative BACKWARDS on
    // screen, and a pinned answer landing after voice was turned off would put
    // the delayed board back over the live one.
    var gen = ++S.snapGen;
    var forGame = S.gameId;
    return api(url).then(function (g) {
      if (gen !== S.snapGen || forGame !== S.gameId) return;
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
    renderTable();
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

  function hpClass(frac) { return frac > 0.5 ? '' : (frac > 0.25 ? 'hurt' : 'bad'); }

  // Two shapes, one card. The party card is two lines (name + AC over bar +
  // hit points); an enemy is one, because five bandits stacked four lines deep
  // pushed everything under them off the screen. Same nodes either way, so the
  // stylesheet — not this function — decides how tight it sits.
  //
  // `score` is the initiative roll, printed as the card's first column when
  // there is one: the roster is one list in initiative order, so which side a
  // combatant is on is said by the stripe down its edge rather than by which
  // panel it sits in.
  function card(c, id, compact, score, active) {
    var node = el('div', 'card' + (compact ? ' card-tight' : ''));
    node.className += ' side-' + (c.side || 'neutral');
    if (active) node.className += ' is-active';
    var hp = num(c.hp), max = Math.max(1, num(c.max_hp, 1));
    if (c.dead || hp <= 0) node.className += ' is-down';

    var sub = [];
    if (c.ac !== undefined) sub.push('AC ' + num(c.ac));
    if (c.sheet && c.sheet.klass) sub.push(c.sheet.klass + ' ' + num(c.sheet.level));
    // Who the character is sits on the same line as what they can do, because
    // that is the line the card uses for identity. Monsters carry no sheet and
    // so say nothing here, which is the same as before.
    if (c.sheet && c.sheet.pronouns) sub.push(c.sheet.pronouns);
    var name = el('span', 'card-name', c.name || id);
    var subEl = el('span', 'card-sub', sub.join(' · '));
    var init = score === undefined || score === null
      ? null : el('span', 'card-init', String(score));

    // Which side, in words as well as in the colour of the stripe. One list
    // means the stripe is the only thing left saying it, and a stripe says
    // nothing to a screen reader and little to a reader who cannot separate
    // those two browns: the short form is what a sighted reader scans, the
    // hidden one is what gets read out.
    var sideWord = c.side === 'party' ? 'party' : (c.side === 'enemy' ? 'enemy' : 'neutral');
    var sideShort = c.side === 'party' ? 'pc' : (c.side === 'enemy' ? 'foe' : 'npc');
    var sideEl = el('span', 'card-side');
    var sideMark = el('span', '', sideShort);
    sideMark.setAttribute('aria-hidden', 'true');
    sideEl.appendChild(sideMark);
    sideEl.appendChild(el('span', 'sr-only', sideWord + '. '));

    if (compact) {
      if (init) node.appendChild(init);
      node.appendChild(sideEl);
      node.appendChild(name);
    } else {
      var top = el('div', 'card-top');
      if (init) top.appendChild(init);
      top.appendChild(sideEl);
      top.appendChild(name);
      top.appendChild(subEl);
      node.appendChild(top);
    }

    var frac = Math.max(0, Math.min(1, hp / max));
    var vitals = el('div', 'card-vitals');
    var bar = el('div', 'hpbar ' + hpClass(frac));
    var fill = el('i'); fill.style.width = (frac * 100).toFixed(0) + '%';
    bar.appendChild(fill);
    vitals.appendChild(bar);

    var hpText = hp + '/' + num(c.max_hp);
    if (num(c.temp_hp) > 0) hpText += ' (+' + num(c.temp_hp) + ' temp)';
    if (hp <= 0 && !c.dead) {
      var ds = c.death_saves || {};
      hpText += ' — down ' + num(ds.success) + '✓/' + num(ds.failure) + '✗';
    }
    if (c.dead) hpText += ' — dead';
    vitals.appendChild(el('span', 'hpnum', hpText));
    node.appendChild(vitals);
    // Only where there is something to say: an empty span still takes the
    // row's flex gap, which on a one-line card is a visible notch.
    if (compact && sub.length) node.appendChild(subEl);

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

  // Whose turn it is, or nothing. An initiative order OUTLIVES the fight it was
  // rolled for — `combat_end` sets the mode back to exploration and leaves the
  // order in the state — so both the order and the turn pointer are only ever
  // read while the mode says combat. Read them after, and the roster spends the
  // next scene sorted by a finished fight with a ring around whoever happened
  // to be acting when it ended.
  function inCombat() {
    var st = gameState();
    return st.mode === 'combat' && (st.initiative || []).length > 0;
  }

  function activeCombatantId() {
    if (!inCombat()) return null;
    var st = gameState();
    var init = st.initiative || [];
    if (typeof st.turn_index === 'number' && init.length) {
      var cur = init[st.turn_index % init.length];
      if (cur) return Array.isArray(cur) ? cur[0] : (cur.id || null);
    }
    return st.active_id || S.activeId || null;
  }

  // The table: everyone at it, in initiative order, in one list. Three panels
  // of the same people sorted three ways is what this replaces — the order is
  // the thing you read a fight in, and a combatant's side, hit points and
  // conditions belong on the row you are already looking at.
  //
  // Out of combat there is no order to keep, so it is the party and then
  // whoever else is on the board, which is the order the game builds them in.
  function renderTable() {
    var box = $('roster');
    clear(box);
    var cs = combatants();
    var init = inCombat() ? (gameState().initiative || []) : [];
    var activeId = activeCombatantId();

    // In initiative order where there is one, and anyone the order has not
    // heard of (a monster that walked in mid-round) after it, never dropped.
    var seen = {}, rows = [];
    init.forEach(function (row) {
      var id = Array.isArray(row) ? row[0] : (row && row.id);
      if (!id || seen[id] || !cs[id]) return;
      seen[id] = 1;
      rows.push({ id: id, score: Array.isArray(row) ? row[1] : (row && row.score) });
    });
    var rest = Object.keys(cs).filter(function (id) { return !seen[id]; });
    rest.sort(function (a, b) {
      var sa = (cs[a] || {}).side === 'party' ? 0 : 1;
      var sb = (cs[b] || {}).side === 'party' ? 0 : 1;
      return sa - sb;
    });
    rest.forEach(function (id) { rows.push({ id: id, score: undefined }); });

    if (!rows.length) {
      box.appendChild(el('p', 'empty', S.gameId ? 'Party not built yet.' : 'No game loaded.'));
      return;
    }
    rows.forEach(function (r) {
      var c = cs[r.id] || {};
      // A player character carries a sheet worth two lines; everything else is
      // a name, a bar and an AC, and says it on one.
      box.appendChild(card(c, r.id, c.side !== 'party', r.score, r.id === activeId));
    });
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

  // The map is the one thing on the page the stylesheet cannot paint, so it
  // reads the same custom properties everything else does. One lookup per
  // draw, and drawGrid() runs again on a theme change.
  function themePalette() {
    var cs = window.getComputedStyle(document.documentElement);
    function tok(name, fallback) {
      var v = cs.getPropertyValue(name);
      v = v ? v.trim() : '';
      return v || fallback;
    }
    return {
      ground: tok('--bg-sunken', '#241c15'),
      grid: tok('--canvas-grid', '#3d2f24'),
      wall: tok('--wall', '#6b5b4b'),
      difficult: tok('--difficult', '#4e3e24'),
      accent: tok('--accent', '#f0b75e'),
      party: tok('--party', '#8fc9ea'),
      enemy: tok('--enemy', '#f2a794'),
      neutral: tok('--neutral', '#dfc189'),
      danger: tok('--danger', '#f3a392'),
      // Token fills are light on dark and dark on light, so the ground colour
      // is the label ink that stays legible in both.
      onToken: tok('--bg-sunken', '#241c15')
    };
  }

  function drawGrid() {
    var canvas = $('grid');
    var pal = themePalette();
    var st = gameState();
    var grid = st.grid || {};
    var w = Math.max(1, num(grid.width, 12)), h = Math.max(1, num(grid.height, 10));

    // The map lives in a fixed-height quadrant, so the square has to fit the
    // height it has as well as the width — sizing on width alone is what let a
    // 10-row grid run out of the bottom of the panel. Both dimensions are then
    // written back as CSS pixels: the canvas is no longer stretched to the
    // panel by the stylesheet, so nothing else knows how big it should be.
    var wrap = canvas.parentElement;
    var cssW = (wrap && wrap.clientWidth) || canvas.clientWidth || 480;
    var availH = (wrap && wrap.clientHeight) || 0;
    var cell = Math.floor(cssW / w);
    if (availH > 0) cell = Math.min(cell, Math.floor(availH / h));
    // The floor keeps a big grid readable (the panel scrolls instead); the
    // ceiling stops a small one growing to an inch a square.
    cell = Math.max(10, Math.min(48, cell));
    var cssH = cell * h;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(cell * w * dpr);
    canvas.height = Math.floor(cssH * dpr);
    canvas.style.width = (cell * w) + 'px';
    canvas.style.height = cssH + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cell * w, cssH);

    ctx.fillStyle = pal.ground;
    ctx.fillRect(0, 0, cell * w, cssH);

    var difficult = coordSet(grid.difficult);
    var walls = coordSet(grid.walls);
    var cover = grid.cover || {};

    var x, y, key;
    for (y = 0; y < h; y++) {
      for (x = 0; x < w; x++) {
        key = x + ',' + y;
        if (walls[key]) ctx.fillStyle = pal.wall;
        else if (difficult[key]) ctx.fillStyle = pal.difficult;
        else continue;
        ctx.fillRect(x * cell, y * cell, cell, cell);
      }
    }
    Object.keys(cover).forEach(function (k) {
      var parts = k.replace(/[()\s]/g, '').split(',');
      var cx = num(parts[0]), cy = num(parts[1]);
      ctx.strokeStyle = pal.accent;
      ctx.globalAlpha = 0.55;
      ctx.lineWidth = 2;
      ctx.strokeRect(cx * cell + 2, cy * cell + 2, cell - 4, cell - 4);
      ctx.globalAlpha = 1;
    });

    ctx.strokeStyle = pal.grid;
    ctx.lineWidth = 1;
    for (x = 0; x <= w; x++) {
      ctx.beginPath(); ctx.moveTo(x * cell + .5, 0); ctx.lineTo(x * cell + .5, cssH); ctx.stroke();
    }
    for (y = 0; y <= h; y++) {
      ctx.beginPath(); ctx.moveTo(0, y * cell + .5); ctx.lineTo(cell * w, y * cell + .5); ctx.stroke();
    }

    var cs = combatants();
    var activeId = activeCombatantId();
    Object.keys(cs).forEach(function (id) {
      var c = cs[id] || {};
      var pos = c.position;
      if (!pos || pos.length < 2) return;
      var px = num(pos[0]) * cell + cell / 2, py = num(pos[1]) * cell + cell / 2;
      var r = Math.max(5, cell * 0.36);
      var dead = c.dead || num(c.hp) <= 0;
      var color = c.side === 'party' ? pal.party : (c.side === 'enemy' ? pal.enemy : pal.neutral);
      ctx.globalAlpha = dead ? 0.35 : 1;
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      if (id === activeId) {
        ctx.strokeStyle = pal.accent; ctx.lineWidth = 2.5; ctx.stroke();
      }
      ctx.globalAlpha = 1;
      var label = (c.name || id).replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase();
      ctx.fillStyle = pal.onToken;
      ctx.font = 'bold ' + Math.max(8, Math.floor(r)) + 'px ui-monospace, Menlo, monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(label, px, py + 0.5);
      if (dead) {
        ctx.strokeStyle = pal.danger; ctx.lineWidth = 2;
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
  // Rescaled 2026-09-04: what used to be `fast` (1.2) is the comfortable
  // middle for listening to a whole game, so it is `normal` now and the
  // other two keep their old ratios to it (x0.85 and x1.2).
  var VOICE_RATES = { slow: 1.0, normal: 1.2, fast: 1.45 };
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
  var HOLD_RETRY_MAX = 60000;   // ceiling on the back-off after a failed hold call
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
    holdBroken: false,     // this game will not take a hold (ended, or another process)
    holdFails: 0,          // consecutive failed hold calls, for the back-off
    holdRetryAt: 0         // ... and the time before which we do not ask again
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

  // What the event is read as: `[{key, text}]`, utterance-sized and each piece
  // carrying the voice that says it. Most events are one voice throughout; a
  // line of dialogue that names its speaker is two, the narrator saying who
  // and then the speaker saying the words. Cast here rather than on arrival —
  // a monster's first line can beat the snapshot that first names it, so the
  // later this is asked the better the answer.
  function voiceChunks(ev) {
    if (!ev || !Speech.shouldSpeak(ev, V.settings)) return [];
    var ctx = voiceCtx();
    var out = [];
    Speech.segmentsFor(ev, ctx.names, ctx.party, ctx.monsters).forEach(function (seg) {
      Speech.chunksFor(seg.text).forEach(function (text) {
        out.push({ key: seg.key, text: text });
      });
    });
    return out;
  }

  // Unread speakable lines ahead of the playhead, counted up to BACKLOG_SCAN.
  // shouldSpeak alone (not phraseFor) — this runs on every event, and the
  // handful of lines that turn out to have nothing to say do not change any
  // decision made from the count.
  function voiceBacklog() {
    var n = 0, i;
    for (i = V.cursor; i < S.events.length && n < BACKLOG_SCAN; i++) {
      if (Speech.shouldSpeak(S.events[i], V.settings)) n++;
    }
    // Behind the revealed transcript sits the queue, which is the rest of what
    // is waiting to be read. It has to count here or it would never be counted
    // at all: the hold is what stops that queue growing, and a hold driven by
    // the visible part alone would let the game run as far ahead as it liked
    // as long as the screen stayed tidy.
    for (i = 0; i < S.queue.length && n < BACKLOG_SCAN; i++) {
      if (Speech.shouldSpeak(S.queue[i], V.settings)) n++;
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
    revealPump();     // the gate is closed while paused; this is what opens it
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
  function voiceRepump() { setTimeout(function () { voicePump(); revealPump(); }, 80); }

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
    // `revealAll` renders, and rendering pumps the narrator — which, with the
    // playhead still back where it was, would start reading the very backlog
    // this button exists to skip, and then run the cursor off the end of the
    // transcript when that line finished. So the pump is held off until the
    // playhead has been moved to the edge the reveal creates.
    S.jumping = true;
    try {
      voiceStopCurrent(false);
      V.resumeChunk = null;
      revealAll();    // the queue is the rest of "live": being at the live edge means seeing it
      V.cursor = S.events.length;
    } finally {
      S.jumping = false;
    }
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
    if (V.current || !voiceArmed() || S.jumping) { voiceRenderControls(); return; }
    while (V.cursor < S.events.length) {
      var ev = S.events[V.cursor];
      var chunks = voiceChunks(ev);
      if (!chunks.length) { V.cursor++; continue; }
      var idx = 0;
      if (V.resumeChunk && V.resumeChunk.seq === ev.seq && V.resumeChunk.idx < chunks.length) {
        idx = V.resumeChunk.idx;
      }
      V.resumeChunk = null;
      var node = transcriptEl().querySelector('[data-seq="' + ev.seq + '"]');
      var cur = { ev: ev, chunks: chunks, idx: idx, node: node,
                  timer: null, utt: null, clip: null };
      V.current = cur;
      if (node) { node.classList.add('speaking'); voiceFollow(node); }
      voiceSavePos();
      voiceStartLine(cur);
      voiceRenderControls();
      return;
    }
    voiceSavePos();
    voiceRenderControls();
  }

  // The chunk the playhead is on, and the voice that says it. The voice is
  // per chunk rather than per line because an attributed line changes speaker
  // partway through: the narrator names the monster, the monster answers.
  function chunkText(cur) { return cur.chunks[cur.idx].text; }
  function chunkKey(cur) { return cur.chunks[cur.idx].key; }

  // How many voices are left in this line from the playhead on. Two means an
  // attributed line: the narrator naming the speaker, then the speaker.
  function voicesLeft(cur) {
    var keys = {};
    for (var i = cur.idx; i < cur.chunks.length; i++) keys[cur.chunks[i].key] = 1;
    return Object.keys(keys).length;
  }

  // Begin a line — or resume it, which is the same question asked from a later
  // chunk. A line that is more than one voice is settled onto ONE engine before
  // any of it is heard: its clips are asked for together, and a refusal of any
  // takes the whole line to the browser's voices. `cur.local` alone cannot do
  // that, because it is set by a failure that has already happened — a name
  // clip that is cached (they nearly all are, the same name every time) plays
  // through Polly for free, and the words behind it can still be refused. A
  // game running out of budget mid-scene is exactly when that happens, and it
  // arrives as one line in two engines: the narrator on Polly, the goblin on
  // the device.
  //
  // A single-voice line still starts on its first chunk and fetches the rest
  // as it plays. That is what makes a long narration start promptly, and one
  // speaker crossing engines between two chunks is a seam nobody can hear.
  function voiceStartLine(cur) {
    if (!ttsOn() || cur.local || voicesLeft(cur) < 2) { voiceSpeakChunk(cur); return; }
    var rest = cur.chunks.slice(cur.idx);
    // Over the server's cap is not a refusal — it is never asked — so it costs
    // the line its server voices without counting toward giving up on them.
    if (rest.some(function (c) { return c.text.length > V.tts.maxChars; })) {
      cur.local = true;
      voiceSpeakChunk(cur);
      return;
    }
    var token = cur.token = 'line#' + cur.ev.seq + '#' + cur.idx;
    Promise.all(rest.map(function (c) { return clipFetch(ttsUrl(c.key, c.text)); }))
      .then(function () {
        if (V.current !== cur || cur.token !== token) return;   // skipped while in flight
        voiceSpeakChunk(cur);      // every clip is in hand; each is a cache hit now
      })['catch'](function (err) {
        voiceServerFailed(cur, err, token);
      });
  }

  // Speak the chunk the playhead is on, in whichever engine is answering.
  // Both paths end at the same `voiceChunkDone`, so everything above here —
  // the playhead, the transport, the holds, the read marks — is untouched by
  // which one it was.
  function voiceSpeakChunk(cur) {
    // `cur.local` is set when a line has already fallen back, and by
    // `voiceStartLine` before an attributed line starts at all: a line that
    // changes engine halfway through sounds like two people reading it.
    // A chunk over the server's cap would be refused, and a refusal counts
    // toward giving up on the server entirely — so it is not even asked. The
    // chunker caps at 220 and the server at 400, so this is only reachable on
    // a server configured tighter than its client.
    var over = chunkText(cur).length > V.tts.maxChars;
    if (ttsOn() && !cur.local && !over) voiceSpeakServer(cur);
    else voiceSpeakLocal(cur, voiceProfile(chunkKey(cur)));
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
    var text = chunkText(cur);
    var url = ttsUrl(chunkKey(cur), text);
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
      voiceSpeakLocal(cur, voiceProfile(chunkKey(cur)));
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
    var ahead = cur.chunks[cur.idx + 1];
    if (!ahead) {
      var at = indexOfSeq(cur.ev.seq);
      var next = at < 0 ? null : S.events[voiceNextSpeakable(at + 1)];
      ahead = next ? voiceChunks(next)[0] : null;
    }
    if (ahead) url = ttsUrl(ahead.key, ahead.text);
    // A prefetch that fails is not news: the real attempt will report it.
    if (url) clipFetch(url)['catch'](function () { /* ignore */ });
  }

  // ---- the browser's own voices ----
  function voiceSpeakLocal(cur, prof) {
    if (!V.synth) { voicePausePlayback(); return; }
    var rate = (VOICE_RATES[V.settings.rate] || 1) * (prof.rate || 1);
    var text = chunkText(cur);
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
    revealPump();     // ... and with the narrator free, the next beat may land
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
  // The three statuses that mean this game will never take a hold, whatever we
  // do: no such game (404), running in another process (409), a game object
  // without the method (501) — see `hold()` in web/routes/api.py. Nothing else
  // is an answer about the game.
  var HOLD_REFUSED = { 404: 1, 409: 1, 501: 1 };

  function voiceHoldSend(seconds) {
    if (!S.gameId) return;
    var want = seconds > 0;
    var now = Date.now();
    // A renewal is a heartbeat, not a per-line event. voiceHoldEval runs on
    // every finished line, so without this the page would POST a lease for
    // each one instead of once a tick.
    if (want && V.holdWanted && (now - V.holdSentAt) < HOLD_TICK) return;
    // Backing off after a failure the server did not explain (see below). A
    // release is never held back: letting a game go is the one call that must
    // not wait on the reason the last one failed.
    if (want && now < V.holdRetryAt) return;
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
        V.holdFails = 0;
        V.holdRetryAt = 0;
        voiceRenderControls();
      })
      .catch(function (err) {
        if (id !== S.gameId) return;
        V.holding = false;
        V.holdWanted = false;
        if (HOLD_REFUSED[err && err.status]) {
          // The server said this game cannot be held. Stop asking; the
          // narrator just runs behind.
          V.holdBroken = true;
        } else {
          // Anything else — a dropped connection, a 5xx, nginx restarting
          // under a deploy — is the wire failing, not the game answering, and
          // latching on it strands the narrator arbitrarily far behind for the
          // rest of the page's life over one blip. So keep asking; just not at
          // full rate forever. Back off a tick per consecutive failure, capped
          // at HOLD_RETRY_MAX, so a blip costs one tick and a wedged endpoint
          // is asked once a minute. A success clears it.
          V.holdFails += 1;
          V.holdRetryAt = Date.now() +
            Math.min(V.holdFails * HOLD_TICK, HOLD_RETRY_MAX);
        }
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
    V.holdFails = 0;
    V.holdRetryAt = 0;
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
    revealPump();        // from here on, live events wait for the narrator
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
    // A queue whose closing paragraph never came can be let go now — nothing
    // further is coming to describe it.
    revealPump();
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
    // Nothing left to keep step with, so nothing left to hold back: whatever
    // was waiting on a line that will now never be read goes up at once.
    revealAll();
    refreshSnapshot();   // ... and the board catches up with it
  }

  // ---- controls ----
  // Deliberately not the words. The transcript now reveals a line as the
  // narrator begins it and marks it `speaking`, so printing the text here as
  // well would be the same sentence twice on one screen — and for the line the
  // playhead is parked on it was worse than redundant: it printed a line that
  // had not been read yet, which is the one thing this panel must not do.
  // What is left is what the transcript cannot say: that something is being
  // read, what kind of line it is, and in whose voice.
  function voiceNowText() {
    if (!V.settings.enabled) return { tag: '', text: 'voice off' };
    if (!V.unlocked) return { tag: '', text: 'press play to start — browsers only allow speech from a tap' };
    if (V.current) {
      // No tag on a narration: the panel is already titled NARRATION, and the
      // word twice across two inches of the same bar is the thing this panel
      // was just cured of.
      var kind = V.current.ev.kind;
      return { tag: kind === 'narration' ? '' : kind.replace('_', ' '),
               text: 'read by ' + voiceReaderName(V.current.ev) };
    }
    if (!V.playing) {
      return (S.queue.length || V.cursor < S.events.length)
        ? { tag: 'paused', text: 'the transcript is waiting here too' }
        : { tag: '', text: 'paused — nothing to read yet' };
    }
    if (pageHidden()) return { tag: '', text: 'held while the tab is in the background' };
    return { tag: '', text: 'caught up — waiting for the table' };
  }

  // Whose voice is on. A line of dialogue is its speaker's; everything else is
  // the DM, which is what the narrator is.
  function voiceReaderName(ev) {
    if (ev && ev.kind === 'dialogue') {
      var who = (ev.data && ev.data.speaker) || nameOf(ev.actor) || ev.actor;
      if (who) return String(who);
    }
    return 'the DM';
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

    // The score ducks under whatever is being read. Not a rendering concern,
    // but every transition the narrator makes ends up here, which makes this
    // the one place that cannot miss one.
    scoreDuckEval();
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

  // ---------- the score ----------
  // Music, stings and swells under the game. `tools/audio` picks them and
  // writes `audio/manifest.json` carrying each cue's match rule beside its
  // file (AUDIO.md); `/api/audio` serves that; `cues.js` turns an event into
  // the cues it fires; and this is the mixer.
  //
  // Nothing here names a cue or an event kind. What plays is whatever the pack
  // assigns to the rule an event matched, so re-picking the audio changes what
  // the game sounds like without changing a line of this file — which is the
  // whole reason the manifest carries the rules rather than the page.
  //
  // Two layers, because that is what the cue table is. BEDS (music, ambience)
  // are the slot: one plays at a time and a new one crossfades over it, which
  // is why `combat_start` can swap the bed and hit a sting in the same beat
  // without the two fighting for the same channel. Everything else is a
  // one-shot laid on top.
  //
  // The bed DUCKS while a line is being read. A spectator is here for the
  // words; the score is the room they are said in, and a bed that competes
  // with the narrator is worse than no bed at all.
  //
  // Cues fire from `renderEvent` — from the REVEAL, not from arrival. With the
  // narrator running that is the beat the line lands in, so a sting is heard
  // with the sentence it belongs to rather than minutes ahead of it while the
  // queue drains behind the gate.
  var Cues = window.DndCues || null;
  var SCORE_VOLUMES = { quiet: 0.3, normal: 0.6, loud: 1 };
  var SCORE_DUCK = 0.3;        // what the bed drops to under a spoken line
  var SCORE_RAMP_MS = 250;     // duck and unduck; beds fade at the pack's rate
  var SCORE_SHOTS = 3;         // one-shots at once — over that, the oldest goes
  var A = {
    ready: false,           // a pack with at least one playable cue answered
    unlocked: false,        // a user gesture has started audio on this page
    gestureArmed: false,
    settings: { enabled: false, volume: 'normal' },
    cues: [], base: '/audio/', digest: '', reason: '',
    ctx: null, ctxOff: false,   // ctxOff: no Web Audio here; element volume it is
    master: null, bedBus: null, shotBus: null,
    bed: null,              // the bed in play: { cue, el, gain, node, stopping }
    shots: [],              // one-shots in flight, oldest first
    ducked: false
  };

  function scoreArmed() {
    return A.ready && A.settings.enabled && A.unlocked && !pageHidden();
  }

  function scoreLoadSettings() {
    try {
      var o = JSON.parse(localStorage.getItem('dndsim.sound') || 'null');
      if (o && typeof o === 'object') {
        A.settings.enabled = !!o.enabled;
        A.settings.volume = SCORE_VOLUMES[o.volume] ? o.volume : 'normal';
      }
    } catch (e) { /* private mode or junk */ }
  }
  function scoreSaveSettings() {
    try { localStorage.setItem('dndsim.sound', JSON.stringify(A.settings)); } catch (e) { /* ignore */ }
  }

  function scoreVolume() { return SCORE_VOLUMES[A.settings.volume] || SCORE_VOLUMES.normal; }

  // Web Audio where there is any: a gain node per layer is what makes ducking
  // and crossfades a ramp rather than a stepped `volume`. Where there is none
  // (an old browser, a locked-down one), every level below falls back to the
  // element's own `volume`, which cannot ramp — so the score still plays, just
  // without the fades. Built lazily and inside the unlocking gesture: a
  // context created before one is born suspended, and on iOS stays that way.
  function scoreCtx() {
    if (A.ctx || A.ctxOff) return A.ctx;
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) { A.ctxOff = true; return null; }
    try {
      var ctx = new Ctor();
      A.master = ctx.createGain();
      A.bedBus = ctx.createGain();
      A.shotBus = ctx.createGain();
      A.bedBus.connect(A.master);
      A.shotBus.connect(A.master);
      A.master.connect(ctx.destination);
      A.ctx = ctx;
      scoreApplyLevels(0);
    } catch (e) {
      A.ctxOff = true;
      A.ctx = null;
    }
    return A.ctx;
  }

  function scoreRamp(param, to, ms) {
    var now = A.ctx.currentTime;
    try {
      param.cancelScheduledValues(now);
      param.setValueAtTime(param.value, now);
      if (ms > 0) param.linearRampToValueAtTime(to, now + ms / 1000);
      else param.setValueAtTime(to, now);
    } catch (e) { param.value = to; }
  }

  // The user's volume, and the duck, applied wherever they live in this
  // browser: on the two buses with Web Audio, on each element without it.
  function scoreApplyLevels(ms) {
    var duck = A.ducked ? SCORE_DUCK : 1;
    if (A.ctx) {
      scoreRamp(A.master.gain, scoreVolume(), ms);
      scoreRamp(A.bedBus.gain, duck, ms);
      return;
    }
    if (A.bed) scoreElementLevel(A.bed);
    A.shots.forEach(scoreElementLevel);
  }

  function scoreElementLevel(play) {
    if (A.ctx || !play || !play.el) return;
    var duck = (play.bed && A.ducked) ? SCORE_DUCK : 1;
    var v = Cues.gainOf(play.cue.gain_db) * scoreVolume() * duck;
    try { play.el.volume = Math.max(0, Math.min(1, v)); } catch (e) { /* ignore */ }
  }

  // Lower the bed while the narrator is mid-line. Driven off `V.current`,
  // which is true for a server clip and for a browser utterance alike — the
  // score does not care which engine is reading, only that something is.
  function scoreDuckEval() {
    var want = !!V.current;
    if (want === A.ducked) return;
    A.ducked = want;
    scoreApplyLevels(SCORE_RAMP_MS);
  }

  // Start one cue. The element is the source in both worlds; with a context it
  // is routed through a gain node of its own carrying the cue's recorded dB,
  // and without one that dB is folded into `volume`.
  function scorePlay(cue) {
    var play = { cue: cue, el: null, gain: null, node: null, bed: Cues.isBed(cue),
                 stopping: false, dropped: false, detach: null };
    var el;
    try { el = new Audio(); } catch (e) { return null; }
    play.el = el;
    el.preload = 'auto';
    el.src = Cues.assetUrl(A.base, cue, A.digest);

    var ctx = scoreCtx();
    if (ctx) {
      try {
        play.node = ctx.createMediaElementSource(el);
        play.gain = ctx.createGain();
        play.gain.gain.value = 0;          // every cue fades up, if only over 0 ms
        play.node.connect(play.gain);
        play.gain.connect(play.bed ? A.bedBus : A.shotBus);
      } catch (e) { play.node = null; play.gain = null; }
    }
    if (!play.gain) { el.volume = 0; scoreElementLevel(play); }

    // Trim and loop as the pack recorded them. `el.loop` covers the ordinary
    // case; a bed with trimmed ends has to be sent back by hand, because the
    // element loops the file rather than the part of it anyone picked. What
    // counts as trimmed is `Cues.trimOf`, and it is a function rather than two
    // lines here because getting it wrong is inaudible in the code and very
    // audible in the room — see the note on it.
    var trim = Cues.trimOf(cue), from = trim.from, to = trim.to;
    var trimmed = from > 0 || isFinite(to);
    el.loop = !!cue.loop && !trimmed;
    if (from > 0) {
      el.addEventListener('loadedmetadata', function () {
        try { el.currentTime = from; } catch (e) { /* ignore */ }
      });
    }
    var onTime = function () {
      if (!isFinite(to) || el.currentTime < to) return;
      if (cue.loop) { try { el.currentTime = from; } catch (e) { /* ignore */ } }
      else scoreStop(play, 0);
    };
    if (trimmed) el.addEventListener('timeupdate', onTime);
    // A one-shot lets go of itself; a bed that has run out (`music_victory` is
    // 28 seconds and does not loop) leaves the slot empty rather than looping
    // something nobody picked for the silence after a fight.
    var onDone = function () { scoreDrop(play); };
    el.addEventListener('ended', onDone);
    el.addEventListener('error', onDone);
    // Releasing the element is what makes this necessary rather than tidy:
    // emptying `src` makes the element fail to load, which fires `error`,
    // which would call `scoreDrop` again — a loop that ends the tab rather
    // than the sound. The handlers come off first, and `dropped` is the belt
    // to that braces.
    play.detach = function () {
      el.removeEventListener('timeupdate', onTime);
      el.removeEventListener('ended', onDone);
      el.removeEventListener('error', onDone);
    };

    var fade = Math.max(0, Number(cue.fade_in_ms) || 0);
    if (play.gain) scoreRamp(play.gain.gain, Cues.gainOf(cue.gain_db), fade);
    else scoreElementLevel(play);
    try {
      var p = el.play();
      if (p && p['catch']) p['catch'](function () { scoreDrop(play); });
    } catch (e) { scoreDrop(play); return null; }
    return play;
  }

  // Fade a cue out and let it go. `ms` overrides the pack's fade, which is
  // what a game switch wants: gone now, not gone in two seconds.
  function scoreStop(play, ms) {
    if (!play || play.stopping) return;
    play.stopping = true;
    var fade = ms === undefined ? Math.max(0, Number(play.cue.fade_out_ms) || 0) : ms;
    if (play.gain) scoreRamp(play.gain.gain, 0, fade);
    setTimeout(function () { scoreDrop(play); }, fade);
  }

  function scoreDrop(play) {
    if (!play || play.dropped) return;
    play.dropped = true;
    play.stopping = true;
    if (play.detach) play.detach();
    try { play.el.pause(); } catch (e) { /* ignore */ }
    // `removeAttribute` + `load()` is how a media element is let go of: an
    // empty `src` string resolves against the document and fetches the page
    // itself, which is both a wasted request and the error described above.
    try { play.el.removeAttribute('src'); play.el.load(); } catch (e) { /* ignore */ }
    if (play.node) { try { play.node.disconnect(); } catch (e) { /* ignore */ } }
    if (play.gain) { try { play.gain.disconnect(); } catch (e) { /* ignore */ } }
    if (A.bed === play) { A.bed = null; scoreRenderControls(); }
    var i = A.shots.indexOf(play);
    if (i >= 0) A.shots.splice(i, 1);
  }

  function scoreFire(cue) {
    if (!cue || !cue.file) return;
    if (Cues.isBed(cue)) {
      // The same bed firing again is the same bed: `combat_start` on a second
      // fight in one scene must not restart the music under it.
      if (A.bed && !A.bed.stopping && A.bed.cue.id === cue.id) return;
      if (A.bed) scoreStop(A.bed);
      A.bed = scorePlay(cue);
      scoreRenderControls();
      return;
    }
    while (A.shots.length >= SCORE_SHOTS) scoreDrop(A.shots[0]);
    var play = scorePlay(cue);
    if (play) A.shots.push(play);
  }

  function scoreOnEvent(ev) {
    // `V.loading` is the history replay: a page opened mid-game renders
    // hundreds of events in one pass, and firing their cues would be every
    // sting of the evening at once. The bed that fight is being fought to is
    // picked up afterwards by `scoreCatchUp`.
    if (!scoreArmed() || V.loading) return;
    Cues.cuesForEvent(ev, A.cues).forEach(scoreFire);
  }

  // Join a game in progress: take the bed from the newest event in the
  // transcript that fires one and start it, without the one-shots, which
  // belong to moments that have already gone by.
  function scoreCatchUp() {
    if (!scoreArmed() || A.bed) return;
    for (var i = S.events.length - 1; i >= 0; i--) {
      var beds = Cues.cuesForEvent(S.events[i], A.cues).filter(Cues.isBed);
      if (beds.length) { beds.forEach(scoreFire); return; }
    }
    scoreRenderControls();   // nothing to pick up: say so rather than leave the last game's line
  }

  // Switching games: silence at once. The next game's bed comes from its own
  // history, not from whatever the last one was fighting to.
  function scoreReset() {
    if (A.bed) scoreStop(A.bed, 0);
    A.shots.slice().forEach(function (p) { scoreStop(p, 0); });
    A.bed = null;
    A.shots = [];
    scoreRenderControls();
  }

  // Must run inside a user gesture, exactly as the narration must: a browser
  // starts no sound without one. Turning the toggle on IS that gesture; a
  // spectator whose setting was already on gets it from their first tap.
  function scoreUnlock() {
    if (A.unlocked || !A.ready) return;
    A.unlocked = true;
    var ctx = scoreCtx();
    if (ctx && ctx.state === 'suspended') {
      try { ctx.resume(); } catch (e) { /* ignore */ }
    }
  }

  function scoreArmGesture() {
    if (A.gestureArmed || !A.settings.enabled) return;
    A.gestureArmed = true;
    var once = function (e) {
      var id = e && e.target && e.target.id;
      if (id === 'sound-on' || id === 'sound-vol') return;   // those decide for themselves
      document.removeEventListener('click', once, true);
      document.removeEventListener('touchend', once, true);
      if (A.settings.enabled && !A.unlocked) { scoreUnlock(); scoreCatchUp(); scoreRenderControls(); }
    };
    document.addEventListener('click', once, true);
    document.addEventListener('touchend', once, true);
  }

  function scoreEnable() {
    A.settings.enabled = true;
    scoreSaveSettings();
    scoreUnlock();
    scoreCatchUp();
    scoreRenderControls();
  }

  function scoreDisable() {
    A.settings.enabled = false;
    scoreSaveSettings();
    scoreReset();
    scoreRenderControls();
  }

  // The cue id is the only short name a cue has — the manifest's `when` is a
  // sentence and its credit is a paragraph — so the group prefix comes off and
  // the underscores become spaces. `music_combat_desperate` reads "combat
  // desperate", which is what it is.
  function scoreBedName() {
    if (!A.bed) return '';
    return String(A.bed.cue.id).replace(/^[a-z]+_/, '').replace(/_/g, ' ');
  }

  function scoreNowText() {
    if (!A.settings.enabled) return 'sound off';
    if (!A.unlocked) return 'tap anywhere to start the score';
    if (A.bed) return '♪ ' + scoreBedName();
    return pageHidden() ? 'silent while the tab is in the background' : 'silent — waiting for a cue';
  }

  function scoreRenderControls() {
    if (!A.ready) return;
    $('sound-on').checked = A.settings.enabled;
    $('sound-vol').value = A.settings.volume;
    $('sound-vol').disabled = !A.settings.enabled;
    $('sound').classList.toggle('locked', A.settings.enabled && !A.unlocked);
    $('score-now').textContent = scoreNowText();
  }

  // Attribution, which for most of this pack is a licence condition rather
  // than a courtesy: nearly all of it is CC BY, and the wording each source
  // requires is what `/api/audio` hands over as `credit`. Rendered once, for
  // the whole pack rather than for what happens to be playing — a credit that
  // appears for eight seconds during a fight is not one anybody can read.
  function scoreRenderCredits() {
    var list = $('score-credits');
    clear(list);
    A.cues.forEach(function (c) {
      if (!c.credit) return;
      list.appendChild(el('li', null, c.credit));
    });
  }

  // A tab nobody is looking at should not be playing music at them. The bed is
  // paused rather than dropped, so coming back picks it up where it stopped;
  // one-shots are seconds long and are left to finish.
  function scorePageHidden(hidden) {
    if (A.bed && A.bed.el) {
      try {
        if (hidden) A.bed.el.pause();
        else if (scoreArmed()) {
          var p = A.bed.el.play();
          if (p && p['catch']) p['catch'](function () { /* nothing to hear */ });
        }
      } catch (e) { /* ignore */ }
    }
    scoreRenderControls();
  }

  function scoreInit() {
    if (!Cues) {
      console.info('dnd-sim: cues.js missing; the score stays silent');
      return;
    }
    scoreLoadSettings();

    $('sound-on').addEventListener('change', function () {
      if (this.checked) scoreEnable(); else scoreDisable();
    });
    $('sound-vol').addEventListener('change', function () {
      A.settings.volume = SCORE_VOLUMES[this.value] ? this.value : 'normal';
      scoreSaveSettings();
      scoreApplyLevels(SCORE_RAMP_MS);
    });

    // Asked once, and late, like the voice probe: a server with no pack is a
    // page with no score, which is what every page was before this existed.
    fetch('/api/audio', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        if (!d || !d.available) {
          A.reason = (d && d.reason) || 'this server serves no audio pack';
          return;
        }
        A.cues = Cues.fromManifest(d);
        A.base = d.base || A.base;
        A.digest = d.digest || '';
        A.ready = A.cues.length > 0;
        if (!A.ready) return;
        $('sound').hidden = false;
        $('score-panel').hidden = false;
        scoreRenderCredits();
        scoreRenderControls();
        scoreArmGesture();
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
    // From the last seq RECEIVED, not the last revealed: the queue is already
    // holding events this page has, and replaying them would be work for
    // nothing (`ingest` drops them) — or, on a page that had not seen them, a
    // second copy.
    var url = '/api/games/' + encodeURIComponent(gameId) + '/stream?after=' + S.gotSeq;
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
      setConn('ended (' + S.status + ')', 'conn-off');
      // Queued behind the narrator like everything else: the game ending is
      // news the listener has not reached yet.
      S.endBanner = 'session ' + S.status;
      voiceOnGameEnd();
      revealPump();
      refreshSnapshot();
    });
  }

  function onMessage(e) {
    var ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    ingest(ev);
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
    scoreReset();
    S.snapGen++;        // an answer for the outgoing game must not land on this one
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
        // Straight onto the screen: `V.loading` holds the gate open, because
        // history is not something the narrator is about to read out — the
        // playhead is placed into it afterwards, by voiceStartAt.
        (events || []).forEach(ingest);
        return refreshSnapshot();
      })
      .then(function () { if (current()) { voiceStartAt(); scoreCatchUp(); } })
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
      if (!S.gameId) return;
      // Also the queue's slow hand: every other caller of revealPump is an
      // event or a transport move, and a game that has just gone terminal is
      // neither — this is what lets its last unnarrated lines go.
      revealPump();
      if (!TERMINAL[S.status]) refreshSnapshot();
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

  // Which values of a party spec's `age` mean a child. A mirror of
  // `normalize_age` in tts/voices.py, and only for showing the select the
  // right way round: the server re-reads whatever is submitted and is the one
  // that decides what a given answer means.
  var CHILD_AGES = { child: 1, kid: 1, boy: 1, girl: 1 };
  var CHILD_MAX_AGE = 12;
  // The same numeric grammar `_NUMERIC_AGE` pins in tts/voices.py, and here
  // for the same reason: `Number()` and Python's `float()` disagree in both
  // directions ("0xA" is 10 to one and an error to the other, "1_0" the other
  // way round), and a select showing one thing while the server casts another
  // would restate a character's age the moment this panel was submitted.
  var NUMERIC_AGE = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/;

  function isChildAge(said) {
    if (said === null || said === undefined || typeof said === 'boolean') return false;
    if (typeof said === 'number') return isFinite(said) && said > 0 && said <= CHILD_MAX_AGE;
    var s = String(said).trim().toLowerCase();
    if (!s) return false;
    if (CHILD_AGES[s]) return true;
    if (!NUMERIC_AGE.test(s)) return false;
    var n = Number(s);
    return isFinite(n) && n > 0 && n <= CHILD_MAX_AGE;
  }

  // The pronoun sets the panel offers, and the empty answer that is the
  // default. A character states pronouns rather than a gender: pronouns are a
  // fact its own persona already carries, and `gender_for_pronouns` in
  // tts/voices.py is the one place that turns them into a voice pool (he →
  // Polly's male voices, she → its female ones, everything else → the whole
  // roster). The list is not a taxonomy of who a character may be: a config
  // may state any set it likes, and one it states that is not offered here is
  // added to its own row below rather than rounded off to a neighbour.
  var PRONOUN_CHOICES = ['she/her', 'he/him', 'they/them'];
  var PRONOUN_UNSTATED = '— not stated —';

  // What a stated `pronouns` looks like in a select: an option value is a
  // string, and a config's is whatever JSON held.
  function pronounText(said) {
    return String(said === null || said === undefined ? '' : said).trim();
  }

  // What this seat's select offers: the three above, plus whatever the config
  // already says if that is something else. A config's own spelling wins where
  // the two differ only in case ("He/Him"), so the row shows the character as
  // written rather than a tidied-up version of it.
  function pronounOptions(said) {
    var opts = PRONOUN_CHOICES.slice();
    var s = pronounText(said);
    if (!s) return opts;
    var at = -1;
    opts.forEach(function (o, i) { if (o.toLowerCase() === s.toLowerCase()) at = i; });
    if (at >= 0) opts[at] = s; else opts.push(s);
    return opts;
  }

  // One row per seat: the character's name, the pronouns it goes by, and
  // whether its voice is an adult's or a child's. Both narrow who the server
  // may deal the seat and nothing else — the engine never sees either, and the
  // browser's fallback voices cannot answer them at all.
  //
  // A config that states only the older `gender` key shows its pronoun row as
  // unstated, and leaving it that way changes nothing: the server still reads
  // that key. Filling the row in from it would be inferring pronouns from a
  // gender, which is the direction this deliberately does not run — the
  // mapping is pronouns -> a set of voices, and it is not reversible.
  function renderPartySeats(party) {
    var host = $('ng-party');
    if (!host) return;
    clear(host);
    (party || []).forEach(function (member, i) {
      if (!member || typeof member !== 'object') return;
      var row = el('div', 'party-seat');
      row.appendChild(el('span', null, member.name || member.id || 'Seat ' + (i + 1)));

      var pro = el('select');
      pro.id = 'ng-pronouns-' + i;
      var blank = el('option', null, PRONOUN_UNSTATED);
      blank.value = '';
      pro.appendChild(blank);
      pronounOptions(member.pronouns).forEach(function (said) {
        var o = el('option', null, said);
        o.value = said;
        pro.appendChild(o);
      });
      pro.value = pronounText(member.pronouns);
      pro.addEventListener('change', function () { refreshPartyCast(party); });
      row.appendChild(pro);

      var sel = el('select');
      sel.id = 'ng-age-' + i;
      ['adult', 'child'].forEach(function (age) {
        var o = el('option', null, age);
        o.value = age;
        sel.appendChild(o);
      });
      sel.value = isChildAge(member.age) ? 'child' : 'adult';
      sel.addEventListener('change', function () { refreshPartyCast(party); });
      row.appendChild(sel);
      var cast = el('span', 'party-cast', '');
      cast.id = 'ng-cast-' + i;
      row.appendChild(cast);
      host.appendChild(row);
    });
    refreshPartyCast(party);
  }

  // What the two dropdowns actually buy: the voice that will read the seat,
  // its accent and its gender. A panel can show the controls and stay silent
  // about the outcome they turn, which is the state this was in — but the
  // outcome is the interesting half, and it is not one a reader can work out
  // from a pronoun set and an age.
  //
  // Asked of the server, never worked out here. `tts/voices.py: cast_for` is
  // the one place that decides this; a copy of the rules in JS would agree
  // with it exactly until somebody edited one of them, and the failure would
  // be a panel that names a voice the game does not use.
  function refreshPartyCast(party) {
    var seats = (party || []).map(function (m) {
      return (m && typeof m === 'object')
        ? { id: m.id, pronouns: m.pronouns, gender: m.gender, age: m.age } : {};
    });
    seats.forEach(function (seat, i) {
      // The rows as they stand now, not as the config has them: the point of
      // the preview is to answer for the selection in front of you.
      var pro = $('ng-pronouns-' + i);
      if (pro) seat.pronouns = pro.value || undefined;
      var sel = $('ng-age-' + i);
      if (sel) seat.age = sel.value === 'child' ? 'child' : undefined;
    });
    // A stale answer must not land on a newer question: the scenario picker
    // can change the whole party while a request is in flight.
    var token = ++S.castToken;
    api('/api/tts/cast', { method: 'POST', body: { party: seats } })
      .then(function (r) {
        if (token !== S.castToken) return;
        var got = (r && r.seats) || [];
        seats.forEach(function (_, i) {
          var box = $('ng-cast-' + i);
          if (!box) return;
          var seat = got[i];
          box.textContent = (r && r.available && seat && seat.voice)
            ? [seat.voice, seat.accent, seat.gender].filter(Boolean).join(' \u00b7 ')
            : '';
        });
      })
      .catch(function () {
        if (token !== S.castToken) return;
        // Narration is optional and this is a label on it. Say nothing rather
        // than putting an error where a voice name goes.
        seats.forEach(function (_, i) {
          var box = $('ng-cast-' + i);
          if (box) box.textContent = '';
        });
      });
  }

  // Write the panel's answers back into the party spec that is about to be
  // submitted. Both defaults DELETE their key rather than stating it: an
  // unstated age already casts as an adult and unstated pronouns already cast
  // from the whole pool, and a config should not claim a fact about a
  // character that nobody chose to state.
  //
  // Stating pronouns also drops a legacy `gender`, which the server would
  // ignore anyway once pronouns are present — the panel has just answered that
  // question, and leaving both would leave the config arguing with itself.
  // Leaving the row unstated touches neither key: an answer nobody gave is not
  // an answer to write down.
  //
  // A row still showing the config's own answer is not written back AT ALL,
  // rather than written back as the string the select holds. The select can
  // only hold a trimmed string, so re-stating one would rewrite `" they/them "`
  // and turn a `pronouns` that JSON gave as a list or an object into
  // "[object Object]" — a config nobody touched, quietly corrupted by opening
  // the panel.
  function applyPartySeats(party) {
    (party || []).forEach(function (member, i) {
      if (!member || typeof member !== 'object') return;
      var pro = $('ng-pronouns-' + i);
      if (pro && pro.value !== pronounText(member.pronouns)) {
        if (pro.value) { member.pronouns = pro.value; delete member.gender; }
        else delete member.pronouns;
      }
      var sel = $('ng-age-' + i);
      if (!sel) return;
      if (sel.value === 'child') member.age = 'child';
      else delete member.age;
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
    renderPartySeats(cfg.party);
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
    applyPartySeats(cfg.party);

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

  // ---------- theme ----------
  // Three states, not two: "auto" is the default and follows the system, and a
  // visitor who wants the other one gets to keep it. The choice is stored, so
  // it is a per-browser preference and never a server setting. index.html has
  // already applied it before first paint; this is the button and the label.
  var THEMES = ['auto', 'light', 'dark'];
  var THEME_KEY = 'dndsim.theme';

  //: The theme this page is showing. `localStorage` is where it is kept
  //: between visits, not where it is kept between clicks: a browser that
  //: refuses storage (Safari with cookies blocked, an embedded WebView, a
  //: quota that is full) throws on the read, and a cycle that asked storage
  //: what it was showing would answer "auto" every time and step to "light"
  //: for ever — a button that visibly works once and is then inert.
  var themeNow = 'auto';

  function themeStored() {
    try {
      var t = localStorage.getItem(THEME_KEY);
      return (t === 'light' || t === 'dark') ? t : 'auto';
    } catch (e) { return 'auto'; }
  }

  function themeApply(t) {
    themeNow = t;
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
    else document.documentElement.removeAttribute('data-theme');
    try {
      if (t === 'auto') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, t);
    } catch (e) { /* private mode: the theme still applies, it just won't stick */ }
    var label = $('theme-label');
    if (label) label.textContent = t;
    var btn = $('btn-theme');
    if (btn) {
      btn.title = t === 'auto' ? 'Colour theme: following the system'
                               : 'Colour theme: ' + t;
      btn.setAttribute('aria-label', btn.title + ' — click to change');
    }
    drawGrid();   // the map paints from the theme tokens
  }

  function themeInit() {
    themeApply(themeStored());
    $('btn-theme').addEventListener('click', function () {
      themeApply(THEMES[(THEMES.indexOf(themeNow) + 1) % THEMES.length]);
    });
    // On "auto", the system flipping is a repaint the canvas has to follow.
    var mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
    if (mq && typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', function () { if (themeNow === 'auto') drawGrid(); });
    }
  }

  // ---------- wiring ----------
  function init() {
    S.token = tokenLoad();
    // With a token in hand the probe is about to answer; rendering the locked
    // state first would flash "Unlock" at a browser that is already unlocked.
    if (!S.token) renderWriteAccess();
    themeInit();
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
    // The canvas is sized in pixels by drawGrid, so it goes stale whenever its
    // box changes without the window doing anything — and the box changes a
    // lot: unlocking the write controls or a TTS probe revealing the narration
    // bar grows the feed, and the board gives that height up out of the map.
    // The observer is the general answer; the guard is because a redraw can
    // itself change the box by a scrollbar's width, and that must not loop.
    if (window.ResizeObserver) {
      var wrap = $('grid').parentElement;
      var lastW = 0, lastH = 0;
      new ResizeObserver(function () {
        var w = wrap.clientWidth, h = wrap.clientHeight;
        if (w === lastW && h === lastH) return;
        lastW = w; lastH = h;
        drawGrid();
      }).observe(wrap);
    }
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        // Backgrounded synthesis is suspended and comes back garbled, so stop —
        // but leave the playhead on the line it was reading. Coming back
        // resumes there; it does not jump to whatever the game reached
        // meanwhile, which is the whole point of a playhead.
        voiceStopCurrent(false);
        voiceHoldRelease();
      } else {
        voiceRepump();      // pick the same line back up, and let the queue move
        voiceHoldEval();
      }
      scorePageHidden(document.hidden);
      voiceRenderControls();
      if (!document.hidden && S.gameId && !TERMINAL[S.status]) {
        if (!S.es || S.es.readyState === 2) connect(S.gameId);
        refreshSnapshot();
      }
    });

    voiceInit();
    scoreInit();

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
