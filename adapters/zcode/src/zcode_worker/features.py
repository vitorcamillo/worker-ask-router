"""Regras puras e validações do worker complementar.

Este módulo não inicia processos nem altera a configuração global do ZCode.  O
engine em :mod:`zcode_worker.cli` é o único responsável por executar tarefas.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from . import base


PROVIDER_ID = base.PROVIDER_ID
MODELS = base.MODELS
EFFORTS = base.EFFORTS
PROVIDER_MODELS = dict(base.PROVIDER_MODELS)
PROFILES = {
    "rapido": ("glm-5.3-flash", "low"),
    "execucao": ("glm-5.3-flash", "high"),
    "raciocinio": ("glm-5.3-flash", "max"),
}
TASK_MAX_BYTES = base.TASK_MAX_BYTES
START_PLAN_PROVIDER_ID = "account:zai-start-plan"
START_PLAN_MODELS = frozenset({"glm-5.3-flash"})
START_PLAN_HOURS = (0, 24)  # Start Plan vale o dia todo até expirar; janela via ZCODE_WORKER_START_PLAN_HOURS
START_PLAN_BUSY_MINUTES = 2
START_PLAN_MARKER = "start-plan-unavailable"
START_PLAN_OK_MARKER = "start-plan-ok"
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
# Órfãos de provider-*.json (processo morto antes do finally) expiram em 24h.
PROVIDER_TMP_TTL_SECONDS = 24 * 3600
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
SESSION_RE = re.compile(r"^sess_[A-Za-z0-9_-]+$")
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_:-]*$")
SECRET_COMPONENT_RE = re.compile(
    r"(?:^|[-_.])(?:token|key|private|cookie)(?:$|[-_.])", re.IGNORECASE
)
SECRET_NAMES = {"auth.json", "credentials.json"}
KNOWN_USAGE = {
    "modelRequestCount", "webFetchRequests", "webSearchRequests",
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "reasoningTokens",
    "cachedInputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
    "promptTokens",
    "completionTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
}
REJECTED_STATUS = {"error", "rejected", "paused"}
# Protocolo de turnos: o worker encerra a resposta com o marcador e um JSON
# único com a pergunta ao orquestrador. O orquestrador responde com um novo
# turno via --resume na mesma sessão do zcode-cli.
TURN_MARKER = "===NEED_INPUT==="
TURN_MAX_QUESTION_BYTES = 8 * 1024
TURN_MAX_OPTIONS = 8
CONVERSATION_PROTOCOL_INSTRUCTION = (
    "\n--- PROTOCOLO DE CONVERSA ---\n"
    "Se faltar instrução essencial para completar a fatia com segurança, encerre "
    "a resposta com uma linha " + TURN_MARKER + " seguida de um único objeto JSON "
    '{"question": "...", "options": ["a", "b"]} e mais nada depois dele. '
    "Use somente quando não puder decidir sozinho; nunca invente resposta de terceiros."
)
_CORE = base


class FeatureError(Exception):
    """Erro esperado com mensagem segura para o operador."""

    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise FeatureError("argumentos inválidos")


def _finite_positive(value: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("prazo inválido") from None
    if not math.isfinite(number) or not 0 < number <= 3600:
        raise argparse.ArgumentTypeError("prazo deve ser finito, positivo e no máximo 3600s")
    return number


def _positive_bytes(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("limite de saída inválido") from None
    if not 0 < number <= DEFAULT_MAX_OUTPUT_BYTES:
        raise argparse.ArgumentTypeError("limite de saída fora do intervalo")
    return number


def _absolutize_paths(parsed: argparse.Namespace) -> None:
    """Resolve caminhos relativos (ex.: --cwd .) contra o diretório de quem chamou.

    O job em background e o engine exigem caminhos absolutos; sem isso
    `--cwd .` com `--saida` morria como "falha interna segura".
    """

    for name in ("cwd", "spec", "saida"):
        value = getattr(parsed, name, None)
        if isinstance(value, str) and value and "\x00" not in value:
            setattr(parsed, name, os.path.abspath(os.path.expanduser(value)))
    parsed.attach = [
        os.path.abspath(os.path.expanduser(item))
        if isinstance(item, str) and item and "\x00" not in item else item
        for item in (parsed.attach or [])
    ]


def parse_args(argv: list[str] | tuple[str, ...]) -> argparse.Namespace:
    """Analisa somente as opções suportadas pela cápsula."""

    parser = _ArgumentParser(description="worker complementar do ZCode", allow_abbrev=False)
    parser.add_argument("tarefa", nargs="?")
    parser.add_argument("--cwd")
    parser.add_argument("--mode", choices=("plan", "edit", "yolo"), default="edit")
    parser.add_argument("--spec")
    parser.add_argument("--timeout", type=_finite_positive, default=1800.0)
    parser.add_argument("--queue-timeout", type=_finite_positive)
    parser.add_argument("--total-timeout", type=_finite_positive)
    parser.add_argument("--model", type=_canonical_model)
    parser.add_argument("--effort", choices=EFFORTS)
    parser.add_argument("--profile", choices=tuple(PROFILES))
    parser.add_argument("--resume")
    parser.add_argument("--attach", action="append", default=[])
    parser.add_argument("--disallowed-tools", action="append", default=[])
    parser.add_argument("--max-output-bytes", type=_positive_bytes)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--conversation", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--saida")
    parser.add_argument("--status")
    parser.add_argument("--cancel")
    parser.add_argument("--_job-child", dest="_job_child")
    parsed = parser.parse_args(list(argv))
    _absolutize_paths(parsed)
    if parsed.queue_timeout is None:
        # Job em background (--saida) pode esperar slot: com max_parallel=4, o 5º
        # worker disparado em lote morria após 30s na fila ("erro de infra").
        parsed.queue_timeout = 1800.0 if parsed.saida else 30.0
    if parsed.resume and not SESSION_RE.fullmatch(parsed.resume):
        raise FeatureError("sessão inválida")
    if parsed.spec and parsed.tarefa is not None:
        raise FeatureError("tarefa e --spec são mutuamente exclusivos")
    if parsed.status and parsed.cancel:
        raise FeatureError("status e cancel são mutuamente exclusivos")
    controls = {
        "doctor": parsed.doctor,
        "list_models": parsed.list_models,
        "status": bool(parsed.status),
        "cancel": bool(parsed.cancel),
        "saida": bool(parsed.saida),
        "dry_run": parsed.dry_run,
        "job_child": bool(parsed._job_child),
    }
    active = {name for name, enabled in controls.items() if enabled}
    if active & {"doctor", "list_models"}:
        if len(active) != 1 or parsed.tarefa is not None or parsed.spec:
            raise FeatureError("modo de controle incompatível")
    if active & {"status", "cancel"}:
        if active - {"status", "cancel"} or parsed.tarefa is not None or parsed.spec:
            raise FeatureError("modo de controle incompatível")
    if "job_child" in active:
        if len(active) != 1 or parsed.tarefa is not None or parsed.spec:
            raise FeatureError("modo de controle incompatível")
    if "saida" in active and active & {"doctor", "list_models", "status", "cancel", "dry_run", "job_child"}:
        raise FeatureError("modo de controle incompatível")
    if "dry_run" in active and active & {"doctor", "list_models", "status", "cancel", "job_child"}:
        # Defensa em profundidade: qualquer combinação acima já foi recusada
        # pelas checagens anteriores; mantida para futuros controles novos.
        raise FeatureError("modo de controle incompatível")  # pragma: no cover
    return parsed


def _canonical_model(value: str) -> str:
    try:
        return base.canonical_model(value)
    except argparse.ArgumentTypeError:
        raise
    except (AttributeError, TypeError):
        raise argparse.ArgumentTypeError("modelo inválido") from None


def selection_for(profile: str | None, model: str | None, effort: str | None) -> tuple[str, str]:
    """Resolve perfil e overrides sem consultar catálogo externo."""

    if profile is not None and profile not in PROFILES:
        raise FeatureError("perfil inválido")
    selected_model, selected_effort = PROFILES.get(profile, ("glm-5.3-flash", "high"))
    if model is not None:
        if model not in MODELS:
            raise FeatureError("modelo inválido")
        selected_model = model
    if effort is not None:
        if effort not in EFFORTS:
            raise FeatureError("effort inválido")
        selected_effort = effort
    return selected_model, selected_effort


def allowed_providers() -> frozenset:
    return frozenset({PROVIDER_ID, START_PLAN_PROVIDER_ID})


def _start_plan_state_path(state_dir: str | os.PathLike[str] | None = None) -> Path:
    return Path(state_dir or base.STATE) / START_PLAN_MARKER


def start_plan_hours(environ=None) -> tuple[int, int]:
    env = os.environ if environ is None else environ
    raw = env.get("ZCODE_WORKER_START_PLAN_HOURS")
    if not isinstance(raw, str):
        return START_PLAN_HOURS
    text = raw.strip().lower().replace(" ", "")
    if text == "always":
        return (0, 24)
    parts = text.split("-")
    if len(parts) != 2:
        return START_PLAN_HOURS
    try:
        start = int(parts[0])
        end = int(parts[1])
    except ValueError:
        return START_PLAN_HOURS
    if not 0 <= start < end <= 24:
        return START_PLAN_HOURS
    return (start, end)


def _read_start_plan_blocked_until(state_dir=None) -> datetime.datetime | None:
    try:
        text = _start_plan_state_path(state_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    tokens = text.split()
    if len(tokens) < 2:
        return None
    try:
        until = datetime.datetime.fromisoformat(tokens[0])
    except ValueError:
        return None
    if until.tzinfo is not None:
        return None
    return until


def start_plan_blocked(*, now=None, state_dir=None) -> bool:
    until = _read_start_plan_blocked_until(state_dir)
    if until is None:
        return False
    current = now or datetime.datetime.now()
    return current < until


def mark_start_plan_unavailable(reason: str, *, now=None, state_dir=None) -> None:
    try:
        current = now or datetime.datetime.now()
        if reason in {"rate_limit", "refused"}:
            until = current + datetime.timedelta(minutes=START_PLAN_BUSY_MINUTES)
        else:
            base_day = current.replace(hour=0, minute=0, second=0, microsecond=0)
            until = base_day + datetime.timedelta(days=1)
        directory = Path(state_dir or base.STATE)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = f"{until.isoformat(timespec='seconds')} {reason}\n"
        fd, tmp_name = tempfile.mkstemp(prefix="start-plan-", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(content)
            os.replace(tmp_name, str(directory / START_PLAN_MARKER))
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        with contextlib.suppress(OSError):
            os.unlink(str(directory / START_PLAN_OK_MARKER))
    except OSError:
        return


def _start_plan_ok_path(state_dir=None) -> Path:
    return Path(state_dir or base.STATE) / START_PLAN_OK_MARKER


def start_plan_confirmed(*, now=None, state_dir=None) -> bool:
    try:
        text = _start_plan_ok_path(state_dir).read_text(encoding="utf-8")
    except OSError:
        return False
    current = now or datetime.datetime.now()
    return text.strip() == current.strftime("%Y-%m-%dT%H")


def mark_start_plan_confirmed(*, now=None, state_dir=None) -> None:
    try:
        current = now or datetime.datetime.now()
        directory = Path(state_dir or base.STATE)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = f"{current.strftime('%Y-%m-%dT%H')}\n"
        fd, tmp_name = tempfile.mkstemp(prefix="start-plan-ok-", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(content)
            os.replace(tmp_name, str(directory / START_PLAN_OK_MARKER))
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        return


def provider_for(model: str, *, now=None, state_dir=None, environ=None) -> str:
    try:
        canonical = _canonical_model(model)
    except Exception:
        return PROVIDER_ID
    if canonical not in START_PLAN_MODELS:
        return PROVIDER_ID
    current = now or datetime.datetime.now()
    start, end = start_plan_hours(environ)
    if not start <= current.hour < end:
        return PROVIDER_ID
    env = os.environ if environ is None else environ
    raw = env.get("ZCODE_WORKER_START_PLAN")
    if isinstance(raw, str) and raw.strip().lower() in {"0", "off", "false", "no"}:
        return PROVIDER_ID
    if start_plan_blocked(now=current, state_dir=state_dir):
        return PROVIDER_ID
    return START_PLAN_PROVIDER_ID


def start_plan_next_available(model: str, *, now=None, state_dir=None, environ=None) -> datetime.datetime | None:
    try:
        canonical = _canonical_model(model)
    except Exception:
        return None
    if canonical not in START_PLAN_MODELS:
        return None
    env = os.environ if environ is None else environ
    raw = env.get("ZCODE_WORKER_START_PLAN")
    if isinstance(raw, str) and raw.strip().lower() in {"0", "off", "false", "no"}:
        return None
    current = now or datetime.datetime.now()
    moment = current
    until = _read_start_plan_blocked_until(state_dir)
    if until is not None and until > moment:
        moment = until
    start, end = start_plan_hours(environ)
    if start <= moment.hour < end:
        return moment
    if moment.hour < start:
        return moment.replace(hour=start, minute=0, second=0, microsecond=0)
    following = moment + datetime.timedelta(days=1)
    return following.replace(hour=start, minute=0, second=0, microsecond=0)


def model_display(model: str) -> str:
    canonical = dict((value.casefold(), key) for key, value in PROVIDER_MODELS.items())
    if model in MODELS:
        return PROVIDER_MODELS[model]
    if isinstance(model, str) and model.casefold() in canonical:
        return PROVIDER_MODELS[canonical[model.casefold()]]
    raise FeatureError("modelo inválido")


def requested_selection(model: str, effort: str, provider: str | None = None) -> dict[str, str]:
    selected_model, selected_effort = selection_for(None, model, effort)
    provider_id = provider or PROVIDER_ID
    if provider_id not in allowed_providers():
        raise FeatureError("provider inválido", "config")
    return {
        "providerId": provider_id,
        "modelId": PROVIDER_MODELS[selected_model],
        "effort": selected_effort,
    }


def _decode_bounded(data: bytes | bytearray | str) -> str:
    if isinstance(data, str):
        try:
            data = data.encode("utf-8")
        except UnicodeError:
            raise FeatureError("tarefa com texto inválido") from None
    elif isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes) or len(data) > TASK_MAX_BYTES:
        raise FeatureError("tarefa excede o limite seguro")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise FeatureError("tarefa com texto inválido") from None


def read_task(args: argparse.Namespace, stdin_text: bytes | str | None = None) -> str:
    """Lê tarefa de ``--spec``, argumento posicional ou stdin com limite."""

    if args.spec:
        try:
            with Path(args.spec).open("rb") as source:
                task = _decode_bounded(source.read(TASK_MAX_BYTES + 1))
        except (OSError, UnicodeError):
            raise FeatureError("spec ilegível") from None
    elif args.tarefa is not None:
        task = args.tarefa
    elif stdin_text is not None:
        task = _decode_bounded(stdin_text)
    else:
        try:
            source = sys.stdin
            binary = getattr(source, "buffer", None)
            data = binary.read(TASK_MAX_BYTES + 1) if binary is not None else source.read(TASK_MAX_BYTES + 1)
            task = _decode_bounded(data)
        except (EOFError, UnicodeError, OSError):
            raise FeatureError("tarefa vazia") from None
    if not isinstance(task, str) or not task.strip():
        raise FeatureError("tarefa vazia")
    if "\x00" in task:
        raise FeatureError("tarefa contém byte inválido")
    if len(task.encode("utf-8")) > TASK_MAX_BYTES:
        raise FeatureError("tarefa excede o limite seguro")
    return task


def _is_secret_path(path: Path) -> bool:
    for component in path.parts:
        lowered = component.casefold()
        if lowered.startswith(".env") or lowered in SECRET_NAMES:
            return True
        if SECRET_COMPONENT_RE.search(lowered):
            return True
    return False


def validate_attachments(paths: list[str] | tuple[str, ...]) -> list[str]:
    """Valida anexos regulares, pequenos e sem nomes de segredo."""

    if len(paths) > MAX_ATTACHMENTS:
        raise FeatureError("número de anexos excede o limite")
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".txt", ".md", ".json"}
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            raise FeatureError("anexo inválido")
        candidate = Path(raw)
        try:
            info = candidate.lstat()
        except OSError:
            raise FeatureError("anexo ilegível") from None
        if stat_is_symlink(info) or not stat.S_ISREG(info.st_mode):
            raise FeatureError("anexo deve ser arquivo regular")
        resolved = str(candidate.resolve())
        if _is_secret_path(candidate) or _is_secret_path(Path(resolved)):
            raise FeatureError("anexo com nome proibido")
        if candidate.suffix.casefold() not in allowed:
            raise FeatureError("tipo de anexo não permitido")
        try:
            size = candidate.stat().st_size
        except OSError:
            raise FeatureError("anexo ilegível") from None
        if size > MAX_ATTACHMENT_BYTES:
            raise FeatureError("anexo excede o limite")
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def stat_is_symlink(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode)


def validate_denylist(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Valida nomes simples de ferramenta e remove duplicatas."""

    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        if not isinstance(raw, str):
            raise FeatureError("denylist inválida")
        for value in raw.split(","):
            if not TOOL_RE.fullmatch(value):
                raise FeatureError("denylist aceita somente nomes de ferramenta")
            if value not in seen:
                seen.add(value)
                result.append(value)
    return result


