"""Testes e2e do roteador com isolamento por worktree (WORKER_ASK_ISOLATION).

Mesmo padrao de tests/test_out_of_scope.py (test_e2e_*): wrappers falsos no PATH,
HOME/XDG_STATE_HOME dentro de tmp_path e repositorio git descartavel por teste.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ROUTER = str(ROOT / "worker_router.py")
NOMES = ("zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask")
CABECALHO = "#!%s\nimport os,sys,time\nprompt = sys.stdin.read()\n" % sys.executable

CORPO_ESCRITA_AUTORIZADA = """\
open('base.txt', 'w', encoding='utf-8').write('novo\\n')
open('fora.txt', 'w', encoding='utf-8').write('fora\\n')
print('ok')
"""

CORPO_ESTADO_SUJO = """\
if open('b.txt', encoding='utf-8').read() != 'sujo\\n':
    sys.exit(3)
if not os.path.exists('novo.txt'):
    sys.exit(3)
open('base.txt', 'w', encoding='utf-8').write('escrito\\n')
print('ok')
"""

CORPO_CONFLITO = """\
open('base.txt', 'w', encoding='utf-8').write('do worker\\n')
real = os.path.join(os.environ['FAKE_REAL'], 'base.txt')
open(real, 'w', encoding='utf-8').write('concorrente\\n')
print('ok')
"""

CORPO_LENTAO = """\
open('base.txt', 'w', encoding='utf-8').write('antes do term\\n')
open(os.environ['FAKE_MARK'], 'w', encoding='utf-8').write('m')
time.sleep(60)
"""

CORPO_LENTAO_COM_PROGRESSO = """\
open('base.txt', 'w', encoding='utf-8').write('progresso\\n')
time.sleep(60)
"""

CORPO_RC75_ESCREVE_AUTORIZADO = """\
if os.path.basename(sys.argv[0]) == 'zcode-worker':
    open('base.txt', 'w', encoding='utf-8').write('apos falha\\n')
    sys.exit(75)
print('ok')
"""

CORPO_RC75_LIXO = """\
if os.path.basename(sys.argv[0]) == 'zcode-worker':
    open('lixo.txt', 'w', encoding='utf-8').write('x')
    sys.exit(75)
if os.path.exists('lixo.txt'):
    sys.exit(3)
open('base.txt', 'w', encoding='utf-8').write('fallback\\n')
print('ok')
"""

CORPO_SO_FORA = """\
open('fora.txt', 'w', encoding='utf-8').write('fora\\n')
print('ok')
"""

CORPO_ESCREVE_X = """\
open('x.txt', 'w', encoding='utf-8').write('x\\n')
print('ok')
"""

CORPO_ESCRITA_SIMPLES = """\
open('base.txt', 'w', encoding='utf-8').write('simples\\n')
print('ok')
"""

CORPO_ESPELHA_PROMPT = """\
destino = os.environ['FAKE_OUT']
open(destino, 'w', encoding='utf-8').write(prompt)
open(destino + '.env', 'w', encoding='utf-8').write(os.environ.get('WORKER_ASK_ISOLATED', ''))
open(destino + '.cwd', 'w', encoding='utf-8').write(os.getcwd())
open('base.txt', 'w', encoding='utf-8').write('p\\n')
print('ok')
"""

CORPO_FUGA = """\
fuga = os.path.join(os.environ['FAKE_REAL'], 'escapou.txt')
open(fuga, 'w', encoding='utf-8').write('fuga')
open('base.txt', 'w', encoding='utf-8').write('trabalho\\n')
print('ok')
"""

CORPO_ESCREVE_ALVO = """\
open(os.environ['FAKE_ALVO'], 'w', encoding='utf-8').write('x')
time.sleep(1.5)
print('ok')
"""

CORPO_FASES = """\
if os.environ.get('FAKE_FASE') == '2':
    if os.path.exists('resto.txt'):
        sys.exit(3)
    print('ok')
else:
    open('resto.txt', 'w', encoding='utf-8').write('r')
    open('base.txt', 'w', encoding='utf-8').write('fase um\\n')
    print('ok')
"""

CORPO_SUBPASTA = """\
open('m.txt', 'w', encoding='utf-8').write('novo m\\n')
open('../fora.txt', 'w', encoding='utf-8').write('fora')
print('ok')
"""


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
    (r / "b.txt").write_text("b\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _bin(tmp_path, corpo):
    """Cria os cinco wrappers falsos com o mesmo corpo Python (depois do CABECALHO)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
    for nome in NOMES:
        (bin_dir / nome).write_text(CABECALHO + corpo)
        (bin_dir / nome).chmod(0o755)
    return bin_dir


