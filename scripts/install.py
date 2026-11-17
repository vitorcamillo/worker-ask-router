#!/usr/bin/env python3
"""Instala o código, as pontes e as units do worker-ask; credenciais ficam externas.

Verifica antes de agir: --check só relata; sem flag instala só o que difere;
--enable habilita só as units ainda não habilitadas.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
WRAPPERS = ("codex-ask", "claude-ask", "muse-ask", "opencode-ask", "bonsai-ask", "codex-local")
AUXILIARIES = ("ask_presentation.py", "zcode-cli", "zcode_headless_admit.py", "ask-doctor", "ask-check")
BRIDGE_SCRIPTS = (
    ("pontes/ask-claude-bridge/ask-claude-bridge.py", "ask-claude-bridge.py"),
    ("pontes/ask-gpt-bridge/ask-gpt-bridge.py", "ask-gpt-bridge.py"),
    ("pontes/ask-glm-bridge/ask-glm-bridge.py", "ask-glm-bridge.py"),
    ("pontes/comum/ask-bridges-smoke", "ask-bridges-smoke"),
)
BRIDGE_MODULES = (
    ("pontes/ask-glm-bridge/ask_bridge_anthropic.py", "ask_bridge_anthropic.py"),
    ("pontes/comum/ask_bridge_process.py", "ask_bridge_process.py"),
    ("pontes/comum/ask_bridge_stream.py", "ask_bridge_stream.py"),
    ("pontes/comum/ask_bridge_zcode_live.py", "ask_bridge_zcode_live.py"),
)
ENABLE = (
    "ask-check.timer",
    "ask-limites-zcode.timer",
    "worker-ask-disponibilidade.timer",
    "zcode-mcp-reaper.timer",
    "ask-claude-bridge.service",
    "ask-glm-bridge.service",
    "ask-gpt-bridge.service",
)
SYSTEMCTL_TIMEOUT = 30


def _unit_entries(repo: Path, home: Path):
    root = repo / "systemd"
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in (".service", ".timer"):
            sources = [path]
        elif path.is_dir() and path.name.endswith(".service.d"):
            sources = sorted(entry for entry in path.iterdir()
                             if entry.is_file() and entry.suffix == ".conf")
        else:
            continue
        for source in sources:
            relative = source.relative_to(root)
            result.append((source, home / ".config/systemd/user" / relative, "unit", 0o644))
    return result


def entries(repo: Path, home: Path):
    result = [(repo / "worker_router.py", home / ".local/bin/worker-ask", "link", 0o644)]
    result.extend((repo / name, home / ".local/bin" / name, "link", 0o644) for name in ("quota_reset.py", "quota_provider.py", "availability.py", "worker_isolation.py"))
    result.extend((repo / "bin" / name, home / ".local/bin" / name, "link", 0o644)
                  for name in WRAPPERS + AUXILIARIES)
    result.append((repo / "bin/zcode-worker", home / ".local/bin/zcode-worker", "copy", 0o755))
    # cópias: os timers precisam rodar mesmo com o disco do repo desmontado
    result.extend((repo / "bin" / name, home / ".local/bin" / name, "copy", 0o755)
                  for name in ("ask-limites-zcode", "zcode-mcp-reaper"))
    result.append((repo / "adapters/zcode/pyproject.toml",
                   home / ".local/share/zcode-worker/pyproject.toml", "link", 0o644))
    groups = (
        ("src/zcode_worker", ("__init__.py", "base.py", "bridge.py", "cli.py", "features.py", "jobs.py")),
        ("auth", ("launch.cjs", "zai-session.cjs")),
        ("bin", ("zcode-worker-next",)),
    )
    for folder, names in groups:
        result.extend((repo / "adapters/zcode" / folder / name,
                       home / ".local/share/zcode-worker" / folder / name, "link", 0o644) for name in names)
    result.extend((repo / "adapters/bonsai/scripts" / name,
                   home / ".local/share/bonsai/scripts" / name, "link", 0o644)
                  for name in ("bonsai_client.py", "game_guard.py", "gpu_coord.py"))
    result.extend((repo / source, home / ".local/bin" / name, "copy", 0o755)
                  for source, name in BRIDGE_SCRIPTS)
    result.extend((repo / source, home / ".local/bin" / name, "copy", 0o644)
                  for source, name in BRIDGE_MODULES)
    result.extend(_unit_entries(repo, home))
    return result


def _parents(path: Path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise RuntimeError(f"pai simbólico recusado: {parent}")
        if parent.exists() and not parent.is_dir():
            raise RuntimeError(f"pai não é diretório: {parent}")


def preflight(items, state: Path):
    _parents(state)
    for source, dest, _kind, _mode in items:
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


def _nvm_node(home: Path):
    """node mais novo do nvm (shells não interativos não carregam o nvm no PATH)."""
    def versao(path: Path):
        partes = path.parent.parent.name.lstrip("v").split(".")
        return tuple(int(p) if p.isdigit() else -1 for p in partes)
    nodes = [p for p in (home / ".nvm/versions/node").glob("*/bin/node") if p.is_file()]
    return str(max(nodes, key=versao)) if nodes else None


def _node_bin_dir(home: Path) -> str:
    configured = os.environ.get("NODE_BIN_DIR")
    if configured:
        return configured
    node = shutil.which("node") or _nvm_node(home)
    if node:
        directory = str(Path(node).resolve().parent)
        prefix = str(home)
        if directory == prefix:
            return "%h"
        if directory.startswith(prefix + os.sep):
            return "%h" + directory[len(prefix):]
        return directory
    return "%h/.local/bin"


def render_unit(source: Path, home: Path) -> bytes:
    text = source.read_bytes().decode("utf-8")
    return text.replace("@NODE_BIN_DIR@", _node_bin_dir(home)).encode("utf-8")


def _expected(source: Path, kind: str, home: Path) -> bytes:
    return render_unit(source, home) if kind == "unit" else source.read_bytes()


def unchanged(source: Path, dest: Path, kind: str, mode: int, home: Path):
    if kind == "link":
        return dest.is_symlink() and os.readlink(dest) == str(source)
    if dest.is_symlink() or not dest.is_file():
        return False
    if stat.S_IMODE(dest.stat().st_mode) != mode:
        return False
    return dest.read_bytes() == _expected(source, kind, home)


def _pending_state(source: Path, dest: Path, kind: str, mode: int, home: Path):
    try:
        info = dest.lstat()
    except FileNotFoundError:
        return "faltando"
    if kind == "link":
        if not stat.S_ISLNK(info.st_mode):
            return "link-errado"
        if os.readlink(dest) != str(source):
            return "link-errado"
        return None
    if not stat.S_ISREG(info.st_mode):
        return "diferente"
    if dest.read_bytes() != _expected(source, kind, home):
        return "diferente"
    if stat.S_IMODE(info.st_mode) != mode:
        return "modo"
    return None


def check(home: Path, repo: Path = ROOT):
    home = home.absolute()
    repo = repo.resolve()
    items = entries(repo, home)
    preflight(items, home / ".local/state/worker-ask")
    pendentes = []
    for source, dest, kind, mode in items:
        estado = _pending_state(source, dest, kind, mode, home)
        if estado:
            pendentes.append({"destino": str(dest), "tipo": kind, "estado": estado})
    return {"pendentes": pendentes, "total": len(pendentes)}


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
        changes = [(s, d, k, m) for s, d, k, m in items if not unchanged(s, d, k, m, home)]
        if not changes:
            return {"installed": [], "backup": None, "units_alteradas": False}
        backup = state / "install-backups" / uuid.uuid4().hex
        _parents(backup)
        backup.mkdir(parents=True, mode=0o700)
        ledger = []
        staged = []
        try:
            for source, dest, kind, mode in changes:
                dest.parent.mkdir(parents=True, exist_ok=True)
                temp = dest.with_name(f".{dest.name}.install-{uuid.uuid4().hex}")
                staged.append(temp)
                if kind == "link":
                    temp.symlink_to(source)
                else:
                    with temp.open("xb") as output:
                        output.write(_expected(source, kind, home))
                    temp.chmod(mode)
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
        return {"installed": [str(d) for _s, d, _k, _m in changes], "backup": str(backup),
                "units_alteradas": any(k == "unit" for _s, _d, k, _m in changes)}
    finally:
        os.close(lock)


def _run_systemctl(systemctl: str, *args):
    return subprocess.run([systemctl, "--user", *args], capture_output=True, text=True,
                          timeout=SYSTEMCTL_TIMEOUT)


def enable_units(units_alteradas: bool):
    result = {"habilitadas": [], "ja_habilitadas": [], "falhas_enable": [], "avisos": []}
    systemctl = shutil.which("systemctl")
    if os.environ.get("WORKER_ASK_NO_SYSTEMCTL") == "1" or systemctl is None:
        if systemctl is None:
            result["avisos"].append("systemctl ausente")
        result["habilitadas"].extend(ENABLE)
        return result
    if units_alteradas:
        try:
            _run_systemctl(systemctl, "daemon-reload")
        except (OSError, subprocess.TimeoutExpired) as error:
            result["avisos"].append(f"daemon-reload falhou: {error}")
    for unit in ENABLE:
        try:
            probe = _run_systemctl(systemctl, "is-enabled", unit)
            enabled = probe.returncode == 0 and probe.stdout.strip() == "enabled"
        except (OSError, subprocess.TimeoutExpired):
            enabled = False
        if enabled:
            result["ja_habilitadas"].append(unit)
            continue
        try:
            action = _run_systemctl(systemctl, "enable", "--now", unit)
            if action.returncode == 0:
                result["habilitadas"].append(unit)
            else:
                result["falhas_enable"].append(unit)
        except (OSError, subprocess.TimeoutExpired):
            result["falhas_enable"].append(unit)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        try:
            result = check(args.home)
        except (OSError, RuntimeError) as error:
            print(json.dumps({"erro": str(error)}, ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["total"] else 0
    try:
        result = install(args.home)
    except (OSError, RuntimeError) as error:
        print(f"instalação recusada: {error}", file=sys.stderr)
        return 1
    if args.enable:
        result.update(enable_units(result["units_alteradas"]))
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("falhas_enable") else 0


if __name__ == "__main__":
    raise SystemExit(main())
