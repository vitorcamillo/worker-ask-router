"""Testes dos wrappers zcode-worker, codex-ask e claude-ask (contrato de resposta).

Escopo: o contrato de resposta (bloco ```json final) precisa entrar no prompt de
todas as chamadas dos tres wrappers, antes da tarefa, preservando modos, modelos,
permissoes, fallbacks, exit codes e o tratamento do prompt do usuario como dado.

Isolamento: os wrappers sao copiados para um diretorio temporario e executados com
PATH/HOME/TMPDIR proprios; codex, claude, ocx e zcode-worker viram stubs em Python
que apenas capturam argv/env em JSON. Nenhuma conta real, credencial, config real
ou rede e usada. Limites declarados:
- zcode-worker: PREAMBULO extraido por ast.literal_eval e caminho de invocacao
  verificado por AST + chamada funcional de _run_once com CLI stub; o launcher de
  integracao (zcode_worker.cli) nao e executado aqui (usa caminho absoluto da
  maquina); a montagem do prompt por features.build_cli_argv usa o PREAMBULO do
  core injetado, logo o contrato flui pelo mesmo PREAMBULO testado.
- Nao ha teste ponta a ponta com os binarios reais das plataformas (exigiria
  contas e rede).
"""

import ast
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

WRAPPERS_SRC = os.environ.get("WRAPPERS_SOURCE_DIR", str(Path(__file__).resolve().parents[1] / "bin"))
WRAPPER_NAMES = ("zcode-worker", "codex-ask", "claude-ask")
STUB_NAMES = ("codex", "claude", "ocx", "zcode-worker", "zcode-cli")

CONTRACT_START = "--- CONTRATO DE RESPOSTA (obrigatorio) ---"
CONTRACT_END = "--- FIM DO CONTRATO ---"
CONTRACT_TOKENS = (
    "VEREDITO",
    "```json",
    "schema_version",
    "task_id",
    '"unspecified"',
    "execution_state",
    '"completed"',
    '"partial"',
    '"blocked"',
    "changed_files",
    "acceptance",
    "criterion",
    '"met"',
    '"pending"',
    "checks",
    "command",
    "argv",
    '"passed"',
    '"failed"',
    '"not_run"',
    "exit_code",
    "null",
    "evidence",
    "risks",
    "validation_owner",
    '"orchestrator"',
    "approval",
)

STUB_CLI_SOURCE = '''#!/usr/bin/env python3
# Stub de CLI dos testes: captura argv/env em JSON; nunca usa rede nem credencial.
import json
import os
import sys

name = os.path.basename(sys.argv[0])
cap = os.environ.get("STUB_CAPTURE_DIR")
if not cap:
    sys.exit(3)
os.makedirs(cap, exist_ok=True)
index = sum(1 for f in os.listdir(cap) if f.startswith(name + "."))
record = {
    "name": name,
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "anthropic_env": {k: v for k, v in os.environ.items() if k.startswith("ANTHROPIC_")},
}
with open(os.path.join(cap, name + "." + str(index) + ".json"), "w", encoding="utf-8") as fh:
    json.dump(record, fh, ensure_ascii=False, indent=1)

tag = "STUB_BEHAVIOR_" + name.upper().replace("-", "_")
behavior = os.environ.get(tag, "fail_error" if name == "ocx" else "ok")
out_file = None
args = sys.argv[1:]
if name in ("codex", "claude") and args and args[-1].startswith("-") and (len(args) < 2 or args[-2] != "--"): sys.exit(2)
for i in range(len(args) - 1):
    if args[i] == "-o":
        out_file = args[i + 1]
if behavior == "ok":
    if out_file is not None:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write("resposta stub " + name + "\\n")
    print("resposta stub " + name)
    sys.exit(0)
if behavior == "ok_empty":
    sys.exit(0)
if behavior == "fail_quota":
    print("usage limit reached (429 quota exceeded)", file=sys.stderr)
    sys.exit(1)
if behavior == "fail_stdout_quota":
    print("quota exceeded rate limit 429 usage limit")
    print("erro generico do stub " + name, file=sys.stderr)
    sys.exit(1)
if behavior == "fail_stdout_limit_banner":
    print("You've hit your weekly limit \\u00b7 resets Oct 30, 1am (America/Sao_Paulo)")
    sys.exit(1)
if behavior == "fail_model":
    print("ERROR: The 'gpt-x' model is not supported when using Codex with a ChatGPT account.", file=sys.stderr)
    sys.exit(1)
if behavior == "fail_model_claude":
    print("There's an issue with the selected model (claude-x). It may not exist or you may not have access to it.", file=sys.stderr)
    sys.exit(1)
if behavior == "fail_path_quota":
    print("permission denied opening /repo/quota.py", file=sys.stderr)
    sys.exit(1)
if behavior == "fail_quota_exit75":
    print("ERRO: cota do ZCode esgotada", file=sys.stderr)
    sys.exit(75)
print("erro generico do stub " + name, file=sys.stderr)
sys.exit(1)
'''


