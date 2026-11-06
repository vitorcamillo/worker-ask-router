"""Sessões [WORKER...] são apagadas: sucesso, erro e varredura de antigas."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from types import SimpleNamespace

from zcode_worker import base, cli, features


def feature_args(**overrides):
    values = {
        "tarefa": None,
        "cwd": os.getcwd(),
        "mode": "plan",
        "spec": None,
        "timeout": 2.0,
        "queue_timeout": 2.0,
        "total_timeout": None,
        "model": "glm-5.3-flash",
        "effort": "high",
        "profile": None,
        "resume": None,
        "attach": [],
        "disallowed_tools": [],
        "max_output_bytes": None,
        "json": False,
        "dry_run": False,
        "doctor": False,
        "list_models": False,
        "saida": None,
        "status": None,
        "cancel": None,
        "_job_child": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_db(path, *, sessions=(), messages=(), selections=()):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "create table session(id text primary key, parent_id text, title text,"
        " time_updated integer, time_archived integer)"
    )
    connection.execute(
        "create table message(id text primary key,"
        " session_id text references session(id) on delete cascade)"
    )
    connection.execute(
        "create table session_entry(session_id text, type text,"
        " time_created int, data text)"
    )
    for sid, parent, title, updated in sessions:
        connection.execute(
            "insert into session values (?, ?, ?, ?, ?)",
            (sid, parent, title, updated, None),
        )
    for mid, sid in messages:
        connection.execute("insert into message values (?, ?)", (mid, sid))
    for sid, provider in selections:
        connection.execute(
            "insert into session_entry values (?, ?, ?, ?)",
            (
                sid,
                "runtime/model_selection",
                1,
                json.dumps(
                    {
                        "modelSelection": {
                            "providerId": provider,
                            "modelId": "GLM-5.3-Flash",
                            "options": {"reasoningLevel": "high"},
                        }
                    }
                ),
            ),
        )
    connection.commit()
    connection.close()


def session_ids(path):
    connection = sqlite3.connect(path)
    try:
        return sorted(row[0] for row in connection.execute("select id from session"))
    finally:
        connection.close()


def message_ids(path):
    connection = sqlite3.connect(path)
    try:
        return sorted(row[0] for row in connection.execute("select id from message"))
    finally:
        connection.close()


def now_ms():
    return int(time.time() * 1000)


def test_delete_session_removes_worker_cascade_and_child(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[
            ("sess_pai", None, "[WORKER] fatia", now_ms()),
            ("sess_filho", "sess_pai", "filho retomado", now_ms()),
        ],
        messages=[("m1", "sess_pai"), ("m2", "sess_filho")],
    )
    assert cli._delete_session("sess_pai", db_path=db) is True
    assert session_ids(db) == []
    assert message_ids(db) == []


def test_delete_session_keeps_non_worker(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[("sess_user", None, "conversa do usuario", now_ms())],
        messages=[("m1", "sess_user")],
    )
    assert cli._delete_session("sess_user", db_path=db) is False
    assert session_ids(db) == ["sess_user"]
    assert message_ids(db) == ["m1"]


def test_delete_session_rejects_invalid_id_and_missing_db(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db, sessions=[("sess_ok", None, "[WORKER] fatia", now_ms())])
    assert cli._delete_session("id-invalido!", db_path=db) is False
    assert cli._delete_session(None, db_path=db) is False
    assert cli._delete_session("sess_ok", db_path=tmp_path / "ausente.sqlite") is False
    assert session_ids(db) == ["sess_ok"]


def test_purge_removes_only_stale_workers_and_children(tmp_path):
    old = now_ms() - 7 * 3600 * 1000
    recent = now_ms()
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[
            ("sess_velha", None, "[WORKER] antiga", old),
            ("sess_filha", "sess_velha", "filha recente", recent),
            ("sess_nova", None, "[WORKER] recente", recent),
            ("sess_outra", None, "interativa antiga", old),
        ],
        messages=[("m1", "sess_velha"), ("m2", "sess_filha"), ("m3", "sess_nova")],
    )
    assert cli._purge_stale_worker_sessions(db_path=db) == 1
    assert session_ids(db) == ["sess_nova", "sess_outra"]
    assert message_ids(db) == ["m3"]


def test_purge_noop_and_errors_return_zero(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db, sessions=[("sess_nova", None, "[WORKER] recente", now_ms())])
    assert cli._purge_stale_worker_sessions(db_path=db) == 0
    assert cli._purge_stale_worker_sessions(db_path=tmp_path / "ausente.sqlite") == 0
    assert cli._purge_stale_worker_sessions(db_path=db, older_than_seconds="x") == 0
    assert session_ids(db) == ["sess_nova"]
    empty = tmp_path / "vazio.sqlite"
    sqlite3.connect(empty).close()
    assert cli._purge_stale_worker_sessions(db_path=empty) == 0


def test_purge_preserves_pending_turn_and_keep_ids(tmp_path):
    old = now_ms() - 7 * 3600 * 1000
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[
            ("sess_velha", None, "[WORKER] antiga", old),
            ("sess_pendente", None, "[WORKER] aguarda resposta", old),
            ("sess_keep", None, "[WORKER] alvo de resume", old),
        ],
        messages=[("m1", "sess_velha"), ("m2", "sess_pendente"), ("m3", "sess_keep")],
    )
    connection = sqlite3.connect(db)
    connection.execute("alter table message add column data text")
    connection.execute(
        "update message set data=? where id='m2'",
        ("pergunta\n" + features.TURN_MARKER + '\n{"question": "..."}',),
    )
    connection.commit()
    connection.close()
    purged = cli._purge_stale_worker_sessions(db_path=db, keep=(None, "sess_keep"))
    assert purged == 1
    assert session_ids(db) == ["sess_keep", "sess_pendente"]
    assert message_ids(db) == ["m2", "m3"]


def test_purge_keeps_keep_child_and_its_expired_parent(tmp_path):
    old = now_ms() - 7 * 3600 * 1000
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[
            ("sess_pai", None, "[WORKER] antiga", old),
            ("sess_keep", "sess_pai", "[WORKER] alvo de resume", old),
            ("sess_solta", None, "[WORKER] antiga sem filha protegida", old),
        ],
        messages=[("m1", "sess_pai"), ("m2", "sess_keep"), ("m3", "sess_solta")],
    )
    purged = cli._purge_stale_worker_sessions(db_path=db, keep=("sess_keep",))
    assert purged == 1
    assert session_ids(db) == ["sess_keep", "sess_pai"]
    assert message_ids(db) == ["m1", "m2"]


def test_purge_keeps_pending_child_and_its_expired_parent(tmp_path):
    old = now_ms() - 7 * 3600 * 1000
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[
            ("sess_pai", None, "[WORKER] antiga", old),
            ("sess_pendente", "sess_pai", "[WORKER] aguarda resposta", old),
        ],
        messages=[("m1", "sess_pai"), ("m2", "sess_pendente")],
    )
    connection = sqlite3.connect(db)
    connection.execute("alter table message add column data text")
    connection.execute(
        "update message set data=? where id='m2'",
        ("pergunta\n" + features.TURN_MARKER + '\n{"question": "..."}',),
    )
    connection.commit()
    connection.close()
    assert cli._purge_stale_worker_sessions(db_path=db) == 0
    assert session_ids(db) == ["sess_pai", "sess_pendente"]
    assert message_ids(db) == ["m1", "m2"]


def _run_error_case(tmp_path, monkeypatch, *, raw, session_rows):
    db = tmp_path / "db.sqlite"
    make_db(db, sessions=session_rows)
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli.base, "global_slot", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(
        cli.features,
        "temporary_provider",
        lambda *a, **k: contextlib.nullcontext(str(tmp_path / "provider-fake.json")),
    )
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    args = feature_args(cwd=str(tmp_path))
    # Caminho real de _execute: o dublê é o runner, não o _execute.
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: raw)
    return result, code, db


def test_run_task_returncode_nonzero_deletes_session_and_keeps_error(tmp_path, monkeypatch):
    stdout = json.dumps({"sessionId": "sess_err", "response": "parcial"})
    result, code, db = _run_error_case(
        tmp_path,
        monkeypatch,
        raw=(1, stdout, "boom"),
        session_rows=[("sess_err", None, "[WORKER] falha", now_ms())],
    )
    assert code == 1
    assert result["error"]["code"] == "cli_failed"
    assert session_ids(db) == []


def test_run_task_real_runner_rc_nonzero_deletes_session(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[("sess_rc1", None, "[WORKER] falha real", now_ms())],
        messages=[("m1", "sess_rc1")],
    )
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli.base, "global_slot", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(
        cli.features,
        "temporary_provider",
        lambda *a, **k: contextlib.nullcontext(str(tmp_path / "provider-fake.json")),
    )
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    stdout = json.dumps({"sessionId": "sess_rc1", "response": "parcial"})
    commands = []

    def runner(command, env, deadline, limit):
        commands.append(command)
        # stderr sem padrão de retry/quota: classificação é cli_failed, sem nova tentativa.
        return (1, stdout, "boom")

    args = feature_args(cwd=str(tmp_path))
    result, code = cli.run_task(args, "fatia", db_path=db, runner=runner)
    assert code == 1
    assert result["error"]["code"] == "cli_failed"
    assert commands and commands[0][0] == base.CLI
    assert session_ids(db) == []
    assert message_ids(db) == []


def test_run_task_invalid_payload_deletes_session_and_keeps_error(tmp_path, monkeypatch):
    stdout = json.dumps({"sessionId": "sess_ruim"})
    result, code, db = _run_error_case(
        tmp_path,
        monkeypatch,
        raw=(0, stdout, ""),
        session_rows=[("sess_ruim", None, "[WORKER] invalida", now_ms())],
    )
    assert code == 1
    assert result["error"]["code"] in ("invalid_request", "worker", "internal")
    assert session_ids(db) == []


def test_run_task_success_deletes_worker_session(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[("sess_1", None, "[WORKER] fatia", now_ms())],
        selections=[("sess_1", base.PROVIDER_ID)],
    )
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    monkeypatch.setattr(
        cli,
        "_execute",
        lambda *a, **k: (0, json.dumps({"sessionId": "sess_1", "response": "ok"}), ""),
    )
    args = feature_args(cwd=str(tmp_path))
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code == 0 and result["status"] == "ok"
    assert session_ids(db) == []


def test_run_task_dry_run_keeps_stale_sessions(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[("sess_velha", None, "[WORKER] antiga", now_ms() - 7 * 3600 * 1000)],
    )
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    args = feature_args(cwd=str(tmp_path), dry_run=True)
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code == 0 and result["status"] == "dry-run"
    assert session_ids(db) == ["sess_velha"]


def test_run_task_resume_target_survives_purge(tmp_path, monkeypatch):
    old = now_ms() - 7 * 3600 * 1000
    db = tmp_path / "db.sqlite"
    make_db(
        db,
        sessions=[
            ("sess_velha", None, "[WORKER] antiga", old),
            ("sess_alvo", None, "[WORKER] alvo de resume", old),
        ],
        messages=[("m1", "sess_velha"), ("m2", "sess_alvo")],
    )
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    monkeypatch.setattr(cli, "_execute", lambda *a, **k: (1, "", "boom"))
    args = feature_args(cwd=str(tmp_path), resume="sess_alvo")
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code == 1 and result["error"]["code"] == "cli_failed"
    assert session_ids(db) == ["sess_alvo"]
    assert message_ids(db) == ["m2"]


def test_delete_session_from_stdout_without_json_returns_false(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db, sessions=[("sess_1", None, "[WORKER] fatia", now_ms())])
    assert cli._delete_session_from_stdout("saida sem json", db_path=db) is False
    assert session_ids(db) == ["sess_1"]
