import os
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

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


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    (r / "apagar.txt").write_text("vai sumir\n", encoding="utf-8")
    (r / ".gitignore").write_text("node_modules/\nignorado.txt\nbuild/\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def deps(tmp_path):
    estado = tmp_path / "state"
    return wi.Deps(state_dir=lambda: estado, is_sensitive=wr._is_sensitive,
                   snapshot_allowed=wr.snapshot_allowed, parse_status=wr._parse_status_z,
                   max_file_bytes=wr.MAX_SNAPSHOT_BYTES, max_paths=wr._ARVORE_MAX_CAMINHOS)


# ---- 1. resolve_mode ----


def test_resolve_mode_variavel_vence_arquivo(repo):
    (repo / ".worker-ask-isolation").write_text("write\n", encoding="utf-8")
    assert wi.resolve_mode(str(repo), {}) == "write"
    assert wi.resolve_mode(str(repo), {"WORKER_ASK_ISOLATION": "all"}) == "all"


def test_resolve_mode_arquivo_no_topo_vale_para_subdiretorio(repo):
    (repo / ".worker-ask-isolation").write_text("all\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    assert wi.resolve_mode(str(repo / "pkg"), {}) == "all"


def test_resolve_mode_variavel_invalida_cai_para_arquivo(repo):
    (repo / ".worker-ask-isolation").write_text("write\n", encoding="utf-8")
    assert wi.resolve_mode(str(repo), {"WORKER_ASK_ISOLATION": "banana"}) == "write"


def test_resolve_mode_sem_nada_devolve_off(repo):
    assert wi.resolve_mode(str(repo), {}) == "off"


def test_resolve_mode_fora_de_git_le_so_o_proprio_cwd(tmp_path):
    pai = tmp_path / "pai"
    pai.mkdir()
    (pai / ".worker-ask-isolation").write_text("write\n", encoding="utf-8")
    filho = pai / "filho"
    filho.mkdir()
    assert wi.resolve_mode(str(pai), {}) == "write"
    assert wi.resolve_mode(str(filho), {}) == "off"


# ---- 2. slot_count ----


def test_slot_count_padrao_quando_ausente():
    assert wi.slot_count({}) == wi.DEFAULT_SLOTS


def test_slot_count_valor_valido():
    assert wi.slot_count({"WORKER_ASK_ISOLATION_SLOTS": "3"}) == 3


def test_slot_count_valor_invalido_usa_padrao():
    for v in ("0", "abc", "999"):
        assert wi.slot_count({"WORKER_ASK_ISOLATION_SLOTS": v}) == wi.DEFAULT_SLOTS


# ---- 3 a 6. recusas do open_session ----


def test_open_session_sem_git_vira_modo_direto(tmp_path, deps):
    fora = tmp_path / "semgit"
    fora.mkdir()
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(fora), [], "run1", deps, environ={})
    assert exc.value.reason == "not_git"


def test_open_session_sem_commit_vira_modo_direto(tmp_path, deps):
    r = tmp_path / "semcommit"
    r.mkdir()
    _git(r, "init", "-q")
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(r), [], "run1", deps, environ={})
    assert exc.value.reason == "no_commit"


def test_open_session_cwd_ignorado_vira_modo_direto(repo, deps):
    (repo / "build").mkdir()
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(repo / "build"), [], "run1", deps, environ={})
    assert exc.value.reason == "cwd_ignored"


def test_open_session_autorizado_em_repo_aninhado_vira_modo_direto(repo, deps):
    sub = repo / "sub"
    sub.mkdir()
    _git(sub, "init", "-q")
    with pytest.raises(wi.DirectMode) as exc:
        wi.open_session(str(repo), ["sub/x.txt"], "run1", deps, environ={})
    assert exc.value.reason == "nested_repo"


# ---- 7. open_session normal ----