def _diagnostic_line(line: str) -> bool:
    return bool(
        re.fullmatch(r"\[[^\r\n]*\]", line)
        or re.fullmatch(r"(?:diagnostic|info|warning|warn|debug|zcode-cli|zcode-worker):\s+.*", line, re.I)
    )


def decode_response(text: str) -> dict:
    """Decodifica um único objeto JSON, tolerando diagnósticos conhecidos antes dele."""

    if not isinstance(text, str):
        raise FeatureError("resposta JSON inválida")
    decoder = json.JSONDecoder()
    first = text.find("{")
    if first < 0:
        raise FeatureError("resposta JSON inválida")
    prefix = text[:first]
    if prefix and not prefix.endswith(("\n", "\r")):
        raise FeatureError("JSON deve seguir uma linha diagnóstica")
    prefix_lines = prefix.splitlines()
    if prefix and not all(not line.strip() or _diagnostic_line(line.strip()) for line in prefix_lines):
        raise FeatureError("diagnóstico inesperado antes do JSON")
    try:
        payload, end = decoder.raw_decode(text[first:])
    except (TypeError, ValueError, json.JSONDecodeError):
        raise FeatureError("resposta JSON inválida") from None
    trailing = text[first + end :]
    if trailing.strip():
        raise FeatureError("resposta JSON ambígua")
    if not isinstance(payload, dict):
        # Inatingível: o trecho decodificado sempre começa em "{", então
        # raw_decode só devolve objeto ou falha; defesa contra mudanças acima.
        raise FeatureError("resposta JSON deve ser objeto")  # pragma: no cover
    return payload


