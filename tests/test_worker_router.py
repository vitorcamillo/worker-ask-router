import stat
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

# Executaveis falsos, um por binario real. O degrau codex-terra roda o fake
# codex-ask (WRAPPER_BINARIES), entao no log ele aparece como "codex-ask".
FAKE_NAMES = ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"]

FAKE_SCRIPT = r'''
import json, os, sys, time, subprocess

name = os.path.basename(sys.argv[0])
record = {"name": name, "argv": sys.argv[1:], "stdin": ""}
try:
    record["stdin"] = sys.stdin.read()
except Exception as e:
    record["stdin"] = "<err %s>" % e

log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(record) + "\n")

mode = os.environ.get("FAKE_MODE_" + name.upper().replace("-", "_"), os.environ.get("FAKE_MODE", "exit"))
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "10")))
elif mode == "grandchild":
    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(100)"])
    with open(os.environ["FAKE_PID_FILE"], "w") as f:
        f.write(str(child.pid))
    time.sleep(100)
elif mode == "deletefile":
    os.unlink(os.path.join(os.environ["FAKE_CWD"], os.environ["FAKE_WRITE_REL"]))
elif mode == "chmodfile":
    os.chmod(os.path.join(os.environ["FAKE_CWD"], os.environ["FAKE_WRITE_REL"]), 0o700)
elif mode == "writefile" or mode == "writesleep":
    rel = os.environ.get("FAKE_WRITE_REL")
    content = os.environ.get("FAKE_WRITE_CONTENT", "x")
    cwd = os.environ.get("FAKE_CWD")
    if rel and cwd:
        full = os.path.join(cwd, rel)
        d = os.path.dirname(full)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    if mode == "writesleep":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "10")))
elif mode == "delegate":
    result = subprocess.run([sys.executable, os.environ["FAKE_ROUTER"], "--cwd", os.environ["FAKE_CWD"], "--orchestrator", "codex", "--mode", "plan", "nested"], capture_output=True)
    sys.stdout.write("NESTED_RC=" + str(result.returncode))
    sys.exit(0)
elif mode == "overflow":
    sys.stdout.write("A" * (2 * 1024 * 1024))
    sys.stdout.flush()

rc_env = "FAKE_RC_" + name.upper().replace("-", "_")
rc = int(os.environ.get(rc_env, os.environ.get("FAKE_RC", "0")))
sys.stdout.write(os.environ.get("FAKE_STDOUT_" + name.upper().replace("-", "_"), "READY:" + name))
sys.stderr.write(os.environ.get("FAKE_STDERR", "private-worker-stderr"))
sys.exit(rc)
'''


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in FAKE_NAMES:
        p = bin_dir / name
        p.write_text("#!" + sys.executable + "\n" + FAKE_SCRIPT + "\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("WORKER_ASK_MIN_TIMEOUT", "0")
    # Start Plan desligado por padrão (como no conftest do adapter): cooldown
    # do zcode vale. Testes do Start religam com delenv.
    monkeypatch.setenv("ZCODE_WORKER_START_PLAN", "0")
    # Testa o roteador cru; a apresentação tem testes próprios.
    monkeypatch.setenv("ASK_PRESENTATION_ACTIVE", "1")
    monkeypatch.delenv("ASK_PRESENTATION_FD", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("FAKE_CWD", str(work))
    for key in ("MUSE_ASK_MODEL", "MUSE_ASK_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    return {"bin": bin_dir, "state": state, "work": work, "tmp": tmp_path}


def run_cli(fake_env, extra_args, env_extra=None, input_text=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    work = fake_env["work"]
    cmd = [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(work), "--orchestrator", "codex"] + extra_args
    # allow override orchestrator via extra? keep simple
    return subprocess.run(
        cmd,
        input=input_text.encode() if input_text is not None else None,
        capture_output=True,
        env=env,
        timeout=60,
    )


def test_basic_import():
    assert hasattr(wr, "main")


def test_status_empty(fake_env):
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--status"],
        capture_output=True,
        env=os.environ,
        timeout=30,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "cache" in data


# GivenWhenThen style cases

def test_given_first_wrapper_succeeds_when_chain_then_stdout_forwarded(fake_env):
    env = dict(os.environ)
    env["FAKE_RC"] = "0"
    env["FAKE_LOG"] = str(fake_env["tmp"] / "log.jsonl")
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "hello"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0
    # zcode-worker first (orchestrator opencode) should succeed
    assert b"zcode-worker: rc=0" in r.stderr


def test_given_75_then_fallback_to_next(fake_env):
    env = dict(os.environ)
    # first wrapper (zcode-worker) returns 75, next 0
    env["FAKE_RC_ZCODE_WORKER"] = "75"
    env["FAKE_RC_MUSE_ASK"] = "0"
    env["FAKE_LOG"] = str(fake_env["tmp"] / "log.jsonl")
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "claude", "--mode", "write", "--allowed-file", "a.txt", "--allow-muse", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0


def test_given_all_75_then_exit_75(fake_env):
    env = dict(os.environ)
    env["FAKE_RC"] = "75"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "codex", "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 75


def test_given_missing_binary_then_skip(fake_env):
    # remove one wrapper
    (fake_env["bin"] / "zcode-worker").unlink()
    env = dict(os.environ)
    env["FAKE_RC"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0
    assert b"wrapper ausente" in r.stderr


@pytest.mark.parametrize("orch", ["zcode", "muse", "opencode", "codex", "claude"])
def test_orchestrator_does_not_exclude_first_worker(fake_env, orch):
    env = dict(os.environ)
    env["FAKE_RC"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", orch, "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0
    assert FAKE_NAMES[0].encode() in r.stdout


def test_given_muse_not_optin_then_not_called(fake_env):
    env = dict(os.environ)
    env["FAKE_RC"] = "0"
    log = fake_env["tmp"] / "log.jsonl"
    env["FAKE_LOG"] = str(log)
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "codex", "--mode", "write", "--allowed-file", "a.txt", "--no-muse", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0
    if log.exists():
        names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
        assert "muse-ask" not in names


def test_given_argv_includes_cwd_and_model(fake_env):
    env = dict(os.environ)
    env["FAKE_RC"] = "0"
    log = fake_env["tmp"] / "log.jsonl"
    env["FAKE_LOG"] = str(log)
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "plan", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0
    rec = json.loads(log.read_text().splitlines()[0])
    argv = rec["argv"]
    assert "--cwd" in argv
    assert str(fake_env["work"]) in argv or str(fake_env["work"].resolve()) in argv
    # opencode effort high
    if rec["name"] == "opencode-ask":
        assert "--effort" in argv and "high" in argv


@pytest.mark.parametrize("rc", [1, 143, 144])
def test_given_rc_no_fallback(fake_env, rc):
    env = dict(os.environ)
    env["FAKE_RC_ZCODE_WORKER"] = str(rc)
    env["FAKE_RC"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == rc


def test_given_75_with_file_change_then_no_fallback(fake_env):
    env = dict(os.environ)
    env["FAKE_RC_ZCODE_WORKER"] = "75"
    env["FAKE_MODE"] = "writefile"
    env["FAKE_WRITE_REL"] = "a.txt"
    env["FAKE_WRITE_CONTENT"] = "changed"
    # second wrapper would succeed
    env["FAKE_RC_MUSE_ASK"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "claude", "--mode", "write", "--allowed-file", "a.txt", "--allow-muse", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    # should stop, not succeed via fallback
    assert r.returncode != 0
    assert b"arquivos autorizados alterados" in r.stderr or b"parando" in r.stderr


def test_given_75_without_change_then_fallback(fake_env):
    env = dict(os.environ)
    env["FAKE_RC_ZCODE_WORKER"] = "75"
    env["FAKE_RC_MUSE_ASK"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "claude", "--mode", "write", "--allowed-file", "a.txt", "--allow-muse", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0


def test_given_cache_ttl_expired_retry_status(fake_env):
    env = dict(os.environ)
    env["FAKE_RC"] = "75"
    # first run registers cooldown
    subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    # status shows cooldown
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--status"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout)
    # Sem data do provedor: cooldown finito (padrao 1 h), reset_known continua False.
    assert 0 < data["cache"]["zcode-worker"]["ttl_remaining"] <= 3600
    assert data["cache"]["zcode-worker"]["reset_known"] is False


def test_given_cache_corrupted_treated_empty(fake_env):
    (fake_env["state"] / "worker-ask").mkdir(parents=True, exist_ok=True)
    (fake_env["state"] / "worker-ask" / "cache.json").write_text("{bad")
    # load_cache via import
    import worker_router as wr2
    # monkeypatch state dir by env already; call
    c = wr2.load_cache(create_dir=False)
    assert c == {}


def test_given_cache_nan_rejected():
    # unit-ish: construct data
    assert wr._valid_until(float("nan"), time.time()) is False


def test_given_symlink_broken_cache_rejected(fake_env):
    d = fake_env["state"] / "worker-ask"
    d.mkdir(parents=True, exist_ok=True)
    link = d / "cache.json"
    target = d / "nope"
    link.symlink_to(target)
    with pytest.raises(RuntimeError):
        wr.load_cache(create_dir=False)


def test_given_symlink_broken_cache_race_nofollow_rejected(fake_env, monkeypatch):
    # Corrida: cache.json vira symlink entre is_symlink() e a abertura.
    # Com O_NOFOLLOW, os.open falha (ELOOP) -> RuntimeError; sem a trava,
    # o symlink seria lido e devolveria o cache (o teste falharia).
    d = fake_env["state"] / "worker-ask"
    d.mkdir(parents=True, exist_ok=True)
    alvo = d / "alvo.json"
    alvo.write_text(json.dumps({"zcode-worker": {"wrapper": "zcode-worker", "until": time.time() + 600}}))
    (d / "cache.json").symlink_to(alvo)
    monkeypatch.setattr(wr.Path, "is_symlink", lambda self: False)
    with pytest.raises(RuntimeError):
        wr.load_cache(create_dir=False)


def test_given_ancestors_symlink_snapshot_rejected(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sub = work / "sub"
    sub.mkdir()
    # symlink ancestor
    (work / "linkdir").symlink_to("/tmp")
    with pytest.raises(RuntimeError):
        wr._snapshot_one(str(work), "linkdir/x.txt")


def test_given_timeout_kills_grandchild(fake_env):
    env = dict(os.environ)
    env["FAKE_MODE"] = "grandchild"
    env["FAKE_RC"] = "0"
    pid_file = fake_env["tmp"] / "child.pid"
    env["FAKE_PID_FILE"] = str(pid_file)
    start = time.time()
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "--timeout", "1", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    elapsed = time.time() - start
    assert elapsed < 10
    # process exits due to timeout
    assert r.returncode == 124
    pid = int(pid_file.read_text())
    try:
        # A killed child may remain a zombie briefly until its adopter reaps it.
        proc_stat = Path(f"/proc/{pid}/stat")
        assert not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z"
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_given_stdin_not_read_timeout(fake_env):
    env = dict(os.environ)
    env["FAKE_MODE"] = "sleep"
    env["FAKE_SLEEP"] = "10"
    env["FAKE_RC"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "--timeout", "1", "hi"],
        capture_output=True, env=env, timeout=30,
        input=b"prompt",
    )
    assert r.returncode == 124


def test_given_rc_124_then_no_fallback(fake_env):
    log = fake_env["tmp"] / "log.jsonl"
    env = dict(os.environ)
    env["FAKE_RC_ZCODE_WORKER"] = "124"
    env["FAKE_RC"] = "0"
    env["FAKE_LOG"] = str(log)
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 124
    assert b"parando sem fallback" in r.stderr
    assert b"execucao interrompida por timeout" in r.stderr
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names == ["zcode-worker"]


def test_given_step_timeout_in_plan_then_fallback(fake_env):
    log = fake_env["tmp"] / "log.jsonl"
    env = dict(os.environ)
    env["FAKE_MODE_ZCODE_WORKER"] = "sleep"
    env["FAKE_SLEEP"] = "10"
    env["FAKE_RC"] = "0"
    env["FAKE_LOG"] = str(log)
    start = time.time()
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "plan", "--step-timeout", "1", "--timeout", "20", "--no-muse", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    elapsed = time.time() - start
    assert r.returncode == 0
    assert elapsed < 10
    assert b"rc=timeout, fallback (plan)" in r.stderr
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names == ["zcode-worker", "opencode-ask"]


def test_given_rc_124_in_plan_then_fallback(fake_env):
    log = fake_env["tmp"] / "log.jsonl"
    env = dict(os.environ)
    env["FAKE_RC_ZCODE_WORKER"] = "124"
    env["FAKE_RC"] = "0"
    env["FAKE_LOG"] = str(log)
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "plan", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0


def test_given_writesleep_timeout_then_no_fallback(fake_env):
    log = fake_env["tmp"] / "log.jsonl"
    env = dict(os.environ)
    env["FAKE_MODE_ZCODE_WORKER"] = "writesleep"
    env["FAKE_WRITE_REL"] = "a.txt"
    env["FAKE_SLEEP"] = "10"
    env["FAKE_LOG"] = str(log)
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "--step-timeout", "1", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 124
    assert b"parando sem fallback" in r.stderr
    assert b"execucao interrompida por timeout" in r.stderr
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names == ["zcode-worker"]


def test_given_worker_timeout_then_exit_124(fake_env):
    log = fake_env["tmp"] / "log.jsonl"
    env = dict(os.environ)
    env["FAKE_MODE"] = "sleep"
    env["FAKE_SLEEP"] = "10"
    env["FAKE_LOG"] = str(log)
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "codex", "--mode", "plan", "--step-timeout", "1", "--timeout", "30", "hi"],
        capture_output=True, env=env, timeout=60,
    )
    assert r.returncode == 124
    assert b"execucao interrompida por timeout" in r.stderr
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert len(names) > 1


def test_timeout_below_floor_is_raised(fake_env):
    r = run_cli(fake_env, ["--mode", "plan", "--timeout", "1", "--step-timeout", "1", "hi"], {
        "WORKER_ASK_MIN_TIMEOUT": "5",
        "FAKE_MODE": "sleep",
        "FAKE_SLEEP": "2",
        "FAKE_RC": "0",
    })
    assert r.returncode == 0
    assert b"abaixo do minimo" in r.stderr


@pytest.mark.parametrize("raw,expected", [
    (None, 300.0),
    ("abc", 300.0),
    ("-1", 300.0),
    ("nan", 300.0),
    ("12", 12.0),
])
def test_min_timeout_parses_env(raw, expected, monkeypatch):
    if raw is None:
        monkeypatch.delenv("WORKER_ASK_MIN_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("WORKER_ASK_MIN_TIMEOUT", raw)
    assert wr._min_timeout() == expected


@pytest.mark.parametrize("value", ["nan", "0", "-1"])
def test_invalid_step_timeout_rejected(fake_env, value):
    result = run_cli(fake_env, ["--mode", "plan", "--step-timeout", value, "task"])
    assert result.returncode == 2


def test_given_overflow_output(fake_env):
    env = dict(os.environ)
    env["FAKE_MODE"] = "overflow"
    env["FAKE_RC"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode != 0


def test_given_invalid_input_utf8(fake_env):
    env = dict(os.environ)
    bad = b"\xff\xfe"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt"],
        input=bad,
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 2


def test_given_oversize_input(fake_env):
    env = dict(os.environ)
    big = b"a" * (70 * 1024)
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt"],
        input=big,
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 2


def test_given_spec_file_bounded(fake_env, tmp_path):
    spec = tmp_path / "spec.txt"
    spec.write_text("hello")
    env = dict(os.environ)
    env["FAKE_RC"] = "0"
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--spec", str(spec), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt"],
        capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0


def test_given_no_leak_75_output(fake_env):
    env = dict(os.environ)
    env["FAKE_RC_ZCODE_WORKER"] = "75"
    env["FAKE_STDOUT"] = "SECRET"
    # fake script doesn't print stdout by default; rc75 path should not forward
    r = subprocess.run(
        [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]), "--orchestrator", "opencode", "--mode", "write", "--allowed-file", "a.txt", "hi"],
        capture_output=True, env=env, timeout=30,
    )
    assert b"SECRET" not in r.stdout


def test_given_build_argv_unit():
    av = wr.build_argv("opencode-ask", "/bin/x", "write", "/tmp/cwd")
    assert "--cwd" in av
    assert "--effort" in av
    assert "high" in av


def test_given_wrapper_chain_unit():
    ch = wr.wrapper_chain("codex", False, "write")
    assert any(n == "codex-ask" for n, _ in ch)
    assert all(n != "muse-ask" for n, _ in ch)


def test_wrapper_chain_is_independent_of_orchestrator():
    assert wr.wrapper_chain("claude", False, "write") == [
        ("zcode-worker", "zcode"),
        ("opencode-ask", "opencode"),
        ("codex-ask", "codex"),
        ("claude-ask", "claude"),
    ]


@pytest.mark.parametrize("allow_muse", [False, True])
def test_codex_orchestrator_allows_luna_worker(allow_muse):
    names = [name for name, _ in wr.wrapper_chain("codex", allow_muse, "plan")]
    assert "codex-terra" not in names
    assert "codex-ask" in names


def test_codex_terra_resolves_the_codex_ask_binary():
    assert wr.WRAPPER_BINARIES == {"codex-terra": "codex-ask"}


@pytest.mark.parametrize("mode,expected", [("write", "write"), ("plan", "plan")])
def test_build_argv_codex_terra_uses_terra_model(mode, expected):
    argv = wr.build_argv("codex-terra", "/bin/codex-ask", mode, ".")
    assert argv[argv.index("--mode") + 1] == expected
    assert argv[argv.index("--model") + 1] == "gpt-6.1-terra"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--cwd") + 1] == os.path.abspath(".")


def test_zcode_quota_uses_next_available_worker(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--orchestrator", "claude", "--mode", "plan", "--no-muse", "task"], {
        "FAKE_RC_ZCODE_WORKER": "75", "FAKE_LOG": str(log)})
    assert result.returncode == 0
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [record["name"] for record in records] == ["zcode-worker", "opencode-ask"]
    argv = records[1]["argv"]
    assert argv[argv.index("--model") + 1] == "auto"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--mode") + 1] == "plan"


def test_codex_terra_cooldown_does_not_block_codex_ask(fake_env):
    wr.save_cache({"codex-terra": {"wrapper": "codex-terra", "until": time.time() + 60}})
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--orchestrator", "claude", "--mode", "plan", "task"], {
        "FAKE_RC": "75", "FAKE_RC_CODEX_ASK": "0", "FAKE_LOG": str(log)})
    assert result.returncode == 0
    assert result.stdout == b"READY:codex-ask"
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names == ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask"]
    cache = json.loads((fake_env["state"] / "worker-ask/cache.json").read_text())
    assert "codex-terra" in cache and "codex-ask" not in cache


def test_given_validate_allowed_control_chars():
    with pytest.raises(ValueError):
        wr.validate_allowed_file("/tmp", "a\nb.txt")


def test_given_validate_allowed_sensitive_component():
    with pytest.raises(ValueError):
        wr.validate_allowed_file("/tmp", ".ssh/key")


def test_given_save_cache_merge_concurrent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    # write one entry
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": time.time() + 10}})
    # merge another
    wr.save_cache({"muse-ask": {"wrapper": "muse-ask", "until": time.time() + 5}})
    c = wr.load_cache(create_dir=False)
    assert "zcode-worker" in c and "muse-ask" in c


def test_given_cache_extra_keys_expunged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    d = tmp_path / "st" / "worker-ask"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cache.json").write_text(json.dumps({"foo": 1, "zcode-worker": {"wrapper": "zcode-worker", "until": time.time() + 5}}))
    c = wr.load_cache(create_dir=False)
    assert "foo" not in c


def test_given_ensure_state_dir_chmod_failure(fake_env, monkeypatch):
    def denied(*args):
        raise PermissionError("private-path")
    monkeypatch.setattr(wr.os, "chmod", denied)
    with pytest.raises(RuntimeError, match="permissoes"):
        wr.ensure_state_dir()


@pytest.mark.parametrize("mode", ["writefile", "deletefile", "chmodfile"])
def test_partial_write_blocks_handoff(fake_env, mode):
    target = fake_env["work"] / "a.txt"
    target.write_text("original")
    target.chmod(0o600)
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--allowed-file", "a.txt", "task"], {
        "FAKE_MODE": mode, "FAKE_WRITE_REL": "a.txt",
        "FAKE_RC_ZCODE_WORKER": "75", "FAKE_LOG": str(log),
    })
    assert result.returncode == 1
    assert [json.loads(line)["name"] for line in log.read_text().splitlines()] == ["zcode-worker"]


EXPECTED_EXHAUSTED_LOGS = {
    name: ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"]
    for name in wr.ORCHESTRATORS
}


@pytest.mark.parametrize("orchestrator", wr.ORCHESTRATORS)
def test_all_orchestrators_use_same_exhausted_chain(fake_env, orchestrator):
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--orchestrator", orchestrator, "--mode", "plan", "--allow-muse", "task"],
                     {"FAKE_RC": "75", "FAKE_LOG": str(log)})
    assert result.returncode == 75
    # codex-terra nao tem fake proprio: roda o fake codex-ask (WRAPPER_BINARIES).
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names == EXPECTED_EXHAUSTED_LOGS[orchestrator]


