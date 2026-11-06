"""Start Plan: janela, bloqueio com validade, confirmacao e sonda pre-edit."""
from __future__ import annotations
import contextlib
import datetime
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from zcode_worker import base, cli, features

def feature_args(**overrides):
    values = {"tarefa": None, "cwd": os.getcwd(), "mode": "plan", "spec": None, "timeout": 2.0, "queue_timeout": 2.0, "total_timeout": None, "model": "glm-5.3-flash", "effort": "high", "profile": None, "resume": None, "attach": [], "disallowed_tools": [], "max_output_bytes": None, "json": False, "dry_run": False, "doctor": False, "list_models": False, "saida": None, "status": None, "cancel": None, "_job_child": None}
    values.update(overrides)
    return SimpleNamespace(**values)

def cli_args(tmp_path, **overrides):
    return feature_args(cwd=str(tmp_path), **overrides)

def normalized_args(tmp_path, **overrides):
    args = cli_args(tmp_path, **overrides)
    return features.public_namespace(args)

def write_provider(path: Path, *, model="GLM-5.3-Flash", effort="high", nested=False):
    sel = {"providerId": base.PROVIDER_ID, "modelId": model, "options": {"reasoningLevel": effort}}
    payload = {"config": {"defaultModelSelection": sel}} if nested else {"defaultModelSelection": sel}
    path.write_text(json.dumps(payload), encoding="utf-8")

def make_selection_db(path: Path, *, session="sess_db", provider=None, model="GLM-5.3-Flash", effort="high"):
    provider = provider or base.PROVIDER_ID
    connection = sqlite3.connect(path)
    connection.execute("create table if not exists session_entry(session_id text, type text, time_created int, data text)")
    connection.execute("insert into session_entry values (?, ?, ?, ?)", (session, "runtime/model_selection", 1, json.dumps({"modelSelection": {"providerId": provider, "modelId": model, "options": {"reasoningLevel": effort}}})))
    connection.commit()
    connection.close()

def make_resume_db(path: Path, *, session="sess_resume", cwd=".", provider=None, model="GLM-5.3-Flash", effort="high"):
    provider = provider or base.PROVIDER_ID
    connection = sqlite3.connect(path)
    connection.execute("create table session(id text, directory text)")
    connection.execute("insert into session values (?, ?)", (session, cwd))
    connection.execute("create table if not exists session_entry(session_id text, type text, time_created int, data text)")
    connection.execute("insert into session_entry values (?, ?, ?, ?)", (session, "runtime/model_selection", 1, json.dumps({"modelSelection": {"providerId": provider, "modelId": model, "options": {"reasoningLevel": effort}}})))
    connection.commit()
    connection.close()

def noon(day=15):
    return datetime.datetime(2026, 10, day, 12, 0)

def test_start_plan_hours_defaults_and_shapes():
    assert features.start_plan_hours({}) == (0, 24)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": "9-18"}) == (9, 18)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": " 12 - 22 "}) == (12, 22)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": "0-24"}) == (0, 24)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": "always"}) == (0, 24)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": " ALWAYS "}) == (0, 24)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": "Always"}) == (0, 24)

def test_start_plan_hours_invalid_falls_back():
    for raw in ("22-12", "x", "0-25", "12", "12-22-1", "a-b", "", "12-", "-5", "5-5", "24-24"):
        assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": raw}) == (0, 24)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": 12}) == (0, 24)
    assert features.start_plan_hours({"ZCODE_WORKER_START_PLAN_HOURS": None}) == (0, 24)

def test_window_boundaries(tmp_path):
    s = tmp_path / "state"
    window = {"ZCODE_WORKER_START_PLAN_HOURS": "12-22"}
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 11, 59), state_dir=s, environ=window) == base.PROVIDER_ID
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 12, 0), state_dir=s, environ=window) == features.START_PLAN_PROVIDER_ID
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 21, 59), state_dir=s, environ=window) == features.START_PLAN_PROVIDER_ID
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 22, 0), state_dir=s, environ=window) == base.PROVIDER_ID

def test_provider_for_respects_hours_env(tmp_path):
    s = tmp_path / "state"
    env = {"ZCODE_WORKER_START_PLAN_HOURS": "9-18"}
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 8, 0), state_dir=s, environ=env) == base.PROVIDER_ID
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 12, 0), state_dir=s, environ=env) == features.START_PLAN_PROVIDER_ID
    always = {"ZCODE_WORKER_START_PLAN_HOURS": "always"}
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 3, 0), state_dir=s, environ=always) == features.START_PLAN_PROVIDER_ID
    # Inválido cai no padrão (0, 24): vale o dia todo, inclusive fora de qualquer janela restrita.
    invalid = {"ZCODE_WORKER_START_PLAN_HOURS": "x"}
    assert features.provider_for("glm-5.3-flash", now=noon(), state_dir=s, environ=invalid) == features.START_PLAN_PROVIDER_ID
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 3, 0), state_dir=s, environ=invalid) == features.START_PLAN_PROVIDER_ID


def test_provider_for_default_window_covers_all_day(tmp_path):
    s = tmp_path / "state"
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 3, 0), state_dir=s, environ={}) == features.START_PLAN_PROVIDER_ID

def test_glm53_never_start(tmp_path):
    s = tmp_path / "state"
    assert features.provider_for("glm-5.3", now=noon(), state_dir=s, environ={}) == base.PROVIDER_ID
    assert features.provider_for("GLM-5.3", now=noon(), state_dir=s, environ={}) == base.PROVIDER_ID