def test_open_session_cria_slot_zero_no_estado(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        base_estado = os.path.join(str(deps.state_dir()), "worktrees", s.repo_id)
        assert os.path.basename(s.slot) == "slot-0"
        assert os.path.dirname(s.slot) == base_estado
        assert os.path.isdir(s.slot)
        assert os.path.isfile(os.path.join(s.slot, "base.txt"))
        assert s.real_top == os.path.realpath(str(repo))
        assert os.path.isfile(os.path.join(base_estado, "repo.json"))
    finally:
        wi.close_session(s)


def test_open_session_com_cwd_em_subdiretorio_mapeia_slot_cwd(repo, deps):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pkg")
    s = wi.open_session(str(repo / "pkg"), [], "run1", deps, environ={})
    try:
        assert s.slot_cwd == os.path.join(s.slot, "pkg")
    finally:
        wi.close_session(s)


# ---- 8. slots concorrentes ----


def test_duas_sessoes_usam_slots_diferentes_e_reaproveita_slot_zero(repo, deps):
    s1 = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        s2 = wi.open_session(str(repo), [], "run2", deps, environ={})
        try:
            assert os.path.basename(s1.slot) == "slot-0"
            assert os.path.basename(s2.slot) == "slot-1"
        finally:
            wi.close_session(s2)
    finally:
        wi.close_session(s1)
    s3 = wi.open_session(str(repo), [], "run3", deps, environ={})
    try:
        assert os.path.basename(s3.slot) == "slot-0"
    finally:
        wi.close_session(s3)


# ---- 9. fila de slots ----


def test_sem_slot_levanta_no_free_slot(repo, deps, monkeypatch):
    monkeypatch.setattr(wi, "SLOT_WAIT_S", 0.2)
    monkeypatch.setattr(wi, "SLOT_POLL_S", 0.05)
    s1 = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        with pytest.raises(wi.NoFreeSlot) as exc:
            wi.open_session(str(repo), [], "run2", deps,
                            environ={"WORKER_ASK_ISOLATION_SLOTS": "1"})
        assert str(exc.value) == "isolamento sem slot livre"
    finally:
        wi.close_session(s1)


def test_sem_slot_com_deadline_vencido_falha_rapido(repo, deps):
    s1 = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        inicio = time.monotonic()
        with pytest.raises(wi.NoFreeSlot):
            wi.open_session(str(repo), [], "run2", deps,
                            environ={"WORKER_ASK_ISOLATION_SLOTS": "1"},
                            deadline=time.monotonic())
        assert time.monotonic() - inicio < 5
    finally:
        wi.close_session(s1)


# ---- 10. run_id ----


@pytest.mark.parametrize("run_id", ["../x", ""])
def test_run_id_invalido_levanta_isolation_error(repo, deps, run_id):
    with pytest.raises(wi.IsolationError):
        wi.open_session(str(repo), [], run_id, deps, environ={})


# ---- 11. estado sujo vai para o slot ----


def test_prepare_copia_estado_sujo_para_o_slot(repo, deps):
    (repo / "base.txt").write_text("modificado\n", encoding="utf-8")
    (repo / "novo.txt").write_text("novo\n", encoding="utf-8")
    (repo / "apagar.txt").unlink()
    os.symlink("base.txt", repo / "atalho")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={})
        slot = Path(s.slot)
        assert (slot / "base.txt").read_text(encoding="utf-8") == "modificado\n"
        assert (slot / "novo.txt").read_text(encoding="utf-8") == "novo\n"
        assert not (slot / "apagar.txt").exists()
        assert os.path.islink(str(slot / "atalho"))
        assert os.readlink(str(slot / "atalho")) == "base.txt"
        assert info.overlay_files >= 4
    finally:
        wi.close_session(s)


# ---- 12. segredo nao vai para o slot ----


def test_prepare_nao_copia_segredo_nao_rastreado(repo, deps):
    (repo / ".env").write_text("X=1\n", encoding="utf-8")
    (repo / "credentials.json").write_text("{}\n", encoding="utf-8")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={})
        assert not os.path.lexists(os.path.join(s.slot, ".env"))
        assert not os.path.lexists(os.path.join(s.slot, "credentials.json"))
        assert info.skipped_sensitive >= 2
    finally:
        wi.close_session(s)