def test_literal_input_and_failed_output_are_not_reinterpreted(fake_env):
    log = fake_env["tmp"] / "calls"
    prompt = "- literal `touch never` $(never)\nquota exhausted"
    result = run_cli(fake_env, ["--mode", "plan", "--no-muse", "--", prompt], {
        "FAKE_RC_ZCODE_WORKER": "75", "FAKE_LOG": str(log),
        "FAKE_STDOUT_ZCODE_WORKER": "private failed result",
    })
    assert result.returncode == 0
    assert result.stdout == b"READY:opencode-ask"
    assert b"private" not in result.stdout + result.stderr
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [record["stdin"] for record in records] == [prompt, prompt]
    cache = (fake_env["state"] / "worker-ask/cache.json").read_text()
    assert "literal" not in cache and "private" not in cache


@pytest.mark.parametrize("rel", ["../x", "/tmp/x", "a//b", "a/./b", "a\\b", ".ssh/x", ".aws/x", ".env", "auth.json", "a\nx"])
def test_unsafe_allowed_paths_rejected(fake_env, rel):
    result = run_cli(fake_env, ["--allowed-file", rel, "task"])
    assert result.returncode == 2


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1"])
def test_invalid_timeout_rejected(fake_env, timeout):
    result = run_cli(fake_env, ["--mode", "plan", "--timeout", timeout, "task"])
    assert result.returncode == 2


