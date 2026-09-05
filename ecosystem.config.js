// PM2 process definition for dnd-sim (lab980 protocol).
//
// Started and restarted by `dndsim deploy` (bin/dndsim) — never by hand:
//   env -i PATH="$PATH" HOME="$HOME" [PM2_HOME=…] [TERM=…] LANG=C.UTF-8 \
//       pm2 start ecosystem.config.js --only dnd-sim
//   (pm2_clean in bin/dndsim; every pm2 command the CLI runs goes through it,
//   including the `pm2 jlist` that spawns the daemon when it is down)
//
// The platform API keys, the AWS credentials Polly narration uses and the
// write token — the list is KNOWN_KEYS in bin/dndsim — are NOT set here and
// are NOT inherited from the shell. They live in /var/www/dndsim/.env (mode
// 600, gitignored) and nowhere else on the box — put there by hand; `dndsim
// deploy` seeds the non-secret settings and copies no key in from anywhere —
// and run.sh sources that file before exec'ing python. pm2
// gives the process the environment of the command that started it, and
// `pm2 save` writes that into ~/.pm2/dump.pm2 — so the allowlist on the
// start/restart above is what keeps a known key, or anything else the
// calling shell holds, out of both. (The other pm2 commands go through the
// same wrapper as hygiene for the daemon's own environ.) Nothing else from
// the shell reaches the process either: TZ, proxy variables, a DND_* override
// belong in .env. Values in .env override the env block below. Never commit
// a key to this file.
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
        // DND_TTS: '0',              // no Polly narration; the browser's own voices instead
        // DND_TTS_ENGINE / DND_TTS_REGION / DND_TTS_DM_VOICE / DND_TTS_CACHE_MB — see DEPLOY.md
      },
      out_file: '/var/log/pm2/dnd-sim.out.log',
      error_file: '/var/log/pm2/dnd-sim.err.log',
      merge_logs: true,
      time: true,
    },
  ],
};
