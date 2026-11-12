import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
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
    _git(r, "add", "base.txt")
    _git(r, "commit", "-qm", "init")
    return r


def _chave(repo, *partes):
    return os.path.normpath(os.path.join(str(repo), *partes))


def test_fingerprint_fora_de_repo_retorna_none(tmp_path):
    assert wr._tree_fingerprint(str(tmp_path)) is None


def test_arquivo_novo_nao_rastreado_aparece(repo):
    antes = wr._tree_fingerprint(str(repo))
    assert antes is not None
    (repo / "novo.txt").write_text("oi\n", encoding="utf-8")
    depois = wr._tree_fingerprint(str(repo))
    assert depois is not None
    chave = _chave(repo, "novo.txt")
    assert chave not in antes
    assert depois[chave] == hashlib.sha256(b"oi\n").hexdigest()


def test_arquivo_modificado_aparece(repo):
    antes = wr._tree_fingerprint(str(repo))
    (repo / "base.txt").write_text("mudou\n", encoding="utf-8")
    depois = wr._tree_fingerprint(str(repo))
    chave = _chave(repo, "base.txt")
    assert chave not in antes and chave in depois
    assert depois[chave] == hashlib.sha256("mudou\n".encode()).hexdigest()


def test_rename_cobre_origem_e_destino(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "add a")
    _git(repo, "mv", "a.txt", "b.txt")
    fp = wr._tree_fingerprint(str(repo))
    assert fp is not None
    assert _chave(repo, "a.txt") in fp
    assert _chave(repo, "b.txt") in fp


def test_out_of_scope_so_autorizado_retorna_vazio(repo):
    antes = wr._tree_fingerprint(str(repo))
    (repo / "base.txt").write_text("nova\n", encoding="utf-8")
    depois = wr._tree_fingerprint(str(repo))
    assert wr._out_of_scope(antes, depois, str(repo), ["base.txt"]) == []


def test_out_of_scope_outro_arquivo(repo):
    antes = wr._tree_fingerprint(str(repo))
    (repo / "outro.txt").write_text("x\n", encoding="utf-8")
    depois = wr._tree_fingerprint(str(repo))
    assert wr._out_of_scope(antes, depois, str(repo), ["base.txt"]) == ["outro.txt"]


def test_out_of_scope_sujeira_antiga_inalterada_nao_aparece(repo):
    (repo / "sujo.txt").write_text("sujo\n", encoding="utf-8")
    antes = wr._tree_fingerprint(str(repo))
    depois = wr._tree_fingerprint(str(repo))
    assert wr._out_of_scope(antes, depois, str(repo), []) == []


def test_out_of_scope_sujeira_antiga_alterada_aparece(repo):
    (repo / "sujo.txt").write_text("v1\n", encoding="utf-8")
    antes = wr._tree_fingerprint(str(repo))
    (repo / "sujo.txt").write_text("v2\n", encoding="utf-8")
    depois = wr._tree_fingerprint(str(repo))
    assert wr._out_of_scope(antes, depois, str(repo), []) == ["sujo.txt"]


def test_out_of_scope_none_retorna_none(repo):
    antes = wr._tree_fingerprint(str(repo))
    assert wr._out_of_scope(None, antes, str(repo), []) is None
    assert wr._out_of_scope(antes, None, str(repo), []) is None


def test_out_of_scope_em_subdiretorio(repo):
    sub = repo / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "sub/a.txt")
    _git(repo, "commit", "-qm", "add sub")
    antes = wr._tree_fingerprint(str(sub))
    assert antes is not None
    (sub / "a.txt").write_text("b\n", encoding="utf-8")
    depois = wr._tree_fingerprint(str(sub))
    assert wr._out_of_scope(antes, depois, str(sub), ["a.txt"]) == []
    (repo / "base.txt").write_text("z\n", encoding="utf-8")
    depois2 = wr._tree_fingerprint(str(sub))
    esperado = os.path.relpath(_chave(repo, "base.txt"), str(sub))
    assert wr._out_of_scope(antes, depois2, str(sub), ["a.txt"]) == [esperado]


