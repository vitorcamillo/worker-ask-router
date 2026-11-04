import json
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from zcode_worker import base, jobs


@pytest.fixture
def job_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(base, "STATE", str(state))
    monkeypatch.setattr(base, "PROC_ROOT", "/proc")
    jobs.configure(base)
    return state


def child_entrypoint(tmp_path):
    script = tmp_path / "job_child.py"
    script.write_text(
        """
import sys
import time
import os
from pathlib import Path
from zcode_worker import jobs

def runner(options, task):
    if task == 'sleep':
        time.sleep(30)
    if task == 'timeout':
        time.sleep(30)
    if task == 'error':
        raise RuntimeError('sensitive callback detail')
    if task == 'rc124':
        return {'response': 'timeout'}, 124
    if task == 'rc143':
        return {'response': 'cancelled'}, 128 + 15
    return {'response': 'ok', 'taskLength': len(task)}, 0

if __name__ == '__main__':
    barrier = os.environ.get('ZCODE_TEST_CHILD_BARRIER')
    if barrier:
        deadline = time.monotonic() + 5
        while not Path(barrier).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    raise SystemExit(jobs.run_child(sys.argv[-1], runner))
""",
        encoding="utf-8",
    )
    return [str(Path(sys.executable).resolve()), str(script)]


