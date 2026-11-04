"""Regressoes da remocao do modo --local do worker_router.

O roteador nao oferece mais integracao --local: as flags antigas sao
recusadas com rc=2, o stub codex-local nunca e chamado pela cadeia e a
cadeia padrao ZCode->Muse->OpenCode->Codex->Claude permanece intacta.
"""
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

FAKE_NAMES = ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"]

FAKE_SCRIPT = r'''
import json, os, sys

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
    monkeypatch.setenv("ASK_PRESENTATION_ACTIVE", "1")
    monkeypatch.delenv("ASK_PRESENTATION_FD", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    for key in ("MUSE_ASK_MODEL", "MUSE_ASK_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    return {"bin": bin_dir, "state": state, "work": work, "tmp": tmp_path}


def run_cli(fake_env, extra_args, env_extra=None, input_text=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]),
           "--orchestrator", "codex"] + extra_args
    return subprocess.run(cmd, input=input_text.encode() if input_text is not None else None,
                          capture_output=True, env=env, timeout=60)


def test_default_chain_intacta_sem_local():
    for orch in ("codex", "claude", "zcode", "muse", "opencode"):
        chain = wr.wrapper_chain(orch, True, "write")
        names = [n for n, _ in chain]
        assert names == ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"]
        assert "codex-local" not in names
    assert "codex-local" not in wr.CACHE_KEYS
    assert "codex-local" not in wr._EXECUTOR_LABELS


def test_build_argv_sem_parametros_locais(tmp_path):
    params = inspect.signature(wr.build_argv).parameters
    assert "local_model" not in params
    assert "local_base_url" not in params
    assert "local_timeout" not in params
    argv = wr.build_argv("codex-ask", "/bin/codex-ask", "write", str(tmp_path))
    assert argv[argv.index("--mode") + 1] == "write"
    assert argv[argv.index("--cwd") + 1] == os.path.abspath(str(tmp_path))


@pytest.mark.parametrize("flag", ["--local", "--local-model", "--local-base-url"])
def test_flags_locais_recusadas_rc2_sem_executar_stub(fake_env, flag):
    log = fake_env["tmp"] / "calls.jsonl"
    antiga = [flag] if flag == "--local" else [flag, "x"]
    r = run_cli(fake_env, ["--mode", "plan", *antiga, "hi"],
                {"FAKE_LOG": str(log), "FAKE_RC": "0"})
    assert r.returncode == 2
    assert not log.exists()


def test_local_completo_recusado_rc2_sem_executar_stub(fake_env):
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "--local",
                           "--local-model", "m1",
                           "--local-base-url", "http://127.0.0.1:9",
                           "tarefa"],
                {"FAKE_LOG": str(log), "FAKE_RC": "0"})
    assert r.returncode == 2
    assert not log.exists()


def test_codex_local_no_path_nao_chamado_pela_cadeia(fake_env):
    stub = fake_env["bin"] / "codex-local"
    stub.write_text("#!" + sys.executable + "\n" + FAKE_SCRIPT + "\n")
    stub.chmod(0o755)
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "tarefa normal"],
                {"FAKE_LOG": str(log), "FAKE_RC": "0"})
    assert r.returncode == 0
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert "codex-local" not in names
    assert names[0] == "zcode-worker"


def test_validacao_allowed_file_write_continua(fake_env):
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "tarefa sem allowed"],
                {"FAKE_LOG": str(log), "FAKE_RC": "0"})
    assert r.returncode == 2
    assert not log.exists()


def test_validacao_allowed_file_invalido_continua(fake_env):
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "../fora.txt", "tarefa"],
                {"FAKE_LOG": str(log), "FAKE_RC": "0"})
    assert r.returncode == 2
    assert not log.exists()


def test_recursao_bloqueada_continua(fake_env):
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "plan", "hi"],
                {"FAKE_LOG": str(log), "FAKE_RC": "0", "WORKER_ASK_ACTIVE": "1"})
    assert r.returncode == 2
    assert not log.exists()


def test_fluxo_normal_com_prefixo_allowed_file(fake_env):
    log = fake_env["tmp"] / "calls.jsonl"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "tarefa normal"],
                {"FAKE_LOG": str(log), "FAKE_RC": "0"})
    assert r.returncode == 0
    recs = [json.loads(line) for line in log.read_text().splitlines()]
    assert recs[0]["name"] == "zcode-worker"
    assert "[worker-router]" in recs[0]["stdin"]


def _isola_globais_log(monkeypatch):
    monkeypatch.setattr(wr, "_LOG_FD", None)
    monkeypatch.setattr(wr, "_LOG_FAILED", False)
    monkeypatch.setattr(wr, "_LOG_REPORTED", False)
    monkeypatch.setattr(wr, "_LOG_ENDS_WITH_NEWLINE", True)


def _fecha_log_aberto():
    fd = wr._LOG_FD
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.mark.parametrize("estilo", ["espaco", "igual", "sem-valor", "flag-seguinte", "sem-log"])
def test_help_direto_sem_processo(tmp_path, monkeypatch, capsys, estilo):
    _isola_globais_log(monkeypatch)
    log = tmp_path / "ajuda.log"
    if estilo == "espaco":
        argv = ["--help", "--log", str(log)]
        espera_log = True
    elif estilo == "igual":
        argv = ["--help", "--log=" + str(log)]
        espera_log = True
    elif estilo == "sem-valor":
        argv = ["--help", "--log"]
        espera_log = False
    elif estilo == "flag-seguinte":
        argv = ["--help", "--log", "--flag"]
        espera_log = False
    else:
        argv = ["--help"]
        espera_log = False
    try:
        with pytest.raises(SystemExit) as exc:
            wr._main_impl(argv)
        assert exc.value.code == 0
        ajuda = capsys.readouterr().out
        assert "usage" in ajuda.lower()
        assert "--local" not in ajuda
        if espera_log:
            texto = log.read_text(encoding="utf-8")
            assert "usage" in texto.lower()
            assert "--local" not in texto
    finally:
        _fecha_log_aberto()


def test_help_direto_log_diretorio_erro_seguro(tmp_path, monkeypatch, capsys):
    _isola_globais_log(monkeypatch)
    pasta = tmp_path / "logs"
    pasta.mkdir()
    try:
        with pytest.raises(SystemExit) as exc:
            wr._main_impl(["--help", "--log", str(pasta)])
        assert exc.value.code == 2
        assert wr._LOG_FD is None
        assert pasta.is_dir()
    finally:
        _fecha_log_aberto()


def test_help_direto_falha_escrita_log_rc1(tmp_path, monkeypatch, capsys):
    _isola_globais_log(monkeypatch)
    monkeypatch.setattr(wr, "_log_write", lambda data: False)
    log = tmp_path / "ajuda.log"
    try:
        with pytest.raises(SystemExit) as exc:
            wr._main_impl(["--help", "--log", str(log)])
        assert exc.value.code == 1
    finally:
        _fecha_log_aberto()


def test_help_direto_open_log_oserror_rc2(tmp_path, monkeypatch, capsys):
    _isola_globais_log(monkeypatch)

    def _boom(path):
        raise OSError("disco indisponivel")

    monkeypatch.setattr(wr, "_open_log", _boom)
    with pytest.raises(SystemExit) as exc:
        wr._main_impl(["--help", "--log", str(tmp_path / "x.log")])
    assert exc.value.code == 2
    assert wr._LOG_FD is None


def test_build_argv_nome_desconhecido(tmp_path):
    saida = wr.build_argv("executor-desconhecido", "/bin/executor-desconhecido",
                          "write", str(tmp_path))
    assert saida[0] == "/bin/executor-desconhecido"
    assert saida[1] == "--cwd"
    assert saida[2] == os.path.abspath(str(tmp_path))
    assert len(saida) == 3
