"""Fatia C: cobertura de runs/log/eventos e processo/snapshot do worker_router.

In-process, isolado em tmp_path; kill/terminate via monkeypatch de os.killpg.
"""
import hashlib
import json
import os
import signal
import stat as statmod
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

# Globais mutados pelo codigo sob teste; snapshot/restore por teste.
_RUN_GLOBALS = ("_RUN", "_RUN_T0", "_RUN_WRITTEN", "_LOG_FD", "_LOG_FAILED",
                "_LOG_REPORTED", "_LOG_ENDS_WITH_NEWLINE", "_EVENT_FD",
                "_EVENT_FD_READY", "_CURRENT_PROC")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    saved = {name: getattr(wr, name) for name in _RUN_GLOBALS}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    for key in ("ASK_PRESENTATION_FD", "ASK_PRESENTATION", "WORKER_ASK_NO_MUSE",
                "WORKER_ASK_RUNS_LOG", "MUSE_ASK_MODEL", "MUSE_ASK_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    wr._LOG_FD = None
    wr._LOG_FAILED = False
    wr._LOG_REPORTED = False
    wr._LOG_ENDS_WITH_NEWLINE = True
    wr._EVENT_FD = None
    wr._EVENT_FD_READY = False
    wr._RUN = None
    wr._RUN_T0 = time.monotonic()
    wr._RUN_WRITTEN = False
    wr._CURRENT_PROC = None
    yield tmp_path
    for name, value in saved.items():
        setattr(wr, name, value)


@pytest.fixture
def run_state(isolated):
    run = {"attempts": []}
    wr._RUN = run
    return run


def footer(line):
    return ("--- u: " + line + " ---\n").encode()


def state_dir(tmp_path):
    return tmp_path / "st" / "worker-ask"


# ---------------------------------------------------------------- _parse_run_usage


def test_parse_run_usage_tokens_custo_modelo(isolated):
    data = b"resposta\n" + footer("tokens=in:10/out:20 custo=$0.42 modelo=glm-5")
    assert wr._parse_run_usage(data) == {"input": 10, "output": 20,
                                         "cost_usd": 0.42, "model": "glm-5"}


def test_parse_run_usage_converte_digitos(isolated):
    data = footer("input=7 output=8 cacheRead=9 cacheWrite=10 session=s1")
    assert wr._parse_run_usage(data) == {"session": "s1", "input": 7, "output": 8,
                                         "cacheRead": 9, "cacheWrite": 10}


def test_parse_run_usage_sem_rodape(isolated):
    assert wr._parse_run_usage(b"resposta simples sem rodape") == {}


def test_parse_run_usage_rodape_sem_dois_pontos(isolated):
    # Cabecalho com formato de rodape mas sem ": " -> linha ignorada.
    assert wr._parse_run_usage(b"--- tokens=5 ---\nresposta\n") == {}


def test_parse_run_usage_rodape_fora_da_janela(isolated):
    data = footer("tokens=in:1/out:2") + b"y" * 5000
    assert wr._parse_run_usage(data) == {}


def test_parse_run_usage_tokens_malformados(isolated):
    data = footer("tokens=in10/out:3")
    assert wr._parse_run_usage(data) == {"output": 3}


def test_parse_run_usage_token_sem_valor_e_sem_separador(isolated):
    data = footer("tokens= foo=bar custo=$1.5")
    assert wr._parse_run_usage(data) == {"cost_usd": 1.5}


def test_parse_run_usage_num_nao_numerico(isolated):
    data = footer("tokens=in:abc")
    assert wr._parse_run_usage(data) == {}


def test_parse_run_usage_custo_invalido_e_negativo(isolated):
    data = footer("custo=abc")
    assert wr._parse_run_usage(data) == {}
    data = footer("custo=-1")
    assert wr._parse_run_usage(data) == {}


def test_parse_run_usage_modelo_recusado(isolated):
    assert wr._parse_run_usage(footer("modelo=n/d")) == {}
    assert wr._parse_run_usage(footer("modelo=" + "a" * 65)) == {}


# ---------------------------------------------------------------- _record_run_metrics


def test_record_run_metrics_completo(isolated, run_state, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "a.txt").write_text("x")
    stdout = footer("tokens=in:5/out:6")
    wr._record_run_metrics("codex-ask", stdout, ["a.txt"], str(cwd), {})
    run = wr._RUN
    assert run["executor"] == "codex-ask"
    assert run["output_bytes"] == len(stdout)
    assert run["usage"]["input"] == 5
    assert run["changed_files"] == 1  # antes ausente, depois presente


def test_record_run_metrics_sem_mudanca(isolated, run_state, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "a.txt").write_text("x")
    before = wr.snapshot_allowed(str(cwd), ["a.txt"])
    wr._record_run_metrics("codex-ask", b"", ["a.txt"], str(cwd), before)
    assert wr._RUN["changed_files"] == 0


def test_record_run_metrics_sem_rodape_limpa_usage(isolated, run_state):
    run_state["usage"] = {"input": 1}
    wr._record_run_metrics("codex-ask", b"sem rodape", [], str(isolated), {})
    assert "usage" not in wr._RUN


def test_record_run_metrics_snapshot_falha_vira_none(isolated, run_state, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(wr, "snapshot_allowed", boom)
    wr._record_run_metrics("codex-ask", b"", ["a.txt"], str(isolated), {})
    assert wr._RUN["changed_files"] is None
    assert wr._RUN["executor"] == "codex-ask"


def test_record_run_metrics_excecao_externa_nao_propaga(isolated, run_state, monkeypatch):
    def boom(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(wr, "_parse_run_usage", boom)
    wr._record_run_metrics("codex-ask", b"", [], str(isolated), {})
    assert "executor" not in wr._RUN  # try externo abortou antes


def test_record_run_metrics_sem_run_nao_faz_nada(isolated):
    wr._record_run_metrics("codex-ask", b"", [], str(isolated), {})
    assert wr._RUN is None


# ---------------------------------------------------------------- _emit_executor_start


def test_emit_executor_start_registra_e_limpa_tentativa(isolated, run_state, monkeypatch):
    monkeypatch.setattr(wr, "_EVENT_FD_READY", True)  # canal ausente
    run_state.update({"usage": {"input": 1}, "output_bytes": 5, "changed_files": 2,
                      "executor": "antigo"})
    wr._emit_executor_start("codex-ask", "plan", model="glm")
    assert run_state["attempts"][0]["event"] == "start"
    assert run_state["attempts"][0]["executor"] == "codex-ask"
    assert run_state["attempts"][0]["model"] == "glm"
    assert run_state["executor"] == "codex-ask"
    for key in ("usage", "output_bytes", "changed_files"):
        assert key not in run_state


def test_emit_executor_start_canal_de_eventos(isolated, run_state, monkeypatch):
    r, w = os.pipe()
    try:
        monkeypatch.setattr(wr, "_EVENT_FD", w)
        monkeypatch.setattr(wr, "_EVENT_FD_READY", True)
        wr._emit_executor_start("codex-ask", "plan", model=None)
        payload = json.loads(os.read(r, 4096).decode())
        assert payload == {"event": "executor_start", "executor": "codex-ask",
                           "mode": "plan"}
        assert run_state["attempts"][0]["event"] == "start"
        assert run_state["attempts"][1:] == []  # executor_start nao duplica no run
    finally:
        os.close(r)
        os.close(w)


def test_emit_executor_start_apresentacao_off(isolated, monkeypatch, capsys):
    monkeypatch.setenv("ASK_PRESENTATION", "off")
    monkeypatch.setattr(wr, "_EVENT_FD_READY", True)
    wr._emit_executor_start("codex-ask", "plan")
    assert capsys.readouterr().err == ""


def test_emit_executor_start_apresentacao_json(isolated, monkeypatch, capsys):
    monkeypatch.setenv("ASK_PRESENTATION", "json")
    monkeypatch.setattr(wr, "_EVENT_FD_READY", True)
    wr._emit_executor_start("zcode-worker", "write")
    payload = json.loads(capsys.readouterr().err)
    assert payload["event"] == "executor_start"
    assert payload["executor"] == "ZCode"
    assert payload["mode"] == "write"


def test_emit_executor_start_linha_padrao(isolated, monkeypatch, capsys):
    monkeypatch.setattr(wr, "_EVENT_FD_READY", True)
    wr._emit_executor_start("zcode-worker", "plan")
    err = capsys.readouterr().err  # readouterr() reseta o buffer: ler uma vez
    assert "ZCode" in err
    assert "Em execução" in err


def test_emit_executor_start_rotulo_desconhecido(isolated, monkeypatch, capsys):
    monkeypatch.setattr(wr, "_EVENT_FD_READY", True)
    wr._emit_executor_start("wrangler-ask", "plan")
    assert "wrangler-ask" in capsys.readouterr().err


# ---------------------------------------------------------------- _event / canal


def test_event_registra_no_run(isolated, run_state):
    wr._event("stop", None, executor="codex-ask", rc=2)
    entry = run_state["attempts"][0]
    assert entry["event"] == "stop"
    assert entry["executor"] == "codex-ask"
    assert entry["rc"] == 2
    assert "detail" not in entry  # None e filtrado


def test_event_excecao_no_run_nao_propaga(isolated, monkeypatch):
    monkeypatch.setattr(wr, "_RUN", {"attempts": None})
    wr._event("stop", None, executor="x")
    assert wr._RUN["attempts"] is None


def test_event_canal_pipe(isolated, run_state, monkeypatch):
    r, w = os.pipe()
    try:
        monkeypatch.setattr(wr, "_EVENT_FD", w)
        monkeypatch.setattr(wr, "_EVENT_FD_READY", True)
        wr._event("executor_start", None, executor="codex-ask", mode="plan", model=None)
        payload = json.loads(os.read(r, 4096).decode())
        assert payload["event"] == "executor_start"
        assert "model" not in payload
        assert run_state["attempts"] == []  # executor_start nao vai ao run
    finally:
        os.close(r)
        os.close(w)


def test_event_pipe_com_erro_cai_no_legacy(isolated, monkeypatch, capsys):
    r, w = os.pipe()
    os.close(w)
    try:
        monkeypatch.setattr(wr, "_EVENT_FD", w)
        monkeypatch.setattr(wr, "_EVENT_FD_READY", True)
        wr._event("stop", "linha legada", rc=1)
        assert "linha legada" in capsys.readouterr().err
    finally:
        os.close(r)


def test_event_sem_canal_sem_legacy_e_silencioso(isolated, capsys):
    wr._event("stop", None, executor="x")
    assert capsys.readouterr().err == ""


def test_event_fd_pipe_valido(isolated, monkeypatch):
    r, w = os.pipe()
    try:
        ino = os.fstat(w).st_ino
        monkeypatch.setenv("ASK_PRESENTATION_FD", f"{w}:{ino}")
        assert wr._event_fd() == w
        assert os.get_blocking(w) is False
        assert wr._event_fd() == w  # cache pela flag READY
    finally:
        os.close(r)
        os.close(w)


def test_event_fd_recusa_arquivo_regular(isolated, monkeypatch):
    p = isolated / "plain.txt"
    p.write_text("x")
    fd = os.open(p, os.O_RDONLY)
    try:
        monkeypatch.setenv("ASK_PRESENTATION_FD", f"{fd}:{os.fstat(fd).st_ino}")
        assert wr._event_fd() is None
    finally:
        os.close(fd)


def test_event_fd_invalido_e_fd_morto(isolated, monkeypatch):
    monkeypatch.setenv("ASK_PRESENTATION_FD", "abc:1")
    assert wr._event_fd() is None
    r, w = os.pipe()
    ino = os.fstat(w).st_ino
    os.close(w)
    monkeypatch.setattr(wr, "_EVENT_FD_READY", False)  # READY ficou True na 1a chamada
    try:
        monkeypatch.setenv("ASK_PRESENTATION_FD", f"{w}:{ino}")
        assert wr._event_fd() is None  # fstat em fd fechado -> OSError tratado
    finally:
        os.close(r)


# ---------------------------------------------------------------- log


def test_open_log_cria_arquivo_0600_e_trunca(isolated):
    p = isolated / "sub" / "log.txt"
    p.parent.mkdir()
    p.write_text("antigo")
    p.chmod(0o666)
    fd = wr._open_log(str(p))
    try:
        st = os.fstat(fd)
        assert statmod.S_ISREG(st.st_mode)
        assert st.st_mode & 0o777 == 0o600
        assert st.st_size == 0
        os.write(fd, b"novo\n")
    finally:
        os.close(fd)
    assert p.read_text() == "novo\n"


def test_open_log_recusa_vazio_controle_e_sensivel(isolated):
    for path in ("", "lo\x02g.txt", str(isolated / "credentials")):
        with pytest.raises(RuntimeError, match="recusado"):
            wr._open_log(path)


def test_open_log_symlink_recusado(isolated):
    alvo = isolated / "alvo.txt"
    alvo.write_text("x")
    link = isolated / "log.txt"
    os.symlink(alvo, link)
    with pytest.raises(RuntimeError, match="nao foi possivel abrir"):
        wr._open_log(str(link))


def test_open_log_recusa_nao_regular(isolated):
    fifo = isolated / "log.fifo"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(RuntimeError, match="arquivo regular"):
            wr._open_log(str(fifo))
    finally:
        os.close(reader)


def test_log_exit_completa_linha_pendente(isolated):
    p = isolated / "log.txt"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    wr._LOG_FD = fd
    wr._LOG_ENDS_WITH_NEWLINE = False
    try:
        wr._log_exit(3)
    finally:
        os.close(fd)
        wr._LOG_FD = None
    assert p.read_text() == "\nexit=3\n"


def test_log_exit_sem_linha_pendente(isolated):
    p = isolated / "log.txt"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    wr._LOG_FD = fd
    wr._LOG_ENDS_WITH_NEWLINE = True
    try:
        wr._log_exit(0)
    finally:
        os.close(fd)
        wr._LOG_FD = None
    assert p.read_text() == "exit=0\n"


def test_log_exit_sem_fd_e_silencioso(isolated):
    wr._log_exit(0)  # _LOG_FD None: nao faz nada
    assert wr._LOG_FD is None


def test_is_regular_fd(isolated):
    p = isolated / "reg.txt"
    p.write_text("x")
    fd = os.open(p, os.O_RDONLY)
    try:
        assert wr._is_regular_fd(fd) is True
    finally:
        os.close(fd)
    r, w = os.pipe()
    try:
        assert wr._is_regular_fd(w) is False  # pipe nao e regular
    finally:
        os.close(r)
        os.close(w)
    assert wr._is_regular_fd(99999) is False  # fd inexistente -> OSError tratado


def test_note_log_failure_avisa_uma_vez(isolated, capsys):
    wr._note_log_failure()
    assert "falha ao gravar --log" in capsys.readouterr().err
    wr._note_log_failure()
    assert capsys.readouterr().err == ""  # _LOG_REPORTED trava


# ---------------------------------------------------------------- _write_run


def test_write_run_grava_linha_e_idempotente(isolated, run_state):
    run_state["executor"] = "codex-ask"
    wr._write_run(7)
    rp = state_dir(isolated) / "runs.jsonl"
    lines = rp.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["rc"] == 7
    assert entry["executor"] == "codex-ask"
    assert "duration_s" in entry
    assert rp.stat().st_mode & 0o777 == 0o600
    wr._write_run(8)  # _RUN_WRITTEN trava: nada novo
    assert len(rp.read_text().splitlines()) == 1


def test_write_run_sem_run_nao_grava(isolated):
    wr._write_run(0)
    assert wr._RUN_WRITTEN is True
    assert not (state_dir(isolated) / "runs.jsonl").exists()


def test_write_run_desligado_por_env(isolated, run_state, monkeypatch):
    monkeypatch.setenv("WORKER_ASK_RUNS_LOG", "0")
    wr._write_run(0)
    assert not (state_dir(isolated) / "runs.jsonl").exists()


def test_write_run_ja_gravado_nao_faz_nada(isolated, run_state):
    wr._RUN_WRITTEN = True
    wr._write_run(0)
    assert not (state_dir(isolated) / "runs.jsonl").exists()


def test_write_run_symlink_recusado_com_aviso(isolated, run_state, capsys):
    sd = state_dir(isolated)
    sd.mkdir(parents=True)
    alvo = isolated / "alvo.jsonl"
    alvo.write_text("")
    os.symlink(alvo, sd / "runs.jsonl")
    wr._write_run(1)
    assert "aviso: falha ao gravar runs.jsonl" in capsys.readouterr().err
    assert alvo.read_text() == ""


def test_write_run_lock_falha_nao_grava(isolated, run_state, monkeypatch):
    import fcntl as fcntlmod

    def flock_bloqueado(target, mode):
        raise BlockingIOError(11, "bloqueado")
    monkeypatch.setattr(fcntlmod, "flock", flock_bloqueado)
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)  # loop sem espera real
    wr._write_run(1)
    rp = state_dir(isolated) / "runs.jsonl"
    assert rp.read_text() == ""  # arquivo criado, mas nenhuma linha gravada


def test_write_run_ino_mismatch_reabre_e_grava(isolated, run_state, monkeypatch):
    sd = state_dir(isolated)
    rp = sd / "runs.jsonl"
    original_open = wr._open_nofollow
    calls = {"n": 0}

    def open_com_replace(path, flags, mode=0o600):
        fd = original_open(path, flags, mode)
        if calls["n"] == 0:
            calls["n"] += 1
            tmp = sd / ".repl"
            tmp.write_text("")
            os.replace(tmp, rp)  # inode troca entre open e a checagem
        return fd

    monkeypatch.setattr(wr, "_open_nofollow", open_com_replace)
    run_state["executor"] = "codex-ask"
    wr._write_run(5)
    assert calls["n"] == 1  # reaberto uma vez apos o mismatch
    lines = rp.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["rc"] == 5
    assert not (sd / "runs.jsonl.1").exists()


def test_write_run_rotaciona_quando_grande(isolated, run_state, monkeypatch):
    monkeypatch.setattr(wr, "RUNS_LOG_MAX_BYTES", 0)
    sd = state_dir(isolated)
    sd.mkdir(parents=True, exist_ok=True)
    rp = sd / "runs.jsonl"
    rp.write_text("antigo\n")
    run_state["executor"] = "codex-ask"
    wr._write_run(9)
    bp = sd / "runs.jsonl.1"
    assert bp.read_text() == "antigo\n"
    lines = rp.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["rc"] == 9


# ---------------------------------------------------------------- _exit_timeout


def test_exit_timeout_sai_124_registra_e_avisa(isolated, run_state, capsys):
    with pytest.raises(SystemExit) as exc:
        wr._exit_timeout("timeout legado", executor="codex-ask", detail="timeout")
    assert exc.value.code == 124
    assert wr.TIMEOUT_DIRECTIVE in capsys.readouterr().err
    entry = run_state["attempts"][0]
    assert entry["event"] == "stop"
    assert entry["rc"] == 124
    assert entry["executor"] == "codex-ask"


# ---------------------------------------------------------------- _is_sensitive / validate_allowed_file


@pytest.mark.parametrize("nome,esperado", [
    (".ssh", True), (".AWS", True), ("credentials", True), ("credential", True),
    (".env", True), (".env.local", True), ("auth.json", True), ("srv.pem", True),
    ("srv.key", True), ("mycreds.txt", True), ("id_rsa", True), ("id_ed25519", True),
    ("readme.md", False), ("environ", False), ("normal.txt", False),
])
def test_is_sensitive(nome, esperado):
    assert wr._is_sensitive(nome) is esperado


def test_validate_allowed_file_valido(isolated):
    (isolated / "d1").mkdir()
    (isolated / "d1" / "d2").mkdir()
    assert wr.validate_allowed_file(isolated, "d1/d2/f.txt") == "d1/d2/f.txt"
    assert wr.validate_allowed_file(isolated, "topo.txt") == "topo.txt"


@pytest.mark.parametrize("rel", [None, "", "a\nb", "/abs", "a\\b", "a//b",
                                 "a/./b", "a/../b", ".env", "d/.env"])
def test_validate_allowed_file_recusa(isolated, rel):
    with pytest.raises(ValueError):
        wr.validate_allowed_file(isolated, rel)


def test_validate_allowed_file_symlink_em_ancestral(isolated):
    real = isolated / "real"
    real.mkdir()
    os.symlink(real, isolated / "link")
    with pytest.raises(ValueError, match="ancestral"):
        wr.validate_allowed_file(isolated, "link/f.txt")


# ---------------------------------------------------------------- snapshot


def test_snapshot_one_arquivo_regular(isolated):
    p = isolated / "f.txt"
    p.write_bytes(b"hello\n")
    p.chmod(0o644)
    presente, digest, modo = wr._snapshot_one(str(isolated), "f.txt")
    assert presente is True
    assert digest == hashlib.sha256(b"hello\n").hexdigest()
    assert modo == 0o644


def test_snapshot_one_ausente(isolated):
    assert wr._snapshot_one(str(isolated), "missing.txt") == (False, "ABSENT", 0)


def test_snapshot_one_recusa_especiais(isolated):
    with pytest.raises(RuntimeError, match="invalido"):
        wr._snapshot_one(str(isolated), "\x01")
    with pytest.raises(RuntimeError, match="sensivel"):
        wr._snapshot_one(str(isolated), ".env")
    d = isolated / "d"
    d.mkdir()
    with pytest.raises(RuntimeError, match="especial"):
        wr._snapshot_one(str(isolated), "d")  # diretorio
    alvo = isolated / "alvo.txt"
    alvo.write_text("x")
    os.symlink(alvo, isolated / "link.txt")
    with pytest.raises(RuntimeError, match="falha de I/O"):  # O_NOFOLLOW -> ELOOP
        wr._snapshot_one(str(isolated), "link.txt")


def test_snapshot_one_ancestral_symlink_e_io(isolated):
    real = isolated / "real"
    real.mkdir()
    os.symlink(real, isolated / "link")
    with pytest.raises(RuntimeError, match="ancestral"):
        wr._snapshot_one(str(isolated), "link/f.txt")
    (isolated / "bloqueio").write_text("x")
    with pytest.raises(RuntimeError, match="falha de I/O"):  # componente nao-dir
        wr._snapshot_one(str(isolated), "bloqueio/f.txt")


def test_snapshot_one_arquivo_grande(isolated, monkeypatch):
    monkeypatch.setattr(wr, "MAX_SNAPSHOT_BYTES", 4)
    (isolated / "grande.txt").write_bytes(b"0123456789")
    with pytest.raises(RuntimeError, match="muito grande"):
        wr._snapshot_one(str(isolated), "grande.txt")


def test_snapshot_allowed_misto(isolated):
    (isolated / "a.txt").write_text("a")
    out = wr.snapshot_allowed(str(isolated), ["a.txt", "falta.txt"])
    assert out["a.txt"][0] is True
    assert out["falta.txt"] == (False, "ABSENT", 0)


# ---------------------------------------------------------------- kill/terminate


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.kill_calls = 0
        self.wait_calls = []

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


def test_kill_group_ok_e_lookup_ausente(monkeypatch):
    calls = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: calls.append((pid, sig)))
    proc = FakeProc(4242)
    wr._kill_group(proc, signal.SIGTERM)
    assert calls == [(4242, signal.SIGTERM)]
    assert proc.kill_calls == 0
    monkeypatch.setattr("os.killpg", _raise(ProcessLookupError))
    wr._kill_group(proc, signal.SIGKILL)
    assert proc.kill_calls == 0  # ProcessLookupError e ignorado


def test_kill_group_falha_generica_cai_no_kill(monkeypatch):
    monkeypatch.setattr("os.killpg", _raise(ValueError("x")))
    proc = FakeProc(7)
    wr._kill_group(proc, signal.SIGTERM)
    assert proc.kill_calls == 1
    proc2 = FakeProc(8)
    proc2.kill = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    wr._kill_group(proc2, signal.SIGTERM)  # kill() falhando e engolido
    assert proc2.kill_calls == 0


def _raise(exc):
    def _inner(*a, **k):
        raise exc
    return _inner


def test_terminate_group_probe_ausente_soh_wait(monkeypatch):
    monkeypatch.setattr("os.killpg", _raise(ProcessLookupError))
    proc = FakeProc(11)
    wr._terminate_group(proc)
    assert proc.wait_calls == [None]


def test_terminate_group_sigterm_grace_sigkill(monkeypatch):
    calls = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: calls.append((pid, sig)))
    sleeps = []
    monkeypatch.setattr(wr.time, "sleep", lambda s: sleeps.append(s))
    proc = FakeProc(12)
    wr._terminate_group(proc)
    assert calls == [(12, 0), (12, signal.SIGTERM), (12, signal.SIGKILL)]
    assert proc.wait_calls == [5]
    assert sleeps  # grace rodou


