# dnd-sim — working notes

An LLM-run D&D 5e game with no humans at the table: a Sonnet DM, Haiku
players, a deterministic Python rules engine, and a spectator web UI over SSE.
(`dnd-sim` is the working name; the product name is undecided — see PLAN.md.)

Served at **https://dndsim.lab980.com** from the lab980 droplet.

How work lands here — branch, PR, and the fact that merging is not deploying —
is in `.claude/rules/lab980-conventions.md`, which Claude Code loads
automatically every session. That file is owned by the lab980 scaffold and is
overwritten by it; **this** file is the site's own, and everything below is
about this site rather than about the platform. For the box itself, read the
`ivjames/lab980.com` repo's `CLAUDE.md`.

## Open decisions

This repo's own runbook (`deploy/INSTALL.md`, `ecosystem.config.js`) and the
lab980 conventions disagree on the **checkout dir** (`/opt/dnd-sim` vs
`/var/www/dndsim`) and **where the API key lives** (`/etc/environment` vs a
local `.env`). Neither is resolved here — `DEPLOY.md` "Open decisions
(conflicts with lab980 conventions)" lays out both sides of each. (The third
one, the port, was decided 2026-09-03: **8071**, first free in the 8060+ range
on the droplet.) Until the other two are decided,
`deploy/INSTALL.md` is the authored runbook and `DEPLOY.md`'s bring-up block
is the conventions-shaped alternative. Don't quietly pick a side in a code
change; make it a decision.

## Shape

A **proxied app**: nginx fronts a pm2-managed **Python 3.11 / Flask** process
on `127.0.0.1:8071`. Not Node — there is no `package.json`, no `npm ci`, no
build. The install is `python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt`, and `requirements.txt` is deliberately tiny (Flask,
anthropic, pytest; ranges, not pins — no Pydantic, per CONTRACTS.md).

- Repo: `ivjames/dnd-sim` · droplet dir: `/var/www/dndsim` by convention (open
  decision 1)
- pm2 process: **`dnd-sim`** (from `ecosystem.config.js`; note it is not the
  stub `dndsim`) — runs `./run.sh`, which execs `.venv/bin/python -m web.app`.
  **fork mode**, `exec_mode: 'fork'` explicit, and `instances: 1` is a hard
  ceiling, not a default: games run as threads inside the web process and SSE
  subscribers attach to its in-memory event bus, so a second instance would
  answer half the requests from a process that knows nothing about the game.
  Never raise it.
- **SSE.** The spectator UI reads `/api/games/<id>/stream`. The vhost must
  carry `proxy_buffering off`, `proxy_cache off`, hour-long
  `proxy_read_timeout`/`proxy_send_timeout`, and `gzip off` (the reference block
  is `deploy/nginx-dndsim.conf`), or the browser sees nothing until the game
  ends. This survives whichever vhost path open decision picks.
- Config is process environment: `PORT`, `HOST`, `ANTHROPIC_API_KEY`,
  `DND_SIM_MOCK`, `DND_SIM_DB`, the `DND_*_MODEL` overrides (full table in
  README and `DEPLOY.md`). Nothing reads a `.env` file (open decision 3).
  State is SQLite at `data/dndsim.sqlite3`; `data/` and `.env` are gitignored
  and survive a deploy's hard reset.
- vhost: `/etc/nginx/sites-available/dndsim.lab980.com` if `provision-site`
  writes it; `deploy/nginx-dndsim.conf` is the hand-written HTTP-first version.

## Deploying

On the droplet, as root:

```bash
dndsim deploy      # fetch + reset to origin/main, pip install, pm2 restart dnd-sim, probe /api/health
dndsim status      # HEAD, pm2 state, local + public /api/health probe, cert days
dndsim logs        # tail this app's pm2 logs
```

A restart kills any in-flight game (the app marks it `stopped` on boot; the
transcript stays readable) — deploy between games if one matters. Full
runbook, including first-time bring-up and the env keys: `DEPLOY.md`.

## Things worth knowing

- **Mock mode costs nothing.** `DND_SIM_MOCK=1` swaps in `MockLLMClient`: no
  key, no API calls, and same config + seed ⇒ byte-identical game. Run locally
  and test that way; `.venv/bin/python -m orchestrator.cli --config
  examples/goblin_ambush.json --mock --seed 42` is the headless integration
  test.
- **Live mode is real money.** Each game config carries `budget_usd`; the
  orchestrator tracks spend per role and halts the game at `budget_exceeded`.
  Prompts are built for frugality (compact state views, enumerated legal
  actions, prompt-cached rules digest, summaries by the cheap model) — keep
  them that way.
- **Layering is strict and one-way**: `web → orchestrator → agents → llm`,
  with `engine/` pure (no I/O, no LLM, no threads) underneath. `CONTRACTS.md`
  is the binding interface spec; a contract change is recorded under its
  "Amendments" section with rationale, not made silently. Game content is
  SRD 5.1 (CC-BY-4.0) only — no non-SRD Wizards content, ever.
- Tests: `.venv/bin/python -m pytest -q` (everything);
  `.venv/bin/python -m pytest web/tests -q` for the web layer alone (fakes).
- Verify a **clean** clone installs and passes, not just the working tree:

  ```bash
  d=$(mktemp -d) \
    && git archive HEAD | tar -x -C "$d" \
    && ( cd "$d" && python3 -m venv .venv \
         && .venv/bin/pip install -q -r requirements.txt \
         && .venv/bin/python -m pytest -q ) \
    && rm -rf "$d"
  ```

  `mktemp -d` is the point, not tidiness. The directory has to exist — `tar -x
  -C` into a missing one fails outright — and it has to be *empty*, or the
  extract merges over an earlier run's files and the check quietly stops
  testing a clean tree. A fixed `/tmp/x` gets both wrong, and on a shared
  `/tmp` it lets two runs race, each able to delete the other's tree
  mid-build. The subshell keeps you in the checkout; the trailing `rm -rf`
  fires only on success, leaving a failed run in `$d` to look at. A
  kitchen-sink `.gitignore` eating a source dir is the classic thing this
  catches (`data/` is ignored here on purpose; `engine/data/` is not, and must
  stay tracked — `git ls-files engine/data` confirms).
- pm2 process name is `dnd-sim`; `dndsim logs` tails it.
