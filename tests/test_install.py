"""Instalação em HOME temporária: sem CLIs, serviços ou credenciais reais."""
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("suite_install", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_install_all_wrappers_core_regular_and_idempotence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    key = home / ".config/muse/auth.json"
    key.parent.mkdir(parents=True)
    key.write_text("synthetic-private-state")
    original = Path.read_bytes

    def protected_read(path):
        assert path != key, "instalador não pode ler credenciais"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", protected_read)
    result = installer.install(home)
    for name in (*installer.WRAPPERS, *installer.AUXILIARIES, "worker-ask"):
        assert (home / ".local/bin" / name).is_symlink()
    core = home / ".local/bin/zcode-worker"
    assert not core.is_symlink()
    assert core.read_bytes() == (ROOT / "bin/zcode-worker").read_bytes()
    assert stat.S_IMODE(core.stat().st_mode) == 0o755
    assert (home / ".local/share/bonsai/scripts/gpu_coord.py").is_symlink()
    assert key.read_text() == "synthetic-private-state"
    assert result["installed"]
    assert installer.install(home) == {"installed": [], "backup": None, "units_alteradas": False}
    assert len(list((home / ".local/state/worker-ask/install-backups").iterdir())) == 1


def test_preflight_rejects_directory_before_any_change(tmp_path):
    binary = tmp_path / ".local/bin"
    binary.mkdir(parents=True)
    old = binary / "worker-ask"
    old.write_text("old-worker")
    (binary / "codex-ask").mkdir()
    with pytest.raises(RuntimeError, match="destino"):
        installer.install(tmp_path)
    assert old.read_text() == "old-worker"
    assert not (tmp_path / ".local/state").exists()


def test_preflight_rejects_parent_symlink_and_missing_source(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="simbólico"):
        installer.install(home)
    assert not list(outside.iterdir())
    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    with pytest.raises(RuntimeError, match="fonte"):
        installer.install(tmp_path / "other-home", empty_repo)


def test_backup_preserves_dangling_symlink_without_reading_target(tmp_path):
    binary = tmp_path / ".local/bin"
    binary.mkdir(parents=True)
    original = binary / "worker-ask"
    original.symlink_to("missing-private-target")
    result = installer.install(tmp_path)
    saved = Path(result["backup"]) / ".local/bin/worker-ask"
    assert saved.is_symlink() and os.readlink(saved) == "missing-private-target"
    assert original.resolve() == ROOT / "worker_router.py"


def test_install_failure_restores_previous_files(tmp_path, monkeypatch):
    binary = tmp_path / ".local/bin"
    binary.mkdir(parents=True)
    for name in ("worker-ask", "codex-ask"):
        (binary / name).write_text("old-" + name)
    replace = os.replace
    failed = False

    def fail_once(source, dest):
        nonlocal failed
        if not failed and Path(dest).name == "codex-ask" and ".install-" in Path(source).name:
            assert Path(dest).read_text() == "old-codex-ask", "destino deve continuar presente até troca atômica"
            failed = True
            raise OSError("synthetic-install-failure")
        return replace(source, dest)

    monkeypatch.setattr(installer.os, "replace", fail_once)
    with pytest.raises(RuntimeError, match="revertidas"):
        installer.install(tmp_path)
    assert (binary / "worker-ask").read_text() == "old-worker-ask"
    assert (binary / "codex-ask").read_text() == "old-codex-ask"
    assert not list(binary.glob(".*.install-*"))


def test_installer_lock_refuses_concurrent_run(tmp_path):
    state = tmp_path / ".local/state/worker-ask"
    state.mkdir(parents=True)
    with (state / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="andamento"):
            installer.install(tmp_path)
    assert not (tmp_path / ".local/bin").exists()


def test_fifo_destination_refused(tmp_path):
    binary = tmp_path / ".local/bin"
    binary.mkdir(parents=True)
    os.mkfifo(binary / "worker-ask")
    with pytest.raises(RuntimeError, match="destino"):
        installer.install(tmp_path)


def test_cli_staged_home_and_error(tmp_path):
    home = tmp_path / "home"
    p = subprocess.run([sys.executable, str(ROOT / "scripts/install.py"), "--home", str(home)], capture_output=True, text=True, timeout=10)
    assert p.returncode == 0 and '"installed"' in p.stdout
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    assert installer.main(["--home", str(blocker)]) == 1


def test_partial_source_tree_is_rejected_before_install(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    for source, _dest, _kind, _mode in installer.entries(repo, home):
        if source.name != "features.py":
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("fixture-code")
    with pytest.raises(RuntimeError, match="fonte"):
        installer.install(home, repo)
    assert not home.exists()


def test_fifo_lock_does_not_block(tmp_path):
    state = tmp_path / ".local/state/worker-ask"
    state.mkdir(parents=True)
    os.mkfifo(state / "install.lock")
    with pytest.raises(OSError):
        installer.install(tmp_path)


def test_hardlinked_destination_is_not_read_or_replaced(tmp_path, monkeypatch):
    key = tmp_path / "synthetic-private-key"
    key.write_text("synthetic-private-data")
    binary = tmp_path / ".local/bin"
    binary.mkdir(parents=True)
    os.link(key, binary / "zcode-worker")
    def no_reads(_path):
        raise AssertionError("preflight não pode ler o destino compartilhado")
    monkeypatch.setattr(Path, "read_bytes", no_reads)
    with pytest.raises(RuntimeError, match="hardlink"):
        installer.install(tmp_path)
    assert key.read_text() == "synthetic-private-data"


def test_failed_fresh_install_removes_new_links(tmp_path, monkeypatch):
    replace = os.replace
    def fail_new(source, dest):
        if Path(dest).name == "codex-ask" and ".install-" in Path(source).name:
            raise OSError("synthetic-failure")
        return replace(source, dest)
    monkeypatch.setattr(installer.os, "replace", fail_new)
    with pytest.raises(RuntimeError, match="revertidas"):
        installer.install(tmp_path)
    assert not (tmp_path / ".local/bin/worker-ask").exists()
    assert not (tmp_path / ".local/bin/codex-ask").exists()


def test_failed_restore_keeps_backup_and_reports_incomplete(tmp_path, monkeypatch):
    binary = tmp_path / ".local/bin"
    binary.mkdir(parents=True)
    for name in ("worker-ask", "codex-ask"):
        (binary / name).write_text("old-" + name)
    replace = os.replace
    def fail_install_and_one_restore(source, dest):
        source, dest = Path(source), Path(dest)
        if dest.name == "codex-ask" and ".install-" in source.name:
            raise OSError("synthetic-install-failure")
        if dest.name == "worker-ask" and "install-backups" in source.parts:
            raise OSError("synthetic-restore-failure")
        return replace(source, dest)
    monkeypatch.setattr(installer.os, "replace", fail_install_and_one_restore)
    with pytest.raises(RuntimeError, match="restauração incompleta"):
        installer.install(tmp_path)
    backups = list((tmp_path / ".local/state/worker-ask/install-backups").glob("*/.local/bin/worker-ask"))
    assert len(backups) == 1 and backups[0].read_text() == "old-worker-ask"
    assert (binary / "codex-ask").read_text() == "old-codex-ask"


def test_restore_cross_device_via_dest_temp_preserves_backup(tmp_path, monkeypatch):
    binary = tmp_path / ".local/bin"
    binary.mkdir(parents=True)
    for name in ("worker-ask", "codex-ask"):
        (binary / name).write_text("old-" + name)
    replace = os.replace

    def cross_device(source, dest):
        source, dest = Path(source), Path(dest)
        if dest.name == "codex-ask" and ".install-" in source.name:
            raise OSError("synthetic-install-failure")
        if "install-backups" in source.parts:
            raise OSError(errno.EXDEV, "cross-device link")
        return replace(source, dest)

    monkeypatch.setattr(installer.os, "replace", cross_device)
    with pytest.raises(RuntimeError, match="revertidas"):
        installer.install(tmp_path)
    assert (binary / "worker-ask").read_text() == "old-worker-ask"
    assert (binary / "codex-ask").read_text() == "old-codex-ask"
    backups = list((tmp_path / ".local/state/worker-ask/install-backups").glob("*/.local/bin/worker-ask"))
    assert len(backups) == 1 and backups[0].read_text() == "old-worker-ask"
    assert not list(binary.glob(".*.restore-*"))


def test_check_reports_missing_without_writing(tmp_path):
    home = tmp_path / "home"
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts/install.py"), "--check", "--home", str(home)],
        capture_output=True, text=True, timeout=10,
    )
    data = json.loads(p.stdout)
    assert p.returncode == 1
    assert data["total"] == len(installer.entries(ROOT, home.absolute()))
    assert {item["estado"] for item in data["pendentes"]} == {"faltando"}
    assert not home.exists()


def test_check_is_clean_after_install(tmp_path):
    home = tmp_path / "home"
    installer.install(home)
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts/install.py"), "--check", "--home", str(home)],
        capture_output=True, text=True, timeout=10,
    )
    assert p.returncode == 0
    assert json.loads(p.stdout) == {"pendentes": [], "total": 0}