def test_stats_summary_conta_out_of_scope(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    sd = state / "worker-ask"
    sd.mkdir(parents=True)
    agora = "2026-01-01T00:00:00+00:00"

    def linha(executor, i, oos="ausente"):
        rec = {"schema": 1, "run_id": "%032x" % i, "ts": agora, "orchestrator": "codex",
               "mode": "write", "cwd": "/tmp/x", "allowed_files": ["a.txt"], "attempts": [],
               "rc": 0, "duration_s": 1.0, "executor": executor, "output_bytes": 5,
               "changed_files": 0, "task_id": "t%d" % i, "result": "completed"}
        if oos != "ausente":
            rec["out_of_scope"] = oos
        return rec

    linhas = [
        linha("zcode-worker", 1, 2),
        linha("zcode-worker", 2, 0),
        linha("zcode-worker", 3, None),
        linha("zcode-worker", 4),
        linha("muse-ask", 5, 3),
    ]
    (sd / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in linhas) + "\n",
                                   encoding="utf-8")
    res = wr._stats_summary()
    assert res["out_of_scope_runs"] == 2
    assert res["by_executor"]["zcode-worker"]["out_of_scope_runs"] == 1
    assert res["by_executor"]["zcode-worker"]["scope_checked_runs"] == 2
    assert res["by_executor"]["muse-ask"]["out_of_scope_runs"] == 1
    assert res["by_executor"]["muse-ask"]["scope_checked_runs"] == 1


def test_out_of_scope_detail_separa_total_da_lista(repo):
    antes = {}
    depois = {_chave(repo, "f%02d.txt" % i): "x" for i in range(60)}
    total, lista = wr._out_of_scope_detail(antes, depois, str(repo), [])
    assert total == 60
    assert len(lista) == 50
    assert wr._out_of_scope(antes, depois, str(repo), []) == lista


def test_stats_summary_atribui_out_of_scope_por_tentativa(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    sd = state / "worker-ask"
    sd.mkdir(parents=True)
    agora = "2026-01-01T00:00:00+00:00"
    rec = {"schema": 1, "run_id": "%032x" % 1, "ts": agora, "orchestrator": "codex",
           "mode": "write", "cwd": "/tmp/x", "allowed_files": ["a.txt"], "attempts": [],
           "rc": 0, "duration_s": 1.0, "executor": "muse-ask", "output_bytes": 5,
           "changed_files": 0, "task_id": "t1", "result": "completed",
           "out_of_scope": 1, "out_of_scope_files": ["fora.txt"],
           "out_of_scope_attempts": [
               {"executor": "zcode-worker", "out_of_scope": 1, "out_of_scope_files": ["fora.txt"]},
               {"executor": "muse-ask", "out_of_scope": 0, "out_of_scope_files": []},
           ]}
    (sd / "runs.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    res = wr._stats_summary()
    assert res["out_of_scope_runs"] == 1
    assert res["by_executor"]["zcode-worker"]["out_of_scope_runs"] == 1
    assert res["by_executor"]["zcode-worker"]["scope_checked_runs"] == 1
    assert res["by_executor"]["muse-ask"]["out_of_scope_runs"] == 0
    assert res["by_executor"]["muse-ask"]["scope_checked_runs"] == 1


@pytest.mark.parametrize("modo", ["write", "plan"])
def test_e2e_router_avisa_e_registra_fora_do_escopo(repo, tmp_path, modo):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
    fake = "#!%s\nimport os,sys\nsys.stdin.read()\nopen(os.path.join(os.environ['FAKE_CWD'],'fora.txt'),'w').write('x')\nprint('ok')\n" % sys.executable
    for nome in ("zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"):
        (bin_dir / nome).write_text(fake)
        (bin_dir / nome).chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bin_dir), XDG_STATE_HOME=str(tmp_path / "st"),
               WORKER_ASK_MIN_TIMEOUT="0", ASK_PRESENTATION_ACTIVE="1", FAKE_CWD=str(repo))
    env.pop("ASK_PRESENTATION_FD", None)
    extra = ["--allowed-file", "base.txt"] if modo == "write" else []
    r = subprocess.run([sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(repo), "--orchestrator",
                        "codex", "--mode", modo, *extra, "tarefa"], capture_output=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert b"fora.txt" in r.stderr and b"alterou 1 arquivo" in r.stderr
    run = [json.loads(x) for x in (tmp_path / "st" / "worker-ask" / "runs.jsonl").read_text().splitlines()
           if "event" not in json.loads(x)][-1]
    assert run["out_of_scope"] == 1 and run["out_of_scope_files"] == ["fora.txt"]


def _e2e_env(repo, tmp_path, bin_dir):
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bin_dir), XDG_STATE_HOME=str(tmp_path / "st"),
               WORKER_ASK_MIN_TIMEOUT="0", ASK_PRESENTATION_ACTIVE="1", FAKE_CWD=str(repo))
    env.pop("ASK_PRESENTATION_FD", None)
    env.pop("WORKER_ASK_NO_MUSE", None)
    return env


