# Deploying dnd-sim

Target: **https://dndsim.lab980.com** on the lab980 droplet (platform
conventions: the `ivjames/lab980.com` repo's `CLAUDE.md`). nginx proxies to a
pm2-managed **Python/Flask** process on `127.0.0.1:8071`; pm2 runs `./run.sh`,
which sources `./.env` and execs `.venv/bin/python -m web.app`. No Node, no
build: `pip install -r requirements.txt` into `.venv` is the whole install.
The pm2 process is **`dnd-sim`** (from `ecosystem.config.js`), not the stub.

There is no hand runbook. Bring-up and every deploy are `dndsim deploy`;
`bin/dndsim` carries all of it, idempotently.

## Decisions (2026-09-03, final)

Three things this repo and the lab980 conventions used to disagree on are
decided: the checkout is **`/var/www/dndsim`** (always `/var/www/<stub>` on
this box; the repo was authored against `/opt/dnd-sim`), the port is **8071**
(first free in lab980's 8060+ range on the droplet; the repo was authored on
8045), and the keys: **the platform API keys stay in `/etc/environment`**, the
known-key store on this box, and the app reads them from **`/var/www/dndsim/.env`**,
which `dndsim deploy` writes once by copying each value across and `run.sh`
sources on every start. pm2 never sees a key — `deploy` launches it under
`env -u <every known key> -u GITHUB_TOKEN`, so `~/.pm2/dump.pm2` does not carry
the root shell's copies — and no key is ever on any argv or in any log line.

## Bring-up (on the droplet, as root)

The one hand step: make sure the keys are in `/etc/environment`, which is the
only file `dndsim deploy` reads them from. Until 2026-09-04 it also consulted
`/var/www/ffc/server/.env` — another site's untracked runtime state, which is
not this site's to depend on, and which a deploy would have adopted once and
then held stale through any rotation there. It no longer does.
`DNDSIM_KEY_SOURCE` (below) still takes a colon-separated list, so a second
file remains possible and is now a deliberate act rather than a default. The
known names, one per LLM platform the app can seat at the table:

| key | platform |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) — the default DM; the only one whose absence `deploy` warns about |
| `OPENAI_API_KEY` | OpenAI |
| `GEMINI_API_KEY` | Google Gemini (`GOOGLE_API_KEY` in the store is accepted as an alias and adopted under the `GEMINI_API_KEY` name) |
| `XAI_API_KEY` | xAI (Grok) |
| `MISTRAL_API_KEY` | Mistral |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `SILICONFLOW_API_KEY` | SiliconFlow (international platform, `api.siliconflow.com`) — a host; seats are `siliconflow:<model id>` |
| `DEEPINFRA_API_KEY` | DeepInfra — a host; seats are `deepinfra:<model id>` |
| `AWS_ACCESS_KEY_ID` | Amazon Polly, which reads the game aloud — with `AWS_SECRET_ACCESS_KEY` and `AWS_REGION` |
| `AWS_SECRET_ACCESS_KEY` | the other half of the pair |
| `AWS_REGION` | e.g. `us-east-1`; not a secret, but adopted with the pair because boto3 needs all three in the same file |

(`CARTESIA_API_KEY`, also in the store, is a text-to-speech key this app does
not use — narration is Polly — and is deliberately not a known key.
`DND_WRITE_TOKEN` does not belong in the store either: it is this app's own
secret rather than one of the box's shared vendor keys, so `dndsim token`
generates it straight into `.env` — see **The write token** below.) Any subset is fine. A seat configured for a platform whose key is missing
fails at game creation with a message naming the variable — that is the app's
behaviour, not the CLI's — and the other seats are unaffected. To adopt a key
for a platform not in this list, set `DNDSIM_KEYS` (space-separated names) when
running `dndsim deploy`; no code change is needed.

```bash
grep -q '^ANTHROPIC_API_KEY=' /etc/environment || echo 'ANTHROPIC_API_KEY=sk-ant-...' >> /etc/environment
# and likewise OPENAI_API_KEY=..., GEMINI_API_KEY=..., XAI_API_KEY=..., MISTRAL_API_KEY=..., DEEPSEEK_API_KEY=...,
# SILICONFLOW_API_KEY=..., DEEPINFRA_API_KEY=...
```

