import os
import shutil
import signal
import subprocess
import sys
import time
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
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    (r / "b.txt").write_text("b\n", encoding="utf-8")
    (r / "fora.txt").write_text("fora\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def deps(tmp_path):
    estado = tmp_path / "state"
    return wi.Deps(state_dir=lambda: estado, is_sensitive=wr._is_sensitive,
                   snapshot_allowed=wr.snapshot_allowed, parse_status=wr._parse_status_z,
                   max_file_bytes=wr.MAX_SNAPSHOT_BYTES, max_paths=wr._ARVORE_MAX_CAMINHOS)


def _abrir(repo, deps, allowed, run_id="run1", cwd=None):
    """Abre a sessão e prepara o slot; devolve (sessao, prep)."""
    s = wi.open_session(str(cwd or repo), allowed, run_id, deps, environ={})
    try:
        return s, wi.prepare(s, allowed, deps, environ={})
    except BaseException:
        wi.close_session(s)
        raise


def _repo_com_pkg(repo):
    """Commita pkg/m.txt no repositorio e devolve o diretorio pkg."""
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "m.txt").write_text("m\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pkg")
    return pkg


# --- sync_back: devolucao de arquivos autorizados ---


def test_sync_back_aplica_autorizado_alterado_no_slot(repo, deps):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, prep, deps)
        assert res.applied == ["a.txt"]
        assert res.conflicts == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "slot\n"
    finally:
        wi.close_session(s)


