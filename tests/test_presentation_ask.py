"""Given um worker, When supervisionado, Then protocolo e estado são preservados."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ASK_PATH = Path(__file__).resolve().parents[1] / "bin/ask_presentation.py"
spec = importlib.util.spec_from_file_location("ask_presentation", ASK_PATH)
ask = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ask)


@pytest.fixture
def wrapper(tmp_path):
    p = tmp_path / "worker"
    p.write_text(
        "#!/usr/bin/python3\nimport sys,os,json,time\n"
        'if os.environ.get("SLEEP"): time.sleep(30)\n'
        "data=sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(data)\n"
        'if os.environ.get("ARGS"): print(json.dumps(sys.argv[1:]))\n'
        'sys.exit(int(os.environ.get("STUB_EXIT","0")))\n'
    )
    p.chmod(0o755)
    return p


def run(wrapper, *args, env=None, data=b"\x00\xffJSON\n", cwd=None, provider="codex"):
    environ = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ASK_PRESENTATION_ACTIVE", "ASK_TITLE", "ASK_PRESENTATION")
    }
    environ.update(env or {})
    return subprocess.run(
        [
            sys.executable,
            str(ASK_PATH),
            "--provider",
            provider,
            "--",
            str(wrapper),
            *args,
        ],
        input=data,
        capture_output=True,
        env=environ,
        timeout=8,
        cwd=cwd,
    )


@pytest.mark.parametrize("rc", [0, 1, 75, 124])
def test_exit_and_bytes(wrapper, rc):
    result = run(wrapper, env={"STUB_EXIT": str(rc)})
    assert result.returncode == rc
    assert result.stdout == b"\x00\xffJSON\n"
    assert "Codex" in result.stderr.decode()
    assert ("validação pendente" in result.stderr.decode()) == (rc == 0)


@pytest.mark.parametrize("value", ["off", "plain", "json", "auto", "invalid"])
def test_display_styles(wrapper, value):
    result = run(wrapper, env={"ASK_PRESENTATION": value}, data=b"")
    assert result.returncode == 0
    assert result.stdout == b""
    if value == "off":
        assert result.stderr == b""
    elif value == "json":
        events = [json.loads(line) for line in result.stderr.splitlines()]
        assert [e["event"] for e in events] == ["start", "end"]
        assert events[-1]["exit"] == 0
    else:
        assert "◆ Codex" in result.stderr.decode()
        assert "✓ Codex" in result.stderr.decode()
        assert b"\x1b" not in result.stderr


def test_title_and_prompt_privacy(wrapper):
    result = run(
        wrapper,
        "--title",
        "\x1b]0;hidden\x07\x1b[31mConferir\u202e\n",
        "PROMPT_PRIVATE",
        env={"ARGS": "1"},
        data=b"",
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["PROMPT_PRIVATE"]
    assert b"PROMPT_PRIVATE" not in result.stderr
    assert b"hidden" not in result.stderr
    assert "Conferir" in result.stderr.decode()
    assert b"\x1b" not in result.stderr


@pytest.mark.parametrize(
    "flag", ["--help", "-h", "--list-models", "--_background-child"]
)
def test_flags_pass_through_without_recursion(wrapper, flag):
    result = run(wrapper, flag, env={"ARGS": "1"}, data=b"")
    assert result.returncode == 0
    assert json.loads(result.stdout) == [flag]
    assert result.stderr == b""


def test_bootstrap_off_does_not_recurse(tmp_path):
    boot = tmp_path / "boot"
    boot.write_text(
        '#!/bin/sh\nif [ "${ASK_PRESENTATION_ACTIVE:-}" != 1 ]; then exec /usr/bin/python3 '
        + str(ASK_PATH)
        + ' --provider codex -- "$0" "$@"; fi\nprintf original\n'
    )
    boot.chmod(0o755)
    result = run(boot, env={"ASK_PRESENTATION": "off"})
    assert (
        result.returncode == 0 and result.stdout == b"original" and result.stderr == b""
    )


@pytest.mark.parametrize("flag", ["--saida", "--saida=result"])
def test_background_is_submission(wrapper, flag):
    result = run(wrapper, flag, data=b"")
    assert result.returncode == 0
    assert "Enviado para segundo plano" in result.stderr.decode()
    assert "validação pendente" not in result.stderr.decode()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "abc", "0", "3601"])
def test_invalid_interval_falls_back(monkeypatch, value):
    monkeypatch.setenv("ASK_PRESENTATION_INTERVAL", value)
    assert ask._parse_interval() == 60


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("codex-ask", "review"),
        ("zcode-worker", "edit"),
        ("bonsai-ask", "consulta"),
        ("muse-ask", "plan"),
    ],
)
def test_default_mode(provider, expected):
    assert ask._mode_default(provider) == expected


def test_sanitize():
    assert ask.sanitize("\x1b]0;secret\x1b\\\x1b[31mA\u202e\t\nB\x00", 2) == "AB"
    assert ask.sanitize(None) == ""


def test_signal_and_heartbeat(wrapper, tmp_path):
    env = dict(
        os.environ, SLEEP="1", ASK_PRESENTATION_INTERVAL="1", ASK_PRESENTATION="json"
    )
    env.pop("ASK_PRESENTATION_ACTIVE", None)
    proc = subprocess.Popen(
        [sys.executable, str(ASK_PATH), "--provider", "codex", "--", str(wrapper)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        first = json.loads(proc.stderr.readline())
        heartbeat = json.loads(proc.stderr.readline())
        assert first["event"] == "start" and heartbeat["event"] == "heartbeat"
        proc.send_signal(signal.SIGTERM)
        _, remaining = proc.communicate(timeout=5)
        assert proc.returncode == 143
        assert json.loads(remaining.splitlines()[-1])["exit"] == 143
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_missing_worker():
    result = run(Path("/nonexistent/presentation-worker"), data=b"")
    assert result.returncode == 1
    assert b"Traceback" not in result.stderr


def test_slow_stdout_reader_receives_everything_with_log(tmp_path):
    stub = tmp_path / "big"
    stub.write_text(
        "#!/usr/bin/python3\nimport sys\n"
        "sys.stdout.buffer.write(b'x' * 131072)\nsys.stdout.buffer.flush()\n"
    )
    stub.chmod(0o755)
    log = tmp_path / "big.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ASK_PRESENTATION_ACTIVE", "ASK_TITLE", "ASK_PRESENTATION")
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ASK_PATH),
            "--provider",
            "worker-ask",
            "--",
            str(stub),
            "--log",
            str(log),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    chunks = []
    try:
        while True:
            data = os.read(proc.stdout.fileno(), 4096)
            if not data:
                break
            chunks.append(data)
            time.sleep(0.1)
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert rc == 0
    assert b"".join(chunks) == b"x" * 131072
    logged = log.read_bytes()
    assert b"x" * 131072 in logged
    assert logged.endswith(b"exit=0\n")


def test_blocked_stdout_reader_beyond_drain_timeout_still_delivers(tmp_path):
    payload = b"x" * 60000
    stub = tmp_path / "blocked"
    stub.write_text(
        "#!/usr/bin/python3\nimport sys\n"
        "sys.stdout.buffer.write(b'x' * 60000)\nsys.stdout.buffer.flush()\n"
    )
    stub.chmod(0o755)
    log = tmp_path / "blocked.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ASK_PRESENTATION_ACTIVE", "ASK_TITLE", "ASK_PRESENTATION")
    }
    read_fd, write_fd = os.pipe()
    fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ASK_PATH),
            "--provider",
            "worker-ask",
            "--",
            str(stub),
            "--log",
            str(log),
        ],
        stdin=subprocess.DEVNULL,
        stdout=write_fd,
        stderr=subprocess.PIPE,
        env=env,
    )
    os.close(write_fd)
    chunks = []
    try:
        # Pipe de saída cheio e ninguém lendo além de DRAIN_TIMEOUT + grace:
        # o repasse fica bloqueado na escrita e a apresentação não pode
        # finalizar o interpretador nem fechar o log nesse estado.
        time.sleep(ask.DRAIN_TIMEOUT + 3.0)
        assert proc.poll() is None
        while True:
            data = os.read(read_fd, 65536)
            if not data:
                break
            chunks.append(data)
        rc = proc.wait(timeout=15)
        stderr = proc.stderr.read()
    finally:
        os.close(read_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert rc == 0
    assert b"".join(chunks) == payload
    assert "validação pendente" in stderr.decode()
    logged = log.read_bytes()
    assert payload in logged
    assert logged.endswith(b"exit=0\n")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_cancels_blocked_stdout_with_log(tmp_path, signum):
    donefile = tmp_path / "producer.done"
    stub = tmp_path / "blocked-on-stdout"
    stub.write_text(
        "#!/usr/bin/python3\nimport pathlib, sys\n"
        "sys.stdout.buffer.write(b'x' * 6000)\nsys.stdout.buffer.flush()\n"
        f"pathlib.Path({str(donefile)!r}).write_text('done')\n"
    )
    stub.chmod(0o755)
    log = tmp_path / "cancelled.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ASK_PRESENTATION_ACTIVE", "ASK_TITLE", "ASK_PRESENTATION")
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ASK_PATH),
            "--provider",
            "worker-ask",
            "--",
            str(stub),
            "--log",
            str(log),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        try:
            fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        except (AttributeError, OSError):
            pytest.skip("não foi possível reduzir o pipe de stdout neste ambiente")

        deadline = time.monotonic() + 5
        while not donefile.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.02)
        assert donefile.exists(), "o produtor não concluiu a emissão"

        proc.send_signal(signum)
        rc = proc.wait(timeout=3)
        # Só depois do wait é seguro drenar: a regressão travava até essa leitura.
        forwarded = os.read(proc.stdout.fileno(), 65536)
        assert rc == 128 + signum
        assert forwarded.startswith(b"x")
        logged = log.read_bytes()
        assert b"x" * 6000 in logged
        assert logged.endswith(f"exit={rc}\n".encode())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def test_short_option_is_not_consumed_as_log_value():
    fields, forwarded = ask._options(["--log", "-x"], consume_log=True)
    assert fields.get("log") == ""
    assert forwarded == ["-x"]


def test_log_short_option_does_not_truncate_sentinel(wrapper, tmp_path):
    sentinel = tmp_path / "-x"
    sentinel.write_text("preservar")
    result = run(
        wrapper, "--log", "-x", data=b"", cwd=tmp_path, provider="worker-ask"
    )
    assert result.returncode == 2
    assert sentinel.read_text() == "preservar"


def test_early_reader_close_turns_child_quota_into_error(tmp_path):
    stub = tmp_path / "loud"
    stub.write_text(
        "#!/usr/bin/python3\nimport sys\n"
        "sys.stdout.buffer.write(b'x' * 1048576)\nsys.stdout.buffer.flush()\n"
        "sys.exit(75)\n"
    )
    stub.chmod(0o755)
    log = tmp_path / "loud.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ASK_PRESENTATION_ACTIVE", "ASK_TITLE", "ASK_PRESENTATION")
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ASK_PATH),
            "--provider",
            "worker-ask",
            "--",
            str(stub),
            "--log",
            str(log),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        proc.stdout.close()
        rc = proc.wait(timeout=15)
        proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert rc == 1


def test_log_preserves_late_background_output(tmp_path):
    stub = tmp_path / "late"
    stub.write_text(
        "#!/usr/bin/python3\n"
        "import os, sys, time\n"
        "sys.stdout.write('FOREGROUND')\n"
        "sys.stdout.flush()\n"
        "if os.fork() == 0:\n"
        "    time.sleep(6.5)\n"
        "    sys.stdout.write('BACKGROUND')\n"
        "    sys.stdout.flush()\n"
        "    os._exit(0)\n"
    )
    stub.chmod(0o755)
    log = tmp_path / "late.log"
    result = run(stub, "--log", str(log), provider="worker-ask", data=b"")
    assert result.returncode == 0
    assert result.stdout == b"FOREGROUNDBACKGROUND"
    logged = log.read_bytes()
    assert b"FOREGROUNDBACKGROUND" in logged
    assert logged.endswith(b"exit=0\n")


def _holder_stub(tmp_path):
    pidfile = tmp_path / "desc.pid"
    stub = tmp_path / "holder"
    stub.write_text(
        "#!/usr/bin/python3\n"
        "import os, sys, time\n"
        "sys.stdout.write('FOREGROUND')\n"
        "sys.stdout.flush()\n"
        "if os.fork() == 0:\n"
        f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "    sys.stderr.write('holder\\n')\n"
        "    sys.stderr.flush()\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
    )
    stub.chmod(0o755)
    return stub, pidfile


def _kill_pidfile(pidfile):
    try:
        pid = int(Path(pidfile).read_text())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def test_drain_waits_for_descendant_eof(tmp_path):
    stub, pidfile = _holder_stub(tmp_path)
    stub.write_text(stub.read_text().replace("time.sleep(30)", "time.sleep(6.5)"))
    log = tmp_path / "holder.log"
    started = time.monotonic()
    try:
        result = run(stub, "--log", str(log), provider="worker-ask", data=b"")
        assert result.returncode == 0
        assert result.stdout.startswith(b"FOREGROUND")
        assert time.monotonic() - started < 8
    finally:
        _kill_pidfile(pidfile)


@pytest.mark.parametrize("ignore_term", [False, True, "detached"])
def test_signal_during_drain_reaches_descendant_holding_pipe(tmp_path, ignore_term):
    stub, pidfile = _holder_stub(tmp_path)
    stub.write_text(stub.read_text().replace("import os, sys, time", "import os, sys, time, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else "import os, sys, time").replace("if os.fork() == 0:", "if os.fork() == 0:\n    os.setsid()" if ignore_term == "detached" else "if os.fork() == 0:"))
    log = tmp_path / "holder.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ASK_PRESENTATION_ACTIVE", "ASK_TITLE", "ASK_PRESENTATION")
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ASK_PATH),
            "--provider",
            "worker-ask",
            "--",
            str(stub),
            "--log",
            str(log),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    descendant = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
            assert proc.poll() is None
            time.sleep(0.05)
        assert pidfile.exists() and pidfile.read_text().strip()
        descendant = int(pidfile.read_text())
        # Wrapper já saiu; a apresentação está drenando o pipe do descendente.
        time.sleep(0.5)
        assert proc.poll() is None
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        assert proc.returncode == 143
        deadline = time.monotonic() + 5
        while _pid_alive(descendant) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ignore_term == "detached" or not _pid_alive(descendant)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if descendant is not None and _pid_alive(descendant):
            try:
                os.kill(descendant, signal.SIGKILL)
            except OSError:
                pass
