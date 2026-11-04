"""Testes das melhorias de conversa: turnos, eventos, parada em dois estágios e bridge MCP.

Nenhum teste toca rede ou modelos; processos são filhos locais criados em
``tmp_path`` e o orquestrador simulado escreve arquivos privados do job.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import warnings
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zcode_worker import base, bridge, cli, features, jobs


ROOT = Path(__file__).resolve().parents[1]
BIN_BRIDGE = ROOT / "bin" / "worker-bridge"


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(base, "STATE", str(state))
    monkeypatch.setattr(base, "PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setattr(jobs, "_PROC_ROOT", str(tmp_path / "proc"))
    jobs.configure(base)
    yield state
    jobs.configure(base)


def feature_args(**overrides):
    values = {
        "tarefa": None,
        "cwd": os.getcwd(),
        "mode": "plan",
        "spec": None,
        "timeout": 5.0,
        "queue_timeout": 5.0,
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
        "conversation": False,
        "doctor": False,
        "list_models": False,
        "saida": None,
        "status": None,
        "cancel": None,
        "_job_child": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_provider(tmp_path: Path, monkeypatch):
    provider = tmp_path / "provider.json"
    provider.write_text(
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
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "cli-state"))


def fake_cli_payload(response_text: str) -> str:
    return json.dumps(
        {
            "sessionId": "sess_fake123",
            "response": response_text,
            "status": "success",
            "usage": {"totalTokens": 1},
        }
    )


NEED_INPUT_BLOCK = (
    "resultado parcial seguro\n" + features.TURN_MARKER + '\n{"question": "qual caminho?", "options": ["a", "b"]}'
)


# ---------------------------------------------------------------------------
# features.parse_turn_request
# ---------------------------------------------------------------------------


def test_parse_turn_request_accepts_valid_requests():
    parsed = features.parse_turn_request(NEED_INPUT_BLOCK)
    assert parsed == {"question": "qual caminho?", "options": ["a", "b"]}
    minimal = "texto\n" + features.TURN_MARKER + '\n{"question": "seguir?"}'
    assert features.parse_turn_request(minimal) == {"question": "seguir?", "options": []}
    assert features.parse_turn_request("resposta comum sem marcador") is None
    assert features.parse_turn_request(None) is None


@pytest.mark.parametrize(
    "response",
    [
        "x\n" + features.TURN_MARKER,
        "x\n" + features.TURN_MARKER + "\nisto não é json",
        "x\n" + features.TURN_MARKER + '\n["lista"]',
        "x\n" + features.TURN_MARKER + '\n{"question": "  "}',
        "x\n" + features.TURN_MARKER + '\n{"question": "' + "q" * (features.TURN_MAX_QUESTION_BYTES + 1) + '"}',
        "x\n" + features.TURN_MARKER + '\n{"question": "q", "options": "nao-lista"}',
        "x\n" + features.TURN_MARKER + '\n{"question": "q", "options": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]}',
        "x\n" + features.TURN_MARKER + '\n{"question": "q", "options": [""]}',
        "x\n" + features.TURN_MARKER + '\n{"question": "q", "options": ["' + "o" * 1025 + '"]}',
    ],
)
def test_parse_turn_request_rejects_malformed_requests(response):
    with pytest.raises(features.FeatureError, match="instrução"):
        features.parse_turn_request(response)


def test_parse_args_and_namespace_carry_conversation(tmp_path):
    parsed = features.parse_args(["--cwd", str(tmp_path), "--conversation", "task"])
    assert parsed.conversation is True
    normalized = features.public_namespace(parsed)
    assert normalized.conversation is True
    plain = features.public_namespace(feature_args(cwd=str(tmp_path)))
    assert plain.conversation is False


def test_build_cli_argv_appends_protocol_only_when_opted_in(tmp_path):
    argv_plain = features.build_cli_argv("task", str(tmp_path), "plan", "glm-5.3-flash", "high")
    argv_conv = features.build_cli_argv(
        "task", str(tmp_path), "plan", "glm-5.3-flash", "high", conversation=True
    )
    assert features.TURN_MARKER not in argv_plain[2]
    assert features.TURN_MARKER in argv_conv[2]


# ---------------------------------------------------------------------------
# cli.run_task: status need_input com exit code 2
# ---------------------------------------------------------------------------


def test_run_task_reports_need_input_with_exit_code_2(tmp_path, monkeypatch):
    make_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: contextlib.nullcontext())
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"modelId": "ok"})
    monkeypatch.setattr(cli, "_invoke", lambda *a, **k: (0, fake_cli_payload(NEED_INPUT_BLOCK), ""))
    args = feature_args(cwd=str(tmp_path), conversation=True)
    result, code = cli.run_task(args, "task")
    assert code == 2
    assert result["status"] == "need_input"
    assert result.get("pending") == {"question": "qual caminho?", "options": ["a", "b"]}
    assert features.TURN_MARKER not in result["response"]
    assert result["turnResume"] == "sess_fake123"


def test_run_task_keeps_marker_as_plain_text_without_flag(tmp_path, monkeypatch):
    make_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: contextlib.nullcontext())
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"modelId": "ok"})
    monkeypatch.setattr(cli, "_invoke", lambda *a, **k: (0, fake_cli_payload(NEED_INPUT_BLOCK), ""))
    result, code = cli.run_task(feature_args(cwd=str(tmp_path)), "task")
    assert code == 0 and result["status"] == "ok"
    assert features.TURN_MARKER in result["response"]
    assert "pending" not in result


def test_run_task_rejects_malformed_turn_request(tmp_path, monkeypatch):
    make_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: contextlib.nullcontext())
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"modelId": "ok"})
    broken = "x\n" + features.TURN_MARKER + "\n{json inválido}"
    monkeypatch.setattr(cli, "_invoke", lambda *a, **k: (0, fake_cli_payload(broken), ""))
    result, code = cli.run_task(feature_args(cwd=str(tmp_path), conversation=True), "task")
    assert code == 1 and result["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Streaming de eventos: on_chunk e events.jsonl do job
# ---------------------------------------------------------------------------


def test_chunk_sink_counts_bytes_per_stream():
    events = []
    sink = cli._chunk_sink(events.append)
    sink("stdout", 128)
    sink("stderr", 32)
    assert events == [
        {"kind": "cli_output", "stream": "stdout", "bytes": 128},
        {"kind": "cli_output", "stream": "stderr", "bytes": 32},
    ]
    assert cli._chunk_sink(None) is None


def test_invoke_limited_emits_chunks_without_content(monkeypatch):
    events = []
    sink = cli._chunk_sink(events.append)
    code, stdout, _stderr = cli._invoke(
        [sys.executable, "-c", "print('linha um'); print('linha dois')"],
        dict(os.environ),
        time.monotonic() + 10,
        on_chunk=sink,
    )
    assert code == 0 and "linha dois" in stdout
    total = sum(event["bytes"] for event in events if event["stream"] == "stdout")
    assert total == len(stdout.encode())
    assert all(set(event) == {"kind", "stream", "bytes"} for event in events)


def prepare_running_job(state: Path, tmp_path: Path, job_id: str, options: dict | None = None):
    directory = Path(jobs._job_dir(job_id, create=True))
    output = tmp_path / f"{job_id}.out"
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
    payload_options = {"cwd": str(tmp_path), "conversation": True}
    payload_options.update(options or {})
    jobs._atomic_write(
        jobs._input_path(str(directory)),
        {"options": payload_options, "task": "task"},
        limit=200000,
    )
    return directory


def test_run_child_records_turn_events_inprocess(isolated_state, tmp_path, monkeypatch):
    make_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: contextlib.nullcontext())
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"modelId": "ok"})
    monkeypatch.setattr(cli, "_invoke", lambda *a, **k: (0, fake_cli_payload("ok simples"), ""))
    job_id = "1" * 32
    prepare_running_job(isolated_state, tmp_path, job_id)
    code = jobs.run_child(job_id, cli._job_runner)
    assert code == 0
    events = [
        json.loads(line)
        for line in (Path(jobs._job_dir(job_id)) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "turn_start" and kinds[-1] == "turn_end"
    assert events[0]["conversation"] is True
    assert events[-1] == {"kind": "turn_end", "status": "ok", "exitCode": 0, "ts": events[-1]["ts"]}
    assert "task" not in json.dumps(events)


def test_run_child_records_error_and_need_input_events(isolated_state, tmp_path, monkeypatch):
    make_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: contextlib.nullcontext())
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"modelId": "ok"})

    error_id = "2" * 32
    prepare_running_job(isolated_state, tmp_path, error_id)
    monkeypatch.setattr(cli, "_invoke", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert jobs.run_child(error_id, cli._job_runner) == 1
    events = (Path(jobs._job_dir(error_id)) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(events[-1])["status"] == "error"

    need_id = "3" * 32
    prepare_running_job(isolated_state, tmp_path, need_id)
    monkeypatch.setattr(cli, "_invoke", lambda *a, **k: (0, fake_cli_payload(NEED_INPUT_BLOCK), ""))
    assert jobs.run_child(need_id, cli._job_runner) == 2
    events = (Path(jobs._job_dir(need_id)) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(events[-1])["status"] == "need_input"
    public_output = Path(jobs.read_status(need_id)["output"]).read_text(encoding="utf-8")
    assert "exit=2" in public_output


def test_append_event_validates_shape_and_limits(isolated_state, tmp_path):
    events_path = str(tmp_path / "events.jsonl")
    jobs._append_event(events_path, {"kind": "progress", "stage": "build"})
    assert '"stage":"build"' in Path(events_path).read_text(encoding="utf-8")
    jobs._append_event(events_path, {"stage": "sem kind"})
    jobs._append_event(events_path, {"kind": "grande", "blob": "x" * 5000})
    jobs._append_event(events_path, {"kind": "nan", "value": float("nan")})
    assert len(Path(events_path).read_text(encoding="utf-8").splitlines()) == 1
    stat_line = Path(events_path).stat()
    assert stat.S_IMODE(stat_line.st_mode) == 0o600

    full = tmp_path / "full.jsonl"
    with full.open("wb") as handle:
        handle.write(b"x" * jobs._EVENTS_MAX_BYTES)
    full.chmod(0o600)
    jobs._append_event(str(full), {"kind": "progress"})
    marker_size = len(b'{"kind":"events_truncated"}\n')
    assert full.stat().st_size == jobs._EVENTS_MAX_BYTES + marker_size
    assert full.read_bytes().endswith(b'{"kind":"events_truncated"}\n')
    # A segunda tentativa não cresce além do marcador.
    jobs._append_event(str(full), {"kind": "progress"})
    assert full.stat().st_size == jobs._EVENTS_MAX_BYTES + marker_size


def test_current_event_sink_is_none_outside_job(isolated_state):
    original = jobs._CURRENT_EVENT_SINK
    jobs._CURRENT_EVENT_SINK = None
    try:
        assert jobs.current_event_sink() is None
    finally:
        jobs._CURRENT_EVENT_SINK = original


def test_jobs_options_accept_conversation_flag(isolated_state, tmp_path):
    assert jobs._validate_options({"conversation": True, "cwd": str(tmp_path)})["conversation"] is True
    with pytest.raises(base.WorkerError, match="conversa"):
        jobs._validate_options({"conversation": "sim"})


# ---------------------------------------------------------------------------
# Cancelamento em dois estágios
# ---------------------------------------------------------------------------


def child_script(tmp_path: Path, name: str, body: str) -> list:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    script.chmod(0o700)
    return [str(Path(sys.executable).resolve()), str(script)]


def wait_status(job_id: str, wanted: set, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = jobs.read_status(job_id)
        if last["status"] in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"status não alcançado: {last}")


def test_cancel_two_stage_forces_kill_on_stubborn_process(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_PROC_ROOT", "/proc")
    entrypoint = child_script(
        tmp_path,
        "stubborn.py",
        "import signal, sys, time\n"
        "sys.path.insert(0, %r)\n" % str(ROOT / "src")
        + "from zcode_worker import jobs\n"
        "def runner(_o, _t):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(60)\n"
        "    return {'status': 'ok'}, 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(jobs.run_child(sys.argv[-1], runner))\n",
    )
    started = jobs.start_job({}, "task", str(tmp_path / "stubborn.out"), entrypoint)
    wait_status(started["id"], {"running"})
    began = time.monotonic()
    cancelled = jobs.cancel_job(started["id"])
    elapsed = time.monotonic() - began
    assert cancelled.get("killForced") is True
    assert elapsed >= jobs._CANCEL_KILL_GRACE
    final = wait_status(started["id"], jobs._TERMINAL)
    assert final["status"] == "lost"


def test_cancel_two_stage_cooperative_stays_fast(isolated_state, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_PROC_ROOT", "/proc")
    entrypoint = child_script(
        tmp_path,
        "cooperative.py",
        "import sys, time\n"
        "sys.path.insert(0, %r)\n" % str(ROOT / "src")
        + "from zcode_worker import jobs\n"
        "def runner(_o, _t):\n"
        "    time.sleep(60)\n"
        "    return {'status': 'ok'}, 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(jobs.run_child(sys.argv[-1], runner))\n",
    )
    started = jobs.start_job({}, "task", str(tmp_path / "coop.out"), entrypoint)
    wait_status(started["id"], {"running"})
    began = time.monotonic()
    cancelled = jobs.cancel_job(started["id"])
    elapsed = time.monotonic() - began
    assert "killForced" not in cancelled
    assert elapsed < jobs._CANCEL_KILL_GRACE
    final = wait_status(started["id"], {"cancelled"})
    assert final["exitCode"] == 128 + signal.SIGTERM


# ---------------------------------------------------------------------------
# Bridge MCP: protocolo stdio e troca de mensagens com o orquestrador
# ---------------------------------------------------------------------------


class BridgeClient:
    def __init__(self, env: dict, tmp: Path):
        self.tmp = tmp
        self.process = subprocess.Popen(
            [sys.executable, str(BIN_BRIDGE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=str(ROOT),
        )

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict:
        line = self.process.stdout.readline()
        assert line, "bridge encerrou sem resposta"
        return json.loads(line)

    def call(self, message: dict) -> dict:
        self.send(message)
        return self.receive()

    def close(self):
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        self.process.wait(timeout=5)


@pytest.fixture
def bridge_env(isolated_state, tmp_path, monkeypatch):
    job_id = "4" * 32
    prepare_running_job(isolated_state, tmp_path, job_id)
    env = dict(os.environ)
    env.update(
        {
            "ZCODE_WORKER_JOB_ID": job_id,
            "ZCODE_WORKER_CONTEXT_STATE": str(isolated_state),
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    return {"env": env, "job_id": job_id, "directory": Path(jobs._job_dir(job_id))}


def test_bridge_handshake_and_progress(bridge_env):
    client = BridgeClient(bridge_env["env"], bridge_env["directory"].parent)
    try:
        init = client.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init["result"]["serverInfo"]["name"] == "worker-bridge"
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = client.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "ask_orchestrator", "report_progress", "request_stop"
        }
        progress = client.call({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "report_progress", "arguments": {"stage": "build", "detail": "metade"}},
        })
        assert progress["result"]["content"][0]["text"] == "progress registrado"
        events = (bridge_env["directory"] / "events.jsonl").read_text(encoding="utf-8")
        assert '"stage":"build"' in events and '"detail":"metade"' in events
        pong = client.call({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        assert pong["result"] == {}
        unknown = client.call({"jsonrpc": "2.0", "id": 5, "method": "metodo/estranho"})
        assert unknown["error"]["code"] == -32601
    finally:
        client.close()


def test_bridge_request_stop_is_idempotent(bridge_env):
    client = BridgeClient(bridge_env["env"], bridge_env["directory"].parent)
    try:
        first = client.call({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "request_stop", "arguments": {"reason": "escopo mudou"}},
        })
        assert first["result"]["content"][0]["text"] == "parada solicitada"
        control = json.loads((bridge_env["directory"] / "control.json").read_text(encoding="utf-8"))
        assert control["op"] == "stop" and control["reason"] == "escopo mudou"
        second = client.call({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "request_stop", "arguments": {"reason": "de novo"}},
        })
        assert second["result"]["content"][0]["text"] == "parada já solicitada"
        assert json.loads((bridge_env["directory"] / "control.json").read_text(encoding="utf-8"))["reason"] == "escopo mudou"
    finally:
        client.close()


def test_bridge_ask_orchestrator_receives_answer(bridge_env):
    directory = bridge_env["directory"]
    client = BridgeClient(bridge_env["env"], bridge_env["directory"].parent)
    try:
        def orchestrator():
            asks = directory / "asks.jsonl"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not asks.exists():
                time.sleep(0.05)
            ask = json.loads(asks.read_text(encoding="utf-8").splitlines()[-1])
            answer = directory / f"answer-{ask['id']}.json"
            fd = jobs._private_file(str(answer), create=True)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                target.write(json.dumps({"answer": "use o plano B"}))

        thread = threading.Thread(target=orchestrator)
        thread.start()
        answered = client.call({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {
                "name": "ask_orchestrator",
                "arguments": {"question": "plano A ou B?", "options": ["A", "B"], "timeout": 8},
            },
        })
        thread.join(5)
        assert answered["result"]["content"][0]["text"] == "use o plano B"
        ask = json.loads((directory / "asks.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert ask["question"] == "plano A ou B?" and ask["options"] == ["A", "B"]
        assert not (directory / f"answer-{ask['id']}.json").exists()
    finally:
        client.close()


def test_bridge_ask_orchestrator_times_out_safely(bridge_env):
    client = BridgeClient(bridge_env["env"], bridge_env["directory"].parent)
    try:
        expired = client.call({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_orchestrator", "arguments": {"question": "alguma resposta?", "timeout": 0.4}},
        })
        text = expired["result"]["content"][0]["text"]
        assert text.startswith("ERRO:") and "prazo" in text
    finally:
        client.close()


def test_bridge_rejects_bad_arguments_and_missing_job(tmp_path, monkeypatch):
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(ROOT / "src")})
    env.pop("ZCODE_WORKER_JOB_ID", None)
    client = BridgeClient(env, tmp_path)
    try:
        missing = client.call({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "report_progress", "arguments": {"stage": "x"}},
        })
        assert "job não identificado" in missing["result"]["content"][0]["text"]
        assert missing["result"]["isError"] is True
        bogus_tool = client.call({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "nao_existe", "arguments": {}},
        })
        assert bogus_tool["error"]["code"] == -32602
        bad_json = b"isto nao eh json"
        client.process.stdin.write(bad_json.decode() + "\n")
        client.process.stdin.flush()
        parse_error = client.receive()
        assert parse_error["error"]["code"] == -32700
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Bridge in-process: matriz de mensagens, ferramentas e limites
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge_job(isolated_state, tmp_path, monkeypatch):
    job_id = "5" * 32
    directory = Path(jobs._job_dir(job_id, create=True))
    monkeypatch.setenv("ZCODE_WORKER_JOB_ID", job_id)
    return {"job_id": job_id, "directory": directory}


def rpc_lines(capsys):
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_bridge_handle_message_matrix(bridge_job, capsys):
    assert bridge.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) is True
    assert bridge.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is True
    assert bridge.handle_message("mensagem não dict") is True
    assert bridge.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"}) is True
    assert bridge.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}) is True
    assert bridge.handle_message({"jsonrpc": "2.0", "id": 4, "method": "estranho"}) is True
    assert bridge.handle_message({"method": "sem id desconhecido"}) is True
    assert bridge.handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "x", "arguments": []}}) is True
    replies = rpc_lines(capsys)
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert replies[1]["result"] == {}
    assert len(replies[2]["result"]["tools"]) == 3
    assert replies[3]["error"]["code"] == -32601
    assert replies[4]["error"]["code"] == -32602


def test_bridge_tool_call_wraps_unexpected_failure(bridge_job, capsys, monkeypatch):
    def boom(_arguments):
        raise RuntimeError("detalhe interno")

    monkeypatch.setitem(bridge.TOOL_IMPLEMENTATIONS, "ask_orchestrator", boom)
    bridge.handle_message({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "ask_orchestrator", "arguments": {"question": "q"}},
    })
    reply = rpc_lines(capsys)[0]
    assert reply["result"]["isError"] is True
    assert "detalhe interno" not in reply["result"]["content"][0]["text"]


def test_bridge_tools_validate_fields(bridge_job, monkeypatch):
    monkeypatch.delenv("ZCODE_WORKER_JOB_ID", raising=False)
    with pytest.raises(bridge.BridgeError, match="job não identificado"):
        bridge.tool_ask_orchestrator({"question": "q"})
    monkeypatch.setenv("ZCODE_WORKER_JOB_ID", bridge_job["job_id"])
    with pytest.raises(bridge.BridgeError, match="pergunta"):
        bridge.tool_ask_orchestrator({"question": "   "})
    with pytest.raises(bridge.BridgeError, match="opção"):
        bridge.tool_ask_orchestrator({"question": "q", "options": ["ok", ""]})
    with pytest.raises(bridge.BridgeError, match="timeout"):
        bridge.tool_ask_orchestrator({"question": "q", "timeout": "logo"})
    with pytest.raises(bridge.BridgeError, match="timeout"):
        bridge.tool_ask_orchestrator({"question": "q", "timeout": 0})
    with pytest.raises(bridge.BridgeError, match="etapa"):
        bridge.tool_report_progress({"stage": ""})
    with pytest.raises(bridge.BridgeError, match="detalhe"):
        bridge.tool_report_progress({"stage": "s", "detail": 7})
    with pytest.raises(bridge.BridgeError, match="motivo"):
        bridge.tool_request_stop({"reason": ""})


def test_bridge_ask_orchestrator_flow_inprocess(bridge_job, monkeypatch):
    directory = bridge_job["directory"]
    fixed_now = 1700000000.5
    monkeypatch.setattr(bridge.time, "time", lambda: fixed_now)
    ask_id = int(fixed_now * 1000)
    answer = directory / f"answer-{ask_id}.json"
    fd = jobs._private_file(str(answer), create=True)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        target.write(json.dumps({"answer": "siga pelo plano B"}))
    reply = bridge.tool_ask_orchestrator({"question": "A ou B?", "options": ["A"], "timeout": 5})
    assert reply == "siga pelo plano B"
    assert not answer.exists()
    asks_file = directory / "asks.jsonl"
    ask_lines = asks_file.read_text(encoding="utf-8").splitlines() if asks_file.exists() else []
    assert ask_lines
    ask = json.loads(ask_lines[-1])
    assert ask["kind"] == "ask" and ask["options"] == ["A"]


def test_bridge_ask_orchestrator_rejects_bad_answer(bridge_job, monkeypatch):
    directory = bridge_job["directory"]
    fixed_now = 1700000001.5
    monkeypatch.setattr(bridge.time, "time", lambda: fixed_now)
    answer = directory / f"answer-{int(fixed_now * 1000)}.json"
    answer.write_text(json.dumps({"answer": ""}), encoding="utf-8")
    answer.chmod(0o600)
    with pytest.raises(bridge.BridgeError, match="resposta"):
        bridge.tool_ask_orchestrator({"question": "q", "timeout": 5})


def test_bridge_ask_orchestrator_timeout_inprocess(bridge_job, monkeypatch):
    class ExhaustedClock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            self.now += 10.0
            return self.now

    clock = ExhaustedClock()
    monkeypatch.setattr(bridge.time, "monotonic", clock.monotonic)
    outcome = "sem-erro"
    try:
        bridge.tool_ask_orchestrator({"question": "q", "timeout": 5})
    except bridge.BridgeError as error:
        outcome = str(error)
    assert "prazo" in outcome


def test_bridge_request_stop_and_progress_inprocess(bridge_job):
    directory = bridge_job["directory"]
    assert bridge.tool_request_stop({"reason": "mudou o escopo"}) == "parada solicitada"
    assert bridge.tool_request_stop({"reason": "de novo"}) == "parada já solicitada"
    control = json.loads((directory / "control.json").read_text(encoding="utf-8"))
    assert control["op"] == "stop" and control["reason"] == "mudou o escopo"
    assert bridge.tool_report_progress({"stage": "analise"}) == "progress registrado"
    events = (directory / "events.jsonl").read_text(encoding="utf-8")
    assert "stop_requested" in events and '"stage":"analise"' in events


def test_bridge_main_reads_stdin_until_eof(monkeypatch):
    import io

    fake_stdin = io.StringIO(
        "\n"
        "isto nao é json\n"
        '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
    )
    monkeypatch.setattr(bridge.sys, "stdin", fake_stdin)
    assert bridge.main() == 0


def test_append_event_handles_unwritable_and_foreign_targets(isolated_state, tmp_path, monkeypatch):
    missing_dir = tmp_path / "sem-dir" / "events.jsonl"
    jobs._append_event(str(missing_dir), {"kind": "progress"})
    assert not missing_dir.exists()

    foreign_target = tmp_path / "foreign-events.jsonl"
    foreign_target.write_text("x", encoding="utf-8")
    foreign_target.chmod(0o600)
    real_fstat = os.fstat
    foreign_dir_fd = os.open(str(tmp_path), os.O_RDONLY)
    armed = []

    def fstat_directory(fd, *args, **kwargs):
        if not armed:
            armed.append(fd)
            return real_fstat(foreign_dir_fd)
        return real_fstat(fd, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "fstat", fstat_directory)
    jobs._append_event(str(foreign_target), {"kind": "progress"})
    assert foreign_target.read_text(encoding="utf-8") == "x"
    os.close(foreign_dir_fd)


def test_signal_cancel_two_stage_race_window(monkeypatch):
    checks = iter([True, False])

    def identity_once(*_args):
        return next(checks)

    monkeypatch.setattr(jobs, "_proc_has_identity", identity_once)
    monkeypatch.setattr(jobs, "_CANCEL_KILL_GRACE", 0.0)
    sent = []
    monkeypatch.setattr(jobs.os, "pidfd_open", lambda *_a: os.open("/dev/null", os.O_RDONLY))
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", lambda *args: sent.append(args))
    forced = jobs._signal_cancel_two_stage(4242, os.getuid(), 7, "6" * 32)
    assert forced is False and sent and sent[0][1] == signal.SIGTERM


# ---------------------------------------------------------------------------
# Fechamento de cobertura do bridge
# ---------------------------------------------------------------------------


def test_bridge_text_field_and_options_limits():
    assert bridge._text_field("  ok  ", limit=16, name="campo") == "ok"
    with pytest.raises(bridge.BridgeError, match="limite"):
        bridge._text_field("x" * 300, limit=16, name="campo")
    assert bridge._validated_options(None) == []
    with pytest.raises(bridge.BridgeError, match="opções inválidas"):
        bridge._validated_options("nao-lista")
    with pytest.raises(bridge.BridgeError, match="opções inválidas"):
        bridge._validated_options(["o"] * (bridge.ASK_MAX_OPTIONS + 1))


def test_bridge_ask_waits_poll_cycles_for_orchestrator(bridge_job, monkeypatch):
    directory = bridge_job["directory"]
    fixed_now = 1700000002.5
    monkeypatch.setattr(bridge.time, "time", lambda: fixed_now)
    ask_id = int(fixed_now * 1000)

    def slow_orchestrator():
        time.sleep(0.6)
        answer = directory / f"answer-{ask_id}.json"
        fd = jobs._private_file(str(answer), create=True)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(json.dumps({"answer": "demorei mas respondi"}))

    thread = threading.Thread(target=slow_orchestrator)
    thread.start()
    try:
        reply = bridge.tool_ask_orchestrator({"question": "pode?", "timeout": 10})
    finally:
        thread.join(5)
    assert reply == "demorei mas respondi"


def test_bridge_progress_with_detail_inprocess(bridge_job):
    directory = bridge_job["directory"]
    bridge.tool_report_progress({"stage": "teste", "detail": "detalhe longo"})
    event = json.loads((directory / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["stage"] == "teste" and event["detail"] == "detalhe longo"


def test_bridge_handle_call_paths(bridge_job, capsys):
    bridge.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "ask_orchestrator", "arguments": "texto"},
    })
    bridge.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "ask_orchestrator", "arguments": {"question": "q", "timeout": 0.001}},
    })
    bridge.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "report_progress", "arguments": {"stage": "s"}},
    })
    replies = rpc_lines(capsys)
    assert replies[0]["error"]["code"] == -32602
    assert replies[1]["result"]["isError"] is True and "prazo" in replies[1]["result"]["content"][0]["text"]
    assert replies[2]["result"] == {"content": [{"type": "text", "text": "progress registrado"}]}


def test_bridge_main_guard_executes_inprocess(monkeypatch):
    import io
    import runpy

    fake_stdin = io.StringIO('{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
    monkeypatch.setattr(bridge.sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["worker-bridge"])
    with pytest.raises(SystemExit) as excinfo:
        with contextlib.suppress(ValueError):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                runpy.run_module("zcode_worker.bridge", run_name="__main__", alter_sys=False)
    assert excinfo.value.code in (0, None)


def test_run_child_cleans_orphan_answers_and_keeps_history(isolated_state, tmp_path, monkeypatch):
    job_id = "7" * 32
    directory = Path(jobs._job_dir(job_id, create=True))
    output = tmp_path / "orphan.out"
    fd = jobs._create_output(str(output))
    identity = os.fstat(fd)
    os.close(fd)
    metadata = {
        "id": job_id,
        "status": "completed",
        "createdAt": 1.0,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "starttime": 123,
        "output": str(output),
        "exitCode": 0,
        "outputDev": identity.st_dev,
        "outputIno": identity.st_ino,
    }
    jobs._atomic_write(jobs._metadata_path(str(directory)), metadata, limit=64 * 1024)
    orphan = directory / "answer-1700000000000.json"
    orphan.write_text(json.dumps({"answer": "chegou tarde"}), encoding="utf-8")
    orphan.chmod(0o600)
    asks = directory / "asks.jsonl"
    fd = jobs._private_file(str(asks), create=True)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        target.write('{"kind":"ask","id":1,"question":"q?"}\n')
    monkeypatch.setattr(jobs, "_proc_stat", lambda _pid: (os.getuid(), 123))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_a: True)
    monkeypatch.setattr(jobs, "_stdout_matches_output", lambda _identity: True)
    assert jobs.run_child(job_id, lambda *_: pytest.fail("job terminal")) == 0
    assert not orphan.exists()
    assert asks.exists()
    assert '"question":"q?"' in asks.read_text(encoding="utf-8")
    assert (directory / "metadata.json").exists()


def test_orphan_answers_ignores_foreign_entries(isolated_state, tmp_path, monkeypatch):
    job_id = "8" * 32
    directory = Path(jobs._job_dir(job_id, create=True))
    carrier = tmp_path / "carrier.json"
    carrier.write_text("{}", encoding="utf-8")
    foreign = directory / "answer-foreign.json"
    foreign.symlink_to(carrier)
    with contextlib.suppress(OSError):
        os.unlink(str(directory / "answer-x.json"))
    assert jobs._orphan_answers(str(directory)) == []
    real = directory / "answer-9.json"
    real.write_text("{}", encoding="utf-8")
    real.chmod(0o600)
    assert jobs._orphan_answers(str(directory)) == [str(real)]

    assert jobs._orphan_answers(str(tmp_path / "diretorio-que-sumiu")) == []
    real_lstat = os.lstat

    def lstat_vanishing(path, *args, **kwargs):
        if os.path.basename(str(path)) == "answer-9.json":
            raise OSError("sumiu entre listdir e lstat")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "lstat", lstat_vanishing)
    assert jobs._orphan_answers(str(directory)) == []