@pytest.mark.parametrize("component", ["cache.json", "lock"])
def test_state_fifo_never_blocks(fake_env, component):
    state = fake_env["state"] / "worker-ask"
    state.mkdir(parents=True)
    os.mkfifo(state / component)
    started = time.monotonic()
    result = run_cli(fake_env, ["--mode", "plan", "task"], {"FAKE_RC": "75"})
    assert result.returncode == 1
    assert time.monotonic() - started < 3


def test_concurrent_cache_updates_are_merged(fake_env):
    processes = []
    for name in FAKE_NAMES:
        script = ("import sys,time;sys.path.insert(0,sys.argv[1]);import worker_router as w;"
                  "name=sys.argv[2];w.save_cache({name:{'wrapper':name,'until':time.time()+60}})")
        processes.append(subprocess.Popen([sys.executable, "-c", script, str(ROOT), name], env=os.environ))
    assert all(process.wait(timeout=10) == 0 for process in processes)
    cache = wr.load_cache()
    assert set(cache) == set(FAKE_NAMES)
    state = fake_env["state"] / "worker-ask"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "cache.json").stat().st_mode) == 0o600


def test_cooldown_skips_until_expiry_and_retry_overrides(fake_env):
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": time.time() + 60}})
    result = run_cli(fake_env, ["--mode", "plan", "--no-muse", "task"])
    assert result.stdout == b"READY:opencode-ask"
    result = run_cli(fake_env, ["--mode", "plan", "--retry-exhausted", "task"])
    assert result.stdout == b"READY:zcode-worker"

    state = fake_env["state"] / "worker-ask/cache.json"
    state.write_text(json.dumps({"zcode-worker": {"wrapper": "zcode-worker", "until": time.time() - 1}}))
    result = run_cli(fake_env, ["--mode", "plan", "task"])
    assert result.stdout == b"READY:zcode-worker"