# ---- 13. repo aninhado nao rastreado ----


def test_prepare_nao_copia_repo_aninhado_nao_rastreado(repo, deps):
    sub = repo / "sub"
    sub.mkdir()
    _git(sub, "init", "-q")
    (sub / "interno.txt").write_text("i\n", encoding="utf-8")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={})
        assert not os.path.lexists(os.path.join(s.slot, "sub"))
        assert info.skipped_nested == 1
    finally:
        wi.close_session(s)


# ---- 14. sujeira nao atravessa execucoes ----


def test_prepare_segunda_execucao_limpa_sujeira_do_slot(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        wi.prepare(s, [], deps, environ={})
        (Path(s.slot) / "lixo.txt").write_text("lixo\n", encoding="utf-8")
        (Path(s.slot) / "base.txt").write_text("estragou\n", encoding="utf-8")
        wi.prepare(s, [], deps, environ={})
        assert not os.path.lexists(os.path.join(s.slot, "lixo.txt"))
        assert (Path(s.slot) / "base.txt").read_text(encoding="utf-8") == "base\n"
    finally:
        wi.close_session(s)


# ---- 15. hooks nao rodam ----


def test_prepare_nao_executa_hooks(repo, deps, tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marcador = tmp_path / "marcador"
    script = hooks / "post-checkout"
    script.write_text("#!/bin/sh\nprintf x > " + shlex.quote(str(marcador)) + "\n",
                      encoding="utf-8")
    os.chmod(script, 0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        wi.prepare(s, [], deps, environ={})
        assert not marcador.exists()
    finally:
        wi.close_session(s)


# ---- 16. link de dependencia ----


def test_prepare_cria_link_de_dependencia(repo, deps):
    pkg = repo / "node_modules" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "index.js").write_text("js\n", encoding="utf-8")
    real_nm = str(repo / "node_modules")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={})
        slot_nm = os.path.join(s.slot, "node_modules")
        assert os.path.islink(slot_nm)
        assert os.path.realpath(slot_nm) == os.path.realpath(real_nm)
        assert "node_modules" in info.links
        info2 = wi.prepare(s, [], deps, environ={})
        assert (pkg / "index.js").read_text(encoding="utf-8") == "js\n"
        assert os.path.islink(slot_nm)
        assert "node_modules" in info2.links
    finally:
        wi.close_session(s)


# ---- 17. quando o link nao cabe ----


def test_sem_link_para_diretorio_nao_ignorado(repo, deps):
    (repo / "venv").mkdir()
    (repo / "venv" / "pyvenv.cfg").write_text("v\n", encoding="utf-8")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={})
        assert not os.path.islink(os.path.join(s.slot, "venv"))
        assert info.links == []
    finally:
        wi.close_session(s)


def test_sem_link_para_nome_inexistente(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={})
        assert info.links == []
        assert not os.path.lexists(os.path.join(s.slot, ".venv"))
    finally:
        wi.close_session(s)


def test_sem_link_com_variavel_links_vazia(repo, deps):
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x\n", encoding="utf-8")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={"WORKER_ASK_ISOLATION_LINKS": ""})
        assert info.links == []
        assert not os.path.lexists(os.path.join(s.slot, "node_modules"))
    finally:
        wi.close_session(s)


# ---- 18. autorizado ignorado pelo git ----


def test_prepare_copia_autorizado_ignorado_pelo_git(repo, deps):
    (repo / "ignorado.txt").write_text("conteudo local\n", encoding="utf-8")
    s = wi.open_session(str(repo), ["ignorado.txt"], "run1", deps, environ={})
    try:
        wi.prepare(s, ["ignorado.txt"], deps, environ={})
        slot_txt = Path(s.slot) / "ignorado.txt"
        assert slot_txt.read_text(encoding="utf-8") == "conteudo local\n"
    finally:
        wi.close_session(s)


# ---- 19. limites de copia ----


