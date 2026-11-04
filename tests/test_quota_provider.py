"""Consulta de limites isolada: sem rede, credenciais ou geracao de modelo."""

import json
import time

import pytest
import quota_provider as p
from test_worker_router import fake_env as router_fake_env, run_cli

fake_env = router_fake_env


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def install_source(home, body):
    path = home / ".local/bin/ask-limites-json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def limites_glm():\n    " + body + "\n")
    return path


def test_consult_glm_without_model(home):
    reset = time.time() + 7 * 86400
    install_source(home, "return " + repr({"janelas": [{"pct": 100, "reset": reset}]}))
    assert p.query_reset("zcode-worker") == reset


def test_preflight_skips_blocked_before_task(fake_env):
    until = time.time() + 86400
    install_source(
        fake_env["tmp"], "return " + repr({"janelas": [{"pct": 100, "reset": until}]})
    )
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "task"], {"FAKE_LOG": str(log), "WORKER_ASK_NO_MUSE": "1"})
    assert result.returncode == 0
    assert [json.loads(s)["name"] for s in log.read_text().splitlines()] == [
        "opencode-ask"
    ]
    # Nem consulta de novo o endpoint antes do reset.
    install_source(fake_env["tmp"], 'raise RuntimeError("nao consultar de novo")')
    assert run_cli(fake_env, ["--mode", "plan", "task"], {"WORKER_ASK_NO_MUSE": "1"}).stdout == b"READY:opencode-ask"


@pytest.mark.parametrize(
    "windows,expected",
    [
        ([{"pct": 100, "reset": 2000}, {"pct": 100, "reset": 3000}], 3000),
        ([{"pct": 99, "reset": 2000}], None),
        ([{"pct": 100, "reset": 2000}, {"pct": 100, "reset": None}], None),
        ([{"pct": True, "reset": 2000}], None),
        ([{"pct": 100, "reset": float("nan")}], None),
        (None, None),
    ],
)
def test_multiple_windows(windows, expected):
    assert p._blocked_until(windows, 1000) == expected


def test_source_failure_no_raw_error(home, capsys):
    install_source(home, 'raise RuntimeError("synthetic-private")')
    assert p.query_reset("zcode-worker") is None
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_claude_cache_reset_and_staleness(home):
    now = time.time()
    reset = now + 86400
    file = home / ".local/state/ask-bridges/claude-limits.json"
    file.parent.mkdir(parents=True)
    data = {
        "at": now * 1000,
        "rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": reset}},
    }
    file.write_text(json.dumps(data))
    assert p.query_reset("claude-ask", now) == reset
    data["at"] = (now - 1900) * 1000
    file.write_text(json.dumps(data))
    assert p.query_reset("claude-ask", now) is None
    file.unlink()
    file.symlink_to("/dev/null")
    assert p.query_reset("claude-ask", now) is None


@pytest.mark.parametrize("name", ["codex-ask", "muse-ask", "opencode-ask", "unknown"])
def test_no_model_probe_for_unsupported_provider(home, name):
    install_source(home, 'raise AssertionError("nao executar")')
    assert p.query_reset(name) is None


def test_codex_uses_native_account_not_bridge(home, monkeypatch):
    reset = time.time() + 86400
    source = home / ".local/bin/ask-limites-json"
    source.parent.mkdir(parents=True)
    source.write_text(
        'CODEX_HOME="wrong-bridge"\ndef limites_gpt():\n'
        '    assert CODEX_HOME == "native-synthetic-home"\n'
        "    return " + repr({"janelas": [{"pct": 100, "reset": reset}]}) + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", "native-synthetic-home")
    assert p.query_reset("codex-ask") == reset