def extract_contract(text):
    start = text.index(CONTRACT_START)
    end = text.index(CONTRACT_END) + len(CONTRACT_END)
    return text[start:end]


def read_wrapper(name):
    with open(os.path.join(WRAPPERS_SRC, name), encoding="utf-8") as fh:
        return fh.read()


def preamble_of_zcode_worker():
    tree = ast.parse(read_wrapper("zcode-worker"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "PREAMBULO" for t in node.targets)
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("PREAMBULO nao encontrado no zcode-worker")


class WrapperTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wrappers-contract-")
        self.root = os.path.abspath(self._tmp.name)
        self.wrappers = os.path.join(self.root, "wrappers")
        self.bindir = os.path.join(self.root, "bin")
        self.fakehome = os.path.join(self.root, "home")
        self.rundir = os.path.join(self.root, "run")
        self.capdir = os.path.join(self.root, "capture")
        self.cwd = os.path.join(self.root, "cwd")
        for path in (self.wrappers, self.bindir, self.fakehome, self.rundir, self.capdir, self.cwd):
            os.makedirs(path, exist_ok=True)
        for name in WRAPPER_NAMES:
            dst = os.path.join(self.wrappers, name)
            shutil.copyfile(os.path.join(WRAPPERS_SRC, name), dst)
            os.chmod(dst, 0o755)
        stub = os.path.join(self.bindir, "stub-cli")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(STUB_CLI_SOURCE)
        os.chmod(stub, 0o755)
        for name in STUB_NAMES:
            os.symlink("stub-cli", os.path.join(self.bindir, name))

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **extra):
        base = {
            "PATH": self.bindir + ":/usr/bin:/bin",
            "HOME": self.fakehome,
            "TMPDIR": self.rundir,
            "LANG": "C.UTF-8",
            "STUB_CAPTURE_DIR": self.capdir,
        }
        base.update(extra)
        return base

    def run_wrapper(self, name, args, behaviors=None, stdin=None, extra_env=None):
        env = self.env(**(extra_env or {}))
        env.update(behaviors or {})
        kwargs = {"env": env, "capture_output": True, "timeout": 120,
                  "text": True, "encoding": "utf-8", "errors": "replace"}
        if stdin is not None:
            kwargs["input"] = stdin
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        return subprocess.run([os.path.join(self.wrappers, name), *args], **kwargs)

    def captures(self, name):
        records = []
        for fname in sorted(f for f in os.listdir(self.capdir) if f.startswith(name + ".")):
            with open(os.path.join(self.capdir, fname), encoding="utf-8") as fh:
                records.append(json.load(fh))
        return records

    @staticmethod
    def flag(argv, name):
        return argv[argv.index(name) + 1]

    def assert_contract_prefixed(self, prompt, task):
        contrato = extract_contract(read_wrapper("codex-ask"))
        if prompt.startswith("[WORKER — chamado via claude-ask]\n"):
            self.assertIn("[WORKER — chamado via claude-ask]", prompt)
            prompt = prompt[prompt.index(CONTRACT_START):]
        self.assertEqual(prompt, contrato + "\n\n" + task)


