"""Contratos de efeitos parciais depois da invocação da CLI."""

from __future__ import annotations

import json
import signal
import sqlite3
from argparse import Namespace
from contextlib import contextmanager

import pytest

from zcode_worker import base, cli, features


def _args(tmp_path, **overrides):
    values = dict(
        tarefa=None,
        cwd=str(tmp_path),
        mode="edit",
        spec=None,
        timeout=2.0,
        queue_timeout=2.0,
        total_timeout=None,
        model="glm-5.3-flash",
        effort="high",
        profile=None,
        resume=None,
        attach=[],
        disallowed_tools=[],
        max_output_bytes=None,
        json=True,
        dry_run=False,
        doctor=False,
        list_models=False,
        saida=None,
        status=None,
        cancel=None,
        _job_child=None,
    )
    values.update(overrides)
    return Namespace(**values)


@contextmanager
def _provider(tmp_path):
    yield str(tmp_path / "provider.json")


def _prepare_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: _provider(tmp_path))
    monkeypatch.setattr(
        cli.features,
        "temporary_provider",
        lambda *_args, **_kwargs: _provider(tmp_path),
    )


def _write_selection_db(path, *, model="GLM-5.3-Flash", effort="high"):
    connection = sqlite3.connect(path)
    connection.execute(
        "create table session_entry(session_id text, type text, time_created integer, data text)"
    )
    connection.execute(
        "insert into session_entry values (?, ?, ?, ?)",
        (
            "sess_effects",
            "runtime/model_selection",
            1,
            json.dumps(
                {
                    "modelSelection": {
                        "providerId": features.PROVIDER_ID,
                        "modelId": model,
                        "options": {"reasoningLevel": effort},
                    }
                }
            ),
        ),
    )
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        '{"sessionId":"sess_effects","response":"ok","usage":{"unknown":1}}',
        '{"sessionId":"sess_effects","response":""}',
    ],
)
def test_post_cli_validation_errors_preserve_file_and_warn(tmp_path, monkeypatch, stdout):
    _prepare_engine(monkeypatch, tmp_path)
    output = tmp_path / "dummy.txt"

    def runner(*_args):
        output.write_text("written-before-validation", encoding="utf-8")
        return 0, stdout, "raw stderr must stay private"

    result, code = cli.run_task(_args(tmp_path), "tarefa", runner=runner)

    assert code == 1
    assert result["status"] == "error"
    assert result["effectsMayExist"] is True
    assert output.read_text(encoding="utf-8") == "written-before-validation"
    assert cli._EFFECTS_WARNING in result["error"]["message"]
    assert "raw stderr" not in json.dumps(result)
    assert "tarefa" not in json.dumps(result)


def test_post_cli_effective_selection_mismatch_preserves_file_and_warns(tmp_path, monkeypatch):
    _prepare_engine(monkeypatch, tmp_path)
    db = tmp_path / "sessions.sqlite"
    _write_selection_db(db, model="GLM-5.3", effort="low")
    output = tmp_path / "mismatch.txt"

    def runner(*_args):
        output.write_text("runner-effect", encoding="utf-8")
        return 0, '{"sessionId":"sess_effects","response":"ok"}', ""

    result, code = cli.run_task(_args(tmp_path), "tarefa", db_path=db, runner=runner)

    assert code == 1
    assert result["error"]["code"] == "invalid_request"
    assert result["effectsMayExist"] is True
    assert output.read_text(encoding="utf-8") == "runner-effect"
    assert cli._EFFECTS_WARNING in result["error"]["message"]


@pytest.mark.parametrize(
    "mode, error",
    [
        ("edit", features.FeatureError("quota", "quota")),
        ("edit", features.FeatureError("cli", "cli_failed")),
        ("yolo", features.FeatureError("timeout", "timeout")),
    ],
)
def test_runner_failures_mark_effects_and_do_not_retry(tmp_path, monkeypatch, mode, error):
    _prepare_engine(monkeypatch, tmp_path)
    calls = []

    def runner(*_args):
        calls.append(1)
        raise error

    result, code = cli.run_task(_args(tmp_path, mode=mode), "tarefa", runner=runner)

    assert calls == [1]
    assert result["effectsMayExist"] is True
    assert cli._EFFECTS_WARNING in result["error"]["message"]
    assert result["error"]["code"] == error.code
    assert code == (75 if error.code == "quota" else 124 if error.code == "timeout" else 1)


