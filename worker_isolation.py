"""Isolamento de execução do worker-ask em worktrees persistentes.

O worker roda numa cópia isolada do repositório (o "slot": worktree git
persistente e reaproveitado) e só os arquivos autorizados voltam para a
árvore real. Tudo que depende do roteador chega pelo objeto `Deps`; este
módulo não importa `worker_router`.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

SLOT_WAIT_S = 120.0
SLOT_POLL_S = 0.5
DEFAULT_SLOTS = 8
MAX_SLOTS = 32
DEFAULT_LINKS = "node_modules:.venv:venv"
DEFAULT_MAX_OVERLAY = 512 * 1024 * 1024
REJECTED_MAX_FILES = 20
REJECTED_MAX_BYTES = 8 * 1024 * 1024
SYNC_LOG_MAX_BYTES = 1024 * 1024
GIT_TIMEOUT_S = 15
GIT_HEAVY_TIMEOUT_S = 600
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")

# Configuração por ambiente/arquivo (nomes privados só para evitar strings soltas).
_ENV_MODE = "WORKER_ASK_ISOLATION"
_ENV_SLOTS = "WORKER_ASK_ISOLATION_SLOTS"
_ENV_LINKS = "WORKER_ASK_ISOLATION_LINKS"
_ENV_MAX_OVERLAY = "WORKER_ASK_ISOLATION_MAX_OVERLAY"
_MARKER = ".worker-ask-isolation"
_MARKER_MAX_BYTES = 64
_MODOS = ("off", "write", "all")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")
_CHUNK = 1024 * 1024
_LOCK_RE = re.compile(r"slot-(?P<n>\d+)\.lock")


class IsolationError(Exception):
    """Falha que faz o roteador parar com rc 1."""


class NoFreeSlot(IsolationError):
    """Nenhum slot livre no prazo."""


class SlotGuardError(IsolationError):
    """A guarda do slot recusou o caminho."""


class DirectMode(Exception):
    """Não dá para isolar; o roteador roda sem isolamento."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _SlotUnusable(Exception):
    """Slot existente recusado pela guarda; tentar o próximo índice."""


@dataclass(frozen=True)
class Deps:
    state_dir: Callable[[], Path]
    is_sensitive: Callable[[str], bool]
    snapshot_allowed: Callable
    parse_status: Callable
    max_file_bytes: int
    max_paths: int


@dataclass
class Session:
    real_cwd: str
    real_top: str
    common_dir: str
    repo_id: str
    slot: str
    slot_cwd: str
    slot_index: int
    lock_fd: int
    run_id: str
    tainted: bool = False


@dataclass
class PrepInfo:
    snap_before: dict
    snap_slot_before: dict
    overlay_files: int
    skipped_sensitive: int
    skipped_nested: int
    links: list
    prep_s: float
    started_ts: float


@dataclass
class SyncResult:
    applied: list
    conflicts: list


@dataclass
class RejectedInfo:
    dir: Optional[str]
    saved: int
    skipped: int


# ---------------------------------------------------------------------------
# Git interno
# ---------------------------------------------------------------------------


