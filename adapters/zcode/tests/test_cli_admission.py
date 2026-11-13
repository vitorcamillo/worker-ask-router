"""Contagem de workers por ancestralidade e grupo; /proc inteiramente sintético."""
import builtins
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest
from zcode_worker import base

ROOT = Path(__file__).resolve().parents[3]
loader = importlib.machinery.SourceFileLoader("admission_core_fixture", str(ROOT / "bin/zcode-worker"))
spec = importlib.util.spec_from_loader(loader.name, loader)
core = importlib.util.module_from_spec(spec)
loader.exec_module(core)


@pytest.fixture(params=[base, core], ids=["package", "installed-core-source"])
def counter(request):
    return request.param


def process(root, pid, parent, group, *, renamed=False, comm="zcode-cli"):
    directory = root / str(pid)
    directory.mkdir()
    (directory / "comm").write_text(comm)
    argv = b"zcode-cli\0" if renamed else b"zcode-cli\0-p\0SYNTHETIC_PRIVATE_PROMPT\0"
    (directory / "cmdline").write_bytes(argv)
    (directory / "exe").symlink_to("/fixture/node")
    (directory / "stat").write_text(f"{pid} (zcode-cli) S {parent} {group} {group} 0 0")
    return directory


@pytest.mark.parametrize("renamed", [False, True])
def test_parent_plus_four_helpers_counts_one(counter, tmp_path, renamed, capsys):
    process(tmp_path, 100, 1, 100, renamed=renamed)
    for pid in range(101, 105):
        process(tmp_path, pid, 100, 100, renamed=renamed)
    assert counter.count_active_cli_processes(str(tmp_path)) == 1
    assert capsys.readouterr().out == ""


def test_independent_group_is_still_counted(counter, tmp_path):
    process(tmp_path, 100, 1, 100)
    process(tmp_path, 101, 100, 100)
    process(tmp_path, 200, 100, 200)
    process(tmp_path, 201, 200, 200)
    assert counter.count_active_cli_processes(str(tmp_path)) == 2


def test_unrelated_roots_same_group_are_not_merged(counter, tmp_path):
    process(tmp_path, 100, 1, 100)
    process(tmp_path, 200, 1, 100)
    assert counter.count_active_cli_processes(str(tmp_path)) == 2


def test_two_worker_families_count_two(counter, tmp_path):
    for parent in (100, 200):
        process(tmp_path, parent, 1, parent)
        for pid in range(parent + 1, parent + 5):
            process(tmp_path, pid, parent, parent)
    assert counter.count_active_cli_processes(str(tmp_path)) == 2


def test_gui_foreign_uid_and_nonnumeric_entries_are_ignored(counter, tmp_path):
    gui = process(tmp_path, 100, 1, 100)
    (gui / "cmdline").write_bytes(b"zcode-cli\0--version\0")
    process(tmp_path, 101, 1, 101, comm="other-app")
    (tmp_path / "not-a-pid").mkdir()
    assert counter.count_active_cli_processes(str(tmp_path)) == 0
    process(tmp_path, 200, 1, 200)
    assert counter.count_active_cli_processes(str(tmp_path), uid=os.getuid() + 1) == 0


@pytest.mark.parametrize("metadata", ["invalid", "100 (zcode-cli) S", "100 (zcode-cli) S invalid 100"])
def test_invalid_metadata_fails_closed(counter, tmp_path, metadata):
    node = process(tmp_path, 100, 1, 100)
    (node / "stat").write_text(metadata)
    with pytest.raises(counter.AdmissionError, match="inspecionar processos"):
        counter.count_active_cli_processes(str(tmp_path))


def test_exited_process_is_ignored(counter, tmp_path):
    node = process(tmp_path, 100, 1, 100)
    (node / "stat").unlink()
    assert counter.count_active_cli_processes(str(tmp_path)) == 0


def test_metadata_permission_error_is_static(counter, tmp_path, monkeypatch):
    node = process(tmp_path, 100, 1, 100)
    original = builtins.open
    def denied(path, *args, **kwargs):
        if Path(path) == node / "stat":
            raise PermissionError("SYNTHETIC_PRIVATE_DETAIL")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(counter.AdmissionError) as caught:
        counter.count_active_cli_processes(str(tmp_path))
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)


def test_relative_proc_root_fails_closed(counter):
    with pytest.raises(counter.AdmissionError, match="absoluto"):
        counter.count_active_cli_processes("relative")


def test_unreadable_proc_root_fails_closed(counter, tmp_path, monkeypatch):
    def denied(_path):
        raise PermissionError("synthetic-private-detail")
    with monkeypatch.context() as patch:
        patch.setattr(counter.os, "scandir", denied)
        with pytest.raises(counter.AdmissionError, match="inspecionar processos"):
            counter.count_active_cli_processes(str(tmp_path))


def test_process_lookup_race_is_ignored(counter, tmp_path, monkeypatch):
    node = process(tmp_path, 100, 1, 100)
    original = os.stat
    def vanished(path, *args, **kwargs):
        if Path(path) == node:
            raise ProcessLookupError()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(counter.os, "stat", vanished)
    assert counter.count_active_cli_processes(str(tmp_path)) == 0


def test_real_worker_count_still_reaches_limit(counter, tmp_path):
    for parent in (100, 200, 300, 400):
        process(tmp_path, parent, 1, parent)
        for pid in range(parent + 1, parent + 5):
            process(tmp_path, pid, parent, parent)
    assert counter.count_active_cli_processes(str(tmp_path)) == 4
    process(tmp_path, 500, 1, 500)
    assert counter.count_active_cli_processes(str(tmp_path)) == 5


def test_active_cli_pids_lists_the_counted_leaders(counter, tmp_path):
    # Mesma deduplicação da contagem: só os líderes, na ordem crescente.
    process(tmp_path, 100, 1, 100)
    for pid in range(101, 105):
        process(tmp_path, pid, 100, 100)
    process(tmp_path, 200, 1, 200)
    process(tmp_path, 201, 200, 200)
    pids = counter.active_cli_pids(str(tmp_path))
    assert pids == [100, 200]
    assert counter.count_active_cli_processes(str(tmp_path)) == len(pids)