def _number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def validate_response(payload: object) -> dict:
    """Valida e normaliza o contrato de resposta sem aceitar valores ambíguos."""

    if not isinstance(payload, dict):
        raise FeatureError("resposta inválida")
    response = payload.get("response")
    session_id = payload.get("sessionId")
    if not isinstance(response, str) or not response.strip():
        raise FeatureError("resposta vazia ou inválida")
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        raise FeatureError("sessão inválida")
    status = payload.get("status")
    if status is not None and (not isinstance(status, str) or status.casefold() in REJECTED_STATUS):
        raise FeatureError("status de resposta não concluído")
    missing = object()
    projection = payload.get("projection", missing)
    if projection is not missing:
        if isinstance(projection, dict):
            projection_status = projection.get("status")
            if projection_status is not None and not isinstance(projection_status, str):
                raise FeatureError("projection inválida")
        elif isinstance(projection, str):
            projection_status = projection
        else:
            raise FeatureError("projection inválida")
        if isinstance(projection_status, str) and projection_status.casefold() in REJECTED_STATUS:
            raise FeatureError("projection não concluída")
    usage = payload.get("usage", {})
    if "usage" in payload and usage is None:
        raise FeatureError("usage inválido")
    if not isinstance(usage, dict):
        raise FeatureError("usage inválido")
    for name, value in usage.items():
        if name == "source" and value == "provider":
            continue
        if name not in KNOWN_USAGE or not _number(value) or value < 0:
            raise FeatureError("usage inválido")
    normalized = dict(payload)
    normalized["usage"] = dict(usage)
    return normalized


