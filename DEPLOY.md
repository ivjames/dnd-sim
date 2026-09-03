# Deploying dnd-sim

Target: **https://dndsim.lab980.com** — served from the lab980 droplet (conventions in
the `ivjames/lab980.com` repo's `CLAUDE.md`).

Shape: nginx proxies to a pm2-managed **Python/Flask** process on
`127.0.0.1:8071`. This is not a Node app: pm2 runs `./run.sh`, which execs
`.venv/bin/python -m web.app`. There is no `package.json`, no `npm ci` and no
build step — `pip install -r requirements.txt` into `.venv` is the whole
install. The pm2 process is named **`dnd-sim`** (from `ecosystem.config.js`),
not `dndsim`.

## Open decisions (conflicts with lab980 conventions)

This repo was authored with its own deploy runbook (`deploy/INSTALL.md`,
`deploy/nginx-dndsim.conf`, `ecosystem.config.js`) before the lab980
conventions were scaffolded in, and the two disagree on three points. Nothing
here resolves them — none of those files have been edited — and **until each
is decided, `deploy/INSTALL.md` is the authored runbook and the bring-up block
below is the conventions-shaped alternative.** Decide, then make the loser
match the winner in the same PR.

1. **Checkout dir.** `deploy/INSTALL.md` clones to `/opt/dnd-sim`, and
   `ecosystem.config.js` hardcodes it twice (`cwd` and `DND_SIM_DB`). lab980
   says every site lives at `/var/www/<stub>` = **`/var/www/dndsim`**: that is
   what `provision-site` creates, what `health-check` walks, and what
   `.claude/sites.json` records (flagged unverified). Choosing `/var/www/dndsim`
   means changing those two lines in `ecosystem.config.js` and the paths in
   `deploy/INSTALL.md`. Choosing `/opt/dnd-sim` makes this the second site on
   the box outside `/var/www` (after photos) and leaves `health-check` unable
   to match its pm2 entry to a site dir. `bin/dndsim` derives its root from its
   own path, so it works under either.

2. **Port — decided: 8071** (2026-09-03). The repo was authored on 8045,
   "next after qa-engine's 8044", which is below lab980's **8060+** range.
   A scan of the droplet (listening sockets + nginx `proxy_pass` targets +
   the registry) showed 8060–8070 and 8081 in use and 8071 the first free
   port, so 8071 it is — PLAN.md, README, INSTALL.md, the vhost,
   `ecosystem.config.js`, `run.sh`, `web/app.py`'s default and `bin/dndsim`'s
   `DNDSIM_PORT` all say so now. `provision-site --port 8071` must still be
   passed explicitly, or it allocates its own and the vhost proxies to a port
   nothing listens on.

3. **Secrets.** `deploy/INSTALL.md` puts `ANTHROPIC_API_KEY` in
   `/etc/environment` and has pm2 inherit it from a login shell. lab980 keeps
   config in a local **`.env` in the app dir**, not `/etc`, and its Engineering
   lessons warn that pm2 snapshots the launching shell's *whole* environment
   into `~/.pm2/dump.pm2` — a key in `/etc/environment` is in every root login
   shell, and so in every pm2 process ever started from one, not just this
   app's. Two facts to hold while deciding: nothing in this app reads a `.env`
   file (no `python-dotenv` in `requirements.txt`; `run.sh` sources nothing;
   `web/app.py` reads `os.environ` only), so a `.env` here is inert unless
   `run.sh` sources it or pm2 is started from a shell that has. And this
   process needs the key in its own env either way, so it lands in the dump
   regardless — the lesson's point is to keep *other* secrets out
   (`env -u GITHUB_TOKEN ... pm2 start`).

Also, not a conflict but a constraint on all three: `deploy/INSTALL.md`'s vhost
is hand-written HTTP-first (`deploy/nginx-dndsim.conf`, certbot run afterwards),
whereas `provision-site` writes the proxy vhost with the shared
`lab980-security-headers.conf` include and runs certbot itself. **The
SSE-specific proxy settings in `deploy/nginx-dndsim.conf` must survive whichever
path is chosen** — `proxy_buffering off`, `proxy_cache off`,
`proxy_read_timeout 3600s` / `proxy_send_timeout 3600s`, `gzip off`,
`proxy_http_version 1.1` with `Connection ""` — because without them nginx
buffers the spectator stream and the browser sees nothing until the game ends.
If `provision-site` writes the vhost, paste that block into its `location /`
before TLS is added, and re-check it after certbot has run.

## One-time bring-up (on the droplet, as root) — conventions-shaped

Read "Open decisions" first. This block assumes `/var/www/dndsim`, port 8071
and a local `.env`; the repo's `ecosystem.config.js` does not yet agree with
the first of those, so `pm2 start ecosystem.config.js` from `/var/www/dndsim`
will run the app from `/opt/dnd-sim` (and fail) until decision 1 lands.

