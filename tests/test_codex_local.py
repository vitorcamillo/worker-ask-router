"""Blackbox do worker local codex-local; sem modelo ou servico real."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "codex-local"
EXAMPLE = ROOT / "docs" / "codex-local-spec.example.json"


def _load_runtime():
    spec = importlib.util.spec_from_file_location(
        "codex_local_rt", str(BIN),
        loader=importlib.machinery.SourceFileLoader("codex_local_rt", str(BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RT = _load_runtime()


def valid_spec(**over):
    base = {
        "task_id": "t-001",
        "objective": "objetivo sintético",
        "context": ("trecho real de src/exemplo.py (linhas 1-20):\n"
                    "def soma(xs):\n    return sum(xs)\n"
                    "Nomes: soma. Alteração exata: trocar sum(xs) por "
                    "functools.reduce na linha 2, sem tocar outras linhas."),
        "allowed_files": ["src/exemplo.py"],
        "steps": ["ler src/exemplo.py no intervalo 1,80p"],
        "acceptance": ["arquivo contém ajuste"],
        "constraints": ["sem checks próprios"],
        "checks": [["python3", "-m", "pytest", "-q"]],
    }
    base.update(over)
    return base


def _open_fds():
    try:
        return set(os.listdir("/proc/self/fd"))
    except OSError:
        return set()


def _run_inprocess(argv, spec_text, env_over):
    saved_environ = dict(os.environ)
    saved_stdin = sys.stdin
    fds_before = _open_fds()
    if env_over:
        os.environ.update(env_over)
    if spec_text is not None:
        sys.stdin = io.TextIOWrapper(io.BytesIO(spec_text.encode()),
                                     encoding="utf-8")
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(out_buf), \
                contextlib.redirect_stderr(err_buf):
            try:
                rc = RT.main(list(argv))
            except SystemExit as exc:
                rc = exc.code
    finally:
        sys.stdin = saved_stdin
        os.environ.clear()
        os.environ.update(saved_environ)
        # O runtime so libera o lock singleton nos ramos de orcamento e
        # contexto insuficiente; fora deles o fd vaza ate o fim do
        # processo. Em subprocesso isso some no exit, mas in-process a
        # proxima chamada veria "ocupado": fechar os fds vazados libera.
        for fd in _open_fds() - fds_before:
            if fd in ("0", "1", "2"):
                continue
            try:
                os.close(int(fd))
            except (OSError, ValueError):
                pass
    return subprocess.CompletedProcess(args=list(argv), returncode=int(rc),
                                       stdout=out_buf.getvalue().encode(),
                                       stderr=err_buf.getvalue().encode())


def run_cli(args, cwd=None, spec_path=None, spec_text=None, env=None,
            timeout=60, base_url=None, inprocess=None):
    argv = list(args)
    if spec_path is not None and "--spec" not in argv:
        argv += ["--spec", str(spec_path)]
    if base_url is not None and "--base-url" not in argv:
        argv += ["--base-url", base_url]
    use_ip = (os.environ.get("CODEX_LOCAL_TEST_INPROCESS") == "1"
              if inprocess is None else bool(inprocess))
    if use_ip:
        # DEFAULT_MODEL/DEFAULT_BASE_URL sao capturados no import do modulo;
        # a fixture ajusta o ambiente depois, entao o teste passa os valores
        # atuais de forma explicita.
        if "--model" not in argv:
            argv += ["--model",
                     os.environ.get("CODEX_LOCAL_MODEL", "test-local")]
        if "--base-url" not in argv:
            argv += ["--base-url", os.environ.get(
                "CODEX_LOCAL_BASE_URL", "http://127.0.0.1:8080/v1")]
        return _run_inprocess(argv, spec_text, env)
    cmd = [sys.executable, str(BIN)] + argv
    final_env = dict(os.environ)
    if env:
        final_env.update(env)
    inp = spec_text.encode() if spec_text is not None else None
    return subprocess.run(cmd, input=inp, capture_output=True, env=final_env,
                          cwd=str(cwd) if cwd else None, timeout=timeout)


FAKE_CODEX = r'''
import json, os, sys, time
from pathlib import Path

exe = Path(sys.argv[0]).resolve()
ctl = exe.parent.parent / "ctl.json"
calls = exe.parent.parent / "calls.log"

def read_ctl():
    try:
        return json.loads(ctl.read_text())
    except Exception:
        return {}

def record(ev):
    try:
        with open(calls, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        pass

def record_config():
    try:
        ch = Path(os.environ.get("CODEX_HOME", "/nonexistent"))
        found = {}
        for p in list(ch.rglob("*.toml"))[:5]:
            try:
                found[str(p)] = p.read_text()[:4000]
            except Exception:
                pass
        for p in list(ch.rglob("model_catalog.json"))[:2]:
            try:
                found[str(p)] = p.read_text()[:4000]
            except Exception:
                pass
        record({"cmd": "exec-config", "files": found})
    except Exception:
        pass

args = sys.argv[1:]
ctl_doc = read_ctl()
if args[:2] == ["debug", "models"]:
    ctx = int(ctl_doc.get("catalog_ctx", 16384))
    model = ctl_doc.get("server_model", "test-local")
    doc = {"models": [
        {"slug": "gpt-5.5", "id": "gpt-5.5", "code_mode_only": False,
         "base_instructions": "x" * 4000, "model_messages": [{"role": "x"}],
         "meta": {"n_ctx": ctx}},
        {"slug": model, "id": model, "code_mode_only": False,
         "meta": {"n_ctx": ctx}},
    ]}
    sys.stdout.write(json.dumps(doc))
    sys.exit(0)
if args[:2] == ["debug", "prompt-input"]:
    record({"cmd": "prompt-input", "argv": args})
    # dry-run nunca chega ao exec: o config gerado e registrado aqui.
    record_config()
    rc = int(ctl_doc.get("prompt_input_rc", 0) or 0)
    if rc:
        sys.stdout.write(json.dumps([{"role": "system", "content": "x"}]))
        sys.exit(rc)
    if "prompt_input_raw" in ctl_doc:
        sys.stdout.write(str(ctl_doc["prompt_input_raw"]))
        sys.exit(0)
    content = "contexto global vazio"
    if ctl_doc.get("prompt_input_skills"):
        content += " <skills_instructions> skill-creator"
    if ctl_doc.get("prompt_input_agents"):
        content += " AGENTS project_doc <project_doc>algo</project_doc>"
    sys.stdout.write(json.dumps([{"role": "system", "content": content},
                                 {"role": "user", "content": "ping"}]))
    sys.exit(0)
if args and args[0] == "exec":
    out = None
    cwd = None
    for i, a in enumerate(args):
        if a == "-o" and i + 1 < len(args):
            out = args[i + 1]
        if a == "-C" and i + 1 < len(args):
            cwd = args[i + 1]
    envrec = {"CODEX_HOME": os.environ.get("CODEX_HOME", ""),
              "HOME": os.environ.get("HOME", ""),
              "WORKER_ASK_ACTIVE": os.environ.get("WORKER_ASK_ACTIVE", ""),
              "leak": [k for k in os.environ if "MARKER_SECRET" in k]}
    record({"cmd": "exec", "argv": args, "env": envrec, "cwd": cwd})
    record_config()
    if ctl_doc.get("exec_sleep"):
        time.sleep(float(ctl_doc["exec_sleep"]))
    if ctl_doc.get("exec_stderr_secret"):
        sys.stderr.write("SUPERSECRET-MARKER-12345\n")
        sys.stderr.flush()
    final = ctl_doc.get("exec_final", {"schema_version": 1, "task_id": "t-001",
        "execution_state": "completed", "changed_files": ["src/exemplo.py"],
        "acceptance": [{"criterion": "arquivo contém ajuste", "status": "met",
                        "evidence": ["e"]}],
        "checks": [],
        "risks": [], "validation_owner": "orchestrator", "approval": "pending"})
    if isinstance(final, dict) and ctl_doc.get("exec_task_id"):
        final = dict(final)
        final["task_id"] = ctl_doc["exec_task_id"]
    payload = ctl_doc.get("exec_raw", json.dumps(final))
    if out:
        Path(out).write_text(payload)
    sys.stdout.write("fake-exec-ok")
    sys.exit(0)
sys.stdout.write("fake-ok")
sys.exit(0)
'''


class _Handler(BaseHTTPRequestHandler):
    ctx = 16384
    model = "test-local"

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"data": [{"id": type(self).model,
                                  "meta": {"n_ctx": type(self).ctx}}]})
        elif self.path == "/props":
            self._send({"default_generation_settings":
                        {"n_ctx": type(self).ctx}})
        else:
            self._send({"error": "nf"}, 404)


@pytest.fixture
def ctl_env(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "codex"
    fake.write_text("#!" + sys.executable + "\n" + FAKE_CODEX + "\n")
    fake.chmod(0o755)
    (tmp_path / "ctl.json").write_text(json.dumps({}))
    (tmp_path / "calls.log").write_text("")
    (tmp_path / "emptybin").mkdir()
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_LOCAL_MODEL", "test-local")
    for k in ("CODEX_HOME", "CODEX_MANAGED_PACKAGE_ROOT", "OPENAI_API_KEY",
              "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MARKER_SECRET_PARENT", "parent-secret-value")
    return {"work": work, "tmp": tmp_path, "bin": bindir}


@pytest.fixture
def local_server(monkeypatch):
    _Handler.ctx = 16384
    _Handler.model = "test-local"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    thr = threading.Thread(target=srv.serve_forever, daemon=True)
    thr.start()
    monkeypatch.setenv("CODEX_LOCAL_BASE_URL",
                       f"http://127.0.0.1:{port}/v1")
    yield f"http://127.0.0.1:{port}/v1"
    srv.shutdown()


def set_ctl(tmp, **kw):
    p = Path(tmp) / "ctl.json"
    doc = json.loads(p.read_text())
    doc.update(kw)
    p.write_text(json.dumps(doc))


def reset_ctl(tmp):
    (Path(tmp) / "ctl.json").write_text(json.dumps({}))


def calls_of(tmp):
    lines = (Path(tmp) / "calls.log").read_text().splitlines()
    return [json.loads(x) for x in lines if x.strip()]


def write_spec(work, spec):
    p = work / "tarefa.json"
    p.write_text(json.dumps(spec, ensure_ascii=False))
    return p


@pytest.mark.acceptance
def test_given_template_when_no_cwd_then_prints_example():
    r = run_cli(["--template"])
    assert r.returncode == 0
    doc = json.loads(r.stdout.decode())
    assert doc["task_id"] and doc["allowed_files"] and doc["steps"]
    assert doc.get("context")


@pytest.mark.acceptance
def test_given_help_when_requested_then_ptbr_usage():
    r = run_cli(["--help"])
    assert r.returncode == 0
    out = r.stdout.decode() + r.stderr.decode()
    assert "--cwd" in out and "--template" in out


@pytest.mark.qa
def test_given_remote_url_when_preflight_then_rejects_before_model(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    for bad in ("http://192.0.2.1:8080/v1", "https://127.0.0.1:8080/v1",
                "http://127.0.0.1:8080/v1?tok=1", "http://127.0.0.1:8080/v1#frag",
                "http://user@127.0.0.1:8080/v1", "http://127.0.0.1:8080/api",
                "http://127.0.0.1/v1"):
        r = run_cli(["--cwd", str(work), "--spec", str(spec),
                     "--base-url", bad])
        assert r.returncode == 2, bad
    assert not [c for c in calls_of(ctl_env["tmp"]) if c.get("cmd") == "exec"]


@pytest.mark.qa
def test_given_context_above32k_when_validated_then_rejects(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    r = run_cli(["--cwd", str(work), "--spec", str(spec),
                 "--context-window", "32769"])
    assert r.returncode == 2
    assert not [c for c in calls_of(ctl_env["tmp"]) if c.get("cmd") == "exec"]


@pytest.mark.qa
def test_given_bad_specs_when_validated_then_rc2_before_model(ctl_env, local_server):
    work = ctl_env["work"]
    tmp = ctl_env["tmp"]
    no_ctx = valid_spec()
    del no_ctx["context"]
    bad_specs = [
        {k: v for k, v in valid_spec().items() if k != "task_id"},
        no_ctx,
        dict(valid_spec(), context="   "),
        dict(valid_spec(), context="C" * 6001),
        dict(valid_spec(), unknown_key="x"),
        dict(valid_spec(), task_id=123),
        dict(valid_spec(), task_id=True),
        dict(valid_spec(), objective="  "),
        dict(valid_spec(), steps=[]),
        dict(valid_spec(), steps=[""]),
        dict(valid_spec(), allowed_files=["/absoluto.py"]),
        dict(valid_spec(), allowed_files=["../fora.py"]),
        dict(valid_spec(), allowed_files=[".git/config"]),
        dict(valid_spec(), allowed_files=[".env"]),
        dict(valid_spec(), allowed_files=["auth.json"]),
        dict(valid_spec(), allowed_files=["a\x00b.py"]),
        dict(valid_spec(), allowed_files=[f"f{i}.py" for i in range(9)]),
        dict(valid_spec(), checks=[["python3", ""]]),
        dict(valid_spec(), checks=[["python3"] * 17]),
    ]
    for i, bad in enumerate(bad_specs):
        spec = write_spec(work, bad)
        r = run_cli(["--cwd", str(work), "--spec", str(spec)])
        assert r.returncode == 2, f"case {i}"
    big = dict(valid_spec(), objective="y" * 24001)
    spec = write_spec(work, big)
    assert run_cli(["--cwd", str(work), "--spec", str(spec)]).returncode == 2
    assert not [c for c in calls_of(tmp) if c.get("cmd") == "exec"]


@pytest.mark.qa
def test_given_spec_over_budget_when_validated_then_rejects_before_inference(
        ctl_env, local_server):
    # Servidor 16K => efetivo 16384 => orcamento min(12000, 8192) = 8192.
    # Spec ~10K: dentro dos caps por campo, acima do orcamento => rc2 sem exec.
    work = ctl_env["work"]
    tmp = ctl_env["tmp"]
    spec = valid_spec(objective="o" * 800, context="C" * 5000,
                      steps=["etapa %d " % i + "x" * 880 for i in range(4)],
                      acceptance=["a" * 500, "b" * 500])
    raw = json.dumps(spec, ensure_ascii=False).encode("utf-8")
    assert 8192 < len(raw) <= 12000
    specp = write_spec(work, spec)
    r = run_cli(["--cwd", str(work), "--spec", str(specp)])
    assert r.returncode == 2
    assert "orcamento" in (r.stdout.decode() + r.stderr.decode()).lower()
    assert not [c for c in calls_of(tmp) if c.get("cmd") == "exec"]


@pytest.mark.qa
def test_given_small_server_ctx_when_preflight_then_rc1(ctl_env, local_server):
    _Handler.ctx = 4096
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    r = run_cli(["--cwd", str(work), "--spec", str(spec)])
    assert r.returncode == 1
    assert not [c for c in calls_of(ctl_env["tmp"]) if c.get("cmd") == "exec"]


@pytest.mark.qa
def test_given_prompt_input_unhealthy_when_preflight_then_failclosed(
        ctl_env, local_server):
    work = ctl_env["work"]
    tmp = ctl_env["tmp"]
    spec = write_spec(work, valid_spec())
    cases = [
        {"prompt_input_rc": 3},
        {"prompt_input_raw": "nao-json{{{,}lima"},
        {"prompt_input_skills": True},
        {"prompt_input_agents": True},
    ]
    for i, ctl in enumerate(cases):
        reset_ctl(tmp)
        set_ctl(tmp, **ctl)
        r = run_cli(["--cwd", str(work), "--spec", str(spec)])
        assert r.returncode == 1, f"case {i}"
        assert not [c for c in calls_of(tmp) if c.get("cmd") == "exec"], \
            f"case {i}"
    # codex ausente no PATH tambem aborta fechado antes de qualquer exec.
    reset_ctl(tmp)
    r = run_cli(["--cwd", str(work), "--spec", str(spec)],
                env={"PATH": str(tmp / "emptybin")})
    assert r.returncode == 1
    assert not [c for c in calls_of(tmp) if c.get("cmd") == "exec"]


@pytest.mark.qa
def test_given_symlink_allowed_when_validated_then_rejects(ctl_env, local_server):
    work = ctl_env["work"]
    (work / "real.py").write_text("x")
    link = work / "elo.py"
    try:
        link.symlink_to(work / "real.py")
    except OSError:
        pytest.skip("symlink indisponível")
    spec = write_spec(work, valid_spec(allowed_files=["elo.py"]))
    r = run_cli(["--cwd", str(work), "--spec", str(spec)])
    assert r.returncode == 2


@pytest.mark.e2e
def test_given_checks_declared_when_executed_then_not_run_by_wrapper(ctl_env, local_server):
    work = ctl_env["work"]
    sentinel = work / "check-rodou.txt"
    spec = write_spec(work, valid_spec(
        checks=[["python3", "-c",
                 f"open({str(sentinel)!r}, 'w').write('x')"]]))
    set_ctl(ctl_env["tmp"])
    r = run_cli(["--cwd", str(work), "--spec", str(spec)])
    assert r.returncode == 0
    assert not sentinel.exists()


@pytest.mark.e2e
def test_given_dry_run_when_server16k_then_effective_min_without_inference(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    r = run_cli(["--cwd", str(work), "--spec", str(spec),
                 "--context-window", "32768", "--dry-run"])
    assert r.returncode == 0
    doc = json.loads(r.stdout.decode())
    assert doc["requested_context"] == 32768
    assert doc["effective_context"] == 16384
    assert doc["model"] == "test-local"
    assert "prompt" not in json.dumps(doc).lower() or len(r.stdout) < 20000
    assert not [c for c in calls_of(ctl_env["tmp"]) if c.get("cmd") == "exec"]


@pytest.mark.e2e
def test_given_parent_secrets_when_child_runs_then_env_isolated(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    set_ctl(ctl_env["tmp"])
    r = run_cli(["--cwd", str(work), "--spec", str(spec)])
    assert r.returncode == 0
    execs = [c for c in calls_of(ctl_env["tmp"]) if c.get("cmd") == "exec"]
    assert execs
    env = execs[0]["env"]
    assert env["WORKER_ASK_ACTIVE"] == "1"
    assert env["leak"] == []
    assert "parent-secret-value" not in json.dumps(execs)
    state = Path(os.environ["XDG_STATE_HOME"])
    assert env["CODEX_HOME"].startswith(str(state))
    assert env["HOME"].startswith(str(state))


@pytest.mark.e2e
def test_given_run_when_configured_then_catalog_and_config_are_minimal(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    set_ctl(ctl_env["tmp"])
    r = run_cli(["--cwd", str(work), "--spec", str(spec), "--dry-run"])
    assert r.returncode == 0
    execs = [c for c in calls_of(ctl_env["tmp"]) if c.get("cmd") == "exec-config"]
    assert execs
    blob = json.dumps(execs[0].get("files", {}))
    assert "project_doc_max_bytes" in blob and "0" in blob
    assert "shell_command" in blob
    assert "apply_patch" in blob and "null" in blob.lower()
    assert "tool_mode" in blob and "null" in blob.lower()
    doc = json.loads(r.stdout.decode())
    assert doc.get("skills_count") == 0
    assert doc.get("global_context_loaded") is False


@pytest.mark.acceptance
def test_given_valid_final_when_exec_then_returns_zero(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    set_ctl(ctl_env["tmp"])
    r = run_cli(["--cwd", str(work), "--spec", str(spec)])
    assert r.returncode == 0
    out = json.loads(r.stdout.decode())
    assert out["task_id"] == "t-001"
    assert out["changed_files"] == ["src/exemplo.py"]
    assert out["validation_owner"] == "orchestrator"


@pytest.mark.qa
def test_given_bad_finals_when_validated_then_no_success(ctl_env, local_server):
    work = ctl_env["work"]
    tmp = ctl_env["tmp"]
    cases = [
        {"exec_raw": "nao-json", "name": "invalido"},
        {"exec_final": {"schema_version": 1, "task_id": "t-001",
                        "execution_state": "partial", "changed_files": [],
                        "acceptance": [], "checks": [], "risks": [],
                        "validation_owner": "orchestrator",
                        "approval": "pending"}, "name": "partial"},
        {"exec_final": {"schema_version": 1, "task_id": "t-001",
                        "execution_state": "blocked", "changed_files": [],
                        "acceptance": [], "checks": [], "risks": [],
                        "validation_owner": "orchestrator",
                        "approval": "pending"}, "name": "blocked"},
        {"exec_final": {"schema_version": 1, "task_id": "t-001",
                        "execution_state": "completed",
                        "changed_files": ["fora/escopo.py"],
                        "acceptance": [], "checks": [], "risks": [],
                        "validation_owner": "orchestrator",
                        "approval": "pending"}, "name": "fora-escopo"},
    ]
    for case in cases:
        reset_ctl(tmp)
        set_ctl(tmp, **{k: v for k, v in case.items() if k != "name"})
        spec = write_spec(work, valid_spec())
        r = run_cli(["--cwd", str(work), "--spec", str(spec)])
        assert r.returncode != 0, case["name"]


@pytest.mark.qa
def test_given_hang_when_timeout_then_process_killed(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    set_ctl(ctl_env["tmp"], exec_sleep=30)
    r = run_cli(["--cwd", str(work), "--spec", str(spec), "--timeout", "1"],
                inprocess=False)
    assert r.returncode == 124
    # Timeout libera o lock: um dry-run subsequente passa.
    r2 = run_cli(["--cwd", str(work), "--spec", str(spec), "--dry-run"],
                 inprocess=False)
    assert r2.returncode == 0


@pytest.mark.qa
def test_given_singleton_when_second_starts_then_busy_before_second_exec(
        ctl_env, local_server):
    work = ctl_env["work"]
    tmp = ctl_env["tmp"]
    work2 = tmp / "work2"
    work2.mkdir()
    spec1 = write_spec(work, valid_spec())
    spec2 = write_spec(work2, valid_spec(task_id="t-002",
                                        objective="outra fatia pequena"))
    reset_ctl(tmp)
    set_ctl(tmp, exec_sleep=3)
    cmd1 = [sys.executable, str(BIN), "--cwd", str(work),
            "--spec", str(spec1)]
    p1 = subprocess.Popen(cmd1, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=dict(os.environ))
    try:
        deadline = time.monotonic() + 5
        started = False
        while time.monotonic() < deadline:
            if any(c.get("cmd") == "exec" for c in calls_of(tmp)):
                started = True
                break
            if p1.poll() is not None:
                break
            time.sleep(0.05)
        assert started, "CLI1 nao iniciou o exec a tempo"
        r2 = run_cli(["--cwd", str(work2), "--spec", str(spec2),
                      "--model", "other-local"], inprocess=False)
        assert r2.returncode == 1
        assert "ocupado" in (r2.stdout.decode() + r2.stderr.decode()).lower()
        execs = [c for c in calls_of(tmp) if c.get("cmd") == "exec"]
        assert len(execs) == 1
        lock = Path(os.environ["XDG_STATE_HOME"]) / "codex-local" / "worker.lock"
        assert lock.exists()
    finally:
        p1.send_signal(signal.SIGTERM)
        try:
            p1.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p1.kill()
            p1.communicate(timeout=10)
    assert p1.returncode in (143, 130)
    # Lock liberado na saida, mas o arquivo permanece (sem split lock).
    assert lock.exists()
    r3 = run_cli(["--cwd", str(work), "--spec", str(spec1), "--dry-run"],
                 inprocess=False)
    assert r3.returncode == 0


@pytest.mark.qa
def test_given_secret_stderr_when_exec_then_not_leaked(ctl_env, local_server):
    work = ctl_env["work"]
    spec = write_spec(work, valid_spec())
    set_ctl(ctl_env["tmp"], exec_stderr_secret=True)
    r = run_cli(["--cwd", str(work), "--spec", str(spec)])
    assert r.returncode == 0
    combined = r.stdout.decode(errors="replace") + r.stderr.decode(
        errors="replace")
    assert "SUPERSECRET-MARKER-12345" not in combined


@pytest.mark.e2e
def test_given_project_config_when_preflight_then_not_loaded(ctl_env, local_server):
    work = ctl_env["work"]
    (work / ".codex").mkdir(exist_ok=True)
    (work / ".codex" / "config").write_text("MARKER-PROJETO-SECRETO")
    (work / "AGENTS").write_text("MARKER-AGENTS-SECRETO")
    spec = write_spec(work, valid_spec())
    set_ctl(ctl_env["tmp"])
    r = run_cli(["--cwd", str(work), "--spec", str(spec), "--dry-run"])
    assert r.returncode == 0
    assert "MARKER-PROJETO-SECRETO" not in r.stdout.decode()
    assert "MARKER-AGENTS-SECRETO" not in r.stdout.decode()
    prompts = [c for c in calls_of(ctl_env["tmp"]) if c.get("cmd") == "prompt-input"]
    assert prompts


def test_given_base_url_when_validated_then_normalizes():
    assert RT.validate_base_url("http://127.0.0.1:8080/v1") == (
        "http://127.0.0.1:8080/v1", 8080)
    with pytest.raises(ValueError):
        RT.validate_base_url("http://192.0.2.1:8080/v1")
    with pytest.raises(ValueError):
        RT.validate_base_url("http://127.0.0.1:8080/v1?q=1")


def test_given_spec_dict_when_validated_then_context_required(tmp_path):
    base = valid_spec()
    ok = RT.validate_spec(base, str(tmp_path), "write")
    assert ok["context"] == base["context"]
    no_ctx = dict(base)
    del no_ctx["context"]
    with pytest.raises(ValueError):
        RT.validate_spec(no_ctx, str(tmp_path), "write")
    with pytest.raises(ValueError):
        RT.validate_spec(dict(base, context="   "), str(tmp_path), "write")
    with pytest.raises(ValueError):
        RT.validate_spec(dict(base, context="C" * 6001),
                         str(tmp_path), "write")
    assert RT.validate_spec(json.dumps(base), str(tmp_path),
                            "write")["task_id"] == "t-001"
    with pytest.raises(ValueError):
        RT.validate_spec(dict(base, extra="x"), str(tmp_path), "write")


def test_given_spec_when_prompt_built_then_compact(tmp_path):
    spec = RT.validate_spec(valid_spec(), str(tmp_path), "write")
    system, user = RT.build_prompt(spec, "write")
    assert len(system) <= 6000
    assert "t-001" in user
    assert json.dumps(spec["context"], ensure_ascii=False) in user


def test_given_template_when_built_inprocess_then_example_valid():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = RT.main(["--template"])
    assert rc == 0
    doc = json.loads(out.getvalue())
    assert doc.get("context")
    assert doc.get("allowed_files")
    assert any("Substituir somente" in s for s in doc.get("steps", []))


@pytest.mark.parametrize("url", [None, "", "http://localhost:0/v1", "http://localhost:99999/v1",
    "http://localhost:abc/v1", "http://localhost:8080/v1\n", "http://[invalid/v1",
    "http://localhost:8080/v1;params", "http://localhost:8080/v1#x"])
def test_url_boundary_rejections(url):
    with pytest.raises(ValueError):
        RT.validate_base_url(url)


@pytest.mark.parametrize("raw", [123, [], b'\xff', '{bad', b'{bad',
    '"plain"', "x" * 12001, b'x' * 12001])
def test_spec_encoding_and_type_rejections(raw, tmp_path):
    with pytest.raises(ValueError):
        RT.validate_spec(raw, tmp_path, "write")


@pytest.mark.parametrize("changes", [
    {"task_id": "x" * 101}, {"objective": "x" * 2001},
    {"steps": "not-list"}, {"steps": [True]}, {"steps": ["x" * 1501]},
    {"acceptance": []}, {"acceptance": [42]}, {"acceptance": ["x" * 1001]},
    {"allowed_files": "file.py"}, {"allowed_files": []}, {"allowed_files": [None]},
    {"allowed_files": [""]}, {"allowed_files": [" "]}, {"allowed_files": ["C:/file"]},
    {"allowed_files": ["a//b"]}, {"allowed_files": ["a/./b"]},
    {"allowed_files": ["a/credential.json"]}, {"allowed_files": [".ENV.local"]},
    {"allowed_files": ["private.key"]}, {"allowed_files": ["id_rsa"]},
    {"allowed_files": ["a.py", "a.py"]},
    {"constraints": "wrong"}, {"constraints": ["x"] * 9},
    {"constraints": [True]}, {"constraints": [""]}, {"constraints": ["x" * 501]},
    {"checks": "wrong"}, {"checks": [["a"]] * 9}, {"checks": [True]},
    {"checks": [[]]}, {"checks": [[42]]}, {"checks": [["x" * 501]]},
])
def test_spec_field_boundaries(changes, tmp_path):
    with pytest.raises(ValueError):
        RT.validate_spec(valid_spec(**changes), tmp_path, "write")


def test_plan_can_have_no_files_and_byte_spec(tmp_path):
    spec = valid_spec(allowed_files=[])
    assert RT.validate_spec(json.dumps(spec).encode(), tmp_path, "plan")["allowed_files"] == []


def _final(**changes):
    value = {"schema_version": 1, "task_id": "t-001", "execution_state": "completed",
             "changed_files": ["src/exemplo.py"], "acceptance": [], "checks": [],
             "risks": [], "validation_owner": "orchestrator", "approval": "pending"}
    value.update(changes)
    return value


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"task_id": "other"},
    {"execution_state": "approved"}, {"changed_files": "a"}, {"changed_files": [False]},
    {"validation_owner": "worker"}, {"approval": "approved"}, {"risks": {}},
    {"risks": [42]}, {"acceptance": {}}, {"checks": {}},
    {"acceptance": ["met"]}, {"acceptance": [{"criterion": "arquivo contém ajuste", "status": "bad", "evidence": []}]},
    {"acceptance": [{"criterion": "arquivo contém ajuste", "status": "met", "evidence": []}]},
    {"checks": ["passed"]},
    {"checks": [{"command": "cat a", "status": "passed", "exit_code": 0, "evidence": ["a"]}]},
    {"checks": [{"command": ["cat", "a"], "status": "not_run", "exit_code": 0, "evidence": []}]},
    {"checks": [{"command": ["cat", "a"], "status": "passed", "exit_code": 1, "evidence": ["a"]}]},
])
def test_final_contract_rejects_false_approval_and_malformed_checks(changes):
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(_final(**changes)), valid_spec())


@pytest.mark.parametrize("text", ["", "plain text", '[]', '{}',
    '```json\n{}\n```\n```json\n{}\n```'])
def test_final_requires_one_contract_object(text):
    with pytest.raises(ValueError):
        RT.validate_final_output(text, valid_spec())


def test_fenced_contract_prose_is_data_and_check_argv_is_preserved():
    result = _final(
        checks=[],
        acceptance=[{"criterion": "arquivo contém ajuste", "status": "met",
                     "evidence": ["read a"]}])
    text = 'Resultado:\n```json\n' + json.dumps(result) + '\n```\nFim.'
    assert RT.validate_final_output(text, valid_spec()) == result


def test_final_rejects_nonempty_checks_reserved_to_orchestrator():
    nonempty = _final(
        checks=[{"command": ["python3", "-m", "pytest", "-q"],
                 "status": "passed", "exit_code": 0,
                 "evidence": ["suite ok"]}],
        acceptance=[{"criterion": "arquivo contém ajuste", "status": "met",
                     "evidence": ["e"]}])
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(nonempty), valid_spec())
    notrun = _final(
        checks=[{"command": ["python3", "-m", "pytest", "-q"],
                 "status": "not_run", "exit_code": None, "evidence": []}])
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(notrun), valid_spec())


def _acc(criterion="arquivo contém ajuste", status="met", evidence=None):
    return {"criterion": criterion, "status": status,
            "evidence": ["e"] if evidence is None else evidence}


def test_final_completed_empty_acceptance_is_rejected():
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(_final()), valid_spec())


@pytest.mark.parametrize("status", ["blocked", "pending"])
def test_final_completed_with_unmet_status_is_rejected(status):
    doc = _final(acceptance=[_acc(status=status, evidence=[])])
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(doc), valid_spec())


def test_final_completed_with_omitted_criterion_is_rejected():
    spec = valid_spec(acceptance=["critério um", "critério dois"])
    doc = _final(acceptance=[_acc(criterion="critério um")])
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(doc), spec)


@pytest.mark.parametrize("state", ["completed", "partial", "blocked"])
def test_final_with_extra_or_duplicate_criterion_is_rejected(state):
    spec = valid_spec()
    extra = _final(execution_state=state,
                   acceptance=[_acc(), _acc(criterion="criterio extra")])
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(extra), spec)
    dup = _final(execution_state=state,
                 acceptance=[_acc(), _acc()])
    with pytest.raises(ValueError):
        RT.validate_final_output(json.dumps(dup), spec)


@pytest.mark.parametrize("state,status", [
    ("partial", "pending"), ("partial", "blocked"),
    ("blocked", "pending"), ("blocked", "blocked"),
    ("partial", "met"), ("blocked", "met"),
])
def test_final_non_completed_with_full_coverage_is_accepted(state, status):
    ev = [] if status != "met" else ["e"]
    doc = _final(execution_state=state,
                 acceptance=[_acc(status=status, evidence=ev)])
    assert RT.validate_final_output(
        json.dumps(doc), valid_spec())["execution_state"] == state


def test_final_completed_with_full_coverage_is_accepted():
    doc = _final(acceptance=[_acc()])
    assert RT.validate_final_output(
        json.dumps(doc), valid_spec())["execution_state"] == "completed"


@pytest.mark.parametrize("document,expected", [
    ({"data": [{"id": "m"}]}, True), ({"models": [{"name": "m"}]}, True),
    ([{"slug": "m"}], True), ({"id": "m"}, True), ({"data": []}, False),
    ({"data": [None, {"id": "other"}]}, False), (None, False),
])
def test_model_must_be_present_in_local_catalog(document, expected):
    assert (RT._find_model_entry(document, "m") is not None) == expected


@pytest.mark.parametrize("entry,expected", [
    ({"meta": {"n_ctx": 16000}}, 16000), ({"metadata": {"n_ctx": 8192}}, 8192),
    ({"context_window": 32768}, 32768), ({"n_ctx": -1}, None),
    ({"n_ctx": True}, None), ({"n_ctx": 10000.5}, 10000), ({}, None),
])
def test_context_metadata_is_positive_and_not_boolean(entry, expected):
    assert RT._entry_ctx(entry) == expected


def test_context_server_smaller_than_model_metadata_wins(monkeypatch):
    def fetch(url):
        if url.endswith('/models'):
            return {"data": [{"id": "m", "meta": {"n_ctx": 32768}}]}
        return {"default_generation_settings": {"n_ctx": 8192}}
    monkeypatch.setattr(RT, '_http_get_json', fetch)
    assert RT.server_preflight('http://localhost:8080/v1', 'm') == (True, 8192)
    with pytest.raises(RuntimeError):
        RT.server_preflight('http://localhost:8080/v1', 'missing')


def test_unknown_server_context_uses_safe_fallback(monkeypatch):
    monkeypatch.setattr(RT, '_http_get_json', lambda url: {"data": [{"id": "m"}]})
    assert RT.server_preflight('http://localhost:8080/v1', 'm') == (True, None)


@pytest.mark.parametrize("response", [b'not-json', b'\xff', b'x' * (1024 * 1024 + 1)])
def test_http_invalid_and_oversized_responses_fail_closed(monkeypatch, response):
    class Reply:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            return response[:size]
    class Opener:
        def open(self, *args, **kwargs):
            return Reply()
    monkeypatch.setattr(RT.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(RuntimeError):
        RT._http_get_json('http://localhost:8080/v1/models')


@pytest.mark.parametrize("control", [{"models": []}, 'not-json', [], {"models": [None]}])
def test_invalid_bundled_catalog_cannot_launch_model(monkeypatch, tmp_path, control):
    payload = control if isinstance(control, str) else json.dumps(control)
    monkeypatch.setattr(RT.shutil, 'which', lambda *args, **kwargs: '/fake/codex')
    monkeypatch.setattr(RT.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, payload.encode(), b''))
    with pytest.raises(RuntimeError):
        RT.build_catalog({'PATH': '/fake'}, tmp_path, 'm', 8192, 'worker')


def test_lock_rejects_hardlinks_and_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    lock = tmp_path / 'codex-local/worker.lock'
    lock.parent.mkdir()
    target = tmp_path / 'unrelated'
    target.write_text('keep')
    lock.symlink_to(target)
    with pytest.raises(RuntimeError):
        RT._acquire_worker_lock()
    lock.unlink()
    os.link(target, lock)
    with pytest.raises(RuntimeError):
        RT._acquire_worker_lock()
    assert target.read_text() == 'keep'


def test_normalizing_check_string_does_not_execute_it(tmp_path):
    sentinel = tmp_path / 'not-created'
    raw = _final(checks=[{"command": 'touch ' + str(sentinel), "status": "not_run",
                         "exit_code": None, "evidence": []}])
    normalized = RT.normalize_final_output(json.dumps(raw))
    assert json.loads(normalized)['checks'][0]['command'] == ['touch', str(sentinel)]
    with pytest.raises(ValueError):
        RT.validate_final_output(normalized, valid_spec())
    assert not sentinel.exists()


def test_cli_flags_missing_input_and_unavailable_model(ctl_env, local_server):
    work = ctl_env['work']
    spec = write_spec(work, valid_spec())
    base = ['--cwd', str(work), '--spec', str(spec)]
    for argv in (['--unknown'], [], ['--cwd', str(work / 'missing')],
                 base + ['--timeout', '0'], base + ['--context-window', '8191']):
        assert run_cli(argv).returncode == 2
    assert run_cli(base + ['--model', 'missing-local']).returncode == 1
    for data in ('', ' ', 'x' * 12001):
        assert run_cli(['--cwd', str(work)], spec_text=data).returncode == 2
    assert run_cli(['--cwd', str(work)], spec_text=json.dumps(valid_spec())).returncode == 0
    spec.write_text(' ')
    assert run_cli(base).returncode == 2
    spec.unlink()
    assert run_cli(base).returncode == 2


def test_unknown_context_fallback_is_explicit_in_diagnostic(ctl_env, local_server, monkeypatch):
    work = ctl_env['work']
    spec = write_spec(work, valid_spec())
    monkeypatch.setattr(RT, 'server_preflight', lambda *args: (True, None))
    r = run_cli(['--cwd', str(work), '--spec', str(spec), '--dry-run'], inprocess=True)
    assert r.returncode == 0
    assert json.loads(r.stdout)['effective_context'] == 8192
    assert 'warning' in json.loads(r.stdout)
    monkeypatch.setattr(RT, 'server_preflight', lambda *args: (True, 4096))
    assert run_cli(['--cwd', str(work), '--spec', str(spec)], inprocess=True).returncode == 1


def test_http_connection_timeout_and_redirect_fail_closed(monkeypatch):
    import urllib.error
    for failure in (TimeoutError(), urllib.error.URLError('offline'),
                    urllib.error.HTTPError('http://localhost:8080/v1', 302, 'redirect', {}, None)):
        class Opener:
            def open(self, *args, **kwargs):
                raise failure
        monkeypatch.setattr(RT.urllib.request, 'build_opener', lambda *args: Opener())
        with pytest.raises(RuntimeError):
            RT._http_get_json('http://localhost:8080/v1/models')
    assert RT._BlockRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com') is None


def test_given_build_config_when_effective_then_shell_preserves_recursion_lock(tmp_path):
    rt = tmp_path / "rt"
    rt.mkdir()
    catalog = tmp_path / "model_catalog.json"
    catalog.write_text('{"models": []}')
    work = tmp_path / "work"
    work.mkdir()
    RT.build_config(
        runtime_dir=rt,
        cwd=work,
        mode="plan",
        model="test-local",
        base_url="http://127.0.0.1:8080/v1",
        effective_ctx=8192,
        system_prompt="worker",
        catalog_path=catalog,
        child_path="/usr/bin:/bin",
        child_home=str(rt / "home"),
    )
    text = (rt / "config.toml").read_text(encoding="utf-8")
    assert '[shell_environment_policy]' in text
    assert 'inherit = "none"' in text
    assert '[shell_environment_policy.set]' in text
    assert 'WORKER_ASK_ACTIVE = "1"' in text
    set_section = text.split('[shell_environment_policy.set]')[1]
    assert 'PATH = ' in set_section and 'HOME = ' in set_section
