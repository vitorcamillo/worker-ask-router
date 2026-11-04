import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worker_router as wr  # noqa: E402

# Copia fiel da fixture de tests/test_worker_router.py (sem importar o modulo de teste).
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


def test_sucesso_write_grava_uma_linha_sem_conteudo(fake_env):
    prompt = "tarefa-secreta-xyz-123"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", prompt])
    assert r.returncode == 0
    recs = read_runs(fake_env)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["schema"] == 1
    assert len(rec["run_id"]) == 32
    assert rec["orchestrator"] == "codex"
    assert rec["mode"] == "write"
    assert rec["rc"] == 0
    assert rec["executor"] == "zcode-worker"
    assert rec["allowed_files"] == ["a.txt"]
    assert rec["spec_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert rec["prompt_bytes"] == len(prompt.encode())
    assert rec["output_bytes"] == len(b"READY:zcode-worker")
    assert isinstance(rec["duration_s"], (int, float))
    raw = runs_path(fake_env).read_bytes()
    assert prompt.encode() not in raw
    assert b"READY:" not in raw
    assert b"private-worker-stderr" not in raw


def test_fallback_rc75_registra_fallback_e_dois_starts(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_RC_ZCODE_WORKER": "75"})
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    attempts = rec["attempts"]
    assert any(a.get("event") == "fallback" and a.get("rc") == 75 for a in attempts)
    starts = {a.get("executor") for a in attempts if a.get("event") == "start"}
    assert {"zcode-worker", "muse-ask"} <= starts


def test_todos_75_grava_rc_75(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_RC": "75"})
    assert r.returncode == 75
    rec = read_runs(fake_env)[0]
    assert rec["rc"] == 75


def test_status_e_erro_de_uso_nao_criam_linha(fake_env):
    r = subprocess.run([sys.executable, str(ROOT / "worker_router.py"), "--status"],
                       capture_output=True, env=os.environ, timeout=30)
    assert r.returncode == 0
    assert not runs_path(fake_env).exists()
    r = subprocess.run([sys.executable, str(ROOT / "worker_router.py"),
                        "--orchestrator", "codex", "--mode", "plan", "task"],
                       capture_output=True, env=os.environ, timeout=30)
    assert r.returncode == 2
    assert not runs_path(fake_env).exists()


def test_opt_out_por_env_nao_cria_arquivo(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"WORKER_ASK_RUNS_LOG": "0"})
    assert r.returncode == 0
    assert not runs_path(fake_env).exists()


def test_symlink_em_runs_jsonl_mantem_rc_e_stdout(fake_env):
    sd = fake_env["state"] / "worker-ask"
    sd.mkdir(parents=True, exist_ok=True)
    alvo = fake_env["tmp"] / "alvo.txt"
    alvo.write_text("intocado")
    (sd / "runs.jsonl").symlink_to(alvo)
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0
    assert r.stdout == b"READY:zcode-worker"
    assert alvo.read_text() == "intocado"


def test_arquivo_nasce_com_permissao_0600(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0
    assert stat.S_IMODE(runs_path(fake_env).stat().st_mode) == 0o600


def test_rotacao_acima_do_limite(fake_env):
    sd = fake_env["state"] / "worker-ask"
    sd.mkdir(parents=True, exist_ok=True)
    antigo = b"x" * (wr.RUNS_LOG_MAX_BYTES + 1)
    runs_path(fake_env).write_bytes(antigo)
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0
    backup = sd / "runs.jsonl.1"
    assert backup.exists()
    assert backup.read_bytes() == antigo
    lines = runs_path(fake_env).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["schema"] == 1


def test_changed_files_conta_arquivo_alterado(fake_env):
    (fake_env["work"] / "a.txt").write_text("antes")
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_MODE": "writefile", "FAKE_WRITE_REL": "a.txt",
                 "FAKE_WRITE_CONTENT": "depois"})
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["changed_files"] == 1


def test_runs_report_json_com_retrabalho_e_sem_mudanca(fake_env, tmp_path):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        {"schema": 1, "run_id": "a" * 32, "ts": now, "orchestrator": "codex",
         "mode": "write", "cwd": "/tmp/x", "spec_sha256": "abc123",
         "prompt_bytes": 3, "allowed_files": ["a.txt"], "attempts": [],
         "rc": 0, "duration_s": 1.0, "executor": "zcode-worker",
         "output_bytes": 5, "changed_files": 0},
        {"schema": 1, "run_id": "b" * 32, "ts": now, "orchestrator": "codex",
         "mode": "write", "cwd": "/tmp/x", "spec_sha256": "abc123",
         "prompt_bytes": 3, "allowed_files": ["a.txt"], "attempts": [],
         "rc": 0, "duration_s": 2.0, "executor": "zcode-worker",
         "output_bytes": 5, "changed_files": 1},
    ]
    log = tmp_path / "runs.jsonl"
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "runs_report.py"),
                        "--file", str(log), "--json"],
                       capture_output=True, env=os.environ, timeout=30)
    assert r.returncode == 0
    report = json.loads(r.stdout)
    assert report["total"] == 2
    assert report["rework_same_spec"]["groups"] == 1
    assert report["rework_same_spec"]["extra_runs"] == 1
    assert report["rework_write_24h"]["groups"] == 1
    assert report["write_no_change"] == 1


