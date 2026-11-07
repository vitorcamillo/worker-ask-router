"""Testes da flag --chat: prompt exatamente a tarefa, sem preâmbulo de worker.

Pontes HTTP de conversa chamam o zcode-worker para herdar provider/Start Plan,
mas não devem receber as instruções de worker nem o contrato de resposta JSON.
"""

from __future__ import annotations

import contextlib
import json
import os
from types import SimpleNamespace

import pytest

from zcode_worker import base, cli, features


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
        "chat": False,
        "doctor": False,
        "list_models": False,
        "saida": None,
        "status": None,
        "cancel": None,
        "_job_child": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_provider(tmp_path, monkeypatch):
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


# ---------------------------------------------------------------------------
# features.build_cli_argv
# ---------------------------------------------------------------------------


def test_build_cli_argv_chat_prompt_is_exactly_the_task(tmp_path):
    argv = features.build_cli_argv(
        "tarefa limpa", str(tmp_path), "plan", "glm-5.3-flash", "high", chat=True
    )
    assert argv[1] == "-p" and argv[2] == "tarefa limpa"
    assert base.PREAMBULO not in argv[2]
    assert features.TURN_MARKER not in argv[2]


def test_build_cli_argv_without_chat_keeps_preamble(tmp_path):
    argv = features.build_cli_argv("tarefa", str(tmp_path), "plan", "glm-5.3-flash", "high")
    assert argv[2].startswith(base.PREAMBULO)
    assert argv[2].endswith("tarefa")


# ---------------------------------------------------------------------------
# features.parse_args: conflitos e propagação
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--chat", "--conversation"],
        ["--chat", "--resume", "sess_abcd1234"],
        ["--chat", "--saida", "saida.json"],
    ],
)
def test_parse_args_chat_rejects_incompatible_flags(tmp_path, argv):
    with pytest.raises(features.FeatureError, match="--chat não combina"):
        features.parse_args(argv + ["--cwd", str(tmp_path), "task"])


def test_parse_args_and_namespace_carry_chat(tmp_path):
    parsed = features.parse_args(["--cwd", str(tmp_path), "--chat", "task"])
    assert parsed.chat is True
    normalized = features.public_namespace(parsed)
    assert normalized.chat is True
    plain = features.public_namespace(feature_args(cwd=str(tmp_path)))
    assert plain.chat is False


# ---------------------------------------------------------------------------
# cli.run_task com chat e runner falso
# ---------------------------------------------------------------------------


def test_run_task_chat_executes_prompt_without_preamble(tmp_path, monkeypatch):
    make_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: contextlib.nullcontext())
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"modelId": "ok"})
    seen = {}
    payload = fake_cli_payload("resposta de conversa")

    def runner(command, env, deadline, limit):
        seen["command"] = command
        return 0, payload, ""

    args = feature_args(cwd=str(tmp_path), chat=True)
    result, code = cli.run_task(args, "pergunta da conversa", runner=runner)
    assert code == 0 and result["status"] == "ok"
    argv = seen["command"]
    assert argv[1] == "-p" and argv[2] == "pergunta da conversa"
    assert base.PREAMBULO not in argv[2]
