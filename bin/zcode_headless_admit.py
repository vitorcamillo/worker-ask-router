#!/usr/bin/env python3
"""Admissão concorrente segura para processos headless da CLI."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MIN_MEM_KIB = 4 * 1024 * 1024
MAX_PARALLEL = 16
DEFAULT_TIMEOUT = 30.0
LOCK_NAMES = ("cli-headless.lock", "cli-headless-{slot}.lock")


class AdmissionError(RuntimeError):
    """Falha de configuração, memória, lock ou comando; sempre sai com 1."""


class AdmissionTimeout(AdmissionError):
    """Nenhuma vaga foi liberada dentro do timeout; sai com 124."""


@dataclass
class LockHandle:
    path: Path
    fd: int
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            os.close(self.fd)
            self.closed = True


def _error(message: str) -> AdmissionError:
    return AdmissionError(message)


def read_max_parallel(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 1
    except OSError as exc:
        raise _error("configuracao de admissao ilegivel") from exc
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _error("configuracao de admissao invalida") from exc
    if not isinstance(value, dict) or "max_parallel" not in value:
        raise _error("configuracao de admissao invalida")
    limit = value["max_parallel"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PARALLEL:
        raise _error("configuracao de admissao invalida")
    return limit


def read_mem_available(path: Path = Path("/proc/meminfo")) -> bool:
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise _error("fonte de memoria indisponivel") from exc
    match = re.search(r"^MemAvailable:\s*(\d+)\s+kB\s*$", text, re.MULTILINE)
    if match is None or int(match.group(1)) < MIN_MEM_KIB:
        raise _error("memoria disponivel insuficiente")
    return True


def _ensure_state_dir(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise _error("diretorio de locks invalido")
        os.chmod(path, 0o700)
    except AdmissionError:
        raise
    except OSError as exc:
        raise _error("diretorio de locks invalido") from exc
    return path


def _check_regular_lock(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _error("lock de admissao invalido") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _error("lock de admissao invalido")


def open_lock(path: Path) -> LockHandle:
    _ensure_state_dir(path.parent)
    _check_regular_lock(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _error("lock de admissao invalido")
        os.fchmod(fd, 0o600)
    except AdmissionError:
        with contextlib.suppress(OSError):
            if fd >= 0:
                os.close(fd)
        raise
    except OSError as exc:
        with contextlib.suppress(OSError):
            if fd >= 0:
                os.close(fd)
        raise _error("lock de admissao invalido") from exc
    return LockHandle(path, fd)


def try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except (OSError, ValueError) as exc:
        raise _error("lock de admissao invalido") from exc
    return True


def _lock_paths(state_dir: Path, limit: int) -> list[Path]:
    state = _ensure_state_dir(state_dir)
    paths = [state / LOCK_NAMES[0]]
    paths.extend(state / LOCK_NAMES[1].format(slot=slot) for slot in range(1, limit))
    return paths


def validate_inherited_fd(fd: int, state_dir: Path, limit: int) -> LockHandle:
    if not isinstance(fd, int) or fd < 0:
        raise _error("descritor de lock invalido")
    paths = _lock_paths(state_dir, limit)
    allowed = {os.path.normpath(str(path)) for path in paths}
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if os.path.normpath(target) not in allowed:
            raise _error("descritor de lock invalido")
        path = Path(target)
        _check_regular_lock(path)
        os.fstat(fd)
        if not try_lock(fd):
            raise _error("lock de admissao nao herdado")
        os.set_inheritable(fd, True)
    except AdmissionError:
        raise
    except OSError as exc:
        raise _error("descritor de lock invalido") from exc
    return LockHandle(path, fd)


def _env_fd() -> int | None:
    raw = os.environ.get("ZCODE_WORKER_LOCK_FD")
    if raw is None:
        return None
    if not raw.isdecimal():
        raise _error("descritor de lock invalido")
    return int(raw)


@contextlib.contextmanager
def acquire_slot(settings_path: Path, state_dir: Path, meminfo_path: Path, timeout: float = DEFAULT_TIMEOUT, inherited_fd: int | None = None, on_wait=None):
    limit = read_max_parallel(settings_path)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise _error("timeout de admissao invalido")
    read_mem_available(meminfo_path)
    if inherited_fd is not None:
        lock = validate_inherited_fd(inherited_fd, state_dir, limit)
        try:
            read_mem_available(meminfo_path)
            yield lock
        finally:
            lock.close()
        return

    handles: list[LockHandle] = []
    selected: LockHandle | None = None
    waiting_announced = False
    deadline = time.monotonic() + timeout
    try:
        for path in _lock_paths(state_dir, limit):
            handles.append(open_lock(path))
        while selected is None:
            for handle in handles:
                if try_lock(handle.fd):
                    selected = handle
                    break
            if selected is not None:
                for handle in handles:
                    if handle is not selected:
                        handle.close()
                read_mem_available(meminfo_path)
                os.set_inheritable(selected.fd, True)
                yield selected
                return
            if not waiting_announced:
                waiting_announced = True
                if on_wait is not None:
                    on_wait()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdmissionTimeout(f"fila de admissao esgotada (limite {timeout:g}s)")
            time.sleep(min(0.2, remaining))
    finally:
        for handle in handles:
            handle.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--settings", type=Path, default=Path.home() / ".config/zcode-worker/settings.json")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/zcode-worker")
    parser.add_argument("--meminfo", type=Path, default=Path("/proc/meminfo"))
    parser.add_argument("--timeout", default=str(DEFAULT_TIMEOUT))
    parser.add_argument("--exec", dest="command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if not args.command:
            raise _error("comando de execucao ausente")
        timeout = float(args.timeout)
        inherited_fd = _env_fd()
        def announce_wait() -> None:
            print(f"zcode-cli: fila cheia; aguardando vaga (limite {timeout:g}s)", file=sys.stderr, flush=True)

        with acquire_slot(args.settings, args.state_dir, args.meminfo, timeout, inherited_fd, announce_wait):
            os.execvpe(args.command[0], args.command, os.environ.copy())
    except AdmissionTimeout as exc:
        print(f"zcode-cli: {exc}", file=sys.stderr)
        return 124
    except (AdmissionError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, AdmissionError) else "falha na admissao headless"
        print(f"zcode-cli: {message}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
