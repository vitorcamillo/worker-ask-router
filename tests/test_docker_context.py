"""Regressao do gate de contexto de build: matcher do .dockerignore e falha com sensivel."""
import importlib.util
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
    dentro = ["worker_router.py", "bin/muse-ask", "adapters/zcode/bin/worker-bridge"]
    fora = [
        "tests/test_x.py", "adapters/outro/x", ".env.local", "sub/auth.json",
        "api-key", "sub/dir/api-key", "chave.key", "run.db-wal", "state.sqlite3",
        "app.log", "novo.bak2", "sessions/sessao.json", "runtime/x.json",
        "node_modules/pacote/index.js", ".venv/bin/python", ".claude/checkpoint.json",
        ".ruff_cache/info", ".worker-ask-isolation", "slot/.worker-ask-isolation",
    ]
    for caminho in dentro:
        assert not mod.excluido(caminho, padroes), caminho
    for caminho in fora:
        assert mod.excluido(caminho, padroes), caminho


def test_repo_atual_deve_passar(capsys):
    mod = _modulo()
    assert mod.main(["--root", str(ROOT)]) == 0
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