```bash
provision-site dndsim ivjames/dnd-sim --port 8071
cd /var/www/dndsim
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
mkdir -p data
$EDITOR .env                         # provision-site seeded PORT; add the rest (table below)

# carry the SSE block from deploy/nginx-dndsim.conf into the vhost's location /
$EDITOR /etc/nginx/sites-available/dndsim.lab980.com && nginx -t && systemctl reload nginx

# smoke test in mock mode before pm2 — no key, no API calls
DND_SIM_MOCK=1 PORT=8071 ./run.sh & sleep 2
curl -s 127.0.0.1:8071/api/health    # {"ok":true,"mock":true,"games_running":0}
kill %1

# the app reads process env only (decision 3): export .env into this shell,
# then start pm2 from it, dropping anything the app does not need
set -a; . ./.env; set +a
env -u GITHUB_TOKEN pm2 start ecosystem.config.js   # process name: dnd-sim
pm2 list                             # EVERY app online before saving
pm2 save
ln -sf /var/www/dndsim/bin/dndsim /usr/local/bin/dndsim
dndsim status
```

`provision-site` stops before install/run on purpose — each site is deployed
its own way afterward.

Two details in that first line matter more than they look:

- **`--port 8071` is not optional.** Without it `provision-site` picks the
  next free port from 8060 and writes *that* into the vhost, while this repo's
  CLI, `.env` and app config all use `8071`. nginx then proxies to a port
  nothing is listening on and every request is a 502 that looks like the app is
  down while it runs perfectly on the wrong port.
- **`provision-site` seeds `.env` with `PORT=` itself** (only if there isn't one
  already, mode 600). Add the remaining keys to that file — don't `cp` over it,
  or the port goes back out of sync.

Reboot survival needs the pm2 boot hook installed **once per droplet**
(`pm2 startup systemd -u root --hp /root`, then run the line it prints; verify
`systemctl is-enabled pm2-root` → enabled). `pm2 save` alone only writes the
dump — nothing replays it at boot without the hook. Confirm every app is
`online` before `pm2 save`: it overwrites the dump with whatever is running.

`ecosystem.config.js` sets `instances: 1` alongside `exec_mode: 'fork'`. The
explicit fork keeps it out of cluster mode, and **1 is a hard ceiling**: games
run as threads inside the web process and SSE subscribers attach to that
process's in-memory event bus, so a second instance would answer half the
requests from a process that knows nothing about the game.

### `.env` keys

From README's environment-variable table. Only `ANTHROPIC_API_KEY` is required
for live mode; everything else has a default. Remember that the app reads the
process environment, not this file (decision 3).

| key | what it is |
|---|---|
| `PORT` | `8071` — must match the vhost's `proxy_pass` (seeded by `provision-site`) |
| `HOST` | `127.0.0.1` — bind address; keep it loopback, nginx fronts it |
| `ANTHROPIC_API_KEY` | required for live mode; the real money switch |
| `DND_SIM_MOCK` | unset in production; `1` → `MockLLMClient`, zero API calls |
| `DND_SIM_DB` | `./data/dndsim.sqlite3` — SQLite transcript store (`ecosystem.config.js` sets an absolute path) |
| `DND_SIM_EXAMPLES` | `./examples` — where `/api/presets` reads scenarios from |
| `DND_DM_MODEL` | `claude-sonnet-5` — DM model |
| `DND_PLAYER_MODEL` | `claude-haiku-4-5-20251001` — player model |
| `DND_SUMMARY_MODEL` | defaults to the player model — rolling-summary model |
| `DND_SIM_LOGLEVEL` | `INFO` — server log level |

## Deploying updates

Land changes on `main` (via a PR — see `CLAUDE.md`), then on the droplet:

```bash
dndsim deploy        # sync, pip install -r requirements.txt, pm2 restart, probe
```

**How `deploy` syncs, since the conventions file sends you here for it:**
`git fetch` then `git reset --hard origin/<branch>`. A tracked file edited on
the droplet is destroyed silently on the next deploy — fix it in the repo. The
gitignored state is the exception and survives: `.venv/`, `.env` and `data/`
are meant to live on the box.

**A restart kills any in-flight game.** That is expected: on boot the app
marks every DB game still in `running`/`paused`/`created` as `stopped`, and
its transcript stays readable. Deploy between games if one matters.

## Check it

```bash
dndsim status              # HEAD, pm2 state, local + public /api/health, cert
dndsim logs                # tail pm2 logs for this app
health-check --site dndsim # the droplet-wide auditor
```

And the stream end to end, because a vhost that lost the SSE block passes every
check above and still shows spectators nothing:

```bash
curl -N 'https://dndsim.lab980.com/api/games/<id>/stream?after=-1' | head -20
```

You should see `event: ...` lines appear *as the game plays*, plus `: hb`
heartbeat comments every 15s. If everything arrives in one burst at the end,
nginx is buffering.

## Operations

| Task | Command |
|---|---|
| DB | `sqlite3 data/dndsim.sqlite3 'select id,status,cost_usd,title from games;'` (from the app dir) |
| wipe history | `pm2 stop dnd-sim`, delete `data/dndsim.sqlite3*`, `pm2 start dnd-sim` |
| tests | `.venv/bin/python -m pytest -q` |
| cost safety | every game carries `budget_usd`; the orchestrator halts at `budget_exceeded`. When in doubt, `DND_SIM_MOCK=1`. |

## Overrides

- `DNDSIM_FQDN` — default `dndsim.lab980.com`
- `DNDSIM_BRANCH` — default `main`
- `DNDSIM_PORT` — default `8071`
