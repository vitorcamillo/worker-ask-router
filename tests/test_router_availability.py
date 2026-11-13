# ruff: noqa: F811  (fixture cov_env importada de test_router_main_cov)
"""Roteador lendo a tabela de disponibilidade (availability.py) em vez de consultar cota.

Mesmo padrao de test_router_main_cov.py: wr._main_impl in-process com wrappers falsos
no PATH e HOME/XDG_STATE_HOME isolados; helpers importados desse modulo. Sem rede e
sem workers reais; a sonda de slots vem de monkeypatch (conftest desliga a real).
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import availability  # noqa: E402
import quota_provider  # noqa: E402
import worker_router as wr  # noqa: E402

# Fixture e helpers vivem no modulo de cobertura existente (mesmo padrao de fakes).
from test_router_main_cov import (  # noqa: E402, F401
    FAKE_NAMES,
    base_args,
    cov_env,
    records,
    run_main,
)


def _state_dir(env):
    return env["state"] / "worker-ask"


def _grava_tabela(env, tabela):
    sd = _state_dir(env)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / availability.TABLE_NAME).write_text(json.dumps(tabela))


def _refresh(cov_env, monkeypatch, blocked=None):
    """Grava a tabela via availability.refresh com query_reset falsificado."""
    def fake_query(name, now=None, budget=12.0):
        if blocked and name in blocked:
            return blocked[name]
        return None
    monkeypatch.setattr(quota_provider, "query_reset", fake_query)
    sd = _state_dir(cov_env)
    sd.mkdir(parents=True, exist_ok=True)
    return availability.refresh(sd)


# 1. Tabela fresca com zcode-worker bloqueado: pula o zcode sem consultar cota.
def test_tabela_fresca_pula_zcode_sem_consultar_cota(cov_env, monkeypatch):
    reset = time.time() + 600
    _refresh(cov_env, monkeypatch, blocked={"zcode-worker": reset})
    chamadas = []
    def proibido(*args, **kwargs):
        chamadas.append(args)
        raise AssertionError("consulta de cota na chamada com tabela fresca")
    monkeypatch.setattr(quota_provider, "query_reset", proibido)
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert [r["name"] for r in records(cov_env)] == ["opencode-ask"]
    assert chamadas == []


# 2. Sonda de vagas: zcode ocupado e pulado sem ser invocado.
def test_slots_ocupado_pula_zcode(cov_env, monkeypatch, capsys):
    monkeypatch.setattr(quota_provider, "query_reset", lambda *a, **k: None)
    monkeypatch.setattr(availability, "slots",
                        lambda name: {"free": False} if name == "zcode-worker" else None)
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert "ocupado" in capsys.readouterr().err
    nomes = [r["name"] for r in records(cov_env)]
    assert nomes == ["opencode-ask"]


# 3. Todos ocupados: rc 75 imediato com o nome de cada worker da cadeia.
def test_todos_ocupados_rc75_com_motivos(cov_env, monkeypatch, capsys):
    monkeypatch.setattr(quota_provider, "query_reset", lambda *a, **k: None)
    monkeypatch.setattr(availability, "slots", lambda name: {"free": False})
    argv = base_args(cov_env)
    assert run_main(argv) == 75
    assert records(cov_env) == []
    err = capsys.readouterr().err
    assert "nenhum worker disponivel agora" in err
    for name in FAKE_NAMES:
        assert name in err


# 4. Tabela existente porem vencida: aviso e contingencia consulta cota.
def test_tabela_vencida_avisa_e_consulta_cota(cov_env, monkeypatch, capsys):
    tabela = {"schema": availability.SCHEMA, "generated_at": time.time() - 10_000,
              "workers": {name: {"checked": True, "blocked_until": None}
                          for name in availability.WORKERS}}
    _grava_tabela(cov_env, tabela)
    chamadas = []
    def contando(name, now=None, budget=12.0):
        chamadas.append(name)
        return None
    monkeypatch.setattr(quota_provider, "query_reset", contando)
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert "tabela de disponibilidade vencida ou invalida" in capsys.readouterr().err
    assert chamadas


# 5. Tabela ausente: sem aviso; comportamento igual ao de antes.
def test_tabela_ausente_sem_aviso(cov_env, monkeypatch, capsys):
    monkeypatch.setattr(quota_provider, "query_reset", lambda *a, **k: None)
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    assert "tabela de disponibilidade" not in capsys.readouterr().err
    assert [r["name"] for r in records(cov_env)] == ["zcode-worker"]


# 6. --refresh-availability: rc 0, tabela criada, sem exigir --orchestrator.
def test_refresh_availability_cria_tabela(cov_env, monkeypatch):
    def fake_query(name, now=None, budget=12.0):
        return None
    monkeypatch.setattr(quota_provider, "query_reset", fake_query)
    assert run_main(["--refresh-availability"]) == 0
    tabela = availability.load(_state_dir(cov_env))
    assert tabela is not None
    assert set(tabela["workers"]) == set(availability.WORKERS)


def test_refresh_availability_falha_sai_1(cov_env, monkeypatch, capsys):
    def quebra(state_dir, *a, **k):
        raise RuntimeError("falha de I/O ao gravar tabela")
    monkeypatch.setattr(availability, "refresh", quebra)
    assert run_main(["--refresh-availability"]) == 1
    assert "falha de I/O ao gravar tabela" in capsys.readouterr().err


# 7. --status inclui availability (cinco workers) e availability_age numerico/null.
def test_status_availability_apos_refresh(cov_env, monkeypatch, capsys):
    monkeypatch.setattr(availability, "slots", lambda name: None)
    _refresh(cov_env, monkeypatch)
    assert run_main(["--status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data["availability"]) == set(availability.WORKERS)
    assert isinstance(data["availability_age"], float)
    assert data["availability_age"] >= 0.0


def test_status_sem_tabela_availability_age_null(cov_env, capsys):
    assert run_main(["--status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["availability_age"] is None
    assert set(data["availability"]) == set(availability.WORKERS)


# 8. Fila de admissao cheia no zcode: fallback com reason "fila", sem cooldown.
def test_rc75_zcode_fila_sem_cooldown(cov_env, monkeypatch):
    monkeypatch.setattr(quota_provider, "query_reset", lambda *a, **k: None)
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "75")
    monkeypatch.setenv("FAKE_STDERR_ZCODE_WORKER",
                       "ERRO: fila de admissao cheia: nenhum worker iniciado")
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    fallbacks = [a for a in wr._RUN["attempts"] if a.get("event") == "fallback"]
    assert fallbacks and fallbacks[-1]["reason"] == "fila"
    assert wr.load_cache() == {}
    assert [r["name"] for r in records(cov_env)] == ["zcode-worker", "opencode-ask"]


# 9. rc75 do opencode-ask com vagas ocupadas: sem cooldown para opencode-ask.
def test_rc75_opencode_vagas_ocupadas_sem_cooldown(cov_env, monkeypatch):
    monkeypatch.setattr(quota_provider, "query_reset", lambda *a, **k: None)
    monkeypatch.setenv("FAKE_RC_ZCODE_WORKER", "75")
    monkeypatch.setenv("FAKE_STDERR_ZCODE_WORKER",
                       "ERRO: fila de admissao cheia: nenhum worker iniciado")
    monkeypatch.setenv("FAKE_RC_OPENCODE_ASK", "75")
    monkeypatch.setenv("FAKE_STDERR_OPENCODE_ASK",
                       "opencode-ask: vagas ocupadas ha mais de 20s; tente depois")
    argv = base_args(cov_env, "--no-muse")
    assert run_main(argv) == 0
    cache = wr.load_cache()
    assert "opencode-ask" not in cache
    assert cache == {}
    assert [r["name"] for r in records(cov_env)] == \
        ["zcode-worker", "opencode-ask", "codex-ask"]


# 10. run_wrapper define OPENCODE_ASK_WAIT=20 e preserva valor existente.
_RECORDER = "import os, sys\nsys.stdout.write(os.environ.get('OPENCODE_ASK_WAIT', ''))\n"


def test_run_wrapper_define_opencode_ask_wait_20(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_ASK_WAIT", raising=False)
    recorder = tmp_path / "rec.py"
    recorder.write_text(_RECORDER)
    rc, out, _err, timed_out = wr.run_wrapper([sys.executable, str(recorder)],
                                              str(tmp_path), b"x", 30)
    assert rc == 0
    assert not timed_out
    assert out == b"6"


def test_run_wrapper_preserva_opencode_ask_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_ASK_WAIT", "900")
    recorder = tmp_path / "rec.py"
    recorder.write_text(_RECORDER)
    rc, out, _err, timed_out = wr.run_wrapper([sys.executable, str(recorder)],
                                              str(tmp_path), b"x", 30)
    assert rc == 0
    assert not timed_out
    assert out == b"900"
