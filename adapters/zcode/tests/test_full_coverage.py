"""Fechamento de cobertura de linha e branch em cli/features/jobs.

Complementa as suítes existentes com caminhos adversariais locais: nenhum
teste aqui toca rede, modelos ou o estado de produção; processos são filhos
curtos criados em ``tmp_path`` e erros de sistema são simulados por
monkeypatch escopado no alvo exato.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid as uuid_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from zcode_worker import base, cli, features, jobs


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(base, "STATE", str(state))
    monkeypatch.setattr(base, "PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setattr(jobs, "_PROC_ROOT", str(tmp_path / "proc"))
    jobs.configure(base)
    yield state
    jobs.configure(base)


def write_provider(path: Path):
    path.write_text(
        json.dumps(
            {
                "defaultModelSelection": {
                    "providerId": base.PROVIDER_ID,
                    "modelId": "GLM-5.3-Flash",
                    "options": {"reasoningLevel": "high"},
                }
            }
        ),
        encoding="utf-8",
    )


def feature_args(**overrides):
    values = {
        "tarefa": None,
        "cwd": os.getcwd(),
        "mode": "plan",
        "spec": None,
        "timeout": 2.0,
        "queue_timeout": 2.0,
        "total_timeout": None,
        "model": "glm-5.3-flash",
        "effort": "high",
        "profile": None,
        "resume": None,
        "attach": [],
        "disallowed_tools": [],
        "max_output_bytes": None,
        "json": False,
        "dry_run": False,
        "doctor": False,
        "list_models": False,
        "saida": None,
        "status": None,
        "cancel": None,
        "_job_child": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# cli._invoke_limited: BlockingIOError, deadline pós-loop e wait estourado
# ---------------------------------------------------------------------------


def test_invoke_limited_tolerates_transient_blocking_read(monkeypatch):
    real_read = os.read
    calls = []

    def fake_read(fd, size):
        # A primeira chamada acontece dentro do Popen (errpipe) e deve passar.
        calls.append(fd)
        if len(calls) == 2:
            raise BlockingIOError()
        return real_read(fd, size)

    monkeypatch.setattr(cli.os, "read", fake_read)
    code, stdout, stderr = cli._invoke(
        [sys.executable, "-c", "print('payload')"], dict(os.environ), time.monotonic() + 10
    )
    assert code == 0 and "payload" in stdout and stderr == ""


def test_invoke_limited_deadline_expires_after_pipes_close(monkeypatch):
    ticks = iter([0.0, 12.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    with pytest.raises(features.FeatureError, match="prazo"):
        cli._invoke(
            [sys.executable, "-c", "pass"], dict(os.environ), 10.0
        )


def test_invoke_limited_wait_times_out_while_process_holds_no_pipes():
    child = (
        "import os, time\n"
        "os.close(1)\nos.close(2)\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(features.FeatureError, match="prazo"):
        cli._invoke(
            [sys.executable, "-c", child], dict(os.environ), time.monotonic() + 0.5
        )


def test_classify_process_failure_maps_cli_124_to_timeout():
    assert cli._classify_process_failure(124, "qualquer stderr").code == "timeout"


# ---------------------------------------------------------------------------
# cli._session_cwd_matches: schema sem chave/localização
# ---------------------------------------------------------------------------


def test_session_cwd_matches_requires_identifying_columns(tmp_path):
    odd = tmp_path / "odd.sqlite"
    connection = sqlite3.connect(odd)
    connection.execute("create table session(nota text)")
    connection.commit()
    connection.close()
    assert cli._session_cwd_matches("sess_one", str(tmp_path), odd) is False


# ---------------------------------------------------------------------------
# cli._termination_handlers em thread sem permissão de instalar sinais
# ---------------------------------------------------------------------------


def test_termination_handlers_in_thread_degrade_safely():
    results = []

    def work():
        with cli._termination_handlers():
            results.append(threading.current_thread().name)

    thread = threading.Thread(target=work, name="sem-sinal")
    thread.start()
    thread.join(5)
    assert results == ["sem-sinal"]
    assert threading.current_thread() is threading.main_thread()


def test_run_task_rejects_non_positive_total_timeout(tmp_path):
    result, code = cli.run_task(feature_args(cwd=str(tmp_path), total_timeout=0.0), "task")
    assert code == 1 and result["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# cli.doctor: memória indisponível e versão da CLI
# ---------------------------------------------------------------------------


def test_doctor_reports_unavailable_memory_and_cli_version(tmp_path, monkeypatch):
    def boom():
        raise base.AdmissionError("MemAvailable ilegível")

    monkeypatch.setattr(cli.base, "_read_mem_available_bytes", boom)
    fake_cli = tmp_path / "zcode-cli"
    fake_cli.write_text("#!/bin/sh\necho 'zcode-cli 9.9.9'\n", encoding="ascii")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(cli.base, "CLI", str(fake_cli))
    report = cli.doctor()
    assert report["memory"] == "indisponível"
    assert report["cli"]["version"] == "9.9.9"


def test_doctor_handles_unexecutable_cli(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked-cli"
    blocked.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    blocked.chmod(0o600)
    monkeypatch.setattr(cli.base, "CLI", str(blocked))
    monkeypatch.setattr(cli.base, "_read_mem_available_bytes", lambda: 1)
    report = cli.doctor()
    assert report["memory"] == "abaixo do mínimo"
    assert report["cli"]["available"] is True


# ---------------------------------------------------------------------------
# cli._dispatch_jobs: entrypoint injetado, max_output_bytes e resultado inválido
# ---------------------------------------------------------------------------


def test_dispatch_jobs_uses_injected_entrypoint_and_keeps_output_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "start_job", lambda *a: {"id": "a" * 32})
    cli.configure(base)
    try:
        args = feature_args(
            cwd=str(tmp_path),
            saida=str(tmp_path / "job.out"),
            max_output_bytes=2048,
            attachments=[],
        )
        result, code = cli._dispatch_jobs(args, "task")
        assert code == 0 and result["id"] == "a" * 32
    finally:
        cli.configure(base)


def test_dispatch_jobs_rejects_non_dict_job_result(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "start_job", lambda *a: "não-dict")
    with pytest.raises(features.FeatureError, match="jobs"):
        cli._dispatch_jobs(
            feature_args(cwd=str(tmp_path), saida=str(tmp_path / "job.out")), "task"
        )


def test_configure_injects_entrypoint_and_survives_missing_jobs(monkeypatch):
    import sys as sys_module

    cli.configure(base)
    assert cli._INJECTED_ENTRYPOINT == os.path.abspath(base.__file__)
    # ImportError do pacote jobs não pode derrubar o configure.
    monkeypatch.setitem(sys_module.modules, "zcode_worker.jobs", None)
    try:
        assert cli.configure(base) is base
    finally:
        monkeypatch.undo()
        cli.configure(base)


# ---------------------------------------------------------------------------
# cli._log_internal_error e rotas do main
# ---------------------------------------------------------------------------


def test_log_internal_error_writes_private_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    try:
        raise RuntimeError("detalhe interno")
    except RuntimeError:
        cli._log_internal_error()
    log = tmp_path / "state" / "last-internal-error.log"
    assert log.exists() and stat.S_IMODE(log.stat().st_mode) == 0o600
    assert "detalhe interno" in log.read_text(encoding="utf-8")


def test_log_internal_error_swallows_unwritable_state(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("sou arquivo", encoding="utf-8")
    original = cli.base.STATE
    cli.base.STATE = str(blocker / "state")
    try:
        try:
            raise RuntimeError("x")
        except RuntimeError:
            cli._log_internal_error()  # não pode propagar
    finally:
        cli.base.STATE = original


def test_main_routes_job_child_doctor_models_and_saida(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(jobs, "run_child", lambda job_id, runner: 0)
    assert cli.main(["--_job-child", "a" * 32]) == 0
    monkeypatch.setattr(cli, "doctor", lambda: {"status": "doctor"})
    assert cli.main(["--json", "--doctor"]) == 0
    assert cli.main(["--json", "--list-models"]) == 0
    monkeypatch.setattr(cli, "_dispatch_jobs", lambda args, task: ({"id": "b" * 32}, 0))
    assert cli.main(["--json", "--cwd", str(tmp_path), "--saida", str(tmp_path / "o"), "t"]) == 0
    assert cli.main(["--json", "--list-models"], core=base) == 0
    capsys.readouterr()


def test_main_logs_internal_error_for_unexpected_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))

    def boom(_args, _task):
        raise RuntimeError("secreto interno")

    monkeypatch.setattr(cli, "run_task", boom)
    code = cli.main(["--json", "--cwd", str(tmp_path), "t"])
    assert code == 1
    captured = capsys.readouterr()
    assert "secreto interno" not in captured.out + captured.err
    assert (tmp_path / "state" / "last-internal-error.log").exists()


def test_cli_module_entrypoint_runs_as_script(tmp_path):
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-m", "zcode_worker.cli", "--json", "--list-models"],
        cwd=str(root / "src"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "models"


# ---------------------------------------------------------------------------
# features.parse_args: conflitos de controles restantes e re-raise de tipo
# ---------------------------------------------------------------------------


def test_parse_args_rejects_saida_with_dry_run():
    with pytest.raises(features.FeatureError):
        features.parse_args(["--cwd", "/tmp", "--saida", "/tmp/out", "--dry-run", "task"])


def test_parse_args_propagates_argument_type_error(monkeypatch):
    with pytest.raises(features.FeatureError):
        features.parse_args(["--cwd", "/tmp", "--model", "inexistente", "task"])


def test_decode_bounded_rejects_unencodable_surrogate():
    with pytest.raises(features.FeatureError, match="texto inválido"):
        features._decode_bounded("\ud800")


def test_read_task_rejects_oversized_positional_task():
    oversized = "x" * (features.TASK_MAX_BYTES + 1)
    with pytest.raises(features.FeatureError, match="limite seguro"):
        features.read_task(feature_args(tarefa=oversized))


def test_validate_attachments_survives_vanishing_file(tmp_path, monkeypatch):
    target = tmp_path / "vanish.txt"
    target.write_text("ok", encoding="utf-8")
    real_stat = pathlib.Path.stat

    def fake_stat(self, *args, follow_symlinks=True, **kwargs):
        # Em 3.10 Path.lstat() chama stat(follow_symlinks=False); só a
        # chamada de tamanho (follow_symlinks=True) deve falhar aqui.
        if follow_symlinks and self.name == "vanish.txt":
            raise OSError("sumiu entre lstat e stat")
        return real_stat(self, *args, follow_symlinks=follow_symlinks, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", fake_stat)
    with pytest.raises(features.FeatureError, match="ilegível"):
        features.validate_attachments([str(target)])


def test_temporary_provider_rejects_loose_tmp_directory(tmp_path, monkeypatch):
    write_provider(tmp_path / "provider.json")
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    # state/tmp não é reparável (repair_mode só cobre o próprio state)
    (state / "tmp").mkdir(mode=0o755)
    with pytest.raises(features.FeatureError, match="não é privado"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state):
            pass


def test_decode_response_rejects_non_string_input():
    with pytest.raises(features.FeatureError, match="JSON"):
        features.decode_response(None)


def test_build_cli_argv_rejects_invalid_resume(tmp_path):
    with pytest.raises(features.FeatureError, match="sessão"):
        features.build_cli_argv("task", str(tmp_path), "plan", "glm-5.3-flash", "high", resume="invalido")


# ---------------------------------------------------------------------------
# features.temporary_provider: falhas de infraestrutura do estado privado
# ---------------------------------------------------------------------------


def test_temporary_provider_provider_body_errors_clean_up(tmp_path, monkeypatch):
    write_provider(tmp_path / "provider.json")
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    state = tmp_path / "state"
    created = []
    with pytest.raises(features.FeatureError, match="própria"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state) as path:
            created.append(path)
            raise features.FeatureError("falha própria do corpo", "invalid_request")
    assert created and not os.path.exists(created[0])
    with pytest.raises(features.FeatureError, match="provider"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state) as path:
            raise OSError("erro de infra no corpo")
    assert not os.path.exists(path)


def test_temporary_provider_rejects_unusable_state_directories(tmp_path, monkeypatch):
    write_provider(tmp_path / "provider.json")
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "tmp").mkdir(mode=0o700)
    with pytest.raises(features.FeatureError, match="ilegível"):
        # componente de caminho longo demais: lstat devolve OSError (não ENOENT)
        long_state = state / ("g" * 300)
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=long_state):
            pass
    with pytest.raises(features.FeatureError, match="não é privado"):
        regular = tmp_path / "regular-file"
        regular.write_text("x", encoding="utf-8")
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=regular):
            pass


def test_temporary_provider_treats_lost_mkdir_race_as_unreadable(tmp_path, monkeypatch):
    write_provider(tmp_path / "provider.json")
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    real_mkdir = pathlib.Path.mkdir

    def mkdir_created_then_claims_exists(self, *args, **kwargs):
        real_mkdir(self, *args, **kwargs)
        raise FileExistsError()

    monkeypatch.setattr(pathlib.Path, "mkdir", mkdir_created_then_claims_exists)
    with pytest.raises(features.FeatureError, match="ilegível"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state):
            pass


def test_temporary_provider_rejects_readonly_parent(tmp_path, monkeypatch):
    write_provider(tmp_path / "provider.json")
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        with pytest.raises(features.FeatureError, match="ilegível"):
            with features.temporary_provider(
                "glm-5.3-flash", "high", state_dir=readonly / "state"
            ):
                pass
    finally:
        readonly.chmod(0o755)


def test_temporary_provider_rejects_state_mode_repair_failure(tmp_path, monkeypatch):
    write_provider(tmp_path / "provider.json")
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    monkeypatch.setattr(features.os, "chmod", lambda *_a, **_k: (_ for _ in ()).throw(OSError("sem permissão")))
    with pytest.raises(features.FeatureError, match="não é privado"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state):
            pass


def test_temporary_provider_rejects_mkstemp_failure(tmp_path, monkeypatch):
    write_provider(tmp_path / "provider.json")
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    state = tmp_path / "state"
    monkeypatch.setattr(
        features.tempfile,
        "mkstemp",
        lambda **_k: (_ for _ in ()).throw(OSError("sem espaço para temporários")),
    )
    with pytest.raises(features.FeatureError, match="provider temporário"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state):
            pass


def test_features_configure_snapshots_core_constants():
    assert features.configure(base) is base
    assert features.PROVIDER_ID == base.PROVIDER_ID
    assert features.MODELS == base.MODELS
    assert features.EFFORTS == base.EFFORTS
    assert features.PROVIDER_MODELS == base.PROVIDER_MODELS
    assert features.TASK_MAX_BYTES == base.TASK_MAX_BYTES


# ---------------------------------------------------------------------------
# jobs: diretórios e arquivos privados
# ---------------------------------------------------------------------------


def test_private_dir_survives_mkdir_race_and_rejects_foreign_inputs(isolated_state, tmp_path, monkeypatch):
    state = isolated_state
    state.mkdir(mode=0o700, parents=True)
    real_mkdir = os.mkdir

    def mkdir_then_exists(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        raise FileExistsError()

    if (state / "jobs").exists():
        (state / "jobs").rmdir()
    monkeypatch.setattr(jobs.os, "mkdir", mkdir_then_exists)
    assert jobs._jobs_dir(create=True).endswith("jobs")
    monkeypatch.setattr(jobs.os, "mkdir", real_mkdir)

    def mkdir_denied(path, *args, **kwargs):
        raise PermissionError("somente leitura")

    fresh = tmp_path / "fresh-state"
    monkeypatch.setattr(base, "STATE", str(fresh))
    monkeypatch.setattr(jobs._CORE, "STATE", str(fresh))
    monkeypatch.setattr(jobs.os, "mkdir", mkdir_denied)
    with pytest.raises(base.WorkerError, match="preparar o estado privado"):
        jobs._jobs_dir(create=True)
    monkeypatch.setattr(jobs.os, "mkdir", real_mkdir)
    monkeypatch.setattr(base, "STATE", str(state))
    monkeypatch.setattr(jobs._CORE, "STATE", str(state))

    long_state = tmp_path / ("h" * 300)
    with pytest.raises(base.WorkerError, match="ilegível"):
        jobs._private_dir(str(long_state), create=True)


def test_private_dir_rejects_non_directory_descriptor(isolated_state, tmp_path, monkeypatch):
    state = isolated_state
    state.mkdir(mode=0o700, parents=True)
    regular = tmp_path / "regular.txt"
    regular.write_text("x", encoding="utf-8")
    real_open = os.open
    target = os.path.join(str(state), "jobs")

    def fake_open(path, flags, *args, **kwargs):
        if str(path) == target:
            return real_open(str(regular), os.O_RDONLY)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "open", fake_open)
    with pytest.raises(base.WorkerError, match="não é privado"):
        jobs._jobs_dir(create=True)


def test_private_dir_rejects_chmod_repair_failure(isolated_state, monkeypatch):
    state = isolated_state
    state.mkdir(mode=0o700, parents=True)
    (state / "jobs").mkdir(mode=0o755)
    monkeypatch.setattr(jobs.os, "fchmod", lambda *_a: (_ for _ in ()).throw(OSError("read-only fs")))
    with pytest.raises(base.WorkerError, match="não é privado"):
        jobs._jobs_dir(create=False)


def test_jobs_dir_remaps_inner_worker_errors(isolated_state, monkeypatch):
    state = isolated_state
    state.mkdir(mode=0o700, parents=True)
    real_lock = jobs._private_dir

    def fake_dir(path, *, create):
        if str(path) == str(state):
            real_lock(path, create=create)
            return
        raise base.WorkerError("não foi possível preparar o estado privado")

    monkeypatch.setattr(jobs, "_private_dir", fake_dir)
    with pytest.raises(base.WorkerError, match="estado de jobs ilegível"):
        jobs._jobs_dir(create=True)

    def fake_dir2(path, *, create):
        if str(path) == str(state):
            real_lock(path, create=create)
            return
        raise base.WorkerError("estado local não é privado")

    monkeypatch.setattr(jobs, "_private_dir", fake_dir2)
    with pytest.raises(base.WorkerError, match="estado de jobs não é privado"):
        jobs._jobs_dir(create=True)
    monkeypatch.setattr(jobs, "_private_dir", real_lock)


def test_job_dir_rejects_duplicate_foreign_and_unprivate(isolated_state, monkeypatch):
    state = isolated_state
    job_id = "1" * 32
    directory = Path(jobs._job_dir(job_id, create=True))
    with pytest.raises(base.WorkerError, match="job já existe"):
        jobs._job_dir(job_id, create=True)
    directory.chmod(0o755)
    with pytest.raises(base.WorkerError, match="não é privado"):
        jobs._job_dir(job_id, create=False)
    directory.chmod(0o700)

    other = "2" * 32
    real_mkdir = os.mkdir

    def mkdir_denied(path, *args, **kwargs):
        raise PermissionError("negado")

    monkeypatch.setattr(jobs.os, "mkdir", mkdir_denied)
    with pytest.raises(base.WorkerError, match="job não encontrado"):
        jobs._job_dir(other, create=True)
    monkeypatch.setattr(jobs.os, "mkdir", real_mkdir)
    real_lstat = os.lstat
    target = os.path.join(str(state), "jobs", other)

    def lstat_boom(path, *args, **kwargs):
        if str(path) == target:
            raise OSError("falha de leitura")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "lstat", lstat_boom)
    with pytest.raises(base.WorkerError, match="job ilegível"):
        jobs._job_dir(other, create=True)


def test_private_file_truncate_mode_and_non_regular_rejection(tmp_path):
    target = tmp_path / "truncatable.json"
    target.write_text("conteúdo antigo", encoding="utf-8")
    target.chmod(0o600)
    fd = jobs._private_file(str(target), truncate=True)
    with os.fdopen(fd, "wb") as handle:
        handle.write(b"novo")
    assert target.read_bytes() == b"novo"
    with pytest.raises(base.WorkerError, match="regular"):
        jobs._private_file(str(tmp_path), create=False)


def test_read_private_json_rejects_oversize_and_read_failure(tmp_path, monkeypatch):
    big = tmp_path / "big.json"
    big.write_text("x" * 11, encoding="utf-8")
    big.chmod(0o600)
    with pytest.raises(base.WorkerError, match="limite"):
        jobs._read_private_json(str(big), limit=10)
    real_fdopen = os.fdopen

    class FailingReader:
        def __init__(self, inner):
            self.inner = inner

        def read(self, *_args):
            raise OSError("io sumiu")

        def close(self):
            self.inner.close()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    def fake_fdopen(fd, *args, **kwargs):
        inner = real_fdopen(fd, *args, **kwargs)
        return FailingReader(inner)

    small = tmp_path / "small.json"
    small.write_text("{}", encoding="utf-8")
    small.chmod(0o600)
    monkeypatch.setattr(jobs.os, "fdopen", fake_fdopen)
    with pytest.raises(base.WorkerError, match="ilegível"):
        jobs._read_private_json(str(small), limit=100)


def test_atomic_write_rejects_oversize_and_hostile_swaps(tmp_path, monkeypatch):
    target = tmp_path / "meta.json"
    with pytest.raises(base.WorkerError, match="limite"):
        jobs._atomic_write(str(target), {"blob": "y" * 100}, limit=16)
    real_lstat = os.lstat
    calls = []

    def lstat_vanishing_then_symlink(path, *args, **kwargs):
        if os.path.abspath(str(path)) == str(target):
            calls.append(1)
            if len(calls) == 1:
                raise FileNotFoundError()
            os.symlink(str(tmp_path / "other.json"), str(target))
            return real_lstat(str(target), *args, **kwargs)
        return real_lstat(path, *args, **kwargs)

    (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(jobs.os, "lstat", lstat_vanishing_then_symlink)
    with pytest.raises(base.WorkerError, match="persistir"):
        jobs._atomic_write(str(target), {"ok": True}, limit=1024)

    monkeypatch.setattr(jobs.os, "lstat", real_lstat)
    target2 = tmp_path / "meta2.json"

    def lstat_always_missing(path, *args, **kwargs):
        if os.path.abspath(str(path)) == str(target2):
            raise FileNotFoundError()
        return real_lstat(path, *args, **kwargs)

    def replace_boom(src, dst):
        raise OSError("replace negado")

    monkeypatch.setattr(jobs.os, "lstat", lstat_always_missing)
    monkeypatch.setattr(jobs.os, "replace", replace_boom)
    with pytest.raises(base.WorkerError, match="persistir"):
        jobs._atomic_write(str(target2), {"ok": True}, limit=1024)

    monkeypatch.setattr(jobs.os, "replace", os.replace)

    def fchmod_boom(fd, *_args):
        raise OSError("fchmod negado")

    monkeypatch.setattr(jobs.os, "fchmod", fchmod_boom)
    with pytest.raises(base.WorkerError, match="persistir"):
        jobs._atomic_write(str(tmp_path / "meta3.json"), {"ok": True}, limit=1024)


def test_job_lock_rejects_flock_failure(isolated_state, monkeypatch):
    job_id = "3" * 32
    Path(jobs._job_dir(job_id, create=True))

    def flock_boom(_fd, _op):
        raise OSError("flock indisponível")

    monkeypatch.setattr(fcntl, "flock", flock_boom)
    with pytest.raises(base.WorkerError, match="proteger"):
        with jobs._job_lock(job_id):
            pass


# ---------------------------------------------------------------------------
# jobs: validações de opções, entrypoint e proc
# ---------------------------------------------------------------------------


def test_validate_options_rejects_bad_text_cwd_and_bulk(isolated_state, tmp_path):
    with pytest.raises(base.WorkerError, match="texto"):
        jobs._validate_options({"model": None})
    with pytest.raises(base.WorkerError, match="cwd"):
        jobs._validate_options({"cwd": "relativo"})
    with pytest.raises(base.WorkerError, match="cwd"):
        jobs._validate_options({"cwd": str(tmp_path / "nao-existe")})
    with pytest.raises(base.WorkerError, match="limite seguro"):
        jobs._validate_options({"attach": ["x" * 4096] * 32})


def test_validate_entrypoint_rejects_shell_script_paths_and_missing(tmp_path):
    shell_copy = tmp_path / "bash"
    shell_copy.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    shell_copy.chmod(0o755)
    with pytest.raises(base.WorkerError, match="shell"):
        jobs._validate_entrypoint([str(shell_copy), "x"])
    python_bin = str(Path(sys.executable).resolve())
    with pytest.raises(base.WorkerError, match="absoluto"):
        jobs._validate_entrypoint([python_bin, "script.py"])
    with pytest.raises(base.WorkerError, match="script Python inexistente"):
        jobs._validate_entrypoint([python_bin, str(tmp_path / "missing.py")])


def test_create_output_identity_fallback_on_fstat_failure(tmp_path, monkeypatch):
    target = tmp_path / "out.jsonl"
    real_fstat = os.fstat
    fired = []

    def fstat_boom(fd, *args, **kwargs):
        if not fired:
            fired.append(fd)
            raise OSError("fstat falhou")
        return real_fstat(fd, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "fstat", fstat_boom)
    with pytest.raises(base.WorkerError, match="saída"):
        jobs._create_output(str(target))
    assert not target.exists()


def test_proc_details_rejects_relative_root_and_short_stat(tmp_path):
    real_root = jobs._PROC_ROOT
    jobs._PROC_ROOT = "relativo"
    try:
        assert jobs._proc_details(123) is None
    finally:
        jobs._PROC_ROOT = real_root
    proc = tmp_path / "proc"
    pid_dir = proc / "321"
    pid_dir.mkdir(parents=True)
    (pid_dir / "stat").write_text("321 (curto) S 1\n", encoding="ascii")
    jobs._PROC_ROOT = str(proc)
    try:
        assert jobs._proc_details(321) is None
        assert jobs._proc_has_identity(321, os.getuid(), 1, "3" * 32) is False
    finally:
        jobs._PROC_ROOT = real_root


def test_metadata_valid_rejects_non_dict_and_bad_identity(tmp_path):
    with pytest.raises(base.WorkerError, match="metadata"):
        jobs._metadata_valid("não-dict", "a" * 32)
    base_meta = {
        "id": "a" * 32,
        "status": "queued",
        "createdAt": 1.0,
        "pid": None,
        "uid": os.getuid(),
        "starttime": None,
        "output": str(tmp_path / "out"),
        "exitCode": None,
        "outputDev": 1,
        "outputIno": 2,
    }
    with pytest.raises(base.WorkerError, match="metadata"):
        jobs._metadata_valid({**base_meta, "uid": -1}, "a" * 32)
    with pytest.raises(base.WorkerError, match="metadata"):
        jobs._metadata_valid({**base_meta, "starttime": -1}, "a" * 32)


def test_load_input_rejects_foreign_shape(isolated_state, tmp_path):
    directory = Path(jobs._job_dir("4" * 32, create=True))
    jobs._atomic_write(jobs._input_path(str(directory)), {"surpresa": 1}, limit=1024)
    with pytest.raises(base.WorkerError, match="input privado"):
        jobs._load_input(str(directory))


def test_remove_input_ignores_missing_and_foreign(isolated_state):
    directory = Path(jobs._job_dir("5" * 32, create=True))
    jobs._remove_input(str(directory))
    assert not (directory / "input.json").exists()


def test_start_job_rejects_output_inside_job_directory(isolated_state, tmp_path, monkeypatch):
    fixed = uuid_module.UUID(int=0xC0FFEE)
    monkeypatch.setattr(jobs.uuid, "uuid4", lambda: fixed)
    helper = tmp_path / "helper.py"
    helper.write_text("pass\n", encoding="utf-8")
    helper.chmod(0o700)
    entrypoint = [str(Path(sys.executable).resolve()), str(helper)]
    inside = Path(jobs._jobs_dir(create=True)) / fixed.hex / "result.jsonl"
    with pytest.raises(base.WorkerError, match="externo ao job"):
        jobs.start_job({}, "task", str(inside), entrypoint)


# ---------------------------------------------------------------------------
# jobs: leitura de status, cancelamento e escrita de resultado
# ---------------------------------------------------------------------------


def prepare_direct_job(state: Path, tmp_path: Path, job_id: str, *, status="queued", cancel=False, pid=None):
    directory = Path(jobs._job_dir(job_id, create=True))
    output = tmp_path / f"{job_id}.out"
    fd = jobs._create_output(str(output))
    identity = os.fstat(fd)
    os.close(fd)
    metadata = {
        "id": job_id,
        "status": status,
        "createdAt": 1.0,
        "pid": os.getpid() if pid is None else pid,
        "uid": os.getuid(),
        "starttime": 123,
        "output": str(output),
        "exitCode": 0 if status in jobs._TERMINAL else None,
        "outputDev": identity.st_dev,
        "outputIno": identity.st_ino,
    }
    if cancel:
        metadata["cancelRequestedAt"] = time.time()
    jobs._atomic_write(jobs._metadata_path(str(directory)), metadata, limit=64 * 1024)
    if status not in jobs._TERMINAL:
        jobs._atomic_write(jobs._input_path(str(directory)), {"options": {}, "task": "task"}, limit=200000)
    return directory, output, metadata


def test_read_status_keeps_queued_without_pid_and_running_in_grace(isolated_state, tmp_path, monkeypatch):
    queued_id = "6" * 32
    directory, _output, _meta = prepare_direct_job(isolated_state, tmp_path, queued_id, pid=None)
    jobs._atomic_write(
        jobs._metadata_path(str(directory)),
        {**_meta, "pid": None},
        limit=64 * 1024,
    )
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: False)
    assert jobs.read_status(queued_id)["status"] == "queued"

    grace_id = "7" * 32
    _directory, _output, _meta2 = prepare_direct_job(
        isolated_state, tmp_path, grace_id, status="running", cancel=True, pid=999999
    )
    assert jobs.read_status(grace_id)["status"] == "running"


def test_cancel_job_pidfd_paths_cover_error_and_identity_recheck(isolated_state, tmp_path, monkeypatch):
    running_id = "8" * 32
    _directory, _output, _metadata = prepare_direct_job(isolated_state, tmp_path, running_id, status="running")
    real_open = os.open
    holder = {}

    def fake_pidfd_open(_pid, _flags):
        holder["fd"] = real_open("/dev/null", os.O_RDONLY)
        return holder["fd"]

    sent = []
    checks = []

    def identity_once(pid, uid, starttime, job_id):
        checks.append(1)
        return len(checks) == 1

    monkeypatch.setattr(jobs.os, "pidfd_open", fake_pidfd_open)
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", lambda *args: sent.append(args))
    monkeypatch.setattr(jobs, "_proc_has_identity", identity_once)
    result = jobs.cancel_job(running_id)
    assert result["status"] == "running" and not sent
    with pytest.raises(OSError):
        os.fstat(holder["fd"])

    denied_id = "9" * 32
    _directory, _output, _metadata = prepare_direct_job(isolated_state, tmp_path, denied_id, status="running")
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)

    def pidfd_open_denied(_pid, _flags):
        raise PermissionError("pidfd negado")

    def send_boom(*_args):
        raise OSError("envio falhou")

    monkeypatch.setattr(jobs.os, "pidfd_open", pidfd_open_denied)
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", send_boom)
    denied = jobs.cancel_job(denied_id)
    assert denied["status"] == "running" and denied["cancelRequestedAt"] > 0


def test_write_result_rejects_overflow_and_closes_descriptor(isolated_state, tmp_path):
    output = tmp_path / "limited"
    fd = jobs._create_output(str(output))
    identity = os.fstat(fd)
    os.close(fd)
    expected = (identity.st_dev, identity.st_ino)
    with pytest.raises(base.WorkerError, match="limite seguro"):
        jobs._write_result(str(output), {"blob": "z" * 200}, 0, 128, expected)
    assert output.read_bytes() == b""


def test_run_callback_restores_preexisting_real_timer():
    signal.setitimer(signal.ITIMER_REAL, 30.0)
    try:
        result = jobs._run_callback(lambda *_: ({"ok": True}, 0), {"timeout": 0.05}, "task")
    finally:
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
        signal.setitimer(signal.ITIMER_REAL, 0.0)
    assert result == ({"ok": True}, 0)


# ---------------------------------------------------------------------------
# jobs.run_child: código 124, falha de escrita e Terminated no lock
# ---------------------------------------------------------------------------


def make_entrypoint(tmp_path: Path, body_extra: str = "") -> list:
    script = tmp_path / "runner-child.py"
    script.write_text(
        (
            "import sys\n"
            "from zcode_worker import jobs\n"
            f"{body_extra}\n"
            "def runner(options, task):\n"
            "    if task == 'exit124':\n"
            "        return {'status': 'ok'}, 124\n"
            "    if task == 'writefail':\n"
            "        return {'status': 'ok'}, 0\n"
            "    return {'status': 'ok'}, 0\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(jobs.run_child(sys.argv[-1], runner))\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)
    return [str(Path(sys.executable).resolve()), str(script)]


def test_run_child_marks_exit_124_as_timed_out(isolated_state, tmp_path):
    monkeypatch_proc = "/proc"
    original = jobs._PROC_ROOT
    jobs._PROC_ROOT = monkeypatch_proc
    try:
        entrypoint = make_entrypoint(tmp_path)
        started = jobs.start_job({}, "exit124", str(tmp_path / "t124.out"), entrypoint)
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = jobs.read_status(started["id"])
            if status["status"] in jobs._TERMINAL:
                break
            time.sleep(0.01)
        assert status["status"] == "timed_out" and status["exitCode"] == 124
    finally:
        jobs._PROC_ROOT = original


def test_run_child_write_failure_fails_safe(isolated_state, tmp_path):
    original = jobs._PROC_ROOT
    jobs._PROC_ROOT = "/proc"
    try:
        entrypoint = make_entrypoint(
            tmp_path,
            "import zcode_worker.jobs as _jobs\n"
            "def _boom(*_a, **_k):\n"
            "    raise OSError('escrita quebrada')\n"
            "_jobs._write_result = _boom\n",
        )
        started = jobs.start_job({}, "writefail", str(tmp_path / "wfail.out"), entrypoint)
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = jobs.read_status(started["id"])
            if status["status"] in jobs._TERMINAL:
                break
            time.sleep(0.01)
        assert status["status"] == "failed" and status["exitCode"] == 1
        output = Path(status["output"]).read_text(encoding="utf-8")
        assert "escrita quebrada" not in output
    finally:
        jobs._PROC_ROOT = original


def test_run_child_rejects_foreign_identity_and_stdout(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)

    foreign = "a" * 32
    prepare_direct_job(isolated_state, tmp_path, foreign, pid=os.getpid() + 1)
    assert jobs.run_child(foreign, lambda *_: ({"ok": True}, 0)) == 1
    assert jobs.read_status(foreign)["status"] == "failed"

    stdout_id = "b" * 32
    prepare_direct_job(isolated_state, tmp_path, stdout_id, pid=os.getpid())
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: False)
    assert jobs.run_child(stdout_id, lambda *_: ({"ok": True}, 0)) == 1
    assert jobs.read_status(stdout_id)["status"] == "failed"


def test_run_child_terminated_while_waiting_for_lock(isolated_state, tmp_path, monkeypatch):
    job_id = "c" * 32
    prepare_direct_job(isolated_state, tmp_path, job_id)
    real_lock = jobs._job_lock
    armed = {"active": True}

    @contextlib.contextmanager
    def lock_that_signals_first(job, *, create=False):
        if armed["active"]:
            armed["active"] = False
            signal.raise_signal(signal.SIGTERM)
        with real_lock(job, create=create) as directory:
            yield directory

    monkeypatch.setattr(jobs, "_job_lock", lock_that_signals_first)
    code = jobs.run_child(job_id, lambda *_: pytest.fail("callback não deve rodar"))
    assert code == 128 + signal.SIGTERM
    assert jobs.read_status(job_id)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Rodada final: branches residuais de cli e jobs
# ---------------------------------------------------------------------------


def test_verify_resume_accepts_matching_session(tmp_path):
    db = tmp_path / "resume.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("create table session(id text, directory text)")
    connection.execute("insert into session values (?, ?)", ("sess_ok", str(tmp_path)))
    connection.execute(
        "create table session_entry(session_id text, type text, time_created int, data text)"
    )
    connection.execute(
        "insert into session_entry values (?, ?, ?, ?)",
        (
            "sess_ok",
            "runtime/model_selection",
            1,
            json.dumps(
                {
                    "modelSelection": {
                        "providerId": base.PROVIDER_ID,
                        "modelId": "GLM-5.3-Flash",
                        "options": {"reasoningLevel": "high"},
                    }
                }
            ),
        ),
    )
    connection.commit()
    connection.close()
    args = feature_args(resume="sess_ok", cwd=str(tmp_path), model="glm-5.3-flash", effort="high")
    assert cli._verify_resume(args, db_path=db) is None


def test_doctor_version_without_match_and_failing_cli(tmp_path, monkeypatch):
    odd = tmp_path / "odd-cli"
    odd.write_text("#!/bin/sh\necho 'sem versao conhecida aqui'\n", encoding="ascii")
    odd.chmod(0o755)
    monkeypatch.setattr(cli.base, "CLI", str(odd))
    monkeypatch.setattr(cli.base, "_read_mem_available_bytes", lambda: 1)
    assert cli.doctor()["cli"]["version"] == "indisponível"
    monkeypatch.setattr(cli.base, "CLI", "/bin/false")
    assert cli.doctor()["cli"]["version"] == "indisponível"


def test_configure_accepts_foreign_and_missing_core_files(monkeypatch):
    fake = SimpleNamespace(
        PROVIDER_ID=base.PROVIDER_ID,
        MODELS=base.MODELS,
        EFFORTS=base.EFFORTS,
        PROVIDER_MODELS=base.PROVIDER_MODELS,
        TASK_MAX_BYTES=base.TASK_MAX_BYTES,
        STATE="/tmp/state-fake",
        __file__="relativo.py",
    )
    assert cli.configure(fake) is fake
    fake.__file__ = "/caminho/absoluto/inexistente.py"
    assert cli.configure(fake) is fake
    cli.configure(base)


def test_configure_skips_uncallable_jobs_configure(monkeypatch):
    monkeypatch.setattr(jobs, "configure", None)
    assert cli.configure(base) is base


def test_configure_swallows_jobs_import_error(monkeypatch):
    import zcode_worker as package

    monkeypatch.delattr(package, "jobs")
    monkeypatch.setitem(__import__("sys").modules, "zcode_worker.jobs", None)
    assert cli.configure(base) is base


def test_main_reports_worker_error_without_logging(tmp_path, monkeypatch, capsys):
    def boom(_args, _task):
        raise base.WorkerError("erro seguro do worker")

    monkeypatch.setattr(cli, "run_task", boom)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    code = cli.main(["--json", "--cwd", str(tmp_path), "t"])
    assert code == 1
    assert not (tmp_path / "state" / "last-internal-error.log").exists()
    capsys.readouterr()


def test_cli_main_guard_executes_inprocess(monkeypatch):
    import runpy
    import warnings

    monkeypatch.setattr(sys, "argv", ["zcode-worker-cli", "--json", "--list-models"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("zcode_worker.cli", run_name="__main__", alter_sys=False)
    assert excinfo.value.code == 0


def test_validate_options_accepts_valid_output_limit(tmp_path):
    options = jobs._validate_options({"cwd": str(tmp_path), "max_output_bytes": 4096})
    assert options["max_output_bytes"] == 4096


def test_atomic_write_rejects_lstat_permission_errors(tmp_path, monkeypatch):
    target = tmp_path / "perm.json"
    real_lstat = os.lstat
    calls = []

    def lstat_denied(path, *args, **kwargs):
        if os.path.abspath(str(path)) == str(target):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError("negado")
            return real_lstat(path, *args, **kwargs)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "lstat", lstat_denied)
    with pytest.raises(base.WorkerError, match="persistir"):
        jobs._atomic_write(str(target), {"ok": True}, limit=1024)

    target2 = tmp_path / "perm2.json"
    calls2 = []

    def lstat_missing_then_denied(path, *args, **kwargs):
        if os.path.abspath(str(path)) == str(target2):
            calls2.append(1)
            if len(calls2) == 1:
                raise FileNotFoundError()
            raise PermissionError("negado na segunda leitura")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "lstat", lstat_missing_then_denied)
    with pytest.raises(base.WorkerError, match="persistir"):
        jobs._atomic_write(str(target2), {"ok": True}, limit=1024)


def test_proc_identity_without_cmdline_fails_closed(tmp_path):
    proc = tmp_path / "proc"
    pid_dir = proc / "322"
    pid_dir.mkdir(parents=True)
    (pid_dir / "stat").write_text(
        "322 (worker) S 1 322 322 0 -1 4194560 0 0 0 0 0 0 0 0 0 0 0 0 777 0\n",
        encoding="ascii",
    )
    original = jobs._PROC_ROOT
    jobs._PROC_ROOT = str(proc)
    try:
        assert jobs._proc_has_identity(322, os.getuid(), 777, "3" * 32) is False
    finally:
        jobs._PROC_ROOT = original


def test_private_and_output_cleanup_skip_divergent_identity(tmp_path, monkeypatch):
    # _private_file (create) e _create_output: fstat apontando para outro
    # arquivo deixa o resíduo no disco em vez de apagar arquivo estranho.
    foreign_dir = tmp_path / "foreign-dir"
    foreign_dir.mkdir()
    real_fstat = os.fstat
    foreign_fd = os.open(str(foreign_dir), os.O_RDONLY)
    armed = []

    def fstat_foreign(fd, *args, **kwargs):
        if not armed:
            armed.append(fd)
            return real_fstat(foreign_fd)
        return real_fstat(fd, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "fstat", fstat_foreign)
    leftover = tmp_path / "leftover.json"
    with pytest.raises(base.WorkerError, match="não é regular"):
        jobs._private_file(str(leftover), create=True)
    assert leftover.exists()
    leftover.unlink()

    armed2 = []

    def fstat_foreign2(fd, *args, **kwargs):
        if not armed2:
            armed2.append(fd)
            return real_fstat(foreign_fd)
        return real_fstat(fd, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "fstat", fstat_foreign2)
    leftover2 = tmp_path / "leftover2.out"
    fd2 = jobs._create_output(str(leftover2))
    os.close(fd2)
    assert leftover2.exists()
    leftover2.unlink()
    os.close(foreign_fd)


def test_start_job_cleanup_skips_replaced_output(isolated_state, tmp_path, monkeypatch):
    entrypoint = make_entrypoint(tmp_path)
    output = tmp_path / "replaced.out"

    def popen_that_replaces_output(*args, **kwargs):
        with contextlib.suppress(OSError):
            os.unlink(output)
        output.write_text("novo conteúdo de outro dono", encoding="utf-8")
        raise OSError("spawn negado")

    monkeypatch.setattr(jobs.subprocess, "Popen", popen_that_replaces_output)
    with pytest.raises(base.WorkerError, match="iniciar o job"):
        jobs.start_job({}, "task", str(output), entrypoint)
    assert output.read_text(encoding="utf-8") == "novo conteúdo de outro dono"


def test_start_job_race_with_fast_terminal_child(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_PROC_ROOT", "/proc")
    entrypoint = make_entrypoint(tmp_path)
    real_load = jobs._load_metadata

    def load_already_failed(directory, job_id):
        metadata = real_load(directory, job_id)
        # Simula o filho que gravou estado terminal antes da leitura do pai.
        return dict(metadata, status="failed", exitCode=1)

    monkeypatch.setattr(jobs, "_load_metadata", load_already_failed)
    started = jobs.start_job({}, "ok", str(tmp_path / "race.out"), entrypoint)
    assert started["id"] and started["status"] == "failed"


def test_cancel_job_without_pidfd_support(isolated_state, tmp_path, monkeypatch):
    running_id = "9" * 32
    prepare_direct_job(isolated_state, tmp_path, running_id, status="running")
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)
    monkeypatch.delattr(jobs.os, "pidfd_open", raising=False)
    result = jobs.cancel_job(running_id)
    assert result["status"] == "running" and result["cancelRequestedAt"] > 0


def test_cancel_job_send_signal_failure_is_tolerated(isolated_state, tmp_path, monkeypatch):
    running_id = "a" * 32
    _directory, _output, _metadata = prepare_direct_job(isolated_state, tmp_path, running_id, status="running")
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)
    real_open = os.open
    holder = {}

    def fake_pidfd_open(_pid, _flags):
        holder["fd"] = real_open("/dev/null", os.O_RDONLY)
        return holder["fd"]

    def send_boom(*_args):
        raise OSError("envio bloqueado")

    monkeypatch.setattr(jobs.os, "pidfd_open", fake_pidfd_open)
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", send_boom)
    result = jobs.cancel_job(running_id)
    assert result["status"] == "running"


def armed_child_state(isolated_state, tmp_path, job_id, *, status="queued", input_mode=None):
    directory = Path(jobs._job_dir(job_id, create=True))
    output = tmp_path / f"{job_id}.armed"
    fd = jobs._create_output(str(output))
    identity = os.fstat(fd)
    os.close(fd)
    metadata = {
        "id": job_id,
        "status": status,
        "createdAt": 1.0,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "starttime": 123,
        "output": str(output),
        "exitCode": 0 if status in jobs._TERMINAL else None,
        "outputDev": identity.st_dev,
        "outputIno": identity.st_ino,
    }
    jobs._atomic_write(jobs._metadata_path(str(directory)), metadata, limit=64 * 1024)
    input_path = directory / "input.json"
    if input_mode == "symlink":
        carrier = tmp_path / f"{job_id}.carrier"
        carrier.write_text("{}", encoding="utf-8")
        input_path.symlink_to(carrier)
    elif status not in jobs._TERMINAL:
        jobs._atomic_write(str(input_path), {"options": {}, "task": "task"}, limit=200000)
    return directory, output


def run_child_inprocess(monkeypatch, job_id, callback, *, invalid_input=False):
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    return jobs.run_child(job_id, callback)


def test_run_child_inprocess_exit_124_marks_timed_out(isolated_state, tmp_path, monkeypatch):
    job_id = "b" * 32
    _directory, _output = armed_child_state(isolated_state, tmp_path, job_id)
    code = run_child_inprocess(monkeypatch, job_id, lambda _o, _t: ({"status": "ok"}, 124))
    assert code == 124
    assert jobs.read_status(job_id)["status"] == "timed_out"


def test_run_child_inprocess_write_failure_fails_safe(isolated_state, tmp_path, monkeypatch):
    job_id = "c" * 32
    _directory, output = armed_child_state(isolated_state, tmp_path, job_id)

    def boom(*_a, **_k):
        raise OSError("escrita quebrada")

    monkeypatch.setattr(jobs, "_write_result", boom)
    code = run_child_inprocess(monkeypatch, job_id, lambda _o, _t: ({"status": "ok"}, 0))
    assert code == 1
    assert jobs.read_status(job_id)["status"] == "failed"
    assert "escrita quebrada" not in output.read_text(encoding="utf-8")


def test_run_child_inprocess_invalid_input_fails_safe(isolated_state, tmp_path, monkeypatch):
    job_id = "d" * 32
    directory, output = armed_child_state(isolated_state, tmp_path, job_id)
    jobs._atomic_write(jobs._input_path(str(directory)), {"surpresa": 1}, limit=1024)
    code = run_child_inprocess(monkeypatch, job_id, lambda _o, _t: ({"status": "ok"}, 0))
    assert code == 1
    assert jobs.read_status(job_id)["status"] == "failed"
    assert "falha segura no job" in output.read_text(encoding="utf-8")


def test_run_child_inprocess_terminal_metadata_keeps_exit_code(isolated_state, tmp_path, monkeypatch):
    job_id = "e" * 32
    directory, output = armed_child_state(isolated_state, tmp_path, job_id)
    real_load = jobs._load_metadata
    calls = []

    def load_metadata(directory_arg, job_id_arg):
        metadata = real_load(directory_arg, job_id_arg)
        calls.append(1)
        if len(calls) == 3:
            metadata = dict(metadata, status="completed", exitCode=None)
        return metadata

    monkeypatch.setattr(jobs, "_load_metadata", load_metadata)
    code = run_child_inprocess(monkeypatch, job_id, lambda _o, _t: ({"status": "ok"}, 0))
    assert code == 0
    assert "exit=0" in output.read_text(encoding="utf-8")


def test_run_child_terminated_after_terminal_metadata(isolated_state, tmp_path, monkeypatch):
    job_id = "f" * 32
    armed_child_state(isolated_state, tmp_path, job_id, status="completed")
    real_lock = jobs._job_lock
    armed = {"active": True}

    @contextlib.contextmanager
    def lock_that_signals_first(job, *, create=False):
        if armed["active"]:
            armed["active"] = False
            signal.raise_signal(signal.SIGTERM)
        with real_lock(job, create=create) as directory:
            yield directory

    monkeypatch.setattr(jobs, "_job_lock", lock_that_signals_first)
    code = jobs.run_child(job_id, lambda *_: pytest.fail("callback não deve rodar"))
    assert code == 128 + signal.SIGTERM
    assert jobs.read_status(job_id)["status"] == "completed"


def test_run_child_lock_failure_fails_without_side_effects(isolated_state):
    missing = "9" * 32
    assert jobs.run_child(missing, lambda _o, _t: ({"status": "ok"}, 0)) == 1


def test_remove_input_skips_symlink_and_run_child_leaves_it(isolated_state, tmp_path, monkeypatch):
    job_id = "1" * 32
    directory, _output = armed_child_state(isolated_state, tmp_path, job_id, input_mode="symlink")
    jobs._remove_input(str(directory))
    assert (directory / "input.json").is_symlink()
    code = run_child_inprocess(monkeypatch, job_id, lambda _o, _t: ({"status": "ok"}, 0))
    assert code == 1
    assert (directory / "input.json").is_symlink()
