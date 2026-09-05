"""`dndsim token` — the write token is set in `.env` and nowhere else.

The script is written to be sourced (`if [ "${BASH_SOURCE[0]}" != "$0" ]` stops
it before the dispatch), so its shell functions can be exercised directly
against temp files. What is checked here is the part that has to be right every
time: the token replaces its own line rather than stacking a new one, the rest
of `.env` survives, and nothing is left world-readable.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(ROOT, "bin", "dndsim")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


SHIM_PATH = ""


@pytest.fixture(autouse=True, scope="module")
def root_shim(tmp_path_factory):
    """`cmd_token` asks `need_root` before it does anything, and `need_root`
    asks `id -u`. An `id` on PATH that answers 0 lets the whole thing run for
    any user; everything it touches is a temp file the test hands it."""
    global SHIM_PATH
    d = tmp_path_factory.mktemp("id-shim")
    p = d / "id"
    p.write_text('#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; else PATH=/usr/bin:/bin exec id "$@"; fi\n')
    p.chmod(0o755)
    SHIM_PATH = str(d)


def run(body: str, **env: str) -> subprocess.CompletedProcess:
    """Source the CLI (so its functions are defined) and run `body`."""
    script = 'source "%s" >/dev/null 2>&1\n%s' % (CLI, body)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": SHIM_PATH + os.pathsep + os.environ.get("PATH", ""), **env},
    )


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_script_still_sources_without_running_anything():
    """Everything below depends on this, and on the dispatch staying guarded."""
    out = run('echo "$WRITE_TOKEN_KEY"')
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "DND_WRITE_TOKEN"


def test_setting_a_token_writes_it_where_run_sh_reads_it(tmp_path):
    envf = str(tmp_path / ".env")
    out = run(
        'env_file_set "%s" DND_WRITE_TOKEN abc123; env_file_get "%s" DND_WRITE_TOKEN' % (envf, envf)
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "abc123"
    assert "DND_WRITE_TOKEN=abc123" in read(envf)


def test_rotating_replaces_the_line_rather_than_stacking_one(tmp_path):
    """A file that grows a line per rotation is a file with every old secret
    still in it."""
    envf = str(tmp_path / ".env")
    out = run(
        'env_file_set "%s" DND_WRITE_TOKEN first;'
        ' env_file_set "%s" DND_WRITE_TOKEN second;'
        ' env_file_set "%s" DND_WRITE_TOKEN third;'
        ' env_file_get "%s" DND_WRITE_TOKEN' % (envf, envf, envf, envf)
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "third"
    body = read(envf)
    assert body.count("DND_WRITE_TOKEN=") == 1
    assert "first" not in body and "second" not in body


def test_the_other_keys_survive_a_rotation(tmp_path):
    envf = str(tmp_path / ".env")
    with open(envf, "w", encoding="utf-8") as fh:
        fh.write(
            "PORT=8071\n"
            "ANTHROPIC_API_KEY=sk-ant-keepme\n"
            "DND_WRITE_TOKEN=old\n"
            "AWS_REGION=us-east-1\n"
        )
    out = run('env_file_set "%s" DND_WRITE_TOKEN new' % envf)
    assert out.returncode == 0, out.stderr
    body = read(envf)
    assert "ANTHROPIC_API_KEY=sk-ant-keepme" in body
    assert "AWS_REGION=us-east-1" in body
    assert "PORT=8071" in body
    assert "old" not in body


def test_an_exported_line_is_replaced_too(tmp_path):
    """`run.sh` sources the file, so `export KEY=` is a legal way to have
    written it by hand; leaving one behind would shadow the new value."""
    envf = str(tmp_path / ".env")
    with open(envf, "w", encoding="utf-8") as fh:
        fh.write("export DND_WRITE_TOKEN=old\n")
    out = run('env_file_set "%s" DND_WRITE_TOKEN new; env_file_get "%s" DND_WRITE_TOKEN'
              % (envf, envf))
    assert out.returncode == 0, out.stderr
    assert out.stdout == "new"
    assert "old" not in read(envf)


def test_a_missing_env_file_is_created_private(tmp_path):
    envf = str(tmp_path / "fresh.env")
    out = run('env_file_set "%s" DND_WRITE_TOKEN abc' % envf)
    assert out.returncode == 0, out.stderr
    assert os.path.exists(envf)
    assert oct(os.stat(envf).st_mode & 0o777) == "0o600"


def test_a_rotated_env_file_stays_private(tmp_path):
    envf = str(tmp_path / ".env")
    with open(envf, "w", encoding="utf-8") as fh:
        fh.write("DND_WRITE_TOKEN=old\n")
    os.chmod(envf, 0o644)
    out = run('env_file_set "%s" DND_WRITE_TOKEN new' % envf)
    assert out.returncode == 0, out.stderr
    assert oct(os.stat(envf).st_mode & 0o777) == "0o600"


def test_show_reads_the_token_back(tmp_path):
    envf = str(tmp_path / ".env")
    with open(envf, "w", encoding="utf-8") as fh:
        fh.write("DND_WRITE_TOKEN=shown\n")
    out = run('cmd_token --show', DNDSIM_ENV_FILE=envf)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "shown"


def test_show_fails_loudly_when_there_is_no_token(tmp_path):
    envf = str(tmp_path / ".env")
    open(envf, "w").close()
    out = run('cmd_token --show', DNDSIM_ENV_FILE=envf)
    assert out.returncode != 0
    assert "dndsim token" in out.stderr


def test_the_token_command_is_dispatched_and_documented():
    with open(CLI, encoding="utf-8") as fh:
        src = fh.read()
    assert "  token)   shift; cmd_token" in src
    assert "dndsim token [--show|--stdin]" in src
    # ...and the help says not to put this one in the key store.
    assert "NOT one to put in /etc/environment" in src


def test_the_token_never_reaches_a_command_line():
    """/proc/<pid>/cmdline is world-readable; the rest of this script is careful
    about that and the check has to be too, so the header goes to curl through
    a config on stdin rather than as `-H`."""
    with open(CLI, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("token_check() {")
    body = src[start:src.index("\n}\n", start)]
    assert "-K -" in body, "curl should read the header from a config on stdin"
    assert "-H " not in body, "a -H argument would put the token in argv"
