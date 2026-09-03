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
8045), and the key: **`ANTHROPIC_API_KEY` stays in `/etc/environment`**, the
known-key store on this box, and the app reads it from **`/var/www/dndsim/.env`**,
which `dndsim deploy` writes once by copying the value across and `run.sh`
sources on every start. pm2 never sees the key — `deploy` launches it under
`env -u ANTHROPIC_API_KEY -u GITHUB_TOKEN`, so `~/.pm2/dump.pm2` does not carry
the root shell's copy — and the key is never on any argv or in any log line.

## Bring-up (on the droplet, as root)

The one hand step: make sure the key is in `/etc/environment`.

```bash
grep -q '^ANTHROPIC_API_KEY=' /etc/environment || echo 'ANTHROPIC_API_KEY=sk-ant-...' >> /etc/environment
```

Then:

```bash
git clone https://github.com/ivjames/dnd-sim /var/www/dndsim \
  && ln -sf /var/www/dndsim/bin/dndsim /usr/local/bin/dndsim \
  && dndsim deploy
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
   and if `.env` has no `ANTHROPIC_API_KEY`, parses it out of `/etc/environment`
   and appends it. Existing values are never overwritten. Refuses if `.env`
   is somehow not gitignored.
4. If there is no vhost for `dndsim.lab980.com` yet, runs **`dndsim setup`**
   (below) first.
5. pm2: `pm2 start ecosystem.config.js --only dnd-sim` on first registration,
   `pm2 restart` after — both under `env -u ANTHROPIC_API_KEY -u GITHUB_TOKEN`.
   Then `pm2 save`, but **only if the process reports `online`** (a save while
   it is down would persist a dump that omits it).
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

Only `ANTHROPIC_API_KEY` is required for live mode; everything else has a
default. `.env` values override the process environment pm2 provides.

| key | what it is |
|---|---|
| `PORT` | `8071` — must match the vhost's `proxy_pass` |
| `HOST` | `127.0.0.1` — bind address; keep it loopback, nginx fronts it |
| `ANTHROPIC_API_KEY` | required for live mode; adopted from `/etc/environment` by `dndsim deploy` |
| `DND_SIM_MOCK` | unset in production; `1` → `MockLLMClient`, zero API calls |
| `DND_SIM_DB` | `/var/www/dndsim/data/dndsim.sqlite3` — SQLite transcript store |
| `DND_SIM_EXAMPLES` | `./examples` — where `/api/presets` reads scenarios from |
| `DND_DM_MODEL` | `claude-sonnet-5` — DM model |
| `DND_PLAYER_MODEL` | `claude-haiku-4-5-20251001` — player model |
| `DND_SUMMARY_MODEL` | defaults to the player model — rolling-summary model |
| `DND_SIM_LOGLEVEL` | `INFO` — server log level |

To rotate the key: change it in `/etc/environment`, delete the
`ANTHROPIC_API_KEY=` line from `.env`, `dndsim deploy` (or `dndsim restart`
after editing `.env` directly — `run.sh` re-reads it on every start).

## Confirm what is live

```bash
dndsim status              # HEAD, pm2, .env/key presence, upstream family,
                           # vhost + SSE block, local + public /api/health, cert
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

## Operations

| Task | Command |
|---|---|
| DB | `sqlite3 data/dndsim.sqlite3 'select id,status,cost_usd,title from games;'` (from the app dir) |
| wipe history | `pm2 stop dnd-sim`, delete `data/dndsim.sqlite3*`, `dndsim restart` |
| tests | `.venv/bin/python -m pytest -q` |
| cost safety | every game carries `budget_usd`; the orchestrator halts at `budget_exceeded`. When in doubt, `DND_SIM_MOCK=1`. |

## Overrides

`DNDSIM_FQDN` (default `dndsim.lab980.com`), `DNDSIM_BRANCH` (`main`),
`DNDSIM_PORT` (`8071`), `DNDSIM_KEY_SOURCE` (`/etc/environment`),
`DNDSIM_ENV_FILE` (`<app dir>/.env`).
