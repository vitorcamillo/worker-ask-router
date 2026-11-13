"""Tabela de disponibilidade: testes determinísticos, sem rede nem processo real."""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import availability as av
import quota_provider


def _valida(now):
    """Tabela mínima válida para os testes de load."""
    return {
        "schema": 1,
        "generated_at": now,
        "workers": {"zcode-worker": {"checked": True, "blocked_until": now + 10}},
    }


def _grava(path, payload):
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    os.chmod(path, 0o600)


def test_refresh_grava_0600_e_blocked_until(tmp_path, monkeypatch):
    now = 1_000_000.0

    def fake_reset(wrapper, now=None, budget=10.0):
        return now + 600 if wrapper == "zcode-worker" else None

    monkeypatch.setattr(quota_provider, "query_reset", fake_reset)
    table = av.refresh(tmp_path, now=now, budget=12.0)
    assert table["schema"] == 1
    assert table["workers"]["zcode-worker"] == {
        "checked": True,
        "blocked_until": now + 600,
    }
    assert table["workers"]["muse-ask"] == {"checked": True, "blocked_until": None}
    path = tmp_path / av.TABLE_NAME
    assert path.stat().st_mode & 0o777 == 0o600


def test_refresh_excecao_marcada_e_continua(tmp_path, monkeypatch):
    def fake_reset(wrapper, now=None, budget=10.0):
        if wrapper == "muse-ask":
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(quota_provider, "query_reset", fake_reset)
    table = av.refresh(tmp_path, now=1_000.0)
    assert table["workers"]["muse-ask"] == {"checked": False, "blocked_until": None}
    assert table["workers"]["zcode-worker"]["checked"] is True


def test_refresh_budget_zero_nao_consulta(tmp_path, monkeypatch):
    chamadas = []

    def fake_reset(*args, **kwargs):
        chamadas.append(args)
        return None

    monkeypatch.setattr(quota_provider, "query_reset", fake_reset)
    table = av.refresh(tmp_path, now=1.0, budget=0)
    assert chamadas == []
    assert all(not entry["checked"] for entry in table["workers"].values())


def test_refresh_recusa_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(quota_provider, "query_reset", lambda *a, **k: None)
    alvo = tmp_path / "alvo"
    alvo.mkdir()
    link = tmp_path / "link"
    link.symlink_to(alvo)
    with pytest.raises(RuntimeError):
        av.refresh(link)
    with pytest.raises(RuntimeError):
        av.refresh(tmp_path / "ausente")
    destino = tmp_path / "real.json"
    _grava(destino, {"schema": 1})
    os.symlink(destino, alvo / av.TABLE_NAME)
    with pytest.raises(RuntimeError):
        av.refresh(alvo)
    assert os.path.islink(alvo / av.TABLE_NAME)