Then:

```bash
git clone https://github.com/ivjames/dnd-sim /var/www/dndsim \
  && ln -sf /var/www/dndsim/bin/dndsim /usr/local/bin/dndsim \
  && dndsim deploy \
  && dndsim token
```

That is the whole thing. The only once-per-droplet item it does not do is the
pm2 boot hook (`pm2 startup systemd -u root --hp /root`, then the line it
prints) — `dndsim setup` checks `systemctl is-enabled pm2-root` and warns if
it is missing.

## What `dndsim deploy` does

In order, each step a no-op when already done:

1. `git fetch` + `git reset --hard origin/main` (`DNDSIM_BRANCH` overrides).
   A tracked file edited on the droplet is destroyed silently — fix it in
   the repo. `.venv/`, `.env` and `data/` are gitignored and survive.
2. Creates `.venv` if missing; `pip install -r requirements.txt` (ranges, not
   pins — a deploy can resolve newer versions than the last one). `--no-install`
   skips this step.
3. `mkdir -p data`; writes `.env` (mode 600): seeds `PORT=8071`,
   `HOST=127.0.0.1`, `DND_SIM_DB=/var/www/dndsim/data/dndsim.sqlite3` if absent,
   then for **each known key** that `.env` lacks (absent or empty) and
   `/etc/environment` has, parses the value out and appends it — one log line
   per adopted key, then one summary line of which known keys are present and
   absent (names only, never values). Existing values are never overwritten,
   so a second run adopts nothing. Only a missing `ANTHROPIC_API_KEY` is a
   warning; the others are informational. Refuses if `.env` is somehow not
   gitignored.
4. If there is no vhost for `dndsim.lab980.com` yet, runs **`dndsim setup`**
   (below) first.
5. pm2: `pm2 start ecosystem.config.js --only dnd-sim` on first registration,
   `pm2 restart` after — both under `env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY
   -u GEMINI_API_KEY -u XAI_API_KEY -u MISTRAL_API_KEY -u DEEPSEEK_API_KEY
   -u SILICONFLOW_API_KEY -u DEEPINFRA_API_KEY -u GITHUB_TOKEN` (the unset
   list is built from the same known-key list as step 3, so a key added
   there is unset here too). Then `pm2 save`, but
   **only if the process reports `online`** (a save while it is down would
   persist a dump that omits it).
6. Probes `http://127.0.0.1:8071/api/health` and the public URL, prints the
   deployed commit, and warns if the health body says `"mock":true`.

**A (re)start kills any in-flight game.** On boot the app marks every game
still `running`/`paused`/`created` as `stopped`; its transcript stays
readable. Deploy between games if one matters.

### `dndsim setup` (box-outward half, once, idempotent)

- With lab980's `provision-site` on PATH and no vhost yet:
  `provision-site dndsim ivjames/dnd-sim --port 8071 --dir /var/www/dndsim`
  — DNS A record, vhost with the shared security-headers include, DNS wait,
  certbot. `--port` is not optional (it would otherwise allocate the next free
  port from 8060 and proxy to that); `--dir` is the existing checkout, which
  it detects and leaves alone. It also seeds `.env` with `PORT=` if there is
  none, which step 3 above then fills in.
- Without `provision-site`: installs `deploy/nginx-dndsim.conf` as the vhost
  (HTTP only, no headers include) and tells you to run
  `certbot --nginx -d dndsim.lab980.com --redirect` and `fix-security-headers --fix`.
- Either way, then **ensures the SSE block** (next section) is in every proxied
  `location` of the vhost, with backup under `/var/backups/dndsim-sse-*`,
  `nginx -t`, reload, and rollback on failure. `dndsim setup --dry-run` shows
  the diff without touching anything.

## Two constraints that survive everything

**SSE.** The spectator UI reads `/api/games/<id>/stream`. The vhost's proxied
location must carry `proxy_buffering off`, `proxy_cache off`,
`proxy_read_timeout 3600s` / `proxy_send_timeout 3600s`, `gzip off`,
`proxy_http_version 1.1` with `proxy_set_header Connection ""`, or nginx
buffers the stream and the browser sees nothing until the game ends.
`provision-site`'s generic vhost has the opposite settings, so `dndsim setup`
rewrites the location: it drops those directives and inserts one block between
`# dndsim-sse begin` / `# dndsim-sse end` markers, which it repairs in place
on every run. `dndsim status` reports whether the block is present.

