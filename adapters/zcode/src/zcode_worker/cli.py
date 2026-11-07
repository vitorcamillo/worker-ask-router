"""Engine seguro e launcher da cápsula de features do zcode-worker."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from . import base, features


class OutputLimitExceeded(features.FeatureError):
    """A saída do processo ultrapassou o limite solicitado."""

    def __init__(self):
        super().__init__("saída do processo excedeu o limite", "output_limit")


_EFFECTS_WARNING = "Arquivos podem ter sido alterados; revise o diff antes de tentar novamente."
_AUTH_REQUIRED_RE = re.compile(
    r"(?:project_token_login_failed|login_required|business_session_storage_failed)"
)
_AUTH_REQUIRED_MESSAGE = "autenticação necessária"
_LAUNCHER_INCOMPATIBLE_RE = re.compile(
    r"auth launcher patch drift: (business request|resolver store|login store"
    r"|business invalidation|native credential CAS|async bundle lifecycle)\n?"
)
_LAUNCHER_INCOMPATIBLE_MESSAGE = "launcher de autenticacao incompativel com o bundle do ZCode"
QUOTA_RETRY_DEFAULT_SECONDS = 3600
START_PLAN_RETRY_DELAYS = (15, 30)
WORKER_SESSION_TTL_SECONDS = 6 * 3600


def _kill(process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=0.25)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=0.25)


def _invoke_limited(command, env, deadline, max_output_bytes, on_chunk=None):
    """Executa argv sem shell, limitando a coleta durante a leitura."""

    max_output_bytes = max_output_bytes or features.DEFAULT_MAX_OUTPUT_BYTES

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
            start_new_session=True,
        )
    except (OSError, ValueError):
        raise features.FeatureError("não foi possível iniciar zcode-cli", "infra") from None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    total = 0
    try:
        assert process.stdout is not None and process.stderr is not None
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        selector.register(stdout_fd, selectors.EVENT_READ, stdout)
        selector.register(stderr_fd, selectors.EVENT_READ, stderr)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise features.FeatureError("prazo de execução esgotado", "timeout")
            events = selector.select(min(remaining, 0.25))
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                total += len(chunk)
                if total > max_output_bytes:
                    raise OutputLimitExceeded()
                key.data.extend(chunk)
                if on_chunk is not None:
                    stream = "stdout" if key.fd == stdout_fd else "stderr"
                    with contextlib.suppress(Exception):
                        on_chunk(stream, len(chunk))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise features.FeatureError("prazo de execução esgotado", "timeout")
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        raise features.FeatureError("prazo de execução esgotado", "timeout") from None
    finally:
        # O líder pode ter saído enquanto um descendente ainda mantinha os
        # pipes abertos. O cleanup do grupo encerra quaisquer descendentes
        # ainda vivos depois de aguardar o líder dentro do prazo.
        _kill(process)
        selector.close()
        with contextlib.suppress(OSError):
            process.stdout.close() if process.stdout else None
        with contextlib.suppress(OSError):
            process.stderr.close() if process.stderr else None
    return process.returncode, bytes(stdout).decode("utf-8", "replace"), bytes(stderr).decode("utf-8", "replace")


def _invoke(command, env, deadline, max_output_bytes=None, on_chunk=None):
    """Executa o CLI com teto de saída também no caminho padrão."""

    limit = max_output_bytes or features.DEFAULT_MAX_OUTPUT_BYTES
    return _invoke_limited(command, env, deadline, limit, on_chunk)


def _error_result(
    error: Exception,
    duration: float,
    *,
    code: int = 1,
    effects_may_exist: bool = False,
) -> tuple[dict, int]:
    deadline_error = getattr(base, "DeadlineExpired", ())
    if deadline_error and isinstance(error, deadline_error):
        error_code = "timeout"
        message = "prazo de execução esgotado"
    elif isinstance(error, features.FeatureError):
        error_code = error.code
        message = str(error)
    elif isinstance(error, getattr(base, "WorkerError", ())):
        # WorkerError do core já nasce com mensagem segura (sem saída bruta do CLI);
        # mascará-la como "falha interna" escondia a causa real (ex.: cwd inválido).
        quota_error = getattr(base, "QuotaError", ())
        error_code = "quota" if quota_error and isinstance(error, quota_error) else "worker"
        message = str(error) or "falha do worker"
    else:
        error_code = "internal"
        message = "falha interna segura"
    effects_may_exist = bool(effects_may_exist)
    if effects_may_exist:
        message = f"{message} {_EFFECTS_WARNING}"
    if error_code == "timeout":
        code = 124
    elif error_code == "quota":
        code = 75
    return (
        {
            "schemaVersion": 1,
            "status": "error",
            "exitCode": code,
            "sessionId": None,
            "requested": None,
            "effective": None,
            "usage": {},
            "durationSeconds": round(max(0.0, duration), 3),
            "response": None,
            "error": {"code": error_code, "message": message},
            "effectsMayExist": effects_may_exist,
        },
        code,
    )


def _emit(event_sink, event: dict) -> None:
    if event_sink is None:
        return
    enriched = dict(event)
    enriched.setdefault("ts", time.time())
    with contextlib.suppress(Exception):
        event_sink(enriched)


def _chunk_sink(event_sink):
    """Converte leituras do invoke em eventos de contagem, sem conteúdo."""

    if event_sink is None:
        return None

    def on_chunk(stream, size):
        event_sink({"kind": "cli_output", "stream": stream, "bytes": size})

    return on_chunk


def _classify_process_failure(returncode: int, stderr: str) -> features.FeatureError:
    # O conteúdo só é usado para classificação local; nunca é devolvido ao operador.
    if returncode == 124:
        return features.FeatureError("prazo de execução esgotado", "timeout")
    if returncode == 1 and re.fullmatch(r"auth launcher start-plan drift\n?", stderr or ""):
        return features.FeatureError("Start Plan indisponível no bundle do ZCode", "start_plan_drift")
    # Antes das checagens de cota/limite: esse diagnóstico nunca pode sair como
    # quota (exit 75); a mensagem é estática e o stderr não é devolvido.
    if returncode == 1 and _LAUNCHER_INCOMPATIBLE_RE.fullmatch(stderr or ""):
        return features.FeatureError(_LAUNCHER_INCOMPATIBLE_MESSAGE, "launcher_incompatible")
    if base.QUOTA_RE.search(stderr or ""):
        return features.FeatureError("cota do ZCode esgotada", "quota")
    if base.RATE_RE.search(stderr or ""):
        return features.FeatureError("limite temporário do ZCode", "rate_limit")
    if returncode in {-signal.SIGTERM, -signal.SIGKILL}:
        return features.FeatureError("processo terminado", "terminated")
    if "business_session_storage_failed" in (stderr or ""):
        return features.FeatureError("não foi possível persistir a sessão", "auth_storage_failed")
    if _AUTH_REQUIRED_RE.search(stderr or ""):
        return features.FeatureError(_AUTH_REQUIRED_MESSAGE, "auth_required")
    return features.FeatureError("zcode-cli falhou", "cli_failed")


def _session_cwd_matches(session_id: str, cwd: str, db_path=None) -> bool:
    path = os.fspath(db_path or base.DB)
    if not os.path.exists(path):
        return False
    import sqlite3
    from urllib.parse import quote

    connection = None
    try:
        uri = f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
        table = connection.execute(
            "select name from sqlite_master where type='table' and name='session'"
        ).fetchone()
        if not table:
            return False
        columns = {row[1] for row in connection.execute("pragma table_info(session)")}
        key = "session_id" if "session_id" in columns else "id" if "id" in columns else None
        location = (
            "cwd" if "cwd" in columns
            else "workspace" if "workspace" in columns
            else "directory" if "directory" in columns
            else None
        )
        if not key or not location:
            return False
        row = connection.execute(
            f"select {location} from session where {key}=? limit 1", (session_id,)
        ).fetchone()
        return bool(row and isinstance(row[0], str) and os.path.realpath(row[0]) == os.path.realpath(cwd))
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _verify_resume(args, db_path=None) -> None:
    if not args.resume:
        return
    if not _session_cwd_matches(args.resume, args.cwd, db_path=db_path):
        raise features.FeatureError("sessão não pertence ao cwd informado", "resume_mismatch")
    stored = features.effective_selection(args.resume, db_path=db_path)
    expected = features.requested_selection(
        args.model, args.effort, getattr(args, "_provider", None)
    )
    # Mesma regra de verify_effective: só o rebaixamento silencioso de Start
    # Plan para o provider pago é tolerado. No sentido contrário a sessão
    # manteria o provider persistido e falharia só depois de executar, com
    # arquivos já alterados; a troca é resolvida aqui, antes de rodar.
    provider_ok = stored is not None and (
        stored.get("providerId") == expected.get("providerId")
        or (
            expected.get("providerId") == features.START_PLAN_PROVIDER_ID
            and stored.get("providerId") == features.PROVIDER_ID
        )
    )
    if (
        stored is None
        or not provider_ok
        or stored.get("modelId") != expected.get("modelId")
        or stored.get("effort") != expected.get("effort")
    ):
        raise features.FeatureError("seleção da sessão diverge do pedido", "resume_mismatch")


def _retry_allowed(args, error, attempt, deadline):
    if args.mode != "plan" or not isinstance(error, features.FeatureError) or error.code != "rate_limit":
        return False
    if getattr(args, "_provider", None) == features.START_PLAN_PROVIDER_ID:
        # Rate limit do Start em plan é transitório (concorrência): tentar de
        # novo com atrasos curtos antes de liberar o fallback pago. Sem prazo
        # para o próximo atraso, devolve False para o fallback pagar rodar em
        # vez de o erro virar timeout.
        delays = START_PLAN_RETRY_DELAYS
        if attempt >= len(delays):
            return False
        delay = float(delays[attempt])
        if time.monotonic() + delay >= deadline:
            return False
        time.sleep(delay)
        return True
    delays = tuple(base.RETRY_DELAYS)
    if attempt >= len(delays):
        return False
    delay = float(delays[attempt])
    if time.monotonic() + delay >= deadline:
        raise features.FeatureError("prazo esgotado durante retry", "timeout")
    time.sleep(delay)
    return True


def _execute(args, task, *, db_path=None, runner=None, total_deadline=None, event_sink=None):
    start = time.monotonic()
    # Estado interno: run_task o reinicializa após a normalização pública.
    args._effects_may_exist = False
    total_deadline = total_deadline or (start + (args.total_timeout or args.timeout + args.queue_timeout))
    queue_deadline = min(start + args.queue_timeout, total_deadline)
    _verify_resume(args, db_path=db_path)

    def invoke(command, env, deadline, limit):
        return _invoke(command, env, deadline, limit, on_chunk=_chunk_sink(event_sink))

    try:
        slot_context = base.global_slot(queue_deadline, total_deadline)
    except TypeError:
        # Dubles antigos de global_slot aceitam só o prazo da fila.
        slot_context = base.global_slot(queue_deadline)
    with slot_context:
        execution_deadline = min(time.monotonic() + args.timeout, total_deadline)
        _provider = getattr(args, "_provider", None)
        _provider_ctx = features.temporary_provider(args.model, args.effort, provider=_provider)
        with _provider_ctx as provider_path:
            env = dict(os.environ)
            env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = provider_path
            env["ZCODE_DATA_BASE_DIR"] = base.AUTH_BASE
            if _provider == features.START_PLAN_PROVIDER_ID:
                env["ZCODE_WORKER_START_PLAN_PATCHES"] = "1"
            else:
                env.pop("ZCODE_WORKER_START_PLAN_PATCHES", None)
            command = features.build_cli_argv(
                task,
                args.cwd,
                args.mode,
                args.model,
                args.effort,
                resume=args.resume,
                attachments=args.attachments,
                disallowed_tools=args.disallowed_tools,
                conversation=bool(getattr(args, "conversation", False)),
                chat=bool(getattr(args, "chat", False)),
            )
            # O loop sempre termina por return ou raise: na última tentativa
            # _retry_allowed devolve False e o erro é propagado.
            _retry_budget = max(len(base.RETRY_DELAYS), len(START_PLAN_RETRY_DELAYS))
            for attempt in range(_retry_budget + 1):  # pragma: no branch
                try:
                    if args.mode in {"edit", "yolo"}:
                        # A CLI pode ter escrito antes de devolver um erro ou
                        # de o wrapper validar a resposta.
                        args._effects_may_exist = True
                    response = (runner or invoke)(
                        command, env, execution_deadline, args.max_output_bytes
                    )
                    if isinstance(response, tuple) and len(response) == 3 and response[0] != 0:
                        _delete_session_from_stdout(response[1], db_path=db_path, keep=(args.resume,))
                        raise _classify_process_failure(response[0], response[2])
                    return response
                except features.FeatureError as error:
                    if _retry_allowed(args, error, attempt, execution_deadline):
                        continue
                    raise


@contextlib.contextmanager
def _termination_handlers():
    """Converte TERM/INT/HUP em Terminated e sempre restaura o processo."""

    handlers = {}

    def terminate(signum, _frame):
        raise base.Terminated(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, terminate)
    except (AttributeError, ValueError):
        # Chamadas em thread não podem instalar handlers; o core ainda limpa
        # processos pelos finally do executor.
        for signum, handler in handlers.items():
            with contextlib.suppress(Exception):
                signal.signal(signum, handler)
        handlers.clear()
    try:
        yield
    finally:
        for signum, handler in handlers.items():
            with contextlib.suppress(Exception):
                signal.signal(signum, handler)


def _terminated_result(
    error: Exception,
    duration: float,
    *,
    effects_may_exist: bool = False,
) -> tuple[dict, int]:
    signum = int(getattr(error, "signum", signal.SIGTERM))
    code = 128 + signum
    effects_may_exist = bool(effects_may_exist)
    message = "execução interrompida"
    if effects_may_exist:
        message = f"{message} {_EFFECTS_WARNING}"
    return (
        {
            "schemaVersion": 1,
            "status": "cancelled",
            "exitCode": code,
            "sessionId": None,
            "requested": None,
            "effective": None,
            "usage": {},
            "durationSeconds": round(max(0.0, duration), 3),
            "response": None,
            "error": {"code": "terminated", "message": message},
            "effectsMayExist": effects_may_exist,
        },
        code,
    )


def _delete_session(session_id: str, *, db_path=None) -> bool:
    """Apaga a própria sessão do worker ao finalizar (best-effort).

    Só toca em sessões cujo título começa com o preâmbulo de worker, para
    nunca apagar sessão interativa do usuário. O cascade via
    ``PRAGMA foreign_keys=ON`` remove message/part/session_entry; os filhos
    (``parent_id``) são apagados explicitamente antes do pai. Falha é apenas
    ignorada.
    """
    from urllib.parse import quote

    import sqlite3

    if not isinstance(session_id, str) or not features.SESSION_RE.fullmatch(session_id):
        return False
    path = os.fspath(db_path or base.DB)
    if not os.path.exists(path):
        return False
    connection = None
    try:
        uri = f"file:{quote(os.path.abspath(path), safe='/')}?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute(
            "select title from session where id=?", (session_id,)
        ).fetchone()
        if not row or not str(row[0]).startswith("[WORKER"):
            return False
        with connection:
            connection.execute("delete from session where parent_id=?", (session_id,))
            connection.execute(
                "delete from session where id=? and title like '[WORKER%'",
                (session_id,),
            )
        return True
    except (OSError, sqlite3.Error, ValueError):
        return False
    finally:
        if connection is not None:
            connection.close()


def _delete_session_from_stdout(stdout: str, *, db_path=None, keep=()) -> bool:
    """Extrai o sessionId do stdout e apaga a sessão de uma tentativa falha.

    A sessão retomada via ``--resume`` (id válido em ``keep``) é preservada:
    o stdout da tentativa devolve esse mesmo id, e apagá-lo destruiria o
    histórico pendente e invalidaria o retry, que reutiliza o id.
    """

    keep_ids = {sid for sid in (keep or ()) if isinstance(sid, str)}
    with contextlib.suppress(Exception):
        payload = features.decode_response(stdout)
        if isinstance(payload, dict):
            session_id = payload.get("sessionId")
            if session_id in keep_ids:
                return False
            return bool(_delete_session(session_id, db_path=db_path))
    return False


_CONTENT_COLUMNS = ("data", "content", "text")
_PART_SESSION_COLUMNS = ("session_id", "sessionId", "sessionid")
_PART_MESSAGE_COLUMNS = ("message_id", "messageId", "messageid")
_MESSAGE_ID_COLUMNS = ("id", "message_id", "messageId")


def _pending_session_ids(connection) -> set[str] | None:
    """Sessões com marcador de turno na mensagem ou nas partes da resposta.

    O texto pode estar na própria ``message`` ou em ``part`` (com
    ``message.data`` guardando só metadados); as colunas são descobertas por
    pragma, schema defensivo. Parte com ``session_id`` NULL é resolvida pelo
    vínculo com ``message``; devolve ``None`` quando há conteúdo em
    ``message`` ou ``part`` que não pode ser atribuído a uma sessão (sessão
    NULL na mensagem, sem colunas de vínculo, message_id NULL ou mensagem
    inexistente/sem sessão): a pendência fica indeterminável e o expurgo deve
    preservar todas as sessões.
    """
    message_columns = {
        row[1] for row in connection.execute("pragma table_info(message)")
    }
    part_columns = {row[1] for row in connection.execute("pragma table_info(part)")}
    message_content = next(
        (name for name in _CONTENT_COLUMNS if name in message_columns), None
    )
    part_content = next(
        (name for name in _CONTENT_COLUMNS if name in part_columns), None
    )
    pending = set()
    if message_content is not None:
        rows = connection.execute(
            f"select distinct session_id from message"
            f" where instr({message_content}, ?) > 0",
            (features.TURN_MARKER,),
        ).fetchall()
        # Marcador em mensagem com session_id NULL não atribui a pendência a
        # nenhuma sessão; {None} no conjunto deixava o expurgo apagar sessões.
        if any(row[0] is None for row in rows):
            return None
        pending |= {row[0] for row in rows}
    if part_content is None:
        return pending
    part_session = next(
        (name for name in _PART_SESSION_COLUMNS if name in part_columns), None
    )
    part_message = next(
        (name for name in _PART_MESSAGE_COLUMNS if name in part_columns), None
    )
    message_id = next(
        (name for name in _MESSAGE_ID_COLUMNS if name in message_columns), None
    )
    # O vínculo pela mensagem resolve também parte com session_id NULL; sem
    # as colunas de vínculo (ou sem session_id na message), NULL segue
    # indeterminável e preserva todas as sessões.
    linked = (
        part_message is not None
        and message_id is not None
        and "session_id" in message_columns
    )
    if part_session is not None:
        if linked:
            rows = connection.execute(
                f"select distinct coalesce(p.{part_session}, m.session_id)"
                f" from part p left join message m"
                f" on m.{message_id} = p.{part_message}"
                f" where instr(p.{part_content}, ?) > 0",
                (features.TURN_MARKER,),
            ).fetchall()
            if any(row[0] is None for row in rows):
                return None
            pending |= {row[0] for row in rows}
            return pending
        rows = connection.execute(
            f"select distinct {part_session} from part"
            f" where instr({part_content}, ?) > 0",
            (features.TURN_MARKER,),
        ).fetchall()
        if any(row[0] is None for row in rows):
            return None
        pending |= {row[0] for row in rows}
        return pending
    if part_message is None or message_id is None:
        return None
    # LEFT JOIN mantém a parte sem mensagem correspondente (message_id NULL
    # ou inexistente); sessão NULL na mensagem também é indeterminável.
    rows = connection.execute(
        f"select distinct m.session_id from part p"
        f" left join message m on m.{message_id} = p.{part_message}"
        f" where instr(p.{part_content}, ?) > 0",
        (features.TURN_MARKER,),
    ).fetchall()
    if any(row[0] is None for row in rows):
        return None
    pending |= {row[0] for row in rows}
    return pending


def _purge_stale_worker_sessions(
    *, db_path=None, older_than_seconds=WORKER_SESSION_TTL_SECONDS, keep=()
) -> int:
    """Apaga sessões ``[WORKER...]`` desatualizadas e seus filhos (best-effort).

    Sessões pendentes (``need_input`` aguardando resposta, mesmo além do TTL) e
    as ids válidas em ``keep`` (ex.: a sessão alvo de ``--resume``) são
    preservadas, junto com os ancestrais delas: o pai expirado não pode
    apagar a filha protegida. Retorna quantas sessões foram apagadas; falha
    devolve 0.
    """
    from urllib.parse import quote

    import sqlite3

    try:
        threshold = float(older_than_seconds)
    except (TypeError, ValueError):
        return 0
    path = os.fspath(db_path or base.DB)
    if not os.path.exists(path):
        return 0
    connection = None
    try:
        uri = f"file:{quote(os.path.abspath(path), safe='/')}?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        cutoff = int((time.time() - threshold) * 1000)
        keep_ids = {
            sid
            for sid in keep
            if isinstance(sid, str) and features.SESSION_RE.fullmatch(sid)
        }
        # A transação de escrita começa antes das leituras: sem o BEGIN
        # IMMEDIATE, outra conexão podia registrar pendência depois da
        # seleção/detecção e antes da exclusão, que seguiria com ids antigos.
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "select id from session where title like '[WORKER%' and time_updated < ?",
            (cutoff,),
        ).fetchall()
        ids = [row[0] for row in rows if row[0] not in keep_ids]
        # Sessão need_input aguarda resposta do orquestrador por tempo
        # indeterminado; o marcador de turno é o traço durável de que a
        # conversa segue viva e não pode ser expurgada pelo TTL. O marcador é
        # procurado na mensagem e nas partes da resposta; sem coluna de
        # conteúdo em nenhuma das duas, nenhuma sessão é considerada
        # pendente. Pendência indeterminável preserva todas as sessões.
        pending = set()
        if ids:
            detected = _pending_session_ids(connection)
            if detected is None:
                return 0
            pending = detected
            ids = [sid for sid in ids if sid not in pending]
        # Uma filha protegida (keep/need_input) não pode morrer junto com o
        # pai expirado: preserve-a e preserve também os ancestrais dela, ou o
        # delete do pai — direto ou pela cascade do schema real — apaga a
        # sessão que o expurgo prometeu manter.
        protected = tuple(keep_ids | pending)
        if ids and protected:
            seed = ",".join("?" for _ in protected)
            preserve = {
                row[0]
                for row in connection.execute(
                    "with recursive ancestors(node) as ("
                    f"select id from session where id in ({seed})"
                    " union select s.parent_id from session s"
                    " join ancestors a on s.id = a.node"
                    ") select distinct node from ancestors where node is not null",
                    protected,
                )
            }
            ids = [sid for sid in ids if sid not in preserve]
        if not ids:
            return 0
        with connection:
            connection.executemany(
                "delete from session where parent_id=?", [(sid,) for sid in ids]
            )
            connection.executemany(
                "delete from session where id=? and title like '[WORKER%'",
                [(sid,) for sid in ids],
            )
        return len(ids)
    except (OSError, sqlite3.Error, ValueError):
        return 0
    finally:
        if connection is not None:
            connection.close()


def _probe_start_plan(args, *, db_path=None, runner=None, total_deadline=None):
    """Sonda o Start Plan em modo plan antes de edit/yolo."""

    probe_args = copy.copy(args)
    probe_args.mode = "plan"
    probe_args.resume = None
    probe_args.attachments = []
    probe_args.conversation = False
    probe_args.chat = False
    probe_args.timeout = min(args.timeout, 120)
    try:
        raw = _execute(
            probe_args,
            "Responda apenas: OK",
            db_path=db_path,
            runner=runner,
            total_deadline=total_deadline,
        )
    finally:
        with contextlib.suppress(Exception):
            args._effects_may_exist = False
    returncode, stdout, stderr = raw
    if returncode != 0:
        raise _classify_process_failure(returncode, stderr)
    payload = features.validate_response(features.decode_response(stdout))
    effective = features.verify_effective(
        payload, probe_args.model, probe_args.effort, db_path=db_path, provider=features.START_PLAN_PROVIDER_ID
    )
    _delete_session(payload.get("sessionId"), db_path=db_path)
    return payload, effective


def _quota_retry_after(args, now=None) -> float:
    now_value = time.time() if now is None else float(now)
    default = now_value + QUOTA_RETRY_DEFAULT_SECONDS
    try:
        coming = features.start_plan_next_available(getattr(args, "model", None))
    except Exception:
        return default
    candidate = coming.timestamp() if coming is not None else None
    if candidate is not None and candidate > now_value and candidate < default:
        return max(float(candidate), now_value + 60.0)
    return default


def run_task(args: SimpleNamespace, task: str, *, db_path=None, runner=None, event_sink=None) -> tuple[dict, int]:
    """Valida e executa uma tarefa, retornando ``(resultado, exit_code)``."""

    started = time.monotonic()
    normalized_args = None
    try:
        with _termination_handlers():
            normalized_args = features.public_namespace(args)
            # Não herdar campos privados de um Namespace externo: a política
            # nasce após a normalização e só _execute pode marcá-la.
            normalized_args._effects_may_exist = False
            args = normalized_args
            if not isinstance(args.cwd, str) or not os.path.isabs(args.cwd) or not os.path.isdir(args.cwd):
                raise features.FeatureError("cwd inválido")
            if args.total_timeout is not None and args.total_timeout <= 0:
                # Redundante com a validação de public_namespace (todo valor
                # <= 0 já foi recusado); defesa contra namespaces externos.
                raise features.FeatureError("prazo total inválido")  # pragma: no cover
            task = task if isinstance(task, str) else ""
            if not task.strip() or "\x00" in task or len(task.encode("utf-8")) > features.TASK_MAX_BYTES:
                raise features.FeatureError("tarefa inválida")
            args._provider = features.provider_for(args.model)
            requested = features.requested_selection(args.model, args.effort, args._provider)
            total_deadline = started + (args.total_timeout or args.timeout + args.queue_timeout)
            _emit(event_sink, {
                "kind": "turn_start",
                "mode": args.mode,
                "model": requested.get("modelId"),
                "effort": requested.get("effort"),
                "conversation": bool(getattr(args, "conversation", False)),
            })

            def _finish(payload, _requested, _effective, _fallback):
                _pending = None
                if getattr(args, "conversation", False):
                    # Levanta FeatureError controlada se o pedido vier malformado.
                    _pending = features.parse_turn_request(payload["response"])
                    if _pending is not None:
                        payload["response"] = payload["response"].rpartition(features.TURN_MARKER)[0].rstrip()
                _status = "need_input" if _pending is not None else "ok"
                _exit_code = 2 if _pending is not None else 0
                if _status == "ok":
                    # Fatia encerrada: apaga a sessão do worker para não acumular.
                    _delete_session(payload.get("sessionId"), db_path=db_path)
                    if _effective.get("providerId") == features.START_PLAN_PROVIDER_ID:
                        features.mark_start_plan_confirmed()
                _emit(event_sink, {"kind": "turn_end", "status": _status, "exitCode": _exit_code})
                _result = {
                    "schemaVersion": 1,
                    "status": _status,
                    "exitCode": _exit_code,
                    "sessionId": payload["sessionId"],
                    "requested": _requested,
                    "effective": _effective,
                    "usage": payload.get("usage", {}),
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "response": payload["response"],
                    "auditoria": AVISO_AUDITORIA,
                }
                if _pending is not None:
                    _result["pending"] = _pending
                    _result["turnResume"] = payload["sessionId"]
                if _fallback is not None:
                    _result["startPlanFallback"] = _fallback
                return _result, _exit_code

            if args.dry_run:
                _verify_resume(args, db_path=db_path)
                features.build_cli_argv(
                    task,
                    args.cwd,
                    args.mode,
                    args.model,
                    args.effort,
                    resume=args.resume,
                    attachments=args.attachments,
                    disallowed_tools=args.disallowed_tools,
                    chat=bool(getattr(args, "chat", False)),
                )
                result = {
                    "schemaVersion": 1,
                    "status": "dry-run",
                    "exitCode": 0,
                    "sessionId": None,
                    "requested": requested,
                    "effective": None,
                    "usage": {},
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "response": "não executado",
                }
                return result, 0
            # Expurgo somente em execução real: uma simulação não pode apagar
            # sessões, e a sessão alvo de --resume precisa sobreviver à limpeza
            # para ser validada em vez de virar resume_mismatch.
            with contextlib.suppress(Exception):
                _purge_stale_worker_sessions(db_path=db_path, keep=(args.resume,))
            probe_fallback = None
            if (
                args._provider == features.START_PLAN_PROVIDER_ID
                and args.mode in {"edit", "yolo"}
                and not args.resume
                and not features.start_plan_confirmed()
            ):
                try:
                    _probe_payload, _probe_effective = _probe_start_plan(
                        args, db_path=db_path, runner=runner, total_deadline=total_deadline
                    )
                except features.FeatureError as _probe_error:
                    _probe_code = getattr(_probe_error, "code", None)
                    if _probe_code == "rate_limit":
                        features.mark_start_plan_unavailable("refused")
                    elif _probe_code == "quota":
                        features.mark_start_plan_unavailable("quota")
                    elif _probe_code == "start_plan_drift":
                        features.mark_start_plan_unavailable("drift")
                    else:
                        raise
                    args._provider = features.PROVIDER_ID
                    requested = features.requested_selection(args.model, args.effort, args._provider)
                    probe_fallback = "probe"
                else:
                    if _probe_effective.get("providerId") == features.START_PLAN_PROVIDER_ID:
                        features.mark_start_plan_confirmed()
                    else:
                        features.mark_start_plan_unavailable("mismatch")
                        args._provider = features.PROVIDER_ID
                        requested = features.requested_selection(args.model, args.effort, args._provider)
                        probe_fallback = "probe"
            try:
                raw = _execute(args, task, db_path=db_path, runner=runner, event_sink=event_sink, total_deadline=total_deadline)
                returncode, stdout, stderr = raw
                if returncode != 0:
                    _delete_session_from_stdout(stdout, db_path=db_path, keep=(args.resume,))
                    raise _classify_process_failure(returncode, stderr)
                try:
                    payload = features.validate_response(features.decode_response(stdout))
                    effective = features.verify_effective(payload, args.model, args.effort, db_path=db_path, provider=args._provider)
                except Exception:
                    _delete_session_from_stdout(stdout, db_path=db_path, keep=(args.resume,))
                    raise
                fallback = probe_fallback
                if args._provider == features.START_PLAN_PROVIDER_ID and effective.get("providerId") != args._provider:
                    features.mark_start_plan_unavailable("mismatch")
                    fallback = "mismatch"
                return _finish(payload, requested, effective, fallback)
            except features.FeatureError as _error:
                _is_start = getattr(args, "_provider", None) == features.START_PLAN_PROVIDER_ID
                _code = getattr(_error, "code", None)
                if not _is_start or _code not in ("start_plan_drift", "quota", "rate_limit"):
                    raise
                if _code == "start_plan_drift":
                    features.mark_start_plan_unavailable("drift")
                    _fallback = "drift"
                else:
                    if _code == "quota":
                        features.mark_start_plan_unavailable("quota")
                    elif _code == "rate_limit":
                        features.mark_start_plan_unavailable("rate_limit")
                    _fallback = _code
                    if args.mode != "plan":
                        raise features.FeatureError(
                            "Start Plan esgotado durante a fatia; revise o diff e relance (a próxima chamada usa o plano)",
                            "start_plan_exhausted",
                        ) from _error
                args._provider = features.PROVIDER_ID
                requested = features.requested_selection(args.model, args.effort, args._provider)
                raw = _execute(args, task, db_path=db_path, runner=runner, event_sink=event_sink, total_deadline=total_deadline)
                returncode, stdout, stderr = raw
                if returncode != 0:
                    _delete_session_from_stdout(stdout, db_path=db_path, keep=(args.resume,))
                    raise _classify_process_failure(returncode, stderr)
                try:
                    payload = features.validate_response(features.decode_response(stdout))
                    effective = features.verify_effective(payload, args.model, args.effort, db_path=db_path, provider=args._provider)
                except Exception:
                    _delete_session_from_stdout(stdout, db_path=db_path, keep=(args.resume,))
                    raise
                return _finish(payload, requested, effective, _fallback)
    except getattr(base, "Terminated", ()) as error:
        effects_may_exist = bool(
            normalized_args is not None
            and getattr(normalized_args, "_effects_may_exist", False)
        )
        _emit(event_sink, {"kind": "turn_end", "status": "cancelled", "exitCode": 128 + int(getattr(error, "signum", signal.SIGTERM))})
        return _terminated_result(
            error,
            time.monotonic() - started,
            effects_may_exist=effects_may_exist,
        )
    except Exception as error:
        effects_may_exist = bool(
            normalized_args is not None
            and getattr(normalized_args, "_effects_may_exist", False)
        )
        _emit(event_sink, {"kind": "turn_end", "status": "error", "exitCode": None})
        result, code = _error_result(
            error,
            time.monotonic() - started,
            effects_may_exist=effects_may_exist,
        )
        if code == 75:
            if normalized_args is None:
                retry_after = time.time() + QUOTA_RETRY_DEFAULT_SECONDS
            else:
                retry_after = _quota_retry_after(normalized_args)
            result["retryAfter"] = round(float(retry_after), 0)
        return result, code


def doctor() -> dict:
    """Exibe somente diagnóstico operacional sem conteúdo de configuração."""

    version = "indisponível"
    try:
        completed = subprocess.run(
            [base.CLI, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=5,
            check=False,
        )
        if completed.returncode == 0:
            text = completed.stdout if isinstance(completed.stdout, str) else ""
            match = re.search(r"(?m)^(?:zcode-cli\s+)?([0-9][A-Za-z0-9._+-]{0,31})\s*$", text, re.I)
            if match:
                version = match.group(1)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    try:
        memory = base._read_mem_available_bytes()
        memory_status = "adequada" if memory >= base.MIN_MEM_AVAILABLE else "abaixo do mínimo"
    except Exception:
        memory_status = "indisponível"
    try:
        active_cli = int(base.count_active_cli_processes())
    except Exception:
        active_cli = None
    try:
        max_parallel = int(base._read_settings())
    except Exception:
        max_parallel = None
    return {
        "schemaVersion": 1,
        "status": "doctor",
        "cli": {"path": os.path.abspath(base.CLI), "available": os.path.exists(base.CLI), "version": version},
        "paths": {
            "state": os.path.abspath(base.STATE),
            "db": os.path.abspath(base.DB),
            "settings": os.path.abspath(base.SETTINGS),
        },
        "memory": memory_status,
        "active_cli": active_cli,
        "max_parallel": max_parallel,
        "lock": {"activeCli": active_cli, "maxParallel": max_parallel},
        "cancel": {"pidfdAvailable": callable(getattr(os, "pidfd_open", None)) and callable(getattr(signal, "pidfd_send_signal", None))},
        "limitations": ["catálogo não é consultado", "a trava nativa da CLI continua valendo"],
    }


def list_models() -> dict:
    combinations = [
        {"providerId": features.PROVIDER_ID, "modelId": name, "effort": effort}
        for name in features.PROVIDER_MODELS.values()
        for effort in features.EFFORTS
    ]
    return {"schemaVersion": 1, "status": "models", "combinations": combinations}


def _dispatch_jobs(args, task):
    from . import jobs  # importação tardia para preservar o core instalado

    if args.status:
        result = jobs.read_status(args.status)
        return result, 0
    if args.cancel:
        result = jobs.cancel_job(args.cancel)
        return result, 0
    if not args.saida or task is None:
        raise features.FeatureError("arquivo de saída e tarefa são obrigatórios")
    normalized = features.public_namespace(args)
    options = {
        "cwd": normalized.cwd,
        "mode": normalized.mode,
        "model": normalized.model,
        "effort": normalized.effort,
        "profile": normalized.profile,
        "timeout": normalized.timeout,
        "queue_timeout": normalized.queue_timeout,
        "total_timeout": normalized.total_timeout,
        "max_output_bytes": normalized.max_output_bytes,
        "resume": normalized.resume,
        "attach": normalized.attachments,
        "disallowed_tools": normalized.disallowed_tools,
        "json": bool(normalized.json),
        "conversation": bool(getattr(normalized, "conversation", False)),
    }
    options = {key: value for key, value in options.items() if value is not None}
    if getattr(args, "max_output_bytes", None) is None:
        options.pop("max_output_bytes", None)
    if _INJECTED_ENTRYPOINT:
        python_executable = (
            os.path.realpath(sys.executable)
            if os.path.islink(sys.executable)
            else sys.executable
        )
        entrypoint = [python_executable, _INJECTED_ENTRYPOINT]
    else:
        launcher = Path(__file__).resolve().parents[2] / "bin" / "zcode-worker-next"
        entrypoint = [sys.executable, str(launcher)]
    result = jobs.start_job(options, task, args.saida, entrypoint)
    if not isinstance(result, dict):
        raise features.FeatureError("resultado de jobs inválido", "jobs_invalid")
    return result, 0


def _job_runner(options, task):
    """Reconstrói somente opções permitidas antes de chamar o engine."""

    values = {
        "tarefa": None,
        "cwd": options.get("cwd"),
        "mode": options.get("mode", "edit"),
        "spec": None,
        "timeout": options.get("timeout", 1800.0),
        "queue_timeout": options.get("queue_timeout"),
        "total_timeout": options.get("total_timeout"),
        "model": options.get("model"),
        "effort": options.get("effort"),
        "profile": options.get("profile"),
        "resume": options.get("resume"),
        "attach": options.get("attach", []),
        "disallowed_tools": options.get("disallowed_tools", []),
        "max_output_bytes": options.get("max_output_bytes") or features.DEFAULT_MAX_OUTPUT_BYTES,
        "json": bool(options.get("json", False)),
        "dry_run": False,
        "doctor": False,
        "list_models": False,
        "saida": None,
        "status": None,
        "cancel": None,
        "_job_child": None,
        "conversation": bool(options.get("conversation", False)),
    }
    event_sink = None
    try:
        from . import jobs as jobs_module

        event_sink = jobs_module.current_event_sink()
    except ImportError:  # pragma: no cover - jobs é parte do pacote
        pass
    return run_task(SimpleNamespace(**values), task, event_sink=event_sink)


AVISO_AUDITORIA = ("PENDENTE: saída de worker NÃO auditada. Quem chamou deve rodar os testes, "
                   "revisar o diff e reler os trechos citados antes de usar ou reportar.")


def _print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return
    if result.get("status") == "ok":
        print(result.get("response", ""))
        usage = result.get("usage") or {}
        effective = result.get("effective") or {}
        print(
            "\n--- zcode-worker: "
            f"session={result.get('sessionId') or 'n/d'} "
            f"model={effective.get('modelId') or 'n/d'} "
            f"effort={effective.get('effort') or 'n/d'} "
            f"input={usage.get('inputTokens', 'n/d')} "
            f"output={usage.get('outputTokens', 'n/d')} "
            f"cacheRead={usage.get('cacheReadTokens', 'n/d')} "
            f"cacheWrite={usage.get('cacheWriteTokens', 'n/d')} ---"
        )
        print(f"⚠ AUDITORIA {AVISO_AUDITORIA}")
    elif result.get("status") == "models":
        for item in result["combinations"]:
            print(f"{item['modelId']} effort={item['effort']}")
    elif result.get("status") in {"doctor", "dry-run"} or "id" in result:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        error = result.get("error") or {}
        print(f"ERRO: {error.get('message', 'falha segura')}", file=sys.stderr)


_INJECTED_ENTRYPOINT = None


def configure(core):
    """Injeta o core instalado também nas integrações opcionais."""

    global base, _INJECTED_ENTRYPOINT
    if core is None:
        return base
    core_file = getattr(core, "__file__", None)
    _INJECTED_ENTRYPOINT = None
    if isinstance(core_file, str) and os.path.isabs(core_file):
        resolved = os.path.abspath(core_file)
        if os.path.isfile(resolved) and not os.path.islink(resolved):
            _INJECTED_ENTRYPOINT = resolved
    base = core
    features.configure(core)
    try:
        from . import jobs
        if callable(getattr(jobs, "configure", None)):
            jobs.configure(core)
    except ImportError:
        pass
    return core


def _log_internal_error() -> None:
    """Grava o traceback em arquivo privado (0600) para diagnóstico; nunca no terminal."""

    try:
        import traceback

        state = getattr(base, "STATE", None) or os.path.expanduser("~/.local/state/zcode-worker")
        os.makedirs(state, mode=0o700, exist_ok=True)
        path = os.path.join(state, "last-internal-error.log")
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n" + traceback.format_exc())
        print(f"zcode-worker: traceback em {path}", file=sys.stderr)
    except Exception:
        pass


def main(argv=None, core=None) -> int:
    """Ponto de entrada do launcher novo."""

    try:
        if core is not None:
            configure(core)
        args = features.parse_args(list(sys.argv[1:] if argv is None else argv))
        if args._job_child:
            from . import jobs
            return int(jobs.run_child(args._job_child, _job_runner))
        if args.doctor:
            _print_result(doctor(), args.json)
            return 0
        if args.list_models:
            _print_result(list_models(), args.json)
            return 0
        if args.status or args.cancel:
            result, _ = _dispatch_jobs(args, None)
            _print_result(result, args.json)
            return 0
        if not args.cwd:
            raise features.FeatureError("--cwd é obrigatório para tarefa")
        task = features.read_task(args)
        if args.saida:
            result, _ = _dispatch_jobs(args, task)
            _print_result(result, args.json)
            return 0
        result, code = run_task(args, task)
        _print_result(result, args.json)
        if code == 75:
            retry_after = result.get("retryAfter")
            if retry_after is not None:
                sys.stderr.write(f"worker-ask-quota-reset: {int(retry_after)}\n")
                sys.stderr.flush()
        return code
    except features.FeatureError as error:
        result, code = _error_result(error, 0.0)
        _print_result(result, True if "--json" in (argv or sys.argv[1:]) else False)
        return code
    except Exception as error:
        if not isinstance(error, getattr(base, "WorkerError", ())):
            _log_internal_error()
        result, code = _error_result(error, 0.0)
        _print_result(result, True if "--json" in (argv or sys.argv[1:]) else False)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
