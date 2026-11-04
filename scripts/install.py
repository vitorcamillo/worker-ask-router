#!/usr/bin/env python3
"""Instala somente código da suíte; credenciais e serviços ficam externos."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
WRAPPERS = ("codex-ask", "claude-ask", "muse-ask", "opencode-ask", "bonsai-ask", "codex-local")
AUXILIARIES = ("ask_presentation.py", "zcode-cli", "zcode_headless_admit.py", "ask-doctor", "ask-check")


def entries(repo: Path, home: Path):
    result = [(repo / "worker_router.py", home / ".local/bin/worker-ask", False)]
    result.extend((repo / name, home / ".local/bin" / name, False) for name in ("quota_reset.py", "quota_provider.py"))
    result.extend((repo / "bin" / name, home / ".local/bin" / name, False)
                  for name in WRAPPERS + AUXILIARIES)
    result.append((repo / "bin/zcode-worker", home / ".local/bin/zcode-worker", True))
    groups = (
        ("src/zcode_worker", ("__init__.py", "base.py", "bridge.py", "cli.py", "features.py", "jobs.py")),
        ("auth", ("launch.cjs", "zai-session.cjs")),
        ("bin", ("zcode-worker-next",)),
    )
    for folder, names in groups:
        result.extend((repo / "adapters/zcode" / folder / name,
                       home / ".local/share/zcode-worker" / folder / name, False) for name in names)
    result.extend((repo / "adapters/bonsai/scripts" / name,
                   home / ".local/share/bonsai/scripts" / name, False)
                  for name in ("bonsai_client.py", "game_guard.py", "gpu_coord.py"))
    return result


def _parents(path: Path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise RuntimeError(f"pai simbólico recusado: {parent}")
        if parent.exists() and not parent.is_dir():
            raise RuntimeError(f"pai não é diretório: {parent}")


def preflight(items, state: Path):
    _parents(state)
    for source, dest, _copy in items:
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"fonte de código ausente ou inválida: {source}")
        _parents(dest.parent)
        try:
            info = dest.lstat()
            mode = info.st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISREG(mode) and info.st_nlink != 1:
            raise RuntimeError(f"destino de código compartilhado por hardlink: {dest}")
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise RuntimeError(f"destino de código inválido: {dest}")


def unchanged(source: Path, dest: Path, copy: bool):
    if dest.is_symlink():
        return not copy and os.readlink(dest) == str(source)
    return (copy and dest.is_file() and stat.S_IMODE(dest.stat().st_mode) == 0o755
            and dest.read_bytes() == source.read_bytes())


def install(home: Path, repo: Path = ROOT):
    home = home.absolute()
    repo = repo.resolve()
    items = entries(repo, home)
    state = home / ".local/state/worker-ask"
    preflight(items, state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state / "install.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("outra instalação está em andamento") from None
        preflight(items, state)
        changes = [(s, d, c) for s, d, c in items if not unchanged(s, d, c)]
        if not changes:
            return {"installed": [], "backup": None}
        backup = state / "install-backups" / uuid.uuid4().hex
        _parents(backup)
        backup.mkdir(parents=True, mode=0o700)
        ledger = []
        staged = []
        try:
            for source, dest, copy in changes:
                dest.parent.mkdir(parents=True, exist_ok=True)
                temp = dest.with_name(f".{dest.name}.install-{uuid.uuid4().hex}")
                staged.append(temp)
                if copy:
                    with temp.open("xb") as output, source.open("rb") as input_file:
                        shutil.copyfileobj(input_file, output)
                    temp.chmod(0o755)
                else:
                    temp.symlink_to(source)
                saved = None
                if dest.exists() or dest.is_symlink():
                    saved = backup / dest.relative_to(home)
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    if dest.is_symlink():
                        saved.symlink_to(os.readlink(dest))
                    else:
                        shutil.copy2(dest, saved, follow_symlinks=False)
                ledger.append((dest, saved))
                os.replace(temp, dest)
        except BaseException as error:
            failures = []
            for dest, saved in reversed(ledger):
                try:
                    if saved is None:
                        dest.unlink(missing_ok=True)
                    else:
                        try:
                            os.replace(saved, dest)
                        except OSError as exc:
                            if exc.errno != errno.EXDEV:
                                raise
                            # Backup e destino em filesystems distintos:
                            # copia ao temporário no diretório do destino e
                            # troca atômica dali, preservando o backup.
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            temp = dest.with_name(
                                f".{dest.name}.restore-{uuid.uuid4().hex}")
                            try:
                                if saved.is_symlink():
                                    temp.symlink_to(os.readlink(saved))
                                else:
                                    shutil.copy2(saved, temp, follow_symlinks=False)
                                os.replace(temp, dest)
                            finally:
                                temp.unlink(missing_ok=True)
                except OSError:
                    failures.append(dest)
            if failures:
                raise RuntimeError(f"restauração incompleta; backups preservados em {backup}") from error
            raise RuntimeError("falha na instalação; alterações revertidas") from error
        finally:
            for temp in staged:
                temp.unlink(missing_ok=True)
        return {"installed": [str(d) for _s, d, _c in changes], "backup": str(backup)}
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    try:
        result = install(args.home)
    except (OSError, RuntimeError) as error:
        print(f"instalação recusada: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
