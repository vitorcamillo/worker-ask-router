import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

ENV_VAR = "WORKER_ASK_ZCODE_QUEUE_TIMEOUT"


@pytest.mark.parametrize("raw,expected", [
    (None, 600.0),
    ("120", 120.0),
    ("abc", 600.0),
    ("-5", 600.0),
    ("inf", 600.0),
    ("nan", 600.0),
    ("0", 600.0),
])
def test_zcode_queue_timeout_parses_env(raw, expected, monkeypatch):
    if raw is None:
        monkeypatch.delenv(ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(ENV_VAR, raw)
    assert wr._zcode_queue_timeout() == expected


def test_build_argv_zcode_worker_has_default_queue_timeout(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    argv = wr.build_argv("zcode-worker", "/bin/zcode-worker", "write", ".")
    assert argv[argv.index("--queue-timeout") + 1] == "600"


def test_build_argv_zcode_worker_queue_timeout_from_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "120")
    argv = wr.build_argv("zcode-worker", "/bin/zcode-worker", "plan", ".")
    assert argv[argv.index("--queue-timeout") + 1] == "120"


@pytest.mark.parametrize("raw", ["abc", "-5", "inf"])
def test_build_argv_zcode_worker_invalid_env_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv(ENV_VAR, raw)
    argv = wr.build_argv("zcode-worker", "/bin/zcode-worker", "write", ".")
    assert argv[argv.index("--queue-timeout") + 1] == "600"


@pytest.mark.parametrize("name", ["muse-ask", "opencode-ask", "codex-ask", "claude-ask", "codex-terra"])
def test_other_executors_do_not_receive_queue_timeout(monkeypatch, name):
    monkeypatch.delenv(ENV_VAR, raising=False)
    argv = wr.build_argv(name, "/bin/wrapper", "write", ".")
    assert "--queue-timeout" not in argv