@pytest.mark.parametrize("args,input_bytes", [
    (["--mode", "plan"], b""),
    (["--mode", "write", "task"], None),
    (["--mode", "plan", "--cwd", "/nonexistent-router-fixture", "task"], None),
    (["--mode", "plan", "--spec", "/missing-router-spec", "task"], None),
    (["--mode", "plan", "--spec", "/missing-router-spec"], None),
    (["--mode", "plan"], b"\xff"),
    (["--mode", "plan"], b"x" * (wr.MAX_INPUT_BYTES + 1)),
    (["--mode", "plan", "x" * (wr.MAX_INPUT_BYTES + 1)], None),
    (["--allowed-file", "x"] * 129 + ["task"], None),
])
def test_invalid_cli_inputs(fake_env, args, input_bytes):
    result = subprocess.run([sys.executable, str(ROOT / "worker_router.py"),
                             "--cwd", str(fake_env["work"]), "--orchestrator", "codex", *args],
                            input=input_bytes, capture_output=True, timeout=5)
    assert result.returncode == 2
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("content", [b"\xff", b"[]", b"{}", b"x" * 70000,
    b'{"zcode-worker":{"wrapper":"zcode-worker","until":NaN}}'])
def test_malformed_cache_is_sanitized(fake_env, content):
    state = wr.ensure_state_dir()
    (state / "cache.json").write_bytes(content)
    assert wr.load_cache() == {}
    wr.save_cache({"opencode-ask": {"wrapper": "opencode-ask", "until": time.time() + 60}})
    assert set(wr.load_cache()) == {"opencode-ask"}


