"""Ramos de erro do worker_isolation: guarda, sessao, preparacao, devolucao, rejeitados e gc.

Um teste por comportamento; todo efeito observado com assert e nada escapa de tmp_path.
Base (imports, fixtures e auxiliares) copiada de tests/test_isolation_sync.py.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_isolation as wi  # noqa: E402
import worker_router as wr  # noqa: E402


def _git(repo, *args, timeout=30):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, env=dict(os.environ), timeout=timeout)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r


def _deps_em(estado, **trocas):
    base = dict(state_dir=lambda: estado, is_sensitive=wr._is_sensitive,
                snapshot_allowed=wr.snapshot_allowed, parse_status=wr._parse_status_z,
                max_file_bytes=wr.MAX_SNAPSHOT_BYTES, max_paths=wr._ARVORE_MAX_CAMINHOS)
    base.update(trocas)
    return wi.Deps(**base)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    (r / "b.txt").write_text("b\n", encoding="utf-8")
    (r / "fora.txt").write_text("fora\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def deps(tmp_path):
    return _deps_em(tmp_path / "state")


def _abrir(repo, deps, allowed, run_id="run1", cwd=None):
    """Abre a sessão e prepara o slot; devolve (sessao, prep)."""
    s = wi.open_session(str(cwd or repo), allowed, run_id, deps, environ={})
    try:
        return s, wi.prepare(s, allowed, deps, environ={})
    except BaseException:
        wi.close_session(s)
        raise


def _repo_id(repo):
    return hashlib.sha256(wi._common_dir(str(repo)).encode()).hexdigest()[:16]


def _envelhecer(caminho, dias=20):
    t = time.time() - dias * 86400
    os.utime(caminho, (t, t))


def _falso_git(monkeypatch, alvo):
    """Substitui wi._git por um falso que falha (rc 1) só quando `alvo(args)` é verdade."""
    original = wi._git

    def falso(args, cwd, **kw):
        if alvo(args):
            return subprocess.CompletedProcess(args, 1, b"", b"falhou")
        return original(args, cwd, **kw)

    monkeypatch.setattr(wi, "_git", falso)


# --- guarda e sessao -------------------------------------------------------


def test_guard_slot_recusa_diretorio_que_nao_e_worktree(deps, tmp_path):
    slot = tmp_path / "state" / "worktrees" / "qualquer" / "slot-0"
    slot.mkdir(parents=True)
    with pytest.raises(wi.SlotGuardError, match="nao e worktree git"):
        wi.guard_slot(str(slot), str(tmp_path), deps)


def test_guard_slot_recusa_quando_git_common_dir_falha(repo, deps, monkeypatch):
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        _falso_git(monkeypatch, lambda args: "--git-common-dir" in args)
        with pytest.raises(wi.SlotGuardError, match="git-common-dir falhou"):
            wi.guard_slot(s.slot, s.common_dir, deps)
    finally:
        wi.close_session(s)


def test_guard_slot_repo_comum_diverge(deps, tmp_path):
    slot = tmp_path / "state" / "worktrees" / "qualquer" / "slot-0"
    slot.mkdir(parents=True)
    _git(slot, "init", "-q")
    with pytest.raises(wi.SlotGuardError, match="common-dir do slot diverge"):
        wi.guard_slot(str(slot), str(tmp_path / "outro-common"), deps)


def test_open_session_recusa_worktrees_symlink(repo, deps, tmp_path):
    estado = deps.state_dir()
    estado.mkdir()
    fora = tmp_path / "fora"
    fora.mkdir()
    os.symlink(str(fora), str(estado / "worktrees"))
    with pytest.raises(wi.IsolationError, match="worktrees e symlink"):
        wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})


def test_open_session_recusa_diretorio_do_repositorio_symlink(repo, deps, tmp_path):
    estado = deps.state_dir()
    (estado / "worktrees").mkdir(parents=True)
    fora = tmp_path / "fora"
    fora.mkdir()
    os.symlink(str(fora), str(estado / "worktrees" / _repo_id(repo)))
    with pytest.raises(wi.IsolationError, match="repositorio e symlink"):
        wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})


def test_open_session_converte_oserror_de_estado_em_isolationerror(repo, deps, monkeypatch):
    estado = deps.state_dir()
    original = os.makedirs

    def falso(path, *a, **k):
        if os.fspath(path) == str(estado):
            raise OSError("disco cheio")
        return original(path, *a, **k)

    monkeypatch.setattr(wi.os, "makedirs", falso)
    with pytest.raises(wi.IsolationError) as exc:
        wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    assert not isinstance(exc.value, OSError)
    assert "diretorio de estado" in str(exc.value)


def test_open_session_recusa_run_id_invalido(repo, deps):
    for run_id in ("a/b", ""):
        with pytest.raises(wi.IsolationError, match="run_id invalido"):
            wi.open_session(str(repo), ["a.txt"], run_id, deps, environ={})


def test_open_session_sem_slot_utilizavel_preserva_slot_existente(repo, deps, monkeypatch):
    rd = deps.state_dir() / "worktrees" / _repo_id(repo)
    (rd / "slot-0").mkdir(parents=True)
    monkeypatch.setattr(wi, "SLOT_WAIT_S", 0.2)
    with pytest.raises(wi.IsolationError, match="nenhum slot utilizavel"):
        wi.open_session(str(repo), ["a.txt"], "run1", deps,
                        environ={"WORKER_ASK_ISOLATION_SLOTS": "1"})
    assert (rd / "slot-0").is_dir()


def test_open_session_falha_worktree_add_libera_trava(repo, deps, monkeypatch):
    original = wi._git

    def falso(args, cwd, **kw):
        if "worktree" in args and "add" in args:
            return subprocess.CompletedProcess(args, 1, b"", b"falhou")
        return original(args, cwd, **kw)

    monkeypatch.setattr(wi, "SLOT_WAIT_S", 0.2)
    wi._git = falso
    try:
        with pytest.raises(wi.IsolationError, match="falha ao criar slot"):
            wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    finally:
        wi._git = original
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        assert os.path.isdir(s.slot)
    finally:
        wi.close_session(s)


def test_open_session_falha_common_dir_vira_isolationerror(repo, deps, monkeypatch):
    _falso_git(monkeypatch, lambda args: "--git-common-dir" in args)
    with pytest.raises(wi.IsolationError, match="falha ao obter git-common-dir"):
        wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})


def test_open_session_direct_mode_no_commit(deps, tmp_path):
    r = tmp_path / "semcommit"
    r.mkdir()
    _git(r, "init", "-q")
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(r), ["x.txt"], "run1", deps, environ={})
    assert exc.value.reason == "no_commit"


def test_open_session_direct_mode_not_git(deps, tmp_path):
    r = tmp_path / "naogit"
    r.mkdir()
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(r), ["x.txt"], "run1", deps, environ={})
    assert exc.value.reason == "not_git"


def test_open_session_direct_mode_cwd_ignored(repo, deps):
    (repo / ".gitignore").write_text("sub/\n", encoding="utf-8")
    sub = repo / "sub"
    sub.mkdir()
    (sub / "x.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(sub), ["x.txt"], "run1", deps, environ={})
    assert exc.value.reason == "cwd_ignored"


def test_open_session_direct_mode_nested_repo(repo):
    sub = repo / "sub"
    sub.mkdir()
    (sub / ".git").mkdir()
    (sub / "x.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(repo), ["sub/x.txt"], "run1", _deps_em(repo.parent / "state"),
                        environ={})
    assert exc.value.reason == "nested_repo"


def test_close_session_idempotente_com_trava_sumida(repo, deps):
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    lock = os.path.join(os.path.dirname(s.slot), f"slot-{s.slot_index}.lock")
    os.unlink(lock)
    wi.close_session(s)
    wi.close_session(s)
    assert not os.path.exists(lock)
    with pytest.raises(OSError):
        os.fstat(s.lock_fd)


def test_resolve_mode_marcador_grande_vira_off(repo):
    marcador = repo / ".worker-ask-isolation"
    marcador.write_text("write\n", encoding="utf-8")
    assert wi.resolve_mode(str(repo), environ={}) == "write"
    marcador.write_text("write" + "x" * 100, encoding="utf-8")
    assert wi.resolve_mode(str(repo), environ={}) == "off"


def test_resolve_mode_marcador_symlink_vira_off(repo):
    os.symlink("write", str(repo / ".worker-ask-isolation"))
    assert wi.resolve_mode(str(repo), environ={}) == "off"


def test_resolve_mode_marcador_ilegivel_vira_off(repo, monkeypatch):
    (repo / ".worker-ask-isolation").write_text("write\n", encoding="utf-8")

    def falso(fd, n):
        raise OSError("ilegivel")

    monkeypatch.setattr(wi.os, "read", falso)
    assert wi.resolve_mode(str(repo), environ={}) == "off"


def test_slot_count_valor_invalido_usa_padrao():
    for bruto in ("abc", "0", "33", ""):
        assert wi.slot_count({"WORKER_ASK_ISOLATION_SLOTS": bruto}) == wi.DEFAULT_SLOTS
    assert wi.slot_count({"WORKER_ASK_ISOLATION_SLOTS": "3"}) == 3


# --- preparacao ------------------------------------------------------------


def test_prepare_falha_status_vira_isolationerror(repo, deps, monkeypatch):
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        _falso_git(monkeypatch, lambda args: args[0] == "status")
        with pytest.raises(wi.IsolationError, match="falha ao ler status do repositorio"):
            wi.prepare(s, ["a.txt"], deps, environ={})
    finally:
        wi.close_session(s)


def test_prepare_falha_head_sha_vira_isolationerror(repo, deps, monkeypatch):
    s, _prep = _abrir(repo, deps, ["a.txt"])
    try:
        _falso_git(monkeypatch, lambda args: args[:2] == ["rev-parse", "--verify"])
        with pytest.raises(wi.IsolationError, match="falha ao limpar slot"):
            wi.prepare(s, ["a.txt"], deps, environ={})
    finally:
        wi.close_session(s)


def test_prepare_falha_clean_vira_isolationerror(repo, deps, monkeypatch):
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        _falso_git(monkeypatch, lambda args: args[0] == "clean")
        with pytest.raises(wi.IsolationError, match="falha ao limpar slot"):
            wi.prepare(s, ["a.txt"], deps, environ={})
    finally:
        wi.close_session(s)


def test_prepare_oserror_vira_isolationerror(repo, deps, monkeypatch):
    os.symlink("a.txt", str(repo / "link"))

    def falso(*a, **k):
        raise OSError("disco cheio")

    monkeypatch.setattr(wi.os, "symlink", falso)
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        with pytest.raises(wi.IsolationError) as exc:
            wi.prepare(s, ["a.txt"], deps, environ={})
        assert not isinstance(exc.value, OSError)
        assert "falha de arquivo" in str(exc.value)
    finally:
        wi.close_session(s)


def test_prepare_autorizado_mudou_durante_preparacao(repo, deps, tmp_path):
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:

        def falso(cwd, allowed):
            if cwd == s.real_cwd:
                return {rel: (True, "h1", 0o644) for rel in allowed}
            return {rel: (True, "h2", 0o644) for rel in allowed}

        deps2 = _deps_em(tmp_path / "state", snapshot_allowed=falso)
        with pytest.raises(wi.IsolationError, match="mudou durante a preparacao"):
            wi.prepare(s, ["a.txt"], deps2, environ={})
    finally:
        wi.close_session(s)


def test_overlay_copia_symlink_nao_rastreado_com_mesmo_alvo(repo, deps):
    os.symlink("a.txt", str(repo / "link"))
    s, info = _abrir(repo, deps, ["a.txt"])
    try:
        assert os.readlink(os.path.join(s.slot_cwd, "link")) == "a.txt"
        assert info.overlay_files >= 1
    finally:
        wi.close_session(s)


def test_overlay_remove_arquivo_rastreado_apagado(repo, deps):
    (repo / "a.txt").unlink()
    s, info = _abrir(repo, deps, ["a.txt"])
    try:
        assert not os.path.exists(os.path.join(s.slot_cwd, "a.txt"))
        assert info.overlay_files == 1
    finally:
        wi.close_session(s)


def test_overlay_arquivo_grande_vira_overlay_limit(repo, deps, tmp_path):
    deps2 = _deps_em(tmp_path / "state", max_file_bytes=10)
    (repo / "grande.txt").write_text("x" * 100, encoding="utf-8")
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps2, environ={})
    try:
        with pytest.raises(wi.DirectMode) as exc:
            wi.prepare(s, ["a.txt"], deps2, environ={})
        assert exc.value.reason == "overlay_limit"
    finally:
        wi.close_session(s)


def test_overlay_arquivo_vira_diretorio_e_vice_versa(repo, deps):
    d = repo / "d"
    d.mkdir()
    (d / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "d")
    (repo / "a.txt").unlink()
    (repo / "a.txt").mkdir()
    (repo / "a.txt" / "inner.txt").write_text("interior\n", encoding="utf-8")
    shutil.rmtree(d)
    d.write_text("agora arquivo\n", encoding="utf-8")
    s, _prep = _abrir(repo, deps, ["b.txt"])
    try:
        slot = Path(s.slot_cwd)
        assert (slot / "a.txt").is_dir()
        assert (slot / "a.txt" / "inner.txt").read_text(encoding="utf-8") == "interior\n"
        assert (slot / "d").is_file()
        assert (slot / "d").read_text(encoding="utf-8") == "agora arquivo\n"
    finally:
        wi.close_session(s)


def test_max_overlay_invalido_usa_padrao(repo, deps):
    (repo / "novo.txt").write_text("conteudo\n", encoding="utf-8")
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        info = wi.prepare(s, ["a.txt"], deps,
                          environ={"WORKER_ASK_ISOLATION_MAX_OVERLAY": "abc"})
        assert isinstance(info, wi.PrepInfo)
        assert info.overlay_files >= 1
    finally:
        wi.close_session(s)
    bruto = {"WORKER_ASK_ISOLATION_MAX_OVERLAY": "abc"}
    assert wi._max_overlay(bruto) == wi.DEFAULT_MAX_OVERLAY


def test_overlay_origem_sumida_no_copy_trata_como_removida(repo, deps, tmp_path, monkeypatch):
    deps2 = _deps_em(tmp_path / "state",
                     snapshot_allowed=lambda cwd, allowed: {r: (True, "h", 0o644) for r in allowed})
    (repo / "a.txt").write_text("sujosujosujo\n", encoding="utf-8")
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps2, environ={})
    try:
        src = os.path.join(s.real_cwd, "a.txt")
        original = os.open

        def falso_open(path, flags, *a, **k):
            if os.fspath(path) == src:
                raise FileNotFoundError(2, "sumiu")
            return original(path, flags, *a, **k)

        monkeypatch.setattr(wi.os, "open", falso_open)
        info = wi.prepare(s, ["a.txt"], deps2, environ={})
        monkeypatch.undo()
        assert info.overlay_files >= 1
        assert not os.path.exists(os.path.join(s.slot_cwd, "a.txt"))
    finally:
        wi.close_session(s)


def test_overlay_symlink_ilegivel_remove_destino(repo, deps, monkeypatch):
    os.symlink("a.txt", str(repo / "link"))
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        src = os.path.join(s.real_top, "link")
        original = os.readlink

        def falso(path, *a, **k):
            if os.fspath(path) == src:
                raise OSError("ilegivel")
            return original(path, *a, **k)

        monkeypatch.setattr(wi.os, "readlink", falso)
        info = wi.prepare(s, ["a.txt"], deps, environ={})
        monkeypatch.undo()
        assert info.overlay_files >= 1
        assert not os.path.lexists(os.path.join(s.slot_cwd, "link"))
    finally:
        wi.close_session(s)


def test_create_links_diretorio_nao_ignorado_nao_vira_link(repo, deps):
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "l.txt").write_text("l\n", encoding="utf-8")
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        info = wi.prepare(s, ["a.txt"], deps,
                          environ={"WORKER_ASK_ISOLATION_LINKS": ".env:node_modules"})
        assert info.links == []
    finally:
        wi.close_session(s)


# --- devolucao (sync_back) -------------------------------------------------


def test_sync_back_ancestral_symlink_vira_conflito_e_taint(repo, deps, tmp_path):
    sub = repo / "sub"
    sub.mkdir()
    (sub / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "sub")
    s, prep = _abrir(repo, deps, ["sub/x.txt"])
    try:
        alvo = tmp_path / "alvo"
        alvo.mkdir()
        shutil.rmtree(sub)
        os.symlink(str(alvo), str(sub))
        (Path(s.slot_cwd) / "sub" / "x.txt").write_text("novo\n", encoding="utf-8")
        resultado = wi.sync_back(s, ["sub/x.txt"], prep, deps)
        assert resultado.conflicts == ["sub/x.txt"]
        assert resultado.applied == []
        assert s.tainted is True
        assert not (alvo / "x.txt").exists()
        assert os.path.islink(sub)
    finally:
        wi.close_session(s)


def test_sync_back_oserror_no_replace_vira_conflito_sem_temporario(repo, deps, monkeypatch):
    s, prep = _abrir(repo, deps, ["a.txt"])
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("novo\n", encoding="utf-8")

        def falso(src, dst, *a, **k):
            raise OSError("negado")

        monkeypatch.setattr(wi.os, "replace", falso)
        resultado = wi.sync_back(s, ["a.txt"], prep, deps)
        monkeypatch.undo()
        assert resultado.conflicts == ["a.txt"]
        assert resultado.applied == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
        sobras = [n for n in os.listdir(repo) if ".wa-sync-" in n]
        assert sobras == []
    finally:
        wi.close_session(s)


def test_sync_back_arquivo_slot_virou_diretorio_vira_conflito(repo, deps):
    s, prep = _abrir(repo, deps, ["a.txt"])
    try:
        os.unlink(os.path.join(s.slot_cwd, "a.txt"))
        os.mkdir(os.path.join(s.slot_cwd, "a.txt"))
        resultado = wi.sync_back(s, ["a.txt"], prep, deps)
        assert resultado.conflicts == ["a.txt"]
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
    finally:
        wi.close_session(s)


def test_sync_back_runtimeerror_snapshot_arvore_real_vira_conflito(repo, deps, tmp_path):
    s, prep = _abrir(repo, deps, ["a.txt"])
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("novo\n", encoding="utf-8")

        def falso(cwd, allowed):
            if cwd == s.real_cwd:
                raise RuntimeError("boom")
            return wr.snapshot_allowed(cwd, allowed)

        deps2 = _deps_em(tmp_path / "state", snapshot_allowed=falso)
        resultado = wi.sync_back(s, ["a.txt"], prep, deps2)
        assert resultado.conflicts == ["a.txt"]
        assert resultado.applied == []
        assert s.tainted is True
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
    finally:
        wi.close_session(s)


def test_sync_back_valueerror_generico_isola_apenas_um_arquivo(repo, deps, tmp_path):
    s, prep = _abrir(repo, deps, ["a.txt", "b.txt"])
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("novo a\n", encoding="utf-8")
        (Path(s.slot_cwd) / "b.txt").write_text("novo b\n", encoding="utf-8")

        def falso(cwd, allowed):
            if cwd == s.slot_cwd and "a.txt" in allowed:
                raise ValueError("boom")
            return wr.snapshot_allowed(cwd, allowed)

        deps2 = _deps_em(tmp_path / "state", snapshot_allowed=falso)
        resultado = wi.sync_back(s, ["a.txt", "b.txt"], prep, deps2)
        assert resultado.conflicts == ["a.txt"]
        assert resultado.applied == ["b.txt"]
        assert (repo / "b.txt").read_text(encoding="utf-8") == "novo b\n"
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
    finally:
        wi.close_session(s)


def test_sync_back_unlink_real_com_oserror_vira_conflito(repo, deps, monkeypatch):
    s, prep = _abrir(repo, deps, ["a.txt"])
    try:
        os.unlink(os.path.join(s.slot_cwd, "a.txt"))
        dst = os.path.join(s.real_cwd, "a.txt")
        original = os.unlink

        def falso(path, *a, **k):
            if os.fspath(path) == dst:
                raise OSError("negado")
            return original(path, *a, **k)

        monkeypatch.setattr(wi.os, "unlink", falso)
        resultado = wi.sync_back(s, ["a.txt"], prep, deps)
        monkeypatch.undo()
        assert resultado.conflicts == ["a.txt"]
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
    finally:
        wi.close_session(s)


def test_sync_back_fchmod_com_oserror_ainda_aplica(repo, deps, monkeypatch):
    s, prep = _abrir(repo, deps, ["a.txt"])
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("novo\n", encoding="utf-8")

        def falso(fd, modo):
            raise OSError("negado")

        monkeypatch.setattr(wi.os, "fchmod", falso)
        resultado = wi.sync_back(s, ["a.txt"], prep, deps)
        monkeypatch.undo()
        assert resultado.applied == ["a.txt"]
        assert (repo / "a.txt").read_text(encoding="utf-8") == "novo\n"
    finally:
        wi.close_session(s)


def test_sync_back_sem_trava_do_repositorio_nao_devolve(repo, deps):
    # sync.lock inutilizável (diretório): sem exclusão mútua a devolução inteira
    # vira conflito em vez de escrever na árvore real sem serialização.
    s, prep = _abrir(repo, deps, ["a.txt"])
    try:
        os.makedirs(os.path.join(os.path.dirname(s.slot), "sync.lock"))
        (Path(s.slot_cwd) / "a.txt").write_text("novo\n", encoding="utf-8")
        resultado = wi.sync_back(s, ["a.txt"], prep, deps)
        assert resultado.applied == []
        assert resultado.conflicts == ["a.txt"]
        assert s.tainted is True
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
        assert not os.path.exists(os.path.join(os.path.dirname(s.slot), "sync.jsonl"))
    finally:
        wi.close_session(s)


def test_sync_back_diario_ilegivel_nao_levanta(repo, deps):
    s, prep = _abrir(repo, deps, ["a.txt"])
    try:
        os.makedirs(os.path.join(os.path.dirname(s.slot), "sync.jsonl"))
        (Path(s.slot_cwd) / "a.txt").write_text("novo\n", encoding="utf-8")
        resultado = wi.sync_back(s, ["a.txt"], prep, deps)
        assert resultado.applied == ["a.txt"]
        assert (repo / "a.txt").read_text(encoding="utf-8") == "novo\n"
    finally:
        wi.close_session(s)


def test_synced_by_others_ignora_linhas_invalidas(repo, deps):
    s = wi.open_session(str(repo), ["a.txt"], "run1", deps, environ={})
    try:
        diario = os.path.join(os.path.dirname(s.slot), "sync.jsonl")
        linhas = [
            '{"ts": "texto", "run_id": "outro", "paths": ["/p1"]}',
            '{"ts": true, "run_id": "outro", "paths": ["/p2"]}',
            '{"ts": 5, "run_id": 123, "paths": ["/p3"]}',
            '{"ts": 5, "run_id": "outro", "paths": "nao-lista"}',
            '{"ts": 5, "run_id": "outro", "paths": [42]}',
            "[1, 2]",
            "linha que nao e json",
            '{"ts": 5000000000, "run_id": "outro", "paths": ["/ok"]}',
            '{"ts": 5000000000, "run_id": "run1", "paths": ["/proprio"]}',
        ]
        with open(diario, "w", encoding="utf-8") as fh:
            fh.write("\n".join(linhas) + "\n")
        assert wi.synced_by_others(s, 0, deps) == {"/ok"}
    finally:
        wi.close_session(s)


def test_sync_back_caminho_com_parent_vira_conflito(repo, deps):
    sub = repo / "sub"
    sub.mkdir()
    (sub / "marca.txt").write_text("m\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "sub")
    allowed = ["sub/../a.txt", "sub/../b.txt"]
    # A sessão abre com caminhos limpos; a devolução recebe os caminhos com "..".
    s = wi.open_session(str(repo), ["a.txt", "b.txt"], "run1", deps, environ={})
    try:
        snap = wr.snapshot_allowed(str(repo), allowed)
        prep = SimpleNamespace(snap_before=snap, snap_slot_before=snap)
        (Path(s.slot_cwd) / "a.txt").write_text("novo\n", encoding="utf-8")
        os.unlink(os.path.join(s.slot_cwd, "b.txt"))
        resultado = wi.sync_back(s, allowed, prep, deps)
        assert set(resultado.conflicts) == set(allowed)
        assert resultado.applied == []
        assert s.tainted is True
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
        assert (repo / "b.txt").exists()
    finally:
        wi.close_session(s)


# --- rejeitados ------------------------------------------------------------


def test_save_rejected_subdiretorio_modos_0700_0600(repo, deps):
    s, _prep = _abrir(repo, deps, ["b.txt"])
    try:
        base = Path(s.slot_cwd) / "sub" / "dir"
        base.mkdir(parents=True)
        (base / "x.txt").write_text("lixo\n", encoding="utf-8")
        info = wi.save_rejected(s, ["sub/dir/x.txt"], deps)
        assert info.saved == 1
        assert info.skipped == 0
        run_dir = deps.state_dir() / "rejected" / "run1"
        destino = run_dir / "sub" / "dir" / "x.txt"
        assert destino.read_text(encoding="utf-8") == "lixo\n"
        assert stat.S_IMODE(os.stat(run_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(run_dir / "sub").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(run_dir / "sub" / "dir").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(destino).st_mode) == 0o600
    finally:
        wi.close_session(s)


def test_save_rejected_rejected_symlink_salva_nada(repo, deps, tmp_path):
    estado = deps.state_dir()
    estado.mkdir()
    fora = tmp_path / "fora"
    fora.mkdir()
    os.symlink(str(fora), str(estado / "rejected"))
    s, _prep = _abrir(repo, deps, ["a.txt"])
    try:
        info = wi.save_rejected(s, ["a.txt"], deps)
        assert info.saved == 0
        assert info.skipped == 1
        assert info.dir is None
        assert list(fora.iterdir()) == []
    finally:
        wi.close_session(s)


def test_save_rejected_ancestral_symlink_no_slot_pulado(repo, deps, tmp_path):
    alvo = tmp_path / "alvo"
    alvo.mkdir()
    s, _prep = _abrir(repo, deps, ["b.txt"])
    try:
        os.symlink(str(alvo), os.path.join(s.slot_cwd, "sub"))
        info = wi.save_rejected(s, ["sub/x.txt"], deps)
        assert info.saved == 0
        assert info.skipped == 1
        assert list(alvo.iterdir()) == []
    finally:
        wi.close_session(s)


def test_save_rejected_segunda_chamada_respeita_limite(repo, deps, monkeypatch):
    monkeypatch.setattr(wi, "REJECTED_MAX_FILES", 1)
    s, _prep = _abrir(repo, deps, ["a.txt"])
    try:
        primeira = wi.save_rejected(s, ["a.txt"], deps)
        assert primeira.saved == 1
        segunda = wi.save_rejected(s, ["b.txt"], deps)
        assert segunda.saved == 0
        assert segunda.skipped == 1
        run_dir = deps.state_dir() / "rejected" / "run1"
        arquivos = [p.name for p in run_dir.rglob("*") if p.is_file()]
        assert arquivos == ["a.txt"]
    finally:
        wi.close_session(s)


def test_save_rejected_state_dir_com_excecao_salva_nada(repo, deps, tmp_path):
    def _falha():
        raise RuntimeError("falha de estado")

    deps2 = _deps_em(tmp_path / "state", state_dir=_falha)
    s, _prep = _abrir(repo, deps, ["a.txt"])
    try:
        info = wi.save_rejected(s, ["a.txt", "b.txt"], deps2)
        assert info.saved == 0
        assert info.skipped == 2
        assert info.dir is None
    finally:
        wi.close_session(s)


def test_save_rejected_destino_existente_pulado(repo, deps):
    s, _prep = _abrir(repo, deps, ["a.txt"])
    try:
        run_dir = deps.state_dir() / "rejected" / "run1"
        run_dir.mkdir(parents=True)
        (run_dir / "a.txt").write_text("pre-existe\n", encoding="utf-8")
        info = wi.save_rejected(s, ["a.txt"], deps)
        assert info.saved == 0
        assert info.skipped == 1
        assert (run_dir / "a.txt").read_text(encoding="utf-8") == "pre-existe\n"
    finally:
        wi.close_session(s)


# --- list_slots e gc -------------------------------------------------------


def test_list_slots_sem_estado_e_repo_json_invalido(deps, tmp_path):
    assert wi.list_slots(_deps_em(tmp_path / "nada")) == []
    estado = deps.state_dir()
    repo_dir = estado / "worktrees" / "r1"
    repo_dir.mkdir(parents=True)
    (repo_dir / "repo.json").write_text("{{{", encoding="utf-8")
    (repo_dir / "slot-0.lock").write_text("", encoding="utf-8")
    (repo_dir / "slot-1.lock").mkdir()
    (estado / "worktrees" / "solto").write_text("x", encoding="utf-8")
    itens = wi.list_slots(deps)
    assert len(itens) == 2
    por_slot = {item["slot"]: item for item in itens}
    assert por_slot[0]["repo"] is None
    assert por_slot[0]["exists"] is False
    assert por_slot[0]["busy"] is False
    assert por_slot[1]["busy"] is True


def test_gc_state_dir_com_excecao_soma_erro(deps, tmp_path):
    def _falha():
        raise RuntimeError("falha de estado")

    resultado = wi.gc(_deps_em(tmp_path / "state", state_dir=_falha))
    assert resultado == {"slots_removed": 0, "rejected_removed": 0, "errors": 1}


def test_gc_ignora_entrada_symlink_em_worktrees(deps, tmp_path):
    (deps.state_dir() / "worktrees").mkdir(parents=True)
    alvo = tmp_path / "fora"
    alvo.mkdir()
    (alvo / "keep.txt").write_text("k\n", encoding="utf-8")
    os.symlink(str(alvo), str(deps.state_dir() / "worktrees" / "zz"))
    resultado = wi.gc(deps, now=time.time())
    assert resultado["errors"] == 0
    assert (alvo / "keep.txt").exists()


def test_gc_diretorio_sem_permissao_soma_erro(deps):
    rd = deps.state_dir() / "worktrees" / "r1"
    rd.mkdir(parents=True)
    rd.chmod(0o100)
    try:
        resultado = wi.gc(deps, now=time.time())
        assert resultado["errors"] == 1
    finally:
        rd.chmod(0o700)


def test_gc_slot_orfo_symlink_preserva_alvo(deps, tmp_path):
    rd = deps.state_dir() / "worktrees" / "r1"
    rd.mkdir(parents=True)
    (rd / "repo.json").write_text(
        json.dumps({"top": str(tmp_path), "common_dir": str(tmp_path / "nada")}),
        encoding="utf-8")
    (rd / "slot-0.lock").write_text("", encoding="utf-8")
    alvo = tmp_path / "fora"
    alvo.mkdir()
    (alvo / "conteudo.txt").write_text("c\n", encoding="utf-8")
    os.symlink(str(alvo), str(rd / "slot-0"))
    resultado = wi.gc(deps, now=time.time())
    assert resultado["errors"] == 1
    assert resultado["slots_removed"] == 0
    assert (alvo / "conteudo.txt").exists()
    assert os.path.islink(rd / "slot-0")


def test_gc_slot_velho_falha_worktree_remove_soma_erro(repo, deps, monkeypatch):
    s, _prep = _abrir(repo, deps, ["a.txt"])
    slot = s.slot
    wi.close_session(s)
    _envelhecer(os.path.join(os.path.dirname(slot), "slot-0.lock"))
    _falso_git(monkeypatch, lambda args: "worktree" in args and "remove" in args)
    resultado = wi.gc(deps)
    assert resultado["errors"] == 1
    assert resultado["slots_removed"] == 0
    assert os.path.isdir(slot)


def test_gc_slot_velho_top_ausente_usa_common_dir(repo, deps, tmp_path):
    s, _prep = _abrir(repo, deps, ["a.txt"])
    slot = s.slot
    rd = Path(s.slot).parent
    wi.close_session(s)
    (rd / "repo.json").write_text(
        json.dumps({"top": str(tmp_path / "sumiu"), "common_dir": s.common_dir}),
        encoding="utf-8")
    _envelhecer(rd / "slot-0.lock")
    resultado = wi.gc(deps)
    assert resultado["slots_removed"] == 1
    assert not os.path.exists(slot)


def test_gc_slot_velho_guarda_recusada_preserva_diretorio(repo, deps, monkeypatch):
    rd = deps.state_dir() / "worktrees" / "r1"
    rd.mkdir(parents=True)
    comum = wi._common_dir(str(repo))
    (rd / "repo.json").write_text(json.dumps({"top": str(repo), "common_dir": comum}),
                                  encoding="utf-8")
    (rd / "slot-0.lock").write_text("", encoding="utf-8")
    slot = rd / "slot-0"
    slot.mkdir()
    (slot / "arquivo.txt").write_text("x\n", encoding="utf-8")
    _envelhecer(rd / "slot-0.lock")
    chamadas = []
    original = wi._git

    def espiao(args, cwd, **kw):
        chamadas.append(list(args))
        return original(args, cwd, **kw)

    monkeypatch.setattr(wi, "_git", espiao)
    resultado = wi.gc(deps, now=time.time())
    assert resultado["errors"] == 1
    assert resultado["slots_removed"] == 0
    assert (slot / "arquivo.txt").exists()
    # Guarda recusou: nenhum comando destrutivo chega a ser emitido.
    assert not any("remove" in args for args in chamadas)


def test_gc_rejected_ignora_symlink_e_arquivo_solto(deps, tmp_path):
    rejected = deps.state_dir() / "rejected"
    rejected.mkdir(parents=True)
    alvo = tmp_path / "fora"
    alvo.mkdir()
    (alvo / "f.txt").write_text("a\n", encoding="utf-8")
    os.symlink(str(alvo), str(rejected / "lnk"))
    (rejected / "solto.txt").write_text("x\n", encoding="utf-8")
    (rejected / "recente").mkdir()
    (rejected / "recente" / "d.txt").write_text("d\n", encoding="utf-8")
    (rejected / "velho").mkdir()
    (rejected / "velho" / "v.txt").write_text("v\n", encoding="utf-8")
    _envelhecer(rejected / "velho", dias=8)
    resultado = wi.gc(deps, now=time.time())
    assert resultado["rejected_removed"] == 1
    assert resultado["errors"] == 0
    assert not (rejected / "velho").exists()
    assert (rejected / "recente" / "d.txt").exists()
    assert (rejected / "solto.txt").exists()
    assert (alvo / "f.txt").exists()


# --- prompt e auxiliares ---------------------------------------------------


def test_rewrite_prompt_topo_prefixo_do_slot_substitui_uma_vez():
    s = wi.Session(real_cwd="/a", real_top="/a", common_dir="", repo_id="r",
                   slot="/a/st/slot-0", slot_cwd="/a/st/slot-0", slot_index=0,
                   lock_fd=-1, run_id="run1")
    resultado = wi.rewrite_prompt(b"cd /lnk && cat /a/arq.txt", s, "/lnk")
    assert resultado == b"cd /a/st/slot-0 && cat /a/st/slot-0/arq.txt"
    assert resultado.count(b"/a/st/slot-0") == 2
    assert b"/a/st/slot-0/st" not in resultado


def test_rewrite_prompt_formas_relatica_e_externa():
    s = wi.Session(real_cwd="/a/sub", real_top="/a", common_dir="", repo_id="r",
                   slot="/a/st/slot-0", slot_cwd="/a/st/slot-0", slot_index=0,
                   lock_fd=-1, run_id="run1")
    assert wi.rewrite_prompt(b"veja /a/x.txt", s, "/outro") == b"veja /a/st/slot-0/x.txt"
    resultado = wi.rewrite_prompt(b"cd /q/sub && cat /a/x.txt", s, "/q/sub")
    assert resultado == b"cd /a/st/slot-0/sub && cat /a/st/slot-0/x.txt"


def test_git_timeout_vira_isolationerror(monkeypatch):
    def falso(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(wi.subprocess, "run", falso)
    with pytest.raises(wi.IsolationError, match="falha ao executar git"):
        wi._git(["status"], ".")


def test_remove_in_slot_laco_de_symlink_e_ausente(tmp_path, monkeypatch):
    elo = tmp_path / "elo"
    alvo = tmp_path / "alvo"
    alvo.write_text("a\n", encoding="utf-8")
    os.symlink(str(alvo), str(elo))
    original = os.lstat

    def falso(path, *a, **k):
        if os.fspath(path) == str(elo):
            raise OSError("elo")
        return original(path, *a, **k)

    monkeypatch.setattr(wi.os, "lstat", falso)
    wi._remove_in_slot(str(elo))
    monkeypatch.undo()
    assert not os.path.lexists(str(elo))
    assert alvo.exists()
    wi._remove_in_slot(str(tmp_path / "ausente.txt"))


def test_remove_quietly_caminho_ausente_nao_levanta(tmp_path):
    assert wi._remove_quietly(str(tmp_path / "ausente.tmp")) is None
