"""Jobs locais, privados e canceláveis para integração futura com o CLI.

O módulo deliberadamente não conhece ``cli.py``.  O processo filho recebe apenas
o id do job e lê a tarefa de um arquivo privado; assim, o prompt nunca aparece
na linha de comando nem no metadata público.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import base


STATE = base.STATE
WorkerError = base.WorkerError
Terminated = base.Terminated
DeadlineExpired = base.DeadlineExpired


JOB_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_PROC_ROOT = "/proc"
_CORE = base
_STATES = {"queued", "running", "completed", "failed", "cancelled", "timed_out", "lost"}
_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "lost"}
_ALLOWED_OPTIONS = {
    "cwd", "mode", "model", "effort", "profile", "timeout", "queue_timeout",
    "total_timeout", "max_output_bytes", "resume", "attach", "disallowed_tools", "json",
    "conversation",
}
_SECRET_KEY_PARTS = ("token", "secret", "password", "passwd", "cookie", "credential", "api_key", "apikey")
_TASK_LIMIT = 96 * 1024
_OPTIONS_LIMIT = 128 * 1024
_DEFAULT_OUTPUT_LIMIT = 16 * 1024 * 1024
# O limite passado ao engine é no máximo 16 MiB.  O arquivo persistido também
# pode conter o envelope JSON e o footer, portanto não deve reutilizar esse
# limite de coleta como se fosse o tamanho final do arquivo.
_MAX_OUTPUT_LIMIT = 16 * 1024 * 1024
_PERSISTED_OUTPUT_LIMIT = (6 * _MAX_OUTPUT_LIMIT) + (64 * 1024)
_CANCEL_CONFIRM_GRACE = 1.0
# Parada em dois estágios: SIGTERM, espera cooperativa e só então SIGKILL.
_CANCEL_KILL_GRACE = 2.0
_EVENTS_MAX_BYTES = 1024 * 1024
_EVENT_MAX_LINE_BYTES = 4 * 1024
_CURRENT_EVENT_SINK: Optional[str] = None


def configure(core: Any) -> Any:
    """Injeta um módulo compatível com :mod:`base` para a integração instalada."""

    global _CORE, WorkerError, Terminated, DeadlineExpired
    if core is None or not hasattr(core, "STATE"):
        raise base.WorkerError("core de jobs inválido")
    _CORE = core
    WorkerError = getattr(core, "WorkerError", base.WorkerError)
    Terminated = getattr(core, "Terminated", base.Terminated)
    DeadlineExpired = getattr(core, "DeadlineExpired", base.DeadlineExpired)
    return _CORE


def _now() -> float:
    return time.time()


def _state() -> str:
    value = getattr(_CORE, "STATE", None)
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise WorkerError("estado local inválido")
    return value


def _private_dir(path: str, *, create: bool) -> None:
    """Cria/valida um diretório sem seguir symlink e só então o endurece."""

    try:
        os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise WorkerError("estado local ilegível") from None
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise WorkerError("não foi possível preparar o estado privado") from None
    except OSError:
        raise WorkerError("estado local ilegível") from None

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise WorkerError("estado local ilegível") from None
    try:
        directory_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.getuid()
        ):
            raise WorkerError("estado local não é privado")
        if stat.S_IMODE(directory_stat.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
    except WorkerError:
        raise
    except OSError:
        raise WorkerError("estado local não é privado") from None
    finally:
        os.close(descriptor)


def _jobs_dir(create: bool = False) -> str:
    state = _state()
    _private_dir(state, create=create)
    path = os.path.join(state, "jobs")
    try:
        _private_dir(path, create=create)
    except WorkerError as error:
        if str(error) == "estado local ilegível":
            raise WorkerError("estado de jobs ilegível") from None
        if str(error) == "não foi possível preparar o estado privado":
            raise WorkerError("estado de jobs ilegível") from None
        raise WorkerError("estado de jobs não é privado") from None
    return path


def _validate_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise WorkerError("id de job inválido")
    return job_id


def _job_dir(job_id: str, *, create: bool = False) -> str:
    job_id = _validate_id(job_id)
    root = _jobs_dir(create=create)
    path = os.path.join(root, job_id)
    try:
        if create:
            os.mkdir(path, 0o700)
    except FileExistsError:
        # Uma colisão de UUID não pode transformar esta chamada em uma
        # abertura/reutilização de job de outra chamada.
        raise WorkerError("job já existe") from None
    except OSError:
        raise WorkerError("job não encontrado") from None
    try:
        st = os.lstat(path)
    except OSError:
        raise WorkerError("job ilegível") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise WorkerError("job não é privado")
    return path


def _private_file(path: str, *, create: bool = False, truncate: bool = False) -> int:
    flags = os.O_RDONLY
    mode = 0o600
    if create:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    elif truncate:
        flags = os.O_WRONLY | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    identity: Optional[Tuple[int, int]] = None
    completed = False
    try:
        fd = os.open(path, flags, mode)
        try:
            st = os.fstat(fd)
        except OSError:
            # Ainda é possível identificar com segurança o inode aberto para
            # limpar uma criação exclusiva quando o fstat injetado falha.
            st = os.stat(f"/proc/self/fd/{fd}")
            identity = (st.st_dev, st.st_ino)
            raise
        identity = (st.st_dev, st.st_ino)
        if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode):
            raise WorkerError("arquivo privado não é regular")
        if create:
            os.fchmod(fd, 0o600)
        elif stat.S_IMODE(st.st_mode) != 0o600:
            raise WorkerError("arquivo privado não é privado")
        result = fd
        fd = -1
        completed = True
        return result
    except WorkerError:
        raise
    except OSError:
        raise WorkerError("arquivo privado ilegível") from None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if create and not completed and identity is not None:
            with contextlib.suppress(OSError):
                current = os.lstat(path)
                if (
                    stat.S_ISREG(current.st_mode)
                    and not stat.S_ISLNK(current.st_mode)
                    and (current.st_dev, current.st_ino) == identity
                ):
                    os.unlink(path)


def _json_dump(value: Any) -> bytes:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        raise WorkerError("JSON inválido") from None
    return text.encode("utf-8")


def _read_private_json(path: str, *, limit: int) -> Any:
    fd = _private_file(path)
    try:
        with os.fdopen(fd, "rb") as source:
            data = source.read(limit + 1)
    except (OSError, UnicodeError):
        raise WorkerError("arquivo privado ilegível") from None
    if len(data) > limit:
        raise WorkerError("arquivo privado excede o limite")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WorkerError("JSON privado inválido") from None


def _atomic_write(path: str, value: Any, *, limit: int) -> None:
    data = _json_dump(value)
    if len(data) > limit:
        raise WorkerError("JSON privado excede o limite")
    parent = os.path.dirname(path)
    fd = -1
    temporary = None
    try:
        # Nunca substitua um metadata/input symlink ou arquivo estrangeiro.
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        except OSError:
            raise WorkerError("não foi possível persistir o job") from None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.getuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
        ):
            raise WorkerError("não foi possível persistir o job")
        fd, temporary = tempfile.mkstemp(prefix=".job-", dir=parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as target:
            fd = -1
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        except OSError:
            raise WorkerError("não foi possível persistir o job") from None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.getuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
        ):
            raise WorkerError("não foi possível persistir o job")
        os.replace(temporary, path)
        temporary = None
    except WorkerError:
        raise
    except (OSError, ValueError):
        raise WorkerError("não foi possível persistir o job") from None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


@contextlib.contextmanager
def _job_lock(job_id: str, *, create: bool = False) -> Iterator[str]:
    directory = _job_dir(job_id, create=create)
    lock_path = os.path.join(directory, ".lock")
    try:
        fd = _private_file(lock_path, create=True)
    except WorkerError:
        # O O_EXCL pode perder a corrida de criação. Só reabra um arquivo
        # privado já validado; arquivo estrangeiro/symlink continua recusado.
        fd = _private_file(lock_path, create=False)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    except OSError:
        raise WorkerError("não foi possível proteger o job") from None
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _metadata_path(directory: str) -> str:
    return os.path.join(directory, "metadata.json")


def _events_path(directory: str) -> str:
    return os.path.join(directory, "events.jsonl")


def _append_event(path: str, event: Any) -> None:
    """Anexa um evento público de andamento sem conteúdo de tarefa/resposta."""

    if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
        return
    try:
        line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    except (TypeError, ValueError):
        return
    encoded = line.encode("utf-8")
    if len(encoded) > _EVENT_MAX_LINE_BYTES:
        return
    try:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return
            if info.st_size >= _EVENTS_MAX_BYTES:
                # Teto atingido: marca uma única vez para a causa ser visível
                # ao orquestrador em vez de eventos sumirem em silêncio.
                tail = os.pread(fd, 128, max(0, info.st_size - 128))
                if b'"events_truncated"' not in tail:
                    marker = b'{"kind":"events_truncated"}\n'
                    with contextlib.suppress(OSError):
                        os.write(fd, marker)
                return
            os.write(fd, encoded)
        finally:
            os.close(fd)
    except OSError:
        return


def current_event_sink():
    """Sink de eventos do job em execução neste processo, se houver."""

    path = _CURRENT_EVENT_SINK
    if not path:
        return None

    def sink(event: dict) -> None:
        with contextlib.suppress(Exception):
            _append_event(path, event)

    return sink


def _input_path(directory: str) -> str:
    return os.path.join(directory, "input.json")


def _validate_task(task: Any) -> str:
    if not isinstance(task, str) or not task.strip() or "\x00" in task:
        raise WorkerError("tarefa inválida")
    if len(task.encode("utf-8")) > _TASK_LIMIT:
        raise WorkerError("tarefa excede o limite seguro")
    return task


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                return True
            lowered = key.casefold()
            if any(part in lowered for part in _SECRET_KEY_PARTS):
                return True
            if _contains_secret_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _validate_options(options: Any) -> Dict[str, Any]:
    if not isinstance(options, dict) or _contains_secret_key(options):
        raise WorkerError("opções inválidas")
    if set(options) - _ALLOWED_OPTIONS:
        raise WorkerError("opções não permitidas")
    result = dict(options)
    for key in ("timeout", "queue_timeout", "total_timeout"):
        if key in result:
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
                raise WorkerError("timeout inválido")
    if "max_output_bytes" in result:
        value = result["max_output_bytes"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_OUTPUT_LIMIT:
            raise WorkerError("limite de saída inválido")
    for key in ("cwd", "resume", "profile", "mode", "model", "effort"):
        if key in result and (not isinstance(result[key], str) or "\x00" in result[key]):
            raise WorkerError("opção de texto inválida")
    if "cwd" in result and (not os.path.isabs(result["cwd"]) or not os.path.isdir(result["cwd"])):
        raise WorkerError("cwd inválido")
    for key in ("attach", "disallowed_tools"):
        if key in result:
            values = result[key]
            if not isinstance(values, list) or len(values) > 256 or any(
                not isinstance(item, str) or "\x00" in item or len(item) > 4096 for item in values
            ):
                raise WorkerError("lista de opções inválida")
    if "json" in result and not isinstance(result["json"], bool):
        raise WorkerError("opção JSON inválida")
    if "conversation" in result and not isinstance(result["conversation"], bool):
        raise WorkerError("opção de conversa inválida")
    if len(_json_dump(result)) > _OPTIONS_LIMIT:
        raise WorkerError("opções excedem o limite seguro")
    return result


def _validate_entrypoint(entrypoint: Any) -> List[str]:
    if not isinstance(entrypoint, list) or not entrypoint or any(
        not isinstance(value, str) or not value or "\x00" in value for value in entrypoint
    ):
        raise WorkerError("entrypoint inválido")
    executable = entrypoint[0]
    if not os.path.isabs(executable):
        raise WorkerError("entrypoint deve ser absoluto")
    try:
        st = os.lstat(executable)
    except OSError:
        raise WorkerError("entrypoint inexistente") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or not os.access(executable, os.X_OK):
        raise WorkerError("entrypoint não é executável")
    executable_name = os.path.basename(executable).casefold()
    if executable_name in {"sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "cmd", "powershell", "pwsh"}:
        raise WorkerError("shell command não permitido")
    if executable_name.startswith("python"):
        if len(entrypoint) < 2 or not os.path.isabs(entrypoint[1]):
            raise WorkerError("script Python deve ser absoluto")
        try:
            script_stat = os.lstat(entrypoint[1])
        except OSError:
            raise WorkerError("script Python inexistente") from None
        if stat.S_ISLNK(script_stat.st_mode) or not stat.S_ISREG(script_stat.st_mode):
            raise WorkerError("script Python inválido")
    return list(entrypoint)


def _validate_output_path(output: Any) -> str:
    if not isinstance(output, str) or not output or "\x00" in output or not os.path.isabs(output):
        raise WorkerError("arquivo de saída inválido")
    return output


def _create_output(path: str) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    identity: Optional[Tuple[int, int]] = None
    try:
        fd = os.open(path, flags, 0o600)
        try:
            created_stat = os.fstat(fd)
        except OSError:
            created_stat = os.stat(f"/proc/self/fd/{fd}")
            identity = (created_stat.st_dev, created_stat.st_ino)
            raise
        identity = (created_stat.st_dev, created_stat.st_ino)
        os.fchmod(fd, 0o600)
        result = fd
        fd = -1
        return result
    except OSError:
        raise WorkerError("arquivo de saída já existe ou não pode ser criado") from None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
            if identity is not None:
                with contextlib.suppress(OSError):
                    current = os.lstat(path)
                    # O ramo FALSE (identity divergente: arquivo pertence a
                    # outro dono) é exercitado no fluxo return->finally, que o
                    # tracer do coverage não enxerga; o teste
                    # test_private_and_output_cleanup_skip_divergent_identity
                    # o valida comportamentalmente (o arquivo permanece).
                    if (  # pragma: no branch
                        stat.S_ISREG(current.st_mode)
                        and not stat.S_ISLNK(current.st_mode)
                        and (current.st_dev, current.st_ino) == identity
                    ):
                        os.unlink(path)


def _proc_details(pid: int) -> Optional[Tuple[int, int, int, int]]:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    root = _PROC_ROOT
    if not isinstance(root, str) or not os.path.isabs(root):
        return None
    proc_dir = os.path.join(root, str(pid))
    try:
        directory_stat = os.stat(proc_dir)
        with open(os.path.join(proc_dir, "stat"), encoding="ascii") as source:
            line = source.read(4096)
        closing = line.rfind(")")
        if closing < 0:
            return None
        fields = line[closing + 2 :].split()
        if len(fields) <= 19:
            return None
        process_group = int(fields[2])
        session = int(fields[3])
        starttime = int(fields[19])
        return directory_stat.st_uid, starttime, process_group, session
    except (OSError, UnicodeError, ValueError):
        return None


def _proc_stat(pid: int) -> Optional[Tuple[int, int]]:
    details = _proc_details(pid)
    if details is None:
        return None
    return details[0], details[1]


def _proc_has_identity(pid: int, uid: int, starttime: int, job_id: str) -> bool:
    details = _proc_details(pid)
    if details is None or details[:2] != (uid, starttime) or details[2:] != (pid, pid):
        return False
    root = _PROC_ROOT
    try:
        with open(os.path.join(root, str(pid), "cmdline"), "rb") as source:
            argv = [part for part in source.read(64 * 1024).split(b"\0") if part]
    except (OSError, UnicodeError):
        return False
    return b"--_job-child" in argv and job_id.encode("ascii") in argv


def _own_process_identity(pid: int, attempts: int = 20) -> Optional[Tuple[int, int]]:
    for _ in range(attempts):
        found = _proc_stat(pid)
        if found is not None:
            return found
        time.sleep(0.005)
    return None


def _metadata_valid(metadata: Any, job_id: str) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        raise WorkerError("metadata inválido")
    if set(metadata) - {
        "id", "status", "createdAt", "pid", "uid", "starttime", "output", "exitCode",
        "duration", "cancelRequestedAt", "outputDev", "outputIno",
    }:
        raise WorkerError("metadata inválido")
    if metadata.get("id") != job_id or metadata.get("status") not in _STATES:
        raise WorkerError("metadata inválido")
    if not isinstance(metadata.get("output"), str) or not os.path.isabs(metadata["output"]):
        raise WorkerError("metadata inválido")
    if (
        isinstance(metadata.get("createdAt"), bool)
        or not isinstance(metadata.get("createdAt"), (int, float))
        or not math.isfinite(metadata["createdAt"])
    ):
        raise WorkerError("metadata inválido")
    for key in ("outputDev", "outputIno"):
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerError("metadata inválido")
    pid, uid, starttime = metadata.get("pid"), metadata.get("uid"), metadata.get("starttime")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise WorkerError("metadata inválido")
    if uid is not None and (isinstance(uid, bool) or not isinstance(uid, int) or uid < 0):
        raise WorkerError("metadata inválido")
    if starttime is not None and (isinstance(starttime, bool) or not isinstance(starttime, int) or starttime < 0):
        raise WorkerError("metadata inválido")
    exit_code = metadata.get("exitCode")
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255
    ):
        raise WorkerError("metadata inválido")
    for key in ("duration", "cancelRequestedAt"):
        value = metadata.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise WorkerError("metadata inválido")
    return dict(metadata)


def _load_metadata(directory: str, job_id: str) -> Dict[str, Any]:
    return _metadata_valid(_read_private_json(_metadata_path(directory), limit=64 * 1024), job_id)


def _load_input(directory: str) -> Tuple[Dict[str, Any], str]:
    payload = _read_private_json(_input_path(directory), limit=_OPTIONS_LIMIT + _TASK_LIMIT + 4096)
    if not isinstance(payload, dict) or set(payload) != {"options", "task"}:
        raise WorkerError("input privado inválido")
    return _validate_options(payload["options"]), _validate_task(payload["task"])


def _public(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": metadata["id"],
        "status": metadata["status"],
        "pid": metadata.get("pid"),
        "output": metadata["output"],
        "exitCode": metadata.get("exitCode"),
    }


def _set_terminal(directory: str, metadata: Dict[str, Any], status: str, exit_code: int, duration: float) -> Dict[str, Any]:
    if metadata["status"] in _TERMINAL:
        return metadata
    metadata["status"] = status
    metadata["exitCode"] = int(exit_code)
    metadata["duration"] = max(0.0, float(duration))
    _atomic_write(_metadata_path(directory), metadata, limit=64 * 1024)
    return metadata


def _remove_input(directory: str) -> None:
    path = _input_path(directory)
    try:
        st = os.lstat(path)
        if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return
        os.unlink(path)
    except OSError:
        return


def start_job(options: dict, task: str, output: str, entrypoint: list[str]) -> dict:
    """Cria e inicia um job privado sem colocar a tarefa em ``argv``."""

    options = _validate_options(options)
    task = _validate_task(task)
    output = _validate_output_path(output)
    entrypoint = _validate_entrypoint(entrypoint)
    if task in entrypoint:
        raise WorkerError("entrypoint não pode conter a tarefa")
    jobs_root = _jobs_dir(create=True)
    job_id = uuid.uuid4().hex
    directory = _job_dir(job_id, create=True)
    output_fd = -1
    output_created = False
    output_identity: Optional[Tuple[int, int]] = None
    child = [*entrypoint, "--_job-child", job_id]
    metadata = {
        "id": job_id,
        "status": "queued",
        "createdAt": _now(),
        "pid": None,
        "uid": os.getuid(),
        "starttime": None,
        "output": output,
        "exitCode": None,
        "outputDev": None,
        "outputIno": None,
    }
    try:
        # O output não pode sobrescrever nenhum artefato de controle do próprio job.
        if os.path.commonpath((os.path.abspath(directory), os.path.abspath(output))) == os.path.abspath(directory):
            raise WorkerError("arquivo de saída deve ser externo ao job")
        output_fd = _create_output(output)
        created_stat = os.fstat(output_fd)
        output_identity = (created_stat.st_dev, created_stat.st_ino)
        output_created = True
        metadata.update({"outputDev": output_identity[0], "outputIno": output_identity[1]})
        _atomic_write(_metadata_path(directory), metadata, limit=64 * 1024)
        _atomic_write(_input_path(directory), {"options": options, "task": task}, limit=_OPTIONS_LIMIT + _TASK_LIMIT + 4096)
        environment = dict(os.environ)
        environment["ZCODE_WORKER_CONTEXT_STATE"] = _state()
        with _job_lock(job_id) as locked_directory:
            try:
                process = subprocess.Popen(
                    child,
                    stdin=subprocess.DEVNULL,
                    stdout=output_fd,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                    shell=False,
                    env=environment,
                )
            except (OSError, ValueError):
                raise WorkerError("não foi possível iniciar o job") from None
            identity = _own_process_identity(process.pid)
            if identity is None:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGTERM)
                with contextlib.suppress(Exception):
                    process.wait(timeout=1.0)
                raise WorkerError("não foi possível identificar o job iniciado")
            current = _load_metadata(locked_directory, job_id)
            if current["status"] not in _TERMINAL:
                current.update({"pid": process.pid, "uid": identity[0], "starttime": identity[1]})
                _atomic_write(_metadata_path(locked_directory), current, limit=64 * 1024)
            metadata = current
        return _public(metadata)
    except BaseException:
        if "process" in locals() and process.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(Exception):
                process.wait(timeout=1.0)
        with contextlib.suppress(OSError):
            os.unlink(_input_path(directory))
        if output_created and output_identity is not None:
            with contextlib.suppress(OSError):
                output_stat = os.lstat(output)
                if (
                    not stat.S_ISLNK(output_stat.st_mode)
                    and stat.S_ISREG(output_stat.st_mode)
                    and (output_stat.st_dev, output_stat.st_ino) == output_identity
                ):
                    os.unlink(output)
        try:
            metadata_stat = os.lstat(_metadata_path(directory))
        except OSError:
            metadata_stat = None
        if metadata_stat is not None and stat.S_ISREG(metadata_stat.st_mode) and metadata_stat.st_uid == os.getuid():
            with contextlib.suppress(Exception):
                with _job_lock(job_id) as locked_directory:
                    failed = _load_metadata(locked_directory, job_id)
                    _set_terminal(locked_directory, failed, "failed", 1, 0.0)
        raise
    finally:
        if output_fd >= 0:
            os.close(output_fd)


def read_status(job_id: str) -> dict:
    """Lê apenas o estado público e marca ``lost`` quando a identidade morreu."""

    job_id = _validate_id(job_id)
    with _job_lock(job_id) as directory:
        metadata = _load_metadata(directory, job_id)
        if metadata["status"] in _TERMINAL:
            _remove_input(directory)
        else:
            pid, uid, starttime = metadata.get("pid"), metadata.get("uid"), metadata.get("starttime")
            valid = (
                pid is not None and uid == os.getuid() and starttime is not None
                and _proc_has_identity(pid, uid, starttime, job_id)
            )
            if not valid:
                # Durante a transação de start o filho ainda pode estar com
                # pid=None; o lock normalmente oculta essa janela, e o
                # estado continua queued caso ela seja observada diretamente.
                if metadata.get("pid") is None and metadata["status"] == "queued":
                    return _public(metadata)
                # Depois de um cancelamento, a ausência do processo não prova
                # que o filho observou o sinal e gravou seu estado terminal.
                cancel_requested = metadata.get("cancelRequestedAt")
                if (
                    isinstance(cancel_requested, (int, float))
                    and _now() - cancel_requested < _CANCEL_CONFIRM_GRACE
                ):
                    return _public(metadata)
                _set_terminal(directory, metadata, "lost", 1, 0.0)
                _remove_input(directory)
        return _public(metadata)


def cancel_job(job_id: str) -> dict:
    """Solicita cancelamento somente ao processo ainda identificado.

    O sinal em dois estágios acontece FORA do lock do job: o filho precisa
    conseguir adquirir o lock para gravar o estado terminal cooperativo
    enquanto o cancelador aguarda o grace antes de forçar com SIGKILL.
    """

    job_id = _validate_id(job_id)
    signal_target = None
    with _job_lock(job_id) as directory:
        metadata = _load_metadata(directory, job_id)
        if metadata["status"] in _TERMINAL:
            _remove_input(directory)
            return _public(metadata)
        pid, uid, starttime = metadata.get("pid"), metadata.get("uid"), metadata.get("starttime")
        if metadata["status"] == "queued":
            requested = _now()
            metadata["cancelRequestedAt"] = requested
            _atomic_write(_metadata_path(directory), metadata, limit=64 * 1024)
            result = _public(metadata)
            result["cancelRequestedAt"] = requested
            return result
        if (
            pid is None or uid != os.getuid() or starttime is None
            or not _proc_has_identity(pid, uid, starttime, job_id)
        ):
            _set_terminal(directory, metadata, "lost", 1, 0.0)
            _remove_input(directory)
            return _public(metadata)
        requested = _now()
        metadata["cancelRequestedAt"] = requested
        _atomic_write(_metadata_path(directory), metadata, limit=64 * 1024)
        signal_target = (pid, uid, starttime)
    forced = _signal_cancel_two_stage(signal_target[0], signal_target[1], signal_target[2], job_id)
    result = _public(metadata)
    result["cancelRequestedAt"] = requested
    if forced:
        result["killForced"] = True
    return result


def _signal_cancel_two_stage(pid: int, uid: int, starttime: int, job_id: str) -> bool:
    """SIGTERM, espera cooperativa limitada e SIGKILL apenas se persistir."""

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        return False
    pidfd = -1
    try:
        try:
            pidfd = pidfd_open(pid, 0)
        except (OSError, PermissionError):
            pidfd = -1
        if pidfd < 0 or not _proc_has_identity(pid, uid, starttime, job_id):
            return False
        try:
            pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
        except (OSError, PermissionError):
            return False
        deadline = time.monotonic() + _CANCEL_KILL_GRACE
        while time.monotonic() < deadline:
            if not _proc_has_identity(pid, uid, starttime, job_id):
                return False
            time.sleep(0.1)
        if _proc_has_identity(pid, uid, starttime, job_id):
            with contextlib.suppress(OSError, PermissionError):
                pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
            return True
        return False
    finally:
        if pidfd >= 0:
            with contextlib.suppress(OSError):
                os.close(pidfd)


def _output_identity(value: Any) -> Tuple[int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value)
    ):
        raise WorkerError("identidade de saída inválida")
    return value


def _open_output_for_append(path: str, limit: int, expected_identity: Tuple[int, int]) -> int:
    expected_identity = _output_identity(expected_identity)
    fd = -1
    try:
        st = os.lstat(path)
        if (
            st.st_uid != os.getuid()
            or not stat.S_ISREG(st.st_mode)
            or stat.S_IMODE(st.st_mode) != 0o600
            or (st.st_dev, st.st_ino) != expected_identity
        ):
            raise OSError
        fd = os.open(path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        actual = os.fstat(fd)
        if (
            actual.st_uid != os.getuid()
            or not stat.S_ISREG(actual.st_mode)
            or stat.S_IMODE(actual.st_mode) != 0o600
            or (actual.st_dev, actual.st_ino) != expected_identity
            or actual.st_size > limit
        ):
            raise OSError
        result = fd
        fd = -1
        return result
    except OSError:
        raise WorkerError("arquivo de saída inválido") from None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def _write_result(path: str, result: Any, exit_code: int, limit: int, expected_identity: Tuple[int, int]) -> None:
    if not isinstance(result, dict) or isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise WorkerError("resultado inválido")
    payload = _json_dump(result) + b"\n" + f"exit={int(exit_code)}\n".encode("ascii")
    fd = _open_output_for_append(path, limit, expected_identity)
    try:
        if os.fstat(fd).st_size + len(payload) > limit:
            raise WorkerError("saída excede o limite seguro")
        with os.fdopen(fd, "ab") as target:
            fd = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _safe_error_output(path: str, exit_code: int, limit: int, expected_identity: Tuple[int, int]) -> None:
    with contextlib.suppress(Exception):
        _write_result(path, {"status": "error", "message": "falha segura no job"}, exit_code, limit, expected_identity)


def _ensure_footer(path: str, exit_code: int, limit: int, expected_identity: Tuple[int, int]) -> None:
    """Garante footer para um filho que entrou em estado terminal cedo."""

    fd = _open_output_for_append(path, limit, expected_identity)
    try:
        size = os.fstat(fd).st_size
        tail = b""
        if size:
            tail = os.pread(fd, min(size, 256), max(0, size - 256))
        footer = f"exit={int(exit_code)}\n".encode("ascii")
        if not tail.rstrip(b"\n").endswith(footer.rstrip(b"\n")):
            if size and not tail.endswith(b"\n"):
                os.write(fd, b"\n")
            os.write(fd, footer)
            os.fsync(fd)
    finally:
        os.close(fd)


def _run_callback(callback: Callable[[dict, str], Tuple[dict, int]], options: Dict[str, Any], task: str) -> Tuple[dict, int]:
    timeout = options.get("total_timeout")
    if timeout is None and options.get("timeout") is not None:
        queue_timeout = options.get("queue_timeout") or 0.0
        timeout = float(options["timeout"]) + float(queue_timeout)
    if timeout is None or threading_like_signal_unavailable():
        return callback(options, task)
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def alarm_handler(signum: int, _frame: Any) -> None:
        raise DeadlineExpired("timeout do job")

    try:
        signal.signal(signal.SIGALRM, alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, float(timeout))
        return callback(options, task)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)
        # Preserva eventual timer preexistente do processo chamador.
        if old_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *old_timer)


def threading_like_signal_unavailable() -> bool:
    return threading.current_thread() is not threading.main_thread()


def _stdout_matches_output(expected_identity: Tuple[int, int]) -> bool:
    try:
        actual = os.fstat(1)
    except OSError:
        return False
    return (
        actual.st_uid == os.getuid()
        and stat.S_ISREG(actual.st_mode)
        and stat.S_IMODE(actual.st_mode) == 0o600
        and (actual.st_dev, actual.st_ino) == _output_identity(expected_identity)
    )


def _orphan_answers(directory: str) -> list:
    """Respostas do orquestrador não consumidas pelo bridge do job."""

    leftover: list = []
    try:
        names = os.listdir(directory)
    except OSError:
        return leftover
    for name in names:
        if not name.startswith("answer-") or not name.endswith(".json"):
            continue
        candidate = os.path.join(directory, name)
        try:
            info = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.getuid():
            leftover.append(candidate)
    return leftover


def run_child(job_id: str, runner: Callable[[dict, str], Tuple[dict, int]]) -> int:
    """Executa o callback do filho, persiste um estado terminal e limpa ``input``."""

    job_id = _validate_id(job_id)
    if not callable(runner):
        raise WorkerError("runner inválido")
    started = time.monotonic()
    old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    directory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    output: Optional[str] = None
    expected_identity: Optional[Tuple[int, int]] = None
    limit = _PERSISTED_OUTPUT_LIMIT
    result: Dict[str, Any] = {"status": "error", "message": "falha segura no job"}
    exit_code = 1
    final_status = "failed"
    early_terminal = False
    try:
        # Instale antes de esperar o lock: cancel_job pode sinalizar enquanto
        # este processo ainda está bloqueado na transação de start.
        for sig in old_handlers:
            signal.signal(sig, lambda signum, _frame: (_ for _ in ()).throw(Terminated(signum)))
        with _job_lock(job_id) as locked_directory:
            directory = locked_directory
            metadata = _load_metadata(directory, job_id)
            current = _proc_stat(os.getpid())
            if (
                metadata.get("pid") != os.getpid() or metadata.get("uid") != os.getuid()
                or metadata.get("starttime") is None or current != (metadata["uid"], metadata["starttime"])
                or not _proc_has_identity(os.getpid(), metadata["uid"], metadata["starttime"], job_id)
            ):
                raise WorkerError("identidade do filho inválida")
            output = metadata["output"]
            candidate_identity = (metadata["outputDev"], metadata["outputIno"])
            if not _stdout_matches_output(candidate_identity):
                raise WorkerError("identidade do output do filho inválida")
            expected_identity = candidate_identity
            global _CURRENT_EVENT_SINK
            _CURRENT_EVENT_SINK = _events_path(directory)
            # O bridge MCP do job descobre o diretório por esta variável.
            os.environ["ZCODE_WORKER_JOB_ID"] = job_id
            if metadata["status"] in _TERMINAL:
                stored_exit_code = metadata.get("exitCode")
                exit_code = stored_exit_code if isinstance(stored_exit_code, int) else 1
                final_status = metadata["status"]
                early_terminal = True
            elif metadata.get("cancelRequestedAt") is not None:
                final_status = "cancelled"
                exit_code = 128 + signal.SIGTERM
                _set_terminal(directory, metadata, final_status, exit_code, 0.0)
                early_terminal = True
            else:
                metadata["status"] = "running"
                _atomic_write(_metadata_path(directory), metadata, limit=64 * 1024)
                options, task = _load_input(directory)
        if early_terminal:
            # output vem de metadata validada (_metadata_valid exige str).
            if isinstance(output, str):  # pragma: no branch
                with contextlib.suppress(Exception):
                    _ensure_footer(output, exit_code, limit, expected_identity)
            return int(exit_code)
        try:
            result, exit_code = _run_callback(runner, options, task)
            if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
                raise WorkerError("código de saída inválido")
            if exit_code == 124:
                final_status = "timed_out"
            elif exit_code in {128 + signal.SIGTERM, 128 + signal.SIGINT, 128 + signal.SIGHUP}:
                final_status = "cancelled"
            else:
                final_status = "completed" if exit_code == 0 else "failed"
            # Cancelamento que chegou no fim da execução ainda é público como pedido atendido.
            with _job_lock(job_id) as check_directory:
                latest = _load_metadata(check_directory, job_id)
                if latest.get("cancelRequestedAt") is not None and latest["status"] not in _TERMINAL:
                    final_status = "cancelled"
                    exit_code = 128 + signal.SIGTERM
                    result = {**result, "status": "cancelled", "exitCode": exit_code, "effectsMayExist": result.get("effectsMayExist", options.get("mode", "edit") in {"edit", "yolo"})}
                    if result["effectsMayExist"]:
                        result["message"] = "job cancelado. Arquivos podem ter sido alterados; revise o diff antes de tentar novamente."
        except Terminated as error:
            exit_code = 128 + int(error.signum)
            final_status = "cancelled"
            result = {"status": "cancelled", "message": "job cancelado"}
        except DeadlineExpired:
            exit_code = 124
            final_status = "timed_out"
            result = {"status": "timed_out", "message": "tempo limite excedido"}
        except Exception:
            exit_code = 1
            final_status = "failed"
            result = {"status": "error", "message": "falha segura no job"}
        try:
            _write_result(output, result, exit_code, limit, expected_identity)
        except Exception:
            exit_code = 1
            final_status = "failed"
            result = {"status": "error", "message": "falha segura no job"}
            _safe_error_output(output, exit_code, limit, expected_identity)
        with _job_lock(job_id) as locked_directory:
            latest = _load_metadata(locked_directory, job_id)
            terminal = _set_terminal(locked_directory, latest, final_status, exit_code, time.monotonic() - started)
            if terminal.get("status") in _TERMINAL and terminal.get("exitCode") is not None:
                exit_code = int(terminal["exitCode"])
        return int(exit_code)
    except Terminated as error:
        # O sinal pode chegar enquanto o filho aguardava o lock e, portanto,
        # antes de carregar metadata. Confirme o cancelamento sob o lock se
        # possível; sem isso o processo não deve vazar detalhes nem ficar sem
        # footer/input cleanup.
        with contextlib.suppress(Exception):
            with _job_lock(job_id) as locked_directory:
                directory = locked_directory
                latest = _load_metadata(locked_directory, job_id)
                metadata = latest
                output = latest.get("output")
                if latest["status"] not in _TERMINAL:
                    _set_terminal(locked_directory, latest, "cancelled", 128 + int(error.signum), 0.0)
                if isinstance(output, str):  # pragma: no branch
                    _safe_error_output(output, 128 + int(error.signum), limit, expected_identity)
        return 128 + int(error.signum)
    except Exception:
        # Falhas de validação antes do callback não devem vazar detalhes nem deixar input.
        if directory is not None and metadata is not None:
            output = output or metadata.get("output")
            if isinstance(output, str) and expected_identity is not None:
                _safe_error_output(output, 1, limit, expected_identity)
            with contextlib.suppress(Exception):
                with _job_lock(job_id) as locked_directory:
                    latest = _load_metadata(locked_directory, job_id)
                    _set_terminal(locked_directory, latest, "failed", 1, time.monotonic() - started)
        return 1
    finally:
        for sig, handler in old_handlers.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)
        _CURRENT_EVENT_SINK = None
        if directory is not None:
            input_path = _input_path(directory)
            with contextlib.suppress(OSError):
                st = os.lstat(input_path)
                if st.st_uid == os.getuid() and stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    os.unlink(input_path)
            # answers não consumidas são resíduo efêmero; asks/events/control
            # permanecem como histórico do job.
            with contextlib.suppress(OSError):
                for leftover in _orphan_answers(directory):
                    os.unlink(leftover)