@pytest.mark.parametrize("value", [None, True, "later", float("inf"), float("nan"), -1, 10**20])
def test_cache_timestamp_is_finite_and_bounded(value):
    assert wr._valid_until(value, time.time()) is False


@pytest.mark.parametrize("updates", [{"unknown": {}}, {"zcode-worker": None},
    {"zcode-worker": {"wrapper": "wrong", "until": 1}},
    {"zcode-worker": {"wrapper": "zcode-worker", "until": float("inf")}}])
def test_invalid_cache_updates_not_persisted(fake_env, updates):
    wr.save_cache(updates)
    assert wr.load_cache() == {}


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "large"])
def test_snapshot_rejects_unsafe_file_types(fake_env, kind, monkeypatch):
    target = fake_env["work"] / "target"
    if kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(fake_env["tmp"] / "absent")
    else:
        target.write_bytes(b"xx")
        monkeypatch.setattr(wr, "MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(RuntimeError):
        wr._snapshot_one(str(fake_env["work"]), "target")


@pytest.mark.parametrize("mode", ["write", "plan"])
def test_wrapper_argv_modes_are_explicit(mode):
    for name in FAKE_NAMES + ["codex-terra"]:
        argv = wr.build_argv(name, "/bin/wrapper", mode, ".")
        assert argv[argv.index("--cwd") + 1] == os.path.abspath(".")
        expected = {"zcode-worker": "edit" if mode == "write" else "plan",
                    "codex-terra": "write" if mode == "write" else "plan",
                    "codex-ask": "write" if mode == "write" else "plan"}.get(name, mode)
        assert argv[argv.index("--mode") + 1] == expected
        if name in ["zcode-worker", "muse-ask", "opencode-ask"]:
            assert argv[argv.index("--effort") + 1] == "high"
        if name == "muse-ask":
            assert argv[argv.index("--model") + 1] == "muse-spark-1.3-contributor"


def test_optout_muse_remains_excluded_after_all_failures(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "--no-muse", "task"], {"FAKE_RC": "75", "FAKE_LOG": str(log)})
    assert result.returncode == 75
    assert [json.loads(line)["name"] for line in log.read_text().splitlines()] == ["zcode-worker", "opencode-ask", "codex-ask", "claude-ask"]


