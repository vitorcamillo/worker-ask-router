import os
import signal
import sqlite3
import sys
import time
from argparse import Namespace

import pytest

from zcode_worker import cli, features


def _args(**overrides):
    values = dict(
        tarefa=None,
        cwd=os.getcwd(),
        mode="edit",
        spec=None,
        timeout=1.0,
        queue_timeout=1.0,
        total_timeout=None,
        model="glm-5.3-flash",
        effort="high",
        profile=None,
        resume=None,
        attach=[],
        disallowed_tools=[],
        max_output_bytes=None,
        json=False,
        dry_run=False,
        doctor=False,
        list_models=False,
        saida=None,
        status=None,
        cancel=None,
        _job_child=None,
    )
    values.update(overrides)
    return Namespace(**values)


def _pid_gone(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as source:
            fields = source.read().split()
        return len(fields) < 3 or fields[2] == "Z"
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def _wait_pid_gone(pid, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_gone(pid):
            return
        time.sleep(0.02)
    assert _pid_gone(pid), f"descendente ainda ativo: {pid}"


def _cleanup_pid(pid):
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return
    _wait_pid_gone(pid)


def test_limited_reader_cleans_descendant_after_leader_exits(tmp_path):
    child_pid_file = tmp_path / "child-no-pipes.pid"
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"open({str(child_pid_file)!r},'w').write(str(child.pid))"
    )
    child_pid = None
    try:
        result = cli._invoke_limited(
            [sys.executable, "-c", code],
            dict(os.environ),
            time.monotonic() + 2.0,
            1024,
        )
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        assert result[0] == 0
        _wait_pid_gone(child_pid)
    finally:
        _cleanup_pid(child_pid)


def test_limited_reader_cleans_descendant_holding_pipe_until_deadline(tmp_path):
    child_pid_file = tmp_path / "child-pipe.pid"
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"open({str(child_pid_file)!r},'w').write(str(child.pid)); "
        "time.sleep(30)"
    )
    child_pid = None
    try:
        with pytest.raises(features.FeatureError, match="prazo") as caught:
            cli._invoke_limited(
                [sys.executable, "-c", code],
                dict(os.environ),
                time.monotonic() + 0.35,
                1024,
            )
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        assert caught.value.code == "timeout"
        _wait_pid_gone(child_pid)
    finally:
        _cleanup_pid(child_pid)


