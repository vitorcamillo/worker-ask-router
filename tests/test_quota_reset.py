"""Cenarios: limite -> pular ate reset -> liberar; sem gerar modelos reais."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import quota_reset as q
import worker_router as w
from test_worker_router import fake_env as router_fake_env, run_cli

fake_env = router_fake_env

NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc).timestamp()
FUTURE_RESET = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()


@pytest.mark.parametrize(
    "raw,seconds",
    [
        ('{"responseHeaders":{"retry-after":"Wed Oct 28 00:00:00 2026"}}', 86400),
        ("Retry-After: Wed Oct 28 00:00:00 2026", 86400),
        (
            '{"error":{"responseHeaders":{"retry-after":"Wed, 28 Oct 2026 00:00:00 GMT"}}}',
            86400,
        ),
        ('{"responseHeaders":{"retry-after":"7200"}}', 7200),
        (
            "Weekly Limit Exhausted. Your limit will reset at 2026-11-03T00:00:00Z",
            7 * 86400,
        ),
        ("quota resets at 2026-10-28T03:00:00+03:00", 86400),
        ("Retry-After: 7200", 7200),
        ("Retry-After: Wed, 28 Oct 2026 00:00:00 GMT", 86400),
        ('{"error":{"retryAfterMs":7200000}}', 7200),
        ('{"reset_at":"2026-10-28T00:00:00Z"}', 86400),
        ('{"nextResetTime":1793145600000}', 86400),
    ],
)
def test_reset_formats(raw, seconds):
    assert q.parse_reset(raw, NOW) == NOW + seconds


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "quota exceeded",
        '{"retry_after":true}',
        '{"reset_at":NaN}',
        '{"reset_at":Infinity}',
        '{"retry_after":-1}',
        "reset at 2020-01-01T00:00:00Z",
        "reset at 9999-01-01T00:00:00Z",
        "reset at 2026-99-99",
        "[" * 10000,
        "secret=synthetic-private",
    ],
)
def test_invalid_reset_never_invents_date(raw):
    assert q.parse_reset(raw, NOW) is None


def test_multiple_exhausted_dates_use_latest():
    assert (
        q.parse_reset(
            '{"reset_at":"2026-10-28T00:00:00Z","nested":{"reset_at":"2026-11-03T00:00:00Z"}}',
            NOW,
        )
        == NOW + 7 * 86400
    )


def test_cache_weekly_reset_merge_and_expiry(fake_env, monkeypatch):
    until = time.time() + 7 * 86400
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": until,
                "reset_known": True,
            }
        }
    )
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": until - 100,
                "reset_known": True,
            }
        }
    )
    assert w.load_cache()["zcode-worker"]["until"] == until
    assert w.load_cache()["zcode-worker"]["reset_known"] is True
    monkeypatch.setattr(w.time, "time", lambda: until + 1)
    assert w.load_cache() == {}


def test_e2e_quota_persists_and_second_call_skips_without_retry(fake_env):
    until = time.time() + 7 * 86400
    log = fake_env["tmp"] / "calls"
    env = {
        "FAKE_RC_ZCODE_WORKER": "75",
        "FAKE_STDERR": q.MARKER + str(until),
        "FAKE_LOG": str(log),
        "WORKER_ASK_NO_MUSE": "1",
    }
    first = run_cli(fake_env, ["--mode", "plan", "task"], env)
    assert first.returncode == 0
    assert [json.loads(x)["name"] for x in log.read_text().splitlines()] == [
        "zcode-worker",
        "opencode-ask",
    ]
    log.write_text("")
    second = run_cli(fake_env, ["--mode", "plan", "task"], env)
    assert second.returncode == 0
    assert [json.loads(x)["name"] for x in log.read_text().splitlines()] == [
        "opencode-ask"
    ]
    status = run_cli(fake_env, ["--status"])
    entry = json.loads(status.stdout)["cache"]["zcode-worker"]
    assert entry["until"] == until and entry["reset_known"] is True
    # Expiracao automatica: a mesma CLI volta a executar o worker.
    state = fake_env["state"] / "worker-ask/cache.json"
    data = json.loads(state.read_text())
    data["zcode-worker"]["until"] = time.time() - 1
    state.write_text(json.dumps(data))
    log.write_text("")
    result = run_cli(fake_env, ["--mode", "plan", "task"], {"FAKE_LOG": str(log), "WORKER_ASK_NO_MUSE": "1"})
    assert result.stdout == b"READY:zcode-worker"


def test_unknown_reset_does_not_hammer_worker(fake_env):
    env = {"FAKE_RC_ZCODE_WORKER": "75", "WORKER_ASK_NO_MUSE": "1"}
    assert run_cli(fake_env, ["--mode", "plan", "task"], env).returncode == 0
    entry = w.load_cache()["zcode-worker"]
    assert entry["until"] is None and entry["reset_known"] is False
    assert run_cli(fake_env, ["--mode", "plan", "task"], {"WORKER_ASK_NO_MUSE": "1"}).stdout == b"READY:opencode-ask"
    assert (
        run_cli(fake_env, ["--retry-exhausted", "--mode", "plan", "task"]).stdout
        == b"READY:zcode-worker"
    )


def test_helper_emits_only_timestamp(tmp_path):
    path = tmp_path / "error"
    path.write_text(
        f"quota exhausted secret=synthetic-private resets at {FUTURE_RESET}"
    )
    result = subprocess.run(
        [sys.executable, str(Path(q.__file__)), str(path)],
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 0
    assert b"synthetic-private" not in result.stdout + result.stderr
    assert result.stdout.startswith(q.MARKER.encode())
    assert q.marker_reset(result.stdout) is not None


@pytest.mark.parametrize("wrapper", ["codex-ask", "claude-ask"])
def test_real_shell_wrapper_preserves_reset_without_raw_error(tmp_path, wrapper):
    binary = tmp_path / ("codex" if wrapper == "codex-ask" else "claude")
    binary.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "usage limit reached; reset at {FUTURE_RESET} secret=synthetic-private" >&2\nexit 1\n'
    )
    binary.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": str(tmp_path) + ":" + os.environ["PATH"],
        "WORKER_ASK_QUOTA_HELPER": str(Path(q.__file__)),
        "ASK_PRESENTATION_ACTIVE": "1",
    }
    result = subprocess.run(
        [
            str(Path(q.__file__).parent / "bin" / wrapper),
            "--cwd",
            str(tmp_path),
            "--mode",
            "write",
            "task",
        ],
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 75
    assert q.marker_reset(result.stderr) is not None
    assert b"synthetic-private" not in result.stderr + result.stdout


def test_timeout_opens_circuit_for_next_call(fake_env):
    before = time.time()
    result = run_cli(
        fake_env,
        ["--mode", "write", "--allowed-file", "a.txt", "--step-timeout", "0.1", "task"],
        {"FAKE_MODE_ZCODE_WORKER": "sleep", "FAKE_SLEEP": "2", "WORKER_ASK_NO_MUSE": "1"},
    )
    assert result.returncode == 124
    entry = w.load_cache()["zcode-worker"]
    assert entry["blocked_reason"] == "timeout"
    assert entry["reset_known"] is False
    assert isinstance(entry["until"], float)
    assert before + w.TIMEOUT_COOLDOWN_SECONDS - 5 <= entry["until"] <= time.time() + w.TIMEOUT_COOLDOWN_SECONDS + 5
    assert run_cli(fake_env, ["--mode", "plan", "task"], {"WORKER_ASK_NO_MUSE": "1"}).stdout == b"READY:opencode-ask"


def test_write_timeout_with_progress_skips_circuit(fake_env):
    result = run_cli(
        fake_env,
        ["--mode", "write", "--allowed-file", "a.txt", "--step-timeout", "0.1", "task"],
        {
            "FAKE_MODE_ZCODE_WORKER": "writesleep",
            "FAKE_WRITE_REL": "a.txt",
            "FAKE_WRITE_CONTENT": "progresso",
            "FAKE_SLEEP": "2",
            "WORKER_ASK_NO_MUSE": "1",
        },
    )
    assert result.returncode == 124
    assert "estourou com progresso; sem cooldown" in result.stderr.decode()
    assert "zcode-worker" not in w.load_cache()
    # Sem cooldown: a chamada seguinte volta a executar o zcode-worker.
    assert run_cli(fake_env, ["--mode", "plan", "task"], {"WORKER_ASK_NO_MUSE": "1"}).stdout == b"READY:zcode-worker"


def test_successful_explicit_retry_clears_unknown_block(fake_env):
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": None,
                "reset_known": False,
            }
        }
    )
    assert (
        run_cli(fake_env, ["--retry-exhausted", "--mode", "plan", "task"]).returncode
        == 0
    )
    assert "zcode-worker" not in w.load_cache()
    assert run_cli(fake_env, ["--mode", "plan", "task"]).stdout == b"READY:zcode-worker"


def test_concurrent_new_block_survives_old_success(fake_env):
    unknown = {"wrapper": "zcode-worker", "until": None, "reset_known": False}
    w.save_cache({"zcode-worker": unknown})
    observed = w.load_cache()["zcode-worker"]
    later = time.time() + 86400
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": later,
                "reset_known": True,
            }
        }
    )
    w.save_cache({}, clear={"zcode-worker": observed})
    assert w.load_cache()["zcode-worker"]["until"] == later


def test_new_unknown_generation_survives_old_success(fake_env):
    unknown = {"wrapper": "zcode-worker", "until": None, "reset_known": False}
    w.save_cache({"zcode-worker": unknown})
    old = w.load_cache()["zcode-worker"]
    w.save_cache({"zcode-worker": unknown})
    new = w.load_cache()["zcode-worker"]
    assert old["generation"] != new["generation"]
    w.save_cache({}, clear={"zcode-worker": old})
    assert w.load_cache()["zcode-worker"] == new


def test_admission_busy_does_not_persist_quota(fake_env):
    result = run_cli(
        fake_env,
        ["--mode", "plan", "task"],
        {
            "FAKE_RC_ZCODE_WORKER": "75",
            "FAKE_STDERR": "ERRO: fila de admissão cheia; nenhum worker iniciado",
        },
    )
    assert result.returncode == 0
    assert "zcode-worker" not in w.load_cache()
    assert run_cli(fake_env, ["--mode", "plan", "task"]).stdout == b"READY:zcode-worker"


def test_quota_query_respects_router_total_deadline(fake_env):
    source = fake_env["tmp"] / ".local/bin/ask-limites-json"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def limites_glm():\n    import time\n    time.sleep(2)\n    return {}\n"
    )
    start = time.monotonic()
    result = run_cli(fake_env, ["--mode", "plan", "--timeout", "0.1", "task"])
    assert result.returncode == 124
    assert time.monotonic() - start < 1.5
    assert result.stdout == b""


def test_shorter_new_quota_survives_old_success(fake_env):
    until = time.time() + 86400
    entry = {"wrapper": "zcode-worker", "until": until, "reset_known": True}
    w.save_cache({"zcode-worker": entry})
    old = w.load_cache()["zcode-worker"]
    w.save_cache({"zcode-worker": {**entry, "until": until - 100}})
    new = w.load_cache()["zcode-worker"]
    assert new["until"] == until
    assert new["generation"] != old["generation"]
    w.save_cache({}, clear={"zcode-worker": old})
    assert w.load_cache()["zcode-worker"] == new


def test_new_timeout_does_not_override_known_quota_reset(fake_env):
    until = time.time() + 86400
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": until,
                "reset_known": True,
            }
        }
    )
    old = w.load_cache()["zcode-worker"]
    timeout_until = time.time() + w.TIMEOUT_COOLDOWN_SECONDS
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": timeout_until,
                "reset_known": False,
                "blocked_reason": "timeout",
            }
        }
    )
    entry = w.load_cache()["zcode-worker"]
    assert entry.get("blocked_reason") != "timeout"
    assert entry["reset_known"] is True
    assert entry["until"] == pytest.approx(until)
    assert entry["generation"] != old["generation"]
    w.save_cache({}, clear={"zcode-worker": old})
    assert w.load_cache()["zcode-worker"] == entry


def test_late_quota_replaces_timeout_circuit(fake_env):
    timeout_until = time.time() + w.TIMEOUT_COOLDOWN_SECONDS
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": timeout_until,
                "reset_known": False,
                "blocked_reason": "timeout",
            }
        }
    )
    old = w.load_cache()["zcode-worker"]
    quota_until = time.time() + 86400
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": quota_until,
                "reset_known": True,
            }
        }
    )
    entry = w.load_cache()["zcode-worker"]
    assert entry.get("blocked_reason") != "timeout"
    assert entry["reset_known"] is True
    assert entry["until"] == pytest.approx(quota_until)
    assert entry["generation"] != old["generation"]
    w.save_cache({}, clear={"zcode-worker": old})
    assert w.load_cache()["zcode-worker"] == entry


def test_timeout_does_not_override_unknown_quota(fake_env):
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": None,
                "reset_known": False,
            }
        }
    )
    old = w.load_cache()["zcode-worker"]
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": time.time() + w.TIMEOUT_COOLDOWN_SECONDS,
                "reset_known": False,
                "blocked_reason": "timeout",
            }
        }
    )
    entry = w.load_cache()["zcode-worker"]
    assert entry["until"] is None and entry["reset_known"] is False
    assert entry.get("blocked_reason") != "timeout"
    assert entry["generation"] != old["generation"]
    w.save_cache({}, clear={"zcode-worker": old})
    assert w.load_cache()["zcode-worker"] == entry


def test_unknown_quota_replaces_timeout_circuit(fake_env):
    timeout_until = time.time() + w.TIMEOUT_COOLDOWN_SECONDS
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": timeout_until,
                "reset_known": False,
                "blocked_reason": "timeout",
            }
        }
    )
    old = w.load_cache()["zcode-worker"]
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": None,
                "reset_known": False,
            }
        }
    )
    entry = w.load_cache()["zcode-worker"]
    assert entry["until"] is None and entry["reset_known"] is False
    assert entry.get("blocked_reason") != "timeout"
    assert entry["generation"] != old["generation"]
    w.save_cache({}, clear={"zcode-worker": old})
    assert w.load_cache()["zcode-worker"] == entry


def test_timeout_circuit_expires_after_cooldown(fake_env, monkeypatch):
    timeout_until = time.time() + w.TIMEOUT_COOLDOWN_SECONDS
    w.save_cache(
        {
            "zcode-worker": {
                "wrapper": "zcode-worker",
                "until": timeout_until,
                "reset_known": False,
                "blocked_reason": "timeout",
            }
        }
    )
    assert w.load_cache()["zcode-worker"]["blocked_reason"] == "timeout"
    monkeypatch.setattr(w.time, "time", lambda: timeout_until + 1)
    assert w.load_cache() == {}


def test_legacy_timeout_cache_migrates_and_unknown_quota_preserved(fake_env):
    cache_path = fake_env["state"] / "worker-ask" / "cache.json"
    w.ensure_state_dir()
    cache_path.write_text(
        json.dumps(
            {
                "zcode-worker": {
                    "wrapper": "zcode-worker",
                    "until": None,
                    "reset_known": False,
                    "blocked_reason": "timeout",
                },
                "opencode-ask": {"wrapper": "opencode-ask", "until": None, "reset_known": False},
            }
        )
    )
    cache = w.load_cache()
    assert "zcode-worker" not in cache
    entry = cache["opencode-ask"]
    assert entry["until"] is None and entry["reset_known"] is False
    assert entry["blocked_reason"] == "quota"
    assert run_cli(fake_env, ["--mode", "plan", "task"], {"WORKER_ASK_NO_MUSE": "1"}).stdout == b"READY:zcode-worker"