def test_lock_contention_is_bounded(fake_env):
    import fcntl
    state = wr.ensure_state_dir()
    with open(state / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="timeout"):
            wr.save_cache({})
        assert time.monotonic() - started < 2


@pytest.mark.parametrize("name", [".env", "auth.json", "secret.key", ".ssh/spec.md"])
def test_sensitive_spec_never_read_or_sent(fake_env, name):
    target = fake_env["tmp"] / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("fixture-sensitive-content")
    result = run_cli(fake_env, ["--mode", "plan", "--spec", str(target)])
    assert result.returncode == 2
    assert b"fixture-sensitive-content" not in result.stdout + result.stderr


def test_symlink_spec_rejected(fake_env):
    target = fake_env["tmp"] / "spec.md"
    target.symlink_to(fake_env["tmp"] / "missing")
    result = run_cli(fake_env, ["--mode", "plan", "--spec", str(target)])
    assert result.returncode == 2


def test_cache_directory_types_and_default_location(fake_env, monkeypatch):
    state = fake_env["state"] / "worker-ask"
    state.write_text("wrong type") if state.parent.exists() else state.parent.mkdir()
    if not state.exists():
        state.write_text("wrong type")
    with pytest.raises(RuntimeError, match="diretorio"):
        wr.ensure_state_dir()
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(wr.Path, "home", lambda: fake_env["tmp"])
    assert wr._state_dir() == fake_env["tmp"] / ".local/state/worker-ask"


def test_snapshot_read_failures_and_size_growth(fake_env, monkeypatch):
    target = fake_env["work"] / "x"
    target.write_text("data")
    original_read = wr.os.read
    monkeypatch.setattr(wr.os, "read", lambda *args: b"xxx")
    monkeypatch.setattr(wr, "MAX_SNAPSHOT_BYTES", 5)
    with pytest.raises(RuntimeError, match="grande"):
        wr._snapshot_one(str(fake_env["work"]), "x")
    monkeypatch.setattr(wr.os, "read", original_read)
    def denied(*args):
        raise PermissionError("private")
    monkeypatch.setattr(wr.os, "open", denied)
    with pytest.raises(RuntimeError, match="I/O"):
        wr._snapshot_one(str(fake_env["work"]), "x")


def test_cache_write_permission_failure(fake_env, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("private")
    monkeypatch.setattr(wr.tempfile, "mkstemp", denied)
    with pytest.raises(PermissionError):
        wr.save_cache({})


def test_existing_later_cooldown_is_preserved(fake_env):
    until = time.time() + 60
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": until}})
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": until - 10}})
    assert wr.load_cache()["zcode-worker"]["until"] == until


def test_positive_spec_uses_literal_contents(fake_env):
    spec = fake_env["tmp"] / "spec.md"
    spec.write_text("- quota\n$(never)")
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "--spec", str(spec)], {"FAKE_LOG": str(log)})
    assert result.returncode == 0
    assert json.loads(log.read_text().splitlines()[0])["stdin"] == spec.read_text()