def test_prepare_limite_de_bytes_vira_overlay_limit(repo, deps):
    (repo / "grande.txt").write_bytes(b"a" * 100)
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        with pytest.raises(wi.DirectMode) as exc:
            wi.prepare(s, [], deps, environ={"WORKER_ASK_ISOLATION_MAX_OVERLAY": "10"})
        assert exc.value.reason == "overlay_limit"
    finally:
        wi.close_session(s)


def test_prepare_limite_de_caminhos_vira_overlay_limit(repo, deps):
    deps1 = replace(deps, max_paths=1)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        with pytest.raises(wi.DirectMode) as exc:
            wi.prepare(s, [], deps1, environ={})
        assert exc.value.reason == "overlay_limit"
    finally:
        wi.close_session(s)


# ---- 20. snapshots e tempo ----


def test_prepare_registra_snapshots_e_tempo(repo, deps):
    s = wi.open_session(str(repo), ["base.txt"], "run1", deps, environ={})
    try:
        info = wi.prepare(s, ["base.txt"], deps, environ={})
        assert info.snap_before == wr.snapshot_allowed(s.real_cwd, ["base.txt"])
        for rel in ("base.txt",):
            assert info.snap_slot_before[rel][:2] == info.snap_before[rel][:2]
        assert info.prep_s >= 0
    finally:
        wi.close_session(s)


# ---- 21 e 22. guard_slot ----


