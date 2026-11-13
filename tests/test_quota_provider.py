"""Consulta de limites isolada: sem rede, credenciais ou geracao de modelo."""

import datetime
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
    monkeypatch.delenv("ZCODE_WORKER_CONTEXT_STATE", raising=False)
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN", raising=False)
    return tmp_path


def install_source(home, body):
    path = home / ".local/bin/ask-limites-json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def limites_glm():\n    " + body + "\n")
    return path


def block_start(home, until_epoch, reason="quota"):
    d = home / ".local/state/zcode-worker"
    d.mkdir(parents=True, exist_ok=True)
    (d / "start-plan-unavailable").write_text(
        datetime.datetime.fromtimestamp(until_epoch).isoformat(timespec="seconds")
        + " "
        + reason
    )


def test_consult_glm_without_model(home):
    now = time.time()
    reset = now + 7 * 86400
    install_source(home, "return " + repr({"janelas": [{"pct": 100, "reset": reset}]}))
    block_start(home, now + 10 * 86400)
    assert p.query_reset("zcode-worker") == pytest.approx(reset, abs=1.0)


def test_preflight_skips_blocked_before_task(fake_env, monkeypatch):
    monkeypatch.delenv("ZCODE_WORKER_CONTEXT_STATE", raising=False)
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN", raising=False)
    until = time.time() + 86400
    install_source(
        fake_env["tmp"], "return " + repr({"janelas": [{"pct": 100, "reset": until}]})
    )
    block_start(fake_env["tmp"], time.time() + 2 * 86400)
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "task"], {"FAKE_LOG": str(log), "WORKER_ASK_NO_MUSE": "1"})
    assert result.returncode == 0
    assert [json.loads(s)["name"] for s in log.read_text().splitlines()] == [
        "opencode-ask"
    ]
    # Nem consulta de novo o endpoint antes do reset.
    install_source(fake_env["tmp"], 'raise RuntimeError("nao consultar de novo")')
    assert run_cli(fake_env, ["--mode", "plan", "task"], {"WORKER_ASK_NO_MUSE": "1"}).stdout == b"READY:opencode-ask"


def test_start_free_skips_paid(home, monkeypatch):
    install_source(home, 'raise RuntimeError("limites_glm nao deve ser chamado")')

    def _fail(*args, **kwargs):
        raise RuntimeError("pago consultado")

    monkeypatch.setattr(p, "_query_plan_reset", _fail)
    assert p.query_reset("zcode-worker") is None


def test_start_marker_expired(home):
    now = time.time()
    install_source(
        home, "return " + repr({"janelas": [{"pct": 100, "reset": now + 7 * 86400}]})
    )
    block_start(home, now - 60)
    assert p.query_reset("zcode-worker") is None


def test_start_blocked_limits_reset(home):
    now = time.time()
    paid = now + 7 * 86400
    install_source(home, "return " + repr({"janelas": [{"pct": 100, "reset": paid}]}))
    block_start(home, now + 1800)
    marker = (
        (home / ".local/state/zcode-worker/start-plan-unavailable")
        .read_text()
        .split()[0]
    )
    expected = datetime.datetime.fromisoformat(marker).timestamp()
    assert p.query_reset("zcode-worker") == pytest.approx(expected, abs=1.0)


def test_start_disabled_uses_paid(home, monkeypatch):
    reset = time.time() + 7 * 86400
    install_source(home, "return " + repr({"janelas": [{"pct": 100, "reset": reset}]}))
    monkeypatch.setenv("ZCODE_WORKER_START_PLAN", "0")
    assert p.query_reset("zcode-worker") == reset


def test_start_marker_unreadable(home):
    now = time.time()
    install_source(
        home, "return " + repr({"janelas": [{"pct": 100, "reset": now + 7 * 86400}]})
    )
    d = home / ".local/state/zcode-worker"
    d.mkdir(parents=True, exist_ok=True)
    (d / "start-plan-unavailable").write_text("lixo")
    assert p.query_reset("zcode-worker") is None


def test_preflight_runs_zcode_when_start_free(fake_env, monkeypatch):
    monkeypatch.delenv("ZCODE_WORKER_CONTEXT_STATE", raising=False)
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN", raising=False)
    until = time.time() + 86400
    install_source(
        fake_env["tmp"], "return " + repr({"janelas": [{"pct": 100, "reset": until}]})
    )
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "task"], {"FAKE_LOG": str(log), "WORKER_ASK_NO_MUSE": "1"})
    assert result.returncode == 0
    names = [json.loads(s)["name"] for s in log.read_text().splitlines()]
    assert names and names[0] == "zcode-worker"


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


def _stale_zcode_entry(**extra):
    import worker_router as w

    entry = {"wrapper": "zcode-worker", "until": time.time() + 86400, "reset_known": True, **extra}
    w.save_cache({"zcode-worker": entry})
    return w