def test_rotacao_concorrente_preserva_backup(fake_env, monkeypatch):
    import fcntl as _fcntl
    import time as _time

    sd = fake_env["state"] / "worker-ask"
    sd.mkdir(parents=True, exist_ok=True)
    rp = runs_path(fake_env)
    bp = sd / "runs.jsonl.1"
    grande = b"x" * (wr.RUNS_LOG_MAX_BYTES + 1)
    rp.write_bytes(grande)
    antigo_run, antigo_t0, antigo_escrito = wr._RUN, wr._RUN_T0, wr._RUN_WRITTEN
    try:
        wr._RUN = {"schema": 1, "run_id": "c" * 32, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "orchestrator": "codex", "mode": "write", "cwd": str(fake_env["work"]),
                   "allowed_files": ["a.txt"], "attempts": []}
        wr._RUN_T0 = _time.monotonic()
        wr._RUN_WRITTEN = False
        orig_flock = _fcntl.flock
        estado = {"girou": False}

        def _flock_concorrente(fd, op):
            if op & _fcntl.LOCK_EX and not estado["girou"]:
                estado["girou"] = True
                os.replace(str(rp), str(bp))
                rp.write_bytes(b"novo\n")
            return orig_flock(fd, op)

        monkeypatch.setattr(_fcntl, "flock", _flock_concorrente)
        wr._write_run(0)
    finally:
        wr._RUN, wr._RUN_T0, wr._RUN_WRITTEN = antigo_run, antigo_t0, antigo_escrito
    assert estado["girou"]
    assert bp.read_bytes() == grande
    linhas = rp.read_text(encoding="utf-8").splitlines()
    assert linhas[0] == "novo"
    assert json.loads(linhas[-1])["rc"] == 0


def test_log_nao_recebe_aviso_de_runs_symlink(fake_env, tmp_path):
    sd = fake_env["state"] / "worker-ask"
    sd.mkdir(parents=True, exist_ok=True)
    alvo = fake_env["tmp"] / "alvo.txt"
    alvo.write_text("intocado")
    (sd / "runs.jsonl").symlink_to(alvo)
    logf = tmp_path / "saida.log"
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt",
                           "--log", str(logf), "hi"])
    assert r.returncode == 0
    assert r.stdout == b"READY:zcode-worker"
    assert b"runs.jsonl" in r.stderr
    assert "runs.jsonl" not in logf.read_text(encoding="utf-8")
    assert alvo.read_text() == "intocado"


def test_runs_jsonl_fifo_sem_leitor_nao_bloqueia(fake_env):
    sd = fake_env["state"] / "worker-ask"
    sd.mkdir(parents=True, exist_ok=True)
    os.mkfifo(runs_path(fake_env))
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"])
    assert r.returncode == 0
    assert r.stdout == b"READY:zcode-worker"
    assert b"runs.jsonl" in r.stderr


def test_falha_rc1_registra_metricas_da_tentativa(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_RC_ZCODE_WORKER": "1", "FAKE_STDOUT_ZCODE_WORKER": ZCODE_STDOUT})
    assert r.returncode == 1
    rec = read_runs(fake_env)[0]
    assert rec["rc"] == 1
    assert rec["executor"] == "zcode-worker"
    assert rec["output_bytes"] == len(ZCODE_STDOUT.encode())
    assert rec["usage"] == {"input": 10, "output": 2, "cacheRead": 5,
                            "cacheWrite": 1, "model": "m"}
    assert rec["changed_files"] == 0


def test_timeout_em_write_registra_metricas(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt",
                           "--timeout", "5", "--step-timeout", "1", "hi"],
                {"FAKE_MODE_ZCODE_WORKER": "sleep", "FAKE_SLEEP": "30"})
    assert r.returncode == 124
    rec = read_runs(fake_env)[0]
    assert rec["rc"] == 124
    assert rec["executor"] == "zcode-worker"
    assert rec["output_bytes"] == 0
    assert "usage" not in rec
    assert rec["changed_files"] == 0