def test_terminate_group_grace_expirado_pula_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: calls.append((pid, sig)))
    sleeps = []
    monkeypatch.setattr(wr.time, "sleep", lambda s: sleeps.append(s))
    clock = iter([100.0, 200.0])

    def monotonic():
        return next(clock, 300.0)
    monkeypatch.setattr(wr.time, "monotonic", monotonic)
    proc = FakeProc(13)
    wr._terminate_group(proc)
    assert calls == [(13, 0), (13, signal.SIGTERM), (13, signal.SIGKILL)]
    assert sleeps == []  # deadline ja passou no primeiro check


# ---------------------------------------------------------------- run_wrapper


RW_SCRIPT = r'''
import json, os, signal, sys, time
if os.environ.get("RW_BIG") == "1":
    sys.stdout.write("A" * (2 * 1024 * 1024))
if os.environ.get("RW_SLEEP"):
    time.sleep(float(os.environ["RW_SLEEP"]))
if os.environ.get("RW_STDIN_READ") == "1":
    sys.stdout.write(sys.stdin.read())
sys.stdout.write(os.environ.get("RW_STDOUT", ""))
sys.stderr.write(os.environ.get("RW_STDERR", ""))
log = os.environ.get("RW_ENV_LOG")
if log:
    with open(log, "w") as f:
        json.dump({"active": os.environ.get("WORKER_ASK_ACTIVE"),
                   "quota_helper": os.environ.get("WORKER_ASK_QUOTA_HELPER"),
                   "muse_model": os.environ.get("MUSE_ASK_MODEL"),
                   "muse_effort": os.environ.get("MUSE_ASK_EFFORT"),
                   "presentation_fd": os.environ.get("ASK_PRESENTATION_FD")}, f)
rc = int(os.environ.get("RW_RC", "0"))
if rc == -15:
    os.kill(os.getpid(), signal.SIGTERM)
sys.exit(rc if rc >= 0 else 0)
'''