class ContratoIdenticoNosTresTest(WrapperTestBase):
    def test_contrato_literal_identico_nos_tres_wrappers(self):
        preamble = preamble_of_zcode_worker()
        codex_src = read_wrapper("codex-ask")
        claude_src = read_wrapper("claude-ask")
        contratos = [
            extract_contract(preamble),
            extract_contract(codex_src),
            extract_contract(claude_src),
        ]
        self.assertEqual(contratos[0], contratos[1])
        self.assertEqual(contratos[0], contratos[2])
        for token in CONTRACT_TOKENS:
            self.assertIn(token, contratos[0], token)

    def test_prefixo_bash_e_heredoc_quotado(self):
        # ancora do primeiro mktemp do FLUXO PRINCIPAL (via_pool usa pout/plog antes)
        primeira_chamada = {"codex-ask": '\nout="$(mktemp)"', "claude-ask": '\nlog="$(mktemp)"'}
        for name in ("codex-ask", "claude-ask"):
            src = read_wrapper(name)
            self.assertIn("<<'CONTRATO_EOF'", src, name)
            concat = 'prompt="$contrato_resposta' if name == "codex-ask" else 'prompt="$preamble'
            self.assertIn(concat, src, name)
            # o concat acontece depois da validacao de modo e antes da primeira chamada
            self.assertLess(src.index("modo invalido"), src.index(concat))
            self.assertLess(src.index(concat),
                            src.index(primeira_chamada[name]), name)


class ZcodeWorkerContratoTest(WrapperTestBase):
    def test_preamble_contem_contrato_antes_da_tarefa(self):
        preamble = preamble_of_zcode_worker()
        self.assertTrue(preamble.startswith("[WORKER"))
        self.assertTrue(preamble.endswith("--- TAREFA ---\n"))
        self.assertLess(preamble.index(CONTRACT_START), preamble.index("--- TAREFA ---"))
        contrato = extract_contract(preamble)
        for token in CONTRACT_TOKENS:
            self.assertIn(token, contrato, token)

    def test_caminho_de_invocacao_usa_preamble_mais_tarefa(self):
        tree = ast.parse(read_wrapper("zcode-worker"))
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            if not any(isinstance(e, ast.Constant) and e.value == "-p" for e in node.elts):
                continue
            for e in node.elts:
                if (
                    isinstance(e, ast.BinOp)
                    and isinstance(e.op, ast.Add)
                    and isinstance(e.left, ast.Name)
                    and e.left.id == "PREAMBULO"
                    and isinstance(e.right, ast.Name)
                    and e.right.id == "task"
                ):
                    found = True
        self.assertTrue(found, "argv do CLI deve conter '-p' com PREAMBULO + task")

    def _load_module(self, module_name):
        provider = os.path.join(self.root, "provider.json")
        with open(provider, "w", encoding="utf-8") as fh:
            json.dump({"config": {"defaultModelSelection": {
                "providerId": "account:zai-individual-coding-plan",
                "modelId": "GLM-5.3-Flash",
                "options": {"reasoningLevel": "low"},
            }}}, fh)
        settings = os.path.join(self.root, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump({"max_parallel": 2}, fh)
        meminfo = os.path.join(self.root, "meminfo")
        with open(meminfo, "w", encoding="ascii") as fh:
            fh.write("MemAvailable: 99999999 kB\n")
        proc_root = os.path.join(self.root, "proc")
        os.makedirs(proc_root, exist_ok=True)
        patch_env = {
            "HOME": self.fakehome,
            "ZCODE_WORKER_CONTEXT_CLI": os.path.join(self.bindir, "zcode-cli"),
            "ZCODE_WORKER_CONTEXT_DB": os.path.join(self.root, "db.sqlite"),
            "ZCODE_WORKER_CONTEXT_PROVIDER_CONFIG": provider,
            "ZCODE_WORKER_CONTEXT_STATE": os.path.join(self.root, "zw-state"),
            "ZCODE_WORKER_CONTEXT_SETTINGS": settings,
            "ZCODE_WORKER_CONTEXT_PROC_ROOT": proc_root,
            "ZCODE_WORKER_CONTEXT_MEMINFO": meminfo,
            "STUB_CAPTURE_DIR": self.capdir,
        }
        loader = importlib.machinery.SourceFileLoader(
            module_name, os.path.join(self.wrappers, "zcode-worker"))
        spec = importlib.util.spec_from_file_location(
            module_name, os.path.join(self.wrappers, "zcode-worker"), loader=loader)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, patch_env):
            loader.exec_module(module)
            return module

    def test_run_once_modo_plan_envia_preamble_com_contrato(self):
        module = self._load_module("zw_copia_plan")
        task = "TAREFA EXEMPLO ZCODE"
        with mock.patch.dict(os.environ, {"STUB_CAPTURE_DIR": self.capdir}):
            returncode, _stdout, _stderr = module._run_once(
                task, self.cwd, "plan", "glm-5.3-flash", "high", time.monotonic() + 60)
        self.assertEqual(returncode, 0)
        chamadas = self.captures("zcode-cli")
        self.assertEqual(len(chamadas), 1)
        argv = chamadas[0]["argv"]
        prompt = self.flag(argv, "-p")
        self.assertEqual(prompt, module.PREAMBULO + task)
        self.assertIn(CONTRACT_START, prompt)
        self.assertTrue(prompt.endswith(task))
        self.assertEqual(self.flag(argv, "--mode"), "plan")
        self.assertEqual(self.flag(argv, "--cwd"), self.cwd)
        self.assertIn("--json", argv)
        self.assertNotIn("--disallowed-tools", argv)

    def test_run_once_modo_edit_mantem_denylist(self):
        module = self._load_module("zw_copia_edit")
        task = "TAREFA EDIT ZCODE"
        with mock.patch.dict(os.environ, {"STUB_CAPTURE_DIR": self.capdir}):
            returncode, _stdout, _stderr = module._run_once(
                task, self.cwd, "edit", "glm-5.3-flash", "high", time.monotonic() + 60)
        self.assertEqual(returncode, 0)
        argv = self.captures("zcode-cli")[0]["argv"]
        prompt = self.flag(argv, "-p")
        self.assertEqual(prompt, module.PREAMBULO + task)
        self.assertIn(CONTRACT_START, prompt)
        self.assertEqual(self.flag(argv, "--mode"), "edit")
        self.assertIn("--disallowed-tools", argv)
        self.assertIn("Bash", argv)
        self.assertIn("mcp__mcp-router__call_tool", argv)


