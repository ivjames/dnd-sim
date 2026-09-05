# Deploying dnd-sim on lab980

This file used to be the hand runbook. It is kept so old links resolve; the
content moved to [`../DEPLOY.md`](../DEPLOY.md), and the runbook itself is now
`bin/dndsim`. On the droplet, as root:

```sh
git clone https://github.com/ivjames/dnd-sim /var/www/dndsim \
  && ln -sf /var/www/dndsim/bin/dndsim /usr/local/bin/dndsim \
  && dndsim deploy
```

`dndsim deploy` syncs the checkout, installs into `.venv`, writes `.env`
(seeding the settings; `ANTHROPIC_API_KEY` and any other platform key — see
`DEPLOY.md` — go in by hand with an editor, then `dndsim restart`), provisions the vhost via
`dndsim setup` when it is missing (falling back to `nginx-dndsim.conf` in this
directory when `provision-site` is unavailable), keeps the SSE block in the
vhost, starts or restarts pm2 process `dnd-sim`, and probes `/api/health`.
Every deploy after the first is the same command.