@pytest.fixture
def rw_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "rwbin"
    bin_dir.mkdir()
    p = bin_dir / "fake-exec"
    p.write_text("#!" + sys.executable + "\n" + RW_SCRIPT + "\n")
    p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return p


def test_run_wrapper_sucesso_com_prompt(isolated, rw_bin):
    cwd = isolated / "work"
    cwd.mkdir()
    rc, out, marker, timed_out = wr.run_wrapper([str(rw_bin)], str(cwd),
                                                b"dados-do-prompt", 10)
    assert (rc, out, marker, timed_out) == (0, b"", b"", False)


def test_run_wrapper_prompt_passa_pelo_stdin(isolated, rw_bin, monkeypatch):
    cwd = isolated / "work"
    cwd.mkdir()
    monkeypatch.setenv("RW_STDIN_READ", "1")
    rc, out, _, _ = wr.run_wrapper([str(rw_bin)], str(cwd), b"conteudo", 10)
    assert rc == 0
    assert out == b"conteudo"


def test_run_wrapper_rc_positivo_e_por_sinal(isolated, rw_bin, monkeypatch):
    cwd = isolated / "work"
    cwd.mkdir()
    monkeypatch.setenv("RW_RC", "2")
    assert wr.run_wrapper([str(rw_bin)], str(cwd), b"", 10)[0] == 2
    monkeypatch.setenv("RW_RC", "-15")  # morre por SIGTERM
    assert wr.run_wrapper([str(rw_bin)], str(cwd), b"", 10)[0] == 143