def test_todos_rc75_registra_metricas_da_ultima_tentativa(fake_env):
    env = {("FAKE_RC_" + n): "75" for n in ("ZCODE_WORKER", "MUSE_ASK",
                                            "OPENCODE_ASK", "CODEX_ASK", "CLAUDE_ASK")}
    env["FAKE_STDOUT_CLAUDE_ASK"] = ("resposta util\n--- claude-ask: "
                                     "tokens=in:7/out:3 custo=$0.02 ---")
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"], env)
    assert r.returncode == 75
    rec = read_runs(fake_env)[0]
    assert rec["rc"] == 75
    assert rec["executor"] == "claude-ask"
    assert rec["usage"] == {"input": 7, "output": 3, "cost_usd": 0.02}
    assert rec["changed_files"] == 0


def test_sucesso_apos_rc75_nao_herdou_usage_da_falha(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_RC_ZCODE_WORKER": "75", "FAKE_STDOUT_ZCODE_WORKER": ZCODE_STDOUT})
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["executor"] == "muse-ask"
    assert "usage" not in rec
    assert rec["changed_files"] == 0


def test_sigterm_apos_fallback_nao_herda_metricas_da_tentativa_anterior(fake_env, tmp_path):
    env = dict(os.environ)
    env.update({
        "FAKE_RC_ZCODE_WORKER": "75",
        "FAKE_STDOUT_ZCODE_WORKER": ZCODE_STDOUT,
        "FAKE_MODE_MUSE_ASK": "sleep",
        "FAKE_SLEEP": "30",
        "FAKE_LOG": str(tmp_path / "fake.log"),
    })
    cmd = [sys.executable, str(ROOT / "worker_router.py"), "--cwd", str(fake_env["work"]),
           "--orchestrator", "codex", "--mode", "write", "--allowed-file", "a.txt", "hi"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)

    def _log_tem_muse():
        if not (tmp_path / "fake.log").exists():
            return False
        for linha in (tmp_path / "fake.log").read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(linha).get("name") == "muse-ask":
                    return True
            except json.JSONDecodeError:
                continue
        return False

    try:
        prazo = time.monotonic() + 20
        while not _log_tem_muse():
            if time.monotonic() > prazo:
                proc.kill()
                proc.wait(timeout=10)
                pytest.fail("fake muse-ask nao gravou registro no FAKE_LOG em 20s")
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == 143
    rec = read_runs(fake_env)[0]
    assert rec["rc"] == 143
    assert rec["executor"] == "muse-ask"
    assert "usage" not in rec
    assert "output_bytes" not in rec


def test_runs_report_retrabalho_24h_por_diferenca_adjacente(tmp_path):
    from datetime import timedelta

    base = datetime.now(timezone.utc) - timedelta(days=5)
    momentos = [base, base + timedelta(hours=1),
                base + timedelta(days=3), base + timedelta(days=3, hours=1)]
    linhas = []
    for i, momento in enumerate(momentos):
        linhas.append({"schema": 1, "run_id": "%032x" % (i + 1),
                      "ts": momento.isoformat(timespec="seconds"),
                      "orchestrator": "codex", "mode": "write", "cwd": "/tmp/x",
                      "allowed_files": ["a.txt"], "attempts": [],
                      "rc": 0, "duration_s": 1.0, "executor": "zcode-worker",
                      "output_bytes": 5, "changed_files": 1})
    log = tmp_path / "runs.jsonl"
    log.write_text("\n".join(json.dumps(linha) for linha in linhas) + "\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "runs_report.py"),
                        "--file", str(log), "--json"],
                       capture_output=True, env=os.environ, timeout=30)
    assert r.returncode == 0
    relatorio = json.loads(r.stdout)
    assert relatorio["rework_write_24h"] == {"groups": 1, "extra_runs": 2}


MUSE_STDOUT = ("resposta util\n--- muse-ask: modo=write modelo=muse-spark-1.3-contributor "
               "esforco=high sessao=ABC123 tokens=in:1869683/out:24444 "
               "cota=janela 0%/semana 53% ---")

OPENCODE_STDOUT = ("resposta util\n--- opencode-ask: modo=write modelo=auto classe=C "
                   "esforco=high sessao=S tokens=in:100/out:50 custo=$0.0123 ---")

ZCODE_STDOUT = ("resposta util\n--- zcode-worker: input=10 output=2 cacheRead=5 "
                "cacheWrite=1 model=m ---")

CLAUDE_STDOUT = "resposta util\n--- claude-ask: modo=write ---"


