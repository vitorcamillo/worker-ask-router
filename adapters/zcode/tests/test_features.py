import json
import os
import sqlite3
import stat
import sys
from contextlib import contextmanager
from argparse import Namespace
from pathlib import Path

import pytest

from zcode_worker import cli, features


def args_for(**overrides):
    values = dict(
        tarefa=None,
        cwd=os.getcwd(),
        mode="plan",
        spec=None,
        timeout=10.0,
        queue_timeout=10.0,
        total_timeout=None,
        model="glm-5.3-flash",
        effort="high",
        profile=None,
        resume=None,
        attach=[],
        disallowed_tools=[],
        max_output_bytes=None,
        json=False,
        dry_run=False,
        doctor=False,
        list_models=False,
        saida=None,
        status=None,
        cancel=None,
    )
    values.update(overrides)
    return Namespace(**values)


def test_profiles_and_explicit_overrides():
    assert features.selection_for("rapido", None, None) == (
        "glm-5.3-flash", "low"
    )
    assert features.selection_for("execucao", None, None) == (
        "glm-5.3-flash", "high"
    )
    # Start Plan só tem entitlement de GLM-5.3-Flash; "raciocinio" vira o
    # mesmo modelo com o effort mais alto.
    assert features.selection_for("raciocinio", None, None) == (
        "glm-5.3-flash", "max"
    )
    assert features.selection_for("rapido", "glm-5.3", "max") == (
        "glm-5.3", "max"
    )
    assert features.selection_for(None, None, None) == ("glm-5.3-flash", "high")


@pytest.mark.parametrize("value", ["0", "nan", "inf", "-1", "not-a-number"])
def test_invalid_deadlines_are_rejected(value):
    with pytest.raises(features.FeatureError):
        features.parse_args(["tarefa", "--timeout", value])


def test_response_validation_is_strict_and_normalizes_missing_usage():
    valid = features.validate_response(
        {"sessionId": "sess_abc-1", "response": "ok", "status": "success"}
    )
    assert valid["usage"] == {}
    assert valid["sessionId"] == "sess_abc-1"
    native_usage = {"source": "provider", "modelRequestCount": 1, "inputTokens": 3, "outputTokens": 2, "totalTokens": 5, "cacheReadTokens": 0, "cacheWriteTokens": 0, "reasoningTokens": 0, "webFetchRequests": 0, "webSearchRequests": 0}
    assert features.validate_response({"sessionId": "sess_native", "response": "OK", "usage": native_usage})["usage"] == native_usage
    for payload in (
        None,
        [],
        {"sessionId": "sess_x", "response": "   "},
        {"sessionId": "sess_x", "response": None},
        {"sessionId": "sess_x", "response": "ok", "usage": {"totalTokens": True}},
        {"sessionId": "sess_x", "response": "ok", "usage": {"totalTokens": -1}},
        {"sessionId": "sess_x", "response": "ok", "usage": {"unknown": 1}},
        {"sessionId": "sess_x", "response": "ok", "usage": None},
        {"sessionId": "sess_x", "response": "ok", "projection": {"status": "paused"}},
        {"sessionId": "sess_x", "response": "ok", "projection": "error"},
        {"sessionId": "sess_x", "response": "ok", "status": "error"},
    ):
        with pytest.raises(features.FeatureError):
            features.validate_response(payload)


def test_decode_allows_diagnostics_but_not_ambiguous_json():
    payload = {"sessionId": "sess_ok", "response": "resposta"}
    encoded = "diagnostic: iniciando\n" + json.dumps(payload) + "\n"
    assert features.decode_response(encoded) == payload
    with pytest.raises(features.FeatureError):
        features.decode_response(json.dumps(payload) + "\n{}")
    with pytest.raises(features.FeatureError):
        features.decode_response("texto arbitrário\n" + json.dumps(payload))


