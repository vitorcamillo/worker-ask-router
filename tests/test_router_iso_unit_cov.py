"""Cobertura in-process das funções de isolamento, runs e fingerprint do roteador.

Fatia cov-iso-router: importa worker_router e chama as funções diretamente
(sem subprocesso do roteador), com tmp_path/monkeypatch e dublês simples nas
dependências de worker_isolation — mesmo padrão de tests/test_router_main_cov.py
e tests/test_router_runs_cov.py.
"""

import builtins
import hashlib
import json
import os
import stat as statmod
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402
import worker_isolation as wi  # noqa: E402

# Globais do roteador mutados pelos caminhos sob teste; restaurados no teardown.
_RUN_GLOBALS = ("_RUN", "_ISO", "_LOG_FD", "_LOG_FAILED", "_LOG_REPORTED",
                "_LOG_ENDS_WITH_NEWLINE")


@pytest.fixture
def iso_cov(tmp_path, monkeypatch):
    """Estado em tmp_path, envs de isolamento limpos e globals do roteador zerados."""
    saved = {name: getattr(wr, name) for name in _RUN_GLOBALS}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    for key in ("WORKER_ASK_ISOLATION", "WORKER_ASK_ISOLATION_STRICT",
                "WORKER_ASK_STATE_FALLBACK", "ASK_PRESENTATION",
                "ASK_PRESENTATION_FD"):
        monkeypatch.delenv(key, raising=False)
    wr._LOG_FD = None
    wr._LOG_FAILED = False
    wr._LOG_REPORTED = False
    wr._LOG_ENDS_WITH_NEWLINE = True
    wr._RUN = None
    wr._ISO = None
    yield tmp_path
    for name, value in saved.items():
        setattr(wr, name, value)


@pytest.fixture
def run_cov(iso_cov):
    """_RUN preenchido (o código sob teste lê run_id e grava métricas nele)."""
    run = {"run_id": "run-cov-1", "attempts": []}
    wr._RUN = run
    return run


@pytest.fixture
def iso_ativo(iso_cov, run_cov):
    """_ISO preenchido com sessão falsa e deps reais do roteador."""
    sessao = fake_session(iso_cov)
    wr._ISO = {"wi": wi, "deps": wr._iso_deps(wi), "session": sessao}
    return sessao


def state_dir(tmp_path):
    return tmp_path / "st" / "worker-ask"


def fake_session(tmp_path):
    """Session válida (dataclass de worker_isolation) apontando para um slot falso."""
    slot = tmp_path / "slot"
    slot.mkdir(exist_ok=True)
    return wi.Session(real_cwd=str(tmp_path), real_top=str(tmp_path),
                      common_dir=str(tmp_path / "common"), repo_id="repo",
                      slot=str(slot), slot_cwd=str(slot), slot_index=0,
                      lock_fd=-1, run_id="run-cov-1")


def make_attempt(applied=(), conflicts=()):
    """Tentativa isolada no formato que _iso_begin devolve, com sync controlado."""
    return {"prep": None, "cwd": "", "prompt": b"", "fp_before": None,
            "fp_after": None,
            "sync": wi.SyncResult(applied=list(applied), conflicts=list(conflicts))}