def test_run_wrapper_marker_de_falha(isolated, rw_bin, monkeypatch):
    cwd = isolated / "work"
    cwd.mkdir()
    monkeypatch.setenv("RW_STDERR",
                       "ERRO: launcher de autenticacao incompativel com o bundle do ZCode")
    _, _, marker, _ = wr.run_wrapper([str(rw_bin)], str(cwd), b"", 10)
    assert marker == b"launcher_incompatible"


def test_run_wrapper_limpa_env_do_filho(isolated, rw_bin, monkeypatch):
    cwd = isolated / "work"
    cwd.mkdir()
    log = isolated / "env.json"
    monkeypatch.setenv("RW_ENV_LOG", str(log))
    monkeypatch.setenv("MUSE_ASK_MODEL", "nao-passar")
    monkeypatch.setenv("MUSE_ASK_EFFORT", "nao-passar")
    monkeypatch.setenv("ASK_PRESENTATION_FD", "9:9")
    rc, _, _, _ = wr.run_wrapper([str(rw_bin)], str(cwd), b"", 10)
    assert rc == 0
    child = json.loads(log.read_text())
    assert child["active"] == "1"
    assert child["quota_helper"].endswith("quota_reset.py")
    for key in ("muse_model", "muse_effort", "presentation_fd"):
        assert child[key] is None