def _env(repo, tmp_path, bin_dir, **extra):
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bin_dir), XDG_STATE_HOME=str(tmp_path / "st"),
               WORKER_ASK_MIN_TIMEOUT="0", ASK_PRESENTATION_ACTIVE="1", FAKE_REAL=str(repo),
               WORKER_ASK_SLOT_PROBE="0")
    for chave in ("ASK_PRESENTATION_FD", "WORKER_ASK_NO_MUSE", "WORKER_ASK_ISOLATION",
                  "WORKER_ASK_ISOLATION_STRICT", "WORKER_ASK_ACTIVE"):
        env.pop(chave, None)
    env.update(extra)
    return env


def _router(repo, env, *args, timeout=90):
    return subprocess.run([sys.executable, ROUTER, "--cwd", str(repo), "--orchestrator", "codex", *args],
                          capture_output=True, env=env, timeout=timeout)


def _last_run(tmp_path):
    linhas = (tmp_path / "st" / "worker-ask" / "runs.jsonl").read_text().splitlines()
    return [json.loads(x) for x in linhas if "event" not in json.loads(x)][-1]


def _runs(tmp_path):
    caminho = tmp_path / "st" / "worker-ask" / "runs.jsonl"
    return [json.loads(x) for x in caminho.read_text(encoding="utf-8").splitlines()
            if "event" not in json.loads(x)]


ISO = {"WORKER_ASK_ISOLATION": "write"}
ISO_ALL = {"WORKER_ASK_ISOLATION": "all"}
STRICT = {"WORKER_ASK_ISOLATION_STRICT": "1"}


# 1. So o arquivo declarado em --allowed-file volta da copia.
def test_isolado_aplica_so_o_arquivo_autorizado(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_ESCRITA_AUTORIZADA)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r.returncode == 0, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "novo\n", r.stderr
    assert not (repo / "fora.txt").exists(), r.stderr
    stderr = r.stderr.decode("utf-8", "replace")
    assert "NAO aplicados" in stderr and "fora.txt" in stderr, r.stderr
    reg = _last_run(tmp_path)
    copia = tmp_path / "st" / "worker-ask" / "rejected" / reg["run_id"] / "fora.txt"
    assert copia.exists(), r.stderr
    assert reg["isolation"] == "worktree", reg
    assert reg["out_of_scope"] == 1, reg
    assert reg["sync_applied"] == 1, reg
    assert reg["sync_conflicts"] == 0, reg
    assert reg["oos_discarded"] == 1, reg


# 2. Estado sujo (modificado + nao rastreado) chega ao worker dentro do slot.
def test_estado_sujado_chega_ao_worker(tmp_path, repo):
    (repo / "b.txt").write_text("sujo\n", encoding="utf-8")
    (repo / "novo.txt").write_text("n\n", encoding="utf-8")
    bin_dir = _bin(tmp_path, CORPO_ESTADO_SUJO)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r.returncode == 0, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "escrito\n", r.stderr


# 3. Arquivo autorizado alterado na arvore real durante a execucao: conflito, rc 1.
def test_conflito_na_devolucao_nao_sobrescreve_arquivo_real(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_CONFLITO)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r.returncode == 1, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "concorrente\n", r.stderr
    assert "conflito na devolucao" in r.stderr.decode("utf-8", "replace"), r.stderr
    assert _last_run(tmp_path)["sync_conflicts"] == 1


# 4. SIGTERM no roteador devolve o trabalho que o wrapper ja escreveu.
def test_sigterm_preserva_trabalho_do_worker(tmp_path, repo):
    marca = tmp_path / "marca"
    bin_dir = _bin(tmp_path, CORPO_LENTAO)
    env = _env(repo, tmp_path, bin_dir, **ISO, FAKE_MARK=str(marca))
    proc = subprocess.Popen(
        [sys.executable, ROUTER, "--cwd", str(repo), "--orchestrator", "codex",
         "--mode", "write", "--allowed-file", "base.txt", "tarefa"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        limite = time.monotonic() + 30
        while not marca.exists() and time.monotonic() < limite:
            time.sleep(0.05)
        assert marca.exists(), "wrapper nao chegou a escrever"
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, ValueError):
                pass
    assert proc.returncode == 143, f"rc={proc.returncode}"
    assert (repo / "base.txt").read_text(encoding="utf-8") == "antes do term\n", proc.returncode


# 5. Timeout com progresso preserva o escrito, direto e isolado.
@pytest.mark.parametrize("extra", [{}, ISO], ids=["direto", "isolado"])
def test_timeout_com_progresso_igual_ao_modo_direto(tmp_path, repo, extra):
    bin_dir = _bin(tmp_path, CORPO_LENTAO_COM_PROGRESSO)
    env = _env(repo, tmp_path, bin_dir, **extra)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt",
                "--timeout", "2", "--step-timeout", "2", "tarefa")
    assert r.returncode == 124, r.stderr
    assert "estourou com progresso" in r.stderr.decode("utf-8", "replace"), r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "progresso\n", r.stderr