def test_limited_reader_times_out_without_blocking_and_kills_descendant(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"open({str(child_pid_file)!r},'w').write(str(child.pid)); "
        "print('x', flush=True); time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(features.FeatureError, match="prazo") as caught:
        cli._invoke_limited(
            [sys.executable, "-c", code],
            dict(os.environ),
            time.monotonic() + 0.35,
            1024,
        )
    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 2.0
    deadline = time.monotonic() + 1.0
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    _wait_pid_gone(child_pid)


def test_limited_reader_enforces_output_cap_with_real_process():
    code = "import os,time; os.write(1,b'x'*200000); time.sleep(30)"
    with pytest.raises(cli.OutputLimitExceeded):
        cli._invoke_limited(
            [sys.executable, "-c", code],
            dict(os.environ),
            time.monotonic() + 2.0,
            4096,
        )


def test_run_task_maps_term_signal_and_restores_handlers(tmp_path, monkeypatch):
    args = _args(cwd=str(tmp_path))
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}

    def interrupted(*_args, **_kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("o handler deveria interromper a execução")

    monkeypatch.setattr(cli, "_execute", interrupted)
    result, rc = cli.run_task(args, "tarefa")
    assert result["status"] == "cancelled"
    assert result["error"]["code"] == "terminated"
    assert rc == 128 + signal.SIGTERM
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_run_task_maps_admission_deadline_without_starting_cli(tmp_path, monkeypatch):
    args = _args(cwd=str(tmp_path))
    called = False

    class BlockedSlot:
        def __enter__(self):
            raise cli.base.DeadlineExpired("fila cheia")

        def __exit__(self, *_exc):
            return False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("CLI não deveria iniciar quando a admissão expirou")

    monkeypatch.setattr(cli.base, "global_slot", lambda _deadline: BlockedSlot())
    monkeypatch.setattr(cli, "_invoke", forbidden)
    result, rc = cli.run_task(args, "tarefa bloqueada")
    assert rc == 124
    assert result["error"] == {"code": "timeout", "message": "prazo de execução esgotado"}
    assert called is False


def test_session_cwd_requires_table_and_supports_directory_column(tmp_path):
    missing = tmp_path / "missing.sqlite"
    assert not cli._session_cwd_matches("sess_x", str(tmp_path), missing)

    db = tmp_path / "session.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("create table session(id text, directory text)")
    connection.execute("insert into session values (?, ?)", ("sess_x", str(tmp_path)))
    connection.commit()
    connection.close()
    assert cli._session_cwd_matches("sess_x", str(tmp_path), db)
    assert not cli._session_cwd_matches("sess_x", str(tmp_path / "other"), db)


def test_read_task_limits_bytes_before_utf8_decode(tmp_path):
    spec = tmp_path / "large.txt"
    spec.write_bytes(b"a" * (features.TASK_MAX_BYTES + 1))
    with pytest.raises(features.FeatureError):
        features.read_task(_args(spec=str(spec)))
    with pytest.raises(features.FeatureError):
        features.read_task(_args(), stdin_text=b"a" * (features.TASK_MAX_BYTES + 1))
    with pytest.raises(features.FeatureError):
        features.read_task(_args(), stdin_text=b"\xff")


def test_feature_guards_reject_unknown_profile_huge_number_and_tmp_symlink(tmp_path):
    with pytest.raises(features.FeatureError, match="perfil"):
        features.selection_for("perfil-inexistente", None, None)
    assert features._number(10**10000) is False

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (state / "tmp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(features.FeatureError, match="privado"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state):
            pass
    assert list(outside.iterdir()) == []


def test_provider_config_failure_does_not_leave_descriptor_open(tmp_path, monkeypatch):
    missing = tmp_path / "missing-provider.json"
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(missing))
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(features.FeatureError, match="provider"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=tmp_path / "state"):
            pass
    after = set(os.listdir("/proc/self/fd"))
    assert after - before == set()


def test_response_usage_projection_and_default_output_cap():
    payload = features.validate_response(
        {
            "sessionId": "sess_valid",
            "response": "ok",
            "usage": {
                "cacheReadTokens": 2,
                "cacheWriteTokens": 3,
                "inputTokens": 4,
            },
            "projection": {"status": "completed"},
        }
    )
    assert payload["usage"]["cacheReadTokens"] == 2
    assert payload["usage"]["cacheWriteTokens"] == 3
    for value in (True, float("nan"), float("inf")):
        with pytest.raises(features.FeatureError):
            features.validate_response(
                {"sessionId": "sess_valid", "response": "ok", "usage": {"inputTokens": value}}
            )
    with pytest.raises(features.FeatureError):
        features.validate_response({"sessionId": "sess_valid", "response": "ok", "projection": None})
    assert features.public_namespace(_args()).max_output_bytes == features.DEFAULT_MAX_OUTPUT_BYTES


def test_effective_selection_rejects_missing_db_and_ignores_claimed_payload(tmp_path):
    expected = features.requested_selection("glm-5.3-flash", "high")
    with pytest.raises(features.FeatureError):
        features.verify_effective(
            {"sessionId": "sess_missing", "effective": expected},
            "glm-5.3-flash",
            "high",
            db_path=tmp_path / "missing.sqlite",
        )


def test_cli_argv_uses_one_variadic_denylist_flag(tmp_path):
    attachment = tmp_path / "note.txt"
    attachment.write_text("ok", encoding="utf-8")
    argv = features.build_cli_argv(
        "task",
        str(tmp_path),
        "edit",
        "glm-5.3-flash",
        "high",
        attachments=[str(attachment)],
        disallowed_tools=["ToolA", "ToolA", "ToolB"],
    )
    index = argv.index("--disallowed-tools")
    assert argv.count("--disallowed-tools") == 1
    assert argv[index + 1 :].count("ToolA") == 1
    assert argv[index + 1 :].count("ToolB") == 1