**`instances: 1`** in `ecosystem.config.js` is a hard ceiling, with
`exec_mode: 'fork'` explicit. Games run as threads inside the web process and
SSE subscribers attach to its in-memory event bus; a second instance would
answer half the requests from a process that knows nothing about the game.

## Environment (`.env`, sourced by `run.sh`)

Only `ANTHROPIC_API_KEY` is required for the default live game (its DM is
Claude); the other platform keys are needed only by seats that use that
platform, and any subset is fine. Everything else has a default. `.env` values
override the process environment pm2 provides.

| key | what it is |
|---|---|
| `PORT` | `8071` — must match the vhost's `proxy_pass` |
| `HOST` | `127.0.0.1` — bind address; keep it loopback, nginx fronts it |
| `ANTHROPIC_API_KEY` | Anthropic (Claude) — the default DM; adopted from `/etc/environment` by `dndsim deploy`, warned about when missing |
| `OPENAI_API_KEY` | OpenAI — optional; adopted the same way |
| `GEMINI_API_KEY` | Google Gemini — optional; adopted the same way, also from a `GOOGLE_API_KEY` line in the store |
| `XAI_API_KEY` | xAI (Grok) — optional; adopted the same way |
| `MISTRAL_API_KEY` | Mistral — optional; adopted the same way |
| `DEEPSEEK_API_KEY` | DeepSeek — optional; adopted the same way |
| `SILICONFLOW_API_KEY` | SiliconFlow (international, `api.siliconflow.com`) — optional; adopted the same way; seats use `siliconflow:<id>` |
| `DEEPINFRA_API_KEY` | DeepInfra — optional; adopted the same way; seats use `deepinfra:<id>` |
| `AWS_ACCESS_KEY_ID` | Amazon Polly narration — optional; adopted the same way |
| `AWS_SECRET_ACCESS_KEY` | ditto |
| `AWS_REGION` | Polly region, e.g. `us-east-1`; adopted the same way. boto3 will not build a client without one |
| `DND_WRITE_TOKEN` | the shared secret the write routes require (`X-Dnd-Token` header): `POST /api/games`, `/note`, `/pause`, `/resume`, `/stop`. Unset → each answers 503 with `{"code": "writes_unconfigured"}`, and reading a game, listing games, the SSE stream, `/api/tts` and the narration hold stay anonymous. Set it with **`dndsim token`**, which writes it straight into `.env`; it is not kept in `/etc/environment`, being this app's own secret rather than one of the box's shared vendor keys. Unset for pm2's launch like every other key |
| `DND_TTS` | unset (auto) — `0` switches server voices off entirely; `1` turns them on even for mock games |
| `DND_TTS_ENGINE` | `neural` — Polly engine for the table ($16/1M). `standard`, `long-form` and `generative` also work; each is sent only the SSML it accepts |
| `DND_TTS_MONSTER_ENGINE` | unset — speaking monsters sit on `DND_TTS_ENGINE` with everyone else. They were held on `standard` ($4/1M) for `vocal-tract-length`; `tts/dsp.py` makes an ogre bigger than a goblin now, so name an engine here only if you want the split back for its own sake |
| `DND_TTS_MONSTER_FX` | `1` — post-process a speaking monster: a playback-rate size shift, and grit or a stone room for some of them. Its clip is asked for as `pcm` and served as `audio/wav`. `0` restores the old arrangement, `vocal-tract-length` on the standard engine, and with it the $4/1M rate for monster lines |
| `DND_TTS_LANG` | `en-US` — the language whose voices are cast from |
| `DND_TTS_DM_VOICE` | `Brian` — the DM's own voice; everyone else is dealt out of the rest |
| `DND_TTS_CACHE` | `<dir of DND_SIM_DB>/tts` — where synthesized clips live |
| `DND_TTS_CACHE_MB` | `512` — ceiling; least-recently-played clips are dropped past it |
| `DND_TTS_MAX_CHARS` | `400` — longest line the endpoint will synthesize (the browser chunks at 220) |
| `DND_TTS_MAX_USD` | `10.00` — server-owned ceiling on one game's spend before narration stops, regardless of the `budget_usd` its config asked for. It caps one game; `DND_WRITE_TOKEN` is what caps how many a stranger may start |
| `DND_SIM_MOCK` | unset in production; `1` → `MockLLMClient`, zero API calls |
| `DND_SIM_DB` | `/var/www/dndsim/data/dndsim.sqlite3` — SQLite transcript store |
| `DND_SIM_EXAMPLES` | `./examples` — where `/api/presets` reads scenarios from |
| `DND_DM_MODEL` | `claude-sonnet-5` — DM model |
| `DND_PLAYER_MODEL` | `claude-haiku-4-5-20251001` — player model |
| `DND_SUMMARY_MODEL` | defaults to the player model — rolling-summary model |
| `DND_SIM_LOGLEVEL` | `INFO` — server log level |