@pytest.mark.parametrize("args", [["--orchestrator", "codex"], ["--cwd", "."]])
def test_required_execution_context(fake_env, args):
    result = subprocess.run([sys.executable, str(ROOT / "worker_router.py"), "--mode", "plan", *args, "task"],
                            capture_output=True, timeout=5)
    assert result.returncode == 2


def test_missing_state_not_created_for_readonly_status(fake_env):
    state = wr.ensure_state_dir(create=False)
    assert not state.exists()


def test_lock_symlink_and_state_symlink_refused(fake_env):
    state = wr.ensure_state_dir()
    (state / "lock").symlink_to(fake_env["tmp"] / "missing")
    with pytest.raises(RuntimeError, match="symlink"):
        wr.save_cache({})
    (state / "lock").unlink()
    state.rmdir()
    state.symlink_to(fake_env["tmp"] / "missing")
    with pytest.raises(RuntimeError, match="symlink"):
        wr.ensure_state_dir()


def test_binary_disappears_before_spawn(fake_env, monkeypatch):
    monkeypatch.setattr(wr.shutil, "which", lambda _: str(fake_env["tmp"] / "disappeared"))
    with pytest.raises(SystemExit) as stopped:
        wr._main_impl(["--cwd", str(fake_env["work"]), "--orchestrator", "codex", "--mode", "plan", "task"])
    assert stopped.value.code == 75


def test_snapshot_failure_stops_before_execution(fake_env, monkeypatch):
    def failure(*args):
        raise RuntimeError("fixture failure")
    monkeypatch.setattr(wr, "snapshot_allowed", failure)
    with pytest.raises(SystemExit) as stopped:
        wr._main_impl(["--cwd", str(fake_env["work"]), "--orchestrator", "codex", "--allowed-file", "file", "task"])
    assert stopped.value.code == 1


LAUNCHER_MESSAGE = "ERRO: launcher de autenticacao incompativel com o bundle do ZCode"
EFFECTS_WARNING = " Arquivos podem ter sido alterados; revise o diff antes de tentar novamente."


def test_launcher_diagnostic_with_effects_stops_without_fallback(fake_env):
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"], {
        "FAKE_RC_ZCODE_WORKER": "1", "FAKE_STDERR": LAUNCHER_MESSAGE + EFFECTS_WARNING + "\nprivate-secret",
        "FAKE_LOG": str(log),
    })
    assert r.returncode == 1
    assert b"launcher de autenticacao incompativel com o bundle; fallback bloqueado (rc=1)" in r.stderr
    assert b"private-secret" not in r.stdout + r.stderr
    assert len(log.read_text().splitlines()) == 1


def test_launcher_diagnostic_without_effects_falls_back(fake_env):
    # Launcher quebrado por update do ZCode não pode derrubar a cadeia inteira.
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"], {
        "FAKE_RC_ZCODE_WORKER": "1", "FAKE_STDERR": LAUNCHER_MESSAGE + "\nprivate-secret",
        "FAKE_LOG": str(log),
    })
    assert r.returncode == 0
    assert b"Seguindo para o proximo worker (rc=1)" in r.stderr
    assert b"fallback bloqueado" not in r.stderr
    assert b"private-secret" not in r.stdout + r.stderr
    assert len(log.read_text().splitlines()) == 2


def test_launcher_without_effects_but_snapshot_changed_blocks_fallback(fake_env):
    (fake_env["work"] / "a.txt").write_text("original\n")
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"], {
        "FAKE_RC_ZCODE_WORKER": "1", "FAKE_STDERR": LAUNCHER_MESSAGE + "\nprivate-secret",
        "FAKE_LOG": str(log), "FAKE_MODE_ZCODE_WORKER": "writefile",
        "FAKE_WRITE_REL": "a.txt", "FAKE_WRITE_CONTENT": "changed\n",
    })
    assert r.returncode == 1
    assert b"launcher de autenticacao incompativel com o bundle; fallback bloqueado (rc=1)" in r.stderr
    assert b"private-secret" not in r.stdout + r.stderr
    assert (fake_env["work"] / "a.txt").read_text() == "changed\n"
    assert len(log.read_text().splitlines()) == 1


@pytest.mark.parametrize("raw", ["private-secret", LAUNCHER_MESSAGE + " token=private-secret", "prefix " + LAUNCHER_MESSAGE, "", LAUNCHER_MESSAGE.replace("ZCode", "other")])
def test_launcher_unknown_stderr_stays_private(fake_env, raw):
    r = run_cli(fake_env, ["--mode", "plan", "hi"], {"FAKE_RC_ZCODE_WORKER": "1", "FAKE_STDERR": raw})
    assert r.returncode == 1
    assert b"launcher de autenticacao" not in r.stderr
    assert b"private-secret" not in r.stdout + r.stderr


@pytest.mark.parametrize("rc", [0, 75])
def test_launcher_diagnostic_does_not_change_other_exit_codes(fake_env, rc):
    r = run_cli(fake_env, ["--mode", "plan", "hi"], {"FAKE_RC_ZCODE_WORKER": str(rc), "FAKE_STDERR": LAUNCHER_MESSAGE})
    assert r.returncode == (0 if rc == 75 else rc)
    assert b"launcher de autenticacao" not in r.stderr


def test_launcher_diagnostic_not_attributed_to_other_provider(fake_env):
    (fake_env["bin"] / "zcode-worker").unlink()
    r = run_cli(fake_env, ["--mode", "plan", "hi"], {"FAKE_RC_OPENCODE_ASK": "1", "FAKE_STDERR": LAUNCHER_MESSAGE, "WORKER_ASK_NO_MUSE": "1"})
    assert r.returncode == 1
    assert b"launcher de autenticacao" not in r.stderr


