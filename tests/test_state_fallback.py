import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

ROUTER = str(ROOT / "worker_router.py")
NOMES = ("zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask")
CABECALHO = ("#!%s\nimport os,sys,time\nprompt = sys.stdin.read()\n"
             "eu = os.path.basename(sys.argv[0])\n") % sys.executable
OK = "open('base.txt','w').write('feito por ' + eu)\nprint('ok')\n"
SAIR_1 = "sys.exit(1)\n"
SAIR_75 = "sys.exit(75)\n"
DORME = "time.sleep(60)\n"
LINHA_LAUNCHER = ("ERRO: launcher de autenticacao incompativel com o bundle do ZCode "
                  "Arquivos podem ter sido alterados; revise o diff antes de tentar novamente.\n")


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
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _bin(tmp_path, corpos):
    """corpos: dict nome_do_executor -> corpo Python; quem não estiver no dict usa OK."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
    for nome in NOMES:
        (bin_dir / nome).write_text(CABECALHO + corpos.get(nome, OK))
        (bin_dir / nome).chmod(0o755)
    return bin_dir


def _env(repo, tmp_path, bin_dir, **extra):
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bin_dir), XDG_STATE_HOME=str(tmp_path / "st"),
               WORKER_ASK_MIN_TIMEOUT="0", WORKER_ASK_ZCODE_QUEUE_TIMEOUT="0.001",
               ASK_PRESENTATION_ACTIVE="1", FAKE_REAL=str(repo),
               WORKER_ASK_ISOLATION="write", WORKER_ASK_STATE_FALLBACK="1")
    for chave in ("ASK_PRESENTATION_FD", "WORKER_ASK_NO_MUSE", "WORKER_ASK_ISOLATION_STRICT", "WORKER_ASK_ACTIVE"):
        env.pop(chave, None)
    for chave, valor in extra.items():
        if valor is None:
            env.pop(chave, None)
        else:
            env[chave] = valor
    return env


def _router(repo, env, *args, timeout=120):
    return subprocess.run([sys.executable, ROUTER, "--cwd", str(repo), "--orchestrator", "codex",
                           "--mode", "write", "--allowed-file", "base.txt", *args, "tarefa"],
                          capture_output=True, env=env, timeout=timeout)


def _last_run(tmp_path):
    linhas = (tmp_path / "st" / "worker-ask" / "runs.jsonl").read_text().splitlines()
    return [json.loads(x) for x in linhas if "event" not in json.loads(x)][-1]


def test_rc1_sem_efeito_segue_para_proximo(repo, tmp_path):
    bin_dir = _bin(tmp_path, {"zcode-worker": SAIR_1})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 0, r.stderr
    rec = _last_run(tmp_path)
    assert rec["executor"] == "muse-ask", r.stderr
    assert rec.get("state_fallbacks") == 1, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "feito por muse-ask", r.stderr
    assert b"sem efeito" in r.stderr, r.stderr


def test_timeout_sem_efeito_segue_e_grava_cooldown(repo, tmp_path):
    bin_dir = _bin(tmp_path, {"zcode-worker": DORME})
    env = _env(repo, tmp_path, bin_dir)
    r = _router(repo, env, "--timeout", "60", "--step-timeout", "2")
    assert r.returncode == 0, r.stderr
    rec = _last_run(tmp_path)
    assert rec["executor"] == "muse-ask", r.stderr
    assert rec.get("state_fallbacks") == 1, r.stderr
    status = subprocess.run([sys.executable, ROUTER, "--status"], capture_output=True, env=env, timeout=30)
    assert status.returncode == 0, status.stderr
    dados = json.loads(status.stdout.decode("utf-8"))
    assert dados["cache"]["zcode-worker"]["blocked_reason"] == "timeout", status.stdout


def test_nao_segue_se_alterou_arquivo_autorizado(repo, tmp_path):
    corpo = "open('base.txt','w').write('parcial')\nsys.exit(1)\n"
    bin_dir = _bin(tmp_path, {"zcode-worker": corpo})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 1, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "parcial", r.stderr
    assert _last_run(tmp_path).get("state_fallbacks", 0) == 0, r.stderr


def test_nao_segue_se_alterou_arquivo_fora_do_escopo(repo, tmp_path):
    corpo = "open('fora.txt','w').write('x')\nsys.exit(1)\n"
    bin_dir = _bin(tmp_path, {"zcode-worker": corpo})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 1, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "base\n", r.stderr


def test_nao_segue_se_autorizado_mudou_na_arvore_real(repo, tmp_path):
    corpo = "open(os.path.join(os.environ['FAKE_REAL'],'base.txt'),'w').write('concorrente')\nsys.exit(1)\n"
    bin_dir = _bin(tmp_path, {"zcode-worker": corpo})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 1, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "concorrente", r.stderr


@pytest.mark.parametrize("alvo", ["codex-ask", "claude-ask"])
def test_nao_segue_executor_com_shell(repo, tmp_path, alvo):
    corpos = {nome: SAIR_75 for nome in NOMES}
    corpos[alvo] = SAIR_1
    bin_dir = _bin(tmp_path, corpos)
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 1, r.stderr
    assert r.stdout == b""
    assert (repo / "base.txt").read_text(encoding="utf-8") == "base\n", r.stderr
    assert _last_run(tmp_path).get("state_fallbacks", 0) == 0, r.stderr


def test_nao_segue_rc143(repo, tmp_path):
    bin_dir = _bin(tmp_path, {"zcode-worker": "sys.exit(143)\n"})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 143, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "base\n", r.stderr


def test_nao_segue_rc2(repo, tmp_path):
    # Só rc 1 e timeout entram na regra; outro código de falha para a cadeia.
    bin_dir = _bin(tmp_path, {"zcode-worker": "sys.exit(2)\n"})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 2, r.stderr
    assert r.stdout == b""
    assert _last_run(tmp_path).get("state_fallbacks", 0) == 0, r.stderr


def test_nao_segue_marcador_launcher_incompativel(repo, tmp_path):
    corpo = "sys.stderr.write(%s)\nsys.exit(1)\n" % repr(LINHA_LAUNCHER)
    bin_dir = _bin(tmp_path, {"zcode-worker": corpo})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 1, r.stderr
    assert b"fallback bloqueado" in r.stderr, r.stderr


def test_nao_segue_com_link_de_dependencia(repo, tmp_path):
    # node_modules ignorado vira link do slot para a árvore real: mudanças ali são
    # invisíveis à impressão do git status; sem provar a integridade, o fallback é negado.
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "gitignore")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x\n", encoding="utf-8")
    bin_dir = _bin(tmp_path, {"zcode-worker": SAIR_1})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 1, r.stderr
    assert b"sem efeito" not in r.stderr, r.stderr
    assert _last_run(tmp_path).get("state_fallbacks", 0) == 0, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "base\n", r.stderr
    assert (repo / "node_modules" / "x.js").read_text(encoding="utf-8") == "x\n", r.stderr


def test_chave_desligada_para_como_hoje(repo, tmp_path):
    bin_dir = _bin(tmp_path, {"zcode-worker": SAIR_1})
    for valor in (None, "0"):
        env = _env(repo, tmp_path, bin_dir, WORKER_ASK_STATE_FALLBACK=valor)
        r = _router(repo, env)
        assert r.returncode == 1, r.stderr
        assert (repo / "base.txt").read_text(encoding="utf-8") == "base\n", r.stderr


def test_sem_isolamento_para_como_hoje(repo, tmp_path):
    bin_dir = _bin(tmp_path, {"zcode-worker": SAIR_1})
    env = _env(repo, tmp_path, bin_dir, WORKER_ASK_ISOLATION=None)
    r = _router(repo, env)
    assert r.returncode == 1, r.stderr


def test_timeout_com_chave_desligada_para_como_hoje(repo, tmp_path):
    bin_dir = _bin(tmp_path, {"zcode-worker": DORME})
    env = _env(repo, tmp_path, bin_dir, WORKER_ASK_STATE_FALLBACK=None)
    r = _router(repo, env, "--timeout", "60", "--step-timeout", "2")
    assert r.returncode == 124, r.stderr


def test_todos_falham_conta_tres_fallbacks(repo, tmp_path):
    falham = ("zcode-worker", "muse-ask", "opencode-ask", "codex-ask")
    bin_dir = _bin(tmp_path, {nome: SAIR_1 for nome in falham})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 1, r.stderr
    assert _last_run(tmp_path).get("state_fallbacks") == 3, r.stderr
    assert (repo / "base.txt").read_text(encoding="utf-8") == "base\n", r.stderr


def test_dois_fallbacks_em_sequencia(repo, tmp_path):
    corpos = {"zcode-worker": SAIR_1, "muse-ask": SAIR_1, "opencode-ask": OK}
    bin_dir = _bin(tmp_path, corpos)
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 0, r.stderr
    rec = _last_run(tmp_path)
    assert rec["executor"] == "opencode-ask", r.stderr
    assert rec.get("state_fallbacks") == 2, r.stderr


def test_estatisticas_contam_fallback_isolado(repo, tmp_path, monkeypatch):
    bin_dir = _bin(tmp_path, {"zcode-worker": SAIR_1})
    r = _router(repo, _env(repo, tmp_path, bin_dir))
    assert r.returncode == 0, r.stderr
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    resumo = wr._stats_summary()
    assert resumo["by_executor"]["muse-ask"]["state_fallback_runs"] == 1, resumo
    assert resumo["by_executor"]["muse-ask"]["isolated_runs"] == 1, resumo


def _cenario_unitario(tmp_path, monkeypatch):
    real = tmp_path / "real"
    copia = tmp_path / "copia"
    real.mkdir()
    copia.mkdir()
    (real / "base.txt").write_text("conteudo\n", encoding="utf-8")
    (copia / "base.txt").write_text("conteudo\n", encoding="utf-8")
    allowed = ["base.txt"]
    attempt = {"sync": SimpleNamespace(applied=[], conflicts=[]), "fp_before": {}, "fp_after": {},
               "cwd": str(copia),
               "prep": SimpleNamespace(snap_slot_before=wr.snapshot_allowed(str(copia), allowed),
                                       links=[])}
    monkeypatch.setattr(wr, "_ISO", {"x": 1})
    monkeypatch.setenv("WORKER_ASK_STATE_FALLBACK", "1")
    return {"attempt": attempt, "name": "zcode-worker", "mode": "write", "cwd": str(real),
            "allowed": allowed, "snap_before": wr.snapshot_allowed(str(real), allowed),
            "marker": b"", "deadline": time.monotonic() + 60}


def _trocar_por_symlink(alvo):
    alvo.unlink()
    alvo.symlink_to("alvo_inexistente")


@pytest.mark.parametrize("nome", ["zcode-worker", "muse-ask", "opencode-ask"])
def test_unitario_cenario_base_segue(nome, tmp_path, monkeypatch):
    args = _cenario_unitario(tmp_path, monkeypatch)
    args["name"] = nome
    assert wr._iso_no_effect(**args) is True


MUTACOES = [
    pytest.param(lambda a, c: a.update(attempt=None), id="attempt_none"),
    pytest.param(lambda a, c: c["mp"].setattr(wr, "_ISO", None), id="iso_none"),
    pytest.param(lambda a, c: a.update(mode="plan"), id="modo_plan"),
    pytest.param(lambda a, c: c["mp"].delenv("WORKER_ASK_STATE_FALLBACK"), id="env_ausente"),
    pytest.param(lambda a, c: c["mp"].setenv("WORKER_ASK_STATE_FALLBACK", "0"), id="env_zero"),
    pytest.param(lambda a, c: a.update(name="codex-ask"), id="nome_codex_ask"),
    pytest.param(lambda a, c: a.update(name="claude-ask"), id="nome_claude_ask"),
    pytest.param(lambda a, c: a.update(marker=b"launcher_incompatible_effects"), id="marcador_efeitos"),
    pytest.param(lambda a, c: a.update(deadline=time.monotonic() - 1), id="prazo_esgotado"),
    pytest.param(lambda a, c: a["attempt"].update(sync=None), id="sync_none"),
    pytest.param(lambda a, c: setattr(a["attempt"]["sync"], "applied", ["base.txt"]), id="sync_aplicou"),
    pytest.param(lambda a, c: setattr(a["attempt"]["sync"], "conflicts", ["base.txt"]), id="sync_conflitos"),
    pytest.param(lambda a, c: a["attempt"].update(fp_before=None), id="fp_before_none"),
    pytest.param(lambda a, c: a["attempt"].update(fp_after=None), id="fp_after_none"),
    pytest.param(lambda a, c: a["attempt"].update(fp_before=None, fp_after=None), id="fp_ambos_none"),
    pytest.param(lambda a, c: a["attempt"].update(fp_after={"/x/fora.txt": "abc"}), id="fp_after_diferente"),
    pytest.param(lambda a, c: (c["copia"] / "base.txt").write_text("mudou\n", encoding="utf-8"), id="copia_alterada"),
    pytest.param(lambda a, c: (c["real"] / "base.txt").write_text("mudou\n", encoding="utf-8"), id="real_alterada"),
    pytest.param(lambda a, c: _trocar_por_symlink(c["copia"] / "base.txt"), id="copia_symlink"),
    pytest.param(lambda a, c: setattr(a["attempt"]["prep"], "links", ["node_modules"]), id="links_dependencia"),
]


@pytest.mark.parametrize("mutacao", MUTACOES)
def test_unitario_variacao_unica_bloqueia(mutacao, tmp_path, monkeypatch):
    args = _cenario_unitario(tmp_path, monkeypatch)
    contexto = {"mp": monkeypatch, "copia": tmp_path / "copia", "real": tmp_path / "real"}
    mutacao(args, contexto)
    assert wr._iso_no_effect(**args) is False


def test_unitario_marcador_sem_sufixo_efeitos_nao_bloqueia(tmp_path, monkeypatch):
    args = _cenario_unitario(tmp_path, monkeypatch)
    args["marker"] = b"launcher_incompatible"
    assert wr._iso_no_effect(**args) is True