def test_runs_log_usage_muse_tokens_e_modelo(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_RC_ZCODE_WORKER": "75", "FAKE_STDOUT_MUSE_ASK": MUSE_STDOUT})
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["executor"] == "muse-ask"
    assert rec["usage"] == {"input": 1869683, "output": 24444,
                            "model": "muse-spark-1.3-contributor"}
    assert "executor_start" not in {a.get("event") for a in rec["attempts"]}


def test_runs_log_usage_opencode_custo(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_RC_ZCODE_WORKER": "75", "FAKE_RC_MUSE_ASK": "75",
                 "FAKE_STDOUT_OPENCODE_ASK": OPENCODE_STDOUT})
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["executor"] == "opencode-ask"
    assert rec["usage"] == {"input": 100, "output": 50,
                            "model": "auto", "cost_usd": 0.0123}


def test_runs_log_usage_zcode_inteiros(fake_env):
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"],
                {"FAKE_STDOUT_ZCODE_WORKER": ZCODE_STDOUT})
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["usage"] == {"input": 10, "output": 2, "cacheRead": 5,
                            "cacheWrite": 1, "model": "m"}
    assert all(isinstance(rec["usage"][k], int)
               for k in ("input", "output", "cacheRead", "cacheWrite"))


def test_runs_log_sem_tokens_sem_usage(fake_env):
    env = {("FAKE_RC_" + n): "75" for n in ("ZCODE_WORKER", "MUSE_ASK",
                                            "OPENCODE_ASK", "CODEX_ASK")}
    env["FAKE_STDOUT_CLAUDE_ASK"] = CLAUDE_STDOUT
    r = run_cli(fake_env, ["--mode", "write", "--allowed-file", "a.txt", "hi"], env)
    assert r.returncode == 0
    rec = read_runs(fake_env)[0]
    assert rec["executor"] == "claude-ask"
    assert "usage" not in rec


def test_event_executor_start_nao_duplica_attempts(monkeypatch):
    antigo = (wr._RUN, wr._RUN_T0, wr._EVENT_FD, wr._EVENT_FD_READY)
    try:
        wr._RUN = {"attempts": []}
        wr._RUN_T0 = time.monotonic()
        monkeypatch.delenv("ASK_PRESENTATION_FD", raising=False)
        wr._EVENT_FD = None
        wr._EVENT_FD_READY = False
        wr._event("executor_start", None, executor="muse-ask", mode="write")
        assert wr._RUN["attempts"] == []
        leitura, escrita = os.pipe()
        try:
            ino = os.fstat(escrita).st_ino
            monkeypatch.setenv("ASK_PRESENTATION_FD", "%d:%d" % (escrita, ino))
            wr._EVENT_FD = None
            wr._EVENT_FD_READY = False
            wr._event("executor_start", None, executor="muse-ask", mode="write")
            assert wr._RUN["attempts"] == []
            payload = json.loads(os.read(leitura, 65536).decode("utf-8"))
            assert payload["event"] == "executor_start"
        finally:
            os.close(leitura)
            os.close(escrita)
    finally:
        wr._RUN, wr._RUN_T0, wr._EVENT_FD, wr._EVENT_FD_READY = antigo


def test_runs_report_json_com_custo_e_runs_with_usage(tmp_path):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        {"schema": 1, "run_id": "a" * 32, "ts": now, "orchestrator": "codex",
         "mode": "write", "cwd": "/tmp/x", "allowed_files": ["a.txt"],
         "attempts": [], "rc": 0, "duration_s": 1.0, "executor": "zcode-worker",
         "usage": {"input": 10, "output": 2, "cost_usd": 0.5}},
        {"schema": 1, "run_id": "b" * 32, "ts": now, "orchestrator": "codex",
         "mode": "write", "cwd": "/tmp/x", "allowed_files": ["a.txt"],
         "attempts": [], "rc": 0, "duration_s": 3.0, "executor": "zcode-worker"},
        {"schema": 1, "run_id": "c" * 32, "ts": now, "orchestrator": "codex",
         "mode": "write", "cwd": "/tmp/x", "allowed_files": ["a.txt"],
         "attempts": [], "rc": 0, "duration_s": 2.0, "executor": "muse-ask",
         "usage": {"input": 5, "cost_usd": 0.01}},
    ]
    log = tmp_path / "runs.jsonl"
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "runs_report.py"),
                        "--file", str(log), "--json"],
                       capture_output=True, env=os.environ, timeout=30)
    assert r.returncode == 0
    report = json.loads(r.stdout)
    by = report["by_executor"]
    assert by["zcode-worker"]["cost_usd"] == 0.5
    assert by["zcode-worker"]["runs_with_usage"] == 1
    assert by["zcode-worker"]["n"] == 2
    assert by["zcode-worker"]["input"] == 10
    assert by["muse-ask"]["cost_usd"] == 0.01
    assert by["muse-ask"]["runs_with_usage"] == 1
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "runs_report.py"),
                        "--file", str(log)],
                       capture_output=True, env=os.environ, timeout=30)
    assert r.returncode == 0
    assert "cost_usd=" in r.stdout.decode("utf-8")