def _selection_from_json(data: object) -> dict[str, str] | None:
    selection = data.get("modelSelection") if isinstance(data, dict) else None
    if not isinstance(selection, dict):
        return None
    provider = selection.get("providerId")
    model = selection.get("modelId")
    options = selection.get("options")
    effort = options.get("reasoningLevel") if isinstance(options, dict) else None
    if provider not in allowed_providers() or not isinstance(model, str) or not isinstance(effort, str):
        raise FeatureError("seleção efetiva inválida")
    display = None
    for canonical, name in PROVIDER_MODELS.items():
        if model.casefold() in {canonical.casefold(), name.casefold()}:
            display = name
            break
    if display is None or effort not in EFFORTS:
        raise FeatureError("seleção efetiva inválida")
    return {"providerId": provider, "modelId": display, "effort": effort}


def parse_turn_request(response: object) -> dict | None:
    """Extrai um pedido de instrução do protocolo de turnos, se houver.

    O marcador só é honrado quando é a última declaração da resposta; qualquer
    conteúdo depois do JSON invalida o pedido (o worker não pode emendar texto
    próprio após pedir instrução).
    """

    if not isinstance(response, str) or TURN_MARKER not in response:
        return None
    head, _, tail = response.rpartition(TURN_MARKER)
    tail = tail.strip()
    if not tail:
        raise FeatureError("pedido de instrução sem pergunta")
    try:
        request = json.loads(tail)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise FeatureError("pedido de instrução malformado") from None
    if not isinstance(request, dict):
        raise FeatureError("pedido de instrução malformado")
    question = request.get("question")
    options = request.get("options", [])
    if not isinstance(question, str) or not question.strip():
        raise FeatureError("pedido de instrução sem pergunta")
    if len(question.encode("utf-8")) > TURN_MAX_QUESTION_BYTES:
        raise FeatureError("pedido de instrução excede o limite")
    if not isinstance(options, list) or len(options) > TURN_MAX_OPTIONS:
        raise FeatureError("opções de instrução inválidas")
    cleaned_options = []
    for option in options:
        if not isinstance(option, str) or not option.strip() or len(option.encode("utf-8")) > 1024:
            raise FeatureError("opções de instrução inválidas")
        cleaned_options.append(option.strip())
    return {"question": question.strip(), "options": cleaned_options}