# 6. rc 75 com arquivo autorizado alterado para a execucao, direto e isolado.
@pytest.mark.parametrize("extra", [{}, ISO], ids=["direto", "isolado"])
def test_rc75_com_autorizado_alterado_igual_ao_modo_direto(tmp_path, repo, extra):
    bin_dir = _bin(tmp_path, CORPO_RC75_ESCREVE_AUTORIZADO)
    env = _env(repo, tmp_path, bin_dir, **extra)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r.returncode == 1, r.stderr
    assert "arquivos autorizados alterados apos falha rc75" in r.stderr.decode("utf-8", "replace"), r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "apos falha\n", r.stderr


# 7. rc 75 sem mudanca: fallback roda em slot limpo (sem lixo da tentativa anterior).
def test_rc75_sem_mudanca_faz_fallback_com_slot_limpo(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_RC75_LIXO)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r.returncode == 0, r.stderr
    assert _last_run(tmp_path)["executor"] == "muse-ask", r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "fallback\n", r.stderr
    assert not (repo / "lixo.txt").exists(), r.stderr


# 8. WORKER_ASK_ISOLATION=all isola tambem o modo plan.
def test_isolamento_all_isola_mode_plan(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_SO_FORA)
    env = _env(repo, tmp_path, bin_dir, **ISO_ALL)
    r = _router(repo, env, "--mode", "plan", "tarefa")
    assert r.returncode == 0, r.stderr
    assert not (repo / "fora.txt").exists(), r.stderr
    assert "em modo plan; NAO aplicados" in r.stderr.decode("utf-8", "replace"), r.stderr


# 9. WORKER_ASK_ISOLATION=write nao isola o modo plan (comportamento antigo).
def test_isolamento_write_nao_isola_mode_plan(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_SO_FORA)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(repo, env, "--mode", "plan", "tarefa")
    assert r.returncode == 0, r.stderr
    assert (repo / "fora.txt").exists(), r.stderr
    assert "isolation" not in _last_run(tmp_path)


# 10a. Fora de repositorio git: roda direto, registro marca not_git.
def test_fora_de_git_roda_direto(tmp_path):
    solto = tmp_path / "solto"
    solto.mkdir()
    bin_dir = _bin(tmp_path, CORPO_ESCREVE_X)
    env = _env(solto, tmp_path, bin_dir, **ISO)
    r = _router(solto, env, "--mode", "write", "--allowed-file", "x.txt", "tarefa")
    assert r.returncode == 0, r.stderr
    assert (solto / "x.txt").exists(), r.stderr
    reg = _last_run(tmp_path)
    assert reg["isolation"] == "none", reg
    assert reg["isolation_reason"] == "not_git", reg


# 10b. Fora de git com WORKER_ASK_ISOLATION_STRICT=1: falha com rc 1.
def test_fora_de_git_strict_falha(tmp_path):
    solto = tmp_path / "solto"
    solto.mkdir()
    bin_dir = _bin(tmp_path, CORPO_ESCREVE_X)
    env = _env(solto, tmp_path, bin_dir, **{**ISO, **STRICT})
    r = _router(solto, env, "--mode", "write", "--allowed-file", "x.txt", "tarefa")
    assert r.returncode == 1, r.stderr


# 11. Sem a variavel, comportamento antigo e nenhum worktree criado.
def test_desligado_mantem_comportamento_antigo(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_SO_FORA)
    env = _env(repo, tmp_path, bin_dir)
    r = _router(repo, env, "--mode", "plan", "tarefa")
    assert r.returncode == 0, r.stderr
    assert (repo / "fora.txt").exists(), r.stderr
    assert "pode incluir" in r.stderr.decode("utf-8", "replace"), r.stderr
    assert "isolation" not in _last_run(tmp_path)
    assert not (tmp_path / "st" / "worker-ask" / "worktrees").exists(), r.stderr


# 12. Prompt reescrito para o slot e ambiente marcado com WORKER_ASK_ISOLATED=1.
def test_prompt_isolado_troca_caminho_e_marca_ambiente(tmp_path, repo):
    saida = tmp_path / "espelho"
    bin_dir = _bin(tmp_path, CORPO_ESPELHA_PROMPT)
    env = _env(repo, tmp_path, bin_dir, **ISO, FAKE_OUT=str(saida))
    prompt = "edite " + str(repo.resolve()) + "/base.txt"
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", prompt)
    assert r.returncode == 0, r.stderr
    gravado = saida.read_text(encoding="utf-8")
    assert "Execucao isolada" in gravado, gravado
    assert str(repo.resolve()) + "/base.txt" not in gravado, gravado
    cwd_slot = Path(str(saida) + ".cwd").read_text(encoding="utf-8")
    assert cwd_slot + "/base.txt" in gravado, gravado
    assert Path(str(saida) + ".env").read_text(encoding="utf-8").strip() == "1"
    assert cwd_slot.startswith(str(tmp_path / "st" / "worker-ask" / "worktrees")), cwd_slot


