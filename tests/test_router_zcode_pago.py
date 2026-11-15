"""Degrau pago do ZCode: ultimo da cadeia, mesmo binario, pago liberado."""

import json
import sys

import worker_router as wr
from test_worker_router import fake_env as router_fake_env, run_cli

fake_env = router_fake_env


def test_run_wrapper_nao_herda_allow_paid(tmp_path, monkeypatch):
    monkeypatch.setenv("ZCODE_WORKER_ALLOW_PAID", "1")
    argv = [
        sys.executable,
        "-c",
        "import os,sys; sys.stdout.write(os.environ.get('ZCODE_WORKER_ALLOW_PAID','ausente'))",
    ]
    rc, stdout_data, _marker, _timed_out = wr.run_wrapper(argv, str(tmp_path), b"", 10)
    assert rc == 0
    assert stdout_data == b"ausente"
    rc, stdout_data, _marker, _timed_out = wr.run_wrapper(
        argv, str(tmp_path), b"", 10, paid=True
    )
    assert rc == 0
    assert stdout_data == b"1"


def test_pago_so_roda_depois_de_todos(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(
        fake_env, ["--mode", "plan", "task"], {"FAKE_RC": "75", "FAKE_LOG": str(log)}
    )
    assert result.returncode == 75
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names[0] == "zcode-worker"
    assert names[-1] == "zcode-worker"
    assert names.count("zcode-worker") == 2


def test_outro_worker_atende_antes_do_pago(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(
        fake_env,
        ["--mode", "plan", "task"],
        {"FAKE_RC_ZCODE_WORKER": "75", "FAKE_LOG": str(log)},
    )
    assert result.returncode == 0
    names = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert names.count("zcode-worker") == 1
    assert names[-1] != "zcode-worker"


def test_build_argv_do_pago_igual_ao_zcode():
    assert wr.build_argv("zcode-pago", "/x/zcode-worker", "write", "/tmp") == wr.build_argv(
        "zcode-worker", "/x/zcode-worker", "write", "/tmp"
    )