def wait_for_status(job_id, wanted, timeout=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = jobs.read_status(job_id)
        if last["status"] in wanted:
            return last
        time.sleep(0.02)
    raise AssertionError(f"status não alcançado: {last}")


def test_start_status_result_and_private_input(job_state, tmp_path):
    task = "segredo do prompt que não deve ir para argv"
    output = tmp_path / "result.jsonl"
    started = jobs.start_job({}, task, str(output), child_entrypoint(tmp_path))

    assert len(started["id"]) == 32
    assert started["status"] in {"queued", "running", "completed"}
    assert started["pid"] > 0
    public = wait_for_status(started["id"], {"completed"})
    assert public["exitCode"] == 0
    assert set(public) == {"id", "status", "pid", "output", "exitCode"}
    contents = output.read_text(encoding="utf-8")
    assert json.loads(contents.splitlines()[0]) == {"response": "ok", "taskLength": len(task)}
    assert contents.rstrip().endswith("exit=0")
    assert task not in contents
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    job_dir = job_state / "jobs" / started["id"]
    assert not (job_dir / "input.json").exists()
    metadata = (job_dir / "metadata.json").read_text(encoding="utf-8")
    assert task not in metadata
    assert "response" not in metadata
    assert "options" not in metadata


def test_private_files_and_validation(job_state, tmp_path):
    output = tmp_path / "result.jsonl"
    symlink = tmp_path / "symlink"
    symlink.symlink_to(output)
    with pytest.raises(base.WorkerError):
        jobs.start_job({}, "task", str(symlink), child_entrypoint(tmp_path))
    with pytest.raises(base.WorkerError):
        jobs.start_job({"token": "must not be accepted"}, "task", str(output), child_entrypoint(tmp_path))
    with pytest.raises(base.WorkerError):
        jobs.start_job({}, "\x00", str(output), child_entrypoint(tmp_path))
    with pytest.raises(base.WorkerError):
        jobs.read_status("../not-a-job")

    started = jobs.start_job({}, "task", str(output), child_entrypoint(tmp_path))
    job_dir = job_state / "jobs" / started["id"]
    assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((job_dir / "metadata.json").stat().st_mode) == 0o600
    # O_EXCL evita que uma segunda chamada sobrescreva o resultado.
    with pytest.raises(base.WorkerError):
        jobs.start_job({}, "task", str(output), child_entrypoint(tmp_path))
    wait_for_status(started["id"], {"completed"})


def test_task_never_enters_process_argv(job_state, tmp_path):
    task = "UNIQUE_PRIVATE_TASK_12345"
    started = jobs.start_job({}, task, str(tmp_path / "result"), child_entrypoint(tmp_path))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            cmdline = Path(f"/proc/{started['pid']}/cmdline").read_bytes()
        except FileNotFoundError:
            break
        assert task.encode() not in cmdline
        if jobs.read_status(started["id"])["status"] == "completed":
            return
        time.sleep(0.01)
    wait_for_status(started["id"], {"completed"})


def test_cancel_sends_signal_and_removes_input(job_state, tmp_path):
    started = jobs.start_job({}, "sleep", str(tmp_path / "cancelled"), child_entrypoint(tmp_path))
    wait_for_status(started["id"], {"running"})
    requested = jobs.cancel_job(started["id"])
    assert requested["status"] == "running"
    assert requested["cancelRequestedAt"] > 0
    final = wait_for_status(started["id"], {"cancelled"})
    assert final["exitCode"] == 128 + signal.SIGTERM
    assert not (job_state / "jobs" / started["id"] / "input.json").exists()


def test_callback_failure_is_safe_and_timeout_is_terminal(job_state, tmp_path):
    failed = jobs.start_job({}, "error", str(tmp_path / "failed"), child_entrypoint(tmp_path))
    final_failed = wait_for_status(failed["id"], {"failed"})
    assert final_failed["exitCode"] == 1
    assert "sensitive callback detail" not in Path(final_failed["output"]).read_text(encoding="utf-8")
    assert "falha segura" in Path(final_failed["output"]).read_text(encoding="utf-8")

    timed = jobs.start_job({"timeout": 0.05}, "timeout", str(tmp_path / "timed"), child_entrypoint(tmp_path))
    final_timed = wait_for_status(timed["id"], {"timed_out"})
    assert final_timed["exitCode"] == 124


def test_external_death_becomes_lost_and_does_not_signal_reused_pid(job_state, tmp_path):
    started = jobs.start_job({}, "sleep", str(tmp_path / "lost"), child_entrypoint(tmp_path))
    wait_for_status(started["id"], {"running"})
    os.killpg(started["pid"], signal.SIGKILL)
    final = wait_for_status(started["id"], {"lost"})
    assert final["exitCode"] == 1
    assert not (job_state / "jobs" / started["id"] / "input.json").exists()


def test_output_identity_blocks_forged_victim_and_keeps_victim_bytes(job_state, tmp_path, monkeypatch):
    barrier = tmp_path / "release-child"
    monkeypatch.setenv("ZCODE_TEST_CHILD_BARRIER", str(barrier))
    output = tmp_path / "result"
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim-original")
    victim.chmod(0o600)
    started = jobs.start_job({}, "task", str(output), child_entrypoint(tmp_path))
    job_dir = job_state / "jobs" / started["id"]
    with jobs._job_lock(started["id"]) as directory:
        metadata = jobs._load_metadata(directory, started["id"])
        victim_stat = victim.stat()
        metadata.update({
            "output": str(victim),
            "outputDev": victim_stat.st_dev,
            "outputIno": victim_stat.st_ino,
        })
        jobs._atomic_write(jobs._metadata_path(directory), metadata, limit=64 * 1024)
    barrier.touch()
    final = wait_for_status(started["id"], {"failed"})
    assert final["exitCode"] == 1
    assert victim.read_bytes() == b"victim-original"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600
    assert not (job_dir / "input.json").exists()


def test_replaced_output_is_rejected_without_touching_victim(job_state, tmp_path, monkeypatch):
    barrier = tmp_path / "release-child"
    monkeypatch.setenv("ZCODE_TEST_CHILD_BARRIER", str(barrier))
    output = tmp_path / "result"
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim-original")
    victim.chmod(0o600)
    started = jobs.start_job({}, "task", str(output), child_entrypoint(tmp_path))
    with jobs._job_lock(started["id"]) as directory:
        metadata = jobs._load_metadata(directory, started["id"])
    output.unlink()
    os.link(victim, output)
    barrier.touch()
    final = wait_for_status(started["id"], {"failed"})
    assert final["exitCode"] == 1
    assert victim.read_bytes() == b"victim-original"
    assert output.read_bytes() == b"victim-original"
    with pytest.raises(base.WorkerError):
        jobs._write_result(
            str(output), {"response": "must-not-write"}, 0, jobs._PERSISTED_OUTPUT_LIMIT,
            (metadata["outputDev"], metadata["outputIno"]),
        )


def test_private_fd_failures_close_and_cleanup_created_files(job_state, tmp_path, monkeypatch):
    private = tmp_path / "private"
    before = set(os.listdir("/proc/self/fd"))
    real_fstat = jobs.os.fstat

    def fail_fstat(_fd):
        raise OSError("injected fstat failure")

    monkeypatch.setattr(jobs.os, "fstat", fail_fstat)
    with pytest.raises(base.WorkerError):
        jobs._private_file(str(private), create=True)
    assert set(os.listdir("/proc/self/fd")) == before
    monkeypatch.setattr(jobs.os, "fstat", real_fstat)
    assert not private.exists()

    private_fchmod = tmp_path / "private-fchmod"
    real_fchmod = jobs.os.fchmod
    monkeypatch.setattr(jobs.os, "fchmod", lambda _fd, _mode: (_ for _ in ()).throw(OSError("injected fchmod failure")))
    with pytest.raises(base.WorkerError):
        jobs._private_file(str(private_fchmod), create=True)
    assert not private_fchmod.exists()
    monkeypatch.setattr(jobs.os, "fchmod", real_fchmod)

    output = tmp_path / "output"

    def fail_fchmod(_fd, _mode):
        raise OSError("injected fchmod failure")

    monkeypatch.setattr(jobs.os, "fchmod", fail_fchmod)
    with pytest.raises(base.WorkerError):
        jobs._create_output(str(output))
    assert not output.exists()
    assert set(os.listdir("/proc/self/fd")) == before
    monkeypatch.setattr(jobs.os, "fchmod", real_fchmod)


def test_cancel_pidfd_revalidation_fails_closed_and_closes_fd(job_state, tmp_path, monkeypatch):
    started = jobs.start_job({}, "sleep", str(tmp_path / "cancel"), child_entrypoint(tmp_path))
    wait_for_status(started["id"], {"running"})
    real_open = os.open
    real_pidfd_open = jobs.os.pidfd_open
    real_pidfd_send_signal = jobs.signal.pidfd_send_signal
    real_proc_has_identity = jobs._proc_has_identity
    pidfd_holder = {}
    checks = iter((True, False))

    def fake_pidfd_open(_pid, _flags):
        fd = real_open("/dev/null", os.O_RDONLY)
        pidfd_holder["fd"] = fd
        return fd

    monkeypatch.setattr(jobs.os, "pidfd_open", fake_pidfd_open)
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", lambda *_args: pytest.fail("sinal indevido"))
    monkeypatch.setattr(jobs, "_proc_has_identity", lambda *_args: next(checks))
    requested = jobs.cancel_job(started["id"])
    assert requested["status"] == "running"
    with pytest.raises(OSError):
        os.fstat(pidfd_holder["fd"])
    monkeypatch.setattr(jobs.os, "pidfd_open", real_pidfd_open)
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", real_pidfd_send_signal)
    monkeypatch.setattr(jobs, "_proc_has_identity", real_proc_has_identity)
    try:
        jobs.cancel_job(started["id"])
        wait_for_status(started["id"], {"cancelled", "lost"})
    finally:
        if jobs.read_status(started["id"])["status"] not in {"cancelled", "lost"}:
            os.killpg(started["pid"], signal.SIGKILL)


def test_early_queued_cancel_writes_correct_footer_and_cleans_input(job_state, tmp_path, monkeypatch):
    barrier = tmp_path / "release-child"
    monkeypatch.setenv("ZCODE_TEST_CHILD_BARRIER", str(barrier))
    output = tmp_path / "queued-cancel"
    started = jobs.start_job({}, "task", str(output), child_entrypoint(tmp_path))
    requested = jobs.cancel_job(started["id"])
    assert requested["status"] == "queued"
    barrier.touch()
    final = wait_for_status(started["id"], {"cancelled"})
    assert final["exitCode"] == 128 + signal.SIGTERM
    assert output.read_bytes().rstrip().endswith(f"exit={128 + signal.SIGTERM}".encode())
    assert not (job_state / "jobs" / started["id"] / "input.json").exists()


def test_callback_exit_codes_and_worker_thread_timeout_guard(job_state, tmp_path):
    timed = jobs.start_job({}, "rc124", str(tmp_path / "rc124"), child_entrypoint(tmp_path))
    assert wait_for_status(timed["id"], {"timed_out"})["exitCode"] == 124
    cancelled = jobs.start_job({}, "rc143", str(tmp_path / "rc143"), child_entrypoint(tmp_path))
    assert wait_for_status(cancelled["id"], {"cancelled"})["exitCode"] == 128 + signal.SIGTERM

    result = []
    errors = []

    def invoke():
        try:
            result.append(jobs._run_callback(lambda _options, _task: ({"ok": True}, 0), {"total_timeout": 0.1}, "task"))
        except BaseException as error:  # noqa: BLE001 - o teste verifica ausência de ValueError
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert result == [({"ok": True}, 0)]
