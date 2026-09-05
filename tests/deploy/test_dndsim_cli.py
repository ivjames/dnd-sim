"""`bin/dndsim` — the parts of deploy/setup/restart that have to fail loudly.

Same technique as test_dndsim_token.py: the script is sourced so its functions
can be called against temp files, and the commands it shells out to — pm2,
curl, nginx, systemctl, provision-site, and python3 where the SSE rewrite is
the thing under test — are replaced by shims on PATH that record what they
were given. What is checked:

- an option a command does not know is an error, not silently ignored;
- the SSE rewrite is refused when it fails or comes back empty/truncated,
  and the vhost is left untouched (an empty vhost passes `nginx -t`);
- `probe` returns non-zero unless the local health check answered 200, and
  deploy/restart exit on that instead of printing `deployed:` regardless;
- every pm2 command runs under `env -i` with an allowlist, so nothing exported
  in the caller — known key or not — reaches pm2;
- `pm2 jlist` failing stops a deploy rather than being read as "not
  registered" (a `pm2 start` on a registered process is a silent no-op);
- `ensure_env` never adopts the write token from the store;
- `deploy` survives a failing `setup` (subshell), and repairs the SSE block.

The deploy tests run against a throwaway clone of this repo, so the
`git reset --hard` inside `cmd_deploy` never touches the working tree.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(ROOT, "bin", "dndsim")
FALLBACK_VHOST = os.path.join(ROOT, "deploy", "nginx-dndsim.conf")
SSE_BEGIN = "# dndsim-sse begin"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="this path calls need_root")


def run(body: str, cli: str = CLI, path: str = "", **env: str) -> subprocess.CompletedProcess:
    """Source the CLI (so its functions are defined) and run `body`.

    `path` is prepended to PATH so shims win; the rest of PATH stays, because
    the script needs the real coreutils/git/python3 for everything else.
    """
    script = 'source "%s" >/dev/null 2>&1\n%s' % (cli, body)
    full = {**os.environ, **env}
    if path:
        full["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=120, env=full)


def read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def write(p: str, body: str) -> None:
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)


def shim(bindir: str, name: str, body: str) -> None:
    """A stub command on PATH. `body` is the shell after `#!/bin/sh`."""
    p = os.path.join(bindir, name)
    write(p, "#!/bin/sh\n" + body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def recorder(bindir: str, name: str, logfile: str, tail: str = "exit 0\n") -> None:
    """A shim that appends its argv to `logfile`, then runs `tail`."""
    shim(bindir, name, 'printf \'%s\\n\' "$*" >> "{}"\n{}'.format(logfile, tail))


def vhost_without_block() -> str:
    """The fallback vhost with the managed block stripped: what provision-site
    (or a hand edit) leaves behind, and what ensure_sse has to repair."""
    src = read(FALLBACK_VHOST)
    return re.sub(r"[ \t]*# dndsim-sse begin.*?# dndsim-sse end\n", "", src, flags=re.S)


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd, flag", [
    ("cmd_deploy", "--bogus"),
    ("cmd_deploy", "--no-instal"),
    ("cmd_setup", "--dry"),
    ("cmd_setup", "--no-install"),
    ("cmd_restart", "--now"),
])
def test_an_unknown_option_is_an_error(cmd, flag):
    out = run("%s %s" % (cmd, flag))
    assert out.returncode != 0
    assert "unknown option" in out.stderr and flag in out.stderr


def test_the_known_options_are_still_accepted_before_anything_runs(tmp_path):
    """`--dry-run` on a box with no vhost and no provision-site just reports;
    that is the one setup path that needs neither root nor nginx."""
    avail = tmp_path / "avail"; avail.mkdir()
    enabled = tmp_path / "enabled"; enabled.mkdir()
    out = run("cmd_setup --dry-run", NGINX_AVAIL=str(avail), NGINX_ENABLED=str(enabled),
              DNDSIM_FQDN="test.example")
    assert out.returncode == 0, out.stderr
    assert "dry run" in out.stdout


# ---------------------------------------------------------------------------
# ensure_sse: refuse a bad rewrite, keep the vhost
# ---------------------------------------------------------------------------

def sse_fixture(tmp_path, python3_body: str | None, nginx_tail: str = "exit 0\n"):
    """A vhost under a temp sites-available, shims for nginx/systemctl, and —
    when `python3_body` is given — a python3 shim standing in for the rewrite."""
    bindir = tmp_path / "bin"; bindir.mkdir()
    avail = tmp_path / "avail"; avail.mkdir()
    enabled = tmp_path / "enabled"; enabled.mkdir()
    backups = tmp_path / "backups"
    calls = str(tmp_path / "calls.log")
    vhost = avail / "test.example"
    write(str(vhost), vhost_without_block())
    recorder(str(bindir), "nginx", calls, nginx_tail)
    recorder(str(bindir), "systemctl", calls)
    if python3_body is not None:
        shim(str(bindir), "python3", python3_body)
    env = dict(NGINX_AVAIL=str(avail), NGINX_ENABLED=str(enabled), BACKUP_ROOT=str(backups),
               DNDSIM_FQDN="test.example")
    return bindir, vhost, calls, env


def test_a_failing_rewrite_leaves_the_vhost_untouched(tmp_path):
    bindir, vhost, calls, env = sse_fixture(tmp_path, "exit 1\n")
    before = read(str(vhost))
    out = run("ensure_sse", path=str(bindir), **env)
    assert out.returncode != 0
    assert "untouched" in out.stderr
    assert read(str(vhost)) == before
    assert not os.path.exists(calls), "nginx/systemctl must not be touched"


def test_an_empty_rewrite_is_refused_even_with_exit_0(tmp_path):
    """The case that used to slip through: exit 0, nothing on stdout, and an
    empty file passes `nginx -t`."""
    bindir, vhost, calls, env = sse_fixture(tmp_path, "exit 0\n")
    before = read(str(vhost))
    out = run("ensure_sse", path=str(bindir), **env)
    assert out.returncode != 0
    assert "empty or truncated" in out.stderr
    assert read(str(vhost)) == before
    assert not os.path.exists(calls)


def test_a_truncated_rewrite_is_refused(tmp_path):
    bindir, vhost, calls, env = sse_fixture(tmp_path, "printf 'server {\\n}\\n'\n")
    before = read(str(vhost))
    out = run("ensure_sse", path=str(bindir), **env)
    assert out.returncode != 0
    assert "empty or truncated" in out.stderr
    assert read(str(vhost)) == before


def test_dry_run_reports_a_failing_rewrite_too(tmp_path):
    bindir, vhost, calls, env = sse_fixture(tmp_path, "exit 1\n")
    out = run("ensure_sse --dry-run", path=str(bindir), **env)
    assert out.returncode != 0
    assert read(str(vhost)) == vhost_without_block()


@needs_root
def test_the_real_rewrite_inserts_the_block_and_reloads(tmp_path):
    bindir, vhost, calls, env = sse_fixture(tmp_path, None)
    out = run("ensure_sse", path=str(bindir), **env)
    assert out.returncode == 0, out.stderr
    body = read(str(vhost))
    assert SSE_BEGIN in body and "proxy_buffering off;" in body
    assert read(calls).splitlines() == ["-t", "reload nginx"]
    backups = [f for _, _, fs in os.walk(str(tmp_path / "backups")) for f in fs]
    assert backups == ["test.example"], "the original goes under BACKUP_ROOT first"
    # ...and a second run changes nothing.
    again = run("ensure_sse", path=str(bindir), **env)
    assert again.returncode == 0 and "nothing to change" in again.stdout
    assert read(str(vhost)) == body


@needs_root
def test_a_rejected_rewrite_is_rolled_back(tmp_path):
    bindir, vhost, calls, env = sse_fixture(tmp_path, None, nginx_tail="exit 1\n")
    before = read(str(vhost))
    out = run("ensure_sse", path=str(bindir), **env)
    assert out.returncode != 0
    assert "rolling back" in out.stderr
    assert read(str(vhost)) == before
    assert "reload nginx" not in read(calls)


# ---------------------------------------------------------------------------
# probe: exit status follows the local health check
# ---------------------------------------------------------------------------

def curl_shim(bindir: str, code: str, body: str = '{"ok":true,"mock":false}') -> None:
    """curl as the script uses it: `-w '%{http_code}'` calls get the code,
    plain body calls get the body."""
    shim(bindir, "curl", 'case "$*" in *"-w "*) printf \'%s\' "{}";; *) printf \'%s\' \'{}\';; esac\n'
         .format(code, body))


def test_probe_fails_on_a_non_200_local_answer(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    curl_shim(str(bindir), "503", '{"ok":false}')
    out = run("probe 1", path=str(bindir), DNDSIM_PORT="8099")
    assert out.returncode == 1
    assert "HTTP 503" in out.stdout
    assert "public:" in out.stdout, "the public line is printed regardless"


def test_probe_fails_when_nothing_answers(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    shim(str(bindir), "curl", "exit 7\n")
    out = run("probe 1", path=str(bindir), DNDSIM_PORT="8099")
    assert out.returncode == 1
    assert "unreachable" in out.stdout


def test_probe_passes_on_200(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    curl_shim(str(bindir), "200")
    out = run("probe 1", path=str(bindir), DNDSIM_PORT="8099")
    assert out.returncode == 0, out.stderr
    assert "HTTP 200" in out.stdout


def test_probe_warns_about_mock_mode(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    curl_shim(str(bindir), "200", '{"ok":true,"mock":true}')
    out = run("probe 1", path=str(bindir), DNDSIM_PORT="8099")
    assert out.returncode == 0
    assert "MOCK" in out.stderr


# ---------------------------------------------------------------------------
# pm2: an allowlisted environment, and a readable state or no start at all
# ---------------------------------------------------------------------------

def pm2_shim(bindir: str, envdump: str, argslog: str, jlist: str = "echo '[]'",
             tail: str = "exit 0") -> None:
    """pm2 that records the environment it received and its argv; `jlist`
    is the shell that answers that subcommand."""
    shim(bindir, "pm2",
         'env | sort > "{dump}"\nprintf \'%s\\n\' "$*" >> "{log}"\n'
         'case "$1" in jlist) {jlist};; esac\n{tail}\n'
         .format(dump=envdump, log=argslog, jlist=jlist, tail=tail))


LEAKS = dict(ANTHROPIC_API_KEY="sk-ant-leak", CARTESIA_API_KEY="cart-leak",
             GITHUB_TOKEN="gh-leak", DND_WRITE_TOKEN="tok-leak",
             SOME_OTHER_SECRET="whatever-is-in-etc-environment-tomorrow")


def test_pm2_never_sees_the_callers_environment(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    dump = str(tmp_path / "env.out"); log = str(tmp_path / "args.log")
    pm2_shim(str(bindir), dump, log)
    out = run("pm2_clean logs dnd-sim --lines 5", path=str(bindir), TERM="xterm", **LEAKS)
    assert out.returncode == 0, out.stderr
    seen = read(dump)
    for name in LEAKS:
        assert name + "=" not in seen, name + " reached pm2"
    names = {ln.split("=", 1)[0] for ln in seen.splitlines()}
    # what pm2 does need: where it is, where its state lives, a terminal.
    assert {"PATH", "HOME", "LANG", "TERM"} <= names
    # ...and nothing beyond the allowlist (PWD is the stub's own shell).
    assert names - {"PATH", "HOME", "PM2_HOME", "TERM", "LANG", "PWD", "SHLVL", "_"} == set()
    assert read(log).strip() == "logs dnd-sim --lines 5"


def test_every_pm2_command_in_the_script_goes_through_pm2_clean():
    """The first pm2 command of a deploy spawns the daemon with whatever
    environment it is given; a bare `pm2 jlist` there would undo the rest."""
    src = read(CLI)
    src = re.sub(r"cat <<'USAGE'.*?\nUSAGE\n", "", src, flags=re.S)   # the help text
    src = re.sub(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', '""', src)         # message strings
    bare = re.compile(r"(^|[;&|(])\s*pm2\s")
    offenders = [ln for ln in src.splitlines()
                 if not ln.lstrip().startswith("#") and bare.search(ln)]
    assert offenders == [], offenders
    # ...and the sanity check on the check: the one real invocation is in pm2_clean.
    assert re.search(r"env -i .* pm2 \"\$@\"", read(CLI))


def test_pm2_state_reads_past_the_daemon_spawn_preamble(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    dump = str(tmp_path / "env.out"); log = str(tmp_path / "args.log")
    pm2_shim(str(bindir), dump, log, jlist=(
        "printf '[PM2] Spawning PM2 daemon with pm2_home=/root/.pm2\\n"
        "[PM2] PM2 Successfully daemonized\\n"
        "[{\"name\":\"dnd-sim\",\"pm2_env\":{\"status\":\"online\",\"exec_mode\":\"fork_mode\","
        "\"restart_time\":0,\"pm_uptime\":1}}]\\n'"))
    out = run("pm2_state", path=str(bindir))
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("online  mode=fork_mode")


def test_pm2_state_says_not_registered_for_an_empty_list(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    pm2_shim(str(bindir), str(tmp_path / "e"), str(tmp_path / "a"))
    out = run("pm2_state", path=str(bindir))
    assert out.returncode == 0 and out.stdout.strip() == "not registered"


@pytest.mark.parametrize("jlist", ["exit 3", "echo 'not a list'", "printf ''"])
def test_pm2_state_fails_rather_than_guess(tmp_path, jlist):
    bindir = tmp_path / "bin"; bindir.mkdir()
    pm2_shim(str(bindir), str(tmp_path / "e"), str(tmp_path / "a"), jlist=jlist)
    out = run("pm2_state", path=str(bindir))
    assert out.returncode == 1
    assert out.stdout.startswith("unknown")


# ---------------------------------------------------------------------------
# ensure_env: the write token is never adopted
# ---------------------------------------------------------------------------

def test_ensure_env_adopts_vendor_keys_but_never_the_write_token(tmp_path):
    store = str(tmp_path / "environment")
    envf = str(tmp_path / ".env")
    write(store, "ANTHROPIC_API_KEY=sk-ant-adoptme\nCARTESIA_API_KEY=cart-notknown\n"
                 "DND_WRITE_TOKEN=tok-stays-in-store\n")
    out = run("ensure_env", DNDSIM_KEY_SOURCE=store, DNDSIM_ENV_FILE=envf)
    assert out.returncode == 0, out.stderr
    body = read(envf)
    assert "ANTHROPIC_API_KEY=sk-ant-adoptme" in body
    assert "DND_WRITE_TOKEN" not in body
    assert "CARTESIA_API_KEY" not in body
    assert "DND_WRITE_TOKEN is in the key store" in out.stderr
    assert "NOT adopted" in out.stderr
    # names only, ever: no value reaches stdout/stderr, seeds included.
    for value in ("sk-ant-adoptme", "tok-stays-in-store", "=8071", "=127.0.0.1"):
        assert value not in out.stdout + out.stderr
    assert "seeded PORT" in out.stdout


# ---------------------------------------------------------------------------
# deploy, end to end, against a throwaway clone
# ---------------------------------------------------------------------------

def current_branch() -> str:
    return subprocess.run(["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def clone_or_skip(tmp_path) -> str:
    """A throwaway clone of this checkout, with the working-tree CLI copied in
    so the code under test is what is on disk, not what is committed. Skipped
    where there is nothing to clone: a `git archive` tree (the clean-clone
    check in CLAUDE.md) has no .git, and a detached HEAD has no branch name
    for deploy's `git fetch origin <branch>`."""
    probe = subprocess.run(["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("not a git work tree: nothing to clone (a git-archive tree?)")
    if probe.stdout.strip() == "HEAD":
        pytest.skip("detached HEAD: deploy needs a branch name to fetch")
    clone = str(tmp_path / "clone")
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", ROOT, clone], check=True)
    shutil.copy(CLI, os.path.join(clone, "bin", "dndsim"))
    return clone


def deploy_fixture(tmp_path, curl_code: str = "200", vhost: bool = True, jlist_fail: bool = False):
    """clone_or_skip plus shims for everything deploy shells out to.
    Returns (clone, bindir, env, logs)."""
    clone = clone_or_skip(tmp_path)
    bindir = tmp_path / "bin"; bindir.mkdir()
    logs = dict(pm2=str(tmp_path / "pm2.args"), env=str(tmp_path / "pm2.env"),
                calls=str(tmp_path / "calls.log"))
    started = str(tmp_path / "started")
    jlist = "exit 3" if jlist_fail else (
        'if [ -e "%s" ]; then printf \'[{"name":"dnd-sim","pm2_env":{"status":"online",'
        '"exec_mode":"fork_mode","restart_time":0,"pm_uptime":1}}]\\n\'; else echo "[]"; fi' % started)
    pm2_shim(str(bindir), logs["env"], logs["pm2"], jlist=jlist,
             tail='case "$1" in start|restart) touch "%s";; esac' % started)
    curl_shim(str(bindir), curl_code, '{"ok":true,"mock":false,"games_running":0}')
    recorder(str(bindir), "nginx", logs["calls"])
    recorder(str(bindir), "systemctl", logs["calls"])
    recorder(str(bindir), "provision-site", logs["calls"], "exit 2\n")
    avail = tmp_path / "avail"; avail.mkdir()
    enabled = tmp_path / "enabled"; enabled.mkdir()
    if vhost:
        write(str(avail / "test.example"), vhost_without_block())
    store = str(tmp_path / "environment")
    write(store, "ANTHROPIC_API_KEY=sk-ant-adoptme\nDND_WRITE_TOKEN=tok-in-store\n")
    env = dict(NGINX_AVAIL=str(avail), NGINX_ENABLED=str(enabled), BACKUP_ROOT=str(tmp_path / "backups"),
               DNDSIM_FQDN="test.example", DNDSIM_BRANCH=current_branch(), DNDSIM_KEY_SOURCE=store,
               DNDSIM_PROBE_TRIES="1", DNDSIM_PORT="8099", **LEAKS)
    return clone, bindir, env, logs


@needs_root
def test_deploy_end_to_end_repairs_the_vhost_and_keeps_pm2_clean(tmp_path):
    clone, bindir, env, logs = deploy_fixture(tmp_path)
    out = run("cmd_deploy --no-install", cli=os.path.join(clone, "bin", "dndsim"),
              path=str(bindir), **env)
    assert out.returncode == 0, out.stderr + out.stdout
    assert "deployed:" in out.stdout.splitlines()[-1]
    # first registration: start, then save (the stub reports online after a start)
    assert read(logs["pm2"]).splitlines() == [
        "jlist", "start ecosystem.config.js --only dnd-sim", "jlist", "save"]
    # the vhost was there without the block; deploy put it back and reloaded
    vhost = read(os.path.join(env["NGINX_AVAIL"], "test.example"))
    assert SSE_BEGIN in vhost
    assert read(logs["calls"]).splitlines() == ["-t", "reload nginx"]
    # the keys: adopted into .env, and none of them anywhere near pm2
    dotenv = read(os.path.join(clone, ".env"))
    assert "ANTHROPIC_API_KEY=sk-ant-adoptme" in dotenv
    assert "DND_WRITE_TOKEN" not in dotenv
    seen = read(logs["env"])
    for name in LEAKS:
        assert name + "=" not in seen, name + " reached pm2"
    assert "sk-ant-adoptme" not in out.stdout + out.stderr


@needs_root
def test_deploy_survives_a_failing_setup_and_says_so(tmp_path):
    """No vhost, provision-site on PATH and failing: setup dies, deploy
    reports it and still deploys the app — the branch that used to be
    unreachable because `die` exited the whole deploy."""
    clone, bindir, env, logs = deploy_fixture(tmp_path, vhost=False)
    out = run("cmd_deploy --no-install", cli=os.path.join(clone, "bin", "dndsim"),
              path=str(bindir), **env)
    assert out.returncode == 0, out.stderr + out.stdout
    assert "provision-site failed (exit 2)" in out.stderr
    assert "setup did not complete (exit 1); deploying the app anyway" in out.stderr
    assert "start ecosystem.config.js --only dnd-sim" in read(logs["pm2"])
    assert "deployed:" in out.stdout
    assert read(logs["calls"]).splitlines() == ["dndsim ivjames/dnd-sim --port 8099 --dir " + clone]


@needs_root
def test_deploy_exits_nonzero_when_the_app_does_not_answer(tmp_path):
    clone, bindir, env, logs = deploy_fixture(tmp_path, curl_code="502")
    out = run("cmd_deploy --no-install", cli=os.path.join(clone, "bin", "dndsim"),
              path=str(bindir), **env)
    assert out.returncode != 0
    assert "deployed:" not in out.stdout
    assert "not deployed" in out.stderr
    assert "HTTP 502" in out.stdout and "public:" in out.stdout


@needs_root
def test_deploy_stops_when_pm2_cannot_be_read(tmp_path):
    clone, bindir, env, logs = deploy_fixture(tmp_path, jlist_fail=True)
    out = run("cmd_deploy --no-install", cli=os.path.join(clone, "bin", "dndsim"),
              path=str(bindir), **env)
    assert out.returncode != 0
    assert "cannot tell whether dnd-sim is registered" in out.stderr
    assert "start" not in read(logs["pm2"])


@needs_root
def test_restart_exits_nonzero_when_the_app_does_not_answer(tmp_path):
    clone = clone_or_skip(tmp_path)
    bindir = tmp_path / "bin"; bindir.mkdir()
    pm2_shim(str(bindir), str(tmp_path / "e"), str(tmp_path / "a"))
    curl_shim(str(bindir), "000", "")
    out = run("cmd_restart", cli=os.path.join(clone, "bin", "dndsim"), path=str(bindir),
              DNDSIM_PROBE_TRIES="1", DNDSIM_PORT="8099", DNDSIM_FQDN="test.example")
    assert out.returncode != 0
    assert "did not answer HTTP 200" in out.stderr
    assert read(str(tmp_path / "a")).strip() == "restart ecosystem.config.js --only dnd-sim"


# ---------------------------------------------------------------------------
# the help says what the code does
# ---------------------------------------------------------------------------

def test_the_docs_describe_the_allowlist_not_an_unset_list():
    src = read(CLI)
    assert "env -u" not in src
    assert "pm2_unset_names" not in src
    for doc in ("DEPLOY.md", "ecosystem.config.js", "CLAUDE.md"):
        assert "env -u" not in read(os.path.join(ROOT, doc)), doc
    assert "NOT one to put in /etc/environment" in src
    assert 'NO_ADOPT_KEYS="$WRITE_TOKEN_KEY"' in src
