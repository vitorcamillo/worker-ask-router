"""Cobertura in-process de worker_router._main_impl (fatia B do quality gate).

Chama wr._main_impl diretamente (SystemExit capturado) com wrappers falsos no
PATH, isolando HOME/XDG_STATE_HOME em tmp_path — mesmo padrao da fixture
fake_env de tests/test_worker_router.py, mas sem subprocess do roteador.
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402
import quota_provider  # noqa: E402

FAKE_NAMES = ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"]

# Executaveis falsos, um por binario real (subconjunto dos modos de
# test_worker_router.py: registro de chamada, sleep e escrita de arquivo).
FAKE_SCRIPT = r'''
import json, os, sys, time

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

key = name.upper().replace("-", "_")
mode = os.environ.get("FAKE_MODE_" + key, os.environ.get("FAKE_MODE", "exit"))
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "10")))
elif mode in ("writefile", "writesleep"):
    rel = os.environ.get("FAKE_WRITE_REL")
    cwd = os.environ.get("FAKE_CWD")
    if rel and cwd:
        full = os.path.join(cwd, rel)
        d = os.path.dirname(full)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(full, "w") as f:
            f.write(os.environ.get("FAKE_WRITE_CONTENT", "x"))
    if mode == "writesleep":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "10")))

rc = int(os.environ.get("FAKE_RC_" + key, os.environ.get("FAKE_RC", "0")))
sys.stdout.write(os.environ.get("FAKE_STDOUT_" + key, "READY:" + name))
sys.stderr.write(os.environ.get("FAKE_STDERR_" + key, os.environ.get("FAKE_STDERR", "private-worker-stderr")))
sys.exit(rc)
'''


@pytest.fixture
def cov_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("WORKER_ASK_ACTIVE", raising=False)
    monkeypatch.delenv("WORKER_ASK_NO_MUSE", raising=False)
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
    # Start Plan desligado por padrão: cooldown do zcode vale (ver test_quota_provider).
    monkeypatch.setenv("ZCODE_WORKER_START_PLAN", "0")
    monkeypatch.setenv("ASK_PRESENTATION_ACTIVE", "1")
    monkeypatch.delenv("ASK_PRESENTATION_FD", raising=False)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("FAKE_CWD", str(work))
    # Estado global do roteador isolado e restaurado pelo monkeypatch.
    for attr, value in (("_LOG_FD", None), ("_RUN", None), ("_RUN_T0", None),
                        ("_RUN_WRITTEN", False), ("_LOG_FAILED", False),
                        ("_LOG_REPORTED", False), ("_INTERRUPTED", False),
                        ("_LOG_ENDS_WITH_NEWLINE", True)):
        monkeypatch.setattr(wr, attr, value)
    return {"bin": bin_dir, "state": state, "work": work, "tmp": tmp_path, "log": log}


def run_main(argv):
    with pytest.raises(SystemExit) as exc:
        wr._main_impl(argv)
    return exc.value.code


def base_args(env, *extra, mode="plan", orch="codex", prompt="task"):
    args = ["--cwd", str(env["work"]), "--orchestrator", orch, "--mode", mode]
    if prompt is not None:
        args.append(prompt)
    return args + list(extra)


def records(env):
    if not env["log"].exists():
        return []
    return [json.loads(line) for line in env["log"].read_text().splitlines()]


def cache_path(env):
    return env["state"] / "worker-ask" / "cache.json"


# --- help e --log -----------------------------------------------------------


def test_help_with_log_writes_help_to_file(cov_env):
    log = cov_env["tmp"] / "help.log"
    assert run_main(["--help", "--log", str(log)]) == 0
    assert "usage:" in log.read_text()


def test_help_with_preset_log_fd_skips_reopen(cov_env, monkeypatch, capsys):
    fd = os.open(str(cov_env["tmp"] / "pre.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    monkeypatch.setattr(wr, "_LOG_FD", fd)
    try:
        assert run_main(["--help", "--log", "ignored.log"]) == 0
    finally:
        os.close(fd)
    assert "usage:" in capsys.readouterr().out
    assert "usage:" in (cov_env["tmp"] / "pre.log").read_text()


def test_help_with_unopenable_log_exits_2(cov_env):
    assert run_main(["--help", "--log", str(cov_env["tmp"])]) == 2


def test_main_with_unopenable_log_exits_2(cov_env):
    assert run_main(base_args(cov_env, "--log", str(cov_env["tmp"]))) == 2


# --- status -----------------------------------------------------------------


def test_status_shows_cache_entries(cov_env, capsys):
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": time.time() + 60}})
    assert run_main(["--status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state_dir"] == str(cov_env["state"] / "worker-ask")
    entry = data["cache"]["zcode-worker"]
    assert 0 < entry["ttl_remaining"] <= 60
    # codex-terra ausente do cache: ramo de entrada vazia do laco.
    assert "codex-terra" not in data["cache"]


def test_status_with_broken_cache_symlink_exits_1(cov_env):
    d = cov_env["state"] / "worker-ask"
    d.mkdir(parents=True)
    (d / "cache.json").symlink_to(d / "missing")
    assert run_main(["--status"]) == 1


def test_status_log_write_failure_exits_1(cov_env, monkeypatch, capsys):
    def broken(*args):
        raise OSError("fixture")
    monkeypatch.setattr(wr, "_write_all", broken)
    log = cov_env["tmp"] / "status.log"
    assert run_main(["--status", "--log", str(log)]) == 1
    assert "falha ao gravar --log" in capsys.readouterr().err


# --- travas e validacao de argumentos ---------------------------------------


def test_recursion_blocked(cov_env, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_ACTIVE", "1")
    assert run_main(base_args(cov_env)) == 2
    assert "delegacao recursiva" in capsys.readouterr().err


@pytest.mark.parametrize("flag,value", [("--timeout", "nan"), ("--step-timeout", "inf")])
def test_nonfinite_timeouts_rejected(cov_env, flag, value, capsys):
    assert run_main(base_args(cov_env, flag, value)) == 2
    assert "deve ser float positivo finito" in capsys.readouterr().err


def test_timeout_below_floor_is_raised_not_rejected(cov_env, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_MIN_TIMEOUT", "5")
    assert run_main(base_args(cov_env, "--timeout", "1", "--step-timeout", "1")) == 0
    assert "abaixo do minimo" in capsys.readouterr().err
    assert records(cov_env)[0]["name"] == "zcode-worker"


def test_missing_cwd_rejected(cov_env, capsys):
    argv = ["--orchestrator", "codex", "--mode", "plan", "task"]
    assert run_main(argv) == 2
    assert "--cwd obrigatorio" in capsys.readouterr().err


def test_cwd_must_be_directory(cov_env, capsys):
    not_dir = cov_env["tmp"] / "file.txt"
    not_dir.write_text("x")
    assert run_main(["--cwd", str(not_dir), "--orchestrator", "codex", "task"]) == 2
    assert "--cwd deve ser diretorio valido" in capsys.readouterr().err


def test_missing_orchestrator_rejected(cov_env, capsys):
    assert run_main(["--cwd", str(cov_env["work"]), "--mode", "plan", "task"]) == 2
    assert "--orchestrator obrigatorio" in capsys.readouterr().err


def test_excess_allowed_files_rejected(cov_env, capsys):
    argv = base_args(cov_env, *(["--allowed-file", "x"] * 129))
    assert run_main(argv) == 2
    assert "excesso de --allowed-file" in capsys.readouterr().err


def test_write_mode_requires_allowed_file(cov_env, capsys):
    assert run_main(base_args(cov_env, mode="write")) == 2
    assert "--allowed-file obrigatorio para --mode write" in capsys.readouterr().err


def test_invalid_allowed_file_rejected(cov_env, capsys):
    argv = base_args(cov_env, "--allowed-file", "../x")
    assert run_main(argv) == 2
    assert "erro:" in capsys.readouterr().err


# --- fontes de prompt --------------------------------------------------------


def test_spec_and_prompt_conflict_rejected(cov_env, capsys):
    spec = cov_env["tmp"] / "spec.md"
    spec.write_text("hello")
    assert run_main(base_args(cov_env, "--spec", str(spec))) == 2
    assert "apenas uma fonte de prompt" in capsys.readouterr().err


def test_spec_success_forwards_literal_content(cov_env):
    spec = cov_env["tmp"] / "spec.md"
    spec.write_text("hello spec")
    assert run_main(base_args(cov_env, "--spec", str(spec), prompt=None)) == 0
    assert records(cov_env)[0]["stdin"] == "hello spec"


def test_sensitive_spec_rejected(cov_env):
    spec = cov_env["tmp"] / "auth.json"
    spec.write_text("secret")
    assert run_main(base_args(cov_env, "--spec", str(spec), prompt=None)) == 2


def test_missing_spec_rejected(cov_env):
    assert run_main(base_args(cov_env, "--spec", str(cov_env["tmp"] / "absent.md"))) == 2


def test_oversize_spec_rejected(cov_env):
    spec = cov_env["tmp"] / "big.md"
    spec.write_text("x" * (wr.MAX_INPUT_BYTES + 1))
    assert run_main(base_args(cov_env, "--spec", str(spec), prompt=None)) == 2


def test_non_utf8_spec_rejected(cov_env):
    spec = cov_env["tmp"] / "bad.md"
    spec.write_bytes(b"\xff\xfe")
    assert run_main(base_args(cov_env, "--spec", str(spec), prompt=None)) == 2


class _FakeStdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


def test_stdin_prompt_is_forwarded(cov_env, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"via stdin"))
    assert run_main(base_args(cov_env, prompt=None)) == 0
    assert records(cov_env)[0]["stdin"] == "via stdin"


def test_stdin_read_error_rejected(cov_env, monkeypatch):
    class Broken:
        class buffer:
            @staticmethod
            def read(*_):
                raise OSError("fixture")
    monkeypatch.setattr(sys, "stdin", Broken)
    assert run_main(base_args(cov_env, prompt=None)) == 2


def test_stdin_oversize_rejected(cov_env, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"x" * (wr.MAX_INPUT_BYTES + 1)))
    assert run_main(base_args(cov_env, prompt=None)) == 2


def test_stdin_non_utf8_rejected(cov_env, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"\xff\xfe"))
    assert run_main(base_args(cov_env, prompt=None)) == 2


def test_empty_stdin_prompt_rejected(cov_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b""))
    assert run_main(base_args(cov_env, prompt=None)) == 2
    assert "prompt vazio" in capsys.readouterr().err


def test_oversize_positional_prompt_rejected(cov_env):
    argv = base_args(cov_env, prompt="x" * (wr.MAX_INPUT_BYTES + 1))
    assert run_main(argv) == 2


def test_prefix_pushing_prompt_over_limit_rejected(cov_env):
    argv = base_args(cov_env, "--allowed-file", "a" * 200 + ".txt", mode="write",
                     prompt="x" * (wr.MAX_INPUT_BYTES - 10))
    assert run_main(argv) == 2


# --- cache, cadeia e fallback ------------------------------------------------


def test_cache_symlink_aborts_before_chain(cov_env):
    d = cov_env["state"] / "worker-ask"
    d.mkdir(parents=True)
    (d / "cache.json").symlink_to(d / "missing")
    assert run_main(base_args(cov_env)) == 1


def test_success_forwards_stdout(cov_env, capsys):
    assert run_main(base_args(cov_env)) == 0
    out, err = capsys.readouterr()
    assert out == "READY:zcode-worker"
    assert "zcode-worker: rc=0" in err


def test_write_mode_adds_allowed_prefix_and_snapshots(cov_env, capsys):
    (cov_env["work"] / "a.txt").write_text("original")
    argv = base_args(cov_env, "--allowed-file", "a.txt", mode="write")
    assert run_main(argv) == 0
    assert capsys.readouterr().out == "READY:zcode-worker"
    assert records(cov_env)[0]["stdin"].startswith("[worker-router] Arquivos autorizados")


def test_success_with_log_write_failure_exits_1(cov_env, monkeypatch, capsys):
    def broken(*args):
        raise OSError("fixture")
    monkeypatch.setattr(wr, "_write_all", broken)
    log = cov_env["tmp"] / "run.log"
    argv = base_args(cov_env, "--log", str(log))
    assert run_main(argv) == 1
    assert "falha ao gravar --log" in capsys.readouterr().err


def test_cooldown_skips_wrapper(cov_env):
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": time.time() + 60}})
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert [r["name"] for r in records(cov_env)] == ["opencode-ask"]


def test_retry_exhausted_overrides_cooldown_and_clears_entry(cov_env):
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": time.time() + 60}})
    argv = base_args(cov_env, "--retry-exhausted")
    assert run_main(argv) == 0
    assert "zcode-worker" not in wr.load_cache()


def test_missing_wrapper_is_skipped(cov_env, capsys):
    (cov_env["bin"] / "zcode-worker").unlink()
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert "wrapper ausente" in capsys.readouterr().err
    assert [r["name"] for r in records(cov_env)] == ["opencode-ask"]


def test_quota_reset_known_skips_whole_chain(cov_env, monkeypatch):
    monkeypatch.setattr(quota_provider, "query_reset",
                        lambda name, budget=10.0: time.time() + 600)
    assert run_main(base_args(cov_env)) == 75
    assert records(cov_env) == []
    cache = wr.load_cache()
    assert set(cache) == set(FAKE_NAMES)
    assert all(e["reset_known"] is True for e in cache.values())


def test_quota_save_failure_exits_1(cov_env, monkeypatch):
    monkeypatch.setattr(quota_provider, "query_reset",
                        lambda name, budget=10.0: time.time() + 600)
    def broken(*args, **kwargs):
        raise RuntimeError("fixture")
    monkeypatch.setattr(wr, "save_cache", broken)
    assert run_main(base_args(cov_env)) == 1


def test_quota_query_past_deadline_exits_124(cov_env, monkeypatch):
    def slow(name, budget=10.0):
        time.sleep(0.25)
        return None
    monkeypatch.setattr(quota_provider, "query_reset", slow)
    assert run_main(base_args(cov_env, "--timeout", "0.1")) == 124


def test_snapshot_failure_exits_1(cov_env, monkeypatch):
    def broken(*args):
        raise RuntimeError("fixture")
    monkeypatch.setattr(wr, "snapshot_allowed", broken)
    argv = base_args(cov_env, "--allowed-file", "a.txt", mode="write")
    assert run_main(argv) == 1


def test_spawn_file_error_skips_and_exhausts(cov_env, monkeypatch, capsys):
    monkeypatch.setattr(wr.shutil, "which",
                        lambda name: str(cov_env["bin"] / "fantasma"))
    assert run_main(base_args(cov_env)) == 75
    assert "wrapper ausente" in capsys.readouterr().err


def test_run_wrapper_runtime_error_exits_1(cov_env, monkeypatch):
    def broken(*args):
        raise RuntimeError("fixture")
    monkeypatch.setattr(wr, "run_wrapper", broken)
    assert run_main(base_args(cov_env)) == 1


def test_remaining_timeout_before_spawn_exits_124(cov_env, monkeypatch):
    real_which = wr.shutil.which
    def slow_which(name):
        time.sleep(0.25)
        return real_which(name)
    monkeypatch.setattr(wr.shutil, "which", slow_which)
    argv = base_args(cov_env, "--retry-exhausted", "--timeout", "0.1")
    assert run_main(argv) == 124


# --- timeout do executor ------------------------------------------------------


@pytest.fixture
def queue_timeout_small(monkeypatch):
    monkeypatch.setenv("WORKER_ASK_ZCODE_QUEUE_TIMEOUT", "0.001")


def test_write_timeout_with_progress_stops_without_fallback(cov_env, queue_timeout_small,
                                                            monkeypatch, capsys):
    monkeypatch.setenv("FAKE_MODE_ZCODE_WORKER", "writesleep")
    monkeypatch.setenv("FAKE_WRITE_REL", "a.txt")
    monkeypatch.setenv("FAKE_SLEEP", "5")
    argv = base_args(cov_env, "--allowed-file", "a.txt",
                     "--step-timeout", "0.5", mode="write")
    assert run_main(argv) == 124
    assert "estourou com progresso" in capsys.readouterr().err
    assert [r["name"] for r in records(cov_env)] == ["zcode-worker"]


def test_write_timeout_without_progress_saves_cooldown(cov_env, queue_timeout_small, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_MODE_ZCODE_WORKER", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "5")
    argv = base_args(cov_env, "--allowed-file", "a.txt",
                     "--step-timeout", "0.5", mode="write")
    assert run_main(argv) == 124
    err = capsys.readouterr().err
    assert "parando sem fallback" in err
    assert "execucao interrompida por timeout" in err
    entry = wr.load_cache()["zcode-worker"]
    assert entry["blocked_reason"] == "timeout"
    assert [r["name"] for r in records(cov_env)] == ["zcode-worker"]


def test_plan_timeout_falls_back_to_next(cov_env, queue_timeout_small, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_MODE_ZCODE_WORKER", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "5")
    argv = base_args(cov_env, "--step-timeout", "0.5", "--no-muse")
    assert run_main(argv) == 0
    assert "rc=timeout, fallback (plan)" in capsys.readouterr().err
    assert [r["name"] for r in records(cov_env)] == ["zcode-worker", "opencode-ask"]


def test_timeout_cooldown_save_failure_exits_1(cov_env, queue_timeout_small, monkeypatch):
    monkeypatch.setenv("FAKE_MODE_ZCODE_WORKER", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "5")
    def broken(*args, **kwargs):
        raise RuntimeError("fixture")
    monkeypatch.setattr(wr, "save_cache", broken)
    argv = base_args(cov_env, "--step-timeout", "0.5", "--no-muse")
    assert run_main(argv) == 1


def test_deadline_checked_at_loop_top(cov_env, monkeypatch, capsys):
    calls = []
    def slow_run(argv, cwd, prompt, budget):
        calls.append(argv)
        time.sleep(0.3)
        return 124, b"", b"", True
    monkeypatch.setattr(wr, "run_wrapper", slow_run)
    argv = base_args(cov_env, "--timeout", "0.2", "--no-muse")
    assert run_main(argv) == 124
    assert len(calls) == 1
    assert "execucao interrompida por timeout" in capsys.readouterr().err


def test_had_timeout_after_exhausted_chain_exits_124(cov_env, monkeypatch, capsys):
    for name in FAKE_NAMES[1:]:
        (cov_env["bin"] / name).unlink()
    monkeypatch.setattr(wr, "run_wrapper",
                        lambda argv, cwd, prompt, budget: (124, b"", b"", True))
    argv = base_args(cov_env, "--timeout", "30", "--no-muse")
    assert run_main(argv) == 124
    assert "todos os workers indisponiveis ou com timeout" in capsys.readouterr().err


# --- rc75 ---------------------------------------------------------------------


def test_rc75_falls_back_and_caches_unknown_cooldown(cov_env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "75")
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert capsys.readouterr().out == "READY:opencode-ask"
    entry = wr.load_cache()["zcode-worker"]
    assert entry["blocked_reason"] == "quota"
    assert entry["reset_known"] is False
    fallbacks = [a for a in wr._RUN["attempts"] if a.get("event") == "fallback"]
    assert fallbacks and fallbacks[-1]["reason"] == "cota"
    assert [r["name"] for r in records(cov_env)] == ["zcode-worker", "opencode-ask"]


def test_rc75_with_changed_files_stops(cov_env, monkeypatch, capsys):
    (cov_env["work"] / "a.txt").write_text("original")
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "75")
    monkeypatch.setenv("FAKE_MODE_ZCODE_WORKER", "writefile")
    monkeypatch.setenv("FAKE_WRITE_REL", "a.txt")
    monkeypatch.setenv("FAKE_WRITE_CONTENT", "changed")
    argv = base_args(cov_env, "--allowed-file", "a.txt", "--no-muse", mode="write")
    assert run_main(argv) == 1
    assert "arquivos autorizados alterados" in capsys.readouterr().err
    assert [r["name"] for r in records(cov_env)] == ["zcode-worker"]


def test_rc75_with_reset_marker_uses_known_reset(cov_env, monkeypatch):
    reset = time.time() + 1800
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "75")
    monkeypatch.setenv("FAKE_STDERR_ZCODE_WORKER", f"worker-ask-quota-reset: {reset:.6f}")
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    entry = wr.load_cache()["zcode-worker"]
    assert entry["reset_known"] is True
    assert abs(entry["until"] - reset) < 1.0


def test_rc75_admission_busy_does_not_touch_cache(cov_env, monkeypatch):
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "75")
    monkeypatch.setenv("FAKE_STDERR_ZCODE_WORKER",
                       "ERRO: fila de admissao cheia: nenhum worker iniciado")
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert wr.load_cache() == {}
    fallbacks = [a for a in wr._RUN["attempts"] if a.get("event") == "fallback"]
    assert fallbacks and fallbacks[-1]["reason"] == "fila"


def test_rc75_log_failure_exits_1(cov_env, monkeypatch, capsys):
    def broken(*args):
        raise OSError("fixture")
    monkeypatch.setattr(wr, "_write_all", broken)
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "75")
    log = cov_env["tmp"] / "run.log"
    argv = base_args(cov_env, "--log", str(log), "--no-muse")
    assert run_main(argv) == 1
    assert "falha ao gravar --log" in capsys.readouterr().err


# --- codigos de saida diversos -------------------------------------------------


@pytest.mark.parametrize("rc", [143, 144])
def test_interrupted_rc_stops_without_fallback(cov_env, monkeypatch, rc):
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", str(rc))
    assert run_main(base_args(cov_env)) == rc


def test_generic_failure_rc_stops(cov_env, monkeypatch):
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "2")
    assert run_main(base_args(cov_env)) == 2


def test_launcher_diagnostic_falls_back(cov_env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "1")
    monkeypatch.setenv("FAKE_STDERR",
                       "ERRO: launcher de autenticacao incompativel com o bundle do ZCode")
    assert run_main(base_args(cov_env)) == 0
    assert "Seguindo para o proximo worker (rc=1)" in capsys.readouterr().err


def test_launcher_diagnostic_with_effects_stops_without_fallback(cov_env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "1")
    monkeypatch.setenv("FAKE_STDERR",
                       "ERRO: launcher de autenticacao incompativel com o bundle do ZCode"
                       " Arquivos podem ter sido alterados; revise o diff antes de tentar novamente.")
    assert run_main(base_args(cov_env)) == 1
    assert "fallback bloqueado (rc=1)" in capsys.readouterr().err


# --- opt-out do Muse ------------------------------------------------------------


@pytest.mark.parametrize("marker", [False, True])
def test_muse_chain_default_and_marker_optout(cov_env, monkeypatch, marker):
    if marker:
        (cov_env["work"] / ".worker-ask-no-muse").write_text("")
    monkeypatch.setenv("FAKE_RC", "75")
    assert run_main(base_args(cov_env)) == 75
    names = [r["name"] for r in records(cov_env)]
    if marker:
        assert names == ["zcode-worker", "opencode-ask", "codex-ask", "claude-ask"]
    else:
        assert names == FAKE_NAMES


def test_no_muse_flag_and_env_optout(cov_env, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "75")
    monkeypatch.setenv("WORKER_ASK_NO_MUSE", "1")
    assert run_main(base_args(cov_env)) == 75
    assert [r["name"] for r in records(cov_env)] == \
        ["zcode-worker", "opencode-ask", "codex-ask", "claude-ask"]
    # Sem env, a flag --no-muse tambem exclui o Muse.
    monkeypatch.delenv("WORKER_ASK_NO_MUSE")
    assert run_main(base_args(cov_env, "--no-muse")) == 75
    assert [r["name"] for r in records(cov_env)][-4:] == \
        ["zcode-worker", "opencode-ask", "codex-ask", "claude-ask"]
