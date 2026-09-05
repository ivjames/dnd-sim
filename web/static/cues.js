/* dnd-sim score routing — the pure half. No DOM, no audio, no state: given an
   event (engine/events.py shape) and the cue table out of the pack's manifest,
   decide which sounds it fires. app.js owns the mixer and the <audio>
   elements. Exposed as the global `DndCues` for the page and as
   module.exports for `node` tests.

   The rules here are a transliteration of `tools/audio/cues.py`, which is
   where they are decided and where the picker and the fetcher read them from.
   Nothing in this file may know a cue id or an event kind: the manifest
   carries every `match` rule with its file (AUDIO.md), so re-picking the pack
   changes what the game sounds like without touching a line of this. */
(function (root) {
  'use strict';

  // Layer order, and the order a multi-cue event is returned in. Same tuple as
  // `cues.GROUPS`; the first two are BEDS — one plays at a time and a new one
  // replaces it — and the rest are one-shots that land on top.
  var GROUPS = ['music', 'ambience', 'sting', 'swell', 'sfx'];
  var BEDS = { music: 1, ambience: 1 };

  function isBed(cue) { return !!(cue && BEDS[cue.group]); }

  // `a.b` → data.a.b, or undefined where the path runs out.
  function dig(data, path) {
    var cur = data, parts = String(path).split('.'), i;
    for (i = 0; i < parts.length; i++) {
      if (!cur || typeof cur !== 'object' || !Object.prototype.hasOwnProperty.call(cur, parts[i])) {
        return undefined;
      }
      cur = cur[parts[i]];
    }
    return cur;
  }

  // True where `ev` satisfies a cue's match rule: the kind, plus zero or more
  // equality constraints on `data` with dotted paths for nested values.
  // Deliberately dumb — no ranges, no negation — and identical to
  // `cues.event_matches`, including its one subtlety: a rule that says `true`
  // is not satisfied by `1`. Python needs that spelled out because `1 == True`
  // there; JavaScript's `===` would catch it anyway, and it is still written
  // out so the two files can be read against each other line for line.
  function eventMatches(match, ev) {
    if (!match || !ev) return false;
    if (ev.kind !== match.kind) return false;
    var want = match.data || {}, data = ev.data || {}, path;
    for (path in want) {
      if (!Object.prototype.hasOwnProperty.call(want, path)) continue;
      var got = dig(data, path);
      if ((typeof want[path] === 'boolean') !== (typeof got === 'boolean')) return false;
      if (got !== want[path]) return false;
    }
    return true;
  }

  // Every layer this event fires: at most one cue per group, most specific
  // first. Audio layers rather than replaces — `combat_start` swaps the bed
  // AND hits a sting — so one event can light one cue in each group and no
  // more. Within a group the rule with the most constraints wins (a crit beats
  // a plain hit); ties break on the order the cues arrive in, which is the
  // manifest's order, which is `cues.py`'s declaration order (`fetch.plan`).
  function cuesForEvent(ev, cues) {
    var best = {}, rank = {}, i, c, n;
    for (i = 0; i < (cues || []).length; i++) {
      c = cues[i];
      if (!eventMatches(c && c.match, ev)) continue;
      n = Object.keys((c.match && c.match.data) || {}).length;
      if (!(c.group in best) || n > rank[c.group]) { best[c.group] = c; rank[c.group] = n; }
    }
    var out = [];
    GROUPS.forEach(function (g) { if (best[g]) out.push(best[g]); });
    return out;
  }

  function cueForEvent(ev, group, cues) {
    var list = cuesForEvent(ev, cues);
    for (var i = 0; i < list.length; i++) if (list[i].group === group) return list[i];
    return null;
  }

  // The cue table as an ordered array, whichever shape it arrives in. The
  // server sends a list precisely because the order is the tie-break above and
  // a JSON object's key order is not a thing to lean on; the manifest ON DISK
  // is an object keyed by cue id, and reading that directly is what the tests
  // do, so both are accepted here and nowhere else.
  function fromManifest(doc) {
    var cues = (doc && doc.cues) || [];
    if (Array.isArray(cues)) return cues.slice();
    return Object.keys(cues).map(function (id) {
      var c = cues[id] || {};
      return c.id ? c : Object.assign({ id: id }, c);
    });
  }

  // Where a cue's file is fetched from. The digest is the pack's, not the
  // file's: clips are served immutable, so a re-picked bed needs the URL to
  // move or every open tab goes on playing the old one out of its cache.
  function assetUrl(base, cue, digest) {
    var url = String(base || '/audio/') + String((cue && cue.file) || '');
    return digest ? url + '?v=' + encodeURIComponent(digest) : url;
  }

  // The part of the file to play: `{from, to}` in seconds, `to` NaN where the
  // cue plays to the end — which is what the pack records for nearly all of
  // them, as `trim_end_s: null`.
  //
  // Written out rather than done inline because the obvious inline version is
  // wrong in a way that sounds like a broken pack: `Number(null)` is 0, not
  // NaN, so a cue with no end trim reads as one trimmed to nothing. Every bed
  // then loops at position zero and every sting is cut a fraction of a second
  // in. A non-positive end is read as absent for the same reason — nobody
  // picks a cue that stops before it starts, so a 0 there means "unset" in
  // whatever wrote it.
  function trimOf(cue) {
    var from = Number((cue && cue.trim_start_s) || 0);
    if (!isFinite(from) || from < 0) from = 0;
    var raw = cue && cue.trim_end_s;
    var to = (raw === null || raw === undefined || raw === '') ? NaN : Number(raw);
    if (!isFinite(to) || to <= from) to = NaN;
    return { from: from, to: to };
  }

  // dB is how the pack records level (and how anyone mixing it thinks); the
  // Web Audio API and an <audio> element both want a linear multiplier.
  function gainOf(db) {
    var n = Number(db);
    return isFinite(n) ? Math.pow(10, n / 20) : 1;
  }

  var api = {
    GROUPS: GROUPS,
    BEDS: BEDS,
    isBed: isBed,
    dig: dig,
    eventMatches: eventMatches,
    cuesForEvent: cuesForEvent,
    cueForEvent: cueForEvent,
    fromManifest: fromManifest,
    assetUrl: assetUrl,
    trimOf: trimOf,
    gainOf: gainOf
  };

  root.DndCues = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
