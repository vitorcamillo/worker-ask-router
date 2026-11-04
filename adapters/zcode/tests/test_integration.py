"""Integração real do candidato, sem rede nem modelo GLM."""

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


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "out" / "zcode-worker-candidate"


@pytest.fixture
def context(tmp_path):
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json, os, sqlite3, sys, time
            from pathlib import Path
            if "--version" in sys.argv:
                print("fake-zcode-cli 1.0")
                raise SystemExit(0)
            if os.environ.get("FAKE_ARGV_LOG"):
                Path(os.environ["FAKE_ARGV_LOG"]).write_text(json.dumps(sys.argv), encoding="utf-8")
            if os.environ.get("FAKE_SLEEP"):
                time.sleep(float(os.environ["FAKE_SLEEP"]))
            config = json.loads(Path(os.environ["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"]).read_text())
            selection = config["defaultModelSelection"]
            db = sqlite3.connect(os.environ["ZCODE_WORKER_CONTEXT_DB"])
            db.execute("create table if not exists session_entry(session_id text, type text, time_created integer, data text)")
            db.execute("insert into session_entry values (?, ?, ?, ?)", ("sess_fake", "runtime/model_selection", int(time.time()), json.dumps({"modelSelection": selection})))
            db.commit()
            db.close()
            print(json.dumps({"sessionId": "sess_fake", "response": "fake response", "status": "success", "usage": {"totalTokens": 1}}))
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
    settings = tmp_path / "settings.json"
    settings.write_text('{"max_parallel": 1}', encoding="utf-8")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 8388608 kB\n", encoding="ascii")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "tests"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "ZCODE_WORKER_CONTEXT_CLI": str(fake_cli),
            "ZCODE_WORKER_CONTEXT_DB": str(tmp_path / "db.sqlite"),
            "ZCODE_WORKER_CONTEXT_PROVIDER_CONFIG": str(provider),
            "ZCODE_WORKER_CONTEXT_STATE": str(tmp_path / "state"),
            "ZCODE_WORKER_CONTEXT_SETTINGS": str(settings),
            "ZCODE_WORKER_CONTEXT_MEMINFO": str(meminfo),
            # jobs precisa verificar o PID real do filho; a admissão continua
            # limitada por settings/meminfo fixtures abaixo.
            "ZCODE_WORKER_CONTEXT_PROC_ROOT": str(proc_root),
        }
    )
    return {"env": env, "tmp": tmp_path, "meminfo": meminfo}


def run_candidate(context, *args, env_extra=None, input_text=None):
    env = dict(context["env"])
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(CANDIDATE), *args],
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )


def _json(completed):
    assert completed.stdout, completed.stderr
    return json.loads(completed.stdout)


def _wait_status(context, job_id, wanted, timeout=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _json(run_candidate(context, "--json", "--status", job_id))
        if last["status"] in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"status não alcançado: {last}")


def test_candidate_foreground_and_private_task_not_in_job_argv(context):
    foreground = run_candidate(
        context, "--json", "--cwd", str(context["tmp"]), "tarefa pública"
    )
    assert foreground.returncode == 0, foreground.stderr
    assert _json(foreground)["status"] == "ok"

    output = context["tmp"] / "background.json"
    secret = "tarefa privada que não pode ir no argv do job"
    started = run_candidate(
        context,
        "--json",
        "--cwd",
        str(context["tmp"]),
        "--saida",
        str(output),
        secret,
        env_extra={"FAKE_SLEEP": "1"},
    )
    assert started.returncode == 0, started.stderr
    job = _json(started)
    job_id = job["id"]
    running = _wait_status(context, job_id, {"running", "queued"})
    if running.get("pid"):
        cmdline = Path(f"/proc/{running['pid']}/cmdline").read_bytes().decode("utf-8", "replace")
        assert secret not in cmdline
    cancelled = run_candidate(context, "--json", "--cancel", job_id)
    assert cancelled.returncode == 0, cancelled.stderr
    assert _wait_status(context, job_id, {"cancelled", "completed", "failed"})["status"] == "cancelled"


def test_candidate_rejects_dry_run_output_without_running_job(context):
    output = context["tmp"] / "must-not-exist"
    log = context["tmp"] / "fake-argv.json"
    completed = run_candidate(
        context,
        "--json",
        "--dry-run",
        "--saida",
        str(output),
        "--cwd",
        str(context["tmp"]),
        "tarefa",
        env_extra={"FAKE_ARGV_LOG": str(log)},
    )
    assert completed.returncode == 1
    assert _json(completed)["error"]["code"] == "invalid_request"
    assert not output.exists()
    assert not log.exists()


def test_candidate_doctor_defaults_and_memory_admission_guard(context):
    doctor = run_candidate(context, "--json", "--doctor")
    report = _json(doctor)
    assert doctor.returncode == 0
    assert report["max_parallel"] == 1
    assert report["memory"] == "adequada"

    context["meminfo"].write_text("MemAvailable: 1024 kB\n", encoding="ascii")
    guarded = run_candidate(
        context,
        "--json",
        "--timeout",
        "0.15",
        "--queue-timeout",
        "0.15",
        "--cwd",
        str(context["tmp"]),
        "não iniciar",
    )
    assert guarded.returncode == 75
    assert _json(guarded)["error"]["code"] == "quota"


def test_candidate_uses_fixed_src_and_legacy_core_child(context, tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "zcode_worker").mkdir()
    (shadow / "zcode_worker" / "__init__.py").write_text("raise RuntimeError('shadow')", encoding="utf-8")
    env_extra = {"PYTHONPATH": str(shadow)}
    listed = run_candidate(context, "--json", "--list-models", env_extra=env_extra)
    assert listed.returncode == 0
    assert _json(listed)["status"] == "models"

    legacy = run_candidate(context, "--_background-child", "--list-models")
    assert legacy.returncode == 0
    assert "glm-5.3-flash" in legacy.stdout
    assert "exit=0" in legacy.stdout