def test_env_disable_and_enable(tmp_path):
    s = tmp_path / "state"
    for value in ("0", "off", "OFF", "false", "False", "no", "NO", " 0 "):
        assert features.provider_for("glm-5.3-flash", now=noon(), state_dir=s, environ={"ZCODE_WORKER_START_PLAN": value}) == base.PROVIDER_ID
    assert features.provider_for("glm-5.3-flash", now=noon(), state_dir=s, environ={"ZCODE_WORKER_START_PLAN": "1"}) == features.START_PLAN_PROVIDER_ID
    assert features.provider_for("glm-5.3-flash", now=noon(), state_dir=s, environ={}) == features.START_PLAN_PROVIDER_ID

def test_provider_for_invalid_model(tmp_path):
    assert features.provider_for("invalido", now=noon(), state_dir=tmp_path / "s", environ={}) == base.PROVIDER_ID

def test_block_rate_limit_last_2_minutes(tmp_path):
    s = tmp_path / "state"
    now = datetime.datetime(2026, 10, 15, 13, 20)
    assert features.start_plan_blocked(now=now, state_dir=s) is False
    features.mark_start_plan_unavailable("rate_limit", now=now, state_dir=s)
    marker = s / features.START_PLAN_MARKER
    assert marker.read_text(encoding="utf-8") == "2026-10-15T13:22:00 rate_limit\n"
    assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 15, 13, 21), state_dir=s) is True
    assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 15, 13, 22), state_dir=s) is False
    assert features.provider_for("glm-5.3-flash", now=datetime.datetime(2026, 10, 15, 13, 21), state_dir=s, environ={}) == base.PROVIDER_ID

def test_block_refused_last_2_minutes(tmp_path):
    s = tmp_path / "state"
    now = datetime.datetime(2026, 10, 15, 13, 20)
    features.mark_start_plan_unavailable("refused", now=now, state_dir=s)
    assert (s / features.START_PLAN_MARKER).read_text(encoding="utf-8") == "2026-10-15T13:22:00 refused\n"
    assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 15, 13, 21), state_dir=s) is True
    assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 15, 13, 22), state_dir=s) is False

def test_block_quota_until_midnight(tmp_path):
    s = tmp_path / "state"
    now = datetime.datetime(2026, 10, 15, 13, 20)
    features.mark_start_plan_unavailable("quota", now=now, state_dir=s)
    assert (s / features.START_PLAN_MARKER).read_text(encoding="utf-8") == "2026-10-16T00:00:00 quota\n"
    assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 15, 23, 59), state_dir=s) is True
    assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 16, 0, 0), state_dir=s) is False
    for reason in ("drift", "mismatch"):
        features.mark_start_plan_unavailable(reason, now=now, state_dir=s)
        assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 15, 23, 0), state_dir=s) is True
        assert features.start_plan_blocked(now=datetime.datetime(2026, 10, 16, 0, 0), state_dir=s) is False

def test_blocked_malformed_not_blocked(tmp_path, monkeypatch):
    s = tmp_path / "state"
    assert features.start_plan_blocked(now=noon(), state_dir=s) is False
    s.mkdir(parents=True)
    (s / features.START_PLAN_MARKER).write_text("", encoding="utf-8")
    assert features.start_plan_blocked(now=noon(), state_dir=s) is False
    (s / features.START_PLAN_MARKER).write_text("so-um-token\n", encoding="utf-8")
    assert features.start_plan_blocked(now=noon(), state_dir=s) is False
    (s / features.START_PLAN_MARKER).write_text("nao-data quota\n", encoding="utf-8")
    assert features.start_plan_blocked(now=noon(), state_dir=s) is False
    (s / features.START_PLAN_MARKER).write_text("2026-10-15T14:00:00+00:00 quota\n", encoding="utf-8")
    assert features.start_plan_blocked(now=noon(), state_dir=s) is False
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert features.start_plan_blocked(now=noon(), state_dir=s) is False

def test_mark_removes_positive_marker(tmp_path):
    s = tmp_path / "state"
    now = noon()
    features.mark_start_plan_confirmed(now=now, state_dir=s)
    assert (s / features.START_PLAN_OK_MARKER).exists()
    features.mark_start_plan_unavailable("quota", now=now, state_dir=s)
    assert not (s / features.START_PLAN_OK_MARKER).exists()