def git_repo(tmp_path):
    """Repositório git real e mínimo (status marca tudo como não rastreado)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    return repo


# ---------------------------------------------------------------- _extract_run_result


def bloco(payload):
    return ("intro\n```json\n" + payload + "\n```\nfim\n").encode()


def test_extract_run_result_sem_bloco():
    assert wr._extract_run_result(b"saida sem marcador") is None


def test_extract_run_result_bloco_aberto():
    dados = b"```json\n{\"execution_state\": \"completed\"}"
    assert wr._extract_run_result(dados) is None  # sem fechamento ```


def test_extract_run_result_estado_valido():
    dados = bloco('{"execution_state": "completed"}')
    assert wr._extract_run_result(dados) == "completed"


def test_extract_run_result_ultimo_bloco_vence():
    dados = (bloco('{"execution_state": "completed"}')
             + bloco('{"execution_state": "partial"}'))
    assert wr._extract_run_result(dados) == "partial"


def test_extract_run_result_json_invalido():
    assert wr._extract_run_result(bloco("{quebrado")) is None


def test_extract_run_result_nao_dict():
    assert wr._extract_run_result(bloco("[1, 2]")) is None


def test_extract_run_result_estado_desconhecido():
    assert wr._extract_run_result(bloco('{"execution_state": "running"}')) is None


def test_extract_run_result_fora_da_janela():
    dados = bloco('{"execution_state": "completed"}') + b"x" * 300000
    assert wr._extract_run_result(dados) is None  # bloco além dos 256KiB finais


def test_extract_run_result_bytes_nao_utf8():
    dados = b"\xff\xfe" + bloco('{"execution_state": "blocked"}')
    assert wr._extract_run_result(dados) == "blocked"  # decode com replace


# ------------------------------------------- _count_task_runs / _find_last_task_run


def rec(task_id, result="completed", extra=None):
    linha = {"task_id": task_id, "result": result}
    if extra:
        linha.update(extra)
    return json.dumps(linha)


def test_count_task_runs_somente_orcamento(iso_cov):
    texto = "\n".join([
        rec("t1"),
        rec("t1", result="blocked_question"),   # não consome orçamento
        rec("t2"),                              # task_id diferente
        rec("t1", extra={"event": "redo"}),     # evento, não execução
        "linha invalida",                       # JSON quebrado
        "[1, 2]",                               # JSON válido, não dict
    ]) + "\n"
    write_run_file(iso_cov, texto)
    assert wr._count_task_runs("t1") == 1


def test_count_task_runs_soma_rotacao(iso_cov):
    write_run_file(iso_cov, rec("t1") + "\n")
    write_run_file(iso_cov, rec("t1") + "\n", name="runs.jsonl.1")
    assert wr._count_task_runs("t1") == 2


def test_count_task_runs_sem_arquivo(iso_cov):
    assert wr._count_task_runs("t1") == 0


def test_find_last_task_run_ultima_linha(iso_cov):
    write_run_file(iso_cov, rec("t1", extra={"executor": "antigo"}) + "\n"
                   + rec("t2") + "\n"
                   + rec("t1", extra={"executor": "novo"}) + "\n")
    ultima = wr._find_last_task_run("t1")
    assert ultima["executor"] == "novo"


def test_find_last_task_run_preferencia_runs_jsonl(iso_cov):
    write_run_file(iso_cov, rec("t1", extra={"executor": "atual"}) + "\n")
    write_run_file(iso_cov, rec("t1", extra={"executor": "antigo"}) + "\n",
                   name="runs.jsonl.1")
    assert wr._find_last_task_run("t1")["executor"] == "atual"


def test_find_last_task_run_cai_na_rotacao(iso_cov):
    write_run_file(iso_cov, '{"event": "redo", "task_id": "t1"}\n')
    write_run_file(iso_cov, rec("t1") + "\n", name="runs.jsonl.1")
    assert wr._find_last_task_run("t1")["task_id"] == "t1"


def test_find_last_task_run_nada_encontrado(iso_cov):
    assert wr._find_last_task_run("t1") is None


def test_runs_symlink_ignorado(iso_cov):
    write_run_file(iso_cov, rec("t1") + "\n", name="runs.jsonl.1")
    sd = state_dir(iso_cov)
    os.symlink(sd / "ausente.jsonl", sd / "runs.jsonl")  # O_NOFOLLOW recusa
    assert wr._count_task_runs("t1") == 1
    assert wr._find_last_task_run("t1")["task_id"] == "t1"


def write_run_file(tmp_path, texto, name="runs.jsonl"):
    sd = state_dir(tmp_path)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / name).write_text(texto)


# ---------------------------------------------------------------- _tree_fingerprint


def test_tree_fingerprint_none_fora_de_repo(iso_cov):
    assert wr._tree_fingerprint(str(iso_cov)) is None


def test_tree_fingerprint_arquivo_regular(iso_cov):
    repo = git_repo(iso_cov)
    (repo / "a.txt").write_bytes(b"conteudo")
    fp = wr._tree_fingerprint(str(repo))
    assert fp == {os.path.normpath(str(repo / "a.txt")):
                  hashlib.sha256(b"conteudo").hexdigest()}


def test_tree_fingerprint_symlink(iso_cov):
    repo = git_repo(iso_cov)
    (repo / "alvo.txt").write_text("x")
    os.symlink(repo / "alvo.txt", repo / "link.txt")
    fp = wr._tree_fingerprint(str(repo))
    assert fp[os.path.normpath(str(repo / "link.txt"))] == \
        "symlink:" + str(repo / "alvo.txt")


def test_tree_fingerprint_symlink_readlink_erro(iso_cov, monkeypatch):
    repo = git_repo(iso_cov)
    (repo / "alvo.txt").write_text("x")
    os.symlink(repo / "alvo.txt", repo / "link.txt")

    def readlink_quebrado(*args, **kwargs):
        raise OSError("fixture")
    monkeypatch.setattr(os, "readlink", readlink_quebrado)
    fp = wr._tree_fingerprint(str(repo))
    assert fp[os.path.normpath(str(repo / "link.txt"))] == "err"


def test_tree_fingerprint_caminho_sumido(iso_cov, monkeypatch):
    repo = git_repo(iso_cov)
    (repo / "someindo.txt").write_text("x")
    real_lstat = os.lstat

    def lstat_alvo(path, *args, **kwargs):
        if os.fspath(path).endswith("someindo.txt"):
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(os, "lstat", lstat_alvo)
    fp = wr._tree_fingerprint(str(repo))
    assert fp[os.path.normpath(str(repo / "someindo.txt"))] == "missing"


def test_tree_fingerprint_arquivo_grande(iso_cov, monkeypatch):
    repo = git_repo(iso_cov)
    (repo / "grande.txt").write_bytes(b"0123456789")
    monkeypatch.setattr(wr, "_ARVORE_MAX_BYTES", 4)
    fp = wr._tree_fingerprint(str(repo))
    assert fp[os.path.normpath(str(repo / "grande.txt"))].startswith("big:10:")


def test_tree_fingerprint_excesso_de_caminhos(iso_cov, monkeypatch):
    repo = git_repo(iso_cov)
    (repo / "a.txt").write_text("x")
    monkeypatch.setattr(wr, "_ARVORE_MAX_CAMINHOS", 0)
    assert wr._tree_fingerprint(str(repo)) is None


def test_tree_fingerprint_status_falha(iso_cov, monkeypatch):
    repo = git_repo(iso_cov)
    (repo / "a.txt").write_text("x")
    real_run = subprocess.run

    def run_falso(argv, *args, **kwargs):
        if "rev-parse" in argv:  # toplevel aponta para diretório inexistente
            falso = subprocess.CompletedProcess(argv, 0)
            falso.stdout = str(iso_cov / "nao-existe").encode() + b"\n"
            return falso
        return real_run(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run_falso)
    assert wr._tree_fingerprint(str(repo)) is None


def test_tree_fingerprint_close_com_erro_e_ignorado(iso_cov, monkeypatch):
    repo = git_repo(iso_cov)
    (repo / "a.txt").write_bytes(b"conteudo")
    real_open = os.open
    real_close = os.close
    marcados = []

    def open_rastreado(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if flags & getattr(os, "O_NOFOLLOW", 0) and \
                os.fspath(path).endswith("a.txt"):
            marcados.append(fd)  # só o fd do fingerprint falha no close
        return fd

    def close_seletivo(fd):
        if fd in marcados:
            raise OSError("fixture")
        return real_close(fd)
    monkeypatch.setattr(os, "open", open_rastreado)
    monkeypatch.setattr(os, "close", close_seletivo)
    fp = wr._tree_fingerprint(str(repo))
    assert fp[os.path.normpath(str(repo / "a.txt"))] == \
        hashlib.sha256(b"conteudo").hexdigest()


def test_tree_fingerprint_erro_externo(iso_cov, monkeypatch):
    repo = git_repo(iso_cov)
    (repo / "a.txt").write_text("x")

    def islink_quebrada(mode):
        raise OSError("fixture")
    monkeypatch.setattr(statmod, "S_ISLNK", islink_quebrada)
    fp = wr._tree_fingerprint(str(repo))
    assert fp[os.path.normpath(str(repo / "a.txt"))] == "err"


# ---------------------------------------------------------------- _iso_requested


def test_iso_requested_sem_pedido(iso_cov):
    assert wr._iso_requested(str(iso_cov), "plan") is None


def test_iso_requested_env_all(iso_cov, monkeypatch):
    monkeypatch.setenv("WORKER_ASK_ISOLATION", "all")
    assert wr._iso_requested(str(iso_cov), "plan") is wi


def test_iso_requested_level_write_em_write(iso_cov, monkeypatch):
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "write")
    assert wr._iso_requested(str(iso_cov), "write") is wi


def test_iso_requested_level_write_em_plan(iso_cov, monkeypatch):
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "write")
    assert wr._iso_requested(str(iso_cov), "plan") is None


def test_iso_requested_modulo_ausente_com_pedido(iso_cov, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_ISOLATION", "write")
    monkeypatch.setitem(sys.modules, "worker_isolation", None)
    with pytest.raises(SystemExit) as exc:
        wr._iso_requested(str(iso_cov), "write")
    assert exc.value.code == 1
    assert "worker_isolation.py ausente" in capsys.readouterr().err


def test_iso_requested_modulo_ausente_sem_pedido(iso_cov, monkeypatch):
    monkeypatch.setitem(sys.modules, "worker_isolation", None)
    assert wr._iso_requested(str(iso_cov), "plan") is None


def test_iso_requested_modulo_invalido_com_pedido(iso_cov, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_ISOLATION", "all")
    real_import = builtins.__import__

    def import_quebrado(name, *args, **kwargs):
        if name == "worker_isolation":
            raise RuntimeError("modulo quebrado")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", import_quebrado)
    with pytest.raises(SystemExit) as exc:
        wr._iso_requested(str(iso_cov), "plan")
    assert exc.value.code == 1
    assert "worker_isolation.py invalido" in capsys.readouterr().err


def test_iso_requested_modulo_invalido_sem_pedido(iso_cov, monkeypatch, capsys):
    real_import = builtins.__import__

    def import_quebrado(name, *args, **kwargs):
        if name == "worker_isolation":
            raise RuntimeError("modulo quebrado")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", import_quebrado)
    assert wr._iso_requested(str(iso_cov), "plan") is None
    assert "aviso: worker_isolation.py invalido" in capsys.readouterr().err


# ---------------------------------------------------------------- _iso_direct


def test_iso_direct_registra_motivo(run_cov):
    wr._iso_direct("slot ocupado")
    assert run_cov["isolation"] == "none"
    assert run_cov["isolation_reason"] == "slot ocupado"


def test_iso_direct_sem_run_nao_faz_nada(iso_cov, capsys):
    wr._iso_direct("qualquer")  # _RUN None: apenas segue
    assert wr._RUN is None
    assert capsys.readouterr().err == ""


def test_iso_direct_strict_para_com_rc1(run_cov, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_ISOLATION_STRICT", "1")
    with pytest.raises(SystemExit) as exc:
        wr._iso_direct("slot ocupado")
    assert exc.value.code == 1
    assert "isolamento indisponivel" in capsys.readouterr().err


# ---------------------------------------------------------------- _iso_start


def test_iso_start_sem_isolamento(iso_cov, run_cov, monkeypatch):
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "off")
    wr._iso_start("write", str(iso_cov), ["a.txt"], 100.0)
    assert wr._ISO is None
    assert "isolation" not in run_cov


def test_iso_start_abre_sessao(iso_cov, run_cov, monkeypatch):
    chamadas = {}
    sessao = fake_session(iso_cov)

    def abre(cwd, allowed, run_id, deps, deadline=None):
        chamadas.update(cwd=cwd, allowed=list(allowed), run_id=run_id,
                        deps=deps, deadline=deadline)
        return sessao
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "all")
    monkeypatch.setattr(wi, "open_session", abre)
    wr._iso_start("write", str(iso_cov), ["a.txt"], 55.0)
    assert wr._ISO is not None
    assert wr._ISO["session"] is sessao
    assert wr._ISO["wi"] is wi
    assert wr._ISO["deps"].max_file_bytes == wr.MAX_SNAPSHOT_BYTES
    assert chamadas["cwd"] == str(iso_cov)
    assert chamadas["allowed"] == ["a.txt"]
    assert chamadas["run_id"] == "run-cov-1"
    assert chamadas["deps"] is wr._ISO["deps"]
    assert chamadas["deadline"] == 55.0
    assert run_cov["isolation"] == "worktree"


def test_iso_start_direct_mode_vira_direto(iso_cov, run_cov, monkeypatch):
    def abre(*args, **kwargs):
        raise wi.DirectMode("nenhum slot livre")
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "all")
    monkeypatch.setattr(wi, "open_session", abre)
    wr._iso_start("write", str(iso_cov), ["a.txt"], None)
    assert wr._ISO is None
    assert run_cov["isolation"] == "none"
    assert run_cov["isolation_reason"] == "nenhum slot livre"


def test_iso_start_direct_mode_strict_sai_1(iso_cov, run_cov, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_ISOLATION_STRICT", "1")

    def abre(*args, **kwargs):
        raise wi.DirectMode("nenhum slot livre")
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "all")
    monkeypatch.setattr(wi, "open_session", abre)
    with pytest.raises(SystemExit) as exc:
        wr._iso_start("write", str(iso_cov), ["a.txt"], None)
    assert exc.value.code == 1
    assert "isolamento indisponivel" in capsys.readouterr().err


def test_iso_start_isolation_error_sai_1(iso_cov, run_cov, monkeypatch, capsys):
    def abre(*args, **kwargs):
        raise wi.IsolationError("git falhou")
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "all")
    monkeypatch.setattr(wi, "open_session", abre)
    with pytest.raises(SystemExit) as exc:
        wr._iso_start("write", str(iso_cov), ["a.txt"], None)
    assert exc.value.code == 1
    assert "erro: git falhou" in capsys.readouterr().err


def test_iso_start_state_dir_symlink_sai_1(iso_cov, run_cov, monkeypatch, capsys):
    st = iso_cov / "st"
    st.mkdir()
    os.symlink(iso_cov / "alvo", st / "worker-ask")
    monkeypatch.setattr(wi, "resolve_mode", lambda cwd: "all")
    with pytest.raises(SystemExit) as exc:
        wr._iso_start("write", str(iso_cov), ["a.txt"], None)
    assert exc.value.code == 1
    assert "recusado" in capsys.readouterr().err


# ---------------------------------------------------------------- _iso_begin


def test_iso_begin_sem_isolamento(iso_cov):
    assert wr._iso_begin(str(iso_cov), ["a.txt"], b"p") is None


def test_iso_begin_prepara_tentativa(iso_cov, run_cov, iso_ativo, monkeypatch):
    prep = wi.PrepInfo(snap_before={}, snap_slot_before={}, overlay_files=2,
                       skipped_sensitive=0, skipped_nested=0, links=[],
                       prep_s=0.25, started_ts=0.0)
    run_cov["isolation_prep_s"] = 1.0
    monkeypatch.setattr(wi, "prepare", lambda session, allowed, deps: prep)
    monkeypatch.setattr(wi, "isolation_notice", lambda session: "aviso")
    monkeypatch.setattr(wi, "rewrite_prompt",
                        lambda prompt, session, cwd: b"prompt-novo")
    resultado = wr._iso_begin(str(iso_cov / "real"), ["a.txt"], b"original")
    assert resultado["prep"] is prep
    assert resultado["cwd"] == str(iso_cov / "slot")
    assert resultado["prompt"] == b"aviso\nprompt-novo"
    assert resultado["fp_before"] is None  # slot fora de repo git
    assert resultado["fp_after"] is None
    assert resultado["sync"] is None
    assert run_cov["isolation_prep_s"] == 1.25
    assert run_cov["overlay_files"] == 2


def test_iso_begin_direct_mode_fecha_e_vira_direto(iso_cov, run_cov, iso_ativo,
                                                   monkeypatch):
    fechadas = []

    def prep_quebrado(session, allowed, deps):
        raise wi.DirectMode("slot sujo")
    monkeypatch.setattr(wi, "prepare", prep_quebrado)
    monkeypatch.setattr(wi, "close_session", lambda s: fechadas.append(s))
    assert wr._iso_begin(str(iso_cov), ["a.txt"], b"p") is None
    assert fechadas == [iso_ativo]
    assert wr._ISO is None
    assert run_cov["isolation"] == "none"
    assert run_cov["isolation_reason"] == "slot sujo"


def test_iso_begin_direct_mode_strict_sai_1(iso_cov, run_cov, iso_ativo,
                                            monkeypatch, capsys):
    monkeypatch.setenv("WORKER_ASK_ISOLATION_STRICT", "1")

    def prep_quebrado(session, allowed, deps):
        raise wi.DirectMode("slot sujo")
    monkeypatch.setattr(wi, "prepare", prep_quebrado)
    monkeypatch.setattr(wi, "close_session", lambda s: None)
    with pytest.raises(SystemExit) as exc:
        wr._iso_begin(str(iso_cov), ["a.txt"], b"p")
    assert exc.value.code == 1
    assert "isolamento indisponivel" in capsys.readouterr().err


def test_iso_begin_isolation_error_sai_1(iso_cov, run_cov, iso_ativo,
                                         monkeypatch, capsys):
    def prep_quebrado(session, allowed, deps):
        raise wi.IsolationError("falha na copia")
    monkeypatch.setattr(wi, "prepare", prep_quebrado)
    with pytest.raises(SystemExit) as exc:
        wr._iso_begin(str(iso_cov), ["a.txt"], b"p")
    assert exc.value.code == 1
    assert "erro: falha na copia" in capsys.readouterr().err


def test_iso_begin_runtime_error_sai_1(iso_cov, run_cov, iso_ativo,
                                       monkeypatch, capsys):
    def prep_quebrado(session, allowed, deps):
        raise RuntimeError("fixture")
    monkeypatch.setattr(wi, "prepare", prep_quebrado)
    with pytest.raises(SystemExit) as exc:
        wr._iso_begin(str(iso_cov), ["a.txt"], b"p")
    assert exc.value.code == 1
    assert "snapshot falhou" in capsys.readouterr().err


def test_iso_begin_prompt_excede_limite_sai_1(iso_cov, run_cov, iso_ativo,
                                              monkeypatch, capsys):
    monkeypatch.setattr(wi, "prepare",
                        lambda session, allowed, deps: wi.PrepInfo(
                            snap_before={}, snap_slot_before={}, overlay_files=0,
                            skipped_sensitive=0, skipped_nested=0, links=[],
                            prep_s=0.0, started_ts=0.0))
    monkeypatch.setattr(wi, "isolation_notice", lambda session: "aviso")
    monkeypatch.setattr(wi, "rewrite_prompt",
                        lambda prompt, session, cwd: b"x" * wr.MAX_INPUT_BYTES)
    with pytest.raises(SystemExit) as exc:
        wr._iso_begin(str(iso_cov), ["a.txt"], b"p")
    assert exc.value.code == 1
    assert "excede 64KiB" in capsys.readouterr().err


# ---------------------------------------------------------------- _iso_report


def test_iso_report_contadores_sync(iso_cov, run_cov, iso_ativo, capsys):
    wr._iso_report(make_attempt(applied=["a.txt", "b.txt"]), "codex-ask",
                   "write", str(iso_cov), ["a.txt"], None, 0.0, 0, [])
    assert run_cov["sync_applied"] == 2
    assert run_cov["sync_conflicts"] == 0
    assert capsys.readouterr().err == ""  # árvore real fora de git: nada a vigiar


def test_iso_report_oos_guarda_descartados(iso_cov, run_cov, iso_ativo,
                                           monkeypatch, capsys):
    destino = iso_cov / "rejeitados"
    monkeypatch.setattr(wi, "save_rejected",
                        lambda s, oos, deps: wi.RejectedInfo(dir=str(destino),
                                                             saved=2, skipped=0))
    wr._iso_report(make_attempt(), "codex-ask", "write", str(iso_cov),
                   ["a.txt"], None, 0.0, 2, ["x.py", "y.py"])
    err = capsys.readouterr().err
    assert "fora do escopo" in err
    assert "x.py, y.py" in err
    assert "copia em " + str(destino) in err
    assert run_cov["oos_discarded"] == 2
    assert run_cov["sync_applied"] == 0


def test_iso_report_oos_modo_plan(iso_cov, run_cov, iso_ativo, monkeypatch, capsys):
    monkeypatch.setattr(wi, "save_rejected",
                        lambda s, oos, deps: wi.RejectedInfo(dir=None, saved=0,
                                                             skipped=1))
    wr._iso_report(make_attempt(), "codex-ask", "plan", str(iso_cov),
                   [], None, 0.0, 1, ["y.py"])
    err = capsys.readouterr().err
    assert "em modo plan" in err
    assert "sem copia" in err


def test_iso_report_conflito_sai_1(iso_cov, run_cov, iso_ativo, capsys):
    with pytest.raises(SystemExit) as exc:
        wr._iso_report(make_attempt(conflicts=["f.txt"]), "codex-ask", "write",
                       str(iso_cov), ["a.txt"], None, 0.0, 0, [])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "conflito na devolucao" in err
    assert "f.txt" in err
    assert run_cov["sync_conflicts"] == 1


def test_iso_report_arvore_real_mudou(iso_cov, run_cov, iso_ativo, monkeypatch,
                                      capsys):
    real = iso_cov / "real"
    real.mkdir()
    alvo_abs = os.path.normpath(str(real / "outro.txt"))
    antes = {alvo_abs: "sha-1"}
    monkeypatch.setattr(wr, "_tree_fingerprint", lambda cwd: {alvo_abs: "sha-2"})
    monkeypatch.setattr(wi, "synced_by_others", lambda s, ts, deps: set())
    wr._iso_report(make_attempt(applied=["a.txt"]), "codex-ask", "write",
                   str(real), ["a.txt"], antes, 0.0, 0, [])
    assert run_cov["real_tree_changed"] == 1
    assert "arvore real mudou" in capsys.readouterr().err


def test_iso_report_sync_de_outros_descarta(iso_cov, run_cov, iso_ativo,
                                            monkeypatch, capsys):
    real = iso_cov / "real"
    real.mkdir()
    alvo_abs = os.path.normpath(str(real / "outro.txt"))
    antes = {alvo_abs: "sha-1"}
    monkeypatch.setattr(wr, "_tree_fingerprint", lambda cwd: {alvo_abs: "sha-2"})
    monkeypatch.setattr(wi, "synced_by_others", lambda s, ts, deps: {alvo_abs})
    wr._iso_report(make_attempt(applied=["a.txt"]), "codex-ask", "write",
                   str(real), ["a.txt"], antes, 0.0, 0, [])
    assert "real_tree_changed" not in run_cov
    assert capsys.readouterr().err == ""


def test_iso_report_filtro_wa_sync(iso_cov, run_cov, iso_ativo, monkeypatch,
                                   capsys):
    real = iso_cov / "real"
    real.mkdir()
    alvo_abs = os.path.normpath(str(real / ".wa-sync-registro"))
    antes = {alvo_abs: "sha-1"}
    monkeypatch.setattr(wr, "_tree_fingerprint", lambda cwd: {alvo_abs: "sha-2"})
    monkeypatch.setattr(wi, "synced_by_others", lambda s, ts, deps: set())
    wr._iso_report(make_attempt(applied=["a.txt"]), "codex-ask", "write",
                   str(real), ["a.txt"], antes, 0.0, 0, [])
    assert "real_tree_changed" not in run_cov  # devolução própria não é concorrência
    assert capsys.readouterr().err == ""


def test_iso_report_excecao_interna_engolida(iso_cov, run_cov, iso_ativo,
                                             monkeypatch, capsys):
    def quebrado(*args, **kwargs):
        raise ValueError("boom")
    monkeypatch.setattr(wi, "save_rejected", quebrado)
    wr._iso_report(make_attempt(), "codex-ask", "write", str(iso_cov),
                   ["a.txt"], None, 0.0, 2, ["x.py"])
    assert "sync_applied" not in run_cov  # exceção abortou antes dos contadores
    assert capsys.readouterr().err == ""
