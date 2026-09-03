# Deploying dnd-sim on lab980

Target: `dndsim.lab980.com` → `127.0.0.1:8071`, PM2 process `dnd-sim`.

Order matters: **app first, then nginx over HTTP, then DNS, then certbot.**
Never ship an SSL server block before certbot has run.

## 1. Check the port is free

```sh
sudo ss -lntp | grep 8071     # expect nothing (8070 is ffc's centeredge mock)
```

## 2. Clone and build

```sh
sudo mkdir -p /var/www/dndsim
git clone <repo> /var/www/dndsim
cd /var/www/dndsim
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
mkdir -p data
chmod +x run.sh
```

## 3. Smoke test before PM2

```sh
cd /var/www/dndsim
DND_SIM_MOCK=1 PORT=8071 ./run.sh &
curl -s localhost:8071/api/health      # {"ok":true,"mock":true,"games_running":0}
kill %1
```

## 4. API key

The key lives in `/etc/environment` (same as the other lab980 Python apps):

```sh
sudo sh -c 'grep -q ANTHROPIC_API_KEY /etc/environment || echo "ANTHROPIC_API_KEY=sk-ant-..." >> /etc/environment'
```

`/etc/environment` is read at login, so a running PM2 daemon will not see a new
key until you re-source it and update the env:

```sh
set -a; . /etc/environment; set +a
pm2 restart dnd-sim --update-env
```

## 5. PM2

```sh
cd /var/www/dndsim
pm2 start ecosystem.config.js
pm2 save
pm2 logs dnd-sim --lines 50
```

`instances` must stay **1**. Games run as threads inside the web process and
SSE subscribers attach to that process's in-memory event bus; a second instance
would serve half the requests from a process that knows nothing about the game.

A restart kills any in-flight game. That is expected: on boot the app marks
every DB game still in `running`/`paused`/`created` as `stopped`, and its
transcript stays readable.

## 6. nginx (HTTP only)

```sh
sudo cp deploy/nginx-dndsim.conf /etc/nginx/sites-available/dndsim
sudo ln -s /etc/nginx/sites-available/dndsim /etc/nginx/sites-enabled/dndsim
sudo nginx -t && sudo systemctl reload nginx
curl -s -H 'Host: dndsim.lab980.com' localhost/api/health
```

## 7. DNS, then TLS

Point `dndsim.lab980.com` at the box. Confirm `http://dndsim.lab980.com/`
loads the spectator UI **over plain HTTP**, then:

```sh
sudo certbot --nginx -d dndsim.lab980.com
sudo nginx -t && sudo systemctl reload nginx
```

Certbot adds the 443 block and the redirect to the file from step 6. Re-check
the SSE proxy settings survived (`proxy_buffering off`, `proxy_read_timeout
3600s`) — they should, certbot only appends.

## 8. Verify the stream end to end

```sh
curl -N -H 'Host: dndsim.lab980.com' \
  'http://127.0.0.1/api/games/<id>/stream?after=-1' | head -20
```

You should see `event: ...` lines appear *as the game plays*, plus `: hb`
heartbeat comments every 15s. If everything arrives in one burst at the end,
nginx is still buffering.

## Operations

| Task | Command |
|---|---|
| logs | `pm2 logs dnd-sim` |
| restart | `pm2 restart dnd-sim --update-env` |
| DB | `sqlite3 /var/www/dndsim/data/dndsim.sqlite3 'select id,status,cost_usd,title from games;'` |
| wipe history | stop PM2, delete `data/dndsim.sqlite3*`, start |
| tests | `.venv/bin/python -m pytest -q` |

## Cost safety

Every game carries `budget_usd`; the orchestrator halts at `budget_exceeded`.
The spectator UI shows spend against budget in the top bar. When in doubt run
with `DND_SIM_MOCK=1` — no API calls at all.
