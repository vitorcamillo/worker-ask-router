import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Copia fiel da fixture de tests/test_runs_log.py (sem importar o modulo de teste).
FAKE_NAMES = ["zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask"]

FAKE_SCRIPT = r'''
import json, os, sys, time, subprocess

name = os.path.basename(sys.argv[0])
record = {"name": name, "argv": sys.argv[1:], "stdin": ""}
try:
    record["stdin"] = sys.stdin.read()
except Exception as e:
    record["stdin"] = "<err %s>" % e

log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(record) + "\n")

mode = os.environ.get("FAKE_MODE_" + name.upper().replace("-", "_"), os.environ.get("FAKE_MODE", "exit"))
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "10")))
elif mode == "grandchild":
    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(100)"])
    with open(os.environ["FAKE_PID_FILE"], "w") as f:
        f.write(str(child.pid))
    time.sleep(100)
elif mode == "deletefile":
    os.unlink(os.path.join(os.environ["FAKE_CWD"], os.environ["FAKE_WRITE_REL"]))
elif mode == "chmodfile":
    os.chmod(os.path.join(os.environ["FAKE_CWD"], os.environ["FAKE_WRITE_REL"]), 0o700)
elif mode == "writefile" or mode == "writesleep":
    rel = os.environ.get("FAKE_WRITE_REL")
    content = os.environ.get("FAKE_WRITE_CONTENT", "x")
    cwd = os.environ.get("FAKE_CWD")
    if rel and cwd:
        full = os.path.join(cwd, rel)
        d = os.path.dirname(full)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    if mode == "writesleep":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "10")))
elif mode == "delegate":
    result = subprocess.run([sys.executable, os.environ["FAKE_ROUTER"], "--cwd", os.environ["FAKE_CWD"], "--orchestrator", "codex", "--mode", "plan", "nested"], capture_output=True)
    sys.stdout.write("NESTED_RC=" + str(result.returncode))
    sys.exit(0)
elif mode == "overflow":
    sys.stdout.write("A" * (2 * 1024 * 1024))
    sys.stdout.flush()

rc_env = "FAKE_RC_" + name.upper().replace("-", "_")
rc = int(os.environ.get(rc_env, os.environ.get("FAKE_RC", "0")))
sys.stdout.write(os.environ.get("FAKE_STDOUT_" + name.upper().replace("-", "_"), "READY:" + name))
sys.stderr.write(os.environ.get("FAKE_STDERR", "private-worker-stderr"))
sys.exit(rc)
'''


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in FAKE_NAMES:
        p = bin_dir / name
        p.write_text("#!" + sys.executable + "\n" + FAKE_SCRIPT + "\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("WORKER_ASK_MIN_TIMEOUT", "0")
    monkeypatch.setenv("ASK_PRESENTATION_ACTIVE", "1")
    monkeypatch.delenv("ASK_PRESENTATION_FD", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("FAKE_CWD", str(work))
    for key in ("MUSE_ASK_MODEL", "MUSE_ASK_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    return {"bin": bin_dir, "state": state, "work": work, "tmp": tmp_path}


def run_cli(fake_env, extra_args, env_extra=None, input_text=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    work = fake_env["work"]
    cmd = [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(work), "--orchestrator", "codex"] + extra_args
    return subprocess.run(
        cmd,
        input=input_text.encode() if input_text is not None else None,
        capture_output=True,
        env=env,
        timeout=60,
    )


def runs_path(fake_env):
    return fake_env["state"] / "worker-ask" / "runs.jsonl"


def read_runs(fake_env):
    lines = runs_path(fake_env).read_text(encoding="utf-8").splitlines()
    assert lines, "runs.jsonl vazio"
    return [json.loads(line) for line in lines]


def run_row(task_id, result=None, executor="zcode-worker", rc=0, duration_s=1.0):
    row = {"schema": 1, "run_id": "0" * 32, "ts": "2026-01-01T00:00:00+00:00",
           "orchestrator": "codex", "mode": "write", "cwd": "/tmp/x",
           "spec_sha256": "abc123", "prompt_bytes": 2, "allowed_files": ["a.txt"],
           "attempts": [], "rc": rc, "duration_s": duration_s,
           "executor": executor, "output_bytes": 5, "changed_files": 0,
           "task_id": task_id}
    if result is not None:
        row["result"] = result
    return row


def seed_runs(fake_env, rows, backup=False):
    sd = fake_env["state"] / "worker-ask"
    sd.mkdir(parents=True, exist_ok=True)
    alvo = sd / ("runs.jsonl.1" if backup else "runs.jsonl")
    alvo.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_run_com_task_id_grava_task_id_e_result(fake_env):
    stdout = "resposta\n```json\n{\"execution_state\": \"partial\"}\n```"
    r = run_cli(fake_env, ["--task-id", "t1", "--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_STDOUT_ZCODE_WORKER": stdout})
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["task_id"] == "t1"
    assert rec["result"] == "partial"


def test_run_sem_task_id_e_sem_bloco_json_grava_nulos(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["task_id"] is None
    assert "result" in rec
    assert rec["result"] is None


def test_task_id_invalido_recusa(fake_env):
    r = run_cli(fake_env, ["--task-id", "id invalido!", "hi"])
    assert r.returncode == 1
    assert "erro: --task-id invalido" in r.stderr.decode("utf-8")
    assert not runs_path(fake_env).exists()
    r = run_cli(fake_env, ["--mark-redo", "id invalido!"])
    assert r.returncode == 1
    assert "erro: --task-id invalido" in r.stderr.decode("utf-8")


def test_orcamento_esgotado_recusa_quinta_execucao(fake_env, tmp_path):
    fake_log = tmp_path / "fake.log"
    seed_runs(fake_env, [run_row("cheio", result="completed") for _ in range(4)])
    r = run_cli(fake_env, ["--task-id", "cheio", "--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_LOG": str(fake_log)})
    assert r.returncode == 1
    err = r.stderr.decode("utf-8")
    assert "orcamento esgotado para task_id cheio (4 execucoes)" in err
    assert not fake_log.exists()
    assert len(read_runs(fake_env)) == 4


def test_orcamento_considera_runs_jsonl_ponto_1(fake_env):
    seed_runs(fake_env, [run_row("velha", result="completed") for _ in range(4)], backup=True)
    r = run_cli(fake_env, ["--task-id", "velha", "--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 1
    assert "orcamento esgotado para task_id velha" in r.stderr.decode("utf-8")


def test_blocked_question_nao_consomem_orcamento(fake_env):
    rows = [run_row("bq", result="completed") for _ in range(3)]
    rows.append(run_row("bq", result="blocked_question"))
    seed_runs(fake_env, rows)
    r = run_cli(fake_env, ["--task-id", "bq", "--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0


def test_outra_task_id_nao_e_afetada(fake_env):
    seed_runs(fake_env, [run_row("cheio", result="completed") for _ in range(4)])
    r = run_cli(fake_env, ["--task-id", "outra", "--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0


def test_mark_redo_grava_evento_com_dados_da_ultima_run(fake_env):
    r = run_cli(fake_env, ["--task-id", "mk", "--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0
    r = run_cli(fake_env, ["--mark-redo", "mk"])
    assert r.returncode == 0
    recs = read_runs(fake_env)
    assert len(recs) == 2
    redo = recs[-1]
    assert redo["event"] == "redo"
    assert redo["task_id"] == "mk"
    assert redo["orchestrator"] == "codex"
    assert redo["cwd"] == recs[0]["cwd"]
    assert redo["allowed_files"] == ["a.txt"]
    assert len(redo["run_id"]) == 32
    assert redo["ts"]


def test_mark_redo_task_id_desconhecida_recusa(fake_env):
    r = run_cli(fake_env, ["--mark-redo", "fantasma"])
    assert r.returncode == 1
    assert "erro: task_id sem execucao registrada" in r.stderr.decode("utf-8")
    assert not runs_path(fake_env).exists()


def test_mark_redo_respeita_runs_log_desligado(fake_env):
    r = run_cli(fake_env, ["--task-id", "mk", "--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0
    r = run_cli(fake_env, ["--mark-redo", "mk"], {"WORKER_ASK_RUNS_LOG": "0"})
    assert r.returncode == 0
    assert len(read_runs(fake_env)) == 1


def test_stats_com_log_de_exemplo(fake_env):
    rows = [
        run_row("t1", result="completed", executor="zcode-worker", rc=0, duration_s=1.0),
        run_row("t1", executor="zcode-worker", rc=1, duration_s=3.0),
        run_row("t2", result="partial", executor="muse-ask", rc=0, duration_s=2.0),
    ] + [run_row("t3", result="blocked_question", executor="claude-ask") for _ in range(4)]
    redo = {"schema": 1, "event": "redo", "run_id": "f" * 32,
            "ts": "2026-01-01T00:00:00+00:00", "task_id": "t1",
            "orchestrator": "codex", "cwd": "/tmp/x", "allowed_files": ["a.txt"]}
    seed_runs(fake_env, rows + [redo])
    r = run_cli(fake_env, ["--stats"])
    assert r.returncode == 0
    out = json.loads(r.stdout.decode("utf-8"))
    assert out["runs"] == 7
    assert out["redo"] == 1
    assert out["by_executor"]["zcode-worker"]["runs"] == 2
    assert out["by_executor"]["zcode-worker"]["rc"] == {"0": 1, "1": 1}
    assert out["by_executor"]["zcode-worker"]["result"] == {"completed": 1, "null": 1}
    assert out["by_executor"]["zcode-worker"]["avg_duration_s"] == 2.0
    assert out["by_executor"]["muse-ask"]["runs"] == 1
    assert out["by_executor"]["muse-ask"]["result"] == {"partial": 1}
    assert out["by_executor"]["muse-ask"]["avg_duration_s"] == 2.0
    assert out["by_executor"]["claude-ask"]["runs"] == 4
    assert out["by_executor"]["claude-ask"]["result"] == {"blocked_question": 4}
    assert out["tasks_relaunched"] == 1  # blocked_question nao conta, como no orcamento
    assert out["tasks_over_budget"] == 0


def test_stats_sem_log_retorna_zeros(fake_env):
    r = run_cli(fake_env, ["--stats"])
    assert r.returncode == 0
    out = json.loads(r.stdout.decode("utf-8"))
    assert out == {"runs": 0, "redo": 0, "by_executor": {},
                   "tasks_relaunched": 0, "tasks_over_budget": 0}
