"""Testes de ask-check/ask-doctor com HOME e PATH falsos (sem rede/contas)."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _entry(nome: str, path: Path) -> str:
    st = path.stat()
    return f"{nome}|{os.path.realpath(path)}|{int(st.st_mtime)} {st.st_size}"


def _run(script: str, home: Path, bindir: Path):
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    return subprocess.run(
        ["bash", str(ROOT / "bin" / script)],
        capture_output=True, text=True, env=env, timeout=30,
    )


_CHECK_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "--version" ]; then echo "stub 1.0.0"; exit 0; fi\n'
    "exit 0\n"
)


def _check_fixture(tmp_path: Path, nomes=("codex", "claude", "muse", "opencode")):
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    _exe(bindir / "npm", "#!/bin/sh\nexit 1\n")
    known_dir = home / ".local/state/ask-doctor"
    known_dir.mkdir(parents=True)
    linhas = []
    for nome in nomes:
        exe = _exe(bindir / nome, _CHECK_STUB)
        linhas.append(_entry(nome, exe))
    (known_dir / "known-good.txt").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return home, bindir


def test_ask_check_sem_drift(tmp_path):
    home, bindir = _check_fixture(tmp_path)
    proc = _run("ask-check", home, bindir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DRIFT" not in proc.stdout


@pytest.mark.parametrize("nome", ["muse", "opencode"])
def test_ask_check_detecta_alteracao(tmp_path, nome):
    home, bindir = _check_fixture(tmp_path)
    with open(bindir / nome, "a", encoding="utf-8") as fh:
        fh.write("# atualizado sem revalidar\n")
    proc = _run("ask-check", home, bindir)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"DRIFT: {nome} mudou" in proc.stdout


def test_ask_check_sinaliza_binario_removido(tmp_path):
    home, bindir = _check_fixture(tmp_path)
    (bindir / "muse").unlink()
    proc = _run("ask-check", home, bindir)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "DRIFT: muse ausente" in proc.stdout


def _doctor_fixture(tmp_path: Path, *, claude_ok: bool = True):
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    _exe(bindir / "codex", (
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"--help"*) echo "-o, --ephemeral --skip-git-repo-check" ;;\n'
        '  *"login status"*) echo "Logged in" ;;\n'
        '  *) echo "stub 1.0.0" ;;\n'
        "esac\n"
        "exit 0\n"
    ))
    claude_help = "-p, --print" if claude_ok else "ajuda sem a flag"
    _exe(bindir / "claude", f'#!/bin/sh\necho "{claude_help}"\nexit 0\n')
    _exe(bindir / "muse", (
        "#!/bin/sh\n"
        'echo "--json --prompt-file --approval-mode '
        '--no-foreign-personal-context"\n'
        "exit 0\n"
    ))
    _exe(bindir / "opencode", (
        "#!/bin/sh\n"
        "# Blocked by configuration policy\n"
        'echo "--format json --standalone"\n'
        "exit 0\n"
    ))
    _exe(home / ".local/bin/zcode-cli", '#!/bin/sh\necho "--json"\nexit 0\n')
    (home / ".claude").mkdir(parents=True)
    return home, bindir


def test_ask_doctor_sucesso_publica_base_com_muse_opencode(tmp_path):
    home, bindir = _doctor_fixture(tmp_path)
    proc = _run("ask-doctor", home, bindir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    known = (home / ".local/state/ask-doctor/known-good.txt").read_text(encoding="utf-8")
    assert "muse|" in known
    assert "opencode|" in known


def test_ask_doctor_falha_preserva_base_anterior(tmp_path):
    home, bindir = _doctor_fixture(tmp_path, claude_ok=False)
    state = home / ".local/state/ask-doctor"
    state.mkdir(parents=True)
    known = state / "known-good.txt"
    known.write_text("sentinela|/caminho/antigo|1 1\n", encoding="utf-8")
    proc = _run("ask-doctor", home, bindir)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert known.read_text(encoding="utf-8") == "sentinela|/caminho/antigo|1 1\n"
    assert [p.name for p in state.iterdir()] == ["known-good.txt"]


def test_ask_doctor_falha_de_mv_nao_anuncia_publicacao(tmp_path):
    home, bindir = _doctor_fixture(tmp_path)
    state = home / ".local/state/ask-doctor"
    state.mkdir(parents=True)
    # known-good como diretorio: o mv da publicacao falha sem substituir a base
    (state / "known-good.txt").mkdir()
    proc = _run("ask-doctor", home, bindir)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "falha ao publicar a digital" in proc.stderr
    assert "digital gravada" not in proc.stdout
    assert [p.name for p in state.iterdir()] == ["known-good.txt"]