def test_check_reports_wrong_copy_mode(tmp_path):
    home = tmp_path / "home"
    installer.install(home)
    dest = home / ".local/bin/ask_bridge_process.py"
    dest.chmod(0o755)
    result = installer.check(home)
    assert result["total"] == 1
    pending = result["pendentes"][0]
    assert pending["estado"] == "modo"
    assert pending["tipo"] == "copy"
    assert pending["destino"] == str(dest)


def test_check_missing_source_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="fonte"):
        installer.check(tmp_path / "home", tmp_path / "empty-repo")


def test_install_copies_bridges_with_modes(tmp_path):
    home = tmp_path / "home"
    installer.install(home)
    for name in ("ask-claude-bridge.py", "ask-gpt-bridge.py", "ask-glm-bridge.py", "ask-bridges-smoke"):
        path = home / ".local/bin" / name
        assert path.is_file() and not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    for name in ("ask_bridge_anthropic.py", "ask_bridge_process.py",
                 "ask_bridge_stream.py", "ask_bridge_zcode_live.py"):
        path = home / ".local/bin" / name
        assert path.is_file() and not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_unit_renders_node_bin_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_BIN_DIR", "/opt/node/bin")
    home = tmp_path / "home"
    installer.install(home)
    unit = home / ".config/systemd/user/ask-check.service"
    content = unit.read_text()
    assert "/opt/node/bin" in content
    assert "@NODE_BIN_DIR@" not in content
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644


