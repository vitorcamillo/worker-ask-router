"""Cobertura complementar de contratos e caminhos adversariais locais.

Este arquivo não chama a CLI real, a rede ou qualquer modelo.  Os processos
usados pelos testes são pequenos filhos locais criados em ``tmp_path``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zcode_worker import base, cli, features, jobs


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


def cli_args(tmp_path, **overrides):
    return feature_args(cwd=str(tmp_path), **overrides)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(base, "STATE", str(state))
    monkeypatch.setattr(base, "PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setattr(jobs, "_PROC_ROOT", str(tmp_path / "proc"))
    jobs.configure(base)
    return state


def write_provider(path: Path, *, model="GLM-5.3-Flash", effort="high"):
    path.write_text(
        json.dumps(
            {
                "defaultModelSelection": {
                    "providerId": base.PROVIDER_ID,
                    "modelId": model,
                    "options": {"reasoningLevel": effort},
                }
            }
        ),
        encoding="utf-8",
    )


def make_child(tmp_path: Path, body: str = "print('child')") -> list[str]:
    script = tmp_path / "local-child.py"
    script.write_text(body, encoding="utf-8")
    return [str(Path(sys.executable).resolve()), str(script)]


def make_job_entrypoint(tmp_path: Path) -> list[str]:
    return make_child(
        tmp_path,
        """
import sys
import time
from zcode_worker import jobs

def runner(options, task):
    if task == 'boom':
        raise RuntimeError('private callback detail')
    if task == 'timeout':
        raise jobs.DeadlineExpired('deadline')
    if task == 'cancel':
        raise jobs.Terminated(15)
    if task == 'hold':
        time.sleep(30)
    if task == 'bad-code':
        return {'ok': True}, 999
    return {'task': task}, 0

if __name__ == '__main__':
    raise SystemExit(jobs.run_child(sys.argv[-1], runner))