def test_load_invalido_devolve_none(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ASK_AVAIL_MAX_AGE", raising=False)
    now = time.time()
    path = tmp_path / av.TABLE_NAME
    assert av.load(tmp_path, now=now) is None  # ausente
    _grava(path, "{invalido")
    assert av.load(tmp_path, now=now) is None  # JSON inválido
    ruim = _valida(now)
    ruim["schema"] = 2
    _grava(path, ruim)
    assert av.load(tmp_path, now=now) is None  # schema errado
    _grava(path, _valida(now - av.max_age() - 1))
    assert av.load(tmp_path, now=now) is None  # velho demais
    _grava(path, _valida(True))
    assert av.load(tmp_path, now=now) is None  # generated_at booleano
    _grava(path, _valida(now + 10))
    assert av.load(tmp_path, now=now) is None  # no futuro além de 5 s
    _grava(path, json.dumps(_valida(now)) + " " * (av.MAX_TABLE_BYTES + 1))
    assert av.load(tmp_path, now=now) is None  # maior que 64 KiB
    real = tmp_path / "real.json"
    _grava(real, _valida(now))
    path.unlink()
    os.symlink(real, path)
    assert av.load(tmp_path, now=now) is None  # symlink


def test_load_e_blocked_until(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ASK_AVAIL_MAX_AGE", raising=False)
    now = time.time()
    _grava(tmp_path / av.TABLE_NAME, _valida(now))
    table = av.load(tmp_path, now=now)
    assert table is not None
    assert table["workers"]["zcode-worker"]["blocked_until"] == now + 10
    assert av.blocked_until(table, "zcode-worker", now=now) == now + 10
    passado = {
        "schema": 1,
        "generated_at": now,
        "workers": {"zcode-worker": {"checked": True, "blocked_until": now - 10}},
    }
    assert av.blocked_until(passado, "zcode-worker", now=now) is None
    assert av.blocked_until(None, "zcode-worker", now=now) is None


def test_max_age_env_valida_e_invalida(monkeypatch):
    monkeypatch.delenv("WORKER_ASK_AVAIL_MAX_AGE", raising=False)
    assert av.max_age() == av.DEFAULT_MAX_AGE
    monkeypatch.setenv("WORKER_ASK_AVAIL_MAX_AGE", "45")
    assert av.max_age() == 45.0
    for invalido in ("abc", "-1", "inf"):
        monkeypatch.setenv("WORKER_ASK_AVAIL_MAX_AGE", invalido)
        assert av.max_age() == av.DEFAULT_MAX_AGE


def test_slots_opencode(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ASK_SLOT_PROBE", raising=False)
    monkeypatch.delenv("OPENCODE_ASK_SLOTS", raising=False)
    monkeypatch.setenv("WORKER_ASK_OPENCODE_VAGA_DIR", str(tmp_path))
    assert av.slots("opencode-ask") == {"active": 0, "max": 2, "free": True}
    vaga = tmp_path / f"opencode-ask-{os.getuid()}.vaga1"
    fd = os.open(vaga, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert av.slots("opencode-ask") == {"active": 1, "max": 2, "free": True}
        monkeypatch.setenv("OPENCODE_ASK_SLOTS", "1")
        assert av.slots("opencode-ask") == {"active": 1, "max": 1, "free": False}
    finally:
        os.close(fd)
    assert av.slots("opencode-ask") == {"active": 0, "max": 1, "free": True}


def test_slots_zcode(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ASK_SLOT_PROBE", raising=False)
    import zcode_worker.base as base

    monkeypatch.setattr(base, "_read_settings", lambda: 3)
    monkeypatch.setattr(base, "count_active_cli_processes", lambda *a, **k: 3)
    monkeypatch.setattr(
        base, "_read_mem_available_bytes", lambda *a, **k: 8 * 1024 * 1024 * 1024
    )
    assert av.slots("zcode-worker") == {
        "active": 3,
        "max": 3,
        "mem_ok": True,
        "free": False,
    }

    def quebra(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(base, "count_active_cli_processes", quebra)
    assert av.slots("zcode-worker") is None


def test_slots_probe_desligada_e_demais_nomes(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_ASK_SLOT_PROBE", "0")
    assert av.slots("zcode-worker") is None
    assert av.slots("opencode-ask") is None
    monkeypatch.delenv("WORKER_ASK_SLOT_PROBE", raising=False)
    assert av.slots("muse-ask") is None
    assert av.slots("codex-ask") is None


def test_describe_precedencia(monkeypatch):
    monkeypatch.delenv("WORKER_ASK_SLOT_PROBE", raising=False)
    now = time.time()
    table = {
        "schema": 1,
        "generated_at": now,
        "workers": {
            "zcode-worker": {"checked": True, "blocked_until": now + 600},
            "muse-ask": {"checked": True, "blocked_until": now + 600},
            "opencode-ask": {"checked": True, "blocked_until": None},
            "codex-ask": {"checked": True, "blocked_until": None},
            "claude-ask": {"checked": True, "blocked_until": None},
        },
    }
    sonda = {"active": 1, "max": 2, "free": False}

    def fake_slots(nome):
        return sonda if nome in {"muse-ask", "opencode-ask"} else None

    monkeypatch.setattr(av, "slots", fake_slots)
    cache = {"zcode-worker": {"until": now + 900, "blocked_reason": "rate"}}
    status = av.describe(table, cache, now=now)
    assert status["zcode-worker"]["reason"] == "cooldown"  # cache vence a tabela
    assert status["zcode-worker"]["until"] == now + 900
    assert status["muse-ask"]["reason"] == "cota"  # cota vence ocupado
    assert status["muse-ask"]["until"] == now + 600
    assert status["opencode-ask"]["reason"] == "ocupado"
    assert status["codex-ask"]["reason"] == "ok"
    assert status["codex-ask"]["available"] is True
    assert status["claude-ask"]["reason"] == "ok"
    assert status["claude-ask"]["until"] is None


def test_reserve_atomica_e_release(tmp_path, monkeypatch):
    monkeypatch.setattr(av, "slots", lambda nome: {"active": 0, "max": 2, "free": True})
    a = av.reserve("zcode-worker", tmp_path)
    b = av.reserve("zcode-worker", tmp_path)
    assert isinstance(a, int) and isinstance(b, int) and a != b
    assert av.reserve("zcode-worker", tmp_path) is False  # as duas vagas reservadas
    av.release(a)
    c = av.reserve("zcode-worker", tmp_path)
    assert isinstance(c, int) and not isinstance(c, bool)
    av.release(b)
    av.release(c)
    av.release(None)
    av.release(False)


def test_reserve_sem_vaga_sem_sonda_e_erro(tmp_path, monkeypatch):
    monkeypatch.setattr(av, "slots", lambda nome: {"active": 2, "max": 2, "free": False})
    assert av.reserve("zcode-worker", tmp_path) is False
    monkeypatch.setattr(av, "slots", lambda nome: None)
    assert av.reserve("muse-ask", tmp_path) is None
    monkeypatch.setattr(av, "slots", lambda nome: {"active": 0, "max": 1, "free": True})
    assert av.reserve("zcode-worker", tmp_path / "ausente") is None  # diretório inexistente