def test_unit_falls_back_to_home_local_bin_without_node(tmp_path, monkeypatch):
    monkeypatch.delenv("NODE_BIN_DIR", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)
    home = tmp_path / "home"
    installer.install(home)
    content = (home / ".config/systemd/user/worker-ask-disponibilidade.service").read_text()
    assert "@NODE_BIN_DIR@" not in content
    assert "%h/.local/bin:%h/.local/bin:" in content


def test_second_install_is_idempotent_and_units_unchanged(tmp_path):
    home = tmp_path / "home"
    first = installer.install(home)
    assert first["units_alteradas"] is True
    second = installer.install(home)
    assert second == {"installed": [], "backup": None, "units_alteradas": False}


def test_enable_simulated_lists_all_units(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_NO_SYSTEMCTL", "1")
    home = tmp_path / "home"
    code = installer.main(["--home", str(home), "--enable"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["habilitadas"] == list(installer.ENABLE)
    assert len(output["habilitadas"]) == 7
    assert output["falhas_enable"] == []


def test_enable_without_systemctl_adds_warning(tmp_path, monkeypatch, capsys):
    original_which = installer.shutil.which
    monkeypatch.delenv("WORKER_ASK_NO_SYSTEMCTL", raising=False)
    monkeypatch.setattr(installer.shutil, "which",
                        lambda name: None if name == "systemctl" else original_which(name))
    home = tmp_path / "home"
    code = installer.main(["--home", str(home), "--enable"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["avisos"] == ["systemctl ausente"]
    assert output["habilitadas"] == list(installer.ENABLE)


def test_enable_with_fake_systemctl(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    log = tmp_path / "systemctl.log"
    script = fake / "systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [ "$1" = "--user" ] && [ "$2" = "is-enabled" ]; then\n'
        '  if [ "$3" = "ask-check.timer" ]; then echo enabled; exit 0; fi\n'
        '  exit 1\n'
        'fi\n'
        'exit 0\n'
    )
    script.chmod(0o755)
    monkeypatch.delenv("WORKER_ASK_NO_SYSTEMCTL", raising=False)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")
    home = tmp_path / "home"
    assert installer.main(["--home", str(home), "--enable"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ja_habilitadas"] == ["ask-check.timer"]
    assert "ask-check.timer" not in output["habilitadas"]
    assert len(output["habilitadas"]) == len(installer.ENABLE) - 1
    calls = log.read_text().splitlines()
    assert sum(line == "--user daemon-reload" for line in calls) == 1
    assert not any("enable --now ask-check.timer" in line for line in calls)
    assert sum("enable --now" in line for line in calls) == len(installer.ENABLE) - 1
    for unit in installer.ENABLE:
        if unit == "ask-check.timer":
            continue
        assert any(f"enable --now {unit}" in line for line in calls)
    capsys.readouterr()
    assert installer.main(["--home", str(home), "--enable"]) == 0
    calls = log.read_text().splitlines()
    assert sum(line == "--user daemon-reload" for line in calls) == 1


def test_unit_uses_newest_nvm_node_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.delenv("NODE_BIN_DIR", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)
    home = tmp_path / "home"
    for versao in ("v9.11.2", "v22.22.2", "v18.20.0"):
        node = home / ".nvm/versions/node" / versao / "bin/node"
        node.parent.mkdir(parents=True)
        node.write_text("")
    installer.install(home)
    content = (home / ".config/systemd/user/worker-ask-disponibilidade.service").read_text()
    assert "%h/.local/bin:%h/.nvm/versions/node/v22.22.2/bin:" in content