def test_selection_and_resume_are_read_only_and_compare_effort(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table session_entry(session_id text, type text, time_created integer, data text)")
    selection = {
        "modelSelection": {
            "providerId": features.PROVIDER_ID,
            "modelId": "GLM-5.3-Flash",
            "options": {"reasoningLevel": "high"},
        }
    }
    conn.execute(
        "insert into session_entry values (?, ?, ?, ?)",
        ("sess_resume", "runtime/model_selection", 1, json.dumps(selection)),
    )
    conn.commit()
    conn.close()
    os.chmod(db, stat.S_IRUSR | stat.S_IWUSR)

    found = features.effective_selection("sess_resume", db_path=db)
    assert found == {
        "providerId": features.PROVIDER_ID,
        "modelId": "GLM-5.3-Flash",
        "effort": "high",
    }
    assert features.verify_effective(
        {"sessionId": "sess_resume", "effective": found}, "glm-5.3-flash", "high", db_path=db
    ) == found
    assert features.verify_effective(
        {
            "sessionId": "sess_resume",
            "effective": {
                "providerId": features.PROVIDER_ID,
                "modelId": "GLM-5.3-Flash",
                "effort": "low",
            },
        },
        "glm-5.3-flash",
        "high",
        db_path=db,
    ) == found
    with pytest.raises(features.FeatureError):
        features.verify_effective(
            {"sessionId": "sess_resume", "effective": found},
            "glm-5.3-flash",
            "low",
            db_path=db,
        )
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(tmp_path / "missing.json"))
    assert not (tmp_path / "provider.json").exists()


def test_temp_provider_is_private_and_does_not_modify_source(tmp_path, monkeypatch):
    source = tmp_path / "provider.json"
    original = {
        "defaultModelSelection": {
            "providerId": features.PROVIDER_ID,
            "modelId": "GLM-5.3-Flash",
            "options": {"reasoningLevel": "low"},
        }
    }
    source.write_text(json.dumps(original), encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(source))
    with features.temporary_provider("glm-5.3", "high", state_dir=state) as path:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert json.loads(Path(path).read_text(encoding="utf-8"))["defaultModelSelection"]["modelId"] == "GLM-5.3"
    assert not Path(path).exists()
    assert json.loads(source.read_text(encoding="utf-8")) == original


def test_argv_resume_attach_and_mode_specific_denylist(monkeypatch, tmp_path):
    monkeypatch.setattr(features.base, "_headless_tools", lambda: ["Bash", "WebFetch", "mcp__x"])
    attachment = tmp_path / "a.txt"
    attachment.write_text("anexo", encoding="utf-8")
    argv = features.build_cli_argv(
        "tarefa",
        "/tmp/work",
        "edit",
        "glm-5.3-flash",
        "high",
        resume="sess_1",
        attachments=[str(attachment)],
        disallowed_tools=["mcp__y"],
        cli_path="/bin/zcode-cli",
    )
    assert argv[:2] == ["/bin/zcode-cli", "-p"]
    assert ["--resume", "sess_1"] == argv[argv.index("--resume") : argv.index("--resume") + 2]
    assert argv.count("--attach") == 1
    assert "mcp__x" in argv and "mcp__y" in argv
    plan = features.build_cli_argv(
        "tarefa", "/tmp/work", "plan", "glm-5.3-flash", "high", disallowed_tools=["mcp__y"]
    )
    assert "Bash" not in plan and "mcp__y" in plan
    yolo = features.build_cli_argv(
        "tarefa", "/tmp/work", "yolo", "glm-5.3-flash", "high"
    )
    assert "--disallowed-tools" not in yolo


def test_attachments_and_denylist_reject_dangerous_inputs(tmp_path):
    safe = tmp_path / "note.txt"
    safe.write_text("ok", encoding="utf-8")
    assert features.validate_attachments([str(safe)]) == [str(safe.resolve())]
    with pytest.raises(features.FeatureError):
        features.validate_attachments([str(tmp_path / ".env")])
    with pytest.raises(features.FeatureError):
        features.validate_denylist(["Bash", "bad*pattern"])
    assert features.validate_denylist(["mcp__x,mcp__x", "Tool-1"]) == ["mcp__x", "Tool-1"]