def test_launcher_run_wrapper_only_returns_static_marker(fake_env, monkeypatch):
    monkeypatch.setenv("FAKE_STDERR", LAUNCHER_MESSAGE + "\nprivate-secret")
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "1")
    rc, _stdout, marker, timeout = wr.run_wrapper([str(fake_env["bin"] / "zcode-worker")], str(fake_env["work"]), b"hi", 10)
    assert (rc, marker, timeout) == (1, b"launcher_incompatible", False)
    assert wr._safe_failure_marker(b"private-secret") == b""


def test_codex_uses_luna_when_other_workers_are_blocked(fake_env):
    until = time.time() + 86400
    wr.save_cache({name: {"wrapper": name, "until": until, "reset_known": True}
                   for name in ("zcode-worker", "muse-ask", "opencode-ask", "claude-ask")})
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "task"], {"FAKE_LOG": str(log)})
    assert result.returncode == 0
    assert result.stdout == b"READY:codex-ask"
    assert [json.loads(line)["name"] for line in log.read_text().splitlines()] == ["codex-ask"]


def test_worker_cannot_delegate_recursively(fake_env):
    result = run_cli(fake_env, ["--mode", "plan", "task"], {
        "FAKE_MODE_ZCODE_WORKER": "delegate", "FAKE_ROUTER": str(ROOT / "worker_router.py")})
    assert result.returncode == 0
    assert result.stdout == b"NESTED_RC=2"


def test_muse_in_chain_by_default(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--orchestrator", "codex", "--mode", "plan", "task"],
                     {"FAKE_RC": "75", "FAKE_LOG": str(log)})
    assert result.returncode == 75
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names == ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"]
    assert names[1] == "muse-ask"


def test_no_muse_flag_removes_muse(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "--no-muse", "task"],
                     {"FAKE_RC": "75", "FAKE_LOG": str(log)})
    assert result.returncode == 75
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert "muse-ask" not in names
    assert names == ["zcode-worker", "opencode-ask", "codex-ask", "claude-ask"]


def test_no_muse_marker_removes_muse(fake_env):
    (fake_env["work"] / ".worker-ask-no-muse").write_text("")
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "task"],
                     {"FAKE_RC": "75", "FAKE_LOG": str(log)})
    assert result.returncode == 75
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert "muse-ask" not in names


def test_no_muse_env_removes_muse(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "task"],
                     {"FAKE_RC": "75", "FAKE_LOG": str(log), "WORKER_ASK_NO_MUSE": "1"})
    assert result.returncode == 75
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert "muse-ask" not in names


def test_muse_opted_out_marker_in_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ASK_NO_MUSE", raising=False)
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    assert wr.muse_opted_out(str(sub)) is False
    (repo / ".worker-ask-no-muse").write_text("")
    assert wr.muse_opted_out(str(sub)) is True


def test_muse_opted_out_stops_at_git(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ASK_NO_MUSE", raising=False)
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".worker-ask-no-muse").write_text("")
    assert wr.muse_opted_out(str(sub)) is True
    (repo / ".worker-ask-no-muse").unlink()
    (tmp_path / ".worker-ask-no-muse").write_text("")
    assert wr.muse_opted_out(str(sub)) is False


def test_muse_opted_out_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_ASK_NO_MUSE", "1")
    assert wr.muse_opted_out(str(tmp_path)) is True
    monkeypatch.setenv("WORKER_ASK_NO_MUSE", "0")
    assert wr.muse_opted_out(str(tmp_path)) is False


def test_nested_call_rejected_before_start(fake_env):
    log = fake_env['tmp'] / 'calls'
    result = run_cli(fake_env, ['--mode', 'plan', 'task'], {
        'WORKER_ASK_ACTIVE': '1', 'FAKE_LOG': str(log)})
    assert result.returncode == 2
    assert b'delegacao recursiva' in result.stderr
    assert not log.exists()


def test_wrapper_chain_segue_ordem_do_painel(tmp_path, monkeypatch):
    arq = tmp_path / "ordem.json"
    monkeypatch.setenv("WORKER_ASK_ORDEM", str(arq))
    arq.write_text('{"provedores": ["opencode", "zcode", "claude", "muse", "codex"]}')
    assert [n for _, n in wr.wrapper_chain("claude", True, "write")] == ["opencode", "zcode", "claude", "muse", "codex"]
    assert [n for _, n in wr.wrapper_chain("claude", False, "write")] == ["opencode", "zcode", "claude", "codex"]
    # Lista que não é permutação exata (falta, sobra ou repete) cai na ordem padrão.
    for ruim in ('{"provedores": ["zcode", "zcode", "muse", "opencode", "codex"]}',
                 '{"provedores": ["zcode", "muse", "opencode", "codex", "claude", "x"]}', "{", "[]",
                 '{"provedores": [1, "zcode", "muse", "opencode", "codex"]}',
                 '{"provedores": [["zcode"], "muse", "opencode", "codex", "claude"]}'):
        arq.write_text(ruim)
        assert [n for _, n in wr.wrapper_chain("claude", True, "write")] == list(wr._ORDEM_PADRAO)
