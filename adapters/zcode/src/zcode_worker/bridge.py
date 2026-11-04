"""Servidor MCP stdio para o worker conversar com o orquestrador.

O bridge roda como servidor MCP do zcode-cli durante um job. As ferramentas
trocam mensagens com o orquestrador por arquivos privados no diretório do job
(``asks.jsonl``/``answer-<n>.json``/``control.json``/``events.jsonl``), validando
o diretório com as mesmas guardas de :mod:`zcode_worker.jobs`. A resposta do
orquestrador é sempre DADO para o modelo, nunca instrução de terceiros.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from typing import Any

from . import jobs


MCP_PROTOCOL_VERSION = "2024-11-05"
JOB_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
ASK_MAX_QUESTION_BYTES = 8 * 1024
ASK_MAX_OPTIONS = 8
ASK_MAX_OPTION_BYTES = 1024
ANSWER_MAX_BYTES = 96 * 1024
STOP_REASON_MAX_BYTES = 2 * 1024
DEFAULT_ASK_TIMEOUT_SECONDS = 300.0
MAX_ASK_TIMEOUT_SECONDS = 3600.0
ASK_POLL_SECONDS = 0.25

TOOLS = [
    {
        "name": "ask_orchestrator",
        "description": (
            "Pergunta ao orquestrador e aguarda a resposta dele. Use quando "
            "faltar instrução essencial que você não possa decidir sozinho."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "report_progress",
        "description": "Registra o andamento atual para o orquestrador acompanhar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage": {"type": "string"},
                "detail": {"type": "string"},
            },
            "required": ["stage"],
        },
    },
    {
        "name": "request_stop",
        "description": "Pede ao orquestrador para encerrar este job de forma cooperativa.",
        "inputSchema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


class BridgeError(Exception):
    """Erro seguro do bridge, com mensagem fixa e sem conteúdo externo."""


def _job_directory() -> str:
    job_id = os.environ.get("ZCODE_WORKER_JOB_ID", "")
    if not JOB_ID_RE.fullmatch(job_id):
        raise BridgeError("job não identificado para o bridge")
    return jobs._job_dir(job_id, create=False)


def _text_field(value: Any, *, limit: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"campo {name} inválido")
    if len(value.encode("utf-8")) > limit:
        raise BridgeError(f"campo {name} excede o limite")
    return value.strip()


def _validated_options(options: Any) -> list[str]:
    if options is None:
        return []
    if not isinstance(options, list) or len(options) > ASK_MAX_OPTIONS:
        raise BridgeError("opções inválidas")
    return [
        _text_field(option, limit=ASK_MAX_OPTION_BYTES, name="opção")
        for option in options
    ]


def tool_ask_orchestrator(arguments: dict[str, Any]) -> str:
    question = _text_field(
        arguments.get("question"), limit=ASK_MAX_QUESTION_BYTES, name="pergunta"
    )
    options = _validated_options(arguments.get("options"))
    try:
        timeout = float(arguments.get("timeout", DEFAULT_ASK_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        raise BridgeError("timeout inválido") from None
    if not 0 < timeout <= MAX_ASK_TIMEOUT_SECONDS:
        raise BridgeError("timeout inválido")
    directory = _job_directory()
    sequence = int(time.time() * 1000)
    ask = {"kind": "ask", "id": sequence, "ts": time.time(), "question": question, "options": options}
    jobs._append_event(os.path.join(directory, "asks.jsonl"), ask)
    answer_path = os.path.join(directory, f"answer-{sequence}.json")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(answer_path):
            payload = jobs._read_private_json(answer_path, limit=ANSWER_MAX_BYTES)
            answer = payload.get("answer") if isinstance(payload, dict) else None
            if not isinstance(answer, str) or not answer.strip():
                raise BridgeError("resposta do orquestrador inválida")
            with contextlib.suppress(OSError):
                os.unlink(answer_path)
            return answer
        time.sleep(ASK_POLL_SECONDS)
    raise BridgeError("orquestrador não respondeu no prazo")


def tool_report_progress(arguments: dict[str, Any]) -> str:
    stage = _text_field(arguments.get("stage"), limit=256, name="etapa")
    detail = arguments.get("detail")
    if detail is not None:
        detail = _text_field(detail, limit=2 * 1024, name="detalhe")
    directory = _job_directory()
    event: dict[str, Any] = {"kind": "progress", "stage": stage, "ts": time.time()}
    if detail is not None:
        event["detail"] = detail
    jobs._append_event(os.path.join(directory, "events.jsonl"), event)
    return "progress registrado"


def tool_request_stop(arguments: dict[str, Any]) -> str:
    reason = _text_field(
        arguments.get("reason"), limit=STOP_REASON_MAX_BYTES, name="motivo"
    )
    directory = _job_directory()
    control_path = os.path.join(directory, "control.json")
    payload = {"op": "stop", "reason": reason, "ts": time.time()}
    try:
        fd = jobs._private_file(control_path, create=True)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            target.flush()
            os.fsync(target.fileno())
    except jobs.WorkerError:
        # Pedido de parada já registrado: idempotente.
        return "parada já solicitada"
    jobs._append_event(os.path.join(directory, "events.jsonl"), {"kind": "stop_requested", "reason": reason, "ts": time.time()})
    return "parada solicitada"


TOOL_IMPLEMENTATIONS = {
    "ask_orchestrator": tool_ask_orchestrator,
    "report_progress": tool_report_progress,
    "request_stop": tool_request_stop,
}


def _rpc_result(message_id: Any, result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _rpc_error(message_id: Any, code: int, message: str) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_message(message: Any) -> bool:
    """Processa uma mensagem MCP; devolve False para encerrar o loop."""

    if not isinstance(message, dict):
        return True
    method = message.get("method")
    message_id = message.get("id")
    if method == "initialize":
        _rpc_result(message_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "worker-bridge", "version": "1"},
        })
        return True
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return True
    if method == "ping":
        _rpc_result(message_id, {})
        return True
    if method == "tools/list":
        _rpc_result(message_id, {"tools": TOOLS})
        return True
    if method == "tools/call":
        name = message.get("params", {}).get("name") if isinstance(message.get("params"), dict) else None
        implementation = TOOL_IMPLEMENTATIONS.get(name) if isinstance(name, str) else None
        if implementation is None:
            _rpc_error(message_id, -32602, "ferramenta desconhecida")
            return True
        arguments = message.get("params", {}).get("arguments") or {}
        if not isinstance(arguments, dict):
            _rpc_error(message_id, -32602, "argumentos inválidos")
            return True
        try:
            text = implementation(arguments)
        except BridgeError as error:
            _rpc_result(message_id, {
                "content": [{"type": "text", "text": f"ERRO: {error}"}],
                "isError": True,
            })
            return True
        except Exception:
            _rpc_result(message_id, {
                "content": [{"type": "text", "text": "ERRO: falha interna do bridge"}],
                "isError": True,
            })
            return True
        _rpc_result(message_id, {"content": [{"type": "text", "text": text}]})
        return True
    if message_id is not None:
        _rpc_error(message_id, -32601, "método não suportado")
    return True


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except (TypeError, ValueError):
            _rpc_error(None, -32700, "parse error")
            continue
        if not handle_message(message):  # pragma: no branch
            # Reservado a controles futuros de encerramento do loop; hoje o
            # EOF do stdin encerra o servidor.
            break  # pragma: no cover
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
