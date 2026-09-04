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

## Decisions

This repo's own runbook and the lab980 conventions used to disagree on three
points. All three are decided (2026-09-03) and the files agree: the checkout
dir is **`/var/www/dndsim`** (always `/var/www/<stub>` on this box), the port
is **8071** (first free in the 8060+ range on the droplet), and the platform
keys live in **`/etc/environment`** — the known-key store on this box — from
where `dndsim deploy` copies each one once into **`/var/www/dndsim/.env`**,
which `run.sh` sources on every start. pm2 is launched with every known key
unset so its dump never carries one. There is no hand runbook: `deploy/INSTALL.md` is a
pointer, `DEPLOY.md` is the doc, and `bin/dndsim` does the work. Don't
reopen these in a code change; if one has to change, make it a decision.

## Shape

A **proxied app**: nginx fronts a pm2-managed **Python 3.11 / Flask** process
on `127.0.0.1:8071`. Not Node — there is no `package.json`, no `npm ci`, no
build. The install is `python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt`, and `requirements.txt` is deliberately tiny (Flask, httpx,
anthropic, pytest; ranges, not pins — no Pydantic, per CONTRACTS.md).

- Repo: `ivjames/dnd-sim` · droplet dir: `/var/www/dndsim`
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
  ends. `provision-site`'s generic vhost says the opposite, so `dndsim
  setup` rewrites the proxied location with a marker-tagged block and
  repairs it on every run; `dndsim status` says whether it is present.
- Config is process environment, loaded from `./.env` by `run.sh` (`set -a;
  . ./.env; set +a`, only if the file exists) before it execs python — so
  pm2-managed and hand runs read the same file. Keys: `PORT`, `HOST`, the
  platform API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `XAI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, and for the two
  inference hosts `SILICONFLOW_API_KEY` and `DEEPINFRA_API_KEY`, whose seats
  are written `siliconflow:<id>` / `deepinfra:<id>` — any subset; only the
  Anthropic one is warned about, since the default DM needs it, and a seat
  whose platform has no key fails at game creation naming the variable;
  `CARTESIA_API_KEY` on the droplet is text-to-speech and is not one of these),
  `DND_SIM_MOCK`, `DND_SIM_DB`, the `DND_*_MODEL` overrides (full table in
  README and `DEPLOY.md`). The app itself reads `os.environ` only; there is
  no `python-dotenv`. `dndsim deploy` writes `.env` (mode 600) and adopts
  every known key it lacks into it from `/etc/environment`, then from
  `/var/www/ffc/server/.env` (first file holding a key wins; the list is
  `DNDSIM_KEY_SOURCE`; `GOOGLE_API_KEY` is accepted for `GEMINI_API_KEY`); the list is `KNOWN_KEYS` in
  `bin/dndsim`, overridable with `DNDSIM_KEYS`, and the same list is what
  the pm2 launch unsets. `dndsim keys` prints which known keys `.env` and
  the store hold (names only) and exits 1 without `ANTHROPIC_API_KEY`. State
  is SQLite at `data/dndsim.sqlite3`; `data/`, `.env` and `.venv/` are
  gitignored and survive a deploy's hard reset.
- vhost: `/etc/nginx/sites-available/dndsim.lab980.com`, written by
  `provision-site` (via `dndsim setup`) or, without it, from
  `deploy/nginx-dndsim.conf`. `dndsim setup` keeps the marker-tagged SSE
  block in it either way.

## Deploying

On the droplet, as root. First time, with `ANTHROPIC_API_KEY` (and whichever
other platform keys you have) already in `/etc/environment`:

```bash
git clone https://github.com/ivjames/dnd-sim /var/www/dndsim \
  && ln -sf /var/www/dndsim/bin/dndsim /usr/local/bin/dndsim \
  && dndsim deploy
```

Every time after:

```bash
dndsim deploy      # reset to origin/main, venv + pip, .env (adopt keys), vhost via
                   # setup if missing, pm2 start/restart (keys unset), save if online, probe
dndsim setup       # once, idempotent: provision-site (or HTTP-only fallback vhost),
                   # SSE block in the vhost, pm2-root check; --dry-run shows the diff
dndsim status      # HEAD, pm2, .env + per-key presence, upstream family, vhost + SSE
                   # block, local + public /api/health, cert days
dndsim keys        # per-key present/absent in .env and /etc/environment (names only);
                   # exit 1 if ANTHROPIC_API_KEY is missing from .env
dndsim logs        # tail this app's pm2 logs
```

A restart kills any in-flight game (the app marks it `stopped` on boot; the
transcript stays readable) — deploy between games if one matters. What each
step does, the env keys, and how to confirm what is live: `DEPLOY.md`.

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
- **Scenarios are `examples/*.json` and the web panel offers all of them**, so a
  file dropped in there is a shipped scenario. `budget_usd` in each is sized
  from a full mock run of that file (`--mock --tempo 0 --budget 20` prints the
  total) with headroom — the budget is a stop, not an estimate, and a game that
  hits it halts mid-scene. Grid bounds, `scene_<i>` triggers, party size and
  monster names are checked for every file by the suite, so a broken scenario
  fails `pytest` rather than a live game.
- **Players sample at `player_temperature` (default 1.0), not 0.8.** Per game,
  per seat (`temperature` in a party spec), or from the panel's Improv field;
  clamped to `[0, 1]` because that is Anthropic's ceiling. The DM stays at 0.8
  — it owns world facts — and the summarizer at 0.3.
- pm2 process name is `dnd-sim`; `dndsim logs` tails it.