def _git_env() -> dict:
    """Cópia do ambiente sem variáveis que desviem o git do repositório alvo."""
    env = dict(os.environ)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        env.pop(var, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(args: list, cwd: str, *, timeout: float = GIT_TIMEOUT_S, no_hooks: bool = False):
    """Roda `git` em `cwd` e devolve o CompletedProcess; TimeoutExpired/OSError viram IsolationError."""
    argv = ["git"]
    if no_hooks:
        argv.extend(NO_HOOKS)
    argv.extend(("-C", os.fspath(cwd)))
    argv.extend(args)
    try:
        return subprocess.run(argv, capture_output=True, env=_git_env(), timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        raise IsolationError("isolamento: falha ao executar git") from None


def _common_dir(real_top: str) -> str:
    """git-common-dir resolvido; saída relativa é relativa ao toplevel."""
    proc = _git(["rev-parse", "--git-common-dir"], real_top)
    if proc.returncode != 0:
        raise IsolationError("isolamento: falha ao obter git-common-dir")
    raw = os.fsdecode(proc.stdout).strip()
    if raw and not os.path.isabs(raw):
        raw = os.path.join(real_top, raw)
    return os.path.realpath(raw)


def _head_sha(real_top: str) -> str:
    """SHA do HEAD atual da árvore real; falha conta como limpeza impossível."""
    proc = _git(["rev-parse", "--verify", "HEAD^{commit}"], real_top)
    if proc.returncode != 0:
        raise IsolationError("isolamento: falha ao limpar slot")
    return os.fsdecode(proc.stdout).strip()


# ---------------------------------------------------------------------------
# Resolução de modo, slots e guarda
# ---------------------------------------------------------------------------


def resolve_mode(cwd, environ=None) -> str:
    """Resolve "off"/"write"/"all": ambiente primeiro, depois arquivo marcador; nunca levanta."""
    env = os.environ if environ is None else environ
    val = str(env.get(_ENV_MODE, "")).strip().lower()
    if val in _MODOS:
        return val
    for base in _marker_dirs(cwd):
        val = _read_marker(base / _MARKER)
        if val in _MODOS:
            return val
    return "off"


def _marker_dirs(cwd) -> list:
    """Diretórios onde o marcador pode estar: do cwd até o toplevel git (inclusive)."""
    chain = []
    cur = os.path.abspath(os.fspath(cwd))
    while True:
        chain.append(Path(cur))
        if os.path.lexists(os.path.join(cur, ".git")):
            return chain
        parent = os.path.dirname(cur)
        if parent == cur:
            return chain[:1]  # sem git: só o cwd
        cur = parent


def _read_marker(path: Path) -> str:
    """Primeira linha do marcador em minúsculas; symlink, >64 bytes ou ilegível devolve ""."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        if os.fstat(fd).st_size > _MARKER_MAX_BYTES:
            return ""
        data = os.read(fd, _MARKER_MAX_BYTES + 1)
    except OSError:
        return ""
    finally:
        os.close(fd)
    return data.split(b"\n", 1)[0].decode("utf-8", "replace").strip().lower()


def slot_count(environ=None) -> int:
    """WORKER_ASK_ISOLATION_SLOTS entre 1 e MAX_SLOTS; ausente ou inválido: DEFAULT_SLOTS."""
    env = os.environ if environ is None else environ
    raw = env.get(_ENV_SLOTS)
    if raw is None:
        return DEFAULT_SLOTS
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_SLOTS
    if 1 <= val <= MAX_SLOTS:
        return val
    return DEFAULT_SLOTS


def _max_overlay(environ) -> int:
    """Limite total do overlay em bytes; valor inválido cai no padrão."""
    env = os.environ if environ is None else environ
    raw = env.get(_ENV_MAX_OVERLAY)
    if raw is None:
        return DEFAULT_MAX_OVERLAY
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_OVERLAY


def _link_names(environ) -> list:
    """Nomes de diretórios de dependência; descarta vazio, com "/", "." e ".."."""
    env = os.environ if environ is None else environ
    raw = env.get(_ENV_LINKS, DEFAULT_LINKS)
    out = []
    for name in str(raw).split(":"):
        if not name or "/" in name or name in (".", ".."):
            continue
        out.append(name)
    return out


def guard_slot(slot, common_dir, deps) -> None:
    """Recusa (SlotGuardError) qualquer slot fora da área de worktrees ou não correspondente."""
    slot_real = os.path.realpath(os.fspath(slot))
    worktrees = os.path.realpath(str(Path(deps.state_dir()) / "worktrees")) + os.sep
    if not slot_real.startswith(worktrees):
        raise SlotGuardError("isolamento: slot fora da area de worktrees")
    proc = _git(["rev-parse", "--show-toplevel"], slot_real)
    if proc.returncode != 0:
        raise SlotGuardError("isolamento: slot nao e worktree git")
    if os.path.realpath(os.fsdecode(proc.stdout).strip()) != slot_real:
        raise SlotGuardError("isolamento: toplevel do slot diverge")
    proc = _git(["rev-parse", "--git-common-dir"], slot_real)
    if proc.returncode != 0:
        raise SlotGuardError("isolamento: git-common-dir falhou")
    raw = os.fsdecode(proc.stdout).strip()
    if raw and not os.path.isabs(raw):
        raw = os.path.join(slot_real, raw)
    if os.path.realpath(raw) != os.path.realpath(os.fspath(common_dir)):
        raise SlotGuardError("isolamento: git-common-dir do slot diverge")


# ---------------------------------------------------------------------------
# Ciclo de sessão
# ---------------------------------------------------------------------------


def open_session(cwd, allowed, run_id, deps, *, deadline=None, environ=None) -> Session:
    """Valida o repositório, escolhe um slot livre e devolve a sessão com a trava o sitio."""
    if not _RUN_ID_RE.match(str(run_id)):
        raise IsolationError("isolamento: run_id invalido")
    real_cwd = os.path.realpath(os.fspath(cwd))
    proc = _git(["rev-parse", "--show-toplevel"], real_cwd)
    if proc.returncode != 0:
        raise DirectMode("not_git")
    real_top = os.path.realpath(os.fsdecode(proc.stdout).strip())
    proc = _git(["rev-parse", "--verify", "HEAD^{commit}"], real_top)
    if proc.returncode != 0:
        raise DirectMode("no_commit")
    head = os.fsdecode(proc.stdout).strip()
    rel_cwd = os.path.relpath(real_cwd, real_top)
    if real_cwd != real_top:
        proc = _git(["check-ignore", "-q", "--", rel_cwd], real_top)
        if proc.returncode == 0:
            raise DirectMode("cwd_ignored")
    _reject_nested(real_cwd, allowed)
    common_dir = _common_dir(real_top)
    repo_id = hashlib.sha256(common_dir.encode()).hexdigest()[:16]
    try:
        repo_dir = _ensure_repo_dir(deps, repo_id, real_top, common_dir)
    except OSError:
        raise IsolationError("isolamento: falha ao criar diretorio de estado") from None
    return _acquire_slot(
        str(repo_dir), real_cwd, real_top, common_dir, repo_id, rel_cwd, deps, head, run_id,
        deadline=deadline, environ=environ,
    )


def _reject_nested(real_cwd: str, allowed) -> None:
    """Recusa (nested_repo) se diretório ancestral de algum arquivo autorizado tiver .git."""
    for rel in allowed:
        parts = [p for p in str(rel).split("/") if p not in ("", ".")]
        acc = real_cwd
        for part in parts[:-1]:
            acc = os.path.join(acc, part)
            if not acc.startswith(real_cwd + os.sep):
                break
            if os.path.lexists(os.path.join(acc, ".git")):
                raise DirectMode("nested_repo")


def _ensure_repo_dir(deps, repo_id: str, real_top: str, common_dir: str) -> Path:
    """Cria <state_dir>/worktrees/<repo_id> (0700) e repo.json se faltar; symlink recusa."""
    state = Path(deps.state_dir())
    os.makedirs(state, mode=0o700, exist_ok=True)
    worktrees = state / "worktrees"
    if os.path.islink(worktrees):
        raise IsolationError("isolamento: diretorio de worktrees e symlink")
    _mkdir_0700(worktrees)
    repo_dir = worktrees / repo_id
    if os.path.islink(repo_dir):
        raise IsolationError("isolamento: diretorio do repositorio e symlink")
    _mkdir_0700(repo_dir)
    meta = repo_dir / "repo.json"
    try:
        fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return repo_dir
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"top": real_top, "common_dir": common_dir}, fh)
    return repo_dir


def _mkdir_0700(path: Path) -> None:
    """Cria o diretório garantindo modo 0700 (umask não reduz as permissões pedidas)."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)


def _lock_path(repo_dir: str, n: int) -> str:
    return os.path.join(repo_dir, f"slot-{n}.lock")


def _open_lock(path: str):
    """Abre a trava (O_NOFOLLOW) e pega flock exclusivo sem bloquear; None se ocupada."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd: int) -> None:
    """Soltou a trava ignorando erros; fechar o fd já desfaz o flock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _acquire_slot(repo_dir, real_cwd, real_top, common_dir, repo_id, rel_cwd, deps, head, run_id,
                  *, deadline, environ) -> Session:
    """Procura slot livre até SLOT_WAIT_S ou deadline; devolve Session com a trava retida."""
    total = slot_count(environ)
    started = time.monotonic()
    while True:
        unusable = 0
        for n in range(total):
            path = _lock_path(repo_dir, n)
            fd = _open_lock(path)
            if fd is None:
                continue
            try:
                os.utime(path)
            except OSError:
                pass
            try:
                return _setup_slot(fd, n, repo_dir, real_cwd, real_top, common_dir, repo_id,
                                   rel_cwd, deps, head, run_id)
            except _SlotUnusable:
                _release_lock(fd)
                unusable += 1
            except BaseException:
                _release_lock(fd)
                raise
        if unusable >= total:
            raise IsolationError("isolamento: nenhum slot utilizavel")
        now = time.monotonic()
        if now - started >= SLOT_WAIT_S or (deadline is not None and now >= deadline):
            raise NoFreeSlot("isolamento sem slot livre")
        time.sleep(SLOT_POLL_S)


def _setup_slot(fd, n, repo_dir, real_cwd, real_top, common_dir, repo_id, rel_cwd, deps, head,
                run_id) -> Session:
    """Cria ou valida o slot n e monta a Session; guarda recusada vira _SlotUnusable."""
    slot = os.path.join(repo_dir, f"slot-{n}")
    if os.path.isdir(slot):
        try:
            guard_slot(slot, common_dir, deps)
        except SlotGuardError as exc:
            raise _SlotUnusable(str(exc)) from exc
    else:
        proc = _git(["worktree", "add", "-f", "--detach", slot, head], real_top,
                    timeout=GIT_HEAVY_TIMEOUT_S, no_hooks=True)
        if proc.returncode != 0:
            raise IsolationError("isolamento: falha ao criar slot")
    return Session(
        real_cwd=real_cwd,
        real_top=real_top,
        common_dir=common_dir,
        repo_id=repo_id,
        slot=slot,
        slot_cwd=os.path.normpath(os.path.join(slot, rel_cwd)),
        slot_index=n,
        lock_fd=fd,
        run_id=run_id,
    )


def close_session(session: Session) -> None:
    """Soltou a trava do slot marcando o último uso; idempotente e nunca levanta."""
    lock = os.path.join(os.path.dirname(session.slot), f"slot-{session.slot_index}.lock")
    try:
        os.utime(lock)
    except OSError:
        pass
    try:
        fcntl.flock(session.lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(session.lock_fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Preparação do slot
# ---------------------------------------------------------------------------


def prepare(session: Session, allowed, deps, *, environ=None) -> PrepInfo:
    """Prepara o slot: reset, overlay do estado sujo, autorizados e links; confere o resultado."""
    started_ts = time.time()
    started = time.monotonic()
    for _ in range(2):  # divergência na conferência refaz o ciclo uma vez
        snap_before = deps.snapshot_allowed(session.real_cwd, allowed)  # RuntimeError propaga
        try:
            info = _prepare_attempt(session, allowed, deps, environ, snap_before, started_ts, started)
        except OSError:
            # Disco cheio, permissão etc.: erro limpo para o roteador, nunca traceback.
            raise IsolationError("isolamento: falha de arquivo ao preparar slot") from None
        if info is not None:
            return info
    raise IsolationError("isolamento: arquivo autorizado mudou durante a preparacao")


def _prepare_attempt(session, allowed, deps, environ, snap_before, started_ts, started):
    """Um ciclo de preparação; devolve PrepInfo ou None quando a conferência diverge."""
    guard_slot(session.slot, session.common_dir, deps)
    sha = _head_sha(session.real_top)
    _reset_slot(session, sha)
    overlay_files, skipped_sensitive, skipped_nested = _apply_overlay(session, deps, environ)
    _validate_slot_symlinks(session)
    _copy_allowed(session, allowed, deps)
    links = _create_links(session, deps, environ)
    os.makedirs(session.slot_cwd, exist_ok=True)
    snap_slot_before = deps.snapshot_allowed(session.slot_cwd, allowed)
    for rel in allowed:
        if snap_slot_before[rel][:2] != snap_before[rel][:2]:
            return None  # existe + hash divergem; modo não entra
    return PrepInfo(
        snap_before=snap_before,
        snap_slot_before=snap_slot_before,
        overlay_files=overlay_files,
        skipped_sensitive=skipped_sensitive,
        skipped_nested=skipped_nested,
        links=links,
        prep_s=time.monotonic() - started,
        started_ts=started_ts,
    )


def _reset_slot(session, sha: str) -> None:
    """checkout --detach -f e clean -ffdx no slot (nunca na árvore real)."""
    for args in (["checkout", "--detach", "-f", sha], ["clean", "-ffdx"]):
        proc = _git(args, session.slot, timeout=GIT_HEAVY_TIMEOUT_S, no_hooks=True)
        if proc.returncode != 0:
            raise IsolationError("isolamento: falha ao limpar slot")


def _validate_slot_symlinks(session: Session) -> None:
    """Remove do slot todo symlink que aponte fora da cópia.

    O checkout repõe symlinks versionados limpos sem passar por _copy_symlink
    (que só vê o overlay): um link versionado absoluto ou em fuga daria ao
    worker acesso a arquivos fora do slot, inclusive na árvore real.
    """

    def _negar(erro):
        raise IsolationError("isolamento: falha ao varrer symlinks do slot") from erro

    for base, dirs, files in os.walk(session.slot, followlinks=False, onerror=_negar):
        for nome in dirs + files:
            caminho = os.path.join(base, nome)
            if not os.path.islink(caminho):
                continue
            try:
                alvo = os.readlink(caminho)
            except OSError:
                _remove_in_slot(caminho)  # ilegível não pode ser provado seguro
                continue
            if _symlink_foge(session.slot, caminho, alvo):
                _remove_in_slot(caminho)


def _apply_overlay(session, deps, environ):
    """Copia o estado sujo da árvore real para o slot; devolve os contadores do PrepInfo."""
    proc = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], session.real_top)
    if proc.returncode != 0:
        raise IsolationError("isolamento: falha ao ler status do repositorio")
    rels = [os.fsdecode(raw) for raw in deps.parse_status(proc.stdout)]
    if len(rels) > deps.max_paths:
        raise DirectMode("overlay_limit")
    max_overlay = _max_overlay(environ)
    total = 0
    overlay_files = skipped_sensitive = skipped_nested = 0
    for rel in rels:
        if rel.endswith("/"):
            skipped_nested += 1
            continue
        parts = rel.split("/")
        if os.path.isabs(rel) or any(p in ("", "..") for p in parts):
            continue
        if any(deps.is_sensitive(p) for p in parts):
            skipped_sensitive += 1
            continue
        src = os.path.join(session.real_top, rel)
        dst = os.path.join(session.slot, rel)
        try:
            st = os.lstat(src)
        except OSError:
            st = None  # origem sumiu no meio: tratar como ausente
        if st is None:
            _remove_in_slot(dst)
            overlay_files += 1
        elif stat.S_ISREG(st.st_mode):
            _ensure_parent_dir(session.slot, dst)
            try:
                written = _copy_regular(src, dst, deps.max_file_bytes)
            except FileNotFoundError:
                _remove_in_slot(dst)
                written = 0
            if written is None:  # deixou de ser arquivo regular: pular
                continue
            overlay_files += 1
            total += written
            if total > max_overlay:
                raise DirectMode("overlay_limit")
        elif stat.S_ISLNK(st.st_mode):
            _copy_symlink(session.slot, src, dst)
            overlay_files += 1
        # outros tipos: pular
    return overlay_files, skipped_sensitive, skipped_nested


def _copy_allowed(session, allowed, deps) -> None:
    """Copia os autorizados que existem como arquivo regular no cwd real para o slot_cwd."""
    for rel in allowed:
        src = os.path.join(session.real_cwd, os.fspath(rel))
        dst = os.path.join(session.slot_cwd, os.fspath(rel))
        try:
            st = os.lstat(src)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        _ensure_parent_dir(session.slot, dst)
        try:
            _copy_regular(src, dst, deps.max_file_bytes)
        except FileNotFoundError:
            continue


def _create_links(session, deps, environ) -> list:
    """Cria no slot symlinks para diretórios de dependência ignorados pelo git."""
    links = []
    for base in dict.fromkeys([session.real_top, session.real_cwd]):  # ordem, sem repetir
        for name in _link_names(environ):
            if deps.is_sensitive(name):
                continue
            path = os.path.join(base, name)
            if not os.path.isdir(path):
                continue
            rel = os.path.relpath(path, session.real_top)
            proc = _git(["check-ignore", "-q", "--", rel], session.real_top)
            if proc.returncode != 0:
                continue
            target = os.path.join(session.slot, rel)
            if os.path.lexists(target):
                continue
            _ensure_parent_dir(session.slot, target)
            os.symlink(path, target)
            links.append(rel)
    return links


# ---------------------------------------------------------------------------
# Escrita e remoção dentro do slot (nunca na árvore real)
# ---------------------------------------------------------------------------


def _copy_regular(src: str, dst: str, max_file_bytes: int):
    """Copia arquivo regular src -> dst com o modo da origem; devolve bytes ou None se não regular."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(src, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > max_file_bytes:
            raise DirectMode("overlay_limit")
        _clear_slot_path(dst)
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            written = 0
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                _write_all(out, chunk)
                written += len(chunk)
            os.fchmod(out, stat.S_IMODE(st.st_mode))
        finally:
            os.close(out)
        return written
    finally:
        os.close(fd)


def _symlink_foge(slot_root: str, dst: str, alvo: str) -> bool:
    """True quando o symlink em dst com alvo aponta fora do slot_root."""
    # Alvo absoluto ou que sai do slot daria ao worker leitura/escrita fora
    # da cópia: o link não pode existir no slot.
    raiz = os.path.normpath(slot_root)
    final = os.path.normpath(os.path.join(os.path.dirname(dst), alvo))
    try:
        dentro = os.path.commonpath([raiz, final]) == raiz
    except ValueError:  # mistura de caminho absoluto e relativo: inconfundível
        dentro = False
    return os.path.isabs(alvo) or not dentro


def _copy_symlink(slot_root: str, src: str, dst: str) -> None:
    """Recria no slot o symlink da origem; alvo ilegível vira remoção do destino."""
    try:
        target = os.readlink(src)
    except OSError:
        _remove_in_slot(dst)
        return
    if _symlink_foge(slot_root, dst, target):
        _remove_in_slot(dst)
        return
    _ensure_parent_dir(slot_root, dst)
    _remove_in_slot(dst)
    os.symlink(target, dst)


def _write_all(fd: int, data: bytes) -> None:
    """Escreve todos os bytes; OSError propaga."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _clear_slot_path(dst: str) -> None:
    """Garante que dst possa virar arquivo: remove se existir e não for arquivo regular."""
    try:
        st = os.lstat(dst)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        _remove_in_slot(dst)


def _ensure_parent_dir(slot_root: str, dst: str) -> None:
    """Cria os diretórios pais de dst no slot, substituindo ancestral arquivo/symlink."""
    chain = []
    cur = os.path.dirname(dst)
    root = os.fspath(slot_root) + os.sep
    while (cur + os.sep).startswith(root) and cur != os.fspath(slot_root):
        chain.append(cur)
        cur = os.path.dirname(cur)
    for path in reversed(chain):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            os.mkdir(path, 0o755)
            continue
        if stat.S_ISDIR(st.st_mode):
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.mkdir(path, 0o755)


def _remove_in_slot(path: str) -> None:
    """Remove arquivo, symlink ou diretório dentro do slot; ausente é ignorado."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        st = None  # ex.: laço de symlink; o unlink abaixo resolve o próprio elo
    if st is not None and stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except IsADirectoryError:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def rewrite_prompt(prompt: bytes, session: Session, cwd_arg) -> bytes:
    """Troca o topo real pelo slot nas formas resolvida e não resolvida; a mais longa primeiro."""
    forms = [os.fsencode(session.real_top)]
    rel = os.path.relpath(session.real_cwd, session.real_top)
    abscwd = os.path.abspath(os.fspath(cwd_arg))
    if rel == ".":
        cand = abscwd
    elif abscwd.endswith(os.sep + rel):
        cand = abscwd[: -len(rel) - len(os.sep)]
    else:
        cand = None
    if cand is not None and cand != session.real_top:
        forms.append(os.fsencode(cand))
    slot_b = os.fsencode(session.slot)
    # Uma passada só: o caminho do slot já inserido nunca é reexaminado (o topo real pode
    # ser prefixo do diretório de estado).
    alternatives = b"|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
    pattern = b"(?:" + alternatives + rb")(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])"
    return re.sub(pattern, lambda _match: slot_b, prompt)


def isolation_notice(session: Session) -> str:
    """Aviso exibido ao worker sobre o diretório isolado."""
    return ("[worker-router] Execucao isolada: o projeto esta em " + session.slot_cwd
            + ". Trabalhe somente aqui.")


# ---------------------------------------------------------------------------
# Parte 2: devolução, diário, rejeitados e limpeza
# ---------------------------------------------------------------------------


def sync_back(session: Session, allowed, prep, deps) -> SyncResult:
    """Devolve mudanças dos autorizados para a árvore real; conflito marca tainted.

    Conferência da árvore real, escrita e diário correm sob flock em sync.lock
    do repositório: devoluções simultâneas de slots distintos não leem o mesmo
    estado e não sobrescrevem umas às outras. Sem a trava nada é devolvido
    (tudo vira conflito; escrever sem excluir pode perder devolução silenciosamente).
    """
    if prep is None:
        return SyncResult([], [])
    antigo = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        applied: list = []
        conflicts: list = []
        lock_fd = _sync_lock(session)
        if lock_fd is None:
            conflicts.extend(allowed)  # sem exclusão mútua não há devolução segura
        else:
            try:
                for rel in allowed:
                    try:
                        _sync_one(session, rel, prep, deps, applied, conflicts)
                    except Exception:
                        conflicts.append(rel)  # falha em um não derruba os outros
                _log_sync(session, applied)
            finally:
                _release_lock(lock_fd)
        if conflicts:
            session.tainted = True
        return SyncResult(applied, conflicts)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, antigo)


def _sync_lock(session: Session):
    """Abre sync.lock do repositório e retém flock exclusivo; None se não conseguiu."""
    try:
        fd = os.open(os.path.join(os.path.dirname(session.slot), "sync.lock"),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        _release_lock(fd)
        return None
    return fd


def _sync_one(session, rel, prep, deps, applied, conflicts) -> None:
    """Sincroniza um rel comparando os snapshots da preparação; erro vira conflito."""
    try:
        agora_slot = deps.snapshot_allowed(session.slot_cwd, [rel])[rel]
    except RuntimeError:
        conflicts.append(rel)
        return
    if agora_slot[:2] == prep.snap_slot_before[rel][:2]:
        return  # sem mudança de conteúdo; mudança só de modo não é devolvida
    try:
        agora_real = deps.snapshot_allowed(session.real_cwd, [rel])[rel]
    except RuntimeError:
        conflicts.append(rel)
        return
    if agora_real[:2] != prep.snap_before[rel][:2]:
        conflicts.append(rel)  # a árvore real mudou durante a execução
        return
    dst = os.path.join(session.real_cwd, rel)
    if agora_slot[0]:
        ok = _sync_copy_file(session, rel, dst, agora_real, agora_slot, deps)
    else:
        ok = _sync_remove_real(session.real_cwd, rel, dst)
    if ok:
        applied.append(rel)
    else:
        conflicts.append(rel)


def _sync_copy_file(session, rel, dst, agora_real, agora_slot, deps) -> bool:
    """Grava na árvore real o conteúdo do slot via temporário + os.replace."""
    tmp = None
    try:
        src = os.path.join(session.slot_cwd, rel)
        data = _read_regular(src, deps.max_file_bytes)
        if data is None:
            return False
        if not _ancestors_not_symlink(session.real_cwd, rel):
            return False
        parent = os.path.dirname(dst)
        os.makedirs(parent, exist_ok=True)
        nome = f".{os.path.basename(dst)}.wa-sync-{session.run_id[:8]}.tmp"
        tmp = os.path.join(parent, nome)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
            try:
                os.fchmod(fd, _sync_target_mode(agora_real, agora_slot))
            except OSError:
                pass  # falha de chmod é ignorada
        finally:
            os.close(fd)
        os.replace(tmp, dst)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            _remove_quietly(tmp)


def _sync_remove_real(base, rel, dst) -> bool:
    """Apaga na árvore real o autorizado removido no slot; só arquivo regular."""
    if not _ancestors_not_symlink(base, rel):
        return False
    try:
        st = os.lstat(dst)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        os.unlink(dst)
    except OSError:
        return False
    return True


def _sync_target_mode(agora_real, agora_slot) -> int:
    """Modo final do devolvido: o que a árvore real já tinha; novo herda 644/755 do slot."""
    if agora_real[0]:
        return stat.S_IMODE(int(agora_real[2]))
    return 0o755 if int(agora_slot[2]) & 0o111 else 0o644


def _ancestors_not_symlink(base, rel) -> bool:
    """Nenhum diretório ancestral de base/rel (entre base e o arquivo) é symlink."""
    parts = os.fspath(rel).split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        return False
    cur = base
    for part in parts[:-1]:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return False
    return True


def _read_regular(path, cap: int):
    """Lê até cap bytes de arquivo regular; None se sumiu, não regular ou grande demais."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                return None
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks)


def _log_sync(session, applied) -> None:
    """Acrescenta a devolução ao sync.jsonl; a trava do repositório já está retida."""
    if not applied:
        return
    _write_sync_entry(os.path.dirname(session.slot), session, applied)


def _write_sync_entry(repo_dir, session, applied) -> None:
    """Rotaciona o diário cheio e grava a linha JSON; OSError fica em silêncio."""
    try:
        log_path = os.path.join(repo_dir, "sync.jsonl")
        try:
            cheio = os.path.getsize(log_path) > SYNC_LOG_MAX_BYTES
        except OSError:
            cheio = False
        if cheio:
            os.replace(log_path, os.path.join(repo_dir, "sync.jsonl.1"))
        entry = {
            "ts": time.time(),
            "run_id": session.run_id,
            "paths": [os.path.normpath(os.path.join(session.real_cwd, rel)) for rel in applied],
        }
        # Diário privado como o resto do estado; nunca segue symlink plantado.
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def synced_by_others(session: Session, since_ts, deps) -> set:
    """Caminhos absolutos devolvidos por outro run_id com ts >= since_ts; nunca levanta."""
    out: set = set()
    repo_dir = os.path.dirname(session.slot)
    for nome in ("sync.jsonl.1", "sync.jsonl"):
        path = os.path.join(repo_dir, nome)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for linha in fh:
                    _collect_sync_line(linha, session.run_id, since_ts, out)
        except OSError:
            continue  # diário ausente ou ilegível: pula só este arquivo
    return out


def _collect_sync_line(linha, run_id, since_ts, out) -> None:
    """Valida uma linha do diário e junta os caminhos de outro run no conjunto."""
    try:
        entry = json.loads(linha)
    except ValueError:
        return
    if not isinstance(entry, dict):
        return
    ts = entry.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts < since_ts:
        return
    outro = entry.get("run_id")
    if not isinstance(outro, str) or outro == run_id:
        return
    paths = entry.get("paths")
    if not isinstance(paths, list):
        return
    for path in paths:
        if isinstance(path, str):
            out.add(path)


def save_rejected(session: Session, rel_paths, deps) -> RejectedInfo:
    """Guarda cópias dos descartados em <state>/rejected/<run_id>; erro vira skipped."""
    saved = skipped = 0
    run_dir: Optional[str] = None
    rels: list = []
    try:
        rels = [os.fspath(rel) for rel in rel_paths]
        run_dir = str(Path(deps.state_dir()) / "rejected" / session.run_id)
        rejected_root = os.path.dirname(run_dir)
        files_used, bytes_used = _rejected_usage(run_dir)
        for rel in rels:
            data, rel_slot = _rejected_source(session, rel, deps)
            if data is None:
                skipped += 1
                continue
            if files_used >= REJECTED_MAX_FILES or bytes_used + len(data) > REJECTED_MAX_BYTES:
                skipped += 1
                continue
            if _rejected_write(rejected_root, run_dir, rel_slot, data):
                saved += 1
                files_used += 1
                bytes_used += len(data)
            else:
                skipped += 1
    except Exception:
        skipped += max(len(rels) - saved - skipped, 0)
    return RejectedInfo(run_dir if saved > 0 else None, saved, skipped)


def _rejected_source(session, rel, deps):
    """Lê do slot o arquivo descartado; devolve (bytes, rel ao slot) ou (None, "")."""
    abs_path = os.path.normpath(os.path.join(session.slot_cwd, rel))
    if not abs_path.startswith(session.slot + os.sep):
        return None, ""  # escapa do slot ou é o próprio slot
    rel_slot = os.path.relpath(abs_path, session.slot)
    if any(deps.is_sensitive(p) for p in rel_slot.split(os.sep)):
        return None, ""
    if not _ancestors_not_symlink(session.slot, rel_slot):
        return None, ""
    data = _read_regular(abs_path, REJECTED_MAX_BYTES + 1)
    if data is None or len(data) > REJECTED_MAX_BYTES:
        return None, ""
    return data, rel_slot


def _rejected_write(rejected_root, run_dir, rel_slot, data) -> bool:
    """Grava a cópia 0600 sob run_dir com diretórios 0700; False em qualquer falha."""
    dest = os.path.join(run_dir, rel_slot)
    criado = False
    try:
        chain = [rejected_root, run_dir]
        cur = run_dir
        for part in rel_slot.split(os.sep)[:-1]:
            cur = os.path.join(cur, part)
            chain.append(cur)
        for path in chain:
            if os.path.islink(path):
                return False  # escrita nunca atravessa symlink
            _mkdir_0700(Path(path))
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        criado = True
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        return True
    except OSError:
        if criado:
            _remove_quietly(dest)
        return False


def _rejected_usage(run_dir):
    """Arquivos regulares e bytes já presentes em run_dir; ausente conta como zero."""
    files = total = 0
    for cur, _subdirs, names in os.walk(run_dir):
        for name in names:
            try:
                st = os.lstat(os.path.join(cur, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                files += 1
                total += st.st_size
    return files, total


def list_slots(deps) -> list:
    """Lista travas de slot com estado e ocupação; ordena por (repo_id, slot); nunca levanta."""
    itens: list = []
    try:
        worktrees = str(Path(deps.state_dir()) / "worktrees")
        repo_ids = sorted(os.listdir(worktrees))
    except Exception:
        return []
    for repo_id in repo_ids:
        repo_dir = os.path.join(worktrees, repo_id)
        try:
            nomes = sorted(os.listdir(repo_dir))
        except OSError:
            continue
        repo_top = _repo_top(repo_dir)
        for nome in nomes:
            match = _LOCK_RE.fullmatch(nome)
            if match is None:
                continue
            n = int(match.group("n"))
            lock_path = os.path.join(repo_dir, nome)
            slot_path = os.path.join(repo_dir, f"slot-{n}")
            itens.append({
                "repo_id": repo_id,
                "repo": repo_top,
                "slot": n,
                "path": slot_path,
                "exists": os.path.isdir(slot_path),
                "busy": _lock_busy(lock_path),
                "last_used": _lock_mtime(lock_path),
            })
    itens.sort(key=lambda item: (item["repo_id"], item["slot"]))
    return itens


def _load_repo_json(repo_dir):
    """repo.json como dict; None se ausente, ilegível ou JSON inválido; nunca levanta."""
    try:
        with open(os.path.join(repo_dir, "repo.json"), "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _repo_top(repo_dir):
    """Campo "top" do repo.json; None se ausente ou não textual; nunca levanta."""
    meta = _load_repo_json(repo_dir)
    top = meta.get("top") if isinstance(meta, dict) else None
    return top if isinstance(top, str) else None


def _lock_busy(lock_path) -> bool:
    """True se a trava está ocupada (ou não pôde ser aberta); livre, solta na hora."""
    fd = _open_lock(lock_path)
    if fd is None:
        return True
    _release_lock(fd)
    return False


def _lock_mtime(lock_path):
    """mtime da trava ou None se sumiu; nunca levanta."""
    try:
        return os.stat(lock_path).st_mtime
    except OSError:
        return None


def gc(deps, *, now=None, slot_max_age_s: float = 14 * 86400,
       rejected_max_age_s: float = 7 * 86400) -> dict:
    """Remove slots órfãos ou velhos e rejeitados antigos; falhas somam em errors."""
    if now is None:
        now = time.time()
    resultado = {"slots_removed": 0, "rejected_removed": 0, "errors": 0}
    try:
        state = Path(deps.state_dir())
    except Exception:
        resultado["errors"] += 1
        return resultado
    _gc_slots(resultado, str(state / "worktrees"), deps, now, slot_max_age_s)
    _gc_rejected(resultado, str(state / "rejected"), now, rejected_max_age_s)
    return resultado


def _gc_slots(resultado, worktrees, deps, now, max_age) -> None:
    """Varre as travas de slot e limpa o que for órfão ou velho; nunca levanta."""
    try:
        repo_ids = sorted(os.listdir(worktrees))
    except OSError:
        return  # sem diretório de worktrees não há o que limpar
    for repo_id in repo_ids:
        repo_dir = os.path.join(worktrees, repo_id)
        if not os.path.isdir(repo_dir) or os.path.islink(repo_dir):
            continue
        try:
            nomes = sorted(os.listdir(repo_dir))
        except OSError:
            resultado["errors"] += 1
            continue
        meta = _load_repo_json(repo_dir)
        for nome in nomes:
            match = _LOCK_RE.fullmatch(nome)
            if match is None:
                continue
            fd = _open_lock(os.path.join(repo_dir, nome))
            if fd is None:
                continue  # ocupada ou ilegível: pular
            try:
                _gc_slot(resultado, repo_dir, int(match.group("n")), meta, deps, now,
                         max_age, worktrees)
            except Exception:
                resultado["errors"] += 1
            finally:
                _release_lock(fd)


def _gc_slot(resultado, repo_dir, n, meta, deps, now, max_age, worktrees) -> None:
    """Limpa um slot com a trava retida: órfão via rmtree confinado; velho via git."""
    slot = os.path.join(repo_dir, f"slot-{n}")
    top = meta.get("top") if isinstance(meta, dict) else None
    common = meta.get("common_dir") if isinstance(meta, dict) else None
    saudavel = isinstance(top, str) and isinstance(common, str) and os.path.isdir(common)
    if not saudavel:
        if not os.path.lexists(slot):
            return
        if _confined_rmtree(slot, worktrees):
            resultado["slots_removed"] += 1
        else:
            resultado["errors"] += 1
        return
    lock_path = os.path.join(repo_dir, f"slot-{n}.lock")
    try:
        velho = now - os.stat(lock_path).st_mtime > max_age
    except OSError:
        resultado["errors"] += 1
        return
    if not velho or not os.path.isdir(slot):
        return
    try:
        guard_slot(slot, common, deps)
    except IsolationError:
        resultado["errors"] += 1
        return
    # O topo gravado pode ter sido um worktree temporário já removido: o diretório git comum serve.
    base = top if os.path.isdir(top) else common
    try:
        proc = _git(["worktree", "remove", "--force", slot], base,
                    timeout=GIT_HEAVY_TIMEOUT_S, no_hooks=True)
    except IsolationError:
        resultado["errors"] += 1
        return
    if proc.returncode == 0:
        resultado["slots_removed"] += 1
    else:
        resultado["errors"] += 1


def _confined_rmtree(path, root) -> bool:
    """rmtree só se path não é symlink e realpath fica dentro de realpath(root)."""
    if os.path.islink(path):
        return False
    if not os.path.realpath(path).startswith(os.path.realpath(root) + os.sep):
        return False
    try:
        shutil.rmtree(path)
    except OSError:
        return False
    return True


def _gc_rejected(resultado, root, now, max_age) -> None:
    """Remove diretórios de rejeitados mais velhos que max_age; symlink nunca é seguido."""
    try:
        nomes = sorted(os.listdir(root))
    except OSError:
        return  # sem diretório de rejeitados não há o que limpar
    for nome in nomes:
        path = os.path.join(root, nome)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            velho = now - os.stat(path).st_mtime > max_age
        except OSError:
            resultado["errors"] += 1
            continue
        if not velho:
            continue
        if _confined_rmtree(path, root):
            resultado["rejected_removed"] += 1
        else:
            resultado["errors"] += 1


def _remove_quietly(path) -> None:
    """unlink ignorando qualquer erro; só para limpar temporário próprio."""
    try:
        os.unlink(path)
    except OSError:
        pass
