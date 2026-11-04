"""Given o worker-ask sob a apresentação, When executa a cadeia, Then a saída é curta e o contrato fica intacto."""

import importlib.util
import json
import os
import pty
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "worker_router.py"
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

spec = importlib.util.spec_from_file_location("ask_presentation", ROOT / "bin/ask_presentation.py")
ask = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ask)

FAKE = r'''
import os, sys, time
name = os.path.basename(sys.argv[0])
key = name.upper().replace("-", "_")
sys.stdin.read()
time.sleep(float(os.environ.get("FAKE_SLEEP_" + key, "0")))
sys.stdout.write("RESPOSTA:" + name + "\n")
if name == "zcode-worker":
    sys.stdout.write("\n--- zcode-worker: session=sess_abc model=GLM-5.3-Flash effort=high input=24123 output=372 cacheRead=0 cacheWrite=0 ---\n")
sys.stderr.write("stderr-privado-do-wrapper\n")
sys.exit(int(os.environ.get("FAKE_RC_" + key, "0")))
'''
FOOTER = b"--- zcode-worker: session=sess_abc model=GLM-5.3-Flash effort=high input=24123 output=372 cacheRead=0 cacheWrite=0 ---"


@pytest.fixture
def chain(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"):
        path = bin_dir / name
        path.write_text("#!" + sys.executable + "\n" + FAKE)
        path.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ASK_", "WORKER_ASK_", "FAKE_")) and k != "NO_COLOR"
    }
    env.update(PATH=str(bin_dir), XDG_STATE_HOME=str(tmp_path / "state"), WORKER_ASK_MIN_TIMEOUT="0")
    env.update(HOME=str(tmp_path / "home"), CODEX_HOME=str(tmp_path / "codex"),
               CLAUDE_CONFIG_DIR=str(tmp_path / "claude"))
    return {"env": env, "work": work, "tmp": tmp_path}


def worker(chain, *extra, env=None, mode="plan"):
    environ = dict(chain["env"], **(env or {}))
    args = ["--cwd", str(chain["work"]), "--orchestrator", "claude", "--mode", mode, *extra]
    if "--spec" not in extra:
        args.append("tarefa")
    return subprocess.run([sys.executable, str(ROUTER), *args], capture_output=True, env=environ, timeout=60)


def test_success_is_two_lines_and_stdout_intact(chain):
    result = worker(chain)
    err = result.stderr.decode()
    assert result.returncode == 0
    assert result.stdout == b"RESPOSTA:zcode-worker\n\n" + FOOTER + b"\n"
    lines = err.splitlines()
    assert lines[0].startswith("◆ Workers · leitura · ")
    assert "↳ ZCode (glm-5.3-flash)" in err
    assert lines[-1].startswith("✓ ZCode (glm-5.3-flash) · ")
    assert "24.1k→372 tokens · validação pendente" in lines[-1]
    assert "rc=0" not in err and "Em execução" not in err
    assert "stderr-privado" not in err
    assert "\x1b" not in err


def test_fallback_is_summarized(chain):
    result = worker(chain, env={"FAKE_RC_ZCODE_WORKER": "75", "WORKER_ASK_NO_MUSE": "1"})
    err = result.stderr.decode()
    assert result.returncode == 0
    assert result.stdout == b"RESPOSTA:opencode-ask\n"
    assert "↷ ZCode: cota, tentando o próximo" in err
    assert err.splitlines()[-2].startswith("✓ OpenCode (auto) · ")
    assert err.splitlines()[-1] == "  tentativas: ZCode cota (rc 75) → OpenCode ✓"
    assert "rc=75, fallback" not in err


def test_write_timeout_keeps_directive(chain):
    target = chain["work"] / "a.txt"
    target.write_text("x")
    result = worker(chain, "--allowed-file", "a.txt", "--timeout", "1", mode="write",
                    env={"FAKE_SLEEP_ZCODE_WORKER": "5"})
    err = result.stderr.decode()
    assert result.returncode == 124
    assert result.stdout == b""
    assert wr.TIMEOUT_DIRECTIVE in err
    assert "parando sem fallback" not in err
    last = err.splitlines()[-1]
    assert last.startswith("× tempo limite (rc 124) · ZCode (glm-5.3-flash) · escrita sem fallback · ")
    assert "1 arquivo · limite 1.0s" in err.splitlines()[0]


@pytest.mark.parametrize("rc,label", [(1, "× falha (rc 1) · ZCode"), (75, "× sem worker disponível (rc 75)")])
def test_failures_are_short(chain, rc, label):
    env = {f"FAKE_RC_{n}": str(rc) for n in ("ZCODE_WORKER", "MUSE_ASK", "OPENCODE_ASK", "CODEX_ASK", "CLAUDE_ASK")}
    result = worker(chain, env=env)
    assert result.returncode == rc
    assert result.stderr.decode().splitlines()[-1 if rc == 1 else -2].startswith(label)