def _e2e_bin(tmp_path, fake):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
    for nome in ("zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"):
        (bin_dir / nome).write_text(fake)
        (bin_dir / nome).chmod(0o755)
    return bin_dir


def _last_run(tmp_path):
    return [json.loads(x) for x in (tmp_path / "st" / "worker-ask" / "runs.jsonl").read_text().splitlines()
            if "event" not in json.loads(x)][-1]


def test_e2e_fallback_rc75_preserva_out_of_scope_da_primeira_tentativa(repo, tmp_path, monkeypatch):
    fake = ("#!%s\nimport os,sys\nsys.stdin.read()\n"
            "if os.path.basename(sys.argv[0]) == 'zcode-worker':\n"
            " open(os.path.join(os.environ['FAKE_CWD'],'fora.txt'),'w').write('x')\n"
            " sys.exit(75)\n"
            "print('ok')\n") % sys.executable
    bin_dir = _e2e_bin(tmp_path, fake)
    r = subprocess.run([sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(repo), "--orchestrator",
                        "codex", "--mode", "write", "--allowed-file", "base.txt", "tarefa"],
                       capture_output=True, env=_e2e_env(repo, tmp_path, bin_dir), timeout=60)
    assert r.returncode == 0, r.stderr
    assert b"fora.txt" in r.stderr and b"alterou 1 arquivo" in r.stderr
    run = _last_run(tmp_path)
    assert run["executor"] == "muse-ask"
    assert run["out_of_scope"] == 1 and run["out_of_scope_files"] == ["fora.txt"]
    assert run["out_of_scope_attempts"] == [
        {"executor": "zcode-worker", "out_of_scope": 1, "out_of_scope_files": ["fora.txt"]},
        {"executor": "muse-ask", "out_of_scope": 0, "out_of_scope_files": []},
    ]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    res = wr._stats_summary()
    assert res["out_of_scope_runs"] == 1
    assert res["by_executor"]["zcode-worker"]["out_of_scope_runs"] == 1
    assert res["by_executor"]["muse-ask"]["out_of_scope_runs"] == 0


def test_e2e_router_anuncia_total_acima_do_limite_da_lista(repo, tmp_path):
    fake = ("#!%s\nimport os,sys\nsys.stdin.read()\n"
            "for i in range(60):\n"
            " open(os.path.join(os.environ['FAKE_CWD'], 'fora' + str(i).zfill(2) + '.txt'), 'w').write('x')\n"
            "print('ok')\n") % sys.executable
    bin_dir = _e2e_bin(tmp_path, fake)
    r = subprocess.run([sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(repo), "--orchestrator",
                        "codex", "--mode", "write", "--allowed-file", "base.txt", "tarefa"],
                       capture_output=True, env=_e2e_env(repo, tmp_path, bin_dir), timeout=60)
    assert r.returncode == 0, r.stderr
    assert b"alterou 60 arquivo" in r.stderr
    run = _last_run(tmp_path)
    assert run["out_of_scope"] == 60
    assert len(run["out_of_scope_files"]) == 20
    assert run["out_of_scope_attempts"][0]["out_of_scope"] == 60
