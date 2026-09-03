// PM2 process definition for dnd-sim (lab980 protocol).
//
//   cd /var/www/dndsim && pm2 start ecosystem.config.js && pm2 save
//
// ANTHROPIC_API_KEY is NOT set here. It lives in /etc/environment on the host;
// PM2 inherits it from the shell that starts it, so start PM2 from a login
// shell (or `pm2 restart dnd-sim --update-env` after sourcing /etc/environment).
// Never commit the key to this file.
module.exports = {
  apps: [
    {
      name: 'dnd-sim',
      cwd: '/var/www/dndsim',            // <-- absolute path to the checkout
      script: './run.sh',             // picks .venv/bin/python, execs `python -m web.app`
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
        // DND_SIM_MOCK: '1',         // uncomment to run without touching the API
        // DND_DM_MODEL / DND_PLAYER_MODEL / DND_SUMMARY_MODEL override the defaults
      },
      out_file: '/var/log/pm2/dnd-sim.out.log',
      error_file: '/var/log/pm2/dnd-sim.err.log',
      merge_logs: true,
      time: true,
    },
  ],
};