A seat whose platform has no key in `.env` fails at game creation with a
message naming the variable; the fix is to add that key to `/etc/environment`
and `dndsim deploy` (or to `.env` directly and `dndsim restart`).

To rotate a key: change it in `/etc/environment`, delete that key's line from
`.env` (adoption never overwrites an existing value), `dndsim deploy` (or
`dndsim restart` after editing `.env` directly — `run.sh` re-reads it on every
start). `dndsim keys` shows, names only, which known keys `.env` and
`/etc/environment` each hold, and exits 1 if `ANTHROPIC_API_KEY` is not in
`.env`.

## The write token

Creating a game, sending the table a DM note and pause/resume/stop require the
`X-Dnd-Token` header to match `DND_WRITE_TOKEN` (`web/auth.py`). Reading a game,
listing games, the SSE stream, `/api/tts`, the paid narration endpoint and the
narration hold stay anonymous — the public spectator UI is the product.

One command sets it and then uses it:

```bash
dndsim token          # generate → .env (mode 600) → pm2 restart so the app
                      # re-reads it → ask the running app whether it accepts it
                      # → print it, to paste into the page
dndsim token --show   # print the current one; changes nothing, restarts nothing
dndsim token --stdin  # use your own instead of a generated one, read from
                      # stdin — not an argument, because /proc/<pid>/cmdline is
                      # world-readable and the rest of this CLI avoids that
```

Rotating is the same command again: it replaces the line in `.env` rather than
appending a second one, so old secrets do not accumulate in the file. Every
browser then needs the new token pasted in again, which is what revocation
looks like here — there is no per-writer identity to revoke instead.

**It is not kept in `/etc/environment`.** That file exists so `dndsim deploy`
can adopt the box's *shared vendor* keys into `.env`; this secret is this app's
own, generated on the box, and a second copy would be one more place to leak it
from and one more to forget on a rotation. It stays on `KNOWN_KEYS` in
`bin/dndsim` only so pm2's launch unsets it and `keys`/`status` report it by
name.

Unset, it fails closed: the write routes answer `503 {"code":
"writes_unconfigured"}` and everything else — the page, every read, the stream,
a game already running — is unaffected. So a deploy that forgets it does not
take the site down; it just means nobody can start a game until `dndsim token`
has been run.

In the browser: the New game button, the pause/resume/stop row and the DM-note
form are not rendered until the page holds a token the server accepts. The
header button takes it (and, once unlocked, is how you Forget it again); it is
kept in that browser's `localStorage` only.

## Confirm what is live

```bash
dndsim status              # HEAD, pm2, .env + per-key presence, upstream family,
                           # vhost + SSE block, local + public /api/health, cert
dndsim keys                # per-key present/absent in .env and /etc/environment
                           # (names only); exit 1 if ANTHROPIC_API_KEY is missing
dndsim logs                # tail pm2 logs for this app
health-check --site dndsim # the droplet-wide auditor (lab980 repo)
```

A 200 proves the endpoint answered, not which build: `status`'s `checkout:`
line is the deployed commit — compare it with `origin/main`. And the stream
end to end, because a vhost that lost the SSE block passes every check above:

```bash
curl -N 'https://dndsim.lab980.com/api/games/<id>/stream?after=-1' | head -20
```

