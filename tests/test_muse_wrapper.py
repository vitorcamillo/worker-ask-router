"""Exercita o wrapper Muse real com CLI e catálogo inteiramente sintéticos."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def muse(tmp_path):
    home = tmp_path / "home"
    binary = home / ".local/bin/muse"
    binary.parent.mkdir(parents=True)
    log = tmp_path / "argv.json"
    binary.write_text("#!" + sys.executable + '''
import json,os,sys
from pathlib import Path
if '--version' in sys.argv: print('fixture-muse'); raise SystemExit(0)
args=sys.argv[1:]
prompt=Path(args[args.index('--prompt-file')+1]).read_text()
Path(os.environ['FAKE_LOG']).write_text(json.dumps({'argv':args,'prompt':prompt}))
mode=os.environ.get('FAKE_MODE','ok')
if mode=='quota': print('weekly usage limit reached',file=sys.stderr);raise SystemExit(1)
if mode=='error': print('synthetic failure',file=sys.stderr);raise SystemExit(1)
if mode!='empty': print(json.dumps({'payload_type':'run.terminal.completed','payload':{'text':prompt}}))
''')
    binary.chmod(0o755)
    env = dict(os.environ, HOME=str(home), ASK_PRESENTATION_ACTIVE="1", FAKE_LOG=str(log))
    for key in list(env):
        if key.startswith("MUSE_ASK_") or key == "META_API_KEY":
            env.pop(key)

    def run(*args, mode="ok"):
        return subprocess.run(["bash", str(ROOT / "bin/muse-ask"), "--cwd", str(tmp_path), *args], env=dict(env, FAKE_MODE=mode), capture_output=True, text=True, timeout=15)
    return run, log, home


@pytest.mark.parametrize("mode,readonly", [("plan", True), ("write", False)])
def test_muse_modes_deny_shell_and_preserve_prompt(muse, mode, readonly, tmp_path):
    run, log, _home = muse
    sentinel = tmp_path / "must-not-run"
    task = f"literal $(touch {sentinel}) `echo fixture`"
    result = run("--mode", mode, task)
    assert result.returncode == 0
    data = json.loads(log.read_text())
    assert "--disable-shell" in data["argv"]
    assert ("--disable-write" in data["argv"]) is readonly
    assert data["argv"][data["argv"].index("--approval-mode") + 1] == "never"
    assert task in data["prompt"]
    assert not sentinel.exists()


@pytest.mark.parametrize("mode,code", [("quota", 75), ("error", 1), ("empty", 1)])
def test_muse_errors_do_not_succeed(muse, mode, code):
    run, _log, _home = muse
    assert run("task", mode=mode).returncode == code


def test_muse_spec_and_invalid_options(muse, tmp_path):
    run, log, _home = muse
    spec = tmp_path / "spec.txt"
    spec.write_text("literal specification")
    assert run("--spec", str(spec)).returncode == 0
    assert "literal specification" in json.loads(log.read_text())["prompt"]
    log.unlink()
    assert run("--unknown", "task").returncode == 1
    assert run("--mode", "invalid", "task").returncode == 1
    assert run("--effort", "invalid", "task").returncode == 1
    assert not log.exists()


def test_muse_catalog_refuses_unknown_model(muse):
    run, log, home = muse
    catalog = home / ".local/share/muse/model-catalog/fixture.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"rows": [{"model_id": "muse-spark-1.3-contributor", "reasoning_effort_variants": [{"tier": "high"}]}]}))
    assert run("--model", "missing-model", "task").returncode == 1
    assert not log.exists()