def test_cancel_after_runner_marks_effects_and_preserves_file(tmp_path, monkeypatch):
    _prepare_engine(monkeypatch, tmp_path)
    output = tmp_path / "cancelled.txt"

    def runner(*_args):
        output.write_text("cancelled-after-write", encoding="utf-8")
        raise base.Terminated(signal.SIGTERM)

    result, code = cli.run_task(_args(tmp_path), "tarefa", runner=runner)

    assert code == 128 + signal.SIGTERM
    assert result["status"] == "cancelled"
    assert result["effectsMayExist"] is True
    assert output.read_text(encoding="utf-8") == "cancelled-after-write"
    assert cli._EFFECTS_WARNING in result["error"]["message"]


def test_pre_cli_failures_and_plan_never_claim_effects(tmp_path, monkeypatch):
    _prepare_engine(monkeypatch, tmp_path)
    runner_called = False

    def runner(*_args):
        nonlocal runner_called
        runner_called = True
        return 0, '{"sessionId":"sess_effects","response":"ok"}', ""

    invalid_cwd, _ = cli.run_task(_args(tmp_path / "missing"), "tarefa", runner=runner)
    invalid_task, _ = cli.run_task(_args(tmp_path), "", runner=runner)
    dry_run, _ = cli.run_task(_args(tmp_path, dry_run=True), "tarefa", runner=runner)
    plan, _ = cli.run_task(_args(tmp_path, mode="plan"), "tarefa", runner=runner)

    assert invalid_cwd["effectsMayExist"] is False
    assert invalid_task["effectsMayExist"] is False
    assert dry_run.get("effectsMayExist", False) is False
    assert plan["effectsMayExist"] is False
    assert runner_called is True


def test_resume_config_and_queue_failures_are_pre_cli(tmp_path, monkeypatch):
    _prepare_engine(monkeypatch, tmp_path)
    runner_called = False

    def runner(*_args):
        nonlocal runner_called
        runner_called = True
        pytest.fail("CLI não deveria ser chamada")

    resume, _ = cli.run_task(
        _args(tmp_path, resume="sess_missing"), "tarefa", runner=runner
    )
    assert resume["error"]["code"] == "resume_mismatch"
    assert resume["effectsMayExist"] is False

    def fail_provider(*_args, **_kwargs):
        raise features.FeatureError("provider falhou", "config")

    monkeypatch.setattr(cli.features, "temporary_provider", fail_provider)
    config, _ = cli.run_task(_args(tmp_path), "tarefa", runner=runner)
    assert config["error"]["code"] == "config"
    assert config["effectsMayExist"] is False

    class BlockedSlot:
        def __enter__(self):
            raise base.DeadlineExpired("fila cheia")

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: BlockedSlot())
    queue, _ = cli.run_task(_args(tmp_path), "tarefa", runner=runner)
    assert queue["error"]["code"] == "timeout"
    assert queue["effectsMayExist"] is False
    assert runner_called is False


def test_control_mode_failures_are_false(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "doctor",
        lambda: (_ for _ in ()).throw(features.FeatureError("doctor falhou")),
    )
    doctor_code = cli.main(["--json", "--doctor"])
    assert doctor_code == 1

    monkeypatch.setattr(
        cli,
        "list_models",
        lambda: (_ for _ in ()).throw(features.FeatureError("models falhou")),
    )
    models_code = cli.main(["--json", "--list-models"])
    assert models_code == 1


def test_internal_effect_flag_from_input_is_ignored(tmp_path, monkeypatch):
    _prepare_engine(monkeypatch, tmp_path)
    args = _args(tmp_path, _effects_may_exist=True)
    result, _ = cli.run_task(args, "", runner=lambda *_args: pytest.fail("não deveria iniciar"))
    assert result["effectsMayExist"] is False