def test_run_wrapper_timeout(isolated, rw_bin, monkeypatch):
    cwd = isolated / "work"
    cwd.mkdir()
    monkeypatch.setenv("RW_SLEEP", "30")
    rc, out, marker, timed_out = wr.run_wrapper([str(rw_bin)], str(cwd), b"", 0.3)
    assert timed_out is True
    assert rc == 143  # SIGTERM do grupo
    assert (out, marker) == (b"", b"")


def test_run_wrapper_overflow_de_saida(isolated, rw_bin, monkeypatch):
    cwd = isolated / "work"
    cwd.mkdir()
    monkeypatch.setenv("RW_BIG", "1")
    rc, out, marker, timed_out = wr.run_wrapper([str(rw_bin)], str(cwd), b"", 10)
    assert rc == 1
    assert (out, marker, timed_out) == (b"", b"", False)


# ---------------------------------------------------------------- muse_opted_out


def test_muse_opted_out_env(isolated, monkeypatch):
    monkeypatch.setenv("WORKER_ASK_NO_MUSE", "1")
    assert wr.muse_opted_out(str(isolated)) is True


def test_muse_opted_out_marker_no_cwd(isolated):
    (isolated / ".worker-ask-no-muse").write_text("")
    assert wr.muse_opted_out(str(isolated)) is True


def test_muse_opted_out_marker_no_ancestral(isolated):
    sub = isolated / "a" / "b"
    sub.mkdir(parents=True)
    (isolated / ".worker-ask-no-muse").write_text("")
    assert wr.muse_opted_out(str(sub)) is True


def test_muse_opted_out_git_no_cwd_para_a_busca(isolated):
    (isolated / ".git").mkdir()
    assert wr.muse_opted_out(str(isolated)) is False


def test_muse_opted_out_isfile_com_erro_nao_propaga(isolated, monkeypatch):
    def boom(path):
        raise OSError("x")
    monkeypatch.setattr("os.path.isfile", boom)
    resultado = wr.muse_opted_out(str(isolated))
    assert isinstance(resultado, bool)


def test_muse_opted_out_exists_com_erro_anda_ate_a_raiz(isolated, monkeypatch):
    def boom(path):
        raise OSError("x")
    monkeypatch.setattr("os.path.exists", boom)
    assert wr.muse_opted_out(str(isolated)) is False