def _first_called(fake_env):
    log = fake_env["tmp"] / "calls"
    result = run_cli(fake_env, ["--mode", "plan", "task"], {"FAKE_LOG": str(log), "WORKER_ASK_NO_MUSE": "1"})
    assert result.returncode == 0
    return [json.loads(s)["name"] for s in log.read_text().splitlines()][0]


@pytest.mark.parametrize("start_blocked,extra,expected", [
    (False, {}, "zcode-worker"),  # cooldown de cota antigo + Start livre: roda o zcode
    (True, {}, "opencode-ask"),  # Start também bloqueado: respeita o cooldown
    (False, {"blocked_reason": "timeout"}, "opencode-ask"),  # timeout não é cota
])
def test_cached_zcode_cooldown_vs_start_plan(fake_env, monkeypatch, start_blocked, extra, expected):
    monkeypatch.delenv("ZCODE_WORKER_CONTEXT_STATE", raising=False)
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN", raising=False)
    if start_blocked:
        block_start(fake_env["tmp"], time.time() + 3600)
    w = _stale_zcode_entry(**extra)
    assert _first_called(fake_env) == expected
    if expected == "zcode-worker":
        assert "zcode-worker" not in w.load_cache()


@pytest.mark.parametrize("reason", ["rate_limit", "refused"])
def test_start_busy_marker_does_not_block_or_consult_paid(home, reason):
    now = time.time()
    install_source(
        home, "return " + repr({"janelas": [{"pct": 100, "reset": now + 7 * 86400}]})
    )
    block_start(home, now + 3600, reason)
    assert p.start_plan_free(now) is True
    assert p.query_reset("zcode-worker", now) is None


@pytest.mark.parametrize("reason", ["rate_limit", "refused"])
def test_preflight_runs_zcode_with_busy_marker(fake_env, monkeypatch, reason):
    monkeypatch.delenv("ZCODE_WORKER_CONTEXT_STATE", raising=False)
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN", raising=False)
    until = time.time() + 86400
    install_source(
        fake_env["tmp"], "return " + repr({"janelas": [{"pct": 100, "reset": until}]})
    )
    block_start(fake_env["tmp"], time.time() + 3600, reason)
    assert _first_called(fake_env) == "zcode-worker"


@pytest.mark.parametrize("reason", ["rate_limit", "refused"])
def test_cached_cooldown_ignored_with_busy_marker(fake_env, monkeypatch, reason):
    monkeypatch.delenv("ZCODE_WORKER_CONTEXT_STATE", raising=False)
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN", raising=False)
    block_start(fake_env["tmp"], time.time() + 3600, reason)
    w = _stale_zcode_entry()
    assert _first_called(fake_env) == "zcode-worker"
    assert "zcode-worker" not in w.load_cache()


def _muse_fake(monkeypatch, tmp_path, corpo, rc=0):
    import quota_provider as qp

    fake = tmp_path / "muse"
    fake.write_text("#!/bin/sh\ncat <<'X'\n" + corpo + "\nX\nexit " + str(rc) + "\n")
    fake.chmod(0o755)
    monkeypatch.setattr(qp.shutil, "which", lambda _n: str(fake))
    return qp


def test_muse_cota_esgotada_bloqueia_ate_o_reset(monkeypatch, tmp_path):
    now = 1_000_000.0
    corpo = (
        '{"allowance":{"window":{"resetsAtMs":%d,"usedPercent":100},'
        '"weekly":{"resetsAtMs":%d,"usedPercent":73}}}'
        % ((now + 600) * 1000, (now + 9000) * 1000)
    )
    qp = _muse_fake(monkeypatch, tmp_path, corpo)
    assert qp.query_reset("muse-ask", now) == now + 600


def test_muse_com_cota_fica_livre(monkeypatch, tmp_path):
    now = 1_000_000.0
    corpo = '{"allowance":{"window":{"resetsAtMs":%d,"usedPercent":20}}}' % (
        (now + 600) * 1000
    )
    qp = _muse_fake(monkeypatch, tmp_path, corpo)
    assert qp.query_reset("muse-ask", now) is None


def test_muse_falha_ou_ausente_fica_desconhecido(monkeypatch, tmp_path):
    now = 1_000_000.0
    assert _muse_fake(monkeypatch, tmp_path, "{}", rc=1).query_reset("muse-ask", now) is None
    assert _muse_fake(monkeypatch, tmp_path, "nao-json").query_reset("muse-ask", now) is None
    assert _muse_fake(monkeypatch, tmp_path, '{"allowance":3}').query_reset("muse-ask", now) is None
    qp = _muse_fake(monkeypatch, tmp_path, "{}")
    monkeypatch.setattr(qp.shutil, "which", lambda _n: None)
    assert qp.query_reset("muse-ask", now) is None
    monkeypatch.setattr(qp.shutil, "which", lambda _n: "/x")
    def lento(*a, **k):
        raise qp.subprocess.TimeoutExpired("muse", 1)
    monkeypatch.setattr(qp.subprocess, "run", lento)
    assert qp.query_reset("muse-ask", now) is None