`event:` lines should appear *as the game plays*, plus `: hb` heartbeats every
15s. Everything arriving in one burst at the end means nginx is buffering.

### That the monsters can speak

`/api/tts` answering `available:true` says the table's engine has a roster; it
does not say Polly will read a **monster** line. Those are the only seats asked
for as `pcm` rather than `mp3` — their clips are post-processed (`tts/dsp.py`)
and `pcm` is the one format that can be worked on without a codec — and, with
`DND_TTS_MONSTER_FX=0`, the only ones writing `<amazon:effect
vocal-tract-length>` and routed to `DND_TTS_MONSTER_ENGINE` (standard) to be
allowed to. Polly errors on a tag or a sample rate its engine does not support
rather than ignoring it, so a wrong monster request is a 502 per line — which
the page answers by speaking that line in the spectator's own browser voice. The
game sounds fine. Nothing on the page says otherwise.

```bash
cd /var/www/dndsim && (set -a; . ./.env; set +a; .venv/bin/python -m tools.polly_check)
```

**Source `.env` — the parentheses and the `set -a` are the point.** The keys and
the `DND_TTS*` overrides live there and nowhere else in a shell's environment;
`run.sh` sources them exactly this way before it execs python, and a hand-run
`python -m` does not. Skip it and the check either reports exit 2 for missing
credentials on a server whose narration is working, or quietly tests the default
engines rather than the ones this deployment is configured for. The subshell is
so `set -a` does not follow you into the rest of your session. Its first line of
output names the engines and voice it is actually using — read it.

Two clips against real Polly — one table line, one monster line — reporting the
engine, voice, SSML, byte count and billed characters for each, and exiting
non-zero if either fails. It uses a temporary cache directory and no game, so
it neither fills `data/tts` nor charges a ledger; the two clips cost about
$0.0005. `--dry-run` prints the documents without sending them, `--out DIR`
keeps the clips to listen to, and `--ab` renders the monster line a second time
the *other* way — whichever of the treatment and the old standard-engine
`vocal-tract-length` this deployment is not configured for — so the two can be
played back to back. Whether the new one is better is a judgement; that is
where it is made.

When a monster line does fail, `dndsim logs` is the only place it shows:

```bash
dndsim logs | grep 'tts failed'    # names the seat, the engine and the voice
```

## Operations

| Task | Command |
|---|---|
| DB | `sqlite3 data/dndsim.sqlite3 'select id,status,cost_usd,title from games;'` (from the app dir) |
| wipe history | `pm2 stop dnd-sim`, delete `data/dndsim.sqlite3*`, `dndsim restart` |
| tests | `.venv/bin/python -m pytest -q` |
| cost safety | every game carries `budget_usd`; the orchestrator halts at `budget_exceeded`. When in doubt, `DND_SIM_MOCK=1`. |
| narration spend | Polly is charged to the same `budget_usd` as the model calls (`by_role.narrator` in the ledger), each seat at its own engine's rate. A game's whole narration is ~$0.45 on the shipped neural/standard split, ~$0.12 with `DND_TTS_ENGINE=standard`; `DND_TTS=0` removes it entirely. |
| clip cache | `du -sh data/tts` — safe to delete wholesale; the next listen re-synthesizes and re-pays. |
| voices sound wrong | `curl -s localhost:8071/api/tts` says whether Polly answered and which engine; `available:false` means the page is using each spectator's own browser voices. |
| monsters sound like everyone else | They are falling back to the browser's voices: `(set -a; . ./.env; set +a; .venv/bin/python -m tools.polly_check)` sends one real monster line and says why. `dndsim logs \| grep 'tts failed'` names the seat and engine of every refused line. |

## Overrides

`DNDSIM_FQDN` (default `dndsim.lab980.com`), `DNDSIM_BRANCH` (`main`),
`DNDSIM_PORT` (`8071`), `DNDSIM_KEY_SOURCE` (colon-separated list of files the
keys are adopted from, first hit wins; default `/etc/environment` alone —
any other file is consulted only because you named it here),
`DNDSIM_ENV_FILE` (`<app dir>/.env`), `DNDSIM_KEYS` (space-separated key names
to adopt, report and unset for pm2; default is the eleven known keys above —
setting it replaces the list, so include `ANTHROPIC_API_KEY`).