def test_mark_writes_mode_and_swallows_errors(tmp_path, monkeypatch):
    s = tmp_path / "state"
    now = noon()
    features.mark_start_plan_unavailable("drift", now=now, state_dir=s)
    marker = s / features.START_PLAN_MARKER
    assert marker.read_text(encoding="utf-8") == "2026-10-16T00:00:00 drift\n"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(s.stat().st_mode) == 0o700
    monkeypatch.setattr("tempfile.mkstemp", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    features.mark_start_plan_unavailable("quota", now=now, state_dir=tmp_path / "s2")
    monkeypatch.undo()
    def boom_replace(*a, **k):
        raise OSError("replace boom")
    monkeypatch.setattr(os, "replace", boom_replace)
    features.mark_start_plan_unavailable("quota", now=now, state_dir=tmp_path / "s3")

def test_mark_confirmed_writes_mode_and_errors(tmp_path, monkeypatch):
    s = tmp_path / "state"
    now = datetime.datetime(2026, 10, 15, 13, 20)
    features.mark_start_plan_confirmed(now=now, state_dir=s)
    ok_marker = s / features.START_PLAN_OK_MARKER
    assert ok_marker.read_text(encoding="utf-8") == "2026-10-15T13\n"
    assert stat.S_IMODE(ok_marker.stat().st_mode) == 0o600
    monkeypatch.setattr("tempfile.mkstemp", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    features.mark_start_plan_confirmed(now=now, state_dir=tmp_path / "s2")
    monkeypatch.undo()
    def boom_replace(*a, **k):
        raise OSError("replace boom")
    monkeypatch.setattr(os, "replace", boom_replace)
    features.mark_start_plan_confirmed(now=now, state_dir=tmp_path / "s3")

def test_start_plan_confirmed_only_same_hour(tmp_path, monkeypatch):
    s = tmp_path / "state"
    now = datetime.datetime(2026, 10, 15, 13, 20)
    assert features.start_plan_confirmed(now=now, state_dir=s) is False
    features.mark_start_plan_confirmed(now=now, state_dir=s)
    assert features.start_plan_confirmed(now=now, state_dir=s) is True
    assert features.start_plan_confirmed(now=datetime.datetime(2026, 10, 15, 13, 59), state_dir=s) is True
    assert features.start_plan_confirmed(now=datetime.datetime(2026, 10, 15, 14, 0), state_dir=s) is False
    assert features.start_plan_confirmed(now=datetime.datetime(2026, 10, 16, 13, 20), state_dir=s) is False
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert features.start_plan_confirmed(now=now, state_dir=s) is False

def test_temporary_provider_ids(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    write_provider(provider)
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    state = tmp_path / "state"
    with features.temporary_provider("glm-5.3-flash", "high", state_dir=state, provider=features.START_PLAN_PROVIDER_ID) as path:
        assert json.loads(Path(path).read_text())["defaultModelSelection"]["providerId"] == features.START_PLAN_PROVIDER_ID
    with features.temporary_provider("glm-5.3-flash", "high", state_dir=state) as path:
        assert json.loads(Path(path).read_text())["defaultModelSelection"]["providerId"] == base.PROVIDER_ID
    with pytest.raises(features.FeatureError, match="provider inválido"):
        with features.temporary_provider("glm-5.3-flash", "high", state_dir=state, provider="wrong"):
            pass

def test_temporary_provider_nested_shape(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    write_provider(provider, nested=True)
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    state = tmp_path / "state"
    with features.temporary_provider("glm-5.3-flash", "high", state_dir=state, provider=features.START_PLAN_PROVIDER_ID) as path:
        assert json.loads(Path(path).read_text())["config"]["defaultModelSelection"]["providerId"] == features.START_PLAN_PROVIDER_ID

def test_selection_from_json_both(tmp_path):
    good_plan = {"modelSelection": {"providerId": features.START_PLAN_PROVIDER_ID, "modelId": "GLM-5.3-Flash", "options": {"reasoningLevel": "high"}}}
    assert features._selection_from_json(good_plan)["providerId"] == features.START_PLAN_PROVIDER_ID
    good_paid = {"modelSelection": {"providerId": base.PROVIDER_ID, "modelId": "GLM-5.3-Flash", "options": {"reasoningLevel": "high"}}}
    assert features._selection_from_json(good_paid)["providerId"] == base.PROVIDER_ID
    with pytest.raises(features.FeatureError):
        features._selection_from_json({"modelSelection": {"providerId": "wrong", "modelId": "GLM-5.3-Flash", "options": {"reasoningLevel": "high"}}})
    with pytest.raises(features.FeatureError):
        features.requested_selection("glm-5.3-flash", "high", "wrong")
    assert features.requested_selection("glm-5.3-flash", "high", features.START_PLAN_PROVIDER_ID)["providerId"] == features.START_PLAN_PROVIDER_ID
    assert features.allowed_providers() == frozenset({base.PROVIDER_ID, features.START_PLAN_PROVIDER_ID})
    listed = cli.list_models()
    assert all(features.START_PLAN_PROVIDER_ID not in json.dumps(combo) for combo in listed["combinations"])

def test_verify_silent_fallback(tmp_path):
    db = tmp_path / "sel.sqlite"
    make_selection_db(db, provider=base.PROVIDER_ID, model="GLM-5.3-Flash", effort="high")
    actual = features.verify_effective({"sessionId": "sess_db"}, "glm-5.3-flash", "high", db_path=db, provider=features.START_PLAN_PROVIDER_ID)
    assert actual["providerId"] == base.PROVIDER_ID
    with pytest.raises(features.FeatureError):
        features.verify_effective({"sessionId": "sess_db"}, "glm-5.3-flash", "low", db_path=db, provider=features.START_PLAN_PROVIDER_ID)

def test_classify_drift():
    assert cli._classify_process_failure(1, "auth launcher start-plan drift").code == "start_plan_drift"
    assert cli._classify_process_failure(1, "auth launcher start-plan drift\n").code == "start_plan_drift"
    assert cli._classify_process_failure(1, "quota exceeded").code == "quota"

def test_execute_patches_env(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    write_provider(provider)
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli.base, "global_slot", lambda *a: contextlib.nullcontext())
    seen = {}
    def runner(command, env, deadline, limit):
        seen.update(env)
        return 0, '{"sessionId":"sess_ok","response":"ok"}', ""
    monkeypatch.setattr(cli.features, "verify_effective", lambda *a, **k: {"ok": True})
    args = normalized_args(tmp_path, mode="plan")
    args._provider = features.START_PLAN_PROVIDER_ID
    cli._execute(args, "task", runner=runner)
    assert seen.get("ZCODE_WORKER_START_PLAN_PATCHES") == "1"
    monkeypatch.setenv("ZCODE_WORKER_START_PLAN_PATCHES", "1")
    args2 = normalized_args(tmp_path, mode="plan")
    args2._provider = base.PROVIDER_ID
    seen2 = {}
    def runner2(command, env, deadline, limit):
        seen2.update(env)
        return 0, '{"sessionId":"sess_ok","response":"ok"}', ""
    cli._execute(args2, "task", runner=runner2)
    assert "ZCODE_WORKER_START_PLAN_PATCHES" not in seen2

def _run_with_mocks(tmp_path, monkeypatch, *, mode, responses, provider_start=True, conversation=False, db_provider=None):
    db = tmp_path / "db.sqlite"
    make_selection_db(db, session="sess_1", provider=db_provider or base.PROVIDER_ID)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    if provider_start:
        monkeypatch.setattr(features, "provider_for", lambda *a, **k: features.START_PLAN_PROVIDER_ID)
    else:
        monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    monkeypatch.setattr(features, "start_plan_confirmed", lambda *a, **k: True)
    calls = {"n": 0, "deadlines": []}
    def fake_execute(args, task, **kwargs):
        calls["n"] += 1
        calls["deadlines"].append(kwargs.get("total_deadline"))
        item = responses[calls["n"] - 1]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(cli, "_execute", fake_execute)
    args = cli_args(tmp_path, mode=mode, conversation=conversation)
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    return result, code, calls

def test_fallback_plan_quota(tmp_path, monkeypatch):
    ok = (0, '{"sessionId":"sess_1","response":"ok"}', "")
    result, code, calls = _run_with_mocks(tmp_path, monkeypatch, mode="plan", responses=[features.FeatureError("q", "quota"), ok])
    assert code == 0 and result["status"] == "ok"
    assert result["startPlanFallback"] == "quota"
    assert result["effective"]["providerId"] == base.PROVIDER_ID
    assert result["requested"]["providerId"] == base.PROVIDER_ID
    assert calls["n"] == 2 and calls["deadlines"][0] == calls["deadlines"][1]
    marker = Path(base.STATE) / features.START_PLAN_MARKER
    assert marker.exists()

def test_fallback_plan_rate_limit_marks(tmp_path, monkeypatch):
    ok = (0, '{"sessionId":"sess_1","response":"ok"}', "")
    result, code, calls = _run_with_mocks(tmp_path, monkeypatch, mode="plan", responses=[features.FeatureError("r", "rate_limit"), ok])
    assert result["startPlanFallback"] == "rate_limit"
    assert calls["n"] == 2
    marker = Path(base.STATE) / features.START_PLAN_MARKER
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").endswith(" rate_limit\n")

def test_execute_start_plan_rate_limit_propagates_without_retry(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    write_provider(provider)
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli.base, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(cli.base, "global_slot", lambda *a: contextlib.nullcontext())
    calls = []

    def runner(command, env, deadline, limit):
        calls.append(env.get("ZCODE_WORKER_START_PLAN_PATCHES"))
        return 1, "", "HTTP 429 rate limit"

    args = normalized_args(tmp_path, mode="plan")
    args._provider = features.START_PLAN_PROVIDER_ID
    with pytest.raises(features.FeatureError) as raised:
        cli._execute(args, "task", runner=runner)
    assert raised.value.code == "rate_limit"
    assert calls == ["1"]

def test_fallback_drift_edit_repeats(tmp_path, monkeypatch):
    ok = (0, '{"sessionId":"sess_1","response":"ok"}', "")
    result, code, calls = _run_with_mocks(tmp_path, monkeypatch, mode="edit", responses=[(1, "", "auth launcher start-plan drift"), ok])
    assert code == 0 and result["startPlanFallback"] == "drift" and calls["n"] == 2

def test_fallback_mismatch(tmp_path, monkeypatch):
    ok = (0, '{"sessionId":"sess_1","response":"ok"}', "")
    result, code, calls = _run_with_mocks(tmp_path, monkeypatch, mode="plan", responses=[ok])
    assert result["startPlanFallback"] == "mismatch" and calls["n"] == 1
    assert result["effective"]["providerId"] == base.PROVIDER_ID

def test_success_start_plan_marks_confirmed(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    make_selection_db(db, session="sess_1", provider=features.START_PLAN_PROVIDER_ID)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: features.START_PLAN_PROVIDER_ID)
    monkeypatch.setattr(features, "start_plan_confirmed", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_execute", lambda *a, **k: (0, '{"sessionId":"sess_1","response":"ok"}', ""))
    args = cli_args(tmp_path, mode="plan")
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code == 0 and result["effective"]["providerId"] == features.START_PLAN_PROVIDER_ID
    assert "startPlanFallback" not in result
    assert (Path(base.STATE) / features.START_PLAN_OK_MARKER).exists()

def test_exhausted_edit_quota(tmp_path, monkeypatch):
    result, code, calls = _run_with_mocks(tmp_path, monkeypatch, mode="edit", responses=[features.FeatureError("q", "quota")])
    assert code == 1 and result["error"]["code"] == "start_plan_exhausted"
    assert calls["n"] == 1
    assert (Path(base.STATE) / features.START_PLAN_MARKER).exists()

def test_second_attempt_failure_flows_normal(tmp_path, monkeypatch):
    result, code, calls = _run_with_mocks(tmp_path, monkeypatch, mode="plan", responses=[features.FeatureError("d", "start_plan_drift"), (1, "", "quota exceeded")])
    assert code == 75 and result["error"]["code"] == "quota" and calls["n"] == 2

def test_start_other_error_no_repeat(tmp_path, monkeypatch):
    result, code, calls = _run_with_mocks(tmp_path, monkeypatch, mode="plan", responses=[features.FeatureError("x", "cli_failed")])
    assert result["error"]["code"] == "cli_failed" and calls["n"] == 1

def test_probe_unit_success_and_effects_restored(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    make_selection_db(db, session="sess_probe", provider=features.START_PLAN_PROVIDER_ID)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    seen = {}
    def fake_execute(probe_args, task, **kwargs):
        seen["mode"] = probe_args.mode
        seen["task"] = task
        seen["timeout"] = probe_args.timeout
        probe_args._effects_may_exist = True
        return (0, '{"sessionId":"sess_probe","response":"ok"}', "")
    monkeypatch.setattr(cli, "_execute", fake_execute)
    args = normalized_args(tmp_path, mode="edit", timeout=500.0)
    args._provider = features.START_PLAN_PROVIDER_ID
    args._effects_may_exist = True
    payload, effective = cli._probe_start_plan(args, db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert seen["mode"] == "plan" and seen["task"] == "Responda apenas: OK"
    assert seen["timeout"] == 120
    assert effective["providerId"] == features.START_PLAN_PROVIDER_ID
    assert args._effects_may_exist is False

def test_probe_unit_returncode_maps_to_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_execute", lambda *a, **k: (1, "", "quota exceeded"))
    args = normalized_args(tmp_path, mode="edit")
    args._provider = features.START_PLAN_PROVIDER_ID
    with pytest.raises(features.FeatureError, match="cota"):
        cli._probe_start_plan(args, db_path=tmp_path / "db.sqlite")

def _run_probe_integration(tmp_path, monkeypatch, *, probe_side, main_ok_provider, mode="edit", resume=None, timeout=2.0):
    db = tmp_path / "db.sqlite"
    make_selection_db(db, session="sess_1", provider=main_ok_provider)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: features.START_PLAN_PROVIDER_ID)
    monkeypatch.setattr(features, "start_plan_confirmed", lambda *a, **k: False)
    probe_calls = {"n": 0}
    def fake_probe(args, **kwargs):
        probe_calls["n"] += 1
        if isinstance(probe_side, Exception):
            raise probe_side
        return probe_side
    monkeypatch.setattr(cli, "_probe_start_plan", fake_probe)
    main_calls = {"n": 0, "modes": []}
    def fake_execute(args, task, **kwargs):
        main_calls["n"] += 1
        main_calls["modes"].append(args.mode)
        return (0, '{"sessionId":"sess_1","response":"ok"}', "")
    monkeypatch.setattr(cli, "_execute", fake_execute)
    args = cli_args(tmp_path, mode=mode, resume=resume, timeout=timeout)
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    return result, code, probe_calls, main_calls

def test_probe_edit_ok_keeps_start_plan(tmp_path, monkeypatch):
    start_effective = {"providerId": features.START_PLAN_PROVIDER_ID, "modelId": "GLM-5.3-Flash", "effort": "high"}
    result, code, probe_calls, main_calls = _run_probe_integration(
        tmp_path, monkeypatch,
        probe_side=({"sessionId": "sess_probe", "response": "ok"}, start_effective),
        main_ok_provider=features.START_PLAN_PROVIDER_ID,
    )
    assert code == 0 and probe_calls["n"] == 1 and main_calls["n"] == 1
    assert main_calls["modes"] == ["edit"]
    assert result["effective"]["providerId"] == features.START_PLAN_PROVIDER_ID
    assert "startPlanFallback" not in result
    assert (Path(base.STATE) / features.START_PLAN_OK_MARKER).exists()

def test_probe_edit_rate_limit_falls_back_probe(tmp_path, monkeypatch):
    result, code, probe_calls, main_calls = _run_probe_integration(
        tmp_path, monkeypatch,
        probe_side=features.FeatureError("r", "rate_limit"),
        main_ok_provider=base.PROVIDER_ID,
    )
    assert code == 0 and result["startPlanFallback"] == "probe"
    assert result["effective"]["providerId"] == base.PROVIDER_ID
    assert result["requested"]["providerId"] == base.PROVIDER_ID
    marker = (Path(base.STATE) / features.START_PLAN_MARKER).read_text(encoding="utf-8")
    assert marker.endswith(" refused\n")

def test_probe_edit_quota_and_drift_mark(tmp_path, monkeypatch):
    for probe_code, reason in (("quota", "quota"), ("start_plan_drift", "drift")):
        result, code, _, _ = _run_probe_integration(
            tmp_path, monkeypatch,
            probe_side=features.FeatureError("x", probe_code),
            main_ok_provider=base.PROVIDER_ID,
        )
        assert code == 0 and result["startPlanFallback"] == "probe"
        assert (Path(base.STATE) / features.START_PLAN_MARKER).read_text(encoding="utf-8").endswith(f" {reason}\n")

def test_probe_edit_silent_plan_mismatch(tmp_path, monkeypatch):
    plan_effective = {"providerId": base.PROVIDER_ID, "modelId": "GLM-5.3-Flash", "effort": "high"}
    result, code, _, _ = _run_probe_integration(
        tmp_path, monkeypatch,
        probe_side=({"sessionId": "sess_probe", "response": "ok"}, plan_effective),
        main_ok_provider=base.PROVIDER_ID,
    )
    assert code == 0 and result["startPlanFallback"] == "probe"
    assert (Path(base.STATE) / features.START_PLAN_MARKER).exists()

def test_probe_skipped_when_confirmed(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    make_selection_db(db, session="sess_1", provider=features.START_PLAN_PROVIDER_ID)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: features.START_PLAN_PROVIDER_ID)
    monkeypatch.setattr(features, "start_plan_confirmed", lambda *a, **k: True)
    def boom_probe(*a, **k):
        raise AssertionError("sonda nao deveria rodar")
    monkeypatch.setattr(cli, "_probe_start_plan", boom_probe)
    monkeypatch.setattr(cli, "_execute", lambda *a, **k: (0, '{"sessionId":"sess_1","response":"ok"}', ""))
    args = cli_args(tmp_path, mode="edit")
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code == 0 and "startPlanFallback" not in result

def test_probe_skipped_plan_resume_dryrun(tmp_path, monkeypatch):
    for mode, resume, dry_run in (("plan", None, False), ("edit", "sess_resume", False), ("edit", None, True)):
        db = tmp_path / f"db-{mode}-{dry_run}-{bool(resume)}.sqlite"
        make_selection_db(db, session="sess_1", provider=base.PROVIDER_ID)
        monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
        monkeypatch.setattr(features, "provider_for", lambda *a, **k: features.START_PLAN_PROVIDER_ID)
        monkeypatch.setattr(features, "start_plan_confirmed", lambda *a, **k: False)
        def boom_probe(*a, **k):
            raise AssertionError("sonda nao deveria rodar")
        monkeypatch.setattr(cli, "_probe_start_plan", boom_probe)
        monkeypatch.setattr(cli, "_execute", lambda *a, **k: (0, '{"sessionId":"sess_1","response":"ok"}', ""))
        args = cli_args(tmp_path, mode=mode, resume=resume, dry_run=dry_run)
        result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
        assert code == 0
        assert result.get("startPlanFallback") in (None, "mismatch")

def test_probe_cli_failed_propagates_without_mark(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    make_selection_db(db, session="sess_1", provider=base.PROVIDER_ID)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: features.START_PLAN_PROVIDER_ID)
    monkeypatch.setattr(features, "start_plan_confirmed", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_probe_start_plan", lambda *a, **k: (_ for _ in ()).throw(features.FeatureError("x", "cli_failed")))
    main_calls = {"n": 0}
    def fake_execute(*a, **k):
        main_calls["n"] += 1
        return (0, '{"sessionId":"sess_1","response":"ok"}', "")
    monkeypatch.setattr(cli, "_execute", fake_execute)
    args = cli_args(tmp_path, mode="edit")
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert result["error"]["code"] == "cli_failed" and main_calls["n"] == 0
    assert not (Path(base.STATE) / features.START_PLAN_MARKER).exists()

def test_resume_start_plan(tmp_path):
    db = tmp_path / "resume.sqlite"
    make_resume_db(db, session="sess_resume", cwd=str(tmp_path), provider=features.START_PLAN_PROVIDER_ID)
    args = cli_args(tmp_path, resume="sess_resume")
    args._provider = features.START_PLAN_PROVIDER_ID
    cli._verify_resume(args, db_path=db)
    args2 = cli_args(tmp_path, resume="sess_resume")
    args2._provider = base.PROVIDER_ID
    # Mesma regra de verify_effective: sessão Start Plan retomada com o
    # provider pago seria rejeitada só depois de executar; recusar antes.
    with pytest.raises(features.FeatureError) as exc_info:
        cli._verify_resume(args2, db_path=db)
    assert exc_info.value.code == "resume_mismatch"
    args3 = cli_args(tmp_path, resume="sess_resume", model="glm-5.3", effort="high")
    args3._provider = base.PROVIDER_ID
    with pytest.raises(features.FeatureError):
        cli._verify_resume(args3, db_path=db)

def test_resume_paid_session_accepts_start_plan_request(tmp_path):
    db = tmp_path / "resume.sqlite"
    make_resume_db(db, session="sess_resume", cwd=str(tmp_path), provider=base.PROVIDER_ID)
    args = cli_args(tmp_path, resume="sess_resume")
    args._provider = features.START_PLAN_PROVIDER_ID
    # Rebaixamento silencioso Start Plan -> pago permanece tolerado, como em
    # verify_effective.
    cli._verify_resume(args, db_path=db)

def test_resume_start_plan_after_block_or_window_rejects_before_run(tmp_path, monkeypatch):
    db = tmp_path / "resume.sqlite"
    make_resume_db(db, session="sess_resume", cwd=str(tmp_path), provider=features.START_PLAN_PROVIDER_ID)
    state = tmp_path / "state"
    monkeypatch.setattr(cli.base, "STATE", str(state))
    # Marcador de bloqueio ativo (rate_limit/quota) faz provider_for resolver
    # para o pago em qualquer horário; fora da janela a resolução é a mesma.
    state.mkdir(parents=True)
    (state / features.START_PLAN_MARKER).write_text("2099-01-01T00:00:00 rate_limit\n", encoding="utf-8")
    assert features.provider_for("glm-5.3-flash") == base.PROVIDER_ID
    def boom_runner(*_args, **_kwargs):
        pytest.fail("CLI não deveria rodar com seleção divergente")
    args = cli_args(tmp_path, mode="edit", resume="sess_resume")
    result, _ = cli.run_task(args, "fatia", db_path=db, runner=boom_runner)
    assert result["error"]["code"] == "resume_mismatch"
    assert result["effectsMayExist"] is False

def test_conversation_pending_and_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    db = tmp_path / "db.sqlite"
    make_selection_db(db, session="sess_1")
    def fake_execute(args, task, **kwargs):
        return (0, '{"sessionId":"sess_1","response":"antes\\n===NEED_INPUT===\\n{\\"question\\": \\"q?\\", \\"options\\": [\\"a\\"]}"}', "")
    monkeypatch.setattr(cli, "_execute", fake_execute)
    args = cli_args(tmp_path, mode="plan", conversation=True)
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code == 2 and result["status"] == "need_input" and "pending" in result
    assert "startPlanFallback" not in result
    def fake_execute2(args, task, **kwargs):
        return (0, '{"sessionId":"sess_1","response":"ok sem pedido"}', "")
    monkeypatch.setattr(cli, "_execute", fake_execute2)
    args2 = cli_args(tmp_path, mode="plan", conversation=True)
    result2, code2 = cli.run_task(args2, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code2 == 0 and result2["status"] == "ok"


def test_next_available_unknown_or_disabled_model(tmp_path):
    s = tmp_path / "state"
    assert features.start_plan_next_available("glm-5.3", now=noon(), state_dir=s, environ={}) is None
    assert features.start_plan_next_available("GLM-5.3", now=noon(), state_dir=s, environ={}) is None
    assert features.start_plan_next_available("invalido", now=noon(), state_dir=s, environ={}) is None
    assert features.start_plan_next_available(None, now=noon(), state_dir=s, environ={}) is None
    assert features.start_plan_next_available(
        "glm-5.3-flash", now=noon(), state_dir=s, environ={"ZCODE_WORKER_START_PLAN": "0"}
    ) is None
    assert features.start_plan_next_available(
        "glm-5.3-flash", now=noon(), state_dir=s, environ={"ZCODE_WORKER_START_PLAN": "1"}
    ) == noon()


def test_next_available_inside_window_without_marker(tmp_path):
    s = tmp_path / "state"
    now = noon()
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ={}) == now


def test_next_available_before_window_returns_same_day_open(tmp_path):
    s = tmp_path / "state"
    window = {"ZCODE_WORKER_START_PLAN_HOURS": "12-22"}
    now = datetime.datetime(2026, 10, 15, 10, 0)
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ=window) == datetime.datetime(2026, 10, 15, 12, 0)


def test_next_available_after_window_returns_next_day_open(tmp_path):
    s = tmp_path / "state"
    window = {"ZCODE_WORKER_START_PLAN_HOURS": "12-22"}
    now = datetime.datetime(2026, 10, 15, 22, 30)
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ=window) == datetime.datetime(2026, 10, 16, 12, 0)


def test_next_available_busy_marker_inside_window(tmp_path):
    s = tmp_path / "state"
    s.mkdir(parents=True)
    (s / features.START_PLAN_MARKER).write_text("2026-10-15T21:50:00 rate_limit\n", encoding="utf-8")
    now = datetime.datetime(2026, 10, 15, 21, 40)
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ={}) == datetime.datetime(2026, 10, 15, 21, 50)


def test_next_available_midnight_marker_after_hours(tmp_path):
    s = tmp_path / "state"
    window = {"ZCODE_WORKER_START_PLAN_HOURS": "12-22"}
    s.mkdir(parents=True)
    (s / features.START_PLAN_MARKER).write_text("2026-10-16T00:00:00 quota\n", encoding="utf-8")
    now = datetime.datetime(2026, 10, 15, 15, 0)
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ=window) == datetime.datetime(2026, 10, 16, 12, 0)


def test_next_available_expired_marker_behaves_as_no_marker(tmp_path):
    s = tmp_path / "state"
    s.mkdir(parents=True)
    (s / features.START_PLAN_MARKER).write_text("2026-10-15T13:00:00 rate_limit\n", encoding="utf-8")
    now = datetime.datetime(2026, 10, 15, 14, 0)
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ={}) == now
    assert features.start_plan_blocked(now=now, state_dir=s) is False


def test_next_available_always_hours_returns_moment_or_until(tmp_path):
    s = tmp_path / "state"
    always = {"ZCODE_WORKER_START_PLAN_HOURS": "always"}
    now = datetime.datetime(2026, 10, 15, 3, 0)
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ=always) == now
    s.mkdir(parents=True)
    (s / features.START_PLAN_MARKER).write_text("2026-10-15T04:00:00 quota\n", encoding="utf-8")
    assert features.start_plan_next_available("glm-5.3-flash", now=now, state_dir=s, environ=always) == datetime.datetime(2026, 10, 15, 4, 0)


def test_next_available_uses_current_time_and_real_environ(tmp_path, monkeypatch):
    s = tmp_path / "state"
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN", raising=False)
    monkeypatch.delenv("ZCODE_WORKER_START_PLAN_HOURS", raising=False)
    outcome = features.start_plan_next_available("glm-5.3-flash", state_dir=s)
    assert isinstance(outcome, datetime.datetime)


def test_blocked_without_explicit_now_uses_current_time(tmp_path):
    s = tmp_path / "state"
    s.mkdir(parents=True)
    (s / features.START_PLAN_MARKER).write_text("2099-01-01T00:00:00 rate_limit\n", encoding="utf-8")
    assert features.start_plan_blocked(state_dir=s) is True


def test_quota_retry_after_prefers_start_plan_eta(monkeypatch, tmp_path):
    args = cli_args(tmp_path)
    fixed = 1760000000.0
    coming = datetime.datetime.fromtimestamp(fixed + 1200.0)
    monkeypatch.setattr(features, "start_plan_next_available", lambda *a, **k: coming)
    assert cli._quota_retry_after(args, now=fixed) == pytest.approx(fixed + 1200.0)


def test_quota_retry_after_defaults_without_start_plan(monkeypatch, tmp_path):
    args = cli_args(tmp_path)
    fixed = 1760000000.0
    monkeypatch.setattr(features, "start_plan_next_available", lambda *a, **k: None)
    assert cli._quota_retry_after(args, now=fixed) == fixed + cli.QUOTA_RETRY_DEFAULT_SECONDS


def test_quota_retry_after_ignores_past_candidate(monkeypatch, tmp_path):
    args = cli_args(tmp_path)
    fixed = 1760000000.0
    monkeypatch.setattr(
        features, "start_plan_next_available",
        lambda *a, **k: datetime.datetime.fromtimestamp(fixed - 100.0),
    )
    assert cli._quota_retry_after(args, now=fixed) == fixed + cli.QUOTA_RETRY_DEFAULT_SECONDS


def test_quota_retry_after_ignores_candidate_beyond_default(monkeypatch, tmp_path):
    args = cli_args(tmp_path)
    fixed = 1760000000.0
    monkeypatch.setattr(
        features, "start_plan_next_available",
        lambda *a, **k: datetime.datetime.fromtimestamp(fixed + 7200.0),
    )
    assert cli._quota_retry_after(args, now=fixed) == fixed + cli.QUOTA_RETRY_DEFAULT_SECONDS


def test_quota_retry_after_clamps_imminent_candidate_to_one_minute(monkeypatch, tmp_path):
    args = cli_args(tmp_path)
    fixed = 1760000000.0
    monkeypatch.setattr(
        features, "start_plan_next_available",
        lambda *a, **k: datetime.datetime.fromtimestamp(fixed + 10.0),
    )
    assert cli._quota_retry_after(args, now=fixed) == fixed + 60.0


def test_quota_retry_after_without_now_uses_wall_clock(monkeypatch, tmp_path):
    args = cli_args(tmp_path)
    monkeypatch.setattr(features, "start_plan_next_available", lambda *a, **k: None)
    before = time.time()
    assert cli._quota_retry_after(args) == pytest.approx(before + cli.QUOTA_RETRY_DEFAULT_SECONDS, abs=5.0)


def test_quota_retry_after_survives_lookup_failure(monkeypatch, tmp_path):
    args = cli_args(tmp_path)
    fixed = 1760000000.0
    def boom(*a, **k):
        raise RuntimeError("estado ilegível")
    monkeypatch.setattr(features, "start_plan_next_available", boom)
    assert cli._quota_retry_after(args, now=fixed) == fixed + cli.QUOTA_RETRY_DEFAULT_SECONDS


def test_run_task_paid_quota_carries_retry_after(monkeypatch, tmp_path):
    monkeypatch.setenv("ZCODE_WORKER_START_PLAN", "0")
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    def quota_runner(*a, **k):
        raise features.FeatureError("cota do ZCode esgotada", "quota")
    monkeypatch.setattr(cli, "_execute", quota_runner)
    before = time.time()
    result, code = cli.run_task(cli_args(tmp_path, mode="plan"), "fatia")
    assert code == 75
    assert result["error"]["code"] == "quota"
    assert isinstance(result["retryAfter"], float)
    assert result["retryAfter"] == pytest.approx(before + cli.QUOTA_RETRY_DEFAULT_SECONDS, abs=5.0)


def test_run_task_quota_before_normalization_uses_default(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise features.FeatureError("cota do ZCode esgotada", "quota")
    monkeypatch.setattr(features, "public_namespace", boom)
    before = time.time()
    result, code = cli.run_task(cli_args(tmp_path, mode="plan"), "fatia")
    assert code == 75
    assert result["retryAfter"] == pytest.approx(before + cli.QUOTA_RETRY_DEFAULT_SECONDS, abs=5.0)


def test_main_quota_writes_reset_line(monkeypatch, tmp_path, capsys):
    retry_after = 1790000000.0
    monkeypatch.setattr(cli, "run_task", lambda *a, **k: ({"status": "error", "exitCode": 75, "error": {"code": "quota", "message": "cota"}, "retryAfter": retry_after}, 75))
    code = cli.main(["--json", "--cwd", str(tmp_path), "tarefa"])
    assert code == 75
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if "worker-ask-quota-reset" in line]
    assert lines == [f"worker-ask-quota-reset: {int(retry_after)}"]
    match = re.fullmatch(r"worker-ask-quota-reset: (\d+)", lines[0])
    assert match is not None and int(match.group(1)) == int(retry_after)


def test_main_quota_without_retry_after_writes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "run_task", lambda *a, **k: ({"status": "error", "exitCode": 75, "error": {"code": "quota", "message": "cota"}}, 75))
    assert cli.main(["--json", "--cwd", str(tmp_path), "tarefa"]) == 75
    assert "worker-ask-quota-reset" not in capsys.readouterr().err