# 13. Worker que escreve por caminho absoluto na arvore real: aviso e registro.
def test_worker_que_escapa_da_copia_marca_arvore_real_mudou(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_FUGA)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r.returncode == 0, r.stderr
    assert "arvore real mudou" in r.stderr.decode("utf-8", "replace"), r.stderr
    assert _last_run(tmp_path)["real_tree_changed"] == 1


# 14. Duas invocacoes simultaneas no mesmo repositorio usam slots distintos.
def test_duas_invocacoes_simultaneas_usam_slots_distintos(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_ESCREVE_ALVO)
    argv = [sys.executable, ROUTER, "--cwd", str(repo), "--orchestrator", "codex",
            "--mode", "write", "tarefa"]
    pops = []
    for alvo in ("base.txt", "b.txt"):
        env = _env(repo, tmp_path, bin_dir, **ISO, FAKE_ALVO=alvo)
        pops.append(subprocess.Popen(argv + ["--allowed-file", alvo], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))
    try:
        saidas = [proc.communicate(timeout=90) for proc in pops]
    finally:
        for proc in pops:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.communicate(timeout=10)
                except ValueError:
                    pass
    for proc, (_, erro) in zip(pops, saidas):
        assert proc.returncode == 0, erro
    assert (repo / "base.txt").read_text(encoding="utf-8") == "x", saidas
    assert (repo / "b.txt").read_text(encoding="utf-8") == "x", saidas
    slots = set()
    for diretorio in (tmp_path / "st" / "worker-ask" / "worktrees").iterdir():
        slots.update(filho.name for filho in diretorio.iterdir())
    assert {"slot-0", "slot-1"} <= slots, slots
    registros = _runs(tmp_path)
    assert len(registros) == 2, registros
    for reg in registros:
        assert reg.get("real_tree_changed", 0) == 0, reg


# 15. Slot reaproveitado nao carrega sujeira da execucao anterior.
def test_slot_reaproveitado_sem_sujeira(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_FASES)
    env1 = _env(repo, tmp_path, bin_dir, **ISO, FAKE_FASE="1")
    r1 = _router(repo, env1, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r1.returncode == 0, r1.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "fase um\n", r1.stderr
    env2 = _env(repo, tmp_path, bin_dir, **ISO, FAKE_FASE="2")
    r2 = _router(repo, env2, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r2.returncode == 0, r2.stderr


# 16. --status lista o slot e --isolation-gc limpa com rc 0.
def test_status_e_isolation_gc(tmp_path, repo):
    bin_dir = _bin(tmp_path, CORPO_ESCRITA_SIMPLES)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(repo, env, "--mode", "write", "--allowed-file", "base.txt", "tarefa")
    assert r.returncode == 0, r.stderr
    proc_status = subprocess.run([sys.executable, ROUTER, "--status"], capture_output=True,
                                 env=env, timeout=30)
    assert proc_status.returncode == 0, proc_status.stderr
    status = json.loads(proc_status.stdout.decode("utf-8"))
    assert any(s.get("slot") == 0 and s.get("busy") is False for s in status["isolation_slots"]), status
    proc_gc = subprocess.run([sys.executable, ROUTER, "--isolation-gc"], capture_output=True,
                             env=env, timeout=30)
    assert proc_gc.returncode == 0, proc_gc.stderr
    gc = json.loads(proc_gc.stdout.decode("utf-8"))
    assert {"slots_removed", "rejected_removed", "errors"} <= set(gc), gc


# 17. cwd em subdiretorio: devolucao relativa ao subdiretorio, fora do escopo descartado.
def test_cwd_em_subdiretorio(tmp_path, repo):
    pacote = repo / "pkg"
    pacote.mkdir()
    (pacote / "m.txt").write_text("m\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pkg")
    bin_dir = _bin(tmp_path, CORPO_SUBPASTA)
    env = _env(repo, tmp_path, bin_dir, **ISO)
    r = _router(pacote, env, "--mode", "write", "--allowed-file", "m.txt", "tarefa")
    assert r.returncode == 0, r.stderr
    assert (pacote / "m.txt").read_text(encoding="utf-8") == "novo m\n", r.stderr
    assert not (repo / "fora.txt").exists(), r.stderr
