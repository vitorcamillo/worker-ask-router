"""Testes públicos da superfície CLI e das garantias de isolamento locais."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from zcode_worker import features, jobs


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "zcode-worker-next"


@pytest.fixture
def fake_context(tmp_path):
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sqlite3
            import sys
            import time
            from pathlib import Path

            if "--version" in sys.argv:
                print("fake-zcode-cli 1.0")
                raise SystemExit(0)
            log = os.environ.get("FAKE_ARGV_LOG")
            if log:
                Path(log).write_text(json.dumps(sys.argv), encoding="utf-8")
            if os.environ.get("FAKE_SLEEP"):
                time.sleep(float(os.environ["FAKE_SLEEP"]))
            if os.environ.get("FAKE_LARGE"):
                sys.stdout.write("x" * int(os.environ["FAKE_LARGE"]))
                sys.stdout.flush()
            config = json.loads(Path(os.environ["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"]).read_text())
            selection = config["defaultModelSelection"]
            if os.environ.get("FAKE_MISMATCH"):
                selection = dict(selection)
                selection["modelId"] = "GLM-5.3"
                selection["options"] = {"reasoningLevel": "low"}
            db = os.environ["ZCODE_WORKER_CONTEXT_DB"]
            connection = sqlite3.connect(db)
            connection.execute(
                "create table if not exists session_entry(session_id text, type text, time_created integer, data text)"
            )
            connection.execute(
                "insert into session_entry values (?, ?, ?, ?)",
                ("sess_fake", "runtime/model_selection", int(time.time()), json.dumps({"modelSelection": selection})),
            )
            connection.commit()
            connection.close()
            print(json.dumps({
                "sessionId": "sess_fake",
                "response": "fake response",
                "status": "success",
                "usage": {"totalTokens": 3},
                "effective": {
                    "providerId": selection["providerId"],
                    "modelId": selection["modelId"],
                    "effort": selection["options"]["reasoningLevel"],
                },
            }))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IXUSR)
    provider = tmp_path / "provider.json"
    provider.write_text(
        json.dumps(
            {
                "defaultModelSelection": {
                    "providerId": "account:zai-individual-coding-plan",
                    "modelId": "GLM-5.3-Flash",
                    "options": {"reasoningLevel": "high"},
                }
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "db.sqlite"
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text('{"max_parallel": 1}', encoding="utf-8")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 8388608 kB\n", encoding="ascii")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "ZCODE_WORKER_CONTEXT_CLI": str(fake_cli),
            "ZCODE_WORKER_CONTEXT_DB": str(db),
            "ZCODE_WORKER_CONTEXT_PROVIDER_CONFIG": str(provider),
            "ZCODE_WORKER_CONTEXT_STATE": str(state),
            "ZCODE_WORKER_CONTEXT_SETTINGS": str(settings),
            "ZCODE_WORKER_CONTEXT_MEMINFO": str(meminfo),
            "ZCODE_WORKER_CONTEXT_PROC_ROOT": str(proc_root),
        }
    )
    return {"env": env, "tmp": tmp_path, "db": db, "state": state}


def run_cli(context, *args, input_text=None, extra_env=None):
    env = dict(context["env"])
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )


def test_cli_foreground_stdin_json(fake_context):
    completed = run_cli(
        fake_context,
        "--json",
        "--mode",
        "plan",
        "--cwd",
        str(fake_context["tmp"]),
        input_text="tarefa pública\n",
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["response"] == "fake response"
    assert payload["effective"]["effort"] == "high"
    assert "tarefa pública" not in completed.stdout + completed.stderr


@pytest.mark.acceptance
def test_cli_rejects_effective_selection_mismatch(fake_context):
    completed = run_cli(
        fake_context,
        "--json",
        "--cwd",
        str(fake_context["tmp"]),
        input_text="tarefa",
        extra_env={"FAKE_MISMATCH": "1"},
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_request"
    assert "GLM-5.3" not in completed.stdout


@pytest.mark.acceptance
def test_cli_dry_run_doctor_and_argument_policy(fake_context):
    dry = run_cli(
        fake_context,
        "--json",
        "--dry-run",
        "--cwd",
        str(fake_context["tmp"]),
        input_text="não executar",
    )
    assert dry.returncode == 0
    assert json.loads(dry.stdout)["status"] == "dry-run"

    doctor = run_cli(fake_context, "--json", "--doctor")
    assert doctor.returncode == 0
    assert json.loads(doctor.stdout)["status"] == "doctor"

    log = fake_context["tmp"] / "argv.json"
    executed = run_cli(
        fake_context,
        "--json",
        "--mode",
        "edit",
        "--cwd",
        str(fake_context["tmp"]),
        "--disallowed-tools",
        "mcp__test",
        input_text="argv",
        extra_env={"FAKE_ARGV_LOG": str(log)},
    )
    assert executed.returncode == 0, executed.stderr
    argv = json.loads(log.read_text(encoding="utf-8"))
    assert "--json" in argv and "--no-color" in argv
    assert "zcode-worker-next" not in " ".join(argv)
    assert "--disallowed-tools" in argv


@pytest.mark.acceptance
def test_cli_timeout_and_output_limit_are_safe(fake_context):
    timeout = run_cli(
        fake_context,
        "--json",
        "--timeout",
        "0.2",
        "--queue-timeout",
        "1",
        "--cwd",
        str(fake_context["tmp"]),
        input_text="timeout",
        extra_env={"FAKE_SLEEP": "2"},
    )
    assert timeout.returncode == 124
    assert json.loads(timeout.stdout)["error"]["code"] == "timeout"

    limited = run_cli(
        fake_context,
        "--json",
        "--max-output-bytes",
        "64",
        "--cwd",
        str(fake_context["tmp"]),
        input_text="output",
        extra_env={"FAKE_LARGE": "256"},
    )
    assert limited.returncode == 1
    assert json.loads(limited.stdout)["error"]["code"] == "output_limit"


@pytest.mark.qa
def test_adversarial_attachments_denylist_and_json_are_rejected(tmp_path):
    secret = tmp_path / ".env.local"
    secret.write_text("not a credential", encoding="utf-8")
    regular = tmp_path / "note.txt"
    regular.write_text("ok", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(regular)
    allowed = []
    for index in range(5):
        item = tmp_path / f"note-{index}.txt"
        item.write_text("ok", encoding="utf-8")
        allowed.append(str(item))
    assert len(features.validate_attachments(allowed)) == 5
    sixth = tmp_path / "note-5.txt"
    sixth.write_text("ok", encoding="utf-8")
    with pytest.raises(features.FeatureError):
        features.validate_attachments([str(secret)])
    with pytest.raises(features.FeatureError):
        features.validate_attachments([str(link)])
    with pytest.raises(features.FeatureError):
        features.validate_attachments([*allowed, str(sixth)])
    with pytest.raises(features.FeatureError):
        features.validate_denylist(["Bash *"])
    with pytest.raises(features.FeatureError):
        features.decode_response('{"sessionId":"sess_x","response":"ok"}\n{}')


@pytest.mark.qa
def test_private_job_rejects_traversal_and_secret_options(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(jobs.base, "STATE", str(state))
    jobs.configure(jobs.base)
    with pytest.raises(jobs.base.WorkerError):
        jobs.read_status("../job")
    with pytest.raises(jobs.base.WorkerError):
        jobs._validate_options({"api_key": "redacted"})


def _child_entrypoint(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/python3
            import sys
            import time
            from zcode_worker import jobs

            def runner(options, task):
                if task == "sleep":
                    time.sleep(30)
                return {"response": "ok", "length": len(task)}, 0

            if __name__ == "__main__":
                raise SystemExit(jobs.run_child(sys.argv[-1], runner))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return [str(script)]


@pytest.mark.e2e
def test_private_background_job_status_cancel_and_cleanup(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(jobs.base, "STATE", str(state))
    jobs.configure(jobs.base)
    output = tmp_path / "job.jsonl"
    started = jobs.start_job({}, "sleep", str(output), _child_entrypoint(tmp_path))
    job_dir = state / "jobs" / started["id"]
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and jobs.read_status(started["id"])["status"] != "running":
        time.sleep(0.02)
    requested = jobs.cancel_job(started["id"])
    assert requested["id"] == started["id"]
    while time.monotonic() < deadline:
        status = jobs.read_status(started["id"])
        if status["status"] in {"cancelled", "lost"}:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"job não terminou após cancelamento: {status}")
    assert not (job_dir / "input.json").exists()
    assert output.exists()
    assert "sleep" not in output.read_text(encoding="utf-8")