def effective_selection(session_id: str, db_path: str | os.PathLike[str] | None = None) -> dict[str, str] | None:
    """Lê a seleção efetiva da sessão em SQLite somente leitura."""

    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        raise FeatureError("sessão inválida")
    path = os.fspath(db_path or base.DB)
    if not os.path.exists(path):
        return None
    connection = None
    try:
        uri = f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
        row = connection.execute(
            "select data from session_entry where session_id=? and type=? "
            "order by time_created desc limit 1",
            (session_id, "runtime/model_selection"),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except (TypeError, ValueError):
            raise FeatureError("seleção efetiva inválida") from None
        return _selection_from_json(data)
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()


def verify_effective(
    payload: dict,
    model: str,
    effort: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
    provider: str | None = None,
) -> dict[str, str]:
    """Compara o pedido exclusivamente com a seleção persistida da sessão."""

    expected = requested_selection(model, effort, provider)
    session_id = payload.get("sessionId") if isinstance(payload, dict) else None
    actual = effective_selection(session_id, db_path=db_path) if session_id else None
    if actual is None:
        raise FeatureError("seleção efetiva ausente ou divergente")
    if actual != expected:
        if (
            expected.get("providerId") == START_PLAN_PROVIDER_ID
            and actual.get("providerId") == PROVIDER_ID
            and actual.get("modelId") == expected.get("modelId")
            and actual.get("effort") == expected.get("effort")
        ):
            return actual
        raise FeatureError("seleção efetiva ausente ou divergente")
    return actual


def _purge_stale_provider_files(directory: Path) -> None:
    """Remove best-effort órfãos provider-*.json mais velhos que o TTL.

    Só arquivos regulares do diretório informado, donos do uid atual; symlinks
    e qualquer OSError são ignorados. Nunca alcança outros diretórios.
    """

    cutoff = time.time() - PROVIDER_TMP_TTL_SECONDS
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith("provider-") or not name.endswith(".json"):
            continue
        try:
            info = (directory / name).lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mtime > cutoff:
            continue
        with contextlib.suppress(OSError):
            os.unlink(directory / name)


@contextlib.contextmanager
def temporary_provider(model: str, effort: str, *, state_dir: str | os.PathLike[str] | None = None, provider: str | None = None):
    """Cria e remove um provider temporário privado com modo 0600."""

    selection_for(None, model, effort)
    if provider is not None and provider not in allowed_providers():
        raise FeatureError("provider inválido", "config")
    state = Path(state_dir or base.STATE)
    if not state.is_absolute():
        raise FeatureError("estado local inválido", "infra")
    directory = state / "tmp"

    def ensure_private_directory(path: Path, *, create: bool, repair_mode: bool = False) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                # Ramo interno defensivo: create=False não é usado por nenhum
                # chamador atual da closure.
                raise FeatureError("estado local ilegível", "infra") from None  # pragma: no cover
            try:
                path.mkdir(mode=0o700)
                info = path.lstat()
            except (FileExistsError, OSError):
                raise FeatureError("estado local ilegível", "infra") from None
        except OSError:
            raise FeatureError("estado local ilegível", "infra") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise FeatureError("estado local não é privado", "infra")
        if stat.S_IMODE(info.st_mode) != 0o700:
            if not repair_mode:
                raise FeatureError("estado local não é privado", "infra")
            try:
                os.chmod(path, 0o700)
            except OSError:
                raise FeatureError("estado local não é privado", "infra") from None

    # O core pode criar STATE/slots via os.makedirs antes desta etapa; depois
    # de validar dono/tipo sem seguir symlink, endurecemos apenas esse diretório.
    ensure_private_directory(state, create=True, repair_mode=True)
    ensure_private_directory(directory, create=True)
    # Processos mortos (SIGKILL/timeout) nunca passam pelo finally e deixam
    # órfãos provider-*.json; limpamos os vencidos logo antes de criar o novo.
    _purge_stale_provider_files(directory)
    try:
        config = base.carregar_config_provider(model, effort)
        if provider is not None:
            if isinstance(config.get("config"), dict):
                config["config"]["defaultModelSelection"]["providerId"] = provider
            else:
                config["defaultModelSelection"]["providerId"] = provider
        fd, path = tempfile.mkstemp(prefix="provider-", suffix=".json", dir=directory)
    except OSError:
        raise FeatureError("não foi possível preparar provider temporário", "infra") from None
    except base.WorkerError:
        raise FeatureError("provider temporário ilegível", "config") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(copy.deepcopy(config), target)
        yield path
    except FeatureError:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    except (OSError, ValueError, TypeError):
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise FeatureError("provider temporário ilegível") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def build_cli_argv(
    task: str,
    cwd: str,
    mode: str,
    model: str,
    effort: str,
    *,
    resume: str | None = None,
    attachments: list[str] | tuple[str, ...] = (),
    disallowed_tools: list[str] | tuple[str, ...] = (),
    cli_path: str | None = None,
    conversation: bool = False,
) -> list[str]:
    """Monta argv sem shell e aplica a política de ferramentas por modo."""

    selection_for(None, model, effort)
    if mode not in {"plan", "edit", "yolo"} or not os.path.isabs(cwd):
        raise FeatureError("contexto da tarefa inválido")
    if not isinstance(task, str) or not task or "\x00" in task:
        raise FeatureError("tarefa inválida")
    extra = validate_denylist(disallowed_tools)
    if mode == "edit":
        tools = list(base._headless_tools()) + extra
    else:
        tools = extra
    prompt = base.PREAMBULO + task
    if conversation:
        prompt += CONVERSATION_PROTOCOL_INSTRUCTION
    argv = [
        cli_path or base.CLI,
        "-p",
        prompt,
        "--cwd",
        cwd,
        "--mode",
        mode,
        "--json",
        "--no-color",
    ]
    if resume:
        if not SESSION_RE.fullmatch(resume):
            raise FeatureError("sessão inválida")
        argv.extend(["--resume", resume])
    for attachment in validate_attachments(list(attachments)):
        argv.extend(["--attach", attachment])
    if tools:
        argv.extend(["--disallowed-tools", *dict.fromkeys(tools)])
    return argv


def public_namespace(args: argparse.Namespace) -> SimpleNamespace:
    """Normaliza namespaces de testes e do parser sem copiar conteúdo de tarefa."""

    model, effort = selection_for(args.profile, args.model, args.effort)
    values = dict(vars(args))
    values["model"] = model
    values["effort"] = effort
    timeout = values.get("timeout", 1800.0)
    queue_timeout = values.get("queue_timeout")
    total_timeout = values.get("total_timeout")
    for name, value in (("timeout", timeout), ("queue_timeout", queue_timeout), ("total_timeout", total_timeout)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
            raise FeatureError("prazo inválido")
    values["timeout"] = timeout
    values["queue_timeout"] = 30.0 if queue_timeout is None else queue_timeout
    values["attachments"] = validate_attachments(getattr(args, "attach", []))
    values["disallowed_tools"] = validate_denylist(getattr(args, "disallowed_tools", []))
    max_output_bytes = getattr(args, "max_output_bytes", None)
    if max_output_bytes is None:
        max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 0 < max_output_bytes <= DEFAULT_MAX_OUTPUT_BYTES
    ):
        raise FeatureError("limite de saída inválido")
    values["max_output_bytes"] = max_output_bytes
    values["conversation"] = bool(getattr(args, "conversation", False))
    return SimpleNamespace(**values)


def configure(core):
    """Atualiza o módulo de core usado pela cápsula instalada."""

    global base, _CORE, PROVIDER_ID, MODELS, EFFORTS, PROVIDER_MODELS, TASK_MAX_BYTES
    if core is None:
        return _CORE
    required = ("PROVIDER_ID", "MODELS", "EFFORTS", "PROVIDER_MODELS", "TASK_MAX_BYTES")
    if any(not hasattr(core, name) for name in required):
        raise FeatureError("core incompatível", "infra")
    base = core
    _CORE = core
    PROVIDER_ID = core.PROVIDER_ID
    MODELS = tuple(core.MODELS)
    EFFORTS = tuple(core.EFFORTS)
    PROVIDER_MODELS = dict(core.PROVIDER_MODELS)
    TASK_MAX_BYTES = int(core.TASK_MAX_BYTES)
    return core
