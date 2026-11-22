"""Regressao do gate de contexto de build: matcher do .dockerignore e falha com sensivel."""
import importlib.util
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_docker_context.py"


def _modulo():
    spec = importlib.util.spec_from_file_location(
        "check_docker_context_sob_teste", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _padroes_do_repo(mod):
    return mod.carregar((ROOT / ".dockerignore").read_text(encoding="utf-8"))


def test_dockerignore_do_repo_segura_o_sensivel_e_deixa_o_repo_entrar():
    mod = _modulo()
    padroes = _padroes_do_repo(mod)
    dentro = ["worker_router.py", "bin/claude-ask", "adapters/zcode/bin/worker-bridge"]
    fora = [
        "tests/test_x.py", "adapters/outro/x", "adapters/bonsai/scripts/gpu_coord.py",
        "bin/bonsai-ask", "bin/muse-ask", ".env.local", "sub/auth.json",
        "api-key", "sub/dir/api-key", "chave.key", "run.db-wal", "state.sqlite3",
        "app.log", "novo.bak2", "sessions/sessao.json", "runtime/x.json",
        "node_modules/pacote/index.js", ".venv/bin/python", ".claude/checkpoint.json",
        ".ruff_cache/info", ".worker-ask-isolation", "slot/.worker-ask-isolation",
    ]
    for caminho in dentro:
        assert not mod.excluido(caminho, padroes), caminho
    for caminho in fora:
        assert mod.excluido(caminho, padroes), caminho


def test_repo_atual_deve_passar(tmp_path, capsys):
    # Copia so o versionado: a arvore de trabalho local sempre tem artefatos
    # ignorados pelo git (.ruff_cache, out/*.log, .claude/, marcador de
    # isolamento) que o gate recusa de proposito num build real.
    rastreados = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True,
        capture_output=True, timeout=30).stdout.decode().split("\0")
    for rel in filter(None, rastreados):
        origem = ROOT / rel
        if not origem.exists() and not origem.is_symlink():
            continue  # removido na arvore e ainda nao commitado
        destino = tmp_path / rel
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origem, destino, follow_symlinks=False)
    mod = _modulo()
    assert mod.main(["--root", str(tmp_path)]) == 0
    assert "nenhum sensivel" in capsys.readouterr().out


def test_arvore_com_sensivel_falha_e_aponta_os_arquivos(tmp_path, capsys):
    mod = _modulo()
    (tmp_path / ".dockerignore").write_text(
        (ROOT / ".dockerignore").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "worker_router.py").write_text("# repo\n", encoding="utf-8")
    (tmp_path / "api-key").write_text("segredo\n", encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "sessao.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".worker-ask-isolation").write_text("", encoding="utf-8")
    assert mod.main(["--root", str(tmp_path)]) == 1
    erro = capsys.readouterr().err
    for nome in ("api-key", "sessions/sessao.json", ".worker-ask-isolation"):
        assert nome in erro, nome
    assert "worker_router.py" not in erro


def test_opcao_desconhecida_bloqueia_com_rc2(capsys):
    mod = _modulo()
    assert mod.main(["--coisa"]) == 2
    assert "opcao desconhecida" in capsys.readouterr().err


def test_dockerfile_aponta_a_chave_da_credencial_do_zcode():
    """Sem ZCODE_CREDENTIAL_SECRET_FILE o zcode-cli não decifra o login ("Select a model")."""
    texto = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "ZCODE_CREDENTIAL_SECRET_FILE=/home/worker/.config/zcode-worker/credential-secret" in texto
