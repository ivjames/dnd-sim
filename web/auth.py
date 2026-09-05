"""Authentication for the routes that mutate a game or spend money.

The spectator UI is public and anonymous: reading a game, listing games and the
SSE stream take no credential and must keep taking none — that is the site.
What needed a perimeter is the other half of the API. `POST /api/games` starts
a real game against real API keys, and `POST /api/games/<id>/note` feeds up to
2,000 characters into a `dm_note` that `web/static/speech.js` always speaks, at
Polly's neural rate. Both were open to anyone who could reach the box, and
`DND_TTS_MAX_USD` bounds one game rather than how many a stranger may start.
TTS-COSTS.md §1 and §4 land on this three separate times.

The mechanism is a shared secret in a header:

    X-Dnd-Token: <the value of DND_WRITE_TOKEN>

One secret for everyone who may write, compared in constant time, no session,
no store, no dependency. It is deliberately the smallest thing that closes the
hole: the alternative designs (basic auth, signed tokens, per-user accounts)
all buy revocation and identity, and there is no second writer here to revoke
or to identify. Rotating the value in `.env` and restarting is the whole
revocation story, and that is a stated cost rather than an oversight.

Two rules with a reason worth stating:

- **The token is read from the header alone**, never a query string. A query
  string is in nginx's access log, in `Referer` on any outbound link, and in
  the browser's history; the header is in none of them.
- **An unset token refuses writes (503) rather than opening them.** The token
  is generated onto the box by `dndsim token`, so a deploy alone never produces
  one and the first deploy carrying this code reaches a droplet where it is not
  set yet. Failing open there would ship a no-op — the exact hole this closes,
  still open, and now believed closed. Failing closed costs one edit to `.env`
  before the next game can be created, and nothing else: the app starts, the
  page loads, every read and the stream work, and any game already running
  keeps running and stays audible.
"""

from __future__ import annotations

import hmac
import os
from functools import wraps
from typing import Any, Callable

from flask import current_app, jsonify, request

#: Where the secret comes from. On lab980 it lives in `/var/www/dndsim/.env`,
#: which `run.sh` sources, and `dndsim token` is what puts it there — the same
#: file every platform key lives in, and the only one on the box; the
#: difference is that this one is generated rather than typed in. It is on
#: `KNOWN_KEYS` in `bin/dndsim` so `keys`/`status` report it.
ENV_VAR = "DND_WRITE_TOKEN"

#: The request header carrying it.
HEADER = "X-Dnd-Token"

#: `code` in the refusal body, so a client can tell the two apart without
#: reading the prose (the status codes say the same thing).
UNCONFIGURED = "writes_unconfigured"
UNAUTHORIZED = "unauthorized"


def configured_token() -> str:
    """The write token this app was built with, or ``""`` if it has none.

    `create_app` snapshots the environment into `app.config` so tests can
    inject one; outside an app context (and for a config that never set the
    key) the environment is the fallback, which is what `python -m web.app`
    runs with.
    """
    token: Any = None
    if current_app:
        token = current_app.config.get(ENV_VAR)
    if token is None:
        token = os.environ.get(ENV_VAR)
    return str(token or "").strip()


def presented_token() -> str:
    return str(request.headers.get(HEADER) or "").strip()


def writes_configured() -> bool:
    return bool(configured_token())


def authenticated() -> bool:
    """Does this request carry the write token?

    Constant-time, so the comparison does not leak the secret a byte at a time,
    and False whenever there is no token to compare against — an unconfigured
    server authenticates nobody rather than everybody.
    """
    expected = configured_token()
    if not expected:
        return False
    presented = presented_token()
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def write_refusal():
    """``(response, status)`` if this request may not write, else ``None``."""
    if not writes_configured():
        return (
            jsonify(
                {
                    "error": (
                        "writes are not configured on this server: set "
                        f"{ENV_VAR} and restart"
                    ),
                    "code": UNCONFIGURED,
                }
            ),
            503,
        )
    if not authenticated():
        return (
            jsonify(
                {
                    "error": f"a valid {HEADER} header is required",
                    "code": UNAUTHORIZED,
                }
            ),
            401,
        )
    return None


def require_write(fn: Callable) -> Callable:
    """Gate a route that mutates a game or spends money.

    Applied to `POST /api/games` and to the per-game controls that change what
    a game is doing — pause, resume, stop, note. Not to `POST
    /api/games/<id>/hold`: the hold is how an anonymous listener keeps the
    simulation in step with the spoken line (CONTRACTS.md, 2026-09-04), every
    spectator renews it every few seconds, it spends nothing and it expires by
    itself. Gating it would take narration away from exactly the audience this
    site is for.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        refusal = write_refusal()
        if refusal is not None:
            return refusal
        return fn(*args, **kwargs)

    return wrapper


__all__ = [
    "ENV_VAR",
    "HEADER",
    "UNAUTHORIZED",
    "UNCONFIGURED",
    "authenticated",
    "configured_token",
    "presented_token",
    "require_write",
    "write_refusal",
    "writes_configured",
]