def test_full_style_and_skip_details(chain):
    (chain["tmp"] / "bin/zcode-worker").unlink()
    spec_file = chain["tmp"] / "minha-spec.md"
    spec_file.write_text("CONTEUDO_PRIVADO")
    result = worker(chain, "--spec", str(spec_file), env={"ASK_STYLE": "full", "WORKER_ASK_NO_MUSE": "1"})
    err = result.stderr.decode()
    assert result.returncode == 0
    assert "minha-spec.md" in err and "CONTEUDO_PRIVADO" not in err
    assert "orquestrador claude" in err
    assert "  · ZCode ausente" in err
    assert "tentativas: ZCode ausente → OpenCode ✓" in err


def test_json_events_from_channel(chain):
    result = worker(chain, env={"ASK_PRESENTATION": "json"})
    events = [json.loads(line) for line in result.stderr.splitlines()]
    assert [e["event"] for e in events] == ["start", "executor_start", "end"]
    assert events[-1]["exit"] == 0 and events[-1]["executor"] == "ZCode"
    assert events[-1]["usage"]["input"] == "24123"


def test_log_flag_replaces_shell_redirection(chain):
    log = chain["tmp"] / "saida.log"
    log.write_text("antigo\n")
    result = worker(chain, "--log", str(log))
    assert result.returncode == 0
    assert result.stdout.startswith(b"RESPOSTA:zcode-worker")
    text = log.read_text()
    assert "antigo" not in text
    assert "RESPOSTA:zcode-worker" in text and "✓ ZCode" in text
    assert text.endswith("exit=0\n")


def test_log_records_failure_exit(chain):
    log = chain["tmp"] / "falha.log"
    result = worker(chain, "--log", str(log), env={"FAKE_RC_ZCODE_WORKER": "1"})
    assert result.returncode == 1
    assert log.read_text().endswith("exit=1\n")
    assert log.stat().st_mode & 0o777 == 0o600


def test_open_log_tightens_preexisting_mode(tmp_path):
    for opener, name in ((wr._open_log, "router.log"), (ask.open_log, "ask.log")):
        path = tmp_path / name
        path.write_text("antigo\n")
        path.chmod(0o644)
        fd = opener(str(path))
        try:
            assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        finally:
            os.close(fd)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text() == ""


def test_status_log_records_json(chain):
    log = chain["tmp"] / "status.log"
    result = subprocess.run(
        [sys.executable, str(ROUTER), "--status", "--log", str(log)],
        capture_output=True, env=chain["env"], timeout=30,
    )
    assert result.returncode == 0
    assert "cache" in json.loads(result.stdout)
    text = log.read_text()
    assert result.stdout.decode() in text
    assert text.endswith("exit=0\n")


def test_status_log_empty_value_is_rejected(chain):
    result = subprocess.run(
        [sys.executable, str(ROUTER), "--status", "--log="],
        capture_output=True, env=chain["env"], timeout=30,
    )
    assert result.returncode == 2


def test_help_uses_last_log_value(chain):
    first = chain["tmp"] / "primeiro.log"
    first.write_text("preservar")
    second = chain["tmp"] / "segundo.log"
    result = subprocess.run(
        [sys.executable, str(ROUTER), "--help", "--log", str(first), "--log", str(second)],
        capture_output=True, env=chain["env"], timeout=30,
    )
    assert result.returncode == 0
    assert b"usage:" in result.stdout
    assert first.read_text() == "preservar"
    text = second.read_text()
    assert "usage:" in text
    assert text.endswith("exit=0\n")


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_log_records_help(chain, flag):
    log = chain["tmp"] / "help.log"
    result = subprocess.run(
        [sys.executable, str(ROUTER), flag, "--log", str(log)],
        capture_output=True, env=chain["env"], timeout=30,
    )
    assert result.returncode == 0
    assert b"usage:" in result.stdout
    text = log.read_text()
    assert "usage:" in text
    assert text.endswith("exit=0\n")


def test_router_log_write_completes_partial_writes(tmp_path, monkeypatch):
    path = tmp_path / "partial.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    monkeypatch.setattr(wr, "_LOG_FD", fd)
    monkeypatch.setattr(wr, "_LOG_FAILED", False)
    real_write = os.write
    monkeypatch.setattr(wr.os, "write", lambda fd_, data: real_write(fd_, data[:3]))
    try:
        assert wr._log_write(b"0123456789") is True
    finally:
        os.close(fd)
    assert path.read_bytes() == b"0123456789"
    assert wr._LOG_FAILED is False


