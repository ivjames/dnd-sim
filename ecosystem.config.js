// PM2 process definition for dnd-sim (lab980 protocol).
//
// Started and restarted by `dndsim deploy` (bin/dndsim) — never by hand:
//   env -u ANTHROPIC_API_KEY -u GITHUB_TOKEN pm2 start ecosystem.config.js --only dnd-sim
//
// ANTHROPIC_API_KEY is NOT set here and is NOT inherited from the shell.
// It lives in /var/www/dndsim/.env (mode 600, gitignored), which `dndsim
// deploy` writes by copying the value out of /etc/environment, and which
// run.sh sources before exec'ing python. pm2 is deliberately launched with
// the key unset so ~/.pm2/dump.pm2 never carries it. Values in .env override
// the env block below. Never commit the key to this file.
module.exports = {
  apps: [
    {
      name: 'dnd-sim',
      cwd: '/var/www/dndsim',            // <-- absolute path to the checkout
      script: './run.sh',             // sources ./.env, picks .venv/bin/python, execs `python -m web.app`
      interpreter: '/bin/sh',
      instances: 1,                   // MUST stay 1: games live in-process, SSE is stateful
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      restart_delay: 4000,
      max_memory_restart: '600M',
      kill_timeout: 8000,             // let in-flight SSE writes drain
      env: {
        PORT: '8071',
        HOST: '127.0.0.1',
        DND_SIM_DB: '/var/www/dndsim/data/dndsim.sqlite3',
        PYTHONUNBUFFERED: '1',
        // DND_SIM_MOCK: '1',         // uncomment (or set in .env) to run without touching the API
        // DND_DM_MODEL / DND_PLAYER_MODEL / DND_SUMMARY_MODEL override the defaults
      },
      out_file: '/var/log/pm2/dnd-sim.out.log',
      error_file: '/var/log/pm2/dnd-sim.err.log',
      merge_logs: true,
      time: true,
    },
  ],
};
