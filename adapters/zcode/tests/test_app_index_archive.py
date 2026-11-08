"""Índice do app desktop: tarefas ``[WORKER...]`` viram archived (best-effort)."""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from zcode_worker import base, cli, features


@pytest.fixture(autouse=True)
def _app_index_isolated(tmp_path, monkeypatch):
    """Nenhum teste pode tocar o ``~/.zcode/v2/tasks-index.sqlite`` real."""
    monkeypatch.setattr(cli, "APP_TASKS_INDEX", str(tmp_path / "tasks-index.sqlite"))


def make_index(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "create table tasks("
        "task_id text, title text, archived integer, deleted integer, updated_at integer)"
    )
    connection.executemany("insert into tasks values (?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


WORKER_TITLE = "[WORKER — chamado via zcode-worker] tarefa de exemplo"


def test_archive_only_unarchived_worker_rows(tmp_path):
    index = tmp_path / "tasks-index.sqlite"
    make_index(index, [
        ("sess_w1", WORKER_TITLE, 0, 0, 1000),
        ("sess_w2", "[WORKER — chamado via zcode-worker] já arquivada", 1, 0, 2000),
        ("sess_u1", "conversa comum", 0, 0, 3000),
    ])
    before = int(time.time() * 1000)
    assert cli._archive_app_worker_tasks(index_path=str(index)) == 1
    connection = sqlite3.connect(index)
    rows = connection.execute(
        "select task_id, archived, updated_at from tasks order by task_id"
    ).fetchall()
    connection.close()
    # Conversa comum intacta; só a worker pendente vira archived=1 com updated_at novo.
    assert rows[0] == ("sess_u1", 0, 3000)
    assert rows[1][0] == "sess_w1" and rows[1][1] == 1
    assert rows[1][2] >= before
    # Já arquivada não conta e não tem updated_at reescrito.
    assert rows[2] == ("sess_w2", 1, 2000)
    assert cli._archive_app_worker_tasks(index_path=str(index)) == 0


def test_missing_index_file_returns_zero(tmp_path):
    assert cli._archive_app_worker_tasks(index_path=str(tmp_path / "ausente.sqlite")) == 0


def test_schema_without_archived_column_returns_zero(tmp_path):
    index = tmp_path / "tasks-index.sqlite"
    connection = sqlite3.connect(index)
    connection.execute(
        "create table tasks(task_id text, title text, deleted integer, updated_at integer)"
    )
    connection.commit()
    connection.close()
    assert cli._archive_app_worker_tasks(index_path=str(index)) == 0


def test_corrupted_index_returns_zero(tmp_path):
    index = tmp_path / "tasks-index.sqlite"
    index.write_bytes(b"lixo-nao-sqlite" * 64)
    assert cli._archive_app_worker_tasks(index_path=str(index)) == 0


def test_default_index_path_respected(tmp_path, monkeypatch):
    # Com index_path=None o helper usa a constante do módulo (onde
    # ZCODE_WORKER_APP_TASKS_INDEX é lido na importação); sobrescrever o
    # atributo cobre o caminho padrão sem tocar o índice real.
    index = tmp_path / "outro-index.sqlite"
    make_index(index, [("sess_w9", WORKER_TITLE, 0, 0, 1)])
    monkeypatch.setattr(cli, "APP_TASKS_INDEX", str(index))
    assert cli._archive_app_worker_tasks() == 1


def cli_args(tmp_path, **overrides):
    values = {
        "tarefa": None, "cwd": str(tmp_path), "mode": "plan", "spec": None,
        "timeout": 2.0, "queue_timeout": 2.0, "total_timeout": None,
        "model": "glm-5.3-flash", "effort": "high", "profile": None,
        "resume": None, "attach": [], "disallowed_tools": [],
        "max_output_bytes": None, "json": False, "dry_run": False,
        "doctor": False, "list_models": False, "saida": None, "status": None,
        "cancel": None, "_job_child": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_run_task_calls_archive_helper(tmp_path, monkeypatch):
    """Caminho real de run_task (modelo de _run_start_rate_limit): expurgo
    inicial e fim ok da fatia chamam o helper."""
    db = tmp_path / "db.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        "create table if not exists session_entry"
        "(session_id text, type text, time_created int, data text)"
    )
    connection.execute(
        "insert into session_entry values (?, ?, ?, ?)",
        (
            "sess_1", "runtime/model_selection", 1,
            json.dumps({"modelSelection": {
                "providerId": base.PROVIDER_ID,
                "modelId": "GLM-5.3-Flash",
                "options": {"reasoningLevel": "high"},
            }}),
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(cli.base, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(features, "provider_for", lambda *a, **k: base.PROVIDER_ID)
    monkeypatch.setattr(features, "start_plan_confirmed", lambda *a, **k: True)
    monkeypatch.setattr(
        cli, "_execute",
        lambda *a, **k: (0, '{"sessionId":"sess_1","response":"ok"}', ""),
    )
    calls = {"n": 0}

    def _counting(**kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(cli, "_archive_app_worker_tasks", _counting)
    args = cli_args(tmp_path, mode="plan")
    result, code = cli.run_task(args, "fatia", db_path=db, runner=lambda *a, **k: (0, "", ""))
    assert code == 0 and result["status"] == "ok"
    assert calls["n"] == 2