def test_router_log_write_failure_is_flagged(tmp_path, monkeypatch, capsys):
    path = tmp_path / "full.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    monkeypatch.setattr(wr, "_LOG_FD", fd)
    monkeypatch.setattr(wr, "_LOG_FAILED", False)
    monkeypatch.setattr(wr, "_LOG_REPORTED", False)

    def full_disk(fd_, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(wr.os, "write", full_disk)
    try:
        assert wr._log_write(b"x") is False
        assert wr._LOG_FAILED is True
        wr._note_log_failure()
    finally:
        os.close(fd)
    assert "falha ao gravar --log" in capsys.readouterr().err


def test_router_fallback_stops_before_next_worker_on_log_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    calls = []
    monkeypatch.setattr(wr, "_LOG_FD", None)
    monkeypatch.setattr(wr, "_LOG_FAILED", False)
    monkeypatch.setattr(wr, "_LOG_REPORTED", False)
    monkeypatch.setattr(wr, "load_cache", lambda create_dir=False: {})
    monkeypatch.setattr(wr, "save_cache", lambda updates: None)
    monkeypatch.setattr(wr.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(wr, "_emit_executor_start", lambda *a, **k: None)

    def fake_run(argv_list, cwd, prompt_bytes, budget):
        calls.append(argv_list[0])
        return 75, b"", b"", False

    monkeypatch.setattr(wr, "run_wrapper", fake_run)

    def fake_event(kind, legacy=None, **data):
        if kind == "fallback":
            wr._LOG_FAILED = True

    monkeypatch.setattr(wr, "_event", fake_event)
    with pytest.raises(SystemExit) as excinfo:
        wr._main_impl(
            ["--cwd", str(work), "--orchestrator", "claude", "--mode", "plan", "tarefa"]
        )
    assert excinfo.value.code == 1
    assert len(calls) == 1


def test_view_log_completes_partial_and_records_failure(tmp_path, monkeypatch):
    path = tmp_path / "view.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    view = ask.View("plain", False, False, fd)
    real_write = os.write
    monkeypatch.setattr(ask.os, "write", lambda fd_, data: real_write(fd_, data[:3]))
    try:
        view._log(b"0123456789")
        assert view.log_error is None
    finally:
        os.close(fd)
    assert path.read_bytes() == b"0123456789"

    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    failing = ask.View("plain", False, False, fd)

    def full_disk(fd_, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ask.os, "write", full_disk)
    try:
        failing._log(b"x")
        assert failing.log_error is not None
    finally:
        os.close(fd)


def test_log_refuses_symlink(chain):
    real = chain["tmp"] / "real.log"
    real.write_text("preservar")
    link = chain["tmp"] / "link.log"
    link.symlink_to(real)
    result = worker(chain, "--log", str(link))
    assert result.returncode == 2
    assert real.read_text() == "preservar"


@pytest.mark.parametrize("value", ["off", None])
def test_log_without_presentation(chain, value):
    log = chain["tmp"] / "cru.log"
    env = {"ASK_PRESENTATION": "off"} if value else {"ASK_PRESENTATION_ACTIVE": "1"}
    result = worker(chain, "--log", str(log), env=env)
    assert result.returncode == 0
    text = log.read_text()
    assert "RESPOSTA:zcode-worker" in text and text.endswith("exit=0\n")
    if value == "off":
        assert result.stderr == b""


def test_spoofed_event_fd_is_ignored(chain):
    result = worker(chain, env={"ASK_PRESENTATION_ACTIVE": "1", "ASK_PRESENTATION_FD": "2:1"})
    assert result.returncode == 0
    assert b"zcode-worker: rc=0" in result.stderr


def test_live_line_on_tty(chain):
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(ROUTER), "--cwd", str(chain["work"]), "--orchestrator", "claude",
             "--mode", "plan", "tarefa"],
            stdout=subprocess.PIPE, stderr=slave, env=dict(chain["env"], FAKE_SLEEP_ZCODE_WORKER="1", TERM="xterm"),
        )
        os.close(slave)
        slave = None
        stdout, _ = proc.communicate(timeout=30)
        chunks = []
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
    screen = b"".join(chunks).decode()
    assert proc.returncode == 0
    assert stdout.startswith(b"RESPOSTA:zcode-worker")
    assert "\r\x1b[2K" in screen and "ZCode (glm-5.3-flash) · " in screen
    assert "\x1b[32m✓ ZCode" in screen
    assert "↳" not in screen


def test_parse_usage_and_formatters():
    assert wr._parse_usage(b"ok\n" + FOOTER + b"\n")["output"] == "372"
    assert wr._parse_usage(b"sem rodape") == {}
    assert wr._parse_usage(b"--- x: input=\x1b[31m ---") == {}
    assert ask.fmt_duration(20.34) == "20.3s" and ask.fmt_duration(182.7) == "3m02s"
    assert ask.fmt_duration(3720) == "1h02m"
    assert ask.fmt_tokens("24123") == "24.1k" and ask.fmt_tokens(2_500_000) == "2.5M"
    assert ask.fmt_limit("2400") == "40min" and ask.fmt_limit("nan") == ""
    assert ask.short_path("/a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q/r/s/t/u/v") == "…/u/v"


@pytest.mark.parametrize("env,expected", [
    ({}, ("auto", "compact")),
    ({"ASK_STYLE": "full"}, ("auto", "full")),
    ({"WORKER_ASK_STYLE": "plain"}, ("plain", "compact")),
    ({"ASK_PRESENTATION": "json", "ASK_STYLE": "plain"}, ("json", "compact")),
    ({"ASK_PRESENTATION": "xyz", "ASK_STYLE": "xyz"}, ("auto", "compact")),
])
def test_style_resolution(monkeypatch, env, expected):
    for key in ("ASK_PRESENTATION", "ASK_STYLE", "WORKER_ASK_STYLE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert ask._styles() == expected