def test_sync_back_ignora_arquivo_fora_do_escopo(repo, deps):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        (Path(s.slot_cwd) / "fora.txt").write_text("mudou\n", encoding="utf-8")
        (Path(s.slot_cwd) / "extra.txt").write_text("extra\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, prep, deps)
        assert res.applied == []
        assert res.conflicts == []
        assert (repo / "fora.txt").read_text(encoding="utf-8") == "fora\n"
        assert not (repo / "extra.txt").exists()
    finally:
        wi.close_session(s)


def test_sync_back_cria_autorizado_novo_em_diretorio_novo(repo, deps):
    permitido = ["novo/dir/c.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        destino = Path(s.slot_cwd) / "novo" / "dir" / "c.txt"
        destino.parent.mkdir(parents=True)
        destino.write_text("conteudo c\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, prep, deps)
        assert res.applied == ["novo/dir/c.txt"]
        real = repo / "novo" / "dir" / "c.txt"
        assert real.read_text(encoding="utf-8") == "conteudo c\n"
        assert (real.stat().st_mode & 0o777) == 0o644
    finally:
        wi.close_session(s)


def test_sync_back_arquivo_novo_herda_bit_de_execucao(repo, deps):
    permitido = ["novo/dir/c.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        destino = Path(s.slot_cwd) / "novo" / "dir" / "c.txt"
        destino.parent.mkdir(parents=True)
        destino.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(destino, 0o755)
        res = wi.sync_back(s, permitido, prep, deps)
        assert res.applied == ["novo/dir/c.txt"]
        real = repo / "novo" / "dir" / "c.txt"
        assert real.read_text(encoding="utf-8") == "#!/bin/sh\n"
        assert (real.stat().st_mode & 0o777) == 0o755
    finally:
        wi.close_session(s)


def test_sync_back_apaga_autorizado_apagado_no_slot(repo, deps):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        os.unlink(Path(s.slot_cwd) / "a.txt")
        res = wi.sync_back(s, permitido, prep, deps)
        assert "a.txt" in res.applied
        assert "a.txt" not in res.conflicts
        assert not (repo / "a.txt").exists()
    finally:
        wi.close_session(s)


def test_sync_back_conflito_quando_real_e_slot_mudam(repo, deps):
    permitido = ["a.txt", "b.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        (repo / "a.txt").write_text("real\n", encoding="utf-8")
        (Path(s.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        (Path(s.slot_cwd) / "b.txt").write_text("slot b\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, prep, deps)
        assert (repo / "a.txt").read_text(encoding="utf-8") == "real\n"
        assert "a.txt" in res.conflicts
        assert "a.txt" not in res.applied
        assert s.tainted is True
        assert "b.txt" in res.applied
        assert (repo / "b.txt").read_text(encoding="utf-8") == "slot b\n"
    finally:
        wi.close_session(s)


def test_sync_back_symlink_no_slot_vira_conflito(repo, deps):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        os.unlink(Path(s.slot_cwd) / "a.txt")
        os.symlink("/etc/hostname", Path(s.slot_cwd) / "a.txt")
        res = wi.sync_back(s, permitido, prep, deps)
        assert "a.txt" in res.conflicts
        assert "a.txt" not in res.applied
        real = repo / "a.txt"
        assert not real.is_symlink()
        assert real.is_file()
        assert real.read_text(encoding="utf-8") == "a\n"
    finally:
        wi.close_session(s)


def test_sync_back_arquivo_existente_mantem_modo(repo, deps):
    permitido = ["a.txt"]
    os.chmod(repo / "a.txt", 0o600)
    s, prep = _abrir(repo, deps, permitido)
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, prep, deps)
        assert res.applied == ["a.txt"]
        assert ((repo / "a.txt").stat().st_mode & 0o777) == 0o600
    finally:
        wi.close_session(s)


def test_sync_back_sem_mudanca_nada_a_fazer(repo, deps):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        res = wi.sync_back(s, permitido, prep, deps)
        assert res.applied == []
        assert res.conflicts == []
        assert s.tainted is False
    finally:
        wi.close_session(s)


def test_sync_back_com_prep_none(repo, deps):
    permitido = ["a.txt"]
    s, _ = _abrir(repo, deps, permitido)
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, None, deps)
        assert res.applied == []
        assert res.conflicts == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
    finally:
        wi.close_session(s)


def test_sync_back_nao_deixa_temporario_para_tras(repo, deps):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        wi.sync_back(s, permitido, prep, deps)
        sobras = [n for n in os.listdir(repo) if ".wa-sync-" in n]
        assert sobras == []
    finally:
        wi.close_session(s)


def test_sync_back_cwd_em_subdiretorio(repo, deps):
    pkg = _repo_com_pkg(repo)
    permitido = ["m.txt"]
    s, prep = _abrir(repo, deps, permitido, cwd=pkg)
    try:
        (Path(s.slot_cwd) / "m.txt").write_text("slot m\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, prep, deps)
        assert res.applied == ["m.txt"]
        assert (pkg / "m.txt").read_text(encoding="utf-8") == "slot m\n"
    finally:
        wi.close_session(s)


def test_sync_back_bloqueia_sinais_durante_a_copia(repo, deps, monkeypatch):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        (Path(s.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        recebidos = []
        dentro = {}

        def handler(sig, frame):
            recebidos.append(sig)

        antigo = signal.signal(signal.SIGTERM, handler)
        real_replace = os.replace

        def falso(src, dst):
            os.kill(os.getpid(), signal.SIGTERM)
            dentro["vazio"] = list(recebidos)
            real_replace(src, dst)

        monkeypatch.setattr(wi.os, "replace", falso)
        try:
            res = wi.sync_back(s, permitido, prep, deps)
        finally:
            signal.signal(signal.SIGTERM, antigo)
        assert res.applied == ["a.txt"]
        assert dentro["vazio"] == []
        assert len(recebidos) == 1
        assert (repo / "a.txt").read_text(encoding="utf-8") == "slot\n"
    finally:
        wi.close_session(s)


def test_sync_back_restaura_mascara_de_sinais(repo, deps):
    permitido = ["a.txt"]
    s, prep = _abrir(repo, deps, permitido)
    try:
        antes = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        (Path(s.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        res = wi.sync_back(s, permitido, prep, deps)
        depois = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        assert res.applied == ["a.txt"]
        assert antes == depois
    finally:
        wi.close_session(s)


# --- diario de devolucoes (sync.jsonl) ---


def test_synced_by_others_ve_run_de_outro_worker(repo, deps):
    s1, prep1 = _abrir(repo, deps, ["a.txt"], run_id="run1")
    try:
        (Path(s1.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        assert wi.sync_back(s1, ["a.txt"], prep1, deps).applied == ["a.txt"]
    finally:
        wi.close_session(s1)
    s2, _ = _abrir(repo, deps, ["a.txt"], run_id="run2")
    try:
        caminho = os.path.join(os.path.realpath(repo), "a.txt")
        assert caminho in wi.synced_by_others(s2, 0, deps)
    finally:
        wi.close_session(s2)


def test_synced_by_others_ignora_o_proprio_run(repo, deps):
    s1, prep1 = _abrir(repo, deps, ["a.txt"], run_id="run1")
    try:
        (Path(s1.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        wi.sync_back(s1, ["a.txt"], prep1, deps)
        assert wi.synced_by_others(s1, 0, deps) == set()
    finally:
        wi.close_session(s1)


def test_diario_de_devolucao_e_privado(repo, deps):
    s1, prep1 = _abrir(repo, deps, ["a.txt"], run_id="run1")
    try:
        (Path(s1.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        wi.sync_back(s1, ["a.txt"], prep1, deps)
        diario = Path(s1.slot).parent / "sync.jsonl"
        assert diario.stat().st_mode & 0o777 == 0o600
    finally:
        wi.close_session(s1)


def test_synced_by_others_since_no_futuro_vira_vazio(repo, deps):
    s1, prep1 = _abrir(repo, deps, ["a.txt"], run_id="run1")
    try:
        (Path(s1.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        wi.sync_back(s1, ["a.txt"], prep1, deps)
    finally:
        wi.close_session(s1)
    s2, _ = _abrir(repo, deps, ["a.txt"], run_id="run2")
    try:
        assert wi.synced_by_others(s2, time.time() + 3600, deps) == set()
    finally:
        wi.close_session(s2)


def test_synced_by_others_ignora_linha_invalida(repo, deps):
    s1, prep1 = _abrir(repo, deps, ["a.txt"], run_id="run1")
    try:
        repo_id = s1.repo_id
        (Path(s1.slot_cwd) / "a.txt").write_text("slot\n", encoding="utf-8")
        wi.sync_back(s1, ["a.txt"], prep1, deps)
    finally:
        wi.close_session(s1)
    diario = deps.state_dir() / "worktrees" / repo_id / "sync.jsonl"
    with open(diario, "a", encoding="utf-8") as f:
        f.write("lixo\n")
    s2, _ = _abrir(repo, deps, ["a.txt"], run_id="run2")
    try:
        caminho = os.path.join(os.path.realpath(repo), "a.txt")
        assert caminho in wi.synced_by_others(s2, 0, deps)
    finally:
        wi.close_session(s2)


def test_synced_by_others_rotaciona_diario_cheio(repo, deps, monkeypatch):
    monkeypatch.setattr(wi, "SYNC_LOG_MAX_BYTES", 10)
    s1, prep1 = _abrir(repo, deps, ["a.txt"], run_id="run1")
    try:
        repo_id = s1.repo_id
        (Path(s1.slot_cwd) / "a.txt").write_text("slot a\n", encoding="utf-8")
        wi.sync_back(s1, ["a.txt"], prep1, deps)
    finally:
        wi.close_session(s1)
    s2, prep2 = _abrir(repo, deps, ["b.txt"], run_id="run2")
    try:
        (Path(s2.slot_cwd) / "b.txt").write_text("slot b\n", encoding="utf-8")
        wi.sync_back(s2, ["b.txt"], prep2, deps)
    finally:
        wi.close_session(s2)
    diario = deps.state_dir() / "worktrees" / repo_id
    assert (diario / "sync.jsonl.1").exists()
    caminho_a = os.path.join(os.path.realpath(repo), "a.txt")
    caminho_b = os.path.join(os.path.realpath(repo), "b.txt")
    assert caminho_a in wi.synced_by_others(s2, 0, deps)
    assert caminho_b in wi.synced_by_others(s1, 0, deps)


# --- save_rejected ---


def test_save_rejected_copia_arquivo_fora_do_escopo(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"], run_id="run1")
    try:
        (Path(s.slot_cwd) / "extra.txt").write_text("rejeitado\n", encoding="utf-8")
        info = wi.save_rejected(s, ["extra.txt"], deps)
        assert info.saved == 1
        assert info.dir is not None
        assert info.dir.endswith(os.path.join("rejected", "run1"))
        copia = Path(info.dir) / "extra.txt"
        assert copia.read_text(encoding="utf-8") == "rejeitado\n"
        assert (Path(info.dir).stat().st_mode & 0o777) == 0o700
        assert (copia.stat().st_mode & 0o777) == 0o600
    finally:
        wi.close_session(s)


def test_save_rejected_pula_nome_sensivel(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    try:
        (Path(s.slot_cwd) / ".env").write_text("X=1\n", encoding="utf-8")
        info = wi.save_rejected(s, [".env"], deps)
        assert info.saved == 0
        assert info.skipped == 1
        assert info.dir is None
    finally:
        wi.close_session(s)


def test_save_rejected_pula_ausente_e_symlink(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    try:
        os.symlink("/etc/hostname", Path(s.slot_cwd) / "link.txt")
        info = wi.save_rejected(s, ["naoexiste.txt", "link.txt"], deps)
        assert info.saved == 0
        assert info.skipped == 2
        assert info.dir is None
    finally:
        wi.close_session(s)


def test_save_rejected_limite_de_quantidade(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    try:
        nomes = [f"f{i:02d}.txt" for i in range(25)]
        for nome in nomes:
            (Path(s.slot_cwd) / nome).write_text("x\n", encoding="utf-8")
        info = wi.save_rejected(s, nomes, deps)
        assert info.saved == wi.REJECTED_MAX_FILES
        assert info.skipped == 5
        assert info.dir is not None
    finally:
        wi.close_session(s)


def test_save_rejected_limite_de_bytes(repo, deps, monkeypatch):
    monkeypatch.setattr(wi, "REJECTED_MAX_BYTES", 10)
    s, _ = _abrir(repo, deps, ["a.txt"])
    try:
        (Path(s.slot_cwd) / "grande.txt").write_text("x" * 100, encoding="utf-8")
        info = wi.save_rejected(s, ["grande.txt"], deps)
        assert info.saved == 0
        assert info.skipped == 1
        assert info.dir is None
    finally:
        wi.close_session(s)


def test_save_rejected_cwd_em_subdiretorio(repo, deps):
    pkg = _repo_com_pkg(repo)
    s, _ = _abrir(repo, deps, ["m.txt"], cwd=pkg)
    try:
        # cwd é pkg/: o arquivo fora do escopo fica no topo do slot e chega como ../extra.txt.
        (Path(s.slot) / "extra.txt").write_text("extra\n", encoding="utf-8")
        info = wi.save_rejected(s, ["../extra.txt"], deps)
        assert info.saved == 1
        assert info.dir is not None
        assert (Path(info.dir) / "extra.txt").read_text(encoding="utf-8") == "extra\n"
    finally:
        wi.close_session(s)


def test_save_rejected_pula_caminho_que_escapa_do_slot(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    try:
        info = wi.save_rejected(s, ["../../../../etc/hostname"], deps)
        assert info.saved == 0
        assert info.skipped == 1
        assert info.dir is None
    finally:
        wi.close_session(s)


# --- list_slots e gc ---


def test_list_slots_vazio_ocupado_e_livre(repo, deps):
    assert wi.list_slots(deps) == []
    s, _ = _abrir(repo, deps, ["a.txt"])
    try:
        itens = wi.list_slots(deps)
        assert len(itens) == 1
        item = itens[0]
        assert item["busy"] is True
        assert item["slot"] == 0
        assert item["exists"] is True
        assert item["repo"] == os.path.realpath(repo)
    finally:
        wi.close_session(s)
    itens = wi.list_slots(deps)
    assert len(itens) == 1
    assert itens[0]["busy"] is False


def test_gc_remove_slot_velho(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    slot = s.slot
    wi.close_session(s)
    r = wi.gc(deps, now=time.time() + 30 * 86400)
    assert r["slots_removed"] == 1
    assert not os.path.exists(slot)
    saida = _git(repo, "worktree", "list").stdout.decode("utf-8", "replace")
    assert slot not in saida


def test_gc_mantem_slot_recente(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    slot = s.slot
    wi.close_session(s)
    r = wi.gc(deps)
    assert r["slots_removed"] == 0
    assert os.path.isdir(slot)


def test_gc_mantem_slot_ocupado(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    try:
        r = wi.gc(deps, now=time.time() + 30 * 86400)
        assert r["slots_removed"] == 0
        assert os.path.isdir(s.slot)
    finally:
        wi.close_session(s)


def test_gc_remove_slot_de_repositorio_sumido(repo, deps):
    s, _ = _abrir(repo, deps, ["a.txt"])
    slot = s.slot
    wi.close_session(s)
    shutil.rmtree(repo)
    r = wi.gc(deps)
    assert r["slots_removed"] == 1
    assert not os.path.exists(slot)


def test_gc_remove_rejected_velho(deps):
    estado = deps.state_dir()
    velho = estado / "rejected" / "velho"
    novo = estado / "rejected" / "novo"
    velho.mkdir(parents=True)
    (velho / "x.txt").write_text("x\n", encoding="utf-8")
    novo.mkdir(parents=True)
    (novo / "x.txt").write_text("x\n", encoding="utf-8")
    t = time.time() - 8 * 86400
    os.utime(velho, (t, t))
    r = wi.gc(deps)
    assert r["rejected_removed"] == 1
    assert not velho.exists()
    assert novo.exists()


def test_gc_sem_diretorio_de_estado(deps):
    r = wi.gc(deps)
    assert r == {"slots_removed": 0, "rejected_removed": 0, "errors": 0}