def test_guard_slot_aceita_slot_de_sessao_valida(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        wi.guard_slot(s.slot, s.common_dir, deps)
    finally:
        wi.close_session(s)


def test_guard_slot_recusa_diretorio_fora_de_worktrees(repo, deps):
    with pytest.raises(wi.SlotGuardError):
        wi.guard_slot(str(repo), str(repo / ".git"), deps)


def test_guard_slot_recusa_diretorio_comum_sem_worktree(repo, deps):
    falso = deps.state_dir() / "worktrees" / "x" / "slot-falso"
    falso.mkdir(parents=True)
    with pytest.raises(wi.SlotGuardError):
        wi.guard_slot(str(falso), str(falso), deps)


def test_guard_slot_recusa_subdiretorio_de_slot(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        subdir = os.path.join(s.slot, "subdir")
        os.mkdir(subdir)
        with pytest.raises(wi.SlotGuardError):
            wi.guard_slot(subdir, s.common_dir, deps)
    finally:
        wi.close_session(s)


def test_guard_slot_recusa_common_dir_de_outro_repo(repo, deps, tmp_path):
    r2 = tmp_path / "repo2"
    r2.mkdir()
    _git(r2, "init", "-q")
    _git(r2, "config", "user.email", "t@t")
    _git(r2, "config", "user.name", "t")
    (r2 / "base.txt").write_text("base\n", encoding="utf-8")
    _git(r2, "add", "-A")
    _git(r2, "commit", "-qm", "init")
    s_a = wi.open_session(str(repo), [], "run1", deps, environ={})
    s_b = wi.open_session(str(r2), [], "run2", deps, environ={})
    try:
        with pytest.raises(wi.SlotGuardError):
            wi.guard_slot(s_b.slot, s_a.common_dir, deps)
    finally:
        wi.close_session(s_b)
        wi.close_session(s_a)


# ---- 23. rewrite_prompt ----


def test_rewrite_prompt_troca_topo_pelo_slot(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        topo = s.real_top.encode()
        prompt = b"edite " + topo + b"/src/a.py\n"
        trocado = wi.rewrite_prompt(prompt, s, str(repo))
        assert trocado == b"edite " + s.slot.encode() + b"/src/a.py\n"
    finally:
        wi.close_session(s)


def test_rewrite_prompt_preserva_sufixos_parecidos(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        topo = s.real_top.encode()
        prompt = topo + b"-outro/x e " + topo + b".bak"
        assert wi.rewrite_prompt(prompt, s, str(repo)) == prompt
    finally:
        wi.close_session(s)


def test_rewrite_prompt_troca_ocorrencia_no_fim(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        assert wi.rewrite_prompt(s.real_top.encode(), s, str(repo)) == s.slot.encode()
    finally:
        wi.close_session(s)


def test_rewrite_prompt_troca_caminho_via_symlink(repo, deps, tmp_path):
    atalho = tmp_path / "atalho_repo"
    os.symlink(str(repo), atalho)
    s = wi.open_session(str(atalho), [], "run1", deps, environ={})
    try:
        pelo_link = os.path.abspath(str(atalho)).encode()
        prompt = b"edite " + pelo_link + b"/a.py"
        trocado = wi.rewrite_prompt(prompt, s, str(atalho))
        assert trocado == b"edite " + s.slot.encode() + b"/a.py"
    finally:
        wi.close_session(s)


# ---- 24 e 25 ----


def test_isolation_notice_texto_exato(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        esperado = ("[worker-router] Execucao isolada: o projeto esta em "
                    + s.slot_cwd + ". Trabalhe somente aqui.")
        assert wi.isolation_notice(s) == esperado
    finally:
        wi.close_session(s)


def test_close_session_idempotente(repo, deps):
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    wi.close_session(s)
    wi.close_session(s)


def test_copy_symlink_recusa_alvo_absoluto_e_escape(tmp_path):
    slot = tmp_path / "slot"
    (slot / "sub").mkdir(parents=True)
    fora = tmp_path / "fora.txt"
    fora.write_text("x")
    origem = tmp_path / "origem"
    origem.mkdir()
    casos = {"abs": str(fora), "escape": "../../fora.txt", "interno": "../base.txt"}
    for nome, alvo in casos.items():
        os.symlink(alvo, origem / nome)
        wi._copy_symlink(str(slot), str(origem / nome), str(slot / "sub" / nome))
    assert not os.path.lexists(slot / "sub" / "abs")
    assert not os.path.lexists(slot / "sub" / "escape")
    assert os.readlink(slot / "sub" / "interno") == "../base.txt"


# ---- 26. symlinks versionados no HEAD ----


def test_prepare_remove_symlink_versionado_que_sai_do_slot(repo, deps, tmp_path):
    # Links limpos do HEAD voltam pelo checkout sem passar por _copy_symlink
    # (que só vê o overlay): absoluto ou em fuga daria acesso fora da cópia.
    alvo = tmp_path / "alvo-fora.txt"
    alvo.write_text("fora\n", encoding="utf-8")
    os.symlink(str(alvo), repo / "link-abs")
    os.symlink("../../fora.txt", repo / "link-fuga")
    os.symlink("..", repo / "link-dir-fuga")
    os.symlink("base.txt", repo / "link-interno")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "links")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        wi.prepare(s, [], deps, environ={})
        slot = Path(s.slot)
        assert not os.path.lexists(slot / "link-abs")
        assert not os.path.lexists(slot / "link-fuga")
        assert not os.path.lexists(slot / "link-dir-fuga")
        assert os.readlink(slot / "link-interno") == "base.txt"
        assert (slot / "base.txt").is_file()
    finally:
        wi.close_session(s)


def test_validate_slot_symlinks_link_ilegivel_e_removido(repo, deps, monkeypatch):
    os.symlink("base.txt", repo / "link-ok")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "link")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    real_readlink = os.readlink

    def readlink_quebra(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == "link-ok":
            raise OSError("boom")
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", readlink_quebra)
    try:
        wi.prepare(s, [], deps, environ={})
        assert not os.path.lexists(os.path.join(s.slot, "link-ok"))
    finally:
        wi.close_session(s)


def test_validate_slot_symlinks_falha_de_varredura_para_com_erro(repo, deps, monkeypatch):
    (repo / "sub").mkdir()
    (repo / "sub" / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "sub")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    real_scandir = os.scandir

    def scandir_quebra(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == "sub":
            raise OSError("boom")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir_quebra)
    try:
        with pytest.raises(wi.IsolationError):
            wi.prepare(s, [], deps, environ={})
    finally:
        wi.close_session(s)


def test_symlink_foge_caminho_incomparavel_conta_como_fuga(tmp_path):
    slot = tmp_path / "slot"
    slot.mkdir()
    # Raiz relativa com destino absoluto: commonpath não decide, vira fuga.
    assert wi._symlink_foge("relativo", "/abs/dst", "alvo") is True
    assert wi._symlink_foge(str(slot), str(slot / "a"), "b") is False


def test_symlink_foge_composicao_com_link_intermediario(tmp_path):
    # `a -> .` e `b -> a/../fora.txt` são internos na forma lexical, mas o
    # kernel resolve `a` antes do `..`: b sai do slot de verdade.
    slot = tmp_path / "slot"
    slot.mkdir()
    os.symlink(".", slot / "a")
    assert wi._symlink_foge(str(slot), str(slot / "a"), ".") is False
    assert wi._symlink_foge(str(slot), str(slot / "b"), "a/../fora.txt") is True


def test_symlink_foge_ciclo_de_links_conta_como_fuga(tmp_path):
    slot = tmp_path / "slot"
    slot.mkdir()
    os.symlink("y", slot / "x")
    os.symlink("x", slot / "y")
    os.symlink("z", slot / "z")
    assert wi._symlink_foge(str(slot), str(slot / "x"), "y") is True
    assert wi._symlink_foge(str(slot), str(slot / "y"), "x") is True
    assert wi._symlink_foge(str(slot), str(slot / "z"), "z") is True


def test_copy_symlink_recusa_composicao_com_link_intermediario(tmp_path):
    slot = tmp_path / "slot"
    slot.mkdir()
    origem = tmp_path / "origem"
    origem.mkdir()
    os.symlink(".", origem / "a")
    os.symlink("a/../fora.txt", origem / "b")
    wi._copy_symlink(str(slot), str(origem / "a"), str(slot / "a"))
    wi._copy_symlink(str(slot), str(origem / "b"), str(slot / "b"))
    assert os.readlink(slot / "a") == "."
    assert not os.path.lexists(slot / "b")


def test_prepare_remove_composicao_e_ciclo_versionados(repo, deps, tmp_path):
    # A composição também entra pelo checkout de links versionados: a
    # validação efetiva remove o elo que foge e o ciclo e mantém `a -> .`.
    (tmp_path / "fora.txt").write_text("fora\n", encoding="utf-8")
    os.symlink(".", repo / "a")
    os.symlink("a/../fora.txt", repo / "b")
    os.symlink("loop", repo / "loop")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "composicao")
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        wi.prepare(s, [], deps, environ={})
        slot = Path(s.slot)
        assert os.readlink(slot / "a") == "."
        assert not os.path.lexists(slot / "b")
        assert not os.path.lexists(slot / "loop")
    finally:
        wi.close_session(s)


def test_prepare_revalida_symlinks_depois_dos_links_de_dependencia(repo, deps):
    # `b -> node_modules/../base.txt` parece interno enquanto node_modules não
    # existe no slot; com o elo de dependência criado depois, o `..` alcança a
    # árvore real. A revalidação posterior remove o elo que atravessa a
    # dependência e preserva apenas o link explícito de _create_links.
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x\n", encoding="utf-8")
    os.symlink("node_modules/../base.txt", repo / "b")
    os.symlink("node_modules/x.js", repo / "atraves")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "composicao com link de dependencia")
    os.symlink("node_modules/../base.txt", repo / "u")  # mesma fuga via overlay
    s = wi.open_session(str(repo), [], "run1", deps, environ={})
    try:
        info = wi.prepare(s, [], deps, environ={})
        slot = Path(s.slot)
        assert "node_modules" in info.links
        assert os.path.islink(os.path.join(s.slot, "node_modules"))
        assert os.path.realpath(os.path.join(s.slot, "node_modules")) == \
            os.path.realpath(str(repo / "node_modules"))
        assert not os.path.lexists(slot / "b")
        assert not os.path.lexists(slot / "u")
        assert not os.path.lexists(slot / "atraves")
        assert (slot / "base.txt").is_file()
    finally:
        wi.close_session(s)
