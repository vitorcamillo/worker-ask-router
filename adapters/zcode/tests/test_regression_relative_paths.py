"""Regressão 17/10/2026: `--cwd .` + `--saida` morria como "falha interna segura"."""

import types

from zcode_worker import cli, features


def test_relative_paths_become_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.md").write_text("x", encoding="utf-8")
    parsed = features.parse_args(["--cwd", ".", "--saida", "out.md", "--mode", "plan", "t"])
    assert parsed.cwd == str(tmp_path)
    assert parsed.saida == str(tmp_path / "out.md")
    parsed = features.parse_args(["--cwd", ".", "--spec", "s.md", "--attach", "s.md"])
    assert parsed.spec == str(tmp_path / "s.md")
    assert parsed.attach == [str(tmp_path / "s.md")]


def test_background_waits_longer_in_queue_than_foreground():
    assert features.parse_args(["--cwd", "/tmp", "--saida", "/tmp/o.md", "t"]).queue_timeout == 1800.0
    assert features.parse_args(["--cwd", "/tmp", "t"]).queue_timeout == 30.0
    assert features.parse_args(["--cwd", "/tmp", "--saida", "/tmp/o", "--queue-timeout", "5", "t"]).queue_timeout == 5.0


def test_core_worker_error_message_is_surfaced(monkeypatch):
    class WorkerError(Exception):
        pass

    class QuotaError(WorkerError):
        pass

    fake = types.SimpleNamespace(WorkerError=WorkerError, QuotaError=QuotaError, DeadlineExpired=TimeoutError)
    monkeypatch.setattr(cli, "base", fake)
    result, code = cli._error_result(WorkerError("cwd inválido"), 0.0)
    assert (code, result["error"]["message"], result["error"]["code"]) == (1, "cwd inválido", "worker")
    result, code = cli._error_result(QuotaError("cota do ZCode esgotada"), 0.0)
    assert code == 75 and result["error"]["code"] == "quota"
    result, code = cli._error_result(RuntimeError("segredo bruto"), 0.0)
    assert result["error"]["message"] == "falha interna segura"
