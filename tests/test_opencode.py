import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = Path(os.environ.get("GUARDS_SRC", ROOT / "bin")).resolve()
OPENCODE_ASK = SRC_DIR / "opencode-ask"
PYTHON = sys.executable


FAKE_OC_SOURCE = r'''#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import time

args = sys.argv[1:]


def _oc_active():
    db = os.environ.get("FAKE_OC_DB")
    if not db:
        return ""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            row = con.execute(
                "SELECT label FROM credential WHERE integration_id='opencode-go' AND active=1"
            ).fetchone()
        finally:
            con.close()
        return row[0] if row else ""
    except Exception:
        return ""


# auth nunca vai ao log de modelos
if args and args[0] == "auth":
    ok = False
    if len(args) == 4 and args[1] == "switch" and args[2] == "opencode-go":
        label = args[3]
        db = os.environ.get("FAKE_OC_DB")
        if db:
            try:
                con = sqlite3.connect(db)
                try:
                    existe = con.execute(
                        "SELECT COUNT(*) FROM credential "
                        "WHERE integration_id='opencode-go' AND label=?",
                        (label,)).fetchone()[0]
                    if existe:
                        con.execute("UPDATE credential SET active=0 "
                                    "WHERE integration_id='opencode-go'")
                        con.execute("UPDATE credential SET active=1 "
                                    "WHERE integration_id='opencode-go' AND label=?",
                                    (label,))
                        con.commit()
                        ok = True
                finally:
                    con.close()
            except Exception:
                ok = False
    if ok:
        auth_log = os.environ.get("FAKE_OC_AUTH_LOG")
        if auth_log:
            with open(auth_log, "a", encoding="utf-8") as fh:
                fh.write("switch " + args[3] + "\n")
        sys.exit(0)
    sys.exit(1)

model = ""
if "-m" in args:
    model = args[args.index("-m") + 1]
if "#" in model:
    model = model.split("#", 1)[0]

log = os.environ.get("FAKE_OC_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(model + "\n")

# session export path
if len(args) >= 2 and args[0] == "session" and args[1] == "export":
    if os.environ.get("FAKE_OC_EXPORT_FAIL") == "1":
        sys.exit(1)
    print(json.dumps({"info": {"tokens": {"input": 1, "output": 1}, "cost": 0.0}}))
    sys.exit(0)

sleep = float(os.environ.get("FAKE_OC_SLEEP", "0") or 0)
if sleep:
    time.sleep(sleep)

quota_models = set((os.environ.get("FAKE_OC_QUOTA_MODELS") or "").split())
quota_accounts = [a for a in (os.environ.get("FAKE_OC_QUOTA_ACCOUNTS") or "").split("|") if a]
noroute_models = set((os.environ.get("FAKE_OC_NOROUTE_MODELS") or "").split())
generic_fail = os.environ.get("FAKE_OC_GENERIC_FAIL") == "1"
text = os.environ.get("FAKE_OC_TEXT", "ok")

conta_esgotada = (model.startswith("opencode-go/") and not model.endswith("-free")
                  and bool(quota_accounts) and _oc_active() in quota_accounts)

if model in quota_models or conta_esgotada:
    size = int(os.environ.get("FAKE_OC_STDERR_SIZE", "0") or 0)
    msg = "quota exceeded: rate limit reached\n"
    sys.stderr.write(msg * max(1, size // len(msg) + 1) if size else msg)
    if size:
        sys.stderr.write("x" * max(0, size - (len(msg) * (size // len(msg) + 1))) )
    sys.exit(1)

if model in noroute_models:
    sys.stderr.write(json.dumps({"type": "provider.no-route", "message": "Model unavailable: " + model}) + "\n")
    sys.exit(1)

if generic_fail:
    sys.stderr.write("boom: common failure\n")
    sys.exit(1)

event = {"type": "text", "sessionID": "s1", "part": {"text": text, "messageID": "m1"}}
print(json.dumps(event))
print(json.dumps({"type": "step_finish", "part": {"tokens": {"input": 1, "output": 1}, "cost": 0.0}}))
sys.exit(int(os.environ.get("FAKE_OC_EXIT", "0") or 0))
'''


class OpencodeAskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        # fake systemd-run returning 1 (scopes disabled)
        systemd = self.bindir / "systemd-run"
        systemd.write_text("#!/bin/sh\nexit 1\n")
        systemd.chmod(0o755)
        # Isola apenas o namespace dos locks de vaga; não disputa workers reais.
        self.lock_id = "fixture-" + self.tmp.name.rsplit("/", 1)[-1]
        fake_id = self.bindir / "id"
        fake_id.write_text("#!/bin/sh\nprintf '%s\\n' '" + self.lock_id + "'\n")
        fake_id.chmod(0o755)
        # fake oc
        self.oc = self.bindir / "fake-oc"
        self.oc.write_text(FAKE_OC_SOURCE)
        self.oc.chmod(0o755)
        self.log = Path(self.tmp.name) / "models.log"
        # catalog local
        cache = self.home / ".cache" / "opencode"
        cache.mkdir(parents=True)
        catalog = {
            "opencode-go": {
                "models": {
                    "gpt-6-luna": {"cost": {"output": 0.5}},
                    "claude-haiku-5-5": {"cost": {"output": 0.5}},
                    "mimo-v2.6-flash": {"cost": {"output": 0.28}},
                    "qwen3.8-flash": {"cost": {"output": 0.47}},
                    "deepseek-v4.1-flash": {"cost": {"output": 0.6}},
                    "expensive": {"cost": {"output": 15.0}},
                }
            }
        }
        (cache / "models.json").write_text(json.dumps(catalog))
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("OPENCODE_ASK_") or key in (
                    "CLAUDE_ASK_WORKER", "XDG_DATA_HOME", "WORKER_ASK_QUOTA_HELPER"):
                self.env.pop(key, None)
        self.env["HOME"] = str(self.home)
        self.env["OPENCODE_ASK_BIN"] = str(self.oc)
        self.env["FAKE_OC_LOG"] = str(self.log)
        self.env["FAKE_OC_AUTH_LOG"] = str(Path(self.tmp.name) / "auth.log")
        # Banco inexistente: rotacao de contas inativa nos testes antigos.
        self.env["OPENCODE_ASK_OC_DB"] = str(Path(self.tmp.name) / "oc-inexistente.db")
        self.env["PATH"] = str(self.bindir) + os.pathsep + self.env.get("PATH", "")
        curl = self.bindir / "curl"
        curl.write_text("#!/bin/sh\\necho refused >&2\\nexit 1\\n")
        curl.chmod(0o755)

    def tearDown(self):
        for pattern in (".vaga*", ".conta.lock"):
            for path in Path("/tmp").glob("opencode-ask-" + self.lock_id + pattern):
                path.unlink(missing_ok=True)
        self.tmp.cleanup()

    def _run(self, argv, env_extra=None, timeout=60):
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", str(OPENCODE_ASK)] + argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    def _accounts_db(self, labels=(("Conta A", 1, 1), ("Conta B", 0, 2))):
        db = Path(self.tmp.name) / "opencode.db"
        con = sqlite3.connect(db)
        try:
            con.execute(
                "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, "
                "value TEXT, active INTEGER, time_created INTEGER)")
            for i, (label, active, criado) in enumerate(labels, start=1):
                con.execute(
                    "INSERT INTO credential (id, integration_id, label, value, active, time_created) "
                    "VALUES (?, 'opencode-go', ?, 'x', ?, ?)",
                    ("id%d" % i, label, active, criado))
            con.commit()
        finally:
            con.close()
        self.env["OPENCODE_ASK_OC_DB"] = str(db)
        self.env["FAKE_OC_DB"] = str(db)
        return db

    def _state_path(self):
        return self.home / ".local" / "state" / "opencode-ask" / "go-contas.json"

    def _active_label(self):
        con = sqlite3.connect(self.env["FAKE_OC_DB"])
        try:
            row = con.execute(
                "SELECT label FROM credential "
                "WHERE integration_id='opencode-go' AND active=1").fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def _auth_log(self):
        p = Path(self.env["FAKE_OC_AUTH_LOG"])
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def _logged(self):
        return [m for m in (self.log.read_text().splitlines() if self.log.exists() else []) if m]

    def test_stderr_only_quota_triggers_beam(self):
        # Cota detectada so no stderr em todos os degraus antes do beam: a cadeia chega nele.
        antes_beam = ("opencode-go/gpt-6-luna opencode-go/claude-haiku-5-5 "
                      "opencode-go/mimo-v2.6-flash opencode-go/qwen3.8-flash "
                      "opencode-go/deepseek-v4.1-flash opencode/deepseek-v4.1-flash-free")
        env_extra = {"FAKE_OC_QUOTA_MODELS": antes_beam}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertIn("opencode-go/beam-free", logged)

    def test_quota_on_frees_only_no_fallback(self):
        # Com os Go saudaveis, cota nos free nem chega a ser atingida: o auto roda o padrao.
        env_extra = {"FAKE_OC_QUOTA_MODELS": "opencode/deepseek-v4.1-flash-free opencode-go/beam-free"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        logged = [m for m in (self.log.read_text().splitlines() if self.log.exists() else []) if m]
        self.assertEqual(logged, ["opencode-go/deepseek-v4.1-flash"])

    def test_all_go_quota_reaches_free(self):
        # Cota em todos os Go baratos do auto: a cadeia chega no free do provider opencode.
        todos_go = ("opencode-go/gpt-6-luna opencode-go/claude-haiku-5-5 "
                    "opencode-go/mimo-v2.6-flash opencode-go/qwen3.8-flash "
                    "opencode-go/deepseek-v4.1-flash")
        env_extra = {"FAKE_OC_QUOTA_MODELS": todos_go}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertIn("opencode/deepseek-v4.1-flash-free", logged)
        self.assertNotIn("opencode-go/beam-free", logged)

    # Regressão: "Model unavailable" (provider.no-route) também dispara o fallback.
    def test_noroute_chain_triggers_beam(self):
        antes_beam = ("opencode-go/gpt-6-luna opencode-go/claude-haiku-5-5 "
                      "opencode-go/mimo-v2.6-flash opencode-go/qwen3.8-flash "
                      "opencode-go/deepseek-v4.1-flash opencode/deepseek-v4.1-flash-free")
        env_extra = {"FAKE_OC_NOROUTE_MODELS": antes_beam}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertIn("opencode-go/beam-free", logged)

    def test_noroute_all_exits_75(self):
        todos = ("opencode-go/deepseek-v4.1-flash opencode-go/gpt-6-luna opencode-go/claude-haiku-5-5 "
                 "opencode-go/mimo-v2.6-flash opencode-go/qwen3.8-flash "
                 "opencode/deepseek-v4.1-flash-free opencode-go/beam-free")
        env_extra = {"FAKE_OC_NOROUTE_MODELS": todos}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 75, (r.returncode, r.stderr))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertEqual(logged, todos.split())
        self.assertIn("worker-ask-quota-reset:", r.stderr)

    def test_noroute_explicit_model_no_fallback(self):
        env_extra = {"FAKE_OC_NOROUTE_MODELS": "opencode-go/deepseek-v4.1-flash"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "--model", "opencode-go/deepseek-v4.1-flash", "task"],
                      env_extra=env_extra)
        self.assertEqual(r.returncode, 75, (r.returncode, r.stderr))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertEqual(logged, ["opencode-go/deepseek-v4.1-flash"])

    def test_auto_go_expensive_blocked(self):
        # deepseek-v4.1-flash caro no catalogo: e pulado no meio da cadeia, sem executar.
        catalog = {"opencode-go": {"models": {
            "gpt-6-luna": {"cost": {"output": 0.5}},
            "claude-haiku-5-5": {"cost": {"output": 0.5}},
            "mimo-v2.6-flash": {"cost": {"output": 0.28}},
            "qwen3.8-flash": {"cost": {"output": 0.47}},
            "deepseek-v4.1-flash": {"cost": {"output": 15.0}},
        }}}
        (self.home / ".cache" / "opencode" / "models.json").write_text(json.dumps(catalog))
        quota = ("opencode-go/gpt-6-luna opencode-go/claude-haiku-5-5 opencode-go/mimo-v2.6-flash "
                 "opencode-go/qwen3.8-flash opencode/deepseek-v4.1-flash-free opencode-go/beam-free")
        env_extra = {"FAKE_OC_QUOTA_MODELS": quota}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 75, (r.returncode, r.stderr))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertNotIn("opencode-go/deepseek-v4.1-flash", logged)
        self.assertIn("bloqueado pela politica", r.stderr)

    def test_auto_no_price_starts_free(self):
        # Sem catalogo, todo Go fica sem preco: o auto comeca no free (nao morre com rc 1).
        (self.home / ".cache" / "opencode" / "models.json").write_text(json.dumps({}))
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"])
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        logged = [m for m in (self.log.read_text().splitlines() if self.log.exists() else []) if m]
        self.assertEqual(logged, ["opencode/deepseek-v4.1-flash-free"])
        self.assertIn("auto comeca em", r.stderr)

    def test_explicit_go_no_price_blocked(self):
        # Modelo Go pedido explicitamente sem preco no catalogo: bloqueado (rc 1), sem fallback.
        (self.home / ".cache" / "opencode" / "models.json").write_text(json.dumps({}))
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "--model", "go-luna", "task"])
        self.assertEqual(r.returncode, 1, (r.returncode, r.stderr))
        logged = [m for m in (self.log.read_text().splitlines() if self.log.exists() else []) if m]
        self.assertEqual(logged, [])
        self.assertIn("bloqueado", r.stderr)

    def test_auto_deepseek_expensive_starts_luna(self):
        # deepseek-v4.1-flash caro no catalogo: o auto comeca no proximo degrau (gpt-6-luna), sem rc 1.
        catalog = {"opencode-go": {"models": {
            "deepseek-v4.1-flash": {"cost": {"output": 15.0}},
            "gpt-6-luna": {"cost": {"output": 0.5}},
            "claude-haiku-5-5": {"cost": {"output": 0.5}},
            "mimo-v2.6-flash": {"cost": {"output": 0.28}},
            "qwen3.8-flash": {"cost": {"output": 0.47}},
        }}}
        (self.home / ".cache" / "opencode" / "models.json").write_text(json.dumps(catalog))
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"])
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        logged = [m for m in (self.log.read_text().splitlines() if self.log.exists() else []) if m]
        self.assertEqual(logged, ["opencode-go/gpt-6-luna"])
        self.assertIn("auto comeca em opencode-go/gpt-6-luna", r.stderr)

    def test_auto_go_cheap_runs(self):
        # Primeiro degrau do auto em cota: cai para o segundo Go barato (gpt-6-luna).
        env_extra = {"FAKE_OC_QUOTA_MODELS": "opencode-go/deepseek-v4.1-flash"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertIn("opencode-go/gpt-6-luna", logged)

    def test_go_expensive_blocked(self):
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "--model", "opencode-go/expensive", "task"])
        self.assertEqual(r.returncode, 1, (r.returncode, r.stderr))

    def test_go_no_price_blocked(self):
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "--model", "opencode-go/nope", "task"])
        self.assertEqual(r.returncode, 1, (r.returncode, r.stderr))

    def test_explicit_model_no_fallback(self):
        env_extra = {"FAKE_OC_QUOTA_MODELS": "opencode/deepseek-v4.1-flash-free"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "--model", "deepseek", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 75, (r.returncode, r.stderr))

    def test_model_text_quota_no_fallback(self):
        env_extra = {"FAKE_OC_TEXT": "rate limit reached"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))

    def test_common_failure_not_fallback(self):
        env_extra = {"FAKE_OC_GENERIC_FAIL": "1"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra)
        self.assertEqual(r.returncode, 1, (r.returncode, r.stderr))

    def test_timeout_not_fallback(self):
        env_extra = {"FAKE_OC_SLEEP": "3", "OPENCODE_ASK_TIMEOUT": "1"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra, timeout=30)
        self.assertEqual(r.returncode, 124, (r.returncode, r.stderr))

    def test_long_stderr_quota_sigpipe(self):
        antes_beam = ("opencode-go/gpt-6-luna opencode-go/claude-haiku-5-5 "
                      "opencode-go/mimo-v2.6-flash opencode-go/qwen3.8-flash "
                      "opencode-go/deepseek-v4.1-flash opencode/deepseek-v4.1-flash-free")
        env_extra = {"FAKE_OC_QUOTA_MODELS": antes_beam, "FAKE_OC_STDERR_SIZE": "2000000"}
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"], env_extra=env_extra, timeout=60)
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))

    def test_rotacao_conta_a_cota_gira_para_b(self):
        # Cota so na Conta A: gira para a Conta B e repete o mesmo modelo.
        self._accounts_db()
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"],
                      env_extra={"FAKE_OC_QUOTA_ACCOUNTS": "Conta A"})
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertEqual(self._logged(), ["opencode-go/deepseek-v4.1-flash"] * 2)
        self.assertEqual(self._auth_log(), ["switch Conta B"])
        self.assertEqual(self._active_label(), "Conta B")
        estado = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertGreater(estado["Conta A"], time.time())
        self.assertIn("conta Go 'Conta A'", r.stderr)
        self.assertIn("usando 'Conta B'", r.stderr)

    def test_rotacao_cota_nas_duas_contas_uma_troca(self):
        # Sem conta disponivel depois da troca, desce para o free com exatamente 1 switch.
        self._accounts_db()
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"],
                      env_extra={"FAKE_OC_QUOTA_ACCOUNTS": "Conta A|Conta B"})
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertIn("opencode/deepseek-v4.1-flash-free", self._logged())
        self.assertEqual(len(self._auth_log()), 1)

    def test_rotacao_desligada_comportamento_antigo(self):
        self._accounts_db()
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"],
                      env_extra={"FAKE_OC_QUOTA_ACCOUNTS": "Conta A",
                                 "OPENCODE_ASK_ACCOUNT_ROTATE": "0"})
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertEqual(self._auth_log(), [])
        self.assertIn("opencode/deepseek-v4.1-flash-free", self._logged())

    def test_rotacao_pre_troca_conta_esgotada(self):
        # Estado marca a Conta A esgotada: troca antes de rodar, sem gastar chamada nela.
        self._accounts_db()
        p = self._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"Conta A": time.time() + 3600}), encoding="utf-8")
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"])
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertEqual(self._logged(), ["opencode-go/deepseek-v4.1-flash"])
        self.assertEqual(self._auth_log(), ["switch Conta B"])
        self.assertIn("ainda sem cota", r.stderr)

    def test_rotacao_noroute_nao_gira_conta(self):
        self._accounts_db()
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"],
                      env_extra={"FAKE_OC_NOROUTE_MODELS": "opencode-go/deepseek-v4.1-flash"})
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertEqual(self._auth_log(), [])

    def test_rotacao_conta_unica_nao_gira(self):
        self._accounts_db(labels=(("Conta A", 1, 1),))
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"],
                      env_extra={"FAKE_OC_QUOTA_ACCOUNTS": "Conta A"})
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertEqual(self._auth_log(), [])
        self.assertIn("opencode/deepseek-v4.1-flash-free", self._logged())

    def _write_limits(self, content):
        d = self.home / ".config" / "opencode"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "context-limits.json"
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        return p

    def _generated_cfg(self):
        p = self.home / ".local" / "share" / "opencode-ask" / "xdg-plan" / "opencode" / "opencode.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_context_limits_malformed_ignored_safe(self):
        self._write_limits("{nao-json-marcador-xyz,,,")
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"])
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertIn("AVISO", r.stderr)
        self.assertNotIn("nao-json-marcador-xyz", r.stderr)
        gen = self._generated_cfg()
        self.assertNotIn("providers", gen)
        self.assertNotIn("compaction", gen)

    def test_context_limits_root_non_object_ignored(self):
        self._write_limits("[1, 2, 3]")
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"])
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertIn("AVISO", r.stderr)
        gen = self._generated_cfg()
        self.assertNotIn("providers", gen)
        self.assertNotIn("compaction", gen)

    def test_context_limits_non_object_values_ignored(self):
        self._write_limits(json.dumps({"providers": "MARCA-NAO-OBJETO", "compaction": [1, 2]}))
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"])
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertIn("AVISO", r.stderr)
        self.assertNotIn("MARCA-NAO-OBJETO", r.stderr)
        gen = self._generated_cfg()
        self.assertNotIn("providers", gen)
        self.assertNotIn("compaction", gen)

    def test_context_limits_valid_objects_merged(self):
        self._write_limits(json.dumps({"providers": {"opencode": {"x": 1}},
                                       "compaction": {"auto": True},
                                       "evil": "MARCA-MALICIOSA"}))
        r = self._run(["--cwd", str(self.home), "--mode", "plan", "task"])
        self.assertEqual(r.returncode, 0, (r.returncode, r.stderr))
        self.assertNotIn("MARCA-MALICIOSA", r.stderr)
        gen = self._generated_cfg()
        self.assertEqual(gen.get("providers"), {"opencode": {"x": 1}})
        self.assertEqual(gen.get("compaction"), {"auto": True})
        self.assertNotIn("evil", gen)

    def test_context_limits_unreadable_guarded_in_source(self):
        src = OPENCODE_ASK.read_text(encoding="utf-8")
        self.assertIn("except (OSError, ValueError)", src)
        self.assertIn("raiz nao objeto", src)


if __name__ == "__main__":
    unittest.main()