class CodexAskContratoTest(WrapperTestBase):
    def test_worker_plan_luna_readonly_sem_fallback_de_revisao(self):
        proc = self.run_wrapper("codex-ask", ["--cwd", self.cwd, "--mode", "plan", "triagem"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.captures("codex")[0]["argv"]
        self.assertEqual(self.flag(argv, "-m"), "gpt-6-luna")
        self.assertEqual(self.flag(argv, "-s"), "read-only")
        self.assertEqual(self.flag(argv, "-c"), "model_reasoning_effort=high")

    def test_plan_aceita_model_e_effort_explicitos(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "--mode", "plan",
                          "--model", "gpt-6.1-terra", "--effort", "medium", "triagem"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.captures("codex")[0]["argv"]
        self.assertEqual(self.flag(argv, "-m"), "gpt-6.1-terra")
        self.assertEqual(self.flag(argv, "-c"), "model_reasoning_effort=medium")
        self.assertEqual(self.flag(argv, "-s"), "read-only")

    def test_review_ignora_model_e_effort_explicitos(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "--model", "gpt-6.1-sol", "--effort", "low", "tarefa"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.captures("codex")[0]["argv"]
        self.assertEqual(self.flag(argv, "-m"), "gpt-6.1-sol")
        self.assertEqual(self.flag(argv, "-c"), "model_reasoning_effort=medium")

    def test_effort_invalido_exit_1_sem_chamar_cli(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "--effort", "banana", "tarefa"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--model/--effort invalido", proc.stderr)
        self.assertEqual(self.captures("codex"), [])

    def test_model_invalido_exit_1_sem_chamar_cli(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "--model", "bad;model", "tarefa"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--model/--effort invalido", proc.stderr)
        self.assertEqual(self.captures("codex"), [])

    def test_review_modo_readonly_e_prompt_com_contrato(self):
        task = "tarefa exemplo codex"
        proc = self.run_wrapper("codex-ask", ["--cwd", self.cwd, task])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("resposta stub codex", proc.stdout)
        self.assertIn("modo=review gpt-6.1-sol", proc.stdout)
        chamadas = self.captures("codex")
        self.assertEqual(len(chamadas), 1)
        argv = chamadas[0]["argv"]
        self.assertEqual(self.flag(argv, "-m"), "gpt-6.1-sol")
        self.assertEqual(self.flag(argv, "-c"), "model_reasoning_effort=medium")
        self.assertEqual(self.flag(argv, "-s"), "read-only")
        self.assertEqual(self.flag(argv, "-C"), self.cwd)
        self.assert_contract_prefixed(argv[-1], task)
        # sucesso nativo nao aciona nenhum degrau
        self.assertEqual(self.captures("ocx"), [])
        self.assertEqual(self.captures("zcode-worker"), [])
        self.assertEqual(self.captures("claude"), [])

    def test_write_modo_preserva_modelo_e_sandbox(self):
        task = "tarefa write codex"
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "--mode", "write", task])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.captures("codex")[0]["argv"]
        self.assertEqual(self.flag(argv, "-m"), "gpt-6-luna")
        self.assertEqual(self.flag(argv, "-s"), "workspace-write")
        self.assertEqual(self.flag(argv, "-c"), "model_reasoning_effort=high")
        self.assert_contract_prefixed(argv[-1], task)

    def test_prompt_malicioso_e_dado_nao_executavel(self):
        pwn1 = os.path.join(self.capdir, "PWNED1")
        pwn2 = os.path.join(self.capdir, "PWNED2")
        task = ("revise $(touch %s) e `touch %s` e $HOME e ${PWD}" % (pwn1, pwn2))
        proc = self.run_wrapper("codex-ask", ["--cwd", self.cwd, task])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(pwn1))
        self.assertFalse(os.path.exists(pwn2))
        prompt = self.captures("codex")[0]["argv"][-1]
        self.assertIn("$(touch", prompt)
        self.assertIn("`touch", prompt)
        self.assertIn("$HOME", prompt)
        self.assertIn(CONTRACT_START, prompt)

    def test_erro_nao_cota_exit_1_sem_fallback(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_error"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self.captures("codex")), 1)
        self.assertEqual(self.captures("ocx"), [])
        self.assertEqual(self.captures("zcode-worker"), [])
        self.assertEqual(self.captures("claude"), [])

    def test_quota_so_no_stdout_nao_vira_cota(self):
        # Palavra de cota em texto do modelo (stdout) é conteúdo, não erro
        # estruturado: falha comum continua exit 1, sem fallback.
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa sobre quota"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_stdout_quota"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.captures("claude"), [])
        self.assertEqual(self.captures("zcode-worker"), [])

    def test_palavra_quota_em_caminho_nao_e_cota(self):
        # "permission denied opening /repo/quota.py" é erro comum: a palavra
        # "quota" dentro de um caminho não pode virar rc75 (falso positivo).
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_path_quota"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.captures("claude"), [])
        self.assertEqual(self.captures("zcode-worker"), [])

    def test_stderr_bruto_fica_no_diag_privado(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_error"})
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("erro generico do stub codex", proc.stderr)
        diag = os.path.join(
            self.fakehome, ".local/state/codex-ask/last-error.log")
        with open(diag, encoding="utf-8") as fh:
            self.assertIn("erro generico do stub codex", fh.read())

    def test_muse_rc1_para_sem_chamar_glm(self):
        os.symlink("stub-cli", os.path.join(self.bindir, "muse-ask"))
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_CLAUDE": "fail_quota",
                       "STUB_BEHAVIOR_MUSE_ASK": "fail_error",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "ok"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len(self.captures("muse-ask")), 1)
        self.assertEqual(self.captures("zcode-worker"), [])

    def test_muse_rc0_resposta_vazia_vira_erro_sem_descer_glm(self):
        # rc0 com saida vazia nao e entrega: vira erro comum, para no proprio
        # degrau com exit 1 em vez de encerrar a revisao com exit 0 sem resposta.
        os.symlink("stub-cli", os.path.join(self.bindir, "muse-ask"))
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_CLAUDE": "fail_quota",
                       "STUB_BEHAVIOR_MUSE_ASK": "ok_empty",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "ok"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("saida vazia", proc.stderr)
        self.assertEqual(len(self.captures("muse-ask")), 1)
        self.assertEqual(self.captures("zcode-worker"), [])

    def test_glm_rc0_resposta_vazia_vira_erro_exit_1(self):
        # Ultimo degrau com rc0 e saida vazia: erro comum, exit 1 sem anunciar
        # cadeia esgotada (75 seria falso esgotamento de cota).
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_CLAUDE": "fail_quota",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "ok_empty"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("saida vazia", proc.stderr)
        self.assertNotIn("cadeia de revisao esgotada", proc.stderr)
        self.assertEqual(len(self.captures("zcode-worker")), 1)

    def test_cota_aciona_opus_medium_com_contrato(self):
        task = "tarefa revisao em cota"
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, task],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.captures("codex")), 1)  # so o nativo (Sol); sem degrau extra
        claude = self.captures("claude")
        self.assertEqual(len(claude), 1)
        self.assertEqual(self.flag(claude[0]["argv"], "--model"), "claude-opus-5-5")
        self.assertEqual(self.flag(claude[0]["argv"], "--effort"), "medium")
        self.assertIn(CONTRACT_START, claude[0]["argv"][-1])
        self.assertTrue(claude[0]["argv"][-1].endswith(task))
        self.assertEqual(self.captures("zcode-worker"), [])

    def test_modelo_indisponivel_desce_para_opus_sem_pool(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_model"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.captures("codex")), 1)
        self.assertEqual(self.captures("ocx"), [])  # pool usaria o mesmo modelo
        claude = self.captures("claude")
        self.assertEqual(len(claude), 1)
        self.assertEqual(self.flag(claude[0]["argv"], "--model"), "claude-opus-5-5")

    def test_modelo_claude_invalido_desce_para_glm(self):
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_CLAUDE": "fail_model_claude",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "ok"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("degrau GLM", proc.stdout)
        self.assertEqual(len(self.captures("claude")), 1)

    def test_cota_sem_claude_aciona_degrau_glm_modo_plan(self):
        task = "tarefa revisao em cota"
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, task],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_CLAUDE": "fail_quota",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "ok"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("degrau GLM", proc.stdout)
        glm = self.captures("zcode-worker")
        self.assertEqual(len(glm), 1)
        self.assertEqual(self.flag(glm[0]["argv"], "--mode"), "plan")
        self.assertEqual(self.flag(glm[0]["argv"], "--model"), "GLM-5.3")
        self.assertEqual(self.flag(glm[0]["argv"], "--effort"), "high")
        self.assertEqual(self.flag(glm[0]["argv"], "--cwd"), self.cwd)
        self.assertIn(CONTRACT_START, glm[0]["argv"][-1])
        self.assertTrue(glm[0]["argv"][-1].endswith(task))

    def test_cota_claude_somente_stdout_desce_para_glm(self):
        task = "tarefa revisao em cota"
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, task],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_CLAUDE": "fail_stdout_limit_banner",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "ok"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("degrau GLM", proc.stdout)
        glm = self.captures("zcode-worker")
        self.assertEqual(len(glm), 1)
        self.assertIn(CONTRACT_START, glm[0]["argv"][-1])
        self.assertTrue(glm[0]["argv"][-1].endswith(task))

    def test_glm_erro_comum_interrompe_cadeia_exit_1(self):
        # Falha comum (rc1) do último degrau GLM não é cota: o contrato manda
        # parar preservando o código, sem anunciar cadeia esgotada.
        task = "tarefa revisao sem cota nenhuma"
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, task],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "fail_error",
                       "STUB_BEHAVIOR_CLAUDE": "fail_quota"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("cadeia de revisao esgotada", proc.stderr)
        self.assertIn("nao-cota", proc.stderr)
        self.assertEqual(len(self.captures("codex")), 1)
        self.assertEqual(len(self.captures("claude")), 1)
        self.assertEqual(len(self.captures("zcode-worker")), 1)

    def test_cota_cadeia_completa_exit_75_so_com_stubs(self):
        # Esgotamento real: todos os degraus devolvem cota de verdade (o
        # zcode-worker sai 75 em QuotaError), até acabar a cadeia.
        task = "tarefa revisao sem cota nenhuma"
        proc = self.run_wrapper(
            "codex-ask", ["--cwd", self.cwd, task],
            behaviors={"STUB_BEHAVIOR_CODEX": "fail_quota",
                       "STUB_BEHAVIOR_ZCODE_WORKER": "fail_quota_exit75",
                       "STUB_BEHAVIOR_CLAUDE": "fail_quota"})
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn("cadeia de revisao esgotada", proc.stderr)
        self.assertEqual(len(self.captures("codex")), 1)
        self.assertEqual(len(self.captures("ocx")), 0)  # pool legado removido
        self.assertEqual(len(self.captures("zcode-worker")), 1)
        claude = self.captures("claude")
        self.assertEqual(len(claude), 1)  # so opus 5.5 medium
        for chamada in claude:
            prompt = chamada["argv"][-1]
            self.assertIn(CONTRACT_START, prompt)
            self.assertTrue(prompt.endswith(task))


