"""Regressões dos achados independentes da revisão.

Os casos deste arquivo usam somente fixtures locais e processos Python reais.
Não devem depender da CLI do ZCode, da conta do provedor ou de rede.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import textwrap
import time
from argparse import Namespace
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from zcode_worker import base, cli, features, jobs


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "out" / "zcode-worker-candidate"


def _cli_args(cwd: Path, **overrides) -> Namespace:
    values = {
        "tarefa": None,
        "cwd": str(cwd),
        "mode": "edit",
        "spec": None,
        "timeout": 1.0,
        "queue_timeout": 1.0,
        "total_timeout": None,
        "model": "glm-5.3-flash",
        "effort": "high",
        "profile": None,
        "resume": None,
        "attach": [],
        "disallowed_tools": [],
        "max_output_bytes": None,
        "json": True,
        "dry_run": False,
        "doctor": False,
        "list_models": False,
        "saida": None,
        "status": None,
        "cancel": None,
        "_job_child": None,
    }
    values.update(overrides)
    return Namespace(**values)


@contextlib.contextmanager
def _local_admission():
    """Mantém o caminho real de _execute sem consultar admissão global."""

    yield


def _write_provider_fixture(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "defaultModelSelection": {
                    "providerId": features.PROVIDER_ID,
                    "modelId": "GLM-5.3-Flash",
                    "options": {"reasoningLevel": "high"},
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("signum", "exit_code"),
    [
        (signal.SIGTERM, 143),
        (signal.SIGINT, 130),
        (signal.SIGHUP, 129),
    ],
)
def test_run_task_preserves_real_signal_class_and_provider_cleanup(
    tmp_path, monkeypatch, signum, exit_code
):
    provider = tmp_path / "provider.json"
    state = tmp_path / "state"
    _write_provider_fixture(provider)
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(provider))
    monkeypatch.setattr(cli.base, "STATE", str(state))
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: _local_admission())
    previous_handlers = {
        sig: signal.getsignal(sig)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    }

    def runner(*_args):
        os.kill(os.getpid(), signum)
        pytest.fail("o handler real deveria interromper o runner")

    result, code = cli.run_task(_cli_args(tmp_path), "tarefa", runner=runner)

    assert code == exit_code
    assert result["status"] == "cancelled"
    assert result["exitCode"] == exit_code
    assert result["effectsMayExist"] is True
    assert result["error"]["code"] == "terminated"
    assert {
        sig: signal.getsignal(sig)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    } == previous_handlers
    assert list((state / "tmp").iterdir()) == []


@contextlib.contextmanager
def _admission_by_earliest_deadline(queue_deadline, total_deadline=None):
    if total_deadline is not None and total_deadline <= queue_deadline:
        raise base.DeadlineExpired("prazo total esgotado na fila de admissão")
    raise base.QuotaError("fila de admissão cheia; nenhum worker iniciado")
    yield  # pragma: no cover


def test_admission_queue_deadline_before_total_is_quota_75(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.base, "global_slot", _admission_by_earliest_deadline)

    def forbidden_runner(*_args):
        pytest.fail("runner não deve ser chamado quando a admissão falha")

    result, code = cli.run_task(
        _cli_args(tmp_path, timeout=10.0, queue_timeout=0.05, total_timeout=None),
        "tarefa",
        runner=forbidden_runner,
    )

    assert code == 75
    assert result["exitCode"] == 75
    assert result["error"]["code"] == "quota"


def test_admission_total_deadline_before_queue_is_timeout_124(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.base, "global_slot", _admission_by_earliest_deadline)

    def forbidden_runner(*_args):
        pytest.fail("runner não deve ser chamado quando a admissão falha")

    result, code = cli.run_task(
        _cli_args(tmp_path, timeout=10.0, queue_timeout=10.0, total_timeout=0.05),
        "tarefa",
        runner=forbidden_runner,
    )

    assert code == 124
    assert result["exitCode"] == 124
    assert result["error"]["code"] == "timeout"


def _prepare_direct_job(state: Path, output_dir: Path, job_id: str, options: dict):
    directory = Path(jobs._job_dir(job_id, create=True))
    output = output_dir / f"{job_id}.out"
    fd = jobs._create_output(str(output))
    identity = os.fstat(fd)
    os.close(fd)
    metadata = {
        "id": job_id,
        "status": "queued",
        "createdAt": 1.0,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "starttime": 123,
        "output": str(output),
        "exitCode": None,
        "outputDev": identity.st_dev,
        "outputIno": identity.st_ino,
    }
    jobs._atomic_write(jobs._metadata_path(str(directory)), metadata, limit=64 * 1024)
    jobs._atomic_write(
        jobs._input_path(str(directory)),
        {"options": options, "task": "timeout"},
        limit=200000,
    )
    return directory, output


def test_real_job_callback_deadline_is_timeout_124_and_cleans_input(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    monkeypatch.setattr(base, "STATE", str(state))
    jobs.configure(base)
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_args: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    job_id = "d" * 32
    _directory, output = _prepare_direct_job(
        state,
        tmp_path,
        job_id,
        {"total_timeout": 0.05},
    )

    def slow_callback(_options, _task):
        time.sleep(30)
        return {"status": "late"}, 0

    assert jobs.run_child(job_id, slow_callback) == 124
    public = jobs.read_status(job_id)
    assert public["status"] == "timed_out"
    assert public["exitCode"] == 124
    assert output.read_bytes().rstrip().endswith(b"exit=124")
    assert not (Path(jobs._job_dir(job_id)) / "input.json").exists()


def test_invoke_limited_reaps_leader_after_pipes_reach_eof(tmp_path):
    command = [
        sys.executable,
        "-c",
        (
            "import os, sys, time; "
            "os.write(1, b'out'); os.write(2, b'err'); "
            "os.close(1); os.close(2); time.sleep(0.2); sys.exit(7)"
        ),
    ]
    started = time.monotonic()
    returncode, stdout, stderr = cli._invoke_limited(
        command,
        dict(os.environ),
        time.monotonic() + 2.0,
        1024,
    )

    assert time.monotonic() - started >= 0.18
    assert returncode == 7
    assert stdout == "out"
    assert stderr == "err"


def test_invoke_limited_timeout_kills_process_group(tmp_path):
    marker = tmp_path / "processes"
    script = textwrap.dedent(
        """
        import os
        import signal
        import sys
        import time
        from pathlib import Path

        marker = Path(sys.argv[1])

        def stopped(_signum, _frame):
            Path(f"{marker}.{os.getpid()}.stopped").write_text("stopped")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stopped)
        child = os.fork()
        if child == 0:
            signal.signal(signal.SIGTERM, stopped)
            Path(f"{marker}.{os.getpid()}.child").write_text("child")
            time.sleep(30)
        else:
            marker.write_text(f"{os.getpid()} {child}")
            time.sleep(30)
        """
    )
    command = [sys.executable, "-c", script, str(marker)]
    try:
        with pytest.raises(features.FeatureError, match="prazo") as raised:
            cli._invoke_limited(
                command,
                dict(os.environ),
                time.monotonic() + 0.1,
                1024,
            )
        assert raised.value.code == "timeout"
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            stopped_files = list(tmp_path.glob("processes.*.stopped"))
            if len(stopped_files) >= 2:
                break
            time.sleep(0.02)
        assert len(stopped_files) >= 2
        assert marker.exists()
    finally:
        if marker.exists():
            for value in marker.read_text(encoding="utf-8").split():
                with contextlib.suppress(ProcessLookupError, ValueError):
                    os.kill(int(value), signal.SIGKILL)


def _run_candidate(*args, env_extra=None):
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                [str(env_extra["shadow"]), str(ROOT / "src")]
            )
            if env_extra and "shadow" in env_extra
            else str(ROOT / "src"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    return subprocess.run(
        [sys.executable, str(CANDIDATE), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )


def test_candidate_always_prioritizes_fixed_src_over_shadow_package(tmp_path):
    shadow = tmp_path / "shadow"
    package = shadow / "zcode_worker"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "raise SystemExit(42)\n",
        encoding="utf-8",
    )

    listed = _run_candidate("--list-models", "--json", env_extra={"shadow": shadow})

    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["status"] == "models"
    assert payload["combinations"]
    assert listed.returncode != 42


def test_core_source_loader_does_not_invoke_main_and_legacy_catalog_has_footer():
    source = ROOT / "src" / "zcode_worker" / "base.py"
    loader = SourceFileLoader("review_loaded_core", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    assert callable(module.main)

    legacy = _run_candidate("--_background-child", "--list-models")

    assert legacy.returncode == 0, legacy.stderr
    assert "glm-5.3-flash" in legacy.stdout
    assert "exit=0" in legacy.stdout


def test_native_provider_usage_contract_is_numeric_and_secret_safe():
    usage = {
        "source": "provider",
        "modelRequestCount": 1,
        "webFetchRequests": 2,
        "webSearchRequests": 3,
        "inputTokens": 11,
        "outputTokens": 7,
        "totalTokens": 18,
        "reasoningTokens": 4,
        "cachedInputTokens": 5,
        "cacheReadInputTokens": 6,
        "cacheCreationInputTokens": 7,
        "promptTokens": 8,
        "completionTokens": 9,
        "cacheReadTokens": 10,
        "cacheWriteTokens": 11,
    }
    payload = features.validate_response(
        {"sessionId": "sess_native_review", "response": "synthetic", "usage": usage}
    )
    assert payload["usage"] == usage

    invalid = [
        {**usage, "source": "unknown-provider"},
        {**usage, "source": True},
        {**usage, "inputTokens": True},
        {**usage, "reasoningTokens": math.nan},
        {**usage, "cacheReadTokens": math.inf},
        {**usage, "unknownMetric": 1},
    ]
    for candidate in invalid:
        with pytest.raises(features.FeatureError) as raised:
            features.validate_response(
                {
                    "sessionId": "sess_native_review",
                    "response": "synthetic",
                    "usage": candidate,
                }
            )
        assert "unknown-provider" not in str(raised.value)


def test_jobs_use_real_proc_identity_even_when_core_admission_root_is_fake(
    tmp_path, monkeypatch
):
    fake_proc = tmp_path / "fake-proc"
    fake_proc.mkdir()
    monkeypatch.setenv("ZCODE_WORKER_CONTEXT_PROC_ROOT", str(fake_proc))
    monkeypatch.setattr(base, "PROC_ROOT", str(fake_proc))
    jobs.configure(base)

    details = jobs._proc_details(os.getpid())
    identity = jobs._own_process_identity(os.getpid())

    assert jobs._PROC_ROOT == "/proc"
    assert details is not None
    assert details[0] == os.getuid()
    assert identity == (os.getuid(), details[1])


def test_jobs_fail_closed_without_signaling_pid_with_wrong_starttime(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    monkeypatch.setattr(base, "STATE", str(state))
    jobs.configure(base)
    job_id = "e" * 32
    directory = Path(jobs._job_dir(job_id, create=True))
    output = tmp_path / "result"
    fd = jobs._create_output(str(output))
    identity = os.fstat(fd)
    os.close(fd)
    metadata = {
        "id": job_id,
        "status": "running",
        "createdAt": 1.0,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "starttime": 0,
        "output": str(output),
        "exitCode": None,
        "outputDev": identity.st_dev,
        "outputIno": identity.st_ino,
    }
    jobs._atomic_write(jobs._metadata_path(str(directory)), metadata, limit=64 * 1024)
    signaled = []
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", lambda *args: signaled.append(args))

    result = jobs.cancel_job(job_id)

    assert result["status"] == "lost"
    assert signaled == []
    assert not (directory / "input.json").exists()


@pytest.mark.parametrize("effects", [True, False])
def test_late_background_cancel_preserves_effects_and_warning(tmp_path, monkeypatch, effects):
    state = tmp_path / "state"
    monkeypatch.setattr(base, "STATE", str(state))
    jobs.configure(base)
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_args: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    job_id = "e" * 32
    directory, output = _prepare_direct_job(state, tmp_path, job_id, {"mode": "edit" if effects else "plan"})

    def callback(_options, _task):
        with jobs._job_lock(job_id) as locked:
            metadata = jobs._load_metadata(locked, job_id)
            metadata["cancelRequestedAt"] = time.time()
            jobs._atomic_write(jobs._metadata_path(locked), metadata, limit=64 * 1024)
        return {"schemaVersion": 1, "effectsMayExist": effects, "response": "retained", "exitCode": 143}, 143

    assert jobs.run_child(job_id, callback) == 143
    public = jobs.read_status(job_id)
    assert public["status"] == "cancelled" and public["exitCode"] == 143
    payload = json.loads(output.read_text().splitlines()[0])
    assert payload.get("effectsMayExist") is effects
    assert payload["schemaVersion"] == 1 and payload["response"] == "retained"
    assert payload["exitCode"] == 143 and payload["status"] == "cancelled"
    if effects:
        assert "revise o diff" in payload["message"]
    else:
        assert "revise o diff" not in payload.get("message", "")
    assert output.read_bytes().rstrip().endswith(b"exit=143")
    assert not (directory / "input.json").exists()