def test_dry_run_validates_without_running_cli(tmp_path, monkeypatch):
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("CLI não deveria ser chamado")

    monkeypatch.setattr(cli, "_invoke", forbidden)
    args = args_for(cwd=str(tmp_path), dry_run=True)
    result, rc = cli.run_task(args, "tarefa seca")
    assert rc == 0
    assert result["status"] == "dry-run"
    assert result["response"] == "não executado"
    assert called is False


def test_engine_retries_only_plan_and_returns_safe_schema(tmp_path, monkeypatch):
    args = args_for(cwd=str(tmp_path), timeout=2.0, queue_timeout=2.0)
    calls = []

    class Slot:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: Slot())
    @contextmanager
    def fake_provider(_model, _effort, **_kwargs):
        yield str(tmp_path / "provider.json")

    monkeypatch.setattr(cli.features, "temporary_provider", fake_provider)
    monkeypatch.setattr(cli.base, "RETRY_DELAYS", (0.0,))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    def fake_invoke(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return 429, "", "rate limit"
        return 0, json.dumps({
            "sessionId": "sess_ok",
            "response": "feito",
            "usage": {"totalTokens": 3},
            "effective": {
                "providerId": features.PROVIDER_ID,
                "modelId": "GLM-5.3-Flash",
                "effort": "high",
            },
        }), ""

    monkeypatch.setattr(cli, "_invoke", fake_invoke)
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table session_entry(session_id text, type text, time_created integer, data text)")
    conn.execute(
        "insert into session_entry values (?, ?, ?, ?)",
        (
            "sess_ok",
            "runtime/model_selection",
            1,
            json.dumps({
                "modelSelection": {
                    "providerId": features.PROVIDER_ID,
                    "modelId": "GLM-5.3-Flash",
                    "options": {"reasoningLevel": "high"},
                }
            }),
        ),
    )
    conn.commit()
    conn.close()
    result, rc = cli.run_task(args, "tarefa", db_path=db)
    assert rc == 0
    assert len(calls) == 2
    assert result["response"] == "feito"
    assert result["usage"] == {"totalTokens": 3}

    calls.clear()
    args.mode = "edit"
    result, rc = cli.run_task(args, "tarefa", db_path=db)
    assert rc == 1
    assert len(calls) == 1
    assert result["status"] == "error"
    assert "rate limit" not in json.dumps(result).lower()


@pytest.mark.parametrize("version_output", ["zcode-cli 1.2.3\n", "1.2.3\n"])
def test_doctor_is_bounded_and_does_not_call_model(monkeypatch, tmp_path, version_output):
    monkeypatch.setattr(cli.base, "CLI", str(tmp_path / "zcode-cli"))

    class Completed:
        returncode = 0
        stdout = version_output
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: Completed())
    report = cli.doctor()
    assert report["status"] == "doctor"
    assert report["cli"]["version"] == "1.2.3"
    assert "provider_config" not in json.dumps(report)


def test_job_dispatch_uses_whitelist_and_absolute_launcher(tmp_path, monkeypatch):
    from zcode_worker import jobs

    captured = {}

    def fake_start(options, task, output, entrypoint):
        captured.update(options=options, task=task, output=output, entrypoint=entrypoint)
        return {"id": "a" * 32, "status": "queued", "pid": 123, "output": output, "exitCode": None}

    monkeypatch.setattr(jobs, "start_job", fake_start)
    args = args_for(cwd=str(tmp_path), saida=str(tmp_path / "job.out"), profile=None)
    result, rc = cli._dispatch_jobs(args, "private task")
    assert rc == 0
    assert result["status"] == "queued"
    assert captured["task"] == "private task"
    assert captured["options"]["cwd"] == str(tmp_path)
    assert None not in captured["options"].values()
    assert captured["entrypoint"][0] == sys.executable
    assert Path(captured["entrypoint"][1]).name == "zcode-worker-next"