class ClaudeAskContratoTest(WrapperTestBase):
    def test_plan_padrao_permissoes_e_contrato(self):
        task = "tarefa exemplo claude"
        proc = self.run_wrapper("claude-ask", ["--cwd", self.cwd, task])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("resposta stub claude", proc.stdout)
        self.assertIn("modo=plan modelo=padrao", proc.stdout)
        argv = self.captures("claude")[0]["argv"]
        self.assertIn("-p", argv)
        self.assertEqual(self.flag(argv, "--permission-mode"), "plan")
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)
        self.assert_contract_prefixed(argv[-1], task)

    def test_write_model_effort_preservados(self):
        task = "tarefa write claude"
        proc = self.run_wrapper(
            "claude-ask",
            ["--cwd", self.cwd, "--mode", "write", "--model", "fable",
             "--effort", "high", task])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.captures("claude")[0]["argv"]
        self.assertEqual(self.flag(argv, "--permission-mode"), "acceptEdits")
        self.assertEqual(self.flag(argv, "--model"), "fable")
        self.assertEqual(self.flag(argv, "--effort"), "high")
        self.assert_contract_prefixed(argv[-1], task)

    def test_remove_anthropic_env_na_chamada(self):
        proc = self.run_wrapper(
            "claude-ask", ["--cwd", self.cwd, "tarefa env"],
            extra_env={"ANTHROPIC_BASE_URL": "http://stub-proxy.invalid",
                       "ANTHROPIC_AUTH_TOKEN": "stub-nao-e-segredo"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.captures("claude")[0]["anthropic_env"], {})

    def test_prompt_via_stdin_tambem_recebe_contrato(self):
        proc = self.run_wrapper("claude-ask", ["--cwd", self.cwd],
                                stdin="tarefa por stdin\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_contract_prefixed(
            self.captures("claude")[0]["argv"][-1], "tarefa por stdin")

    def test_prompt_malicioso_e_dado_nao_executavel(self):
        pwn = os.path.join(self.capdir, "PWNED")
        task = "analise $(touch %s) e `touch %s`" % (pwn, pwn)
        proc = self.run_wrapper("claude-ask", ["--cwd", self.cwd, task])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(pwn))
        prompt = self.captures("claude")[0]["argv"][-1]
        self.assertIn("$(touch", prompt)
        self.assertIn("`touch", prompt)
        self.assertIn(CONTRACT_START, prompt)

    def test_cota_exit_75(self):
        proc = self.run_wrapper(
            "claude-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CLAUDE": "fail_quota"})
        self.assertEqual(proc.returncode, 75)

    def test_erro_nao_cota_exit_1(self):
        proc = self.run_wrapper(
            "claude-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CLAUDE": "fail_error"})
        self.assertEqual(proc.returncode, 1)

    def test_quota_so_no_stdout_nao_vira_cota(self):
        proc = self.run_wrapper(
            "claude-ask", ["--cwd", self.cwd, "tarefa sobre quota"],
            behaviors={"STUB_BEHAVIOR_CLAUDE": "fail_stdout_quota"})
        self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_palavra_quota_em_caminho_nao_e_cota(self):
        # "permission denied opening /repo/quota.py" é erro comum: a palavra
        # "quota" dentro de um caminho não pode virar rc75 (falso positivo).
        proc = self.run_wrapper(
            "claude-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CLAUDE": "fail_path_quota"})
        self.assertEqual(proc.returncode, 1)

    def test_limite_no_stdout_com_stderr_vazio_vira_cota_75(self):
        proc = self.run_wrapper(
            "claude-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CLAUDE": "fail_stdout_limit_banner"})
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertNotIn("You've hit", proc.stderr)

    def test_stderr_bruto_fica_no_diag_privado(self):
        proc = self.run_wrapper(
            "claude-ask", ["--cwd", self.cwd, "tarefa"],
            behaviors={"STUB_BEHAVIOR_CLAUDE": "fail_error"})
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("erro generico do stub claude", proc.stderr)
        diag = os.path.join(
            self.fakehome, ".local/state/claude-ask/last-error.log")
        with open(diag, encoding="utf-8") as fh:
            self.assertIn("erro generico do stub claude", fh.read())

    def test_uso_sem_cwd_exit_1_sem_chamar_cli(self):
        for name in ("codex-ask", "claude-ask"):
            proc = self.run_wrapper(name, ["somente prompt"])
            self.assertEqual(proc.returncode, 1, name)
            self.assertIn("uso:", proc.stderr, name)
        self.assertEqual(os.listdir(self.capdir), [])


if __name__ == "__main__":
    unittest.main()
