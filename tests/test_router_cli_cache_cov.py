"""Cobertura fatia D: entrada CLI (main/_cli/handler/_presentation_helper), cache e estado.

Tudo in-process: HOME/XDG_STATE_HOME isolados em tmp_path, sem rede, sem wrappers reais.
"""
import fcntl
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

BIN_HELPER = ROOT / "bin" / "ask_presentation.py"


@pytest.fixture
def router_state(tmp_path, monkeypatch):
    """Estado global do roteador isolado por teste."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    monkeypatch.delenv("WORKER_ASK_RUNS_LOG", raising=False)
    monkeypatch.delenv("WORKER_ASK_UNKNOWN_RESET_COOLDOWN", raising=False)
    monkeypatch.setattr(wr, "_RUN", None)
    monkeypatch.setattr(wr, "_RUN_T0", time.monotonic())
    monkeypatch.setattr(wr, "_RUN_WRITTEN", False)
    monkeypatch.setattr(wr, "_LOG_FD", None)
    monkeypatch.setattr(wr, "_LOG_FAILED", False)
    monkeypatch.setattr(wr, "_LOG_REPORTED", False)
    monkeypatch.setattr(wr, "_INTERRUPTED", False)
    monkeypatch.setattr(wr, "_CURRENT_PROC", None)
    return tmp_path


def _run_dict():
    return {"attempts": [], "executor": "test"}


def _runs_lines(tmp_path):
    rp = tmp_path / "st" / "worker-ask" / "runs.jsonl"
    if not rp.exists():
        return []
    return [json.loads(linha) for linha in rp.read_text().splitlines() if linha.strip()]


def _state_dir(tmp_path):
    return tmp_path / "st" / "worker-ask"


# ---------------------------------------------------------------- main/handler

def test_main_systemexit_int_grava_run(router_state, monkeypatch):
    def fake_impl(argv=None):
        raise SystemExit(3)

    monkeypatch.setattr(wr, "_main_impl", fake_impl)
    monkeypatch.setattr(wr, "_RUN", _run_dict())
    prev_int = signal.getsignal(signal.SIGINT)
    with pytest.raises(SystemExit) as ei:
        wr.main([])
    assert ei.value.code == 3
    lines = _runs_lines(router_state)
    assert len(lines) == 1 and lines[0]["rc"] == 3
    assert signal.getsignal(signal.SIGINT) is prev_int


def test_main_systemexit_none_vira_rc0(router_state, monkeypatch):
    def fake_impl(argv=None):
        raise SystemExit(None)

    monkeypatch.setattr(wr, "_main_impl", fake_impl)
    monkeypatch.setattr(wr, "_RUN", _run_dict())
    with pytest.raises(SystemExit) as ei:
        wr.main([])
    assert ei.value.code is None
    lines = _runs_lines(router_state)
    assert len(lines) == 1 and lines[0]["rc"] == 0


def test_main_systemexit_str_vira_rc1(router_state, monkeypatch):
    def fake_impl(argv=None):
        raise SystemExit("boom")

    monkeypatch.setattr(wr, "_main_impl", fake_impl)
    monkeypatch.setattr(wr, "_RUN", _run_dict())
    with pytest.raises(SystemExit) as ei:
        wr.main([])
    assert ei.value.code == "boom"
    lines = _runs_lines(router_state)
    assert len(lines) == 1 and lines[0]["rc"] == 1


def test_main_excecao_generica_grava_rc1(router_state, monkeypatch):
    def fake_impl(argv=None):
        raise ValueError("falha de teste")

    monkeypatch.setattr(wr, "_main_impl", fake_impl)
    monkeypatch.setattr(wr, "_RUN", _run_dict())
    with pytest.raises(ValueError):
        wr.main([])
    lines = _runs_lines(router_state)
    assert len(lines) == 1 and lines[0]["rc"] == 1


def test_main_sucesso_grava_rc0(router_state, monkeypatch):
    monkeypatch.setattr(wr, "_main_impl", lambda argv=None: None)
    monkeypatch.setattr(wr, "_RUN", _run_dict())
    wr.main([])
    lines = _runs_lines(router_state)
    assert len(lines) == 1 and lines[0]["rc"] == 0


def test_main_sigint_sem_filho_sai_130(router_state, monkeypatch):
    def fake_impl(argv=None):
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(wr, "_main_impl", fake_impl)
    monkeypatch.setattr(wr, "_RUN", _run_dict())
    with pytest.raises(SystemExit) as ei:
        wr.main([])
    assert ei.value.code == 128 + signal.SIGINT
    assert wr._INTERRUPTED is True
    lines = _runs_lines(router_state)
    assert len(lines) == 1 and lines[0]["rc"] == 128 + signal.SIGINT


def test_main_sigint_com_filho_termina_grupo(router_state, monkeypatch):
    mortos = []
    monkeypatch.setattr(wr, "_terminate_group", lambda p: mortos.append(p))
    proc = object()
    monkeypatch.setattr(wr, "_CURRENT_PROC", proc)

    def fake_impl(argv=None):
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(wr, "_main_impl", fake_impl)
    with pytest.raises(SystemExit) as ei:
        wr.main([])
    assert ei.value.code == 128 + signal.SIGINT
    assert mortos == [proc]


# ------------------------------------------------------- _presentation_helper

def test_presentation_helper_prefere_irma(router_state):
    assert BIN_HELPER.is_file()
    assert wr._presentation_helper() == BIN_HELPER


def test_presentation_helper_cai_para_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    helper = home / ".local" / "bin" / "ask_presentation.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# helper\n")
    monkeypatch.setenv("HOME", str(home))
    real_is_file = Path.is_file

    def fake_is_file(self):
        if str(self) == str(BIN_HELPER):
            return False
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    assert wr._presentation_helper() == helper


def test_presentation_helper_nada_encontrado(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    assert wr._presentation_helper() is None


# ----------------------------------------------------------------------- _cli

def test_cli_exit_int_propaga(router_state, monkeypatch):
    monkeypatch.setattr(wr, "main", lambda: (_ for _ in ()).throw(SystemExit(3)))
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 3


def test_cli_exit_none_vira_zero(router_state, monkeypatch):
    monkeypatch.setattr(wr, "main", lambda: (_ for _ in ()).throw(SystemExit(None)))
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 0


def test_cli_exit_str_releva_original(router_state, monkeypatch):
    monkeypatch.setattr(wr, "main", lambda: (_ for _ in ()).throw(SystemExit("boom")))
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == "boom"


def test_cli_log_failed_bumpa_zero(router_state, monkeypatch, capsys):
    monkeypatch.setattr(wr, "_LOG_FAILED", True)
    monkeypatch.setattr(wr, "main", lambda: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 1
    assert "falha ao gravar --log" in capsys.readouterr().err


def test_cli_log_failed_bumpa_75(router_state, monkeypatch):
    monkeypatch.setattr(wr, "_LOG_FAILED", True)
    monkeypatch.setattr(wr, "main", lambda: (_ for _ in ()).throw(SystemExit(75)))
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 1


def test_cli_runtimeerror(router_state, monkeypatch, capsys):
    def boom():
        raise RuntimeError("sem permissao")

    monkeypatch.setattr(wr, "main", boom)
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 1
    assert "sem permissao" in capsys.readouterr().err


def test_cli_runtimeerror_com_log_failed(router_state, monkeypatch, capsys):
    monkeypatch.setattr(wr, "_LOG_FAILED", True)

    def boom():
        raise RuntimeError("sem permissao")

    monkeypatch.setattr(wr, "main", boom)
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "sem permissao" in err and "falha ao gravar --log" in err


def test_cli_excecao_generica(router_state, monkeypatch, capsys):
    def boom():
        raise ValueError("qualquer coisa")

    monkeypatch.setattr(wr, "main", boom)
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 1
    assert "infraestrutura" in capsys.readouterr().err


def test_cli_excecao_generica_com_log_failed(router_state, monkeypatch, capsys):
    monkeypatch.setattr(wr, "_LOG_FAILED", True)

    def boom():
        raise ValueError("qualquer coisa")

    monkeypatch.setattr(wr, "main", boom)
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "infraestrutura" in err and "falha ao gravar --log" in err


def test_cli_ok_sem_log_failed(router_state, monkeypatch):
    monkeypatch.setattr(wr, "main", lambda: None)
    assert wr._cli() is None


def test_cli_ok_com_log_failed_sai_1(router_state, monkeypatch, capsys):
    monkeypatch.setattr(wr, "_LOG_FAILED", True)
    monkeypatch.setattr(wr, "main", lambda: None)
    with pytest.raises(SystemExit) as ei:
        wr._cli()
    assert ei.value.code == 1
    assert "falha ao gravar --log" in capsys.readouterr().err


# ---------------------------------------------------- _unknown_reset_cooldown

def test_unknown_reset_cooldown_padrao(router_state):
    assert wr._unknown_reset_cooldown() == wr.UNKNOWN_RESET_COOLDOWN_SECONDS


@pytest.mark.parametrize("raw,expected", [
    ("7200", 7200.0),
    ("1.5", 1.5),
    ("abc", wr.UNKNOWN_RESET_COOLDOWN_SECONDS),
    ("-1", wr.UNKNOWN_RESET_COOLDOWN_SECONDS),
    ("0", wr.UNKNOWN_RESET_COOLDOWN_SECONDS),
    ("inf", wr.UNKNOWN_RESET_COOLDOWN_SECONDS),
    ("nan", wr.UNKNOWN_RESET_COOLDOWN_SECONDS),
])
def test_unknown_reset_cooldown_valores(router_state, monkeypatch, raw, expected):
    monkeypatch.setenv("WORKER_ASK_UNKNOWN_RESET_COOLDOWN", raw)
    assert wr._unknown_reset_cooldown() == expected


# ---------------------------------------------------------------- _valid_until

@pytest.mark.parametrize("value,ok", [
    (True, False),
    (False, False),
    ("500", False),
    (None, False),
    (-1.0, False),
    (float("inf"), False),
    (float("nan"), False),
    (0.0, True),
    (500.0, True),
    (1000.0 + wr.MAX_RESET_SECONDS + 1.0, True),
    (1000.0 + wr.MAX_RESET_SECONDS + 2.0, False),
])
def test_valid_until_casos(router_state, value, ok):
    assert wr._valid_until(value, 1000.0) is ok


def test_valid_until_isfinite_excecao(router_state, monkeypatch):
    def quebra(value):
        raise TypeError("nao numerico")

    monkeypatch.setattr(wr.math, "isfinite", quebra)
    assert wr._valid_until(1.0, 1000.0) is False


# ---------------------------------------------------------------- _cache_entry

def test_cache_entry_valida_completa(router_state):
    entry = wr._cache_entry("muse-ask", {
        "wrapper": "muse-ask", "until": 1500.0, "reset_known": True,
        "blocked_reason": "quota", "generation": "a" * 32,
    }, 1000.0)
    assert entry == {"wrapper": "muse-ask", "until": 1500.0, "reset_known": True,
                     "blocked_reason": "quota", "generation": "a" * 32}


def test_cache_entry_recusas(router_state):
    now = 1000.0
    assert wr._cache_entry("muse-ask", {"wrapper": "outro", "until": 1500.0}, now) is None
    assert wr._cache_entry("muse-ask", "string", now) is None
    # Legado: bloqueio permanente (until None) destrava no proximo load.
    assert wr._cache_entry("muse-ask", {"wrapper": "muse-ask", "until": None}, now) is None
    # Expirado: until no passado.
    assert wr._cache_entry("muse-ask", {"wrapper": "muse-ask", "until": 900.0}, now) is None
    entry = wr._cache_entry("muse-ask", {"wrapper": "muse-ask", "until": 1500.0,
                                         "blocked_reason": "estranho"}, now)
    assert "blocked_reason" not in entry
    entry = wr._cache_entry("muse-ask", {"wrapper": "muse-ask", "until": 1500.0,
                                         "generation": "zz"}, now)
    assert "generation" not in entry


def test_cache_entry_reset_known_falso(router_state):
    entry = wr._cache_entry("muse-ask", {"wrapper": "muse-ask", "until": 1500.0,
                                         "reset_known": "sim"}, 1000.0)
    assert entry["reset_known"] is False


# ------------------------------------------------------------------ load_cache

def _seed_cache(tmp_path, data, raw=None):
    sd = _state_dir(tmp_path)
    sd.mkdir(parents=True, exist_ok=True)
    cp = sd / "cache.json"
    if raw is not None:
        cp.write_bytes(raw)
    else:
        cp.write_text(json.dumps(data))
    return sd


def test_load_cache_vazio_sem_arquivo(router_state):
    assert wr.load_cache() == {}
    assert wr.load_cache(create_dir=True) == {}


def test_load_cache_mistura_legado_e_expirado(router_state):
    now = time.time()
    _seed_cache(router_state, {
        "zcode-worker": {"wrapper": "zcode-worker", "until": now + 1000, "reset_known": True},
        "muse-ask": {"wrapper": "muse-ask", "until": None},
        "opencode-ask": {"wrapper": "opencode-ask", "until": now - 10},
        "codex-ask": {"wrapper": "codex-ask", "until": now + 50, "blocked_reason": "quota",
                      "generation": "a" * 32},
    })
    cache = wr.load_cache()
    assert set(cache) == {"zcode-worker", "codex-ask"}
    assert cache["zcode-worker"]["reset_known"] is True
    assert cache["codex-ask"]["blocked_reason"] == "quota"
    assert cache["codex-ask"]["generation"] == "a" * 32


def test_load_cache_corrompido(router_state, capsys):
    _seed_cache(router_state, None, raw=b"isto nao e json{")
    assert wr.load_cache() == {}
    assert "corrompido" in capsys.readouterr().err


def test_load_cache_nao_dicionario(router_state):
    _seed_cache(router_state, None, raw=b"[1, 2]")
    assert wr.load_cache() == {}


def test_load_cache_nao_utf8(router_state):
    _seed_cache(router_state, None, raw=b"\xff\xfe{}")
    assert wr.load_cache() == {}


def test_load_cache_oversize(router_state):
    _seed_cache(router_state, None, raw=b"x" * (64 * 1024 + 1))
    assert wr.load_cache() == {}


def test_load_cache_recusa_symlink_estado(router_state):
    alvo = router_state / "real"
    alvo.mkdir()
    sd = _state_dir(router_state)
    sd.parent.mkdir(parents=True)
    sd.symlink_to(alvo)
    with pytest.raises(RuntimeError, match="diretorio de estado eh symlink"):
        wr.load_cache()


def test_load_cache_recusa_symlink_cache(router_state):
    sd = _state_dir(router_state)
    sd.mkdir(parents=True)
    (sd / "cache.json").symlink_to(sd / "outro.json")
    with pytest.raises(RuntimeError, match="cache eh symlink"):
        wr.load_cache()


def test_load_cache_recusa_fifo(router_state):
    sd = _state_dir(router_state)
    sd.mkdir(parents=True)
    os.mkfifo(str(sd / "cache.json"))
    with pytest.raises(RuntimeError, match="cache invalido"):
        wr.load_cache()


def test_load_cache_erro_io_abertura(router_state, monkeypatch):
    _seed_cache(router_state, {})
    real_open = os.open

    def fake_open(path, flags, mode=0o777):
        if str(path).endswith("cache.json"):
            raise OSError(13, "negado")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fake_open)
    with pytest.raises(RuntimeError, match="falha de I/O ao ler cache"):
        wr.load_cache()


def test_load_cache_erro_io_leitura(router_state, monkeypatch):
    _seed_cache(router_state, {})

    def fake_read(fd, n):
        raise OSError(5, "io")

    monkeypatch.setattr(os, "read", fake_read)
    with pytest.raises(RuntimeError, match="falha de I/O ao ler cache"):
        wr.load_cache()


def test_load_cache_erro_io_close(router_state, monkeypatch):
    _seed_cache(router_state, {})
    real_close = os.close

    def fake_close(fd):
        real_close(fd)
        raise OSError(5, "close")

    monkeypatch.setattr(os, "close", fake_close)
    assert wr.load_cache() == {}


# ------------------------------------------------------------------ save_cache

def test_save_cache_grava_e_normaliza(router_state):
    now = time.time()
    wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker", "until": now + 100}})
    cp = _state_dir(router_state) / "cache.json"
    assert cp.exists()
    assert stat.S_IMODE(cp.stat().st_mode) == 0o600
    data = json.loads(cp.read_text())
    entry = data["zcode-worker"]
    assert entry["until"] == pytest.approx(now + 100)
    assert entry["reset_known"] is False
    assert len(entry["generation"]) == 32
    int(entry["generation"], 16)  # hex valido


def test_save_cache_chave_desconhecida_ignorada(router_state):
    wr.save_cache({"intruso": {"wrapper": "intruso", "until": time.time() + 100}})
    data = json.loads((_state_dir(router_state) / "cache.json").read_text())
    assert "intruso" not in data


def test_save_cache_segunda_gravacao_preserva(router_state):
    now = time.time()
    wr.save_cache({"muse-ask": {"wrapper": "muse-ask", "until": now + 200}})
    wr.save_cache({"muse-ask": {"wrapper": "muse-ask", "until": now + 300}})
    cache = wr.load_cache()
    assert cache["muse-ask"]["until"] == pytest.approx(now + 300)


def test_save_cache_timeout_nao_suplanta_outro(router_state):
    now = time.time()
    wr.save_cache({"muse-ask": {"wrapper": "muse-ask", "until": now + 200,
                                "blocked_reason": "quota"}})
    primeiro = wr.load_cache()["muse-ask"]
    wr.save_cache({"muse-ask": {"wrapper": "muse-ask", "until": now + 100,
                                "blocked_reason": "timeout"}})
    cache = wr.load_cache()
    assert cache["muse-ask"]["until"] == pytest.approx(now + 200)
    assert cache["muse-ask"]["generation"] != primeiro["generation"]


def test_save_cache_ate_recente_nao_suplanta_maior(router_state):
    now = time.time()
    wr.save_cache({"claude-ask": {"wrapper": "claude-ask", "until": now + 200}})
    wr.save_cache({"claude-ask": {"wrapper": "claude-ask", "until": now + 100}})
    assert wr.load_cache()["claude-ask"]["until"] == pytest.approx(now + 200)


def test_save_cache_clear_remove(router_state):
    now = time.time()
    wr.save_cache({"claude-ask": {"wrapper": "claude-ask", "until": now + 100}})
    entrada = wr.load_cache()["claude-ask"]
    wr.save_cache({}, clear={"claude-ask": entrada})
    assert "claude-ask" not in wr.load_cache()


def test_save_cache_recusa_symlink_lock(router_state):
    sd = _state_dir(router_state)
    sd.mkdir(parents=True)
    (sd / "lock").symlink_to(sd / "alvo")
    with pytest.raises(RuntimeError, match="symlink"):
        wr.save_cache({})


def test_save_cache_lock_invalido_fifo(router_state):
    sd = _state_dir(router_state)
    sd.mkdir(parents=True)
    os.mkfifo(str(sd / "lock"))
    with pytest.raises(RuntimeError, match="lock invalido"):
        wr.save_cache({})


def test_save_cache_erro_io_lock(router_state, monkeypatch):
    real_open = os.open

    def fake_open(path, flags, mode=0o777):
        if str(path).endswith("/lock"):
            raise OSError(13, "negado")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fake_open)
    with pytest.raises(RuntimeError, match="falha de I/O no lock"):
        wr.save_cache({})


def test_save_cache_falha_escrita_remove_temp(router_state, monkeypatch):
    wr.ensure_state_dir(create=True)

    def fake_dump(obj, fp, *args, **kwargs):
        raise OSError(28, "cheio")

    monkeypatch.setattr(wr.json, "dump", fake_dump)
    with pytest.raises(RuntimeError, match="falha ao escrever cache"):
        wr.save_cache({"zcode-worker": {"wrapper": "zcode-worker",
                                        "until": time.time() + 100}})
    sobras = [p.name for p in _state_dir(router_state).iterdir()
              if p.name.startswith(".cache-")]
    assert sobras == []


def test_save_cache_erro_unlock_nao_impede(router_state, monkeypatch):
    real_flock = fcntl.flock

    def fake_flock(fd, op):
        if op & fcntl.LOCK_UN:
            raise OSError(5, "unlock")
        return real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    now = time.time()
    wr.save_cache({"muse-ask": {"wrapper": "muse-ask", "until": now + 100}})
    assert wr.load_cache()["muse-ask"]["until"] == pytest.approx(now + 100)


def test_save_cache_erro_close_nao_impede(router_state, monkeypatch):
    real_close = os.close

    def fake_close(fd):
        real_close(fd)
        raise OSError(5, "close")

    monkeypatch.setattr(os, "close", fake_close)
    now = time.time()
    wr.save_cache({"muse-ask": {"wrapper": "muse-ask", "until": now + 100}})
    assert wr.load_cache()["muse-ask"]["until"] == pytest.approx(now + 100)


# ------------------------------------------------------ ensure_state_dir

def test_ensure_state_dir_cria_com_700(router_state):
    sd = wr.ensure_state_dir(create=True)
    assert sd.is_dir()
    assert stat.S_IMODE(sd.stat().st_mode) == 0o700


def test_ensure_state_dir_sem_criar_nao_existe(router_state):
    sd = wr.ensure_state_dir(create=False)
    assert not sd.exists()


def test_ensure_state_dir_recusa_symlink(router_state):
    alvo = router_state / "real"
    alvo.mkdir()
    sd = _state_dir(router_state)
    sd.parent.mkdir(parents=True)
    sd.symlink_to(alvo)
    with pytest.raises(RuntimeError, match="symlink"):
        wr.ensure_state_dir(create=True)


def test_ensure_state_dir_invalida_pos_mkdir(router_state, monkeypatch):
    sd = _state_dir(router_state)
    real_is_symlink = Path.is_symlink
    vistas = set()

    def fake_is_symlink(self):
        if str(self) == str(sd):
            if str(self) in vistas:
                return True
            vistas.add(str(self))
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(RuntimeError, match="diretorio de estado invalido"):
        wr.ensure_state_dir(create=True)


# --------------------------------------- _lock_or_none (via _write_run)

def test_write_run_lock_ocupado_nao_grava(router_state, monkeypatch):
    # Verifica _lock_or_none: lock held externamente ate o deadline de 0,5 s
    # (sem patch de tempo; deadline real de 0,5 s < 2 s por teste).
    monkeypatch.setattr(wr, "_RUN", _run_dict())
    monkeypatch.setattr(wr, "_RUN_WRITTEN", False)
    sd = wr.ensure_state_dir(create=True)
    rp = sd / "runs.jsonl"
    rp.write_text("")
    hold_fd = os.open(str(rp), os.O_RDWR)
    try:
        fcntl.flock(hold_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wr._write_run(0)
        assert rp.read_text() == ""
        assert wr._RUN_WRITTEN is True
    finally:
        fcntl.flock(hold_fd, fcntl.LOCK_UN)
        os.close(hold_fd)
