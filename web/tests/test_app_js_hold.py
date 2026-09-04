"""The narration hold latches off a server answer, not off any failure at all.

`voiceHoldSend` POSTs a renewable lease and `_gate()` waits on it, so while it
is in force the game cannot get more than one event ahead of the narrator. When
it stops, nothing says so — the game simply runs on at `tempo_ms` and the
listener falls behind with no explanation. That makes the *condition* for
giving up the interesting part of the code: it has to be a status the server
actually returned and that actually means "this game cannot be held", because
`api()` rejects on a dropped connection and a 502 too, and one of those used to
end holding for the rest of the page's life.

Source-level assertions, like `test_app_js_narration.py`: what is being guarded
against is an edit that quietly widens the catch back out to every rejection,
and no fake server can show that — the failure it models is the one the fake
never has.
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(ROOT, "web", "static", "app.js")
API_PY = os.path.join(ROOT, "web", "routes", "api.py")


def read(path: str = APP_JS) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def fn(js: str, signature: str) -> str:
    """The body of a top-level function in app.js, by its exact signature."""
    m = re.search(r"\n  function " + re.escape(signature) + r" \{(.+?)\n  \}\n", js, re.S)
    assert m, "app.js no longer has a `%s`" % signature
    return m.group(1)


def hold_catch(js: str) -> tuple[str, str]:
    """The rejection handler on the hold POST, and the name it binds."""
    body = fn(js, "voiceHoldSend(seconds)")
    m = re.search(r"\.catch\(function \((\w*)\) \{(.+?)\n      \}\)", body, re.S)
    assert m, "the hold POST no longer has a catch"
    return m.group(1), m.group(2)


def test_api_hands_its_callers_the_status_it_failed_with():
    """Without it no catch can tell a refusal from a blip: the message is the
    server's own words when there are any, and carries no code at all."""
    body = fn(read(), "api(path, opts)")
    assert re.search(r"\berr\.status = r\.status;", body), \
        "api() no longer attaches the status to the error it throws"
    assert "throw err;" in body
    # Additive: the message every existing catch reads is unchanged, and a
    # rejection from fetch itself still never reaches this line — so an absent
    # `status` keeps meaning "no server answered".
    assert "(data && data.error) || ('HTTP ' + r.status)" in body


def test_the_hold_gives_up_only_on_a_status_the_server_returned():
    name, catch = hold_catch(read())
    assert name, "the hold's catch no longer looks at the rejection"
    guard = re.search(
        r"if \(HOLD_REFUSED\[[^\]]+\]\) \{(.+?)\n        \} else \{(.+?)\n        \}",
        catch, re.S)
    assert guard, "the hold's catch no longer branches on the status"
    refused, transient = guard.group(1), guard.group(2)
    assert name + ".status" in catch, "the branch is not keyed on the status"
    # Before the branch nothing is decided about the game — only that this
    # request did not hold it.
    assert "holdBroken" not in catch[:catch.index("if (HOLD_REFUSED")]
    assert "V.holdBroken = true" in refused
    # The whole bug: a network error or a 5xx leaves the hold wanted, so the
    # next tick asks again.
    assert "holdBroken" not in transient, \
        "a failure the server did not explain still latches the hold off"


def test_a_failure_the_server_did_not_explain_is_retried_with_a_bounded_backoff():
    js = read()
    _, catch = hold_catch(js)
    transient = re.search(r"\} else \{(.+?)\n        \}", catch, re.S).group(1)
    assert "V.holdFails += 1" in transient
    assert "HOLD_RETRY_MAX" in transient, "the retry is unbounded"
    assert re.search(r"var HOLD_RETRY_MAX = \d+;", js)
    # Not asked again before then, and a success forgets the whole thing.
    send = fn(js, "voiceHoldSend(seconds)")
    assert "if (want && now < V.holdRetryAt) return;" in send
    assert "V.holdFails = 0;" in send and "V.holdRetryAt = 0;" in send
    # Per game, like the rest of the hold state.
    reset = fn(js, "voiceReset()")
    assert "V.holdFails = 0;" in reset and "V.holdRetryAt = 0;" in reset


def test_the_refused_statuses_are_the_ones_hold_actually_returns():
    """404 no such game, 409 another process, 501 no such method — and not the
    400 for a bad `seconds`, which is this page's own bug to fix, nor any 5xx."""
    m = re.search(r"var HOLD_REFUSED = \{(.+?)\};", read())
    assert m, "app.js no longer names the statuses it gives up on"
    assert set(re.findall(r"(\d+):", m.group(1))) == {"404", "409", "501"}
    hold = re.search(r"\ndef hold\(game_id: str\).+?\n\n\n", read(API_PY), re.S)
    assert hold, "web/routes/api.py no longer has a hold()"
    for status in ("404", "409", "501"):
        assert status in hold.group(0), \
            "hold() no longer answers %s; the page still gives up on it" % status