""".lstrip(),
    )


def test_feature_numeric_parsers_reject_types_and_bounds():
    for value in ("bad", "nan", "inf", "0", "-1", "3601", None):
        with pytest.raises(argparse.ArgumentTypeError):
            features._finite_positive(value)
    for value in ("bad", "0", "-1", str(features.DEFAULT_MAX_OUTPUT_BYTES + 1), None):
        with pytest.raises(argparse.ArgumentTypeError):
            features._positive_bytes(value)
    assert features._finite_positive("1.5") == 1.5
    assert features._positive_bytes("4") == 4


@pytest.mark.parametrize(
    "argv",
    [
        ["--doctor", "task"],
        ["--list-models", "--doctor"],
        ["--status", "x" * 32, "--cancel", "y" * 32],
        ["--status", "x" * 32, "task"],
        ["--cancel", "x" * 32, "--dry-run"],
        ["--_job-child", "x" * 32, "task"],
        ["--saida", "/tmp/o", "--doctor"],
        ["--spec", "/tmp/spec", "task"],
        ["--resume", "invalid"],
    ],
)
def test_parse_args_rejects_conflicting_controls(argv):
    with pytest.raises(features.FeatureError):
        features.parse_args(argv)


def test_parse_args_fills_queue_timeout_and_accepts_all_controls():
    parsed = features.parse_args(
        [
            "--cwd",
            "/tmp",
            "--mode",
            "yolo",
            "--timeout",
            "3",
            "--model",
            "FLASH",
            "--effort",
            "max",
            "--profile",
            "rapido",
            "--attach",
            "one.txt",
            "--disallowed-tools",
            "a,b",
            "--max-output-bytes",
            "10",
            "--json",
            "task",
        ]
    )
    # Sem --queue-timeout a fila foreground usa o default de 30s; o prazo de
    # execução (--timeout) é independente (job em background usa 1800s).
    assert parsed.queue_timeout == 30.0
    assert parsed.model == "glm-5.3-flash"
    assert parsed._job_child is None
    background = features.parse_args(["--cwd", "/tmp", "--saida", "/tmp/out", "task"])
    assert background.queue_timeout == 1800.0
    explicit = features.parse_args(["--cwd", "/tmp", "--queue-timeout", "7", "task"])
    assert explicit.queue_timeout == 7.0


def test_feature_selection_display_and_canonical_error_paths(monkeypatch):
    with pytest.raises(features.FeatureError, match="perfil"):
        features.selection_for("missing", None, None)
    with pytest.raises(features.FeatureError, match="modelo"):
        features.selection_for(None, "missing", None)
    with pytest.raises(features.FeatureError, match="effort"):
        features.selection_for(None, None, "medium")
    assert features.model_display("GLM-5.3-FLASH") == "GLM-5.3-Flash"
    assert features.model_display("glm-5.3") == "GLM-5.3"
    with pytest.raises(features.FeatureError):
        features.model_display("unknown")
    monkeypatch.setattr(features.base, "canonical_model", lambda _value: (_ for _ in ()).throw(TypeError()))
    with pytest.raises(argparse.ArgumentTypeError):
        features._canonical_model("flash")


def test_task_decoding_covers_bytes_utf8_nul_and_input_sources(tmp_path, monkeypatch):
    assert features._decode_bounded(bytearray("ação".encode())) == "ação"
    assert features._decode_bounded("texto") == "texto"
    for data in (b"\xff", object(), b"x" * (features.TASK_MAX_BYTES + 1)):
        with pytest.raises(features.FeatureError):
            features._decode_bounded(data)

    spec = tmp_path / "spec.md"
    spec.write_text("from-file", encoding="utf-8")
    assert features.read_task(feature_args(spec=str(spec))) == "from-file"
    assert features.read_task(feature_args(tarefa="positional")) == "positional"
    assert features.read_task(feature_args(), stdin_text=b"stdin") == "stdin"
    assert features.read_task(feature_args(), stdin_text="unicode") == "unicode"
    with pytest.raises(features.FeatureError, match="ilegível"):
        features.read_task(feature_args(spec=str(tmp_path / "missing")))
    with pytest.raises(features.FeatureError, match="vazia"):
        features.read_task(feature_args(), stdin_text=" \n")
    with pytest.raises(features.FeatureError, match="byte inválido"):
        features.read_task(feature_args(tarefa="a\x00b"))
    monkeypatch.setattr(features.sys, "stdin", io.StringIO("from-stdin"))
    assert features.read_task(feature_args()) == "from-stdin"


def test_task_stdin_read_failure_is_safe(monkeypatch):
    class Broken:
        def read(self, _limit):
            raise OSError("private detail")

    monkeypatch.setattr(features.sys, "stdin", Broken())
    with pytest.raises(features.FeatureError, match="vazia"):
        features.read_task(feature_args())


def test_attachments_cover_regular_duplicate_secret_type_size_and_special_file(tmp_path):
    first = tmp_path / "note.txt"
    first.write_text("ok", encoding="utf-8")
    assert features.validate_attachments([str(first), str(first)]) == [str(first.resolve())]
    with pytest.raises(features.FeatureError, match="inválido"):
        features.validate_attachments([None])
    with pytest.raises(features.FeatureError, match="ilegível"):
        features.validate_attachments([str(tmp_path / "missing.txt")])
    with pytest.raises(features.FeatureError, match="regular"):
        features.validate_attachments([str(tmp_path)])
    unsupported = tmp_path / "note.exe"
    unsupported.write_text("x", encoding="utf-8")
    with pytest.raises(features.FeatureError, match="tipo"):
        features.validate_attachments([str(unsupported)])
    secret = tmp_path / "my-token.txt"
    secret.write_text("x", encoding="utf-8")
    with pytest.raises(features.FeatureError, match="proibido"):
        features.validate_attachments([str(secret)])
    huge = tmp_path / "huge.txt"
    with huge.open("wb") as stream:
        stream.truncate(features.MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(features.FeatureError, match="limite"):
        features.validate_attachments([str(huge)])
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    with pytest.raises(features.FeatureError, match="regular"):
        features.validate_attachments([str(fifo)])
    with pytest.raises(features.FeatureError, match="número"):
        features.validate_attachments([str(first)] * (features.MAX_ATTACHMENTS + 1))


def test_denylist_and_response_decoder_reject_ambiguous_inputs():
    assert features.validate_denylist(None) == []
    assert features.validate_denylist(["tool,tool", "Other_1"]) == ["tool", "Other_1"]
    for value in ([1], ["a,,b"], ["-bad"], [" with space"]):
        with pytest.raises(features.FeatureError):
            features.validate_denylist(value)
    assert features._diagnostic_line("[notice]")
    assert features._diagnostic_line("warning: local")
    assert not features._diagnostic_line("not trusted")
    assert features.decode_response("info: local\n{\"x\": 1}") == {"x": 1}
    for text in ("", "plain", "prefix {\"x\": 1}", "[ok]\n{}{}", "[]", "{bad}"):
        with pytest.raises(features.FeatureError):
            features.decode_response(text)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"response": "", "sessionId": "sess_ok"},
        {"response": "ok", "sessionId": "bad"},
        {"response": "ok", "sessionId": "sess_ok", "status": "error"},
        {"response": "ok", "sessionId": "sess_ok", "status": 1},
        {"response": "ok", "sessionId": "sess_ok", "projection": []},
        {"response": "ok", "sessionId": "sess_ok", "projection": {"status": 1}},
        {"response": "ok", "sessionId": "sess_ok", "projection": "paused"},
        {"response": "ok", "sessionId": "sess_ok", "usage": None},
        {"response": "ok", "sessionId": "sess_ok", "usage": []},
        {"response": "ok", "sessionId": "sess_ok", "usage": {"unknown": 1}},
        {"response": "ok", "sessionId": "sess_ok", "usage": {"totalTokens": -1}},
        {"response": "ok", "sessionId": "sess_ok", "usage": {"totalTokens": True}},
        {"response": "ok", "sessionId": "sess_ok", "usage": {"totalTokens": float("inf")}},
    ],
)
def test_validate_response_rejects_contract_attacks(payload):
    with pytest.raises(features.FeatureError):
        features.validate_response(payload)


def test_validate_response_accepts_string_projection_and_normalizes_usage():
    normalized = features.validate_response(
        {"response": "ok", "sessionId": "sess_ok", "projection": "success", "usage": {"totalTokens": 2.0}}
    )
    assert normalized["usage"] == {"totalTokens": 2.0}
    assert features.validate_response({"response": "ok", "sessionId": "sess_ok"})["usage"] == {}


def make_selection_db(path: Path, *, session="sess_db", data=None, schema=True):
    connection = sqlite3.connect(path)
    if schema:
        connection.execute("create table session_entry(session_id text, type text, time_created int, data text)")
        if data is not None:
            connection.execute(
                "insert into session_entry values (?, ?, ?, ?)",
                (session, "runtime/model_selection", 1, json.dumps(data)),
            )
    connection.commit()
    connection.close()


def test_effective_selection_handles_missing_table_bad_json_and_valid_alias(tmp_path):
    assert features.effective_selection("sess_missing", tmp_path / "none.sqlite") is None
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert features.effective_selection("sess_missing", empty) is None
    bad = tmp_path / "bad.sqlite"
    make_selection_db(bad, data="not-json")
    connection = sqlite3.connect(bad)
    connection.execute("update session_entry set data=?", ("{",))
    connection.commit()
    connection.close()
    with pytest.raises(features.FeatureError):
        features.effective_selection("sess_db", bad)
    valid = tmp_path / "valid.sqlite"
    make_selection_db(
        valid,
        data={
            "modelSelection": {
                "providerId": base.PROVIDER_ID,
                "modelId": "glm-5.3-flash",
                "options": {"reasoningLevel": "high"},
            }
        },
    )
    assert features.effective_selection("sess_db", valid)["modelId"] == "GLM-5.3-Flash"
    with pytest.raises(features.FeatureError):
        features.effective_selection("not-a-session", valid)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"modelSelection": []},
        {"modelSelection": {"providerId": "wrong", "modelId": "GLM-5.3", "options": {"reasoningLevel": "high"}}},
        {"modelSelection": {"providerId": base.PROVIDER_ID, "modelId": "unknown", "options": {"reasoningLevel": "high"}}},
        {"modelSelection": {"providerId": base.PROVIDER_ID, "modelId": "GLM-5.3", "options": {"reasoningLevel": "medium"}}},
    ],
)
def test_selection_from_json_rejects_claimed_or_corrupt_selection(data):
    if data == {} or data == {"modelSelection": []}:
        assert features._selection_from_json(data) is None
    else:
        with pytest.raises(features.FeatureError):
            features._selection_from_json(data)


def test_verify_effective_requires_persisted_matching_selection(tmp_path):
    db = tmp_path / "selection.sqlite"
    make_selection_db(
        db,
        data={
            "modelSelection": {
                "providerId": base.PROVIDER_ID,
                "modelId": "GLM-5.3-Flash",
                "options": {"reasoningLevel": "high"},
            }
        },
    )
    payload = {"sessionId": "sess_db"}
    assert features.verify_effective(payload, "glm-5.3-flash", "high", db_path=db)["effort"] == "high"
    with pytest.raises(features.FeatureError):
        features.verify_effective({"sessionId": "sess_db"}, "glm-5.3", "high", db_path=db)
    with pytest.raises(features.FeatureError):
        features.verify_effective({}, "glm-5.3-flash", "high", db_path=db)


def test_temporary_provider_is_private_and_cleans_on_body_or_config_error(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    write_provider(provider)
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    state = tmp_path / "state"
    with features.temporary_provider("glm-5.3-flash", "high", state_dir=state) as path:
        temp = Path(path)
        assert temp.exists()
        assert stat.S_IMODE(temp.stat().st_mode) == 0o600
        assert json.loads(temp.read_text())["defaultModelSelection"]["options"]["reasoningLevel"] == "high"
    assert not temp.exists()
    with pytest.raises(features.FeatureError):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=Path("relative")):
            pass
    monkeypatch.setattr(features.base, "carregar_config_provider", lambda *_: (_ for _ in ()).throw(base.WorkerError("bad")))
    with pytest.raises(features.FeatureError, match="provider"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state):
            pass


def test_temporary_provider_rejects_symlink_or_unprivate_state(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "state"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(features.FeatureError):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=symlink):
            pass
    state = tmp_path / "state2"
    state.mkdir(mode=0o755)
    with features.temporary_provider("glm-5.3-flash", "high", state_dir=state):
        assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_build_argv_public_namespace_and_configure_guards(tmp_path):
    attachment = tmp_path / "note.txt"
    attachment.write_text("ok", encoding="utf-8")
    argv = features.build_cli_argv(
        "task", str(tmp_path), "edit", "glm-5.3-flash", "high",
        resume="sess_resume", attachments=[str(attachment)], disallowed_tools=["x,x"], cli_path="/bin/cli",
    )
    assert argv[0:2] == ["/bin/cli", "-p"]
    assert "--resume" in argv and argv.count("--disallowed-tools") == 1
    assert features.build_cli_argv("task", str(tmp_path), "plan", "glm-5.3-flash", "high")[9:] == []
    for bad in (("relative", "plan"), (str(tmp_path), "bad")):
        with pytest.raises(features.FeatureError):
            features.build_cli_argv("task", bad[0], bad[1], "glm-5.3-flash", "high")
    with pytest.raises(features.FeatureError):
        features.build_cli_argv("\x00", str(tmp_path), "plan", "glm-5.3-flash", "high")
    normalized = features.public_namespace(
        feature_args(profile="rapido", model=None, effort=None, queue_timeout=None, max_output_bytes=None)
    )
    assert normalized.model == "glm-5.3-flash" and normalized.effort == "low"
    for kwargs in ({"timeout": True}, {"queue_timeout": 0}, {"total_timeout": float("inf")}, {"max_output_bytes": True}):
        with pytest.raises(features.FeatureError):
            features.public_namespace(feature_args(**kwargs))
    with pytest.raises(features.FeatureError, match="core"):
        features.configure(SimpleNamespace())
    assert features.configure(None) is features._CORE


def test_cli_limited_invoke_failures_and_safe_classification(tmp_path):
    with pytest.raises(features.FeatureError, match="iniciar"):
        cli._invoke_limited([str(tmp_path / "missing")], {}, time.monotonic() + 1, 10)
    command = [sys.executable, "-c", "import sys; sys.stderr.write('diagnostic')"]
    code, stdout, stderr = cli._invoke(command, dict(os.environ), time.monotonic() + 2)
    assert code == 0 and stdout == "" and stderr == "diagnostic"
    assert cli._classify_process_failure(1, "quota exhausted").code == "quota"
    # Z.ai código 1310 chega como HTTP 429, mas é cota semanal/mensal esgotada (exit 75)
    assert cli._classify_process_failure(
        1, "429 [1310][Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-10-26]"
    ).code == "quota"
    assert cli._classify_process_failure(1, "HTTP 429").code == "rate_limit"
    assert cli._classify_process_failure(-signal.SIGTERM, "").code == "terminated"
    assert cli._classify_process_failure(2, "other").code == "cli_failed"
    assert cli._error_result(base.DeadlineExpired("late"), -1)[1] == 124
    assert cli._error_result(features.FeatureError("quota", "quota"), 0)[1] == 75
    assert cli._error_result(RuntimeError("secret"), 0)[0]["error"]["code"] == "internal"


def test_cli_session_resume_schema_variants(tmp_path):
    db = tmp_path / "sessions.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("create table session(id text, directory text)")
    connection.execute("insert into session values (?, ?)", ("sess_one", str(tmp_path)))
    connection.commit()
    connection.close()
    assert cli._session_cwd_matches("sess_one", str(tmp_path), db)
    assert not cli._session_cwd_matches("sess_one", str(tmp_path / "other"), db)
    no_table = tmp_path / "no-table.sqlite"
    sqlite3.connect(no_table).close()
    assert not cli._session_cwd_matches("sess_one", str(tmp_path), no_table)
    malformed = tmp_path / "malformed.sqlite"
    malformed.write_text("not sqlite", encoding="utf-8")
    assert not cli._session_cwd_matches("sess_one", str(tmp_path), malformed)
    args = cli_args(tmp_path, resume="sess_one", model="glm-5.3-flash", effort="high")
    with pytest.raises(features.FeatureError):
        cli._verify_resume(args, db_path=db)


def test_cli_retry_policy_is_plan_only_and_deadline_bounded(monkeypatch):
    error = features.FeatureError("rate", "rate_limit")
    assert cli._retry_allowed(feature_args(mode="edit"), error, 0, time.monotonic() + 10) is False
    assert cli._retry_allowed(feature_args(mode="plan"), features.FeatureError("bad"), 0, time.monotonic() + 10) is False
    monkeypatch.setattr(cli.base, "RETRY_DELAYS", (0.0,))
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 10.0)
    assert cli._retry_allowed(feature_args(mode="plan"), error, 0, 20.0)
    assert slept == [0.0]
    assert cli._retry_allowed(feature_args(mode="plan"), error, 1, 20.0) is False
    monkeypatch.setattr(cli.base, "RETRY_DELAYS", (2.0,))
    with pytest.raises(features.FeatureError, match="retry"):
        cli._retry_allowed(feature_args(mode="plan"), error, 0, 11.0)


def test_cli_execute_retries_rate_limit_then_succeeds(monkeypatch, tmp_path):
    provider = tmp_path / "provider.json"
    write_provider(provider)
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli.base, "RETRY_DELAYS", (0.0,))
    monkeypatch.setattr(cli.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: contextlib.nullcontext())
    args = cli_args(tmp_path, mode="plan", model="glm-5.3-flash", effort="high", attachments=[])
    calls = []

    def runner(command, env, deadline, limit):
        calls.append((command, env, deadline, limit))
        if len(calls) == 1:
            return 1, "", "429 temporary"
        return 0, '{"sessionId":"sess_x","response":"ok"}', ""

    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"ok": "persisted"})
    result = cli._execute(args, "task", runner=runner)
    assert result[0] == 0 and len(calls) == 2
    assert "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE" in calls[0][1]


def test_cli_run_task_dry_run_success_errors_and_termination(monkeypatch, tmp_path):
    args = cli_args(tmp_path, dry_run=True)
    result, code = cli.run_task(args, "do not execute", runner=lambda *_: pytest.fail("runner called"))
    assert code == 0 and result["status"] == "dry-run"
    bad, bad_code = cli.run_task(cli_args(tmp_path / "missing"), "task")
    assert bad_code == 1 and bad["error"]["code"] == "invalid_request"
    monkeypatch.setattr(cli, "_execute", lambda *a, **k: (1, "", "quota exceeded"))
    quota, quota_code = cli.run_task(cli_args(tmp_path), "task")
    assert quota_code == 75 and quota["error"]["code"] == "quota"
    monkeypatch.setattr(cli, "_execute", lambda *a, **k: (0, '{"sessionId":"sess_ok","response":"ok"}', ""))
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"modelId": "ok"})
    ok, ok_code = cli.run_task(cli_args(tmp_path), "task")
    assert ok_code == 0 and ok["status"] == "ok"
    monkeypatch.setattr(cli, "_execute", lambda *a, **k: (_ for _ in ()).throw(base.Terminated(signal.SIGINT)))
    terminated, terminated_code = cli.run_task(cli_args(tmp_path), "task")
    assert terminated_code == 130 and terminated["status"] == "cancelled"


def test_cli_handlers_printing_doctor_models_and_main_routes(monkeypatch, tmp_path, capsys):
    assert cli._terminated_result(base.Terminated(signal.SIGHUP), -1)[1] == 129
    monkeypatch.setattr(cli.base, "CLI", str(tmp_path / "cli"))
    monkeypatch.setattr(cli.base, "_read_mem_available_bytes", lambda: 1)
    monkeypatch.setattr(cli.base, "count_active_cli_processes", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(cli.base, "_read_settings", lambda: (_ for _ in ()).throw(OSError()))
    report = cli.doctor()
    assert report["memory"] == "abaixo do mínimo" and report["active_cli"] is None
    models = cli.list_models()
    assert models["status"] == "models" and len(models["combinations"]) == len(base.PROVIDER_MODELS) * len(base.EFFORTS)
    for payload, as_json in (
        ({"status": "ok", "response": "text", "sessionId": "s", "effective": {}, "usage": {}}, False),
        ({"status": "models", "combinations": [{"modelId": "m", "effort": "e"}]}, False),
        ({"status": "dry-run", "id": "x"}, False),
        ({"status": "error", "error": {"message": "safe"}}, False),
        ({"status": "ok", "response": "json"}, True),
    ):
        cli._print_result(payload, as_json)
    assert "safe" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_dispatch_jobs", lambda args, task: ({"id": "job"}, 0))
    assert cli.main(["--json", "--status", "a" * 32]) == 0
    assert cli.main(["--json", "--cwd", str(tmp_path), "--dry-run", "task"]) == 0
    assert cli.main(["--json", "--cwd", str(tmp_path), "task"]) == 1


def test_jobs_validation_and_private_persistence_edges(isolated_state, tmp_path, monkeypatch):
    with pytest.raises(base.WorkerError):
        jobs.configure(None)
    with pytest.raises(base.WorkerError):
        jobs._validate_id("../job")
    monkeypatch.setattr(jobs._CORE, "STATE", "relative")
    with pytest.raises(base.WorkerError):
        jobs._state()
    monkeypatch.setattr(jobs._CORE, "STATE", str(isolated_state))
    for task in ("", " \n", "a\x00b", "x" * (jobs._TASK_LIMIT + 1)):
        with pytest.raises(base.WorkerError):
            jobs._validate_task(task)
    assert jobs._validate_task("ok") == "ok"
    for value in ({"api_key": "redacted"}, {"nested": [{"password": "x"}]}, {1: "key"}):
        with pytest.raises(base.WorkerError):
            jobs._validate_options(value)
    for value in ({"unknown": 1}, {"timeout": True}, {"max_output_bytes": 0}, {"json": 1}, {"attach": [1]}):
        with pytest.raises(base.WorkerError):
            jobs._validate_options(value)
    assert jobs._validate_options({"json": False}) == {"json": False}
    assert jobs._json_dump({"x": "á"}).startswith(b"{")
    for value in ({"x": float("nan")}, {"x": object()}):
        with pytest.raises(base.WorkerError):
            jobs._json_dump(value)


def test_jobs_private_dirs_files_atomic_and_output_paths(isolated_state, tmp_path, monkeypatch):
    assert jobs._jobs_dir(create=True).endswith("jobs")
    bad_link = tmp_path / "link-state"
    bad_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(base.WorkerError):
        jobs._private_dir(str(bad_link), create=False)
    path = tmp_path / "private.json"
    fd = jobs._private_file(str(path), create=True)
    os.close(fd)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(base.WorkerError):
        jobs._private_file(str(path), create=True)
    path.write_text('{"ok": true}', encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(base.WorkerError):
        jobs._private_file(str(path))
    path.chmod(0o600)
    assert jobs._read_private_json(str(path), limit=100) == {"ok": True}
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(base.WorkerError):
        jobs._read_private_json(str(path), limit=100)
    target = tmp_path / "atomic.json"
    jobs._atomic_write(str(target), {"v": 1}, limit=100)
    assert json.loads(target.read_text()) == {"v": 1}
    target.unlink()
    target.symlink_to(path)
    with pytest.raises(base.WorkerError):
        jobs._atomic_write(str(target), {"v": 2}, limit=100)
    with pytest.raises(base.WorkerError):
        jobs._validate_output_path("relative")
    output = tmp_path / "output"
    fd = jobs._create_output(str(output))
    os.close(fd)
    with pytest.raises(base.WorkerError):
        jobs._create_output(str(output))
    with pytest.raises(base.WorkerError):
        jobs._output_identity((True, 1))
    with pytest.raises(base.WorkerError):
        jobs._output_identity((1,))


def test_jobs_entrypoint_and_proc_identity_parsing(isolated_state, tmp_path, monkeypatch):
    with pytest.raises(base.WorkerError):
        jobs._validate_entrypoint([])
    with pytest.raises(base.WorkerError):
        jobs._validate_entrypoint(["python"])
    with pytest.raises(base.WorkerError):
        jobs._validate_entrypoint(["/bin/sh"])
    script = tmp_path / "script.py"
    script.write_text("print(1)", encoding="utf-8")
    script.chmod(0o700)
    valid = [str(Path(sys.executable).resolve()), str(script)]
    assert jobs._validate_entrypoint(valid) == valid
    proc = tmp_path / "proc"
    proc.mkdir()
    pid_dir = proc / "123"
    pid_dir.mkdir()
    stat_line = "123 (python worker) S 1 123 123 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 777 0\n"
    (pid_dir / "stat").write_text(stat_line, encoding="ascii")
    (pid_dir / "cmdline").write_bytes(b"python\0script\0--_job-child\0" + b"a" * 32 + b"\0")
    monkeypatch.setattr(jobs, "_PROC_ROOT", str(proc))
    assert jobs._proc_details(0) is None
    assert jobs._proc_details(123)[1] == 777
    assert jobs._proc_has_identity(123, os.getuid(), 777, "a" * 32)
    assert not jobs._proc_has_identity(123, os.getuid(), 778, "a" * 32)
    (pid_dir / "stat").write_text("bad", encoding="ascii")
    assert jobs._proc_details(123) is None


def test_jobs_metadata_input_and_terminal_guards(isolated_state, tmp_path):
    job_id = "a" * 32
    valid = {
        "id": job_id,
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
    assert jobs._metadata_valid(valid, job_id)["id"] == job_id
    for bad in (
        {**valid, "extra": 1},
        {**valid, "status": "unknown"},
        {**valid, "output": "relative"},
        {**valid, "createdAt": float("nan")},
        {**valid, "outputDev": -1},
        {**valid, "pid": 0},
        {**valid, "exitCode": 256},
        {**valid, "duration": -1},
    ):
        with pytest.raises(base.WorkerError):
            jobs._metadata_valid(bad, job_id)
    directory = tmp_path / "job"
    directory.mkdir()
    jobs._atomic_write(jobs._metadata_path(str(directory)), valid, limit=10000)
    jobs._atomic_write(jobs._input_path(str(directory)), {"options": {}, "task": "x"}, limit=10000)
    assert jobs._load_input(str(directory))[1] == "x"
    jobs._set_terminal(str(directory), valid, "failed", 1, -3)
    assert jobs._load_metadata(str(directory), job_id)["status"] == "failed"
    already = jobs._load_metadata(str(directory), job_id)
    assert jobs._set_terminal(str(directory), already, "completed", 0, 1)["status"] == "failed"
    jobs._remove_input(str(directory))
    assert not Path(jobs._input_path(str(directory))).exists()


def test_jobs_start_job_rejects_output_inside_entrypoint_secret_and_spawn_failure(isolated_state, tmp_path, monkeypatch):
    entrypoint = make_job_entrypoint(tmp_path)
    job_root = isolated_state / "jobs"
    with pytest.raises(base.WorkerError):
        jobs.start_job({}, "task", str(job_root / "inside"), entrypoint)
    with pytest.raises(base.WorkerError):
        jobs.start_job({}, entrypoint[1], str(tmp_path / "out"), entrypoint)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("spawn")))
    with pytest.raises(base.WorkerError, match="iniciar"):
        jobs.start_job({}, "task", str(tmp_path / "spawn-fail"), entrypoint)
    assert not (tmp_path / "spawn-fail").exists()


def test_jobs_status_cancel_and_output_footer_edges(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_PROC_ROOT", "/proc")
    entrypoint = make_job_entrypoint(tmp_path)
    started = jobs.start_job({}, "ok", str(tmp_path / "result"), entrypoint)
    final = None
    for _ in range(100):
        final = jobs.read_status(started["id"])
        if final["status"] == "completed":
            break
        time.sleep(0.01)
    assert final["status"] == "completed"
    assert jobs.read_status(started["id"])["status"] == "completed"
    assert jobs.cancel_job(started["id"])["status"] == "completed"
    with pytest.raises(base.WorkerError):
        jobs.read_status("b" * 31)
    output = tmp_path / "footer"
    output.write_bytes(b"payload")
    output.chmod(0o600)
    identity = (output.stat().st_dev, output.stat().st_ino)
    jobs._ensure_footer(str(output), 0, 100, identity)
    assert output.read_bytes().endswith(b"payload\nexit=0\n")
    jobs._ensure_footer(str(output), 0, 100, identity)
    with pytest.raises(base.WorkerError):
        jobs._open_output_for_append(str(output), 1, identity)
    with pytest.raises(base.WorkerError):
        jobs._write_result(str(output), [], 0, 100, identity)
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_: False)
    lost = jobs.read_status(started["id"])
    assert lost["status"] == "completed"


def test_jobs_read_status_lost_and_cancel_queued_paths(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_PROC_ROOT", "/proc")
    entrypoint = make_job_entrypoint(tmp_path)
    started = jobs.start_job({}, "ok", str(tmp_path / "result"), entrypoint)
    with jobs._job_lock(started["id"]) as directory:
        metadata = jobs._load_metadata(directory, started["id"])
        metadata.update({"status": "running", "pid": 999999, "uid": os.getuid(), "starttime": 1})
        jobs._atomic_write(jobs._metadata_path(directory), metadata, limit=64 * 1024)
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_: False)
    assert jobs.read_status(started["id"])["status"] == "lost"
    queued = jobs.start_job({}, "hold", str(tmp_path / "queued"), entrypoint)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and jobs.read_status(queued["id"])["status"] != "running":
        time.sleep(0.01)
    with jobs._job_lock(queued["id"]) as directory:
        metadata = jobs._load_metadata(directory, queued["id"])
        metadata.update({"status": "running", "pid": None, "cancelRequestedAt": 1.0})
        jobs._atomic_write(jobs._metadata_path(directory), metadata, limit=64 * 1024)
    monkeypatch.setattr(jobs, "_now", lambda: time.time() + jobs._CANCEL_CONFIRM_GRACE + 1)
    try:
        assert jobs.read_status(queued["id"])["status"] == "lost"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(started["pid"], signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(queued["pid"], signal.SIGTERM)


def test_jobs_run_callback_footer_and_thread_safety(monkeypatch):
    assert jobs.threading_like_signal_unavailable() is False
    assert jobs._run_callback(lambda o, t: ({"ok": o["x"]}, 0), {"x": 1}, "t") == ({"ok": 1}, 0)
    with pytest.raises(jobs.DeadlineExpired):
        jobs._run_callback(lambda *_: (_ for _ in ()).throw(jobs.DeadlineExpired("already")), {"total_timeout": 1}, "t")
    values = []

    def call():
        values.append(jobs._run_callback(lambda *_: ({"ok": True}, 0), {"total_timeout": 0.01}, "t"))

    thread = threading.Thread(target=call)
    thread.start()
    thread.join(2)
    assert values == [({"ok": True}, 0)]
    with pytest.raises(base.WorkerError):
        jobs.run_child("not-an-id", lambda *_: ({}, 0))
    with pytest.raises(base.WorkerError):
        jobs.run_child("a" * 32, None)


def prepare_direct_job(state: Path, tmp_path: Path, job_id: str, *, status="queued", cancel=False):
    directory = Path(jobs._job_dir(job_id, create=True))
    output = tmp_path / f"{job_id}.out"
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
    if cancel:
        metadata["cancelRequestedAt"] = 1.0
    jobs._atomic_write(jobs._metadata_path(str(directory)), metadata, limit=64 * 1024)
    if status not in jobs._TERMINAL:
        jobs._atomic_write(jobs._input_path(str(directory)), {"options": {}, "task": "task"}, limit=200000)
    return directory, output, metadata


def test_jobs_run_child_directly_persists_all_callback_outcomes(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(base, "PROC_ROOT", "/proc")
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_args: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    cases = (
        ("completed", lambda *_: ({"status": "ok"}, 0), "completed", 0),
        ("failed", lambda *_: (_ for _ in ()).throw(RuntimeError("hidden")), "failed", 1),
        ("timed", lambda *_: (_ for _ in ()).throw(jobs.DeadlineExpired("late")), "timed_out", 124),
        ("cancelled", lambda *_: (_ for _ in ()).throw(jobs.Terminated(15)), "cancelled", 143),
        ("bad-code", lambda *_: ({"status": "bad"}, 256), "failed", 1),
    )
    for index, (_suffix, callback, expected_status, expected_code) in enumerate(cases, 1):
        job_id = f"{index:032x}"
        _directory, output, _metadata = prepare_direct_job(isolated_state, tmp_path, job_id)
        assert jobs.run_child(job_id, callback) == expected_code
        public = jobs.read_status(job_id)
        assert public["status"] == expected_status
        assert public["exitCode"] == expected_code
        assert output.read_bytes().rstrip().endswith(f"exit={expected_code}".encode())
        assert not (Path(jobs._job_dir(job_id)) / "input.json").exists()


def test_jobs_run_child_honors_early_terminal_and_cancelled_metadata(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_args: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    terminal_id = "e" * 32
    _directory, terminal_output, _metadata = prepare_direct_job(
        isolated_state, tmp_path, terminal_id, status="completed"
    )
    assert jobs.run_child(terminal_id, lambda *_: pytest.fail("terminal callback called")) == 0
    assert terminal_output.read_bytes().rstrip().endswith(b"exit=0")
    cancel_id = "c" * 32
    _directory, cancel_output, _metadata = prepare_direct_job(
        isolated_state, tmp_path, cancel_id, cancel=True
    )
    assert jobs.run_child(cancel_id, lambda *_: pytest.fail("cancel callback called")) == 143
    assert cancel_output.read_bytes().rstrip().endswith(b"exit=143")
    assert jobs.read_status(cancel_id)["status"] == "cancelled"


def test_jobs_private_dir_entrypoint_and_output_failure_branches(isolated_state, tmp_path, monkeypatch):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    jobs._private_dir(str(loose), create=False)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700
    with pytest.raises(base.WorkerError):
        jobs._jobs_dir(create=False)
    def fail_jobs_dir(path, create):
        if path == str(isolated_state):
            Path(path).mkdir(mode=0o700)
            return
        raise base.WorkerError("estado local ilegível")

    monkeypatch.setattr(jobs, "_private_dir", fail_jobs_dir)
    with pytest.raises(base.WorkerError, match="jobs ilegível"):
        jobs._jobs_dir(create=True)
    monkeypatch.undo()
    for value in (["/bin/sh", "x"], [str(tmp_path / "missing")], [str(Path(sys.executable).resolve()), str(tmp_path / "link.py")]):
        if len(value) > 1 and value[0] != "/bin/sh":
            Path(value[1]).symlink_to(tmp_path / "target.py")
        with pytest.raises(base.WorkerError):
            jobs._validate_entrypoint(value)
    oversized = {"attach": ["x" * 4097]}
    with pytest.raises(base.WorkerError):
        jobs._validate_options(oversized)
    with pytest.raises(base.WorkerError):
        jobs._validate_options({"attach": ["x"] * 257})


def test_jobs_append_result_and_footer_newline_paths(isolated_state, tmp_path):
    output = tmp_path / "output"
    fd = jobs._create_output(str(output))
    identity = os.fstat(fd)
    os.close(fd)
    expected = (identity.st_dev, identity.st_ino)
    with pytest.raises(base.WorkerError):
        jobs._open_output_for_append(str(output), 200, (expected[0], expected[1] + 1))
    jobs._write_result(str(output), {"ok": True}, 0, 200, expected)
    assert b"exit=0" in output.read_bytes()
    jobs._ensure_footer(str(output), 0, 200, expected)
    output.write_bytes(b"no-newline")
    output.chmod(0o600)
    jobs._ensure_footer(str(output), 3, 200, expected)
    assert output.read_bytes().endswith(b"\nexit=3\n")
    with pytest.raises(base.WorkerError):
        jobs._write_result(str(output), {"x": 1}, True, 200, expected)
    with pytest.raises(base.WorkerError):
        jobs._write_result(str(output), {"x": 1}, 0, 1, expected)
    jobs._safe_error_output(str(tmp_path / "missing"), 1, 10, expected)


def test_jobs_cancel_queued_lost_and_pidfd_paths(isolated_state, tmp_path, monkeypatch):
    queued_id = "d" * 32
    _directory, _output, _metadata = prepare_direct_job(isolated_state, tmp_path, queued_id)
    queued = jobs.cancel_job(queued_id)
    assert queued["status"] == "queued" and queued["cancelRequestedAt"] > 0

    lost_id = "f" * 32
    _directory, _output, metadata = prepare_direct_job(isolated_state, tmp_path, lost_id, status="running")
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_args: False)
    assert jobs.cancel_job(lost_id)["status"] == "lost"

    running_id = "1" * 32
    _directory, _output, metadata = prepare_direct_job(isolated_state, tmp_path, running_id, status="running")
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_args: True)
    real_open = os.open
    holder = {}

    def fake_pidfd_open(_pid, _flags):
        holder["fd"] = real_open("/dev/null", os.O_RDONLY)
        return holder["fd"]

    sent = []
    monkeypatch.setattr(jobs.os, "pidfd_open", fake_pidfd_open)
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", lambda *args: sent.append(args))
    requested = jobs.cancel_job(running_id)
    assert requested["status"] == "running" and sent and sent[0][1] == signal.SIGTERM
    with pytest.raises(OSError):
        os.fstat(holder["fd"])


def test_jobs_callback_timer_and_stdout_identity_paths(monkeypatch):
    assert jobs._run_callback(lambda *_: ({"ok": True}, 0), {"timeout": 0.01, "queue_timeout": 0.0}, "x") == ({"ok": True}, 0)
    real_fstat = jobs.os.fstat
    monkeypatch.setattr(jobs.os, "fstat", lambda _fd: (_ for _ in ()).throw(OSError("closed")))
    assert jobs._stdout_matches_output((0, 0)) is False
    monkeypatch.setattr(jobs.os, "fstat", real_fstat)
    assert jobs._stdout_matches_output((0, 0)) is False


def test_cli_dispatch_jobs_job_runner_configure_and_main_errors(monkeypatch, tmp_path, capsys):
    args = cli_args(tmp_path, saida=str(tmp_path / "job.out"), attachments=[])
    seen = {}
    monkeypatch.setattr(jobs, "start_job", lambda options, task, output, entrypoint: seen.update(
        {"options": options, "task": task, "output": output, "entrypoint": entrypoint}
    ) or {"id": "a" * 32})
    result, code = cli._dispatch_jobs(args, "task")
    assert code == 0 and result["id"] == "a" * 32 and seen["task"] == "task"
    monkeypatch.setattr(jobs, "read_status", lambda job_id: {"id": job_id, "status": "queued"})
    assert cli._dispatch_jobs(cli_args(tmp_path, status="b" * 32), None)[0]["status"] == "queued"
    monkeypatch.setattr(jobs, "cancel_job", lambda job_id: {"id": job_id, "status": "cancelled"})
    assert cli._dispatch_jobs(cli_args(tmp_path, cancel="c" * 32), None)[0]["status"] == "cancelled"
    with pytest.raises(features.FeatureError):
        cli._dispatch_jobs(cli_args(tmp_path), None)
    runner_args = {"cwd": str(tmp_path), "model": "glm-5.3-flash", "effort": "high"}
    monkeypatch.setattr(cli, "run_task", lambda args, task, **_kwargs: ({"status": "ok", "task": task}, 0))
    assert cli._job_runner(runner_args, "task")[0]["status"] == "ok"
    assert cli.configure(None) is cli.base
    assert cli.main(["--json", "--unknown"]) == 1
    assert cli.main(["--json", "task"]) == 1
    assert "error" in capsys.readouterr().out


def test_jobs_run_child_terminal_callback_outcomes(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_PROC_ROOT", "/proc")
    entrypoint = make_job_entrypoint(tmp_path)
    for task, expected in (("boom", "failed"), ("timeout", "timed_out"), ("cancel", "cancelled"), ("bad-code", "failed")):
        started = jobs.start_job({}, task, str(tmp_path / f"{task}.out"), entrypoint)
        deadline = time.monotonic() + 3
        status = None
        while time.monotonic() < deadline:
            status = jobs.read_status(started["id"])
            if status["status"] in jobs._TERMINAL:
                break
            time.sleep(0.01)
        assert status["status"] == expected
        assert "private callback detail" not in Path(status["output"]).read_text(encoding="utf-8")
